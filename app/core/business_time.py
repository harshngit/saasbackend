"""The organization's business day -- the one place "today", "this month" and "which day did
this happen" are decided, so the dashboard, reports, attendance, Day-End and cash
reconciliation all agree.

An organization works in its own timezone (organizations.timezone, default Asia/Kolkata).
Dates sent by a client (date_from / date_to = YYYY-MM-DD) are that timezone's calendar
days, inclusive; timestamps in the database are UTC. So 00:30 IST on 5 Nov belongs to
5 Nov even though it is 19:00 UTC on 4 Nov.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger("crm.time")

DEFAULT_BUSINESS_TIMEZONE = "Asia/Kolkata"


def zone(name: str | None) -> ZoneInfo:
    """A timezone by name; an unknown or empty name means the default. UTC only if even the
    default cannot be loaded (no timezone data installed) -- and that is logged."""
    for candidate in (name, DEFAULT_BUSINESS_TIMEZONE):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            if candidate == DEFAULT_BUSINESS_TIMEZONE:
                logger.warning("Timezone data for %s is missing (install tzdata); using UTC", candidate)
    return ZoneInfo("UTC")


def org_timezone(db, org_id: str | None) -> ZoneInfo:  # noqa: ANN001
    """The organization's business timezone (default Asia/Kolkata)."""
    from app.models import Organization

    try:
        org = db.get(Organization, org_id) if (db is not None and org_id) else None
    except Exception:  # noqa: BLE001 -- a lookup problem must not break a report; use the default
        org = None
    return zone(getattr(org, "timezone", None))


def today(tz: ZoneInfo) -> date:
    return datetime.now(tz).date()


def day_start(day: date, tz: ZoneInfo) -> datetime:
    """The first instant of `day` in `tz`, as a UTC datetime (for comparing with stored timestamps)."""
    return datetime.combine(day, time.min, tzinfo=tz).astimezone(timezone.utc)


def day_end(day: date, tz: ZoneInfo) -> datetime:
    """The last instant of `day` in `tz`, as a UTC datetime -- ranges are inclusive."""
    return datetime.combine(day, time.max, tzinfo=tz).astimezone(timezone.utc)


def local_date(moment, tz: ZoneInfo) -> date:  # noqa: ANN001
    """The business day a stored timestamp falls on. Naive timestamps are UTC (that is how
    the app stores them); a plain date is returned as it is."""
    if not isinstance(moment, datetime):
        return moment
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(tz).date()
