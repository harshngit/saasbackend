from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _strip(v: object) -> object:
    return v.strip() if isinstance(v, str) else v


class SubcategoryVariantOut(BaseModel):
    """A Product Type option configured under one sub category."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    subcategory_id: str | None
    name: str
    is_active: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime


class SubcategoryVariantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    sort_order: int = 0

    _strip_name = field_validator("name", mode="before")(_strip)


class SubcategoryVariantUpdate(BaseModel):
    """Rename, reorder, or deactivate (`is_active: false`). There is no delete: products
    that already use a Product Type keep it."""

    name: str | None = Field(default=None, min_length=1, max_length=150)
    is_active: bool | None = None
    sort_order: int | None = None

    _strip_name = field_validator("name", mode="before")(_strip)
