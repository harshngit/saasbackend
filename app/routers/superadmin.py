from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_roles
from app.models import Organization, OrganizationStatus, Plan, UpgradeStatus, User, UserRole
from app.schemas.organization import OrganizationOut, OrgStatusUpdate, RejectUpgrade
from app.schemas.plan import PlanCreate, PlanOut, PlanUpdate
from app.services import org_service

# Every endpoint here is Super Admin only.
router = APIRouter(
    prefix="/superadmin",
    tags=["superadmin"],
    dependencies=[Depends(require_roles(UserRole.SUPER_ADMIN))],
)


def _get_org(db: Session, org_id: str) -> Organization:
    org = db.get(Organization, org_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    return org


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


@router.patch("/plans/{plan_id}/deactivate", response_model=PlanOut)
def deactivate_plan(plan_id: str, db: Session = Depends(get_db)) -> Plan:
    """Hide a plan from new selection. Never hard-delete — existing subscribers keep it."""
    plan = db.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")
    plan.is_active = False
    db.commit()
    db.refresh(plan)
    return plan


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
