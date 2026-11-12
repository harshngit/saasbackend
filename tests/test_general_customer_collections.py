"""Test suite for General Customer Outstanding Collection for Delivery Partners."""

import os
import sys
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.main import app
from app.core.database import SessionLocal
from app.models.enums import UserRole
from app.models import (
    Customer,
    CustomerPayment,
    CustomerPaymentAllocation,
    Delivery,
    DeliveryCollection,
    DeliveryCollectionAllocation,
    Invoice,
    Organization,
    SalesOrder,
    User,
)

from app.core.security import create_access_token, hash_password

client = TestClient(app)


def _auth_headers(user: User) -> dict[str, str]:
    token = create_access_token(user.id, user.system_role or "admin", user.organization_id)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def collection_setup():
    """Setup org, admin user, delivery partner user, and 2 customers with invoices."""
    db = SessionLocal()
    try:
        # Create unique org codes to avoid collision
        ts = int(datetime.now(timezone.utc).timestamp())
        code1 = f"CMP-{ts % 90000 + 10000}"
        org = Organization(name=f"Collection Test Org {ts}", company_code=code1)
        db.add(org)
        db.flush()

        # Admin / Accountant User
        admin = User(
            email=f"admin_coll_{ts}@test.com",
            name="Admin Collector",
            organization_id=org.id,
            system_role="admin",
            role=UserRole.ADMIN,
            password_hash=hash_password("password123"),
            is_active=True,
        )
        db.add(admin)

        # Delivery Partner User
        partner = User(
            email=f"partner_coll_{ts}@test.com",
            name="Field Delivery Partner",
            organization_id=org.id,
            system_role="staff",
            role=UserRole.DELIVERY_PARTNER,
            password_hash=hash_password("password123"),
            is_active=True,
        )
        db.add(partner)

        # Org 2 (For tenant isolation tests)
        code2 = f"CMP-{(ts + 1) % 90000 + 10000}"
        org2 = Organization(name=f"Other Org {ts}", company_code=code2)
        db.add(org2)
        db.flush()

        admin2 = User(
            email=f"admin_other_{ts}@test.com",
            name="Other Admin",
            organization_id=org2.id,
            system_role="admin",
            role=UserRole.ADMIN,
            password_hash=hash_password("password123"),
            is_active=True,
        )
        db.add(admin2)
        db.flush()

        # Customer 1 (Rahul)
        cust1 = Customer(
            organization_id=org.id,
            customer_id=f"CUST-RAHUL-{ts}",
            name="Rahul Customer",
            phone="9998887771",
            opening_balance=0.0,
            total_billed=1500.0,
            outstanding_balance=1500.0,
        )
        db.add(cust1)

        # Customer 2 (Priya)
        cust2 = Customer(
            organization_id=org.id,
            customer_id=f"CUST-PRIYA-{ts}",
            name="Priya Customer",
            phone="9998887772",
            opening_balance=0.0,
            total_billed=1000.0,
            outstanding_balance=1000.0,
        )
        db.add(cust2)

        # Customer in Org 2
        cust_org2 = Customer(
            organization_id=org2.id,
            customer_id=f"CUST-ORG2-{ts}",
            name="Other Org Customer",
            phone="9998887773",
            opening_balance=0.0,
            total_billed=500.0,
            outstanding_balance=500.0,
        )
        db.add(cust_org2)
        db.flush()

        # Sales Orders for Customer 1
        orderA = SalesOrder(
            organization_id=org.id,
            customer_id=cust1.id,
            order_number=f"ORD-A-{ts}",
            total=500.0,
            status="confirmed",
            fulfilment_status="planned",
        )
        orderB = SalesOrder(
            organization_id=org.id,
            customer_id=cust1.id,
            order_number=f"ORD-B-{ts}",
            total=1000.0,
            status="confirmed",
            fulfilment_status="planned",
        )
        db.add_all([orderA, orderB])
        db.flush()

        # Invoices for Customer 1 (Rahul): Invoice A = 500, Invoice B = 1000
        invA = Invoice(
            organization_id=org.id,
            customer_id=cust1.id,
            order_id=orderA.id,
            invoice_number=f"INV-A-1001-{ts}",
            invoice_date=datetime.now(timezone.utc),
            total=500.0,
            amount_paid=0.0,
            status="unpaid",
            payment_status="Unpaid",
        )
        invB = Invoice(
            organization_id=org.id,
            customer_id=cust1.id,
            order_id=orderB.id,
            invoice_number=f"INV-B-1005-{ts}",
            invoice_date=datetime.now(timezone.utc),
            total=1000.0,
            amount_paid=0.0,
            status="unpaid",
            payment_status="Unpaid",
        )
        db.add_all([invA, invB])

        # Invoice for Customer 2 (Priya): Invoice C = 1000
        invC = Invoice(
            organization_id=org.id,
            customer_id=cust2.id,
            invoice_number=f"INV-C-2001-{ts}",
            invoice_date=datetime.now(timezone.utc),
            total=1000.0,
            amount_paid=0.0,
            status="unpaid",
            payment_status="Unpaid",
        )
        db.add(invC)

        # Invoice for Org 2
        inv_org2 = Invoice(
            organization_id=org2.id,
            customer_id=cust_org2.id,
            invoice_number=f"INV-ORG2-9001-{ts}",
            invoice_date=datetime.now(timezone.utc),
            total=500.0,
            amount_paid=0.0,
            status="unpaid",
            payment_status="Unpaid",
        )
        db.add(inv_org2)

        db.commit()

        admin_headers = _auth_headers(admin)
        partner_headers = _auth_headers(partner)
        org2_headers = _auth_headers(admin2)

        yield {
            "db": db,
            "org": org,
            "admin": admin,
            "partner": partner,
            "cust1": cust1,
            "cust2": cust2,
            "cust_org2": cust_org2,
            "orderA": orderA,
            "orderB": orderB,
            "invA": invA,
            "invB": invB,
            "invC": invC,
            "inv_org2": inv_org2,
            "admin_headers": admin_headers,
            "partner_headers": partner_headers,
            "org2_headers": org2_headers,
        }
    finally:
        db.close()


def test_1_general_customer_collection(collection_setup):
    """TEST 1 — General Customer Collection recording without immediate financial posting."""
    s = collection_setup
    db = s["db"]
    payload = {
        "customer_id": s["cust1"].id,
        "amount": 500,
        "payment_method": "cash",
        "reference": "REF101",
        "notes": "Field collection test 1",
        "delivery_id": None,
    }
    res = client.post("/customer-payments/collections", json=payload, headers=s["partner_headers"])
    assert res.status_code == 201, f"Failed: {res.json()}"
    data = res.json()
    assert data["amount"] == 500.0
    assert data["delivery_id"] is None
    assert data["reconciliation_status"] == "recorded"
    assert data["collector_id"] == s["partner"].id

    # Verify no immediate financial posting
    db.refresh(s["cust1"])
    assert s["cust1"].outstanding_balance == 1500.0


def test_2_multiple_invoice_allocation(collection_setup):
    """TEST 2 — Multiple Invoice Allocation across Invoice A (500) and Invoice B (200)."""
    s = collection_setup
    db = s["db"]
    payload = {
        "customer_id": s["cust1"].id,
        "amount": 700,
        "payment_method": "upi",
        "reference": "UPI700",
        "notes": "Multi invoice partial allocation",
        "delivery_id": None,
        "allocations": [
            {"invoice_id": s["invA"].id, "amount": 500},
            {"invoice_id": s["invB"].id, "amount": 200},
        ],
    }
    res = client.post("/customer-payments/collections", json=payload, headers=s["partner_headers"])
    assert res.status_code == 201
    col_id = res.json()["id"]

    # Accountant reconciles
    rec_res = client.post(f"/deliveries/collections/{col_id}/reconcile", headers=s["admin_headers"])
    assert rec_res.status_code == 200, f"Reconcile failed: {rec_res.json()}"
    rec_data = rec_res.json()
    assert rec_data["reconciliation_status"] == "reconciled"

    # Verify financial updates
    db.refresh(s["invA"])
    db.refresh(s["invB"])
    db.refresh(s["cust1"])

    assert s["invA"].amount_paid == 500.0
    assert s["invA"].status == "paid"

    assert s["invB"].amount_paid == 200.0
    assert s["invB"].status == "partial"

    # Total outstanding reduced from 1500 to 800
    assert s["cust1"].outstanding_balance == 800.0


def test_3_full_multiple_invoice_allocation(collection_setup):
    """TEST 3 — Full Multiple Invoice Allocation (1500 across A:500 and B:1000)."""
    s = collection_setup
    db = s["db"]
    payload = {
        "customer_id": s["cust1"].id,
        "amount": 1500,
        "payment_method": "bank_transfer",
        "reference": "BANK1500",
        "delivery_id": None,
        "allocations": [
            {"invoice_id": s["invA"].id, "amount": 500},
            {"invoice_id": s["invB"].id, "amount": 1000},
        ],
    }
    res = client.post("/customer-payments/collections", json=payload, headers=s["partner_headers"])
    assert res.status_code == 201
    col_id = res.json()["id"]

    rec_res = client.post(f"/deliveries/collections/{col_id}/reconcile", headers=s["admin_headers"])
    assert rec_res.status_code == 200

    db.refresh(s["invA"])
    db.refresh(s["invB"])
    db.refresh(s["cust1"])

    assert s["invA"].status == "paid"
    assert s["invB"].status == "paid"
    assert s["cust1"].outstanding_balance == 0.0


def test_4_collection_overpayment(collection_setup):
    """TEST 4 — Collection Overpayment (Customer outstanding = 1500, collection = 1600 -> 400)."""
    s = collection_setup
    payload = {
        "customer_id": s["cust1"].id,
        "amount": 1600,
        "payment_method": "cash",
        "delivery_id": None,
    }
    res = client.post("/customer-payments/collections", json=payload, headers=s["partner_headers"])
    assert res.status_code == 400
    assert "exceeds customer's total outstanding balance" in res.json()["detail"]


def test_5_invoice_allocation_overpayment(collection_setup):
    """TEST 5 — Invoice Allocation Overpayment (Invoice A outstanding = 500, allocation = 600 -> 400)."""
    s = collection_setup
    payload = {
        "customer_id": s["cust1"].id,
        "amount": 600,
        "payment_method": "cash",
        "delivery_id": None,
        "allocations": [
            {"invoice_id": s["invA"].id, "amount": 600},
        ],
    }
    res = client.post("/customer-payments/collections", json=payload, headers=s["partner_headers"])
    assert res.status_code == 400
    assert "exceeds outstanding balance" in res.json()["detail"]


def test_6_allocation_total_exceeds_collection(collection_setup):
    """TEST 6 — Allocation Total Exceeds Collection (Collection = 700, Allocations = 500+300=800 -> 400)."""
    s = collection_setup
    payload = {
        "customer_id": s["cust1"].id,
        "amount": 700,
        "payment_method": "cash",
        "delivery_id": None,
        "allocations": [
            {"invoice_id": s["invA"].id, "amount": 500},
            {"invoice_id": s["invB"].id, "amount": 300},
        ],
    }
    res = client.post("/customer-payments/collections", json=payload, headers=s["partner_headers"])
    assert res.status_code == 400
    assert "exceeds collection amount" in res.json()["detail"]


def test_7_wrong_customer_allocation(collection_setup):
    """TEST 7 — Allocation invoice belongs to different customer (Customer 1 vs Invoice C of Customer 2)."""
    s = collection_setup
    payload = {
        "customer_id": s["cust1"].id,
        "amount": 500,
        "payment_method": "cash",
        "delivery_id": None,
        "allocations": [
            {"invoice_id": s["invC"].id, "amount": 500},
        ],
    }
    res = client.post("/customer-payments/collections", json=payload, headers=s["partner_headers"])
    assert res.status_code == 400
    assert "does not belong to customer" in res.json()["detail"]


def test_8_delivery_partner_not_assigned(collection_setup):
    """TEST 8 — Delivery Partner not assigned to customer/delivery can collect with delivery_id = None."""
    s = collection_setup
    payload = {
        "customer_id": s["cust2"].id,
        "amount": 300,
        "payment_method": "cash",
        "delivery_id": None,
    }
    res = client.post("/customer-payments/collections", json=payload, headers=s["partner_headers"])
    assert res.status_code == 201
    assert res.json()["customer_id"] == s["cust2"].id


def test_9_collector_identity(collection_setup):
    """TEST 9 — Collector identity derived from authenticated token."""
    s = collection_setup
    payload = {
        "customer_id": s["cust1"].id,
        "amount": 100,
        "payment_method": "cash",
        "delivery_id": None,
    }
    res = client.post("/customer-payments/collections", json=payload, headers=s["partner_headers"])
    assert res.status_code == 201
    assert res.json()["collector_id"] == s["partner"].id


def test_10_reconciliation(collection_setup):
    """TEST 10 — Complete Reconciliation creating CustomerPayment and updating ledger."""
    s = collection_setup
    db = s["db"]
    payload = {
        "customer_id": s["cust1"].id,
        "amount": 500,
        "payment_method": "cash",
        "delivery_id": None,
        "allocations": [
            {"invoice_id": s["invA"].id, "amount": 500},
        ],
    }
    res = client.post("/customer-payments/collections", json=payload, headers=s["partner_headers"])
    assert res.status_code == 201
    col_id = res.json()["id"]

    rec_res = client.post(f"/deliveries/collections/{col_id}/reconcile", headers=s["admin_headers"])
    assert rec_res.status_code == 200

    coll = db.get(DeliveryCollection, col_id)
    assert coll.customer_payment_id is not None
    pmt = db.get(CustomerPayment, coll.customer_payment_id)
    assert pmt is not None
    assert pmt.amount == 500.0


def test_11_void(collection_setup):
    """TEST 11 — Reconciled General Collection Voiding."""
    s = collection_setup
    db = s["db"]
    payload = {
        "customer_id": s["cust1"].id,
        "amount": 500,
        "payment_method": "cash",
        "delivery_id": None,
        "allocations": [
            {"invoice_id": s["invA"].id, "amount": 500},
        ],
    }
    res = client.post("/customer-payments/collections", json=payload, headers=s["partner_headers"])
    assert res.status_code == 201
    col_id = res.json()["id"]

    # Reconcile first
    client.post(f"/deliveries/collections/{col_id}/reconcile", headers=s["admin_headers"])
    db.refresh(s["invA"])
    assert s["invA"].amount_paid == 500.0

    # Void collection
    v_res = client.post(f"/deliveries/collections/{col_id}/void", headers=s["admin_headers"])
    assert v_res.status_code == 200
    assert v_res.json()["reconciliation_status"] == "voided"

    # Verify balances reversed
    db.refresh(s["invA"])
    db.refresh(s["cust1"])
    assert s["invA"].amount_paid == 0.0
    assert s["invA"].status == "unpaid"
    assert s["cust1"].outstanding_balance == 1500.0


def test_12_existing_delivery_cod_regression(collection_setup):
    """TEST 12 — Existing Delivery COD collection POST /deliveries/{delivery_id}/collections works."""
    s = collection_setup
    db = s["db"]
    # Create order & delivery assigned to partner
    order = SalesOrder(
        organization_id=s["org"].id,
        customer_id=s["cust1"].id,
        order_number=f"ORD-COD-{int(datetime.now(timezone.utc).timestamp())}",
        total=500.0,
        status="confirmed",
        fulfilment_status="planned",
        assigned_delivery_partner_id=s["partner"].id,
    )
    db.add(order)
    db.flush()

    delivery = Delivery(
        organization_id=s["org"].id,
        sales_order_id=order.id,
        customer_id=s["cust1"].id,
        delivery_partner_id=s["partner"].id,
        delivery_note_number=f"DLV-COD-{int(datetime.now(timezone.utc).timestamp())}",
        status="planned",
    )
    db.add(delivery)
    db.commit()

    cod_payload = {
        "amount": 500,
        "payment_mode": "cash",
        "reference": "CODREF1",
    }
    res = client.post(f"/deliveries/{delivery.id}/collections", json=cod_payload, headers=s["partner_headers"])
    assert res.status_code == 201
    data = res.json()
    assert data["delivery_id"] == delivery.id
    assert data["reconciliation_status"] == "recorded"


def test_13_tenant_isolation(collection_setup):
    """TEST 13 — Tenant isolation prevents collection for customer or invoice in another organization."""
    s = collection_setup
    payload = {
        "customer_id": s["cust_org2"].id,
        "amount": 100,
        "payment_method": "cash",
        "delivery_id": None,
    }
    res = client.post("/customer-payments/collections", json=payload, headers=s["partner_headers"])
    assert res.status_code == 404

    payload2 = {
        "customer_id": s["cust1"].id,
        "amount": 500,
        "payment_method": "cash",
        "delivery_id": None,
        "allocations": [
            {"invoice_id": s["inv_org2"].id, "amount": 500},
        ],
    }
    res2 = client.post("/customer-payments/collections", json=payload2, headers=s["partner_headers"])
    assert res2.status_code == 404


def test_14_zero_negative_amount(collection_setup):
    """TEST 14 — Zero and negative collection amounts are rejected."""
    s = collection_setup
    for amt in (0, -100):
        payload = {
            "customer_id": s["cust1"].id,
            "amount": amt,
            "payment_method": "cash",
            "delivery_id": None,
        }
        res = client.post("/customer-payments/collections", json=payload, headers=s["partner_headers"])
        assert res.status_code in (400, 422)


def test_15_database_migration_verification(collection_setup):
    """TEST 15 — Verify delivery_collections.delivery_id can be NULL in database."""
    s = collection_setup
    db = s["db"]
    coll_null = DeliveryCollection(
        organization_id=s["org"].id,
        delivery_id=None,
        customer_id=s["cust1"].id,
        amount=100.0,
        payment_mode="cash",
        reconciliation_status="recorded",
        collected_at=datetime.now(timezone.utc),
    )
    db.add(coll_null)
    db.commit()
    assert coll_null.id is not None
    assert coll_null.delivery_id is None


def test_16_unreconciled_collection_visibility(collection_setup):
    """TEST 16 — Unreconciled collection visibility on Sales Order and Customer."""
    s = collection_setup
    db = s["db"]

    # 1. Check initial state of order & customer
    ord_res = client.get(f"/orders/{s['orderA'].id}", headers=s["admin_headers"])
    assert ord_res.status_code == 200
    ord_data = ord_res.json()
    assert ord_data["payment_status"] == "pending"
    assert ord_data["unreconciled_collection_amount"] == 0.0
    assert ord_data["unreconciled_collection_count"] == 0

    cust_res = client.get(f"/customers/{s['cust1'].id}", headers=s["admin_headers"])
    assert cust_res.status_code == 200
    cust_data = cust_res.json()
    assert cust_data["financial_summary"]["unreconciled_collection_amount"] == 0.0
    assert cust_data["financial_summary"]["unreconciled_collection_count"] == 0

    # 2. Record a collection allocated to invoice A (500)
    payload = {
        "customer_id": s["cust1"].id,
        "amount": 500,
        "payment_method": "cash",
        "delivery_id": None,
        "allocations": [
            {"invoice_id": s["invA"].id, "amount": 500},
        ],
    }
    col_res = client.post("/customer-payments/collections", json=payload, headers=s["partner_headers"])
    assert col_res.status_code == 201
    col_id = col_res.json()["id"]

    # 3. Verify order now shows pending_reconciliation and unreconciled_collection_amount
    ord_res2 = client.get(f"/orders/{s['orderA'].id}", headers=s["admin_headers"])
    assert ord_res2.status_code == 200
    ord_data2 = ord_res2.json()
    assert ord_data2["payment_status"] == "pending_reconciliation"
    assert ord_data2["unreconciled_collection_amount"] == 500.0
    assert ord_data2["unreconciled_collection_count"] == 1
    # Financial ledger balance is NOT yet changed
    assert ord_data2["paid_amount"] == 0.0
    assert ord_data2["remaining_balance"] == 500.0

    # 4. Verify customer profile & customer list shows unreconciled amount
    cust_res2 = client.get(f"/customers/{s['cust1'].id}", headers=s["admin_headers"])
    assert cust_res2.status_code == 200
    cust_data2 = cust_res2.json()
    assert cust_data2["financial_summary"]["unreconciled_collection_amount"] == 500.0
    assert cust_data2["financial_summary"]["unreconciled_collection_count"] == 1
    assert cust_data2["financial_summary"]["outstanding_balance"] == 1500.0

    # GET /customers is paginated ({"items": [...]}).
    cust_list = client.get("/customers?page_size=100", headers=s["admin_headers"]).json()["items"]
    cust_item = next(c for c in cust_list if c["id"] == s["cust1"].id)
    assert cust_item["unreconciled_collection_amount"] == 500.0
    assert cust_item["unreconciled_collection_count"] == 1

    # 5. Accountant reconciles
    rec_res = client.post(f"/deliveries/collections/{col_id}/reconcile", headers=s["admin_headers"])
    assert rec_res.status_code == 200

    # 6. Verify order updates to paid, and unreconciled collection drops to 0.0
    ord_res3 = client.get(f"/orders/{s['orderA'].id}", headers=s["admin_headers"])
    assert ord_res3.status_code == 200
    ord_data3 = ord_res3.json()
    assert ord_data3["payment_status"] == "paid"
    assert ord_data3["paid_amount"] == 500.0
    assert ord_data3["remaining_balance"] == 0.0
    assert ord_data3["unreconciled_collection_amount"] == 0.0
    assert ord_data3["unreconciled_collection_count"] == 0


def test_17_voided_collections_excluded_from_unreconciled(collection_setup):
    """TEST 17 — Voided collections are excluded from unreconciled amounts."""
    s = collection_setup
    payload = {
        "customer_id": s["cust1"].id,
        "amount": 300,
        "payment_method": "cash",
        "delivery_id": None,
        "allocations": [
            {"invoice_id": s["invB"].id, "amount": 300},
        ],
    }
    col_res = client.post("/customer-payments/collections", json=payload, headers=s["partner_headers"])
    assert col_res.status_code == 201
    col_id = col_res.json()["id"]

    # Void without reconcile
    v_res = client.post(f"/deliveries/collections/{col_id}/void", headers=s["admin_headers"])
    assert v_res.status_code == 200

    # Verify order B unreconciled is 0
    ord_res = client.get(f"/orders/{s['orderB'].id}", headers=s["admin_headers"])
    assert ord_res.json()["unreconciled_collection_amount"] == 0.0
    assert ord_res.json()["payment_status"] == "pending"


def test_18_payment_proof_upload_and_reconciliation_propagation(collection_setup):
    """TEST 18 — Payment proof upload, persistence, normalization, and propagation on reconciliation."""
    s = collection_setup
    db = s["db"]

    proof_id = "proof-12345-abcde"
    payload = {
        "customer_id": s["cust1"].id,
        "amount": 400,
        "payment_method": "upi",
        "reference": "UPIPROOF400",
        "payment_proof_url": proof_id,
        "delivery_id": None,
        "allocations": [
            {"invoice_id": s["invB"].id, "amount": 400},
        ],
    }
    col_res = client.post("/customer-payments/collections", json=payload, headers=s["partner_headers"])
    assert col_res.status_code == 201
    data = col_res.json()
    assert data["payment_proof_url"] == f"/files/{proof_id}"
    col_id = data["id"]

    # Reconcile collection
    rec_res = client.post(f"/deliveries/collections/{col_id}/reconcile", headers=s["admin_headers"])
    assert rec_res.status_code == 200
    rec_data = rec_res.json()
    assert rec_data["payment_proof_url"] == f"/files/{proof_id}"

    # Verify the formal CustomerPayment inherited the payment proof URL
    coll_db = db.get(DeliveryCollection, col_id)
    assert coll_db.customer_payment_id is not None
    pmt = db.get(CustomerPayment, coll_db.customer_payment_id)
    assert pmt is not None
    assert pmt.payment_proof_url == f"/files/{proof_id}" or pmt.payment_proof_url == proof_id


def test_19_mobile_payload_order_id_persisted_and_status(collection_setup):
    """TEST 19 — A mobile payload with customer_id + order_id keeps the order link (it used
    to be dropped), and create / detail / history all report status awaiting_reconciliation."""
    s = collection_setup
    db = s["db"]
    res = client.post(
        "/customer-payments/collections",
        json={"customer_id": s["cust1"].id, "order_id": s["orderA"].id, "amount": 200, "payment_mode": "upi"},
        headers=s["partner_headers"],
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["order_id"] == s["orderA"].id
    assert body["sales_order_id"] == s["orderA"].id
    assert body["customer_id"] == s["cust1"].id
    assert body["reconciliation_status"] == "recorded"
    assert body["status"] == "awaiting_reconciliation"
    db.expire_all()
    assert db.get(DeliveryCollection, body["id"]).sales_order_id == s["orderA"].id

    detail = client.get(f"/deliveries/collections/{body['id']}", headers=s["partner_headers"])
    assert detail.status_code == 200
    assert detail.json()["status"] == "awaiting_reconciliation"

    history = client.get("/deliveries/collections?page_size=100", headers=s["partner_headers"])
    assert history.status_code == 200
    row = next(r for r in history.json()["items"] if r["id"] == body["id"])
    assert row["status"] == "awaiting_reconciliation"
    assert row["order_id"] == s["orderA"].id

    # sales_order_id is accepted as an alias of order_id.
    res2 = client.post(
        "/customer-payments/collections",
        json={"customer_id": s["cust1"].id, "sales_order_id": s["orderB"].id, "amount": 100},
        headers=s["partner_headers"],
    )
    assert res2.status_code == 201, res2.text
    assert res2.json()["order_id"] == s["orderB"].id

    # customer_id only (no order) still works and has no order link.
    res3 = client.post(
        "/customer-payments/collections",
        json={"customer_id": s["cust1"].id, "amount": 50},
        headers=s["partner_headers"],
    )
    assert res3.status_code == 201
    assert res3.json()["order_id"] is None
    assert res3.json()["status"] == "awaiting_reconciliation"


def test_20_order_id_must_belong_to_customer_and_org(collection_setup):
    """TEST 20 — order_id of another customer is a 400, of another firm a 404; nothing saved."""
    s = collection_setup
    db = s["db"]
    before = db.query(DeliveryCollection).filter(DeliveryCollection.organization_id == s["org"].id).count()

    wrong_customer = client.post(
        "/customer-payments/collections",
        json={"customer_id": s["cust2"].id, "order_id": s["orderA"].id, "amount": 100},
        headers=s["partner_headers"],
    )
    assert wrong_customer.status_code == 400
    assert "does not belong to customer" in wrong_customer.json()["detail"]

    unknown = client.post(
        "/customer-payments/collections",
        json={"customer_id": s["cust1"].id, "order_id": "no-such-order", "amount": 100},
        headers=s["partner_headers"],
    )
    assert unknown.status_code == 404

    # The other firm cannot attach this firm's order to its own customer.
    cross = client.post(
        "/customer-payments/collections",
        json={"customer_id": s["cust_org2"].id, "order_id": s["orderA"].id, "amount": 100},
        headers=s["org2_headers"],
    )
    assert cross.status_code == 404

    db.expire_all()
    assert db.query(DeliveryCollection).filter(DeliveryCollection.organization_id == s["org"].id).count() == before


def test_21_history_filters_status_search_customer_order(collection_setup):
    """TEST 21 — ?status= and ?search= used to 500 (they named columns the model lacks);
    customer_id / order_id filters narrow the history."""
    s = collection_setup
    h = s["admin_headers"]
    c1 = client.post(
        "/customer-payments/collections",
        json={"customer_id": s["cust1"].id, "order_id": s["orderA"].id, "amount": 120, "reference": "UPI-T21-XYZ"},
        headers=s["partner_headers"],
    ).json()
    c2 = client.post(
        "/customer-payments/collections",
        json={"customer_id": s["cust2"].id, "amount": 80, "notes": "t21 priya"},
        headers=s["partner_headers"],
    ).json()
    assert client.post(f"/deliveries/collections/{c2['id']}/reconcile", headers=h).status_code == 200

    def ids(url: str, headers: dict = h) -> set[str]:
        r = client.get(url, headers=headers)
        assert r.status_code == 200, r.text
        return {x["id"] for x in r.json()["items"]}

    assert c1["id"] in ids("/deliveries/collections?status=awaiting_reconciliation&page_size=100")
    assert c1["id"] in ids("/deliveries/collections?status=recorded&page_size=100")
    assert c2["id"] not in ids("/deliveries/collections?status=awaiting_reconciliation&page_size=100")
    assert c2["id"] in ids("/deliveries/collections?status=reconciled&page_size=100")
    assert client.get("/deliveries/collections?status=bogus", headers=h).status_code == 422

    assert ids("/deliveries/collections?search=UPI-T21") == {c1["id"]}
    assert c2["id"] in ids("/deliveries/collections?search=t21 priya&page_size=100")
    assert c1["id"] in ids("/deliveries/collections?search=Rahul&page_size=100")

    by_customer = ids(f"/deliveries/collections?customer_id={s['cust1'].id}&page_size=100")
    assert c1["id"] in by_customer and c2["id"] not in by_customer
    assert ids(f"/deliveries/collections?order_id={s['orderA'].id}&page_size=100") == {c1["id"]}

    reconciled = client.get(f"/deliveries/collections/{c2['id']}", headers=h).json()
    assert reconciled["status"] == "reconciled"

    # The other firm sees none of it, even when filtering by this firm's customer.
    assert ids(f"/deliveries/collections?customer_id={s['cust1'].id}", s["org2_headers"]) == set()
    assert client.get(f"/deliveries/collections/{c1['id']}", headers=s["org2_headers"]).status_code == 404


def test_22_customer_payments_list_includes_pending_collections_once(collection_setup):
    """TEST 22 — GET /customers/{id}/payments shows a mobile collection while it awaits
    reconciliation, then shows it once as a payment after reconciliation; voided ones drop out."""
    s = collection_setup
    h = s["admin_headers"]
    cid = s["cust1"].id
    pending = client.post(
        "/customer-payments/collections",
        json={"customer_id": cid, "order_id": s["orderA"].id, "amount": 300, "payment_mode": "cash"},
        headers=s["partner_headers"],
    ).json()
    voided = client.post(
        "/customer-payments/collections",
        json={"customer_id": cid, "amount": 40},
        headers=s["partner_headers"],
    ).json()
    assert client.post(f"/deliveries/collections/{voided['id']}/void", headers=h).status_code == 200

    rows = client.get(f"/customers/{cid}/payments", headers=h)
    assert rows.status_code == 200, rows.text
    pending_rows = [r for r in rows.json() if r["id"] == pending["id"]]
    assert len(pending_rows) == 1
    row = pending_rows[0]
    assert row["status"] == "awaiting_reconciliation"
    assert row["source"] == "field_collection"
    assert row["amount"] == 300.0
    assert row["order_id"] == s["orderA"].id
    assert row["receipt_number"] is None
    assert row["collected_by_user_id"] == s["partner"].id
    assert row["collector"]["id"] == s["partner"].id
    assert not any(r["id"] == voided["id"] for r in rows.json())

    assert client.post(f"/deliveries/collections/{pending['id']}/reconcile", headers=h).status_code == 200
    after = client.get(f"/customers/{cid}/payments", headers=h).json()
    assert not any(r["id"] == pending["id"] for r in after), "pending row must disappear once reconciled"
    s["db"].expire_all()
    payment_id = s["db"].get(DeliveryCollection, pending["id"]).customer_payment_id
    posted = [r for r in after if r["id"] == payment_id]
    assert len(posted) == 1
    assert posted[0]["status"] == "reconciled"
    assert posted[0]["amount"] == 300.0

    # Another firm cannot read this customer's list at all.
    assert client.get(f"/customers/{cid}/payments", headers=s["org2_headers"]).status_code == 404


def test_23_delivery_partner_history_only_own(collection_setup):
    """TEST 23 — A delivery partner's history holds only their own collections."""
    s = collection_setup
    mine = client.post(
        "/customer-payments/collections",
        json={"customer_id": s["cust2"].id, "amount": 60},
        headers=s["partner_headers"],
    ).json()
    by_admin = client.post(
        "/customer-payments/collections",
        json={"customer_id": s["cust2"].id, "amount": 70},
        headers=s["admin_headers"],
    ).json()
    items = client.get("/deliveries/collections?page_size=100", headers=s["partner_headers"]).json()["items"]
    seen = {x["id"] for x in items}
    assert mine["id"] in seen
    assert by_admin["id"] not in seen
    assert all(x["delivery_partner_id"] == s["partner"].id for x in items)


def test_24_payment_date_parsed_or_refused(collection_setup):
    """TEST 24 — payment_date sets collected_at; an unreadable one is a 422 and saves nothing
    (it used to be ignored silently and the collection dated "now")."""
    s = collection_setup
    db = s["db"]
    base = {"customer_id": s["cust2"].id, "amount": 10}
    count = lambda: db.query(DeliveryCollection).filter(  # noqa: E731
        DeliveryCollection.organization_id == s["org"].id).count()

    before = count()
    for bad in ("09/11/2026", "yesterday", "2026-13-45"):
        r = client.post("/customer-payments/collections", json={**base, "payment_date": bad}, headers=s["partner_headers"])
        assert r.status_code == 422, (bad, r.text)
    db.expire_all()
    assert count() == before

    for good, day in (("2026-11-08", "2026-11-08"), ("2026-11-08T10:30:00+05:30", "2026-11-08")):
        r = client.post("/customer-payments/collections", json={**base, "payment_date": good}, headers=s["partner_headers"])
        assert r.status_code == 201, (good, r.text)
        assert r.json()["collected_at"].startswith(day)


def test_25_history_date_and_collector_filters_and_summary(collection_setup):
    """TEST 25 — date_from / date_to / collected_by_user_id on the history, and
    GET /deliveries/collections/summary with the same filters."""
    s = collection_setup
    h = s["admin_headers"]

    def collect(headers, day, amount, mode="cash"):
        r = client.post("/customer-payments/collections", json={
            "customer_id": s["cust1"].id, "amount": amount, "payment_mode": mode,
            "payment_date": f"{day}T12:00:00+05:30"}, headers=headers)
        assert r.status_code == 201, r.text
        return r.json()["id"]

    a = collect(s["partner_headers"], "2026-10-01", 100)
    b = collect(s["partner_headers"], "2026-10-02", 50, "upi")
    c = collect(h, "2026-10-02", 30)
    assert client.post(f"/deliveries/collections/{b}/reconcile", headers=h).status_code == 200
    v = collect(s["partner_headers"], "2026-10-03", 20)
    assert client.post(f"/deliveries/collections/{v}/void", headers=h).status_code == 200

    def ids(**params):
        r = client.get("/deliveries/collections", params={"page_size": 100, **params}, headers=h)
        assert r.status_code == 200, r.text
        return {x["id"] for x in r.json()["items"]}

    window = {"date_from": "2026-10-01", "date_to": "2026-10-03"}
    assert ids(**window) >= {a, b, c, v}
    assert ids(date_from="2026-10-02", date_to="2026-10-02") >= {b, c} and not ids(
        date_from="2026-10-02", date_to="2026-10-02") & {a, v}
    assert a not in ids(date_from="2026-10-02") and a in ids(date_to="2026-10-01")
    mine = ids(collected_by_user_id=s["partner"].id, **window)
    assert {a, b, v} <= mine and c not in mine
    assert ids(collected_by_user_id=s["admin"].id, **window) & {a, b, c, v} == {c}
    assert ids(delivery_partner_id=s["partner"].id, **window) == mine  # same filter, older name
    bad = client.get("/deliveries/collections", params={"date_from": "2026-10-05", "date_to": "2026-10-01"}, headers=h)
    assert bad.status_code == 422
    assert client.get("/deliveries/collections", params={"date_from": "01-10-2026"}, headers=h).status_code == 422

    summary = client.get("/deliveries/collections/summary", params=window, headers=h)
    assert summary.status_code == 200, summary.text
    body = summary.json()
    # Only this test's four collections fall in the window for this firm.
    assert body["total"] == {"count": 4, "amount": 200.0}
    assert body["by_status"] == {
        "awaiting_reconciliation": {"count": 2, "amount": 130.0},
        "reconciled": {"count": 1, "amount": 50.0},
        "voided": {"count": 1, "amount": 20.0},
    }
    assert body["by_payment_mode"]["upi"] == {"count": 1, "amount": 50.0}
    assert body["by_payment_mode"]["cash"] == {"count": 3, "amount": 150.0}
    only = client.get("/deliveries/collections/summary", params={**window, "status": "awaiting_reconciliation",
                                                                 "collected_by_user_id": s["partner"].id}, headers=h)
    assert only.json()["total"] == {"count": 1, "amount": 100.0}
    # A delivery partner's summary covers only their own collections, whatever they ask for.
    dp = client.get("/deliveries/collections/summary", params={**window, "collected_by_user_id": s["admin"].id},
                    headers=s["partner_headers"]).json()
    assert dp["total"]["count"] == 3
    # Another firm sees nothing of this one.
    assert client.get("/deliveries/collections/summary", params=window, headers=s["org2_headers"]).json()["total"] == {
        "count": 0, "amount": 0.0}

