"""Field-level symmetric encryption using Fernet (AES-128-CBC + HMAC-SHA256).

Used to encrypt organization-specific payment gateway secrets (key_secret,
webhook_secret) at rest in the database.
"""

import logging
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, status

from app.core.config import settings

logger = logging.getLogger("crm.encryption")


def get_fernet() -> Fernet:
    """Return a Fernet cipher instance using the configured FIELD_ENCRYPTION_KEY.

    Raises HTTP 503 if encryption key is not configured or invalid.
    """
    if not settings.field_encryption_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Field encryption is not configured",
        )
    try:
        return Fernet(settings.field_encryption_key.encode("utf-8"))
    except Exception as exc:
        logger.error("Invalid FIELD_ENCRYPTION_KEY format: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Field encryption key is invalid",
        )


def encrypt_field(value: str) -> str:
    """Encrypt a plaintext string and return base64-encoded ciphertext."""
    if not value:
        return ""
    fernet = get_fernet()
    return fernet.encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_field(ciphertext: str) -> str:
    """Decrypt a base64-encoded ciphertext and return plaintext string."""
    if not ciphertext:
        return ""
    fernet = get_fernet()
    try:
        return fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        logger.error("Failed to decrypt field with current FIELD_ENCRYPTION_KEY")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to decrypt secret credentials",
        )
