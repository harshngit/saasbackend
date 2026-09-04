from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core import scoping, workflow
from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.models import Customer, Delivery, Invoice, Role, SalesOrder, StockReservation, User, UserRole
from app.schemas.sales_order import (
    AssignDeliveryBody,
    CancelBody,
    OrderCreate,
    OrderOut,
    OrderUpdate,
    PickupConfirmRequest,
    RejectBody,
)
from app.services import delivery_service, lookup_service, order_service, payment_service, stock_service

router = APIRouter(prefix="/orders", tags=["sales_orders"])

_view = require_permission("sales_orders", "view")
_create = require_permission("sales_orders", "create")
_approve = require_permission("sales_orders", "approve")
_edit = require_permission("sales_orders", "edit")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account")
    return user.organization_id


def _is_delivery_partner(db: Session, partner: User) -> bool:
    """Verify that the target user is a legitimate delivery partner."""
    return delivery_service.is_delivery_partner(db, partner)


def _owned(db: Session, order_id: str, org_id: str, user: User | None = None) -> SalesOrder:
    """The order, if this user may see it. Accepts the UUID or the human-facing code
    (order_number, sales_order_number).

    Pass `user` on anything a field role can reach: out of their scope reads as
    "not found", so they cannot probe the firm's order ids."""
    record = lookup_service.by_id_or_code(
        db, SalesOrder, order_id, org_id, SalesOrder.order_number, SalesOrder.sales_order_number
    )
    # Team Scope widens only the true CRM-ownership field (salesperson_id) to
    # teammates -- created_by/assigned_delivery_partner_id stay "this user
    # only" concepts even under team scope, same as they always were under
    # own scope. See app.core.scoping.owns_record's team_attributes param.
    out_of_scope = user is not None and not scoping.owns_record(
        db, user, record, "created_by", "salesperson_id", "assigned_delivery_partner_id",
        team_attributes=("salesperson_id",),
    ) if record is not None else False
    if record is None or out_of_scope:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sales order not found")
    return record


def _order_out(db: Session, order: SalesOrder, warnings: list[str] | None = None) -> OrderOut:
    """The order plus what the warehouse now holds for it, so the sales screen sees
    the effect of placing it without a second call."""
    out = OrderOut.model_validate(order)
    if order.warehouse_id:
        seen: set[tuple[str, str | None]] = set()
        summary = []
        for item in order.items:
            key = (item.product_id, item.variant_id)
            if not item.product_id or key in seen:
                continue
            seen.add(key)
            summary.append(
                stock_service.stock_summary(db, order.warehouse_id, item.product_id, item.variant_id)
            )
        out.stock_summary = summary
    out.warnings = warnings or []

    # Ensure delivery and invoice references are populated
    if not out.delivery_id:
        latest_del = (
            db.query(Delivery)
            .filter(
                Delivery.sales_order_id == order.id,
                Delivery.organization_id == order.organization_id,
                Delivery.status != "cancelled",
            )
            .order_by(Delivery.created_at.desc())
            .first()
        )
        if latest_del:
            out.delivery_id = latest_del.id
            out.delivery_number = latest_del.delivery_note_number

    if not out.invoice_id:
        latest_inv = (
            db.query(Invoice)
            .filter(
                Invoice.order_id == order.id,
                Invoice.organization_id == order.organization_id,
                Invoice.is_credit_note.is_(False),
            )
            .order_by(Invoice.created_at.desc())
            .first()
        )
        if latest_inv:
            out.invoice_id = latest_inv.id
            out.invoice_number = latest_inv.invoice_number

    # Fallback source for historical rows
    if order.source == "office":
        out.source = "quotation" if order.quotation_id else "direct"

    # Populate Financial Summary
    cust = order.customer or (db.get(Customer, order.customer_id) if order.customer_id else None)
    prev_bal = round(float(cust.outstanding_balance or 0.0), 2) if cust else 0.0
    out.previous_balance = prev_bal
    out.current_order_amount = round(float(order.total or 0.0), 2)

    inv = None
    if out.invoice_id:
        inv = db.get(Invoice, out.invoice_id)

    if inv:
        # Invoice has no `paid_amount`/`balance_due` attributes -- the real
        # columns/helper are `amount_paid` and payment_service.outstanding().
        out.paid_amount = round(float(inv.amount_paid or 0.0), 2)
        out.remaining_balance = payment_service.outstanding(inv)
    else:
        out.paid_amount = 0.0
        out.remaining_balance = out.current_order_amount

    out.total_due = round(prev_bal + out.remaining_balance, 2)

    return out


@router.get("", response_model=list[OrderOut])
def list_orders(
    user: User = Depends(_view),
    status_filter: str | None = Query(
        default=None, alias="status",
        description="draft | placed | awaiting_approval | processing | completed | cancelled. "
                    "Old values (pending, confirmed, out_for_delivery, …) still work.",
    ),
    fulfilment_status: str | None = Query(
        default=None,
        description="not_started | reserved | planned | loaded | in_transit | "
                    "partially_delivered | delivered | failed",
    ),
    customer_id: str | None = Query(default=None),
    assigned_delivery_partner_id: str | None = Query(default=None),
    search: str | None = Query(default=None, description="matches order_number"),
    db: Session = Depends(get_db),
) -> list[SalesOrder]:
    org_id = _org_id(user)
    query = db.query(SalesOrder).filter(SalesOrder.organization_id == org_id)
    if status_filter:
        # A client still filtering by an old status value is served through the same
        # map the stored rows were migrated with, so nothing broke on the day of the
        # split. `fulfilment_status` is the parameter for the goods-side states.
        mapped = workflow.LEGACY_ORDER_STATUS.get(status_filter)
        if mapped and status_filter not in workflow.ORDER_STATUSES:
            new_status, fulfilment = mapped
            query = query.filter(
                SalesOrder.status == new_status, SalesOrder.fulfilment_status == fulfilment
            )
        else:
            query = query.filter(SalesOrder.status == status_filter)
    if fulfilment_status:
        query = query.filter(SalesOrder.fulfilment_status == fulfilment_status)
    if customer_id:
        query = query.filter(SalesOrder.customer_id == customer_id)
    if assigned_delivery_partner_id:
        query = query.filter(SalesOrder.assigned_delivery_partner_id == assigned_delivery_partner_id)
    if search:
        query = query.filter(SalesOrder.order_number.ilike(f"%{search}%"))
    # A field role sees the orders it raised, was recorded against, or must
    # deliver. Team Scope widens only salesperson_id to teammates -- see
    # app.core.scoping.owned_by's team_columns param and _owned's matching note.
    query = scoping.owned_by(
        query, db, user,
        SalesOrder.created_by, SalesOrder.salesperson_id, SalesOrder.assigned_delivery_partner_id,
        team_columns=(SalesOrder.salesperson_id,),
    )
    return query.order_by(SalesOrder.created_at.desc()).all()


@router.get("/{order_id}", response_model=OrderOut)
def get_order(order_id: str, user: User = Depends(_view), db: Session = Depends(get_db)) -> OrderOut:
    return _order_out(db, _owned(db, order_id, _org_id(user), user))


@router.post("", response_model=OrderOut, status_code=status.HTTP_201_CREATED)
def create_order(
    payload: OrderCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> OrderOut:
    """Place a sales order.

    Validated, priced, reserved and **placed** in one call — no Admin approval step
    unless the firm has turned `order_requires_approval` on. Warehouse stock is held,
    not deducted (that happens when a vehicle is loaded), and no receivable is
    created (that starts at the invoice).

    `warehouse_id` defaults to the firm's default warehouse. Each line snapshots the
    tax rate it was sold at — its own `tax_rate`, else the product's — so an invoice
    raised later bills the agreed figure.

    A shortage returns 400 with `{"error": "INSUFFICIENT_STOCK", "shortages": [...]}`
    naming what is short, unless the firm allows backorders. The response carries
    `stock_summary` (on hand / reserved / available) and any `warnings`, such as the
    customer going past their credit limit.
    """
    org_id = _org_id(user)
    settings = workflow.sales_settings(user.organization)
    customer = db.get(Customer, payload.customer_id)
    if customer is None or customer.organization_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="customer_id is not a customer in your firm",
        )

    # Own-scope users (Sales Officers) are strictly forced to their own user ID
    if scoping.scope_to_own(db, user):
        salesperson_id = user.id
    else:
        salesperson_id = payload.salesperson_id
        if salesperson_id:
            sp = db.get(User, salesperson_id)
            if sp is None or sp.organization_id != org_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="salesperson_id is not a user in your firm",
                )

    order, warnings = order_service.place_order(
        db, user, customer,
        lines=[
            order_service.OrderLine(
                product_id=it.product_id,
                variant_id=it.variant_id,
                quantity=it.quantity,
                unit_price=it.unit_price,
                discount=it.discount,
                discount_percent=it.discount_percent,
                tax_rate=it.tax_rate,
                uom=it.uom,
                cost_price=it.cost_price,
            )
            for it in payload.items
        ],
        warehouse_id=payload.warehouse_id,
        order_date=payload.order_date,
        delivery_date=payload.delivery_date,
        fulfilment_method=payload.fulfilment_method,
        payment_type=payload.payment_type,
        payment_terms_days=payload.payment_terms_days,
        salesperson_id=salesperson_id,
        quotation_id=payload.quotation_id,
        source=payload.source,
        order_level_discount=payload.discount,
        order_level_tax=payload.tax,
        notes=payload.notes,
        order_status_label=payload.order_status,
        create_as_draft=settings["draft_orders_enabled"],
        billing_address=payload.billing_address or customer.billing_address,
        shipping_address=payload.shipping_address or payload.delivery_address or customer.delivery_address,
        delivery_address=payload.delivery_address or payload.shipping_address or customer.delivery_address,
        payment_terms=payload.payment_terms,
        delivery_terms=payload.delivery_terms,
        currency=payload.currency or "INR",
    )
    db.commit()
    db.refresh(order)
    return _order_out(db, order, warnings)


@router.patch("/{order_id}", response_model=OrderOut)
def update_order(
    order_id: str,
    payload: OrderUpdate,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> OrderOut:
    """Edit a sales order before fulfillment/dispatch."""
    org_id = _org_id(user)
    order = _owned(db, order_id, org_id, user)
    order, warnings = order_service.update_order(db, user, order, payload)
    db.commit()
    db.refresh(order)
    return _order_out(db, order, warnings)


@router.post("/{order_id}/confirm", response_model=OrderOut)
def confirm_order_endpoint(
    order_id: str,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> OrderOut:
    """Confirm a draft order: perform stock checks, reserve and move to placed/awaiting_approval."""
    org_id = _org_id(user)
    order = _owned(db, order_id, org_id, user)
    order, warnings = order_service.confirm_order(db, user, order)
    db.commit()
    db.refresh(order)
    return _order_out(db, order, warnings)


@router.patch("/{order_id}/approve", response_model=OrderOut)
def approve_order(
    order_id: str,
    user: User = Depends(_approve),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> SalesOrder:
    """Release an order that was waiting for approval → `placed`.

    Only reachable for a firm whose `order_requires_approval` is on; by default an
    order is placed on creation and never passes through here.

    Approval moves no stock and creates no receivable. Stock was reserved when the
    order was placed and leaves the warehouse when a vehicle is loaded; the
    receivable starts at the invoice.
    """
    order = _owned(db, order_id, _org_id(user), user)
    if order.status != "awaiting_approval":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Only an order awaiting approval can be approved (this is '{order.status}')",
        )
    order.status = "placed"
    order.approved_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(order)
    return _order_out(db, order)


@router.patch("/{order_id}/reject", response_model=OrderOut)
def reject_order(
    order_id: str,
    payload: RejectBody,
    user: User = Depends(_approve),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> SalesOrder:
    """Decline an order that was waiting for approval. Its reservations are released,
    so the stock is free for someone else immediately."""
    order = _owned(db, order_id, _org_id(user), user)
    if order.status != "awaiting_approval":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only an order awaiting approval can be rejected",
        )
    stock_service.release_for_order(db, order.id)
    order.status = "cancelled"
    order.fulfilment_status = "not_started"
    order.reject_reason = payload.reason
    db.commit()
    db.refresh(order)
    return _order_out(db, order)


@router.patch("/{order_id}/assign-delivery-partner", response_model=OrderOut)
def assign_delivery_partner(
    order_id: str,
    payload: AssignDeliveryBody,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> SalesOrder:
    """Name who will deliver the order.

    Assigning somebody is planning, not dispatch: the order becomes `planned`, not in
    transit. Goods go in transit when a vehicle has actually been loaded and the
    delivery is dispatched.

    This also **plans a Delivery** for whatever is still outstanding on the order, so
    assigned work means one thing everywhere — the deliveries list, the partner's app
    and the Staff Detail overview all read the same records. An order whose lines are
    already planned into a delivery just changes hands, since there is nothing left to
    plan.
    """
    org_id = _org_id(user)
    order = _owned(db, order_id, org_id, user)
    if order.fulfilment_method == "pickup":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot assign delivery partner to a pickup order",
        )
    if order.status == "cancelled" or order.fulfilment_status == "delivered":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot assign delivery on a '{order.status}' order",
        )
    if order.status == "draft":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot assign delivery to a draft order. Please confirm the order first.",
        )
    partner = db.get(User, payload.delivery_partner_id)
    if partner is None or partner.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="delivery_partner_id is not a user in your firm")
    if not _is_delivery_partner(db, partner):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Selected user is not a delivery partner")
    delivery_service.require_partner_available(db, org_id, partner, order.delivery_date or datetime.now(timezone.utc))
    order.assigned_delivery_partner_id = partner.id
    if order.status == "placed":
        order.status = "processing"
    if order.fulfilment_status in (None, "not_started", "reserved"):
        order.fulfilment_status = "planned"

    # Give the assignment a Delivery to hang off. Any delivery already raised for this
    # order simply changes hands instead — there is nothing outstanding left to plan.
    existing = (
        db.query(Delivery)
        .filter(
            Delivery.sales_order_id == order.id,
            Delivery.status.in_(workflow.OPEN_DELIVERY_STATUSES),
        )
        .all()
    )
    if existing:
        for delivery in existing:
            previous_partner_id = delivery.delivery_partner_id
            delivery.delivery_partner_id = partner.id
            if previous_partner_id != partner.id:
                delivery_service.record_history(
                    db, delivery, "reassigned" if previous_partner_id else "assigned", actor=user,
                    metadata={"previous_delivery_partner_id": previous_partner_id, "delivery_partner_id": partner.id},
                )
    else:
        try:
            delivery_service.plan(
                db, user, order, partner,
                vehicle_id=None, warehouse_id=None, scheduled_date=None,
                delivery_address=None, notes=None, wanted=None,
            )
        except HTTPException:
            # Nothing outstanding to plan (already delivered, or fully planned
            # elsewhere). The assignment itself still stands.
            pass

    db.commit()
    db.refresh(order)
    return _order_out(db, order)


@router.patch("/{order_id}/cancel", response_model=OrderOut)
def cancel_order(
    order_id: str,
    payload: CancelBody,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> SalesOrder:
    """Cancel an order and release whatever the warehouse was holding for it.

    Physical stock is untouched, because while goods are only reserved nothing
    physical has happened — no invented stock-in movements.

    Once goods have been loaded or dispatched a plain cancel is refused: those units
    are out of the warehouse, and bringing them back is the delivery-return /
    return-to-warehouse flow, not a status change.

    Any Delivery still linked to this order and still in a pre-operational
    state (planned / rejected / accepted / ready — i.e. nothing loaded onto a
    vehicle yet) is cancelled along with the order, in the same transaction,
    so an order never ends up cancelled while a Delivery still claims to be
    planned for it. The DISPATCHED_FULFILMENT guard above already refuses the
    whole operation once anything has been loaded, so nothing reachable past
    that point should ever be an operationally active Delivery — the loop
    below is a defensive belt-and-suspenders check, not the primary guard.
    """
    order = _owned(db, order_id, _org_id(user), user)
    if order.status == "cancelled":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This order is already cancelled")
    if order.fulfilment_status in workflow.DISPATCHED_FULFILMENT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Goods are already {order.fulfilment_status.replace('_', ' ')}. "
                   "Use the delivery return flow to bring them back to the warehouse.",
        )
    stock_service.release_for_order(db, order.id)
    for item in order.items:
        item.reserved_quantity = 0
    order.status = "cancelled"
    order.fulfilment_status = "not_started"
    if payload.reason:
        order.reject_reason = payload.reason

    linked_deliveries = (
        db.query(Delivery)
        .filter(Delivery.sales_order_id == order.id, Delivery.organization_id == order.organization_id)
        .all()
    )
    for delivery in linked_deliveries:
        if delivery.status == "cancelled":
            continue
        if "cancelled" not in workflow.DELIVERY_TRANSITIONS.get(delivery.status, set()):
            # Not reachable in practice given the DISPATCHED_FULFILMENT guard
            # above, but skipped rather than raised so an edge case here can
            # never block the order cancellation that was already approved.
            continue
        delivery_service.cancel(db, delivery, reason=payload.reason, actor=user)

    db.commit()
    db.refresh(order)
    return _order_out(db, order)


@router.post("/{order_id}/pickup/pick", response_model=OrderOut)
def pick_order_for_pickup(
    order_id: str,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> OrderOut:
    """Transition a confirmed pickup order from not_started -> picking.

    No stock movement happens at this stage.
    """
    org_id = _org_id(user)
    order = _owned(db, order_id, org_id, user)
    if order.status in ("draft", "cancelled", "rejected", "awaiting_approval"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot start picking a '{order.status}' order. Confirm the order first.",
        )
    if order.fulfilment_status == "delivered" or order.pickup_status == "collected":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Order has already been collected",
        )
    order.fulfilment_method = "pickup"
    order.pickup_status = "picking"
    order.status = "processing"
    db.commit()
    db.refresh(order)
    return _order_out(db, order)


@router.post("/{order_id}/pickup/ready", response_model=OrderOut)
def ready_order_for_pickup(
    order_id: str,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> OrderOut:
    """Transition a pickup order from picking -> ready for customer collection.

    No stock movement happens at this stage.
    """
    org_id = _org_id(user)
    order = _owned(db, order_id, org_id, user)
    if order.status in ("draft", "cancelled", "rejected", "awaiting_approval"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot mark a '{order.status}' order ready for pickup. Confirm the order first.",
        )
    if order.fulfilment_status == "delivered" or order.pickup_status == "collected":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Order has already been collected",
        )
    order.fulfilment_method = "pickup"
    order.pickup_status = "ready"
    order.status = "processing"
    db.commit()
    db.refresh(order)
    return _order_out(db, order)


@router.post("/{order_id}/pickup/confirm", response_model=OrderOut)
def confirm_order_pickup(
    order_id: str,
    payload: PickupConfirmRequest | None = None,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> OrderOut:
    """Customer collects the pickup order.

    Deducts warehouse physical stock and consumes outstanding reservations.
    Updates delivered/fulfilled quantity so subsequent invoicing bills the collected items.
    No vehicle stock or delivery movements are created.
    """
    org_id = _org_id(user)
    order = _owned(db, order_id, org_id, user)
    if order.status in ("draft", "cancelled", "rejected", "awaiting_approval"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot confirm pickup for a '{order.status}' order. Confirm the order first.",
        )
    if order.fulfilment_status == "delivered" or order.pickup_status == "collected":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Pickup for this order has already been confirmed",
        )

    order.fulfilment_method = "pickup"

    # Process items
    if payload and payload.items:
        items_map = {item.id: item for item in order.items}
        for item_in in payload.items:
            item = items_map.get(item_in.order_item_id)
            if item is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Item {item_in.order_item_id} does not belong to this order",
                )
            qty = float(item_in.collected_quantity)
            if qty > 0 and item.product_id and order.warehouse_id:
                stock_service.adjust_on_hand(
                    db, org_id, order.warehouse_id, item.product_id, item.variant_id,
                    -qty, "sale", note=f"Pickup for order {order.order_number}", created_by=user.id,
                )
                res = (
                    db.query(StockReservation)
                    .filter(StockReservation.order_item_id == item.id, StockReservation.status == "active")
                    .first()
                )
                if res:
                    stock_service.consume_reservation(db, res, qty)
                item.delivered_quantity = round((item.delivered_quantity or 0) + qty, 3)
                item.reserved_quantity = max(0.0, round((item.reserved_quantity or 0) - qty, 3))
    else:
        for item in order.items:
            qty = float(item.quantity or 0)
            if qty > 0 and item.product_id and order.warehouse_id:
                stock_service.adjust_on_hand(
                    db, org_id, order.warehouse_id, item.product_id, item.variant_id,
                    -qty, "sale", note=f"Pickup for order {order.order_number}", created_by=user.id,
                )
                res = (
                    db.query(StockReservation)
                    .filter(StockReservation.order_item_id == item.id, StockReservation.status == "active")
                    .first()
                )
                if res:
                    stock_service.consume_reservation(db, res, qty)
                item.delivered_quantity = qty
                item.reserved_quantity = 0.0

    order.pickup_status = "collected"
    order.fulfilment_status = "delivered"
    order.status = "completed"
    if payload:
        if payload.collected_by:
            order.collected_by = payload.collected_by
        if payload.notes:
            order.pickup_notes = payload.notes
    order.collected_at = datetime.now(timezone.utc)
    order.stock_deducted = True

    db.commit()
    db.refresh(order)
    return _order_out(db, order)


@router.post("/{order_id}/ready-for-pickup", response_model=OrderOut)
def ready_order_for_pickup_alias(
    order_id: str,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> OrderOut:
    """Alias for POST /orders/{id}/pickup/ready."""
    return ready_order_for_pickup(order_id, user, _unlocked, db)


@router.post("/{order_id}/picked-up", response_model=OrderOut)
def confirm_order_pickup_alias(
    order_id: str,
    payload: PickupConfirmRequest | None = None,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> OrderOut:
    """Alias for POST /orders/{id}/pickup/confirm."""
    return confirm_order_pickup(order_id, payload, user, _unlocked, db)
