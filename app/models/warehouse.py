import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Warehouse(Base):
    """A place a firm holds stock. Every firm gets a default one, so a caller that
    never mentions a warehouse still lands somewhere real."""

    __tablename__ = "warehouses"
    __table_args__ = (UniqueConstraint("organization_id", "code", name="uq_warehouse_org_code"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    code: Mapped[str] = mapped_column(String(30), nullable=False)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    contact_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Exactly one warehouse per firm carries this — see warehouse_service.
    is_default: Mapped[bool] = mapped_column(default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )


class WarehouseStock(Base):
    """What is physically on hand, per warehouse and per product/variant.

    `on_hand_quantity` only moves on a real goods movement — receiving a purchase,
    loading a vehicle, taking a return back in. Placing an order does not touch it;
    that creates a StockReservation instead, and

        available = on_hand − active reservations

    is what an order is validated against.
    """

    __tablename__ = "warehouse_stock"
    __table_args__ = (
        UniqueConstraint(
            "warehouse_id", "product_id", "variant_id", name="uq_warehouse_stock_item"
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
    on_hand_quantity: Mapped[float] = mapped_column(Float, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    warehouse: Mapped["Warehouse"] = relationship(lazy="joined")


# A reservation's life: held against an order, then either consumed when the goods
# are loaded onto a vehicle, or released when the order is cancelled or amended.
RESERVATION_STATUSES = ("active", "consumed", "released")


class StockReservation(Base):
    """Stock promised to an order but still sitting in the warehouse.

    A reservation is not a stock movement — nothing physical has happened. It exists
    so two orders cannot promise the same unit, and so cancelling an order is a
    matter of releasing the hold rather than inventing a stock-in that never
    occurred.
    """

    __tablename__ = "stock_reservations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    warehouse_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("warehouses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sales_orders.id", ondelete="CASCADE"), nullable=True, index=True
    )
    order_item_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sales_order_items.id", ondelete="CASCADE"), nullable=True, index=True
    )
    product_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    variant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("product_variants.id", ondelete="CASCADE"), nullable=True, index=True
    )
    reserved_quantity: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    # How much of this reservation has already been loaded out, so a partial
    # loading leaves the rest held.
    consumed_quantity: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    @property
    def outstanding_quantity(self) -> float:
        """Still held: reserved minus what has been loaded out."""
        if self.status != "active":
            return 0.0
        return round(max((self.reserved_quantity or 0) - (self.consumed_quantity or 0), 0), 3)
