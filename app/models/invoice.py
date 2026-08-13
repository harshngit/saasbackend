import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    invoice_number: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    sales_id: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    sales_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    sales_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sales_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    invoice_status: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    payment_status: Mapped[str | None] = mapped_column(String(30), nullable=True)

    order_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sales_orders.id", ondelete="SET NULL"), nullable=True, index=True
    )
    customer_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Which delivery this bills. A partial delivery is billed for what was actually
    # handed over, so one order can carry several invoices — see
    # `partial_delivery_invoice_mode` in the firm's workflow settings.
    delivery_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("deliveries.id", ondelete="SET NULL"), nullable=True, index=True
    )
    invoice_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    # When payment is due, from the order's payment terms. Drives the ageing buckets.
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Copied from the customer when the invoice is raised — a bill must keep the
    # address it was issued to, even if the customer later moves.
    billing_address: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="unpaid", nullable=False, index=True)

    subtotal: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    discount: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    tax: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    # Freight, packing, or a delivery fee added to the bill, and the paise rounding
    # that makes the total a whole rupee. Both print on the detailed invoice.
    additional_charges: Mapped[float | None] = mapped_column(Float, nullable=True)
    round_off: Mapped[float | None] = mapped_column(Float, nullable=True)
    total: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    amount_paid: Mapped[float] = mapped_column(Float, default=0, nullable=False)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_credit_note: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    credit_note_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    items: Mapped[list["InvoiceItem"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", lazy="joined"
    )
    customer: Mapped["Customer | None"] = relationship(lazy="joined")  # noqa: F821
    order: Mapped["SalesOrder | None"] = relationship(lazy="joined")  # noqa: F821


class InvoiceItem(Base):
    __tablename__ = "invoice_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    invoice_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    variant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("product_variants.id", ondelete="SET NULL"), nullable=True
    )
    product_name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Copied from the product at invoicing time — a GST invoice must show the HSN
    # that applied then, even if the product's code is corrected later.
    hsn_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    discount: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    tax: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    line_total: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    uom: Mapped[str | None] = mapped_column(String(30), nullable=True)
    tax_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    tax_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    # The delivery line this bills, so an invoice traces back to what was handed over.
    delivery_item_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("delivery_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    order_item_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sales_order_items.id", ondelete="SET NULL"), nullable=True, index=True
    )

    invoice: Mapped["Invoice"] = relationship(back_populates="items")
