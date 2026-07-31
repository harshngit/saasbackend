import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Supplier(Base):
    """A vendor/supplier for an organization, with running payable balances."""

    __tablename__ = "suppliers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    contact_person: Mapped[str | None] = mapped_column(String(150), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    gst_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Balances (all kept in sync). outstanding = opening + purchases - paid.
    opening_balance: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    total_purchases: Mapped[float] = mapped_column(Float, default=0, nullable=False)  # by Purchases module (later)
    total_paid: Mapped[float] = mapped_column(Float, default=0, nullable=False)       # by supplier payments

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    payments: Mapped[list["SupplierPayment"]] = relationship(
        back_populates="supplier", cascade="all, delete-orphan"
    )

    @property
    def outstanding_payable(self) -> float:
        return round((self.opening_balance or 0) + (self.total_purchases or 0) - (self.total_paid or 0), 2)


class SupplierPayment(Base):
    """A payment made to a supplier (reduces outstanding payable)."""

    __tablename__ = "supplier_payments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    supplier_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )

    amount: Mapped[float] = mapped_column(Float, nullable=False)
    payment_mode: Mapped[str] = mapped_column(String(30), default="cash", nullable=False)
    reference: Mapped[str | None] = mapped_column(String(150), nullable=True)  # txn/cheque ref
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    paid_on: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    supplier: Mapped["Supplier"] = relationship(back_populates="payments")
