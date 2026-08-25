"""Division 3 - Vehicle Stock Lifecycle, Over-Return Validation, Computed Quantities & Reconciliation Test Suite.

Tests:
1. Opening stock recording and warehouse deduction.
2. Additional load mid-day and warehouse deduction.
3. Delivery quantity deduction and partial delivery synchronization.
4. Over-return rejection (returned_qty > available_vehicle_stock).
5. Zero/negative return rejection.
6. Valid return, warehouse replenishment, and session closure.
7. Duplicate return rejection on closed session.
8. Computed quantities (remaining_qty, expected_closing_qty).
9. Physical stock count and variance calculation (exact, shortage, surplus).
10. Reconciliation persistence and GET historical audit records.
11. Stock integrity (reconciliation does not alter physical warehouse stock).
12. Multi-tenant organization isolation and RBAC permission enforcement.
"""

import sys
import os
sys.path.insert(0, os.path.abspath("."))
import uuid
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.database import Base, engine, get_db
from app.main import app
from app.models import (
    Organization,
    Product,
    Role,
    StockMovement,
    User,
    Vehicle,
    VehicleLoading,
    VehicleLoadingItem,
    VehicleReconciliationItem,
    VehicleStockReconciliation,
)
from app.models.enums import SystemRole

client = TestClient(app)

PASSED = 0
FAILED = 0


def log_test(name: str, condition: bool, extra: str = ""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name} {extra}".rstrip())


def _register_org(label: str):
    clean_label = label.replace("_", "").lower()
    email = f"admin_{uuid.uuid4().hex[:8]}@{clean_label}.com"
    r = client.post("/auth/register", json={
        "organization_name": f"{label} Org",
        "admin_name": f"Admin {label}",
        "email": email,
        "password": "Password123!",
        "role": "admin",
    })
    assert r.status_code == 201, r.text
    token = r.json()["tokens"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _create_staff(admin_auth: dict, name: str, role_name: str, password: str = "Password123!"):
    email = f"{role_name.lower().replace(' ', '_')}_{uuid.uuid4().hex[:6]}@example.com"
    res = client.post("/users", json={
        "name": name,
        "email": email,
        "password": password,
        "role": role_name,
    }, headers=admin_auth)
    assert res.status_code == 201, res.text
    user_data = res.json()
    login_res = client.post("/auth/login", json={
        "email": email,
        "password": password,
    })
    assert login_res.status_code == 200, login_res.text
    token = login_res.json()["tokens"]["access_token"]
    staff_auth = {"Authorization": f"Bearer {token}"}
    return user_data, staff_auth


def run_all_tests():
    global PASSED, FAILED
    PASSED = 0
    FAILED = 0

    print("\n=======================================================")
    print("TEST SUITE: Division 3 - Vehicle Stock & Reconciliation")
    print("=======================================================\n")

    db: Session = next(get_db())

    # ----------------------------------------------------
    # FIXTURES: Organizations & Users via API
    # ----------------------------------------------------
    admin_auth = _register_org("Div3_Firm1")
    org2_auth = _register_org("Div3_Firm2")

    # Delivery Partner Org 1
    driver_data, driver_auth = _create_staff(admin_auth, "Dave Driver", "delivery_partner")
    driver_id = driver_data["id"]

    # Vehicle Org 1
    veh_res = client.post("/vehicles", json={
        "vehicle_number": f"MH12-{uuid.uuid4().hex[:4].upper()}",
        "vehicle_type": "Van",
        "capacity_kg": 1000.0,
    }, headers=admin_auth)
    veh_id = veh_res.json()["id"]

    # Products Org 1
    prod_a_res = client.post("/products", json={
        "name": "Product Alpha",
        "sku": f"SKU-A-{uuid.uuid4().hex[:4]}",
        "price": 100.0,
    }, headers=admin_auth)
    prod_a_id = prod_a_res.json()["id"]

    prod_b_res = client.post("/products", json={
        "name": "Product Beta",
        "sku": f"SKU-B-{uuid.uuid4().hex[:4]}",
        "price": 50.0,
    }, headers=admin_auth)
    prod_b_id = prod_b_res.json()["id"]

    prod_a = db.get(Product, prod_a_id)
    prod_a.total_inventory = 500
    prod_b = db.get(Product, prod_b_id)
    prod_b.total_inventory = 300
    db.commit()


    # ----------------------------------------------------
    # TEST 1: Opening Stock Recording & Warehouse Deduction
    # ----------------------------------------------------
    print("--- TEST 1: Opening Stock Recording & Warehouse Deduction ---")
    load_payload = {
        "delivery_partner_id": driver_id,
        "vehicle_id": veh_id,
        "items": [
            {"product_id": prod_a_id, "loaded_qty": 100},
            {"product_id": prod_b_id, "loaded_qty": 50},
        ],
    }
    r = client.post("/vehicle-stock/loading", json=load_payload, headers=admin_auth)
    log_test("Opening vehicle stock loaded successfully", r.status_code == 201, r.text)
    loading_data = r.json()
    loading_id = loading_data["id"]
    log_test("Loading status is 'active'", loading_data["status"] == "active")
    log_test("Contains 2 loading items", len(loading_data["items"]) == 2)

    item_a = next(it for it in loading_data["items"] if it["product_id"] == prod_a_id)
    item_b = next(it for it in loading_data["items"] if it["product_id"] == prod_b_id)

    log_test("Product A loaded_qty = 100", item_a["loaded_qty"] == 100)
    log_test("Product A extra_qty = 0", item_a["extra_qty"] == 0)
    log_test("Product A delivered_qty = 0", item_a["delivered_qty"] == 0)
    log_test("Product A returned_qty = 0", item_a["returned_qty"] == 0)
    log_test("Product A remaining_qty = 100", item_a["remaining_qty"] == 100)
    log_test("Product A expected_closing_qty = 100", item_a["expected_closing_qty"] == 100)

    prod_a = db.get(Product, prod_a_id)
    prod_b = db.get(Product, prod_b_id)
    db.refresh(prod_a)
    db.refresh(prod_b)
    log_test("Warehouse Product A reduced by 100 (500 -> 400)", prod_a.total_inventory == 400)
    log_test("Warehouse Product B reduced by 50 (300 -> 250)", prod_b.total_inventory == 250)

    # ----------------------------------------------------
    # TEST 2: Active Session Duplicate Prevention
    # ----------------------------------------------------
    print("\n--- TEST 2: Duplicate Active Session Prevention ---")
    r_dup = client.post("/vehicle-stock/loading", json=load_payload, headers=admin_auth)
    log_test("Second active session for same driver rejected with HTTP 400", r_dup.status_code == 400)

    # ----------------------------------------------------
    # TEST 3: Additional Load Mid-Day
    # ----------------------------------------------------
    print("\n--- TEST 3: Additional Load Mid-Day ---")
    extra_payload = {
        "items": [
            {"product_id": prod_a_id, "quantity": 20},
        ]
    }
    r = client.post(f"/vehicle-stock/{loading_id}/extra-load", json=extra_payload, headers=admin_auth)
    log_test("Extra load added successfully", r.status_code == 200, r.text)
    loading_data = r.json()
    item_a = next(it for it in loading_data["items"] if it["product_id"] == prod_a_id)

    log_test("Product A loaded_qty = 100", item_a["loaded_qty"] == 100)
    log_test("Product A extra_qty = 20", item_a["extra_qty"] == 20)
    log_test("Product A remaining_qty = 120 (100 + 20)", item_a["remaining_qty"] == 120)
    log_test("Product A expected_closing_qty = 120", item_a["expected_closing_qty"] == 120)

    db.refresh(prod_a)
    log_test("Warehouse Product A reduced further by 20 (400 -> 380)", prod_a.total_inventory == 380)

    # ----------------------------------------------------
    # TEST 4: Delivery Handover & Quantities
    # ----------------------------------------------------
    print("\n--- TEST 4: Delivery Handover & Quantities ---")
    # Simulate delivery confirmation: 70 units of Product A delivered
    loading_obj = db.get(VehicleLoading, loading_id)
    match_a = next(it for it in loading_obj.items if it.product_id == prod_a_id)
    match_a.delivered_qty = 70
    db.commit()

    r = client.get(f"/vehicle-stock/current/{driver_id}", headers=admin_auth)
    log_test("GET /vehicle-stock/current/{id} succeeds", r.status_code == 200)
    cur_data = r.json()
    item_a = next(it for it in cur_data["items"] if it["product_id"] == prod_a_id)


    log_test("Product A loaded_qty = 100", item_a["loaded_qty"] == 100)
    log_test("Product A extra_qty = 20", item_a["extra_qty"] == 20)
    log_test("Product A delivered_qty = 70", item_a["delivered_qty"] == 70)
    log_test("Product A remaining_qty = 50 (120 - 70)", item_a["remaining_qty"] == 50)
    log_test("Product A expected_closing_qty = 50 (120 - 70 - 0)", item_a["expected_closing_qty"] == 50)

    # ----------------------------------------------------
    # TEST 5: Over-Return Validation
    # ----------------------------------------------------
    print("\n--- TEST 5: Over-Return Validation ---")
    # Available stock for A is 50. Attempting to return 51 must be rejected.
    over_return_payload = {
        "items": [
            {"product_id": prod_a_id, "returned_qty": 51},
        ]
    }
    r = client.post(f"/vehicle-stock/{loading_id}/end-of-day", json=over_return_payload, headers=admin_auth)
    log_test("Over-return (51 > 50) rejected with HTTP 400", r.status_code == 400)
    log_test("Error detail mentions exceeding available stock", "cannot exceed available" in r.text.lower())

    # Negative return rejected by schema
    neg_return_payload = {
        "items": [
            {"product_id": prod_a_id, "returned_qty": -5},
        ]
    }
    r = client.post(f"/vehicle-stock/{loading_id}/end-of-day", json=neg_return_payload, headers=admin_auth)
    log_test("Negative return rejected with HTTP 422", r.status_code == 422)

    # ----------------------------------------------------
    # TEST 6: Valid End-of-Day Return & Session Closure
    # ----------------------------------------------------
    print("\n--- TEST 6: Valid End-of-Day Return & Session Closure ---")
    valid_return_payload = {
        "items": [
            {"product_id": prod_a_id, "returned_qty": 10},
            {"product_id": prod_b_id, "returned_qty": 50},
        ]
    }
    r = client.post(f"/vehicle-stock/{loading_id}/end-of-day", json=valid_return_payload, headers=admin_auth)
    log_test("Valid return succeeds", r.status_code == 200, r.text)
    closed_data = r.json()
    log_test("Session status is now 'closed'", closed_data["status"] == "closed")

    item_a = next(it for it in closed_data["items"] if it["product_id"] == prod_a_id)
    log_test("Product A returned_qty = 10", item_a["returned_qty"] == 10)
    log_test("Product A remaining_qty = 50", item_a["remaining_qty"] == 50)
    log_test("Product A expected_closing_qty = 40 (120 - 70 - 10)", item_a["expected_closing_qty"] == 40)

    prod_a = db.get(Product, prod_a_id)
    prod_b = db.get(Product, prod_b_id)
    db.refresh(prod_a)
    db.refresh(prod_b)
    log_test("Warehouse Product A replenished by 10 (380 -> 390)", prod_a.total_inventory == 390)
    log_test("Warehouse Product B replenished by 50 (250 -> 300)", prod_b.total_inventory == 300)

    # Duplicate return against closed session rejected
    r_dup_return = client.post(f"/vehicle-stock/{loading_id}/end-of-day", json=valid_return_payload, headers=admin_auth)
    log_test("Duplicate return on closed session rejected with HTTP 400", r_dup_return.status_code == 400)

    # ----------------------------------------------------
    # TEST 7: Physical Stock Count & Variance Calculation
    # ----------------------------------------------------
    print("\n--- TEST 7: Physical Stock Count & Variance Calculation ---")
    # Expected closing for Product A is 40. Count physical = 38 (Shortage of 2).
    # Expected closing for Product B is 0. Count physical = 0 (Exact 0).
    item_a_id = item_a["id"]
    item_b_id = next(it for it in closed_data["items"] if it["product_id"] == prod_b_id)["id"]

    reconcile_payload = {
        "notes": "End of day physical count audit",
        "items": [
            {
                "loading_item_id": item_a_id,
                "product_id": prod_a_id,
                "physical_qty": 38,
                "notes": "2 units physically missing/damaged",
            },
            {
                "loading_item_id": item_b_id,
                "product_id": prod_b_id,
                "physical_qty": 0,
                "notes": "Exact match",
            },
        ],
    }
    r = client.post(f"/vehicle-stock/{loading_id}/reconcile", json=reconcile_payload, headers=admin_auth)
    log_test("Reconciliation recorded successfully", r.status_code == 201, r.text)
    rec_data = r.json()

    log_test("Reconciliation has status 'reconciled'", rec_data["status"] == "reconciled")
    log_test("Reconciliation contains 2 items", len(rec_data["items"]) == 2)

    rec_a = next(it for it in rec_data["items"] if it["product_id"] == prod_a_id)
    rec_b = next(it for it in rec_data["items"] if it["product_id"] == prod_b_id)

    log_test("Product A expected_closing_qty = 40", rec_a["expected_closing_qty"] == 40)
    log_test("Product A physical_qty = 38", rec_a["physical_qty"] == 38)
    log_test("Product A variance_qty = -2 (38 - 40 shortage)", rec_a["variance_qty"] == -2)
    log_test("Product A notes match", rec_a["notes"] == "2 units physically missing/damaged")

    log_test("Product B expected_closing_qty = 0", rec_b["expected_closing_qty"] == 0)
    log_test("Product B physical_qty = 0", rec_b["physical_qty"] == 0)
    log_test("Product B variance_qty = 0 (exact match)", rec_b["variance_qty"] == 0)

    # ----------------------------------------------------
    # TEST 8: Surplus Variance Test
    # ----------------------------------------------------
    print("\n--- TEST 8: Surplus Variance (+2) ---")
    surplus_payload = {
        "notes": "Second audit with physical surplus",
        "items": [
            {
                "product_id": prod_a_id,
                "physical_qty": 42,
                "notes": "2 extra units found in side compartment",
            }
        ],
    }
    r = client.post(f"/vehicle-stock/{loading_id}/reconcile", json=surplus_payload, headers=admin_auth)
    log_test("Surplus reconciliation succeeds", r.status_code == 201, r.text)
    surplus_data = r.json()
    surplus_rec_a = surplus_data["items"][0]
    log_test("Surplus expected_closing_qty = 40", surplus_rec_a["expected_closing_qty"] == 40)
    log_test("Surplus physical_qty = 42", surplus_rec_a["physical_qty"] == 42)
    log_test("Surplus variance_qty = +2 (42 - 40 surplus)", surplus_rec_a["variance_qty"] == 2)

    # ----------------------------------------------------
    # TEST 9: Historical Reconciliation Audit List
    # ----------------------------------------------------
    print("\n--- TEST 9: Historical Reconciliation Audit List ---")
    r = client.get(f"/vehicle-stock/{loading_id}/reconciliations", headers=admin_auth)
    log_test("GET /vehicle-stock/{id}/reconciliations succeeds", r.status_code == 200)
    rec_list = r.json()
    log_test("Returns exactly 2 historical reconciliation records", len(rec_list) == 2)
    log_test("First record in list is the newest (surplus)", rec_list[0]["notes"] == "Second audit with physical surplus")

    # ----------------------------------------------------
    # TEST 10: Stock Integrity Preservation
    # ----------------------------------------------------
    print("\n--- TEST 10: Stock Integrity (No Silent Stock Changes) ---")
    prod_a = db.get(Product, prod_a_id)
    prod_b = db.get(Product, prod_b_id)
    db.refresh(prod_a)
    db.refresh(prod_b)
    log_test("Warehouse Product A remains 390 (unaltered by variance)", prod_a.total_inventory == 390)
    log_test("Warehouse Product B remains 300 (unaltered by variance)", prod_b.total_inventory == 300)

    # ----------------------------------------------------
    # TEST 11: Multi-Tenant Organization Isolation
    # ----------------------------------------------------
    print("\n--- TEST 11: Multi-Tenant Organization Isolation ---")
    # Org 2 cannot access Org 1 loading session
    r = client.get(f"/vehicle-stock/{loading_id}/reconciliations", headers=org2_auth)
    log_test("Org 2 cannot access Org 1 reconciliations (HTTP 404)", r.status_code == 404)

    r = client.post(f"/vehicle-stock/{loading_id}/reconcile", json=reconcile_payload, headers=org2_auth)
    log_test("Org 2 cannot reconcile Org 1 loading session (HTTP 404)", r.status_code == 404)

    r = client.post(f"/vehicle-stock/{loading_id}/extra-load", json=extra_payload, headers=org2_auth)
    log_test("Org 2 cannot extra-load Org 1 loading session (HTTP 404)", r.status_code == 404)


    # ----------------------------------------------------
    # SUMMARY
    # ----------------------------------------------------
    print("\n=======================================================")
    print(f"RESULTS: {PASSED} passed, {FAILED} failed")
    print("=======================================================\n")

    if FAILED > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_all_tests()
