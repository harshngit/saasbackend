"""Goods coming back: request, receive, check, approve, credit.

Nothing here moves stock until a return is approved, and even then only the lines a
human marked saleable go back on the shelf. Damaged and expired goods are recorded
against the return — the firm can still credit the customer for them — but they never
become sellable stock again.

The credit note is its own document: an invoice row flagged `is_credit_note`, pointing
at the invoice it credits. The original bill stays on the account as the debit it
always was, and the customer's receivable comes down by the credit.
"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core import workflow
from app.models import (
    Customer,
    Invoice,
    InvoiceItem,
    Product,
    ProductVariant,
    ReturnItem,
    SalesReturn,
)
from app.services import numbering_service, stock_service


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ReturnError(ValueError):
    """Something the caller can fix — surfaced as a 400 with this message."""


# --------------------------- building a request ----------------------------


def already_returned(db: Session, invoice_item_id: str, exclude_return_id: str | None = None) -> float:
    """How much of an invoice line is already committed to open or approved returns."""
    query = (
        db.query(ReturnItem)
        .join(SalesReturn, ReturnItem.return_id == SalesReturn.id)
        .filter(
            ReturnItem.invoice_item_id == invoice_item_id,
            SalesReturn.return_status.in_(("requested", "received", "approved")),
        )
    )
    if exclude_return_id:
        query = query.filter(SalesReturn.id != exclude_return_id)
    return round(sum(row.quantity_returned or 0 for row in query.all()), 3)


def _line_from_invoice_item(
    db: Session, item: InvoiceItem, quantity: float, return_id: str | None
) -> ReturnItem:
    """One return line, priced from the invoice line it comes back off."""
    outstanding = round((item.quantity or 0) - already_returned(db, item.id, return_id), 3)
    if quantity > outstanding:
        raise ReturnError(
            f"{item.product_name}: only {outstanding:g} of the {item.quantity:g} invoiced "
            "can still be returned"
        )
    share = quantity / (item.quantity or 1)
    line_total = round((item.unit_price or 0) * quantity - (item.discount or 0) * share, 2)
    return ReturnItem(
        product_id=item.product_id,
        variant_id=item.variant_id,
        product_name=item.product_name,
        quantity_returned=quantity,
        invoice_item_id=item.id,
        unit_price=item.unit_price,
        tax_rate=item.tax_rate,
        line_total=line_total,
    )


def _line_from_product(db: Session, org_id: str, entry, invoice: Invoice | None) -> ReturnItem:
    """A return line for goods with no invoice line behind them.

    Still supported because a firm may take goods back against a paper bill, or none
    at all. Priced from the invoice if one is named, else from the product.
    """
    product = db.get(Product, entry.product_id)
    if product is None or product.organization_id != org_id:
        raise ReturnError("An item's product is not in your firm")
    variant = None
    if entry.variant_id:
        variant = db.get(ProductVariant, entry.variant_id)
        if variant is None or variant.product_id != product.id:
            raise ReturnError("An item's variant is invalid")

    unit_price = (variant.price if variant is not None else None) or product.price or 0
    tax_rate = product.tax_rate
    if invoice is not None:
        match = next(
            (
                row for row in invoice.items
                if row.product_id == product.id and row.variant_id == entry.variant_id
            ),
            None,
        )
        if match is not None:
            unit_price = match.unit_price
            tax_rate = match.tax_rate
    name = product.name if variant is None else f"{product.name} ({variant.name})"
    return ReturnItem(
        product_id=product.id,
        variant_id=entry.variant_id,
        product_name=name,
        quantity_returned=entry.quantity_returned,
        unit_price=unit_price,
        tax_rate=tax_rate,
        line_total=round(unit_price * entry.quantity_returned, 2),
    )


def build_items(
    db: Session,
    org_id: str,
    entries: list,
    invoice: Invoice | None,
    return_id: str | None = None,
) -> list[ReturnItem]:
    """The lines of a return request, priced as they were billed."""
    if not entries:
        raise ReturnError("A return needs at least one item")
    lines = []
    for entry in entries:
        quantity = entry.quantity_returned
        if quantity is None or quantity <= 0:
            raise ReturnError("Each returned quantity must be more than zero")
        if getattr(entry, "invoice_item_id", None):
            if invoice is None:
                raise ReturnError("invoice_item_id needs invoice_reference_id as well")
            item = next((row for row in invoice.items if row.id == entry.invoice_item_id), None)
            if item is None:
                raise ReturnError("invoice_item_id is not a line on that invoice")
            lines.append(_line_from_invoice_item(db, item, quantity, return_id))
        elif getattr(entry, "product_id", None):
            lines.append(_line_from_product(db, org_id, entry, invoice))
        else:
            raise ReturnError("Each item needs an invoice_item_id or a product_id")
    return lines


# ------------------------------ the decisions ------------------------------


def receive(sales_return: SalesReturn, entries: list | None) -> None:
    """Record that the goods are physically back, with what actually arrived.

    An optional step: a firm that checks the goods as they arrive can go straight to
    approve, which takes the same per-line figures.
    """
    if sales_return.return_status not in workflow.OPEN_RETURN_STATUSES:
        raise ReturnError(f"A '{sales_return.return_status}' return cannot be received")
    _apply_line_findings(sales_return, entries, default_to_requested=True)
    sales_return.return_status = "received"
    sales_return.received_at = sales_return.received_at or _now()


def _apply_line_findings(
    sales_return: SalesReturn, entries: list | None, default_to_requested: bool
) -> None:
    """Write the received quantity, condition and restock decision onto each line."""
    by_id = {line.id: line for line in sales_return.items}
    for entry in entries or []:
        line = by_id.get(entry.return_item_id)
        if line is None:
            raise ReturnError("return_item_id is not a line on this return")
        received = entry.received_quantity
        if received is None:
            received = line.received_quantity
            if received is None and default_to_requested:
                received = line.quantity_returned
        if received is not None:
            if received < 0:
                raise ReturnError("A received quantity cannot be negative")
            if received > (line.quantity_returned or 0):
                raise ReturnError(
                    f"{line.product_name}: {received:g} received is more than the "
                    f"{line.quantity_returned:g} the customer asked to return"
                )
            line.received_quantity = received
        if entry.condition is not None:
            line.condition = entry.condition
        if entry.restock is not None:
            if entry.restock and not workflow.is_saleable(line.condition):
                raise ReturnError(
                    f"{line.product_name} is marked '{line.condition or 'unknown'}', so it "
                    "cannot be restocked. Only goods in saleable condition go back on the shelf."
                )
            line.restock = entry.restock

    # Anything the caller did not mention still needs a received quantity to be
    # decided on, so a line left out is taken as fully received.
    for line in sales_return.items:
        if line.received_quantity is None and default_to_requested:
            line.received_quantity = line.quantity_returned


def approve(
    db: Session,
    org_id: str,
    sales_return: SalesReturn,
    entries: list | None,
    user_id: str | None,
    warehouse_id: str | None = None,
    credit: bool = True,
) -> Invoice | None:
    """Accept a return: restock what is saleable, then credit the customer.

    Returns the credit note raised, or None when there was nothing to credit.
    """
    if sales_return.return_status not in workflow.OPEN_RETURN_STATUSES:
        raise ReturnError(f"A '{sales_return.return_status}' return cannot be approved")

    _apply_line_findings(sales_return, entries, default_to_requested=True)

    warehouse = stock_service.owned_warehouse(db, warehouse_id or sales_return.warehouse_id, org_id)
    if (warehouse_id or sales_return.warehouse_id) and warehouse is None:
        raise ReturnError("warehouse_id is not a warehouse in your firm")
    if warehouse is None:
        warehouse = stock_service.default_warehouse(db, org_id)
    sales_return.warehouse_id = warehouse.id

    credited = 0.0
    tax_credited = 0.0
    for line in sales_return.items:
        received = line.received_quantity or 0
        saleable = workflow.is_saleable(line.condition) and line.restock
        line.restocked_quantity = received if saleable else 0
        if saleable and received > 0 and line.product_id:
            # Back into the lot it was sold from, and the units back on the shelf, so a
            # batch-tracked return does not quietly become untracked stock.
            source = db.get(InvoiceItem, line.invoice_item_id) if line.invoice_item_id else None
            batch_number = getattr(source, "batch_number", None)
            serials = list(getattr(source, "serial_numbers", None) or [])[: int(received)]
            stock_service.adjust_on_hand(
                db, org_id, warehouse.id, line.product_id, line.variant_id, received,
                movement_type="sales_return",
                note=f"Sales Return {sales_return.return_number}",
                created_by=user_id,
                batch={
                    "batch_number": batch_number,
                    "expiry_date": getattr(source, "expiry_date", None),
                } if batch_number else None,
                serial_numbers=serials or None,
            )
        # The customer is credited for what came back, whatever condition it is in —
        # whether damaged goods are refunded is a commercial decision, not a stock one.
        if received > 0:
            share = received / (line.quantity_returned or 1)
            value = round((line.line_total or 0) * share, 2)
            credited += value
            tax_credited += round(value * (line.tax_rate or 0) / 100, 2)

    sales_return.return_status = "approved"
    sales_return.received_at = sales_return.received_at or _now()
    sales_return.approved_at = _now()
    sales_return.approved_by = user_id
    sales_return.credit_amount = round(credited + tax_credited, 2)

    note = None
    if credit and sales_return.credit_amount > 0:
        note = credit_note(
            db, org_id, sales_return=sales_return,
            reason=sales_return.return_reason or "Sales return",
            created_by=user_id,
        )
        sales_return.credit_note_id = note.id
    return note


def reject(sales_return: SalesReturn, reason: str | None) -> None:
    """Refuse a return: nothing is restocked and nothing is credited."""
    if sales_return.return_status not in workflow.OPEN_RETURN_STATUSES:
        raise ReturnError(f"A '{sales_return.return_status}' return cannot be rejected")
    sales_return.return_status = "rejected"
    sales_return.rejected_reason = reason
    for line in sales_return.items:
        line.restocked_quantity = 0


# ------------------------------ credit notes -------------------------------


def credited_against(db: Session, invoice_id: str) -> float:
    """How much has already been credited back against one invoice."""
    rows = (
        db.query(Invoice)
        .filter(
            Invoice.credit_note_for_invoice_id == invoice_id,
            Invoice.is_credit_note.is_(True),
        )
        .all()
    )
    return round(sum(row.total or 0 for row in rows), 2)


def credit_note(
    db: Session,
    org_id: str,
    *,
    sales_return: SalesReturn | None = None,
    invoice: Invoice | None = None,
    lines: list[dict] | None = None,
    reason: str | None = None,
    created_by: str | None = None,
) -> Invoice:
    """Raise a credit note — its own document, against the invoice it credits.

    Called with a return, it credits what that return received. Called with an invoice
    and lines, it credits those lines directly, which is what "credit this invoice"
    from the invoice itself does.

    The customer's billed figure comes down by the credit, so their outstanding falls;
    the original invoice is untouched except for its status.
    """
    source_invoice = invoice
    if sales_return is not None and source_invoice is None and sales_return.invoice_reference_id:
        source_invoice = db.get(Invoice, sales_return.invoice_reference_id)

    customer_id = None
    if sales_return is not None:
        customer_id = sales_return.customer_id
    if customer_id is None and source_invoice is not None:
        customer_id = source_invoice.customer_id

    note = Invoice(
        organization_id=org_id,
        invoice_number=numbering_service.next_number(db, org_id, Invoice.invoice_number, "CN"),
        sales_id=numbering_service.next_number(db, org_id, Invoice.sales_id, "SALE"),
        customer_id=customer_id,
        credit_note_for_invoice_id=source_invoice.id if source_invoice is not None else None,
        sales_return_id=sales_return.id if sales_return is not None else None,
        invoice_date=_now(),
        billing_address=source_invoice.billing_address if source_invoice is not None else None,
        sales_type="Credit Note",
        sales_date=_now(),
        sales_status="Returned",
        invoice_status="Issued",
        payment_status="Refunded",
        # A credit note is not a receivable, so it never sits in an unpaid list.
        status="returned",
        is_credit_note=True,
        credit_note_reason=reason,
        created_by=created_by,
    )

    subtotal = 0.0
    tax_total = 0.0
    entries = lines
    if entries is None and sales_return is not None:
        entries = []
        for line in sales_return.items:
            received = line.received_quantity or 0
            if received <= 0:
                continue
            share = received / (line.quantity_returned or 1)
            entries.append({
                "product_id": line.product_id,
                "variant_id": line.variant_id,
                "product_name": line.product_name,
                "quantity": received,
                "unit_price": line.unit_price or 0,
                "tax_rate": line.tax_rate,
                "line_total": round((line.line_total or 0) * share, 2),
                "invoice_item_id": line.invoice_item_id,
            })

    for entry in entries or []:
        line_total = entry["line_total"]
        tax_amount = round(line_total * (entry.get("tax_rate") or 0) / 100, 2)
        subtotal += line_total
        tax_total += tax_amount
        note.items.append(
            InvoiceItem(
                product_id=entry.get("product_id"),
                variant_id=entry.get("variant_id"),
                product_name=entry["product_name"],
                hsn_code=entry.get("hsn_code"),
                quantity=entry["quantity"],
                unit_price=entry["unit_price"],
                discount=0,
                tax=tax_amount,
                tax_amount=tax_amount,
                tax_rate=entry.get("tax_rate"),
                line_total=line_total,
                order_item_id=entry.get("order_item_id"),
            )
        )

    note.subtotal = round(subtotal, 2)
    note.tax = round(tax_total, 2)
    note.total = round(note.subtotal + note.tax, 2)
    db.add(note)
    db.flush()

    # The receivable comes down by the credit. An anonymous counter sale has no ledger
    # to adjust — the credit note is the record.
    if customer_id:
        customer = db.get(Customer, customer_id)
        if customer is not None:
            customer.total_billed = round((customer.total_billed or 0) - note.total, 2)
            customer.recompute_outstanding()

    if source_invoice is not None:
        outstanding = round(
            (source_invoice.total or 0) - credited_against(db, source_invoice.id), 2
        )
        if outstanding <= 0.01:
            source_invoice.status = "returned"
            source_invoice.sales_status = "Returned"
    return note
