"""Follow-up reminders: "due" and "overdue" notifications for the assigned salesperson.

There is no job scheduler in this backend, so a sweep runs in-process on a daemon thread
(started at app startup, FOLLOW_UP_REMINDERS_ENABLED) and can also be run once from an
external cron: `python -m app.scripts.send_follow_up_reminders`.

Each reminder carries a dedupe key built from the follow-up id, the kind (due / overdue)
and the due time, backed by the unique (user_id, dedupe_key) index on notifications. So
any number of sweeps -- repeated, overlapping, or on several instances at once -- send each
reminder at most once, and rescheduling a follow-up (a new due time) arms it again.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core import notification_types as nt
from app.core.config import settings
from app.core.security import as_utc
from app.models import FollowUp
from app.services import notification_service

logger = logging.getLogger("crm.reminders")


def _recipient(db: Session, follow_up: FollowUp):  # noqa: ANN202
    """The follow-up's assignee, else its Lead's salesperson -- an active member of the same
    organization who can open the follow-up."""
    lead = follow_up.lead
    for user_id in (follow_up.assigned_to_id, getattr(lead, "assigned_salesperson_id", None)):
        user = notification_service._member(db, user_id, follow_up.organization_id)
        if user is not None:
            if notification_service._can_view(db, user, "follow_ups", follow_up, "assigned_to_id"):
                return user
            return None
    return None


def run_follow_up_reminders(db: Session, now: datetime | None = None) -> dict[str, int]:
    """One sweep across every organization. Commits per reminder, so one bad row never
    holds back the rest. Returns how many due / overdue reminders were created."""
    now = as_utc(now) if now is not None else datetime.now(timezone.utc)
    lead = timedelta(minutes=settings.follow_up_due_lead_minutes)
    grace = timedelta(minutes=settings.follow_up_overdue_after_minutes)
    oldest = now - grace - timedelta(days=settings.follow_up_overdue_lookback_days)

    candidates = (
        db.query(FollowUp)
        .filter(FollowUp.status == "pending", FollowUp.due_date <= now + lead, FollowUp.due_date >= oldest)
        .order_by(FollowUp.due_date, FollowUp.id)
        .all()
    )
    counts = {"due": 0, "overdue": 0}
    for follow_up in candidates:
        due = as_utc(follow_up.due_date)
        kind = "overdue" if now >= due + grace else "due"
        try:
            user = _recipient(db, follow_up)
            if user is None:
                continue
            event = nt.FOLLOW_UP_OVERDUE if kind == "overdue" else nt.FOLLOW_UP_DUE
            party = getattr(follow_up.customer, "name", None) or getattr(follow_up.lead, "name", None)
            when = due.strftime("%d %b %Y %H:%M UTC")
            title = "Follow-up overdue" if kind == "overdue" else "Follow-up due"
            body = f"{follow_up.title}" + (f" · {party}" if party else "") + (
                f" — was due {when}" if kind == "overdue" else f" — due {when}")
            created = notification_service.notify(
                db, user.id, title, body, link=f"/follow-ups/{follow_up.id}",
                organization_id=follow_up.organization_id,
                data=nt.build_data(event, follow_up_id=follow_up.id, lead_id=follow_up.lead_id,
                                   customer_id=follow_up.customer_id),
                dedupe_key=f"follow_up:{follow_up.id}:{kind}:{due.isoformat()}",
            )
            db.commit()
            if created is not None:
                counts[kind] += 1
        except Exception:  # noqa: BLE001 -- one follow-up must not stop the sweep
            db.rollback()
            logger.exception("Follow-up reminder failed for %s", follow_up.id)
    if counts["due"] or counts["overdue"]:
        logger.info("Follow-up reminders sent: %d due, %d overdue", counts["due"], counts["overdue"])
    return counts


def run_once() -> dict[str, int]:
    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        return run_follow_up_reminders(db)
    finally:
        db.close()


_stop = threading.Event()
_thread: threading.Thread | None = None


def _loop(interval: int) -> None:
    # Wait first: a process that is starting (or a test client) does no sweep work.
    while not _stop.wait(interval):
        try:
            run_once()
        except Exception:  # noqa: BLE001 -- e.g. the database is briefly unreachable
            logger.exception("Follow-up reminder sweep failed")


def start_background_sweeps() -> bool:
    """Start the in-process sweep once per process. Returns whether it is running."""
    global _thread
    if not settings.follow_up_reminders_enabled:
        return False
    if _thread is not None and _thread.is_alive():
        return True
    interval = max(int(settings.follow_up_reminder_interval_seconds), 30)
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(interval,), name="follow-up-reminders", daemon=True)
    _thread.start()
    logger.info("Follow-up reminder sweep every %ds", interval)
    return True


def stop_background_sweeps() -> None:
    _stop.set()
