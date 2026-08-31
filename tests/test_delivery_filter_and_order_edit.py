"""Comprehensive test suite for:
  TASK 1: Strict Delivery Partner Filtering
  TASK 2: General Order Edit API (PATCH /orders/{order_id})
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from app.main import app
from app.seed import main as seed_main

seed_main()
client = TestClient(app)

_passed = 0
_failed = 0


def ok(msg: str):
    global _passed
    _passed += 1
    print(f"  PASS  {msg}")


def fail(msg: str, detail: str = ""):
    global _failed
    _failed += 1
    print(f"  FAIL  {msg}  {detail}")


def assert_eq(actual, expected, msg: str):
    if actual == expected:
        ok(msg)
    else:
        fail(msg, f"Expected {expected!r}, got {actual!r}")


def assert_true(condition: bool, msg: str):
    if condition:
        ok(msg)
    else:
        fail(msg, "Condition was False")


def _register_org(name_prefix: str = "Org"):
    email = f"admin_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={
        "organization_name": f"{name_prefix} {uuid.uuid4().hex[:6]}",
        "admin_name": "Admin User",
        "email": email,
        "password": "Password123!",
        "role": "admin",
    })
    assert r.status_code == 201, r.text
    auth = {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}
    return auth, email


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
    login_res = client.post("/auth/login", json={"email": email, "password": password})
    assert login_res.status_code == 200, login_res.text
    auth = {"Authorization": f"Bearer {login_res.json()['tokens']['access_token']}"}
    return auth, user_data


def _create_custom_role(admin_auth: dict, role_name: str, permissions: dict, workspace: str = "sales"):
    res = client.post("/roles", json={
        "name": role_name,
        "permissions": permissions,
        "workspace": workspace,
        "data_scope": "all",
    }, headers=admin_auth)
    assert res.status_code == 201, res.text
    return res.json()


def _create_staff_with_role_id(admin_auth: dict, name: str, role_id: str, password: str = "Password123!"):
    email = f"staff_{uuid.uuid4().hex[:6]}@example.com"
    res = client.post("/users", json={
        "name": name,
        "email": email,
        "password": password,
        "role_id": role_id,
    }, headers=admin_auth)
    assert res.status_code == 201, res.text
    user_data = res.json()
    login_res = client.post("/auth/login", json={"email": email, "password": password})
    assert login_res.status_code == 200, login_res.text
    auth = {"Authorization": f"Bearer {login_res.json()['tokens']['access_token']}"}
    return auth, user_data


def _create_product(admin_auth: dict, name: str = "Test Product", price: float = 100.0, stock: int = 50):
    res = client.post("/products", json={
        "name": f"{name} {uuid.uuid4().hex[:4]}",
        "price": price,
        "total_inventory": stock,
        "tax_rate": 18.0,
    }, headers=admin_auth)
    assert res.status_code == 201, res.text
    return res.json()


def _create_customer(admin_auth: dict, name: str = "Test Customer"):
    res = client.post("/customers", json={
        "name": f"{name} {uuid.uuid4().hex[:4]}",
        "phone": "9876543210",
        "billing_address": "123 Main St",
        "delivery_address": "456 Delivery Ave",
    }, headers=admin_auth)
    assert res.status_code == 201, res.text
    return res.json()


def main():
    print("=======================================================")
    print("TEST SUITE: Strict Delivery Partner & Order Edit API")
    print("=======================================================")

    # Setup Org 1
    auth_admin1, _ = _register_org("Firm Alpha")
    auth_sales1, sales1 = _create_staff(auth_admin1, "Alice Sales", "Sales Officer")
    auth_driver1, driver1 = _create_staff(auth_admin1, "Dave Driver", "Delivery Partner")
    auth_acct1, acct1 = _create_staff(auth_admin1, "Bob Accountant", "Accountant")

    # Setup Org 2
    auth_admin2, _ = _register_org("Firm Beta")
    auth_driver2, driver2 = _create_staff(auth_admin2, "Frank Beta Driver", "Delivery Partner")

    # Super Admin login
    res_sa = client.post("/auth/login", json={"email": "superadmin@demo.com", "password": "SuperAdminPassword123!"})
    auth_superadmin = {"Authorization": f"Bearer {res_sa.json()['tokens']['access_token']}"} if res_sa.status_code == 200 else None

    # Products & Customers for Org 1
    prod1 = _create_product(auth_admin1, "Widget A", price=100.0, stock=50)
    prod2 = _create_product(auth_admin1, "Widget B", price=200.0, stock=30)
    cust1 = _create_customer(auth_admin1, "Customer One")
    cust2 = _create_customer(auth_admin1, "Customer Two")

    # Products & Customers for Org 2
    prod_beta = _create_product(auth_admin2, "Beta Product", price=50.0, stock=20)
    cust_beta = _create_customer(auth_admin2, "Beta Customer")

    print("\n--- TASK 1: STRICT DELIVERY PARTNER FILTERING ---")

    # TEST 1.1: Genuine Delivery Partner appears in GET /deliveries/partners
    res = client.get("/deliveries/partners", headers=auth_admin1)
    assert_eq(res.status_code, 200, "Admin can GET /deliveries/partners")
    partner_ids = [p["id"] for p in res.json()]
    assert_true(driver1["id"] in partner_ids, "Genuine Delivery Partner appears in GET /deliveries/partners")
    assert_true(sales1["id"] not in partner_ids, "Sales Officer is not in GET /deliveries/partners")
    assert_true(acct1["id"] not in partner_ids, "Accountant is not in GET /deliveries/partners")

    # TEST 1.2: Custom role with deliveries: view/create/edit permissions does NOT appear if not delivery partner
    role_dispatcher = _create_custom_role(auth_admin1, "Dispatcher Role", {
        "deliveries": {"view": True, "create": True, "edit": True},
        "sales_orders": {"view": True, "create": True, "edit": True},
    }, workspace="sales")
    auth_dispatcher, dispatcher = _create_staff_with_role_id(auth_admin1, "Dan Dispatcher", role_dispatcher["id"])

    res_after_disp = client.get("/deliveries/partners", headers=auth_admin1)
    partner_ids_after = [p["id"] for p in res_after_disp.json()]
    assert_true(dispatcher["id"] not in partner_ids_after, "Staff with deliveries.view/create/edit permission is EXCLUDED from /deliveries/partners")

    # TEST 1.3: Custom role with workspace="delivery" or name="Delivery Partner" DOES appear
    role_custom_driver = _create_custom_role(auth_admin1, "Custom Courier", {
        "deliveries": {"view": True, "edit": True},
    }, workspace="delivery")
    auth_custom_driver, custom_driver = _create_staff_with_role_id(auth_admin1, "Charlie Courier", role_custom_driver["id"])

    res_after_custom = client.get("/deliveries/partners", headers=auth_admin1)
    partner_ids_custom = [p["id"] for p in res_after_custom.json()]
    assert_true(custom_driver["id"] in partner_ids_custom, "Custom role with workspace='delivery' appears in /deliveries/partners")

    # TEST 1.4: Cross-org isolation on /deliveries/partners
    res_beta_partners = client.get("/deliveries/partners", headers=auth_admin2)
    beta_partner_ids = [p["id"] for p in res_beta_partners.json()]
    assert_true(driver2["id"] in beta_partner_ids, "Org 2 sees Org 2 driver")
    assert_true(driver1["id"] not in beta_partner_ids, "Org 2 CANNOT see Org 1 driver")
    assert_true(custom_driver["id"] not in beta_partner_ids, "Org 2 CANNOT see Org 1 custom driver")

    # TEST 1.5: Inactive user exclusion
    client.patch(f"/users/{custom_driver['id']}", json={"status": "inactive"}, headers=auth_admin1)
    res_inactive_check = client.get("/deliveries/partners", headers=auth_admin1)
    inactive_check_ids = [p["id"] for p in res_inactive_check.json()]
    assert_true(custom_driver["id"] not in inactive_check_ids, "Deactivated driver is excluded from /deliveries/partners")

    # TEST 1.6: Genuine driver assignment to order works
    order_res = client.post("/orders", json={
        "customer_id": cust1["id"],
        "items": [{"product_id": prod1["id"], "quantity": 2}],
    }, headers=auth_admin1)
    assert_eq(order_res.status_code, 201, "Order created")
    order_id = order_res.json()["id"]

    assign_res = client.patch(f"/orders/{order_id}/assign-delivery-partner", json={
        "delivery_partner_id": driver1["id"],
    }, headers=auth_admin1)
    assert_eq(assign_res.status_code, 200, "Genuine delivery partner can be assigned to order")
    assert_eq(assign_res.json()["assigned_delivery_partner_id"], driver1["id"], "Order assigned_delivery_partner_id updated")

    # TEST 1.7: Non-delivery staff cannot be assigned to order
    assign_fail = client.patch(f"/orders/{order_id}/assign-delivery-partner", json={
        "delivery_partner_id": dispatcher["id"],
    }, headers=auth_admin1)
    assert_eq(assign_fail.status_code, 400, "Non-delivery staff assignment rejected (HTTP 400)")

    print("\n--- TASK 2: GENERAL ORDER EDIT API (PATCH /orders/{order_id}) ---")

    # Create a fresh order for editing
    o_res = client.post("/orders", json={
        "customer_id": cust1["id"],
        "items": [{"product_id": prod1["id"], "quantity": 5, "unit_price": 100.0, "tax_rate": 18.0}],
        "notes": "Original note",
        "payment_terms": "Net 30",
        "discount": 10.0,
    }, headers=auth_sales1)
    assert_eq(o_res.status_code, 201, "Sales Officer created order")
    order1 = o_res.json()
    order1_id = order1["id"]
    assert_eq(order1["total"], 580.0, "Initial total = 5*100 = 500 - 10 + 90 = 580")

    # TEST 2.1: Header field update by Admin
    edit_header_res = client.patch(f"/orders/{order1_id}", json={
        "notes": "Updated note by Admin",
        "payment_terms": "Net 15",
        "delivery_date": "2026-09-15T00:00:00Z",
        "billing_address": "New Billing Address 99",
    }, headers=auth_admin1)
    assert_eq(edit_header_res.status_code, 200, "Admin can edit order header fields (HTTP 200)")
    assert_eq(edit_header_res.json()["notes"], "Updated note by Admin", "PATCH response has updated notes")
    assert_eq(edit_header_res.json()["payment_terms"], "Net 15", "PATCH response has updated payment terms")

    # Verify persistence via GET /orders/{id}
    fetch_res = client.get(f"/orders/{order1_id}", headers=auth_admin1)
    assert_eq(fetch_res.status_code, 200, "GET /orders/{id} succeeds")
    assert_eq(fetch_res.json()["notes"], "Updated note by Admin", "Notes persisted in DB")
    assert_eq(fetch_res.json()["payment_terms"], "Net 15", "Payment terms persisted in DB")
    assert_eq(fetch_res.json()["billing_address"], "New Billing Address 99", "Billing address persisted in DB")

    # TEST 2.2: Sales Officer can edit their own order
    sales_edit_res = client.patch(f"/orders/{order1_id}", json={
        "notes": "Updated note by Sales Officer",
    }, headers=auth_sales1)
    assert_eq(sales_edit_res.status_code, 200, "Sales Officer can edit own order (HTTP 200)")
    assert_eq(sales_edit_res.json()["notes"], "Updated note by Sales Officer", "Sales Officer edit persisted")

    # TEST 2.3: Sales Officer CANNOT edit another user's order
    order_admin_res = client.post("/orders", json={
        "customer_id": cust1["id"],
        "items": [{"product_id": prod1["id"], "quantity": 1}],
    }, headers=auth_admin1)
    order_admin_id = order_admin_res.json()["id"]

    sales_forbidden = client.patch(f"/orders/{order_admin_id}", json={
        "notes": "Hacked by Sales Officer",
    }, headers=auth_sales1)
    assert_eq(sales_forbidden.status_code, 404, "Sales Officer out-of-scope edit rejected with HTTP 404")

    # TEST 2.4: Unauthorized roles blocked from editing
    acct_edit = client.patch(f"/orders/{order1_id}", json={"notes": "Accountant edit"}, headers=auth_acct1)
    assert_eq(acct_edit.status_code, 403, "Accountant edit rejected (HTTP 403)")

    driver_edit = client.patch(f"/orders/{order1_id}", json={"notes": "Driver edit"}, headers=auth_driver1)
    assert_eq(driver_edit.status_code, 403, "Delivery partner edit rejected (HTTP 403)")

    unauth_edit = client.patch(f"/orders/{order1_id}", json={"notes": "Unauth edit"})
    assert_true(unauth_edit.status_code in (401, 403), "Unauthenticated edit rejected")

    # TEST 2.5: Cross-organization edit blocked
    cross_org_edit = client.patch(f"/orders/{order1_id}", json={"notes": "Beta firm edit"}, headers=auth_admin2)
    assert_eq(cross_org_edit.status_code, 404, "Cross-org edit rejected with HTTP 404")

    # TEST 2.6: Line item update & Total recalculation & Stock reservation update
    inv_before = client.get(f"/inventory/{prod1['id']}", headers=auth_admin1).json()
    available_before = inv_before["total_stock"]

    edit_items_res = client.patch(f"/orders/{order1_id}", json={
        "items": [
            {"product_id": prod1["id"], "quantity": 3, "unit_price": 100.0, "tax_rate": 18.0},
            {"product_id": prod2["id"], "quantity": 2, "unit_price": 200.0, "tax_rate": 18.0},
        ],
        "discount": 20.0,
    }, headers=auth_admin1)
    assert_eq(edit_items_res.status_code, 200, "Admin can edit order line items (HTTP 200)")
    order_data = edit_items_res.json()
    assert_eq(len(order_data["items"]), 2, "Order now has 2 line items")
    # Subtotal = 3*100 + 2*200 = 300 + 400 = 700
    assert_eq(order_data["subtotal"], 700.0, "Subtotal recalculated to 700")
    # Tax = 700 * 18% = 126
    assert_eq(order_data["tax"], 126.0, "Tax recalculated to 126")
    # Total = 700 - 20 + 126 = 806
    assert_eq(order_data["total"], 806.0, "Total recalculated to 806 (700 - 20 + 126)")

    # Verify item reserved_quantity
    assert_eq(order_data["items"][0]["reserved_quantity"], 3.0, "Prod1 item has reserved_quantity = 3")
    assert_eq(order_data["items"][1]["reserved_quantity"], 2.0, "Prod2 item has reserved_quantity = 2")

    # Verify persistence of items via re-fetch
    refetch = client.get(f"/orders/{order1_id}", headers=auth_admin1).json()
    assert_eq(len(refetch["items"]), 2, "Persisted order has 2 items on re-fetch")
    assert_eq(refetch["total"], 806.0, "Persisted order total is 806 on re-fetch")
    assert_eq(refetch["items"][0]["reserved_quantity"], 3.0, "Re-fetched item 1 reserved_quantity is 3")
    assert_eq(refetch["items"][1]["reserved_quantity"], 2.0, "Re-fetched item 2 reserved_quantity is 2")

    # Check stock summary on order response: total prod1 warehouse reservations is 6 (3 for this order + 2 from test 1.6 + 1 from test 2.3)
    s1 = next((s for s in order_data["stock_summary"] if s["product_id"] == prod1["id"]), None)
    assert_true(s1 is not None, "Stock summary for prod1 present")
    assert_eq(s1["reserved"], 6.0, "Prod1 has 6 reserved across warehouse")
    assert_eq(s1["available"], 44.0, "Prod1 has 44 available across warehouse (50 - 6)")

    # TEST 2.7: Customer change on order
    edit_cust_res = client.patch(f"/orders/{order1_id}", json={
        "customer_id": cust2["id"],
    }, headers=auth_admin1)
    assert_eq(edit_cust_res.status_code, 200, "Customer can be changed on order")
    assert_eq(edit_cust_res.json()["customer_id"], cust2["id"], "Customer updated in response")
    refetch_cust = client.get(f"/orders/{order1_id}", headers=auth_admin1).json()
    assert_eq(refetch_cust["customer_id"], cust2["id"], "Customer change persisted on re-fetch")

    # TEST 2.8: Invalid customer / product from another org rejected
    bad_cust = client.patch(f"/orders/{order1_id}", json={"customer_id": cust_beta["id"]}, headers=auth_admin1)
    assert_eq(bad_cust.status_code, 400, "Cross-org customer rejected (HTTP 400)")

    bad_prod = client.patch(f"/orders/{order1_id}", json={
        "items": [{"product_id": prod_beta["id"], "quantity": 1}],
    }, headers=auth_admin1)
    assert_eq(bad_prod.status_code, 400, "Cross-org product rejected (HTTP 400)")

    # TEST 2.9: Cancelled order cannot be edited
    cancel_res = client.patch(f"/orders/{order1_id}/cancel", json={"reason": "Customer changed mind"}, headers=auth_admin1)
    assert_eq(cancel_res.status_code, 200, "Order cancelled")

    edit_cancelled = client.patch(f"/orders/{order1_id}", json={"notes": "Try edit cancelled"}, headers=auth_admin1)
    assert_eq(edit_cancelled.status_code, 400, "Cannot edit cancelled order (HTTP 400)")

    # TEST 2.10: Completed order cannot be edited
    # Create order, complete via pickup flow
    order_pickup_res = client.post("/orders", json={
        "customer_id": cust1["id"],
        "items": [{"product_id": prod1["id"], "quantity": 1}],
        "fulfilment_method": "pickup",
    }, headers=auth_admin1)
    order_pickup_id = order_pickup_res.json()["id"]

    client.post(f"/orders/{order_pickup_id}/pickup/pick", headers=auth_admin1)
    client.post(f"/orders/{order_pickup_id}/pickup/ready", headers=auth_admin1)
    pickup_confirm_res = client.post(f"/orders/{order_pickup_id}/pickup/confirm", json={
        "collected_by": "John Doe",
    }, headers=auth_admin1)
    assert_eq(pickup_confirm_res.status_code, 200, "Pickup confirmed")
    assert_eq(pickup_confirm_res.json()["status"], "completed", "Order status is completed")

    edit_completed = client.patch(f"/orders/{order_pickup_id}", json={"notes": "Try edit completed"}, headers=auth_admin1)
    assert_eq(edit_completed.status_code, 400, "Cannot edit completed order (HTTP 400)")

    # TEST 2.11: Super Admin can edit order
    if auth_superadmin:
        order_sa_res = client.post("/orders", json={
            "customer_id": cust1["id"],
            "items": [{"product_id": prod1["id"], "quantity": 1}],
        }, headers=auth_admin1)
        order_sa_id = order_sa_res.json()["id"]
        sa_edit = client.patch(f"/orders/{order_sa_id}", json={"notes": "Super Admin Edit"}, headers=auth_superadmin)
        assert_eq(sa_edit.status_code, 200, "Super Admin can edit order")
        assert_eq(sa_edit.json()["notes"], "Super Admin Edit", "Super Admin edit persisted")

    # TEST 2.12: Existing confirm / invoice / workflow regression check
    order_inv_res = client.post("/orders", json={
        "customer_id": cust1["id"],
        "items": [{"product_id": prod1["id"], "quantity": 2, "unit_price": 100.0}],
        "fulfilment_method": "pickup",
    }, headers=auth_admin1)
    order_inv_id = order_inv_res.json()["id"]

    # Edit before pickup / invoice
    edit_pre_inv = client.patch(f"/orders/{order_inv_id}", json={"notes": "Final edit before billing"}, headers=auth_admin1)
    assert_eq(edit_pre_inv.status_code, 200, "Can edit order before delivery/pickup/invoicing")
    assert_eq(edit_pre_inv.json()["notes"], "Final edit before billing", "Pre-invoice edit persisted")

    # Complete pickup so it is delivered
    client.post(f"/orders/{order_inv_id}/pickup/pick", headers=auth_admin1)
    client.post(f"/orders/{order_inv_id}/pickup/ready", headers=auth_admin1)
    client.post(f"/orders/{order_inv_id}/pickup/confirm", json={"collected_by": "Buyer"}, headers=auth_admin1)

    # Generate invoice from order
    inv_gen_res = client.post(f"/orders/{order_inv_id}/invoice", headers=auth_admin1)
    assert_eq(inv_gen_res.status_code, 201, "Invoice generated from order (HTTP 201)")

    # Editing order after invoice is issued should be rejected
    edit_after_inv = client.patch(f"/orders/{order_inv_id}", json={"notes": "Edit after invoice"}, headers=auth_admin1)
    assert_eq(edit_after_inv.status_code, 400, "Cannot edit order after invoice generated (HTTP 400)")

    print("\n=======================================================")
    print(f"RESULTS: {_passed} passed, {_failed} failed")
    print("=======================================================")
    if _failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
