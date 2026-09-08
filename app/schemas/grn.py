from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, field_validator


class GRNItemIn(BaseModel):
    purchase_item_id: str | None = None
    product_id: str | None = None
    variant_id: str | None = None
    product_code: str | None = None
    barcode: str | None = None
    description: str | None = None
    unit_of_measure_uom: str | None = None
    ordered_qty: int = Field(default=0, ge=0)
    received_qty: int = Field(default=0, ge=0)
    damaged_qty: int = Field(default=0, ge=0)
    rejected_qty: int = Field(default=0, ge=0)
    batch_number: str | None = None
    serial_numbers: list[str] | None = None
    expiry_date: datetime | None = None
    notes: str | None = None

    @field_validator("purchase_item_id", "product_id", "variant_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v


class GRNItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    grn_id: str
    purchase_item_id: str | None = None
    product_id: str | None = None
    variant_id: str | None = None
    product_code: str | None = None
    barcode: str | None = None
    product_name: str
    unit_of_measure_uom: str | None = None
    ordered_qty: int = 0
    received_qty: int = 0
    damaged_qty: int = 0
    rejected_qty: int = 0
    accepted_qty: int = 0
    batch_number: str | None = None
    serial_numbers: list[str] | None = None
    expiry_date: datetime | None = None
    notes: str | None = None


class GRNCreate(BaseModel):
    purchase_id: str
    grn_number: str | None = Field(default=None, max_length=50)
    supplier_id: str | None = None
    warehouse_id: str | None = None
    received_date: datetime | None = None
    notes: str | None = None
    items: list[GRNItemIn] | None = None

    @field_validator("supplier_id", "warehouse_id", "grn_number", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v


class GRNUpdate(BaseModel):
    warehouse_id: str | None = None
    received_date: datetime | None = None
    notes: str | None = None
    items: list[GRNItemIn] | None = None

    @field_validator("warehouse_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v


class GRNOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    grn_number: str
    purchase_id: str
    supplier_id: str | None = None
    warehouse_id: str | None = None
    status: str
    received_date: datetime
    notes: str | None = None
    created_by: str | None = None
    created_at: datetime
    updated_at: datetime
    confirmed_at: datetime | None = None
    confirmed_by: str | None = None
    items: list[GRNItemOut] = []
