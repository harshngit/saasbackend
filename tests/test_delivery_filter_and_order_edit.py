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
    client.post(f"/orders/{order_id}/confirm", headers=auth_admin1)

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
    # Every order now starts as an unreserved Draft -- confirm it so later
    # line-item-edit assertions (which test reservation adjustment, only
    # meaningful once an order is past Draft) see the same starting state
    # they did before that change.
    order1 = client.post(f"/orders/{order1_id}/confirm", headers=auth_sales1).json()

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
    client.post(f"/orders/{order_admin_id}/confirm", headers=auth_admin1)

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
    client.post(f"/orders/{order_pickup_id}/confirm", headers=auth_admin1)

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
    client.post(f"/orders/{order_inv_id}/confirm", headers=auth_admin1)

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

    print("\n--- Assigned Deliveries: status filters (GET /deliveries?status=) ---")
    test_assigned_deliveries_status_filters()
    ok("Assigned Deliveries status filters, search, sort, pagination and scoping")
    test_delivery_summary_and_internal_filters()
    ok("Delivery summary counts, internal_status / picking_status filters, unassigned, scheduled_date")
    test_order_cancel_follows_or_refuses_deliveries()
    ok("Order cancel cancels its deliveries, or refuses when one is already out")

    print("\n=======================================================")
    print(f"RESULTS: {_passed} passed, {_failed} failed")
    print("=======================================================")
    if _failed > 0:
        sys.exit(1)


def test_assigned_deliveries_status_filters():
    """The Assigned Deliveries screen (GET /deliveries as a Delivery Partner) filters by the
    status each card shows: All / Accepted / Pending / Intransit / Delivered.

    Responses carry the public status, but the filter used to compare the stored column
    with it: "pending" is stored as "planned" (0 results) and "in_transit" also covers
    stored "loaded" (missed). ?search= also answered 500 (it filtered on a property).
    """
    from datetime import datetime, timedelta, timezone

    from app.core.database import SessionLocal
    from app.models import Delivery, User

    admin, _ = _register_org("AssignedFilters")
    dp1_auth, dp1 = _create_staff(admin, "Driver One", "Delivery Partner")
    dp2_auth, dp2 = _create_staff(admin, "Driver Two", "Delivery Partner")
    other_admin, _ = _register_org("OtherFirm")
    other_dp_auth, other_dp = _create_staff(other_admin, "Other Driver", "Delivery Partner")

    db = SessionLocal()
    try:
        org_id = db.get(User, dp1["id"]).organization_id
        other_org = db.get(User, other_dp["id"]).organization_id
        tag = uuid.uuid4().hex[:6]
        start = datetime.now(timezone.utc) - timedelta(days=30)
        made: dict[str, list[str]] = {}

        def add(key, stored, partner, org=org_id, minutes=0, notes=None):
            d = Delivery(organization_id=org, delivery_note_number=f"DN-{tag}-{len(made.get(key, []))}-{key}",
                         status=stored, delivery_partner_id=partner, notes=notes,
                         created_at=start + timedelta(minutes=minutes))
            db.add(d)
            db.flush()
            made.setdefault(key, []).append(d.id)

        # Pending ones are the OLDEST: with the default newest-first order they sit beyond
        # the first unfiltered page of 10.
        add("pending", "planned", dp1["id"], minutes=1, notes=f"gate code {tag}")
        add("pending", "planned", dp1["id"], minutes=2)
        add("accepted", "accepted", dp1["id"], minutes=10)
        add("accepted", "ready", dp1["id"], minutes=11)
        add("in_transit", "loaded", dp1["id"], minutes=20, notes=f"fragile {tag}")
        add("in_transit", "in_transit", dp1["id"], minutes=21)
        for i in range(10):
            add("delivered", "delivered", dp1["id"], minutes=100 + i)
        add("dp2", "in_transit", dp2["id"], minutes=30)
        add("dp2", "planned", dp2["id"], minutes=31)
        add("other_org", "planned", other_dp["id"], org=other_org, minutes=5)
        db.commit()
    finally:
        db.close()
    mine = {i for k in ("pending", "accepted", "in_transit", "delivered") for i in made[k]}

    def listing(auth, **params):
        r = client.get("/deliveries", params={"page_size": 100, **params}, headers=auth)
        assert r.status_code == 200, (params, r.status_code, r.text)
        return r.json()

    def ids(auth, **params):
        return {d["id"] for d in listing(auth, **params)["items"]}

    # 1. All: every delivery assigned to this partner, and nobody else's.
    all_page = listing(dp1_auth)
    assert {d["id"] for d in all_page["items"]} == mine and all_page["total"] == len(mine)
    assert ids(dp1_auth, status="all") == mine and ids(dp1_auth, status="") == mine

    # 2.-5. Each Flutter filter, in every spelling seen, matches what the cards show.
    expected = {
        "accepted": ({"Accepted", "accepted", "ACCEPTED"}, "accepted"),
        "pending": ({"Pending", "pending"}, "pending"),
        "in_transit": ({"Intransit", "intransit", "in_transit", "In Transit", "in-transit"}, "in_transit"),
        "delivered": ({"Delivered", "delivered"}, "delivered"),
    }
    for key, (spellings, public) in expected.items():
        for spelling in spellings:
            page = listing(dp1_auth, status=spelling)
            got = {d["id"] for d in page["items"]}
            assert got == set(made[key]), (spelling, len(got), len(made[key]))
            assert page["total"] == len(made[key]), (spelling, page["total"])
            assert all(d["status"] == public for d in page["items"]), (spelling, {d["status"] for d in page["items"]})

    # Stored values still filter exactly, as before (web consumers).
    assert ids(dp1_auth, status="loaded") == {made["in_transit"][0]}
    assert ids(dp1_auth, status="ready") == {made["accepted"][1]}
    assert ids(dp1_auth, status="planned") == set(made["pending"])
    bad = client.get("/deliveries", params={"status": "bogus"}, headers=dp1_auth)
    assert bad.status_code == 422 and "status must be one of" in bad.json()["detail"]

    # 6. Filtering happens before pagination: the pending ones are not on the first
    #    unfiltered page, yet the filtered first page holds both.
    first_unfiltered = ids(dp1_auth, page_size=10)
    assert not (set(made["pending"]) & first_unfiltered)
    assert ids(dp1_auth, status="Pending", page_size=10) == set(made["pending"])

    # 9. Pagination totals follow the filter.
    p1 = listing(dp1_auth, status="Delivered", page_size=4, page=1)
    p3 = listing(dp1_auth, status="Delivered", page_size=4, page=3)
    assert (p1["total"], p1["total_pages"], len(p1["items"]), len(p3["items"])) == (10, 3, 4, 2)
    paged = set()
    for n in (1, 2, 3):
        paged |= {d["id"] for d in listing(dp1_auth, status="Delivered", page_size=4, page=n)["items"]}
    assert paged == set(made["delivered"])

    # 7. Search together with a status filter (search used to answer 500).
    assert ids(dp1_auth, status="Pending", search=f"gate code {tag}") == {made["pending"][0]}
    assert ids(dp1_auth, status="Intransit", search=f"fragile {tag}") == {made["in_transit"][0]}
    assert ids(dp1_auth, status="Intransit", search=tag) == set(made["in_transit"])  # tag is in every number
    assert ids(dp1_auth, status="Delivered", search=f"gate code {tag}") == set()
    assert ids(dp1_auth, search=f"DN-{tag}-0-accepted") == {made["accepted"][0]}

    # 8. Sorting together with a status filter.
    asc = [d["id"] for d in listing(dp1_auth, status="Intransit", sort_by="delivery_number", sort_order="asc")["items"]]
    desc = [d["id"] for d in listing(dp1_auth, status="Intransit", sort_by="delivery_number", sort_order="desc")["items"]]
    assert set(asc) == set(made["in_transit"]) and asc == list(reversed(desc)) and len(asc) == 2

    # 10. Another partner's deliveries are never exposed, whatever the filter.
    for spelling in ("all", "Pending", "Intransit"):
        assert not (ids(dp1_auth, status=spelling) & set(made["dp2"]))
    assert ids(dp2_auth) == set(made["dp2"])
    assert ids(dp2_auth, status="Intransit") == {made["dp2"][0]}

    # 11. Organization isolation: the other firm's admin and partner see none of these.
    assert not (ids(other_admin) & (mine | set(made["dp2"])))
    assert ids(other_dp_auth, status="Pending") == set(made["other_org"])

    # 12. Transitions still work, and the card moves between filters.
    pending_id = made["pending"][1]
    accepted = client.post(f"/deliveries/{pending_id}/accept", headers=dp1_auth)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "accepted" and accepted.json()["internal_status"] == "accepted"
    assert pending_id not in ids(dp1_auth, status="Pending")
    assert pending_id in ids(dp1_auth, status="Accepted")
    # dp2 cannot accept dp1's delivery.
    assert client.post(f"/deliveries/{made['pending'][0]}/accept", headers=dp2_auth).status_code in (403, 404)

    # 13. Response shape unchanged for existing consumers.
    item = listing(dp1_auth, status="Delivered", page_size=1)
    assert set(item) == {"items", "total", "page", "page_size", "total_pages"}
    assert {"id", "status", "internal_status", "delivery_number"} <= set(item["items"][0])


def _seed_deliveries(org_id: str, rows: list[tuple[str, str | None, dict]]) -> list[str]:
    """Insert deliveries directly: (stored status, partner id, extra columns)."""
    from app.core.database import SessionLocal
    from app.models import Delivery

    db = SessionLocal()
    try:
        tag, made = uuid.uuid4().hex[:6], []
        for i, (stored, partner, extra) in enumerate(rows):
            d = Delivery(organization_id=org_id, delivery_note_number=f"DS-{tag}-{i}", status=stored,
                         delivery_partner_id=partner, **extra)
            db.add(d)
            db.flush()
            made.append(d.id)
        db.commit()
        return made
    finally:
        db.close()


def _org_of(user_id: str) -> str:
    from app.core.database import SessionLocal
    from app.models import User

    db = SessionLocal()
    try:
        return db.get(User, user_id).organization_id
    finally:
        db.close()


def test_delivery_summary_and_internal_filters():
    """GET /deliveries/summary (one call instead of one per status), the internal_status /
    picking_status filters, the "unassigned" status and the scheduled_date fallback."""
    from datetime import datetime, timezone

    admin, _ = _register_org("Summary")
    dp_auth, dp = _create_staff(admin, "Driver S", "Delivery Partner")
    org = _org_of(dp["id"])
    when = datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)
    ids = _seed_deliveries(org, [
        ("planned", None, {"delivery_date": when}),               # 0 unassigned, no scheduled_date
        ("planned", dp["id"], {}),                                 # 1 pending
        ("accepted", dp["id"], {"picking_status": "picking"}),     # 2 accepted, picking
        ("accepted", dp["id"], {"picking_status": "not_started"}), # 3 accepted
        ("ready", dp["id"], {"picking_status": "picked"}),         # 4 accepted (ready)
        ("loaded", dp["id"], {}),                                  # 5 in_transit
        ("in_transit", dp["id"], {}),                              # 6 in_transit
        ("delivered", dp["id"], {}),                               # 7 delivered
        ("failed", dp["id"], {}),                                  # 8 returned
        ("rejected", dp["id"], {}),                                # 9 rejected
        ("cancelled", None, {}),                                   # 10 cancelled
    ])

    s = client.get("/deliveries/summary", headers=admin)
    assert s.status_code == 200, s.text
    body = s.json()
    assert body["total"] == 11
    assert body["by_status"] == {"unassigned": 1, "pending": 1, "accepted": 3, "in_transit": 2,
                                 "partially_delivered": 0, "delivered": 1, "rejected": 1,
                                 "returned": 1, "cancelled": 1}
    assert body["by_internal_status"]["planned"] == 2 and body["by_internal_status"]["failed"] == 1
    assert body["by_internal_status"]["ready"] == 1 and body["by_internal_status"]["loaded"] == 1
    assert sum(body["by_internal_status"].values()) == 11
    # Each count equals what the list returns for that filter.
    for key, count in body["by_status"].items():
        listed = client.get("/deliveries", params={"status": key, "page_size": 1}, headers=admin).json()
        assert listed["total"] == count, (key, listed["total"], count)
    # A delivery partner's summary counts only their own (no unassigned / other rows).
    mine = client.get("/deliveries/summary", headers=dp_auth).json()
    assert mine["total"] == 9 and mine["by_status"]["unassigned"] == 0 and mine["by_status"]["cancelled"] == 0
    # Filters narrow the summary like the list.
    assert client.get("/deliveries/summary", params={"delivery_partner_id": dp["id"]}, headers=admin).json()["total"] == 9

    def listed_ids(**params):
        r = client.get("/deliveries", params={"page_size": 100, **params}, headers=admin)
        assert r.status_code == 200, r.text
        return {d["id"]: d for d in r.json()["items"]}

    # "unassigned" is its own status, both in the list and as a filter; "pending" keeps
    # only deliveries waiting for a named partner.
    assert set(listed_ids(status="unassigned")) == {ids[0]}
    assert set(listed_ids(status="Pending")) == {ids[1]}
    assert set(listed_ids(status="planned")) == {ids[0], ids[1]}  # stored value: both
    rows = listed_ids()
    assert rows[ids[0]]["status"] == "unassigned" and rows[ids[1]]["status"] == "pending"
    # internal_status / picking_status filters (comma-separated).
    assert set(listed_ids(internal_status="ready")) == {ids[4]}
    assert set(listed_ids(internal_status="loaded,in_transit")) == {ids[5], ids[6]}
    assert set(listed_ids(picking_status="picking")) == {ids[2]}
    assert set(listed_ids(status="accepted", picking_status="not_started")) == {ids[3]}
    for bad in ({"internal_status": "picking"}, {"picking_status": "loaded"}, {"status": "bogus"}):
        r = client.get("/deliveries", params=bad, headers=admin)
        assert r.status_code == 422, (bad, r.text)
    # scheduled_date falls back to delivery_date when it was never saved.
    assert rows[ids[0]]["scheduled_date"].startswith("2026-11-20")


def test_order_cancel_follows_or_refuses_deliveries():
    """Cancelling an order cancels its deliveries; if one is already loaded / in transit,
    the cancel is refused (it used to go through and leave that delivery in transit under
    a cancelled order whenever the order's own fulfilment_status lagged behind)."""
    admin, _ = _register_org("CancelRule")
    product = _create_product(admin, "Cancel Widget")
    customer = _create_customer(admin, "Cancel Cust")

    def new_order():
        r = client.post("/orders", json={"customer_id": customer["id"], "items": [
            {"product_id": product["id"], "quantity": 1, "unit_price": 10}]}, headers=admin)
        assert r.status_code == 201, r.text
        return r.json()

    o1 = new_order()
    org = o1["organization_id"]
    planned, accepted = _seed_deliveries(org, [("planned", None, {"sales_order_id": o1["id"]}),
                                               ("accepted", None, {"sales_order_id": o1["id"]})])
    r = client.patch(f"/orders/{o1['id']}/cancel", json={"reason": "customer changed mind"}, headers=admin)
    assert r.status_code == 200, r.text
    for did in (planned, accepted):
        d = client.get(f"/deliveries/by-id/{did}", headers=admin).json()
        assert d["status"] == "cancelled" and d["internal_status"] == "cancelled", d["status"]

    # The order still says not_started, but its delivery is already on the vehicle.
    o2 = new_order()
    (loaded,) = _seed_deliveries(org, [("loaded", None, {"sales_order_id": o2["id"]})])
    r = client.patch(f"/orders/{o2['id']}/cancel", json={}, headers=admin)
    assert r.status_code == 400, r.text
    assert "already out" in r.json()["detail"] and "(in_transit)" in r.json()["detail"]
    assert client.get(f"/orders/{o2['id']}", headers=admin).json()["status"] != "cancelled"
    assert client.get(f"/deliveries/by-id/{loaded}", headers=admin).json()["internal_status"] == "loaded"


if __name__ == "__main__":
    main()
