"""scripts/data_audit.py and scripts/fix_delivery_customers.py on an in-memory database."""

import csv
import importlib.util
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core.database import Base  # noqa: E402
from app.models import ActivityLog, Customer, Delivery, Organization, SalesOrder  # noqa: E402


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses need the module registered
    spec.loader.exec_module(module)
    return module


audit = _load("data_audit")
fix = _load("fix_delivery_customers")


@pytest.fixture
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    event.listen(engine, "connect", lambda c, _r: c.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def _org(db, name):
    o = Organization(name=name, company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}")
    db.add(o)
    db.flush()
    return o


def _customer(db, org, code):
    c = Customer(organization_id=org.id, name=f"Customer {code}", customer_id=code)
    db.add(c)
    db.flush()
    return c


def _order(db, org, status, customer=None):
    o = SalesOrder(organization_id=org.id, order_number=f"SO-{uuid.uuid4().hex[:6]}", status=status,
                   customer_id=customer.id if customer else None)
    db.add(o)
    db.flush()
    return o


def _delivery(db, org, number, status="planned", order=None, customer=None):
    d = Delivery(organization_id=org.id, delivery_note_number=number, status=status,
                 sales_order_id=order.id if order else None, customer_id=customer.id if customer else None)
    db.add(d)
    db.flush()
    return d


@pytest.fixture
def world(db):
    a, b = _org(db, "Firm A"), _org(db, "Firm B")
    w = {"a": a, "b": b, "ca": _customer(db, a, "CS-0001"), "ca2": _customer(db, a, "CS-0002"),
         "cb": _customer(db, b, "CS-0001")}
    cancelled, live = _order(db, a, "cancelled", w["ca"]), _order(db, a, "processing", w["ca"])
    w["deliveries"] = {
        # category (A): cancelled order, goods moved
        "in_transit": _delivery(db, a, "DLV-1003", "in_transit", cancelled, w["ca"]),
        "loaded": _delivery(db, a, "DLV-1004", "loaded", cancelled, w["ca"]),
        "delivered": _delivery(db, a, "DLV-1005", "delivered", cancelled, w["ca"]),
        # cancelled order but nothing moved: not reported
        **{s: _delivery(db, a, f"DLV-2{i}", s, cancelled, w["ca"])
           for i, s in enumerate(("cancelled", "planned", "accepted", "ready", "rejected"))},
        # live order in transit: not reported
        "live": _delivery(db, a, "DLV-3000", "in_transit", live, w["ca"]),
        # category (B)
        "orphan_a": _delivery(db, a, "DLV-9001"),
        "orphan_a2": _delivery(db, a, "DLV-9002", "in_transit"),
        "orphan_b": _delivery(db, b, "DLV-9001"),  # same number in another firm
        "only_customer": _delivery(db, a, "DLV-9003", customer=w["ca"]),
        "only_order": _delivery(db, a, "DLV-9004", order=live),
    }
    db.commit()
    return w


# ------------------------------------------------------------------ audit


def test_audit_categories_and_csv_files(db, world, tmp_path):
    counts = audit.run_audit(db, tmp_path)
    with (tmp_path / audit.FILE_A).open(encoding="utf-8") as fh:
        a = list(csv.DictReader(fh))
    with (tmp_path / audit.FILE_B).open(encoding="utf-8") as fh:
        b = list(csv.DictReader(fh))
    assert {r["delivery_note_number"] for r in a} == {"DLV-1003", "DLV-1004", "DLV-1005"}
    assert all(r["order_status"] == "cancelled" and r["customer_number"] == "CS-0001" for r in a)
    assert {(r["delivery_note_number"], r["organization_name"]) for r in b} == {
        ("DLV-9001", "Firm A"), ("DLV-9002", "Firm A"), ("DLV-9001", "Firm B")}
    assert set(b[0]) == set(audit.COLUMNS_B) and "scheduled_date" in b[0] and b[0]["created_at"]
    assert counts == {audit.FILE_A: 3, audit.FILE_B: 3}
    # one organization only
    audit.run_audit(db, tmp_path / "b", world["b"].id)
    with (tmp_path / "b" / audit.FILE_B).open(encoding="utf-8") as fh:
        assert [r["organization_name"] for r in csv.DictReader(fh)] == ["Firm B"]


def test_audit_writes_nothing(db, world, tmp_path):
    before = [(d.id, d.status, d.customer_id, d.sales_order_id) for d in db.query(Delivery).order_by(Delivery.id)]
    audit.run_audit(db, tmp_path)
    db.expire_all()
    assert [(d.id, d.status, d.customer_id, d.sales_order_id) for d in db.query(Delivery).order_by(Delivery.id)] == before
    assert not db.new and not db.dirty


# ------------------------------------------------------------------ fix script


def _state(db):
    db.expire_all()
    return {d.id: (d.customer_id, d.sales_order_id) for d in db.query(Delivery)}


def test_dry_run_plan_changes_nothing_and_explains_refusals(db, world):
    rows = [
        {"delivery_note_number": "DLV-9002", "customer_number": "CS-0002"},   # change
        {"delivery_note_number": "DLV-9001", "customer_number": "CS-0001"},   # ambiguous (2 firms)
        {"delivery_note_number": "DLV-9003", "customer_number": "CS-0002"},   # has another customer
        {"delivery_note_number": "DLV-9004", "customer_number": "CS-0001"},   # has an order
        {"delivery_note_number": "DLV-9003", "customer_number": "CS-0001"},   # listed twice
        {"delivery_note_number": "NOPE", "customer_number": "CS-0001"},       # unknown delivery
        {"delivery_note_number": "DLV-9001", "customer_number": "CS-9999",    # unknown customer
         "organization_id": world["b"].id},
        {"delivery_note_number": "", "customer_number": "CS-0001"},           # blank
    ]
    before = _state(db)
    plan = fix.build_plan(db, rows)
    outcomes = [(r.delivery_note_number, r.outcome) for r in plan.rows]
    assert outcomes == [("DLV-9002", "change"), ("DLV-9001", "refused"), ("DLV-9003", "refused"),
                        ("DLV-9004", "refused"), ("DLV-9003", "refused"), ("NOPE", "refused"),
                        ("DLV-9001", "refused"), ("", "refused")]
    reasons = " | ".join(r.reason for r in plan.refused)
    assert "2 deliveries have this number" in reasons and "different customer" in reasons
    assert "has a sales order" in reasons and "no customer with this number" in reasons
    assert _state(db) == before
    assert "already listed on line 4" in plan.rows[4].reason
    # DLV-9003 already has CS-0001 and no order: asking for CS-0001 again is "already done"
    single = fix.build_plan(db, [{"delivery_note_number": "DLV-9003", "customer_number": "CS-0001"}])
    assert single.rows[0].outcome == "already_done" and not single.changes and not single.refused


def test_customer_must_belong_to_the_deliverys_firm(db, world):
    # Firm B's delivery DLV-9001 with a customer number that exists only in firm A
    _customer(db, world["a"], "CS-ONLY-A")
    db.commit()
    plan = fix.build_plan(db, [{"delivery_note_number": "DLV-9001", "customer_number": "CS-ONLY-A",
                                "organization_id": world["b"].id}])
    assert plan.rows[0].outcome == "refused" and "delivery's organization" in plan.rows[0].reason


def test_apply_is_all_or_nothing(db, world):
    before = _state(db)
    rows = [{"delivery_note_number": "DLV-9002", "customer_number": "CS-0002"},
            {"delivery_note_number": "DLV-9004", "customer_number": "CS-0001"}]  # refused
    plan = fix.apply_plan(db, rows, "tester")
    assert plan.refused and _state(db) == before
    assert db.query(ActivityLog).count() == 0


def test_apply_updates_audits_and_is_idempotent(db, world, tmp_path):
    a = world["a"].id
    rows = [{"delivery_note_number": "DLV-9002", "customer_number": "CS-0002"},
            {"delivery_note_number": "DLV-9001", "customer_number": "CS-0001", "organization_id": a}]
    plan = fix.apply_plan(db, rows, "Ops Person <ops@example.com>")
    assert not plan.refused and len(plan.changes) == 2
    db.commit()
    audit_file = tmp_path / "audit.jsonl"
    fix.write_audit_file(audit_file, plan, "Ops Person <ops@example.com>")
    d = world["deliveries"]
    assert db.get(Delivery, d["orphan_a2"].id).customer_id == world["ca2"].id
    assert db.get(Delivery, d["orphan_a"].id).customer_id == world["ca"].id
    assert db.get(Delivery, d["orphan_b"].id).customer_id is None  # other firm's same number untouched
    logs = db.query(ActivityLog).filter(ActivityLog.action == fix.AUDIT_ACTION).all()
    assert len(logs) == 2 and all(l.actor_name == "Ops Person <ops@example.com>" for l in logs)
    detail = json.loads(next(l for l in logs if l.entity_id == d["orphan_a2"].id).description)
    assert detail["old"] is None and detail["new"] == world["ca2"].id
    lines = [json.loads(x) for x in audit_file.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2 and {l["actor"] for l in lines} == {"Ops Person <ops@example.com>"}
    assert all(l["old_customer_id"] is None and l["new_customer_id"] for l in lines)

    # running the same file again changes nothing and logs nothing
    again = fix.apply_plan(db, rows, "Ops Person <ops@example.com>")
    db.commit()
    assert not again.refused and not again.changes
    assert [r.outcome for r in again.rows] == ["already_done", "already_done"]
    assert db.query(ActivityLog).filter(ActivityLog.action == fix.AUDIT_ACTION).count() == 2


def test_duplicate_delivery_in_csv_is_refused(db, world):
    rows = [{"delivery_note_number": "DLV-9002", "customer_number": "CS-0002"},
            {"delivery_note_number": "DLV-9002", "customer_number": "CS-0001"}]
    plan = fix.build_plan(db, rows)
    assert plan.rows[1].outcome == "refused" and "already listed on line 2" in plan.rows[1].reason


def test_cli_requires_confirmation_and_csv_columns(tmp_path, capsys):
    good = tmp_path / "fixes.csv"
    good.write_text("delivery_note_number,customer_number\nDLV-1,CS-1\n", encoding="utf-8")
    assert fix.main([str(good), "--apply"]) == 1
    assert "--confirm-apply" in capsys.readouterr().err
    bad = tmp_path / "bad.csv"
    bad.write_text("number,customer\nDLV-1,CS-1\n", encoding="utf-8")
    assert fix.main([str(bad)]) == 1
    assert "missing column" in capsys.readouterr().err
