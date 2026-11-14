"""QA 14 Nov backend tasks:
- Vehicle Stock page: GET /vehicle-stock/today, /vehicle-stock/history, /vehicle-stock/{id}
- sku / uom / variant_name on vehicle stock items
- GET /dashboard/admin `today_overall`: today's figures for the whole organization, no filter
- POST /expenses: notifications are written after the response, still for the same people
"""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Customer, CustomerPayment, Notification, Organization, Product, ProductVariant, Role, SalesOrder, User, Vehicle,
    VehicleLoading, VehicleLoadingItem,
)
from app.models.enums import UserRole  # noqa: E402
from app.services import dashboard_service, entitlement_service, numbering_service, role_service  # noqa: E402

client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)


@pytest.fixture(autouse=True)
def _no_plan_gates(monkeypatch):
    monkeypatch.setattr(entitlement_service, "check_entitlement", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "check_limit", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "has_entitlement", lambda *a, **k: True)
    dashboard_service._dashboard_cache.clear()


def _h(user):
    return {"Authorization": f"Bearer {create_access_token(user.id, user.system_role, user.organization_id)}"}


@pytest.fixture
def world():
    db = SessionLocal()
    try:
        org = Organization(name=f"QA14 {uuid.uuid4().hex[:6]}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}")
        db.add(org)
        db.flush()
        role_service.seed_default_roles(db, org.id)
        dp_role = db.query(Role).filter(Role.organization_id == org.id, Role.name == "Delivery Partner").one()
        acct_role = db.query(Role).filter(Role.organization_id == org.id, Role.name == "Accountant").one()

        def user(name, system_role, legacy, role_id=None):
            u = User(email=f"{name}.{uuid.uuid4().hex[:8]}@example.com", name=name, organization_id=org.id,
                     system_role=system_role, role=legacy, role_id=role_id,
                     password_hash=hash_password("Password123!"), is_active=True)
            db.add(u)
            db.flush()
            return u

        admin = user("Admin", "admin", UserRole.ADMIN)
        admin2 = user("Admin Two", "admin", UserRole.ADMIN)
        ravi = user("Ravi Driver", "staff", UserRole.DELIVERY_PARTNER, dp_role.id)
        sunil = user("Sunil Driver", "staff", UserRole.DELIVERY_PARTNER, dp_role.id)
        acct = user("Accountant", "staff", UserRole.ACCOUNTANT, acct_role.id)
        v1 = Vehicle(organization_id=org.id, vehicle_number="MH12AB1234")
        v2 = Vehicle(organization_id=org.id, vehicle_number="KA01ZZ9999")
        prod = Product(organization_id=org.id, name="Wire", sku="WIRE-90", uom="coil", total_inventory=0,
                       is_active=True)
        db.add_all([v1, v2, prod])
        db.flush()
        var = ProductVariant(product_id=prod.id, name="Red", sku="WIRE-90-R", inventory=0,
                             variant_code=numbering_service.next_variant_code(db, org.id))
        db.add(var)
        db.flush()
        now = datetime.now(timezone.utc)

        def loading(partner, vehicle, when, status, loaded, extra, delivered, returned):
            lo = VehicleLoading(organization_id=org.id, delivery_partner_id=partner.id, vehicle_id=vehicle.id,
                                date=when, status=status)
            lo.items.append(VehicleLoadingItem(product_id=prod.id, product_name=prod.name, loaded_qty=loaded,
                                               extra_qty=extra, delivered_qty=delivered, returned_qty=returned))
            lo.items.append(VehicleLoadingItem(product_id=prod.id, variant_id=var.id, product_name=prod.name,
                                               loaded_qty=1, extra_qty=0, delivered_qty=0, returned_qty=0))
            db.add(lo)
            db.flush()
            return lo.id

        ids = {
            "active_ravi": loading(ravi, v1, now, "active", 10, 2, 5, 0),
            "active_sunil_old": loading(sunil, v2, now - timedelta(days=3), "active", 4, 0, 1, 0),
            "closed_ravi_old": loading(ravi, v1, now - timedelta(days=10), "closed", 8, 0, 6, 2),
            "closed_sunil_older": loading(sunil, v2, now - timedelta(days=40), "closed", 5, 0, 5, 0),
        }
        from types import SimpleNamespace

        def plain(u):  # survives the session closing
            return SimpleNamespace(id=u.id, system_role=u.system_role, organization_id=u.organization_id)

        out = dict(org=org.id, admin=plain(admin), admin2=plain(admin2), ravi=plain(ravi), sunil=plain(sunil),
                   acct=plain(acct), ids=ids)
        db.commit()
        return out
    finally:
        db.close()


# ------------------------------------------------------------------ Task 1: Vehicle Stock page


def test_today_lists_every_loaded_vehicle_with_the_row_columns(world):
    r = client.get("/vehicle-stock/today", headers=_h(world["admin"]))
    assert r.status_code == 200, r.text
    rows = {row["id"]: row for row in r.json()}
    # both active vehicles, including one loaded 3 days ago and still out; no old closed runs
    assert set(rows) == {world["ids"]["active_ravi"], world["ids"]["active_sunil_old"]}
    ravi = rows[world["ids"]["active_ravi"]]
    assert ravi["vehicle_number"] == "MH12AB1234" and ravi["driver_name"] == "Ravi Driver"
    # order 10+1, loaded 10+2+1, delivered 5, returned 0, available 13-5
    assert (ravi["order_units"], ravi["loaded_units"], ravi["delivered_units"], ravi["returned_units"],
            ravi["available_units"]) == (11, 13, 5, 0, 8)
    assert ravi["status"] == "active" and ravi["item_count"] == 2 and ravi["vehicle_id"]


def test_history_is_paginated_filtered_and_counted(world):
    h = _h(world["admin"])
    r = client.get("/vehicle-stock/history", params={"page": 1, "page_size": 2}, headers=h)
    body = r.json()
    assert r.status_code == 200 and body["total"] == 4 and body["total_pages"] == 2 and len(body["items"]) == 2
    assert body["items"][0]["id"] == world["ids"]["active_ravi"]  # newest first

    def ids(**params):
        return {row["id"] for row in client.get("/vehicle-stock/history", params=params, headers=h).json()["items"]}

    assert ids(search="ka01") == {world["ids"]["active_sunil_old"], world["ids"]["closed_sunil_older"]}
    assert ids(search="ravi") == {world["ids"]["active_ravi"], world["ids"]["closed_ravi_old"]}
    assert ids(status="closed") == {world["ids"]["closed_ravi_old"], world["ids"]["closed_sunil_older"]}
    today = datetime.now(timezone.utc).date()
    assert ids(date_from=(today - timedelta(days=15)).isoformat(), date_to=(today - timedelta(days=5)).isoformat()) \
        == {world["ids"]["closed_ravi_old"]}
    db = SessionLocal()
    try:
        v1 = db.query(Vehicle).filter(Vehicle.organization_id == world["org"], Vehicle.vehicle_number == "MH12AB1234").one()
    finally:
        db.close()
    assert ids(vehicle_id=v1.id) == {world["ids"]["active_ravi"], world["ids"]["closed_ravi_old"]}


def test_detail_by_id_opens_closed_and_active_sessions(world):
    r = client.get(f"/vehicle-stock/{world['ids']['closed_ravi_old']}", headers=_h(world["admin"]))
    assert r.status_code == 200 and r.json()["status"] == "closed" and len(r.json()["items"]) == 2


def test_delivery_partner_sees_only_their_own_runs(world):
    h = _h(world["ravi"])
    assert {row["id"] for row in client.get("/vehicle-stock/today", headers=h).json()} == {world["ids"]["active_ravi"]}
    assert client.get("/vehicle-stock/history", headers=h).json()["total"] == 2
    assert client.get(f"/vehicle-stock/{world['ids']['active_sunil_old']}", headers=h).status_code == 404
    # the existing Delivery Partner endpoint still works as before
    r = client.get(f"/vehicle-stock/current/{world['ravi'].id}", headers=h)
    assert r.status_code == 200 and r.json()["id"] == world["ids"]["active_ravi"]


# ------------------------------------------------------------------ Task 2: item fields


def test_items_carry_sku_uom_and_variant_name(world):
    r = client.get("/vehicle-stock", headers=_h(world["admin"]), params={"page_size": 100})
    items = [i for row in r.json()["items"] for i in row["items"]]
    plain = next(i for i in items if i["variant_id"] is None)
    variant = next(i for i in items if i["variant_id"] is not None)
    assert (plain["sku"], plain["uom"], plain["variant_name"]) == ("WIRE-90", "coil", None)
    assert (variant["sku"], variant["uom"], variant["variant_name"]) == ("WIRE-90-R", "coil", "Red")
    detail = client.get(f"/vehicle-stock/current/{world['ravi'].id}", headers=_h(world["admin"])).json()
    assert {i["sku"] for i in detail["items"]} == {"WIRE-90", "WIRE-90-R"}


# ------------------------------------------------------------------ Task 3: dashboard today_overall


def test_today_overall_ignores_every_filter(world):
    db = SessionLocal()
    try:
        c1 = Customer(organization_id=world["org"], name="Shop One")
        c2 = Customer(organization_id=world["org"], name="Shop Two")
        db.add_all([c1, c2])
        db.flush()
        now = datetime.now(timezone.utc)
        for cust, total in ((c1, 100.0), (c2, 250.0)):
            db.add(SalesOrder(organization_id=world["org"], order_number=f"SO-{uuid.uuid4().hex[:6]}",
                              customer_id=cust.id, status="placed", total=total, created_at=now))
        db.add(CustomerPayment(organization_id=world["org"], customer_id=c1.id, amount=40.0, received_on=now))
        db.add(CustomerPayment(organization_id=world["org"], customer_id=c2.id, amount=60.0, received_on=now))
        db.commit()
        c1_id = c1.id
    finally:
        db.close()
    h = _h(world["admin"])
    plain = client.get("/dashboard/admin", headers=h).json()
    old_range = client.get("/dashboard/admin", headers=h, params={"date_from": "2024-01-01", "date_to": "2024-01-31"}).json()
    one_customer = client.get("/dashboard/admin", headers=h, params={"customer_id": c1_id}).json()

    overall = plain["today_overall"]
    assert overall["orders"] == 2 and overall["sales"] == 350.0 and overall["received"] == 100.0
    assert overall["pending_orders"] == 2 and overall["pending_amount"] == 350.0
    assert old_range["today_overall"] == overall and one_customer["today_overall"] == overall
    # `today` still follows the customer filter, as it always did
    assert one_customer["today"]["sales"] == 100.0 and plain["today"]["sales"] == 350.0


# ------------------------------------------------------------------ Task 5: expense notifications after the response


def test_expense_notifications_still_reach_the_admins(world, monkeypatch):
    from app.routers import expenses as expenses_router

    seen = []
    original = expenses_router._notify_expense_created

    def spy(expense_id, actor_id, creator_is_admin):
        s = SessionLocal()  # runs after the commit: the expense is already visible to another session
        try:
            from app.models import Expense

            seen.append(s.get(Expense, expense_id) is not None)
        finally:
            s.close()
        return original(expense_id, actor_id, creator_is_admin)

    monkeypatch.setattr(expenses_router, "_notify_expense_created", spy)
    r = client.post("/expenses", headers=_h(world["acct"]), json={"category": "Travel", "amount": 120, "description": "taxi"})
    assert r.status_code == 201, r.text
    assert seen == [True]
    db = SessionLocal()
    try:
        recipients = {n.user_id for n in db.query(Notification).filter(Notification.link == r.json()["id"])}
    finally:
        db.close()
    assert recipients == {world["admin"].id, world["admin2"].id}
