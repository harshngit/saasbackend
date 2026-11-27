"""Create Order -> Home Delivery: the Delivery Partner picker (GET /deliveries/partners) by role.

Sales Officer loads it (Sales Orders > Create) without the admin staff directory (GET /users);
it lists only active delivery partners of the caller's own firm, with minimal fields; a
zero-permission user gets 403. Two firms.
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
    Customer, Organization, Product, ProductPricing, Role, SalesOrder, User, Warehouse, WarehouseStock,
)
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
        org = Organization(name=f"DP {tag}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}",
                           currency="INR", timezone="Asia/Kolkata", state="Maharashtra")
        db.add(org)
        db.flush()
        role_service.seed_default_roles(db, org.id)
        db.flush()
        roles = {r.name: r for r in db.query(Role).filter(Role.organization_id == org.id)}
        zero = Role(organization_id=org.id, name=f"Nothing {tag}", permissions={})
        db.add(zero)
        db.flush()
        h, ids = {}, {}
        for key, name, sr, legacy, rid, active in (
            ("admin", f"Admin {tag}", "admin", UserRole.ADMIN, None, True),
            ("acct", f"Accountant {tag}", "staff", UserRole.ACCOUNTANT, roles["Accountant"].id, True),
            ("so", f"Sales Officer {tag}", "staff", UserRole.SALES_OFFICER, roles["Sales Officer"].id, True),
            ("rider", f"Rider A {tag}", "staff", UserRole.DELIVERY_PARTNER, roles["Delivery Partner"].id, True),
            ("rider2", f"Rider B {tag}", "staff", UserRole.DELIVERY_PARTNER, roles["Delivery Partner"].id, True),
            ("gone", f"Rider Inactive {tag}", "staff", UserRole.DELIVERY_PARTNER, roles["Delivery Partner"].id, False),
            ("zero", f"Zero Perm {tag}", "staff", UserRole.SALES_OFFICER, zero.id, True),
        ):
            u = User(email=f"{key}.{uuid.uuid4().hex[:8]}@example.com", name=name, organization_id=org.id,
                     system_role=sr, role=legacy, role_id=rid, password_hash=hash_password("Password123!"),
                     is_active=active, phone="9876543210")
            db.add(u)
            db.flush()
            h[key] = {"Authorization": f"Bearer {create_access_token(u.id, sr, org.id)}"}
            ids[key] = u.id
        wh = Warehouse(organization_id=org.id, name="Main", code=f"W{tag}", is_default=True)
        cust = Customer(organization_id=org.id, name=f"Shop {tag}", state="Maharashtra", delivery_address="Addr 1")
        db.add_all([wh, cust])
        db.flush()
        p = Product(organization_id=org.id, name="Item", sku=f"I-{tag}", price=100, tax_rate=18, hsn_code="8544",
                    total_inventory=0, is_active=True)
        p.pricing = ProductPricing(selling_price=100, purchase_price=50, tax_inclusive=False, currency="INR")
        db.add(p)
        db.flush()
        db.add(WarehouseStock(organization_id=org.id, warehouse_id=wh.id, product_id=p.id, on_hand_quantity=100))
        db.commit()
        return {"org": org.id, "h": h, "ids": ids, "wh": wh.id, "cust": cust.id, "prod": p.id}


def _order(f, who, method, partner=None):
    body = {"customer_id": f["cust"], "warehouse_id": f["wh"], "delivery_method": method,
            "payment_status": "pending", "items": [{"product_id": f["prod"], "quantity": 1}]}
    if partner:
        body["delivery_partner_id"] = partner
    return c.post("/orders", headers=f["h"][who], json=body)


def test_sales_officer_loads_picker_selects_and_creates_home_delivery_order():
    f, other = _firm(), _firm()
    r = c.get("/deliveries/partners", headers=f["h"]["so"])
    assert r.status_code == 200, r.text
    rows = r.json()
    assert {x["id"] for x in rows} == {f["ids"]["rider"], f["ids"]["rider2"]}  # active DPs of this firm only
    assert all(set(x) == {"id", "name", "is_active", "role"} for x in rows)     # no email / phone / profile
    assert all(x["is_active"] is True and x["role"] == "delivery_partner" for x in rows)
    assert [x["name"] for x in rows] == sorted(x["name"] for x in rows)
    assert not {x["id"] for x in rows} & set(other["ids"].values())
    # Still no staff directory for a Sales Officer.
    assert c.get("/users", headers=f["h"]["so"]).status_code == 403

    o = _order(f, "so", "home_delivery", rows[0]["id"])
    assert o.status_code == 201, o.text
    with SessionLocal() as db:
        assert db.get(SalesOrder, o.json()["id"]).assigned_delivery_partner_id == rows[0]["id"]
    # Another firm's partner is refused.
    bad = _order(f, "so", "home_delivery", other["ids"]["rider"])
    assert bad.status_code == 400 and "not a user in your firm" in bad.text
    # A non-partner (Accountant) is refused.
    assert _order(f, "so", "home_delivery", f["ids"]["acct"]).status_code == 400


def test_takeaway_needs_no_partner():
    f = _firm()
    o = _order(f, "so", "takeaway")
    assert o.status_code == 201, o.text
    assert o.json().get("delivery_method") == "takeaway"


def test_admin_and_delivery_partner_and_zero_permission():
    f = _firm()
    a = c.get("/deliveries/partners", headers=f["h"]["admin"])
    assert a.status_code == 200 and len(a.json()) == 2
    # Delivery Partner: already has Sales Orders > Create by default; the picker gives nothing more.
    d = c.get("/deliveries/partners", headers=f["h"]["rider"])
    assert d.status_code == 200 and len(d.json()) == 2
    assert c.get("/users", headers=f["h"]["rider"]).status_code == 403
    # Zero permission: no picker, no directory.
    z = c.get("/deliveries/partners", headers=f["h"]["zero"])
    assert z.status_code == 403 and z.json()["detail"] == "You don't have permission to load delivery partners"
    assert c.get("/users", headers=f["h"]["zero"]).status_code == 403
    assert c.get("/users/assignable", headers=f["h"]["zero"]).status_code == 403
    # Accountant (no order / delivery create or edit) gets no picker either.
    assert c.get("/deliveries/partners", headers=f["h"]["acct"]).status_code == 403
    assert c.get("/deliveries/partners").status_code in (401, 403)  # no token


def test_custom_role_with_only_order_create_gets_the_picker():
    f = _firm()
    with SessionLocal() as db:
        role = Role(organization_id=f["org"], name=f"Counter {uuid.uuid4().hex[:4]}",
                    permissions={"sales_orders": {"create": True}})
        db.add(role)
        db.flush()
        db.get(User, f["ids"]["zero"]).role_id = role.id
        db.commit()
    assert c.get("/deliveries/partners", headers=f["h"]["zero"]).status_code == 200
