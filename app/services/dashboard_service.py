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
from sqlalchemy import and_, case, func, or_
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core import business_time, workflow
from app.models import (
    Customer,
    CustomerPayment,
    Delivery,
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
    DashboardToday,
    DashboardTodayOverall,
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


def _day(value: str | None, fallback: date, end: bool = False, tz=None) -> tuple[date, datetime]:  # noqa: ANN001
    """Parse a YYYY-MM-DD filter (a calendar day in the organization's timezone `tz`)
    into its date and the UTC instant to compare stored timestamps against."""
    if value is None:
        parsed = fallback
    else:
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Dates must be YYYY-MM-DD"
            )
    tz = tz or timezone.utc
    moment = business_time.day_end(parsed, tz) if end else business_time.day_start(parsed, tz)
    return parsed, moment


def _resolve_range(date_from: str | None, date_to: str | None, tz=None) -> tuple[date, date, datetime, datetime]:  # noqa: ANN001
    """The window to report on — the current month to date when nothing is asked for.
    Days are the organization's (`tz`), both ends inclusive."""
    tz = tz or timezone.utc
    today = business_time.today(tz)
    start, df = _day(date_from, today.replace(day=1), tz=tz)
    end, dt = _day(date_to, today, end=True, tz=tz)
    if start > end:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="date_from cannot be after date_to"
        )
    return start, end, df, dt


def _sale_orders(db: Session, org_id: str, customer_ids: tuple[str, ...], company_id: str | None = None):
    eff_org = company_id if company_id else org_id
    query = db.query(SalesOrder).filter(
        SalesOrder.organization_id == eff_org, SalesOrder.status.in_(SALE_STATUSES)
    )
    if customer_ids:
        query = query.filter(SalesOrder.customer_id.in_(customer_ids))
    return query


def _purchase_invoices(
    db: Session, org_id: str, supplier_ids: tuple[str, ...], warehouse_ids: tuple[str, ...], company_id: str | None = None
):
    eff_org = company_id if company_id else org_id
    query = db.query(PurchaseInvoice).filter(
        PurchaseInvoice.organization_id == eff_org, PurchaseInvoice.status == "approved"
    )
    if supplier_ids:
        query = query.filter(PurchaseInvoice.supplier_id.in_(supplier_ids))
    if warehouse_ids:
        query = query.filter(PurchaseInvoice.warehouse_id.in_(warehouse_ids))
    return query


def _sales_total(
    db: Session, org_id: str, customer_ids: tuple[str, ...], company_id: str | None, df: datetime, dt: datetime
) -> float:
    # Only the totals are read -- not whole orders with their customers and lines.
    orders = _sale_orders(db, org_id, customer_ids, company_id).filter(
        SalesOrder.created_at >= df, SalesOrder.created_at <= dt
    )
    return round(sum(total or 0 for (total,) in orders.with_entities(SalesOrder.total)), 2)


def _sales_totals(
    db: Session, org_id: str, customer_ids: tuple[str, ...], company_id: str | None,
    windows: list[tuple[datetime, datetime]],
) -> list[float]:
    """_sales_total for several windows in one query: one conditional SUM per window."""
    sums = [
        func.coalesce(func.sum(case((and_(SalesOrder.created_at >= df, SalesOrder.created_at <= dt),
                                     SalesOrder.total), else_=0)), 0)
        for df, dt in windows
    ]
    row = (
        _sale_orders(db, org_id, customer_ids, company_id)
        .filter(SalesOrder.created_at >= min(w[0] for w in windows),
                SalesOrder.created_at <= max(w[1] for w in windows))
        .with_entities(*sums)
        .one()
    )
    return [round(float(v or 0), 2) for v in row]


def _summary(
    db: Session, org_id: str, start: date, end: date, df: datetime, dt: datetime,
    period_sales: float, purchases: float, expenses: float, customer_ids: tuple[str, ...],
    company_id: str | None = None, tz=None, totals: tuple[float, float, float] | None = None,  # noqa: ANN001
) -> DashboardSummary:
    eff_org = company_id if company_id else org_id
    tz = tz or timezone.utc
    if totals is None:
        w = _windows(start, end, tz)
        totals = tuple(_sales_totals(db, org_id, customer_ids, company_id, [w["previous"], w["today"], w["month"]]))
    previous, today_sales, month_sales = totals

    # Growth compares with the previous window of the same length (see _windows).
    growth = round((period_sales - previous) / previous * 100, 2) if previous else 0.0

    cust_q = db.query(Customer).filter(
        Customer.organization_id == eff_org,
        Customer.created_at >= df,
        Customer.created_at <= dt,
    )
    if customer_ids:
        cust_q = cust_q.filter(Customer.id.in_(customer_ids))
    new_customers = cust_q.count()

    gross_profit = round(period_sales - purchases, 2)
    return DashboardSummary(
        today_sales=today_sales,
        month_sales=month_sales,
        period_sales=period_sales,
        purchases=purchases,
        expenses=expenses,
        gross_profit=gross_profit,
        net_profit=round(gross_profit - expenses, 2),
        new_customers=new_customers,
        sales_growth_percentage=growth,
    )


def _windows(start: date, end: date, tz) -> dict:  # noqa: ANN001
    """The org-local windows the summary compares: the previous period of the same length
    as start..end, today, and this month to date (each as a UTC instant pair)."""
    today = business_time.today(tz)
    span = (end - start).days + 1
    prev_start, prev_end = start - timedelta(days=span), start - timedelta(days=1)
    return {
        "previous": (business_time.day_start(prev_start, tz), business_time.day_end(prev_end, tz)),
        "today": (business_time.day_start(today, tz), business_time.day_end(today, tz)),
        "month": (business_time.day_start(today.replace(day=1), tz), business_time.day_end(today, tz)),
    }


def _orders(
    db: Session, org_id: str, customer_ids: tuple[str, ...], company_id: str | None, df: datetime, dt: datetime,
    rows: list | None = None,
) -> DashboardOrders:
    """Order counts for the window, every status. `rows` ((status, fulfilment_status) for
    the same window) saves the query when the caller already has them."""
    if rows is None:
        eff_org = company_id if company_id else org_id
        query = db.query(SalesOrder).filter(
            SalesOrder.organization_id == eff_org,
            SalesOrder.created_at >= df,
            SalesOrder.created_at <= dt,
        )
        if customer_ids:
            query = query.filter(SalesOrder.customer_id.in_(customer_ids))
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
    customer_ids: tuple[str, ...], supplier_ids: tuple[str, ...], company_id: str | None = None,
    expense_rows: list | None = None, tz=None,  # noqa: ANN001
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
    if customer_ids:
        payments = payments.filter(CustomerPayment.customer_id.in_(customer_ids))
    for received_on, amount in payments.with_entities(CustomerPayment.received_on, CustomerPayment.amount):
        inflow[business_time.local_date(received_on, tz or timezone.utc).isoformat()] += amount or 0

    paid = db.query(SupplierPayment).filter(
        SupplierPayment.organization_id == eff_org,
        SupplierPayment.paid_on >= df,
        SupplierPayment.paid_on <= dt,
    )
    if supplier_ids:
        paid = paid.filter(SupplierPayment.supplier_id.in_(supplier_ids))
    for paid_on, amount in paid.with_entities(SupplierPayment.paid_on, SupplierPayment.amount):
        outflow[business_time.local_date(paid_on, tz or timezone.utc).isoformat()] += amount or 0

    if not supplier_ids and not customer_ids:
        for expense in expense_rows if expense_rows is not None else _approved_expenses(db, eff_org, df, dt):
            outflow[business_time.local_date(expense.expense_date, tz or timezone.utc).isoformat()] += expense.amount or 0

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
    db: Session, org_id: str, customer_ids: tuple[str, ...], supplier_ids: tuple[str, ...],
    warehouse_ids: tuple[str, ...] = (), company_id: str | None = None,
) -> ReceivablesPayables:
    """Balances are a position, not a period — they are always "as of now", so the
    date filter does not apply to them. Only the columns the figures need are read."""
    eff_org = company_id if company_id else org_id
    customers = db.query(Customer).filter(Customer.organization_id == eff_org)
    if customer_ids:
        customers = customers.filter(Customer.id.in_(customer_ids))
    customers = customers.with_entities(Customer.id, Customer.outstanding_balance, Customer.payment_terms).all()
    receivables = round(sum(max(c.outstanding_balance or 0, 0) for c in customers), 2)

    suppliers = db.query(Supplier).filter(Supplier.organization_id == eff_org)
    if supplier_ids:
        suppliers = suppliers.filter(Supplier.id.in_(supplier_ids))
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
    if customer_ids:
        invoices = invoices.filter(Invoice.customer_id.in_(customer_ids))
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
    if supplier_ids:
        purchases = purchases.filter(PurchaseInvoice.supplier_id.in_(supplier_ids))
    if warehouse_ids:
        purchases = purchases.filter(PurchaseInvoice.warehouse_id.in_(warehouse_ids))
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
    company_id: str | None = None, supplier_ids: tuple[str, ...] = (), customer_ids: tuple[str, ...] = (),
    expense_rows: list | None = None,
) -> list[ExpenseSlice]:
    if supplier_ids or customer_ids:
        return []
    eff_org = company_id if company_id else org_id
    totals: dict[str, float] = defaultdict(float)
    for expense in expense_rows if expense_rows is not None else _approved_expenses(db, eff_org, df, dt):
        totals[expense.category] += expense.amount or 0
    return [
        ExpenseSlice(category=category, amount=round(amount, 2))
        for category, amount in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    ]


def _sales_trend(orders: list, start: date, end: date, tz=None) -> list[SalesTrendPoint]:  # noqa: ANN001
    sales: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    for order in orders:
        key = business_time.local_date(order.created_at, tz or timezone.utc).isoformat()
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
    db: Session, org_id: str, warehouse_ids: tuple[str, ...] = (), company_id: str | None = None
) -> list[StockWatchItem]:
    """Products out of stock or at/below their minimum level.

    With a warehouse filter each selected warehouse is judged on its own stock (D1): a
    product is flagged when it is low in ANY selected warehouse -- stock is never added
    up across warehouses first, which would hide a warehouse that has run low. The item
    shows that warehouse's figures (the worst one when several qualify). Without a
    warehouse filter nothing changes: the product's overall stock is used."""
    eff_org = company_id if company_id else org_id
    watch: list[StockWatchItem] = []

    # (warehouse, product) -> on hand, variants of a product summed within a warehouse.
    wh_stock_map: dict[tuple[str, str], float] = {}
    if warehouse_ids:
        for wh_id, product_id, on_hand in db.query(
            WarehouseStock.warehouse_id, WarehouseStock.product_id, WarehouseStock.on_hand_quantity
        ).filter(
            WarehouseStock.organization_id == eff_org,
            WarehouseStock.warehouse_id.in_(warehouse_ids),
        ):
            key = (wh_id, product_id)
            wh_stock_map[key] = wh_stock_map.get(key, 0.0) + (on_hand or 0.0)

    def _flag(stock: int, minimum: int | None) -> str | None:
        if stock <= 0:
            return "out_of_stock"
        if minimum and stock <= minimum:
            return "low_stock"
        return None

    def _pct(stock: int, minimum: int | None) -> int:
        # Against the minimum level, so 100% means "just at the reorder line".
        return round(stock / minimum * 100) if minimum else 0

    # Columns only -- a Product row eagerly joins seven relations. Product.total_stock is
    # the variants' inventory when the product has variants, else total_inventory.
    variant_stock: dict[str, int] = {}
    if not warehouse_ids:
        for product_id, inventory in (
            db.query(ProductVariant.product_id, ProductVariant.inventory)
            .join(Product, Product.id == ProductVariant.product_id)
            .filter(Product.organization_id == eff_org)
        ):
            variant_stock[product_id] = variant_stock.get(product_id, 0) + (inventory or 0)
    for product in db.query(Product).filter(Product.organization_id == eff_org).with_entities(
        Product.id, Product.name, Product.total_inventory, Product.minimum_stock_level
    ):
        minimum = product.minimum_stock_level
        if warehouse_ids:
            # Every selected warehouse separately; keep the worst one that is flagged.
            flagged = []
            for wh_id in warehouse_ids:
                stock = int(wh_stock_map.get((wh_id, product.id), 0))
                status_ = _flag(stock, minimum)
                if status_:
                    flagged.append((status_ != "out_of_stock", _pct(stock, minimum), wh_id, stock, status_))
            if not flagged:
                continue
            _, _, warehouse_id, stock, status_ = min(flagged)
        else:
            warehouse_id = None
            stock = variant_stock[product.id] if product.id in variant_stock else (product.total_inventory or 0)
            status_ = _flag(stock, minimum)
            if not status_:
                continue
        watch.append(
            StockWatchItem(
                product_id=product.id,
                product_name=product.name,
                stock=stock,
                minimum_stock_level=minimum,
                stock_percentage=_pct(stock, minimum),
                status=status_,
                warehouse_id=warehouse_id,
            )
        )
    watch.sort(key=lambda item: (item.status != "out_of_stock", item.stock_percentage))
    return watch[:STOCK_WATCH_N]


def _recent_orders(
    db: Session, org_id: str, customer_ids: tuple[str, ...], company_id: str | None = None, tz=None,  # noqa: ANN001
) -> list[RecentOrder]:
    eff_org = company_id if company_id else org_id
    query = db.query(SalesOrder).filter(SalesOrder.organization_id == eff_org)
    if customer_ids:
        query = query.filter(SalesOrder.customer_id.in_(customer_ids))
    # Each order's (non-credit-note) invoice totals come in the same query.
    billed = (
        db.query(Invoice.order_id.label("order_id"),
                 func.count(Invoice.id).label("invoices"),
                 func.coalesce(func.sum(func.coalesce(Invoice.total, 0)), 0).label("billed"),
                 func.coalesce(func.sum(func.coalesce(Invoice.amount_paid, 0)), 0).label("paid"))
        .filter(Invoice.organization_id == eff_org, Invoice.is_credit_note.is_(False))
        .group_by(Invoice.order_id)
        .subquery()
    )
    orders = (
        query.outerjoin(Customer, Customer.id == SalesOrder.customer_id)
        .outerjoin(billed, billed.c.order_id == SalesOrder.id)
        .order_by(SalesOrder.created_at.desc())
        .limit(RECENT_ORDERS)
        .with_entities(SalesOrder.id, SalesOrder.order_number, SalesOrder.status, SalesOrder.total,
                       SalesOrder.order_date, SalesOrder.created_at, Customer.id.label("customer_row_id"),
                       Customer.business_name.label("customer_business_name"), Customer.name.label("customer_name"),
                       billed.c.invoices, billed.c.billed, billed.c.paid)
        .all()
    )
    if not orders:
        return []

    recent = []
    for order in orders:
        has_invoices = bool(order.invoices)
        total = float(order.billed or 0)
        paid = float(order.paid or 0)
        if not has_invoices or paid <= 0:
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
                date=business_time.local_date(order.order_date or order.created_at, tz or timezone.utc).isoformat(),
            )
        )
    return recent


def _today_figures(db: Session, org_id: str, tz, today, today_from: datetime, today_to: datetime) -> DashboardToday:  # noqa: ANN001
    """`today` for the whole organization (no customer filter) -- the same definitions."""
    fully_delivered = or_(SalesOrder.status == "completed", SalesOrder.fulfilment_status == "delivered")
    orders, delivered_orders, amount, delivered_amount = (
        _sale_orders(db, org_id, ())
        .filter(SalesOrder.created_at >= today_from, SalesOrder.created_at <= today_to)
        .with_entities(
            func.count(SalesOrder.id),
            func.coalesce(func.sum(case((fully_delivered, 1), else_=0)), 0),
            func.coalesce(func.sum(SalesOrder.total), 0),
            func.coalesce(func.sum(case((fully_delivered, SalesOrder.total), else_=0)), 0),
        )
        .one()
    )
    amount = round(float(amount or 0), 2)
    delivered_amount = round(float(delivered_amount or 0), 2)
    delivered = db.query(Delivery).filter(
        Delivery.organization_id == org_id, Delivery.status == "delivered",
        Delivery.confirmed_at >= today_from, Delivery.confirmed_at <= today_to,
    ).count()
    return DashboardToday(
        date=today.isoformat(), timezone=str(tz), orders=int(orders or 0), sales=amount, delivered=delivered,
        delivered_orders=int(delivered_orders or 0), pending_orders=int(orders or 0) - int(delivered_orders or 0),
        delivered_amount=delivered_amount, pending_amount=round(amount - delivered_amount, 2),
    )


def build_admin_dashboard(
    db: Session,
    org_id: str,
    date_from: str | None = None,
    date_to: str | None = None,
    company_ids: tuple[str, ...] = (),
    warehouse_ids: tuple[str, ...] = (),
    customer_ids: tuple[str, ...] = (),
    supplier_ids: tuple[str, ...] = (),
) -> AdminDashboardOut:
    """The filters arrive normalized (app.core.id_filters.normalize_ids): sorted, distinct
    tuples, empty = no filter, already checked to belong to the firm. Each narrows the
    figures it always narrowed, with IN instead of =; no figure gains a new filter."""
    # Every company id equals the caller's own organization (checked by the router).
    company_id = company_ids[0] if company_ids else None
    eff_org = company_id if company_id else org_id
    tz = business_time.org_timezone(db, eff_org)
    start, end, df, dt = _resolve_range(date_from, date_to, tz)
    today = business_time.today(tz)
    # Sorted tuples: A+B and B+A share one entry, A / B / A+B never do.
    cache_key = (org_id, str(tz), start, end, today, company_ids, warehouse_ids, customer_ids, supplier_ids)
    cached = _dashboard_cache_get(cache_key)
    if cached is not None:
        return cached

    # The period's orders as plain rows (id, customer, total, date, customer name) --
    # everything the summary, top customers, top products and trend need.
    # The window's orders of every status, in one query: the status counts use all of
    # them, the sales figures only the sale statuses.
    window = db.query(SalesOrder).filter(
        SalesOrder.organization_id == eff_org, SalesOrder.created_at >= df, SalesOrder.created_at <= dt)
    if customer_ids:
        window = window.filter(SalesOrder.customer_id.in_(customer_ids))
    window_rows = (
        window.outerjoin(Customer, Customer.id == SalesOrder.customer_id)
        .order_by(SalesOrder.created_at)
        .with_entities(SalesOrder.id, SalesOrder.customer_id, SalesOrder.total, SalesOrder.created_at,
                       SalesOrder.status, SalesOrder.fulfilment_status,
                       Customer.business_name.label("customer_business_name"), Customer.name.label("customer_name"))
        .all()
    )
    sale_statuses = set(SALE_STATUSES)
    orders = [o for o in window_rows if o.status in sale_statuses]
    period_sales = round(sum(o.total or 0 for o in orders), 2)
    purchases = round(
        sum(
            total or 0
            for (total,) in _purchase_invoices(db, eff_org, supplier_ids, warehouse_ids, company_id).filter(
                PurchaseInvoice.invoice_date >= df, PurchaseInvoice.invoice_date <= dt
            ).with_entities(PurchaseInvoice.total)
        ),
        2,
    )
    expense_rows = [] if (supplier_ids or customer_ids) else _approved_expenses(db, eff_org, df, dt)
    expenses = 0.0 if (supplier_ids or customer_ids) else round(sum(e.amount or 0 for e in expense_rows), 2)

    windows = _windows(start, end, tz)
    totals = tuple(_sales_totals(db, eff_org, customer_ids, company_id,
                                 [windows["previous"], windows["today"], windows["month"]]))
    today_from, today_to = windows["today"]
    # Today's placed orders split into fully delivered (completed) and the rest, in one query.
    fully_delivered = or_(SalesOrder.status == "completed", SalesOrder.fulfilment_status == "delivered")
    today_orders, delivered_orders, today_amount, delivered_amount = (
        _sale_orders(db, eff_org, customer_ids, company_id)
        .filter(SalesOrder.created_at >= today_from, SalesOrder.created_at <= today_to)
        .with_entities(
            func.count(SalesOrder.id),
            func.coalesce(func.sum(case((fully_delivered, 1), else_=0)), 0),
            func.coalesce(func.sum(SalesOrder.total), 0),
            func.coalesce(func.sum(case((fully_delivered, SalesOrder.total), else_=0)), 0),
        )
        .one()
    )
    delivered_amount = round(float(delivered_amount or 0), 2)
    delivered_q = db.query(Delivery).filter(
        Delivery.organization_id == eff_org, Delivery.status == "delivered",
        Delivery.confirmed_at >= today_from, Delivery.confirmed_at <= today_to,
    )
    if customer_ids:
        delivered_q = delivered_q.filter(Delivery.customer_id.in_(customer_ids))

    today_block = DashboardToday(
        date=today.isoformat(), timezone=str(tz), orders=today_orders, sales=totals[1],
        delivered=delivered_q.count(),
        delivered_orders=int(delivered_orders or 0), pending_orders=today_orders - int(delivered_orders or 0),
        delivered_amount=delivered_amount,
        pending_amount=round(float(today_amount or 0) - delivered_amount, 2),
    )
    # The same figures with no filter at all: only the customer filter ever narrowed
    # `today` (the window is always today; company = own org), so without one they are
    # identical and are reused instead of queried again.
    overall = today_block if not customer_ids else _today_figures(db, org_id, tz, today, today_from, today_to)
    received_today = (
        db.query(func.coalesce(func.sum(CustomerPayment.amount), 0))
        .filter(CustomerPayment.organization_id == org_id,
                CustomerPayment.received_on >= today_from, CustomerPayment.received_on <= today_to)
        .scalar()
    )

    result = AdminDashboardOut(
        today_overall=DashboardTodayOverall(**overall.model_dump(), received=round(float(received_today or 0), 2)),
        filters=DashboardFilters(
            date_from=start.isoformat(),
            date_to=end.isoformat(),
            # The original single-value fields: one ID as before, several comma-joined
            # (sorted), none -> null. The *_ids lists carry the same IDs as arrays.
            company_id=",".join(company_ids) or None,
            warehouse_id=",".join(warehouse_ids) or None,
            customer_id=",".join(customer_ids) or None,
            supplier_id=",".join(supplier_ids) or None,
            company_ids=list(company_ids),
            warehouse_ids=list(warehouse_ids),
            customer_ids=list(customer_ids),
            supplier_ids=list(supplier_ids),
        ),
        summary=_summary(db, eff_org, start, end, df, dt, period_sales, purchases, expenses, customer_ids, company_id,
                         tz=tz, totals=totals),
        today=today_block,
        orders=_orders(db, eff_org, customer_ids, company_id, df, dt,
                       rows=[(o.status, o.fulfilment_status) for o in window_rows]),
        cashflow=_cashflow(db, eff_org, start, end, df, dt, customer_ids, supplier_ids, company_id,
                           expense_rows=expense_rows, tz=tz),
        receivables_payables=_receivables_payables(db, eff_org, customer_ids, supplier_ids, warehouse_ids, company_id),
        top_customers=_top_customers(orders),
        top_products=_top_products(db, eff_org, [o.id for o in orders]),
        expense_breakdown=_expense_breakdown(db, eff_org, df, dt, company_id, supplier_ids, customer_ids,
                                             expense_rows=expense_rows),
        sales_trend=_sales_trend(orders, start, end, tz),
        stock_watch=_stock_watch(db, eff_org, warehouse_ids, company_id),
        recent_orders=_recent_orders(db, eff_org, customer_ids, company_id, tz),
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


def _not_business_data() -> tuple:
    """Writes that never change a dashboard figure. Every other write -- orders, invoices,
    payments and receipts, deliveries and collections, returns, purchases and GRNs,
    supplier payments, expenses, stock, customers, suppliers, products, and anything
    added later -- clears the cache, so nothing new can be missed."""
    from app.models import ActivityLog, Notification, PasswordResetToken, PushDevice, RefreshToken

    return (Notification, ActivityLog, PushDevice, RefreshToken, PasswordResetToken, User)


def _is_business_write(obj) -> bool:  # noqa: ANN001
    return not isinstance(obj, _not_business_data())


@event.listens_for(Session, "after_flush")
def _clear_dashboard_cache(session: Session, _flush_context) -> None:  # noqa: ANN001
    if any(_is_business_write(obj) for obj in (*session.new, *session.dirty, *session.deleted)):
        session.info["crm_dashboard_changed"] = True
        clear_dashboard_cache()


@event.listens_for(Session, "do_orm_execute")
def _clear_dashboard_cache_on_bulk_write(state) -> None:  # noqa: ANN001
    # Bulk query.update() / delete() bypass after_flush.
    if (state.is_delete or state.is_update) and (
            state.bind_mapper is None or not issubclass(state.bind_mapper.class_, _not_business_data())):
        state.session.info["crm_dashboard_changed"] = True
        clear_dashboard_cache()


@event.listens_for(Session, "after_commit")
def _clear_dashboard_cache_after_commit(session: Session) -> None:
    # Again once committed: a request between this session's flush and its commit may
    # have rebuilt the dashboard from the old figures.
    if session.info.pop("crm_dashboard_changed", False):
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

