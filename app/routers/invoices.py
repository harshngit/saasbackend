from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core import workflow
from app.core.deps import require_permission, require_unlocked_org
from app.core.pdf_docs import invoice_detailed_pdf, invoice_simple_pdf
from app.models import (
    Customer,
    Delivery,
    Invoice,
    InvoiceItem,
    Product,
    ProductVariant,
    SalesOrder,
    StoredFile,
    User,
)
from app.services.tracking_service import TrackingError
from app.services import (
    lookup_service,
    numbering_service,
    payment_service,
    return_service,
    stock_service,
)
from app.schemas.invoice import (
    CreditNoteBody,
    InvoiceCreate,
    InvoiceFromDelivery,
    InvoiceOut,
)

router = APIRouter(prefix="/invoices", tags=["invoices"])
# "Invoice this order" reads better hanging off the order it belongs to, so the
# same handler is served at POST /orders/{order_id}/invoice as well.
orders_router = APIRouter(prefix="/orders", tags=["invoices"])

_view = require_permission("invoices", "view")
_create = require_permission("invoices", "create")
_edit = require_permission("invoices", "edit")
_approve = require_permission("invoices", "approve")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account")
    return user.organization_id


def _owned(db: Session, id: str, org_id: str) -> Invoice:
    """Accepts the UUID or the human-facing code (invoice_number, sales_id)."""
    record = lookup_service.by_id_or_code(
        db, Invoice, id, org_id, Invoice.invoice_number, Invoice.sales_id
    )
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")
    return record


def _hsn_for(db: Session, product_id: str | None) -> str | None:
    """The product's HSN/SAC, copied onto the line at invoicing time."""
    if not product_id:
        return None
    product = db.get(Product, product_id)
    return product.hsn_code if product else None


def _next_invoice_number(db: Session, org_id: str) -> str:
    # max+1, not count+1: counting reissues a number after any deletion.
    return numbering_service.next_number(db, org_id, Invoice.invoice_number, "INV")


def _due_date(order: SalesOrder | None, issued: datetime) -> datetime | None:
    """When payment falls due, from the order's agreed terms."""
    if order is None or order.payment_terms_days is None:
        return None
    return issued + timedelta(days=order.payment_terms_days)


def _billable_lines(db: Session, order: SalesOrder, delivery: Delivery | None) -> list[dict]:
    """What to bill: a delivery's delivered quantities, or the whole order as ordered.

    Rates, discounts and tax rates always come from the order line — the customer is
    billed what they agreed, at the quantity they actually received.
    """
    if delivery is None:
        return [
            {
                "order_item": item,
                "delivery_item_id": None,
                "quantity": item.quantity,
            }
            for item in order.items
        ]

    by_order_item = {item.id: item for item in order.items}
    lines = []
    for line in delivery.items:
        if not line.delivered_quantity:
            continue
        order_item = by_order_item.get(line.order_item_id)
        if order_item is None:
            continue
        already = sum(
            (row.quantity or 0)
            for row in db.query(InvoiceItem)
            .join(Invoice, InvoiceItem.invoice_id == Invoice.id)
            .filter(
                InvoiceItem.delivery_item_id == line.id,
                Invoice.is_credit_note.is_(False),
            )
        )
        outstanding = round((line.delivered_quantity or 0) - already, 3)
        if outstanding <= 0:
            continue
        lines.append(
            {"order_item": order_item, "delivery_item_id": line.id, "quantity": outstanding}
        )
    return lines


# Also exposed as POST /orders/{order_id}/invoice — the route decorator returns the
# function untouched, so stacking registers the one handler on both routers.
@orders_router.post("/{order_id}/invoice", response_model=InvoiceOut, status_code=status.HTTP_201_CREATED)
@router.post("/orders/{order_id}/invoice", response_model=InvoiceOut, status_code=status.HTTP_201_CREATED)
def generate_from_order(
    order_id: str,
    payload: InvoiceFromDelivery | None = None,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Invoice:
    """Invoice a sales order.

    Send `{"delivery_id": "del_001"}` and the invoice bills **what that delivery
    actually handed over** — 15 of 20 delivered means an invoice for 15 — at the rates,
    discounts and tax rates the order line agreed. Line by line it points back at the
    delivery item it bills, and a delivery already billed cannot be billed twice.

    Send no body to bill the whole order as ordered, which is what an order with no
    delivery behind it needs.

    For a part-delivered order the firm's `partial_delivery_invoice_mode` decides:
    `per_delivery` bills each delivery as it happens (several invoices per order),
    `after_full_order` waits until everything has been delivered and bills once.

    Tax comes from the snapshot on the order line — the line's own rate, else the
    product's. Nothing is hardcoded.
    """
    org_id = _org_id(user)
    settings = workflow.sales_settings(user.organization)
    order = db.get(SalesOrder, order_id)
    if order is None or order.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sales order not found")

    # An order past approval can be billed; a draft or cancelled one cannot.
    if order.status not in ("placed", "processing", "completed"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot invoice a '{order.status}' order",
        )

    delivery = None
    if payload is not None and payload.delivery_id:
        delivery = db.get(Delivery, payload.delivery_id)
        if delivery is None or delivery.organization_id != org_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="delivery_id is not a delivery in your firm",
            )
        if delivery.sales_order_id != order.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="That delivery belongs to a different order",
            )
        if delivery.delivered_total <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Nothing has been delivered on this delivery yet",
            )
        if (
            settings["partial_delivery_invoice_mode"] == "after_full_order"
            and order.fulfilment_status != "delivered"
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This firm bills once the whole order has been delivered "
                       "(partial_delivery_invoice_mode = after_full_order)",
            )
    else:
        # Billing the whole order: only once, as before.
        existing = (
            db.query(Invoice)
            .filter(Invoice.order_id == order_id, Invoice.is_credit_note.is_(False))
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invoice already generated for this order (Invoice ID: {existing.id})",
            )

    lines = _billable_lines(db, order, delivery)
    if not lines:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Everything delivered on this delivery has already been invoiced",
        )

    issued = datetime.now(timezone.utc)
    invoice = Invoice(
        organization_id=org_id,
        invoice_number=_next_invoice_number(db, org_id),
        order_id=order.id,
        delivery_id=delivery.id if delivery is not None else None,
        sales_id=numbering_service.next_number(db, org_id, Invoice.sales_id, "SALE"),
        customer_id=order.customer_id,
        invoice_date=issued,
        due_date=_due_date(order, issued),
        billing_address=order.customer.billing_address if order.customer else None,
        sales_type="Sales Order",
        sales_date=issued,
        sales_status="Confirmed",
        invoice_status="Issued",
        payment_status="Unpaid",
        status="unpaid",
        amount_paid=0.0,
        notes=order.notes,
        created_by=user.id,
    )

    subtotal = 0.0
    tax_total = 0.0
    for line in lines:
        item = line["order_item"]
        quantity = line["quantity"]
        # Per-unit figures from the order line, applied to the billed quantity, so a
        # part delivery is billed proportionally rather than for the whole line.
        ordered = item.quantity or 1
        share = quantity / ordered
        discount = round((item.discount or 0) * share, 2)
        line_total = round((item.unit_price or 0) * quantity - discount, 2)
        rate = item.tax_rate or 0
        tax_amount = round(line_total * rate / 100, 2)
        subtotal += line_total
        tax_total += tax_amount
        invoice.items.append(
            InvoiceItem(
                product_id=item.product_id,
                variant_id=item.variant_id,
                product_name=item.product_name,
                hsn_code=_hsn_for(db, item.product_id),
                quantity=quantity,
                unit_price=item.unit_price,
                discount=discount,
                tax=tax_amount,
                tax_amount=tax_amount,
                tax_rate=item.tax_rate,
                line_total=line_total,
                delivery_item_id=line["delivery_item_id"],
                order_item_id=item.id,
            )
        )

    if delivery is None:
        # Billing the whole order: carry the order's own totals, so an order-level
        # discount or a flat order-level tax is billed exactly as agreed.
        invoice.subtotal = order.subtotal
        invoice.discount = order.discount
        invoice.tax = order.tax
        invoice.total = order.total
    else:
        # Billing one delivery: only the lines it handed over, with their own taxes.
        # An order-level discount belongs to the order as a whole, not to a part of it.
        invoice.subtotal = round(subtotal, 2)
        invoice.discount = 0
        invoice.tax = round(tax_total, 2)
        invoice.total = round(invoice.subtotal + invoice.tax, 2)

    # The receivable starts here, not when the order was placed — an order is a
    # promise, an invoice is a bill. The direct-invoice route below does the same.
    if order.customer is not None:
        order.customer.total_billed = round(
            (order.customer.total_billed or 0) + invoice.total, 2
        )
        order.customer.recompute_outstanding()

    db.add(invoice)
    db.commit()
    db.refresh(invoice)
    return invoice


@router.post("", response_model=InvoiceOut, status_code=status.HTTP_201_CREATED)
def create_direct_invoice(
    payload: InvoiceCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Invoice:
    """Quick billing — the retail counter, a shop sale, an immediate sale.

    No sales order and no delivery: the goods leave the warehouse as the bill is
    raised, and the invoice, the stock movements and the payment all land in one
    transaction, so a failure anywhere leaves no half-finished sale behind.

    The buyer is either `customer_id` or a `walk_in_customer` block. A walk-in is
    snapshotted onto the invoice — no customer record is created, and an anonymous
    sale that is paid in full opens no receivable ledger at all.

    Tax is the line's `tax_rate` when given, else the product's own rate, so a counter
    sale is taxed exactly like an order for the same goods. Nothing is hardcoded.

    Send `payment` to settle it now and get back a paid invoice with its receipt
    recorded; leave it out for a credit sale, which stands unpaid as a receivable.
    """
    org_id = _org_id(user)

    customer = None
    if payload.customer_id:
        customer = db.get(Customer, payload.customer_id)
        if customer is None or customer.organization_id != org_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="customer_id is not a customer in your firm",
            )
    walk_in = payload.walk_in_customer
    if customer is None and walk_in is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Send customer_id, or a walk_in_customer block for a counter sale",
        )

    warehouse = stock_service.owned_warehouse(db, payload.warehouse_id, org_id)
    if payload.warehouse_id and warehouse is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="warehouse_id is not a warehouse in your firm",
        )
    if warehouse is None:
        warehouse = stock_service.default_warehouse(db, org_id)

    issued = payload.invoice_date or datetime.now(timezone.utc)
    invoice = Invoice(
        organization_id=org_id,
        invoice_number=_next_invoice_number(db, org_id),
        sales_id=numbering_service.next_number(db, org_id, Invoice.sales_id, "SALE"),
        customer_id=customer.id if customer is not None else None,
        walk_in_name=(walk_in.name if walk_in is not None else None),
        walk_in_phone=(walk_in.mobile_number if walk_in is not None else None),
        invoice_date=issued,
        # The sheet marks Billing Address "copied from related record at creation time".
        billing_address=payload.billing_address or (customer.billing_address if customer else None),
        sales_type=payload.sales_type or "Invoice",
        sales_date=payload.sales_date or issued,
        sales_status=payload.sales_status or "Confirmed",
        invoice_status=payload.invoice_status or "Issued",
        payment_status=payload.payment_status or "Unpaid",
        status="unpaid",
        discount=payload.discount,
        additional_charges=payload.additional_charges,
        round_off=payload.round_off,
        notes=payload.notes,
        created_by=user.id,
    )

    # One pass to resolve and price every line, so nothing moves until the whole
    # basket is known to be valid and in stock.
    lines: list[dict] = []
    subtotal = 0.0
    line_tax_total = 0.0
    for entry in payload.items:
        product = db.get(Product, entry.product_id)
        if product is None or product.organization_id != org_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="An item's product is not in your firm",
            )
        variant = None
        if entry.variant_id:
            variant = db.get(ProductVariant, entry.variant_id)
            if variant is None or variant.product_id != product.id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail="An item's variant is invalid"
                )

        unit_price = entry.unit_price
        if unit_price is None:
            unit_price = (variant.price if variant is not None else None) or product.price or 0
        line_total = round(unit_price * entry.quantity - entry.discount, 2)
        # The line's own rate, else the product's — never a hardcoded percentage.
        rate = entry.tax_rate if entry.tax_rate is not None else product.tax_rate
        if rate is not None:
            tax_amount = round(line_total * rate / 100, 2)
        else:
            tax_amount = round(entry.tax, 2)
        subtotal += line_total
        line_tax_total += tax_amount
        lines.append({
            "product": product, "variant": variant, "quantity": entry.quantity,
            "unit_price": unit_price, "discount": entry.discount, "line_total": line_total,
            "tax_rate": rate, "tax_amount": tax_amount,
            "batch_number": entry.batch_number, "serial_numbers": entry.serial_numbers,
        })

    wanted = [
        {"product_id": line["product"].id,
         "variant_id": line["variant"].id if line["variant"] is not None else None,
         "quantity": line["quantity"],
         "product_name": line["product"].name}
        for line in lines
    ]
    short = stock_service.shortages(db, warehouse.id, wanted)
    if short:
        detail = "; ".join(
            f"{row['product_name']}: {row['available_quantity']} available, "
            f"{row['required_quantity']} needed"
            for row in short
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Not enough stock — {detail}"
        )

    for line in lines:
        variant = line["variant"]
        product = line["product"]
        try:
            _, tracked = stock_service.move_tracked(
                db, org_id, warehouse.id, product.id,
                variant.id if variant is not None else None,
                -line["quantity"],
                movement_type="sale_out",
                note=f"Direct Invoice {invoice.invoice_number}",
                created_by=user.id,
                batch={"batch_number": line["batch_number"]} if line["batch_number"] else None,
                serial_numbers=line["serial_numbers"],
            )
        except TrackingError as exc:
            db.rollback()
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        invoice.items.append(
            InvoiceItem(
                batch_number=tracked["batch_number"],
                expiry_date=tracked["expiry_date"],
                serial_numbers=tracked["serial_numbers"] or None,
                product_id=product.id,
                variant_id=variant.id if variant is not None else None,
                product_name=product.name if variant is None else f"{product.name} ({variant.name})",
                hsn_code=product.hsn_code,
                quantity=line["quantity"],
                unit_price=line["unit_price"],
                discount=line["discount"],
                tax=line["tax_amount"],
                tax_amount=line["tax_amount"],
                tax_rate=line["tax_rate"],
                line_total=line["line_total"],
            )
        )

    invoice.subtotal = round(subtotal, 2)
    # An order-level `tax` in the body is still honoured for older clients; otherwise
    # the tax is what the lines add up to.
    invoice.tax = round(payload.tax, 2) if payload.tax else round(line_tax_total, 2)
    invoice.total = round(
        invoice.subtotal
        - payload.discount
        + invoice.tax
        + (payload.additional_charges or 0)
        + (payload.round_off or 0),
        2,
    )

    # A named customer owes this from now on. An anonymous walk-in has no ledger to
    # open — the invoice itself carries whatever is unpaid.
    if customer is not None:
        customer.total_billed = round((customer.total_billed or 0) + invoice.total, 2)
        customer.recompute_outstanding()

    db.add(invoice)
    db.flush()

    if payload.payment is not None:
        try:
            payment_service.record(
                db, org_id,
                customer=customer,
                invoice=invoice,
                amount=payload.payment.amount,
                payment_mode=payload.payment.payment_method,
                reference=payload.payment.transaction_reference,
                received_on=payload.payment.received_on,
                note=f"Counter payment against {invoice.invoice_number}",
            )
        except ValueError as exc:
            # Nothing is committed yet, so a bad payment takes the whole sale with it
            # rather than leaving a billed-but-unsold invoice behind.
            db.rollback()
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    db.commit()
    db.refresh(invoice)
    return invoice


@router.get("", response_model=list[InvoiceOut])
def list_invoices(
    user: User = Depends(_view),
    customer_id: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
) -> list[Invoice]:
    org_id = _org_id(user)
    q = db.query(Invoice).filter(Invoice.organization_id == org_id)
    if customer_id:
        q = q.filter(Invoice.customer_id == customer_id)
    if status_filter:
        q = q.filter(Invoice.status == status_filter)
    return q.order_by(Invoice.created_at.desc()).all()


@router.get("/{id}", response_model=InvoiceOut)
def get_invoice(id: str, user: User = Depends(_view), db: Session = Depends(get_db)) -> Invoice:
    return _owned(db, id, _org_id(user))


def _invoice_logo(db: Session, org_id: str, settings: dict) -> bytes | None:
    """The logo bytes behind `branding.logo_file_id`, if the firm has uploaded one.

    The setting holds whatever the upload handed back, so both a bare file id and a
    full `/files/{id}` URL resolve. Another firm's file never does.
    """
    reference = (settings.get("branding") or {}).get("logo_file_id")
    if not reference:
        return None
    file_id = str(reference).rstrip("/").rsplit("/", 1)[-1]
    stored = db.get(StoredFile, file_id)
    if stored is None or (stored.organization_id and stored.organization_id != org_id):
        return None
    return stored.data


@router.get("/{id}/pdf")
def download_invoice_pdf(
    id: str,
    user: User = Depends(_view),
    format: str = Query(
        default="detailed",
        pattern="^(simple|detailed)$",
        description="`detailed` is the full tax invoice; `simple` is the short customer copy",
    ),
    db: Session = Depends(get_db),
) -> Response:
    """Download the invoice as a PDF — one record, two formats.

    `format=detailed` prints the statutory tax invoice: company and customer GSTIN,
    billing and shipping address, HSN/SAC, the tax rate and amount per line,
    discount, additional charges, round off, bank and UPI details, terms and a
    signature line.

    `format=simple` prints the short copy for the customer: items, quantity, amount,
    total, what is paid, the balance due, the due date and the payment status.

    Both are rendered from the firm's own `GET /invoice-settings` — its paper size,
    brand colour, logo, terms and footer, and the fifteen show/hide field toggles. A
    field the firm has switched off does not print, so nothing here is hardcoded.
    """
    org_id = _org_id(user)
    invoice = _owned(db, id, org_id)
    settings = workflow.invoice_settings(user.organization)
    builder = invoice_simple_pdf if format == "simple" else invoice_detailed_pdf
    pdf_bytes = builder(
        user.organization,
        invoice.customer,
        invoice,
        settings,
        _invoice_logo(db, org_id, settings),
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition":
                f'attachment; filename="invoice-{invoice.invoice_number}-{format}.pdf"'
        },
    )


@router.post("/{id}/credit-note", response_model=InvoiceOut)
def create_credit_note(
    id: str,
    payload: CreditNoteBody,
    user: User = Depends(_approve),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Invoice:
    """Credit an invoice straight away — the shortcut for goods already back in hand.

    This raises a **credit note of its own**, against this invoice: the original bill
    stays on the account as the debit it always was, and the credit note is the entry
    that cancels it. The customer's outstanding balance falls by the credited amount,
    and the goods go back into the warehouse.

    Send `items` to credit part of the invoice, or nothing to credit all of it. Because
    the goods are taken as saleable here, they re-enter stock; a return whose condition
    still has to be checked belongs on POST /sales-returns instead, which restocks only
    what someone has confirmed is fit to sell.
    """
    org_id = _org_id(user)
    invoice = _owned(db, id, org_id)

    if invoice.is_credit_note:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That is already a credit note",
        )
    already = return_service.credited_against(db, invoice.id)
    if already + 0.01 >= (invoice.total or 0):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invoice is already fully credited"
        )

    warehouse = stock_service.default_warehouse(db, org_id)

    lines = []
    for item in invoice.items:
        quantity = item.quantity
        if payload.items:
            match = next(
                (
                    row for row in payload.items
                    if row.product_id == item.product_id and row.variant_id == item.variant_id
                ),
                None,
            )
            quantity = match.quantity if match else 0
        if quantity <= 0:
            continue

        if item.product_id:
            # Back into the warehouse the goods left, through the one stock path, so the
            # warehouse row, the catalog counter and the movement ledger stay in step.
            stock_service.adjust_on_hand(
                db, org_id, warehouse.id, item.product_id, item.variant_id, quantity,
                movement_type="sales_return",
                note=f"Credit Note for {invoice.invoice_number}",
                created_by=user.id,
            )

        share = quantity / (item.quantity or 1)
        lines.append({
            "product_id": item.product_id,
            "variant_id": item.variant_id,
            "product_name": item.product_name,
            "hsn_code": item.hsn_code,
            "quantity": quantity,
            "unit_price": item.unit_price,
            "tax_rate": item.tax_rate,
            "line_total": round((item.line_total or 0) * share, 2),
        })

    if not lines:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="None of those items are on this invoice",
        )

    note = return_service.credit_note(
        db, org_id, invoice=invoice, lines=lines,
        reason=payload.reason, created_by=user.id,
    )
    db.commit()
    db.refresh(note)
    return note

