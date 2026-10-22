"""Invoice theme catalog: GET /invoice-templates, template_variant validation on
PATCH /invoice-settings, and theme-aware PDF rendering with per-download overrides."""

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core import invoice_regular_pdf, pdf_docs, workflow
from app.core.database import Base, engine
from app.core.security import create_access_token
from app.main import app
from app.models import Customer, Invoice, InvoiceItem, Organization, Role, User

REGULAR_IDS = [
    "classic", "modern", "compact", "tally", "landscape-1", "landscape-2", "gst-1", "gst-2",
    "gst-3", "gst-4", "gst-5", "gst-6", "double-divine", "french-elite",
]
THERMAL_IDS = [
    "thermal-compact", "thermal-advanced", "thermal-simple", "thermal-classic",
    "thermal-theme-1", "thermal-theme-2", "thermal-theme-3", "thermal-theme-4",
]


@pytest.fixture
def db_session():
    Base.metadata.create_all(bind=engine)
    session = Session(bind=engine)
    try:
        yield session
    finally:
        session.close()


def _client(user: User) -> TestClient:
    client = TestClient(app)
    token = create_access_token(user.id, role=user.system_role, organization_id=user.organization_id)
    client.headers = {"Authorization": f"Bearer {token}"}
    return client


def _org(db: Session, name: str, settings: dict | None = None):
    org = Organization(id=str(uuid.uuid4()), name=name, company_code="CMP-10001", invoice_template_settings=settings or {})
    db.add(org)
    db.flush()
    admin = User(
        id=str(uuid.uuid4()), organization_id=org.id, email=f"a_{uuid.uuid4().hex[:8]}@x.com",
        name="Admin", password_hash="h", role="admin", system_role="admin", is_active=True,
    )
    db.add(admin)
    db.commit()
    return org, admin


def _staff(db: Session, org: Organization, permissions: dict) -> User:
    role = Role(organization_id=org.id, name=f"Role {uuid.uuid4().hex[:6]}", permissions=permissions)
    db.add(role)
    db.flush()
    staff = User(
        id=str(uuid.uuid4()), organization_id=org.id, email=f"s_{uuid.uuid4().hex[:8]}@x.com",
        name="Staff", password_hash="h", system_role="staff", role_id=role.id, is_active=True,
    )
    db.add(staff)
    db.commit()
    return staff


def _invoice(db: Session, org: Organization) -> Invoice:
    customer = Customer(organization_id=org.id, name="Buyer", customer_id=f"CS-T-{uuid.uuid4().hex[:6]}")
    db.add(customer)
    db.flush()
    inv = Invoice(
        organization_id=org.id, customer_id=customer.id, invoice_number=f"INV-T-{uuid.uuid4().hex[:6]}",
        invoice_date=datetime.now(timezone.utc), status="unpaid",
        subtotal=1500.0, tax=270.0, total=1770.0, amount_paid=0.0,
    )
    db.add(inv)
    db.flush()
    db.add_all([
        InvoiceItem(invoice_id=inv.id, product_name="Widget", quantity=2, unit_price=500.0, tax=180.0, line_total=1180.0),
        InvoiceItem(invoice_id=inv.id, product_name="Gadget", quantity=1, unit_price=500.0, tax=90.0, line_total=590.0),
    ])
    db.commit()
    return inv


@pytest.fixture
def style_spy(monkeypatch):
    """Records which theme the PDF renderer resolved for each render."""
    seen: list[str | None] = []
    original = invoice_regular_pdf.resolve_theme

    def spy(settings):
        theme = original(settings)
        seen.append(theme["id"])
        return theme

    monkeypatch.setattr(invoice_regular_pdf, "resolve_theme", spy)
    return seen


# --------------------------------- catalog ---------------------------------


def test_catalog_lists_every_theme_with_frontend_metadata(db_session: Session):
    org, admin = _org(db_session, "Theme Catalog Org")
    res = _client(admin).get("/invoice-templates")
    assert res.status_code == 200, res.text
    body = res.json()
    assert [t["id"] for t in body["regular"]] == REGULAR_IDS
    assert [t["id"] for t in body["thermal"]] == THERMAL_IDS
    gst3 = next(t for t in body["regular"] if t["id"] == "gst-3")
    assert gst3 == {"id": "gst-3", "name": "GST Theme 3", "base_template": "compact", "header": "band-sm",
                    "table": "lined", "totals": "boxed", "accent": "bold", "density": "compact"}
    assert body["selected"] == {"template": "classic", "template_variant": None,
                                "resolved_template_variant": "classic", "thermal_layout": "thermal-compact"}
    assert workflow.THERMAL_LAYOUTS == tuple(THERMAL_IDS)  # existing thermal validation unchanged


def test_catalog_readable_by_invoice_viewers_only_and_org_scoped(db_session: Session):
    org_a, admin_a = _org(db_session, "Theme Org A", {"template": "modern", "template_variant": "gst-5"})
    org_b, admin_b = _org(db_session, "Theme Org B", {"template": "compact", "template_variant": "gst-3"})
    viewer = _staff(db_session, org_a, {"invoices": {"view": True}})
    outsider = _staff(db_session, org_a, {"products": {"view": True}})

    res = _client(viewer).get("/invoice-templates")
    assert res.status_code == 200, res.text
    assert res.json()["selected"]["template_variant"] == "gst-5"  # own firm only
    assert _client(admin_b).get("/invoice-templates").json()["selected"]["template_variant"] == "gst-3"

    assert _client(outsider).get("/invoice-templates").status_code == 403
    # The admin-only settings endpoints keep their authorization
    assert _client(viewer).get("/invoice-settings").status_code == 403
    assert _client(viewer).patch("/invoice-settings", json={"template_variant": "gst-1"}).status_code == 403


# -------------------------------- validation --------------------------------


def test_valid_selection_saved_and_invalid_rejected(db_session: Session):
    org, admin = _org(db_session, "Theme Validation Org")
    client = _client(admin)
    res = client.patch("/invoice-settings", json={"template": "compact", "template_variant": "gst-3"})
    assert res.status_code == 200, res.text
    assert (res.json()["template"], res.json()["template_variant"]) == ("compact", "gst-3")

    assert client.patch("/invoice-settings", json={"template_variant": "gst-99"}).status_code == 422
    assert client.patch("/invoice-settings", json={"template": "classic", "template_variant": "gst-5"}).status_code == 422
    # A thermal firm keeps a regular theme for its A4 copy, as the web app saves it
    assert client.patch("/invoice-settings", json={"template": "thermal", "template_variant": "gst-5"}).status_code == 200
    assert client.patch("/invoice-settings", json={"thermal_print": {"layout": "thermal-nope"}}).status_code == 422
    assert client.patch("/invoice-settings", json={"template": "modern", "template_variant": None}).status_code == 200
    db_session.refresh(org)
    assert org.invoice_template_settings["template_variant"] is None


def test_legacy_unknown_stored_variant_falls_back(db_session: Session, style_spy):
    org, admin = _org(db_session, "Theme Legacy Org", {"template": "modern", "template_variant": "removed-theme"})
    client = _client(admin)
    assert client.get("/invoice-settings").json()["template_variant"] == "removed-theme"  # still readable
    selected = client.get("/invoice-templates").json()["selected"]
    assert selected["resolved_template_variant"] == "modern"
    inv = _invoice(db_session, org)
    res = client.get(f"/invoices/{inv.id}/pdf")
    assert res.status_code == 200 and res.content.startswith(b"%PDF")
    assert invoice_regular_pdf.resolve_theme({"template": "modern", "template_variant": "removed-theme"})["id"] == "modern"


# ----------------------------------- PDFs -----------------------------------


def test_pdf_uses_selected_theme_and_overrides_do_not_persist(db_session: Session, style_spy):
    org, admin = _org(db_session, "Theme PDF Org", {"template": "modern", "template_variant": "gst-5"})
    client = _client(admin)
    inv = _invoice(db_session, org)

    for fmt in ("detailed", "simple"):
        style_spy.clear()
        res = client.get(f"/invoices/{inv.id}/pdf", params={"format": fmt})
        assert res.status_code == 200 and res.content.startswith(b"%PDF")
        assert "gst-5" in style_spy

    style_spy.clear()
    res = client.get(f"/invoices/{inv.id}/pdf", params={"template_variant": "french-elite"})
    assert res.status_code == 200
    assert "french-elite" in style_spy and "gst-5" not in style_spy

    res = client.get(f"/invoices/{inv.id}/pdf", params={"thermal_layout": "thermal-theme-2"})
    assert res.status_code == 200 and res.content.startswith(b"%PDF")

    for bad in ({"template_variant": "nope"}, {"thermal_layout": "nope"},
                {"template_variant": "gst-1", "thermal_layout": "thermal-simple"}):
        assert client.get(f"/invoices/{inv.id}/pdf", params=bad).status_code == 422

    db_session.refresh(org)
    assert org.invoice_template_settings == {"template": "modern", "template_variant": "gst-5"}

    # Another firm cannot download this invoice, with or without an override
    _, other_admin = _org(db_session, "Theme PDF Other Org")
    assert _client(other_admin).get(f"/invoices/{inv.id}/pdf").status_code == 404
    assert _client(other_admin).get(f"/invoices/{inv.id}/pdf", params={"template_variant": "gst-1"}).status_code == 404


@pytest.mark.parametrize("theme_id", REGULAR_IDS)
@pytest.mark.parametrize("fmt", ["detailed", "simple"])
def test_every_regular_theme_renders(theme_id, fmt):
    org = Organization(id=str(uuid.uuid4()), name="Render Org", company_code="CMP-10001")
    customer = Customer(name="Buyer", phone="9999999999")
    inv = Invoice(
        invoice_number="INV-R-1", invoice_date=datetime.now(timezone.utc), status="unpaid",
        subtotal=50000.0, tax=9000.0, total=59000.0, amount_paid=0.0, discount=0, additional_charges=0, round_off=0,
    )
    inv.items = [
        InvoiceItem(id=str(i), product_name=f"Item {i}", quantity=1, unit_price=1000.0, tax=180.0, line_total=1180.0)
        for i in range(50)
    ]
    theme = workflow.REGULAR_THEMES_BY_ID[theme_id]
    settings = workflow.invoice_settings(org)
    settings.update({"template": theme["base_template"], "template_variant": theme_id})
    settings["branding"] = {**settings["branding"], "primary_color": "#1F6FEB"}
    pdf = pdf_docs.INVOICE_PDF_FORMATS[fmt](org, customer, inv, settings)
    assert pdf.startswith(b"%PDF") and len(pdf) > 1000
    pages = pdf.count(b"/Type /Page") - pdf.count(b"/Type /Pages")
    assert 1 <= pages <= 6  # 50 lines paginate, nothing runs away


def test_theme_resolution_matches_frontend_fallbacks():
    """resolveRegularPresetId(): saved variant, else the base template's own theme, else classic."""
    resolve = invoice_regular_pdf.resolve_theme
    for name in ("classic", "modern", "compact"):
        assert resolve({"template": name})["id"] == name
        assert resolve({"template": name, "template_variant": "removed-theme"})["id"] == name
    assert resolve({"template": "classic", "template_variant": "tally"})["id"] == "tally"
    assert resolve({"template": "modern", "template_variant": "GST-5"})["id"] == "gst-5"
    assert resolve({})["id"] == "classic"


@pytest.mark.parametrize("layout", THERMAL_IDS)
def test_thermal_rendering_unchanged(layout):
    org = Organization(id=str(uuid.uuid4()), name="Thermal Org", company_code="CMP-10001")
    inv = Invoice(invoice_number="INV-TH-1", invoice_date=datetime.now(timezone.utc), status="unpaid",
                  subtotal=100.0, tax=18.0, total=118.0, amount_paid=0.0, discount=0, additional_charges=0, round_off=0)
    inv.items = [InvoiceItem(id="1", product_name="Tea", quantity=1, unit_price=100.0, tax=18.0, line_total=118.0)]
    settings = workflow.invoice_settings(org)
    settings.update({"template": "thermal", "template_variant": "gst-5"})
    settings["thermal_print"] = {**settings["thermal_print"], "layout": layout}
    assert pdf_docs._is_thermal(settings)  # a regular theme never leaks into thermal
    for builder in pdf_docs.INVOICE_PDF_FORMATS.values():
        assert builder(org, None, inv, settings).startswith(b"%PDF")


# ------------------------- frontend layout parity (regular) -------------------------
# The regular renderer mirrors invoiceTemplates.jsx's RegularInvoiceDocument. These tests
# record what it draws (text with colour and position, boxes, images) and assert the
# frontend's layout rules, independent of PDF text extraction.

BRAND = "#1F6FEB"
BRAND_RGB = (0x1F, 0x6F, 0xEB)
NEUTRAL_600 = invoice_regular_pdf.NEUTRAL[600]


def _png(size, color=(30, 30, 200)) -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _parity_invoice(items: int = 5, *, shipping: str | None = "Warehouse 7, Doddaballapur", extras: bool = True,
                    gstin: str | None = "29XYZAB5678C1Z2"):
    from app.models import Product, ProductPricing, ProductVariant

    org = Organization(id=str(uuid.uuid4()), name="Parity Org", company_code="CMP-10001", city="Bangalore",
                       state="Karnataka", pin_code="560095", registered_address="123 Business Park",
                       gst_number="29ABCDE1234F1Z5", phone="+91 98765 43210", email="billing@parity.in",
                       bank_name="HDFC Bank", bank_account_details="50200012345678", bank_ifsc="HDFC0001234",
                       upi_id="parity@hdfc")
    customer = Customer(name="Hotel Grand Meridian", phone="+91 90000 12345", gst_number=gstin,
                        billing_address="45 Residency Road, Bangalore", delivery_address=shipping)
    inv = Invoice(invoice_number="IN-PARITY-0001", invoice_date=datetime(2026, 7, 1, tzinfo=timezone.utc),
                  due_date=datetime(2026, 7, 16, tzinfo=timezone.utc), status="unpaid", subtotal=123456.0,
                  discount=250.0, tax=696.8, additional_charges=150.0 if extras else 0, round_off=0.2 if extras else 0,
                  total=124053.0, amount_paid=0.0, billing_address="45 Residency Road, Bangalore", notes="Handle with care")
    rows = []
    for i in range(items):
        item = InvoiceItem(id=f"line-{i}", product_name=f"Product line {i}", hsn_code="22011010", quantity=2,
                           unit_price=90.0, discount=20.0 if i == 0 else 0.0, tax=8.0, tax_amount=8.0, tax_rate=5.0,
                           line_total=160.0, uom="Jar")
        if i == 0:
            item.variant = ProductVariant(name="1L", mrp=150.0)  # MRP from the variant
        elif i == 1:
            product = Product(name="P")
            product.pricing = ProductPricing(purchase_price=1, selling_price=90, mrp=120.0)
            item.product = product  # MRP from product pricing
        rows.append(item)
    inv.items = rows
    return org, customer, inv


def _parity_settings(theme_id: str, **overrides):
    theme = workflow.REGULAR_THEMES_BY_ID[theme_id]
    settings = workflow.invoice_settings(Organization(invoice_template_settings={}))
    settings.update({"template": theme["base_template"], "template_variant": theme_id})
    settings["branding"] = {**settings["branding"], "primary_color": BRAND}
    settings["footer"] = {**settings["footer"], "terms": "Pay within 15 days", "footer_text": "Thank you!"}
    for key, value in overrides.items():
        settings[key] = {**settings.get(key, {}), **value} if isinstance(value, dict) else value
    return settings


@pytest.fixture
def drawn(monkeypatch):
    """Everything the regular renderer draws: text, boxes and images, in order."""
    record = {"text": [], "box": [], "image": [], "pages": 0}
    doc_cls = invoice_regular_pdf._Doc
    original_text, original_box = doc_cls.text, doc_cls.box
    original_image = pdf_docs.FPDF.image

    def text(self, x, y, width, value, size_px, **kw):
        record["text"].append({"text": str(value), "x": x, "y": y, "color": tuple(kw.get("color", NEUTRAL_600)),
                               "style": kw.get("style", ""), "page": self.pdf.page})
        return original_text(self, x, y, width, value, size_px, **kw)

    def box(self, x, y, w, h, **kw):
        record["box"].append({"y": y, "h": h, "w": w, "fill": kw.get("fill"), "page": self.pdf.page})
        return original_box(self, x, y, w, h, **kw)

    def image(self, name, x=None, y=None, w=0, h=0, **kw):
        record["image"].append({"x": x, "y": y, "w": w, "h": h, "page": self.page})
        return original_image(self, name, x=x, y=y, w=w, h=h, **kw)

    monkeypatch.setattr(doc_cls, "text", text)
    monkeypatch.setattr(doc_cls, "box", box)
    monkeypatch.setattr(pdf_docs.FPDF, "image", image)

    def render(theme_id="classic", fmt="detailed", invoice=None, settings=None, **assets):
        record["text"].clear(), record["box"].clear(), record["image"].clear()
        org, customer, inv = invoice or _parity_invoice()
        pdf = pdf_docs.INVOICE_PDF_FORMATS[fmt](org, customer, inv, settings or _parity_settings(theme_id), **assets)
        record["pages"] = pdf.count(b"/Type /Page") - pdf.count(b"/Type /Pages")
        record["pdf"] = pdf
        record["strings"] = [t["text"] for t in record["text"]]
        return record

    return render


def _find(record, needle):
    return [t for t in record["text"] if needle in t["text"]]


def test_inr_formatting_matches_frontend_money():
    inr = invoice_regular_pdf.inr
    assert inr(123456) == "₹ 1,23,456.00"
    assert inr(1234567.5) == "₹ 12,34,567.50"
    assert inr(999) == "₹ 999.00"
    assert inr(0.2) == "₹ 0.20"
    assert inr(-0.3) == "₹ -0.30"  # money(-0.3) in the frontend
    assert inr(None) == "₹ 0.00"


@pytest.mark.parametrize("theme_id", REGULAR_IDS)
@pytest.mark.parametrize("fmt", ["detailed", "simple"])
def test_every_theme_follows_frontend_section_layout(drawn, theme_id, fmt):
    rec = drawn(theme_id, fmt)
    s = rec["strings"]
    theme = workflow.REGULAR_THEMES_BY_ID[theme_id]
    # Title treatment per header axis
    if theme["header"] in ("band", "band-sm", "banner"):
        assert "INVOICE" in s and "TAX INVOICE" not in s
    else:
        assert "TAX INVOICE" in s
    # Header block, metadata, Bill To + Ship To, default frontend columns
    for needle in ("Parity Org", "Invoice No: IN-PARITY-0001", "Date: 01 Jul 2026", "Due: 16 Jul 2026",
                   "Bill To", "Hotel Grand Meridian", "Ship To", "ITEM", "HSN/SAC", "QUANTITY", "RATE", "AMOUNT"):
        assert _find(rec, needle), needle
    assert "DISCOUNT" not in s and not _find(rec, "Not Provided") and "Grand Total" not in s
    # Totals: frontend labels, stored figures, Additional Charges and Round Off preserved
    joined = " ".join(s)
    for needle in ("Subtotal", "Total", "₹ 1,23,456.00", "Additional Charges", "Round Off", "₹ 1,24,053.00"):
        assert needle in joined, needle
    # Simple hides payment details, terms, footer text and signature; the note stays
    footer_only = ("Bank: HDFC Bank", "Scan to pay via UPI", "Terms: ", "Thank you!", "Authorised Signatory")
    for needle in footer_only:
        assert bool(_find(rec, needle)) == (fmt == "detailed"), needle
    assert _find(rec, "Note: ")
    assert rec["pages"] >= 1 and b"DMSans" in rec["pdf"]  # rupee glyph font embedded


@pytest.mark.parametrize("theme_id", ["gst-3", "landscape-1", "gst-5", "classic"])  # boxed, inline, banded, plain
def test_simple_totals_hide_discount_and_tax(drawn, theme_id):
    import copy

    settings = _parity_settings(theme_id)
    assert settings["fields"]["show_discount"] and settings["fields"]["show_tax_amount"]
    before = copy.deepcopy(settings)

    detailed = " ".join(drawn(theme_id, "detailed", settings=settings)["strings"])
    assert "Discount" in detailed and "-₹ 250.00" in detailed
    assert "Tax" in detailed and "₹ 696.80" in detailed

    simple = " ".join(drawn(theme_id, "simple", settings=settings)["strings"])
    assert "Discount" not in simple and "-₹ 250.00" not in simple
    assert "Tax" not in simple and "₹ 696.80" not in simple
    for joined in (detailed, simple):  # hidden rows still count: the stored Total is unchanged
        for needle in ("Subtotal", "₹ 1,23,456.00", "Additional Charges", "Round Off", "₹ 1,24,053.00"):
            assert needle in joined, needle
    assert settings == before  # the firm's settings are not rewritten to print the simple copy

    hidden = _parity_settings(theme_id, fields={"show_discount": False, "show_tax_amount": False})
    detailed_off = " ".join(drawn(theme_id, "detailed", settings=hidden)["strings"])
    assert "Discount" not in detailed_off and "Tax" not in detailed_off and "₹ 1,24,053.00" in detailed_off


@pytest.mark.parametrize("theme_id", REGULAR_IDS)
def test_table_header_never_brand_filled_and_accent_rules(drawn, theme_id):
    rec = drawn(theme_id)
    theme = workflow.REGULAR_THEMES_BY_ID[theme_id]
    header_row_y = _find(rec, "ITEM")[0]["y"]
    assert not [b for b in rec["box"] if b["fill"] == BRAND_RGB and abs(b["y"] - header_row_y) < 0.5]
    bill_to = _find(rec, "Bill To")[0]["color"]
    item_head = _find(rec, "ITEM")[0]["color"]
    if theme["accent"] == "bold":
        assert bill_to == BRAND_RGB and item_head == BRAND_RGB
    else:  # standard / minimal / title-only are identical in the frontend
        assert bill_to == NEUTRAL_600 and item_head == invoice_regular_pdf.NEUTRAL[400]
    on_color = theme["header"] in ("band", "band-sm", "banner", "banner-split")
    assert (_find(rec, "Parity Org")[0]["color"] == BRAND_RGB) == (not on_color)
    if theme["totals"] in ("plain", "boxed", "grid"):
        assert [t for t in rec["text"] if t["text"] == "Total"][0]["color"] == BRAND_RGB  # minimal/title-only keep colour


def test_default_primary_color_when_unset(drawn):
    settings = _parity_settings("classic")
    settings["branding"] = {**settings["branding"], "primary_color": None}
    rec = drawn(settings=settings)
    assert _find(rec, "Parity Org")[0]["color"] == (0x00, 0x09, 0x2A)  # frontend '#00092A'


def test_item_cells_mrp_discount_tax_rate(drawn):
    settings = _parity_settings("classic", item_table={"columns": ["product", "mrp", "discount", "tax_rate", "amount"]})
    rec = drawn(settings=settings)
    for needle in ("MRP", "DISCOUNT", "TAX RATE"):
        assert needle in rec["strings"]
    assert "₹ 150.00" in rec["strings"]  # variant MRP, not the 90.00 sale price
    assert "₹ 120.00" in rec["strings"]  # product pricing MRP
    assert "₹ 20.00" in rec["strings"]   # stored discount amount
    assert "5%" in rec["strings"]
    assert rec["strings"].count("—") >= 3  # no MRP / no discount print a dash


def test_letterhead_reserves_space_and_images_keep_aspect(drawn):
    letterhead, logo = _png((1200, 150)), _png((240, 120))
    rec = drawn("classic", letterhead=letterhead, logo=logo, signature=_png((240, 80)), stamp=_png((120, 120)), qr=_png((200, 200)))
    head = rec["image"][0]
    assert abs(head["w"] / head["h"] - 8.0) < 0.01  # 1200x150 drawn at 8:1
    company_y = _find(rec, "Parity Org")[0]["y"]
    assert company_y >= head["y"] + head["h"]  # header starts below the letterhead
    for img in rec["image"][1:]:
        assert img["w"] > 0 and img["h"] > 0
    assert any(abs(i["w"] / i["h"] - 2.0) < 0.01 for i in rec["image"])  # 2:1 logo not squashed square


def test_ship_to_only_when_present_and_missing_optional_fields(drawn):
    rec = drawn(invoice=_parity_invoice(shipping=None, gstin=None, extras=False))
    assert not _find(rec, "Ship To") and not _find(rec, "GSTIN: 29XYZ")
    assert not _find(rec, "Additional Charges") and not _find(rec, "Round Off")
    assert _find(rec, "Bill To") and _find(rec, "Hotel Grand Meridian")


def test_long_invoice_repeats_table_heading_without_overlap(drawn):
    org, customer, inv = _parity_invoice(60)
    rec = drawn("gst-4", invoice=(org, customer, inv))
    assert rec["pages"] >= 2
    headings = _find(rec, "ITEM")
    assert len({h["page"] for h in headings}) >= rec["pages"] - 1  # every table page has its heading
    assert len([t for t in rec["text"] if t["text"] == "₹ 160.00"]) == 60  # no row dropped
    for page in {t["page"] for t in rec["text"]}:
        ys = [t["y"] for t in rec["text"] if t["page"] == page]
        assert max(ys) < 297  # nothing below the A4 page


def test_missing_and_invalid_assets_are_skipped(drawn):
    rec = drawn("modern", logo=b"not an image", signature=b"", qr=None, stamp=b"\x89PNG broken", letterhead=b"junk")
    assert rec["pdf"].startswith(b"%PDF")
    assert "Signature" in rec["strings"] and "Payment QR unavailable" in rec["strings"]


def test_thermal_never_uses_regular_renderer(monkeypatch):
    called = []
    monkeypatch.setattr(invoice_regular_pdf, "render_regular_invoice", lambda *a, **k: called.append(1))
    org, customer, inv = _parity_invoice()
    settings = _parity_settings("gst-5")
    settings["template"] = "thermal"
    for builder in pdf_docs.INVOICE_PDF_FORMATS.values():
        assert builder(org, customer, inv, settings).startswith(b"%PDF")
    assert not called
