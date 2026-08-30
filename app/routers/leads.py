from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core import scoping
from app.core.deps import require_permission, require_unlocked_org
from app.models import Customer, Lead, User
from app.services import numbering_service, lookup_service
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


def _owned(db: Session, id: str, org_id: str, user: User | None = None) -> Lead:
    """Accepts the UUID or the human-facing code (lead_id)."""
    record = lookup_service.by_id_or_code(
        db, Lead, id, org_id, Lead.lead_id
    )
    if record is None or (
        user is not None
        and not scoping.owns_record(db, user, record, "assigned_salesperson_id")
    ):
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
    salesperson_id = payload.assigned_salesperson_id or (user.id if scoping.scope_to_own(db, user) else None)
    if salesperson_id:
        sales = db.get(User, salesperson_id)
        if sales is None or sales.organization_id != org_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid assigned_salesperson_id")

    lead = Lead(
        organization_id=org_id,
        lead_id=payload.lead_id or numbering_service.next_number(
            db, org_id, Lead.lead_id, "LEAD"
        ),
        name=payload.name,
        contact_person=payload.contact_person,
        mobile_number=payload.mobile_number,
        email=payload.email,
        lead_source=payload.lead_source,
        interested_product=payload.interested_product,
        notes=payload.notes,
        customer_id=payload.customer_id,
        assigned_salesperson_id=salesperson_id,
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
    # A field role sees only the leads assigned to them.
    q = scoping.owned_by(q, db, user, Lead.assigned_salesperson_id)
    return q.order_by(Lead.created_at.desc()).all()


@router.get("/{id}", response_model=LeadOut)
def get_lead_detail(
    id: str,
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> Lead:
    return _owned(db, id, _org_id(user), user)


@router.patch("/{id}", response_model=LeadOut)
def update_lead(
    id: str,
    payload: LeadUpdate,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Lead:
    org_id = _org_id(user)
    lead = _owned(db, id, org_id, user)

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
        if hasattr(lead, field):
            setattr(lead, field, value)

    db.commit()
    db.refresh(lead)
    return lead


@router.post("/{id}/convert-to-customer", response_model=LeadConvertResponse)
def convert_lead_to_customer(
    id: str,
    payload: LeadConvertToCustomerIn | None = None,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> dict:
    """Convert a lead to a full customer record.

    If already converted, returns the existing customer without creating a duplicate.
    Updates the lead's status to 'won' and links `customer_id` and `converted_at`.
    """
    org_id = _org_id(user)
    lead = _owned(db, id, org_id, user)

    # 1. If already converted, return existing customer (idempotent duplicate protection)
    if lead.customer_id:
        existing_customer = db.get(Customer, lead.customer_id)
        if existing_customer and existing_customer.organization_id == org_id:
            return {
                "lead_id": lead.id,
                "customer_id": existing_customer.id,
                "lead_status": lead.lead_status or "won",
                "converted": True,
                "customer": existing_customer,
            }

    # 2. Extract customer fields, falling back to Lead's own information
    data = payload.model_dump(exclude_unset=True) if payload else {}
    cust_name = data.get("name") or lead.name or lead.contact_person or f"Lead {lead.lead_id or lead.id[:8]}"
    cust_phone = data.get("phone") or lead.mobile_number
    cust_email = data.get("email") or lead.email
    cust_contact = data.get("primary_contact_person") or lead.contact_person
    cust_assignee = data.get("assigned_sales_officer_id") or lead.assigned_salesperson_id
    if not cust_assignee and scoping.scope_to_own(db, user):
        cust_assignee = user.id
    cust_notes = data.get("notes") or lead.notes

    if cust_assignee:
        officer = db.get(User, cust_assignee)
        if officer is None or officer.organization_id != org_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="assigned_sales_officer_id is not a user in your firm",
            )

    # 3. Create customer
    new_cust_code = numbering_service.next_number(db, org_id, Customer.customer_id, "CUST")
    b_name = data.get("business_name") or data.get("legal_business_name")
    customer = Customer(
        organization_id=org_id,
        customer_id=new_cust_code,
        name=cust_name,
        business_name=b_name,
        legal_business_name=b_name,
        phone=cust_phone,
        email=cust_email,
        gst_number=data.get("gst_number"),
        billing_address=data.get("billing_address"),
        delivery_address=data.get("delivery_address"),
        assigned_sales_officer_id=cust_assignee,
        credit_limit=data.get("credit_limit") or 0.0,
        opening_balance=data.get("opening_balance") or 0.0,
        category=data.get("category"),
        notes=cust_notes,
        primary_contact_person=cust_contact,
        lead_source=lead.lead_source,
        customer_type=data.get("customer_type"),
        customer_since=data.get("customer_since"),
        status=data.get("status"),
        maps_latitude=data.get("maps_latitude"),
        maps_longitude=data.get("maps_longitude"),
    )
    customer.recompute_outstanding()
    db.add(customer)
    db.flush()

    # 4. Link lead
    lead.customer_id = customer.id
    lead.lead_status = "won"
    lead.converted_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(customer)
    db.refresh(lead)

    return {
        "lead_id": lead.id,
        "customer_id": customer.id,
        "lead_status": "won",
        "converted": True,
        "customer": customer,
    }


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_lead(
    id: str,
    user: User = Depends(_delete),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> None:
    lead = _owned(db, id, _org_id(user), user)
    db.delete(lead)
    db.commit()
