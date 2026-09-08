"""Focused automated test suite for Accounts Payable Module."""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from app.main import app
from app.core.database import SessionLocal
from app.models.product import Product
from app.models.purchase_invoice import PurchaseInvoice, PurchaseInvoiceItem
from app.models.supplier import Supplier
from app.models.supplier_invoice import SupplierInvoice, SupplierInvoiceItem
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


def register_org(name_prefix: str = "AP Firm"):
    email = f"ap_admin_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post(
        "/auth/register",
        json={
            "organization_name": f"{name_prefix} {uuid.uuid4().hex[:6]}",
            "admin_name": "AP Admin",
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
        wh = Warehouse(organization_id=org_id, name="AP Main Warehouse", code=f"WH-{uuid.uuid4().hex[:4]}")
        db.add(wh)
        sup = Supplier(organization_id=org_id, name=f"Supplier {uuid.uuid4().hex[:4]}", is_active=True)
        db.add(sup)
        prod = Product(organization_id=org_id, name="Widget AP", sku=f"SKU-{uuid.uuid4().hex[:4]}", price=100.0, total_inventory=0)
        db.add(prod)
        db.commit()
        db.refresh(wh)
        db.refresh(sup)
        db.refresh(prod)
        return wh.id, sup.id, prod.id
    finally:
        db.close()


print("\n=======================================================")
print("TEST SUITE: Accounts Payable Derived Read Layer")
print("=======================================================\n")

auth, org_id = register_org()
wh_id, sup_id, prod_id = setup_procurement_env(auth, org_id)

now = datetime.now(timezone.utc)

# -------------------------------------------------------------
# Setup Baseline Purchase Order & Confirmed GRN
# -------------------------------------------------------------
r_pur = client.post(
    "/purchases",
    headers=auth,
    json={
        "invoice_number": f"PO-{uuid.uuid4().hex[:6]}",
        "supplier_id": sup_id,
        "warehouse_id": wh_id,
        "items": [{"product_id": prod_id, "ordered_qty": 100, "purchase_price": 100.0}],
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
        "items": [{"purchase_item_id": pur_item_id, "product_id": prod_id, "received_qty": 100}],
    },
).json()
client.post(f"/grns/{r_grn['id']}/confirm", headers=auth)

# -------------------------------------------------------------
# Test 1 & Test 8 — Recorded Unpaid Invoice & Null Due Date
# -------------------------------------------------------------
print("--- Test 1 & 8: Recorded Unpaid Invoice & Null Due Date ---")
r_inv1 = client.post(
    "/supplier-invoices",
    headers=auth,
    json={
        "supplier_id": sup_id,
        "purchase_id": pur_id,
        "supplier_invoice_number": "AP-INV-001",
        "items": [{"purchase_item_id": pur_item_id, "billed_qty": 10, "unit_price": 100.0}],
    },
).json()
client.post(f"/supplier-invoices/{r_inv1['id']}/record", headers=auth)

r_ap = client.get("/accounts-payable", headers=auth)
check("Get open payables returns HTTP 200", r_ap.status_code == 200)
ap_data = r_ap.json()
check("AP list contains 1 item", len(ap_data["items"]) == 1)
item1 = ap_data["items"][0]
check("Item 1 invoice number is AP-INV-001", item1["supplier_invoice_number"] == "AP-INV-001")
check("Item 1 grand_total is 1000.0", item1["grand_total"] == 1000.0)
check("Item 1 amount_paid is 0.0", item1["amount_paid"] == 0.0)
check("Item 1 outstanding_amount is 1000.0", item1["outstanding_amount"] == 1000.0)
check("Item 1 is_overdue is false (null due_date)", item1["is_overdue"] is False)
check("Item 1 days_overdue is 0", item1["days_overdue"] == 0)

# -------------------------------------------------------------
# Test 2 — Draft Invoice Excluded from AP
# -------------------------------------------------------------
print("\n--- Test 2: Draft Invoice Excluded from AP ---")
r_inv_draft = client.post(
    "/supplier-invoices",
    headers=auth,
    json={
        "supplier_id": sup_id,
        "purchase_id": pur_id,
        "supplier_invoice_number": "AP-INV-DRAFT",
        "items": [{"purchase_item_id": pur_item_id, "billed_qty": 5, "unit_price": 100.0}],
    },
).json()

r_ap2 = client.get("/accounts-payable", headers=auth).json()
check("Draft invoice is excluded from AP list", len(r_ap2["items"]) == 1)

# -------------------------------------------------------------
# Test 3 — Cancelled Invoice Excluded from AP
# -------------------------------------------------------------
print("\n--- Test 3: Cancelled Invoice Excluded from AP ---")
r_inv_can = client.post(
    "/supplier-invoices",
    headers=auth,
    json={
        "supplier_id": sup_id,
        "purchase_id": pur_id,
        "supplier_invoice_number": "AP-INV-CANCELLED",
        "items": [{"purchase_item_id": pur_item_id, "billed_qty": 5, "unit_price": 100.0}],
    },
).json()
client.post(f"/supplier-invoices/{r_inv_can['id']}/cancel", headers=auth)

r_ap3 = client.get("/accounts-payable", headers=auth).json()
check("Cancelled invoice is excluded from AP list", len(r_ap3["items"]) == 1)

# -------------------------------------------------------------
# Test 4 — Fully Paid Invoice Excluded from AP
# -------------------------------------------------------------
print("\n--- Test 4: Fully Paid Invoice Excluded from AP ---")
r_inv_paid = client.post(
    "/supplier-invoices",
    headers=auth,
    json={
        "supplier_id": sup_id,
        "purchase_id": pur_id,
        "supplier_invoice_number": "AP-INV-PAID",
        "items": [{"purchase_item_id": pur_item_id, "billed_qty": 5, "unit_price": 100.0}],
    },
).json()
client.post(f"/supplier-invoices/{r_inv_paid['id']}/record", headers=auth)

# Mark as paid in DB
db = SessionLocal()
try:
    sinv_p = db.get(SupplierInvoice, r_inv_paid['id'])
    sinv_p.amount_paid = 500.0
    sinv_p.payment_status = "paid"
    db.commit()
finally:
    db.close()

r_ap4 = client.get("/accounts-payable", headers=auth).json()
check("Fully paid invoice (outstanding = 0) excluded from open AP", len(r_ap4["items"]) == 1)

# -------------------------------------------------------------
# Test 5 — Partially Paid Invoice Included in AP
# -------------------------------------------------------------
print("\n--- Test 5: Partially Paid Invoice Included in AP ---")
r_inv_part = client.post(
    "/supplier-invoices",
    headers=auth,
    json={
        "supplier_id": sup_id,
        "purchase_id": pur_id,
        "supplier_invoice_number": "AP-INV-PARTIAL",
        "items": [{"purchase_item_id": pur_item_id, "billed_qty": 10, "unit_price": 100.0}],
    },
).json()
client.post(f"/supplier-invoices/{r_inv_part['id']}/record", headers=auth)

# Mark partially paid in DB
db = SessionLocal()
try:
    sinv_pt = db.get(SupplierInvoice, r_inv_part['id'])
    sinv_pt.amount_paid = 400.0
    sinv_pt.payment_status = "partially_paid"
    db.commit()
finally:
    db.close()

r_ap5 = client.get("/accounts-payable", headers=auth).json()
check("AP list contains 2 open invoices (unpaid + partially_paid)", len(r_ap5["items"]) == 2)
part_item = next(i for i in r_ap5["items"] if i["supplier_invoice_number"] == "AP-INV-PARTIAL")
check("Partially paid invoice outstanding is 600.0", part_item["outstanding_amount"] == 600.0)
check("Partially paid invoice payment_status is partially_paid", part_item["payment_status"] == "partially_paid")

# -------------------------------------------------------------
# Test 6 & 16 — Overdue Invoice, Aging Buckets & Days Overdue
# -------------------------------------------------------------
print("\n--- Test 6 & 16: Overdue Invoice & Aging Buckets ---")
past_due = (now - timedelta(days=45)).isoformat()
r_inv_over = client.post(
    "/supplier-invoices",
    headers=auth,
    json={
        "supplier_id": sup_id,
        "purchase_id": pur_id,
        "supplier_invoice_number": "AP-INV-OVERDUE-45",
        "due_date": past_due,
        "items": [{"purchase_item_id": pur_item_id, "billed_qty": 10, "unit_price": 100.0}],
    },
).json()
client.post(f"/supplier-invoices/{r_inv_over['id']}/record", headers=auth)

r_ap6 = client.get("/accounts-payable", headers=auth).json()
over_item = next(i for i in r_ap6["items"] if i["supplier_invoice_number"] == "AP-INV-OVERDUE-45")
check("Overdue invoice is_overdue is true", over_item["is_overdue"] is True)
check("Overdue invoice days_overdue is ~45", over_item["days_overdue"] in (44, 45, 46))
check("Overdue invoice ageing_bucket is 31_60", over_item["ageing_bucket"] == "31_60")
check("Ageing bucket 31_60 total is 1000.0", r_ap6["ageing"]["bucket_31_60"] == 1000.0)

# -------------------------------------------------------------
# Test 7 — Future Due Invoice Not Overdue
# -------------------------------------------------------------
print("\n--- Test 7: Future Due Invoice Not Overdue ---")
future_due = (now + timedelta(days=15)).isoformat()
r_inv_fut = client.post(
    "/supplier-invoices",
    headers=auth,
    json={
        "supplier_id": sup_id,
        "purchase_id": pur_id,
        "supplier_invoice_number": "AP-INV-FUTURE-15",
        "due_date": future_due,
        "items": [{"purchase_item_id": pur_item_id, "billed_qty": 5, "unit_price": 100.0}],
    },
).json()
client.post(f"/supplier-invoices/{r_inv_fut['id']}/record", headers=auth)

r_ap7 = client.get("/accounts-payable", headers=auth).json()
fut_item = next(i for i in r_ap7["items"] if i["supplier_invoice_number"] == "AP-INV-FUTURE-15")
check("Future due invoice is_overdue is false", fut_item["is_overdue"] is False)
check("Future due invoice days_overdue is 0", fut_item["days_overdue"] == 0)
check("Future due invoice ageing_bucket is None", fut_item["ageing_bucket"] is None)

# -------------------------------------------------------------
# Test 9 — Mismatched Recorded Invoice Included in AP
# -------------------------------------------------------------
print("\n--- Test 9: Mismatched Recorded Invoice Included in AP ---")
r_inv_mismatch = client.post(
    "/supplier-invoices",
    headers=auth,
    json={
        "supplier_id": sup_id,
        "purchase_id": pur_id,
        "supplier_invoice_number": "AP-INV-MISMATCH",
        "items": [{"purchase_item_id": pur_item_id, "billed_qty": 5, "unit_price": 120.0}], # Price variance
    },
).json()
client.post(f"/supplier-invoices/{r_inv_mismatch['id']}/record", headers=auth)

r_ap9 = client.get("/accounts-payable", headers=auth).json()
mis_item = next(i for i in r_ap9["items"] if i["supplier_invoice_number"] == "AP-INV-MISMATCH")
check("Mismatched invoice status is recorded", mis_item["status"] == "recorded")
check("Mismatched invoice verification_status is mismatched", mis_item["verification_status"] == "mismatched")
check("Mismatched invoice is included in AP list", mis_item["outstanding_amount"] == 600.0)

# -------------------------------------------------------------
# Test 10, 11, 12, 13, 14 — Query Filters & Search
# -------------------------------------------------------------
print("\n--- Test 10-14: Query Filters & Search ---")
# Test 10: Supplier filter
r_f_sup = client.get(f"/accounts-payable?supplier_id={sup_id}", headers=auth).json()
check("Supplier filter returns all supplier payables", len(r_f_sup["items"]) == 5)

# Test 11: Payment status filter
r_f_payst = client.get("/accounts-payable?payment_status=partially_paid", headers=auth).json()
check("Payment status filter partially_paid returns 1 item", len(r_f_payst["items"]) == 1)

# Test 12: Verification status filter
r_f_verst = client.get("/accounts-payable?verification_status=mismatched", headers=auth).json()
check("Verification status filter mismatched returns 1 item", len(r_f_verst["items"]) == 1)

# Test 13: Overdue-only filter
r_f_over = client.get("/accounts-payable?overdue_only=true", headers=auth).json()
check("Overdue-only filter returns 1 overdue item", len(r_f_over["items"]) == 1)
check("Overdue-only item is AP-INV-OVERDUE-45", r_f_over["items"][0]["supplier_invoice_number"] == "AP-INV-OVERDUE-45")

# Test 14: Search
r_f_srch = client.get("/accounts-payable?search=OVERDUE", headers=auth).json()
check("Search for OVERDUE returns 1 matching item", len(r_f_srch["items"]) == 1)

# Test 14b: Pagination (page and page_size)
r_f_page = client.get("/accounts-payable?page=1&page_size=2", headers=auth).json()
check("Pagination page=1&page_size=2 returns 2 items in items list", len(r_f_page["items"]) == 2)
check("Pagination response summary open_invoice_count reflects total matching open invoices (5)", r_f_page["summary"]["open_invoice_count"] == 5)

# -------------------------------------------------------------
# Test 16b — Exact Aging Bucket Boundaries & Due-Today Logic
# -------------------------------------------------------------
print("\n--- Test 16b: Exact Aging Bucket Boundaries & Due Today ---")
db = SessionLocal()
try:
    from app.services.accounts_payable_service import compute_ap_item_details

    ref_now = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)

    def mock_inv(due_dt):
        inv = SupplierInvoice()
        inv.due_date = due_dt
        return inv

    # Due today
    ov, d, b = compute_ap_item_details(mock_inv(datetime(2026, 9, 8, 0, 0, 0, tzinfo=timezone.utc)), ref_now)
    check("Due today: is_overdue is False", ov is False and d == 0 and b is None)

    # 1 day overdue -> 0_30
    ov, d, b = compute_ap_item_details(mock_inv(datetime(2026, 9, 7, 0, 0, 0, tzinfo=timezone.utc)), ref_now)
    check("1 day overdue -> bucket 0_30", ov is True and d == 1 and b == "0_30")

    # 30 days overdue -> 0_30
    ov, d, b = compute_ap_item_details(mock_inv(datetime(2026, 8, 9, 0, 0, 0, tzinfo=timezone.utc)), ref_now)
    check("30 days overdue -> bucket 0_30", ov is True and d == 30 and b == "0_30")

    # 31 days overdue -> 31_60
    ov, d, b = compute_ap_item_details(mock_inv(datetime(2026, 8, 8, 0, 0, 0, tzinfo=timezone.utc)), ref_now)
    check("31 days overdue -> bucket 31_60", ov is True and d == 31 and b == "31_60")

    # 60 days overdue -> 31_60
    ov, d, b = compute_ap_item_details(mock_inv(datetime(2026, 7, 10, 0, 0, 0, tzinfo=timezone.utc)), ref_now)
    check("60 days overdue -> bucket 31_60", ov is True and d == 60 and b == "31_60")

    # 61 days overdue -> 61_90
    ov, d, b = compute_ap_item_details(mock_inv(datetime(2026, 7, 9, 0, 0, 0, tzinfo=timezone.utc)), ref_now)
    check("61 days overdue -> bucket 61_90", ov is True and d == 61 and b == "61_90")

    # 90 days overdue -> 61_90
    ov, d, b = compute_ap_item_details(mock_inv(datetime(2026, 6, 10, 0, 0, 0, tzinfo=timezone.utc)), ref_now)
    check("90 days overdue -> bucket 61_90", ov is True and d == 90 and b == "61_90")

    # 91 days overdue -> 90_plus
    ov, d, b = compute_ap_item_details(mock_inv(datetime(2026, 6, 9, 0, 0, 0, tzinfo=timezone.utc)), ref_now)
    check("91 days overdue -> bucket 90_plus", ov is True and d == 91 and b == "90_plus")
finally:
    db.close()

# -------------------------------------------------------------
# Test 15 — AP Summary Endpoint
# -------------------------------------------------------------
print("\n--- Test 15: AP Summary Endpoint ---")
r_sum = client.get("/accounts-payable/summary", headers=auth)
check("GET /accounts-payable/summary returns HTTP 200", r_sum.status_code == 200)
sum_body = r_sum.json()
check("Summary total_payable is 3700.0", sum_body["summary"]["total_payable"] == 3700.0)
check("Summary total_overdue is 1000.0", sum_body["summary"]["total_overdue"] == 1000.0)
check("Summary open_invoice_count is 5", sum_body["summary"]["open_invoice_count"] == 5)
check("Summary supplier_count is 1", sum_body["summary"]["supplier_count"] == 1)

# -------------------------------------------------------------
# Test 17 — Supplier AP Statement Endpoint
# -------------------------------------------------------------
print("\n--- Test 17: Supplier AP Statement Endpoint ---")
r_stmt = client.get(f"/accounts-payable/supplier/{sup_id}", headers=auth)
check("GET /accounts-payable/supplier/{id} returns HTTP 200", r_stmt.status_code == 200)
stmt_body = r_stmt.json()
check("Statement supplier_id matches", stmt_body["supplier_id"] == sup_id)
check("Statement total_open_payable is 3700.0", stmt_body["total_open_payable"] == 3700.0)
check("Statement open_invoice_count is 5", stmt_body["open_invoice_count"] == 5)
check("Statement invoices list contains 5 items", len(stmt_body["invoices"]) == 5)

# -------------------------------------------------------------
# Test 18 — Tenant Isolation
# -------------------------------------------------------------
print("\n--- Test 18: Tenant Isolation ---")
auth_b, org_b = register_org("Firm B AP")
r_cross_ap = client.get("/accounts-payable", headers=auth_b).json()
check("Cross-tenant AP list returns 0 items for Firm B", len(r_cross_ap["items"]) == 0)

r_cross_stmt = client.get(f"/accounts-payable/supplier/{sup_id}", headers=auth_b)
check("Cross-tenant supplier statement returns HTTP 404", r_cross_stmt.status_code == 404)

# -------------------------------------------------------------
# Test 19 — Zero Stock Movement Verification (Read-Only Side Effects)
# -------------------------------------------------------------
print("\n--- Test 19: Zero Inventory Side Effects ---")
db = SessionLocal()
try:
    moves_before = len(db.query(StockMovement).filter(StockMovement.organization_id == org_id).all())
finally:
    db.close()

# Execute all AP read endpoints
client.get("/accounts-payable", headers=auth)
client.get("/accounts-payable/summary", headers=auth)
client.get(f"/accounts-payable/supplier/{sup_id}", headers=auth)

db = SessionLocal()
try:
    moves_after = len(db.query(StockMovement).filter(StockMovement.organization_id == org_id).all())
    check("Calling AP read endpoints produces ZERO StockMovement rows", moves_after == moves_before)
finally:
    db.close()

# -------------------------------------------------------------
# Test 20 — Existing Supplier Invoice Regression Safety
# -------------------------------------------------------------
print("\n--- Test 20: Existing Supplier Invoice Regression Safety ---")
db = SessionLocal()
try:
    sinv_check = db.get(SupplierInvoice, r_inv1['id'])
    check("SupplierInvoice status remains recorded", sinv_check.status == "recorded")
    check("SupplierInvoice grand_total remains 1000.0", sinv_check.grand_total == 1000.0)
    check("SupplierInvoice amount_paid remains 0.0", sinv_check.amount_paid == 0.0)
finally:
    db.close()

print("\n=======================================================")
print(f"ACCOUNTS PAYABLE TEST RESULTS: Passed={passed}, Failed={failed}")
print("=======================================================\n")

if failed > 0:
    sys.exit(1)
