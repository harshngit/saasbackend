from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


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
    outstanding_balance: float
    category: str | None
    notes: str | None
    is_active: bool
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
    category: str | None = Field(default=None, max_length=100)
    notes: str | None = None


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
