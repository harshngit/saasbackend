"""GET /deliveries/collections (plain list, view=collected) and /collections/summary:
payment_mode filter, display names on every row, wider search, collected-by vs assigned
partner, completion metadata, and ?collection_status= as an alias of ?status=. Two firms.
"""

import os
import sys
import uuid
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event  # noqa: E402

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Customer, CustomerPayment, Delivery, DeliveryCollection, Organization, Role, SalesOrder, User,
)
from app.models.enums import UserRole  # noqa: E402
from app.services import entitlement_service, role_service  # noqa: E402

c = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def _schema():
    Base.metadata.create_all(engine)


@pytest.fixture(autouse=True)
def _no_plan_gates(monkeypatch):
    monkeypatch.setattr(entitlement_service, "check_entitlement", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "check_limit", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "has_entitlement", lambda *a, **k: True)


def _firm():
    """Admin, accountant, two delivery partners, a view-only user without Customers; two
    customers; an order + delivery assigned to partner A; six collections across modes
    and states, inserted directly (the collect / reconcile / void flows are tested elsewhere
    and below)."""
    tag = uuid.uuid4().hex[:5]
    with SessionLocal() as db:
        org = Organization(name=f"LF {tag}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}", currency="INR")
        db.add(org)
        db.flush()
        role_service.seed_default_roles(db, org.id)
        roles = {r.name: r for r in db.query(Role).filter(Role.organization_id == org.id)}
        viewer_role = Role(organization_id=org.id, name=f"Collections viewer {tag}",
                           permissions={"deliveries": {"view": True}})
        db.add(viewer_role)
        db.flush()
        h, ids = {}, {}
        for key, name, sr, legacy, rid in (
            ("admin", f"Admin {tag}", "admin", UserRole.ADMIN, None),
            ("acct", f"Asha Accountant {tag}", "staff", UserRole.ACCOUNTANT, roles["Accountant"].id),
            ("a", f"Ravi Rider {tag}", "staff", UserRole.DELIVERY_PARTNER, roles["Delivery Partner"].id),
            ("b", f"Bala Biker {tag}", "staff", UserRole.DELIVERY_PARTNER, roles["Delivery Partner"].id),
            ("viewer", f"Vinod Viewer {tag}", "staff", UserRole.ACCOUNTANT, viewer_role.id),
        ):
            u = User(email=f"{key}.{uuid.uuid4().hex[:8]}@example.com", name=name, organization_id=org.id,
                     system_role=sr, role=legacy, role_id=rid, password_hash=hash_password("Password123!"),
                     is_active=True)
            db.add(u)
            db.flush()
            h[key] = {"Authorization": f"Bearer {create_access_token(u.id, sr, org.id)}"}
            ids[key] = u.id
        ganesh = Customer(organization_id=org.id, name=f"Ganesh Stores {tag}", business_name=f"Ganesh Pvt {tag}",
                          primary_contact_person="Mr Ganesh", opening_balance=50000.0, outstanding_balance=50000.0)
        laxmi = Customer(organization_id=org.id, name=f"Laxmi Traders {tag}", opening_balance=50000.0,
                         outstanding_balance=50000.0)
        db.add_all([ganesh, laxmi])
        db.flush()
        so = SalesOrder(organization_id=org.id, customer_id=ganesh.id, order_number=f"SO-{tag}-77", total=900.0,
                        status="confirmed")
        db.add(so)
        db.flush()
        dlv = Delivery(organization_id=org.id, customer_id=ganesh.id, sales_order_id=so.id,
                       delivery_note_number=f"DO-{tag}-0049", delivery_partner_id=ids["a"], status="delivered")
        db.add(dlv)
        db.flush()
        t = datetime(2026, 11, 20, 6, 0, tzinfo=timezone.utc)
        rows = {}
        for i, (key, cust, who, amt, mode, st, extra) in enumerate((
            ("dlv_upi", ganesh, "a", 900.0, "upi", "recorded", {"delivery_id": dlv.id, "sales_order_id": so.id}),
            ("acct_cash", laxmi, "acct", 300.0, "cash", "recorded", {"reference": f"REF-{tag}-X1"}),
            ("b_cash", ganesh, "b", 120.0, "cash", "recorded", {}),
            ("b_cheque", laxmi, "b", 200.0, "cheque", "recorded", {}),
            ("void_cash", laxmi, "acct", 50.0, "cash", "voided", {}),
            ("neft", ganesh, "acct", 400.0, "neft", "recorded", {}),
        )):
            r = DeliveryCollection(organization_id=org.id, customer_id=cust.id, delivery_partner_id=ids[who],
                                   amount=amt, payment_mode=mode, reconciliation_status=st,
                                   collected_at=t.replace(hour=6 + i), **extra)
            db.add(r)
            db.flush()
            rows[key] = r.id
        db.commit()
        return {"org": org.id, "h": h, "ids": ids, "rows": rows, "tag": tag, "ganesh": ganesh.id, "laxmi": laxmi.id,
                "so": so.id, "so_no": so.order_number, "dlv": dlv.id, "dlv_no": dlv.delivery_note_number,
                "names": {"a": f"Ravi Rider {tag}", "b": f"Bala Biker {tag}", "acct": f"Asha Accountant {tag}",
                          "admin": f"Admin {tag}"}}


def _list(f, who="admin", path="/deliveries/collections", **params):
    r = c.get(path, headers=f["h"][who], params={"page_size": 100, **params})
    assert r.status_code == 200, r.text
    return r.json()


def _ids(body):
    return {i["id"] for i in body["items"]}


def _summary(f, **params):
    r = c.get("/deliveries/collections/summary", headers=f["h"]["admin"], params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_payment_mode_filter_pagination_and_summary():
    f, other = _firm(), _firm()
    R = f["rows"]
    assert _ids(_list(f, payment_mode="cash")) == {R["acct_cash"], R["b_cash"], R["void_cash"]}          # 1
    assert _ids(_list(f, payment_mode="UPI")) == {R["dlv_upi"]}                                            # 2
    assert _ids(_list(f, payment_mode="cash", status="collected")) == {R["acct_cash"], R["b_cash"]}        # 3
    assert _ids(_list(f, payment_mode="bank_transfer")) == {R["neft"]}            # bank group, as the summary
    assert _ids(_list(f, payment_mode="other")) == set()
    # 4: filter first, then total / pages / page.
    seen = []
    for page in (1, 2):
        body = c.get("/deliveries/collections", headers=f["h"]["admin"],
                     params={"payment_mode": "cash", "page": page, "page_size": 2}).json()
        assert (body["total"], body["total_pages"]) == (3, 2)
        seen += [i["id"] for i in body["items"]]
    assert len(seen) == 3 and set(seen) == {R["acct_cash"], R["b_cash"], R["void_cash"]}
    # 5: summaries follow the mode filter (plain summary and the view's summary).
    s = _summary(f, payment_mode="cash")
    assert (s["total"]["count"], s["total"]["amount"]) == (3, 470.0) and set(s["by_payment_mode"]) == {"cash"}
    v = _list(f, view="collected", payment_mode="cash")
    assert (v["total"], v["summary"]["total_collected"], v["summary"]["upi_total"]) == (2, 420.0, 0.0)
    theirs = _ids(_list(other, payment_mode="cash"))                           # other firm: only its own
    assert len(theirs) == 3 and not theirs & set(R.values())


def test_display_names_on_every_row_without_customers_permission():
    f = _firm()
    R, n = f["rows"], f["names"]
    for who in ("admin", "viewer"):                                             # 10: viewer has no Customers
        rows = {i["id"]: i for i in _list(f, who)["items"]}
        assert len(rows) == 6
        assert all(i["customer_name"] for i in rows.values())                              # 6
        d = rows[R["dlv_upi"]]                                                             # 8
        assert (d["delivery_number"], d["order_number"], d["order_id"], d["delivery_id"]) == \
               (f["dlv_no"], f["so_no"], f["so"], f["dlv"])
        assert (d["customer_name"], d["business_name"], d["contact_name"]) == \
               (f"Ganesh Stores {f['tag']}", f"Ganesh Pvt {f['tag']}", "Mr Ganesh")
        assert (d["collected_by_name"], d["delivery_partner_name"], d["assigned_delivery_partner_id"]) == \
               (n["a"], n["a"], f["ids"]["a"])
        g = rows[R["acct_cash"]]                                                           # 9 + section 4
        assert (g["delivery_number"], g["order_number"], g["delivery_id"], g["order_id"]) == (None,) * 4
        assert (g["collected_by_name"], g["collected_by_user_id"], g["delivery_partner_name"]) == \
               (n["acct"], f["ids"]["acct"], None)
        assert g["delivery_partner_id"] == f["ids"]["acct"]          # older field: unchanged meaning (collector)
        v = rows[R["void_cash"]]                                                           # 7
        assert (v["customer_name"], v["collected_by_name"], v["collection_status"]) == \
               (f"Laxmi Traders {f['tag']}", n["acct"], "voided")
    voided = _list(f, "viewer", status="voided")["items"]
    assert [(i["customer_name"], i["collected_by_name"]) for i in voided] == [(f"Laxmi Traders {f['tag']}", n["acct"])]
    one = c.get(f"/deliveries/collections/{R['dlv_upi']}", headers=f["h"]["viewer"]).json()
    assert (one["delivery_number"], one["collected_by_name"]) == (f["dlv_no"], n["a"])
    # A customer that is gone: null, not a made-up name.
    with SessionLocal() as db:
        db.get(DeliveryCollection, R["b_cheque"]).customer_id = None
        db.commit()
    gone = next(i for i in _list(f)["items"] if i["id"] == R["b_cheque"])
    assert gone["customer_name"] is None and gone["collected_by_name"] == n["b"]


def test_search_covers_customer_order_delivery_collector_reference():
    f = _firm()
    R, tag = f["rows"], f["tag"]
    assert _ids(_list(f, search=f"ganesh stores {tag}")) == {R["dlv_upi"], R["b_cash"], R["neft"]}       # 11
    assert _ids(_list(f, search=f"Ganesh Pvt {tag}")) == {R["dlv_upi"], R["b_cash"], R["neft"]}
    assert _ids(_list(f, search=f"DO-{tag}-0049")) == {R["dlv_upi"]}                                       # 12
    assert _ids(_list(f, search=f"so-{tag}-77")) == {R["dlv_upi"]}                                         # 13
    assert _ids(_list(f, search=f"Bala Biker {tag}")) == {R["b_cash"], R["b_cheque"]}                      # 14
    assert _ids(_list(f, search=f"REF-{tag}-X1")) == {R["acct_cash"]}                                      # 15
    assert _ids(_list(f, view="collected", search=f"DO-{tag}-0049")) == {R["dlv_upi"]}
    assert _summary(f, search=f"Bala Biker {tag}")["total"]["count"] == 2
    # 16: search + pagination
    pages = [c.get("/deliveries/collections", headers=f["h"]["admin"],
                   params={"search": f"Laxmi Traders {tag}", "page": p, "page_size": 2}).json() for p in (1, 2)]
    assert [(p["total"], p["total_pages"]) for p in pages] == [(3, 2), (3, 2)]
    got = [i["id"] for p in pages for i in p["items"]]
    assert len(got) == 3 and set(got) == {R["acct_cash"], R["b_cheque"], R["void_cash"]}
    other = _firm()
    assert _list(other, search=f"DO-{tag}-0049")["total"] == 0                                             # tenant
    # Partner B searching partner A's delivery: still only own rows.
    assert _list(f, "b", search=f"DO-{tag}-0049")["total"] == 0


def test_completion_metadata_and_one_payment():
    f = _firm()
    R, n = f["rows"], f["names"]
    row = next(i for i in _list(f, view="collected")["items"] if i["id"] == R["dlv_upi"])                  # 17
    assert (row["collected_by_name"], row["reconciled_by_name"], row["completed_at"], row["receipt_number"]) == \
           (n["a"], None, None, None)
    r = c.post(f"/deliveries/collections/{R['dlv_upi']}/reconcile", headers=f["h"]["acct"])
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        pays = db.query(CustomerPayment).filter(CustomerPayment.organization_id == f["org"]).all()
        assert len(pays) == 1                                                                              # 20
        pay = pays[0]
    for body in (_list(f, view="collected", status="completed"), _list(f, status="completed")):
        done = body["items"][0]
        assert body["total"] == 1 and done["id"] == R["dlv_upi"]
        assert (done["collection_status"], done["reconciled_by_user_id"], done["reconciled_by_name"]) == \
               ("completed", f["ids"]["acct"], n["acct"])                                                 # 18
        assert done["completed_at"] and done["completed_at"] == done["reconciled_at"]
        assert (done["customer_payment_id"], done["receipt_number"]) == (pay.id, pay.receipt_number)       # 19
        assert done["receipt_number"]
    one = c.get(f"/deliveries/collections/{R['dlv_upi']}", headers=f["h"]["admin"]).json()
    assert (one["reconciled_by_name"], one["receipt_number"]) == (n["acct"], pay.receipt_number)
    again = c.post(f"/deliveries/collections/{R['dlv_upi']}/reconcile", headers=f["h"]["acct"])
    assert again.status_code in (200, 400, 409)
    with SessionLocal() as db:
        assert db.query(CustomerPayment).filter(CustomerPayment.organization_id == f["org"]).count() == 1


def test_collection_status_alias():
    f = _firm()
    R = f["rows"]
    with SessionLocal() as db:
        db.get(DeliveryCollection, R["neft"]).reconciliation_status = "reconciled"
        db.commit()
    for path, view in (("/deliveries/collections", None), ("/deliveries/collections", "collected")):
        extra = {"view": view} if view else {}
        a = _list(f, path=path, status="completed", **extra)                                               # 21
        b = _list(f, path=path, collection_status="completed", **extra)                                    # 22
        assert a == b and _ids(a) == {R["neft"]}                                                           # 23
        assert _list(f, path=path, status="completed", collection_status="reconciled", **extra) == a       # same state
        r = c.get(path, headers=f["h"]["admin"], params={"status": "completed", "collection_status": "collected",
                                                          **extra})
        assert r.status_code == 400 and "conflict" in r.json()["detail"]                                   # 24
    assert _ids(_list(f, collection_status="voided")) == {R["void_cash"]}
    assert _summary(f, collection_status="completed") == _summary(f, status="completed")
    assert c.get("/deliveries/collections/summary", headers=f["h"]["admin"],
                 params={"status": "voided", "collection_status": "completed"}).status_code == 400
    assert c.get("/deliveries/collections", headers=f["h"]["admin"],
                 params={"collection_status": "bogus"}).status_code == 422


def test_page_of_25_query_count_is_constant():
    """Display names add a fixed number of queries, not one per row."""
    f = _firm()
    with SessionLocal() as db:
        for i in range(30):
            db.add(DeliveryCollection(organization_id=f["org"], customer_id=f["laxmi"] if i % 2 else f["ganesh"],
                                      delivery_partner_id=f["ids"]["a" if i % 3 else "b"], amount=5.0 + i,
                                      payment_mode="cash", delivery_id=f["dlv"] if i % 4 == 0 else None,
                                      sales_order_id=f["so"] if i % 4 == 0 else None))
        db.commit()
    counts = {}

    def count(*_a, **_k):
        counts["n"] += 1

    event.listen(engine, "before_cursor_execute", count)
    try:
        for size in (5, 25):
            counts["n"] = 0
            assert len(_list(f, page_size=size)["items"]) == size
            counts[size] = counts["n"]
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert counts[25] == counts[5], counts
