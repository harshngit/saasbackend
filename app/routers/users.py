import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_system_role, require_unlocked_org
from app.core.files import read_upload, store_upload
from app.core.reference_data import COUNTRIES, INDIAN_STATES
from app.core.security import hash_password
from app.models import LEGACY_ROLE_BY_NAME, Role, SystemRole, User, VehicleLoading
from app.schemas.auth import MessageResponse
from app.schemas.company import UploadResponse
from app.schemas.user import (
    BLOOD_GROUPS,
    DESIGNATIONS,
    EMERGENCY_CONTACT_RELATIONSHIPS,
    GENDERS,
    IDENTITY_PROOF_TYPES,
    MARITAL_STATUSES,
    AccountStatus,
    AccountStatusUpdate,
    AdminResetPassword,
    EmployeeDocument,
    EmployeeDocumentCollection,
    EmployeeFileField,
    EmployeeOptions,
    EmployeeProfileIn,
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

# Used when a firm has not set its own employee_id_prefix in Company Settings.
DEFAULT_EMPLOYEE_ID_PREFIX = "EMP-"

# Employee-profile columns the create/update endpoints may write — exactly the
# fields the shared profile schema declares, so the two never drift apart.
_PROFILE_FIELDS = tuple(EmployeeProfileIn.model_fields)

# Choice fields stored as their plain string value, not the Enum member.
_ENUM_FIELDS = ("employment_type", "employee_status", "status")


def _resolve_role(db: Session, admin: User, role_id: str | None, role_name: str | None) -> Role:
    """Turn the payload's role_id / role name into one of the firm's roles."""
    role = role_service.resolve_role(db, admin.organization_id, role_id=role_id, role_name=role_name)
    if role is None:
        available = ", ".join(role_service.role_names(db, admin.organization_id))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"'{role_id or role_name}' is not a role in your firm. Available roles: {available}",
        )
    return role


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


def _employee_id_prefix(admin: User) -> str:
    """The firm's auto-number prefix, set on the Company Settings page."""
    org = admin.organization
    prefix = (org.employee_id_prefix or "").strip() if org is not None else ""
    return prefix or DEFAULT_EMPLOYEE_ID_PREFIX


def _next_employee_id(taken: set[str], prefix: str = DEFAULT_EMPLOYEE_ID_PREFIX) -> str:
    """Next free <prefix>#### code for the firm."""
    number = len(taken) + 1
    while f"{prefix}{number:04d}" in taken:
        number += 1
    return f"{prefix}{number:04d}"


def _validate_reporting_manager(
    db: Session, admin: User, manager_id: str | None, target: User | None = None
) -> None:
    """The manager must be someone else in the same firm, and the reporting chain
    must not loop back to the employee being edited."""
    if manager_id is None:
        return
    if target is not None and manager_id == target.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An employee cannot report to themselves",
        )
    manager = db.get(User, manager_id)
    if manager is None or manager.organization_id != admin.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="reporting_manager_id is not an employee in your firm",
        )
    if target is None:
        return
    seen = {target.id}
    while manager is not None and manager.id not in seen:
        seen.add(manager.id)
        manager = db.get(User, manager.reporting_manager_id) if manager.reporting_manager_id else None
    if manager is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That would make the reporting line loop back on itself",
        )


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
    if "reporting_manager_id" in values:
        _validate_reporting_manager(db, admin, values["reporting_manager_id"], target=target)
    for field in _ENUM_FIELDS:
        if isinstance(values.get(field), (EmploymentType, EmployeeStatus, AccountStatus)):
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
    `role` name — any role from the firm's Roles page, not a fixed list.

    `designation`, `employment_type`, `date_of_joining`, `employee_status`,
    `identity_proof_type`, `identity_proof_file` and `status` are required by the
    employee form; the rest of the profile is optional. `name` may be omitted when
    first/last name are given, and `employee_id` is auto-assigned if omitted."""
    role = _resolve_role(db, admin, payload.role_id, payload.role)

    if _email_taken(db, payload.email):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")
    if _username_taken(db, payload.username):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already taken")

    profile = _profile_values(payload.model_dump(exclude_unset=True), db, admin)
    if profile.get("employee_id") is None:
        profile["employee_id"] = _next_employee_id(
            _employee_ids_in_org(db, admin.organization_id), _employee_id_prefix(admin)
        )

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
        # Only an ACTIVE account may log in — see AccountStatus.
        is_active=payload.status is AccountStatus.ACTIVE,
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
    search: str | None = Query(default=None, description="matches name / email / employee ID / phone / username"),
    designation: str | None = Query(default=None),
    employment_type: EmploymentType | None = Query(default=None),
    employee_status: EmployeeStatus | None = Query(default=None),
    account_status: AccountStatus | None = Query(default=None, alias="status"),
    work_location: str | None = Query(default=None),
    reporting_manager_id: str | None = Query(default=None),
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
                User.username.ilike(like),
            )
        )
    if designation is not None:
        query = query.filter(User.designation == designation)
    if employment_type is not None:
        query = query.filter(User.employment_type == employment_type.value)
    if employee_status is not None:
        query = query.filter(User.employee_status == employee_status.value)
    if account_status is not None:
        query = query.filter(User.status == account_status.value)
    if work_location is not None:
        query = query.filter(User.work_location == work_location)
    if reporting_manager_id is not None:
        query = query.filter(User.reporting_manager_id == reporting_manager_id)
    return query.order_by(User.created_at.desc()).all()


@router.get("/meta/employee-options", response_model=EmployeeOptions)
def employee_options(admin: User = Depends(_ADMIN), db: Session = Depends(get_db)) -> EmployeeOptions:
    """Dropdown data for the employee form — the fixed choice lists, suggested
    values for the free-text choice fields, and the values already used in this firm."""

    def in_use(column) -> list[str]:  # noqa: ANN001
        rows = (
            db.query(column)
            .filter(User.organization_id == admin.organization_id, column.isnot(None))
            .distinct()
        )
        return sorted(row[0] for row in rows if row[0])

    return EmployeeOptions(
        employment_types=[e.value for e in EmploymentType],
        employee_statuses=[e.value for e in EmployeeStatus],
        account_statuses=[e.value for e in AccountStatus],
        genders=GENDERS,
        marital_statuses=MARITAL_STATUSES,
        blood_groups=BLOOD_GROUPS,
        identity_proof_types=IDENTITY_PROOF_TYPES,
        emergency_contact_relationships=EMERGENCY_CONTACT_RELATIONSHIPS,
        countries=COUNTRIES,
        nationalities=COUNTRIES,  # nationality is recorded as the country name
        states=INDIAN_STATES,
        # Presets first, then anything else this firm already uses.
        designations=DESIGNATIONS + [d for d in in_use(User.designation) if d not in DESIGNATIONS],
        work_locations=in_use(User.work_location),
        shifts=in_use(User.shift),
        employee_id_prefix=_employee_id_prefix(admin),
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
    # Renaming either half of the name refreshes the combined `name`, unless the
    # caller set it themselves in the same request.
    if "name" not in data and ("first_name" in data or "last_name" in data):
        composed = " ".join(p for p in (target.first_name, target.last_name) if p).strip()
        if composed:
            target.name = composed
    if "status" in data:
        target.is_active = data["status"] == AccountStatus.ACTIVE.value
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
    """Assign a staff member to a different role within the same firm (by id or name)."""
    target = _owned_user(db, user_id, admin)
    if target.id == admin.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot change your own role")
    role = _resolve_role(db, admin, payload.role_id, payload.role)
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


@router.patch("/{user_id}/account-status", response_model=UserOut)
def update_account_status(
    user_id: str,
    payload: AccountStatusUpdate,
    admin: User = Depends(_ADMIN),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> User:
    """Set the richer account status (active / inactive / suspended / locked).
    `is_active` follows it, so anything other than ACTIVE also blocks login."""
    target = _owned_user(db, user_id, admin)
    if target.id == admin.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot change your own status")
    target.status = payload.status.value
    target.is_active = payload.status is AccountStatus.ACTIVE
    db.commit()
    db.refresh(target)
    return target


@router.post("/{user_id}/identity-proof", response_model=UploadResponse)
def upload_identity_proof(
    user_id: str,
    admin: User = Depends(_ADMIN),
    _unlocked: User = Depends(require_unlocked_org),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> UploadResponse:
    """Attach a scanned ID document (image or PDF) to an employee's profile.

    Kept for older clients — the same slot is reachable as
    POST /users/{id}/files/identity_proof_file."""
    target = _owned_user(db, user_id, admin)
    target.identity_proof_file = store_upload(file)
    target.identify_proofs = target.identity_proof_file
    db.commit()
    return UploadResponse(url=target.identity_proof_file)


# ------------------------------ Employee uploads ------------------------------
# Every file slot is a plain column holding a URL, so it can also be set through
# PATCH /users/{id} with an external URL — preferable for anything large, since
# uploads here are inlined as base64 data: URLs (max 10 MB, no S3 yet).

_FILE_RULES: dict[EmployeeFileField, dict[str, bool]] = {
    EmployeeFileField.profile_photo: {"allow_pdf": False},
    EmployeeFileField.identity_proof_file: {},
    EmployeeFileField.resume_cv: {},
    EmployeeFileField.offer_letter: {},
    EmployeeFileField.appointment_letter: {},
}

_MAX_DOCUMENTS = 20


@router.post("/{user_id}/files/{field}", response_model=UserOut)
def upload_employee_file(
    user_id: str,
    field: EmployeeFileField,
    file: UploadFile = File(...),
    admin: User = Depends(_ADMIN),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> User:
    """Upload one of the employee's single-file slots — profile photo, identity
    proof, resume/CV, offer letter or appointment letter. Replaces whatever was there."""
    target = _owned_user(db, user_id, admin)
    url, _size = read_upload(file, **_FILE_RULES[field])
    setattr(target, field.value, url)
    if field is EmployeeFileField.identity_proof_file:
        target.identify_proofs = url  # keep the legacy alias in step
    db.commit()
    db.refresh(target)
    return target


@router.delete("/{user_id}/files/{field}", response_model=UserOut)
def clear_employee_file(
    user_id: str,
    field: EmployeeFileField,
    admin: User = Depends(_ADMIN),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> User:
    """Empty one of the employee's single-file slots."""
    target = _owned_user(db, user_id, admin)
    setattr(target, field.value, None)
    if field is EmployeeFileField.identity_proof_file:
        target.identify_proofs = None
    db.commit()
    db.refresh(target)
    return target


def _documents(target: User, collection: EmployeeDocumentCollection) -> list[dict]:
    return list(getattr(target, collection.value) or [])


def _save_documents(
    db: Session, target: User, collection: EmployeeDocumentCollection, documents: list[dict]
) -> list[dict]:
    """Persist one document collection. Reassigned wholesale — SQLAlchemy does not
    track in-place mutation of a JSON column."""
    setattr(target, collection.value, documents)
    db.commit()
    db.refresh(target)
    return _documents(target, collection)


@router.get("/{user_id}/documents/{collection}", response_model=list[EmployeeDocument])
def list_employee_documents(
    user_id: str,
    collection: EmployeeDocumentCollection,
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Every file in one of the employee's multi-file slots."""
    return _documents(_owned_user(db, user_id, admin), collection)


@router.post(
    "/{user_id}/documents/{collection}",
    response_model=list[EmployeeDocument],
    status_code=status.HTTP_201_CREATED,
)
def upload_employee_documents(
    user_id: str,
    collection: EmployeeDocumentCollection,
    files: list[UploadFile] = File(..., description="One or more image / PDF files"),
    admin: User = Depends(_ADMIN),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Upload one or more files into a multi-file slot — general documents,
    experience certificates or educational certificates. Appended to what is
    already on file, so uploading again does not replace the earlier ones."""
    target = _owned_user(db, user_id, admin)
    documents = _documents(target, collection)
    if len(documents) + len(files) > _MAX_DOCUMENTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"At most {_MAX_DOCUMENTS} files in {collection.value} "
                   f"({len(documents)} already uploaded). Delete some first.",
        )
    for file in files:
        url, size = read_upload(file)
        documents.append(
            {
                "id": str(uuid.uuid4()),
                "name": file.filename or "document",
                "url": url,
                "content_type": file.content_type,
                "size": size,
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    return _save_documents(db, target, collection, documents)


@router.delete(
    "/{user_id}/documents/{collection}/{document_id}", response_model=list[EmployeeDocument]
)
def delete_employee_document(
    user_id: str,
    collection: EmployeeDocumentCollection,
    document_id: str,
    admin: User = Depends(_ADMIN),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Remove one file from a multi-file slot. Returns the remaining list."""
    target = _owned_user(db, user_id, admin)
    documents = _documents(target, collection)
    remaining = [d for d in documents if d.get("id") != document_id]
    if len(remaining) == len(documents):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return _save_documents(db, target, collection, remaining)


@router.delete("/{user_id}/documents/{collection}", status_code=status.HTTP_204_NO_CONTENT)
def clear_employee_documents(
    user_id: str,
    collection: EmployeeDocumentCollection,
    admin: User = Depends(_ADMIN),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> None:
    """Empty one of the employee's multi-file slots."""
    _save_documents(db, _owned_user(db, user_id, admin), collection, [])


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
