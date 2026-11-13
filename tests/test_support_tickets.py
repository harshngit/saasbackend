"""Support tickets: organization and platform APIs, attachments, notifications, concurrency.

Run against PostgreSQL (advisory-lock rate limiting and row-locked numbering are only
concurrency-safe there):
    DATABASE_URL=postgresql://... python -m pytest tests/test_support_tickets.py
"""

import io
import os
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402

from app.core import notification_types as nt  # noqa: E402
from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.core.files import file_is_referenced  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Notification,
    Organization,
    StoredFile,
    SupportTicket,
    SupportTicketMessage,
    User,
)
from app.models.enums import OrganizationStatus, UserRole  # noqa: E402

client = TestClient(app)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
REOPEN_EXPIRED = "Reopen window expired, please raise a new ticket."


# ------------------------------------------------------------------ helpers


def _auth(user: User) -> dict:
    token = create_access_token(user.id, user.system_role, user.organization_id)
    return {"Authorization": f"Bearer {token}"}


def _user(db, org_id, system_role, role=None, name="User") -> User:
    u = User(email=f"{uuid.uuid4().hex[:10]}@support.test", name=f"{name} {uuid.uuid4().hex[:4]}",
             organization_id=org_id, system_role=system_role, role=role,
             password_hash=hash_password("Password123!"), is_active=True)
    db.add(u)
    db.flush()
    return u


@pytest.fixture(scope="module", autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)


@pytest.fixture
def world():
    """Org A (admin + two staff), Org B (admin), a locked org, and platform users."""
    db = SessionLocal()
    try:
        tag = uuid.uuid4().hex[:6]
        org_a = Organization(name=f"Support A {tag}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}")
        org_b = Organization(name=f"Support B {tag}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}")
        org_l = Organization(name=f"Support Locked {tag}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}",
                             status=OrganizationStatus.LOCKED)
        db.add_all([org_a, org_b, org_l])
        db.flush()
        w = {
            "org_a": org_a.id, "org_b": org_b.id, "org_l": org_l.id,
            "admin_a": _user(db, org_a.id, "admin", UserRole.ADMIN, "Admin A"),
            "staff_a1": _user(db, org_a.id, "staff", UserRole.SALES_OFFICER, "Staff A1"),
            "staff_a2": _user(db, org_a.id, "staff", UserRole.SALES_OFFICER, "Staff A2"),
            "admin_b": _user(db, org_b.id, "admin", UserRole.ADMIN, "Admin B"),
            "admin_l": _user(db, org_l.id, "admin", UserRole.ADMIN, "Admin L"),
            "super": _user(db, None, "super_admin", UserRole.SUPER_ADMIN, "Super"),
            "sm1": _user(db, None, "sales_manager", None, "SM1"),
            "sm2": _user(db, None, "sales_manager", None, "SM2"),
        }
        db.commit()
        for key, value in list(w.items()):
            if isinstance(value, User):
                w[key + "_id"] = value.id
                w[key] = _auth(value)
        yield w
    finally:
        db.close()


def _upload(headers, content=PNG, name="shot.png", content_type="image/png") -> str:
    r = client.post("/files/upload", files={"file": (name, io.BytesIO(content), content_type)}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["file_id"]


def _create(headers, **extra) -> dict:
    body = {"subject": "Printer not working", "description": "Invoice PDF does not print", "category": "bug", **extra}
    r = client.post("/support/tickets", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _set(ticket_id, **values):
    db = SessionLocal()
    try:
        t = db.get(SupportTicket, ticket_id)
        for k, v in values.items():
            setattr(t, k, v)
        db.commit()
    finally:
        db.close()


def _ticket_notifications(ticket_id) -> list[Notification]:
    db = SessionLocal()
    try:
        return [n for n in db.query(Notification).all() if (n.data or {}).get("ticket_id") == ticket_id]
    finally:
        db.close()


# ------------------------------------------------------------------ organization API


def test_create_numbering_validation_and_defaults(world):
    t = _create(world["staff_a1"])
    year = datetime.now(timezone(timedelta(hours=5, minutes=30))).year  # Asia/Kolkata
    assert t["ticket_number"].startswith(f"TKT-{year}-") and len(t["ticket_number"].split("-")[-1]) >= 4
    assert (t["status"], t["priority"], t["created_by_user_id"]) == ("open", "medium", world["staff_a1_id"])
    t2 = _create(world["admin_b"], priority="urgent", category="billing")  # another org, same global series
    n1, n2 = int(t["ticket_number"].rsplit("-", 1)[1]), int(t2["ticket_number"].rsplit("-", 1)[1])
    assert n2 == n1 + 1
    bad = [
        {"subject": "", "description": "x", "category": "bug"},
        {"subject": "s" * 201, "description": "x", "category": "bug"},
        {"subject": "s", "description": "d" * 5001, "category": "bug"},
        {"subject": "s", "description": "x", "category": "complaint"},
        {"subject": "s", "description": "x", "category": "bug", "priority": "critical"},
        {"description": "x", "category": "bug"},
    ]
    for body in bad:
        r = client.post("/support/tickets", json=body, headers=world["staff_a1"])
        assert r.status_code == 422, body
    # organization / creator come from the token, never the body
    r = client.post("/support/tickets", json={"subject": "s", "description": "d", "category": "other",
                                               "organization_id": world["org_b"], "created_by_user_id": world["admin_b_id"]},
                    headers=world["staff_a1"])
    assert r.status_code == 201
    db = SessionLocal()
    row = db.get(SupportTicket, r.json()["id"])
    assert (row.organization_id, row.created_by_user_id) == (world["org_a"], world["staff_a1_id"])
    db.close()
    # no token: refused exactly like any other protected endpoint of this API
    anonymous = client.get("/customers").status_code
    assert anonymous in (401, 403)
    assert client.post("/support/tickets", json={"subject": "s"}).status_code == anonymous
    assert client.get("/support/tickets").status_code == anonymous


def test_visibility_admin_staff_and_other_org(world):
    mine = _create(world["staff_a1"], subject="Staff one ticket")
    theirs = _create(world["staff_a2"], subject="Staff two ticket")
    admin_t = _create(world["admin_a"], subject="Admin ticket")
    other = _create(world["admin_b"], subject="Other org ticket")

    def ids(headers, **params):
        r = client.get("/support/tickets", params={"page_size": 100, **params}, headers=headers)
        assert r.status_code == 200, r.text
        return {t["id"] for t in r.json()["items"]}

    assert ids(world["staff_a1"]) == {mine["id"]}
    assert {mine["id"], theirs["id"], admin_t["id"]} <= ids(world["admin_a"]) and other["id"] not in ids(world["admin_a"])
    for tid in (theirs["id"], admin_t["id"], other["id"]):
        assert client.get(f"/support/tickets/{tid}", headers=world["staff_a1"]).status_code == 404
        assert client.post(f"/support/tickets/{tid}/messages", json={"body": "hi"}, headers=world["staff_a1"]).status_code == 404
        assert client.post(f"/support/tickets/{tid}/close", headers=world["staff_a1"]).status_code == 404
        assert client.post(f"/support/tickets/{tid}/reopen", headers=world["staff_a1"]).status_code == 404
    assert client.get(f"/support/tickets/{mine['id']}", headers=world["admin_a"]).status_code == 200
    assert client.get(f"/support/tickets/{mine['id']}", headers=world["admin_b"]).status_code == 404
    # filters, search and pagination
    assert ids(world["admin_a"], search=mine["ticket_number"]) == {mine["id"]}
    assert ids(world["admin_a"], search="staff TWO") == {theirs["id"]}
    assert ids(world["admin_a"], search="%' OR 1=1 --") == set()
    assert ids(world["admin_a"], status="closed") == set()
    assert client.get("/support/tickets", params={"status": "pending"}, headers=world["admin_a"]).status_code == 422
    page = client.get("/support/tickets", params={"page_size": 2}, headers=world["admin_a"]).json()
    assert set(page) == {"items", "total", "page", "page_size", "total_pages"} and len(page["items"]) == 2
    assert client.get("/support/tickets", params={"page_size": 101}, headers=world["admin_a"]).status_code == 422
    assert client.get("/support/tickets", headers=world["admin_a"]).json()["page_size"] == 20
    # platform accounts use the platform routes, not these
    assert client.get("/support/tickets", headers=world["super"]).status_code == 403


def test_reply_close_reopen_rules(world):
    t = _create(world["staff_a1"])
    tid = t["id"]
    r = client.post(f"/support/tickets/{tid}/messages", json={"body": "Any update?"}, headers=world["staff_a1"])
    assert r.status_code == 201 and r.json()["status"] == "open"
    # waiting_on_customer -> open on reply
    _set(tid, status="waiting_on_customer")
    assert client.post(f"/support/tickets/{tid}/messages", json={"body": "Here"}, headers=world["staff_a1"]).json()["status"] == "open"
    # resolved within 7 days -> reply reopens; resolved_at cleared
    _set(tid, status="resolved", resolved_at=datetime.now(timezone.utc) - timedelta(days=6, hours=23))
    body = client.post(f"/support/tickets/{tid}/messages", json={"body": "Again"}, headers=world["staff_a1"]).json()
    assert body["status"] == "open" and body["resolved_at"] is None
    # resolved more than 7 days ago -> exact 409
    _set(tid, status="resolved", resolved_at=datetime.now(timezone.utc) - timedelta(days=7, minutes=1))
    r = client.post(f"/support/tickets/{tid}/messages", json={"body": "Late"}, headers=world["staff_a1"])
    assert r.status_code == 409 and r.json()["detail"] == REOPEN_EXPIRED
    # close / closed reply 409 / already closed 409
    _set(tid, status="open", resolved_at=None)
    closed = client.post(f"/support/tickets/{tid}/close", headers=world["staff_a1"]).json()
    assert closed["status"] == "closed" and closed["closed_at"] is not None
    assert client.post(f"/support/tickets/{tid}/messages", json={"body": "x"}, headers=world["staff_a1"]).status_code == 409
    assert client.post(f"/support/tickets/{tid}/close", headers=world["staff_a1"]).status_code == 409
    # reopen within 7 days of closing
    reopened = client.post(f"/support/tickets/{tid}/reopen", headers=world["staff_a1"]).json()
    assert reopened["status"] == "open" and reopened["closed_at"] is None and reopened["resolved_at"] is None
    # reopen only from resolved/closed
    assert client.post(f"/support/tickets/{tid}/reopen", headers=world["staff_a1"]).status_code == 409
    # reopen after 7 days -> exact 409; a missing timestamp counts as expired
    for stamp in (datetime.now(timezone.utc) - timedelta(days=8), None):
        _set(tid, status="closed", closed_at=stamp)
        r = client.post(f"/support/tickets/{tid}/reopen", headers=world["staff_a1"])
        assert r.status_code == 409 and r.json()["detail"] == REOPEN_EXPIRED
    _set(tid, status="resolved", resolved_at=None, closed_at=None)
    assert client.post(f"/support/tickets/{tid}/reopen", headers=world["staff_a1"]).json()["detail"] == REOPEN_EXPIRED
    # every change moved last_activity_at
    db = SessionLocal()
    before = db.get(SupportTicket, tid).last_activity_at
    db.close()
    _set(tid, status="open")
    client.post(f"/support/tickets/{tid}/messages", json={"body": "bump"}, headers=world["staff_a1"])
    db = SessionLocal()
    assert db.get(SupportTicket, tid).last_activity_at > before
    db.close()


def test_cross_org_and_cross_staff_access_is_404_on_every_action(world):
    """Org A users (admin or staff) get 404, never 403, on org B's ticket for every action,
    and a STAFF user gets 404 on another STAFF user's ticket in the same org. None of
    these attempts changes the ticket."""
    other_org = _create(world["admin_b"], subject="Org B private")
    other_staff = _create(world["staff_a2"], subject="Staff A2 private")
    cases = [(world["admin_a"], other_org), (world["staff_a1"], other_org), (world["staff_a1"], other_staff)]
    for headers, t in cases:
        tid = t["id"]
        _set(tid, status="open")
        assert client.get(f"/support/tickets/{tid}", headers=headers).status_code == 404
        assert client.post(f"/support/tickets/{tid}/messages", json={"body": "hi"}, headers=headers).status_code == 404
        assert client.post(f"/support/tickets/{tid}/close", headers=headers).status_code == 404
        _set(tid, status="closed", closed_at=datetime.now(timezone.utc))  # reopenable, but not by them
        assert client.post(f"/support/tickets/{tid}/reopen", headers=headers).status_code == 404
        assert tid not in {x["id"] for x in client.get("/support/tickets", params={"page_size": 100},
                                                         headers=headers).json()["items"]}
        db = SessionLocal()
        try:
            assert db.get(SupportTicket, tid).status == "closed"
            assert db.query(SupportTicketMessage).filter(SupportTicketMessage.ticket_id == tid).count() == 0
        finally:
            db.close()


def test_internal_notes_never_in_any_org_facing_response(world):
    secret = "INTERNAL-ONLY-" + uuid.uuid4().hex
    t = _create(world["staff_a1"], subject="Internal note leak check")
    tid = t["id"]
    r = client.post(f"/platform/support/tickets/{tid}/messages", json={"body": secret, "is_internal": True},
                    headers=world["super"])
    assert r.status_code == 201
    org_responses = []
    for headers in (world["staff_a1"], world["admin_a"]):
        org_responses.append(client.get(f"/support/tickets/{tid}", headers=headers))
        org_responses.append(client.get("/support/tickets", params={"search": t["ticket_number"]}, headers=headers))
    org_responses.append(client.post(f"/support/tickets/{tid}/messages", json={"body": "customer reply"},
                                     headers=world["staff_a1"]))
    org_responses.append(client.post(f"/support/tickets/{tid}/close", headers=world["staff_a1"]))
    org_responses.append(client.post(f"/support/tickets/{tid}/reopen", headers=world["admin_a"]))
    for resp in org_responses:
        assert resp.status_code in (200, 201), resp.text
        assert secret not in resp.text, resp.request.url
        body = resp.json()
        for m in body.get("messages", []) if isinstance(body, dict) else []:
            assert m["is_internal"] is False and m["author_type"] in ("customer", "platform"), m
    # the platform still sees it
    assert secret in client.get(f"/platform/support/tickets/{tid}", headers=world["super"]).text


def test_locked_org_can_raise_and_read(world):
    t = _create(world["admin_l"])
    assert client.get(f"/support/tickets/{t['id']}", headers=world["admin_l"]).status_code == 200
    assert client.post(f"/support/tickets/{t['id']}/messages", json={"body": "help"}, headers=world["admin_l"]).status_code == 201
    # ...while an ordinary write is still blocked for that org
    assert client.post("/customers", json={"name": "x", "phone": "9876543210"}, headers=world["admin_l"]).status_code == 403


def test_rate_limit_rolling_24h_per_org(world):
    for _ in range(10):
        _create(world["staff_a1"] if _ % 2 else world["staff_a2"])  # shared across users
    r = client.post("/support/tickets", json={"subject": "s", "description": "d", "category": "bug"}, headers=world["admin_a"])
    assert r.status_code == 429
    _create(world["admin_b"])  # another organization is unaffected
    # rolling window: age the events past 24 hours and creation works again
    db = SessionLocal()
    from app.models import AuthRateLimitEvent
    db.query(AuthRateLimitEvent).filter(AuthRateLimitEvent.scope == "support_ticket_org").update(
        {AuthRateLimitEvent.created_at: datetime.now(timezone.utc) - timedelta(hours=24, minutes=1)})
    db.commit()
    db.close()
    _create(world["admin_a"])


# ------------------------------------------------------------------ attachments


def test_attachment_ownership_type_count_and_uniqueness(world):
    own = [_upload(world["staff_a1"]) for _ in range(4)]
    pdf = _upload(world["staff_a1"], b"%PDF-1.4 test", "doc.pdf", "application/pdf")
    txt = _upload(world["staff_a1"], b"hello", "notes.txt", "text/plain")
    colleague = _upload(world["staff_a2"])
    foreign = _upload(world["admin_b"])
    db = SessionLocal()
    legacy = StoredFile(organization_id=world["org_a"], filename="old.png", content_type="image/png", size=1, data=b"x")
    db.add(legacy)
    db.commit()
    legacy_id = legacy.id
    db.close()

    t = _create(world["staff_a1"], attachment_file_ids=[own[0], pdf])
    assert {a["file_id"] for a in t["attachments"]} == {own[0], pdf} and all(a["message_id"] is None for a in t["attachments"])
    assert all(a["url"].endswith(f"/files/{a['file_id']}") for a in t["attachments"])

    def attempt(ids, expect):
        r = client.post(f"/support/tickets/{t['id']}/messages", json={"body": "files", "attachment_file_ids": ids},
                        headers=world["staff_a1"])
        assert r.status_code == expect, (ids, r.status_code, r.text)
        return r

    for bad in ([foreign], [colleague], [str(uuid.uuid4())], [legacy_id]):
        assert attempt(bad, 400).json()["detail"].endswith("is not a file you uploaded")
    assert attempt([txt], 400).json()["detail"] == "Attachments must be images or PDF files"
    attempt(own[1:4] + [own[0]], 422)            # 4 files
    attempt([own[1], own[1]], 422)               # duplicate in one request
    attempt([own[0]], 409)                       # already attached to a ticket
    ok = attempt(own[1:4], 201).json()
    msg = [m for m in ok["messages"] if m["attachments"]][0]
    assert len(msg["attachments"]) == 3
    # attachments are visible to authorized users only
    assert client.get(f"/support/tickets/{t['id']}", headers=world["staff_a2"]).status_code == 404
    assert len(client.get(f"/support/tickets/{t['id']}", headers=world["admin_a"]).json()["attachments"]) == 2
    # files attached to tickets are protected from cleanup
    db = SessionLocal()
    assert file_is_referenced(db, own[1]) and file_is_referenced(db, pdf) and not file_is_referenced(db, txt)
    assert db.get(StoredFile, own[1]).uploaded_by_user_id == world["staff_a1_id"]
    db.close()


# ------------------------------------------------------------------ platform API


def test_platform_access_list_detail_and_internal_notes(world, monkeypatch):
    t = _create(world["staff_a1"], subject="Platform view ticket")
    tid = t["id"]
    for headers in (world["admin_a"], world["staff_a1"]):
        assert client.get("/platform/support/tickets", headers=headers).status_code == 403
        assert client.get("/platform/support/summary", headers=headers).status_code == 403
    assert client.get("/platform/support/tickets").status_code == client.get("/customers").status_code
    for headers in (world["super"], world["sm1"]):
        assert client.get("/platform/support/tickets", headers=headers).status_code == 200

    page = client.get("/platform/support/tickets", params={"search": t["ticket_number"]}, headers=world["super"]).json()
    item = page["items"][0]
    assert item["organization_name"].startswith("Support A") and item["created_by_name"].startswith("Staff A1")
    assert client.get("/platform/support/tickets", params={"organization_id": world["org_b"], "search": t["ticket_number"]},
                      headers=world["super"]).json()["total"] == 0

    # internal note: platform-only, text-only, notifies nobody, nothing over realtime
    secret = "INTERNAL-SECRET-REFUND-NOTE"
    events = []
    from app.services import notification_service
    monkeypatch.setattr(notification_service, "queue_event", lambda *a, **k: events.append((a, k)))
    before = len(_ticket_notifications(tid))
    file_id = _upload(world["staff_a1"])
    assert client.post(f"/platform/support/tickets/{tid}/messages",
                       json={"body": secret, "is_internal": True, "attachment_file_ids": [file_id]},
                       headers=world["super"]).status_code == 422
    r = client.post(f"/platform/support/tickets/{tid}/messages", json={"body": secret, "is_internal": True},
                    headers=world["super"])
    assert r.status_code == 201 and r.json()["status"] == "open"  # an internal note does not move status
    assert len(_ticket_notifications(tid)) == before and not any(secret in repr(e) for e in events)
    assert any(m["body"] == secret and m["is_internal"] for m in
               client.get(f"/platform/support/tickets/{tid}", headers=world["sm1"]).json()["messages"])
    for headers in (world["staff_a1"], world["admin_a"]):
        org_view = client.get(f"/support/tickets/{tid}", headers=headers).text
        assert secret not in org_view
    assert secret not in client.get("/support/tickets", headers=world["admin_a"]).text

    # public reply: open -> in_progress, visible to the org, creator notified
    r = client.post(f"/platform/support/tickets/{tid}/messages", json={"body": "We are looking into it"},
                    headers=world["sm1"])
    assert r.status_code == 201 and r.json()["status"] == "in_progress"
    org_msgs = client.get(f"/support/tickets/{tid}", headers=world["staff_a1"]).json()["messages"]
    assert [m["body"] for m in org_msgs if m["author_type"] == "platform"] == ["We are looking into it"]
    assert client.post(f"/platform/support/tickets/{tid}/messages",
                       json={"body": "x", "attachment_file_ids": [file_id]}, headers=world["sm1"]).status_code == 422
    assert client.get(f"/platform/support/tickets/{uuid.uuid4()}", headers=world["super"]).status_code == 404


def test_platform_update_assignment_and_timestamps(world):
    tid = _create(world["staff_a1"])["id"]

    def patch(headers, body, expect=200):
        r = client.patch(f"/platform/support/tickets/{tid}", json=body, headers=headers)
        assert r.status_code == expect, (body, r.text)
        return r.json()

    patch(world["sm1"], {"assigned_to_user_id": world["sm2_id"]}, 403)        # SM cannot assign others
    assert patch(world["sm1"], {"assigned_to_user_id": world["sm1_id"]})["assigned_to_user_id"] == world["sm1_id"]
    patch(world["sm2"], {"assigned_to_user_id": None}, 403)                   # nor take it from another
    assert patch(world["sm1"], {"assigned_to_user_id": None})["assigned_to_user_id"] is None  # own: may release
    assert patch(world["super"], {"assigned_to_user_id": world["sm2_id"]})["assigned_to_user_id"] == world["sm2_id"]
    for bad in (world["admin_a_id"], world["staff_a1_id"], str(uuid.uuid4())):
        patch(world["super"], {"assigned_to_user_id": bad}, 422)              # not a platform user
    patch(world["super"], {"status": "pending"}, 422)
    patch(world["super"], {"priority": "critical"}, 422)
    assert patch(world["super"], {"priority": "high"})["priority"] == "high"

    resolved = patch(world["super"], {"status": "resolved"})
    assert resolved["resolved_at"] and resolved["closed_at"] is None
    closed = patch(world["super"], {"status": "closed"})
    assert closed["closed_at"] and closed["resolved_at"]                      # when it was resolved stays true
    back = patch(world["super"], {"status": "in_progress"})
    assert back["resolved_at"] is None and back["closed_at"] is None          # no stale terminal timestamps
    assert back["last_activity_at"] >= closed["last_activity_at"]


def test_summary_counts_and_no_reply_over_24h(world):
    db = SessionLocal()
    db.query(SupportTicketMessage).delete()
    db.query(SupportTicket).delete()
    db.commit()
    db.close()
    old = datetime.now(timezone.utc) - timedelta(hours=30)
    a = _create(world["admin_a"], priority="high")                     # new: not waiting yet
    b = _create(world["admin_a"])                                      # old, never answered -> counts
    c = _create(world["admin_a"])                                      # old, answered publicly -> no
    d = _create(world["admin_a"])                                      # old, only an internal note -> counts
    e = _create(world["admin_a"])                                      # old, answered then customer replied long ago -> counts
    f = _create(world["admin_a"])                                      # resolved -> never counts
    for t in (b, c, d, e, f):
        _set(t["id"], created_at=old)
    client.post(f"/platform/support/tickets/{c['id']}/messages", json={"body": "answer"}, headers=world["super"])
    client.post(f"/platform/support/tickets/{d['id']}/messages", json={"body": "note", "is_internal": True},
                headers=world["super"])
    client.post(f"/platform/support/tickets/{e['id']}/messages", json={"body": "answer"}, headers=world["super"])
    client.post(f"/support/tickets/{e['id']}/messages", json={"body": "still broken"}, headers=world["admin_a"])
    db = SessionLocal()
    msgs = db.query(SupportTicketMessage).filter(SupportTicketMessage.ticket_id == e["id"]).order_by(SupportTicketMessage.created_at).all()
    msgs[0].created_at, msgs[1].created_at = old + timedelta(hours=1), old + timedelta(hours=2)
    db.commit()
    db.close()
    client.patch(f"/platform/support/tickets/{f['id']}", json={"status": "resolved"}, headers=world["super"])
    client.patch(f"/platform/support/tickets/{a['id']}", json={"assigned_to_user_id": world["sm1_id"]}, headers=world["super"])

    s = client.get("/platform/support/summary", headers=world["sm1"]).json()
    assert s["total"] == 6 and sum(s["by_status"].values()) == 6
    assert s["by_status"]["resolved"] == 1 and s["by_status"]["in_progress"] == 2  # c and e got public replies
    assert s["by_priority"]["high"] == 1 and s["by_priority"]["medium"] == 5
    assert s["unassigned"] == 4                                        # active (not f) and not a
    assert s["no_public_reply_over_24h"] == 3                          # b, d, e


# ------------------------------------------------------------------ notifications


def test_notifications_recipients_payload_and_no_push(world):
    db = SessionLocal()
    platform_ids = {u.id for u in db.query(User).filter(User.organization_id.is_(None), User.is_active.is_(True),
                                                         User.system_role.in_(["super_admin", "sales_manager"]))}
    db.close()
    t = _create(world["staff_a1"])
    tid = t["id"]
    created = [n for n in _ticket_notifications(tid) if n.data["event"] == nt.TICKET_CREATED]
    assert {n.user_id for n in created} == platform_ids and all(n.organization_id is None for n in created)

    client.post(f"/support/tickets/{tid}/messages", json={"body": "SECRET-CUSTOMER-TEXT"}, headers=world["staff_a1"])
    unassigned_reply = [n for n in _ticket_notifications(tid) if n.data["event"] == nt.TICKET_REPLIED_BY_CUSTOMER]
    assert {n.user_id for n in unassigned_reply} == platform_ids

    client.patch(f"/platform/support/tickets/{tid}", json={"assigned_to_user_id": world["sm2_id"]}, headers=world["super"])
    client.post(f"/support/tickets/{tid}/messages", json={"body": "again"}, headers=world["staff_a1"])
    replies = [n for n in _ticket_notifications(tid) if n.data["event"] == nt.TICKET_REPLIED_BY_CUSTOMER]
    assert len(replies) == len(platform_ids) + 1 and replies[-1].user_id == world["sm2_id"]

    client.post(f"/platform/support/tickets/{tid}/messages", json={"body": "SECRET-PLATFORM-TEXT"}, headers=world["sm2"])
    platform_reply = [n for n in _ticket_notifications(tid) if n.data["event"] == nt.TICKET_REPLIED_BY_PLATFORM]
    assert [n.user_id for n in platform_reply] == [world["staff_a1_id"]] and platform_reply[0].organization_id == world["org_a"]

    client.patch(f"/platform/support/tickets/{tid}", json={"status": "waiting_on_customer"}, headers=world["super"])
    changed = [n for n in _ticket_notifications(tid) if n.data["event"] == nt.TICKET_STATUS_CHANGED]
    assert [n.user_id for n in changed] == [world["staff_a1_id"]]
    # the creator closing their own ticket does not notify themselves
    client.post(f"/support/tickets/{tid}/close", headers=world["staff_a1"])
    assert len([n for n in _ticket_notifications(tid) if n.data["event"] == nt.TICKET_STATUS_CHANGED]) == 1

    for n in _ticket_notifications(tid):
        assert n.data == {"event": n.data["event"], "ticket_id": tid, "ticket_number": t["ticket_number"]}
        assert "SECRET" not in (n.title or "") + (n.body or "")
        assert nt.should_push(n.data["event"], n.data) is False


# ------------------------------------------------------------------ concurrency (PostgreSQL)


def _parallel(n, fn):
    barrier, out = threading.Barrier(n), []

    def run(i):
        barrier.wait()
        out.append(fn(i))

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=120)
    return out


@pytest.mark.skipif(engine.dialect.name != "postgresql", reason="needs PostgreSQL locking")
def test_concurrent_creation_unique_numbers_and_rate_limit(world):
    db = SessionLocal()
    orgs = []
    for _ in range(4):
        o = Organization(name=f"Burst {uuid.uuid4().hex[:6]}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}")
        db.add(o)
        db.flush()
        orgs.append(_auth(_user(db, o.id, "admin", UserRole.ADMIN)))
    db.commit()
    db.close()
    body = {"subject": "burst", "description": "d", "category": "other"}

    # 16 at once across 4 organizations: every number distinct
    results = _parallel(16, lambda i: client.post("/support/tickets", json=body, headers=orgs[i % 4]))
    assert all(r.status_code == 201 for r in results), [r.text for r in results if r.status_code != 201]
    numbers = [r.json()["ticket_number"] for r in results]
    assert len(set(numbers)) == 16

    # 14 at once in one organization that already has 4 in the window: exactly 6 more succeed
    results = _parallel(14, lambda i: client.post("/support/tickets", json=body, headers=orgs[0]))
    codes = sorted(r.status_code for r in results)
    assert codes.count(201) == 6 and codes.count(429) == 8, codes

    # the same uploaded file attached twice at once: one wins, the other 409
    file_id = _upload(world["staff_a1"])
    t1, t2 = _create(world["staff_a1"]), _create(world["staff_a1"])
    results = _parallel(2, lambda i: client.post(f"/support/tickets/{(t1, t2)[i]['id']}/messages",
                                                 json={"body": "x", "attachment_file_ids": [file_id]},
                                                 headers=world["staff_a1"]))
    assert sorted(r.status_code for r in results) == [201, 409], [r.text for r in results]
