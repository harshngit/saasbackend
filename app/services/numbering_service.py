"""Per-firm auto-numbering system.

Canonical formats:
- Transactional: PREFIX-COMPANY_NUMBER-YEAR-SEQUENCE (e.g. CS-10001-2026-0001)
- Master: PREFIX-SEQUENCE (e.g. SUP-0001, PRD-0001)
- Company: CMP-SEQUENCE (e.g. CMP-10001)

Deliberately derived from atomic counters stored in NumberSequence, initialized from the highest
matching stem already present in the data, not from row counts. Taking atomic increments
never reuses a number on deletion or rollback, guaranteeing monotonicity.
"""

import logging
import re
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

logger = logging.getLogger("crm.numbering")

CANONICAL_PREFIXES: dict[str, str] = {
    "customer": "CS",
    "cust": "CS",
    "cs": "CS",
    "invoice": "IN",
    "inv": "IN",
    "in": "IN",
    "sales_order": "SO",
    "so": "SO",
    "delivery": "DO",
    "dlv": "DO",
    "dn": "DO",
    "do": "DO",
    "purchase_order": "PO",
    "purchase": "PO",
    "pur": "PO",
    "purid": "PO",
    "po": "PO",
    "employee": "EMP",
    "emp": "EMP",
    "lead": "L",
    "l": "L",
    "quotation": "QT",
    "qt": "QT",
    "supplier": "SUP",
    "sup": "SUP",
    "product": "PRD",
    "prod": "PRD",
    "prd": "PRD",
    "company": "CMP",
    "cmp": "CMP",
}

MASTER_SERIES = {"SUP", "PRD"}
TRANSACTIONAL_SERIES = {"CS", "IN", "SO", "DO", "PO", "EMP", "L", "QT"}


DEFAULT_BUSINESS_TIMEZONE = "Asia/Kolkata"


def get_business_year(
    db: Session | None = None,
    org_id: str | None = None,
    dt: datetime | None = None,
) -> int:
    """Current or specified timestamp's calendar year in the business timezone.

    Defaults to Asia/Kolkata, or the organization's configured timezone if available and valid.
    """
    from zoneinfo import ZoneInfo

    tz_name = DEFAULT_BUSINESS_TIMEZONE
    if db is not None and org_id is not None:
        try:
            from app.models.organization import Organization
            org = db.get(Organization, org_id)
            if org and org.timezone:
                ZoneInfo(org.timezone)
                tz_name = org.timezone
        except Exception:
            tz_name = DEFAULT_BUSINESS_TIMEZONE

    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo(DEFAULT_BUSINESS_TIMEZONE)

    if dt is None:
        dt = datetime.now(timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(tz).year


def _year(db: Session | None = None, org_id: str | None = None) -> int:
    """Current business calendar year."""
    return get_business_year(db, org_id)


def normalize_series(series: str) -> str:
    """Map any legacy/alias series name to its canonical uppercase key."""
    cleaned = (series or "").strip().lower()
    return CANONICAL_PREFIXES.get(cleaned, series.strip().upper())


def extract_company_number(db: Session, org_id: str) -> str:
    """Extract the numeric portion of an organization's company_code (e.g. 'CMP-10001' -> '10001').

    Fails safely if company_code is missing or malformed.
    """
    from app.models.organization import Organization
    from app.services.org_service import COMPANY_CODE_PREFIX, ensure_company_code

    org = db.get(Organization, org_id)
    if org is None:
        raise ValueError(f"Organization '{org_id}' not found")

    code = org.company_code
    if not code:
        code = ensure_company_code(db, org, auto_commit=False)

    if not isinstance(code, str) or not code.startswith(COMPANY_CODE_PREFIX):
        raise ValueError(
            f"Organization company_code '{code}' does not match expected format '{COMPANY_CODE_PREFIX}XXXXX'"
        )

    num_part = code[len(COMPANY_CODE_PREFIX):]
    if not num_part.isdigit():
        raise ValueError(
            f"Organization company_code '{code}' has invalid non-numeric segment '{num_part}'"
        )

    return num_part


def _highest_issued(db: Session, org_id: str, column, stem: str) -> int:
    """Highest sequence number already present matching the exact stem.

    Used only to seed the counter the first time so existing canonical records
    are not collided with and old legacy formats do not interfere.
    """
    if column is None:
        return 0
    try:
        model = column.parent.class_
        rows = (
            db.query(column)
            .filter(model.organization_id == org_id, column.like(f"{stem}%"))
            .all()
        )
    except Exception:
        return 0

    highest = 0
    for (value,) in rows:
        match = re.fullmatch(rf"{re.escape(stem)}(\d+)", value or "")
        if match:
            highest = max(highest, int(match.group(1)))
    return highest


def prefix_for(db: Session, org_id: str, series: str) -> str:
    """The firm's prefix for a series — its Company Settings override, else the
    canonical series name (CS, IN, SO, DO, PO, EMP, L, QT, SUP, PRD, CMP)."""
    from app.models.organization import Organization

    canonical = normalize_series(series)
    org = db.get(Organization, org_id)
    overrides = (org.number_prefixes or {}) if org is not None else {}
    override = overrides.get(canonical) or overrides.get(series) or overrides.get(series.lower())
    return (override or canonical).strip() or canonical


def _allocate_next(
    db: Session, org_id: str, column, series: str, year: int, stem: str
) -> str:
    """Atomic, concurrency-safe sequential allocation using NumberSequence.

    1. First creation is wrapped in a SAVEPOINT to handle concurrent first-creations.
    2. Sequence counter is verified to be at least the highest issued canonical number.
    3. The increment is an atomic database-side `UPDATE ... SET last_number = last_number + 1 RETURNING last_number`.
    4. Result is zero-padded with minimum 4 digits.
    """
    from app.models.number_sequence import NumberSequence

    _match = (
        NumberSequence.organization_id == org_id,
        NumberSequence.series == series,
        NumberSequence.year == year,
    )

    exists = db.query(NumberSequence).filter(*_match).first()
    if exists is None:
        try:
            with db.begin_nested():
                initial_highest = _highest_issued(db, org_id, column, stem)
                db.add(
                    NumberSequence(
                        organization_id=org_id,
                        series=series,
                        year=year,
                        last_number=initial_highest,
                    )
                )
                db.flush()
        except (IntegrityError, OperationalError):
            # Another concurrent transaction inserted the row; proceed to increment
            pass
    else:
        # Ensure sequence counter is synchronized if records with higher numbers exist
        initial_highest = _highest_issued(db, org_id, column, stem)
        if exists.last_number < initial_highest:
            exists.last_number = initial_highest
            db.flush()

    result = db.execute(
        sa_update(NumberSequence)
        .where(*_match)
        .values(last_number=NumberSequence.last_number + 1)
        .returning(NumberSequence.last_number)
    )
    row = result.first()
    db.flush()
    if row is None:
        logger.error(
            "Number sequence for org=%s series=%s year=%s was not found for increment",
            org_id,
            series,
            year,
        )
        raise RuntimeError(f"Could not allocate a number for series '{series}'")
    return f"{stem}{row[0]:04d}"


def next_transactional_number(
    db: Session, org_id: str, column, series: str, year: int | None = None
) -> str:
    """Next PREFIX-COMPANY_NUMBER-YEAR-SEQUENCE for transactional entities."""
    canonical = normalize_series(series)
    year = year or get_business_year(db, org_id)
    company_number = extract_company_number(db, org_id)
    prefix = prefix_for(db, org_id, canonical)
    stem = f"{prefix}-{company_number}-{year}-"
    return _allocate_next(db, org_id, column, canonical, year, stem)


def next_master_number(db: Session, org_id: str, column, series: str) -> str:
    """Next PREFIX-SEQUENCE (e.g. SUP-0001, PRD-0001) for master/catalog entities (year=0)."""
    canonical = normalize_series(series)
    prefix = prefix_for(db, org_id, canonical)
    stem = f"{prefix}-"
    return _allocate_next(db, org_id, column, canonical, 0, stem)


def next_number(
    db: Session,
    org_id: str,
    column,
    series: str,
    year: int | None = None,
    is_master: bool | None = None,
) -> str:
    """Allocate the next canonical business ID for any entity."""
    canonical = normalize_series(series)
    if is_master is True or (is_master is None and canonical in MASTER_SERIES):
        return next_master_number(db, org_id, column, canonical)
    return next_transactional_number(db, org_id, column, canonical, year=year)


# Every auto-numbered column, with its canonical series key.
SERIES: list[tuple[str, str, str]] = [
    ("Customer", "customer_id", "CS"),
    ("Product", "product_id", "PRD"),
    ("Lead", "lead_id", "L"),
    ("Quotation", "quotation_number", "QT"),
    ("Delivery", "delivery_note_number", "DO"),
    ("Supplier", "supplier_code", "SUP"),
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
        for model_name, column_name, series in SERIES:
            model = getattr(models, model_name, None)
            if model is None:
                continue
            column = getattr(model, column_name, None)
            if column is None:
                continue
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
                row.__setattr__(
                    column_name, next_number(db, row.organization_id, column, series, year)
                )
                db.flush()
            db.commit()
            logger.info("Backfilled %d %s numbers", len(rows), model_name)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def ensure_unique(
    db: Session, org_id: str, column, value: str, exclude_id: str | None = None
) -> bool:
    """True when `value` is free for this firm."""
    model = column.parent.class_
    query = db.query(model).filter(model.organization_id == org_id, column == value)
    if exclude_id is not None:
        query = query.filter(model.id != exclude_id)
    return db.query(~query.exists()).scalar()


def count_for(db: Session, org_id: str, model) -> int:
    """Row count for a firm — used only where a caller genuinely wants a count."""
    return db.query(func.count(model.id)).filter(model.organization_id == org_id).scalar() or 0
