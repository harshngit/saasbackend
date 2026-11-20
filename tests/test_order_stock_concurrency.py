"""Order create stock safety (written before batching the per-line stock queries, and
run against both the old and the new code):

1. Concurrency: several orders for the same stock at the same moment never reserve more
   than is available -- exactly as many succeed as the stock covers.
2. Negative stock: an order for more than is available is refused (also when two lines of
   the same product only together go over), and nothing is reserved or moved.
3. Rollback: a failure after the stock was reserved leaves no order, reservation,
   delivery or invoice behind.
"""

import os
import sys
import threading
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Customer, Delivery, Invoice, Organization, Product, ProductPricing, SalesOrder, StockReservation, User,
    Warehouse, WarehouseStock,
)
from app.models.enums import UserRole  # noqa: E402
from app.services import delivery_service, entitlement_service, role_service  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)


@pytest.fixture(autouse=True)
def _no_plan_gates(monkeypatch):
    monkeypatch.setattr(entitlement_service, "check_entitlement", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "check_limit", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "has_entitlement", lambda *a, **k: True)


@pytest.fixture
def w():
    """A firm with one warehouse, one customer and products A (stock 10), B (stock 4), C (stock 50)."""
    db = SessionLocal()
    try:
        org = Organization(name=f"Conc {uuid.uuid4().hex[:6]}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}",
                           gst_number="27AAAAA0000A1Z5", state="Maharashtra", currency="INR")
        db.add(org)
        db.flush()
        role_service.seed_default_roles(db, org.id)
        admin = User(email=f"conc.{uuid.uuid4().hex[:8]}@example.com", name="Admin", organization_id=org.id,
                     system_role="admin", role=UserRole.ADMIN, password_hash=hash_password("Password123!"),
                     is_active=True)
        wh = Warehouse(organization_id=org.id, name="Main", code=f"W{uuid.uuid4().hex[:4]}", is_default=True)
        cust = Customer(organization_id=org.id, name="Shop", gst_number="27BBBBB1111B1Z5", state="Maharashtra",
                        delivery_address="Addr 1")
        db.add_all([admin, wh, cust])
        db.flush()
        ids = {"org": org.id, "wh": wh.id, "cust": cust.id}
        for key, stock in (("A", 10), ("B", 4), ("C", 50)):
            p = Product(organization_id=org.id, name=f"Item {key}", sku=f"{key}-{uuid.uuid4().hex[:5]}", price=100,
                        tax_rate=18, hsn_code="8544", total_inventory=stock, is_active=True)
            p.pricing = ProductPricing(selling_price=100, purchase_price=50, tax_inclusive=False, currency="INR")
            db.add(p)
            db.flush()
            db.add(WarehouseStock(organization_id=org.id, warehouse_id=wh.id, product_id=p.id, on_hand_quantity=stock))
            ids[key] = p.id
        db.commit()
        ids["h"] = {"Authorization": f"Bearer {create_access_token(admin.id, 'admin', org.id)}"}
        return ids
    finally:
        db.close()


def _body(w, lines):
    return {"customer_id": w["cust"], "warehouse_id": w["wh"], "delivery_method": "home_delivery",
            "payment_status": "pending", "items": [{"product_id": w[k], "quantity": q} for k, q in lines]}


def _state(w):
    """(on_hand, active reserved) per product, plus counts of orders / deliveries / invoices."""
    s = SessionLocal()
    try:
        out = {}
        for key in ("A", "B", "C"):
            row = s.query(WarehouseStock).filter(WarehouseStock.warehouse_id == w["wh"],
                                                 WarehouseStock.product_id == w[key]).one()
            reserved = sum(r.outstanding_quantity for r in s.query(StockReservation).filter(
                StockReservation.product_id == w[key], StockReservation.status == "active"))
            out[key] = (row.on_hand_quantity, reserved)
        out["orders"] = s.query(SalesOrder).filter(SalesOrder.organization_id == w["org"]).count()
        out["deliveries"] = s.query(Delivery).filter(Delivery.organization_id == w["org"]).count()
        out["invoices"] = s.query(Invoice).filter(Invoice.organization_id == w["org"]).count()
        return out
    finally:
        s.close()


def _parallel(w, bodies):
    """Send all bodies at the same moment, one thread and client each. Returns status codes."""
    start = threading.Barrier(len(bodies))
    codes: list[int] = [0] * len(bodies)

    def go(i):
        c = TestClient(app, raise_server_exceptions=False)
        start.wait()
        codes[i] = c.post("/orders", headers=w["h"], json=bodies[i]).status_code

    threads = [threading.Thread(target=go, args=(i,)) for i in range(len(bodies))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    return codes


# ------------------------------------------------------------------- 1. concurrency
def test_parallel_orders_never_reserve_more_than_the_stock(w):
    codes = _parallel(w, [_body(w, [("A", 3)]) for _ in range(5)])      # 5 x 3 against 10
    assert sorted(codes) == [201, 201, 201, 400, 400], codes
    st = _state(w)
    assert st["A"] == (10, 9) and st["orders"] == 3


def test_parallel_multi_line_orders_in_opposite_order_do_not_deadlock(w):
    # Same two products, lines listed in opposite order: deterministic locking must not deadlock.
    bodies = [_body(w, [("A", 2), ("C", 5)]), _body(w, [("C", 5), ("A", 2)])] * 3
    codes = _parallel(w, bodies)
    assert sorted(codes) == [201, 201, 201, 201, 201, 400], codes        # A: 10 covers 5 orders of 2
    st = _state(w)
    assert st["A"] == (10, 10) and st["C"] == (50, 25)


# ---------------------------------------------------------------- 2. negative stock
def test_order_over_available_is_refused_and_changes_nothing(w):
    before = _state(w)
    r = TestClient(app).post("/orders", headers=w["h"], json=_body(w, [("B", 5)]))   # B has 4
    assert r.status_code == 400 and r.json()["detail"]["error"] == "INSUFFICIENT_STOCK"
    short = r.json()["detail"]["shortages"][0]
    assert (short["required_quantity"], short["available_quantity"], short["short_quantity"]) == (5, 4, 1)
    assert _state(w) == before


def test_two_lines_of_one_product_are_checked_together(w):
    before = _state(w)
    r = TestClient(app).post("/orders", headers=w["h"], json=_body(w, [("B", 3), ("C", 1), ("B", 2)]))
    assert r.status_code == 400 and r.json()["detail"]["shortages"][0]["required_quantity"] == 5
    assert _state(w) == before


def test_reserved_stock_is_not_available_to_the_next_order(w):
    c = TestClient(app)
    assert c.post("/orders", headers=w["h"], json=_body(w, [("B", 4)])).status_code == 201
    r = c.post("/orders", headers=w["h"], json=_body(w, [("B", 1)]))
    assert r.status_code == 400 and r.json()["detail"]["shortages"][0]["available_quantity"] == 0
    assert _state(w)["B"] == (4, 4)


# ---------------------------------------------------------------------- 3. rollback
def test_failure_after_reserving_rolls_everything_back(w, monkeypatch):
    before = _state(w)

    def boom(*a, **k):
        raise RuntimeError("delivery planning failed")

    monkeypatch.setattr(delivery_service, "plan", boom)
    r = TestClient(app, raise_server_exceptions=False).post("/orders", headers=w["h"],
                                                            json=_body(w, [("A", 2), ("C", 3)]))
    assert r.status_code == 500
    assert _state(w) == before                    # no order, reservation, delivery or invoice


def test_successful_order_reserves_each_line_once(w):
    r = TestClient(app).post("/orders", headers=w["h"], json=_body(w, [("A", 2), ("B", 1), ("C", 7)]))
    assert r.status_code == 201, r.text
    st = _state(w)
    assert (st["A"], st["B"], st["C"]) == ((10, 2), (4, 1), (50, 7))
    assert (st["orders"], st["deliveries"], st["invoices"]) == (1, 1, 1)
