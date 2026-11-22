"""Order create is all-or-nothing, announces nothing until it commits, and does not
deadlock with itself or with a customer payment.

1. A payment failure (400) after the stock was reserved: no order, reservation, delivery,
   invoice, journal entry or notification is saved, and no realtime event or push goes out.
2. Simultaneous orders for the same product (same stock rows, same number counters): all
   succeed, numbers never repeat, reservations add up.
3. An order and a customer payment for the same customer at the same moment: no deadlock.
   (Order create takes the customer row before the journal-entry counter; the payment flow
   used to take the counters first, so the two could each wait for the other.)
"""

import os
import sys
import threading
import time
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event  # noqa: E402

from app.core import realtime  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Customer, Delivery, Invoice, Notification, Organization, Product, ProductPricing, SalesOrder,
    StockReservation, User, Warehouse, WarehouseStock,
)
from app.models.accounting import JournalEntry  # noqa: E402
from app.models.enums import UserRole  # noqa: E402
from app.services import entitlement_service, payment_service, push_service, role_service  # noqa: E402


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
    db = SessionLocal()
    try:
        org = Organization(name=f"Atom {uuid.uuid4().hex[:6]}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}",
                           gst_number="27AAAAA0000A1Z5", state="Maharashtra", currency="INR")
        db.add(org)
        db.flush()
        role_service.seed_default_roles(db, org.id)
        admin = User(email=f"atom.{uuid.uuid4().hex[:8]}@example.com", name="Admin", organization_id=org.id,
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
        return {"org": org.id, "wh": wh.id, "cust": cust.id, "prod": p.id,
                "h": {"Authorization": f"Bearer {create_access_token(admin.id, 'admin', org.id)}"}}
    finally:
        db.close()


def _order(f, qty=1, **extra):
    return {"customer_id": f["cust"], "warehouse_id": f["wh"], "delivery_method": "home_delivery",
            "payment_status": "pending", "items": [{"product_id": f["prod"], "quantity": qty}], **extra}


def _counts(f):
    with SessionLocal() as s:
        org = f["org"]
        return {
            "orders": s.query(SalesOrder).filter(SalesOrder.organization_id == org).count(),
            "reservations": s.query(StockReservation).filter(StockReservation.organization_id == org).count(),
            "deliveries": s.query(Delivery).filter(Delivery.organization_id == org).count(),
            "invoices": s.query(Invoice).filter(Invoice.organization_id == org).count(),
            "journal": s.query(JournalEntry).filter(JournalEntry.organization_id == org).count(),
            "notifications": s.query(Notification).filter(Notification.organization_id == org).count(),
        }


# ------------------------------------------------------------- 1. all-or-nothing
def test_payment_failure_saves_nothing_and_announces_nothing(firm, monkeypatch):
    emitted, pushed = [], []
    monkeypatch.setattr(realtime.manager, "emit", lambda *a, **k: emitted.append(a or k))
    monkeypatch.setattr(push_service, "_dispatch", lambda ids: pushed.append(ids))
    monkeypatch.setattr(settings, "push_notifications_enabled", True)

    def refuse(*a, **k):
        raise ValueError("Payment gateway said no")

    monkeypatch.setattr(payment_service, "record", refuse)
    before = _counts(firm)
    r = TestClient(app).post("/orders", headers=firm["h"],
                             json=_order(firm, 2, payment_status="paid", paid_amount=236, payment_method="cash"))
    assert r.status_code == 400 and "gateway" in r.text
    assert _counts(firm) == before                              # nothing saved, stock not reserved
    with SessionLocal() as s:
        row = s.query(WarehouseStock).filter(WarehouseStock.product_id == firm["prod"]).one()
        assert row.on_hand_quantity == 100
    assert emitted == [] and pushed == []                       # no realtime event, no push


def test_successful_order_announces_after_commit(firm, monkeypatch):
    emitted = []
    monkeypatch.setattr(realtime.manager, "emit", lambda *a, **k: emitted.append(k.get("event_name") or a[1]))
    r = TestClient(app).post("/orders", headers=firm["h"], json=_order(firm, 2))
    assert r.status_code == 201, r.text
    assert "inventory.stock_updated" in emitted
    assert _counts(firm)["orders"] == 1


# -------------------------------------------------------- 2. simultaneous orders
def _parallel(calls):
    start = threading.Barrier(len(calls))
    out = [None] * len(calls)

    def go(i):
        client = TestClient(app, raise_server_exceptions=False)
        start.wait()
        method, url, kw = calls[i]
        out[i] = getattr(client, method)(url, **kw)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(len(calls))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=180)
    return out


@pytest.fixture
def slow_queries():
    """A few ms per statement so concurrent requests really overlap (as over a WAN)."""
    def _slow(*_a):
        time.sleep(0.01)

    event.listen(engine, "before_cursor_execute", _slow)
    yield
    event.remove(engine, "before_cursor_execute", _slow)


def test_three_simultaneous_orders_same_product_and_counters(firm, slow_queries):
    rs = _parallel([("post", "/orders", {"json": _order(firm, 5), "headers": firm["h"]})] * 3)
    assert [r.status_code for r in rs] == [201, 201, 201], [r.text[:200] for r in rs]
    bodies = [r.json() for r in rs]
    assert len({b["order_number"] for b in bodies}) == 3
    with SessionLocal() as s:
        invs = [i.invoice_number for i in s.query(Invoice).filter(Invoice.organization_id == firm["org"])]
        dels = [d.delivery_note_number for d in s.query(Delivery).filter(Delivery.organization_id == firm["org"])]
        reserved = sum(r.reserved_quantity for r in s.query(StockReservation)
                       .filter(StockReservation.organization_id == firm["org"], StockReservation.status == "active"))
    assert len(invs) == len(set(invs)) == 3 and len(dels) == len(set(dels)) == 3
    assert reserved == 15


# ------------------------------------------ 3. order + payment, same customer, same time
def test_order_and_payment_for_same_customer_do_not_deadlock(firm, slow_queries):
    client = TestClient(app)
    invoices = []
    for _ in range(3):
        r = client.post("/orders", headers=firm["h"], json=_order(firm, 1))
        assert r.status_code == 201, r.text
        invoices.append(r.json()["invoice_id"])
    for inv in invoices:
        rs = _parallel([
            ("post", "/orders", {"json": _order(firm, 1), "headers": firm["h"]}),
            ("post", f"/customers/{firm['cust']}/payments",
             {"json": {"amount": 50, "payment_mode": "cash", "invoice_id": inv}, "headers": firm["h"]}),
        ])
        assert [r.status_code for r in rs] == [201, 201], [r.text[:300] for r in rs]


# ------------------------------------------- savepoints do not release queued events
def test_events_wait_for_the_outer_commit_not_a_savepoint(monkeypatch):
    """SQLAlchemy fires after_commit / after_rollback when a savepoint (begin_nested) ends,
    too. Queued realtime events must go out only when the outer transaction commits, and a
    rolled-back savepoint must not throw away events queued for the whole transaction."""
    from sqlalchemy import text

    emitted = []
    monkeypatch.setattr(realtime.manager, "emit", lambda *a, **k: emitted.append(k.get("event_name")))
    with SessionLocal() as db:
        db.execute(text("SELECT 1"))
        realtime.queue_event(db, org_id="org", event_name="before.savepoint", data={})
        with db.begin_nested():
            db.execute(text("SELECT 1"))
        assert emitted == []                                    # savepoint released: still queued
        try:
            with db.begin_nested():
                realtime.queue_event(db, org_id="org", event_name="inside.savepoint", data={})
                raise ValueError
        except ValueError:
            pass
        assert emitted == []
        db.commit()
    assert emitted == ["before.savepoint", "inside.savepoint"]    # once, at the outer commit
    with SessionLocal() as db:
        db.execute(text("SELECT 1"))
        realtime.queue_event(db, org_id="org", event_name="rolled.back", data={})
        db.rollback()
        db.commit()
    assert "rolled.back" not in emitted


def test_order_and_payment_interleaved_at_the_worst_moment(firm):
    """Deterministic version of the race: the order pauses holding the customer row just
    before it takes the journal-entry counter; the payment pauses just before it updates
    the customer row. With the old payment lock order (counters first) this is a deadlock
    and one request fails; with the customer row taken first, the payment simply waits."""
    client = TestClient(app)
    first = client.post("/orders", headers=firm["h"], json=_order(firm, 1))
    assert first.status_code == 201, first.text
    inv = first.json()["invoice_id"]

    order_at_je, payment_at_customer = threading.Event(), threading.Event()

    def _pause(conn, cur, statement, params, ctx, many):
        # Requests run on worker threads, so each request is told apart by its connection:
        # the order takes the SO counter, the payment the RCPT counter.
        s = " ".join(statement.split())
        series = next((v for k, v in params.items() if str(k).startswith("series")), None) \
            if isinstance(params, dict) else None
        if s.startswith("UPDATE number_sequences") and series in ("SO", "RCPT"):
            conn.info["race_role"] = "order" if series == "SO" else "payment"
        role = conn.info.get("race_role")
        if role == "order" and s.startswith("UPDATE number_sequences") and series == "JE":
            order_at_je.set()
            payment_at_customer.wait(timeout=3)
        elif role == "payment" and s.startswith("UPDATE customers"):
            payment_at_customer.set()
            order_at_je.wait(timeout=3)

    results = {}

    def run(role, method, url, body):
        results[role] = getattr(TestClient(app, raise_server_exceptions=False), method)(
            url, json=body, headers=firm["h"])

    def _forget(conn, *_a):                 # pooled connections keep conn.info: reset per transaction
        conn.info.pop("race_role", None)

    event.listen(engine, "before_cursor_execute", _pause)
    event.listen(engine, "commit", _forget)
    event.listen(engine, "rollback", _forget)
    try:
        t1 = threading.Thread(target=run, name="order", args=("order", "post", "/orders", _order(firm, 1)))
        t2 = threading.Thread(target=run, name="payment", args=(
            "payment", "post", f"/customers/{firm['cust']}/payments",
            {"amount": 50, "payment_mode": "cash", "invoice_id": inv}))
        t1.start(); time.sleep(0.2); t2.start()
        t1.join(timeout=60); t2.join(timeout=60)
    finally:
        event.remove(engine, "before_cursor_execute", _pause)
        event.remove(engine, "commit", _forget)
        event.remove(engine, "rollback", _forget)
    assert order_at_je.is_set(), "the order never reached the journal counter: the race was not set up"
    assert results["order"].status_code == 201, results["order"].text[:300]
    assert results["payment"].status_code == 201, results["payment"].text[:300]


# ------------------------------------------------- stage timings in Server-Timing
def test_order_create_reports_its_stages_in_server_timing(firm):
    import re

    for body in (_order(firm, 2), _order(firm, 1, payment_status="paid", paid_amount=118, payment_method="cash")):
        r = TestClient(app).post("/orders", headers=firm["h"], json=body)
        assert r.status_code == 201, r.text
        header = r.headers["server-timing"]
        stages = dict(re.findall(r'(\w+);dur=[\d.]+;desc="queries=(\d+)"', header))
        total = int(stages.pop("db"))
        for name in ("validate", "place", "reserve", "delivery_plan", "invoice", "payment", "pickup", "commit",
                     "response"):
            assert name in stages, (name, header)
        # Every query of the request falls in exactly one stage, except the auth lookup
        # made by the dependencies before the endpoint starts.
        assert 0 <= total - sum(int(v) for v in stages.values()) <= 3, header
