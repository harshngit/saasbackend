"""Focused automated test suite for Supplier Invoice / Vendor Bill Module."""

import os
import sys
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from app.main import app
from app.core.database import SessionLocal
from app.models.product import Product
from app.models.purchase_invoice import PurchaseInvoice, PurchaseInvoiceItem
from app.models.supplier import Supplier
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


def register_org(name_prefix: str = "Vendor Firm"):
    email = f"supp_inv_admin_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post(
        "/auth/register",
        json={
            "organization_name": f"{name_prefix} {uuid.uuid4().hex[:6]}",
            "admin_name": "Invoice Admin",
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
        wh = Warehouse(organization_id=org_id, name="Central Warehouse", code=f"WH-{uuid.uuid4().hex[:4]}")
        db.add(wh)
        sup = Supplier(organization_id=org_id, name=f"Supplier {uuid.uuid4().hex[:4]}", is_active=True)
        db.add(sup)
        prod = Product(organization_id=org_id, name="Widget X", sku=f"SKU-{uuid.uuid4().hex[:4]}", price=100.0, total_inventory=0)
        db.add(prod)
        db.commit()
        db.refresh(wh)
        db.refresh(sup)
        db.refresh(prod)
        return wh.id, sup.id, prod.id
    finally:
        db.close()


print("\n=======================================================")
print("TEST SUITE: Supplier Invoice / Vendor Bill Module")
print("=======================================================\n")

auth, org_id = register_org()
wh_id, sup_id, prod_id = setup_procurement_env(auth, org_id)

# -------------------------------------------------------------
# Test A — Terminology & Model Separation
# -------------------------------------------------------------
print("--- Test A: Terminology & Model Separation ---")
db = SessionLocal()
try:
    check("PurchaseInvoice table is purchase_invoices", PurchaseInvoice.__tablename__ == "purchase_invoices")
    check("SupplierInvoice table is supplier_invoices", SupplierInvoice.__tablename__ == "supplier_invoices")
finally:
    db.close()

# -------------------------------------------------------------
# Setup Baseline Purchase Order (ordered_qty = 10 @ $50.0)
# -------------------------------------------------------------
r_pur_draft = client.post(
    "/purchases",
    headers=auth,
    json={
        "invoice_number": f"PO-{uuid.uuid4().hex[:6]}",
        "supplier_id": sup_id,
        "warehouse_id": wh_id,
        "items": [{"product_id": prod_id, "ordered_qty": 10, "purchase_price": 50.0}],
    },
)
assert r_pur_draft.status_code == 201
pur = r_pur_draft.json()
pur_id = pur["id"]
pur_item_id = pur["items"][0]["id"]

# -------------------------------------------------------------
# Test J — Invoice against Draft Purchase Blocked
# -------------------------------------------------------------
print("\n--- Test J: Invoice Against Draft Purchase Blocked ---")
r_inv_draft_pur = client.post(
    "/supplier-invoices",
    headers=auth,
    json={
        "supplier_id": sup_id,
        "purchase_id": pur_id,
        "supplier_invoice_number": "INV-DRAFT-001",
        "items": [{"purchase_item_id": pur_item_id, "billed_qty": 10, "unit_price": 50.0}],
    },
)
check("Draft Purchase invoice creation rejected (HTTP 400)", r_inv_draft_pur.status_code == 400)

# Confirm Purchase
client.post(f"/purchases/{pur_id}/confirm", headers=auth)

# -------------------------------------------------------------
# Test B — Draft Supplier Invoice Creation & Zero Stock Movement
# -------------------------------------------------------------
print("\n--- Test B: Draft Supplier Invoice Creation ---")
r_inv_create = client.post(
    "/supplier-invoices",
    headers=auth,
    json={
        "supplier_id": sup_id,
        "purchase_id": pur_id,
        "supplier_invoice_number": "VEND-INV-1001",
        "items": [{"purchase_item_id": pur_item_id, "billed_qty": 5, "unit_price": 50.0}],
    },
)
check("Create Supplier Invoice returns HTTP 201", r_inv_create.status_code == 201)
sinv = r_inv_create.json()
sinv_id = sinv["id"]
check("Status defaults to draft", sinv["status"] == "draft")
check("Verification status defaults to pending", sinv["verification_status"] == "pending")
check("Payment status defaults to unpaid", sinv["payment_status"] == "unpaid")
check("Subtotal calculated correctly: 250.0", sinv["subtotal"] == 250.0)

# Verify zero stock movement
db = SessionLocal()
try:
    moves = db.query(StockMovement).filter(StockMovement.organization_id == org_id).all()
    check("Zero StockMovement rows on draft invoice creation", len(moves) == 0)
finally:
    db.close()

# -------------------------------------------------------------
# Test L & M — Invoice against Unconfirmed / Cancelled GRNs
# -------------------------------------------------------------
print("\n--- Test L & M: Invoice Against Unconfirmed / Cancelled GRN ---")
# Create Draft GRN
r_grn_draft = client.post(
    "/grns",
    headers=auth,
    json={
        "purchase_id": pur_id,
        "supplier_id": sup_id,
        "warehouse_id": wh_id,
        "items": [{"purchase_item_id": pur_item_id, "product_id": prod_id, "received_qty": 10}],
    },
)
grn_draft_id = r_grn_draft.json()["id"]

# Attempt recording invoice when only Draft GRN exists -> fails cumulative protection (0 confirmed accepted)
r_rec_unconf = client.post(f"/supplier-invoices/{sinv_id}/record", headers=auth)
check("Recording invoice with 0 confirmed GRNs rejected (HTTP 400)", r_rec_unconf.status_code == 400)

# Confirm GRN for 10 units
client.post(f"/grns/{grn_draft_id}/confirm", headers=auth)

# -------------------------------------------------------------
# Focused Test 1 — Exact 3-Way Match & Recording
# -------------------------------------------------------------
print("\n--- Focused Test 1: Exact 3-Way Match & Recording ---")
# Invoice 1 billed_qty = 5 @ $50.0 (exact match on unit price)
r_rec_succ = client.post(f"/supplier-invoices/{sinv_id}/record", headers=auth)
check("Record invoice returns HTTP 200", r_rec_succ.status_code == 200)
rec_data = r_rec_succ.json()
check("Status updated to recorded", rec_data["status"] == "recorded")
check("Verification status is matched", rec_data["verification_status"] == "matched")

# Verify ZERO stock movement on record
db = SessionLocal()
try:
    moves = db.query(StockMovement).filter(StockMovement.organization_id == org_id).all()
    # 1 movement from GRN confirmation earlier, 0 from invoice record
    check("Zero extra StockMovement rows created on invoice record", len(moves) == 1)
finally:
    db.close()

# -------------------------------------------------------------
# Test F — Partial Invoicing (Invoice 2 for remaining 5 units)
# -------------------------------------------------------------
print("\n--- Test F: Partial Invoicing ---")
r_inv2 = client.post(
    "/supplier-invoices",
    headers=auth,
    json={
        "supplier_id": sup_id,
        "purchase_id": pur_id,
        "supplier_invoice_number": "VEND-INV-1002",
        "items": [{"purchase_item_id": pur_item_id, "billed_qty": 5, "unit_price": 50.0}],
    },
)
check("Second partial invoice created HTTP 201", r_inv2.status_code == 201)
sinv2_id = r_inv2.json()["id"]

r_rec2 = client.post(f"/supplier-invoices/{sinv2_id}/record", headers=auth)
check("Second partial invoice recorded HTTP 200", r_rec2.status_code == 200)
check("Total invoiced reached 10/10", r_rec2.json()["verification_status"] == "matched")

# -------------------------------------------------------------
# Test G — Over-Invoicing Protection (Double Invoicing)
# -------------------------------------------------------------
print("\n--- Test G: Cumulative Over-Invoicing Protection ---")
r_inv3 = client.post(
    "/supplier-invoices",
    headers=auth,
    json={
        "supplier_id": sup_id,
        "purchase_id": pur_id,
        "supplier_invoice_number": "VEND-INV-1003",
        "items": [{"purchase_item_id": pur_item_id, "billed_qty": 1, "unit_price": 50.0}],
    },
)
sinv3_id = r_inv3.json()["id"]

r_rec3 = client.post(f"/supplier-invoices/{sinv3_id}/record", headers=auth)
check("Over-invoicing beyond confirmed GRN accepted qty rejected (HTTP 400)", r_rec3.status_code == 400)

# -------------------------------------------------------------
# Focused Test 2 — Price Mismatch (Proves no unreachable disputed state)
# -------------------------------------------------------------
print("\n--- Focused Test 2: Price Mismatch & Status Verification ---")
# Setup second PO & GRN for 10 units @ $100.0
r_pur2 = client.post(
    "/purchases",
    headers=auth,
    json={
        "invoice_number": f"PO-{uuid.uuid4().hex[:6]}",
        "supplier_id": sup_id,
        "warehouse_id": wh_id,
        "items": [{"product_id": prod_id, "ordered_qty": 10, "purchase_price": 100.0}],
    },
).json()
client.post(f"/purchases/{r_pur2['id']}/confirm", headers=auth)

r_grn2 = client.post(
    "/grns",
    headers=auth,
    json={
        "purchase_id": r_pur2["id"],
        "supplier_id": sup_id,
        "warehouse_id": wh_id,
        "items": [{"purchase_item_id": r_pur2["items"][0]["id"], "product_id": prod_id, "received_qty": 10}],
    },
).json()
client.post(f"/grns/{r_grn2['id']}/confirm", headers=auth)

# Vendor bills at $120.0 (price variance)
r_inv_price = client.post(
    "/supplier-invoices",
    headers=auth,
    json={
        "supplier_id": sup_id,
        "purchase_id": r_pur2["id"],
        "supplier_invoice_number": "VEND-INV-PRICE-VAR",
        "items": [{"purchase_item_id": r_pur2["items"][0]["id"], "billed_qty": 10, "unit_price": 120.0}],
    },
).json()
r_rec_price = client.post(f"/supplier-invoices/{r_inv_price['id']}/record", headers=auth)
check("Price variance invoice records HTTP 200", r_rec_price.status_code == 200)
check("Status remains recorded (not disputed)", r_rec_price.json()["status"] == "recorded")
check("Verification status is mismatched", r_rec_price.json()["verification_status"] == "mismatched")

# -------------------------------------------------------------
# Focused Test 3 & 4 — Invoice Number Uniqueness Rules
# -------------------------------------------------------------
print("\n--- Focused Test 3 & 4: Invoice Number Uniqueness ---")
# Focused Test 3: Duplicate invoice number for same supplier -> blocked
r_dup_num = client.post(
    "/supplier-invoices",
    headers=auth,
    json={
        "supplier_id": sup_id,
        "purchase_id": r_pur2["id"],
        "supplier_invoice_number": "VEND-INV-PRICE-VAR",
        "items": [{"purchase_item_id": r_pur2["items"][0]["id"], "billed_qty": 1, "unit_price": 100.0}],
    },
)
check("Duplicate invoice number for same supplier blocked (HTTP 400)", r_dup_num.status_code == 400)

# Focused Test 4: Same invoice number for different supplier -> allowed
db = SessionLocal()
try:
    sup2 = Supplier(organization_id=org_id, name="Other Vendor", is_active=True)
    db.add(sup2)
    db.commit()
    db.refresh(sup2)
    sup2_id = sup2.id
finally:
    db.close()

# Create PO for Supplier 2
r_pur_sup2 = client.post(
    "/purchases",
    headers=auth,
    json={
        "invoice_number": f"PO-{uuid.uuid4().hex[:6]}",
        "supplier_id": sup2_id,
        "warehouse_id": wh_id,
        "items": [{"product_id": prod_id, "ordered_qty": 10, "purchase_price": 100.0}],
    },
).json()
client.post(f"/purchases/{r_pur_sup2['id']}/confirm", headers=auth)

r_inv_diff_sup = client.post(
    "/supplier-invoices",
    headers=auth,
    json={
        "supplier_id": sup2_id,
        "purchase_id": r_pur_sup2["id"],
        "supplier_invoice_number": "VEND-INV-PRICE-VAR", # Same invoice number, different supplier
        "items": [{"purchase_item_id": r_pur_sup2["items"][0]["id"], "billed_qty": 5, "unit_price": 100.0}],
    },
)
check("Same invoice number for different supplier returns HTTP 201", r_inv_diff_sup.status_code == 201)

# -------------------------------------------------------------
# Focused Test 5 — Cancel Unpaid Recorded Invoice
# -------------------------------------------------------------
print("\n--- Focused Test 5: Cancel Unpaid Recorded Invoice ---")
# sinv_id is recorded with amount_paid = 0
r_can_unpaid = client.post(f"/supplier-invoices/{sinv_id}/cancel", headers=auth)
check("Cancel unpaid recorded invoice returns HTTP 200", r_can_unpaid.status_code == 200)
check("Status updated to cancelled", r_can_unpaid.json()["status"] == "cancelled")

# -------------------------------------------------------------
# Focused Test 6 — Cancel Invoice with Payment Blocked
# -------------------------------------------------------------
print("\n--- Focused Test 6: Cancel Invoice with Payment Blocked ---")
# Set amount_paid > 0 on sinv2_id directly in DB for testing guard
db = SessionLocal()
try:
    inv_paid = db.get(SupplierInvoice, sinv2_id)
    inv_paid.amount_paid = 100.0
    db.commit()
finally:
    db.close()

r_can_paid = client.post(f"/supplier-invoices/{sinv2_id}/cancel", headers=auth)
check("Cancel invoice with amount_paid > 0 blocked (HTTP 400)", r_can_paid.status_code == 400)

db = SessionLocal()
try:
    inv_check = db.get(SupplierInvoice, sinv2_id)
    check("Invoice status remains recorded", inv_check.status == "recorded")
    check("Invoice amount_paid remains 100.0 (no payment reversal)", inv_check.amount_paid == 100.0)
finally:
    db.close()

# -------------------------------------------------------------
# Focused Test 7 — Cancelled Invoice Excluded from Cumulative Billed Qty
# -------------------------------------------------------------
print("\n--- Focused Test 7: Cancelled Invoice Excluded from Cumulative Billed Qty ---")
# Purchase 1 originally had 10 accepted units on GRN.
# sinv_id (5 units) was cancelled above.
# sinv2_id (5 units) is recorded.
# Now remaining billable capacity is 5 units because sinv_id (5 units) was cancelled and freed up!
r_inv_freed = client.post(
    "/supplier-invoices",
    headers=auth,
    json={
        "supplier_id": sup_id,
        "purchase_id": pur_id,
        "supplier_invoice_number": "VEND-INV-FREED-001",
        "items": [{"purchase_item_id": pur_item_id, "billed_qty": 5, "unit_price": 50.0}],
    },
).json()

r_rec_freed = client.post(f"/supplier-invoices/{r_inv_freed['id']}/record", headers=auth)
check("Recording invoice after previous cancellation succeeds (HTTP 200)", r_rec_freed.status_code == 200)

# -------------------------------------------------------------
# Focused Test 8 — Zero Stock Movement Verification
# -------------------------------------------------------------
print("\n--- Focused Test 8: Zero Stock Movement Verification ---")
db = SessionLocal()
try:
    moves = db.query(StockMovement).filter(StockMovement.organization_id == org_id).all()
    # 2 stock movements from 2 GRN confirmations, ZERO from invoice creation, record, or cancel
    check("Only 2 GRN StockMovements exist (0 from invoices)", len(moves) == 2)
finally:
    db.close()

# -------------------------------------------------------------
# Test P & Q — Delete Safety
# -------------------------------------------------------------
print("\n--- Test P & Q: Delete Safety ---")
# Draft invoice delete -> HTTP 204
r_del_draft = client.delete(f"/supplier-invoices/{sinv3_id}", headers=auth)
check("Delete draft invoice returns 204", r_del_draft.status_code == 204)

# Recorded invoice delete -> HTTP 400
r_del_rec = client.delete(f"/supplier-invoices/{sinv2_id}", headers=auth)
check("Delete recorded invoice blocked (HTTP 400)", r_del_rec.status_code == 400)

# Cancelled invoice delete -> HTTP 400
r_del_can = client.delete(f"/supplier-invoices/{sinv_id}", headers=auth)
check("Delete cancelled invoice blocked (HTTP 400)", r_del_can.status_code == 400)

# -------------------------------------------------------------
# Test S — Tenant Isolation
# -------------------------------------------------------------
print("\n--- Test S: Tenant Isolation ---")
auth_b, org_b = register_org("Firm B")
r_cross_read = client.get(f"/supplier-invoices/{sinv2_id}", headers=auth_b)
check("Cross-tenant invoice read rejected (HTTP 404)", r_cross_read.status_code == 404)

print("\n=======================================================")
print(f"SUPPLIER INVOICE TEST RESULTS: Passed={passed}, Failed={failed}")
print("=======================================================\n")

if failed > 0:
    sys.exit(1)
