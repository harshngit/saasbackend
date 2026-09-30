import base64
import re

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

    stored = StoredFile(
        id=str(uuid.uuid4()),  # generated up front so the R2 key can use it before the row exists
        organization_id=org_id,
        filename=filename,
        content_type=content_type or "application/octet-stream",
        size=len(content),
    )

    if settings.r2_configured:
        from app.core import r2

        key = r2.object_key(org_id, stored.id)
        r2.put_object(key, content, stored.content_type)  # raises on failure — nothing persisted below if so
        stored.storage_key = key
        stored.data = None
    else:
        stored.data = content

    db.add(stored)
    db.flush()
    return stored


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
