from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.pagination import paginate
from app.models import Notification, User
from app.schemas.notification import MessageResponse, NotificationOut, UnreadCount
from app.schemas.pagination import PaginatedResponse

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=PaginatedResponse[NotificationOut])
def list_notifications(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
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
    q = q.order_by(Notification.created_at.desc(), Notification.id.desc())
    return paginate(q, page=page, page_size=page_size)


@router.get("/unread-count", response_model=UnreadCount)
def unread_count(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> UnreadCount:
    n = db.query(Notification).filter(Notification.user_id == user.id, Notification.is_read.is_(False)).count()
    return UnreadCount(unread=n)


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
