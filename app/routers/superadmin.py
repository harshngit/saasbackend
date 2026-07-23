from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_roles
from app.models import Organization, OrganizationStatus, User, UserRole
from app.schemas.organization import OrganizationOut, OrgStatusUpdate, RejectUpgrade
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
    db: Session = Depends(get_db),
) -> list[Organization]:
    """List all organizations on the platform, optionally filtered by ?status=."""
    query = db.query(Organization)
    if status_filter is not None:
        query = query.filter(Organization.status == status_filter)
    return query.order_by(Organization.created_at.desc()).all()


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
