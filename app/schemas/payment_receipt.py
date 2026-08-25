from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


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

    @model_validator(mode="after")
    def _populate_aliases(self) -> "PaymentReceiptOut":
        if self.amount_collected is None:
            self.amount_collected = self.amount_received
        return self
