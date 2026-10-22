import uuid

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.core import notification_types as nt
from app.core.realtime import queue_event
from app.models import Notification, SystemRole, User, UserRole


def notify(db: Session, user_id: str, title: str, body: str | None = None,
           type: str | None = None, link: str | None = None, organization_id: str | None = None,
           event: str | None = None, data: dict | None = None) -> Notification:
    """Create a notification for a single user (does not commit).

    `event` (an app.core.notification_types name) records what happened: it sets the in-app
    category `type` when none is given and is stored in `data["event"]`. `data` holds only
    identifiers (see notification_types.build_data). After the caller's transaction commits,
    the realtime event is emitted and -- when push is enabled -- a push with the same
    notification id is sent to the recipient's devices; a rollback discards both.
    """
    if data is not None and event is None:
        event = data.get("event")
    if type is None:
        type = nt.EVENT_CATEGORY.get(event, "info") if event else "info"
    if event and data is None:
        data = nt.build_data(event)

    resolved_org_id = organization_id
    if not resolved_org_id:
        user = db.get(User, user_id)
        resolved_org_id = user.organization_id if user else None

    n = Notification(
        id=str(uuid.uuid4()),  # known now, so the realtime event and the push carry it
        user_id=user_id, organization_id=resolved_org_id, title=title, body=body,
        type=type, link=link, data=data,
    )
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
        },
        user_id=user_id,
    )

    from app.services import push_service

    push_service.queue_notification_push(db, n)
    return n


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


def notify_org_admins(db: Session, organization_id: str, title: str, body: str | None = None,
                      type: str | None = None, link: str | None = None, event: str | None = None,
                      data: dict | None = None, exclude_user_id: str | None = None) -> list[Notification]:
    """Notify every admin of an organization once (optionally not the person who acted)."""
    created = []
    for admin in org_admins(db, organization_id):
        if admin.id == exclude_user_id:
            continue
        created.append(notify(db, admin.id, title, body, type, link, organization_id, event=event, data=data))
    return created


# Order statuses worth a notification. Intermediate steps (processing, picking, ready, ...)
# are deliberately left out: they change many times a day and would be noise.
_ORDER_STATUS_TITLES = {
    "completed": "Sales order completed",
    "cancelled": "Sales order cancelled",
}


def notify_order_status_changed(db: Session, order, previous_status: str | None,
                                actor_id: str | None = None) -> list[Notification]:
    """Tell the org admins (not the person who acted) that an order was completed or cancelled.
    A no-op when the status did not actually change or is not one of the above."""
    title = _ORDER_STATUS_TITLES.get(order.status)
    if title is None or order.status == previous_status:
        return []
    return notify_org_admins(
        db, order.organization_id, title, order.order_number, type="order", link=order.id,
        data=nt.build_data(nt.ORDER_STATUS_CHANGED, order_id=order.id, status=order.status),
        exclude_user_id=actor_id,
    )


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
