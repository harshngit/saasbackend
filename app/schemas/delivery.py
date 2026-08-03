from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DeliveryStatusUpdate(BaseModel):
    status: str = Field(description="Delivered | Partial | Failed | Rescheduled")
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("status")
    @classmethod
    def _valid_status(cls, v: str) -> str:
        choices = {"Delivered", "Partial", "Failed", "Rescheduled"}
        if v not in choices:
            raise ValueError(f"status must be one of {choices}")
        return v

    @model_validator(mode="after")
    def _check_reason(self) -> "DeliveryStatusUpdate":
        if self.status != "Delivered" and not self.reason:
            raise ValueError("reason is required for statuses other than 'Delivered'")
        return self


class DeliveryItemBase(BaseModel):
    product_id: str | None = None
    variant_id: str | None = None
    delivered_quantity: float


class DeliveryItemCreate(DeliveryItemBase):
    pass


class DeliveryItemOut(DeliveryItemBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    product_name: str


class DeliveryNoteBase(BaseModel):
    delivery_note_number: str
    delivery_date: datetime | None = None
    sales_order_id: str | None = None
    customer_id: str | None = None
    warehouse: str | None = None
    delivery_address: str | None = None
    delivery_status: str | None = "pending"


class DeliveryNoteCreate(DeliveryNoteBase):
    items: list[DeliveryItemCreate]


class DeliveryCustomerBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    business_name: str | None = None


class DeliveryOrderBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    order_number: str


class DeliveryNoteOut(DeliveryNoteBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    created_at: datetime
    updated_at: datetime

    items: list[DeliveryItemOut]
    customer: DeliveryCustomerBrief | None = None
    sales_order: DeliveryOrderBrief | None = None
