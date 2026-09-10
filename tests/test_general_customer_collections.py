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
        org = Organization(name=f"Collection Test Org {ts}", company_code=f"CTO{ts}")
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
        org2 = Organization(name=f"Other Org {ts}", company_code=f"OTH{ts}")
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

        # Invoices for Customer 1 (Rahul): Invoice A = 500, Invoice B = 1000
        invA = Invoice(
            organization_id=org.id,
            customer_id=cust1.id,
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
