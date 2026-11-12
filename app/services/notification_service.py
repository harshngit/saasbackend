import logging
import uuid

from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import notification_types as nt
from app.core.realtime import queue_event
from app.models import PLATFORM_ROLES, Notification, Role, SystemRole, User, UserRole

logger = logging.getLogger("crm.notifications")


def _already_sent(db: Session, user_id: str, dedupe_key: str) -> bool:
    """Whether this recipient already has the notification for this transition -- in the
    database, or added earlier in the same (not yet flushed) transaction."""
    for pending in db.new:
        if isinstance(pending, Notification) and pending.user_id == user_id and pending.dedupe_key == dedupe_key:
            return True
    return (
        db.query(Notification.id)
        .filter(Notification.user_id == user_id, Notification.dedupe_key == dedupe_key)
        .first()
        is not None
    )


def notify(db: Session, user_id: str, title: str, body: str | None = None,
           type: str | None = None, link: str | None = None, organization_id: str | None = None,
           event: str | None = None, data: dict | None = None, *,
           actor_id: str | None = None, dedupe_key: str | None = None) -> Notification | None:
    """Create a notification for a single user (does not commit).

    `event` (an app.core.notification_types name) records what happened: it sets the in-app
    category `type` when none is given and is stored in `data["event"]`. `data` holds only
    identifiers (see notification_types.build_data); `actor_id`, when given, is stored in it
    as well. After the caller's transaction commits, the realtime event is emitted and --
    when push is enabled -- a push with the same notification id is sent to the recipient's
    devices; a rollback discards both.

    `dedupe_key` names the state transition being reported. When the recipient already has
    a notification with that key (a retried request, a redelivered webhook, a second
    reminder sweep) nothing is created and None is returned. A unique index on
    (user_id, dedupe_key) settles concurrent attempts: the loser is skipped, never raised.
    """
    if data is not None and event is None:
        event = data.get("event")
    if type is None:
        type = nt.EVENT_CATEGORY.get(event, "info") if event else "info"
    if event and data is None:
        data = nt.build_data(event)
    if actor_id:
        data = {**(data or {}), "actor_id": str(actor_id)}

    resolved_org_id = organization_id
    if not resolved_org_id and event not in nt.PLATFORM_EVENTS:
        user = db.get(User, user_id)
        resolved_org_id = user.organization_id if user else None

    if dedupe_key and _already_sent(db, user_id, dedupe_key):
        return None

    n = Notification(
        id=str(uuid.uuid4()),  # known now, so the realtime event and the push carry it
        user_id=user_id, organization_id=resolved_org_id, title=title, body=body,
        type=type, link=link, data=data, dedupe_key=dedupe_key,
    )
    if dedupe_key:
        # Flush the caller's own pending work first, so the savepoint below holds only this
        # row: an IntegrityError inside it can then only be the dedupe index.
        db.flush()
        try:
            with db.begin_nested():
                db.add(n)
                db.flush()
        except IntegrityError:
            if _already_sent(db, user_id, dedupe_key):
                return None  # a concurrent attempt recorded it first
            raise
    else:
        db.add(n)

    queue_event(
        db,
        org_id=resolved_org_id or "global",
        event_name="notification.created",
        data={
            "id": n.id,
            "user_id": user_id,
            "title": title,
            "body": body,
            "type": type,
            "link": link,
            "data": data,
            **nt.describe(data),
        },
        user_id=user_id,
    )

    from app.services import push_service

    push_service.queue_notification_push(db, n)
    return n


def notify_users(db: Session, users, title: str, body: str | None = None, *, link: str | None = None,
                 organization_id: str | None = None, data: dict | None = None,
                 exclude_user_ids=(), actor_id: str | None = None,
                 dedupe_key: str | None = None) -> list[Notification]:
    """Notify each distinct user once, skipping anyone in `exclude_user_ids`."""
    skip = {u for u in exclude_user_ids if u}
    created = []
    for user in users:
        if user is None or user.id in skip:
            continue
        skip.add(user.id)
        n = notify(db, user.id, title, body, link=link, organization_id=organization_id, data=data,
                   actor_id=actor_id, dedupe_key=dedupe_key)
        if n is not None:
            created.append(n)
    return created


# ------------------------------------------------------------------ recipients


def org_admins(db: Session, organization_id: str) -> list[User]:
    """Active admins of one organization by their *effective* role: system_role "admin", or
    (no system_role) the legacy role ADMIN -- the same rule as User.effective_system_role.
    Super admins and staff are never included."""
    return (
        db.query(User)
        .filter(
            User.organization_id == organization_id,
            User.is_active.is_(True),
            or_(
                User.system_role == SystemRole.ADMIN.value,
                and_(or_(User.system_role.is_(None), User.system_role == ""), User.role == UserRole.ADMIN),
            ),
        )
        .order_by(User.id)
        .all()
    )


def staff_with_permission(db: Session, organization_id: str, module: str, action: str) -> list[User]:
    """Active non-admin staff of one organization whose role grants `module:action` -- e.g.
    the Accountant role for payment_receipts:view. Admins are not included: callers notify
    them through org_admins, so nobody is notified twice."""
    rows = (
        db.query(User, Role)
        .join(Role, Role.id == User.role_id)
        .filter(User.organization_id == organization_id, User.is_active.is_(True),
                Role.organization_id == organization_id)
        .order_by(User.id)
        .all()
    )
    return [
        user for user, role in rows
        if user.effective_system_role not in ("admin", "super_admin")
        and ((role.permissions or {}).get(module) or {}).get(action, False)
    ]


def platform_users(db: Session) -> list[User]:
    """Active platform users: Super Admins and platform staff (Sales Manager). They have no
    organization, which is what keeps their notifications out of every tenant feed."""
    return (
        db.query(User)
        .filter(User.organization_id.is_(None), User.is_active.is_(True), User.system_role.in_(PLATFORM_ROLES))
        .order_by(User.id)
        .all()
    )


def _member(db: Session, user_id: str | None, organization_id: str) -> User | None:
    """The user, when they are an active member of this organization -- never anyone else's."""
    if not user_id:
        return None
    user = db.get(User, user_id)
    if user is None or not user.is_active or user.organization_id != organization_id:
        return None
    return user


def _can_view(db: Session, user: User, module: str, record, *owner_attrs: str, team_attributes=None) -> bool:  # noqa: ANN001
    """Whether `user` could open `record` through its detail endpoint: the module's view
    permission and, for an own/team-scope role, ownership -- the same checks the detail route
    applies. A notification is never sent to someone its deep link would refuse."""
    from app.core import scoping
    from app.services import role_service

    if role_service.is_full_access(user):
        return True
    perms = role_service.effective_permissions(db, user)
    if not (perms.get(module) or {}).get("view", False):
        return False
    return scoping.owns_record(db, user, record, *owner_attrs, team_attributes=team_attributes)


def notify_org_admins(db: Session, organization_id: str, title: str, body: str | None = None,
                      type: str | None = None, link: str | None = None, event: str | None = None,
                      data: dict | None = None, exclude_user_id: str | None = None, *,
                      actor_id: str | None = None, dedupe_key: str | None = None) -> list[Notification]:
    """Notify every admin of an organization once (optionally not the person who acted)."""
    created = []
    for admin in org_admins(db, organization_id):
        if admin.id == exclude_user_id:
            continue
        n = notify(db, admin.id, title, body, type, link, organization_id, event=event, data=data,
                   actor_id=actor_id, dedupe_key=dedupe_key)
        if n is not None:
            created.append(n)
    return created


def _money(amount) -> str:  # noqa: ANN001
    return f"₹{float(amount or 0):,.2f}"


# ------------------------------------------------------------------ orders

# Order statuses worth a notification. Intermediate steps (processing, picking, ready, ...)
# are deliberately left out: they change many times a day and would be noise.
_ORDER_STATUS_TITLES = {
    "completed": "Sales order completed",
    "cancelled": "Sales order cancelled",
}


def _order_salesperson(db: Session, order) -> User | None:  # noqa: ANN001
    """The salesperson responsible for an order: its own salesperson, else the customer's
    assigned sales officer -- and only when they can open the order."""
    customer = getattr(order, "customer", None)
    for user_id in (getattr(order, "salesperson_id", None), getattr(customer, "assigned_sales_officer_id", None)):
        user = _member(db, user_id, order.organization_id)
        if user is not None:
            if _can_view(db, user, "sales_orders", order, "created_by", "salesperson_id",
                         "assigned_delivery_partner_id", team_attributes=("salesperson_id",)):
                return user
            return None
    return None


_ORDER_SCOPE = ("created_by", "salesperson_id", "assigned_delivery_partner_id")


def notify_order_placed(db: Session, order, actor_id: str | None, event: str,  # noqa: ANN001
                        title: str) -> list[Notification]:
    """A new (non-draft) or confirmed order: the org admins, the order's salesperson, the
    customer's assigned sales officer and the salesperson's team manager -- each once, never
    the person who placed it, and only people who can open the order (the same scope check
    as the order's detail route). A retry of the same event creates nothing new."""
    from app.models import Customer, Team

    org_id = order.organization_id
    body = f"{order.order_number} — Rs {order.total:,.2f}"
    data = nt.build_data(event, order_id=order.id)
    key = f"order:{order.id}:{event}"
    created = notify_org_admins(db, org_id, title, body, type="order", link=order.id, data=data,
                                exclude_user_id=actor_id, actor_id=actor_id, dedupe_key=key)

    salesperson = _member(db, getattr(order, "salesperson_id", None), org_id)
    customer = getattr(order, "customer", None) or (
        db.get(Customer, order.customer_id) if getattr(order, "customer_id", None) else None)
    team = db.get(Team, salesperson.team_id) if salesperson is not None and salesperson.team_id else None
    candidate_ids = (
        getattr(salesperson, "id", None),
        getattr(customer, "assigned_sales_officer_id", None),
        team.manager_id if team is not None and team.organization_id == org_id else None,
    )
    recipients = []
    for user_id in candidate_ids:
        user = _member(db, user_id, org_id)
        if (user is not None and user.effective_system_role != "admin"  # admins heard above
                and _can_view(db, user, "sales_orders", order, *_ORDER_SCOPE,
                              team_attributes=("salesperson_id",))):
            recipients.append(user)
    created += notify_users(db, recipients, title, body, link=order.id, organization_id=org_id, data=data,
                            exclude_user_ids=[actor_id], actor_id=actor_id, dedupe_key=key)
    return created


def notify_order_status_changed(db: Session, order, previous_status: str | None,
                                actor_id: str | None = None) -> list[Notification]:
    """Tell the org admins and the order's salesperson (never the person who acted) that an
    order was completed or cancelled. A no-op when the status did not actually change or is
    not one of the above; a retry of the same transition creates nothing new."""
    title = _ORDER_STATUS_TITLES.get(order.status)
    if title is None or order.status == previous_status:
        return []
    data = nt.build_data(nt.ORDER_STATUS_CHANGED, order_id=order.id, status=order.status)
    key = f"order:{order.id}:{order.status}"
    created = notify_org_admins(
        db, order.organization_id, title, order.order_number, type="order", link=order.id,
        data=data, exclude_user_id=actor_id, dedupe_key=key,
    )
    salesperson = _order_salesperson(db, order)
    if salesperson is not None and salesperson.effective_system_role != "admin":
        created += notify_users(db, [salesperson], title, order.order_number, link=order.id,
                                organization_id=order.organization_id, data=data,
                                exclude_user_ids=[actor_id], dedupe_key=key)
    return created


_DELIVERY_STATUS_TITLES = {
    "delivered": "Delivery completed",
    "partially_delivered": "Delivery partially completed",
    "failed": "Delivery failed",
    "cancelled": "Delivery cancelled",
}


def notify_delivery_status_changed(db: Session, delivery, previous_status: str | None,
                                   actor_id: str | None = None) -> list[Notification]:
    """Delivery outcomes (delivered / partially delivered / failed) go to the org admins;
    a cancellation goes to the assigned delivery partner. The actor is never notified, and
    nothing is sent when the status did not change."""
    title = _DELIVERY_STATUS_TITLES.get(delivery.status)
    if title is None or delivery.status == previous_status:
        return []
    body = delivery.delivery_number or delivery.delivery_note_number
    data = nt.build_data(nt.DELIVERY_STATUS_CHANGED, delivery_id=delivery.id, status=delivery.status)
    if delivery.status == "cancelled":
        partner_id = delivery.delivery_partner_id
        if not partner_id or partner_id == actor_id:
            return []
        return [notify(db, partner_id, title, body, type="delivery", link=delivery.id,
                       organization_id=delivery.organization_id, data=data)]
    return notify_org_admins(
        db, delivery.organization_id, title, body, type="delivery", link=delivery.id,
        data=data, exclude_user_id=actor_id,
    )


# ------------------------------------------------------------------ sales officer


def notify_lead_assigned(db: Session, lead, previous_assignee_id: str | None,
                         actor_id: str | None = None) -> list[Notification]:
    """The newly assigned salesperson hears about a Lead assigned (or reassigned) to them --
    not when they assigned it to themselves, and not when the assignee did not change."""
    new_id = lead.assigned_salesperson_id
    if not new_id or new_id == previous_assignee_id or new_id == actor_id:
        return []
    user = _member(db, new_id, lead.organization_id)
    if user is None:
        return []
    title = "Lead reassigned to you" if previous_assignee_id else "New lead assigned"
    label = " · ".join(p for p in (lead.lead_id, lead.name) if p)
    return notify_users(db, [user], title, label or None, link=f"/leads/{lead.id}",
                        organization_id=lead.organization_id,
                        data=nt.build_data(nt.LEAD_ASSIGNED, lead_id=lead.id), actor_id=actor_id)


_QUOTATION_TITLES = {"accepted": "Quotation accepted", "rejected": "Quotation rejected"}


def notify_quotation_status_changed(db: Session, quotation, previous_status: str | None,
                                    actor_id: str | None = None) -> list[Notification]:
    """Accepted / rejected goes to the quotation's salesperson (else the Lead's or the
    customer's owner, if they can open the quotation) -- not when they made the change."""
    title = _QUOTATION_TITLES.get(quotation.status)
    if title is None or quotation.status == previous_status:
        return []
    lead = getattr(quotation, "lead", None)
    customer = getattr(quotation, "customer", None)
    candidates = (quotation.salesperson_id, getattr(lead, "assigned_salesperson_id", None),
                  getattr(customer, "assigned_sales_officer_id", None))
    recipient = None
    for user_id in candidates:
        user = _member(db, user_id, quotation.organization_id)
        if user is not None:
            recipient = user if _can_view(db, user, "quotations", quotation, "salesperson_id") else None
            break
    if recipient is None:
        return []
    party = getattr(customer, "name", None) or getattr(lead, "name", None)
    body = " · ".join(p for p in (quotation.quotation_number, party) if p) or None
    return notify_users(db, [recipient], title, body, link=f"/quotations/{quotation.id}",
                        organization_id=quotation.organization_id,
                        data=nt.build_data(nt.QUOTATION_STATUS_CHANGED, quotation_id=quotation.id,
                                           status=quotation.status),
                        exclude_user_ids=[actor_id], actor_id=actor_id)


# ------------------------------------------------------------------ expenses


def notify_expense_status_changed(db: Session, expense, actor_id: str | None = None) -> list[Notification]:
    """After approve / reject / reimburse: the submitter is told (unless they did it
    themselves), and an approved, not yet reimbursed expense goes to the people who can
    reimburse it (admins and staff with expenses:approve), never the approver or the
    submitter. Each transition notifies once per recipient."""
    status_ = expense.lifecycle_status
    if status_ not in ("approved", "rejected", "reimbursed"):
        return []
    org_id = expense.organization_id
    label = expense.expense_number or expense.category
    created: list[Notification] = []

    submitter = _member(db, expense.submitted_by, org_id)
    if submitter is not None:
        if status_ == "approved":
            title = "Expense approved"
            body = f"{label} · {expense.category}: {_money(expense.net_payable)} payable"
        elif status_ == "rejected":
            title = "Expense rejected"
            body = f"{label} · {expense.category}: {_money(expense.amount)}"
            if expense.reject_reason:
                body += f" — {expense.reject_reason}"
        else:
            title = "Expense reimbursed"
            method = (expense.reimbursement_method or "").replace("_", " ")
            body = f"{label} · {_money(expense.reimbursed_amount)} paid" + (f" by {method}" if method else "")
        created += notify_users(
            db, [submitter], title, body, link=expense.id, organization_id=org_id,
            data=nt.build_data(nt.EXPENSE_STATUS_CHANGED, expense_id=expense.id, status=status_),
            exclude_user_ids=[actor_id], actor_id=actor_id, dedupe_key=f"expense:{expense.id}:{status_}",
        )

    if status_ == "approved":
        reimbursers = org_admins(db, org_id) + staff_with_permission(db, org_id, "expenses", "approve")
        created += notify_users(
            db, reimbursers, "Expense ready for reimbursement",
            f"{label} · {expense.category}: {_money(expense.net_payable)}", link=expense.id,
            organization_id=org_id,
            data=nt.build_data(nt.EXPENSE_READY_FOR_REIMBURSEMENT, expense_id=expense.id),
            exclude_user_ids=[actor_id, expense.submitted_by], actor_id=actor_id,
            dedupe_key=f"expense:{expense.id}:ready_for_reimbursement",
        )
    return created


# ------------------------------------------------------------------ payments


def notify_payment_received(db: Session, payment, invoice, *, admin_title: str, admin_body: str | None,
                            link: str | None, actor_id: str | None = None) -> list[Notification]:
    """A customer payment: the org admins get the message they always got (except the person
    who recorded it), and finance staff with payment_receipts:view get "Payment received"
    with customer, document, amount and method. Once per payment and recipient, so a
    redelivered webhook or a replayed request never repeats it."""
    org_id = payment.organization_id
    data = nt.build_data(nt.PAYMENT_RECEIVED, payment_id=payment.id,
                         invoice_id=invoice.id if invoice is not None else None)
    key = f"payment:{payment.id}:received"
    created = notify_org_admins(db, org_id, admin_title, admin_body, type="success", link=link, data=data,
                                exclude_user_id=actor_id, actor_id=actor_id, dedupe_key=key)

    from app.models import Customer

    customer_id = getattr(payment, "customer_id", None)
    customer = (db.get(Customer, customer_id) if customer_id else None) or getattr(payment, "customer", None)         or getattr(invoice, "customer", None)
    document = (f"Invoice {invoice.invoice_number}" if invoice is not None and invoice.invoice_number
                else payment.receipt_number)
    parts = [getattr(customer, "name", None) or "Walk-in", document, _money(payment.amount), payment.payment_mode]
    created += notify_users(
        db, staff_with_permission(db, org_id, "payment_receipts", "view"), "Payment received",
        " · ".join(p for p in parts if p), link=link, organization_id=org_id, data=data,
        exclude_user_ids=[actor_id], actor_id=actor_id, dedupe_key=key,
    )
    return created


# ------------------------------------------------------------------ delivery day-end


def notify_day_end_submitted(db: Session, day_end, partner: User) -> list[Notification]:
    """One notification per admin per submission. A variance turns it into the
    "requires attention" version instead of adding a second one."""
    variance = float(day_end.variance_total or 0)
    date_label = day_end.business_date.isoformat() if day_end.business_date else ""
    summary = (f"Expected {_money(day_end.expected_total)}, handed over {_money(day_end.handed_total)}")
    if abs(variance) >= 0.005:
        title = "Day-End requires attention"
        direction = "short" if variance < 0 else "over"
        body = (f"{partner.name} submitted Day-End for {date_label} with a {_money(abs(variance))} "
                f"variance ({direction}). {summary}.")
    else:
        title = "Day-End submitted"
        body = f"{partner.name} submitted Day-End for {date_label}. {summary}, no variance."
    return notify_org_admins(
        db, day_end.organization_id, title, body, link=f"/delivery-day-ends/{day_end.id}",
        data=nt.build_data(nt.DAY_END_SUBMITTED, day_end_id=day_end.id, delivery_partner_id=partner.id,
                           has_variance="true" if abs(variance) >= 0.005 else "false"),
        exclude_user_id=partner.id, actor_id=partner.id,
        dedupe_key=f"day_end:{day_end.id}:submitted:{day_end.submission_count or 1}",
    )


def notify_day_end_decided(db: Session, day_end, actor_id: str) -> list[Notification]:
    """Approved / rejected goes to the delivery partner; a rejection carries the reason and
    opens Close My Day so they can correct and resubmit."""
    if day_end.status not in ("approved", "rejected"):
        return []
    partner = _member(db, day_end.delivery_partner_id, day_end.organization_id)
    if partner is None:
        return []
    date_label = day_end.business_date.isoformat() if day_end.business_date else ""
    if day_end.status == "approved":
        title, body, link = "Day-End approved", f"Your Day-End for {date_label} was approved.", \
            f"/delivery-day-ends/{day_end.id}"
    else:
        title = "Day-End rejected"
        body = f"Your Day-End for {date_label} was rejected: {day_end.rejection_reason}. Correct it and resubmit."
        link = "/delivery-day-ends/current"
    return notify_users(
        db, [partner], title, body, link=link, organization_id=day_end.organization_id,
        data=nt.build_data(nt.DAY_END_STATUS_CHANGED, day_end_id=day_end.id, status=day_end.status),
        exclude_user_ids=[actor_id], actor_id=actor_id,
        dedupe_key=f"day_end:{day_end.id}:{day_end.status}:{day_end.submission_count or 1}",
    )


# ------------------------------------------------------------------ inventory


def stock_level(quantity: float, minimum: int | None) -> str:
    """The same rule as the dashboard's stock watch: out of stock at or below zero, low at or
    below the product's minimum_stock_level (when one is set), otherwise ok."""
    if quantity <= 0:
        return "out_of_stock"
    if minimum and quantity <= minimum:
        return "low_stock"
    return "ok"


_STOCK_RANK = {"ok": 0, "low_stock": 1, "out_of_stock": 2}


def notify_stock_transition(db: Session, org_id: str, warehouse_id: str, product_id: str,
                            variant_id: str | None, before: float, after: float,
                            available: float | None = None) -> list[Notification]:
    """Called by every physical stock move. Admins are told only when the warehouse's level
    gets worse -- ok -> low, ok/low -> out -- so neither reads nor repeated moves within the
    same level notify again. Products that do not track inventory are ignored."""
    from app.models import Product, ProductVariant, Warehouse

    product = db.get(Product, product_id)
    if product is None or product.organization_id != org_id or not product.inventory_tracking:
        return []
    minimum = product.minimum_stock_level
    old_level, new_level = stock_level(before, minimum), stock_level(after, minimum)
    if _STOCK_RANK[new_level] <= _STOCK_RANK[old_level]:
        return []
    variant = db.get(ProductVariant, variant_id) if variant_id else None
    warehouse = db.get(Warehouse, warehouse_id)
    name = product.name + (f" ({variant.name})" if variant is not None and variant.name else "")
    title = "Out of stock" if new_level == "out_of_stock" else "Low stock"
    shown = available if available is not None else after
    body = (f"{name} at {getattr(warehouse, 'name', 'warehouse')}: {shown:g} available"
            + (f" (minimum {minimum})" if minimum else ""))
    return notify_org_admins(
        db, org_id, title, body,
        link=f"/warehouses/stock?warehouse_id={warehouse_id}&product_id={product_id}",
        data=nt.build_data(nt.STOCK_ALERT, product_id=product_id, variant_id=variant_id,
                           warehouse_id=warehouse_id, status=new_level),
    )


# ------------------------------------------------------------------ platform


def notify_organization_created(db: Session, org) -> list[Notification]:  # noqa: ANN001
    """A new organization (trial) for the platform team -- platform-scoped, never in a tenant feed."""
    status_ = getattr(org.status, "value", org.status)
    return notify_users(
        db, platform_users(db), "New organization created",
        f"{org.name} signed up" + (f" ({status_})" if status_ else ""),
        link=f"/superadmin/organizations/{org.id}",
        data=nt.build_data(nt.ORGANIZATION_CREATED, target_organization_id=org.id),
        dedupe_key=f"organization:{org.id}:created",
    )


def notify_upgrade_requested(db: Session, org, plan_name: str | None, *, request_id: str | None = None,  # noqa: ANN001
                             requested_at=None, actor_id: str | None = None) -> list[Notification]:  # noqa: ANN001
    """An upgrade request for the platform team (Super Admins and platform staff), not the
    person who raised it. Org-admin requests are keyed by their submission time, platform
    requests by their own id, so a re-submitted request after a decision notifies again but
    a replayed one does not."""
    if request_id:
        link, key = f"/platform/upgrade-requests/{request_id}", f"upgrade_request:{request_id}"
    else:
        stamp = requested_at.isoformat() if requested_at is not None else ""
        link, key = f"/superadmin/organizations/{org.id}", f"upgrade:{org.id}:{stamp}"
    return notify_users(
        db, platform_users(db), "New upgrade request",
        f"{org.name} requested an upgrade" + (f" to {plan_name}" if plan_name else ""),
        link=link,
        data=nt.build_data(nt.UPGRADE_REQUESTED, target_organization_id=org.id, upgrade_request_id=request_id),
        exclude_user_ids=[actor_id], actor_id=actor_id, dedupe_key=key,
    )


# ------------------------------------------------------------------ support tickets
# In-app only (none of these events is in notification_types._PUSHED). `data` is exactly
# {"event", "ticket_id", "ticket_number"}: no actor, no status, and titles/bodies name the
# ticket only -- never its subject, a message or an internal note (internal notes notify
# nobody at all). Recipients exclude the person who acted.

_TICKET_STATUS_LABEL = {
    "open": "Open", "in_progress": "In progress", "waiting_on_customer": "Waiting on you",
    "resolved": "Resolved", "closed": "Closed",
}


def _ticket_data(event: str, ticket) -> dict:  # noqa: ANN001
    return nt.build_data(event, ticket_id=ticket.id, ticket_number=ticket.ticket_number)


def _active_platform_user(db: Session, user_id: str | None) -> User | None:
    user = db.get(User, user_id) if user_id else None
    if user is None or user.organization_id is not None or not user.is_active:
        return None
    return user if user.system_role in PLATFORM_ROLES else None


def notify_ticket_created(db: Session, ticket, org_name: str | None, actor_id: str | None) -> list[Notification]:  # noqa: ANN001
    return notify_users(
        db, platform_users(db), f"New support ticket {ticket.ticket_number}",
        f"{org_name or 'An organization'} raised a support ticket.",
        link=f"/platform/support/tickets/{ticket.id}",
        data=_ticket_data(nt.TICKET_CREATED, ticket),
        exclude_user_ids=[actor_id], dedupe_key=f"ticket_created:{ticket.id}",
    )


def notify_ticket_customer_reply(db: Session, ticket, message_id: str, actor_id: str | None) -> list[Notification]:  # noqa: ANN001
    """The assigned platform user, or every platform user while nobody is assigned."""
    assignee = _active_platform_user(db, ticket.assigned_to_user_id)
    recipients = [assignee] if assignee is not None else platform_users(db)
    return notify_users(
        db, recipients, f"New reply on {ticket.ticket_number}", "The customer replied to the ticket.",
        link=f"/platform/support/tickets/{ticket.id}",
        data=_ticket_data(nt.TICKET_REPLIED_BY_CUSTOMER, ticket),
        exclude_user_ids=[actor_id], dedupe_key=f"ticket_reply:{message_id}",
    )


def notify_ticket_platform_reply(db: Session, ticket, message_id: str, actor_id: str | None) -> list[Notification]:  # noqa: ANN001
    """The ticket's creator. Only ever called for a public reply."""
    creator = _member(db, ticket.created_by_user_id, ticket.organization_id)
    return notify_users(
        db, [creator], f"Support replied on {ticket.ticket_number}", "There is a new reply on your support ticket.",
        link=f"/support/tickets/{ticket.id}",
        data=_ticket_data(nt.TICKET_REPLIED_BY_PLATFORM, ticket),
        organization_id=ticket.organization_id,
        exclude_user_ids=[actor_id], dedupe_key=f"ticket_reply:{message_id}",
    )


def notify_ticket_status_changed(db: Session, ticket, actor_id: str | None) -> list[Notification]:  # noqa: ANN001
    """The ticket's creator, once per status transition."""
    creator = _member(db, ticket.created_by_user_id, ticket.organization_id)
    stamp = ticket.last_activity_at.isoformat() if ticket.last_activity_at else ""
    return notify_users(
        db, [creator], f"Ticket {ticket.ticket_number} updated",
        f"Your support ticket is now {_TICKET_STATUS_LABEL.get(ticket.status, ticket.status)}.",
        link=f"/support/tickets/{ticket.id}",
        data=_ticket_data(nt.TICKET_STATUS_CHANGED, ticket),
        organization_id=ticket.organization_id,
        exclude_user_ids=[actor_id], dedupe_key=f"ticket_status:{ticket.id}:{ticket.status}:{stamp}",
    )
