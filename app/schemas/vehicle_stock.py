from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, field_validator


class VehicleLoadingItemIn(BaseModel):
    product_id: str
    variant_id: str | None = None
    loaded_qty: int = Field(gt=0)

    @field_validator("variant_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v


class VehicleLoadingItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    product_id: str | None
    variant_id: str | None
    product_name: str
    loaded_qty: int
    extra_qty: int
    returned_qty: int
    delivered_qty: int


class VehicleLoadingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    delivery_partner_id: str
    date: datetime
    status: str
    items: list[VehicleLoadingItemOut]
    created_at: datetime
    updated_at: datetime


class VehicleLoadingCreate(BaseModel):
    delivery_partner_id: str
    date: datetime | None = None
    items: list[VehicleLoadingItemIn] = Field(min_length=1)


class ExtraLoadItem(BaseModel):
    product_id: str
    variant_id: str | None = None
    quantity: int = Field(gt=0)

    @field_validator("variant_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v


class ExtraLoadBody(BaseModel):
    items: list[ExtraLoadItem] = Field(min_length=1)


class EndOfDayItem(BaseModel):
    product_id: str
    variant_id: str | None = None
    returned_qty: int = Field(ge=0)

    @field_validator("variant_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v


class EndOfDayBody(BaseModel):
    items: list[EndOfDayItem] = Field(min_length=1)
