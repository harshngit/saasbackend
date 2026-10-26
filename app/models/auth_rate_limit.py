import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AuthRateLimitEvent(Base):
    """One attempt against an unauthenticated auth endpoint (e.g. an OTP request).

    Counted per (scope, key) over a time window, in the database so every app instance
    sees the same counts. `key_hash` is a SHA-256 of the phone number or client IP --
    neither is stored in the clear. Rows older than the longest window are pruned as
    new ones are written.
    """

    __tablename__ = "auth_rate_limit_events"
    __table_args__ = (Index("ix_auth_rate_limit_events_lookup", "scope", "key_hash", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    scope: Mapped[str] = mapped_column(String(40), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False, index=True)
