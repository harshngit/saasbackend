"""Lead -> converted Customer activity linkage.

One Visit / Follow-up row serves both sides: it keeps lead_id (where it was
logged) and carries the Lead's Customer as customer_id, so it shows on the
Lead's Visits tab AND the Customer's Activities — never as a duplicate row.

Run: pytest tests/test_lead_customer_activity_link.py
(uses DATABASE_URL from the environment / .env; tables are created if missing)
"""

import importlib.util
import os
import sys
import uuid
from datetime import datetime, timezone

os.environ.setdefault("TESTING", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient

from app.core.database import Base, SessionLocal, auto_add_missing_columns, engine
from app.main import app
from app.models import Customer, FollowUp, Lead, Organization, Plan, Visit

Base.metadata.create_all(bind=engine)
auto_add_missing_columns()
client = TestClient(app)

VISIT_DATE = "2026-10-31T21:00:00+05:30"


def _register_org(label: str) -> tuple[dict, str]:
    r = client.post("/auth/register", json={
        "organization_name": f"{label} {uuid.uuid4().hex[:6]}",
        "admin_name": f"Admin {label}",
        "email": f"admin_{uuid.uuid4().hex[:10]}@example.com",
        "password": "Password123!",
        "role": "admin",
    })
    assert r.status_code == 201, r.text
    auth = {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}
    org_id = client.get("/auth/me", headers=auth).json()["organization_id"]
    # Same approach as test_division4_visit_followup: Leads need a plan with crm.leads.
    with SessionLocal() as db:
        plan = db.query(Plan).filter(Plan.name == "Enterprise").first()
        assert plan is not None, "Enterprise plan missing from the test database"
        db.query(Organization).filter(Organization.id == org_id).update({"plan_id": plan.id})
        db.commit()
    return auth, org_id


def _lead(auth: dict, name: str, **extra) -> dict:
    r = client.post("/leads", json={"name": name, "mobile": "9876543210", "source": "Website", **extra}, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


def _customer(auth: dict, name: str) -> dict:
    r = client.post("/customers", json={"name": name, "email": f"c_{uuid.uuid4().hex[:8]}@example.com"}, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


def _visit(auth: dict, **body) -> dict:
    r = client.post("/visits", json={"visit_date": VISIT_DATE, "visit_type": "site_visit", **body}, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


def _lead_visit_ids(auth: dict, lead_id: str) -> list[str]:
    r = client.get("/visits", params={"lead_id": lead_id, "page_size": 100}, headers=auth)
    assert r.status_code == 200, r.text
    return [v["id"] for v in r.json()["items"]]


def _customer_visit_ids(auth: dict, customer_id: str) -> list[str]:
    r = client.get(f"/customers/{customer_id}/visits", headers=auth)
    assert r.status_code == 200, r.text
    return [v["id"] for v in r.json()]


def _org_customer_count(org_id: str) -> int:
    with SessionLocal() as db:
        return db.query(Customer).filter(Customer.organization_id == org_id).count()


@pytest.fixture(scope="module")
def org_a():
    return _register_org("Activity Org A")


@pytest.fixture(scope="module")
def org_b():
    return _register_org("Activity Org B")


@pytest.fixture(scope="module")
def converted(org_a):
    """Rainbow-style Lead converted through the API."""
    auth, org_id = org_a
    lead = _lead(auth, "Rainbow Children Hospital")
    r = client.post(f"/leads/{lead['id']}/convert-to-customer", json={}, headers=auth)
    assert r.status_code == 200, r.text
    return lead, r.json()["customer_id"]


def test_unlinked_lead_visit_stays_on_lead(org_a):
    auth, _ = org_a
    lead = _lead(auth, "Plain Lead")
    v = _visit(auth, lead_id=lead["id"])
    assert v["lead_id"] == lead["id"]
    assert v["customer_id"] is None
    assert _lead_visit_ids(auth, lead["id"]) == [v["id"]]


def test_converted_lead_state_is_canonical(org_a, converted):
    auth, _ = org_a
    lead, customer_id = converted
    body = client.get(f"/leads/{lead['id']}", headers=auth).json()
    assert body["lead_status"] == "won"
    assert body["status"] == "won"
    assert body["is_converted"] is True
    assert body["converted_customer_id"] == customer_id
    assert body["customer_id"] == customer_id
    assert body["converted_at"] is not None


def test_converted_lead_visit_on_lead_and_customer_single_row(org_a, converted):
    auth, _ = org_a
    lead, customer_id = converted
    v = _visit(auth, lead_id=lead["id"], status="planned")
    assert v["lead_id"] == lead["id"]
    assert v["customer_id"] == customer_id

    assert v["id"] in _lead_visit_ids(auth, lead["id"])
    assert v["id"] in _customer_visit_ids(auth, customer_id)

    with SessionLocal() as db:
        rows = db.query(Visit).filter(Visit.lead_id == lead["id"]).all()
        assert len(rows) == 1
        assert db.query(Visit).filter(Visit.customer_id == customer_id).count() == 1


def test_converted_lead_follow_up_on_customer(org_a, converted):
    auth, _ = org_a
    lead, customer_id = converted
    r = client.post("/follow-ups", json={
        "lead_id": lead["id"], "title": "Call back", "due_date": VISIT_DATE,
    }, headers=auth)
    assert r.status_code == 201, r.text
    fu = r.json()
    assert fu["lead_id"] == lead["id"] and fu["customer_id"] == customer_id
    ids = [f["id"] for f in client.get(f"/customers/{customer_id}/follow-ups", headers=auth).json()]
    assert fu["id"] in ids
    with SessionLocal() as db:
        assert db.query(FollowUp).filter(FollowUp.id == fu["id"]).count() == 1


def test_customer_created_visit_still_works(org_a, converted):
    auth, _ = org_a
    lead, customer_id = converted
    v = _visit(auth, customer_id=customer_id)
    assert v["customer_id"] == customer_id and v["lead_id"] is None
    assert v["id"] in _customer_visit_ids(auth, customer_id)
    assert v["id"] not in _lead_visit_ids(auth, lead["id"])


def test_unrelated_customer_does_not_see_visit(org_a, converted):
    auth, _ = org_a
    lead, _ = converted
    other = _customer(auth, "Unrelated Clinic")
    v = _visit(auth, lead_id=lead["id"])
    assert v["id"] not in _customer_visit_ids(auth, other["id"])


def test_other_org_cannot_access(org_a, org_b, converted):
    auth_a, _ = org_a
    auth_b, _ = org_b
    lead, customer_id = converted
    v = _visit(auth_a, lead_id=lead["id"])

    assert client.get(f"/visits/{v['id']}", headers=auth_b).status_code == 404
    assert client.get(f"/customers/{customer_id}/visits", headers=auth_b).status_code == 404
    assert client.get(f"/leads/{lead['id']}", headers=auth_b).status_code == 404
    assert _lead_visit_ids(auth_b, lead["id"]) == []
    r = client.post("/visits", json={"lead_id": lead["id"], "visit_date": VISIT_DATE}, headers=auth_b)
    assert r.status_code == 400


def test_repeat_conversion_is_idempotent(org_a, converted):
    auth, org_id = org_a
    lead, customer_id = converted
    before = _org_customer_count(org_id)
    for _ in range(2):
        r = client.post(f"/leads/{lead['id']}/convert-to-customer", json={}, headers=auth)
        assert r.status_code == 200, r.text
        assert r.json()["customer_id"] == customer_id
        assert r.json()["lead_status"] == "won"
    assert _org_customer_count(org_id) == before


def test_prelinked_lead_activity_and_conversion(org_a):
    """A Lead created already pointing at a Customer: activity links to that
    Customer immediately; converting it marks it won against the SAME
    Customer (the old path answered converted:true but left it 'new')."""
    auth, org_id = org_a
    cust = _customer(auth, "Prelinked Hospital")
    lead = _lead(auth, "Prelinked Hospital Lead", customer_id=cust["id"])
    assert lead["is_converted"] is False and lead["lead_status"] == "new"

    v = _visit(auth, lead_id=lead["id"])
    assert v["customer_id"] == cust["id"]
    assert v["id"] in _customer_visit_ids(auth, cust["id"])

    before = _org_customer_count(org_id)
    r = client.post(f"/leads/{lead['id']}/convert-to-customer", json={}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["customer_id"] == cust["id"]
    assert r.json()["lead_status"] == "won"
    assert _org_customer_count(org_id) == before
    body = client.get(f"/leads/{lead['id']}", headers=auth).json()
    assert body["lead_status"] == "won" and body["is_converted"] is True and body["converted_at"]


def test_patch_visit_to_unlinked_lead_drops_inherited_customer(org_a, converted):
    auth, _ = org_a
    lead, customer_id = converted
    plain = _lead(auth, "Another Plain Lead")
    v = _visit(auth, lead_id=lead["id"])
    r = client.patch(f"/visits/{v['id']}", json={"lead_id": plain["id"]}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["lead_id"] == plain["id"] and r.json()["customer_id"] is None
    assert v["id"] not in _customer_visit_ids(auth, customer_id)

    # Clearing customer_id on a converted-lead visit re-derives it.
    v2 = _visit(auth, lead_id=lead["id"])
    r = client.patch(f"/visits/{v2['id']}", json={"customer_id": None}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["customer_id"] == customer_id


def test_backfill_migration_links_legacy_rows_within_org(org_a, org_b, converted):
    _, org_a_id = org_a
    _, org_b_id = org_b
    lead, customer_id = converted
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        legacy = Visit(organization_id=org_a_id, lead_id=lead["id"], visit_date=now, status="planned")
        legacy_fu = FollowUp(organization_id=org_a_id, lead_id=lead["id"], title="legacy", due_date=now)
        # Corrupt cross-org reference: must never be linked to org A's customer.
        foreign = Visit(organization_id=org_b_id, lead_id=lead["id"], visit_date=now, status="planned")
        db.add_all([legacy, legacy_fu, foreign])
        db.commit()
        ids = (legacy.id, legacy_fu.id, foreign.id)

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "alembic", "versions", "c8d9e0f1a2b3_link_lead_activity_to_customer.py")
    spec = importlib.util.spec_from_file_location("mig_c8d9e0f1a2b3", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    for _ in range(2):  # idempotent
        with engine.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                mig.upgrade()

    with SessionLocal() as db:
        assert db.get(Visit, ids[0]).customer_id == customer_id
        assert db.get(FollowUp, ids[1]).customer_id == customer_id
        assert db.get(Visit, ids[2]).customer_id is None
        db.query(Visit).filter(Visit.id.in_([ids[0], ids[2]])).delete(synchronize_session=False)
        db.query(FollowUp).filter(FollowUp.id == ids[1]).delete(synchronize_session=False)
        db.commit()
