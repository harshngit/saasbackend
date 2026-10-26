from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class BrandOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    name: str
    description: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class BrandCategoryRef(BaseModel):
    id: str
    name: str


class BrandListItem(BrandOut):
    """GET /brands row: the brand plus the top-level categories it is mapped to."""

    categories: list[BrandCategoryRef] = Field(default_factory=list)


class BrandCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    description: str | None = None


class BrandUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=150)
    description: str | None = None
    is_active: bool | None = None


from app.schemas.bulk import BulkDelete, BulkDeleteResult

