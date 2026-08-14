import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SalesReturn(Base):
    """Goods coming back — a request first, a stock movement only once approved.

    The flow is: the customer asks (`requested`), the goods physically arrive
    (`received`), someone checks their condition and accepts them (`approved`). Only
    at approval do saleable goods go back on the shelf and a credit note go out.
    Damaged or expired goods are recorded, credited if the firm decides so, and never
    re-enter sellable stock.
    """

    __tablename__ = "sales_returns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    return_number: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    return_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    customer_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    invoice_reference_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True, index=True
    )
    return_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    return_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # requested -> received -> approved / rejected (see workflow.RETURN_STATUSES).
    return_status: Mapped[str | None] = mapped_column(String(30), default="requested", nullable=True)

    # Where the goods came back to, and when each step happened.
    warehouse_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("warehouses.id", ondelete="SET NULL"), nullable=True
    )
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    rejected_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The credit note raised on approval, and what it came to.
    credit_note_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True, index=True
    )
    credit_amount: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)

    items: Mapped[list["ReturnItem"]] = relationship(
        back_populates="sales_return", cascade="all, delete-orphan", lazy="joined"
    )
    customer: Mapped["Customer | None"] = relationship(lazy="joined", primaryjoin="SalesReturn.customer_id == Customer.id")  # noqa: F821
    invoice: Mapped["Invoice | None"] = relationship(lazy="joined", primaryjoin="SalesReturn.invoice_reference_id == Invoice.id")  # noqa: F821
    credit_note: Mapped["Invoice | None"] = relationship(lazy="joined", primaryjoin="SalesReturn.credit_note_id == Invoice.id")  # noqa: F821


class ReturnItem(Base):
    __tablename__ = "return_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    return_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sales_returns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    variant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("product_variants.id", ondelete="SET NULL"), nullable=True
    )
    product_name: Mapped[str] = mapped_column(String(200), nullable=False)
    quantity_returned: Mapped[float] = mapped_column(Float, nullable=False)

    # The invoice line this comes back off, so the credit is at the price it was
    # actually billed at rather than today's list price.
    invoice_item_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("invoice_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    unit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    tax_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    line_total: Mapped[float | None] = mapped_column(Float, nullable=True)

    # What the condition check found. `restock` is what the firm decided to do about
    # it; goods that are not saleable never go back into sellable stock whatever the
    # flag says.
    received_quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    condition: Mapped[str | None] = mapped_column(String(50), nullable=True)
    restock: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    restocked_quantity: Mapped[float | None] = mapped_column(Float, nullable=True)

    sales_return: Mapped["SalesReturn"] = relationship(back_populates="items")
