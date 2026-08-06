from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.models import (
    Customer,
    Product,
    ProductVariant,
    SalesOrder,
    SalesOrderItem,
    StockMovement,
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
from app.services import notification_service, numbering_service

router = APIRouter(prefix="/orders", tags=["sales_orders"])

_view = require_permission("sales_orders", "view")
_create = require_permission("sales_orders", "create")
_approve = require_permission("sales_orders", "approve")
_edit = require_permission("sales_orders", "edit")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account")
    return user.organization_id


def _owned(db: Session, order_id: str, org_id: str) -> SalesOrder:
    order = db.get(SalesOrder, order_id)
    if order is None or order.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return order


def _next_order_number(db: Session, org_id: str) -> str:
    # max+1, not count+1: counting reissues a number after any deletion.
    return numbering_service.next_number(db, org_id, SalesOrder.order_number, "SO")


def _stock_target(db: Session, item: SalesOrderItem) -> tuple[object, int]:
    """Return (record, current_stock) for the item's variant or product."""
    if item.variant_id:
        variant = db.get(ProductVariant, item.variant_id)
        return variant, (variant.inventory if variant else 0)
    product = db.get(Product, item.product_id) if item.product_id else None
    return product, (product.total_inventory if product else 0)


def _move_stock(db: Session, order: SalesOrder, sign: int, movement_type: str, user_id: str | None) -> None:
    """Apply stock change for all items (sign -1 to deduct on approve, +1 to restore on cancel)."""
    for item in order.items:
        target, current = _stock_target(db, item)
        if target is None:
            continue  # product/variant was deleted — skip
        new_stock = current + sign * item.quantity
        if new_stock < 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Insufficient stock for {item.product_name}",
            )
        if item.variant_id:
            target.inventory = new_stock
        else:
            target.total_inventory = new_stock
        db.add(
            StockMovement(
                organization_id=order.organization_id,
                product_id=item.product_id,
                variant_id=item.variant_id,
                movement_type=movement_type,
                quantity=sign * item.quantity,
                balance_after=new_stock,
                note=f"Order {order.order_number}",
                created_by=user_id,
            )
        )


@router.get("", response_model=list[OrderOut])
def list_orders(
    user: User = Depends(_view),
    status_filter: str | None = Query(default=None, alias="status"),
    customer_id: str | None = Query(default=None),
    assigned_delivery_partner_id: str | None = Query(default=None),
    search: str | None = Query(default=None, description="matches order_number"),
    db: Session = Depends(get_db),
) -> list[SalesOrder]:
    org_id = _org_id(user)
    query = db.query(SalesOrder).filter(SalesOrder.organization_id == org_id)
    if status_filter:
        query = query.filter(SalesOrder.status == status_filter)
    if customer_id:
        query = query.filter(SalesOrder.customer_id == customer_id)
    if assigned_delivery_partner_id:
        query = query.filter(SalesOrder.assigned_delivery_partner_id == assigned_delivery_partner_id)
    if search:
        query = query.filter(SalesOrder.order_number.ilike(f"%{search}%"))
    return query.order_by(SalesOrder.created_at.desc()).all()


@router.get("/{order_id}", response_model=OrderOut)
def get_order(order_id: str, user: User = Depends(_view), db: Session = Depends(get_db)) -> SalesOrder:
    return _owned(db, order_id, _org_id(user))


@router.post("", response_model=OrderOut, status_code=status.HTTP_201_CREATED)
def create_order(
    payload: OrderCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> SalesOrder:
    org_id = _org_id(user)
    customer = db.get(Customer, payload.customer_id)
    if customer is None or customer.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="customer_id is not a customer in your firm")

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
        status="pending",
        source=payload.source,
        created_by=user.id,
        discount=payload.discount,
        tax=payload.tax,
        notes=payload.notes,
    )

    subtotal = 0.0
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
        subtotal += line_total
        order.items.append(
            SalesOrderItem(
                product_id=product.id,
                variant_id=it.variant_id,
                product_name=product.name if not variant else f"{product.name} ({variant.name})",
                quantity=it.quantity,
                unit_price=unit_price,
                discount=it.discount,
                line_total=line_total,
            )
        )

    order.subtotal = round(subtotal, 2)
    order.total = round(subtotal - payload.discount + payload.tax, 2)
    db.add(order)
    db.flush()
    notification_service.notify_org_admins(
        db, org_id, "New sales order", f"{order.order_number} — Rs {order.total:,.2f}",
        type="order", link=order.id)
    db.commit()
    db.refresh(order)
    return order


@router.patch("/{order_id}/approve", response_model=OrderOut)
def approve_order(
    order_id: str,
    user: User = Depends(_approve),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> SalesOrder:
    """Approve a pending order → confirmed, and deduct stock (records sale_out movements)."""
    order = _owned(db, order_id, _org_id(user))
    if order.status != "pending":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Only pending orders can be approved (this is '{order.status}')")
    _move_stock(db, order, sign=-1, movement_type="sale_out", user_id=user.id)
    order.status = "confirmed"
    order.stock_deducted = True
    order.approved_at = datetime.now(timezone.utc)
    # Add to the customer's receivables (billed).
    if order.customer_id:
        customer = db.get(Customer, order.customer_id)
        if customer:
            customer.total_billed = round((customer.total_billed or 0) + order.total, 2)
            customer.recompute_outstanding()
    db.commit()
    db.refresh(order)
    return order


@router.patch("/{order_id}/reject", response_model=OrderOut)
def reject_order(
    order_id: str,
    payload: RejectBody,
    user: User = Depends(_approve),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> SalesOrder:
    order = _owned(db, order_id, _org_id(user))
    if order.status != "pending":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only pending orders can be rejected")
    order.status = "rejected"
    order.reject_reason = payload.reason
    db.commit()
    db.refresh(order)
    return order


@router.patch("/{order_id}/assign-delivery-partner", response_model=OrderOut)
def assign_delivery_partner(
    order_id: str,
    payload: AssignDeliveryBody,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> SalesOrder:
    org_id = _org_id(user)
    order = _owned(db, order_id, org_id)
    if order.status in ("cancelled", "rejected", "delivered"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Cannot assign delivery on a '{order.status}' order")
    partner = db.get(User, payload.delivery_partner_id)
    if partner is None or partner.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="delivery_partner_id is not a user in your firm")
    order.assigned_delivery_partner_id = partner.id
    order.status = "out_for_delivery"
    db.commit()
    db.refresh(order)
    return order


@router.patch("/{order_id}/cancel", response_model=OrderOut)
def cancel_order(
    order_id: str,
    payload: CancelBody,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> SalesOrder:
    """Cancel an order. If stock was already deducted (approved), restore it."""
    order = _owned(db, order_id, _org_id(user))
    if order.status in ("cancelled", "delivered", "returned"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Cannot cancel a '{order.status}' order")
    if order.stock_deducted:
        _move_stock(db, order, sign=+1, movement_type="sales_return", user_id=user.id)
        order.stock_deducted = False
        # Reverse the customer's receivable for this order.
        if order.customer_id:
            customer = db.get(Customer, order.customer_id)
            if customer:
                customer.total_billed = round((customer.total_billed or 0) - order.total, 2)
                customer.recompute_outstanding()
    order.status = "cancelled"
    if payload.reason:
        order.reject_reason = payload.reason
    db.commit()
    db.refresh(order)
    return order
