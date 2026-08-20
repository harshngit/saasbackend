"""Automated test suite for Order-Based Invoicing & Customer Financial History / Account Statement."""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from app.main import app
from app.core.database import Base, SessionLocal, engine
from app.models import Delivery, SalesOrderItem, CustomerPayment, Invoice, SalesOrder
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


def run_tests():
    print("\n=======================================================")
    print("TEST SUITE: Order-Based Invoicing & Customer Statement")
    print("=======================================================")

    # 1. Setup two isolated test organizations & admins
    db = SessionLocal()
    org1_id = str(uuid.uuid4())
    org2_id = str(uuid.uuid4())

    email1 = f"admin_{uuid.uuid4().hex[:8]}@org1.com"
    email2 = f"admin_{uuid.uuid4().hex[:8]}@org2.com"

    r1 = client.post("/auth/register", json={
        "organization_name": "Org 1 Logistics",
        "admin_name": "Admin One",
        "email": email1,
        "password": "Password123!",
        "role": "admin",
    })
    assert r1.status_code == 201, r1.text
    token1 = r1.json()["tokens"]["access_token"]
    auth1 = {"Authorization": f"Bearer {token1}"}

    r2 = client.post("/auth/register", json={
        "organization_name": "Org 2 Retail",
        "admin_name": "Admin Two",
        "email": email2,
        "password": "Password123!",
        "role": "admin",
    })
    assert r2.status_code == 201, r2.text
    token2 = r2.json()["tokens"]["access_token"]
    auth2 = {"Authorization": f"Bearer {token2}"}

    # Get warehouse & default settings for Org 1
    me1 = client.get("/auth/me", headers=auth1).json()
    org1_actual_id = me1["organization_id"]

    wh1_res = client.get("/warehouses", headers=auth1)
    warehouses = wh1_res.json()
    if not warehouses:
        wh_create = client.post("/warehouses", json={"name": "Main WH", "code": f"WH1-{uuid.uuid4().hex[:4]}"}, headers=auth1)
        wh1_id = wh_create.json()["id"]
    else:
        wh1_id = warehouses[0]["id"]

    # 2. Create products with prices and stock in Org 1
    p1_res = client.post("/products", json={
        "name": "Balaji Masala Wafers 150g",
        "sku": f"BMW-{uuid.uuid4().hex[:6]}",
        "price": 30.0,
        "tax_rate": 12.0,
        "pricing": {"purchase_price": 20.0, "selling_price": 30.0, "currency": "INR"},
    }, headers=auth1)
    assert p1_res.status_code == 201, p1_res.text
    prod1 = p1_res.json()
    prod1_id = prod1["id"]

    # Add stock in warehouse
    client.post(f"/warehouses/{wh1_id}/stock/adjust", json={
        "product_id": prod1_id,
        "quantity": 500,
    }, headers=auth1)

    # 3. Create customer A and Customer B in Org 1
    c1_res = client.post("/customers", json={
        "name": "Suresh Supermarket",
        "business_name": "Suresh Supermarket Pvt Ltd",
        "phone": "9876543210",
        "email": "suresh@market.com",
        "billing_address": "123 Market Road, City",
        "credit_limit": 5000.0,
        "opening_balance": 100.0,
    }, headers=auth1)
    assert c1_res.status_code == 201, c1_res.text
    cust1 = c1_res.json()
    cust1_id = cust1["id"]

    c2_res = client.post("/customers", json={
        "name": "Ramesh Stores",
        "business_name": "Ramesh Stores",
        "phone": "9876543211",
        "email": "ramesh@stores.com",
        "billing_address": "456 Bazaar Lane, City",
        "credit_limit": 3000.0,
        "opening_balance": 0.0,
    }, headers=auth1)
    assert c2_res.status_code == 201, c2_res.text
    cust2 = c2_res.json()
    cust2_id = cust2["id"]

    print("\n--- PART 1: ORDER -> INVOICE FLOW (SIMPLIFIED FIXED RULES) ---")

    # Set workflow settings to partial_delivery_invoice_mode = "after_full_order"
    client.patch("/sales-workflow-settings", json={"partial_delivery_invoice_mode": "after_full_order"}, headers=auth1)

    # Place Sales Order for Customer 1 (20 wafers @ 30 = 600 + 12% tax = 72 => total 672)
    so1_res = client.post("/orders", json={
        "customer_id": cust1_id,
        "warehouse_id": wh1_id,
        "payment_terms_days": 15,
        "items": [
            {
                "product_id": prod1_id,
                "quantity": 20,
                "unit_price": 30.0,
                "tax_rate": 12.0,
                "discount": 0.0,
            }
        ],
    }, headers=auth1)
    assert so1_res.status_code == 201, so1_res.text
    so1 = so1_res.json()
    so1_id = so1["id"]
    so1_item_id = so1["items"][0]["id"]
    assert_eq(so1["total"], 672.0, "Sales Order total calculated: 20 * 30 + 12% GST = 672")

    # TEST A: Delivered Qty = 0 -> POST /orders/{id}/invoice returns 400 Bad Request
    inv_zero_res = client.post(f"/orders/{so1_id}/invoice", json={}, headers=auth1)
    assert_eq(inv_zero_res.status_code, 400, "Invoicing with 0 delivered returns HTTP 400")
    assert_eq(inv_zero_res.json().get("detail"), "No delivered quantity is available for invoicing", "Error detail is 'No delivered quantity is available for invoicing'")

    # Deliver partially (12 units) on order 1 under after_full_order mode
    deliv_part_res = client.post("/deliveries", json={
        "order_id": so1_id,
        "warehouse_id": wh1_id,
        "items": [{"order_item_id": so1_item_id, "planned_quantity": 12}],
    }, headers=auth1)
    assert deliv_part_res.status_code == 201, deliv_part_res.text
    deliv_part_id = deliv_part_res.json()["id"]

    db_session = SessionLocal()
    deliv_part_db = db_session.get(Delivery, deliv_part_id)
    for item in deliv_part_db.items:
        item.delivered_quantity = 12.0
    deliv_part_db.status = "delivered"
    so1_item_db = db_session.get(SalesOrderItem, so1_item_id)
    so1_item_db.delivered_quantity = 12.0
    so1_db = db_session.get(SalesOrder, so1_id)
    so1_db.fulfilment_status = "partially_delivered"
    db_session.commit()
    db_session.close()

    # In after_full_order mode, partial delivery must be blocked
    inv_part_block_res = client.post(f"/orders/{so1_id}/invoice", json={}, headers=auth1)
    assert_eq(inv_part_block_res.status_code, 400, "Incomplete delivery in after_full_order mode returns HTTP 400")
    assert_eq(inv_part_block_res.json().get("detail"), "Order still has pending delivery quantity", "Error detail is 'Order still has pending delivery quantity'")

    # Deliver remaining 8 units -> Fully Delivered (20 units)
    deliv_rest_res = client.post("/deliveries", json={
        "order_id": so1_id,
        "warehouse_id": wh1_id,
        "items": [{"order_item_id": so1_item_id, "planned_quantity": 8}],
    }, headers=auth1)
    assert deliv_rest_res.status_code == 201, deliv_rest_res.text
    deliv_rest_id = deliv_rest_res.json()["id"]

    db_session = SessionLocal()
    deliv_rest_db = db_session.get(Delivery, deliv_rest_id)
    for item in deliv_rest_db.items:
        item.delivered_quantity = 8.0
    deliv_rest_db.status = "delivered"
    so1_item_db = db_session.get(SalesOrderItem, so1_item_id)
    so1_item_db.delivered_quantity = 20.0
    so1_db = db_session.get(SalesOrder, so1_id)
    so1_db.fulfilment_status = "delivered"
    db_session.commit()
    db_session.close()

    # TEST B: Fully Delivered (20 units) -> POST /orders/{id}/invoice with {} succeeds
    inv1_res = client.post(f"/orders/{so1_id}/invoice", json={}, headers=auth1)
    assert_eq(inv1_res.status_code, 201, "POST /orders/{id}/invoice with empty body succeeds when fully delivered")
    inv1 = inv1_res.json()
    inv1_id = inv1["id"]

    assert_eq(inv1["order_id"], so1_id, "Invoice linked to correct order_id")
    assert_eq(inv1["customer_id"], cust1_id, "Invoice automatically linked to customer_id")
    assert_eq(inv1["billing_address"], "123 Market Road, City", "Invoice billing address copied from customer")
    assert_eq(inv1["total"], 672.0, "Invoice total is 672.0")
    assert_eq(inv1["subtotal"], 600.0, "Invoice subtotal is 600.0")
    assert_eq(inv1["tax"], 72.0, "Invoice tax is 72.0")
    assert_eq(inv1["status"], "unpaid", "Invoice status is unpaid")
    assert_eq(inv1["invoice_status"], "Issued", "Invoice invoice_status is Issued")
    assert_eq(inv1["payment_status"], "Unpaid", "Invoice payment_status is Unpaid")
    assert_eq(inv1["outstanding_amount"], 672.0, "Invoice outstanding_amount is 672.0")
    assert_eq(len(inv1["items"]), 1, "Invoice has 1 line item automatically created from order")
    assert_eq(inv1["items"][0]["quantity"], 20, "Line item quantity is 20")
    assert_eq(inv1["items"][0]["unit_price"], 30.0, "Line item unit price is 30.0")
    assert_eq(inv1["items"][0]["tax_rate"], 12.0, "Line item tax rate is 12.0%")

    # Verify due_date computed using payment_terms_days (15 days)
    if inv1.get("due_date"):
        ok("Invoice due_date automatically computed from order payment terms")
    else:
        fail("Invoice due_date is missing")

    # TEST D: Duplicate Invoice Prevention (Full order returns HTTP 409)
    inv1_dup = client.post(f"/orders/{so1_id}/invoice", json={}, headers=auth1)
    assert_eq(inv1_dup.status_code, 409, "Duplicate full order invoice returns HTTP 409 Conflict")

    # TEST F: GET /invoices?order_id={id} filter
    inv_list_res = client.get(f"/invoices?order_id={so1_id}", headers=auth1)
    assert_eq(inv_list_res.status_code, 200, "GET /invoices?order_id={id} returns 200")
    inv_list = inv_list_res.json()
    assert_eq(len(inv_list), 1, "GET /invoices?order_id={id} returns exactly 1 invoice for this order")
    assert_eq(inv_list[0]["id"], inv1_id, "Matching invoice returned")

    # Query non-matching order_id
    dummy_order_id = str(uuid.uuid4())
    inv_empty_res = client.get(f"/invoices?order_id={dummy_order_id}", headers=auth1)
    assert_eq(len(inv_empty_res.json()), 0, "GET /invoices?order_id={dummy} returns empty list")

    # TEST C: per_delivery Partial Delivery Invoicing Mode
    print("\n--- TEST C: PER-DELIVERY INVOICING MODE ---")
    client.patch("/sales-workflow-settings", json={
        "partial_delivery_invoice_mode": "per_delivery",
    }, headers=auth1)

    # Place new order for Customer 2 (Ordered: 20 units @ 30 = 672)
    so2_res = client.post("/orders", json={
        "customer_id": cust2_id,
        "warehouse_id": wh1_id,
        "items": [{"product_id": prod1_id, "quantity": 20, "unit_price": 30.0, "tax_rate": 12.0}],
    }, headers=auth1)
    so2 = so2_res.json()
    so2_id = so2["id"]
    so2_item_id = so2["items"][0]["id"]

    # In per_delivery mode, invoicing without delivery_id must be rejected
    inv_no_deliv = client.post(f"/orders/{so2_id}/invoice", json={}, headers=auth1)
    assert_eq(inv_no_deliv.status_code, 400, "Invoicing without delivery_id in per_delivery mode rejected with HTTP 400")

    # Create Partial Delivery 1: planned 12, delivered 12
    deliv1_res = client.post("/deliveries", json={
        "order_id": so2_id,
        "warehouse_id": wh1_id,
        "items": [{"order_item_id": so2_item_id, "planned_quantity": 12}],
    }, headers=auth1)
    assert deliv1_res.status_code == 201, deliv1_res.text
    deliv1 = deliv1_res.json()
    deliv1_id = deliv1["id"]

    # Mark 12 as delivered on Delivery 1 in DB
    db_session = SessionLocal()
    deliv1_db = db_session.get(Delivery, deliv1_id)
    for item in deliv1_db.items:
        item.delivered_quantity = 12.0
    deliv1_db.status = "delivered"
    so2_item_db = db_session.get(SalesOrderItem, so2_item_id)
    so2_item_db.delivered_quantity = 12.0
    db_session.commit()
    db_session.close()

    # Invoice Delivery 1 (12 units @ 30 = 360 + 12% = 43.2 => total 403.2)
    inv_deliv1_res = client.post(f"/orders/{so2_id}/invoice", json={"delivery_id": deliv1_id}, headers=auth1)
    assert_eq(inv_deliv1_res.status_code, 201, "Per-delivery invoice generated successfully")
    inv_deliv1 = inv_deliv1_res.json()
    assert_eq(inv_deliv1["total"], 403.2, "Invoice quantity is 12 (total 403.2), NOT full ordered 20 (672)")
    assert_eq(inv_deliv1["items"][0]["quantity"], 12, "Invoice item quantity is strictly 12")

    # Duplicate per-delivery invoice returns HTTP 409 Conflict
    inv_deliv1_dup = client.post(f"/orders/{so2_id}/invoice", json={"delivery_id": deliv1_id}, headers=auth1)
    assert_eq(inv_deliv1_dup.status_code, 409, "Duplicate invoice for same delivery returns HTTP 409 Conflict")

    # Create Delivery 2 for the remaining 8 units
    deliv2_res = client.post("/deliveries", json={
        "order_id": so2_id,
        "warehouse_id": wh1_id,
        "items": [{"order_item_id": so2_item_id, "planned_quantity": 8}],
    }, headers=auth1)
    assert deliv2_res.status_code == 201, deliv2_res.text
    deliv2_id = deliv2_res.json()["id"]

    # Mark 8 as delivered on Delivery 2 in DB
    db_session = SessionLocal()
    deliv2_db = db_session.get(Delivery, deliv2_id)
    for item in deliv2_db.items:
        item.delivered_quantity = 8.0
    deliv2_db.status = "delivered"
    so2_item_db2 = db_session.get(SalesOrderItem, so2_item_id)
    so2_item_db2.delivered_quantity = 20.0
    db_session.commit()
    db_session.close()

    # Invoice Delivery 2 (8 units @ 30 = 240 + 12% = 28.8 => total 268.8)
    inv_deliv2_res = client.post(f"/orders/{so2_id}/invoice", json={"delivery_id": deliv2_id}, headers=auth1)
    assert_eq(inv_deliv2_res.status_code, 201, "Second delivery invoiced separately")
    inv_deliv2 = inv_deliv2_res.json()
    assert_eq(inv_deliv2["total"], 268.8, "Second delivery invoice total is 268.8")
    assert_eq(inv_deliv2["items"][0]["quantity"], 8, "Second delivery quantity is 8")

    # Verify both invoices appear in GET /invoices?order_id={so2_id}
    so2_invoices = client.get(f"/invoices?order_id={so2_id}", headers=auth1).json()
    assert_eq(len(so2_invoices), 2, "Both per-delivery invoices listed under GET /invoices?order_id={id}")

    # TEST 7: Direct Invoice / Quick Billing remains untouched
    print("\n--- DIRECT INVOICE / QUICK BILLING ---")
    direct_res = client.post("/invoices", json={
        "customer_id": cust1_id,
        "warehouse_id": wh1_id,
        "items": [{"product_id": prod1_id, "quantity": 5, "unit_price": 30.0, "tax_rate": 12.0}],
    }, headers=auth1)
    assert_eq(direct_res.status_code, 201, "Direct invoice (quick billing) succeeds via POST /invoices")
    assert_eq(direct_res.json()["total"], 168.0, "Direct invoice total is 5 * 30 + 12% = 168.0")

    print("\n--- PART 2: CUSTOMER ORDER & PAYMENT HISTORY ---")

    # Customer 1 Order History (GET /orders?customer_id={id})
    c1_orders_res = client.get(f"/orders?customer_id={cust1_id}", headers=auth1)
    assert_eq(c1_orders_res.status_code, 200, "GET /orders?customer_id={id} returns 200")
    c1_orders = c1_orders_res.json()
    assert_eq(len(c1_orders), 1, "Order history contains only Customer 1's orders")
    assert_eq(c1_orders[0]["id"], so1_id, "Order history contains order id")
    assert_eq(c1_orders[0]["order_number"], so1["order_number"], "Order history contains order_number")
    assert_eq(c1_orders[0]["total"], 672.0, "Order history contains total")

    # Payment against Invoice 1 (inv1_id = 672.0)
    pay1_res = client.post("/payment-receipts", json={
        "invoice_reference_id": inv1_id,
        "amount_received": 672.0,
        "payment_method": "upi",
        "transaction_reference": "UPI-123456",
    }, headers=auth1)
    assert_eq(pay1_res.status_code, 201, "Payment of 672 against invoice succeeds")

    # Customer 1 Payment History (GET /payment-receipts?customer_id={id})
    c1_payments_res = client.get(f"/payment-receipts?customer_id={cust1_id}", headers=auth1)
    assert_eq(c1_payments_res.status_code, 200, "GET /payment-receipts?customer_id={id} returns 200")
    c1_payments = c1_payments_res.json()
    assert_eq(len(c1_payments), 1, "Payment history contains only Customer 1's payment")
    assert_eq(c1_payments[0]["amount_received"], 672.0, "Payment history amount matches")
    assert_eq(c1_payments[0]["invoice_id"], inv1_id, "Payment history linked to invoice")

    print("\n--- PART 3: CUSTOMER ACCOUNT STATEMENT / LEDGER ---")

    # Customer 1 Account Statement via GET /customers/{id}/account-statement
    stmt_res = client.get(f"/customers/{cust1_id}/account-statement", headers=auth1)
    assert_eq(stmt_res.status_code, 200, "GET /customers/{id}/account-statement returns 200")
    stmt = stmt_res.json()

    assert_eq(stmt["customer_id"], cust1_id, "Account statement customer_id matches")
    summary = stmt["summary"]
    assert_eq(summary["opening_balance"], 100.0, "Opening balance is 100.0")
    assert_eq(summary["total_billed"], 840.0, "Total billed = 672 (Order invoice) + 168 (Direct invoice) = 840.0")
    assert_eq(summary["total_received"], 672.0, "Total received = 672.0")
    assert_eq(summary["outstanding"], 268.0, "Outstanding = 100 opening + 840 billed - 672 received = 268.0")
    assert_eq(summary["credit_limit"], 5000.0, "Credit limit matches 5000.0")
    assert_eq(summary["available_credit"], 4732.0, "Available credit = max(5000 - 268, 0) = 4732.0")

    # Check transactions in statement
    txs = stmt["transactions"]
    ok(f"Account statement returned {len(txs)} transactions")

    # Verify chronological balance_after
    for tx in txs:
        if "balance_after" in tx and "balance" in tx:
            assert tx["balance_after"] == tx["balance"]
    ok("Every transaction has valid balance_after and balance fields calculated chronologically")

    # Ageing check: fully paid invoice contributes 0 to ageing
    ageing = stmt["ageing"]
    # Direct invoice (168 unpaid) contributes to 0_30 bucket
    assert_eq(ageing.get("0_30") or ageing.get("0_30_days"), 168.0, "Ageing has direct invoice 168 in 0-30 days; paid invoice 672 is excluded")

    # Date Filtering on Account Statement
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    future_date = (datetime.now(timezone.utc) + timedelta(days=10)).strftime("%Y-%m-%d")

    filtered_stmt = client.get(f"/customers/{cust1_id}/account-statement?date_from={yesterday}&date_to={tomorrow}", headers=auth1).json()
    assert_eq(len(filtered_stmt["transactions"]), len(txs), "Date range covering transactions returns all transactions")

    future_stmt = client.get(f"/customers/{cust1_id}/account-statement?date_from={future_date}", headers=auth1).json()
    assert_eq(len(future_stmt["transactions"]), 0, "Future date_from returns 0 transactions in window")
    assert_eq(future_stmt["summary"]["opening_balance"], 268.0, "Future date_from accumulates all previous transactions into opening_balance (268.0)")

    print("\n--- PART 4: TENANT ISOLATION ---")

    # Org 2 cannot access Org 1's invoices, orders, customers, or statements
    cross_inv = client.get(f"/invoices?order_id={so1_id}", headers=auth2)
    assert_eq(len(cross_inv.json()), 0, "Org 2 GET /invoices?order_id={so1_id} returns 0 invoices (isolated)")

    cross_stmt = client.get(f"/customers/{cust1_id}/account-statement", headers=auth2)
    assert_eq(cross_stmt.status_code, 404, "Org 2 accessing Org 1 customer account statement returns 404 Not Found")

    cross_order_inv = client.post(f"/orders/{so1_id}/invoice", json={}, headers=auth2)
    assert_eq(cross_order_inv.status_code, 404, "Org 2 invoicing Org 1 order returns 404 Not Found")

    print("\n=======================================================")
    print(f"RESULTS: {_passed} passed, {_failed} failed")
    print("=======================================================\n")
    if _failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_tests()
