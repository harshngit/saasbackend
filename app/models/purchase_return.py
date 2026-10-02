import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


PURCHASE_RETURN_STATUSES = {"draft", "confirmed", "dispatched", "completed", "cancelled"}


class PurchaseReturn(Base):
    """A return of goods back to a supplier against a Purchase Invoice."""

    __tablename__ = "purchase_returns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    return_number: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    purchase_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("purchase_invoices.id", ondelete="SET NULL"), nullable=True, index=True
    )
    supplier_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("suppliers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    grn_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("goods_receipt_notes.id", ondelete="SET NULL"), nullable=True, index=True
    )
    warehouse_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("warehouses.id", ondelete="SET NULL"), nullable=True, index=True
    )

    status: Mapped[str] = mapped_column(String(30), default="draft", nullable=False, index=True)
    return_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    stock_deducted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dispatched_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_by: Mapped[str | None] = mapped_column(String(36), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    items: Mapped[list["PurchaseReturnItem"]] = relationship(
        back_populates="purchase_return", cascade="all, delete-orphan", lazy="joined"
    )
    purchase: Mapped["PurchaseInvoice | None"] = relationship(foreign_keys=[purchase_id], lazy="joined")  # noqa: F821
    supplier: Mapped["Supplier | None"] = relationship(foreign_keys=[supplier_id], lazy="joined")  # noqa: F821
    grn: Mapped["GoodsReceiptNote | None"] = relationship(foreign_keys=[grn_id], lazy="joined")  # noqa: F821
    warehouse: Mapped["Warehouse | None"] = relationship(foreign_keys=[warehouse_id], lazy="joined")  # noqa: F821

    @property
    def total_return_qty(self) -> int:
        return sum(item.quantity for item in self.items)

    @property
    def total_amount(self) -> float:
        return round(sum(item.line_total for item in self.items), 2)


class PurchaseReturnItem(Base):
    __tablename__ = "purchase_return_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    purchase_return_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("purchase_returns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    purchase_item_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("purchase_invoice_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    product_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True
    )
    variant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("product_variants.id", ondelete="SET NULL"), nullable=True, index=True
    )

    product_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    barcode: Mapped[str | None] = mapped_column(String(100), nullable=True)
    product_name: Mapped[str] = mapped_column(String(200), nullable=False)
    unit_of_measure_uom: Mapped[str | None] = mapped_column(String(30), nullable=True)

    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    tax_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    tax_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    line_total: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    batch_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    serial_numbers: Mapped[list[str] | None] = mapped_column(JSON, default=list, nullable=True)
    expiry_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    purchase_return: Mapped["PurchaseReturn"] = relationship(back_populates="items")
    product: Mapped["Product | None"] = relationship(foreign_keys=[product_id], lazy="joined")  # noqa: F821
    variant: Mapped["ProductVariant | None"] = relationship(foreign_keys=[variant_id], lazy="joined")  # noqa: F821
