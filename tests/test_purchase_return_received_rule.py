"""Purchase return rule: a line may return at most (received - already returned) and at
most the stock available in the warehouse -- checked on create, on edit and again on
confirm. Violations are 400 naming the product and the allowed quantity, never a 500.

Bug reproduced here: PR-...-0003 (draft, 2 lines, Received = 0) -> Confirm -> 500. The
draft could be created because the old rule compared against the ORDERED quantity, and
the 500 was `AttributeError: 'WarehouseStock' object has no attribute 'quantity'` in
deduct_inventory_for_return as soon as the product had a warehouse stock row.
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
from app.models import (  # noqa: E402
    Organization, Product, PurchaseInvoice, PurchaseInvoiceItem, StockMovement, Supplier, User,
    Warehouse, WarehouseStock,
)
from app.models.enums import UserRole  # noqa: E402
from app.models.purchase_return import PurchaseReturn, PurchaseReturnItem  # noqa: E402
from app.services import entitlement_service, stock_service  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture(scope="module", autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)


@pytest.fixture(autouse=True)
def _no_plan_gates(monkeypatch):
    monkeypatch.setattr(entitlement_service, "check_entitlement", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "check_limit", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "has_entitlement", lambda *a, **k: True)


@pytest.fixture
def world():
    """Org + admin + supplier + default warehouse + two products, and a factory for
    purchases with chosen received quantities and warehouse stock."""
    db = SessionLocal()
    org = Organization(name=f"PRR {uuid.uuid4().hex[:6]}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}")
    db.add(org)
    db.flush()
    admin = User(email=f"prr.{uuid.uuid4().hex[:8]}@example.com", name="Admin", organization_id=org.id,
                 system_role="admin", role=UserRole.ADMIN, password_hash=hash_password("Password123!"), is_active=True)
    sup = Supplier(organization_id=org.id, name="Coastal Beverages Distributio", is_active=True, total_purchases=1000.0)
    wh = Warehouse(organization_id=org.id, name="Main", code=f"W{uuid.uuid4().hex[:4]}", is_default=True, is_active=True)
    p1 = Product(organization_id=org.id, name="Electronics", sku=f"E-{uuid.uuid4().hex[:6]}", total_inventory=0, is_active=True)
    p2 = Product(organization_id=org.id, name="Polycab FR 1.5 sq mm Wire (90m Coil)", sku=f"P-{uuid.uuid4().hex[:6]}",
                 total_inventory=0, is_active=True)
    db.add_all([admin, sup, wh, p1, p2])
    db.commit()
    ids = dict(org=org.id, sup=sup.id, wh=wh.id, p1=p1.id, p2=p2.id)
    headers = {"Authorization": f"Bearer {create_access_token(admin.id, admin.system_role, org.id)}"}
    db.close()

    def purchase(lines, stock):
        """lines: [(product_key, ordered, received)]; stock: {product_key: on_hand in Main}."""
        s = SessionLocal()
        try:
            pi = PurchaseInvoice(organization_id=ids["org"], invoice_number=f"PO-{uuid.uuid4().hex[:6]}",
                                 status="approved", supplier_id=ids["sup"], warehouse_id=ids["wh"], total=10_000)
            for key, ordered, received in lines:
                prod = s.get(Product, ids[key])
                pi.items.append(PurchaseInvoiceItem(product_id=prod.id, product_name=prod.name, quantity=ordered,
                                                    received_qty=received, purchase_price=10, line_total=10 * ordered))
            s.add(pi)
            for key, qty in stock.items():
                if qty:
                    stock_service.adjust_on_hand(s, ids["org"], ids["wh"], ids[key], None, float(qty), "purchase_in")
            s.commit()
            return pi.id
        finally:
            s.close()

    return ids, headers, purchase


def _on_hand(ids, key):
    s = SessionLocal()
    try:
        row = s.query(WarehouseStock).filter(WarehouseStock.warehouse_id == ids["wh"],
                                             WarehouseStock.product_id == ids[key]).first()
        return row.on_hand_quantity if row else 0
    finally:
        s.close()


def _movements(ids, key):
    s = SessionLocal()
    try:
        return s.query(StockMovement).filter(StockMovement.product_id == ids[key],
                                             StockMovement.movement_type == "purchase_return").count()
    finally:
        s.close()


def _create(headers, pid, ids, lines):
    return client.post("/purchase-returns", headers=headers, json={
        "purchase_id": pid, "items": [{"product_id": ids[k], "quantity": q} for k, q in lines]})


def test_valid_confirm_moves_stock_balances_and_status_once(world):
    ids, headers, purchase = world
    pid = purchase([("p1", 10, 10)], {"p1": 10})
    r = _create(headers, pid, ids, [("p1", 4)])
    assert r.status_code == 201, r.text
    line = r.json()["items"][0]
    assert (line["receivedQty"], line["previouslyReturnedQty"], line["returnableQty"]) == (10, 0, 10)
    r = client.post(f"/purchase-returns/{r.json()['id']}/confirm", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "confirmed"
    assert _on_hand(ids, "p1") == 6 and _movements(ids, "p1") == 1
    s = SessionLocal()
    try:
        assert s.get(Supplier, ids["sup"]).total_purchases == 960.0
        assert s.get(PurchaseInvoice, pid).return_amount == 40.0
        assert s.get(Product, ids["p1"]).total_inventory == 6  # catalog counter kept in step
    finally:
        s.close()


def test_return_more_than_received_is_400_on_create_and_edit(world):
    ids, headers, purchase = world
    pid = purchase([("p1", 10, 5)], {"p1": 50})
    r = _create(headers, pid, ids, [("p1", 6)])
    assert r.status_code == 400
    assert "Electronics" in r.json()["detail"] and "only 5 allowed" in r.json()["detail"]
    r = _create(headers, pid, ids, [("p1", 5)])
    assert r.status_code == 201
    r = client.patch(f"/purchase-returns/{r.json()['id']}", headers=headers,
                     json={"items": [{"product_id": ids["p1"], "quantity": 7}]})
    assert r.status_code == 400 and "only 5 allowed" in r.json()["detail"]


def test_return_more_than_stock_is_400_on_create_and_on_confirm_changes_nothing(world):
    ids, headers, purchase = world
    pid = purchase([("p1", 10, 10)], {"p1": 3})  # 7 of the 10 received were already sold
    r = _create(headers, pid, ids, [("p1", 4)])
    assert r.status_code == 400
    assert "Electronics" in r.json()["detail"] and "only 3 can be returned" in r.json()["detail"]

    r = _create(headers, pid, ids, [("p1", 3)])
    assert r.status_code == 201
    rid = r.json()["id"]
    s = SessionLocal()  # stock leaves after the draft was saved
    try:
        stock_service.adjust_on_hand(s, ids["org"], ids["wh"], ids["p1"], None, -2.0, "sale")
        s.commit()
    finally:
        s.close()
    r = client.post(f"/purchase-returns/{rid}/confirm", headers=headers)
    assert r.status_code == 400 and "only 1 can be returned" in r.json()["detail"]
    assert client.get(f"/purchase-returns/{rid}", headers=headers).json()["status"] == "draft"
    assert _on_hand(ids, "p1") == 1 and _movements(ids, "p1") == 0
    s = SessionLocal()
    try:
        assert s.get(Supplier, ids["sup"]).total_purchases == 1000.0
    finally:
        s.close()


def test_double_confirm_deducts_once(world):
    ids, headers, purchase = world
    pid = purchase([("p1", 10, 10)], {"p1": 10})
    rid = _create(headers, pid, ids, [("p1", 2)]).json()["id"]
    assert client.post(f"/purchase-returns/{rid}/confirm", headers=headers).status_code == 200
    r = client.post(f"/purchase-returns/{rid}/confirm", headers=headers)
    assert r.status_code == 400 and "confirmed" in r.json()["detail"]
    assert _on_hand(ids, "p1") == 8 and _movements(ids, "p1") == 1
    s = SessionLocal()
    try:
        assert s.get(Supplier, ids["sup"]).total_purchases == 980.0
    finally:
        s.close()


def test_exact_scenario_draft_with_received_zero(world):
    """PR-...-0003: purchase lines never received (no GRN); 'Electronics' 10, 'Polycab' 5."""
    ids, headers, purchase = world
    pid = purchase([("p1", 10, 0), ("p2", 5, 0)], {"p1": 20, "p2": 20})  # stock from other purchases

    # 1. Such a draft can no longer be created.
    r = _create(headers, pid, ids, [("p1", 10), ("p2", 5)])
    assert r.status_code == 400
    assert "Electronics" in r.json()["detail"] and "only 0 allowed" in r.json()["detail"]

    # 2. The existing production draft (created under the old rule) is stored as-is.
    s = SessionLocal()
    try:
        pi = s.get(PurchaseInvoice, pid)
        pr = PurchaseReturn(organization_id=ids["org"], return_number=f"PR-T-{uuid.uuid4().hex[:6]}",
                            purchase_id=pid, supplier_id=ids["sup"], warehouse_id=ids["wh"], status="draft")
        for it in pi.items:
            pr.items.append(PurchaseReturnItem(purchase_item_id=it.id, product_id=it.product_id,
                                               product_name=it.product_name, quantity=it.quantity,
                                               unit_price=10, line_total=10 * it.quantity))
        s.add(pr)
        s.commit()
        rid = pr.id
    finally:
        s.close()

    # 3. The detail page now gets the real figures for its columns.
    lines = {i["productName"]: i for i in client.get(f"/purchase-returns/{rid}", headers=headers).json()["items"]}
    assert lines["Electronics"]["receivedQty"] == 0 and lines["Electronics"]["returnableQty"] == 0

    # 4. Confirm: a clear 400 (was an unhandled 500), nothing moves.
    r = client.post(f"/purchase-returns/{rid}/confirm", headers=headers)
    assert r.status_code == 400, r.text
    assert "Electronics" in r.json()["detail"] and "only 0 allowed" in r.json()["detail"]
    assert _on_hand(ids, "p1") == 20 and _movements(ids, "p1") == 0

    # 5. Recovery: the user can cancel the draft (no stock was ever deducted) ...
    r = client.post(f"/purchase-returns/{rid}/cancel", headers=headers, json={"cancel_reason": "goods never received"})
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert _on_hand(ids, "p1") == 20

    # ... or, once the goods are received with a GRN, return them normally.
    s = SessionLocal()
    try:
        for it in s.get(PurchaseInvoice, pid).items:
            it.received_qty = it.quantity
        s.commit()
    finally:
        s.close()
    r = _create(headers, pid, ids, [("p1", 10), ("p2", 5)])
    assert r.status_code == 201, r.text
    assert client.post(f"/purchase-returns/{r.json()['id']}/confirm", headers=headers).status_code == 200
    assert _on_hand(ids, "p1") == 10 and _on_hand(ids, "p2") == 15


def test_old_draft_with_a_warehouse_stock_row_no_longer_500s(world):
    """The 500 itself: a valid draft whose product has a WarehouseStock row."""
    ids, headers, purchase = world
    pid = purchase([("p2", 5, 5)], {"p2": 5})
    rid = _create(headers, pid, ids, [("p2", 5)]).json()["id"]
    r = client.post(f"/purchase-returns/{rid}/confirm", headers=headers)
    assert r.status_code == 200, r.text
    assert _on_hand(ids, "p2") == 0
    r = client.post(f"/purchase-returns/{rid}/cancel", headers=headers, json={})
    assert r.status_code == 200 and _on_hand(ids, "p2") == 5  # cancel puts it back
