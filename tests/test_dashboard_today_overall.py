"""GET /dashboard/admin `today_overall` -- the Today's Sales card.

Today only (the organization's business day), whole organization, every dashboard filter
ignored. Orders, payments, cancels and voids go through the real API endpoints; only the
"yesterday" and midnight-boundary rows are written directly, since their timestamps cannot
be produced through the API.

Definitions under test (see app/services/dashboard_service.py):
  sales / orders   sales orders in SALE_ORDER_STATUSES (placed / processing / completed),
                   by created_at inside today; drafts and cancelled orders are not sales
  delivered_orders of those, status completed or fulfilment delivered (one per order)
  pending_orders   orders - delivered_orders
  received         customer payments (customer_payments.received_on inside today);
                   a voided payment is deleted, so it can never count
  due_amount       per today's order: total - paid on its non-void invoices, floored at 0
"""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402

from app.core import business_time  # noqa: E402
from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Customer, CustomerPayment, Delivery, Organization, Product, SalesOrder, Supplier, User, Warehouse, WarehouseStock,
)
from app.models.enums import UserRole  # noqa: E402
from app.services import dashboard_service, entitlement_service, role_service  # noqa: E402

client = TestClient(app)
ZERO = {"sales": 0.0, "received": 0.0, "due_amount": 0.0, "orders": 0, "delivered_orders": 0, "pending_orders": 0}


@pytest.fixture(scope="module", autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)


@pytest.fixture(autouse=True)
def _no_plan_gates(monkeypatch):
    monkeypatch.setattr(entitlement_service, "check_entitlement", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "check_limit", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "has_entitlement", lambda *a, **k: True)
    dashboard_service._dashboard_cache.clear()


def _firm(label):
    db = SessionLocal()
    try:
        org = Organization(name=f"{label} {uuid.uuid4().hex[:6]}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}")
        db.add(org)
        db.flush()
        role_service.seed_default_roles(db, org.id)
        admin = User(email=f"admin.{uuid.uuid4().hex[:8]}@example.com", name="Admin", organization_id=org.id,
                     system_role="admin", role=UserRole.ADMIN, password_hash=hash_password("Password123!"),
                     is_active=True)
        wh = Warehouse(organization_id=org.id, name="Main", code=f"W{uuid.uuid4().hex[:4]}", is_default=True)
        cust = Customer(organization_id=org.id, name="Shop One")
        other = Customer(organization_id=org.id, name="Shop Two")
        sup = Supplier(organization_id=org.id, name="Supplier", is_active=True)
        prod = Product(organization_id=org.id, name="Widget", sku=f"W-{uuid.uuid4().hex[:5]}", price=100.0,
                       total_inventory=1000, is_active=True)
        db.add_all([admin, wh, cust, other, sup, prod])
        db.flush()
        db.add(WarehouseStock(organization_id=org.id, warehouse_id=wh.id, product_id=prod.id, on_hand_quantity=1000))
        db.commit()
        return {"org": org.id, "wh": wh.id, "cust": cust.id, "other": other.id, "sup": sup.id, "prod": prod.id,
                "h": {"Authorization": f"Bearer {create_access_token(admin.id, 'admin', org.id)}"}}
    finally:
        db.close()


@pytest.fixture
def firm():
    return _firm("Today")


def _overall(f, **params):
    dashboard_service._dashboard_cache.clear()
    r = client.get("/dashboard/admin", headers=f["h"], params=params)
    assert r.status_code == 200, r.text
    return r.json()["today_overall"]


def _core(overall):
    return {k: overall[k] for k in ZERO}


def _order(f, qty=2, confirm=True, customer=None, **extra):
    r = client.post("/orders", headers=f["h"], json={
        "customer_id": customer or f["cust"], "warehouse_id": f["wh"],
        "items": [{"product_id": f["prod"], "quantity": qty, "unit_price": 100.0}], **extra})
    assert r.status_code == 201, r.text
    order = r.json()
    if confirm and order["status"] == "draft":
        c = client.post(f"/orders/{order['id']}/confirm", headers=f["h"])
        assert c.status_code == 200, c.text
        order = c.json()
    return order


def _today_window(f):
    db = SessionLocal()
    try:
        tz = business_time.org_timezone(db, f["org"])
    finally:
        db.close()
    today = business_time.today(tz)
    return tz, business_time.day_start(today, tz), business_time.day_end(today, tz)


def _insert_order(f, total, created_at, status="placed", fulfilment="not_started"):
    db = SessionLocal()
    try:
        o = SalesOrder(organization_id=f["org"], order_number=f"SO-{uuid.uuid4().hex[:8]}", customer_id=f["cust"],
                       status=status, fulfilment_status=fulfilment, total=total, created_at=created_at)
        db.add(o)
        db.commit()
        return o.id
    finally:
        db.close()


def _insert_payment(f, amount, received_on):
    db = SessionLocal()
    try:
        db.add(CustomerPayment(organization_id=f["org"], customer_id=f["cust"], amount=amount,
                               received_on=received_on, payment_mode="cash"))
        db.commit()
    finally:
        db.close()


# 1 ---------------------------------------------------------------------------------------
def test_1_no_transaction_today_is_all_zero(firm):
    overall = _overall(firm)
    assert _core(overall) == ZERO
    assert overall["timezone"] == "Asia/Kolkata"


# 2 ---------------------------------------------------------------------------------------
def test_2_an_order_created_today_through_the_api_appears(firm):
    draft = _order(firm, qty=2, confirm=False)
    assert draft["status"] == "draft"
    assert _core(_overall(firm)) == ZERO  # a draft is not a sale (no stock committed)

    placed = client.post(f"/orders/{draft['id']}/confirm", headers=firm["h"])
    assert placed.status_code == 200, placed.text
    total = placed.json()["total"]
    assert total == 200.0
    overall = _overall(firm)
    assert _core(overall) == {**ZERO, "sales": 200.0, "orders": 1, "pending_orders": 1, "due_amount": 200.0}

    # A paid takeaway order in one call: order + invoice + payment.
    paid = _order(firm, qty=1, delivery_method="takeaway", payment_status="paid", paid_amount=100.0,
                  payment_method="cash")
    assert paid["status"] != "draft"
    overall = _overall(firm)
    assert overall["sales"] == 300.0 and overall["orders"] == 2
    assert overall["received"] == 100.0          # the paid order's payment, counted once
    assert overall["due_amount"] == 200.0        # 200 unpaid on the first order, 0 on the paid one


# 3 ---------------------------------------------------------------------------------------
def test_3_yesterday_does_not_count(firm):
    tz, start, _ = _today_window(firm)
    yesterday = start - timedelta(hours=3)
    _insert_order(firm, 999.0, yesterday)
    _insert_payment(firm, 555.0, yesterday)
    assert _core(_overall(firm)) == ZERO


# 4 + 5 -----------------------------------------------------------------------------------
def test_4_and_5_no_dashboard_filter_changes_today_overall(firm):
    _order(firm, qty=3)                                   # 300, customer 1
    _order(firm, qty=1, customer=firm["other"])           # 100, customer 2
    client.post(f"/customers/{firm['cust']}/payments", headers=firm["h"], json={"amount": 50.0})
    base = _overall(firm)
    assert base["sales"] == 400.0 and base["orders"] == 2 and base["received"] == 50.0

    today = datetime.now(timezone.utc).date()
    last_month_end = today.replace(day=1) - timedelta(days=1)
    variants = [
        {"date_from": last_month_end.replace(day=1).isoformat(), "date_to": last_month_end.isoformat()},
        {"date_from": "2024-01-01", "date_to": "2024-01-31"},
        {"customer_id": firm["cust"]},
        {"customer_ids": firm["other"]},
        {"warehouse_id": firm["wh"]},
        {"supplier_id": firm["sup"]},
        {"company_id": firm["org"]},
        {"customer_id": firm["cust"], "warehouse_id": firm["wh"], "supplier_id": firm["sup"],
         "date_from": "2024-01-01", "date_to": "2024-01-31"},
    ]
    for params in variants:
        assert _overall(firm, **params) == base, params


# 6 ---------------------------------------------------------------------------------------
def test_6_a_cancelled_order_is_not_a_sale(firm):
    keep = _order(firm, qty=2)
    gone = _order(firm, qty=5)
    assert _overall(firm)["sales"] == 700.0
    r = client.patch(f"/orders/{gone['id']}/cancel", headers=firm["h"], json={"reason": "customer changed mind"})
    assert r.status_code == 200, r.text
    overall = _overall(firm)
    assert overall["sales"] == 200.0 and overall["orders"] == 1 and overall["due_amount"] == 200.0
    assert keep["total"] == 200.0


# 7 + 8 -----------------------------------------------------------------------------------
def test_7_and_8_payment_counts_once_and_a_voided_one_not_at_all(firm):
    r = client.post(f"/customers/{firm['cust']}/payments", headers=firm["h"], json={"amount": 120.0, "payment_mode": "upi"})
    assert r.status_code == 201, r.text
    assert _overall(firm)["received"] == 120.0

    db = SessionLocal()
    try:  # exactly one row was written for the one payment (splits / receipts are not extra rows)
        pay_id = db.query(CustomerPayment).filter(CustomerPayment.customer_id == firm["cust"]).one().id
    finally:
        db.close()

    v = client.delete(f"/customers/{firm['cust']}/payments/{pay_id}", headers=firm["h"])
    assert v.status_code == 200, v.text
    assert _overall(firm)["received"] == 0.0


# 9 ---------------------------------------------------------------------------------------
def test_9_ist_midnight_boundaries(firm):
    tz, start, end = _today_window(firm)
    assert str(tz) == "Asia/Kolkata"
    # 00:00:00 IST today = 18:30 UTC yesterday
    assert start.utcoffset() == timedelta(0) and (start + timedelta(hours=5, minutes=30)).time().isoformat() == "00:00:00"
    tick = timedelta(microseconds=1)
    _insert_order(firm, 1.0, start)            # first instant of today: in
    _insert_order(firm, 10.0, end)             # last instant of today: in
    _insert_order(firm, 100.0, start - tick)   # last instant of yesterday: out
    _insert_order(firm, 1000.0, end + tick)    # first instant of tomorrow: out
    _insert_payment(firm, 2.0, start)
    _insert_payment(firm, 20.0, end)
    _insert_payment(firm, 200.0, start - tick)
    _insert_payment(firm, 2000.0, end + tick)
    overall = _overall(firm)
    assert overall["sales"] == 11.0 and overall["orders"] == 2
    assert overall["received"] == 22.0


# 10 --------------------------------------------------------------------------------------
def test_10_another_organization_has_no_effect(firm):
    other = _firm("Other")
    _order(other, qty=9)
    client.post(f"/customers/{other['cust']}/payments", headers=other["h"], json={"amount": 77.0})
    assert _core(_overall(firm)) == ZERO
    theirs = _overall(other)
    assert theirs["sales"] == 900.0 and theirs["received"] == 77.0


# delivered / pending ---------------------------------------------------------------------
def test_delivered_and_pending_count_each_order_once(firm):
    tz, start, _ = _today_window(firm)
    now = datetime.now(timezone.utc)
    done = _insert_order(firm, 100.0, now, status="processing", fulfilment="delivered")
    _insert_order(firm, 50.0, now, status="completed", fulfilment="delivered")
    _insert_order(firm, 30.0, now, status="processing", fulfilment="partially_delivered")
    _insert_order(firm, 20.0, now, status="placed", fulfilment="reserved")
    db = SessionLocal()
    try:  # two delivered deliveries for the same order must not count it twice
        for _ in range(2):
            db.add(Delivery(organization_id=firm["org"], delivery_note_number=f"DN-{uuid.uuid4().hex[:6]}",
                            sales_order_id=done, status="delivered", confirmed_at=now))
        db.commit()
    finally:
        db.close()
    overall = _overall(firm)
    assert overall["orders"] == 4 and overall["delivered_orders"] == 2 and overall["pending_orders"] == 2
    assert overall["delivered_amount"] == 150.0 and overall["pending_amount"] == 50.0
