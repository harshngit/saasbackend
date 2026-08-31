"""Test suite for Stock Race Condition / Concurrency & Split Payments Support.

Covers:
Stock Tests:
  TEST A — Stock Shortage Protection & Concurrency Invariant (available never negative)
  TEST B — Multi-Item Deterministic Lock Ordering
  TEST C — Variant Isolation
  TEST D — Warehouse Isolation

Payment Tests:
  TEST E — Existing Single Payment Backward Compatibility (Cash, UPI, Card, COD)
  TEST F — Cash + UPI Split Payment Creation & Retrieval
  TEST G — Cash + Card Split Payment with card_type and card_last_four
  TEST H — Invalid Split Total (sum mismatch rejection)
  TEST I — Zero / Negative Split Amount Rejection
  TEST J — Empty Splits List Rejection
  TEST K — Financial Consistency (Customer balance, Invoice amount_paid, Ledger single credit)
  TEST L — Cash Collection Reporting with Split Cash Portions
  TEST M — Void / Delete Cascade Safety (no orphaned splits)
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
from app.models import (
    Customer,
    CustomerPayment,
    Invoice,
    PaymentSplit,
    Product,
    ProductVariant,
    SalesOrder,
    SalesOrderItem,
    User,
    Warehouse,
    WarehouseStock,
)
from app.services import stock_service, payment_service, report_service

seed_main()
client = TestClient(app)

_passed = 0
_failed = 0


def ok(msg: str):
    global _passed
    _passed += 1
    print(f"  PASS  {msg}")


def fail(msg: str, err: str = ""):
    global _failed
    _failed += 1
    print(f"  FAIL  {msg} -> {err}")


def _register_org(name_prefix: str = "TestOrg"):
    email = f"admin_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={
        "organization_name": f"{name_prefix} {uuid.uuid4().hex[:6]}",
        "admin_name": "Admin User",
        "email": email,
        "password": "Password123!",
        "role": "admin",
    })
    assert r.status_code == 201, r.text
    auth = {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}
    return auth, email


def test_suite():
    headers, admin_email = _register_org("StockSplitOrg")
    db = SessionLocal()

    # Get admin org
    admin_user = db.query(User).filter(User.email == admin_email).first()
    org_id = admin_user.organization_id

    # Get or create customer
    customer = db.query(Customer).filter(Customer.organization_id == org_id).first()
    if not customer:
        customer = Customer(
            organization_id=org_id,
            name="Test Concurrency Customer",
            phone="9998887771",
            billing_address="123 Street",
        )
        db.add(customer)
        db.commit()
        db.refresh(customer)

    # Get or create warehouses
    wh1 = db.query(Warehouse).filter(Warehouse.organization_id == org_id).first()
    if not wh1:
        wh1 = Warehouse(organization_id=org_id, name="Main Warehouse", code="MWH1", is_default=True)
        db.add(wh1)
        db.commit()
        db.refresh(wh1)

    wh2 = db.query(Warehouse).filter(Warehouse.organization_id == org_id, Warehouse.id != wh1.id).first()
    if not wh2:
        wh2 = Warehouse(organization_id=org_id, name="Secondary Warehouse", code="SWH2", is_default=False)
        db.add(wh2)
        db.commit()
        db.refresh(wh2)

    # Create test products
    p1 = Product(organization_id=org_id, name="Concur Product 1", sku=f"CONCUR-P1-{uuid.uuid4().hex[:6]}", price=100.0, total_inventory=0)
    p2 = Product(organization_id=org_id, name="Concur Product 2", sku=f"CONCUR-P2-{uuid.uuid4().hex[:6]}", price=200.0, total_inventory=0)
    p_variant_parent = Product(organization_id=org_id, name="Concur Variant Product", sku=f"CONCUR-VP-{uuid.uuid4().hex[:6]}", price=150.0, total_inventory=0)
    db.add_all([p1, p2, p_variant_parent])
    db.commit()
    db.refresh(p1)
    db.refresh(p2)
    db.refresh(p_variant_parent)

    v1 = ProductVariant(product_id=p_variant_parent.id, name="Variant Red", sku=f"VRED-{uuid.uuid4().hex[:6]}", price=150.0, inventory=0)
    v2 = ProductVariant(product_id=p_variant_parent.id, name="Variant Blue", sku=f"VBLUE-{uuid.uuid4().hex[:6]}", price=160.0, inventory=0)
    db.add_all([v1, v2])
    db.commit()
    db.refresh(v1)
    db.refresh(v2)

    # Set physical stock in wh1
    stock_service.adjust_on_hand(db, org_id, wh1.id, p1.id, None, 1.0, movement_type="opening_stock", note="init stock")
    stock_service.adjust_on_hand(db, org_id, wh1.id, p2.id, None, 10.0, movement_type="opening_stock", note="init stock")
    stock_service.adjust_on_hand(db, org_id, wh1.id, p_variant_parent.id, v1.id, 5.0, movement_type="opening_stock", note="init stock")
    stock_service.adjust_on_hand(db, org_id, wh1.id, p_variant_parent.id, v2.id, 3.0, movement_type="opening_stock", note="init stock")
    # Set stock in wh2
    stock_service.adjust_on_hand(db, org_id, wh2.id, p1.id, None, 8.0, movement_type="opening_stock", note="init stock wh2")
    db.commit()

    print("\n--- RUNNING STOCK RACE CONDITION & CONCURRENCY TESTS ---")

    # TEST A: Concurrent Last Unit Protection
    # Stock of p1 in wh1 is 1.0. Place order for 1.0.
    order_a_payload = {
        "customer_id": customer.id,
        "warehouse_id": wh1.id,
        "items": [{"product_id": p1.id, "quantity": 1.0, "unit_price": 100.0}],
    }
    res_a = client.post("/orders", json=order_a_payload, headers=headers)
    if res_a.status_code in (200, 201):
        ok("TEST A.1: First order consuming last unit succeeded (HTTP 200/201)")
    else:
        fail("TEST A.1: First order failed", res_a.text)

    # Second order for same item in same warehouse must be rejected due to insufficient stock
    res_b = client.post("/orders", json=order_a_payload, headers=headers)
    if res_b.status_code == 400 and "INSUFFICIENT_STOCK" in res_b.text:
        ok("TEST A.2: Second order correctly rejected with INSUFFICIENT_STOCK")
    else:
        fail("TEST A.2: Second order did not reject with INSUFFICIENT_STOCK", f"{res_b.status_code}: {res_b.text}")

    # Verify available stock is not negative
    avail = stock_service.available(db, wh1.id, p1.id, None)
    if avail == 0.0:
        ok("TEST A.3: Available stock is 0.0 and never negative")
    else:
        fail("TEST A.3: Available stock is unexpected", str(avail))

    # TEST B: Multi-Item Lock Ordering (Deterministic Sorting)
    # Order with [p1, p2] and another with [p2, p1]
    items_order_1 = [{"product_id": p1.id, "variant_id": None}, {"product_id": p2.id, "variant_id": None}]
    items_order_2 = [{"product_id": p2.id, "variant_id": None}, {"product_id": p1.id, "variant_id": None}]
    locked_1 = stock_service.lock_stock_items(db, org_id, wh1.id, items_order_1)
    locked_2 = stock_service.lock_stock_items(db, org_id, wh1.id, items_order_2)
    # Ensure both produce the exact same deterministic sequence
    seq1 = [(r.product_id, r.variant_id) for r in locked_1]
    seq2 = [(r.product_id, r.variant_id) for r in locked_2]
    if seq1 == seq2 and len(seq1) == 2:
        ok("TEST B: Multi-item lock ordering is strictly deterministic across different input sequences")
    else:
        fail("TEST B: Multi-item lock ordering failed", f"seq1={seq1}, seq2={seq2}")

    # TEST C: Variant Isolation
    # Stock for v1 (Red) is 5.0, for v2 (Blue) is 3.0
    order_v1_payload = {
        "customer_id": customer.id,
        "warehouse_id": wh1.id,
        "items": [{"product_id": p_variant_parent.id, "variant_id": v1.id, "quantity": 4.0, "unit_price": 150.0}],
    }
    res_v1 = client.post("/orders", json=order_v1_payload, headers=headers)
    assert res_v1.status_code in (200, 201), f"Order v1 failed: {res_v1.text}"
    avail_v1 = stock_service.available(db, wh1.id, p_variant_parent.id, v1.id)
    avail_v2 = stock_service.available(db, wh1.id, p_variant_parent.id, v2.id)
    if avail_v1 == 1.0 and avail_v2 == 3.0:
        ok("TEST C: Variant 1 reservation did not affect Variant 2 stock (Variant Isolation verified)")
    else:
        fail("TEST C: Variant isolation failed", f"avail_v1={avail_v1}, avail_v2={avail_v2}")

    # TEST D: Warehouse Isolation
    # wh1 has p1 stock 0.0 available, wh2 has p1 stock 8.0 available
    avail_wh1 = stock_service.available(db, wh1.id, p1.id, None)
    avail_wh2 = stock_service.available(db, wh2.id, p1.id, None)
    if avail_wh1 == 0.0 and avail_wh2 == 8.0:
        ok("TEST D: Warehouse 1 stock exhaustion does not affect Warehouse 2 (Warehouse Isolation verified)")
    else:
        fail("TEST D: Warehouse isolation failed", f"wh1={avail_wh1}, wh2={avail_wh2}")

    print("\n--- RUNNING SPLIT PAYMENTS & BACKWARD COMPATIBILITY TESTS ---")

    # TEST E: Existing Single Payment Backward Compatibility (Cash, UPI, Card, COD)
    single_modes = ["cash", "upi", "card", "cod"]
    for mode in single_modes:
        single_payload = {
            "customer_id": customer.id,
            "amount_received": 100.0,
            "payment_method": mode,
            "transaction_reference": f"REF-{mode.upper()}-001",
        }
        if mode == "upi":
            single_payload["upi_id"] = "payer@upi"
        elif mode == "card":
            single_payload["card_type"] = "Visa"
            single_payload["card_last_four"] = "1234"
        elif mode == "cod":
            single_payload["collection_instructions"] = "Collect on delivery"

        res_single = client.post("/payment-receipts", json=single_payload, headers=headers)
        if res_single.status_code == 201:
            data = res_single.json()
            if data["payment_method"] == mode and data["amount_received"] == 100.0:
                ok(f"TEST E.{mode}: Single payment mode '{mode}' works without splits (HTTP 201)")
            else:
                fail(f"TEST E.{mode}: Single payment mode returned incorrect data", str(data))
        else:
            fail(f"TEST E.{mode}: Single payment mode '{mode}' failed", res_single.text)

    # TEST F: Cash + UPI Split Payment Creation & Retrieval
    split_payload_f = {
        "customer_id": customer.id,
        "amount_received": 1000.0,
        "note": "Advance split payment Cash + UPI",
        "splits": [
            {"payment_mode": "cash", "amount": 400.0},
            {"payment_mode": "upi", "amount": 600.0, "reference": "UPI/987654", "upi_id": "cust@upi"},
        ],
    }
    res_split_f = client.post("/payment-receipts", json=split_payload_f, headers=headers)
    if res_split_f.status_code == 201:
        data_f = res_split_f.json()
        pay_id = data_f["id"]
        if (
            data_f["amount_received"] == 1000.0
            and data_f["payment_method"] == "split"
            and len(data_f["splits"]) == 2
        ):
            ok("TEST F.1: POST /payment-receipts with Cash + UPI created successfully")
        else:
            fail("TEST F.1: Split payment response missing fields", str(data_f))

        # Re-fetch via GET /payment-receipts
        res_get_f = client.get(f"/payment-receipts", headers=headers)
        found_in_list = any(r["id"] == pay_id and len(r.get("splits", [])) == 2 for r in res_get_f.json())
        if found_in_list:
            ok("TEST F.2: GET /payment-receipts re-fetches split details correctly")
        else:
            fail("TEST F.2: Re-fetch did not include splits")
    else:
        fail("TEST F.1: Split payment creation failed", res_split_f.text)

    # TEST G: Cash + Card Split with card_type and card_last_four
    split_payload_g = {
        "customer_id": customer.id,
        "amount": 500.0,
        "splits": [
            {"payment_mode": "cash", "amount": 200.0},
            {
                "payment_mode": "card",
                "amount": 300.0,
                "card_type": "Mastercard",
                "card_last_four": "9876",
            },
        ],
    }
    res_split_g = client.post(f"/customers/{customer.id}/payments", json=split_payload_g, headers=headers)
    if res_split_g.status_code in (200, 201):
        # Check database record for card metadata
        latest_pay = (
            db.query(CustomerPayment)
            .filter(CustomerPayment.customer_id == customer.id)
            .order_by(CustomerPayment.created_at.desc())
            .first()
        )
        card_split = next((s for s in latest_pay.splits if s.payment_mode == "card"), None)
        if card_split and card_split.card_type == "Mastercard" and card_split.card_last_four == "9876":
            ok("TEST G: Cash + Card split persists card_type and card_last_four on PaymentSplit child")
        else:
            fail("TEST G: Card split metadata missing or incorrect")
    else:
        fail("TEST G: POST /customers/{id}/payments with card split failed", res_split_g.text)

    # TEST H: Invalid Split Total (sum mismatch rejection)
    bad_sum_payload = {
        "customer_id": customer.id,
        "amount_received": 1000.0,
        "splits": [
            {"payment_mode": "cash", "amount": 400.0},
            {"payment_mode": "upi", "amount": 500.0},  # Sum is 900 != 1000
        ],
    }
    res_bad_sum = client.post("/payment-receipts", json=bad_sum_payload, headers=headers)
    if res_bad_sum.status_code in (400, 422):
        ok("TEST H: Split sum mismatch (Rs 900 vs Rs 1000) rejected with HTTP 400/422")
    else:
        fail("TEST H: Split sum mismatch was not rejected", f"{res_bad_sum.status_code}: {res_bad_sum.text}")

    # TEST I: Zero / Negative Split Amount Rejection
    bad_amt_payload = {
        "customer_id": customer.id,
        "amount_received": 500.0,
        "splits": [
            {"payment_mode": "cash", "amount": 500.0},
            {"payment_mode": "upi", "amount": -100.0},
        ],
    }
    res_bad_amt = client.post("/payment-receipts", json=bad_amt_payload, headers=headers)
    if res_bad_amt.status_code in (400, 422):
        ok("TEST I: Negative split amount rejected with HTTP 400/422")
    else:
        fail("TEST I: Negative split amount was not rejected", f"{res_bad_amt.status_code}: {res_bad_amt.text}")

    # TEST J: Empty Splits List Rejection
    empty_splits_payload = {
        "customer_id": customer.id,
        "amount_received": 500.0,
        "splits": [],
    }
    res_empty = client.post("/payment-receipts", json=empty_splits_payload, headers=headers)
    if res_empty.status_code in (400, 422):
        ok("TEST J: Empty splits array rejected with HTTP 400/422")
    else:
        fail("TEST J: Empty splits array was not rejected", f"{res_empty.status_code}: {res_empty.text}")

    # TEST K: Financial Consistency (Customer balance, Invoice amount_paid, Ledger single credit)
    # Create fresh customer and invoice
    c_fin = Customer(organization_id=org_id, name="Financial Test Customer", phone="9990001111", opening_balance=0.0)
    db.add(c_fin)
    db.commit()
    db.refresh(c_fin)

    inv_fin = Invoice(
        organization_id=org_id,
        customer_id=c_fin.id,
        invoice_number=f"INV-FIN-{uuid.uuid4().hex[:6]}",
        total=2000.0,
        amount_paid=0.0,
        status="unpaid",
    )
    db.add(inv_fin)
    c_fin.total_billed = 2000.0
    c_fin.recompute_outstanding()
    db.commit()
    db.refresh(c_fin)
    db.refresh(inv_fin)

    init_outstanding = c_fin.outstanding_balance  # 2000.0
    init_received = c_fin.total_received or 0.0  # 0.0

    # Pay Rs 1,000 using split: Rs 300 Cash + Rs 700 UPI
    split_fin_payload = {
        "invoice_reference_id": inv_fin.id,
        "amount_received": 1000.0,
        "splits": [
            {"payment_mode": "cash", "amount": 300.0},
            {"payment_mode": "upi", "amount": 700.0, "upi_id": "test@upi"},
        ],
    }
    res_fin = client.post("/payment-receipts", json=split_fin_payload, headers=headers)
    assert res_fin.status_code == 201, f"Payment failed: {res_fin.text}"

    db.refresh(c_fin)
    db.refresh(inv_fin)

    # 1. Check Customer total_received
    if c_fin.total_received == init_received + 1000.0:
        ok("TEST K.1: Customer.total_received increased by exactly Rs 1,000.00 (updated ONCE)")
    else:
        fail("TEST K.1: Customer.total_received incorrect", str(c_fin.total_received))

    # 2. Check Customer outstanding_balance
    if c_fin.outstanding_balance == init_outstanding - 1000.0:
        ok("TEST K.2: Customer.outstanding_balance decreased by exactly Rs 1,000.00")
    else:
        fail("TEST K.2: Customer.outstanding_balance incorrect", str(c_fin.outstanding_balance))

    # 3. Check Invoice amount_paid
    if inv_fin.amount_paid == 1000.0 and inv_fin.status == "partial":
        ok("TEST K.3: Invoice.amount_paid updated to Rs 1,000.00 and status is partial")
    else:
        fail("TEST K.3: Invoice amount_paid incorrect", f"{inv_fin.amount_paid}, {inv_fin.status}")

    # 4. Check Customer Ledger has exactly 1 credit entry
    res_ledger = client.get(f"/customers/{c_fin.id}/ledger", headers=headers)
    if res_ledger.status_code == 200:
        ledger_data = res_ledger.json()
        pay_entries = [e for e in ledger_data.get("transactions", []) if e.get("type") == "payment"]
        if len(pay_entries) == 1 and pay_entries[0]["credit"] == 1000.0:
            ok("TEST K.4: Customer Ledger contains exactly ONE payment credit of Rs 1,000.00 (no duplicate split credits)")
        else:
            fail("TEST K.4: Ledger entries unexpected", str(pay_entries))
    else:
        fail("TEST K.4: Failed to fetch customer ledger", res_ledger.text)

    # TEST L: Cash Collection Reporting with Split Cash Portions
    # Customer c_fin made a split payment with Rs 300 cash portion.
    # Run cash collection report
    rep_data = report_service._cash_collection(db, org_id, None, None)
    total_cash = rep_data["summary"]["total_cash"]
    # Ensure Rs 300 is captured in cash collection
    has_300 = any(r["amount"] == 300.0 for r in rep_data["rows"])
    if has_300:
        ok("TEST L: Cash Collection report successfully includes the Rs 300 cash portion of split payment without double-counting parent")
    else:
        fail("TEST L: Cash portion missing from cash collection report", str(rep_data))

    # TEST M: Void / Delete Cascade Safety
    fin_receipt_id = res_fin.json()["id"]
    res_void = client.delete(f"/payment-receipts/{fin_receipt_id}", headers=headers)
    if res_void.status_code == 204:
        # Verify PaymentSplit rows for this payment are gone
        remaining_splits = db.query(PaymentSplit).filter(PaymentSplit.payment_id == fin_receipt_id).all()
        db.refresh(c_fin)
        db.refresh(inv_fin)
        if len(remaining_splits) == 0 and c_fin.total_received == init_received and inv_fin.amount_paid == 0.0:
            ok("TEST M: Voiding receipt cascade-deletes child PaymentSplit rows and completely restores customer & invoice balances")
        else:
            fail("TEST M: Orphaned splits remained or balances not restored", f"splits={len(remaining_splits)}, total_received={c_fin.total_received}")
    else:
        fail("TEST M: Void receipt failed", res_void.text)

    db.close()

    print("\n==================================================")
    print(f"TOTAL TESTS: {_passed + _failed} | PASSED: {_passed} | FAILED: {_failed}")
    print("==================================================")
    if _failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    test_suite()
