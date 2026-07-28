from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.models.enums import UserRole


class RoleBrief(BaseModel):
    """Compact role info nested inside a user (name + permission matrix)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    is_default: bool
    permissions: dict[str, dict[str, bool]]


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str | None
    name: str
    email: EmailStr
    phone: str | None
    system_role: str | None            # super_admin / admin / staff
    role_id: str | None
    role_detail: RoleBrief | None      # the staff member's role + permissions
    role: UserRole | None              # legacy fixed-role enum (kept for backward-compat)
    is_active: bool
    created_at: datetime


class StaffCreate(BaseModel):
    """Admin creates a staff user. Prefer `role_id`; `role` (legacy enum) still
    accepted for backward-compat and mapped to the org's matching default role."""

    name: str = Field(min_length=1, max_length=150)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=20)
    password: str = Field(min_length=8, max_length=128)
    role_id: str | None = None
    role: UserRole | None = None

    @model_validator(mode="after")
    def _require_a_role(self) -> "StaffCreate":
        if self.role_id is None and self.role is None:
            raise ValueError("Provide role_id (preferred) or role")
        return self


class UserUpdate(BaseModel):
    """Edit a staff member's profile (not their role — use /role for that)."""

    name: str | None = Field(default=None, min_length=1, max_length=150)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=20)


class RoleAssign(BaseModel):
    role_id: str


class UserStatusUpdate(BaseModel):
    is_active: bool


class AdminResetPassword(BaseModel):
    new_password: str = Field(min_length=8, max_length=128)
