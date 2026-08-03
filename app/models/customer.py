import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Customer(Base):
    """A customer belonging to one organization (tenant-scoped CRM record)."""

    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )

    name: Mapped[str] = mapped_column(String(150), nullable=False)          # contact person
    business_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    gst_number: Mapped[str | None] = mapped_column(String(20), nullable=True)

    billing_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivery_address: Mapped[str | None] = mapped_column(Text, nullable=True)

    assigned_sales_officer_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    credit_limit: Mapped[float] = mapped_column(Float, default=0, nullable=False)

    # Receivables (kept in sync). outstanding = opening + billed - received.
    opening_balance: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    total_billed: Mapped[float] = mapped_column(Float, default=0, nullable=False)   # from confirmed orders
    total_received: Mapped[float] = mapped_column(Float, default=0, nullable=False)  # from customer payments
    outstanding_balance: Mapped[float] = mapped_column(Float, default=0, nullable=False)  # maintained

    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Customer Profile Fields
    customer_id: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    customer_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    primary_contact_person: Mapped[str | None] = mapped_column(String(150), nullable=True)
    maps_latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    maps_longitude: Mapped[float | None] = mapped_column(Float, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    assigned_sales_officer: Mapped["User | None"] = relationship(  # noqa: F821
        foreign_keys=[assigned_sales_officer_id], lazy="joined"
    )

    def recompute_outstanding(self) -> None:
        self.outstanding_balance = round(
            (self.opening_balance or 0) + (self.total_billed or 0) - (self.total_received or 0), 2
        )


class CustomerPayment(Base):
    """A payment received from a customer (reduces their outstanding balance)."""

    __tablename__ = "customer_payments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    customer_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sales_orders.id", ondelete="SET NULL"), nullable=True
    )
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    payment_mode: Mapped[str] = mapped_column(String(30), default="cash", nullable=False)
    reference: Mapped[str | None] = mapped_column(String(150), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_on: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
