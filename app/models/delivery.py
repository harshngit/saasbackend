import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Delivery(Base):
    __tablename__ = "deliveries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    delivery_note_number: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    delivery_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    sales_order_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sales_orders.id", ondelete="SET NULL"), nullable=True, index=True
    )
    customer_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    warehouse: Mapped[str | None] = mapped_column(String(50), nullable=True)
    delivery_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivery_status: Mapped[str | None] = mapped_column(String(30), default="pending", nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    items: Mapped[list["DeliveryItem"]] = relationship(
        back_populates="delivery", cascade="all, delete-orphan", lazy="joined"
    )
    customer: Mapped["Customer | None"] = relationship(lazy="joined", primaryjoin="Delivery.customer_id == Customer.id")  # noqa: F821
    sales_order: Mapped["SalesOrder | None"] = relationship(lazy="joined", primaryjoin="Delivery.sales_order_id == SalesOrder.id")  # noqa: F821


class DeliveryItem(Base):
    __tablename__ = "delivery_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    delivery_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("deliveries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    variant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("product_variants.id", ondelete="SET NULL"), nullable=True
    )
    product_name: Mapped[str] = mapped_column(String(200), nullable=False)
    delivered_quantity: Mapped[float] = mapped_column(Float, nullable=False)

    delivery: Mapped["Delivery"] = relationship(back_populates="items")
