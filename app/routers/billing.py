import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import require_system_role
from app.models import Organization, Plan, SystemRole, User
from app.schemas.organization import OrganizationOut
from app.schemas.razorpay import (
    RazorpayOrderRequest,
    RazorpayOrderResponse,
    RazorpayPrefill,
    RazorpayVerifyRequest,
    SubscriptionPaymentOut,
)
from app.services import billing_service

logger = logging.getLogger("crm.billing")

router = APIRouter(prefix="/billing", tags=["billing"])

_ADMIN = require_system_role(SystemRole.ADMIN)

_NOT_CONFIGURED = HTTPException(
    status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Online payment not configured"
)


def _require_org(user: User) -> Organization:
    if user.organization is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No organization")
    return user.organization


@router.post("/razorpay/order", response_model=RazorpayOrderResponse)
def create_razorpay_order(
    payload: RazorpayOrderRequest,
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> RazorpayOrderResponse:
    """Start an online plan-payment attempt. The manual flow
    (POST /organizations/upgrade-request) remains available regardless of
    whether Razorpay is configured — this is an additional path, not a
    replacement."""
    if not settings.razorpay_configured:
        raise _NOT_CONFIGURED

    org = _require_org(admin)

    plan = db.get(Plan, payload.plan_id)
    if plan is None or not plan.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Selected plan does not exist or is no longer available",
        )

    payment = billing_service.create_order(db, org, admin, plan, payload.billing_cycle)

    return RazorpayOrderResponse(
        order_id=payment.razorpay_order_id,
        amount=payment.amount_paise,
        currency=payment.currency,
        key_id=settings.razorpay_key_id,
        plan_name=plan.name,
        billing_cycle=payload.billing_cycle,
        prefill=RazorpayPrefill(name=admin.name, email=admin.email, contact=admin.phone),
    )


@router.post("/razorpay/verify", response_model=OrganizationOut)
def verify_razorpay_payment(
    payload: RazorpayVerifyRequest,
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> Organization:
    if not settings.razorpay_configured:
        raise _NOT_CONFIGURED

    org = _require_org(admin)

    payment = billing_service.get_payment_by_order_id(db, payload.razorpay_order_id)
    # Same generic rejection whether the order doesn't exist at all or
    # belongs to another organization — never confirm to the caller which
    # case it is (tenant isolation must not leak via the error message).
    if payment is None or payment.organization_id != org.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid payment")

    if not billing_service.verify_payment_signature(payload):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid payment signature")

    return billing_service.activate_subscription(
        db, payment.id, razorpay_payment_id=payload.razorpay_payment_id, actor=admin
    )


@router.post("/razorpay/webhook", status_code=status.HTTP_200_OK)
async def razorpay_webhook(request: Request, db: Session = Depends(get_db)) -> dict:
    """No application authentication — Razorpay calls this directly.
    Authenticated purely by the X-Razorpay-Signature header against
    RAZORPAY_WEBHOOK_SECRET. The raw body is read and signature-checked
    BEFORE any JSON parsing, since Razorpay signs the literal wire bytes."""
    if not settings.razorpay_configured:
        raise _NOT_CONFIGURED

    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    if not billing_service.verify_webhook_signature(raw_body, signature):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid webhook signature")

    try:
        event = json.loads(raw_body)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid webhook payload")

    event_type = event.get("event")

    if event_type in ("order.paid", "payment.captured"):
        order_id = billing_service.extract_order_id(event)
        payment = billing_service.get_payment_by_order_id(db, order_id) if order_id else None
        if payment is not None:
            billing_service.activate_subscription(
                db, payment.id,
                razorpay_payment_id=billing_service.extract_payment_id(event),
                actor=None,
                raw_event=event,
            )
        else:
            logger.warning("Razorpay webhook %s: no matching subscription_payments row for order_id=%s", event_type, order_id)

    elif event_type == "payment.failed":
        order_id = billing_service.extract_order_id(event)
        payment = billing_service.get_payment_by_order_id(db, order_id) if order_id else None
        if payment is not None:
            billing_service.mark_failed(
                db, payment.id, billing_service.extract_failure_reason(event), event
            )

    # Any other valid-signature event: acknowledged and ignored.
    return {"status": "ok"}


from app.schemas.pagination import PaginatedResponse


@router.get("/payments", response_model=PaginatedResponse[SubscriptionPaymentOut])
def list_payments(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    search: str | None = Query(default=None),
    status: str | None = Query(default=None),
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> PaginatedResponse[SubscriptionPaymentOut]:
    """Only the authenticated admin's own organization's payments — never
    scoped by a client-supplied organization_id."""
    org = _require_org(admin)
    return billing_service.list_payments(
        db,
        organization_id=org.id,
        page=page,
        page_size=page_size,
        search=search,
        status=status,
    )
