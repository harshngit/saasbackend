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


class UnreadCount(BaseModel):
    unread: int


class MessageResponse(BaseModel):
    detail: str
