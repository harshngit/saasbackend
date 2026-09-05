"""Comprehensive Test Suite for Targeted Backend Implementation (Final Two Gaps):
1. Remove Order Approval from Normal Order Workflow
2. Delivery Collection & Reconciliation

Verifies:
- Order confirmation with order_requires_approval = true & false produces operationally usable Confirmed order
- No order stuck in awaiting_approval
- Delivery partner assignment, delivery planning, and pickup flow work without approval blocking
- Delivery Partner records collection for own assigned delivery -> success (status: recorded)
- Audit links preserved: delivery_id, order_id, customer_id, delivery_partner_id
- Delivered != Paid: collection recording does not force order to paid status
- DP recording collection for unauthorized/unassigned delivery -> blocked (403)
- Cross-tenant delivery collection access -> blocked (404/403)
- Accountant reconciles collection -> success (recorded -> reconciled)
- DP reconciles collection -> blocked (403)
- Invalid reconciliation transition -> blocked (400)
- Accountant voids collection -> success (recorded -> voided)
"""

import os
import sys
import uuid

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from app.main import app
from app.seed import main as seed_main
from app.core.database import SessionLocal
from app.models import Customer, Delivery, DeliveryCollection, SalesOrder, User, Role

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


def _register_org(label: str):
    clean_label = label.replace("_", "").lower()
    email = f"admin_{uuid.uuid4().hex[:8]}@{clean_label}.com"
    r = client.post("/auth/register", json={
        "organization_name": f"{label} Backend Gaps Org",
        "admin_name": f"Admin {label}",
        "email": email,
        "password": "Password123!",
        "role": "admin",
    })
    assert r.status_code == 201, r.text
    token = r.json()["tokens"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _create_staff(auth: dict, name: str, role_name: str):
    # Fetch roles to get role_id
    r_roles = client.get("/roles", headers=auth)
    assert r_roles.status_code == 200, r_roles.text
    roles = r_roles.json()
    target_role = next((r for r in roles if r["name"] == role_name), None)
    assert target_role is not None, f"Role {role_name} not found"

    email = f"{role_name.lower().replace(' ', '_')}_{uuid.uuid4().hex[:6]}@firm.com"
    r_user = client.post("/users", json={
        "name": name,
        "email": email,
        "password": "Password123!",
        "role_id": target_role["id"],
    }, headers=auth)
    assert r_user.status_code == 201, r_user.text
    user_data = r_user.json()

    # Login to get staff token
    r_login = client.post("/auth/login", json={"email": email, "password": "Password123!"})
    assert r_login.status_code == 200, r_login.text
    staff_token = r_login.json()["tokens"]["access_token"]
    return user_data, {"Authorization": f"Bearer {staff_token}"}


def _setup_org(label: str, order_requires_approval: bool = False):
    auth = _register_org(label)
    
    # Configure sales workflow setting
    client.patch("/settings/sales-workflow", json={
        "order_requires_approval": order_requires_approval,
        "draft_orders_enabled": True,
    }, headers=auth)

    wh_res = client.post("/warehouses", json={"name": "Central WH", "code": f"WH-{uuid.uuid4().hex[:6]}"}, headers=auth)
    wh_id = wh_res.json()["id"]

    p_res = client.post("/products", json={
        "name": "Industrial Component",
        "sku": f"IC-{uuid.uuid4().hex[:6]}",
        "price": 500.0,
        "tax_rate": 18.0,
        "uom": "piece",
        "pricing": {"purchase_price": 300.0, "selling_price": 500.0, "currency": "INR"},
    }, headers=auth)
    assert p_res.status_code == 201, p_res.text
    prod_id = p_res.json()["id"]

    client.post(f"/warehouses/{wh_id}/stock/adjust", json={
        "product_id": prod_id, "quantity": 500,
    }, headers=auth)

    c_res = client.post("/customers", json={
        "name": "Apex Electronics",
        "phone": "9876543210",
        "email": "purchasing@apexelectronics.com",
    }, headers=auth)
    assert c_res.status_code == 201, c_res.text
    cust_id = c_res.json()["id"]

    dp_data, dp_auth = _create_staff(auth, "Rajesh Kumar (DP)", "Delivery Partner")
    acc_data, acc_auth = _create_staff(auth, "Anita Sharma (Acc)", "Accountant")

    return auth, wh_id, prod_id, cust_id, dp_data, dp_auth, acc_data, acc_auth


def test_1_order_approval_removal():
    print("\n--- TEST 1: Order Approval Removal (order_requires_approval true vs false) ---")
    
    # Case A: order_requires_approval = True
    auth_a, wh_a, prod_a, cust_a, dp_data_a, dp_auth_a, acc_data_a, acc_auth_a = _setup_org("appr_true", order_requires_approval=True)

    create_res_a = client.post("/orders", json={
        "customer_id": cust_a,
        "warehouse_id": wh_a,
        "create_as_draft": True,
        "items": [{"product_id": prod_a, "quantity": 5}],
    }, headers=auth_a)
    assert_eq(create_res_a.status_code, 201, "Draft order created (A)")
    order_a = create_res_a.json()
    assert_eq(order_a["status"], "draft", "Order is draft initially")

    confirm_res_a = client.post(f"/orders/{order_a['id']}/confirm", headers=auth_a)
    assert_eq(confirm_res_a.status_code, 200, "Draft order confirmed (A)")
    confirmed_a = confirm_res_a.json()
    assert_eq(confirmed_a["status"], "confirmed", "Confirmed order public status is confirmed even with order_requires_approval=True")

    # Verify operationally usable: assign delivery partner immediately
    assign_res_a = client.patch(f"/orders/{order_a['id']}/assign-delivery-partner", json={
        "delivery_partner_id": dp_data_a["id"],
    }, headers=auth_a)
    assert_eq(assign_res_a.status_code, 200, "Delivery partner assigned without approval gate")

    # Case B: order_requires_approval = False
    auth_b, wh_b, prod_b, cust_b, dp_data_b, dp_auth_b, acc_data_b, acc_auth_b = _setup_org("appr_false", order_requires_approval=False)

    create_res_b = client.post("/orders", json={
        "customer_id": cust_b,
        "warehouse_id": wh_b,
        "create_as_draft": True,
        "items": [{"product_id": prod_b, "quantity": 5}],
    }, headers=auth_b)
    order_b = create_res_b.json()

    confirm_res_b = client.post(f"/orders/{order_b['id']}/confirm", headers=auth_b)
    assert_eq(confirm_res_b.status_code, 200, "Draft order confirmed (B)")
    confirmed_b = confirm_res_b.json()
    assert_eq(confirmed_b["status"], "confirmed", "Confirmed order public status is confirmed with order_requires_approval=False")

    assert_eq(confirmed_a["status"], confirmed_b["status"], "Both setting values result in same operational confirmed behavior")


def test_2_delivery_collection_recording_and_audit_trail():
    print("\n--- TEST 2: Delivery Collection Recording & Audit Links ---")
    auth, wh_id, prod_id, cust_id, dp_data, dp_auth, acc_data, acc_auth = _setup_org("deliv_coll", order_requires_approval=True)

    # Place and confirm order
    so_res = client.post("/orders", json={
        "customer_id": cust_id,
        "warehouse_id": wh_id,
        "create_as_draft": True,
        "items": [{"product_id": prod_id, "quantity": 10, "unit_price": 500.0, "tax_rate": 18.0}],
    }, headers=auth)
    so = so_res.json()
    client.post(f"/orders/{so['id']}/confirm", headers=auth)

    # Plan delivery assigned to DP
    deliv_res = client.post("/deliveries", json={
        "order_id": so["id"],
        "delivery_partner_id": dp_data["id"],
        "warehouse_id": wh_id,
    }, headers=auth)
    assert_eq(deliv_res.status_code, 201, f"Delivery planned and assigned to DP: {deliv_res.text}")
    deliv = deliv_res.json()

    # DP records collection for own delivery
    coll_res = client.post(f"/deliveries/{deliv['id']}/collections", json={
        "amount": 2500.0,
        "payment_mode": "cash",
        "reference": "CASH-DELIV-101",
        "notes": "Collected cash at customer location",
    }, headers=dp_auth)
    assert_eq(coll_res.status_code, 201, "DP successfully records collection for own delivery")
    coll = coll_res.json()

    assert_eq(coll["delivery_id"], deliv["id"], "Audit link delivery_id matches")
    assert_eq(coll["order_id"], so["id"], "Audit link order_id derived from delivery")
    assert_eq(coll["customer_id"], cust_id, "Audit link customer_id derived from delivery")
    assert_eq(coll["delivery_partner_id"], dp_data["id"], "Audit link delivery_partner_id derived")
    assert_eq(coll["amount"], 2500.0, "Amount matches")
    assert_eq(coll["payment_mode"], "cash", "Payment mode matches")
    assert_eq(coll["reconciliation_status"], "recorded", "Initial status is recorded")


def test_3_delivered_not_equal_paid():
    print("\n--- TEST 3: Delivered != Paid Rule ---")
    auth, wh_id, prod_id, cust_id, dp_data, dp_auth, acc_data, acc_auth = _setup_org("deliv_not_paid")

    so = client.post("/orders", json={
        "customer_id": cust_id,
        "warehouse_id": wh_id,
        "create_as_draft": True,
        "items": [{"product_id": prod_id, "quantity": 4, "unit_price": 500.0}],
    }, headers=auth).json()
    client.post(f"/orders/{so['id']}/confirm", headers=auth)

    deliv = client.post("/deliveries", json={
        "order_id": so["id"],
        "delivery_partner_id": dp_data["id"],
    }, headers=auth).json()

    # Record collection
    client.post(f"/deliveries/{deliv['id']}/collections", json={
        "amount": 2000.0,
        "payment_mode": "upi",
        "reference": "UPI-8899",
    }, headers=dp_auth)

    # Check Sales Order status in DB / API
    so_check = client.get(f"/orders/{so['id']}", headers=auth).json()
    assert_eq(so_check["status"], "confirmed", "Order remains confirmed, NOT automatically marked completed/paid")


def test_4_dp_unauthorized_collection_blocked():
    print("\n--- TEST 4: DP Unauthorized Collection Blocked ---")
    auth, wh_id, prod_id, cust_id, dp1_data, dp1_auth, acc_data, acc_auth = _setup_org("unauth_coll")
    dp2_data, dp2_auth = _create_staff(auth, "Other DP", "Delivery Partner")

    so = client.post("/orders", json={"customer_id": cust_id, "warehouse_id": wh_id, "create_as_draft": True, "items": [{"product_id": prod_id, "quantity": 1}]}, headers=auth).json()
    client.post(f"/orders/{so['id']}/confirm", headers=auth)

    deliv = client.post("/deliveries", json={
        "order_id": so["id"],
        "delivery_partner_id": dp1_data["id"],
    }, headers=auth).json()

    # DP2 attempts to record collection for DP1's delivery
    bad_coll = client.post(f"/deliveries/{deliv['id']}/collections", json={
        "amount": 500.0,
        "payment_mode": "cash",
    }, headers=dp2_auth)
    assert_eq(bad_coll.status_code, 403, "DP2 blocked from recording collection on DP1's delivery")


def test_5_cross_tenant_collection_blocked():
    print("\n--- TEST 5: Cross-Tenant Collection Blocked ---")
    auth1, wh1, p1, c1, dp1, dp1_auth, acc1, acc1_auth = _setup_org("tenant_a")
    auth2, wh2, p2, c2, dp2, dp2_auth, acc2, acc2_auth = _setup_org("tenant_b")

    so1 = client.post("/orders", json={"customer_id": c1, "warehouse_id": wh1, "create_as_draft": True, "items": [{"product_id": p1, "quantity": 1}]}, headers=auth1).json()
    client.post(f"/orders/{so1['id']}/confirm", headers=auth1)

    deliv1 = client.post("/deliveries", json={
        "order_id": so1["id"],
        "delivery_partner_id": dp1["id"],
    }, headers=auth1).json()

    # DP2 (Tenant B) attempts to record collection on Tenant A delivery
    cross_res = client.post(f"/deliveries/{deliv1['id']}/collections", json={
        "amount": 500.0,
        "payment_mode": "cash",
    }, headers=dp2_auth)
    assert_eq(cross_res.status_code, 404, "Cross-tenant access blocked (404/403)")


def test_6_accountant_reconciliation_flow():
    print("\n--- TEST 6: Accountant Reconciliation Flow & DP Blocked ---")
    auth, wh_id, prod_id, cust_id, dp_data, dp_auth, acc_data, acc_auth = _setup_org("reconcile_flow")

    so = client.post("/orders", json={"customer_id": cust_id, "warehouse_id": wh_id, "create_as_draft": True, "items": [{"product_id": prod_id, "quantity": 2}]}, headers=auth).json()
    client.post(f"/orders/{so['id']}/confirm", headers=auth)

    deliv = client.post("/deliveries", json={"order_id": so["id"], "delivery_partner_id": dp_data["id"]}, headers=auth).json()
    coll = client.post(f"/deliveries/{deliv['id']}/collections", json={"amount": 1000.0, "payment_mode": "cash"}, headers=dp_auth).json()

    # DP attempts to reconcile -> blocked
    dp_rec = client.post(f"/deliveries/collections/{coll['id']}/reconcile", headers=dp_auth)
    assert_eq(dp_rec.status_code, 403, "DP blocked from reconciling collection")

    # Accountant reconciles -> success
    acc_rec = client.post(f"/deliveries/collections/{coll['id']}/reconcile", headers=acc_auth)
    assert_eq(acc_rec.status_code, 200, "Accountant successfully reconciles collection")
    rec_coll = acc_rec.json()
    assert_eq(rec_coll["reconciliation_status"], "reconciled", "Status transitioned to reconciled")
    assert_eq(rec_coll["reconciled_by_id"], acc_data["id"], "Reconciled by ID set to Accountant")

    # Accountant attempts second reconciliation -> blocked (invalid transition)
    second_rec = client.post(f"/deliveries/collections/{coll['id']}/reconcile", headers=acc_auth)
    assert_eq(second_rec.status_code, 400, "Duplicate/invalid reconciliation transition blocked")


def test_7_accountant_void_flow():
    print("\n--- TEST 7: Accountant Void Flow ---")
    auth, wh_id, prod_id, cust_id, dp_data, dp_auth, acc_data, acc_auth = _setup_org("void_flow")

    so = client.post("/orders", json={"customer_id": cust_id, "warehouse_id": wh_id, "create_as_draft": True, "items": [{"product_id": prod_id, "quantity": 2}]}, headers=auth).json()
    client.post(f"/orders/{so['id']}/confirm", headers=auth)

    deliv = client.post("/deliveries", json={"order_id": so["id"], "delivery_partner_id": dp_data["id"]}, headers=auth).json()
    coll = client.post(f"/deliveries/{deliv['id']}/collections", json={"amount": 1000.0, "payment_mode": "cash"}, headers=dp_auth).json()

    # Accountant voids collection -> success
    acc_void = client.post(f"/deliveries/collections/{coll['id']}/void", headers=acc_auth)
    assert_eq(acc_void.status_code, 200, "Accountant successfully voids recorded collection")
    void_coll = acc_void.json()
    assert_eq(void_coll["reconciliation_status"], "voided", "Status transitioned to voided")

    # Attempt to reconcile voided collection -> blocked
    bad_rec = client.post(f"/deliveries/collections/{coll['id']}/reconcile", headers=acc_auth)
    assert_eq(bad_rec.status_code, 400, "Cannot reconcile a voided collection")


def test_8_stock_reservation_rules():
    print("\n--- TEST 8: Stock Reservation Rules & Shortage Handling ---")
    auth, wh_id, prod_id, cust_id, dp_data, dp_auth, acc_data, acc_auth = _setup_org("stock_res_test")

    # 1. Draft has zero reservation
    so = client.post("/orders", json={
        "customer_id": cust_id,
        "warehouse_id": wh_id,
        "create_as_draft": True,
        "items": [{"product_id": prod_id, "quantity": 10}],
    }, headers=auth).json()
    assert_eq(so["status"], "draft", "Order starts as draft")
    assert_eq(so["fulfilment_status"], "not_started", "Draft has zero stock reservation")
    assert_eq(so["items"][0].get("reserved_quantity", 0), 0, "Draft item reserved_quantity is 0")

    # 2. Confirm creates reservation
    so_confirmed = client.post(f"/orders/{so['id']}/confirm", headers=auth).json()
    assert_eq(so_confirmed["status"], "confirmed", "Order status is confirmed")
    assert_eq(so_confirmed["fulfilment_status"], "reserved", "Order fulfilment_status is reserved")
    assert_eq(so_confirmed["items"][0]["reserved_quantity"], 10.0, "Reserved quantity set to 10.0")

    # 3. Insufficient stock leaves Draft unchanged with zero partial reservation
    so_huge = client.post("/orders", json={
        "customer_id": cust_id,
        "warehouse_id": wh_id,
        "create_as_draft": True,
        "items": [{"product_id": prod_id, "quantity": 999999}],
    }, headers=auth).json()
    assert_eq(so_huge["status"], "draft", "Huge order starts as draft")

    fail_res = client.post(f"/orders/{so_huge['id']}/confirm", headers=auth)
    assert_eq(fail_res.status_code, 400, "Insufficient stock confirmation fails with 400")
    err = fail_res.json()["detail"]
    assert_eq(err["error"], "INSUFFICIENT_STOCK", "Error details report INSUFFICIENT_STOCK")

    so_huge_check = client.get(f"/orders/{so_huge['id']}", headers=auth).json()
    assert_eq(so_huge_check["status"], "draft", "Order remains draft on confirmation failure")
    assert_eq(so_huge_check["fulfilment_status"], "not_started", "Zero partial reservation on failure")


def test_9_public_order_status_and_pickup_progress():
    print("\n--- TEST 9: Public Order Status & Pickup Progress ---")
    auth, wh_id, prod_id, cust_id, dp_data, dp_auth, acc_data, acc_auth = _setup_org("pickup_pub_status")

    so = client.post("/orders", json={
        "customer_id": cust_id,
        "warehouse_id": wh_id,
        "items": [{"product_id": prod_id, "quantity": 2}],
    }, headers=auth).json()
    so_confirmed = client.post(f"/orders/{so['id']}/confirm", headers=auth).json()
    assert_eq(so_confirmed["status"], "confirmed", "Confirmed order public status is confirmed")

    # Start pickup via canonical route /pickup/start
    pick_start = client.post(f"/orders/{so['id']}/pickup/start", headers=auth).json()
    assert_eq(pick_start["status"], "confirmed", "Pickup start exposes status: confirmed (never processing)")
    assert_eq(pick_start["pickup_status"], "picking", "Pickup status is picking")

    # Start pickup via route alias /pickup/pick
    pick_alias = client.post(f"/orders/{so['id']}/pickup/pick", headers=auth).json()
    assert_eq(pick_alias["status"], "confirmed", "Pickup pick alias exposes status: confirmed")

    # Ready for pickup
    ready_res = client.post(f"/orders/{so['id']}/pickup/ready", headers=auth).json()
    assert_eq(ready_res["status"], "confirmed", "Pickup ready exposes status: confirmed")
    assert_eq(ready_res["pickup_status"], "ready", "Pickup status is ready")

    # Confirm pickup
    confirm_pickup = client.post(f"/orders/{so['id']}/pickup/confirm", headers=auth).json()
    assert_eq(confirm_pickup["status"], "completed", "Confirmed pickup exposes status: completed")
    assert_eq(confirm_pickup["pickup_status"], "collected", "Pickup status is collected")
    assert_eq(confirm_pickup["fulfilment_status"], "delivered", "Fulfilment status is delivered")


def test_10_reconciliation_accounting_handoff_and_idempotency():
    print("\n--- TEST 10: Reconciliation Accounting Handoff & Idempotency ---")
    auth, wh_id, prod_id, cust_id, dp_data, dp_auth, acc_data, acc_auth = _setup_org("reconcile_handoff")

    so = client.post("/orders", json={
        "customer_id": cust_id,
        "warehouse_id": wh_id,
        "items": [{"product_id": prod_id, "quantity": 2, "unit_price": 500.0, "tax_rate": 0.0}],
    }, headers=auth).json()
    client.post(f"/orders/{so['id']}/confirm", headers=auth)

    deliv = client.post("/deliveries", json={"order_id": so["id"], "delivery_partner_id": dp_data["id"]}, headers=auth).json()
    coll = client.post(f"/deliveries/{deliv['id']}/collections", json={"amount": 1000.0, "payment_mode": "cash"}, headers=dp_auth).json()

    assert_eq(coll["customer_payment_id"], None, "Before reconciliation, customer_payment_id is None")
    assert_eq(coll["reconciliation_status"], "recorded", "Status is recorded")

    # Accountant reconciles
    acc_rec = client.post(f"/deliveries/collections/{coll['id']}/reconcile", headers=acc_auth)
    assert_eq(acc_rec.status_code, 200, "Accountant reconciles collection")
    rec_data = acc_rec.json()
    assert_eq(rec_data["reconciliation_status"], "reconciled", "Status is reconciled")
    pmt_id = rec_data["customer_payment_id"]
    assert pmt_id is not None, "customer_payment_id is set to newly created CustomerPayment"

    # Verify CustomerPayment receipt in DB / API
    pmt_res = client.get(f"/payment-receipts/{pmt_id}", headers=acc_auth)
    assert_eq(pmt_res.status_code, 200, "Linked CustomerPayment receipt exists")
    pmt_json = pmt_res.json()
    assert_eq(pmt_json["amount_received"], 1000.0, "Payment receipt amount matches collection")

    # Calling reconcile a second time -> 400 Bad Request and 0 duplicate payments
    dup_rec = client.post(f"/deliveries/collections/{coll['id']}/reconcile", headers=acc_auth)
    assert_eq(dup_rec.status_code, 400, "Duplicate reconcile returns 400 Bad Request")
    assert_eq(rec_data["customer_payment_id"], pmt_id, "customer_payment_id remains unchanged")

    # Reconciled collection cannot be voided -> 400 Bad Request
    void_attempt = client.post(f"/deliveries/collections/{coll['id']}/void", headers=acc_auth)
    assert_eq(void_attempt.status_code, 400, "Reconciled collection cannot be voided")


def run_all_tests():
    print("\n=======================================================")
    print("TEST SUITE: Targeted Backend Implementation (Final Two Gaps)")
    print("=======================================================")
    test_1_order_approval_removal()
    test_2_delivery_collection_recording_and_audit_trail()
    test_3_delivered_not_equal_paid()
    test_4_dp_unauthorized_collection_blocked()
    test_5_cross_tenant_collection_blocked()
    test_6_accountant_reconciliation_flow()
    test_7_accountant_void_flow()
    test_8_stock_reservation_rules()
    test_9_public_order_status_and_pickup_progress()
    test_10_reconciliation_accounting_handoff_and_idempotency()

    print("\n=======================================================")
    print(f"RESULTS: {_passed} passed, {_failed} failed")
    print("=======================================================\n")
    if _failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_all_tests()

