"""Delivery app: surplus quantity redistribution within one vehicle run.

    A ordered 5, takes 3  → 2 spare
    B ordered 3, takes 2  → 1 spare
    C ordered 2, may take 2 + 3 = 5

Covers the app-only endpoints
    GET  /deliveries/{id}/app/delivery-capacity
    POST /deliveries/{id}/app/confirm
and that the website's POST /deliveries/{id}/confirm keeps its loaded-quantity limit.
"""

import os
import sys
import threading
import uuid

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.database import SessionLocal
from app.main import app
from app.models import (
    Delivery,
    DeliveryHistory,
    DeliveryItem,
    Product,
    SalesOrderItem,
    User,
    VehicleLoading,
)
from app.services import delivery_redistribution_service, delivery_service

client = TestClient(app)


# ----------------------------------------------------------------------------- setup


def _register_org() -> dict:
    email = f"admin_{uuid.uuid4().hex[:8]}@surplus.com"
    r = client.post("/auth/register", json={
        "organization_name": f"Surplus Org {uuid.uuid4().hex[:6]}",
        "admin_name": "Admin",
        "email": email,
        "password": "Password123!",
        "role": "admin",
    })
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}


def _create_partner(admin: dict) -> tuple[str, dict]:
    email = f"dp_{uuid.uuid4().hex[:8]}@surplus.com"
    r = client.post("/users", json={
        "name": "Driver", "email": email, "password": "Password123!", "role": "delivery_partner",
    }, headers=admin)
    assert r.status_code == 201, r.text
    login = client.post("/auth/login", json={"email": email, "password": "Password123!"})
    assert login.status_code == 200, login.text
    return r.json()["id"], {"Authorization": f"Bearer {login.json()['tokens']['access_token']}"}


def _create_vehicle(admin: dict) -> str:
    r = client.post("/vehicles", json={
        "vehicle_number": f"MH12-{uuid.uuid4().hex[:6].upper()}", "vehicle_type": "Truck",
    }, headers=admin)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _create_product(admin: dict, wh_id: str, name: str = "Widget") -> str:
    r = client.post("/products", json={
        "name": name,
        "sku": f"SKU-{uuid.uuid4().hex[:6]}",
        "price": 100.0,
        "tax_rate": 0.0,
        "uom": "unit",
        "pricing": {"purchase_price": 50.0, "selling_price": 100.0, "currency": "INR"},
    }, headers=admin)
    assert r.status_code == 201, r.text
    prod_id = r.json()["id"]
    r = client.post(f"/warehouses/{wh_id}/stock/adjust", json={"product_id": prod_id, "quantity": 500}, headers=admin)
    assert r.status_code in (200, 201), r.text
    return prod_id


class World:
    """One org with a warehouse, a product, a partner and their vehicle."""

    def __init__(self) -> None:
        self.admin = _register_org()
        r = client.post("/warehouses", json={"name": "Main", "code": f"WH-{uuid.uuid4().hex[:6]}"}, headers=self.admin)
        assert r.status_code == 201, r.text
        self.wh_id = r.json()["id"]
        self.product_id = _create_product(self.admin, self.wh_id)
        self.partner_id, self.partner = _create_partner(self.admin)
        self.vehicle_id = _create_vehicle(self.admin)

    def customer(self) -> str:
        r = client.post("/customers", json={
            "name": f"Receiver {uuid.uuid4().hex[:4]}",
            "phone": f"9{uuid.uuid4().int % 10**9:09d}",
            "billing_address": "1 Road",
            "delivery_address": "1 Road",
        }, headers=self.admin)
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def delivery(self, qty: float, product_id: str | None = None, partner_id: str | None = None,
                 partner: dict | None = None, vehicle_id: str | None = None,
                 dispatch: bool = True, unit_price: float = 100.0, tax_rate: float = 0.0,
                 discount: float = 0.0) -> dict:
        """Order → plan → accept → pick → ready → load → (dispatch). Returns ids."""
        product_id = product_id or self.product_id
        partner_id = partner_id or self.partner_id
        partner = partner or self.partner
        vehicle_id = vehicle_id or self.vehicle_id

        r = client.post("/orders", json={
            "customer_id": self.customer(), "warehouse_id": self.wh_id, "source": "office",
            "items": [{"product_id": product_id, "quantity": qty, "unit_price": unit_price,
                       "tax_rate": tax_rate, "discount": discount}],
        }, headers=self.admin)
        assert r.status_code == 201, r.text
        order_id = r.json()["id"]
        r = client.post(f"/orders/{order_id}/confirm", headers=self.admin)
        assert r.status_code == 200, r.text
        order_item_id = r.json()["items"][0]["id"]

        r = client.post("/deliveries", json={
            "order_id": order_id, "delivery_partner_id": partner_id, "vehicle_id": vehicle_id,
            "warehouse_id": self.wh_id,
            "items": [{"order_item_id": order_item_id, "planned_quantity": qty}],
        }, headers=self.admin)
        assert r.status_code == 201, r.text
        delivery_id = r.json()["id"]
        item_id = r.json()["items"][0]["id"]

        r = client.post(f"/deliveries/{delivery_id}/accept", headers=partner)
        assert r.status_code == 200, r.text
        r = client.post(f"/deliveries/{delivery_id}/pick",
                        json={"items": [{"delivery_item_id": item_id, "picked_quantity": qty}]}, headers=self.admin)
        assert r.status_code == 200, r.text
        r = client.post(f"/deliveries/{delivery_id}/ready", headers=self.admin)
        assert r.status_code == 200, r.text
        r = client.post(f"/deliveries/{delivery_id}/load", json={}, headers=self.admin)
        assert r.status_code == 200, r.text
        if dispatch:
            self.dispatch(delivery_id)
        return {"order_id": order_id, "order_item_id": order_item_id, "delivery_id": delivery_id, "item_id": item_id}

    def dispatch(self, delivery_id: str) -> None:
        r = client.patch(f"/deliveries/by-id/{delivery_id}", json={"status": "in_transit"}, headers=self.admin)
        assert r.status_code == 200, r.text

    def web_confirm(self, d: dict, qty: float, auth: dict | None = None):
        return client.post(f"/deliveries/{d['delivery_id']}/confirm", json={
            "items": [{"delivery_item_id": d["item_id"], "delivered_quantity": qty}],
        }, headers=auth or self.partner)

    def capacity(self, d: dict, auth: dict | None = None):
        return client.get(f"/deliveries/{d['delivery_id']}/app/delivery-capacity", headers=auth or self.partner)

    def app_confirm(self, d: dict, qty: float, expected_max: float | None = None,
                    auth: dict | None = None, http=None):
        item = {"delivery_item_id": d["item_id"], "delivered_quantity": qty}
        if expected_max is not None:
            item["expected_max"] = expected_max
        return (http or client).post(f"/deliveries/{d['delivery_id']}/app/confirm",
                                     json={"items": [item], "receiver_name": "Gate"},
                                     headers=auth or self.partner)


def _line(delivery_item_id: str) -> DeliveryItem:
    db = SessionLocal()
    try:
        return db.get(DeliveryItem, delivery_item_id)
    finally:
        db.close()


def _order_item(order_item_id: str) -> SalesOrderItem:
    db = SessionLocal()
    try:
        return db.get(SalesOrderItem, order_item_id)
    finally:
        db.close()


def _abc(w: World) -> tuple[dict, dict, dict]:
    """A (5, takes 3) and B (3, takes 2) visited; C (2) still to go."""
    a = w.delivery(5)
    b = w.delivery(3)
    c = w.delivery(2)
    assert w.web_confirm(a, 3).status_code == 200
    assert w.web_confirm(b, 2).status_code == 200
    return a, b, c


# ----------------------------------------------------------------------------- tests


def test_abc_redistribution_end_to_end():
    w = World()
    a, b, c = _abc(w)

    r = w.capacity(c)
    assert r.status_code == 200, r.text
    cap = r.json()
    line = cap["items"][0]
    assert cap["vehicle_loading_id"]
    assert line["ordered_quantity"] == 2
    assert line["own_remaining"] == 2
    assert line["transferable_surplus"] == 3
    assert line["max_allowed_delivery"] == 5
    assert {s["delivery_id"]: s["available"] for s in line["surplus_sources"]} == {
        a["delivery_id"]: 2, b["delivery_id"]: 1,
    }

    r = w.app_confirm(c, 5, expected_max=5)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["items"][0]["delivered_quantity"] == 5
    assert body["internal_status"] == "delivered"
    moved = {x["from_delivery_id"]: x["quantity"] for x in body["reallocations"]}
    assert moved == {a["delivery_id"]: 2, b["delivery_id"]: 1}

    # Line figures: A 5→3, B 3→2, C 2→5 loaded; delivered 3 / 2 / 5.
    assert (_line(a["item_id"]).loaded_quantity, _line(a["item_id"]).delivered_quantity) == (3, 3)
    assert (_line(b["item_id"]).loaded_quantity, _line(b["item_id"]).delivered_quantity) == (2, 2)
    assert (_line(c["item_id"]).loaded_quantity, _line(c["item_id"]).delivered_quantity) == (5, 5)

    # Ordered quantities never change; order lines record what was received.
    for d, ordered, delivered in ((a, 5, 3), (b, 3, 2), (c, 2, 5)):
        oi = _order_item(d["order_item_id"])
        assert oi.quantity == ordered
        assert oi.delivered_quantity == delivered

    # Vehicle stock: 10 loaded, 10 handed over, nothing left.
    db = SessionLocal()
    try:
        loading = db.get(VehicleLoading, cap["vehicle_loading_id"])
        vli = next(x for x in loading.items if x.product_id == w.product_id)
        assert (vli.loaded_qty, vli.delivered_qty) == (10, 10)
        # Audit: one quantity_reallocated entry per side per move.
        events = db.query(DeliveryHistory).filter(DeliveryHistory.event_type == "quantity_reallocated").filter(
            DeliveryHistory.delivery_id.in_([a["delivery_id"], b["delivery_id"], c["delivery_id"]])
        ).all()
        assert len(events) == 4
        meta = next(e.event_metadata for e in events if e.delivery_id == a["delivery_id"])
        assert meta["from_delivery_item_id"] == a["item_id"]
        assert meta["to_delivery_item_id"] == c["item_id"]
        assert meta["product_id"] == w.product_id
        assert meta["quantity"] == 2
    finally:
        db.close()

    # Invoicing C bills the 5 actually received at the order-line price.
    r = client.post(f"/orders/{c['order_id']}/invoice", json={"delivery_id": c["delivery_id"]}, headers=w.admin)
    assert r.status_code == 201, r.text
    assert sum(i["quantity"] for i in r.json()["items"]) == 5
    assert _order_item(c["order_item_id"]).quantity == 2


def test_a_surplus_alone_can_be_consumed():
    w = World()
    a = w.delivery(5)
    c = w.delivery(2)
    assert w.web_confirm(a, 3).status_code == 200
    r = w.app_confirm(c, 4)
    assert r.status_code == 200, r.text
    assert r.json()["reallocations"] == [{
        "from_delivery_id": a["delivery_id"], "from_delivery_item_id": a["item_id"],
        "to_delivery_item_id": c["item_id"], "product_id": w.product_id, "variant_id": None, "quantity": 2.0,
    }]


def test_b_surplus_alone_can_be_consumed():
    w = World()
    b = w.delivery(3)
    c = w.delivery(2)
    assert w.web_confirm(b, 2).status_code == 200
    r = w.app_confirm(c, 3)
    assert r.status_code == 200, r.text
    assert [(x["from_delivery_id"], x["quantity"]) for x in r.json()["reallocations"]] == [(b["delivery_id"], 1.0)]


def test_cannot_exceed_own_plus_surplus():
    w = World()
    a, b, c = _abc(w)
    r = w.app_confirm(c, 6)
    assert r.status_code == 400, r.text
    assert "at most 5" in r.json()["detail"]
    # Nothing moved.
    assert _line(a["item_id"]).loaded_quantity == 5
    assert _line(c["item_id"]).loaded_quantity == 2
    assert _line(c["item_id"]).delivered_quantity == 0


def test_stale_expected_max_is_409():
    w = World()
    a, b, c = _abc(w)
    r = w.app_confirm(c, 2, expected_max=4)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["max_allowed_delivery"] == 5
    assert _line(c["item_id"]).delivered_quantity == 0


def test_delivery_within_own_quantity_moves_nothing():
    w = World()
    a, b, c = _abc(w)
    r = w.app_confirm(c, 1)
    assert r.status_code == 200, r.text
    assert r.json()["reallocations"] == []
    assert r.json()["internal_status"] == "partially_delivered"
    assert _line(a["item_id"]).loaded_quantity == 5


def test_different_partner_gives_no_surplus():
    w = World()
    other_id, other_auth = _create_partner(w.admin)
    other_vehicle = _create_vehicle(w.admin)
    a = w.delivery(5, partner_id=other_id, partner=other_auth, vehicle_id=other_vehicle)
    assert w.web_confirm(a, 3, auth=other_auth).status_code == 200
    c = w.delivery(2)
    line = w.capacity(c).json()["items"][0]
    assert line["transferable_surplus"] == 0
    assert w.app_confirm(c, 3).status_code == 400


def test_different_vehicle_gives_no_surplus():
    w = World()
    a, b, c = _abc(w)
    other_vehicle = _create_vehicle(w.admin)
    db = SessionLocal()
    try:
        for d in (a, b):
            db.get(Delivery, d["delivery_id"]).vehicle_id = other_vehicle
        db.commit()
    finally:
        db.close()
    assert w.capacity(c).json()["items"][0]["transferable_surplus"] == 0
    assert w.app_confirm(c, 3).status_code == 400


def test_different_product_or_variant_gives_no_surplus():
    w = World()
    other_product = _create_product(w.admin, w.wh_id, "Gadget")
    a = w.delivery(5, product_id=other_product)
    assert w.web_confirm(a, 3).status_code == 200
    c = w.delivery(2)
    assert w.capacity(c).json()["items"][0]["transferable_surplus"] == 0
    assert w.app_confirm(c, 3).status_code == 400


def test_previous_loading_session_gives_no_surplus():
    w = World()
    a = w.delivery(5)
    assert w.web_confirm(a, 3).status_code == 200
    # Close the run: the 2 spare go back to the warehouse.
    db = SessionLocal()
    try:
        loading_id = db.query(VehicleLoading.id).filter(
            VehicleLoading.delivery_partner_id == w.partner_id, VehicleLoading.status == "active",
        ).scalar()
    finally:
        db.close()
    r = client.post(f"/vehicle-stock/{loading_id}/end-of-day",
                    json={"items": [{"product_id": w.product_id, "returned_qty": 2}]}, headers=w.admin)
    assert r.status_code == 200, r.text

    c = w.delivery(2)  # opens a new session
    cap = w.capacity(c).json()
    assert cap["vehicle_loading_id"] != loading_id
    assert cap["items"][0]["transferable_surplus"] == 0
    assert w.app_confirm(c, 3).status_code == 400
    assert _line(a["item_id"]).loaded_quantity == 5


def test_in_transit_donor_is_not_eligible():
    w = World()
    a = w.delivery(5)  # dispatched, not yet visited
    c = w.delivery(2)
    assert w.capacity(c).json()["items"][0]["transferable_surplus"] == 0
    assert w.app_confirm(c, 3).status_code == 400
    assert _line(a["item_id"]).loaded_quantity == 5


def test_reattempt_on_donor_cannot_reuse_redistributed_units():
    w = World()
    a, b, c = _abc(w)
    assert w.app_confirm(c, 5).status_code == 200
    # A was part-delivered; its 2 spare went to C. The website's re-attempt is refused.
    r = w.web_confirm(a, 1)
    assert r.status_code == 400, r.text
    # And A cannot pull them back through the app either.
    assert w.capacity(a).json()["items"][0]["max_allowed_delivery"] == 0
    assert w.app_confirm(a, 1).status_code == 400


def test_concurrent_confirms_cannot_overspend():
    w = World()
    a = w.delivery(5)
    c = w.delivery(2)
    d = w.delivery(2)
    assert w.web_confirm(a, 3).status_code == 200  # 2 spare

    barrier = threading.Barrier(2)
    results = {}

    def run(key, target):
        http = TestClient(app)
        barrier.wait()
        results[key] = w.app_confirm(target, 4, http=http)

    threads = [threading.Thread(target=run, args=("c", c)), threading.Thread(target=run, args=("d", d))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    codes = sorted(r.status_code for r in results.values())
    assert codes == [200, 400], [(k, r.status_code, r.text) for k, r in results.items()]
    delivered = _line(c["item_id"]).delivered_quantity + _line(d["item_id"]).delivered_quantity
    assert delivered == 4
    assert _line(a["item_id"]).loaded_quantity == 3


def _set_tracking(product_id: str, *, batch: bool = False, serial: bool = False) -> None:
    db = SessionLocal()
    try:
        product = db.get(Product, product_id)
        product.batch_tracking = batch
        product.serial_number_tracking = serial
        db.commit()
    finally:
        db.close()


def _set_batch(item_id: str, batch_number: str) -> None:
    db = SessionLocal()
    try:
        db.get(DeliveryItem, item_id).batch_number = batch_number
        db.commit()
    finally:
        db.close()


def test_batch_tracked_mismatch_rejected_and_match_allowed():
    w = World()
    a, b, c = _abc(w)
    _set_tracking(w.product_id, batch=True)
    _set_batch(a["item_id"], "LOT-1")
    _set_batch(b["item_id"], "LOT-2")
    _set_batch(c["item_id"], "LOT-1")

    line = w.capacity(c).json()["items"][0]
    assert line["transferable_surplus"] == 2  # only A's lot matches
    r = w.app_confirm(c, 5)
    assert r.status_code == 400, r.text
    assert "different batch" in r.json()["detail"]
    assert _line(b["item_id"]).loaded_quantity == 3

    r = w.app_confirm(c, 4)
    assert r.status_code == 200, r.text
    assert [x["from_delivery_id"] for x in r.json()["reallocations"]] == [a["delivery_id"]]


def test_serial_tracked_rejected():
    w = World()
    a, b, c = _abc(w)
    _set_tracking(w.product_id, serial=True)
    line = w.capacity(c).json()["items"][0]
    assert line["transferable_surplus"] == 0
    assert "Serial-tracked" in line["redistribution_blocked_reason"]
    r = w.app_confirm(c, 3)
    assert r.status_code == 400, r.text
    assert "Serial-tracked" in r.json()["detail"]
    # Delivering within its own quantity is still fine.
    assert w.app_confirm(c, 2).status_code == 200


def test_website_confirm_unchanged_still_capped_at_loaded():
    w = World()
    a, b, c = _abc(w)
    r = w.web_confirm(c, 5)
    assert r.status_code == 400, r.text
    assert "still on the vehicle" in r.json()["detail"]
    assert _line(a["item_id"]).loaded_quantity == 5


def test_tenant_isolation_and_ownership():
    w = World()
    a, b, c = _abc(w)
    other = World()
    # Another org's admin and partner cannot see or confirm this delivery.
    assert w.capacity(c, auth=other.admin).status_code == 404
    assert w.capacity(c, auth=other.partner).status_code == 404
    assert w.app_confirm(c, 5, auth=other.partner).status_code == 404
    # A same-org admin is not the assigned partner — app endpoints are the partner's.
    assert w.capacity(c, auth=w.admin).status_code == 403
    assert w.app_confirm(c, 5, auth=w.admin).status_code == 403
    # Another org's spare units never show up here.
    a2 = other.delivery(5)
    assert other.web_confirm(a2, 1).status_code == 200
    assert w.capacity(c).json()["items"][0]["transferable_surplus"] == 3


# ----------------------------------------------------------------------------- delivered value


def test_delivered_value_and_app_amount_due_follow_delivered_units():
    w = World()
    # 100 each, 12% tax, line discount 20 on C (so 10 per unit).
    a = w.delivery(5, tax_rate=12)
    b = w.delivery(3, tax_rate=12)
    c = w.delivery(2, tax_rate=12, discount=20)
    assert w.web_confirm(a, 3).status_code == 200
    assert w.web_confirm(b, 2).status_code == 200

    before = w.capacity(c).json()
    assert before["delivered_value"] == 0
    assert before["app_amount_due"] == 0

    r = w.app_confirm(c, 5)
    assert r.status_code == 200, r.text
    body = r.json()
    # 5 × 100 − 20 × 5/2 = 450, + 12% = 504. The order itself is 2 × 100 − 20 = 180 + 12% = 201.6.
    assert body["delivered_value"] == 504.0
    assert body["order_delivered_value"] == 504.0
    assert body["paid_amount"] == 0
    assert body["app_amount_due"] == 504.0
    # The shared figure is untouched: still the order total less payments.
    assert body["order_total"] == 201.6
    assert body["amount_due"] == 201.6
    detail = client.get(f"/deliveries/by-id/{c['delivery_id']}", headers=w.partner).json()
    assert detail["amount_due"] == 201.6
    assert _order_item(c["order_item_id"]).quantity == 2

    # The figure matches what the invoice endpoint actually bills for this delivery.
    inv = client.post(f"/orders/{c['order_id']}/invoice", json={"delivery_id": c["delivery_id"]}, headers=w.admin)
    assert inv.status_code == 201, inv.text
    assert inv.json()["total"] == body["delivered_value"]

    # A payment reduces app_amount_due; amount_due keeps its own meaning.
    pay = client.post("/payment-receipts", json={
        "invoice_reference_id": inv.json()["id"], "amount_received": 104.0, "payment_method": "cash",
    }, headers=w.admin)
    assert pay.status_code == 201, pay.text
    after = w.capacity(c).json()
    assert after["paid_amount"] == 104.0
    assert after["app_amount_due"] == 400.0
    detail = client.get(f"/deliveries/by-id/{c['delivery_id']}", headers=w.partner).json()
    assert detail["amount_due"] == 97.6  # 201.6 − 104, exactly as before this feature


# ----------------------------------------------------------------------------- website vs app race


def _invariants(w: World, deliveries: list[dict]) -> int:
    """Every line delivered ≤ loaded; vehicle delivered ≤ loaded. Returns total delivered."""
    total = 0.0
    for d in deliveries:
        line = _line(d["item_id"])
        assert line.delivered_quantity <= line.loaded_quantity + 0.001, (line.delivered_quantity, line.loaded_quantity)
        total += line.delivered_quantity
    db = SessionLocal()
    try:
        loading = db.query(VehicleLoading).filter(
            VehicleLoading.delivery_partner_id == w.partner_id, VehicleLoading.status == "active",
        ).first()
        vli = next(x for x in loading.items if x.product_id == w.product_id)
        assert vli.delivered_qty <= vli.loaded_qty, (vli.delivered_qty, vli.loaded_qty)
        assert vli.delivered_qty == total
    finally:
        db.close()
    return total


def test_stale_website_confirm_after_app_redistribution_is_rejected():
    """The interleaving that used to corrupt: the website request reads A, the app
    moves A's spare units to C and commits, then the website request carries on."""
    w = World()
    a, b, c = _abc(w)

    web = SessionLocal()
    try:
        web_user = web.get(User, w.partner_id)
        web_delivery = web.get(Delivery, a["delivery_id"])
        assert [(i.loaded_quantity, i.delivered_quantity) for i in web_delivery.items] == [(5, 3)]

        app_s = SessionLocal()
        try:
            delivery_redistribution_service.confirm(
                app_s, app_s.get(User, w.partner_id), app_s.get(Delivery, c["delivery_id"]),
                lines=[{"delivery_item_id": c["item_id"], "delivered_quantity": 5, "expected_max": None}],
                pod_photo_file_ids=[], signature_file_id=None, notes=None, failed=False, failure_reason=None,
            )
            app_s.commit()
        finally:
            app_s.close()

        try:
            delivery_service.confirm(
                web, web_user, web_delivery,
                lines=[{"delivery_item_id": a["item_id"], "delivered_quantity": 2}],
                pod_photo_file_ids=[], signature_file_id=None, notes=None, failed=False, failure_reason=None,
            )
            web.commit()
            rejected = None
        except Exception as exc:  # noqa: BLE001
            web.rollback()
            rejected = exc
    finally:
        web.close()

    # The website's ordinary validation error, against the fresh figures.
    assert rejected is not None
    assert getattr(rejected, "status_code", None) == 400
    assert "still on the vehicle" in rejected.detail
    assert (_line(a["item_id"]).loaded_quantity, _line(a["item_id"]).delivered_quantity) == (3, 3)
    assert _order_item(a["order_item_id"]).delivered_quantity == 3
    assert _invariants(w, [a, b, c]) == 10


def test_concurrent_website_and_app_confirm_one_wins():
    w = World()
    a, b, c = _abc(w)

    barrier = threading.Barrier(2)
    results = {}

    def web():
        http = TestClient(app)
        barrier.wait()
        results["web"] = http.post(f"/deliveries/{a['delivery_id']}/confirm", json={
            "items": [{"delivery_item_id": a["item_id"], "delivered_quantity": 2}],
        }, headers=w.partner)

    def app_side():
        http = TestClient(app)
        barrier.wait()
        results["app"] = w.app_confirm(c, 5, http=http)

    threads = [threading.Thread(target=web), threading.Thread(target=app_side)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    codes = {k: r.status_code for k, r in results.items()}
    assert sorted(codes.values()) == [200, 400], {k: (r.status_code, r.text) for k, r in results.items()}
    loser = next(r for r in results.values() if r.status_code == 400)
    assert ("still on the vehicle" in loser.json()["detail"]) or ("at most" in loser.json()["detail"])

    total = _invariants(w, [a, b, c])
    if codes["web"] == 200:   # A took its 2 back first; C only had its own 2 + B's 1
        assert total == 7
        assert _line(c["item_id"]).delivered_quantity == 0
    else:                     # C took A's 2 and B's 1 first; A's re-attempt refused
        assert total == 10
        assert _line(a["item_id"]).delivered_quantity == 3

    # No unit is billed twice: invoicing every delivery bills exactly what was delivered.
    invoiced = 0.0
    for d in (a, b, c):
        if _line(d["item_id"]).delivered_quantity <= 0:
            continue
        r = client.post(f"/orders/{d['order_id']}/invoice", json={"delivery_id": d["delivery_id"]}, headers=w.admin)
        assert r.status_code == 201, r.text
        invoiced += sum(i["quantity"] for i in r.json()["items"])
    assert invoiced == total <= 10
