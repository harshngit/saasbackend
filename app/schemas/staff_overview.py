"""The Admin → Staff Detail operational summary.

`GET /users/{id}` stays the static employee profile. This is the live half of the
page, and what it contains depends on the employee's role `workspace`: a sales
executive's day looks nothing like a delivery partner's. The frontend switches on
the `workspace` field in the response and renders the matching layout.

Blocks that do not apply to a workspace come back as `null` rather than being
omitted, so the shape is stable and a frontend never has to guard for a missing
key.
"""

from datetime import datetime

from pydantic import BaseModel, Field

# The two workspaces with a purpose-built layout. Anything else gets the generic one.
SALES = "sales"
DELIVERY = "delivery"


class LocationPing(BaseModel):
    """Body of POST /users/me/location — one GPS reading from the field app."""

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_meters: float | None = Field(default=None, ge=0)
    label: str | None = Field(
        default=None, max_length=200,
        description="Optional place name. The device does the reverse geocoding — "
                    "the backend never guesses one.",
    )
    captured_at: datetime | None = Field(
        default=None, description="When the reading was taken. Defaults to now.")


class LocationPingOut(BaseModel):
    user_id: str
    latitude: float
    longitude: float
    accuracy_meters: float | None = None
    label: str | None = None
    updated_at: datetime


class CurrentLocation(BaseModel):
    """Where the employee last reported being.

    `available` is false until the field app has posted at least one reading. The
    employee's `work_location` is the office they are posted to and is never used
    here — it is not a live position.
    """

    available: bool
    latitude: float | None = None
    longitude: float | None = None
    accuracy_meters: float | None = None
    label: str | None = None
    updated_at: datetime | None = None


class AttendanceToday(BaseModel):
    """Today's row from the attendance module, which records four checkpoints."""

    date: str | None = None
    status: str = Field(description="checked_in | checked_out | absent")
    check_in: datetime | None = Field(default=None, description="office_check_in")
    departure: datetime | None = None
    return_to_office: datetime | None = None
    check_out: datetime | None = Field(default=None, description="final_check_out")
    active_duration_minutes: int | None = Field(
        default=None, description="From check-in to check-out, or to now while still in")


class RoleBadge(BaseModel):
    id: str
    name: str
    workspace: str | None = None


class Period(BaseModel):
    date_from: str
    date_to: str


# --------------------------------- sales ---------------------------------


class SalesSummary(BaseModel):
    today_sales: float
    orders_today: int
    period_sales: float
    period_orders: int
    assigned_customers: int
    visits_today: int | None = Field(
        default=None, description="null until a visits module exists")
    pending_followups: int | None = Field(
        default=None, description="null until a follow-ups module exists")


class SalesPerformancePoint(BaseModel):
    date: str
    sales_amount: float
    orders: int


class RecentOrderRow(BaseModel):
    id: str
    order_number: str
    customer_id: str | None = None
    customer_name: str | None = None
    amount: float
    status: str
    date: str


class AssignedCustomerRow(BaseModel):
    id: str
    name: str
    area: str | None = Field(default=None, description="territory, else city")
    city: str | None = None
    phone: str | None = None
    outstanding: float = 0
    last_order_date: str | None = None
    last_visit: str | None = Field(default=None, description="null until a visits module exists")
    next_followup: str | None = Field(
        default=None, description="null until a follow-ups module exists")


# -------------------------------- delivery -------------------------------


class DeliverySummary(BaseModel):
    deliveries_today: int
    completed_today: int
    pending_today: int
    partial_today: int
    failed_today: int
    delivery_value: float = Field(description="Value of today's assigned deliveries")
    amount_collected: float = Field(description="Payments received against their orders")
    amount_receivable: float = Field(description="Delivered but not yet paid")


class DeliveryPerformancePoint(BaseModel):
    date: str
    deliveries_completed: int
    delivery_amount: float


class AssignedDeliveryRow(BaseModel):
    id: str = Field(description="The sales order id — what /deliveries endpoints take")
    order_id: str
    order_number: str
    delivery_number: str | None = Field(
        default=None, description="From the delivery note, when one has been raised")
    customer_id: str | None = None
    customer_name: str | None = None
    scheduled_at: datetime | None = None
    payment_type: str = Field(
        description="prepaid when the order's invoices are settled, else cod")
    amount_due: float
    status: str


class DeliveryBreakdown(BaseModel):
    successful: int
    pending: int
    partial: int
    failed: int
    amount_collected: float
    pod_completed: int | None = Field(
        default=None, description="null — proof of delivery is not captured yet")


# -------------------------------- generic --------------------------------


class GenericSummary(BaseModel):
    """For any workspace without a purpose-built layout (accounts, HR, …)."""

    orders_created_period: int
    sales_amount_period: float
    assigned_customers: int
    days_present_period: int


class StaffActivity(BaseModel):
    """One line of the employee's activity feed. Which of the optional fields are
    filled depends on `type`."""

    type: str = Field(
        description="order_created | payment_received | delivery_completed | "
                    "delivery_failed | delivery_partial | attendance_check_in | "
                    "attendance_check_out")
    at: datetime
    customer_id: str | None = None
    customer_name: str | None = None
    order_id: str | None = None
    order_number: str | None = None
    delivery_id: str | None = None
    delivery_number: str | None = None
    amount: float | None = None
    status: str | None = None
    pod_status: str | None = Field(
        default=None, description="null — proof of delivery is not captured yet")


class StaffOverviewOut(BaseModel):
    user_id: str
    employee_id: str | None = None
    name: str
    workspace: str | None = Field(
        default=None, description="From the role. Switch the page layout on this, not on the role name")
    role: RoleBadge | None = None
    period: Period

    attendance: AttendanceToday
    current_location: CurrentLocation

    summary: SalesSummary | DeliverySummary | GenericSummary
    performance: list[SalesPerformancePoint] | list[DeliveryPerformancePoint]
    recent_activity: list[StaffActivity]

    # Sales workspace only; null otherwise.
    recent_orders: list[RecentOrderRow] | None = None
    assigned_customers: list[AssignedCustomerRow] | None = None

    # Delivery workspace only; null otherwise.
    vehicle: "VehicleBadge | None" = None
    assigned_deliveries: list[AssignedDeliveryRow] | None = None
    delivery_summary: DeliveryBreakdown | None = None


class VehicleBadge(BaseModel):
    """The delivery partner's open vehicle loading for the day.

    There is no vehicle master yet, so `vehicle_number` is null — a loading records
    who is out with stock, not which van. `id` is the loading's id.
    """

    id: str
    vehicle_number: str | None = None
    loaded_at: datetime | None = None
    items: int = 0


StaffOverviewOut.model_rebuild()
