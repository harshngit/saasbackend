"""Invoice PDF: the vehicle number falls back to the invoice's delivery's vehicle.

Invoice.delivery is loaded on access (not joined) since 94548e0. Its only readers are the
three invoice PDF renderers, all reached through GET /invoices/{id}/pdf:
  - regular A4/A5 themed layout (invoice_regular_pdf._meta_lines)   -- simple and detailed
  - thermal simple   (pdf_docs.invoice_simple_pdf)
  - thermal detailed (pdf_docs.invoice_detailed_pdf)
This goes through the real route (request session, lazy load) for all of them.
"""

import os
import re
import sys
import uuid
import zlib

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402

from app.core import workflow  # noqa: E402
from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Delivery, Invoice, Organization, Plan  # noqa: E402
from app.seed import seed_plans  # noqa: E402
from app.services import entitlement_service  # noqa: E402

c = TestClient(app)
VEHICLE = f"MH12AB{uuid.uuid4().int % 9000 + 1000}"


@pytest.fixture(autouse=True)
def _no_plan_gates(monkeypatch):
    monkeypatch.setattr(entitlement_service, "check_entitlement", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "check_limit", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "has_entitlement", lambda *a, **k: True)


def _pdf_text(pdf: bytes) -> str:
    parts = []
    for m in re.finditer(rb"stream[\r\n]+(.*?)[\r\n]+endstream", pdf, re.DOTALL):
        try:
            parts.append(zlib.decompress(m.group(1)).decode("latin1", errors="replace"))
        except Exception:  # noqa: BLE001 -- an uncompressed stream
            parts.append(m.group(1).decode("latin1", errors="replace"))
    return " ".join(parts)


def _shows(pdf: bytes, text: str) -> bool:
    """fpdf writes text either plainly or as hex glyph runs; accept both."""
    body = _pdf_text(pdf)
    return text in body or text.encode("latin1").hex().upper() in body.upper().replace(" ", "")


@pytest.fixture(scope="module")
def invoice():
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as s:
        seed_plans(s)
        s.commit()
    r = c.post("/auth/register", json={"organization_name": f"PDF {uuid.uuid4().hex[:6]}", "admin_name": "A",
               "email": f"pdf_{uuid.uuid4().hex[:10]}@example.com", "password": "Password123!", "role": "admin"})
    auth = {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}
    org_id = c.get("/auth/me", headers=auth).json()["organization_id"]
    with SessionLocal() as s:
        s.query(Organization).filter(Organization.id == org_id).update(
            {"plan_id": s.query(Plan).filter(Plan.name == "Enterprise").one().id})
        s.commit()
    assert c.patch("/invoice-settings", json={"invoice_details": {"show_vehicle_number": True}},
                   headers=auth).status_code == 200
    wh = c.post("/warehouses", json={"name": "Main", "code": f"WH-{uuid.uuid4().hex[:6]}", "is_default": True},
                headers=auth).json()["id"]
    cust = c.post("/customers", json={"name": "Shop", "phone": "9876500000", "delivery_address": "x",
                                      "billing_address": "x"}, headers=auth).json()["id"]
    pid = c.post("/products", json={"name": "Item", "sku": f"PV-{uuid.uuid4().hex[:6]}", "price": 100.0,
                 "tax_rate": 18.0, "uom": "unit",
                 "pricing": {"purchase_price": 50.0, "selling_price": 100.0, "currency": "INR"}}, headers=auth).json()["id"]
    c.post(f"/warehouses/{wh}/stock/adjust", json={"product_id": pid, "quantity": 100}, headers=auth)
    veh = c.post("/vehicles", json={"vehicle_number": VEHICLE, "vehicle_type": "Van"}, headers=auth).json()["id"]
    order = c.post("/orders", json={"customer_id": cust, "warehouse_id": wh, "delivery_method": "home_delivery",
                   "payment_status": "pending", "items": [{"product_id": pid, "quantity": 1}]}, headers=auth).json()
    with SessionLocal() as s:   # an invoice made for a delivery, with no vehicle of its own
        s.query(Delivery).filter(Delivery.id == order["delivery_id"]).update({"vehicle_id": veh})
        s.query(Invoice).filter(Invoice.id == order["invoice_id"]).update(
            {"delivery_id": order["delivery_id"], "vehicle_number": None})
        s.commit()
    return {"id": order["invoice_id"], "auth": auth}


@pytest.mark.parametrize("fmt", ["simple", "detailed"])
@pytest.mark.parametrize("layout", ["regular", "thermal"])
def test_pdf_shows_the_delivery_vehicle(invoice, fmt, layout):
    params = {"format": fmt}
    if layout == "thermal":
        params["thermal_layout"] = workflow.THERMAL_LAYOUTS[0]
    r = c.get(f"/invoices/{invoice['id']}/pdf", params=params, headers=invoice["auth"])
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf", r.text[:200]
    assert _shows(r.content, VEHICLE), f"{VEHICLE} missing from the {layout} {fmt} PDF"
