"""Database-backed rate limits for unauthenticated auth endpoints (phone OTP, forgot
password). Counted in `auth_rate_limit_events`, so every app instance sees the same
counts; keys are SHA-256 hashes, never raw phone numbers, emails or IPs.

Concurrency: on PostgreSQL `hold()` takes a transaction-scoped advisory lock per key,
so two simultaneous requests for one number cannot both pass the same count -- the
second waits for the first to commit and then sees its event. The lock is released on
commit or rollback. (SQLite, used for local development, serializes writers itself.)
"""
import hashlib
from datetime import datetime, timedelta, timezone

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import AuthRateLimitEvent

Rules = tuple[tuple[int, timedelta], ...]

# The team's agreed limits.
OTP_SEND_PHONE: Rules = ((1, timedelta(seconds=30)), (5, timedelta(minutes=15)), (10, timedelta(days=1)))
OTP_SEND_IP: Rules = ((30, timedelta(hours=1)),)
OTP_VERIFY_FAILURES_PHONE: Rules = ((5, timedelta(minutes=15)),)
RESET_REQUEST_EMAIL: Rules = ((3, timedelta(minutes=15)), (10, timedelta(days=1)))
RESET_REQUEST_IP: Rules = ((10, timedelta(hours=1)),)
EMAIL_TEST_USER: Rules = ((5, timedelta(hours=1)),)

OTP_RESEND_AFTER_SECONDS = int(OTP_SEND_PHONE[0][1].total_seconds())

# Events older than this can no longer fall inside any window; pruned on every insert.
RETENTION = timedelta(days=2)


def client_ip(request: Request) -> str:
    """The caller's IP: the configured proxy header (Cloudflare: CF-Connecting-IP) when
    set and present, else the socket peer. Only configure the header when the app is
    reachable exclusively through that proxy -- otherwise a client could forge it."""
    if settings.client_ip_header:
        forwarded = request.headers.get(settings.client_ip_header, "").strip()
        if forwarded:
            return forwarded
    return request.client.host if request.client else "unknown"


def _hash(scope: str, key: str) -> str:
    return hashlib.sha256(f"{scope}:{key}".encode("utf-8")).hexdigest()


def hold(db: Session, *keys: tuple[str, str]) -> None:
    """Serialize concurrent requests on these (scope, key) pairs until the caller commits.
    Locks are taken in a fixed order so two requests can never deadlock."""
    if db.get_bind().dialect.name != "postgresql":
        return
    for key_hash in sorted(_hash(scope, key) for scope, key in keys):
        db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": key_hash})


def exceeded(db: Session, scope: str, key: str, rules: Rules) -> bool:
    """Whether any rule's limit has already been reached within its window."""
    now = datetime.now(timezone.utc)
    key_hash = _hash(scope, key)
    for limit, window in rules:
        count = (
            db.query(AuthRateLimitEvent)
            .filter(
                AuthRateLimitEvent.scope == scope,
                AuthRateLimitEvent.key_hash == key_hash,
                AuthRateLimitEvent.created_at > now - window,
            )
            .count()
        )
        if count >= limit:
            return True
    return False


def record(db: Session, scope: str, key: str) -> None:
    """Add one event for (scope, key) and prune everything past retention. Not committed."""
    now = datetime.now(timezone.utc)
    db.query(AuthRateLimitEvent).filter(AuthRateLimitEvent.created_at < now - RETENTION).delete(
        synchronize_session=False
    )
    db.add(AuthRateLimitEvent(scope=scope, key_hash=_hash(scope, key), created_at=now))
    db.flush()
