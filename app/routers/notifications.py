from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.pagination import paginate
from app.core.sorting import apply_sorting
from app.models import Notification, PushDevice, User
from app.schemas.bulk import BulkDelete, BulkDeleteResult
from app.schemas.notification import MessageResponse, NotificationOut, UnreadCount
from app.schemas.pagination import PaginatedResponse
from app.schemas.push import PushDeviceOut, PushDeviceRegister, PushTestResult
from app.services import push_service

router = APIRouter(prefix="/notifications", tags=["notifications"])

NOTIFICATION_SORTABLE_FIELDS = {
    "created_at": Notification.created_at,
    "title": Notification.title,
    "is_read": Notification.is_read,
}


@router.get("", response_model=PaginatedResponse[NotificationOut])
def list_notifications(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    sort_by: str | None = Query(default=None, description="Field to sort by"),
    sort_order: str = Query(default="asc", description="Sort order: asc or desc"),
    search: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    unread_only: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> PaginatedResponse[NotificationOut]:
    q = db.query(Notification).filter(Notification.user_id == user.id)
    if unread_only:
        q = q.filter(Notification.is_read.is_(False))
    if search:
        s = f"%{search.strip()}%"
        q = q.filter(
            or_(
                Notification.title.ilike(s),
                Notification.body.ilike(s),
            )
        )
    q = apply_sorting(
        q,
        sort_by=sort_by,
        sort_order=sort_order,
        sortable_fields=NOTIFICATION_SORTABLE_FIELDS,
        default_order=[Notification.created_at.desc(), Notification.id.desc()],
        secondary_key=Notification.id.desc(),
    )
    return paginate(q, page=page, page_size=page_size)


@router.get("/unread-count", response_model=UnreadCount)
def unread_count(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> UnreadCount:
    n = db.query(Notification).filter(Notification.user_id == user.id, Notification.is_read.is_(False)).count()
    return UnreadCount(unread=n)


@router.post("/bulk-delete", response_model=BulkDeleteResult)
@router.delete("/bulk-delete", response_model=BulkDeleteResult)
def bulk_delete_notifications(
    payload: BulkDelete,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BulkDeleteResult:
    """Bulk delete notifications belonging to the current user.

    User-scoped: users cannot affect another user's notifications.
    Atomic (all-or-nothing).
    """
    unique_ids = list(dict.fromkeys(payload.ids))
    notifs = (
        db.query(Notification)
        .filter(Notification.id.in_(unique_ids), Notification.user_id == user.id)
        .all()
    )
    if len(notifs) != len(unique_ids):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="One or more notifications not found",
        )
    for n in notifs:
        db.delete(n)
    db.commit()
    return BulkDeleteResult(deleted=len(unique_ids))


@router.post("/clear-read", response_model=BulkDeleteResult)
def clear_read_notifications(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BulkDeleteResult:
    """Delete all read notifications for the current user."""
    deleted = (
        db.query(Notification)
        .filter(Notification.user_id == user.id, Notification.is_read.is_(True))
        .delete(synchronize_session=False)
    )
    db.commit()
    return BulkDeleteResult(deleted=deleted)


@router.patch("/{notification_id}/read", response_model=NotificationOut)
def mark_read(notification_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Notification:
    n = db.get(Notification, notification_id)
    if n is None or n.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")
    n.is_read = True
    db.commit()
    db.refresh(n)
    return n


@router.patch("/read-all", response_model=MessageResponse)
def mark_all_read(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> MessageResponse:
    updated = (
        db.query(Notification)
        .filter(Notification.user_id == user.id, Notification.is_read.is_(False))
        .update({Notification.is_read: True}, synchronize_session=False)
    )
    db.commit()
    return MessageResponse(detail=f"Marked {updated} notification(s) read")


@router.delete("/{notification_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_notification(
    notification_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    """Delete a single notification belonging to the current user."""
    n = db.get(Notification, notification_id)
    if n is None or n.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")
    db.delete(n)
    db.commit()


# ------------------------------ push devices (FCM) ------------------------------


@router.post("/devices/register", response_model=PushDeviceOut)
def register_push_device(
    payload: PushDeviceRegister,
    response: Response,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PushDevice:
    """Register this browser / Android install for push, or refresh its token.

    The owner is always the signed-in user (and their organization). A token already registered
    by another user of the same organization moves to the current user -- whoever holds the
    device now gets its pushes. A token registered in a different organization is not moved:
    409, and the existing registration is left as it is (the previous user must unregister the
    device on logout, or the client must request a fresh FCM token).
    201 when a new registration is created, 200 when an existing one is refreshed. The response
    never contains the token, only its last 6 characters."""
    try:
        device, created = push_service.register_device(db, user, payload.token, payload.platform, payload.label)
    except push_service.PushTokenConflict:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This device is registered to another organization. Unregister it there first, "
            "or request a new push token on the device.",
        )
    db.commit()
    db.refresh(device)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return device


@router.delete("/devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_push_device(
    device_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    """Unregister one of the current user's devices (e.g. on logout). Someone else's device,
    or one already removed, is 404."""
    device = db.get(PushDevice, device_id)
    if device is None or device.user_id != user.id or device.organization_id != user.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    db.delete(device)
    db.commit()


@router.post("/devices/test", response_model=PushTestResult)
def send_test_push(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> PushTestResult:
    """Send a test notification to the current user's own registered devices -- for checking a
    web / Android integration end to end. It can only ever reach the caller's devices."""
    result = push_service.send_to_user(
        db, user, "Test notification", "Push notifications are working for this device.",
        data={"type": "push_test"},
    )
    db.commit()  # persists removal of any token Firebase reported as invalid
    return PushTestResult(
        enabled=result.enabled, attempted=result.attempted, accepted=result.accepted,
        invalid_tokens_removed=result.invalid_tokens_removed, transient_failures=result.transient_failures,
        detail=result.detail,
    )
