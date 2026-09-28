from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field


class SupplierBrandLinkCreate(BaseModel):
    brand_id: str = Field(min_length=1, description="UUID of the Brand to link to the Supplier")


class SupplierBrandOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    supplier_id: str
    brand_id: str
    brand_name: str | None = None
    created_at: datetime


class BrandCategoryLinkCreate(BaseModel):
    category_id: str = Field(min_length=1, description="UUID of the Category to link to the Brand")


class BrandCategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    brand_id: str
    category_id: str
    category_name: str | None = None
    created_at: datetime
