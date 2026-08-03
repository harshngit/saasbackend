import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PaymentReceipt(Base):
    __tablename__ = "payment_receipts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    receipt_number: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    receipt_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    customer_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    invoice_reference_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True, index=True
    )
    amount_received: Mapped[float] = mapped_column(Float, nullable=False)
    payment_method: Mapped[str | None] = mapped_column(String(30), nullable=True)
    payment_status: Mapped[str | None] = mapped_column(String(30), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    customer: Mapped["Customer | None"] = relationship(lazy="joined", primaryjoin="PaymentReceipt.customer_id == Customer.id")  # noqa: F821
    invoice: Mapped["Invoice | None"] = relationship(lazy="joined", primaryjoin="PaymentReceipt.invoice_reference_id == Invoice.id")  # noqa: F821
