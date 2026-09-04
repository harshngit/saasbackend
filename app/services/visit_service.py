from datetime import datetime, timezone
from fastapi import HTTPException, status
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.core import scoping
from app.core.workflow import VISIT_OUTCOMES, VISIT_STATUSES, VisitTransitionError, validate_visit_transition
from app.models import Customer, User, Visit
from app.schemas.visit import VisitCreate, VisitUpdate
from app.services import lead_service


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_visit(db: Session, org_id: str, user: User, payload: VisitCreate) -> Visit:
    # Ensure at least one of customer_id or lead_id is provided
    if not payload.customer_id and not payload.lead_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one of customer_id or lead_id must be provided"
        )

    # 1. Validate customer if provided
    if payload.customer_id:
        cust = db.get(Customer, payload.customer_id)
        if cust is None or cust.organization_id != org_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid customer_id")

    # 2. Validate optional lead if provided. Reuses
    # lead_service.validate_lead_reference rather than a raw db.get: it
    # enforces both the organization match and — for an "own"-scope role
    # (Sales Officer) — that this is actually a Lead they're allowed to
    # reference, while keeping the existing 400 "Invalid lead_id" contract.
    lead = None
    if payload.lead_id:
        lead = lead_service.validate_lead_reference(db, org_id, user, payload.lead_id)

    # 3. Validate user / salesperson. When this visit is for a Lead and no
    # user_id was explicitly given, default to that Lead's own salesperson
    # rather than the creator — otherwise an Admin logging a visit for a
    # Lead assigned to a Sales Officer would default it to themselves, and
    # the assigned officer (whose visibility is scoped to user_id) would
    # never see it. Preserves the existing default (the creating user) for
    # every other case.
    user_id = payload.user_id
    if not user_id:
        user_id = lead.assigned_salesperson_id if lead is not None and lead.assigned_salesperson_id else user.id
    sales = db.get(User, user_id)
    if sales is None or sales.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid user_id")

    visit_status = payload.status or "planned"
    if visit_status not in VISIT_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status '{visit_status}'. Valid statuses: {sorted(VISIT_STATUSES)}",
        )

    if payload.outcome is not None and payload.outcome not in VISIT_OUTCOMES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid outcome '{payload.outcome}'. Valid outcomes: {sorted(VISIT_OUTCOMES)}",
        )

    now = _now()
    # A visit created directly at a non-"planned" status still gets the
    # timestamp(s) that status implies, for the same reason update_visit
    # does — e.g. a client that logs a visit after the fact as already
    # "completed" (see TEST 1 in test_division4_visit_followup.py, which has
    # always created visits this way).
    checked_in_at = now if visit_status == "in_progress" else None
    checked_out_at = now if visit_status == "completed" else None
    completed_at = now if visit_status == "completed" else None
    cancelled_at = now if visit_status == "cancelled" else None

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
        checked_in_at=checked_in_at,
        checked_out_at=checked_out_at,
        completed_at=completed_at,
        cancelled_at=cancelled_at,
        cancellation_reason=payload.cancellation_reason,
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

    data = payload.model_dump(exclude_unset=True)

    # Ensure at least one of customer_id or lead_id remains
    new_customer_id = data.get("customer_id", visit.customer_id) if "customer_id" in data else visit.customer_id
    new_lead_id = data.get("lead_id", visit.lead_id) if "lead_id" in data else visit.lead_id
    if not new_customer_id and not new_lead_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one of customer_id or lead_id must be provided"
        )

    if "customer_id" in data and data["customer_id"]:
        cust = db.get(Customer, data["customer_id"])
        if cust is None or cust.organization_id != org_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid customer_id")

    if "lead_id" in data and data["lead_id"]:
        lead_service.validate_lead_reference(db, org_id, user, data["lead_id"])

    if "user_id" in data and data["user_id"]:
        sales = db.get(User, data["user_id"])
        if sales is None or sales.organization_id != org_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid user_id")

    if "outcome" in data and data["outcome"] is not None and data["outcome"] not in VISIT_OUTCOMES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid outcome '{data['outcome']}'. Valid outcomes: {sorted(VISIT_OUTCOMES)}",
        )

    if "status" in data and data["status"]:
        new_status = data["status"]
        try:
            validate_visit_transition(visit.status, new_status)
        except VisitTransitionError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message)

        now = _now()
        # Each timestamp is set only the first time its transition happens
        # and never overwritten afterwards — matches the same
        # set-only-if-null rule complete_follow_up already uses for
        # FollowUp.completed_at.
        if new_status == "in_progress" and visit.checked_in_at is None:
            visit.checked_in_at = now
        elif new_status == "completed":
            # Reachable directly from "planned" (skipping check-in) for
            # backward compatibility -- see TEST 4 in
            # test_division4_visit_followup.py, which has always completed
            # a "planned" visit directly. checked_in_at is deliberately left
            # untouched in that case rather than backfilled with a fake time.
            if visit.checked_out_at is None:
                visit.checked_out_at = now
            if visit.completed_at is None:
                visit.completed_at = now
        elif new_status == "cancelled" and visit.cancelled_at is None:
            visit.cancelled_at = now

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
