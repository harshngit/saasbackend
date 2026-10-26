import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import as_utc
from app.models import PasswordResetToken, RefreshToken, User


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_reset_token(db: Session, user: User) -> str:
    """Create a single-use reset token, store its hash, and return the raw value.

    Any earlier unused token of this user stops working: only the newest link resets.
    """
    raw = secrets.token_urlsafe(32)
    db.execute(
        sa_update(PasswordResetToken)
        .where(PasswordResetToken.user_id == user.id, PasswordResetToken.used.is_(False))
        .values(used=True)
    )
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=_hash(raw),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=settings.reset_token_expire_minutes),
        )
    )
    db.commit()
    return raw


def consume_reset_token(db: Session, raw_token: str) -> User | None:
    """Validate a reset token and mark it used. Returns the user, or None if invalid.

    The used=False -> used=True flip is a single UPDATE...WHERE, so of two concurrent
    requests with the same token at most one gets rowcount == 1 (same pattern as
    auth_service.consume_exchange_ticket). An expired token is burnt and rejected.
    """
    token_hash = _hash(raw_token)
    result = db.execute(
        sa_update(PasswordResetToken)
        .where(PasswordResetToken.token_hash == token_hash, PasswordResetToken.used.is_(False))
        .values(used=True)
    )
    db.commit()
    if result.rowcount != 1:
        return None
    record = db.query(PasswordResetToken).filter(PasswordResetToken.token_hash == token_hash).first()
    if record is None or as_utc(record.expires_at) < datetime.now(timezone.utc):
        return None
    return db.get(User, record.user_id)


def build_reset_link(raw_token: str) -> str:
    sep = "&" if "?" in settings.frontend_reset_url else "?"
    return f"{settings.frontend_reset_url}{sep}token={raw_token}"


def revoke_all_refresh_tokens(db: Session, user_id: str) -> None:
    """Force re-login everywhere after a password change/reset."""
    db.query(RefreshToken).filter(
        RefreshToken.user_id == user_id, RefreshToken.revoked.is_(False)
    ).update({RefreshToken.revoked: True})
    db.commit()
