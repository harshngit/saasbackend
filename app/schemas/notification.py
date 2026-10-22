from datetime import datetime

from pydantic import BaseModel, ConfigDict


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    body: str | None
    type: str
    link: str | None
    is_read: bool
    created_at: datetime
    # Added for push/in-app parity: the event and its identifiers, and when it was first read.
    data: dict[str, str] | None = None
    read_at: datetime | None = None


class UnreadCount(BaseModel):
    unread: int


class MessageResponse(BaseModel):
    detail: str


from app.schemas.bulk import BulkDelete, BulkDeleteResult  # noqa: E402

__all__ = [
    "NotificationOut",
    "UnreadCount",
    "MessageResponse",
    "BulkDelete",
    "BulkDeleteResult",
]
