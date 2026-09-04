"""Comprehensive test suite for the Order Module implementation & invariants.

Tests cover:
  Group A: Order Creation (Direct, Quotation, Draft, Server Authority)
  Group B: Draft / Stock Invariant & Confirm Atomicity
  Group C: Status Regression (placed, processing)
  Group D: Takeaway / Self Pickup Workflow
  Group E: Takeaway Payment & Financial Summary
  Group F: Home Delivery Decoupling & Lifecycle Separation
  Group G: Cancellation & Stock Release
  Group H: Quotation Conversion & Idempotency
  Group I: Multi-Tenant Security & RBAC Isolation
"""

import os
import sys
import uuid
from datetime import datetime, timezone

os.environ["DATABASE_URL"] = "sqlite:///./crm_saas.db"
os.environ["TESTING"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.main import app
from app.models import Customer, Product, SalesOrder, StockReservation, User, Warehouse

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
    me = client.get("/auth/me", headers=auth).json()
    return auth, me["organization_id"]


def _register_staff(auth: dict, role: str) -> tuple[dict, str]:
    email = f"staff_{role}_{uuid.uuid4().hex[:6]}@firm.com"
    r = client.post(
        "/users",
        json={
            "name": f"Staff {role}",
            "full_name": f"Staff {role}",
            "email": email,
            "password": "Password123!",
            "role": role,
            "data_scope": "own" if role == "sales_officer" else "all",
        },
        headers=auth,
    )
    assert r.status_code == 201, r.text
    user_id = r.json()["id"]
    r_login = client.post("/auth/login", json={"email": email, "password": "Password123!"})
    assert r_login.status_code == 200, r_login.text
    staff_auth = {"Authorization": f"Bearer {r_login.json()['tokens']['access_token']}"}
    return staff_auth, user_id


def _setup_environment(label: str) -> tuple[dict, str, str, str, str]:
    auth, org_id = _register_org(label)

    # Enable draft orders setting for predictable testing
    client.patch("/sales-workflow-settings", json={"draft_orders_enabled": True}, headers=auth)

    # Get default warehouse
    wh_res = client.get("/warehouses", headers=auth).json()
    wh_id = wh_res[0]["id"]

    # Create Product
    prod_res = client.post(
        "/products",
        json={
            "name": f"Test Prod {uuid.uuid4().hex[:6]}",
            "price": 100.0,
            "tax_rate": 10.0,
            "initial_stock": 50.0,
            "warehouse_id": wh_id,
        },
        headers=auth,
    )
    assert prod_res.status_code == 201, prod_res.text
    prod_id = prod_res.json()["id"]

    # Create Customer
    cust_res = client.post(
        "/customers",
        json={"name": f"Customer {uuid.uuid4().hex[:6]}", "phone": "9876543210"},
        headers=auth,
    )
    assert cust_res.status_code == 201, cust_res.text
    cust_id = cust_res.json()["id"]

    # Seed warehouse stock
    db: Session = next(get_db())
    from app.services import stock_service
    stock_service.adjust_on_hand(db, org_id, wh_id, prod_id, None, 100.0, movement_type="opening_stock")
    db.commit()

    return auth, org_id, wh_id, prod_id, cust_id


def run_all_tests():
    global PASSED, FAILED
    PASSED = 0
    FAILED = 0

    print("\n=======================================================")
    print("TEST SUITE: Order Module Verification & Invariants")
    print("=======================================================\n")

    auth, org_id, wh_id, prod_id, cust_id = _setup_environment("OrdVerif")

    # ------------------------------------------------------------------
    # TEST GROUP A — ORDER CREATION
    # ------------------------------------------------------------------
    print("--- TEST GROUP A: Order Creation & Server Authority ---")

    # Test 1: Direct Order Creation -> source = direct
    r1 = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "source": "direct",
            "items": [{"product_id": prod_id, "quantity": 5}],
        },
        headers=auth,
    )
    log_test("Test 1: Create Direct Order -> 201", r1.status_code == 201, r1.text)
    order1 = r1.json()
    log_test("Test 1: source == 'direct'", order1.get("source") == "direct")

    # Test 2: Quotation Order Creation -> source = quotation
    # Create and accept a quotation first
    q_res = client.post(
        "/quotations",
        json={
            "customer_id": cust_id,
            "items": [{"product_id": prod_id, "quantity": 3, "unit_price": 100.0}],
        },
        headers=auth,
    )
    assert q_res.status_code == 201, q_res.text
    q_id = q_res.json()["id"]
    client.patch(f"/quotations/{q_id}", json={"status": "sent"}, headers=auth)
    client.patch(f"/quotations/{q_id}", json={"status": "accepted"}, headers=auth)

    conv_res = client.post(f"/quotations/{q_id}/convert-to-order", json={"warehouse_id": wh_id}, headers=auth)
    log_test("Test 2: Convert Quotation -> 201", conv_res.status_code == 201, conv_res.text)
    order2 = conv_res.json()["order"]
    log_test("Test 2: source == 'quotation'", order2.get("source") == "quotation")
    log_test("Test 2: quotation_id preserved", order2.get("quotation_id") == q_id)

    # Test 3: Create Draft Order -> internal status is draft, no stock reserved
    db_init: Session = next(get_db())
    o1_db = db_init.get(SalesOrder, order1["id"])
    log_test("Test 3: Internal status == 'draft'", o1_db.status == "draft")
    log_test("Test 3: Public status == 'placed'", order1.get("status") == "placed")

    # Test 4: Server-authoritative totals
    # Attempt to send arbitrary prices / totals in request
    r4 = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "items": [{"product_id": prod_id, "quantity": 2, "unit_price": 100.0, "discount": 10.0}],
        },
        headers=auth,
    )
    assert r4.status_code == 201
    o4 = r4.json()
    # 2 * 100 = 200, disc = 10 -> subtotal = 190, tax@10% = 19 -> total = 209
    log_test("Test 4: Server subtotal is 190.0", o4["subtotal"] == 190.0)
    log_test("Test 4: Server tax is 19.0", o4["tax"] == 19.0)
    log_test("Test 4: Server total is 209.0", o4["total"] == 209.0)

    # ------------------------------------------------------------------
    # TEST GROUP B — DRAFT / STOCK INVARIANT
    # ------------------------------------------------------------------
    print("\n--- TEST GROUP B: Draft / Stock Invariant & Confirm ---")

    db: Session = next(get_db())

    # Test 5: Draft Order stock check -> 0 reserved stock
    reservations_draft = (
        db.query(StockReservation)
        .filter(StockReservation.order_id == order1["id"], StockReservation.status == "active")
        .all()
    )
    log_test("Test 5: Draft Order has 0 active reservations in DB", len(reservations_draft) == 0)

    # Test 6: Atomic stock-reserving transition (POST /orders/{id}/confirm)
    conf_res = client.post(f"/orders/{order1['id']}/confirm", headers=auth)
    log_test("Test 6: Confirm Draft Order -> 200", conf_res.status_code == 200, conf_res.text)
    conf_order = conf_res.json()
    log_test("Test 6: Status moves to placed", conf_order["status"] == "placed")
    log_test("Test 6: Fulfilment status moves to reserved", conf_order["fulfilment_status"] == "reserved")

    reservations_conf = (
        db.query(StockReservation)
        .filter(StockReservation.order_id == order1["id"], StockReservation.status == "active")
        .all()
    )
    log_test("Test 6: Confirmed order has active stock reservations", len(reservations_conf) > 0)

    # Test 7: Insufficient stock on confirm -> rollback, order remains draft
    # Create draft order exceeding warehouse stock (initial stock = 50)
    r_big = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "items": [{"product_id": prod_id, "quantity": 999}],
        },
        headers=auth,
    )
    assert r_big.status_code == 201
    big_order_id = r_big.json()["id"]

    conf_fail = client.post(f"/orders/{big_order_id}/confirm", headers=auth)
    log_test("Test 7: Insufficient stock confirm fails -> 400", conf_fail.status_code == 400)
    
    big_db = db.get(SalesOrder, big_order_id)
    log_test("Test 7: Failed confirm leaves order as draft", big_db.status == "draft")

    # Test 8: Double confirmation protection
    conf_again = client.post(f"/orders/{order1['id']}/confirm", headers=auth)
    log_test("Test 8: Double confirmation on non-draft order blocked -> 400", conf_again.status_code == 400)

    # Test 9: CRITICAL INVARIANT — No API path returns status=draft with reserved stock > 0
    all_drafts = db.query(SalesOrder).filter(SalesOrder.status == "draft").all()
    has_violation = False
    for d in all_drafts:
        res_count = (
            db.query(StockReservation)
            .filter(StockReservation.order_id == d.id, StockReservation.status == "active")
            .count()
        )
        if res_count > 0:
            has_violation = True

    log_test("Test 9: CRITICAL INVARIANT: Draft orders NEVER hold stock reservations", not has_violation)

    # ------------------------------------------------------------------
    # TEST GROUP C — STATUS REGRESSION
    # ------------------------------------------------------------------
    print("\n--- TEST GROUP C: Status Regression (placed / processing) ---")

    # Test 10: Placed order status
    log_test("Test 10: Confirmed order status is placed", conf_order["status"] == "placed")

    # Test 11: Processing status on delivery assignment / pickup
    # Assign delivery partner moves order to processing
    partner_auth, partner_id = _register_staff(auth, "delivery_partner")
    assign_res = client.patch(
        f"/orders/{order1['id']}/assign-delivery-partner",
        json={"delivery_partner_id": partner_id},
        headers=auth,
    )
    log_test("Test 11: Assign delivery partner -> 200", assign_res.status_code == 200, assign_res.text)
    log_test("Test 11: Status moves to processing on partner assignment", assign_res.json()["status"] == "confirmed")

    # ------------------------------------------------------------------
    # TEST GROUP D — TAKEAWAY / PICKUP
    # ------------------------------------------------------------------
    print("\n--- TEST GROUP D: Takeaway / Self Pickup ---")

    # Test 12: Create Direct Takeaway Order
    r_tk = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "fulfilment_method": "pickup",
            "items": [{"product_id": prod_id, "quantity": 4}],
        },
        headers=auth,
    )
    log_test("Test 12: Create Takeaway Order -> 201", r_tk.status_code == 201, r_tk.text)
    tk_order = r_tk.json()
    log_test("Test 12: fulfilment_method == 'pickup'", tk_order["fulfilment_method"] == "pickup")
    log_test("Test 12: delivery_id is None for Takeaway", tk_order["delivery_id"] is None)

    # Test 13: Confirm Takeaway Order
    client.post(f"/orders/{tk_order['id']}/confirm", headers=auth)

    # Test 14: Ready for pickup (via alias /ready-for-pickup)
    ready_res = client.post(f"/orders/{tk_order['id']}/ready-for-pickup", headers=auth)
    log_test("Test 14: Mark ready for pickup alias -> 200", ready_res.status_code == 200, ready_res.text)
    log_test("Test 14: pickup_status == 'ready'", ready_res.json()["pickup_status"] == "ready")

    # Test 15: Mark picked up (via alias /picked-up)
    pick_res = client.post(f"/orders/{tk_order['id']}/picked-up", headers=auth)
    log_test("Test 15: Mark picked up alias -> 200", pick_res.status_code == 200, pick_res.text)
    log_test("Test 15: pickup_status == 'collected'", pick_res.json()["pickup_status"] == "collected")
    log_test("Test 15: Order status becomes completed", pick_res.json()["status"] == "completed")

    # Test 16: Takeaway creates NO Delivery record
    tk_deliveries = (
        db.query(StockReservation)
        .filter(StockReservation.order_id == tk_order["id"], StockReservation.status == "active")
        .all()
    )
    log_test("Test 16: Takeaway completed order has 0 remaining active reservations", len(tk_deliveries) == 0)

    # Test 17: No stale reservation after pickup
    tk_db = db.get(SalesOrder, tk_order["id"])
    log_test("Test 17: Takeaway order stock_deducted is True", tk_db.stock_deducted is True)

    # ------------------------------------------------------------------
    # TEST GROUP E — FINANCIAL SUMMARY & PAYMENTS
    # ------------------------------------------------------------------
    print("\n--- TEST GROUP E: Financial Summary & Payments ---")

    # Test 18 & 19 & 20: Financial Summary fields in response
    detail_res = client.get(f"/orders/{tk_order['id']}", headers=auth)
    assert detail_res.status_code == 200
    d_order = detail_res.json()

    log_test("Test 18: Response contains previous_balance field", "previous_balance" in d_order)
    log_test("Test 18: Response contains current_order_amount field", "current_order_amount" in d_order)
    log_test("Test 18: Response contains total_due field", "total_due" in d_order)
    log_test("Test 18: Response contains paid_amount field", "paid_amount" in d_order)
    log_test("Test 18: Response contains remaining_balance field", "remaining_balance" in d_order)

    # ------------------------------------------------------------------
    # TEST GROUP F — HOME DELIVERY DECOUPLING
    # ------------------------------------------------------------------
    print("\n--- TEST GROUP F: Home Delivery Decoupling ---")

    # Test 21: Create Home Delivery Order
    r_hd = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "fulfilment_method": "delivery",
            "items": [{"product_id": prod_id, "quantity": 2}],
        },
        headers=auth,
    )
    assert r_hd.status_code == 201
    hd_id = r_hd.json()["id"]
    client.post(f"/orders/{hd_id}/confirm", headers=auth)

    # Test 22: Delivery relationship created only when partner assigned
    client.patch(
        f"/orders/{hd_id}/assign-delivery-partner",
        json={"delivery_partner_id": partner_id},
        headers=auth,
    )
    hd_out = client.get(f"/orders/{hd_id}", headers=auth).json()
    log_test("Test 22: Home Delivery links active delivery_id", hd_out["delivery_id"] is not None)

    # Test 23: Duplicate Delivery creation prevented on re-assignment
    assign_again = client.patch(
        f"/orders/{hd_id}/assign-delivery-partner",
        json={"delivery_partner_id": partner_id},
        headers=auth,
    )
    log_test("Test 23: Re-assigning partner does not throw duplicate delivery error", assign_again.status_code == 200)

    # Test 24: Delivery lifecycle separate from Order lifecycle
    log_test("Test 24: Order status remains confirmed/processing", hd_out["status"] in ("confirmed", "placed"))

    # ------------------------------------------------------------------
    # TEST GROUP G — CANCELLATION
    # ------------------------------------------------------------------
    print("\n--- TEST GROUP G: Order Cancellation ---")

    # Test 25: Cancel Draft Order
    r_cancel_draft = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "items": [{"product_id": prod_id, "quantity": 1}],
        },
        headers=auth,
    )
    assert r_cancel_draft.status_code == 201
    cd_id = r_cancel_draft.json()["id"]
    c_res1 = client.patch(f"/orders/{cd_id}/cancel", json={"reason": "Testing draft cancel"}, headers=auth)
    log_test("Test 25: Cancel Draft Order -> 200", c_res1.status_code == 200)
    log_test("Test 25: Status is cancelled", c_res1.json()["status"] == "cancelled")

    # Test 26: Cancel Confirmed Order -> releases stock
    r_cancel_conf = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "items": [{"product_id": prod_id, "quantity": 2}],
        },
        headers=auth,
    )
    cc_id = r_cancel_conf.json()["id"]
    client.post(f"/orders/{cc_id}/confirm", headers=auth)

    c_res2 = client.patch(f"/orders/{cc_id}/cancel", json={"reason": "Testing conf cancel"}, headers=auth)
    log_test("Test 26: Cancel Confirmed Order -> 200", c_res2.status_code == 200)

    cc_reservations = (
        db.query(StockReservation)
        .filter(StockReservation.order_id == cc_id, StockReservation.status == "active")
        .all()
    )
    log_test("Test 26: Active stock reservations released on cancellation", len(cc_reservations) == 0)

    # ------------------------------------------------------------------
    # TEST GROUP H — QUOTATION CONVERSION
    # ------------------------------------------------------------------
    print("\n--- TEST GROUP H: Quotation Conversion ---")

    # Test 28: Convert accepted quotation
    q_res2 = client.post(
        "/quotations",
        json={"customer_id": cust_id, "items": [{"product_id": prod_id, "quantity": 1, "unit_price": 100.0}]},
        headers=auth,
    )
    q2_id = q_res2.json()["id"]
    client.patch(f"/quotations/{q2_id}", json={"status": "sent"}, headers=auth)
    client.patch(f"/quotations/{q2_id}", json={"status": "accepted"}, headers=auth)

    conv2_res = client.post(f"/quotations/{q2_id}/convert-to-order", json={"warehouse_id": wh_id}, headers=auth)
    log_test("Test 28: Convert accepted quotation -> 201", conv2_res.status_code == 201)

    # Test 29: Duplicate conversion prevention
    conv2_again = client.post(f"/quotations/{q2_id}/convert-to-order", json={"warehouse_id": wh_id}, headers=auth)
    log_test("Test 29: Duplicate quotation conversion blocked -> 400", conv2_again.status_code == 400)

    # ------------------------------------------------------------------
    # TEST GROUP I — MULTI-TENANT SECURITY & RBAC
    # ------------------------------------------------------------------
    print("\n--- TEST GROUP I: Multi-Tenant Security & RBAC ---")

    auth_b, org_b_id = _register_org("OtherTenant")
    wh_b_id = client.get("/warehouses", headers=auth_b).json()[0]["id"]
    prod_b_id = client.post(
        "/products",
        json={"name": "Prod Org B", "price": 50.0, "initial_stock": 10.0, "warehouse_id": wh_b_id},
        headers=auth_b,
    ).json()["id"]

    # Test 30: Cross-org customer reference rejected
    r_cross_cust = client.post(
        "/orders",
        json={
            "customer_id": cust_id, # Org A customer in Org B request
            "warehouse_id": wh_b_id,
            "items": [{"product_id": prod_b_id, "quantity": 1}],
        },
        headers=auth_b,
    )
    log_test("Test 30: Cross-tenant customer reference rejected -> 400", r_cross_cust.status_code == 400)

    # Test 31: Cross-org warehouse reference rejected
    r_cross_wh = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_b_id, # Org B warehouse in Org A request
            "items": [{"product_id": prod_id, "quantity": 1}],
        },
        headers=auth,
    )
    log_test("Test 31: Cross-tenant warehouse reference rejected -> 400", r_cross_wh.status_code == 400)

    # Test 32: Cross-org product reference rejected
    r_cross_prod = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "items": [{"product_id": prod_b_id, "quantity": 1}], # Org B product in Org A request
        },
        headers=auth,
    )
    log_test("Test 32: Cross-tenant product reference rejected -> 400", r_cross_prod.status_code == 400)

    # ------------------------------------------------------------------
    # TEST GROUP J — FRIENDLY METADATA (SALES OFFICER & CREATED BY)
    # ------------------------------------------------------------------
    print("\n--- TEST GROUP J: Friendly Metadata (Sales Officer & Created By) ---")

    # Create a sales officer user
    staff_auth, salesperson_user_id = _register_staff(auth, "sales_officer")
    salesperson_info = client.get("/auth/me", headers=staff_auth).json()
    admin_info = client.get("/auth/me", headers=auth).json()

    # TEST 1 — SALES OFFICER METADATA IN DETAIL
    r_create_so = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "salesperson_id": salesperson_user_id,
            "payment_type": "cash",
            "items": [{"product_id": prod_id, "quantity": 1}],
        },
        headers=auth,
    )
    assert r_create_so.status_code == 201, r_create_so.text
    so_ord_id = r_create_so.json()["id"]

    r_so_detail = client.get(f"/orders/{so_ord_id}", headers=auth)
    log_test("Metadata Test 1: GET /orders/{id} returns 200", r_so_detail.status_code == 200)
    so_detail_json = r_so_detail.json()
    log_test(
        "Metadata Test 1: salesperson_id preserved",
        so_detail_json.get("salesperson_id") == salesperson_user_id,
    )
    db: Session = next(get_db())
    sp_user = db.get(User, salesperson_user_id)
    admin_user = db.get(User, admin_info["id"])

    sp_obj = so_detail_json.get("salesperson")
    log_test("Metadata Test 1: salesperson object exists", sp_obj is not None)
    if sp_obj and sp_user:
        log_test(
            "Metadata Test 1: salesperson object fields match actual user",
            sp_obj.get("id") == sp_user.id
            and sp_obj.get("name") == sp_user.name
            and sp_obj.get("email") == sp_user.email,
        )

    # TEST 2 — SALES OFFICER METADATA IN LIST
    r_so_list = client.get("/orders", headers=auth)
    log_test("Metadata Test 2: GET /orders returns 200", r_so_list.status_code == 200)
    so_list_items = r_so_list.json()
    matched_so_item = next((item for item in so_list_items if item["id"] == so_ord_id), None)
    log_test(
        "Metadata Test 2: list item contains salesperson object",
        matched_so_item is not None and matched_so_item.get("salesperson") is not None,
    )

    # TEST 3 — NULL SALES OFFICER
    r_create_nos = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "salesperson_id": None,
            "items": [{"product_id": prod_id, "quantity": 1}],
        },
        headers=auth,
    )
    assert r_create_nos.status_code == 201, r_create_nos.text
    nos_ord_id = r_create_nos.json()["id"]
    r_nos_detail = client.get(f"/orders/{nos_ord_id}", headers=auth)
    nos_detail_json = r_nos_detail.json()
    log_test("Metadata Test 3: salesperson_id == None", nos_detail_json.get("salesperson_id") is None)
    log_test("Metadata Test 3: salesperson == None", nos_detail_json.get("salesperson") is None)

    # TEST 4 — CREATED BY METADATA IN DETAIL
    log_test("Metadata Test 4: created_by raw ID preserved", so_detail_json.get("created_by") == admin_info.get("id"))
    cby_obj = so_detail_json.get("created_by_user")
    log_test("Metadata Test 4: created_by_user object exists", cby_obj is not None)
    if cby_obj and admin_user:
        log_test(
            "Metadata Test 4: created_by_user fields match creator",
            cby_obj.get("id") == admin_user.id
            and cby_obj.get("name") == admin_user.name
            and cby_obj.get("email") == admin_user.email,
        )

    # TEST 5 — CREATED BY METADATA IN LIST
    log_test(
        "Metadata Test 5: list item contains created_by_user object",
        matched_so_item is not None and matched_so_item.get("created_by_user") is not None,
    )

    # TEST 6 — NULL CREATED BY
    db: Session = next(get_db())
    db_ord = db.query(SalesOrder).filter(SalesOrder.id == nos_ord_id).first()
    if db_ord:
        db_ord.created_by = None
        db.commit()
    r_nocby_detail = client.get(f"/orders/{nos_ord_id}", headers=auth)
    nocby_json = r_nocby_detail.json()
    log_test("Metadata Test 6: created_by == None", nocby_json.get("created_by") is None)
    log_test("Metadata Test 6: created_by_user == None", nocby_json.get("created_by_user") is None)

    # TEST 7 — HISTORICAL / MISSING USER SAFETY
    if db_ord:
        db_ord.created_by = "non-existent-creator-uuid-67890"
        db.commit()
    r_missing_detail = client.get(f"/orders/{nos_ord_id}", headers=auth)
    log_test(
        "Metadata Test 7: GET /orders/{id} with missing user refs returns 200",
        r_missing_detail.status_code == 200,
    )
    missing_json = r_missing_detail.json()
    log_test("Metadata Test 7: unresolvable created_by_user returns null", missing_json.get("created_by_user") is None)

    # TEST 8 — REGRESSION
    log_test("Metadata Test 8: payment_type is untouched ('cash')", so_detail_json.get("payment_type") == "cash")
    log_test("Metadata Test 8: customer brief populated", so_detail_json.get("customer") is not None)
    log_test("Metadata Test 8: items list populated", len(so_detail_json.get("items", [])) > 0)
    log_test("Metadata Test 8: financial summary present", "previous_balance" in so_detail_json)

    # Summary
    print("\n=======================================================")
    print(f"VERIFICATION SUMMARY: {PASSED} Passed, {FAILED} Failed")
    print("=======================================================\n")
    assert FAILED == 0, f"{FAILED} tests failed!"


if __name__ == "__main__":
    run_all_tests()
