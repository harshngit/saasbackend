import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ActivityLog(Base):
    """One recorded change in a firm — the source of the Recent Activity feed.

    Written through `activity_service.record()`. The actor's name is stored on the
    row rather than only referenced, so the feed still reads correctly after the
    user who made the change has been deleted.
    """

    __tablename__ = "activity_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Denormalized so a deleted user's changes still say who made them.
    actor_name: Mapped[str | None] = mapped_column(String(150), nullable=True)

    # Coarse grouping for filtering / icons: company_profile, billing,
    # authorized_person, address, branding, online_presence, document, employee.
    type: Mapped[str] = mapped_column(String(50), default="company_profile", nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False, index=True
    )
