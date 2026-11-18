"""Product rules (QA, 18 Nov): unique SKU per firm, non-negative pricing with
discount <= 100, mandatory Primary Product Image on create, active-only filters."""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Organization, Product, StoredFile, User  # noqa: E402
from app.models.enums import UserRole  # noqa: E402
from app.services import entitlement_service, role_service  # noqa: E402

REQUIRE_COVER_IMAGE = True
client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def _schema():
    Base.metadata.create_all(engine)


@pytest.fixture(autouse=True)
def _no_plan_gates(monkeypatch):
    monkeypatch.setattr(entitlement_service, "check_entitlement", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "check_limit", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "has_entitlement", lambda *a, **k: True)


def _firm(db):
    org = Organization(name=f"Prod {uuid.uuid4().hex[:6]}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}",
                       currency="INR")
    db.add(org)
    db.flush()
    role_service.seed_default_roles(db, org.id)
    u = User(email=f"admin.{uuid.uuid4().hex[:8]}@example.com", name="admin", organization_id=org.id,
             system_role="admin", role=UserRole.ADMIN, password_hash=hash_password("Password123!"), is_active=True)
    db.add(u)
    db.flush()
    return org.id, {"Authorization": f"Bearer {create_access_token(u.id, 'admin', org.id)}"}


def _file(db, org_id, content_type="image/png"):
    f = StoredFile(organization_id=org_id, filename="p.png", content_type=content_type, size=3, data=b"abc")
    db.add(f)
    db.flush()
    return f"/files/{f.id}"


@pytest.fixture
def firm():
    db = SessionLocal()
    try:
        org_id, h = _firm(db)
        other_org, _ = _firm(db)
        out = {"org": org_id, "h": h, "img": _file(db, org_id), "pdf": _file(db, org_id, "application/pdf"),
               "foreign": _file(db, other_org)}
        db.commit()
        return out
    finally:
        db.close()


def _create(f, **extra):
    body = {"name": "Cable", "cover_image": f["img"], "pricing": {"selling_price": 10}}
    body.update(extra)
    return client.post("/products", headers=f["h"], json=body)


def test_duplicate_sku_is_refused_on_create_and_update(firm):
    a = _create(firm, sku="SKU-1")
    assert a.status_code == 201, a.text
    r = _create(firm, sku=" sku-1 ")                       # trimmed, case-insensitive
    assert r.status_code == 409 and r.json() == {"detail": "A product with this SKU already exists."}
    b = _create(firm, sku="SKU-2")
    assert b.status_code == 201
    assert _create(firm, sku=None).status_code == 201      # no SKU, same name: fine
    assert _create(firm, sku="").status_code == 201        # blank = no SKU
    assert _create(firm, sku="").status_code == 201

    a_id, b_id = a.json()["id"], b.json()["id"]
    r = client.patch(f"/products/{a_id}", headers=firm["h"], json={"sku": "SKU-1", "name": "Cable X"})
    assert r.status_code == 200, r.text                     # keeps its own SKU
    r = client.patch(f"/products/{a_id}", headers=firm["h"], json={"sku": "sku-2"})
    assert r.status_code == 409 and r.json()["detail"] == "A product with this SKU already exists."
    r = client.patch(f"/products/{b_id}", headers=firm["h"], json={"sku": "SKU-3"})
    assert r.status_code == 200 and r.json()["sku"] == "SKU-3"


def test_sku_is_per_firm(firm):
    assert _create(firm, sku="SHARED").status_code == 201
    db = SessionLocal()
    try:
        org2, h2 = _firm(db)
        img2 = _file(db, org2)
        db.commit()
    finally:
        db.close()
    r = client.post("/products", headers=h2, json={"name": "X", "sku": "SHARED", "cover_image": img2})
    assert r.status_code == 201, r.text


@pytest.mark.parametrize("field,value", [
    ("purchase_price", -1), ("selling_price", -0.01), ("mrp", -5), ("wholesale_price", -1),
    ("dealer_price", -1), ("discount_percent", -1), ("discount_percent", 100.5),
])
def test_invalid_pricing_is_refused_on_create_and_update(firm, field, value):
    r = _create(firm, pricing={field: value})
    assert r.status_code == 422, r.text
    assert field in str(r.json()["detail"])
    pid = _create(firm).json()["id"]
    r = client.patch(f"/products/{pid}", headers=firm["h"], json={"pricing": {field: value}})
    assert r.status_code == 422, r.text


def test_decimal_pricing_is_kept(firm):
    pricing = {"purchase_price": 5.75, "selling_price": 118.50, "mrp": 120.25, "wholesale_price": 10.25,
               "dealer_price": 9.5, "discount_percent": 100}
    r = _create(firm, pricing=pricing)
    assert r.status_code == 201, r.text
    got = r.json()["pricing"]
    assert all(got[k] == v for k, v in pricing.items()), got
    r = client.patch(f"/products/{r.json()['id']}", headers=firm["h"],
                     json={"pricing": {"mrp": 0, "discount_percent": 0}})
    assert r.status_code == 200 and r.json()["pricing"]["mrp"] == 0


def test_primary_image_is_required_on_create(firm):
    for missing in ({"cover_image": None}, {"cover_image": "  "}, {}):
        r = client.post("/products", headers=firm["h"], json={"name": "No image", **missing})
        assert r.status_code == 400 and r.json() == {"detail": "Primary product image is required."}
    r = _create(firm, cover_image=firm["pdf"])
    assert r.status_code == 400 and "must be an image" in r.json()["detail"]
    r = _create(firm, cover_image=firm["foreign"])          # another firm's upload
    assert r.status_code == 400 and "not found" in r.json()["detail"]
    assert _create(firm, cover_image=f"/files/{uuid.uuid4()}").status_code == 400
    assert _create(firm, cover_image="not-a-url").status_code == 400
    assert _create(firm, cover_image="https://cdn.example.com/p.png").status_code == 201


def test_old_product_without_image_can_still_be_edited(firm):
    db = SessionLocal()
    try:
        old = Product(organization_id=firm["org"], name="Legacy", price=1)
        db.add(old)
        db.commit()
        pid = old.id
    finally:
        db.close()
    r = client.patch(f"/products/{pid}", headers=firm["h"], json={"name": "Legacy 2", "pricing": {"selling_price": 3}})
    assert r.status_code == 200, r.text
    assert r.json()["cover_image"] is None


def test_active_filters_for_pickers(firm):
    act = _create(firm, name="Active one").json()["id"]
    ina = _create(firm, name="Inactive one").json()["id"]
    client.patch(f"/products/{ina}", headers=firm["h"], json={"status": "inactive"})
    for url in ("/products", "/inventory", "/warehouses/stock"):
        for params in ({"is_active": "true"}, {"status": "active"}):
            r = client.get(url, headers=firm["h"], params={**params, "page_size": 100})
            assert r.status_code == 200, (url, r.text)
            key = "product_id" if url == "/warehouses/stock" else "id"
            ids = {row[key] for row in r.json()["items"]}
            assert act in ids and ina not in ids, (url, params)
    r = client.patch(f"/products/{ina}", headers=firm["h"], json={"is_active": True, "status": "inactive"})
    assert r.json()["is_active"] is False                    # the two never disagree


def test_inventory_search_covers_every_page(firm):
    for i in range(12):
        _create(firm, name=f"Bulk {i:02d}")
    _create(firm, name="Zebra rope")
    r = client.get("/inventory", headers=firm["h"], params={"page_size": 5})
    assert r.json()["total"] == 13 and len(r.json()["items"]) == 5
    r = client.get("/inventory", headers=firm["h"], params={"search": "zebra", "page_size": 5})
    assert r.json()["total"] == 1 and r.json()["items"][0]["name"] == "Zebra rope"
