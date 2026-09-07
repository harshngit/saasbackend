import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class WarehouseTransfer(Base):
    __tablename__ = "warehouse_transfers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    transfer_number: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    source_warehouse_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    destination_warehouse_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    dispatched_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    items: Mapped[list["WarehouseTransferItem"]] = relationship(
        back_populates="transfer", cascade="all, delete-orphan", lazy="joined"
    )
    source_warehouse: Mapped["Warehouse"] = relationship(foreign_keys=[source_warehouse_id], lazy="joined")  # noqa: F821
    destination_warehouse: Mapped["Warehouse"] = relationship(foreign_keys=[destination_warehouse_id], lazy="joined")  # noqa: F821


class WarehouseTransferItem(Base):
    __tablename__ = "warehouse_transfer_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    transfer_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("warehouse_transfers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    variant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("product_variants.id", ondelete="SET NULL"), nullable=True, index=True
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)

    transfer: Mapped["WarehouseTransfer"] = relationship(back_populates="items")
    product: Mapped["Product"] = relationship(foreign_keys=[product_id], lazy="joined")  # noqa: F821
    variant: Mapped["ProductVariant | None"] = relationship(foreign_keys=[variant_id], lazy="joined")  # noqa: F821
