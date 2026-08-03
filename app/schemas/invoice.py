from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, field_validator


class InvoiceItemIn(BaseModel):
    product_id: str
    variant_id: str | None = None
    quantity: int = Field(gt=0)
    unit_price: float | None = Field(default=None, ge=0)
    discount: float = Field(default=0, ge=0)
    tax: float = Field(default=0, ge=0)

    @field_validator("variant_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v


class InvoiceItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    product_id: str | None
    variant_id: str | None
    product_name: str
    quantity: int
    unit_price: float
    discount: float
    tax: float
    line_total: float


class CustomerBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    business_name: str | None


class InvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    invoice_number: str
    order_id: str | None
    customer_id: str | None
    customer: CustomerBrief | None
    invoice_date: datetime
    status: str
    subtotal: float
    discount: float
    tax: float
    total: float
    amount_paid: float
    notes: str | None
    is_credit_note: bool
    credit_note_reason: str | None
    items: list[InvoiceItemOut]
    created_at: datetime
    updated_at: datetime


class InvoiceCreate(BaseModel):
    customer_id: str
    invoice_date: datetime | None = None
    discount: float = Field(default=0, ge=0)
    tax: float = Field(default=0, ge=0)
    notes: str | None = None
    items: list[InvoiceItemIn] = Field(min_length=1)


class CreditNoteItem(BaseModel):
    product_id: str
    variant_id: str | None = None
    quantity: int = Field(gt=0)

    @field_validator("variant_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v


class CreditNoteBody(BaseModel):
    items: list[CreditNoteItem] | None = None
    reason: str | None = Field(default=None, max_length=500)
