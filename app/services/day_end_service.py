"""Delivery Day-End: the canonical delivery-partner settlement workflow.

    open ─► submitted ─► approved            (final)
                 │
                 └─► rejected ─► submitted   (corrected and resubmitted)

Nothing here is a second ledger. Expected collections come from the partner's own
DeliveryCollection rows for the business date (voided ones excluded) and delivery
counts from their Delivery rows; the Day-End adds only what the partner handed over,
the variance, and the Admin's decision. Business-date boundaries and payment-method
normalisation are the same helpers Cash Reconciliation uses, so both read a day the
same way.
"""

from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Delivery, DeliveryCollection, User
from app.models.delivery_day_end import DAY_END_METHODS, DeliveryDayEnd
from app.services import activity_service, role_service
from app.services.cash_reconciliation_service import get_business_day_utc_range, normalize_payment_method
from app.services.report_service import get_org_timezone

PENDING_DELIVERY_STATUSES = ("planned", "accepted", "ready", "loaded", "in_transit")
LOCKED_STATUSES = ("submitted", "approved")
_ZERO = Decimal("0.00")


def _d(value) -> Decimal:  # noqa: ANN001
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def bucket(payment_mode: str | None) -> str:
    """cash | bank | upi | cheque | other. Cash-on-delivery is cash; card and bank
    transfers (NEFT/RTGS/IMPS) settle to the bank; UPI stays its own category."""
    method = normalize_payment_method(payment_mode)
    if method in ("cash", "cod"):
        return "cash"
    if method in ("bank_transfer", "card"):
        return "bank"
    if method in ("upi", "cheque"):
        return method
    return "other"


def today(db: Session, org_id: str) -> date:
    return datetime.now(get_org_timezone(db, org_id)).date()


def business_date_of(db: Session, org_id: str, moment: datetime) -> date:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(get_org_timezone(db, org_id)).date()


def collections_for(db: Session, org_id: str, partner_id: str, business_date: date) -> list[DeliveryCollection]:
    start, end = get_business_day_utc_range(db, org_id, business_date)
    return (
        db.query(DeliveryCollection)
        .filter(
            DeliveryCollection.organization_id == org_id,
            DeliveryCollection.delivery_partner_id == partner_id,
            DeliveryCollection.collected_at >= start,
            DeliveryCollection.collected_at <= end,
            DeliveryCollection.reconciliation_status != "voided",
        )
        .order_by(DeliveryCollection.collected_at.asc(), DeliveryCollection.id.asc())
        .all()
    )


def expected_breakdown(rows: list[DeliveryCollection]) -> dict[str, Decimal]:
    totals = {m: _ZERO for m in DAY_END_METHODS}
    for row in rows:
        totals[bucket(row.payment_mode)] += _d(row.amount)
    totals["total"] = sum((totals[m] for m in DAY_END_METHODS), _ZERO)
    return totals


def delivery_summary(db: Session, org_id: str, partner_id: str, business_date: date) -> dict:
    """Derived from the partner's Delivery rows scheduled for that business date
    (scheduled_date, else delivery_date). Cancelled and partner-rejected deliveries
    are not assigned work. `returned` is a failed attempt: goods came back."""
    start, end = get_business_day_utc_range(db, org_id, business_date)
    day = func.coalesce(Delivery.scheduled_date, Delivery.delivery_date)
    rows = (
        db.query(Delivery.status, func.count(Delivery.id))
        .filter(
            Delivery.organization_id == org_id,
            Delivery.delivery_partner_id == partner_id,
            day >= start,
            day <= end,
            Delivery.status.notin_(("cancelled", "rejected")),
        )
        .group_by(Delivery.status)
        .all()
    )
    by_status = {s: n for s, n in rows}
    return {
        "assigned_count": sum(by_status.values()),
        "delivered_count": by_status.get("delivered", 0) + by_status.get("partially_delivered", 0),
        "partially_delivered_count": by_status.get("partially_delivered", 0),
        "returned_count": by_status.get("failed", 0),
        "pending_count": sum(by_status.get(s, 0) for s in PENDING_DELIVERY_STATUSES),
    }


def find(db: Session, org_id: str, partner_id: str, business_date: date) -> DeliveryDayEnd | None:
    return (
        db.query(DeliveryDayEnd)
        .filter(
            DeliveryDayEnd.organization_id == org_id,
            DeliveryDayEnd.delivery_partner_id == partner_id,
            DeliveryDayEnd.business_date == business_date,
        )
        .first()
    )


def _lock(db: Session, day_end: DeliveryDayEnd) -> None:
    """A real UPDATE (row lock on Postgres, write lock on SQLite) so concurrent
    submit / approve calls serialize and the second sees the first's status."""
    db.execute(sa_update(DeliveryDayEnd).where(DeliveryDayEnd.id == day_end.id).values(status=DeliveryDayEnd.status))
    db.refresh(day_end)


def view(db: Session, org_id: str, partner_id: str, business_date: date, day_end: DeliveryDayEnd | None) -> dict:
    """Everything the partner screen and the Admin review show for one day."""
    live_rows = collections_for(db, org_id, partner_id, business_date)
    live = expected_breakdown(live_rows)
    partner = db.get(User, partner_id)
    out = {
        "id": day_end.id if day_end else None,
        "organization_id": org_id,
        "delivery_partner_id": partner_id,
        "delivery_partner_name": partner.name if partner else None,
        "business_date": business_date,
        "status": day_end.status if day_end else "open",
        "deliveries": delivery_summary(db, org_id, partner_id, business_date),
        "notes": day_end.notes if day_end else None,
        "submission_count": day_end.submission_count if day_end else 0,
        "submitted_at": day_end.submitted_at if day_end else None,
        "reviewed_at": day_end.reviewed_at if day_end else None,
        "reviewed_by_id": day_end.reviewed_by_id if day_end else None,
        "reviewed_by_name": day_end.reviewed_by.name if day_end and day_end.reviewed_by else None,
        "review_note": day_end.review_note if day_end else None,
        "rejection_reason": day_end.rejection_reason if day_end else None,
    }
    if day_end is not None and day_end.status in ("submitted", "approved", "rejected") and day_end.submission_count:
        expected = {m: _d(getattr(day_end, f"expected_{m}")) for m in DAY_END_METHODS}
        expected["total"] = _d(day_end.expected_total)
        handed = {m: _d(getattr(day_end, f"handed_{m}")) for m in DAY_END_METHODS}
        handed["total"] = _d(day_end.handed_total)
        ids = list(day_end.collection_ids or [])
    else:
        expected, handed, ids = live, None, [r.id for r in live_rows]
    out["collections"] = {
        "breakdown": [
            {
                "method": m,
                "system_expected": float(expected[m]),
                "actual_handed_over": float(handed[m]) if handed else None,
                "variance": float(handed[m] - expected[m]) if handed else None,
            }
            for m in DAY_END_METHODS
        ],
        "expected_total": float(expected["total"]),
        "handed_over_total": float(handed["total"]) if handed else None,
        "variance_total": float(handed["total"] - expected["total"]) if handed else None,
        "collection_ids": ids,
        "collection_count": len(ids),
        # The live figure today; differs from expected_total only if collections
        # were added or voided after submission.
        "live_expected_total": float(live["total"]),
        "changed_since_submission": bool(handed) and (
            live["total"] != expected["total"] or sorted(ids) != sorted(r.id for r in live_rows)
        ),
    }
    return out


def _history(db: Session, day_end: DeliveryDayEnd, actor: User, action: str, note: str | None) -> None:
    titles = {"submitted": "Day-End submitted", "approved": "Day-End approved", "rejected": "Day-End rejected"}
    activity_service.record(
        db, day_end.organization_id, actor, "day_end",
        f"{titles[action]}: {day_end.business_date.isoformat()}", note,
        entity_type="delivery_day_end", entity_id=day_end.id, action=action,
    )


def history(db: Session, day_end: DeliveryDayEnd) -> list[dict]:
    return [
        {"action": r.action, "actor_id": r.user_id, "actor_name": r.actor_name, "at": r.created_at, "note": r.description}
        for r in activity_service.history(db, day_end.organization_id, "delivery_day_end", day_end.id)
    ]


def submit(
    db: Session, org_id: str, user: User, business_date: date | None, handed_over: dict[str, float], notes: str | None,
) -> DeliveryDayEnd:
    """The partner closes their own day. Only for themselves; never a future date;
    a submitted or approved day cannot be submitted again; a rejected one can."""
    business_date = business_date or today(db, org_id)
    if business_date > today(db, org_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A future day cannot be closed")
    unknown = set(handed_over) - set(DAY_END_METHODS)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown collection categories {sorted(unknown)}; use {list(DAY_END_METHODS)}",
        )

    day_end = find(db, org_id, user.id, business_date)
    if day_end is None:
        day_end = DeliveryDayEnd(organization_id=org_id, delivery_partner_id=user.id, business_date=business_date)
        db.add(day_end)
        try:
            with db.begin_nested():
                db.flush()
        except IntegrityError:
            day_end = find(db, org_id, user.id, business_date)
    _lock(db, day_end)
    if day_end.status in LOCKED_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This day has already been {day_end.status}",
        )

    rows = collections_for(db, org_id, user.id, business_date)
    expected = expected_breakdown(rows)
    handed = {m: _d(handed_over.get(m, 0)) for m in DAY_END_METHODS}
    if any(v < 0 for v in handed.values()):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Handed-over amounts cannot be negative")
    handed_total = sum(handed.values(), _ZERO)
    variance = handed_total - expected["total"]
    if any(handed[m] != expected[m] for m in DAY_END_METHODS) and not (notes or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Explain the difference in notes: the handed-over amounts do not match the recorded collections",
        )

    for m in DAY_END_METHODS:
        setattr(day_end, f"expected_{m}", expected[m])
        setattr(day_end, f"handed_{m}", handed[m])
    day_end.expected_total = expected["total"]
    day_end.handed_total = handed_total
    day_end.variance_total = variance
    day_end.collection_ids = [r.id for r in rows]
    day_end.notes = notes
    day_end.status = "submitted"
    day_end.submission_count = (day_end.submission_count or 0) + 1
    day_end.submitted_at = _now()
    day_end.submitted_by_id = user.id
    day_end.reviewed_at = None
    day_end.reviewed_by_id = None
    day_end.review_note = None
    day_end.rejection_reason = None
    db.flush()
    _history(db, day_end, user, "submitted", notes)
    return day_end


def _require_reviewer(user: User, day_end: DeliveryDayEnd) -> None:
    if not role_service.is_full_access(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only an Admin can review a Day-End")
    if day_end.delivery_partner_id == user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You cannot review your own Day-End")


def approve(db: Session, user: User, day_end: DeliveryDayEnd, note: str | None) -> DeliveryDayEnd:
    _require_reviewer(user, day_end)
    _lock(db, day_end)
    if day_end.status == "approved":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This Day-End is already approved")
    if day_end.status != "submitted":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only a submitted Day-End can be approved")
    live = collections_for(db, day_end.organization_id, day_end.delivery_partner_id, day_end.business_date)
    if sorted(r.id for r in live) != sorted(day_end.collection_ids or []) or expected_breakdown(live)["total"] != _d(day_end.expected_total):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "DAY_END_COLLECTIONS_CHANGED",
                "message": "Collections for this day changed after it was submitted. Reject it so the "
                           "delivery partner can resubmit against the current records.",
            },
        )
    day_end.status = "approved"
    day_end.reviewed_at = _now()
    day_end.reviewed_by_id = user.id
    day_end.review_note = note
    db.flush()
    _history(db, day_end, user, "approved", note)
    return day_end


def reject(db: Session, user: User, day_end: DeliveryDayEnd, reason: str | None) -> DeliveryDayEnd:
    _require_reviewer(user, day_end)
    if not (reason or "").strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A rejection reason is required")
    _lock(db, day_end)
    if day_end.status != "submitted":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only a submitted Day-End can be rejected")
    day_end.status = "rejected"
    day_end.reviewed_at = _now()
    day_end.reviewed_by_id = user.id
    day_end.rejection_reason = reason.strip()
    db.flush()
    _history(db, day_end, user, "rejected", day_end.rejection_reason)
    return day_end


def require_day_open(db: Session, org_id: str, partner_id: str | None, moment: datetime) -> None:
    """Collections cannot be added to, or voided out of, a day whose Day-End is
    submitted or approved: the Admin reviews (and approves) a fixed set."""
    if not partner_id:
        return
    day_end = find(db, org_id, partner_id, business_date_of(db, org_id, moment))
    if day_end is not None and day_end.status in LOCKED_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "DAY_END_CLOSED",
                "message": f"The delivery partner's Day-End for {day_end.business_date.isoformat()} is "
                           f"{day_end.status}; its collections can no longer change.",
            },
        )
