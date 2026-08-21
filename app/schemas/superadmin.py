from pydantic import BaseModel, EmailStr, Field


class SuperAdminCreate(BaseModel):
    """Create another platform Super Admin — a global account, no organization."""

    name: str = Field(min_length=1, max_length=150)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    phone: str | None = None


class SuperAdminUpdate(BaseModel):
    """Partial update — only the fields sent are changed."""

    name: str | None = None
    email: EmailStr | None = None
    phone: str | None = None
    password: str | None = Field(default=None, min_length=8, max_length=128)
    is_active: bool | None = None
