"""Tests for the delivery workflow consistency fixes:

FIX 1/3 — public Delivery status mapping (loaded -> in_transit, rejected is now
          its own public value) is covered by tests/test_public_status_mapping.py.
FIX 5   — centralized Delivery transition validation (valid + invalid jumps).
FIX 6   — the legacy PATCH /deliveries/{id}/status route requires permission.
FIX 7   — Sales Officer delivery permissions (documented as unchanged; verified).
FIX 8   — receiver_name persists through confirm -> re-fetch.

Also includes the explicitly requested stock-concurrency regression: two
near-simultaneous orders against a single unit of stock must not both succeed.
"""

import os
import sys
import threading
import uuid

os.environ["DATABASE_URL"] = "sqlite:///./crm_saas.db"
os.environ["TESTING"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app.main import app

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


def _register_org(label: str) -> dict:
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
    return {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}


def _create_staff(admin_auth: dict, name: str, role_name: str) -> tuple[str, dict]:
    email = f"{role_name.lower().replace(' ', '_')}_{uuid.uuid4().hex[:8]}@example.com"
    res = client.post(
        "/users",
        json={"name": name, "email": email, "password": "Password123!", "role": role_name},
        headers=admin_auth,
    )
    assert res.status_code == 201, res.text
    user_id = res.json()["id"]
    login = client.post("/auth/login", json={"email": email, "password": "Password123!"})
    assert login.status_code == 200, login.text
    return user_id, {"Authorization": f"Bearer {login.json()['tokens']['access_token']}"}


def _setup_full_flow(label: str, stock: int = 100):
    """Org + warehouse + product + customer + Sales Officer + Delivery Partner + Vehicle."""
    admin_auth = _register_org(label)
    so_id, so_auth = _create_staff(admin_auth, "Sales Officer", "Sales Officer")
    dp_id, dp_auth = _create_staff(admin_auth, "Delivery Partner", "Delivery Partner")

    wh = client.post("/warehouses", json={"name": "Main WH", "is_default": True}, headers=admin_auth)
    assert wh.status_code == 201, wh.text
    wh_id = wh.json()["id"]

    prod = client.post(
        "/products", json={"name": f"Widget {uuid.uuid4().hex[:4]}", "sku": f"SKU-{uuid.uuid4().hex[:6]}", "price": 100.0},
        headers=admin_auth,
    )
    assert prod.status_code == 201, prod.text
    prod_id = prod.json()["id"]

    adj = client.post(
        f"/warehouses/{wh_id}/stock/adjust",
        json={"product_id": prod_id, "quantity": stock},
        headers=admin_auth,
    )
    assert adj.status_code == 200, adj.text

    cust = client.post("/customers", json={"name": "Test Customer"}, headers=admin_auth)
    assert cust.status_code == 201, cust.text
    cust_id = cust.json()["id"]

    veh = client.post(
        "/vehicles", json={"vehicle_number": f"DL-{uuid.uuid4().hex[:5].upper()}", "vehicle_type": "van"},
        headers=admin_auth,
    )
    assert veh.status_code == 201, veh.text
    veh_id = veh.json()["id"]

    return {
        "admin_auth": admin_auth, "so_id": so_id, "so_auth": so_auth,
        "dp_id": dp_id, "dp_auth": dp_auth, "wh_id": wh_id, "prod_id": prod_id,
        "cust_id": cust_id, "veh_id": veh_id,
    }


def _place_and_plan_delivery(ctx: dict, qty: int = 10):
    order = client.post(
        "/orders",
        json={"customer_id": ctx["cust_id"], "warehouse_id": ctx["wh_id"],
              "items": [{"product_id": ctx["prod_id"], "quantity": qty, "unit_price": 100.0}]},
        headers=ctx["admin_auth"],
    ).json()
    # Every order now starts as an unreserved Draft (finalized business rule)
    # -- confirm it before planning a Delivery for it.
    order = client.post(f"/orders/{order['id']}/confirm", headers=ctx["admin_auth"]).json()
    dlv = client.post(
        "/deliveries",
        json={"order_id": order["id"], "delivery_partner_id": ctx["dp_id"], "vehicle_id": ctx["veh_id"]},
        headers=ctx["admin_auth"],
    ).json()
    return order, dlv


# ==========================================================================
# FIX 5: full canonical lifecycle, valid transitions
# ==========================================================================


def run_lifecycle_tests():
    print("\n=======================================================")
    print("FIX 5: Delivery workflow — valid transitions end to end")
    print("=======================================================\n")

    ctx = _setup_full_flow(f"DelWF_{uuid.uuid4().hex[:6]}")

    print("--- Test 1: Create/Plan delivery -> initial status ---")
    order, dlv = _place_and_plan_delivery(ctx)
    log_test("Delivery created (201 implied by fixture)", dlv.get("id") is not None, dlv)
    log_test("Initial public status is 'pending'", dlv["status"] == "pending", dlv["status"])

    print("\n--- Test 2: Accept: Pending -> Accepted ---")
    accept_res = client.post(f"/deliveries/{dlv['id']}/accept", headers=ctx["dp_auth"])
    log_test("Accept succeeds (200)", accept_res.status_code == 200, accept_res.text)
    log_test("Status is now 'accepted'", accept_res.json()["status"] == "accepted")

    print("\n--- Test 4: Picking (operational, does not move stock) ---")
    stock_before = client.get(
        "/warehouses/stock", headers=ctx["admin_auth"], params={"product_id": ctx["prod_id"]}
    ).json()[0]["on_hand"]
    pick_res = client.post(
        f"/deliveries/{dlv['id']}/pick",
        json={"items": [{"delivery_item_id": dlv["items"][0]["id"], "picked_quantity": 10}]},
        headers=ctx["admin_auth"],
    )
    log_test("Pick succeeds (200)", pick_res.status_code == 200, pick_res.text)
    log_test("Delivery lifecycle status remains 'accepted' during picking", pick_res.json()["status"] == "accepted")
    log_test("Picking status reflects progress ('picked')", pick_res.json()["picking_status"] == "picked")
    stock_after_pick = client.get(
        "/warehouses/stock", headers=ctx["admin_auth"], params={"product_id": ctx["prod_id"]}
    ).json()[0]["on_hand"]
    log_test("Warehouse stock NOT deducted by picking", stock_after_pick == stock_before)

    ready_res = client.post(f"/deliveries/{dlv['id']}/ready", headers=ctx["admin_auth"])
    log_test("Mark ready succeeds (200)", ready_res.status_code == 200, ready_res.text)
    log_test("Status is still public 'accepted' while ready (pre-load prep)", ready_res.json()["status"] == "accepted")

    print("\n--- Test 5: Loading (atomic stock movement) ---")
    load_res = client.post(f"/deliveries/{dlv['id']}/load", headers=ctx["admin_auth"])
    log_test("Load succeeds (200)", load_res.status_code == 200, load_res.text)
    stock_after_load = client.get(
        "/warehouses/stock", headers=ctx["admin_auth"], params={"product_id": ctx["prod_id"]}
    ).json()[0]["on_hand"]
    log_test("Warehouse stock decreased by loaded quantity", stock_after_load == stock_before - 10)
    log_test(
        "Public status accurately reflects transport progress ('in_transit', not 'accepted')",
        load_res.json()["status"] == "in_transit",
        load_res.json()["status"],
    )

    print("\n--- Test 6: Dispatch -> In Transit ---")
    dispatch_res = client.patch(
        f"/deliveries/by-id/{dlv['id']}", json={"status": "in_transit"}, headers=ctx["admin_auth"]
    )
    log_test("Dispatch succeeds (200)", dispatch_res.status_code == 200, dispatch_res.text)
    log_test("Status is 'in_transit'", dispatch_res.json()["status"] == "in_transit")

    print("\n--- Test 7: Partial delivery ---")
    partial_res = client.post(
        f"/deliveries/{dlv['id']}/confirm",
        json={"items": [{"delivery_item_id": dlv["items"][0]["id"], "delivered_quantity": 6}],
              "receiver_name": "Rakesh (Warehouse Guard)"},
        headers=ctx["dp_auth"],
    )
    log_test("Partial confirm succeeds (200)", partial_res.status_code == 200, partial_res.text)
    log_test(
        "Status is consistently 'partially_delivered'",
        partial_res.json()["status"] == "partially_delivered",
        partial_res.json()["status"],
    )
    log_test("receiver_name persisted in response", partial_res.json()["receiver_name"] == "Rakesh (Warehouse Guard)")

    print("\n--- Test 8: Complete delivery -> Delivered (terminal) ---")
    final_res = client.post(
        f"/deliveries/{dlv['id']}/confirm",
        json={"items": [{"delivery_item_id": dlv["items"][0]["id"], "delivered_quantity": 4}]},
        headers=ctx["dp_auth"],
    )
    log_test("Final confirm succeeds (200)", final_res.status_code == 200, final_res.text)
    log_test("Status is 'delivered'", final_res.json()["status"] == "delivered")

    # Re-fetch to confirm persistence (not just the POST response).
    refetch = client.get(f"/deliveries/by-id/{dlv['id']}", headers=ctx["admin_auth"])
    log_test("Re-fetch shows 'delivered'", refetch.json()["status"] == "delivered")
    log_test("Re-fetched receiver_name still 'Rakesh (Warehouse Guard)'", refetch.json()["receiver_name"] == "Rakesh (Warehouse Guard)")

    print("\n--- Test 10a: Delivered is terminal — further confirm rejected ---")
    again = client.post(
        f"/deliveries/{dlv['id']}/confirm",
        json={"items": [{"delivery_item_id": dlv["items"][0]["id"], "delivered_quantity": 1}]},
        headers=ctx["dp_auth"],
    )
    log_test("Confirming an already-delivered delivery -> 400", again.status_code == 400, again.text)

    # ---------------- Reject / Cancel / Return paths ----------------
    print("\n--- Test 3: Reject (terminal-ish, reassignable) ---")
    order2, dlv2 = _place_and_plan_delivery(ctx, qty=5)
    reject_res = client.post(f"/deliveries/{dlv2['id']}/reject", json={"reason": "Vehicle broke down"}, headers=ctx["dp_auth"])
    log_test("Reject succeeds (200)", reject_res.status_code == 200, reject_res.text)
    log_test("Public status is 'rejected' (not falsely 'pending')", reject_res.json()["status"] == "rejected", reject_res.json()["status"])
    refetch2 = client.get(f"/deliveries/by-id/{dlv2['id']}", headers=ctx["admin_auth"])
    log_test("Re-fetch confirms 'rejected' persists (list/detail consistency)", refetch2.json()["status"] == "rejected")

    print("\n--- Test 9: Return (failed attempt) ---")
    order3, dlv3 = _place_and_plan_delivery(ctx, qty=5)
    client.post(f"/deliveries/{dlv3['id']}/accept", headers=ctx["dp_auth"])
    client.post(f"/deliveries/{dlv3['id']}/pick", json={"items": [{"delivery_item_id": dlv3["items"][0]["id"], "picked_quantity": 5}]}, headers=ctx["admin_auth"])
    client.post(f"/deliveries/{dlv3['id']}/ready", headers=ctx["admin_auth"])
    client.post(f"/deliveries/{dlv3['id']}/load", headers=ctx["admin_auth"])
    client.patch(f"/deliveries/by-id/{dlv3['id']}", json={"status": "in_transit"}, headers=ctx["admin_auth"])
    fail_res = client.post(
        f"/deliveries/{dlv3['id']}/confirm",
        json={"failed": True, "failure_reason": "Customer refused delivery"},
        headers=ctx["dp_auth"],
    )
    log_test("Failed confirm succeeds (200)", fail_res.status_code == 200, fail_res.text)
    log_test("Public status is 'returned' (nothing handed over)", fail_res.json()["status"] == "returned", fail_res.json()["status"])

    print("\n--- Terminal Pending/Accepted -> Cancelled ---")
    order4, dlv4 = _place_and_plan_delivery(ctx, qty=3)
    cancel_res = client.patch(f"/deliveries/by-id/{dlv4['id']}", json={"status": "cancelled"}, headers=ctx["admin_auth"])
    log_test("Cancel a pending (planned) delivery succeeds (200)", cancel_res.status_code == 200, cancel_res.text)
    log_test("Public status is 'cancelled'", cancel_res.json()["status"] == "cancelled")

    return ctx


# ==========================================================================
# FIX 5: invalid transitions
# ==========================================================================


def run_invalid_transition_tests(ctx: dict):
    print("\n=======================================================")
    print("FIX 5: Invalid transitions rejected")
    print("=======================================================\n")

    print("--- Pending -> Delivered (skip everything) ---")
    order, dlv = _place_and_plan_delivery(ctx, qty=2)
    bad1 = client.post(
        f"/deliveries/{dlv['id']}/confirm",
        json={"items": [{"delivery_item_id": dlv["items"][0]["id"], "delivered_quantity": 2}]},
        headers=ctx["admin_auth"],
    )
    log_test("Confirming a pending/planned delivery -> 400", bad1.status_code == 400, bad1.text)

    print("\n--- Delivered -> Accepted (go backwards) ---")
    order2, dlv2 = _place_and_plan_delivery(ctx, qty=2)
    client.post(f"/deliveries/{dlv2['id']}/accept", headers=ctx["dp_auth"])
    client.post(f"/deliveries/{dlv2['id']}/pick", json={"items": [{"delivery_item_id": dlv2["items"][0]["id"], "picked_quantity": 2}]}, headers=ctx["admin_auth"])
    client.post(f"/deliveries/{dlv2['id']}/ready", headers=ctx["admin_auth"])
    client.post(f"/deliveries/{dlv2['id']}/load", headers=ctx["admin_auth"])
    client.patch(f"/deliveries/by-id/{dlv2['id']}", json={"status": "in_transit"}, headers=ctx["admin_auth"])
    client.post(
        f"/deliveries/{dlv2['id']}/confirm",
        json={"items": [{"delivery_item_id": dlv2["items"][0]["id"], "delivered_quantity": 2}]},
        headers=ctx["dp_auth"],
    )
    bad2 = client.post(f"/deliveries/{dlv2['id']}/accept", headers=ctx["dp_auth"])
    log_test("Accepting an already-delivered delivery -> 400", bad2.status_code == 400, bad2.text)

    print("\n--- Cancelled -> In Transit ---")
    order3, dlv3 = _place_and_plan_delivery(ctx, qty=2)
    client.patch(f"/deliveries/by-id/{dlv3['id']}", json={"status": "cancelled"}, headers=ctx["admin_auth"])
    bad3 = client.patch(f"/deliveries/by-id/{dlv3['id']}", json={"status": "in_transit"}, headers=ctx["admin_auth"])
    log_test("Dispatching a cancelled delivery -> 400", bad3.status_code == 400, bad3.text)

    print("\n--- Loaded -> Planned (physical stock already moved; must not silently claim otherwise) ---")
    order4, dlv4 = _place_and_plan_delivery(ctx, qty=2)
    client.post(f"/deliveries/{dlv4['id']}/accept", headers=ctx["dp_auth"])
    client.post(f"/deliveries/{dlv4['id']}/pick", json={"items": [{"delivery_item_id": dlv4["items"][0]["id"], "picked_quantity": 2}]}, headers=ctx["admin_auth"])
    client.post(f"/deliveries/{dlv4['id']}/ready", headers=ctx["admin_auth"])
    client.post(f"/deliveries/{dlv4['id']}/load", headers=ctx["admin_auth"])
    bad4 = client.patch(f"/deliveries/by-id/{dlv4['id']}", json={"status": "planned"}, headers=ctx["admin_auth"])
    log_test("Reverting a loaded delivery to 'planned' -> 400", bad4.status_code == 400, bad4.text)


# ==========================================================================
# FIX 6: legacy PATCH /deliveries/{id}/status is now permission-gated
# ==========================================================================


def run_legacy_endpoint_tests(ctx: dict):
    print("\n=======================================================")
    print("FIX 6: Legacy PATCH /deliveries/{id}/status secured")
    print("=======================================================\n")

    order, dlv = _place_and_plan_delivery(ctx, qty=2)

    # Sales Officer has no deliveries:edit permission by default.
    forbidden = client.patch(f"/deliveries/{order['id']}/status", json={"status": "Delivered"}, headers=ctx["so_auth"])
    log_test(
        "Sales Officer (no deliveries:edit) is forbidden (403), not allowed to bypass workflow",
        forbidden.status_code == 403,
        forbidden.text,
    )

    # Delivery Partner has deliveries:edit (full) by default — still works (backward compatible).
    dp_res = client.patch(f"/deliveries/{order['id']}/status", json={"status": "Delivered"}, headers=ctx["dp_auth"])
    log_test(
        "Delivery Partner (has deliveries:edit) still succeeds — backward compatible",
        dp_res.status_code == 200,
        dp_res.text,
    )
    log_test("Resulting order_status is public 'completed'", dp_res.json()["order_status"] == "completed")

    # Unauthenticated / no token at all.
    anon = client.patch(f"/deliveries/{order['id']}/status", json={"status": "Delivered"})
    log_test("Unauthenticated request rejected", anon.status_code in (401, 403), anon.text)

    # Cross-organization.
    other_auth = _register_org(f"LegacyOther_{uuid.uuid4().hex[:6]}")
    cross = client.patch(f"/deliveries/{order['id']}/status", json={"status": "Delivered"}, headers=other_auth)
    log_test("Cross-organization request rejected (404)", cross.status_code == 404, cross.text)


# ==========================================================================
# Permission matrix: Admin / Sales Officer / Delivery Partner on /deliveries
# ==========================================================================


def run_permission_matrix_tests(ctx: dict):
    print("\n=======================================================")
    print("Permission matrix: Admin / Sales Officer / Delivery Partner")
    print("=======================================================\n")

    order, dlv = _place_and_plan_delivery(ctx, qty=2)

    admin_list = client.get("/deliveries", headers=ctx["admin_auth"])
    log_test("Admin: GET /deliveries succeeds (200)", admin_list.status_code == 200)

    # FIX 7 decision: Sales Officer keeps no deliveries:* permission (documented,
    # not guessed) — verify that decision holds exactly as intended.
    so_list = client.get("/deliveries", headers=ctx["so_auth"])
    log_test(
        "Sales Officer: GET /deliveries forbidden (403) — FIX 7 decision: unchanged",
        so_list.status_code == 403,
        so_list.text,
    )
    so_create = client.post(
        "/deliveries",
        json={"order_id": order["id"], "delivery_partner_id": ctx["dp_id"], "vehicle_id": ctx["veh_id"]},
        headers=ctx["so_auth"],
    )
    log_test("Sales Officer: POST /deliveries forbidden (403)", so_create.status_code == 403, so_create.text)

    dp_list = client.get("/deliveries", headers=ctx["dp_auth"])
    log_test("Delivery Partner: GET /deliveries succeeds (200)", dp_list.status_code == 200, dp_list.text)
    log_test(
        "Delivery Partner sees only their own assigned deliveries",
        all(d["delivery_partner_id"] == ctx["dp_id"] for d in dp_list.json()),
    )

    # Delivery Partner cannot manipulate a delivery not assigned to them.
    other_ctx = _setup_full_flow(f"OtherDP_{uuid.uuid4().hex[:6]}")
    other_order, other_dlv = _place_and_plan_delivery(other_ctx, qty=2)
    cross_accept = client.post(f"/deliveries/{other_dlv['id']}/accept", headers=ctx["dp_auth"])
    log_test(
        "Delivery Partner cannot accept another organization's delivery (404)",
        cross_accept.status_code == 404,
        cross_accept.text,
    )


# ==========================================================================
# Stock concurrency regression: 1 unit of stock, two near-simultaneous orders
# ==========================================================================


def run_stock_concurrency_test():
    print("\n=======================================================")
    print("Stock concurrency regression: 1 unit, two simultaneous orders")
    print("=======================================================\n")

    ctx = _setup_full_flow(f"Concurrency_{uuid.uuid4().hex[:6]}", stock=1)
    stock_before = client.get(
        "/warehouses/stock", headers=ctx["admin_auth"], params={"product_id": ctx["prod_id"]}
    ).json()[0]
    log_test("Setup: exactly 1 unit available before the race", stock_before["available"] == 1, stock_before)

    # Every order now starts as an unreserved Draft (finalized business rule)
    # -- creation itself never touches stock, so it can never race. The
    # atomicity/locking guarantee this test exists to prove now lives at
    # CONFIRM time (order_service.confirm_order), so both Draft orders are
    # created up front (uncontended) and it is their concurrent /confirm
    # calls that race for the single unit of stock.
    order_a = client.post(
        "/orders", json={"customer_id": ctx["cust_id"], "warehouse_id": ctx["wh_id"],
                         "items": [{"product_id": ctx["prod_id"], "quantity": 1, "unit_price": 100.0}]},
        headers=ctx["admin_auth"],
    ).json()
    order_b = client.post(
        "/orders", json={"customer_id": ctx["cust_id"], "warehouse_id": ctx["wh_id"],
                         "items": [{"product_id": ctx["prod_id"], "quantity": 1, "unit_price": 100.0}]},
        headers=ctx["admin_auth"],
    ).json()

    results = {}

    def _confirm(label: str, order_id: str):
        try:
            r = client.post(f"/orders/{order_id}/confirm", headers=ctx["admin_auth"])
            results[label] = r.status_code
        except Exception as exc:  # TestClient re-raises unhandled server exceptions by
            # default (a debugging aid) rather than returning them as a 500 response the
            # way a real HTTP client would see — treat that the same as a failed request.
            results[label] = f"EXC:{type(exc).__name__}"

    t1 = threading.Thread(target=_confirm, args=("A", order_a["id"]))
    t2 = threading.Thread(target=_confirm, args=("B", order_b["id"]))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    successes = [label for label, code in results.items() if code == 200]
    log_test("At most one of the two concurrent confirms reserved the unit (no double-reservation)", len(successes) <= 1, results)
    log_test("Exactly one of the two concurrent confirms succeeded", len(successes) == 1, results)

    stock_after = client.get(
        "/warehouses/stock", headers=ctx["admin_auth"], params={"product_id": ctx["prod_id"]}
    ).json()[0]
    log_test(
        "No overselling: reserved does not exceed the 1 unit that was available",
        stock_after["reserved"] <= 1,
        stock_after,
    )
    log_test("No overselling: available stock is 0 after the race", stock_after["available"] == 0, stock_after)


def run_delivery_partner_assignment_notification_tests(ctx):
    print("\n--- Running Delivery Partner Assignment Notification Tests ---")
    admin_auth = ctx["admin_auth"]

    # Create two dedicated delivery partners for notification tests
    partner_a_id, partner_a_auth = _create_staff(admin_auth, "Partner A", "Delivery Partner")
    partner_b_id, partner_b_auth = _create_staff(admin_auth, "Partner B", "Delivery Partner")

    # Initial state: 0 notifications for both
    r_a = client.get("/notifications", headers=partner_a_auth)
    assert r_a.status_code == 200, r_a.text
    log_test("Partner A initial notification count is 0", len(r_a.json()) == 0)

    # 1. Create a confirmed order and plan delivery via POST /deliveries assigned to Partner A
    order_res = client.post(
        "/orders",
        json={"customer_id": ctx["cust_id"], "warehouse_id": ctx["wh_id"],
              "items": [{"product_id": ctx["prod_id"], "quantity": 2, "unit_price": 100.0}]},
        headers=admin_auth,
    )
    assert order_res.status_code == 201, order_res.text
    order_id = order_res.json()["id"]
    client.post(f"/orders/{order_id}/confirm", headers=admin_auth)

    plan_res = client.post(
        "/deliveries",
        json={"order_id": order_id, "delivery_partner_id": partner_a_id},
        headers=admin_auth,
    )
    assert plan_res.status_code == 201, plan_res.text
    delivery = plan_res.json()
    delivery_id = delivery["id"]
    delivery_num = delivery["delivery_number"]

    # 1a. Verify Partner A received exactly 1 notification
    notifs_a = client.get("/notifications", headers=partner_a_auth).json()
    unread_a = client.get("/notifications/unread-count", headers=partner_a_auth).json()
    log_test("POST /deliveries: Partner A gets exactly 1 Notification", len(notifs_a) == 1)
    log_test("Partner A unread count is 1", unread_a.get("unread") == 1)
    if notifs_a:
        n = notifs_a[0]
        log_test("Notification title is 'New delivery assigned'", n["title"] == "New delivery assigned")
        log_test("Notification body matches initial assignment format", n["body"] == f"Delivery {delivery_num} has been assigned to you")
        log_test("Notification type is 'delivery'", n["type"] == "delivery")
        log_test("Notification link is delivery.id", n["link"] == delivery_id)
        from app.core.database import SessionLocal
        from app.models import Notification
        with SessionLocal() as db_sess:
            notif_row = db_sess.get(Notification, n["id"])
            log_test("Notification user_id in DB is Partner A", notif_row.user_id == partner_a_id)
            log_test("Notification organization_id in DB is correct", notif_row.organization_id is not None)

    # 2. Verify Partner B received 0 notifications
    notifs_b = client.get("/notifications", headers=partner_b_auth).json()
    log_test("Partner B receives 0 notifications on Partner A initial assignment", len(notifs_b) == 0)

    # 3. Normal Reassignment via PATCH /deliveries/by-id/{id}: A -> B
    reassign_res = client.patch(
        f"/deliveries/by-id/{delivery_id}",
        json={"delivery_partner_id": partner_b_id},
        headers=admin_auth,
    )
    assert reassign_res.status_code == 200, reassign_res.text

    # 3a. Partner B gets exactly 1 new notification
    notifs_b = client.get("/notifications", headers=partner_b_auth).json()
    unread_b = client.get("/notifications/unread-count", headers=partner_b_auth).json()
    log_test("PATCH /deliveries: Partner B gets exactly 1 new Notification on reassignment", len(notifs_b) == 1)
    log_test("Partner B unread count is 1", unread_b.get("unread") == 1)
    if notifs_b:
        n_b = notifs_b[0]
        log_test("Reassignment notification title is 'New delivery assigned'", n_b["title"] == "New delivery assigned")
        log_test("Reassignment body format is correct", n_b["body"] == f"Delivery {delivery_num} has been reassigned to you")
        log_test("Reassignment link is delivery.id", n_b["link"] == delivery_id)
        with SessionLocal() as db_sess:
            notif_row_b = db_sess.get(Notification, n_b["id"])
            log_test("Reassignment user_id in DB is Partner B", notif_row_b.user_id == partner_b_id)

    # 4. Old Partner A receives NO new notification
    notifs_a_after = client.get("/notifications", headers=partner_a_auth).json()
    log_test("Old Partner A gets no new notification on reassignment to B", len(notifs_a_after) == 1)

    # 5. PATCH /deliveries no-op: B -> B
    noop_res = client.patch(
        f"/deliveries/by-id/{delivery_id}",
        json={"delivery_partner_id": partner_b_id, "notes": "some new notes"},
        headers=admin_auth,
    )
    assert noop_res.status_code == 200, noop_res.text
    notifs_b_noop = client.get("/notifications", headers=partner_b_auth).json()
    log_test("PATCH /deliveries no-op (B -> B) produces no duplicate notification", len(notifs_b_noop) == 1)

    # 8. Rejected -> Planned reassignment test (Single notification guarantee)
    # Partner B rejects the delivery
    reject_res = client.post(f"/deliveries/{delivery_id}/reject", json={"reason": "Cannot deliver today"}, headers=partner_b_auth)
    assert reject_res.status_code == 200, reject_res.text

    # Dispatcher reassigns to Partner A from rejected state
    replan_res = client.patch(
        f"/deliveries/by-id/{delivery_id}",
        json={"status": "planned", "delivery_partner_id": partner_a_id},
        headers=admin_auth,
    )
    assert replan_res.status_code == 200, replan_res.text

    # Verify Partner A received exactly 1 new notification (total 2 now, exactly 1 from this action)
    notifs_a_replan = client.get("/notifications", headers=partner_a_auth).json()
    log_test("Rejected -> planned reassignment creates exactly ONE new notification for Partner A (total 2)", len(notifs_a_replan) == 2)
    newest_notif_a = notifs_a_replan[0] if notifs_a_replan[0]["id"] != notifs_a[0]["id"] else notifs_a_replan[1]
    log_test("Rejected -> planned notification body is 'reassigned'", "has been reassigned to you" in newest_notif_a["body"])

    # 6 & 7. Order-side assign-delivery-partner tests
    order2_res = client.post(
        "/orders",
        json={"customer_id": ctx["cust_id"], "warehouse_id": ctx["wh_id"],
              "items": [{"product_id": ctx["prod_id"], "quantity": 1, "unit_price": 100.0}]},
        headers=admin_auth,
    )
    assert order2_res.status_code == 201, order2_res.text
    order2_id = order2_res.json()["id"]
    client.post(f"/orders/{order2_id}/confirm", headers=admin_auth)

    # Order-side initial assignment (creates new delivery and notifies partner A)
    count_a_before = len(client.get("/notifications", headers=partner_a_auth).json())
    order_assign_res = client.patch(
        f"/orders/{order2_id}/assign-delivery-partner",
        json={"delivery_partner_id": partner_a_id},
        headers=admin_auth,
    )
    assert order_assign_res.status_code == 200, order_assign_res.text
    notifs_a_order = client.get("/notifications", headers=partner_a_auth).json()
    log_test("Order-side assign-delivery-partner: Partner A gets exactly 1 new notification", len(notifs_a_order) == count_a_before + 1)

    # Order-side reassignment: A -> B
    count_b_before = len(client.get("/notifications", headers=partner_b_auth).json())
    order_reassign_res = client.patch(
        f"/orders/{order2_id}/assign-delivery-partner",
        json={"delivery_partner_id": partner_b_id},
        headers=admin_auth,
    )
    assert order_reassign_res.status_code == 200, order_reassign_res.text
    notifs_b_order = client.get("/notifications", headers=partner_b_auth).json()
    notifs_a_after_order_reassign = client.get("/notifications", headers=partner_a_auth).json()
    log_test("Order-side reassignment: Partner B gets exactly 1 new notification", len(notifs_b_order) == count_b_before + 1)
    log_test("Order-side reassignment: Old Partner A gets no new notification", len(notifs_a_after_order_reassign) == len(notifs_a_order))

    # Order-side no-op: B -> B
    client.patch(
        f"/orders/{order2_id}/assign-delivery-partner",
        json={"delivery_partner_id": partner_b_id},
        headers=admin_auth,
    )
    notifs_b_noop_order = client.get("/notifications", headers=partner_b_auth).json()
    log_test("Order-side no-op (B -> B) produces no new notification", len(notifs_b_noop_order) == len(notifs_b_order))

    # 11. Cross-organization assignment safety
    org2_auth = _register_org("CrossOrg")
    partner_c_id, partner_c_auth = _create_staff(org2_auth, "Partner C", "Delivery Partner")

    cross_assign_res = client.patch(
        f"/deliveries/by-id/{delivery_id}",
        json={"delivery_partner_id": partner_c_id},
        headers=admin_auth,
    )
    log_test("Cross-organization delivery assignment is rejected (HTTP 400)", cross_assign_res.status_code == 400)
    notifs_c = client.get("/notifications", headers=partner_c_auth).json()
    log_test("Cross-org Partner C receives 0 notifications", len(notifs_c) == 0)


def run_vehicle_loading_production_fix_tests():
    print("\n--- Running Vehicle Loading Production Fix Tests ---")
    from app.core.database import SessionLocal
    from app.models import Delivery, DeliveryItem, SalesOrder, Product, Warehouse, Vehicle, VehicleLoading, VehicleLoadingItem
    from app.services import stock_service
    from sqlalchemy.orm import lazyload

    admin_auth = _register_org("VehLoad")
    dp_id, dp_auth = _create_staff(admin_auth, "Driver Dan", "Delivery Partner")

    # 1. Setup Warehouse, Product with UOM & Weight, Vehicle with Capacity
    wh_res = client.post("/warehouses", json={"name": "Central Depot", "is_default": True}, headers=admin_auth)
    assert wh_res.status_code == 201, wh_res.text
    wh_id = wh_res.json()["id"]

    veh_res = client.post(
        "/vehicles",
        json={"vehicle_number": f"TRK-{uuid.uuid4().hex[:4].upper()}", "capacity_kg": 100.0, "default_driver_id": dp_id},
        headers=admin_auth,
    )
    assert veh_res.status_code == 201, veh_res.text
    veh_id = veh_res.json()["id"]

    prod_res = client.post(
        "/products",
        json={
            "name": "Heavy Box",
            "sku": f"BOX-{uuid.uuid4().hex[:4]}",
            "price": 50.0,
            "uom": "box",
            "weight": 10.0,
            "weight_unit": "kg",
        },
        headers=admin_auth,
    )
    assert prod_res.status_code == 201, prod_res.text
    prod_id = prod_res.json()["id"]

    # Adjust stock to 100
    adj = client.post(f"/warehouses/{wh_id}/stock/adjust", json={"product_id": prod_id, "quantity": 100}, headers=admin_auth)
    assert adj.status_code == 200, adj.text

    cust_res = client.post("/customers", json={"name": "Load Customer"}, headers=admin_auth)
    assert cust_res.status_code == 201, cust_res.text
    cust_id = cust_res.json()["id"]

    # 2. Test Single Load Success, DeliveryOut.warehouse, and DeliveryLineOut fields
    order1 = client.post(
        "/orders",
        json={"customer_id": cust_id, "warehouse_id": wh_id, "items": [{"product_id": prod_id, "quantity": 5, "unit_price": 50.0}]},
        headers=admin_auth,
    )
    assert order1.status_code == 201, order1.text
    order1_id = order1.json()["id"]
    client.post(f"/orders/{order1_id}/confirm", headers=admin_auth)

    del1 = client.post(
        "/deliveries",
        json={"order_id": order1_id, "delivery_partner_id": dp_id, "vehicle_id": veh_id},
        headers=admin_auth,
    )
    assert del1.status_code == 201, del1.text
    del1_id = del1.json()["id"]
    del1_data = del1.json()

    # Verify DeliveryOut warehouse object & DeliveryLineOut fields
    log_test("DeliveryOut contains warehouse object with id and name", del1_data.get("warehouse") is not None and del1_data["warehouse"]["id"] == wh_id and del1_data["warehouse"]["name"] == "Central Depot")
    log_test("DeliveryLineOut contains uom", len(del1_data["items"]) > 0 and del1_data["items"][0]["uom"] == "box")
    log_test("DeliveryLineOut contains per-unit weight_kg", len(del1_data["items"]) > 0 and del1_data["items"][0]["weight_kg"] == 10.0)
    log_test("DeliveryLineOut contains warehouse_available", len(del1_data["items"]) > 0 and del1_data["items"][0]["warehouse_available"] is not None)

    # 3. Test PostgreSQL-compatible lock query (ensuring lazyload on joined product/variant)
    db = SessionLocal()
    try:
        locked_query = (
            db.query(DeliveryItem)
            .options(lazyload(DeliveryItem.product), lazyload(DeliveryItem.variant))
            .filter(DeliveryItem.delivery_id == del1_id)
            .with_for_update(nowait=False)
        )
        sql_str = str(locked_query.statement.compile(compile_kwargs={"literal_binds": True}))
        # Verify no LEFT OUTER JOIN is generated in the locked query statement
        log_test("DeliveryItem lock query suppresses outer joins for PostgreSQL safety", "LEFT OUTER JOIN" not in sql_str.upper())
        items = locked_query.all()
        log_test("DeliveryItem lock query successfully returns rows", len(items) == 1)
    finally:
        db.close()

    def _make_delivery_ready(d_data, custom_auth=None):
        d_id = d_data["id"]
        client.post(f"/deliveries/{d_id}/accept", headers=custom_auth or dp_auth)
        items = d_data.get("items", [])
        if items:
            pick_items = [{"delivery_item_id": i["id"], "picked_quantity": i["planned_quantity"]} for i in items]
            client.post(f"/deliveries/{d_id}/pick", json={"items": pick_items}, headers=admin_auth)
        res = client.post(f"/deliveries/{d_id}/ready", headers=admin_auth)
        assert res.status_code == 200, res.text

    # Move to ready and execute single load
    _make_delivery_ready(del1_data)

    load_res = client.post(f"/deliveries/{del1_id}/load", headers=admin_auth)
    log_test("Single load succeeds (HTTP 200)", load_res.status_code == 200, load_res.text)
    loaded_data = load_res.json()
    log_test("Single load response contains warehouse object", loaded_data.get("warehouse") is not None and loaded_data["warehouse"]["name"] == "Central Depot")

    # 4. Repeated Single Load Idempotency & Non-Destructive Invariants
    # Record baseline state before repeat call
    db_chk = SessionLocal()
    try:
        wh_stock_before = stock_service.on_hand(db_chk, wh_id, prod_id, None)
        veh_loadings_before = db_chk.query(VehicleLoading).filter(VehicleLoading.organization_id == del1_data["organization_id"]).count()
        veh_loading_items_before = db_chk.query(VehicleLoadingItem).count()
        del_item_before = db_chk.get(DeliveryItem, del1_data["items"][0]["id"])
        loaded_qty_before = del_item_before.loaded_quantity
    finally:
        db_chk.close()

    repeat_res = client.post(f"/deliveries/{del1_id}/load", headers=admin_auth)
    log_test("Repeated single load on fully loaded delivery returns HTTP 400", repeat_res.status_code == 400)
    log_test("Repeated single load returns readable error message", "cannot transition from loaded to loaded" in repeat_res.json().get("detail", "") or "already loaded" in repeat_res.json().get("detail", ""))

    db_chk2 = SessionLocal()
    try:
        wh_stock_after = stock_service.on_hand(db_chk2, wh_id, prod_id, None)
        veh_loadings_after = db_chk2.query(VehicleLoading).filter(VehicleLoading.organization_id == del1_data["organization_id"]).count()
        veh_loading_items_after = db_chk2.query(VehicleLoadingItem).count()
        del_item_after = db_chk2.get(DeliveryItem, del1_data["items"][0]["id"])
        loaded_qty_after = del_item_after.loaded_quantity
    finally:
        db_chk2.close()

    log_test("Repeated single load leaves warehouse stock unchanged", wh_stock_before == wh_stock_after)
    log_test("Repeated single load creates no duplicate vehicle loadings", veh_loadings_before == veh_loadings_after)
    log_test("Repeated single load creates no duplicate vehicle loading items", veh_loading_items_before == veh_loading_items_after)
    log_test("Repeated single load leaves loaded_quantity unchanged", loaded_qty_before == loaded_qty_after)

    # 5. Delivery Warehouse Hierarchy & Fallback Rules:
    # Rule 1: Delivery warehouse wins over order warehouse
    wh2_res = client.post("/warehouses", json={"name": "Secondary Depot"}, headers=admin_auth)
    wh2_id = wh2_res.json()["id"]
    client.post(f"/warehouses/{wh2_id}/stock/adjust", json={"product_id": prod_id, "quantity": 50}, headers=admin_auth)

    order_wh_test = client.post(
        "/orders",
        json={"customer_id": cust_id, "warehouse_id": wh_id, "items": [{"product_id": prod_id, "quantity": 1, "unit_price": 50.0}]},
        headers=admin_auth,
    )
    order_wh_test_id = order_wh_test.json()["id"]
    client.post(f"/orders/{order_wh_test_id}/confirm", headers=admin_auth)

    del_explicit_wh = client.post(
        "/deliveries",
        json={"order_id": order_wh_test_id, "delivery_partner_id": dp_id, "vehicle_id": veh_id, "warehouse_id": wh2_id},
        headers=admin_auth,
    ).json()
    del_explicit_data = client.get(f"/deliveries/by-id/{del_explicit_wh['id']}", headers=admin_auth).json()
    log_test("Explicit delivery.warehouse_id wins over order.warehouse_id", del_explicit_data.get("warehouse") is not None and del_explicit_data["warehouse"]["id"] == wh2_id)

    # Rule 2: Delivery warehouse NULL + order warehouse exists -> order warehouse used
    order2 = client.post(
        "/orders",
        json={"customer_id": cust_id, "warehouse_id": wh_id, "items": [{"product_id": prod_id, "quantity": 2, "unit_price": 50.0}]},
        headers=admin_auth,
    )
    order2_id = order2.json()["id"]
    client.post(f"/orders/{order2_id}/confirm", headers=admin_auth)

    del2 = client.post(
        "/deliveries",
        json={"order_id": order2_id, "delivery_partner_id": dp_id, "vehicle_id": veh_id, "warehouse_id": None},
        headers=admin_auth,
    )
    del2_id = del2.json()["id"]
    del2_data = client.get(f"/deliveries/by-id/{del2_id}", headers=admin_auth).json()
    log_test("Warehouse fallback resolves warehouse from order when delivery.warehouse_id is null", del2_data.get("warehouse") is not None and del2_data["warehouse"]["id"] == wh_id)

    # Rule 3 & 4: Delivery warehouse NULL + order warehouse NULL -> HTTP 400 (Org default MUST NOT rescue)
    db_no_wh = SessionLocal()
    try:
        # Create an order with warehouse_id = None
        ord_no_wh = SalesOrder(
            organization_id=del1_data["organization_id"],
            customer_id=cust_id,
            warehouse_id=None,
            status="confirmed",
            fulfilment_status="planned",
            order_number=f"ORD-NOWH-{uuid.uuid4().hex[:4]}",
        )
        db_no_wh.add(ord_no_wh)
        db_no_wh.flush()

        del_no_wh = Delivery(
            organization_id=del1_data["organization_id"],
            sales_order_id=ord_no_wh.id,
            delivery_partner_id=dp_id,
            vehicle_id=veh_id,
            warehouse_id=None,
            status="ready",
            delivery_note_number=f"DN-NOWH-{uuid.uuid4().hex[:4]}",
        )
        db_no_wh.add(del_no_wh)
        db_no_wh.flush()

        del_item_no_wh = DeliveryItem(
            delivery_id=del_no_wh.id,
            product_id=prod_id,
            product_name="Heavy Box",
            planned_quantity=1.0,
            loaded_quantity=0.0,
        )
        db_no_wh.add(del_item_no_wh)
        db_no_wh.commit()
        del_no_wh_id = del_no_wh.id
    finally:
        db_no_wh.close()

    # Verify serialization reports warehouse: null
    del_nowh_view = client.get(f"/deliveries/by-id/{del_no_wh_id}", headers=admin_auth).json()
    log_test("Delivery serialization does NOT fall back to org default warehouse when both are null", del_nowh_view.get("warehouse") is None)

    # Attempt to load delivery with no warehouse
    no_wh_load_res = client.post(f"/deliveries/{del_no_wh_id}/load", headers=admin_auth)
    log_test("Loading delivery with no delivery/order warehouse returns HTTP 400", no_wh_load_res.status_code == 400)
    log_test("No warehouse load error returns exact required detail", no_wh_load_res.json().get("detail") == "Unable to determine a warehouse for this delivery. Please assign a warehouse to the delivery or order.")

    # Rule 5: Verify backfill logic rules (NULL delivery warehouse + order warehouse -> eligible; NULL order warehouse -> skipped)
    db_bf = SessionLocal()
    try:
        # Candidate 1: order has warehouse
        d_cand1 = Delivery(
            organization_id=del1_data["organization_id"],
            sales_order_id=order2_id,
            warehouse_id=None,
            status="planned",
            delivery_note_number=f"DN-BF1-{uuid.uuid4().hex[:4]}",
        )
        # Candidate 2: order has NO warehouse
        d_cand2 = Delivery(
            organization_id=del1_data["organization_id"],
            sales_order_id=ord_no_wh.id,
            warehouse_id=None,
            status="planned",
            delivery_note_number=f"DN-BF2-{uuid.uuid4().hex[:4]}",
        )
        db_bf.add_all([d_cand1, d_cand2])
        db_bf.commit()

        # Simulate backfill resolution logic
        res1 = None
        ord1_lookup = db_bf.get(SalesOrder, d_cand1.sales_order_id)
        if ord1_lookup and ord1_lookup.warehouse_id:
            res1 = stock_service.owned_warehouse(db_bf, ord1_lookup.warehouse_id, d_cand1.organization_id)

        res2 = None
        ord2_lookup = db_bf.get(SalesOrder, d_cand2.sales_order_id)
        if ord2_lookup and ord2_lookup.warehouse_id:
            res2 = stock_service.owned_warehouse(db_bf, ord2_lookup.warehouse_id, d_cand2.organization_id)

        log_test("Backfill logic resolves delivery warehouse when order warehouse is present", res1 is not None and res1.id == wh_id)
        log_test("Backfill logic skips delivery warehouse when order warehouse is null (no org default fallback)", res2 is None)
    finally:
        db_bf.close()

    # 6. warehouse_available Reservation Semantics Tests
    # Case A: on_hand = 10, this order reservation = 8, other reservation = 0
    # Expected: warehouse_available == 10 (or 8+), NOT 2! And load() loads 8 successfully.
    prod_resA = client.post(
        "/products",
        json={"name": "Reservation Test Item A", "sku": f"RESA-{uuid.uuid4().hex[:4]}", "price": 100.0, "weight": 1.0, "weight_unit": "kg"},
        headers=admin_auth,
    ).json()["id"]
    client.post(f"/warehouses/{wh_id}/stock/adjust", json={"product_id": prod_resA, "quantity": 10}, headers=admin_auth)

    ord_resA = client.post(
        "/orders",
        json={"customer_id": cust_id, "warehouse_id": wh_id, "items": [{"product_id": prod_resA, "quantity": 8, "unit_price": 100.0}]},
        headers=admin_auth,
    ).json()["id"]
    client.post(f"/orders/{ord_resA}/confirm", headers=admin_auth)

    del_resA = client.post(
        "/deliveries",
        json={"order_id": ord_resA, "delivery_partner_id": dp_id, "vehicle_id": veh_id},
        headers=admin_auth,
    ).json()
    del_resA_id = del_resA["id"]

    del_resA_view = client.get(f"/deliveries/by-id/{del_resA_id}", headers=admin_auth).json()
    availA = del_resA_view["items"][0]["warehouse_available"]
    log_test("warehouse_available respects this delivery's own reservation (shows 10, not 2)", availA == 10.0)

    _make_delivery_ready(del_resA)
    load_resA = client.post(f"/deliveries/{del_resA_id}/load", headers=admin_auth)
    log_test("Delivery successfully loads its 8 reserved units", load_resA.status_code == 200)

    # Case B: on_hand = 10, other order reservation = 8, this order reservation = 0
    # Expected: warehouse_available == 2 (other order's reservation is protected).
    prod_resB = client.post(
        "/products",
        json={"name": "Reservation Test Item B", "sku": f"RESB-{uuid.uuid4().hex[:4]}", "price": 100.0, "weight": 1.0, "weight_unit": "kg"},
        headers=admin_auth,
    ).json()["id"]
    client.post(f"/warehouses/{wh_id}/stock/adjust", json={"product_id": prod_resB, "quantity": 10}, headers=admin_auth)

    # Other order reserves 8
    ord_other = client.post(
        "/orders",
        json={"customer_id": cust_id, "warehouse_id": wh_id, "items": [{"product_id": prod_resB, "quantity": 8, "unit_price": 100.0}]},
        headers=admin_auth,
    ).json()["id"]
    client.post(f"/orders/{ord_other}/confirm", headers=admin_auth)

    # Delivery for a direct/unreserved delivery wanting 8 units
    db_unres = SessionLocal()
    try:
        ord_unres = SalesOrder(
            organization_id=del1_data["organization_id"],
            customer_id=cust_id,
            warehouse_id=wh_id,
            status="confirmed",
            fulfilment_status="planned",
            order_number=f"ORD-UNRES-{uuid.uuid4().hex[:4]}",
        )
        db_unres.add(ord_unres)
        db_unres.flush()

        del_unres = Delivery(
            organization_id=del1_data["organization_id"],
            sales_order_id=ord_unres.id,
            delivery_partner_id=dp_id,
            vehicle_id=veh_id,
            warehouse_id=wh_id,
            status="ready",
            delivery_note_number=f"DN-UNRES-{uuid.uuid4().hex[:4]}",
        )
        db_unres.add(del_unres)
        db_unres.flush()

        del_unres_item = DeliveryItem(
            delivery_id=del_unres.id,
            product_id=prod_resB,
            product_name="Reservation Test Item B",
            planned_quantity=8.0,
            loaded_quantity=0.0,
        )
        db_unres.add(del_unres_item)
        db_unres.commit()
        del_unres_id = del_unres.id
    finally:
        db_unres.close()

    del_unres_view = client.get(f"/deliveries/by-id/{del_unres_id}", headers=admin_auth).json()
    availB = del_unres_view["items"][0]["warehouse_available"]
    log_test("warehouse_available excludes other orders' reservations (shows 2, protecting the 8 reserved)", availB == 2.0)

    # Case C: on_hand = 10, this order reservation = 6, other order reservation = 4
    # Expected: warehouse_available == 6
    prod_resC = client.post(
        "/products",
        json={"name": "Reservation Test Item C", "sku": f"RESC-{uuid.uuid4().hex[:4]}", "price": 100.0, "weight": 1.0, "weight_unit": "kg"},
        headers=admin_auth,
    ).json()["id"]
    client.post(f"/warehouses/{wh_id}/stock/adjust", json={"product_id": prod_resC, "quantity": 10}, headers=admin_auth)

    ord_otherC = client.post(
        "/orders",
        json={"customer_id": cust_id, "warehouse_id": wh_id, "items": [{"product_id": prod_resC, "quantity": 4, "unit_price": 100.0}]},
        headers=admin_auth,
    ).json()["id"]
    client.post(f"/orders/{ord_otherC}/confirm", headers=admin_auth)

    ord_thisC = client.post(
        "/orders",
        json={"customer_id": cust_id, "warehouse_id": wh_id, "items": [{"product_id": prod_resC, "quantity": 6, "unit_price": 100.0}]},
        headers=admin_auth,
    ).json()["id"]
    client.post(f"/orders/{ord_thisC}/confirm", headers=admin_auth)

    del_thisC = client.post(
        "/deliveries",
        json={"order_id": ord_thisC, "delivery_partner_id": dp_id, "vehicle_id": veh_id},
        headers=admin_auth,
    ).json()
    del_thisC_view = client.get(f"/deliveries/by-id/{del_thisC['id']}", headers=admin_auth).json()
    availC = del_thisC_view["items"][0]["warehouse_available"]
    log_test("warehouse_available accurately gives 6 for own reservation while protecting other 4", availC == 6.0)

    # 6b. Insufficient Stock Readable HTTP 400
    prod_low = client.post(
        "/products",
        json={"name": "Rare Item", "sku": f"RARE-{uuid.uuid4().hex[:4]}", "price": 100.0, "weight": 1.0, "weight_unit": "kg"},
        headers=admin_auth,
    ).json()["id"]
    client.post(f"/warehouses/{wh_id}/stock/adjust", json={"product_id": prod_low, "quantity": 10}, headers=admin_auth)

    order3 = client.post(
        "/orders",
        json={"customer_id": cust_id, "warehouse_id": wh_id, "items": [{"product_id": prod_low, "quantity": 10, "unit_price": 100.0}]},
        headers=admin_auth,
    )
    order3_id = order3.json()["id"]
    client.post(f"/orders/{order3_id}/confirm", headers=admin_auth)

    del3 = client.post(
        "/deliveries",
        json={"order_id": order3_id, "delivery_partner_id": dp_id, "vehicle_id": veh_id},
        headers=admin_auth,
    )
    del3_data = del3.json()
    del3_id = del3_data["id"]
    _make_delivery_ready(del3_data)

    # Directly adjust physical stock on hand in DB to 2 to simulate physical stock shortfall
    from app.models import WarehouseStock
    db_s = SessionLocal()
    try:
        ws = db_s.query(WarehouseStock).filter(WarehouseStock.warehouse_id == wh_id, WarehouseStock.product_id == prod_low).first()
        if ws:
            ws.on_hand_quantity = 2
            db_s.commit()
    finally:
        db_s.close()

    insufficient_res = client.post(f"/deliveries/{del3_id}/load", headers=admin_auth)
    log_test("Insufficient stock load returns HTTP 400", insufficient_res.status_code == 400)
    log_test("Insufficient stock error message is human-readable", "only 2 on hand in" in insufficient_res.json().get("detail", ""))

    # 7. Batch Loading: All-Success
    order4 = client.post(
        "/orders",
        json={"customer_id": cust_id, "warehouse_id": wh_id, "items": [{"product_id": prod_id, "quantity": 1, "unit_price": 50.0}]},
        headers=admin_auth,
    )
    order4_id = order4.json()["id"]
    client.post(f"/orders/{order4_id}/confirm", headers=admin_auth)
    del4 = client.post("/deliveries", json={"order_id": order4_id, "delivery_partner_id": dp_id, "vehicle_id": veh_id}, headers=admin_auth).json()
    del4_id = del4["id"]
    _make_delivery_ready(del4)

    order5 = client.post(
        "/orders",
        json={"customer_id": cust_id, "warehouse_id": wh_id, "items": [{"product_id": prod_id, "quantity": 1, "unit_price": 50.0}]},
        headers=admin_auth,
    )
    order5_id = order5.json()["id"]
    client.post(f"/orders/{order5_id}/confirm", headers=admin_auth)
    del5 = client.post("/deliveries", json={"order_id": order5_id, "delivery_partner_id": dp_id, "vehicle_id": veh_id}, headers=admin_auth).json()
    del5_id = del5["id"]
    _make_delivery_ready(del5)

    batch_all_res = client.post(
        "/deliveries/load-batch",
        json={"delivery_ids": [del4_id, del5_id]},
        headers=admin_auth,
    )
    log_test("Batch load all-success returns HTTP 200", batch_all_res.status_code == 200)
    batch_results = batch_all_res.json().get("results", [])
    log_test("Batch load returns 2 results, both ok=True", len(batch_results) == 2 and all(r["ok"] for r in batch_results))

    # 8. Batch Loading: Already-Loaded Idempotency
    batch_repeat_res = client.post(
        "/deliveries/load-batch",
        json={"delivery_ids": [del4_id, del5_id]},
        headers=admin_auth,
    )
    log_test("Batch load already-loaded returns ok=True", batch_repeat_res.status_code == 200 and all(r["ok"] and "already loaded" in r["detail"].lower() for r in batch_repeat_res.json()["results"]))

    # 9. Batch Loading: Partial Failure Isolation (A succeeds, B fails, C succeeds)
    order6 = client.post(
        "/orders",
        json={"customer_id": cust_id, "warehouse_id": wh_id, "items": [{"product_id": prod_id, "quantity": 1, "unit_price": 50.0}]},
        headers=admin_auth,
    )
    order6_id = order6.json()["id"]
    client.post(f"/orders/{order6_id}/confirm", headers=admin_auth)
    del6 = client.post("/deliveries", json={"order_id": order6_id, "delivery_partner_id": dp_id, "vehicle_id": veh_id}, headers=admin_auth).json()
    del6_id = del6["id"]
    _make_delivery_ready(del6)

    order7 = client.post(
        "/orders",
        json={"customer_id": cust_id, "warehouse_id": wh_id, "items": [{"product_id": prod_id, "quantity": 1, "unit_price": 50.0}]},
        headers=admin_auth,
    )
    order7_id = order7.json()["id"]
    client.post(f"/orders/{order7_id}/confirm", headers=admin_auth)
    del7 = client.post("/deliveries", json={"order_id": order7_id, "delivery_partner_id": dp_id, "vehicle_id": veh_id}, headers=admin_auth).json()
    del7_id = del7["id"]
    _make_delivery_ready(del7)

    # del3 is the one with insufficient stock
    batch_partial_res = client.post(
        "/deliveries/load-batch",
        json={"delivery_ids": [del6_id, del3_id, del7_id]},
        headers=admin_auth,
    )
    log_test("Batch load partial failure returns HTTP 200 with isolated results", batch_partial_res.status_code == 200)
    p_results = batch_partial_res.json().get("results", [])
    log_test("Batch results: del6 succeeds", len(p_results) == 3 and p_results[0]["delivery_id"] == del6_id and p_results[0]["ok"] is True)
    log_test("Batch results: del3 fails with readable detail", len(p_results) == 3 and p_results[1]["delivery_id"] == del3_id and p_results[1]["ok"] is False and "only 2 on hand" in p_results[1]["detail"])
    log_test("Batch results: del7 succeeds despite del3 failure", len(p_results) == 3 and p_results[2]["delivery_id"] == del7_id and p_results[2]["ok"] is True)

    # 10. Vehicle Capacity Exceeded Check
    dp2_id, dp2_auth = _create_staff(admin_auth, "Driver Dave", "Delivery Partner")
    small_veh = client.post(
        "/vehicles",
        json={"vehicle_number": f"SCOOTER-{uuid.uuid4().hex[:4]}", "capacity_kg": 15.0, "default_driver_id": dp2_id},
        headers=admin_auth,
    ).json()["id"]

    order8 = client.post(
        "/orders",
        json={"customer_id": cust_id, "warehouse_id": wh_id, "items": [{"product_id": prod_id, "quantity": 1, "unit_price": 50.0}]},
        headers=admin_auth,
    )
    order8_id = order8.json()["id"]
    client.post(f"/orders/{order8_id}/confirm", headers=admin_auth)
    del8 = client.post("/deliveries", json={"order_id": order8_id, "delivery_partner_id": dp2_id, "vehicle_id": small_veh}, headers=admin_auth).json()
    del8_id = del8["id"]
    _make_delivery_ready(del8, custom_auth=dp2_auth)

    order9 = client.post(
        "/orders",
        json={"customer_id": cust_id, "warehouse_id": wh_id, "items": [{"product_id": prod_id, "quantity": 1, "unit_price": 50.0}]},
        headers=admin_auth,
    )
    order9_id = order9.json()["id"]
    client.post(f"/orders/{order9_id}/confirm", headers=admin_auth)
    del9 = client.post("/deliveries", json={"order_id": order9_id, "delivery_partner_id": dp2_id, "vehicle_id": small_veh}, headers=admin_auth).json()
    del9_id = del9["id"]
    _make_delivery_ready(del9, custom_auth=dp2_auth)

    # del8 (10kg) + del9 (10kg) = 20kg > 15kg capacity
    cap_res = client.post(
        "/deliveries/load-batch",
        json={"delivery_ids": [del8_id, del9_id]},
        headers=admin_auth,
    )
    log_test("Batch load exceeding vehicle capacity returns HTTP 400", cap_res.status_code == 400)
    log_test("Capacity exceeded detail is clearly formatted", "Total batch weight (20 kg) exceeds vehicle capacity (15 kg)" in cap_res.json().get("detail", ""))

    # 11. Batch Loading with Unknown Weight (Does NOT fail solely due to unknown weight)
    dp3_id, dp3_auth = _create_staff(admin_auth, "Driver Dan 3", "Delivery Partner")
    veh3 = client.post(
        "/vehicles",
        json={"vehicle_number": f"VAN-{uuid.uuid4().hex[:4]}", "capacity_kg": 50.0, "default_driver_id": dp3_id},
        headers=admin_auth,
    ).json()["id"]
    prod_noweight = client.post(
        "/products",
        json={"name": "Unknown Weight Item", "sku": f"UNKW-{uuid.uuid4().hex[:4]}", "price": 10.0},
        headers=admin_auth,
    ).json()["id"]
    client.post(f"/warehouses/{wh_id}/stock/adjust", json={"product_id": prod_noweight, "quantity": 20}, headers=admin_auth)

    order10 = client.post(
        "/orders",
        json={"customer_id": cust_id, "warehouse_id": wh_id, "items": [{"product_id": prod_noweight, "quantity": 2, "unit_price": 10.0}]},
        headers=admin_auth,
    )
    order10_id = order10.json()["id"]
    client.post(f"/orders/{order10_id}/confirm", headers=admin_auth)
    del10 = client.post("/deliveries", json={"order_id": order10_id, "delivery_partner_id": dp3_id, "vehicle_id": veh3}, headers=admin_auth).json()
    del10_id = del10["id"]
    _make_delivery_ready(del10, custom_auth=dp3_auth)

    batch_unk_res = client.post(
        "/deliveries/load-batch",
        json={"delivery_ids": [del10_id]},
        headers=admin_auth,
    )
    log_test("Batch load with unknown product weight succeeds without false capacity rejection", batch_unk_res.status_code == 200 and batch_unk_res.json()["results"][0]["ok"] is True)

    # 12. Global 500 Handler Verification
    @app.get("/test-unexpected-error-trigger")
    def _test_unexpected():
        raise RuntimeError("Simulated internal explosion")

    client_err = TestClient(app, raise_server_exceptions=False)
    err_res = client_err.get("/test-unexpected-error-trigger", headers={"Origin": "https://crm-saas.asynk.in"})
    log_test("Global exception handler catches unexpected error and returns HTTP 500", err_res.status_code == 500)
    log_test("Global exception response contains sanitized JSON detail", err_res.json() == {"detail": "An unexpected server error occurred. Please try again later or contact support."})
    log_test("Global exception response preserves CORS headers", "access-control-allow-origin" in [h.lower() for h in err_res.headers.keys()])


if __name__ == "__main__":
    ctx = run_lifecycle_tests()
    run_invalid_transition_tests(ctx)
    run_legacy_endpoint_tests(ctx)
    run_permission_matrix_tests(ctx)
    run_stock_concurrency_test()
    run_delivery_partner_assignment_notification_tests(ctx)
    run_vehicle_loading_production_fix_tests()

    print("\n=======================================================")
    print(f"RESULTS: {PASSED} passed, {FAILED} failed")
    print("=======================================================\n")
    if FAILED > 0:
        sys.exit(1)

