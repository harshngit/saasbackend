from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_system_role, require_unlocked_org
from app.core.files import store_upload
from app.core.security import hash_password
from app.models import LEGACY_ROLE_BY_NAME, Role, SystemRole, User, VehicleLoading
from app.schemas.auth import MessageResponse
from app.schemas.company import UploadResponse
from app.schemas.user import (
    AdminResetPassword,
    EmployeeOptions,
    EmployeeStatus,
    EmploymentType,
    RoleAssign,
    StaffCreate,
    UserOut,
    UserStatusUpdate,
    UserUpdate,
)
from app.services import password_service, role_service

router = APIRouter(prefix="/users", tags=["users"])

_ADMIN = require_system_role(SystemRole.ADMIN)

# Employee-profile columns the create/update endpoints may write.
_PROFILE_FIELDS = (
    "employee_id",
    "first_name",
    "last_name",
    "designation",
    "employment_type",
    "date_of_joining",
    "employee_status",
    "identify_proofs",
)


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


def _username_taken(db: Session, username: str, exclude_id: str | None = None) -> bool:
    query = db.query(User).filter(User.username == username)
    if exclude_id is not None:
        query = query.filter(User.id != exclude_id)
    return db.query(query.exists()).scalar()


def _employee_ids_in_org(db: Session, org_id: str | None, exclude_id: str | None = None) -> set[str]:
    # Employee codes are a per-firm series, so uniqueness is scoped to the org.
    query = db.query(User.employee_id).filter(
        User.organization_id == org_id, User.employee_id.isnot(None)
    )
    if exclude_id is not None:
        query = query.filter(User.id != exclude_id)
    return {row[0] for row in query}


def _next_employee_id(taken: set[str]) -> str:
    """Next free EMP-#### code for the firm."""
    number = len(taken) + 1
    while f"EMP-{number:04d}" in taken:
        number += 1
    return f"EMP-{number:04d}"


def _profile_values(data: dict, db: Session, admin: User, target: User | None = None) -> dict:
    """Pull the employee-profile keys out of a validated payload, enforcing the
    per-firm uniqueness of employee_id. Enum values are stored as their strings."""
    values = {field: data[field] for field in _PROFILE_FIELDS if field in data}
    if values.get("employee_id") is not None:
        taken = _employee_ids_in_org(db, admin.organization_id, exclude_id=target.id if target else None)
        if values["employee_id"] in taken:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Employee ID already used in this firm",
            )
    for field in ("employment_type", "employee_status"):
        if isinstance(values.get(field), (EmploymentType, EmployeeStatus)):
            values[field] = values[field].value
    return values


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_staff(
    payload: StaffCreate,
    admin: User = Depends(_ADMIN),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> User:
    """Admin creates a staff user in their firm. Accepts `role_id` (preferred) or a
    legacy `role` enum (mapped to the org's matching default role). The employee
    profile fields are all optional; `employee_id` is auto-assigned if omitted."""
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
    if _username_taken(db, payload.username):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already taken")

    profile = _profile_values(payload.model_dump(exclude_unset=True), db, admin)
    if profile.get("employee_id") is None:
        profile["employee_id"] = _next_employee_id(_employee_ids_in_org(db, admin.organization_id))
    if profile.get("employee_status") is None:
        profile["employee_status"] = EmployeeStatus.ACTIVE.value

    staff = User(
        organization_id=admin.organization_id,
        name=payload.name,
        email=payload.email,
        username=payload.username,
        phone=payload.phone,
        password_hash=hash_password(payload.password),
        system_role=SystemRole.STAFF.value,
        role_id=role.id,
        role=LEGACY_ROLE_BY_NAME.get(role.name),  # legacy enum for default roles, else None
        **profile,
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
    search: str | None = Query(default=None, description="matches name / email / employee ID / phone"),
    designation: str | None = Query(default=None),
    employment_type: EmploymentType | None = Query(default=None),
    employee_status: EmployeeStatus | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[User]:
    """List the firm's employees, filtered by role / login state / employee profile."""
    query = db.query(User).filter(User.organization_id == admin.organization_id)
    if role_id is not None:
        query = query.filter(User.role_id == role_id)
    if is_active is not None:
        query = query.filter(User.is_active == is_active)
    if search:
        like = f"%{search}%"
        query = query.filter(
            or_(
                User.name.ilike(like),
                User.email.ilike(like),
                User.employee_id.ilike(like),
                User.phone.ilike(like),
            )
        )
    if designation is not None:
        query = query.filter(User.designation == designation)
    if employment_type is not None:
        query = query.filter(User.employment_type == employment_type.value)
    if employee_status is not None:
        query = query.filter(User.employee_status == employee_status.value)
    return query.order_by(User.created_at.desc()).all()


@router.get("/meta/employee-options", response_model=EmployeeOptions)
def employee_options(admin: User = Depends(_ADMIN), db: Session = Depends(get_db)) -> EmployeeOptions:
    """Dropdown data for the employee form — the fixed choice lists plus the
    designations already used in this firm."""
    rows = (
        db.query(User.designation)
        .filter(User.organization_id == admin.organization_id, User.designation.isnot(None))
        .distinct()
    )
    return EmployeeOptions(
        employment_types=[e.value for e in EmploymentType],
        employee_statuses=[e.value for e in EmployeeStatus],
        designations=sorted(row[0] for row in rows),
    )


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
    """Edit a staff member's account details and employee profile (not their role)."""
    target = _owned_user(db, user_id, admin)
    data = payload.model_dump(exclude_unset=True)
    if "email" in data and data["email"] != target.email and _email_taken(db, data["email"], exclude_id=target.id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")
    if "username" in data and data["username"] != target.username and _username_taken(db, data["username"], exclude_id=target.id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already taken")
    data.update(_profile_values(data, db, admin, target=target))
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


@router.post("/{user_id}/identity-proof", response_model=UploadResponse)
def upload_identity_proof(
    user_id: str,
    admin: User = Depends(_ADMIN),
    _unlocked: User = Depends(require_unlocked_org),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> UploadResponse:
    """Attach a scanned ID document (image or PDF) to an employee's profile."""
    target = _owned_user(db, user_id, admin)
    target.identify_proofs = store_upload(file)
    db.commit()
    return UploadResponse(url=target.identify_proofs)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: str,
    admin: User = Depends(_ADMIN),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> None:
    """Permanently remove an employee from the firm.

    Records that merely *reference* the user (customers, leads, quotations, sales
    orders) keep their history with the link nulled out; the user's own rows
    (attendance, notifications, sessions) go with them. Use PATCH /status to
    deactivate instead when the history should stay attributed.
    """
    # Blocking self-delete is also what keeps a firm from losing its last admin:
    # the caller is an admin of this org, so a surviving admin is guaranteed.
    target = _owned_user(db, user_id, admin)
    if target.id == admin.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot delete your own account")

    # An open loading means stock is still out on their vehicle — deleting the user
    # would cascade those records away and lose the variance. Close it first.
    open_loading = (
        db.query(VehicleLoading)
        .filter(VehicleLoading.delivery_partner_id == target.id, VehicleLoading.status == "active")
        .first()
    )
    if open_loading is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This user has stock loaded on their vehicle. Close the loading (end-of-day) before deleting.",
        )

    password_service.revoke_all_refresh_tokens(db, target.id)
    db.delete(target)
    db.commit()
