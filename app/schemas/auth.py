from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.models.enums import UserRole
from app.schemas.employee_profile import OptionalDate, OptionalEmail
from app.schemas.organization import OrganizationOut
from app.schemas.role import RoleBriefOut
from app.schemas.theme import OrganizationThemeOut
from app.schemas.user import UserOut


class OrganizationRegistrationFields(BaseModel):
    """The firm + owner-account fields shared by every self-registration entry
    point (password-based and Google-based). Kept as one base class so the two
    request schemas can never drift apart — RegisterOrganization only adds
    `email`/`role` on top; GoogleCompleteRegistrationRequest deliberately adds
    neither (its email comes from the verified registration ticket, and its
    role is always Admin, exactly like RegisterOrganization enforces below).
    """

    # --- Company / firm profile ---
    organization_name: str = Field(min_length=1, max_length=200)
    business_type: str | None = Field(default=None, max_length=100)
    gst_number: str | None = Field(default=None, max_length=20)
    pan_number: str | None = Field(default=None, max_length=20)
    address: str | None = Field(default=None, max_length=500)
    phone: str | None = Field(default=None, max_length=20)
    financial_year: str | None = Field(default=None, max_length=20, examples=["2025-2026"])
    logo_url: str | None = Field(default=None, max_length=500)

    # --- Owner (Admin) account ---
    admin_name: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=8, max_length=128)


class RegisterOrganization(OrganizationRegistrationFields):
    """Admin self-registration: creates a firm and its owner (Admin) account.

    Only Admins self-register. Staff (accountant / sales_officer / delivery_partner)
    are created afterwards by the Admin via POST /users — so `role` is always admin.
    """

    email: EmailStr
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


class GoogleRegistrationRequired(BaseModel):
    """Returned by POST /auth/google when the verified Google identity has no
    matching CRM account yet. The SPA should route to the Complete
    Registration page with this code — no account exists, no tokens are
    issued, nothing was created."""

    status: str = "registration_required"
    registration_code: str


class GoogleRegistrationInfoRequest(BaseModel):
    """Payload for POST /auth/google/registration-info: read (not consume) a
    pending registration ticket, to prefill the Complete Registration form."""

    registration_code: str = Field(min_length=1)


class GoogleRegistrationInfoResponse(BaseModel):
    """Prefill data for the Complete Registration form. `email` is rendered
    read-only in the UI — it is never accepted back from the client."""

    email: EmailStr
    name: str | None = None


class GoogleCompleteRegistrationRequest(OrganizationRegistrationFields):
    """Payload for POST /auth/google/complete-registration.

    Inherits the exact same organization/admin/password fields and validation
    as RegisterOrganization (via OrganizationRegistrationFields), so the two
    contracts cannot drift apart. Deliberately carries NO `email` field: the
    new account's email always comes from the verified Google identity bound
    to `registration_code`, never from the client. `role` is not accepted
    either — Google self-registration always creates an Admin, exactly like
    password self-registration.
    """

    registration_code: str = Field(min_length=1)


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
    # True once this account has signed in with an OTP texted to its staff number.
    phone_verified: bool = False

    user: UserOut
    organization: OrganizationOut | None
    theme: OrganizationThemeOut | None = None


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
    token: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=128)
    # Optional: when sent it must equal new_password (the form also checks this).
    confirm_password: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _passwords_match(self) -> "ResetPasswordRequest":
        if self.confirm_password is not None and self.confirm_password != self.new_password:
            raise ValueError("confirm_password does not match new_password")
        return self


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class ProfileSelfUpdate(BaseModel):
    """PATCH /auth/me — the signed-in user's own personal details; only the fields sent
    change. Anything else (email, phone, role, organization, status, password, photo,
    employment or payroll fields) is rejected with 422: those stay with the admin
    workflows, POST /auth/change-password and POST /auth/me/photo."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=150)
    first_name: str | None = Field(default=None, max_length=50)
    last_name: str | None = Field(default=None, max_length=50)
    display_name: str | None = Field(default=None, max_length=150)
    gender: str | None = Field(default=None, max_length=30)
    date_of_birth: OptionalDate = None
    marital_status: str | None = Field(default=None, max_length=30)
    blood_group: str | None = Field(default=None, max_length=10)
    nationality: str | None = Field(default=None, max_length=100)
    alternate_mobile_number: str | None = Field(default=None, max_length=20)
    personal_email: OptionalEmail = None
    emergency_contact_name: str | None = Field(default=None, max_length=150)
    emergency_contact_number: str | None = Field(default=None, max_length=20)
    emergency_contact_relationship: str | None = Field(default=None, max_length=50)
    current_address: str | None = None
    permanent_address: str | None = None
    city: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=100)
    country: str | None = Field(default=None, max_length=100)
    pin_zip_code: str | None = Field(default=None, max_length=20)
    language: str | None = Field(default=None, max_length=30)
    time_zone: str | None = Field(default=None, max_length=64)

    @field_validator("name", mode="before")
    @classmethod
    def _name_not_blank(cls, v: object) -> object:
        if v is None or (isinstance(v, str) and not v.strip()):
            raise ValueError("name cannot be empty")
        return v.strip() if isinstance(v, str) else v


class OtpRequest(BaseModel):
    """POST /auth/phone/send-otp. An Indian mobile number, e.g. `9876543210`,
    `+91 98765-43210` or `09876543210`."""

    phone: str = Field(min_length=1, max_length=20)


class OtpSendResponse(BaseModel):
    detail: str
    # Seconds before another code may be requested for this number.
    resend_after: int


class OtpVerifyRequest(BaseModel):
    """POST /auth/phone/verify-otp. `code` must be exactly six digits; anything else is
    answered 400 "Invalid or expired OTP" without contacting Twilio."""

    phone: str = Field(min_length=1, max_length=20)
    code: str = Field(max_length=32)
