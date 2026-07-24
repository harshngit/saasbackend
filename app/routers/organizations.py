from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_roles
from app.models import Plan, User, UserRole
from app.schemas.organization import OrganizationOut, UpgradeRequest
from app.services import org_service

router = APIRouter(prefix="/organizations", tags=["organizations"])


@router.get("/me", response_model=OrganizationOut)
def my_organization(
    admin: User = Depends(require_roles(UserRole.ADMIN)),
    db: Session = Depends(get_db),
) -> object:
    """Current org state (status, plan, trial, upgrade status). Works even when locked."""
    org = org_service.apply_trial_expiry(db, admin.organization)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No organization")
    return org


@router.post("/upgrade-request", response_model=OrganizationOut)
def request_upgrade(
    payload: UpgradeRequest,
    admin: User = Depends(require_roles(UserRole.ADMIN)),
    db: Session = Depends(get_db),
) -> object:
    """Admin submits an upgrade request. Allowed even while locked — this is how a
    locked org escapes the lock. A Super Admin then approves it."""
    org = admin.organization
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No organization")

    plan = db.get(Plan, payload.requested_plan_id)
    if plan is None or not plan.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Selected plan does not exist or is no longer available",
        )
    return org_service.request_upgrade(db, org, plan, payload.billing_cycle)
