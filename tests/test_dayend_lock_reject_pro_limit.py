"""Day-End reject permission, the closed-day lock on vehicle stock, and the Pro user limit.

Permissions: Day-End reject needs deliveries.approve like approve (and, as before, the
service lets only an Admin review a Day-End). Closed day: once the partner's Day-End for
today is submitted / approved, vehicle loading (delivery load and plain load), extra load,
end-of-day return and delivery confirm are refused with 409 DAY_END_CLOSED before anything
is written; viewing still works. Plans: Basic 1, Pro 5, Enterprise unlimited, enforced.
Two firms.
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
    DeliveryItem, Organization, Plan, Role, StockMovement, User, VehicleLoading, VehicleLoadingItem, WarehouseStock,
)
from app.models.delivery_day_end import DeliveryDayEnd  # noqa: E402
from app.models.enums import UserRole  # noqa: E402
from app.seed import seed_plans  # noqa: E402
from app.services import day_end_service, entitlement_service  # noqa: E402

c = TestClient(app, raise_server_exceptions=False)


@pytest.fixture(scope="module", autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as s:
        seed_plans(s)
        s.commit()


def _org(plan="Enterprise"):
    r = c.post("/auth/register", json={"organization_name": f"DL {uuid.uuid4().hex[:6]}", "admin_name": "A",
               "email": f"dl_{uuid.uuid4().hex[:10]}@example.com", "password": "Password123!", "role": "admin"})
    assert r.status_code == 201, r.text
    auth = {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}
    org_id = c.get("/auth/me", headers=auth).json()["organization_id"]
    with SessionLocal() as s:
        s.query(Organization).filter(Organization.id == org_id).update(
            {"plan_id": s.query(Plan).filter(Plan.name == plan).one().id})
        s.commit()
    return org_id, auth


def _staff(auth, role="delivery_partner"):
    email = f"{role}_{uuid.uuid4().hex[:8]}@example.com"
    r = c.post("/users", json={"name": role, "email": email, "password": "Password123!", "role": role}, headers=auth)
    assert r.status_code == 201, r.text
    token = c.post("/auth/login", json={"email": email, "password": "Password123!"}).json()["tokens"]["access_token"]
    return r.json()["id"], {"Authorization": f"Bearer {token}"}


def _role_user(org_id, permissions):
    with SessionLocal() as s:
        role = Role(organization_id=org_id, name=f"R {uuid.uuid4().hex[:6]}", permissions=permissions)
        s.add(role)
        s.flush()
        u = User(email=f"r.{uuid.uuid4().hex[:8]}@example.com", name="R", organization_id=org_id, system_role="staff",
                 role=UserRole.ACCOUNTANT, role_id=role.id, password_hash=hash_password("Password123!"), is_active=True)
        s.add(u)
        s.commit()
        return {"Authorization": f"Bearer {create_access_token(u.id, 'staff', org_id)}"}


# ------------------------------------------------------------------ permissions
@pytest.fixture(scope="module")
def review():
    org_id, admin = _org()
    partner, _ = _staff(admin)
    other_org, other_admin = _org()
    view_only = _role_user(org_id, {"deliveries": {"view": True}})
    approver = _role_user(org_id, {"deliveries": {"view": True, "approve": True}})

    def day_end(days_back):
        with SessionLocal() as s:
            from datetime import timedelta
            d = DeliveryDayEnd(organization_id=org_id, delivery_partner_id=partner,
                               business_date=day_end_service.today(s, org_id) - timedelta(days=days_back),
                               status="submitted")
            s.add(d)
            s.commit()
            return d.id

    return {"admin": admin, "view_only": view_only, "approver": approver, "other_admin": other_admin,
            "day_end": day_end}


def test_reject_needs_deliveries_approve(review):
    de = review["day_end"](3)
    url = f"/delivery-day-ends/{de}/reject"
    body = {"reason": "Cash short"}
    r = c.post(url, json=body, headers=review["view_only"])
    assert r.status_code == 403 and "Deliveries > Approve" in r.json()["detail"]
    # deliveries.approve passes the new check; the service still lets only an Admin review.
    r = c.post(url, json=body, headers=review["approver"])
    assert r.status_code == 403 and r.json()["detail"] == "Only an Admin can review a Day-End"
    assert c.post(url, json=body, headers=review["other_admin"]).status_code == 404
    r = c.post(url, json=body, headers=review["admin"])
    assert r.status_code == 200 and r.json()["status"] == "rejected"


def test_approve_still_works(review):
    de = review["day_end"](4)
    url = f"/delivery-day-ends/{de}/approve"
    assert c.post(url, json={}, headers=review["view_only"]).status_code == 403
    r = c.post(url, json={}, headers=review["admin"])
    assert r.status_code == 200 and r.json()["status"] == "approved"


# ------------------------------------------------------------------- closed day
def _new_day():
    org_id, admin = _org()
    wh = c.post("/warehouses", json={"name": "Main", "code": f"WH-{uuid.uuid4().hex[:6]}", "is_default": True},
                headers=admin).json()["id"]
    cust = c.post("/customers", json={"name": "Shop", "phone": "9876500000", "delivery_address": "x",
                                      "billing_address": "x"}, headers=admin).json()["id"]
    pid = c.post("/products", json={"name": "Item", "sku": f"DL-{uuid.uuid4().hex[:6]}", "price": 100.0, "tax_rate": 0.0,
                 "uom": "unit", "pricing": {"purchase_price": 50.0, "selling_price": 100.0, "currency": "INR"}},
                 headers=admin).json()["id"]
    c.post(f"/warehouses/{wh}/stock/adjust", json={"product_id": pid, "quantity": 500}, headers=admin)
    partner, pauth = _staff(admin)
    veh = c.post("/vehicles", json={"vehicle_number": f"MH{uuid.uuid4().hex[:6].upper()}", "vehicle_type": "Van"},
                 headers=admin).json()["id"]
    return {"org": org_id, "admin": admin, "wh": wh, "cust": cust, "pid": pid, "partner": partner, "pauth": pauth,
            "veh": veh}


@pytest.fixture(scope="module")
def day():
    return _new_day()


def _approved_day_end(d, partner, days_back=0):
    from datetime import timedelta
    with SessionLocal() as s:
        s.add(DeliveryDayEnd(organization_id=d["org"], delivery_partner_id=partner, status="approved",
                             business_date=day_end_service.today(s, d["org"]) - timedelta(days=days_back)))
        s.commit()


def _ready(d, qty=3):
    r = c.post("/orders", json={"customer_id": d["cust"], "warehouse_id": d["wh"], "delivery_method": "home_delivery",
               "payment_status": "pending", "items": [{"product_id": d["pid"], "quantity": qty}]}, headers=d["admin"])
    did = r.json()["delivery_id"]
    a = d["admin"]
    assert c.patch(f"/deliveries/by-id/{did}", json={"delivery_partner_id": d["partner"], "vehicle_id": d["veh"]},
                   headers=a).status_code == 200
    assert c.post(f"/deliveries/{did}/accept", headers=d["pauth"]).status_code == 200
    line = c.get(f"/deliveries/by-id/{did}", headers=a).json()["items"][0]["id"]
    assert c.post(f"/deliveries/{did}/pick", json={"items": [{"delivery_item_id": line, "picked_quantity": qty}]},
                  headers=a).status_code == 200
    assert c.post(f"/deliveries/{did}/ready", headers=a).status_code == 200
    return did, line


def _snapshot(d):
    with SessionLocal() as s:
        return {
            "movements": s.query(StockMovement).filter(StockMovement.organization_id == d["org"]).count(),
            "warehouse": [w.on_hand_quantity for w in s.query(WarehouseStock).filter(WarehouseStock.product_id == d["pid"])],
            "vehicle": sorted((i.loaded_qty, i.extra_qty, i.delivered_qty, i.returned_qty)
                              for i in s.query(VehicleLoadingItem).join(VehicleLoading)
                              .filter(VehicleLoading.organization_id == d["org"])),
            "runs": sorted(r.status for r in s.query(VehicleLoading).filter(VehicleLoading.organization_id == d["org"])),
            "delivery_lines": sorted((i.loaded_quantity, i.delivered_quantity) for i in s.query(DeliveryItem)),
        }


def _closed(r):
    return r.status_code == 409 and r.json()["detail"]["code"] == "DAY_END_CLOSED"


def test_closed_day_blocks_every_vehicle_stock_change(day):
    d = day
    # Open day: load, dispatch, extra load all work.
    did, line = _ready(d)
    assert c.post(f"/deliveries/{did}/load", json={}, headers=d["admin"]).status_code == 200
    assert c.patch(f"/deliveries/by-id/{did}", json={"status": "in_transit"}, headers=d["admin"]).status_code == 200
    with SessionLocal() as s:
        run = s.query(VehicleLoading).filter(VehicleLoading.delivery_partner_id == d["partner"],
                                             VehicleLoading.status == "active").one().id
    r = c.post(f"/vehicle-stock/{run}/extra-load", json={"items": [{"product_id": d["pid"], "quantity": 1}]},
               headers=d["admin"])
    assert r.status_code == 200, r.text
    did2, _ = _ready(d)                                         # ready, not loaded yet

    # The partner's Day-End for today is approved (closed).
    with SessionLocal() as s:
        s.add(DeliveryDayEnd(organization_id=d["org"], delivery_partner_id=d["partner"],
                             business_date=day_end_service.today(s, d["org"]), status="approved"))
        s.commit()
    before = _snapshot(d)

    assert _closed(c.post(f"/deliveries/{did2}/load", json={}, headers=d["admin"]))
    assert _closed(c.post("/vehicle-stock/loading", json={"delivery_id": did2}, headers=d["admin"]))
    assert _closed(c.post("/vehicle-stock/loading", json={"delivery_partner_id": d["partner"],
                                                           "items": [{"product_id": d["pid"], "loaded_qty": 1}]},
                          headers=d["admin"]))
    assert _closed(c.post(f"/vehicle-stock/{run}/extra-load", json={"items": [{"product_id": d["pid"], "quantity": 1}]},
                          headers=d["admin"]))
    assert _closed(c.post(f"/vehicle-stock/{run}/end-of-day",
                          json={"items": [{"product_id": d["pid"], "returned_qty": 1}]}, headers=d["admin"]))
    assert _closed(c.post(f"/deliveries/{did}/confirm", json={"items": [{"delivery_item_id": line,
                                                                         "delivered_quantity": 1}]},
                          headers=d["admin"]))
    assert _closed(c.post(f"/deliveries/{did}/app/confirm", json={"items": [{"delivery_item_id": line,
                                                                             "delivered_quantity": 1}]},
                          headers=d["pauth"]))

    assert _snapshot(d) == before                               # zero movements, zero quantity changes
    # Still closed, and history is still readable.
    with SessionLocal() as s:
        assert s.query(DeliveryDayEnd).filter(DeliveryDayEnd.delivery_partner_id == d["partner"]).one().status == "approved"
    assert c.get(f"/deliveries/by-id/{did}", headers=d["admin"]).status_code == 200
    assert c.get(f"/vehicle-stock/{run}", headers=d["admin"]).status_code == 200
    assert c.get("/delivery-day-ends", headers=d["admin"]).status_code == 200


# ------------------------------------------------------------------------- plans
def test_plan_user_limits():
    _, admin = _org()
    plans = {p["name"]: p for p in c.get("/plans", headers=admin).json()}
    assert plans["Basic"]["max_users"] == 1
    assert plans["Pro"]["max_users"] == 5
    assert plans["Enterprise"]["max_users"] is None
    from app.core.entitlements import default_limits_for_plan
    assert default_limits_for_plan("Pro")["max_users"] == 5


def test_pro_enforces_five_users_and_keeps_existing_users():
    org_id, admin = _org("Pro")
    created = []
    for _ in range(10):
        email = f"pro_{uuid.uuid4().hex[:8]}@example.com"
        r = c.post("/users", json={"name": "S", "email": email, "password": "Password123!", "role": "sales_officer"},
                   headers=admin)
        if r.status_code != 201:
            assert r.status_code == 403 and r.json()["detail"]["code"] == "PLAN_LIMIT_REACHED", r.text
            assert r.json()["detail"]["limit"] == 5
            break
        created.append(r.json()["id"])
    with SessionLocal() as s:
        active = s.query(User).filter(User.organization_id == org_id, User.is_active.is_(True)).count()
    assert active == 5                                          # admin + 4 staff
    # A firm already above the limit keeps its users; it just cannot add more.
    with SessionLocal() as s:
        for k in range(3):
            s.add(User(email=f"over{k}.{uuid.uuid4().hex[:6]}@example.com", name="Over", organization_id=org_id,
                       system_role="staff", role=UserRole.SALES_OFFICER, password_hash=hash_password("Password123!"),
                       is_active=True))
        s.commit()
    listed = c.get("/users", params={"page_size": 100}, headers=admin).json()
    rows = listed["items"] if isinstance(listed, dict) else listed
    assert len(rows) >= 8
    r = c.post("/users", json={"name": "S", "email": f"x_{uuid.uuid4().hex[:6]}@example.com",
                               "password": "Password123!", "role": "sales_officer"}, headers=admin)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "PLAN_LIMIT_REACHED"


# ------------------------------------- the lock is only that partner's closed day (addendum B)
def test_next_day_and_other_partner_and_other_firm_can_still_load():
    d = _new_day()
    _approved_day_end(d, d["partner"], days_back=1)            # yesterday approved and closed
    did, _ = _ready(d)
    r = c.post(f"/deliveries/{did}/load", json={}, headers=d["admin"])
    assert r.status_code == 200, r.text                        # today's load works
    partner_2, _ = _staff(d["admin"])                         # a partner has one active run, so a second one
    _approved_day_end(d, partner_2, days_back=1)
    r = c.post("/vehicle-stock/loading", json={"delivery_partner_id": partner_2,
                                               "items": [{"product_id": d["pid"], "loaded_qty": 1}]}, headers=d["admin"])
    assert r.status_code in (200, 201), r.text                 # "Add Vehicle" / plain load today works

    # Partner A's day today is closed; partner B (same firm) and another firm are not affected.
    e = _new_day()
    _approved_day_end(e, e["partner"])
    partner_b, pauth_b = _staff(e["admin"])
    veh_b = c.post("/vehicles", json={"vehicle_number": f"MH{uuid.uuid4().hex[:6].upper()}", "vehicle_type": "Van"},
                   headers=e["admin"]).json()["id"]
    b = {**e, "partner": partner_b, "pauth": pauth_b, "veh": veh_b}
    did_b, _ = _ready(b)
    assert c.post(f"/deliveries/{did_b}/load", json={}, headers=e["admin"]).status_code == 200
    did_a, _ = _ready(e)
    assert _closed(c.post(f"/deliveries/{did_a}/load", json={}, headers=e["admin"]))
    other = _new_day()
    did_o, _ = _ready(other)
    assert c.post(f"/deliveries/{did_o}/load", json={}, headers=other["admin"]).status_code == 200


# ------------------------------------------------------------------- plans (more)
def test_basic_blocks_second_user():
    _, admin = _org("Basic")
    r = c.post("/users", json={"name": "S", "email": f"b_{uuid.uuid4().hex[:8]}@example.com",
                               "password": "Password123!", "role": "sales_officer"}, headers=admin)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "PLAN_LIMIT_REACHED", r.text


def test_grandfathered_pro_firm_keeps_working_and_seed_does_not_rewrite_live_row():
    org_id, admin = _org("Pro")
    with SessionLocal() as s:                                   # a firm already on 9 users (before the change)
        for k in range(8):
            s.add(User(email=f"gf{k}.{uuid.uuid4().hex[:6]}@example.com", name="Old", organization_id=org_id,
                       system_role="staff", role=UserRole.SALES_OFFICER, password_hash=hash_password("Password123!"),
                       is_active=True))
        s.commit()
        emails = [u.email for u in s.query(User).filter(User.organization_id == org_id, User.name == "Old")]
    # Every existing user can still log in; nobody is deactivated; only adding is blocked.
    assert c.post("/auth/login", json={"email": emails[0], "password": "Password123!"}).status_code == 200
    assert c.get("/auth/me", headers=admin).status_code == 200
    with SessionLocal() as s:
        assert s.query(User).filter(User.organization_id == org_id, User.is_active.is_(True)).count() == 9
    r = c.post("/users", json={"name": "S", "email": f"n_{uuid.uuid4().hex[:6]}@example.com",
                               "password": "Password123!", "role": "sales_officer"}, headers=admin)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "PLAN_LIMIT_REACHED"

    # A live Pro row still at 10 is left alone by the seed (the change is the pending SQL file).
    with SessionLocal() as s:
        pro = s.query(Plan).filter(Plan.name == "Pro").one()
        pro.max_users = 10
        s.commit()
        try:
            seed_plans(s)
            s.refresh(pro)
            assert pro.max_users == 10
        finally:
            pro.max_users = 5
            s.commit()


# ------------------------------------------------- seeded roles: new firms only (gap 3 / E)
def _role_perms(org_id):
    from app.models import Role as R
    with SessionLocal() as s:
        return {r.name: dict(r.permissions or {}) for r in s.query(R).filter(R.organization_id == org_id)}


def test_new_firm_seeded_roles():
    org_id, _ = _org()
    perms = _role_perms(org_id)
    dp = perms["Delivery Partner"]["deliveries"]
    assert dp["approve"] is False and dp["delete"] is False
    assert dp["view"] and dp["create"] and dp["edit"]                 # partner flows unchanged
    assert perms["Delivery Partner"]["vehicle_stock"]["create"] is True
    assert perms["Accountant"]["deliveries"] == {"view": True, "create": False, "edit": False, "delete": False,
                                                 "approve": True, "export": False, "download": False}
    assert not perms["Sales Officer"].get("deliveries", {}).get("approve")


def test_existing_firm_roles_are_not_touched_by_reseed():
    from app.models import Role as R
    from app.services import role_service
    org_id, _ = _org()
    old_dp = {"view": True, "create": True, "edit": True, "delete": True, "approve": True, "export": True, "download": True}
    with SessionLocal() as s:                                   # an existing firm, seeded before this change
        for r in s.query(R).filter(R.organization_id == org_id):
            p = dict(r.permissions)
            if r.name == "Delivery Partner":
                p["deliveries"] = old_dp
            if r.name == "Accountant":
                p.pop("deliveries", None)
            r.permissions = p
        s.commit()
        before = _role_perms(org_id)
        role_service.seed_default_roles_for_all_orgs(s)
    after = _role_perms(org_id)
    assert after == before
    assert after["Delivery Partner"]["deliveries"] == old_dp and "deliveries" not in after["Accountant"]
