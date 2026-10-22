import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

PUSH_PLATFORMS = ("web", "android")


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PushDevice(Base):
    """One FCM registration token (a browser or an Android install) for one user.

    The token is unique: whoever registers it last owns it, since holding the token means
    holding the device -- after a logout/login on the same device, pushes follow the person
    now signed in there, never the previous user.
    """

    __tablename__ = "push_devices"
    __table_args__ = (UniqueConstraint("token", name="uq_push_devices_token"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    organization_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # FCM tokens are ~150-250 chars; 1024 keeps the unique index well inside btree limits.
    token: Mapped[str] = mapped_column(String(1024), nullable=False)
    platform: Mapped[str] = mapped_column(String(10), nullable=False)  # web | android
    label: Mapped[str | None] = mapped_column(String(100), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
