"""Build the Admin Dashboard summary.

Everything is computed from the same tables the reports read, and with the same
definitions, so a figure on the dashboard matches the report behind it:

* A **sale** is a sales order in one of `report_service.SALE_STATUSES` — an order
  still pending approval is not revenue yet.
* **Purchases** are approved purchase invoices; **expenses** are approved expenses.
* **Gross profit** is sales − purchases and **net profit** is gross − expenses,
  which is exactly what the profit-loss report reports. There is no cost price on
  a product yet, so a true COGS-based margin is not available.

The organization always comes from the authenticated user, never from the request.
"""

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models import (
    Customer,
    CustomerPayment,
    Expense,
    Invoice,
    Product,
    PurchaseInvoice,
    SalesOrder,
    SalesOrderItem,
    Supplier,
    SupplierPayment,
)
from app.schemas.dashboard import (
    AdminDashboardOut,
    CashflowPoint,
    DashboardFilters,
    DashboardOrders,
    DashboardSummary,
    ExpenseSlice,
    ReceivablesPayables,
    RecentOrder,
    SalesTrendPoint,
    StockWatchItem,
    TopCustomer,
    TopProduct,
)
from app.services.report_service import SALE_STATUSES

# How long a bill may go unpaid before it counts as overdue, when the customer has
# no payment terms of their own.
DEFAULT_DUE_DAYS = 30
TOP_N = 5
RECENT_ORDERS = 10
STOCK_WATCH_N = 10

# Fulfilment states with work still to do. Order status and fulfilment are separate
# axes now, so the "to deliver" tile reads the goods side.
_TO_DELIVER = ("reserved", "planned", "loaded", "in_transit", "partially_delivered")


def _day(value: str | None, fallback: date, end: bool = False) -> tuple[date, datetime]:
    """Parse a YYYY-MM-DD filter into its date and the instant to compare against."""
    if value is None:
        parsed = fallback
    else:
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Dates must be YYYY-MM-DD"
            )
    moment = datetime.combine(parsed, time.max if end else time.min, tzinfo=timezone.utc)
    return parsed, moment


def _resolve_range(date_from: str | None, date_to: str | None) -> tuple[date, date, datetime, datetime]:
    """The window to report on — the current month to date when nothing is asked for."""
    today = datetime.now(timezone.utc).date()
    start, df = _day(date_from, today.replace(day=1))
    end, dt = _day(date_to, today, end=True)
    if start > end:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="date_from cannot be after date_to"
        )
    return start, end, df, dt


def _sale_orders(db: Session, org_id: str, customer_id: str | None):
    query = db.query(SalesOrder).filter(
        SalesOrder.organization_id == org_id, SalesOrder.status.in_(SALE_STATUSES)
    )
    if customer_id:
        query = query.filter(SalesOrder.customer_id == customer_id)
    return query


def _sales_total(db: Session, org_id: str, customer_id: str | None, df: datetime, dt: datetime) -> float:
    orders = _sale_orders(db, org_id, customer_id).filter(
        SalesOrder.created_at >= df, SalesOrder.created_at <= dt
    )
    return round(sum(o.total or 0 for o in orders), 2)


def _purchase_invoices(db: Session, org_id: str, supplier_id: str | None, warehouse_id: str | None):
    query = db.query(PurchaseInvoice).filter(
        PurchaseInvoice.organization_id == org_id, PurchaseInvoice.status == "approved"
    )
    if supplier_id:
        query = query.filter(PurchaseInvoice.supplier_id == supplier_id)
    if warehouse_id:
        query = query.filter(PurchaseInvoice.warehouse_id == warehouse_id)
    return query


def _summary(
    db: Session, org_id: str, start: date, end: date, df: datetime, dt: datetime,
    period_sales: float, purchases: float, expenses: float, customer_id: str | None,
) -> DashboardSummary:
    today = datetime.now(timezone.utc).date()
    _, today_start = _day(today.isoformat(), today)
    _, today_end = _day(today.isoformat(), today, end=True)
    _, month_start = _day(today.replace(day=1).isoformat(), today)

    # Previous window of the same length, so growth compares like with like.
    span = (end - start).days + 1
    _, prev_from = _day((start - timedelta(days=span)).isoformat(), start)
    _, prev_to = _day((start - timedelta(days=1)).isoformat(), start, end=True)
    previous = _sales_total(db, org_id, customer_id, prev_from, prev_to)
    growth = round((period_sales - previous) / previous * 100, 2) if previous else 0.0

    new_customers = (
        db.query(Customer)
        .filter(
            Customer.organization_id == org_id,
            Customer.created_at >= df,
            Customer.created_at <= dt,
        )
        .count()
    )
    gross_profit = round(period_sales - purchases, 2)
    return DashboardSummary(
        today_sales=_sales_total(db, org_id, customer_id, today_start, today_end),
        month_sales=_sales_total(db, org_id, customer_id, month_start, today_end),
        period_sales=period_sales,
        purchases=purchases,
        expenses=expenses,
        gross_profit=gross_profit,
        net_profit=round(gross_profit - expenses, 2),
        new_customers=new_customers,
        sales_growth_percentage=growth,
    )


def _orders(db: Session, org_id: str, customer_id: str | None, df: datetime, dt: datetime) -> DashboardOrders:
    query = db.query(SalesOrder).filter(
        SalesOrder.organization_id == org_id,
        SalesOrder.created_at >= df,
        SalesOrder.created_at <= dt,
    )
    if customer_id:
        query = query.filter(SalesOrder.customer_id == customer_id)
    rows = query.with_entities(SalesOrder.status, SalesOrder.fulfilment_status).all()
    return DashboardOrders(
        total=len(rows),
        pending=sum(1 for status_, _ in rows if status_ == "awaiting_approval"),
        to_deliver=sum(1 for _, fulfilment in rows if fulfilment in _TO_DELIVER),
        delivered=sum(1 for _, fulfilment in rows if fulfilment == "delivered"),
        cancelled=sum(1 for status_, _ in rows if status_ == "cancelled"),
    )


def _cashflow(
    db: Session, org_id: str, start: date, end: date, df: datetime, dt: datetime,
    customer_id: str | None, supplier_id: str | None,
) -> list[CashflowPoint]:
    """Money actually received and paid out, per day: customer payments in,
    supplier payments and expenses out."""
    inflow: dict[str, float] = defaultdict(float)
    outflow: dict[str, float] = defaultdict(float)

    payments = db.query(CustomerPayment).filter(
        CustomerPayment.organization_id == org_id,
        CustomerPayment.received_on >= df,
        CustomerPayment.received_on <= dt,
    )
    if customer_id:
        payments = payments.filter(CustomerPayment.customer_id == customer_id)
    for payment in payments:
        inflow[payment.received_on.date().isoformat()] += payment.amount or 0

    paid = db.query(SupplierPayment).filter(
        SupplierPayment.organization_id == org_id,
        SupplierPayment.paid_on >= df,
        SupplierPayment.paid_on <= dt,
    )
    if supplier_id:
        paid = paid.filter(SupplierPayment.supplier_id == supplier_id)
    for payment in paid:
        outflow[payment.paid_on.date().isoformat()] += payment.amount or 0

    for expense in db.query(Expense).filter(
        Expense.organization_id == org_id,
        Expense.status == "approved",
        Expense.expense_date >= df,
        Expense.expense_date <= dt,
    ):
        outflow[expense.expense_date.date().isoformat()] += expense.amount or 0

    # One point per day in the window, so the chart has no gaps to interpolate.
    points = []
    for offset in range((end - start).days + 1):
        key = (start + timedelta(days=offset)).isoformat()
        points.append(
            CashflowPoint(
                date=key, inflow=round(inflow.get(key, 0), 2), outflow=round(outflow.get(key, 0), 2)
            )
        )
    return points


def _due_days(customer: Customer | None) -> int:
    """The customer's own credit period when it reads like `net_30`, else the default."""
    terms = (customer.payment_terms if customer is not None else None) or ""
    digits = "".join(c for c in terms if c.isdigit())
    return int(digits) if digits else DEFAULT_DUE_DAYS


def _receivables_payables(
    db: Session, org_id: str, customer_id: str | None, supplier_id: str | None
) -> ReceivablesPayables:
    """Balances are a position, not a period — they are always "as of now", so the
    date filter does not apply to them."""
    customers = db.query(Customer).filter(Customer.organization_id == org_id)
    if customer_id:
        customers = customers.filter(Customer.id == customer_id)
    customers = customers.all()
    receivables = round(sum(max(c.outstanding_balance or 0, 0) for c in customers), 2)

    suppliers = db.query(Supplier).filter(Supplier.organization_id == org_id)
    if supplier_id:
        suppliers = suppliers.filter(Supplier.id == supplier_id)
    payables = round(sum(max(s.outstanding_payable, 0) for s in suppliers.all()), 2)

    now = datetime.now(timezone.utc)
    by_id = {c.id: c for c in customers}
    invoices = db.query(Invoice).filter(
        Invoice.organization_id == org_id,
        Invoice.is_credit_note.is_(False),
        Invoice.status != "paid",
    )
    if customer_id:
        invoices = invoices.filter(Invoice.customer_id == customer_id)
    overdue_receivables = 0.0
    for invoice in invoices:
        issued = invoice.invoice_date
        if issued.tzinfo is None:
            issued = issued.replace(tzinfo=timezone.utc)
        if (now - issued).days > _due_days(by_id.get(invoice.customer_id)):
            overdue_receivables += max((invoice.total or 0) - (invoice.amount_paid or 0), 0)

    purchases = db.query(PurchaseInvoice).filter(
        PurchaseInvoice.organization_id == org_id,
        PurchaseInvoice.status == "approved",
        PurchaseInvoice.payment_status != "paid",
    )
    if supplier_id:
        purchases = purchases.filter(PurchaseInvoice.supplier_id == supplier_id)
    overdue_payables = 0.0
    for invoice in purchases:
        issued = invoice.invoice_date
        if issued.tzinfo is None:
            issued = issued.replace(tzinfo=timezone.utc)
        if (now - issued).days > DEFAULT_DUE_DAYS:
            overdue_payables += max((invoice.total or 0) - (invoice.amount_paid or 0), 0)

    return ReceivablesPayables(
        receivables=receivables,
        payables=payables,
        overdue_receivables=round(overdue_receivables, 2),
        overdue_payables=round(overdue_payables, 2),
        overdue_after_days=DEFAULT_DUE_DAYS,
    )


def _top_customers(orders: list[SalesOrder]) -> list[TopCustomer]:
    totals: dict[str, dict] = {}
    for order in orders:
        if not order.customer_id:
            continue
        entry = totals.setdefault(
            order.customer_id,
            {"name": (order.customer.business_name or order.customer.name)
             if order.customer else "Unknown", "sales": 0.0, "orders": 0},
        )
        entry["sales"] += order.total or 0
        entry["orders"] += 1
    ranked = sorted(totals.items(), key=lambda kv: kv[1]["sales"], reverse=True)[:TOP_N]
    return [
        TopCustomer(
            customer_id=cid, customer_name=v["name"], sales=round(v["sales"], 2), orders=v["orders"]
        )
        for cid, v in ranked
    ]


def _top_products(db: Session, org_id: str, order_ids: list[str]) -> list[TopProduct]:
    if not order_ids:
        return []
    totals: dict[str, dict] = {}
    items = db.query(SalesOrderItem).filter(SalesOrderItem.order_id.in_(order_ids))
    for item in items:
        # Group by product where known, else by the name snapshot on the line — a
        # deleted product still has to show up in what was sold.
        key = item.product_id or f"name:{item.product_name}"
        entry = totals.setdefault(
            key,
            {"product_id": item.product_id, "name": item.product_name, "amount": 0.0, "qty": 0},
        )
        entry["amount"] += item.line_total or 0
        entry["qty"] += item.quantity or 0
    ranked = sorted(totals.values(), key=lambda v: v["amount"], reverse=True)[:TOP_N]
    return [
        TopProduct(
            product_id=v["product_id"], product_name=v["name"],
            sales_amount=round(v["amount"], 2), quantity=v["qty"],
        )
        for v in ranked
    ]


def _expense_breakdown(db: Session, org_id: str, df: datetime, dt: datetime) -> list[ExpenseSlice]:
    totals: dict[str, float] = defaultdict(float)
    for expense in db.query(Expense).filter(
        Expense.organization_id == org_id,
        Expense.status == "approved",
        Expense.expense_date >= df,
        Expense.expense_date <= dt,
    ):
        totals[expense.category] += expense.amount or 0
    return [
        ExpenseSlice(category=category, amount=round(amount, 2))
        for category, amount in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    ]


def _sales_trend(orders: list[SalesOrder], start: date, end: date) -> list[SalesTrendPoint]:
    sales: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    for order in orders:
        key = order.created_at.date().isoformat()
        sales[key] += order.total or 0
        counts[key] += 1
    points = []
    for offset in range((end - start).days + 1):
        key = (start + timedelta(days=offset)).isoformat()
        points.append(
            SalesTrendPoint(date=key, sales=round(sales.get(key, 0), 2), orders=counts.get(key, 0))
        )
    return points


def _stock_watch(db: Session, org_id: str) -> list[StockWatchItem]:
    """Products at or below their minimum level — always "right now", not a period."""
    watch: list[StockWatchItem] = []
    for product in db.query(Product).filter(Product.organization_id == org_id):
        stock = product.total_stock
        minimum = product.minimum_stock_level
        if stock <= 0:
            status_ = "out_of_stock"
        elif minimum and stock <= minimum:
            status_ = "low_stock"
        else:
            continue
        watch.append(
            StockWatchItem(
                product_id=product.id,
                product_name=product.name,
                stock=stock,
                minimum_stock_level=minimum,
                # Against the minimum level, so 100% means "just at the reorder line".
                stock_percentage=round(stock / minimum * 100) if minimum else 0,
                status=status_,
            )
        )
    watch.sort(key=lambda item: (item.status != "out_of_stock", item.stock_percentage))
    return watch[:STOCK_WATCH_N]


def _recent_orders(db: Session, org_id: str, customer_id: str | None) -> list[RecentOrder]:
    query = db.query(SalesOrder).filter(SalesOrder.organization_id == org_id)
    if customer_id:
        query = query.filter(SalesOrder.customer_id == customer_id)
    orders = query.order_by(SalesOrder.created_at.desc()).limit(RECENT_ORDERS).all()
    if not orders:
        return []

    # One query for the invoices behind all of them, rather than one per order.
    billed: dict[str, list[Invoice]] = defaultdict(list)
    for invoice in db.query(Invoice).filter(
        Invoice.organization_id == org_id,
        Invoice.is_credit_note.is_(False),
        Invoice.order_id.in_([o.id for o in orders]),
    ):
        billed[invoice.order_id].append(invoice)

    recent = []
    for order in orders:
        invoices = billed.get(order.id, [])
        total = sum(i.total or 0 for i in invoices)
        paid = sum(i.amount_paid or 0 for i in invoices)
        if not invoices or paid <= 0:
            payment_status = "unpaid"
        elif paid + 0.01 >= total:
            payment_status = "paid"
        else:
            payment_status = "partial"
        recent.append(
            RecentOrder(
                id=order.id,
                order_number=order.order_number,
                customer_name=(order.customer.business_name or order.customer.name)
                if order.customer else None,
                status=order.status,
                payment_status=payment_status,
                total=round(order.total or 0, 2),
                date=(order.order_date or order.created_at).date().isoformat(),
            )
        )
    return recent


def build_admin_dashboard(
    db: Session,
    org_id: str,
    date_from: str | None = None,
    date_to: str | None = None,
    branch_id: str | None = None,
    warehouse_id: str | None = None,
    customer_id: str | None = None,
    supplier_id: str | None = None,
) -> AdminDashboardOut:
    start, end, df, dt = _resolve_range(date_from, date_to)

    orders = (
        _sale_orders(db, org_id, customer_id)
        .filter(SalesOrder.created_at >= df, SalesOrder.created_at <= dt)
        .order_by(SalesOrder.created_at)
        .all()
    )
    period_sales = round(sum(o.total or 0 for o in orders), 2)
    purchases = round(
        sum(
            i.total or 0
            for i in _purchase_invoices(db, org_id, supplier_id, warehouse_id).filter(
                PurchaseInvoice.invoice_date >= df, PurchaseInvoice.invoice_date <= dt
            )
        ),
        2,
    )
    expenses = round(
        sum(
            e.amount or 0
            for e in db.query(Expense).filter(
                Expense.organization_id == org_id,
                Expense.status == "approved",
                Expense.expense_date >= df,
                Expense.expense_date <= dt,
            )
        ),
        2,
    )

    return AdminDashboardOut(
        filters=DashboardFilters(
            date_from=start.isoformat(), date_to=end.isoformat(), branch_id=branch_id,
            warehouse_id=warehouse_id, customer_id=customer_id, supplier_id=supplier_id,
        ),
        summary=_summary(db, org_id, start, end, df, dt, period_sales, purchases, expenses, customer_id),
        orders=_orders(db, org_id, customer_id, df, dt),
        cashflow=_cashflow(db, org_id, start, end, df, dt, customer_id, supplier_id),
        receivables_payables=_receivables_payables(db, org_id, customer_id, supplier_id),
        top_customers=_top_customers(orders),
        top_products=_top_products(db, org_id, [o.id for o in orders]),
        expense_breakdown=_expense_breakdown(db, org_id, df, dt),
        sales_trend=_sales_trend(orders, start, end),
        stock_watch=_stock_watch(db, org_id),
        recent_orders=_recent_orders(db, org_id, customer_id),
    )
