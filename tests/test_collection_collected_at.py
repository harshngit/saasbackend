"""Collection dates: payment_date (business day, date only) is kept apart from collected_at
(when it was recorded = server now). Both create paths, confirm, list order, date filter,
Day-End day, the pre-migration fallback, and the pending migration. Two firms.
"""

import importlib.util
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.core.security import create_access_token  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Customer, CustomerPayment, Delivery, DeliveryCollection, Organization, SalesOrder, User  # noqa: E402
from app.models.enums import UserRole  # noqa: E402
from app.services import collection_dates, entitlement_service  # noqa: E402

c = TestClient(app)
IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture(scope="module", autouse=True)
def _schema():
    Base.metadata.create_all(engine)


@pytest.fixture(autouse=True)
def _no_plan_gates(monkeypatch):
    monkeypatch.setattr(entitlement_service, "check_entitlement", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "check_limit", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "has_entitlement", lambda *a, **k: True)


def _ts(s):
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _today():
    return datetime.now(IST).date()


def _firm():
    tag = uuid.uuid4().hex[:5]
    with SessionLocal() as db:
        org = Organization(name=f"CA {tag}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}",
                           currency="INR", timezone="Asia/Kolkata")
        db.add(org)
        db.flush()
        u = User(email=f"a.{uuid.uuid4().hex[:8]}@example.com", name=f"Admin {tag}", organization_id=org.id,
                 system_role="admin", role=UserRole.ADMIN, password_hash="x", is_active=True)
        cu = Customer(organization_id=org.id, name=f"Shop {tag}", opening_balance=100000.0,
                      outstanding_balance=100000.0)
        db.add_all([u, cu])
        db.flush()
        so = SalesOrder(organization_id=org.id, customer_id=cu.id, order_number=f"SO-{tag}", total=5000.0,
                        status="confirmed")
        db.add(so)
        db.flush()
        dlv = Delivery(organization_id=org.id, customer_id=cu.id, sales_order_id=so.id,
                       delivery_note_number=f"DO-{tag}", delivery_partner_id=u.id, status="delivered")
        db.add(dlv)
        # An entry from earlier today, stored correctly.
        earlier = datetime.now(timezone.utc) - timedelta(seconds=30)
        db.add(DeliveryCollection(organization_id=org.id, customer_id=cu.id, delivery_partner_id=u.id,
                                  amount=50.0, payment_mode="cash", reconciliation_status="recorded",
                                  collected_at=earlier))
        db.commit()
        return {"org": org.id, "cust": cu.id, "user": u.id, "dlv": dlv.id,
                "h": {"Authorization": f"Bearer {create_access_token(u.id, 'admin', org.id)}"}}


def _collect(f, **body):
    r = c.post("/customer-payments/collections", headers=f["h"],
               json={"customer_id": f["cust"], "amount": 684.0, "payment_method": "upi", "reference": "",
                     "notes": "", **body})
    assert r.status_code == 201, r.text
    return r.json()


def _is_now(body):
    collected, created = _ts(body["collected_at"]), _ts(body["created_at"])
    assert abs((collected - created).total_seconds()) < 2, (collected, created)
    assert abs((collected - datetime.now(timezone.utc)).total_seconds()) < 60
    assert (collected.hour, collected.minute, collected.second, collected.microsecond) != (0, 0, 0, 0)


def test_today_payment_date_is_now_separate_and_first():
    f, other = _firm(), _firm()
    body = _collect(f, payment_date=_today().isoformat())
    _is_now(body)
    assert body["payment_date"] == _today().isoformat()
    # Old shape kept.
    for key in ("id", "collected_at", "created_at", "reconciliation_status", "status", "amount",
                "delivery_partner_id", "collector_id", "allocations", "reconciled_at", "completed_at"):
        assert key in body, key
    items = c.get("/deliveries/collections", headers=f["h"]).json()["items"]
    assert items[0]["id"] == body["id"] and items[0]["payment_date"] == _today().isoformat()
    assert body["id"] not in [i["id"] for i in c.get("/deliveries/collections", headers=other["h"]).json()["items"]]
    # IST display: the stored UTC instant is the same moment, so its IST date is today.
    assert _ts(items[0]["collected_at"]).astimezone(IST).date() == _today()


def test_no_payment_date_is_now_and_today():
    f = _firm()
    body = _collect(f)
    _is_now(body)
    assert body["payment_date"] == _today().isoformat()


def test_past_payment_date_keeps_day_but_collected_at_is_now():
    f = _firm()
    past = _today() - timedelta(days=3)
    body = _collect(f, payment_date=past.isoformat())
    _is_now(body)
    assert body["payment_date"] == past.isoformat()
    # Newest first by collected_at, even though the business day is older.
    assert c.get("/deliveries/collections", headers=f["h"]).json()["items"][0]["id"] == body["id"]
    # Date filters use the business day.
    on_past = c.get("/deliveries/collections", headers=f["h"],
                    params={"date_from": past.isoformat(), "date_to": past.isoformat()}).json()["items"]
    assert [i["id"] for i in on_past] == [body["id"]]
    on_today = c.get("/deliveries/collections", headers=f["h"],
                     params={"date_from": _today().isoformat(), "date_to": _today().isoformat()}).json()["items"]
    assert body["id"] not in [i["id"] for i in on_today]
    # Day-End rows of that day include it.
    from app.services import day_end_service
    with SessionLocal() as db:
        assert body["id"] in [r.id for r in day_end_service.collections_for(db, f["org"], f["user"], past)]
        assert body["id"] not in [r.id for r in day_end_service.collections_for(db, f["org"], f["user"], _today())]


def test_full_timestamp_gives_its_day_and_bad_date_422():
    f = _firm()
    sent = datetime.combine(_today() - timedelta(days=1), datetime.min.time(), tzinfo=IST).replace(hour=10, minute=30)
    body = _collect(f, payment_date=sent.isoformat())
    _is_now(body)
    assert body["payment_date"] == sent.date().isoformat()
    r = c.post("/customer-payments/collections", headers=f["h"],
               json={"customer_id": f["cust"], "amount": 1.0, "payment_date": "25-11-2026"})
    assert r.status_code == 422


def test_delivery_linked_collection_same_rule():
    f = _firm()
    r = c.post(f"/deliveries/{f['dlv']}/collections", headers=f["h"],
               json={"amount": 100.0, "payment_mode": "cash", "payment_date": _today().isoformat()})
    assert r.status_code == 201, r.text
    body = r.json()
    _is_now(body)
    assert body["payment_date"] == _today().isoformat() and body["delivery_number"]
    past = _today() - timedelta(days=2)
    r = c.post(f"/deliveries/{f['dlv']}/collections", headers=f["h"],
               json={"amount": 10.0, "payment_mode": "cash", "payment_date": past.isoformat()})
    assert r.status_code == 201, r.text
    _is_now(r.json())
    assert r.json()["payment_date"] == past.isoformat()
    # Without payment_date: as before (now, today).
    r = c.post(f"/deliveries/{f['dlv']}/collections", headers=f["h"], json={"amount": 5.0, "payment_mode": "upi"})
    assert r.status_code == 201 and r.json()["payment_date"] == _today().isoformat()
    _is_now(r.json())


def test_confirm_sets_completed_at_and_receipt_on_business_day():
    f = _firm()
    past = _today() - timedelta(days=2)
    body = _collect(f, payment_date=past.isoformat())
    before = datetime.now(timezone.utc)
    r = c.post(f"/deliveries/collections/{body['id']}/reconcile", headers=f["h"], json={})
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["collection_status"] == "completed"
    assert done["completed_at"] == done["reconciled_at"]
    assert _ts(done["reconciled_at"]) >= before - timedelta(seconds=2)
    assert done["collected_at"] == body["collected_at"] and done["payment_date"] == past.isoformat()
    assert done["receipt_number"]
    with SessionLocal() as db:
        pay = db.get(CustomerPayment, done["customer_payment_id"])
        got = pay.received_on if pay.received_on.tzinfo else pay.received_on.replace(tzinfo=timezone.utc)
        assert got.astimezone(IST).date() == past


def test_before_migration_fallback(monkeypatch):
    """Column not released yet: nothing touches it, and a back-dated day is still kept."""
    f = _firm()
    monkeypatch.setattr(collection_dates, "payment_date_ready", lambda db: False)
    today = _collect(f, payment_date=_today().isoformat())
    _is_now(today)
    assert today["payment_date"] == _today().isoformat()
    past = _today() - timedelta(days=3)
    body = _collect(f, payment_date=past.isoformat())
    local = _ts(body["collected_at"]).astimezone(IST)
    assert local.date() == past and (local.hour, local.minute) != (5, 30)
    assert body["payment_date"] == past.isoformat()
    on_past = c.get("/deliveries/collections", headers=f["h"],
                    params={"date_from": past.isoformat(), "date_to": past.isoformat()}).json()["items"]
    assert [i["id"] for i in on_past] == [body["id"]]


def test_pending_migration_adds_column_backfills_safe_rows_only():
    if engine.dialect.name != "postgresql":
        pytest.skip("migration SQL is Postgres")
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    import app as app_pkg

    f, other = _firm(), _firm()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    day = now.astimezone(IST).date()
    if now.date() != day:  # keep the "same day" case unambiguous near UTC midnight
        pytest.skip("IST and UTC dates differ right now")
    mid = lambda d: datetime(d.year, d.month, d.day, tzinfo=timezone.utc)  # noqa: E731
    back = day - timedelta(days=4)
    with SessionLocal() as db:
        db.execute(text("ALTER TABLE delivery_collections DROP COLUMN payment_date"))
        db.commit()
    collection_dates._READY = False
    rows = {}
    with engine.begin() as conn:
        for key, firm, coll_at, made in (
            ("same_day", f, mid(day), now),
            ("dates_differ", f, mid(back), now),
            ("other_firm", other, mid(day), now),
            ("correct", f, now - timedelta(hours=1), now - timedelta(hours=1)),
        ):
            rid = str(uuid.uuid4())
            conn.execute(text(
                "INSERT INTO delivery_collections (id, organization_id, customer_id, delivery_partner_id, amount, "
                "payment_mode, reconciliation_status, collected_at, created_at, updated_at) VALUES "
                "(:id, :o, :c, :u, 10, 'cash', 'recorded', :ca, :cr, :cr)"),
                {"id": rid, "o": firm["org"], "c": firm["cust"], "u": firm["user"], "ca": coll_at, "cr": made})
            rows[key] = rid
        before = conn.execute(text("SELECT count(*) FROM delivery_collections")).scalar()
        report = conn.execute(text(
            "SELECT count(*), count(*) FILTER (WHERE same_day), count(*) FILTER (WHERE NOT same_day) FROM ("
            " SELECT (dc.created_at AT TIME ZONE COALESCE(NULLIF(o.timezone, ''), 'Asia/Kolkata'))::date"
            " = (dc.collected_at AT TIME ZONE 'UTC')::date AS same_day FROM delivery_collections dc"
            " JOIN organizations o ON o.id = dc.organization_id"
            " WHERE (dc.collected_at AT TIME ZONE 'UTC')::time = '00:00:00'"
            " AND dc.id = ANY(:ids)) t"), {"ids": list(rows.values())}).one()
    assert tuple(report) == (3, 2, 1)

    path = os.path.join(os.path.dirname(app_pkg.__file__), "..", "alembic", "pending",
                        "c3d4e5f6a7b8_collection_payment_date.py")
    spec = importlib.util.spec_from_file_location("mig_c3d4", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with engine.begin() as conn:
        mod.op = Operations(MigrationContext.configure(conn))
        mod.upgrade()

    with engine.connect() as conn:
        got = {k: conn.execute(text("SELECT collected_at, created_at, payment_date FROM delivery_collections "
                                    "WHERE id = :i"), {"i": v}).one() for k, v in rows.items()}
        assert conn.execute(text("SELECT count(*) FROM delivery_collections")).scalar() == before
        assert conn.execute(text("SELECT count(*) FROM delivery_collections WHERE payment_date IS NULL")).scalar() == 0
    for k in ("same_day", "other_firm"):
        assert got[k][0] == got[k][1] and got[k][2] == day, k
    assert got["dates_differ"][0] == mid(back) and got["dates_differ"][2] == back
    assert got["correct"][0] == now - timedelta(hours=1)
    assert got["correct"][2] == (now - timedelta(hours=1)).astimezone(IST).date()
    # The app picks the column up again.
    assert collection_dates.payment_date_ready(SessionLocal())
    body = _collect(f, payment_date=(day - timedelta(days=1)).isoformat())
    _is_now(body)
