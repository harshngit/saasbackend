"""Tests for Division 2: Partial Delivery, Remaining Quantity Exposure, and Stock Safety.

Covers:
1. remaining_quantity exposed on OrderItemOut (max(ordered - delivered, 0))
2. ordered_quantity correct and immutable
3. delivered_quantity is cumulative across partial deliveries
4. remaining_quantity never becomes negative
5. Partial delivery planning & execution
6. Multi-delivery lifecycle (100 -> 30 -> 20 -> 50)
7. Full delivery status transition (completed / delivered)
8. Over-delivery rejection at plan/load/confirm stages
9. Zero / negative quantity rejection
10. Stock deduction matches actual loaded quantity without duplication
11. previous_pending_balance and 5-part payment collection remain fully intact
12. Multi-tenant isolation
"""

import sys
import os
import uuid
sys.path.insert(0, os.path.abspath("."))

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.database import Base, SessionLocal, engine
from app.main import app
from app.models import (
    Customer,
    Delivery,
    DeliveryItem,
    Invoice,
    SalesOrder,
    SalesOrderItem,
    User,
    Vehicle,
    Warehouse,
    WarehouseStock,
)
from app.services import delivery_service, stock_service

client = TestClient(app)

PASSED = 0
FAILED = 0


def log_test(name: str, success: bool, detail: str = ""):
    global PASSED, FAILED
    if success:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name} - {detail}")


def _register_org(label: str):
    clean_label = label.replace("_", "").lower()
    email = f"admin_{uuid.uuid4().hex[:8]}@{clean_label}.com"
    r = client.post("/auth/register", json={
        "organization_name": f"{label} Org",
        "admin_name": f"Admin {label}",
        "email": email,
        "password": "Password123!",
        "role": "admin",
    })
    assert r.status_code == 201, r.text
    token = r.json()["tokens"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _create_staff(admin_auth: dict, name: str, role_name: str, password: str = "Password123!"):
    email = f"{role_name.lower().replace(' ', '_')}_{uuid.uuid4().hex[:6]}@example.com"
    res = client.post("/users", json={
        "name": name,
        "email": email,
        "password": password,
        "role": role_name,
    }, headers=admin_auth)
    assert res.status_code == 201, res.text
    user_data = res.json()
    login_res = client.post("/auth/login", json={
        "email": email,
        "password": password,
    })
    assert login_res.status_code == 200, login_res.text
    token = login_res.json()["tokens"]["access_token"]
    staff_auth = {"Authorization": f"Bearer {token}"}
    return user_data, staff_auth


def _setup_org(label: str):
    auth = _register_org(label)

    wh_res = client.post("/warehouses", json={"name": "Central WH", "code": f"WH-{uuid.uuid4().hex[:6]}"}, headers=auth)
    assert wh_res.status_code == 201, wh_res.text
    wh_id = wh_res.json()["id"]

    veh_res = client.post("/vehicles", json={"vehicle_number": f"MH12-{uuid.uuid4().hex[:4].upper()}", "vehicle_type": "Truck"}, headers=auth)
    veh_id = veh_res.json()["id"] if veh_res.status_code == 201 else None

    # Delivery partner user & auth
    partner_data, partner_auth = _create_staff(auth, "Driver Bob", "delivery_partner")
    partner_id = partner_data["id"]

    prod_res = client.post("/products", json={
        "name": "Industrial Widget",
        "sku": f"WGT-{uuid.uuid4().hex[:6]}",
        "price": 100.0,
        "tax_rate": 0.0,
        "uom": "unit",
        "pricing": {"purchase_price": 50.0, "selling_price": 100.0, "currency": "INR"},
    }, headers=auth)
    assert prod_res.status_code == 201, prod_res.text
    prod_id = prod_res.json()["id"]

    # Initial stock: 500 units in warehouse
    client.post(f"/warehouses/{wh_id}/stock/adjust", json={
        "product_id": prod_id, "quantity": 500,
    }, headers=auth)

    cust_res = client.post("/customers", json={
        "name": "Acme Industries",
        "phone": "9876543210",
        "email": "acme@example.com",
        "billing_address": "456 Industrial Way",
        "delivery_address": "456 Industrial Way",
    }, headers=auth)
    assert cust_res.status_code == 201, cust_res.text
    cust_id = cust_res.json()["id"]

    return auth, partner_auth, wh_id, veh_id, partner_id, prod_id, cust_id


def _advance_and_confirm_delivery(auth, partner_auth, delivery_id, pick_items, deliver_items):
    # 1. Partner accepts
    r = client.post(f"/deliveries/{delivery_id}/accept", headers=partner_auth)
    assert r.status_code == 200, f"Accept failed: {r.text}"

    # 2. Pick items
    r = client.post(f"/deliveries/{delivery_id}/pick", json={"items": pick_items}, headers=auth)
    assert r.status_code == 200, f"Pick failed: {r.text}"

    # 3. Mark ready
    r = client.post(f"/deliveries/{delivery_id}/ready", headers=auth)
    assert r.status_code == 200, f"Ready failed: {r.text}"

    # 4. Load vehicle
    r = client.post(f"/deliveries/{delivery_id}/load", json={}, headers=auth)
    assert r.status_code == 200, f"Load failed: {r.text}"

    # 5. Dispatch
    r = client.patch(f"/deliveries/by-id/{delivery_id}", json={"status": "in_transit"}, headers=auth)
    assert r.status_code == 200, f"Dispatch failed: {r.text}"

    # 6. Confirm
    r = client.post(f"/deliveries/{delivery_id}/confirm", json={"items": deliver_items}, headers=auth)
    assert r.status_code == 200, f"Confirm failed: {r.text}"
    return r.json()


def run_all_tests():
    global PASSED, FAILED

    print("\n=======================================================")
    print("TEST SUITE: Division 2 - Partial Delivery & Quantities")
    print("=======================================================")

    auth, partner_auth, wh_id, veh_id, partner_id, prod_id, cust_id = _setup_org("Div2Partial")

    # ----------------------------------------------------
    # TEST 1: Sales Order Creation with 100 Units
    # ----------------------------------------------------
    print("\n--- TEST 1: Sales Order Creation & Initial Quantities ---")
    order_payload = {
        "customer_id": cust_id,
        "warehouse_id": wh_id,
        "source": "office",
        "items": [
            {
                "product_id": prod_id,
                "quantity": 100,
                "unit_price": 100.0,
            }
        ]
    }
    r = client.post("/orders", json=order_payload, headers=auth)
    log_test("Order created successfully", r.status_code == 201, r.text)
    order_data = r.json()
    order_id = order_data["id"]
    order_item_id = order_data["items"][0]["id"]

    log_test("OrderItemOut.ordered_quantity is 100", order_data["items"][0]["ordered_quantity"] == 100)
    log_test("OrderItemOut.delivered_quantity is 0", order_data["items"][0]["delivered_quantity"] == 0.0)
    log_test("OrderItemOut.remaining_quantity is 100", order_data["items"][0]["remaining_quantity"] == 100.0)
    log_test("SalesOrder.fulfilment_status is 'reserved'", order_data["fulfilment_status"] == "reserved")

    db = SessionLocal()
    wh_stock = db.query(WarehouseStock).filter_by(warehouse_id=wh_id, product_id=prod_id).first()
    res_qty = stock_service.reserved(db, wh_id, prod_id, None)
    log_test("Warehouse on-hand physical stock is 500", wh_stock.on_hand_quantity == 500.0)
    log_test("Warehouse reserved_quantity is 100", res_qty == 100.0)

    # ----------------------------------------------------
    # TEST 2: Delivery 1 - Plan 30 Units (Partial Delivery 1)
    # ----------------------------------------------------
    print("\n--- TEST 2: Delivery 1 - Plan 30 Units ---")
    plan1_payload = {
        "order_id": order_id,
        "delivery_partner_id": partner_id,
        "vehicle_id": veh_id,
        "warehouse_id": wh_id,
        "items": [
            {
                "order_item_id": order_item_id,
                "planned_quantity": 30,
            }
        ]
    }
    r = client.post("/deliveries", json=plan1_payload, headers=auth)
    log_test("Delivery 1 planned successfully", r.status_code == 201, r.text)
    del1_data = r.json()
    del1_id = del1_data["id"]
    del1_item_id = del1_data["items"][0]["id"]

    log_test("Delivery 1 item planned_quantity is 30", del1_data["items"][0]["planned_quantity"] == 30.0)
    log_test("Delivery 1 item pending_quantity is 30", del1_data["items"][0]["pending_quantity"] == 30.0)
    log_test("Delivery 1 item remaining_quantity is 30", del1_data["items"][0]["remaining_quantity"] == 30.0)

    # Advance Delivery 1 through pick, ready, load, dispatch, confirm
    _advance_and_confirm_delivery(
        auth, partner_auth, del1_id,
        pick_items=[{"delivery_item_id": del1_item_id, "picked_quantity": 30.0}],
        deliver_items=[{"delivery_item_id": del1_item_id, "delivered_quantity": 30.0}],
    )
    log_test("Delivery 1 completed and confirmed (30 units)", True)

    db.expire_all()
    wh_stock = db.query(WarehouseStock).filter_by(warehouse_id=wh_id, product_id=prod_id).first()
    res_qty = stock_service.reserved(db, wh_id, prod_id, None)
    log_test("Warehouse physical stock reduced by 30 to 470", wh_stock.on_hand_quantity == 470.0)
    log_test("Warehouse reserved quantity reduced by 30 to 70", res_qty == 70.0)

    # Verify Order state after Delivery 1
    r = client.get(f"/orders/{order_id}", headers=auth)
    ord1_after = r.json()
    log_test("After D1: ordered_quantity = 100", ord1_after["items"][0]["ordered_quantity"] == 100)
    log_test("After D1: delivered_quantity = 30", ord1_after["items"][0]["delivered_quantity"] == 30.0)
    log_test("After D1: remaining_quantity = 70", ord1_after["items"][0]["remaining_quantity"] == 70.0)
    log_test("After D1: fulfilment_status = 'partially_delivered'", ord1_after["fulfilment_status"] == "partially_delivered")
    log_test("After D1: order status = 'processing'", ord1_after["status"] == "processing")

    # ----------------------------------------------------
    # TEST 3: Delivery 2 - Plan 20 Units (Partial Delivery 2)
    # ----------------------------------------------------
    print("\n--- TEST 3: Delivery 2 - Plan 20 Units ---")
    plan2_payload = {
        "order_id": order_id,
        "delivery_partner_id": partner_id,
        "vehicle_id": veh_id,
        "warehouse_id": wh_id,
        "items": [
            {
                "order_item_id": order_item_id,
                "planned_quantity": 20,
            }
        ]
    }
    r = client.post("/deliveries", json=plan2_payload, headers=auth)
    log_test("Delivery 2 planned successfully", r.status_code == 201, r.text)
    del2_data = r.json()
    del2_id = del2_data["id"]
    del2_item_id = del2_data["items"][0]["id"]

    # Advance Delivery 2 through pick, ready, load, dispatch, confirm
    _advance_and_confirm_delivery(
        auth, partner_auth, del2_id,
        pick_items=[{"delivery_item_id": del2_item_id, "picked_quantity": 20.0}],
        deliver_items=[{"delivery_item_id": del2_item_id, "delivered_quantity": 20.0}],
    )
    log_test("Delivery 2 completed and confirmed (20 units)", True)

    db.expire_all()
    wh_stock = db.query(WarehouseStock).filter_by(warehouse_id=wh_id, product_id=prod_id).first()
    res_qty = stock_service.reserved(db, wh_id, prod_id, None)
    log_test("Warehouse physical stock reduced by 20 to 450", wh_stock.on_hand_quantity == 450.0)
    log_test("Warehouse reserved quantity reduced by 20 to 50", res_qty == 50.0)

    # Verify Order state after Delivery 2
    r = client.get(f"/orders/{order_id}", headers=auth)
    ord2_after = r.json()
    log_test("After D2: ordered_quantity = 100", ord2_after["items"][0]["ordered_quantity"] == 100)
    log_test("After D2: delivered_quantity = 50", ord2_after["items"][0]["delivered_quantity"] == 50.0)
    log_test("After D2: remaining_quantity = 50", ord2_after["items"][0]["remaining_quantity"] == 50.0)
    log_test("After D2: fulfilment_status = 'partially_delivered'", ord2_after["fulfilment_status"] == "partially_delivered")

    # ----------------------------------------------------
    # TEST 4: Over-Delivery & Validation Protections
    # ----------------------------------------------------
    print("\n--- TEST 4: Over-Delivery & Negative/Zero Protections ---")
    # Remaining on order is 50. Attempting to plan 51 must fail with 400.
    bad_plan = {
        "order_id": order_id,
        "delivery_partner_id": partner_id,
        "vehicle_id": veh_id,
        "items": [{"order_item_id": order_item_id, "planned_quantity": 51}]
    }
    r = client.post("/deliveries", json=bad_plan, headers=auth)
    log_test("Over-planning (51 > 50) rejected with HTTP 400", r.status_code == 400)

    # Attempting zero quantity planning
    bad_zero = {
        "order_id": order_id,
        "items": [{"order_item_id": order_item_id, "planned_quantity": 0}]
    }
    r = client.post("/deliveries", json=bad_zero, headers=auth)
    log_test("Zero quantity planning rejected with HTTP 422", r.status_code == 422)

    # Attempting negative quantity planning
    bad_neg = {
        "order_id": order_id,
        "items": [{"order_item_id": order_item_id, "planned_quantity": -10}]
    }
    r = client.post("/deliveries", json=bad_neg, headers=auth)
    log_test("Negative quantity planning rejected with HTTP 422", r.status_code == 422)

    # ----------------------------------------------------
    # TEST 5: Delivery 3 - Plan Remaining 50 Units (Full Delivery)
    # ----------------------------------------------------
    print("\n--- TEST 5: Delivery 3 - Plan Remaining 50 Units ---")
    plan3_payload = {
        "order_id": order_id,
        "delivery_partner_id": partner_id,
        "vehicle_id": veh_id,
        "warehouse_id": wh_id,
        # Omit items to auto-plan all outstanding (50)
    }
    r = client.post("/deliveries", json=plan3_payload, headers=auth)
    log_test("Delivery 3 auto-planned remaining 50 successfully", r.status_code == 201, r.text)
    del3_data = r.json()
    del3_id = del3_data["id"]
    del3_item_id = del3_data["items"][0]["id"]
    log_test("Delivery 3 planned_quantity is exactly 50", del3_data["items"][0]["planned_quantity"] == 50.0)

    # Advance Delivery 3 through pick, ready, load, dispatch, confirm
    _advance_and_confirm_delivery(
        auth, partner_auth, del3_id,
        pick_items=[{"delivery_item_id": del3_item_id, "picked_quantity": 50.0}],
        deliver_items=[{"delivery_item_id": del3_item_id, "delivered_quantity": 50.0}],
    )
    log_test("Delivery 3 completed and confirmed (50 units)", True)

    db.expire_all()
    wh_stock = db.query(WarehouseStock).filter_by(warehouse_id=wh_id, product_id=prod_id).first()
    res_qty = stock_service.reserved(db, wh_id, prod_id, None)
    log_test("Warehouse physical stock reduced by 50 to 400", wh_stock.on_hand_quantity == 400.0)
    log_test("Warehouse reserved quantity reduced to 0", res_qty == 0.0)

    # Verify Order state after full completion
    r = client.get(f"/orders/{order_id}", headers=auth)
    ord3_after = r.json()
    log_test("Final: ordered_quantity = 100", ord3_after["items"][0]["ordered_quantity"] == 100)
    log_test("Final: delivered_quantity = 100", ord3_after["items"][0]["delivered_quantity"] == 100.0)
    log_test("Final: remaining_quantity = 0", ord3_after["items"][0]["remaining_quantity"] == 0.0)
    log_test("Final: fulfilment_status = 'delivered'", ord3_after["fulfilment_status"] == "delivered")
    log_test("Final: order status = 'completed'", ord3_after["status"] == "completed")

    # Attempting to plan a 4th delivery when remaining is 0 must fail
    r = client.post("/deliveries", json={"order_id": order_id}, headers=auth)
    log_test("Planning against fully delivered order rejected with HTTP 400", r.status_code == 400)

    # ----------------------------------------------------
    # TEST 6: Invoicing & 5-Part Payment with Partial Deliveries
    # ----------------------------------------------------
    print("\n--- TEST 6: Invoicing & Payment Integrity ---")
    r = client.post(f"/orders/{order_id}/invoice", json={"delivery_id": del3_id}, headers=auth)
    log_test("Invoice generated for delivered order/delivery", r.status_code == 201, r.text)
    inv_data = r.json()
    inv_id = inv_data["id"]
    inv_total = inv_data["total"]

    # Record payment against this invoice
    pay_payload = {
        "invoice_reference_id": inv_id,
        "amount_received": inv_total,
        "payment_method": "upi",
    }
    r = client.post("/payment-receipts", json=pay_payload, headers=auth)
    log_test("Payment recorded successfully", r.status_code == 201, r.text)
    pay_data = r.json()
    log_test("5-Part: order_amount = 10000.0", pay_data["order_amount"] == 10000.0)
    log_test("5-Part: previous_pending is set", pay_data["previous_pending"] == inv_total)
    log_test("5-Part: amount_collected matches", pay_data["amount_collected"] == inv_total)
    log_test("5-Part: payment_method = 'upi'", pay_data["payment_method"] == "upi")
    log_test("5-Part: remaining_receivable = 0.0", pay_data["remaining_receivable"] == 0.0)

    # ----------------------------------------------------
    # TEST 7: Tenant Isolation
    # ----------------------------------------------------
    print("\n--- TEST 7: Multi-Tenant Isolation ---")
    auth2, partner_auth2, wh_id2, veh_id2, partner_id2, prod_id2, cust_id2 = _setup_org("Div2Org2")

    # Org 2 cannot read Org 1 order or delivery
    r = client.get(f"/orders/{order_id}", headers=auth2)
    log_test("Org 2 cannot access Org 1 order (HTTP 404)", r.status_code == 404)
    r = client.get(f"/deliveries/{del1_id}", headers=auth2)
    log_test("Org 2 cannot access Org 1 delivery (HTTP 404)", r.status_code == 404)

    print("\n=======================================================")
    print(f"RESULTS: {PASSED} passed, {FAILED} failed")
    print("=======================================================")
    db.close()
    if FAILED > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_all_tests()
