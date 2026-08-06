import base64

from fastapi import HTTPException, UploadFile, status

# No S3 yet: files are stored as base64 data: URLs (persist in the DB, work on
# Render's ephemeral disk). Swap for S3/Cloudinary later — callers keep a URL string.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
_ALLOWED_PREFIXES = ("image/", "application/pdf")


def read_upload(
    file: UploadFile,
    allow_pdf: bool = True,
    allow_video: bool = False,
    allow_any: bool = False,
    max_bytes: int = MAX_UPLOAD_BYTES,
) -> tuple[str, int]:
    """Validate + read an upload, returning its base64 data: URL and byte size.

    Images are always accepted; PDFs by default. `allow_video` also takes
    video/*, and `allow_any` skips the type check entirely (for slots like a
    digital product's download file, which can be any format).
    """
    content_type = file.content_type or "application/octet-stream"
    if not allow_any:
        allowed = ("image/",) + (("application/pdf",) if allow_pdf else ()) + (("video/",) if allow_video else ())
        if not any(content_type.startswith(p) or content_type == p for p in allowed):
            kinds = ["an image"] + (["a PDF"] if allow_pdf else []) + (["a video"] if allow_video else [])
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"File must be {' or '.join(kinds)}",
            )
    content = file.file.read()
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large (max {max_bytes // (1024 * 1024)} MB)",
        )
    encoded = base64.b64encode(content).decode("ascii")
    return f"data:{content_type};base64,{encoded}", len(content)


def store_upload(file: UploadFile, **kwargs: object) -> str:
    """Read an uploaded image (or PDF) and return it as a base64 data: URL."""
    url, _size = read_upload(file, **kwargs)  # type: ignore[arg-type]
    return url
