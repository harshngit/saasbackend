"""Build the Admin → Staff Detail operational summary for one employee.

Which blocks get filled is decided by the role's `workspace`, so a firm that
renames "Sales Officer" to anything it likes still gets the sales layout as long as
the role points at the `sales` workspace.

Everything is read through the employee's own records and scoped to their firm.
Figures reuse the same definitions as the reports and the admin dashboard — a sale
is an order past approval — so the numbers on this page agree with those.
"""

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models import (
    Attendance,
    Customer,
    CustomerPayment,
    Delivery,
    FollowUp,
    Invoice,
    SalesOrder,
    User,
    Vehicle,
    VehicleLoading,
    Visit,
)
from app.schemas.staff_overview import (
    DELIVERY,
    PERIODS,
    SALES,
    AssignedCustomerRow,
    AssignedDeliveryRow,
    AttendanceToday,
    CurrentLocation,
    CustomerRef,
    DeliveryPerformancePoint,
    DeliveryRef,
    DeliverySummary,
    GenericSummary,
    OrderRef,
    RecentOrderRow,
    RoleBadge,
    SalesPerformancePoint,
    SalesSummary,
    StaffActivity,
    StaffOverviewOut,
    VehicleBadge,
)
from app.core.permissions import DEFAULT_DATA_SCOPE
from app.core.workflow import OPEN_DELIVERY_STATUSES
from app.services.report_service import SALE_STATUSES

DEFAULT_DAYS = 7          # the window `performance` covers when none is asked for
RECENT_ORDERS = 10
RECENT_ACTIVITY = 20
ASSIGNED_ROWS = 20

# Fulfilment states a delivery partner still has work to do on. Order status and
# fulfilment are separate axes — see app/core/workflow.py.
# A delivery the partner still has work to do on, and the ones that are done with.
# These are Delivery statuses — see app/core/workflow.py.
_OPEN_DELIVERY = OPEN_DELIVERY_STATUSES
_DELIVERED = ("delivered", "partially_delivered")


def _day(value: str | None, fallback: date, end: bool = False) -> tuple[date, datetime]:
    if value is None:
        parsed = fallback
    else:
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Dates must be YYYY-MM-DD"
            )
    return parsed, datetime.combine(parsed, time.max if end else time.min, tzinfo=timezone.utc)


def _resolve_range(period: str | None, date_from: str | None, date_to: str | None):
    """The window every figure and the performance series cover.

    `period` is what the page sends: `today`, `week` (the last 7 days) or `month` (the
    last 30). Explicit dates are still accepted for a custom range and win when sent,
    and the response says which of the two it ended up using.
    """
    today = datetime.now(timezone.utc).date()

    if date_from or date_to:
        start, df = _day(date_from, today - timedelta(days=DEFAULT_DAYS - 1))
        end, dt = _day(date_to, today, end=True)
        if start > end:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="date_from cannot be after date_to"
            )
        return "custom", start, end, df, dt

    name = (period or "today").strip().lower()
    if name not in PERIODS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"period must be one of {', '.join(PERIODS)}",
        )
    span = {"today": 1, "week": 7, "month": 30}[name]
    start, df = _day((today - timedelta(days=span - 1)).isoformat(), today)
    end, dt = _day(today.isoformat(), today, end=True)
    return name, start, end, df, dt


def _days(start: date, end: date) -> list[str]:
    return [(start + timedelta(days=n)).isoformat() for n in range((end - start).days + 1)]


# ------------------------------ shared blocks ------------------------------


def _attendance(db: Session, staff: User) -> AttendanceToday:
    today = datetime.now(timezone.utc).date()
    row = (
        db.query(Attendance)
        .filter(Attendance.user_id == staff.id, Attendance.day == today)
        .first()
    )
    if row is None:
        return AttendanceToday(date=today.isoformat(), status="absent")

    if row.final_check_out is not None:
        state = "checked_out"
    elif row.office_check_in is not None:
        state = "checked_in"
    else:
        state = "absent"

    minutes = None
    if row.office_check_in is not None:
        started = row.office_check_in
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        ended = row.final_check_out or datetime.now(timezone.utc)
        if ended.tzinfo is None:
            ended = ended.replace(tzinfo=timezone.utc)
        minutes = max(int((ended - started).total_seconds() // 60), 0)

    return AttendanceToday(
        date=row.day.isoformat(),
        status=state,
        check_in=row.office_check_in,
        departure=row.departure,
        return_to_office=row.return_to_office,
        check_out=row.final_check_out,
        active_duration_minutes=minutes,
    )


def _location(staff: User) -> CurrentLocation:
    if staff.last_latitude is None or staff.last_longitude is None:
        return CurrentLocation(available=False)
    return CurrentLocation(
        available=True,
        latitude=staff.last_latitude,
        longitude=staff.last_longitude,
        accuracy_meters=staff.last_location_accuracy_m,
        label=staff.last_location_label,
        updated_at=staff.last_location_at,
    )


def _customer_label(customer: Customer | None) -> str | None:
    if customer is None:
        return None
    return customer.business_name or customer.name


def _customer_ref(customer: Customer | None, customer_id: str | None = None) -> CustomerRef | None:
    """The customer as `{id, name}` — the one shape every row points at them with."""
    if customer is not None:
        return CustomerRef(id=customer.id, name=_customer_label(customer))
    if customer_id:
        return CustomerRef(id=customer_id)
    return None


def _order_ref(order, amount: float | None = None) -> OrderRef | None:  # noqa: ANN001
    if order is None:
        return None
    return OrderRef(
        id=order.id,
        order_number=order.order_number,
        amount=round(order.total or 0, 2) if amount is None else amount,
    )


def _delivery_ref(delivery) -> DeliveryRef | None:  # noqa: ANN001
    if delivery is None:
        return None
    return DeliveryRef(id=delivery.id, delivery_number=delivery.delivery_note_number)


def _their_orders(db: Session, staff: User):
    """Orders this employee is answerable for: they raised it, they are its
    salesperson, or it is out for their delivery."""
    return db.query(SalesOrder).filter(
        SalesOrder.organization_id == staff.organization_id,
        or_(
            SalesOrder.created_by == staff.id,
            SalesOrder.salesperson_id == staff.id,
            SalesOrder.assigned_delivery_partner_id == staff.id,
        ),
    )


def _paid_by_order(db: Session, order_ids: list[str]) -> dict[str, float]:
    """How much has been received against each order.

    Two rows can record the same money: a customer payment names the order, and
    settling an invoice moves that invoice's `amount_paid`. Rather than adding them
    and counting a receipt twice, take whichever source recorded more — a payment
    that named only the invoice still shows up, and one that named only the order
    does too.
    """
    if not order_ids:
        return {}
    from_payments: dict[str, float] = defaultdict(float)
    for payment in db.query(CustomerPayment).filter(CustomerPayment.order_id.in_(order_ids)):
        from_payments[payment.order_id] += payment.amount or 0
    from_invoices: dict[str, float] = defaultdict(float)
    for invoice in db.query(Invoice).filter(
        Invoice.order_id.in_(order_ids), Invoice.is_credit_note.is_(False)
    ):
        from_invoices[invoice.order_id] += invoice.amount_paid or 0
    return {
        order_id: max(from_payments.get(order_id, 0), from_invoices.get(order_id, 0))
        for order_id in set(from_payments) | set(from_invoices)
    }


def _payments_for(db: Session, staff: User, order_ids: list[str], df=None, dt=None):
    """Customer payments recorded against this employee's orders. There is no
    "collected by" column on a payment, so the order's assignment is the link."""
    if not order_ids:
        return []
    query = db.query(CustomerPayment).filter(
        CustomerPayment.organization_id == staff.organization_id,
        CustomerPayment.order_id.in_(order_ids),
    )
    if df is not None:
        query = query.filter(CustomerPayment.received_on >= df)
    if dt is not None:
        query = query.filter(CustomerPayment.received_on <= dt)
    return query.order_by(CustomerPayment.received_on.desc()).all()


def _attendance_activity(db: Session, staff: User, df: datetime, dt: datetime) -> list[StaffActivity]:
    rows = db.query(Attendance).filter(
        Attendance.user_id == staff.id,
        Attendance.day >= df.date(),
        Attendance.day <= dt.date(),
    )
    events = []
    for row in rows:
        if row.office_check_in is not None:
            events.append(StaffActivity(type="attendance_check_in", at=row.office_check_in))
        if row.final_check_out is not None:
            events.append(StaffActivity(type="attendance_check_out", at=row.final_check_out))
    return events


def _vehicle_badge(db, staff, loading, open_deliveries):  # noqa: ANN001
    """The van this partner is out with, and what is on it.

    The vehicle comes from the delivery that names it — the fleet master records which
    van, the loading records what went onto it. With no van named on any delivery there
    is nothing to report, so the block is null rather than a placeholder.
    """
    vehicle = None
    for delivery in open_deliveries:
        if delivery.vehicle_id:
            vehicle = db.get(Vehicle, delivery.vehicle_id)
            if vehicle is not None:
                break
    if vehicle is None:
        recent = (
            db.query(Delivery)
            .filter(
                Delivery.organization_id == staff.organization_id,
                Delivery.delivery_partner_id == staff.id,
                Delivery.vehicle_id.isnot(None),
            )
            .order_by(Delivery.updated_at.desc())
            .first()
        )
        if recent is not None:
            vehicle = db.get(Vehicle, recent.vehicle_id)
    if vehicle is None:
        return None

    return VehicleBadge(
        id=vehicle.id,
        vehicle_number=vehicle.vehicle_number,
        vehicle_type=vehicle.vehicle_type,
        loaded_at=loading.date if loading is not None else None,
        items=len(loading.items or []) if loading is not None else 0,
    )


def _sorted_feed(events: list[StaffActivity]) -> list[StaffActivity]:
    def when(event: StaffActivity) -> datetime:
        moment = event.at
        return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)

    return sorted(events, key=when, reverse=True)[:RECENT_ACTIVITY]


# -------------------------------- sales ----------------------------------


def _sales_blocks(db: Session, staff: User, start: date, end: date, df: datetime, dt: datetime) -> dict:
    today = datetime.now(timezone.utc).date()
    sales_orders = _their_orders(db, staff).filter(SalesOrder.status.in_(SALE_STATUSES))

    in_period = sales_orders.filter(
        SalesOrder.created_at >= df, SalesOrder.created_at <= dt
    ).order_by(SalesOrder.created_at).all()

    def order_day(order: SalesOrder) -> date:
        return (order.order_date or order.created_at).date()

    customers = (
        db.query(Customer)
        .filter(
            Customer.organization_id == staff.organization_id,
            Customer.assigned_sales_officer_id == staff.id,
        )
        .order_by(Customer.created_at.desc())
    )
    assigned_total = customers.count()

    # Last order per customer, so the list can show it without a query each.
    last_order: dict[str, date] = {}
    for order in sales_orders.all():
        if order.customer_id:
            day = order_day(order)
            if order.customer_id not in last_order or day > last_order[order.customer_id]:
                last_order[order.customer_id] = day

    by_day_amount: dict[str, float] = defaultdict(float)
    by_day_count: dict[str, int] = defaultdict(int)
    for order in in_period:
        key = order_day(order).isoformat()
        by_day_amount[key] += order.total or 0
        by_day_count[key] += 1

    recent = _their_orders(db, staff).order_by(SalesOrder.created_at.desc()).limit(RECENT_ORDERS).all()

    feed = [
        StaffActivity(
            type="order_created",
            at=order.created_at,
            customer=_customer_ref(order.customer, order.customer_id),
            order=_order_ref(order),
            amount=round(order.total or 0, 2),
            status=order.status,
        )
        for order in in_period
    ]
    by_id = {order.id: order for order in in_period}
    for payment in _payments_for(db, staff, [o.id for o in in_period], df, dt):
        feed.append(
            StaffActivity(
                type="payment_received",
                at=payment.received_on,
                customer=_customer_ref(None, payment.customer_id),
                order=_order_ref(by_id.get(payment.order_id)),
                amount=round(payment.amount or 0, 2),
            )
        )
    feed += _attendance_activity(db, staff, df, dt)

    # Real visits count for this salesperson in the period
    visits_count = (
        db.query(func.count(Visit.id))
        .filter(
            Visit.organization_id == staff.organization_id,
            Visit.user_id == staff.id,
            Visit.visit_date >= df,
            Visit.visit_date <= dt,
        )
        .scalar()
        or 0
    )

    # Real pending follow-ups assigned to this salesperson
    pending_followups_count = (
        db.query(func.count(FollowUp.id))
        .filter(
            FollowUp.organization_id == staff.organization_id,
            FollowUp.assigned_to_id == staff.id,
            FollowUp.status == "pending",
        )
        .scalar()
        or 0
    )

    # Last visit date per customer
    last_visits = (
        db.query(Visit.customer_id, func.max(Visit.visit_date))
        .filter(Visit.organization_id == staff.organization_id)
        .group_by(Visit.customer_id)
        .all()
    )
    last_visit_map = {row[0]: row[1] for row in last_visits if row[0] and row[1]}

    # Next upcoming pending follow-up date per customer
    next_followups = (
        db.query(FollowUp.customer_id, func.min(FollowUp.due_date))
        .filter(
            FollowUp.organization_id == staff.organization_id,
            FollowUp.status == "pending",
        )
        .group_by(FollowUp.customer_id)
        .all()
    )
    next_followup_map = {row[0]: row[1] for row in next_followups if row[0] and row[1]}

    return {
        "summary": SalesSummary(
            sales_amount=round(sum(o.total or 0 for o in in_period), 2),
            orders=len(in_period),
            assigned_customers=assigned_total,
            visits=visits_count,
            pending_followups=pending_followups_count,
        ),
        "performance": [
            SalesPerformancePoint(
                date=day,
                sales_amount=round(by_day_amount.get(day, 0), 2),
                orders=by_day_count.get(day, 0),
            )
            for day in _days(start, end)
        ],
        "recent_orders": [
            RecentOrderRow(
                id=order.id,
                order_number=order.order_number,
                customer=_customer_ref(order.customer, order.customer_id),
                amount=round(order.total or 0, 2),
                status=order.status,
                date=order_day(order).isoformat(),
            )
            for order in recent
        ],
        "assigned_customers": [
            AssignedCustomerRow(
                id=customer.id,
                name=_customer_label(customer) or customer.name,
                area=customer.territory or customer.city,
                city=customer.city,
                phone=customer.phone,
                outstanding=round(customer.outstanding_balance or 0, 2),
                last_order_date=(
                    last_order[customer.id].isoformat() if customer.id in last_order else None
                ),
                last_visit=(
                    last_visit_map[customer.id].isoformat() if customer.id in last_visit_map else None
                ),
                next_followup=(
                    next_followup_map[customer.id].isoformat() if customer.id in next_followup_map else None
                ),
            )
            for customer in customers.limit(ASSIGNED_ROWS).all()
        ],
        "recent_activity": _sorted_feed(feed),
    }


# ------------------------------- delivery --------------------------------


def _delivery_blocks(db: Session, staff: User, start: date, end: date, df: datetime, dt: datetime) -> dict:
    """The delivery workspace, built from the employee's actual assigned deliveries.

    A Delivery is the record the fulfilment half of the flow turns on, so every figure
    here counts deliveries — not the orders behind them. One order split across two
    vans is two deliveries, which is what the partner actually has to do.
    """
    deliveries = (
        db.query(Delivery)
        .filter(
            Delivery.organization_id == staff.organization_id,
            Delivery.delivery_partner_id == staff.id,
        )
        .order_by(Delivery.created_at.desc())
        .all()
    )

    def delivery_day(delivery: Delivery) -> date:
        moment = delivery.scheduled_date or delivery.created_at
        return moment.date() if isinstance(moment, datetime) else moment

    in_period = [d for d in deliveries if start <= delivery_day(d) <= end]
    orders = {
        order.id: order
        for order in db.query(SalesOrder).filter(
            SalesOrder.id.in_([d.sales_order_id for d in deliveries if d.sales_order_id] or [""])
        )
    }
    paid = _paid_by_order(db, list(orders))

    def order_of(delivery: Delivery):  # noqa: ANN202
        return orders.get(delivery.sales_order_id) if delivery.sales_order_id else None

    def value_of(delivery: Delivery) -> float:
        order = order_of(delivery)
        return round(order.total or 0, 2) if order is not None else 0.0

    def count(rows, *states) -> int:
        return sum(1 for d in rows if d.status in states)

    def pod_status(delivery: Delivery) -> str | None:
        """Whether proof of delivery was actually captured on this delivery."""
        if delivery.pod_signature_file_id or (delivery.pod_photo_file_ids or []):
            return "captured"
        return "pending" if delivery.status in _DELIVERED else None

    completed = [d for d in in_period if d.status in _DELIVERED]
    open_rows = [d for d in deliveries if d.status in _OPEN_DELIVERY]

    # Receivable is on the goods that have actually been handed over, whenever that was.
    receivable = 0.0
    for delivery in deliveries:
        order = order_of(delivery)
        if order is not None and delivery.status in _DELIVERED:
            receivable += max((order.total or 0) - paid.get(order.id, 0), 0)

    payments = _payments_for(db, staff, list(orders), df, dt)
    collected = round(sum(p.amount or 0 for p in payments), 2)

    by_day_count: dict[str, int] = defaultdict(int)
    by_day_amount: dict[str, float] = defaultdict(float)
    for delivery in completed:
        key = delivery_day(delivery).isoformat()
        by_day_count[key] += 1
        by_day_amount[key] += value_of(delivery)

    loading = (
        db.query(VehicleLoading)
        .filter(
            VehicleLoading.organization_id == staff.organization_id,
            VehicleLoading.delivery_partner_id == staff.id,
            VehicleLoading.status == "active",
        )
        .order_by(VehicleLoading.date.desc())
        .first()
    )

    feed: list[StaffActivity] = []
    for delivery in in_period:
        kind = {
            "delivered": "delivery_completed",
            "partially_delivered": "delivery_partial",
            "failed": "delivery_failed",
        }.get(delivery.status)
        if kind is None:
            continue
        order = order_of(delivery)
        feed.append(
            StaffActivity(
                type=kind,
                at=delivery.confirmed_at or delivery.updated_at,
                customer=_customer_ref(delivery.customer, delivery.customer_id),
                order=_order_ref(order),
                delivery=_delivery_ref(delivery),
                amount=value_of(delivery),
                status=delivery.status,
                pod_status=pod_status(delivery),
            )
        )
    for payment in payments:
        feed.append(
            StaffActivity(
                type="payment_received",
                at=payment.received_on,
                customer=_customer_ref(None, payment.customer_id),
                order=_order_ref(orders.get(payment.order_id)),
                amount=round(payment.amount or 0, 2),
            )
        )
    feed += _attendance_activity(db, staff, df, dt)

    return {
        "summary": DeliverySummary(
            deliveries=len(in_period),
            completed=count(in_period, "delivered"),
            pending=count(in_period, *_OPEN_DELIVERY),
            partial=count(in_period, "partially_delivered"),
            failed=count(in_period, "failed"),
            delivery_value=round(sum(value_of(d) for d in in_period), 2),
            amount_collected=collected,
            amount_receivable=round(receivable, 2),
            pod_completed=sum(1 for d in completed if pod_status(d) == "captured"),
        ),
        "performance": [
            DeliveryPerformancePoint(
                date=day,
                deliveries_completed=by_day_count.get(day, 0),
                delivery_amount=round(by_day_amount.get(day, 0), 2),
            )
            for day in _days(start, end)
        ],
        "assigned_deliveries": [
            AssignedDeliveryRow(
                id=delivery.id,
                delivery_number=delivery.delivery_note_number,
                order=_order_ref(order_of(delivery)),
                customer=_customer_ref(delivery.customer, delivery.customer_id),
                scheduled_at=delivery.scheduled_date or delivery.created_at,
                # A settled invoice means it is already paid for; anything else is
                # collect-on-delivery.
                payment_type=_payment_type(order_of(delivery), paid),
                amount_due=_amount_due(order_of(delivery), paid),
                status=delivery.status,
            )
            for delivery in open_rows[:ASSIGNED_ROWS]
        ],
        "vehicle": _vehicle_badge(db, staff, loading, open_rows),
        "recent_activity": _sorted_feed(feed),
    }


def _payment_type(order, paid: dict[str, float]) -> str:  # noqa: ANN001
    if order is None:
        return "cod"
    return "prepaid" if paid.get(order.id, 0) + 0.01 >= (order.total or 0) else "cod"


def _amount_due(order, paid: dict[str, float]) -> float:  # noqa: ANN001
    if order is None:
        return 0.0
    return round(max((order.total or 0) - paid.get(order.id, 0), 0), 2)


# -------------------------------- generic --------------------------------


def _generic_blocks(db: Session, staff: User, start: date, end: date, df: datetime, dt: datetime) -> dict:
    in_period = (
        _their_orders(db, staff)
        .filter(SalesOrder.created_at >= df, SalesOrder.created_at <= dt)
        .all()
    )
    present = (
        db.query(Attendance)
        .filter(
            Attendance.user_id == staff.id,
            Attendance.day >= start,
            Attendance.day <= end,
            Attendance.office_check_in.isnot(None),
        )
        .count()
    )
    assigned = (
        db.query(Customer)
        .filter(
            Customer.organization_id == staff.organization_id,
            Customer.assigned_sales_officer_id == staff.id,
        )
        .count()
    )
    feed = [
        StaffActivity(
            type="order_created", at=order.created_at,
            customer=_customer_ref(order.customer, order.customer_id),
            order=_order_ref(order),
            amount=round(order.total or 0, 2), status=order.status,
        )
        for order in in_period
    ] + _attendance_activity(db, staff, df, dt)

    return {
        "summary": GenericSummary(
            orders=len(in_period),
            sales_amount=round(sum(o.total or 0 for o in in_period), 2),
            assigned_customers=assigned,
            days_present=present,
        ),
        "performance": [
            SalesPerformancePoint(date=day, sales_amount=0, orders=0) for day in _days(start, end)
        ],
        "recent_activity": _sorted_feed(feed),
    }


# --------------------------------- entry ---------------------------------


def build_staff_overview(
    db: Session,
    staff: User,
    period: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> StaffOverviewOut:
    name, start, end, df, dt = _resolve_range(period, date_from, date_to)
    role = staff.role_detail
    workspace = (role.workspace if role is not None else None) or None

    if workspace == SALES:
        blocks = _sales_blocks(db, staff, start, end, df, dt)
    elif workspace == DELIVERY:
        blocks = _delivery_blocks(db, staff, start, end, df, dt)
    else:
        blocks = _generic_blocks(db, staff, start, end, df, dt)

    return StaffOverviewOut(
        user_id=staff.id,
        employee_id=staff.employee_id,
        name=staff.name,
        workspace=workspace,
        role=(
            RoleBadge(
                id=role.id, name=role.name, workspace=role.workspace,
                data_scope=role.data_scope or DEFAULT_DATA_SCOPE,
            )
            if role else None
        ),
        period=name,
        period_from=start.isoformat(),
        period_to=end.isoformat(),
        attendance=_attendance(db, staff),
        current_location=_location(staff),
        **blocks,
    )
