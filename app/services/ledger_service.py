"""The customer's account, built from the invoices and payments themselves.

The receivable starts at the invoice, never at the order: an order is a promise, an
invoice is a bill. So everything here reads invoices and `customer_payments` rows and
nothing else — the running figures on the customer are only a summary of these.
"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import Customer, CustomerPayment, Invoice
from app.schemas.ledger import (
    CustomerLedger,
    LedgerAgeing,
    LedgerSummary,
    LedgerTransaction,
)


def _aware(moment: datetime | None) -> datetime | None:
    """Compare dates safely: SQLite hands back naive datetimes, Postgres aware ones."""
    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _age_in_days(invoice: Invoice, now: datetime) -> int:
    """How long this invoice has been outstanding, from its due date if it has one."""
    reference = _aware(invoice.due_date) or _aware(invoice.invoice_date) or now
    return max((now - reference).days, 0)


def _bucket(days: int) -> str:
    if days <= 30:
        return "0_30"
    if days <= 60:
        return "31_60"
    if days <= 90:
        return "61_90"
    return "90_plus"


def _parse_filter_date(d: datetime | str | None, end_of_day: bool = False) -> datetime | None:
    if d is None:
        return None
    if isinstance(d, datetime):
        return _aware(d)
    if isinstance(d, str):
        try:
            # Handle ISO string with or without time
            if "T" in d or " " in d:
                dt = datetime.fromisoformat(d.replace("Z", "+00:00"))
                return _aware(dt)
            # Date only: YYYY-MM-DD
            parts = [int(p) for p in d.split("-")]
            if len(parts) == 3:
                if end_of_day:
                    return datetime(parts[0], parts[1], parts[2], 23, 59, 59, 999999, tzinfo=timezone.utc)
                return datetime(parts[0], parts[1], parts[2], 0, 0, 0, 0, tzinfo=timezone.utc)
        except Exception:
            return None
    return None


def build(
    db: Session,
    customer: Customer,
    date_from: datetime | str | None = None,
    date_to: datetime | str | None = None,
) -> CustomerLedger:
    """The full account: summary, ageing buckets and every transaction, oldest first."""
    now = datetime.now(timezone.utc)
    from_dt = _parse_filter_date(date_from, end_of_day=False)
    to_dt = _parse_filter_date(date_to, end_of_day=True)

    invoices = (
        db.query(Invoice)
        .filter(
            Invoice.customer_id == customer.id,
            Invoice.organization_id == customer.organization_id,
        )
        .order_by(Invoice.invoice_date, Invoice.id)
        .all()
    )
    payments = (
        db.query(CustomerPayment)
        .filter(
            CustomerPayment.customer_id == customer.id,
            CustomerPayment.organization_id == customer.organization_id,
        )
        .order_by(CustomerPayment.received_on, CustomerPayment.id)
        .all()
    )

    entries: list[tuple[datetime, LedgerTransaction]] = []
    credited_for = {row.id: row.invoice_number for row in invoices}

    # An opening balance is money already owed when the account was created, so it
    # heads the ledger rather than hiding inside the summary.
    if customer.opening_balance:
        opened = _aware(customer.created_at) or now
        entries.append((opened, LedgerTransaction(
            id=f"op-{customer.id}",
            type="opening_balance",
            reference_id=customer.id,
            reference_number="OPENING",
            date=opened,
            description="Opening balance",
            debit=round(customer.opening_balance, 2),
        )))

    ageing = {
        "0_30": 0.0, "31_60": 0.0, "61_90": 0.0, "90_plus": 0.0,
    }
    overdue = 0.0

    # What has been credited back against each invoice, so a returned bill is not
    # still shown as money owed.
    credited: dict[str, float] = {}
    for note in invoices:
        if note.is_credit_note and note.credit_note_for_invoice_id:
            credited[note.credit_note_for_invoice_id] = round(
                credited.get(note.credit_note_for_invoice_id, 0) + (note.total or 0), 2
            )

    for invoice in invoices:
        when = _aware(invoice.invoice_date) or now
        if invoice.is_credit_note:
            # A credit note gives value back, so it settles rather than bills.
            entries.append((when, LedgerTransaction(
                id=invoice.id,
                type="credit_note",
                reference_id=invoice.id,
                reference_number=invoice.invoice_number,
                date=when,
                description=(
                    invoice.credit_note_reason or "Credit note"
                ) + (
                    f" against {credited_for[invoice.credit_note_for_invoice_id]}"
                    if invoice.credit_note_for_invoice_id in credited_for else ""
                ),
                credit=round(invoice.total or 0, 2),
                status=invoice.status,
            )))
            continue

        entries.append((when, LedgerTransaction(
            id=invoice.id,
            type="invoice",
            reference_id=invoice.id,
            reference_number=invoice.invoice_number,
            date=when,
            description=invoice.sales_type or "Invoice",
            debit=round(invoice.total or 0, 2),
            due_date=invoice.due_date,
            status=invoice.status,
        )))

        unpaid = round(
            (invoice.total or 0) - (invoice.amount_paid or 0) - credited.get(invoice.id, 0), 2
        )
        if unpaid > 0:
            days = _age_in_days(invoice, now)
            b = _bucket(days)
            ageing[b] = round(ageing[b] + unpaid, 2)
            due = _aware(invoice.due_date)
            if due is not None and due < now:
                overdue = round(overdue + unpaid, 2)

    for payment in payments:
        when = _aware(payment.received_on) or now
        against = payment.invoice.invoice_number if payment.invoice is not None else None
        entries.append((when, LedgerTransaction(
            id=payment.id,
            type="payment",
            reference_id=payment.id,
            reference_number=payment.receipt_number,
            date=when,
            description=(
                f"Payment against {against}" if against else "Advance / on-account payment"
            ) + (f" ({payment.payment_mode})" if payment.payment_mode else ""),
            credit=round(payment.amount or 0, 2),
        )))

    entries.sort(key=lambda pair: pair[0])
    running = 0.0
    transactions = []
    effective_opening_balance = customer.opening_balance or 0.0

    for when, row in entries:
        running = round(running + row.debit - row.credit, 2)
        row.balance = running
        row.balance_after = running

        if from_dt is not None and when < from_dt:
            # Accumulate prior transactions into opening balance of the period
            effective_opening_balance = running
            continue

        if to_dt is not None and when > to_dt:
            continue

        transactions.append(row)

    total_billed = sum(t.debit for t in transactions if t.type == "invoice") if from_dt or to_dt else (customer.total_billed or 0)
    total_received = sum(t.credit for t in transactions if t.type == "payment") if from_dt or to_dt else (customer.total_received or 0)
    outstanding = round(running, 2) if (from_dt or to_dt) else round(customer.outstanding_balance or 0, 2)

    summary = LedgerSummary(
        total_billed=round(total_billed, 2),
        total_received=round(total_received, 2),
        opening_balance=round(effective_opening_balance, 2),
        outstanding=outstanding,
        credit_limit=round(customer.credit_limit or 0, 2),
        available_credit=round(max((customer.credit_limit or 0) - outstanding, 0), 2),
        overdue_amount=round(overdue, 2),
        overdue=round(overdue, 2),
    )

    return CustomerLedger(
        customer_id=customer.id,
        customer_name=customer.business_name or customer.name,
        summary=summary,
        ageing=LedgerAgeing(**ageing),
        transactions=transactions,
    )
