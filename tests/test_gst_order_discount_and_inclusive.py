"""GST/HSN QA 16 Nov:

1. An order-level (bill-level) discount is split over the lines before GST -- order create,
   order update (lines replaced, and discount-only), invoices from orders (whole and part),
   and the quick bill (POST /invoices).
2. Tax-inclusive pricing (pricing.tax_inclusive) is honoured and persists.
3. Invoice JSON exposes taxable value, CGST/SGST vs IGST, HSN, customer GSTIN, tax_amount.
4. order_status label follows confirmation.
"""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Customer, Organization, Product, ProductPricing, SalesOrder, User, Warehouse, WarehouseStock,
)
from app.models.enums import UserRole  # noqa: E402
from app.services import entitlement_service, gst_calc, role_service  # noqa: E402

client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)


@pytest.fixture(autouse=True)
def _no_plan_gates(monkeypatch):
    monkeypatch.setattr(entitlement_service, "check_entitlement", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "check_limit", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "has_entitlement", lambda *a, **k: True)


@pytest.fixture
def firm():
    db = SessionLocal()
    try:
        org = Organization(name=f"GST {uuid.uuid4().hex[:6]}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}",
                           gst_number="27AAAAA0000A1Z5", state="Maharashtra", currency="INR")
        db.add(org)
        db.flush()
        role_service.seed_default_roles(db, org.id)
        admin = User(email=f"admin.{uuid.uuid4().hex[:8]}@example.com", name="Admin", organization_id=org.id,
                     system_role="admin", role=UserRole.ADMIN, password_hash=hash_password("Password123!"),
                     is_active=True)
        wh = Warehouse(organization_id=org.id, name="Main", code=f"W{uuid.uuid4().hex[:4]}", is_default=True)
        local = Customer(organization_id=org.id, name="Pune Shop", gst_number="27BBBBB1111B1Z5", state="Maharashtra")
        other_state = Customer(organization_id=org.id, name="Bengaluru Shop", gst_number="29CCCCC2222C1Z5")
        unknown = Customer(organization_id=org.id, name="No State Shop")
        db.add_all([admin, wh, local, other_state, unknown])
        db.flush()
        products = {}
        for key, rate, incl, price in (("p18", 18.0, False, 1000.0), ("p5", 5.0, False, 1000.0),
                                       ("p0", 0.0, False, 500.0), ("incl", 18.0, True, 118.0)):
            p = Product(organization_id=org.id, name=f"GST {key}", sku=f"{key}-{uuid.uuid4().hex[:5]}", price=price,
                        tax_rate=rate, hsn_code=f"HSN{key}", total_inventory=1000, is_active=True)
            p.pricing = ProductPricing(selling_price=price, purchase_price=0, tax_inclusive=incl, currency="INR")
            db.add(p)
            db.flush()
            db.add(WarehouseStock(organization_id=org.id, warehouse_id=wh.id, product_id=p.id, on_hand_quantity=1000))
            products[key] = p.id
        db.commit()
        return {"org": org.id, "wh": wh.id, "local": local.id, "other_state": other_state.id, "unknown": unknown.id,
                "p": products, "h": {"Authorization": f"Bearer {create_access_token(admin.id, 'admin', org.id)}"}}
    finally:
        db.close()


def _order(f, items, discount=0.0, customer=None, confirm=True, **extra):
    r = client.post("/orders", headers=f["h"], json={
        "customer_id": customer or f["local"], "warehouse_id": f["wh"], "discount": discount,
        "items": [{"product_id": f["p"][k], "quantity": q} for k, q in items], **extra})
    assert r.status_code == 201, r.text
    order = r.json()
    if confirm and order["status"] == "draft":
        c = client.post(f"/orders/{order['id']}/confirm", headers=f["h"])
        assert c.status_code == 200, c.text
        order = c.json()
    return order


def _db_order(order_id):
    db = SessionLocal()
    try:
        o = db.get(SalesOrder, order_id)
        return {"subtotal": o.subtotal, "discount": o.discount, "tax": o.tax, "total": o.total,
                "order_status": o.order_status,
                "lines": {i.product_name: (i.line_total, i.tax_amount) for i in o.items}}
    finally:
        db.close()


# ------------------------------------------------------------------------------ 1. orders
def test_order_level_discount_is_taken_before_gst(firm):
    o = _order(firm, [("p18", 2)], discount=200.0)              # 2000 - 200 = 1800 taxable
    d = _db_order(o["id"])
    assert d["subtotal"] == 2000.0 and d["tax"] == 324.0 and d["total"] == 2124.0   # was 360 / 2160


def test_mixed_rates_split_proportionally(firm):
    o = _order(firm, [("p18", 1), ("p5", 1), ("p0", 1)], discount=250.0)   # values 1000 / 1000 / 500
    d = _db_order(o["id"])
    lines = d["lines"]
    assert lines["GST p18"] == (1000.0, 162.0)     # share 100 -> 900 x 18%
    assert lines["GST p5"] == (1000.0, 45.0)       # share 100 -> 900 x 5%
    assert lines["GST p0"] == (500.0, 0.0)         # share 50
    assert d["tax"] == 207.0 and d["total"] == 2457.0


def test_shares_add_up_to_the_paisa():
    shares = gst_calc.split_discount([gst_calc.money(100)] * 3, 10)
    assert sum(shares) == gst_calc.money(10) and sorted(shares) == [gst_calc.money("3.33")] * 2 + [gst_calc.money("3.34")]


def test_changing_only_the_discount_retaxes_the_order(firm):
    o = _order(firm, [("p18", 2)], discount=200.0)
    r = client.patch(f"/orders/{o['id']}", headers=firm["h"], json={"discount": 0})
    assert r.status_code == 200, r.text
    d = _db_order(o["id"])
    assert d["tax"] == 360.0 and d["total"] == 2360.0
    r = client.patch(f"/orders/{o['id']}", headers=firm["h"], json={"discount": 500})
    d = _db_order(o["id"])
    assert d["tax"] == 270.0 and d["total"] == 1770.0


def test_flat_order_level_tax_is_still_honoured(firm):
    o = _order(firm, [("p18", 1)], discount=100.0, tax=50.0)
    d = _db_order(o["id"])
    assert d["tax"] == 50.0 and d["total"] == 950.0
    client.patch(f"/orders/{o['id']}", headers=firm["h"], json={"discount": 0})
    assert _db_order(o["id"])["tax"] == 50.0        # a flat tax is not replaced by a discount edit


# ----------------------------------------------------------------------- 2. tax-inclusive
def test_tax_inclusive_price_is_taken_apart(firm):
    o = _order(firm, [("incl", 1)])
    d = _db_order(o["id"])
    assert d["lines"]["GST incl"] == (100.0, 18.0) and d["total"] == 118.0     # customer pays the shown price
    o = _order(firm, [("incl", 1)], discount=10.0)
    d = _db_order(o["id"])
    assert d["lines"]["GST incl"] == (100.0, 16.2) and d["total"] == 106.2     # 90 taxable + 16.20
    # a discount-only edit back to 0 recovers the exact inclusive GST (not 84.75-style rounding drift)
    o = _order(firm, [("incl", 3)])
    client.patch(f"/orders/{o['id']}", headers=firm["h"], json={"discount": 25})
    client.patch(f"/orders/{o['id']}", headers=firm["h"], json={"discount": 0})
    d = _db_order(o["id"])
    assert d["lines"]["GST incl"] == (300.0, 54.0) and d["total"] == 354.0


def test_tax_inclusive_flag_persists_and_is_mirrored(firm):
    r = client.post("/products", headers=firm["h"], json={
        "name": f"Incl {uuid.uuid4().hex[:4]}", "tax_rate": 18,
        "pricing": {"selling_price": 236, "purchase_price": 100, "tax_inclusive": True}})
    assert r.status_code in (200, 201), r.text
    pid = r.json()["id"]
    assert client.get(f"/products/{pid}", headers=firm["h"]).json()["pricing"]["tax_inclusive"] is True
    db = SessionLocal()
    try:
        p = db.get(Product, pid)
        assert p.tax_inclusive is True and p.tax_inclusive_price is True
    finally:
        db.close()
    r = client.patch(f"/products/{pid}", headers=firm["h"], json={"pricing": {"tax_inclusive": False}})
    if r.status_code == 405:
        r = client.put(f"/products/{pid}", headers=firm["h"], json={"pricing": {"tax_inclusive": False}})
    assert r.status_code == 200, r.text
    assert client.get(f"/products/{pid}", headers=firm["h"]).json()["pricing"]["tax_inclusive"] is False
    db = SessionLocal()
    try:
        p = db.get(Product, pid)
        assert p.tax_inclusive is False and p.tax_inclusive_price is False
    finally:
        db.close()


# ------------------------------------------------------------------------- invoices
def test_whole_order_invoice_lines_match_the_header(firm):
    o = _order(firm, [("p18", 1), ("p5", 1), ("p0", 1)], discount=250.0)
    r = client.post(f"/orders/{o['id']}/invoice", headers=firm["h"], params={"allow_upfront": True})
    if r.status_code == 400:   # the route may refuse an undelivered order; mark it delivered then bill it
        _deliver(o["id"], 1.0)
        r = client.post(f"/orders/{o['id']}/invoice", headers=firm["h"])
    assert r.status_code == 201, r.text
    inv = r.json()
    assert inv["tax"] == 207.0 and inv["total"] == 2457.0 and inv["discount"] == 250.0
    assert round(sum(i["tax"] for i in inv["items"]), 2) == inv["tax"]
    taxable = {i["product_name"]: i["taxable_value"] for i in inv["items"]}
    assert taxable == {"GST p18": 900.0, "GST p5": 900.0, "GST p0": 450.0}
    assert inv["taxable_total"] == 2250.0 and inv["tax_amount"] == inv["tax"]


def _deliver(order_id, fraction):
    db = SessionLocal()
    try:
        o = db.get(SalesOrder, order_id)
        for i in o.items:
            i.delivered_quantity = round(i.quantity * fraction, 3)
        o.fulfilment_status = "delivered"
        db.commit()
    finally:
        db.close()


def _delivery(f, order_id, qty):
    from app.models import Delivery, DeliveryItem
    db = SessionLocal()
    try:
        o = db.get(SalesOrder, order_id)
        item = o.items[0]
        d = Delivery(organization_id=f["org"], delivery_note_number=f"DN-{uuid.uuid4().hex[:6]}",
                     sales_order_id=order_id, customer_id=o.customer_id, status="delivered")
        d.items.append(DeliveryItem(product_id=item.product_id, product_name=item.product_name,
                                    order_item_id=item.id, planned_quantity=qty, delivered_quantity=qty))
        db.add(d)
        db.commit()
        return d.id
    finally:
        db.close()


def test_part_invoices_carry_their_share_and_add_up_to_the_order(firm):
    o = _order(firm, [("p18", 4)], discount=400.0)          # 4000 - 400 -> GST 648, total 4248
    invoices = []
    for qty in (1, 3):                                       # two deliveries: 1 unit, then 3
        r = client.post(f"/orders/{o['id']}/invoice", headers=firm["h"],
                        json={"delivery_id": _delivery(firm, o["id"], qty)})
        assert r.status_code == 201, r.text
        invoices.append(r.json())
    first, second = invoices
    assert first["discount"] == 100.0 and first["tax"] == 162.0 and first["total"] == 1062.0   # was: no discount
    assert second["discount"] == 300.0 and second["tax"] == 486.0 and second["total"] == 3186.0
    assert round(first["total"] + second["total"], 2) == 4248.0
    assert first["items"][0]["taxable_value"] == 900.0


def test_quick_bill_splits_the_bill_discount(firm):
    r = client.post("/invoices", headers=firm["h"], json={
        "customer_id": firm["local"], "warehouse_id": firm["wh"], "discount": 250.0,
        "items": [{"product_id": firm["p"]["p18"], "quantity": 1}, {"product_id": firm["p"]["p5"], "quantity": 1},
                  {"product_id": firm["p"]["p0"], "quantity": 1}]})
    assert r.status_code == 201, r.text
    inv = r.json()
    assert inv["tax"] == 207.0 and inv["total"] == 2457.0
    assert sorted(i["tax"] for i in inv["items"]) == [0.0, 45.0, 162.0]


# ---------------------------------------------------------------- 3. invoice GST fields
def _bill(f, customer):
    r = client.post("/invoices", headers=f["h"], json={
        "customer_id": customer, "warehouse_id": f["wh"],
        "items": [{"product_id": f["p"]["p18"], "quantity": 1}]})
    assert r.status_code == 201, r.text
    return r.json()


def test_cgst_sgst_for_intra_state_igst_for_inter_state(firm):
    intra = _bill(firm, firm["local"])
    item = intra["items"][0]
    assert intra["supply_type"] == "intra_state" and intra["place_of_supply"] == "maharashtra"
    assert (item["cgst_rate"], item["cgst_amount"], item["sgst_rate"], item["sgst_amount"]) == (9.0, 90.0, 9.0, 90.0)
    assert item["igst_amount"] == 0.0 and item["hsn_code"] == "HSNp18" and item["tax_amount"] == 180.0
    assert intra["customer_gstin"] == "27BBBBB1111B1Z5" and intra["seller_gstin"] == "27AAAAA0000A1Z5"
    assert intra["cgst_total"] == 90.0 and intra["sgst_total"] == 90.0 and intra["igst_total"] == 0.0

    inter = _bill(firm, firm["other_state"])
    item = inter["items"][0]
    assert inter["supply_type"] == "inter_state" and inter["place_of_supply"] == "karnataka"
    assert (item["igst_rate"], item["igst_amount"], item["cgst_amount"]) == (18.0, 180.0, 0.0)

    unknown = _bill(firm, firm["unknown"])
    assert unknown["supply_type"] is None and unknown["items"][0]["cgst_amount"] is None
    assert unknown["cgst_total"] is None and unknown["items"][0]["taxable_value"] == 1000.0

    got = client.get(f"/invoices/{intra['id']}", headers=firm["h"]).json()
    assert got["supply_type"] == "intra_state" and got["items"][0]["taxable_value"] == 1000.0


# ---------------------------------------------------------------------- 4. order_status
def test_order_status_label_follows_confirmation(firm):
    draft = _order(firm, [("p18", 1)], confirm=False)
    assert _db_order(draft["id"])["order_status"] == "Draft"
    client.post(f"/orders/{draft['id']}/confirm", headers=firm["h"])
    assert _db_order(draft["id"])["order_status"] == "Confirmed"


# ------------------------------------------------- 5. no double billing (QA 17 Nov, item 1)
def test_delivery_invoice_refused_when_order_is_invoiced_as_a_whole(firm):
    o = _order(firm, [("p18", 4)])
    r = client.post(f"/orders/{o['id']}/invoice", headers=firm["h"], params={"allow_upfront": True})
    assert r.status_code == 201, r.text                     # the whole order, no delivery_id
    r = client.post(f"/orders/{o['id']}/invoice", headers=firm["h"],
                    json={"delivery_id": _delivery(firm, o["id"], 1)})
    assert r.status_code == 409, r.text
    assert "the same goods twice" in r.json()["detail"]
    db = SessionLocal()
    try:
        from app.models import Invoice
        assert db.query(Invoice).filter(Invoice.order_id == o["id"]).count() == 1   # nothing billed twice
    finally:
        db.close()


def test_delivery_invoices_still_work_without_a_whole_order_invoice(firm):
    o = _order(firm, [("p18", 2)])
    r1 = client.post(f"/orders/{o['id']}/invoice", headers=firm["h"], json={"delivery_id": _delivery(firm, o["id"], 1)})
    r2 = client.post(f"/orders/{o['id']}/invoice", headers=firm["h"], json={"delivery_id": _delivery(firm, o["id"], 1)})
    assert r1.status_code == 201 and r2.status_code == 201, (r1.text, r2.text)


# ------------------------------------------------------------- 6. invoice PDF (GST layout)
GST_COLS = ["product", "hsn_sac", "quantity", "rate", "taxable_value", "tax_rate", "tax_amount", "amount"]


def _pdf_texts(monkeypatch, f, invoice_id, fmt="detailed"):
    from app.core import invoice_regular_pdf as reg
    seen = []
    original = reg._Doc.text

    def spy(self, x, y, width, text, size_px, **kw):
        seen.append(str(text or ""))
        return original(self, x, y, width, text, size_px, **kw)

    monkeypatch.setattr(reg._Doc, "text", spy)
    r = client.get(f"/invoices/{invoice_id}/pdf", headers=f["h"], params={"format": fmt})
    assert r.status_code == 200 and r.content[:4] == b"%PDF", r.text[:300]
    return seen


def _value_after(texts, label):
    return texts[texts.index(label) + 1]


def test_more_than_five_item_columns_can_be_saved(firm):
    r = client.patch("/invoice-settings", headers=firm["h"], json={"item_table": {"columns": GST_COLS}})
    assert r.status_code == 200, r.text
    assert r.json()["item_table"]["columns"] == GST_COLS


def test_detailed_pdf_prints_gst_columns_and_cgst_sgst(firm, monkeypatch):
    client.patch("/invoice-settings", headers=firm["h"], json={"item_table": {"columns": GST_COLS}})
    o = _order(firm, [("p18", 2)], discount=200.0)          # taxable 1800, GST 324 -> 162 + 162
    inv = client.post(f"/orders/{o['id']}/invoice", headers=firm["h"], params={"allow_upfront": True}).json()
    t = _pdf_texts(monkeypatch, firm, inv["id"])
    for header in ("TAXABLE VALUE", "GST %", "GST AMOUNT"):
        assert header in t, (header, t)
    assert "TAX)" in t or "AMOUNT (BEFORE TAX)" in t, t      # wrapped, not cut off
    assert not any("…" in x for x in t), t
    assert "Place of supply: Maharashtra" in t
    assert _value_after(t, "Discount").endswith("200.00")
    assert _value_after(t, "Taxable Value").endswith("1,800.00")
    assert _value_after(t, "CGST").endswith("162.00") and _value_after(t, "SGST").endswith("162.00")
    assert "IGST" not in t and "Total GST" not in t and "Tax" not in t
    assert _value_after(t, "Grand Total").endswith("2,124.00")


def test_detailed_pdf_igst_unknown_state_and_zero_gst(firm, monkeypatch):
    t = _pdf_texts(monkeypatch, firm, _bill(firm, firm["other_state"])["id"])
    assert _value_after(t, "IGST").endswith("180.00") and "CGST" not in t
    assert "Place of supply: Karnataka" in t
    t = _pdf_texts(monkeypatch, firm, _bill(firm, firm["unknown"])["id"])
    assert _value_after(t, "Total GST").endswith("180.00") and "CGST" not in t and "IGST" not in t
    r = client.post("/invoices", headers=firm["h"], json={
        "customer_id": firm["local"], "warehouse_id": firm["wh"],
        "items": [{"product_id": firm["p"]["p0"], "quantity": 1}]})
    t = _pdf_texts(monkeypatch, firm, r.json()["id"])
    assert _value_after(t, "CGST").endswith("0.00") and _value_after(t, "SGST").endswith("0.00")  # printed at 0


def test_simple_pdf_explains_its_total_without_gst_columns(firm, monkeypatch):
    client.patch("/invoice-settings", headers=firm["h"], json={"item_table": {"columns": GST_COLS}})
    o = _order(firm, [("p18", 2)], discount=200.0)
    inv = client.post(f"/orders/{o['id']}/invoice", headers=firm["h"], params={"allow_upfront": True}).json()
    t = _pdf_texts(monkeypatch, firm, inv["id"], fmt="simple")
    assert "GST %" not in t and "GST AMOUNT" not in t and "TAXABLE VALUE" not in t
    assert _value_after(t, "Discount").endswith("200.00")
    assert _value_after(t, "Total GST").endswith("324.00")
    assert _value_after(t, "Grand Total").endswith("2,124.00")


# ------------------------------------------- 7. GST report agrees with the saved invoice tax
def test_gst_report_taxable_value_is_after_the_invoice_discount(firm):
    o = _order(firm, [("p18", 2)], discount=200.0)                  # taxable 1800, GST 324
    inv = client.post(f"/orders/{o['id']}/invoice", headers=firm["h"], params={"allow_upfront": True}).json()
    r = client.get("/reports/gst-summary", headers=firm["h"], params={"group_by": "transaction", "page_size": 100})
    assert r.status_code == 200, r.text
    row = next(x for x in r.json()["rows"] if x["document_number"] == inv["invoice_number"])
    assert (row["taxable_value"], row["tax_amount"], row["total_amount"]) == (1800.0, 324.0, 2124.0)
    assert row["taxable_value"] == inv["taxable_total"] and row["tax_amount"] == inv["tax"]
    s = client.get("/reports/gst-summary", headers=firm["h"]).json()["rows"][0]
    assert (s["taxable_value"], s["tax_amount"]) == (1800.0, 324.0)
