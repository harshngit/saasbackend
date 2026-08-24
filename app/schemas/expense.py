from datetime import datetime
from typing import Literal

from app.schemas.choices import ApprovalStatus, ExpensePaymentStatus, ExpenseStatus
from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator


class ExpenseItemIn(BaseModel):
    description: str = Field(min_length=1, max_length=255)
    quantity: float = Field(default=1.0, gt=0, description="Quantity of items/service")
    unit_price: float = Field(default=0.0, ge=0, description="Cost per unit")
    tax_rate: float = Field(default=0.0, ge=0, le=100, description="Applicable tax %")


class ExpenseItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    expense_id: str
    description: str
    quantity: float
    unit_price: float
    tax_rate: float
    tax_amount: float
    line_total: float


class SupplierBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    contact_person: str | None = None
    phone: str | None = None
    email: str | None = None
    gst_number: str | None = None


class ExpenseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    category: str
    amount: float
    subtotal: float = 0.0
    tax_rate: float | None = None
    tax_amount: float = 0.0
    currency: str = "INR"
    description: str | None = None
    expense_date: datetime
    payment_mode: str | None = None
    receipt_url: str | None = None
    vendor_invoice_url: str | None = None
    supporting_documents: list[dict | str] | None = None
    status: str
    submitted_by: str | None = None
    approved_by: str | None = None
    reject_reason: str | None = None
    created_at: datetime
    updated_at: datetime

    # Basic Information (Ext)
    expense_id: str | None = None
    expense_number: str | None = None
    expense_type: str | None = None
    expense_status: str | None = None
    financial_year: str | None = None
    branch_id: str | None = None
    department_id: str | None = None

    # Payee Details
    vendor_id: str | None = None
    vendor: SupplierBrief | None = None
    payee_name: str | None = None
    contact_person: str | None = None
    mobile_number: str | None = None
    email_address: str | None = None
    payee_gstin: str | None = None

    # Payment Details
    payment_reference: str | None = None
    payment_status: str | None = None
    approval_status: str | None = None
    paid_from_account_id: str | None = None
    expense_account_id: str | None = None

    # Accounting
    cost_center_id: str | None = None
    project_id: str | None = None
    tax_category: str | None = None
    tds_applicable: bool = False
    tds_amount: float = 0.0

    # Tags
    tags: list[str] | None = None

    # Recurring
    is_recurring: bool = False
    recurrence_frequency: str | None = None
    next_due_date: datetime | None = None

    # Line Items
    items: list[ExpenseItemOut] = []

    @computed_field(description="Net payable amount after TDS deduction")
    @property
    def net_payable(self) -> float:
        return round(max((self.amount or 0.0) - (self.tds_amount or 0.0), 0.0), 2)


class ExpenseCreate(BaseModel):
    category: str = Field(min_length=1, max_length=100)
    amount: float | None = Field(default=None, gt=0, description="Total amount. Computed from items if provided")
    description: str | None = None
    expense_date: datetime | None = None
    payment_mode: str | None = Field(default=None, max_length=30)
    receipt_url: str | None = None
    vendor_invoice_url: str | None = None
    supporting_documents: list[dict | str] | None = None
    currency: str = Field(default="INR", max_length=10)

    # Basic Information (Ext)
    expense_type: str | None = Field(default=None, max_length=50, description="Operational, Capital, Reimbursable, Petty Cash, …")
    expense_status: ExpenseStatus | None = Field(default=None, description="Draft | Submitted | Approved | Rejected | Paid")
    financial_year: str | None = Field(default=None, max_length=20)
    branch_id: str | None = Field(default=None, max_length=50)
    department_id: str | None = Field(default=None, max_length=50)

    # Payee Details
    vendor_id: str | None = Field(default=None, description="Existing supplier reference")
    payee_name: str | None = Field(default=None, max_length=200)
    contact_person: str | None = Field(default=None, max_length=150)
    mobile_number: str | None = Field(default=None, max_length=20)
    email_address: str | None = Field(default=None, max_length=255)
    payee_gstin: str | None = Field(default=None, max_length=20)

    # Taxes & Totals
    tax_rate: float | None = Field(default=None, ge=0, le=100)
    tax_amount: float | None = Field(default=None, ge=0)

    # Payment Details
    payment_reference: str | None = Field(default=None, max_length=150, description="UTR / Cheque No / Txn ID")
    payment_status: ExpensePaymentStatus | None = Field(default=None, description="Pending | Partially Paid | Paid")
    approval_status: ApprovalStatus | None = Field(default=None, description="Pending | Approved | Rejected")
    paid_from_account_id: str | None = Field(default=None, description="Cash/bank account the payment came from")
    expense_account_id: str | None = Field(default=None, description="Ledger account to post against")

    # Accounting
    cost_center_id: str | None = Field(default=None, max_length=50)
    project_id: str | None = Field(default=None, max_length=50)
    tax_category: str | None = Field(default=None, max_length=50)
    tds_applicable: bool = False
    tds_amount: float = Field(default=0.0, ge=0)

    # Tags
    tags: list[str] | None = None

    # Recurring
    is_recurring: bool = False
    recurrence_frequency: Literal["daily", "weekly", "monthly", "yearly"] | None = None
    next_due_date: datetime | None = None

    # Itemized Breakdown
    items: list[ExpenseItemIn] | None = None

    @model_validator(mode="after")
    def validate_amount_and_items(self) -> "ExpenseCreate":
        if not self.amount and not self.items:
            raise ValueError("Either amount or itemized items must be provided")
        if self.is_recurring and not self.recurrence_frequency:
            raise ValueError("recurrence_frequency is required when is_recurring is True")
        return self


class ExpenseUpdate(BaseModel):
    category: str | None = Field(default=None, max_length=100)
    amount: float | None = Field(default=None, gt=0)
    description: str | None = None
    expense_date: datetime | None = None
    payment_mode: str | None = Field(default=None, max_length=30)
    receipt_url: str | None = None
    vendor_invoice_url: str | None = None
    supporting_documents: list[dict | str] | None = None
    currency: str | None = Field(default=None, max_length=10)

    # Basic Information (Ext)
    expense_type: str | None = Field(default=None, max_length=50)
    expense_status: ExpenseStatus | None = None
    financial_year: str | None = Field(default=None, max_length=20)
    branch_id: str | None = Field(default=None, max_length=50)
    department_id: str | None = Field(default=None, max_length=50)

    # Payee Details
    vendor_id: str | None = None
    payee_name: str | None = Field(default=None, max_length=200)
    contact_person: str | None = Field(default=None, max_length=150)
    mobile_number: str | None = Field(default=None, max_length=20)
    email_address: str | None = Field(default=None, max_length=255)
    payee_gstin: str | None = Field(default=None, max_length=20)

    # Taxes & Totals
    tax_rate: float | None = Field(default=None, ge=0, le=100)
    tax_amount: float | None = Field(default=None, ge=0)

    # Payment Details
    payment_reference: str | None = Field(default=None, max_length=150)
    payment_status: ExpensePaymentStatus | None = None
    approval_status: ApprovalStatus | None = None
    paid_from_account_id: str | None = None
    expense_account_id: str | None = None

    # Accounting
    cost_center_id: str | None = Field(default=None, max_length=50)
    project_id: str | None = Field(default=None, max_length=50)
    tax_category: str | None = Field(default=None, max_length=50)
    tds_applicable: bool | None = None
    tds_amount: float | None = Field(default=None, ge=0)

    # Tags
    tags: list[str] | None = None

    # Recurring
    is_recurring: bool | None = None
    recurrence_frequency: Literal["daily", "weekly", "monthly", "yearly"] | None = None
    next_due_date: datetime | None = None

    # Itemized Breakdown
    items: list[ExpenseItemIn] | None = None


class RejectBody(BaseModel):
    reason: str | None = Field(default=None, max_length=500)
