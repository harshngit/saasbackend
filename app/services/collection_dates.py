"""The two dates of a delivery collection.

- payment_date: the business day the payment belongs to (what the user picks; date only).
  It decides the Day-End, the date filters and the receipt date.
- collected_at: when the collection was actually recorded (server UTC now). Lists sort by it.

payment_date is stored in delivery_collections.payment_date, a column added by
alembic/pending/c3d4e5f6a7b8_collection_payment_date.py. Until that migration is released the
column does not exist, so nothing reads or writes it; the business day is then the firm-local
date of collected_at, and a back-dated collection keeps that day by being stamped on it at the
current time of day (the only way to keep the day without the column; never midnight).
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.core import business_time
from app.models import DeliveryCollection

_READY = False


def payment_date_ready(db: Session) -> bool:
    """True once delivery_collections.payment_date exists. Only a positive answer is cached."""
    global _READY
    if not _READY:
        from sqlalchemy import inspect
        try:
            _READY = any(c["name"] == "payment_date"
                         for c in inspect(db.connection()).get_columns("delivery_collections"))
        except Exception:  # noqa: BLE001
            return False
    return _READY


def _parse(raw, tz) -> date | None:  # noqa: ANN001
    """payment_date from a request: a date, a datetime (its local day) or an ISO string."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return business_time.local_date(raw, tz)
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    try:
        if "T" in text or " " in text:
            return business_time.local_date(datetime.fromisoformat(text.replace("Z", "+00:00")), tz)
        return date.fromisoformat(text)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="payment_date must be an ISO 8601 date or date-time, e.g. 2026-11-09 or "
                   "2026-11-09T10:30:00+05:30",
        ) from None


def stamp(db: Session, org_id: str, raw_payment_date) -> tuple[datetime, date]:  # noqa: ANN001
    """(collected_at, payment_date) for a new collection. collected_at is now; payment_date is
    the day sent, or today. Before the column exists, a past/future day is kept by stamping
    collected_at on that day at the current time of day."""
    tz = business_time.org_timezone(db, org_id)
    now = datetime.now(timezone.utc)
    now_local = now.astimezone(tz)
    day = _parse(raw_payment_date, tz) or now_local.date()
    if day != now_local.date() and not payment_date_ready(db):
        return datetime.combine(day, now_local.timetz()).astimezone(timezone.utc), day
    return now, day


def apply(db: Session, coll: DeliveryCollection, day: date) -> None:
    if payment_date_ready(db):
        coll.payment_date_value = day


def day_of(db: Session, coll: DeliveryCollection) -> date:
    """The business day of a stored collection."""
    if payment_date_ready(db) and coll.payment_date_value is not None:
        return coll.payment_date_value
    tz = business_time.org_timezone(db, coll.organization_id)
    return business_time.local_date(coll.collected_at or datetime.now(timezone.utc), tz)


def moment_of(db: Session, coll: DeliveryCollection) -> datetime:
    """A timestamp on the business day: collected_at itself when it falls on that day,
    otherwise that day at collected_at's time of day (used for the receipt's received_on)."""
    collected = coll.collected_at or datetime.now(timezone.utc)
    if collected.tzinfo is None:
        collected = collected.replace(tzinfo=timezone.utc)
    tz = business_time.org_timezone(db, coll.organization_id)
    day = day_of(db, coll)
    local = collected.astimezone(tz)
    if local.date() == day:
        return collected
    return datetime.combine(day, local.timetz()).astimezone(timezone.utc)


def day_moment(db: Session, org_id: str, day: date) -> datetime:
    """A UTC instant inside `day` (for checks that take a timestamp, e.g. the Day-End lock)."""
    return business_time.day_start(day, business_time.org_timezone(db, org_id))


def day_range_filter(db: Session, org_id: str, date_from: date | None, date_to: date | None):  # noqa: ANN201
    """SQL condition: the collection's business day is within [date_from, date_to]. Rows
    without payment_date (none after the backfill) fall back to collected_at."""
    tz = business_time.org_timezone(db, org_id)
    by_time = []
    if date_from:
        by_time.append(DeliveryCollection.collected_at >= business_time.day_start(date_from, tz))
    if date_to:
        by_time.append(DeliveryCollection.collected_at <= business_time.day_end(date_to, tz))
    if not payment_date_ready(db):
        return and_(*by_time)
    col = DeliveryCollection.payment_date_value
    by_day = [col >= date_from] if date_from else []
    if date_to:
        by_day.append(col <= date_to)
    return or_(and_(col.isnot(None), *by_day), and_(col.is_(None), *by_time))


def payment_dates(db: Session, rows: list) -> dict[str, date]:
    """id -> business day for a page of collections, in at most one query."""
    if not rows:
        return {}
    stored: dict[str, date] = {}
    if payment_date_ready(db):
        stored = dict(
            db.query(DeliveryCollection.id, DeliveryCollection.payment_date_value)
            .filter(DeliveryCollection.id.in_([r.id for r in rows]),
                    DeliveryCollection.payment_date_value.isnot(None)).all()
        )
    tz = business_time.org_timezone(db, rows[0].organization_id)
    return {r.id: stored.get(r.id) or business_time.local_date(r.collected_at, tz)
            for r in rows if r.collected_at is not None or r.id in stored}
