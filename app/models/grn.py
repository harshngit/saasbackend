import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class GoodsReceiptNote(Base):
    __tablename__ = "goods_receipt_notes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    grn_number: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    purchase_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("purchase_invoices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    supplier_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("suppliers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    warehouse_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("warehouses.id", ondelete="SET NULL"), nullable=True, index=True
    )

    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False, index=True)
    received_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(String(36), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    items: Mapped[list["GoodsReceiptNoteItem"]] = relationship(
        back_populates="grn", cascade="all, delete-orphan", lazy="joined"
    )
    purchase: Mapped["PurchaseInvoice | None"] = relationship(foreign_keys=[purchase_id], lazy="joined")  # noqa: F821
    supplier: Mapped["Supplier | None"] = relationship(foreign_keys=[supplier_id], lazy="joined")  # noqa: F821
    warehouse: Mapped["Warehouse | None"] = relationship(foreign_keys=[warehouse_id], lazy="joined")  # noqa: F821


class GoodsReceiptNoteItem(Base):
    __tablename__ = "goods_receipt_note_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    grn_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("goods_receipt_notes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    purchase_item_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("purchase_invoice_items.id", ondelete="CASCADE"), nullable=True, index=True
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

    ordered_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    received_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    damaged_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rejected_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    accepted_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    batch_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    serial_numbers: Mapped[list[str] | None] = mapped_column(JSON, default=list, nullable=True)
    expiry_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    grn: Mapped["GoodsReceiptNote"] = relationship(back_populates="items")
