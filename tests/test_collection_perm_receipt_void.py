"""1. Recording a field collection needs Deliveries > Create or Payments > Create (Admins bypass).
2. Voiding a receipt keeps it: the payment is still removed from every total / balance, but a
   copy (receipt number and all) is kept and listed as receipt_status "void" with
   GET /payment-receipts?include_voided=true. Two firms.
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
    Customer, CustomerPayment, Delivery, Organization, Role, User, VoidedCustomerPayment,
)
from app.models.enums import UserRole  # noqa: E402
from app.routers.deliveries import COLLECTION_CREATE_DETAIL  # noqa: E402
from app.services import entitlement_service, payment_service, role_service  # noqa: E402

c = TestClient(app)
ROUTES = ("/customer-payments/collections", "/deliveries/collections/customer")


@pytest.fixture(scope="module", autouse=True)
def _schema():
    Base.metadata.create_all(engine)


@pytest.fixture(autouse=True)
def _no_plan_gates(monkeypatch):
    monkeypatch.setattr(entitlement_service, "check_entitlement", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "check_limit", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "has_entitlement", lambda *a, **k: True)


def _firm():
    with SessionLocal() as db:
        org = Organization(name=f"PV {uuid.uuid4().hex[:6]}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}",
                           currency="INR")
        db.add(org)
        db.flush()
        role_service.seed_default_roles(db, org.id)
        seeded = {r.name: r for r in db.query(Role).filter(Role.organization_id == org.id)}
        custom = {
            "none": Role(organization_id=org.id, name="No rights", data_scope="own", permissions={}),
            "trimmed_dp": Role(organization_id=org.id, name="DP trimmed", data_scope="own",
                               permissions={"deliveries": {"view": True, "create": True, "edit": True}}),
            "view_only": Role(organization_id=org.id, name="View only", permissions={
                "deliveries": {"view": True}, "payments": {"view": True}, "customers": {"view": True}}),
        }
        db.add_all(custom.values())
        db.flush()
        h, ids = {}, {}

        def user(key, role_id, legacy, system_role="staff"):
            u = User(email=f"{key}.{uuid.uuid4().hex[:8]}@example.com", name=key, organization_id=org.id,
                     system_role=system_role, role=legacy, role_id=role_id,
                     password_hash=hash_password("Password123!"), is_active=True)
            db.add(u)
            db.flush()
            h[key] = {"Authorization": f"Bearer {create_access_token(u.id, system_role, org.id)}"}
            ids[key] = u.id

        user("admin", None, UserRole.ADMIN, "admin")
        user("none", custom["none"].id, UserRole.SALES_OFFICER)
        user("trimmed_dp", custom["trimmed_dp"].id, UserRole.DELIVERY_PARTNER)
        user("view_only", custom["view_only"].id, UserRole.ACCOUNTANT)
        user("accountant", seeded["Accountant"].id, UserRole.ACCOUNTANT)
        user("dp", seeded["Delivery Partner"].id, UserRole.DELIVERY_PARTNER)
        user("legacy_dp", None, UserRole.DELIVERY_PARTNER)          # no role assigned: legacy default matrix
        user("legacy_so", None, UserRole.SALES_OFFICER)
        cust = Customer(organization_id=org.id, name="Shop", opening_balance=5000.0, outstanding_balance=5000.0)
        db.add(cust)
        db.flush()
        dlv = Delivery(organization_id=org.id, customer_id=cust.id, delivery_note_number=f"DO-{uuid.uuid4().hex[:8]}",
                       delivery_partner_id=ids["dp"], status="pending")
        db.add(dlv)
        db.commit()
        return {"org": org.id, "cust": cust.id, "dlv": dlv.id, "h": h, "ids": ids}


def _collect(f, who, amount=10.0, route=ROUTES[0], **extra):
    return c.post(route, headers=f["h"][who],
                  json={"customer_id": f["cust"], "amount": amount, "payment_method": "cash", **extra})


# ------------------------------------------------------------------ 1. collection permission
def test_collection_needs_deliveries_or_payments_create():
    f = _firm()
    for route in ROUTES:
        for who in ("none", "view_only", "legacy_so"):
            r = _collect(f, who, route=route)
            assert r.status_code == 403 and r.json()["detail"] == COLLECTION_CREATE_DETAIL, (who, route, r.text)
        for who in ("trimmed_dp", "accountant", "admin", "dp", "legacy_dp"):
            r = _collect(f, who, route=route)
            assert r.status_code == 201, (who, route, r.text)
            assert r.json()["delivery_partner_id"] == f["ids"][who]
    # Someone else's delivery: unchanged 403.
    r = _collect(f, "trimmed_dp", delivery_id=f["dlv"])
    assert r.status_code == 403 and r.json()["detail"] == "Cannot record collection for a delivery not assigned to you"
    # Other firm's customer: 404 even with rights.
    g = _firm()
    r = c.post(ROUTES[0], headers=f["h"]["accountant"], json={"customer_id": g["cust"], "amount": 5, "payment_method": "cash"})
    assert r.status_code == 404


# ------------------------------------------------------------------------ 2. receipt void
def _balance(cust_id):
    with SessionLocal() as s:
        cu = s.get(Customer, cust_id)
        return round(cu.outstanding_balance or 0, 2), round(cu.total_received or 0, 2)


def _register(f, **params):
    r = c.get("/payment-receipts", headers=f["h"]["admin"], params={"page_size": 100, **params})
    assert r.status_code == 200, r.text
    return r.json()


def test_reconcile_then_void_keeps_the_receipt_number_and_totals():
    f = _firm()
    before = _balance(f["cust"])
    coll = _collect(f, "dp", amount=300.0).json()["id"]
    assert c.post(f"/deliveries/collections/{coll}/reconcile", headers=f["h"]["admin"]).status_code == 200
    with SessionLocal() as s:
        pid = s.query(CustomerPayment).filter(CustomerPayment.organization_id == f["org"]).one().id
        number = s.get(CustomerPayment, pid).receipt_number
    assert _balance(f["cust"]) == (round(before[0] - 300, 2), round(before[1] + 300, 2))
    default_before = _register(f)
    assert [i["receipt_number"] for i in default_before["items"]] == [number]
    assert default_before["items"][0]["receipt_status"] == "active"

    assert c.post(f"/deliveries/collections/{coll}/void", headers=f["h"]["admin"]).status_code == 200
    assert _balance(f["cust"]) == before                             # totals as if it never happened
    with SessionLocal() as s:
        assert s.get(CustomerPayment, pid) is None
        kept = s.get(VoidedCustomerPayment, pid)
        assert kept.receipt_number == number and kept.amount == 300.0 and kept.void_source == "delivery_collection"
        assert kept.voided_by_id == f["ids"]["admin"]
    assert _register(f)["total"] == 0                                # default list unchanged: live receipts only
    reg = _register(f, include_voided="true")
    assert reg["total"] == 1
    row = reg["items"][0]
    assert (row["receipt_number"], row["receipt_status"], row["amount_received"]) == (number, "void", 300.0)
    assert row["voided_at"] is not None
    collected = c.get(f"/customers/{f['cust']}/payments/collected", headers=f["h"]["admin"]).json()
    assert collected["total"] == 0 and collected["summary"]["total_collected"] == 0.0

    # Numbering continues after the kept number; the void is listed next to the new one.
    coll2 = _collect(f, "dp", amount=50.0).json()["id"]
    assert c.post(f"/deliveries/collections/{coll2}/reconcile", headers=f["h"]["admin"]).status_code == 200
    reg = _register(f, include_voided="true")
    assert [(i["receipt_status"]) for i in reg["items"]] == ["active", "void"]
    seq = lambda n: int(n.rsplit("-", 1)[1])  # noqa: E731
    assert seq(reg["items"][0]["receipt_number"]) == seq(number) + 1

    # Another firm sees none of it.
    g = _firm()
    assert _register(g, include_voided="true")["total"] == 0


def test_receipt_and_customer_payment_voids_are_kept_too():
    f = _firm()
    r = c.post(f"/customers/{f['cust']}/payments", headers=f["h"]["admin"], json={"amount": 40.0})
    assert r.status_code == 201, r.text                          # returns the customer
    with SessionLocal() as s:
        pid = s.query(CustomerPayment).filter(CustomerPayment.customer_id == f["cust"]).one().id
    assert c.delete(f"/customers/{f['cust']}/payments/{pid}", headers=f["h"]["admin"]).status_code in (200, 204)
    r = c.post("/payment-receipts", headers=f["h"]["admin"],
               json={"customer_id": f["cust"], "amount_received": 60.0, "payment_method": "cash"})
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert c.delete(f"/payment-receipts/{rid}", headers=f["h"]["admin"]).status_code == 204
    with SessionLocal() as s:
        assert {v.void_source for v in s.query(VoidedCustomerPayment).filter(
            VoidedCustomerPayment.organization_id == f["org"])} == {"customer_payment", "payment_receipt"}
    assert _balance(f["cust"]) == (5000.0, 0.0)


def test_without_the_table_void_behaves_as_before(monkeypatch):
    f = _firm()
    monkeypatch.setattr(payment_service, "voided_archive_available", lambda db: False)
    coll = _collect(f, "dp", amount=20.0).json()["id"]
    assert c.post(f"/deliveries/collections/{coll}/reconcile", headers=f["h"]["admin"]).status_code == 200
    assert c.post(f"/deliveries/collections/{coll}/void", headers=f["h"]["admin"]).status_code == 200
    with SessionLocal() as s:
        assert s.query(VoidedCustomerPayment).filter(VoidedCustomerPayment.organization_id == f["org"]).count() == 0
    assert _register(f, include_voided="true")["total"] == 0          # falls back to the plain list
