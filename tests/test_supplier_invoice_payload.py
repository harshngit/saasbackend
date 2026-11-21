"""Supplier invoice items are loaded without the product graph.

SupplierInvoiceItem.product used to be eager-joined, which pulled the product's own joined
relationships (pricing, variants, categories, supplier, brand, type) into every items load:
~220 columns per line, ~1.1 MB per call in production, and 56 GB of database egress a day
from GET /supplier-invoices, /accounts-payable and /accounts-payable/summary. The detail
endpoint also ran one billed-elsewhere query per line.
"""

import os
import re
import sys
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event  # noqa: E402

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Organization, Product, ProductPricing, Supplier, SupplierInvoice, User, Warehouse  # noqa: E402
from app.models.enums import UserRole  # noqa: E402
from app.services import entitlement_service, role_service  # noqa: E402

client = TestClient(app)
LINES = 8


@pytest.fixture(scope="module", autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)


@pytest.fixture(autouse=True)
def _no_plan_gates(monkeypatch):
    monkeypatch.setattr(entitlement_service, "check_entitlement", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "check_limit", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "has_entitlement", lambda *a, **k: True)


def _make_firm(lines: int = LINES, blank_purchase_names: bool = False) -> dict:
    db = SessionLocal()
    try:
        org = Organization(name=f"SI {uuid.uuid4().hex[:6]}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}",
                           gst_number="27AAAAA0000A1Z5", state="Maharashtra", currency="INR")
        db.add(org)
        db.flush()
        role_service.seed_default_roles(db, org.id)
        admin = User(email=f"si.{uuid.uuid4().hex[:8]}@example.com", name="A", organization_id=org.id,
                     system_role="admin", role=UserRole.ADMIN, password_hash=hash_password("Password123!"),
                     is_active=True)
        wh = Warehouse(organization_id=org.id, name="Main", code=f"W{uuid.uuid4().hex[:4]}", is_default=True)
        sup = Supplier(organization_id=org.id, name="Supp", gst_number="27CCCCC2222C1Z5", state="Maharashtra")
        db.add_all([admin, wh, sup])
        db.flush()
        pids = []
        for i in range(lines):
            p = Product(organization_id=org.id, name=f"Line {i}", sku=f"L{i}-{uuid.uuid4().hex[:5]}", price=100,
                        tax_rate=18, hsn_code="8544", is_active=True)
            p.pricing = ProductPricing(selling_price=100, purchase_price=50, tax_inclusive=False, currency="INR")
            db.add(p)
            db.flush()
            pids.append(p.id)
        db.commit()
        h = {"Authorization": f"Bearer {create_access_token(admin.id, 'admin', org.id)}"}
        out = {"org": org.id, "sup": sup.id, "wh": wh.id, "h": h, "pids": pids}
    finally:
        db.close()
    r = client.post("/purchases", json={"supplier_id": out["sup"], "warehouse_id": out["wh"],
                                        "invoice_number": f"INV-{uuid.uuid4().hex[:6]}",
                                        "items": [{"product_id": pid, "quantity": 10, "purchase_price": 50.0}
                                                  for pid in out["pids"]]}, headers=out["h"])
    assert r.status_code == 201, r.text
    client.post(f"/purchases/{r.json()['id']}/confirm", headers=out["h"])
    with SessionLocal() as s:
        inv = s.query(SupplierInvoice).filter(SupplierInvoice.organization_id == out["org"]).first()
        assert inv is not None and len(inv.items) == lines
        out["inv"] = inv.id
        if blank_purchase_names:
            # Purchase lines with no name / code: product_name and sku must fall back to the product.
            for item in inv.items:
                item.purchase_item.product_name = ""
                item.purchase_item.product_code = None
            s.commit()
    return out


@pytest.fixture
def firm():
    return _make_firm()


def _run(fn):
    stmts: list[str] = []

    def _cap(conn, cur, statement, *a):
        stmts.append(re.sub(r"\s+", " ", statement))

    event.listen(engine, "before_cursor_execute", _cap)
    try:
        r = fn()
    finally:
        event.remove(engine, "before_cursor_execute", _cap)
    assert r.status_code == 200, r.text
    return r.json(), stmts


@pytest.mark.parametrize("path", ["/supplier-invoices", "/supplier-invoices/{inv}", "/accounts-payable",
                                  "/accounts-payable/summary"])
def test_items_load_does_not_join_products(firm, path):
    _, stmts = _run(lambda: client.get(path.format(inv=firm["inv"]), headers=firm["h"]))
    items_loads = [s for s in stmts if re.search(r"FROM supplier_invoice_items\b", s) and " AS " in s]
    assert items_loads, "expected the items to be loaded"
    for s in items_loads:
        assert "JOIN products" not in s, s[:300]
        assert len(re.findall(r" AS \w+", s.split(" FROM ")[0])) < 60


def test_detail_has_names_and_a_fixed_query_count(firm):
    body, stmts = _run(lambda: client.get(f"/supplier-invoices/{firm['inv']}", headers=firm["h"]))
    assert sorted(i["product_name"] for i in body["items"]) == sorted(f"Line {i}" for i in range(LINES))
    assert all(i["sku"] for i in body["items"])
    assert len(stmts) <= 8, len(stmts)            # used to grow by one query per line


@pytest.mark.parametrize("path", ["/supplier-invoices/{inv}", "/supplier-invoices"])
def test_fallback_products_load_in_one_batched_query(path):
    """Lines whose purchase line has no name / code read them from the product: one query
    for all such lines, so 20 blank lines cost the same as 2 (not 18 more)."""
    counts = {}
    for n in (2, 20):
        f = _make_firm(n, blank_purchase_names=True)
        body, stmts = _run(lambda: client.get(path.format(inv=f["inv"]), headers=f["h"]))
        inv = body if "items" in body and "total" not in body else body["items"][0]
        assert sorted(i["product_name"] for i in inv["items"]) == sorted(f"Line {i}" for i in range(n))
        assert all(i["sku"] and i["sku"].startswith("L") for i in inv["items"])
        product_loads = [s for s in stmts if re.search(r"FROM products\b", s)]
        assert len(product_loads) == 1, product_loads
        assert "JOIN" not in product_loads[0] and len(re.findall(r" AS \w+", product_loads[0].split(" FROM ")[0])) <= 4
        counts[n] = len(stmts)
    assert counts[20] == counts[2], counts


def test_fallback_is_batched_on_routes_without_the_page_prefetch():
    """Cancel returns the invoice without calling prefetch_verification: the model's own
    guard still loads the fallback products for the whole invoice in one query."""
    f = _make_firm(12, blank_purchase_names=True)
    body, stmts = _run(lambda: client.post(f"/supplier-invoices/{f['inv']}/cancel", headers=f["h"]))
    assert sorted(i["product_name"] for i in body["items"]) == sorted(f"Line {i}" for i in range(12))
    assert len([s for s in stmts if re.search(r"FROM products\b", s)]) == 1
