"""Expense lifecycle: pending_approval -> approved | rejected ; approved -> reimbursed.

Run: pytest tests/test_expense_lifecycle.py   (uses DATABASE_URL / .env)
"""

import os
import sys
import uuid

os.environ.setdefault("TESTING", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from app.core.database import Base, SessionLocal, auto_add_missing_columns, engine
from app.main import app
from app.models import Account, Expense, JournalEntry, JournalLine, Organization, Plan

Base.metadata.create_all(bind=engine)
auto_add_missing_columns()
client = TestClient(app)

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


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
        assert plan is not None, "Enterprise plan missing from the test database"
        db.query(Organization).filter(Organization.id == me["organization_id"]).update({"plan_id": plan.id})
        db.commit()
    return {"auth": auth, "org_id": me["organization_id"], "user_id": me["id"]}


def _staff(auth: dict, role: str) -> dict:
    email = f"{role}_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/users", json={"name": f"{role} user", "email": email, "password": "Password123!", "role": role},
                    headers=auth)
    assert r.status_code == 201, r.text
    login = client.post("/auth/login", json={"email": email, "password": "Password123!"})
    assert login.status_code == 200, login.text
    return {"id": r.json()["id"], "auth": {"Authorization": f"Bearer {login.json()['tokens']['access_token']}"}}


def _create(auth: dict, amount: float = 500.0, **extra) -> dict:
    r = client.post("/expenses", json={"category": "Food and Travel", "amount": amount, "payment_mode": "cash", **extra},
                    headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


def _upload(auth: dict, name: str = "bill.png") -> dict:
    r = client.post("/files/upload", files={"file": (name, PNG, "image/png")}, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


def _lines(org_id: str, source_type: str, expense_id: str) -> dict[str, tuple[float, float]]:
    with SessionLocal() as db:
        je = db.query(JournalEntry).filter(
            JournalEntry.organization_id == org_id, JournalEntry.source_type == source_type,
            JournalEntry.source_id == expense_id,
        ).first()
        if je is None:
            return {}
        out = {}
        for line in db.query(JournalLine).filter(JournalLine.journal_entry_id == je.id):
            code = db.get(Account, line.account_id).account_code
            out[code] = (float(line.debit), float(line.credit))
        return out


@pytest.fixture(scope="module")
def org():
    o = _register("Expense Org")
    o["accountant"] = _staff(o["auth"], "accountant")
    o["accountant2"] = _staff(o["auth"], "accountant")
    o["sales"] = _staff(o["auth"], "sales_officer")
    return o


def test_admin_expense_auto_approved_not_reimbursed(org):
    e = _create(org["auth"])
    assert e["status"] == "approved"
    assert e["lifecycle_status"] == "approved"
    assert e["approved_by"] == org["user_id"] and e["approved_at"]
    assert e["reimbursed_at"] is None and e["payment_status"] == "Pending"
    # Approved-but-unpaid accrues to Payable, not Cash.
    assert _lines(org["org_id"], "expense", e["id"]) == {"6010": (500.0, 0.0), "2010": (0.0, 500.0)}


def test_admin_explicitly_paid_expense_is_reimbursed(org):
    e = _create(org["auth"], 300.0, payment_status="Paid", payment_mode="upi", payment_reference="UTR123")
    assert e["lifecycle_status"] == "reimbursed"
    assert e["reimbursement_method"] == "upi" and e["reimbursement_reference"] == "UTR123"
    assert _lines(org["org_id"], "expense", e["id"]) == {"6010": (300.0, 0.0), "1020": (0.0, 300.0)}
    assert _lines(org["org_id"], "expense_reimbursement", e["id"]) == {}


def test_accountant_and_staff_expenses_start_pending(org):
    a = _create(org["accountant"]["auth"], 120.0)
    assert a["lifecycle_status"] == "pending_approval" and a["status"] == "pending"
    s = _create(org["sales"]["auth"], 80.0, payment_status="Paid", approval_status="Approved", expense_status="Paid")
    assert s["lifecycle_status"] == "pending_approval"
    assert s["payment_status"] == "Pending" and s["approval_status"] == "Pending"
    assert s["reimbursed_at"] is None and s["approved_by"] is None


def test_approve_reimburse_flow_with_audit(org):
    e = _create(org["sales"]["auth"], 250.0)
    acct = org["accountant"]["auth"]
    r = client.post(f"/expenses/{e['id']}/approve", json={"note": "valid bill"}, headers=acct)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["lifecycle_status"] == "approved"
    assert body["approved_by"] == org["accountant"]["id"] and body["approval_note"] == "valid bill"
    assert _lines(org["org_id"], "expense", e["id"]) == {"6010": (250.0, 0.0), "2010": (0.0, 250.0)}

    again = client.post(f"/expenses/{e['id']}/approve", json={}, headers=acct)
    assert again.status_code == 400

    bad = client.post(f"/expenses/{e['id']}/reimburse", json={"payment_method": "card"}, headers=acct)
    assert bad.status_code == 400
    partial = client.post(f"/expenses/{e['id']}/reimburse", json={"payment_method": "upi", "amount": 100}, headers=acct)
    assert partial.status_code == 400

    r = client.post(f"/expenses/{e['id']}/reimburse", json={
        "payment_method": "bank_transfer", "payment_reference": "NEFT-77", "notes": "paid with salary",
    }, headers=acct)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["lifecycle_status"] == "reimbursed" and body["status"] == "approved"
    assert body["reimbursed_by"] == org["accountant"]["id"] and body["reimbursed_at"]
    assert body["reimbursement_method"] == "bank_transfer"
    assert body["reimbursement_reference"] == "NEFT-77" and body["reimbursement_notes"] == "paid with salary"
    assert body["reimbursed_amount"] == 250.0 and body["payment_status"] == "Paid"
    assert _lines(org["org_id"], "expense_reimbursement", e["id"]) == {"2010": (250.0, 0.0), "1020": (0.0, 250.0)}

    dup = client.post(f"/expenses/{e['id']}/reimburse", json={"payment_method": "cash"}, headers=acct)
    assert dup.status_code == 409
    with SessionLocal() as db:
        n = db.query(JournalEntry).filter(JournalEntry.source_id == e["id"]).count()
    assert n == 2

    detail = client.get(f"/expenses/{e['id']}", headers=acct).json()
    actions = [(h["action"], h["actor_id"]) for h in detail["history"]]
    assert actions == [("created", org["sales"]["id"]), ("approved", org["accountant"]["id"]),
                       ("reimbursed", org["accountant"]["id"])]
    assert detail["history"][1]["note"] == "valid bill"
    assert client.get(f"/expenses/{e['id']}/history", headers=acct).json() == detail["history"]
    # Reimbursed is terminal.
    assert client.delete(f"/expenses/{e['id']}", headers=org["auth"]).status_code == 400
    # Still counted as approved by status-based reports.
    listed = client.get("/expenses", params={"lifecycle_status": "reimbursed", "page_size": 100}, headers=acct).json()
    assert e["id"] in [x["id"] for x in listed["items"]]
    listed = client.get("/expenses", params={"status": "approved", "page_size": 100}, headers=acct).json()
    assert e["id"] in [x["id"] for x in listed["items"]]


def test_reject_requires_reason_and_blocks_reimbursement(org):
    e = _create(org["sales"]["auth"], 90.0)
    acct = org["accountant"]["auth"]
    assert client.post(f"/expenses/{e['id']}/reject", json={}, headers=acct).status_code == 400
    r = client.post(f"/expenses/{e['id']}/reject", json={"reason": "no bill attached"}, headers=acct)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["lifecycle_status"] == "rejected"
    assert body["rejected_by"] == org["accountant"]["id"] and body["rejected_at"]
    assert body["rejection_reason"] == "no bill attached"
    r = client.post(f"/expenses/{e['id']}/reimburse", json={"payment_method": "cash"}, headers=acct)
    assert r.status_code == 400
    assert client.post(f"/expenses/{e['id']}/approve", json={}, headers=acct).status_code == 400
    hist = client.get(f"/expenses/{e['id']}/history", headers=acct).json()
    assert [h["action"] for h in hist] == ["created", "rejected"] and hist[1]["note"] == "no bill attached"


def test_amount_cannot_change_after_submission(org):
    e = _create(org["sales"]["auth"], 400.0)
    auth = org["sales"]["auth"]
    r = client.patch(f"/expenses/{e['id']}", json={"amount": 450.0}, headers=auth)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "EXPENSE_AMOUNT_LOCKED" and "amount" in r.json()["detail"]["fields"]
    r = client.patch(f"/expenses/{e['id']}", json={"items": [{"description": "x", "quantity": 1, "unit_price": 999}]},
                     headers=auth)
    assert r.status_code == 400
    r = client.patch(f"/expenses/{e['id']}", json={"approval_status": "Approved"}, headers=auth)
    assert r.status_code == 400
    # Re-sending the same amount with a real edit is fine (full-form save).
    r = client.patch(f"/expenses/{e['id']}", json={"amount": 400.0, "description": "taxi to client"}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["amount"] == 400.0 and r.json()["description"] == "taxi to client"
    with SessionLocal() as db:
        assert db.get(Expense, e["id"]).amount == 400.0
    # Approved: no edits at all, admin included.
    assert client.post(f"/expenses/{e['id']}/approve", json={}, headers=org["accountant"]["auth"]).status_code == 200
    assert client.patch(f"/expenses/{e['id']}", json={"amount": 1.0}, headers=org["auth"]).status_code == 400


def test_proof_attachments_visible_and_tenant_safe(org):
    receipt = _upload(org["sales"]["auth"], "taxi-bill.png")
    doc = _upload(org["sales"]["auth"], "approval.png")
    e = _create(org["sales"]["auth"], 60.0, receipt_url=receipt["url"],
                supporting_documents=[{"url": doc["url"], "filename": "approval.png"}])
    detail = client.get(f"/expenses/{e['id']}", headers=org["accountant"]["auth"]).json()
    kinds = {a["kind"]: a for a in detail["attachments"]}
    assert kinds["receipt"]["filename"] == "taxi-bill.png"
    assert kinds["receipt"]["mime_type"] == "image/png" and kinds["receipt"]["is_image"] is True
    assert kinds["receipt"]["id"] == receipt["file_id"] and kinds["receipt"]["uploaded_at"]
    assert kinds["receipt"]["url"].endswith(f"/files/{receipt['file_id']}")
    assert kinds["supporting_document"]["filename"] == "approval.png"
    # The file itself is served (viewable).
    assert client.get(f"/files/{receipt['file_id']}").status_code in (200, 302, 307)

    other = _register("Expense Foreign")
    foreign = _upload(other["auth"], "foreign.png")
    r = client.post("/expenses", json={"category": "Other", "amount": 10, "receipt_url": foreign["url"]},
                    headers=org["sales"]["auth"])
    assert r.status_code == 400
    r = client.patch(f"/expenses/{e['id']}", json={"receipt_url": foreign["url"]}, headers=org["sales"]["auth"])
    assert r.status_code == 400
    # Another organization cannot read the expense (or its attachment list).
    assert client.get(f"/expenses/{e['id']}", headers=other["auth"]).status_code == 404


def test_permissions_and_self_approval(org):
    own = _create(org["accountant"]["auth"], 70.0)
    r = client.post(f"/expenses/{own['id']}/approve", json={}, headers=org["accountant"]["auth"])
    assert r.status_code == 403
    staff_exp = _create(org["sales"]["auth"], 40.0)
    assert client.post(f"/expenses/{staff_exp['id']}/approve", json={}, headers=org["sales"]["auth"]).status_code == 403
    assert client.post(f"/expenses/{staff_exp['id']}/reject", json={"reason": "x"},
                       headers=org["sales"]["auth"]).status_code == 403
    # Another accountant may approve it; then its own submitter cannot reimburse it.
    assert client.post(f"/expenses/{own['id']}/approve", json={}, headers=org["accountant2"]["auth"]).status_code == 200
    r = client.post(f"/expenses/{own['id']}/reimburse", json={"payment_method": "cash"}, headers=org["accountant"]["auth"])
    assert r.status_code == 403
    r = client.post(f"/expenses/{own['id']}/reimburse", json={"payment_method": "cash"}, headers=org["auth"])
    assert r.status_code == 200, r.text
    # Sales officer (own scope) cannot see someone else's expense.
    assert client.get(f"/expenses/{own['id']}", headers=org["sales"]["auth"]).status_code == 404


def test_tenant_isolation(org):
    e = _create(org["sales"]["auth"], 33.0)
    other = _register("Expense Other")
    assert client.get(f"/expenses/{e['id']}", headers=other["auth"]).status_code == 404
    assert client.post(f"/expenses/{e['id']}/approve", json={}, headers=other["auth"]).status_code == 404
    assert client.post(f"/expenses/{e['id']}/reimburse", json={"payment_method": "cash"},
                       headers=other["auth"]).status_code == 404
    ids = [x["id"] for x in client.get("/expenses", params={"page_size": 100}, headers=other["auth"]).json()["items"]]
    assert e["id"] not in ids
