from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.models import Lead, User
from app.services import lead_service
from app.schemas.lead import (
    LeadConvertToCustomerIn,
    LeadConvertResponse,
    LeadCreate,
    LeadOut,
    LeadUpdate,
)

router = APIRouter(prefix="/leads", tags=["leads"])

_view = require_permission("leads", "view")
_create = require_permission("leads", "create")
_edit = require_permission("leads", "edit")
_delete = require_permission("leads", "delete")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account")
    return user.organization_id


@router.post("", response_model=LeadOut, status_code=status.HTTP_201_CREATED)
def create_lead(
    payload: LeadCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Lead:
    return lead_service.create_lead(db, _org_id(user), user, payload)


@router.get("", response_model=list[LeadOut])
def list_leads(
    user: User = Depends(_view),
    status_filter: str | None = Query(default=None, alias="status"),
    assigned_salesperson_id: str | None = Query(default=None),
    lead_source: str | None = Query(default=None),
    search: str | None = Query(default=None, description="Matches name / mobile_number / email / lead_id"),
    created_from: datetime | None = Query(default=None, description="Leads created on or after this timestamp"),
    created_to: datetime | None = Query(default=None, description="Leads created on or before this timestamp"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[Lead]:
    return lead_service.list_leads(
        db,
        _org_id(user),
        user,
        status_filter=status_filter,
        assigned_salesperson_id=assigned_salesperson_id,
        lead_source=lead_source,
        search=search,
        created_from=created_from,
        created_to=created_to,
        limit=limit,
        offset=offset,
    )


@router.get("/{id}", response_model=LeadOut)
def get_lead_detail(
    id: str,
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> Lead:
    return lead_service.get_lead(db, _org_id(user), id, user)


@router.patch("/{id}", response_model=LeadOut)
def update_lead(
    id: str,
    payload: LeadUpdate,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Lead:
    return lead_service.update_lead(db, _org_id(user), id, user, payload)


@router.post("/{id}/convert-to-customer", response_model=LeadConvertResponse)
def convert_lead_to_customer(
    id: str,
    payload: LeadConvertToCustomerIn | None = None,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> dict:
    """Convert a lead to a full customer record.

    If already converted, returns the existing customer without creating a
    duplicate. Updates the lead's status to 'won' and links `customer_id` and
    `converted_at`. Safe under concurrent double-submission — see
    lead_service.convert_lead_to_customer for the locking strategy.
    """
    org_id = _org_id(user)
    lead = lead_service.get_lead(db, org_id, id, user)
    return lead_service.convert_lead_to_customer(db, org_id, user, lead, payload)


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_lead(
    id: str,
    user: User = Depends(_delete),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> None:
    lead_service.delete_lead(db, _org_id(user), id, user)
