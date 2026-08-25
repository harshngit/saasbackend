import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

# Order lifecycle statuses.
ORDER_STATUSES = {
    "pending",              # created, awaiting approval
    "confirmed",            # approved; stock deducted
    "rejected",             # approval declined
    "processing",           # being prepared
    "out_for_delivery",     # assigned to a delivery partner
    "delivered",
    "partially_delivered",
    "cancelled",
    "returned",
}


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SalesOrder(Base):
    __tablename__ = "sales_orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_number: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    sales_order_number: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    order_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    salesperson_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    order_status: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)

    customer_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # The order's own lifecycle: draft | placed | awaiting_approval | processing |
    # completed | cancelled. See app/core/workflow.py — rows written before the split
    # are migrated from the old single-status vocabulary on startup.
    status: Mapped[str] = mapped_column(String(30), default="placed", nullable=False, index=True)
    # How far the goods have got, independently of the order's status:
    # not_started | reserved | planned | loaded | in_transit | partially_delivered |
    # delivered | failed. Payment and invoicing are deliberately not in either — a
    # delivered, invoiced, unpaid order is normal for a credit customer.
    fulfilment_status: Mapped[str | None] = mapped_column(
        String(30), default="not_started", nullable=True, index=True
    )
    # Which warehouse the goods are promised out of.
    warehouse_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("warehouses.id", ondelete="SET NULL"), nullable=True, index=True
    )
    delivery_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fulfilment_method: Mapped[str | None] = mapped_column(String(30), nullable=True)
    pickup_status: Mapped[str | None] = mapped_column(String(30), default="not_started", nullable=True)
    collected_by: Mapped[str | None] = mapped_column(String(150), nullable=True)
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pickup_notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    payment_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    payment_terms_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quotation_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("quotations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # "office" (warehouse stock) or "delivery_vehicle" (field sale — vehicle stock, module pending)
    source: Mapped[str] = mapped_column(String(30), default="office", nullable=False)

    assigned_delivery_partner_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)

    subtotal: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    discount: Mapped[float] = mapped_column(Float, default=0, nullable=False)   # order-level
    tax: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    total: Mapped[float] = mapped_column(Float, default=0, nullable=False)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    billing_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    shipping_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivery_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    payment_terms: Mapped[str | None] = mapped_column(String(200), nullable=True)
    delivery_terms: Mapped[str | None] = mapped_column(String(200), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(10), default="INR", nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Whether stock has been deducted (on approval) — so cancel can restore it exactly once.
    stock_deducted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    items: Mapped[list["SalesOrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="joined"
    )
    customer: Mapped["Customer | None"] = relationship(lazy="joined")  # noqa: F821
    deliveries: Mapped[list["Delivery"]] = relationship(  # noqa: F821
        lazy="select", primaryjoin="SalesOrder.id == Delivery.sales_order_id", overlaps="sales_order"
    )
    invoices: Mapped[list["Invoice"]] = relationship(  # noqa: F821
        lazy="select", primaryjoin="SalesOrder.id == Invoice.order_id", overlaps="order"
    )

    @property
    def delivery_id(self) -> str | None:
        if self.deliveries:
            active = [d for d in self.deliveries if d.status != "cancelled"]
            if active:
                return active[-1].id
        return None

    @property
    def delivery_number(self) -> str | None:
        if self.deliveries:
            active = [d for d in self.deliveries if d.status != "cancelled"]
            if active:
                return active[-1].delivery_note_number
        return None

    @property
    def invoice_id(self) -> str | None:
        if self.invoices:
            valid = [i for i in self.invoices if not i.is_credit_note]
            if valid:
                return valid[-1].id
        return None

    @property
    def invoice_number(self) -> str | None:
        if self.invoices:
            valid = [i for i in self.invoices if not i.is_credit_note]
            if valid:
                return valid[-1].invoice_number
        return None


class SalesOrderItem(Base):
    __tablename__ = "sales_order_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    order_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sales_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    variant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("product_variants.id", ondelete="SET NULL"), nullable=True
    )
    product_name: Mapped[str] = mapped_column(String(200), nullable=False)  # snapshot
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    discount: Mapped[float] = mapped_column(Float, default=0, nullable=False)  # line-level
    discount_percent: Mapped[float | None] = mapped_column(Float, default=0, nullable=True)
    cost_price: Mapped[float | None] = mapped_column(Float, default=0, nullable=True)
    line_total: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    uom: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # Snapshot of the tax the line was sold at, so an invoice raised later bills the
    # agreed rate rather than a hardcoded one.
    tax_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    tax_amount: Mapped[float | None] = mapped_column(Float, default=0, nullable=True)
    # How much of this line the warehouse is holding, and how much has gone out.
    reserved_quantity: Mapped[float | None] = mapped_column(Float, default=0, nullable=True)
    delivered_quantity: Mapped[float | None] = mapped_column(Float, default=0, nullable=True)
    # The lot and the units the customer asked for, if they asked for particular ones.
    # A request, not a hold: which lot actually goes out is settled at loading.
    batch_number: Mapped[str | None] = mapped_column(String(60), nullable=True)
    order: Mapped["SalesOrder"] = relationship(back_populates="items")

    @property
    def ordered_quantity(self) -> int:
        return self.quantity

    @property
    def remaining_quantity(self) -> float:
        return round(max((self.quantity or 0) - (self.delivered_quantity or 0), 0), 3)
