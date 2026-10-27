import logging
import re

import jwt
from urllib.parse import quote
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.config import settings
from app.core.security import REFRESH_TOKEN, create_access_token, decode_token, hash_password, verify_password
from app.models import User
from app.schemas.auth import (
    AuthResponse,
    ChangePasswordRequest,
    ExchangeTicketRequest,
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    GoogleAuthRequest,
    GoogleCompleteRegistrationRequest,
    GoogleRegistrationInfoRequest,
    GoogleRegistrationInfoResponse,
    GoogleRegistrationRequired,
    LoginRequest,
    LogoutRequest,
    MessageResponse,
    MeResponse,
    OtpRequest,
    OtpSendResponse,
    OtpVerifyRequest,
    RefreshRequest,
    RegisterOrganization,
    ResetPasswordRequest,
    TokenPair,
)
from app.schemas.theme import OrganizationThemeOut
from app.schemas.user import UserOut
from app.core.phone import normalize_indian_mobile
from app.services import (
    activity_service,
    auth_service,
    google_auth_service,
    org_service,
    otp_service,
    password_service,
    rate_limit_service,
    role_service,
    staff_phone_service,
)
from app.services import email_service
from app.services.email_service import send_password_reset
from app.services.google_auth_service import GoogleAuthError
from app.services.otp_service import OtpError

logger = logging.getLogger("crm.auth")

router = APIRouter(prefix="/auth", tags=["auth"])

# The one rate-limit answer every unauthenticated endpoint gives -- it never says why.
_THROTTLED = "Too many attempts. Please try again later."


class GoogleIdentityConflict(Exception):
    """Raised when a verified Google email matches an existing CRM user whose
    `google_id` is already linked to a DIFFERENT Google account.

    This is deliberately NOT the same as "no user found" — the CRM account
    exists, it is just linked to a different Google identity. The existing
    link must never be silently overwritten (see Scenario C of the Google
    auth architecture): the caller must reject the attempt outright, with no
    linking, no login, no exchange ticket, and no registration ticket.
    """


class GoogleAccountAmbiguous(Exception):
    """Raised when a verified Google email matches more than one CRM user once
    casing is ignored. Linking either one would be a guess, so the attempt is
    rejected and nothing is linked, logged in, or ticketed."""


def _resolve_google_identity(db: Session, google_sub: str, google_email: str) -> User | None:
    """Resolve a verified Google identity to an existing CRM user.

    Priority: (1) exact `google_id` match, (2) case-insensitive `email` match —
    auto-linking `google_id` only when the matched user has none set yet. Raises
    GoogleIdentityConflict instead of linking when the matched user's
    `google_id` is already set to a *different* sub than the one just
    verified, and GoogleAccountAmbiguous when the email matches several users.
    Returns None when no user matches by either key (new user).
    """
    user = db.query(User).filter(User.google_id == google_sub).first()
    if user is not None:
        return user

    # Google emails arrive lowercased; stored emails may not be (staff accounts keep
    # the casing they were created with).
    matches = db.query(User).filter(func.lower(User.email) == google_email.strip().lower()).limit(2).all()
    if len(matches) > 1:
        raise GoogleAccountAmbiguous()
    user = matches[0] if matches else None
    if user is None:
        return None

    if user.google_id is not None and user.google_id != google_sub:
        raise GoogleIdentityConflict()

    if user.google_id is None:
        user.google_id = google_sub
        db.commit()
        db.refresh(user)
    return user


@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
def register_organization(payload: RegisterOrganization, db: Session = Depends(get_db)) -> AuthResponse:
    """Admin self-registration. Creates a new firm and its owner (Admin) account.

    Delegates the actual org/admin/trial/role creation to
    auth_service.register_organization, the same function
    POST /auth/google/complete-registration uses — so the two entry points can
    never implement that business logic differently.
    """
    try:
        admin = auth_service.register_organization(
            db,
            email=str(payload.email),
            organization_name=payload.organization_name,
            admin_name=payload.admin_name,
            password=payload.password,
            business_type=payload.business_type,
            gst_number=payload.gst_number,
            pan_number=payload.pan_number,
            address=payload.address,
            phone=payload.phone,
            financial_year=payload.financial_year,
            logo_url=payload.logo_url,
        )
    except auth_service.RegistrationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    tokens = auth_service.issue_tokens(db, admin)
    return auth_service.build_auth_response(admin, tokens)


@router.post("/login", response_model=AuthResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> AuthResponse:
    user = db.query(User).filter(User.email == payload.email).first()
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is deactivated")

    # Lazily lock the org if its trial has expired, so the response reflects reality.
    org_service.apply_trial_expiry(db, user.organization)

    tokens = auth_service.issue_tokens(db, user)
    return auth_service.build_auth_response(user, tokens)


@router.get("/google", status_code=status.HTTP_307_TEMPORARY_REDIRECT)
def google_oauth_redirect() -> RedirectResponse:
    """Initiate Google OAuth 2.0 Authorization Code redirect flow with signed state."""
    try:
        state = google_auth_service.generate_oauth_state()
        auth_url = google_auth_service.build_google_authorization_url(state)
    except GoogleAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return RedirectResponse(url=auth_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@router.get("/google/callback", status_code=status.HTTP_307_TEMPORARY_REDIRECT)
def google_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Handle Google OAuth2 callback: validate state, exchange code, link user, issue exchange ticket."""
    if error:
        err_msg = error_description or error
        target = f"{settings.frontend_url}/auth/callback?error={quote(err_msg)}"
        return RedirectResponse(url=target, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    if not code or not state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing OAuth code or state parameter",
        )

    try:
        google_auth_service.verify_oauth_state(state)
        google_data = google_auth_service.exchange_authorization_code(code)
    except GoogleAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    google_sub = google_data["sub"]
    google_email = google_data["email"]

    try:
        user = _resolve_google_identity(db, google_sub, google_email)
    except GoogleIdentityConflict:
        # Same email, different Google account already linked elsewhere. Do NOT
        # overwrite, link, log in, or start a new registration — send the user
        # back to the frontend with a distinct, actionable error indicator.
        target = f"{settings.frontend_url}/auth/callback?error={quote('google_identity_conflict')}"
        return RedirectResponse(url=target, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    except GoogleAccountAmbiguous:
        target = f"{settings.frontend_url}/auth/callback?error={quote('google_account_ambiguous')}"
        return RedirectResponse(url=target, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    if user is None:
        # No CRM account for this Google identity yet — verified, but not
        # auto-created. Hand off to the frontend's Complete Registration page
        # with a short-lived registration ticket instead of the old raw 404.
        registration_code = auth_service.create_registration_ticket(
            db, google_sub=google_sub, google_email=google_email, google_name=google_data["name"]
        )
        redirect_url = f"{settings.frontend_url}/auth/register/google?registration_code={quote(registration_code)}"
        return RedirectResponse(url=redirect_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is deactivated")

    # Generate short-lived single-use exchange ticket
    exchange_code = auth_service.create_exchange_ticket(db, user.id)

    redirect_url = f"{settings.frontend_url}/auth/callback?exchange_code={quote(exchange_code)}"
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@router.post("/exchange", response_model=AuthResponse)
def exchange_oauth_ticket(payload: ExchangeTicketRequest, db: Session = Depends(get_db)) -> AuthResponse:
    """Exchange a short-lived, single-use OAuth ticket for full CRM access & refresh tokens."""
    user = auth_service.consume_exchange_ticket(db, payload.code)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid, expired, or already used exchange ticket",
        )

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is deactivated")

    org_service.apply_trial_expiry(db, user.organization)
    tokens = auth_service.issue_tokens(db, user)
    return auth_service.build_auth_response(user, tokens)


@router.post("/google/registration-info", response_model=GoogleRegistrationInfoResponse)
def google_registration_info(
    payload: GoogleRegistrationInfoRequest, db: Session = Depends(get_db)
) -> GoogleRegistrationInfoResponse:
    """Prefill data for the Complete Registration form.

    Reads the registration ticket WITHOUT consuming it, so the frontend can
    safely call this repeatedly (e.g. on a page reload) within the ticket's
    validity window. Never returns tokens or credentials of any kind.
    """
    ticket = auth_service.peek_registration_ticket(db, payload.registration_code)
    if ticket is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid, expired, or already used registration code",
        )
    return GoogleRegistrationInfoResponse(email=ticket.google_email, name=ticket.google_name)


@router.post(
    "/google/complete-registration", response_model=AuthResponse, status_code=status.HTTP_201_CREATED
)
def google_complete_registration(
    payload: GoogleCompleteRegistrationRequest, db: Session = Depends(get_db)
) -> AuthResponse:
    """Complete a Google-originated registration.

    Consumes the single-use registration_code and creates Organization + Admin
    + 7-day Trial + default roles via the exact same auth_service.register_organization
    call POST /auth/register uses. The new account's email comes ONLY from the
    verified ticket (`ticket.google_email`) — this request's schema has no
    email field, so a client cannot influence it. The new Admin's `google_id`
    is set from the ticket's verified Google sub, so subsequent Google logins
    resolve immediately via the fast google_id lookup.
    """
    ticket = auth_service.consume_registration_ticket(db, payload.registration_code)
    if ticket is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid, expired, or already used registration code",
        )

    try:
        admin = auth_service.register_organization(
            db,
            email=ticket.google_email,
            organization_name=payload.organization_name,
            admin_name=payload.admin_name,
            password=payload.password,
            business_type=payload.business_type,
            gst_number=payload.gst_number,
            pan_number=payload.pan_number,
            address=payload.address,
            phone=payload.phone,
            financial_year=payload.financial_year,
            logo_url=payload.logo_url,
            google_id=ticket.google_sub,
        )
    except auth_service.RegistrationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    tokens = auth_service.issue_tokens(db, admin)
    return auth_service.build_auth_response(admin, tokens)


@router.post("/google", response_model=AuthResponse | GoogleRegistrationRequired)
def google_sign_in(
    payload: GoogleAuthRequest, db: Session = Depends(get_db)
) -> AuthResponse | GoogleRegistrationRequired:
    """Authenticate with a verified Google OAuth2 ID token (Direct ID-Token flow).

    Validates the cryptographic token signature and claims with Google, then:
    - an existing CRM user (matched/linked by google_id or email) gets standard
      CRM access and refresh JWT tokens, exactly as before;
    - an unrecognized Google identity gets a `registration_required` response
      carrying a `registration_code` instead of the old bare 404 — no account
      is created here;
    - a Google identity whose email matches a CRM user already linked to a
      *different* google_id is rejected with 409, and nothing is linked,
      logged in, or ticketed.
    """
    try:
        google_data = google_auth_service.verify_google_id_token(payload.credential)
    except GoogleAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    google_sub = google_data["sub"]
    google_email = google_data["email"]

    try:
        user = _resolve_google_identity(db, google_sub, google_email)
    except GoogleIdentityConflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "google_identity_conflict",
                "message": "This account is already linked to a different Google identity.",
            },
        )
    except GoogleAccountAmbiguous:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "google_account_ambiguous",
                "message": "More than one account uses this email. Sign in with your password or contact your administrator.",
            },
        )

    if user is None:
        registration_code = auth_service.create_registration_ticket(
            db, google_sub=google_sub, google_email=google_email, google_name=google_data["name"]
        )
        return GoogleRegistrationRequired(registration_code=registration_code)

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is deactivated")

    # Lazily lock the org if its trial has expired
    org_service.apply_trial_expiry(db, user.organization)

    tokens = auth_service.issue_tokens(db, user)
    return auth_service.build_auth_response(user, tokens)



@router.post("/refresh", response_model=TokenPair)
def refresh(payload: RefreshRequest, db: Session = Depends(get_db)) -> TokenPair:
    try:
        claims = decode_token(payload.refresh_token)
    except jwt.PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")

    if claims.get("type") != REFRESH_TOKEN:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")

    record = auth_service.get_active_refresh(db, payload.refresh_token)
    if record is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token expired or revoked")

    user = db.get(User, claims.get("sub"))
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User no longer active")

    # Same role resolution auth_service.issue_tokens() already uses at login —
    # user.role is None for a pure custom-role user (see User.effective_system_role),
    # so reading .role.value here crashed with AttributeError for exactly those
    # users. effective_system_role never returns None: it falls back to the
    # legacy role mapping and, for a role-less custom user, to "staff" — the
    # lowest-privilege bucket, never an escalation.
    access = create_access_token(user.id, user.effective_system_role, user.organization_id)
    return TokenPair(access_token=access, refresh_token=payload.refresh_token)


@router.post("/logout", response_model=MessageResponse)
def logout(payload: LogoutRequest, db: Session = Depends(get_db)) -> MessageResponse:
    auth_service.revoke_refresh(db, payload.refresh_token)
    # Always report success so the client can clear local tokens regardless.
    return MessageResponse(detail="Logged out")


@router.get("/me", response_model=MeResponse)
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> MeResponse:
    """Who is logged in, their firm, their role + workspace, and their permissions.

    The frontend calls this right after login: `role.workspace` selects the
    dashboard and `permissions` drives which pages and buttons appear. The org
    state (status / plan / trial) rides along so a refresh can render banners and
    lock screens without a re-login — which is also when an expired trial locks.
    """
    org = org_service.apply_trial_expiry(db, user.organization)
    role = role_service.role_for(db, user)
    theme_out = OrganizationThemeOut.from_theme(org.theme if org else None)
    return MeResponse(
        id=user.id,
        organization_id=user.organization_id,
        name=user.name,
        role=role,
        permissions=role_service.effective_permissions(db, user),
        full_access=role_service.is_full_access(user),
        data_scope=role_service.data_scope(db, user),
        phone_verified=bool(user.phone_verified),
        user=user,
        organization=org,
        theme=theme_out,
    )


@router.post("/forgot-password", response_model=ForgotPasswordResponse)
def forgot_password(
    payload: ForgotPasswordRequest, request: Request, db: Session = Depends(get_db)
) -> ForgotPasswordResponse:
    """Start a password reset. Always responds the same way to avoid leaking which
    emails exist. If the email matches an active user, a reset link is emailed.

    Rate limited per email address and per client IP before any account lookup, so a
    429 says nothing about whether the address is registered."""
    generic = "If that email is registered, a reset link has been sent."
    email = str(payload.email).strip().lower()
    ip = rate_limit_service.client_ip(request)
    rate_limit_service.hold(db, ("reset_request_ip", ip), ("reset_request_email", email))
    if rate_limit_service.exceeded(db, "reset_request_ip", ip, rate_limit_service.RESET_REQUEST_IP) or \
            rate_limit_service.exceeded(db, "reset_request_email", email, rate_limit_service.RESET_REQUEST_EMAIL):
        db.commit()
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=_THROTTLED)
    rate_limit_service.record(db, "reset_request_ip", ip)
    rate_limit_service.record(db, "reset_request_email", email)
    db.commit()

    # Case-insensitive, like Google sign-in: the reset form lowercases what it sends,
    # while stored emails keep the casing they were created with. Several accounts
    # differing only by case would make the target a guess, so none gets a link.
    matches = db.query(User).filter(func.lower(User.email) == email).limit(2).all()
    user = matches[0] if len(matches) == 1 else None

    if user is None or not user.is_active:
        email_service.logger.info("reset: no active user for %s", email_service.mask_email(email))
        return ForgotPasswordResponse(detail=generic)

    raw_token = password_service.create_reset_token(db, user)
    reset_link = password_service.build_reset_link(raw_token)
    email_service.logger.info(
        "reset: sending via %s to %s", email_service.delivery_mode(), email_service.mask_email(user.email)
    )
    try:
        send_password_reset(to=user.email, name=user.name, reset_link=reset_link)
    except Exception as exc:  # noqa: BLE001 -- a mail outage must look exactly like any other request
        # email_service already logged the masked "email: FAILED ..." line with the reason.
        logger.error("Password reset email could not be sent: %s", type(exc).__name__)

    return ForgotPasswordResponse(
        detail=generic,
        reset_token=raw_token if settings.expose_reset_token else None,
    )


@router.post("/reset-password", response_model=MessageResponse)
def reset_password(payload: ResetPasswordRequest, db: Session = Depends(get_db)) -> MessageResponse:
    """Set a new password with the emailed token. The token is single-use (consumed
    atomically, even when the reset is then refused) and only ever resets the account
    it was issued for. No session is issued: the user signs in with the new password."""
    user = password_service.consume_reset_token(db, payload.token)
    # A deactivated account gets the same answer as a bad token: no reset, nothing revealed.
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired reset token"
        )

    user.password_hash = hash_password(payload.new_password)
    db.commit()
    # Revoke existing sessions so a leaked old token can't be reused.
    password_service.revoke_all_refresh_tokens(db, user.id)
    return MessageResponse(detail="Password has been reset. Please log in.")


# ------------------------------ Staff mobile OTP login ------------------------------
# Twilio Verify sends and checks the code; this backend decides who may receive one.
# Only an active staff account whose number is unambiguous ever does -- admins and
# super admins keep email/password and Google sign-in. Rate limits are counted before
# any account lookup, so neither they nor the responses reveal which numbers exist.

_OTP_SEND_GENERIC = "If this number is registered, an OTP has been sent."
_OTP_INVALID = "Invalid or expired OTP"
_OTP_CODE = re.compile(r"\d{6}")


def _otp_phone(raw: str) -> str:
    e164 = normalize_indian_mobile(raw)
    if e164 is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Enter a valid 10-digit Indian mobile number",
        )
    return e164


def _otp_configured() -> None:
    try:
        otp_service.require_configured()
    except OtpError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


def _otp_failed(db: Session, e164: str) -> HTTPException:
    """Count one failed verification for this number and answer 400."""
    rate_limit_service.record(db, "otp_verify_failure_phone", e164)
    db.commit()
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_OTP_INVALID)


@router.post("/phone/send-otp", response_model=OtpSendResponse)
def send_phone_otp(payload: OtpRequest, request: Request, db: Session = Depends(get_db)) -> OtpSendResponse:
    """Text a one-time code to a staff mobile number.

    The answer is the same whether or not the number belongs to anyone, and no SMS goes
    out unless it belongs to exactly one active staff account. Never creates an account.
    Limits: 1 per number per 30 s, 5 per 15 min, 10 per day; 30 per IP per hour.
    """
    e164 = _otp_phone(payload.phone)
    _otp_configured()
    ip = rate_limit_service.client_ip(request)
    rate_limit_service.hold(db, ("otp_send_ip", ip), ("otp_send_phone", e164))
    if rate_limit_service.exceeded(db, "otp_send_ip", ip, rate_limit_service.OTP_SEND_IP) or \
            rate_limit_service.exceeded(db, "otp_send_phone", e164, rate_limit_service.OTP_SEND_PHONE):
        db.commit()
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=_THROTTLED)
    rate_limit_service.record(db, "otp_send_ip", ip)
    rate_limit_service.record(db, "otp_send_phone", e164)
    db.commit()

    if staff_phone_service.resolve_otp_account(db, e164) is not None:
        try:
            otp_service.start_verification(e164)
        except OtpError as exc:  # already sanitized and logged (masked) by otp_service
            raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return OtpSendResponse(detail=_OTP_SEND_GENERIC, resend_after=rate_limit_service.OTP_RESEND_AFTER_SECONDS)


@router.post("/phone/verify-otp", response_model=AuthResponse)
def verify_phone_otp(payload: OtpVerifyRequest, request: Request, db: Session = Depends(get_db)) -> AuthResponse:
    """Exchange a valid code for the same session /auth/login issues.

    A wrong, expired or used code is 400 (never 401, which the app reads as an expired
    session). Five failed attempts per number per 15 minutes; the sixth is 429. The
    account is re-resolved from the number on every call -- nothing the client sends
    names a user, firm or role -- and checked again after Twilio approves the code.
    """
    e164 = _otp_phone(payload.phone)
    _otp_configured()
    if not _OTP_CODE.fullmatch(payload.code):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_OTP_INVALID)

    # Held until this request's verdict is committed, so parallel guesses for one number
    # are counted one after another instead of all passing the same check.
    rate_limit_service.hold(db, ("otp_verify_failure_phone", e164))
    if rate_limit_service.exceeded(db, "otp_verify_failure_phone", e164,
                                   rate_limit_service.OTP_VERIFY_FAILURES_PHONE):
        db.commit()
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=_THROTTLED)
    if staff_phone_service.resolve_otp_account(db, e164) is None:
        raise _otp_failed(db, e164)
    try:
        approved = otp_service.check_verification(e164, payload.code)
    except OtpError as exc:  # provider trouble is not the user's failed attempt
        db.commit()
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    if not approved:
        raise _otp_failed(db, e164)
    db.commit()

    db.expire_all()
    user = staff_phone_service.resolve_otp_account(db, e164)
    if user is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_OTP_INVALID)

    user.phone_verified = True
    if user.staff_phone_e164 != e164:  # a legacy row the lookup just proved unique
        user.staff_phone_e164 = e164
    activity_service.record(db, user.organization_id, user, "auth", "Logged in via phone OTP")
    # From here on, exactly what POST /auth/login does for a verified password
    # (issue_tokens also stamps last_login and commits the changes above).
    org_service.apply_trial_expiry(db, user.organization)
    tokens = auth_service.issue_tokens(db, user)
    return auth_service.build_auth_response(user, tokens)


@router.post("/change-password", response_model=MessageResponse)
def change_password(
    payload: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MessageResponse:
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect"
        )
    user.password_hash = hash_password(payload.new_password)
    db.commit()
    password_service.revoke_all_refresh_tokens(db, user.id)
    return MessageResponse(detail="Password changed. Please log in again.")
