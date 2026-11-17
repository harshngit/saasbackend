"""A customer's pending invoices and collected payments, as paginated lists.

No ledger of its own: both lists read the records the rest of the app already treats as
the truth.

* Pending -- `invoices` (not credit notes, not void / cancelled). What is still owed on one
  is the customer ledger's own formula (ledger_service): total - amount_paid - the credit
  notes issued against it. `amount_paid` is moved by payment_service.apply_to_invoice on
  every payment, allocation and void, so it already is "paid / allocated".
* Collected -- `customer_payments`. Every surface that takes money (customer payments,
  receipts, counter bills, orders paid on creation, Razorpay, reconciled delivery
  collections) writes exactly one row there through payment_service.record, and voiding
  deletes the row (payment_service.void). A delivery collection that is only `recorded`
  has no row yet and is therefore not listed; once reconciled it is listed once, as the
  payment the reconciliation created.

Amounts are summed as SQL NUMERIC(18,2) -- the columns are floats, the arithmetic is not.
Every filter is applied in SQL before the count, the page and the summary, so the three
always describe the same rows.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import Numeric, and_, case, cast, exists, func, literal, or_, select
from sqlalchemy.orm import Session, aliased

from app.core import business_time
from app.core.pagination import paginate
from app.core.sorting import apply_sorting
from app.models import (
    Customer,
    CustomerPayment,
    CustomerPaymentAllocation,
    DeliveryCollection,
    Invoice,
    Organization,
    PaymentSplit,
)
from app.schemas.customer_payment_lists import (
    CollectedPaymentOut,
    CollectedPaymentsOut,
    CollectedSummary,
    PaymentAllocationOut,
    PaymentSplitBrief,
    PendingInvoiceOut,
    PendingPaymentsOut,
    PendingSummary,
)

MONEY = Numeric(18, 2)
_EXCLUDED_INVOICE_STATUSES = ("void", "cancelled")
PENDING_PAYMENT_STATUSES = ("unpaid", "partial")
COLLECTED_PAYMENT_STATUSES = ("recorded",)


def _money(column):  # noqa: ANN001, ANN202
    return cast(func.coalesce(column, 0), MONEY)


def _amount(value) -> float:  # noqa: ANN001
    return float(Decimal(value or 0).quantize(Decimal("0.01")))


def _parse_day(value: str | None, name: str) -> date | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{name} must be YYYY-MM-DD") from None


def _day_range(start: str | None, end: str | None, start_name: str, end_name: str, tz):  # noqa: ANN001, ANN202
    """(from instant, to instant) for org-local calendar days, both inclusive."""
    first, last = _parse_day(start, start_name), _parse_day(end, end_name)
    if first and last and first > last:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"{start_name} cannot be after {end_name}")
    return (business_time.day_start(first, tz) if first else None,
            business_time.day_end(last, tz) if last else None)


def _currency(db: Session, customer: Customer) -> str:
    if customer.currency:
        return customer.currency
    org = db.get(Organization, customer.organization_id)
    return (org.currency if org is not None and org.currency else None) or "INR"


def _like(text: str) -> str:
    escaped = text.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# ------------------------------------------------------------------------------ pending
def pending(
    db: Session,
    customer: Customer,
    *,
    from_date: str | None = None,
    to_date: str | None = None,
    due_from: str | None = None,
    due_to: str | None = None,
    overdue: bool | None = None,
    payment_status: str | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 10,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> PendingPaymentsOut:
    org_id = customer.organization_id
    tz = business_time.org_timezone(db, org_id)
    today = business_time.today(tz)
    today_start = business_time.day_start(today, tz)
    if payment_status is not None and payment_status not in PENDING_PAYMENT_STATUSES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"payment_status must be one of {list(PENDING_PAYMENT_STATUSES)}")
    inv_from, inv_to = _day_range(from_date, to_date, "from_date", "to_date", tz)
    due_start, due_end = _day_range(due_from, due_to, "due_from", "due_to", tz)

    note = aliased(Invoice)
    credited = (
        select(note.credit_note_for_invoice_id.label("invoice_id"), func.sum(_money(note.total)).label("credited"))
        .where(note.organization_id == org_id, note.is_credit_note.is_(True),
               note.credit_note_for_invoice_id.isnot(None))
        .group_by(note.credit_note_for_invoice_id)
        .subquery()
    )
    total = _money(Invoice.total)
    paid = _money(Invoice.amount_paid)
    credit = cast(func.coalesce(credited.c.credited, 0), MONEY)
    outstanding = total - paid - credit
    is_overdue = and_(Invoice.due_date.isnot(None), Invoice.due_date < today_start)
    status_expr = case((paid + credit > 0, literal("partial")), else_=literal("unpaid"))

    query = (
        db.query(
            Invoice.id.label("invoice_id"), Invoice.invoice_number, Invoice.order_id, Invoice.invoice_date,
            Invoice.due_date, Invoice.status.label("invoice_status"),
            total.label("total"), paid.label("amount_paid"), credit.label("credited_amount"),
            outstanding.label("outstanding"), status_expr.label("payment_status"),
            case((is_overdue, literal(True)), else_=literal(False)).label("is_overdue"),
        )
        .outerjoin(credited, credited.c.invoice_id == Invoice.id)
        .filter(
            Invoice.organization_id == org_id,
            Invoice.customer_id == customer.id,
            Invoice.is_credit_note.is_(False),
            ~func.lower(func.coalesce(Invoice.status, "")).in_(_EXCLUDED_INVOICE_STATUSES),
            outstanding > 0,
        )
    )
    if inv_from is not None:
        query = query.filter(Invoice.invoice_date >= inv_from)
    if inv_to is not None:
        query = query.filter(Invoice.invoice_date <= inv_to)
    if due_start is not None:
        query = query.filter(Invoice.due_date >= due_start)
    if due_end is not None:
        query = query.filter(Invoice.due_date <= due_end)
    if overdue is True:
        query = query.filter(is_overdue)
    elif overdue is False:
        query = query.filter(or_(Invoice.due_date.is_(None), Invoice.due_date >= today_start))
    if payment_status is not None:
        query = query.filter(status_expr == payment_status)
    if search and search.strip():
        query = query.filter(Invoice.invoice_number.ilike(_like(search), escape="\\"))

    rows = query.subquery()
    agg = db.query(
        func.count(rows.c.invoice_id),
        func.coalesce(func.sum(rows.c.outstanding), 0),
        func.coalesce(func.sum(case((rows.c.is_overdue.is_(True), rows.c.outstanding), else_=0)), 0),
        func.coalesce(func.sum(case((rows.c.is_overdue.is_(True), 1), else_=0)), 0),
    ).one()

    query = apply_sorting(
        query, sort_by=sort_by, sort_order=sort_order,
        allowed_fields={
            "invoice_date": Invoice.invoice_date, "due_date": Invoice.due_date,
            "invoice_number": Invoice.invoice_number, "total": total, "outstanding": outstanding,
        },
        default_order=[Invoice.due_date.asc().nullslast(), Invoice.invoice_date.asc(), Invoice.id.asc()],
        secondary_key=Invoice.id.asc(),
    )
    page_rows, count, page, page_size, total_pages = paginate(query, page=page, page_size=page_size)
    currency = _currency(db, customer)

    items = []
    for r in page_rows:
        days = 0
        if r.is_overdue and r.due_date is not None:
            days = max((today - business_time.local_date(r.due_date, tz)).days, 0)
        items.append(PendingInvoiceOut(
            invoice_id=r.invoice_id, invoice_number=r.invoice_number, order_id=r.order_id,
            invoice_date=r.invoice_date, due_date=r.due_date, total=_amount(r.total),
            amount_paid=_amount(r.amount_paid), credited_amount=_amount(r.credited_amount),
            outstanding=_amount(r.outstanding), currency=currency, payment_status=r.payment_status,
            invoice_status=r.invoice_status, is_overdue=bool(r.is_overdue), days_overdue=days,
        ))
    return PendingPaymentsOut(
        items=items, total=count, page=page, page_size=page_size, total_pages=total_pages,
        summary=PendingSummary(
            total_outstanding=_amount(agg[1]), total_overdue=_amount(agg[2]),
            pending_invoices=int(agg[0] or 0), overdue_invoices=int(agg[3] or 0),
            currency=currency, payment_terms=customer.payment_terms, as_of=today,
        ),
    )


# ---------------------------------------------------------------------------- collected
def collected(
    db: Session,
    customer: Customer,
    *,
    from_date: str | None = None,
    to_date: str | None = None,
    payment_mode: str | None = None,
    payment_status: str | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 10,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> CollectedPaymentsOut:
    org_id = customer.organization_id
    tz = business_time.org_timezone(db, org_id)
    if payment_status is not None and payment_status not in COLLECTED_PAYMENT_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"payment_status must be one of {list(COLLECTED_PAYMENT_STATUSES)} "
                   "(a voided payment is deleted, so no other state exists)",
        )
    pay_from, pay_to = _day_range(from_date, to_date, "from_date", "to_date", tz)

    alloc = (
        select(CustomerPaymentAllocation.customer_payment_id.label("payment_id"),
               func.sum(_money(CustomerPaymentAllocation.amount)).label("allocated"))
        .where(CustomerPaymentAllocation.organization_id == org_id)
        .group_by(CustomerPaymentAllocation.customer_payment_id)
        .subquery()
    )
    amount = _money(CustomerPayment.amount)
    # Multi-invoice allocations when the payment has them; otherwise the invoice it settled
    # (payment_service.record applies the whole amount to it); otherwise an advance.
    allocated = case(
        (alloc.c.allocated.isnot(None), cast(alloc.c.allocated, MONEY)),
        (CustomerPayment.invoice_id.isnot(None), amount),
        else_=cast(0, MONEY),
    )
    from_collection = exists().where(DeliveryCollection.customer_payment_id == CustomerPayment.id)

    query = (
        db.query(
            CustomerPayment.id.label("payment_id"), CustomerPayment.receipt_number,
            CustomerPayment.received_on, amount.label("amount"), CustomerPayment.payment_mode,
            CustomerPayment.reference, CustomerPayment.order_id, CustomerPayment.invoice_id,
            allocated.label("allocated_amount"), (amount - allocated).label("unallocated_amount"),
            case((from_collection, literal("delivery_collection")), else_=literal("direct")).label("source"),
        )
        .outerjoin(alloc, alloc.c.payment_id == CustomerPayment.id)
        .filter(CustomerPayment.organization_id == org_id, CustomerPayment.customer_id == customer.id)
    )
    if pay_from is not None:
        query = query.filter(CustomerPayment.received_on >= pay_from)
    if pay_to is not None:
        query = query.filter(CustomerPayment.received_on <= pay_to)
    if payment_mode and payment_mode.strip():
        mode = payment_mode.strip().lower()
        query = query.filter(or_(
            func.lower(CustomerPayment.payment_mode) == mode,
            exists().where(PaymentSplit.payment_id == CustomerPayment.id, func.lower(PaymentSplit.payment_mode) == mode),
        ))
    if search and search.strip():
        like = _like(search)
        inv = aliased(Invoice)
        query = query.filter(or_(
            CustomerPayment.receipt_number.ilike(like, escape="\\"),
            CustomerPayment.reference.ilike(like, escape="\\"),
            exists().where(inv.id == CustomerPayment.invoice_id, inv.invoice_number.ilike(like, escape="\\")),
            exists().where(
                CustomerPaymentAllocation.customer_payment_id == CustomerPayment.id,
                CustomerPaymentAllocation.invoice_id == inv.id,
                inv.invoice_number.ilike(like, escape="\\"),
            ),
        ))

    rows = query.subquery()
    agg = db.query(
        func.count(rows.c.payment_id),
        func.coalesce(func.sum(rows.c.amount), 0),
        func.coalesce(func.sum(rows.c.allocated_amount), 0),
        func.coalesce(func.sum(rows.c.unallocated_amount), 0),
    ).one()

    query = apply_sorting(
        query, sort_by=sort_by, sort_order=sort_order,
        allowed_fields={"payment_date": CustomerPayment.received_on, "amount": amount,
                        "receipt_number": CustomerPayment.receipt_number},
        default_order=[CustomerPayment.received_on.desc(), CustomerPayment.id.desc()],
        secondary_key=CustomerPayment.id.desc(),
    )
    page_rows, count, page, page_size, total_pages = paginate(query, page=page, page_size=page_size)
    currency = _currency(db, customer)

    # Allocation and split details for this page only: two queries, whatever the page size.
    ids = [r.payment_id for r in page_rows]
    allocations: dict[str, list[PaymentAllocationOut]] = {}
    splits: dict[str, list[PaymentSplitBrief]] = {}
    if ids:
        for a in (
            db.query(CustomerPaymentAllocation.customer_payment_id, CustomerPaymentAllocation.invoice_id,
                     Invoice.invoice_number, _money(CustomerPaymentAllocation.amount))
            .outerjoin(Invoice, Invoice.id == CustomerPaymentAllocation.invoice_id)
            .filter(CustomerPaymentAllocation.customer_payment_id.in_(ids))
            .order_by(CustomerPaymentAllocation.customer_payment_id, Invoice.invoice_number,
                      CustomerPaymentAllocation.id)
        ):
            allocations.setdefault(a[0], []).append(
                PaymentAllocationOut(invoice_id=a[1], invoice_number=a[2], amount=_amount(a[3])))
        single = {r.invoice_id for r in page_rows if r.invoice_id and r.payment_id not in allocations}
        numbers = dict(db.query(Invoice.id, Invoice.invoice_number).filter(Invoice.id.in_(single))) if single else {}
        for r in page_rows:
            if r.payment_id not in allocations and r.invoice_id:
                allocations[r.payment_id] = [PaymentAllocationOut(
                    invoice_id=r.invoice_id, invoice_number=numbers.get(r.invoice_id), amount=_amount(r.allocated_amount))]
        for s in (
            db.query(PaymentSplit.payment_id, PaymentSplit.payment_mode, _money(PaymentSplit.amount), PaymentSplit.reference)
            .filter(PaymentSplit.payment_id.in_(ids))
            .order_by(PaymentSplit.payment_id, PaymentSplit.id)
        ):
            splits.setdefault(s[0], []).append(PaymentSplitBrief(payment_mode=s[1], amount=_amount(s[2]), reference=s[3]))

    items = [
        CollectedPaymentOut(
            payment_id=r.payment_id, receipt_number=r.receipt_number, payment_date=r.received_on,
            amount=_amount(r.amount), payment_mode=r.payment_mode, reference=r.reference, status="recorded",
            source=r.source, order_id=r.order_id, allocated_amount=_amount(r.allocated_amount),
            unallocated_amount=_amount(r.unallocated_amount), allocations=allocations.get(r.payment_id, []),
            splits=splits.get(r.payment_id, []), currency=currency,
        )
        for r in page_rows
    ]
    return CollectedPaymentsOut(
        items=items, total=count, page=page, page_size=page_size, total_pages=total_pages,
        summary=CollectedSummary(
            total_collected=_amount(agg[1]), payment_count=int(agg[0] or 0),
            total_allocated=_amount(agg[2]), total_unallocated=_amount(agg[3]), currency=currency,
        ),
    )
