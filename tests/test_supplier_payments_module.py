"""Focused automated test suite for Supplier Payments Module (Allocations, Reversals, & Reconciliation)."""

import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from app.main import app
from app.core.database import SessionLocal
from app.models.product import Product
from app.models.purchase_invoice import PurchaseInvoice
from app.models.supplier import Supplier, SupplierPayment
from app.models.supplier_invoice import SupplierInvoice
from app.models.stock_movement import StockMovement
from app.models.user import User
from app.models.warehouse import Warehouse

client = TestClient(app)

passed = 0
failed = 0


def check(description: str, condition: bool):
    global passed, failed
    if condition:
        print(f"  PASS  {description}")
        passed += 1
    else:
        print(f"  FAIL  {description}")
        failed += 1


def register_org(name_prefix: str = "Payment Firm"):
    email = f"spay_admin_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post(
        "/auth/register",
        json={
            "organization_name": f"{name_prefix} {uuid.uuid4().hex[:6]}",
            "admin_name": "SPAY Admin",
            "email": email,
            "password": "Password123!",
            "role": "admin",
        },
    )
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


def setup_procurement_env(auth: dict, org_id: str):
    db = SessionLocal()
    try:
        wh = Warehouse(organization_id=org_id, name="SPAY Warehouse", code=f"WH-{uuid.uuid4().hex[:4]}")
        db.add(wh)
        sup = Supplier(organization_id=org_id, name=f"Supplier {uuid.uuid4().hex[:4]}", is_active=True)
        db.add(sup)
        prod = Product(organization_id=org_id, name="Widget SPAY", sku=f"SKU-{uuid.uuid4().hex[:4]}", price=100.0, total_inventory=0)
        db.add(prod)
        db.commit()
        db.refresh(wh)
        db.refresh(sup)
        db.refresh(prod)
        return wh.id, sup.id, prod.id
    finally:
        db.close()


def create_recorded_invoice(auth: dict, sup_id: str, wh_id: str, prod_id: str, inv_num: str, billed_qty: int = 10, unit_price: float = 100.0) -> str:
    r_pur = client.post(
        "/purchases",
        headers=auth,
        json={
            "invoice_number": f"PO-{uuid.uuid4().hex[:6]}",
            "supplier_id": sup_id,
            "warehouse_id": wh_id,
            "items": [{"product_id": prod_id, "ordered_qty": billed_qty, "purchase_price": 100.0}],
        },
    ).json()
    pur_id = r_pur["id"]
    pur_item_id = r_pur["items"][0]["id"]
    client.post(f"/purchases/{pur_id}/confirm", headers=auth)

    r_grn = client.post(
        "/grns",
        headers=auth,
        json={
            "purchase_id": pur_id,
            "supplier_id": sup_id,
            "warehouse_id": wh_id,
            "items": [{"purchase_item_id": pur_item_id, "product_id": prod_id, "received_qty": billed_qty}],
        },
    ).json()
    client.post(f"/grns/{r_grn['id']}/confirm", headers=auth)

    r_sinv = client.post(
        "/supplier-invoices",
        headers=auth,
        json={
            "supplier_id": sup_id,
            "purchase_id": pur_id,
            "supplier_invoice_number": inv_num,
            "items": [{"purchase_item_id": pur_item_id, "billed_qty": billed_qty, "unit_price": unit_price}],
        },
    ).json()
    client.post(f"/supplier-invoices/{r_sinv['id']}/record", headers=auth)
    return r_sinv["id"]


print("\n=======================================================")
print("TEST SUITE: Supplier Payments Architecture & Reconciliation")
print("=======================================================\n")

auth, org_id = register_org()
wh_id, sup_id, prod_id = setup_procurement_env(auth, org_id)

# -------------------------------------------------------------
# Scenario 1 — Single Invoice Full Payment
# -------------------------------------------------------------
print("--- Scenario 1: Single Invoice Full Payment ---")
inv1_id = create_recorded_invoice(auth, sup_id, wh_id, prod_id, "INV-SPAY-001", billed_qty=10, unit_price=100.0)

r_pay1 = client.post(
    "/supplier-payments",
    headers=auth,
    json={
        "supplier_id": sup_id,
        "amount": 1000.0,
        "payment_method": "bank_transfer",
        "reference": "TXN-001",
        "allocations": [{"supplier_invoice_id": inv1_id, "amount": 1000.0}],
    },
)
check("Record full payment returns HTTP 201", r_pay1.status_code == 201)
pay1_data = r_pay1.json()
check("Payment number generated (SPAY-...)", pay1_data["payment_number"].startswith("SPAY-"))
check("Allocated amount is 1000.0", pay1_data["allocated_amount"] == 1000.0)
check("Unallocated amount is 0.0", pay1_data["unallocated_amount"] == 0.0)

# Verify Invoice state
r_inv1 = client.get(f"/supplier-invoices/{inv1_id}", headers=auth).json()
check("Invoice amount_paid is 1000.0", r_inv1["amount_paid"] == 1000.0)
check("Invoice outstanding_amount is 0.0", r_inv1["outstanding_amount"] == 0.0)
check("Invoice payment_status is paid", r_inv1["payment_status"] == "paid")

# Verify AP excludes fully paid invoice
r_ap1 = client.get("/accounts-payable", headers=auth).json()
check("Open AP excludes fully paid invoice", all(i["supplier_invoice_id"] != inv1_id for i in r_ap1["items"]))

# -------------------------------------------------------------
# Scenario 2 — Partial Payment
# -------------------------------------------------------------
print("\n--- Scenario 2: Partial Payment ---")
inv2_id = create_recorded_invoice(auth, sup_id, wh_id, prod_id, "INV-SPAY-002", billed_qty=10, unit_price=100.0)

r_pay2 = client.post(
    "/supplier-payments",
    headers=auth,
    json={
        "supplier_id": sup_id,
        "amount": 400.0,
        "payment_method": "upi",
        "allocations": [{"supplier_invoice_id": inv2_id, "amount": 400.0}],
    },
)
check("Record partial payment returns HTTP 201", r_pay2.status_code == 201)

r_inv2 = client.get(f"/supplier-invoices/{inv2_id}", headers=auth).json()
check("Invoice amount_paid is 400.0", r_inv2["amount_paid"] == 400.0)
check("Invoice outstanding_amount is 600.0", r_inv2["outstanding_amount"] == 600.0)
check("Invoice payment_status is partially_paid", r_inv2["payment_status"] == "partially_paid")

# -------------------------------------------------------------
# Scenario 3 — Multi-Invoice Payment
# -------------------------------------------------------------
print("\n--- Scenario 3: Multi-Invoice Payment ---")
inv3a_id = create_recorded_invoice(auth, sup_id, wh_id, prod_id, "INV-SPAY-003A", billed_qty=6, unit_price=100.0)  # 600.0
inv3b_id = create_recorded_invoice(auth, sup_id, wh_id, prod_id, "INV-SPAY-003B", billed_qty=7, unit_price=100.0)  # 700.0

r_pay3 = client.post(
    "/supplier-payments",
    headers=auth,
    json={
        "supplier_id": sup_id,
        "amount": 1000.0,
        "payment_method": "cheque",
        "allocations": [
            {"supplier_invoice_id": inv3a_id, "amount": 600.0},
            {"supplier_invoice_id": inv3b_id, "amount": 400.0},
        ],
    },
)
check("Multi-invoice payment returns HTTP 201", r_pay3.status_code == 201)
pay3_data = r_pay3.json()
check("Multi-invoice payment contains 2 allocations", len(pay3_data["allocations"]) == 2)

r_inv3a = client.get(f"/supplier-invoices/{inv3a_id}", headers=auth).json()
r_inv3b = client.get(f"/supplier-invoices/{inv3b_id}", headers=auth).json()
check("Invoice 3A is fully paid (600.0)", r_inv3a["payment_status"] == "paid" and r_inv3a["outstanding_amount"] == 0.0)
check("Invoice 3B is partially paid (400.0 paid, 300.0 outstanding)", r_inv3b["payment_status"] == "partially_paid" and r_inv3b["outstanding_amount"] == 300.0)

# -------------------------------------------------------------
# Scenario 4 — Unallocated Advance Payment
# -------------------------------------------------------------
print("\n--- Scenario 4: Unallocated Advance Payment ---")
inv4_id = create_recorded_invoice(auth, sup_id, wh_id, prod_id, "INV-SPAY-004", billed_qty=10, unit_price=100.0)  # 1000.0

r_pay4 = client.post(
    "/supplier-payments",
    headers=auth,
    json={
        "supplier_id": sup_id,
        "amount": 1000.0,
        "payment_method": "bank_transfer",
        "allocations": [{"supplier_invoice_id": inv4_id, "amount": 600.0}],
    },
)
check("Partial allocation payment returns HTTP 201", r_pay4.status_code == 201)
pay4_data = r_pay4.json()
check("Allocated amount is 600.0", pay4_data["allocated_amount"] == 600.0)
check("Unallocated advance remainder is 400.0", pay4_data["unallocated_amount"] == 400.0)

# -------------------------------------------------------------
# Scenario 5 — Over-Allocate Payment Blocked
# -------------------------------------------------------------
print("\n--- Scenario 5: Over-Allocate Payment Blocked ---")
r_pay5 = client.post(
    "/supplier-payments",
    headers=auth,
    json={
        "supplier_id": sup_id,
        "amount": 500.0,
        "payment_method": "cash",
        "allocations": [{"supplier_invoice_id": inv4_id, "amount": 600.0}],
    },
)
check("Allocations total exceeding payment amount rejected (HTTP 400)", r_pay5.status_code == 400)

# -------------------------------------------------------------
# Scenario 6 — Overpay Invoice Blocked
# -------------------------------------------------------------
print("\n--- Scenario 6: Overpay Invoice Blocked ---")
# inv4 has 400.0 outstanding remaining
r_pay6 = client.post(
    "/supplier-payments",
    headers=auth,
    json={
        "supplier_id": sup_id,
        "amount": 500.0,
        "payment_method": "cash",
        "allocations": [{"supplier_invoice_id": inv4_id, "amount": 500.0}],
    },
)
check("Allocation exceeding invoice outstanding rejected (HTTP 400)", r_pay6.status_code == 400)

# -------------------------------------------------------------
# Scenario 7 — Wrong Supplier Invoice Allocation Blocked
# -------------------------------------------------------------
print("\n--- Scenario 7: Wrong Supplier Invoice Allocation Blocked ---")
auth_b, org_b_id = register_org("Firm B SPAY")
wh_b_id, sup_b_id, prod_b_id = setup_procurement_env(auth_b, org_b_id)
inv_b_id = create_recorded_invoice(auth_b, sup_b_id, wh_b_id, prod_b_id, "INV-FIRM-B", billed_qty=5, unit_price=100.0)

# Try allocating Supplier A payment to Supplier B invoice
r_pay7 = client.post(
    "/supplier-payments",
    headers=auth,
    json={
        "supplier_id": sup_id,
        "amount": 500.0,
        "payment_method": "cash",
        "allocations": [{"supplier_invoice_id": inv_b_id, "amount": 500.0}],
    },
)
check("Allocation to non-existent/cross-tenant invoice rejected (HTTP 400)", r_pay7.status_code == 400)

# -------------------------------------------------------------
# Scenario 8 — Allocation to Cancelled Invoice Blocked
# -------------------------------------------------------------
print("\n--- Scenario 8: Allocation to Cancelled Invoice Blocked ---")
r_pur_can = client.post(
    "/purchases",
    headers=auth,
    json={
        "invoice_number": f"PO-{uuid.uuid4().hex[:6]}",
        "supplier_id": sup_id,
        "warehouse_id": wh_id,
        "items": [{"product_id": prod_id, "ordered_qty": 5, "purchase_price": 100.0}],
    },
).json()
pur_can_id = r_pur_can["id"]
pur_can_item_id = r_pur_can["items"][0]["id"]
client.post(f"/purchases/{pur_can_id}/confirm", headers=auth)

r_grn_can = client.post(
    "/grns",
    headers=auth,
    json={
        "purchase_id": pur_can_id,
        "supplier_id": sup_id,
        "warehouse_id": wh_id,
        "items": [{"purchase_item_id": pur_can_item_id, "product_id": prod_id, "received_qty": 5}],
    },
).json()
client.post(f"/grns/{r_grn_can['id']}/confirm", headers=auth)

r_inv_can = client.post(
    "/supplier-invoices",
    headers=auth,
    json={
        "supplier_id": sup_id,
        "purchase_id": pur_can_id,
        "supplier_invoice_number": "INV-CANCELLED-TEST",
        "items": [{"purchase_item_id": pur_can_item_id, "billed_qty": 5, "unit_price": 100.0}],
    },
).json()
client.post(f"/supplier-invoices/{r_inv_can['id']}/cancel", headers=auth)

r_pay8 = client.post(
    "/supplier-payments",
    headers=auth,
    json={
        "supplier_id": sup_id,
        "amount": 500.0,
        "payment_method": "cash",
        "allocations": [{"supplier_invoice_id": r_inv_can["id"], "amount": 500.0}],
    },
)
check("Allocation to cancelled invoice rejected (HTTP 400)", r_pay8.status_code == 400)

# -------------------------------------------------------------
# Scenario 9 — Void Partial Payment & Reconciliation
# -------------------------------------------------------------
print("\n--- Scenario 9: Void Partial Payment & Reconciliation ---")
inv9_id = create_recorded_invoice(auth, sup_id, wh_id, prod_id, "INV-SPAY-009", billed_qty=10, unit_price=100.0)  # 1000.0
r_pay9 = client.post(
    "/supplier-payments",
    headers=auth,
    json={
        "supplier_id": sup_id,
        "amount": 400.0,
        "payment_method": "cash",
        "allocations": [{"supplier_invoice_id": inv9_id, "amount": 400.0}],
    },
).json()

# Void payment 9
r_void9 = client.post(
    f"/supplier-payments/{r_pay9['id']}/void",
    headers=auth,
    json={"reason": "Incorrect payment entry"},
)
check("Void payment returns HTTP 200", r_void9.status_code == 200)
check("Payment status is voided", r_void9.json()["status"] == "voided")

# Attempting to void the payment a second time
r_void9_second = client.post(
    f"/supplier-payments/{r_pay9['id']}/void",
    headers=auth,
    json={"reason": "Second void attempt"},
)
check("Voiding an already voided payment is rejected (HTTP 400)", r_void9_second.status_code == 400)

r_inv9 = client.get(f"/supplier-invoices/{inv9_id}", headers=auth).json()
check("Invoice amount_paid restored to 0.0", r_inv9["amount_paid"] == 0.0)
check("Invoice outstanding_amount restored to 1000.0", r_inv9["outstanding_amount"] == 1000.0)
check("Invoice payment_status restored to unpaid", r_inv9["payment_status"] == "unpaid")

r_ap9 = client.get("/accounts-payable", headers=auth).json()
check("Restored invoice returns to open AP list", any(i["supplier_invoice_id"] == inv9_id for i in r_ap9["items"]))

# -------------------------------------------------------------
# Scenario 10 — Void Multi-Invoice Payment
# -------------------------------------------------------------
print("\n--- Scenario 10: Void Multi-Invoice Payment ---")
# Void payment 3 (multi-invoice payment for 3A and 3B)
r_void3 = client.post(
    f"/supplier-payments/{pay3_data['id']}/void",
    headers=auth,
    json={"reason": "Check bounced"},
)
check("Void multi-invoice payment returns HTTP 200", r_void3.status_code == 200)

r_inv3a_v = client.get(f"/supplier-invoices/{inv3a_id}", headers=auth).json()
r_inv3b_v = client.get(f"/supplier-invoices/{inv3b_id}", headers=auth).json()
check("Invoice 3A restored to unpaid (600.0 outstanding)", r_inv3a_v["payment_status"] == "unpaid" and r_inv3a_v["outstanding_amount"] == 600.0)
check("Invoice 3B restored to unpaid (700.0 outstanding)", r_inv3b_v["payment_status"] == "unpaid" and r_inv3b_v["outstanding_amount"] == 700.0)

# -------------------------------------------------------------
# Scenario 11 — Invoice Payment History Endpoint
# -------------------------------------------------------------
print("\n--- Scenario 11: Invoice Payment History ---")
r_hist = client.get(f"/supplier-invoices/{inv2_id}/payments", headers=auth)
check("GET /supplier-invoices/{id}/payments returns HTTP 200", r_hist.status_code == 200)
hist_data = r_hist.json()
check("Payment history contains 1 allocation", len(hist_data) == 1)
check("Allocation amount is 400.0", hist_data[0]["allocated_amount"] == 400.0)

# -------------------------------------------------------------
# Scenario 12 — Paid Invoice Cancellation Blocked
# -------------------------------------------------------------
print("\n--- Scenario 12: Paid Invoice Cancellation Blocked ---")
# inv2 has amount_paid = 400.0
r_can_paid = client.post(f"/supplier-invoices/{inv2_id}/cancel", headers=auth)
check("Cancelling invoice with amount_paid > 0 rejected (HTTP 400)", r_can_paid.status_code == 400)

# -------------------------------------------------------------
# Scenario 13 — Payment to Mismatched Invoice Allowed
# -------------------------------------------------------------
print("\n--- Scenario 13: Payment to Mismatched Invoice Allowed ---")
r_pur_mis = client.post(
    "/purchases",
    headers=auth,
    json={
        "invoice_number": f"PO-{uuid.uuid4().hex[:6]}",
        "supplier_id": sup_id,
        "warehouse_id": wh_id,
        "items": [{"product_id": prod_id, "ordered_qty": 5, "purchase_price": 100.0}],
    },
).json()
pur_mis_id = r_pur_mis["id"]
pur_mis_item_id = r_pur_mis["items"][0]["id"]
client.post(f"/purchases/{pur_mis_id}/confirm", headers=auth)

r_grn_mis = client.post(
    "/grns",
    headers=auth,
    json={
        "purchase_id": pur_mis_id,
        "supplier_id": sup_id,
        "warehouse_id": wh_id,
        "items": [{"purchase_item_id": pur_mis_item_id, "product_id": prod_id, "received_qty": 5}],
    },
).json()
client.post(f"/grns/{r_grn_mis['id']}/confirm", headers=auth)

r_inv_mis = client.post(
    "/supplier-invoices",
    headers=auth,
    json={
        "supplier_id": sup_id,
        "purchase_id": pur_mis_id,
        "supplier_invoice_number": "INV-MISMATCH-PAY",
        "items": [{"purchase_item_id": pur_mis_item_id, "billed_qty": 5, "unit_price": 120.0}],  # Price mismatch
    },
).json()
client.post(f"/supplier-invoices/{r_inv_mis['id']}/record", headers=auth)

r_pay_mis = client.post(
    "/supplier-payments",
    headers=auth,
    json={
        "supplier_id": sup_id,
        "amount": 600.0,
        "payment_method": "upi",
        "allocations": [{"supplier_invoice_id": r_inv_mis["id"], "amount": 600.0}],
    },
)
check("Payment to mismatched invoice returns HTTP 201", r_pay_mis.status_code == 201)
r_inv_mis_after = client.get(f"/supplier-invoices/{r_inv_mis['id']}", headers=auth).json()
check("Mismatched invoice status is recorded and payment_status is paid", r_inv_mis_after["status"] == "recorded" and r_inv_mis_after["verification_status"] == "mismatched" and r_inv_mis_after["payment_status"] == "paid")

# -------------------------------------------------------------
# Scenario 14 — Cross-Tenant Guard
# -------------------------------------------------------------
print("\n--- Scenario 14: Cross-Tenant Guard ---")
r_cross_pay = client.get(f"/supplier-payments/{pay1_data['id']}", headers=auth_b)
check("Cross-tenant payment detail read rejected (HTTP 404)", r_cross_pay.status_code == 404)

r_cross_list = client.get(f"/supplier-payments?supplier_id={sup_id}", headers=auth_b).json()
check("Cross-tenant payment list returns 0 items", len(r_cross_list["items"]) == 0)

# -------------------------------------------------------------
# Scenario 15 — Zero Inventory Side Effects
# -------------------------------------------------------------
print("\n--- Scenario 15: Zero Inventory Side Effects ---")
db = SessionLocal()
try:
    moves_before = len(db.query(StockMovement).filter(StockMovement.organization_id == org_id).all())
finally:
    db.close()

# Record & Void a supplier payment
inv15_id = create_recorded_invoice(auth, sup_id, wh_id, prod_id, "INV-SPAY-015", billed_qty=5, unit_price=100.0)
r_p15 = client.post(
    "/supplier-payments",
    headers=auth,
    json={
        "supplier_id": sup_id,
        "amount": 500.0,
        "payment_method": "cash",
        "allocations": [{"supplier_invoice_id": inv15_id, "amount": 500.0}],
    },
).json()
client.post(f"/supplier-payments/{r_p15['id']}/void", headers=auth, json={"reason": "Test void"})

db = SessionLocal()
try:
    moves_after = len(db.query(StockMovement).filter(StockMovement.organization_id == org_id).all())
    # Note: create_recorded_invoice creates 1 GRN movement. The payment and void must add 0 movements.
    check("Supplier payment recording and voiding produce ZERO StockMovement rows", moves_after == moves_before + 1)
finally:
    db.close()

# -------------------------------------------------------------
# Scenario 16 — Legacy Supplier Payment Endpoint Compatibility
# -------------------------------------------------------------
print("\n--- Scenario 16: Legacy Supplier Payment Endpoint Compatibility ---")
r_leg_post = client.post(
    f"/suppliers/{sup_id}/payments",
    headers=auth,
    json={"amount": 250.0, "payment_mode": "cash", "note": "Legacy payment"},
)
check("Legacy POST /suppliers/{id}/payments returns HTTP 201", r_leg_post.status_code == 201)

r_leg_list = client.get(f"/suppliers/{sup_id}/payments", headers=auth)
check("Legacy GET /suppliers/{id}/payments returns HTTP 200", r_leg_list.status_code == 200)

leg_payments = r_leg_list.json()
leg_pay_id = leg_payments[0]["id"]

r_leg_del = client.delete(f"/suppliers/{sup_id}/payments/{leg_pay_id}", headers=auth)
check("Legacy DELETE /suppliers/{id}/payments/{id} returns HTTP 200 (voided safely)", r_leg_del.status_code == 200)

# -------------------------------------------------------------
# Scenario 17 — Competing Payments Allocation & Locked Balance Protection
# -------------------------------------------------------------
print("\n--- Scenario 17: Competing Payments Allocation & Locked Balance ---")
inv17_id = create_recorded_invoice(auth, sup_id, wh_id, prod_id, "INV-CONCURRENCY-10K", billed_qty=10, unit_price=100.0)  # 1000.0

# Payment A allocates 1000.0
r_pay_a = client.post(
    "/supplier-payments",
    headers=auth,
    json={
        "supplier_id": sup_id,
        "amount": 1000.0,
        "payment_method": "bank_transfer",
        "allocations": [{"supplier_invoice_id": inv17_id, "amount": 1000.0}],
    },
)
check("Payment A allocates full 1000.0 (HTTP 201)", r_pay_a.status_code == 201)

# Competing Payment B attempts allocating 1000.0 on same settled invoice
r_pay_b = client.post(
    "/supplier-payments",
    headers=auth,
    json={
        "supplier_id": sup_id,
        "amount": 1000.0,
        "payment_method": "bank_transfer",
        "allocations": [{"supplier_invoice_id": inv17_id, "amount": 1000.0}],
    },
)
check("Competing Payment B attempting over-allocation on settled invoice is rejected (HTTP 400)", r_pay_b.status_code == 400)

r_inv17 = client.get(f"/supplier-invoices/{inv17_id}", headers=auth).json()
check("Invoice 17 total amount_paid equals grand_total (1000.0)", r_inv17["amount_paid"] == 1000.0)
check("Invoice 17 outstanding_amount is 0.0", r_inv17["outstanding_amount"] == 0.0)

# -------------------------------------------------------------
# Scenario 18 — Multi-Invoice Failure Transaction Rollback
# -------------------------------------------------------------
print("\n--- Scenario 18: Multi-Invoice Failure Transaction Rollback ---")
inv18a_id = create_recorded_invoice(auth, sup_id, wh_id, prod_id, "INV-18A", billed_qty=4, unit_price=100.0)  # 400.0
inv18b_id = create_recorded_invoice(auth, sup_id, wh_id, prod_id, "INV-18B", billed_qty=6, unit_price=100.0)  # 600.0
inv18c_id = create_recorded_invoice(auth, sup_id, wh_id, prod_id, "INV-18C", billed_qty=3, unit_price=100.0)  # 300.0

db = SessionLocal()
try:
    sup_paid_before = db.get(Supplier, sup_id).total_paid
finally:
    db.close()

# Request: Inv A = 400 (valid), Inv B = 600 (valid), Inv C = 400 (invalid > 300 outstanding)
r_pay18 = client.post(
    "/supplier-payments",
    headers=auth,
    json={
        "supplier_id": sup_id,
        "amount": 1000.0,
        "payment_method": "bank_transfer",
        "allocations": [
            {"supplier_invoice_id": inv18a_id, "amount": 400.0},
            {"supplier_invoice_id": inv18b_id, "amount": 600.0},
            {"supplier_invoice_id": inv18c_id, "amount": 400.0},  # invalid
        ],
    },
)
check("Multi-invoice payment with invalid trailing allocation rejected (HTTP 400)", r_pay18.status_code == 400)

# Verify complete transactional rollback
r_inv18a = client.get(f"/supplier-invoices/{inv18a_id}", headers=auth).json()
r_inv18b = client.get(f"/supplier-invoices/{inv18b_id}", headers=auth).json()
r_inv18c = client.get(f"/supplier-invoices/{inv18c_id}", headers=auth).json()

check("Invoice 18A amount_paid remains 0.0 (no partial commit)", r_inv18a["amount_paid"] == 0.0)
check("Invoice 18A outstanding_amount remains 400.0", r_inv18a["outstanding_amount"] == 400.0)
check("Invoice 18B amount_paid remains 0.0 (no partial commit)", r_inv18b["amount_paid"] == 0.0)
check("Invoice 18B outstanding_amount remains 600.0", r_inv18b["outstanding_amount"] == 600.0)
check("Invoice 18C amount_paid remains 0.0", r_inv18c["amount_paid"] == 0.0)
check("Invoice 18C outstanding_amount remains 300.0", r_inv18c["outstanding_amount"] == 300.0)

db = SessionLocal()
try:
    sup_paid_after = db.get(Supplier, sup_id).total_paid
    check("Supplier.total_paid remains completely unchanged after rollback", sup_paid_after == sup_paid_before)
finally:
    db.close()

# -------------------------------------------------------------
# Scenario 19 — Void Failure Transaction Atomicity
# -------------------------------------------------------------
print("\n--- Scenario 19: Void Failure Transaction Atomicity ---")
inv19_id = create_recorded_invoice(auth, sup_id, wh_id, prod_id, "INV-19-TEST", billed_qty=5, unit_price=100.0)  # 500.0
r_pay19 = client.post(
    "/supplier-payments",
    headers=auth,
    json={
        "supplier_id": sup_id,
        "amount": 500.0,
        "payment_method": "cash",
        "allocations": [{"supplier_invoice_id": inv19_id, "amount": 500.0}],
    },
).json()

# Void request without mandatory reason parameter (validation failure)
r_void19_fail = client.post(
    f"/supplier-payments/{r_pay19['id']}/void",
    headers=auth,
    json={"reason": ""},  # empty reason fails min_length validation
)
check("Void attempt with invalid payload rejected (HTTP 422)", r_void19_fail.status_code == 422)

# Verify payment and invoice remain in recorded/paid state
r_pay19_chk = client.get(f"/supplier-payments/{r_pay19['id']}", headers=auth).json()
r_inv19_chk = client.get(f"/supplier-invoices/{inv19_id}", headers=auth).json()
check("Payment status remains recorded after failed void attempt", r_pay19_chk["status"] == "recorded")
check("Invoice payment_status remains paid after failed void attempt", r_inv19_chk["payment_status"] == "paid")
check("Invoice amount_paid remains 500.0", r_inv19_chk["amount_paid"] == 500.0)

print("\n=======================================================")
print(f"SUPPLIER PAYMENTS TEST RESULTS: Passed={passed}, Failed={failed}")
print("=======================================================\n")

if failed > 0:
    sys.exit(1)
