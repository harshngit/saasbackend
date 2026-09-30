"""Razorpay Phase 1 — SaaS plan payments (TEST MODE).

Covers: order creation (server-side pricing, tenant isolation), payment
verification (signature check, cross-tenant block, idempotency), the webhook
(signature check, order.paid/payment.captured/payment.failed/unknown events,
duplicate delivery), plan_expires_at extension semantics, lazy plan-expiry
lockout, organization response fields, payment history (org-scoped and Super
Admin), activity logging, and that the existing manual upgrade flow is
unaffected.

No real Razorpay API/network calls are made: app.services.billing_service._client
is monkeypatched to a fake client whose .order.create() and .utility.verify_*
are fully local and deterministic.
"""

import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import razorpay
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.database import SessionLocal
from app.main import app
from app.models import ActivityLog, Organization, SubscriptionPayment, User
from app.seed import main as seed_main
from app.services import billing_service

seed_main()
client = TestClient(app)

_VALID_SIGNATURE = "valid-signature"


# --------------------------------- fixtures ---------------------------------


def _register_org(label: str) -> tuple[dict, str]:
    email = f"{label}_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={
        "organization_name": f"{label} {uuid.uuid4().hex[:6]}",
        "admin_name": "Owner",
        "email": email,
        "password": "Password123!",
        "role": "admin",
    })
    assert r.status_code == 201, r.text
    token = r.json()["tokens"]["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    db = SessionLocal()
    try:
        org_id = db.query(User).filter(User.email == email).first().organization_id
    finally:
        db.close()
    return headers, org_id


def _make_staff(owner_headers: dict, role: str = "sales_officer") -> dict:
    email = f"staff_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/users", json={
        "name": "Staff", "email": email, "password": "Password123!", "role": role,
    }, headers=owner_headers)
    assert r.status_code == 201, r.text
    r2 = client.post("/auth/login", json={"email": email, "password": "Password123!"})
    assert r2.status_code == 200, r2.text
    return {"Authorization": f"Bearer {r2.json()['tokens']['access_token']}"}


def _super_admin_auth() -> dict:
    r = client.post("/auth/login", json={
        "email": settings.super_admin_email, "password": settings.super_admin_password,
    })
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}


def _create_plan(root_auth: dict, monthly: float = 499.0, yearly: float = 4999.0, is_active: bool = True) -> tuple[str, str]:
    name = f"Plan_{uuid.uuid4().hex[:8]}"
    r = client.post("/superadmin/plans", json={
        "name": name, "price_monthly": monthly, "price_yearly": yearly, "features": [],
    }, headers=root_auth)
    assert r.status_code == 201, r.text
    plan_id = r.json()["id"]
    if not is_active:
        r2 = client.patch(f"/superadmin/plans/{plan_id}/status", json={"is_active": False}, headers=root_auth)
        assert r2.status_code == 200, r2.text
    return plan_id, name


class _FakeOrderAPI:
    def __init__(self):
        self.last_create_kwargs: dict | None = None

    def create(self, data=None, **kwargs):
        self.last_create_kwargs = data
        return {
            "id": f"order_{data['receipt']}",
            "amount": data["amount"],
            "currency": data["currency"],
            "receipt": data["receipt"],
            "notes": data.get("notes", {}),
            "status": "created",
        }


class _FakeUtilityAPI:
    def verify_payment_signature(self, parameters):
        if parameters.get("razorpay_signature") != _VALID_SIGNATURE:
            raise razorpay.errors.SignatureVerificationError("Razorpay Signature Verification Failed")
        return True

    def verify_webhook_signature(self, body, signature, secret):
        if signature != _VALID_SIGNATURE:
            raise razorpay.errors.SignatureVerificationError("Razorpay Signature Verification Failed")
        return True


class _FakeRazorpayClient:
    def __init__(self, auth=None):
        self.auth = auth
        self.order = _FakeOrderAPI()
        self.utility = _FakeUtilityAPI()


def _configure_razorpay(monkeypatch) -> _FakeRazorpayClient:
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_fake_key_id")
    monkeypatch.setattr(settings, "razorpay_key_secret", "fake_key_secret_never_returned")
    monkeypatch.setattr(settings, "razorpay_webhook_secret", "fake_webhook_secret_never_returned")
    fake = _FakeRazorpayClient()
    monkeypatch.setattr(billing_service, "_client", lambda: fake)
    return fake


def _webhook_post(event: dict, signature: str = _VALID_SIGNATURE):
    return client.post(
        "/billing/razorpay/webhook",
        content=json.dumps(event),
        headers={"Content-Type": "application/json", "X-Razorpay-Signature": signature},
    )


def _order_paid_event(order_id: str, payment_id: str) -> dict:
    return {
        "event": "order.paid",
        "payload": {
            "order": {"entity": {"id": order_id}},
            "payment": {"entity": {"id": payment_id, "order_id": order_id}},
        },
    }


def _payment_captured_event(order_id: str, payment_id: str) -> dict:
    return {
        "event": "payment.captured",
        "payload": {"payment": {"entity": {"id": payment_id, "order_id": order_id, "status": "captured"}}},
    }


def _payment_failed_event(order_id: str, payment_id: str, reason: str = "card_declined") -> dict:
    return {
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {"id": payment_id, "order_id": order_id, "status": "failed", "error_description": reason},
            },
        },
    }


def _org_row(org_id: str) -> Organization:
    db = SessionLocal()
    try:
        return db.get(Organization, org_id)
    finally:
        db.close()


def _payment_row(razorpay_order_id: str) -> SubscriptionPayment | None:
    db = SessionLocal()
    try:
        return (
            db.query(SubscriptionPayment)
            .filter(SubscriptionPayment.razorpay_order_id == razorpay_order_id)
            .first()
        )
    finally:
        db.close()


# ----------------------------- 1. configuration gate -------------------------


def test_order_returns_503_when_razorpay_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "razorpay_key_id", "")
    monkeypatch.setattr(settings, "razorpay_key_secret", "")
    monkeypatch.setattr(settings, "razorpay_webhook_secret", "")
    assert settings.razorpay_configured is False

    headers, _ = _register_org("no_razorpay")
    root_auth = _super_admin_auth()
    plan_id, _ = _create_plan(root_auth)

    r = client.post("/billing/razorpay/order", json={"plan_id": plan_id, "billing_cycle": "monthly"}, headers=headers)
    assert r.status_code == 503
    assert r.json()["detail"] == "Online payment not configured"


# ------------------------------- 2. permissions -------------------------------


def test_staff_cannot_create_order(monkeypatch):
    fake = _configure_razorpay(monkeypatch)
    owner_headers, _ = _register_org("staff_order")
    staff_headers = _make_staff(owner_headers)
    root_auth = _super_admin_auth()
    plan_id, _ = _create_plan(root_auth)

    r = client.post("/billing/razorpay/order", json={"plan_id": plan_id, "billing_cycle": "monthly"}, headers=staff_headers)
    assert r.status_code == 403
    assert fake.order.last_create_kwargs is None  # never even reached the SDK


# --------------------------------- 3-4. validation ----------------------------


def test_inactive_plan_rejected(monkeypatch):
    _configure_razorpay(monkeypatch)
    headers, _ = _register_org("inactive_plan")
    root_auth = _super_admin_auth()
    plan_id, _ = _create_plan(root_auth, is_active=False)

    r = client.post("/billing/razorpay/order", json={"plan_id": plan_id, "billing_cycle": "monthly"}, headers=headers)
    assert r.status_code == 400


def test_invalid_billing_cycle_rejected(monkeypatch):
    _configure_razorpay(monkeypatch)
    headers, _ = _register_org("bad_cycle")
    root_auth = _super_admin_auth()
    plan_id, _ = _create_plan(root_auth)

    r = client.post("/billing/razorpay/order", json={"plan_id": plan_id, "billing_cycle": "weekly"}, headers=headers)
    assert r.status_code == 422


# --------------------------- 5-10. order creation / response -----------------


def test_monthly_amount_from_server_side_price(monkeypatch):
    _configure_razorpay(monkeypatch)
    headers, _ = _register_org("monthly_amount")
    root_auth = _super_admin_auth()
    plan_id, _ = _create_plan(root_auth, monthly=499.0, yearly=4999.0)

    r = client.post("/billing/razorpay/order", json={"plan_id": plan_id, "billing_cycle": "monthly"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["amount"] == 49900


def test_yearly_amount_from_server_side_price(monkeypatch):
    _configure_razorpay(monkeypatch)
    headers, _ = _register_org("yearly_amount")
    root_auth = _super_admin_auth()
    plan_id, _ = _create_plan(root_auth, monthly=499.0, yearly=4999.0)

    r = client.post("/billing/razorpay/order", json={"plan_id": plan_id, "billing_cycle": "yearly"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["amount"] == 499900


def test_client_amount_field_ignored(monkeypatch):
    _configure_razorpay(monkeypatch)
    headers, _ = _register_org("client_amount")
    root_auth = _super_admin_auth()
    plan_id, _ = _create_plan(root_auth, monthly=499.0, yearly=4999.0)

    r = client.post(
        "/billing/razorpay/order",
        json={"plan_id": plan_id, "billing_cycle": "monthly", "amount": 1},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["amount"] == 49900  # not 1 — the client-supplied value was never read


def test_razorpay_order_created_with_correct_amount_currency_receipt_notes(monkeypatch):
    fake = _configure_razorpay(monkeypatch)
    headers, org_id = _register_org("order_notes")
    root_auth = _super_admin_auth()
    plan_id, _ = _create_plan(root_auth, monthly=499.0, yearly=4999.0)

    r = client.post("/billing/razorpay/order", json={"plan_id": plan_id, "billing_cycle": "monthly"}, headers=headers)
    assert r.status_code == 200, r.text

    sent = fake.order.last_create_kwargs
    assert sent["amount"] == 49900
    assert sent["currency"] == "INR"
    assert sent["receipt"]  # the local payment row's id
    assert sent["notes"]["organization_id"] == org_id
    assert sent["notes"]["plan_id"] == plan_id
    assert sent["notes"]["billing_cycle"] == "monthly"


def test_order_response_shape(monkeypatch):
    _configure_razorpay(monkeypatch)
    headers, _ = _register_org("order_shape")
    root_auth = _super_admin_auth()
    plan_id, plan_name = _create_plan(root_auth)

    r = client.post("/billing/razorpay/order", json={"plan_id": plan_id, "billing_cycle": "monthly"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    for field in ("order_id", "amount", "currency", "key_id", "plan_name", "billing_cycle", "prefill"):
        assert field in body, field
    assert body["plan_name"] == plan_name
    assert body["billing_cycle"] == "monthly"
    assert body["key_id"] == "rzp_test_fake_key_id"
    assert set(body["prefill"].keys()) == {"name", "email", "contact"}


def test_order_response_excludes_secrets(monkeypatch):
    _configure_razorpay(monkeypatch)
    headers, _ = _register_org("order_no_secrets")
    root_auth = _super_admin_auth()
    plan_id, _ = _create_plan(root_auth)

    r = client.post("/billing/razorpay/order", json={"plan_id": plan_id, "billing_cycle": "monthly"}, headers=headers)
    assert r.status_code == 200, r.text
    body_text = r.text
    assert "fake_key_secret_never_returned" not in body_text
    assert "fake_webhook_secret_never_returned" not in body_text


# --------------------------------- 11-13. verify ------------------------------


def test_valid_signature_activates_subscription(monkeypatch):
    _configure_razorpay(monkeypatch)
    headers, org_id = _register_org("verify_activates")
    root_auth = _super_admin_auth()
    plan_id, _ = _create_plan(root_auth)

    order = client.post("/billing/razorpay/order", json={"plan_id": plan_id, "billing_cycle": "monthly"}, headers=headers).json()
    rzp_payment_id = f"pay_{uuid.uuid4().hex[:10]}"
    v = client.post("/billing/razorpay/verify", json={
        "razorpay_order_id": order["order_id"],
        "razorpay_payment_id": rzp_payment_id,
        "razorpay_signature": _VALID_SIGNATURE,
    }, headers=headers)
    assert v.status_code == 200, v.text
    assert v.json()["status"] == "active"
    assert v.json()["plan_id"] == plan_id

    org = _org_row(org_id)
    assert org.status.value == "active"
    assert org.plan_id == plan_id
    assert org.plan_expires_at is not None

    payment = _payment_row(order["order_id"])
    assert payment.status == "paid"
    assert payment.razorpay_payment_id == rzp_payment_id


def test_invalid_signature_no_state_change(monkeypatch):
    _configure_razorpay(monkeypatch)
    headers, org_id = _register_org("verify_invalid_sig")
    root_auth = _super_admin_auth()
    plan_id, _ = _create_plan(root_auth)

    order = client.post("/billing/razorpay/order", json={"plan_id": plan_id, "billing_cycle": "monthly"}, headers=headers).json()
    org_before = _org_row(org_id)

    v = client.post("/billing/razorpay/verify", json={
        "razorpay_order_id": order["order_id"],
        "razorpay_payment_id": f"pay_{uuid.uuid4().hex[:10]}",
        "razorpay_signature": "tampered-signature",
    }, headers=headers)
    assert v.status_code == 400

    org_after = _org_row(org_id)
    assert org_after.status == org_before.status
    assert org_after.plan_id == org_before.plan_id
    assert org_after.plan_expires_at == org_before.plan_expires_at

    payment = _payment_row(order["order_id"])
    assert payment.status == "created"  # unchanged


def test_cross_organization_verification_blocked(monkeypatch):
    _configure_razorpay(monkeypatch)
    headers_a, org_a = _register_org("cross_org_a")
    headers_b, org_b = _register_org("cross_org_b")
    root_auth = _super_admin_auth()
    plan_id, _ = _create_plan(root_auth)

    order = client.post("/billing/razorpay/order", json={"plan_id": plan_id, "billing_cycle": "monthly"}, headers=headers_a).json()

    # Org B tries to verify Org A's order.
    v = client.post("/billing/razorpay/verify", json={
        "razorpay_order_id": order["order_id"],
        "razorpay_payment_id": f"pay_{uuid.uuid4().hex[:10]}",
        "razorpay_signature": _VALID_SIGNATURE,
    }, headers=headers_b)
    assert v.status_code == 400

    org_b_row = _org_row(org_b)
    assert org_b_row.plan_id is None or org_b_row.plan_id != plan_id
    payment = _payment_row(order["order_id"])
    assert payment.status == "created"
    assert payment.organization_id == org_a  # untouched, still belongs to A


# --------------------------------- 14-18. webhook -----------------------------


def test_webhook_invalid_signature_400_no_activation(monkeypatch):
    _configure_razorpay(monkeypatch)
    headers, org_id = _register_org("webhook_bad_sig")
    root_auth = _super_admin_auth()
    plan_id, _ = _create_plan(root_auth)

    org_before = _org_row(org_id)
    order = client.post("/billing/razorpay/order", json={"plan_id": plan_id, "billing_cycle": "monthly"}, headers=headers).json()
    rzp_payment_id = f"pay_{uuid.uuid4().hex[:10]}"

    r = _webhook_post(_order_paid_event(order["order_id"], rzp_payment_id), signature="tampered")
    assert r.status_code == 400

    payment = _payment_row(order["order_id"])
    assert payment.status == "created"
    org = _org_row(org_id)
    assert org.plan_id == org_before.plan_id  # unchanged — still whatever it was at registration
    assert org.plan_id != plan_id


def test_webhook_order_paid_activates(monkeypatch):
    _configure_razorpay(monkeypatch)
    headers, org_id = _register_org("webhook_order_paid")
    root_auth = _super_admin_auth()
    plan_id, _ = _create_plan(root_auth)

    order = client.post("/billing/razorpay/order", json={"plan_id": plan_id, "billing_cycle": "monthly"}, headers=headers).json()
    rzp_payment_id = f"pay_{uuid.uuid4().hex[:10]}"

    r = _webhook_post(_order_paid_event(order["order_id"], rzp_payment_id))
    assert r.status_code == 200, r.text

    org = _org_row(org_id)
    assert org.status.value == "active"
    assert org.plan_id == plan_id
    payment = _payment_row(order["order_id"])
    assert payment.status == "paid"
    assert payment.razorpay_payment_id == rzp_payment_id


def test_webhook_payment_captured_activates(monkeypatch):
    _configure_razorpay(monkeypatch)
    headers, org_id = _register_org("webhook_captured")
    root_auth = _super_admin_auth()
    plan_id, _ = _create_plan(root_auth)

    order = client.post("/billing/razorpay/order", json={"plan_id": plan_id, "billing_cycle": "monthly"}, headers=headers).json()
    rzp_payment_id = f"pay_{uuid.uuid4().hex[:10]}"

    r = _webhook_post(_payment_captured_event(order["order_id"], rzp_payment_id))
    assert r.status_code == 200, r.text

    org = _org_row(org_id)
    assert org.status.value == "active"
    payment = _payment_row(order["order_id"])
    assert payment.status == "paid"


def test_webhook_payment_failed_marks_failed_not_activated(monkeypatch):
    _configure_razorpay(monkeypatch)
    headers, org_id = _register_org("webhook_failed")
    root_auth = _super_admin_auth()
    plan_id, _ = _create_plan(root_auth)

    org_before = _org_row(org_id)
    order = client.post("/billing/razorpay/order", json={"plan_id": plan_id, "billing_cycle": "monthly"}, headers=headers).json()
    rzp_payment_id = f"pay_{uuid.uuid4().hex[:10]}"

    r = _webhook_post(_payment_failed_event(order["order_id"], rzp_payment_id, reason="insufficient_funds"))
    assert r.status_code == 200, r.text

    payment = _payment_row(order["order_id"])
    assert payment.status == "failed"
    assert payment.failure_reason == "insufficient_funds"
    org = _org_row(org_id)
    assert org.plan_id == org_before.plan_id  # unchanged
    assert org.plan_id != plan_id
    assert org.status.value != "active"


def test_webhook_unknown_event_200_no_activation(monkeypatch):
    _configure_razorpay(monkeypatch)
    headers, org_id = _register_org("webhook_unknown")
    root_auth = _super_admin_auth()
    plan_id, _ = _create_plan(root_auth)

    org_before = _org_row(org_id)
    order = client.post("/billing/razorpay/order", json={"plan_id": plan_id, "billing_cycle": "monthly"}, headers=headers).json()

    r = _webhook_post({"event": "refund.processed", "payload": {}})
    assert r.status_code == 200

    payment = _payment_row(order["order_id"])
    assert payment.status == "created"
    org = _org_row(org_id)
    assert org.plan_id == org_before.plan_id  # unchanged
    assert org.plan_id != plan_id


# --------------------------- 19-21. idempotency / expiry ----------------------


def test_verify_then_webhook_activates_once(monkeypatch):
    _configure_razorpay(monkeypatch)
    headers, org_id = _register_org("verify_then_webhook")
    root_auth = _super_admin_auth()
    plan_id, _ = _create_plan(root_auth)

    order = client.post("/billing/razorpay/order", json={"plan_id": plan_id, "billing_cycle": "monthly"}, headers=headers).json()
    rzp_payment_id = f"pay_{uuid.uuid4().hex[:10]}"

    v = client.post("/billing/razorpay/verify", json={
        "razorpay_order_id": order["order_id"],
        "razorpay_payment_id": rzp_payment_id,
        "razorpay_signature": _VALID_SIGNATURE,
    }, headers=headers)
    assert v.status_code == 200, v.text
    expiry_after_verify = _org_row(org_id).plan_expires_at

    r = _webhook_post(_order_paid_event(order["order_id"], rzp_payment_id))
    assert r.status_code == 200

    org = _org_row(org_id)
    assert org.plan_expires_at == expiry_after_verify  # not extended a second time

    db = SessionLocal()
    try:
        logs = db.query(ActivityLog).filter(
            ActivityLog.organization_id == org_id, ActivityLog.type == "billing"
        ).all()
        assert len(logs) == 1  # not duplicated by the webhook's idempotent no-op
    finally:
        db.close()


def test_duplicate_webhook_no_double_extension(monkeypatch):
    _configure_razorpay(monkeypatch)
    headers, org_id = _register_org("dup_webhook")
    root_auth = _super_admin_auth()
    plan_id, _ = _create_plan(root_auth)

    order = client.post("/billing/razorpay/order", json={"plan_id": plan_id, "billing_cycle": "monthly"}, headers=headers).json()
    rzp_payment_id = f"pay_{uuid.uuid4().hex[:10]}"
    event = _order_paid_event(order["order_id"], rzp_payment_id)

    r1 = _webhook_post(event)
    assert r1.status_code == 200
    expiry_1 = _org_row(org_id).plan_expires_at

    r2 = _webhook_post(event)  # exact same delivery, again
    assert r2.status_code == 200
    expiry_2 = _org_row(org_id).plan_expires_at

    assert expiry_1 == expiry_2


def test_existing_future_expiry_extends_from_future_not_now(monkeypatch):
    _configure_razorpay(monkeypatch)
    headers, org_id = _register_org("renewal_extends")
    root_auth = _super_admin_auth()
    plan_id, _ = _create_plan(root_auth, monthly=100.0, yearly=1000.0)

    # First activation.
    order1 = client.post("/billing/razorpay/order", json={"plan_id": plan_id, "billing_cycle": "monthly"}, headers=headers).json()
    v1 = client.post("/billing/razorpay/verify", json={
        "razorpay_order_id": order1["order_id"],
        "razorpay_payment_id": f"pay_{uuid.uuid4().hex[:10]}",
        "razorpay_signature": _VALID_SIGNATURE,
    }, headers=headers)
    assert v1.status_code == 200, v1.text
    expiry_1 = _org_row(org_id).plan_expires_at
    assert expiry_1.tzinfo is not None or True

    # Renewal while the first period is still in the future.
    order2 = client.post("/billing/razorpay/order", json={"plan_id": plan_id, "billing_cycle": "monthly"}, headers=headers).json()
    v2 = client.post("/billing/razorpay/verify", json={
        "razorpay_order_id": order2["order_id"],
        "razorpay_payment_id": f"pay_{uuid.uuid4().hex[:10]}",
        "razorpay_signature": _VALID_SIGNATURE,
    }, headers=headers)
    assert v2.status_code == 200, v2.text
    expiry_2 = _org_row(org_id).plan_expires_at

    e1 = expiry_1 if expiry_1.tzinfo else expiry_1.replace(tzinfo=timezone.utc)
    e2 = expiry_2 if expiry_2.tzinfo else expiry_2.replace(tzinfo=timezone.utc)
    delta = (e2 - e1).total_seconds()
    # Extended by ~30 days from the FIRST expiry, not from now() — if it had
    # incorrectly reset from now(), delta would be far smaller than 30 days.
    assert 29 * 86400 < delta < 31 * 86400


def test_expired_plan_locks_organization(monkeypatch):
    _configure_razorpay(monkeypatch)
    headers, org_id = _register_org("expired_plan")
    root_auth = _super_admin_auth()
    plan_id, _ = _create_plan(root_auth)

    order = client.post("/billing/razorpay/order", json={"plan_id": plan_id, "billing_cycle": "monthly"}, headers=headers).json()
    v = client.post("/billing/razorpay/verify", json={
        "razorpay_order_id": order["order_id"],
        "razorpay_payment_id": f"pay_{uuid.uuid4().hex[:10]}",
        "razorpay_signature": _VALID_SIGNATURE,
    }, headers=headers)
    assert v.status_code == 200, v.text

    db = SessionLocal()
    try:
        org = db.get(Organization, org_id)
        org.plan_expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        db.commit()
    finally:
        db.close()

    r = client.get("/organizations/me", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "locked"


# ------------------------------ 23. organization response --------------------


def test_organization_response_exposes_plan_expires_at_and_days_left(monkeypatch):
    _configure_razorpay(monkeypatch)
    headers, org_id = _register_org("org_response_fields")
    root_auth = _super_admin_auth()
    plan_id, _ = _create_plan(root_auth)

    order = client.post("/billing/razorpay/order", json={"plan_id": plan_id, "billing_cycle": "monthly"}, headers=headers).json()
    client.post("/billing/razorpay/verify", json={
        "razorpay_order_id": order["order_id"],
        "razorpay_payment_id": f"pay_{uuid.uuid4().hex[:10]}",
        "razorpay_signature": _VALID_SIGNATURE,
    }, headers=headers)

    r = client.get("/organizations/me", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan_expires_at"] is not None
    assert body["days_left"] is not None
    assert 28 <= body["days_left"] <= 30
    assert body["trial_days_left"] is not None  # unaffected / still present


# ---------------------------- 24-26. payment history --------------------------


def test_payment_history_organization_scoped(monkeypatch):
    _configure_razorpay(monkeypatch)
    headers_a, org_a = _register_org("history_a")
    headers_b, org_b = _register_org("history_b")
    root_auth = _super_admin_auth()
    plan_id, _ = _create_plan(root_auth)

    client.post("/billing/razorpay/order", json={"plan_id": plan_id, "billing_cycle": "monthly"}, headers=headers_a)
    client.post("/billing/razorpay/order", json={"plan_id": plan_id, "billing_cycle": "monthly"}, headers=headers_b)

    r_a = client.get("/billing/payments", headers=headers_a)
    assert r_a.status_code == 200, r_a.text
    assert all(row["organization_id"] == org_a for row in r_a.json())
    assert len(r_a.json()) >= 1

    r_b = client.get("/billing/payments", headers=headers_b)
    assert all(row["organization_id"] == org_b for row in r_b.json())


def test_super_admin_payment_history(monkeypatch):
    _configure_razorpay(monkeypatch)
    headers, org_id = _register_org("sa_history")
    root_auth = _super_admin_auth()
    plan_id, _ = _create_plan(root_auth)

    order = client.post("/billing/razorpay/order", json={"plan_id": plan_id, "billing_cycle": "monthly"}, headers=headers).json()

    r = client.get("/superadmin/subscription-payments", headers=root_auth)
    assert r.status_code == 200, r.text
    assert any(row["razorpay_order_id"] == order["order_id"] for row in r.json())


def test_super_admin_organization_filter(monkeypatch):
    _configure_razorpay(monkeypatch)
    headers_a, org_a = _register_org("sa_filter_a")
    headers_b, org_b = _register_org("sa_filter_b")
    root_auth = _super_admin_auth()
    plan_id, _ = _create_plan(root_auth)

    client.post("/billing/razorpay/order", json={"plan_id": plan_id, "billing_cycle": "monthly"}, headers=headers_a)
    client.post("/billing/razorpay/order", json={"plan_id": plan_id, "billing_cycle": "monthly"}, headers=headers_b)

    r = client.get(f"/superadmin/subscription-payments?organization_id={org_a}", headers=root_auth)
    assert r.status_code == 200, r.text
    assert len(r.json()) >= 1
    assert all(row["organization_id"] == org_a for row in r.json())


# --------------------------------- 27-29. security / audit ---------------------


def test_no_secrets_in_serialized_responses(monkeypatch):
    _configure_razorpay(monkeypatch)
    headers, org_id = _register_org("no_secrets_anywhere")
    root_auth = _super_admin_auth()
    plan_id, _ = _create_plan(root_auth)

    order_r = client.post("/billing/razorpay/order", json={"plan_id": plan_id, "billing_cycle": "monthly"}, headers=headers)
    order = order_r.json()
    verify_r = client.post("/billing/razorpay/verify", json={
        "razorpay_order_id": order["order_id"],
        "razorpay_payment_id": f"pay_{uuid.uuid4().hex[:10]}",
        "razorpay_signature": _VALID_SIGNATURE,
    }, headers=headers)
    history_r = client.get("/billing/payments", headers=headers)
    sa_history_r = client.get("/superadmin/subscription-payments", headers=root_auth)

    for resp in (order_r, verify_r, history_r, sa_history_r):
        assert "fake_key_secret_never_returned" not in resp.text
        assert "fake_webhook_secret_never_returned" not in resp.text


def test_activity_log_created_on_activation(monkeypatch):
    _configure_razorpay(monkeypatch)
    headers, org_id = _register_org("activity_log")
    root_auth = _super_admin_auth()
    plan_id, _ = _create_plan(root_auth)

    order = client.post("/billing/razorpay/order", json={"plan_id": plan_id, "billing_cycle": "monthly"}, headers=headers).json()
    v = client.post("/billing/razorpay/verify", json={
        "razorpay_order_id": order["order_id"],
        "razorpay_payment_id": f"pay_{uuid.uuid4().hex[:10]}",
        "razorpay_signature": _VALID_SIGNATURE,
    }, headers=headers)
    assert v.status_code == 200, v.text

    db = SessionLocal()
    try:
        logs = db.query(ActivityLog).filter(
            ActivityLog.organization_id == org_id, ActivityLog.type == "billing"
        ).all()
        assert len(logs) == 1
        assert "activated" in logs[0].description.lower() or "activated" in logs[0].title.lower()
    finally:
        db.close()


def test_webhook_raw_event_stored(monkeypatch):
    _configure_razorpay(monkeypatch)
    headers, org_id = _register_org("raw_event_stored")
    root_auth = _super_admin_auth()
    plan_id, _ = _create_plan(root_auth)

    order = client.post("/billing/razorpay/order", json={"plan_id": plan_id, "billing_cycle": "monthly"}, headers=headers).json()
    rzp_payment_id = f"pay_{uuid.uuid4().hex[:10]}"
    event = _order_paid_event(order["order_id"], rzp_payment_id)

    r = _webhook_post(event)
    assert r.status_code == 200, r.text

    payment = _payment_row(order["order_id"])
    assert payment.raw_event is not None
    assert payment.raw_event.get("event") == "order.paid"


# --------------------------- 30. manual flow unaffected ------------------------


def test_manual_upgrade_flow_still_works(monkeypatch):
    # No Razorpay configuration at all — the manual flow must not depend on it.
    monkeypatch.setattr(settings, "razorpay_key_id", "")
    monkeypatch.setattr(settings, "razorpay_key_secret", "")
    monkeypatch.setattr(settings, "razorpay_webhook_secret", "")

    headers, org_id = _register_org("manual_flow_still_works")
    root_auth = _super_admin_auth()
    plan_id, _ = _create_plan(root_auth)

    req = client.post("/organizations/upgrade-request", json={
        "requested_plan_id": plan_id, "billing_cycle": "monthly",
    }, headers=headers)
    assert req.status_code == 200, req.text
    assert req.json()["upgrade_status"] == "pending"

    appr = client.patch(f"/superadmin/organizations/{org_id}/approve-upgrade", headers=root_auth)
    assert appr.status_code == 200, appr.text
    assert appr.json()["plan_id"] == plan_id
    assert appr.json()["status"] == "active"
    # The manual flow never touches plan_expires_at — it's a Razorpay-only concept.
    assert appr.json()["plan_expires_at"] is None
