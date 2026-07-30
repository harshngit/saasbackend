from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class VariantIn(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    length: float | None = None
    width: float | None = None
    height: float | None = None
    weight: float | None = None
    price: float = Field(default=0, ge=0)
    inventory: int = Field(default=0, ge=0)


class VariantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    length: float | None
    width: float | None
    height: float | None
    weight: float | None
    price: float
    inventory: int


class ProductOut(BaseModel):
    """Full product detail (create/update/get-one responses)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    name: str
    description: str | None
    price: float
    cover_image: str | None
    images: list[str]
    product_type: str | None
    vendor: str | None
    brand: str | None
    sku: str | None
    category_id: str | None
    total_inventory: int
    total_stock: int
    is_active: bool
    variations: list[VariantOut]
    created_at: datetime
    updated_at: datetime


class ProductListItem(BaseModel):
    """Lighter product shape for list responses (omits the heavy `images` array)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    name: str
    description: str | None
    price: float
    cover_image: str | None
    product_type: str | None
    vendor: str | None
    brand: str | None
    sku: str | None
    category_id: str | None
    total_inventory: int
    total_stock: int
    is_active: bool
    variations: list[VariantOut]
    created_at: datetime


class ProductCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    price: float = Field(default=0, ge=0)
    cover_image: str | None = None
    images: list[str] = Field(default_factory=list)
    product_type: str | None = Field(default=None, max_length=100)
    vendor: str | None = Field(default=None, max_length=150)
    brand: str | None = Field(default=None, max_length=150)
    sku: str | None = Field(default=None, max_length=100)
    category_id: str | None = None
    total_inventory: int = Field(default=0, ge=0)
    variations: list[VariantIn] = Field(default_factory=list)


class ProductUpdate(BaseModel):
    """Partial update. If `variations` is provided, it fully replaces the variant set."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    price: float | None = Field(default=None, ge=0)
    cover_image: str | None = None
    images: list[str] | None = None
    product_type: str | None = Field(default=None, max_length=100)
    vendor: str | None = Field(default=None, max_length=150)
    brand: str | None = Field(default=None, max_length=150)
    sku: str | None = Field(default=None, max_length=100)
    category_id: str | None = None
    total_inventory: int | None = Field(default=None, ge=0)
    is_active: bool | None = None
    variations: list[VariantIn] | None = None
