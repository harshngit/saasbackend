from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import STOCK_MOVEMENT_TYPES


class VariantStock(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    inventory: int


class InventoryItemOut(BaseModel):
    """A row on the stock board — a product with its current available stock."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    sku: str | None
    product_type: str | None
    brand: str | None
    category_id: str | None
    total_inventory: int          # base stock (no-variant products)
    total_stock: int              # effective available stock
    is_active: bool
    variations: list[VariantStock]


class StockMovementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    product_id: str
    variant_id: str | None
    movement_type: str
    quantity: int                 # signed: + added, - removed
    balance_after: int
    note: str | None
    created_by: str | None
    created_at: datetime


class InventoryDetailOut(InventoryItemOut):
    movements: list[StockMovementOut]


class StockAdjustmentCreate(BaseModel):
    product_id: str
    variant_id: str | None = None
    movement_type: str = Field(description="opening/purchase_in/sale_out/delivery_out/sales_return/purchase_return/damaged/expired/adjustment")
    quantity: int = Field(description="signed: positive adds stock, negative removes")
    note: str | None = None

    @field_validator("movement_type")
    @classmethod
    def _valid_type(cls, v: str) -> str:
        if v not in STOCK_MOVEMENT_TYPES:
            raise ValueError(f"movement_type must be one of {sorted(STOCK_MOVEMENT_TYPES)}")
        return v

    @field_validator("quantity")
    @classmethod
    def _nonzero(cls, v: int) -> int:
        if v == 0:
            raise ValueError("quantity cannot be 0")
        return v

    @field_validator("variant_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v


class SetStock(BaseModel):
    """Set a product/variant's stock to an exact number (records the delta)."""

    variant_id: str | None = None
    quantity: int = Field(ge=0, description="new absolute stock level")
    note: str | None = None

    @field_validator("variant_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v
