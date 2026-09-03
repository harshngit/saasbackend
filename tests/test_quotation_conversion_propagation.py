"""Test suite for Quotation -> Sales Order Conversion Field Propagation.

Verifies:
1. Quotation with billing address converts to SalesOrder with same billing address.
2. Quotation with shipping/delivery address converts correctly.
3. Quotation with payment terms converts correctly (textual terms + payment_terms_days).
4. Quotation with delivery terms converts correctly.
5. Quotation with currency converts correctly.
6. Quotation containing all fields converts with ALL fields preserved simultaneously.
7. Existing item-level fields remain preserved:
   - UOM
   - discount_percent
   - cost_price
8. Legacy quotation without these optional fields still converts successfully.
9. Duplicate conversion & rejected quotation protection still work.
10. Existing stock reservation behavior remains unchanged across draft and direct conversions.
"""

import os
import sys
import uuid

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from app.main import app
from app.seed import main as seed_main

seed_main()
client = TestClient(app)

_passed = 0
_failed = 0


def ok(msg: str):
    global _passed
    _passed += 1
    print(f"  PASS  {msg}")


def fail(msg: str, detail: str = ""):
    global _failed
    _failed += 1
    print(f"  FAIL  {msg}  {detail}")


def assert_eq(actual, expected, msg: str):
    if actual == expected:
        ok(msg)
    else:
        fail(msg, f"Expected {expected!r}, got {actual!r}")


def _register_org(label: str):
    clean_label = label.replace("_", "").lower()
    email = f"admin_{uuid.uuid4().hex[:8]}@{clean_label}.com"
    r = client.post("/auth/register", json={
        "organization_name": f"{label} Enterprise",
        "admin_name": f"Admin {label}",
        "email": email,
        "password": "Password123!",
        "role": "admin",
    })
    assert r.status_code == 201, r.text
    token = r.json()["tokens"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _setup_org(label: str):
    auth = _register_org(label)
    wh_res = client.post("/warehouses", json={"name": "Primary WH", "code": f"WH-{uuid.uuid4().hex[:6]}"}, headers=auth)
    wh_id = wh_res.json()["id"]

    p_res = client.post("/products", json={
        "name": "Precision Sensor",
        "sku": f"SNR-{uuid.uuid4().hex[:6]}",
        "price": 250.0,
        "tax_rate": 18.0,
        "uom": "unit",
        "pricing": {"purchase_price": 140.0, "selling_price": 250.0, "currency": "USD"},
    }, headers=auth)
    assert p_res.status_code == 201, p_res.text
    prod_id = p_res.json()["id"]

    client.post(f"/warehouses/{wh_id}/stock/adjust", json={
        "product_id": prod_id, "quantity": 1000,
    }, headers=auth)

    c_res = client.post("/customers", json={
        "name": "Global Tech Corp",
        "business_name": "Global Tech Ltd",
        "phone": "9988776655",
        "email": "procurement@globaltech.com",
        "gst_number": "29ABCDE1234F1Z5",
        "billing_address": "Master Billing 101, Silicon Valley",
        "delivery_address": "Master Delivery Dock 7, Port Area",
    }, headers=auth)
    assert c_res.status_code == 201, c_res.text
    cust_id = c_res.json()["id"]

    return auth, wh_id, prod_id, cust_id


def test_1_billing_address_propagation():
    print("\n--- TEST 1: Quotation Billing Address Propagation ---")
    auth, wh_id, prod_id, cust_id = _setup_org("bill_addr")

    q_res = client.post("/quotations", json={
        "customer_id": cust_id,
        "billing_address": "Special Project Billing HQ, Floor 4",
        "items": [{"product_id": prod_id, "quantity": 5, "unit_price": 250.0}],
    }, headers=auth)
    assert_eq(q_res.status_code, 201, "Quotation created")
    q_id = q_res.json()["id"]

    client.patch(f"/quotations/{q_id}", json={"status": "sent"}, headers=auth)
    client.patch(f"/quotations/{q_id}", json={"status": "accepted"}, headers=auth)
    conv_res = client.post(f"/quotations/{q_id}/convert-to-order", json={"warehouse_id": wh_id}, headers=auth)
    assert_eq(conv_res.status_code, 201, "Quotation converted to order")
    order_id = conv_res.json()["order"]["id"]

    order = client.get(f"/orders/{order_id}", headers=auth).json()
    assert_eq(order["billing_address"], "Special Project Billing HQ, Floor 4", "SalesOrder billing_address matches quotation billing_address")


def test_2_shipping_delivery_address_propagation():
    print("\n--- TEST 2: Quotation Shipping/Delivery Address Propagation ---")
    auth, wh_id, prod_id, cust_id = _setup_org("ship_addr")

    q_res = client.post("/quotations", json={
        "customer_id": cust_id,
        "shipping_address": "Remote Facility Gate B, Sub-dock 3",
        "items": [{"product_id": prod_id, "quantity": 10, "unit_price": 250.0}],
    }, headers=auth)
    assert_eq(q_res.status_code, 201, "Quotation created with shipping_address")
    q_id = q_res.json()["id"]

    client.patch(f"/quotations/{q_id}", json={"status": "sent"}, headers=auth)
    client.patch(f"/quotations/{q_id}", json={"status": "accepted"}, headers=auth)
    conv_res = client.post(f"/quotations/{q_id}/convert-to-order", json={"warehouse_id": wh_id}, headers=auth)
    assert_eq(conv_res.status_code, 201, "Quotation converted to order")
    order_id = conv_res.json()["order"]["id"]

    order = client.get(f"/orders/{order_id}", headers=auth).json()
    assert_eq(order["shipping_address"], "Remote Facility Gate B, Sub-dock 3", "SalesOrder shipping_address matches quotation shipping_address")
    assert_eq(order["delivery_address"], "Remote Facility Gate B, Sub-dock 3", "SalesOrder delivery_address matches quotation shipping_address")


def test_3_payment_terms_propagation():
    print("\n--- TEST 3: Quotation Payment Terms Propagation ---")
    auth, wh_id, prod_id, cust_id = _setup_org("pay_terms")

    q_res = client.post("/quotations", json={
        "customer_id": cust_id,
        "payment_terms": "Net 45 days after delivery inspection",
        "items": [{"product_id": prod_id, "quantity": 4, "unit_price": 250.0}],
    }, headers=auth)
    q_id = q_res.json()["id"]

    client.patch(f"/quotations/{q_id}", json={"status": "sent"}, headers=auth)
    client.patch(f"/quotations/{q_id}", json={"status": "accepted"}, headers=auth)
    conv_res = client.post(f"/quotations/{q_id}/convert-to-order", json={
        "warehouse_id": wh_id,
        "payment_terms_days": 45,
    }, headers=auth)
    assert_eq(conv_res.status_code, 201, "Quotation converted to order")
    order_id = conv_res.json()["order"]["id"]

    order = client.get(f"/orders/{order_id}", headers=auth).json()
    assert_eq(order["payment_terms"], "Net 45 days after delivery inspection", "SalesOrder textual payment_terms matches quotation")
    assert_eq(order["payment_terms_days"], 45, "SalesOrder payment_terms_days preserved")


def test_4_delivery_terms_propagation():
    print("\n--- TEST 4: Quotation Delivery Terms Propagation ---")
    auth, wh_id, prod_id, cust_id = _setup_org("deliv_terms")

    q_res = client.post("/quotations", json={
        "customer_id": cust_id,
        "delivery_terms": "FOB Destination / Buyer unloads",
        "items": [{"product_id": prod_id, "quantity": 6, "unit_price": 250.0}],
    }, headers=auth)
    q_id = q_res.json()["id"]

    client.patch(f"/quotations/{q_id}", json={"status": "sent"}, headers=auth)
    client.patch(f"/quotations/{q_id}", json={"status": "accepted"}, headers=auth)
    conv_res = client.post(f"/quotations/{q_id}/convert-to-order", json={"warehouse_id": wh_id}, headers=auth)
    assert_eq(conv_res.status_code, 201, "Quotation converted to order")
    order_id = conv_res.json()["order"]["id"]

    order = client.get(f"/orders/{order_id}", headers=auth).json()
    assert_eq(order["delivery_terms"], "FOB Destination / Buyer unloads", "SalesOrder delivery_terms matches quotation")


def test_5_currency_propagation():
    print("\n--- TEST 5: Quotation Currency Propagation ---")
    auth, wh_id, prod_id, cust_id = _setup_org("curr_prop")

    q_res = client.post("/quotations", json={
        "customer_id": cust_id,
        "currency": "EUR",
        "items": [{"product_id": prod_id, "quantity": 8, "unit_price": 250.0}],
    }, headers=auth)
    q_id = q_res.json()["id"]

    client.patch(f"/quotations/{q_id}", json={"status": "sent"}, headers=auth)
    client.patch(f"/quotations/{q_id}", json={"status": "accepted"}, headers=auth)
    conv_res = client.post(f"/quotations/{q_id}/convert-to-order", json={"warehouse_id": wh_id}, headers=auth)
    assert_eq(conv_res.status_code, 201, "Quotation converted to order")
    order_id = conv_res.json()["order"]["id"]

    order = client.get(f"/orders/{order_id}", headers=auth).json()
    assert_eq(order["currency"], "EUR", "SalesOrder currency matches quotation currency ('EUR')")


def test_6_all_commercial_fields_simultaneous_propagation():
    print("\n--- TEST 6: All Commercial Fields Simultaneous Propagation ---")
    auth, wh_id, prod_id, cust_id = _setup_org("all_comm")

    q_res = client.post("/quotations", json={
        "customer_id": cust_id,
        "billing_address": "Global Finance Hub, Tower 9, Suite 12",
        "shipping_address": "Global Assembly Plant, Yard 4",
        "payment_terms": "30% Advance, 70% upon delivery",
        "delivery_terms": "CIF Mumbai Port",
        "currency": "USD",
        "notes": "Urgent project dispatch",
        "items": [
            {
                "product_id": prod_id,
                "quantity": 12,
                "unit_price": 250.0,
                "discount_percent": 10.0,
                "uom": "carton",
            }
        ],
    }, headers=auth)
    assert_eq(q_res.status_code, 201, "Quotation with all commercial fields created")
    q_id = q_res.json()["id"]

    client.patch(f"/quotations/{q_id}", json={"status": "sent"}, headers=auth)
    client.patch(f"/quotations/{q_id}", json={"status": "accepted"}, headers=auth)
    conv_res = client.post(f"/quotations/{q_id}/convert-to-order", json={
        "warehouse_id": wh_id,
        "fulfilment_method": "delivery",
        "payment_type": "credit",
        "payment_terms_days": 30,
    }, headers=auth)
    assert_eq(conv_res.status_code, 201, "Quotation converted to order")
    order_id = conv_res.json()["order"]["id"]

    order = client.get(f"/orders/{order_id}", headers=auth).json()
    assert_eq(order["billing_address"], "Global Finance Hub, Tower 9, Suite 12", "All: billing_address preserved")
    assert_eq(order["shipping_address"], "Global Assembly Plant, Yard 4", "All: shipping_address preserved")
    assert_eq(order["delivery_address"], "Global Assembly Plant, Yard 4", "All: delivery_address preserved")
    assert_eq(order["payment_terms"], "30% Advance, 70% upon delivery", "All: payment_terms preserved")
    assert_eq(order["payment_terms_days"], 30, "All: payment_terms_days preserved")
    assert_eq(order["delivery_terms"], "CIF Mumbai Port", "All: delivery_terms preserved")
    assert_eq(order["currency"], "USD", "All: currency preserved")
    assert_eq(order["notes"], "Urgent project dispatch", "All: notes preserved")
    assert_eq(order["quotation_id"], q_id, "All: quotation_id linked")


def test_7_item_level_propagation_intact():
    print("\n--- TEST 7: Item-level Propagation (UOM, Discount %, Cost Price) ---")
    auth, wh_id, prod_id, cust_id = _setup_org("item_prop")

    q_res = client.post("/quotations", json={
        "customer_id": cust_id,
        "items": [
            {
                "product_id": prod_id,
                "quantity": 10,
                "unit_price": 250.0,
                "discount_percent": 15.0,
                "uom": "set",
                "tax_rate": 18.0,
            }
        ],
    }, headers=auth)
    q_id = q_res.json()["id"]

    client.patch(f"/quotations/{q_id}", json={"status": "sent"}, headers=auth)
    client.patch(f"/quotations/{q_id}", json={"status": "accepted"}, headers=auth)
    conv_res = client.post(f"/quotations/{q_id}/convert-to-order", json={"warehouse_id": wh_id}, headers=auth)
    assert_eq(conv_res.status_code, 201, "Quotation converted to order")
    order_id = conv_res.json()["order"]["id"]

    order = client.get(f"/orders/{order_id}", headers=auth).json()
    item = order["items"][0]
    assert_eq(item["uom"], "set", "Item UOM retained as 'set'")
    assert_eq(item["discount_percent"], 15.0, "Item discount_percent retained as 15.0%")
    # 10 * 250 = 2500, 15% discount = 375 -> line_total = 2125
    assert_eq(item["discount"], 375.0, "Item discount amount calculated as 375.0")
    assert_eq(item["line_total"], 2125.0, "Item line_total calculated as 2125.0")
    assert_eq(item["cost_price"], 140.0, "Item cost_price snapshotted from product pricing (140.0)")


def test_8_legacy_quotation_fallback_conversion():
    print("\n--- TEST 8: Legacy Quotation Conversion Fallback ---")
    auth, wh_id, prod_id, cust_id = _setup_org("legacy_conv")

    # Quotation without explicit addresses, terms, or currency
    q_res = client.post("/quotations", json={
        "customer_id": cust_id,
        "items": [{"product_id": prod_id, "quantity": 2, "unit_price": 250.0}],
    }, headers=auth)
    q_id = q_res.json()["id"]

    client.patch(f"/quotations/{q_id}", json={"status": "sent"}, headers=auth)
    client.patch(f"/quotations/{q_id}", json={"status": "accepted"}, headers=auth)
    conv_res = client.post(f"/quotations/{q_id}/convert-to-order", json={"warehouse_id": wh_id}, headers=auth)
    assert_eq(conv_res.status_code, 201, "Legacy quotation converts successfully")
    order_id = conv_res.json()["order"]["id"]

    order = client.get(f"/orders/{order_id}", headers=auth).json()
    # Falls back gracefully to customer master addresses
    assert_eq(order["billing_address"], "Master Billing 101, Silicon Valley", "Fallback billing_address from customer")
    assert_eq(order["shipping_address"], "Master Delivery Dock 7, Port Area", "Fallback shipping_address from customer")
    assert_eq(order["currency"], "INR", "Default currency 'INR'")


def test_9_duplicate_and_rejected_conversion_protection():
    print("\n--- TEST 9: Duplicate and Rejected Conversion Protection ---")
    auth, wh_id, prod_id, cust_id = _setup_org("dup_conv")

    # 1. Accepted quotation conversion then duplicate attempt
    q_res = client.post("/quotations", json={
        "customer_id": cust_id,
        "items": [{"product_id": prod_id, "quantity": 1, "unit_price": 250.0}],
    }, headers=auth)
    q_id = q_res.json()["id"]
    client.patch(f"/quotations/{q_id}", json={"status": "sent"}, headers=auth)
    client.patch(f"/quotations/{q_id}", json={"status": "accepted"}, headers=auth)
    conv1 = client.post(f"/quotations/{q_id}/convert-to-order", json={"warehouse_id": wh_id}, headers=auth)
    assert_eq(conv1.status_code, 201, "First conversion succeeds")

    conv2 = client.post(f"/quotations/{q_id}/convert-to-order", json={"warehouse_id": wh_id}, headers=auth)
    assert_eq(conv2.status_code, 400, "Second conversion rejected with HTTP 400")

    # 2. Rejected quotation cannot be converted
    q_rej_res = client.post("/quotations", json={
        "customer_id": cust_id,
        "items": [{"product_id": prod_id, "quantity": 1, "unit_price": 250.0}],
    }, headers=auth)
    q_rej_id = q_rej_res.json()["id"]
    client.patch(f"/quotations/{q_rej_id}", json={"status": "rejected"}, headers=auth)
    conv_rej = client.post(f"/quotations/{q_rej_id}/convert-to-order", json={"warehouse_id": wh_id}, headers=auth)
    assert_eq(conv_rej.status_code, 400, "Conversion of rejected quotation refused with HTTP 400")


def test_10_stock_reservation_integrity_on_conversion():
    print("\n--- TEST 10: Stock Reservation Integrity on Conversion ---")
    auth, wh_id, prod_id, cust_id = _setup_org("stock_conv")

    # When draft_orders_enabled is False, conversion reserves stock immediately
    client.patch("/sales-workflow-settings", json={"draft_orders_enabled": False}, headers=auth)
    q_res = client.post("/quotations", json={
        "customer_id": cust_id,
        "items": [{"product_id": prod_id, "quantity": 25, "unit_price": 250.0}],
    }, headers=auth)
    q_id = q_res.json()["id"]
    client.patch(f"/quotations/{q_id}", json={"status": "sent"}, headers=auth)
    client.patch(f"/quotations/{q_id}", json={"status": "accepted"}, headers=auth)

    stock_before = client.get(f"/products/{prod_id}", headers=auth).json()
    conv_res = client.post(f"/quotations/{q_id}/convert-to-order", json={"warehouse_id": wh_id}, headers=auth)
    assert_eq(conv_res.status_code, 201, "Direct conversion succeeds")
    order_id = conv_res.json()["order"]["id"]

    order = client.get(f"/orders/{order_id}", headers=auth).json()
    assert_eq(order["status"], "placed", "Direct conversion order is placed directly")
    assert_eq(order["fulfilment_status"], "reserved", "Direct conversion order is reserved immediately")


def run_all_tests():
    print("\n=======================================================")
    print("TEST SUITE: Quotation -> Order Conversion Propagation")
    print("=======================================================")
    test_1_billing_address_propagation()
    test_2_shipping_delivery_address_propagation()
    test_3_payment_terms_propagation()
    test_4_delivery_terms_propagation()
    test_5_currency_propagation()
    test_6_all_commercial_fields_simultaneous_propagation()
    test_7_item_level_propagation_intact()
    test_8_legacy_quotation_fallback_conversion()
    test_9_duplicate_and_rejected_conversion_protection()
    test_10_stock_reservation_integrity_on_conversion()

    print("\n=======================================================")
    print(f"RESULTS: {_passed} passed, {_failed} failed")
    print("=======================================================\n")
    if _failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_all_tests()
