"""GET /orders?sort_by=updated_at (recent activity), with status, search and pagination."""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import update  # noqa: E402

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Customer, Organization, SalesOrder, User  # noqa: E402
from app.models.enums import UserRole  # noqa: E402
from app.services import entitlement_service, role_service  # noqa: E402

client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)


@pytest.fixture(autouse=True)
def _no_plan_gates(monkeypatch):
    monkeypatch.setattr(entitlement_service, "check_entitlement", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "check_limit", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "has_entitlement", lambda *a, **k: True)


@pytest.fixture(scope="module")
def world():
    """Five confirmed orders whose updated_at order differs from their created order,
    plus one draft. ORD-A..E created in that order; recent activity: C, A, E, B, D."""
    db = SessionLocal()
    try:
        org = Organization(name=f"SortUpd {uuid.uuid4().hex[:6]}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}")
        db.add(org)
        db.flush()
        role_service.seed_default_roles(db, org.id)
        admin = User(email=f"su.{uuid.uuid4().hex[:8]}@example.com", name="Admin", organization_id=org.id,
                     system_role="admin", role=UserRole.ADMIN, password_hash=hash_password("Password123!"), is_active=True)
        cust = Customer(organization_id=org.id, name="Shop")
        db.add_all([admin, cust])
        db.flush()
        tag = uuid.uuid4().hex[:4]
        base = datetime.now(timezone.utc) - timedelta(days=10)
        activity = {"A": 4, "B": 2, "C": 5, "D": 1, "E": 3}      # bigger = more recent
        ids = {}
        for i, key in enumerate("ABCDE"):
            o = SalesOrder(organization_id=org.id, customer_id=cust.id, order_number=f"SU{tag}-{key}",
                           status="placed", created_at=base + timedelta(hours=i))
            db.add(o)
            db.flush()
            ids[key] = o.id
        draft = SalesOrder(organization_id=org.id, customer_id=cust.id, order_number=f"SU{tag}-DRAFT", status="draft",
                           created_at=base)
        db.add(draft)
        db.commit()
        # Set updated_at directly (onupdate would overwrite it on an ORM update).
        for key, rank in activity.items():
            db.execute(update(SalesOrder).where(SalesOrder.id == ids[key])
                       .values(updated_at=base + timedelta(days=rank)))
        db.execute(update(SalesOrder).where(SalesOrder.id == draft.id).values(updated_at=base + timedelta(days=9)))
        db.commit()
        return {"h": {"Authorization": f"Bearer {create_access_token(admin.id, 'admin', org.id)}"}, "tag": tag}
    finally:
        db.close()


def _keys(w, query):
    r = client.get(f"/orders?{query}", headers=w["h"])
    assert r.status_code == 200, r.text
    return [o["order_number"].split("-")[-1] for o in r.json()["items"]], r.json()


def test_updated_at_desc(world):
    keys, _ = _keys(world, "status=confirmed&sort_by=updated_at&sort_order=desc&page_size=50")
    assert keys == ["C", "A", "E", "B", "D"]


def test_updated_at_asc(world):
    keys, _ = _keys(world, "status=confirmed&sort_by=updated_at&sort_order=asc&page_size=50")
    assert keys == ["D", "B", "E", "A", "C"]


def test_status_filter_with_updated_at(world):
    keys, _ = _keys(world, "sort_by=updated_at&sort_order=desc&page_size=50")
    assert keys == ["DRAFT", "C", "A", "E", "B", "D"]                     # no filter: draft is newest
    keys, _ = _keys(world, "status=draft&sort_by=updated_at&sort_order=desc")
    assert keys == ["DRAFT"]


def test_search_with_updated_at(world):
    keys, _ = _keys(world, f"search=SU{world['tag']}-&status=confirmed&sort_by=updated_at&sort_order=desc")
    assert keys == ["C", "A", "E", "B", "D"]
    keys, _ = _keys(world, f"search=SU{world['tag']}-A&sort_by=updated_at&sort_order=desc")
    assert keys == ["A"]


def test_pagination_with_updated_at(world):
    p1, body1 = _keys(world, "status=confirmed&sort_by=updated_at&sort_order=desc&page=1&page_size=2")
    p2, _ = _keys(world, "status=confirmed&sort_by=updated_at&sort_order=desc&page=2&page_size=2")
    p3, _ = _keys(world, "status=confirmed&sort_by=updated_at&sort_order=desc&page=3&page_size=2")
    assert (p1, p2, p3) == (["C", "A"], ["E", "B"], ["D"]) and body1["total"] == 5


def test_default_sort_unchanged_and_bad_field_rejected(world):
    keys, _ = _keys(world, "status=confirmed&page_size=50")
    assert keys == ["E", "D", "C", "B", "A"]                              # created_at desc, as before
    r = client.get("/orders?sort_by=not_a_field", headers=world["h"])
    assert r.status_code == 400
