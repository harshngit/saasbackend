import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Quotation(Base):
    __tablename__ = "quotations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    quotation_number: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    quotation_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    customer_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    billing_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    salesperson_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    currency: Mapped[str | None] = mapped_column(String(10), default="INR", nullable=True)
    # Sheet fields the quotation was missing. `status` is what the UI's
    # Draft / Sent badge reads.
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False, index=True)
    shipping_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    payment_terms: Mapped[str | None] = mapped_column(String(200), nullable=True)
    delivery_terms: Mapped[str | None] = mapped_column(String(200), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    terms_conditions: Mapped[str | None] = mapped_column(Text, nullable=True)


    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    items: Mapped[list["QuotationItem"]] = relationship(
        back_populates="quotation", cascade="all, delete-orphan", lazy="joined"
    )
    customer: Mapped["Customer | None"] = relationship(lazy="joined", primaryjoin="Quotation.customer_id == Customer.id")  # noqa: F821
    salesperson: Mapped["User | None"] = relationship(lazy="joined", primaryjoin="Quotation.salesperson_id == User.id")  # noqa: F821


    @property
    def total(self) -> float:
        """Sum of the line totals — what the list's Amount column shows."""
        return round(sum((i.quantity or 0) * (i.unit_price or 0) for i in self.items), 2)

    @property
    def item_count(self) -> int:
        return len(self.items)


class QuotationItem(Base):
    __tablename__ = "quotation_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    quotation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("quotations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    variant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("product_variants.id", ondelete="SET NULL"), nullable=True
    )
    product_name: Mapped[str] = mapped_column(String(200), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    uom: Mapped[str | None] = mapped_column(String(30), nullable=True)
    unit_price: Mapped[float] = mapped_column(Float, nullable=False)

    quotation: Mapped["Quotation"] = relationship(back_populates="items")
