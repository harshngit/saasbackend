import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class VehicleLoading(Base):
    __tablename__ = "vehicle_loadings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    delivery_partner_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    vehicle_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("vehicles.id", ondelete="SET NULL"), nullable=True, index=True
    )
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    reconciliations: Mapped[list["VehicleStockReconciliation"]] = relationship(
        back_populates="loading", cascade="all, delete-orphan", lazy="selectin"
    )
    items: Mapped[list["VehicleLoadingItem"]] = relationship(
        back_populates="loading", cascade="all, delete-orphan", lazy="joined"
    )
    delivery_partner: Mapped["User"] = relationship(foreign_keys=[delivery_partner_id], lazy="joined")  # noqa: F821
    vehicle: Mapped["Vehicle | None"] = relationship(foreign_keys=[vehicle_id], lazy="joined")  # noqa: F821


class VehicleLoadingItem(Base):
    __tablename__ = "vehicle_loading_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    loading_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("vehicle_loadings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    variant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("product_variants.id", ondelete="SET NULL"), nullable=True
    )
    product_name: Mapped[str] = mapped_column(String(200), nullable=False)
    loaded_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    extra_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    returned_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    delivered_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    loading: Mapped["VehicleLoading"] = relationship(back_populates="items")

    @property
    def remaining_qty(self) -> int:
        """Stock remaining on vehicle before return (max(0, loaded + extra - delivered))."""
        return max((self.loaded_qty or 0) + (self.extra_qty or 0) - (self.delivered_qty or 0), 0)

    @property
    def expected_closing_qty(self) -> int:
        """Expected closing stock after return (max(0, loaded + extra - delivered - returned))."""
        return max((self.loaded_qty or 0) + (self.extra_qty or 0) - (self.delivered_qty or 0) - (self.returned_qty or 0), 0)


class VehicleStockReconciliation(Base):
    __tablename__ = "vehicle_stock_reconciliations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    loading_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("vehicle_loadings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reconciled_by_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(30), default="reconciled", nullable=False)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    loading: Mapped["VehicleLoading"] = relationship(back_populates="reconciliations")
    reconciled_by: Mapped["User"] = relationship(foreign_keys=[reconciled_by_id], lazy="joined")  # noqa: F821
    items: Mapped[list["VehicleReconciliationItem"]] = relationship(
        back_populates="reconciliation", cascade="all, delete-orphan", lazy="joined"
    )


class VehicleReconciliationItem(Base):
    __tablename__ = "vehicle_reconciliation_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    reconciliation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("vehicle_stock_reconciliations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    loading_item_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("vehicle_loading_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    product_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    variant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("product_variants.id", ondelete="SET NULL"), nullable=True
    )
    product_name: Mapped[str] = mapped_column(String(200), nullable=False)
    loaded_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    extra_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    delivered_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    returned_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    expected_closing_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    physical_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    variance_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    notes: Mapped[str | None] = mapped_column(String(300), nullable=True)

    reconciliation: Mapped["VehicleStockReconciliation"] = relationship(back_populates="items")

