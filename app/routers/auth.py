import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.config import settings
from app.core.security import REFRESH_TOKEN, create_access_token, decode_token, hash_password, verify_password
from app.models import Organization, OrganizationStatus, PlanTier, User, UserRole
from app.schemas.auth import (
    AuthResponse,
    ChangePasswordRequest,
    ForgotPasswordRequest,
    ForgotPasswordResponse,
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
from app.services import auth_service, org_service, password_service
from app.services.email_service import send_password_reset

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
    org_service.start_trial(org)  # status=trial, plan=free, trial_ends_at=now+7d
    db.add(org)
    db.flush()  # assign org.id before creating the user

    admin = User(
        organization_id=org.id,
        name=payload.admin_name,
        email=payload.email,
        phone=payload.phone,
        password_hash=hash_password(payload.password),
        role=UserRole.ADMIN,
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)

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
    # Include org state (status/plan/trial) so the UI can render banners/lock screens
    # on refresh without a re-login. Also lazily locks an expired trial.
    org = org_service.apply_trial_expiry(db, user.organization)
    return MeResponse(user=user, organization=org)


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
