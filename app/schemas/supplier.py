from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class SupplierOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    name: str
    company_name: str | None = None
    contact_person: str | None = None
    phone: str | None = None
    email: str | None = None
    gst_number: str | None = None
    pan_number: str | None = None
    category: str | None = None
    supplier_type: str | None = None
    payment_terms: str | None = None
    credit_limit: float | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    pincode: str | None = None
    country: str | None = None
    notes: str | None = None
    opening_balance: float
    total_purchases: float
    total_paid: float
    outstanding_payable: float
    is_active: bool
    created_at: datetime
    updated_at: datetime


class SupplierCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    company_name: str | None = Field(default=None, max_length=200)
    contact_person: str | None = Field(default=None, max_length=150)
    phone: str | None = Field(default=None, max_length=20)
    email: EmailStr | None = None
    gst_number: str | None = Field(default=None, max_length=20)
    pan_number: str | None = Field(default=None, max_length=20)
    category: str | None = Field(default=None, max_length=100)
    supplier_type: str | None = Field(default=None, max_length=100)
    payment_terms: str | None = Field(default=None, max_length=100)
    credit_limit: float | None = Field(default=None, ge=0)
    address: str | None = None
    city: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=100)
    pincode: str | None = Field(default=None, max_length=20)
    country: str | None = Field(default=None, max_length=100)
    notes: str | None = None
    opening_balance: float = Field(default=0, ge=0)


class SupplierUpdate(BaseModel):
    """Full edit (PUT) — all fields optional so partial edits work too."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    company_name: str | None = Field(default=None, max_length=200)
    contact_person: str | None = Field(default=None, max_length=150)
    phone: str | None = Field(default=None, max_length=20)
    email: EmailStr | None = None
    gst_number: str | None = Field(default=None, max_length=20)
    pan_number: str | None = Field(default=None, max_length=20)
    category: str | None = Field(default=None, max_length=100)
    supplier_type: str | None = Field(default=None, max_length=100)
    payment_terms: str | None = Field(default=None, max_length=100)
    credit_limit: float | None = Field(default=None, ge=0)
    address: str | None = None
    city: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=100)
    pincode: str | None = Field(default=None, max_length=20)
    country: str | None = Field(default=None, max_length=100)
    notes: str | None = None
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


class SupplierProductLinkCreate(BaseModel):
    product_id: str


class SupplierProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    supplier_id: str
    product_id: str
    created_at: datetime
    product_name: str | None = None
    product_sku: str | None = None
    product_category_id: str | None = None
    product_status: str | None = None

