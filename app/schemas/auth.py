from pydantic import BaseModel, EmailStr, Field, field_validator

from app.models.enums import UserRole
from app.schemas.organization import OrganizationOut
from app.schemas.role import RoleBriefOut
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


class GoogleAuthRequest(BaseModel):
    """Payload for POST /auth/google containing the Google ID token/credential."""

    credential: str = Field(min_length=1, description="Google OAuth2 ID token from Google Sign-In")


class ExchangeTicketRequest(BaseModel):
    """Payload for POST /auth/exchange containing the short-lived single-use exchange ticket."""

    code: str = Field(min_length=1, description="Single-use OAuth exchange ticket")


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
    """GET /auth/me — who is logged in, which firm they belong to, which workspace
    to open, and exactly what they may do.

    This is the only call the frontend needs after login: `role.workspace` picks
    the dashboard, and `permissions` decides which pages and buttons to show. The
    same matrix is enforced server-side, so hiding a button is a convenience, not
    the security boundary.
    """

    id: str
    organization_id: str | None = None
    name: str
    # The staff member's role. Null for an Admin / Super Admin, who hold no
    # org-scoped role — `full_access` is what marks them.
    role: RoleBriefOut | None = None
    # Every module + action the user may use. Filled in for an Admin too (all
    # true), so the frontend can read this one field for any kind of user.
    permissions: dict[str, dict[str, bool]] = {}
    # True for admin/super_admin (full access in their scope); false for staff.
    full_access: bool = False
    # `own` means list endpoints return only this user's own/assigned records.
    data_scope: str = "all"

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


# --- DEMO-ONLY direct reset (no email verification). Insecure; replace with the
#     token flow (forgot-password + reset-password) before production. ---
class CheckEmailRequest(BaseModel):
    email: EmailStr


class CheckEmailResponse(BaseModel):
    exists: bool


class DirectResetRequest(BaseModel):
    email: EmailStr
    new_password: str = Field(min_length=8, max_length=128)
