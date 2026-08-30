import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class FollowUp(Base):
    """A scheduled follow-up action or task, optionally linked to a prior Visit."""

    __tablename__ = "follow_ups"
    __table_args__ = (
        Index("ix_follow_ups_org_customer", "organization_id", "customer_id"),
        Index("ix_follow_ups_org_due", "organization_id", "due_date"),
        Index("ix_follow_ups_org_assigned", "organization_id", "assigned_to_id"),
        Index("ix_follow_ups_org_status", "organization_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Nullable: a follow-up on a lead-only Visit (no converted Customer yet) has no
    # customer to point at — its parent chain is FollowUp -> Visit -> Lead instead.
    customer_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("customers.id", ondelete="CASCADE"), nullable=True, index=True
    )
    visit_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("visits.id", ondelete="SET NULL"), nullable=True, index=True
    )
    assigned_to_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    due_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    priority: Mapped[str] = mapped_column(String(20), default="medium", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    customer: Mapped["Customer | None"] = relationship(lazy="joined", foreign_keys=[customer_id])  # noqa: F821
    visit: Mapped["Visit | None"] = relationship(back_populates="follow_ups", lazy="joined", foreign_keys=[visit_id])  # noqa: F821
    assigned_to: Mapped["User | None"] = relationship(lazy="joined", foreign_keys=[assigned_to_id])  # noqa: F821
