"""GET /deliveries/collections?view=pending|collected (Flutter collection screen, 24 Nov).

* view=pending (To Collect): customers with collectable_amount = outstanding - collections not
  completed yet > 0; id = customer id. view=collected: records with collection_status
  `collected` (taken, not completed) or `completed` (reconciled); voided in neither.
* Each view filters first, then computes total / pagination / summary over every matching row.
* The collect rule is unchanged: a collection above collectable_amount is refused (409).
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
                            primary_contact_person="John", opening_balance=1000.0, outstanding_balance=1000.0,
                            maps_latitude=18.52, maps_longitude=73.85),
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
    # Old fields as before; display names and collection_status added (Nov 25).
    assert (body["items"][0]["customer_name"], body["items"][0]["collection_status"]) == ("ABC Traders", "collected")
    assert client.get("/deliveries/collections", headers=f["admin"], params={"view": "bogus"}).status_code == 400


def test_to_collect_collected_completed():
    f = _firm()
    other = _firm()
    body = _view(f["a"], "pending")
    assert {i["customer_id"] for i in body["items"]} == {f["abc"], f["bee"]}       # no paid-up / inactive
    abc = next(i for i in body["items"] if i["customer_id"] == f["abc"])
    # 1 + 5: no collection -> collectable = outstanding; name / contact / image / location.
    assert abc == {"id": f["abc"], "customer_id": f["abc"], "customer_code": abc["customer_code"],
                   "customer_name": "ABC Traders", "business_name": None, "contact_name": "John",
                   "phone": "9999999999", "address": None, "profile_photo_url": None,
                   "latitude": 18.52, "longitude": 73.85,
                   "outstanding_amount": 1000.0, "pending_collection_amount": 0.0, "collectable_amount": 1000.0,
                   "collection_status": None, "status": "to_collect"}
    assert body["summary"]["total_pending"] == 2 and body["summary"]["total_pending_amount"] == 1500.0
    assert body["pagination"] == {"page": 1, "page_size": 100, "total": 2, "total_pages": 1}
    assert other["abc"] not in _pending_ids(f["a"])                                 # 17: firm isolation

    # 2 + 6: collect 400 of 1000 with the id the view gave -> still To Collect, 600 left.
    r = _collect(f["a"], abc["id"], 400.0, mode="upi")
    assert r.status_code == 201, r.text
    coll_id = r.json()["id"]
    for who in ("a", "b", "admin"):
        abc = next(i for i in _view(f[who], "pending")["items"] if i["customer_id"] == f["abc"])
        assert (abc["outstanding_amount"], abc["pending_collection_amount"], abc["collectable_amount"]) == \
               (1000.0, 400.0, 600.0)
    s = _view(f["a"], "pending")["summary"]
    assert (s["total_pending_amount"], s["total_outstanding_amount"], s["total_pending_collection_amount"]) == \
           (1100.0, 1500.0, 400.0)

    # 7: the record is in Collected as `collected`, with the customer's details.
    coll = _view(f["a"], "collected")
    assert coll["total"] == 1
    row = coll["items"][0]
    assert (row["collection_id"], row["customer_name"], row["amount_collected"], row["collection_status"]) == \
           (coll_id, "ABC Traders", 400.0, "collected")
    assert row["reconciliation_status"] == "recorded" and row["status"] == "awaiting_reconciliation"   # kept
    assert (row["contact_name"], row["phone"], row["latitude"], row["longitude"], row["order_number"]) == \
           ("John", "9999999999", 18.52, 73.85, None)
    assert row["collected_by_name"] == "staff" and row["delivery_number"] is None
    assert row["id"] == coll_id and row["customer_id"] == f["abc"]
    s = coll["summary"]
    assert (s["total_collected"], s["collected_amount"], s["completed_amount"], s["upi_total"], s["cash_total"]) == \
           (400.0, 400.0, 0.0, 400.0, 0.0)
    assert _view(f["b"], "collected")["total"] == 0                                # 18: partner B sees only own
    assert _view(other["admin"], "collected")["total"] == 0

    # 16: more than what is left -> existing 409; 3: the rest taken -> not To Collect.
    r = _collect(f["b"], f["abc"], 700.0)
    assert r.status_code == 409 and "awaiting admin approval; at most 600.00" in r.json()["detail"]
    r = _collect(f["b"], f["abc"], 600.0, mode="bank_transfer")
    assert r.status_code == 201, r.text
    second_id = r.json()["id"]
    assert f["abc"] not in _pending_ids(f["a"])

    # 8: reconciled -> `completed`; outstanding drops, collectable follows.
    assert client.post(f"/deliveries/collections/{coll_id}/reconcile", headers=f["admin"]).status_code == 200
    by_id = {i["id"]: i["collection_status"] for i in _view(f["admin"], "collected")["items"]}
    assert by_id == {coll_id: "completed", second_id: "collected"}
    assert f["abc"] not in _pending_ids(f["a"])                    # 600 outstanding, 600 in hand
    assert client.post(f"/deliveries/collections/{second_id}/reconcile", headers=f["admin"]).status_code == 200
    s = _view(f["admin"], "collected")["summary"]
    assert (s["completed_amount"], s["completed_count"], s["collected_amount"], s["bank_transfer_total"]) == \
           (1000.0, 2, 0.0, 600.0)
    assert f["abc"] not in _pending_ids(f["a"])                    # fully paid

    # 4 + 9: a voided collection does not reduce collectable and is not in Collected.
    r = _collect(f["b"], f["bee"], 200.0)
    assert r.status_code == 201
    bee = next(i for i in _view(f["a"], "pending")["items"] if i["customer_id"] == f["bee"])
    assert bee["collectable_amount"] == 300.0
    assert client.post(f"/deliveries/collections/{r.json()['id']}/void", headers=f["admin"]).status_code == 200
    bee = next(i for i in _view(f["a"], "pending")["items"] if i["customer_id"] == f["bee"])
    assert (bee["pending_collection_amount"], bee["collectable_amount"]) == (0.0, 500.0)
    assert all(i["collection_status"] != "voided" for i in _view(f["admin"], "collected")["items"])
    assert _view(f["admin"], "collected", status="voided")["total"] == 0
    # The app's status names also work as a status filter (plain list and view).
    for name, n in (("collected", 0), ("completed", 2), ("pending_approval", 0), ("approved", 2)):
        r = client.get("/deliveries/collections", headers=f["admin"], params={"status": name})
        assert r.status_code == 200 and r.json()["total"] == n, name
    assert _view(f["admin"], "collected", status="completed")["total"] == 2


def test_collected_row_shows_order_delivery_and_allocations_and_posts_once():
    """10 + 13-15: order / delivery numbers and allocations on the row; completing it writes
    exactly one payment, takes it off outstanding and pays the invoice."""
    from datetime import datetime, timezone
    from app.models import CustomerPayment, Delivery, Invoice, SalesOrder
    f = _firm()
    with SessionLocal() as db:
        so = SalesOrder(organization_id=f["org"], customer_id=f["abc"], order_number=f"SO-{uuid.uuid4().hex[:6]}",
                        total=1000.0, status="confirmed")
        db.add(so)
        db.flush()
        inv = Invoice(organization_id=f["org"], customer_id=f["abc"], order_id=so.id,
                      invoice_number=f"INV-{uuid.uuid4().hex[:6]}", invoice_date=datetime.now(timezone.utc),
                      total=1000.0, amount_paid=0.0, status="unpaid", payment_status="Unpaid")
        dlv = Delivery(organization_id=f["org"], customer_id=f["abc"], sales_order_id=so.id,
                       delivery_note_number=f"DO-{uuid.uuid4().hex[:6]}", status="pending")
        db.add_all([inv, dlv])
        db.commit()
        ids = {"so": so.id, "so_no": so.order_number, "inv": inv.id, "dlv": dlv.id, "dlv_no": dlv.delivery_note_number}
    r = client.post("/deliveries/collections/customer", headers=f["admin"], json={
        "customer_id": f["abc"], "amount": 250.0, "payment_method": "cash", "order_id": ids["so"],
        "delivery_id": ids["dlv"], "allocations": [{"invoice_id": ids["inv"], "amount": 250.0}]})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    row = _view(f["admin"], "collected")["items"][0]
    assert (row["order_id"], row["order_number"], row["delivery_id"], row["delivery_number"]) == \
           (ids["so"], ids["so_no"], ids["dlv"], ids["dlv_no"])
    assert [(a["invoice_id"], a["amount"]) for a in row["allocations"]] == [(ids["inv"], 250.0)]
    assert row["collected_by_name"] == "admin"
    assert client.post(f"/deliveries/collections/{cid}/reconcile", headers=f["admin"]).status_code == 200
    again = client.post(f"/deliveries/collections/{cid}/reconcile", headers=f["admin"])
    assert again.status_code in (200, 400, 409), again.text
    with SessionLocal() as db:
        assert db.query(CustomerPayment).filter(CustomerPayment.customer_id == f["abc"]).count() == 1
        assert round(db.get(Customer, f["abc"]).outstanding_balance, 2) == 750.0
        assert round(db.get(Invoice, ids["inv"]).amount_paid, 2) == 250.0
    abc = next(i for i in _view(f["a"], "pending")["items"] if i["customer_id"] == f["abc"])
    assert (abc["outstanding_amount"], abc["pending_collection_amount"], abc["collectable_amount"]) == \
           (750.0, 0.0, 750.0)
    assert _view(f["admin"], "collected")["items"][0]["collection_status"] == "completed"


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
    pages = [client.get("/deliveries/collections", headers=f["a"],
                        params={"view": "collected", "page": p, "page_size": 2}).json()["items"] for p in (1, 2)]
    got = [i["collection_id"] for pg in pages for i in pg]
    assert len(got) == 3 and len(set(got)) == 3                   # 12: no duplicates, none missing
    assert _view(f["a"], "pending", search="ABC")["total"] == 1
    assert _view(f["a"], "pending", customer_id=f["bee"])["total"] == 1


def test_pending_view_needs_customers_view():
    f = _firm()
    r = client.get("/deliveries/collections", headers=f["restricted"], params={"view": "pending"})
    assert r.status_code == 403
    assert client.get("/deliveries/collections", headers=f["restricted"], params={"view": "collected"}).status_code == 200
