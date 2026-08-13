import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Float, ForeignKey, String, Text
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
    warehouse_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("warehouses.id", ondelete="SET NULL"), nullable=True, index=True
    )
    delivery_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivery_status: Mapped[str | None] = mapped_column(String(30), default="pending", nullable=True)

    # --- Who is taking it out, and how far it has got -------------------------
    # The Delivery is the record the whole flow turns on; its id is the identifier
    # every delivery endpoint takes. See app/core/workflow.py DELIVERY_STATUSES.
    delivery_partner_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    vehicle_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("vehicles.id", ondelete="SET NULL"), nullable=True, index=True
    )
    scheduled_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # planned | loaded | in_transit | partially_delivered | delivered | failed | cancelled
    status: Mapped[str] = mapped_column(String(30), default="planned", nullable=False, index=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dispatched_by_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Proof of delivery, captured on confirm. File ids from POST /files/upload.
    pod_photo_file_ids: Mapped[list | None] = mapped_column(JSON, default=list, nullable=True)
    pod_signature_file_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    items: Mapped[list["DeliveryItem"]] = relationship(
        back_populates="delivery", cascade="all, delete-orphan", lazy="joined"
    )

    @property
    def delivery_number(self) -> str:
        """The human code. Stored as `delivery_note_number` since the delivery note
        came first; the flow calls it the delivery / challan number."""
        return self.delivery_note_number

    @property
    def order_id(self) -> str | None:
        """`sales_order_id` under the name the flow uses."""
        return self.sales_order_id

    @property
    def planned_total(self) -> float:
        return round(sum(i.planned_quantity or 0 for i in self.items), 3)

    @property
    def loaded_total(self) -> float:
        return round(sum(i.loaded_quantity or 0 for i in self.items), 3)

    @property
    def delivered_total(self) -> float:
        return round(sum(i.delivered_quantity or 0 for i in self.items), 3)
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
    # The order line this fulfils, so planned / loaded / delivered can be compared
    # against what was ordered.
    order_item_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sales_order_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    planned_quantity: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    loaded_quantity: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    delivered_quantity: Mapped[float] = mapped_column(Float, default=0, nullable=False)

    delivery: Mapped["Delivery"] = relationship(back_populates="items")

    @property
    def pending_quantity(self) -> float:
        """Still to deliver out of what was planned."""
        return round(max((self.planned_quantity or 0) - (self.delivered_quantity or 0), 0), 3)
