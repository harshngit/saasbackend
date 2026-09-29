"""System-wide image/file URL normalization: app.core.files.normalize_file_url
and every schema/model property that now applies it.

Covers the bug class found across the repo: a `*_url` field that's meant to
hold a ready-to-use reference (`/files/{id}`) instead stored/returned a bare
`file_id` — because the value was either accepted from a client with no
format check, or read straight off an underlying column via ORM passthrough
with no normalization. Fields that are genuinely raw ID fields (e.g.
`profile_image_id`, `photo_file_ids`) are untouched and not covered here.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient

from app.core.database import SessionLocal
from app.core.files import normalize_file_url
from app.main import app
from app.models.product import Product, ProductVariant
from app.models.quotation import Quotation, QuotationItem
from app.models.user import User
from app.schemas.company import CompanySettingsOut
from app.schemas.expense import ExpenseOut
from app.schemas.product import VariantOut
from app.schemas.purchase import PurchaseOut
from app.schemas.supplier_invoice import SupplierInvoiceOut

client = TestClient(app)


# ------------------------------- unit tests: the helper ---------------------------


def test_bare_file_id_is_normalized():
    assert normalize_file_url("d4825176-8f39-423e-86f9-758986f896dc") == "/files/d4825176-8f39-423e-86f9-758986f896dc"


def test_existing_files_path_is_unchanged():
    assert normalize_file_url("/files/abc123") == "/files/abc123"


def test_http_url_is_unchanged():
    assert normalize_file_url("http://example.com/files/abc123") == "http://example.com/files/abc123"


def test_https_url_is_unchanged():
    assert normalize_file_url("https://example.com/files/abc123") == "https://example.com/files/abc123"


def test_data_url_is_unchanged():
    val = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"
    assert normalize_file_url(val) == val


def test_null_stays_null():
    assert normalize_file_url(None) is None


def test_blank_string_becomes_none():
    assert normalize_file_url("   ") is None


def test_no_double_files_prefix():
    # Confirms the normalizer never produces /files//files/abc123.
    once = normalize_file_url("abc123")
    assert normalize_file_url(once) == once == "/files/abc123"


# ------------------------------ schema-level regression tests ---------------------
# Each of these constructs the response schema directly with a bare file_id in a
# *_url field — proving the fix holds regardless of how that value got stored,
# without needing a full DB/endpoint round trip for every single field.


def test_company_settings_out_normalizes_bare_ids():
    out = CompanySettingsOut(
        id="org1", name="Acme", business_type=None, gst_number=None, pan_number=None,
        address=None, phone=None, email=None, financial_year=None,
        logo_url="bare-logo-id", signature_url="bare-sig-id",
        stamp_url="bare-stamp-id", payment_qr_url="/files/already-fine",
        auth_person_photo_url="https://cdn.example.com/already-fine.png",
        auth_person_signature_url=None,
        created_at=__import__("datetime").datetime.now(),
    )
    assert out.logo_url == "/files/bare-logo-id"
    assert out.signature_url == "/files/bare-sig-id"
    assert out.stamp_url == "/files/bare-stamp-id"
    assert out.payment_qr_url == "/files/already-fine"  # unchanged
    assert out.auth_person_photo_url == "https://cdn.example.com/already-fine.png"  # unchanged
    assert out.auth_person_signature_url is None


def test_purchase_out_normalizes_bare_attachment_url():
    out = PurchaseOut(
        id="p1", organization_id="org1", invoice_number="INV-1", supplier_id=None,
        invoice_date=__import__("datetime").datetime.now(), status="draft", payment_status="unpaid",
        subtotal=0, discount=0, tax=0, total=0, amount_paid=0, notes=None,
        attachment_url="bare-attachment-id",
        supplier_quotation_url="/files/already-fine",
        created_at=__import__("datetime").datetime.now(), updated_at=__import__("datetime").datetime.now(),
    )
    assert out.attachment_url == "/files/bare-attachment-id"
    assert out.supplier_quotation_url == "/files/already-fine"


def test_supplier_invoice_out_normalizes_bare_attachment_url():
    now = __import__("datetime").datetime.now()
    out = SupplierInvoiceOut(
        id="si1", organization_id="org1", supplier_id="s1", purchase_id="p1",
        supplier_invoice_number="SI-1", supplier_invoice_date=now, status="pending",
        verification_status="unverified", payment_status="unpaid", subtotal=0, tax_amount=0,
        discount_amount=0, grand_total=0, amount_paid=0, outstanding_amount=0,
        attachment_url="bare-si-attachment", created_at=now, updated_at=now,
    )
    assert out.attachment_url == "/files/bare-si-attachment"


def test_expense_out_normalizes_bare_receipt_and_vendor_invoice_url():
    now = __import__("datetime").datetime.now()
    out = ExpenseOut(
        id="e1", organization_id="org1", category="Travel", amount=100.0,
        expense_date=now, status="pending",
        receipt_url="bare-receipt-id", vendor_invoice_url="/files/already-fine",
        created_at=now, updated_at=now,
    )
    assert out.receipt_url == "/files/bare-receipt-id"
    assert out.vendor_invoice_url == "/files/already-fine"


def test_variant_out_normalizes_bare_image_url():
    out = VariantOut(
        id="v1", name="Red", sku="SKU1", barcode=None, length=None, width=None,
        height=None, weight=None, price=10.0, inventory=5, image_url="bare-variant-image-id",
    )
    assert out.image_url == "/files/bare-variant-image-id"


# ---------------------- model property: product_image_url on line items ----------


def _register_org(name_prefix: str) -> tuple[dict, str]:
    email = f"{name_prefix}_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={
        "organization_name": f"{name_prefix} {uuid.uuid4().hex[:6]}",
        "admin_name": "Admin",
        "email": email,
        "password": "Password123!",
        "role": "admin",
    })
    assert r.status_code == 201, r.text
    token = r.json()["tokens"]["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    db = SessionLocal()
    try:
        org_id = db.query(User).filter(User.email == email).first().organization_id
    finally:
        db.close()
    return headers, org_id


def test_quotation_item_product_image_url_normalizes_bare_variant_id():
    """A ProductVariant.image_url that's a bare file_id (e.g. a client sent
    the file_id from POST /files/upload instead of its url) must still come
    back as a usable /files/{id} link through QuotationItem.product_image_url."""
    _, org_id = _register_org("quote_img_norm")
    db = SessionLocal()
    try:
        product = Product(
            organization_id=org_id, name="Bare Variant Product",
            sku=f"SKU-{uuid.uuid4().hex[:6]}", price=50.0, cover_image="/files/cover-fine",
        )
        db.add(product)
        db.flush()
        variant = ProductVariant(
            product_id=product.id, name="Only Size", sku=f"VAR-{uuid.uuid4().hex[:6]}",
            price=50.0, image_url="bare-variant-file-id",
        )
        db.add(variant)
        db.flush()

        quotation = Quotation(
            organization_id=org_id, quotation_number=f"QT-IMG-{uuid.uuid4().hex[:6]}",
            customer_id=None, status="draft",
        )
        item = QuotationItem(
            quotation=quotation, product_id=product.id, variant_id=variant.id,
            product_name=product.name, quantity=1, unit_price=50.0,
        )
        db.add(quotation)
        db.add(item)
        db.commit()
        db.refresh(item)

        assert item.product_image_url == "/files/bare-variant-file-id"
    finally:
        db.close()


# --------------------------------- endpoint round trip -----------------------------


def test_company_settings_endpoint_normalizes_bare_logo_id():
    headers, _ = _register_org("company_settings_img_norm")

    r = client.put("/organizations/settings", json={"logo_url": "bare-logo-file-id"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["logo_url"] == "/files/bare-logo-file-id"

    r2 = client.get("/organizations/settings", headers=headers)
    assert r2.status_code == 200, r2.text
    assert r2.json()["logo_url"] == "/files/bare-logo-file-id"


# ------------------------- person profile photo: User / Employee ------------------
# UserOut.profile_photo and employee_profile.BasicInformation.profile_photo are
# not *_url-named, but PATCH /users/{id} accepts them from the client with no
# format validation — the exact same bare-file_id risk as every *_url field
# above, just under a legacy name. Normalized the same way, at the same schema
# boundary (BasicInformation serves both the create/update body and the
# response, so this also fixes the write path, not just the read path).


def test_employee_profile_photo_normalizes_bare_id_end_to_end():
    headers, _ = _register_org("staff_photo_norm")
    r = client.post("/users", json={
        "name": "Staff One", "email": f"staff_{uuid.uuid4().hex[:8]}@example.com",
        "password": "Password123!", "role": "Sales Officer",
    }, headers=headers)
    assert r.status_code == 201, r.text
    uid = r.json()["id"]

    r = client.patch(f"/users/{uid}", json={"basic_information": {"profile_photo": "bare-staff-photo-id"}}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["basic_information"]["profile_photo"] == "/files/bare-staff-photo-id"

    # Persisted, not just reflected back from the in-memory PATCH response.
    r2 = client.get(f"/users/{uid}", headers=headers)
    assert r2.json()["basic_information"]["profile_photo"] == "/files/bare-staff-photo-id"

    # The flat GET /users list schema (UserOut) reads the same column independently.
    r3 = client.get("/users", headers=headers)
    match = next(u for u in r3.json() if u["id"] == uid)
    assert match["profile_photo"] == "/files/bare-staff-photo-id"


def test_employee_profile_photo_already_correct_url_unchanged():
    headers, _ = _register_org("staff_photo_ok")
    r = client.post("/users", json={
        "name": "Staff Two", "email": f"staff_{uuid.uuid4().hex[:8]}@example.com",
        "password": "Password123!", "role": "Sales Officer",
    }, headers=headers)
    uid = r.json()["id"]
    r = client.patch(f"/users/{uid}", json={"basic_information": {"profile_photo": "/files/already-fine"}}, headers=headers)
    assert r.json()["basic_information"]["profile_photo"] == "/files/already-fine"


def test_employee_profile_photo_null_stays_null():
    headers, _ = _register_org("staff_photo_null")
    r = client.post("/users", json={
        "name": "Staff Three", "email": f"staff_{uuid.uuid4().hex[:8]}@example.com",
        "password": "Password123!", "role": "Sales Officer",
    }, headers=headers)
    assert r.json()["basic_information"]["profile_photo"] is None


# --------- person "brief" reference objects: photo where meaningfully shown -------
# AssigneeBrief / SalespersonBrief / DeliveryPartnerBrief / etc. reference a real
# person the app actually displays (sales officer, salesperson, driver) and now
# carry profile_photo, normalized the same way as every other *_url-style field.
# DeliveryHistoryActorBrief and AssignableStaffOut are deliberately excluded — see
# the tests below pinning that decision, so a future change here is deliberate,
# not an accidental field addition/removal.


def test_customer_assigned_sales_officer_has_photo_field():
    from app.schemas.customer import AssigneeBrief
    assert set(AssigneeBrief.model_fields) == {"id", "name", "profile_photo"}


def test_delivery_partner_brief_has_photo_field():
    from app.schemas.delivery import DeliveryPartnerBrief
    assert "profile_photo" in DeliveryPartnerBrief.model_fields


def test_delivery_history_actor_brief_has_no_photo_field_by_design():
    """Built from DeliveryHistory's own denormalized actor_id/actor_name columns,
    never a join back to `users` (so it still reads correctly after the acting
    user has been deleted) — there is no live User row to read a photo from."""
    from app.schemas.delivery import DeliveryHistoryActorBrief
    assert set(DeliveryHistoryActorBrief.model_fields) == {"id", "name"}


def test_assignable_staff_out_has_no_photo_field_by_design():
    """Deliberately narrow picker for a task-assignee dropdown — its own
    docstring documents "no email, permissions, or profile data"."""
    from app.schemas.user import AssignableStaffOut
    assert set(AssignableStaffOut.model_fields) == {"id", "name", "role"}


# ------------------- person profile photo: sales/delivery references --------------


def _make_user_with_photo(headers: dict, role: str, photo: str | None) -> str:
    r = client.post("/users", json={
        "name": f"Person {uuid.uuid4().hex[:6]}",
        "email": f"person_{uuid.uuid4().hex[:8]}@example.com",
        "password": "Password123!", "role": role,
    }, headers=headers)
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    if photo is not None:
        client.patch(f"/users/{uid}", json={"basic_information": {"profile_photo": photo}}, headers=headers)
    return uid


def test_customer_assigned_sales_officer_photo_normalizes_bare_id():
    headers, _ = _register_org("cust_officer_photo")
    officer_id = _make_user_with_photo(headers, "Sales Officer", "officer-bare-id")
    r = client.post("/customers", json={"name": "Cust Officer", "assigned_sales_officer_id": officer_id}, headers=headers)
    cid = r.json()["id"]
    r2 = client.get("/customers", headers=headers, params={"search": "Cust Officer"})
    match = next(c for c in r2.json() if c["id"] == cid)
    assert match["assigned_sales_officer"]["profile_photo"] == "/files/officer-bare-id"
    assert match["assigned_sales_officer_id"] == officer_id  # raw id untouched


def test_customer_assigned_sales_officer_photo_null_when_unset():
    headers, _ = _register_org("cust_officer_nophoto")
    officer_id = _make_user_with_photo(headers, "Sales Officer", None)
    r = client.post("/customers", json={"name": "Cust NoPhoto", "assigned_sales_officer_id": officer_id}, headers=headers)
    cid = r.json()["id"]
    r2 = client.get("/customers", headers=headers, params={"search": "Cust NoPhoto"})
    match = next(c for c in r2.json() if c["id"] == cid)
    assert match["assigned_sales_officer"]["profile_photo"] is None


def test_order_salesperson_photo_normalizes_bare_id():
    headers, _ = _register_org("order_sp_photo")
    officer_id = _make_user_with_photo(headers, "Sales Officer", "order-sp-bare-id")
    cust = client.post("/customers", json={"name": "Cust Order SP"}, headers=headers).json()
    wh = client.post("/warehouses", json={"name": "WH", "code": f"WH{uuid.uuid4().hex[:4]}"}, headers=headers).json()
    prod = client.post("/products", json={"name": "P", "sku": f"SKU{uuid.uuid4().hex[:6]}", "sale_price": 10.0}, headers=headers).json()
    order = client.post("/orders", json={
        "customer_id": cust["id"], "warehouse_id": wh["id"], "salesperson_id": officer_id,
        "items": [{"product_id": prod["id"], "quantity": 1, "unit_price": 10.0}],
    }, headers=headers)
    assert order.status_code == 201, order.text
    assert order.json()["salesperson"]["profile_photo"] == "/files/order-sp-bare-id"


def test_quotation_salesperson_photo_normalizes_bare_id():
    headers, _ = _register_org("quote_sp_photo")
    officer_id = _make_user_with_photo(headers, "Sales Officer", "quote-sp-bare-id")
    cust = client.post("/customers", json={"name": "Cust Quote SP"}, headers=headers).json()
    prod = client.post("/products", json={"name": "P", "sku": f"SKU{uuid.uuid4().hex[:6]}", "sale_price": 10.0}, headers=headers).json()
    q = client.post("/quotations", json={
        "customer_id": cust["id"], "salesperson_id": officer_id,
        "items": [{"product_id": prod["id"], "quantity": 1, "unit_price": 10.0}],
    }, headers=headers)
    assert q.status_code == 201, q.text
    assert q.json()["salesperson"]["profile_photo"] == "/files/quote-sp-bare-id"


def test_delivery_partner_photo_normalizes_bare_id():
    from app.models.delivery import Delivery
    from app.models.customer import Customer

    headers, _ = _register_org("deliv_partner_photo")
    partner_id = _make_user_with_photo(headers, "Delivery Partner", "deliv-partner-bare-id")

    db = SessionLocal()
    try:
        org_id = db.query(User).filter(User.organization_id.isnot(None)).order_by(User.created_at.desc()).first().organization_id
        cust = Customer(organization_id=org_id, name="Cust Delivery Photo")
        db.add(cust)
        db.flush()
        delivery = Delivery(
            organization_id=org_id, delivery_note_number=f"DLV-{uuid.uuid4().hex[:6]}",
            customer_id=cust.id, delivery_partner_id=partner_id, status="planned",
        )
        db.add(delivery)
        db.commit()
        did = delivery.id
    finally:
        db.close()

    r = client.get(f"/deliveries/by-id/{did}", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["delivery_partner"]["profile_photo"] == "/files/deliv-partner-bare-id"


def test_vehicle_delivery_partner_photo_normalizes_bare_id():
    headers, _ = _register_org("vehicle_partner_photo")
    partner_id = _make_user_with_photo(headers, "Delivery Partner", "vehicle-partner-bare-id")
    r = client.post("/vehicles", json={
        "vehicle_number": f"VEH-{uuid.uuid4().hex[:6]}", "vehicle_type": "van",
        "default_driver_id": partner_id,
    }, headers=headers)
    assert r.status_code == 201, r.text
    assert r.json()["assigned_delivery_partner"]["profile_photo"] == "/files/vehicle-partner-bare-id"


# ------------------- schema-level regression for the remaining Briefs -------------
# (follow-up / lead / visit / leave): full endpoint round trips for each would
# need their own multi-step setup (a follow-up needs a customer+lead, a leave
# needs an approval workflow, etc.) — schema-level construction proves the same
# validator wiring is correct without duplicating that setup four more times.


def test_follow_up_user_brief_normalizes_bare_id():
    from app.schemas.follow_up import FollowUpUserBrief
    out = FollowUpUserBrief(id="u1", name="Rep", email="rep@example.com", profile_photo="bare-id")
    assert out.profile_photo == "/files/bare-id"


def test_lead_salesperson_brief_normalizes_bare_id():
    from app.schemas.lead import LeadSalespersonBrief
    out = LeadSalespersonBrief(id="u1", name="Rep", email="rep@example.com", profile_photo="bare-id")
    assert out.profile_photo == "/files/bare-id"


def test_visit_user_brief_normalizes_bare_id():
    from app.schemas.visit import VisitUserBrief
    out = VisitUserBrief(id="u1", name="Rep", email="rep@example.com", profile_photo="bare-id")
    assert out.profile_photo == "/files/bare-id"


def test_leave_user_brief_normalizes_bare_id_and_full_url_unchanged():
    from app.schemas.leave import LeaveUserBrief
    out = LeaveUserBrief(id="u1", name="Rep", profile_photo="bare-id")
    assert out.profile_photo == "/files/bare-id"
    out2 = LeaveUserBrief(id="u1", name="Rep", profile_photo="https://cdn.example.com/photo.png")
    assert out2.profile_photo == "https://cdn.example.com/photo.png"
    out3 = LeaveUserBrief(id="u1", name="Rep", profile_photo=None)
    assert out3.profile_photo is None
