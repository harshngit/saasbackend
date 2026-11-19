"""Purchase return hardening (QA 19 Nov): supplier / warehouse / GRN come from the source
purchase, strict whole-number quantities within (received - already returned), required
reason, return date not before the purchase / receipt, the lifecycle
draft -> confirmed -> dispatched -> completed with no shortcuts, no cancel after dispatch,
stock moved exactly once (on confirm), all-or-nothing multi-line requests, and returns of
products made inactive after purchase."""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    GoodsReceiptNote, GoodsReceiptNoteItem, Organization, Product, PurchaseInvoice, PurchaseInvoiceItem,
    StockMovement, Supplier, User, Warehouse, WarehouseStock,
)
from app.models.enums import UserRole  # noqa: E402
from app.models.purchase_return import PurchaseReturn  # noqa: E402
from app.services import entitlement_service, stock_service  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)
REASON = "Damaged in transit"


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
    db = SessionLocal()
    org = Organization(name=f"PRH {uuid.uuid4().hex[:6]}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}")
    db.add(org)
    db.flush()
    admin = User(email=f"prh.{uuid.uuid4().hex[:8]}@example.com", name="Admin", organization_id=org.id,
                 system_role="admin", role=UserRole.ADMIN, password_hash=hash_password("Password123!"), is_active=True)
    sup_a = Supplier(organization_id=org.id, name="Supplier A", is_active=True, total_purchases=10_000.0)
    sup_b = Supplier(organization_id=org.id, name="Supplier B", is_active=True)
    main = Warehouse(organization_id=org.id, name="Main", code=f"M{uuid.uuid4().hex[:4]}", is_default=True, is_active=True)
    other = Warehouse(organization_id=org.id, name="Other", code=f"O{uuid.uuid4().hex[:4]}", is_active=True)
    p1 = Product(organization_id=org.id, name="Widget", sku=f"W-{uuid.uuid4().hex[:6]}", total_inventory=0, is_active=True)
    p2 = Product(organization_id=org.id, name="Gadget", sku=f"G-{uuid.uuid4().hex[:6]}", total_inventory=0, is_active=True)
    db.add_all([admin, sup_a, sup_b, main, other, p1, p2])
    db.commit()
    ids = dict(org=org.id, a=sup_a.id, b=sup_b.id, main=main.id, other=other.id, p1=p1.id, p2=p2.id)
    ids["h"] = {"Authorization": f"Bearer {create_access_token(admin.id, admin.system_role, org.id)}"}
    db.close()

    def purchase(lines, with_grn=True, days_ago=5):
        """lines: [(product_key, received)]. Stock for the received quantity goes into Main."""
        s = SessionLocal()
        try:
            when = datetime.now(timezone.utc) - timedelta(days=days_ago)
            pi = PurchaseInvoice(organization_id=ids["org"], invoice_number=f"PO-{uuid.uuid4().hex[:6]}",
                                 status="approved", supplier_id=ids["a"], warehouse_id=ids["main"], total=100_000,
                                 invoice_date=when, purchase_date=when, stock_added=True)
            for key, received in lines:
                prod = s.get(Product, ids[key])
                pi.items.append(PurchaseInvoiceItem(product_id=prod.id, product_name=prod.name, quantity=received,
                                                    received_qty=received, purchase_price=10, line_total=10 * received))
            s.add(pi)
            s.flush()
            grn_id = None
            if with_grn:
                grn = GoodsReceiptNote(organization_id=ids["org"], grn_number=f"GRN-{uuid.uuid4().hex[:6]}",
                                       purchase_id=pi.id, supplier_id=ids["a"], warehouse_id=ids["main"],
                                       status="confirmed", received_date=when + timedelta(days=1))
                for item in pi.items:
                    grn.items.append(GoodsReceiptNoteItem(purchase_item_id=item.id, product_id=item.product_id,
                                                          product_name=item.product_name, ordered_qty=item.quantity,
                                                          received_qty=item.received_qty))
                s.add(grn)
                s.flush()
                grn_id = grn.id
            for key, received in lines:
                stock_service.adjust_on_hand(s, ids["org"], ids["main"], ids[key], None, float(received), "purchase_in")
            s.commit()
            return pi.id, grn_id
        finally:
            s.close()

    ids["purchase"] = purchase
    return ids


def _create(w, pid, lines, **extra):
    body = {"purchase_id": pid, "reason": REASON,
            "items": [{"product_id": w[k], "quantity": q} for k, q in lines]}
    body.update(extra)
    return client.post("/purchase-returns", headers=w["h"], json=body)


def _act(w, rid, action, **body):
    return client.post(f"/purchase-returns/{rid}/{action}", headers=w["h"], json=body or None)


def _on_hand(w, key, wh="main"):
    s = SessionLocal()
    try:
        row = s.query(WarehouseStock).filter(WarehouseStock.warehouse_id == w[wh],
                                             WarehouseStock.product_id == w[key]).first()
        return row.on_hand_quantity if row else 0
    finally:
        s.close()


def _movements(w, key):
    s = SessionLocal()
    try:
        return s.query(StockMovement).filter(StockMovement.product_id == w[key],
                                             StockMovement.movement_type == "purchase_return").count()
    finally:
        s.close()


def _returns(pid):
    s = SessionLocal()
    try:
        return s.query(PurchaseReturn).filter(PurchaseReturn.purchase_id == pid).count()
    finally:
        s.close()


def _full_cycle(w, rid):
    for action in ("confirm", "dispatch", "complete"):
        r = _act(w, rid, action)
        assert r.status_code == 200, (action, r.text)
    return r.json()


# 1, 2, 3 ---------------------------------------------------------------- source data
def test_supplier_must_match_the_purchase(w):
    pid, _ = w["purchase"]([("p1", 10)])
    r = _create(w, pid, [("p1", 1)], supplier_id=w["b"])
    assert r.status_code == 400 and r.json()["detail"] == "Supplier does not match the selected purchase."
    assert _returns(pid) == 0
    r = _create(w, pid, [("p1", 1)])                       # no supplier sent -> the purchase's
    assert r.status_code == 201 and r.json()["supplier_id"] == w["a"]
    r = _create(w, pid, [("p1", 1)], supplier_id=w["a"])   # matching supplier is fine
    assert r.status_code == 201 and r.json()["supplier_id"] == w["a"]


def test_grn_is_linked_and_returned(w):
    pid, grn_id = w["purchase"]([("p1", 10)])
    r = _create(w, pid, [("p1", 2)])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["grn_id"] == grn_id and body["grnNumber"] and body["grn"]["id"] == grn_id
    got = client.get(f"/purchase-returns/{body['id']}", headers=w["h"]).json()
    assert got["grn_id"] == grn_id and got["grnNumber"] == body["grnNumber"]


def test_no_grn_stays_null_and_foreign_grn_refused(w):
    pid, _ = w["purchase"]([("p1", 10)], with_grn=False)
    r = _create(w, pid, [("p1", 1)])
    assert r.status_code == 201 and r.json()["grn_id"] is None
    _, other_grn = w["purchase"]([("p1", 5)])
    r = _create(w, pid, [("p1", 1)], grn_id=other_grn)
    assert r.status_code == 400 and r.json()["detail"] == "Goods receipt does not belong to the selected purchase."


# 4 - 8, 13, 15 ----------------------------------------------------------- quantities
@pytest.mark.parametrize("qty", [0, -1, 1.5, 10_000_000])
def test_bad_quantities_refused(w, qty):
    pid, _ = w["purchase"]([("p1", 10)])
    r = _create(w, pid, [("p1", qty)])
    assert r.status_code == 422, r.text
    assert r.json()["detail"][0]["loc"][-1] == "quantity"
    assert _returns(pid) == 0


def test_cumulative_limit_and_fully_returned(w):
    pid, _ = w["purchase"]([("p1", 10)])
    for q in (4, 3):
        r = _create(w, pid, [("p1", q)])
        assert r.status_code == 201
        _full_cycle(w, r.json()["id"])
    r = _create(w, pid, [("p1", 4)])                        # returnable 3
    assert r.status_code == 400
    assert r.json()["detail"].startswith("Return quantity cannot exceed returnable quantity.")
    r = _create(w, pid, [("p1", 3)])                        # exactly the rest
    assert r.status_code == 201
    assert r.json()["items"][0]["returnableQty"] == 3
    r = _create(w, pid, [("p1", 1)])                        # fully returned now (draft counts)
    assert r.status_code == 400 and "returnable 0" in r.json()["detail"]


def test_two_lines_for_one_product_cannot_pass_the_limit_together(w):
    pid, _ = w["purchase"]([("p1", 5)])
    r = _create(w, pid, [("p1", 3), ("p1", 3)])
    assert r.status_code == 400 and "requested 6, returnable 5" in r.json()["detail"]


# 16 ------------------------------------------------------------------------ atomicity
def test_one_bad_line_rolls_back_the_whole_request(w):
    pid, _ = w["purchase"]([("p1", 10), ("p2", 2)])
    r = _create(w, pid, [("p1", 1), ("p2", 3)])
    assert r.status_code == 400
    assert _returns(pid) == 0
    assert (_on_hand(w, "p1"), _on_hand(w, "p2")) == (10, 2)
    assert (_movements(w, "p1"), _movements(w, "p2")) == (0, 0)


# 9 - 14, 22 ------------------------------------------------------- lifecycle and stock
def test_lifecycle_moves_stock_once_on_confirm(w):
    pid, _ = w["purchase"]([("p1", 10)])
    rid = _create(w, pid, [("p1", 4)]).json()["id"]
    assert _on_hand(w, "p1") == 10                         # draft: nothing moves
    assert _act(w, rid, "confirm").status_code == 200
    assert _on_hand(w, "p1") == 6 and _movements(w, "p1") == 1
    assert _act(w, rid, "confirm").status_code == 400      # replay
    assert _act(w, rid, "dispatch").status_code == 200
    assert _act(w, rid, "dispatch").status_code == 400
    r = _act(w, rid, "complete")
    assert r.status_code == 200 and r.json()["status"] == "completed"
    r2 = _act(w, rid, "complete")                          # duplicate complete
    assert r2.status_code == 400 and r2.json()["detail"].startswith("Invalid purchase return status transition")
    assert _on_hand(w, "p1") == 6 and _movements(w, "p1") == 1
    s = SessionLocal()
    try:
        assert s.get(Product, w["p1"]).total_inventory == 6
    finally:
        s.close()


def test_no_shortcuts(w):
    pid, _ = w["purchase"]([("p1", 10)])
    rid = _create(w, pid, [("p1", 1)]).json()["id"]
    for action in ("complete", "dispatch"):                # draft -> completed / dispatched
        r = _act(w, rid, action)
        assert r.status_code == 400 and r.json()["detail"].startswith("Invalid purchase return status transition")
    assert _act(w, rid, "confirm").status_code == 200
    r = _act(w, rid, "complete")                            # confirmed -> completed
    assert r.status_code == 400 and r.json()["detail"].startswith("Invalid purchase return status transition")
    assert _on_hand(w, "p1") == 9 and _movements(w, "p1") == 1


def test_cancel_only_before_dispatch(w):
    pid, _ = w["purchase"]([("p1", 10)])
    draft = _create(w, pid, [("p1", 1)]).json()["id"]
    assert _act(w, draft, "cancel", reason="x").status_code == 200
    assert _on_hand(w, "p1") == 10                          # draft cancel: nothing moved
    confirmed = _create(w, pid, [("p1", 2)]).json()["id"]
    _act(w, confirmed, "confirm")
    assert _on_hand(w, "p1") == 8
    assert _act(w, confirmed, "cancel").status_code == 200
    assert _on_hand(w, "p1") == 10                          # put back once
    r = _act(w, confirmed, "cancel")
    assert r.status_code == 409 and r.json()["detail"] == "This purchase return is already cancelled."
    assert _on_hand(w, "p1") == 10

    dispatched = _create(w, pid, [("p1", 3)]).json()["id"]
    _act(w, dispatched, "confirm")
    _act(w, dispatched, "dispatch")
    r = _act(w, dispatched, "cancel")
    assert r.status_code == 409 and r.json()["detail"] == "A dispatched purchase return cannot be cancelled."
    _act(w, dispatched, "complete")
    r = _act(w, dispatched, "cancel")
    assert r.status_code == 409 and r.json()["detail"] == "A completed purchase return cannot be cancelled."
    assert _on_hand(w, "p1") == 7


# 17 ------------------------------------------------------------------------ warehouse
def test_warehouse_comes_from_the_purchase(w):
    pid, _ = w["purchase"]([("p1", 10)])
    r = _create(w, pid, [("p1", 1)], warehouse_id=w["other"])
    assert r.status_code == 400 and r.json()["detail"] == "Warehouse does not match the selected purchase."
    r = _create(w, pid, [("p1", 1)])
    assert r.status_code == 201 and r.json()["warehouse_id"] == w["main"]
    rid = r.json()["id"]
    r = client.patch(f"/purchase-returns/{rid}", headers=w["h"], json={"warehouse_id": w["other"]})
    assert r.status_code == 400 and r.json()["detail"] == "Warehouse does not match the selected purchase."


# 18 --------------------------------------------------------------------------- reason
@pytest.mark.parametrize("reason", [None, "", "   "])
def test_reason_required(w, reason):
    pid, _ = w["purchase"]([("p1", 10)])
    r = _create(w, pid, [("p1", 1)], reason=reason)
    assert r.status_code == 400 and r.json()["detail"] == "Return reason is required."
    ok = _create(w, pid, [("p1", 1)]).json()["id"]
    r = client.patch(f"/purchase-returns/{ok}", headers=w["h"], json={"reason": "  "})
    assert r.status_code == 400 and r.json()["detail"] == "Return reason is required."


# 19 ----------------------------------------------------------------------------- date
def test_return_date_rules(w):
    pid, _ = w["purchase"]([("p1", 10)], days_ago=5)      # purchase 5 days ago, GRN 4 days ago
    now = datetime.now(timezone.utc)
    r = _create(w, pid, [("p1", 1)], return_date=(now - timedelta(days=10)).isoformat())
    assert r.status_code == 400 and r.json()["detail"].startswith("Return date cannot be earlier than the purchase date")
    r = _create(w, pid, [("p1", 1)], return_date=(now - timedelta(days=5)).isoformat())
    assert r.status_code == 400 and r.json()["detail"].startswith("Return date cannot be earlier than the goods receipt date")
    r = _create(w, pid, [("p1", 1)], return_date=(now + timedelta(days=5)).isoformat())
    assert r.status_code == 400 and r.json()["detail"] == "Return date cannot be in the future."
    r = _create(w, pid, [("p1", 1)], return_date=now.date().isoformat())
    assert r.status_code == 201, r.text


# 20 ------------------------------------------------------------------ inactive product
def test_inactive_product_can_still_be_returned(w):
    pid, _ = w["purchase"]([("p1", 10)])
    s = SessionLocal()
    try:
        s.get(Product, w["p1"]).is_active = False
        s.commit()
    finally:
        s.close()
    r = _create(w, pid, [("p1", 2)])
    assert r.status_code == 201, r.text
    _full_cycle(w, r.json()["id"])
    assert _on_hand(w, "p1") == 8
