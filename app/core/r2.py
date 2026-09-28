"""Cloudflare R2 (S3-compatible) object storage — Phase 1 code path only.

R2 is used only when `settings.r2_configured` is true (every one of
R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_BUCKET_NAME set).
Until then nothing in this module is ever called — file storage stays exactly
as it is today (bytes in `stored_files.data`).

The bucket is private: no public access is enabled, no `r2.dev` URL is ever
generated. Reads go through a short-lived (~1 hour) presigned GET URL instead.

One boto3 client per process, created lazily on first use (never at import
time) and reused — building it requires `settings.r2_account_id`/credentials
to already be configured, which isn't true for most of this app's lifetime
(R2 is optional), and constructing a boto3 client is not free.
"""

import logging
import threading

logger = logging.getLogger("crm.r2")

_client_lock = threading.Lock()
_client = None


def _get_client():
    """The shared boto3 S3 client, pointed at this account's R2 endpoint.

    Built once per process on first call and cached — callers never construct
    their own client, so there is exactly one connection pool to R2 per worker.
    """
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            import boto3

            from app.core.config import settings

            _client = boto3.client(
                "s3",
                endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
                region_name="auto",
                aws_access_key_id=settings.r2_access_key_id,
                aws_secret_access_key=settings.r2_secret_access_key,
            )
    return _client


def object_key(organization_id: str | None, stored_file_id: str) -> str:
    """The R2 key for one stored file — stable, deterministic, never reused."""
    org_segment = organization_id or "platform"
    return f"org/{org_segment}/{stored_file_id}"


def put_object(key: str, data: bytes, content_type: str) -> None:
    """Upload bytes to R2 under `key`. Raises on failure — callers must not
    create/keep a StoredFile row claiming an object exists if this raises."""
    from app.core.config import settings

    _get_client().put_object(
        Bucket=settings.r2_bucket_name,
        Key=key,
        Body=data,
        ContentType=content_type or "application/octet-stream",
    )


def head_object(key: str) -> dict | None:
    """Metadata (incl. ContentLength) for `key`, or None if it doesn't exist /
    can't be reached. Never raises — callers treat None as "not verifiable"."""
    from app.core.config import settings

    try:
        return _get_client().head_object(Bucket=settings.r2_bucket_name, Key=key)
    except Exception:  # noqa: BLE001 — any client/boto error, including NoSuchKey
        logger.warning("R2 head_object failed for key=%s", key, exc_info=True)
        return None


def get_object_bytes(key: str) -> bytes:
    """The raw bytes of `key`. Raises on failure — this is used by internal
    backend consumers (PDF generation, etc.) that need the actual bytes and
    have no sensible fallback if the object can't be fetched."""
    from app.core.config import settings

    obj = _get_client().get_object(Bucket=settings.r2_bucket_name, Key=key)
    return obj["Body"].read()


def presigned_get_url(
    key: str,
    *,
    content_type: str | None = None,
    content_disposition: str | None = None,
    expires_in: int = 3600,
) -> str:
    """A short-lived (default ~1 hour) GET URL for `key`, with the response's
    Content-Type/Content-Disposition set from the StoredFile's own metadata
    rather than whatever R2 happens to have stored them as."""
    from app.core.config import settings

    params = {"Bucket": settings.r2_bucket_name, "Key": key}
    if content_type:
        params["ResponseContentType"] = content_type
    if content_disposition:
        params["ResponseContentDisposition"] = content_disposition
    return _get_client().generate_presigned_url(
        "get_object", Params=params, ExpiresIn=expires_in
    )


def delete_object(key: str) -> bool:
    """Delete `key` from R2. Never raises — returns whether it succeeded, so a
    caller can log-and-continue rather than fail the whole request over a
    storage-cleanup error (see app/routers/files.py's delete endpoint)."""
    from app.core.config import settings

    try:
        _get_client().delete_object(Bucket=settings.r2_bucket_name, Key=key)
        return True
    except Exception:  # noqa: BLE001
        logger.warning("R2 delete_object failed for key=%s", key, exc_info=True)
        return False
