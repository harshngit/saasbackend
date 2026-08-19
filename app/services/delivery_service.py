"""Deliveries: planning, loading, dispatch and confirmation.

The Delivery is the record the fulfilment half of the flow turns on, and its own id
is what every endpoint takes:

    Sales Order ord_001
      └── Delivery del_001
            └── Delivery Item di_001   (planned / loaded / delivered)

Naming a partner is **planning**, not dispatch. Goods only leave the warehouse when a
vehicle is loaded, and the partner only sees the delivery as live once it has been
dispatched. Each step is deliberately separate because each one is a different real
event.
"""

from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models import (
    Delivery,
    DeliveryItem,
    Invoice,
    SalesOrder,
    SalesOrderItem,
    StockReservation,
    User,
    VehicleLoading,
    VehicleLoadingItem,
)
from app.services.tracking_service import TrackingError
from app.services import numbering_service, stock_service


def next_delivery_number(db: Session, org_id: str) -> str:
    return numbering_service.next_number(db, org_id, Delivery.delivery_note_number, "DLV")


def amount_due(db: Session, delivery: Delivery) -> float:
    """What the customer still owes on the order behind this delivery — what the
    partner needs to know before handing anything over."""
    if not delivery.sales_order_id:
        return 0.0
    order = db.get(SalesOrder, delivery.sales_order_id)
    if order is None:
        return 0.0
    paid = sum(
        i.amount_paid or 0
        for i in db.query(Invoice).filter(
            Invoice.order_id == order.id, Invoice.is_credit_note.is_(False)
        )
    )
    return round(max((order.total or 0) - paid, 0), 2)


def outstanding_for_order_item(db: Session, item: SalesOrderItem) -> float:
    """How much of an order line no delivery has planned yet, so two deliveries
    cannot promise the same units."""
    planned = sum(
        (line.planned_quantity or 0)
        for line in db.query(DeliveryItem)
            .join(Delivery, DeliveryItem.delivery_id == Delivery.id)
            .filter(DeliveryItem.order_item_id == item.id, Delivery.status != "cancelled")
    )
    return round(max((item.quantity or 0) - planned, 0), 3)


def plan(
    db: Session,
    user: User,
    order: SalesOrder,
    delivery_partner: User | None,
    vehicle_id: str | None,
    warehouse_id: str | None,
    scheduled_date: datetime | None,
    delivery_address: str | None,
    notes: str | None,
    wanted: list[dict] | None,
) -> Delivery:
    """Create one Delivery for an order. Does not commit.

    `wanted` is [{order_item_id, planned_quantity}], or None to plan everything still
    outstanding on the order.
    """
    org_id = order.organization_id
    by_id = {item.id: item for item in order.items}

    if wanted is None:
        lines = [
            {"order_item_id": item.id, "planned_quantity": outstanding_for_order_item(db, item)}
            for item in order.items
        ]
        lines = [line for line in lines if line["planned_quantity"] > 0]
        if not lines:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Every line on this order is already planned into a delivery",
            )
    else:
        lines = wanted

    delivery = Delivery(
        organization_id=org_id,
        delivery_note_number=next_delivery_number(db, org_id),
        delivery_date=scheduled_date or datetime.now(timezone.utc),
        sales_order_id=order.id,
        customer_id=order.customer_id,
        warehouse_id=warehouse_id or order.warehouse_id,
        delivery_address=(
            delivery_address
            or (
                (order.customer.delivery_address or order.customer.billing_address)
                if order.customer is not None
                else None
            )
        ),
        delivery_partner_id=delivery_partner.id if delivery_partner else None,
        vehicle_id=vehicle_id,
        scheduled_date=scheduled_date,
        status="planned",
        delivery_status="pending",
        notes=notes,
        created_by=user.id,
    )

    for line in lines:
        item = by_id.get(line["order_item_id"])
        if item is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"order_item_id {line['order_item_id']} is not a line on this order",
            )
        outstanding = outstanding_for_order_item(db, item)
        if line["planned_quantity"] > outstanding + 0.001:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{item.product_name}: only {outstanding:g} left to plan "
                       f"(asked for {line['planned_quantity']:g})",
            )
        delivery.items.append(
            DeliveryItem(
                order_item_id=item.id,
                product_id=item.product_id,
                variant_id=item.variant_id,
                product_name=item.product_name,
                planned_quantity=line["planned_quantity"],
            )
        )

    db.add(delivery)
    db.flush()

    # Planning is not dispatch — the order is being worked on, nothing has moved.
    if order.status == "placed":
        order.status = "processing"
    if order.fulfilment_status in (None, "not_started", "reserved"):
        order.fulfilment_status = "planned"
    if delivery_partner is not None:
        order.assigned_delivery_partner_id = delivery_partner.id
    return delivery


def accept(db: Session, user: User, delivery: Delivery) -> Delivery:
    """Partner accepts a planned delivery. Does not commit."""
    if delivery.status != "planned":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Only planned deliveries can be accepted (this delivery is '{delivery.status}')",
        )
    if delivery.delivery_partner_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This delivery is not assigned to you",
        )
    delivery.status = "accepted"
    return delivery


def reject(db: Session, user: User, delivery: Delivery, reason: str | None = None) -> Delivery:
    """Partner rejects a planned delivery. Clears partner + vehicle. Does not commit."""
    if delivery.status != "planned":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Only planned deliveries can be rejected (this delivery is '{delivery.status}')",
        )
    if delivery.delivery_partner_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This delivery is not assigned to you",
        )
    delivery.status = "rejected"
    reason_str = reason.strip() if reason and reason.strip() else "no reason given"
    note_text = f"Rejected by partner: {reason_str}"
    if delivery.notes:
        delivery.notes = f"{delivery.notes}\n{note_text}"
    else:
        delivery.notes = note_text
    delivery.delivery_partner_id = None
    delivery.vehicle_id = None
    return delivery


def _open_loading(db: Session, org_id: str, partner_id: str) -> VehicleLoading | None:
    return (
        db.query(VehicleLoading)
        .filter(
            VehicleLoading.organization_id == org_id,
            VehicleLoading.delivery_partner_id == partner_id,
            VehicleLoading.status == "active",
        )
        .order_by(VehicleLoading.date.desc())
        .first()
    )


def _consume_reservations(db: Session, order_id: str, item: DeliveryItem, quantity: float) -> None:
    """Take `quantity` out of the holds on this order line. The reservation closes
    once all of it has gone out; a partial load leaves the rest held."""
    remaining = quantity
    holds = (
        db.query(StockReservation)
        .filter(
            StockReservation.order_id == order_id,
            StockReservation.order_item_id == item.order_item_id,
            StockReservation.status == "active",
        )
        .all()
    )
    for hold in holds:
        if remaining <= 0:
            break
        take = min(hold.outstanding_quantity, remaining)
        if take <= 0:
            continue
        stock_service.consume_reservation(db, hold, take)
        remaining -= take


def load(
    db: Session, user: User, delivery: Delivery, wanted: list[dict] | None
) -> tuple[VehicleLoading, list[dict]]:
    """Move the goods from the warehouse onto the vehicle. Does not commit.

    This is the one place physical stock leaves the warehouse:

        warehouse on hand ↓   reservation consumed ↓   vehicle stock ↑   loaded_qty ↑

    Idempotent by quantity: each delivery line can only ever be loaded up to its
    planned quantity, so calling this twice loads the remainder and then nothing —
    it never deducts the same units twice.
    """
    org_id = delivery.organization_id
    if delivery.status == "cancelled":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="This delivery was cancelled"
        )
    if delivery.status == "planned":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The delivery partner has not accepted this delivery yet",
        )
    if delivery.status == "rejected":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This delivery was rejected and needs to be reassigned",
        )
    if not delivery.delivery_partner_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Name a delivery partner before loading — the stock goes onto their vehicle",
        )
    warehouse = stock_service.owned_warehouse(db, delivery.warehouse_id, org_id)
    if warehouse is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="This delivery has no valid warehouse"
        )

    by_id = {item.id: item for item in delivery.items}
    if wanted is None:
        lines = [
            {"delivery_item_id": item.id,
             "loaded_quantity": round((item.planned_quantity or 0) - (item.loaded_quantity or 0), 3)}
            for item in delivery.items
        ]
        lines = [line for line in lines if line["loaded_quantity"] > 0]
        if not lines:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Everything planned on this delivery is already loaded",
            )
    else:
        lines = wanted

    loading = _open_loading(db, org_id, delivery.delivery_partner_id)
    if loading is None:
        loading = VehicleLoading(
            organization_id=org_id,
            delivery_partner_id=delivery.delivery_partner_id,
            date=datetime.now(timezone.utc),
            status="active",
        )
        db.add(loading)
        db.flush()

    results = []
    for line in lines:
        item = by_id.get(line["delivery_item_id"])
        if item is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"delivery_item_id {line['delivery_item_id']} is not on this delivery",
            )
        quantity = line["loaded_quantity"]
        room = round((item.planned_quantity or 0) - (item.loaded_quantity or 0), 3)
        if quantity > room + 0.001:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{item.product_name}: {item.loaded_quantity:g} of {item.planned_quantity:g} "
                       f"is already loaded, so at most {room:g} more can go on",
            )
        if quantity <= 0:
            continue
        if not item.product_id:
            continue

        on_hand = stock_service.on_hand(db, warehouse.id, item.product_id, item.variant_id)
        if quantity > on_hand + 0.001:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{item.product_name}: only {on_hand:g} on hand in {warehouse.name}",
            )

        # Warehouse ↓ and the ledger row for it. For a tracked product the lot and the
        # units go with it — the order line's request if it named one, else earliest
        # expiry first — and the challan then says which lot the customer received.
        order_line = db.get(SalesOrderItem, item.order_item_id) if item.order_item_id else None
        requested_batch = getattr(order_line, "batch_number", None)
        requested_serials = getattr(order_line, "serial_numbers", None)
        try:
            after, tracked = stock_service.move_tracked(
                db, org_id, warehouse.id, item.product_id, item.variant_id,
                -quantity, "delivery_out",
                note=f"Loaded onto vehicle for {delivery.delivery_note_number}",
                created_by=user.id,
                batch={"batch_number": requested_batch} if requested_batch else None,
                serial_numbers=requested_serials,
            )
        except TrackingError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
        if tracked["batch_number"]:
            item.batch_number = tracked["batch_number"]
        if tracked["expiry_date"]:
            item.expiry_date = tracked["expiry_date"]
        if tracked["serial_numbers"]:
            item.serial_numbers = (item.serial_numbers or []) + tracked["serial_numbers"]
        # The hold is now taken rather than promised.
        if delivery.sales_order_id:
            _consume_reservations(db, delivery.sales_order_id, item, quantity)
        # Vehicle ↑.
        match = next(
            (
                x for x in loading.items
                if x.product_id == item.product_id and x.variant_id == item.variant_id
            ),
            None,
        )
        if match is None:
            match = VehicleLoadingItem(
                loading_id=loading.id,
                product_id=item.product_id,
                variant_id=item.variant_id,
                product_name=item.product_name,
                loaded_qty=0,
            )
            loading.items.append(match)
        match.loaded_qty = int((match.loaded_qty or 0) + quantity)

        item.loaded_quantity = round((item.loaded_quantity or 0) + quantity, 3)
        db.flush()
        results.append(
            {
                "delivery_item_id": item.id,
                "loaded_quantity": item.loaded_quantity,
                "warehouse_on_hand_after": after,
                "vehicle_stock_after": match.loaded_qty,
            }
        )

    if delivery.loaded_total > 0:
        delivery.status = "loaded"
        order = db.get(SalesOrder, delivery.sales_order_id) if delivery.sales_order_id else None
        if order is not None:
            order.fulfilment_status = "loaded"
    return loading, results


def dispatch(db: Session, user: User, delivery: Delivery) -> None:
    """Send it out. Only now is the delivery live for the partner."""
    if delivery.status != "loaded":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"A '{delivery.status}' delivery cannot be dispatched",
        )
    if delivery.loaded_total <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nothing has been loaded onto the vehicle yet",
        )
    delivery.status = "in_transit"
    delivery.dispatched_at = datetime.now(timezone.utc)
    delivery.dispatched_by_id = user.id
    order = db.get(SalesOrder, delivery.sales_order_id) if delivery.sales_order_id else None
    if order is not None:
        order.fulfilment_status = "in_transit"
        if order.status == "placed":
            order.status = "processing"


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
) -> None:
    """Record what was actually handed over. Does not commit.

    Vehicle stock falls by the **delivered** quantity only. Anything loaded but not
    handed over stays on the vehicle until a re-attempt or the end-of-day return — it
    is never silently put back into the warehouse.
    """
    if delivery.status in ("delivered", "cancelled"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"This delivery is already {delivery.status}",
        )
    if delivery.status == "planned":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Load and dispatch the delivery before confirming an outcome",
        )

    delivery.pod_photo_file_ids = list(pod_photo_file_ids or [])
    delivery.pod_signature_file_id = signature_file_id
    if notes:
        delivery.notes = notes
    delivery.confirmed_at = datetime.now(timezone.utc)

    if failed:
        # Nothing was handed over: the goods are still on the vehicle.
        delivery.status = "failed"
        delivery.failure_reason = failure_reason
        delivery.delivery_status = "failed"
        order = db.get(SalesOrder, delivery.sales_order_id) if delivery.sales_order_id else None
        if order is not None:
            order.fulfilment_status = "failed"
        return

    by_id = {item.id: item for item in delivery.items}
    loading = (
        _open_loading(db, delivery.organization_id, delivery.delivery_partner_id)
        if delivery.delivery_partner_id else None
    )
    for line in lines:
        item = by_id.get(line["delivery_item_id"])
        if item is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"delivery_item_id {line['delivery_item_id']} is not on this delivery",
            )
        quantity = line["delivered_quantity"]
        room = round((item.loaded_quantity or 0) - (item.delivered_quantity or 0), 3)
        if quantity > room + 0.001:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{item.product_name}: only {room:g} of what was loaded is still "
                       f"on the vehicle",
            )
        if quantity <= 0:
            continue
        item.delivered_quantity = round((item.delivered_quantity or 0) + quantity, 3)
        # Vehicle stock falls by what was handed over.
        if loading is not None:
            match = next(
                (
                    x for x in loading.items
                    if x.product_id == item.product_id and x.variant_id == item.variant_id
                ),
                None,
            )
            if match is not None:
                match.delivered_qty = int((match.delivered_qty or 0) + quantity)
        # And the order line records what the customer actually received.
        if item.order_item_id:
            order_item = db.get(SalesOrderItem, item.order_item_id)
            if order_item is not None:
                order_item.delivered_quantity = round(
                    (order_item.delivered_quantity or 0) + quantity, 3
                )

    delivered = delivery.delivered_total
    planned = delivery.planned_total
    delivery.status = "delivered" if delivered + 0.001 >= planned else "partially_delivered"
    delivery.delivery_status = "delivered" if delivery.status == "delivered" else "partial"

    order = db.get(SalesOrder, delivery.sales_order_id) if delivery.sales_order_id else None
    if order is not None:
        _settle_order_fulfilment(db, order)


def _settle_order_fulfilment(db: Session, order: SalesOrder) -> None:
    """The order is delivered once every line has been, across all its deliveries."""
    ordered = sum(item.quantity or 0 for item in order.items)
    delivered = sum(item.delivered_quantity or 0 for item in order.items)
    if delivered <= 0:
        return
    if delivered + 0.001 >= ordered:
        order.fulfilment_status = "delivered"
        order.status = "completed"
    else:
        order.fulfilment_status = "partially_delivered"
        order.status = "processing"


def sync_delivery_view(db: Session, delivery: Delivery) -> dict:
    """The extra fields the response carries beyond the columns."""
    order = db.get(SalesOrder, delivery.sales_order_id) if delivery.sales_order_id else None
    return {
        # `delivery_number` and `order_id` come off the model as properties.
        "order_number": order.order_number if order is not None else None,
        "order_status": order.status if order is not None else None,
        "order_total": order.total if order is not None else None,
        "fulfilment_status": order.fulfilment_status if order is not None else None,
        "order": {
            "id": order.id,
            "order_number": order.order_number,
            "status": order.status,
            "fulfilment_status": order.fulfilment_status,
            "total": order.total,
        } if order is not None else None,
        "amount_due": amount_due(db, delivery),
        "pod": {
            "photo_file_ids": list(delivery.pod_photo_file_ids or []),
            "signature_file_id": delivery.pod_signature_file_id,
        },
    }

