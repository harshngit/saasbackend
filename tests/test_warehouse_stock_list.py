"""GET /warehouses/stock: same figures as stock_summary() per row, in a fixed number of
queries whatever the page size (it used to make ~2 queries per row: 92 rows took 21 s in
production, past the frontend's 20 s abort).

Covers: tracked stock, untracked catalog stock (counter only, default warehouse only),
variants, active reservations, a non-default warehouse, search and low_stock_only.
"""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event  # noqa: E402

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Organization, Product, ProductVariant, StockReservation, User, Warehouse, WarehouseStock,
)
from app.models.enums import UserRole  # noqa: E402
from app.services import entitlement_service, role_service, stock_service  # noqa: E402

client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)


@pytest.fixture(autouse=True)
def _no_plan_gates(monkeypatch):
    monkeypatch.setattr(entitlement_service, "check_entitlement", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "check_limit", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "has_entitlement", lambda *a, **k: True)


def _firm(n_products: int):
    """Default warehouse W1 and a second W2. Product i (cycling through 4 kinds):
    0 tracked in W1 (+ a reservation), 1 untracked (catalog counter only),
    2 with two variants (one tracked, one untracked), 3 tracked in W2 only."""
    db = SessionLocal()
    try:
        org = Organization(name=f"Stk {uuid.uuid4().hex[:6]}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}",
                           state="Maharashtra", currency="INR")
        db.add(org)
        db.flush()
        role_service.seed_default_roles(db, org.id)
        admin = User(email=f"stk.{uuid.uuid4().hex[:8]}@example.com", name="Admin", organization_id=org.id,
                     system_role="admin", role=UserRole.ADMIN, password_hash=hash_password("Password123!"),
                     is_active=True)
        w1 = Warehouse(organization_id=org.id, name="Main", code=f"W{uuid.uuid4().hex[:4]}", is_default=True)
        w2 = Warehouse(organization_id=org.id, name="Second", code=f"X{uuid.uuid4().hex[:4]}", is_default=False)
        db.add_all([admin, w1, w2])
        db.flush()
        for i in range(n_products):
            kind = i % 4
            p = Product(organization_id=org.id, name=f"Item {i:03d}", sku=f"K{i}-{uuid.uuid4().hex[:5]}",
                        price=10, total_inventory=7 + i, minimum_stock_level=20 if i % 3 == 0 else None,
                        is_active=True)
            db.add(p)
            db.flush()
            if kind == 0:
                db.add(WarehouseStock(organization_id=org.id, warehouse_id=w1.id, product_id=p.id,
                                      on_hand_quantity=30 + i))
                db.add(StockReservation(organization_id=org.id, warehouse_id=w1.id, product_id=p.id,
                                        reserved_quantity=12, consumed_quantity=2, status="active"))
            elif kind == 2:
                v1 = ProductVariant(organization_id=org.id, product_id=p.id, name="Red", sku=f"R{i}-{uuid.uuid4().hex[:4]}",
                                    variant_code=f"VR{uuid.uuid4().hex[:8]}", inventory=5)
                v2 = ProductVariant(organization_id=org.id, product_id=p.id, name="Blue", sku=f"B{i}-{uuid.uuid4().hex[:4]}",
                                    variant_code=f"VB{uuid.uuid4().hex[:8]}", inventory=9)
                db.add_all([v1, v2])
                db.flush()
                db.add(WarehouseStock(organization_id=org.id, warehouse_id=w1.id, product_id=p.id,
                                      variant_id=v1.id, on_hand_quantity=4))
            elif kind == 3:
                db.add(WarehouseStock(organization_id=org.id, warehouse_id=w2.id, product_id=p.id,
                                      on_hand_quantity=11))
        db.commit()
        return {"org": org.id, "w1": w1.id, "w2": w2.id,
                "h": {"Authorization": f"Bearer {create_access_token(admin.id, 'admin', org.id)}"}}
    finally:
        db.close()


def _expected(warehouse_id, rows):
    with SessionLocal() as db:
        return [
            {k: s[k] for k in ("on_hand", "reserved", "available")}
            for s in (stock_service.stock_summary(db, warehouse_id, r["product_id"], r["variant_id"]) for r in rows)
        ]


def _get(f, **params):
    r = client.get("/warehouses/stock", params={"page_size": 100, **params}, headers=f["h"])
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize("wh", ["w1", "w2"])
def test_figures_match_stock_summary_per_row(wh):
    f = _firm(12)
    body = _get(f, warehouse_id=f[wh])
    rows = body["items"]
    assert body["total"] == 12 and len(rows) == 15          # 3 variant products -> 2 rows each
    assert [{k: r[k] for k in ("on_hand", "reserved", "available")} for r in rows] == _expected(f[wh], rows)
    if wh == "w1":
        by_name = {(r["product_name"], r["variant_name"]): r for r in rows}
        assert by_name[("Item 000", None)]["available"] == 20       # 30 on hand - 10 outstanding
        assert by_name[("Item 001", None)]["on_hand"] == 8          # untracked: catalog counter
        assert by_name[("Item 002", "Red")]["on_hand"] == 4         # tracked variant
        assert by_name[("Item 002", "Blue")]["on_hand"] == 9        # untracked variant
        assert by_name[("Item 003", None)]["on_hand"] == 0          # held in W2 only
    else:
        assert {r["product_name"]: r["on_hand"] for r in rows if r["on_hand"]} == \
            {"Item 003": 11, "Item 007": 11, "Item 011": 11}         # catalog stock never shows in W2


def test_low_stock_only_and_search_match():
    f = _firm(12)
    low = _get(f, low_stock_only="true")["items"]
    assert low and all(r["minimum_stock_level"] and r["available"] <= r["minimum_stock_level"] for r in low)
    assert [{k: r[k] for k in ("on_hand", "reserved", "available")} for r in low] == _expected(f["w1"], low)
    found = _get(f, search="Item 002")["items"]
    assert [(r["product_name"], r["variant_name"]) for r in found] == [("Item 002", "Red"), ("Item 002", "Blue")]


def test_query_count_does_not_grow_with_rows():
    counts = {}
    for n in (8, 92):
        f = _firm(n)
        seen = {"q": 0}

        def _count(*_a):
            seen["q"] += 1

        event.listen(engine, "before_cursor_execute", _count)
        try:
            body = _get(f)
        finally:
            event.remove(engine, "before_cursor_execute", _count)
        assert body["total"] == n
        counts[n] = seen["q"]
    assert counts[92] == counts[8], counts
    assert counts[92] <= 15, counts
