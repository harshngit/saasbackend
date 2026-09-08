from datetime import datetime, timezone
from fastapi import HTTPException, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.supplier import Supplier
from app.models.supplier_invoice import SupplierInvoice
from app.schemas.accounts_payable import (
    APAgeingBucketOut,
    APItemOut,
    APListResponse,
    APSummaryOut,
    SupplierAPStatementOut,
)


def _aware(moment: datetime | None) -> datetime | None:
    """Helper to ensure timezone awareness for datetimes (handles SQLite naive datetimes)."""
    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def compute_ap_item_details(
    invoice: SupplierInvoice, now: datetime
) -> tuple[bool, int, str | None]:
    """Compute is_overdue, days_overdue, and ageing_bucket for an AP invoice."""
    due = _aware(invoice.due_date)
    if due is None:
        return False, 0, None

    today = now.date()
    due_date_only = due.date()

    if due_date_only >= today:
        return False, 0, None

    # Overdue invoice (calendar days overdue)
    days = (today - due_date_only).days
    if days <= 30:
        bucket = "0_30"
    elif days <= 60:
        bucket = "31_60"
    elif days <= 90:
        bucket = "61_90"
    else:
        bucket = "90_plus"

    return True, days, bucket


def build_ap_item(invoice: SupplierInvoice, now: datetime) -> APItemOut:
    """Map SupplierInvoice ORM record to APItemOut schema with derived aging data."""
    is_overdue, days_overdue, bucket = compute_ap_item_details(invoice, now)
    supplier_name = invoice.supplier.name if invoice.supplier else "Unknown Supplier"

    return APItemOut(
        supplier_invoice_id=invoice.id,
        supplier_invoice_number=invoice.supplier_invoice_number,
        supplier_id=invoice.supplier_id,
        supplier_name=supplier_name,
        invoice_date=invoice.supplier_invoice_date,
        due_date=invoice.due_date,
        grand_total=invoice.grand_total,
        amount_paid=invoice.amount_paid,
        outstanding_amount=invoice.outstanding_amount,
        payment_status=invoice.payment_status,
        verification_status=invoice.verification_status,
        status=invoice.status,
        is_overdue=is_overdue,
        days_overdue=days_overdue,
        ageing_bucket=bucket,
    )


def query_open_payables(
    db: Session,
    org_id: str,
    supplier_id: str | None = None,
    payment_status: str | None = None,
    verification_status: str | None = None,
    overdue_only: bool = False,
    search: str | None = None,
) -> list[SupplierInvoice]:
    """Derived query returning recorded SupplierInvoices with outstanding_amount > 0."""
    now = datetime.now(timezone.utc)
    q = (
        db.query(SupplierInvoice)
        .join(Supplier, Supplier.id == SupplierInvoice.supplier_id)
        .filter(
            SupplierInvoice.organization_id == org_id,
            SupplierInvoice.status == "recorded",
            SupplierInvoice.grand_total > SupplierInvoice.amount_paid,
        )
    )

    if supplier_id:
        q = q.filter(SupplierInvoice.supplier_id == supplier_id)
    if payment_status:
        q = q.filter(SupplierInvoice.payment_status == payment_status.lower())
    if verification_status:
        q = q.filter(SupplierInvoice.verification_status == verification_status.lower())
    if overdue_only:
        start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        q = q.filter(
            SupplierInvoice.due_date.isnot(None),
            SupplierInvoice.due_date < start_of_today,
        )
    if search:
        s = f"%{search}%"
        q = q.filter(
            or_(
                SupplierInvoice.supplier_invoice_number.ilike(s),
                Supplier.name.ilike(s),
            )
        )

    return q.order_by(SupplierInvoice.supplier_invoice_date.asc(), SupplierInvoice.id.asc()).all()


def get_accounts_payable_list(
    db: Session,
    org_id: str,
    supplier_id: str | None = None,
    payment_status: str | None = None,
    verification_status: str | None = None,
    overdue_only: bool = False,
    search: str | None = None,
    page: int | None = None,
    page_size: int | None = None,
) -> APListResponse:
    """Build complete AP list response with header summary and aging buckets."""
    now = datetime.now(timezone.utc)
    invoices = query_open_payables(
        db,
        org_id,
        supplier_id=supplier_id,
        payment_status=payment_status,
        verification_status=verification_status,
        overdue_only=overdue_only,
        search=search,
    )

    items: list[APItemOut] = []
    total_payable = 0.0
    total_overdue = 0.0
    total_due_today = 0.0
    suppliers_set = set()

    ageing = {
        "0_30": 0.0,
        "31_60": 0.0,
        "61_90": 0.0,
        "90_plus": 0.0,
    }

    for inv in invoices:
        ap_item = build_ap_item(inv, now)
        items.append(ap_item)

        out_amt = ap_item.outstanding_amount
        total_payable = round(total_payable + out_amt, 2)
        suppliers_set.add(inv.supplier_id)

        if ap_item.is_overdue:
            total_overdue = round(total_overdue + out_amt, 2)
            if ap_item.ageing_bucket in ageing:
                ageing[ap_item.ageing_bucket] = round(ageing[ap_item.ageing_bucket] + out_amt, 2)

        due = _aware(inv.due_date)
        if due is not None and due.date() == now.date():
            total_due_today = round(total_due_today + out_amt, 2)

    summary = APSummaryOut(
        total_payable=round(total_payable, 2),
        total_overdue=round(total_overdue, 2),
        total_due_today=round(total_due_today, 2),
        open_invoice_count=len(items),
        supplier_count=len(suppliers_set),
    )

    ageing_out = APAgeingBucketOut(
        bucket_0_30=round(ageing["0_30"], 2),
        bucket_31_60=round(ageing["31_60"], 2),
        bucket_61_90=round(ageing["61_90"], 2),
        bucket_90_plus=round(ageing["90_plus"], 2),
    )

    if page is not None and page_size is not None:
        start = (page - 1) * page_size
        end = start + page_size
        paginated_items = items[start:end]
    else:
        paginated_items = items

    return APListResponse(summary=summary, ageing=ageing_out, items=paginated_items)


def get_supplier_ap_statement(
    db: Session,
    org_id: str,
    supplier_id: str,
) -> SupplierAPStatementOut:
    """Build supplier-specific Accounts Payable statement."""
    supplier = db.get(Supplier, supplier_id)
    if supplier is None or supplier.organization_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Supplier not found",
        )

    now = datetime.now(timezone.utc)
    invoices = query_open_payables(db, org_id, supplier_id=supplier_id)

    items: list[APItemOut] = []
    total_payable = 0.0
    total_overdue = 0.0

    for inv in invoices:
        ap_item = build_ap_item(inv, now)
        items.append(ap_item)

        out_amt = ap_item.outstanding_amount
        total_payable = round(total_payable + out_amt, 2)
        if ap_item.is_overdue:
            total_overdue = round(total_overdue + out_amt, 2)

    return SupplierAPStatementOut(
        supplier_id=supplier.id,
        supplier_name=supplier.name,
        total_open_payable=round(total_payable, 2),
        total_overdue=round(total_overdue, 2),
        open_invoice_count=len(items),
        invoices=items,
    )
