"""Money received from a customer — one path, one row, one ledger.

Every surface that takes a payment goes through here: the customer's payment history
(`POST /customers/{id}/payments`), the receipts module (`POST /payment-receipts`) and
the cash taken at the counter with a quick bill (`POST /invoices`). All three write a
single `CustomerPayment` row carrying its own receipt number, which is why the
customer ledger, the ageing buckets and an invoice's paid figure can never disagree
about how much has actually come in.

Every payment applies to its invoice, the customer's running totals and the ledger in
the one transaction the caller commits.
"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import Customer, CustomerPayment, Invoice, SalesOrder
from app.services import numbering_service


def _now() -> datetime:
    return datetime.now(timezone.utc)


def outstanding(invoice: Invoice) -> float:
    """What is still owed on an invoice."""
    return round((invoice.total or 0) - (invoice.amount_paid or 0), 2)


def payment_status(invoice: Invoice) -> str:
    """`unpaid`, `partial` or `paid`, from the figures rather than from a flag."""
    paid = invoice.amount_paid or 0
    if paid <= 0:
        return "unpaid"
    if paid + 0.01 >= (invoice.total or 0):
        return "paid"
    return "partial"


def apply_to_invoice(invoice: Invoice, amount: float) -> None:
    """Move an invoice's paid figure by `amount` (negative to void) and restate every
    status field that follows from it. Clamped at zero so voiding a payment can never
    push an invoice below nothing."""
    invoice.amount_paid = round(max((invoice.amount_paid or 0) + amount, 0), 2)
    invoice.status = payment_status(invoice)
    # The sheet's own status columns say the same thing in its own words.
    invoice.payment_status = invoice.status.title()
    if invoice.status == "paid":
        invoice.invoice_status = "Paid"
    elif invoice.invoice_status == "Paid":
        invoice.invoice_status = "Issued"


def next_receipt_number(db: Session, org_id: str) -> str:
    """The next RCPT- number for this firm."""
    return numbering_service.next_number(db, org_id, CustomerPayment.receipt_number, "RCPT")


def record(
    db: Session,
    org_id: str,
    *,
    customer: Customer | None,
    invoice: Invoice | None = None,
    amount: float,
    payment_mode: str | None = None,
    reference: str | None = None,
    note: str | None = None,
    received_on: datetime | None = None,
    order_id: str | None = None,
    receipt_number: str | None = None,
    upi_id: str | None = None,
    card_type: str | None = None,
    card_last_four: str | None = None,
    collection_instructions: str | None = None,
) -> CustomerPayment:
    """Record one payment. Raises `ValueError` with a message fit to show a user.

    Naming an invoice settles that invoice: its paid figure and status move with the
    payment, and more than the invoice still owes is refused — an over-payment is
    almost always a typo. Money genuinely held on account is an advance, recorded
    with no invoice, which only moves the customer's running balance.

    The caller commits. Nothing here commits or rolls back, so an invoice, its stock
    movements and its payment all land together or not at all.
    """
    if amount is None or amount <= 0:
        raise ValueError("A payment must be more than zero")

    if invoice is not None:
        if invoice.is_credit_note:
            raise ValueError("A credit note is not payable")
        due = outstanding(invoice)
        if due <= 0:
            raise ValueError(
                f"Invoice {invoice.invoice_number} is already fully paid. "
                "Record money held on account as an advance, with no invoice_id."
            )
        if round(amount, 2) > round(due + 0.01, 2):
            raise ValueError(
                f"That is more than invoice {invoice.invoice_number} still owes "
                f"({due:.2f}). Pay the balance, or record the extra as an advance "
                "with no invoice_id."
            )

    # 1. Capture customer receivable balance BEFORE this payment is recorded
    previous_pending = float(customer.outstanding_balance or 0.0) if customer is not None else 0.0

    # 2. Determine order_id and order_amount if linked to an order or invoice with order
    resolved_order_id = order_id or (invoice.order_id if invoice else None)
    resolved_order_amount = None
    if resolved_order_id:
        order = db.get(SalesOrder, resolved_order_id)
        if order is not None and order.organization_id == org_id:
            resolved_order_amount = float(order.total)

    payment = CustomerPayment(
        organization_id=org_id,
        customer_id=customer.id if customer is not None else None,
        invoice_id=invoice.id if invoice is not None else None,
        order_id=resolved_order_id,
        receipt_number=receipt_number or next_receipt_number(db, org_id),
        amount=round(amount, 2),
        payment_mode=payment_mode or "cash",
        reference=reference,
        note=note,
        received_on=received_on or _now(),
        order_amount=resolved_order_amount,
        previous_pending=previous_pending,
        upi_id=upi_id,
        card_type=card_type,
        card_last_four=card_last_four,
        collection_instructions=collection_instructions,
    )
    db.add(payment)

    if invoice is not None:
        apply_to_invoice(invoice, payment.amount)
    if customer is not None:
        customer.total_received = round((customer.total_received or 0) + payment.amount, 2)
        customer.recompute_outstanding()
        # 3. Capture authoritative remaining receivable AFTER payment is applied
        payment.remaining_receivable = float(customer.outstanding_balance or 0.0)
    else:
        payment.remaining_receivable = 0.0

    return payment


def void(db: Session, payment: CustomerPayment) -> None:
    """Reverse a payment and delete it, leaving every balance as if it never happened."""
    if payment.invoice_id:
        invoice = db.get(Invoice, payment.invoice_id)
        if invoice is not None:
            apply_to_invoice(invoice, -payment.amount)
    if payment.customer_id:
        customer = db.get(Customer, payment.customer_id)
        if customer is not None:
            customer.total_received = round((customer.total_received or 0) - payment.amount, 2)
            customer.recompute_outstanding()
    db.delete(payment)
