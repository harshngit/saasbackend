"""One-off conversion of inline base64 uploads into stored files.

Everything used to be written into the record as `data:image/png;base64,…`, so a
single response could carry megabytes of image data. Uploads now go to
`stored_files` and the record keeps a `/files/{id}` URL. This walks the existing
rows and does the same to what is already there, so old records stop returning
blobs too.

Idempotent: a value that is already a URL is skipped, so it is safe on every boot.
"""

import logging
from typing import NamedTuple

from app.core.files import decode_data_url, save_bytes

logger = logging.getLogger("crm.files")

# (model name, [single-URL columns], [JSON list columns whose dicts carry a "url"])
TARGETS: list[tuple[str, list[str], list[str]]] = [
    ("Organization", [
        "logo_url", "signature_url", "stamp_url", "letterhead_url", "banner_url",
        "payment_qr_url", "doc_gst_url", "doc_pan_url", "doc_coi_url",
        "doc_trade_license_url", "doc_msme_url", "doc_fssai_url", "doc_other_url",
        "auth_person_photo_url", "auth_person_signature_url",
    ], ["doc_other_files"]),
    ("User", [
        "profile_photo", "identity_proof_file", "identify_proofs",
        "resume_cv", "offer_letter", "appointment_letter",
    ], ["uploaded_documents", "experience_certificates", "educational_certificates"]),
    ("Product", [
        "cover_image", "product_video", "product_catalog_brochure", "product_manual",
        "download_file", "product_datasheet", "compliance_certificate", "warranty_document",
    ], ["other_attachments", "images"]),
    ("Category", ["image"], []),
    # NOT ["documents"]: Customer.documents is a SQLAlchemy relationship to
    # CustomerDocument, not a JSON/list column — treating it as one crashed this
    # entire function (AttributeError from calling .get("url") on a
    # CustomerDocument ORM instance), aborting every model listed after this one.
    # CustomerDocument.url is always created via save_upload() (see
    # app/routers/customers.py) and never holds a legacy data: URL, so there is
    # nothing to convert here anyway.
    ("Customer", ["doc_gst_url", "doc_pan_url"], []),
    ("Vehicle", ["rc_document_url", "insurance_document_url", "fitness_document_url", "puc_document_url"], []),
    ("Expense", ["receipt_url", "vendor_invoice_url"], ["supporting_documents"]),
    ("PurchaseInvoice", [
        "attachment_url", "supplier_quotation_url", "purchase_order_url",
        "supplier_invoice_url", "delivery_challan_url",
    ], ["supporting_documents"]),
]

_EXTENSIONS = {
    "image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg", "image/gif": "gif",
    "image/webp": "webp", "image/svg+xml": "svg", "application/pdf": "pdf",
    "video/mp4": "mp4", "application/zip": "zip",
}


def _filename(model_name: str, column: str, content_type: str) -> str:
    return f"{model_name.lower()}-{column}.{_EXTENSIONS.get(content_type, 'bin')}"


def _convert_model(db, model_name: str, model, single_columns, list_columns) -> int:
    """Convert one model's rows. Raises on failure — the caller decides what
    that means for the rest of the run (see convert_inline_uploads)."""
    converted = 0
    for row in db.query(model).all():
        org_id = getattr(row, "organization_id", None) or getattr(row, "id", None)

        for column in single_columns:
            value = getattr(row, column, None)
            if not isinstance(value, str) or not value.startswith("data:"):
                continue
            decoded = decode_data_url(value)
            if decoded is None:
                continue
            content, content_type = decoded
            setattr(row, column, save_bytes(
                db, org_id, content, _filename(model_name, column, content_type), content_type
            ))
            converted += 1

        for column in list_columns:
            entries = getattr(row, column, None)
            if not isinstance(entries, list) or not entries:
                continue
            changed = False
            rebuilt = []
            for entry in entries:
                # `images` is a list of plain strings; the document slots
                # are dicts with a "url" key.
                target = entry if isinstance(entry, str) else (entry or {}).get("url")
                decoded = decode_data_url(target) if isinstance(target, str) else None
                if decoded is None:
                    rebuilt.append(entry)
                    continue
                content, content_type = decoded
                url = save_bytes(
                    db, org_id, content,
                    (entry.get("name") if isinstance(entry, dict) else None)
                    or _filename(model_name, column, content_type),
                    content_type,
                )
                rebuilt.append(url if isinstance(entry, str) else {**entry, "url": url})
                changed = True
                converted += 1
            if changed:
                # Reassign wholesale — SQLAlchemy does not track in-place
                # mutation of a JSON column.
                setattr(row, column, rebuilt)
    return converted


class ConversionResult(NamedTuple):
    converted: int
    failed_models: list[str]


def convert_inline_uploads() -> ConversionResult:
    """Replace every stored `data:` URL with a link to a stored file.

    Each model in TARGETS gets its own failure boundary: a model that raises
    is rolled back and logged, and every *other* model in the list still runs
    — one bad model must never silently prevent the rest from converting, the
    way the old single-try/except-for-everything shape did. Returns a summary
    (rather than raising) so both the startup step and the CLI entry point
    (app/scripts/convert_inline_uploads.py) can report exactly what happened;
    the CLI turns a non-empty `failed_models` into a non-zero exit status.
    """
    from app import models
    from app.core.database import SessionLocal

    db = SessionLocal()
    converted = 0
    failed: list[str] = []
    try:
        for model_name, single_columns, list_columns in TARGETS:
            model = getattr(models, model_name, None)
            if model is None:
                continue
            try:
                converted += _convert_model(db, model_name, model, single_columns, list_columns)
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
                failed.append(model_name)
                logger.exception("Inline-upload conversion failed for model %s — other models still ran", model_name)
        if converted:
            logger.info("Converted %d inline uploads into stored files", converted)
        if failed:
            logger.warning("Inline-upload conversion had failures for: %s", ", ".join(failed))
    finally:
        db.close()
    return ConversionResult(converted=converted, failed_models=failed)
