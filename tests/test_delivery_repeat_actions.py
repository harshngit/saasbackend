"""Repeating a delivery action in the state it already produced is a 200 no-op: the current
delivery comes back, no new history entry, timestamps untouched. Invalid moves are refused as
before. Dispatch ("Start Delivery") and accept had the bug (200 + a new history entry on every
repeat); ready / load already refuse a repeat with 400 and reject clears the partner, so they
are unchanged.
"""

import os
import sys
import threading
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Delivery, DeliveryHistory, Organization, Plan  # noqa: E402
from app.seed import seed_plans  # noqa: E402
from app.services import entitlement_service  # noqa: E402

c = TestClient(app)


@pytest.fixture(autouse=True)
def _no_plan_gates(monkeypatch):
    monkeypatch.setattr(entitlement_service, "check_entitlement", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "check_limit", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "has_entitlement", lambda *a, **k: True)


def _staff(auth, role):
    email = f"{role}_{uuid.uuid4().hex[:8]}@example.com"
    r = c.post("/users", json={"name": role, "email": email, "password": "Password123!", "role": role}, headers=auth)
    assert r.status_code == 201, r.text
    token = c.post("/auth/login", json={"email": email, "password": "Password123!"}).json()["tokens"]["access_token"]
    return r.json()["id"], {"Authorization": f"Bearer {token}"}


def _firm():
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as s:
        seed_plans(s)
        s.commit()
    r = c.post("/auth/register", json={"organization_name": f"RA {uuid.uuid4().hex[:6]}", "admin_name": "A",
               "email": f"ra_{uuid.uuid4().hex[:10]}@example.com", "password": "Password123!", "role": "admin"})
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
    pid = c.post("/products", json={"name": "Item", "sku": f"RA-{uuid.uuid4().hex[:6]}", "price": 100.0, "tax_rate": 18.0,
                 "uom": "unit", "pricing": {"purchase_price": 50.0, "selling_price": 100.0, "currency": "INR"}},
                 headers=auth).json()["id"]
    c.post(f"/warehouses/{wh}/stock/adjust", json={"product_id": pid, "quantity": 100}, headers=auth)
    partner, pauth = _staff(auth, "delivery_partner")
    _other_partner, oauth = _staff(auth, "delivery_partner")
    veh = c.post("/vehicles", json={"vehicle_number": f"MH{uuid.uuid4().hex[:6].upper()}", "vehicle_type": "Van"},
                 headers=auth).json()["id"]
    return {"auth": auth, "pauth": pauth, "oauth": oauth, "wh": wh, "cust": cust, "pid": pid, "partner": partner, "veh": veh}


def _delivery(f, until):
    r = c.post("/orders", json={"customer_id": f["cust"], "warehouse_id": f["wh"], "delivery_method": "home_delivery",
               "payment_status": "pending", "items": [{"product_id": f["pid"], "quantity": 2}]}, headers=f["auth"])
    did = r.json()["delivery_id"]
    a = f["auth"]
    assert c.patch(f"/deliveries/by-id/{did}", json={"delivery_partner_id": f["partner"], "vehicle_id": f["veh"]},
                   headers=a).status_code == 200
    if until == "planned":
        return did
    assert c.post(f"/deliveries/{did}/accept", headers=f["pauth"]).status_code == 200
    if until == "accepted":
        return did
    line = c.get(f"/deliveries/by-id/{did}", headers=a).json()["items"][0]["id"]
    assert c.post(f"/deliveries/{did}/pick", json={"items": [{"delivery_item_id": line, "picked_quantity": 2}]},
                  headers=a).status_code == 200
    assert c.post(f"/deliveries/{did}/ready", headers=a).status_code == 200
    if until == "ready":
        return did
    assert c.post(f"/deliveries/{did}/load", json={}, headers=a).status_code == 200
    return did


def _history(did, event_type):
    with SessionLocal() as s:
        return s.query(DeliveryHistory).filter(DeliveryHistory.delivery_id == did,
                                               DeliveryHistory.event_type == event_type).count()


def _dispatch(f, did, headers=None):
    return c.patch(f"/deliveries/by-id/{did}", json={"status": "in_transit"}, headers=headers or f["auth"])


@pytest.fixture(scope="module")
def firms():
    return _firm(), _firm()


def test_second_dispatch_is_a_no_op(firms):
    f = firms[0]
    did = _delivery(f, "loaded")
    first = _dispatch(f, did)
    assert first.status_code == 200 and first.json()["internal_status"] == "in_transit"
    with SessionLocal() as s:
        stamped = s.get(Delivery, did).dispatched_at
    again = _dispatch(f, did)
    assert again.status_code == 200 and again.json()["internal_status"] == "in_transit"
    assert _history(did, "dispatched") == 1
    with SessionLocal() as s:
        assert s.get(Delivery, did).dispatched_at == stamped
    body1, body2 = first.json(), again.json()
    for k in ("timeline", "updated_at"):
        body1.pop(k, None), body2.pop(k, None)
    assert body1 == body2


def test_simultaneous_dispatches_write_one_history_entry(firms):
    f = firms[0]
    did = _delivery(f, "loaded")
    start = threading.Barrier(3)
    codes = []

    def go():
        cl = TestClient(app, raise_server_exceptions=False)
        start.wait()
        codes.append(cl.patch(f"/deliveries/by-id/{did}", json={"status": "in_transit"}, headers=f["auth"]).status_code)

    ts = [threading.Thread(target=go) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=60)
    assert codes == [200, 200, 200], codes
    assert _history(did, "dispatched") == 1


def test_second_accept_is_a_no_op_and_others_still_refused(firms):
    f = firms[0]
    did = _delivery(f, "planned")
    assert c.post(f"/deliveries/{did}/accept", headers=f["pauth"]).status_code == 200
    again = c.post(f"/deliveries/{did}/accept", headers=f["pauth"])
    assert again.status_code == 200 and again.json()["internal_status"] == "accepted"
    assert _history(did, "accepted") == 1
    # Not their delivery: hidden from them by the route (404), as before.
    assert c.post(f"/deliveries/{did}/accept", headers=f["oauth"]).status_code == 404


def test_invalid_moves_are_refused_as_before(firms):
    f = firms[0]
    did = _delivery(f, "ready")
    assert _dispatch(f, did).status_code == 400                 # ready -> in_transit is not allowed
    did = _delivery(f, "loaded")
    assert _dispatch(f, did).status_code == 200
    with SessionLocal() as s:
        line = s.query(Delivery).get(did).items[0].id
    assert c.post(f"/deliveries/{did}/confirm", json={"items": [{"delivery_item_id": line, "delivered_quantity": 2}]},
                  headers=f["auth"]).status_code == 200
    r = _dispatch(f, did)                                       # delivered -> in_transit
    assert r.status_code == 400
    assert _history(did, "dispatched") == 1
    assert c.post(f"/deliveries/{did}/ready", headers=f["auth"]).status_code == 400


def test_other_firm_cannot_dispatch(firms):
    f, other = firms
    did = _delivery(f, "loaded")
    assert _dispatch(f, did, other["auth"]).status_code == 404
    assert _history(did, "dispatched") == 0
