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
    StockMovement,
    StoredFile,
    User,
)
from app.services import numbering_service, lookup_service
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
    """Direct sale invoice creation. Finalizes the sale, deducts stock, and updates receivables."""
    org_id = _org_id(user)
    customer = db.get(Customer, payload.customer_id)
    if customer is None or customer.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="customer_id is not a customer in your firm")

    invoice = Invoice(
        organization_id=org_id,
        invoice_number=_next_invoice_number(db, org_id),
        sales_id=numbering_service.next_number(db, org_id, Invoice.sales_id, "SALE"),
        customer_id=customer.id,
        invoice_date=payload.invoice_date or datetime.now(timezone.utc),
        # Sheet marks Billing Address "copied from related record at creation time".
        billing_address=payload.billing_address or customer.billing_address,
        sales_type=payload.sales_type or "Invoice",
        sales_date=payload.sales_date or payload.invoice_date or datetime.now(timezone.utc),
        sales_status=payload.sales_status or "Confirmed",
        invoice_status=payload.invoice_status or "Issued",
        payment_status=payload.payment_status or "Unpaid",
        status="unpaid",
        discount=payload.discount,
        tax=payload.tax,
        additional_charges=payload.additional_charges,
        round_off=payload.round_off,
        notes=payload.notes,
        created_by=user.id,
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

        # Deduct Main Warehouse Inventory instantly
        current_inv = variant.inventory if variant else product.total_inventory
        new_inv = current_inv - it.quantity
        if new_inv < 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Insufficient stock for {product.name}",
            )
        if variant:
            variant.inventory = new_inv
        else:
            product.total_inventory = new_inv

        db.add(
            StockMovement(
                organization_id=org_id,
                product_id=product.id,
                variant_id=it.variant_id,
                movement_type="sale_out",
                quantity=-it.quantity,
                balance_after=new_inv,
                note=f"Direct Invoice {invoice.invoice_number}",
                created_by=user.id,
            )
        )

        invoice.items.append(
            InvoiceItem(
                product_id=product.id,
                variant_id=it.variant_id,
                product_name=product.name if not variant else f"{product.name} ({variant.name})",
                hsn_code=product.hsn_code,
                quantity=it.quantity,
                unit_price=unit_price,
                discount=it.discount,
                tax=it.tax,
                line_total=line_total,
            )
        )

    invoice.subtotal = round(subtotal, 2)
    invoice.total = round(
        subtotal
        - payload.discount
        + payload.tax
        + (payload.additional_charges or 0)
        + (payload.round_off or 0),
        2,
    )

    # Add to the customer's receivables
    customer.total_billed = round((customer.total_billed or 0) + invoice.total, 2)
    customer.recompute_outstanding()

    db.add(invoice)
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
    """Record a credit note (sales return). Reverses stock and reduces receivables."""
    org_id = _org_id(user)
    invoice = _owned(db, id, org_id)

    if invoice.status == "returned":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invoice is already returned")

    # Reverse stock deduction
    for item in invoice.items:
        qty_to_return = item.quantity
        if payload.items:
            match = next((x for x in payload.items if x.product_id == item.product_id and x.variant_id == item.variant_id), None)
            if match:
                qty_to_return = match.quantity
            else:
                qty_to_return = 0

        if qty_to_return <= 0:
            continue

        if item.variant_id:
            variant = db.get(ProductVariant, item.variant_id)
            if variant:
                variant.inventory = (variant.inventory or 0) + qty_to_return
                new_inv = variant.inventory
        else:
            product = db.get(Product, item.product_id)
            if product:
                product.total_inventory = (product.total_inventory or 0) + qty_to_return
                new_inv = product.total_inventory

        db.add(
            StockMovement(
                organization_id=org_id,
                product_id=item.product_id,
                variant_id=item.variant_id,
                movement_type="sales_return",
                quantity=qty_to_return,
                balance_after=new_inv,
                note=f"Credit Note Return for {invoice.invoice_number}",
                created_by=user.id,
            )
        )

    # Adjust customer receivables (reduce total_billed)
    if invoice.customer:
        customer = invoice.customer
        customer.total_billed = round((customer.total_billed or 0) - invoice.total, 2)
        customer.recompute_outstanding()

    invoice.status = "returned"
    invoice.is_credit_note = True
    if payload.reason:
        invoice.credit_note_reason = payload.reason

    db.commit()
    db.refresh(invoice)
    return invoice
