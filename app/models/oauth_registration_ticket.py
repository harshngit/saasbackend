import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class OAuthRegistrationTicket(Base):
    """Single-use, expiring ticket binding a verified-but-unregistered Google
    identity to a pending CRM registration attempt.

    Distinct from OAuthExchangeTicket: an exchange ticket always points at an
    existing CRM user; this ticket exists precisely because no CRM user exists
    yet. It stores only a SHA-256 hash of the registration code, plus the
    Google claims needed to complete registration without a second Google
    round-trip. `google_email` is the sole source of truth for the new
    account's email — never trust a client-supplied email during completion.
    """

    __tablename__ = "oauth_registration_tickets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    ticket_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    google_sub: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    google_email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    google_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
