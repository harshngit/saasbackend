from collections.abc import Callable

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import ACCESS_TOKEN, decode_token
from app.models import LOCKED_STATUSES, Role, SystemRole, User, UserRole
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
    if user.effective_system_role == SystemRole.SUPER_ADMIN.value:
        return user
    org = org_service.apply_trial_expiry(db, user.organization)
    if org is not None and org.status in LOCKED_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your trial has expired. Please upgrade your plan to continue.",
        )
    return user


def require_system_role(*roles: SystemRole) -> Callable[[User], User]:
    """Restrict an endpoint to the given system roles (super_admin / admin / staff)."""

    allowed = {r.value for r in roles}

    def _guard(user: User = Depends(get_current_user)) -> User:
        if user.effective_system_role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to perform this action",
            )
        return user

    return _guard


def require_roles(*roles: UserRole) -> Callable[[User], User]:
    """Legacy guard (checks the old role enum). Prefer require_system_role."""

    allowed = set(roles)

    def _guard(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to perform this action",
            )
        return user

    return _guard


def require_permission(module: str, action: str) -> Callable[[User], User]:
    """Gate a module action by the staff user's role permission matrix.

    admin/super_admin bypass (full access in their scope); staff are checked against
    their role's permissions. This is the reusable check every future business
    module (products, orders, invoices, …) will apply.
    """

    def _guard(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> User:
        if user.effective_system_role in (SystemRole.ADMIN.value, SystemRole.SUPER_ADMIN.value):
            return user
        role = db.get(Role, user.role_id) if user.role_id else None
        perms = (role.permissions if role else {}) or {}
        if not perms.get(module, {}).get(action, False):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"You don't have permission to {action} {module}",
            )
        return user

    return _guard
