from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class OrderItemIn(BaseModel):
    product_id: str
    variant_id: str | None = None
    quantity: int = Field(gt=0)
    unit_price: float | None = Field(default=None, ge=0)  # defaults to product/variant price
    discount: float = Field(default=0, ge=0)              # per-line

    @field_validator("variant_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v


class OrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    product_id: str | None
    variant_id: str | None
    product_name: str
    quantity: int
    unit_price: float
    discount: float
    line_total: float


class CustomerBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    business_name: str | None


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    order_number: str
    customer_id: str | None
    customer: CustomerBrief | None
    status: str
    source: str
    assigned_delivery_partner_id: str | None
    created_by: str | None
    subtotal: float
    discount: float
    tax: float
    total: float
    notes: str | None
    reject_reason: str | None
    items: list[OrderItemOut]
    approved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class OrderCreate(BaseModel):
    customer_id: str
    source: str = Field(default="office", description="office | delivery_vehicle")
    discount: float = Field(default=0, ge=0)  # order-level
    tax: float = Field(default=0, ge=0)
    notes: str | None = None
    items: list[OrderItemIn] = Field(min_length=1)

    @field_validator("source")
    @classmethod
    def _valid_source(cls, v: str) -> str:
        if v not in ("office", "delivery_vehicle"):
            raise ValueError("source must be 'office' or 'delivery_vehicle'")
        return v


class RejectBody(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class AssignDeliveryBody(BaseModel):
    delivery_partner_id: str


class CancelBody(BaseModel):
    reason: str | None = Field(default=None, max_length=500)
