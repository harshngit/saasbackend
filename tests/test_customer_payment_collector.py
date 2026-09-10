"""Test suite for Customer Payment Collector + Payment History Enhancement."""

import os
import sys
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.main import app
from app.core.database import SessionLocal, auto_add_missing_columns
from app.models.enums import UserRole
from app.models import (
    Customer,
    CustomerPayment,
    CustomerPaymentAllocation,
    Delivery,
    DeliveryCollection,
    Invoice,
    Organization,
    Product,
    SalesOrder,
    User,
    Warehouse,
)
from app.models.warehouse import WarehouseStock
from app.core.security import create_access_token, hash_password

client = TestClient(app)


def _auth_headers(user: User) -> dict[str, str]:
    uid = str(user.id)
    role = str(user.system_role or "admin")
    org_id = str(user.organization_id) if user.organization_id else None
    token = create_access_token(uid, role, org_id)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def collector_setup():
    auto_add_missing_columns()
    db = SessionLocal()
    try:
        ts = int(datetime.now(timezone.utc).timestamp())
        org = Organization(name=f"Collector Test Org {ts}", company_code=f"COL{ts}")
        db.add(org)
        db.flush()

        admin = User(
            email=f"admin_col_{ts}@test.com",
            name="Admin User",
            organization_id=org.id,
            system_role="admin",
            role=UserRole.ADMIN,
            password_hash=hash_password("password123"),
            is_active=True,
        )
        partner_a = User(
            email=f"partner_a_{ts}@test.com",
            name="Delivery Partner A",
            organization_id=org.id,
            system_role="staff",
            role=UserRole.DELIVERY_PARTNER,
            password_hash=hash_password("password123"),
            is_active=True,
        )
        partner_b = User(
            email=f"partner_b_{ts}@test.com",
            name="Delivery Partner B",
            organization_id=org.id,
            system_role="staff",
            role=UserRole.DELIVERY_PARTNER,
            password_hash=hash_password("password123"),
            is_active=True,
        )
        db.add_all([admin, partner_a, partner_b])
        db.flush()

        warehouse = Warehouse(
            name="Main Warehouse",
            code=f"WH-{ts}",
            organization_id=org.id,
            is_default=True,
            is_active=True,
        )
        customer = Customer(
            name="Test Collector Customer",
            organization_id=org.id,
            opening_balance=0.0,
            total_billed=1000.0,
            total_received=0.0,
            outstanding_balance=1000.0,
            is_active=True,
        )
        db.add_all([warehouse, customer])
        db.flush()

        product = Product(
            organization_id=org.id,
            name="Test Item",
            sku=f"SKU-{ts}",
            price=100.0,
            is_active=True,
        )
        db.add(product)
        db.flush()

        stock = WarehouseStock(
            organization_id=org.id,
            warehouse_id=warehouse.id,
            product_id=product.id,
            on_hand_quantity=100.0,
        )
        db.add(stock)
        db.commit()

        db.refresh(org)
        db.refresh(admin)
        db.refresh(partner_a)
        db.refresh(partner_b)
        db.refresh(customer)
        db.refresh(warehouse)
        db.refresh(product)

        return {
            "org": org,
            "admin": admin,
            "partner_a": partner_a,
            "partner_b": partner_b,
            "customer": customer,
            "warehouse": warehouse,
            "product": product,
        }
    finally:
        db.close()


def test_1_direct_customer_payment_stores_authenticated_collector(collector_setup):
    admin = collector_setup["admin"]
    cust = collector_setup["customer"]

    res = client.post(
        f"/customers/{cust.id}/payments",
        json={"amount": 150.0, "payment_mode": "cash", "note": "Direct payment test"},
        headers=_auth_headers(admin),
    )
    assert res.status_code == 201

    db = SessionLocal()
    try:
        pay = (
            db.query(CustomerPayment)
            .filter(CustomerPayment.customer_id == cust.id)
            .order_by(CustomerPayment.created_at.desc())
            .first()
        )
        assert pay is not None
        assert pay.collected_by_user_id == admin.id
        assert pay.collector is not None
        assert pay.collector.id == admin.id
    finally:
        db.close()


def test_2_order_upfront_payment_stores_authenticated_collector(collector_setup):
    admin = collector_setup["admin"]
    cust = collector_setup["customer"]
    prod = collector_setup["product"]

    res = client.post(
        "/orders",
        json={
            "customer_id": cust.id,
            "items": [{"product_id": prod.id, "quantity": 2, "unit_price": 100.0}],
            "paid_amount": 50.0,
            "payment_status": "partial",
        },
        headers=_auth_headers(admin),
    )
    assert res.status_code == 201, res.text

    db = SessionLocal()
    try:
        pay = (
            db.query(CustomerPayment)
            .filter(CustomerPayment.customer_id == cust.id)
            .order_by(CustomerPayment.created_at.desc())
            .first()
        )
        assert pay is not None
        assert pay.amount == 50.0
        assert pay.collected_by_user_id == admin.id
    finally:
        db.close()


def test_3_direct_invoice_payment_stores_authenticated_collector(collector_setup):
    admin = collector_setup["admin"]
    cust = collector_setup["customer"]
    prod = collector_setup["product"]

    res = client.post(
        "/invoices",
        json={
            "customer_id": cust.id,
            "items": [{"product_id": prod.id, "quantity": 1, "unit_price": 200.0}],
            "payment": {"amount": 200.0, "payment_method": "upi"},
        },
        headers=_auth_headers(admin),
    )
    assert res.status_code == 201, res.text

    db = SessionLocal()
    try:
        pay = (
            db.query(CustomerPayment)
            .filter(CustomerPayment.customer_id == cust.id)
            .order_by(CustomerPayment.created_at.desc())
            .first()
        )
        assert pay is not None
        assert pay.amount == 200.0
        assert pay.collected_by_user_id == admin.id
    finally:
        db.close()


def test_4_payment_receipt_stores_authenticated_collector(collector_setup):
    admin = collector_setup["admin"]
    cust = collector_setup["customer"]

    res = client.post(
        "/payment-receipts",
        json={"customer_id": cust.id, "amount_received": 120.0, "payment_method": "cash"},
        headers=_auth_headers(admin),
    )
    assert res.status_code == 201, res.text

    db = SessionLocal()
    try:
        pay = (
            db.query(CustomerPayment)
            .filter(CustomerPayment.customer_id == cust.id)
            .order_by(CustomerPayment.created_at.desc())
            .first()
        )
        assert pay is not None
        assert pay.amount == 120.0
        assert pay.collected_by_user_id == admin.id
    finally:
        db.close()


def test_5_general_customer_collection_stores_actual_collector(collector_setup):
    partner = collector_setup["partner_a"]
    admin = collector_setup["admin"]
    cust = collector_setup["customer"]

    res_coll = client.post(
        "/customer-payments/collections",
        json={"customer_id": cust.id, "amount": 80.0, "payment_mode": "upi"},
        headers=_auth_headers(partner),
    )
    assert res_coll.status_code == 201, res_coll.text
    coll_id = res_coll.json()["id"]

    res_rec = client.post(
        f"/deliveries/collections/{coll_id}/reconcile",
        json={},
        headers=_auth_headers(admin),
    )
    assert res_rec.status_code == 200, res_rec.text

    db = SessionLocal()
    try:
        pay = (
            db.query(CustomerPayment)
            .filter(CustomerPayment.customer_id == cust.id)
            .order_by(CustomerPayment.created_at.desc())
            .first()
        )
        assert pay is not None
        assert pay.amount == 80.0
        assert pay.collected_by_user_id == partner.id
    finally:
        db.close()


def test_6_and_7_delivery_cod_reconciliation_copies_collector(collector_setup):
    admin = collector_setup["admin"]
    partner = collector_setup["partner_b"]
    cust = collector_setup["customer"]

    res_coll = client.post(
        "/customer-payments/collections",
        json={"customer_id": cust.id, "amount": 250.0, "payment_mode": "cash"},
        headers=_auth_headers(partner),
    )
    assert res_coll.status_code == 201
    coll_id = res_coll.json()["id"]

    res_rec = client.post(
        f"/deliveries/collections/{coll_id}/reconcile",
        json={},
        headers=_auth_headers(admin),
    )
    assert res_rec.status_code == 200, res_rec.text

    db = SessionLocal()
    try:
        coll = db.get(DeliveryCollection, coll_id)
        assert coll.reconciliation_status == "reconciled"
        assert coll.reconciled_by_id == admin.id
        assert coll.customer_payment_id is not None

        pay = db.get(CustomerPayment, coll.customer_payment_id)
        assert pay is not None
        assert pay.collected_by_user_id == partner.id
        assert pay.collected_by_user_id != admin.id
    finally:
        db.close()


def test_8_payment_response_returns_collector_brief(collector_setup):
    admin = collector_setup["admin"]
    cust = collector_setup["customer"]

    res = client.post(
        "/payment-receipts",
        json={"customer_id": cust.id, "amount_received": 90.0, "payment_method": "cash"},
        headers=_auth_headers(admin),
    )
    assert res.status_code == 201
    data = res.json()

    assert "collector" in data
    coll = data["collector"]
    assert coll is not None
    assert coll["id"] == admin.id
    assert coll["name"] == admin.name
    assert coll["role"] == "Admin"


def test_9_and_10_customer_payment_history_fields_and_unfiltered(collector_setup):
    admin = collector_setup["admin"]
    cust = collector_setup["customer"]

    # Record a payment first
    client.post(
        "/payment-receipts",
        json={"customer_id": cust.id, "amount_received": 100.0, "payment_method": "cash"},
        headers=_auth_headers(admin),
    )

    res = client.get(
        f"/customers/{cust.id}/payments",
        headers=_auth_headers(admin),
    )
    assert res.status_code == 200
    payments = res.json()
    assert len(payments) >= 1

    for p in payments:
        assert "date" in p
        assert "amount" in p
        assert "payment_method" in p
        assert "source" in p
        assert "collector" in p
        assert "status" in p


def test_11_multiple_collectors_independent_collections(collector_setup):
    partner_a = collector_setup["partner_a"]
    partner_b = collector_setup["partner_b"]
    cust = collector_setup["customer"]
    admin = collector_setup["admin"]

    res_a = client.post(
        "/customer-payments/collections",
        json={"customer_id": cust.id, "amount": 300.0, "payment_mode": "cash"},
        headers=_auth_headers(partner_a),
    )
    assert res_a.status_code == 201
    coll_a_id = res_a.json()["id"]

    res_b = client.post(
        "/customer-payments/collections",
        json={"customer_id": cust.id, "amount": 700.0, "payment_mode": "upi"},
        headers=_auth_headers(partner_b),
    )
    assert res_b.status_code == 201
    coll_b_id = res_b.json()["id"]

    # Reconcile both collections
    client.post(f"/deliveries/collections/{coll_a_id}/reconcile", json={}, headers=_auth_headers(admin))
    client.post(f"/deliveries/collections/{coll_b_id}/reconcile", json={}, headers=_auth_headers(admin))

    res_hist = client.get(f"/customers/{cust.id}/payments", headers=_auth_headers(admin))
    assert res_hist.status_code == 200
    hist = res_hist.json()

    p_a = next(item for item in hist if item["amount"] == 300.0)
    p_b = next(item for item in hist if item["amount"] == 700.0)

    assert p_a["collector"]["id"] == partner_a.id
    assert p_b["collector"]["id"] == partner_b.id


def test_12_13_14_order_payment_history_direct_and_indirect(collector_setup):
    admin = collector_setup["admin"]
    cust = collector_setup["customer"]
    prod = collector_setup["product"]

    # 1. Create order
    res_ord = client.post(
        "/orders",
        json={
            "customer_id": cust.id,
            "items": [{"product_id": prod.id, "quantity": 1, "unit_price": 200.0}],
        },
        headers=_auth_headers(admin),
    )
    assert res_ord.status_code == 201, res_ord.text
    order_id = res_ord.json()["id"]

    db = SessionLocal()
    try:
        # 2. Create an invoice linked to this order
        inv = Invoice(
            organization_id=collector_setup["org"].id,
            invoice_number=f"INV-TEST-{int(datetime.now(timezone.utc).timestamp())}",
            order_id=order_id,
            customer_id=cust.id,
            subtotal=200.0,
            total=200.0,
            amount_paid=0.0,
            status="unpaid",
        )
        db.add(inv)
        db.flush()

        # 3. Payment A: Direct order relationship (CustomerPayment.order_id == order.id)
        p1 = CustomerPayment(
            organization_id=collector_setup["org"].id,
            customer_id=cust.id,
            order_id=order_id,
            amount=50.0,
            payment_mode="cash",
            received_on=datetime.now(timezone.utc),
            collected_by_user_id=admin.id,
        )
        # 4. Payment B: Indirect allocation relationship (CustomerPaymentAllocation -> Invoice -> Order)
        p2 = CustomerPayment(
            organization_id=collector_setup["org"].id,
            customer_id=cust.id,
            order_id=None,  # No direct order link
            amount=50.0,
            payment_mode="upi",
            received_on=datetime.now(timezone.utc),
            collected_by_user_id=admin.id,
        )
        db.add_all([p1, p2])
        db.flush()

        alloc = CustomerPaymentAllocation(
            organization_id=collector_setup["org"].id,
            customer_payment_id=p2.id,
            invoice_id=inv.id,
            amount=50.0,
        )
        db.add(alloc)
        db.commit()
    finally:
        db.close()

    # 5. Retrieve order detail
    res_detail = client.get(f"/orders/{order_id}", headers=_auth_headers(admin))
    assert res_detail.status_code == 200
    order_data = res_detail.json()

    assert "payments" in order_data
    payments = order_data["payments"]
    assert len(payments) == 2

    # Check deduplication: all payment IDs must be unique
    pay_ids = [p["id"] for p in payments]
    assert len(pay_ids) == len(set(pay_ids))


def test_15_on_account_payment_not_in_unrelated_order_history(collector_setup):
    admin = collector_setup["admin"]
    cust = collector_setup["customer"]
    prod = collector_setup["product"]

    # 1. Create order
    res_ord = client.post(
        "/orders",
        json={
            "customer_id": cust.id,
            "items": [{"product_id": prod.id, "quantity": 1, "unit_price": 500.0}],
        },
        headers=_auth_headers(admin),
    )
    assert res_ord.status_code == 201
    order_id = res_ord.json()["id"]

    # 2. Record standalone advance payment (no order or invoice)
    res_adv = client.post(
        "/payment-receipts",
        json={"customer_id": cust.id, "amount_received": 150.0, "payment_method": "cash"},
        headers=_auth_headers(admin),
    )
    assert res_adv.status_code == 201
    adv_id = res_adv.json()["id"]

    # 3. Verify advance appears in customer payment history
    res_cust_hist = client.get(f"/customers/{cust.id}/payments", headers=_auth_headers(admin))
    assert res_cust_hist.status_code == 200
    cust_payments = res_cust_hist.json()
    assert any(p["id"] == adv_id for p in cust_payments)

    # 4. Verify advance does NOT appear in order payment history
    res_ord_detail = client.get(f"/orders/{order_id}", headers=_auth_headers(admin))
    assert res_ord_detail.status_code == 200
    ord_payments = res_ord_detail.json()["payments"]
    assert not any(p["id"] == adv_id for p in ord_payments)


def test_16_customer_outstanding_remains_correct_after_collections(collector_setup):
    admin = collector_setup["admin"]
    cust = collector_setup["customer"]

    db = SessionLocal()
    try:
        c = db.get(Customer, cust.id)
        c.opening_balance = 0.0
        c.total_billed = 1000.0
        c.total_received = 0.0
        c.recompute_outstanding()
        db.commit()
    finally:
        db.close()

    res_1 = client.post(
        f"/customers/{cust.id}/payments",
        json={"amount": 300.0, "payment_mode": "cash"},
        headers=_auth_headers(admin),
    )
    assert res_1.status_code == 201

    res_2 = client.post(
        f"/customers/{cust.id}/payments",
        json={"amount": 700.0, "payment_mode": "upi"},
        headers=_auth_headers(admin),
    )
    assert res_2.status_code == 201

    db = SessionLocal()
    try:
        c = db.get(Customer, cust.id)
        assert c.total_received == 1000.0
        assert c.outstanding_balance == 0.0
    finally:
        db.close()


def test_20_tenant_isolation_preserved(collector_setup):
    admin = collector_setup["admin"]
    cust = collector_setup["customer"]

    # Create another org
    ts = int(datetime.now(timezone.utc).timestamp())
    db = SessionLocal()
    try:
        other_org = Organization(name=f"Other Org {ts}", company_code=f"OTH{ts}")
        db.add(other_org)
        db.flush()
        other_admin = User(
            email=f"other_admin_{ts}@test.com",
            name="Other Admin",
            organization_id=other_org.id,
            system_role="admin",
            role=UserRole.ADMIN,
            password_hash=hash_password("password123"),
            is_active=True,
        )
        db.add(other_admin)
        db.commit()
        other_headers = _auth_headers(other_admin)
    finally:
        db.close()

    # Attempt to fetch customer payments using user from another organization -> should fail or return 404
    res = client.get(f"/customers/{cust.id}/payments", headers=other_headers)
    assert res.status_code in (403, 404)
