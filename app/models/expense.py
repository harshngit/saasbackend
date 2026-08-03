import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

EXPENSE_STATUSES = {"pending", "approved", "rejected"}
# Suggested categories (free-form; frontend can offer these).
EXPENSE_CATEGORIES = [
    "Petrol/Diesel", "Food and Travel", "Office Expenses", "Rent", "Utilities",
    "Vehicle Maintenance", "Staff Expenses", "Delivery Expenses", "Parking", "Toll", "Other",
]


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Expense(Base):
    __tablename__ = "expenses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    expense_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    payment_mode: Mapped[str | None] = mapped_column(String(30), nullable=True)
    receipt_url: Mapped[str | None] = mapped_column(Text, nullable=True)  # bill/receipt image

    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False, index=True)
    submitted_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )
