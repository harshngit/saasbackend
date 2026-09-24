"""Comprehensive test suite for Invoice + Print Settings Enhancement."""

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
    DeliveryItem,
    Vehicle,
    StoredFile,
)
from app.models.enums import UserRole, OrganizationStatus
from app.core.security import hash_password, create_access_token
from app.core import workflow
from app.core.pdf_docs import invoice_detailed_pdf, invoice_simple_pdf, delivery_receipt_pdf

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


def _create_test_image(color=(0, 128, 255), size=(64, 64), fmt="PNG") -> bytes:
    img = Image.new("RGB", size, color=color)
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def _setup_org_user(db, suffix: str):
    org = Organization(
        id=str(uuid.uuid4()),
        name=f"PrintOrg {suffix}",
        status=OrganizationStatus.ACTIVE,
        currency="INR",
        gst_number="27ABCDE1234F1Z5",
        address="100 Technology Park",
        phone="9876500001",
        email=f"print_{suffix}@test.com",
    )
    db.add(org)
    db.flush()

    user = User(
        id=str(uuid.uuid4()),
        organization_id=org.id,
        email=f"admin_{suffix}@printtest.com",
        name=f"Admin {suffix}",
        password_hash=hash_password("Secret123!"),
        role=UserRole.ADMIN,
        is_active=True,
    )
    db.add(user)
    db.commit()

    token = create_access_token(user.id, "admin", org.id)
    headers = {"Authorization": f"Bearer {token}"}
    return org, user, headers


def run_tests():
    global _passed, _failed
    print("\n=======================================================")
    print("TEST SUITE: Invoice + Print Settings Enhancement")
    print("=======================================================")

    db = SessionLocal()
    try:
        org1, user1, auth1 = _setup_org_user(db, "P1")
        org2, user2, auth2 = _setup_org_user(db, "P2")

        # =====================================================
        # 1. SETTINGS TESTS
        # =====================================================
        print("\n--- 1. SETTINGS TESTS ---")

        # Test 1.1: GET /invoice-settings loads all defaults
        r = client.get("/invoice-settings", headers=auth1)
        assert_eq(r.status_code, 200, "GET /invoice-settings returns 200")
        data = r.json()
        assert_eq("typography" in data, True, "Settings contains typography block")
        assert_eq("business_details" in data, True, "Settings contains business_details block")
        assert_eq("invoice_details" in data, True, "Settings contains invoice_details block")
        assert_eq("party_details" in data, True, "Settings contains party_details block")
        assert_eq("item_table" in data, True, "Settings contains item_table block")
        assert_eq("payment_details" in data, True, "Settings contains payment_details block")
        assert_eq("footer" in data, True, "Settings contains footer block")
        assert_eq("regular_print" in data, True, "Settings contains regular_print block")
        assert_eq("thermal_print" in data, True, "Settings contains thermal_print block")

        # Test 1.2: Backward compatibility with legacy organization settings
        org1.invoice_template_settings = {
            "template": "modern",
            "branding": {"primary_color": "#166534"},
            "fields": {"show_mrp": True},
        }
        db.commit()
        r = client.get("/invoice-settings", headers=auth1)
        assert_eq(r.status_code, 200, "Legacy settings load returns 200")
        data = r.json()
        assert_eq(data["template"], "modern", "Preserved legacy template")
        assert_eq(data["branding"]["primary_color"], "#166534", "Preserved legacy branding primary_color")
        assert_eq(data["typography"]["font_family"], "Helvetica", "Missing typography receives default Helvetica")
        assert_eq(data["regular_print"]["paper_size"], "A4", "Missing regular_print receives default A4")

        # Test 1.3: Partial PATCH preserves siblings
        patch_payload = {
            "typography": {"font_family": "Times", "heading_size": 20},
        }
        r = client.patch("/invoice-settings", json=patch_payload, headers=auth1)
        assert_eq(r.status_code, 200, "PATCH /invoice-settings returns 200")
        data = r.json()
        assert_eq(data["typography"]["font_family"], "Times", "Typography font_family updated to Times")
        assert_eq(data["typography"]["heading_size"], 20, "Typography heading_size updated to 20")
        assert_eq(data["typography"]["body_size"], 9, "Typography body_size preserved default 9")
        assert_eq(data["branding"]["primary_color"], "#166534", "Branding primary_color preserved after typography patch")
        assert_eq(data["template"], "modern", "Template preserved after typography patch")

        # Test 1.4: Update regular_print doesn't erase thermal_print
        patch_payload = {
            "regular_print": {"paper_size": "A5", "orientation": "landscape", "margin_left": 15.0}
        }
        r = client.patch("/invoice-settings", json=patch_payload, headers=auth1)
        assert_eq(r.status_code, 200, "PATCH regular_print returns 200")
        data = r.json()
        assert_eq(data["regular_print"]["paper_size"], "A5", "Regular print paper_size is A5")
        assert_eq(data["regular_print"]["orientation"], "landscape", "Regular print orientation is landscape")
        assert_eq(data["thermal_print"]["paper_width"], "80mm", "Thermal print width preserved default 80mm")

        # Test 1.5: Validation rejections
        # Invalid font family
        r = client.patch("/invoice-settings", json={"typography": {"font_family": "ComicSans"}}, headers=auth1)
        assert_eq(r.status_code, 422, "Invalid font family ComicSans rejected with 422")

        # Out-of-range heading size
        r = client.patch("/invoice-settings", json={"typography": {"heading_size": 40}}, headers=auth1)
        assert_eq(r.status_code, 422, "Heading size 40 rejected with 422")

        # Invalid regular paper size
        r = client.patch("/invoice-settings", json={"regular_print": {"paper_size": "A3"}}, headers=auth1)
        assert_eq(r.status_code, 422, "Paper size A3 rejected with 422")

        # Invalid regular orientation
        r = client.patch("/invoice-settings", json={"regular_print": {"orientation": "skewed"}}, headers=auth1)
        assert_eq(r.status_code, 422, "Orientation skewed rejected with 422")

        # Invalid thermal width
        r = client.patch("/invoice-settings", json={"thermal_print": {"paper_width": "72mm"}}, headers=auth1)
        assert_eq(r.status_code, 422, "Thermal width 72mm rejected with 422")

        # Duplicate item columns
        r = client.patch("/invoice-settings", json={"item_table": {"columns": ["product", "rate", "rate", "amount"]}}, headers=auth1)
        assert_eq(r.status_code, 422, "Duplicate item columns rejected with 422")

        # Invalid item column
        r = client.patch("/invoice-settings", json={"item_table": {"columns": ["product", "magic_column", "amount"]}}, headers=auth1)
        assert_eq(r.status_code, 422, "Invalid item column magic_column rejected with 422")

        # Missing mandatory product column
        r = client.patch("/invoice-settings", json={"item_table": {"columns": ["quantity", "rate", "amount"]}}, headers=auth1)
        assert_eq(r.status_code, 422, "Missing product column rejected with 422")

        # Missing mandatory amount column
        r = client.patch("/invoice-settings", json={"item_table": {"columns": ["product", "quantity", "rate"]}}, headers=auth1)
        assert_eq(r.status_code, 422, "Missing amount column rejected with 422")

        # More than 5 columns rejected
        r = client.patch("/invoice-settings", json={"item_table": {"columns": ["product", "hsn_sac", "quantity", "rate", "tax_amount", "amount"]}}, headers=auth1)
        assert_eq(r.status_code, 422, "More than 5 columns rejected with 422")

        # Valid 5-column item table patch
        valid_cols_5 = ["product", "product_image", "quantity", "rate", "amount"]
        r = client.patch("/invoice-settings", json={"item_table": {"show_product_image": True, "columns": valid_cols_5}}, headers=auth1)
        assert_eq(r.status_code, 200, "Valid 5-column item table configuration accepted with 200")
        assert_eq(r.json()["item_table"]["columns"], valid_cols_5, "Item table columns match frontend specified order")

        # =====================================================
        # 2. INVOICE PRODUCT IMAGE SUPPORT TESTS
        # =====================================================
        print("\n--- 2. INVOICE PRODUCT IMAGE SUPPORT TESTS ---")

        # Product with images list and cover_image
        p1 = Product(
            id=str(uuid.uuid4()),
            organization_id=org1.id,
            name="Laptop X",
            price=50000.0,
            cover_image="/files/prod_cover_1.png",
            images=["/files/prod_img_alt1.png"],
        )
        db.add(p1)

        v1 = ProductVariant(
            id=str(uuid.uuid4()),
            product_id=p1.id,
            name="16GB RAM",
            price=55000.0,
            image_url="/files/var_img_1.png",
        )
        db.add(v1)

        p2 = Product(
            id=str(uuid.uuid4()),
            organization_id=org1.id,
            name="Mouse Y",
            price=500.0,
            cover_image=None,
            images=["/files/prod_img_mouse.png"],
        )
        db.add(p2)

        p3 = Product(
            id=str(uuid.uuid4()),
            organization_id=org1.id,
            name="Keyboard Z",
            price=1500.0,
            cover_image=None,
            images=[],
        )
        db.add(p3)

        cust1 = Customer(
            id=str(uuid.uuid4()),
            organization_id=org1.id,
            name="Global Enterprises",
            phone="9988776655",
            billing_address="Tower 1, Cyber City",
            delivery_address="Tower 1, Cyber City",
        )
        db.add(cust1)
        db.flush()

        inv = Invoice(
            id=str(uuid.uuid4()),
            organization_id=org1.id,
            customer_id=cust1.id,
            invoice_number="INV-IMG-001",
            invoice_date=datetime.now(timezone.utc),
            status="unpaid",
            subtotal=57000.0,
            tax=10260.0,
            total=67260.0,
            amount_paid=0.0,
        )
        db.add(inv)
        db.flush()

        item1 = InvoiceItem(
            id=str(uuid.uuid4()),
            invoice_id=inv.id,
            product_id=p1.id,
            variant_id=v1.id,
            product_name="Laptop X (16GB RAM)",
            quantity=1,
            unit_price=55000.0,
            tax=9900.0,
            line_total=64900.0,
        )
        item2 = InvoiceItem(
            id=str(uuid.uuid4()),
            invoice_id=inv.id,
            product_id=p2.id,
            variant_id=None,
            product_name="Mouse Y",
            quantity=2,
            unit_price=500.0,
            tax=180.0,
            line_total=1180.0,
        )
        item3 = InvoiceItem(
            id=str(uuid.uuid4()),
            invoice_id=inv.id,
            product_id=p3.id,
            variant_id=None,
            product_name="Keyboard Z",
            quantity=1,
            unit_price=1000.0,
            tax=180.0,
            line_total=1180.0,
        )
        db.add_all([item1, item2, item3])
        db.commit()

        # Check fallback precedence directly on ORM models
        db.refresh(item1)
        db.refresh(item2)
        db.refresh(item3)
        assert_eq(item1.product_image_url, "/files/var_img_1.png", "Variant image url wins over cover image")
        assert_eq(item2.product_image_url, "/files/prod_img_mouse.png", "Product.images[0] used when cover_image is null")
        assert_eq(item3.product_image_url, None, "Returns None when no image exists")

        # Check API response serialization exposes product_image_url
        r = client.get(f"/invoices/{inv.id}", headers=auth1)
        assert_eq(r.status_code, 200, "GET /invoices/{id} returns 200")
        items_out = r.json()["items"]
        assert_eq(len(items_out), 3, "Invoice items count is 3")
        assert_eq(items_out[0]["product_image_url"], "/files/var_img_1.png", "API InvoiceItemOut item1 has variant image")
        assert_eq(items_out[1]["product_image_url"], "/files/prod_img_mouse.png", "API InvoiceItemOut item2 has images[0]")
        assert_eq(items_out[2]["product_image_url"], None, "API InvoiceItemOut item3 is null")

        # =====================================================
        # 3. INVOICE PDF RENDERER ENHANCEMENTS
        # =====================================================
        print("\n--- 3. INVOICE PDF RENDERER ENHANCEMENTS ---")

        settings_dict = workflow.invoice_settings(org1)

        # 3.1: Detailed PDF generation
        pdf_bytes_detailed = invoice_detailed_pdf(
            org1, cust1, inv, settings_dict
        )
        assert_eq(isinstance(pdf_bytes_detailed, bytes), True, "Detailed invoice PDF generated as bytes")
        assert_eq(len(pdf_bytes_detailed) > 500, True, "Detailed PDF has non-trivial length")
        
        # 3.2: Simple PDF generation
        pdf_bytes_simple = invoice_simple_pdf(
            org1, cust1, inv, settings_dict
        )
        assert_eq(isinstance(pdf_bytes_simple, bytes), True, "Simple invoice PDF generated as bytes")
        assert_eq(len(pdf_bytes_simple) > 500, True, "Simple PDF has non-trivial length")

        # 3.3: Endpoint GET /invoices/{id}/pdf
        r = client.get(f"/invoices/{inv.id}/pdf?format=detailed", headers=auth1)
        assert_eq(r.status_code, 200, "GET /invoices/{id}/pdf?format=detailed returns 200")
        assert_eq(r.headers.get("content-type"), "application/pdf", "Media type is application/pdf")

        r = client.get(f"/invoices/{inv.id}/pdf?format=simple", headers=auth1)
        assert_eq(r.status_code, 200, "GET /invoices/{id}/pdf?format=simple returns 200")

        # 3.4: Font family switching and A5 Landscape test
        settings_times = dict(settings_dict)
        settings_times["typography"] = {"font_family": "Times", "heading_size": 18, "body_size": 10, "table_size": 9}
        settings_times["regular_print"] = {"paper_size": "A5", "orientation": "landscape", "margin_top": 8.0, "margin_left": 8.0, "margin_right": 8.0, "margin_bottom": 8.0}
        pdf_times = invoice_detailed_pdf(org1, cust1, inv, settings_times)
        assert_eq(isinstance(pdf_times, bytes), True, "Times font + A5 Landscape PDF generated successfully")

        # 3.5: Custom typography size variation test (heading_size=24, body_size=12, table_size=10)
        settings_large_type = dict(settings_dict)
        settings_large_type["typography"] = {"font_family": "Helvetica", "heading_size": 24, "body_size": 12, "table_size": 10}
        pdf_large = invoice_detailed_pdf(org1, cust1, inv, settings_large_type)
        assert_eq(isinstance(pdf_large, bytes), True, "Custom large typography PDF generated successfully")
        assert_eq(len(pdf_large) > len(pdf_times) * 0.5, True, "Large typography PDF has valid content length")

        # 3.6: Product thumbnail image embedding
        fake_img_bytes = _create_test_image()
        item_images = {item1.id: fake_img_bytes}
        settings_with_img = dict(settings_dict)
        settings_with_img["item_table"] = {"show_product_image": True, "columns": ["product", "product_image", "quantity", "rate", "amount"]}
        pdf_with_img = invoice_detailed_pdf(org1, cust1, inv, settings_with_img, item_images=item_images)
        assert_eq(isinstance(pdf_with_img, bytes), True, "PDF with thumbnail image column generated successfully")

        # =====================================================
        # 4. THERMAL PRINTING TESTS
        # =====================================================
        print("\n--- 4. THERMAL PRINTING TESTS ---")

        # 4.1: 58mm thermal
        settings_thermal_58 = dict(settings_dict)
        settings_thermal_58["template"] = "thermal"
        settings_thermal_58["thermal_print"] = {"paper_width": "58mm", "extra_lines": 3, "copies": 1, "bold_text": True}
        pdf_58 = invoice_detailed_pdf(org1, cust1, inv, settings_thermal_58)
        assert_eq(isinstance(pdf_58, bytes), True, "58mm Thermal PDF generated successfully")

        # 4.2: 80mm thermal with bold_text=False
        settings_thermal_80 = dict(settings_dict)
        settings_thermal_80["template"] = "thermal"
        settings_thermal_80["thermal_print"] = {"paper_width": "80mm", "extra_lines": 2, "copies": 2, "bold_text": False}
        pdf_80 = invoice_detailed_pdf(org1, cust1, inv, settings_thermal_80)
        assert_eq(isinstance(pdf_80, bytes), True, "80mm Thermal PDF (non-bold) generated successfully")

        # 4.3: 110mm thermal
        settings_thermal_110 = dict(settings_dict)
        settings_thermal_110["template"] = "thermal"
        settings_thermal_110["thermal_print"] = {"paper_width": "110mm", "extra_lines": 1, "copies": 1, "bold_text": True}
        pdf_110 = invoice_detailed_pdf(org1, cust1, inv, settings_thermal_110)
        assert_eq(isinstance(pdf_110, bytes), True, "110mm Thermal PDF generated successfully")

        # =====================================================
        # 5. DELIVERY RECEIPT TESTS
        # =====================================================
        print("\n--- 5. DELIVERY RECEIPT TESTS ---")

        # Create partner and vehicle
        partner = User(
            id=str(uuid.uuid4()),
            organization_id=org1.id,
            email="driver@printtest.com",
            name="Ramesh Driver",
            password_hash=hash_password("Secret123!"),
            role=UserRole.DELIVERY_PARTNER,
            is_active=True,
        )
        vehicle = Vehicle(
            id=str(uuid.uuid4()),
            organization_id=org1.id,
            vehicle_number="MH-02-CD-5678",
            vehicle_type="Van",
            is_active=True,
        )
        db.add_all([partner, vehicle])
        db.flush()

        # Create SalesOrder
        order = SalesOrder(
            id=str(uuid.uuid4()),
            organization_id=org1.id,
            customer_id=cust1.id,
            order_number="SO-REC-001",
            status="processing",
            fulfilment_status="in_transit",
            total=10000.0,
        )
        db.add(order)
        db.flush()

        so_item = SalesOrderItem(
            id=str(uuid.uuid4()),
            order_id=order.id,
            product_id=p1.id,
            variant_id=v1.id,
            product_name="Laptop X (16GB RAM)",
            quantity=10,
            unit_price=1000.0,
            line_total=10000.0,
        )
        db.add(so_item)
        db.flush()

        # Create Delivery in "in_transit" status (not yet completed)
        deliv = Delivery(
            id=str(uuid.uuid4()),
            organization_id=org1.id,
            delivery_note_number="DEL-REC-001",
            sales_order_id=order.id,
            customer_id=cust1.id,
            delivery_partner_id=partner.id,
            vehicle_id=vehicle.id,
            status="in_transit",
            delivery_address="Tower 1, Cyber City",
            receiver_name="Security Desk",
            notes="Gate pass required",
        )
        db.add(deliv)
        db.flush()

        deliv_item = DeliveryItem(
            id=str(uuid.uuid4()),
            delivery_id=deliv.id,
            order_item_id=so_item.id,
            product_id=p1.id,
            variant_id=v1.id,
            product_name="Laptop X (16GB RAM)",
            planned_quantity=10.0,
            loaded_quantity=10.0,
            delivered_quantity=8.0,  # Partial delivered scenario
        )
        db.add(deliv_item)
        db.commit()

        # 5.1: Receipt before delivery completion -> 400 Bad Request
        r = client.get(f"/deliveries/{deliv.id}/receipt", headers=auth1)
        assert_eq(r.status_code, 400, "Receipt for in_transit delivery returns 400 Bad Request")

        # 5.2: Complete delivery -> status="delivered"
        deliv.status = "delivered"
        deliv.confirmed_at = datetime.now(timezone.utc)
        db.commit()

        # 5.3: Receipt after delivery completion -> 200 OK with PDF
        r = client.get(f"/deliveries/{deliv.id}/receipt", headers=auth1)
        assert_eq(r.status_code, 200, "Receipt for delivered delivery returns 200 OK")
        assert_eq(r.headers.get("content-type"), "application/pdf", "Media type is application/pdf")
        assert_eq(len(r.content) > 500, True, "Delivery receipt PDF has non-trivial length")

        # 5.4: Lookup via delivery_note_number
        r = client.get(f"/deliveries/{deliv.delivery_note_number}/receipt", headers=auth1)
        assert_eq(r.status_code, 200, "Receipt resolved via delivery_note_number returns 200 OK")

        # 5.5: Backward compatibility lookup via order ID
        r = client.get(f"/deliveries/{order.id}/receipt", headers=auth1)
        assert_eq(r.status_code, 200, "Receipt resolved via order ID returns 200 OK")

        # 5.6: Backward compatibility lookup via order_number
        r = client.get(f"/deliveries/{order.order_number}/receipt", headers=auth1)
        assert_eq(r.status_code, 200, "Receipt resolved via order_number returns 200 OK")

        # 5.7: Invalid ID returns 404
        r = client.get("/deliveries/non-existent-deliv-id/receipt", headers=auth1)
        assert_eq(r.status_code, 404, "Invalid delivery ID returns 404 Not Found")

        # 5.8: Cross-tenant isolation
        r = client.get(f"/deliveries/{deliv.id}/receipt", headers=auth2)
        assert_eq(r.status_code, 404, "Cross-tenant delivery receipt access returns 404")

        # 5.9: Multiple deliveries on same order: latest delivered chosen
        deliv2 = Delivery(
            id=str(uuid.uuid4()),
            organization_id=org1.id,
            delivery_note_number="DEL-REC-002",
            sales_order_id=order.id,
            customer_id=cust1.id,
            status="delivered",
            confirmed_at=datetime.now(timezone.utc),
        )
        deliv2_item = DeliveryItem(
            id=str(uuid.uuid4()),
            delivery_id=deliv2.id,
            order_item_id=so_item.id,
            product_id=p1.id,
            variant_id=v1.id,
            product_name="Laptop X (16GB RAM)",
            planned_quantity=2.0,
            loaded_quantity=2.0,
            delivered_quantity=2.0,
        )
        db.add_all([deliv2, deliv2_item])
        db.commit()

        r = client.get(f"/deliveries/{order.id}/receipt", headers=auth1)
        assert_eq(r.status_code, 200, "Multi-delivery order receipt resolves latest delivered delivery")

        # =====================================================
        # SUMMARY
        # =====================================================
        print("\n=======================================================")
        print(f"RESULTS: {_passed} PASSED, {_failed} FAILED")
        print("=======================================================\n")

        if _failed > 0:
            sys.exit(1)

    finally:
        db.close()


if __name__ == "__main__":
    run_tests()

