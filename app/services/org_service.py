from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Organization, OrganizationStatus, PlanTier, UpgradeStatus


def start_trial(org: Organization) -> None:
    """Initialise a freshly-created org onto the free trial."""
    org.status = OrganizationStatus.TRIAL
    org.plan = PlanTier.FREE
    org.trial_ends_at = datetime.now(timezone.utc) + timedelta(days=settings.trial_days)
    org.upgrade_status = UpgradeStatus.NONE.value


def apply_trial_expiry(db: Session, org: Organization | None) -> Organization | None:
    """Lazily flip an expired trial to `locked`. Called on login / me / gated requests.

    No cron needed — the check happens whenever the org is loaded.
    """
    if org is None:
        return None
    if org.status == OrganizationStatus.TRIAL and org.trial_ends_at is not None:
        end = org.trial_ends_at
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        if end < datetime.now(timezone.utc):
            org.status = OrganizationStatus.LOCKED
            db.commit()
            db.refresh(org)
    return org


def request_upgrade(db: Session, org: Organization, requested_plan: PlanTier) -> Organization:
    org.requested_plan = requested_plan.value
    org.upgrade_status = UpgradeStatus.PENDING.value
    org.upgrade_requested_at = datetime.now(timezone.utc)
    org.upgrade_reject_reason = None
    db.commit()
    db.refresh(org)
    return org


def approve_upgrade(db: Session, org: Organization) -> Organization:
    """Super Admin approves: activate the org on the requested plan."""
    if org.requested_plan:
        org.plan = PlanTier(org.requested_plan)
    org.status = OrganizationStatus.ACTIVE
    org.upgrade_status = UpgradeStatus.APPROVED.value
    org.upgrade_reject_reason = None
    db.commit()
    db.refresh(org)
    return org


def reject_upgrade(db: Session, org: Organization, reason: str | None) -> Organization:
    org.upgrade_status = UpgradeStatus.REJECTED.value
    org.upgrade_reject_reason = reason
    db.commit()
    db.refresh(org)
    return org


def set_status(db: Session, org: Organization, status: OrganizationStatus) -> Organization:
    """Super Admin manual override (e.g. suspend/reactivate)."""
    org.status = status
    db.commit()
    db.refresh(org)
    return org
