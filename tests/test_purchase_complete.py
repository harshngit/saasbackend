"""Comprehensive automated test suite for the Complete Purchase Profile & Management System."""

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
from app.models.organization import Organization
from app.models.product import Product, ProductVariant
from app.models.supplier import Supplier
from app.models.user import User
from app.models.warehouse import Warehouse
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


def register_org(name_prefix: str = "Purchase Firm"):
    email = f"admin_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={
        "organization_name": f"{name_prefix} {uuid.uuid4().hex[:6]}",
        "admin_name": "Purchase Admin",
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


print("\n=======================================================")
print("TEST SUITE: Complete Purchase Profile & Management System")
print("=======================================================\n")

headers1, org1_id = register_org("Steel Corp")
headers2, org2_id = register_org("Foreign Motors")

# Setup Test Data in Org 1
db = SessionLocal()
try:
    # Warehouse 1
    wh1 = Warehouse(organization_id=org1_id, name="Central Warehouse", code="WH-CENTRAL-01", address="100 Dockside Rd")
    db.add(wh1)

    # Supplier 1
    sup1 = Supplier(
        organization_id=org1_id,
        name="Apex Industrial Supplies",
        contact_person="Vikas Khanna",
        phone="9876543210",
        email="vikas@apexind.com",
        gst_number="27AAAAA1234A1Z5",
        address="Plot 45, Industrial Zone, Pune",
        category="Hardware",
    )
    db.add(sup1)

    # Product 1
    prod1 = Product(
        organization_id=org1_id,
        name="Steel Beam 10ft",
        sku="ST-BEAM-10",
        barcode="8901234567890",
        uom="piece",
        price=1500.0,
        total_inventory=0,
    )
    db.add(prod1)

    # Product 2 with Variant
    prod2 = Product(
        organization_id=org1_id,
        name="Industrial Motor Oil",
        sku="OIL-IND-5L",
        barcode="8909876543210",
        uom="can",
        price=800.0,
        total_inventory=0,
    )
    db.add(prod2)
    db.flush()

    var2 = ProductVariant(
        product_id=prod2.id,
        name="Synthetic 5W-40",
        sku="OIL-SYN-5W40",
        barcode="8909876543211",
        price=950.0,
        inventory=0,
    )
    db.add(var2)

    # Org 2 Data (for tenant isolation tests)
    sup2 = Supplier(
        organization_id=org2_id,
        name="Foreign Vendor Ltd",
        contact_person="Foreign Rep",
        phone="9123456789",
        email="vendor@foreign.com",
    )
    db.add(sup2)

    prod_org2 = Product(
        organization_id=org2_id,
        name="Foreign Gearbox",
        sku="GB-FOR-01",
        price=5000.0,
    )
    db.add(prod_org2)

    db.commit()
    db.refresh(wh1)
    db.refresh(sup1)
    db.refresh(prod1)
    db.refresh(prod2)
    db.refresh(var2)
    db.refresh(sup2)
    db.refresh(prod_org2)

    wh1_id = wh1.id
    sup1_id = sup1.id
    prod1_id = prod1.id
    prod2_id = prod2.id
    var2_id = var2.id
    sup2_id = sup2.id
    prod_org2_id = prod_org2.id
finally:
    db.close()

# --- TEST 1: BASIC INFORMATION & AUTO-NUMBERING ---
print("--- TEST 1: Basic Information & Auto-Numbering ---")
res1 = client.post(
    "/purchases",
    headers=headers1,
    json={
        "invoice_number": "APEX-INV-9901",
        "supplier_id": sup1_id,
        "purchase_type": "Purchase Order",
        "financial_year": "2026-2027",
        "reference_number": "REF-QUOT-8822",
        "notes": "Urgent procurement for Project Alpha",
        "items": [
            {"product_id": prod1_id, "quantity": 10, "purchase_price": 1200.0}
        ],
    },
)
check("Create purchase invoice with basic info (HTTP 201)", res1.status_code == 201)
pur1 = res1.json()
check("Purchase ID generated with PURID prefix", pur1["purchase_id"].startswith("PURID"))
check("Purchase Number generated with PUR prefix", pur1["purchase_number"].startswith("PUR"))
check("GRN Number generated with GRN prefix", pur1["grn_number"].startswith("GRN"))
check("Purchase Type is Purchase Order", pur1["purchase_type"] == "Purchase Order")
check("Financial Year is 2026-2027", pur1["financial_year"] == "2026-2027")
check("Reference Number is REF-QUOT-8822", pur1["reference_number"] == "REF-QUOT-8822")
check("Initial status is pending", pur1["status"] == "pending")

# --- TEST 2: SUPPLIER DETAILS AUTO-FILL & SHIPPING ADDRESS ---
print("\n--- TEST 2: Supplier Details Auto-fill & Addresses ---")
check("Supplier contact person auto-filled", pur1["contact_person"] == "Vikas Khanna")
check("Supplier phone auto-filled", pur1["mobile_number"] == "9876543210")
check("Supplier email auto-filled", pur1["email_address"] == "vikas@apexind.com")
check("Supplier GSTIN auto-filled", pur1["payee_gstin"] == "27AAAAA1234A1Z5")
check("Supplier billing address auto-filled", pur1["billing_address"] == "Plot 45, Industrial Zone, Pune")

# Manual override of supplier details and shipping address
res_supp_override = client.post(
    "/purchases",
    headers=headers1,
    json={
        "invoice_number": "APEX-INV-9902",
        "supplier_id": sup1_id,
        "contact_person": "Alternate Manager Amit",
        "mobile_number": "9112233445",
        "shipping_address": "Dock 4, Port Warehouse, Mumbai",
        "items": [{"product_id": prod1_id, "quantity": 5, "purchase_price": 1200.0}],
    },
)
check("Create purchase with manual address override (HTTP 201)", res_supp_override.status_code == 201)
pur_so = res_supp_override.json()
check("Custom contact person persisted", pur_so["contact_person"] == "Alternate Manager Amit")
check("Custom shipping address persisted", pur_so["shipping_address"] == "Dock 4, Port Warehouse, Mumbai")

# --- TEST 3: REPEATABLE PURCHASE ITEMS & SERVER-SIDE LINE MATH ---
print("\n--- TEST 3: Repeatable Items & Server-Side Calculations ---")
res_items = client.post(
    "/purchases",
    headers=headers1,
    json={
        "invoice_number": "APEX-INV-9903",
        "supplier_id": sup1_id,
        "items": [
            {
                "product_id": prod1_id,
                "product_code": "ST-BEAM-10-HEAVY",
                "barcode": "8901234567890",
                "description": "Heavy Duty Steel Beam",
                "warehouse_id": wh1_id,
                "quantity": 5,
                "purchase_price": 2000.0,
                "discount_percent": 10.0,  # 10% on 10,000 = 1,000 discount. Taxable = 9,000
                "tax_rate": 18.0,          # 18% on 9,000 = 1,620 tax. Line total = 10,620
                "batch_number": "BATCH-ST-2026",
                "serial_numbers": ["SN001", "SN002", "SN003", "SN004", "SN005"],
            },
            {
                "product_id": prod2_id,
                "variant_id": var2_id,
                "warehouse_id": wh1_id,
                "quantity": 10,
                "purchase_price": 900.0,
                "discount": 500.0,          # Flat discount 500. Subtotal 9000 - 500 = 8500
                "tax_rate": 18.0,           # 18% on 8500 = 1530 tax. Line total = 10,030
            },
        ],
    },
)
check("Create purchase with item discounts & taxes (HTTP 201)", res_items.status_code == 201)
pur_it = res_items.json()
check("Contains 2 line items", len(pur_it["items"]) == 2)
# Line 1 verification
l1 = pur_it["items"][0]
check("Line 1 discount calculated from 10%: 1000.0", l1["discount"] == 1000.0)
check("Line 1 tax calculated from 18%: 1620.0", l1["tax"] == 1620.0)
check("Line 1 total: 10620.0", l1["line_total"] == 10620.0)
check("Line 1 batch number persisted", l1["batch_number"] == "BATCH-ST-2026")
check("Line 1 serial numbers persisted", l1["serial_numbers"] == ["SN001", "SN002", "SN003", "SN004", "SN005"])

# Line 2 verification
l2 = pur_it["items"][1]
check("Line 2 discount: 500.0", l2["discount"] == 500.0)
check("Line 2 tax calculated from 18%: 1530.0", l2["tax"] == 1530.0)
check("Line 2 total: 10030.0", l2["line_total"] == 10030.0)

# Header Totals:
# Subtotal = (5 * 2000) + (10 * 900) = 10,000 + 9,000 = 19,000.0
# Item Discounts = 1000 + 500 = 1500.0
# Item Taxes = 1620 + 1530 = 3150.0
# Grand Total = (19000 - 1500) + 3150 = 20,650.0
check("Header subtotal: 19000.0", pur_it["subtotal"] == 19000.0)
check("Header tax: 3150.0", pur_it["tax"] == 3150.0)
check("Header grand total: 20650.0", pur_it["total"] == 20650.0)

# --- TEST 4: ADDITIONAL CHARGES (Freight, Packing, Insurance, Other, Round-off) ---
print("\n--- TEST 4: Additional Charges & Grand Total Math ---")
res_charges = client.post(
    "/purchases",
    headers=headers1,
    json={
        "invoice_number": "APEX-INV-9904",
        "supplier_id": sup1_id,
        "discount": 200.0,            # Overall invoice discount
        "freight_charges": 500.0,
        "packing_charges": 150.0,
        "insurance_charges": 100.0,
        "other_charges": 50.0,
        "round_off": -0.50,
        "items": [
            {"product_id": prod1_id, "quantity": 10, "purchase_price": 1000.0, "tax_rate": 18.0}
        ],
    },
)
check("Create purchase with additional charges (HTTP 201)", res_charges.status_code == 201)
pur_ch = res_charges.json()
# Math:
# Subtotal = 10,000.0
# Taxable = 10,000 - 200 = 9,800.0
# Tax 18% = 1,764.0 (18% on 9800)
# Subtotal (10000) - Overall Disc (200) + Tax (1800 on item: 10000*0.18=1800)
# Item tax = 1800.0
# Charges: + 500 (freight) + 150 (packing) + 100 (insurance) + 50 (other) - 0.50 (round_off) = + 799.50
# Grand total = (10000 - 200) + 1800 + 799.50 = 12,399.50
check("Freight charges persisted", pur_ch["freight_charges"] == 500.0)
check("Packing charges persisted", pur_ch["packing_charges"] == 150.0)
check("Insurance charges persisted", pur_ch["insurance_charges"] == 100.0)
check("Other charges persisted", pur_ch["other_charges"] == 50.0)
check("Round off persisted", pur_ch["round_off"] == -0.50)
check("Grand total includes all charges correctly: 12399.50", pur_ch["total"] == 12399.50)

# --- TEST 5: INPUT VALIDATION CHECKS ---
print("\n--- TEST 5: Input Validation Checks ---")
res_neg_qty = client.post(
    "/purchases",
    headers=headers1,
    json={"invoice_number": "INV-ERR-1", "supplier_id": sup1_id, "items": [{"product_id": prod1_id, "quantity": -5, "purchase_price": 100}]},
)
check("Reject negative quantity (HTTP 400/422)", res_neg_qty.status_code in (400, 422))

res_neg_price = client.post(
    "/purchases",
    headers=headers1,
    json={"invoice_number": "INV-ERR-2", "supplier_id": sup1_id, "items": [{"product_id": prod1_id, "quantity": 5, "purchase_price": -100}]},
)
check("Reject negative unit cost (HTTP 400/422)", res_neg_price.status_code in (400, 422))

res_neg_charge = client.post(
    "/purchases",
    headers=headers1,
    json={"invoice_number": "INV-ERR-3", "supplier_id": sup1_id, "freight_charges": -50.0, "items": [{"product_id": prod1_id, "quantity": 1, "purchase_price": 100}]},
)
check("Reject negative freight charges (HTTP 400/422)", res_neg_charge.status_code in (400, 422))

res_empty_items = client.post(
    "/purchases",
    headers=headers1,
    json={"invoice_number": "INV-ERR-4", "supplier_id": sup1_id, "items": []},
)
check("Reject purchase without items (HTTP 400/422)", res_empty_items.status_code in (400, 422))

# --- TEST 6: GOODS RECEIPT & WAREHOUSE ---
print("\n--- TEST 6: Goods Receipt & Receiving Status ---")
res_grn = client.post(
    "/purchases",
    headers=headers1,
    json={
        "invoice_number": "APEX-INV-9905",
        "supplier_id": sup1_id,
        "warehouse_id": wh1_id,
        "grn_number": "GRN-CUSTOM-001",
        "receiving_status": "Pending",
        "items": [{"product_id": prod1_id, "quantity": 10, "purchase_price": 500.0}],
    },
)
check("Create purchase with warehouse & GRN (HTTP 201)", res_grn.status_code == 201)
pur_gr = res_grn.json()
check("Warehouse ID persisted", pur_gr["warehouse_id"] == wh1_id)
check("GRN Number persisted", pur_gr["grn_number"] == "GRN-CUSTOM-001")
check("Receiving status is Pending", pur_gr["receiving_status"] == "Pending")

# --- TEST 7: PAYMENT DETAILS & OUTSTANDING BALANCE ---
print("\n--- TEST 7: Payment Details & Outstanding Balance ---")
res_pay = client.post(
    "/purchases",
    headers=headers1,
    json={
        "invoice_number": "APEX-INV-9906",
        "supplier_id": sup1_id,
        "payment_method": "bank_transfer",
        "payment_terms": "Net 30",
        "payment_reference": "UTR-AXIS-998877",
        "amount_paid": 2000.0,
        "items": [{"product_id": prod1_id, "quantity": 5, "purchase_price": 1000.0}],  # Total 5000.0
    },
)
check("Create purchase with payment details (HTTP 201)", res_pay.status_code == 201)
pur_p = res_pay.json()
check("Payment method is bank_transfer", pur_p["payment_method"] == "bank_transfer")
check("Payment terms is Net 30", pur_p["payment_terms"] == "Net 30")
check("Payment reference is UTR-AXIS-998877", pur_p["payment_reference"] == "UTR-AXIS-998877")
check("Payment status is partial", pur_p["payment_status"] == "partial")
check("Amount paid is 2000.0", pur_p["amount_paid"] == 2000.0)
check("Outstanding balance computed: 3000.0", pur_p["outstanding_balance"] == 3000.0)

# Update payment status
res_pay_up = client.patch(
    f"/purchases/{pur_p['id']}/payment-status",
    headers=headers1,
    json={"payment_status": "paid", "amount_paid": 5000.0, "payment_reference": "UTR-AXIS-998878"},
)
check("Update payment status to paid (HTTP 200)", res_pay_up.status_code == 200)
check("Payment status updated to paid", res_pay_up.json()["payment_status"] == "paid")
check("Outstanding balance is now 0.0", res_pay_up.json()["outstanding_balance"] == 0.0)

# --- TEST 8: ACCOUNTING, TERMS & TAGS ---
print("\n--- TEST 8: Accounting, Terms & Conditions, and Tags ---")
res_acct = client.post(
    "/purchases",
    headers=headers1,
    json={
        "invoice_number": "APEX-INV-9907",
        "supplier_id": sup1_id,
        "purchase_account_id": "ACC-PURCHASE-GEN",
        "tax_category": "GST 18%",
        "cost_center_id": "CC-FABRICATION",
        "project_id": "PROJ-METRO-LINE",
        "terms_and_conditions": "Payment within 30 days of inspection. 1 year warranty.",
        "tags": ["capex", "raw-materials", "q3"],
        "items": [{"product_id": prod1_id, "quantity": 2, "purchase_price": 500.0}],
    },
)
check("Create purchase with accounting & tags (HTTP 201)", res_acct.status_code == 201)
pur_ac = res_acct.json()
check("Purchase Account ID persisted", pur_ac["purchase_account_id"] == "ACC-PURCHASE-GEN")
check("Tax Category persisted", pur_ac["tax_category"] == "GST 18%")
check("Cost Center ID persisted", pur_ac["cost_center_id"] == "CC-FABRICATION")
check("Project ID persisted", pur_ac["project_id"] == "PROJ-METRO-LINE")
check("Terms & conditions persisted", "Payment within 30 days" in pur_ac["terms_and_conditions"])
check("Tags list persisted", pur_ac["tags"] == ["capex", "raw-materials", "q3"])

# --- TEST 9: APPROVAL WORKFLOW & INVENTORY STAMP ---
print("\n--- TEST 9: Approval Workflow & Inventory Inwarding ---")
# 9A: Approve
res_app = client.patch(f"/purchases/{pur1['id']}/approve", headers=headers1)
check("Approve purchase invoice (HTTP 200)", res_app.status_code == 200)
pur_app = res_app.json()
check("Status updated to approved", pur_app["status"] == "approved")
check("Approval status updated to Approved", pur_app["approval_status"] == "Approved")
check("Approved by user stamp recorded", pur_app["approved_by"] is not None)
check("Approved at timestamp recorded", pur_app["approved_at"] is not None)
check("Receiving status moved to Completed", pur_app["receiving_status"] == "Completed")

# 9B: Approved purchase cannot be edited
res_edit_app = client.patch(f"/purchases/{pur1['id']}", headers=headers1, json={"notes": "Try edit approved"})
check("Approved purchase cannot be edited (HTTP 400)", res_edit_app.status_code == 400)

# 9C: Cancellation with inventory reversal
res_to_cancel = client.post(
    "/purchases",
    headers=headers1,
    json={"invoice_number": "APEX-INV-CANCEL-1", "supplier_id": sup1_id, "items": [{"product_id": prod1_id, "quantity": 4, "purchase_price": 500.0}]},
).json()
client.patch(f"/purchases/{res_to_cancel['id']}/approve", headers=headers1)
res_canc = client.patch(f"/purchases/{res_to_cancel['id']}/cancel", headers=headers1, json={"reason": "Goods defective on delivery"})
check("Cancel approved purchase (HTTP 200)", res_canc.status_code == 200)
check("Status is cancelled", res_canc.json()["status"] == "cancelled")
check("Approval status is Rejected", res_canc.json()["approval_status"] == "Rejected")
check("Approval remarks recorded", res_canc.json()["approval_remarks"] == "Goods defective on delivery")

# --- TEST 10: DOCUMENTS & UPLOADS ---
print("\n--- TEST 10: Documents & Multi-File Attachments ---")
res_docs = client.post(
    "/purchases",
    headers=headers1,
    json={
        "invoice_number": "APEX-INV-DOCS-1",
        "supplier_id": sup1_id,
        "supplier_quotation_url": "/files/quotation_apex_01.pdf",
        "purchase_order_url": "/files/po_gen_01.pdf",
        "delivery_challan_url": "/files/challan_8822.pdf",
        "supporting_documents": [
            {"name": "test_certificate.pdf", "url": "/files/cert_1.pdf"},
            {"name": "weighbridge_slip.jpg", "url": "/files/slip_2.jpg"},
        ],
        "items": [{"product_id": prod1_id, "quantity": 1, "purchase_price": 1000.0}],
    },
)
check("Create purchase with document links (HTTP 201)", res_docs.status_code == 201)
pur_d = res_docs.json()
check("Supplier quotation URL persisted", pur_d["supplier_quotation_url"] == "/files/quotation_apex_01.pdf")
check("Purchase order URL persisted", pur_d["purchase_order_url"] == "/files/po_gen_01.pdf")
check("Delivery challan URL persisted", pur_d["delivery_challan_url"] == "/files/challan_8822.pdf")
check("Supporting documents array persisted (2 docs)", len(pur_d["supporting_documents"]) == 2)

# Test document upload endpoint
upload_res = client.post(
    f"/purchases/{pur_d['id']}/documents",
    headers=headers1,
    files={"file": ("vendor_invoice_scan.pdf", io.BytesIO(b"%PDF-1.4 scan content"), "application/pdf")},
)
check("Upload document via POST /purchases/{id}/documents (HTTP 200)", upload_res.status_code == 200)
check("Attachment URL assigned on purchase", "/files/" in upload_res.json()["attachment_url"])

# --- TEST 11: MULTI-TENANT SECURITY & ISOLATION ---
print("\n--- TEST 11: Multi-Tenant Security & Isolation ---")
# Org 1 cannot attach Org 2's supplier
res_cross_sup = client.post(
    "/purchases",
    headers=headers1,
    json={"invoice_number": "INV-CROSS-1", "supplier_id": sup2_id, "items": [{"product_id": prod1_id, "quantity": 1, "purchase_price": 100}]},
)
check("Cross-org supplier rejected with HTTP 400", res_cross_sup.status_code == 400)

# Org 1 cannot attach Org 2's product
res_cross_prod = client.post(
    "/purchases",
    headers=headers1,
    json={"invoice_number": "INV-CROSS-2", "supplier_id": sup1_id, "items": [{"product_id": prod_org2_id, "quantity": 1, "purchase_price": 100}]},
)
check("Cross-org product rejected with HTTP 400", res_cross_prod.status_code == 400)

# Org 2 cannot read Org 1's purchase invoice
res_cross_get = client.get(f"/purchases/{pur1['id']}", headers=headers2)
check("Cross-org purchase read rejected with HTTP 404", res_cross_get.status_code == 404)

# --- TEST 12: EXTENDED LIST FILTERS ---
print("\n--- TEST 12: List Query Filters ---")
res_filter_type = client.get("/purchases?purchase_type=Purchase Order", headers=headers1)
check("Filter by purchase_type returns 200", res_filter_type.status_code == 200)
check("Filtered list contains purchase orders", any(p["purchase_type"] == "Purchase Order" for p in res_filter_type.json()))

res_filter_tag = client.get("/purchases?tag=capex", headers=headers1)
check("Filter by tag returns 200", res_filter_tag.status_code == 200)
check("Filtered list contains capex tagged purchase", any("capex" in (p.get("tags") or []) for p in res_filter_tag.json()))

res_del_target = client.post(
    "/purchases",
    headers=headers1,
    json={"invoice_number": "INV-DEL-1", "supplier_id": sup1_id, "items": [{"product_id": prod1_id, "quantity": 1, "purchase_price": 50}]},
).json()
res_del = client.delete(f"/purchases/{res_del_target['id']}", headers=headers1)
check("Delete pending purchase (HTTP 204)", res_del.status_code == 204)
check("Deleted purchase cannot be found (HTTP 404)", client.get(f"/purchases/{res_del_target['id']}", headers=headers1).status_code == 404)

print("\n=======================================================")
print(f"RESULTS: {passed_count} passed, {failed_count} failed")
print("=======================================================\n")

if failed_count > 0:
    sys.exit(1)
