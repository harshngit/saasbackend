from datetime import datetime, timezone
from fastapi import HTTPException, status
from sqlalchemy import asc, desc
from sqlalchemy.orm import Session

from app.core import scoping
from app.models import Customer, FollowUp, Lead, User, Visit
from app.schemas.follow_up import FollowUpComplete, FollowUpCreate, FollowUpUpdate
from app.services import lead_service


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_follow_up(
    db: Session,
    org_id: str,
    user: User,
    payload: FollowUpCreate,
    default_visit_id: str | None = None,
) -> FollowUp:
    """A follow-up may belong directly to a Customer, directly to a Lead
    (before conversion — no Visit required), or reach either transitively
    through a Visit. All three ways of arriving at a parent are independent
    and may be combined (e.g. an explicit lead_id plus a visit_id for the
    same lead) as long as they don't conflict.
    """
    visit_id = payload.visit_id or default_visit_id
    visit = None
    if visit_id:
        visit = db.get(Visit, visit_id)
        # Org match AND — for an "own"-scope role — that this Visit is
        # actually theirs (Visit.user_id, the same field visit_service.get_visit
        # already scopes direct access by), so a Sales Officer cannot link a
        # Follow-up onto a colleague's Visit just by knowing its id.
        if (
            visit is None
            or visit.organization_id != org_id
            or not scoping.owns_record(db, user, visit, "user_id")
        ):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid visit_id")

    # Determine customer_id and lead_id — explicit payload value first, else
    # whatever the Visit (if any) resolves to.
    customer_id = payload.customer_id
    if not customer_id and visit is not None:
        customer_id = visit.customer_id

    lead_id = payload.lead_id
    if not lead_id and visit is not None:
        lead_id = visit.lead_id

    if not customer_id and not lead_id:
        if visit is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Visit must be linked to a customer or lead",
            )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="customer_id or lead_id is required")

    if customer_id:
        cust = db.get(Customer, customer_id)
        # Org match AND — for an "own"-scope role (Sales Officer) — that this
        # Customer is actually assigned to them (Customer.assigned_sales_officer_id),
        # the same scoping.owns_record check customers.py::_owned_customer
        # uses for direct Customer access.
        if (
            cust is None
            or cust.organization_id != org_id
            or not scoping.owns_record(db, user, cust, "assigned_sales_officer_id")
        ):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid customer_id")
        # If both visit and customer are specified, check they match — but a
        # lead-only visit (visit.customer_id is None) has nothing to conflict with.
        if visit is not None and visit.customer_id and visit.customer_id != customer_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="visit_id belongs to a different customer than customer_id",
            )

    lead = None
    if lead_id:
        # Reuses lead_service.validate_lead_reference rather than a raw
        # db.get: it enforces both the organization match and — for an
        # "own"-scope role (Sales Officer) — that this Lead is actually one
        # they're allowed to reference, while keeping the existing 400
        # "Invalid lead_id" contract every other referenced-foreign-key
        # check in this codebase uses.
        lead = lead_service.validate_lead_reference(db, org_id, user, lead_id)
        if visit is not None and visit.lead_id and visit.lead_id != lead_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="visit_id belongs to a different lead than lead_id",
            )

    # Validate assigned_to_id. When this follow-up resolves to a Lead and no
    # assignee was explicitly given, default to that Lead's own salesperson
    # (not the creator) — otherwise an Admin adding a follow-up for a Lead
    # assigned to a Sales Officer would default it to themselves, and the
    # assigned officer (whose visibility is scoped to assigned_to_id) would
    # never see it. The pre-existing default (the creating user) is
    # preserved for every other case, unchanged.
    assigned_to_id = payload.assigned_to_id
    if not assigned_to_id:
        assigned_to_id = lead.assigned_salesperson_id if lead is not None and lead.assigned_salesperson_id else user.id
    assignee = db.get(User, assigned_to_id)
    if assignee is None or assignee.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid assigned_to_id")

    valid_statuses = {"pending", "completed", "cancelled"}
    task_status = payload.status or "pending"
    if task_status not in valid_statuses:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status '{task_status}'. Valid statuses: {sorted(valid_statuses)}",
        )

    follow_up = FollowUp(
        organization_id=org_id,
        customer_id=customer_id,
        lead_id=lead_id,
        visit_id=visit_id,
        assigned_to_id=assigned_to_id,
        title=payload.title,
        description=payload.description,
        due_date=payload.due_date,
        priority=payload.priority or "medium",
        status=task_status,
        completed_at=_now() if task_status == "completed" else None,
    )
    db.add(follow_up)
    db.commit()
    db.refresh(follow_up)
    return follow_up


def get_follow_up(db: Session, org_id: str, follow_up_id: str, user: User | None = None) -> FollowUp:
    follow_up = db.get(FollowUp, follow_up_id)
    if follow_up is None or follow_up.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Follow-up not found")
    if user is not None and not scoping.owns_record(db, user, follow_up, "assigned_to_id"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Follow-up not found")
    return follow_up


def list_follow_ups(
    db: Session,
    org_id: str,
    user: User,
    customer_id: str | None = None,
    lead_id: str | None = None,
    visit_id: str | None = None,
    assigned_to_id: str | None = None,
    status_filter: str | None = None,
    priority: str | None = None,
    due_before: datetime | None = None,
    due_after: datetime | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[FollowUp]:
    query = db.query(FollowUp).filter(FollowUp.organization_id == org_id)

    if customer_id:
        query = query.filter(FollowUp.customer_id == customer_id)
    if lead_id:
        query = query.filter(FollowUp.lead_id == lead_id)
    if visit_id:
        query = query.filter(FollowUp.visit_id == visit_id)
    if assigned_to_id:
        query = query.filter(FollowUp.assigned_to_id == assigned_to_id)
    if status_filter:
        query = query.filter(FollowUp.status == status_filter)
    if priority:
        query = query.filter(FollowUp.priority == priority)
    if due_before:
        query = query.filter(FollowUp.due_date <= due_before)
    if due_after:
        query = query.filter(FollowUp.due_date >= due_after)

    query = scoping.owned_by(query, db, user, FollowUp.assigned_to_id)
    return query.order_by(asc(FollowUp.due_date), desc(FollowUp.created_at)).offset(offset).limit(limit).all()


def update_follow_up(
    db: Session, org_id: str, follow_up_id: str, user: User, payload: FollowUpUpdate
) -> FollowUp:
    follow_up = get_follow_up(db, org_id, follow_up_id, user)
    valid_statuses = {"pending", "completed", "cancelled"}

    data = payload.model_dump(exclude_unset=True)

    if "customer_id" in data and data["customer_id"]:
        cust = db.get(Customer, data["customer_id"])
        if (
            cust is None
            or cust.organization_id != org_id
            or not scoping.owns_record(db, user, cust, "assigned_sales_officer_id")
        ):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid customer_id")

    if "lead_id" in data and data["lead_id"]:
        lead_service.validate_lead_reference(db, org_id, user, data["lead_id"])

    if "visit_id" in data and data["visit_id"]:
        visit = db.get(Visit, data["visit_id"])
        if (
            visit is None
            or visit.organization_id != org_id
            or not scoping.owns_record(db, user, visit, "user_id")
        ):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid visit_id")

    if "assigned_to_id" in data and data["assigned_to_id"]:
        assignee = db.get(User, data["assigned_to_id"])
        if assignee is None or assignee.organization_id != org_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid assigned_to_id")

    if "status" in data and data["status"]:
        new_status = data["status"]
        if new_status not in valid_statuses:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid status '{new_status}'. Valid statuses: {sorted(valid_statuses)}",
            )
        if new_status == "completed" and follow_up.status != "completed":
            follow_up.completed_at = _now()
        elif new_status != "completed":
            follow_up.completed_at = None
        follow_up.status = new_status

    for k, v in data.items():
        if k != "status":
            setattr(follow_up, k, v)

    follow_up.updated_at = _now()
    db.commit()
    db.refresh(follow_up)
    return follow_up


def complete_follow_up(
    db: Session,
    org_id: str,
    follow_up_id: str,
    user: User,
    payload: FollowUpComplete | None = None,
) -> FollowUp:
    follow_up = get_follow_up(db, org_id, follow_up_id, user)
    follow_up.status = "completed"
    if follow_up.completed_at is None:
        follow_up.completed_at = _now()
    else:
        follow_up.completed_at = _now()

    if payload is not None:
        data = payload.model_dump(exclude_unset=True)
        if "outcome" in data:
            follow_up.outcome = data["outcome"]
        if "outcome_notes" in data:
            follow_up.outcome_notes = data["outcome_notes"]

    follow_up.updated_at = _now()
    db.commit()
    db.refresh(follow_up)
    return follow_up


def delete_follow_up(db: Session, org_id: str, follow_up_id: str, user: User) -> None:
    follow_up = get_follow_up(db, org_id, follow_up_id, user)
    db.delete(follow_up)
    db.commit()
