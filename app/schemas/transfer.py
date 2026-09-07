from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, field_validator


class WarehouseTransferItemIn(BaseModel):
    product_id: str
    variant_id: str | None = None
    quantity: int = Field(gt=0, description="Quantity must be greater than zero")

    @field_validator("variant_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v


class WarehouseTransferItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    transfer_id: str
    product_id: str
    variant_id: str | None = None
    quantity: int


class WarehouseBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    code: str | None = None


class WarehouseTransferOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    transfer_number: str
    source_warehouse_id: str
    destination_warehouse_id: str
    status: str
    notes: str | None = None

    created_by: str | None = None
    dispatched_by: str | None = None
    dispatched_at: datetime | None = None
    received_by: str | None = None
    received_at: datetime | None = None

    created_at: datetime
    updated_at: datetime

    items: list[WarehouseTransferItemOut] = Field(default_factory=list)
    source_warehouse: WarehouseBrief | None = None
    destination_warehouse: WarehouseBrief | None = None


class WarehouseTransferCreate(BaseModel):
    source_warehouse_id: str
    destination_warehouse_id: str
    items: list[WarehouseTransferItemIn] = Field(min_length=1)
    notes: str | None = Field(default=None, max_length=500)
