"""Delivery Day-End / Collection Reconciliation — see app/services/day_end_service.py."""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core import scoping
from app.core.database import get_db
from app.core.deps import require_entitlement, require_permission, require_unlocked_org
from app.core.pagination import paginate
from app.models import User
from app.models.delivery_day_end import DAY_END_STATUSES, DeliveryDayEnd
from app.schemas.delivery_day_end import DayEndApprove, DayEndOut, DayEndReject, DayEndSubmit
from app.schemas.pagination import PaginatedResponse
from app.services import day_end_service

router = APIRouter(
    prefix="/delivery-day-ends",
    tags=["delivery-day-end"],
    dependencies=[Depends(require_entitlement("sales.deliveries"))],
)

_view = require_permission("deliveries", "view")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account")
    return user.organization_id


def _out(db: Session, day_end: DeliveryDayEnd) -> DayEndOut:
    data = day_end_service.view(db, day_end.organization_id, day_end.delivery_partner_id, day_end.business_date, day_end)
    data["history"] = day_end_service.history(db, day_end)
    return DayEndOut(**data)


def _owned(db: Session, day_end_id: str, user: User) -> DeliveryDayEnd:
    day_end = db.get(DeliveryDayEnd, day_end_id)
    if day_end is None or day_end.organization_id != _org_id(user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Day-End not found")
    if not scoping.owns_record(db, user, day_end, "delivery_partner_id", team_attributes=()):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Day-End not found")
    return day_end


@router.get("/current", response_model=DayEndOut)
def current_day_end(
    business_date: date | None = Query(default=None, description="Defaults to today (organization timezone)"),
    delivery_partner_id: str | None = Query(
        default=None, description="Admins / all-scope reviewers only; a delivery partner always sees their own"),
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> DayEndOut:
    """The partner's day: delivery counts and expected collections, live while open,
    as submitted afterwards. Read-only — nothing is created until submit."""
    org_id = _org_id(user)
    partner_id = user.id
    if delivery_partner_id and delivery_partner_id != user.id:
        partner = db.get(User, delivery_partner_id)
        if partner is None or partner.organization_id != org_id or scoping.scope_to_own(db, user):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delivery partner not found")
        partner_id = partner.id
    business_date = business_date or day_end_service.today(db, org_id)
    day_end = day_end_service.find(db, org_id, partner_id, business_date)
    data = day_end_service.view(db, org_id, partner_id, business_date, day_end)
    data["history"] = day_end_service.history(db, day_end) if day_end else []
    return DayEndOut(**data)


@router.post("/submit", response_model=DayEndOut)
def submit_day_end(
    payload: DayEndSubmit,
    user: User = Depends(_view),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> DayEndOut:
    """Delivery partner submits their own day: open/rejected -> submitted. 409 if the
    day is already submitted or approved."""
    org_id = _org_id(user)
    day_end = day_end_service.submit(
        db, org_id, user, payload.business_date, payload.handed_over.model_dump(), payload.notes,
    )
    db.commit()
    db.refresh(day_end)
    return _out(db, day_end)


@router.get("", response_model=PaginatedResponse[DayEndOut])
def list_day_ends(
    status_filter: str | None = Query(default=None, alias="status", description="submitted | approved | rejected"),
    delivery_partner_id: str | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> PaginatedResponse[DayEndOut]:
    """Submitted / reviewed Day-Ends, newest first. A delivery partner sees only their own."""
    org_id = _org_id(user)
    q = db.query(DeliveryDayEnd).filter(DeliveryDayEnd.organization_id == org_id)
    if status_filter:
        if status_filter not in DAY_END_STATUSES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"status must be one of {list(DAY_END_STATUSES)}")
        q = q.filter(DeliveryDayEnd.status == status_filter)
    if delivery_partner_id:
        q = q.filter(DeliveryDayEnd.delivery_partner_id == delivery_partner_id)
    if date_from:
        q = q.filter(DeliveryDayEnd.business_date >= date_from)
    if date_to:
        q = q.filter(DeliveryDayEnd.business_date <= date_to)
    q = scoping.owned_by(q, db, user, DeliveryDayEnd.delivery_partner_id, team_columns=())
    q = q.order_by(DeliveryDayEnd.business_date.desc(), DeliveryDayEnd.submitted_at.desc(), DeliveryDayEnd.id.desc())
    items, total, page, page_size, total_pages = paginate(q, page=page, page_size=page_size)
    return PaginatedResponse(
        items=[_out(db, d) for d in items], total=total, page=page, page_size=page_size, total_pages=total_pages,
    )


@router.get("/{day_end_id}", response_model=DayEndOut)
def get_day_end(day_end_id: str, user: User = Depends(_view), db: Session = Depends(get_db)) -> DayEndOut:
    return _out(db, _owned(db, day_end_id, user))


@router.post("/{day_end_id}/approve", response_model=DayEndOut)
def approve_day_end(
    day_end_id: str,
    payload: DayEndApprove | None = None,
    user: User = Depends(_view),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> DayEndOut:
    """Admin only; never the partner themselves. submitted -> approved (final).
    409 if already approved or if the day's collections changed after submission."""
    day_end = _owned(db, day_end_id, user)
    day_end_service.approve(db, user, day_end, payload.note if payload else None)
    db.commit()
    db.refresh(day_end)
    return _out(db, day_end)


@router.post("/{day_end_id}/reject", response_model=DayEndOut)
def reject_day_end(
    day_end_id: str,
    payload: DayEndReject,
    user: User = Depends(_view),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> DayEndOut:
    """Admin only; reason required. submitted -> rejected; the partner may correct and resubmit."""
    day_end = _owned(db, day_end_id, user)
    day_end_service.reject(db, user, day_end, payload.reason)
    db.commit()
    db.refresh(day_end)
    return _out(db, day_end)
