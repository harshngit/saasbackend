from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field


class PaymentAllocationIn(BaseModel):
    supplier_invoice_id: str
    amount: float = Field(gt=0, description="Amount allocated to this supplier invoice")


class SupplierPaymentCreate(BaseModel):
    supplier_id: str
    payment_date: datetime | None = None
    amount: float = Field(gt=0, description="Total payment amount")
    payment_method: str = Field(default="cash", description="Payment method: cash, upi, bank_transfer, cheque, card, other")
    reference: str | None = Field(default=None, max_length=150)
    notes: str | None = None
    allocations: list[PaymentAllocationIn] = Field(default_factory=list)


class SupplierPaymentVoidIn(BaseModel):
    reason: str = Field(min_length=1, description="Reason for voiding the payment")


class PaymentAllocationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    supplier_payment_id: str
    supplier_invoice_id: str
    supplier_invoice_number: str | None = None
    invoice_grand_total: float | None = None
    amount: float
    created_at: datetime


class SupplierPaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    payment_number: str | None = None
    supplier_id: str
    supplier_name: str | None = None
    payment_date: datetime
    amount: float
    payment_method: str
    payment_mode: str
    reference: str | None = None
    notes: str | None = None
    status: str
    allocated_amount: float
    unallocated_amount: float
    created_by: str | None = None
    voided_by: str | None = None
    voided_at: datetime | None = None
    void_reason: str | None = None
    created_at: datetime
    allocations: list[PaymentAllocationOut] = Field(default_factory=list)


class InvoicePaymentAllocationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    allocation_id: str
    supplier_payment_id: str
    payment_number: str | None = None
    payment_date: datetime
    payment_method: str
    payment_status: str
    reference: str | None = None
    allocated_amount: float
    created_at: datetime


class SupplierPaymentListResponse(BaseModel):
    items: list[SupplierPaymentOut]
    total: int
    page: int | None = None
    page_size: int | None = None
