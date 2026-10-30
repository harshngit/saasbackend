"""Delivery Day-End: open -> submitted -> approved | rejected (-> resubmitted).

Run: pytest tests/test_delivery_day_end.py   (uses DATABASE_URL / .env)
"""

import os
import sys
import uuid
from datetime import datetime, timezone

os.environ.setdefault("TESTING", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from app.core.database import Base, SessionLocal, auto_add_missing_columns, engine
from app.main import app
from app.models import DeliveryCollection, Organization, Plan

Base.metadata.create_all(bind=engine)
auto_add_missing_columns()
client = TestClient(app)


def _register(label: str) -> dict:
    r = client.post("/auth/register", json={
        "organization_name": f"{label} {uuid.uuid4().hex[:6]}", "admin_name": f"Admin {label}",
        "email": f"admin_{uuid.uuid4().hex[:10]}@example.com", "password": "Password123!", "role": "admin",
    })
    assert r.status_code == 201, r.text
    auth = {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}
    me = client.get("/auth/me", headers=auth).json()
    with SessionLocal() as db:
        plan = db.query(Plan).filter(Plan.name == "Enterprise").first()
        assert plan is not None, "Enterprise plan missing from the test database"
        db.query(Organization).filter(Organization.id == me["organization_id"]).update({"plan_id": plan.id})
        db.commit()
    return {"auth": auth, "org_id": me["organization_id"], "user_id": me["id"]}


def _staff(auth: dict, role: str) -> dict:
    email = f"{role}_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/users", json={"name": f"Driver {uuid.uuid4().hex[:4]}", "email": email,
                                    "password": "Password123!", "role": role}, headers=auth)
    assert r.status_code == 201, r.text
    login = client.post("/auth/login", json={"email": email, "password": "Password123!"})
    assert login.status_code == 200, login.text
    return {"id": r.json()["id"], "auth": {"Authorization": f"Bearer {login.json()['tokens']['access_token']}"}}


def _delivery(env: dict, partner: dict, finish: str | None) -> str:
    auth = env["auth"]
    r = client.post("/orders", json={"customer_id": env["customer"], "warehouse_id": env["wh"],
                                     "items": [{"product_id": env["product"], "quantity": 5, "unit_price": 100.0}]},
                    headers=auth)
    assert r.status_code == 201, r.text
    oid = r.json()["id"]
    r = client.post(f"/orders/{oid}/confirm", headers=auth)
    assert r.status_code == 200, r.text
    item_id = r.json()["items"][0]["id"]
    r = client.post("/deliveries", json={"order_id": oid, "delivery_partner_id": partner["id"],
                                         "vehicle_id": partner["vehicle"],
                                         "items": [{"order_item_id": item_id, "planned_quantity": 5}]}, headers=auth)
    assert r.status_code == 201, r.text
    d = r.json()
    if finish is None:
        return d["id"]
    did, line = d["id"], d["items"][0]["id"]
    assert client.post(f"/deliveries/{did}/accept", headers=partner["auth"]).status_code == 200
    assert client.post(f"/deliveries/{did}/pick", json={"items": [{"delivery_item_id": line, "picked_quantity": 5}]},
                       headers=auth).status_code == 200
    assert client.post(f"/deliveries/{did}/ready", headers=auth).status_code == 200
    assert client.post(f"/deliveries/{did}/load", json={}, headers=auth).status_code == 200
    assert client.patch(f"/deliveries/by-id/{did}", json={"status": "in_transit"}, headers=auth).status_code == 200
    if finish == "delivered":
        body = {"items": [{"delivery_item_id": line, "delivered_quantity": 5}]}
    else:
        body = {"items": [], "failed": True, "failure_reason": "Customer not available"}
    r = client.post(f"/deliveries/{did}/confirm", json=body, headers=auth)
    assert r.status_code == 200, r.text
    return did


def _collect(partner: dict, delivery_id: str, amount: float, mode: str):
    return client.post(f"/deliveries/{delivery_id}/collections", json={"amount": amount, "payment_mode": mode},
                       headers=partner["auth"])


@pytest.fixture(scope="module")
def env():
    o = _register("DayEnd Org")
    auth = o["auth"]
    wh = client.post("/warehouses", json={"name": "Main", "code": f"WH-{uuid.uuid4().hex[:6]}"}, headers=auth).json()["id"]
    product = client.post("/products", json={
        "name": "Can", "sku": f"C-{uuid.uuid4().hex[:6]}", "price": 100.0, "tax_rate": 0.0, "uom": "unit",
        "pricing": {"purchase_price": 50.0, "selling_price": 100.0, "currency": "INR"},
    }, headers=auth).json()["id"]
    assert client.post(f"/warehouses/{wh}/stock/adjust", json={"product_id": product, "quantity": 500},
                       headers=auth).status_code == 200
    customer = client.post("/customers", json={"name": "Shop", "phone": "9876500000", "delivery_address": "x",
                                               "billing_address": "x"}, headers=auth).json()["id"]
    o.update(wh=wh, product=product, customer=customer)
    partners = []
    for _ in range(2):
        p = _staff(auth, "delivery_partner")
        p["vehicle"] = client.post("/vehicles", json={"vehicle_number": f"MH{uuid.uuid4().hex[:6].upper()}",
                                                      "vehicle_type": "Van"}, headers=auth).json()["id"]
        partners.append(p)
    o["partner"], o["partner2"] = partners
    p = o["partner"]
    o["delivered"] = _delivery(o, p, "delivered")
    o["returned"] = _delivery(o, p, "failed")
    o["pending"] = _delivery(o, p, None)
    for amount, mode in ((200, "cash"), (150, "bank_transfer"), (100, "upi")):
        r = _collect(p, o["delivered"], amount, mode)
        assert r.status_code == 201, r.text
    r = _collect(p, o["pending"], 50, "cheque")
    assert r.status_code == 201, r.text
    return o


def _line(view: dict, method: str) -> dict:
    return next(x for x in view["collections"]["breakdown"] if x["method"] == method)


def test_partner_sees_open_day_with_counts_and_breakdown(env):
    r = client.get("/delivery-day-ends/current", headers=env["partner"]["auth"])
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["status"] == "open" and v["id"] is None and v["delivery_partner_id"] == env["partner"]["id"]
    assert v["deliveries"] == {"assigned_count": 3, "delivered_count": 1, "partially_delivered_count": 0,
                               "returned_count": 1, "pending_count": 1}
    assert _line(v, "cash")["system_expected"] == 200
    assert _line(v, "bank")["system_expected"] == 150
    assert _line(v, "upi")["system_expected"] == 100
    assert _line(v, "cheque")["system_expected"] == 50
    assert v["collections"]["expected_total"] == 500
    assert v["collections"]["collection_count"] == 4


def test_submit_review_cycle(env):
    p, admin = env["partner"], env["auth"]
    handed = {"cash": 190, "bank": 150, "upi": 100, "cheque": 50}
    r = client.post("/delivery-day-ends/submit", json={"handed_over": handed}, headers=p["auth"])
    assert r.status_code == 400                       # variance needs an explanation
    r = client.post("/delivery-day-ends/submit", json={"handed_over": handed, "notes": "Rs 10 change given"},
                    headers=p["auth"])
    assert r.status_code == 200, r.text
    v = r.json()
    day_id = v["id"]
    assert v["status"] == "submitted" and v["submission_count"] == 1
    assert _line(v, "cash") == {"method": "cash", "system_expected": 200.0, "actual_handed_over": 190.0, "variance": -10.0}
    assert v["collections"]["variance_total"] == -10.0 and v["collections"]["handed_over_total"] == 490.0

    # Submitted: cannot submit again, cannot self-approve, day takes no new collections.
    assert client.post("/delivery-day-ends/submit", json={"handed_over": handed, "notes": "x"},
                       headers=p["auth"]).status_code == 409
    assert client.post(f"/delivery-day-ends/{day_id}/approve", json={}, headers=p["auth"]).status_code == 403
    late = _collect(p, env["pending"], 10, "cash")
    assert late.status_code == 409 and late.json()["detail"]["code"] == "DAY_END_CLOSED"

    # Admin rejects (reason required), partner corrects and resubmits.
    assert client.post(f"/delivery-day-ends/{day_id}/reject", json={"reason": ""}, headers=admin).status_code == 422
    r = client.post(f"/delivery-day-ends/{day_id}/reject", json={"reason": "Count cash again"}, headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rejected" and r.json()["rejection_reason"] == "Count cash again"
    r = client.post("/delivery-day-ends/submit", json={"handed_over": {**handed, "cash": 200}}, headers=p["auth"])
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "submitted" and r.json()["submission_count"] == 2
    assert r.json()["collections"]["variance_total"] == 0.0 and r.json()["id"] == day_id

    # Admin approves once; approval is final and stable.
    r = client.post(f"/delivery-day-ends/{day_id}/approve", json={"note": "ok"}, headers=admin)
    assert r.status_code == 200, r.text
    approved = r.json()
    assert approved["status"] == "approved" and approved["reviewed_by_id"] == env["user_id"]
    assert client.post(f"/delivery-day-ends/{day_id}/approve", json={}, headers=admin).status_code == 409
    assert client.post(f"/delivery-day-ends/{day_id}/reject", json={"reason": "x"}, headers=admin).status_code == 400
    again = client.get(f"/delivery-day-ends/{day_id}", headers=admin).json()
    assert again["status"] == "approved" and again["collections"] == approved["collections"]
    assert [h["action"] for h in again["history"]] == ["submitted", "rejected", "submitted", "approved"]

    # Its collections are frozen: no void.
    coll_id = approved["collections"]["collection_ids"][0]
    assert client.post(f"/deliveries/collections/{coll_id}/void", json={}, headers=admin).status_code == 409


def test_collections_untouched_and_traceable(env):
    with SessionLocal() as db:
        rows = db.query(DeliveryCollection).filter(DeliveryCollection.delivery_partner_id == env["partner"]["id"]).all()
        assert sorted(float(r.amount) for r in rows) == [50.0, 100.0, 150.0, 200.0]
        assert {r.reconciliation_status for r in rows} == {"recorded"}
        ids = sorted(r.id for r in rows)
    lst = client.get("/delivery-day-ends", params={"status": "approved"}, headers=env["auth"]).json()
    day = next(d for d in lst["items"] if d["delivery_partner_id"] == env["partner"]["id"])
    assert sorted(day["collections"]["collection_ids"]) == ids


def test_no_cross_user_or_cross_org_access(env):
    p2 = env["partner2"]
    day = client.get("/delivery-day-ends/current", headers=env["partner"]["auth"]).json()
    assert client.get(f"/delivery-day-ends/{day['id']}", headers=p2["auth"]).status_code == 404
    assert client.get("/delivery-day-ends/current", params={"delivery_partner_id": env["partner"]["id"]},
                      headers=p2["auth"]).status_code == 404
    mine = client.get("/delivery-day-ends", headers=p2["auth"]).json()
    assert all(d["delivery_partner_id"] == p2["id"] for d in mine["items"])
    # Partner 2's own day is separate and empty.
    v = client.get("/delivery-day-ends/current", headers=p2["auth"]).json()
    assert v["collections"]["expected_total"] == 0 and v["deliveries"]["assigned_count"] == 0

    other = _register("DayEnd Other")
    assert client.get(f"/delivery-day-ends/{day['id']}", headers=other["auth"]).status_code == 404
    assert client.post(f"/delivery-day-ends/{day['id']}/approve", json={}, headers=other["auth"]).status_code == 404
    assert client.get("/delivery-day-ends/current", params={"delivery_partner_id": env["partner"]["id"]},
                      headers=other["auth"]).status_code == 404


def test_approval_refused_when_collections_changed_after_submit(env):
    p2 = env["partner2"]
    did = _delivery(env, p2, "delivered")
    assert _collect(p2, did, 120, "cash").status_code == 201
    r = client.post("/delivery-day-ends/submit", json={"handed_over": {"cash": 120}}, headers=p2["auth"])
    assert r.status_code == 200, r.text
    day_id = r.json()["id"]
    with SessionLocal() as db:   # a write that bypassed the API lock
        db.add(DeliveryCollection(organization_id=env["org_id"], delivery_id=did, delivery_partner_id=p2["id"],
                                  amount=5, payment_mode="cash", collected_at=datetime.now(timezone.utc)))
        db.commit()
    r = client.post(f"/delivery-day-ends/{day_id}/approve", json={}, headers=env["auth"])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "DAY_END_COLLECTIONS_CHANGED"
    assert client.get(f"/delivery-day-ends/{day_id}", headers=env["auth"]).json()["collections"]["changed_since_submission"]
