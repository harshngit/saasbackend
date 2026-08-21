from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_system_role
from app.core.security import hash_password
from app.models import Organization, OrganizationStatus, Plan, SystemRole, UpgradeStatus, User, UserRole
from app.schemas.organization import OrganizationOut, OrgStatusUpdate, RejectUpgrade
from app.schemas.plan import PlanCreate, PlanOut, PlanStatusUpdate, PlanUpdate
from app.schemas.superadmin import SuperAdminCreate, SuperAdminUpdate
from app.schemas.user import UserOut
from app.services import org_service, password_service

# Every endpoint here is Super Admin only.
_super_admin_guard = require_system_role(SystemRole.SUPER_ADMIN)

router = APIRouter(
    prefix="/superadmin",
    tags=["superadmin"],
    dependencies=[Depends(_super_admin_guard)],
)


def _get_org(db: Session, org_id: str) -> Organization:
    org = db.get(Organization, org_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    return org


# --------------------------- Super Admin management ---------------------------
# Super Admin is a platform-level account (organization_id is always NULL — see
# User's docstring), so there is no tenant/organization to scope these by. The
# router-level `_super_admin_guard` dependency already keeps every route below
# unreachable for a normal user or a firm Admin.


def _is_super_admin(candidate: User) -> bool:
    return candidate.system_role == SystemRole.SUPER_ADMIN.value or candidate.role == UserRole.SUPER_ADMIN


def _get_super_admin(db: Session, admin_id: str) -> User:
    target = db.get(User, admin_id)
    if target is None or not _is_super_admin(target):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Super Admin not found")
    return target


def _super_admin_count(db: Session) -> int:
    return (
        db.query(User)
        .filter(or_(User.system_role == SystemRole.SUPER_ADMIN.value, User.role == UserRole.SUPER_ADMIN))
        .count()
    )


def _email_taken(db: Session, email: str, exclude_id: str | None = None) -> bool:
    query = db.query(User).filter(User.email == email)
    if exclude_id is not None:
        query = query.filter(User.id != exclude_id)
    return db.query(query.exists()).scalar()


@router.post("/admins", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_super_admin(payload: SuperAdminCreate, db: Session = Depends(get_db)) -> User:
    """Create another platform Super Admin.

    Only a Super Admin can reach this route (enforced by the router-level guard,
    the same `require_system_role` dependency every other endpoint here uses — not
    a bare `role ==` check). The new account is global: `organization_id` is null,
    exactly like the seeded Super Admin in `app.seed`.
    """
    if _email_taken(db, payload.email):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")
    admin = User(
        organization_id=None,
        name=payload.name,
        email=payload.email,
        phone=payload.phone,
        password_hash=hash_password(payload.password),
        # Both fields, matching app.seed.seed_super_admin — role is what
        # POST /auth/refresh reads, system_role is what everything else reads.
        role=UserRole.SUPER_ADMIN,
        system_role=SystemRole.SUPER_ADMIN.value,
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return admin


@router.get("/admins", response_model=list[UserOut])
def list_super_admins(db: Session = Depends(get_db)) -> list[User]:
    """Every platform Super Admin. `UserOut` never carries `password_hash`."""
    return (
        db.query(User)
        .filter(or_(User.system_role == SystemRole.SUPER_ADMIN.value, User.role == UserRole.SUPER_ADMIN))
        .order_by(User.created_at.desc())
        .all()
    )


@router.patch("/admins/{admin_id}", response_model=UserOut)
def update_super_admin(
    admin_id: str,
    payload: SuperAdminUpdate,
    caller: User = Depends(_super_admin_guard),
    db: Session = Depends(get_db),
) -> User:
    """Change a Super Admin's own account details.

    There is no separate Super Admin "role" to reassign or transfer — system_role
    is a fixed platform flag on the user row (see User.effective_system_role), so
    "change Super Admin" here means editing that account: name, email, phone,
    password or active state. Same shape as PATCH /users/{id} uses for firm staff.
    """
    target = _get_super_admin(db, admin_id)
    data = payload.model_dump(exclude_unset=True)

    if data.get("email") and data["email"] != target.email and _email_taken(db, data["email"], exclude_id=target.id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    password = data.pop("password", None)
    is_active = data.pop("is_active", None)
    for field, value in data.items():
        setattr(target, field, value)
    if password:
        target.password_hash = hash_password(password)
    if is_active is not None:
        if target.id == caller.id and not is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot deactivate your own account"
            )
        target.is_active = is_active

    db.commit()
    if password:
        # A new password ends the sessions opened with the old one.
        password_service.revoke_all_refresh_tokens(db, target.id)
    db.refresh(target)
    return target


@router.delete("/admins/{admin_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_super_admin(
    admin_id: str,
    caller: User = Depends(_super_admin_guard),
    db: Session = Depends(get_db),
) -> None:
    """Permanently remove a platform Super Admin (hard delete, matching DELETE
    /users/{id} and DELETE /superadmin/organizations/{id} conventions elsewhere).

    A Super Admin cannot delete their own account, and the last remaining Super
    Admin can never be deleted — the platform must always keep at least one.
    """
    target = _get_super_admin(db, admin_id)
    if target.id == caller.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot delete your own account")
    if _super_admin_count(db) <= 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot delete the last remaining Super Admin"
        )
    password_service.revoke_all_refresh_tokens(db, target.id)
    db.delete(target)
    db.commit()


@router.get("/organizations", response_model=list[OrganizationOut])
def list_organizations(
    status_filter: OrganizationStatus | None = Query(default=None, alias="status"),
    upgrade_status: UpgradeStatus | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[Organization]:
    """List all organizations, optionally filtered by ?status= and ?upgrade_status=."""
    query = db.query(Organization)
    if status_filter is not None:
        query = query.filter(Organization.status == status_filter)
    if upgrade_status is not None:
        query = query.filter(Organization.upgrade_status == upgrade_status.value)
    return query.order_by(Organization.created_at.desc()).all()


# ----------------------------- Plan catalog management -----------------------------


@router.get("/plans", response_model=list[PlanOut])
def list_all_plans(db: Session = Depends(get_db)) -> list[Plan]:
    """All plans (active + inactive) for the Super Admin management view."""
    return db.query(Plan).order_by(Plan.price_monthly).all()


@router.post("/plans", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
def create_plan(payload: PlanCreate, db: Session = Depends(get_db)) -> Plan:
    plan = Plan(**payload.model_dump())
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


@router.put("/plans/{plan_id}", response_model=PlanOut)
def update_plan(plan_id: str, payload: PlanUpdate, db: Session = Depends(get_db)) -> Plan:
    plan = db.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(plan, field, value)
    db.commit()
    db.refresh(plan)
    return plan


@router.patch("/plans/{plan_id}/status", response_model=PlanOut)
def set_plan_status(plan_id: str, payload: PlanStatusUpdate, db: Session = Depends(get_db)) -> Plan:
    """Toggle a plan active/inactive (both directions). Inactive plans are hidden
    from GET /plans but never hard-deleted, so existing subscribers keep working."""
    plan = db.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")
    plan.is_active = payload.is_active
    db.commit()
    db.refresh(plan)
    return plan


@router.patch("/plans/{plan_id}/deactivate", response_model=PlanOut)
def deactivate_plan(plan_id: str, db: Session = Depends(get_db)) -> Plan:
    """(Kept for compatibility — prefer PATCH /plans/{id}/status.) Hide a plan."""
    plan = db.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")
    plan.is_active = False
    db.commit()
    db.refresh(plan)
    return plan


@router.delete("/plans/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_plan(plan_id: str, db: Session = Depends(get_db)) -> None:
    """Permanently remove an unused subscription plan from the catalog.

    Refused if the plan is currently assigned to any organization (plan_id) or has
    pending/historical upgrade requests referencing it (requested_plan_id) — deactivate
    the plan via PATCH /plans/{id}/status instead so existing subscribers keep functioning.
    Default plans cannot be deleted.
    """
    plan = db.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")

    if plan.is_default:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The default plan cannot be deleted. Set another plan as default first.",
        )

    used_count = db.query(Organization).filter(Organization.plan_id == plan.id).count()
    if used_count > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot delete plan: {used_count} organization(s) are currently on this plan. Deactivate it instead.",
        )

    requested_count = db.query(Organization).filter(Organization.requested_plan_id == plan.id).count()
    if requested_count > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot delete plan: {requested_count} organization(s) have requested this plan. Deactivate it instead.",
        )

    db.delete(plan)
    db.commit()


@router.get("/organizations/{org_id}", response_model=OrganizationOut)
def get_organization(org_id: str, db: Session = Depends(get_db)) -> Organization:
    return _get_org(db, org_id)


@router.patch("/organizations/{org_id}/approve-upgrade", response_model=OrganizationOut)
def approve_upgrade(org_id: str, db: Session = Depends(get_db)) -> Organization:
    """Approve the pending upgrade: activate the org on its requested plan."""
    org = _get_org(db, org_id)
    return org_service.approve_upgrade(db, org)


@router.patch("/organizations/{org_id}/reject-upgrade", response_model=OrganizationOut)
def reject_upgrade(org_id: str, payload: RejectUpgrade, db: Session = Depends(get_db)) -> Organization:
    org = _get_org(db, org_id)
    return org_service.reject_upgrade(db, org, payload.reason)


@router.patch("/organizations/{org_id}/status", response_model=OrganizationOut)
def override_status(org_id: str, payload: OrgStatusUpdate, db: Session = Depends(get_db)) -> Organization:
    """Manual status override (e.g. suspend an abusive account, or reactivate)."""
    org = _get_org(db, org_id)
    return org_service.set_status(db, org, payload.status)


@router.delete("/organizations/{org_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_organization(org_id: str, db: Session = Depends(get_db)) -> None:
    """Permanently delete an organization and all its data (users, customers,
    products, roles, etc. cascade). Irreversible — Super Admin only."""
    org = _get_org(db, org_id)
    db.delete(org)
    db.commit()
