from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PurchaseItemIn(BaseModel):
    product_id: str
    variant_id: str | None = None
    quantity: int = Field(gt=0)
    purchase_price: float = Field(ge=0)
    discount: float = Field(default=0, ge=0)
    tax: float = Field(default=0, ge=0)

    @field_validator("variant_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v


class PurchaseItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    product_id: str | None
    variant_id: str | None
    product_name: str
    quantity: int
    purchase_price: float
    discount: float
    tax: float
    line_total: float


class SupplierBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str


class PurchaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    invoice_number: str
    supplier_id: str | None
    supplier: SupplierBrief | None
    invoice_date: datetime
    status: str
    payment_status: str
    subtotal: float
    discount: float
    tax: float
    total: float
    amount_paid: float
    notes: str | None
    attachment_url: str | None
    items: list[PurchaseItemOut]
    created_at: datetime
    updated_at: datetime


class PurchaseCreate(BaseModel):
    invoice_number: str = Field(min_length=1, max_length=60)
    supplier_id: str
    invoice_date: datetime | None = None
    discount: float = Field(default=0, ge=0)
    tax: float = Field(default=0, ge=0)
    notes: str | None = None
    attachment_url: str | None = None
    items: list[PurchaseItemIn] = Field(min_length=1)


class PurchaseUpdate(BaseModel):
    invoice_number: str | None = Field(default=None, max_length=60)
    invoice_date: datetime | None = None
    discount: float | None = Field(default=None, ge=0)
    tax: float | None = Field(default=None, ge=0)
    notes: str | None = None
    attachment_url: str | None = None
    items: list[PurchaseItemIn] | None = None


class PaymentStatusUpdate(BaseModel):
    payment_status: str = Field(description="unpaid | partial | paid")
    amount_paid: float | None = Field(default=None, ge=0)


class CancelBody(BaseModel):
    reason: str | None = None


class ReturnItem(BaseModel):
    product_id: str
    variant_id: str | None = None
    quantity: int = Field(gt=0)

    @field_validator("variant_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v


class PurchaseReturnBody(BaseModel):
    items: list[ReturnItem] = Field(min_length=1)
    reason: str | None = None
