from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    name: str
    image: str | None
    description: str | None
    is_active: bool
    parent_id: str | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("image", mode="after")
    @classmethod
    def _normalize_image(cls, v: str | None) -> str | None:
        """Response-only: absolutize against PUBLIC_BASE_URL — see
        app.core.files.normalize_file_url. The database keeps holding
        whatever was stored (relative /files/{id}, or a pasted external URL)."""
        from app.core.files import normalize_file_url
        return normalize_file_url(v)


class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    image: str | None = None
    description: str | None = None
    parent_id: str | None = Field(default=None, description="Set to make this a subcategory of another category")


class CategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=150)
    image: str | None = None
    description: str | None = None
    is_active: bool | None = None
    parent_id: str | None = None


class BulkDelete(BaseModel):
    ids: list[str] = Field(min_length=1)


class BulkDeleteResult(BaseModel):
    deleted: int
