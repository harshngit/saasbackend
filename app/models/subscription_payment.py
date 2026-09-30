import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SubscriptionPayment(Base):
    """One Razorpay payment attempt for an organization's plan upgrade
    (Phase 1 — TEST MODE). A row is created as `status="created"` when the
    Razorpay order is created (POST /billing/razorpay/order), then moved to
    `"paid"` or `"failed"` by app.services.billing_service.activate_subscription
    / mark_failed — never by anything else. See app.routers.billing.
    """

    __tablename__ = "subscription_payments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    plan_id: Mapped[str] = mapped_column(String(36), ForeignKey("plans.id"), nullable=False)

    # "monthly" / "yearly" (BillingCycle value) — plain String + Python-side
    # enum validation, matching Organization.billing_cycle's own convention.
    billing_cycle: Mapped[str] = mapped_column(String(10), nullable=False)

    # Integer paise, never Float — this mirrors Razorpay's own API contract
    # (amounts are always integer paise), not a general ledger amount.
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="INR")

    razorpay_order_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    razorpay_payment_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )

    # "created" / "paid" / "failed" — plain String + Python-side validation,
    # same reasoning as billing_cycle above.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="created")
    failure_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_by_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # The raw Razorpay webhook event that last touched this row — for support
    # / debugging only. Never holds RAZORPAY_KEY_SECRET or RAZORPAY_WEBHOOK_SECRET;
    # those never appear anywhere in a Razorpay event payload in the first place.
    raw_event: Mapped[dict | None] = mapped_column(JSON, nullable=True)
