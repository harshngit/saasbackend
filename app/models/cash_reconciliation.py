import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Index, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class CashReconciliation(Base):
    """Daily Cash & Tender Reconciliation session for an organization.

    Tracks two distinct reconciliations:
    1. Per-method collections: Actual customer payments vs physical/system tally.
    2. Cash balance movement: Opening balance + cash inflows - cash outflows vs actual cash on hand.
    """

    __tablename__ = "cash_reconciliations"
    __table_args__ = (
        UniqueConstraint("organization_id", "reconciliation_date", name="uq_cash_reconciliation_org_date"),
        Index("ix_cash_reconciliation_org_date", "organization_id", "reconciliation_date"),
        Index("ix_cash_reconciliation_org_status_date", "organization_id", "status", "reconciliation_date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reconciliation_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # Status: 'draft' or 'closed'
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False, index=True)

    # ---------------------------------------------------------
    # Concept 1: Collections across all payment methods
    # ---------------------------------------------------------
    total_expected_collections: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00"), nullable=False
    )
    total_actual_collections: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00"), nullable=False
    )
    collections_variance: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00"), nullable=False
    )

    # ---------------------------------------------------------
    # Concept 2: Cash Movement & Closing Cash Balance
    # Formula: expected_closing_cash = opening_balance + cash_sales + cash_received - cash_paid - expenses_paid
    # Variance: cash_balance_variance = actual_cash_on_hand - expected_closing_cash
    # ---------------------------------------------------------
    opening_balance: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00"), nullable=False
    )
    cash_sales: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00"), nullable=False
    )
    cash_received: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00"), nullable=False
    )
    cash_paid: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00"), nullable=False
    )
    expenses_paid: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00"), nullable=False
    )
    expected_closing_cash: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00"), nullable=False
    )
    actual_cash_on_hand: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00"), nullable=False
    )
    cash_balance_variance: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00"), nullable=False
    )

    # ---------------------------------------------------------
    # Explanations & Metadata
    # ---------------------------------------------------------
    variance_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    closed_by_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    # Relationships
    details: Mapped[list["CashReconciliationDetail"]] = relationship(
        back_populates="reconciliation", cascade="all, delete-orphan", lazy="selectin"
    )
    created_by: Mapped["User | None"] = relationship(foreign_keys=[created_by_id], lazy="selectin")  # noqa: F821
    closed_by: Mapped["User | None"] = relationship(foreign_keys=[closed_by_id], lazy="selectin")  # noqa: F821


class CashReconciliationDetail(Base):
    """Payment method breakdown for a CashReconciliation session."""

    __tablename__ = "cash_reconciliation_details"
    __table_args__ = (
        UniqueConstraint("reconciliation_id", "payment_method", name="uq_cash_reconciliation_detail_method"),
        Index("ix_cash_reconciliation_detail_org_method", "organization_id", "payment_method"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    reconciliation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("cash_reconciliations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Payment method key (e.g. 'cash', 'upi', 'card', 'bank_transfer', 'cheque', 'cod')
    payment_method: Mapped[str] = mapped_column(String(30), nullable=False)

    expected_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    actual_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    variance_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"), nullable=False)

    notes: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    reconciliation: Mapped["CashReconciliation"] = relationship(back_populates="details")
