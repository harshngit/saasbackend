"""POST /deliveries/{id}/confirm: a fixed number of queries whatever the number of lines,
stage timings in Server-Timing, and an identical retry (e.g. after a client timeout) that
returns the recorded result without applying anything a second time.

Root cause of the 35 s confirms: building the response looked up stock for every line
(~4 queries per line: 138 queries for 20 lines, 218 for 40), each a ~90 ms round trip to
the database in production; a retry sent after the frontend's 20 s timeout then queued
behind the first request on the vehicle-run lock and was answered 400 (or, for a partial
delivery, recorded twice).
"""

import os
import sys
import threading
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event  # noqa: E402

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import DeliveryHistory, DeliveryItem, Organization, Plan, VehicleLoadingItem  # noqa: E402
from app.seed import seed_plans  # noqa: E402
from app.services import entitlement_service  # noqa: E402

c = TestClient(app)


@pytest.fixture(autouse=True)
def _no_plan_gates(monkeypatch):
    monkeypatch.setattr(entitlement_service, "check_entitlement", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "check_limit", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "has_entitlement", lambda *a, **k: True)


@pytest.fixture(scope="module")
def env():
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as s:
        seed_plans(s)
        s.commit()
    r = c.post("/auth/register", json={"organization_name": f"DC {uuid.uuid4().hex[:6]}", "admin_name": "A",
               "email": f"dc_{uuid.uuid4().hex[:10]}@example.com", "password": "Password123!", "role": "admin"})
    assert r.status_code == 201, r.text
    auth = {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}
    org_id = c.get("/auth/me", headers=auth).json()["organization_id"]
    with SessionLocal() as s:
        s.query(Organization).filter(Organization.id == org_id).update(
            {"plan_id": s.query(Plan).filter(Plan.name == "Enterprise").one().id})
        s.commit()
    wh = c.post("/warehouses", json={"name": "Main", "code": f"WH-{uuid.uuid4().hex[:6]}", "is_default": True},
                headers=auth).json()["id"]
    cust = c.post("/customers", json={"name": "Shop", "phone": "9876500000", "delivery_address": "x",
                                      "billing_address": "x"}, headers=auth).json()["id"]
    pids = []
    for i in range(20):
        pid = c.post("/products", json={"name": f"Item {i:02d}", "sku": f"S{i}-{uuid.uuid4().hex[:5]}",
                                        "price": 100.0, "tax_rate": 18.0, "uom": "unit",
                                        "pricing": {"purchase_price": 50.0, "selling_price": 100.0, "currency": "INR"}},
                     headers=auth).json()["id"]
        assert c.post(f"/warehouses/{wh}/stock/adjust", json={"product_id": pid, "quantity": 1000},
                      headers=auth).status_code == 200
        pids.append(pid)
    email = f"dp_{uuid.uuid4().hex[:8]}@example.com"
    partner = c.post("/users", json={"name": "Driver", "email": email, "password": "Password123!",
                                     "role": "delivery_partner"}, headers=auth).json()["id"]
    login = c.post("/auth/login", json={"email": email, "password": "Password123!"}).json()
    veh = c.post("/vehicles", json={"vehicle_number": f"MH{uuid.uuid4().hex[:6].upper()}", "vehicle_type": "Van"},
                 headers=auth).json()["id"]
    return {"auth": auth, "pauth": {"Authorization": f"Bearer {login['tokens']['access_token']}"}, "wh": wh,
            "cust": cust, "pids": pids, "partner": partner, "veh": veh}


def _dispatched(env, n, qty=3):
    """A dispatched delivery of n lines, qty each. Returns (delivery id, line ids)."""
    r = c.post("/orders", json={"customer_id": env["cust"], "warehouse_id": env["wh"], "delivery_method": "home_delivery",
               "payment_status": "pending", "items": [{"product_id": p, "quantity": qty} for p in env["pids"][:n]]},
               headers=env["auth"])
    assert r.status_code == 201, r.text
    did = r.json()["delivery_id"]
    a = env["auth"]
    assert c.patch(f"/deliveries/by-id/{did}", json={"delivery_partner_id": env["partner"], "vehicle_id": env["veh"]},
                   headers=a).status_code == 200
    assert c.post(f"/deliveries/{did}/accept", headers=env["pauth"]).status_code == 200
    lines = [i["id"] for i in c.get(f"/deliveries/by-id/{did}", headers=a).json()["items"]]
    assert c.post(f"/deliveries/{did}/pick", json={"items": [{"delivery_item_id": i, "picked_quantity": qty}
                  for i in lines]}, headers=a).status_code == 200
    assert c.post(f"/deliveries/{did}/ready", headers=a).status_code == 200
    assert c.post(f"/deliveries/{did}/load", json={}, headers=a).status_code == 200
    assert c.patch(f"/deliveries/by-id/{did}", json={"status": "in_transit"}, headers=a).status_code == 200
    return did, lines


def _state(did):
    with SessionLocal() as s:
        items = sorted((i.id, i.delivered_quantity) for i in s.query(DeliveryItem).filter(DeliveryItem.delivery_id == did))
        vehicle = sorted(v.delivered_qty or 0 for v in s.query(VehicleLoadingItem))
        history = s.query(DeliveryHistory).filter(DeliveryHistory.delivery_id == did).count()
        return items, sum(vehicle), history


def _confirm(env, did, body):
    return c.post(f"/deliveries/{did}/confirm", json=body, headers=env["auth"])


def test_query_count_does_not_grow_with_lines(env):
    counts = {}
    for n in (1, 20):
        did, lines = _dispatched(env, n)
        seen = {"q": 0}

        def _count(*_a):
            seen["q"] += 1

        event.listen(engine, "before_cursor_execute", _count)
        try:
            r = _confirm(env, did, {"items": [{"delivery_item_id": i, "delivered_quantity": 3} for i in lines]})
        finally:
            event.remove(engine, "before_cursor_execute", _count)
        assert r.status_code == 200, r.text
        counts[n] = seen["q"]
    assert counts[20] == counts[1], counts
    assert counts[20] <= 50, counts


def test_server_timing_has_the_confirm_stages(env):
    did, lines = _dispatched(env, 3)
    r = _confirm(env, did, {"items": [{"delivery_item_id": i, "delivered_quantity": 3} for i in lines]})
    assert r.status_code == 200
    header = r.headers["server-timing"]
    for stage in ("load", "lock", "lines", "order_fulfilment", "history_events", "commit", "response"):
        assert f"{stage};dur=" in header, header


def test_identical_retry_of_a_full_confirm_changes_nothing(env):
    did, lines = _dispatched(env, 5)
    body = {"items": [{"delivery_item_id": i, "delivered_quantity": 3} for i in lines], "receiver_name": "Ravi"}
    first = _confirm(env, did, body)
    assert first.status_code == 200 and first.json()["internal_status"] == "delivered"
    before = _state(did)
    again = _confirm(env, did, body)
    assert again.status_code == 200, again.text
    assert again.json()["internal_status"] == "delivered"
    assert [i["delivered_quantity"] for i in again.json()["items"]] == [i["delivered_quantity"] for i in first.json()["items"]]
    assert _state(did) == before                                   # nothing applied, no extra history entry
    assert "confirm_fingerprint" not in str(again.json()["timeline"] if again.json().get("timeline") else "")


def test_identical_retry_of_a_partial_confirm_is_not_recorded_twice(env):
    did, lines = _dispatched(env, 3, qty=3)
    part = {"items": [{"delivery_item_id": i, "delivered_quantity": 1} for i in lines]}
    assert _confirm(env, did, part).status_code == 200
    before = _state(did)
    assert _confirm(env, did, part).status_code == 200             # the retry
    assert _state(did) == before                                   # still 1 each, not 2
    # A genuinely new hand-over (different content) still goes through as before.
    rest = {"items": [{"delivery_item_id": i, "delivered_quantity": 2} for i in lines], "notes": "second visit"}
    r = _confirm(env, did, rest)
    assert r.status_code == 200 and r.json()["internal_status"] == "delivered"
    assert {q for _, q in _state(did)[0]} == {3.0}


def test_different_payload_after_full_delivery_is_still_refused(env):
    did, lines = _dispatched(env, 2, qty=2)
    assert _confirm(env, did, {"items": [{"delivery_item_id": i, "delivered_quantity": 2} for i in lines]}).status_code == 200
    r = _confirm(env, did, {"items": [{"delivery_item_id": lines[0], "delivered_quantity": 1}]})
    assert r.status_code == 400


def test_identical_retry_of_a_failed_attempt(env):
    did, _lines = _dispatched(env, 2)
    body = {"items": [], "failed": True, "failure_reason": "Shop closed"}
    assert _confirm(env, did, body).status_code == 200
    before = _state(did)
    r = _confirm(env, did, body)
    assert r.status_code == 200 and r.json()["internal_status"] == "failed"
    assert _state(did) == before


def test_two_identical_confirms_at_the_same_moment_apply_once(env):
    did, lines = _dispatched(env, 4, qty=4)
    body = {"items": [{"delivery_item_id": i, "delivered_quantity": 1} for i in lines]}
    start = threading.Barrier(2)
    codes = []

    def go():
        client = TestClient(app, raise_server_exceptions=False)
        start.wait()
        codes.append(client.post(f"/deliveries/{did}/confirm", json=body, headers=env["auth"]).status_code)

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert sorted(codes) == [200, 200], codes
    assert {q for _, q in _state(did)[0]} == {1.0}                 # delivered once, not twice


def test_two_genuine_identical_partials_are_both_recorded(env, monkeypatch):
    """2 + 2 of 6: a second, identical partial hand-over after the retry window is a real
    one and is recorded; an identical confirm right after it (a retry) is not."""
    import time
    from datetime import timedelta

    from app.services import delivery_service

    monkeypatch.setattr(delivery_service, "PARTIAL_RETRY_WINDOW", timedelta(seconds=1))
    did, lines = _dispatched(env, 2, qty=6)
    two = {"items": [{"delivery_item_id": i, "delivered_quantity": 2} for i in lines]}
    assert _confirm(env, did, two).status_code == 200
    assert {q for _, q in _state(did)[0]} == {2.0}
    time.sleep(1.5)                                             # the driver's second hand-over
    second = _confirm(env, did, two)
    assert second.status_code == 200 and second.json()["internal_status"] == "partially_delivered"
    assert {q for _, q in _state(did)[0]} == {4.0}               # both recorded: 2 + 2
    retry = _confirm(env, did, two)                              # immediate repeat = a retry
    assert retry.status_code == 200
    assert {q for _, q in _state(did)[0]} == {4.0}               # not 6


def test_full_delivery_retry_is_still_deduplicated_after_the_partial_window(env, monkeypatch):
    import time
    from datetime import timedelta

    from app.services import delivery_service

    monkeypatch.setattr(delivery_service, "PARTIAL_RETRY_WINDOW", timedelta(seconds=1))
    did, lines = _dispatched(env, 2, qty=2)
    body = {"items": [{"delivery_item_id": i, "delivered_quantity": 2} for i in lines]}
    assert _confirm(env, did, body).status_code == 200
    before = _state(did)
    time.sleep(1.5)
    r = _confirm(env, did, body)
    assert r.status_code == 200 and r.json()["internal_status"] == "delivered"
    assert _state(did) == before
