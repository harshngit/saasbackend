"""Organization payment gateway configuration model.

Stores per-organization Razorpay API credentials with encrypted secrets.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class OrgPaymentGateway(Base):
    """Payment gateway credentials belonging to one organization (one-to-one)."""

    __tablename__ = "org_payment_gateways"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    provider: Mapped[str] = mapped_column(String(30), default="razorpay", nullable=False)
    key_id: Mapped[str] = mapped_column(String(100), nullable=False)
    key_secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    webhook_secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)

    mode: Mapped[str] = mapped_column(String(10), nullable=False)  # "test" or "live"
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    organization = relationship("Organization", back_populates="payment_gateway")
