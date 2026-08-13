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
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import (
    Attendance,
    Customer,
    CustomerPayment,
    Delivery,
    Invoice,
    SalesOrder,
    User,
    VehicleLoading,
)
from app.schemas.staff_overview import (
    DELIVERY,
    SALES,
    AssignedCustomerRow,
    AssignedDeliveryRow,
    AttendanceToday,
    CurrentLocation,
    DeliveryBreakdown,
    DeliveryPerformancePoint,
    DeliverySummary,
    GenericSummary,
    Period,
    RecentOrderRow,
    RoleBadge,
    SalesPerformancePoint,
    SalesSummary,
    StaffActivity,
    StaffOverviewOut,
    VehicleBadge,
)
from app.services.report_service import SALE_STATUSES

DEFAULT_DAYS = 7          # the window `performance` covers when none is asked for
RECENT_ORDERS = 10
RECENT_ACTIVITY = 20
ASSIGNED_ROWS = 20

# Fulfilment states a delivery partner still has work to do on. Order status and
# fulfilment are separate axes — see app/core/workflow.py.
_PENDING_DELIVERY = ("reserved", "planned", "loaded", "in_transit")
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


def _resolve_range(date_from: str | None, date_to: str | None):
    """The window `performance` and the period figures cover — the last week by
    default, so the page has a sparkline without asking for one."""
    today = datetime.now(timezone.utc).date()
    start, df = _day(date_from, today - timedelta(days=DEFAULT_DAYS - 1))
    end, dt = _day(date_to, today, end=True)
    if start > end:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="date_from cannot be after date_to"
        )
    return start, end, df, dt


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

    today_orders = [o for o in in_period if order_day(o) == today]

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
            customer_id=order.customer_id,
            customer_name=_customer_label(order.customer),
            order_id=order.id,
            order_number=order.order_number,
            amount=round(order.total or 0, 2),
            status=order.status,
        )
        for order in in_period
    ]
    for payment in _payments_for(db, staff, [o.id for o in in_period], df, dt):
        feed.append(
            StaffActivity(
                type="payment_received",
                at=payment.received_on,
                customer_id=payment.customer_id,
                order_id=payment.order_id,
                amount=round(payment.amount or 0, 2),
            )
        )
    feed += _attendance_activity(db, staff, df, dt)

    return {
        "summary": SalesSummary(
            today_sales=round(sum(o.total or 0 for o in today_orders), 2),
            orders_today=len(today_orders),
            period_sales=round(sum(o.total or 0 for o in in_period), 2),
            period_orders=len(in_period),
            assigned_customers=assigned_total,
            visits_today=None,
            pending_followups=None,
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
                customer_id=order.customer_id,
                customer_name=_customer_label(order.customer),
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
                last_visit=None,
                next_followup=None,
            )
            for customer in customers.limit(ASSIGNED_ROWS).all()
        ],
        "recent_activity": _sorted_feed(feed),
    }


# ------------------------------- delivery --------------------------------


def _delivery_blocks(db: Session, staff: User, start: date, end: date, df: datetime, dt: datetime) -> dict:
    today = datetime.now(timezone.utc).date()
    assigned = db.query(SalesOrder).filter(
        SalesOrder.organization_id == staff.organization_id,
        SalesOrder.assigned_delivery_partner_id == staff.id,
    )

    def order_day(order: SalesOrder) -> date:
        return (order.order_date or order.created_at).date()

    all_assigned = assigned.order_by(SalesOrder.created_at.desc()).all()
    todays = [o for o in all_assigned if order_day(o) == today]
    in_period = [o for o in all_assigned if start <= order_day(o) <= end]

    def count(orders, *states) -> int:
        return sum(1 for o in orders if o.fulfilment_status in states)

    paid = _paid_by_order(db, [o.id for o in all_assigned])
    delivered_period = [o for o in in_period if o.fulfilment_status in _DELIVERED]
    receivable = sum(
        max((o.total or 0) - paid.get(o.id, 0), 0)
        for o in all_assigned
        if o.fulfilment_status in _DELIVERED
    )

    payments = _payments_for(db, staff, [o.id for o in all_assigned], df, dt)
    collected = round(sum(p.amount or 0 for p in payments), 2)

    by_day_count: dict[str, int] = defaultdict(int)
    by_day_amount: dict[str, float] = defaultdict(float)
    for order in delivered_period:
        key = order_day(order).isoformat()
        by_day_count[key] += 1
        by_day_amount[key] += order.total or 0

    # Delivery notes, so a row can show its note number where one was raised.
    notes = {
        note.sales_order_id: note
        for note in db.query(Delivery).filter(
            Delivery.organization_id == staff.organization_id,
            Delivery.sales_order_id.in_([o.id for o in all_assigned] or [""]),
        )
    }

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
    for order in in_period:
        kind = {
            "delivered": "delivery_completed",
            "partially_delivered": "delivery_partial",
            "failed": "delivery_failed",
        }.get(order.fulfilment_status)
        if kind is None:
            continue
        note = notes.get(order.id)
        feed.append(
            StaffActivity(
                type=kind,
                at=order.updated_at,
                customer_id=order.customer_id,
                customer_name=_customer_label(order.customer),
                order_id=order.id,
                order_number=order.order_number,
                delivery_id=note.id if note else None,
                delivery_number=note.delivery_note_number if note else None,
                amount=round(order.total or 0, 2),
                status=order.status,
                pod_status=None,
            )
        )
    for payment in payments:
        feed.append(
            StaffActivity(
                type="payment_received",
                at=payment.received_on,
                customer_id=payment.customer_id,
                order_id=payment.order_id,
                amount=round(payment.amount or 0, 2),
            )
        )
    feed += _attendance_activity(db, staff, df, dt)

    open_deliveries = [o for o in all_assigned if o.fulfilment_status in _PENDING_DELIVERY]

    return {
        "summary": DeliverySummary(
            deliveries_today=len(todays),
            completed_today=count(todays, "delivered"),
            pending_today=count(todays, *_PENDING_DELIVERY),
            partial_today=count(todays, "partially_delivered"),
            failed_today=count(todays, "failed"),
            delivery_value=round(sum(o.total or 0 for o in todays), 2),
            amount_collected=collected,
            amount_receivable=round(receivable, 2),
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
                id=order.id,
                order_id=order.id,
                order_number=order.order_number,
                delivery_number=(
                    notes[order.id].delivery_note_number if order.id in notes else None
                ),
                customer_id=order.customer_id,
                customer_name=_customer_label(order.customer),
                scheduled_at=order.order_date or order.created_at,
                # No payment-type column exists; a settled invoice means it is
                # already paid for, anything else is collect-on-delivery.
                payment_type="prepaid" if paid.get(order.id, 0) + 0.01 >= (order.total or 0) else "cod",
                amount_due=round(max((order.total or 0) - paid.get(order.id, 0), 0), 2),
                status=order.status,
            )
            for order in open_deliveries[:ASSIGNED_ROWS]
        ],
        "delivery_summary": DeliveryBreakdown(
            successful=count(in_period, "delivered"),
            pending=count(in_period, *_PENDING_DELIVERY),
            partial=count(in_period, "partially_delivered"),
            failed=count(in_period, "failed"),
            amount_collected=collected,
            pod_completed=None,
        ),
        "vehicle": (
            VehicleBadge(
                id=loading.id,
                vehicle_number=None,
                loaded_at=loading.date,
                items=len(loading.items or []),
            )
            if loading is not None
            else None
        ),
        "recent_activity": _sorted_feed(feed),
    }


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
            customer_id=order.customer_id, customer_name=_customer_label(order.customer),
            order_id=order.id, order_number=order.order_number,
            amount=round(order.total or 0, 2), status=order.status,
        )
        for order in in_period
    ] + _attendance_activity(db, staff, df, dt)

    return {
        "summary": GenericSummary(
            orders_created_period=len(in_period),
            sales_amount_period=round(sum(o.total or 0 for o in in_period), 2),
            assigned_customers=assigned,
            days_present_period=present,
        ),
        "performance": [
            SalesPerformancePoint(date=day, sales_amount=0, orders=0) for day in _days(start, end)
        ],
        "recent_activity": _sorted_feed(feed),
    }


# --------------------------------- entry ---------------------------------


def build_staff_overview(
    db: Session, staff: User, date_from: str | None = None, date_to: str | None = None
) -> StaffOverviewOut:
    start, end, df, dt = _resolve_range(date_from, date_to)
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
        role=RoleBadge(id=role.id, name=role.name, workspace=role.workspace) if role else None,
        period=Period(date_from=start.isoformat(), date_to=end.isoformat()),
        attendance=_attendance(db, staff),
        current_location=_location(staff),
        **blocks,
    )
