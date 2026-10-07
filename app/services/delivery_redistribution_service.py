"""Delivery app only: hand one receiver units another receiver on the same run
did not take.

    Vehicle run (the partner's active VehicleLoading)
      ├── Delivery A  loaded 5, delivered 3   → 2 still on the vehicle
      ├── Delivery B  loaded 3, delivered 2   → 1 still on the vehicle
      └── Delivery C  loaded 2                → may receive up to 2 + 3 = 5

Nothing here changes what anyone ordered (`SalesOrderItem.quantity`), and nothing
moves warehouse stock — the units already left the warehouse when they were
loaded. What moves is the **line each unit is loaded against**: the units C takes
from A are moved off A's line onto C's (`loaded_quantity` −2 on A, +2 on C), so on
every line `loaded_quantity − delivered_quantity` keeps meaning "still on the
vehicle for this line". The existing `delivery_service.confirm()` then records the
hand-over exactly as it always has, its own per-line check passes legitimately,
and a later re-attempt on A cannot hand out the same two units again.

The website's POST /deliveries/{id}/confirm never comes through here and keeps its
loaded-quantity limit unchanged.
"""

from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy.orm import Session, lazyload

from app.models import (
    Delivery,
    DeliveryHistory,
    DeliveryItem,
    Invoice,
    SalesOrder,
    SalesOrderItem,
    User,
    VehicleLoading,
)
from app.services import delivery_service

# The target is mid-run: dispatched and not finished.
TARGET_STATUSES = ("in_transit", "partially_delivered")
# A donor has already been visited — what it did not take is genuinely spare.
# Deliveries still in_transit are owed their own units and never donate.
DONOR_STATUSES = ("partially_delivered", "failed")

_EPS = 0.001


def _naive(value: datetime | None) -> datetime | None:
    """SQLite hands datetimes back without a tzinfo; compare like with like."""
    if value is None:
        return None
    return value.replace(tzinfo=None)


def _loaded_in_session(db: Session, delivery: Delivery, loading: VehicleLoading) -> bool:
    """Whether every unit on this delivery went onto the vehicle during `loading`.

    There is no foreign key from a Delivery to the VehicleLoading it was loaded
    into, so this reads the delivery's own "loaded" timeline events: at least one,
    and none before the session started. A delivery loaded in an earlier session
    is excluded — its leftovers went back to the warehouse with that session's
    end-of-day return even though its line figures were never reset.
    """
    stamps = [
        _naive(row.created_at)
        for row in db.query(DeliveryHistory.created_at).filter(
            DeliveryHistory.organization_id == delivery.organization_id,
            DeliveryHistory.delivery_id == delivery.id,
            DeliveryHistory.event_type == "loaded",
        )
    ]
    if not stamps:
        return False
    return min(stamps) >= _naive(loading.created_at)


def active_loading(db: Session, delivery: Delivery) -> VehicleLoading | None:
    """The run this delivery belongs to: its partner's active session, on its own
    vehicle, with the delivery loaded during it. None otherwise."""
    if not delivery.delivery_partner_id or not delivery.vehicle_id:
        return None
    loading = delivery_service._open_loading(db, delivery.organization_id, delivery.delivery_partner_id)
    if loading is None or loading.vehicle_id != delivery.vehicle_id:
        return None
    if not _loaded_in_session(db, delivery, loading):
        return None
    return loading


def _candidate_donor_ids(db: Session, delivery: Delivery) -> list[str]:
    """Deliveries on the same run that could donate anything at all — used only to
    decide which rows to lock; the figures are re-read once they are locked."""
    rows = (
        db.query(Delivery.id)
        .filter(
            Delivery.organization_id == delivery.organization_id,
            Delivery.delivery_partner_id == delivery.delivery_partner_id,
            Delivery.vehicle_id == delivery.vehicle_id,
            Delivery.status.in_(DONOR_STATUSES),
            Delivery.id != delivery.id,
        )
        .all()
    )
    return [r.id for r in rows]


def _donor_lines(
    db: Session, delivery: Delivery, item: DeliveryItem, loading: VehicleLoading
) -> list[tuple[Delivery, DeliveryItem]]:
    """Every line on the same run, same product and variant, with units still on
    the vehicle. Oldest confirmation first, so consumption order is deterministic."""
    rows = (
        db.query(DeliveryItem, Delivery)
        .join(Delivery, DeliveryItem.delivery_id == Delivery.id)
        .options(lazyload(DeliveryItem.product), lazyload(DeliveryItem.variant))
        .filter(
            Delivery.organization_id == delivery.organization_id,
            Delivery.delivery_partner_id == delivery.delivery_partner_id,
            Delivery.vehicle_id == delivery.vehicle_id,
            Delivery.status.in_(DONOR_STATUSES),
            Delivery.id != delivery.id,
            DeliveryItem.product_id == item.product_id,
            DeliveryItem.variant_id.is_(None) if item.variant_id is None
            else DeliveryItem.variant_id == item.variant_id,
        )
        .populate_existing()
        .all()
    )
    lines = []
    for line, donor in rows:
        if round((line.loaded_quantity or 0) - (line.delivered_quantity or 0), 3) <= 0:
            continue
        if not _loaded_in_session(db, donor, loading):
            continue
        lines.append((donor, line))
    lines.sort(
        key=lambda pair: (
            _naive(pair[0].confirmed_at) or datetime.max,
            pair[0].id,
            pair[1].id,
        )
    )
    return lines


def _vehicle_available(loading: VehicleLoading | None, item: DeliveryItem) -> float:
    """What is physically on the vehicle for this product out of what was loaded
    for deliveries. `extra_qty` is left out on purpose: mid-day extra stock was
    never part of any delivery on this run."""
    if loading is None:
        return 0.0
    match = next(
        (
            x for x in loading.items
            if x.product_id == item.product_id and x.variant_id == item.variant_id
        ),
        None,
    )
    if match is None:
        return 0.0
    return float(max((match.loaded_qty or 0) - (match.delivered_qty or 0) - (match.returned_qty or 0), 0))


def item_capacity(
    db: Session, delivery: Delivery, item: DeliveryItem, loading: VehicleLoading | None
) -> dict:
    """The most this line can receive right now, and where the extra would come from.

        own_remaining        = loaded − delivered on this line
        transferable_surplus = Σ (loaded − delivered) over eligible donor lines,
                               capped by what the vehicle physically holds beyond
                               own_remaining
        max_allowed_delivery = own_remaining + transferable_surplus
    """
    own_remaining = round(max((item.loaded_quantity or 0) - (item.delivered_quantity or 0), 0), 3)
    order_item = db.get(SalesOrderItem, item.order_item_id) if item.order_item_id else None
    product = item.product

    blocked_reason = None
    sources: list[tuple[Delivery, DeliveryItem, float]] = []
    batch_excluded = 0.0

    if loading is None:
        blocked_reason = "Not part of the delivery partner's active vehicle run"
    elif not item.product_id:
        blocked_reason = "Line has no product"
    elif product is not None and product.serial_number_tracking:
        blocked_reason = "Serial-tracked products cannot be redistributed between receivers"
    else:
        for donor, line in _donor_lines(db, delivery, item, loading):
            surplus = round((line.loaded_quantity or 0) - (line.delivered_quantity or 0), 3)
            if product is not None and product.batch_tracking and (
                not item.batch_number or line.batch_number != item.batch_number
            ):
                batch_excluded += surplus
                continue
            sources.append((donor, line, surplus))

    donor_total = round(sum(s for _, _, s in sources), 3)
    physical_room = round(max(_vehicle_available(loading, item) - own_remaining, 0), 3)
    transferable = round(min(donor_total, physical_room), 3)
    if batch_excluded > 0 and not blocked_reason and transferable <= 0:
        blocked_reason = "Spare units on this run are from a different batch"

    return {
        "item": item,
        "delivery_item_id": item.id,
        "product_id": item.product_id,
        "variant_id": item.variant_id,
        "product_name": item.product_name,
        "batch_number": item.batch_number,
        "ordered_quantity": float(order_item.quantity) if order_item is not None else None,
        "planned_quantity": item.planned_quantity or 0,
        "loaded_quantity": item.loaded_quantity or 0,
        "delivered_quantity": item.delivered_quantity or 0,
        "own_remaining": own_remaining,
        "transferable_surplus": transferable,
        "max_allowed_delivery": round(own_remaining + transferable, 3),
        "batch_mismatched_surplus": round(batch_excluded, 3),
        "redistribution_blocked_reason": blocked_reason,
        "sources": sources,
        "surplus_sources": [
            {
                "delivery_id": donor.id,
                "delivery_number": donor.delivery_number,
                "delivery_item_id": line.id,
                "available": surplus,
            }
            for donor, line, surplus in sources
        ],
    }


def _billed_value(lines: list[tuple[SalesOrderItem, float]]) -> float:
    """What these quantities come to when billed at their order lines' terms.

    The same arithmetic POST /orders/{id}/invoice applies when it bills a delivery
    (routers/invoices.py::generate_from_order): unit price × quantity less the
    line discount scaled by quantity / ordered, the line's own tax rate on top,
    and no order-level discount — that belongs to the order as a whole.
    """
    subtotal = 0.0
    tax_total = 0.0
    for item, quantity in lines:
        if not quantity:
            continue
        ordered = item.quantity or 1
        share = quantity / ordered
        discount = round((item.discount or 0) * share, 2)
        line_total = round((item.unit_price or 0) * quantity - discount, 2)
        rate = item.tax_rate or 0
        subtotal += line_total
        tax_total += round(line_total * rate / 100, 2)
    return round(round(subtotal, 2) + round(tax_total, 2), 2)


def delivered_amounts(db: Session, delivery: Delivery) -> dict:
    """Delivery app only: money figures based on what was actually handed over.

    `DeliveryOut.amount_due` stays the order total less payments — the website's
    figure. When a receiver takes more than they ordered that understates what
    they owe, so the app gets these alongside it:

        delivered_value       this delivery's delivered units, billed as above
        order_delivered_value every delivered unit on the order, billed as above
        paid_amount           paid on the order's invoices — the same figure
                              amount_due subtracts
        app_amount_due        max(order_delivered_value − paid_amount, 0)

    Order-level on purpose, like amount_due: a payment is recorded against the
    order's invoices, not against one delivery. For an order with a single
    delivery, order_delivered_value == delivered_value.

    Collecting more than the order total still needs the delivery invoiced first:
    POST /deliveries/{id}/collections caps a collection at the latest invoice's
    outstanding amount, or the order total when nothing is invoiced yet.
    """
    order = db.get(SalesOrder, delivery.sales_order_id) if delivery.sales_order_id else None
    if order is None:
        return {"delivered_value": 0.0, "order_delivered_value": 0.0, "paid_amount": 0.0, "app_amount_due": 0.0}
    by_id = {item.id: item for item in order.items}
    delivered_value = _billed_value([
        (by_id[line.order_item_id], line.delivered_quantity or 0)
        for line in delivery.items
        if line.order_item_id in by_id
    ])
    order_delivered_value = _billed_value([(item, item.delivered_quantity or 0) for item in order.items])
    paid = round(sum(
        inv.amount_paid or 0
        for inv in db.query(Invoice).filter(Invoice.order_id == order.id, Invoice.is_credit_note.is_(False))
    ), 2)
    return {
        "delivered_value": delivered_value,
        "order_delivered_value": order_delivered_value,
        "paid_amount": paid,
        "app_amount_due": round(max(order_delivered_value - paid, 0), 2),
    }


def capacity(db: Session, delivery: Delivery) -> dict:
    """GET /deliveries/{id}/app/delivery-capacity. Read-only."""
    loading = active_loading(db, delivery)
    items = [item_capacity(db, delivery, item, loading) for item in delivery.items]
    return {
        "delivery_id": delivery.id,
        "delivery_number": delivery.delivery_number,
        "status": delivery.status,
        "vehicle_loading_id": loading.id if loading is not None else None,
        **delivered_amounts(db, delivery),
        "items": items,
    }


def require_own_target(user: User, delivery: Delivery) -> None:
    if delivery.delivery_partner_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This delivery is not assigned to you",
        )


def confirm(
    db: Session,
    user: User,
    delivery: Delivery,
    lines: list[dict],
    pod_photo_file_ids: list[str],
    signature_file_id: str | None,
    notes: str | None,
    failed: bool,
    failure_reason: str | None,
    receiver_name: str | None = None,
) -> list[dict]:
    """POST /deliveries/{id}/app/confirm. Does not commit.

    `lines` is [{delivery_item_id, delivered_quantity, expected_max}]. Anything
    beyond a line's own remaining quantity is first moved onto it from donor lines
    on the same run, then `delivery_service.confirm()` records the hand-over
    unchanged. Returns the reallocations made.
    """
    require_own_target(user, delivery)

    if failed:
        # Nothing handed over, nothing to move — the ordinary path in full.
        delivery_service.confirm(
            db, user, delivery, lines=[], pod_photo_file_ids=pod_photo_file_ids,
            signature_file_id=signature_file_id, notes=notes, failed=True,
            failure_reason=failure_reason, receiver_name=receiver_name,
        )
        return []

    if delivery.status not in TARGET_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Only an in-transit or part-delivered delivery can be confirmed here "
                   f"(current status: {delivery.status})",
        )
    seen: set[str] = set()
    for line in lines:
        if line["delivery_item_id"] in seen:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"delivery_item_id {line['delivery_item_id']} is listed more than once",
            )
        seen.add(line["delivery_item_id"])

    # Run, then this delivery — the same lock the website confirm takes, in the same
    # order — then the donors in id order. Everything on a run queues behind the
    # run's lock, so neither another app confirm nor a website confirm on a donor
    # can act on figures this request is about to change.
    if delivery_service.lock_run_for_confirm(db, delivery) is None:
        loading = None
    else:
        loading = active_loading(db, delivery)
    donor_ids = sorted(_candidate_donor_ids(db, delivery)) if loading is not None else []
    for delivery_id in donor_ids:
        delivery_service.lock_delivery_row(db, delivery_id)
    if donor_ids:
        (
            db.query(DeliveryItem)
            .options(lazyload(DeliveryItem.product), lazyload(DeliveryItem.variant))
            .filter(DeliveryItem.delivery_id.in_(donor_ids))
            .populate_existing()
            .all()
        )

    # Re-check under the lock: the status may have moved while we waited.
    if delivery.status not in TARGET_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Only an in-transit or part-delivered delivery can be confirmed here "
                   f"(current status: {delivery.status})",
        )

    by_id = {item.id: item for item in delivery.items}
    reallocations: list[dict] = []
    for line in lines:
        item = by_id.get(line["delivery_item_id"])
        if item is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"delivery_item_id {line['delivery_item_id']} is not on this delivery",
            )
        wanted = float(line["delivered_quantity"])
        cap = item_capacity(db, delivery, item, loading)
        expected_max = line.get("expected_max")
        if expected_max is not None and abs(float(expected_max) - cap["max_allowed_delivery"]) > _EPS:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": f"{item.product_name}: the quantity available has changed "
                               f"(now {cap['max_allowed_delivery']:g}, you saw {float(expected_max):g}). "
                               f"Refresh and try again.",
                    "delivery_item_id": item.id,
                    "max_allowed_delivery": cap["max_allowed_delivery"],
                },
            )

        extra = round(wanted - cap["own_remaining"], 3)
        if extra <= _EPS:
            continue
        if extra > cap["transferable_surplus"] + _EPS:
            reason = cap["redistribution_blocked_reason"]
            if reason is None and cap["batch_mismatched_surplus"] > 0:
                reason = "the remaining spare units on this run are from a different batch"
            detail = (
                f"{item.product_name}: at most {cap['max_allowed_delivery']:g} can be delivered "
                f"({cap['own_remaining']:g} of its own plus {cap['transferable_surplus']:g} spare "
                f"on this vehicle run), asked for {wanted:g}"
            )
            if reason:
                detail = f"{detail}. {reason[0].upper()}{reason[1:]}"
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)

        remaining = extra
        for donor, donor_line, surplus in cap["sources"]:
            if remaining <= _EPS:
                break
            take = round(min(surplus, remaining), 3)
            if take <= 0:
                continue
            donor_line.loaded_quantity = round((donor_line.loaded_quantity or 0) - take, 3)
            item.loaded_quantity = round((item.loaded_quantity or 0) + take, 3)
            remaining = round(remaining - take, 3)
            meta = {
                "from_delivery_id": donor.id,
                "from_delivery_item_id": donor_line.id,
                "to_delivery_id": delivery.id,
                "to_delivery_item_id": item.id,
                "product_id": item.product_id,
                "variant_id": item.variant_id,
                "quantity": take,
                "vehicle_loading_id": loading.id if loading is not None else None,
            }
            delivery_service.record_history(
                db, donor, "quantity_reallocated", actor=user,
                notes=f"{take:g} × {item.product_name} handed to {delivery.delivery_number}",
                metadata=meta,
            )
            delivery_service.record_history(
                db, delivery, "quantity_reallocated", actor=user,
                notes=f"{take:g} × {item.product_name} taken from {donor.delivery_number}",
                metadata=meta,
            )
            reallocations.append(
                {
                    "from_delivery_id": donor.id,
                    "from_delivery_item_id": donor_line.id,
                    "to_delivery_item_id": item.id,
                    "product_id": item.product_id,
                    "variant_id": item.variant_id,
                    "quantity": take,
                }
            )
        db.flush()

    # confirm() re-reads under the same lock (already held), so the moved units
    # must be flushed for it to see them.
    db.flush()
    delivery_service.confirm(
        db, user, delivery,
        lines=[
            {"delivery_item_id": line["delivery_item_id"], "delivered_quantity": line["delivered_quantity"]}
            for line in lines
        ],
        pod_photo_file_ids=pod_photo_file_ids,
        signature_file_id=signature_file_id,
        notes=notes,
        failed=False,
        failure_reason=None,
        receiver_name=receiver_name,
    )
    return reallocations
