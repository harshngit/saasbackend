"""Performance changes keep every response the same and the query count flat.

- The order list is built from data preloaded for the whole page: each item must equal
  GET /orders/{id} for the same order, and a bigger page must not cost more queries.
- The entitlements summary reads each override table once; it must agree with the
  per-key checks, and a new override must apply at once (the cache is cleared).
- The admin dashboard is cached briefly; a write must clear it.
- PERF_LOG's request line, the idle-connection ping and RUN_STARTUP_MIGRATIONS.
"""

import logging
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from app.core.database import Base, engine
from app.core.security import create_access_token
from app.main import app
from app.models import (
    Customer, CustomerPayment, Invoice, Organization, OrganizationFeatureOverride, Plan, Product, User, Warehouse,
)
from app.seed import seed_plans
from app.services import dashboard_service, entitlement_service, order_service, stock_service


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


def _tenant(db: Session, n_orders: int):
    """An Enterprise firm with an admin, a stocked warehouse and `n_orders` orders; the
    first order also has an invoice and a direct payment."""
    plan = db.query(Plan).filter(Plan.name == "Enterprise").first()
    org = Organization(id=str(uuid.uuid4()), name=f"Perf {uuid.uuid4().hex[:6]}",
                       company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}", plan_id=plan.id, status="active")
    db.add(org)
    db.flush()
    admin = User(id=str(uuid.uuid4()), organization_id=org.id, email=f"p_{uuid.uuid4().hex[:10]}@x.com",
                 name="Admin", password_hash="h", role="admin", system_role="admin", is_active=True)
    wh = Warehouse(id=str(uuid.uuid4()), organization_id=org.id, name="Main", code=f"W{uuid.uuid4().hex[:5]}")
    products = [Product(id=str(uuid.uuid4()), organization_id=org.id, name=f"P{i}", price=50 + i,
                        tax_rate=18, inventory_tracking=True) for i in range(3)]
    customer = Customer(id=str(uuid.uuid4()), organization_id=org.id, name="Acme", phone="9876500002",
                        credit_limit=1_000_000)
    db.add_all([admin, wh, customer, *products])
    db.commit()
    for p in products:
        stock_service.adjust_on_hand(db, org.id, wh.id, p.id, None, 500, "adjustment")
    db.commit()
    orders = []
    for i in range(n_orders):
        order, _ = order_service.place_order(
            db, admin, customer,
            [order_service.OrderLine(product_id=products[i % 3].id, quantity=2),
             order_service.OrderLine(product_id=products[(i + 1) % 3].id, quantity=1)],
            warehouse_id=wh.id)
        orders.append(order)
    db.commit()
    first = orders[0]
    invoice = Invoice(id=str(uuid.uuid4()), organization_id=org.id, invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
                      order_id=first.id, customer_id=customer.id, subtotal=first.subtotal, tax=first.tax,
                      total=first.total, amount_paid=10.0, status="partial", invoice_date=datetime.now(timezone.utc))
    db.add(invoice)
    db.add(CustomerPayment(id=str(uuid.uuid4()), organization_id=org.id, customer_id=customer.id, order_id=first.id,
                           amount=10.0, payment_mode="cash", received_on=datetime.now(timezone.utc)))
    db.commit()
    client = TestClient(app)
    client.headers = {"Authorization": f"Bearer {create_access_token(admin.id, role='admin', organization_id=org.id)}"}
    return org, admin, client


class _Counter:
    def __init__(self):
        self.n = 0

    def __call__(self, *_a, **_k):
        self.n += 1


def _count_queries(fn):
    counter = _Counter()
    event.listen(engine, "before_cursor_execute", counter)
    try:
        result = fn()
    finally:
        event.remove(engine, "before_cursor_execute", counter)
    return result, counter.n


def test_order_list_items_equal_order_detail(db):
    _org, _admin, client = _tenant(db, 4)
    page = client.get("/orders", params={"page_size": 100})
    assert page.status_code == 200, page.text
    items = page.json()["items"]
    assert len(items) == 4
    for item in items:
        detail = client.get(f"/orders/{item['id']}")
        assert detail.status_code == 200
        assert item == detail.json()
    assert any(i["invoice_id"] and i["payments"] for i in items)  # the preloaded paths were exercised
    assert all(i["stock_summary"] for i in items)


def test_order_list_query_count_does_not_grow_with_page_size(db):
    _, _, small = _tenant(db, 2)
    _, _, large = _tenant(db, 12)
    small.get("/orders", params={"page_size": 100})  # warm both caches
    large.get("/orders", params={"page_size": 100})
    _, n_small = _count_queries(lambda: small.get("/orders", params={"page_size": 100}))
    _, n_large = _count_queries(lambda: large.get("/orders", params={"page_size": 100}))
    assert n_large == n_small, (n_small, n_large)
    assert n_large <= 30


def test_entitlements_summary_matches_per_key_checks_and_new_overrides_apply_at_once(db):
    org, _admin, client = _tenant(db, 1)
    body = client.get("/organizations/me/entitlements").json()
    org = db.get(Organization, org.id)
    for key, allowed in body["features"].items():
        assert allowed == entitlement_service.has_entitlement(db, org, key), key
    for key, limit in body["limits"].items():
        assert limit == entitlement_service.get_effective_limit(db, org, key), key

    key = next(k for k, v in body["features"].items() if v)
    db.add(OrganizationFeatureOverride(id=str(uuid.uuid4()), organization_id=org.id, entitlement_key=key,
                                       effect="BLOCK", is_allowed=False))
    db.commit()
    after = client.get("/organizations/me/entitlements").json()
    assert after["features"][key] is False and after["active_feature_overrides_count"] == 1
    assert entitlement_service.has_entitlement(db, org, key) is False

    _, n = _count_queries(lambda: client.get("/organizations/me/entitlements"))
    assert n <= 4

    # A bulk delete (how "reset overrides" works) must clear the cache too.
    db.query(OrganizationFeatureOverride).filter(OrganizationFeatureOverride.organization_id == org.id).delete(
        synchronize_session=False)
    db.commit()
    assert client.get("/organizations/me/entitlements").json()["features"][key] is True


def test_admin_dashboard_cache_is_cleared_by_a_write(db):
    org, admin, client = _tenant(db, 2)
    first = client.get("/dashboard/admin").json()
    _, cached = _count_queries(lambda: client.get("/dashboard/admin"))
    assert cached <= 2  # served from the cache: only the signed-in user is looked up

    customer = db.query(Customer).filter(Customer.organization_id == org.id).first()
    product = db.query(Product).filter(Product.organization_id == org.id).first()
    order_service.place_order(db, db.get(User, admin.id), customer,
                              [order_service.OrderLine(product_id=product.id, quantity=1)],
                              warehouse_id=db.query(Warehouse).filter(Warehouse.organization_id == org.id).first().id)
    db.commit()
    second = client.get("/dashboard/admin").json()
    assert second["orders"]["total"] == first["orders"]["total"] + 1
    assert second["summary"]["period_sales"] > first["summary"]["period_sales"]
    dashboard_service.clear_dashboard_cache()
    assert client.get("/dashboard/admin").json() == second


def test_dashboard_cache_is_cleared_by_any_business_write_but_not_by_a_notification(db):
    from app.models import Notification, SalesReturn

    org, admin, client = _tenant(db, 1)
    client.get("/dashboard/admin")
    assert dashboard_service._dashboard_cache  # filled
    db.add(Notification(id=str(uuid.uuid4()), user_id=admin.id, organization_id=org.id, title="hi"))
    db.commit()
    assert dashboard_service._dashboard_cache  # a notification never changes a figure
    db.add(SalesReturn(id=str(uuid.uuid4()), organization_id=org.id, return_number=f"SR-{uuid.uuid4().hex[:6]}"))
    db.commit()
    assert not dashboard_service._dashboard_cache  # any business table clears it, listed or not


def test_read_requests_are_retried_once_after_a_dropped_connection():
    from sqlalchemy.exc import OperationalError

    from app.core.database import install_disconnect_retry

    mini = FastAPI()
    install_disconnect_retry(mini)
    calls = {"get": 0, "post": 0}

    def dropped():
        return OperationalError("SELECT 1", {}, Exception("server closed the connection"),
                                connection_invalidated=True)

    @mini.get("/read")
    def read():
        calls["get"] += 1
        if calls["get"] == 1:
            raise dropped()
        return {"ok": True}

    @mini.post("/write")
    def write():
        calls["post"] += 1
        raise dropped()

    client = TestClient(mini, raise_server_exceptions=False)
    assert client.get("/read").json() == {"ok": True} and calls["get"] == 2
    assert client.post("/write").status_code == 500 and calls["post"] == 1  # writes never run twice


def test_perf_log_line_counts_queries(caplog):
    from app.core import perf

    local_engine = create_engine("sqlite://")
    perf.install_query_counter(local_engine)
    mini = FastAPI()
    perf.install_middleware(mini)

    @mini.get("/two")
    def two():
        with local_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            conn.execute(text("SELECT 2"))
        return {"ok": True}

    with caplog.at_level(logging.INFO, logger="crm.perf"):
        assert TestClient(mini).get("/two?secret=1").status_code == 200
    [line] = [r.getMessage() for r in caplog.records if r.name == "crm.perf"]
    assert line.startswith("perf: GET /two 200 ") and " sql=2 " in line and "secret" not in line


def test_idle_ping_only_after_idle(tmp_path):
    from app.core.database import install_idle_ping

    local_engine = create_engine(f"sqlite:///{tmp_path / 'p.db'}")
    pings = []
    install_idle_ping(local_engine, idle_seconds=-1)  # always "idle": ping on every reuse

    @event.listens_for(local_engine, "checkout")
    def _seen(_dbapi, record, _proxy):  # noqa: ANN001
        pings.append(record.info.get("crm_checked_in_at") is not None)

    for _ in range(2):
        with local_engine.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar() == 1
    assert pings == [False, True]  # the reused connection was the one checked


@pytest.mark.parametrize("enabled", [True, False])
def test_startup_schema_steps_follow_run_startup_migrations(monkeypatch, enabled):
    import app.main as main
    from app.services import reminder_service

    calls = []
    monkeypatch.setattr(main.settings, "run_startup_migrations", enabled)
    # inline, so the steps have finished when on_startup() returns
    monkeypatch.setattr(main.settings, "startup_tasks_in_background", False)
    monkeypatch.setattr(main, "auto_add_missing_columns", lambda: calls.append("auto_add_missing_columns"))
    monkeypatch.setattr(main, "extend_pg_enum_types", lambda: calls.append("extend_pg_enum_types"))
    monkeypatch.setattr(reminder_service, "start_background_sweeps", lambda: False)
    main.on_startup()
    assert bool(calls) is enabled
