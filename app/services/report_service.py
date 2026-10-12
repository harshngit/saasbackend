"""Financial / transaction / inventory report builders.

Complete reporting suite providing 15 report types with:
- Standardized response contract: { type, date_from, date_to, summary, rows, pagination, meta, chart }
- Robust business timezone date-range conversion (defaulting to Asia/Kolkata)
- Exact accounting sources of truth (issued invoices for sales, approved supplier invoices for purchases,
  canonical SalesReturn/PurchaseReturn models, COGS-based P&L, canonical AP/AR aging buckets)
- Strict tenant isolation on every query and entity filter
- Server-side pagination with full-dataset summary calculations
- Stable column metadata and structured chart trend models
- 100% backward compatibility for existing frontend calls
"""

from collections import defaultdict
from datetime import date, datetime, time, timezone
import math
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.models import (
    Customer,
    CustomerPayment,
    Delivery,
    DeliveryItem,
    Expense,
    Invoice,
    InvoiceItem,
    Product,
    ProductPricing,
    ProductVariant,
    PurchaseInvoice,
    PurchaseReturn,
    ReturnItem,
    SalesOrder,
    SalesOrderItem,
    SalesReturn,
    StockMovement,
    StockReservation,
    Supplier,
    SupplierInvoice,
    SupplierPayment,
    User,
    Vehicle,
    Warehouse,
    WarehouseStock,
)
from app.models.organization import Organization
from app.services.numbering_service import DEFAULT_BUSINESS_TIMEZONE
from app.core.workflow import SALE_ORDER_STATUSES as SALE_STATUSES

# All supported report types
REPORT_TYPES = {
    "daily-transaction",
    "sales",
    "purchase",
    "customer-outstanding",
    "supplier-outstanding",
    "payment-collection",
    "expense",
    "cash-collection",
    "gst-summary",
    "sales-return",
    "purchase-return",
    "profit-loss",
    "supplier-payment",
    "inventory-summary",
    "stock-movement",
    "sales-overview",
    "cash-flow-sheet",
}

# --------------------------------- Registry ---------------------------------

REPORT_REGISTRY: dict[str, dict[str, Any]] = {
    "daily-transaction": {
        "title": "Daily Transaction Report",
        "description": "Ledger of sales, purchases, customer collections, supplier payments, and expenses for a date range with strict separation of accrual bookings and actual cash flows.",
        "supported_filters": ["date_from", "date_to", "search", "page", "page_size"],
        "allowed_group_by": [],
        "currency": "INR",
    },
    "sales": {
        "title": "Sales Report",
        "description": "Realised sales based on issued, non-void tax invoices, with transaction, customer, product, and salesperson groupings.",
        "supported_filters": [
            "date_from", "date_to", "customer_id", "product_id", "salesperson_id",
            "warehouse_id", "payment_status", "search", "group_by", "page", "page_size",
        ],
        "allowed_group_by": ["transaction", "customer", "product", "salesperson"],
        "currency": "INR",
    },
    "purchase": {
        "title": "Purchase Report",
        "description": "Financially recognized purchases based on approved/recorded supplier purchase invoices.",
        "supported_filters": [
            "date_from", "date_to", "supplier_id", "product_id", "warehouse_id",
            "status", "search", "group_by", "page", "page_size",
        ],
        "allowed_group_by": ["transaction", "supplier", "product"],
        "currency": "INR",
    },
    "customer-outstanding": {
        "title": "Customer Outstanding (Receivables) Report",
        "description": "Accounts receivable aging and open invoice balances per customer, with 0-30, 31-60, 61-90, 90+ aging buckets.",
        "supported_filters": [
            "customer_id", "search", "overdue_only", "ageing_bucket",
            "as_of_date", "date_from", "date_to", "page", "page_size",
        ],
        "allowed_group_by": [],
        "currency": "INR",
    },
    "supplier-outstanding": {
        "title": "Supplier Outstanding (Payables) Report",
        "description": "Accounts payable aging and open supplier invoice balances with Purchase Return deductions and aging brackets.",
        "supported_filters": [
            "supplier_id", "payment_status", "verification_status", "overdue_only",
            "ageing_bucket", "search", "as_of_date", "date_from", "date_to", "page", "page_size",
        ],
        "allowed_group_by": [],
        "currency": "INR",
    },
    "payment-collection": {
        "title": "Payment Collection Report",
        "description": "Confirmed customer collections and receipts with payment mode breakdown.",
        "supported_filters": [
            "customer_id", "payment_mode", "date_from", "date_to", "search", "page", "page_size",
        ],
        "allowed_group_by": [],
        "currency": "INR",
    },
    "expense": {
        "title": "Expense Report",
        "description": "Operating expense records categorized and audited by approval status (approved, pending, rejected), tax, and TDS.",
        "supported_filters": [
            "category", "status", "payment_mode", "vendor_id", "submitted_by",
            "search", "date_from", "date_to", "page", "page_size",
        ],
        "allowed_group_by": [],
        "currency": "INR",
    },
    "cash-collection": {
        "title": "Cash Collection Report",
        "description": "Strict cash collections including pure cash receipts and the exact cash component of split payments.",
        "supported_filters": ["customer_id", "date_from", "date_to", "search", "page", "page_size"],
        "allowed_group_by": [],
        "currency": "INR",
    },
    "gst-summary": {
        "title": "GST Summary Report",
        "description": "Output GST from issued tax invoices minus sales returns, and Input GST from recorded purchase invoices minus purchase returns. group_by=transaction lists the underlying documents.",
        "supported_filters": ["date_from", "date_to", "group_by", "page", "page_size"],
        "transaction_filters": [
            "date_from", "date_to", "customer_id", "supplier_id", "search", "invoice_number",
            "gst_number", "gst_classification", "invoice_type", "status",
            "min_amount", "max_amount", "min_taxable_value", "max_taxable_value",
            "min_tax_amount", "max_tax_amount", "sort_by", "sort_order", "group_by", "page", "page_size",
        ],
        "allowed_group_by": ["summary", "transaction"],
        "currency": "INR",
    },
    "sales-return": {
        "title": "Sales Return Report",
        "description": "Audited sales returns and credit notes issued against returned goods.",
        "supported_filters": ["customer_id", "status", "search", "date_from", "date_to", "page", "page_size"],
        "allowed_group_by": [],
        "currency": "INR",
    },
    "purchase-return": {
        "title": "Purchase Return Report",
        "description": "Goods returned to suppliers against purchase invoices with debit note adjustments.",
        "supported_filters": ["supplier_id", "status", "purchase_id", "search", "date_from", "date_to", "page", "page_size"],
        "allowed_group_by": [],
        "currency": "INR",
    },
    "profit-loss": {
        "title": "Profit & Loss Statement",
        "description": "P&L Statement calculating Net Sales (Gross Invoices minus Sales Returns), COGS (sold item quantities multiplied by cost snapshots), Gross Profit, Operating Expenses (approved only), and Net Profit.",
        "supported_filters": ["date_from", "date_to", "page", "page_size"],
        "allowed_group_by": [],
        "currency": "INR",
    },
    "supplier-payment": {
        "title": "Supplier Payment Report",
        "description": "Disbursements and payments made to suppliers, detailing allocated vs unallocated balances and excluding voided payments.",
        "supported_filters": [
            "supplier_id", "payment_method", "payment_mode", "status",
            "date_from", "date_to", "search", "page", "page_size",
        ],
        "allowed_group_by": [],
        "currency": "INR",
    },
    "inventory-summary": {
        "title": "Inventory Summary Report",
        "description": "Current stock valuation, warehouse on-hand, reserved, and available quantities per SKU/variant with low stock alerts (Current cost valuation basis).",
        "supported_filters": [
            "warehouse_id", "product_id", "category_id", "brand_id",
            "low_stock_only", "search", "page", "page_size",
        ],
        "allowed_group_by": [],
        "currency": "INR",
    },
    "stock-movement": {
        "title": "Stock Movement Audit Ledger",
        "description": "Granular audit trail of all physical inventory movements (openings, purchases, sales, returns, transfers, adjustments) with signed quantities and running balances.",
        "supported_filters": [
            "warehouse_id", "product_id", "movement_type", "date_from", "date_to",
            "search", "page", "page_size",
        ],
        "allowed_group_by": [],
        "currency": "INR",
    },
    "sales-overview": {
        "title": "Sales Overview (Orders & Deliveries)",
        "description": "Operational sales order progression and delivery fulfillment tracking per order, ordered vs delivered quantities, and logistics status.",
        "supported_filters": [
            "date_from", "date_to", "customer_id", "salesperson_id", "order_status", "status",
            "fulfilment_status", "delivery_status", "search", "page", "page_size",
        ],
        "allowed_group_by": [],
        "currency": "INR",
    },
    "cash-flow-sheet": {
        "title": "Cash Flow Sheet (Recorded Cash Movements)",
        "description": "Chronological audit ledger of recorded cash inflows (Customer Payments) and cash outflows (Supplier Payments and settled Operating Expenses) with period running movement.",
        "supported_filters": ["date_from", "date_to", "search", "page", "page_size"],
        "allowed_group_by": [],
        "currency": "INR",
    },
}

# --------------------------------- Timezone / Helpers ---------------------------------

def get_org_timezone(db: Session, org_id: str) -> ZoneInfo:
    """Resolve the organization's business timezone or fall back to Asia/Kolkata."""
    try:
        org = db.get(Organization, org_id)
        if org and org.timezone:
            return ZoneInfo(org.timezone)
    except Exception:
        pass
    try:
        return ZoneInfo(DEFAULT_BUSINESS_TIMEZONE)
    except Exception:
        return ZoneInfo("UTC")


def _range(
    date_from: str | None,
    date_to: str | None,
    db: Session | None = None,
    org_id: str | None = None,
) -> tuple[datetime | None, datetime | None]:
    """Parse business date strings (YYYY-MM-DD) into exact UTC timestamp boundaries."""
    if not date_from and not date_to:
        return None, None
    try:
        d_from = date.fromisoformat(date_from) if date_from else None
        d_to = date.fromisoformat(date_to) if date_to else None
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Dates must be YYYY-MM-DD")
    if d_from and d_to and d_from > d_to:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="date_from cannot be after date_to")

    tz = get_org_timezone(db, org_id) if (db and org_id) else ZoneInfo(DEFAULT_BUSINESS_TIMEZONE)
    df_utc = None
    dt_utc = None
    if d_from:
        start_dt = datetime.combine(d_from, time.min).replace(tzinfo=tz)
        df_utc = start_dt.astimezone(timezone.utc)
    if d_to:
        end_dt = datetime.combine(d_to, time.max).replace(tzinfo=tz)
        dt_utc = end_dt.astimezone(timezone.utc)
    return df_utc, dt_utc


def _between(query, col, df, dt):
    if df is not None:
        query = query.filter(col >= df)
    if dt is not None:
        query = query.filter(col <= dt)
    return query


def _paginate(rows: list[dict[str, Any]], page: int, page_size: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Helper to paginate in-memory or query-derived row lists cleanly.
    Screen queries are bounded to 100 rows per page; exports can retrieve up to 10,000 rows.
    """
    total = len(rows)
    page_size = max(1, min(page_size, 10000))
    page = max(1, page)
    total_pages = math.ceil(total / page_size) if total > 0 else 0
    start = (page - 1) * page_size
    end = start + page_size
    paged_rows = rows[start:end]
    pagination = {
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
    }
    return paged_rows, pagination


def _expense_vendor_name(expense: Expense) -> str | None:
    """`Expense.vendor` is the Supplier relationship; reports show its name, else the payee."""
    return expense.vendor.name if expense.vendor else expense.payee_name


def _verify_tenant_entity(db: Session, org_id: str, model_cls, entity_id: str, entity_label: str) -> None:
    """Ensure entity belongs to organization to prevent cross-tenant filtering leakage."""
    item = db.query(model_cls.id).filter(model_cls.id == entity_id, model_cls.organization_id == org_id).first()
    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{entity_label} '{entity_id}' not found in organization",
        )


# --------------------------------- BUILDERS ---------------------------------

def _daily_transaction(db: Session, org_id: str, df: datetime | None, dt: datetime | None, params: dict) -> dict:
    page = int(params.get("page", 1))
    page_size = int(params.get("page_size", 25))
    search = params.get("search")

    # Invoices (Accrual Realised Sales)
    inv_q = db.query(Invoice).filter(
        Invoice.organization_id == org_id,
        Invoice.is_credit_note.is_(False),
        ~Invoice.status.in_(("void", "cancelled")),
    )
    inv_q = _between(inv_q, Invoice.invoice_date, df, dt)
    invoices = inv_q.all()

    # Purchases (Accrual Purchases)
    purch_q = db.query(SupplierInvoice).filter(
        SupplierInvoice.organization_id == org_id,
        SupplierInvoice.status == "recorded",
    )
    purch_q = _between(purch_q, SupplierInvoice.supplier_invoice_date, df, dt)
    purchases = purch_q.all()

    # Customer Payments (Cash Inflow)
    pay_in_q = db.query(CustomerPayment).filter(
        CustomerPayment.organization_id == org_id,
    )
    pay_in_q = _between(pay_in_q, CustomerPayment.received_on, df, dt)
    customer_payments = pay_in_q.all()

    # Supplier Payments (Cash Outflow)
    pay_out_q = db.query(SupplierPayment).filter(
        SupplierPayment.organization_id == org_id,
        SupplierPayment.status != "void",
    )
    pay_out_q = _between(pay_out_q, SupplierPayment.paid_on, df, dt)
    supplier_payments = pay_out_q.all()

    # Expenses (Accrual activity and Actual Cash Outflow)
    exp_q = db.query(Expense).filter(
        Expense.organization_id == org_id,
        Expense.status == "approved",
    )
    exp_q = _between(exp_q, Expense.expense_date, df, dt)
    expenses = exp_q.all()

    def _is_paid_expense(e: Expense) -> bool:
        ps = (e.payment_status or "").strip().lower()
        return ps in ("paid", "settled", "completed")

    all_rows = []

    for inv in invoices:
        party_name = inv.customer.name if inv.customer else (inv.walk_in_name or "Walk-in")
        all_rows.append({
            "date": inv.invoice_date.date().isoformat() if hasattr(inv.invoice_date, "date") else str(inv.invoice_date)[:10],
            "type": "Sale Invoice",
            "reference": inv.invoice_number,
            "party": party_name,
            "amount": round(inv.total, 2),
            "payment_mode": inv.payment_status or "unpaid",
            "direction": "Accrual Sale",
            "category": "Revenue",
        })

    for p in purchases:
        supplier_name = p.supplier.name if p.supplier else "Unknown Supplier"
        all_rows.append({
            "date": p.supplier_invoice_date.date().isoformat() if hasattr(p.supplier_invoice_date, "date") else str(p.supplier_invoice_date)[:10],
            "type": "Purchase Invoice",
            "reference": p.supplier_invoice_number,
            "party": supplier_name,
            "amount": round(-p.grand_total, 2),
            "payment_mode": p.payment_status or "unpaid",
            "direction": "Accrual Purchase",
            "category": "Inventory / AP",
        })

    for cp in customer_payments:
        cust = db.get(Customer, cp.customer_id) if cp.customer_id else None
        party_name = (cust.business_name or cust.name) if cust else "Unknown Customer"
        all_rows.append({
            "date": cp.received_on.date().isoformat() if hasattr(cp.received_on, "date") else str(cp.received_on)[:10],
            "type": "Customer Payment",
            "reference": cp.reference or cp.receipt_number or cp.id[:8],
            "party": party_name,
            "amount": round(cp.amount, 2),
            "payment_mode": cp.payment_mode or "cash",
            "direction": "Cash In",
            "category": "Receivable Inflow",
        })

    for sp in supplier_payments:
        supplier_name = sp.supplier.name if sp.supplier else "Unknown Supplier"
        all_rows.append({
            "date": sp.paid_on.date().isoformat() if hasattr(sp.paid_on, "date") else str(sp.paid_on)[:10],
            "type": "Supplier Payment",
            "reference": sp.payment_number or sp.reference or sp.id[:8],
            "party": supplier_name,
            "amount": round(-sp.amount, 2),
            "payment_mode": sp.payment_mode or sp.payment_method or "cash",
            "direction": "Cash Out",
            "category": "Payable Outflow",
        })

    for exp in expenses:
        is_paid = _is_paid_expense(exp)
        all_rows.append({
            "date": exp.expense_date.date().isoformat() if hasattr(exp.expense_date, "date") else str(exp.expense_date)[:10],
            "type": "Expense",
            "reference": exp.category,
            "party": _expense_vendor_name(exp) or exp.submitted_by or "Expense",
            "amount": round(-exp.amount, 2),
            "payment_mode": exp.payment_mode or (exp.payment_status or "unpaid"),
            "direction": "Cash Out" if is_paid else "Accrual Expense",
            "category": exp.category or "Operating Expense",
        })

    # Filter search
    if search:
        s_lower = search.lower()
        all_rows = [
            r for r in all_rows
            if s_lower in (r["reference"] or "").lower()
            or s_lower in (r["party"] or "").lower()
            or s_lower in (r["type"] or "").lower()
            or s_lower in (r["category"] or "").lower()
        ]

    all_rows.sort(key=lambda r: r["date"], reverse=True)

    sales_total = round(sum(inv.total for inv in invoices), 2)
    purchases_total = round(sum(p.grand_total for p in purchases), 2)
    customer_collections = round(sum(cp.amount for cp in customer_payments), 2)
    supplier_payments_total = round(sum(sp.amount for sp in supplier_payments), 2)
    expenses_total = round(sum(e.amount for e in expenses), 2)
    paid_expenses_total = round(sum(e.amount for e in expenses if _is_paid_expense(e)), 2)

    cash_in = customer_collections
    cash_out = round(supplier_payments_total + paid_expenses_total, 2)
    net_cash_flow = round(cash_in - cash_out, 2)

    summary = {
        "sales": sales_total,
        "purchases": purchases_total,
        "customer_collections": customer_collections,
        "supplier_payments": supplier_payments_total,
        "expenses": expenses_total,
        "paid_expenses": paid_expenses_total,
        "cash_in": cash_in,
        "cash_out": cash_out,
        "net_cash_flow": net_cash_flow,
        "activity_count": len(all_rows),
    }

    paged_rows, pagination = _paginate(all_rows, page, page_size)

    meta = {
        "currency": "INR",
        "group_by": None,
        "columns": [
            {"key": "date", "label": "Date", "type": "date"},
            {"key": "type", "label": "Transaction Type", "type": "text"},
            {"key": "reference", "label": "Reference #", "type": "reference"},
            {"key": "party", "label": "Party / Payee", "type": "text"},
            {"key": "amount", "label": "Amount", "type": "currency"},
            {"key": "payment_mode", "label": "Payment Mode", "type": "text"},
            {"key": "direction", "label": "Flow / Direction", "type": "status"},
            {"key": "category", "label": "Category", "type": "text"},
        ],
    }

    chart = {
        "type": "bar",
        "title": "Daily Cash Flow Comparison",
        "data": [
            {"name": "Cash In (Collections)", "value": cash_in},
            {"name": "Cash Out (Disbursements & Expenses)", "value": cash_out},
            {"name": "Net Cash Flow", "value": net_cash_flow},
            {"name": "Accrual Sales", "value": sales_total},
            {"name": "Accrual Purchases", "value": purchases_total},
        ],
    }

    return {
        "summary": summary,
        "rows": paged_rows,
        "pagination": pagination,
        "meta": meta,
        "chart": chart,
    }


def _sales(db: Session, org_id: str, df: datetime | None, dt: datetime | None, params: dict) -> dict:
    page = int(params.get("page", 1))
    page_size = int(params.get("page_size", 25))
    search = params.get("search")
    group_by = params.get("group_by") or "transaction"
    customer_id = params.get("customer_id")
    product_id = params.get("product_id")
    salesperson_id = params.get("salesperson_id")
    payment_status = params.get("payment_status")
    warehouse_id = params.get("warehouse_id")

    if group_by not in REPORT_REGISTRY["sales"]["allowed_group_by"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid group_by '{group_by}' for sales report. Allowed: {REPORT_REGISTRY['sales']['allowed_group_by']}",
        )

    if customer_id:
        _verify_tenant_entity(db, org_id, Customer, customer_id, "Customer")
    if product_id:
        _verify_tenant_entity(db, org_id, Product, product_id, "Product")
    if salesperson_id:
        _verify_tenant_entity(db, org_id, User, salesperson_id, "Salesperson")
    if warehouse_id:
        _verify_tenant_entity(db, org_id, Warehouse, warehouse_id, "Warehouse")

    q = db.query(Invoice).filter(
        Invoice.organization_id == org_id,
        Invoice.is_credit_note.is_(False),
        ~Invoice.status.in_(("void", "cancelled")),
    )
    q = _between(q, Invoice.invoice_date, df, dt)

    if customer_id:
        q = q.filter(Invoice.customer_id == customer_id)
    if payment_status:
        q = q.filter(or_(Invoice.payment_status == payment_status, Invoice.status == payment_status))
    if salesperson_id:
        q = q.join(SalesOrder, SalesOrder.id == Invoice.order_id, isouter=True).filter(
            or_(SalesOrder.salesperson_id == salesperson_id, Invoice.created_by == salesperson_id)
        )
    if product_id:
        q = q.join(InvoiceItem, InvoiceItem.invoice_id == Invoice.id).filter(InvoiceItem.product_id == product_id)
    if warehouse_id:
        # An invoice ships from its delivery's warehouse; without a delivery (or when the
        # delivery has none) it falls back to the warehouse its sales order is promised from.
        # Invoices with neither (direct / POS invoices) are not attributable to any warehouse
        # and are excluded while this filter is active. Subqueries keep one row per invoice.
        deliveries_from_wh = select(Delivery.id).where(
            Delivery.organization_id == org_id, Delivery.warehouse_id == warehouse_id
        )
        deliveries_without_wh = select(Delivery.id).where(
            Delivery.organization_id == org_id, Delivery.warehouse_id.is_(None)
        )
        orders_from_wh = select(SalesOrder.id).where(
            SalesOrder.organization_id == org_id, SalesOrder.warehouse_id == warehouse_id
        )
        q = q.filter(
            or_(
                Invoice.delivery_id.in_(deliveries_from_wh),
                and_(
                    or_(Invoice.delivery_id.is_(None), Invoice.delivery_id.in_(deliveries_without_wh)),
                    Invoice.order_id.in_(orders_from_wh),
                ),
            )
        )

    if search:
        s = f"%{search}%"
        q = q.join(Customer, Customer.id == Invoice.customer_id, isouter=True).filter(
            or_(
                Invoice.invoice_number.ilike(s),
                Customer.name.ilike(s),
                Customer.business_name.ilike(s),
                Invoice.walk_in_name.ilike(s),
            )
        )

    invoices = q.order_by(Invoice.invoice_date.desc(), Invoice.id.desc()).all()

    total_sales = round(sum(i.total for i in invoices), 2)
    total_paid = round(sum(i.amount_paid for i in invoices), 2)
    total_outstanding = round(sum(max(i.total - i.amount_paid, 0.0) for i in invoices), 2)
    total_tax = round(sum(i.tax for i in invoices), 2)
    total_discount = round(sum(i.discount for i in invoices), 2)

    summary = {
        "total_sales": total_sales,
        "total_invoices": len(invoices),
        "total_paid": total_paid,
        "total_outstanding": total_outstanding,
        "total_tax": total_tax,
        "total_discount": total_discount,
    }

    if group_by == "customer":
        cust_map: dict[str, dict[str, Any]] = defaultdict(lambda: {"invoice_count": 0, "net_sales": 0.0, "collected": 0.0, "outstanding": 0.0})
        for i in invoices:
            name = (i.customer.business_name or i.customer.name) if i.customer else (i.walk_in_name or "Walk-in")
            cust_map[name]["invoice_count"] += 1
            cust_map[name]["net_sales"] = round(cust_map[name]["net_sales"] + i.total, 2)
            cust_map[name]["collected"] = round(cust_map[name]["collected"] + i.amount_paid, 2)
            cust_map[name]["outstanding"] = round(cust_map[name]["outstanding"] + max(i.total - i.amount_paid, 0.0), 2)

        rows = [{"customer": k, **v} for k, v in sorted(cust_map.items(), key=lambda x: x[1]["net_sales"], reverse=True)]
        columns = [
            {"key": "customer", "label": "Customer", "type": "text"},
            {"key": "invoice_count", "label": "Invoices", "type": "number"},
            {"key": "net_sales", "label": "Net Sales", "type": "currency"},
            {"key": "collected", "label": "Collected", "type": "currency"},
            {"key": "outstanding", "label": "Outstanding", "type": "currency"},
        ]

    elif group_by == "salesperson":
        sp_map: dict[str, dict[str, Any]] = defaultdict(lambda: {"transaction_count": 0, "sales_value": 0.0})
        # One lookup for every creator needed as a fallback, instead of one per invoice.
        creator_ids = {
            i.created_by for i in invoices
            if i.created_by and not (i.order and i.order.salesperson)
        }
        creator_names = (
            dict(db.query(User.id, User.name).filter(User.id.in_(creator_ids)).all())
            if creator_ids else {}
        )
        for i in invoices:
            sp_name = "Unassigned"
            if i.order and i.order.salesperson:
                sp_name = i.order.salesperson.name
            elif i.created_by and i.created_by in creator_names:
                sp_name = creator_names[i.created_by]
            sp_map[sp_name]["transaction_count"] += 1
            sp_map[sp_name]["sales_value"] = round(sp_map[sp_name]["sales_value"] + i.total, 2)

        rows = [{"salesperson": k, **v} for k, v in sorted(sp_map.items(), key=lambda x: x[1]["sales_value"], reverse=True)]
        columns = [
            {"key": "salesperson", "label": "Salesperson", "type": "text"},
            {"key": "transaction_count", "label": "Transactions", "type": "number"},
            {"key": "sales_value", "label": "Sales Value", "type": "currency"},
        ]

    elif group_by == "product":
        prod_map: dict[str, dict[str, Any]] = defaultdict(lambda: {"quantity_sold": 0.0, "sales_value": 0.0, "cogs": 0.0, "gross_profit": 0.0})
        for i in invoices:
            for item in i.items:
                if product_id and item.product_id != product_id:
                    continue
                p_name = item.product_name or (item.product.name if item.product else "Unknown Product")
                qty = float(item.quantity or 0)
                val = float(item.line_total or (qty * (item.unit_price or 0)))
                # derive cost price strictly from SalesOrderItem snapshot (never fabricate catalog pricing)
                cost = 0.0
                if item.order_item_id:
                    so_item = db.get(SalesOrderItem, item.order_item_id)
                    if so_item and so_item.cost_price is not None and so_item.cost_price > 0:
                        cost = round(so_item.cost_price * qty, 2)

                prod_map[p_name]["quantity_sold"] = round(prod_map[p_name]["quantity_sold"] + qty, 2)
                prod_map[p_name]["sales_value"] = round(prod_map[p_name]["sales_value"] + val, 2)
                prod_map[p_name]["cogs"] = round(prod_map[p_name]["cogs"] + cost, 2)
                prod_map[p_name]["gross_profit"] = round(prod_map[p_name]["sales_value"] - prod_map[p_name]["cogs"], 2)

        rows = [{"product": k, **v} for k, v in sorted(prod_map.items(), key=lambda x: x[1]["sales_value"], reverse=True)]
        columns = [
            {"key": "product", "label": "Product", "type": "text"},
            {"key": "quantity_sold", "label": "Qty Sold", "type": "number"},
            {"key": "sales_value", "label": "Sales Value", "type": "currency"},
            {"key": "cogs", "label": "COGS", "type": "currency"},
            {"key": "gross_profit", "label": "Gross Profit", "type": "currency"},
        ]

    else:  # transaction
        rows = [
            {
                "invoice_number": i.invoice_number,
                "invoice_date": i.invoice_date.date().isoformat() if hasattr(i.invoice_date, "date") else str(i.invoice_date)[:10],
                "customer": (i.customer.business_name or i.customer.name) if i.customer else (i.walk_in_name or "Walk-in"),
                "order_number": i.order.order_number if i.order else None,
                "salesperson": i.order.salesperson.name if (i.order and i.order.salesperson) else None,
                "subtotal": round(i.subtotal, 2),
                "discount": round(i.discount, 2),
                "tax": round(i.tax, 2),
                "total": round(i.total, 2),
                "paid": round(i.amount_paid, 2),
                "outstanding": round(max(i.total - i.amount_paid, 0.0), 2),
                "payment_status": i.payment_status or i.status,
            }
            for i in invoices
        ]
        columns = [
            {"key": "invoice_number", "label": "Invoice #", "type": "reference"},
            {"key": "invoice_date", "label": "Date", "type": "date"},
            {"key": "customer", "label": "Customer", "type": "text"},
            {"key": "order_number", "label": "Order #", "type": "reference"},
            {"key": "salesperson", "label": "Salesperson", "type": "text"},
            {"key": "subtotal", "label": "Subtotal", "type": "currency"},
            {"key": "discount", "label": "Discount", "type": "currency"},
            {"key": "tax", "label": "Tax", "type": "currency"},
            {"key": "total", "label": "Total", "type": "currency"},
            {"key": "paid", "label": "Paid", "type": "currency"},
            {"key": "outstanding", "label": "Outstanding", "type": "currency"},
            {"key": "payment_status", "label": "Status", "type": "status"},
        ]

    paged_rows, pagination = _paginate(rows, page, page_size)

    # Chart data
    daily_sales: dict[str, float] = defaultdict(float)
    for i in invoices:
        d_str = i.invoice_date.date().isoformat() if hasattr(i.invoice_date, "date") else str(i.invoice_date)[:10]
        daily_sales[d_str] += i.total

    chart = {
        "type": "line",
        "title": "Daily Sales Trend",
        "data": [{"date": d, "sales": round(amt, 2)} for d, amt in sorted(daily_sales.items())],
    }

    return {
        "summary": summary,
        "rows": paged_rows,
        "pagination": pagination,
        "meta": {"currency": "INR", "group_by": group_by, "columns": columns},
        "chart": chart,
    }


def _purchase(db: Session, org_id: str, df: datetime | None, dt: datetime | None, params: dict) -> dict:
    page = int(params.get("page", 1))
    page_size = int(params.get("page_size", 25))
    search = params.get("search")
    group_by = params.get("group_by") or "transaction"
    supplier_id = params.get("supplier_id")
    product_id = params.get("product_id")
    p_status = params.get("status")
    warehouse_id = params.get("warehouse_id")

    if group_by not in REPORT_REGISTRY["purchase"]["allowed_group_by"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid group_by '{group_by}' for purchase report. Allowed: {REPORT_REGISTRY['purchase']['allowed_group_by']}",
        )

    if supplier_id:
        _verify_tenant_entity(db, org_id, Supplier, supplier_id, "Supplier")
    if product_id:
        _verify_tenant_entity(db, org_id, Product, product_id, "Product")
    if warehouse_id:
        _verify_tenant_entity(db, org_id, Warehouse, warehouse_id, "Warehouse")

    q = db.query(SupplierInvoice).filter(
        SupplierInvoice.organization_id == org_id,
        SupplierInvoice.status == "recorded",
    )
    q = _between(q, SupplierInvoice.supplier_invoice_date, df, dt)

    if supplier_id:
        q = q.filter(SupplierInvoice.supplier_id == supplier_id)
    if warehouse_id:
        # The receiving warehouse is recorded on the purchase header (the same field the
        # purchases list filters on). Supplier invoices whose purchase has no receiving
        # warehouse are excluded while this filter is active.
        q = q.filter(
            SupplierInvoice.purchase_id.in_(
                select(PurchaseInvoice.id).where(
                    PurchaseInvoice.organization_id == org_id,
                    PurchaseInvoice.warehouse_id == warehouse_id,
                )
            )
        )
    if p_status:
        q = q.filter(or_(SupplierInvoice.payment_status == p_status, SupplierInvoice.verification_status == p_status))
    if search:
        s = f"%{search}%"
        q = q.join(Supplier, Supplier.id == SupplierInvoice.supplier_id, isouter=True).filter(
            or_(
                SupplierInvoice.supplier_invoice_number.ilike(s),
                Supplier.name.ilike(s),
            )
        )

    invoices = q.order_by(SupplierInvoice.supplier_invoice_date.desc(), SupplierInvoice.id.desc()).all()

    total_purchases = round(sum(i.grand_total for i in invoices), 2)
    total_paid = round(sum(i.amount_paid for i in invoices), 2)
    total_outstanding = round(sum(i.outstanding_amount for i in invoices), 2)
    total_tax = round(sum(i.tax_amount or 0.0 for i in invoices), 2)

    summary = {
        "total_purchases": total_purchases,
        "total_invoices": len(invoices),
        "total_paid": total_paid,
        "total_outstanding": total_outstanding,
        "total_tax": total_tax,
    }

    if group_by == "supplier":
        sup_map: dict[str, dict[str, Any]] = defaultdict(lambda: {"transaction_count": 0, "purchase_value": 0.0, "paid_amount": 0.0, "outstanding": 0.0})
        for i in invoices:
            s_name = i.supplier.name if i.supplier else "Unknown Supplier"
            sup_map[s_name]["transaction_count"] += 1
            sup_map[s_name]["purchase_value"] = round(sup_map[s_name]["purchase_value"] + i.grand_total, 2)
            sup_map[s_name]["paid_amount"] = round(sup_map[s_name]["paid_amount"] + i.amount_paid, 2)
            sup_map[s_name]["outstanding"] = round(sup_map[s_name]["outstanding"] + i.outstanding_amount, 2)

        rows = [{"supplier": k, **v} for k, v in sorted(sup_map.items(), key=lambda x: x[1]["purchase_value"], reverse=True)]
        columns = [
            {"key": "supplier", "label": "Supplier", "type": "text"},
            {"key": "transaction_count", "label": "Invoices", "type": "number"},
            {"key": "purchase_value", "label": "Purchase Value", "type": "currency"},
            {"key": "paid_amount", "label": "Paid", "type": "currency"},
            {"key": "outstanding", "label": "Outstanding", "type": "currency"},
        ]

    elif group_by == "product":
        prod_map: dict[str, dict[str, Any]] = defaultdict(lambda: {"quantity_purchased": 0.0, "purchase_value": 0.0})
        for i in invoices:
            for item in (i.items or []):
                if product_id and item.product_id != product_id:
                    continue
                p_name = item.product_name or (item.product.name if item.product else "Unknown Product")
                qty = float(item.quantity or 0)
                val = float(item.total_amount or (qty * (item.unit_price or 0)))
                prod_map[p_name]["quantity_purchased"] = round(prod_map[p_name]["quantity_purchased"] + qty, 2)
                prod_map[p_name]["purchase_value"] = round(prod_map[p_name]["purchase_value"] + val, 2)

        rows = [{"product": k, **v} for k, v in sorted(prod_map.items(), key=lambda x: x[1]["purchase_value"], reverse=True)]
        columns = [
            {"key": "product", "label": "Product", "type": "text"},
            {"key": "quantity_purchased", "label": "Qty Purchased", "type": "number"},
            {"key": "purchase_value", "label": "Purchase Value", "type": "currency"},
        ]

    else:  # transaction
        rows = [
            {
                "invoice_number": i.supplier_invoice_number,
                "date": i.supplier_invoice_date.date().isoformat() if hasattr(i.supplier_invoice_date, "date") else str(i.supplier_invoice_date)[:10],
                "supplier": i.supplier.name if i.supplier else None,
                "subtotal": round(i.subtotal, 2),
                "tax": round(i.tax_amount or 0.0, 2),
                "total": round(i.grand_total, 2),
                "paid_amount": round(i.amount_paid, 2),
                "outstanding": round(i.outstanding_amount, 2),
                "status": i.payment_status or i.status,
            }
            for i in invoices
        ]
        columns = [
            {"key": "invoice_number", "label": "Invoice #", "type": "reference"},
            {"key": "date", "label": "Date", "type": "date"},
            {"key": "supplier", "label": "Supplier", "type": "text"},
            {"key": "subtotal", "label": "Subtotal", "type": "currency"},
            {"key": "tax", "label": "Tax", "type": "currency"},
            {"key": "total", "label": "Total", "type": "currency"},
            {"key": "paid_amount", "label": "Paid", "type": "currency"},
            {"key": "outstanding", "label": "Outstanding", "type": "currency"},
            {"key": "status", "label": "Status", "type": "status"},
        ]

    paged_rows, pagination = _paginate(rows, page, page_size)

    daily_purch: dict[str, float] = defaultdict(float)
    for i in invoices:
        d_str = i.supplier_invoice_date.date().isoformat() if hasattr(i.supplier_invoice_date, "date") else str(i.supplier_invoice_date)[:10]
        daily_purch[d_str] += i.grand_total

    chart = {
        "type": "line",
        "title": "Daily Purchase Trend",
        "data": [{"date": d, "purchases": round(amt, 2)} for d, amt in sorted(daily_purch.items())],
    }

    return {
        "summary": summary,
        "rows": paged_rows,
        "pagination": pagination,
        "meta": {"currency": "INR", "group_by": group_by, "columns": columns},
        "chart": chart,
    }


def _customer_outstanding(db: Session, org_id: str, df: datetime | None, dt: datetime | None, params: dict) -> dict:
    page = int(params.get("page", 1))
    page_size = int(params.get("page_size", 25))
    search = params.get("search")
    customer_id = params.get("customer_id")
    overdue_only = str(params.get("overdue_only", "")).lower() in ("true", "1", "yes")
    ageing_bucket = params.get("ageing_bucket")
    as_of_date = params.get("as_of_date")

    now = datetime.now(timezone.utc)
    if as_of_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Historical point-in-time balance reconstruction (as_of_date) is not supported for open customer receivables. Use transactional date_from/date_to reports for historical activity.",
        )

    now = datetime.now(timezone.utc)
    if customer_id:
        _verify_tenant_entity(db, org_id, Customer, customer_id, "Customer")

    # Query all open invoices for customers in the organization
    q = db.query(Invoice).filter(
        Invoice.organization_id == org_id,
        Invoice.is_credit_note.is_(False),
        ~Invoice.status.in_(("void", "cancelled")),
        Invoice.total > Invoice.amount_paid,
    )

    if customer_id:
        q = q.filter(Invoice.customer_id == customer_id)
    if search:
        s = f"%{search}%"
        q = q.join(Customer, Customer.id == Invoice.customer_id, isouter=True).filter(
            or_(
                Invoice.invoice_number.ilike(s),
                Customer.name.ilike(s),
                Customer.business_name.ilike(s),
                Customer.phone.ilike(s),
            )
        )

    invoices = q.order_by(Invoice.invoice_date.asc(), Invoice.id.asc()).all()

    rows = []
    total_outstanding = 0.0
    total_overdue = 0.0
    customers_set = set()
    age_buckets = {"0_30": 0.0, "31_60": 0.0, "61_90": 0.0, "90_plus": 0.0}

    for inv in invoices:
        outstanding = round(max(inv.total - inv.amount_paid, 0.0), 2)
        if outstanding <= 0:
            continue

        ref_date = inv.due_date or inv.invoice_date or now
        if ref_date.tzinfo is None:
            ref_date = ref_date.replace(tzinfo=timezone.utc)

        days_overdue = max((now.date() - ref_date.date()).days, 0)
        is_overdue = (now.date() > ref_date.date())

        if days_overdue <= 30:
            bucket = "0_30"
        elif days_overdue <= 60:
            bucket = "31_60"
        elif days_overdue <= 90:
            bucket = "61_90"
        else:
            bucket = "90_plus"

        # Track global summaries before row filter
        total_outstanding = round(total_outstanding + outstanding, 2)
        customers_set.add(inv.customer_id)
        if is_overdue:
            total_overdue = round(total_overdue + outstanding, 2)
            age_buckets[bucket] = round(age_buckets[bucket] + outstanding, 2)

        if overdue_only and not is_overdue:
            continue
        if ageing_bucket and bucket != ageing_bucket:
            continue

        cust = inv.customer
        rows.append({
            "customer_id": inv.customer_id,
            "customer": (cust.business_name or cust.name) if cust else (inv.walk_in_name or "Walk-in"),
            "phone": cust.phone if cust else inv.walk_in_phone,
            "invoice_number": inv.invoice_number,
            "invoice_date": inv.invoice_date.date().isoformat() if hasattr(inv.invoice_date, "date") else str(inv.invoice_date)[:10],
            "due_date": inv.due_date.date().isoformat() if (inv.due_date and hasattr(inv.due_date, "date")) else (str(inv.due_date)[:10] if inv.due_date else None),
            "billed": round(inv.total, 2),
            "received": round(inv.amount_paid, 2),
            "outstanding": outstanding,
            "days_overdue": days_overdue,
            "ageing_bucket": bucket,
            "credit_limit": cust.credit_limit if cust else None,
        })

    summary = {
        "total_outstanding": total_outstanding,
        "total_overdue": total_overdue,
        "customers_with_balance": len(customers_set),
        "age_0_30": age_buckets["0_30"],
        "age_31_60": age_buckets["31_60"],
        "age_61_90": age_buckets["61_90"],
        "age_90_plus": age_buckets["90_plus"],
    }

    paged_rows, pagination = _paginate(rows, page, page_size)

    meta = {
        "currency": "INR",
        "group_by": None,
        "columns": [
            {"key": "customer", "label": "Customer", "type": "text"},
            {"key": "phone", "label": "Phone", "type": "text"},
            {"key": "invoice_number", "label": "Invoice #", "type": "reference"},
            {"key": "invoice_date", "label": "Invoice Date", "type": "date"},
            {"key": "due_date", "label": "Due Date", "type": "date"},
            {"key": "billed", "label": "Billed", "type": "currency"},
            {"key": "received", "label": "Received", "type": "currency"},
            {"key": "outstanding", "label": "Outstanding", "type": "currency"},
            {"key": "days_overdue", "label": "Days Overdue", "type": "number"},
            {"key": "ageing_bucket", "label": "Ageing Bucket", "type": "status"},
        ],
    }

    chart = {
        "type": "bar",
        "title": "Receivables Ageing Breakdown",
        "data": [
            {"bucket": "0-30 Days", "amount": age_buckets["0_30"]},
            {"bucket": "31-60 Days", "amount": age_buckets["31_60"]},
            {"bucket": "61-90 Days", "amount": age_buckets["61_90"]},
            {"bucket": "90+ Days", "amount": age_buckets["90_plus"]},
        ],
    }

    return {
        "summary": summary,
        "rows": paged_rows,
        "pagination": pagination,
        "meta": meta,
        "chart": chart,
    }


def _supplier_outstanding(db: Session, org_id: str, df: datetime | None, dt: datetime | None, params: dict) -> dict:
    from app.services.accounts_payable_service import build_ap_item, query_open_payables

    page = int(params.get("page", 1))
    page_size = int(params.get("page_size", 25))
    search = params.get("search")
    supplier_id = params.get("supplier_id")
    payment_status = params.get("payment_status")
    verification_status = params.get("verification_status")
    overdue_only = str(params.get("overdue_only", "")).lower() in ("true", "1", "yes")
    ageing_bucket = params.get("ageing_bucket")
    as_of_date = params.get("as_of_date")

    if as_of_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Historical point-in-time balance reconstruction (as_of_date) is not supported for open supplier payables. Use transactional date_from/date_to reports for historical activity.",
        )

    if supplier_id:
        _verify_tenant_entity(db, org_id, Supplier, supplier_id, "Supplier")

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

    rows = []
    total_payable = 0.0
    total_overdue = 0.0
    suppliers_set = set()
    age_buckets = {"0_30": 0.0, "31_60": 0.0, "61_90": 0.0, "90_plus": 0.0}

    for inv in invoices:
        ap_item = build_ap_item(inv, now)
        out_amt = ap_item.outstanding_amount
        total_payable = round(total_payable + out_amt, 2)
        suppliers_set.add(inv.supplier_id)

        if ap_item.is_overdue:
            total_overdue = round(total_overdue + out_amt, 2)
            if ap_item.ageing_bucket in age_buckets:
                age_buckets[ap_item.ageing_bucket] = round(age_buckets[ap_item.ageing_bucket] + out_amt, 2)

        if ageing_bucket and ap_item.ageing_bucket != ageing_bucket:
            continue

        rows.append({
            "supplier_code": inv.supplier.supplier_code if inv.supplier else None,
            "supplier": ap_item.supplier_name,
            "invoice_number": ap_item.supplier_invoice_number,
            "invoice_date": ap_item.invoice_date.date().isoformat() if hasattr(ap_item.invoice_date, "date") else str(ap_item.invoice_date)[:10],
            "due_date": ap_item.due_date.date().isoformat() if (ap_item.due_date and hasattr(ap_item.due_date, "date")) else (str(ap_item.due_date)[:10] if ap_item.due_date else None),
            "grand_total": ap_item.grand_total,
            "return_amount": ap_item.return_amount,
            "paid_amount": ap_item.amount_paid,
            "outstanding": ap_item.outstanding_amount,
            "payment_status": ap_item.payment_status,
            "days_overdue": ap_item.days_overdue,
            "ageing_bucket": ap_item.ageing_bucket,
        })

    summary = {
        "total_payable": total_payable,
        "total_overdue": total_overdue,
        "suppliers_with_balance": len(suppliers_set),
        "age_0_30": age_buckets["0_30"],
        "age_31_60": age_buckets["31_60"],
        "age_61_90": age_buckets["61_90"],
        "age_90_plus": age_buckets["90_plus"],
    }

    paged_rows, pagination = _paginate(rows, page, page_size)

    meta = {
        "currency": "INR",
        "group_by": None,
        "columns": [
            {"key": "supplier_code", "label": "Supplier Code", "type": "text"},
            {"key": "supplier", "label": "Supplier", "type": "text"},
            {"key": "invoice_number", "label": "Invoice #", "type": "reference"},
            {"key": "invoice_date", "label": "Date", "type": "date"},
            {"key": "due_date", "label": "Due Date", "type": "date"},
            {"key": "grand_total", "label": "Grand Total", "type": "currency"},
            {"key": "return_amount", "label": "Returns", "type": "currency"},
            {"key": "paid_amount", "label": "Paid", "type": "currency"},
            {"key": "outstanding", "label": "Outstanding", "type": "currency"},
            {"key": "payment_status", "label": "Status", "type": "status"},
            {"key": "days_overdue", "label": "Days Overdue", "type": "number"},
            {"key": "ageing_bucket", "label": "Ageing", "type": "status"},
        ],
    }

    chart = {
        "type": "bar",
        "title": "Payables Ageing Breakdown",
        "data": [
            {"bucket": "0-30 Days", "amount": age_buckets["0_30"]},
            {"bucket": "31-60 Days", "amount": age_buckets["31_60"]},
            {"bucket": "61-90 Days", "amount": age_buckets["61_90"]},
            {"bucket": "90+ Days", "amount": age_buckets["90_plus"]},
        ],
    }

    return {
        "summary": summary,
        "rows": paged_rows,
        "pagination": pagination,
        "meta": meta,
        "chart": chart,
    }


def _payment_collection(db: Session, org_id: str, df: datetime | None, dt: datetime | None, params: dict) -> dict:
    page = int(params.get("page", 1))
    page_size = int(params.get("page_size", 25))
    search = params.get("search")
    customer_id = params.get("customer_id")
    payment_mode = params.get("payment_mode")

    if customer_id:
        _verify_tenant_entity(db, org_id, Customer, customer_id, "Customer")

    q = db.query(CustomerPayment).filter(
        CustomerPayment.organization_id == org_id,
    )
    q = _between(q, CustomerPayment.received_on, df, dt)

    if customer_id:
        q = q.filter(CustomerPayment.customer_id == customer_id)
    if payment_mode:
        q = q.filter(CustomerPayment.payment_mode == payment_mode.lower())
    if search:
        s = f"%{search}%"
        q = q.join(Customer, Customer.id == CustomerPayment.customer_id, isouter=True).filter(
            or_(
                CustomerPayment.reference.ilike(s),
                CustomerPayment.receipt_number.ilike(s),
                Customer.name.ilike(s),
                Customer.business_name.ilike(s),
            )
        )

    payments = q.order_by(CustomerPayment.received_on.desc(), CustomerPayment.id.desc()).all()

    total_collected = round(sum(p.amount for p in payments), 2)
    mode_totals: dict[str, float] = defaultdict(float)
    total_unallocated = 0.0

    for p in payments:
        mode_totals[p.payment_mode or "other"] += p.amount
        total_unallocated += getattr(p, "unallocated_amount", 0.0) or 0.0

    summary = {
        "total_collected": total_collected,
        "payment_count": len(payments),
        "cash": round(mode_totals.get("cash", 0.0), 2),
        "upi": round(mode_totals.get("upi", 0.0), 2),
        "card": round(mode_totals.get("card", 0.0), 2),
        "bank_other": round(sum(v for k, v in mode_totals.items() if k not in ("cash", "upi", "card")), 2),
        "on_account": round(total_unallocated, 2),
    }

    cust_names = {c.id: (c.business_name or c.name) for c in db.query(Customer).filter(Customer.organization_id == org_id).all()}
    rows = [
        {
            "reference": p.receipt_number or p.reference or p.id[:8],
            "date": p.received_on.date().isoformat() if hasattr(p.received_on, "date") else str(p.received_on)[:10],
            "customer": cust_names.get(p.customer_id) if p.customer_id else None,
            "amount": round(p.amount, 2),
            "mode": p.payment_mode,
            "txn_reference": p.reference,
            "allocated_amount": round(getattr(p, "allocated_amount", 0.0) or 0.0, 2),
            "unallocated_amount": round(getattr(p, "unallocated_amount", 0.0) or 0.0, 2),
        }
        for p in payments
    ]

    paged_rows, pagination = _paginate(rows, page, page_size)

    meta = {
        "currency": "INR",
        "group_by": None,
        "columns": [
            {"key": "reference", "label": "Receipt #", "type": "reference"},
            {"key": "date", "label": "Date", "type": "date"},
            {"key": "customer", "label": "Customer", "type": "text"},
            {"key": "amount", "label": "Amount", "type": "currency"},
            {"key": "mode", "label": "Mode", "type": "text"},
            {"key": "txn_reference", "label": "Txn Reference", "type": "text"},
            {"key": "allocated_amount", "label": "Allocated", "type": "currency"},
            {"key": "unallocated_amount", "label": "Unallocated", "type": "currency"},
        ],
    }

    chart = {
        "type": "pie",
        "title": "Collections by Payment Mode",
        "data": [{"mode": k, "amount": round(v, 2)} for k, v in mode_totals.items()],
    }

    return {
        "summary": summary,
        "rows": paged_rows,
        "pagination": pagination,
        "meta": meta,
        "chart": chart,
    }


def _expense(db: Session, org_id: str, df: datetime | None, dt: datetime | None, params: dict) -> dict:
    page = int(params.get("page", 1))
    page_size = int(params.get("page_size", 25))
    search = params.get("search")
    category = params.get("category")
    exp_status = params.get("status")
    payment_mode = params.get("payment_mode")
    submitted_by = params.get("submitted_by")
    vendor_id = params.get("vendor_id")

    if submitted_by:
        _verify_tenant_entity(db, org_id, User, submitted_by, "User")
    if vendor_id:
        _verify_tenant_entity(db, org_id, Supplier, vendor_id, "Vendor")

    q = db.query(Expense).filter(Expense.organization_id == org_id)
    q = _between(q, Expense.expense_date, df, dt)

    if submitted_by:
        q = q.filter(Expense.submitted_by == submitted_by)
    if vendor_id:
        q = q.filter(Expense.vendor_id == vendor_id)

    if category:
        q = q.filter(Expense.category == category)
    if exp_status:
        q = q.filter(Expense.status == exp_status.lower())
    if payment_mode:
        q = q.filter(Expense.payment_mode == payment_mode.lower())
    if search:
        s = f"%{search}%"
        q = q.filter(
            or_(
                Expense.category.ilike(s),
                Expense.description.ilike(s),
                Expense.vendor.has(Supplier.name.ilike(s)),
                Expense.payee_name.ilike(s),
                Expense.submitted_by.ilike(s),
            )
        )

    expenses = q.order_by(Expense.expense_date.desc(), Expense.id.desc()).all()

    approved_total = round(sum(e.amount for e in expenses if e.status == "approved"), 2)
    pending_total = round(sum(e.amount for e in expenses if e.status == "pending"), 2)
    rejected_total = round(sum(e.amount for e in expenses if e.status == "rejected"), 2)
    tax_total = round(sum(getattr(e, "tax_amount", 0.0) or 0.0 for e in expenses if e.status == "approved"), 2)
    tds_total = round(sum(getattr(e, "tds_amount", 0.0) or 0.0 for e in expenses if e.status == "approved"), 2)

    summary = {
        "approved_expense": approved_total,
        "pending_expense": pending_total,
        "rejected_expense": rejected_total,
        "tax_amount": tax_total,
        "tds_amount": tds_total,
        "entry_count": len(expenses),
    }

    rows = [
        {
            "expense_reference": e.category or e.id[:8],
            "date": e.expense_date.date().isoformat() if hasattr(e.expense_date, "date") else str(e.expense_date)[:10],
            "category": e.category,
            "vendor": _expense_vendor_name(e),
            "description": e.description,
            "amount": round(e.amount, 2),
            "tax": round(getattr(e, "tax_amount", 0.0) or 0.0, 2),
            "tds": round(getattr(e, "tds_amount", 0.0) or 0.0, 2),
            "net_payable": round(e.amount - (getattr(e, "tds_amount", 0.0) or 0.0), 2),
            "payment_mode": e.payment_mode,
            "status": e.status,
            "submitted_by": e.submitted_by,
        }
        for e in expenses
    ]

    paged_rows, pagination = _paginate(rows, page, page_size)

    cat_map: dict[str, float] = defaultdict(float)
    for e in expenses:
        if e.status == "approved":
            cat_map[e.category or "General"] += e.amount

    chart = {
        "type": "pie",
        "title": "Approved Expenses by Category",
        "data": [{"category": k, "amount": round(v, 2)} for k, v in cat_map.items()],
    }

    meta = {
        "currency": "INR",
        "group_by": None,
        "columns": [
            {"key": "date", "label": "Date", "type": "date"},
            {"key": "category", "label": "Category", "type": "text"},
            {"key": "vendor", "label": "Vendor", "type": "text"},
            {"key": "description", "label": "Description", "type": "text"},
            {"key": "amount", "label": "Amount", "type": "currency"},
            {"key": "tax", "label": "Tax", "type": "currency"},
            {"key": "tds", "label": "TDS", "type": "currency"},
            {"key": "net_payable", "label": "Net Payable", "type": "currency"},
            {"key": "payment_mode", "label": "Mode", "type": "text"},
            {"key": "status", "label": "Status", "type": "status"},
        ],
    }

    return {
        "summary": summary,
        "rows": paged_rows,
        "pagination": pagination,
        "meta": meta,
        "chart": chart,
    }


def _cash_collection(db: Session, org_id: str, df: datetime | None, dt: datetime | None, params: dict) -> dict:
    page = int(params.get("page", 1))
    page_size = int(params.get("page_size", 25))
    search = params.get("search")
    customer_id = params.get("customer_id")

    if customer_id:
        _verify_tenant_entity(db, org_id, Customer, customer_id, "Customer")

    q = db.query(CustomerPayment).filter(
        CustomerPayment.organization_id == org_id,
    )
    q = _between(q, CustomerPayment.received_on, df, dt)
    if customer_id:
        q = q.filter(CustomerPayment.customer_id == customer_id)

    all_pays = q.order_by(CustomerPayment.received_on.desc(), CustomerPayment.id.desc()).all()
    cust_names = {c.id: (c.business_name or c.name) for c in db.query(Customer).filter(Customer.organization_id == org_id).all()}

    rows = []
    customers_set = set()

    for p in all_pays:
        c_name = cust_names.get(p.customer_id) if p.customer_id else None
        if p.payment_mode == "cash":
            rows.append({
                "date": p.received_on.date().isoformat() if hasattr(p.received_on, "date") else str(p.received_on)[:10],
                "receipt_reference": p.receipt_number or p.reference or p.id[:8],
                "customer": c_name,
                "cash_amount": round(p.amount, 2),
                "source": "Pure Cash",
            })
            if p.customer_id:
                customers_set.add(p.customer_id)
        elif p.payment_mode == "split" and getattr(p, "splits", None):
            for s in p.splits:
                if s.payment_mode == "cash" and s.amount > 0:
                    rows.append({
                        "date": p.received_on.date().isoformat() if hasattr(p.received_on, "date") else str(p.received_on)[:10],
                        "receipt_reference": p.receipt_number or p.reference or p.id[:8],
                        "customer": c_name,
                        "cash_amount": round(s.amount, 2),
                        "source": "Split Payment (Cash Component)",
                    })
                    if p.customer_id:
                        customers_set.add(p.customer_id)

    if search:
        s_lower = search.lower()
        rows = [r for r in rows if s_lower in (r["receipt_reference"] or "").lower() or s_lower in (r["customer"] or "").lower()]

    total_cash = round(sum(r["cash_amount"] for r in rows), 2)

    summary = {
        "total_cash": total_cash,
        "entry_count": len(rows),
        "customer_count": len(customers_set),
    }

    paged_rows, pagination = _paginate(rows, page, page_size)

    meta = {
        "currency": "INR",
        "group_by": None,
        "columns": [
            {"key": "date", "label": "Date", "type": "date"},
            {"key": "receipt_reference", "label": "Receipt #", "type": "reference"},
            {"key": "customer", "label": "Customer", "type": "text"},
            {"key": "cash_amount", "label": "Cash Collected", "type": "currency"},
            {"key": "source", "label": "Payment Source", "type": "text"},
        ],
    }

    daily_cash: dict[str, float] = defaultdict(float)
    for r in rows:
        daily_cash[r["date"]] += r["cash_amount"]

    chart = {
        "type": "line",
        "title": "Daily Cash Collections Trend",
        "data": [{"date": d, "amount": round(amt, 2)} for d, amt in sorted(daily_cash.items())],
    }

    return {
        "summary": summary,
        "rows": paged_rows,
        "pagination": pagination,
        "meta": meta,
        "chart": chart,
    }


# GST transaction view vocabulary. Each source document type carries exactly one GST
# classification, mirroring the four lines of the aggregate summary.
GST_DOCUMENT_TYPES = ("sales_invoice", "sales_return", "supplier_invoice", "purchase_return")
GST_CLASSIFICATIONS = {
    "sales_invoice": "output",
    "sales_return": "output_adjustment",
    "supplier_invoice": "input",
    "purchase_return": "input_adjustment",
}
_GST_CUSTOMER_DOCUMENTS = ("sales_invoice", "sales_return")
_GST_SUPPLIER_DOCUMENTS = ("supplier_invoice", "purchase_return")
# Values a `status` filter may take per document type. Returns and supplier invoices are
# counted only in these statuses (the aggregate summary's scopes); sales invoices are
# counted unless void/cancelled, as in the summary, and these are their live statuses.
GST_DOCUMENT_STATUSES = {
    "sales_invoice": ("unpaid", "partial", "paid", "returned"),
    "sales_return": ("received", "approved", "completed"),
    "supplier_invoice": ("recorded",),
    "purchase_return": ("confirmed", "dispatched", "completed"),
}
GST_SORT_FIELDS = (
    "document_date", "document_number", "party_name", "gst_number", "document_type",
    "gst_classification", "status", "taxable_value", "tax_amount", "total_amount",
)
_GST_RANGE_FILTERS = (
    ("min_amount", "max_amount", "total_amount"),
    ("min_taxable_value", "max_taxable_value", "taxable_value"),
    ("min_tax_amount", "max_tax_amount", "tax_amount"),
)


def _is_blank(value: Any) -> bool:
    return value is None or value is False or (isinstance(value, str) and not value.strip())


def _reject_unsupported_filters(params: dict, supported: list[str], context: str) -> None:
    """Fail loudly instead of silently ignoring a filter the selected GST view cannot honour."""
    unsupported = sorted(
        k for k, v in params.items()
        if k not in supported and k not in ("page", "page_size") and not _is_blank(v)
    )
    if unsupported:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Filter(s) {unsupported} are not supported by {context}. Supported: {sorted(supported)}",
        )


def _enum_filter(params: dict, key: str, allowed) -> set[str] | None:
    """Parse a single or comma-separated enum filter; reject anything outside `allowed`."""
    raw = params.get(key)
    if _is_blank(raw):
        return None
    values = {v.strip().lower() for v in str(raw).split(",") if v.strip()}
    invalid = sorted(values - set(allowed))
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {key} {invalid}. Allowed: {sorted(set(allowed))}",
        )
    return values


def _amount_bounds(params: dict, lo_key: str, hi_key: str) -> tuple[float | None, float | None]:
    bounds = []
    for key in (lo_key, hi_key):
        raw = params.get(key)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            bounds.append(None)
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{key} must be a number")
        if not math.isfinite(value) or value < 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{key} must be a non-negative number")
        bounds.append(value)
    lo, hi = bounds
    if lo is not None and hi is not None and lo > hi:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{lo_key} cannot be greater than {hi_key}")
    return lo, hi


def _iso_date(value) -> str:
    return value.date().isoformat() if hasattr(value, "date") else str(value)[:10]


def _gst_summary(db: Session, org_id: str, df: datetime | None, dt: datetime | None, params: dict) -> dict:
    registry = REPORT_REGISTRY["gst-summary"]
    group_by = params.get("group_by") or "summary"
    if group_by not in registry["allowed_group_by"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid group_by '{group_by}' for gst-summary report. Allowed: {registry['allowed_group_by']}",
        )
    if group_by == "transaction":
        _reject_unsupported_filters(params, registry["transaction_filters"], "the GST transaction view (group_by=transaction)")
        return _gst_transactions(db, org_id, df, dt, params)
    # The aggregate covers every in-scope document; a party/document filter applied to only
    # part of it would misstate the liability, so those filters require the transaction view.
    _reject_unsupported_filters(params, registry["supported_filters"], "the aggregate GST summary; use group_by=transaction")
    return _gst_aggregate(db, org_id, df, dt, params)


def _gst_transactions(db: Session, org_id: str, df: datetime | None, dt: datetime | None, params: dict) -> dict:
    page = int(params.get("page", 1))
    page_size = int(params.get("page_size", 25))
    customer_id = params.get("customer_id") or None
    supplier_id = params.get("supplier_id") or None
    search = (params.get("search") or "").strip().lower()
    doc_ref = (params.get("invoice_number") or "").strip().lower()

    gst_number = None
    if not _is_blank(params.get("gst_number")):
        gst_number = "".join(str(params["gst_number"]).split()).upper()
        if not gst_number.isalnum() or len(gst_number) > 20:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="gst_number must be alphanumeric and at most 20 characters",
            )

    doc_types = _enum_filter(params, "invoice_type", GST_DOCUMENT_TYPES)
    classifications = _enum_filter(params, "gst_classification", GST_CLASSIFICATIONS.values())
    all_statuses = {s for statuses in GST_DOCUMENT_STATUSES.values() for s in statuses}
    statuses = _enum_filter(params, "status", all_statuses)
    ranges = [(field, *_amount_bounds(params, lo, hi)) for lo, hi, field in _GST_RANGE_FILTERS]

    sort_by = params.get("sort_by") or "document_date"
    if sort_by not in GST_SORT_FIELDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid sort_by '{sort_by}' for GST transactions. Allowed: {list(GST_SORT_FIELDS)}",
        )
    sort_order = (params.get("sort_order") or "desc").lower()
    if sort_order not in ("asc", "desc"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="sort_order must be 'asc' or 'desc'")

    if customer_id and supplier_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="customer_id applies to sales-side documents and supplier_id to purchase-side documents; use one at a time",
        )

    # Which document types are in play: narrowed by invoice_type, gst_classification and
    # the side a party filter belongs to.
    active = set(GST_DOCUMENT_TYPES)
    if doc_types is not None:
        active &= doc_types
    if classifications is not None:
        active &= {t for t, c in GST_CLASSIFICATIONS.items() if c in classifications}
    if customer_id or supplier_id:
        side = _GST_CUSTOMER_DOCUMENTS if customer_id else _GST_SUPPLIER_DOCUMENTS
        requested = doc_types is not None or classifications is not None
        if requested and not (active & set(side)):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"{'customer_id' if customer_id else 'supplier_id'} only applies to {list(side)} documents, "
                    "which the requested invoice_type / gst_classification exclude"
                ),
            )
        active &= set(side)

    if customer_id:
        _verify_tenant_entity(db, org_id, Customer, customer_id, "Customer")
    if supplier_id:
        _verify_tenant_entity(db, org_id, Supplier, supplier_id, "Supplier")

    def _statuses_for(doc_type: str) -> tuple[str, ...]:
        scope = GST_DOCUMENT_STATUSES[doc_type]
        return scope if statuses is None else tuple(s for s in scope if s in statuses)

    rows: list[dict[str, Any]] = []

    if "sales_invoice" in active and _statuses_for("sales_invoice"):
        q = db.query(Invoice).filter(
            Invoice.organization_id == org_id,
            Invoice.is_credit_note.is_(False),
            ~Invoice.status.in_(("void", "cancelled")),
        )
        if statuses is not None:
            q = q.filter(Invoice.status.in_(_statuses_for("sales_invoice")))
        if customer_id:
            q = q.filter(Invoice.customer_id == customer_id)
        for inv in _between(q, Invoice.invoice_date, df, dt).all():
            cust = inv.customer
            rows.append({
                "document_type": "sales_invoice",
                "gst_classification": "output",
                "source_id": inv.id,
                "document_number": inv.invoice_number,
                "document_date": _iso_date(inv.invoice_date),
                "reference_document": None,
                "party_type": "customer",
                "party_id": inv.customer_id,
                "party_name": (cust.business_name or cust.name) if cust else (inv.walk_in_name or "Walk-in"),
                "gst_number": cust.gst_number if cust else None,
                "status": inv.status,
                "taxable_value": round(inv.subtotal or 0.0, 2),
                "tax_amount": round(inv.tax or 0.0, 2),
                "total_amount": round(inv.total or 0.0, 2),
            })

    if "sales_return" in active and _statuses_for("sales_return"):
        q = db.query(SalesReturn).options(selectinload(SalesReturn.items)).filter(
            SalesReturn.organization_id == org_id,
            SalesReturn.return_status.in_(_statuses_for("sales_return")),
        )
        if customer_id:
            q = q.filter(SalesReturn.customer_id == customer_id)
        for r in _between(q, SalesReturn.return_date, df, dt).all():
            cust = r.customer
            # Return lines store a pre-tax line total and a rate; tax is derived exactly as
            # the aggregate summary derives it.
            taxable = sum(item.line_total or 0.0 for item in r.items)
            tax = sum((item.tax_rate or 0.0) / 100.0 * (item.line_total or 0.0) for item in r.items)
            rows.append({
                "document_type": "sales_return",
                "gst_classification": "output_adjustment",
                "source_id": r.id,
                "document_number": r.return_number,
                "document_date": _iso_date(r.return_date),
                "reference_document": r.invoice.invoice_number if r.invoice else None,
                "party_type": "customer",
                "party_id": r.customer_id,
                "party_name": (cust.business_name or cust.name) if cust else None,
                "gst_number": cust.gst_number if cust else None,
                "status": r.return_status,
                "taxable_value": round(taxable, 2),
                "tax_amount": round(tax, 2),
                "total_amount": round(r.credit_amount if r.credit_amount is not None else taxable + tax, 2),
            })

    if "supplier_invoice" in active and _statuses_for("supplier_invoice"):
        q = db.query(SupplierInvoice).filter(
            SupplierInvoice.organization_id == org_id,
            SupplierInvoice.status.in_(_statuses_for("supplier_invoice")),
        )
        if supplier_id:
            q = q.filter(SupplierInvoice.supplier_id == supplier_id)
        for si in _between(q, SupplierInvoice.supplier_invoice_date, df, dt).all():
            sup = si.supplier
            purchase = si.purchase
            rows.append({
                "document_type": "supplier_invoice",
                "gst_classification": "input",
                "source_id": si.id,
                "document_number": si.supplier_invoice_number,
                "document_date": _iso_date(si.supplier_invoice_date),
                "reference_document": purchase.invoice_number if purchase else None,
                "party_type": "supplier",
                "party_id": si.supplier_id,
                "party_name": sup.name if sup else None,
                "gst_number": (sup.gst_number if sup else None) or (purchase.payee_gstin if purchase else None),
                "status": si.status,
                "taxable_value": round(si.subtotal or 0.0, 2),
                "tax_amount": round(si.tax_amount or 0.0, 2),
                "total_amount": round(si.grand_total or 0.0, 2),
            })

    if "purchase_return" in active and _statuses_for("purchase_return"):
        q = db.query(PurchaseReturn).options(selectinload(PurchaseReturn.items)).filter(
            PurchaseReturn.organization_id == org_id,
            PurchaseReturn.status.in_(_statuses_for("purchase_return")),
        )
        if supplier_id:
            q = q.filter(PurchaseReturn.supplier_id == supplier_id)
        for r in _between(q, PurchaseReturn.return_date, df, dt).all():
            sup = r.supplier
            purchase = r.purchase
            # Purchase return lines store tax_amount and a tax-inclusive line_total.
            tax = sum(item.tax_amount or 0.0 for item in r.items)
            total = sum(item.line_total or 0.0 for item in r.items)
            rows.append({
                "document_type": "purchase_return",
                "gst_classification": "input_adjustment",
                "source_id": r.id,
                "document_number": r.return_number,
                "document_date": _iso_date(r.return_date),
                "reference_document": purchase.invoice_number if purchase else None,
                "party_type": "supplier",
                "party_id": r.supplier_id,
                "party_name": sup.name if sup else None,
                "gst_number": (sup.gst_number if sup else None) or (purchase.payee_gstin if purchase else None),
                "status": r.status,
                "taxable_value": round(total - tax, 2),
                "tax_amount": round(tax, 2),
                "total_amount": round(total, 2),
            })

    def _keep(row: dict) -> bool:
        if gst_number and "".join((row["gst_number"] or "").split()).upper() != gst_number:
            return False
        if doc_ref and not any(doc_ref in (row[k] or "").lower() for k in ("document_number", "reference_document")):
            return False
        if search and not any(
            search in (row[k] or "").lower()
            for k in ("party_name", "document_number", "reference_document", "gst_number")
        ):
            return False
        for field, lo, hi in ranges:
            if lo is not None and row[field] < lo:
                return False
            if hi is not None and row[field] > hi:
                return False
        return True

    rows = [r for r in rows if _keep(r)]

    def _sort_key(row: dict):
        v = row[sort_by]
        return v if isinstance(v, (int, float)) else (v or "").lower()

    rows.sort(key=lambda r: (r["document_number"] or "", r["source_id"]), reverse=sort_order == "desc")
    rows.sort(key=_sort_key, reverse=sort_order == "desc")

    totals = {c: {"taxable_value": 0.0, "tax_amount": 0.0, "count": 0} for c in GST_CLASSIFICATIONS.values()}
    for r in rows:
        t = totals[r["gst_classification"]]
        t["taxable_value"] += r["taxable_value"]
        t["tax_amount"] += r["tax_amount"]
        t["count"] += 1

    summary: dict[str, Any] = {"row_count": len(rows)}
    for c, t in totals.items():
        summary[f"{c}_count"] = t["count"]
        summary[f"{c}_taxable_value"] = round(t["taxable_value"], 2)
        summary[f"{c}_tax_amount"] = round(t["tax_amount"], 2)
    summary["net_tax_amount"] = round(
        (totals["output"]["tax_amount"] - totals["output_adjustment"]["tax_amount"])
        - (totals["input"]["tax_amount"] - totals["input_adjustment"]["tax_amount"]),
        2,
    )

    paged_rows, pagination = _paginate(rows, page, page_size)

    meta = {
        "currency": "INR",
        "group_by": "transaction",
        "notes": (
            "Transaction view: one row per GST document. All amounts are positive document values; "
            "gst_classification gives the direction (output, output_adjustment reduces output, input, "
            "input_adjustment reduces input). net_tax_amount = (output - output_adjustment) - (input - input_adjustment) "
            "over the filtered rows. Return tax: sales returns = stored rate x pre-tax line total; purchase returns = "
            "stored line tax_amount (same as the aggregate view). customer_id limits rows to sales-side documents, supplier_id to purchase-side ones. "
            "min_amount/max_amount compare total_amount (tax inclusive; credited amount for sales returns)."
        ),
        "columns": [
            {"key": "document_date", "label": "Date", "type": "date"},
            {"key": "document_type", "label": "Document Type", "type": "text"},
            {"key": "gst_classification", "label": "GST Classification", "type": "status"},
            {"key": "document_number", "label": "Document #", "type": "reference"},
            {"key": "reference_document", "label": "Reference #", "type": "reference"},
            {"key": "party_name", "label": "Party", "type": "text"},
            {"key": "gst_number", "label": "GSTIN", "type": "text"},
            {"key": "status", "label": "Status", "type": "status"},
            {"key": "taxable_value", "label": "Taxable Value", "type": "currency"},
            {"key": "tax_amount", "label": "Tax Amount", "type": "currency"},
            {"key": "total_amount", "label": "Total", "type": "currency"},
        ],
    }

    chart = {
        "type": "bar",
        "title": "GST by Classification",
        "data": [{"component": c, "amount": round(t["tax_amount"], 2)} for c, t in totals.items()],
    }

    return {
        "summary": summary,
        "rows": paged_rows,
        "pagination": pagination,
        "meta": meta,
        "chart": chart,
    }


def _gst_aggregate(db: Session, org_id: str, df: datetime | None, dt: datetime | None, params: dict) -> dict:
    page = int(params.get("page", 1))
    page_size = int(params.get("page_size", 25))

    # Output GST from Invoices
    inv_q = db.query(Invoice).filter(
        Invoice.organization_id == org_id,
        Invoice.is_credit_note.is_(False),
        ~Invoice.status.in_(("void", "cancelled")),
    )
    inv_q = _between(inv_q, Invoice.invoice_date, df, dt)
    invoices = inv_q.all()

    # Sales Returns adjustments
    sr_q = db.query(SalesReturn).filter(
        SalesReturn.organization_id == org_id,
        SalesReturn.return_status.in_(("approved", "completed", "received")),
    )
    sr_q = _between(sr_q, SalesReturn.return_date, df, dt)
    sales_returns = sr_q.all()

    # Input GST from Supplier Invoices
    sup_q = db.query(SupplierInvoice).filter(
        SupplierInvoice.organization_id == org_id,
        SupplierInvoice.status == "recorded",
    )
    sup_q = _between(sup_q, SupplierInvoice.supplier_invoice_date, df, dt)
    sup_invoices = sup_q.all()

    # Purchase Returns adjustments
    pr_q = db.query(PurchaseReturn).filter(
        PurchaseReturn.organization_id == org_id,
        PurchaseReturn.status.in_(("confirmed", "dispatched", "completed")),
    )
    pr_q = _between(pr_q, PurchaseReturn.return_date, df, dt)
    purchase_returns = pr_q.options(selectinload(PurchaseReturn.items)).all()

    gross_output_gst = sum(i.tax for i in invoices)
    sales_return_gst = sum(
        sum((item.tax_rate or 0.0) / 100.0 * (item.line_total or 0.0) for item in r.items)
        for r in sales_returns
    )
    output_gst = round(max(gross_output_gst - sales_return_gst, 0.0), 2)

    gross_input_gst = sum(i.tax_amount or 0.0 for i in sup_invoices)
    # Purchase return lines store their own tax_amount; that input tax is reversed.
    purchase_return_gst = sum(item.tax_amount or 0.0 for r in purchase_returns for item in r.items)
    input_gst = round(max(gross_input_gst - purchase_return_gst, 0.0), 2)

    # Taxable values are pre-tax: return lines store a pre-tax line_total (sales) or a
    # tax-inclusive line_total alongside its tax_amount (purchase).
    sales_return_taxable = sum(item.line_total or 0.0 for r in sales_returns for item in r.items)
    purchase_return_taxable = sum(
        (item.line_total or 0.0) - (item.tax_amount or 0.0) for r in purchase_returns for item in r.items
    )

    gst_adjustments = round(sales_return_gst + purchase_return_gst, 2)
    net_gst = round(output_gst - input_gst, 2)

    summary = {
        "output_gst": output_gst,
        "input_gst": input_gst,
        "gst_adjustments": gst_adjustments,
        "net_gst": net_gst,
    }

    rows = [
        {
            "type": "Output GST (Sales Invoices)",
            "taxable_value": round(sum(i.subtotal for i in invoices), 2),
            "tax_amount": round(gross_output_gst, 2),
            "description": f"Tax on {len(invoices)} issued tax sales invoices",
        },
        {
            "type": "Output GST Adjustment (Sales Returns)",
            "taxable_value": round(-sales_return_taxable, 2),
            "tax_amount": round(-sales_return_gst, 2),
            "description": f"Deduction for {len(sales_returns)} approved sales returns / credit notes",
        },
        {
            "type": "Input GST (Purchase Invoices)",
            "taxable_value": round(sum(i.subtotal for i in sup_invoices), 2),
            "tax_amount": round(gross_input_gst, 2),
            "description": f"Input tax credit on {len(sup_invoices)} supplier purchase invoices (before purchase-return reversals)",
        },
        {
            "type": "Net GST Liability",
            "taxable_value": round(
                (sum(i.subtotal for i in invoices) - sales_return_taxable)
                - (sum(i.subtotal for i in sup_invoices) - purchase_return_taxable),
                2,
            ),
            "tax_amount": net_gst,
            "description": f"Output GST minus eligible Input Tax Credit (net of {len(purchase_returns)} purchase returns)",
        },
    ]

    paged_rows, pagination = _paginate(rows, page, page_size)

    meta = {
        "currency": "INR",
        "group_by": "summary",
        "notes": (
            "Aggregate view. Taxable values are pre-tax. input_gst and net_gst deduct the stored tax on "
            "confirmed/dispatched/completed purchase returns; the Input GST row shows gross input tax. "
            "Use group_by=transaction for document-level rows and filters."
        ),
        "columns": [
            {"key": "type", "label": "GST Classification", "type": "text"},
            {"key": "taxable_value", "label": "Taxable Value", "type": "currency"},
            {"key": "tax_amount", "label": "Tax Amount", "type": "currency"},
            {"key": "description", "label": "Description", "type": "text"},
        ],
    }

    chart = {
        "type": "bar",
        "title": "GST Breakdown",
        "data": [
            {"component": "Output GST", "amount": output_gst},
            {"component": "Input GST", "amount": input_gst},
            {"component": "Net GST Payable", "amount": net_gst},
        ],
    }

    return {
        "summary": summary,
        "rows": paged_rows,
        "pagination": pagination,
        "meta": meta,
        "chart": chart,
    }


def _sales_return(db: Session, org_id: str, df: datetime | None, dt: datetime | None, params: dict) -> dict:
    page = int(params.get("page", 1))
    page_size = int(params.get("page_size", 25))
    search = params.get("search")
    customer_id = params.get("customer_id")
    ret_status = params.get("status")

    if customer_id:
        _verify_tenant_entity(db, org_id, Customer, customer_id, "Customer")

    q = db.query(SalesReturn).filter(SalesReturn.organization_id == org_id)
    q = _between(q, SalesReturn.return_date, df, dt)

    if customer_id:
        q = q.filter(SalesReturn.customer_id == customer_id)
    if ret_status:
        q = q.filter(SalesReturn.return_status == ret_status.lower())
    if search:
        s = f"%{search}%"
        q = q.join(Customer, Customer.id == SalesReturn.customer_id, isouter=True).filter(
            or_(
                SalesReturn.return_number.ilike(s),
                SalesReturn.return_reason.ilike(s),
                Customer.name.ilike(s),
                Customer.business_name.ilike(s),
            )
        )

    returns = q.order_by(SalesReturn.return_date.desc(), SalesReturn.id.desc()).all()

    total_credit = round(sum(r.credit_amount or 0.0 for r in returns), 2)
    total_qty = sum(sum(item.quantity_returned for item in r.items) for r in returns)

    summary = {
        "total_returns": total_credit,
        "total_credit_amount": total_credit,
        "return_count": len(returns),
    }

    rows = [
        {
            "return_number": r.return_number,
            "date": r.return_date.date().isoformat() if hasattr(r.return_date, "date") else str(r.return_date)[:10],
            "customer": (r.customer.business_name or r.customer.name) if r.customer else None,
            "invoice_reference": r.invoice.invoice_number if r.invoice else None,
            "reason": r.return_reason,
            "status": r.return_status,
            "returned_quantity": sum(item.quantity_returned for item in r.items),
            "credit_amount": round(r.credit_amount or 0.0, 2),
            "warehouse": r.warehouse_id,
        }
        for r in returns
    ]

    paged_rows, pagination = _paginate(rows, page, page_size)

    meta = {
        "currency": "INR",
        "group_by": None,
        "columns": [
            {"key": "return_number", "label": "Return #", "type": "reference"},
            {"key": "date", "label": "Date", "type": "date"},
            {"key": "customer", "label": "Customer", "type": "text"},
            {"key": "invoice_reference", "label": "Invoice #", "type": "reference"},
            {"key": "reason", "label": "Reason", "type": "text"},
            {"key": "returned_quantity", "label": "Qty", "type": "number"},
            {"key": "credit_amount", "label": "Credit Amount", "type": "currency"},
            {"key": "status", "label": "Status", "type": "status"},
        ],
    }

    daily_ret: dict[str, float] = defaultdict(float)
    for r in returns:
        d_str = r.return_date.date().isoformat() if hasattr(r.return_date, "date") else str(r.return_date)[:10]
        daily_ret[d_str] += (r.credit_amount or 0.0)

    chart = {
        "type": "bar",
        "title": "Sales Returns Over Time",
        "data": [{"date": d, "amount": round(amt, 2)} for d, amt in sorted(daily_ret.items())],
    }

    return {
        "summary": summary,
        "rows": paged_rows,
        "pagination": pagination,
        "meta": meta,
        "chart": chart,
    }


def _purchase_return(db: Session, org_id: str, df: datetime | None, dt: datetime | None, params: dict) -> dict:
    page = int(params.get("page", 1))
    page_size = int(params.get("page_size", 25))
    search = params.get("search")
    supplier_id = params.get("supplier_id")
    ret_status = params.get("status")
    purchase_id = params.get("purchase_id")

    if supplier_id:
        _verify_tenant_entity(db, org_id, Supplier, supplier_id, "Supplier")
    if purchase_id:
        _verify_tenant_entity(db, org_id, PurchaseInvoice, purchase_id, "Purchase")

    q = db.query(PurchaseReturn).filter(PurchaseReturn.organization_id == org_id)
    q = _between(q, PurchaseReturn.return_date, df, dt)

    if supplier_id:
        q = q.filter(PurchaseReturn.supplier_id == supplier_id)
    if purchase_id:
        q = q.filter(PurchaseReturn.purchase_id == purchase_id)
    if ret_status:
        q = q.filter(PurchaseReturn.status == ret_status.lower())
    if search:
        s = f"%{search}%"
        q = q.join(Supplier, Supplier.id == PurchaseReturn.supplier_id, isouter=True).filter(
            or_(
                PurchaseReturn.return_number.ilike(s),
                PurchaseReturn.reason.ilike(s),
                Supplier.name.ilike(s),
            )
        )

    returns = q.order_by(PurchaseReturn.return_date.desc(), PurchaseReturn.id.desc()).all()

    total_amount = round(sum(getattr(r, "total_amount", 0.0) or 0.0 for r in returns), 2)

    summary = {
        "total_returns": total_amount,
        "return_count": len(returns),
    }

    rows = [
        {
            "return_number": r.return_number,
            "date": r.return_date.date().isoformat() if hasattr(r.return_date, "date") else str(r.return_date)[:10],
            "supplier": r.supplier.name if r.supplier else None,
            "purchase_reference": r.purchase.invoice_number if getattr(r, "purchase", None) else None,
            "quantity": getattr(r, "total_return_qty", 0.0),
            "amount": round(getattr(r, "total_amount", 0.0) or 0.0, 2),
            "status": r.status,
            "reason": r.reason,
        }
        for r in returns
    ]

    paged_rows, pagination = _paginate(rows, page, page_size)

    meta = {
        "currency": "INR",
        "group_by": None,
        "columns": [
            {"key": "return_number", "label": "Return #", "type": "reference"},
            {"key": "date", "label": "Date", "type": "date"},
            {"key": "supplier", "label": "Supplier", "type": "text"},
            {"key": "purchase_reference", "label": "Purchase Ref", "type": "reference"},
            {"key": "quantity", "label": "Quantity", "type": "number"},
            {"key": "amount", "label": "Amount", "type": "currency"},
            {"key": "status", "label": "Status", "type": "status"},
            {"key": "reason", "label": "Reason", "type": "text"},
        ],
    }

    chart = {
        "type": "bar",
        "title": "Purchase Returns Trend",
        "data": [
            {"return_number": r["return_number"], "amount": r["amount"]}
            for r in rows[:10]
        ],
    }

    return {
        "summary": summary,
        "rows": paged_rows,
        "pagination": pagination,
        "meta": meta,
        "chart": chart,
    }


def _profit_loss(db: Session, org_id: str, df: datetime | None, dt: datetime | None, params: dict) -> dict:
    page = int(params.get("page", 1))
    page_size = int(params.get("page_size", 25))

    # 1. Gross Sales (Invoices)
    inv_q = db.query(Invoice).filter(
        Invoice.organization_id == org_id,
        Invoice.is_credit_note.is_(False),
        ~Invoice.status.in_(("void", "cancelled")),
    )
    inv_q = _between(inv_q, Invoice.invoice_date, df, dt)
    invoices = inv_q.all()
    gross_sales = round(sum(i.total for i in invoices), 2)

    # 2. Sales Returns
    sr_q = db.query(SalesReturn).filter(
        SalesReturn.organization_id == org_id,
        SalesReturn.return_status.in_(("approved", "completed", "received")),
    )
    sr_q = _between(sr_q, SalesReturn.return_date, df, dt)
    sales_returns = sr_q.all()
    sales_returns_total = round(sum(r.credit_amount or 0.0 for r in sales_returns), 2)

    net_sales = round(gross_sales - sales_returns_total, 2)

    # 3. Cost of Goods Sold (COGS) strictly from sale-time cost snapshots
    cogs = 0.0
    missing_cogs_lines = 0
    for i in invoices:
        for item in i.items:
            qty = float(item.quantity or 0)
            cost = 0.0
            if item.order_item_id:
                so_item = db.get(SalesOrderItem, item.order_item_id)
                if so_item and so_item.cost_price is not None and so_item.cost_price > 0:
                    cost = round(so_item.cost_price * qty, 2)
            if cost == 0.0 and qty > 0:
                missing_cogs_lines += 1
            cogs += cost
    cogs = round(cogs, 2)

    gross_profit = round(net_sales - cogs, 2)

    # 4. Operating Expenses (Approved only)
    exp_q = db.query(Expense).filter(
        Expense.organization_id == org_id,
        Expense.status == "approved",
    )
    exp_q = _between(exp_q, Expense.expense_date, df, dt)
    expenses = exp_q.all()
    operating_expenses = round(sum(e.amount for e in expenses), 2)

    net_profit = round(gross_profit - operating_expenses, 2)

    gross_margin_pct = round((gross_profit / net_sales * 100.0), 2) if net_sales > 0 else 0.0
    net_margin_pct = round((net_profit / net_sales * 100.0), 2) if net_sales > 0 else 0.0

    summary = {
        "gross_sales": gross_sales,
        "sales_returns": sales_returns_total,
        "net_sales": net_sales,
        "cogs": cogs,
        "cogs_basis": "historical_cost_snapshots",
        "missing_cogs_lines": missing_cogs_lines,
        "gross_profit": gross_profit,
        "operating_expenses": operating_expenses,
        "net_profit": net_profit,
        "gross_margin_percent": gross_margin_pct,
        "net_margin_percent": net_margin_pct,
    }

    rows = [
        {"item": "Gross Sales Revenue", "amount": gross_sales, "percent_of_revenue": 100.0 if net_sales > 0 else 0.0},
        {"item": "Less: Sales Returns & Allowances", "amount": -sales_returns_total, "percent_of_revenue": round((-sales_returns_total / net_sales * 100.0), 2) if net_sales > 0 else 0.0},
        {"item": "Net Sales Revenue", "amount": net_sales, "percent_of_revenue": 100.0 if net_sales > 0 else 0.0},
        {"item": "Cost of Goods Sold (COGS)", "amount": -cogs, "percent_of_revenue": round((-cogs / net_sales * 100.0), 2) if net_sales > 0 else 0.0},
        {"item": "Gross Profit", "amount": gross_profit, "percent_of_revenue": gross_margin_pct},
        {"item": "Operating Expenses (Approved)", "amount": -operating_expenses, "percent_of_revenue": round((-operating_expenses / net_sales * 100.0), 2) if net_sales > 0 else 0.0},
        {"item": "Net Profit / Loss", "amount": net_profit, "percent_of_revenue": net_margin_pct},
    ]

    paged_rows, pagination = _paginate(rows, page, page_size)

    meta = {
        "currency": "INR",
        "group_by": None,
        "cogs_notes": (
            f"COGS is calculated strictly from sale-time cost snapshots. {missing_cogs_lines} line(s) had no historical cost snapshot and were not assigned fabricated catalog pricing."
            if missing_cogs_lines > 0
            else "COGS is calculated strictly from historical sale-time cost snapshots."
        ),
        "columns": [
            {"key": "item", "label": "P&L Statement Line Item", "type": "text"},
            {"key": "amount", "label": "Amount", "type": "currency"},
            {"key": "percent_of_revenue", "label": "% of Net Sales", "type": "percent"},
        ],
    }

    chart = {
        "type": "bar",
        "title": "Profit & Loss Waterfall",
        "data": [
            {"metric": "Net Sales", "amount": net_sales},
            {"metric": "COGS", "amount": cogs},
            {"metric": "Gross Profit", "amount": gross_profit},
            {"metric": "Operating Expenses", "amount": operating_expenses},
            {"metric": "Net Profit", "amount": net_profit},
        ],
    }

    return {
        "summary": summary,
        "rows": paged_rows,
        "pagination": pagination,
        "meta": meta,
        "chart": chart,
    }


def _supplier_payment(db: Session, org_id: str, df: datetime | None, dt: datetime | None, params: dict) -> dict:
    page = int(params.get("page", 1))
    page_size = int(params.get("page_size", 25))
    search = params.get("search")
    supplier_id = params.get("supplier_id")
    p_method = params.get("payment_method") or params.get("payment_mode")
    p_status = params.get("status")

    if supplier_id:
        _verify_tenant_entity(db, org_id, Supplier, supplier_id, "Supplier")

    q = db.query(SupplierPayment).filter(
        SupplierPayment.organization_id == org_id,
        SupplierPayment.status != "void",
    )
    q = _between(q, SupplierPayment.paid_on, df, dt)

    if supplier_id:
        q = q.filter(SupplierPayment.supplier_id == supplier_id)
    if p_method:
        q = q.filter(or_(SupplierPayment.payment_mode == p_method.lower(), SupplierPayment.payment_method == p_method.lower()))
    if p_status:
        q = q.filter(SupplierPayment.status == p_status.lower())
    if search:
        s = f"%{search}%"
        q = q.join(Supplier, Supplier.id == SupplierPayment.supplier_id, isouter=True).filter(
            or_(
                SupplierPayment.payment_number.ilike(s),
                SupplierPayment.reference.ilike(s),
                Supplier.name.ilike(s),
            )
        )

    payments = q.order_by(SupplierPayment.paid_on.desc(), SupplierPayment.id.desc()).all()

    total_paid = round(sum(p.amount for p in payments), 2)
    allocated_amount = round(sum(p.allocated_amount for p in payments), 2)
    unallocated_amount = round(sum(p.unallocated_amount for p in payments), 2)

    summary = {
        "total_paid": total_paid,
        "payment_count": len(payments),
        "allocated_amount": allocated_amount,
        "unallocated_amount": unallocated_amount,
    }

    rows = [
        {
            "payment_number": p.payment_number or p.id[:8],
            "date": p.paid_on.date().isoformat() if hasattr(p.paid_on, "date") else str(p.paid_on)[:10],
            "supplier": p.supplier.name if p.supplier else None,
            "amount": round(p.amount, 2),
            "method": p.payment_mode or p.payment_method or "cash",
            "reference": p.reference,
            "allocated": round(p.allocated_amount, 2),
            "unallocated": round(p.unallocated_amount, 2),
            "status": p.status,
        }
        for p in payments
    ]

    paged_rows, pagination = _paginate(rows, page, page_size)

    meta = {
        "currency": "INR",
        "group_by": None,
        "columns": [
            {"key": "payment_number", "label": "Payment #", "type": "reference"},
            {"key": "date", "label": "Date", "type": "date"},
            {"key": "supplier", "label": "Supplier", "type": "text"},
            {"key": "amount", "label": "Amount", "type": "currency"},
            {"key": "method", "label": "Method", "type": "text"},
            {"key": "reference", "label": "Reference", "type": "text"},
            {"key": "allocated", "label": "Allocated", "type": "currency"},
            {"key": "unallocated", "label": "Unallocated", "type": "currency"},
            {"key": "status", "label": "Status", "type": "status"},
        ],
    }

    daily_sp: dict[str, float] = defaultdict(float)
    for p in payments:
        d_str = p.paid_on.date().isoformat() if hasattr(p.paid_on, "date") else str(p.paid_on)[:10]
        daily_sp[d_str] += p.amount

    chart = {
        "type": "line",
        "title": "Supplier Payments Over Time",
        "data": [{"date": d, "amount": round(amt, 2)} for d, amt in sorted(daily_sp.items())],
    }

    return {
        "summary": summary,
        "rows": paged_rows,
        "pagination": pagination,
        "meta": meta,
        "chart": chart,
    }


def _inventory_summary(db: Session, org_id: str, df: datetime | None, dt: datetime | None, params: dict) -> dict:
    page = int(params.get("page", 1))
    page_size = int(params.get("page_size", 25))
    search = params.get("search")
    warehouse_id = params.get("warehouse_id")
    product_id = params.get("product_id")
    category_id = params.get("category_id")
    brand_id = params.get("brand_id")
    low_stock_only = str(params.get("low_stock_only", "")).lower() in ("true", "1", "yes")

    if warehouse_id:
        _verify_tenant_entity(db, org_id, Warehouse, warehouse_id, "Warehouse")
    if product_id:
        _verify_tenant_entity(db, org_id, Product, product_id, "Product")

    # Query warehouse stock
    q = db.query(WarehouseStock).join(Product, Product.id == WarehouseStock.product_id).filter(
        WarehouseStock.organization_id == org_id,
    )

    if warehouse_id:
        q = q.filter(WarehouseStock.warehouse_id == warehouse_id)
    if product_id:
        q = q.filter(WarehouseStock.product_id == product_id)
    if category_id:
        q = q.filter(Product.category_id == category_id)
    if brand_id:
        q = q.filter(Product.brand_id == brand_id)
    if search:
        s = f"%{search}%"
        q = q.filter(
            or_(
                Product.name.ilike(s),
                Product.sku.ilike(s),
                Product.product_id.ilike(s),
            )
        )

    stocks = q.order_by(Product.name.asc()).all()

    # Active reservations per (warehouse_id, product_id, variant_id)
    res_q = db.query(
        StockReservation.warehouse_id,
        StockReservation.product_id,
        StockReservation.variant_id,
        func.sum(StockReservation.reserved_quantity - StockReservation.consumed_quantity).label("held"),
    ).filter(
        StockReservation.organization_id == org_id,
        StockReservation.status == "active",
    ).group_by(
        StockReservation.warehouse_id,
        StockReservation.product_id,
        StockReservation.variant_id,
    ).all()

    res_map: dict[tuple[str, str, str | None], float] = {
        (r[0], r[1], r[2]): float(r[3] or 0.0) for r in res_q
    }

    rows = []
    total_on_hand = 0.0
    total_reserved = 0.0
    total_available = 0.0
    inventory_value = 0.0
    low_stock_count = 0
    skus_set = set()

    for ws in stocks:
        p = db.get(Product, ws.product_id)
        if not p:
            continue
        v = ws.variant_id
        variant_obj = db.get(ProductVariant, v) if v else None

        on_hand = float(ws.on_hand_quantity or 0.0)
        held = res_map.get((ws.warehouse_id, ws.product_id, v), 0.0)
        available = max(on_hand - held, 0.0)

        cost_price = 0.0
        if p.pricing and p.pricing.purchase_price:
            cost_price = p.pricing.purchase_price
        elif p.price:
            cost_price = p.price

        stock_val = round(on_hand * cost_price, 2)
        min_level = p.minimum_stock_level or p.reorder_level or 0
        is_low_stock = (on_hand <= min_level) and min_level > 0

        # Accumulate full summary metrics
        total_on_hand = round(total_on_hand + on_hand, 2)
        total_reserved = round(total_reserved + held, 2)
        total_available = round(total_available + available, 2)
        inventory_value = round(inventory_value + stock_val, 2)
        skus_set.add(p.sku or p.id)
        if is_low_stock:
            low_stock_count += 1

        if low_stock_only and not is_low_stock:
            continue

        rows.append({
            "product_id": p.product_id or p.id[:8],
            "product": p.name,
            "sku": variant_obj.sku if variant_obj else p.sku,
            "variant": variant_obj.name if variant_obj else None,
            "warehouse": ws.warehouse.name if ws.warehouse else "Main",
            "on_hand": on_hand,
            "reserved": held,
            "available": available,
            "cost_price": cost_price,
            "stock_value": stock_val,
            "low_stock_status": "Low Stock" if is_low_stock else "Normal",
        })

    summary = {
        "sku_count": len(skus_set),
        "total_on_hand": total_on_hand,
        "total_reserved": total_reserved,
        "total_available": total_available,
        "inventory_value": inventory_value,
        "low_stock_count": low_stock_count,
    }

    paged_rows, pagination = _paginate(rows, page, page_size)

    meta = {
        "currency": "INR",
        "group_by": None,
        "valuation_basis": "Current cost valuation",
        "columns": [
            {"key": "product", "label": "Product", "type": "text"},
            {"key": "sku", "label": "SKU", "type": "text"},
            {"key": "variant", "label": "Variant", "type": "text"},
            {"key": "warehouse", "label": "Warehouse", "type": "text"},
            {"key": "on_hand", "label": "On Hand", "type": "number"},
            {"key": "reserved", "label": "Reserved", "type": "number"},
            {"key": "available", "label": "Available", "type": "number"},
            {"key": "cost_price", "label": "Cost Price", "type": "currency"},
            {"key": "stock_value", "label": "Stock Value", "type": "currency"},
            {"key": "low_stock_status", "label": "Status", "type": "status"},
        ],
    }

    wh_val: dict[str, float] = defaultdict(float)
    for r in rows:
        wh_val[r["warehouse"]] += r["stock_value"]

    chart = {
        "type": "bar",
        "title": "Inventory Value by Warehouse",
        "data": [{"warehouse": k, "value": round(v, 2)} for k, v in wh_val.items()],
    }

    return {
        "summary": summary,
        "rows": paged_rows,
        "pagination": pagination,
        "meta": meta,
        "chart": chart,
    }


def _stock_movement(db: Session, org_id: str, df: datetime | None, dt: datetime | None, params: dict) -> dict:
    page = int(params.get("page", 1))
    page_size = int(params.get("page_size", 25))
    search = params.get("search")
    warehouse_id = params.get("warehouse_id")
    product_id = params.get("product_id")
    movement_type = params.get("movement_type")

    if warehouse_id:
        _verify_tenant_entity(db, org_id, Warehouse, warehouse_id, "Warehouse")
    if product_id:
        _verify_tenant_entity(db, org_id, Product, product_id, "Product")

    q = db.query(StockMovement).filter(StockMovement.organization_id == org_id)
    q = _between(q, StockMovement.created_at, df, dt)

    if warehouse_id:
        q = q.filter(StockMovement.warehouse_id == warehouse_id)
    if product_id:
        q = q.filter(StockMovement.product_id == product_id)
    if movement_type:
        q = q.filter(StockMovement.movement_type == movement_type.lower())
    if search:
        s = f"%{search}%"
        q = q.join(Product, Product.id == StockMovement.product_id, isouter=True).filter(
            or_(
                Product.name.ilike(s),
                Product.sku.ilike(s),
                StockMovement.movement_type.ilike(s),
                StockMovement.note.ilike(s),
            )
        )

    movements = q.order_by(StockMovement.created_at.desc(), StockMovement.id.desc()).all()

    stock_in = sum(m.quantity for m in movements if m.quantity > 0)
    stock_out = sum(abs(m.quantity) for m in movements if m.quantity < 0)

    summary = {
        "stock_in": stock_in,
        "stock_out": stock_out,
        "movement_count": len(movements),
    }

    rows = [
        {
            "date_time": m.created_at.isoformat(),
            "warehouse": m.warehouse.name if m.warehouse else None,
            "product": m.product.name if m.product else "Unknown Product",
            "variant": m.variant.name if m.variant else None,
            "movement_type": m.movement_type,
            "signed_quantity": m.quantity,
            "balance_after": m.balance_after,
            "reference": m.note,
            "actor": m.created_by,
        }
        for m in movements
    ]

    paged_rows, pagination = _paginate(rows, page, page_size)

    meta = {
        "currency": "INR",
        "group_by": None,
        "columns": [
            {"key": "date_time", "label": "Date & Time", "type": "datetime"},
            {"key": "warehouse", "label": "Warehouse", "type": "text"},
            {"key": "product", "label": "Product", "type": "text"},
            {"key": "variant", "label": "Variant", "type": "text"},
            {"key": "movement_type", "label": "Movement Type", "type": "text"},
            {"key": "signed_quantity", "label": "Quantity", "type": "number"},
            {"key": "balance_after", "label": "Balance After", "type": "number"},
            {"key": "reference", "label": "Reference / Note", "type": "text"},
        ],
    }

    chart = {
        "type": "bar",
        "title": "Stock In vs Stock Out Volume",
        "data": [
            {"type": "Stock In (+)", "quantity": stock_in},
            {"type": "Stock Out (-)", "quantity": stock_out},
        ],
    }

    return {
        "summary": summary,
        "rows": paged_rows,
        "pagination": pagination,
        "meta": meta,
        "chart": chart,
    }


def _sales_overview(db: Session, org_id: str, df: datetime | None, dt: datetime | None, params: dict) -> dict:
    """Operational Sales Overview report rooted on SalesOrder with multiple delivery rollup."""
    page = int(params.get("page", 1))
    page_size = int(params.get("page_size", 25))
    search = params.get("search")
    customer_id = params.get("customer_id")
    salesperson_id = params.get("salesperson_id")
    order_status = params.get("order_status") or params.get("status")
    fulfilment_status = params.get("fulfilment_status")
    delivery_status = params.get("delivery_status")

    if customer_id:
        _verify_tenant_entity(db, org_id, Customer, customer_id, "Customer")
    if salesperson_id:
        _verify_tenant_entity(db, org_id, User, salesperson_id, "Salesperson")

    q = (
        db.query(SalesOrder)
        .options(
            selectinload(SalesOrder.deliveries).selectinload(Delivery.items),
            joinedload(SalesOrder.customer),
            joinedload(SalesOrder.salesperson),
            joinedload(SalesOrder.items),
        )
        .filter(SalesOrder.organization_id == org_id)
    )

    if df is not None:
        q = q.filter(func.coalesce(SalesOrder.order_date, SalesOrder.created_at) >= df)
    if dt is not None:
        q = q.filter(func.coalesce(SalesOrder.order_date, SalesOrder.created_at) <= dt)

    if customer_id:
        q = q.filter(SalesOrder.customer_id == customer_id)
    if salesperson_id:
        q = q.filter(SalesOrder.salesperson_id == salesperson_id)
    if order_status:
        q = q.filter(SalesOrder.status == order_status)
    if fulfilment_status:
        q = q.filter(SalesOrder.fulfilment_status == fulfilment_status)

    if search:
        s = f"%{search}%"
        q = q.outerjoin(Customer, Customer.id == SalesOrder.customer_id).outerjoin(
            Delivery, Delivery.sales_order_id == SalesOrder.id
        ).filter(
            or_(
                SalesOrder.order_number.ilike(s),
                SalesOrder.sales_order_number.ilike(s),
                Customer.name.ilike(s),
                Customer.business_name.ilike(s),
                Customer.customer_id.ilike(s),
                Delivery.delivery_note_number.ilike(s),
            )
        ).distinct()

    orders = q.order_by(func.coalesce(SalesOrder.order_date, SalesOrder.created_at).desc(), SalesOrder.id.desc()).all()

    if delivery_status:
        filtered_orders = []
        for o in orders:
            active_d = [d for d in o.deliveries if d.status != "cancelled"]
            if any(d.status == delivery_status for d in active_d):
                filtered_orders.append(o)
        orders = filtered_orders

    # Collect partner and vehicle IDs for batch lookup to prevent N+1 queries
    partner_ids = set()
    vehicle_ids = set()
    for o in orders:
        for d in o.deliveries:
            if d.delivery_partner_id:
                partner_ids.add(d.delivery_partner_id)
            if d.vehicle_id:
                vehicle_ids.add(d.vehicle_id)

    partners_map = {u.id: u.name for u in db.query(User.id, User.name).filter(User.id.in_(partner_ids)).all()} if partner_ids else {}
    vehicles_map = {v.id: v.vehicle_number for v in db.query(Vehicle.id, Vehicle.vehicle_number).filter(Vehicle.id.in_(vehicle_ids)).all()} if vehicle_ids else {}

    all_rows = []
    total_sales_value = 0.0
    total_ordered_qty = 0.0
    total_delivered_qty = 0.0
    total_remaining_qty = 0.0
    not_started_count = 0
    planned_count = 0
    partially_delivered_count = 0
    delivered_count = 0
    cancelled_count = 0

    for o in orders:
        is_cancelled = (o.status == "cancelled")
        if not is_cancelled:
            total_sales_value = round(total_sales_value + o.total, 2)
        else:
            cancelled_count += 1

        ord_qty = float(sum(item.quantity or 0 for item in o.items))
        deliv_qty = float(sum(item.delivered_quantity or 0 for item in o.items))
        rem_qty = max(ord_qty - deliv_qty, 0.0)

        total_ordered_qty = round(total_ordered_qty + ord_qty, 2)
        total_delivered_qty = round(total_delivered_qty + deliv_qty, 2)
        total_remaining_qty = round(total_remaining_qty + rem_qty, 2)

        f_st = (o.fulfilment_status or "not_started").lower()
        if f_st in ("not_started", "pending", "reserved"):
            not_started_count += 1
        elif f_st in ("planned", "loaded", "in_transit"):
            planned_count += 1
        elif f_st == "partially_delivered":
            partially_delivered_count += 1
        elif f_st == "delivered":
            delivered_count += 1

        active_deliveries = [d for d in o.deliveries if d.status != "cancelled"]
        active_deliveries.sort(key=lambda d: d.delivery_date or d.created_at, reverse=True)
        latest_d = active_deliveries[0] if active_deliveries else None

        latest_d_dict = None
        if latest_d:
            partner_name = partners_map.get(latest_d.delivery_partner_id) if latest_d.delivery_partner_id else None
            vehicle_num = vehicles_map.get(latest_d.vehicle_id) if latest_d.vehicle_id else None
            latest_d_dict = {
                "id": latest_d.id,
                "delivery_number": latest_d.delivery_note_number,
                "status": latest_d.status,
                "scheduled_date": latest_d.scheduled_date.date().isoformat() if latest_d.scheduled_date else None,
                "delivery_partner": {
                    "id": latest_d.delivery_partner_id,
                    "name": partner_name,
                } if latest_d.delivery_partner_id else None,
                "vehicle": {
                    "id": latest_d.vehicle_id,
                    "vehicle_number": vehicle_num,
                } if latest_d.vehicle_id else None,
            }

        cust = o.customer
        salesp = o.salesperson
        o_date_val = o.order_date or o.created_at
        o_date_str = o_date_val.date().isoformat() if hasattr(o_date_val, "date") else str(o_date_val)[:10]

        all_rows.append({
            "order_id": o.id,
            "order_number": o.order_number,
            "sales_order_number": o.sales_order_number,
            "order_date": o_date_str,
            "customer": {
                "id": o.customer_id,
                "customer_id": cust.customer_id if cust else None,
                "customer_code": cust.customer_id if cust else None,
                "name": (cust.business_name or cust.name) if cust else "Walk-in",
            },
            "customer_name": (cust.business_name or cust.name) if cust else "Walk-in",
            "salesperson": {
                "id": o.salesperson_id,
                "name": salesp.name if salesp else "Unassigned",
            },
            "salesperson_name": salesp.name if salesp else "Unassigned",
            "order_total": round(o.total, 2),
            "order_status": o.status,
            "fulfilment_status": o.fulfilment_status or "not_started",
            "ordered_quantity": ord_qty,
            "delivered_quantity": deliv_qty,
            "remaining_quantity": rem_qty,
            "delivery_count": len(active_deliveries),
            "latest_delivery": latest_d_dict,
            "latest_delivery_number": latest_d.delivery_note_number if latest_d else None,
            "latest_delivery_status": latest_d.status if latest_d else None,
        })

    summary = {
        "total_orders": len(orders),
        "total_sales_value": total_sales_value,
        "not_started": not_started_count,
        "planned": planned_count,
        "partially_delivered": partially_delivered_count,
        "delivered": delivered_count,
        "cancelled_orders": cancelled_count,
        "total_ordered_quantity": total_ordered_qty,
        "total_delivered_quantity": total_delivered_qty,
        "total_remaining_quantity": total_remaining_qty,
    }

    paged_rows, pagination = _paginate(all_rows, page, page_size)

    meta = {
        "currency": "INR",
        "group_by": None,
        "columns": [
            {"key": "order_number", "label": "Order #", "type": "reference"},
            {"key": "order_date", "label": "Order Date", "type": "date"},
            {"key": "customer_name", "label": "Customer", "type": "text"},
            {"key": "salesperson_name", "label": "Salesperson", "type": "text"},
            {"key": "order_total", "label": "Total Amount", "type": "currency"},
            {"key": "order_status", "label": "Order Status", "type": "status"},
            {"key": "fulfilment_status", "label": "Fulfilment", "type": "status"},
            {"key": "ordered_quantity", "label": "Ordered Qty", "type": "number"},
            {"key": "delivered_quantity", "label": "Delivered Qty", "type": "number"},
            {"key": "remaining_quantity", "label": "Remaining Qty", "type": "number"},
            {"key": "delivery_count", "label": "Deliveries", "type": "number"},
            {"key": "latest_delivery_number", "label": "Latest Delivery #", "type": "reference"},
            {"key": "latest_delivery_status", "label": "Delivery Status", "type": "status"},
        ],
    }

    chart = {
        "type": "bar",
        "title": "Fulfilment Status Distribution",
        "data": [
            {"status": "Not Started / Pending", "count": not_started_count},
            {"status": "Planned / In Transit", "count": planned_count},
            {"status": "Partially Delivered", "count": partially_delivered_count},
            {"status": "Delivered", "count": delivered_count},
            {"status": "Cancelled", "count": cancelled_count},
        ],
    }

    return {
        "summary": summary,
        "rows": paged_rows,
        "pagination": pagination,
        "meta": meta,
        "chart": chart,
    }


def _cash_flow_sheet(db: Session, org_id: str, df: datetime | None, dt: datetime | None, params: dict) -> dict:
    """Recorded Cash Movements Cash Flow Sheet matching Daily Transaction semantics."""
    page = int(params.get("page", 1))
    page_size = int(params.get("page_size", 25))
    search = params.get("search")

    # 1. Customer Payments (Cash Inflow)
    pay_in_q = db.query(CustomerPayment).filter(
        CustomerPayment.organization_id == org_id,
    )
    pay_in_q = _between(pay_in_q, CustomerPayment.received_on, df, dt)
    customer_payments = pay_in_q.all()

    # 2. Supplier Payments (Cash Outflow, excluding void)
    pay_out_q = db.query(SupplierPayment).filter(
        SupplierPayment.organization_id == org_id,
        SupplierPayment.status != "void",
    )
    pay_out_q = _between(pay_out_q, SupplierPayment.paid_on, df, dt)
    supplier_payments = pay_out_q.all()

    # 3. Expenses (Approved and actually paid/settled/completed)
    exp_q = db.query(Expense).filter(
        Expense.organization_id == org_id,
        Expense.status == "approved",
    )
    exp_q = _between(exp_q, Expense.expense_date, df, dt)
    expenses = exp_q.all()

    def _is_paid_expense(e: Expense) -> bool:
        ps = (e.payment_status or "").strip().lower()
        return ps in ("paid", "settled", "completed")

    paid_expenses = [e for e in expenses if _is_paid_expense(e)]

    all_rows = []

    for cp in customer_payments:
        cust = db.get(Customer, cp.customer_id) if cp.customer_id else None
        party_name = (cust.business_name or cust.name) if cust else "Unknown Customer"
        d_val = cp.received_on
        d_str = d_val.date().isoformat() if hasattr(d_val, "date") else str(d_val)[:10]
        all_rows.append({
            "timestamp": d_val,
            "date": d_str,
            "transaction_type": "Customer Payment",
            "reference_number": cp.reference or cp.receipt_number or cp.id[:8],
            "party": party_name,
            "cash_in": round(cp.amount, 2),
            "cash_out": 0.0,
            "amount": round(cp.amount, 2),
            "payment_mode": cp.payment_mode or "cash",
            "category": "Customer Collection",
        })

    for sp in supplier_payments:
        supplier_name = sp.supplier.name if sp.supplier else "Unknown Supplier"
        d_val = sp.paid_on
        d_str = d_val.date().isoformat() if hasattr(d_val, "date") else str(d_val)[:10]
        all_rows.append({
            "timestamp": d_val,
            "date": d_str,
            "transaction_type": "Supplier Payment",
            "reference_number": sp.payment_number or sp.reference or sp.id[:8],
            "party": supplier_name,
            "cash_in": 0.0,
            "cash_out": round(sp.amount, 2),
            "amount": round(-sp.amount, 2),
            "payment_mode": sp.payment_mode or sp.payment_method or "cash",
            "category": "Supplier Disbursement",
        })

    for exp in paid_expenses:
        d_val = exp.expense_date
        d_str = d_val.date().isoformat() if hasattr(d_val, "date") else str(d_val)[:10]
        all_rows.append({
            "timestamp": d_val,
            "date": d_str,
            "transaction_type": "Paid Expense",
            "reference_number": exp.expense_number or exp.category or exp.id[:8],
            "party": _expense_vendor_name(exp) or exp.submitted_by or "Expense Payee",
            "cash_in": 0.0,
            "cash_out": round(exp.amount, 2),
            "amount": round(-exp.amount, 2),
            "payment_mode": exp.payment_mode or "paid",
            "category": exp.category or "Operating Expense",
        })

    if search:
        s_lower = search.lower()
        all_rows = [
            r for r in all_rows
            if s_lower in (r["reference_number"] or "").lower()
            or s_lower in (r["party"] or "").lower()
            or s_lower in (r["transaction_type"] or "").lower()
            or s_lower in (r["category"] or "").lower()
            or s_lower in (r["payment_mode"] or "").lower()
        ]

    # Chronological sort ascending to calculate running balance
    all_rows.sort(key=lambda r: r["timestamp"] or datetime.min)
    running = 0.0
    for r in all_rows:
        running = round(running + r["amount"], 2)
        r["running_balance"] = running

    daily_cash_in: dict[str, float] = defaultdict(float)
    daily_cash_out: dict[str, float] = defaultdict(float)
    for r in all_rows:
        daily_cash_in[r["date"]] += r["cash_in"]
        daily_cash_out[r["date"]] += r["cash_out"]

    all_dates = sorted(set(list(daily_cash_in.keys()) + list(daily_cash_out.keys())))
    chart_data = [
        {
            "date": d,
            "cash_in": round(daily_cash_in[d], 2),
            "cash_out": round(daily_cash_out[d], 2),
            "net_flow": round(daily_cash_in[d] - daily_cash_out[d], 2),
        }
        for d in all_dates
    ]

    # Display rows in reverse chronological order
    all_rows.reverse()
    for r in all_rows:
        r.pop("timestamp", None)

    cash_in_total = round(sum(cp.amount for cp in customer_payments), 2)
    supplier_payments_total = round(sum(sp.amount for sp in supplier_payments), 2)
    paid_expenses_total = round(sum(e.amount for e in paid_expenses), 2)
    cash_out_total = round(supplier_payments_total + paid_expenses_total, 2)
    net_cash_flow = round(cash_in_total - cash_out_total, 2)

    summary = {
        "cash_in": cash_in_total,
        "cash_out": cash_out_total,
        "net_cash_flow": net_cash_flow,
        "customer_payments_count": len(customer_payments),
        "supplier_payments_count": len(supplier_payments),
        "paid_expenses_count": len(paid_expenses),
        "total_transactions": len(customer_payments) + len(supplier_payments) + len(paid_expenses),
    }

    paged_rows, pagination = _paginate(all_rows, page, page_size)

    meta = {
        "currency": "INR",
        "group_by": None,
        "notes": "Represents recorded cash transactions (Customer Payments, Supplier Payments, Paid Expenses). Does not include accrual invoices or unrecorded opening bank balances.",
        "columns": [
            {"key": "date", "label": "Date", "type": "date"},
            {"key": "transaction_type", "label": "Transaction Type", "type": "text"},
            {"key": "reference_number", "label": "Reference #", "type": "reference"},
            {"key": "party", "label": "Party / Payee", "type": "text"},
            {"key": "cash_in", "label": "Cash In (+)", "type": "currency"},
            {"key": "cash_out", "label": "Cash Out (-)", "type": "currency"},
            {"key": "payment_mode", "label": "Payment Mode", "type": "text"},
            {"key": "category", "label": "Category", "type": "text"},
            {"key": "running_balance", "label": "Period Net Movement", "type": "currency"},
        ],
    }

    chart = {
        "type": "bar",
        "title": "Daily Cash In vs Cash Out",
        "data": chart_data,
    }

    return {
        "summary": summary,
        "rows": paged_rows,
        "pagination": pagination,
        "meta": meta,
        "chart": chart,
    }


_BUILDERS = {
    "daily-transaction": _daily_transaction,
    "sales": _sales,
    "purchase": _purchase,
    "customer-outstanding": _customer_outstanding,
    "supplier-outstanding": _supplier_outstanding,
    "payment-collection": _payment_collection,
    "expense": _expense,
    "cash-collection": _cash_collection,
    "gst-summary": _gst_summary,
    "sales-return": _sales_return,
    "purchase-return": _purchase_return,
    "profit-loss": _profit_loss,
    "supplier-payment": _supplier_payment,
    "inventory-summary": _inventory_summary,
    "stock-movement": _stock_movement,
    "sales-overview": _sales_overview,
    "cash-flow-sheet": _cash_flow_sheet,
}


from app.core.entitlements import REPORT_TYPE_TO_ENTITLEMENT
from app.services import entitlement_service


def build_report(
    db: Session,
    org_id: str,
    report_type: str,
    date_from: str | None = None,
    date_to: str | None = None,
    filters: dict[str, Any] | None = None,
    is_export: bool = False,
) -> dict[str, Any]:
    """Build standardized report response for screen or export."""
    if report_type not in _BUILDERS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown report type '{report_type}'. Valid: {sorted(REPORT_TYPES)}",
        )

    entitlement_key = REPORT_TYPE_TO_ENTITLEMENT.get(report_type)
    if entitlement_key:
        entitlement_service.check_entitlement(db, org_id, entitlement_key)

    df, dt = _range(date_from, date_to, db=db, org_id=org_id)
    params = filters.copy() if filters else {}
    # gst-summary validates per view (summary vs transaction) inside its builder.
    if report_type != "gst-summary":
        _reject_unsupported_filters(params, REPORT_REGISTRY[report_type]["supported_filters"], f"the {report_type} report")

    # If export requested, fetch full dataset (up to 10000)
    if is_export:
        params["page"] = 1
        params["page_size"] = 10000

    result = _BUILDERS[report_type](db, org_id, df, dt, params)

    if is_export:
        total_rows = result.get("pagination", {}).get("total", len(result.get("rows", [])))
        if total_rows > 10000:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Export limit exceeded ({total_rows} rows). Maximum supported export is 10,000 rows. Please filter by date range or specific criteria.",
            )

    result["type"] = report_type
    result["date_from"] = date_from
    result["date_to"] = date_to

    return result
