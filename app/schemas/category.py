from datetime import datetime

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

# Names are stored trimmed; uniqueness compares them trimmed and case-insensitively.
CategoryName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=150)]


class CategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    name: str
    image: str | None
    description: str | None
    is_active: bool
    parent_id: str | None = None
    parent_name: str | None = Field(
        default=None,
        description="The parent category's name for a sub category (null for a top-level one). "
                    "Sub category names repeat across parents, so show 'Parent › Name'.",
    )
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
    name: CategoryName
    image: str | None = None
    description: str | None = None
    parent_id: str | None = Field(default=None, description="Set to make this a subcategory of another category")


class CategoryUpdate(BaseModel):
    name: CategoryName | None = None
    image: str | None = None
    description: str | None = None
    is_active: bool | None = None
    parent_id: str | None = None


from app.schemas.bulk import BulkDelete, BulkDeleteResult

