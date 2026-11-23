"""GET /customers/{id}/payments/pending and /customers/{id}/payments/collected.

Payments, voids and delivery-collection reconciliation go through the real API;
invoices and credit notes are written directly (controlled totals, dates and statuses).
A multi-invoice allocation goes through payment_service.record -- the only path that
creates one (delivery-collection reconciliation calls it the same way).
"""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402

from app.core import business_time  # noqa: E402
from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Customer, CustomerPayment, DeliveryCollection, Invoice, Organization, Role, User,
)
from app.models.enums import UserRole  # noqa: E402
from app.services import entitlement_service, payment_service, role_service  # noqa: E402

client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)


@pytest.fixture(autouse=True)
def _no_plan_gates(monkeypatch):
    monkeypatch.setattr(entitlement_service, "check_entitlement", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "check_limit", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "has_entitlement", lambda *a, **k: True)


def _h(user_id, role, org_id):
    return {"Authorization": f"Bearer {create_access_token(user_id, role, org_id)}"}


def _firm(label="Pay"):
    db = SessionLocal()
    try:
        org = Organization(name=f"{label} {uuid.uuid4().hex[:6]}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}",
                           currency="INR")
        db.add(org)
        db.flush()
        role_service.seed_default_roles(db, org.id)

        def user(name, system_role, legacy, role_id=None):
            u = User(email=f"{name.replace(' ', '.').lower()}.{uuid.uuid4().hex[:8]}@example.com", name=name,
                     organization_id=org.id, system_role=system_role, role=legacy, role_id=role_id,
                     password_hash=hash_password("Password123!"), is_active=True)
            db.add(u)
            db.flush()
            return u

        roles = {r.name: r for r in db.query(Role).filter(Role.organization_id == org.id)}
        own_role = Role(organization_id=org.id, name="Collector (own)", data_scope="own",
                        permissions={"customers": {"view": True}, "payments": {"view": True}})
        db.add(own_role)
        db.flush()
        admin = user("Admin", "admin", UserRole.ADMIN)
        acct = user("Accountant", "staff", UserRole.ACCOUNTANT, roles["Accountant"].id)
        so = user("Sales Officer", "staff", UserRole.SALES_OFFICER, roles["Sales Officer"].id)
        own = user("Own Collector", "staff", UserRole.SALES_OFFICER, own_role.id)
        cust = Customer(organization_id=org.id, name="Shop One", payment_terms="net_30")
        other = Customer(organization_id=org.id, name="Shop Two")
        mine = Customer(organization_id=org.id, name="Shop Mine", assigned_sales_officer_id=own.id)
        db.add_all([cust, other, mine])
        db.commit()
        tz = business_time.org_timezone(db, org.id)
        return {
            "org": org.id, "cust": cust.id, "other": other.id, "mine": mine.id, "tz": tz,
            "admin": _h(admin.id, "admin", org.id), "acct": _h(acct.id, "staff", org.id),
            "so": _h(so.id, "staff", org.id), "own": _h(own.id, "staff", org.id),
        }
    finally:
        db.close()


@pytest.fixture
def firm():
    return _firm()


def _day(f, offset_days):
    today = business_time.today(f["tz"])
    return business_time.day_start(today + timedelta(days=offset_days), f["tz"])


def _invoice(f, total, *, customer=None, due_in=None, inv_offset=-1, status="unpaid", credit_for=None, number=None):
    db = SessionLocal()
    try:
        inv = Invoice(
            organization_id=f["org"], customer_id=customer or f["cust"],
            invoice_number=number or f"INV-{uuid.uuid4().hex[:6]}", invoice_date=_day(f, inv_offset),
            due_date=_day(f, due_in) if due_in is not None else None, status=status, total=total, amount_paid=0,
            is_credit_note=credit_for is not None, credit_note_for_invoice_id=credit_for,
        )
        db.add(inv)
        db.commit()
        return inv.id
    finally:
        db.close()


def _pay(f, amount, *, customer=None, invoice_id=None, headers=None, **extra):
    body = {"amount": amount, **extra}
    if invoice_id:
        body["invoice_id"] = invoice_id
    r = client.post(f"/customers/{customer or f['cust']}/payments", headers=headers or f["admin"], json=body)
    assert r.status_code == 201, r.text


def _pending(f, headers=None, customer=None, **params):
    return client.get(f"/customers/{customer or f['cust']}/payments/pending", headers=headers or f["admin"], params=params)


def _collected(f, headers=None, customer=None, **params):
    return client.get(f"/customers/{customer or f['cust']}/payments/collected", headers=headers or f["admin"], params=params)


# =============================================================================== pending
def test_pending_no_invoices(firm):
    body = _pending(firm).json()
    assert body["items"] == [] and body["total"] == 0 and body["total_pages"] == 0
    assert body["summary"]["total_outstanding"] == 0 and body["summary"]["pending_invoices"] == 0
    assert body["summary"]["payment_terms"] == "net_30" and body["summary"]["currency"] == "INR"


def test_pending_unpaid_partial_paid_cancelled_and_credit_note(firm):
    unpaid = _invoice(firm, 1000.0, due_in=10, number="INV-A-UNPAID")
    partial = _invoice(firm, 500.0, due_in=10, number="INV-B-PARTIAL")
    full = _invoice(firm, 300.0, due_in=10, number="INV-C-PAID")
    _invoice(firm, 999.0, due_in=10, status="cancelled", number="INV-D-CANCELLED")
    credited = _invoice(firm, 400.0, due_in=10, number="INV-E-CREDITED")
    _invoice(firm, 150.0, credit_for=credited, status="returned", number="CN-1")    # credit note: never a row
    wiped = _invoice(firm, 200.0, due_in=10, number="INV-F-CREDITED-FULL")
    _invoice(firm, 200.0, credit_for=wiped, status="returned", number="CN-2")
    _pay(firm, 200.0, invoice_id=partial)
    _pay(firm, 300.0, invoice_id=full)

    body = _pending(firm, sort_by="invoice_number", sort_order="asc").json()
    rows = {r["invoice_number"]: r for r in body["items"]}
    assert set(rows) == {"INV-A-UNPAID", "INV-B-PARTIAL", "INV-E-CREDITED"}
    assert rows["INV-A-UNPAID"]["outstanding"] == 1000.0 and rows["INV-A-UNPAID"]["payment_status"] == "unpaid"
    assert rows["INV-B-PARTIAL"]["amount_paid"] == 200.0 and rows["INV-B-PARTIAL"]["outstanding"] == 300.0
    assert rows["INV-B-PARTIAL"]["payment_status"] == "partial"
    assert rows["INV-E-CREDITED"]["credited_amount"] == 150.0 and rows["INV-E-CREDITED"]["outstanding"] == 250.0
    assert body["summary"]["total_outstanding"] == 1550.0 and body["summary"]["pending_invoices"] == 3
    assert unpaid in {r["invoice_id"] for r in body["items"]}


def test_pending_due_dates_overdue_and_filters(firm):
    late = _invoice(firm, 100.0, due_in=-5, inv_offset=-40, number="INV-LATE")
    _invoice(firm, 200.0, due_in=0, inv_offset=-2, number="INV-DUE-TODAY")
    _invoice(firm, 300.0, due_in=15, inv_offset=-1, number="INV-FUTURE")
    _invoice(firm, 400.0, due_in=None, inv_offset=-3, number="INV-NO-DUE")

    body = _pending(firm).json()
    # default order: earliest due date first, no due date last
    assert [r["invoice_number"] for r in body["items"]] == ["INV-LATE", "INV-DUE-TODAY", "INV-FUTURE", "INV-NO-DUE"]
    by = {r["invoice_number"]: r for r in body["items"]}
    assert by["INV-LATE"]["is_overdue"] is True and by["INV-LATE"]["days_overdue"] == 5
    assert by["INV-DUE-TODAY"]["is_overdue"] is False and by["INV-DUE-TODAY"]["days_overdue"] == 0
    assert by["INV-FUTURE"]["is_overdue"] is False and by["INV-NO-DUE"]["is_overdue"] is False
    assert body["summary"]["total_overdue"] == 100.0 and body["summary"]["overdue_invoices"] == 1

    only_late = _pending(firm, overdue="true").json()
    assert [r["invoice_id"] for r in only_late["items"]] == [late]
    assert only_late["summary"]["total_outstanding"] == 100.0       # summary follows the filters
    assert _pending(firm, overdue="false").json()["total"] == 3

    today = business_time.today(firm["tz"])
    by_due = _pending(firm, due_from=today.isoformat(), due_to=(today + timedelta(days=30)).isoformat()).json()
    assert {r["invoice_number"] for r in by_due["items"]} == {"INV-DUE-TODAY", "INV-FUTURE"}
    by_inv = _pending(firm, from_date=(today - timedelta(days=2)).isoformat(), to_date=today.isoformat()).json()
    assert {r["invoice_number"] for r in by_inv["items"]} == {"INV-DUE-TODAY", "INV-FUTURE"}
    assert _pending(firm, search="future").json()["items"][0]["invoice_number"] == "INV-FUTURE"
    assert _pending(firm, search="%").json()["total"] == 0          # LIKE wildcards are literal
    assert _pending(firm, payment_status="partial").json()["total"] == 0

    assert _pending(firm, from_date="17-11-2026").status_code == 400
    assert _pending(firm, from_date="2026-11-20", to_date="2026-11-01").status_code == 400
    assert _pending(firm, payment_status="paid").status_code == 400
    assert _pending(firm, sort_by="secret_column").status_code == 400
    assert _pending(firm, page=0).status_code == 422 and _pending(firm, page_size=101).status_code == 422


def test_pending_pagination_is_stable(firm):
    for i in range(7):
        _invoice(firm, 10.0 + i, due_in=3, number=f"INV-P{i}")      # same due date: id breaks the tie
    pages = [_pending(firm, page=p, page_size=3).json() for p in (1, 2, 3)]
    assert [len(p["items"]) for p in pages] == [3, 3, 1] and pages[0]["total_pages"] == 3
    ids = [r["invoice_id"] for p in pages for r in p["items"]]
    assert len(ids) == len(set(ids)) == 7
    assert all(p["summary"]["pending_invoices"] == 7 and p["summary"]["total_outstanding"] == 91.0 for p in pages)


def test_pending_multi_invoice_allocation_counts_once(firm):
    a = _invoice(firm, 100.0, due_in=5, number="INV-M1")
    b = _invoice(firm, 100.0, due_in=5, number="INV-M2")
    db = SessionLocal()
    try:
        payment_service.record(db, firm["org"], customer=db.get(Customer, firm["cust"]), amount=150.0,
                               allocations=[{"invoice_id": a, "amount": 100.0}, {"invoice_id": b, "amount": 50.0}])
        db.commit()
    finally:
        db.close()
    body = _pending(firm).json()
    assert [(r["invoice_number"], r["outstanding"]) for r in body["items"]] == [("INV-M2", 50.0)]
    assert body["summary"]["total_outstanding"] == 50.0


# ============================================================================= collected
def test_collected_no_payments(firm):
    body = _collected(firm).json()
    assert body["items"] == [] and body["summary"]["total_collected"] == 0 and body["summary"]["payment_count"] == 0


def test_collected_payments_allocations_and_summary(firm):
    inv1 = _invoice(firm, 500.0, due_in=5, number="INV-C1")
    inv2 = _invoice(firm, 300.0, due_in=5, number="INV-C2")
    inv3 = _invoice(firm, 200.0, due_in=5, number="INV-C3")
    _pay(firm, 200.0, invoice_id=inv1, payment_mode="upi", reference="UTR-111")      # partial, one invoice
    _pay(firm, 75.5, payment_mode="cash")                                              # advance / on account
    db = SessionLocal()
    try:  # one payment over two invoices, with 20 left on account
        payment_service.record(db, firm["org"], customer=db.get(Customer, firm["cust"]), amount=420.0,
                               payment_mode="bank_transfer", reference="NEFT-9",
                               allocations=[{"invoice_id": inv2, "amount": 300.0}, {"invoice_id": inv3, "amount": 100.0}])
        db.commit()
    finally:
        db.close()

    body = _collected(firm).json()
    assert body["total"] == 3 and len(body["items"]) == 3                # one row per payment, never per allocation
    by_ref = {r["reference"]: r for r in body["items"]}
    multi = by_ref["NEFT-9"]
    assert multi["allocated_amount"] == 400.0 and multi["unallocated_amount"] == 20.0
    assert sorted((a["invoice_number"], a["amount"]) for a in multi["allocations"]) == [("INV-C2", 300.0), ("INV-C3", 100.0)]
    single = by_ref["UTR-111"]
    assert single["allocations"] == [{"invoice_id": inv1, "invoice_number": "INV-C1", "amount": 200.0}]
    assert single["allocated_amount"] == 200.0 and single["unallocated_amount"] == 0
    advance = by_ref[None]
    assert advance["allocations"] == [] and advance["unallocated_amount"] == 75.5
    assert all(r["status"] == "recorded" and r["source"] == "direct" and r["receipt_number"] for r in body["items"])
    s = body["summary"]
    assert s == {"total_collected": 695.5, "payment_count": 3, "total_allocated": 600.0,
                 "total_unallocated": 95.5, "currency": "INR",
                 "awaiting_approval_amount": 0.0, "awaiting_approval_count": 0}

    # filters: mode, search (receipt / reference / invoice number), status, dates
    assert [r["reference"] for r in _collected(firm, payment_mode="UPI").json()["items"]] == ["UTR-111"]
    assert _collected(firm, search="neft").json()["total"] == 1
    assert _collected(firm, search="INV-C3").json()["items"][0]["reference"] == "NEFT-9"      # via an allocation
    assert _collected(firm, search="INV-C1").json()["items"][0]["reference"] == "UTR-111"     # via invoice_id
    receipt = single["receipt_number"]
    assert _collected(firm, search=receipt).json()["items"][0]["payment_id"] == single["payment_id"]
    assert _collected(firm, payment_status="recorded").json()["total"] == 3
    assert _collected(firm, payment_status="voided").status_code == 400
    today = business_time.today(firm["tz"]).isoformat()
    assert _collected(firm, from_date=today, to_date=today).json()["total"] == 3
    yesterday = (business_time.today(firm["tz"]) - timedelta(days=1)).isoformat()
    assert _collected(firm, to_date=yesterday).json()["summary"]["payment_count"] == 0
    asc = _collected(firm, sort_by="amount", sort_order="asc").json()["items"]
    assert [r["amount"] for r in asc] == [75.5, 200.0, 420.0]


def test_collected_decimal_sums_do_not_drift(firm):
    for _ in range(3):
        _pay(firm, 0.1)
    _pay(firm, 0.2)
    s = _collected(firm).json()["summary"]
    assert s["total_collected"] == 0.5 and s["total_unallocated"] == 0.5


def test_collected_voided_payment_is_not_listed(firm):
    _pay(firm, 50.0)
    _pay(firm, 70.0)
    pay_id = _collected(firm, sort_by="amount", sort_order="asc").json()["items"][0]["payment_id"]
    assert client.delete(f"/customers/{firm['cust']}/payments/{pay_id}", headers=firm["admin"]).status_code == 200
    body = _collected(firm).json()
    assert [r["amount"] for r in body["items"]] == [70.0] and body["summary"]["total_collected"] == 70.0


def test_delivery_collection_appears_only_after_reconciliation_and_once(firm):
    db = SessionLocal()
    try:
        coll = DeliveryCollection(organization_id=firm["org"], customer_id=firm["cust"], amount=250.0,
                                  payment_mode="cash", reference="COD-1", reconciliation_status="recorded")
        db.add(coll)
        db.commit()
        coll_id = coll.id
    finally:
        db.close()
    assert _collected(firm).json()["total"] == 0                       # recorded only: not a payment yet
    # (the older GET /payments still shows it, as awaiting reconciliation)
    old = client.get(f"/customers/{firm['cust']}/payments", headers=firm["admin"]).json()
    assert [r["status"] for r in old] == ["awaiting_reconciliation"]

    # Admin: since 076b116 reconcile also needs Deliveries > Approve, which the built-in Accountant lacks.
    r = client.post(f"/deliveries/collections/{coll_id}/reconcile", headers=firm["admin"])
    assert r.status_code == 200, r.text
    body = _collected(firm).json()
    assert body["total"] == 1 and body["items"][0]["source"] == "delivery_collection"
    assert body["items"][0]["amount"] == 250.0 and body["items"][0]["reference"] == "COD-1"
    assert body["summary"]["total_collected"] == 250.0


def test_other_customer_and_other_organization_are_isolated(firm):
    _invoice(firm, 100.0, due_in=5, customer=firm["other"])
    _pay(firm, 33.0, customer=firm["other"])
    assert _pending(firm).json()["total"] == 0 and _collected(firm).json()["total"] == 0
    assert _pending(firm, customer=firm["other"]).json()["total"] == 1
    assert _collected(firm, customer=firm["other"]).json()["summary"]["total_collected"] == 33.0

    stranger = _firm("Stranger")
    _invoice(stranger, 900.0, due_in=5)
    _pay(stranger, 90.0)
    for call in (_pending, _collected):
        r = call(firm, customer=stranger["cust"])                      # another firm's customer id
        assert r.status_code == 404, r.text
        assert call(firm, customer=str(uuid.uuid4())).status_code == 404


def test_staff_and_admin_authorization(firm):
    _invoice(firm, 100.0, due_in=5)
    _pay(firm, 10.0)
    for call in (_pending, _collected):
        assert call(firm, headers=firm["admin"]).status_code == 200
        assert call(firm, headers=firm["acct"]).status_code == 200          # Accountant: payments view, all data
        assert call(firm, headers=firm["so"]).status_code == 403            # Sales Officer: no payments permission
        assert call(firm, headers=firm["own"]).status_code == 404           # own scope, not their customer
        assert call(firm, headers=firm["own"], customer=firm["mine"]).status_code == 200
        assert client.get(f"/customers/{firm['cust']}/payments/pending").status_code in (401, 403)


def test_existing_payment_endpoints_unchanged(firm):
    inv = _invoice(firm, 100.0, due_in=5)
    _pay(firm, 40.0, invoice_id=inv)
    rows = client.get(f"/customers/{firm['cust']}/payments", headers=firm["admin"]).json()
    assert len(rows) == 1 and rows[0]["amount"] == 40.0 and rows[0]["invoice_id"] == inv
    ledger = client.get(f"/customers/{firm['cust']}/ledger", headers=firm["admin"])
    assert ledger.status_code == 200
    db = SessionLocal()
    try:
        assert db.get(Invoice, inv).amount_paid == 40.0 and db.get(Invoice, inv).status == "partial"
        assert db.query(CustomerPayment).filter(CustomerPayment.customer_id == firm["cust"]).count() == 1
    finally:
        db.close()


def test_query_count_does_not_grow_with_the_page(firm):
    from sqlalchemy import event

    invoices = [_invoice(firm, 100.0, due_in=5) for _ in range(8)]
    for inv in invoices:
        _pay(firm, 10.0, invoice_id=inv, payment_mode="split",
             splits=[{"payment_mode": "cash", "amount": 4.0}, {"payment_mode": "upi", "amount": 6.0}])
    counts = {}
    for size in (2, 8):
        statements = []

        def _count(conn, cursor, statement, *args):  # noqa: ANN001
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", _count)
        try:
            pend = _pending(firm, page_size=size).json()
            coll = _collected(firm, page_size=size).json()
        finally:
            event.remove(engine, "before_cursor_execute", _count)
        assert len(pend["items"]) == size and len(coll["items"]) == size
        assert all(len(r["splits"]) == 2 and len(r["allocations"]) == 1 for r in coll["items"])
        counts[size] = len(statements)
    assert counts[2] == counts[8], counts


# ------------------------------------------- collections waiting for admin approval
def _submitted(f, amount, *, invoice_id=None, reference=None, mode="cash"):
    db = SessionLocal()
    try:
        coll = DeliveryCollection(organization_id=f["org"], customer_id=f["cust"], amount=amount,
                                  payment_mode=mode, reference=reference, reconciliation_status="recorded")
        db.add(coll)
        db.flush()
        if invoice_id:
            from app.models import DeliveryCollectionAllocation
            db.add(DeliveryCollectionAllocation(organization_id=f["org"], delivery_collection_id=coll.id,
                                                invoice_id=invoice_id, amount=amount))
        db.commit()
        return coll.id
    finally:
        db.close()


def test_awaiting_approval_list_and_summaries(firm):
    inv = _invoice(firm, 1000.0, due_in=5)
    with SessionLocal() as s:                                  # the invoice is on the customer's balance
        cust = s.get(Customer, firm["cust"])
        cust.total_billed = 1000.0
        cust.recompute_outstanding()
        s.commit()
    first = _submitted(firm, 300.0, invoice_id=inv, reference="COD-7")
    _submitted(firm, 200.0, mode="upi")

    # Default list unchanged: no payment yet. Summary tells what is waiting.
    body = _collected(firm).json()
    assert body["total"] == 0
    assert body["summary"]["awaiting_approval_amount"] == 500.0 and body["summary"]["awaiting_approval_count"] == 2

    body = _collected(firm, payment_status="awaiting_approval").json()
    assert body["total"] == 2 and body["summary"]["total_collected"] == 500.0
    row = next(r for r in body["items"] if r["collection_id"] == first)
    assert row["status"] == "awaiting_approval" and row["source"] == "delivery_collection"
    assert row["payment_id"] == first and row["receipt_number"] is None and row["reference"] == "COD-7"
    assert row["allocations"] == [{"invoice_id": inv, "invoice_number": row["allocations"][0]["invoice_number"],
                                   "amount": 300.0}]
    assert row["allocated_amount"] == 300.0 and row["unallocated_amount"] == 0.0
    assert _collected(firm, payment_status="awaiting_approval", payment_mode="UPI").json()["total"] == 1
    assert _collected(firm, payment_status="awaiting_approval", search="COD-7").json()["total"] == 1

    pend = _pending(firm).json()["summary"]
    assert pend["awaiting_approval_amount"] == 500.0 and pend["awaiting_approval_count"] == 2
    assert pend["collectable_amount"] == 500.0                   # outstanding 1000 - 500 waiting

    # Approved: leaves the waiting list, appears once as a payment.
    assert client.post(f"/deliveries/collections/{first}/reconcile", headers=firm["admin"]).status_code == 200
    assert _collected(firm, payment_status="awaiting_approval").json()["total"] == 1
    body = _collected(firm).json()
    assert body["total"] == 1 and body["items"][0]["source"] == "delivery_collection"
    assert body["summary"]["awaiting_approval_amount"] == 200.0


def test_unknown_payment_status_still_400(firm):
    r = _collected(firm, payment_status="pending")
    assert r.status_code == 400 and "awaiting_approval" in r.json()["detail"]


def test_awaiting_approval_is_scoped_to_the_customer_and_firm(firm):
    other_firm = _firm("Other")
    _submitted(firm, 50.0)
    assert _collected(firm, customer=firm["other"], payment_status="awaiting_approval").json()["total"] == 0
    r = client.get(f"/customers/{firm['cust']}/payments/collected", headers=other_firm["admin"],
                   params={"payment_status": "awaiting_approval"})
    assert r.status_code == 404


# ------------------------- one place for a collection: Pending (still collectable) or Completed
def _balance(f, customer, billed):
    with SessionLocal() as s:
        c = s.get(Customer, customer)
        c.total_billed = billed
        c.recompute_outstanding()
        s.commit()


def test_pending_can_leave_out_invoices_fully_awaiting_approval(firm):
    full = _invoice(firm, 400.0, number="INV-FULL")
    part = _invoice(firm, 1000.0, number="INV-PART")
    untouched = _invoice(firm, 250.0, number="INV-NONE")
    _submitted(firm, 400.0, invoice_id=full)                   # whole invoice collected, waiting
    _submitted(firm, 300.0, invoice_id=part)                   # part collected, waiting

    rows = {r["invoice_number"]: r for r in _pending(firm).json()["items"]}
    assert set(rows) == {"INV-FULL", "INV-PART", "INV-NONE"}   # default unchanged
    assert (rows["INV-FULL"]["outstanding"], rows["INV-FULL"]["awaiting_approval_amount"],
            rows["INV-FULL"]["collectable_outstanding"]) == (400.0, 400.0, 0.0)
    assert (rows["INV-PART"]["awaiting_approval_amount"], rows["INV-PART"]["collectable_outstanding"]) == (300.0, 700.0)
    assert (rows["INV-NONE"]["awaiting_approval_amount"], rows["INV-NONE"]["collectable_outstanding"]) == (0.0, 250.0)

    body = _pending(firm, exclude_awaiting_approval="true").json()
    assert [r["invoice_number"] for r in body["items"]] and {r["invoice_number"] for r in body["items"]} == {"INV-PART", "INV-NONE"}
    assert body["total"] == 2 and body["summary"]["pending_invoices"] == 2
    # The fully-collected invoice is in Completed (waiting for approval) only.
    waiting = _collected(firm, payment_status="awaiting_approval").json()["items"]
    assert {a["invoice_id"] for w in waiting for a in w["allocations"]} == {full, part}
    assert untouched not in {a["invoice_id"] for w in waiting for a in w["allocations"]}


def test_customer_list_has_collectable_filter(firm):
    _balance(firm, firm["cust"], 500.0)                        # owes 500, 500 collected -> nothing left
    _balance(firm, firm["other"], 800.0)                       # owes 800, 300 collected -> 500 left
    _submitted(firm, 500.0)
    with SessionLocal() as s:
        s.add(DeliveryCollection(organization_id=firm["org"], customer_id=firm["other"], amount=300.0,
                                 payment_mode="cash", reconciliation_status="recorded"))
        s.commit()
    other_firm = _firm("Other")
    _balance(other_firm, other_firm["cust"], 900.0)

    def ids(**params):
        r = client.get("/customers", headers=firm["admin"], params={"page_size": 100, **params})
        assert r.status_code == 200, r.text
        return {c["id"]: c for c in r.json()["items"]}

    assert {firm["cust"], firm["other"]} <= set(ids(has_outstanding="true"))   # old filter unchanged
    yes = ids(has_collectable="true")
    assert firm["other"] in yes and firm["cust"] not in yes and other_firm["cust"] not in yes
    assert yes[firm["other"]]["collectable_amount"] == 500.0
    no = ids(has_collectable="false")
    assert firm["cust"] in no and firm["other"] not in no
    assert no[firm["cust"]]["collection_status"] == "pending_approval"
