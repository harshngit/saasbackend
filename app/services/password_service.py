import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import PasswordResetToken, RefreshToken, User


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_reset_token(db: Session, user: User) -> str:
    """Create a single-use reset token, store its hash, and return the raw value."""
    raw = secrets.token_urlsafe(32)
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
    """Validate a reset token and mark it used. Returns the user, or None if invalid."""
    record = (
        db.query(PasswordResetToken)
        .filter(PasswordResetToken.token_hash == _hash(raw_token))
        .first()
    )
    if record is None or record.used:
        return None
    if record.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        return None

    user = db.get(User, record.user_id)
    if user is None:
        return None

    record.used = True
    db.commit()
    return user


def build_reset_link(raw_token: str) -> str:
    sep = "&" if "?" in settings.frontend_reset_url else "?"
    return f"{settings.frontend_reset_url}{sep}token={raw_token}"


def revoke_all_refresh_tokens(db: Session, user_id: str) -> None:
    """Force re-login everywhere after a password change/reset."""
    db.query(RefreshToken).filter(
        RefreshToken.user_id == user_id, RefreshToken.revoked.is_(False)
    ).update({RefreshToken.revoked: True})
    db.commit()
