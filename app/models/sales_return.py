import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SalesReturn(Base):
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
    return_status: Mapped[str | None] = mapped_column(String(30), default="requested", nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    items: Mapped[list["ReturnItem"]] = relationship(
        back_populates="sales_return", cascade="all, delete-orphan", lazy="joined"
    )
    customer: Mapped["Customer | None"] = relationship(lazy="joined", primaryjoin="SalesReturn.customer_id == Customer.id")  # noqa: F821
    invoice: Mapped["Invoice | None"] = relationship(lazy="joined", primaryjoin="SalesReturn.invoice_reference_id == Invoice.id")  # noqa: F821


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

    sales_return: Mapped["SalesReturn"] = relationship(back_populates="items")
