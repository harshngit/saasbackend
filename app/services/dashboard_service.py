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

import threading
import time as _time
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core import workflow
from app.models import (
    Customer,
    CustomerPayment,
    Expense,
    Invoice,
    Product,
    ProductVariant,
    PurchaseInvoice,
    SalesOrder,
    SalesOrderItem,
    Supplier,
    SupplierPayment,
    User,
    WarehouseStock,
)
from app.schemas.dashboard import (
    AdminDashboardOut,
    CashflowPoint,
    DashboardFilters,
    DashboardOrders,
    DashboardSummary,
    DeliveryPartnerDashboardOut,
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


def _sale_orders(db: Session, org_id: str, customer_id: str | None, company_id: str | None = None):
    eff_org = company_id if company_id else org_id
    query = db.query(SalesOrder).filter(
        SalesOrder.organization_id == eff_org, SalesOrder.status.in_(SALE_STATUSES)
    )
    if customer_id:
        query = query.filter(SalesOrder.customer_id == customer_id)
    return query


def _purchase_invoices(
    db: Session, org_id: str, supplier_id: str | None, warehouse_id: str | None, company_id: str | None = None
):
    eff_org = company_id if company_id else org_id
    query = db.query(PurchaseInvoice).filter(
        PurchaseInvoice.organization_id == eff_org, PurchaseInvoice.status == "approved"
    )
    if supplier_id:
        query = query.filter(PurchaseInvoice.supplier_id == supplier_id)
    if warehouse_id:
        query = query.filter(PurchaseInvoice.warehouse_id == warehouse_id)
    return query


def _sales_total(
    db: Session, org_id: str, customer_id: str | None, company_id: str | None, df: datetime, dt: datetime
) -> float:
    # Only the totals are read -- not whole orders with their customers and lines.
    orders = _sale_orders(db, org_id, customer_id, company_id).filter(
        SalesOrder.created_at >= df, SalesOrder.created_at <= dt
    )
    return round(sum(total or 0 for (total,) in orders.with_entities(SalesOrder.total)), 2)


def _summary(
    db: Session, org_id: str, start: date, end: date, df: datetime, dt: datetime,
    period_sales: float, purchases: float, expenses: float, customer_id: str | None,
    company_id: str | None = None,
) -> DashboardSummary:
    eff_org = company_id if company_id else org_id
    today = datetime.now(timezone.utc).date()
    _, today_start = _day(today.isoformat(), today)
    _, today_end = _day(today.isoformat(), today, end=True)
    _, month_start = _day(today.replace(day=1).isoformat(), today)

    # Previous window of the same length, so growth compares like with like.
    span = (end - start).days + 1
    _, prev_from = _day((start - timedelta(days=span)).isoformat(), start)
    _, prev_to = _day((start - timedelta(days=1)).isoformat(), start, end=True)
    previous = _sales_total(db, org_id, customer_id, company_id, prev_from, prev_to)
    growth = round((period_sales - previous) / previous * 100, 2) if previous else 0.0

    cust_q = db.query(Customer).filter(
        Customer.organization_id == eff_org,
        Customer.created_at >= df,
        Customer.created_at <= dt,
    )
    if customer_id:
        cust_q = cust_q.filter(Customer.id == customer_id)
    new_customers = cust_q.count()

    gross_profit = round(period_sales - purchases, 2)
    return DashboardSummary(
        today_sales=_sales_total(db, org_id, customer_id, company_id, today_start, today_end),
        month_sales=_sales_total(db, org_id, customer_id, company_id, month_start, today_end),
        period_sales=period_sales,
        purchases=purchases,
        expenses=expenses,
        gross_profit=gross_profit,
        net_profit=round(gross_profit - expenses, 2),
        new_customers=new_customers,
        sales_growth_percentage=growth,
    )


def _orders(
    db: Session, org_id: str, customer_id: str | None, company_id: str | None, df: datetime, dt: datetime
) -> DashboardOrders:
    eff_org = company_id if company_id else org_id
    query = db.query(SalesOrder).filter(
        SalesOrder.organization_id == eff_org,
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
    customer_id: str | None, supplier_id: str | None, company_id: str | None = None,
    expense_rows: list | None = None,
) -> list[CashflowPoint]:
    """Money actually received and paid out, per day: customer payments in,
    supplier payments and expenses out."""
    eff_org = company_id if company_id else org_id
    inflow: dict[str, float] = defaultdict(float)
    outflow: dict[str, float] = defaultdict(float)

    payments = db.query(CustomerPayment).filter(
        CustomerPayment.organization_id == eff_org,
        CustomerPayment.received_on >= df,
        CustomerPayment.received_on <= dt,
    )
    if customer_id:
        payments = payments.filter(CustomerPayment.customer_id == customer_id)
    for received_on, amount in payments.with_entities(CustomerPayment.received_on, CustomerPayment.amount):
        inflow[received_on.date().isoformat()] += amount or 0

    paid = db.query(SupplierPayment).filter(
        SupplierPayment.organization_id == eff_org,
        SupplierPayment.paid_on >= df,
        SupplierPayment.paid_on <= dt,
    )
    if supplier_id:
        paid = paid.filter(SupplierPayment.supplier_id == supplier_id)
    for paid_on, amount in paid.with_entities(SupplierPayment.paid_on, SupplierPayment.amount):
        outflow[paid_on.date().isoformat()] += amount or 0

    if not supplier_id and not customer_id:
        for expense in expense_rows if expense_rows is not None else _approved_expenses(db, eff_org, df, dt):
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


def _approved_expenses(db: Session, eff_org: str, df: datetime, dt: datetime) -> list:
    """The period's approved expenses -- amount, category and date only. Read once and
    shared by the expense total, the cash-flow outflow and the expense breakdown."""
    return (
        db.query(Expense)
        .filter(
            Expense.organization_id == eff_org,
            Expense.status == "approved",
            Expense.expense_date >= df,
            Expense.expense_date <= dt,
        )
        .with_entities(Expense.amount, Expense.category, Expense.expense_date)
        .all()
    )


def _due_days(customer: Customer | None) -> int:
    """The customer's own credit period when it reads like `net_30`, else the default."""
    terms = (customer.payment_terms if customer is not None else None) or ""
    digits = "".join(c for c in terms if c.isdigit())
    return int(digits) if digits else DEFAULT_DUE_DAYS


def _receivables_payables(
    db: Session, org_id: str, customer_id: str | None, supplier_id: str | None,
    warehouse_id: str | None = None, company_id: str | None = None,
) -> ReceivablesPayables:
    """Balances are a position, not a period — they are always "as of now", so the
    date filter does not apply to them. Only the columns the figures need are read."""
    eff_org = company_id if company_id else org_id
    customers = db.query(Customer).filter(Customer.organization_id == eff_org)
    if customer_id:
        customers = customers.filter(Customer.id == customer_id)
    customers = customers.with_entities(Customer.id, Customer.outstanding_balance, Customer.payment_terms).all()
    receivables = round(sum(max(c.outstanding_balance or 0, 0) for c in customers), 2)

    suppliers = db.query(Supplier).filter(Supplier.organization_id == eff_org)
    if supplier_id:
        suppliers = suppliers.filter(Supplier.id == supplier_id)
    payables = round(sum(
        max(round((opening or 0) + (purchased or 0) - (paid or 0), 2), 0)  # Supplier.outstanding_payable
        for opening, purchased, paid in suppliers.with_entities(
            Supplier.opening_balance, Supplier.total_purchases, Supplier.total_paid)
    ), 2)

    now = datetime.now(timezone.utc)
    by_id = {c.id: c for c in customers}
    invoices = db.query(Invoice).filter(
        Invoice.organization_id == eff_org,
        Invoice.is_credit_note.is_(False),
        Invoice.status != "paid",
    )
    if customer_id:
        invoices = invoices.filter(Invoice.customer_id == customer_id)
    overdue_receivables = 0.0
    for issued, inv_customer_id, total, amount_paid in invoices.with_entities(
        Invoice.invoice_date, Invoice.customer_id, Invoice.total, Invoice.amount_paid
    ):
        if issued.tzinfo is None:
            issued = issued.replace(tzinfo=timezone.utc)
        if (now - issued).days > _due_days(by_id.get(inv_customer_id)):
            overdue_receivables += max((total or 0) - (amount_paid or 0), 0)

    purchases = db.query(PurchaseInvoice).filter(
        PurchaseInvoice.organization_id == eff_org,
        PurchaseInvoice.status == "approved",
        PurchaseInvoice.payment_status != "paid",
    )
    if supplier_id:
        purchases = purchases.filter(PurchaseInvoice.supplier_id == supplier_id)
    if warehouse_id:
        purchases = purchases.filter(PurchaseInvoice.warehouse_id == warehouse_id)
    overdue_payables = 0.0
    for issued, total, returned, amount_paid in purchases.with_entities(
        PurchaseInvoice.invoice_date, PurchaseInvoice.total, PurchaseInvoice.return_amount, PurchaseInvoice.amount_paid
    ):
        if issued.tzinfo is None:
            issued = issued.replace(tzinfo=timezone.utc)
        if (now - issued).days > DEFAULT_DUE_DAYS:
            # PurchaseInvoice.outstanding_balance
            overdue_payables += round(max(total - (returned or 0.0) - (amount_paid or 0.0), 0.0), 2)

    return ReceivablesPayables(
        receivables=receivables,
        payables=payables,
        overdue_receivables=round(overdue_receivables, 2),
        overdue_payables=round(overdue_payables, 2),
        overdue_after_days=DEFAULT_DUE_DAYS,
    )


def _top_customers(orders: list) -> list[TopCustomer]:
    totals: dict[str, dict] = {}
    for order in orders:
        if not order.customer_id:
            continue
        has_customer = order.customer_name is not None or order.customer_business_name is not None
        entry = totals.setdefault(
            order.customer_id,
            {"name": (order.customer_business_name or order.customer_name)
             if has_customer else "Unknown", "sales": 0.0, "orders": 0},
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
    items = db.query(SalesOrderItem).filter(SalesOrderItem.order_id.in_(order_ids)).with_entities(
        SalesOrderItem.product_id, SalesOrderItem.product_name, SalesOrderItem.line_total, SalesOrderItem.quantity)
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


def _expense_breakdown(
    db: Session, org_id: str, df: datetime, dt: datetime,
    company_id: str | None = None, supplier_id: str | None = None, customer_id: str | None = None,
    expense_rows: list | None = None,
) -> list[ExpenseSlice]:
    if supplier_id or customer_id:
        return []
    eff_org = company_id if company_id else org_id
    totals: dict[str, float] = defaultdict(float)
    for expense in expense_rows if expense_rows is not None else _approved_expenses(db, eff_org, df, dt):
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


def _stock_watch(
    db: Session, org_id: str, warehouse_id: str | None = None, company_id: str | None = None
) -> list[StockWatchItem]:
    eff_org = company_id if company_id else org_id
    watch: list[StockWatchItem] = []

    wh_stock_map: dict[str, float] = {}
    if warehouse_id:
        ws_rows = db.query(WarehouseStock).filter(
            WarehouseStock.organization_id == eff_org,
            WarehouseStock.warehouse_id == warehouse_id,
        ).all()
        for ws in ws_rows:
            wh_stock_map[ws.product_id] = wh_stock_map.get(ws.product_id, 0.0) + (ws.on_hand_quantity or 0.0)

    # Columns only -- a Product row eagerly joins seven relations. Product.total_stock is
    # the variants' inventory when the product has variants, else total_inventory.
    variant_stock: dict[str, int] = {}
    if not warehouse_id:
        for product_id, inventory in (
            db.query(ProductVariant.product_id, ProductVariant.inventory)
            .join(Product, Product.id == ProductVariant.product_id)
            .filter(Product.organization_id == eff_org)
        ):
            variant_stock[product_id] = variant_stock.get(product_id, 0) + (inventory or 0)
    for product in db.query(Product).filter(Product.organization_id == eff_org).with_entities(
        Product.id, Product.name, Product.total_inventory, Product.minimum_stock_level
    ):
        if warehouse_id:
            stock = int(wh_stock_map.get(product.id, 0))
        elif product.id in variant_stock:
            stock = variant_stock[product.id]
        else:
            stock = product.total_inventory or 0
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


def _recent_orders(
    db: Session, org_id: str, customer_id: str | None, company_id: str | None = None
) -> list[RecentOrder]:
    eff_org = company_id if company_id else org_id
    query = db.query(SalesOrder).filter(SalesOrder.organization_id == eff_org)
    if customer_id:
        query = query.filter(SalesOrder.customer_id == customer_id)
    orders = (
        query.outerjoin(Customer, Customer.id == SalesOrder.customer_id)
        .order_by(SalesOrder.created_at.desc())
        .limit(RECENT_ORDERS)
        .with_entities(SalesOrder.id, SalesOrder.order_number, SalesOrder.status, SalesOrder.total,
                       SalesOrder.order_date, SalesOrder.created_at, Customer.id.label("customer_row_id"),
                       Customer.business_name.label("customer_business_name"), Customer.name.label("customer_name"))
        .all()
    )
    if not orders:
        return []

    # One query for the invoices behind all of them, rather than one per order.
    billed: dict[str, list[Invoice]] = defaultdict(list)
    for invoice in db.query(Invoice).filter(
        Invoice.organization_id == eff_org,
        Invoice.is_credit_note.is_(False),
        Invoice.order_id.in_([o.id for o in orders]),
    ).with_entities(Invoice.order_id, Invoice.total, Invoice.amount_paid):
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
                customer_name=(order.customer_business_name or order.customer_name)
                if order.customer_row_id is not None else None,
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
    company_id: str | None = None,
    warehouse_id: str | None = None,
    customer_id: str | None = None,
    supplier_id: str | None = None,
) -> AdminDashboardOut:
    start, end, df, dt = _resolve_range(date_from, date_to)
    eff_org = company_id if company_id else org_id
    cache_key = (org_id, date_from, date_to, company_id, warehouse_id, customer_id, supplier_id,
                 datetime.now(timezone.utc).date())
    cached = _dashboard_cache_get(cache_key)
    if cached is not None:
        return cached

    # The period's orders as plain rows (id, customer, total, date, customer name) --
    # everything the summary, top customers, top products and trend need.
    orders = (
        _sale_orders(db, eff_org, customer_id, company_id)
        .filter(SalesOrder.created_at >= df, SalesOrder.created_at <= dt)
        .outerjoin(Customer, Customer.id == SalesOrder.customer_id)
        .order_by(SalesOrder.created_at)
        .with_entities(SalesOrder.id, SalesOrder.customer_id, SalesOrder.total, SalesOrder.created_at,
                       Customer.business_name.label("customer_business_name"), Customer.name.label("customer_name"))
        .all()
    )
    period_sales = round(sum(o.total or 0 for o in orders), 2)
    purchases = round(
        sum(
            total or 0
            for (total,) in _purchase_invoices(db, eff_org, supplier_id, warehouse_id, company_id).filter(
                PurchaseInvoice.invoice_date >= df, PurchaseInvoice.invoice_date <= dt
            ).with_entities(PurchaseInvoice.total)
        ),
        2,
    )
    expense_rows = [] if (supplier_id or customer_id) else _approved_expenses(db, eff_org, df, dt)
    expenses = 0.0 if (supplier_id or customer_id) else round(sum(e.amount or 0 for e in expense_rows), 2)

    result = AdminDashboardOut(
        filters=DashboardFilters(
            date_from=start.isoformat(),
            date_to=end.isoformat(),
            company_id=company_id,
            warehouse_id=warehouse_id,
            customer_id=customer_id,
            supplier_id=supplier_id,
        ),
        summary=_summary(db, eff_org, start, end, df, dt, period_sales, purchases, expenses, customer_id, company_id),
        orders=_orders(db, eff_org, customer_id, company_id, df, dt),
        cashflow=_cashflow(db, eff_org, start, end, df, dt, customer_id, supplier_id, company_id,
                           expense_rows=expense_rows),
        receivables_payables=_receivables_payables(db, eff_org, customer_id, supplier_id, warehouse_id, company_id),
        top_customers=_top_customers(orders),
        top_products=_top_products(db, eff_org, [o.id for o in orders]),
        expense_breakdown=_expense_breakdown(db, eff_org, df, dt, company_id, supplier_id, customer_id,
                                             expense_rows=expense_rows),
        sales_trend=_sales_trend(orders, start, end),
        stock_watch=_stock_watch(db, eff_org, warehouse_id, company_id),
        recent_orders=_recent_orders(db, eff_org, customer_id, company_id),
    )
    _dashboard_cache_put(cache_key, result)
    return result


# The whole admin dashboard per organization + filters, for a short while: it is opened
# often and every figure costs database round trips. Any write to the records it reads
# clears it (see _clear_dashboard_cache), so a change made here shows immediately; another
# instance shows it within DASHBOARD_CACHE_SECONDS.
DASHBOARD_CACHE_SECONDS = 30.0
_dashboard_cache: dict[tuple, tuple[float, AdminDashboardOut]] = {}
_dashboard_lock = threading.Lock()


def _dashboard_cache_get(key: tuple) -> AdminDashboardOut | None:
    with _dashboard_lock:
        hit = _dashboard_cache.get(key)
    if hit is None or hit[0] <= _time.monotonic():
        return None
    return hit[1].model_copy(deep=True)


def _dashboard_cache_put(key: tuple, value: AdminDashboardOut) -> None:
    with _dashboard_lock:
        if len(_dashboard_cache) > 500:
            _dashboard_cache.clear()
        _dashboard_cache[key] = (_time.monotonic() + DASHBOARD_CACHE_SECONDS, value.model_copy(deep=True))


def clear_dashboard_cache() -> None:
    with _dashboard_lock:
        _dashboard_cache.clear()


def _dashboard_sources() -> tuple:
    from app.models import (
        CustomerPaymentAllocation, Delivery, DeliveryItem, PurchaseInvoiceItem, StockMovement,
    )

    return (SalesOrder, SalesOrderItem, Invoice, CustomerPayment, CustomerPaymentAllocation, Customer,
            Supplier, SupplierPayment, PurchaseInvoice, PurchaseInvoiceItem, Expense, Product, ProductVariant,
            WarehouseStock, StockMovement, Delivery, DeliveryItem)


@event.listens_for(Session, "after_flush")
def _clear_dashboard_cache(session: Session, _flush_context) -> None:  # noqa: ANN001
    sources = _dashboard_sources()
    if any(isinstance(obj, sources) for obj in (*session.new, *session.dirty, *session.deleted)):
        clear_dashboard_cache()


@event.listens_for(Session, "do_orm_execute")
def _clear_dashboard_cache_on_bulk_write(state) -> None:  # noqa: ANN001
    # Bulk query.update() / delete() bypass after_flush.
    if (state.is_delete or state.is_update) and state.bind_mapper is not None \
            and state.bind_mapper.class_ in _dashboard_sources():
        clear_dashboard_cache()


def build_delivery_partner_dashboard(
    db: Session,
    org_id: str,
    user_id: str,
) -> DeliveryPartnerDashboardOut:
    total_company_orders = (
        db.query(func.count(SalesOrder.id))
        .filter(SalesOrder.organization_id == org_id)
        .scalar()
        or 0
    )

    my_assigned_deliveries = (
        db.query(func.count(SalesOrder.id))
        .filter(
            SalesOrder.organization_id == org_id,
            SalesOrder.assigned_delivery_partner_id == user_id,
            SalesOrder.fulfilment_status.in_(
                ("planned", "loaded", "in_transit", "partially_delivered")
            ),
        )
        .scalar()
        or 0
    )

    return DeliveryPartnerDashboardOut(
        total_company_orders=total_company_orders,
        my_assigned_deliveries=my_assigned_deliveries,
    )


from app.core.pagination import paginate
from app.schemas.pagination import PaginatedResponse


from app.core.sorting import apply_sorting
from sqlalchemy import func, select

DP_ORDER_SORTABLE_FIELDS = {
    "order_number": SalesOrder.order_number,
    "customer": select(Customer.name).where(Customer.id == SalesOrder.customer_id).scalar_subquery(),
    "customer_name": select(Customer.name).where(Customer.id == SalesOrder.customer_id).scalar_subquery(),
    "order_date": SalesOrder.order_date,
    "sales_officer": select(User.name).where(User.id == func.coalesce(SalesOrder.salesperson_id, SalesOrder.created_by)).scalar_subquery(),
    "total": SalesOrder.total,
    "total_amount": SalesOrder.total,
    "status": SalesOrder.status,
    "order_status": SalesOrder.status,
    "fulfilment_status": SalesOrder.fulfilment_status,
    "delivery_status": SalesOrder.fulfilment_status,
    "delivery_date": SalesOrder.delivery_date,
    "created_at": SalesOrder.created_at,
}


def list_delivery_partner_company_orders(
    db: Session,
    org_id: str,
    page: int = 1,
    page_size: int = 10,
    status_filter: str | None = None,
    fulfilment_status: str | None = None,
    customer_id: str | None = None,
    search: str | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> PaginatedResponse[SalesOrder]:
    query = db.query(SalesOrder).filter(SalesOrder.organization_id == org_id)

    if status_filter:
        mapped = workflow.LEGACY_ORDER_STATUS.get(status_filter)
        if mapped and status_filter not in workflow.ORDER_STATUSES:
            new_status, fulfilment = mapped
            query = query.filter(
                SalesOrder.status == new_status, SalesOrder.fulfilment_status == fulfilment
            )
        else:
            query = query.filter(SalesOrder.status == status_filter)

    if fulfilment_status:
        query = query.filter(SalesOrder.fulfilment_status == fulfilment_status)

    if customer_id:
        query = query.filter(SalesOrder.customer_id == customer_id)

    if search:
        query = query.filter(SalesOrder.order_number.ilike(f"%{search.strip()}%"))

    query = apply_sorting(
        query,
        sort_by=sort_by,
        sort_order=sort_order,
        allowed_fields=DP_ORDER_SORTABLE_FIELDS,
        default_order=[SalesOrder.created_at.desc(), SalesOrder.id.desc()],
        secondary_key=SalesOrder.id.desc(),
    )
    return paginate(query, page=page, page_size=page_size)

