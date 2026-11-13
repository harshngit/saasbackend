"""Category (A) of scripts/data_audit.py: a delivery that is loaded / in transit under a
CANCELLED order (legacy rows -- the API now refuses to cancel such an order).

This drives the EXISTING endpoints only, step by step, and records each request and
response so the steps can be followed by hand. No new endpoint is involved.

    python -m pytest tests/test_cancelled_order_delivery_return_flow.py -s   # -s prints the steps
"""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import SalesOrder  # noqa: E402
from app.services import entitlement_service  # noqa: E402

client = TestClient(app)
STEPS: list[str] = []


@pytest.fixture(scope="module", autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)


@pytest.fixture(autouse=True)
def _no_plan_gates(monkeypatch):
    monkeypatch.setattr(entitlement_service, "check_entitlement", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "check_limit", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "has_entitlement", lambda *a, **k: True)


def _call(method, url, headers, label, **kw):
    r = client.request(method, url, headers=headers, **kw)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    summary = {k: body.get(k) for k in ("status", "order_status", "fulfilment_status", "detail") if k in body}
    STEPS.append(f"{label}: {method} {url.split('?')[0]} -> {r.status_code} {summary}")
    return r


def _ok(r, code=200):
    assert r.status_code == code, r.text
    return r.json()


def _setup():
    email = f"admin_{uuid.uuid4().hex[:8]}@retflow-example.com"
    admin = {"Authorization": "Bearer " + _ok(client.post("/auth/register", json={
        "organization_name": "Return Flow Org", "admin_name": "Admin", "email": email,
        "password": "Password123!", "role": "admin"}), 201)["tokens"]["access_token"]}
    dp_email = f"dp_{uuid.uuid4().hex[:8]}@retflow-example.com"
    dp_id = _ok(client.post("/users", json={"name": "Driver", "email": dp_email, "password": "Password123!",
                                            "role": "Delivery Partner"}, headers=admin), 201)["id"]
    dp = {"Authorization": "Bearer " + _ok(client.post("/auth/login", json={
        "email": dp_email, "password": "Password123!"}))["tokens"]["access_token"]}
    wh = _ok(client.post("/warehouses", json={"name": "Main WH", "is_default": True}, headers=admin), 201)["id"]
    prod = _ok(client.post("/products", json={"name": "Widget", "sku": f"SKU-{uuid.uuid4().hex[:6]}", "price": 100.0},
                           headers=admin), 201)["id"]
    _ok(client.post(f"/warehouses/{wh}/stock/adjust", json={"product_id": prod, "quantity": 100}, headers=admin))
    cust = _ok(client.post("/customers", json={"name": "Shop"}, headers=admin), 201)["id"]
    veh = _ok(client.post("/vehicles", json={"vehicle_number": f"MH-{uuid.uuid4().hex[:5].upper()}",
                                              "vehicle_type": "van"}, headers=admin), 201)["id"]
    return {"admin": admin, "dp": dp, "dp_id": dp_id, "wh": wh, "prod": prod, "cust": cust, "veh": veh}


def _on_hand(ctx) -> float:
    rows = _ok(client.get("/warehouses/stock", params={"product_id": ctx["prod"], "page_size": 100}, headers=ctx["admin"]))
    rows = rows["items"] if isinstance(rows, dict) else rows
    return sum(r["on_hand"] for r in rows if r.get("warehouse_id", ctx["wh"]) == ctx["wh"])


def _loaded_delivery(ctx, qty=10):
    order = _ok(client.post("/orders", json={"customer_id": ctx["cust"], "warehouse_id": ctx["wh"],
                                             "items": [{"product_id": ctx["prod"], "quantity": qty,
                                                        "unit_price": 100.0}]}, headers=ctx["admin"]), 201)
    _ok(client.post(f"/orders/{order['id']}/confirm", headers=ctx["admin"]))
    dlv = _ok(client.post("/deliveries", json={"order_id": order["id"], "delivery_partner_id": ctx["dp_id"],
                                               "vehicle_id": ctx["veh"]}, headers=ctx["admin"]), 201)
    _ok(client.post(f"/deliveries/{dlv['id']}/accept", headers=ctx["dp"]))
    _ok(client.post(f"/deliveries/{dlv['id']}/pick", json={"items": [
        {"delivery_item_id": dlv["items"][0]["id"], "picked_quantity": qty}]}, headers=ctx["admin"]))
    _ok(client.post(f"/deliveries/{dlv['id']}/ready", headers=ctx["admin"]))
    loaded = _ok(client.post(f"/deliveries/{dlv['id']}/load", headers=ctx["admin"]))
    return order, loaded


def _force_cancel(order_id):
    """Recreates the legacy state: the order was cancelled before the API refused it."""
    db = SessionLocal()
    try:
        db.get(SalesOrder, order_id).status = "cancelled"
        db.commit()
    finally:
        db.close()


def test_return_flow_for_loaded_delivery_under_cancelled_order():
    ctx = _setup()
    start = _on_hand(ctx)
    order, dlv = _loaded_delivery(ctx)
    did = dlv["id"]
    assert _on_hand(ctx) == start - 10  # goods left the warehouse at load

    # Today the API refuses to cancel this order, and points at the return flow.
    r = _call("PATCH", f"/orders/{order['id']}/cancel", ctx["admin"], "0. cancel order (today)", json={"reason": "x"})
    assert r.status_code == 400 and "return flow" in r.json()["detail"]
    _force_cancel(order["id"])
    STEPS.append("   (legacy state recreated: order.status = cancelled, delivery loaded)")

    # 1. A loaded delivery cannot be cancelled: the goods are on the vehicle.
    r = _call("PATCH", f"/deliveries/by-id/{did}", ctx["admin"], "1. cancel delivery", json={"status": "cancelled"})
    assert r.status_code in (400, 409)
    # 2. Dispatch it (loaded -> in_transit), the only move a loaded delivery has.
    r = _call("PATCH", f"/deliveries/by-id/{did}", ctx["admin"], "2. dispatch", json={"status": "in_transit"})
    assert r.status_code == 200 and r.json()["status"] == "in_transit"
    # 3. Record a FAILED attempt: nothing handed over, goods stay on the vehicle.
    r = _call("POST", f"/deliveries/{did}/confirm", ctx["dp"], "3. confirm failed",
              json={"failed": True, "failure_reason": "Order cancelled - return goods to warehouse", "items": []})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "returned"  # public name of the stored `failed` status
    assert _on_hand(ctx) == start - 10  # still on the vehicle, not back yet
    db = SessionLocal()
    try:
        o = db.get(SalesOrder, order["id"])
        assert (o.status, o.fulfilment_status) == ("cancelled", "failed")  # order stays cancelled
    finally:
        db.close()
    # 4. End-of-day return of the vehicle loading puts the stock back in the warehouse.
    r = _call("GET", f"/vehicle-stock/current/{ctx['dp_id']}", ctx["admin"], "4a. partner's active loading")
    loading_id = _ok(r)["id"]
    r = _call("POST", f"/vehicle-stock/{loading_id}/end-of-day", ctx["admin"], "4b. end-of-day return",
              json={"items": [{"product_id": ctx["prod"], "variant_id": None, "returned_qty": 10}]})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "closed"
    assert _on_hand(ctx) == start  # back in the warehouse
    STEPS.append(f"   warehouse on_hand: {start} -> {start - 10} (load) -> {start} (end-of-day return)")
    # Repeating the failed confirm is accepted as a no-op: still returned, stock unchanged.
    r = _call("POST", f"/deliveries/{did}/confirm", ctx["dp"], "5. confirm failed again",
              json={"failed": True, "failure_reason": "again", "items": []})
    assert r.status_code == 200 and r.json()["status"] == "returned" and _on_hand(ctx) == start


def test_delivering_under_a_cancelled_order_silently_reinstates_it():
    """Documents a risk for category (A): confirming a DELIVERED outcome on a delivery
    whose order is cancelled moves the order to completed without any review."""
    ctx = _setup()
    order, dlv = _loaded_delivery(ctx, qty=4)
    _force_cancel(order["id"])
    _ok(client.patch(f"/deliveries/by-id/{dlv['id']}", json={"status": "in_transit"}, headers=ctx["admin"]))
    r = _call("POST", f"/deliveries/{dlv['id']}/confirm", ctx["dp"], "R. confirm delivered (cancelled order)",
              json={"items": [{"delivery_item_id": dlv["items"][0]["id"], "delivered_quantity": 4}]})
    assert r.status_code == 200
    db = SessionLocal()
    try:
        assert db.get(SalesOrder, order["id"]).status == "completed"
    finally:
        db.close()


def test_print_steps():
    print("\n" + "\n".join(STEPS))
