from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

# permissions shape: { "<module>": { "view": bool, "create": bool, ... }, ... }
PermissionMatrix = dict[str, dict[str, bool]]


class RoleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    name: str
    is_default: bool
    permissions: PermissionMatrix
    created_at: datetime
    updated_at: datetime


class RoleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    permissions: PermissionMatrix = Field(default_factory=dict)


class RoleUpdate(BaseModel):
    """Partial update — only provided fields change. permissions is a full replace."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    permissions: PermissionMatrix | None = None
