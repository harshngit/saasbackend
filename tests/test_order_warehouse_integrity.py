"""Sales Order warehouse stock integrity.

Every order has ONE warehouse. Stock is reserved there on confirm and leaves exactly
that warehouse at load (delivery) or pickup — never the default warehouse, another
warehouse, or the firm-wide catalog total.

Run: pytest tests/test_order_warehouse_integrity.py   (uses DATABASE_URL / .env)
"""

import os
import sys
import uuid

os.environ.setdefault("TESTING", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from app.core.database import Base, SessionLocal, auto_add_missing_columns, engine
from app.main import app
from app.models import Organization, Plan, Product, VehicleLoading, WarehouseStock
from app.services import stock_service

Base.metadata.create_all(bind=engine)
auto_add_missing_columns()
client = TestClient(app)


def _register(label: str) -> dict:
    r = client.post("/auth/register", json={
        "organization_name": f"{label} {uuid.uuid4().hex[:6]}", "admin_name": f"Admin {label}",
        "email": f"admin_{uuid.uuid4().hex[:10]}@example.com", "password": "Password123!", "role": "admin",
    })
    assert r.status_code == 201, r.text
    auth = {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}
    org_id = client.get("/auth/me", headers=auth).json()["organization_id"]
    with SessionLocal() as db:
        plan = db.query(Plan).filter(Plan.name == "Enterprise").first()
        assert plan is not None, "Enterprise plan missing from the test database"
        db.query(Organization).filter(Organization.id == org_id).update({"plan_id": plan.id})
        db.commit()
    return {"auth": auth, "org_id": org_id}


def _staff(auth: dict, role: str) -> tuple[dict, dict]:
    email = f"{role}_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/users", json={"name": f"{role} user", "email": email, "password": "Password123!", "role": role},
                    headers=auth)
    assert r.status_code == 201, r.text
    login = client.post("/auth/login", json={"email": email, "password": "Password123!"})
    assert login.status_code == 200, login.text
    return r.json(), {"Authorization": f"Bearer {login.json()['tokens']['access_token']}"}


def _warehouse(auth: dict, name: str) -> str:
    r = client.post("/warehouses", json={"name": name, "code": f"WH-{uuid.uuid4().hex[:6]}"}, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _product(auth: dict) -> str:
    r = client.post("/products", json={
        "name": f"Widget {uuid.uuid4().hex[:4]}", "sku": f"W-{uuid.uuid4().hex[:6]}", "price": 100.0,
        "tax_rate": 0.0, "uom": "unit",
        "pricing": {"purchase_price": 50.0, "selling_price": 100.0, "currency": "INR"},
    }, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _adjust(auth: dict, wh: str, product_id: str, qty: float) -> None:
    r = client.post(f"/warehouses/{wh}/stock/adjust", json={"product_id": product_id, "quantity": qty}, headers=auth)
    assert r.status_code == 200, r.text


def _on_hand(wh: str, product_id: str) -> float:
    with SessionLocal() as db:
        return stock_service.on_hand(db, wh, product_id, None)


def _reserved(wh: str, product_id: str) -> float:
    with SessionLocal() as db:
        return stock_service.reserved(db, wh, product_id, None)


def _order(auth: dict, customer_id: str, wh: str, product_id: str, qty: float, confirm: bool = True):
    r = client.post("/orders", json={
        "customer_id": customer_id, "warehouse_id": wh, "source": "office",
        "items": [{"product_id": product_id, "quantity": qty, "unit_price": 100.0}],
    }, headers=auth)
    assert r.status_code == 201, r.text
    order = r.json()
    if confirm:
        r = client.post(f"/orders/{order['id']}/confirm", headers=auth)
        return order, r
    return order, r


@pytest.fixture(scope="module")
def env():
    org = _register("WH Integrity")
    auth = org["auth"]
    main = _warehouse(auth, "Main Warehouse")
    w2 = _warehouse(auth, "Warehouse W2")
    with SessionLocal() as db:
        default_id = stock_service.default_warehouse(db, org["org_id"]).id
        db.commit()
    product = _product(auth)
    _adjust(auth, main, product, 100)
    _adjust(auth, w2, product, 50)
    vehicle = client.post("/vehicles", json={"vehicle_number": f"MH12-{uuid.uuid4().hex[:4].upper()}",
                                             "vehicle_type": "Truck"}, headers=auth).json()["id"]
    partner, partner_auth = _staff(auth, "delivery_partner")
    customer = client.post("/customers", json={
        "name": "Acme", "phone": "9876543210", "delivery_address": "1 Road", "billing_address": "1 Road",
    }, headers=auth).json()["id"]
    return {**org, "main": main, "w2": w2, "default": default_id, "product": product, "vehicle": vehicle,
            "partner": partner, "partner_auth": partner_auth, "customer": customer}


def _deliver(env: dict, order_id: str, order_item_id: str, qty: float, warehouse_id: str | None = None):
    auth = env["auth"]
    body = {"order_id": order_id, "delivery_partner_id": env["partner"]["id"], "vehicle_id": env["vehicle"],
            "items": [{"order_item_id": order_item_id, "planned_quantity": qty}]}
    if warehouse_id:
        body["warehouse_id"] = warehouse_id
    r = client.post("/deliveries", json=body, headers=auth)
    if r.status_code != 201:
        return r, None
    delivery = r.json()
    did = delivery["id"]
    assert client.post(f"/deliveries/{did}/accept", headers=env["partner_auth"]).status_code == 200
    pick = [{"delivery_item_id": i["id"], "picked_quantity": i["planned_quantity"]} for i in delivery["items"]]
    r = client.post(f"/deliveries/{did}/pick", json={"items": pick}, headers=auth)
    assert r.status_code == 200, r.text
    assert client.post(f"/deliveries/{did}/ready", headers=auth).status_code == 200
    r = client.post(f"/deliveries/{did}/load", json={}, headers=auth)
    assert r.status_code == 200, r.text
    return r, delivery


def test_delivery_deducts_only_the_order_warehouse(env):
    order, r = _order(env["auth"], env["customer"], env["w2"], env["product"], 10)
    assert r.status_code == 200, r.text
    assert _reserved(env["w2"], env["product"]) == 10
    assert _reserved(env["main"], env["product"]) == 0

    r, delivery = _deliver(env, order["id"], r.json()["items"][0]["id"], 10)
    assert _on_hand(env["w2"], env["product"]) == 40      # 50 - 10
    assert _on_hand(env["main"], env["product"]) == 100    # untouched
    assert _reserved(env["w2"], env["product"]) == 0       # reservation consumed, not double-counted

    # The vehicle run remembers its source, so end-of-day returns go back to W2.
    with SessionLocal() as db:
        loading = db.query(VehicleLoading).filter(
            VehicleLoading.organization_id == env["org_id"],
            VehicleLoading.delivery_partner_id == env["partner"]["id"],
            VehicleLoading.status == "active",
        ).first()
        assert loading is not None and loading.warehouse_id == env["w2"]

    # Retry / double delivery: nothing more leaves the warehouse.
    again = client.post(f"/deliveries/{delivery['id']}/load", json={}, headers=env["auth"])
    assert again.status_code == 400
    assert _on_hand(env["w2"], env["product"]) == 40

    # End of day: the unsold goods go back into W2 (the run's source), not Main.
    r = client.post(f"/vehicle-stock/{loading.id}/end-of-day",
                    json={"items": [{"product_id": env["product"], "returned_qty": 10}]}, headers=env["auth"])
    assert r.status_code == 200, r.text
    assert _on_hand(env["w2"], env["product"]) == 50
    assert _on_hand(env["main"], env["product"]) == 100


def test_delivery_cannot_be_planned_from_another_warehouse(env):
    order, r = _order(env["auth"], env["customer"], env["w2"], env["product"], 2)
    assert r.status_code == 200, r.text
    bad, _ = _deliver(env, order["id"], r.json()["items"][0]["id"], 2, warehouse_id=env["main"])
    assert bad.status_code == 400
    assert "order's own warehouse" in bad.text
    client.patch(f"/orders/{order['id']}/cancel", json={"reason": "test"}, headers=env["auth"])


def test_quantity_above_warehouse_availability_rejected(env):
    before = _on_hand(env["w2"], env["product"])
    order, r = _order(env["auth"], env["customer"], env["w2"], env["product"], before + 1)
    assert r.status_code == 400
    assert "INSUFFICIENT_STOCK" in r.text
    # Main has plenty, but it is not this order's warehouse.
    assert _on_hand(env["main"], env["product"]) >= before + 1


def test_untracked_catalog_stock_belongs_to_default_warehouse_only(env):
    """Catalog stock no warehouse tracks yet must not be spendable from W2."""
    product = _product(env["auth"])
    with SessionLocal() as db:
        db.query(Product).filter(Product.id == product).update({"total_inventory": 30})
        db.commit()
        assert db.query(WarehouseStock).filter(WarehouseStock.product_id == product).count() == 0
    non_default = env["w2"] if env["w2"] != env["default"] else env["main"]
    _, r = _order(env["auth"], env["customer"], non_default, product, 5)
    assert r.status_code == 400 and "INSUFFICIENT_STOCK" in r.text
    _, r = _order(env["auth"], env["customer"], env["default"], product, 5)
    assert r.status_code == 200, r.text
    assert _on_hand(non_default, product) == 0


def test_cancel_releases_reservation_without_moving_stock(env):
    on_hand_before = _on_hand(env["w2"], env["product"])
    order, r = _order(env["auth"], env["customer"], env["w2"], env["product"], 5)
    assert r.status_code == 200, r.text
    assert _reserved(env["w2"], env["product"]) == 5
    r = client.patch(f"/orders/{order['id']}/cancel", json={"reason": "customer changed mind"}, headers=env["auth"])
    assert r.status_code == 200, r.text
    assert _reserved(env["w2"], env["product"]) == 0
    assert _on_hand(env["w2"], env["product"]) == on_hand_before


def test_pickup_deducts_order_warehouse_once(env):
    order, r = _order(env["auth"], env["customer"], env["w2"], env["product"], 4)
    assert r.status_code == 200, r.text
    item_id = r.json()["items"][0]["id"]
    before_w2, before_main = _on_hand(env["w2"], env["product"]), _on_hand(env["main"], env["product"])

    over = client.post(f"/orders/{order['id']}/pickup/confirm",
                       json={"items": [{"order_item_id": item_id, "collected_quantity": 5}]}, headers=env["auth"])
    assert over.status_code == 400
    assert _on_hand(env["w2"], env["product"]) == before_w2

    ok = client.post(f"/orders/{order['id']}/pickup/confirm", json={}, headers=env["auth"])
    assert ok.status_code == 200, ok.text
    assert _on_hand(env["w2"], env["product"]) == before_w2 - 4
    assert _on_hand(env["main"], env["product"]) == before_main

    twice = client.post(f"/orders/{order['id']}/pickup/confirm", json={}, headers=env["auth"])
    assert twice.status_code == 400
    assert _on_hand(env["w2"], env["product"]) == before_w2 - 4


def test_return_goes_back_to_the_order_warehouse(env):
    order, r = _order(env["auth"], env["customer"], env["w2"], env["product"], 3)
    assert r.status_code == 200, r.text
    assert client.post(f"/orders/{order['id']}/pickup/confirm", json={}, headers=env["auth"]).status_code == 200
    inv = client.post(f"/orders/{order['id']}/invoice", json={}, headers=env["auth"])
    assert inv.status_code == 201, inv.text
    invoice = inv.json()
    before_w2, before_main = _on_hand(env["w2"], env["product"]), _on_hand(env["main"], env["product"])

    r = client.post("/sales-returns", json={
        "invoice_reference_id": invoice["id"], "reason": "damaged box",
        "items": [{"invoice_item_id": invoice["items"][0]["id"], "quantity_returned": 2}],
    }, headers=env["auth"])
    assert r.status_code == 201, r.text
    ret = r.json()
    assert ret["warehouse_id"] == env["w2"]
    r = client.patch(f"/sales-returns/{ret['id']}/approve", json={
        "items": [{"return_item_id": ret["items"][0]["id"], "received_quantity": 2,
                   "condition": "saleable", "restock": True}],
    }, headers=env["auth"])
    assert r.status_code == 200, r.text
    assert _on_hand(env["w2"], env["product"]) == before_w2 + 2
    assert _on_hand(env["main"], env["product"]) == before_main


def test_order_detail_exposes_warehouse(env):
    order, r = _order(env["auth"], env["customer"], env["w2"], env["product"], 1, confirm=False)
    body = client.get(f"/orders/{order['id']}", headers=env["auth"]).json()
    assert body["warehouse_id"] == env["w2"]
    assert body["warehouse"]["id"] == env["w2"]
    assert body["warehouse"]["name"] == "Warehouse W2"
    assert body["warehouse_name"] == "Warehouse W2"


def test_cross_org_warehouse_rejected(env):
    other = _register("WH Other")
    foreign_wh = _warehouse(other["auth"], "Foreign WH")
    r = client.post("/orders", json={
        "customer_id": env["customer"], "warehouse_id": foreign_wh,
        "items": [{"product_id": env["product"], "quantity": 1, "unit_price": 100.0}],
    }, headers=env["auth"])
    assert r.status_code == 400
    for sentinel in ("all", "ALL_WAREHOUSES"):
        r = client.post("/orders", json={
            "customer_id": env["customer"], "warehouse_id": sentinel,
            "items": [{"product_id": env["product"], "quantity": 1, "unit_price": 100.0}],
        }, headers=env["auth"])
        assert r.status_code == 400

    order, r = _order(env["auth"], env["customer"], env["w2"], env["product"], 1, confirm=False)
    r = client.patch(f"/orders/{order['id']}", json={"warehouse_id": foreign_wh}, headers=env["auth"])
    assert r.status_code == 400
    r = client.post(f"/warehouses/{foreign_wh}/stock/adjust",
                    json={"product_id": env["product"], "quantity": 5}, headers=env["auth"])
    assert r.status_code == 404
    # Another org cannot see this org's order at all.
    assert client.get(f"/orders/{order['id']}", headers=other["auth"]).status_code == 404
