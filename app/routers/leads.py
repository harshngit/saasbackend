from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.models import Customer, Lead, User
from app.services import numbering_service, lookup_service
from app.schemas.lead import LeadCreate, LeadOut, LeadUpdate

router = APIRouter(prefix="/leads", tags=["leads"])

_view = require_permission("leads", "view")
_create = require_permission("leads", "create")
_edit = require_permission("leads", "edit")
_delete = require_permission("leads", "delete")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account")
    return user.organization_id


def _owned(db: Session, id: str, org_id: str) -> Lead:
    """Accepts the UUID or the human-facing code (lead_id)."""
    record = lookup_service.by_id_or_code(
        db, Lead, id, org_id, Lead.lead_id
    )
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lead not found")
    return record


@router.post("", response_model=LeadOut, status_code=status.HTTP_201_CREATED)
def create_lead(
    payload: LeadCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Lead:
    org_id = _org_id(user)

    # Validate customer if provided
    if payload.customer_id:
        cust = db.get(Customer, payload.customer_id)
        if cust is None or cust.organization_id != org_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid customer_id")

    # Validate salesperson if provided
    if payload.assigned_salesperson_id:
        sales = db.get(User, payload.assigned_salesperson_id)
        if sales is None or sales.organization_id != org_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid assigned_salesperson_id")

    lead = Lead(
        organization_id=org_id,
        lead_id=payload.lead_id or numbering_service.next_number(
            db, org_id, Lead.lead_id, "LEAD"
        ),
        lead_source=payload.lead_source,
        customer_id=payload.customer_id,
        mobile_number=payload.mobile_number,
        assigned_salesperson_id=payload.assigned_salesperson_id,
        lead_status=payload.lead_status or "new",
    )

    db.add(lead)
    db.commit()
    db.refresh(lead)
    return lead


@router.get("", response_model=list[LeadOut])
def list_leads(
    user: User = Depends(_view),
    status_filter: str | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
) -> list[Lead]:
    org_id = _org_id(user)
    q = db.query(Lead).filter(Lead.organization_id == org_id)
    if status_filter:
        q = q.filter(Lead.lead_status == status_filter)
    return q.order_by(Lead.created_at.desc()).all()


@router.get("/{id}", response_model=LeadOut)
def get_lead_detail(
    id: str,
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> Lead:
    return _owned(db, id, _org_id(user))


@router.put("/{id}", response_model=LeadOut)
def update_lead(
    id: str,
    payload: LeadUpdate,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Lead:
    org_id = _org_id(user)
    lead = _owned(db, id, org_id)

    # Validate customer if provided
    if payload.customer_id:
        cust = db.get(Customer, payload.customer_id)
        if cust is None or cust.organization_id != org_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid customer_id")

    # Validate salesperson if provided
    if payload.assigned_salesperson_id:
        sales = db.get(User, payload.assigned_salesperson_id)
        if sales is None or sales.organization_id != org_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid assigned_salesperson_id")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(lead, field, value)

    db.commit()
    db.refresh(lead)
    return lead


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_lead(
    id: str,
    user: User = Depends(_delete),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> None:
    lead = _owned(db, id, _org_id(user))
    db.delete(lead)
    db.commit()
