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


if __name__ == "__main__":
    ctx = run_lifecycle_tests()
    run_invalid_transition_tests(ctx)
    run_legacy_endpoint_tests(ctx)
    run_permission_matrix_tests(ctx)
    run_stock_concurrency_test()

    print("\n=======================================================")
    print(f"RESULTS: {PASSED} passed, {FAILED} failed")
    print("=======================================================\n")
    if FAILED > 0:
        sys.exit(1)
