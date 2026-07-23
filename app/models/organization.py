import math
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.enums import OrganizationStatus, PlanTier


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Organization(Base):
    """A firm/shop/business — the tenant boundary. All firm data is scoped to it."""

    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # Business profile.
    business_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    gst_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    pan_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    financial_year: Mapped[str | None] = mapped_column(String(20), nullable=True)
    logo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    plan: Mapped[PlanTier] = mapped_column(Enum(PlanTier), default=PlanTier.FREE, nullable=False)
    status: Mapped[OrganizationStatus] = mapped_column(
        Enum(OrganizationStatus), default=OrganizationStatus.TRIAL, nullable=False
    )

    # --- Trial & upgrade lifecycle (all nullable so they auto-migrate on the live DB) ---
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Stored as the PlanTier *value* (e.g. "pro"); nullable until an upgrade is requested.
    requested_plan: Mapped[str | None] = mapped_column(String(20), nullable=True)
    upgrade_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Stored as the UpgradeStatus value ("none"/"pending"/"approved"/"rejected").
    upgrade_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    upgrade_reject_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    users: Mapped[list["User"]] = relationship(  # noqa: F821
        back_populates="organization", cascade="all, delete-orphan"
    )

    @property
    def trial_days_left(self) -> int | None:
        """Whole days remaining in the trial (0 if past), or None if no trial set."""
        if self.trial_ends_at is None:
            return None
        end = self.trial_ends_at
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        seconds = (end - datetime.now(timezone.utc)).total_seconds()
        return max(0, math.ceil(seconds / 86400))
