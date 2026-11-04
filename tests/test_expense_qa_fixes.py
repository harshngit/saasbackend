"""Expense fixes from live QA.

1. expenses.status must hold every value written to it ("clarification_requested" is 23
   characters; the column was VARCHAR(20) and Postgres refused it -- SQLite never checks).
   The length tests below fail on SQLite too, and the clarification loop runs end to end:
   clarify -> submitter edits -> pending again -> clarify again -> reject.
2/5. submitted_by_name, approved_by_name and reimbursed_by_name are returned next to the
   ids; the list resolves them for the whole page in one query.
3/6. A date-only (or bare-midnight) paid_at is that day at the time of the action in
   Asia/Kolkata, so the history reads created < approved < reimbursed; a paid_at before
   the approval is refused with 400.
4. POST /expenses: fewer queries for an Admin, and an Idempotency-Key makes a retry return
   the first expense instead of a duplicate.

Run: pytest tests/test_expense_qa_fixes.py   (uses DATABASE_URL / .env; Postgres works too)
"""

import ast
import os
import pathlib
import sys
import uuid
from datetime import datetime, timedelta, timezone

os.environ.setdefault("TESTING", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import String, event

from app.core import business_time
from app.core.database import Base, SessionLocal, auto_add_missing_columns, engine
from app.main import app
from app.models import Expense, Organization, Plan
from app.models.expense import EXPENSE_STATUSES
from app.services import numbering_service

Base.metadata.create_all(bind=engine)
auto_add_missing_columns()
client = TestClient(app)
IST = business_time.zone("Asia/Kolkata")
APP = pathlib.Path(__file__).resolve().parent.parent / "app"


def _register(label: str) -> dict:
    r = client.post("/auth/register", json={
        "organization_name": f"{label} {uuid.uuid4().hex[:6]}", "admin_name": f"Admin {label}",
        "email": f"admin_{uuid.uuid4().hex[:10]}@example.com", "password": "Password123!", "role": "admin",
    })
    assert r.status_code == 201, r.text
    auth = {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}
    me = client.get("/auth/me", headers=auth).json()
    with SessionLocal() as db:
        plan = db.query(Plan).filter(Plan.name == "Enterprise").first()
        db.query(Organization).filter(Organization.id == me["organization_id"]).update({"plan_id": plan.id})
        db.commit()
    return {"auth": auth, "org_id": me["organization_id"], "user_id": me["id"], "name": f"Admin {label}"}


def _staff(auth: dict, role: str, name: str) -> dict:
    email = f"{role}_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/users", json={"name": name, "email": email, "password": "Password123!", "role": role},
                    headers=auth)
    assert r.status_code == 201, r.text
    login = client.post("/auth/login", json={"email": email, "password": "Password123!"})
    return {"id": r.json()["id"], "name": name,
            "auth": {"Authorization": f"Bearer {login.json()['tokens']['access_token']}"}}


def _create(auth: dict, amount: float = 300.0, headers: dict | None = None, **extra):
    return client.post("/expenses", json={"category": "Food and Travel", "amount": amount, "payment_mode": "cash",
                                          "description": "client lunch", **extra},
                       headers={**auth, **(headers or {})})


def _at(value: str) -> datetime:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def org():
    o = _register("Expense QA")
    o["accountant"] = _staff(o["auth"], "accountant", "Asha Accountant")
    o["sales"] = _staff(o["auth"], "sales_officer", "Sam Sales")
    return o


# ------------------------------------------------------------- 1. status length


def _string_length(model, attr: str) -> int:
    col = model.__table__.columns[attr]
    assert isinstance(col.type, String) and col.type.length, f"{model.__name__}.{attr} is not a sized String"
    return col.type.length


def test_every_expense_status_fits_the_column():
    # Postgres enforces VARCHAR(n); SQLite does not -- so this is checked here, not left to the DB.
    limit = _string_length(Expense, "status")
    assert all(len(s) <= limit for s in EXPENSE_STATUSES), {s: len(s) for s in EXPENSE_STATUSES}
    for attr, values in (("expense_status", ("Submitted", "Approved", "Rejected", "Paid")),
                         ("approval_status", ("Pending", "Approved", "Rejected")),
                         ("payment_status", ("Pending", "Paid")),
                         ("reimbursement_method", ("cash", "bank_transfer", "upi", "cheque"))):
        assert all(len(v) <= _string_length(Expense, attr) for v in values), attr


def test_no_literal_written_to_a_string_column_is_longer_than_it():
    """Every string literal the code ties to a model column -- Model(col="..."),
    Model.col == "...", Model.col.in_((...)) or a module constant used there -- fits."""
    models = {m.class_.__name__: m.class_ for m in Base.registry.mappers}

    def lits(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value
        elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            for e in node.elts:
                yield from lits(e)

    consts = {}
    trees = {p: ast.parse(p.read_text(encoding="utf-8")) for p in APP.rglob("*.py")}
    for tree in trees.values():
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                consts[node.targets[0].id] = list(lits(node.value))

    def values(node):
        out = list(lits(node))
        name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
        return out + consts.get(name, [])

    too_long = []
    for path, tree in trees.items():
        for node in ast.walk(tree):
            pairs = []
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in models:
                pairs += [(node.func.id, kw.arg, kw.value) for kw in node.keywords if kw.arg]
            if isinstance(node, ast.Compare) and isinstance(node.left, ast.Attribute) \
                    and isinstance(node.left.value, ast.Name) and node.left.value.id in models:
                pairs += [(node.left.value.id, node.left.attr, c) for c in node.comparators]
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "in_" \
                    and isinstance(node.func.value, ast.Attribute) and isinstance(node.func.value.value, ast.Name) \
                    and node.func.value.value.id in models:
                pairs += [(node.func.value.value.id, node.func.value.attr, a) for a in node.args]
            for model_name, attr, value in pairs:
                col = models[model_name].__table__.columns.get(attr)
                if col is None or not isinstance(col.type, String) or not col.type.length:
                    continue
                for s in values(value):
                    if len(s) > col.type.length:
                        too_long.append(f"{col.table.name}.{attr} VARCHAR({col.type.length}) <- {s!r} "
                                        f"({path.name}:{node.lineno})")
    assert not too_long, "\n".join(too_long)


def test_clarification_loop_end_to_end(org):
    e = _create(org["sales"]["auth"]).json()
    acct = org["accountant"]["auth"]
    r = client.patch(f"/expenses/{e['id']}/request-clarification", json={"reason": "Attach the bill"}, headers=acct)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "clarification_requested" and r.json()["lifecycle_status"] == "pending_approval"

    # The submitter answers by editing: it goes back to pending approval.
    r = client.patch(f"/expenses/{e['id']}", json={"description": "client lunch, bill attached"},
                     headers=org["sales"]["auth"])
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending"

    r = client.patch(f"/expenses/{e['id']}/request-clarification", json={"reason": "Which client?"}, headers=acct)
    assert r.status_code == 200 and r.json()["status"] == "clarification_requested"
    r = client.post(f"/expenses/{e['id']}/reject", json={"reason": "Not a business expense"}, headers=acct)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rejected" and r.json()["rejection_reason"] == "Not a business expense"

    actions = [h["action"] for h in client.get(f"/expenses/{e['id']}/history", headers=acct).json()]
    assert actions == ["created", "clarification_requested", "resubmitted", "clarification_requested", "rejected"]
    listed = client.get("/expenses", params={"status": "pending_approval", "page_size": 100}, headers=acct).json()
    assert e["id"] not in [x["id"] for x in listed["items"]]


# ------------------------------------------------------------- 2/5. names next to the ids


def test_submitted_approved_and_reimbursed_names_are_returned(org):
    e = _create(org["sales"]["auth"]).json()
    acct = org["accountant"]["auth"]
    assert e["submitted_by"] == org["sales"]["id"] and e["submitted_by_name"] == "Sam Sales"
    assert e["approved_by_name"] is None and e["reimbursed_by_name"] is None
    r = client.post(f"/expenses/{e['id']}/approve", json={}, headers=acct).json()
    assert r["approved_by"] == org["accountant"]["id"] and r["approved_by_name"] == "Asha Accountant"
    r = client.post(f"/expenses/{e['id']}/reimburse", json={"payment_method": "upi"}, headers=acct)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reimbursed_by"] == org["accountant"]["id"] and body["reimbursed_by_name"] == "Asha Accountant"
    detail = client.get(f"/expenses/{e['id']}", headers=acct).json()
    assert (detail["submitted_by_name"], detail["approved_by_name"], detail["reimbursed_by_name"]) == (
        "Sam Sales", "Asha Accountant", "Asha Accountant")
    listed = client.get("/expenses", params={"page_size": 100}, headers=acct).json()
    row = next(x for x in listed["items"] if x["id"] == e["id"])
    assert (row["submitted_by_name"], row["approved_by_name"], row["reimbursed_by_name"]) == (
        "Sam Sales", "Asha Accountant", "Asha Accountant")
    assert all(x["submitted_by_name"] for x in listed["items"])  # no more "-" in Submitted By


def test_list_resolves_names_in_one_query(org):
    # Expenses from three submitters, approved and reimbursed by different people.
    for who in ("sales", "accountant"):
        _create(org[who]["auth"])
    _create(org["auth"], payment_status="Paid")
    acct = org["accountant"]["auth"]
    client.get("/expenses", params={"page_size": 100}, headers=acct)
    users = []

    def spy(_conn, _cur, statement, *_a):
        if "FROM users" in statement:
            users.append(statement)

    event.listen(engine, "before_cursor_execute", spy)
    try:
        page = client.get("/expenses", params={"page_size": 100}, headers=acct).json()
    finally:
        event.remove(engine, "before_cursor_execute", spy)
    assert len({x["submitted_by"] for x in page["items"]}) >= 3
    assert len(users) <= 2, users  # the logged-in user + one batch for every name on the page


# ------------------------------------------------------------- 3. paid_at date only


@pytest.mark.parametrize("sent", ["today", "today_midnight_utc", "today_midnight_ist"])
def test_date_only_paid_at_is_today_at_the_action_time(org, sent):
    e = _create(org["sales"]["auth"]).json()
    acct = org["accountant"]["auth"]
    approved = client.post(f"/expenses/{e['id']}/approve", json={}, headers=acct).json()
    today = business_time.today(IST)
    paid_at = {"today": today.isoformat(), "today_midnight_utc": f"{today.isoformat()}T00:00:00.000Z",
               "today_midnight_ist": f"{today.isoformat()}T00:00:00+05:30"}[sent]
    before = datetime.now(timezone.utc)
    r = client.post(f"/expenses/{e['id']}/reimburse", json={"payment_method": "cash", "paid_at": paid_at},
                    headers=acct)
    assert r.status_code == 200, r.text
    reimbursed = _at(r.json()["reimbursed_at"])
    assert reimbursed.astimezone(IST).date() == today
    assert reimbursed > _at(approved["approved_at"]) > _at(e["created_at"]) - timedelta(seconds=1)
    assert abs((reimbursed - before).total_seconds()) < 60  # the time of the action, not midnight


def test_past_date_keeps_its_day_and_an_exact_time_is_kept(org):
    acct = org["accountant"]["auth"]
    e = _create(org["sales"]["auth"]).json()
    client.post(f"/expenses/{e['id']}/approve", json={}, headers=acct)
    approved_long_ago = datetime.now(timezone.utc) - timedelta(days=10)
    with SessionLocal() as db:
        db.get(Expense, e["id"]).approved_at = approved_long_ago
        db.commit()
    picked = business_time.today(IST) - timedelta(days=3)
    r = client.post(f"/expenses/{e['id']}/reimburse", json={"payment_method": "cash", "paid_at": picked.isoformat()},
                    headers=acct).json()
    moment = _at(r["reimbursed_at"]).astimezone(IST)
    assert moment.date() == picked and moment.time() != datetime.min.time()

    e = _create(org["sales"]["auth"]).json()
    client.post(f"/expenses/{e['id']}/approve", json={}, headers=acct)
    with SessionLocal() as db:
        db.get(Expense, e["id"]).approved_at = approved_long_ago
        db.commit()
    exact = (datetime.now(IST) - timedelta(days=2)).replace(hour=15, minute=45, second=10, microsecond=0).isoformat()
    r = client.post(f"/expenses/{e['id']}/reimburse", json={"payment_method": "cash", "paid_at": exact},
                    headers=acct).json()
    assert _at(r["reimbursed_at"]) == datetime.fromisoformat(exact)


def test_paid_at_on_the_approval_day_is_never_before_the_approval(org):
    """Approved yesterday at 21:00 IST, recorded this morning with the approval day picked:
    "paid the same day", placed just after the approval (not refused)."""
    acct = org["accountant"]["auth"]
    e = _create(org["sales"]["auth"]).json()
    client.post(f"/expenses/{e['id']}/approve", json={}, headers=acct)
    yesterday_evening = datetime.combine(business_time.today(IST) - timedelta(days=1),
                                         datetime.min.time().replace(hour=21), tzinfo=IST)
    with SessionLocal() as db:
        db.get(Expense, e["id"]).approved_at = yesterday_evening.astimezone(timezone.utc)
        db.commit()
    r = client.post(f"/expenses/{e['id']}/reimburse", json={
        "payment_method": "cash", "paid_at": yesterday_evening.date().isoformat()}, headers=acct).json()
    assert _at(r["reimbursed_at"]) > yesterday_evening
    assert _at(r["reimbursed_at"]).astimezone(IST).date() == yesterday_evening.date()


def test_paid_at_before_the_approval_is_refused(org):
    acct = org["accountant"]["auth"]
    for paid_at in ((business_time.today(IST) - timedelta(days=1)).isoformat(),   # the day before
                    (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()):  # an hour before
        e = _create(org["sales"]["auth"]).json()
        client.post(f"/expenses/{e['id']}/approve", json={}, headers=acct)
        r = client.post(f"/expenses/{e['id']}/reimburse", json={"payment_method": "cash", "paid_at": paid_at},
                        headers=acct)
        assert r.status_code == 400, (paid_at, r.text)
        assert r.json()["detail"]["code"] == "PAID_BEFORE_APPROVAL"
        detail = client.get(f"/expenses/{e['id']}", headers=acct).json()
        assert detail["lifecycle_status"] == "approved" and detail["reimbursed_at"] is None  # nothing recorded


# ------------------------------------------------------------- 4. create: queries, idempotency


def _statements(fn):
    seen = []

    def spy(_conn, _cur, statement, *_a):
        seen.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", spy)
    try:
        return fn(), seen
    finally:
        event.remove(engine, "before_cursor_execute", spy)


def test_admin_create_is_one_insert_with_an_ordered_history(org):
    _create(org["auth"])  # the firm's first: sequences and accounts are set up once
    for extra in ({"payment_status": "Paid"}, {}):
        r, sql = _statements(lambda: _create(org["auth"], **extra))
        assert r.status_code == 201, r.text
        body = r.json()
        # Written approved (and paid) in its one INSERT: no follow-up UPDATE per step.
        assert sum(s.startswith("INSERT INTO expenses ") for s in sql) == 1
        assert not [s for s in sql if s.startswith("UPDATE expenses ")]
        assert _at(body["created_at"]) <= _at(body["approved_at"])
        history = [h["action"] for h in client.get(f"/expenses/{body['id']}/history", headers=org["auth"]).json()]
        if extra:
            assert body["lifecycle_status"] == "reimbursed" and body["reimbursed_by_name"] == org["name"]
            assert _at(body["approved_at"]) <= _at(body["reimbursed_at"])
            assert history == ["created", "approved", "reimbursed"]
        else:
            assert body["lifecycle_status"] == "approved" and history == ["created", "approved"]


def test_idempotency_key_returns_the_first_expense(org):
    key = {"Idempotency-Key": f"exp-{uuid.uuid4()}"}
    first = _create(org["sales"]["auth"], 410.0, headers=key)
    assert first.status_code == 201, first.text
    again = _create(org["sales"]["auth"], 410.0, headers=key)
    assert again.status_code == 201 and again.headers.get("Idempotent-Replayed") == "true"
    assert again.json() == first.json()
    with SessionLocal() as db:
        assert db.query(Expense).filter(Expense.organization_id == org["org_id"],
                                        Expense.idempotency_key == key["Idempotency-Key"]).count() == 1
    # The same key for a different expense is refused, not guessed at.
    other = _create(org["sales"]["auth"], 999.0, headers=key)
    assert other.status_code == 409 and other.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    # Keys belong to their submitter; and without a key nothing changes.
    assert _create(org["accountant"]["auth"], 410.0, headers=key).json()["id"] != first.json()["id"]
    a, b = _create(org["sales"]["auth"], 5.0).json(), _create(org["sales"]["auth"], 5.0).json()
    assert a["id"] != b["id"]


def test_admin_retry_with_the_same_key_posts_once(org):
    key = {"Idempotency-Key": f"exp-{uuid.uuid4()}"}
    first = _create(org["auth"], 77.0, headers=key).json()
    second = _create(org["auth"], 77.0, headers=key).json()
    assert first["id"] == second["id"]
    from app.models import JournalEntry
    with SessionLocal() as db:
        assert db.query(JournalEntry).filter(JournalEntry.source_id == first["id"]).count() == 1


def test_counter_check_reads_only_numbers_that_could_be_higher():
    """_highest_issued(above=k) finds every number above k that a full scan finds."""
    stem = f"QA-{uuid.uuid4().hex[:6]}-2026-"
    org_id = str(uuid.uuid4())
    with SessionLocal() as db:
        db.add(Organization(id=org_id, name="Numbers", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}",
                            status="active"))
        db.flush()
        values = [f"{stem}{n:04d}" for n in range(1, 13)] + [f"{stem}13", f"{stem}7", f"{stem}00099",
                                                             f"{stem}12345", f"{stem}x9"]
        for v in values:
            db.add(Expense(organization_id=org_id, category="c", amount=1, expense_number=v))
        db.flush()
        full = numbering_service._highest_issued(db, org_id, Expense.expense_number, stem)
        assert full == 12345
        for above in (0, 5, 9, 12, 13, 99, 100, 9999, 12345, 20000):
            got = numbering_service._highest_issued(db, org_id, Expense.expense_number, stem, above=above)
            assert max(got, above) == max(full, above), above
        db.rollback()
