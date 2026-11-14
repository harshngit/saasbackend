"""QA retest 14 Nov:
1. POST /auth/login must never hang on a locked row: last_login is skipped when the user
   row is locked, any other lock wait ends in a clear 503 within seconds, and the startup
   schema steps take no table lock when there is nothing to change.
2. POST/PUT /supplier-invoices accept `invoice_date` (it used to be dropped -> dated today).
3. GET /dashboard/admin today_overall.due_amount.

The lock tests need PostgreSQL (row / table locks); they are skipped on SQLite.
"""

import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core import database  # noqa: E402
from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Customer, Invoice, Organization, Product, SalesOrder, SupplierInvoice, Supplier, User, Warehouse,
)
from app.models.enums import UserRole  # noqa: E402
from app.services import dashboard_service, entitlement_service  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)
PG = engine.dialect.name == "postgresql"
needs_pg = pytest.mark.skipif(not PG, reason="row/table locks need PostgreSQL")


@pytest.fixture(scope="module", autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)


@pytest.fixture(autouse=True)
def _no_plan_gates(monkeypatch):
    monkeypatch.setattr(entitlement_service, "check_entitlement", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "check_limit", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "has_entitlement", lambda *a, **k: True)
    dashboard_service._dashboard_cache.clear()


@pytest.fixture
def account():
    db = SessionLocal()
    try:
        org = Organization(name=f"Login {uuid.uuid4().hex[:6]}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}")
        db.add(org)
        db.flush()
        email = f"login.{uuid.uuid4().hex[:8]}@example.com"
        user = User(email=email, name="Login User", organization_id=org.id, system_role="admin", role=UserRole.ADMIN,
                    password_hash=hash_password("Password123!"), is_active=True)
        db.add(user)
        db.commit()
        return {"email": email, "user_id": user.id, "org_id": org.id,
                "headers": {"Authorization": f"Bearer {create_access_token(user.id, 'admin', org.id)}"}}
    finally:
        db.close()


def _login(email):
    started = time.perf_counter()
    r = client.post("/auth/login", json={"email": email, "password": "Password123!"})
    return r, time.perf_counter() - started


def _last_login(user_id):
    db = SessionLocal()
    try:
        return db.get(User, user_id).last_login
    finally:
        db.close()


# ------------------------------------------------------------------ 1. login never hangs


def test_login_works_normally_and_stamps_last_login(account):
    r, _ = _login(account["email"])
    assert r.status_code == 200 and r.json()["tokens"]["access_token"]
    assert _last_login(account["user_id"]) is not None


@needs_pg
def test_login_succeeds_fast_while_another_transaction_holds_the_user_row(account):
    """The live hang: an open transaction holding the user's row. The last_login UPDATE
    used to wait for it forever; now the stamp is skipped after 2 s and login succeeds."""
    holder = engine.connect()
    tx = holder.begin()
    try:
        holder.execute(text("UPDATE users SET name = name WHERE id = :id"), {"id": account["user_id"]})
        r, seconds = _login(account["email"])
    finally:
        tx.rollback()
        holder.close()
    assert r.status_code == 200, r.text
    assert seconds < 6
    assert _last_login(account["user_id"]) is None  # skipped, not waited for


@needs_pg
def test_any_other_lock_wait_on_login_is_a_clear_503_within_seconds(account):
    holder = engine.connect()
    tx = holder.begin()
    try:
        holder.execute(text("LOCK TABLE refresh_tokens IN ACCESS EXCLUSIVE MODE"))
        r, seconds = _login(account["email"])
    finally:
        tx.rollback()
        holder.close()
    assert r.status_code == 503 and "try again" in r.json()["detail"]
    assert seconds < 10
    r, _ = _login(account["email"])  # and the next login is fine
    assert r.status_code == 200


@needs_pg
def test_startup_schema_steps_take_no_lock_when_nothing_changes(account):
    """With another transaction open on `users`, the schema steps used to queue an
    ALTER TABLE / CREATE INDEX behind it -- and every write to users (login) behind that."""
    database.enforce_unique_email()  # make sure the indexes exist first (no lock held yet)
    database.enforce_unique_google_id()
    holder = engine.connect()
    tx = holder.begin()
    try:
        holder.execute(text("UPDATE users SET name = name WHERE id = :id"), {"id": account["user_id"]})
        started = time.perf_counter()
        steps = threading.Thread(target=lambda: (database.relax_not_null_columns(), database.widen_columns_to_text(),
                                                 database.enforce_unique_email(), database.enforce_unique_google_id()))
        steps.start()
        steps.join(15)
        steps_seconds = time.perf_counter() - started
        assert not steps.is_alive()
    finally:
        tx.rollback()
        holder.close()
    assert steps_seconds < 5  # nothing to change: no DDL, no lock wait
    with engine.connect() as c:
        assert c.execute(text("SELECT 1 FROM pg_locks l JOIN pg_class t ON t.oid = l.relation "
                              "WHERE t.relname = 'users' AND NOT l.granted")).first() is None


# ------------------------------------------------------------------ 2. supplier invoice date


def _received_purchase(headers, org_id):
    db = SessionLocal()
    try:
        wh = Warehouse(organization_id=org_id, name="Main", code=f"W{uuid.uuid4().hex[:4]}", is_default=True)
        sup = Supplier(organization_id=org_id, name=f"Supplier {uuid.uuid4().hex[:4]}", is_active=True)
        prod = Product(organization_id=org_id, name="Widget", sku=f"S-{uuid.uuid4().hex[:4]}", price=100.0,
                       total_inventory=0)
        db.add_all([wh, sup, prod])
        db.commit()
        wh_id, sup_id, prod_id = wh.id, sup.id, prod.id
    finally:
        db.close()
    pur = client.post("/purchases", headers=headers, json={
        "invoice_number": f"PO-{uuid.uuid4().hex[:6]}", "supplier_id": sup_id, "warehouse_id": wh_id,
        "items": [{"product_id": prod_id, "ordered_qty": 10, "purchase_price": 100.0}]}).json()
    assert client.post(f"/purchases/{pur['id']}/confirm", headers=headers).status_code == 200
    db = SessionLocal()
    try:  # the auto-created invoice would already bill the whole purchase
        for inv in db.query(SupplierInvoice).filter(SupplierInvoice.purchase_id == pur["id"]):
            inv_id = inv.id
    finally:
        db.close()
    client.post(f"/supplier-invoices/{inv_id}/cancel", headers=headers)
    grn = client.post("/grns", headers=headers, json={
        "purchase_id": pur["id"], "supplier_id": sup_id, "warehouse_id": wh_id,
        "items": [{"purchase_item_id": pur["items"][0]["id"], "product_id": prod_id, "received_qty": 10}]}).json()
    assert client.post(f"/grns/{grn['id']}/confirm", headers=headers).status_code == 200
    return pur, sup_id


def test_supplier_invoice_keeps_the_invoice_date_sent_on_create_and_update(account):
    headers = account["headers"]
    pur, sup_id = _received_purchase(headers, account["org_id"])
    r = client.post("/supplier-invoices", headers=headers, json={
        "supplier_id": sup_id, "purchase_id": pur["id"], "supplier_invoice_number": f"INV-{uuid.uuid4().hex[:5]}",
        "invoice_date": "2026-11-10", "due_date": "2026-12-10",
        "items": [{"purchase_item_id": pur["items"][0]["id"], "billed_qty": 10, "unit_price": 100.0}]})
    assert r.status_code == 201, r.text
    assert r.json()["supplier_invoice_date"].startswith("2026-11-10")
    assert r.json()["invoice_date"].startswith("2026-11-10")
    assert r.json()["due_date"].startswith("2026-12-10")

    r = client.put(f"/supplier-invoices/{r.json()['id']}", headers=headers, json={"invoice_date": "2026-11-08"})
    assert r.status_code == 200, r.text
    assert r.json()["supplier_invoice_date"].startswith("2026-11-08")
    # the original field name still works
    r = client.put(f"/supplier-invoices/{r.json()['id']}", headers=headers,
                   json={"supplier_invoice_date": "2026-11-07T00:00:00Z"})
    assert r.json()["supplier_invoice_date"].startswith("2026-11-07")


# ------------------------------------------------------------------ 3. due_amount


def test_today_overall_due_amount_is_the_unpaid_part_of_todays_orders(account):
    db = SessionLocal()
    try:
        cust = Customer(organization_id=account["org_id"], name="Shop")
        db.add(cust)
        db.flush()
        now = datetime.now(timezone.utc)
        orders = []
        for total in (1000.0, 500.0, 300.0):
            o = SalesOrder(organization_id=account["org_id"], order_number=f"SO-{uuid.uuid4().hex[:6]}",
                           customer_id=cust.id, status="placed", total=total, created_at=now)
            db.add(o)
            orders.append(o)
        db.flush()
        # 1000: invoiced, 400 paid -> 600 due; 500: invoiced and fully paid -> 0; 300: no invoice -> 300
        # a void invoice's payment does not count
        db.add_all([
            Invoice(organization_id=account["org_id"], invoice_number=f"I-{uuid.uuid4().hex[:5]}",
                    order_id=orders[0].id, total=1000.0, amount_paid=400.0, status="partial"),
            Invoice(organization_id=account["org_id"], invoice_number=f"I-{uuid.uuid4().hex[:5]}",
                    order_id=orders[1].id, total=500.0, amount_paid=500.0, status="paid"),
            Invoice(organization_id=account["org_id"], invoice_number=f"I-{uuid.uuid4().hex[:5]}",
                    order_id=orders[2].id, total=300.0, amount_paid=300.0, status="void"),
        ])
        db.commit()
        cust_id = cust.id
    finally:
        db.close()
    body = client.get("/dashboard/admin", headers=account["headers"]).json()
    assert body["today_overall"]["due_amount"] == 900.0
    filtered = client.get("/dashboard/admin", headers=account["headers"],
                          params={"customer_id": cust_id, "date_from": "2024-01-01", "date_to": "2024-01-31"}).json()
    assert filtered["today_overall"]["due_amount"] == 900.0
