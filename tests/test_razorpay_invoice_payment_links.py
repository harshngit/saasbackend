"""Tests for Razorpay Part B — Invoice Payment Links using per-organization gateways.

Covers gateway settings, test connections, payment link generation, lifecycle,
cancellation, refresh, webhook handling, idempotency, accounting integration,
and tenant isolation.
"""

import hmac
import hashlib
import json
import uuid
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet

from app.main import app
from app.core.config import settings
from app.core.database import SessionLocal
from app.models import (
    Customer,
    CustomerPayment,
    Invoice,
    InvoiceItem,
    InvoicePaymentLink,
    OrgPaymentGateway,
    Organization,
    Product,
    User,
    UserRole,
)
from app.seed import main as seed_main
from app.services import payment_service

# Generate a valid Fernet key for tests
_TEST_FERNET_KEY = Fernet.generate_key().decode()

seed_main()
client = TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _register_org(name_prefix: str = "Invoice Org") -> tuple[dict, str, str]:
    """Register an organization and return (auth_headers, org_id, user_id)."""
    email = f"admin_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post(
        "/auth/register",
        json={
            "organization_name": f"{name_prefix} {uuid.uuid4().hex[:6]}",
            "admin_name": "Invoice Admin",
            "email": email,
            "password": "Password123!",
            "role": "admin",
        },
    )
    assert r.status_code == 201, r.text
    token = r.json()["tokens"]["access_token"]
    auth = {"Authorization": f"Bearer {token}"}

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        org_id = user.organization_id
        user_id = user.id
    finally:
        db.close()

    return auth, org_id, user_id


def _create_customer_and_invoice(auth: dict, org_id: str, total: float = 1000.0) -> tuple[str, str]:
    """Create a customer and an invoice with the given total amount."""
    # 1. Create customer
    r_c = client.post(
        "/customers",
        json={
            "name": "Acme Buyer",
            "phone": "9876543210",
            "email": "buyer@acme.com",
            "billing_address": "123 Market St",
        },
        headers=auth,
    )
    assert r_c.status_code in (200, 201), r_c.text
    cust_id = r_c.json()["id"]

    # 2. Create a product
    r_p = client.post(
        "/products",
        json={
            "name": f"Product {uuid.uuid4().hex[:6]}",
            "sku": f"SKU-{uuid.uuid4().hex[:6]}",
            "selling_price": total,
            "tax_rate": 0,
        },
        headers=auth,
    )
    assert r_p.status_code in (200, 201), r_p.text
    prod_id = r_p.json()["id"]

    # Add stock so invoice can be billed directly
    from app.services import stock_service
    db = SessionLocal()
    try:
        wh = stock_service.default_warehouse(db, org_id)
        stock_service.adjust_on_hand(db, org_id, wh.id, prod_id, None, 100, "purchase", "initial stock")
        db.commit()
    finally:
        db.close()

    # 3. Create invoice
    r_inv = client.post(
        "/invoices",
        json={
            "customer_id": cust_id,
            "items": [
                {
                    "product_id": prod_id,
                    "quantity": 1,
                    "unit_price": total,
                    "tax_rate": 0,
                }
            ],
        },
        headers=auth,
    )
    assert r_inv.status_code in (200, 201), r_inv.text
    inv_id = r_inv.json()["id"]
    return cust_id, inv_id


class _FakePaymentLinkResource:
    def __init__(self):
        self.created_links = []
        self.cancelled_link_ids = []
        self.fetched_links = {}

    def create(self, data=None):
        plink_id = f"plink_{uuid.uuid4().hex[:10]}"
        link_dict = {
            "id": plink_id,
            "short_url": f"https://rzp.io/i/{plink_id}",
            "status": "created",
            "amount": data.get("amount", 0),
            "amount_paid": 0,
            "currency": data.get("currency", "INR"),
            "customer": data.get("customer", {}),
            "notes": data.get("notes", {}),
            "created_at": int(datetime.now(timezone.utc).timestamp()),
        }
        self.created_links.append(link_dict)
        self.fetched_links[plink_id] = link_dict
        return link_dict

    def cancel(self, payment_link_id):
        self.cancelled_link_ids.append(payment_link_id)
        if payment_link_id in self.fetched_links:
            self.fetched_links[payment_link_id]["status"] = "cancelled"
        return {"id": payment_link_id, "status": "cancelled"}

    def fetch(self, payment_link_id):
        if payment_link_id in self.fetched_links:
            return self.fetched_links[payment_link_id]
        return {
            "id": payment_link_id,
            "status": "created",
            "amount": 100000,
            "amount_paid": 0,
            "payments": [],
        }

    def all(self, params=None):
        return {"items": []}


class _FakeOrgRazorpayClient:
    def __init__(self, auth=None):
        self.auth = auth
        self.payment_link = _FakePaymentLinkResource()


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------


def test_01_gateway_settings_lifecycle_and_encryption(monkeypatch):
    """Tests 1-11: Gateway configuration, mode derivation, encryption at rest, secret masking."""
    monkeypatch.setattr(settings, "field_encryption_key", _TEST_FERNET_KEY)

    auth, org_id, _ = _register_org("GW Test Org")

    # 1. GET with no gateway configured
    r_get = client.get("/settings/payment-gateway", headers=auth)
    assert r_get.status_code == 200
    data = r_get.json()
    assert data["configured"] is False
    assert data["key_id"] is None
    assert f"/payments/razorpay/webhook/{org_id}" in data["webhook_url"]

    # 2. PUT creates gateway (test mode)
    r_put = client.put(
        "/settings/payment-gateway",
        json={
            "key_id": "rzp_test_org_key123",
            "key_secret": "org_secret_xyz789",
            "webhook_secret": "whsec_abc123",
        },
        headers=auth,
    )
    assert r_put.status_code == 200, r_put.text
    gw_data = r_put.json()
    assert gw_data["configured"] is True
    assert gw_data["key_id"] == "rzp_test_org_key123"
    assert gw_data["mode"] == "test"
    assert gw_data["is_active"] is True
    assert gw_data["verified_at"] is None

    # 3. Verify encrypted at rest in DB
    db = SessionLocal()
    try:
        gw_row = db.query(OrgPaymentGateway).filter(OrgPaymentGateway.organization_id == org_id).first()
        assert gw_row is not None
        assert gw_row.key_secret_encrypted != "org_secret_xyz789"
        assert gw_row.webhook_secret_encrypted != "whsec_abc123"
        # Can decrypt
        f = Fernet(_TEST_FERNET_KEY.encode())
        assert f.decrypt(gw_row.key_secret_encrypted.encode()).decode() == "org_secret_xyz789"
        assert f.decrypt(gw_row.webhook_secret_encrypted.encode()).decode() == "whsec_abc123"
    finally:
        db.close()

    # 4-6. GET never returns secrets
    r_get2 = client.get("/settings/payment-gateway", headers=auth)
    res_json = r_get2.json()
    assert "key_secret" not in res_json
    assert "webhook_secret" not in res_json
    assert "key_secret_encrypted" not in res_json
    assert "webhook_secret_encrypted" not in res_json

    # 7. Mode live derivation
    r_live = client.put(
        "/settings/payment-gateway",
        json={
            "key_id": "rzp_live_production_key456",
            "key_secret": "live_secret",
            "webhook_secret": "live_wh_secret",
        },
        headers=auth,
    )
    assert r_live.status_code == 200
    assert r_live.json()["mode"] == "live"

    # 8. Invalid key prefix rejected
    r_bad = client.put(
        "/settings/payment-gateway",
        json={
            "key_id": "invalid_prefix_key",
            "key_secret": "some_secret",
            "webhook_secret": "some_wh",
        },
        headers=auth,
    )
    assert r_bad.status_code == 422

    # 9. PUT with omitted secret preserves existing encrypted secret
    r_preserve = client.put(
        "/settings/payment-gateway",
        json={"key_id": "rzp_test_new_key"},
        headers=auth,
    )
    assert r_preserve.status_code == 200
    assert r_preserve.json()["key_id"] == "rzp_test_new_key"

    db = SessionLocal()
    try:
        gw_row = db.query(OrgPaymentGateway).filter(OrgPaymentGateway.organization_id == org_id).first()
        f = Fernet(_TEST_FERNET_KEY.encode())
        assert f.decrypt(gw_row.key_secret_encrypted.encode()).decode() == "live_secret"
    finally:
        db.close()

    # 11. DELETE removes gateway
    r_del = client.delete("/settings/payment-gateway", headers=auth)
    assert r_del.status_code == 200
    r_check = client.get("/settings/payment-gateway", headers=auth)
    assert r_check.json()["configured"] is False


def test_02_gateway_test_connection(monkeypatch):
    """Tests 12-15: Test connection probe, verified_at update, and failure handling."""
    monkeypatch.setattr(settings, "field_encryption_key", _TEST_FERNET_KEY)

    fake_client = _FakeOrgRazorpayClient()
    from app.services import invoice_payment_link_service
    monkeypatch.setattr(invoice_payment_link_service, "get_org_razorpay_client", lambda gw: fake_client)

    auth, org_id, _ = _register_org("Test Probe Org")

    # Configure gateway
    client.put(
        "/settings/payment-gateway",
        json={
            "key_id": "rzp_test_valid_key",
            "key_secret": "valid_secret",
            "webhook_secret": "valid_wh_secret",
        },
        headers=auth,
    )

    # 12-13. Success sets verified_at
    r_test = client.post("/settings/payment-gateway/test", headers=auth)
    assert r_test.status_code == 200
    assert r_test.json()["verified_at"] is not None

    # 14-15. Failure returns 400 and clear error
    def _failing_client(gw):
        class _Failing:
            @property
            def payment_link(self):
                class _PL:
                    def all(self, params=None):
                        raise Exception("Authentication failed")
                return _PL()
        return _Failing()

    monkeypatch.setattr(invoice_payment_link_service, "get_org_razorpay_client", _failing_client)

    r_fail = client.post("/settings/payment-gateway/test", headers=auth)
    assert r_fail.status_code == 400
    assert "Invalid key id/secret" in r_fail.text


def test_03_payment_link_creation_validation_and_cancellation(monkeypatch):
    """Tests 16-30: Link creation validation, balance checks, Razorpay payload, and replacement of previous link."""
    monkeypatch.setattr(settings, "field_encryption_key", _TEST_FERNET_KEY)

    fake_client = _FakeOrgRazorpayClient()
    from app.services import invoice_payment_link_service
    monkeypatch.setattr(invoice_payment_link_service, "get_org_razorpay_client", lambda gw: fake_client)

    auth, org_id, _ = _register_org("Link Creation Org")
    cust_id, inv_id = _create_customer_and_invoice(auth, org_id, total=2500.0)

    # 16. No gateway -> 400
    r_nogw = client.post(f"/invoices/{inv_id}/payment-link", json={}, headers=auth)
    assert r_nogw.status_code == 400
    assert "Connect Razorpay in Settings first" in r_nogw.text

    # Configure gateway
    client.put(
        "/settings/payment-gateway",
        json={
            "key_id": "rzp_test_key1",
            "key_secret": "secret1",
            "webhook_secret": "whsec1",
        },
        headers=auth,
    )

    # 19. amount <= 0 rejected
    r_neg = client.post(f"/invoices/{inv_id}/payment-link", json={"amount": -10}, headers=auth)
    assert r_neg.status_code in (400, 422)

    # 20. amount > outstanding rejected
    r_over = client.post(f"/invoices/{inv_id}/payment-link", json={"amount": 3000.0}, headers=auth)
    assert r_over.status_code == 400
    assert "exceeds" in r_over.text

    # 25-26. Expiry bounds validation
    r_exp0 = client.post(f"/invoices/{inv_id}/payment-link", json={"expire_in_days": 0}, headers=auth)
    assert r_exp0.status_code == 422
    r_exp31 = client.post(f"/invoices/{inv_id}/payment-link", json={"expire_in_days": 31}, headers=auth)
    assert r_exp31.status_code == 422

    # 21-24, 27-28. Valid creation with default amount (=2500) and expiry (=7 days)
    r_create = client.post(f"/invoices/{inv_id}/payment-link", json={}, headers=auth)
    assert r_create.status_code == 201, r_create.text
    link1 = r_create.json()
    assert link1["amount"] == 2500.0
    assert link1["status"] == "created"
    assert link1["short_url"].startswith("https://rzp.io/i/")

    # Check InvoiceOut has_active_payment_link
    r_inv_get = client.get(f"/invoices/{inv_id}", headers=auth)
    assert r_inv_get.status_code == 200
    inv_data = r_inv_get.json()
    assert inv_data["has_active_payment_link"] is True
    assert inv_data["payment_link_url"] == link1["short_url"]
    assert inv_data["payment_link_status"] == "created"

    # 29. Second active link: old Razorpay link cancelled, old local link marked cancelled, new link created
    r_create2 = client.post(f"/invoices/{inv_id}/payment-link", json={"amount": 1500.0}, headers=auth)
    assert r_create2.status_code == 201
    link2 = r_create2.json()
    assert link2["amount"] == 1500.0
    assert link1["razorpay_link_id"] in fake_client.payment_link.cancelled_link_ids

    # 31. History
    r_hist = client.get(f"/invoices/{inv_id}/payment-links", headers=auth)
    assert r_hist.status_code == 200
    hist = r_hist.json()
    assert len(hist) == 2
    # Old link marked cancelled
    assert any(l["id"] == link1["id"] and l["status"] == "cancelled" for l in hist)

    # 33. Cancel active link
    r_cancel = client.post(f"/invoices/{inv_id}/payment-links/{link2['id']}/cancel", headers=auth)
    assert r_cancel.status_code == 200
    assert r_cancel.json()["status"] == "cancelled"


def test_04_webhook_and_idempotent_payment_recording(monkeypatch):
    """Tests 41-50, 59-65: Webhook signature verification, payment_link.paid event, payment_service integration."""
    monkeypatch.setattr(settings, "field_encryption_key", _TEST_FERNET_KEY)

    fake_client = _FakeOrgRazorpayClient()
    from app.services import invoice_payment_link_service
    monkeypatch.setattr(invoice_payment_link_service, "get_org_razorpay_client", lambda gw: fake_client)

    auth, org_id, _ = _register_org("Webhook Org")
    cust_id, inv_id = _create_customer_and_invoice(auth, org_id, total=1000.0)

    # Setup gateway
    wh_secret = "my_wh_secret_123"
    client.put(
        "/settings/payment-gateway",
        json={
            "key_id": "rzp_test_whkey",
            "key_secret": "whkeysecret",
            "webhook_secret": wh_secret,
        },
        headers=auth,
    )

    # Create link
    r_link = client.post(f"/invoices/{inv_id}/payment-link", json={}, headers=auth)
    assert r_link.status_code == 201
    link_data = r_link.json()
    rzp_link_id = link_data["razorpay_link_id"]

    # 41. Send payment_link.paid webhook
    pay_payment_id = f"pay_test_{uuid.uuid4().hex[:10]}"
    event_payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": rzp_link_id,
                    "status": "paid",
                    "amount": 100000,
                    "amount_paid": 100000,
                }
            },
            "payment": {
                "entity": {
                    "id": pay_payment_id,
                    "amount": 100000,
                    "created_at": int(datetime.now(timezone.utc).timestamp()),
                    "method": "upi",
                }
            },
        },
    }
    raw_body = json.dumps(event_payload).encode("utf-8")
    sig = hmac.new(wh_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()

    # 43. Bad signature -> 400
    r_bad_sig = client.post(
        f"/payments/razorpay/webhook/{org_id}",
        content=raw_body,
        headers={"X-Razorpay-Signature": "invalid_sig", "Content-Type": "application/json"},
    )
    assert r_bad_sig.status_code == 400

    # Valid webhook
    r_wh = client.post(
        f"/payments/razorpay/webhook/{org_id}",
        content=raw_body,
        headers={"X-Razorpay-Signature": sig, "Content-Type": "application/json"},
    )
    assert r_wh.status_code == 200

    # 59-65. Verify accounting and invoice state
    db = SessionLocal()
    try:
        inv = db.get(Invoice, inv_id)
        assert inv.amount_paid == 1000.0
        assert inv.status == "paid"
        assert inv.payment_status == "Paid"

        # Customer payment created
        pay = db.query(CustomerPayment).filter(CustomerPayment.invoice_id == inv_id).first()
        assert pay is not None
        assert pay.amount == 1000.0
        assert pay.payment_mode == "Online – Razorpay"
        assert pay.reference == pay_payment_id
        assert pay.receipt_number.startswith("RCPT-")
    finally:
        db.close()

    # 42. Duplicate webhook -> idempotent no-op
    r_wh2 = client.post(
        f"/payments/razorpay/webhook/{org_id}",
        content=raw_body,
        headers={"X-Razorpay-Signature": sig, "Content-Type": "application/json"},
    )
    assert r_wh2.status_code == 200

    db = SessionLocal()
    try:
        pays = db.query(CustomerPayment).filter(CustomerPayment.invoice_id == inv_id).all()
        assert len(pays) == 1, "Duplicate payment must not be created on repeated webhook"
    finally:
        db.close()


def test_05_refresh_fallback_and_tenant_isolation(monkeypatch):
    """Tests 36-40, 51-58: Refresh polling fallback and cross-tenant security."""
    monkeypatch.setattr(settings, "field_encryption_key", _TEST_FERNET_KEY)

    fake_client_org1 = _FakeOrgRazorpayClient()
    from app.services import invoice_payment_link_service
    monkeypatch.setattr(invoice_payment_link_service, "get_org_razorpay_client", lambda gw: fake_client_org1)

    auth1, org1_id, _ = _register_org("Tenant Org 1")
    auth2, org2_id, _ = _register_org("Tenant Org 2")

    # Org 1 setup
    client.put(
        "/settings/payment-gateway",
        json={"key_id": "rzp_test_org1", "key_secret": "s1", "webhook_secret": "wh1"},
        headers=auth1,
    )
    _, inv1_id = _create_customer_and_invoice(auth1, org1_id, total=800.0)

    # Org 2 setup
    client.put(
        "/settings/payment-gateway",
        json={"key_id": "rzp_test_org2", "key_secret": "s2", "webhook_secret": "wh2"},
        headers=auth2,
    )
    _, inv2_id = _create_customer_and_invoice(auth2, org2_id, total=1200.0)

    # Org 1 creates link
    r_l1 = client.post(f"/invoices/{inv1_id}/payment-link", json={}, headers=auth1)
    assert r_l1.status_code == 201
    l1_id = r_l1.json()["id"]
    l1_rzp_id = r_l1.json()["razorpay_link_id"]

    # 51-55. Cross-tenant isolation checks
    # Org 2 cannot read Org 1 links
    r_cross_list = client.get(f"/invoices/{inv1_id}/payment-links", headers=auth2)
    assert r_cross_list.status_code == 404

    # Org 2 cannot cancel Org 1 link
    r_cross_cancel = client.post(f"/invoices/{inv1_id}/payment-links/{l1_id}/cancel", headers=auth2)
    assert r_cross_cancel.status_code == 404

    # Org 2 cannot refresh Org 1 link
    r_cross_ref = client.get(f"/invoices/{inv1_id}/payment-links/{l1_id}/refresh", headers=auth2)
    assert r_cross_ref.status_code == 404

    # Org 2 cannot create link for Org 1 invoice
    r_cross_create = client.post(f"/invoices/{inv1_id}/payment-link", json={}, headers=auth2)
    assert r_cross_create.status_code == 404

    # 36-39. Refresh fallback marks paid
    pay_refresh_id = f"pay_ref_{uuid.uuid4().hex[:10]}"
    fake_client_org1.payment_link.fetched_links[l1_rzp_id]["status"] = "paid"
    fake_client_org1.payment_link.fetched_links[l1_rzp_id]["amount_paid"] = 80000
    fake_client_org1.payment_link.fetched_links[l1_rzp_id]["payments"] = [
        {"id": pay_refresh_id, "created_at": int(datetime.now(timezone.utc).timestamp())}
    ]

    r_refresh = client.get(f"/invoices/{inv1_id}/payment-links/{l1_id}/refresh", headers=auth1)
    assert r_refresh.status_code == 200
    assert r_refresh.json()["status"] == "paid"

    db = SessionLocal()
    try:
        inv = db.get(Invoice, inv1_id)
        assert inv.amount_paid == 800.0
        assert inv.status == "paid"
    finally:
        db.close()


def test_06_hardened_idempotency_and_link_replacement_safety(monkeypatch):
    """Specific tests for Payment ID DB uniqueness and link replacement safety edge cases."""
    monkeypatch.setattr(settings, "field_encryption_key", _TEST_FERNET_KEY)

    def _test_now():
        return datetime.now(timezone.utc)

    fake_client = _FakeOrgRazorpayClient()
    from app.services import invoice_payment_link_service
    monkeypatch.setattr(invoice_payment_link_service, "get_org_razorpay_client", lambda gw: fake_client)

    auth, org_id, _ = _register_org("Safety Test Org")
    cust_id, inv_id = _create_customer_and_invoice(auth, org_id, total=1500.0)

    # Configure gateway
    client.put(
        "/settings/payment-gateway",
        json={"key_id": "rzp_test_safe", "key_secret": "sec", "webhook_secret": "whsec"},
        headers=auth,
    )

    # 1. DB uniqueness: Verify manual payments with same or NULL reference work fine
    db = SessionLocal()
    try:
        cust = db.get(Customer, cust_id)
        inv = db.get(Invoice, inv_id)
        # Manual payment with string reference
        p1 = payment_service.record(
            db, org_id=org_id, customer=cust, invoice=None, amount=10.0, payment_mode="cash", reference="MANUAL-REF-1"
        )
        # Another manual payment with SAME reference (permitted for non-Razorpay)
        p2 = payment_service.record(
            db, org_id=org_id, customer=cust, invoice=None, amount=10.0, payment_mode="cash", reference="MANUAL-REF-1"
        )
        # Manual payment with NULL reference
        p3 = payment_service.record(
            db, org_id=org_id, customer=cust, invoice=None, amount=10.0, payment_mode="cash", reference=None
        )
        db.commit()
        assert p1.id != p2.id
        assert p3.reference is None
    finally:
        db.close()

    # Create link 1
    r_l1 = client.post(f"/invoices/{inv_id}/payment-link", json={"amount": 500.0}, headers=auth)
    assert r_l1.status_code == 201
    l1 = r_l1.json()

    # 2. Test link replacement when Razorpay cancellation throws an error but link is already remotely cancelled
    def _cancel_fails_but_fetch_cancelled(payment_link_id):
        raise Exception("Payment link already cancelled")

    orig_cancel = fake_client.payment_link.cancel
    fake_client.payment_link.cancel = _cancel_fails_but_fetch_cancelled
    fake_client.payment_link.fetched_links[l1["razorpay_link_id"]]["status"] = "cancelled"

    r_rep1 = client.post(f"/invoices/{inv_id}/payment-link", json={"amount": 400.0}, headers=auth)
    assert r_rep1.status_code == 201
    l2 = r_rep1.json()
    assert l2["id"] != l1["id"]

    # 3. Test link replacement when Razorpay cancellation completely fails (network/auth error)
    def _cancel_and_fetch_fail(payment_link_id):
        raise Exception("Network timeout")

    fake_client.payment_link.cancel = _cancel_and_fetch_fail
    fake_client.payment_link.fetch = _cancel_and_fetch_fail

    r_rep_fail = client.post(f"/invoices/{inv_id}/payment-link", json={"amount": 300.0}, headers=auth)
    assert r_rep_fail.status_code == 400
    assert "Failed to cancel existing active payment link on Razorpay" in r_rep_fail.text

    # Verify old link l2 is NOT falsely marked cancelled in DB
    db = SessionLocal()
    try:
        l2_db = db.get(InvoicePaymentLink, l2["id"])
        assert l2_db.status == "created"
    finally:
        db.close()

    # Restore normal cancel & fetch
    fake_client.payment_link.cancel = orig_cancel
    fake_client.payment_link.fetch = _FakePaymentLinkResource().fetch

    # 4. Test link replacement when cancellation succeeds but new link creation fails
    def _create_fails(data=None):
        raise Exception("Razorpay API error 500")

    fake_client.payment_link.create = _create_fails
    r_create_fail = client.post(f"/invoices/{inv_id}/payment-link", json={"amount": 300.0}, headers=auth)
    assert r_create_fail.status_code == 400
    assert "Razorpay link creation failed" in r_create_fail.text

    # Verify l2 was persisted as cancelled (since remote cancellation succeeded) and no new link was added
    db = SessionLocal()
    try:
        l2_db = db.get(InvoicePaymentLink, l2["id"])
        assert l2_db.status == "cancelled"
        links = db.query(InvoicePaymentLink).filter(InvoicePaymentLink.invoice_id == inv_id).all()
        assert not any(l.status in ("created", "partially_paid") for l in links)
    finally:
        db.close()

    # 5. Restore create and generate a fresh live link
    fake_client.payment_link.create = _FakePaymentLinkResource().create
    r_live = client.post(f"/invoices/{inv_id}/payment-link", json={"amount": 500.0}, headers=auth)
    assert r_live.status_code == 201
    live_link = r_live.json()

    # 6. Webhook and refresh concurrent race / duplicate payment ID protection
    pay_id = f"pay_unique_{uuid.uuid4().hex[:8]}"
    db = SessionLocal()
    try:
        # First call records payment
        rec1 = invoice_payment_link_service.record_online_payment(
            db, link_id=live_link["id"], razorpay_payment_id=pay_id, captured_at=_test_now(), amount_inr=500.0
        )
        assert rec1.status == "paid"

        # Second call with same payment ID resolves idempotently without error or duplicate payment
        rec2 = invoice_payment_link_service.record_online_payment(
            db, link_id=live_link["id"], razorpay_payment_id=pay_id, captured_at=_test_now(), amount_inr=500.0
        )
        assert rec2.status == "paid"

        pmts = db.query(CustomerPayment).filter(CustomerPayment.reference == pay_id).all()
        assert len(pmts) == 1, "Only one CustomerPayment must exist for the Razorpay payment ID"
    finally:
        db.close()
