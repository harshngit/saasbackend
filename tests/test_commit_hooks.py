"""After-commit hooks (realtime events, push, dashboard cache, entitlement-override cache)
run once, only after the FINAL commit -- never at a savepoint (begin_nested) release -- and
are not lost by flows that use a savepoint and then commit. Plus: two payments for the same
customer at the same moment both succeed (payments lock the customer row first).
"""

import os
import sys
import threading
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event, text  # noqa: E402

from app.core import notification_types as nt  # noqa: E402
from app.core import realtime  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Customer, CustomerPayment, Invoice, Organization, Product, ProductPricing, User, Warehouse, WarehouseStock,
)
from app.models.enums import UserRole  # noqa: E402
from app.services import (  # noqa: E402
    dashboard_service, entitlement_service, notification_service, push_service, role_service,
)


@pytest.fixture(scope="module", autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)


@pytest.fixture(autouse=True)
def _no_plan_gates(monkeypatch):
    monkeypatch.setattr(entitlement_service, "check_entitlement", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "check_limit", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "has_entitlement", lambda *a, **k: True)


@pytest.fixture
def firm():
    with SessionLocal() as db:
        org = Organization(name=f"Hook {uuid.uuid4().hex[:6]}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}",
                           gst_number="27AAAAA0000A1Z5", state="Maharashtra", currency="INR")
        db.add(org)
        db.flush()
        role_service.seed_default_roles(db, org.id)
        admin = User(email=f"hook.{uuid.uuid4().hex[:8]}@example.com", name="Admin", organization_id=org.id,
                     system_role="admin", role=UserRole.ADMIN, password_hash=hash_password("Password123!"),
                     is_active=True)
        wh = Warehouse(organization_id=org.id, name="Main", code=f"W{uuid.uuid4().hex[:4]}", is_default=True)
        cust = Customer(organization_id=org.id, name="Shop", gst_number="27BBBBB1111B1Z5", state="Maharashtra",
                        delivery_address="Addr")
        db.add_all([admin, wh, cust])
        db.flush()
        p = Product(organization_id=org.id, name="Item", sku=f"I-{uuid.uuid4().hex[:5]}", price=100, tax_rate=18,
                    hsn_code="8544", total_inventory=0, is_active=True)
        p.pricing = ProductPricing(selling_price=100, purchase_price=50, tax_inclusive=False, currency="INR")
        db.add(p)
        db.flush()
        db.add(WarehouseStock(organization_id=org.id, warehouse_id=wh.id, product_id=p.id, on_hand_quantity=100))
        db.commit()
        return {"org": org.id, "wh": wh.id, "cust": cust.id, "prod": p.id, "admin": admin.id,
                "h": {"Authorization": f"Bearer {create_access_token(admin.id, 'admin', org.id)}"}}


@pytest.fixture
def log(monkeypatch):
    """One ordered log of: real COMMITs on the connection, savepoint releases, and every hook
    action (emit / push dispatch / dashboard-cache clear / override-cache clear)."""
    entries: list[str] = []
    monkeypatch.setattr(realtime.manager, "emit", lambda *a, **k: entries.append("emit:" + (k.get("event_name") or a[1])))
    monkeypatch.setattr(push_service, "_dispatch", lambda ids: entries.append(f"push:{len(ids)}"))
    monkeypatch.setattr(settings, "push_notifications_enabled", True)
    monkeypatch.setattr(dashboard_service, "clear_dashboard_cache", lambda *a, **k: entries.append("dashboard_clear"))
    monkeypatch.setattr(entitlement_service, "clear_override_cache", lambda *a, **k: entries.append("override_clear"))

    def _commit(conn):
        entries.append("COMMIT")

    def _release(conn, name, context):
        entries.append("RELEASE_SAVEPOINT")

    event.listen(engine, "commit", _commit)
    event.listen(engine, "release_savepoint", _release)
    yield entries
    event.remove(engine, "commit", _commit)
    event.remove(engine, "release_savepoint", _release)


def _after_last_commit(entries):
    last = max(i for i, e in enumerate(entries) if e == "COMMIT")
    return entries[:last], entries[last + 1:]


# --------------------------------------------- 1. a real order create (uses savepoints)
def test_order_create_hooks_run_once_after_the_final_commit(firm, log):
    r = TestClient(app).post("/orders", headers=firm["h"], json={
        "customer_id": firm["cust"], "warehouse_id": firm["wh"], "delivery_method": "home_delivery",
        "payment_status": "pending", "items": [{"product_id": firm["prod"], "quantity": 2}]})
    assert r.status_code == 201, r.text
    assert "RELEASE_SAVEPOINT" in log                            # the flow does use savepoints
    assert log.count("COMMIT") == 1                               # one transaction
    before, after = _after_last_commit(log)
    assert not [e for e in before if e.startswith(("emit:", "push:"))], before   # nothing before the commit
    emits = [e for e in after if e.startswith("emit:")]
    assert "emit:order.created" in emits and "emit:inventory.stock_updated" in emits
    assert len(emits) == len(set(emits)) or emits.count("emit:order.created") == 1   # each event once
    assert emits.count("emit:order.created") == 1
    assert after.count("dashboard_clear") == 1                    # the post-commit cache clear, once


# ------------------------------- 2. savepoint, then commit: hooks still run, exactly once
def _session_with_flags(db):
    db.execute(text("SELECT 1"))
    db.info["crm_dashboard_changed"] = True
    db.info["crm_overrides_changed"] = True
    realtime.queue_event(db, org_id="org", event_name="queued.before.savepoint", data={})


def test_savepoint_then_commit_runs_every_hook_once_after_the_commit(log):
    with SessionLocal() as db:
        _session_with_flags(db)
        with db.begin_nested():                                   # released: hooks must NOT run
            db.execute(text("SELECT 1"))
        assert [e for e in log if e != "RELEASE_SAVEPOINT"] == []
        with db.begin_nested():
            realtime.queue_event(db, org_id="org", event_name="queued.inside.savepoint", data={})
        db.commit()
    before, after = _after_last_commit(log)
    assert [e for e in before if e != "RELEASE_SAVEPOINT"] == []
    assert after == ["emit:queued.before.savepoint", "emit:queued.inside.savepoint", "dashboard_clear",
                     "override_clear"] or sorted(after) == sorted(
        ["emit:queued.before.savepoint", "emit:queued.inside.savepoint", "dashboard_clear", "override_clear"])


def test_rolled_back_savepoint_keeps_the_transactions_queue(log):
    with SessionLocal() as db:
        _session_with_flags(db)
        try:
            with db.begin_nested():
                db.execute(text("SELECT 1"))
                raise ValueError
        except ValueError:
            pass
        db.commit()
    _, after = _after_last_commit(log)
    assert "emit:queued.before.savepoint" in after and "dashboard_clear" in after and "override_clear" in after


def test_notification_with_dedupe_savepoint_pushes_once_after_commit(firm, log, monkeypatch):
    """notify(dedupe_key=...) inserts inside its own savepoint; its realtime event and its
    push must still go out -- once -- when the caller commits."""
    monkeypatch.setattr(nt, "should_push", lambda *a, **k: True)
    with SessionLocal() as db:
        n = notification_service.notify(db, firm["admin"], "Hello", organization_id=firm["org"],
                                        event=nt.ORDER_CREATED, dedupe_key=f"k-{uuid.uuid4().hex}")
        assert n is not None
        push_service.queue_notification_push(db, n)
        assert not [e for e in log if e.startswith(("emit:", "push:"))]
        db.commit()
    before, after = _after_last_commit(log)
    assert "RELEASE_SAVEPOINT" in before
    assert after.count("push:1") == 1
    assert len([e for e in after if e.startswith("emit:")]) == 1


# ------------------------------- 3. two payments, same customer, same moment
def test_two_simultaneous_payments_for_one_customer_both_succeed(firm):
    client = TestClient(app)
    invoices = []
    for _ in range(2):
        r = client.post("/orders", headers=firm["h"], json={
            "customer_id": firm["cust"], "warehouse_id": firm["wh"], "delivery_method": "home_delivery",
            "payment_status": "pending", "items": [{"product_id": firm["prod"], "quantity": 1}]})
        assert r.status_code == 201, r.text
        invoices.append(r.json()["invoice_id"])
    with SessionLocal() as s:
        owed_before = s.get(Customer, firm["cust"]).outstanding_balance

    start = threading.Barrier(2)
    codes = []

    def pay(inv):
        c = TestClient(app, raise_server_exceptions=False)
        start.wait()
        codes.append(c.post(f"/customers/{firm['cust']}/payments", headers=firm["h"],
                            json={"amount": 50, "payment_mode": "cash", "invoice_id": inv}).status_code)

    threads = [threading.Thread(target=pay, args=(inv,)) for inv in invoices]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert codes == [201, 201], codes
    with SessionLocal() as s:
        assert s.query(CustomerPayment).filter(CustomerPayment.customer_id == firm["cust"]).count() == 2
        assert round(s.get(Customer, firm["cust"]).outstanding_balance, 2) == round(owed_before - 100, 2)
        paid = sorted(float(i.amount_paid or 0) for i in s.query(Invoice).filter(Invoice.id.in_(invoices)))
        assert paid == [50.0, 50.0]


def test_order_payment_and_customer_payment_at_the_same_moment(firm):
    """Two DIFFERENT routes for the same customer at once: POST /orders/{id}/payments and
    POST /customers/{id}/payments. Both succeed and both reduce the balance."""
    client = TestClient(app)
    made = []
    for _ in range(2):
        r = client.post("/orders", headers=firm["h"], json={
            "customer_id": firm["cust"], "warehouse_id": firm["wh"], "delivery_method": "home_delivery",
            "payment_status": "pending", "items": [{"product_id": firm["prod"], "quantity": 1}]})
        assert r.status_code == 201, r.text
        made.append(r.json())
    with SessionLocal() as s:
        owed_before = s.get(Customer, firm["cust"]).outstanding_balance

    start = threading.Barrier(2)
    results = {}

    def call(name, url, body):
        c = TestClient(app, raise_server_exceptions=False)
        start.wait()
        results[name] = c.post(url, headers=firm["h"], json=body)

    threads = [
        threading.Thread(target=call, args=("order", f"/orders/{made[0]['id']}/payments",
                                            {"amount": 40, "payment_method": "cash"})),
        threading.Thread(target=call, args=("customer", f"/customers/{firm['cust']}/payments",
                                            {"amount": 60, "payment_mode": "upi", "invoice_id": made[1]["invoice_id"]})),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert results["order"].status_code in (200, 201), results["order"].text[:300]
    assert results["customer"].status_code == 201, results["customer"].text[:300]
    with SessionLocal() as s:
        assert round(s.get(Customer, firm["cust"]).outstanding_balance, 2) == round(owed_before - 100, 2)
        paid = {i.id: float(i.amount_paid or 0) for i in s.query(Invoice).filter(
            Invoice.id.in_([made[0]["invoice_id"], made[1]["invoice_id"]]))}
        assert paid == {made[0]["invoice_id"]: 40.0, made[1]["invoice_id"]: 60.0}
