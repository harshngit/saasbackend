"""Razorpay Phase 1 — SaaS plan payments (TEST MODE).

Additive to, never a replacement for, the manual Super Admin upgrade-approval
flow (app.services.org_service.request_upgrade / approve_upgrade), which
keeps working unconditionally regardless of whether Razorpay is configured.

activate_subscription() is the single shared activation path for both
POST /billing/razorpay/verify and POST /billing/razorpay/webhook — it must
never be duplicated between the two.
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

try:
    import razorpay
    _SignatureVerificationError = razorpay.errors.SignatureVerificationError
except (ImportError, AttributeError):
    razorpay = None
    class _SignatureVerificationError(Exception):
        pass
from sqlalchemy.orm import Session, lazyload

from app.core.config import settings
from app.models import (
    BillingCycle,
    Organization,
    OrganizationStatus,
    Plan,
    SubscriptionPayment,
    UpgradeStatus,
    User,
)
from app.schemas.razorpay import RazorpayVerifyRequest, SubscriptionPaymentOut
from app.services import activity_service

# How long one payment extends the subscription by. Not the same as
# calendar months/years on purpose — a fixed day-count is unambiguous and
# never needs timezone/DST-aware calendar math.
PLAN_PERIOD_DAYS: dict[str, int] = {
    BillingCycle.MONTHLY.value: 30,
    BillingCycle.YEARLY.value: 365,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _client() -> Any:
    """Only ever constructed after the caller has checked
    settings.razorpay_configured — this reads whatever key id/secret is
    currently set without validating them itself."""
    if razorpay is None:
        raise RuntimeError("razorpay package is not installed")
    return razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))


def _amount_paise(price: float) -> int:
    """Deterministic float-price -> integer-paise conversion. Goes through
    Decimal(str(price)) rather than price * 100 directly so a price like
    499.99 can never land on 49998 due to binary-float representation error."""
    return int((Decimal(str(price)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _plan_price(plan: Plan, billing_cycle: BillingCycle) -> float:
    return plan.price_monthly if billing_cycle == BillingCycle.MONTHLY else plan.price_yearly


# --------------------------------- order -----------------------------------


def create_order(
    db: Session, org: Organization, user: User, plan: Plan, billing_cycle: BillingCycle
) -> SubscriptionPayment:
    """Create the Razorpay order (via the official SDK) and the matching
    SubscriptionPayment row, in that order, so the order's `receipt` can be
    the payment row's own id without a two-phase insert-then-update.

    The amount is computed here, server-side, from `plan`'s own price —
    callers must never accept an amount from the client.
    """
    amount_paise = _amount_paise(_plan_price(plan, billing_cycle))
    payment_id = str(uuid.uuid4())

    client = _client()
    razorpay_order = client.order.create(
        data={
            "amount": amount_paise,
            "currency": "INR",
            "receipt": payment_id,
            "notes": {
                "organization_id": org.id,
                "plan_id": plan.id,
                "billing_cycle": billing_cycle.value,
            },
        }
    )

    payment = SubscriptionPayment(
        id=payment_id,
        organization_id=org.id,
        plan_id=plan.id,
        billing_cycle=billing_cycle.value,
        amount_paise=amount_paise,
        currency="INR",
        razorpay_order_id=razorpay_order["id"],
        status="created",
        created_by_user_id=user.id,
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)
    return payment


def get_payment_by_order_id(db: Session, razorpay_order_id: str) -> SubscriptionPayment | None:
    return (
        db.query(SubscriptionPayment)
        .filter(SubscriptionPayment.razorpay_order_id == razorpay_order_id)
        .first()
    )


# ------------------------------- signatures ---------------------------------


def verify_payment_signature(payload: RazorpayVerifyRequest) -> bool:
    """Official SDK utility only — never a hand-rolled HMAC comparison. Uses
    hmac.compare_digest internally (constant-time)."""
    client = _client()
    try:
        client.utility.verify_payment_signature(
            {
                "razorpay_order_id": payload.razorpay_order_id,
                "razorpay_payment_id": payload.razorpay_payment_id,
                "razorpay_signature": payload.razorpay_signature,
            }
        )
        return True
    except _SignatureVerificationError:
        return False


def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    """`raw_body` must be the exact, unparsed request bytes — Razorpay signs
    the literal wire bytes, not a re-serialized dict, so parsing JSON first
    and re-dumping it would make a valid signature fail to verify."""
    client = _client()
    try:
        client.utility.verify_webhook_signature(
            raw_body.decode("utf-8"), signature, settings.razorpay_webhook_secret
        )
        return True
    except _SignatureVerificationError:
        return False


# ------------------------------- activation ---------------------------------


def activate_subscription(
    db: Session,
    payment_id: str,
    razorpay_payment_id: str | None = None,
    actor: User | None = None,
    raw_event: dict | None = None,
) -> Organization:
    """The one shared activation path for both the verify endpoint and the
    webhook handler — never duplicate this logic at either call site.

    Idempotent: locks the SubscriptionPayment row first; if it's already
    "paid" (verify ran first, then the webhook arrived for the same payment,
    or the webhook was delivered twice), returns the already-activated
    organization untouched — no second expiry extension, no duplicate
    activity log entry.

    Organization.plan / .requested_plan / .theme are lazy="joined" at the
    mapper level, which would otherwise pull a LEFT OUTER JOIN into the
    locking SELECT — Postgres refuses FOR UPDATE on the nullable side of an
    outer join. lazyload() suppresses that default for this one query only,
    the same pattern used in app.services.stock_service.
    """
    payment = (
        db.query(SubscriptionPayment)
        .filter(SubscriptionPayment.id == payment_id)
        .with_for_update(nowait=False)
        .first()
    )
    if payment is None:
        raise ValueError(f"SubscriptionPayment {payment_id} not found")

    if payment.status == "paid":
        org = db.get(Organization, payment.organization_id)
        if org is None:
            raise ValueError(f"Organization {payment.organization_id} not found")
        return org

    plan = db.get(Plan, payment.plan_id)
    # A plan that's since been deactivated for *new* purchases doesn't
    # invalidate a payment that already happened — the org still gets what
    # it paid for. `plan` may be None here only if the row was deleted
    # outright, in which case plan_label below falls back to the raw id.

    payment.status = "paid"
    if razorpay_payment_id:
        payment.razorpay_payment_id = razorpay_payment_id
    payment.paid_at = _now()
    if raw_event is not None:
        payment.raw_event = raw_event

    org = (
        db.query(Organization)
        .filter(Organization.id == payment.organization_id)
        .options(
            lazyload(Organization.plan),
            lazyload(Organization.requested_plan),
            lazyload(Organization.theme),
        )
        .with_for_update(nowait=False)
        .first()
    )
    if org is None:
        raise ValueError(f"Organization {payment.organization_id} not found")

    # Same core activation semantics as org_service.approve_upgrade(), plus
    # what the manual flow doesn't need: billing_cycle from the payment (the
    # manual flow already set it at request-upgrade time) and plan_expires_at.
    org.plan_id = payment.plan_id
    org.billing_cycle = payment.billing_cycle
    org.status = OrganizationStatus.ACTIVE
    org.upgrade_status = UpgradeStatus.APPROVED.value
    org.upgrade_reject_reason = None
    org.requested_plan_id = None

    now = _now()
    current_expiry = org.plan_expires_at
    if current_expiry is not None and current_expiry.tzinfo is None:
        current_expiry = current_expiry.replace(tzinfo=timezone.utc)
    base = max(now, current_expiry) if current_expiry is not None else now
    period_days = PLAN_PERIOD_DAYS.get(payment.billing_cycle, 30)
    org.plan_expires_at = base + timedelta(days=period_days)

    plan_label = plan.name if plan is not None else payment.plan_id
    activity_service.record(
        db, org.id, actor, "billing", "Subscription payment completed",
        f"{plan_label} plan activated via Razorpay ({payment.billing_cycle} billing)",
    )

    db.commit()
    db.refresh(org)
    db.refresh(payment)
    return org


def mark_failed(db: Session, payment_id: str, reason: str | None, raw_event: dict | None) -> None:
    """Never downgrades an already-paid payment — a late/duplicate
    payment.failed event arriving after activation must not undo it."""
    payment = (
        db.query(SubscriptionPayment)
        .filter(SubscriptionPayment.id == payment_id)
        .with_for_update(nowait=False)
        .first()
    )
    if payment is None or payment.status == "paid":
        return
    payment.status = "failed"
    payment.failure_reason = reason
    payment.raw_event = raw_event
    db.commit()


# --------------------------------- webhook -----------------------------------


def extract_order_id(event: dict) -> str | None:
    payload = event.get("payload", {}) or {}
    order_entity = ((payload.get("order") or {}).get("entity")) or {}
    if order_entity.get("id"):
        return order_entity["id"]
    payment_entity = ((payload.get("payment") or {}).get("entity")) or {}
    return payment_entity.get("order_id")


def extract_payment_id(event: dict) -> str | None:
    payload = event.get("payload", {}) or {}
    payment_entity = ((payload.get("payment") or {}).get("entity")) or {}
    return payment_entity.get("id")


def extract_failure_reason(event: dict) -> str | None:
    payload = event.get("payload", {}) or {}
    payment_entity = ((payload.get("payment") or {}).get("entity")) or {}
    return payment_entity.get("error_description") or payment_entity.get("error_reason")


# ----------------------------------- history ---------------------------------


def list_payments(db: Session, organization_id: str | None = None) -> list[SubscriptionPaymentOut]:
    """GET /billing/payments (organization_id always the caller's own org —
    enforced by the router, never client-supplied) and
    GET /superadmin/subscription-payments (organization_id optional —
    None lists across every organization)."""
    query = db.query(SubscriptionPayment, Plan.name).outerjoin(Plan, Plan.id == SubscriptionPayment.plan_id)
    if organization_id is not None:
        query = query.filter(SubscriptionPayment.organization_id == organization_id)
    rows = query.order_by(SubscriptionPayment.created_at.desc()).all()
    return [
        SubscriptionPaymentOut(
            id=payment.id,
            organization_id=payment.organization_id,
            plan_id=payment.plan_id,
            plan_name=plan_name,
            billing_cycle=payment.billing_cycle,
            amount_paise=payment.amount_paise,
            currency=payment.currency,
            razorpay_order_id=payment.razorpay_order_id,
            razorpay_payment_id=payment.razorpay_payment_id,
            status=payment.status,
            failure_reason=payment.failure_reason,
            created_at=payment.created_at,
            paid_at=payment.paid_at,
        )
        for payment, plan_name in rows
    ]
