from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ReceiptCustomerBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    business_name: str | None = None


class ReceiptInvoiceBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    invoice_number: str
    total: float


class PaymentSplitIn(BaseModel):
    payment_mode: str = Field(max_length=30, description="cash | upi | card | bank_transfer | cod | …")
    amount: float = Field(gt=0)
    reference: str | None = Field(default=None, max_length=150, description="UTR, UPI ref, cheque no")
    upi_id: str | None = Field(default=None, max_length=100, description="Payer's UPI id")
    card_type: str | None = Field(default=None, max_length=30, description="e.g. Visa, Mastercard")
    card_last_four: str | None = Field(
        default=None, max_length=4, description="Last 4 digits only — never full card number"
    )
    collection_instructions: str | None = Field(default=None, description="Where/how to collect")

    @field_validator("card_last_four")
    @classmethod
    def _valid_card_last_four(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not (v.isdigit() and len(v) == 4):
            raise ValueError("card_last_four must be exactly 4 digits")
        return v


class PaymentSplitOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    payment_mode: str
    amount: float
    reference: str | None = None
    upi_id: str | None = None
    card_type: str | None = None
    card_last_four: str | None = None
    collection_instructions: str | None = None
    created_at: datetime | None = None


class PaymentReceiptCreate(BaseModel):
    """Record money received.

    Name the invoice in `invoice_reference_id` and everything else about the payer is
    derived from it — there is no need to send the customer again. That one call moves
    the invoice's paid figure and status, the customer's received and outstanding
    totals and the customer ledger, in a single transaction.

    Send no invoice and the money is an advance held on account, which only moves the
    customer's balance. More than an invoice still owes is refused: an over-payment is
    almost always a typo, and money genuinely held on account belongs on an advance.
    """

    invoice_reference_id: str | None = Field(
        default=None, description="The invoice this settles. The customer is derived from it"
    )
    customer_id: str | None = Field(
        default=None, description="Only needed for an advance with no invoice behind it"
    )
    amount_received: float = Field(gt=0)
    payment_method: str | None = Field(default=None, max_length=30, description="cash | upi | bank_transfer | …")
    transaction_reference: str | None = Field(default=None, max_length=150, description="UTR, UPI ref, cheque no")
    receipt_date: datetime | None = None
    receipt_number: str | None = Field(default=None, description="Auto-generated when omitted")
    note: str | None = None

    # Optional split payments breakdown
    splits: list[PaymentSplitIn] | None = Field(
        default=None, description="Optional multi-tender split payment breakdown"
    )

    # Method-specific details — which ones are relevant depends on payment_method
    # (cash uses none of them). Never send the full card number or CVV.
    upi_id: str | None = Field(default=None, max_length=100, description="Payer's UPI id, for payment_method='upi'")
    card_type: str | None = Field(default=None, max_length=30, description="e.g. Visa, Mastercard — for payment_method='card'")
    card_last_four: str | None = Field(
        default=None, max_length=4, description="Last 4 digits only — never the full card number"
    )
    collection_instructions: str | None = Field(
        default=None, description="For payment_method='cod' — where/how to collect"
    )

    @field_validator("card_last_four")
    @classmethod
    def _valid_card_last_four(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not (v.isdigit() and len(v) == 4):
            raise ValueError("card_last_four must be exactly 4 digits")
        return v


class PaymentReceiptUpdate(BaseModel):
    """Correct the details of a receipt without touching the money.

    A wrong amount is not an edit — void the receipt with DELETE and record it again,
    so the invoice and the ledger are restated together.
    """

    payment_method: str | None = Field(default=None, max_length=30)
    transaction_reference: str | None = Field(default=None, max_length=150)
    receipt_date: datetime | None = None
    receipt_number: str | None = None
    note: str | None = None
    upi_id: str | None = Field(default=None, max_length=100)
    card_type: str | None = Field(default=None, max_length=30)
    card_last_four: str | None = Field(default=None, max_length=4)
    collection_instructions: str | None = None

    @field_validator("card_last_four")
    @classmethod
    def _valid_card_last_four(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not (v.isdigit() and len(v) == 4):
            raise ValueError("card_last_four must be exactly 4 digits")
        return v


class PaymentReceiptOut(BaseModel):
    id: str
    organization_id: str
    receipt_number: str | None
    receipt_date: datetime
    customer_id: str | None
    invoice_reference_id: str | None
    invoice_id: str | None = Field(default=None, description="Same as invoice_reference_id")
    amount_received: float
    amount_collected: float | None = None
    payment_method: str | None
    transaction_reference: str | None
    note: str | None = None

    # Method-specific details, persisted alongside the payment.
    upi_id: str | None = None
    card_type: str | None = None
    card_last_four: str | None = None
    collection_instructions: str | None = None

    # Where the invoice stands after this receipt.
    invoice_total: float | None = None
    total_paid: float | None = None
    outstanding_amount: float | None = None
    payment_status: str | None = Field(
        default=None, description="The invoice's status after this payment: unpaid | partial | paid"
    )

    # Snapshot foundation fields
    order_amount: float | None = None
    previous_pending: float | None = None
    remaining_receivable: float | None = None

    created_at: datetime
    customer: ReceiptCustomerBrief | None = None
    invoice: ReceiptInvoiceBrief | None = None
    splits: list[PaymentSplitOut] = Field(default_factory=list)

    @model_validator(mode="after")
    def _populate_aliases(self) -> "PaymentReceiptOut":
        if self.amount_collected is None:
            self.amount_collected = self.amount_received
        return self
