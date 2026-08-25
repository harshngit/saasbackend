import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Visit(Base):
    """A customer / sales visit (field CRM activity), independent of Sales Orders."""

    __tablename__ = "visits"
    __table_args__ = (
        Index("ix_visits_org_customer", "organization_id", "customer_id"),
        Index("ix_visits_org_date", "organization_id", "visit_date"),
        Index("ix_visits_org_user", "organization_id", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lead_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("leads.id", ondelete="SET NULL"), nullable=True, index=True
    )
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    visit_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False, index=True)
    visit_type: Mapped[str] = mapped_column(String(50), default="meeting", nullable=False)
    purpose: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="planned", nullable=False, index=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    customer: Mapped["Customer"] = relationship(lazy="joined", foreign_keys=[customer_id])  # noqa: F821
    lead: Mapped["Lead | None"] = relationship(lazy="joined", foreign_keys=[lead_id])  # noqa: F821
    user: Mapped["User | None"] = relationship(lazy="joined", foreign_keys=[user_id])  # noqa: F821
    follow_ups: Mapped[list["FollowUp"]] = relationship(
        back_populates="visit", cascade="all, delete-orphan", lazy="selectin"
    )  # noqa: F821
