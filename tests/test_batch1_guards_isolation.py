"""Batch 1: permission guards (A) and cross-firm isolation (D). QA-TEST data, two firms."""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Customer, Delivery, Invoice, Organization, Role, SalesOrder, StoredFile, User  # noqa: E402
from app.models.enums import UserRole  # noqa: E402
from app.services import entitlement_service, role_service  # noqa: E402

c = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def _schema():
    Base.metadata.create_all(engine)


@pytest.fixture(autouse=True)
def _no_plan_gates(monkeypatch):
    monkeypatch.setattr(entitlement_service, "check_entitlement", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "check_limit", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "has_entitlement", lambda *a, **k: True)


def _firm():
    tag = uuid.uuid4().hex[:5]
    with SessionLocal() as db:
        org = Organization(name=f"QA-TEST {tag}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}",
                           currency="INR", timezone="Asia/Kolkata")
        db.add(org)
        db.flush()
        role_service.seed_default_roles(db, org.id)
        db.flush()
        roles = {r.name: r for r in db.query(Role).filter(Role.organization_id == org.id)}
        zero = Role(organization_id=org.id, name=f"Nothing {tag}", permissions={})
        db.add(zero)
        db.flush()
        h, ids = {}, {}
        for key, sr, legacy, rid in (
            ("admin", "admin", UserRole.ADMIN, None),
            ("so", "staff", UserRole.SALES_OFFICER, roles["Sales Officer"].id),
            ("rider", "staff", UserRole.DELIVERY_PARTNER, roles["Delivery Partner"].id),
            ("rider2", "staff", UserRole.DELIVERY_PARTNER, roles["Delivery Partner"].id),
            ("legacy", "staff", UserRole.DELIVERY_PARTNER, None),
            ("zero", "staff", UserRole.SALES_OFFICER, zero.id),
        ):
            u = User(email=f"qa.{key}.{uuid.uuid4().hex[:8]}@example.com", name=f"QA-TEST {key} {tag}",
                     organization_id=org.id, system_role=sr, role=legacy, role_id=rid,
                     password_hash=hash_password("Password123!"), is_active=True)
            db.add(u)
            db.flush()
            h[key] = {"Authorization": f"Bearer {create_access_token(u.id, sr, org.id)}"}
            ids[key] = u.id
        cu = Customer(organization_id=org.id, name=f"QA-TEST Cust {tag}", opening_balance=50000.0,
                      outstanding_balance=50000.0)
        db.add(cu)
        db.flush()
        so = SalesOrder(organization_id=org.id, customer_id=cu.id, order_number=f"SO-{tag}", total=10000.0,
                        status="confirmed")
        db.add(so)
        db.flush()
        inv = Invoice(organization_id=org.id, customer_id=cu.id, invoice_number=f"QA-INV-{tag}", total=5000.0,
                      amount_paid=0.0, status="unpaid", payment_status="Unpaid")
        db.add(inv)
        dl = {}
        for key, partner, st in (("dlv", "rider", "delivered"), ("plan1", "rider", "planned"),
                                 ("plan2", "rider", "planned"), ("plan3", "legacy", "planned")):
            d = Delivery(organization_id=org.id, customer_id=cu.id, sales_order_id=so.id,
                         delivery_note_number=f"DO-{key}-{tag}", delivery_partner_id=ids[partner], status=st)
            db.add(d)
            db.flush()
            dl[key] = d.id
        db.commit()
        return {"org": org.id, "h": h, "ids": ids, "cust": cu.id, "inv": inv.id, "so": so.id, **dl}


@pytest.fixture(scope="module")
def firms():
    return _firm(), _firm()


def _collect(f, who, dlv="dlv", amount=10.0):
    return c.post(f"/deliveries/{f[dlv]}/collections", headers=f["h"][who],
                  json={"amount": amount, "payment_mode": "cash"})


# ------------------------------------------------------------------ A1 delivery collections
def test_a1_delivery_collection_guard(firms):
    a, b = firms
    assert _collect(a, "zero").status_code == 403
    assert _collect(a, "so").status_code == 403              # no deliveries/payments create
    assert _collect(a, "rider").status_code == 201
    r = _collect(a, "rider2")                                 # someone else's delivery
    assert r.status_code == 403 and "not assigned to you" in r.text
    assert _collect(a, "admin").status_code == 201
    assert c.post(f"/deliveries/{a['dlv']}/collections", headers=b["h"]["admin"],
                  json={"amount": 10.0, "payment_mode": "cash"}).status_code == 404


# ------------------------------------------------------------------ A2 accept / reject
def test_a2_accept_reject_guard(firms):
    a, b = firms
    z = c.post(f"/deliveries/{a['plan1']}/accept", headers=a["h"]["zero"])
    assert z.status_code == 403 and z.json()["detail"] == "You don't have permission to edit deliveries"
    assert c.post(f"/deliveries/{a['plan1']}/reject", headers=a["h"]["zero"], json={}).status_code == 403
    assert c.post(f"/deliveries/{a['plan1']}/accept", headers=a["h"]["so"]).status_code == 403
    assert c.post(f"/deliveries/{a['plan1']}/accept", headers=b["h"]["admin"]).status_code == 404
    assert c.post(f"/deliveries/{a['plan1']}/accept", headers=b["h"]["rider"]).status_code == 404
    # Admin: unchanged (only the assigned partner accepts) -> 403 from the service, as before.
    assert c.post(f"/deliveries/{a['plan1']}/accept", headers=a["h"]["admin"]).status_code == 403
    assert c.post(f"/deliveries/{a['plan1']}/accept", headers=a["h"]["rider2"]).status_code in (403, 404)
    ok = c.post(f"/deliveries/{a['plan1']}/accept", headers=a["h"]["rider"])
    assert ok.status_code == 200 and ok.json()["id"] == a["plan1"]
    rej = c.post(f"/deliveries/{a['plan2']}/reject", headers=a["h"]["rider"], json={"reason": "QA-TEST"})
    assert rej.status_code == 200
    # Legacy partner account (no role row) keeps working.
    assert c.post(f"/deliveries/{a['plan3']}/accept", headers=a["h"]["legacy"]).status_code == 200


# ------------------------------------------------------------------ A3 delivery notes
def test_a3_delivery_notes_guard(firms):
    a, b = firms
    body = {"customer_id": a["cust"], "sales_order_id": a["so"], "items": []}
    assert c.post("/deliveries/notes", headers=a["h"]["zero"], json=body).status_code == 403
    assert c.post("/deliveries/notes", headers=a["h"]["so"], json=body).status_code == 403
    assert c.post("/deliveries/notes", headers=a["h"]["rider"], json=body).status_code == 201
    assert c.post("/deliveries/notes", headers=a["h"]["admin"], json=body).status_code == 201
    assert c.post("/deliveries/notes", headers=b["h"]["admin"], json=body).status_code == 400  # other firm's ids


# ------------------------------------------------------------------ A4/A5 files
def _upload(f, who):
    r = c.post("/files/upload", headers=f["h"][who], files={"file": ("qa-test.txt", b"QA-TEST", "text/plain")})
    assert r.status_code == 201, r.text
    return r.json()["file_id"]


def test_a5_file_delete_guard(firms):
    a, b = firms
    fid = _upload(a, "rider")                                # upload unchanged (any signed-in user)
    assert c.delete(f"/files/{fid}", headers=a["h"]["rider2"]).status_code == 403
    assert c.delete(f"/files/{fid}", headers=a["h"]["zero"]).status_code == 403
    assert c.delete(f"/files/{fid}", headers=b["h"]["admin"]).status_code == 404
    assert c.delete(f"/files/{fid}", headers=a["h"]["rider"]).status_code == 204   # uploader
    fid2 = _upload(a, "rider")
    assert c.delete(f"/files/{fid2}", headers=a["h"]["admin"]).status_code == 204  # admin of the firm
    with SessionLocal() as db:                               # no recorded uploader: admin only
        sf = StoredFile(organization_id=a["org"], filename="old.txt", content_type="text/plain", size=1, data=b"x")
        db.add(sf)
        db.commit()
        old = sf.id
    assert c.delete(f"/files/{old}", headers=a["h"]["rider"]).status_code == 403
    assert c.delete(f"/files/{old}", headers=a["h"]["admin"]).status_code == 204
    assert c.delete(f"/files/{uuid.uuid4()}", headers=a["h"]["admin"]).status_code == 404


# ------------------------------------------------------------------ D cross-firm isolation
def test_d_cross_firm_isolation(firms):
    a, b = firms
    rb = c.post("/payment-receipts", headers=b["h"]["admin"],
                json={"invoice_reference_id": b["inv"], "amount_received": 100, "payment_method": "cash"})
    assert rb.status_code == 201, rb.text
    vb = c.post("/payment-receipts", headers=b["h"]["admin"],
                json={"invoice_reference_id": b["inv"], "amount_received": 50, "payment_method": "upi"}).json()["id"]
    assert c.delete(f"/payment-receipts/{vb}", headers=b["h"]["admin"]).status_code == 204  # B's voided archive row
    b_ids = {rb.json()["id"], vb}
    for iv in ("false", "true"):
        r = c.get("/payment-receipts", headers=a["h"]["admin"], params={"include_voided": iv, "page_size": 100})
        assert r.status_code == 200 and not b_ids & {x["id"] for x in r.json()["items"]}
        assert b["org"] not in r.text
        r = c.get("/payment-receipts", headers=a["h"]["admin"],
                  params={"include_voided": iv, "customer_id": b["cust"]})
        assert r.json()["total"] == 0
        assert c.get("/payment-receipts", headers=b["h"]["admin"],
                     params={"include_voided": iv}).json()["total"] >= 1
    for rid in b_ids:
        assert c.get(f"/payment-receipts/{rid}", headers=a["h"]["admin"]).status_code == 404
        assert c.patch(f"/payment-receipts/{rid}", headers=a["h"]["admin"], json={"note": "x"}).status_code == 404
        assert c.delete(f"/payment-receipts/{rid}", headers=a["h"]["admin"]).status_code == 404
    # Collections
    cb = _collect(b, "rider", amount=20.0)
    assert cb.status_code == 201
    cid = cb.json()["id"]
    for path in ("/deliveries/collections", "/deliveries/collections/all"):
        r = c.get(path, headers=a["h"]["admin"], params={"page_size": 100})
        assert r.status_code == 200 and cid not in r.text and b["org"] not in r.text
    assert c.get(f"/deliveries/collections/{cid}", headers=a["h"]["admin"]).status_code == 404
    assert c.get(f"/deliveries/{b['dlv']}/collections", headers=a["h"]["admin"]).status_code == 404
    assert c.post(f"/deliveries/collections/{cid}/void", headers=a["h"]["admin"], json={}).status_code == 404
    # Files: B's file cannot be deleted from A.
    fb = _upload(b, "admin")
    assert c.delete(f"/files/{fb}", headers=a["h"]["admin"]).status_code == 404
    assert c.delete(f"/files/{fb}", headers=a["h"]["rider"]).status_code == 404
