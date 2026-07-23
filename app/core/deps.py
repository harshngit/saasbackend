from collections.abc import Callable

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import ACCESS_TOKEN, decode_token
from app.models import LOCKED_STATUSES, User, UserRole
from app.services import org_service

bearer_scheme = HTTPBearer(auto_error=True)

_CREDENTIALS_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Resolve the authenticated user from a Bearer access token."""
    try:
        payload = decode_token(credentials.credentials)
    except jwt.PyJWTError:
        raise _CREDENTIALS_ERROR

    if payload.get("type") != ACCESS_TOKEN:
        raise _CREDENTIALS_ERROR

    user_id = payload.get("sub")
    if not user_id:
        raise _CREDENTIALS_ERROR

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise _CREDENTIALS_ERROR
    return user


def require_unlocked_org(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    """Gate for data-mutation endpoints: block if the org's trial has expired /
    it's locked or suspended. Super Admin (no org) is always allowed. Also lazily
    flips an expired trial to locked."""
    if user.role == UserRole.SUPER_ADMIN:
        return user
    org = org_service.apply_trial_expiry(db, user.organization)
    if org is not None and org.status in LOCKED_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your trial has expired. Please upgrade your plan to continue.",
        )
    return user


def require_roles(*roles: UserRole) -> Callable[[User], User]:
    """Dependency factory restricting an endpoint to the given roles."""

    allowed = set(roles)

    def _guard(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to perform this action",
            )
        return user

    return _guard
