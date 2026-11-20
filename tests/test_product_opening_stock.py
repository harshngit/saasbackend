"""Opening stock on product create.

POST /products with opening_stock > 0 puts that stock into the firm's default warehouse:
one warehouse_stock row + one "opening" stock movement, and the catalog counter follows.
Editing the product never posts it again. Products with variants, serial tracking or
inventory tracking off are skipped. (There is no server-side product import: a bulk
import is the client calling POST /products per row, so it gets exactly this behaviour.)
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
from app.models import Organization, Product, StockMovement, User, Warehouse, WarehouseStock  # noqa: E402
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


def _firm(with_warehouse=True):
    db = SessionLocal()
    try:
        org = Organization(name=f"Open {uuid.uuid4().hex[:6]}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}",
                           state="Maharashtra", currency="INR")
        db.add(org)
        db.flush()
        role_service.seed_default_roles(db, org.id)
        admin = User(email=f"open.{uuid.uuid4().hex[:8]}@example.com", name="Admin", organization_id=org.id,
                     system_role="admin", role=UserRole.ADMIN, password_hash=hash_password("Password123!"),
                     is_active=True)
        db.add(admin)
        out = {"org": org.id}
        if with_warehouse:
            other = Warehouse(organization_id=org.id, name="Second", code=f"S{uuid.uuid4().hex[:4]}", is_default=False)
            wh = Warehouse(organization_id=org.id, name="Main", code=f"W{uuid.uuid4().hex[:4]}", is_default=True)
            db.add_all([other, wh])
            db.flush()
            out.update(wh=wh.id, other=other.id)
        db.commit()
        out["h"] = {"Authorization": f"Bearer {create_access_token(admin.id, 'admin', org.id)}"}
        return out
    finally:
        db.close()


def _create(f, **extra):
    body = {"name": f"P {uuid.uuid4().hex[:5]}", "sku": f"S-{uuid.uuid4().hex[:6]}",
            "pricing": {"selling_price": 10, "purchase_price": 5, "currency": "INR"}, **extra}
    r = client.post("/products", json=body, headers=f["h"])
    assert r.status_code == 201, r.text
    return r.json()


def _stock(product_id):
    with SessionLocal() as db:
        rows = {r.warehouse_id: r.on_hand_quantity for r in
                db.query(WarehouseStock).filter(WarehouseStock.product_id == product_id)}
        moves = [(m.warehouse_id, m.movement_type, m.quantity) for m in
                 db.query(StockMovement).filter(StockMovement.product_id == product_id)]
        counter = db.get(Product, product_id).total_inventory
        return rows, moves, counter


def test_create_posts_opening_stock_into_default_warehouse():
    f = _firm()
    p = _create(f, opening_stock=25)
    rows, moves, counter = _stock(p["id"])
    assert rows == {f["wh"]: 25}
    assert moves == [(f["wh"], "opening", 25)]
    assert counter == 25 and p["total_inventory"] == 25


def test_counter_sent_with_opening_stock_is_not_counted_twice():
    f = _firm()
    p = _create(f, opening_stock=10, total_inventory=10)
    rows, moves, counter = _stock(p["id"])
    assert rows == {f["wh"]: 10} and counter == 10 and len(moves) == 1


def test_edit_never_posts_opening_stock_again():
    f = _firm()
    p = _create(f, opening_stock=8)
    for body in ({"name": "Renamed"}, {"opening_stock": 8}, {"opening_stock": 30}, {"description": "x"}):
        r = client.patch(f"/products/{p['id']}", json=body, headers=f["h"])
        assert r.status_code == 200, r.text
    rows, moves, counter = _stock(p["id"])
    assert rows == {f["wh"]: 8} and moves == [(f["wh"], "opening", 8)] and counter == 8


def test_no_opening_stock_means_no_stock_rows():
    f = _firm()
    for extra in ({}, {"opening_stock": 0}):
        rows, moves, _ = _stock(_create(f, **extra)["id"])
        assert rows == {} and moves == []


@pytest.mark.parametrize("extra", [
    {"inventory_tracking": False},
    {"serial_number_tracking": True},
    {"variations": [{"name": "Red", "sku": f"R-{uuid.uuid4().hex[:5]}", "inventory": 0}]},
])
def test_skipped_kinds_post_nothing(extra):
    f = _firm()
    rows, moves, _ = _stock(_create(f, opening_stock=5, **extra)["id"])
    assert [m for m in moves if m[1] == "opening"] == []


def test_firm_without_warehouse_gets_default_one():
    f = _firm(with_warehouse=False)
    p = _create(f, opening_stock=4)
    rows, moves, _ = _stock(p["id"])
    with SessionLocal() as db:
        wh = db.query(Warehouse).filter(Warehouse.organization_id == f["org"]).one()
    assert wh.is_default and rows == {wh.id: 4} and moves == [(wh.id, "opening", 4)]


def test_bulk_import_rows_each_post_their_own_opening_stock():
    """A bulk import is POST /products once per row."""
    f = _firm()
    made = [_create(f, opening_stock=q) for q in (3, 0, 12)]
    assert [sum(_stock(p["id"])[0].values()) for p in made] == [3, 0, 12]


def test_failed_create_leaves_no_stock():
    f = _firm()
    before = None
    with SessionLocal() as db:
        before = db.query(StockMovement).filter(StockMovement.organization_id == f["org"]).count()
    r = client.post("/products", json={"name": "bad", "opening_stock": 5, "category_id": str(uuid.uuid4())},
                    headers=f["h"])
    assert r.status_code == 400
    with SessionLocal() as db:
        assert db.query(StockMovement).filter(StockMovement.organization_id == f["org"]).count() == before
