import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SupplierPaymentAllocation(Base):
    """An allocation of a Supplier Payment against a specific Supplier Invoice."""

    __tablename__ = "supplier_payment_allocations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    supplier_payment_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("supplier_payments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    supplier_invoice_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("supplier_invoices.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    amount: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    supplier_payment: Mapped["SupplierPayment"] = relationship(  # noqa: F821
        back_populates="allocations"
    )
    supplier_invoice: Mapped["SupplierInvoice"] = relationship(lazy="joined")  # noqa: F821
