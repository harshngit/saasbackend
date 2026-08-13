"""Per-firm auto-numbers: CUST-2026-1001, QT-2026-1003, INV-2026-1007 …

The sheet marks these "Auto Number", so they are generated here rather than
accepted from the client.

Deliberately derived from the highest number already issued, not from a row
count. Counting repeats a number as soon as anything is deleted: three
quotations give 1003, delete one and the count drops to two, so the next one is
1003 again. Taking max+1 never reuses a number, which is what these are for.
"""

import logging
import re
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

logger = logging.getLogger("crm.numbering")

FIRST_NUMBER = 1001


def _year() -> int:
    return datetime.now(timezone.utc).year


def _highest_issued(db: Session, org_id: str, column, stem: str) -> int:
    """Highest number already present in the data — used only to seed the counter
    the first time, so existing records are not renumbered or collided with."""
    model = column.parent.class_
    rows = (
        db.query(column)
        .filter(model.organization_id == org_id, column.like(f"{stem}%"))
        .all()
    )
    highest = FIRST_NUMBER - 1
    for (value,) in rows:
        match = re.fullmatch(rf"{re.escape(stem)}(\d+)", value or "")
        if match:
            highest = max(highest, int(match.group(1)))
    return highest


def prefix_for(db: Session, org_id: str, series: str) -> str:
    """The firm's prefix for a series — its Company Settings override, else the
    series' own name (QT, INV, CUST …)."""
    from app.models.organization import Organization

    org = db.get(Organization, org_id)
    overrides = (org.number_prefixes or {}) if org is not None else {}
    return (overrides.get(series) or series).strip() or series


def next_number(db: Session, org_id: str, column, series: str, year: int | None = None) -> str:
    """Next `<prefix>-<year>-<n>` for this firm, from a stored counter that only
    moves forward — deleting a record never frees its number for reuse.

    The counter is keyed on the series, not the rendered prefix, so renaming the
    prefix continues the same run instead of restarting it."""
    from app.models.number_sequence import NumberSequence

    year = year or _year()
    prefix = prefix_for(db, org_id, series)
    stem = f"{prefix}-{year}-"

    sequence = (
        db.query(NumberSequence)
        .filter(
            NumberSequence.organization_id == org_id,
            NumberSequence.series == series,
            NumberSequence.year == year,
        )
        .with_for_update(nowait=False)
        .first()
    )
    if sequence is None:
        sequence = NumberSequence(
            organization_id=org_id,
            series=series,
            year=year,
            last_number=_highest_issued(db, org_id, column, stem),
        )
        db.add(sequence)
        db.flush()

    sequence.last_number += 1
    db.flush()
    return f"{stem}{sequence.last_number}"


# Every auto-numbered column, with the prefix its series uses.
SERIES: list[tuple[str, str, str]] = [
    ("Customer", "customer_id", "CUST"),
    ("Product", "product_id", "PROD"),
    ("Lead", "lead_id", "LEAD"),
    ("Quotation", "quotation_number", "QT"),
    ("CustomerPayment", "receipt_number", "RCPT"),
    ("SalesReturn", "return_number", "RET"),
    ("Delivery", "delivery_note_number", "DN"),
]


def backfill_missing_numbers() -> None:
    """Give a number to every row that predates auto-numbering.

    Idempotent: only touches rows where the column is NULL or blank, and numbers
    them by creation order within each firm and year, so the series stays sensible.
    """
    from app.core.database import SessionLocal
    from app import models

    db = SessionLocal()
    try:
        for model_name, column_name, prefix in SERIES:
            model = getattr(models, model_name, None)
            if model is None:
                continue
            column = getattr(model, column_name)
            rows = (
                db.query(model)
                .filter((column.is_(None)) | (column == ""))
                .order_by(model.created_at)
                .all()
            )
            if not rows:
                continue
            for row in rows:
                created = getattr(row, "created_at", None)
                year = created.year if created else _year()
                row.__setattr__(column_name, next_number(db, row.organization_id, column, prefix, year))
                db.flush()  # so the next call sees the number we just issued
            db.commit()
            logger.info("Backfilled %d %s numbers", len(rows), model_name)
    except Exception:  # noqa: BLE001
        db.rollback()
        raise
    finally:
        db.close()


def ensure_unique(db: Session, org_id: str, column, value: str, exclude_id: str | None = None) -> bool:
    """True when `value` is free for this firm (client-supplied numbers still need checking)."""
    model = column.parent.class_
    query = db.query(model).filter(model.organization_id == org_id, column == value)
    if exclude_id is not None:
        query = query.filter(model.id != exclude_id)
    return db.query(~query.exists()).scalar()


def count_for(db: Session, org_id: str, model) -> int:
    """Row count for a firm — used only where a caller genuinely wants a count."""
    return db.query(func.count(model.id)).filter(model.organization_id == org_id).scalar() or 0
