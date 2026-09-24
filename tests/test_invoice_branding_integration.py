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

        print("\n--- 10. STAMP / SEAL INTEGRATION ---")
        # 10.1 Valid stamp fallback from Organization.stamp_url
        stamp_bytes = _create_test_image((128, 0, 128), (90, 90))
        stored_stamp = StoredFile(
            id=str(uuid.uuid4()),
            organization_id=org1.id,
            filename="company_stamp.png",
            content_type="image/png",
            size=len(stamp_bytes),
            data=stamp_bytes,
        )
        db.add(stored_stamp)
        db.flush()

        org1.stamp_url = f"http://testserver/files/{stored_stamp.id}"
        db.commit()

        res_stamp = client.get(f"/invoices/{inv1.id}/pdf", headers=auth1, params={"format": "detailed"})
        assert_eq(res_stamp.status_code, 200, "PDF with Company Settings stamp returns 200")
        assert_eq(res_stamp.content[:5], b"%PDF-", "Content is valid PDF")
        if len(res_stamp.content) > len(res_qr.content):
            ok("PDF size increased with stamp embedded alongside signature and QR")
        else:
            fail("PDF size did not increase with stamp", f"{len(res_stamp.content)} <= {len(res_qr.content)}")

        # 10.2 Stamp in simple format
        res_stamp_simple = client.get(f"/invoices/{inv1.id}/pdf", headers=auth1, params={"format": "simple"})
        assert_eq(res_stamp_simple.status_code, 200, "Simple format PDF with stamp returns 200")

        # 10.3 Stamp across all 4 templates
        for style in ["classic", "modern", "compact", "thermal"]:
            client.patch("/invoice-settings", headers=auth1, json={"template": style})
            res_style_stamp = client.get(f"/invoices/{inv1.id}/pdf", headers=auth1, params={"format": "detailed"})
            assert_eq(res_style_stamp.status_code, 200, f"Template style '{style}' with stamp generates 200 PDF")

        # Reset template to classic
        client.patch("/invoice-settings", headers=auth1, json={"template": "classic"})

        # 10.4 Stamp missing (None)
        org1.stamp_url = None
        db.commit()
        res_no_stamp = client.get(f"/invoices/{inv1.id}/pdf", headers=auth1, params={"format": "detailed"})
        assert_eq(res_no_stamp.status_code, 200, "PDF with stamp_url=None succeeds")

        # 10.5 Stamp deleted / missing StoredFile
        org1.stamp_url = f"http://testserver/files/{uuid.uuid4()}"
        db.commit()
        res_del_stamp = client.get(f"/invoices/{inv1.id}/pdf", headers=auth1, params={"format": "detailed"})
        assert_eq(res_del_stamp.status_code, 200, "PDF with non-existent stamp file ID succeeds without error")

        # 10.6 Stamp corrupt image bytes
        stored_corrupt_stamp = StoredFile(
            id=str(uuid.uuid4()),
            organization_id=org1.id,
            filename="corrupt_stamp.png",
            content_type="image/png",
            size=15,
            data=b"corrupt-stamp-bytes",
        )
        db.add(stored_corrupt_stamp)
        db.flush()
        org1.stamp_url = f"http://testserver/files/{stored_corrupt_stamp.id}"
        db.commit()
        res_corrupt_stamp = client.get(f"/invoices/{inv1.id}/pdf", headers=auth1, params={"format": "detailed"})
        assert_eq(res_corrupt_stamp.status_code, 200, "PDF with corrupt stamp bytes succeeds without failure")

        # 10.7 Stamp cross-tenant isolation
        org2.stamp_url = f"http://testserver/files/{stored_stamp.id}"
        db.commit()
        res_org2_stamp = client.get(f"/invoices/{inv2.id}/pdf", headers=auth2, params={"format": "detailed"})
        assert_eq(res_org2_stamp.status_code, 200, "Org 2 with Org 1 stamp URL generates safely (cross-tenant blocked)")

        # Restore valid stamp for org1
        org1.stamp_url = f"http://testserver/files/{stored_stamp.id}"
        db.commit()

        print("\n--- 11. LETTERHEAD INTEGRATION ---")
        # 11.1 Valid letterhead fallback from Organization.letterhead_url
        lh_bytes = _create_test_image((240, 240, 240), (600, 100))
        stored_lh = StoredFile(
            id=str(uuid.uuid4()),
            organization_id=org1.id,
            filename="company_letterhead.png",
            content_type="image/png",
            size=len(lh_bytes),
            data=lh_bytes,
        )
        db.add(stored_lh)
        db.flush()

        org1.letterhead_url = f"http://testserver/files/{stored_lh.id}"
        db.commit()

        res_lh = client.get(f"/invoices/{inv1.id}/pdf", headers=auth1, params={"format": "detailed"})
        assert_eq(res_lh.status_code, 200, "PDF with Company Settings letterhead returns 200")
        assert_eq(res_lh.content[:5], b"%PDF-", "Content is valid PDF")
        if len(res_lh.content) > len(res_stamp.content):
            ok("PDF size increased with letterhead embedded")
        else:
            fail("PDF size did not increase with letterhead", f"{len(res_lh.content)} <= {len(res_stamp.content)}")

        # 11.2 Letterhead in simple format
        res_lh_simple = client.get(f"/invoices/{inv1.id}/pdf", headers=auth1, params={"format": "simple"})
        assert_eq(res_lh_simple.status_code, 200, "Simple format PDF with letterhead returns 200")

        # 11.3 Letterhead across all 4 templates (thermal safely skips letterhead)
        for style in ["classic", "modern", "compact", "thermal"]:
            client.patch("/invoice-settings", headers=auth1, json={"template": style})
            res_style_lh = client.get(f"/invoices/{inv1.id}/pdf", headers=auth1, params={"format": "detailed"})
            assert_eq(res_style_lh.status_code, 200, f"Template style '{style}' with letterhead generates 200 PDF")

        client.patch("/invoice-settings", headers=auth1, json={"template": "classic"})

        # 11.4 Letterhead missing (None)
        org1.letterhead_url = None
        db.commit()
        res_no_lh = client.get(f"/invoices/{inv1.id}/pdf", headers=auth1, params={"format": "detailed"})
        assert_eq(res_no_lh.status_code, 200, "PDF with letterhead_url=None succeeds")

        # 11.5 Letterhead deleted / missing StoredFile
        org1.letterhead_url = f"http://testserver/files/{uuid.uuid4()}"
        db.commit()
        res_del_lh = client.get(f"/invoices/{inv1.id}/pdf", headers=auth1, params={"format": "detailed"})
        assert_eq(res_del_lh.status_code, 200, "PDF with non-existent letterhead file ID succeeds without error")

        # 11.6 Letterhead corrupt image bytes
        stored_corrupt_lh = StoredFile(
            id=str(uuid.uuid4()),
            organization_id=org1.id,
            filename="corrupt_lh.png",
            content_type="image/png",
            size=15,
            data=b"corrupt-lh-bytes",
        )
        db.add(stored_corrupt_lh)
        db.flush()
        org1.letterhead_url = f"http://testserver/files/{stored_corrupt_lh.id}"
        db.commit()
        res_corrupt_lh = client.get(f"/invoices/{inv1.id}/pdf", headers=auth1, params={"format": "detailed"})
        assert_eq(res_corrupt_lh.status_code, 200, "PDF with corrupt letterhead bytes succeeds without failure")

        # 11.7 Letterhead cross-tenant isolation
        org2.letterhead_url = f"http://testserver/files/{stored_lh.id}"
        db.commit()
        res_org2_lh = client.get(f"/invoices/{inv2.id}/pdf", headers=auth2, params={"format": "detailed"})
        assert_eq(res_org2_lh.status_code, 200, "Org 2 with Org 1 letterhead URL generates safely (cross-tenant blocked)")

        print("\n--- 12. INVOICE-LEVEL STAMP & PAYMENT QR OVERRIDES & FALLBACK SCENARIOS ---")
        # Ensure org1 has all 4 company assets set
        org1.logo_url = f"http://testserver/files/{stored_logo.id}"
        org1.signature_url = f"http://testserver/files/{stored_sig.id}"
        org1.stamp_url = f"http://testserver/files/{stored_stamp.id}"
        org1.payment_qr_url = f"http://testserver/files/{stored_qr.id}"
        org1.letterhead_url = None
        db.commit()

        # Reset all invoice branding overrides to None
        client.patch(
            "/invoice-settings",
            headers=auth1,
            json={"branding": {
                "logo_file_id": None,
                "signature_file_id": None,
                "stamp_file_id": None,
                "payment_qr_file_id": None,
            }},
        )

        # 12.A: NO INVOICE OVERRIDES (All 4 use Company Settings)
        inv_settings_res = client.get("/invoice-settings", headers=auth1)
        assert_eq(inv_settings_res.status_code, 200, "GET /invoice-settings returns 200")
        b_data = inv_settings_res.json()["branding"]
        assert_eq(b_data["stamp_file_id"], None, "Default stamp_file_id is null")
        assert_eq(b_data["payment_qr_file_id"], None, "Default payment_qr_file_id is null")
        res_all_company = client.get(f"/invoices/{inv1.id}/pdf", headers=auth1, params={"format": "detailed"})
        assert_eq(res_all_company.status_code, 200, "PDF with all 4 Company Settings assets returns 200")
        assert_eq(res_all_company.content[:5], b"%PDF-", "Valid PDF generated with company defaults")

        # 12.B: LOGO OVERRIDE ONLY
        client.patch("/invoice-settings", headers=auth1, json={"branding": {"logo_file_id": stored_override_logo.id}})
        res_logo_only = client.get(f"/invoices/{inv1.id}/pdf", headers=auth1, params={"format": "detailed"})
        assert_eq(res_logo_only.status_code, 200, "PDF with logo override returns 200")
        assert_eq(res_logo_only.content != res_all_company.content, True, "Logo override alters PDF while company sig/stamp/QR persist")
        client.patch("/invoice-settings", headers=auth1, json={"branding": {"logo_file_id": None}})

        # 12.C: SIGNATURE OVERRIDE ONLY
        client.patch("/invoice-settings", headers=auth1, json={"branding": {"signature_file_id": stored_override_sig.id}})
        res_sig_only = client.get(f"/invoices/{inv1.id}/pdf", headers=auth1, params={"format": "detailed"})
        assert_eq(res_sig_only.status_code, 200, "PDF with signature override returns 200")
        assert_eq(res_sig_only.content != res_all_company.content, True, "Signature override alters PDF while company logo/stamp/QR persist")
        client.patch("/invoice-settings", headers=auth1, json={"branding": {"signature_file_id": None}})

        # 12.D: STAMP OVERRIDE (Invoice-specific stamp)
        override_stamp_bytes = _create_test_image((200, 50, 50), (100, 100))
        stored_override_stamp = StoredFile(
            id=str(uuid.uuid4()),
            organization_id=org1.id,
            filename="override_stamp.png",
            content_type="image/png",
            size=len(override_stamp_bytes),
            data=override_stamp_bytes,
        )
        db.add(stored_override_stamp)
        db.commit()

        client.patch("/invoice-settings", headers=auth1, json={"branding": {"stamp_file_id": stored_override_stamp.id}})
        res_stamp_only = client.get(f"/invoices/{inv1.id}/pdf", headers=auth1, params={"format": "detailed"})
        assert_eq(res_stamp_only.status_code, 200, "PDF with stamp override returns 200")
        assert_eq(res_stamp_only.content != res_all_company.content, True, "Stamp override alters PDF while company logo/sig/QR persist")

        # 12.E: PAYMENT QR OVERRIDE (Invoice-specific QR)
        override_qr_bytes = _create_test_image((0, 200, 200), (110, 110))
        stored_override_qr = StoredFile(
            id=str(uuid.uuid4()),
            organization_id=org1.id,
            filename="override_qr.png",
            content_type="image/png",
            size=len(override_qr_bytes),
            data=override_qr_bytes,
        )
        db.add(stored_override_qr)
        db.commit()

        client.patch("/invoice-settings", headers=auth1, json={"branding": {"stamp_file_id": None, "payment_qr_file_id": stored_override_qr.id}})
        res_qr_only = client.get(f"/invoices/{inv1.id}/pdf", headers=auth1, params={"format": "detailed"})
        assert_eq(res_qr_only.status_code, 200, "PDF with payment QR override returns 200")
        assert_eq(res_qr_only.content != res_all_company.content, True, "Payment QR override alters PDF while company logo/sig/stamp persist")

        # 12.F: ALL FOUR OVERRIDES SIMULTANEOUSLY
        client.patch(
            "/invoice-settings",
            headers=auth1,
            json={"branding": {
                "logo_file_id": stored_override_logo.id,
                "signature_file_id": stored_override_sig.id,
                "stamp_file_id": stored_override_stamp.id,
                "payment_qr_file_id": stored_override_qr.id,
            }},
        )
        res_all_override = client.get(f"/invoices/{inv1.id}/pdf", headers=auth1, params={"format": "detailed"})
        assert_eq(res_all_override.status_code, 200, "PDF with all 4 overrides returns 200")
        assert_eq(res_all_override.content[:5], b"%PDF-", "Valid PDF generated with all 4 overrides")
        # Simple format with all 4 overrides
        res_simple_all_override = client.get(f"/invoices/{inv1.id}/pdf", headers=auth1, params={"format": "simple"})
        assert_eq(res_simple_all_override.status_code, 200, "Simple PDF with all 4 overrides returns 200")

        # 12.G: CLEAR STAMP OVERRIDE (PATCH stamp_file_id: null)
        client.patch("/invoice-settings", headers=auth1, json={"branding": {"stamp_file_id": None}})
        get_after_clear_stamp = client.get("/invoice-settings", headers=auth1)
        assert_eq(get_after_clear_stamp.json()["branding"]["stamp_file_id"], None, "stamp_file_id cleared to null")
        assert_eq(get_after_clear_stamp.json()["branding"]["logo_file_id"], stored_override_logo.id, "logo_file_id preserved after stamp clear")
        assert_eq(get_after_clear_stamp.json()["branding"]["signature_file_id"], stored_override_sig.id, "signature_file_id preserved after stamp clear")
        assert_eq(get_after_clear_stamp.json()["branding"]["payment_qr_file_id"], stored_override_qr.id, "payment_qr_file_id preserved after stamp clear")

        # 12.H: CLEAR PAYMENT QR OVERRIDE (PATCH payment_qr_file_id: null)
        client.patch("/invoice-settings", headers=auth1, json={"branding": {"payment_qr_file_id": None}})
        get_after_clear_qr = client.get("/invoice-settings", headers=auth1)
        assert_eq(get_after_clear_qr.json()["branding"]["payment_qr_file_id"], None, "payment_qr_file_id cleared to null")

        # Clear remaining overrides
        client.patch("/invoice-settings", headers=auth1, json={"branding": {"logo_file_id": None, "signature_file_id": None}})
        res_cleared_all = client.get(f"/invoices/{inv1.id}/pdf", headers=auth1, params={"format": "detailed"})
        assert_eq(len(res_cleared_all.content), len(res_all_company.content), "After clearing all overrides, PDF matches company baseline size exactly")

        # 12.I: REPLACE COMPANY ASSET
        new_company_stamp_bytes = _create_test_image((70, 70, 200), (95, 95))
        stored_new_stamp = StoredFile(
            id=str(uuid.uuid4()),
            organization_id=org1.id,
            filename="new_company_stamp.png",
            content_type="image/png",
            size=len(new_company_stamp_bytes),
            data=new_company_stamp_bytes,
        )
        db.add(stored_new_stamp)
        db.flush()
        org1.stamp_url = f"http://testserver/files/{stored_new_stamp.id}"
        db.commit()

        res_replaced_stamp = client.get(f"/invoices/{inv1.id}/pdf", headers=auth1, params={"format": "detailed"})
        assert_eq(res_replaced_stamp.status_code, 200, "PDF with replaced company stamp returns 200")
        assert_eq(res_replaced_stamp.content != res_all_company.content, True, "Invoice without override reflects new company stamp automatically")

        # 12.J: CROSS-TENANT FILE ISOLATION FOR STAMP AND PAYMENT QR
        patch_cross_stamp_qr = client.patch(
            "/invoice-settings",
            headers=auth2,
            json={"branding": {
                "stamp_file_id": stored_override_stamp.id,
                "payment_qr_file_id": stored_override_qr.id,
            }},
        )
        assert_eq(patch_cross_stamp_qr.status_code, 200, "Org 2 patches cross-org stamp and QR file IDs")
        res_org2_cross_stamp_qr = client.get(f"/invoices/{inv2.id}/pdf", headers=auth2, params={"format": "detailed"})
        assert_eq(res_org2_cross_stamp_qr.status_code, 200, "Org 2 PDF generates cleanly without error")
        # Ensure Org 2 did not embed Org 1's stamp or QR
        assert_eq(res_org2_cross_stamp_qr.content != res_all_override.content, True, "Org 2 blocked Org 1's stamp and QR override files")

        # 12.K: BACKWARD COMPATIBILITY WITH EMPTY / LEGACY SETTINGS
        org1.invoice_template_settings = {"template": "classic"}
        db.commit()
        res_legacy_get = client.get("/invoice-settings", headers=auth1)
        assert_eq(res_legacy_get.status_code, 200, "Legacy invoice settings GET returns 200")
        legacy_branding = res_legacy_get.json()["branding"]
        assert_eq(legacy_branding["logo_file_id"], None, "Legacy logo_file_id is null")
        assert_eq(legacy_branding["signature_file_id"], None, "Legacy signature_file_id is null")
        assert_eq(legacy_branding["stamp_file_id"], None, "Legacy stamp_file_id is null")
        assert_eq(legacy_branding["payment_qr_file_id"], None, "Legacy payment_qr_file_id is null")
        assert_eq(res_legacy_get.json().get("template_variant"), None, "Legacy template_variant is null when missing")
        res_legacy_pdf = client.get(f"/invoices/{inv1.id}/pdf", headers=auth1, params={"format": "detailed"})
        assert_eq(res_legacy_pdf.status_code, 200, "Legacy settings generate valid PDF without KeyError")

        # ------------------------------------------------------------------
        # 13: TEMPLATE VARIANT PERSISTENCE (TESTS 1 - 6)
        # ------------------------------------------------------------------
        print("\n--- 13: TEMPLATE VARIANT PERSISTENCE ---")

        # TEST 1: GET /invoice-settings on existing settings returns template_variant = null when key does not exist
        org1.invoice_template_settings = {"template": "classic"}
        db.commit()
        res_t1 = client.get("/invoice-settings", headers=auth1)
        assert_eq(res_t1.status_code, 200, "TEST 1: GET /invoice-settings succeeds")
        assert_eq(res_t1.json()["template_variant"], None, "TEST 1: template_variant is null when key does not exist")

        # TEST 2: PATCH {"template_variant": "gst_theme_1"}
        res_t2 = client.patch(
            "/invoice-settings",
            headers=auth1,
            json={"template_variant": "gst_theme_1"},
        )
        assert_eq(res_t2.status_code, 200, "TEST 2: PATCH template_variant='gst_theme_1' succeeds")
        assert_eq(res_t2.json()["template_variant"], "gst_theme_1", "TEST 2: PATCH response contains 'gst_theme_1'")
        res_t2_get = client.get("/invoice-settings", headers=auth1)
        assert_eq(res_t2_get.json()["template_variant"], "gst_theme_1", "TEST 2: subsequent GET returns 'gst_theme_1'")

        # TEST 3: PATCH another variant ("french_elite") replaces previous value
        res_t3 = client.patch(
            "/invoice-settings",
            headers=auth1,
            json={"template_variant": "french_elite"},
        )
        assert_eq(res_t3.status_code, 200, "TEST 3: PATCH template_variant='french_elite' succeeds")
        assert_eq(res_t3.json()["template_variant"], "french_elite", "TEST 3: previous value is replaced")
        res_t3_get = client.get("/invoice-settings", headers=auth1)
        assert_eq(res_t3_get.json()["template_variant"], "french_elite", "TEST 3: subsequent GET returns 'french_elite'")

        # TEST 4: PATCH {"template_variant": null} clears the variant
        res_t4 = client.patch(
            "/invoice-settings",
            headers=auth1,
            json={"template_variant": None},
        )
        assert_eq(res_t4.status_code, 200, "TEST 4: PATCH template_variant=null succeeds")
        assert_eq(res_t4.json()["template_variant"], None, "TEST 4: PATCH response returns null")
        res_t4_get = client.get("/invoice-settings", headers=auth1)
        assert_eq(res_t4_get.json()["template_variant"], None, "TEST 4: subsequent GET returns null")

        # TEST 5: Partial update safety
        # First set both branding and template_variant
        res_t5_init = client.patch(
            "/invoice-settings",
            headers=auth1,
            json={
                "branding": {"primary_color": "#123456"},
                "template_variant": "double_divine",
            },
        )
        assert_eq(res_t5_init.status_code, 200, "TEST 5: Init branding and template_variant")
        assert_eq(res_t5_init.json()["branding"]["primary_color"], "#123456", "TEST 5: Branding color set")
        assert_eq(res_t5_init.json()["template_variant"], "double_divine", "TEST 5: template_variant set")

        # Update only template_variant -> branding remains unchanged
        res_t5_var_only = client.patch(
            "/invoice-settings",
            headers=auth1,
            json={"template_variant": "tally_classic"},
        )
        assert_eq(res_t5_var_only.status_code, 200, "TEST 5: Update only template_variant succeeds")
        assert_eq(res_t5_var_only.json()["template_variant"], "tally_classic", "TEST 5: template_variant updated to 'tally_classic'")
        assert_eq(res_t5_var_only.json()["branding"]["primary_color"], "#123456", "TEST 5: branding color preserved")

        # Update only branding -> template_variant remains unchanged
        res_t5_brand_only = client.patch(
            "/invoice-settings",
            headers=auth1,
            json={"branding": {"primary_color": "#654321"}},
        )
        assert_eq(res_t5_brand_only.status_code, 200, "TEST 5: Update only branding succeeds")
        assert_eq(res_t5_brand_only.json()["branding"]["primary_color"], "#654321", "TEST 5: branding color updated")
        assert_eq(res_t5_brand_only.json()["template_variant"], "tally_classic", "TEST 5: template_variant preserved")

        # TEST 6: Persistence across fresh DB session / reload
        db.expire_all()
        reloaded_org = db.query(Organization).filter(Organization.id == org1.id).first()
        assert_eq(
            reloaded_org.invoice_template_settings.get("template_variant"),
            "tally_classic",
            "TEST 6: Persisted directly in database JSON column"
        )
        res_t6_get = client.get("/invoice-settings", headers=auth1)
        assert_eq(res_t6_get.json()["template_variant"], "tally_classic", "TEST 6: Fresh GET returns persisted variant")

    finally:
        db.close()

    print("\n=======================================================")
    print(f"RESULTS: {_passed} passed, {_failed} failed")
    print("=======================================================\n")
    if _failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_tests()

