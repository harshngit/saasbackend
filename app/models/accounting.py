from decimal import Decimal
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


ACCOUNT_TYPES = {"asset", "liability", "equity", "revenue", "expense"}

ACCOUNT_CLASSIFICATIONS = {
    # Assets
    "cash_and_bank",
    "accounts_receivable",
    "inventory",
    "fixed_asset",
    "other_current_asset",
    "other_asset",
    # Liabilities
    "accounts_payable",
    "tax_liability",
    "loan_liability",
    "accrued_liability",
    "other_liability",
    # Equity
    "owner_equity",
    "retained_earnings",
    "capital",
    # Income statement
    "operating_revenue",
    "contra_revenue",
    "cogs",
    "operating_expense",
    "other_expense",
}


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("organization_id", "account_code", name="uq_accounts_org_code"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    account_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    classification: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)

    journal_lines: Mapped[list["JournalLine"]] = relationship(
        "JournalLine", back_populates="account"
    )


class JournalEntry(Base):
    __tablename__ = "journal_entries"
    __table_args__ = (
        UniqueConstraint("organization_id", "entry_number", name="uq_journal_entries_org_number"),
        UniqueConstraint("organization_id", "source_type", "source_id", name="uq_journal_entries_org_source"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entry_number: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    entry_type: Mapped[str] = mapped_column(String(32), default="manual", nullable=False, index=True)
    effective_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False, index=True)
    reference: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    source_type: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    source_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="posted", nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_reversal: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reversed_entry_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("journal_entries.id", ondelete="SET NULL"), nullable=True
    )
    created_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)

    lines: Mapped[list["JournalLine"]] = relationship(
        "JournalLine",
        back_populates="entry",
        cascade="all, delete-orphan",
        order_by="JournalLine.line_number",
    )
    reversed_entry: Mapped["JournalEntry | None"] = relationship("JournalEntry", remote_side=[id])


class JournalLine(Base):
    __tablename__ = "journal_lines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    journal_entry_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("journal_entries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    line_number: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    debit: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0.00"), nullable=False)
    credit: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0.00"), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)

    entry: Mapped["JournalEntry"] = relationship("JournalEntry", back_populates="lines")
    account: Mapped["Account"] = relationship("Account", back_populates="journal_lines")
