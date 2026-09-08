import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, String, Text, UniqueConstraint
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
    company_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    contact_person: Mapped[str | None] = mapped_column(String(150), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    gst_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    pan_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    categories: Mapped[list | None] = mapped_column(JSON, nullable=True)
    supplier_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    payment_terms: Mapped[str | None] = mapped_column(String(100), nullable=True)
    credit_limit: Mapped[float | None] = mapped_column(Float, nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state: Mapped[str | None] = mapped_column(String(100), nullable=True)
    pincode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    country: Mapped[str | None] = mapped_column(String(100), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

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
    supplier_products: Mapped[list["SupplierProduct"]] = relationship(
        back_populates="supplier", cascade="all, delete-orphan"
    )

    @property
    def outstanding_payable(self) -> float:
        return round((self.opening_balance or 0) + (self.total_purchases or 0) - (self.total_paid or 0), 2)

    @property
    def supplier_categories(self) -> list[str]:
        if self.categories and isinstance(self.categories, list) and len(self.categories) > 0:
            return self.categories
        elif self.category:
            return [self.category]
        return []


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

    payment_number: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    payment_mode: Mapped[str] = mapped_column(String(30), default="cash", nullable=False)
    payment_method: Mapped[str | None] = mapped_column(String(50), default="cash", nullable=True)
    reference: Mapped[str | None] = mapped_column(String(150), nullable=True)  # txn/cheque ref
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    paid_on: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    status: Mapped[str] = mapped_column(String(20), default="recorded", nullable=False, index=True)
    allocated_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    unallocated_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    voided_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    void_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    supplier: Mapped["Supplier"] = relationship(back_populates="payments")
    allocations: Mapped[list["SupplierPaymentAllocation"]] = relationship(  # noqa: F821
        back_populates="supplier_payment", cascade="all, delete-orphan", lazy="joined"
    )


class SupplierProduct(Base):
    """A link table representing many-to-many relationships between Suppliers and Products."""

    __tablename__ = "supplier_products"
    __table_args__ = (
        UniqueConstraint("supplier_id", "product_id", name="uq_supplier_product"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    supplier_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    supplier: Mapped["Supplier"] = relationship(back_populates="supplier_products")
    product: Mapped["Product"] = relationship(lazy="joined")  # noqa: F821

