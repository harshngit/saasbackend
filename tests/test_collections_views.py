"""GET /deliveries/collections?view=pending|collected (Flutter collection screen, 24 Nov).

* view=pending: customers a new collection can be taken from (active, outstanding > 0, nothing
  awaiting approval). view=collected: collections awaiting approval (pending_approval) or
  approved; voided in neither. An awaiting-approval collection is in exactly one tab.
* Each view filters first, then computes total / pagination / summary over every matching row.
* The collect rule is unchanged: a collection above what is left after those awaiting approval
  is refused (409); a further one within it (another order) is allowed.
* Without `view` the endpoint is unchanged. Two firms.
"""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Customer, Organization, Role, User  # noqa: E402
from app.models.enums import UserRole  # noqa: E402
from app.services import entitlement_service, role_service  # noqa: E402

client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def _schema():
    Base.metadata.create_all(engine)


@pytest.fixture(autouse=True)
def _no_plan_gates(monkeypatch):
    monkeypatch.setattr(entitlement_service, "check_entitlement", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "check_limit", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "has_entitlement", lambda *a, **k: True)


def _user(db, org_id, legacy, system_role, role_id=None):
    u = User(email=f"{system_role}.{uuid.uuid4().hex[:8]}@example.com", name=system_role, organization_id=org_id,
             system_role=system_role, role=legacy, role_id=role_id, password_hash=hash_password("Password123!"),
             is_active=True)
    db.add(u)
    db.flush()
    return {"Authorization": f"Bearer {create_access_token(u.id, system_role, org_id)}"}


def _firm(extra_customers=0):
    with SessionLocal() as db:
        org = Organization(name=f"CV {uuid.uuid4().hex[:6]}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}",
                           currency="INR")
        db.add(org)
        db.flush()
        role_service.seed_default_roles(db, org.id)
        roles = {r.name: r for r in db.query(Role).filter(Role.organization_id == org.id)}
        no_customers = Role(organization_id=org.id, name=f"No customers {uuid.uuid4().hex[:4]}",
                            permissions={"deliveries": {"view": True}})
        db.add(no_customers)
        db.flush()
        f = {
            "org": org.id,
            "admin": _user(db, org.id, UserRole.ADMIN, "admin"),
            "a": _user(db, org.id, UserRole.DELIVERY_PARTNER, "staff", roles["Delivery Partner"].id),
            "b": _user(db, org.id, UserRole.DELIVERY_PARTNER, "staff", roles["Delivery Partner"].id),
            "restricted": _user(db, org.id, UserRole.ACCOUNTANT, "staff", no_customers.id),
        }
        rows = {
            "abc": Customer(organization_id=org.id, name="ABC Traders", phone="9999999999",
                            primary_contact_person="John", opening_balance=1000.0, outstanding_balance=1000.0),
            "bee": Customer(organization_id=org.id, name="Bee Stores", opening_balance=500.0, outstanding_balance=500.0),
            "paid": Customer(organization_id=org.id, name="Paid Up"),
            "inactive": Customer(organization_id=org.id, name="Closed Shop", is_active=False,
                                 opening_balance=300.0, outstanding_balance=300.0),
        }
        for i in range(extra_customers):
            rows[f"x{i}"] = Customer(organization_id=org.id, name=f"Z Extra {i:02d}", opening_balance=10.0,
                                     outstanding_balance=10.0)
        db.add_all(rows.values())
        db.commit()
        f.update({k: c.id for k, c in rows.items()})
        return f


def _collect(h, customer_id, amount, mode="cash"):
    return client.post("/deliveries/collections/customer", headers=h,
                       json={"customer_id": customer_id, "amount": amount, "payment_method": mode})


def _view(h, view, **params):
    r = client.get("/deliveries/collections", headers=h, params={"view": view, "page_size": 100, **params})
    assert r.status_code == 200, r.text
    return r.json()


def _pending_ids(h, **params):
    return {i["customer_id"] for i in _view(h, "pending", **params)["items"]}


def test_default_list_is_unchanged():
    f = _firm()
    assert _collect(f["a"], f["abc"], 100.0).status_code == 201
    body = client.get("/deliveries/collections", headers=f["admin"]).json()
    assert set(body) == {"items", "total", "page", "page_size", "total_pages"}
    assert body["total"] == 1 and body["items"][0]["reconciliation_status"] == "recorded"
    assert "collection_status" not in body["items"][0] and "customer_name" not in body["items"][0]
    assert client.get("/deliveries/collections", headers=f["admin"], params={"view": "bogus"}).status_code == 400


def test_awaiting_approval_is_in_exactly_one_tab():
    f = _firm()
    other = _firm()
    body = _view(f["a"], "pending")
    assert {i["customer_id"] for i in body["items"]} == {f["abc"], f["bee"]}       # no paid-up / inactive
    abc = next(i for i in body["items"] if i["customer_id"] == f["abc"])
    assert abc == {"customer_id": f["abc"], "customer_code": abc["customer_code"], "customer_name": "ABC Traders",
                   "business_name": None, "contact_name": "John", "phone": "9999999999", "address": None,
                   "outstanding_amount": 1000.0, "collectable_amount": 1000.0, "collection_status": None,
                   "status": "pending"}
    assert body["summary"]["total_pending"] == 2 and body["summary"]["total_pending_amount"] == 1500.0
    assert body["pagination"] == {"page": 1, "page_size": 100, "total": 2, "total_pages": 1}
    assert other["abc"] not in _pending_ids(f["a"])                                 # firm isolation

    r = _collect(f["a"], f["abc"], 300.0, mode="upi")
    assert r.status_code == 201, r.text
    coll_id = r.json()["id"]
    # Pending tab: ABC is gone for every partner (and the admin). Collected tab: the row.
    assert _pending_ids(f["a"]) == {f["bee"]} and _pending_ids(f["b"]) == {f["bee"]}
    assert _pending_ids(f["admin"]) == {f["bee"]}
    coll = _view(f["a"], "collected")
    assert coll["total"] == 1
    row = coll["items"][0]
    assert (row["collection_id"], row["customer_name"], row["amount_collected"], row["collection_status"]) == \
           (coll_id, "ABC Traders", 300.0, "pending_approval")
    assert row["reconciliation_status"] == "recorded"                              # old field kept
    s = coll["summary"]
    assert (s["total_collected"], s["awaiting_approval_amount"], s["awaiting_approval_count"], s["upi_total"],
            s["cash_total"]) == (300.0, 300.0, 1, 300.0, 0.0)
    assert _view(f["b"], "collected")["total"] == 0                                # partner B sees only own
    assert _view(other["admin"], "collected")["total"] == 0

    # Collect rule unchanged (business decision): more than what is left is refused (existing
    # 409 text); a second collection within the rest (e.g. another order) is still allowed and
    # ABC stays out of Pending while anything is awaiting approval.
    r = _collect(f["b"], f["abc"], 800.0)
    assert r.status_code == 409 and "awaiting admin approval; at most 700.00" in r.json()["detail"]
    r = _collect(f["b"], f["abc"], 200.0, mode="bank_transfer")
    assert r.status_code == 201, r.text
    second_id = r.json()["id"]
    assert f["abc"] not in _pending_ids(f["a"])
    assert _view(f["admin"], "collected")["summary"]["awaiting_approval_amount"] == 500.0

    # Both approved: they stay in Collected (approved); ABC returns to Pending with the rest.
    for cid in (coll_id, second_id):
        assert client.post(f"/deliveries/collections/{cid}/reconcile", headers=f["admin"]).status_code == 200
    assert {i["collection_status"] for i in _view(f["a"], "collected")["items"]} == {"approved"}
    abc = next(i for i in _view(f["a"], "pending")["items"] if i["customer_id"] == f["abc"])
    assert abc["outstanding_amount"] == 500.0 and abc["collectable_amount"] == 500.0
    assert _collect(f["a"], f["abc"], 50.0).status_code == 201
    s = _view(f["admin"], "collected")["summary"]
    assert (s["approved_amount"], s["awaiting_approval_amount"], s["bank_transfer_total"], s["upi_total"],
            s["cash_total"], s["collected_count"]) == (500.0, 50.0, 200.0, 300.0, 50.0, 3)

    # Voided: in neither view; the customer is collectable again.
    r = _collect(f["b"], f["bee"], 500.0)
    assert r.status_code == 201
    assert f["bee"] not in _pending_ids(f["a"])
    assert client.post(f"/deliveries/collections/{r.json()['id']}/void", headers=f["admin"]).status_code == 200
    assert f["bee"] in _pending_ids(f["a"])
    assert all(i["collection_status"] != "voided" for i in _view(f["admin"], "collected")["items"])
    assert _view(f["admin"], "collected", status="voided")["total"] == 0
    # The canonical names also work as a status filter on the plain list.
    r = client.get("/deliveries/collections", headers=f["admin"], params={"status": "pending_approval"})
    assert r.status_code == 200 and r.json()["total"] == 1


def test_pagination_and_summary_are_per_view_and_filter_first():
    f = _firm(extra_customers=11)                                # 13 pending customers
    for cid in (f["x0"], f["x1"], f["x2"]):
        assert _collect(f["a"], cid, 10.0).status_code == 201    # 3 move to Collected
    seen, totals = [], set()
    for page in (1, 2, 3):
        body = client.get("/deliveries/collections", headers=f["a"],
                          params={"view": "pending", "page": page, "page_size": 4}).json()
        seen += [i["customer_id"] for i in body["items"]]
        totals.add((body["total"], body["pagination"]["total_pages"], body["summary"]["total_pending"],
                    body["summary"]["total_pending_amount"]))
    assert len(seen) == 10 and len(set(seen)) == 10               # 4 + 4 + 2, no overlap, no gaps
    assert totals == {(10, 3, 10, 1580.0)}                        # 1000 + 500 + 8 x 10, same on every page
    body = client.get("/deliveries/collections", headers=f["a"],
                      params={"view": "collected", "page": 2, "page_size": 2}).json()
    assert body["pagination"] == {"page": 2, "page_size": 2, "total": 3, "total_pages": 2}
    assert len(body["items"]) == 1 and body["summary"]["total_collected"] == 30.0   # whole set, not the page
    assert _view(f["a"], "pending", search="ABC")["total"] == 1
    assert _view(f["a"], "pending", customer_id=f["bee"])["total"] == 1


def test_pending_view_needs_customers_view():
    f = _firm()
    r = client.get("/deliveries/collections", headers=f["restricted"], params={"view": "pending"})
    assert r.status_code == 403
    assert client.get("/deliveries/collections", headers=f["restricted"], params={"view": "collected"}).status_code == 200
