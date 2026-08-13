"""The customer's account: what they were billed, what they paid, what is overdue."""

from datetime import datetime

from pydantic import BaseModel, Field


class LedgerSummary(BaseModel):
    total_billed: float = 0
    total_received: float = 0
    opening_balance: float = 0
    outstanding: float = 0
    credit_limit: float = 0
    available_credit: float = Field(
        default=0, description="Credit limit less what is outstanding, never below zero"
    )
    overdue_amount: float = Field(default=0, description="Of the outstanding, what is past its due date")


class LedgerAgeing(BaseModel):
    """Unpaid invoices bucketed by how long they have been outstanding.

    Age runs from the invoice's due date when it has one, and from its date when it
    does not — an invoice with no agreed terms is due on issue.
    """

    bucket_0_30: float = Field(default=0, alias="0_30")
    bucket_31_60: float = Field(default=0, alias="31_60")
    bucket_61_90: float = Field(default=0, alias="61_90")
    bucket_90_plus: float = Field(default=0, alias="90_plus")

    model_config = {"populate_by_name": True}


class LedgerTransaction(BaseModel):
    """One line of the account. A debit is what the customer owes, a credit what they
    have settled — an invoice debits, a payment or a credit note credits."""

    type: str = Field(description="invoice | payment | credit_note | opening_balance")
    reference_id: str | None = None
    reference_number: str | None = None
    date: datetime
    description: str | None = None
    debit: float = 0
    credit: float = 0
    balance: float = Field(default=0, description="The running balance after this line")
    due_date: datetime | None = None
    status: str | None = None


class CustomerLedger(BaseModel):
    customer_id: str
    customer_name: str | None = None
    summary: LedgerSummary
    ageing: LedgerAgeing
    transactions: list[LedgerTransaction]
