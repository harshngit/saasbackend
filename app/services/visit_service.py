from datetime import datetime, timezone
from fastapi import HTTPException, status
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.core import scoping
from app.models import Customer, Lead, User, Visit
from app.schemas.visit import VisitCreate, VisitUpdate


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_visit(db: Session, org_id: str, user: User, payload: VisitCreate) -> Visit:
    # 1. Validate customer
    cust = db.get(Customer, payload.customer_id)
    if cust is None or cust.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid customer_id")

    # 2. Validate optional lead
    if payload.lead_id:
        lead = db.get(Lead, payload.lead_id)
        if lead is None or lead.organization_id != org_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid lead_id")

    # 3. Validate user / salesperson
    user_id = payload.user_id or user.id
    if user_id:
        sales = db.get(User, user_id)
        if sales is None or sales.organization_id != org_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid user_id")

    valid_statuses = {"planned", "completed", "cancelled"}
    visit_status = payload.status or "planned"
    if visit_status not in valid_statuses:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status '{visit_status}'. Valid statuses: {sorted(valid_statuses)}",
        )

    visit = Visit(
        organization_id=org_id,
        customer_id=payload.customer_id,
        lead_id=payload.lead_id,
        user_id=user_id,
        visit_date=payload.visit_date or _now(),
        visit_type=payload.visit_type or "meeting",
        purpose=payload.purpose,
        notes=payload.notes,
        outcome=payload.outcome,
        status=visit_status,
        location=payload.location,
    )
    db.add(visit)
    db.commit()
    db.refresh(visit)
    return visit


def get_visit(db: Session, org_id: str, visit_id: str, user: User | None = None) -> Visit:
    visit = db.get(Visit, visit_id)
    if visit is None or visit.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Visit not found")
    if user is not None and not scoping.owns_record(db, user, visit, "user_id"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Visit not found")
    return visit


def list_visits(
    db: Session,
    org_id: str,
    user: User,
    customer_id: str | None = None,
    lead_id: str | None = None,
    salesperson_id: str | None = None,
    status_filter: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Visit]:
    query = db.query(Visit).filter(Visit.organization_id == org_id)

    if customer_id:
        query = query.filter(Visit.customer_id == customer_id)
    if lead_id:
        query = query.filter(Visit.lead_id == lead_id)
    if salesperson_id:
        query = query.filter(Visit.user_id == salesperson_id)
    if status_filter:
        query = query.filter(Visit.status == status_filter)
    if date_from:
        query = query.filter(Visit.visit_date >= date_from)
    if date_to:
        query = query.filter(Visit.visit_date <= date_to)

    query = scoping.owned_by(query, db, user, Visit.user_id)
    return query.order_by(desc(Visit.visit_date), desc(Visit.created_at)).offset(offset).limit(limit).all()


def update_visit(db: Session, org_id: str, visit_id: str, user: User, payload: VisitUpdate) -> Visit:
    visit = get_visit(db, org_id, visit_id, user)

    valid_statuses = {"planned", "completed", "cancelled"}
    data = payload.model_dump(exclude_unset=True)

    if "customer_id" in data and data["customer_id"]:
        cust = db.get(Customer, data["customer_id"])
        if cust is None or cust.organization_id != org_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid customer_id")

    if "lead_id" in data and data["lead_id"]:
        lead = db.get(Lead, data["lead_id"])
        if lead is None or lead.organization_id != org_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid lead_id")

    if "user_id" in data and data["user_id"]:
        sales = db.get(User, data["user_id"])
        if sales is None or sales.organization_id != org_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid user_id")

    if "status" in data and data["status"]:
        new_status = data["status"]
        if new_status not in valid_statuses:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid status '{new_status}'. Valid statuses: {sorted(valid_statuses)}",
            )
        visit.status = new_status

    for k, v in data.items():
        if k != "status":
            setattr(visit, k, v)

    visit.updated_at = _now()
    db.commit()
    db.refresh(visit)
    return visit


def delete_visit(db: Session, org_id: str, visit_id: str, user: User) -> None:
    visit = get_visit(db, org_id, visit_id, user)
    db.delete(visit)
    db.commit()
