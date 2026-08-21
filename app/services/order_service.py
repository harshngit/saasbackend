"""Placing a sales order — the one path, wherever the order comes from.

`POST /orders` and `POST /quotations/{id}/convert-to-order` both land here, so a
converted quotation is validated, priced, reserved and placed exactly like an order
typed in by hand. Anything that changes about placing an order changes in one place.

The flow, and what it deliberately does not do:

    validate customer, products, warehouse
      → check available stock (unless the firm allows backorders)
      → create the order and its items, each snapshotting its own tax rate
      → reserve the stock
      → status = placed (or awaiting_approval, if the firm asks for approval)

No warehouse deduction: on-hand only moves when a vehicle is loaded. No receivable:
that starts at the invoice. No Admin approval unless the firm turned it on.
"""

from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core import workflow
from app.models import Customer, Product, ProductVariant, SalesOrder, SalesOrderItem, User
from app.services import notification_service, numbering_service, stock_service


class OrderLine:
    """One line to place, however the caller described it."""

    __slots__ = ("product_id", "variant_id", "quantity", "unit_price", "discount", "tax_rate")

    def __init__(
        self,
        product_id: str,
        quantity: float,
        variant_id: str | None = None,
        unit_price: float | None = None,
        discount: float = 0,
        tax_rate: float | None = None,
    ) -> None:
        self.product_id = product_id
        self.variant_id = variant_id
        self.quantity = quantity
        self.unit_price = unit_price
        self.discount = discount or 0
        self.tax_rate = tax_rate


def next_order_number(db: Session, org_id: str) -> str:
    # max+1, not count+1: counting reissues a number after any deletion.
    return numbering_service.next_number(db, org_id, SalesOrder.order_number, "SO")


def credit_warning(
    db: Session, customer: Customer, order_total: float, action: str
) -> str | None:
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


def place_order(
    db: Session,
    user: User,
    customer: Customer,
    lines: list[OrderLine],
    warehouse_id: str | None = None,
    order_date: datetime | None = None,
    delivery_date: datetime | None = None,
    fulfilment_method: str | None = None,
    payment_type: str | None = None,
    payment_terms_days: int | None = None,
    salesperson_id: str | None = None,
    quotation_id: str | None = None,
    source: str = "office",
    order_level_discount: float = 0,
    order_level_tax: float = 0,
    notes: str | None = None,
    order_status_label: str | None = None,
    create_as_draft: bool = False,
) -> tuple[SalesOrder, list[str]]:
    """Validate, price, reserve and place. Returns the order and any warnings.

    If `create_as_draft` is True this creates a draft order: products and prices
    are validated and snapshot, but no stock shortage check or reservation is
    performed, no credit warnings generated and no admin notification sent.

    Does not commit — the caller owns the transaction, so converting a quotation can
    mark it converted in the same one.
    """
    org_id = customer.organization_id
    settings = workflow.sales_settings(user.organization)

    warehouse = stock_service.owned_warehouse(db, warehouse_id, org_id)
    if warehouse is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="warehouse_id is not a warehouse in your firm",
        )

    number = next_order_number(db, org_id)
    order = SalesOrder(
        organization_id=org_id,
        order_number=number,
        # The sheet calls it Sales Order Number; same value, kept in its own column
        # so either name works.
        sales_order_number=number,
        customer_id=customer.id,
        order_date=order_date or datetime.now(timezone.utc),
        salesperson_id=salesperson_id,
        order_status=order_status_label or "Draft",
        # If creating as a draft, set the lifecycle to draft and do not reserve.
        # Otherwise preserve existing behaviour: placed or awaiting_approval.
        status="draft" if create_as_draft else ("awaiting_approval" if settings["order_requires_approval"] else "placed"),
        fulfilment_status="not_started",
        warehouse_id=warehouse.id,
        quotation_id=quotation_id,
        delivery_date=delivery_date,
        fulfilment_method=fulfilment_method,
        payment_type=payment_type,
        payment_terms_days=payment_terms_days,
        source=source,
        created_by=user.id,
        discount=order_level_discount,
        notes=notes,
    )

    subtotal = 0.0
    line_tax = 0.0
    wanted: list[dict] = []
    for line in lines:
        product = db.get(Product, line.product_id)
        if product is None or product.organization_id != org_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="An item's product is not in your firm",
            )
        variant = None
        if line.variant_id:
            variant = db.get(ProductVariant, line.variant_id)
            if variant is None or variant.product_id != product.id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail="An item's variant is invalid"
                )
        unit_price = (
            line.unit_price
            if line.unit_price is not None
            else (variant.price if variant else product.price)
        )
        line_total = round(unit_price * line.quantity - line.discount, 2)
        # The line's own rate, else the product's. Never a hardcoded figure — an
        # invoice raised later bills this snapshot.
        rate = line.tax_rate if line.tax_rate is not None else product.tax_rate
        tax_amount = round(line_total * (rate or 0) / 100, 2)
        subtotal += line_total
        line_tax += tax_amount
        order.items.append(
            SalesOrderItem(
                product_id=product.id,
                variant_id=line.variant_id,
                product_name=product.name if not variant else f"{product.name} ({variant.name})",
                quantity=line.quantity,
                unit_price=unit_price,
                discount=line.discount,
                tax_rate=rate,
                tax_amount=tax_amount,
                line_total=line_total,
            )
        )
        wanted.append(
            {
                "product_id": product.id,
                "variant_id": line.variant_id,
                "quantity": line.quantity,
                "product_name": product.name,
            }
        )

    # Can the warehouse actually cover it? When creating a draft we skip this
    # check; it will run at confirm time.
    if not create_as_draft and not settings["allow_backorder"]:
        short = stock_service.shortages(db, warehouse.id, wanted)
        if short:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "INSUFFICIENT_STOCK", "shortages": short},
            )

    order.subtotal = round(subtotal, 2)
    # An order-level tax overrides the per-line total when one is sent, so callers
    # written against the old flat `tax` field keep working.
    order.tax = order_level_tax if order_level_tax else round(line_tax, 2)
    order.total = round(order.subtotal - order_level_discount + order.tax, 2)

    warnings = []
    # Credit warnings are deferred for drafts until confirm time.
    if not create_as_draft:
        credit = credit_warning(db, customer, order.total, settings["credit_limit_action"])
        if credit:
            warnings.append(credit)

    db.add(order)
    db.flush()

    # Hold the stock. For drafts we skip reservation until confirm time.
    if not create_as_draft and settings["reserve_stock_on_order"]:
        for reservation in stock_service.reserve_for_order(db, order, warehouse.id):
            item = db.get(SalesOrderItem, reservation.order_item_id)
            if item is not None:
                item.reserved_quantity = reservation.reserved_quantity
        order.fulfilment_status = "reserved"

    # Notify admins only for actual placed/awaiting orders, not drafts.
    if not create_as_draft:
        notification_service.notify_org_admins(
            db, org_id, "New sales order", f"{order.order_number} — Rs {order.total:,.2f}",
            type="order", link=order.id,
        )
    return order, warnings


def confirm_order(db: Session, user: User, order: SalesOrder) -> tuple[SalesOrder, list[str]]:
    """Confirm a previously-created draft order: validate, check shortages,
    reserve stock, set lifecycle status and generate warnings.

    Does not commit.
    """
    org_id = order.organization_id
    settings = workflow.sales_settings(user.organization)
    if order.status != "draft":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Only a draft order may be confirmed (this is '{order.status}')"
        )

    # Re-validate customer
    customer = db.get(Customer, order.customer_id) if order.customer_id else None
    if customer is None or customer.organization_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Order's customer not found in your firm"
        )

    # Re-validate products/variants and build wanted list
    wanted: list[dict] = []
    for item in order.items:
        product = db.get(Product, item.product_id)
        if product is None or product.organization_id != org_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="An item's product is not in your firm"
            )
        if item.variant_id:
            variant = db.get(ProductVariant, item.variant_id)
            if variant is None or variant.product_id != product.id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="An item's variant is invalid"
                )
        wanted.append({
            "product_id": item.product_id,
            "variant_id": item.variant_id,
            "quantity": item.quantity,
            "product_name": product.name,
        })

    # Stock shortages
    if not settings["allow_backorder"]:
        short = stock_service.shortages(db, order.warehouse_id, wanted)
        if short:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "INSUFFICIENT_STOCK", "shortages": short}
            )

    # Reserve
    if settings["reserve_stock_on_order"]:
        for reservation in stock_service.reserve_for_order(db, order, order.warehouse_id):
            item = db.get(SalesOrderItem, reservation.order_item_id)
            if item is not None:
                item.reserved_quantity = reservation.reserved_quantity
        order.fulfilment_status = "reserved"

    # Lifecycle status
    order.status = "awaiting_approval" if settings["order_requires_approval"] else "placed"

    # Credit warnings
    warnings: list[str] = []
    credit = credit_warning(db, customer, order.total, settings["credit_limit_action"])
    if credit:
        warnings.append(credit)

    # Notify admins
    notification_service.notify_org_admins(
        db, org_id, "Sales order confirmed", f"{order.order_number} — Rs {order.total:,.2f}",
        type="order", link=order.id,
    )
    return order, warnings