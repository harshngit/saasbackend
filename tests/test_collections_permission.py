"""Reconcile / void a delivery collection and approve a Day-End need the `deliveries`
module's `approve` right on top of each endpoint's existing check. Two firms: Admin 200,
Delivery Partner 403, built-in Accountant (no Deliveries rights) now 403, a custom role with
Deliveries > Approve + Payments > View 200, another firm's admin 404."""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Customer, DeliveryCollection, Organization, Role, User  # noqa: E402
from app.models.delivery_day_end import DeliveryDayEnd  # noqa: E402
from app.models.enums import UserRole  # noqa: E402
from app.routers.deliveries import COLLECTIONS_APPROVE_DETAIL  # noqa: E402
from app.services import entitlement_service, role_service  # noqa: E402

c = TestClient(app, raise_server_exceptions=False)


@pytest.fixture(scope="module", autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)


@pytest.fixture(autouse=True)
def _no_plan_gates(monkeypatch):
    monkeypatch.setattr(entitlement_service, "check_entitlement", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "check_limit", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "has_entitlement", lambda *a, **k: True)


def _user(db, org, name, system_role, role=UserRole.ADMIN, role_id=None):
    u = User(email=f"{name.lower().replace(' ', '')}.{uuid.uuid4().hex[:8]}@example.com", name=name,
             organization_id=org.id, system_role=system_role, role=role, role_id=role_id,
             password_hash=hash_password("Password123!"), is_active=True)
    db.add(u)
    db.flush()
    return u


def _firm():
    with SessionLocal() as db:
        org = Organization(name=f"CP {uuid.uuid4().hex[:6]}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}",
                           state="Maharashtra", currency="INR")
        db.add(org)
        db.flush()
        role_service.seed_default_roles(db, org.id)
        roles = {r.name: r for r in db.query(Role).filter(Role.organization_id == org.id)}
        # A firm seeded before the Accountant got Deliveries > View + Approve by default: its
        # Accountant role has no deliveries rights (existing firms' roles are never rewritten).
        new_default_acct = Role(organization_id=org.id, name=f"Accountant (new default) {uuid.uuid4().hex[:4]}",
                                permissions=dict(roles["Accountant"].permissions))
        old_perms = dict(roles["Accountant"].permissions)
        old_perms.pop("deliveries", None)
        roles["Accountant"].permissions = old_perms
        db.add(new_default_acct)
        clerk = Role(organization_id=org.id, name=f"Collections Clerk {uuid.uuid4().hex[:4]}",
                     permissions={"deliveries": {"view": True, "approve": True}, "payments": {"view": True}})
        db.add(clerk)
        db.flush()
        admin = _user(db, org, "Admin", "admin")
        dp = _user(db, org, "Driver", "staff", UserRole.DELIVERY_PARTNER, roles["Delivery Partner"].id)
        acct = _user(db, org, "Accountant", "staff", UserRole.ACCOUNTANT, roles["Accountant"].id)
        clerk_user = _user(db, org, "Clerk", "staff", UserRole.ACCOUNTANT, clerk.id)
        new_acct = _user(db, org, "New Accountant", "staff", UserRole.ACCOUNTANT, new_default_acct.id)
        cust = Customer(organization_id=org.id, name="Shop", state="Maharashtra")
        db.add(cust)
        db.flush()
        colls = []
        for _ in range(5):
            coll = DeliveryCollection(organization_id=org.id, customer_id=cust.id, delivery_partner_id=dp.id,
                                      amount=10, payment_mode="cash")
            db.add(coll)
            db.flush()
            colls.append(coll.id)
        day_end = DeliveryDayEnd(organization_id=org.id, delivery_partner_id=dp.id,
                                 business_date=(datetime.now(timezone.utc) - timedelta(days=1)).date(), status="submitted")
        db.add(day_end)
        db.commit()

        def tok(u, sr):
            return {"Authorization": f"Bearer {create_access_token(u.id, sr, org.id)}"}

        return {"admin": tok(admin, "admin"), "dp": tok(dp, "staff"), "acct": tok(acct, "staff"),
                "clerk": tok(clerk_user, "staff"), "new_acct": tok(new_acct, "staff"), "colls": colls, "day_end": day_end.id}


@pytest.fixture(scope="module")
def firms():
    return _firm(), _firm()


def _reconcile(f, idx, who):
    return c.post(f"/deliveries/collections/{f['colls'][idx]}/reconcile", headers=f[who])


def test_reconcile_and_void(firms):
    f, other = firms
    r = _reconcile(f, 0, "dp")
    assert r.status_code == 403
    r = _reconcile(f, 0, "acct")
    assert r.status_code == 403 and r.json()["detail"] == COLLECTIONS_APPROVE_DETAIL
    assert c.post(f"/deliveries/collections/{f['colls'][0]}/reconcile", headers=other["admin"]).status_code == 404
    assert _reconcile(f, 0, "clerk").status_code == 200
    assert _reconcile(f, 1, "admin").status_code == 200
    for who, code in (("dp", 403), ("acct", 403)):
        assert c.post(f"/deliveries/collections/{f['colls'][2]}/void", headers=f[who]).status_code == code
    assert c.post(f"/deliveries/collections/{f['colls'][2]}/void", headers=other["admin"]).status_code == 404
    assert c.post(f"/deliveries/collections/{f['colls'][2]}/void", headers=f["clerk"]).status_code == 200
    assert c.post(f"/deliveries/collections/{f['colls'][3]}/void", headers=f["admin"]).status_code == 200
    # A new firm's seeded Accountant has Deliveries > Approve and can approve collections.
    assert _reconcile(f, 4, "new_acct").status_code == 200


def test_day_end_approve(firms):
    f, other = firms
    url = f"/delivery-day-ends/{f['day_end']}/approve"
    assert c.post(url, json={}, headers=f["dp"]).status_code == 403
    assert c.post(url, json={}, headers=f["acct"]).status_code == 403
    assert c.post(url, json={}, headers=other["admin"]).status_code == 404
    assert c.post(url, json={}, headers=f["admin"]).status_code == 200
