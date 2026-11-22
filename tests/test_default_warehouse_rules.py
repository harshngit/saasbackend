"""Default warehouse rules (code only, no data change), two firms:
first warehouse is the default; choosing a new default clears the old one in the same
transaction; the sole default cannot be un-ticked or deactivated (400, clear message);
several flagged (old data): the oldest is THE default and the flags are left alone;
none flagged: the oldest is promoted (as before); other firm 404, delivery partner 403."""

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
from app.models import Organization, User, Warehouse  # noqa: E402
from app.models.enums import UserRole  # noqa: E402
from app.services import entitlement_service, role_service, stock_service  # noqa: E402

c = TestClient(app, raise_server_exceptions=False)


@pytest.fixture(scope="module", autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)


@pytest.fixture(autouse=True)
def _no_plan_gates(monkeypatch):
    monkeypatch.setattr(entitlement_service, "check_entitlement", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "check_limit", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "has_entitlement", lambda *a, **k: True)


def _firm():
    with SessionLocal() as db:
        org = Organization(name=f"DW {uuid.uuid4().hex[:6]}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}",
                           state="Maharashtra", currency="INR")
        db.add(org)
        db.flush()
        role_service.seed_default_roles(db, org.id)
        admin = User(email=f"dw.{uuid.uuid4().hex[:8]}@example.com", name="A", organization_id=org.id, system_role="admin",
                     role=UserRole.ADMIN, password_hash=hash_password("Password123!"), is_active=True)
        dp = User(email=f"dwdp.{uuid.uuid4().hex[:8]}@example.com", name="D", organization_id=org.id, system_role="staff",
                  role=UserRole.DELIVERY_PARTNER, password_hash=hash_password("Password123!"), is_active=True)
        db.add_all([admin, dp])
        db.commit()
        return {"org": org.id, "h": {"Authorization": f"Bearer {create_access_token(admin.id, 'admin', org.id)}"},
                "dh": {"Authorization": f"Bearer {create_access_token(dp.id, 'staff', org.id)}"}}


def _create(f, **extra):
    r = c.post("/warehouses", json={"name": f"W {uuid.uuid4().hex[:4]}", "code": f"C{uuid.uuid4().hex[:6]}", **extra},
               headers=f["h"])
    assert r.status_code == 201, r.text
    return r.json()


def _defaults(org):
    with SessionLocal() as s:
        return sorted(w.id for w in s.query(Warehouse).filter(Warehouse.organization_id == org, Warehouse.is_default.is_(True)))


def test_first_warehouse_is_the_default_and_a_new_default_replaces_it():
    f = _firm()
    first = _create(f, is_default=False)
    assert first["is_default"] is True and _defaults(f["org"]) == [first["id"]]
    second = _create(f)
    assert second["is_default"] is False and _defaults(f["org"]) == [first["id"]]
    third = _create(f, is_default=True)
    assert _defaults(f["org"]) == [third["id"]]
    r = c.patch(f"/warehouses/{second['id']}", json={"is_default": True}, headers=f["h"])
    assert r.status_code == 200 and _defaults(f["org"]) == [second["id"]]


def test_the_sole_default_cannot_be_unticked_or_deactivated():
    f = _firm()
    first = _create(f)
    other = _create(f)
    for body in ({"is_default": False}, {"is_active": False}):
        r = c.patch(f"/warehouses/{first['id']}", json=body, headers=f["h"])
        assert r.status_code == 400 and "Choose another default first" in r.json()["detail"], r.text
    assert _defaults(f["org"]) == [first["id"]]
    # Editing other fields of the default (the form sends is_default: true) still works.
    r = c.patch(f"/warehouses/{first['id']}", json={"name": "Renamed", "is_default": True}, headers=f["h"])
    assert r.status_code == 200 and r.json()["name"] == "Renamed"
    # A non-default warehouse can be deactivated as before.
    assert c.patch(f"/warehouses/{other['id']}", json={"is_active": False}, headers=f["h"]).status_code == 200


def test_several_flagged_oldest_wins_and_flags_stay():
    f = _firm()
    now = datetime.now(timezone.utc)
    with SessionLocal() as s:
        old = Warehouse(organization_id=f["org"], name="Old", code=f"O{uuid.uuid4().hex[:5]}", is_default=True,
                        created_at=now - timedelta(days=2))
        new = Warehouse(organization_id=f["org"], name="New", code=f"N{uuid.uuid4().hex[:5]}", is_default=True,
                        created_at=now - timedelta(days=1))
        s.add_all([new, old])
        s.commit()
        old_id, new_id = old.id, new.id
    with SessionLocal() as s:
        assert stock_service.default_warehouse(s, f["org"]).id == old_id
        assert stock_service._is_default_warehouse(s, old_id) is True
        assert stock_service._is_default_warehouse(s, new_id) is False
        s.commit()
    assert _defaults(f["org"]) == sorted([old_id, new_id])                  # data untouched
    # Un-ticking one of two flagged is allowed (another default remains).
    assert c.patch(f"/warehouses/{new_id}", json={"is_default": False}, headers=f["h"]).status_code == 200


def test_none_flagged_promotes_the_oldest_as_before():
    f = _firm()
    now = datetime.now(timezone.utc)
    with SessionLocal() as s:
        a = Warehouse(organization_id=f["org"], name="A", code=f"A{uuid.uuid4().hex[:5]}", is_default=False,
                      created_at=now - timedelta(days=3))
        b = Warehouse(organization_id=f["org"], name="B", code=f"B{uuid.uuid4().hex[:5]}", is_default=False,
                      created_at=now - timedelta(days=1))
        s.add_all([b, a])
        s.commit()
        a_id = a.id
    with SessionLocal() as s:
        assert stock_service.default_warehouse(s, f["org"]).id == a_id
        s.commit()
    assert _defaults(f["org"]) == [a_id]


def test_other_firm_and_delivery_partner():
    f, other = _firm(), _firm()
    w = _create(f)
    assert c.patch(f"/warehouses/{w['id']}", json={"is_default": False}, headers=other["h"]).status_code == 404
    assert c.patch(f"/warehouses/{w['id']}", json={"name": "x"}, headers=f["dh"]).status_code == 403
