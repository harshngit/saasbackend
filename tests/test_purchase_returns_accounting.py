"""Automated accounting consistency test suite for Purchase Returns.

Verifies end-to-end financial consistency between:
- Supplier.total_purchases / Supplier.outstanding_payable
- PurchaseInvoice.return_amount / PurchaseInvoice.outstanding_balance
- SupplierInvoice.return_amount / SupplierInvoice.outstanding_amount / payment_status
- Accounts Payable endpoints (/accounts-payable, /accounts-payable/summary, /accounts-payable/supplier/{id})
- Supplier Payment allocation validation
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from app.main import app
from app.core.database import SessionLocal
from app.models import (
    Product,
    ProductVariant,
    PurchaseInvoice,
    PurchaseInvoiceItem,
    PurchaseReturn,
    PurchaseReturnItem,
    Supplier,
    SupplierInvoice,
    User,
    Warehouse,
)
from app.seed import main as seed_main

seed_main()
client = TestClient(app)

passed_count = 0
failed_count = 0


def check(description: str, condition: bool):
    global passed_count, failed_count
    if condition:
        print(f"  PASS  {description}")
        passed_count += 1
    else:
        print(f"  FAIL  {description}")
        failed_count += 1


def register_org(name_prefix: str = "PR Acct Firm"):
    email = f"pr_acct_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={
        "organization_name": f"{name_prefix} {uuid.uuid4().hex[:6]}",
        "admin_name": "PR Acct Admin",
        "email": email,
        "password": "Password123!",
        "role": "admin",
    })
    assert r.status_code == 201, r.text
    token = r.json()["tokens"]["access_token"]
    auth = {"Authorization": f"Bearer {token}"}

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        org_id = user.organization_id
    finally:
        db.close()

    return auth, org_id


def setup_test_purchase(auth, org_id, total_qty=100, unit_price=1000.0):
    """Creates a supplier, warehouse, product, and confirmed purchase order for 100 * 1000 = 100,000 INR."""
    db = SessionLocal()
    try:
        supplier = Supplier(
            organization_id=org_id,
            name=f"Supplier {uuid.uuid4().hex[:6]}",
            email=f"sup_{uuid.uuid4().hex[:6]}@example.com",
            total_purchases=0.0,
            total_paid=0.0,
        )
        wh = Warehouse(
            organization_id=org_id,
            name=f"Warehouse {uuid.uuid4().hex[:6]}",
            code=f"WH-{uuid.uuid4().hex[:4]}",
        )
        prod = Product(
            organization_id=org_id,
            name="Industrial Widget",
            sku=f"WIDGET-{uuid.uuid4().hex[:4]}",
            total_inventory=0,
        )
        db.add_all([supplier, wh, prod])
        db.flush()

        # Create confirmed purchase invoice
        po_total = total_qty * unit_price
        po = PurchaseInvoice(
            organization_id=org_id,
            invoice_number=f"PO-{uuid.uuid4().hex[:6]}",
            purchase_number=f"PO-{uuid.uuid4().hex[:6]}",
            supplier_id=supplier.id,
            warehouse_id=wh.id,
            status="confirmed",
            payment_status="unpaid",
            total=po_total,
            amount_paid=0.0,
            return_amount=0.0,
        )
        db.add(po)
        db.flush()

        po_item = PurchaseInvoiceItem(
            invoice_id=po.id,
            product_id=prod.id,
            product_name=prod.name,
            quantity=total_qty,
            purchase_price=unit_price,
            tax_rate=0.0,
            tax=0.0,
            line_total=po_total,
        )
        db.add(po_item)

        # Update inventory and supplier totals for confirmed PO
        prod.total_inventory = total_qty
        supplier.total_purchases = po_total

        # Auto-create linked SupplierInvoice
        sinv = SupplierInvoice(
            organization_id=org_id,
            supplier_id=supplier.id,
            purchase_id=po.id,
            supplier_invoice_number=f"SINV-{uuid.uuid4().hex[:6]}",
            status="recorded",
            payment_status="unpaid",
            verification_status="matched",
            subtotal=po_total,
            grand_total=po_total,
            amount_paid=0.0,
            return_amount=0.0,
        )
        db.add(sinv)
        db.commit()

        return {
            "supplier_id": supplier.id,
            "warehouse_id": wh.id,
            "product_id": prod.id,
            "purchase_id": po.id,
            "purchase_item_id": po_item.id,
            "supplier_invoice_id": sinv.id,
        }
    finally:
        db.close()


def test_basic_unpaid_return_scenario():
    print("\n--- Scenario 1: Basic Unpaid Invoice (Purchase INR 100,000, Return INR 20,000) ---")
    auth, org_id = register_org("Unpaid PO Test")
    data = setup_test_purchase(auth, org_id, total_qty=100, unit_price=1000.0)

    # Initial checks: All report 100,000
    db = SessionLocal()
    try:
        sup = db.get(Supplier, data["supplier_id"])
        po = db.get(PurchaseInvoice, data["purchase_id"])
        sinv = db.get(SupplierInvoice, data["supplier_invoice_id"])
        check("Initial Supplier total_purchases is 100,000", sup.total_purchases == 100000.0)
        check("Initial Supplier outstanding_payable is 100,000", sup.outstanding_payable == 100000.0)
        check("Initial PurchaseInvoice outstanding_balance is 100,000", po.outstanding_balance == 100000.0)
        check("Initial SupplierInvoice outstanding_amount is 100,000", sinv.outstanding_amount == 100000.0)
    finally:
        db.close()

    # Create & confirm Purchase Return for 20 items (INR 20,000)
    r_create = client.post("/purchase-returns", headers=auth, json={
        "purchase_id": data["purchase_id"],
        "reason": "Defective components",
        "items": [
            {
                "purchase_item_id": data["purchase_item_id"],
                "product_id": data["product_id"],
                "quantity": 20,
                "unit_price": 1000.0,
            }
        ]
    })
    check("Create draft return status 201", r_create.status_code == 201)
    return_id = r_create.json()["id"]

    r_confirm = client.post(f"/purchase-returns/{return_id}/confirm", headers=auth)
    check("Confirm return status 200", r_confirm.status_code == 200)

    # Verify Supplier balance
    db = SessionLocal()
    try:
        sup = db.get(Supplier, data["supplier_id"])
        po = db.get(PurchaseInvoice, data["purchase_id"])
        sinv = db.get(SupplierInvoice, data["supplier_invoice_id"])
        check("Supplier.total_purchases reduced to 80,000", sup.total_purchases == 80000.0)
        check("Supplier.outstanding_payable reduced to 80,000", sup.outstanding_payable == 80000.0)
        check("PurchaseInvoice.return_amount is 20,000", po.return_amount == 20000.0)
        check("PurchaseInvoice.outstanding_balance reduced to 80,000", po.outstanding_balance == 80000.0)
        check("SupplierInvoice.return_amount is 20,000", sinv.return_amount == 20000.0)
        check("SupplierInvoice.outstanding_amount reduced to 80,000", sinv.outstanding_amount == 80000.0)
        check("SupplierInvoice.payment_status is partially_paid", sinv.payment_status == "partially_paid")
    finally:
        db.close()

    # Verify Accounts Payable list endpoint
    r_ap = client.get("/accounts-payable", headers=auth)
    check("GET /accounts-payable status 200", r_ap.status_code == 200)
    ap_items = r_ap.json()["items"]
    check("AP list has 1 item", len(ap_items) == 1)
    check("AP item return_amount is 20,000", ap_items[0]["return_amount"] == 20000.0)
    check("AP item outstanding_amount is 80,000", ap_items[0]["outstanding_amount"] == 80000.0)

    # Verify AP Supplier Statement endpoint
    r_stmt = client.get(f"/accounts-payable/supplier/{data['supplier_id']}", headers=auth)
    check("GET /accounts-payable/supplier status 200", r_stmt.status_code == 200)
    check("Supplier AP statement total_open_payable is 80,000", r_stmt.json()["total_open_payable"] == 80000.0)

    # Verify Supplier Payment Allocation: allocating 80,000 succeeds, allocating 80,001 fails
    r_pay_over = client.post("/supplier-payments", headers=auth, json={
        "supplier_id": data["supplier_id"],
        "amount": 80001.0,
        "payment_method": "bank_transfer",
        "allocations": [
            {
                "supplier_invoice_id": data["supplier_invoice_id"],
                "amount": 80001.0,
            }
        ]
    })
    check("Supplier payment allocation > 80,000 is rejected (HTTP 400)", r_pay_over.status_code == 400)

    r_pay_exact = client.post("/supplier-payments", headers=auth, json={
        "supplier_id": data["supplier_id"],
        "amount": 80000.0,
        "payment_method": "bank_transfer",
        "allocations": [
            {
                "supplier_invoice_id": data["supplier_invoice_id"],
                "amount": 80000.0,
            }
        ]
    })
    check("Supplier payment allocation exactly 80,000 succeeds (HTTP 201)", r_pay_exact.status_code == 201)

    # Verify settled state
    db = SessionLocal()
    try:
        sinv = db.get(SupplierInvoice, data["supplier_invoice_id"])
        check("SupplierInvoice after payment: outstanding_amount is 0.0", sinv.outstanding_amount == 0.0)
        check("SupplierInvoice after payment: payment_status is paid", sinv.payment_status == "paid")
    finally:
        db.close()


def test_partial_payment_scenario():
    print("\n--- Scenario 2: Partial Payment (Purchase INR 100,000, Paid INR 30,000, Return INR 20,000) ---")
    auth, org_id = register_org("Partial Pay PO Test")
    data = setup_test_purchase(auth, org_id, total_qty=100, unit_price=1000.0)

    # 1. Pay INR 30,000 first
    r_pay = client.post("/supplier-payments", headers=auth, json={
        "supplier_id": data["supplier_id"],
        "amount": 30000.0,
        "payment_method": "bank_transfer",
        "allocations": [
            {
                "supplier_invoice_id": data["supplier_invoice_id"],
                "amount": 30000.0,
            }
        ]
    })
    check("Initial payment of 30,000 recorded status 201", r_pay.status_code == 201)

    # Verify state after payment
    db = SessionLocal()
    try:
        sup = db.get(Supplier, data["supplier_id"])
        sinv = db.get(SupplierInvoice, data["supplier_invoice_id"])
        check("Supplier.total_paid is 30,000", sup.total_paid == 30000.0)
        check("Supplier.outstanding_payable is 70,000", sup.outstanding_payable == 70000.0)
        check("SupplierInvoice.amount_paid is 30,000", sinv.amount_paid == 30000.0)
        check("SupplierInvoice.outstanding_amount is 70,000", sinv.outstanding_amount == 70000.0)
    finally:
        db.close()

    # 2. Return INR 20,000 worth of items
    r_create = client.post("/purchase-returns", headers=auth, json={
        "purchase_id": data["purchase_id"],
        "reason": "Damaged in transit",
        "items": [
            {
                "purchase_item_id": data["purchase_item_id"],
                "product_id": data["product_id"],
                "quantity": 20,
                "unit_price": 1000.0,
            }
        ]
    })
    check("Create return for 20,000 status 201", r_create.status_code == 201)
    ret_id = r_create.json()["id"]

    r_confirm = client.post(f"/purchase-returns/{ret_id}/confirm", headers=auth)
    check("Confirm return status 200", r_confirm.status_code == 200)

    # 3. Verify that:
    # - Payment history remains 30,000
    # - Outstanding becomes 50,000
    # - AP outstanding becomes 50,000
    # - Supplier balance becomes 50,000
    db = SessionLocal()
    try:
        sup = db.get(Supplier, data["supplier_id"])
        po = db.get(PurchaseInvoice, data["purchase_id"])
        sinv = db.get(SupplierInvoice, data["supplier_invoice_id"])
        check("Payment history intact: Supplier.total_paid is 30,000", sup.total_paid == 30000.0)
        check("Payment history intact: SupplierInvoice.amount_paid is 30,000", sinv.amount_paid == 30000.0)
        check("Supplier.total_purchases is 80,000", sup.total_purchases == 80000.0)
        check("Supplier.outstanding_payable is 50,000", sup.outstanding_payable == 50000.0)
        check("SupplierInvoice.return_amount is 20,000", sinv.return_amount == 20000.0)
        check("SupplierInvoice.outstanding_amount is 50,000", sinv.outstanding_amount == 50000.0)
        check("SupplierInvoice.payment_status is partially_paid", sinv.payment_status == "partially_paid")
    finally:
        db.close()

    # AP item verification
    r_ap = client.get("/accounts-payable", headers=auth)
    ap_item = r_ap.json()["items"][0]
    check("AP outstanding_amount is 50,000", ap_item["outstanding_amount"] == 50000.0)
    check("AP amount_paid is 30,000", ap_item["amount_paid"] == 30000.0)
    check("AP return_amount is 20,000", ap_item["return_amount"] == 20000.0)


def test_full_payment_and_over_return_edge_cases():
    print("\n--- Scenario 3: Full Payment & Over-Return Edge Cases ---")
    auth, org_id = register_org("Edge Case PO Test")
    data = setup_test_purchase(auth, org_id, total_qty=100, unit_price=1000.0)

    # 1. Pay INR 80,000
    r_pay80 = client.post("/supplier-payments", headers=auth, json={
        "supplier_id": data["supplier_id"],
        "amount": 80000.0,
        "payment_method": "bank_transfer",
        "allocations": [
            {
                "supplier_invoice_id": data["supplier_invoice_id"],
                "amount": 80000.0,
            }
        ]
    })
    check("Pay 80,000 status 201", r_pay80.status_code == 201)

    # 2. Return INR 20,000 (Exactly remaining unpaid balance) -> Must succeed!
    r_ret20 = client.post("/purchase-returns", headers=auth, json={
        "purchase_id": data["purchase_id"],
        "reason": "Excess inventory return",
        "items": [
            {
                "purchase_item_id": data["purchase_item_id"],
                "product_id": data["product_id"],
                "quantity": 20,
                "unit_price": 1000.0,
            }
        ]
    })
    check("Create return for remaining 20,000 status 201", r_ret20.status_code == 201)
    ret_id = r_ret20.json()["id"]
    client.post(f"/purchase-returns/{ret_id}/confirm", headers=auth)

    db = SessionLocal()
    try:
        sinv = db.get(SupplierInvoice, data["supplier_invoice_id"])
        check("SupplierInvoice outstanding is now 0.0", sinv.outstanding_amount == 0.0)
        check("SupplierInvoice payment_status is paid", sinv.payment_status == "paid")
    finally:
        db.close()

    # Open AP must now exclude this settled invoice
    r_ap = client.get("/accounts-payable", headers=auth)
    check("Settled invoice excluded from open AP", len(r_ap.json()["items"]) == 0)

    # 3. Attempting an additional return of 1 item (INR 1,000) when unpaid balance is 0 must be rejected
    r_over_ret = client.post("/purchase-returns", headers=auth, json={
        "purchase_id": data["purchase_id"],
        "reason": "Attempting return on fully settled purchase",
        "items": [
            {
                "purchase_item_id": data["purchase_item_id"],
                "product_id": data["product_id"],
                "quantity": 1,
                "unit_price": 1000.0,
            }
        ]
    })
    check("Return on fully settled invoice rejected (HTTP 400)", r_over_ret.status_code == 400)


def test_cancellation_reversal_scenario():
    print("\n--- Scenario 4: Cancellation & Accounting Reversal ---")
    auth, org_id = register_org("Cancel Reversal PO Test")
    data = setup_test_purchase(auth, org_id, total_qty=100, unit_price=1000.0)

    # 1. Create and confirm return of 20,000
    r_create = client.post("/purchase-returns", headers=auth, json={
        "purchase_id": data["purchase_id"],
        "reason": "Quality rejection",
        "items": [
            {
                "purchase_item_id": data["purchase_item_id"],
                "product_id": data["product_id"],
                "quantity": 20,
                "unit_price": 1000.0,
            }
        ]
    })
    ret_id = r_create.json()["id"]
    client.post(f"/purchase-returns/{ret_id}/confirm", headers=auth)

    # Outstanding is 80,000
    db = SessionLocal()
    try:
        sinv = db.get(SupplierInvoice, data["supplier_invoice_id"])
        sup = db.get(Supplier, data["supplier_id"])
        check("Before cancel: SupplierInvoice outstanding is 80,000", sinv.outstanding_amount == 80000.0)
        check("Before cancel: Supplier outstanding is 80,000", sup.outstanding_payable == 80000.0)
    finally:
        db.close()

    # 2. Cancel return
    r_cancel = client.post(f"/purchase-returns/{ret_id}/cancel", headers=auth, json={"reason": "Supplier rectified issue"})
    check("Cancel return status 200", r_cancel.status_code == 200)

    # 3. Verify full restoration to 100,000
    db = SessionLocal()
    try:
        sinv = db.get(SupplierInvoice, data["supplier_invoice_id"])
        po = db.get(PurchaseInvoice, data["purchase_id"])
        sup = db.get(Supplier, data["supplier_id"])
        check("After cancel: Supplier.total_purchases restored to 100,000", sup.total_purchases == 100000.0)
        check("After cancel: Supplier.outstanding_payable restored to 100,000", sup.outstanding_payable == 100000.0)
        check("After cancel: PurchaseInvoice.return_amount restored to 0.0", po.return_amount == 0.0)
        check("After cancel: PurchaseInvoice.outstanding_balance restored to 100,000", po.outstanding_balance == 100000.0)
        check("After cancel: SupplierInvoice.return_amount restored to 0.0", sinv.return_amount == 0.0)
        check("After cancel: SupplierInvoice.outstanding_amount restored to 100,000", sinv.outstanding_amount == 100000.0)
        check("After cancel: SupplierInvoice.payment_status restored to unpaid", sinv.payment_status == "unpaid")
    finally:
        db.close()

    # 4. Repeated cancel attempt rejected
    r_cancel_again = client.post(f"/purchase-returns/{ret_id}/cancel", headers=auth, json={"reason": "Repeat"})
    check("Repeated cancel rejected (HTTP 400)", r_cancel_again.status_code == 400)


def test_idempotency_and_tenant_isolation():
    print("\n--- Scenario 5: Idempotency & Tenant Isolation ---")
    auth_a, org_a = register_org("Org A Firm")
    auth_b, org_b = register_org("Org B Firm")

    data_a = setup_test_purchase(auth_a, org_a, total_qty=50, unit_price=1000.0)
    data_b = setup_test_purchase(auth_b, org_b, total_qty=50, unit_price=1000.0)

    # Cross-tenant return creation attempt
    r_cross = client.post("/purchase-returns", headers=auth_b, json={
        "purchase_id": data_a["purchase_id"],
        "reason": "Cross tenant attempt",
        "items": [
            {
                "purchase_item_id": data_a["purchase_item_id"],
                "product_id": data_a["product_id"],
                "quantity": 5,
                "unit_price": 1000.0,
            }
        ]
    })
    check("Cross-tenant return creation rejected (HTTP 404)", r_cross.status_code == 404)

    # Valid return in Org A
    r_ret = client.post("/purchase-returns", headers=auth_a, json={
        "purchase_id": data_a["purchase_id"],
        "reason": "Org A return",
        "items": [
            {
                "purchase_item_id": data_a["purchase_item_id"],
                "product_id": data_a["product_id"],
                "quantity": 10,
                "unit_price": 1000.0,
            }
        ]
    })
    ret_id = r_ret.json()["id"]

    # Cross-tenant confirm attempt
    r_cross_confirm = client.post(f"/purchase-returns/{ret_id}/confirm", headers=auth_b)
    check("Cross-tenant confirm rejected (HTTP 404)", r_cross_confirm.status_code == 404)

    # Confirm in Org A
    client.post(f"/purchase-returns/{ret_id}/confirm", headers=auth_a)

    # Repeated confirm attempt rejected
    r_repeat_confirm = client.post(f"/purchase-returns/{ret_id}/confirm", headers=auth_a)
    check("Repeated confirm rejected (HTTP 400)", r_repeat_confirm.status_code == 400)

    # Verify Org B remains completely unaffected
    db = SessionLocal()
    try:
        sup_b = db.get(Supplier, data_b["supplier_id"])
        sinv_b = db.get(SupplierInvoice, data_b["supplier_invoice_id"])
        check("Org B Supplier total_purchases untouched (50,000)", sup_b.total_purchases == 50000.0)
        check("Org B SupplierInvoice outstanding untouched (50,000)", sinv_b.outstanding_amount == 50000.0)
    finally:
        db.close()


if __name__ == "__main__":
    print("\n=======================================================")
    print("TEST SUITE: Purchase Returns Accounting Consistency")
    print("=======================================================")
    test_basic_unpaid_return_scenario()
    test_partial_payment_scenario()
    test_full_payment_and_over_return_edge_cases()
    test_cancellation_reversal_scenario()
    test_idempotency_and_tenant_isolation()
    print("\n=======================================================")
    print(f"RESULTS: {passed_count} PASSED, {failed_count} FAILED")
    print("=======================================================")
    if failed_count > 0:
        sys.exit(1)
