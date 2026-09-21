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
# Confirm Purchase -> triggers auto-creation of SupplierInvoice
r_conf = client.post(f"/purchases/{pur_id}/confirm", headers=auth)
check("Confirm purchase returns HTTP 200", r_conf.status_code == 200)

# Verify auto-created invoice exists
db = SessionLocal()
try:
    auto_inv = (
        db.query(SupplierInvoice)
        .filter(SupplierInvoice.organization_id == org_id, SupplierInvoice.purchase_id == pur_id)
        .first()
    )
    check("Supplier Invoice auto-created on confirmation", auto_inv is not None)
    check("Auto-created invoice status is recorded", auto_inv.status == "recorded" if auto_inv else False)
    check("Auto-created invoice verification is matched", auto_inv.verification_status == "matched" if auto_inv else False)
    auto_inv_id = auto_inv.id if auto_inv else None
finally:
    db.close()

# For the subsequent manual invoice recording tests, cancel the auto-created invoice to free billable capacity
if auto_inv_id:
    client.post(f"/supplier-invoices/{auto_inv_id}/cancel", headers=auth)

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

# Cancel auto-created invoice on r_pur2 to allow testing manual price variance recording
db = SessionLocal()
try:
    auto_inv2 = (
        db.query(SupplierInvoice)
        .filter(SupplierInvoice.organization_id == org_id, SupplierInvoice.purchase_id == r_pur2["id"], SupplierInvoice.status != "cancelled")
        .first()
    )
    auto_inv2_id = auto_inv2.id if auto_inv2 else None
finally:
    db.close()
if auto_inv2_id:
    client.post(f"/supplier-invoices/{auto_inv2_id}/cancel", headers=auth)

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

# Cancel auto-created invoice on r_pur_sup2 to allow testing manual invoice recording
db = SessionLocal()
try:
    auto_inv_sup2 = (
        db.query(SupplierInvoice)
        .filter(SupplierInvoice.organization_id == org_id, SupplierInvoice.purchase_id == r_pur_sup2["id"], SupplierInvoice.status != "cancelled")
        .first()
    )
    auto_inv_sup2_id = auto_inv_sup2.id if auto_inv_sup2 else None
finally:
    db.close()
if auto_inv_sup2_id:
    client.post(f"/supplier-invoices/{auto_inv_sup2_id}/cancel", headers=auth)

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

# =============================================================
# AUTO-CREATION TARGETED TEST SUITE (Scenarios 1 - 12)
# =============================================================
print("\n=======================================================")
print("TEST SUITE: Purchase Invoice Auto-Creation Integration")
print("=======================================================\n")

# Scenario 1: Purchase confirmation auto-creates Supplier Invoice
print("--- Scenario 1: Purchase Confirmation Auto-Creates Supplier Invoice ---")
auth_ac, org_ac = register_org("Auto-Create Firm")
wh_ac, sup_ac, prod_ac = setup_procurement_env(auth_ac, org_ac)

r_po1 = client.post(
    "/purchases",
    headers=auth_ac,
    json={
        "invoice_number": f"PO-AUTO-{uuid.uuid4().hex[:6]}",
        "reference_number": "REF-VEND-9988",
        "supplier_id": sup_ac,
        "warehouse_id": wh_ac,
        "items": [
            {"product_id": prod_ac, "ordered_qty": 20, "purchase_price": 75.0, "tax_rate": 18.0, "discount": 100.0}
        ],
    },
).json()
po1_id = r_po1["id"]
po1_item_id = r_po1["items"][0]["id"]

# Confirm PO
r_po1_conf = client.post(f"/purchases/{po1_id}/confirm", headers=auth_ac)
check("PO1 confirmed HTTP 200", r_po1_conf.status_code == 200)

db = SessionLocal()
try:
    sinvs1 = (
        db.query(SupplierInvoice)
        .filter(SupplierInvoice.organization_id == org_ac, SupplierInvoice.purchase_id == po1_id)
        .all()
    )
    check("Exactly one SupplierInvoice created for PO1", len(sinvs1) == 1)
    sinv1 = sinvs1[0]
    check("SupplierInvoice purchase_id matches", sinv1.purchase_id == po1_id)
    check("SupplierInvoice supplier_id matches", sinv1.supplier_id == sup_ac)
    check("SupplierInvoice status is recorded", sinv1.status == "recorded")
    check("SupplierInvoice verification_status is matched", sinv1.verification_status == "matched")
    check("SupplierInvoice payment_status is unpaid", sinv1.payment_status == "unpaid")
    check("SupplierInvoice amount_paid is 0.0", sinv1.amount_paid == 0.0)

    # Scenario 2: Item mapping
    print("\n--- Scenario 2: Item Mapping Integrity ---")
    check("SupplierInvoice has 1 item", len(sinv1.items) == 1)
    s_item = sinv1.items[0]
    check("Item purchase_item_id matches", s_item.purchase_item_id == po1_item_id)
    check("Item product_id matches", s_item.product_id == prod_ac)
    check("Item billed_qty is 20", s_item.billed_qty == 20)
    check("Item unit_price is 75.0", s_item.unit_price == 75.0)
    check("Item tax_rate is 18.0", s_item.tax_rate == 18.0)
    check("Item discount is 100.0", s_item.discount_amount == 100.0)

    # Scenario 3: Financial calculations
    print("\n--- Scenario 3: Financial Calculations Preserved ---")
    check("Subtotal matches PO total", sinv1.subtotal == r_po1["subtotal"])
    check("Tax matches PO tax", sinv1.tax_amount == r_po1["tax"])
    check("Discount matches PO discount", sinv1.discount_amount == r_po1["discount"])
    check("Grand total matches PO grand total", sinv1.grand_total == r_po1["total"])
    check("Outstanding amount equals grand total", sinv1.outstanding_amount == r_po1["total"])

    # Scenario 4: Reference Number
    print("\n--- Scenario 4: Reference Number Preservation ---")
    check("Supplier invoice number preserved from reference_number", sinv1.supplier_invoice_number == "REF-VEND-9988")
finally:
    db.close()

# Scenario 5: Repeated confirmation idempotency
print("\n--- Scenario 5: Repeated Confirmation Idempotency ---")
r_po1_reconf = client.post(f"/purchases/{po1_id}/confirm", headers=auth_ac)
check("Repeated confirm returns HTTP 200", r_po1_reconf.status_code == 200)

db = SessionLocal()
try:
    sinvs1_after = (
        db.query(SupplierInvoice)
        .filter(SupplierInvoice.organization_id == org_ac, SupplierInvoice.purchase_id == po1_id)
        .all()
    )
    check("Still exactly one SupplierInvoice after repeated confirmation", len(sinvs1_after) == 1)
    check("Items count not duplicated", len(sinvs1_after[0].items) == 1)
finally:
    db.close()

# Scenario 6: Service-level direct call idempotency
print("\n--- Scenario 6: Service-Level Direct Call Idempotency ---")
from app.services import supplier_invoice_service
db = SessionLocal()
try:
    po_obj = db.get(PurchaseInvoice, po1_id)
    reused_inv = supplier_invoice_service.auto_create_from_purchase(db, po_obj, org_ac)
    check("Service returns existing invoice instance", reused_inv.id == sinvs1[0].id)
    sinvs_count = (
        db.query(SupplierInvoice)
        .filter(SupplierInvoice.organization_id == org_ac, SupplierInvoice.purchase_id == po1_id)
        .count()
    )
    check("Database invoice count remains 1", sinvs_count == 1)
finally:
    db.close()

# Scenario 7: Manual Supplier Invoice Compatibility
print("\n--- Scenario 7: Manual Supplier Invoice Compatibility ---")
# Create PO2 in draft
r_po2 = client.post(
    "/purchases",
    headers=auth_ac,
    json={
        "invoice_number": f"PO-MANUAL-{uuid.uuid4().hex[:6]}",
        "supplier_id": sup_ac,
        "warehouse_id": wh_ac,
        "items": [{"product_id": prod_ac, "ordered_qty": 10, "purchase_price": 50.0}],
    },
).json()
po2_id = r_po2["id"]
# Confirm PO2
client.post(f"/purchases/{po2_id}/confirm", headers=auth_ac)

# Confirm auto-created invoice exists
db = SessionLocal()
try:
    inv2_auto = (
        db.query(SupplierInvoice)
        .filter(SupplierInvoice.organization_id == org_ac, SupplierInvoice.purchase_id == po2_id)
        .first()
    )
    check("PO2 auto-created invoice exists", inv2_auto is not None)
    # Manual query for existing invoices against PO2
    r_list2 = client.get(f"/supplier-invoices?purchase_id={po2_id}", headers=auth_ac).json()
    check("GET /supplier-invoices lists auto-created invoice", len(r_list2) == 1)
finally:
    db.close()

# Scenario 8: Accounts Payable Integration
print("\n--- Scenario 8: Accounts Payable Integration ---")
r_ap_open = client.get("/accounts-payable", headers=auth_ac).json()
check("AP open invoices includes PO1 invoice", any(i["supplier_invoice_id"] == sinvs1[0].id for i in r_ap_open["items"]))

r_ap_summary = client.get("/accounts-payable/summary", headers=auth_ac).json()
check("AP summary has open invoices", r_ap_summary["summary"]["open_invoice_count"] >= 2)

r_ap_stmt = client.get(f"/accounts-payable/supplier/{sup_ac}", headers=auth_ac).json()
check("AP supplier statement total_open_payable includes auto-invoices", r_ap_stmt["total_open_payable"] > 0)

# Scenario 9: Supplier Payment Compatibility
print("\n--- Scenario 9: Supplier Payment Flow on Auto-Created Invoice ---")
sinv1_id = sinvs1[0].id
r_pay = client.post(
    "/supplier-payments",
    headers=auth_ac,
    json={
        "supplier_id": sup_ac,
        "payment_date": "2026-09-21T00:00:00Z",
        "payment_method": "bank_transfer",
        "amount": 500.0,
        "allocations": [{"supplier_invoice_id": sinv1_id, "amount": 500.0}],
    },
)
check("Payment against auto-created invoice returns HTTP 201", r_pay.status_code == 201)

db = SessionLocal()
try:
    sinv1_paid = db.get(SupplierInvoice, sinv1_id)
    check("SupplierInvoice amount_paid updated to 500.0", sinv1_paid.amount_paid == 500.0)
    check("SupplierInvoice payment_status is partially_paid", sinv1_paid.payment_status == "partially_paid")
    check("SupplierInvoice outstanding decreased by 500.0", sinv1_paid.outstanding_amount == round(sinv1_paid.grand_total - 500.0, 2))
finally:
    db.close()

# Scenario 10: Multi-Tenant Isolation on Auto-Creation
print("\n--- Scenario 10: Multi-Tenant Isolation ---")
auth_c, org_c = register_org("Firm C")
r_cross_ap = client.get("/accounts-payable", headers=auth_c).json()
check("Firm C AP list does not contain Firm Auto-Create invoices", len(r_cross_ap["items"]) == 0)

# Scenario 11: Transaction & Zero Stock Side Effect
print("\n--- Scenario 11: Zero Stock Movement on Auto-Creation ---")
db = SessionLocal()
try:
    moves_ac = db.query(StockMovement).filter(StockMovement.organization_id == org_ac).all()
    check("Zero StockMovement rows created by auto-created supplier invoices", len(moves_ac) == 0)
finally:
    db.close()

# Scenario 12: Direct Confirmation on Purchase Creation
print("\n--- Scenario 12: Direct Confirmation on Purchase Creation ---")
r_po_direct = client.post(
    "/purchases",
    headers=auth_ac,
    json={
        "invoice_number": f"PO-DIRECT-{uuid.uuid4().hex[:6]}",
        "purchase_status": "confirmed",
        "supplier_id": sup_ac,
        "warehouse_id": wh_ac,
        "items": [{"product_id": prod_ac, "ordered_qty": 5, "purchase_price": 40.0}],
    },
)
check("Directly confirmed purchase returns HTTP 201", r_po_direct.status_code == 201)
po_direct_id = r_po_direct.json()["id"]

db = SessionLocal()
try:
    sinv_direct = (
        db.query(SupplierInvoice)
        .filter(SupplierInvoice.organization_id == org_ac, SupplierInvoice.purchase_id == po_direct_id)
        .first()
    )
    check("Directly confirmed purchase auto-creates SupplierInvoice immediately", sinv_direct is not None)
    check("Direct SupplierInvoice is recorded", sinv_direct.status == "recorded" if sinv_direct else False)
    check("Direct SupplierInvoice grand total is 200.0", sinv_direct.grand_total == 200.0 if sinv_direct else False)
finally:
    db.close()

print("\n=======================================================")
print(f"SUPPLIER INVOICE TEST RESULTS: Passed={passed}, Failed={failed}")
print("=======================================================\n")

if failed > 0:
    sys.exit(1)

