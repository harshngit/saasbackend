"""The Admin Dashboard summary — every widget on the page in one response.

Summarised figures only. The detail screens behind "View Sales Report",
"View All Orders" and friends keep using the existing report / list endpoints;
nothing here is meant to be paginated or drilled into.
"""

from pydantic import BaseModel, Field


class DashboardSummary(BaseModel):
    """The top stat cards."""

    today_sales: float
    month_sales: float
    period_sales: float = Field(description="Sales inside the requested date range")
    purchases: float
    expenses: float
    gross_profit: float = Field(description="Sales − cost of the goods sold in the period")
    net_profit: float = Field(description="Gross profit − expenses")
    new_customers: int
    sales_growth_percentage: float = Field(
        description="Period sales vs. the previous period of the same length; "
                    "null-safe (0 when there is nothing to compare against)"
    )


class DashboardOrders(BaseModel):
    total: int
    pending: int = Field(description="Awaiting approval")
    to_deliver: int = Field(description="Confirmed / processing / out for delivery")
    delivered: int
    cancelled: int


class DeliveryPartnerDashboardOut(BaseModel):
    total_company_orders: int
    my_assigned_deliveries: int


class CashflowPoint(BaseModel):
    date: str
    inflow: float
    outflow: float


class ReceivablesPayables(BaseModel):
    receivables: float
    payables: float
    overdue_receivables: float
    overdue_payables: float
    overdue_after_days: int = Field(
        description="How old an unpaid bill must be to count as overdue. Taken from "
                    "the customer's payment_terms (net_30 → 30) where set, else this default"
    )


class TopCustomer(BaseModel):
    customer_id: str
    customer_name: str
    sales: float
    orders: int


class TopProduct(BaseModel):
    product_id: str | None = None
    product_name: str
    sales_amount: float
    quantity: int


class ExpenseSlice(BaseModel):
    category: str
    amount: float


class SalesTrendPoint(BaseModel):
    date: str
    sales: float
    orders: int


class StockWatchItem(BaseModel):
    product_id: str
    product_name: str
    stock: int
    minimum_stock_level: int | None = None
    stock_percentage: int = Field(description="Stock as a % of the minimum level; 0 when out")
    status: str = Field(description="out_of_stock | low_stock | healthy")
    warehouse_id: str | None = Field(
        default=None,
        description="With a warehouse filter: the selected warehouse whose stock is shown (the worst "
                    "one when several are selected). Null without a warehouse filter.",
    )


class RecentOrder(BaseModel):
    id: str
    order_number: str
    customer_name: str | None = None
    status: str
    payment_status: str = Field(description="paid | partial | unpaid, from the order's invoices")
    total: float
    date: str


class DashboardFilters(BaseModel):
    """Echo of what the numbers were computed over, so the page can label itself."""

    date_from: str
    date_to: str
    company_id: str | None = Field(default=None, description="The selected ID; several are comma-joined (sorted)")
    warehouse_id: str | None = Field(default=None, description="The selected ID; several are comma-joined (sorted)")
    customer_id: str | None = Field(default=None, description="The selected ID; several are comma-joined (sorted)")
    supplier_id: str | None = Field(default=None, description="The selected ID; several are comma-joined (sorted)")
    company_ids: list[str] = Field(default_factory=list, description="Selected company IDs (empty = no filter)")
    warehouse_ids: list[str] = Field(default_factory=list, description="Selected warehouse IDs (empty = no filter)")
    customer_ids: list[str] = Field(default_factory=list, description="Selected customer IDs (empty = no filter)")
    supplier_ids: list[str] = Field(default_factory=list, description="Selected supplier IDs (empty = no filter)")


class DashboardToday(BaseModel):
    """Today in the organization's timezone, whatever date range was asked for."""

    date: str = Field(description="Today's date (YYYY-MM-DD) in the organization's timezone")
    timezone: str = Field(description="The organization's timezone; every date and day boundary in the "
                                      "dashboard uses it")
    orders: int = Field(description="Orders placed today (placed / processing / completed)")
    sales: float = Field(description="Their total amount (same as summary.today_sales)")
    delivered: int = Field(description="Deliveries confirmed as delivered today")
    delivered_orders: int = Field(description="Of today's orders, those fully delivered (completed)")
    pending_orders: int = Field(description="Of today's orders, the rest (orders - delivered_orders)")
    delivered_amount: float = Field(description="Total amount of delivered_orders")
    pending_amount: float = Field(description="Total amount of pending_orders (sales - delivered_amount)")


class DashboardTodayOverall(DashboardToday):
    """Today for the WHOLE organization: ignores every filter (date range, company,
    warehouse, customer, supplier). For the "Today's Sales" card. `today` above, by
    contrast, follows the customer filter."""

    received: float = Field(description="Customer payments received today (all customers)")


class AdminDashboardOut(BaseModel):
    filters: DashboardFilters
    summary: DashboardSummary
    today: DashboardToday
    today_overall: DashboardTodayOverall
    orders: DashboardOrders
    cashflow: list[CashflowPoint]
    receivables_payables: ReceivablesPayables
    top_customers: list[TopCustomer]
    top_products: list[TopProduct]
    expense_breakdown: list[ExpenseSlice]
    sales_trend: list[SalesTrendPoint]
    stock_watch: list[StockWatchItem]
    recent_orders: list[RecentOrder]


# ---------------------------------------------------------------------------------------
# Mobile "my dashboard" responses -- GET /dashboard/me/sales and /dashboard/me/delivery.
# Only what the field app shows, always for the logged-in employee. Every figure has the
# same definition as the matching block of GET /users/{id}/overview (Staff Detail).
# ---------------------------------------------------------------------------------------

from app.schemas.staff_overview import (  # noqa: E402
    AssignedDeliveryRow,
    DeliveryPerformancePoint,
    DeliverySummary,
    PeriodName,
    RecentOrderRow,
    SalesPerformancePoint,
    VehicleBadge,
)


class MySalesSummary(BaseModel):
    sales_amount: float = Field(description="Sales in the period (orders past approval, by creation date)")
    orders: int = Field(description="Number of those orders")
    today_sales_amount: float = Field(description="Same definition, for today (organization's timezone)")
    assigned_customers: int = Field(description="Customers whose assigned sales officer is this employee")


class MySalesDashboardOut(BaseModel):
    period: PeriodName
    period_from: str
    period_to: str
    summary: MySalesSummary
    performance: list[SalesPerformancePoint] = Field(description="One point per day of the period")
    recent_orders: list[RecentOrderRow] = Field(description="Newest first, at most 5")


class MyDeliveryDashboardOut(BaseModel):
    period: PeriodName
    period_from: str
    period_to: str
    summary: DeliverySummary
    performance: list[DeliveryPerformancePoint] = Field(description="Completed deliveries per day")
    open_deliveries_count: int = Field(description="All deliveries still open for this employee")
    open_deliveries: list[AssignedDeliveryRow] = Field(description="Newest first, at most 5")
    vehicle: VehicleBadge | None = None
