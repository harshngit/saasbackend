import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Notification(Base):
    """An in-app notification addressed to a single user."""

    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_user_id_created_at", "user_id", "created_at"),
        Index("ix_notifications_user_id_is_read", "user_id", "is_read"),
        Index("ix_notifications_organization_id_created_at", "organization_id", "created_at"),
        # One notification per (recipient, state transition): a retried request, a webhook
        # redelivery or a second reminder sweep finds the row and creates nothing. NULL keys
        # (events that may legitimately repeat) are never compared.
        Index("ux_notifications_user_id_dedupe_key", "user_id", "dedupe_key", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    organization_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    type: Mapped[str] = mapped_column(String(50), default="info", nullable=False)  # info/order/expense/...
    link: Mapped[str | None] = mapped_column(String(300), nullable=True)  # optional deep link / ref id
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    # Structured identifiers for the event (see app/core/notification_types.py), e.g.
    # {"event": "delivery_assigned", "delivery_id": "..."}. Never amounts, names or secrets.
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Identifies the state transition this notification reports, e.g.
    # "follow_up:<id>:overdue:<due iso>". Server-side only; not exposed by the API.
    dedupe_key: Mapped[str | None] = mapped_column(String(200), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
