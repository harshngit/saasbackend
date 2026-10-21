"""Test suite proving Invoice PDF settings parity with preview rendering,
custom column configurations, delivery vehicle resolution, and PO/E-Way bill visibility.
"""

import io
import os
import re
import sys
import uuid
import zlib
from datetime import datetime, timezone

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from app.main import app
from app.core.database import SessionLocal
from app.models import (
    Organization,
    User,
    Customer,
    Invoice,
    InvoiceItem,
    Product,
    ProductVariant,
    SalesOrder,
    SalesOrderItem,
    Delivery,
    Vehicle,
)
from app.models.enums import UserRole, OrganizationStatus
from app.core.security import hash_password, create_access_token
from app.core import workflow
from app.core.pdf_docs import invoice_detailed_pdf, invoice_simple_pdf

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


def extract_pdf_text_and_streams(pdf_bytes: bytes) -> str:
    """Extract raw decompressed text streams from a generated PDF byte array."""
    decompressed_parts = []
    # FPDF embeds compressed content streams between stream and endstream
    stream_matches = re.finditer(b"stream[\r\n]+(.*?)[\r\n]+endstream", pdf_bytes, re.DOTALL)
    for m in stream_matches:
        raw_stream = m.group(1)
        try:
            decompressed = zlib.decompress(raw_stream)
            decompressed_parts.append(decompressed.decode("latin1", errors="replace"))
        except Exception:
            # Not zlib compressed or already uncompressed
            decompressed_parts.append(raw_stream.decode("latin1", errors="replace"))
    
    full_text = " ".join(decompressed_parts)
    # Also include the overall raw pdf (for font names, metadata, etc.)
    full_text += " " + pdf_bytes.decode("latin1", errors="replace")
    return full_text


def setup_fixtures(db):
    suffix = uuid.uuid4().hex[:8]
    org = Organization(
        id=str(uuid.uuid4()),
        name=f"Parity Org {suffix}",
        status=OrganizationStatus.ACTIVE,
        currency="INR",
        gst_number="29ABCDE1234F1Z5",
        address="123 Business Park, Koramangala",
        phone="9876543210",
        email=f"parity_{suffix}@test.com",
        bank_name="HDFC Bank",
        bank_account_holder="Parity Corp",
        bank_account_details="50200012345678",
        bank_ifsc="HDFC0001234",
    )
    db.add(org)
    db.flush()

    user = User(
        id=str(uuid.uuid4()),
        organization_id=org.id,
        email=f"admin_{suffix}@parity.com",
        name=f"Admin {suffix}",
        password_hash=hash_password("Secret123!"),
        role=UserRole.ADMIN,
        is_active=True,
    )
    db.add(user)
    db.flush()

    customer = Customer(
        id=str(uuid.uuid4()),
        organization_id=org.id,
        name="Hotel Grand Meridian",
        business_name="Grand Meridian Hospitality",
        phone="9123456789",
        gst_number="29XYZAB1234C1Z1",
        billing_address="45 Residency Road, Bangalore",
        delivery_address="45 Residency Road, Bangalore",
    )
    db.add(customer)
    db.flush()

    product = Product(
        id=str(uuid.uuid4()),
        organization_id=org.id,
        name="20L Water Jar (Refill)",
        sku=f"WTR-{suffix}",
        hsn_code="22011010",
        tax_rate=5.0,
        price=90.0,
    )
    db.add(product)
    db.flush()

    order = SalesOrder(
        id=str(uuid.uuid4()),
        organization_id=org.id,
        order_number=f"SO-{suffix}",
        customer_id=customer.id,
        customer_po_number="PO-98765-CLIENT",
        total=4960.0,
        subtotal=4600.0,
        status="completed",
    )
    db.add(order)
    db.flush()

    vehicle = Vehicle(
        id=str(uuid.uuid4()),
        organization_id=org.id,
        vehicle_number="KA-04-E-9999",
        vehicle_type="Tata Ace",
    )
    db.add(vehicle)
    db.flush()

    delivery = Delivery(
        id=str(uuid.uuid4()),
        organization_id=org.id,
        delivery_note_number=f"DN-{suffix}",
        sales_order_id=order.id,
        vehicle_id=vehicle.id,
        status="delivered",
    )
    db.add(delivery)
    db.flush()

    invoice = Invoice(
        id=str(uuid.uuid4()),
        organization_id=org.id,
        invoice_number=f"INV-{suffix}",
        order_id=order.id,
        customer_id=customer.id,
        delivery_id=delivery.id,
        invoice_date=datetime.now(timezone.utc),
        due_date=datetime.now(timezone.utc),
        billing_address="45 Residency Road, Bangalore",
        subtotal=4600.0,
        tax=360.0,
        total=4960.0,
        amount_paid=0.0,
        eway_bill_number="EWB-889900112233",
        vehicle_number=None,  # Intentionally null to test fallback to delivery relationship
        status="completed",
        payment_status="unpaid",
    )
    db.add(invoice)
    db.flush()

    invoice_item = InvoiceItem(
        id=str(uuid.uuid4()),
        invoice_id=invoice.id,
        product_id=product.id,
        product_name=product.name,
        hsn_code="22011010",
        quantity=40.0,
        unit_price=90.0,
        tax_rate=5.0,
        tax=180.0,
        line_total=3780.0,
    )
    db.add(invoice_item)
    db.commit()

    token = create_access_token(user.id, "admin", org.id)
    headers = {"Authorization": f"Bearer {token}"}
    return org, user, customer, order, delivery, invoice, headers


def run_all_tests():
    db = SessionLocal()
    try:
        org, user, customer, order, delivery, invoice, headers = setup_fixtures(db)

        print("\n=======================================================")
        print("TEST SUITE: Invoice PDF Settings Parity & Field Visibility")
        print("=======================================================")

        # -------------------------------------------------------------
        # 1. API SETTINGS PRESERVATION & NORMALIZATION
        # -------------------------------------------------------------
        print("\n--- 1. Settings Normalization & Persistence ---")
        get_res = client.get("/invoice-settings", headers=headers)
        if get_res.status_code == 200:
            ok("GET /invoice-settings returns 200")
        else:
            fail("GET /invoice-settings failed", str(get_res.status_code))
        
        settings_data = get_res.json()
        if "invoice_details" in settings_data and "typography" in settings_data:
            ok("Settings contains nested invoice_details and typography")
        else:
            fail("Missing nested blocks in settings response")

        # Update nested blocks: PO, E-way, vehicle, typography, item table
        patch_payload = {
            "template": "classic",
            "paper_size": "A4",
            "typography": {
                "font_family": "Times",
                "heading_size": 18,
                "body_size": 10,
                "table_size": 9,
            },
            "invoice_details": {
                "show_invoice_number": True,
                "show_invoice_date": True,
                "show_due_date": True,
                "show_order_reference": True,
                "show_po_number": True,
                "show_eway_bill_number": True,
                "show_vehicle_number": True,
            },
            "payment_details": {
                "show_bank_details": True,
                "show_upi_qr": True,
            },
            "item_table": {
                "columns": ["product", "hsn_sac", "quantity", "rate", "amount"],
            },
            "thermal_print": {
                "auto_cut": True,
                "open_cash_drawer": True,
                "copies": 2,
            },
        }

        patch_res = client.patch("/invoice-settings", json=patch_payload, headers=headers)
        if patch_res.status_code == 200:
            ok("PATCH /invoice-settings with full nested config succeeds")
        else:
            fail("PATCH /invoice-settings failed", patch_res.text)

        patched = patch_res.json()
        if patched["invoice_details"]["show_po_number"] is True:
            ok("Persisted show_po_number=True in invoice_details")
        else:
            fail("Failed to persist show_po_number")

        if patched["thermal_print"]["auto_cut"] is True and patched["thermal_print"]["copies"] == 2:
            ok("Preserved hardware-only settings (auto_cut, copies) in database without failure")
        else:
            fail("Failed to preserve thermal hardware settings")

        # -------------------------------------------------------------
        # 2. PDF GENERATION WITH PO, E-WAY BILL, AND VEHICLE RESOLUTION
        # -------------------------------------------------------------
        print("\n--- 2. PDF Rendering with PO, E-Way Bill & Delivery Vehicle ---")
        base_settings = workflow.invoice_settings(org)
        # Enable po, eway, vehicle
        active_settings = {
            **base_settings,
            "invoice_details": {
                **base_settings.get("invoice_details", {}),
                "show_po_number": True,
                "show_eway_bill_number": True,
                "show_vehicle_number": True,
            },
            "payment_details": {
                "show_bank_details": True,
                "show_upi_qr": False,
            },
        }

        # Generate Detailed PDF
        detailed_bytes = invoice_detailed_pdf(org, customer, invoice, active_settings)
        detailed_text = extract_pdf_text_and_streams(detailed_bytes)

        if "PO-98765-CLIENT" in detailed_text:
            ok("Customer PO number 'PO-98765-CLIENT' rendered in Detailed PDF")
        else:
            fail("Customer PO number missing from Detailed PDF")

        if "EWB-889900112233" in detailed_text:
            ok("E-Way Bill number 'EWB-889900112233' rendered in Detailed PDF")
        else:
            fail("E-Way Bill number missing from Detailed PDF")

        if "KA-04-E-9999" in detailed_text:
            ok("Vehicle number 'KA-04-E-9999' resolved from Delivery relationship in Detailed PDF")
        else:
            fail("Vehicle number missing from Detailed PDF")

        if "HDFC Bank" in detailed_text and "50200012345678" in detailed_text:
            ok("Bank details rendered when show_bank_details=True in Detailed PDF")
        else:
            fail("Bank details missing from Detailed PDF")

        # Generate Simple PDF
        simple_bytes = invoice_simple_pdf(org, customer, invoice, active_settings)
        simple_text = extract_pdf_text_and_streams(simple_bytes)

        if "PO-98765-CLIENT" in simple_text:
            ok("Customer PO number rendered in Simple PDF")
        else:
            fail("Customer PO number missing from Simple PDF")

        if "EWB-889900112233" in simple_text:
            ok("E-Way Bill number rendered in Simple PDF")
        else:
            fail("E-Way Bill number missing from Simple PDF")

        if "KA-04-E-9999" in simple_text:
            ok("Vehicle number resolved from Delivery relationship in Simple PDF")
        else:
            fail("Vehicle number missing from Simple PDF")

        # The simple format follows the frontend's simple view (InvoicePrintView.jsx):
        # payment details are hidden there; the detailed format (checked above) keeps them.
        if "HDFC Bank" not in simple_text and "50200012345678" not in simple_text:
            ok("Bank details omitted from Simple PDF, matching the frontend simple view")
        else:
            fail("Bank details printed on Simple PDF")

        # -------------------------------------------------------------
        # 3. DIRECT INVOICE VEHICLE OVERRIDE
        # -------------------------------------------------------------
        print("\n--- 3. Direct Invoice Vehicle Number Override ---")
        invoice.vehicle_number = "DL-01-XY-5555"
        db.commit()

        override_detailed = invoice_detailed_pdf(org, customer, invoice, active_settings)
        override_text = extract_pdf_text_and_streams(override_detailed)

        if "DL-01-XY-5555" in override_text:
            ok("Direct invoice.vehicle_number 'DL-01-XY-5555' takes precedence over delivery vehicle")
        else:
            fail("Direct vehicle number did not override delivery vehicle")

        # -------------------------------------------------------------
        # 4. TOGGLING VISIBILITY OFF
        # -------------------------------------------------------------
        print("\n--- 4. Toggling Visibility Off Completely Removes Fields ---")
        hidden_settings = {
            **base_settings,
            "invoice_details": {
                **base_settings.get("invoice_details", {}),
                "show_po_number": False,
                "show_eway_bill_number": False,
                "show_vehicle_number": False,
            },
            "payment_details": {
                "show_bank_details": False,
            },
        }

        hidden_detailed = invoice_detailed_pdf(org, customer, invoice, hidden_settings)
        hidden_text = extract_pdf_text_and_streams(hidden_detailed)

        if "PO-98765-CLIENT" not in hidden_text:
            ok("PO number excluded when show_po_number=False")
        else:
            fail("PO number still appeared when toggled off")

        if "EWB-889900112233" not in hidden_text:
            ok("E-Way Bill excluded when show_eway_bill_number=False")
        else:
            fail("E-Way Bill still appeared when toggled off")

        if "DL-01-XY-5555" not in hidden_text and "KA-04-E-9999" not in hidden_text:
            ok("Vehicle number excluded when show_vehicle_number=False")
        else:
            fail("Vehicle number still appeared when toggled off")

        if "50200012345678" not in hidden_text:
            ok("Bank account details excluded when show_bank_details=False")
        else:
            fail("Bank account details still appeared when toggled off")

        # -------------------------------------------------------------
        # 5. TYPOGRAPHY & STREAM DIFF PROOF
        # -------------------------------------------------------------
        print("\n--- 5. Typography Settings Alter Generated PDF Stream ---")
        helvetica_settings = {
            **base_settings,
            "typography": {
                "font_family": "Helvetica",
                "heading_size": 16,
                "body_size": 9,
                "table_size": 8,
            },
        }
        times_settings = {
            **base_settings,
            "typography": {
                "font_family": "Times",
                "heading_size": 22,
                "body_size": 12,
                "table_size": 11,
            },
        }

        helv_bytes = invoice_detailed_pdf(org, customer, invoice, helvetica_settings)
        times_bytes = invoice_detailed_pdf(org, customer, invoice, times_settings)

        if helv_bytes != times_bytes:
            ok("Font and typography changes produce distinct PDF binary streams")
        else:
            fail("Typography changes produced identical PDF streams")

        helv_decomp = extract_pdf_text_and_streams(helv_bytes)
        times_decomp = extract_pdf_text_and_streams(times_bytes)

        if "Helvetica" in helv_decomp and "Times" in times_decomp:
            ok("PDF font dictionaries reflect Helvetica vs Times configuration")
        else:
            fail("Font descriptors not found in PDF stream")

        # -------------------------------------------------------------
        # 6. MARGIN & PAPER SIZE STREAM PROOF
        # -------------------------------------------------------------
        print("\n--- 6. Regular Print Margins and Paper Sizes Alter PDF ---")
        a4_settings = {
            **base_settings,
            "regular_print": {
                "paper_size": "A4",
                "orientation": "portrait",
                "margin_top": 10.0,
                "margin_left": 10.0,
            },
        }
        a5_settings = {
            **base_settings,
            "regular_print": {
                "paper_size": "A5",
                "orientation": "landscape",
                "margin_top": 25.0,
                "margin_left": 30.0,
            },
        }

        a4_bytes = invoice_detailed_pdf(org, customer, invoice, a4_settings)
        a5_bytes = invoice_detailed_pdf(org, customer, invoice, a5_settings)

        if len(a4_bytes) != len(a5_bytes) or a4_bytes != a5_bytes:
            ok("Paper size and margin changes alter PDF stream dimensions and content")
        else:
            fail("A4 portrait and A5 landscape generated identical streams")

        # -------------------------------------------------------------
        # 7. ENDPOINT DOWNLOAD TEST: GET /invoices/{id}/pdf
        # -------------------------------------------------------------
        print("\n--- 7. Download API Endpoint Tests ---")
        ep_detailed = client.get(f"/invoices/{invoice.id}/pdf?format=detailed", headers=headers)
        if ep_detailed.status_code == 200 and ep_detailed.headers["content-type"] == "application/pdf":
            ok("GET /invoices/{id}/pdf?format=detailed returns 200 with application/pdf")
        else:
            fail("GET /invoices/{id}/pdf?format=detailed failed", str(ep_detailed.status_code))

        ep_simple = client.get(f"/invoices/{invoice.id}/pdf?format=simple", headers=headers)
        if ep_simple.status_code == 200 and ep_simple.headers["content-type"] == "application/pdf":
            ok("GET /invoices/{id}/pdf?format=simple returns 200 with application/pdf")
        else:
            fail("GET /invoices/{id}/pdf?format=simple failed", str(ep_simple.status_code))

        print("\n=======================================================")
        print(f"RESULTS: {_passed} PASSED, {_failed} FAILED")
        print("=======================================================\n")
        assert _failed == 0, f"{_failed} tests failed!"

    finally:
        db.close()


if __name__ == "__main__":
    run_all_tests()
