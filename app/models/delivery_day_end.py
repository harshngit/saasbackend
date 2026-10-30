import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

# open -> submitted -> approved | rejected ; rejected -> submitted (resubmission)
DAY_END_STATUSES = ("open", "submitted", "approved", "rejected")
# Collection categories a Day-End settles, in display order. UPI stays separate from
# Bank because the payments code already treats it as its own method.
DAY_END_METHODS = ("cash", "bank", "upi", "cheque", "other")


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _money() -> Mapped[Decimal]:
    return mapped_column(Numeric(12, 2), default=Decimal("0.00"), nullable=False)


class DeliveryDayEnd(Base):
    """A delivery partner's end-of-day settlement for one business date.

    The canonical Delivery Day-End / Collection Reconciliation record. It never
    holds money of its own: the expected figures are derived from the partner's
    DeliveryCollection rows for that date (snapshotted here at submission, with
    the ids in `collection_ids`, so the Admin approves exactly what was submitted),
    and the delivery counts are always derived live from Delivery rows. What it
    adds is what only a settlement can know: what the partner actually handed
    over, the variance, and the Admin's decision.
    """

    __tablename__ = "delivery_day_ends"
    __table_args__ = (
        UniqueConstraint("organization_id", "delivery_partner_id", "business_date", name="uq_day_end_org_partner_date"),
        Index("ix_day_end_org_status_date", "organization_id", "status", "business_date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    delivery_partner_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    business_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False, index=True)

    # System-expected collections, snapshotted at submission.
    expected_cash: Mapped[Decimal] = _money()
    expected_bank: Mapped[Decimal] = _money()
    expected_upi: Mapped[Decimal] = _money()
    expected_cheque: Mapped[Decimal] = _money()
    expected_other: Mapped[Decimal] = _money()
    expected_total: Mapped[Decimal] = _money()
    collection_ids: Mapped[list | None] = mapped_column(JSON, default=list, nullable=True)

    # Actually handed over by the partner.
    handed_cash: Mapped[Decimal] = _money()
    handed_bank: Mapped[Decimal] = _money()
    handed_upi: Mapped[Decimal] = _money()
    handed_cheque: Mapped[Decimal] = _money()
    handed_other: Mapped[Decimal] = _money()
    handed_total: Mapped[Decimal] = _money()
    variance_total: Mapped[Decimal] = _money()

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    submission_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submitted_by_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    delivery_partner: Mapped["User"] = relationship(foreign_keys=[delivery_partner_id], lazy="selectin")  # noqa: F821
    reviewed_by: Mapped["User | None"] = relationship(foreign_keys=[reviewed_by_id], lazy="selectin")  # noqa: F821
