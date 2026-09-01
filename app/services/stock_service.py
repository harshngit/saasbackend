"""Warehouse stock, and the reservations held against it.

The rule the whole sales flow rests on:

    available = on_hand − outstanding reservations

`on_hand` moves only on a real goods movement — a purchase received, a vehicle
loaded, a return taken back in. Placing an order never touches it; that holds a
reservation, so two orders cannot promise the same unit and cancelling an order
releases the hold instead of inventing a stock-in that never happened.

The legacy per-product `total_inventory` / per-variant `inventory` counters are kept
in step with the sum across warehouses, so every existing product, inventory and
report endpoint keeps reporting the same figure it always did.
"""

from sqlalchemy import func
from sqlalchemy.orm import Session, lazyload

from app.models import (
    Product,
    ProductVariant,
    StockMovement,
    StockReservation,
    Warehouse,
    WarehouseStock,
)
from app.services import tracking_service

DEFAULT_WAREHOUSE_NAME = "Main Warehouse"
DEFAULT_WAREHOUSE_CODE = "WH-001"


# ------------------------------- warehouses --------------------------------


def default_warehouse(db: Session, org_id: str) -> Warehouse:
    """The firm's default warehouse, created on first use.

    Every firm needs somewhere for stock to live before anyone opens a warehouse
    screen, and a caller that never mentions a warehouse has to land somewhere
    real — so this is created lazily rather than demanded up front.
    """
    existing = (
        db.query(Warehouse)
        .filter(Warehouse.organization_id == org_id, Warehouse.is_default.is_(True))
        .first()
    )
    if existing is not None:
        return existing
    # A firm may already have warehouses without one marked default (an import, or
    # the flag cleared by hand) — promote the oldest rather than adding another.
    any_warehouse = (
        db.query(Warehouse)
        .filter(Warehouse.organization_id == org_id)
        .order_by(Warehouse.created_at)
        .first()
    )
    if any_warehouse is not None:
        any_warehouse.is_default = True
        db.flush()
        return any_warehouse

    warehouse = Warehouse(
        organization_id=org_id,
        name=DEFAULT_WAREHOUSE_NAME,
        code=DEFAULT_WAREHOUSE_CODE,
        is_default=True,
    )
    db.add(warehouse)
    db.flush()
    # Whatever the product catalog already counted as stock belongs somewhere: fold
    # it into this warehouse so `available` is right from the first order.
    _seed_from_catalog(db, org_id, warehouse)
    return warehouse


def _seed_from_catalog(db: Session, org_id: str, warehouse: Warehouse) -> None:
    """Open the default warehouse with whatever the catalog already holds."""
    for product in db.query(Product).filter(Product.organization_id == org_id):
        variants = product.variations or []
        if variants:
            for variant in variants:
                if variant.inventory:
                    _row(db, org_id, warehouse.id, product.id, variant.id).on_hand_quantity = (
                        variant.inventory
                    )
        elif product.total_inventory:
            _row(db, org_id, warehouse.id, product.id, None).on_hand_quantity = (
                product.total_inventory
            )
    db.flush()


def owned_warehouse(db: Session, warehouse_id: str | None, org_id: str) -> Warehouse | None:
    """The warehouse if it belongs to this firm, else None. No id means the default."""
    if not warehouse_id:
        return default_warehouse(db, org_id)
    warehouse = db.get(Warehouse, warehouse_id)
    if warehouse is None or warehouse.organization_id != org_id:
        return None
    return warehouse


# ------------------------------ stock figures ------------------------------


def _row(
    db: Session, org_id: str, warehouse_id: str, product_id: str, variant_id: str | None
) -> WarehouseStock:
    """The stock row for one item in one warehouse, created at zero if absent."""
    row = (
        db.query(WarehouseStock)
        .filter(
            WarehouseStock.warehouse_id == warehouse_id,
            WarehouseStock.product_id == product_id,
            WarehouseStock.variant_id == variant_id,
        )
        .first()
    )
    if row is None:
        row = WarehouseStock(
            organization_id=org_id,
            warehouse_id=warehouse_id,
            product_id=product_id,
            variant_id=variant_id,
            # An item nobody has tracked per warehouse yet opens with whatever the
            # catalog counter says — see on_hand() for why.
            on_hand_quantity=_untracked_catalog_stock(db, org_id, product_id, variant_id),
        )
        db.add(row)
        db.flush()
    return row


def lock_stock_items(
    db: Session,
    org_id: str,
    warehouse_id: str,
    items: list[dict | tuple[str, str | None]],
) -> list[WarehouseStock]:
    """Acquire pessimistic row-level locks on warehouse stock rows in a deterministic order.

    Deduplicates items, ensures each WarehouseStock row exists physically, sorts
    deterministically by (warehouse_id, product_id, variant_id or "") to eliminate
    PostgreSQL deadlocks across concurrent multi-item transactions, and locks each row
    using .with_for_update(nowait=False).
    """
    if not warehouse_id or not items:
        return []

    # Extract unique (product_id, variant_id)
    unique_pairs: set[tuple[str, str | None]] = set()
    for item in items:
        if isinstance(item, dict):
            pid = item.get("product_id")
            vid = item.get("variant_id")
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            pid, vid = item[0], item[1]
        elif hasattr(item, "product_id"):
            pid = item.product_id
            vid = getattr(item, "variant_id", None)
        else:
            continue
        if pid:
            unique_pairs.add((pid, vid))

    # Deterministic sorting to prevent cyclic lock dependency deadlocks
    sorted_pairs = sorted(unique_pairs, key=lambda p: (warehouse_id, p[0], p[1] or ""))

    locked_rows: list[WarehouseStock] = []
    for pid, vid in sorted_pairs:
        # Ensure row exists in DB
        _row(db, org_id, warehouse_id, pid, vid)
        # Lock row with SELECT ... FOR UPDATE. `WarehouseStock.warehouse` is
        # lazy="joined" at the mapper level, which would otherwise pull a LEFT
        # OUTER JOIN into this statement — Postgres refuses FOR UPDATE on the
        # nullable side of an outer join. lazyload() suppresses just that
        # default for this one query so the lock targets warehouse_stock alone.
        query = (
            db.query(WarehouseStock)
            .filter(
                WarehouseStock.warehouse_id == warehouse_id,
                WarehouseStock.product_id == pid,
                WarehouseStock.variant_id == vid,
            )
            .options(lazyload(WarehouseStock.warehouse))
            .with_for_update(nowait=False)
        )
        row = query.first()
        if row is not None:
            locked_rows.append(row)

    return locked_rows


def _untracked_catalog_stock(
    db: Session, org_id: str, product_id: str, variant_id: str | None
) -> float:
    """The catalog's own counter, but only while no warehouse tracks this item.

    Once any warehouse holds a row for it the warehouses are the truth, so this
    returns 0 and the counter is never counted twice.
    """
    already_tracked = (
        db.query(WarehouseStock.id)
        .filter(
            WarehouseStock.organization_id == org_id,
            WarehouseStock.product_id == product_id,
            WarehouseStock.variant_id == variant_id,
        )
        .first()
    )
    if already_tracked is not None:
        return 0.0
    return _catalog_stock(db, product_id, variant_id)


def _catalog_stock(db: Session, product_id: str, variant_id: str | None) -> float:
    """The product/variant's own counter — the pre-warehouse view of its stock."""
    if variant_id:
        variant = db.get(ProductVariant, variant_id)
        return float(variant.inventory or 0) if variant is not None else 0.0
    product = db.get(Product, product_id)
    return float(product.total_inventory or 0) if product is not None else 0.0


def on_hand(db: Session, warehouse_id: str, product_id: str, variant_id: str | None) -> float:
    """What is physically in this warehouse.

    An item no warehouse tracks yet reads from the catalog counter instead: products
    are created with a `total_inventory`, and purchases received before warehouses
    existed only moved that counter. Without the fallback such stock would read as
    zero the first time an order looked for it. As soon as any warehouse holds a row
    for the item, the warehouses are the truth and the counter is ignored.
    """
    rows = db.query(WarehouseStock).filter(
        WarehouseStock.product_id == product_id, WarehouseStock.variant_id == variant_id
    )
    tracked = rows.all()
    if not tracked:
        return round(_catalog_stock(db, product_id, variant_id), 3)
    return round(sum(r.on_hand_quantity or 0 for r in tracked if r.warehouse_id == warehouse_id), 3)


def reserved(db: Session, warehouse_id: str, product_id: str, variant_id: str | None) -> float:
    """Outstanding reservations — what is promised but not yet loaded out."""
    rows = db.query(StockReservation).filter(
        StockReservation.warehouse_id == warehouse_id,
        StockReservation.product_id == product_id,
        StockReservation.variant_id == variant_id,
        StockReservation.status == "active",
    )
    return round(sum(row.outstanding_quantity for row in rows), 3)


def available(db: Session, warehouse_id: str, product_id: str, variant_id: str | None) -> float:
    return round(
        on_hand(db, warehouse_id, product_id, variant_id)
        - reserved(db, warehouse_id, product_id, variant_id),
        3,
    )


def stock_summary(
    db: Session, warehouse_id: str, product_id: str, variant_id: str | None
) -> dict:
    """The three figures together, which is what an order response reports."""
    held = on_hand(db, warehouse_id, product_id, variant_id)
    promised = reserved(db, warehouse_id, product_id, variant_id)
    return {
        "product_id": product_id,
        "variant_id": variant_id,
        "warehouse_id": warehouse_id,
        "on_hand": held,
        "reserved": promised,
        "available": round(held - promised, 3),
    }


# --------------------------- physical stock moves ---------------------------


def adjust_on_hand(
    db: Session,
    org_id: str,
    warehouse_id: str,
    product_id: str,
    variant_id: str | None,
    delta: float,
    movement_type: str,
    note: str | None = None,
    created_by: str | None = None,
    batch: dict | None = None,
    serial_numbers: list[str] | None = None,
) -> float:
    """Move physical stock and record it in the ledger. Returns the new on-hand.

    Every caller goes through here so the warehouse row, the legacy catalog
    counters and the stock_movements ledger can never drift apart.

    For a product whose tracking flags are on, the lots and the serial numbers move
    with it — see `move_tracked` when the caller needs to know which lot it got.
    """
    return move_tracked(
        db, org_id, warehouse_id, product_id, variant_id, delta, movement_type,
        note=note, created_by=created_by, batch=batch, serial_numbers=serial_numbers,
    )[0]


def move_tracked(
    db: Session,
    org_id: str,
    warehouse_id: str,
    product_id: str,
    variant_id: str | None,
    delta: float,
    movement_type: str,
    note: str | None = None,
    created_by: str | None = None,
    batch: dict | None = None,
    serial_numbers: list[str] | None = None,
) -> tuple[float, dict]:
    """The same movement, and the batch / serials it actually moved.

    Returns `(on_hand, {batch_number, expiry_date, serial_numbers})`. A sale keeps that
    snapshot on its line, so an invoice or a challan can say which lot went out and
    which units — which is the whole point of tracking them.
    """
    _row(db, org_id, warehouse_id, product_id, variant_id)
    # See lock_stock_items() above: lazyload() keeps the eager-joined `warehouse`
    # relationship out of this FOR UPDATE statement so Postgres doesn't see it
    # as locking the nullable side of a LEFT OUTER JOIN.
    row = (
        db.query(WarehouseStock)
        .filter(
            WarehouseStock.warehouse_id == warehouse_id,
            WarehouseStock.product_id == product_id,
            WarehouseStock.variant_id == variant_id,
        )
        .options(lazyload(WarehouseStock.warehouse))
        .with_for_update(nowait=False)
        .first()
    )
    if row is None:
        row = _row(db, org_id, warehouse_id, product_id, variant_id)
    row.on_hand_quantity = round((row.on_hand_quantity or 0) + delta, 3)
    # This session runs with autoflush off, so the new figure has to be written
    # before the sum below can see it — otherwise the catalog counter is set from
    # the pre-change total and silently drifts from the warehouses.
    db.flush()
    _sync_catalog_counter(db, org_id, product_id, variant_id)
    db.add(
        StockMovement(
            organization_id=org_id,
            product_id=product_id,
            variant_id=variant_id,
            movement_type=movement_type,
            quantity=delta,
            balance_after=row.on_hand_quantity,
            note=note,
            created_by=created_by,
        )
    )
    db.flush()

    # Lots and serials move with the goods, whichever part of the app moved them.
    snapshot = tracking_service.apply(
        db, org_id, warehouse_id, product_id, variant_id, delta,
        batch=batch, serial_numbers=serial_numbers,
    )
    return row.on_hand_quantity, snapshot


def _sync_catalog_counter(db: Session, org_id: str, product_id: str, variant_id: str | None) -> None:
    """Keep `product.total_inventory` / `variant.inventory` equal to the sum across
    warehouses, so every endpoint written against those still reports the truth."""
    total = (
        db.query(func.coalesce(func.sum(WarehouseStock.on_hand_quantity), 0.0))
        .filter(
            WarehouseStock.organization_id == org_id,
            WarehouseStock.product_id == product_id,
            WarehouseStock.variant_id == variant_id,
        )
        .scalar()
        or 0
    )
    if variant_id:
        variant = db.get(ProductVariant, variant_id)
        if variant is not None:
            variant.inventory = int(total)
        return
    product = db.get(Product, product_id)
    if product is not None:
        product.total_inventory = int(total)


# ------------------------------ reservations -------------------------------


def shortages(db: Session, warehouse_id: str, wanted: list[dict]) -> list[dict]:
    """Which of the wanted lines the warehouse cannot cover.

    `wanted` is [{product_id, variant_id, quantity, order_item_id?}]. Lines for the
    same item are summed first — two lines of the same product must not each be
    checked against the full availability.
    """
    needed: dict[tuple[str, str | None], float] = {}
    for line in wanted:
        key = (line["product_id"], line.get("variant_id"))
        needed[key] = needed.get(key, 0) + (line.get("quantity") or 0)

    short = []
    for (product_id, variant_id), quantity in needed.items():
        free = available(db, warehouse_id, product_id, variant_id)
        if quantity > free:
            first = next(
                line for line in wanted
                if line["product_id"] == product_id and line.get("variant_id") == variant_id
            )
            short.append(
                {
                    "order_item_id": first.get("order_item_id"),
                    "product_id": product_id,
                    "variant_id": variant_id,
                    "product_name": first.get("product_name"),
                    "required_quantity": round(quantity, 3),
                    "available_quantity": free,
                    "short_quantity": round(quantity - free, 3),
                }
            )
    return short


def reserve_for_order(db: Session, order, warehouse_id: str) -> list[StockReservation]:  # noqa: ANN001
    """Hold stock for every line of an order. No physical movement happens."""
    held = []
    for item in order.items:
        if not item.product_id or not item.quantity:
            continue
        reservation = StockReservation(
            organization_id=order.organization_id,
            warehouse_id=warehouse_id,
            order_id=order.id,
            order_item_id=item.id,
            product_id=item.product_id,
            variant_id=item.variant_id,
            reserved_quantity=item.quantity,
            status="active",
        )
        db.add(reservation)
        held.append(reservation)
    db.flush()
    return held


def active_reservations(db: Session, order_id: str) -> list[StockReservation]:
    return (
        db.query(StockReservation)
        .filter(StockReservation.order_id == order_id, StockReservation.status == "active")
        .all()
    )


def release_for_order(db: Session, order_id: str) -> int:
    """Drop the holds on an order — what cancelling does. Physical stock is
    untouched, because nothing physical ever happened."""
    rows = active_reservations(db, order_id)
    for row in rows:
        row.status = "released"
    db.flush()
    return len(rows)


def consume_reservation(db: Session, reservation: StockReservation, quantity: float) -> None:
    """Mark part of a hold as taken — called when goods are loaded onto a vehicle.
    The hold closes once all of it has gone out."""
    reservation.consumed_quantity = round((reservation.consumed_quantity or 0) + quantity, 3)
    if reservation.consumed_quantity + 0.001 >= (reservation.reserved_quantity or 0):
        reservation.status = "consumed"
    db.flush()


# ------------------------- startup migration -------------------------------


def migrate_order_statuses() -> None:
    """Split the old single order status into status + fulfilment_status.

    Idempotent: only rows still holding a legacy value are touched, so it is safe to
    run on every boot. Rows already on the new vocabulary are left alone.
    """
    from app.core.database import SessionLocal
    from app.core.workflow import LEGACY_ORDER_STATUS
    from app.models import SalesOrder

    db = SessionLocal()
    try:
        changed = 0
        for order in db.query(SalesOrder).filter(SalesOrder.status.in_(list(LEGACY_ORDER_STATUS))):
            new_status, fulfilment = LEGACY_ORDER_STATUS[order.status]
            order.status = new_status
            if not order.fulfilment_status or order.fulfilment_status == "not_started":
                order.fulfilment_status = fulfilment
            changed += 1
        # Anything created before the column existed reads as not_started.
        for order in db.query(SalesOrder).filter(SalesOrder.fulfilment_status.is_(None)):
            order.fulfilment_status = "not_started"
            changed += 1
        if changed:
            db.commit()
    finally:
        db.close()
