"""Payment lock order: collection -> customer -> invoices (id order) -> counters.

- two collections allocated to the same two invoices in OPPOSITE order, reconciled at the
  same moment: no deadlock, both succeed, each invoice gets both amounts;
- the same collection reconciled twice at once: one payment, the other request refused;
- the same collection voided twice at once: the payment is reversed once;
- the invoice row locks are one statement ordered by id;
- org isolation (another firm's admin: 404) and the existing role check (delivery partner: 403).
"""

import os
import re
import sys
import threading
import time
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event  # noqa: E402

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Customer, CustomerPayment, DeliveryCollection, DeliveryCollectionAllocation, Invoice, Organization, Product,
    ProductPricing, User, Warehouse, WarehouseStock,
)
from app.models.enums import UserRole  # noqa: E402
from app.services import entitlement_service, role_service  # noqa: E402


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
        org = Organization(name=f"LO {uuid.uuid4().hex[:6]}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}",
                           gst_number="27AAAAA0000A1Z5", state="Maharashtra", currency="INR")
        db.add(org)
        db.flush()
        role_service.seed_default_roles(db, org.id)
        admin = User(email=f"lo.{uuid.uuid4().hex[:8]}@example.com", name="Admin", organization_id=org.id,
                     system_role="admin", role=UserRole.ADMIN, password_hash=hash_password("Password123!"),
                     is_active=True)
        partner = User(email=f"lodp.{uuid.uuid4().hex[:8]}@example.com", name="Driver", organization_id=org.id,
                       system_role="staff", role=UserRole.DELIVERY_PARTNER, password_hash=hash_password("Password123!"),
                       is_active=True)
        wh = Warehouse(organization_id=org.id, name="Main", code=f"W{uuid.uuid4().hex[:4]}", is_default=True)
        cust = Customer(organization_id=org.id, name="Shop", gst_number="27BBBBB1111B1Z5", state="Maharashtra",
                        delivery_address="Addr")
        db.add_all([admin, partner, wh, cust])
        db.flush()
        p = Product(organization_id=org.id, name="Item", sku=f"I-{uuid.uuid4().hex[:5]}", price=100, tax_rate=0,
                    hsn_code="8544", total_inventory=0, is_active=True)
        p.pricing = ProductPricing(selling_price=100, purchase_price=50, tax_inclusive=False, currency="INR")
        db.add(p)
        db.flush()
        db.add(WarehouseStock(organization_id=org.id, warehouse_id=wh.id, product_id=p.id, on_hand_quantity=100))
        db.commit()
        wh_id, product_id = wh.id, p.id
        out = {"org": org.id, "cust": cust.id, "partner": partner.id,
               "h": {"Authorization": f"Bearer {create_access_token(admin.id, 'admin', org.id)}"},
               "ph": {"Authorization": f"Bearer {create_access_token(partner.id, 'staff', org.id)}"}}
    invoices = []
    for _ in range(2):
        r = TestClient(app).post("/orders", headers=out["h"], json={
            "customer_id": out["cust"], "warehouse_id": wh_id, "delivery_method": "home_delivery",
            "payment_status": "pending", "items": [{"product_id": product_id, "quantity": 1}]})
        assert r.status_code == 201, r.text
        invoices.append(r.json()["invoice_id"])
    out["invoices"] = invoices          # each 100.00
    return out


def _collection(f, allocations):
    with SessionLocal() as s:
        coll = DeliveryCollection(organization_id=f["org"], customer_id=f["cust"], delivery_partner_id=f["partner"],
                                  amount=sum(a for _, a in allocations), payment_mode="cash")
        s.add(coll)
        s.flush()
        for inv, amount in allocations:
            s.add(DeliveryCollectionAllocation(organization_id=f["org"], delivery_collection_id=coll.id,
                                               invoice_id=inv, amount=amount))
        s.commit()
        return coll.id


@pytest.fixture
def slow():
    def _slow(*_a):
        time.sleep(0.01)

    event.listen(engine, "before_cursor_execute", _slow)
    yield
    event.remove(engine, "before_cursor_execute", _slow)


def _race(*calls):
    start = threading.Barrier(len(calls))
    out = [None] * len(calls)

    def go(i):
        start.wait()
        out[i] = calls[i]()

    threads = [threading.Thread(target=go, args=(i,)) for i in range(len(calls))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    return out


def _reconcile(f, coll_id, headers=None):
    return lambda: TestClient(app, raise_server_exceptions=False).post(
        f"/deliveries/collections/{coll_id}/reconcile", headers=headers or f["h"]).status_code


def _paid(f):
    with SessionLocal() as s:
        return [round(float(s.get(Invoice, i).amount_paid or 0), 2) for i in f["invoices"]]


def test_two_payments_locking_the_same_invoices_in_opposite_order(slow):
    f = _firm()
    a, b = f["invoices"]
    first = _collection(f, [(a, 30), (b, 20)])
    second = _collection(f, [(b, 15), (a, 25)])
    codes = _race(_reconcile(f, first), _reconcile(f, second))
    assert codes == [200, 200], codes
    assert _paid(f) == [55.0, 35.0]
    with SessionLocal() as s:
        cust = s.get(Customer, f["cust"])
        assert round(cust.total_received, 2) == 90.0


def test_one_collection_reconciled_twice_at_once_records_one_payment(slow):
    f = _firm()
    coll = _collection(f, [(f["invoices"][0], 40)])
    codes = sorted(_race(_reconcile(f, coll), _reconcile(f, coll)))
    assert codes == [200, 400], codes
    with SessionLocal() as s:
        assert s.query(CustomerPayment).filter(CustomerPayment.customer_id == f["cust"]).count() == 1
    assert _paid(f) == [40.0, 0.0]


def test_one_collection_voided_twice_at_once_reverses_once(slow):
    f = _firm()
    coll = _collection(f, [(f["invoices"][0], 40)])
    assert _reconcile(f, coll)() == 200
    void = lambda: TestClient(app, raise_server_exceptions=False).post(  # noqa: E731
        f"/deliveries/collections/{coll}/void", headers=f["h"]).status_code
    codes = sorted(_race(void, void))
    assert codes == [200, 400], codes
    assert _paid(f) == [0.0, 0.0]
    with SessionLocal() as s:
        assert round(s.get(Customer, f["cust"]).total_received or 0, 2) == 0.0


def test_invoice_rows_are_locked_in_one_statement_in_id_order():
    f = _firm()
    a, b = f["invoices"]
    coll = _collection(f, [(b, 10), (a, 10)])
    stmts = []

    def _cap(conn, cur, statement, *x):
        stmts.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", _cap)
    try:
        assert _reconcile(f, coll)() == 200
    finally:
        event.remove(engine, "before_cursor_execute", _cap)
    invoice_locks = [s for s in stmts if re.search(r"FROM invoices\b", s) and "FOR UPDATE" in s]
    assert invoice_locks and all("ORDER BY invoices.id" in s for s in invoice_locks), invoice_locks
    first_counter = next(i for i, s in enumerate(stmts) if s.startswith("UPDATE number_sequences"))
    first_customer_lock = next(i for i, s in enumerate(stmts) if "FROM customers" in s and "FOR UPDATE" in s)
    first_invoice_lock = stmts.index(invoice_locks[0])
    first_collection_lock = next(i for i, s in enumerate(stmts) if "FROM delivery_collections" in s and "FOR UPDATE" in s)
    assert first_collection_lock < first_customer_lock < first_invoice_lock < first_counter


def test_other_firm_and_delivery_partner_cannot_reconcile():
    f, other = _firm(), _firm()
    coll = _collection(f, [(f["invoices"][0], 10)])
    assert _reconcile(f, coll, other["h"])() == 404
    assert _reconcile(f, coll, f["ph"])() == 403
    assert _paid(f) == [0.0, 0.0]
