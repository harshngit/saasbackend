"""List endpoints run a fixed number of queries, whatever the data on the page.

GET /invoices: invoices created for a delivery carry delivery_id; Invoice.delivery used to
be an eager join that pulled each delivery's own relationships (lines, customer, order,
vehicle, collections) -- 13 queries for a page instead of 7-8.
"""

import os
import re
import sys
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event, text  # noqa: E402

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Organization, Plan  # noqa: E402
from app.seed import seed_plans  # noqa: E402
from app.services import entitlement_service  # noqa: E402

c = TestClient(app)


@pytest.fixture(autouse=True)
def _no_plan_gates(monkeypatch):
    monkeypatch.setattr(entitlement_service, "check_entitlement", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "check_limit", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "has_entitlement", lambda *a, **k: True)


def _firm(n_orders: int) -> dict:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as s:
        seed_plans(s)
        s.commit()
    r = c.post("/auth/register", json={"organization_name": f"LQ {uuid.uuid4().hex[:6]}", "admin_name": "A",
               "email": f"lq_{uuid.uuid4().hex[:10]}@example.com", "password": "Password123!", "role": "admin"})
    auth = {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}
    org_id = c.get("/auth/me", headers=auth).json()["organization_id"]
    with SessionLocal() as s:
        s.query(Organization).filter(Organization.id == org_id).update(
            {"plan_id": s.query(Plan).filter(Plan.name == "Enterprise").one().id})
        s.commit()
    wh = c.post("/warehouses", json={"name": "Main", "code": f"WH-{uuid.uuid4().hex[:6]}", "is_default": True},
                headers=auth).json()["id"]
    custs = [c.post("/customers", json={"name": f"Shop {k}", "phone": f"98765000{k:02d}", "delivery_address": "x",
                    "billing_address": "x"}, headers=auth).json()["id"] for k in range(3)]
    pid = c.post("/products", json={"name": "Item", "sku": f"LQ-{uuid.uuid4().hex[:6]}", "price": 100.0,
                 "tax_rate": 18.0, "uom": "unit",
                 "pricing": {"purchase_price": 50.0, "selling_price": 100.0, "currency": "INR"}}, headers=auth).json()["id"]
    c.post(f"/warehouses/{wh}/stock/adjust", json={"product_id": pid, "quantity": 10000}, headers=auth)
    for k in range(n_orders):
        r = c.post("/orders", json={"customer_id": custs[k % 3], "warehouse_id": wh, "delivery_method": "home_delivery",
                   "payment_status": "pending", "items": [{"product_id": pid, "quantity": 1 + k % 3}]}, headers=auth)
        assert r.status_code == 201, r.text
    with SessionLocal() as s:  # as live delivery invoices are: linked to their delivery
        s.execute(text("UPDATE invoices i SET delivery_id = d.id FROM deliveries d WHERE d.sales_order_id = i.order_id "
                       "AND i.organization_id = :o"), {"o": org_id})
        s.commit()
    return auth


def _statements(path, auth, **params):
    stmts: list[str] = []

    def _cap(conn, cur, statement, *a):
        stmts.append(re.sub(r"\s+", " ", statement))

    event.listen(engine, "before_cursor_execute", _cap)
    try:
        r = c.get(path, params=params, headers=auth)
    finally:
        event.remove(engine, "before_cursor_execute", _cap)
    assert r.status_code == 200, r.text
    return stmts


def test_invoice_list_does_not_load_the_deliveries():
    counts = {}
    for n in (2, 12):
        auth = _firm(n)
        stmts = _statements("/invoices", auth, page_size=25)
        assert not [s for s in stmts if re.search(r"FROM (delivery_items|deliveries)\b", s)], stmts
        counts[n] = len(stmts)
    assert counts[12] == counts[2] and counts[12] <= 8, counts
