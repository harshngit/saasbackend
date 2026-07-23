from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_roles, require_unlocked_org
from app.core.security import hash_password
from app.models import STAFF_ROLES, User, UserRole
from app.schemas.auth import MessageResponse
from app.schemas.user import AdminResetPassword, StaffCreate, UserOut, UserStatusUpdate
from app.services import password_service

router = APIRouter(prefix="/users", tags=["users"])


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_staff(
    payload: StaffCreate,
    admin: User = Depends(require_roles(UserRole.ADMIN)),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> User:
    """Admin creates a staff user (Accountant / Sales Officer / Delivery Partner) in their firm."""
    if payload.role not in STAFF_ROLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Admins may only create Accountant, Sales Officer, or Delivery Partner accounts",
        )

    # Email must be unique within the firm.
    clash = (
        db.query(User)
        .filter(User.organization_id == admin.organization_id, User.email == payload.email)
        .first()
    )
    if clash is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already exists in this firm")

    staff = User(
        organization_id=admin.organization_id,
        name=payload.name,
        email=payload.email,
        phone=payload.phone,
        password_hash=hash_password(payload.password),
        role=payload.role,
    )
    db.add(staff)
    db.commit()
    db.refresh(staff)
    return staff


@router.get("", response_model=list[UserOut])
def list_staff(
    admin: User = Depends(require_roles(UserRole.ADMIN)),
    db: Session = Depends(get_db),
) -> list[User]:
    """List all users belonging to the admin's firm."""
    return (
        db.query(User)
        .filter(User.organization_id == admin.organization_id)
        .order_by(User.created_at.desc())
        .all()
    )


@router.patch("/{user_id}/status", response_model=UserOut)
def update_staff_status(
    user_id: str,
    payload: UserStatusUpdate,
    admin: User = Depends(require_roles(UserRole.ADMIN)),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> User:
    """Activate / deactivate a user within the admin's firm (cannot target self)."""
    target = db.get(User, user_id)
    if target is None or target.organization_id != admin.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found in this firm")
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
    admin: User = Depends(require_roles(UserRole.ADMIN)),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> MessageResponse:
    """Admin sets a new password for a staff member in their firm (e.g. staff forgot it)."""
    target = db.get(User, user_id)
    if target is None or target.organization_id != admin.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found in this firm")
    if target.id == admin.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Use /auth/change-password to change your own password",
        )

    target.password_hash = hash_password(payload.new_password)
    db.commit()
    # Log the staff member out of any existing sessions.
    password_service.revoke_all_refresh_tokens(db, target.id)
    return MessageResponse(detail="Password reset for user")
