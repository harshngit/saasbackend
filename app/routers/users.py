from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_system_role, require_unlocked_org
from app.core.security import hash_password
from app.models import LEGACY_ROLE_BY_NAME, Role, SystemRole, User
from app.schemas.auth import MessageResponse
from app.schemas.user import (
    AdminResetPassword,
    RoleAssign,
    StaffCreate,
    UserOut,
    UserStatusUpdate,
    UserUpdate,
)
from app.services import password_service, role_service

router = APIRouter(prefix="/users", tags=["users"])

_ADMIN = require_system_role(SystemRole.ADMIN)


def _owned_user(db: Session, user_id: str, admin: User) -> User:
    target = db.get(User, user_id)
    if target is None or target.organization_id != admin.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found in this firm")
    return target


def _email_taken(db: Session, email: str, exclude_id: str | None = None) -> bool:
    # Email is the login identifier — keep it unique platform-wide.
    query = db.query(User).filter(User.email == email)
    if exclude_id is not None:
        query = query.filter(User.id != exclude_id)
    return db.query(query.exists()).scalar()


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_staff(
    payload: StaffCreate,
    admin: User = Depends(_ADMIN),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> User:
    """Admin creates a staff user in their firm. Accepts `role_id` (preferred) or a
    legacy `role` enum (mapped to the org's matching default role)."""
    # Resolve the role (must belong to the admin's org).
    if payload.role_id is not None:
        role = role_service.get_role_in_org(db, admin.organization_id, payload.role_id)
        if role is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="role_id is not a role in your firm")
    else:
        role = role_service.default_role_for_legacy(db, admin.organization_id, payload.role)
        if role is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown role")

    if _email_taken(db, payload.email):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    staff = User(
        organization_id=admin.organization_id,
        name=payload.name,
        email=payload.email,
        phone=payload.phone,
        password_hash=hash_password(payload.password),
        system_role=SystemRole.STAFF.value,
        role_id=role.id,
        role=LEGACY_ROLE_BY_NAME.get(role.name),  # legacy enum for default roles, else None
    )
    db.add(staff)
    db.commit()
    db.refresh(staff)
    return staff


@router.get("", response_model=list[UserOut])
def list_staff(
    admin: User = Depends(_ADMIN),
    role_id: str | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[User]:
    """List users in the admin's firm, optionally filtered by role_id / is_active."""
    query = db.query(User).filter(User.organization_id == admin.organization_id)
    if role_id is not None:
        query = query.filter(User.role_id == role_id)
    if is_active is not None:
        query = query.filter(User.is_active == is_active)
    return query.order_by(User.created_at.desc()).all()


@router.get("/{user_id}", response_model=UserOut)
def get_user(user_id: str, admin: User = Depends(_ADMIN), db: Session = Depends(get_db)) -> User:
    return _owned_user(db, user_id, admin)


@router.patch("/{user_id}", response_model=UserOut)
def update_user(
    user_id: str,
    payload: UserUpdate,
    admin: User = Depends(_ADMIN),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> User:
    """Edit a staff member's name / email / phone (not their role)."""
    target = _owned_user(db, user_id, admin)
    data = payload.model_dump(exclude_unset=True)
    if "email" in data and data["email"] != target.email and _email_taken(db, data["email"], exclude_id=target.id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")
    for field, value in data.items():
        setattr(target, field, value)
    db.commit()
    db.refresh(target)
    return target


@router.patch("/{user_id}/role", response_model=UserOut)
def change_role(
    user_id: str,
    payload: RoleAssign,
    admin: User = Depends(_ADMIN),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> User:
    """Assign a staff member to a different role within the same firm."""
    target = _owned_user(db, user_id, admin)
    if target.id == admin.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot change your own role")
    role = role_service.get_role_in_org(db, admin.organization_id, payload.role_id)
    if role is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="role_id is not a role in your firm")
    target.role_id = role.id
    target.role = LEGACY_ROLE_BY_NAME.get(role.name)
    db.commit()
    db.refresh(target)
    return target


@router.patch("/{user_id}/status", response_model=UserOut)
def update_staff_status(
    user_id: str,
    payload: UserStatusUpdate,
    admin: User = Depends(_ADMIN),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> User:
    """Activate / deactivate a user within the admin's firm (cannot target self)."""
    target = _owned_user(db, user_id, admin)
    if target.id == admin.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot change your own status")
    target.is_active = payload.is_active
    db.commit()
    db.refresh(target)
    return target


@router.post("/{user_id}/reset-password", response_model=MessageResponse)
def admin_reset_password(
    user_id: str,
    payload: AdminResetPassword,
    admin: User = Depends(_ADMIN),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> MessageResponse:
    """Admin sets a new password for a staff member in their firm."""
    target = _owned_user(db, user_id, admin)
    if target.id == admin.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Use /auth/change-password to change your own password",
        )
    target.password_hash = hash_password(payload.new_password)
    db.commit()
    password_service.revoke_all_refresh_tokens(db, target.id)
    return MessageResponse(detail="Password reset for user")
