"""GET /dashboard/me/sales and /dashboard/me/delivery -- the field app's own dashboards.

Every figure must equal the matching block of the admin Staff Detail page
(GET /users/{id}/overview) for the same employee and period, be scoped to the logged-in
employee only, and cost a fixed number of queries however much data there is.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from sqlalchemy import event

from app.core.database import Base, SessionLocal, engine
from app.core.security import create_access_token, hash_password
from app.main import app
from app.models import Customer, CustomerPayment, Delivery, Role, SalesOrder, User, Vehicle
from app.models.enums import SystemRole

Base.metadata.create_all(bind=engine)
client = TestClient(app)

NOW = datetime.now(timezone.utc)
DAY = timedelta(days=1)


# ------------------------------------------------------------------ helpers


def _org(prefix: str = "Mobile Dash"):
    email = f"mdash_{uuid.uuid4().hex[:10]}@example.com"
    r = client.post("/auth/register", json={
        "organization_name": f"{prefix} {uuid.uuid4().hex[:6]}", "admin_name": "Admin",
        "email": email, "password": "Password123!", "role": "admin",
    })
    assert r.status_code == 201, r.text
    db = SessionLocal()
    try:
        org_id = db.query(User).filter(User.email == email).one().organization_id
    finally:
        db.close()
    return {"org_id": org_id, "admin": {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}}


def _staff(org_id: str, role_name: str | None = None, permissions: dict | None = None):
    """A staff member on one of the firm's seeded roles (or a custom one), and their token."""
    db = SessionLocal()
    try:
        if permissions is not None:
            role = Role(organization_id=org_id, name=f"Custom {uuid.uuid4().hex[:6]}", permissions=permissions,
                        workspace="sales", data_scope="own")
            db.add(role)
            db.flush()
        else:
            role = db.query(Role).filter(Role.organization_id == org_id, Role.name == role_name).one()
        user = User(organization_id=org_id, name=f"Staff {uuid.uuid4().hex[:6]}",
                    email=f"staff_{uuid.uuid4().hex[:10]}@example.com", password_hash=hash_password("Password123!"),
                    system_role=SystemRole.STAFF.value, role_id=role.id, is_active=True)
        db.add(user)
        db.commit()
        return user.id, {"Authorization": f"Bearer {create_access_token(user.id, 'staff', org_id)}"}
    finally:
        db.close()


def _customer(db, org_id: str, assigned_to: str | None = None) -> Customer:
    c = Customer(organization_id=org_id, name=f"Cust {uuid.uuid4().hex[:6]}", business_name=f"Biz {uuid.uuid4().hex[:4]}",
                 phone=f"+91{uuid.uuid4().int % 10**10:010d}", assigned_sales_officer_id=assigned_to)
    db.add(c)
    db.flush()
    return c


def _order(db, org_id: str, customer: Customer | None, total: float, status: str = "placed", when: datetime = NOW,
           **people) -> SalesOrder:
    o = SalesOrder(organization_id=org_id, order_number=f"SO-{uuid.uuid4().hex[:8]}",
                   customer_id=customer.id if customer else None, status=status, total=total, subtotal=total,
                   discount=0, tax=0, source="direct", created_at=when, order_date=when, **people)
    db.add(o)
    db.flush()
    return o


def _overview(env, user_id: str, **params) -> dict:
    r = client.get(f"/users/{user_id}/overview", params=params, headers=env["admin"])
    assert r.status_code == 200, r.text
    return r.json()


def _mine(path: str, auth: dict, **params) -> dict:
    r = client.get(f"/dashboard/me/{path}", params=params, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _count_queries(action):
    count = 0

    def _inc(*_args):
        nonlocal count
        count += 1

    event.listen(engine, "before_cursor_execute", _inc)
    try:
        action()
    finally:
        event.remove(engine, "before_cursor_execute", _inc)
    return count


PERIODS = [{"period": "today"}, {"period": "week"}, {"period": "month"},
           {"date_from": (NOW - 12 * DAY).date().isoformat(), "date_to": (NOW - 2 * DAY).date().isoformat()}]


# ------------------------------------------------------------------ sales


def _sales_world():
    env = _org()
    seller, seller_auth = _staff(env["org_id"], "Sales Officer")
    other, other_auth = _staff(env["org_id"], "Sales Officer")
    db = SessionLocal()
    try:
        mine = _customer(db, env["org_id"], assigned_to=seller)
        _customer(db, env["org_id"], assigned_to=seller)
        theirs = _customer(db, env["org_id"], assigned_to=other)
        _order(db, env["org_id"], mine, 1000.0, "placed", NOW, created_by=seller)
        _order(db, env["org_id"], mine, 2500.0, "processing", NOW, salesperson_id=seller)
        _order(db, env["org_id"], mine, 4000.0, "completed", NOW - 3 * DAY, created_by=seller)
        _order(db, env["org_id"], None, 7000.0, "completed", NOW - 10 * DAY, created_by=seller)
        _order(db, env["org_id"], mine, 9999.0, "draft", NOW - 1 * DAY, created_by=seller)  # not a sale
        _order(db, env["org_id"], mine, 50.0, "cancelled", NOW - 2 * DAY, created_by=seller)  # not a sale
        _order(db, env["org_id"], theirs, 5555.0, "placed", NOW, created_by=other)           # someone else's
        db.commit()
    finally:
        db.close()
    return env, seller, seller_auth, other, other_auth


def test_sales_dashboard_matches_staff_detail_for_every_period():
    env, seller, auth, _, _ = _sales_world()
    for params in PERIODS:
        mine = _mine("sales", auth, **params)
        detail = _overview(env, seller, **params)
        assert set(mine) == {"period", "period_from", "period_to", "summary", "performance", "recent_orders"}
        assert (mine["period"], mine["period_from"], mine["period_to"]) == \
            (detail["period"], detail["period_from"], detail["period_to"])
        assert mine["summary"]["sales_amount"] == detail["summary"]["sales_amount"], params
        assert mine["summary"]["orders"] == detail["summary"]["orders"], params
        assert mine["summary"]["assigned_customers"] == detail["summary"]["assigned_customers"] == 2
        assert mine["performance"] == detail["performance"], params
        assert mine["recent_orders"] == detail["recent_orders"][:5], params
        assert mine["summary"]["today_sales_amount"] == _overview(env, seller, period="today")["summary"]["sales_amount"]


def test_sales_dashboard_values_and_limits():
    env, seller, auth, _, other_auth = _sales_world()
    today = _mine("sales", auth)["summary"]
    assert (today["sales_amount"], today["orders"], today["today_sales_amount"]) == (3500.0, 2, 3500.0)
    week = _mine("sales", auth, period="week")["summary"]
    assert (week["sales_amount"], week["orders"]) == (7500.0, 3)       # drafts / cancelled excluded
    month = _mine("sales", auth, period="month")
    assert (month["summary"]["sales_amount"], month["summary"]["orders"]) == (14500.0, 4)
    assert len(month["performance"]) == 30 and sum(p["orders"] for p in month["performance"]) == 4

    recent = month["recent_orders"]
    assert len(recent) == 5                                           # 6 of theirs, capped at 5
    assert all(o["amount"] != 5555.0 for o in recent)                 # never another employee's
    assert recent[0]["customer"]["name"].startswith("Biz ")           # business name, like Staff Detail

    other = _mine("sales", other_auth)["summary"]                     # and the other one sees only theirs
    assert (other["sales_amount"], other["orders"], other["assigned_customers"]) == (5555.0, 1, 1)


def test_sales_dashboard_empty_for_a_new_salesperson():
    env = _org()
    _, auth = _staff(env["org_id"], "Sales Officer")
    mine = _mine("sales", auth, period="week")
    assert mine["summary"] == {"sales_amount": 0.0, "orders": 0, "today_sales_amount": 0.0, "assigned_customers": 0}
    assert mine["recent_orders"] == []
    assert len(mine["performance"]) == 7 and all(p == {**p, "sales_amount": 0.0, "orders": 0} for p in mine["performance"])


# ------------------------------------------------------------------ delivery


def _delivery_world():
    env = _org()
    driver, driver_auth = _staff(env["org_id"], "Delivery Partner")
    other, _ = _staff(env["org_id"], "Delivery Partner")
    db = SessionLocal()
    try:
        cust = _customer(db, env["org_id"])
        van = Vehicle(organization_id=env["org_id"], vehicle_number=f"VAN-{uuid.uuid4().hex[:4]}", status="active")
        db.add(van)
        db.flush()

        def delivery(status, total, when, partner=driver, **extra):
            order = _order(db, env["org_id"], cust, total, "placed", when)
            d = Delivery(organization_id=env["org_id"], delivery_note_number=f"DN-{uuid.uuid4().hex[:6]}",
                         sales_order_id=order.id, customer_id=cust.id, delivery_partner_id=partner,
                         status=status, scheduled_date=when, **extra)
            db.add(d)
            db.flush()
            return order

        o1 = delivery("delivered", 1000.0, NOW, pod_signature_file_id=str(uuid.uuid4()), vehicle_id=van.id)
        o2 = delivery("partially_delivered", 2000.0, NOW)
        delivery("failed", 3000.0, NOW)
        delivery("planned", 500.0, NOW, vehicle_id=van.id)
        delivery("in_transit", 800.0, NOW - 2 * DAY)
        o6 = delivery("delivered", 1500.0, NOW - 20 * DAY)
        for _ in range(4):                                             # more open work than the list shows
            delivery("planned", 100.0, NOW - 5 * DAY)
        delivery("delivered", 9999.0, NOW, partner=other)              # someone else's
        for order, amount, when in ((o1, 1000.0, NOW), (o2, 500.0, NOW), (o6, 200.0, NOW - 20 * DAY)):
            db.add(CustomerPayment(organization_id=env["org_id"], customer_id=cust.id, order_id=order.id,
                                   amount=amount, payment_mode="cash", received_on=when))
        db.commit()
        return env, driver, driver_auth, van.id
    finally:
        db.close()


def test_delivery_dashboard_matches_staff_detail_for_every_period():
    env, driver, auth, _ = _delivery_world()
    for params in PERIODS:
        mine = _mine("delivery", auth, **params)
        detail = _overview(env, driver, **params)
        assert set(mine) == {"period", "period_from", "period_to", "summary", "performance",
                             "open_deliveries_count", "open_deliveries", "vehicle"}
        assert (mine["period"], mine["period_from"], mine["period_to"]) == \
            (detail["period"], detail["period_from"], detail["period_to"])
        assert mine["summary"] == detail["summary"], params
        assert mine["performance"] == detail["performance"], params
        assert mine["open_deliveries"] == detail["assigned_deliveries"][:5], params
        assert mine["open_deliveries_count"] == len(detail["assigned_deliveries"])
        assert mine["vehicle"] == detail["vehicle"]


def test_delivery_dashboard_values_and_limits():
    env, driver, auth, van_id = _delivery_world()
    today = _mine("delivery", auth)
    assert today["summary"] == {
        "deliveries": 4, "completed": 1, "pending": 2, "partial": 1, "failed": 1,
        "delivery_value": 6500.0, "amount_collected": 1500.0,
        "amount_receivable": 2800.0,        # (2000 - 500) + (1500 - 200); the paid one owes nothing
        "pod_completed": 1,
    }
    assert today["open_deliveries_count"] == 7                        # partial + planned + in_transit + 4 planned
    assert len(today["open_deliveries"]) == 5                         # capped
    assert all(d["order"]["amount"] != 9999.0 for d in today["open_deliveries"])
    assert today["vehicle"]["id"] == van_id
    month = _mine("delivery", auth, period="month")["summary"]
    assert month["completed"] == 2 and month["amount_collected"] == 1700.0


def test_delivery_dashboard_empty_for_a_new_driver():
    env = _org()
    _, auth = _staff(env["org_id"], "Delivery Partner")
    mine = _mine("delivery", auth)
    assert mine["summary"] == {"deliveries": 0, "completed": 0, "pending": 0, "partial": 0, "failed": 0,
                               "delivery_value": 0.0, "amount_collected": 0.0, "amount_receivable": 0.0,
                               "pod_completed": 0}
    assert (mine["open_deliveries_count"], mine["open_deliveries"], mine["vehicle"]) == (0, [], None)


# ------------------------------------------------------------------ access, filters, isolation


def test_access_rules():
    env = _org()
    _, seller = _staff(env["org_id"], "Sales Officer")
    _, driver = _staff(env["org_id"], "Delivery Partner")
    _, accountant = _staff(env["org_id"], "Accountant")
    _, no_dashboard = _staff(env["org_id"], permissions={"customers": {"view": True}})

    assert client.get("/dashboard/me/sales", headers=seller).status_code == 200
    assert client.get("/dashboard/me/delivery", headers=driver).status_code == 200
    # Wrong workspace, an accountant, an admin, a role without dashboard.view, no token
    for path, auth in (("delivery", seller), ("sales", driver), ("sales", accountant), ("delivery", accountant),
                       ("sales", env["admin"]), ("sales", no_dashboard)):
        assert client.get(f"/dashboard/me/{path}", headers=auth).status_code == 403, (path, auth)
    assert client.get("/dashboard/me/sales").status_code in (401, 403)
    # No way to ask for someone else: extra parameters are ignored, figures stay the caller's own
    other_id, _ = _staff(env["org_id"], "Sales Officer")
    r = client.get("/dashboard/me/sales", params={"user_id": other_id}, headers=seller)
    assert r.status_code == 200 and r.json()["summary"]["assigned_customers"] == 0


def test_period_and_date_validation():
    env = _org()
    _, seller = _staff(env["org_id"], "Sales Officer")
    for params in ({"period": "year"}, {"date_from": "2026-13-01"}, {"date_from": "2026-10-10", "date_to": "2026-10-01"}):
        assert client.get("/dashboard/me/sales", params=params, headers=seller).status_code == 400, params
    r = _mine("sales", seller, date_from="2026-10-01", date_to="2026-10-05")
    assert (r["period"], r["period_from"], r["period_to"], len(r["performance"])) == ("custom", "2026-10-01", "2026-10-05", 5)


def test_firm_isolation():
    env_a, seller_a, auth_a, _, _ = _sales_world()
    env_b = _org("Other Firm")
    _, auth_b = _staff(env_b["org_id"], "Sales Officer")
    b = _mine("sales", auth_b, period="month")
    assert b["summary"]["sales_amount"] == 0.0 and b["recent_orders"] == []
    assert _mine("sales", auth_a, period="month")["summary"]["sales_amount"] == 14500.0


def test_query_count_does_not_grow_with_data():
    env = _org()
    seller, seller_auth = _staff(env["org_id"], "Sales Officer")
    driver, driver_auth = _staff(env["org_id"], "Delivery Partner")

    def seed(n: int) -> None:
        db = SessionLocal()
        try:
            cust = _customer(db, env["org_id"], assigned_to=seller)
            for i in range(n):
                order = _order(db, env["org_id"], cust, 100.0, "placed", NOW - (i % 20) * DAY, created_by=seller)
                db.add(Delivery(organization_id=env["org_id"], delivery_note_number=f"DN-{uuid.uuid4().hex[:6]}",
                                sales_order_id=order.id, customer_id=cust.id, delivery_partner_id=driver,
                                status=("delivered", "planned", "failed")[i % 3], scheduled_date=NOW - (i % 20) * DAY))
                db.add(CustomerPayment(organization_id=env["org_id"], customer_id=cust.id, order_id=order.id,
                                       amount=50.0, payment_mode="cash", received_on=NOW))
            db.commit()
        finally:
            db.close()

    seed(3)
    small = (_count_queries(lambda: _mine("sales", seller_auth, period="month")),
             _count_queries(lambda: _mine("delivery", driver_auth, period="month")))
    seed(60)
    large = (_count_queries(lambda: _mine("sales", seller_auth, period="month")),
             _count_queries(lambda: _mine("delivery", driver_auth, period="month")))
    assert large == small, (small, large)
