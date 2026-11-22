"""Websocket-level check (cannot hang: every read is on a thread with a timeout):
a client connected to /ws gets `order.created` exactly once for POST /orders, and only
after the order is committed (a separate session can already read it when the event
arrives). Also: an admin who places an order gets no bell notification for it (by design:
never the person who acted), while another admin does.
"""

import json
import os
import sys
import threading
import time
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Customer, Notification, Organization, Product, ProductPricing, SalesOrder, User, Warehouse, WarehouseStock,
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


@pytest.fixture
def firm():
    with SessionLocal() as db:
        org = Organization(name=f"RT {uuid.uuid4().hex[:6]}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}",
                           gst_number="27AAAAA0000A1Z5", state="Maharashtra", currency="INR")
        db.add(org)
        db.flush()
        role_service.seed_default_roles(db, org.id)
        admins = [User(email=f"rt{k}.{uuid.uuid4().hex[:8]}@example.com", name=f"Admin {k}", organization_id=org.id,
                       system_role="admin", role=UserRole.ADMIN, password_hash=hash_password("Password123!"),
                       is_active=True) for k in range(2)]
        wh = Warehouse(organization_id=org.id, name="Main", code=f"W{uuid.uuid4().hex[:4]}", is_default=True)
        cust = Customer(organization_id=org.id, name="Shop", gst_number="27BBBBB1111B1Z5", state="Maharashtra",
                        delivery_address="Addr")
        db.add_all([*admins, wh, cust])
        db.flush()
        p = Product(organization_id=org.id, name="Item", sku=f"I-{uuid.uuid4().hex[:5]}", price=100, tax_rate=18,
                    hsn_code="8544", total_inventory=0, is_active=True)
        p.pricing = ProductPricing(selling_price=100, purchase_price=50, tax_inclusive=False, currency="INR")
        db.add(p)
        db.flush()
        db.add(WarehouseStock(organization_id=org.id, warehouse_id=wh.id, product_id=p.id, on_hand_quantity=100))
        db.commit()
        tokens = [create_access_token(a.id, "admin", org.id) for a in admins]
        return {"org": org.id, "admins": [a.id for a in admins], "tokens": tokens,
                "body": {"customer_id": cust.id, "warehouse_id": wh.id, "delivery_method": "home_delivery",
                         "payment_status": "pending", "items": [{"product_id": p.id, "quantity": 1}]}}


def _collect(ws, seconds: float) -> list[dict]:
    """Messages received within `seconds`; for order.created, whether the order was already
    committed (readable from a separate session) at the moment the event arrived."""
    got: list[dict] = []

    def reader():
        while True:
            try:
                raw = ws.receive_text()
            except Exception:  # noqa: BLE001 -- socket closed at the end of the test
                return
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("event") == "order.created":
                order_id = (msg.get("data") or {}).get("order_id")
                with SessionLocal() as s:
                    msg["_committed"] = s.get(SalesOrder, order_id) is not None
            got.append(msg)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    t.join(timeout=seconds)
    return got


def test_order_created_reaches_the_websocket_once_after_commit(firm):
    client = TestClient(app)
    viewer = firm["tokens"][1]                       # the open dashboard of another admin
    with client.websocket_connect(f"/ws?token={viewer}") as ws:
        ws.send_text("ping")
        assert ws.receive_text() == "pong"
        r = client.post("/orders", headers={"Authorization": f"Bearer {firm['tokens'][0]}"}, json=firm["body"])
        assert r.status_code == 201, r.text
        msgs = _collect(ws, 3.0)
    created = [m for m in msgs if m.get("event") == "order.created"]
    assert len(created) == 1, [m.get("event") for m in msgs]
    assert created[0]["data"]["order_id"] == r.json()["id"]
    assert created[0]["_committed"] is True


def test_bell_goes_to_other_admins_not_to_the_one_who_placed_the_order(firm):
    client = TestClient(app)
    r = client.post("/orders", headers={"Authorization": f"Bearer {firm['tokens'][0]}"}, json=firm["body"])
    assert r.status_code == 201, r.text
    time.sleep(0.2)
    with SessionLocal() as s:
        placer = s.query(Notification).filter(Notification.user_id == firm["admins"][0]).count()
        other = s.query(Notification).filter(Notification.user_id == firm["admins"][1]).count()
    assert placer == 0 and other == 1, (placer, other)
