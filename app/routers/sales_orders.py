from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core import scoping, workflow
from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.models import Customer, Delivery, SalesOrder, User
from app.schemas.sales_order import (
    AssignDeliveryBody,
    CancelBody,
    OrderCreate,
    OrderOut,
    RejectBody,
)
from app.services import delivery_service, lookup_service, order_service, stock_service

router = APIRouter(prefix="/orders", tags=["sales_orders"])

_view = require_permission("sales_orders", "view")
_create = require_permission("sales_orders", "create")
_approve = require_permission("sales_orders", "approve")
_edit = require_permission("sales_orders", "edit")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account")
    return user.organization_id


def _owned(db: Session, order_id: str, org_id: str, user: User | None = None) -> SalesOrder:
    """The order, if this user may see it. Accepts the UUID or the human-facing code
    (order_number, sales_order_number).

    Pass `user` on anything a field role can reach: out of their scope reads as
    "not found", so they cannot probe the firm's order ids."""
    record = lookup_service.by_id_or_code(
        db, SalesOrder, order_id, org_id, SalesOrder.order_number, SalesOrder.sales_order_number
    )
    out_of_scope = user is not None and not scoping.owns_record(
        db, user, record, "created_by", "salesperson_id", "assigned_delivery_partner_id"
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
    # A field role sees the orders it raised, was recorded against, or must deliver.
    query = scoping.owned_by(
        query, db, user,
        SalesOrder.created_by, SalesOrder.salesperson_id, SalesOrder.assigned_delivery_partner_id,
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
    order, warnings = order_service.place_order(
        db, user, customer,
        lines=[
            order_service.OrderLine(
                product_id=it.product_id, variant_id=it.variant_id, quantity=it.quantity,
                unit_price=it.unit_price, discount=it.discount, tax_rate=it.tax_rate,
            )
            for it in payload.items
        ],
        warehouse_id=payload.warehouse_id,
        order_date=payload.order_date,
        delivery_date=payload.delivery_date,
        fulfilment_method=payload.fulfilment_method,
        payment_type=payload.payment_type,
        payment_terms_days=payload.payment_terms_days,
        salesperson_id=payload.salesperson_id,
        quotation_id=payload.quotation_id,
        source=payload.source,
        order_level_discount=payload.discount,
        order_level_tax=payload.tax,
        notes=payload.notes,
        order_status_label=payload.order_status,
        create_as_draft=settings["draft_orders_enabled"],
    )
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
    order = _owned(db, order_id, _org_id(user))
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
    order = _owned(db, order_id, _org_id(user))
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
    order = _owned(db, order_id, org_id)
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
            delivery.delivery_partner_id = partner.id
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
    """
    order = _owned(db, order_id, _org_id(user))
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
    db.commit()
    db.refresh(order)
    return _order_out(db, order)
