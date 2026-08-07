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
    """Absolute URL for a stored file.

    Uses PUBLIC_BASE_URL when it is configured (so links stay stable behind a
    proxy or custom domain) and otherwise derives the host from the request that
    uploaded the file.
    """
    base = (settings.public_base_url or "").rstrip("/")
    if not base and request is not None:
        base = str(request.base_url).rstrip("/")
    return f"{base}{FILES_PATH}/{file_id}"


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
    """Validate and store one upload. Returns its public URL and byte size."""
    from app.models.stored_file import StoredFile

    content_type = file.content_type or "application/octet-stream"
    _check_type(content_type, allow_pdf, allow_video, allow_any)

    content = file.file.read()
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large (max {max_bytes // (1024 * 1024)} MB)",
        )

    stored = StoredFile(
        organization_id=org_id,
        filename=file.filename or "upload",
        content_type=content_type,
        size=len(content),
        data=content,
    )
    db.add(stored)
    db.flush()  # assigns the id we build the URL from
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
    from app.models.stored_file import StoredFile

    stored = StoredFile(
        organization_id=org_id,
        filename=filename,
        content_type=content_type or "application/octet-stream",
        size=len(content),
        data=content,
    )
    db.add(stored)
    db.flush()
    return public_url(request, stored.id)


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
