"""Invoice theme catalog: GET /invoice-templates, template_variant validation on
PATCH /invoice-settings, and theme-aware PDF rendering with per-download overrides."""

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core import pdf_docs, workflow
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
    original = pdf_docs._style

    def spy(settings):
        seen.append((pdf_docs._regular_theme(settings) or {}).get("id"))
        return original(settings)

    monkeypatch.setattr(pdf_docs, "_style", spy)
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
    assert pdf_docs._style({"template": "modern", "template_variant": "removed-theme"}) == pdf_docs._TEMPLATE_STYLES["modern"]


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


def test_base_and_unthemed_styles_unchanged():
    for name in ("classic", "modern", "compact"):
        assert pdf_docs._style({"template": name}) is pdf_docs._TEMPLATE_STYLES[name]
        assert pdf_docs._style({"template": name, "template_variant": name}) is pdf_docs._TEMPLATE_STYLES[name]
    themed = pdf_docs._style({"template": "classic", "template_variant": "tally"})
    assert themed["border"] == 1 and themed["header_fill"] and themed["row_height"] < pdf_docs._TEMPLATE_STYLES["classic"]["row_height"]


@pytest.mark.parametrize("layout", THERMAL_IDS)
def test_thermal_rendering_unchanged(layout):
    org = Organization(id=str(uuid.uuid4()), name="Thermal Org", company_code="CMP-10001")
    inv = Invoice(invoice_number="INV-TH-1", invoice_date=datetime.now(timezone.utc), status="unpaid",
                  subtotal=100.0, tax=18.0, total=118.0, amount_paid=0.0, discount=0, additional_charges=0, round_off=0)
    inv.items = [InvoiceItem(id="1", product_name="Tea", quantity=1, unit_price=100.0, tax=18.0, line_total=118.0)]
    settings = workflow.invoice_settings(org)
    settings.update({"template": "thermal", "template_variant": "gst-5"})
    settings["thermal_print"] = {**settings["thermal_print"], "layout": layout}
    assert pdf_docs._regular_theme(settings) is None  # a regular theme never leaks into thermal
    for builder in pdf_docs.INVOICE_PDF_FORMATS.values():
        assert builder(org, None, inv, settings).startswith(b"%PDF")
