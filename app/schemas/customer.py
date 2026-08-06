from datetime import datetime

from app.schemas.choices import CustomerStatus
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class AssigneeBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str


class CustomerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    name: str
    business_name: str | None
    phone: str | None
    email: str | None
    gst_number: str | None
    billing_address: str | None
    delivery_address: str | None
    assigned_sales_officer_id: str | None
    assigned_sales_officer: AssigneeBrief | None
    credit_limit: float
    opening_balance: float
    total_billed: float
    total_received: float
    outstanding_balance: float
    category: str | None
    notes: str | None
    is_active: bool

    # Customer profile. These columns existed but were never exposed, so
    # `customer_id` looked absent to every caller.
    customer_id: str | None = None
    customer_since: datetime | None = None
    status: str | None = None
    primary_contact_person: str | None = None
    maps_latitude: float | None = None
    maps_longitude: float | None = None

    created_at: datetime
    updated_at: datetime


class CustomerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    business_name: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=20)
    email: EmailStr | None = None
    gst_number: str | None = Field(default=None, max_length=20)
    billing_address: str | None = None
    delivery_address: str | None = None
    assigned_sales_officer_id: str | None = None
    credit_limit: float = Field(default=0, ge=0)
    opening_balance: float = Field(default=0, ge=0)  # prior dues, if any
    category: str | None = Field(default=None, max_length=100)
    notes: str | None = None

    @field_validator("assigned_sales_officer_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v

    # Customer profile (customer_id is auto-generated, so not settable)
    customer_since: datetime | None = None
    status: CustomerStatus | None = Field(default=None, description="Active | Inactive | Blacklisted | Prospect")
    primary_contact_person: str | None = Field(default=None, max_length=150)
    maps_latitude: float | None = Field(default=None, ge=-90, le=90)
    maps_longitude: float | None = Field(default=None, ge=-180, le=180)


class CustomerPaymentCreate(BaseModel):
    amount: float = Field(gt=0)
    payment_mode: str = Field(default="cash", max_length=30)
    reference: str | None = Field(default=None, max_length=150)
    note: str | None = None
    order_id: str | None = None
    invoice_id: str | None = Field(
        default=None,
        description="Settle this invoice. Omit for an advance / on-account payment.",
    )
    received_on: datetime | None = None


class PaymentInvoiceBrief(BaseModel):
    """The invoice a payment settled, resolved from invoice_id."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    invoice_number: str
    total: float
    amount_paid: float
    status: str


class CustomerPaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    customer_id: str
    order_id: str | None
    invoice_id: str | None = None
    invoice: PaymentInvoiceBrief | None = None
    amount: float
    payment_mode: str
    reference: str | None
    note: str | None
    received_on: datetime
    created_at: datetime


class CustomerUpdate(BaseModel):
    """Partial update — send only changed fields."""

    name: str | None = Field(default=None, min_length=1, max_length=150)
    business_name: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=20)
    email: EmailStr | None = None
    gst_number: str | None = Field(default=None, max_length=20)
    billing_address: str | None = None
    delivery_address: str | None = None
    assigned_sales_officer_id: str | None = None
    credit_limit: float | None = Field(default=None, ge=0)
    category: str | None = Field(default=None, max_length=100)
    notes: str | None = None
    is_active: bool | None = None

    @field_validator("assigned_sales_officer_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v

    # Customer profile (customer_id is auto-generated, so not settable)
    customer_since: datetime | None = None
    status: CustomerStatus | None = Field(default=None, description="Active | Inactive | Blacklisted | Prospect")
    primary_contact_person: str | None = Field(default=None, max_length=150)
    maps_latitude: float | None = Field(default=None, ge=-90, le=90)
    maps_longitude: float | None = Field(default=None, ge=-180, le=180)


