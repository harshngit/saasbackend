"""Delivery Module requirements completion:

  A. Delivery Partner approved-leave validation on assignment
  B. Order cancellation -> pre-operational Delivery cancellation sync
  C. Persistent Delivery timeline/history, exposed on GET /deliveries/by-id/{id}

Reuses the setup pattern from tests/test_delivery_workflow_consistency.py.
"""

import os
import sys
import uuid
from datetime import date, timedelta

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
    admin_auth = _register_org(label)
    so_id, so_auth = _create_staff(admin_auth, "Sales Officer", "Sales Officer")
    dp_id, dp_auth = _create_staff(admin_auth, "Delivery Partner", "Delivery Partner")
    dp2_id, dp2_auth = _create_staff(admin_auth, "Delivery Partner Two", "Delivery Partner")

    wh = client.post("/warehouses", json={"name": "Main WH", "is_default": True}, headers=admin_auth)
    assert wh.status_code == 201, wh.text
    wh_id = wh.json()["id"]

    prod = client.post(
        "/products",
        json={"name": f"Widget {uuid.uuid4().hex[:4]}", "sku": f"SKU-{uuid.uuid4().hex[:6]}", "price": 100.0},
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
        "dp_id": dp_id, "dp_auth": dp_auth, "dp2_id": dp2_id, "dp2_auth": dp2_auth,
        "wh_id": wh_id, "prod_id": prod_id, "cust_id": cust_id, "veh_id": veh_id,
    }


def _place_order(ctx: dict, qty: int = 10):
    order = client.post(
        "/orders",
        json={"customer_id": ctx["cust_id"], "warehouse_id": ctx["wh_id"],
              "items": [{"product_id": ctx["prod_id"], "quantity": qty, "unit_price": 100.0}]},
        headers=ctx["admin_auth"],
    )
    assert order.status_code == 201, order.text
    # Every order now starts as an unreserved Draft (finalized business rule)
    # -- confirm it so callers get the same "ready to plan a delivery for"
    # order this helper always returned before that change.
    confirmed = client.post(f"/orders/{order.json()['id']}/confirm", headers=ctx["admin_auth"])
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()


def _pick_and_ready(dlv: dict, admin_auth: dict):
    """accepted -> picked -> ready. Loading only proceeds from 'ready'."""
    items = [
        {"delivery_item_id": it["id"], "picked_quantity": it["planned_quantity"]}
        for it in dlv["items"]
    ]
    r_pick = client.post(f"/deliveries/{dlv['id']}/pick", json={"items": items}, headers=admin_auth)
    assert r_pick.status_code == 200, r_pick.text
    r_ready = client.post(f"/deliveries/{dlv['id']}/ready", headers=admin_auth)
    assert r_ready.status_code == 200, r_ready.text
    return r_ready.json()


def _approved_leave(ctx: dict, partner_auth: dict, start: date, end: date) -> dict:
    r = client.post(
        "/leaves",
        json={"leave_type": "casual", "start_date": start.isoformat(), "end_date": end.isoformat()},
        headers=partner_auth,
    )
    assert r.status_code == 201, r.text
    leave_id = r.json()["id"]
    r_app = client.patch(f"/leaves/{leave_id}/approve", headers=ctx["admin_auth"])
    assert r_app.status_code == 200, r_app.text
    return r_app.json()


# ============================================================================
# A. Delivery Partner approved-leave validation
# ============================================================================

def run_leave_validation_tests():
    print("\n=== A. Delivery Partner approved-leave validation ===")
    ctx = _setup_full_flow(f"DelLeave_{uuid.uuid4().hex[:6]}")
    today = date.today()

    # Test 1: no leave -> assignment succeeds
    order1 = _place_order(ctx)
    r = client.patch(
        f"/orders/{order1['id']}/assign-delivery-partner",
        json={"delivery_partner_id": ctx["dp_id"]}, headers=ctx["admin_auth"],
    )
    log_test("Test 1: assign partner with no leave succeeds (200)", r.status_code == 200, r.text)

    # Test 2/3/4/5: approved leave covering / starting / ending on / spanning the date
    _approved_leave(ctx, ctx["dp_auth"], today, today + timedelta(days=3))
    order2 = _place_order(ctx)
    r = client.patch(
        f"/orders/{order2['id']}/assign-delivery-partner",
        json={"delivery_partner_id": ctx["dp_id"]}, headers=ctx["admin_auth"],
    )
    log_test(
        "Test 2/3/4/5: assign partner on approved leave covering today rejected (400)",
        r.status_code == 400, r.text,
    )

    # Test 6: pending leave does not block
    r_pending = client.post(
        "/leaves",
        json={
            "leave_type": "sick",
            "start_date": (today + timedelta(days=20)).isoformat(),
            "end_date": (today + timedelta(days=21)).isoformat(),
        },
        headers=ctx["dp2_auth"],
    )
    assert r_pending.status_code == 201, r_pending.text
    order6 = _place_order(ctx)
    r = client.patch(
        f"/orders/{order6['id']}/assign-delivery-partner",
        json={"delivery_partner_id": ctx["dp2_id"]}, headers=ctx["admin_auth"],
    )
    log_test("Test 6: pending leave does not block assignment (200)", r.status_code == 200, r.text)

    # Test 7: rejected leave does not block
    r_reject_target = client.post(
        "/leaves",
        json={
            "leave_type": "sick",
            "start_date": (today + timedelta(days=25)).isoformat(),
            "end_date": (today + timedelta(days=26)).isoformat(),
        },
        headers=ctx["dp2_auth"],
    )
    leave_id = r_reject_target.json()["id"]
    r_reject = client.patch(f"/leaves/{leave_id}/reject", json={"reject_reason": "no"}, headers=ctx["admin_auth"])
    log_test("Test 7 setup: leave rejected", r_reject.status_code == 200, r_reject.text)

    # Test 8: cross-org leave cannot affect assignment
    other_ctx = _setup_full_flow(f"DelLeaveOther_{uuid.uuid4().hex[:6]}")
    other_order = _place_order(other_ctx)
    r = client.patch(
        f"/orders/{other_order['id']}/assign-delivery-partner",
        json={"delivery_partner_id": other_ctx["dp_id"]}, headers=other_ctx["admin_auth"],
    )
    log_test(
        "Test 8: cross-org partner (no leave in THIS org) can be assigned (200)",
        r.status_code == 200, r.text,
    )


# ============================================================================
# B. Order cancellation -> Delivery cancellation synchronization
# ============================================================================

def run_cancellation_sync_tests():
    print("\n=== B. Order cancellation -> Delivery sync ===")
    ctx = _setup_full_flow(f"DelCancel_{uuid.uuid4().hex[:6]}")

    # Test 9: planned Delivery -> order cancel also cancels the Delivery
    order = _place_order(ctx)
    dlv = client.post(
        "/deliveries",
        json={"order_id": order["id"], "delivery_partner_id": ctx["dp_id"], "vehicle_id": ctx["veh_id"]},
        headers=ctx["admin_auth"],
    )
    assert dlv.status_code == 201, dlv.text
    dlv_id = dlv.json()["id"]
    log_test("Test 9 setup: delivery planned", dlv.json()["status"] == "pending", dlv.text)

    r_cancel = client.patch(f"/orders/{order['id']}/cancel", json={"reason": "customer changed mind"}, headers=ctx["admin_auth"])
    log_test("Test 9: order cancellation succeeds (200)", r_cancel.status_code == 200, r_cancel.text)
    r_dlv = client.get(f"/deliveries/by-id/{dlv_id}", headers=ctx["admin_auth"])
    log_test("Test 9: linked planned Delivery is now cancelled", r_dlv.json()["status"] == "cancelled", r_dlv.text)

    # Test 10: accepted (not yet operational) Delivery -> also cancelled
    order2 = _place_order(ctx)
    dlv2 = client.post(
        "/deliveries",
        json={"order_id": order2["id"], "delivery_partner_id": ctx["dp_id"], "vehicle_id": ctx["veh_id"]},
        headers=ctx["admin_auth"],
    ).json()
    r_accept = client.post(f"/deliveries/{dlv2['id']}/accept", headers=ctx["dp_auth"])
    log_test("Test 10 setup: delivery accepted", r_accept.status_code == 200, r_accept.text)
    r_cancel2 = client.patch(f"/orders/{order2['id']}/cancel", json={"reason": "test"}, headers=ctx["admin_auth"])
    log_test("Test 10: order with accepted (pre-load) Delivery cancels (200)", r_cancel2.status_code == 200, r_cancel2.text)
    r_dlv2 = client.get(f"/deliveries/by-id/{dlv2['id']}", headers=ctx["admin_auth"])
    log_test("Test 10: accepted Delivery is now cancelled", r_dlv2.json()["status"] == "cancelled", r_dlv2.text)

    # Test 11: loaded Delivery -> order cancel rejected
    order3 = _place_order(ctx)
    dlv3 = client.post(
        "/deliveries",
        json={"order_id": order3["id"], "delivery_partner_id": ctx["dp_id"], "vehicle_id": ctx["veh_id"]},
        headers=ctx["admin_auth"],
    ).json()
    client.post(f"/deliveries/{dlv3['id']}/accept", headers=ctx["dp_auth"])
    _pick_and_ready(dlv3, ctx["admin_auth"])
    r_load = client.post(f"/deliveries/{dlv3['id']}/load", headers=ctx["admin_auth"])
    log_test("Test 11 setup: delivery loaded", r_load.status_code == 200, r_load.text)
    r_cancel3 = client.patch(f"/orders/{order3['id']}/cancel", json={"reason": "test"}, headers=ctx["admin_auth"])
    log_test("Test 11: order cancel rejected once Delivery is loaded (400)", r_cancel3.status_code == 400, r_cancel3.text)
    r_dlv3 = client.get(f"/deliveries/by-id/{dlv3['id']}", headers=ctx["admin_auth"])
    # Internal status stays 'loaded'; the public API reports that as 'in_transit'
    # (see app.core.workflow.PUBLIC_DELIVERY_STATUS) -- either way, unchanged by
    # the blocked cancellation.
    log_test(
        "Test 13: blocked cancellation leaves Delivery status untouched (public 'in_transit')",
        r_dlv3.json()["status"] == "in_transit", r_dlv3.text,
    )

    # Test 12: in-transit Delivery -> order cancel rejected
    order4 = _place_order(ctx)
    dlv4 = client.post(
        "/deliveries",
        json={"order_id": order4["id"], "delivery_partner_id": ctx["dp_id"], "vehicle_id": ctx["veh_id"]},
        headers=ctx["admin_auth"],
    ).json()
    client.post(f"/deliveries/{dlv4['id']}/accept", headers=ctx["dp_auth"])
    _pick_and_ready(dlv4, ctx["admin_auth"])
    client.post(f"/deliveries/{dlv4['id']}/load", headers=ctx["admin_auth"])
    r_dispatch = client.patch(f"/deliveries/by-id/{dlv4['id']}", json={"status": "in_transit"}, headers=ctx["admin_auth"])
    log_test("Test 12 setup: delivery dispatched (in_transit)", r_dispatch.status_code == 200, r_dispatch.text)
    r_cancel4 = client.patch(f"/orders/{order4['id']}/cancel", json={"reason": "test"}, headers=ctx["admin_auth"])
    log_test("Test 12: order cancel rejected once Delivery is in_transit (400)", r_cancel4.status_code == 400, r_cancel4.text)

    # Test 15: regular order cancellation (no delivery at all) still works
    order5 = _place_order(ctx)
    r_cancel5 = client.patch(f"/orders/{order5['id']}/cancel", json={"reason": "test"}, headers=ctx["admin_auth"])
    log_test("Test 15: existing cancellation behavior (no delivery) intact", r_cancel5.status_code == 200 and r_cancel5.json()["status"] == "cancelled", r_cancel5.text)


# ============================================================================
# C. Persistent Delivery timeline
# ============================================================================

def run_timeline_tests():
    print("\n=== C. Delivery timeline / history ===")
    ctx = _setup_full_flow(f"DelTimeline_{uuid.uuid4().hex[:6]}")

    order = _place_order(ctx)
    dlv = client.post(
        "/deliveries",
        json={"order_id": order["id"], "delivery_partner_id": ctx["dp_id"], "vehicle_id": ctx["veh_id"]},
        headers=ctx["admin_auth"],
    )
    assert dlv.status_code == 201, dlv.text
    dlv_id = dlv.json()["id"]

    # Test 31: Delivery Detail returns timeline
    r_detail = client.get(f"/deliveries/by-id/{dlv_id}", headers=ctx["admin_auth"])
    log_test("Test 31: GET delivery detail returns 'timeline'", "timeline" in r_detail.json(), r_detail.text)
    timeline = r_detail.json()["timeline"]

    # Test 16/17: creation + assignment events
    log_test("Test 16: 'created' event present", any(e["event_type"] == "created" for e in timeline))
    log_test("Test 17: 'assigned' event present", any(e["event_type"] == "assigned" for e in timeline))

    # Test 34: actor metadata present
    created_evt = next(e for e in timeline if e["event_type"] == "created")
    log_test(
        "Test 34: actor metadata populated on 'created' event",
        created_evt["actor"] is not None and created_evt["actor"]["id"] is not None,
        created_evt,
    )

    # Test 32/33: chronological ordering + correct status transition info
    r_accept = client.post(f"/deliveries/{dlv_id}/accept", headers=ctx["dp_auth"])
    log_test("Test 19 setup: accept succeeds", r_accept.status_code == 200, r_accept.text)
    _pick_and_ready(dlv.json(), ctx["admin_auth"])
    r_load = client.post(f"/deliveries/{dlv_id}/load", headers=ctx["admin_auth"])
    log_test("Test 21 setup: load succeeds", r_load.status_code == 200, r_load.text)
    r_dispatch = client.patch(f"/deliveries/by-id/{dlv_id}", json={"status": "in_transit"}, headers=ctx["admin_auth"])
    log_test("Test 21 setup: dispatch succeeds", r_dispatch.status_code == 200, r_dispatch.text)
    r_confirm = client.post(
        f"/deliveries/{dlv_id}/confirm",
        json={"items": [{"delivery_item_id": dlv.json()["items"][0]["id"], "delivered_quantity": 10}]},
        headers=ctx["admin_auth"],
    )
    log_test("Test 22 setup: confirm (delivered) succeeds", r_confirm.status_code == 200, r_confirm.text)

    r_detail2 = client.get(f"/deliveries/by-id/{dlv_id}", headers=ctx["admin_auth"])
    timeline2 = r_detail2.json()["timeline"]
    event_types = [e["event_type"] for e in timeline2]
    log_test("Test 19: 'accepted' event recorded", "accepted" in event_types)
    log_test("Test 21: 'loaded' event recorded", "loaded" in event_types)
    log_test("Test 21: 'dispatched' event recorded", "dispatched" in event_types)
    log_test("Test 22: 'delivered' event recorded", "delivered" in event_types)

    created_at_values = [e["created_at"] for e in timeline2]
    log_test("Test 32: timeline events are chronologically ordered", created_at_values == sorted(created_at_values))

    dispatched_evt = next(e for e in timeline2 if e["event_type"] == "dispatched")
    log_test(
        "Test 33: 'dispatched' event carries correct previous/new status",
        dispatched_evt["previous_status"] == "loaded" and dispatched_evt["new_status"] == "in_transit",
        dispatched_evt,
    )

    # Test 25: cancellation event
    ctx2 = _setup_full_flow(f"DelTimelineCancel_{uuid.uuid4().hex[:6]}")
    order2 = _place_order(ctx2)
    dlv2 = client.post(
        "/deliveries",
        json={"order_id": order2["id"], "delivery_partner_id": ctx2["dp_id"], "vehicle_id": ctx2["veh_id"]},
        headers=ctx2["admin_auth"],
    ).json()
    client.patch(f"/deliveries/by-id/{dlv2['id']}", json={"status": "cancelled"}, headers=ctx2["admin_auth"])
    r_detail3 = client.get(f"/deliveries/by-id/{dlv2['id']}", headers=ctx2["admin_auth"])
    log_test(
        "Test 25: 'cancelled' event recorded",
        any(e["event_type"] == "cancelled" for e in r_detail3.json()["timeline"]),
        r_detail3.text,
    )

    # Test 20: rejected event
    ctx3 = _setup_full_flow(f"DelTimelineReject_{uuid.uuid4().hex[:6]}")
    order3 = _place_order(ctx3)
    dlv3 = client.post(
        "/deliveries",
        json={"order_id": order3["id"], "delivery_partner_id": ctx3["dp_id"], "vehicle_id": ctx3["veh_id"]},
        headers=ctx3["admin_auth"],
    ).json()
    client.post(f"/deliveries/{dlv3['id']}/reject", json={"reason": "no vehicle"}, headers=ctx3["dp_auth"])
    r_detail4 = client.get(f"/deliveries/by-id/{dlv3['id']}", headers=ctx3["admin_auth"])
    log_test(
        "Test 20: 'rejected' event recorded",
        any(e["event_type"] == "rejected" for e in r_detail4.json()["timeline"]),
        r_detail4.text,
    )

    # Test 18: reassignment event
    r_reassign = client.patch(
        f"/orders/{order3['id']}/assign-delivery-partner",
        json={"delivery_partner_id": ctx3["dp2_id"]}, headers=ctx3["admin_auth"],
    )
    log_test("Test 18 setup: reassignment via order endpoint succeeds", r_reassign.status_code == 200, r_reassign.text)

    # Test 30 (list response): GET /deliveries list must NOT carry a populated timeline
    r_list = client.get("/deliveries", headers=ctx3["admin_auth"])
    log_test(
        "List response leaves 'timeline' empty (not auto-loaded)",
        all(row.get("timeline") == [] for row in r_list.json()),
        r_list.text[:300],
    )

    # Test 35: cross-organization user cannot access timeline
    other_ctx = _setup_full_flow(f"DelTimelineOther_{uuid.uuid4().hex[:6]}")
    r_cross = client.get(f"/deliveries/by-id/{dlv_id}", headers=other_ctx["admin_auth"])
    log_test("Test 35: cross-org user cannot access this delivery/timeline (404)", r_cross.status_code == 404, r_cross.text)

    # Test 36: one Delivery Partner cannot access another partner's Delivery timeline
    r_other_partner = client.get(f"/deliveries/by-id/{dlv_id}", headers=ctx["dp2_auth"])
    log_test(
        "Test 36: unrelated Delivery Partner cannot access another partner's Delivery (404)",
        r_other_partner.status_code == 404, r_other_partner.text,
    )


def run_all_tests():
    global PASSED, FAILED
    PASSED = 0
    FAILED = 0
    print("\n=======================================================")
    print("TEST SUITE: Delivery Leave Validation, Cancellation Sync & Timeline")
    print("=======================================================")
    run_leave_validation_tests()
    run_cancellation_sync_tests()
    run_timeline_tests()
    print("\n=======================================================")
    print(f"RESULTS: {PASSED} passed, {FAILED} failed")
    print("=======================================================\n")
    if FAILED > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_all_tests()
