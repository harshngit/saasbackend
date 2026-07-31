from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class SupplierOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    name: str
    contact_person: str | None
    phone: str | None
    email: str | None
    gst_number: str | None
    category: str | None
    address: str | None
    city: str | None
    opening_balance: float
    total_purchases: float
    total_paid: float
    outstanding_payable: float
    is_active: bool
    created_at: datetime
    updated_at: datetime


class SupplierCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    contact_person: str | None = Field(default=None, max_length=150)
    phone: str | None = Field(default=None, max_length=20)
    email: EmailStr | None = None
    gst_number: str | None = Field(default=None, max_length=20)
    category: str | None = Field(default=None, max_length=100)
    address: str | None = None
    city: str | None = Field(default=None, max_length=100)
    opening_balance: float = Field(default=0, ge=0)


class SupplierUpdate(BaseModel):
    """Full edit (PUT) — all fields optional so partial edits work too."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    contact_person: str | None = Field(default=None, max_length=150)
    phone: str | None = Field(default=None, max_length=20)
    email: EmailStr | None = None
    gst_number: str | None = Field(default=None, max_length=20)
    category: str | None = Field(default=None, max_length=100)
    address: str | None = None
    city: str | None = Field(default=None, max_length=100)
    opening_balance: float | None = Field(default=None, ge=0)


class SupplierStatusUpdate(BaseModel):
    is_active: bool


class PaymentCreate(BaseModel):
    amount: float = Field(gt=0)
    payment_mode: str = Field(default="cash", max_length=30)  # cash/upi/bank_transfer/cheque/other
    reference: str | None = Field(default=None, max_length=150)
    note: str | None = None
    paid_on: datetime | None = None


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    supplier_id: str
    amount: float
    payment_mode: str
    reference: str | None
    note: str | None
    paid_on: datetime
    created_at: datetime
