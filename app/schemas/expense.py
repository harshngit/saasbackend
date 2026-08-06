from datetime import datetime

from app.schemas.choices import ApprovalStatus, ExpensePaymentStatus, ExpenseStatus
from pydantic import BaseModel, ConfigDict, Field


class ExpenseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    category: str
    amount: float
    description: str | None
    expense_date: datetime
    payment_mode: str | None
    receipt_url: str | None
    status: str
    submitted_by: str | None
    approved_by: str | None
    reject_reason: str | None
    created_at: datetime
    updated_at: datetime

    # Sheet fields
    expense_id: str | None = None
    expense_number: str | None = None
    expense_type: str | None = None
    expense_status: str | None = None
    financial_year: str | None = None
    payment_status: str | None = None
    approval_status: str | None = None
    paid_from_account_id: str | None = None
    expense_account_id: str | None = None


class ExpenseCreate(BaseModel):
    category: str = Field(min_length=1, max_length=100)
    amount: float = Field(gt=0)
    description: str | None = None
    expense_date: datetime | None = None
    payment_mode: str | None = Field(default=None, max_length=30)
    receipt_url: str | None = None

    # Sheet fields (expense_id / expense_number are auto-generated)
    expense_type: str | None = Field(default=None, max_length=50, description="Operational, Capital, Reimbursable, Petty Cash, …")
    expense_status: ExpenseStatus | None = Field(default=None, description="Draft | Submitted | Approved | Rejected | Paid")
    financial_year: str | None = Field(default=None, max_length=20)
    payment_status: ExpensePaymentStatus | None = Field(default=None, description="Pending | Partially Paid | Paid")
    approval_status: ApprovalStatus | None = Field(default=None, description="Pending | Approved | Rejected")
    paid_from_account_id: str | None = Field(default=None, description="Cash/bank account the payment came from")
    expense_account_id: str | None = Field(default=None, description="Ledger account to post against")


class ExpenseUpdate(BaseModel):
    category: str | None = Field(default=None, max_length=100)
    amount: float | None = Field(default=None, gt=0)
    description: str | None = None
    expense_date: datetime | None = None
    payment_mode: str | None = Field(default=None, max_length=30)
    receipt_url: str | None = None

    # Sheet fields (expense_id / expense_number are auto-generated)
    expense_type: str | None = Field(default=None, max_length=50, description="Operational, Capital, Reimbursable, Petty Cash, …")
    expense_status: ExpenseStatus | None = Field(default=None, description="Draft | Submitted | Approved | Rejected | Paid")
    financial_year: str | None = Field(default=None, max_length=20)
    payment_status: ExpensePaymentStatus | None = Field(default=None, description="Pending | Partially Paid | Paid")
    approval_status: ApprovalStatus | None = Field(default=None, description="Pending | Approved | Rejected")
    paid_from_account_id: str | None = Field(default=None, description="Cash/bank account the payment came from")
    expense_account_id: str | None = Field(default=None, description="Ledger account to post against")


class RejectBody(BaseModel):
    reason: str | None = Field(default=None, max_length=500)
