"""Responses of GET /customers/{id}/payments/pending and /payments/collected.

Money is computed as SQL NUMERIC (2 decimals) and only turned into a JSON number at the
end, like every other amount this API returns.
"""

from datetime import date, datetime

from pydantic import BaseModel, Field


# ------------------------------------------------------------------------------ pending
class PendingInvoiceOut(BaseModel):
    invoice_id: str
    invoice_number: str
    order_id: str | None = None
    invoice_date: datetime
    due_date: datetime | None = Field(default=None, description="Null when the invoice has no due date")
    total: float = Field(description="Invoice total")
    amount_paid: float = Field(description="Paid / allocated against this invoice (payments)")
    credited_amount: float = Field(description="Credit notes issued against this invoice")
    outstanding: float = Field(description="total - amount_paid - credited_amount; always > 0 here")
    currency: str
    payment_status: str = Field(description="unpaid | partial")
    invoice_status: str = Field(description="The invoice's stored status")
    is_overdue: bool = Field(description="The due date (a calendar day in the organization's timezone) is before today")
    days_overdue: int = Field(description="Whole days past the due date; 0 when not overdue")
    awaiting_approval_amount: float = Field(
        default=0.0, description="Collected against this invoice by delivery partners, waiting for admin approval "
                                 "(not yet deducted from outstanding)")
    collectable_outstanding: float = Field(
        default=0.0, description="outstanding - awaiting_approval_amount (never below 0): what can still be collected "
                                 "on this invoice")


class PendingSummary(BaseModel):
    """Over every row matching the filters (not just this page)."""

    total_outstanding: float
    total_overdue: float
    pending_invoices: int
    overdue_invoices: int
    currency: str
    payment_terms: str | None = Field(default=None, description="The customer's payment terms (e.g. net_30)")
    as_of: date = Field(description="Today in the organization's timezone; overdue is computed against it")
    awaiting_approval_amount: float = Field(
        default=0.0, description="Collected by delivery partners from this customer, waiting for admin approval "
                                 "(whole customer, not filtered). Not yet deducted from outstanding")
    awaiting_approval_count: int = Field(default=0, description="Number of those collections")
    collectable_amount: float = Field(
        default=0.0, description="Customer's outstanding balance less awaiting_approval_amount: the most that can "
                                 "still be collected")


class PendingPaymentsOut(BaseModel):
    items: list[PendingInvoiceOut]
    total: int
    page: int
    page_size: int
    total_pages: int
    summary: PendingSummary


# ---------------------------------------------------------------------------- collected
class PaymentAllocationOut(BaseModel):
    invoice_id: str
    invoice_number: str | None = None
    amount: float


class PaymentSplitBrief(BaseModel):
    payment_mode: str
    amount: float
    reference: str | None = None


class CollectedPaymentOut(BaseModel):
    payment_id: str
    receipt_number: str | None = None
    payment_date: datetime = Field(description="When the money was received (received_on)")
    amount: float
    payment_mode: str
    reference: str | None = Field(default=None, description="Transaction / UTR / cheque reference")
    status: str = Field(description="`recorded` (a finalized payment). With payment_status=awaiting_approval: "
                                    "`awaiting_approval` (a partner's collection waiting for admin approval)")
    source: str = Field(description="direct | delivery_collection (a reconciled field / delivery collection)")
    order_id: str | None = None
    allocated_amount: float = Field(description="Applied to invoices")
    unallocated_amount: float = Field(description="Held on account (advance); amount - allocated_amount")
    allocations: list[PaymentAllocationOut]
    splits: list[PaymentSplitBrief] = Field(default_factory=list, description="Tender breakdown of a split payment")
    currency: str
    collection_id: str | None = Field(
        default=None, description="The delivery collection behind this row: set for awaiting_approval rows (then "
                                  "equal to payment_id); null for payments")
    collected_by: str | None = Field(default=None, description="Delivery partner's name, for awaiting_approval rows")


class CollectedSummary(BaseModel):
    """Over every row matching the filters (not just this page)."""

    total_collected: float
    payment_count: int
    total_allocated: float
    total_unallocated: float
    currency: str
    awaiting_approval_amount: float = Field(
        default=0.0, description="Collections waiting for admin approval for this customer (whole customer, not "
                                 "filtered); list them with payment_status=awaiting_approval")
    awaiting_approval_count: int = Field(default=0, description="Number of those collections")


class CollectedPaymentsOut(BaseModel):
    items: list[CollectedPaymentOut]
    total: int
    page: int
    page_size: int
    total_pages: int
    summary: CollectedSummary
