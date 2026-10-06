from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import BillingCycle, Organization, OrganizationStatus, Plan, UpgradeStatus


COMPANY_CODE_PREFIX = "CMP-"
_FIRST_COMPANY_NUMBER = 10001


def get_default_plan(db: Session) -> Plan | None:
    """The plan new trial orgs are placed on (the free/default plan), if seeded."""
    return db.query(Plan).filter(Plan.is_default.is_(True)).first()


def ensure_company_code(db: Session, org: Organization, auto_commit: bool = True) -> str:
    """The firm's human-facing code (`CMP-10001`), issued on first use.

    Assigned lazily rather than at registration so firms created before the column
    existed also get one, and never reused: the next number is one past the highest
    code in use, so deleting a firm does not hand its code to the next one.
    """
    if org.company_code:
        return org.company_code
    codes = [
        row[0] for row in db.query(Organization.company_code)
        .filter(Organization.company_code.isnot(None)).all()
    ]
    numbers = [int(c[len(COMPANY_CODE_PREFIX):]) for c in codes
               if c.startswith(COMPANY_CODE_PREFIX) and c[len(COMPANY_CODE_PREFIX):].isdigit()]
    org.company_code = f"{COMPANY_CODE_PREFIX}{max(numbers, default=_FIRST_COMPANY_NUMBER - 1) + 1}"
    if auto_commit:
        db.commit()
        db.refresh(org)
    else:
        db.flush()
    return org.company_code


def start_trial(db: Session, org: Organization) -> None:
    """Initialise a freshly-created org onto the free trial + default plan."""
    org.status = OrganizationStatus.TRIAL
    org.trial_ends_at = datetime.now(timezone.utc) + timedelta(days=settings.trial_days)
    org.upgrade_status = UpgradeStatus.NONE.value
    default_plan = get_default_plan(db)
    if default_plan is not None:
        org.plan_id = default_plan.id


def apply_trial_expiry(db: Session, org: Organization | None) -> Organization | None:
    """Lazily flip an expired trial — or an expired *paid* plan period
    (Razorpay Phase 1's `plan_expires_at`) — to `locked`. Called on login /
    me / gated requests (including app.core.deps.require_unlocked_org, so
    every data-mutation endpoint already gets this for free).

    Reuses the same LOCKED status and upgrade-to-continue flow for both
    cases rather than inventing a second one: an org only ever has
    plan_expires_at set once it's had a plan activated online (see
    app.services.billing_service.activate_subscription), so this never
    fires for an org on the manual-approval flow, which doesn't touch that
    column — existing trial behavior is unaffected.
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
    elif org.status == OrganizationStatus.ACTIVE and org.plan_expires_at is not None:
        end = org.plan_expires_at
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        if end < datetime.now(timezone.utc):
            org.status = OrganizationStatus.LOCKED
            db.commit()
            db.refresh(org)
    return org


def request_upgrade(db: Session, org: Organization, plan: Plan, billing_cycle: BillingCycle) -> Organization:
    org.requested_plan_id = plan.id
    org.billing_cycle = billing_cycle.value
    org.upgrade_status = UpgradeStatus.PENDING.value
    org.upgrade_requested_at = datetime.now(timezone.utc)
    org.upgrade_reject_reason = None
    db.commit()
    db.refresh(org)
    return org


def approve_upgrade(db: Session, org: Organization) -> Organization:
    """Super Admin approves: move the org onto its requested plan and activate it."""
    org.plan_id = org.requested_plan_id
    org.status = OrganizationStatus.ACTIVE
    org.upgrade_status = UpgradeStatus.APPROVED.value
    org.upgrade_reject_reason = None
    org.requested_plan_id = None  # clear the pending request
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
