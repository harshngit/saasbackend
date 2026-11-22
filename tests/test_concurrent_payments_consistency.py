"""Concurrent payments on ONE invoice keep every figure consistent.

Live (d6b71fc): receipts of 40 + 60 on IN-0070 left invoice.amount_paid at 60 (and 4 + 6 at
4): each request moved amount_paid from a copy loaded before the other one committed. Now
payment_service.lock_for_payment row-locks and re-reads the customer and the invoices
before the checks and updates. After each race:
  invoice.amount_paid == sum of its receipts, status follows, order paid_amount (GET
  /orders/{id}) == invoice.amount_paid, customer balance == opening + billed - received.
Same route twice, two different routes, a payment link + a customer payment, and a void
racing a payment.
"""

import os
import sys
import threading
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func  # noqa: E402

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Customer, CustomerPayment, CustomerPaymentAllocation, Invoice, Organization, Product, ProductPricing, User,
    Warehouse, WarehouseStock,
)
from app.models.enums import UserRole  # noqa: E402
from app.models.invoice_payment_link import InvoicePaymentLink  # noqa: E402
from app.services import entitlement_service, invoice_payment_link_service, payment_service, role_service  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)


@pytest.fixture(autouse=True)
def _no_plan_gates(monkeypatch):
    monkeypatch.setattr(entitlement_service, "check_entitlement", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "check_limit", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "has_entitlement", lambda *a, **k: True)


@pytest.fixture
def order():
    """A firm, a customer and one unpaid order of 236 (2 x 100 + 18%) with its invoice."""
    with SessionLocal() as db:
        org = Organization(name=f"Pay {uuid.uuid4().hex[:6]}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}",
                           gst_number="27AAAAA0000A1Z5", state="Maharashtra", currency="INR")
        db.add(org)
        db.flush()
        role_service.seed_default_roles(db, org.id)
        admin = User(email=f"pay.{uuid.uuid4().hex[:8]}@example.com", name="Admin", organization_id=org.id,
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
        f = {"org": org.id, "cust": cust.id, "h": {"Authorization": f"Bearer {create_access_token(admin.id, 'admin', org.id)}"}}
        body = {"customer_id": cust.id, "warehouse_id": wh.id, "delivery_method": "home_delivery",
                "payment_status": "pending", "items": [{"product_id": p.id, "quantity": 2}]}
    r = TestClient(app).post("/orders", headers=f["h"], json=body)
    assert r.status_code == 201, r.text
    f.update(order_id=r.json()["id"], invoice_id=r.json()["invoice_id"], total=r.json()["total"])
    return f


def _race(*calls):
    start = threading.Barrier(len(calls))
    results = [None] * len(calls)

    def go(i):
        start.wait()
        results[i] = calls[i]()

    threads = [threading.Thread(target=go, args=(i,)) for i in range(len(calls))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    return results


def _post(f, url, body):
    return lambda: TestClient(app, raise_server_exceptions=False).post(url, headers=f["h"], json=body).status_code


def _assert_consistent(f, expected_paid):
    with SessionLocal() as s:
        inv = s.get(Invoice, f["invoice_id"])
        direct = s.query(func.coalesce(func.sum(CustomerPayment.amount), 0)).filter(
            CustomerPayment.invoice_id == inv.id).scalar()
        allocated = s.query(func.coalesce(func.sum(CustomerPaymentAllocation.amount), 0)).filter(
            CustomerPaymentAllocation.invoice_id == inv.id).scalar()
        receipts = s.query(func.coalesce(func.sum(CustomerPayment.amount), 0)).filter(
            CustomerPayment.customer_id == f["cust"]).scalar()
        cust = s.get(Customer, f["cust"])
        assert round(float(inv.amount_paid), 2) == expected_paid
        # A payment against an invoice is recorded either with invoice_id or as allocations.
        assert round(float(inv.amount_paid), 2) == round(float(max(direct, allocated)), 2)
        assert inv.status == payment_service.payment_status(inv)
        assert round(float(cust.total_received), 2) == round(float(receipts), 2)
        assert round(float(cust.outstanding_balance), 2) == round(
            (cust.opening_balance or 0) + (cust.total_billed or 0) - (cust.total_received or 0), 2)
    order_out = TestClient(app).get(f"/orders/{f['order_id']}", headers=f["h"]).json()
    assert round(order_out["paid_amount"], 2) == expected_paid


def test_same_route_twice_on_one_invoice(order):
    url = f"/customers/{order['cust']}/payments"
    codes = _race(_post(order, url, {"amount": 40, "payment_mode": "cash", "invoice_id": order["invoice_id"]}),
                  _post(order, url, {"amount": 60, "payment_mode": "upi", "invoice_id": order["invoice_id"]}))
    assert codes == [201, 201], codes
    _assert_consistent(order, 100.0)


def test_two_routes_on_one_invoice(order):
    codes = _race(_post(order, f"/orders/{order['order_id']}/payments", {"amount": 4, "payment_method": "cash"}),
                  _post(order, f"/customers/{order['cust']}/payments",
                        {"amount": 6, "payment_mode": "upi", "invoice_id": order["invoice_id"]}))
    assert all(c in (200, 201) for c in codes), codes
    _assert_consistent(order, 10.0)


def test_payment_link_and_customer_payment_on_one_invoice(order):
    with SessionLocal() as s:
        link = InvoicePaymentLink(organization_id=order["org"], invoice_id=order["invoice_id"],
                                  razorpay_link_id=f"plink_{uuid.uuid4().hex[:10]}", short_url="https://rzp.io/x",
                                  amount_paise=int(order["total"] * 100))
        s.add(link)
        s.commit()
        link_id = link.id

    def online():
        with SessionLocal() as db:
            invoice_payment_link_service.record_online_payment(db, link_id, f"pay_{uuid.uuid4().hex[:10]}", None, 50.0)
            db.commit()
        return 201

    codes = _race(online, _post(order, f"/customers/{order['cust']}/payments",
                                {"amount": 30, "payment_mode": "cash", "invoice_id": order["invoice_id"]}))
    assert codes == [201, 201], codes
    _assert_consistent(order, 80.0)


def test_void_racing_a_payment_on_one_invoice(order):
    url = f"/customers/{order['cust']}/payments"
    c = TestClient(app)
    assert c.post(url, headers=order["h"], json={"amount": 50, "payment_mode": "cash",
                                                 "invoice_id": order["invoice_id"]}).status_code == 201
    with SessionLocal() as s:
        first = s.query(CustomerPayment).filter(CustomerPayment.invoice_id == order["invoice_id"]).one()
        first_id = first.id

    def void():
        with SessionLocal() as db:
            payment_service.void(db, db.get(CustomerPayment, first_id))
            db.commit()
        return 200

    codes = _race(void, _post(order, url, {"amount": 20, "payment_mode": "upi", "invoice_id": order["invoice_id"]}))
    assert codes == [200, 201], codes
    _assert_consistent(order, 20.0)
