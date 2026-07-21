from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.enums import UserRole


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str | None
    name: str
    email: EmailStr
    phone: str | None
    role: UserRole
    is_active: bool
    created_at: datetime


class StaffCreate(BaseModel):
    """Payload an Admin sends to create a staff user in their own firm."""

    name: str = Field(min_length=1, max_length=150)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=20)
    password: str = Field(min_length=8, max_length=128)
    role: UserRole


class UserStatusUpdate(BaseModel):
    is_active: bool


class AdminResetPassword(BaseModel):
    new_password: str = Field(min_length=8, max_length=128)
