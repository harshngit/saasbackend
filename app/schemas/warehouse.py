from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class WarehouseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    name: str
    code: str
    address: str | None = None
    city: str | None = None
    contact_number: str | None = None
    is_default: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime


class WarehouseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    code: str | None = Field(
        default=None, max_length=30, description="Auto-assigned (WH-002, WH-003, …) when omitted")
    address: str | None = None
    city: str | None = Field(default=None, max_length=100)
    contact_number: str | None = Field(default=None, max_length=20)
    is_default: bool = Field(
        default=False, description="Making one default clears the flag on the previous one")
    is_active: bool = True


class WarehouseUpdate(BaseModel):
    """Partial update — only the fields you send change."""

    name: str | None = Field(default=None, min_length=1, max_length=150)
    code: str | None = Field(default=None, max_length=30)
    address: str | None = None
    city: str | None = Field(default=None, max_length=100)
    contact_number: str | None = Field(default=None, max_length=20)
    is_default: bool | None = None
    is_active: bool | None = None


class StockRow(BaseModel):
    """One item's position in one warehouse.

    `available` is what an order is checked against: on hand, less everything already
    promised to other orders.
    """

    warehouse_id: str
    warehouse_name: str | None = None
    product_id: str
    product_name: str | None = None
    variant_id: str | None = None
    variant_name: str | None = None
    on_hand: float
    reserved: float
    available: float
    minimum_stock_level: int | None = None


class StockAdjustment(BaseModel):
    """A manual correction — a stock take, a write-off, an opening balance."""

    product_id: str
    variant_id: str | None = None
    quantity: float = Field(description="Signed: positive adds, negative removes")
    movement_type: str = Field(
        default="adjustment",
        pattern="^(opening|adjustment|damaged|expired)$",
        description="opening | adjustment | damaged | expired",
    )
    note: str | None = Field(default=None, max_length=300)
