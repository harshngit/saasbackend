"""Invoice payment link model.

Stores payment links generated via the organization's own Razorpay account.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InvoicePaymentLink(Base):
    """A Razorpay Payment Link generated for an invoice."""

    __tablename__ = "invoice_payment_links"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    invoice_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, index=True
    )

    razorpay_link_id: Mapped[str] = mapped_column(
        String(100), nullable=False, unique=True, index=True
    )
    short_url: Mapped[str] = mapped_column(String(255), nullable=False)

    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_paid_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(10), default="INR", nullable=False)

    # created | partially_paid | paid | expired | cancelled
    status: Mapped[str] = mapped_column(String(30), default="created", nullable=False, index=True)

    expire_by: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notify_sms: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notify_email: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_event: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Relationships
    organization = relationship("Organization", backref="payment_links")
    invoice = relationship("Invoice", backref="payment_links")
    created_by_user = relationship("User", foreign_keys=[created_by_user_id])
