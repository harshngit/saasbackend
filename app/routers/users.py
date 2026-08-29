from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, require_permission, require_system_role, require_unlocked_org
from app.core.security import hash_password
from app.models import LEGACY_ROLE_BY_NAME, Role, SystemRole, User, VehicleLoading
from app.schemas.auth import MessageResponse
from app.schemas.employee_profile import EmployeeProfileIn, EmployeeProfileOut
from app.schemas.staff_overview import LocationPing, LocationPingOut, StaffOverviewOut
from app.schemas.user import (
    COLLECTION_COLUMN,
    AccountStatus,
    AdminResetPassword,
    AssignableStaffOut,
    EmployeeDocumentCollection,
    EmployeeStatus,
    EmploymentType,
    UserOut,
)
from app.services import (
    activity_service,
    employee_profile_service,
    password_service,
    role_service,
    staff_overview_service,
)

router = APIRouter(prefix="/users", tags=["users"])

_ADMIN = require_system_role(SystemRole.ADMIN)
# Same gate as actually creating a follow-up (POST /follow-ups, POST
# /visits/{id}/follow-ups) — whoever may create one may see who it can go to.
_ASSIGNABLE = require_permission("follow_ups", "create")

# Used when a firm has not set its own employee_id_prefix in Company Settings.
DEFAULT_EMPLOYEE_ID_PREFIX = "EMP-"


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
            detail="employment_information.reporting_manager_id is not an employee in your firm",
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


def _check_columns(db: Session, admin: User, values: dict, target: User | None = None) -> None:
    """Cross-record checks on the columns a body wants to write: the employee code
    is unique within the firm, and the reporting line is sane."""
    if values.get("employee_id") is not None:
        taken = _employee_ids_in_org(db, admin.organization_id, exclude_id=target.id if target else None)
        if values["employee_id"] in taken:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Employee ID already used in this firm",
            )
    if "reporting_manager_id" in values:
        _validate_reporting_manager(db, admin, values["reporting_manager_id"], target=target)


def _composed_name(values: dict, email: str) -> str:
    """`name` is the record's label and cannot be empty, so fall back through the
    other names the body carries before settling for the email's local part."""
    first_last = " ".join(p for p in (values.get("first_name"), values.get("last_name")) if p).strip()
    return values.get("name") or values.get("display_name") or first_last or email.split("@")[0]


def _sync_identity_alias(target: User) -> None:
    """`identify_proofs` is the legacy name of identity_proof_file — kept in step so
    older clients reading the flat shape still see the document."""
    target.identify_proofs = target.identity_proof_file


@router.post("", response_model=EmployeeProfileOut, status_code=status.HTTP_201_CREATED)
def create_staff(
    payload: EmployeeProfileIn,
    admin: User = Depends(_ADMIN),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> EmployeeProfileOut:
    """Create an employee from the sectioned profile body.

    Every field is optional except the two a login account cannot exist without:
    `contact_information.official_email` and `login_security.password`. Which
    others a form insists on is the form's business.

    Files are not uploaded here — POST /files/upload first (it needs no employee
    id, so it works before the employee exists) and send the URLs it returns in
    `basic_information.profile_photo` and the `documents` section.

    A flat body is still accepted: any top-level field belonging to a section is
    folded into it. `employee_id` is auto-assigned (EMP-0001, …) when omitted,
    using the firm's `employee_id_prefix`, and `name` is composed from first +
    last name.
    """
    data = payload.to_columns()
    email = data.get("email")
    if not email:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="contact_information.official_email is required — it is the login identifier",
        )
    if not payload.password:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="login_security.password is required",
        )

    role_id = data.pop("role_id", None)
    role = _resolve_role(db, admin, role_id, payload.role) if (role_id or payload.role) else None

    if _email_taken(db, email):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")
    if data.get("username") and _username_taken(db, data["username"]):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already taken")

    _check_columns(db, admin, data)
    if data.get("employee_id") is None:
        data["employee_id"] = _next_employee_id(
            _employee_ids_in_org(db, admin.organization_id), _employee_id_prefix(admin)
        )
    data["name"] = _composed_name(data, email)
    # A new employee is on the payroll and able to log in unless told otherwise.
    data.setdefault("employee_status", EmployeeStatus.ACTIVE.value)
    data.setdefault("status", AccountStatus.ACTIVE.value)

    staff = User(
        organization_id=admin.organization_id,
        password_hash=hash_password(payload.password),
        system_role=SystemRole.STAFF.value,
        role_id=role.id if role is not None else None,
        # Legacy enum for the default roles, else None.
        role=LEGACY_ROLE_BY_NAME.get(role.name) if role is not None else None,
        # Only an ACTIVE account may log in — see AccountStatus.
        is_active=data["status"] == AccountStatus.ACTIVE.value,
        **data,
    )
    db.add(staff)
    db.flush()
    employee_profile_service.apply_documents(db, staff, payload.documents)
    _sync_identity_alias(staff)
    activity_service.record(
        db, admin.organization_id, admin, "employee", "Employee added",
        f"{staff.name} ({staff.employee_id}) joined the firm",
    )
    db.commit()
    db.refresh(staff)
    return employee_profile_service.build_profile(db, staff)


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
    """List the firm's employees, filtered by role / login state / employee profile.

    Rows come back flat — one row per employee, everything at the top level. The
    sectioned profile is what GET /users/{id} serves."""
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


def _assignee_role_label(user: User) -> str:
    """A short role string for the assignee picker: the legacy enum value for
    admins and the 3 default-seeded staff roles (matches what those roles have
    always been called — "admin", "sales_officer", …), else the firm's own
    name for a custom role, else a generic fallback."""
    if user.effective_system_role in ("admin", "super_admin"):
        return user.effective_system_role
    if user.role is not None:
        return user.role.value
    if user.role_detail is not None:
        return user.role_detail.name
    return "staff"


@router.get("/assignable", response_model=list[AssignableStaffOut])
def list_assignable_staff(
    user: User = Depends(_ASSIGNABLE),
    db: Session = Depends(get_db),
) -> list[AssignableStaffOut]:
    """Minimal staff picker for the Follow-up Task assignee dropdown.

    Unlike GET /users (admin-only, full employee profile), this is reachable
    by anyone who may create a follow-up — the same `follow_ups:create`
    permission gate as POST /follow-ups and POST /visits/{id}/follow-ups —
    and returns only active users in the caller's own organization whose
    role actually has some access to the follow_ups module (so the list never
    offers a role that could never see or act on the task once assigned).
    """
    candidates = (
        db.query(User)
        .filter(User.organization_id == user.organization_id, User.is_active.is_(True))
        .order_by(User.name)
        .all()
    )
    assignable = [
        u for u in candidates
        if any(role_service.effective_permissions(db, u).get("follow_ups", {}).values())
    ]
    return [AssignableStaffOut(id=u.id, name=u.name, role=_assignee_role_label(u)) for u in assignable]


@router.post("/me/location", response_model=LocationPingOut)
def report_my_location(
    payload: LocationPing,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> LocationPingOut:
    """The field app posts the device's own GPS reading here.

    Writes to the caller only — nobody can post a position for someone else. Only
    the latest reading is kept; it is what `current_location` reports on
    GET /users/{id}/overview, and until one arrives that block says
    `available: false`. An employee's `work_location` is the office they are posted
    to and is never treated as a live position.
    """
    user.last_latitude = payload.latitude
    user.last_longitude = payload.longitude
    user.last_location_accuracy_m = payload.accuracy_meters
    user.last_location_label = payload.label
    user.last_location_at = payload.captured_at or datetime.now(timezone.utc)
    db.commit()
    db.refresh(user)
    return LocationPingOut(
        user_id=user.id,
        latitude=user.last_latitude,
        longitude=user.last_longitude,
        accuracy_meters=user.last_location_accuracy_m,
        label=user.last_location_label,
        updated_at=user.last_location_at,
    )


@router.get("/{user_id}/overview", response_model=StaffOverviewOut)
def staff_overview(
    user_id: str,
    period: str = Query(
        default="today", description="today | week (last 7 days) | month (last 30 days)"
    ),
    date_from: str | None = Query(
        default=None, description="YYYY-MM-DD — a custom range instead of `period`"),
    date_to: str | None = Query(default=None, description="YYYY-MM-DD; defaults to today"),
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> StaffOverviewOut:
    """The operational half of the Staff Detail page, shaped by the employee's role.

    GET /users/{id} stays the static profile; this is the live summary beside it.
    Which blocks are filled depends on `role_detail.workspace`, and the response
    repeats it as `workspace` so the page can pick a layout:

    * `sales` — today's and the period's sales, assigned customers, recent orders,
      a per-day performance series, attendance, last known location.
    * `delivery` — today's deliveries split by outcome, delivery value, amount
      collected and receivable, the open assigned deliveries, the day's vehicle
      loading, attendance, last known location.
    * anything else — a generic block: orders raised, sales value, assigned
      customers and days present.

    Blocks that do not apply to the workspace come back `null` rather than being
    left out, so the shape never changes under the frontend.

    `period` sizes every figure and the `performance` series: `today`, `week` (the last
    7 days) or `month` (the last 30). The response repeats it, with the exact
    `period_from` / `period_to` it worked out. `date_from` / `date_to` are still accepted
    for a custom range and win when sent.

    Visits and follow-ups report `null` — not zero — until those modules exist. Current
    location is real GPS only; the employee's `work_location` is never reported as a live
    position. There is no route, stop list or view-route here: there is no
    route-planning module to base one on.
    """
    return staff_overview_service.build_staff_overview(
        db, _owned_user(db, user_id, admin),
        period=period, date_from=date_from, date_to=date_to,
    )


@router.get("/{user_id}", response_model=EmployeeProfileOut)
def get_user(
    user_id: str, admin: User = Depends(_ADMIN), db: Session = Depends(get_db)
) -> EmployeeProfileOut:
    """The employee's full profile, in the same sections the create body uses."""
    return employee_profile_service.build_profile(db, _owned_user(db, user_id, admin))


@router.patch("/{user_id}", response_model=EmployeeProfileOut)
def update_user(
    user_id: str,
    payload: EmployeeProfileIn,
    admin: User = Depends(_ADMIN),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> EmployeeProfileOut:
    """Partial update — the one endpoint for every change to an employee.

    Only the sections you send are touched, and within a section only the fields
    you send. That covers what used to need endpoints of its own: an employee
    resigning is `employment_information` (`employee_status`, `date_of_exit`) plus
    `system_preferences.account_status` in a single call, and `account_status`
    still drives whether they can log in.
    """
    target = _owned_user(db, user_id, admin)
    data = payload.to_columns()
    # The label and the login identifier are the two columns that cannot be empty,
    # so an explicit null for them means "leave it alone", not "erase it".
    for required in ("name", "email"):
        if required in data and data[required] is None:
            del data[required]

    # Resolve everything that can fail before writing anything.
    role_sent = "role_id" in data or payload.role is not None
    role: Role | None = None
    if role_sent:
        role_id = data.pop("role_id", None)
        if target.id == admin.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot change your own role"
            )
        # An explicit null clears the assignment rather than failing to resolve it.
        if role_id or payload.role:
            role = _resolve_role(db, admin, role_id, payload.role)

    if data.get("email") and data["email"] != target.email and _email_taken(db, data["email"], exclude_id=target.id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")
    if data.get("username") and data["username"] != target.username and _username_taken(db, data["username"], exclude_id=target.id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already taken")
    if "status" in data and target.id == admin.id and data["status"] != AccountStatus.ACTIVE.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot deactivate your own account",
        )
    _check_columns(db, admin, data, target=target)

    if role_sent:
        target.role_id = role.id if role is not None else None
        target.role = LEGACY_ROLE_BY_NAME.get(role.name) if role is not None else None
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
    if payload.password is not None:
        target.password_hash = hash_password(payload.password)
    employee_profile_service.apply_documents(db, target, payload.documents)
    if payload.documents is not None:
        _sync_identity_alias(target)
    activity_service.record(
        db, admin.organization_id, admin, "employee", "Employee updated",
        f"{target.name}'s profile was updated",
    )
    db.commit()
    if payload.password is not None:
        # A new password ends the sessions opened with the old one.
        password_service.revoke_all_refresh_tokens(db, target.id)
    db.refresh(target)
    return employee_profile_service.build_profile(db, target)


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


# ---------------------------- Employee documents -----------------------------
# Every file slot on an employee is a plain URL column, written by PATCH /users/{id}:
# upload with POST /files/upload, then send the URL in `documents` (or
# `basic_information.profile_photo`). Sending null empties a named slot and sending
# [] empties one of the lists. The one thing PATCH cannot do is drop a single file
# out of a list without resending the rest — that is what this endpoint is for.


@router.delete("/{user_id}/documents/{collection}", response_model=EmployeeProfileOut)
def delete_employee_document(
    user_id: str,
    collection: EmployeeDocumentCollection,
    document_id: str = Query(
        ...,
        description="The `file_id` from POST /files/upload — the last segment of the "
                    "document's URL",
    ),
    admin: User = Depends(_ADMIN),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> EmployeeProfileOut:
    """Remove one file from `experience_certificates`, `educational_certificates` or
    `other_documents`, leaving the rest of that list alone."""
    target = _owned_user(db, user_id, admin)
    column = COLLECTION_COLUMN[collection]
    documents = list(getattr(target, column) or [])
    remaining = [d for d in documents if d.get("id") != document_id]
    if len(remaining) == len(documents):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    # Reassigned wholesale — SQLAlchemy does not track in-place JSON mutation.
    setattr(target, column, remaining)
    db.commit()
    db.refresh(target)
    return employee_profile_service.build_profile(db, target)


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
    (attendance, notifications, sessions) go with them. Set
    `system_preferences.account_status` to `inactive` instead when the history
    should stay attributed.
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
    activity_service.record(
        db, admin.organization_id, admin, "employee", "Employee removed",
        f"{target.name} ({target.employee_id}) was removed from the firm",
    )
    db.delete(target)
    db.commit()
