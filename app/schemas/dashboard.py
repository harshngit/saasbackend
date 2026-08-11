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
    branch_id: str | None = None
    warehouse_id: str | None = None
    customer_id: str | None = None
    supplier_id: str | None = None


class AdminDashboardOut(BaseModel):
    filters: DashboardFilters
    summary: DashboardSummary
    orders: DashboardOrders
    cashflow: list[CashflowPoint]
    receivables_payables: ReceivablesPayables
    top_customers: list[TopCustomer]
    top_products: list[TopProduct]
    expense_breakdown: list[ExpenseSlice]
    sales_trend: list[SalesTrendPoint]
    stock_watch: list[StockWatchItem]
    recent_orders: list[RecentOrder]
