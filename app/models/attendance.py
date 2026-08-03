import uuid
from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# The four daily checkpoints, in order.
ATTENDANCE_TYPES = ["office_check_in", "departure", "return_to_office", "final_check_out"]


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Attendance(Base):
    """One row per user per day, holding up to four checkpoint timestamps."""

    __tablename__ = "attendance"
    __table_args__ = (UniqueConstraint("user_id", "day", name="uq_attendance_user_day"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    day: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    office_check_in: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    departure: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    return_to_office: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    final_check_out: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    @property
    def date(self) -> date:  # exposed as `date` in API responses
        return self.day
