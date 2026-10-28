import base64
import logging
import re
import unicodedata
from urllib.parse import quote

from fastapi import HTTPException, Request, UploadFile, status
from sqlalchemy.orm import Session

from app.core.config import settings

# Uploads are stored as rows in `stored_files` and handed back as a URL that
# points at GET /files/{id}. Nothing is base64'd into an API response any more —
# a product or employee payload carries a link, not the image itself.
#
# Swapping this for S3/Cloudinary means changing `save_upload` alone: everything
# downstream already treats the value as an opaque URL string.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_IMAGE_BYTES = 5 * 1024 * 1024    # 5 MB — the sheet's limit for images

FILES_PATH = "/files"

_DATA_URL_RE = re.compile(r"^data:([^;,]+)?(;base64)?,(.*)$", re.S)

_MAX_FILENAME = 150


def safe_filename(name: str | None) -> str:
    """A client-supplied filename reduced to something safe to store and to put in a
    header: the last path segment only, no control characters (so no CR/LF), no quotes
    or backslashes, at most 150 characters. Unicode letters are kept."""
    base = re.split(r"[\\/]", name or "")[-1]
    cleaned = "".join(
        ch for ch in base if unicodedata.category(ch)[0] != "C" and ch not in '"\\'
    ).strip(" .")
    if len(cleaned) > _MAX_FILENAME:
        stem, dot, ext = cleaned.rpartition(".")
        cleaned = (stem[: _MAX_FILENAME - len(ext) - 1] + dot + ext) if dot and len(ext) <= 10 else cleaned[:_MAX_FILENAME]
    return cleaned or "file"


def document_filename(name: str) -> str:
    """A download name built from document numbers: path separators (as in
    `INV/2024/001`) become "-" rather than cutting the name down to its last part."""
    return name.replace("/", "-").replace("\\", "-")


def content_disposition(filename: str | None, disposition: str = "inline") -> str:
    """A Content-Disposition value that is always plain ASCII: an ASCII `filename`
    for every client plus RFC 6266 `filename*` carrying the UTF-8 name when it differs.
    Sanitizes again, so rows stored before uploads were sanitized are safe too."""
    name = safe_filename(filename)
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    ascii_name = re.sub(r"[^A-Za-z0-9 ._()\-]", "_", ascii_name).strip(" .") or "file"
    value = f'{disposition}; filename="{ascii_name}"'
    if ascii_name != name:
        value += f"; filename*=UTF-8''{quote(name, safe='')}"
    return value


def _check_type(
    content_type: str, allow_pdf: bool, allow_video: bool, allow_any: bool
) -> None:
    if allow_any:
        return
    allowed = ("image/",) + (("application/pdf",) if allow_pdf else ()) + (("video/",) if allow_video else ())
    if not any(content_type.startswith(p) or content_type == p for p in allowed):
        kinds = ["an image"] + (["a PDF"] if allow_pdf else []) + (["a video"] if allow_video else [])
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File must be {' or '.join(kinds)}",
        )


def public_url(request: Request | None, file_id: str) -> str:
    """Relative reference for a stored file: /files/{id}.

    Deliberately host-independent. This backend has already moved host three
    times (Render -> EC2 -> Cloudflare Workers) — a stored/returned URL that
    embedded one of those hosts would break the moment the app moved to the
    next one. `GET /files/{id}` itself still works from any host that's
    actually serving the API; callers resolve a relative reference against
    whichever host they're already talking to (the frontend's own
    `getFileUrl()` already does exactly this).

    `request` is accepted but unused — kept so every existing call site
    (`save_upload`, `save_bytes`, `customer_profile_service.py`) keeps
    working unchanged.
    """
    return f"{FILES_PATH}/{file_id}"


def _with_public_base(path: str) -> str:
    """Join PUBLIC_BASE_URL with a `/files/...`-style path, trailing-slash safe.

    Response-layer only: the database always keeps storing `path` as-is
    (relative). When PUBLIC_BASE_URL is unset, returns `path` unchanged —
    that is the existing, expected behavior for every environment that
    hasn't configured it (e.g. local dev, tests).
    """
    base = settings.public_base_url.strip()
    if not base:
        return path
    return f"{base.rstrip('/')}{path}"


def normalize_file_url(value: str | None) -> str | None:
    """Make a `*_url` field usable no matter what shape ended up stored in it,
    and make it absolute for the API response when PUBLIC_BASE_URL is configured.

    Every one of these columns is meant to hold a ready-to-use reference
    (`/files/{id}`, an absolute URL, or a legacy `data:` URI), but several of
    them accept a plain string from the client on create/update with no
    format check — so a caller that pastes a bare `file_id` (e.g. the `file_id`
    field of POST /files/upload's response instead of its `url` field) ends up
    with that raw id stored verbatim. Applied at the response-schema boundary
    so the API is correct regardless of what's already in the database, with
    no data migration required.

    Bare id -> {PUBLIC_BASE_URL}/files/{id} (or /files/{id} if unset); already
    relative `/...` -> prefixed with PUBLIC_BASE_URL the same way; already
    `http(s)://...` or `data:...` -> unchanged (never rewrites an
    already-absolute URL, whatever host it names); None/blank -> None.

    The database itself never sees this function's output — it only runs
    when building a response, so DB rows stay host-independent regardless of
    what PUBLIC_BASE_URL is set to.
    """
    if value is None:
        return None
    val = value.strip()
    if not val:
        return None
    if val.startswith("http://") or val.startswith("https://") or val.startswith("data:"):
        return val
    if val.startswith("/"):
        return _with_public_base(val)
    return _with_public_base(f"{FILES_PATH}/{val}")


def _persist(
    db: Session, org_id: str | None, filename: str, content: bytes, content_type: str,
) -> "StoredFile":  # noqa: F821
    """Create the StoredFile row and, when R2 is configured, upload the bytes
    to it. Shared by save_upload/save_bytes so there is exactly one place that
    decides DB-backed vs. R2-backed storage.

    The row's id is generated up front (rather than left to the column default)
    so the R2 key can be derived from it before the row is even added — and if
    the R2 upload itself fails, no row is created at all: a StoredFile must
    never claim an R2 object exists that was never actually written.
    """
    import uuid

    from app.core.config import settings
    from app.models.stored_file import StoredFile

    # 1. Enforce storage quota before object upload or database write
    if org_id:
        from app.services import storage_service
        storage_service.check_storage_quota(org_id, len(content), db)

    stored = StoredFile(
        id=str(uuid.uuid4()),  # generated up front so the R2 key can use it before the row exists
        organization_id=org_id,
        filename=safe_filename(filename),
        content_type=content_type or "application/octet-stream",
        size=len(content),
    )

    r2_uploaded_key: str | None = None
    if settings.r2_configured:
        from app.core import r2

        key = r2.object_key(org_id, stored.id)
        r2.put_object(key, content, stored.content_type)  # raises on failure — nothing persisted below if so
        r2_uploaded_key = key
        stored.storage_key = key
        stored.data = None
    else:
        stored.data = content

    try:
        db.add(stored)
        db.flush()

        # 2. Atomically reserve quota and increment accounted storage usage under row lock
        if org_id:
            from app.services import storage_service
            storage_service.record_file_upload_serialized(org_id, stored.size, db)
    except Exception:
        # If DB write or serialized quota enforcement fails, clean up the object in R2 immediately
        if r2_uploaded_key:
            try:
                from app.core import r2
                r2.delete_object(r2_uploaded_key)
            except Exception:
                pass
        raise

    return stored


def extract_file_id_from_url(url: str | None) -> str | None:
    """Extract UUID / file_id from a /files/{id} or absolute URL."""
    if not url or not isinstance(url, str):
        return None
    val = url.strip()
    if not val or val.startswith("data:"):
        return None
    parts = val.split("/files/")
    if len(parts) > 1:
        extracted = parts[-1].split("?")[0].split("/")[0].strip()
        return extracted or None
    # Might be a bare UUID
    if len(val) == 36 and "-" in val:
        return val
    return None


def delete_stored_file(
    db: Session,
    stored_or_id: object,
    org_id: str | None = None,
) -> bool:
    """Delete a StoredFile row, remove R2 object (best effort), and decrement storage usage.

    Returns True if deleted, False if not found.
    Raises HTTPException(404) if org_id is provided and does not match stored.organization_id.
    """
    import logging
    from app.models.stored_file import StoredFile

    logger = logging.getLogger("crm.files")

    if isinstance(stored_or_id, StoredFile):
        stored = stored_or_id
    else:
        file_id = extract_file_id_from_url(str(stored_or_id)) or str(stored_or_id)
        stored = db.get(StoredFile, file_id)

    if stored is None:
        return False

    if org_id is not None and stored.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    file_size = stored.size or 0
    file_org_id = stored.organization_id

    if stored.storage_key:
        try:
            from app.core import r2
            if not r2.delete_object(stored.storage_key):
                logger.warning("R2 object delete failed for file_id=%s — DB row deleted anyway", stored.id)
        except Exception as e:
            logger.warning("R2 deletion error for file_id=%s: %s", stored.id, e)

    db.delete(stored)
    db.flush()

    if file_org_id:
        from app.services import storage_service
        storage_service.record_file_deletion(file_org_id, file_size, db)

    return True


def safe_delete_unreferenced_file(
    db: Session,
    old_url_or_id: str | None,
    org_id: str | None = None,
) -> bool:
    """If old_url_or_id points to a StoredFile that is no longer referenced anywhere else, delete it."""
    file_id = extract_file_id_from_url(old_url_or_id)
    if not file_id:
        return False

    from app.models.stored_file import StoredFile
    stored = db.get(StoredFile, file_id)
    if not stored:
        return False

    if org_id is not None and stored.organization_id != org_id:
        return False

    if file_is_referenced(db, file_id):
        return False

    return delete_stored_file(db, stored, org_id)


def file_is_referenced(db: Session, file_id: str) -> bool:
    """True when any file-holding column anywhere still points at `file_id`."""
    pattern = f"%{file_id}%"

    from sqlalchemy import cast, Text, or_
    from app.models import (
        Category,
        Customer,
        CustomerDocument,
        CustomerPayment,
        Delivery,
        DeliveryCollection,
        Expense,
        Organization,
        OrganizationTheme,
        Product,
        ProductVariant,
        PurchaseInvoice,
        SupplierInvoice,
        User,
    )

    # 1. Organization branding & compliance documents
    org_refs = db.query(Organization).filter(
        or_(
            Organization.logo_url.like(pattern),
            Organization.signature_url.like(pattern),
            Organization.stamp_url.like(pattern),
            Organization.letterhead_url.like(pattern),
            Organization.banner_url.like(pattern),
            Organization.payment_qr_url.like(pattern),
            Organization.doc_gst_url.like(pattern),
            Organization.doc_pan_url.like(pattern),
            Organization.doc_coi_url.like(pattern),
            Organization.doc_trade_license_url.like(pattern),
            Organization.doc_msme_url.like(pattern),
            Organization.doc_fssai_url.like(pattern),
            Organization.doc_other_url.like(pattern),
            Organization.auth_person_photo_url.like(pattern),
            Organization.auth_person_signature_url.like(pattern),
            cast(Organization.doc_other_files, Text).like(pattern),
        )
    ).count()
    if org_refs > 0:
        return True

    # 2. Product images, brochures, manuals, attachments, variants
    product_refs = db.query(Product).filter(
        or_(
            Product.cover_image.like(pattern),
            Product.product_video.like(pattern),
            Product.product_catalog_brochure.like(pattern),
            Product.product_manual.like(pattern),
            Product.download_file.like(pattern),
            Product.warranty_document.like(pattern),
            cast(Product.images, Text).like(pattern),
            cast(Product.other_attachments, Text).like(pattern),
        )
    ).count()
    if product_refs > 0:
        return True

    variant_refs = db.query(ProductVariant).filter(
        ProductVariant.image_url.like(pattern)
    ).count()
    if variant_refs > 0:
        return True

    # 3. Expense receipts & vendor invoices
    expense_refs = db.query(Expense).filter(
        or_(
            Expense.receipt_url.like(pattern),
            Expense.vendor_invoice_url.like(pattern),
            cast(Expense.supporting_documents, Text).like(pattern),
        )
    ).count()
    if expense_refs > 0:
        return True

    # 4. User profile photos & employee documents
    user_refs = db.query(User).filter(
        or_(
            User.profile_photo.like(pattern),
            User.identity_proof_file.like(pattern),
            cast(User.uploaded_documents, Text).like(pattern),
            cast(User.experience_certificates, Text).like(pattern),
            cast(User.educational_certificates, Text).like(pattern),
        )
    ).count()
    if user_refs > 0:
        return True

    # 5. Customer documents & payment proofs
    cust_refs = db.query(Customer).filter(
        Customer.profile_image_id.like(pattern),
    ).count()
    if cust_refs > 0:
        return True

    pay_refs = db.query(CustomerPayment).filter(
        CustomerPayment.payment_proof_url.like(pattern),
    ).count()
    if pay_refs > 0:
        return True

    doc_refs = db.query(CustomerDocument).filter(
        or_(
            CustomerDocument.file_id == file_id,
            CustomerDocument.url.like(pattern),
        )
    ).count()
    if doc_refs > 0:
        return True

    # 6. Organization Theme background
    theme_refs = db.query(OrganizationTheme).filter(
        OrganizationTheme.background_image_url.like(pattern)
    ).count()
    if theme_refs > 0:
        return True

    # 7. Purchase & Supplier Invoices
    pi_refs = db.query(PurchaseInvoice).filter(
        or_(
            PurchaseInvoice.attachment_url.like(pattern),
            PurchaseInvoice.supplier_quotation_url.like(pattern),
            PurchaseInvoice.purchase_order_url.like(pattern),
            PurchaseInvoice.supplier_invoice_url.like(pattern),
            PurchaseInvoice.delivery_challan_url.like(pattern),
            cast(PurchaseInvoice.supporting_documents, Text).like(pattern),
        )
    ).count()
    if pi_refs > 0:
        return True

    si_refs = db.query(SupplierInvoice).filter(
        SupplierInvoice.attachment_url.like(pattern)
    ).count()
    if si_refs > 0:
        return True

    # 8. Delivery proof of delivery / payment
    del_refs = db.query(Delivery).filter(
        cast(Delivery.pod_photo_file_ids, Text).like(pattern)
    ).count()
    if del_refs > 0:
        return True

    dc_refs = db.query(DeliveryCollection).filter(
        DeliveryCollection.payment_proof_url.like(pattern)
    ).count()
    if dc_refs > 0:
        return True

    # 9. Category images
    cat_refs = db.query(Category).filter(
        Category.image.like(pattern)
    ).count()
    if cat_refs > 0:
        return True

    return False


def delete_replaced_file(db: Session, old_url_or_id: str | None, org_id: str | None) -> None:
    """Call AFTER committing the change that dropped the reference to `old_url_or_id`:
    deletes that file when it belongs to `org_id`'s scope (None = platform) and nothing
    references it any more. Best effort — a failure is logged and leaves an orphaned
    file, never a broken reference, and never undoes the committed change.

    Order, so a concurrent request that attaches the same file wins over the cleanup:
    1. lock the StoredFile row (Postgres; serializes cleanups of one file), check every
       reference, delete the row and its storage accounting, commit;
    2. check the references again: one committed meanwhile restores the row from a
       snapshot (the bytes are still there) and nothing else happens;
    3. only then delete the R2 object. Failing there leaves an orphaned object, which is
       logged — no row points at it any more.
    Attach paths do not lock files, so a reference committed after step 2 can still
    point at a removed file; that is the same as attaching any stale URL."""
    file_id = extract_file_id_from_url(old_url_or_id)
    if not file_id:
        return
    from app.models.stored_file import StoredFile
    from app.services import storage_service

    log = logging.getLogger("crm.files")
    try:
        stored = db.query(StoredFile).filter(StoredFile.id == file_id).with_for_update().first()
        if stored is None or stored.organization_id != org_id or file_is_referenced(db, file_id):
            db.rollback()  # releases the lock
            return
        snapshot = {column.key: getattr(stored, column.key) for column in StoredFile.__table__.columns}
        db.delete(stored)
        db.flush()
        storage_service.record_file_deletion(org_id, snapshot["size"] or 0, db)
        db.commit()
    except Exception:
        db.rollback()
        log.warning("Could not delete replaced file_id=%s", file_id, exc_info=True)
        return

    try:
        if file_is_referenced(db, file_id):
            db.add(StoredFile(**snapshot))
            db.flush()
            storage_service.record_file_restored(org_id, snapshot["size"] or 0, db)
            db.commit()
            log.info("Kept file_id=%s: it was attached elsewhere while being cleaned up", file_id)
            return
    except Exception:
        db.rollback()
        # The row could not be put back, so keep the bytes: never delete them here.
        log.error("Could not restore file_id=%s after a late reference; its storage object was kept",
                  file_id, exc_info=True)
        return

    key = snapshot.get("storage_key")
    if key:
        try:
            from app.core import r2
            if not r2.delete_object(key):
                log.warning("R2 object for removed file_id=%s was not deleted (orphaned)", file_id)
        except Exception:
            log.warning("R2 object for removed file_id=%s was not deleted (orphaned)", file_id, exc_info=True)


def save_upload(
    db: Session,
    org_id: str | None,
    file: UploadFile,
    request: Request | None = None,
    allow_pdf: bool = True,
    allow_video: bool = False,
    allow_any: bool = False,
    max_bytes: int = MAX_UPLOAD_BYTES,
) -> tuple[str, int]:
    """Validate and store one upload. Returns its public URL and byte size.

    The returned URL/size contract is unchanged regardless of whether R2 is
    configured — every existing caller keeps working without modification.
    """
    content_type = file.content_type or "application/octet-stream"
    _check_type(content_type, allow_pdf, allow_video, allow_any)

    content = file.file.read()
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large (max {max_bytes // (1024 * 1024)} MB)",
        )

    stored = _persist(db, org_id, file.filename or "upload", content, content_type)
    return public_url(request, stored.id), stored.size


def save_bytes(
    db: Session,
    org_id: str | None,
    content: bytes,
    filename: str,
    content_type: str,
    request: Request | None = None,
) -> str:
    """Store raw bytes (used when converting old inline data: URLs)."""
    stored = _persist(db, org_id, filename, content, content_type)
    return public_url(request, stored.id)


def get_bytes(stored: "StoredFile") -> bytes:  # noqa: F821
    """The actual bytes for a StoredFile row, regardless of where they live —
    the one shared helper every internal consumer (PDF generation, branding,
    document rows, …) should use instead of touching `.data`/`.storage_key`
    directly, so DB-backed and R2-backed rows behave identically to callers.

    Raises if neither location has the bytes (a row that's neither
    storage_key-set nor data-set is a data-integrity problem, not something to
    paper over with an empty result).
    """
    if stored.storage_key:
        from app.core import r2

        return r2.get_object_bytes(stored.storage_key)
    if stored.data is not None:
        return stored.data
    raise ValueError(f"StoredFile {stored.id} has neither storage_key nor data")


def decode_data_url(value: str) -> tuple[bytes, str] | None:
    """Split a legacy `data:<type>;base64,<payload>` string into bytes and type."""
    match = _DATA_URL_RE.match(value or "")
    if not match:
        return None
    content_type = match.group(1) or "application/octet-stream"
    payload = match.group(3)
    try:
        content = base64.b64decode(payload) if match.group(2) else payload.encode()
    except Exception:  # noqa: BLE001
        return None
    return content, content_type


# --- Backwards compatibility -------------------------------------------------
# Callers that still want the old inline behaviour. Kept so nothing breaks
# mid-migration; new code should use save_upload.


def read_upload(
    file: UploadFile,
    allow_pdf: bool = True,
    allow_video: bool = False,
    allow_any: bool = False,
    max_bytes: int = MAX_UPLOAD_BYTES,
) -> tuple[str, int]:
    """Deprecated: returns a base64 data: URL rather than a stored-file link."""
    content_type = file.content_type or "application/octet-stream"
    _check_type(content_type, allow_pdf, allow_video, allow_any)
    content = file.file.read()
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large (max {max_bytes // (1024 * 1024)} MB)",
        )
    encoded = base64.b64encode(content).decode("ascii")
    return f"data:{content_type};base64,{encoded}", len(content)


def store_upload(file: UploadFile, **kwargs: object) -> str:
    """Deprecated: base64 data: URL. Use save_upload."""
    url, _size = read_upload(file, **kwargs)  # type: ignore[arg-type]
    return url
