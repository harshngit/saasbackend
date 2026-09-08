import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

SUPPLIER_INVOICE_STATUSES = {"draft", "recorded", "disputed", "cancelled"}
VERIFICATION_STATUSES = {"pending", "matched", "mismatched"}
PAYMENT_STATUSES = {"unpaid", "partially_paid", "paid"}


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SupplierInvoice(Base):
    __tablename__ = "supplier_invoices"
    __table_args__ = (
        UniqueConstraint("organization_id", "supplier_id", "supplier_invoice_number", name="uq_org_supplier_invoice_num"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    supplier_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    purchase_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("purchase_invoices.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    supplier_invoice_number: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    supplier_invoice_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False, index=True)
    verification_status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False, index=True)
    payment_status: Mapped[str] = mapped_column(String(20), default="unpaid", nullable=False, index=True)

    subtotal: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    tax_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    discount_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    grand_total: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    amount_paid: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachment_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recorded_by: Mapped[str | None] = mapped_column(String(36), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    items: Mapped[list["SupplierInvoiceItem"]] = relationship(
        back_populates="supplier_invoice", cascade="all, delete-orphan", lazy="joined"
    )
    supplier: Mapped["Supplier"] = relationship(foreign_keys=[supplier_id], lazy="joined")  # noqa: F821
    purchase: Mapped["PurchaseInvoice"] = relationship(foreign_keys=[purchase_id], lazy="joined")  # noqa: F821

    @property
    def outstanding_amount(self) -> float:
        return round(max(self.grand_total - (self.amount_paid or 0.0), 0.0), 2)


class SupplierInvoiceItem(Base):
    __tablename__ = "supplier_invoice_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    supplier_invoice_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("supplier_invoices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    purchase_item_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("purchase_invoice_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True
    )
    variant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("product_variants.id", ondelete="SET NULL"), nullable=True, index=True
    )

    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    billed_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    tax_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    tax_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    discount_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    line_total: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    supplier_invoice: Mapped["SupplierInvoice"] = relationship(back_populates="items")
    purchase_item: Mapped["PurchaseInvoiceItem"] = relationship(lazy="joined")  # noqa: F821
    product: Mapped["Product | None"] = relationship(lazy="joined")  # noqa: F821
