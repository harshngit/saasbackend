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

from app.core import scoping, workflow
from app.models import Customer, Delivery, Invoice, Product, ProductVariant, SalesOrder, SalesOrderItem, User
from app.schemas.sales_order import OrderUpdate
from app.services import notification_service, numbering_service, stock_service


class OrderLine:
    """One line to place, however the caller described it."""

    __slots__ = (
        "product_id",
        "variant_id",
        "quantity",
        "unit_price",
        "discount",
        "discount_percent",
        "tax_rate",
        "uom",
        "cost_price",
    )

    def __init__(
        self,
        product_id: str,
        quantity: float,
        variant_id: str | None = None,
        unit_price: float | None = None,
        discount: float = 0,
        discount_percent: float | None = 0,
        tax_rate: float | None = None,
        uom: str | None = None,
        cost_price: float | None = None,
    ) -> None:
        self.product_id = product_id
        self.variant_id = variant_id
        self.quantity = quantity
        self.unit_price = unit_price
        self.discount = discount or 0
        self.discount_percent = discount_percent or 0
        self.tax_rate = tax_rate
        self.uom = uom
        self.cost_price = cost_price


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
    billing_address: str | None = None,
    shipping_address: str | None = None,
    delivery_address: str | None = None,
    payment_terms: str | None = None,
    delivery_terms: str | None = None,
    currency: str | None = "INR",
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
    resolved_shipping = shipping_address or delivery_address
    resolved_source = source
    if quotation_id and source in ("office", "direct"):
        resolved_source = "quotation"
    elif not quotation_id and source == "office":
        resolved_source = "direct"

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
        status="draft" if create_as_draft else "placed",
        fulfilment_status="not_started",
        warehouse_id=warehouse.id,
        quotation_id=quotation_id,
        delivery_date=delivery_date,
        fulfilment_method=fulfilment_method,
        payment_type=payment_type,
        payment_terms_days=payment_terms_days,
        source=resolved_source,
        created_by=user.id,
        discount=order_level_discount,
        notes=notes,
        billing_address=billing_address,
        shipping_address=resolved_shipping,
        delivery_address=resolved_shipping,
        payment_terms=payment_terms,
        delivery_terms=delivery_terms,
        currency=currency or "INR",
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
        gross = unit_price * line.quantity
        disc_amount = line.discount or 0
        disc_pct = getattr(line, "discount_percent", None) or 0
        if disc_amount == 0 and disc_pct > 0:
            disc_amount = round(gross * disc_pct / 100, 2)
        line_total = round(gross - disc_amount, 2)
        # The line's own rate, else the product's. Never a hardcoded figure — an
        # invoice raised later bills this snapshot.
        rate = line.tax_rate if line.tax_rate is not None else product.tax_rate
        tax_amount = round(line_total * (rate or 0) / 100, 2)
        subtotal += line_total
        line_tax += tax_amount
        cost_price = (
            line.cost_price
            if getattr(line, "cost_price", None) is not None
            else (product.pricing.purchase_price if product.pricing else 0.0)
        )
        uom = getattr(line, "uom", None) or product.uom
        order.items.append(
            SalesOrderItem(
                product_id=product.id,
                variant_id=line.variant_id,
                product_name=product.name if not variant else f"{product.name} ({variant.name})",
                quantity=line.quantity,
                unit_price=unit_price,
                discount=disc_amount,
                discount_percent=disc_pct,
                cost_price=cost_price,
                uom=uom,
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
    if not create_as_draft and warehouse.id and wanted:
        stock_service.lock_stock_items(db, org_id, warehouse.id, wanted)

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

    # Acquire deterministic row-level locks before checking shortages/reserving
    if order.warehouse_id and wanted:
        stock_service.lock_stock_items(db, org_id, order.warehouse_id, wanted)

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
    order.status = "placed"

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


def update_order(
    db: Session, user: User, order: SalesOrder, payload: OrderUpdate
) -> tuple[SalesOrder, list[str]]:
    """Update a sales order before fulfillment/dispatch.

    Does not commit — caller owns the transaction.
    """
    org_id = order.organization_id
    settings = workflow.sales_settings(user.organization)

    # 1. Status / workflow restrictions
    if order.status in ("completed", "cancelled", "rejected"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot edit an order with status '{order.status}'",
        )
    if order.fulfilment_status in workflow.DISPATCHED_FULFILMENT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot edit order when fulfilment status is '{order.fulfilment_status}'",
        )
    if order.stock_deducted:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot edit order after stock has been deducted",
        )

    # Check if an invoice has already been issued for this order
    inv = (
        db.query(Invoice)
        .filter(
            Invoice.order_id == order.id,
            Invoice.organization_id == org_id,
            Invoice.is_credit_note.is_(False),
        )
        .first()
    )
    if inv:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot edit order because invoice {inv.invoice_number} has already been issued",
        )

    # If updating line items, check that active deliveries don't exist
    if payload.items is not None:
        active_deliveries = (
            db.query(Delivery)
            .filter(
                Delivery.sales_order_id == order.id,
                Delivery.status.in_(workflow.OPEN_DELIVERY_STATUSES),
            )
            .all()
        )
        if active_deliveries:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot edit line items while open deliveries exist for this order. Please cancel or update deliveries first.",
            )

    # 2. Update customer if provided
    if payload.customer_id is not None and payload.customer_id != order.customer_id:
        customer = db.get(Customer, payload.customer_id)
        if customer is None or customer.organization_id != org_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="customer_id is not a customer in your firm",
            )
        order.customer_id = customer.id
    else:
        customer = db.get(Customer, order.customer_id) if order.customer_id else None

    # 3. Update warehouse if provided
    warehouse_changed = False
    if payload.warehouse_id is not None and payload.warehouse_id != order.warehouse_id:
        wh = stock_service.owned_warehouse(db, payload.warehouse_id, org_id)
        if wh is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="warehouse_id is not a warehouse in your firm",
            )
        order.warehouse_id = wh.id
        warehouse_changed = True

    # 4. Update header fields if provided
    if payload.quotation_id is not None:
        order.quotation_id = payload.quotation_id or None
    if payload.delivery_date is not None:
        order.delivery_date = payload.delivery_date
    if payload.order_date is not None:
        order.order_date = payload.order_date
    if payload.fulfilment_method is not None:
        order.fulfilment_method = payload.fulfilment_method
    if payload.payment_type is not None:
        order.payment_type = payload.payment_type
    if payload.payment_terms_days is not None:
        order.payment_terms_days = payload.payment_terms_days
    if payload.payment_terms is not None:
        order.payment_terms = payload.payment_terms
    if payload.delivery_terms is not None:
        order.delivery_terms = payload.delivery_terms
    if payload.currency is not None:
        order.currency = payload.currency
    if payload.billing_address is not None:
        order.billing_address = payload.billing_address
    if payload.shipping_address is not None or payload.delivery_address is not None:
        resolved_shipping = payload.shipping_address or payload.delivery_address
        order.shipping_address = resolved_shipping
        order.delivery_address = resolved_shipping
    if payload.source is not None:
        order.source = payload.source
    if payload.notes is not None:
        order.notes = payload.notes
    if payload.order_status is not None:
        order.order_status = payload.order_status

    # Salesperson handling (enforcing own-scope)
    if scoping.scope_to_own(db, user):
        order.salesperson_id = user.id
    elif payload.salesperson_id is not None:
        if payload.salesperson_id:
            sp = db.get(User, payload.salesperson_id)
            if sp is None or sp.organization_id != org_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="salesperson_id is not a user in your firm",
                )
            order.salesperson_id = sp.id
        else:
            order.salesperson_id = None

    # 5. Line items update (if provided)
    if payload.items is not None:
        if len(payload.items) == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Order must contain at least one item",
            )

        # Acquire deterministic row-level locks on all affected items (both old and new)
        if order.status != "draft" and order.warehouse_id:
            all_affected_items = [
                (it.product_id, it.variant_id) for it in order.items if it.product_id
            ] + [
                (line.product_id, line.variant_id) for line in payload.items if line.product_id
            ]
            stock_service.lock_stock_items(db, org_id, order.warehouse_id, all_affected_items)

        # Release existing reservations before item changes
        if order.status != "draft":
            stock_service.release_for_order(db, order.id)

        order.items.clear()
        db.flush()

        subtotal = 0.0
        line_tax = 0.0
        wanted: list[dict] = []
        for line in payload.items:
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
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="An item's variant is invalid",
                    )
            unit_price = (
                line.unit_price
                if line.unit_price is not None
                else (variant.price if variant else product.price)
            )
            gross = unit_price * line.quantity
            disc_amount = line.discount or 0
            disc_pct = getattr(line, "discount_percent", None) or 0
            if disc_amount == 0 and disc_pct > 0:
                disc_amount = round(gross * disc_pct / 100, 2)
            line_total = round(gross - disc_amount, 2)
            rate = line.tax_rate if line.tax_rate is not None else product.tax_rate
            tax_amount = round(line_total * (rate or 0) / 100, 2)
            subtotal += line_total
            line_tax += tax_amount
            cost_price = (
                line.cost_price
                if getattr(line, "cost_price", None) is not None
                else (product.pricing.purchase_price if product.pricing else 0.0)
            )
            uom = getattr(line, "uom", None) or product.uom
            order.items.append(
                SalesOrderItem(
                    product_id=product.id,
                    variant_id=line.variant_id,
                    product_name=product.name if not variant else f"{product.name} ({variant.name})",
                    quantity=line.quantity,
                    unit_price=unit_price,
                    discount=disc_amount,
                    discount_percent=disc_pct,
                    cost_price=cost_price,
                    uom=uom,
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

        # Check stock shortages if order is already active (not draft) and no backorders
        if order.status != "draft" and not settings["allow_backorder"] and order.warehouse_id:
            short = stock_service.shortages(db, order.warehouse_id, wanted)
            if short:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "INSUFFICIENT_STOCK", "shortages": short},
                )

        order.subtotal = round(subtotal, 2)
        if payload.discount is not None:
            order.discount = payload.discount
        if payload.tax is not None:
            order.tax = payload.tax
        else:
            order.tax = round(line_tax, 2)
        order.total = round(order.subtotal - order.discount + order.tax, 2)

        db.flush()

        # Re-reserve stock if order is active
        if order.status != "draft" and settings["reserve_stock_on_order"] and order.warehouse_id:
            for reservation in stock_service.reserve_for_order(db, order, order.warehouse_id):
                item = db.get(SalesOrderItem, reservation.order_item_id)
                if item is not None:
                    item.reserved_quantity = reservation.reserved_quantity
            order.fulfilment_status = "reserved"

    else:
        # Items were not replaced, but discount / tax or warehouse might have changed
        recalculate = False
        if payload.discount is not None:
            order.discount = payload.discount
            recalculate = True
        if payload.tax is not None:
            order.tax = payload.tax
            recalculate = True
        if recalculate:
            order.total = round(order.subtotal - order.discount + order.tax, 2)

        if warehouse_changed and order.status != "draft" and order.warehouse_id:
            wanted = [
                {
                    "product_id": it.product_id,
                    "variant_id": it.variant_id,
                    "quantity": it.quantity,
                    "product_name": it.product_name,
                }
                for it in order.items
                if it.product_id
            ]
            stock_service.lock_stock_items(db, org_id, order.warehouse_id, wanted)
            stock_service.release_for_order(db, order.id)
            if not settings["allow_backorder"]:
                short = stock_service.shortages(db, order.warehouse_id, wanted)
                if short:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail={"error": "INSUFFICIENT_STOCK", "shortages": short},
                    )
            if settings["reserve_stock_on_order"]:
                for reservation in stock_service.reserve_for_order(db, order, order.warehouse_id):
                    item = db.get(SalesOrderItem, reservation.order_item_id)
                    if item is not None:
                        item.reserved_quantity = reservation.reserved_quantity
                order.fulfilment_status = "reserved"

    order.updated_at = datetime.now(timezone.utc)

    warnings = []
    if order.status != "draft" and customer:
        credit = credit_warning(db, customer, order.total, settings["credit_limit_action"])
        if credit:
            warnings.append(credit)

    return order, warnings