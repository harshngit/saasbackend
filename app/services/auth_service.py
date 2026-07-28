import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import create_access_token, create_refresh_token
from app.models import RefreshToken, User
from app.schemas.auth import AuthResponse, TokenPair


def _hash_refresh(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_tokens(db: Session, user: User) -> TokenPair:
    """Create an access/refresh pair and persist the refresh token hash for revocation."""
    access = create_access_token(user.id, user.effective_system_role, user.organization_id)
    refresh = create_refresh_token(user.id)

    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=_hash_refresh(refresh),
            expires_at=datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_expire_days),
        )
    )
    db.commit()
    return TokenPair(access_token=access, refresh_token=refresh)


def get_active_refresh(db: Session, token: str) -> RefreshToken | None:
    """Return the stored refresh-token record if it exists, is unrevoked, and unexpired."""
    record = db.query(RefreshToken).filter(RefreshToken.token_hash == _hash_refresh(token)).first()
    if record is None or record.revoked:
        return None
    if record.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        return None
    return record


def revoke_refresh(db: Session, token: str) -> bool:
    record = db.query(RefreshToken).filter(RefreshToken.token_hash == _hash_refresh(token)).first()
    if record is None or record.revoked:
        return False
    record.revoked = True
    db.commit()
    return True


def build_auth_response(user: User, tokens: TokenPair) -> AuthResponse:
    return AuthResponse(user=user, organization=user.organization, tokens=tokens)
