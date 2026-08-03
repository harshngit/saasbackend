import base64

from fastapi import HTTPException, UploadFile, status

# No S3 yet: files are stored as base64 data: URLs (persist in the DB, work on
# Render's ephemeral disk). Swap for S3/Cloudinary later — callers keep a URL string.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
_ALLOWED_PREFIXES = ("image/", "application/pdf")


def store_upload(file: UploadFile, allow_pdf: bool = True) -> str:
    """Read an uploaded image (or PDF) and return it as a base64 data: URL."""
    content_type = file.content_type or ""
    allowed = _ALLOWED_PREFIXES if allow_pdf else ("image/",)
    if not any(content_type.startswith(p) or content_type == p for p in allowed):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File must be an image" + (" or PDF" if allow_pdf else ""),
        )
    content = file.file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="File too large (max 10 MB)")
    encoded = base64.b64encode(content).decode("ascii")
    return f"data:{content_type};base64,{encoded}"
