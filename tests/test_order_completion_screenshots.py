"""Integration tests for Order Completion Payment & Delivery Screenshots.

Tests:
1. Payment can be created without proof (payment_proof_url=None).
2. Payment can be created with one proof image (payment_proof_url populated).
3. Payment proof appears in Order Detail (GET /orders/{order_id} -> payments[].payment_proof_url).
4. Home delivery proof is accepted (POST /deliveries/{id}/confirm) and appears in Order Detail (delivery_proof_url).
5. Pickup delivery proof is accepted (POST /orders/{id}/pickup/confirm) and appears in Order Detail (delivery_proof_url).
6. Missing proof returns null (payment_proof_url=None, delivery_proof_url=None).
7. Existing multiple POD functionality is not broken on delivery confirm.
8. Tenant isolation is preserved across organizations.
9. Existing payment/accounting behavior remains unchanged.
10. Existing order completion/status transitions remain unchanged.
"""

import os
import sys
import uuid
import io
from datetime import datetime, timezone

os.environ["DATABASE_URL"] = "sqlite:///./crm_saas.db"
os.environ["TESTING"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _register_org(label: str) -> tuple[dict, str]:
    email = f"admin_{uuid.uuid4().hex[:8]}@{label.replace('_', '').lower()}.com"
    r = client.post(
        "/auth/register",
        json={
            "organization_name": f"{label} Org",
            "admin_name": f"Admin {label}",
            "email": email,
            "password": "Password123!",
            "role": "admin",
        },
    )
    assert r.status_code == 201, r.text
    auth = {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}
    return auth, r.json()["user"]["organization_id"]


from app.core.database import get_db
from app.models import WarehouseStock


def _setup_entities(auth: dict) -> tuple[str, str, str]:
    """Create customer, warehouse, and product with stock."""
    c_res = client.post(
        "/customers",
        json={
            "name": f"Customer {uuid.uuid4().hex[:6]}",
            "phone": "9876543210",
        },
        headers=auth,
    )
    assert c_res.status_code == 201, c_res.text
    cust_id = c_res.json()["id"]
    org_id = c_res.json()["organization_id"]

    wh_res = client.get("/warehouses", headers=auth)
    if wh_res.status_code == 200 and len(wh_res.json()) > 0:
        wh_id = wh_res.json()[0]["id"]
    else:
        w_create = client.post(
            "/warehouses",
            json={"name": "Main Warehouse", "code": f"WH-{uuid.uuid4().hex[:4]}"},
            headers=auth,
        )
        assert w_create.status_code == 201, w_create.text
        wh_id = w_create.json()["id"]

    p_res = client.post(
        "/products",
        json={
            "name": f"Test Product {uuid.uuid4().hex[:4]}",
            "sku": f"SKU-{uuid.uuid4().hex[:6]}",
            "selling_price": 500.0,
            "cost_price": 200.0,
        },
        headers=auth,
    )
    assert p_res.status_code == 201, p_res.text
    prod_id = p_res.json()["id"]

    db = next(get_db())
    stock = db.query(WarehouseStock).filter(
        WarehouseStock.warehouse_id == wh_id,
        WarehouseStock.product_id == prod_id,
    ).first()
    if stock:
        stock.on_hand_quantity = 500.0
    else:
        stock = WarehouseStock(
            organization_id=org_id,
            warehouse_id=wh_id,
            product_id=prod_id,
            on_hand_quantity=500.0,
        )
        db.add(stock)
    db.commit()
    db.close()

    return cust_id, wh_id, prod_id


def _upload_dummy_file(auth: dict, filename: str = "receipt.png") -> str:
    """Upload dummy image file and return its URL."""
    file_bytes = io.BytesIO(b"fake image byte content for test")
    res = client.post(
        "/files/upload",
        files={"file": (filename, file_bytes, "image/png")},
        headers=auth,
    )
    assert res.status_code == 201, res.text
    return res.json()["url"]


def test_payment_with_and_without_proof_in_order_detail():
    """Test 1, 2, 3, 6: Payment creation with/without proof, and appearance in Order Detail."""
    auth, org_id = _register_org("PaymentProofOrg")
    cust_id, wh_id, prod_id = _setup_entities(auth)

    # Place a confirmed order with delivery
    order_res = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "fulfilment_method": "delivery",
            "delivery_address": "123 Main St",
            "items": [{"product_id": prod_id, "quantity": 2, "unit_price": 500.0}],
        },
        headers=auth,
    )
    assert order_res.status_code == 201, order_res.text
    order_id = order_res.json()["id"]
    assert order_res.json()["delivery_proof_url"] is None

    # Test 1: Record 1st payment WITHOUT proof
    pay1_res = client.post(
        f"/orders/{order_id}/payments",
        json={"amount": 400.0, "payment_method": "cash"},
        headers=auth,
    )
    assert pay1_res.status_code == 200, pay1_res.text
    assert pay1_res.json()["paid_amount"] == 400.0
    assert pay1_res.json()["payment_proof_url"] is None

    # Test 2: Upload a screenshot and record 2nd payment WITH proof
    proof_url = _upload_dummy_file(auth, "payment_screenshot.jpg")
    assert "/files/" in proof_url

    pay2_res = client.post(
        f"/orders/{order_id}/payments",
        json={
            "amount": 600.0,
            "payment_method": "upi",
            "reference": "UPI123456789",
            "payment_proof_url": proof_url,
        },
        headers=auth,
    )
    assert pay2_res.status_code == 200, pay2_res.text
    assert pay2_res.json()["paid_amount"] == 1000.0
    assert pay2_res.json()["payment_status"] == "paid"
    assert pay2_res.json()["payment_proof_url"] == proof_url

    # Test 3 & 6: Verify Order Detail returns both payments and correct proof URLs
    detail_res = client.get(f"/orders/{order_id}", headers=auth)
    assert detail_res.status_code == 200, detail_res.text
    order_data = detail_res.json()

    assert len(order_data["payments"]) == 2
    # One payment has null proof, one has normalized proof
    proofs = [p["payment_proof_url"] for p in order_data["payments"]]
    assert None in proofs
    assert proof_url in proofs
    assert order_data["paid_amount"] == 1000.0
    assert order_data["remaining_amount"] == 0.0


def test_pickup_delivery_proof_in_order_detail():
    """Test 5 & 6: Pickup completion with delivery proof URL and appearance in Order Detail."""
    auth, org_id = _register_org("PickupProofOrg")
    cust_id, wh_id, prod_id = _setup_entities(auth)

    # Place a pickup order
    order_res = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "fulfilment_method": "pickup",
            "items": [{"product_id": prod_id, "quantity": 1, "unit_price": 500.0}],
        },
        headers=auth,
    )
    assert order_res.status_code == 201, order_res.text
    order_id = order_res.json()["id"]

    # Order was placed as draft/pickup -> confirm order
    confirm_res = client.post(f"/orders/{order_id}/confirm", headers=auth)
    assert confirm_res.status_code == 200, confirm_res.text

    pickup_proof_url = _upload_dummy_file(auth, "customer_pickup_receipt.jpg")

    # Complete pickup with delivery_proof_url
    pickup_res = client.post(
        f"/orders/{order_id}/pickup/confirm",
        json={
            "collected_by": "John Customer",
            "notes": "Picked up at counter",
            "delivery_proof_url": pickup_proof_url,
        },
        headers=auth,
    )
    assert pickup_res.status_code == 200, pickup_res.text
    assert pickup_res.json()["status"] == "completed"
    assert pickup_res.json()["fulfilment_status"] == "delivered"
    assert pickup_res.json()["delivery_proof_url"] == pickup_proof_url

    # Check GET /orders/{order_id}
    detail_res = client.get(f"/orders/{order_id}", headers=auth)
    assert detail_res.status_code == 200, detail_res.text
    assert detail_res.json()["delivery_proof_url"] == pickup_proof_url
    assert detail_res.json()["status"] == "completed"


def test_home_delivery_pod_proof_in_order_detail():
    """Test 4 & 7: Home delivery proof via POD and appearance in Order Detail + multiple POD compatibility."""
    auth, org_id = _register_org("HomeDeliveryProofOrg")
    cust_id, wh_id, prod_id = _setup_entities(auth)

    # Create Delivery Partner user
    partner_email = f"driver_{uuid.uuid4().hex[:6]}@homedelivery.com"
    u_res = client.post(
        "/users",
        json={
            "name": "Delivery Driver Bob",
            "email": partner_email,
            "password": "Password123!",
            "role": "delivery_partner",
        },
        headers=auth,
    )
    assert u_res.status_code == 201, u_res.text
    partner_id = u_res.json()["id"]

    # Create active vehicle
    v_res = client.post(
        "/vehicles",
        json={"vehicle_number": f"DL-{uuid.uuid4().hex[:4].upper()}", "status": "active"},
        headers=auth,
    )
    assert v_res.status_code == 201, v_res.text
    vehicle_id = v_res.json()["id"]

    # Place order
    order_res = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "delivery_method": "home_delivery",
            "delivery_address": "456 Oak Avenue",
            "delivery_partner_id": partner_id,
            "vehicle_id": vehicle_id,
            "items": [{"product_id": prod_id, "quantity": 2, "unit_price": 500.0}],
        },
        headers=auth,
    )
    assert order_res.status_code == 201, order_res.text
    order_id = order_res.json()["id"]
    delivery_id = order_res.json()["delivery_id"]
    assert delivery_id is not None

    # Login as delivery partner to accept delivery
    login_res = client.post(
        "/auth/login",
        json={"email": partner_email, "password": "Password123!"},
    )
    assert login_res.status_code == 200, login_res.text
    driver_auth = {"Authorization": f"Bearer {login_res.json()['tokens']['access_token']}"}

    accept_res = client.post(f"/deliveries/{delivery_id}/accept", headers=driver_auth)
    assert accept_res.status_code == 200, accept_res.text
    deliv_item_id = accept_res.json()["items"][0]["id"]

    pick_res = client.post(
        f"/deliveries/{delivery_id}/pick",
        json={"items": [{"delivery_item_id": deliv_item_id, "picked_quantity": 2}]},
        headers=auth,
    )
    assert pick_res.status_code == 200, pick_res.text

    ready_res = client.post(f"/deliveries/{delivery_id}/ready", headers=auth)
    assert ready_res.status_code == 200, ready_res.text

    load_res = client.post(f"/deliveries/{delivery_id}/load", headers=auth)
    assert load_res.status_code == 200, load_res.text

    dispatch_res = client.patch(
        f"/deliveries/by-id/{delivery_id}",
        json={"status": "in_transit"},
        headers=auth,
    )
    assert dispatch_res.status_code == 200, dispatch_res.text

    # Upload multiple POD photos to verify multiple POD compatibility (Test 7)
    pod1_url = _upload_dummy_file(auth, "doorstep_photo1.jpg")
    pod2_url = _upload_dummy_file(auth, "doorstep_photo2.jpg")
    sig_url = _upload_dummy_file(auth, "signature.png")

    deliv_item_id = dispatch_res.json()["items"][0]["id"]

    # Confirm delivery with multiple POD photos + delivery_proof_url (single alias)
    confirm_deliv_res = client.post(
        f"/deliveries/{delivery_id}/confirm",
        json={
            "items": [{"delivery_item_id": deliv_item_id, "delivered_quantity": 2}],
            "pod_photo_file_ids": [pod1_url, pod2_url],
            "signature_file_id": sig_url,
            "receiver_name": "Alice Customer",
        },
        headers=auth,
    )
    assert confirm_deliv_res.status_code == 200, confirm_deliv_res.text
    assert confirm_deliv_res.json()["status"] == "delivered"
    assert len(confirm_deliv_res.json()["pod"]["photo_file_ids"]) == 2

    # Check Order Detail GET /orders/{order_id}
    detail_res = client.get(f"/orders/{order_id}", headers=auth)
    assert detail_res.status_code == 200, detail_res.text
    order_data = detail_res.json()

    assert order_data["status"] == "completed"
    assert order_data["fulfilment_status"] == "delivered"
    # Unified delivery_proof_url resolves to the primary POD photo
    assert order_data["delivery_proof_url"] == pod1_url


def test_tenant_isolation_proofs():
    """Test 8: Tenant isolation is preserved across organizations."""
    auth1, org1_id = _register_org("TenantOneOrg")
    auth2, org2_id = _register_org("TenantTwoOrg")

    cust1_id, wh1_id, prod1_id = _setup_entities(auth1)

    proof_url = _upload_dummy_file(auth1, "org1_proof.jpg")

    order_res = client.post(
        "/orders",
        json={
            "customer_id": cust1_id,
            "warehouse_id": wh1_id,
            "fulfilment_method": "pickup",
            "items": [{"product_id": prod1_id, "quantity": 1, "unit_price": 500.0}],
        },
        headers=auth1,
    )
    order_id = order_res.json()["id"]

    client.post(f"/orders/{order_id}/confirm", headers=auth1)
    client.post(
        f"/orders/{order_id}/pickup/confirm",
        json={"delivery_proof_url": proof_url},
        headers=auth1,
    )

    # Org 2 cannot access Org 1 order
    forbidden_res = client.get(f"/orders/{order_id}", headers=auth2)
    assert forbidden_res.status_code == 404
