"""Collection QA blockers: collector identity on delivery-linked collections (and Day-End),
read permission on every collection view, and Confirm/Void per default role. Two firms.
"""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Customer, Delivery, Organization, Role, SalesOrder, User  # noqa: E402
from app.models.enums import UserRole  # noqa: E402
from app.services import collection_dates, day_end_service, entitlement_service, role_service  # noqa: E402

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
    tag = uuid.uuid4().hex[:5]
    with SessionLocal() as db:
        org = Organization(name=f"CP {tag}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}",
                           currency="INR", timezone="Asia/Kolkata")
        db.add(org)
        db.flush()
        role_service.seed_default_roles(db, org.id)
        db.flush()
        roles = {r.name: r for r in db.query(Role).filter(Role.organization_id == org.id)}
        zero = Role(organization_id=org.id, name=f"Nothing {tag}", permissions={})
        db.add(zero)
        db.flush()
        h, ids, names = {}, {}, {}
        for key, name, sr, legacy, rid in (
            ("admin", f"Rahul Sharma {tag}", "admin", UserRole.ADMIN, None),
            ("acct", f"Asha Accountant {tag}", "staff", UserRole.ACCOUNTANT, roles["Accountant"].id),
            ("rider", f"QA-TEST CmRiderOne {tag}", "staff", UserRole.DELIVERY_PARTNER, roles["Delivery Partner"].id),
            ("rider2", f"Other Rider {tag}", "staff", UserRole.DELIVERY_PARTNER, roles["Delivery Partner"].id),
            ("so", f"Sales Sam {tag}", "staff", UserRole.SALES_OFFICER, roles["Sales Officer"].id),
            ("zero", f"Zero Perm {tag}", "staff", UserRole.SALES_OFFICER, zero.id),
        ):
            u = User(email=f"{key}.{uuid.uuid4().hex[:8]}@example.com", name=name, organization_id=org.id,
                     system_role=sr, role=legacy, role_id=rid, password_hash=hash_password("Password123!"),
                     is_active=True)
            db.add(u)
            db.flush()
            h[key] = {"Authorization": f"Bearer {create_access_token(u.id, sr, org.id)}"}
            ids[key], names[key] = u.id, name
        cu = Customer(organization_id=org.id, name=f"Ganesh {tag}", opening_balance=20000.0, outstanding_balance=20000.0)
        db.add(cu)
        db.flush()
        so = SalesOrder(organization_id=org.id, customer_id=cu.id, order_number=f"SO-{tag}", total=10000.0,
                        status="confirmed")
        db.add(so)
        db.flush()
        dlv = Delivery(organization_id=org.id, customer_id=cu.id, sales_order_id=so.id,
                       delivery_note_number=f"DO-{tag}", delivery_partner_id=ids["rider"], status="delivered")
        db.add(dlv)
        db.commit()
        return {"org": org.id, "h": h, "ids": ids, "names": names, "dlv": dlv.id, "cust": cu.id,
                "so_role": roles["Sales Officer"].id, "dlv_no": f"DO-{tag}", "order_no": f"SO-{tag}"}


def _record(f, who, amount, mode="cash"):
    r = c.post(f"/deliveries/{f['dlv']}/collections", headers=f["h"][who],
               json={"amount": amount, "payment_mode": mode})
    return r


def test_collector_identity_and_day_end():
    f = _firm()
    rider = _record(f, "rider", 300.0)
    admin = _record(f, "admin", 500.0, "upi")
    acct = _record(f, "acct", 200.0)
    for r in (rider, admin, acct):
        assert r.status_code == 201, r.text
    rider, admin, acct = rider.json(), admin.json(), acct.json()
    for body, who in ((rider, "rider"), (admin, "admin"), (acct, "acct")):
        assert body["collected_by_user_id"] == f["ids"][who] == body["delivery_partner_id"] == body["collector_id"]
        assert body["collected_by_name"] == f["names"][who]
        assert body["assigned_delivery_partner_id"] == f["ids"]["rider"]
        assert body["delivery_partner_name"] == f["names"]["rider"]
        assert body["delivery_number"] == f["dlv_no"] and body["order_number"] == f["order_no"]
    # Same on the list.
    items = {i["id"]: i for i in c.get("/deliveries/collections", headers=f["h"]["admin"]).json()["items"]}
    assert items[admin["id"]]["collected_by_name"] == f["names"]["admin"]
    assert items[admin["id"]]["delivery_partner_name"] == f["names"]["rider"]
    # Day-End: the rider's day has only the rider's own ₹300.
    with SessionLocal() as db:
        day = collection_dates.day_of(db, db.get(__import__("app.models", fromlist=["DeliveryCollection"]).DeliveryCollection, rider["id"]))
        rows = day_end_service.collections_for(db, f["org"], f["ids"]["rider"], day)
        assert [r.id for r in rows] == [rider["id"]]
        totals = day_end_service.expected_breakdown(rows)
        assert (float(totals["cash"]), float(totals["upi"])) == (300.0, 0.0)   # admin UPI ₹500 not here
        assert [r.id for r in day_end_service.collections_for(db, f["org"], f["ids"]["admin"], day)] == [admin["id"]]
    # A rider cannot record another rider's delivery; zero-permission user cannot record at all.
    assert _record(f, "rider2", 1.0).status_code == 403
    assert _record(f, "zero", 1.0).status_code == 403


def test_general_customer_collection_unchanged():
    f = _firm()
    r = c.post("/customer-payments/collections", headers=f["h"]["rider"],
               json={"customer_id": f["cust"], "amount": 250.0, "payment_method": "cash"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["collected_by_user_id"] == f["ids"]["rider"] == body["delivery_partner_id"]
    assert body["delivery_id"] is None and body["assigned_delivery_partner_id"] is None
    assert body["collected_by_name"] == f["names"]["rider"]


def test_zero_permission_user_gets_403_everywhere():
    f = _firm()
    coll = _record(f, "admin", 50.0).json()["id"]
    h = f["h"]["zero"]
    for path, params in (
        ("/deliveries/collections", {}),
        ("/deliveries/collections/all", {}),
        ("/deliveries/collections", {"view": "pending"}),
        ("/deliveries/collections", {"view": "collected"}),
        ("/deliveries/collections", {"status": "completed"}),
        ("/deliveries/collections", {"status": "voided"}),
        ("/deliveries/collections/summary", {}),
        (f"/deliveries/collections/{coll}", {}),
        (f"/deliveries/{f['dlv']}/collections", {}),
    ):
        r = c.get(path, headers=h, params=params)
        assert r.status_code == 403, (path, params, r.status_code)
    assert r.json()["detail"]
    for action in ("reconcile", "void"):
        assert c.post(f"/deliveries/collections/{coll}/{action}", headers=h, json={}).status_code == 403


def test_rider_sees_own_only_and_cannot_confirm_or_void():
    f = _firm()
    own = _record(f, "rider", 100.0).json()["id"]
    other = _record(f, "admin", 40.0).json()["id"]
    ids = [i["id"] for i in c.get("/deliveries/collections", headers=f["h"]["rider"]).json()["items"]]
    assert ids == [own]
    assert c.get(f"/deliveries/collections/{other}", headers=f["h"]["rider"]).status_code == 403
    for action in ("reconcile", "void"):
        assert c.post(f"/deliveries/collections/{own}/{action}", headers=f["h"]["rider"], json={}).status_code == 403


def test_default_sales_officer_cannot_confirm_or_void_admin_and_accountant_can():
    f = _firm()
    a = _record(f, "rider", 100.0).json()["id"]
    b = _record(f, "rider", 60.0).json()["id"]
    for action in ("reconcile", "void"):
        assert c.post(f"/deliveries/collections/{a}/{action}", headers=f["h"]["so"], json={}).status_code == 403
    r = c.post(f"/deliveries/collections/{a}/reconcile", headers=f["h"]["acct"], json={})
    assert r.status_code == 200 and r.json()["collection_status"] == "completed", r.text
    r = c.post(f"/deliveries/collections/{a}/void", headers=f["h"]["admin"], json={})
    assert r.status_code == 200 and r.json()["collection_status"] == "voided", r.text
    r = c.post(f"/deliveries/collections/{b}/reconcile", headers=f["h"]["admin"], json={})
    assert r.status_code == 200, r.text
    r = c.post(f"/deliveries/collections/{b}/void", headers=f["h"]["acct"], json={})
    assert r.status_code == 200, r.text


def test_sales_officer_role_data_fix_sql():
    """A live Sales Officer role edited to have deliveries.approve (+ invoices view) can Confirm;
    the pending SQL removes only that right, only on the default Sales Officer role."""
    if engine.dialect.name != "postgresql":
        pytest.skip("SQL file is Postgres")
    f, other = _firm(), _firm()
    with SessionLocal() as db:
        for firm in (f, other):
            role = db.get(Role, firm["so_role"])
            perms = dict(role.permissions)
            perms["deliveries"] = {**perms.get("deliveries", {}), "view": True, "approve": True}
            perms["invoices"] = {**perms.get("invoices", {}), "view": True}
            role.permissions = perms
        db.commit()
    a = _record(f, "rider", 10.0).json()["id"]
    b = _record(f, "rider", 10.0).json()["id"]
    assert c.post(f"/deliveries/collections/{a}/reconcile", headers=f["h"]["so"], json={}).status_code == 200

    import app as app_pkg
    path = os.path.join(os.path.dirname(app_pkg.__file__), "..", "alembic", "pending",
                        "sales_officer_no_collection_approve.sql")
    sql = open(path, encoding="utf-8").read()
    statements = [s.strip() for s in "\n".join(
        ln for ln in sql.splitlines() if not ln.strip().startswith("--")).split(";") if s.strip()]
    assert len(statements) == 2
    with engine.begin() as conn:
        found = conn.execute(text(statements[0])).fetchall()
        assert {row[0] for row in found} >= {f["org"], other["org"]}
        conn.execute(text(statements[1]))
    with SessionLocal() as db:
        for firm in (f, other):
            perms = db.get(Role, firm["so_role"]).permissions
            assert perms["deliveries"]["approve"] is False and perms["deliveries"]["view"] is True
            assert perms["invoices"]["view"] is True
        acct = db.query(Role).filter(Role.organization_id == f["org"], Role.name == "Accountant").one()
        assert acct.permissions["deliveries"]["approve"] is True if "deliveries" in acct.permissions else True
    assert c.post(f"/deliveries/collections/{b}/reconcile", headers=f["h"]["so"], json={}).status_code == 403
    assert c.post(f"/deliveries/collections/{b}/reconcile", headers=f["h"]["acct"], json={}).status_code == 200


def test_cross_tenant_blocked():
    f, other = _firm(), _firm()
    coll = _record(f, "admin", 20.0).json()["id"]
    assert c.get(f"/deliveries/collections/{coll}", headers=other["h"]["admin"]).status_code == 404
    assert coll not in [i["id"] for i in c.get("/deliveries/collections", headers=other["h"]["admin"]).json()["items"]]
    assert c.post(f"/deliveries/collections/{coll}/void", headers=other["h"]["admin"], json={}).status_code == 404
    assert c.post(f"/deliveries/{f['dlv']}/collections", headers=other["h"]["admin"],
                  json={"amount": 1.0, "payment_mode": "cash"}).status_code == 404
