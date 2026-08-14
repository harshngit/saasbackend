"""Batches, expiry and serials, kept in step with warehouse stock.

Every physical stock movement goes through `stock_service.adjust_on_hand`, and for a
product whose tracking flags are on, that same call comes through here. So the batch
quantities always add up to the warehouse count and a serial is never sold twice, no
matter which part of the app moved the goods.

Goods in can name the batch and the serials. Goods out can too — and when they do not,
the oldest stock goes first: earliest expiry (FEFO) for batches, oldest received for
serials. That is what makes the flags operational without forcing the app to choose a
lot on every sale.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import Product, ProductSerial, StockBatch


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TrackingError(ValueError):
    """Something the caller can fix — surfaced as a 400 with this message."""


def tracks_batches(product: Product | None) -> bool:
    """Batch tracking, or expiry tracking — either needs stock kept in lots."""
    if product is None:
        return False
    return bool(product.batch_tracking or product.expiry_tracking)


def tracks_serials(product: Product | None) -> bool:
    return bool(product is not None and product.serial_number_tracking)


def _as_datetime(value) -> datetime | None:
    """Accept a date, a datetime or an ISO string from the request body."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    # A plain date
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# --------------------------------- batches ---------------------------------


def batch_rows(
    db: Session, warehouse_id: str, product_id: str, variant_id: str | None
) -> list[StockBatch]:
    """Every lot of this item in this warehouse, earliest expiry first (FEFO).

    Lots with no expiry date sort last: dated stock has to move before undated stock.
    """
    rows = (
        db.query(StockBatch)
        .filter(
            StockBatch.warehouse_id == warehouse_id,
            StockBatch.product_id == product_id,
            StockBatch.variant_id == variant_id,
        )
        .all()
    )
    return sorted(
        rows,
        key=lambda row: (
            row.expiry_date is None,
            _aware(row.expiry_date) or _now(),
            row.created_at or _now(),
        ),
    )


def _batch_row(
    db: Session,
    org_id: str,
    warehouse_id: str,
    product_id: str,
    variant_id: str | None,
    batch_number: str | None,
    create: bool = True,
) -> StockBatch | None:
    row = (
        db.query(StockBatch)
        .filter(
            StockBatch.warehouse_id == warehouse_id,
            StockBatch.product_id == product_id,
            StockBatch.variant_id == variant_id,
            StockBatch.batch_number == batch_number,
        )
        .first()
    )
    if row is None and create:
        row = StockBatch(
            organization_id=org_id,
            warehouse_id=warehouse_id,
            product_id=product_id,
            variant_id=variant_id,
            batch_number=batch_number,
            quantity=0,
            received_quantity=0,
        )
        db.add(row)
        db.flush()
    return row


def available_in_batches(
    db: Session, warehouse_id: str, product_id: str, variant_id: str | None
) -> float:
    return round(sum(row.quantity or 0 for row in batch_rows(db, warehouse_id, product_id, variant_id)), 3)


# --------------------------------- serials ---------------------------------


def serial_rows(
    db: Session, org_id: str, product_id: str, variant_id: str | None, status: str = "in_stock"
) -> list[ProductSerial]:
    query = (
        db.query(ProductSerial)
        .filter(
            ProductSerial.organization_id == org_id,
            ProductSerial.product_id == product_id,
            ProductSerial.variant_id == variant_id,
        )
    )
    if status:
        query = query.filter(ProductSerial.status == status)
    # Oldest received first. Units received together tie on the timestamp, so the
    # serial number breaks the tie — otherwise the pick is a random UUID's order.
    return query.order_by(ProductSerial.created_at, ProductSerial.serial_number).all()


# ------------------------------ the movement -------------------------------


def apply(
    db: Session,
    org_id: str,
    warehouse_id: str,
    product_id: str,
    variant_id: str | None,
    delta: float,
    batch: dict | None = None,
    serial_numbers: list[str] | None = None,
) -> dict:
    """Mirror one stock movement into batches and serials, and say what moved.

    Returns `{batch_number, expiry_date, serial_numbers}` — the snapshot an invoice,
    delivery or order line keeps, so a document always says which lot and which units
    it was for. An untracked product returns empty values and nothing is written.

    `batch` is `{batch_number, manufacturing_date, expiry_date, mrp}`; only
    `batch_number` matters on the way out.
    """
    product = db.get(Product, product_id)
    snapshot: dict = {"batch_number": None, "expiry_date": None, "serial_numbers": []}
    if product is None or not delta:
        return snapshot

    if tracks_batches(product):
        snapshot.update(
            _apply_batches(db, org_id, warehouse_id, product_id, variant_id, delta, batch)
        )
    elif batch and batch.get("batch_number"):
        # The firm sent a batch for a product it does not track in lots. Keep it on the
        # document rather than throwing it away — it is still what was written on the box.
        snapshot["batch_number"] = batch.get("batch_number")
        snapshot["expiry_date"] = _as_datetime(batch.get("expiry_date"))

    if tracks_serials(product):
        snapshot["serial_numbers"] = _apply_serials(
            db, org_id, warehouse_id, product_id, variant_id, delta, serial_numbers,
            batch_number=snapshot["batch_number"],
        )
    elif serial_numbers:
        snapshot["serial_numbers"] = list(serial_numbers)

    return snapshot


def _apply_batches(
    db: Session,
    org_id: str,
    warehouse_id: str,
    product_id: str,
    variant_id: str | None,
    delta: float,
    batch: dict | None,
) -> dict:
    number = (batch or {}).get("batch_number") or None
    if delta > 0:
        row = _batch_row(db, org_id, warehouse_id, product_id, variant_id, number)
        row.quantity = round((row.quantity or 0) + delta, 3)
        row.received_quantity = round((row.received_quantity or 0) + delta, 3)
        if batch:
            row.manufacturing_date = _as_datetime(batch.get("manufacturing_date")) or row.manufacturing_date
            row.expiry_date = _as_datetime(batch.get("expiry_date")) or row.expiry_date
            if batch.get("mrp") is not None:
                row.mrp = batch["mrp"]
        db.flush()
        return {"batch_number": row.batch_number, "expiry_date": row.expiry_date}

    # Goods leaving: the named lot, else oldest-expiry first.
    wanted = -delta
    if number is not None:
        row = _batch_row(db, org_id, warehouse_id, product_id, variant_id, number, create=False)
        if row is None:
            raise TrackingError(f"Batch {number} is not in stock in this warehouse")
        if (row.quantity or 0) + 0.001 < wanted:
            raise TrackingError(
                f"Batch {number} has only {row.quantity:g} left, {wanted:g} needed"
            )
        row.quantity = round((row.quantity or 0) - wanted, 3)
        db.flush()
        return {"batch_number": row.batch_number, "expiry_date": row.expiry_date}

    taken: list[StockBatch] = []
    left = wanted
    for row in batch_rows(db, warehouse_id, product_id, variant_id):
        if left <= 0:
            break
        available = row.quantity or 0
        if available <= 0:
            continue
        used = min(available, left)
        row.quantity = round(available - used, 3)
        left = round(left - used, 3)
        taken.append(row)
    if left > 0.001:
        # Batch rows can lag behind an older warehouse count. Rather than block a sale
        # the warehouse says it can make, the shortfall comes out of the untracked lot.
        remainder = _batch_row(db, org_id, warehouse_id, product_id, variant_id, None)
        remainder.quantity = round((remainder.quantity or 0) - left, 3)
        taken.append(remainder)
    db.flush()
    first = taken[0] if taken else None
    return {
        "batch_number": first.batch_number if first is not None else None,
        "expiry_date": first.expiry_date if first is not None else None,
    }


def _apply_serials(
    db: Session,
    org_id: str,
    warehouse_id: str,
    product_id: str,
    variant_id: str | None,
    delta: float,
    serial_numbers: list[str] | None,
    batch_number: str | None = None,
) -> list[str]:
    numbers = [str(value).strip() for value in (serial_numbers or []) if str(value).strip()]
    count = int(abs(delta))

    if delta > 0:
        if numbers and len(numbers) != count:
            raise TrackingError(
                f"{len(numbers)} serial number(s) for {count} unit(s) — they have to match"
            )
        batch = (
            _batch_row(db, org_id, warehouse_id, product_id, variant_id, batch_number, create=False)
            if batch_number else None
        )
        created = []
        for value in numbers:
            existing = (
                db.query(ProductSerial)
                .filter(
                    ProductSerial.organization_id == org_id,
                    ProductSerial.serial_number == value,
                )
                .first()
            )
            if existing is not None:
                if existing.status == "in_stock":
                    raise TrackingError(f"Serial {value} is already in stock")
                # A unit coming back after a return goes back on the shelf.
                existing.status = "in_stock"
                existing.warehouse_id = warehouse_id
                existing.invoice_item_id = None
                existing.delivery_item_id = None
                existing.sold_at = None
                created.append(value)
                continue
            db.add(ProductSerial(
                organization_id=org_id,
                product_id=product_id,
                variant_id=variant_id,
                warehouse_id=warehouse_id,
                batch_id=batch.id if batch is not None else None,
                serial_number=value,
                status="in_stock",
            ))
            created.append(value)
        db.flush()
        return created

    # Goods leaving: the named units, else the oldest in stock.
    if numbers:
        if len(numbers) != count:
            raise TrackingError(
                f"{len(numbers)} serial number(s) for {count} unit(s) — they have to match"
            )
        rows = []
        for value in numbers:
            row = (
                db.query(ProductSerial)
                .filter(
                    ProductSerial.organization_id == org_id,
                    ProductSerial.serial_number == value,
                )
                .first()
            )
            if row is None:
                raise TrackingError(f"Serial {value} is not on record")
            if row.product_id != product_id:
                raise TrackingError(f"Serial {value} belongs to a different product")
            if row.status != "in_stock":
                raise TrackingError(f"Serial {value} is not in stock ({row.status})")
            rows.append(row)
    else:
        rows = serial_rows(db, org_id, product_id, variant_id)[:count]
        # Serial rows can lag behind an older warehouse count; sell what is on record.

    for row in rows:
        row.status = "sold"
        row.sold_at = _now()
    db.flush()
    return [row.serial_number for row in rows]


def release_serials(db: Session, org_id: str, serial_numbers: list[str], warehouse_id: str) -> None:
    """Put named units back in stock — what an approved return does."""
    for value in serial_numbers or []:
        row = (
            db.query(ProductSerial)
            .filter(
                ProductSerial.organization_id == org_id,
                ProductSerial.serial_number == str(value).strip(),
            )
            .first()
        )
        if row is not None:
            row.status = "in_stock"
            row.warehouse_id = warehouse_id
            row.invoice_item_id = None
            row.delivery_item_id = None
            row.sold_at = None
    db.flush()


def mark_sold_on(db: Session, org_id: str, serial_numbers: list[str], **links) -> None:
    """Record which document a unit went out on (invoice line, delivery line)."""
    for value in serial_numbers or []:
        row = (
            db.query(ProductSerial)
            .filter(
                ProductSerial.organization_id == org_id,
                ProductSerial.serial_number == str(value).strip(),
            )
            .first()
        )
        if row is not None:
            for field, target in links.items():
                setattr(row, field, target)
    db.flush()


# ------------------------------ expiry report ------------------------------


def expiring(db: Session, org_id: str, within_days: int = 30, include_expired: bool = True) -> list[StockBatch]:
    """Lots that are expired or about to be, soonest first."""
    cutoff = _now() + timedelta(days=within_days)
    query = (
        db.query(StockBatch)
        .filter(
            StockBatch.organization_id == org_id,
            StockBatch.quantity > 0,
            StockBatch.expiry_date.isnot(None),
            StockBatch.expiry_date <= cutoff,
        )
    )
    if not include_expired:
        query = query.filter(StockBatch.expiry_date >= _now())
    return query.order_by(StockBatch.expiry_date).all()
