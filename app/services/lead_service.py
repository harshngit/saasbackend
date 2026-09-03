"""Lead lifecycle: creation defaults, assignment, status workflow, and
conversion to Customer — the business rules app/routers/leads.py enforces.
"""

from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import or_
from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session

from app.core import scoping
from app.core.workflow import LEAD_STATUSES, LeadTransitionError, validate_lead_transition
from app.models import Customer, Lead, Quotation, Role, User
from app.schemas.lead import LeadConvertToCustomerIn, LeadCreate, LeadUpdate
from app.services import lookup_service, numbering_service, role_service


def _require_manual_transition(current: str | None, new: str) -> None:
    try:
        validate_lead_transition(current or "new", new)
    except LeadTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc


def is_converted(lead: Lead) -> bool:
    """A Lead counts as converted once it has actually been through
    convert_lead_to_customer() — signalled by `converted_at` (set nowhere
    else) or `lead_status == "won"` (also set nowhere else, and kept as a
    defensive fallback for any pre-existing row that reached 'won' before
    this protection existed).

    Deliberately NOT based on `customer_id` alone: a Lead may be created with
    a `customer_id` already pointing at an existing Customer (a pre-existing,
    still-supported way to pre-link a Lead) without ever having gone through
    conversion — that Lead must stay editable.
    """
    return lead.converted_at is not None or lead.lead_status == "won"


def is_lead_assignable(db: Session, candidate: User) -> bool:
    """Whether `candidate` may be set as a Lead's assigned_salesperson_id.

    Reuses the existing role/permission infrastructure rather than hardcoding
    a role name: Admins and Super Admins always qualify; otherwise a role
    counts either by its seeded workspace ("sales", same signal
    delivery_service.is_delivery_partner uses for "delivery") or by actually
    holding `leads:view` — so a custom role an Admin has deliberately given
    Leads access to still works, without a second, drifting copy of "who can
    touch Leads" logic.
    """
    if role_service.is_full_access(candidate):
        return True
    if candidate.role_id:
        role = db.get(Role, candidate.role_id)
        if role is not None:
            if role.workspace == "sales":
                return True
            if (role.permissions or {}).get("leads", {}).get("view"):
                return True
    return False


def _validate_assignee(db: Session, org_id: str, salesperson_id: str) -> None:
    sales = db.get(User, salesperson_id)
    if sales is None or sales.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid assigned_salesperson_id")
    if not is_lead_assignable(db, sales):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="assigned_salesperson_id must be a user with Leads access (e.g. a Sales Officer)",
        )


def get_lead(db: Session, org_id: str, lead_id: str, user: User | None = None) -> Lead:
    """Accepts the UUID or the human-facing code (lead_id)."""
    record = lookup_service.by_id_or_code(db, Lead, lead_id, org_id, Lead.lead_id)
    if record is None or (
        user is not None and not scoping.owns_record(db, user, record, "assigned_salesperson_id")
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lead not found")
    return record


def create_lead(db: Session, org_id: str, user: User, payload: LeadCreate) -> Lead:
    if payload.customer_id:
        cust = db.get(Customer, payload.customer_id)
        if cust is None or cust.organization_id != org_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid customer_id")

    # An "own"-scope role (Sales Officer by default) may only create Leads for
    # themselves: an explicit assignee that isn't them is rejected outright,
    # never silently reassigned.
    salesperson_id = payload.assigned_salesperson_id
    if scoping.scope_to_own(db, user):
        if salesperson_id and salesperson_id != user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You may only create Leads assigned to yourself",
            )
        salesperson_id = user.id
    if salesperson_id:
        _validate_assignee(db, org_id, salesperson_id)

    lead = Lead(
        organization_id=org_id,
        lead_id=payload.lead_id or numbering_service.next_number(db, org_id, Lead.lead_id, "LEAD"),
        name=payload.name,
        contact_person=payload.contact_person,
        mobile_number=payload.mobile_number,
        email=payload.email,
        lead_source=payload.lead_source,
        interested_product=payload.interested_product,
        notes=payload.notes,
        customer_id=payload.customer_id,
        assigned_salesperson_id=salesperson_id,
        lead_status="new",
    )

    db.add(lead)
    db.commit()
    db.refresh(lead)
    return lead


def list_leads(
    db: Session,
    org_id: str,
    user: User,
    status_filter: str | None = None,
    assigned_salesperson_id: str | None = None,
    lead_source: str | None = None,
    search: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Lead]:
    query = db.query(Lead).filter(Lead.organization_id == org_id)

    if status_filter:
        if status_filter not in LEAD_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid status '{status_filter}'. Valid statuses: {list(LEAD_STATUSES)}",
            )
        query = query.filter(Lead.lead_status == status_filter)
    if assigned_salesperson_id:
        query = query.filter(Lead.assigned_salesperson_id == assigned_salesperson_id)
    if lead_source:
        query = query.filter(Lead.lead_source == lead_source)
    if search:
        like = f"%{search}%"
        query = query.filter(
            or_(
                Lead.name.ilike(like),
                Lead.mobile_number.ilike(like),
                Lead.email.ilike(like),
                Lead.lead_id.ilike(like),
            )
        )
    if created_from:
        query = query.filter(Lead.created_at >= created_from)
    if created_to:
        query = query.filter(Lead.created_at <= created_to)

    # A field role (data_scope "own") sees only the Leads assigned to them.
    query = scoping.owned_by(query, db, user, Lead.assigned_salesperson_id)
    return query.order_by(Lead.created_at.desc()).offset(offset).limit(limit).all()


def update_lead(db: Session, org_id: str, lead_id: str, user: User, payload: LeadUpdate) -> Lead:
    lead = get_lead(db, org_id, lead_id, user)
    data = payload.model_dump(exclude_unset=True)
    if not data:
        return lead

    # A converted Lead is a closed record: its conversion history (status,
    # customer link, timestamp) must not be corrupted, and its business
    # fields are frozen too rather than letting them silently drift out of
    # sync with the Customer they produced.
    if is_converted(lead):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This Lead has already been converted to a Customer and can no longer be edited",
        )

    if "customer_id" in data or "converted_customer_id" in data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="customer_id can only be set by POST /leads/{id}/convert-to-customer",
        )

    new_status = data.get("lead_status") or data.get("status")
    if new_status:
        _require_manual_transition(lead.lead_status, new_status)

    assignee_touched = "assigned_salesperson_id" in data or "assigned_sales_officer_id" in data
    new_assignee = data.get("assigned_salesperson_id") or data.get("assigned_sales_officer_id")
    if assignee_touched:
        # An "own"-scope role may not hand their Lead to someone else.
        if scoping.scope_to_own(db, user) and new_assignee and new_assignee != user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You may only assign Leads to yourself",
            )
        if new_assignee:
            _validate_assignee(db, org_id, new_assignee)

    for field, value in data.items():
        if field in ("customer_id", "converted_customer_id"):
            continue
        if field in ("lead_status", "status") and value is None:
            continue
        if hasattr(lead, field):
            setattr(lead, field, value)

    db.commit()
    db.refresh(lead)
    return lead


def delete_lead(db: Session, org_id: str, lead_id: str, user: User) -> None:
    lead = get_lead(db, org_id, lead_id, user)
    if is_converted(lead):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This Lead has been converted to a Customer and cannot be deleted",
        )
    # Quotation.lead_id declares ON DELETE SET NULL, but that only actually
    # exists in the database for a `quotations` table created fresh via
    # create_all() — a table that already existed before this column shipped
    # only ever gets the bare column from auto_add_missing_columns()'s
    # `ALTER TABLE ... ADD COLUMN`, never the constraint (and SQLite cannot
    # add a foreign key to an existing table via ALTER TABLE at all, so this
    # gap cannot be closed there after the fact). Null the reference here
    # explicitly so a Lead's quotations survive its deletion on every
    # database this app actually runs against, not only a brand new one.
    db.query(Quotation).filter(Quotation.lead_id == lead.id).update({"lead_id": None})
    db.delete(lead)
    db.commit()


def convert_lead_to_customer(
    db: Session, org_id: str, user: User, lead: Lead, payload: LeadConvertToCustomerIn | None
) -> dict:
    """Convert a Lead to a full Customer record. Does the commit itself.

    If already converted, returns the existing Customer without creating a
    duplicate. Updates the Lead's status to 'won' and links `customer_id` and
    `converted_at`.

    Concurrency: the Lead row is locked with a real `UPDATE` before the
    conversion-state check below, so two simultaneous calls for the same
    never-converted Lead serialize instead of both reading `customer_id is
    None` and each creating a Customer. A real `UPDATE` is used rather than
    `.with_for_update()` on purpose — `Lead.customer` and
    `Lead.assigned_salesperson` are `lazy="joined"`, and Postgres refuses
    `FOR UPDATE` on the nullable side of the `LEFT OUTER JOIN` that a locked
    SELECT against this model would pull in (the same failure class fixed for
    WarehouseStock locking in stock_service.py). The no-op UPDATE below takes
    a genuine row lock on Postgres, and SQLite's file-level write lock, the
    same pattern already proven in delivery_service.load().
    """
    db.execute(sa_update(Lead).where(Lead.id == lead.id).values(lead_status=Lead.lead_status))
    db.refresh(lead)

    # 1. If already converted (by us or by the request we were racing),
    # return the existing customer — idempotent duplicate protection.
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

    # 2. Extract customer fields, falling back to the Lead's own information.
    data = payload.model_dump(exclude_unset=True) if payload else {}
    cust_name = data.get("name") or lead.name or lead.contact_person or f"Lead {lead.lead_id or lead.id[:8]}"
    cust_phone = data.get("phone") or lead.mobile_number
    cust_email = data.get("email") or lead.email
    cust_contact = data.get("primary_contact_person") or lead.contact_person
    cust_assignee = data.get("assigned_sales_officer_id") or lead.assigned_salesperson_id
    # Same self-assignment rule as Lead creation/update: an "own"-scope role
    # converting a Lead cannot hand the resulting Customer to someone else —
    # otherwise the create/update restriction above would be pointless to
    # route around via the convert endpoint.
    if scoping.scope_to_own(db, user):
        if cust_assignee and cust_assignee != user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You may only convert Leads into Customers assigned to yourself",
            )
        cust_assignee = user.id
    cust_notes = data.get("notes") or lead.notes

    if cust_assignee:
        _validate_assignee(db, org_id, cust_assignee)

    # 3. Create customer.
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

    # 4. Link lead.
    lead.customer_id = customer.id
    lead.lead_status = "won"
    lead.converted_at = datetime.now(timezone.utc)

    # 5. Carry this Lead's quotations over to the new Customer, same
    # transaction — the relationship must survive a page refresh or a
    # different device from the moment conversion succeeds, not depend on a
    # follow-up PATCH the frontend might never send. lead_id is deliberately
    # preserved rather than cleared: it is the quotation's history of where it
    # came from, and Quotation.lead_id/.customer_id are only mutually
    # exclusive at the point a *new* quotation is created or a user actively
    # re-targets one via PATCH (see quotation_service._validate_party_for_*) —
    # not for this system-driven "the Lead behind it just became a real
    # Customer" transition. Only quotations not already linked to some other
    # Customer are touched, so a quotation a user already manually re-pointed
    # elsewhere is left alone.
    db.query(Quotation).filter(
        Quotation.organization_id == org_id,
        Quotation.lead_id == lead.id,
        Quotation.customer_id.is_(None),
    ).update({"customer_id": customer.id})

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
