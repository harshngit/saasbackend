"""Razorpay Phase 1 — SaaS plan payments (TEST MODE). Additive to, never a
replacement for, the manual Super Admin upgrade-approval flow
(app.schemas.organization.UpgradeRequest / RejectUpgrade).
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import BillingCycle


class RazorpayOrderRequest(BaseModel):
    plan_id: str
    billing_cycle: BillingCycle


class RazorpayPrefill(BaseModel):
    name: str | None = None
    email: str | None = None
    contact: str | None = None


class RazorpayOrderResponse(BaseModel):
    """Only the public key id is ever returned here — never
    RAZORPAY_KEY_SECRET or RAZORPAY_WEBHOOK_SECRET."""

    order_id: str
    amount: int = Field(description="Integer paise, computed server-side from the plan's price — never client-supplied")
    currency: str = "INR"
    key_id: str
    plan_name: str
    billing_cycle: BillingCycle
    prefill: RazorpayPrefill


class RazorpayVerifyRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


class SubscriptionPaymentOut(BaseModel):
    """One row of GET /billing/payments or GET /superadmin/subscription-payments.
    Deliberately excludes raw_event — no reason for a normal response to carry
    the raw webhook payload."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    plan_id: str
    plan_name: str | None = None
    billing_cycle: BillingCycle
    amount_paise: int
    currency: str
    razorpay_order_id: str
    razorpay_payment_id: str | None = None
    status: str
    failure_reason: str | None = None
    created_at: datetime
    paid_at: datetime | None = None
