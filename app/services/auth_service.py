import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, update as sa_update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import create_access_token, create_refresh_token, hash_password
from app.models import OAuthExchangeTicket, OAuthRegistrationTicket, Organization, RefreshToken, User, UserRole
from app.schemas.auth import AuthResponse, TokenPair
from app.services import org_service, role_service

# Registration tickets give a real user time to fill in a form; exchange
# tickets only ever hand off a redirect, so they stay short-lived (60s).
REGISTRATION_TICKET_TTL = timedelta(minutes=30)


class RegistrationError(Exception):
    """Raised by register_organization when the request cannot be fulfilled."""

    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_tokens(db: Session, user: User) -> TokenPair:
    """Create an access/refresh pair and persist the refresh token hash for revocation."""
    access = create_access_token(user.id, user.effective_system_role, user.organization_id)
    refresh = create_refresh_token(user.id)

    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=_hash_token(refresh),
            expires_at=datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_expire_days),
        )
    )
    db.commit()
    return TokenPair(access_token=access, refresh_token=refresh)


def get_active_refresh(db: Session, token: str) -> RefreshToken | None:
    """Return the stored refresh-token record if it exists, is unrevoked, and unexpired."""
    record = db.query(RefreshToken).filter(RefreshToken.token_hash == _hash_token(token)).first()
    if record is None or record.revoked:
        return None
    if record.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        return None
    return record


def revoke_refresh(db: Session, token: str) -> bool:
    record = db.query(RefreshToken).filter(RefreshToken.token_hash == _hash_token(token)).first()
    if record is None or record.revoked:
        return False
    record.revoked = True
    db.commit()
    return True


def create_exchange_ticket(db: Session, user_id: str) -> str:
    """Generate a short-lived (60s), single-use exchange ticket for OAuth redirect token hand-off."""
    raw_ticket = secrets.token_urlsafe(32)
    ticket_hash = _hash_token(raw_ticket)
    ticket = OAuthExchangeTicket(
        user_id=user_id,
        ticket_hash=ticket_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
        used=False,
    )
    db.add(ticket)
    db.commit()
    return raw_ticket


def consume_exchange_ticket(db: Session, code: str) -> User | None:
    """Validate, expire, and atomically consume an OAuth exchange ticket. Returns User if valid.

    The used=False -> used=True flip is a single UPDATE...WHERE statement, so at
    most one of any concurrent duplicate requests can ever have rowcount==1 — a
    plain SELECT-then-UPDATE would let two racing requests both see used=False
    and both proceed.
    """
    ticket_hash = _hash_token(code)
    result = db.execute(
        sa_update(OAuthExchangeTicket)
        .where(OAuthExchangeTicket.ticket_hash == ticket_hash, OAuthExchangeTicket.used.is_(False))
        .values(used=True)
    )
    db.commit()
    if result.rowcount != 1:
        return None
    ticket = db.query(OAuthExchangeTicket).filter(OAuthExchangeTicket.ticket_hash == ticket_hash).first()
    if ticket is None or ticket.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        return None
    return db.get(User, ticket.user_id)


def create_registration_ticket(db: Session, *, google_sub: str, google_email: str, google_name: str | None) -> str:
    """Generate a 30-minute, single-use registration ticket for a verified Google
    identity that has no matching CRM user yet. Only a SHA-256 hash is stored —
    the raw code is returned once, to the client, and never persisted."""
    raw_ticket = secrets.token_urlsafe(32)
    ticket = OAuthRegistrationTicket(
        ticket_hash=_hash_token(raw_ticket),
        google_sub=google_sub,
        google_email=google_email,
        google_name=google_name,
        expires_at=datetime.now(timezone.utc) + REGISTRATION_TICKET_TTL,
        used=False,
    )
    db.add(ticket)
    db.commit()
    return raw_ticket


def peek_registration_ticket(db: Session, code: str) -> OAuthRegistrationTicket | None:
    """Look up a registration ticket WITHOUT consuming it, for prefill purposes.

    Safe to call repeatedly (page reloads) within the ticket's validity window.
    """
    ticket_hash = _hash_token(code)
    ticket = (
        db.query(OAuthRegistrationTicket)
        .filter(OAuthRegistrationTicket.ticket_hash == ticket_hash)
        .first()
    )
    if ticket is None or ticket.used:
        return None
    if ticket.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        return None
    return ticket


def consume_registration_ticket(db: Session, code: str) -> OAuthRegistrationTicket | None:
    """Atomically consume a registration ticket (single-use), mirroring
    consume_exchange_ticket's UPDATE...WHERE used=False race protection."""
    ticket_hash = _hash_token(code)
    result = db.execute(
        sa_update(OAuthRegistrationTicket)
        .where(OAuthRegistrationTicket.ticket_hash == ticket_hash, OAuthRegistrationTicket.used.is_(False))
        .values(used=True)
    )
    db.commit()
    if result.rowcount != 1:
        return None
    ticket = (
        db.query(OAuthRegistrationTicket)
        .filter(OAuthRegistrationTicket.ticket_hash == ticket_hash)
        .first()
    )
    if ticket is None or ticket.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        return None
    return ticket


def register_organization(
    db: Session,
    *,
    email: str,
    organization_name: str,
    admin_name: str,
    password: str,
    business_type: str | None = None,
    gst_number: str | None = None,
    pan_number: str | None = None,
    address: str | None = None,
    phone: str | None = None,
    financial_year: str | None = None,
    logo_url: str | None = None,
    google_id: str | None = None,
) -> User:
    """Create a new Organization + its owner Admin, start the 7-day trial, and
    seed the firm's default roles. The single place both password
    self-registration (POST /auth/register) and Google registration completion
    (POST /auth/google/complete-registration) create a firm, so the two paths
    can never implement organization/admin/trial/role creation differently.

    `google_id` is the verified Google `sub` for a Google-originated
    registration, or None for a password-only one — it is never anything else
    Google supplied (name/email only shape the request's other fields).

    `email` is normalized (stripped + lowercased) so the platform-wide
    uniqueness rule ("one email = one CRM user") holds regardless of casing —
    matching how Google-verified emails already arrive lowercased, and
    checked case-insensitively so this catches a case-variant duplicate even
    on a database where the DB-level unique index hasn't been created yet.
    """
    email = email.strip().lower()
    existing = db.query(User).filter(func.lower(User.email) == email).first()
    if existing is not None:
        raise RegistrationError("Email already registered")

    org = Organization(
        name=organization_name,
        business_type=business_type,
        gst_number=gst_number,
        pan_number=pan_number,
        address=address,
        email=email,
        phone=phone,
        financial_year=financial_year,
        logo_url=logo_url,
    )
    org_service.start_trial(db, org)  # status=trial, default plan, trial_ends_at=now+7d
    db.add(org)
    db.flush()  # assign org.id before creating the user

    admin = User(
        organization_id=org.id,
        name=admin_name,
        email=email,
        phone=phone,
        password_hash=hash_password(password),
        role=UserRole.ADMIN,
        system_role="admin",
        google_id=google_id,
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)

    # Auto-seed the 3 default roles (Sales Officer / Delivery Partner / Accountant).
    role_service.seed_default_roles(db, org.id)

    return admin


def build_auth_response(user: User, tokens: TokenPair) -> AuthResponse:
    return AuthResponse(user=user, organization=user.organization, tokens=tokens)

