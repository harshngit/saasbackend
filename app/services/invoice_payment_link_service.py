"""Invoice payment link & organization payment gateway service (Razorpay Part B).

Enables each organization to accept invoice payments using their own Razorpay account.
"""

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

import razorpay
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.encryption import decrypt_field, encrypt_field
from app.models import (
    Customer,
    Invoice,
    InvoicePaymentLink,
    OrgPaymentGateway,
    Organization,
    User,
)
from app.schemas.payment_gateway import (
    InvoicePaymentLinkCreate,
    InvoicePaymentLinkOut,
    PaymentGatewayIn,
    PaymentGatewayOut,
    REQUIRED_WEBHOOK_EVENTS,
)
from app.services import activity_service, notification_service, payment_service

logger = logging.getLogger("crm.invoice_payment_link")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _amount_paise(price: float) -> int:
    """Convert float INR price to integer paise deterministically."""
    return int((Decimal(str(price)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _webhook_url(org_id: str) -> str:
    base = settings.public_base_url.rstrip("/") if settings.public_base_url else ""
    return f"{base}/payments/razorpay/webhook/{org_id}"


# ---------------------------------------------------------------------------
# Organization Gateway Management
# ---------------------------------------------------------------------------


def get_gateway(db: Session, org_id: str) -> OrgPaymentGateway | None:
    return db.query(OrgPaymentGateway).filter(OrgPaymentGateway.organization_id == org_id).first()


def format_gateway_out(gateway: OrgPaymentGateway | None, org_id: str) -> PaymentGatewayOut:
    if gateway is None or not gateway.is_active:
        return PaymentGatewayOut(
            key_id=None,
            mode=None,
            is_active=False,
            verified_at=None,
            configured=False,
            webhook_url=_webhook_url(org_id),
            required_events=list(REQUIRED_WEBHOOK_EVENTS),
        )
    return PaymentGatewayOut(
        key_id=gateway.key_id,
        mode=gateway.mode,
        is_active=gateway.is_active,
        verified_at=gateway.verified_at,
        configured=True,
        webhook_url=_webhook_url(org_id),
        required_events=list(REQUIRED_WEBHOOK_EVENTS),
    )


def upsert_gateway(db: Session, org_id: str, payload: PaymentGatewayIn) -> OrgPaymentGateway:
    """Create or update the organization's Razorpay gateway credentials."""
    gateway = get_gateway(db, org_id)

    key_id = payload.key_id.strip()
    mode = "test" if key_id.startswith("rzp_test_") else "live"

    if gateway is None:
        # Creating a new gateway requires both secrets
        if not payload.key_secret or not payload.webhook_secret:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Both key_secret and webhook_secret are required to configure a new gateway",
            )
        gateway = OrgPaymentGateway(
            organization_id=org_id,
            provider="razorpay",
            key_id=key_id,
            key_secret_encrypted=encrypt_field(payload.key_secret.strip()),
            webhook_secret_encrypted=encrypt_field(payload.webhook_secret.strip()),
            mode=mode,
            is_active=True,
            verified_at=None,
        )
        db.add(gateway)
    else:
        # Updating existing gateway
        creds_changed = False
        if gateway.key_id != key_id:
            gateway.key_id = key_id
            gateway.mode = mode
            creds_changed = True

        if payload.key_secret and payload.key_secret.strip():
            gateway.key_secret_encrypted = encrypt_field(payload.key_secret.strip())
            creds_changed = True

        if payload.webhook_secret and payload.webhook_secret.strip():
            gateway.webhook_secret_encrypted = encrypt_field(payload.webhook_secret.strip())
            creds_changed = True

        gateway.is_active = True
        if creds_changed:
            gateway.verified_at = None

    db.commit()
    db.refresh(gateway)
    return gateway


def delete_gateway(db: Session, org_id: str) -> None:
    """Remove/deactivate the organization's gateway.

    Preserves all historical payment link rows.
    """
    gateway = get_gateway(db, org_id)
    if gateway is not None:
        db.delete(gateway)
        db.commit()


def get_org_razorpay_client(gateway: OrgPaymentGateway) -> razorpay.Client:
    """Initialize a Razorpay SDK client using the organization's decrypted credentials."""
    decrypted_key_secret = decrypt_field(gateway.key_secret_encrypted)
    return razorpay.Client(auth=(gateway.key_id, decrypted_key_secret))


def test_gateway(db: Session, org_id: str) -> OrgPaymentGateway:
    """Test the organization's stored Razorpay credentials with a harmless probe."""
    gateway = get_gateway(db, org_id)
    if gateway is None or not gateway.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No active payment gateway configured",
        )

    try:
        client = get_org_razorpay_client(gateway)
        client.payment_link.all({"count": 1})
    except Exception as exc:
        logger.warning("Razorpay gateway test failed for org %s: %s", org_id, exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid key id/secret",
        )

    gateway.verified_at = _now()
    db.commit()
    db.refresh(gateway)
    return gateway


# ---------------------------------------------------------------------------
# Invoice Payment Links
# ---------------------------------------------------------------------------


def to_payment_link_out(link: InvoicePaymentLink) -> InvoicePaymentLinkOut:
    return InvoicePaymentLinkOut(
        id=link.id,
        invoice_id=link.invoice_id,
        razorpay_link_id=link.razorpay_link_id,
        short_url=link.short_url,
        amount=round(link.amount_paise / 100.0, 2),
        amount_paid=round(link.amount_paid_paise / 100.0, 2),
        currency=link.currency,
        status=link.status,
        expire_by=link.expire_by,
        notify_sms=link.notify_sms,
        notify_email=link.notify_email,
        created_at=link.created_at,
        paid_at=link.paid_at,
    )


def create_payment_link(
    db: Session,
    org_id: str,
    user: User,
    invoice: Invoice,
    payload: InvoicePaymentLinkCreate,
) -> InvoicePaymentLink:
    """Create a new Razorpay Payment Link for the invoice."""
    gateway = get_gateway(db, org_id)
    if gateway is None or not gateway.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Connect Razorpay in Settings first",
        )

    if invoice.status == "cancelled" or (invoice.invoice_status and invoice.invoice_status.lower() == "cancelled"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot create payment link for a cancelled invoice",
        )

    if invoice.is_credit_note:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Credit notes cannot have payment links",
        )

    due = payment_service.outstanding(invoice)
    if due <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invoice is already fully paid",
        )

    requested_amt = payload.amount if payload.amount is not None else due
    if requested_amt <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Payment amount must be greater than zero",
        )

    if round(requested_amt, 2) > round(due + 0.01, 2):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Amount ({requested_amt:.2f}) exceeds invoice outstanding balance ({due:.2f})",
        )

    client = get_org_razorpay_client(gateway)

    # Check for existing active link (created or partially_paid)
    active_links = (
        db.query(InvoicePaymentLink)
        .filter(
            InvoicePaymentLink.invoice_id == invoice.id,
            InvoicePaymentLink.organization_id == org_id,
            InvoicePaymentLink.status.in_(["created", "partially_paid"]),
        )
        .all()
    )

    for old_link in active_links:
        try:
            client.payment_link.cancel(old_link.razorpay_link_id)
        except Exception as exc:
            logger.warning("Could not cancel previous Razorpay link %s: %s", old_link.razorpay_link_id, exc)
        old_link.status = "cancelled"

    amount_paise = _amount_paise(requested_amt)
    local_link_id = str(uuid.uuid4())
    org = db.get(Organization, org_id)
    org_name = org.name if org else "Firm"
    description = f"Invoice {invoice.invoice_number} – {org_name}"

    # Customer info
    cust = invoice.customer
    customer_name = cust.name if cust else (invoice.walk_in_name or "Customer")
    customer_contact = cust.phone if cust else (invoice.walk_in_phone or "")
    customer_email = cust.email if cust else ""

    customer_dict: dict[str, str] = {"name": customer_name}
    if customer_contact:
        customer_dict["contact"] = customer_contact
    if customer_email:
        customer_dict["email"] = customer_email

    expire_by = _now() + timedelta(days=payload.expire_in_days)
    expire_by_ts = int(expire_by.timestamp())

    random_suffix = uuid.uuid4().hex[:6]
    reference_id = f"{invoice.invoice_number}-{random_suffix}"

    try:
        rzp_link = client.payment_link.create(
            data={
                "amount": amount_paise,
                "currency": "INR",
                "accept_partial": False,
                "description": description,
                "customer": customer_dict,
                "notify": {
                    "sms": payload.notify_sms,
                    "email": payload.notify_email,
                },
                "reminder_enable": True,
                "expire_by": expire_by_ts,
                "reference_id": reference_id,
                "notes": {
                    "organization_id": org_id,
                    "invoice_id": invoice.id,
                    "link_id": local_link_id,
                },
            }
        )
    except Exception as exc:
        logger.error("Razorpay link creation failed for invoice %s: %s", invoice.id, exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Razorpay link creation failed: {exc}",
        )

    link = InvoicePaymentLink(
        id=local_link_id,
        organization_id=org_id,
        invoice_id=invoice.id,
        razorpay_link_id=rzp_link["id"],
        short_url=rzp_link.get("short_url") or "",
        amount_paise=amount_paise,
        amount_paid_paise=0,
        currency="INR",
        status="created",
        expire_by=expire_by,
        notify_sms=payload.notify_sms,
        notify_email=payload.notify_email,
        created_by_user_id=user.id if user else None,
        created_at=_now(),
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    return link


def list_invoice_payment_links(db: Session, org_id: str, invoice_id: str) -> list[InvoicePaymentLink]:
    return (
        db.query(InvoicePaymentLink)
        .filter(
            InvoicePaymentLink.invoice_id == invoice_id,
            InvoicePaymentLink.organization_id == org_id,
        )
        .order_by(InvoicePaymentLink.created_at.desc())
        .all()
    )


def cancel_payment_link(db: Session, org_id: str, invoice: Invoice, link_id: str) -> InvoicePaymentLink:
    """Cancel an unpaid active payment link."""
    link = db.get(InvoicePaymentLink, link_id)
    if link is None or link.organization_id != org_id or link.invoice_id != invoice.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment link not found")

    if link.status in ("paid", "cancelled", "expired"):
        return link

    gateway = get_gateway(db, org_id)
    if gateway is not None and gateway.is_active:
        client = get_org_razorpay_client(gateway)
        try:
            client.payment_link.cancel(link.razorpay_link_id)
        except Exception as exc:
            logger.warning("Error cancelling link %s on Razorpay: %s", link.razorpay_link_id, exc)

    link.status = "cancelled"
    db.commit()
    db.refresh(link)
    return link


def record_online_payment(
    db: Session,
    link_id: str,
    razorpay_payment_id: str,
    captured_at: datetime | None,
    amount_inr: float,
    raw_event: dict | None = None,
) -> InvoicePaymentLink:
    """Canonical payment recording path for Razorpay invoice settlements.

    Idempotent: locks the payment link row and executes only once.
    Calls payment_service.record() to settle the invoice, update customer balance,
    generate receipt, record activity, and dispatch notifications.
    """
    link = (
        db.query(InvoicePaymentLink)
        .filter(InvoicePaymentLink.id == link_id)
        .with_for_update(nowait=False)
        .first()
    )
    if link is None:
        raise ValueError(f"InvoicePaymentLink {link_id} not found")

    if link.status == "paid":
        return link  # Already recorded idempotently

    invoice = db.get(Invoice, link.invoice_id)
    if invoice is None or invoice.organization_id != link.organization_id:
        raise ValueError(f"Invoice {link.invoice_id} not found")

    captured_time = captured_at or _now()
    amount_paid_paise = _amount_paise(amount_inr)

    # 1. Update link state
    link.status = "paid"
    link.paid_at = captured_time
    link.amount_paid_paise = amount_paid_paise
    if raw_event:
        link.raw_event = raw_event

    # 2. Record through canonical payment pipeline
    payment_service.record(
        db,
        org_id=link.organization_id,
        customer=invoice.customer,
        invoice=invoice,
        amount=amount_inr,
        payment_mode="Online – Razorpay",
        reference=razorpay_payment_id,
        received_on=captured_time,
    )

    # 3. Activity Feed Entry
    activity_service.record(
        db,
        organization_id=link.organization_id,
        actor=None,
        type="payment",
        title=f"₹{amount_inr:,.2f} received for Invoice #{invoice.invoice_number} via Razorpay",
        description=f"Online payment of ₹{amount_inr:,.2f} captured via Razorpay (Payment ID: {razorpay_payment_id})",
    )

    # 4. In-App Notification to Org Admins
    notification_service.notify_org_admins(
        db,
        organization_id=link.organization_id,
        title=f"₹{amount_inr:,.2f} received for Invoice #{invoice.invoice_number} via Razorpay",
        body=f"Payment ID: {razorpay_payment_id}",
        type="success",
        link=f"/invoices/{invoice.id}",
    )

    db.commit()
    db.refresh(link)
    return link


def refresh_payment_link(db: Session, org_id: str, invoice: Invoice, link_id: str) -> InvoicePaymentLink:
    """Fetch latest payment link status from Razorpay and reconcile locally."""
    link = db.get(InvoicePaymentLink, link_id)
    if link is None or link.organization_id != org_id or link.invoice_id != invoice.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment link not found")

    gateway = get_gateway(db, org_id)
    if gateway is None or not gateway.is_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Connect Razorpay in Settings first")

    client = get_org_razorpay_client(gateway)
    try:
        rzp_link = client.payment_link.fetch(link.razorpay_link_id)
    except Exception as exc:
        logger.error("Error fetching link %s from Razorpay: %s", link.razorpay_link_id, exc)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Failed to fetch from Razorpay: {exc}")

    rzp_status = rzp_link.get("status")
    amount_paid_paise = rzp_link.get("amount_paid", 0)

    if rzp_status == "paid":
        # Extract payment id
        payments_list = rzp_link.get("payments") or []
        payment_id = None
        captured_time = None
        if payments_list and isinstance(payments_list, list):
            first_pay = payments_list[0]
            if isinstance(first_pay, dict):
                payment_id = first_pay.get("payment_id") or first_pay.get("id")
                created_epoch = first_pay.get("created_at")
                if created_epoch:
                    captured_time = datetime.fromtimestamp(created_epoch, tz=timezone.utc)
            elif isinstance(first_pay, str):
                payment_id = first_pay

        if not payment_id:
            payment_id = f"pay_link_{link.razorpay_link_id}"

        amount_inr = round((amount_paid_paise or link.amount_paise) / 100.0, 2)
        return record_online_payment(
            db,
            link_id=link.id,
            razorpay_payment_id=payment_id,
            captured_at=captured_time,
            amount_inr=amount_inr,
            raw_event=rzp_link,
        )

    if rzp_status in ("partially_paid", "expired", "cancelled"):
        link.status = rzp_status
        link.amount_paid_paise = amount_paid_paise
        db.commit()
        db.refresh(link)

    return link


def verify_org_webhook_signature(raw_body: bytes, signature: str, webhook_secret: str) -> bool:
    """Verify X-Razorpay-Signature against the organization's decrypted webhook secret."""
    try:
        client = razorpay.Client(auth=("test", "test"))
        client.utility.verify_webhook_signature(raw_body.decode("utf-8"), signature, webhook_secret)
        return True
    except razorpay.errors.SignatureVerificationError:
        return False


def process_webhook(db: Session, organization_id: str, raw_body: bytes, signature: str) -> dict:
    """Handle incoming Razorpay webhooks for an organization's invoice payment links."""
    gateway = get_gateway(db, organization_id)
    if gateway is None or not gateway.is_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Organization gateway not found or inactive")

    webhook_secret = decrypt_field(gateway.webhook_secret_encrypted)
    if not verify_org_webhook_signature(raw_body, signature, webhook_secret):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid webhook signature")

    try:
        event = json.loads(raw_body)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid webhook payload")

    event_type = event.get("event")
    payload = event.get("payload", {}) or {}
    plink_entity = ((payload.get("payment_link") or {}).get("entity")) or {}
    razorpay_link_id = plink_entity.get("id")

    if not razorpay_link_id:
        return {"status": "ok"}

    link = (
        db.query(InvoicePaymentLink)
        .filter(
            InvoicePaymentLink.razorpay_link_id == razorpay_link_id,
            InvoicePaymentLink.organization_id == organization_id,
        )
        .first()
    )
    if link is None:
        logger.warning("Payment link %s not found for organization %s", razorpay_link_id, organization_id)
        return {"status": "ok"}

    if event_type == "payment_link.paid":
        payment_entity = ((payload.get("payment") or {}).get("entity")) or {}
        payment_id = payment_entity.get("id") or f"pay_{uuid.uuid4().hex[:8]}"
        created_epoch = payment_entity.get("created_at")
        captured_at = (
            datetime.fromtimestamp(created_epoch, tz=timezone.utc)
            if created_epoch
            else _now()
        )
        amount_paise = payment_entity.get("amount") or plink_entity.get("amount_paid") or link.amount_paise
        amount_inr = round(amount_paise / 100.0, 2)

        record_online_payment(
            db,
            link_id=link.id,
            razorpay_payment_id=payment_id,
            captured_at=captured_at,
            amount_inr=amount_inr,
            raw_event=event,
        )

    elif event_type == "payment_link.partially_paid":
        amount_paid_paise = plink_entity.get("amount_paid", link.amount_paid_paise)
        link.status = "partially_paid"
        link.amount_paid_paise = amount_paid_paise
        link.raw_event = event
        db.commit()

    elif event_type == "payment_link.expired":
        if link.status != "paid":
            link.status = "expired"
            link.raw_event = event
            db.commit()

    elif event_type == "payment_link.cancelled":
        if link.status != "paid":
            link.status = "cancelled"
            link.raw_event = event
            db.commit()

    return {"status": "ok"}
