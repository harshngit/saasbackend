from pydantic import BaseModel, EmailStr, Field, field_validator

from app.models.enums import UserRole
from app.schemas.organization import OrganizationOut
from app.schemas.user import UserOut


class RegisterOrganization(BaseModel):
    """Admin self-registration: creates a firm and its owner (Admin) account.

    Only Admins self-register. Staff (accountant / sales_officer / delivery_partner)
    are created afterwards by the Admin via POST /users — so `role` is always admin.
    """

    # --- Company / firm profile ---
    organization_name: str = Field(min_length=1, max_length=200)
    business_type: str | None = Field(default=None, max_length=100)
    gst_number: str | None = Field(default=None, max_length=20)
    pan_number: str | None = Field(default=None, max_length=20)
    address: str | None = Field(default=None, max_length=500)
    phone: str | None = Field(default=None, max_length=20)
    email: EmailStr
    financial_year: str | None = Field(default=None, max_length=20, examples=["2025-2026"])
    logo_url: str | None = Field(default=None, max_length=500)

    # --- Owner (Admin) account ---
    admin_name: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=8, max_length=128)
    role: UserRole = Field(default=UserRole.ADMIN, description="Always 'admin' for self-registration")

    @field_validator("role")
    @classmethod
    def _role_must_be_admin(cls, v: UserRole) -> UserRole:
        if v != UserRole.ADMIN:
            raise ValueError("Self-registration can only create an admin. Staff are created by the Admin.")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class AuthResponse(BaseModel):
    """Returned on login/register: tokens plus the resolved user + firm context."""

    user: UserOut
    organization: OrganizationOut | None
    tokens: TokenPair


class MeResponse(BaseModel):
    """GET /auth/me — current user plus their org (status/plan/trial) for the UI."""

    user: UserOut
    organization: OrganizationOut | None


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class MessageResponse(BaseModel):
    detail: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ForgotPasswordResponse(BaseModel):
    detail: str
    # Populated only when EXPOSE_RESET_TOKEN=true (dev). None otherwise.
    reset_token: str | None = None


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=128)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)
