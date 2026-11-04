"""Admin dashboard: firm-wide figures only for firm-wide roles, and every day in the
organization's timezone (default Asia/Kolkata).

- Sales Officer / Delivery Partner (own scope) get 403; Accountant (all scope) and admins
  keep working -- decided by permission and data scope, not by role name.
- An order at 00:30 IST belongs to that IST day, one at 23:50 IST too; month boundaries and
  date_from/date_to are org-local inclusive days; the `today` block counts today's orders,
  their amount and today's delivered deliveries.
"""

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core import business_time
from app.core.database import Base, engine
from app.core.security import create_access_token
from app.main import app
from app.models import Customer, Delivery, Organization, Plan, Role, SalesOrder, User
from app.seed import seed_plans
from app.services import dashboard_service, role_service

IST = business_time.zone("Asia/Kolkata")


@pytest.fixture
def db():
    Base.metadata.create_all(bind=engine)
    session = Session(bind=engine)
    seed_plans(session)
    session.commit()
    dashboard_service.clear_dashboard_cache()
    try:
        yield session
    finally:
        session.close()
        dashboard_service.clear_dashboard_cache()


def _firm(db: Session, tz_name: str | None = "Asia/Kolkata"):
    plan = db.query(Plan).filter(Plan.name == "Enterprise").first()
    org = Organization(id=str(uuid.uuid4()), name=f"TZ {uuid.uuid4().hex[:6]}",
                       company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}", plan_id=plan.id, status="active",
                       timezone=tz_name)
    db.add(org)
    db.commit()
    role_service.seed_default_roles(db, org.id)
    roles = {r.name: r for r in db.query(Role).filter(Role.organization_id == org.id)}

    def person(system_role, role, role_name=None):
        user = User(id=str(uuid.uuid4()), organization_id=org.id, email=f"tz_{uuid.uuid4().hex[:10]}@x.com",
                    name=role, password_hash="h", role=role, system_role=system_role, is_active=True,
                    role_id=roles[role_name].id if role_name else None)
        db.add(user)
        db.commit()
        client = TestClient(app)
        client.headers = {"Authorization": "Bearer " + create_access_token(
            user.id, role=system_role, organization_id=org.id)}
        return user, client

    return org, {
        "admin": person("admin", "admin"),
        "sales": person("staff", "sales_officer", "Sales Officer"),
        "delivery": person("staff", "delivery_partner", "Delivery Partner"),
        "accountant": person("staff", "accountant", "Accountant"),
    }


def _order(db, org, created_at, total=100.0, status="placed", fulfilment="reserved", customer_id=None, commit=True):
    order = SalesOrder(id=str(uuid.uuid4()), organization_id=org.id, order_number=f"SO-{uuid.uuid4().hex[:8]}",
                       status=status, fulfilment_status=fulfilment, subtotal=total, total=total,
                       created_at=created_at, customer_id=customer_id)
    db.add(order)
    if commit:
        db.commit()
    return order


@pytest.fixture
def fixed_today(monkeypatch):
    """Pin the organization's "today" to a chosen date."""
    def pin(day: date):
        monkeypatch.setattr(business_time, "today", lambda tz: day)
    return pin


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


# ------------------------------------------------------------------ access


def test_firm_wide_dashboard_only_for_firm_wide_roles(db):
    _org, people = _firm(db)
    assert people["admin"][1].get("/dashboard/admin").status_code == 200
    assert people["accountant"][1].get("/dashboard/admin").status_code == 200  # all-scope role
    for name in ("sales", "delivery"):
        res = people[name][1].get("/dashboard/admin")
        assert res.status_code == 403, (name, res.text)
        assert "whole firm" in res.json()["detail"]
    # Their own dashboards keep working.
    assert people["sales"][1].get("/dashboard/me/sales").status_code == 200
    assert people["delivery"][1].get("/dashboard/me/delivery").status_code == 200


def test_access_follows_the_role_scope_not_its_name(db):
    org, people = _firm(db)
    sales_role = db.query(Role).filter(Role.organization_id == org.id, Role.name == "Sales Officer").first()
    sales_role.data_scope = "all"  # a firm-wide sales head
    db.commit()
    assert people["sales"][1].get("/dashboard/admin").status_code == 200
    accountant_role = db.query(Role).filter(Role.organization_id == org.id, Role.name == "Accountant").first()
    accountant_role.data_scope = "own"
    db.commit()
    assert people["accountant"][1].get("/dashboard/admin").status_code == 403


# ------------------------------------------------------------------ today's split
# Of today's placed orders: delivered/completed vs. the rest, count and amount.

# (status, fulfilment) -> fully delivered?
_TODAY_MIX = [
    (("completed", "delivered"), True),
    (("processing", "delivered"), True),          # goods delivered, status not yet closed
    (("completed", "partially_delivered"), True),  # closed short
    (("placed", "reserved"), False),
    (("processing", "in_transit"), False),
    (("processing", "partially_delivered"), False),
    (("processing", "failed"), False),
]


def test_today_delivered_and_pending_split_on_a_busy_day(db, fixed_today):
    org, people = _firm(db)
    fixed_today(date(2026, 11, 5))
    a = Customer(id=str(uuid.uuid4()), organization_id=org.id, name="Cust A")
    b = Customer(id=str(uuid.uuid4()), organization_id=org.id, name="Cust B")
    db.add_all([a, b])
    db.commit()

    expect = {None: [0, 0, 0.0, 0.0], a.id: [0, 0, 0.0, 0.0]}  # orders, delivered, amount, delivered amount

    def counted(customer, total, delivered):
        for key in (None, customer):
            if key in expect:
                e = expect[key]
                e[0] += 1
                e[1] += int(delivered)
                e[2] += total
                e[3] += total if delivered else 0

    # 00:30 IST on 5 Nov (19:00 UTC on 4 Nov), delivered -- counts for today.
    _order(db, org, utc(2026, 11, 4, 19, 0), 111.11, "completed", "delivered", a.id)
    counted(a.id, 111.11, True)
    # 63 more through the IST day (00:31 .. 23:59 IST), cycling through the mix and both customers.
    for i in range(63):
        (status, fulfilment), delivered = _TODAY_MIX[i % len(_TODAY_MIX)]
        customer = a.id if i % 3 else b.id
        total = round(10 + i * 7.25, 2)
        minute = 1 + i * 22  # minutes after 00:30 IST
        _order(db, org, utc(2026, 11, 4, 19, 0) + timedelta(minutes=minute), total, status, fulfilment,
               customer, commit=False)
        counted(customer, total, delivered)
    # Not today's sales: drafts, awaiting approval, cancelled, yesterday 23:50 IST, tomorrow 00:10 IST.
    for status, fulfilment in (("draft", "not_started"), ("awaiting_approval", "not_started"),
                               ("cancelled", "not_started")):
        _order(db, org, utc(2026, 11, 5, 6, 0), 5000, status, fulfilment, a.id, commit=False)
    _order(db, org, utc(2026, 11, 4, 18, 20), 4000, "completed", "delivered", a.id, commit=False)
    _order(db, org, utc(2026, 11, 5, 18, 40), 3000, "completed", "delivered", a.id, commit=False)
    db.commit()
    assert expect[None][0] == 64  # a 60+ order day

    for customer in (None, a.id):
        params = {"customer_id": customer} if customer else {}
        body = people["admin"][1].get("/dashboard/admin", params=params).json()
        orders, delivered, amount, delivered_amount = expect[customer]
        today = body["today"]
        # The existing fields keep their meaning.
        assert today["orders"] == orders and today["sales"] == round(amount, 2)
        assert today["sales"] == body["summary"]["today_sales"]
        assert today["delivered"] == 0  # no delivery confirmations recorded today
        # The new split.
        assert today["delivered_orders"] == delivered, customer
        assert today["pending_orders"] == orders - delivered
        assert today["delivered_amount"] == round(delivered_amount, 2)
        assert today["pending_amount"] == round(amount - delivered_amount, 2)
        assert today["delivered_orders"] + today["pending_orders"] == today["orders"]
        assert round(today["delivered_amount"] + today["pending_amount"], 2) == today["sales"]
    assert expect[a.id][0] < expect[None][0]  # the customer filter really narrowed it


# ------------------------------------------------------------------ timezone


def test_orders_at_0030_and_2350_ist_belong_to_that_ist_day(db, fixed_today):
    org, people = _firm(db)
    fixed_today(date(2026, 11, 5))
    early = _order(db, org, utc(2026, 11, 4, 19, 0), 100)    # 00:30 IST 5 Nov
    late = _order(db, org, utc(2026, 11, 5, 18, 20), 250)    # 23:50 IST 5 Nov
    _order(db, org, utc(2026, 11, 4, 18, 20), 40)             # 23:50 IST 4 Nov
    _order(db, org, utc(2026, 11, 5, 18, 40), 7)              # 00:10 IST 6 Nov (tomorrow)
    _order(db, org, utc(2026, 11, 5, 10, 0), 999, status="cancelled", fulfilment="cancelled")
    db.add(Delivery(id=str(uuid.uuid4()), organization_id=org.id, delivery_note_number="DN-1", status="delivered",
                    sales_order_id=early.id, confirmed_at=utc(2026, 11, 4, 20, 0)))       # 01:30 IST 5 Nov
    db.add(Delivery(id=str(uuid.uuid4()), organization_id=org.id, delivery_note_number="DN-2", status="delivered",
                    sales_order_id=late.id, confirmed_at=utc(2026, 11, 4, 18, 0)))        # 23:30 IST 4 Nov
    db.commit()

    body = people["admin"][1].get("/dashboard/admin").json()
    assert body["today"] == {"date": "2026-11-05", "timezone": "Asia/Kolkata", "orders": 2, "sales": 350.0,
                             "delivered": 1, "delivered_orders": 0, "pending_orders": 2,
                             "delivered_amount": 0.0, "pending_amount": 350.0}
    assert set(body["filters"]) == {"date_from", "date_to", "company_id", "warehouse_id", "customer_id",
                                    "supplier_id"}  # unchanged
    assert body["summary"]["today_sales"] == 350.0
    trend = {p["date"]: p for p in body["sales_trend"]}
    assert (trend["2026-11-05"]["orders"], trend["2026-11-05"]["sales"]) == (2, 350.0)
    assert (trend["2026-11-04"]["orders"], trend["2026-11-04"]["sales"]) == (1, 40.0)

    # date_from / date_to are org-local, inclusive days.
    day = people["admin"][1].get("/dashboard/admin", params={"date_from": "2026-11-05", "date_to": "2026-11-05"}).json()
    assert day["summary"]["period_sales"] == 350.0
    assert day["orders"]["total"] == 3  # incl. the cancelled one; the 6 Nov order is excluded
    assert [p["date"] for p in day["sales_trend"]] == ["2026-11-05"]


def test_month_boundary_is_the_ist_month(db, fixed_today):
    org, people = _firm(db)
    fixed_today(date(2026, 12, 1))
    _order(db, org, utc(2026, 11, 30, 18, 40), 70)   # 00:10 IST 1 Dec -> December, today
    _order(db, org, utc(2026, 11, 30, 18, 20), 30)   # 23:50 IST 30 Nov -> November
    body = people["admin"][1].get("/dashboard/admin").json()
    assert body["filters"]["date_from"] == "2026-12-01" and body["filters"]["date_to"] == "2026-12-01"
    assert body["summary"]["month_sales"] == 70.0 and body["summary"]["today_sales"] == 70.0
    assert body["summary"]["period_sales"] == 70.0
    nov = people["admin"][1].get("/dashboard/admin", params={"date_from": "2026-11-01", "date_to": "2026-11-30"}).json()
    assert nov["summary"]["period_sales"] == 30.0


def test_unset_or_unknown_org_timezone_means_asia_kolkata(db, fixed_today):
    for tz_name in (None, "Not/AZone"):
        org, people = _firm(db, tz_name)
        fixed_today(date(2026, 11, 5))
        _order(db, org, utc(2026, 11, 4, 19, 0), 10)  # 00:30 IST 5 Nov
        body = people["admin"][1].get("/dashboard/admin").json()
        assert body["today"]["timezone"] == "Asia/Kolkata" and body["today"]["orders"] == 1


def test_business_time_helper():
    assert business_time.local_date(utc(2026, 11, 4, 19, 0), IST) == date(2026, 11, 5)
    assert business_time.local_date(datetime(2026, 11, 4, 18, 29), IST) == date(2026, 11, 4)  # naive = UTC
    assert business_time.day_start(date(2026, 11, 5), IST) == utc(2026, 11, 4, 18, 30)
    assert business_time.day_end(date(2026, 11, 5), IST).astimezone(IST).date() == date(2026, 11, 5)
    assert str(business_time.zone(None)) == "Asia/Kolkata" and str(business_time.zone("Bad/Zone")) == "Asia/Kolkata"


def test_attendance_check_in_uses_the_org_day(db, monkeypatch):
    org, people = _firm(db, "Asia/Kolkata")
    seen = []

    def pinned(tz):
        seen.append(str(tz))
        return date(2026, 11, 5)

    monkeypatch.setattr(business_time, "today", pinned)
    res = people["sales"][1].post("/attendance/check-in", json={"type": "office_check_in"})
    assert res.status_code in (200, 201), res.text
    assert res.json()["date"] == "2026-11-05" and seen == ["Asia/Kolkata"]
