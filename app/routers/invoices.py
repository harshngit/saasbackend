from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.core.pdf_docs import invoice_pdf
from app.models import (
    Customer,
    Invoice,
    InvoiceItem,
    Product,
    ProductVariant,
    SalesOrder,
    StockMovement,
    User,
)
from app.services import numbering_service
from app.schemas.invoice import CreditNoteBody, InvoiceCreate, InvoiceOut

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
    inv = db.get(Invoice, id)
    if inv is None or inv.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")
    return inv


def _hsn_for(db: Session, product_id: str | None) -> str | None:
    """The product's HSN/SAC, copied onto the line at invoicing time."""
    if not product_id:
        return None
    product = db.get(Product, product_id)
    return product.hsn_code if product else None


def _next_invoice_number(db: Session, org_id: str) -> str:
    # max+1, not count+1: counting reissues a number after any deletion.
    return numbering_service.next_number(db, org_id, Invoice.invoice_number, "INV")


# Also exposed as POST /orders/{order_id}/invoice — the route decorator returns the
# function untouched, so stacking registers the one handler on both routers.
@orders_router.post("/{order_id}/invoice", response_model=InvoiceOut, status_code=status.HTTP_201_CREATED)
@router.post("/orders/{order_id}/invoice", response_model=InvoiceOut, status_code=status.HTTP_201_CREATED)
def generate_from_order(
    order_id: str,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Invoice:
    """Generate an invoice from a Sales Order."""
    org_id = _org_id(user)
    order = db.get(SalesOrder, order_id)
    if order is None or order.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sales order not found")

    if order.status not in ("confirmed", "out_for_delivery", "delivered"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot generate invoice for a '{order.status}' order. Order must be confirmed/delivered first.",
        )

    # Check if invoice already exists for this order
    existing = db.query(Invoice).filter(Invoice.order_id == order_id).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invoice already generated for this order (Invoice ID: {existing.id})",
        )

    invoice = Invoice(
        organization_id=org_id,
        invoice_number=_next_invoice_number(db, org_id),
        order_id=order.id,
        sales_id=numbering_service.next_number(db, org_id, Invoice.sales_id, "SALE"),
        customer_id=order.customer_id,
        invoice_date=datetime.now(timezone.utc),
        billing_address=order.customer.billing_address if order.customer else None,
        sales_type="Sales Order",
        sales_date=datetime.now(timezone.utc),
        sales_status="Confirmed",
        invoice_status="Issued",
        payment_status="Unpaid",
        status="unpaid",
        subtotal=order.subtotal,
        discount=order.discount,
        tax=order.tax,
        total=order.total,
        amount_paid=0.0,
        notes=order.notes,
        created_by=user.id,
    )

    for item in order.items:
        # Resolve prices / tax if not present on order item
        invoice.items.append(
            InvoiceItem(
                product_id=item.product_id,
                variant_id=item.variant_id,
                product_name=item.product_name,
                hsn_code=_hsn_for(db, item.product_id),
                quantity=item.quantity,
                unit_price=item.unit_price,
                discount=item.discount,
                tax=round(item.line_total * 0.18, 2),  # Default 18% tax representation
                line_total=item.line_total,
            )
        )

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
    invoice.total = round(subtotal - payload.discount + payload.tax, 2)

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


@router.get("/{id}/pdf")
def download_invoice_pdf(
    id: str,
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> Response:
    """Download a formatted GST invoice PDF."""
    org_id = _org_id(user)
    invoice = _owned(db, id, org_id)
    if not invoice.customer:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invoice does not have a customer associated")
    
    pdf_bytes = invoice_pdf(user.organization, invoice.customer, invoice)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="invoice-{invoice.invoice_number}.pdf"'},
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
