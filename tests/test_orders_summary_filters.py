"""GET /orders date/source filters and GET /orders/summary.

- date_from / date_to are organization-local calendar days (Asia/Kolkata by default),
  inclusive, on the order date (creation time when no order date was entered);
- source filters exactly; status=confirmed matches every order whose public status is
  confirmed (placed, processing, awaiting approval);
- the summary counts exactly what the list returns for the same filters and scope.
"""

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.database import Base, engine
from app.core.security import create_access_token
from app.main import app
from app.models import Organization, Plan, Role, SalesOrder, User
from app.seed import seed_plans
from app.services import role_service


@pytest.fixture
def db():
    Base.metadata.create_all(bind=engine)
    session = Session(bind=engine)
    seed_plans(session)
    session.commit()
    try:
        yield session
    finally:
        session.close()


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def _firm(db: Session):
    plan = db.query(Plan).filter(Plan.name == "Enterprise").first()
    org = Organization(id=str(uuid.uuid4()), name=f"Sum {uuid.uuid4().hex[:6]}", timezone="Asia/Kolkata",
                       company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}", plan_id=plan.id, status="active")
    db.add(org)
    db.commit()
    role_service.seed_default_roles(db, org.id)
    sales_role = db.query(Role).filter(Role.organization_id == org.id, Role.name == "Sales Officer").first()
    admin = User(id=str(uuid.uuid4()), organization_id=org.id, email=f"s_{uuid.uuid4().hex[:10]}@x.com",
                 name="Admin", password_hash="h", role="admin", system_role="admin", is_active=True)
    sales = User(id=str(uuid.uuid4()), organization_id=org.id, email=f"o_{uuid.uuid4().hex[:10]}@x.com",
                 name="Sales", password_hash="h", role="sales_officer", system_role="staff", is_active=True,
                 role_id=sales_role.id)
    db.add_all([admin, sales])
    db.commit()

    def client(user):
        c = TestClient(app)
        c.headers = {"Authorization": "Bearer " + create_access_token(
            user.id, role=user.system_role, organization_id=org.id)}
        return c

    return org, admin, sales, client(admin), client(sales)


def _order(db, org, by, created_at, total, status, source="direct", order_date=None):
    db.add(SalesOrder(id=str(uuid.uuid4()), organization_id=org.id, order_number=f"SO-{uuid.uuid4().hex[:8]}",
                      status=status, fulfilment_status="not_started" if status == "draft" else "reserved",
                      subtotal=total, total=total, created_at=created_at, order_date=order_date, source=source,
                      created_by=by.id, salesperson_id=by.id))


def _seed(db, org, admin, sales):
    rows = [
        # created_at (UTC)          total  status               source
        (utc(2026, 11, 4, 19, 0), 100, "placed", "direct"),             # 00:30 IST 5 Nov
        (utc(2026, 11, 5, 18, 20), 200, "processing", "quotation"),      # 23:50 IST 5 Nov
        (utc(2026, 11, 5, 9, 0), 300, "awaiting_approval", "direct"),    # 5 Nov
        (utc(2026, 11, 5, 10, 0), 400, "completed", "delivery_vehicle"),  # 5 Nov
        (utc(2026, 11, 5, 11, 0), 50, "draft", "direct"),                # 5 Nov
        (utc(2026, 11, 5, 12, 0), 70, "cancelled", "office"),            # 5 Nov
        (utc(2026, 11, 4, 18, 20), 999, "placed", "direct"),             # 23:50 IST 4 Nov
        (utc(2026, 11, 5, 18, 40), 888, "completed", "direct"),          # 00:10 IST 6 Nov
    ]
    for created_at, total, status, source in rows:
        _order(db, org, admin, created_at, total, status, source)
    # Backdated: created on 6 Nov but the order date says 5 Nov (IST).
    _order(db, org, admin, utc(2026, 11, 6, 6, 0), 5, "placed", order_date=utc(2026, 11, 5, 5, 0))
    # A sales officer's own order (they see only theirs).
    _order(db, org, sales, utc(2026, 11, 5, 8, 0), 60, "placed")
    db.commit()


def test_date_filter_is_the_org_day_and_summary_matches_the_list(db):
    org, admin, sales, client, _ = _firm(db)
    _seed(db, org, admin, sales)
    day = {"date_from": "2026-11-05", "date_to": "2026-11-05"}
    listed = client.get("/orders", params={**day, "page_size": 100}).json()
    totals = sorted(o["total"] for o in listed["items"])
    assert totals == sorted([100, 200, 300, 400, 50, 70, 5, 60])  # 00:30 + 23:50 IST in; 4 Nov / 6 Nov out

    summary = client.get("/orders/summary", params=day).json()
    assert summary == {
        "total": 8, "draft": 1, "confirmed": 5, "completed": 1, "cancelled": 1,
        "total_value": 1065.0, "draft_value": 50.0, "confirmed_value": 665.0, "completed_value": 400.0,
        "cancelled_value": 70.0,
    }
    # Each card equals the list filtered by that status.
    for public in ("draft", "confirmed", "completed", "cancelled"):
        page = client.get("/orders", params={**day, "status": public, "page_size": 100}).json()
        assert page["total"] == summary[public], public
        assert {o["status"] for o in page["items"]} <= {public}
        assert round(sum(o["total"] for o in page["items"]), 2) == summary[f"{public}_value"]


def test_source_filter_and_one_sided_ranges(db):
    org, admin, sales, client, _ = _firm(db)
    _seed(db, org, admin, sales)
    direct = client.get("/orders", params={"source": "direct", "page_size": 100}).json()
    assert direct["total"] == 7 and all(o["source"] == "direct" for o in direct["items"])
    assert client.get("/orders/summary", params={"source": "quotation"}).json()["total"] == 1
    assert client.get("/orders/summary", params={"date_from": "2026-11-06"}).json()["total"] == 1   # 00:10 IST 6 Nov
    assert client.get("/orders/summary", params={"date_to": "2026-11-04"}).json()["total"] == 1     # 23:50 IST 4 Nov
    bad = client.get("/orders/summary", params={"date_from": "2026-11-06", "date_to": "2026-11-05"})
    assert bad.status_code == 422
    assert client.get("/orders", params={"date_from": "05-11-2026"}).status_code == 422


def test_summary_follows_the_data_scope(db):
    org, admin, sales, _, sales_client = _firm(db)
    _seed(db, org, admin, sales)
    mine = sales_client.get("/orders/summary").json()
    assert mine["total"] == 1 and mine["confirmed"] == 1 and mine["total_value"] == 60.0
    assert sales_client.get("/orders").json()["total"] == 1
