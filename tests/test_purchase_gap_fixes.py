"""Focused automated test suite verifying Purchase Module Gap Fixes & GRN Preparation."""

import os
import sys
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from app.main import app
from app.core.database import SessionLocal
from app.models.product import Product
from app.models.supplier import Supplier
from app.models.user import User
from app.models.warehouse import Warehouse

client = TestClient(app)

passed = 0
failed = 0


def check(description: str, condition: bool):
    global passed, failed
    if condition:
        print(f"  PASS  {description}")
        passed += 1
    else:
        print(f"  FAIL  {description}")
        failed += 1


def register_org(name_prefix: str = "Procurement Firm"):
    email = f"purchase_admin_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={
        "organization_name": f"{name_prefix} {uuid.uuid4().hex[:6]}",
        "admin_name": "Procurement Admin",
        "email": email,
        "password": "Password123!",
        "role": "admin",
    })
    assert r.status_code == 201, r.text
    token = r.json()["tokens"]["access_token"]
    auth = {"Authorization": f"Bearer {token}"}

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        org_id = user.organization_id
    finally:
        db.close()

    return auth, org_id


print("\n=======================================================")
print("TEST SUITE: Purchase Module Gap Fixes & GRN Preparation")
print("=======================================================\n")

headers, org_id = register_org()

# Setup test warehouse, supplier, product
db = SessionLocal()
try:
    wh = Warehouse(organization_id=org_id, name="Main Hub", code="WH-MAIN-01")
    db.add(wh)
    sup = Supplier(organization_id=org_id, name="Primary Vendor", is_active=True)
    db.add(sup)
    prod = Product(organization_id=org_id, name="Widget A", sku="WIDGET-A", price=100.0, total_inventory=50)
    db.add(prod)
    db.commit()
    db.refresh(wh)
    db.refresh(sup)
    db.refresh(prod)
    wh_id = wh.id
    sup_id = sup.id
    prod_id = prod.id
finally:
    db.close()

# TEST 1: Creation Defaults to draft & not_received, 0 stock movement
print("--- TEST 1: Create Purchase Defaults & Stock Stability ---")
res_create = client.post(
    "/purchases",
    headers=headers,
    json={
        "invoice_number": "PO-2026-001",
        "supplier_id": sup_id,
        "warehouse_id": wh_id,
        "items": [{"product_id": prod_id, "ordered_qty": 20, "purchase_price": 80.0}],
    },
)
check("Create purchase returns HTTP 201", res_create.status_code == 201)
pur = res_create.json()
check("Status defaults to draft", pur["status"] == "draft")
check("Receiving status defaults to not_received", pur["receiving_status"] == "not_received")
check("Item has ordered_qty = 20", pur["items"][0]["ordered_qty"] == 20)
check("Item has received_qty = 0", pur["items"][0]["received_qty"] == 0)
check("Item has remaining_qty = 20", pur["items"][0]["remaining_qty"] == 20)

# Verify stock unchanged in DB
db = SessionLocal()
try:
    p_check = db.get(Product, prod_id)
    check("Product inventory remains 50 after purchase creation", p_check.total_inventory == 50)
finally:
    db.close()

# TEST 2: Canonical Confirmation (POST /purchases/{id}/confirm) with 0 Stock Movement
print("\n--- TEST 2: Canonical Confirmation & Stock Non-Mutation ---")
res_confirm = client.post(f"/purchases/{pur['id']}/confirm", headers=headers)
check("Confirm purchase returns HTTP 200", res_confirm.status_code == 200)
pur_conf = res_confirm.json()
check("Status updated to confirmed", pur_conf["status"] == "confirmed")
check("Approval status updated to Approved", pur_conf["approval_status"] == "Approved")
check("Receiving status remains not_received", pur_conf["receiving_status"] == "not_received")

db = SessionLocal()
try:
    p_check = db.get(Product, prod_id)
    check("Product inventory remains 50 after purchase confirmation", p_check.total_inventory == 50)
finally:
    db.close()

# TEST 3: Close Purchase (POST /purchases/{id}/close)
print("\n--- TEST 3: Canonical Close Operation ---")
res_close_blocked = client.post(f"/purchases/{pur['id']}/close", headers=headers)
check("Close on unreceived purchase rejected (HTTP 400)", res_close_blocked.status_code == 400)

# Create and confirm GRN to receive all 20 units
res_grn = client.post("/grns", headers=headers, json={
    "purchase_id": pur['id'],
    "supplier_id": sup_id,
    "warehouse_id": wh_id,
    "items": [{"purchase_item_id": pur['items'][0]['id'], "product_id": prod_id, "received_qty": 20}]
})
client.post(f"/grns/{res_grn.json()['id']}/confirm", headers=headers)

res_close = client.post(f"/purchases/{pur['id']}/close", headers=headers)
check("Close fully_received purchase returns HTTP 200", res_close.status_code == 200)
check("Status updated to closed", res_close.json()["status"] == "closed")

# TEST 4: Invalid Status Transitions
print("\n--- TEST 4: Invalid Status Transition Guards ---")
res_invalid_confirm = client.post(f"/purchases/{pur['id']}/confirm", headers=headers)
check("Confirming closed purchase rejected (HTTP 400)", res_invalid_confirm.status_code == 400)

# TEST 5: Cancel Purchase (POST /purchases/{id}/cancel)
print("\n--- TEST 5: Canonical Cancellation & Stock Stability ---")
res_draft2 = client.post(
    "/purchases",
    headers=headers,
    json={
        "invoice_number": "PO-2026-002",
        "supplier_id": sup_id,
        "items": [{"product_id": prod_id, "quantity": 10, "purchase_price": 80.0}],
    },
).json()
res_cancel = client.post(f"/purchases/{res_draft2['id']}/cancel", headers=headers, json={"reason": "Budget cut"})
check("Cancel purchase returns HTTP 200", res_cancel.status_code == 200)
check("Status updated to cancelled", res_cancel.json()["status"] == "cancelled")

db = SessionLocal()
try:
    p_check = db.get(Product, prod_id)
    check("Product inventory remains 70 after purchase cancellation", p_check.total_inventory == 70)
finally:
    db.close()

# TEST 6: Legacy Endpoint Compatibility (PATCH /{id}/approve)
print("\n--- TEST 6: Legacy Endpoint Compatibility ---")
res_draft3 = client.post(
    "/purchases",
    headers=headers,
    json={
        "invoice_number": "PO-2026-003",
        "supplier_id": sup_id,
        "items": [{"product_id": prod_id, "quantity": 5, "purchase_price": 80.0}],
    },
).json()
res_legacy_app = client.patch(f"/purchases/{res_draft3['id']}/approve", headers=headers)
check("Legacy PATCH approve returns HTTP 200", res_legacy_app.status_code == 200)
check("Status transitions to confirmed", res_legacy_app.json()["status"] in ("confirmed", "approved"))

db = SessionLocal()
try:
    p_check = db.get(Product, prod_id)
    check("Product inventory remains 70 after legacy approve", p_check.total_inventory == 70)
finally:
    db.close()

print("\n=======================================================")
print(f"RESULTS: {passed} passed, {failed} failed")
print("=======================================================\n")

if failed > 0:
    sys.exit(1)
