"""Delivery-partner collection approval (Flutter team, 18 Nov).

- GET /customers carries collection_status ("pending_approval" | null) and
  collectable_amount (outstanding less collections awaiting approval).
- POST /deliveries/collections/customer refuses (409) a collection that would take more
  than is still collectable, so two partners cannot collect the same balance.
- Approving or voiding the pending collection clears the status.
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
from app.models import Customer, Organization, User  # noqa: E402
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


def _user(db, org_id, role, system_role):
    u = User(email=f"{system_role}.{uuid.uuid4().hex[:8]}@example.com", name=system_role, organization_id=org_id,
             system_role=system_role, role=role, password_hash=hash_password("Password123!"), is_active=True)
    db.add(u)
    db.flush()
    return {"Authorization": f"Bearer {create_access_token(u.id, system_role, org_id)}"}


@pytest.fixture
def firm():
    db = SessionLocal()
    try:
        org = Organization(name=f"Coll {uuid.uuid4().hex[:6]}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}",
                           currency="INR")
        db.add(org)
        db.flush()
        role_service.seed_default_roles(db, org.id)
        admin = _user(db, org.id, UserRole.ADMIN, "admin")
        partner_a = _user(db, org.id, UserRole.DELIVERY_PARTNER, "delivery_partner")
        partner_b = _user(db, org.id, UserRole.DELIVERY_PARTNER, "delivery_partner")
        cust = Customer(organization_id=org.id, name="ABC Traders", opening_balance=5000.0, outstanding_balance=5000.0)
        clear = Customer(organization_id=org.id, name="Paid Up")
        db.add_all([cust, clear])
        db.commit()
        return {"admin": admin, "a": partner_a, "b": partner_b, "cust": cust.id, "clear": clear.id}
    finally:
        db.close()


def _collect(h, customer_id, amount):
    return client.post("/deliveries/collections/customer", headers=h,
                       json={"customer_id": customer_id, "amount": amount, "payment_method": "cash"})


def _listed(f, customer_id):
    r = client.get("/customers", headers=f["admin"], params={"is_active": True, "page_size": 100})
    assert r.status_code == 200, r.text
    return next(c for c in r.json()["items"] if c["id"] == customer_id)


def test_no_collection_means_no_status(firm):
    c = _listed(firm, firm["cust"])
    assert c["collection_status"] is None and c["collectable_amount"] == 5000.0
    assert _listed(firm, firm["clear"])["collectable_amount"] == 0.0


def test_submitted_collection_shows_pending_approval_and_blocks_a_second_partner(firm):
    r = _collect(firm["a"], firm["cust"], 5000)
    assert r.status_code == 201, r.text
    assert r.json()["reconciliation_status"] == "recorded"
    c = _listed(firm, firm["cust"])
    assert c["collection_status"] == "pending_approval" and c["collectable_amount"] == 0.0
    assert c["outstanding_balance"] == 5000.0          # unchanged until an admin approves

    r = _collect(firm["b"], firm["cust"], 5000)       # partner B tries the same balance
    assert r.status_code == 409, r.text
    assert "awaiting admin approval" in r.json()["detail"]
    r = _collect(firm["a"], firm["cust"], 1)          # and the same partner cannot submit twice
    assert r.status_code == 409, r.text


def test_part_collection_leaves_the_rest_collectable(firm):
    assert _collect(firm["a"], firm["cust"], 2000).status_code == 201
    c = _listed(firm, firm["cust"])
    assert c["collection_status"] == "pending_approval" and c["collectable_amount"] == 3000.0
    assert _collect(firm["b"], firm["cust"], 3000).status_code == 201     # the rest is fine
    assert _collect(firm["b"], firm["cust"], 0.5).status_code == 409      # nothing left


def test_approval_and_void_clear_the_status(firm):
    coll = _collect(firm["a"], firm["cust"], 2000).json()
    r = client.post(f"/deliveries/collections/{coll['id']}/reconcile", headers=firm["admin"])
    assert r.status_code == 200, r.text
    c = _listed(firm, firm["cust"])
    assert c["collection_status"] is None and c["outstanding_balance"] == 3000.0 and c["collectable_amount"] == 3000.0

    coll = _collect(firm["b"], firm["cust"], 3000).json()
    assert _listed(firm, firm["cust"])["collection_status"] == "pending_approval"
    r = client.post(f"/deliveries/collections/{coll['id']}/void", headers=firm["admin"])
    assert r.status_code == 200, r.text
    c = _listed(firm, firm["cust"])
    assert c["collection_status"] is None and c["collectable_amount"] == 3000.0
