from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core import scoping
from app.core.deps import require_permission, require_unlocked_org
from app.models import (
    Customer,
    Product,
    ProductVariant,
    SalesOrder,
    SalesOrderItem,
    User,
)
from app.schemas.sales_order import (
    AssignDeliveryBody,
    CancelBody,
    OrderCreate,
    OrderItemIn,
    OrderOut,
    RejectBody,
)
from app.core import workflow
from app.services import notification_service, numbering_service, lookup_service, stock_service

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


def _next_order_number(db: Session, org_id: str) -> str:
    # max+1, not count+1: counting reissues a number after any deletion.
    return numbering_service.next_number(db, org_id, SalesOrder.order_number, "SO")


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


def _credit_warning(db: Session, customer: Customer, order_total: float, action: str) -> str | None:
    """Whether this order takes the customer past their credit limit, and what the
    firm's `credit_limit_action` says to do about it."""
    limit = customer.credit_limit or 0
    if action == "ignore" or limit <= 0:
        return None
    projected = round((customer.outstanding_balance or 0) + order_total, 2)
    if projected <= limit:
        return None
    message = (
        f"{customer.name} would be at Rs {projected:,.2f} against a credit limit of "
        f"Rs {limit:,.2f}"
    )
    if action == "block":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)
    return message


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
) -> SalesOrder:
    org_id = _org_id(user)
    settings = workflow.sales_settings(user.organization)
    customer = db.get(Customer, payload.customer_id)
    if customer is None or customer.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="customer_id is not a customer in your firm")
    warehouse = stock_service.owned_warehouse(db, payload.warehouse_id, org_id)
    if warehouse is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="warehouse_id is not a warehouse in your firm")

    _order_no = _next_order_number(db, org_id)
    order = SalesOrder(
        organization_id=org_id,
        order_number=_order_no,
        # The sheet calls it Sales Order Number; same value, kept in its own
        # column so either name works.
        sales_order_number=_order_no,
        customer_id=customer.id,
        order_date=payload.order_date or datetime.now(timezone.utc),
        salesperson_id=payload.salesperson_id,
        order_status=payload.order_status or "Draft",
        # Placed straight away — an Admin is not a step in every sale. A firm that
        # turns order_requires_approval on gets the old awaiting-approval gate.
        status="awaiting_approval" if settings["order_requires_approval"] else "placed",
        fulfilment_status="not_started",
        warehouse_id=warehouse.id,
        quotation_id=payload.quotation_id,
        delivery_date=payload.delivery_date,
        fulfilment_method=payload.fulfilment_method,
        payment_type=payload.payment_type,
        payment_terms_days=payload.payment_terms_days,
        source=payload.source,
        created_by=user.id,
        discount=payload.discount,
        tax=payload.tax,
        notes=payload.notes,
    )

    subtotal = 0.0
    line_tax = 0.0
    wanted: list[dict] = []
    for it in payload.items:
        product = db.get(Product, it.product_id)
        if product is None or product.organization_id != org_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="An item's product is not in your firm")
        variant = None
        if it.variant_id:
            variant = db.get(ProductVariant, it.variant_id)
            if variant is None or variant.product_id != product.id:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="An item's variant is invalid")
        unit_price = it.unit_price if it.unit_price is not None else (variant.price if variant else product.price)
        line_total = round(unit_price * it.quantity - it.discount, 2)
        # The line's own rate, else the product's. Never a hardcoded figure — an
        # invoice raised later bills this snapshot.
        rate = it.tax_rate if it.tax_rate is not None else product.tax_rate
        tax_amount = round(line_total * (rate or 0) / 100, 2)
        subtotal += line_total
        line_tax += tax_amount
        order.items.append(
            SalesOrderItem(
                product_id=product.id,
                variant_id=it.variant_id,
                product_name=product.name if not variant else f"{product.name} ({variant.name})",
                quantity=it.quantity,
                unit_price=unit_price,
                discount=it.discount,
                tax_rate=rate,
                tax_amount=tax_amount,
                line_total=line_total,
            )
        )
        wanted.append({
            "product_id": product.id,
            "variant_id": it.variant_id,
            "quantity": it.quantity,
            "product_name": product.name,
        })

    # Can the warehouse actually cover it? `available` is on-hand less what other
    # orders already hold, so this is the check that stops overselling.
    if not settings["allow_backorder"]:
        short = stock_service.shortages(db, warehouse.id, wanted)
        if short:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "INSUFFICIENT_STOCK", "shortages": short},
            )

    order.subtotal = round(subtotal, 2)
    # An order-level tax overrides the per-line total when one is sent, so callers
    # written against the old flat `tax` field keep working.
    order.tax = payload.tax if payload.tax else round(line_tax, 2)
    order.total = round(subtotal - payload.discount + order.tax, 2)

    warnings = []
    credit = _credit_warning(db, customer, order.total, settings["credit_limit_action"])
    if credit:
        warnings.append(credit)

    db.add(order)
    db.flush()

    # Hold the stock. Nothing physical moves — on-hand only drops when a vehicle is
    # loaded, so cancelling this order is a release, not an invented stock-in.
    if settings["reserve_stock_on_order"]:
        for reservation in stock_service.reserve_for_order(db, order, warehouse.id):
            item = db.get(SalesOrderItem, reservation.order_item_id)
            if item is not None:
                item.reserved_quantity = reservation.reserved_quantity
        order.fulfilment_status = "reserved"

    notification_service.notify_org_admins(
        db, org_id, "New sales order", f"{order.order_number} — Rs {order.total:,.2f}",
        type="order", link=order.id)
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

    Assigning somebody is planning, not dispatch: the order becomes `planned`, not
    in transit. Goods go in transit when a vehicle has actually been loaded and the
    delivery is dispatched.
    """
    org_id = _org_id(user)
    order = _owned(db, order_id, org_id)
    if order.status == "cancelled" or order.fulfilment_status == "delivered":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot assign delivery on a '{order.status}' order",
        )
    partner = db.get(User, payload.delivery_partner_id)
    if partner is None or partner.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="delivery_partner_id is not a user in your firm")
    order.assigned_delivery_partner_id = partner.id
    if order.status == "placed":
        order.status = "processing"
    if order.fulfilment_status in (None, "not_started", "reserved"):
        order.fulfilment_status = "planned"
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
