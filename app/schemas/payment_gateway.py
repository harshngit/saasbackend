"""Schemas for organization-specific Razorpay payment gateway configuration
and invoice payment links.
"""

from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, field_validator

REQUIRED_WEBHOOK_EVENTS: list[str] = [
    "payment_link.paid",
    "payment_link.partially_paid",
    "payment_link.expired",
    "payment_link.cancelled",
]


class PaymentGatewayIn(BaseModel):
    """Input payload to configure or update the organization's Razorpay gateway.

    Secrets are write-only. If omitted during an update, existing encrypted
    secrets are preserved.
    """

    key_id: str = Field(..., min_length=5, max_length=100)
    key_secret: str | None = Field(default=None, max_length=200)
    webhook_secret: str | None = Field(default=None, max_length=200)

    @field_validator("key_id")
    @classmethod
    def validate_key_id(cls, v: str) -> str:
        clean = v.strip()
        if not (clean.startswith("rzp_test_") or clean.startswith("rzp_live_")):
            raise ValueError("Razorpay Key ID must start with 'rzp_test_' or 'rzp_live_'")
        return clean


class PaymentGatewayOut(BaseModel):
    """Output schema for payment gateway configuration.

    Encrypted or plaintext secrets are NEVER returned here.
    """

    model_config = ConfigDict(from_attributes=True)

    key_id: str | None = None
    mode: str | None = None
    is_active: bool = False
    verified_at: datetime | None = None
    configured: bool = False
    webhook_url: str | None = None
    required_events: list[str] = Field(default_factory=lambda: list(REQUIRED_WEBHOOK_EVENTS))


class InvoicePaymentLinkCreate(BaseModel):
    """Request payload to generate a payment link for an invoice."""

    amount: float | None = Field(default=None, gt=0, description="Amount to bill. Defaults to invoice outstanding.")
    expire_in_days: int = Field(default=7, ge=1, le=30, description="Link expiry in days (1 to 30). Defaults to 7.")
    notify_sms: bool = Field(default=True, description="Whether Razorpay should send SMS notification to customer.")
    notify_email: bool = Field(default=True, description="Whether Razorpay should send Email notification to customer.")


class InvoicePaymentLinkOut(BaseModel):
    """Payment link detail returned to the client."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    invoice_id: str
    razorpay_link_id: str
    short_url: str
    amount: float
    amount_paid: float = 0.0
    currency: str = "INR"
    status: str
    expire_by: datetime | None = None
    notify_sms: bool = True
    notify_email: bool = True
    created_at: datetime
    paid_at: datetime | None = None
