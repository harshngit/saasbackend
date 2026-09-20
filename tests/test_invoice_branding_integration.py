"""Comprehensive tests for invoice branding integration using existing Company Settings data."""

import io
import os
import sys
import uuid
from datetime import datetime, timezone
from PIL import Image

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from app.main import app
from app.core.database import SessionLocal
from app.models import Organization, User, Customer, Invoice, InvoiceItem, StoredFile
from app.models.enums import UserRole, OrganizationStatus
from app.core.security import hash_password, create_access_token
from app.seed import main as seed_main

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


def _create_test_image(color=(255, 0, 0), size=(64, 64), fmt="PNG") -> bytes:
    img = Image.new("RGB", size, color=color)
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def _setup_org_user(db, suffix: str):
    org = Organization(
        id=str(uuid.uuid4()),
        name=f"Brand Org {suffix}",
        status=OrganizationStatus.ACTIVE,
        currency="INR",
        gst_number="27AAAAA0000A1Z5",
        address="123 Market Street",
        phone="9876543210",
        email=f"org_{suffix}@test.com",
        upi_id=f"pay_{suffix}@upi",
    )
    db.add(org)
    db.flush()

    user = User(
        id=str(uuid.uuid4()),
        organization_id=org.id,
        email=f"admin_{suffix}@test.com",
        name=f"Admin {suffix}",
        password_hash=hash_password("Secret123!"),
        role=UserRole.ADMIN,
        is_active=True,
    )
    db.add(user)
    db.flush()

    token = create_access_token(user.id, "admin", org.id)
    headers = {"Authorization": f"Bearer {token}"}
    return org, user, headers


def run_tests():
    global _passed, _failed
    print("\n=======================================================")
    print("TEST SUITE: Invoice Branding Integration (Company Settings)")
    print("=======================================================")

    db = SessionLocal()
    try:
        org1, user1, auth1 = _setup_org_user(db, "A")
        org2, user2, auth2 = _setup_org_user(db, "B")

        # Create customer and invoice in Org 1
        cust1 = Customer(
            id=str(uuid.uuid4()),
            organization_id=org1.id,
            name="Retail Buyer",
            phone="9123456780",
            billing_address="456 High Street",
        )
        db.add(cust1)
        db.flush()

        inv1 = Invoice(
            id=str(uuid.uuid4()),
            organization_id=org1.id,
            customer_id=cust1.id,
            invoice_number="INV-BR-001",
            invoice_date=datetime.now(timezone.utc),
            status="unpaid",
            subtotal=1000.0,
            tax=180.0,
            total=1180.0,
            amount_paid=0.0,
        )
        db.add(inv1)
        db.flush()

        inv_item1 = InvoiceItem(
            id=str(uuid.uuid4()),
            invoice_id=inv1.id,
            product_name="Branded Widget",
            quantity=10,
            unit_price=100.0,
            tax_rate=18.0,
            tax=180.0,
            tax_amount=180.0,
            line_total=1180.0,
        )
        db.add(inv_item1)
        db.commit()

        print("\n--- 1. BASELINE: Invoice PDF without branding ---")
        res_baseline = client.get(f"/invoices/{inv1.id}/pdf", headers=auth1, params={"format": "detailed"})
        assert_eq(res_baseline.status_code, 200, "GET /invoices/{id}/pdf returns 200 without branding")
        assert_eq(res_baseline.content[:5], b"%PDF-", "Generated content is valid PDF")
        baseline_len = len(res_baseline.content)

        print("\n--- 2. LOGO: Fallback to Company Settings (Organization.logo_url) ---")
        logo_bytes = _create_test_image((255, 0, 0), (80, 80))
        stored_logo = StoredFile(
            id=str(uuid.uuid4()),
            organization_id=org1.id,
            filename="company_logo.png",
            content_type="image/png",
            size=len(logo_bytes),
            data=logo_bytes,
        )
        db.add(stored_logo)
        db.flush()

        # Update org1.logo_url
        org1.logo_url = f"http://testserver/files/{stored_logo.id}"
        db.commit()

        res_logo = client.get(f"/invoices/{inv1.id}/pdf", headers=auth1, params={"format": "detailed"})
        assert_eq(res_logo.status_code, 200, "PDF with Company Settings logo returns 200")
        assert_eq(res_logo.content[:5], b"%PDF-", "Content is PDF")
        # PDF with embedded image should be larger than baseline
        if len(res_logo.content) > baseline_len:
            ok("PDF size increased with Company Settings logo embedded")
        else:
            fail("PDF size did not increase with logo", f"{len(res_logo.content)} <= {baseline_len}")

        print("\n--- 3. LOGO: Explicit invoice_template_settings takes precedence ---")
        override_logo_bytes = _create_test_image((0, 255, 0), (120, 120))
        stored_override_logo = StoredFile(
            id=str(uuid.uuid4()),
            organization_id=org1.id,
            filename="override_logo.png",
            content_type="image/png",
            size=len(override_logo_bytes),
            data=override_logo_bytes,
        )
        db.add(stored_override_logo)
        db.commit()

        patch_res = client.patch(
            "/invoice-settings",
            headers=auth1,
            json={"branding": {"logo_file_id": stored_override_logo.id}},
        )
        assert_eq(patch_res.status_code, 200, "PATCH /invoice-settings with logo_file_id succeeds")

        res_override_logo = client.get(f"/invoices/{inv1.id}/pdf", headers=auth1, params={"format": "detailed"})
        assert_eq(res_override_logo.status_code, 200, "PDF with override logo returns 200")
        if res_override_logo.content != res_logo.content:
            ok("Explicit invoice_template_settings logo_file_id overrides Company Settings logo")
        else:
            fail("Override logo did not change output PDF")

        # Reset invoice settings branding override to None to verify Company Settings fallback continues working
        client.patch("/invoice-settings", headers=auth1, json={"branding": {"logo_file_id": None}})

        print("\n--- 4. SIGNATURE: Fallback to Company Settings (Organization.signature_url) ---")
        sig_bytes = _create_test_image((0, 0, 255), (100, 40))
        stored_sig = StoredFile(
            id=str(uuid.uuid4()),
            organization_id=org1.id,
            filename="authorized_signature.png",
            content_type="image/png",
            size=len(sig_bytes),
            data=sig_bytes,
        )
        db.add(stored_sig)
        db.flush()

        org1.signature_url = f"http://testserver/files/{stored_sig.id}"
        db.commit()

        res_sig = client.get(f"/invoices/{inv1.id}/pdf", headers=auth1, params={"format": "detailed"})
        assert_eq(res_sig.status_code, 200, "PDF with Company Settings signature returns 200")
        if len(res_sig.content) > len(res_logo.content):
            ok("PDF size increased with signature embedded")
        else:
            fail("PDF size did not increase with signature", f"{len(res_sig.content)} <= {len(res_logo.content)}")

        print("\n--- 5. SIGNATURE: Explicit invoice_template_settings takes precedence ---")
        override_sig_bytes = _create_test_image((128, 128, 0), (140, 50))
        stored_override_sig = StoredFile(
            id=str(uuid.uuid4()),
            organization_id=org1.id,
            filename="override_signature.png",
            content_type="image/png",
            size=len(override_sig_bytes),
            data=override_sig_bytes,
        )
        db.add(stored_override_sig)
        db.commit()

        client.patch(
            "/invoice-settings",
            headers=auth1,
            json={"branding": {"signature_file_id": stored_override_sig.id}},
        )
        res_override_sig = client.get(f"/invoices/{inv1.id}/pdf", headers=auth1, params={"format": "detailed"})
        assert_eq(res_override_sig.status_code, 200, "PDF with override signature returns 200")
        if res_override_sig.content != res_sig.content:
            ok("Explicit invoice_template_settings signature_file_id overrides Company Settings signature")
        else:
            fail("Override signature did not change output PDF")

        # Reset signature override
        client.patch("/invoice-settings", headers=auth1, json={"branding": {"signature_file_id": None}})

        print("\n--- 6. PAYMENT QR: Fallback to Company Settings (Organization.payment_qr_url) ---")
        qr_bytes = _create_test_image((0, 0, 0), (100, 100))
        stored_qr = StoredFile(
            id=str(uuid.uuid4()),
            organization_id=org1.id,
            filename="payment_qr.png",
            content_type="image/png",
            size=len(qr_bytes),
            data=qr_bytes,
        )
        db.add(stored_qr)
        db.flush()

        org1.payment_qr_url = f"http://testserver/files/{stored_qr.id}"
        db.commit()

        res_qr = client.get(f"/invoices/{inv1.id}/pdf", headers=auth1, params={"format": "detailed"})
        assert_eq(res_qr.status_code, 200, "PDF with Company Settings payment QR returns 200")
        if len(res_qr.content) > len(res_sig.content):
            ok("PDF size increased with QR image embedded")
        else:
            fail("PDF size did not increase with QR image", f"{len(res_qr.content)} <= {len(res_sig.content)}")

        print("\n--- 7. PAYMENT QR: show_upi_qr = False hides QR completely ---")
        client.patch("/invoice-settings", headers=auth1, json={"fields": {"show_upi_qr": False}})
        res_no_qr = client.get(f"/invoices/{inv1.id}/pdf", headers=auth1, params={"format": "detailed"})
        assert_eq(res_no_qr.status_code, 200, "PDF with show_upi_qr=False returns 200")
        if len(res_no_qr.content) < len(res_qr.content):
            ok("show_upi_qr=False correctly omitted QR code from output PDF")
        else:
            fail("show_upi_qr=False did not reduce PDF size")

        client.patch("/invoice-settings", headers=auth1, json={"fields": {"show_upi_qr": True}})

        print("\n--- 8. TEMPLATE FORMATS & STYLES ---")
        res_simple = client.get(f"/invoices/{inv1.id}/pdf", headers=auth1, params={"format": "simple"})
        assert_eq(res_simple.status_code, 200, "Simple format PDF returns 200 with branding")
        assert_eq(res_simple.content[:5], b"%PDF-", "Simple format is valid PDF")

        for style in ["classic", "modern", "compact", "thermal"]:
            client.patch("/invoice-settings", headers=auth1, json={"template": style})
            res_style = client.get(f"/invoices/{inv1.id}/pdf", headers=auth1, params={"format": "detailed"})
            assert_eq(res_style.status_code, 200, f"Template style '{style}' generates 200 PDF with branding")

        print("\n--- 9. MULTI-TENANT ISOLATION ---")
        # Org 2 attempts to use Org 1's stored file IDs in invoice settings
        patch_cross = client.patch(
            "/invoice-settings",
            headers=auth2,
            json={"branding": {"logo_file_id": stored_logo.id, "signature_file_id": stored_sig.id}},
        )
        assert_eq(patch_cross.status_code, 200, "Org 2 patches invoice settings")

        # Create invoice in Org 2
        cust2 = Customer(
            id=str(uuid.uuid4()),
            organization_id=org2.id,
            name="Org2 Buyer",
        )
        db.add(cust2)
        db.flush()

        inv2 = Invoice(
            id=str(uuid.uuid4()),
            organization_id=org2.id,
            customer_id=cust2.id,
            invoice_number="INV-ORG2-001",
            invoice_date=datetime.now(timezone.utc),
            status="unpaid",
            subtotal=500.0,
            tax=90.0,
            total=590.0,
        )
        db.add(inv2)
        db.flush()
        db.add(InvoiceItem(
            id=str(uuid.uuid4()),
            invoice_id=inv2.id,
            product_name="Org 2 Item",
            quantity=5,
            unit_price=100.0,
            tax_rate=18.0,
            tax=90.0,
            tax_amount=90.0,
            line_total=590.0,
        ))
        db.commit()

        # Org 2's PDF should NOT resolve Org 1's files and should generate cleanly as unbranded
        res_org2_pdf = client.get(f"/invoices/{inv2.id}/pdf", headers=auth2, params={"format": "detailed"})
        assert_eq(res_org2_pdf.status_code, 200, "Org 2 PDF generates cleanly with 200")
        assert_eq(res_org2_pdf.content[:5], b"%PDF-", "Org 2 PDF is valid")

        # Set Org 2's logo_url directly pointing to Org 1's file ID
        org2.logo_url = f"http://testserver/files/{stored_logo.id}"
        org2.signature_url = f"http://testserver/files/{stored_sig.id}"
        org2.payment_qr_url = f"http://testserver/files/{stored_qr.id}"
        db.commit()

        res_org2_cross_org = client.get(f"/invoices/{inv2.id}/pdf", headers=auth2, params={"format": "detailed"})
        assert_eq(res_org2_cross_org.status_code, 200, "Org 2 PDF with cross-org file URLs generates safely")
        # Ensure it didn't embed Org 1's logo
        if len(res_org2_cross_org.content) < len(res_qr.content):
            ok("Org 2 safely ignored Org 1 branding files (cross-tenant files blocked)")
        else:
            fail("Org 2 rendered cross-tenant branding!")

    finally:
        db.close()

    print("\n=======================================================")
    print(f"RESULTS: {_passed} passed, {_failed} failed")
    print("=======================================================\n")
    if _failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_tests()
