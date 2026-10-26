import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt

from app.core.config import settings

ACCESS_TOKEN = "access"
REFRESH_TOKEN = "refresh"


def as_utc(moment: datetime) -> datetime:
    """A stored timestamp as an aware UTC datetime, for expiry comparisons.

    SQLite hands back naive UTC values; PostgreSQL hands back aware values in the
    session's time zone. Those must be converted, not relabelled: `replace(tzinfo=utc)`
    on an IST value would shift an expiry by 5h30m.
    """
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def _create_token(subject: str, token_type: str, expires_delta: timedelta, extra: dict[str, Any]) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "iat": now,
        "exp": now + expires_delta,
        "jti": uuid.uuid4().hex,  # unique per token so rapid re-issues never collide
        **extra,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_access_token(user_id: str, role: str, organization_id: str | None) -> str:
    return _create_token(
        subject=user_id,
        token_type=ACCESS_TOKEN,
        expires_delta=timedelta(minutes=settings.access_token_expire_minutes),
        extra={"role": role, "org": organization_id},
    )


def create_refresh_token(user_id: str) -> str:
    return _create_token(
        subject=user_id,
        token_type=REFRESH_TOKEN,
        expires_delta=timedelta(days=settings.refresh_token_expire_days),
        extra={},
    )


def decode_token(token: str) -> dict[str, Any]:
    """Decode and validate a JWT. Raises jwt.PyJWTError on failure."""
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
