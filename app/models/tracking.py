"""Batch, expiry and serial tracking — the stock behind the product's tracking flags.

A product with `batch_tracking` (or `expiry_tracking`) on keeps its stock in batches, so
the same product can sit in the warehouse in several lots with different expiry dates,
and a sale can say which lot it came out of. A product with `serial_number_tracking` on
keeps a row per physical unit, so a specific serial can be traced to the invoice it went
out on and back again if it is returned.

Both are layers over warehouse stock, not a replacement for it: the warehouse row is
still the count, and these say what that count is made of.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class StockBatch(Base):
    """One lot of a product in one warehouse.

    `quantity` is what is left of the lot. A lot with no `batch_number` is the
    untracked remainder — stock that was on hand before batches were used, kept as a
    lot of its own so the batch quantities always add up to the warehouse count.
    """

    __tablename__ = "stock_batches"
    __table_args__ = (
        UniqueConstraint(
            "warehouse_id", "product_id", "variant_id", "batch_number",
            name="uq_batch_per_item_per_warehouse",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    warehouse_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("warehouses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    variant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("product_variants.id", ondelete="CASCADE"), nullable=True, index=True
    )

    batch_number: Mapped[str | None] = mapped_column(String(60), nullable=True, index=True)
    manufacturing_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expiry_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    quantity: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    received_quantity: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    mrp: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )


class ProductSerial(Base):
    """One physical unit of a serial-tracked product, and where it is now.

    `status` is `in_stock`, `sold` or `returned`. A serial that has gone out keeps the
    invoice line it went out on, which is what makes a warranty claim traceable.
    """

    __tablename__ = "product_serials"
    __table_args__ = (
        UniqueConstraint("organization_id", "serial_number", name="uq_serial_per_firm"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    variant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("product_variants.id", ondelete="CASCADE"), nullable=True
    )
    warehouse_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("warehouses.id", ondelete="SET NULL"), nullable=True, index=True
    )
    batch_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("stock_batches.id", ondelete="SET NULL"), nullable=True
    )

    serial_number: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default="in_stock", nullable=False, index=True)

    # Where it went, so a unit can be traced from the shelf to the customer.
    invoice_item_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    delivery_item_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    sold_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )
