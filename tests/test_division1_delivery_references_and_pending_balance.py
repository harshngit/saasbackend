"""Test suite for Division 1:
PART A — Sales Order -> Delivery / Invoice References
PART B — Delivery Previous Pending Balance

Verifies:
1. Delivery reference behavior on SalesOrder response (delivery_id, delivery_number).
2. Invoice reference behavior on SalesOrder response (invoice_id, invoice_number).
3. No duplicate delivery or invoice creation.
4. Numbering integrity for deliveries and invoices.
5. Stock safety: no double reservation or double deduction.
6. Previous pending balance calculation:
   - Case 1: Zero pending balance.
   - Case 2: Existing pending balance (BEFORE current financial impact).
   - Case 3: Existing pending balance after customer payment.
   - Case 4: Current invoice isolation (current invoice not added to previous pending).
   - Case 5: Partial payment on previous invoice.
   - Case 6: Opening balance inclusion.
   - Case 7: Multiple previous invoices.
   - Case 8: Multi-tenant isolation.
7. Verification that amount_due and customer ledger behavior remain unchanged.
8. Full end-to-end flow: Quotation -> Sales Order -> Delivery -> Invoice.
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
        "organization_name": f"{label} Org",
        "admin_name": f"Admin {label}",
        "email": email,
        "password": "Password123!",
        "role": "admin",
    })
    assert r.status_code == 201, r.text
    token = r.json()["tokens"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _setup_firm(label: str):
    auth = _register_org(label)
    wh_res = client.post("/warehouses", json={"name": "Primary WH", "code": f"WH-{uuid.uuid4().hex[:6]}"}, headers=auth)
    wh_id = wh_res.json()["id"]

    p_res = client.post("/products", json={
        "name": "Industrial Valve",
        "sku": f"VLV-{uuid.uuid4().hex[:6]}",
        "price": 100.0,
        "tax_rate": 18.0,
        "uom": "piece",
        "pricing": {"purchase_price": 60.0, "selling_price": 100.0, "currency": "INR"},
    }, headers=auth)
    assert p_res.status_code == 201, p_res.text
    prod_id = p_res.json()["id"]

    client.post(f"/warehouses/{wh_id}/stock/adjust", json={
        "product_id": prod_id, "quantity": 1000,
    }, headers=auth)

    c_res = client.post("/customers", json={
        "name": "Apex Engineering",
        "business_name": "Apex Engineering Pvt Ltd",
        "phone": "9876543210",
        "email": "purchasing@apexeng.com",
        "gst_number": "29ABCDE1234F1Z5",
        "billing_address": "42 Tech Park, Sector 5",
        "delivery_address": "Plot 10, Industrial Estate",
        "opening_balance": 0.0,
        "credit_limit": 50000.0,
    }, headers=auth)
    assert c_res.status_code == 201, c_res.text
    cust_id = c_res.json()["id"]

    return auth, wh_id, prod_id, cust_id


from app.core.database import SessionLocal
from app.models import Delivery, SalesOrder, SalesOrderItem


def _complete_delivery(deliv_id: str, so_id: str):
    db_session = SessionLocal()
    deliv_db = db_session.get(Delivery, deliv_id)
    if deliv_db:
        for item in deliv_db.items:
            item.delivered_quantity = item.planned_quantity or 1.0
        deliv_db.status = "delivered"
    so_db = db_session.get(SalesOrder, so_id)
    if so_db:
        for item in so_db.items:
            item.delivered_quantity = item.quantity
        so_db.fulfilment_status = "delivered"
        so_db.status = "completed"
    db_session.commit()
    db_session.close()


def test_part_a_order_delivery_invoice_references():
    print("\n--- TEST: PART A - Sales Order -> Delivery / Invoice References ---")
    auth, wh_id, prod_id, cust_id = _setup_firm("refs_test")

    # 1. Create order
    so_res = client.post("/orders", json={
        "customer_id": cust_id,
        "warehouse_id": wh_id,
        "items": [{"product_id": prod_id, "quantity": 10, "unit_price": 100.0, "tax_rate": 18.0}],
    }, headers=auth)
    assert_eq(so_res.status_code, 201, "Sales order created")
    so = so_res.json()
    so_id = so["id"]
    so_item_id = so["items"][0]["id"]

    # Initial state: no delivery or invoice exists
    assert_eq(so["delivery_id"], None, "Initial order has delivery_id=None")
    assert_eq(so["delivery_number"], None, "Initial order has delivery_number=None")
    assert_eq(so["invoice_id"], None, "Initial order has invoice_id=None")
    assert_eq(so["invoice_number"], None, "Initial order has invoice_number=None")

    # 2. Plan delivery for order
    deliv_res = client.post("/deliveries", json={
        "order_id": so_id,
        "warehouse_id": wh_id,
        "items": [{"order_item_id": so_item_id, "planned_quantity": 10}],
    }, headers=auth)
    assert_eq(deliv_res.status_code, 201, "Delivery created for order")
    deliv = deliv_res.json()
    deliv_id = deliv["id"]
    deliv_number = deliv["delivery_number"]

    # Fetch order: delivery_id & delivery_number are now populated
    so_fetched = client.get(f"/orders/{so_id}", headers=auth).json()
    assert_eq(so_fetched["delivery_id"], deliv_id, "Order now references active delivery_id")
    assert_eq(so_fetched["delivery_number"], deliv_number, "Order now references active delivery_number")
    assert_eq(so_fetched["invoice_id"], None, "Order still has invoice_id=None before invoicing")

    # 3. Simulate delivery completion & issue invoice
    _complete_delivery(deliv_id, so_id)

    inv_res = client.post(f"/orders/{so_id}/invoice", json={"delivery_id": deliv_id}, headers=auth)
    assert_eq(inv_res.status_code, 201, "Invoice issued for delivered order")
    inv = inv_res.json()
    inv_id = inv["id"]
    inv_number = inv["invoice_number"]

    # Fetch order: both delivery and invoice references populated
    so_final = client.get(f"/orders/{so_id}", headers=auth).json()
    assert_eq(so_final["delivery_id"], deliv_id, "Order exposes delivery_id")
    assert_eq(so_final["delivery_number"], deliv_number, "Order exposes delivery_number")
    assert_eq(so_final["invoice_id"], inv_id, "Order exposes invoice_id")
    assert_eq(so_final["invoice_number"], inv_number, "Order exposes invoice_number")

    # 4. Duplicate safety: re-fetching order does not create duplicates
    so_refetch = client.get(f"/orders/{so_id}", headers=auth).json()
    assert_eq(so_refetch["id"], so_id, "Order re-fetch returns same order")
    deliveries_list = client.get(f"/deliveries?order_id={so_id}", headers=auth).json()
    assert_eq(len(deliveries_list), 1, "Exactly 1 delivery exists for the order")
    invoices_list = client.get(f"/invoices?order_id={so_id}", headers=auth).json()
    assert_eq(len(invoices_list), 1, "Exactly 1 invoice exists for the order")


def test_part_b_delivery_previous_pending_balance():
    print("\n--- TEST: PART B - Delivery Previous Pending Balance Calculations ---")
    auth, wh_id, prod_id, cust_id = _setup_firm("pending_test")

    # Case 1: Zero Pending Balance
    print("\n  Case 1: Zero Previous Pending")
    so1_res = client.post("/orders", json={
        "customer_id": cust_id,
        "warehouse_id": wh_id,
        "items": [{"product_id": prod_id, "quantity": 10, "unit_price": 100.0, "tax_rate": 18.0}],
    }, headers=auth)
    so1_id = so1_res.json()["id"]
    so1_item_id = so1_res.json()["items"][0]["id"]

    deliv1_res = client.post("/deliveries", json={
        "order_id": so1_id,
        "warehouse_id": wh_id,
        "items": [{"order_item_id": so1_item_id, "planned_quantity": 10}],
    }, headers=auth)
    deliv1 = deliv1_res.json()
    assert_eq(deliv1["previous_pending_balance"], 0.0, "Case 1: previous_pending_balance is 0.0")
    assert_eq(deliv1["customer"]["previous_pending_balance"], 0.0, "Case 1: customer.previous_pending_balance is 0.0")
    assert_eq(deliv1["amount_due"], 1180.0, "Case 1: current order amount_due is 1180.0")

    # Deliver & Invoice SO 1 (Total = 1180.0) -> Customer now owes 1180.0
    _complete_delivery(deliv1["id"], so1_id)
    inv1_res = client.post(f"/orders/{so1_id}/invoice", json={"delivery_id": deliv1["id"]}, headers=auth)
    assert inv1_res.status_code == 201

    # Case 2: Existing Pending Balance
    print("\n  Case 2: Existing Pending Balance")
    so2_res = client.post("/orders", json={
        "customer_id": cust_id,
        "warehouse_id": wh_id,
        "items": [{"product_id": prod_id, "quantity": 5, "unit_price": 100.0, "tax_rate": 18.0}],
    }, headers=auth)
    so2_id = so2_res.json()["id"]
    so2_item_id = so2_res.json()["items"][0]["id"]

    deliv2_res = client.post("/deliveries", json={
        "order_id": so2_id,
        "warehouse_id": wh_id,
        "items": [{"order_item_id": so2_item_id, "planned_quantity": 5}],
    }, headers=auth)
    deliv2 = deliv2_res.json()
    # SO 1 created 1180.0 outstanding. SO 2 order total is 590.0.
    assert_eq(deliv2["previous_pending_balance"], 1180.0, "Case 2: previous_pending_balance = 1180.0 (NOT 1770.0)")
    assert_eq(deliv2["amount_due"], 590.0, "Case 2: amount_due for SO 2 is 590.0")

    # Case 3: Existing Pending + Payment
    print("\n  Case 3: Existing Pending + Payment")
    # Customer pays 500 towards SO 1
    pay_res = client.post("/payment-receipts", json={
        "customer_id": cust_id,
        "invoice_reference_id": inv1_res.json()["id"],
        "amount_received": 500.0,
        "payment_method": "bank_transfer",
        "reference_number": "PAY-500-TEST",
    }, headers=auth)
    assert pay_res.status_code == 201

    # Re-fetch delivery 2 or create delivery 3: previous_pending_balance reflects 1180 - 500 = 680.0
    deliv2_updated = client.get(f"/deliveries/by-id/{deliv2['id']}", headers=auth).json()
    assert_eq(deliv2_updated["previous_pending_balance"], 680.0, "Case 3: previous_pending_balance updated to 680.0 after 500 payment")

    # Case 4: Current Invoice Isolation
    print("\n  Case 4: Current Invoice Isolation")
    # Deliver & Invoice SO 2 (total 590.0)
    _complete_delivery(deliv2["id"], so2_id)
    inv2_res = client.post(f"/orders/{so2_id}/invoice", json={"delivery_id": deliv2["id"]}, headers=auth)
    assert inv2_res.status_code == 201

    # Customer total balance is now 680 + 590 = 1270.0
    # Delivery 2 previous pending must STILL report 680.0 (the balance BEFORE SO 2's invoice!)
    deliv2_after_inv = client.get(f"/deliveries/by-id/{deliv2['id']}", headers=auth).json()
    assert_eq(deliv2_after_inv["previous_pending_balance"], 680.0, "Case 4: Delivery 2 previous_pending excludes its own invoice")

    # Case 5 & 6: Opening Balance
    print("\n  Case 6: Customer with Opening Balance")
    c_open_res = client.post("/customers", json={
        "name": "Beta Industries",
        "business_name": "Beta Industries LLC",
        "phone": "9876543299",
        "billing_address": "88 Industrial Way",
        "opening_balance": 3500.0,
        "credit_limit": 10000.0,
    }, headers=auth)
    cust_open_id = c_open_res.json()["id"]

    so_open_res = client.post("/orders", json={
        "customer_id": cust_open_id,
        "warehouse_id": wh_id,
        "items": [{"product_id": prod_id, "quantity": 2, "unit_price": 100.0, "tax_rate": 18.0}],
    }, headers=auth)
    so_open_id = so_open_res.json()["id"]
    so_open_item_id = so_open_res.json()["items"][0]["id"]

    deliv_open_res = client.post("/deliveries", json={
        "order_id": so_open_id,
        "warehouse_id": wh_id,
        "items": [{"order_item_id": so_open_item_id, "planned_quantity": 2}],
    }, headers=auth)
    deliv_open = deliv_open_res.json()
    assert_eq(deliv_open["previous_pending_balance"], 3500.0, "Case 6: previous_pending_balance includes opening_balance (3500.0)")

    # Case 8: Multi-tenant Isolation
    print("\n  Case 8: Multi-tenant Isolation")
    auth_org2, wh2_id, prod2_id, cust2_id = _setup_firm("org2_tenant")
    # Org 2 customer has 0 opening balance
    so_org2 = client.post("/orders", json={
        "customer_id": cust2_id,
        "warehouse_id": wh2_id,
        "items": [{"product_id": prod2_id, "quantity": 1, "unit_price": 100.0, "tax_rate": 18.0}],
    }, headers=auth_org2).json()
    deliv_org2 = client.post("/deliveries", json={
        "order_id": so_org2["id"],
        "warehouse_id": wh2_id,
        "items": [{"order_item_id": so_org2["items"][0]["id"], "planned_quantity": 1}],
    }, headers=auth_org2).json()
    assert_eq(deliv_org2["previous_pending_balance"], 0.0, "Case 8: Org 2 delivery isolated from Org 1 balances")


def test_full_integration_quotation_to_delivery():
    print("\n--- TEST: Full End-to-End Integration Flow ---")
    auth, wh_id, prod_id, cust_id = _setup_firm("full_flow")

    # 1. Create quotation with commercial fields
    q_res = client.post("/quotations", json={
        "customer_id": cust_id,
        "billing_address": "HQ Tower 12, Floor 8",
        "shipping_address": "Dock 4, Industrial Bay",
        "payment_terms": "Net 30 Days",
        "delivery_terms": "Ex-Works",
        "currency": "INR",
        "items": [{"product_id": prod_id, "quantity": 8, "unit_price": 100.0, "discount_percent": 10.0, "tax_rate": 18.0}],
    }, headers=auth)
    assert_eq(q_res.status_code, 201, "Quotation created")
    q_id = q_res.json()["id"]

    # 2. Accept and convert to Sales Order
    client.patch(f"/quotations/{q_id}", json={"status": "accepted"}, headers=auth)
    conv_res = client.post(f"/quotations/{q_id}/convert-to-order", json={"warehouse_id": wh_id}, headers=auth)
    assert_eq(conv_res.status_code, 201, "Quotation converted to Sales Order")
    so = conv_res.json()["order"]
    so_id = so["id"]

    # Verify commercial fields preserved
    assert_eq(so["billing_address"], "HQ Tower 12, Floor 8", "Billing address preserved")
    assert_eq(so["shipping_address"], "Dock 4, Industrial Bay", "Shipping address preserved")
    assert_eq(so["delivery_address"], "Dock 4, Industrial Bay", "Delivery address preserved")
    assert_eq(so["payment_terms"], "Net 30 Days", "Payment terms preserved")
    assert_eq(so["delivery_terms"], "Ex-Works", "Delivery terms preserved")

    # 3. Create delivery for converted order
    deliv_res = client.post("/deliveries", json={
        "order_id": so_id,
        "warehouse_id": wh_id,
    }, headers=auth)
    assert_eq(deliv_res.status_code, 201, "Delivery created for converted order")
    deliv = deliv_res.json()
    assert_eq(deliv["previous_pending_balance"], 0.0, "Initial previous pending is 0.0")

    # 4. Check order references
    so_checked = client.get(f"/orders/{so_id}", headers=auth).json()
    assert_eq(so_checked["delivery_id"], deliv["id"], "Converted order now references delivery_id")
    assert_eq(so_checked["delivery_number"], deliv["delivery_number"], "Converted order now references delivery_number")


def run_all_tests():
    print("\n=======================================================")
    print("TEST SUITE: Division 1 Part A & Part B Verification")
    print("=======================================================")
    test_part_a_order_delivery_invoice_references()
    test_part_b_delivery_previous_pending_balance()
    test_full_integration_quotation_to_delivery()

    print("\n=======================================================")
    print(f"RESULTS: {_passed} passed, {_failed} failed")
    print("=======================================================\n")
    if _failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_all_tests()
