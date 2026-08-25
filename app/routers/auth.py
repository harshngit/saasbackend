import jwt
from urllib.parse import quote
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.config import settings
from app.core.security import REFRESH_TOKEN, create_access_token, decode_token, hash_password, verify_password
from app.models import Organization, User, UserRole
from app.schemas.auth import (
    AuthResponse,
    ChangePasswordRequest,
    CheckEmailRequest,
    CheckEmailResponse,
    DirectResetRequest,
    ExchangeTicketRequest,
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    GoogleAuthRequest,
    LoginRequest,
    LogoutRequest,
    MessageResponse,
    MeResponse,
    RefreshRequest,
    RegisterOrganization,
    ResetPasswordRequest,
    TokenPair,
)
from app.schemas.user import UserOut
from app.services import auth_service, google_auth_service, org_service, password_service, role_service
from app.services.email_service import send_password_reset
from app.services.google_auth_service import GoogleAuthError

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
def register_organization(payload: RegisterOrganization, db: Session = Depends(get_db)) -> AuthResponse:
    """Admin self-registration. Creates a new firm and its owner (Admin) account."""
    existing = db.query(User).filter(User.email == payload.email).first()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    org = Organization(
        name=payload.organization_name,
        business_type=payload.business_type,
        gst_number=payload.gst_number,
        pan_number=payload.pan_number,
        address=payload.address,
        email=payload.email,
        phone=payload.phone,
        financial_year=payload.financial_year,
        logo_url=payload.logo_url,
    )
    org_service.start_trial(db, org)  # status=trial, default plan, trial_ends_at=now+7d
    db.add(org)
    db.flush()  # assign org.id before creating the user

    admin = User(
        organization_id=org.id,
        name=payload.admin_name,
        email=payload.email,
        phone=payload.phone,
        password_hash=hash_password(payload.password),
        role=UserRole.ADMIN,
        system_role="admin",
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)

    # Auto-seed the 3 default roles (Sales Officer / Delivery Partner / Accountant).
    role_service.seed_default_roles(db, org.id)

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

    # 1. Look up by existing linked Google ID
    user = db.query(User).filter(User.google_id == google_sub).first()

    # 2. If not found by google_id, look up by verified email and link the account
    if user is None:
        user = db.query(User).filter(User.email == google_email).first()
        if user is not None:
            user.google_id = google_sub
            db.commit()
            db.refresh(user)

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No CRM account found for this Google account. Please contact your organization administrator or register your organization.",
        )

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


@router.post("/google", response_model=AuthResponse)
def google_sign_in(payload: GoogleAuthRequest, db: Session = Depends(get_db)) -> AuthResponse:
    """Authenticate with a verified Google OAuth2 ID token (Direct ID-Token flow).

    Validates the cryptographic token signature and claims with Google, links or locates
    the matching active CRM user, and issues standard CRM access and refresh JWT tokens.
    """
    try:
        google_data = google_auth_service.verify_google_id_token(payload.credential)
    except GoogleAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    google_sub = google_data["sub"]
    google_email = google_data["email"]

    # 1. Look up by existing linked Google ID
    user = db.query(User).filter(User.google_id == google_sub).first()

    # 2. If not found by google_id, look up by verified email and link the account
    if user is None:
        user = db.query(User).filter(User.email == google_email).first()
        if user is not None:
            user.google_id = google_sub
            db.commit()
            db.refresh(user)

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No CRM account found for this Google account. Please contact your organization administrator or register your organization.",
        )

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

    access = create_access_token(user.id, user.role.value, user.organization_id)
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
    return MeResponse(
        id=user.id,
        organization_id=user.organization_id,
        name=user.name,
        role=role,
        permissions=role_service.effective_permissions(db, user),
        full_access=role_service.is_full_access(user),
        data_scope=role_service.data_scope(db, user),
        user=user,
        organization=org,
    )


@router.post("/forgot-password", response_model=ForgotPasswordResponse)
def forgot_password(payload: ForgotPasswordRequest, db: Session = Depends(get_db)) -> ForgotPasswordResponse:
    """Start a password reset. Always responds the same way to avoid leaking which
    emails exist. If the email matches an active user, a reset link is emailed."""
    generic = "If that email is registered, a reset link has been sent."
    user = db.query(User).filter(User.email == payload.email).first()

    if user is None or not user.is_active:
        return ForgotPasswordResponse(detail=generic)

    raw_token = password_service.create_reset_token(db, user)
    reset_link = password_service.build_reset_link(raw_token)
    send_password_reset(to=user.email, name=user.name, reset_link=reset_link)

    return ForgotPasswordResponse(
        detail=generic,
        reset_token=raw_token if settings.expose_reset_token else None,
    )


@router.post("/reset-password", response_model=MessageResponse)
def reset_password(payload: ResetPasswordRequest, db: Session = Depends(get_db)) -> MessageResponse:
    user = password_service.consume_reset_token(db, payload.token)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired reset token"
        )

    user.password_hash = hash_password(payload.new_password)
    db.commit()
    # Revoke existing sessions so a leaked old token can't be reused.
    password_service.revoke_all_refresh_tokens(db, user.id)
    return MessageResponse(detail="Password has been reset. Please log in.")


@router.post("/check-email", response_model=CheckEmailResponse)
def check_email(payload: CheckEmailRequest, db: Session = Depends(get_db)) -> CheckEmailResponse:
    """DEMO: report whether an account exists for this email, so the UI can then
    show the 'set new password' field. (Reveals account existence — demo only.)"""
    user = db.query(User).filter(User.email == payload.email).first()
    return CheckEmailResponse(exists=user is not None and user.is_active)


@router.post("/reset-password-direct", response_model=MessageResponse)
def reset_password_direct(payload: DirectResetRequest, db: Session = Depends(get_db)) -> MessageResponse:
    """DEMO-ONLY: reset a password using just the email — NO ownership verification.
    Insecure (anyone can reset anyone's password); replace with the token-based
    forgot-password / reset-password flow before production."""
    user = db.query(User).filter(User.email == payload.email).first()
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No account found for that email")
    user.password_hash = hash_password(payload.new_password)
    db.commit()
    password_service.revoke_all_refresh_tokens(db, user.id)
    return MessageResponse(detail="Password has been reset. Please log in.")


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
