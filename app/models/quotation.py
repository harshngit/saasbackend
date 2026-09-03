import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.workflow import quotation_effective_status


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
    # A quotation is for exactly one party: a Customer, or — before that Lead has
    # converted — a Lead directly. Both are nullable and independently optional at
    # this layer; app/routers/quotations.py enforces "exactly one of the two" since
    # that is a cross-field business rule, not something a column constraint can
    # express. ON DELETE SET NULL, not CASCADE: deleting a Lead must not delete the
    # quotations raised against it.
    lead_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("leads.id", ondelete="SET NULL"), nullable=True, index=True
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
    # The order this quotation turned into, so the conversion is traceable both ways
    # and a second conversion can be refused.
    converted_order_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sales_orders.id", ondelete="SET NULL"), nullable=True, index=True
    )
    converted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    items: Mapped[list["QuotationItem"]] = relationship(
        back_populates="quotation", cascade="all, delete-orphan", lazy="joined"
    )
    customer: Mapped["Customer | None"] = relationship(lazy="joined", primaryjoin="Quotation.customer_id == Customer.id")  # noqa: F821
    lead: Mapped["Lead | None"] = relationship(lazy="joined", primaryjoin="Quotation.lead_id == Lead.id")  # noqa: F821
    salesperson: Mapped["User | None"] = relationship(lazy="joined", primaryjoin="Quotation.salesperson_id == User.id")  # noqa: F821


    @property
    def subtotal(self) -> float:
        """Line totals after per-line discounts."""
        return round(sum(i.line_total for i in self.items), 2)

    @property
    def tax_total(self) -> float:
        return round(sum(i.tax_amount for i in self.items), 2)

    @property
    def total(self) -> float:
        """What the list's Amount column shows — quoted lines plus quoted tax."""
        return round(self.subtotal + self.tax_total, 2)

    @property
    def item_count(self) -> int:
        return len(self.items)

    @property
    def effective_status(self) -> str:
        """`status`, except a still-`sent` quotation past `valid_until` reads as
        "expired" — computed at read time, never stored. See
        app.core.workflow.quotation_effective_status."""
        return quotation_effective_status(self.status, self.valid_until)


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
    # Quoted terms per line, carried through to the order on conversion so the
    # customer is billed what they were quoted.
    discount: Mapped[float | None] = mapped_column(Float, default=0, nullable=True)
    discount_percent: Mapped[float | None] = mapped_column(Float, default=0, nullable=True)
    tax_rate: Mapped[float | None] = mapped_column(Float, nullable=True)

    @property
    def line_total(self) -> float:
        gross = (self.quantity or 0) * (self.unit_price or 0)
        disc = self.discount or 0
        if disc == 0 and (self.discount_percent or 0) > 0:
            disc = round(gross * (self.discount_percent or 0) / 100, 2)
        return round(gross - disc, 2)

    @property
    def tax_amount(self) -> float:
        return round(self.line_total * (self.tax_rate or 0) / 100, 2)

    quotation: Mapped["Quotation"] = relationship(back_populates="items")
