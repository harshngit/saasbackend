"""GET /reports/{type}/export?format=pdf -- the shared PDF renderer (app/core/report_pdf.py).

Every report type and grouping is exported from real rows with long names / numbers, and the
renderer's layout facts are checked: every source row drawn, no cell text lost or wider than
its cell, the header on every page, the table inside the page, rows above the footer. The
same rows are exported to Excel. Also: Purchases by Product (used to 500), formatting
helpers, multi-page synthetic data in each report's real columns, Balance Sheet still gated
by the plan. Two firms.
"""

import io
import os
import sys
import uuid
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402

from app.core import report_pdf  # noqa: E402
from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.core.security import create_access_token  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Customer, CustomerPayment, Expense, Invoice, InvoiceItem, Organization, Product, PurchaseInvoice,
    PurchaseInvoiceItem, StockMovement, Supplier, SupplierInvoice, SupplierInvoiceItem, SupplierPayment, User,
    Warehouse,
)
from app.models.enums import UserRole  # noqa: E402
from app.services import entitlement_service  # noqa: E402
from app.services.report_service import REPORT_REGISTRY  # noqa: E402

c = TestClient(app)
RANGE = {"date_from": "2026-01-01", "date_to": "2026-12-31"}
LONG_CUSTOMER = "Shree Ganesh Mahalaxmi Wholesale Traders and General Merchants Private Limited"
LONG_SUPPLIER = "Bharat Heavy Industrial Components and Precision Engineering Supplies Corporation"
LONG_PRODUCT = "Premium Stainless Steel Industrial Grade Double Walled Insulated Storage Container 50L"
LONG_NUMBER = "INV-2026-27/MUMBAI-WEST/BRANCH-0042/"


@pytest.fixture(scope="module", autouse=True)
def _schema():
    Base.metadata.create_all(engine)


def _mk(model, **kw):
    return model(**{k: v for k, v in kw.items() if hasattr(model, k)})


def _firm(entitled=True):
    tag = uuid.uuid4().hex[:5]
    when = datetime(2026, 11, 20, 6, 0, tzinfo=timezone.utc)
    with SessionLocal() as db:
        org = Organization(name=f"Very Long Organisation Name Private Limited {tag}",
                           company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}", timezone="Asia/Kolkata")
        db.add(org)
        db.flush()
        u = User(email=f"r.{uuid.uuid4().hex[:8]}@example.com", name=f"Salesperson With A Long Name {tag}",
                 organization_id=org.id, system_role="admin", role=UserRole.ADMIN, password_hash="x", is_active=True)
        wh = _mk(Warehouse, organization_id=org.id, name="Main Warehouse Bhiwandi Logistics Park", code=f"W{tag}",
                 is_default=True, is_active=True)
        sup = _mk(Supplier, organization_id=org.id, name=f"{LONG_SUPPLIER} {tag}", supplier_code=f"S{tag}",
                  gstin="27AABCU9603R1ZX", gst_number="27AABCU9603R1ZX")
        prod = _mk(Product, organization_id=org.id, name=f"{LONG_PRODUCT} {tag}", sku=f"SKU-{tag}", price=100.0,
                   is_active=True)
        db.add_all([u, wh, sup, prod])
        db.flush()
        custs = []
        for i in range(3):
            cu = _mk(Customer, organization_id=org.id, name=f"{LONG_CUSTOMER} {i} {tag}",
                     business_name=f"{LONG_CUSTOMER} Retail {i}", gstin="27AAPFU0939F1ZV", gst_number="27AAPFU0939F1ZV",
                     outstanding_balance=1000.0 * (i + 1))
            db.add(cu)
            custs.append(cu)
        db.flush()
        for i in range(40):
            inv = _mk(Invoice, organization_id=org.id, invoice_number=f"{LONG_NUMBER}{i:05d}-{tag}",
                      customer_id=custs[i % 3].id, invoice_date=when, due_date=when, status="issued",
                      invoice_status="issued", payment_status=("unpaid", "partially_paid", "paid")[i % 3],
                      subtotal=1000.0 + i, tax=180.0, total=1180.0 + i, amount_paid=(0.0, 500.0, 1180.0 + i)[i % 3],
                      created_by=u.id)
            db.add(inv)
            db.flush()
            db.add(_mk(InvoiceItem, invoice_id=inv.id, organization_id=org.id, product_id=prod.id,
                       product_name=prod.name, quantity=2, unit_price=500.0, tax_rate=18.0, tax_amount=180.0,
                       total=1180.0 + i, line_total=1180.0 + i, amount=1180.0 + i))
        for i in range(8):
            db.add(_mk(CustomerPayment, organization_id=org.id, customer_id=custs[i % 3].id, amount=250.5 + i,
                       payment_mode=("cash", "upi", "bank_transfer", "cheque")[i % 4], receipt_number=f"RCPT-{tag}-{i}",
                       reference=f"UTR{tag}{i:012d}", received_on=when, collected_by_user_id=u.id))
        for i in range(6):
            pi = _mk(PurchaseInvoice, organization_id=org.id, invoice_number=f"PUR-{tag}-{i}", supplier_id=sup.id,
                     invoice_date=when, purchase_date=when, status="received", total=5900.0, subtotal=5000.0,
                     tax=900.0, warehouse_id=wh.id)
            db.add(pi)
            db.flush()
            pii = _mk(PurchaseInvoiceItem, purchase_invoice_id=pi.id, invoice_id=pi.id, product_id=prod.id,
                      product_name=prod.name, product_code=prod.sku, quantity=10, unit_price=500.0)
            db.add(pii)
            db.flush()
            si = _mk(SupplierInvoice, organization_id=org.id, supplier_id=sup.id, purchase_id=pi.id,
                     supplier_invoice_number=f"SUPINV/{tag}/LONG-REFERENCE-{i:04d}", supplier_invoice_date=when,
                     due_date=when, status="recorded", verification_status="verified",
                     payment_status=("unpaid", "partially_paid")[i % 2], subtotal=5000.0, tax_amount=900.0,
                     grand_total=5900.0, amount_paid=(0.0, 1000.0)[i % 2])
            db.add(si)
            db.flush()
            db.add(SupplierInvoiceItem(supplier_invoice_id=si.id, purchase_item_id=pii.id, product_id=prod.id,
                                       billed_qty=10, unit_price=500.0, tax_rate=18.0, tax_amount=900.0,
                                       line_total=5900.0))
        for i in range(3):
            db.add(_mk(SupplierPayment, organization_id=org.id, supplier_id=sup.id, amount=1000.0,
                       payment_number=f"SPAY-{tag}-{i}", payment_mode="bank_transfer", payment_method="bank_transfer",
                       paid_on=when, status="completed", reference=f"NEFT{tag}{i:014d}"))
        for i in range(12):
            db.add(_mk(Expense, organization_id=org.id, category="Travel and Conveyance Expenses for Field Staff",
                       amount=1234.5 + i, description=f"Outstation travel reimbursement for client site visits {i} "
                                                         "including toll, parking, fuel and lodging charges",
                       expense_date=when, payment_mode="upi", status="approved", expense_status="approved",
                       approval_status="approved", payment_status="paid", submitted_by=u.id,
                       expense_number=f"EXP-{tag}-{i:05d}", payee_name=f"{LONG_SUPPLIER} Travels"))
        for i in range(30):
            db.add(StockMovement(organization_id=org.id, warehouse_id=wh.id, product_id=prod.id,
                                 movement_type=("purchase_receipt", "sale_dispatch", "adjustment")[i % 3],
                                 quantity=(10, -2, 1)[i % 3], balance_after=10 + i,
                                 note=f"Stock movement against {LONG_NUMBER}{i:05d} for {LONG_CUSTOMER}",
                                 created_by=u.id, created_at=when))
        db.commit()
        return {"org": org.id, "h": {"Authorization": f"Bearer {create_access_token(u.id, 'admin', org.id)}"},
                "tag": tag}


NOTES_TO_PL = ("revenue", "sales_returns", "cogs", "operating_expenses")


def _views():
    """(report type, extra params) for every report view the screens offer."""
    for rtype, reg in REPORT_REGISTRY.items():
        if rtype in ("balance-sheet", "notes-to-balance-sheet"):
            continue
        if rtype == "notes-to-pl":
            for item in NOTES_TO_PL:
                yield rtype, {"line_item": item}
            continue
        for group in reg.get("allowed_group_by") or [None]:
            yield rtype, ({"group_by": group} if group else {})


@pytest.fixture
def entitled(monkeypatch):
    monkeypatch.setattr(entitlement_service, "check_entitlement", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "has_entitlement", lambda *a, **k: True)


@pytest.fixture
def capture(monkeypatch):
    seen = []
    original = report_pdf.render

    def spy(report, org_name=None, stats=None):  # noqa: ANN001
        st = {}
        out = original(report, org_name, stats=st)
        seen.append((report, st))
        return out

    monkeypatch.setattr(report_pdf, "render", spy)
    return seen


def _check_layout(st, expected_rows):
    assert st["rows_drawn"] == expected_rows
    assert st["lost_text"] == [], st["lost_text"][:3]
    assert st["overflow"] == [], st["overflow"][:3]
    assert sorted(set(st["header_pages"])) == list(range(st["header_pages"][0], st["pages"] + 1))
    assert st["table_width"] <= st["width"] + 0.05
    assert st["row_bottom_max"] <= st["bottom"] + 0.01


def test_every_report_exports_pdf_and_excel_with_all_rows(entitled, capture):
    f, other = _firm(), _firm()
    matrix = []
    for rtype, group in _views():
        params = {**RANGE, **group}
        js = c.get(f"/reports/{rtype}", headers=f["h"], params={**params, "page_size": 100})
        assert js.status_code == 200, (rtype, group, js.text[:300])
        body = js.json()
        total = (body.get("pagination") or {}).get("total", len(body.get("rows") or []))
        r = c.get(f"/reports/{rtype}/export", headers=f["h"], params={**params, "format": "pdf"})
        assert r.status_code == 200, (rtype, group, r.text[:300])
        assert r.headers["content-type"] == "application/pdf" and r.content[:5] == b"%PDF-"
        report, st = capture[-1]
        assert len(report["rows"]) == total, (rtype, group)
        _check_layout(st, total)
        # No other firm's names leak in.
        assert other["tag"] not in str(report["rows"])
        x = c.get(f"/reports/{rtype}/export", headers=f["h"], params={**params, "format": "excel"})
        assert x.status_code == 200, (rtype, group, x.text[:300])
        from openpyxl import load_workbook
        ws = load_workbook(io.BytesIO(x.content)).active
        values = [[cell for cell in row] for row in ws.iter_rows(values_only=True)]
        labels = [col["label"] for col in (report.get("meta") or {}).get("columns") or []]
        if labels:
            head = next(i for i, row in enumerate(values) if list(row[:len(labels)]) == labels)
            assert len([row for row in values[head + 1:] if any(v is not None for v in row)]) == total, (rtype, group)
        matrix.append((rtype, next(iter(group.values()), None), total, st["pages"], st["orientation"]))
    by_key = {(m[0], m[1]): m for m in matrix}
    # The seeded data reaches the main reports.
    assert by_key[("sales", "transaction")][2] == 40
    assert by_key[("purchase", "product")][2] == 1
    assert by_key[("expense", None)][2] == 12
    assert by_key[("stock-movement", None)][2] >= 30
    print("\nMATRIX", *matrix, sep="\n")


def test_purchases_by_product_no_longer_500(entitled):
    f = _firm()
    r = c.get("/reports/purchase", headers=f["h"], params={**RANGE, "group_by": "product"})
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert len(rows) == 1 and rows[0]["product"].startswith(LONG_PRODUCT)
    assert rows[0]["quantity_purchased"] == 60.0 and rows[0]["purchase_value"] == 6 * 5900.0
    for group in ("transaction", "supplier"):
        assert c.get("/reports/purchase", headers=f["h"], params={**RANGE, "group_by": group}).status_code == 200
    pdf = c.get("/reports/purchase/export", headers=f["h"], params={**RANGE, "group_by": "product", "format": "pdf"})
    assert pdf.status_code == 200 and pdf.content[:5] == b"%PDF-"


def test_multi_page_long_values_in_every_report_layout(entitled):
    """Each report's real columns, 150 rows of long values: wraps, repeats the header, keeps
    every row, never overflows -- in portrait or landscape as chosen."""
    f = _firm()
    samples = {
        "currency": 987654321.456, "number": 1234567.5, "percent": 12.3456, "date": "2026-11-26",
        "datetime": "2026-11-26T09:45:00Z", "status": "partially_paid",
        "reference": f"{LONG_NUMBER}000123/ADDITIONAL-SUFFIX", "text": f"{LONG_CUSTOMER} {LONG_PRODUCT}",
    }
    for rtype, group in _views():
        params = {**RANGE, **group}
        cols = c.get(f"/reports/{rtype}", headers=f["h"], params=params).json()["meta"].get("columns") or []
        if not cols:
            continue
        rows = [{col["key"]: samples.get(col.get("type") or "text", samples["text"]) for col in cols} for _ in range(150)]
        st = {}
        out = report_pdf.render({"type": rtype, "meta": {"columns": cols}, "rows": rows, "summary": {}}, "Org", stats=st)
        assert out[:5] == b"%PDF-"
        _check_layout(st, 150)
        assert st["pages"] > 1


def test_formatting_helpers():
    assert report_pdf.money(1180.0) == "₹ 1,180.00"
    assert report_pdf.money(12345678.9) == "₹ 1,23,45,678.90"
    assert report_pdf.money(-500) == "-₹ 500.00"
    assert report_pdf.format_value("2026-11-26", "date") == "26 Nov 2026"
    assert report_pdf.format_value("2026-11-26T09:45:00Z", "datetime") == "26 Nov 2026, 03:15 PM"
    assert report_pdf.format_value("partially_paid", "status") == "Partially Paid"
    assert report_pdf.format_value("gst_output", "status") == "GST Output"
    assert report_pdf.format_value(12.5, "percent") == "12.50%"
    assert report_pdf.format_value(1500, "number") == "1,500"
    assert report_pdf.format_value(None, "currency") == ""
    assert report_pdf.format_value("supplier_invoice", "text") == "Supplier Invoice"
    assert report_pdf.format_value("upi", "text") == "UPI"
    assert report_pdf.format_value("0_30", "text") == "0–30 days"
    assert report_pdf.format_value("90_plus", "status") == "90+ days"
    assert report_pdf.label("age_0_30") == "Age 0–30"
    assert report_pdf.format_value("Shree Ganesh Traders", "text") == "Shree Ganesh Traders"
    assert report_pdf.format_summary("total_invoices", 40) == "40"
    assert report_pdf.format_summary("total_sales", 1180.0) == "₹ 1,180.00"
    assert report_pdf.format_summary("gross_margin_pct", 25.0) == "25.00%"
    # Without a column list, ids are not printed.
    cols = report_pdf._columns({"rows": [{"id": str(uuid.uuid4()), "customer_id": "x", "customer": "A", "amount": 1.0}]})
    assert [col["key"] for col in cols] == ["customer", "amount"]


def test_balance_sheet_still_gated_by_plan(monkeypatch):
    """Real entitlement checks (the test harness may stub them) and a Basic-plan firm."""
    import importlib.util

    from app.core.entitlements import default_entitlements_for_plan
    from app.models import Plan

    spec = importlib.util.find_spec("app.services.entitlement_service")
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    for name in ("check_entitlement", "has_entitlement"):
        monkeypatch.setattr(entitlement_service, name, getattr(real, name))
    f = _firm()
    with SessionLocal() as db:
        plan = Plan(name=f"Basic {f['tag']}", price_monthly=0, price_yearly=0,
                    entitlements=default_entitlements_for_plan("Basic"))
        db.add(plan)
        db.flush()
        db.get(Organization, f["org"]).plan_id = plan.id
        db.commit()
    for rtype in ("balance-sheet", "notes-to-balance-sheet"):
        for path in (f"/reports/{rtype}", f"/reports/{rtype}/export"):
            r = c.get(path, headers=f["h"], params={"format": "pdf"} if path.endswith("export") else {})
            assert r.status_code in (402, 403), (path, r.status_code)


NON_LATIN = {
    "telugu": "పీటర్8 ట్రేడర్స్",
    "hindi": "राम किराना स्टोर",
    "marathi": "श्री गणेश व्यापारी मंडळ",
    "emoji": "Chai ☕ & snacks 😀👨‍👩‍👧👍🏽",
}


def test_non_latin_and_emoji_text_never_breaks_pdf(entitled, capture):
    """Telugu / Hindi / Marathi / emoji in names and descriptions: every report's PDF is 200.
    The bundled font has no glyphs for them, so each such word prints as "?" (as the old
    export did) instead of a 500; Latin text and ₹ are untouched."""
    f = _firm()
    with SessionLocal() as db:
        org = db.get(Organization, f["org"])
        org.name = f"{NON_LATIN['marathi']} {f['tag']}"
        for model, field in ((Expense, "description"), (Expense, "category"), (Expense, "payee_name"),
                             (Customer, "name"), (Customer, "business_name"), (Supplier, "name"),
                             (Product, "name"), (StockMovement, "note")):
            for i, row in enumerate(db.query(model).filter(model.organization_id == f["org"]).all()):
                setattr(row, field, f"{list(NON_LATIN.values())[i % 4]} {list(NON_LATIN.values())[(i + 1) % 4]} {i}")
        db.commit()
    for rtype, group in _views():
        params = {**RANGE, **group}
        r = c.get(f"/reports/{rtype}/export", headers=f["h"], params={**params, "format": "pdf"})
        assert r.status_code == 200, (rtype, group, r.text[:300])
        assert r.content[:5] == b"%PDF-"
        report, st = capture[-1]
        _check_layout(st, len(report["rows"]))
    exp = c.get("/reports/expense/export", headers=f["h"], params={**RANGE, "format": "pdf"})
    assert exp.status_code == 200

    doc = report_pdf._Doc()
    doc.add_font(report_pdf.FAMILY, "", str(report_pdf.FONT_DIR / "DMSans-Regular.ttf"))
    doc.set_font(report_pdf.FAMILY, "", 8)
    assert doc.normalize_text("పీటర్8") == "???8"  # one "?" per letter, vowel signs / virama dropped
    assert doc.normalize_text("Chai ☕ 😀👨‍👩‍👧 ok") == "Chai ? ? ok"
    assert doc.normalize_text("Café ₹ 1,180.00 – ok?") == "Café ₹ 1,180.00 – ok?"  # supported text unchanged


def test_gstin_stays_on_one_line_in_wide_gst_summary(entitled):
    cols = c.get("/reports/gst-summary", headers=_firm()["h"],
                 params={**RANGE, "group_by": "transaction"}).json()["meta"]["columns"]
    row = {col["key"]: {"currency": 123456.78, "date": "2026-11-20", "status": "partially_paid",
                        "reference": "INV/2026-27/000123"}.get(col["type"], "Very Long Customer Trading Company")
           for col in cols}
    row["gst_number"] = "27AAACV1234F1Z5"
    st = {}
    report_pdf.render({"type": "gst-summary", "meta": {"columns": cols}, "rows": [row] * 40}, stats=st)
    _check_layout(st, 40)
    i = [col["key"] for col in cols].index("gst_number")
    doc = report_pdf._Doc()
    doc.add_font(report_pdf.FAMILY, "", str(report_pdf.FONT_DIR / "DMSans-Regular.ttf"))
    doc.set_font(report_pdf.FAMILY, "", st["font_size"])
    assert st["col_widths"][i] >= doc.get_string_width("27AAACV1234F1Z5") + 2 * report_pdf.PAD_X
