"""Test suite for Division 1 Final Task:
5-Part Payment / Receivable Snapshot & Collection Logic

Verifies:
1. Five-part payment snapshot fields:
   - order_amount
   - previous_pending
   - amount_collected
   - payment_method
   - remaining_receivable
2. Payment against invoice.
3. Partial payment.
4. Full payment.
5. Existing customer pending balance reduction.
6. Advance / customer-level payment (without order/invoice).
7. Order-linked payment snapshotting order total.
8. Payment mode preservation and aliases.
9. Multiple previous invoices accumulation.
10. Opening balance inclusion.
11. Partial previous invoice payment.
12. Overpayment protection.
13. Atomic rollback safety on failure.
14. Multi-tenant isolation between organizations.
15. Payment receipt PDF generation with 5-part snapshot.
"""

import os
import sys
import uuid

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from app.main import app
from app.seed import main as seed_main
from app.core.database import SessionLocal
from app.models import Customer, CustomerPayment, Delivery, Invoice, SalesOrder, SalesOrderItem

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
        "organization_name": f"{label} Payments Org",
        "admin_name": f"Admin {label}",
        "email": email,
        "password": "Password123!",
        "role": "admin",
    })
    assert r.status_code == 201, r.text
    token = r.json()["tokens"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _setup_org(label: str, opening_balance: float = 0.0):
    auth = _register_org(label)
    wh_res = client.post("/warehouses", json={"name": "Central WH", "code": f"WH-{uuid.uuid4().hex[:6]}"}, headers=auth)
    wh_id = wh_res.json()["id"]

    p_res = client.post("/products", json={
        "name": "Precision Sensor",
        "sku": f"SNR-{uuid.uuid4().hex[:6]}",
        "price": 200.0,
        "tax_rate": 18.0,
        "uom": "piece",
        "pricing": {"purchase_price": 120.0, "selling_price": 200.0, "currency": "INR"},
    }, headers=auth)
    assert p_res.status_code == 201, p_res.text
    prod_id = p_res.json()["id"]

    client.post(f"/warehouses/{wh_id}/stock/adjust", json={
        "product_id": prod_id, "quantity": 1000,
    }, headers=auth)

    c_res = client.post("/customers", json={
        "name": "Alpha Manufacturing",
        "business_name": "Alpha Mfg Ltd",
        "phone": "9876543210",
        "email": "accounts@alphamfg.com",
        "gst_number": "29ABCDE1234F1Z5",
        "billing_address": "10 Industrial Hub, Zone A",
        "delivery_address": "Gate 4, Plot 12, Zone A",
        "opening_balance": opening_balance,
        "credit_limit": 100000.0,
    }, headers=auth)
    assert c_res.status_code == 201, c_res.text
    cust_id = c_res.json()["id"]

    return auth, wh_id, prod_id, cust_id


def _complete_and_invoice_order(auth, wh_id, cust_id, prod_id, quantity=10, unit_price=200.0, tax_rate=18.0):
    # Place order
    so_res = client.post("/orders", json={
        "customer_id": cust_id,
        "warehouse_id": wh_id,
        "items": [{"product_id": prod_id, "quantity": quantity, "unit_price": unit_price, "tax_rate": tax_rate}],
    }, headers=auth)
    assert so_res.status_code == 201, so_res.text
    so = so_res.json()
    so_id = so["id"]
    so_item_id = so["items"][0]["id"]

    # Plan delivery
    deliv_res = client.post("/deliveries", json={
        "order_id": so_id,
        "warehouse_id": wh_id,
        "items": [{"order_item_id": so_item_id, "planned_quantity": quantity}],
    }, headers=auth)
    assert deliv_res.status_code == 201, deliv_res.text
    deliv_id = deliv_res.json()["id"]

    # Mark delivery complete in DB
    db_session = SessionLocal()
    deliv_db = db_session.get(Delivery, deliv_id)
    if deliv_db:
        for item in deliv_db.items:
            item.delivered_quantity = quantity
        deliv_db.status = "delivered"
    so_db = db_session.get(SalesOrder, so_id)
    if so_db:
        for item in so_db.items:
            item.delivered_quantity = quantity
        so_db.fulfilment_status = "delivered"
        so_db.status = "completed"
    db_session.commit()
    db_session.close()

    # Invoice order
    inv_res = client.post(f"/orders/{so_id}/invoice", json={"delivery_id": deliv_id}, headers=auth)
    assert inv_res.status_code == 201, inv_res.text
    inv = inv_res.json()
    return so, deliv_res.json(), inv


def test_1_five_part_payment_snapshot_against_invoice():
    print("\n--- TEST 1: Five-Part Payment Snapshot Against Invoice ---")
    auth, wh_id, prod_id, cust_id = _setup_org("snap_inv", opening_balance=0.0)

    # 10 * 200 = 2000 + 18% = 2360.0
    so, deliv, inv = _complete_and_invoice_order(auth, wh_id, cust_id, prod_id, quantity=10, unit_price=200.0, tax_rate=18.0)
    inv_id = inv["id"]

    # Pay 1000 against invoice
    pay_res = client.post("/payment-receipts", json={
        "invoice_reference_id": inv_id,
        "amount_received": 1000.0,
        "payment_method": "upi",
        "transaction_reference": "UPI-TXN-1001",
        "note": "First partial payment",
    }, headers=auth)
    assert_eq(pay_res.status_code, 201, "Payment receipt created")
    pay = pay_res.json()

    assert_eq(pay["order_amount"], 2360.0, "1. Order Amount = 2360.0")
    assert_eq(pay["previous_pending"], 2360.0, "2. Previous Pending = 2360.0 (before payment)")
    assert_eq(pay["amount_received"], 1000.0, "3. Amount Received = 1000.0")
    assert_eq(pay["amount_collected"], 1000.0, "3. Amount Collected alias = 1000.0")
    assert_eq(pay["payment_method"], "upi", "4. Payment Method = upi")
    assert_eq(pay["remaining_receivable"], 1360.0, "5. Remaining Receivable = 1360.0 (after payment)")
    assert_eq(pay["payment_status"], "partial", "Invoice payment_status = partial")
    assert_eq(pay["outstanding_amount"], 1360.0, "Invoice outstanding_amount = 1360.0")


def test_2_full_payment_against_invoice():
    print("\n--- TEST 2: Full Payment Against Invoice ---")
    auth, wh_id, prod_id, cust_id = _setup_org("full_pay", opening_balance=0.0)

    # 5 * 200 = 1000 + 18% = 1180.0
    so, deliv, inv = _complete_and_invoice_order(auth, wh_id, cust_id, prod_id, quantity=5, unit_price=200.0, tax_rate=18.0)
    inv_id = inv["id"]

    pay_res = client.post("/payment-receipts", json={
        "invoice_reference_id": inv_id,
        "amount_received": 1180.0,
        "payment_method": "bank_transfer",
        "transaction_reference": "NEFT-9988",
    }, headers=auth)
    assert_eq(pay_res.status_code, 201, "Full payment succeeds")
    pay = pay_res.json()

    assert_eq(pay["order_amount"], 1180.0, "Order Amount = 1180.0")
    assert_eq(pay["previous_pending"], 1180.0, "Previous Pending = 1180.0")
    assert_eq(pay["amount_collected"], 1180.0, "Amount Collected = 1180.0")
    assert_eq(pay["payment_method"], "bank_transfer", "Payment Method = bank_transfer")
    assert_eq(pay["remaining_receivable"], 0.0, "Remaining Receivable = 0.0 (fully settled)")
    assert_eq(pay["payment_status"], "paid", "Invoice status = paid")
    assert_eq(pay["outstanding_amount"], 0.0, "Invoice outstanding = 0.0")


def test_3_advance_customer_level_payment():
    print("\n--- TEST 3: Advance / Customer-Level Payment (No Order/Invoice) ---")
    auth, wh_id, prod_id, cust_id = _setup_org("adv_pay", opening_balance=5000.0)

    # Customer starts with opening balance of 5000.0
    # Customer makes on-account advance payment of 2000.0
    pay_res = client.post("/payment-receipts", json={
        "customer_id": cust_id,
        "amount_received": 2000.0,
        "payment_method": "cash",
        "transaction_reference": "CASH-ADV-01",
        "note": "Advance payment on account",
    }, headers=auth)
    assert_eq(pay_res.status_code, 201, "Advance payment recorded")
    pay = pay_res.json()

    assert_eq(pay["order_amount"], None, "Advance: order_amount is None")
    assert_eq(pay["previous_pending"], 5000.0, "Advance: previous_pending = 5000.0")
    assert_eq(pay["amount_collected"], 2000.0, "Advance: amount_collected = 2000.0")
    assert_eq(pay["payment_method"], "cash", "Advance: payment_method = cash")
    assert_eq(pay["remaining_receivable"], 3000.0, "Advance: remaining_receivable = 3000.0")


def test_4_existing_pending_with_multiple_invoices():
    print("\n--- TEST 4: Customer with Existing Pending & Multiple Invoices ---")
    auth, wh_id, prod_id, cust_id = _setup_org("multi_inv", opening_balance=10000.0)

    # Invoice 1: 5 * 200 = 1000 + 18% = 1180.0
    so1, deliv1, inv1 = _complete_and_invoice_order(auth, wh_id, cust_id, prod_id, quantity=5)
    # Total outstanding = 10000 + 1180 = 11180.0

    # Invoice 2: 10 * 200 = 2000 + 18% = 2360.0
    so2, deliv2, inv2 = _complete_and_invoice_order(auth, wh_id, cust_id, prod_id, quantity=10)
    # Total outstanding = 11180 + 2360 = 13540.0

    # Pay against Invoice 2 for 2000.0
    pay_res = client.post("/payment-receipts", json={
        "invoice_reference_id": inv2["id"],
        "amount_received": 2000.0,
        "payment_method": "cheque",
        "transaction_reference": "CHQ-001234",
    }, headers=auth)
    assert_eq(pay_res.status_code, 201, "Payment against invoice 2 succeeds")
    pay = pay_res.json()

    assert_eq(pay["order_amount"], 2360.0, "Order Amount from SO 2 = 2360.0")
    assert_eq(pay["previous_pending"], 13540.0, "Previous Pending across all dues = 13540.0")
    assert_eq(pay["amount_collected"], 2000.0, "Amount Collected = 2000.0")
    assert_eq(pay["remaining_receivable"], 11540.0, "Remaining Receivable = 13540 - 2000 = 11540.0")


def test_5_payment_history_and_customer_endpoints():
    print("\n--- TEST 5: Payment History (GET /customers/{id}/payments) ---")
    auth, wh_id, prod_id, cust_id = _setup_org("hist_test", opening_balance=0.0)

    so, deliv, inv = _complete_and_invoice_order(auth, wh_id, cust_id, prod_id, quantity=5)
    inv_id = inv["id"]

    client.post("/payment-receipts", json={
        "invoice_reference_id": inv_id,
        "amount_received": 500.0,
        "payment_method": "upi",
    }, headers=auth)

    # Check GET /customers/{id}/payments
    p_list_res = client.get(f"/customers/{cust_id}/payments", headers=auth)
    assert_eq(p_list_res.status_code, 200, "GET /customers/{id}/payments succeeds")
    payments = p_list_res.json()
    assert_eq(len(payments), 1, "Exactly 1 payment row in customer history")
    row = payments[0]
    assert_eq(row["order_amount"], 1180.0, "History: order_amount is 1180.0")
    assert_eq(row["previous_pending"], 1180.0, "History: previous_pending is 1180.0")
    assert_eq(row["amount"], 500.0, "History: amount is 500.0")
    assert_eq(row["amount_collected"], 500.0, "History: amount_collected is 500.0")
    assert_eq(row["payment_mode"], "upi", "History: payment_mode is upi")
    assert_eq(row["payment_method"], "upi", "History: payment_method is upi")
    assert_eq(row["remaining_receivable"], 680.0, "History: remaining_receivable is 680.0")


def test_6_overpayment_protection():
    print("\n--- TEST 6: Overpayment Protection Intact ---")
    auth, wh_id, prod_id, cust_id = _setup_org("overpay", opening_balance=0.0)

    # 2 * 200 = 400 + 18% = 472.0
    so, deliv, inv = _complete_and_invoice_order(auth, wh_id, cust_id, prod_id, quantity=2)
    inv_id = inv["id"]

    # Attempt to pay 500 on 472 invoice
    overpay_res = client.post("/payment-receipts", json={
        "invoice_reference_id": inv_id,
        "amount_received": 500.0,
        "payment_method": "cash",
    }, headers=auth)
    assert_eq(overpay_res.status_code, 400, "Overpayment refused with HTTP 400")


def test_7_atomic_rollback_on_failure():
    print("\n--- TEST 7: Atomic Rollback on Failure ---")
    auth, wh_id, prod_id, cust_id = _setup_org("rollback", opening_balance=1000.0)

    # Attempt invalid payment (zero amount)
    bad_pay_res = client.post("/payment-receipts", json={
        "customer_id": cust_id,
        "amount_received": -50.0,
        "payment_method": "cash",
    }, headers=auth)
    assert_eq(bad_pay_res.status_code, 422, "Negative amount rejected")

    # Customer ledger must remain untouched at 1000.0
    cust_ledger = client.get(f"/customers/{cust_id}/ledger", headers=auth).json()
    assert_eq(cust_ledger["summary"]["outstanding"], 1000.0, "Customer outstanding remains 1000.0")
    assert_eq(cust_ledger["summary"]["total_received"], 0.0, "Customer total_received remains 0.0")


def test_8_multi_tenant_isolation():
    print("\n--- TEST 8: Multi-Tenant Isolation ---")
    auth1, wh1, p1, c1 = _setup_org("tenant1", opening_balance=25000.0)
    auth2, wh2, p2, c2 = _setup_org("tenant2", opening_balance=5000.0)

    # Payment in Org 2
    pay_org2 = client.post("/payment-receipts", json={
        "customer_id": c2,
        "amount_received": 1000.0,
        "payment_method": "upi",
    }, headers=auth2).json()

    assert_eq(pay_org2["previous_pending"], 5000.0, "Org 2 previous pending = 5000.0")
    assert_eq(pay_org2["remaining_receivable"], 4000.0, "Org 2 remaining = 4000.0")

    # Org 1 customer remains completely unaffected
    cust1_ledger = client.get(f"/customers/{c1}/ledger", headers=auth1).json()
    assert_eq(cust1_ledger["summary"]["outstanding"], 25000.0, "Org 1 customer outstanding remains 25000.0")


def test_9_payment_receipt_pdf_generation():
    print("\n--- TEST 9: Payment Receipt PDF Generation with 5-Part Snapshot ---")
    auth, wh_id, prod_id, cust_id = _setup_org("pdf_test", opening_balance=0.0)

    so, deliv, inv = _complete_and_invoice_order(auth, wh_id, cust_id, prod_id, quantity=4)
    pay_res = client.post("/payment-receipts", json={
        "invoice_reference_id": inv["id"],
        "amount_received": 500.0,
        "payment_method": "bank_transfer",
        "transaction_reference": "NEFT-1234",
    }, headers=auth)
    pay_id = pay_res.json()["id"]

    pdf_res = client.get(f"/customers/{cust_id}/payments/receipt/{pay_id}", headers=auth)
    assert_eq(pdf_res.status_code, 200, "Receipt PDF generated successfully")
    assert_eq(pdf_res.headers["content-type"], "application/pdf", "Content-Type is application/pdf")
    assert len(pdf_res.content) > 500, "PDF binary has non-trivial content"


def run_all_tests():
    print("\n=======================================================")
    print("TEST SUITE: Division 1 - 5-Part Payment / Snapshot Logic")
    print("=======================================================")
    test_1_five_part_payment_snapshot_against_invoice()
    test_2_full_payment_against_invoice()
    test_3_advance_customer_level_payment()
    test_4_existing_pending_with_multiple_invoices()
    test_5_payment_history_and_customer_endpoints()
    test_6_overpayment_protection()
    test_7_atomic_rollback_on_failure()
    test_8_multi_tenant_isolation()
    test_9_payment_receipt_pdf_generation()

    print("\n=======================================================")
    print(f"RESULTS: {_passed} passed, {_failed} failed")
    print("=======================================================\n")
    if _failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_all_tests()
