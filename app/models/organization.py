import math
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.enums import OrganizationStatus


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
    # logo/signature can hold a data: URL (base64) or an external URL — use Text (no length cap).
    logo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    signature_url: Mapped[str | None] = mapped_column(Text, nullable=True)  # used on invoices

    status: Mapped[OrganizationStatus] = mapped_column(
        Enum(OrganizationStatus), default=OrganizationStatus.TRIAL, nullable=False
    )

    # --- Subscription & trial lifecycle (all nullable so they auto-migrate on the live DB) ---
    plan_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("plans.id"), nullable=True, index=True
    )
    requested_plan_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("plans.id"), nullable=True
    )
    # "monthly" / "yearly" (BillingCycle value); which cycle the org is/wants to be billed on.
    billing_cycle: Mapped[str | None] = mapped_column(String(10), nullable=True)

    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    upgrade_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # UpgradeStatus value ("none"/"pending"/"approved"/"rejected").
    upgrade_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    upgrade_reject_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    users: Mapped[list["User"]] = relationship(  # noqa: F821
        back_populates="organization", cascade="all, delete-orphan"
    )
    plan: Mapped["Plan | None"] = relationship(  # noqa: F821
        foreign_keys=[plan_id], lazy="joined"
    )
    requested_plan: Mapped["Plan | None"] = relationship(  # noqa: F821
        foreign_keys=[requested_plan_id], lazy="joined"
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
