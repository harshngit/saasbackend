"""The Admin → Staff Detail operational summary.

`GET /users/{id}` stays the static employee profile. This is the live half of the
page, and what it contains depends on the employee's role `workspace`: a sales
executive's day looks nothing like a delivery partner's. The frontend switches on
the `workspace` field in the response and renders the matching layout.

Blocks that do not apply to a workspace come back as `null` rather than being
omitted, so the shape is stable and a frontend never has to guard for a missing
key. Figures a module for does not exist yet — visits, follow-ups — come back `null`
rather than as a made-up zero.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# The two workspaces with a purpose-built layout. Anything else gets the generic one.
SALES = "sales"
DELIVERY = "delivery"

# What `?period=` accepts. Anything else is rejected rather than silently widened.
PERIODS = ("today", "week", "month")
PeriodName = Literal["today", "week", "month", "custom"]


class LocationPing(BaseModel):
    """Body of POST /users/me/location — one GPS reading from the field app."""

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_meters: float | None = Field(default=None, ge=0)
    label: str | None = Field(
        default=None, max_length=200,
        description="Optional human label the app resolved, e.g. a street name")
    captured_at: datetime | None = Field(
        default=None, description="When the reading was taken. Defaults to now")


class LocationPingOut(BaseModel):
    user_id: str
    latitude: float
    longitude: float
    accuracy_meters: float | None = None
    label: str | None = None
    updated_at: datetime


class CurrentLocation(BaseModel):
    """Where the employee actually is, from real GPS only.

    `available` is false and every field null until the field app has posted a ping.
    The employee's `work_location` is a posting on their profile, not a live position,
    and is deliberately never reported here.
    """

    available: bool
    latitude: float | None = None
    longitude: float | None = None
    accuracy_meters: float | None = None
    label: str | None = None
    updated_at: datetime | None = None


class AttendanceToday(BaseModel):
    """Today's attendance for this employee."""

    date: str | None = None
    status: str = Field(description="checked_in | checked_out | absent")
    check_in: datetime | None = Field(default=None, description="office_check_in")
    departure: datetime | None = None
    return_to_office: datetime | None = None
    check_out: datetime | None = Field(default=None, description="final_check_out")
    active_duration_minutes: int | None = Field(
        default=None, description="Minutes between check-in and check-out (or now)")


class RoleBadge(BaseModel):
    id: str
    name: str
    workspace: str | None = None
    data_scope: str | None = None


# ------------------------------- references -------------------------------
# Every row that points at another record does it the same way: a small object with
# the id and the one label the page shows, so the frontend never stitches
# `*_id` and `*_name` fields back together itself.


class CustomerRef(BaseModel):
    id: str
    name: str | None = None


class OrderRef(BaseModel):
    id: str
    order_number: str | None = None
    amount: float | None = None


class DeliveryRef(BaseModel):
    id: str
    delivery_number: str | None = None


# --------------------------------- sales ---------------------------------


class SalesSummary(BaseModel):
    """The sales cards, over the requested period."""

    sales_amount: float
    orders: int
    assigned_customers: int
    visits: int | None = Field(
        default=None, description="null until a visits module exists — not a zero")
    pending_followups: int | None = Field(
        default=None, description="null until a follow-ups module exists — not a zero")


class SalesPerformancePoint(BaseModel):
    date: str
    sales_amount: float
    orders: int


class RecentOrderRow(BaseModel):
    id: str
    order_number: str
    customer: CustomerRef | None = None
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
    """The delivery cards, over the requested period, from the employee's own
    assigned deliveries."""

    deliveries: int
    completed: int
    pending: int
    partial: int
    failed: int
    delivery_value: float = Field(description="Value of the orders behind those deliveries")
    amount_collected: float = Field(description="Payments received against those orders")
    amount_receivable: float = Field(description="Delivered but not yet paid")
    pod_completed: int = Field(
        default=0, description="Of the completed ones, how many have a POD photo or signature")


class DeliveryPerformancePoint(BaseModel):
    date: str
    deliveries_completed: int
    delivery_amount: float


class AssignedDeliveryRow(BaseModel):
    """One delivery this employee is carrying. `id` is the delivery's own id — what
    every /deliveries endpoint takes."""

    id: str
    delivery_number: str | None = None
    order: OrderRef | None = None
    customer: CustomerRef | None = None
    scheduled_at: datetime | None = None
    payment_type: str = Field(
        description="prepaid when the order's invoices are settled, else cod")
    amount_due: float
    status: str = Field(description="The delivery's own status: planned | loaded | in_transit | …")


class VehicleBadge(BaseModel):
    """The van this delivery partner is out with.

    `id` and `vehicle_number` come from the fleet master, taken off the delivery that
    names the van. `loaded_at` and `items` describe the open vehicle loading — what
    actually went onto it — and stay null when nothing has been loaded yet.
    """

    id: str = Field(description="The vehicle's id")
    vehicle_number: str | None = None
    vehicle_type: str | None = None
    loaded_at: datetime | None = None
    items: int = Field(default=0, description="Distinct items currently on the vehicle")


# -------------------------------- generic --------------------------------


class GenericSummary(BaseModel):
    """For any workspace without a purpose-built layout (accounts, HR, …)."""

    orders: int
    sales_amount: float
    assigned_customers: int
    days_present: int


class StaffActivity(BaseModel):
    """One line of the employee's activity feed. Which of the optional blocks are
    filled depends on `type`."""

    type: str = Field(
        description="order_created | payment_received | delivery_completed | "
                    "delivery_failed | delivery_partial | attendance_check_in | "
                    "attendance_check_out")
    at: datetime
    customer: CustomerRef | None = None
    order: OrderRef | None = None
    delivery: DeliveryRef | None = None
    amount: float | None = None
    status: str | None = None
    pod_status: str | None = Field(
        default=None,
        description="captured when a POD photo or signature is on the delivery, "
                    "pending when it was delivered without one, else null")


class StaffOverviewOut(BaseModel):
    user_id: str
    employee_id: str | None = None
    name: str
    workspace: str | None = Field(
        default=None,
        description="From the role. Switch the page layout on this, not on the role name")
    role: RoleBadge | None = None

    period: PeriodName = Field(description="today | week | month (custom when dates were sent)")
    period_from: str = Field(description="First day the figures cover, YYYY-MM-DD")
    period_to: str = Field(description="Last day the figures cover, YYYY-MM-DD")

    attendance: AttendanceToday
    current_location: CurrentLocation

    summary: SalesSummary | DeliverySummary | GenericSummary
    performance: list[SalesPerformancePoint] | list[DeliveryPerformancePoint]
    recent_activity: list[StaffActivity]

    # Sales workspace only; null otherwise.
    recent_orders: list[RecentOrderRow] | None = None
    assigned_customers: list[AssignedCustomerRow] | None = None

    # Delivery workspace only; null otherwise.
    vehicle: VehicleBadge | None = None
    assigned_deliveries: list[AssignedDeliveryRow] | None = None
