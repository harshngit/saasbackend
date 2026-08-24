"""Comprehensive test suite for the complete Expense Profile & Management system."""

import io
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from app.main import app
from app.core.database import SessionLocal
from app.models.supplier import Supplier
from app.models.user import User
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


def register_org(name_prefix: str = "Expense Firm"):
    email = f"admin_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={
        "organization_name": f"{name_prefix} {uuid.uuid4().hex[:6]}",
        "admin_name": "Expense Admin",
        "email": email,
        "password": "Password123!",
        "role": "admin",
    })
    assert r.status_code == 201, r.text
    token = r.json()["tokens"]["access_token"]
    auth = {"Authorization": f"Bearer {token}"}

    # Get org_id from database
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        org_id = user.organization_id
    finally:
        db.close()

    return auth, org_id


print("\n=======================================================")
print("TEST SUITE: Complete Expense Profile & Management System")
print("=======================================================\n")

headers, org1_id = register_org("Expense Org 1")
headers2, org2_id = register_org("Expense Org 2")

# Create a test supplier in Org 1
db = SessionLocal()
try:
    supplier1 = Supplier(
        organization_id=org1_id,
        name="Office World Supplies",
        contact_person="Ramesh Gupta",
        phone="9876543210",
        email="ramesh@officeworld.com",
        gst_number="27AAAAA0000A1Z5",
        category="Stationery",
    )
    db.add(supplier1)

    supplier2 = Supplier(
        organization_id=org2_id,
        name="Foreign Org Vendor",
        contact_person="Foreign Rep",
        phone="9123456789",
        email="rep@org2.com",
    )
    db.add(supplier2)
    db.commit()
    db.refresh(supplier1)
    db.refresh(supplier2)
    supplier1_id = supplier1.id
    supplier2_id = supplier2.id
finally:
    db.close()

# --- TEST 1: BASIC INFORMATION (Branch, Department, Financial Year, Auto-Numbering) ---
print("--- TEST 1: Basic Information & Auto-Numbering ---")
res = client.post(
    "/expenses",
    headers=headers,
    json={
        "category": "Office Expenses",
        "amount": 1500.0,
        "description": "Office Stationery & Printer Cartridges",
        "expense_type": "Operational",
        "financial_year": "2026-2027",
        "branch_id": "BR-MUMBAI-01",
        "department_id": "DEPT-ADMIN",
        "payment_mode": "bank_transfer",
    },
)
check("Create expense with branch & department (HTTP 201)", res.status_code == 201)
exp1 = res.json()
check("Expense ID generated with EXPID prefix", exp1["expense_id"].startswith("EXPID"))
check("Expense Number generated with EXP prefix", exp1["expense_number"].startswith("EXP"))
check("Branch ID persisted correctly", exp1["branch_id"] == "BR-MUMBAI-01")
check("Department ID persisted correctly", exp1["department_id"] == "DEPT-ADMIN")
check("Financial Year persisted correctly", exp1["financial_year"] == "2026-2027")
check("Expense Type is Operational", exp1["expense_type"] == "Operational")
check("Initial status is pending", exp1["status"] == "pending")

# --- TEST 2: PAYEE DETAILS (Vendor Auto-fill & Manual Payee Fallback) ---
print("\n--- TEST 2: Payee Details (Vendor Auto-fill & Manual Fallback) ---")
# 2A: With vendor_id -> auto-fills contact person, phone, email, GSTIN
res_vendor = client.post(
    "/expenses",
    headers=headers,
    json={
        "category": "Office Expenses",
        "amount": 2400.0,
        "vendor_id": supplier1_id,
        "description": "Paper Rims from Registered Vendor",
    },
)
check("Create expense with vendor_id (HTTP 201)", res_vendor.status_code == 201)
exp_v = res_vendor.json()
check("Payee name auto-filled from vendor", exp_v["payee_name"] == "Office World Supplies")
check("Contact person auto-filled from vendor", exp_v["contact_person"] == "Ramesh Gupta")
check("Mobile number auto-filled from vendor", exp_v["mobile_number"] == "9876543210")
check("Email auto-filled from vendor", exp_v["email_address"] == "ramesh@officeworld.com")
check("Payee GSTIN auto-filled from vendor", exp_v["payee_gstin"] == "27AAAAA0000A1Z5")
check("Vendor object populated in response", exp_v.get("vendor") is not None and exp_v["vendor"]["id"] == supplier1_id)

# 2B: Manual payee information (no vendor_id)
res_manual = client.post(
    "/expenses",
    headers=headers,
    json={
        "category": "Food and Travel",
        "amount": 450.0,
        "payee_name": "City Express Taxi Service",
        "contact_person": "Driver Suresh",
        "mobile_number": "9988776655",
        "description": "Airport Client Pickup Taxi",
    },
)
check("Create expense with manual payee details (HTTP 201)", res_manual.status_code == 201)
exp_m = res_manual.json()
check("Manual payee name persisted", exp_m["payee_name"] == "City Express Taxi Service")
check("Manual contact person persisted", exp_m["contact_person"] == "Driver Suresh")
check("Manual mobile number persisted", exp_m["mobile_number"] == "9988776655")
check("Vendor ID is None for manual payee", exp_m["vendor_id"] is None)

# --- TEST 3: ITEMIZED EXPENSE BREAKDOWN & SERVER-SIDE CALCULATIONS ---
print("\n--- TEST 3: Itemized Breakdown & Server-Side Calculations ---")
res_items = client.post(
    "/expenses",
    headers=headers,
    json={
        "category": "Vehicle Maintenance",
        "description": "Delivery Van Routine Service",
        "items": [
            {"description": "Engine Oil 5L", "quantity": 2, "unit_price": 600.0, "tax_rate": 18.0},
            {"description": "Air Filter", "quantity": 1, "unit_price": 300.0, "tax_rate": 18.0},
            {"description": "Labor Charges", "quantity": 1, "unit_price": 500.0, "tax_rate": 18.0},
        ],
    },
)
check("Create itemized expense (HTTP 201)", res_items.status_code == 201)
exp_i = res_items.json()
check("Items list contains 3 items", len(exp_i["items"]) == 3)
# Math verification:
# Item 1: 2 * 600 = 1200, tax 18% = 216, total = 1416
# Item 2: 1 * 300 = 300, tax 18% = 54, total = 354
# Item 3: 1 * 500 = 500, tax 18% = 90, total = 590
# Subtotal = 1200 + 300 + 500 = 2000.0
# Tax Total = 216 + 54 + 90 = 360.0
# Grand Total = 2360.0
check("Line 1 subtotal + tax = 1416.0", exp_i["items"][0]["line_total"] == 1416.0)
check("Line 2 subtotal + tax = 354.0", exp_i["items"][1]["line_total"] == 354.0)
check("Line 3 subtotal + tax = 590.0", exp_i["items"][2]["line_total"] == 590.0)
check("Expense subtotal calculated: 2000.0", exp_i["subtotal"] == 2000.0)
check("Expense tax_amount calculated: 360.0", exp_i["tax_amount"] == 360.0)
check("Expense total amount calculated: 2360.0", exp_i["amount"] == 2360.0)

# --- TEST 4: VALIDATION CHECKS (Invalid Qty, Price, Tax, Missing Body) ---
print("\n--- TEST 4: Input Validation Checks ---")
res_neg_qty = client.post(
    "/expenses",
    headers=headers,
    json={"category": "Rent", "items": [{"description": "Office Space", "quantity": -1, "unit_price": 1000}]},
)
check("Reject negative quantity with HTTP 422/400", res_neg_qty.status_code in (400, 422))

res_neg_price = client.post(
    "/expenses",
    headers=headers,
    json={"category": "Rent", "items": [{"description": "Office Space", "quantity": 1, "unit_price": -500}]},
)
check("Reject negative unit price with HTTP 422/400", res_neg_price.status_code in (400, 422))

res_inv_tax = client.post(
    "/expenses",
    headers=headers,
    json={"category": "Rent", "items": [{"description": "Office Space", "quantity": 1, "unit_price": 1000, "tax_rate": 150}]},
)
check("Reject tax rate > 100 with HTTP 422/400", res_inv_tax.status_code in (400, 422))

res_empty = client.post("/expenses", headers=headers, json={"category": "Rent"})
check("Reject expense without amount and without items", res_empty.status_code in (400, 422))

# --- TEST 5: PAYMENT REFERENCE & ACCOUNTING (Cost Center, Project, TDS) ---
print("\n--- TEST 5: Payment Reference, Cost Center, Project & TDS ---")
res_acct = client.post(
    "/expenses",
    headers=headers,
    json={
        "category": "Utilities",
        "amount": 10000.0,
        "payment_mode": "bank_transfer",
        "payment_reference": "UTR-HDFC-9988221100",
        "cost_center_id": "CC-LOGISTICS",
        "project_id": "PROJ-EXPANSION-2026",
        "tax_category": "GST 18%",
        "tds_applicable": True,
        "tds_amount": 1000.0,  # 10% TDS
        "tags": ["capex", "warehouse", "q3"],
    },
)
check("Create expense with accounting & TDS (HTTP 201)", res_acct.status_code == 201)
exp_a = res_acct.json()
check("Payment reference persisted", exp_a["payment_reference"] == "UTR-HDFC-9988221100")
check("Cost center persisted", exp_a["cost_center_id"] == "CC-LOGISTICS")
check("Project ID persisted", exp_a["project_id"] == "PROJ-EXPANSION-2026")
check("Tax category persisted", exp_a["tax_category"] == "GST 18%")
check("TDS applicable is True", exp_a["tds_applicable"] is True)
check("TDS amount persisted: 1000.0", exp_a["tds_amount"] == 1000.0)
check("Net payable correctly computed: 9000.0", exp_a["net_payable"] == 9000.0)
check("Tags list persisted", exp_a["tags"] == ["capex", "warehouse", "q3"])

# TDS amount exceeding total expense must be rejected
res_bad_tds = client.post(
    "/expenses",
    headers=headers,
    json={"category": "Rent", "amount": 1000.0, "tds_applicable": True, "tds_amount": 1500.0},
)
check("Reject TDS amount exceeding total expense (HTTP 400)", res_bad_tds.status_code == 400)

# --- TEST 6: DOCUMENTS & ATTACHMENTS (Receipt, Vendor Invoice & Supporting Docs) ---
print("\n--- TEST 6: Supporting Documents & Attachments ---")
res_docs = client.post(
    "/expenses",
    headers=headers,
    json={
        "category": "Office Expenses",
        "amount": 3200.0,
        "vendor_invoice_url": "/files/invoice_scan_001.pdf",
        "supporting_documents": [
            {"name": "quotation_comparison.pdf", "url": "/files/supp_doc_1.pdf"},
            {"name": "delivery_challan.png", "url": "/files/supp_doc_2.png"},
        ],
    },
)
check("Create expense with multi-document attachments (HTTP 201)", res_docs.status_code == 201)
exp_d = res_docs.json()
check("Vendor invoice URL persisted", exp_d["vendor_invoice_url"] == "/files/invoice_scan_001.pdf")
check("Supporting documents array persisted (2 items)", len(exp_d["supporting_documents"]) == 2)

# Test receipt upload endpoint
receipt_res = client.post(
    f"/expenses/{exp_d['id']}/receipt",
    headers=headers,
    files={"file": ("tax_invoice.pdf", io.BytesIO(b"%PDF-1.4 test invoice content"), "application/pdf")},
)
check("Upload receipt via POST /expenses/{id}/receipt (HTTP 200)", receipt_res.status_code == 200)
check("Receipt URL assigned on expense", "/files/" in receipt_res.json()["receipt_url"])

# --- TEST 7: RECURRING EXPENSES & RECURRENCE PROCESSING ---
print("\n--- TEST 7: Recurring Expenses & Automation ---")
now_iso = datetime.now(timezone.utc).isoformat()
res_rec = client.post(
    "/expenses",
    headers=headers,
    json={
        "category": "Rent",
        "amount": 25000.0,
        "description": "Monthly Office Space Rent",
        "is_recurring": True,
        "recurrence_frequency": "monthly",
        "next_due_date": now_iso,  # Due now for testing batch processing
    },
)
check("Create recurring expense (HTTP 201)", res_rec.status_code == 201)
exp_r = res_rec.json()
check("is_recurring is True", exp_r["is_recurring"] is True)
check("recurrence_frequency is monthly", exp_r["recurrence_frequency"] == "monthly")
check("next_due_date populated", exp_r["next_due_date"] is not None)

# Trigger recurring batch processor
res_proc = client.post("/expenses/recurring/process", headers=headers)
check("POST /expenses/recurring/process succeeds (HTTP 200)", res_proc.status_code == 200)
generated_list = res_proc.json()
check("At least 1 child expense generated from recurring template", len(generated_list) >= 1)
child = next((e for e in generated_list if e["amount"] == 25000.0), None)
check("Child expense has description prefix 'Recurring:'", child is not None and child["description"].startswith("Recurring:"))
check("Child expense has status 'pending'", child is not None and child["status"] == "pending")

# Verify template's next_due_date has advanced
updated_template = client.get(f"/expenses/{exp_r['id']}", headers=headers).json()
check("Template next_due_date advanced into future", updated_template["next_due_date"] > now_iso)

# --- TEST 8: APPROVAL WORKFLOW & STATUS RESTRICTIONS ---
print("\n--- TEST 8: Approval Workflow & Restrictions ---")
# 8A: Approve
res_app = client.patch(f"/expenses/{exp1['id']}/approve", headers=headers)
check("Approve expense (HTTP 200)", res_app.status_code == 200)
check("Status updated to approved", res_app.json()["status"] == "approved")
check("Approval status updated to Approved", res_app.json()["approval_status"] == "Approved")

# 8B: Approved expense cannot be edited
res_edit_app = client.patch(f"/expenses/{exp1['id']}", headers=headers, json={"amount": 2000.0})
check("Approved expense cannot be edited (HTTP 400)", res_edit_app.status_code == 400)

# 8C: Clarification workflow
res_new = client.post("/expenses", headers=headers, json={"category": "Staff Expenses", "amount": 800.0}).json()
res_clar = client.patch(f"/expenses/{res_new['id']}/request-clarification", headers=headers, json={"reason": "Please attach GST invoice"})
check("Request clarification (HTTP 200)", res_clar.status_code == 200)
check("Status is clarification_requested", res_clar.json()["status"] == "clarification_requested")

# Editing clarification-requested expense resubmits it as pending
res_resub = client.patch(f"/expenses/{res_new['id']}", headers=headers, json={"description": "Added detailed bill reference"})
check("Editing clarification-requested expense succeeds (HTTP 200)", res_resub.status_code == 200)
check("Status resets back to pending upon edit", res_resub.json()["status"] == "pending")

# 8D: Reject expense
res_rej = client.patch(f"/expenses/{res_new['id']}/reject", headers=headers, json={"reason": "Non-business expense"})
check("Reject expense (HTTP 200)", res_rej.status_code == 200)
check("Status is rejected", res_rej.json()["status"] == "rejected")
check("Reject reason recorded", res_rej.json()["reject_reason"] == "Non-business expense")

# --- TEST 9: TENANT ISOLATION ---
print("\n--- TEST 9: Multi-Tenant Security & Isolation ---")
# Attempt to attach Org 2's vendor to Org 1 expense
res_cross_vendor = client.post(
    "/expenses",
    headers=headers,
    json={"category": "Office Expenses", "amount": 500.0, "vendor_id": supplier2_id},
)
check("Attaching foreign organization vendor is rejected with HTTP 400", res_cross_vendor.status_code == 400)

# --- TEST 10: EXTENDED LIST FILTERS ---
print("\n--- TEST 10: List Filters ---")
res_filter_v = client.get(f"/expenses?vendor_id={supplier1_id}", headers=headers)
check("Filter by vendor_id returns 200", res_filter_v.status_code == 200)
check("Filtered list contains vendor expenses", all(e["vendor_id"] == supplier1_id for e in res_filter_v.json()))

res_filter_br = client.get("/expenses?branch_id=BR-MUMBAI-01", headers=headers)
check("Filter by branch_id returns 200", res_filter_br.status_code == 200)
check("Filtered list contains branch expenses", any(e["branch_id"] == "BR-MUMBAI-01" for e in res_filter_br.json()))

# --- TEST 11: DELETE EXPENSE ---
print("\n--- TEST 11: Delete Expense ---")
del_target = client.post("/expenses", headers=headers, json={"category": "Parking", "amount": 50.0}).json()
res_del = client.delete(f"/expenses/{del_target['id']}", headers=headers)
check("Delete expense (HTTP 204)", res_del.status_code == 204)
check("Deleted expense cannot be retrieved (HTTP 404)", client.get(f"/expenses/{del_target['id']}", headers=headers).status_code == 404)

print("\n=======================================================")
print(f"RESULTS: {passed_count} passed, {failed_count} failed")
print("=======================================================\n")

if failed_count > 0:
    sys.exit(1)
