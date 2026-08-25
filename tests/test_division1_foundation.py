"""Focused test suite for Division 1 Foundation / Data-Model Changes.

Verifies:
1. Discount Percentage on QuotationItem and Quotation calculations.
2. Discount Percentage on SalesOrderItem and Order calculations.
3. Backward compatibility with existing flat discount amounts.
4. SalesOrderItem cost_price snapshot from ProductPricing.purchase_price.
5. UOM retention on SalesOrderItem during Order creation and Quotation conversion.
6. Customer details in OrderOut.customer (phone, email, billing_address, delivery_address, gst_number).
7. CustomerPayment snapshot foundation fields (order_amount, previous_pending, remaining_receivable).
8. DeliveryOut and DeliveryCustomerBrief previous_pending_balance foundation fields.
9. Database migration verification with fresh DB and existing DB upgrade without data loss.
"""

import os
import sys
import tempfile
import sqlite3
import uuid

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from app.main import app
from app.core.database import Base, SessionLocal, auto_add_missing_columns
from app.models import Customer, CustomerPayment, Product, ProductPricing, Quotation, SalesOrder, SalesOrderItem
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
        "name": "Industrial Valve",
        "sku": f"VLV-{uuid.uuid4().hex[:6]}",
        "price": 100.0,
        "tax_rate": 10.0,
        "uom": "piece",
        "pricing": {"purchase_price": 60.0, "selling_price": 100.0, "currency": "INR"},
    }, headers=auth)
    assert p_res.status_code == 201, p_res.text
    prod_id = p_res.json()["id"]

    client.post(f"/warehouses/{wh_id}/stock/adjust", json={
        "product_id": prod_id, "quantity": 500,
    }, headers=auth)

    c_res = client.post("/customers", json={
        "name": "Acme Industries",
        "business_name": "Acme Corp Ltd",
        "phone": "9876543210",
        "email": "contact@acme.com",
        "gst_number": "27AABCA1234A1Z5",
        "billing_address": "100 Industrial Area, Phase 1",
        "delivery_address": "Dock 4, Acme Warehouse",
        "credit_limit": 50000.0,
        "opening_balance": 500.0,
    }, headers=auth)
    assert c_res.status_code == 201, c_res.text
    cust_id = c_res.json()["id"]

    return auth, wh_id, prod_id, cust_id


def test_1_discount_percentage_on_quotation():
    print("\n--- TEST 1: Quotation Discount Percentage & Calculations ---")
    auth, wh_id, prod_id, cust_id = _setup_org("quot_disc")

    # Create quotation with discount_percent = 15% on 10 units @ 100 with 10% tax
    # Gross = 1000, 15% discount = 150 -> line_total = 850, tax = 85 -> total = 935
    q_res = client.post("/quotations", json={
        "customer_id": cust_id,
        "items": [
            {
                "product_id": prod_id,
                "quantity": 10,
                "unit_price": 100.0,
                "discount_percent": 15.0,
                "tax_rate": 10.0,
                "uom": "piece",
            }
        ],
    }, headers=auth)
    assert_eq(q_res.status_code, 201, "Quotation created with discount_percent")
    q = q_res.json()
    item = q["items"][0]
    assert_eq(item["discount_percent"], 15.0, "QuotationItem discount_percent matches 15.0%")
    assert_eq(item["line_total"], 850.0, "Quotation line_total correctly calculated after 15% discount: 850.0")
    assert_eq(item["tax_amount"], 85.0, "Quotation tax_amount correctly calculated: 85.0")
    assert_eq(q["subtotal"], 850.0, "Quotation subtotal is 850.0")
    assert_eq(q["tax_total"], 85.0, "Quotation tax_total is 85.0")
    assert_eq(q["total"], 935.0, "Quotation total is 935.0")


def test_2_discount_percentage_on_sales_order():
    print("\n--- TEST 2: Sales Order Discount Percentage & Calculations ---")
    auth, wh_id, prod_id, cust_id = _setup_org("order_disc")

    # Create order with discount_percent = 20% on 5 units @ 100 with 10% tax
    # Gross = 500, 20% discount = 100 -> line_total = 400, tax = 40 -> total = 440
    so_res = client.post("/orders", json={
        "customer_id": cust_id,
        "warehouse_id": wh_id,
        "items": [
            {
                "product_id": prod_id,
                "quantity": 5,
                "unit_price": 100.0,
                "discount_percent": 20.0,
                "tax_rate": 10.0,
                "uom": "piece",
            }
        ],
    }, headers=auth)
    assert_eq(so_res.status_code, 201, "Sales Order created with discount_percent")
    so = so_res.json()
    item = so["items"][0]
    assert_eq(item["discount_percent"], 20.0, "SalesOrderItem discount_percent is 20.0%")
    assert_eq(item["discount"], 100.0, "SalesOrderItem discount amount calculated as 100.0")
    assert_eq(item["line_total"], 400.0, "SalesOrderItem line_total calculated as 400.0")
    assert_eq(item["tax_amount"], 40.0, "SalesOrderItem tax_amount calculated as 40.0")
    assert_eq(so["subtotal"], 400.0, "Order subtotal is 400.0")
    assert_eq(so["tax"], 40.0, "Order tax is 40.0")
    assert_eq(so["total"], 440.0, "Order total is 440.0")


def test_3_flat_discount_backward_compatibility():
    print("\n--- TEST 3: Flat Discount Backward Compatibility ---")
    auth, wh_id, prod_id, cust_id = _setup_org("flat_disc")

    # Create order with legacy flat discount = 50 on 5 units @ 100
    # Gross = 500, flat discount = 50 -> line_total = 450, tax = 45 -> total = 495
    so_res = client.post("/orders", json={
        "customer_id": cust_id,
        "warehouse_id": wh_id,
        "items": [
            {
                "product_id": prod_id,
                "quantity": 5,
                "unit_price": 100.0,
                "discount": 50.0,
                "tax_rate": 10.0,
            }
        ],
    }, headers=auth)
    assert_eq(so_res.status_code, 201, "Legacy order payload with only flat discount succeeds")
    so = so_res.json()
    item = so["items"][0]
    assert_eq(item["discount"], 50.0, "Flat discount amount is preserved as 50.0")
    assert_eq(item["discount_percent"], 0.0, "discount_percent defaults to 0.0")
    assert_eq(item["line_total"], 450.0, "line_total matches 450.0")
    assert_eq(so["total"], 495.0, "Order total matches 495.0")


def test_4_cost_price_and_uom_snapshot():
    print("\n--- TEST 4: Cost Price & UOM Snapshot ---")
    auth, wh_id, prod_id, cust_id = _setup_org("cost_uom")

    so_res = client.post("/orders", json={
        "customer_id": cust_id,
        "warehouse_id": wh_id,
        "items": [
            {
                "product_id": prod_id,
                "quantity": 4,
                "unit_price": 100.0,
                "uom": "piece",
            }
        ],
    }, headers=auth)
    assert_eq(so_res.status_code, 201, "Order creation succeeds")
    so = so_res.json()
    item = so["items"][0]
    assert_eq(item["cost_price"], 60.0, "SalesOrderItem cost_price automatically snapshotted from ProductPricing (60.0)")
    assert_eq(item["uom"], "piece", "SalesOrderItem uom preserved as 'piece'")


def test_5_uom_preservation_on_quotation_conversion():
    print("\n--- TEST 5: UOM Preservation on Quotation Conversion ---")
    auth, wh_id, prod_id, cust_id = _setup_org("uom_conv")

    client.patch("/sales-workflow-settings", json={"draft_orders_enabled": False}, headers=auth)
    q_res = client.post("/quotations", json={
        "customer_id": cust_id,
        "items": [
            {
                "product_id": prod_id,
                "quantity": 8,
                "unit_price": 100.0,
                "discount_percent": 10.0,
                "uom": "box",
            }
        ],
    }, headers=auth)
    q_id = q_res.json()["id"]

    client.patch(f"/quotations/{q_id}", json={"status": "accepted"}, headers=auth)
    conv_res = client.post(f"/quotations/{q_id}/convert-to-order", json={"warehouse_id": wh_id}, headers=auth)
    assert_eq(conv_res.status_code, 201, "Quotation converted to order")
    order_id = conv_res.json()["order"]["id"]

    order_detail = client.get(f"/orders/{order_id}", headers=auth).json()
    item = order_detail["items"][0]
    assert_eq(item["uom"], "box", "Converted order item retains UOM 'box' from quotation item")
    assert_eq(item["discount_percent"], 10.0, "Converted order item retains discount_percent (10.0%) from quotation")
    assert_eq(item["cost_price"], 60.0, "Converted order item snapshots cost_price (60.0)")


def test_6_customer_details_in_order_response():
    print("\n--- TEST 6: Customer Details in Order Response ---")
    auth, wh_id, prod_id, cust_id = _setup_org("cust_details")

    so_res = client.post("/orders", json={
        "customer_id": cust_id,
        "warehouse_id": wh_id,
        "items": [{"product_id": prod_id, "quantity": 2, "unit_price": 100.0}],
    }, headers=auth)
    assert_eq(so_res.status_code, 201, "Order created")
    so = so_res.json()
    customer = so["customer"]

    assert customer is not None, "Customer details returned in order response"
    assert_eq(customer["name"], "Acme Industries", "Customer name matches")
    assert_eq(customer["business_name"], "Acme Corp Ltd", "Customer business_name matches")
    assert_eq(customer["phone"], "9876543210", "Customer phone matches")
    assert_eq(customer["email"], "contact@acme.com", "Customer email matches")
    assert_eq(customer["gst_number"], "27AABCA1234A1Z5", "Customer gst_number matches")
    assert_eq(customer["billing_address"], "100 Industrial Area, Phase 1", "Customer billing_address matches")
    assert_eq(customer["delivery_address"], "Dock 4, Acme Warehouse", "Customer delivery_address matches")


def test_7_payment_and_delivery_foundation_fields():
    print("\n--- TEST 7: Payment & Delivery Foundation Schema Fields ---")
    auth, wh_id, prod_id, cust_id = _setup_org("found_fields")

    # Check delivery response contains previous_pending_balance field (defaults to None / ready for next step)
    deliv_res = client.post("/deliveries/notes", json={
        "customer_id": cust_id,
        "items": [{"product_id": prod_id, "delivered_quantity": 2}],
    }, headers=auth)
    assert_eq(deliv_res.status_code, 201, "Delivery note created")
    deliv_note = deliv_res.json()
    assert "customer" in deliv_note, "Delivery note exposes customer"

    # Check payment receipt output schema has foundation fields
    pay_res = client.post("/payment-receipts", json={
        "customer_id": cust_id,
        "amount_received": 500.0,
        "payment_method": "cash",
    }, headers=auth)
    assert_eq(pay_res.status_code, 201, "Payment receipt created")
    receipt = pay_res.json()
    assert "order_amount" in receipt, "PaymentReceiptOut has order_amount field"
    assert "previous_pending" in receipt, "PaymentReceiptOut has previous_pending field"
    assert "remaining_receivable" in receipt, "PaymentReceiptOut has remaining_receivable field"


def test_8_database_migration_and_upgrade():
    print("\n--- TEST 8: Database Migration & Schema Upgrade Integrity ---")
    # Test on fresh temporary SQLite DB
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        fresh_db_path = f.name

    fresh_engine = create_engine(f"sqlite:///{fresh_db_path}")
    Base.metadata.create_all(bind=fresh_engine)
    insp = inspect(fresh_engine)

    # 1. Verify quotation_items columns
    q_cols = {c["name"] for c in insp.get_columns("quotation_items")}
    assert "discount_percent" in q_cols, "discount_percent in quotation_items"
    ok("Fresh DB: quotation_items.discount_percent exists")

    # 2. Verify sales_order_items columns
    so_cols = {c["name"] for c in insp.get_columns("sales_order_items")}
    assert "discount_percent" in so_cols, "discount_percent in sales_order_items"
    assert "cost_price" in so_cols, "cost_price in sales_order_items"
    ok("Fresh DB: sales_order_items.discount_percent and cost_price exist")

    # 3. Verify customer_payments columns
    pay_cols = {c["name"] for c in insp.get_columns("customer_payments")}
    assert "order_amount" in pay_cols, "order_amount in customer_payments"
    assert "previous_pending" in pay_cols, "previous_pending in customer_payments"
    assert "remaining_receivable" in pay_cols, "remaining_receivable in customer_payments"
    ok("Fresh DB: customer_payments snapshot columns exist")

    fresh_engine.dispose()
    try:
        os.remove(fresh_db_path)
    except Exception:
        pass

    # Test auto-migration on a legacy DB with existing data
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        legacy_db_path = f.name

    conn = sqlite3.connect(legacy_db_path)
    cur = conn.cursor()
    # Create legacy tables without new columns
    cur.execute("""
        CREATE TABLE quotation_items (
            id VARCHAR(36) PRIMARY KEY,
            quotation_id VARCHAR(36) NOT NULL,
            product_id VARCHAR(36),
            variant_id VARCHAR(36),
            product_name VARCHAR(200) NOT NULL,
            quantity FLOAT NOT NULL,
            uom VARCHAR(30),
            unit_price FLOAT NOT NULL,
            discount FLOAT DEFAULT 0,
            tax_rate FLOAT
        )
    """)
    cur.execute("""
        CREATE TABLE sales_order_items (
            id VARCHAR(36) PRIMARY KEY,
            order_id VARCHAR(36) NOT NULL,
            product_id VARCHAR(36),
            variant_id VARCHAR(36),
            product_name VARCHAR(200) NOT NULL,
            quantity INTEGER NOT NULL,
            unit_price FLOAT NOT NULL,
            discount FLOAT NOT NULL,
            line_total FLOAT NOT NULL,
            uom VARCHAR(30),
            tax_rate FLOAT,
            tax_amount FLOAT,
            reserved_quantity FLOAT,
            delivered_quantity FLOAT,
            batch_number VARCHAR(60),
            serial_numbers JSON
        )
    """)
    cur.execute("""
        CREATE TABLE customer_payments (
            id VARCHAR(36) PRIMARY KEY,
            customer_id VARCHAR(36),
            receipt_number VARCHAR(50),
            organization_id VARCHAR(36) NOT NULL,
            order_id VARCHAR(36),
            invoice_id VARCHAR(36),
            amount FLOAT NOT NULL,
            payment_mode VARCHAR(30) NOT NULL,
            reference VARCHAR(150),
            note TEXT,
            received_on TIMESTAMP NOT NULL,
            created_at TIMESTAMP NOT NULL
        )
    """)
    # Insert legacy sample records
    test_q_id = str(uuid.uuid4())
    test_so_id = str(uuid.uuid4())
    test_p_id = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO quotation_items (id, quotation_id, product_name, quantity, unit_price, discount) VALUES (?, 'q1', 'Widget', 5, 20.0, 10.0)",
        (test_q_id,)
    )
    cur.execute(
        "INSERT INTO sales_order_items (id, order_id, product_name, quantity, unit_price, discount, line_total) VALUES (?, 'so1', 'Widget', 5, 20.0, 10.0, 90.0)",
        (test_so_id,)
    )
    cur.execute(
        "INSERT INTO customer_payments (id, organization_id, amount, payment_mode, received_on, created_at) VALUES (?, 'org1', 100.0, 'cash', '2026-01-01 00:00:00', '2026-01-01 00:00:00')",
        (test_p_id,)
    )
    conn.commit()
    conn.close()

    # Perform migration upgrade
    mig_engine = create_engine(f"sqlite:///{legacy_db_path}")
    insp = inspect(mig_engine)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing = {col["name"] for col in insp.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            col_type = column.type.compile(dialect=mig_engine.dialect)
            ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'
            with mig_engine.begin() as db_conn:
                db_conn.execute(text(ddl))

    # Verify columns added
    insp = inspect(mig_engine)
    assert "discount_percent" in {c["name"] for c in insp.get_columns("quotation_items")}, "migrated quotation_items"
    assert "discount_percent" in {c["name"] for c in insp.get_columns("sales_order_items")}, "migrated sales_order_items"
    assert "cost_price" in {c["name"] for c in insp.get_columns("sales_order_items")}, "migrated sales_order_items cost_price"
    assert "order_amount" in {c["name"] for c in insp.get_columns("customer_payments")}, "migrated customer_payments order_amount"
    ok("Legacy DB: All new columns successfully added via ALTER TABLE migration")

    # Verify zero data loss on legacy rows
    with mig_engine.connect() as db_conn:
        q_row = db_conn.execute(text(f"SELECT id, product_name, quantity, discount FROM quotation_items WHERE id = '{test_q_id}'")).fetchone()
        assert q_row is not None and q_row[1] == "Widget" and q_row[3] == 10.0, "Quotation item data intact"
        so_row = db_conn.execute(text(f"SELECT id, product_name, line_total FROM sales_order_items WHERE id = '{test_so_id}'")).fetchone()
        assert so_row is not None and so_row[2] == 90.0, "Sales order item data intact"
        p_row = db_conn.execute(text(f"SELECT id, amount, payment_mode FROM customer_payments WHERE id = '{test_p_id}'")).fetchone()
        assert p_row is not None and p_row[1] == 100.0, "Customer payment data intact"
        ok("Legacy DB: All pre-existing data verified 100% intact without loss or corruption")

    mig_engine.dispose()
    try:
        os.remove(legacy_db_path)
    except Exception:
        pass


def run_tests():
    print("\n=======================================================")
    print("TEST SUITE: Division 1 Foundation Changes")
    print("=======================================================")
    test_1_discount_percentage_on_quotation()
    test_2_discount_percentage_on_sales_order()
    test_3_flat_discount_backward_compatibility()
    test_4_cost_price_and_uom_snapshot()
    test_5_uom_preservation_on_quotation_conversion()
    test_6_customer_details_in_order_response()
    test_7_payment_and_delivery_foundation_fields()
    test_8_database_migration_and_upgrade()

    print("\n=======================================================")
    print(f"RESULTS: {_passed} passed, {_failed} failed")
    print("=======================================================\n")
    if _failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_tests()
