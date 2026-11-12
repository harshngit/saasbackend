"""Business notifications: who receives what, and that nobody receives it twice.

Covers the sales-officer, expense-lifecycle, finance, Day-End, inventory and platform
events on top of the existing ones (see tests/test_push_notifications.py), plus actor
exclusion, tenant isolation, retries / dedupe, push failure and the API's contract fields.
Plan gates are stubbed: entitlements are not what is under test."""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core import notification_types as nt
from app.core.database import Base, engine
from app.core.security import create_access_token
from app.main import app
from app.models import (
    Expense, FollowUp, Notification, Organization, Product, Quotation, Role, User, Warehouse,
)
from app.services import entitlement_service, notification_service, push_service, reminder_service, role_service

# ------------------------------------------------------------------ fixtures


@pytest.fixture(autouse=True)
def _no_plan_gates(monkeypatch):
    monkeypatch.setattr(entitlement_service, "check_entitlement", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "check_limit", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "has_entitlement", lambda *a, **k: True)
    monkeypatch.setattr(push_service.settings, "push_notifications_enabled", False)


@pytest.fixture
def db():
    Base.metadata.create_all(bind=engine)
    session = Session(bind=engine)
    try:
        yield session
    finally:
        session.close()


class Firm(SimpleNamespace):
    pass


def _client(user: User) -> TestClient:
    client = TestClient(app)
    token = create_access_token(user.id, role=user.system_role or "staff", organization_id=user.organization_id)
    client.headers = {"Authorization": f"Bearer {token}"}
    return client


def _person(db: Session, org_id: str | None, *, system_role: str, role: str, role_id: str | None = None,
            name: str = "Person") -> User:
    user = User(id=str(uuid.uuid4()), organization_id=org_id, email=f"n_{uuid.uuid4().hex[:10]}@x.com",
                name=name, password_hash="h", role=role, system_role=system_role, role_id=role_id, is_active=True)
    db.add(user)
    db.commit()
    return user


def _firm(db: Session) -> Firm:
    org = Organization(id=str(uuid.uuid4()), name=f"Firm {uuid.uuid4().hex[:6]}",
                       company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}")
    db.add(org)
    db.commit()
    role_service.seed_default_roles(db, org.id)
    roles = {r.name: r for r in db.query(Role).filter(Role.organization_id == org.id)}
    f = Firm(org=org, id=org.id)
    f.admin = _person(db, org.id, system_role="admin", role="admin", name="Owner")
    f.admin2 = _person(db, org.id, system_role="admin", role="admin", name="Second Admin")
    f.sales = _person(db, org.id, system_role="staff", role="sales_officer",
                      role_id=roles["Sales Officer"].id, name="Rahul Sales")
    f.sales2 = _person(db, org.id, system_role="staff", role="sales_officer",
                       role_id=roles["Sales Officer"].id, name="Other Sales")
    f.partner = _person(db, org.id, system_role="staff", role="delivery_partner",
                        role_id=roles["Delivery Partner"].id, name="Rahul")
    f.accountant = _person(db, org.id, system_role="staff", role="accountant",
                           role_id=roles["Accountant"].id, name="Accountant")
    return f


def _inbox(db: Session, user: User, event: str | None = None) -> list[Notification]:
    db.expire_all()
    rows = db.query(Notification).filter(Notification.user_id == user.id).order_by(Notification.created_at).all()
    return [r for r in rows if event is None or (r.data or {}).get("event") == event]


def _all_for_org(db: Session, org_id: str) -> list[Notification]:
    db.expire_all()
    return db.query(Notification).filter(Notification.organization_id == org_id).all()


# ------------------------------------------------------------------ sales officer: leads

_LEAD = {"mobile_number": "9876543210", "lead_source": "Referral"}


def test_lead_assignment_notifies_only_the_new_assignee(db):
    f = _firm(db)
    admin = _client(f.admin)
    res = admin.post("/leads", json={**_LEAD, "name": "Acme Traders", "assigned_salesperson_id": f.sales.id})
    assert res.status_code == 201, res.text
    lead_id = res.json()["id"]

    [n] = _inbox(db, f.sales, nt.LEAD_ASSIGNED)
    assert n.title == "New lead assigned" and "Acme Traders" in n.body
    assert n.data == {"event": "lead_assigned", "lead_id": lead_id, "actor_id": f.admin.id}
    assert n.organization_id == f.id and n.link == f"/leads/{lead_id}"
    assert _inbox(db, f.admin) == []  # the actor is not told about their own action

    # Re-sending the same assignee (a retry) creates nothing.
    assert admin.patch(f"/leads/{lead_id}", json={"assigned_salesperson_id": f.sales.id}).status_code == 200
    assert admin.patch(f"/leads/{lead_id}", json={"notes": "called"}).status_code == 200
    assert len(_inbox(db, f.sales, nt.LEAD_ASSIGNED)) == 1

    # Reassignment goes to the new owner only.
    assert admin.patch(f"/leads/{lead_id}", json={"assigned_salesperson_id": f.sales2.id}).status_code == 200
    [r] = _inbox(db, f.sales2, nt.LEAD_ASSIGNED)
    assert r.title == "Lead reassigned to you"
    assert len(_inbox(db, f.sales, nt.LEAD_ASSIGNED)) == 1

    # A sales officer creating their own lead is not notified about it.
    res = _client(f.sales).post("/leads", json={**_LEAD, "name": "Self lead"})
    assert res.status_code == 201, res.text
    assert len(_inbox(db, f.sales, nt.LEAD_ASSIGNED)) == 1


def test_lead_cannot_be_assigned_across_organizations(db):
    a, b = _firm(db), _firm(db)
    res = _client(a.admin).post("/leads", json={**_LEAD, "name": "X", "assigned_salesperson_id": b.sales.id})
    assert res.status_code == 400
    assert _inbox(db, b.sales) == []


# ------------------------------------------------------------------ sales officer: follow-ups


def _follow_up(db, f, due, assignee=None, status="pending", lead=None) -> FollowUp:
    fu = FollowUp(id=str(uuid.uuid4()), organization_id=f.id, title="Call back", due_date=due,
                  assigned_to_id=(assignee or f.sales).id, status=status, lead_id=lead.id if lead else None)
    db.add(fu)
    db.commit()
    return fu


def test_follow_up_due_and_overdue_reminders_are_sent_once(db):
    f, other = _firm(db), _firm(db)
    now = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
    soon = _follow_up(db, f, now + timedelta(minutes=20))          # inside the 30-min lead window
    late = _follow_up(db, f, now - timedelta(hours=2))             # past the 60-min grace
    _follow_up(db, f, now + timedelta(hours=5))                    # not yet
    _follow_up(db, f, now - timedelta(hours=2), status="completed")
    _follow_up(db, f, now - timedelta(days=30))                    # older than the lookback
    other_fu = _follow_up(db, other, now + timedelta(minutes=5), assignee=other.sales)

    reminder_service.run_follow_up_reminders(db, now)
    reminder_service.run_follow_up_reminders(db, now)              # a second sweep / instance
    reminder_service.run_follow_up_reminders(db, now + timedelta(minutes=5))

    due = _inbox(db, f.sales, nt.FOLLOW_UP_DUE)
    overdue = _inbox(db, f.sales, nt.FOLLOW_UP_OVERDUE)
    assert [n.data["follow_up_id"] for n in due] == [soon.id]
    assert [n.data["follow_up_id"] for n in overdue] == [late.id]
    assert due[0].title == "Follow-up due" and overdue[0].title == "Follow-up overdue"
    assert due[0].organization_id == f.id
    assert [n.data["follow_up_id"] for n in _inbox(db, other.sales)] == [other_fu.id]  # tenant isolation
    assert _inbox(db, f.sales2) == [] and _inbox(db, f.admin) == []

    # Once the due one passes its grace it is reminded as overdue -- once.
    later = now + timedelta(hours=2)
    reminder_service.run_follow_up_reminders(db, later)
    reminder_service.run_follow_up_reminders(db, later)
    assert {n.data["follow_up_id"] for n in _inbox(db, f.sales, nt.FOLLOW_UP_OVERDUE)} == {soon.id, late.id}
    assert len(_inbox(db, f.sales, nt.FOLLOW_UP_OVERDUE)) == 2

    # Rescheduling re-arms it.
    db.expire_all()
    fu = db.get(FollowUp, late.id)
    fu.due_date = later + timedelta(minutes=10)
    db.commit()
    reminder_service.run_follow_up_reminders(db, later)
    assert len([n for n in _inbox(db, f.sales, nt.FOLLOW_UP_DUE) if n.data["follow_up_id"] == late.id]) == 1


def test_follow_up_reminder_skips_inactive_or_foreign_assignees(db):
    f, other = _firm(db), _firm(db)
    now = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
    f.sales2.is_active = False
    db.commit()
    _follow_up(db, f, now + timedelta(minutes=5), assignee=f.sales2)
    _follow_up(db, f, now + timedelta(minutes=5), assignee=other.sales)  # corrupt cross-org row
    reminder_service.run_follow_up_reminders(db, now)
    assert _inbox(db, f.sales2) == [] and _inbox(db, other.sales) == []


def test_follow_up_reminder_settings_are_read_from_the_environment(monkeypatch):
    from app.core.config import Settings

    defaults = Settings(_env_file=None)
    assert (defaults.follow_up_reminders_enabled, defaults.follow_up_reminder_interval_seconds,
            defaults.follow_up_due_lead_minutes, defaults.follow_up_overdue_after_minutes,
            defaults.follow_up_overdue_lookback_days) == (True, 300, 30, 60, 7)
    for name, value in {"FOLLOW_UP_REMINDERS_ENABLED": "false", "FOLLOW_UP_REMINDER_INTERVAL_SECONDS": "900",
                        "FOLLOW_UP_DUE_LEAD_MINUTES": "15", "FOLLOW_UP_OVERDUE_AFTER_MINUTES": "120",
                        "FOLLOW_UP_OVERDUE_LOOKBACK_DAYS": "3"}.items():
        monkeypatch.setenv(name, value)
    loaded = Settings(_env_file=None)
    assert (loaded.follow_up_reminders_enabled, loaded.follow_up_reminder_interval_seconds,
            loaded.follow_up_due_lead_minutes, loaded.follow_up_overdue_after_minutes,
            loaded.follow_up_overdue_lookback_days) == (False, 900, 15, 120, 3)


def test_background_sweep_respects_enabled_flag_and_interval(monkeypatch):
    started = []

    class FakeThread:
        def __init__(self, target, args, name, daemon):
            started.append(args[0])

        def start(self):
            pass

        def is_alive(self):
            return False

    monkeypatch.setattr(reminder_service.threading, "Thread", FakeThread)
    monkeypatch.setattr(reminder_service, "_thread", None)
    monkeypatch.setattr(reminder_service.settings, "follow_up_reminders_enabled", False)
    assert reminder_service.start_background_sweeps() is False and started == []

    monkeypatch.setattr(reminder_service.settings, "follow_up_reminders_enabled", True)
    monkeypatch.setattr(reminder_service.settings, "follow_up_reminder_interval_seconds", 900)
    assert reminder_service.start_background_sweeps() is True and started == [900]
    monkeypatch.setattr(reminder_service.settings, "follow_up_reminder_interval_seconds", 5)
    reminder_service.start_background_sweeps()
    assert started[-1] == 30  # clamped: a typo cannot hammer the database


def test_reminder_timing_settings_are_respected(db, monkeypatch):
    f = _firm(db)
    now = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(reminder_service.settings, "follow_up_due_lead_minutes", 10)
    monkeypatch.setattr(reminder_service.settings, "follow_up_overdue_after_minutes", 5)
    monkeypatch.setattr(reminder_service.settings, "follow_up_overdue_lookback_days", 1)
    outside_lead = _follow_up(db, f, now + timedelta(minutes=20))   # due-lead is only 10 min
    inside_lead = _follow_up(db, f, now + timedelta(minutes=8))
    just_late = _follow_up(db, f, now - timedelta(minutes=6))       # past the 5-min grace
    too_old = _follow_up(db, f, now - timedelta(days=2))            # beyond the 1-day lookback

    reminder_service.run_follow_up_reminders(db, now)
    due = {n.data["follow_up_id"] for n in _inbox(db, f.sales, nt.FOLLOW_UP_DUE)}
    overdue = {n.data["follow_up_id"] for n in _inbox(db, f.sales, nt.FOLLOW_UP_OVERDUE)}
    assert due == {inside_lead.id} and overdue == {just_late.id}
    assert outside_lead.id not in due | overdue and too_old.id not in due | overdue


def test_reminder_sweep_isolates_failures(db, monkeypatch):
    f = _firm(db)
    now = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
    bad = _follow_up(db, f, now + timedelta(minutes=5))
    good = _follow_up(db, f, now + timedelta(minutes=6))
    real_notify = notification_service.notify

    def flaky(session, user_id, title, body=None, *a, data=None, **k):
        if data and data.get("follow_up_id") == bad.id:
            raise RuntimeError("boom")
        return real_notify(session, user_id, title, body, *a, data=data, **k)

    monkeypatch.setattr(notification_service, "notify", flaky)
    reminder_service.run_follow_up_reminders(db, now)  # must not raise
    assert [n.data["follow_up_id"] for n in _inbox(db, f.sales, nt.FOLLOW_UP_DUE)] == [good.id]

    monkeypatch.setattr(notification_service, "notify", real_notify)
    reminder_service.run_follow_up_reminders(db, now)  # the failed one is retried by the next sweep
    reminder_service.run_follow_up_reminders(db, now)  # ...and then never sent again
    assert sorted(n.data["follow_up_id"] for n in _inbox(db, f.sales, nt.FOLLOW_UP_DUE)) == sorted([good.id, bad.id])


def test_external_cron_command_runs_one_sweep(monkeypatch, capsys):
    from app.scripts import send_follow_up_reminders

    monkeypatch.setattr(reminder_service, "run_once", lambda: {"due": 2, "overdue": 1})
    send_follow_up_reminders.main()
    assert capsys.readouterr().out.strip() == "Follow-up reminders: 2 due, 1 overdue"


# ------------------------------------------------------------------ sales officer: quotations


def _quotation(db, f, status="sent", salesperson=None) -> Quotation:
    q = Quotation(id=str(uuid.uuid4()), organization_id=f.id, quotation_number=f"Q-{uuid.uuid4().hex[:5]}",
                  status=status, salesperson_id=(salesperson or f.sales).id)
    db.add(q)
    db.commit()
    return q


def test_quotation_accepted_and_rejected_notify_the_salesperson(db):
    f = _firm(db)
    admin = _client(f.admin)
    accepted, rejected = _quotation(db, f), _quotation(db, f)

    assert admin.patch(f"/quotations/{accepted.id}", json={"status": "accepted"}).status_code == 200
    assert admin.patch(f"/quotations/{accepted.id}", json={"status": "accepted"}).status_code == 200  # replay
    assert admin.patch(f"/quotations/{rejected.id}", json={"status": "rejected"}).status_code == 200

    rows = _inbox(db, f.sales, nt.QUOTATION_STATUS_CHANGED)
    assert [(r.title, r.data["quotation_id"], r.data["status"]) for r in rows] == [
        ("Quotation accepted", accepted.id, "accepted"), ("Quotation rejected", rejected.id, "rejected")]
    assert rows[0].link == f"/quotations/{accepted.id}"
    assert _inbox(db, f.sales2) == [] and _inbox(db, f.admin) == []

    # The salesperson changing their own quotation is not notified.
    own = _quotation(db, f)
    assert _client(f.sales).patch(f"/quotations/{own.id}", json={"status": "accepted"}).status_code == 200
    assert len(_inbox(db, f.sales, nt.QUOTATION_STATUS_CHANGED)) == 2


# ------------------------------------------------------------------ orders: admins + salesperson


def _order(f, **kw):
    base = dict(id=str(uuid.uuid4()), organization_id=f.id, order_number="SO-1", status="completed",
                salesperson_id=f.sales.id, created_by=f.admin.id, assigned_delivery_partner_id=None, customer=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_order_completion_reaches_admins_and_the_relevant_salesperson_once(db):
    f = _firm(db)
    order = _order(f)
    created = notification_service.notify_order_status_changed(db, order, "processing", actor_id=f.admin.id)
    db.commit()
    assert {n.user_id for n in created} == {f.admin2.id, f.sales.id}
    # A retried completion (the same transition again) creates nothing new.
    assert notification_service.notify_order_status_changed(db, order, "processing", actor_id=f.admin.id) == []
    db.commit()
    assert _inbox(db, f.sales2) == [] and _inbox(db, f.partner) == [] and _inbox(db, f.accountant) == []

    cancelled = _order(f, status="cancelled", salesperson_id=f.sales2.id)
    created = notification_service.notify_order_status_changed(db, cancelled, "placed", actor_id=f.sales2.id)
    assert {n.user_id for n in created} == {f.admin.id, f.admin2.id}  # the acting salesperson is excluded
    db.rollback()


def test_order_customer_owner_is_notified_only_if_they_can_open_the_order(db):
    f = _firm(db)
    customer = SimpleNamespace(assigned_sales_officer_id=f.sales.id)
    # Own-scope Sales Officer who neither created nor sells the order: its link would 404.
    hidden = _order(f, salesperson_id=None, customer=customer)
    assert {n.user_id for n in notification_service.notify_order_status_changed(db, hidden, "processing")} == {
        f.admin.id, f.admin2.id}
    visible = _order(f, salesperson_id=None, customer=customer, created_by=f.sales.id)
    assert f.sales.id in {n.user_id for n in notification_service.notify_order_status_changed(db, visible, "processing")}
    db.rollback()


# ------------------------------------------------------------------ new orders


def _order_setup(db, f):
    """A stocked product, a customer owned by sales2, and a team (sales in it) whose manager
    has a team-scope role that can view sales orders."""
    from app.models import Customer, Team
    from app.services import stock_service

    wh = Warehouse(id=str(uuid.uuid4()), organization_id=f.id, name="Main", code=f"W{uuid.uuid4().hex[:5]}")
    product = Product(id=str(uuid.uuid4()), organization_id=f.id, name="Cement Bag", price=100.0,
                      inventory_tracking=True)
    customer = Customer(id=str(uuid.uuid4()), organization_id=f.id, name="Acme", phone="9876500001",
                        assigned_sales_officer_id=f.sales2.id)
    db.add_all([wh, product, customer])
    db.commit()
    stock_service.adjust_on_hand(db, f.id, wh.id, product.id, None, 100, "adjustment")
    mgr_role = Role(id=str(uuid.uuid4()), organization_id=f.id, name="Area Manager",
                    permissions={"sales_orders": {"view": True}}, data_scope="team")
    db.add(mgr_role)
    db.commit()
    f.manager = _person(db, f.id, system_role="staff", role="sales_officer", role_id=mgr_role.id, name="Manager")
    team = Team(id=str(uuid.uuid4()), organization_id=f.id, name=f"North {uuid.uuid4().hex[:4]}",
                manager_id=f.manager.id)
    db.add(team)
    db.flush()
    f.manager.team_id = team.id
    f.sales.team_id = team.id
    db.commit()
    return {"customer_id": customer.id, "warehouse_id": wh.id,
            "items": [{"product_id": product.id, "quantity": 2}]}


def test_order_placed_notifies_admins_salesperson_and_team_manager_once(db):
    f = _firm(db)
    other = _firm(db)
    body = _order_setup(db, f)

    # A draft (pickup, no automated flow) notifies nobody...
    res = _client(f.sales).post("/orders", json={**body, "fulfilment_method": "pickup"})
    assert res.status_code == 201, res.text
    order_id = res.json()["id"]
    assert [n for n in _all_for_org(db, f.id) if (n.data or {}).get("order_id") == order_id] == []

    # ...until it is confirmed: admins + the team manager, never the acting salesperson.
    assert _client(f.sales).post(f"/orders/{order_id}/confirm").status_code == 200
    rows = [n for n in _all_for_org(db, f.id) if (n.data or {}).get("order_id") == order_id]
    assert sorted(n.user_id for n in rows) == sorted([f.admin.id, f.admin2.id, f.manager.id])
    assert {n.title for n in rows} == {"Sales order confirmed"}
    assert {n.data["event"] for n in rows} == {nt.ORDER_CONFIRMED}
    # The customer's own-scope owner (sales2) cannot open this order, so is not told.
    assert _inbox(db, f.sales2) == [] and _inbox(db, f.partner) == []
    assert _all_for_org(db, other.id) == []

    # Placed by an admin for the salesperson: the salesperson and manager hear, not the actor.
    from app.models import Customer, SalesOrder
    from app.services import order_service

    db.expire_all()
    order, _ = order_service.place_order(
        db, f.admin, db.get(Customer, body["customer_id"]),
        [order_service.OrderLine(product_id=body["items"][0]["product_id"], quantity=1)],
        warehouse_id=body["warehouse_id"], salesperson_id=f.sales.id)
    db.commit()
    rows = [n for n in _all_for_org(db, f.id) if (n.data or {}).get("order_id") == order.id]
    assert sorted(n.user_id for n in rows) == sorted([f.admin2.id, f.sales.id, f.manager.id])
    assert {n.title for n in rows} == {"New sales order"} and {n.data["event"] for n in rows} == {nt.ORDER_CREATED}

    # A retry of the same event, or the salesperson also being an admin, never doubles up.
    order = db.get(SalesOrder, order.id)
    order.salesperson_id = f.admin2.id
    assert notification_service.notify_order_placed(db, order, f.admin.id, nt.ORDER_CREATED, "New sales order") == []
    db.rollback()


# ------------------------------------------------------------------ push allowlist


@pytest.mark.parametrize("event, data, expected", [
    (nt.DELIVERY_ASSIGNED, {}, True),
    (nt.DELIVERY_REJECTED, {}, True),
    (nt.DELIVERY_STATUS_CHANGED, {"status": "failed"}, True),
    (nt.DELIVERY_STATUS_CHANGED, {"status": "cancelled"}, True),
    (nt.DELIVERY_STATUS_CHANGED, {"status": "delivered"}, False),
    (nt.DELIVERY_STATUS_CHANGED, {"status": "partially_delivered"}, False),
    (nt.DAY_END_STATUS_CHANGED, {"status": "rejected"}, True),
    (nt.DAY_END_STATUS_CHANGED, {"status": "approved"}, False),
    (nt.DAY_END_SUBMITTED, {"has_variance": "true"}, True),
    (nt.DAY_END_SUBMITTED, {"has_variance": "false"}, False),
    (nt.ORDER_CREATED, {}, True),
    (nt.ORDER_CONFIRMED, {}, True),
    (nt.ORDER_STATUS_CHANGED, {"status": "completed"}, False),
    (nt.ORDER_STATUS_CHANGED, {"status": "cancelled"}, False),
    (nt.LEAD_ASSIGNED, {}, True),
    (nt.FOLLOW_UP_DUE, {}, True),
    (nt.FOLLOW_UP_OVERDUE, {}, False),
    (nt.QUOTATION_STATUS_CHANGED, {"status": "accepted"}, True),
    (nt.QUOTATION_STATUS_CHANGED, {"status": "rejected"}, True),
    (nt.EXPENSE_STATUS_CHANGED, {"status": "approved"}, True),
    (nt.EXPENSE_STATUS_CHANGED, {"status": "rejected"}, True),
    (nt.EXPENSE_STATUS_CHANGED, {"status": "reimbursed"}, False),
    (nt.EXPENSE_CLARIFICATION_REQUESTED, {}, True),
    (nt.EXPENSE_READY_FOR_REIMBURSEMENT, {}, True),
    (nt.EXPENSE_CREATED, {}, False),
    (nt.LEAVE_STATUS_CHANGED, {"status": "approved"}, True),
    (nt.PAYMENT_RECEIVED, {}, False),
    (nt.STOCK_ALERT, {"status": "out_of_stock"}, True),
    (nt.STOCK_ALERT, {"status": "low_stock"}, False),
    (nt.ORGANIZATION_CREATED, {}, True),
    (nt.UPGRADE_REQUESTED, {}, True),
    (nt.PUSH_TEST, {}, True),
    (None, {}, False),
])
def test_should_push_allowlist(event, data, expected):
    assert nt.should_push(event, data) is expected


def test_every_event_has_a_push_decision():
    # A new event must be added to the allowlist on purpose (or stay in-app only on purpose).
    in_app_only = {nt.ORDER_STATUS_CHANGED, nt.FOLLOW_UP_OVERDUE, nt.PAYMENT_RECEIVED, nt.EXPENSE_CREATED,
                   nt.DAY_END_SUBMITTED,
                   # Support tickets: in-app only by decision (D11), never pushed.
                   nt.TICKET_CREATED, nt.TICKET_REPLIED_BY_PLATFORM, nt.TICKET_REPLIED_BY_CUSTOMER,
                   nt.TICKET_STATUS_CHANGED}
    assert set(nt._PUSHED) | in_app_only == nt.ALL_EVENTS


@pytest.fixture
def dispatched(monkeypatch):
    """Push on, and the worker pool replaced by a recorder of the notification ids handed to it."""
    monkeypatch.setattr(push_service.settings, "push_notifications_enabled", True)
    sent: list[str] = []
    monkeypatch.setattr(push_service, "_dispatch", lambda ids: sent.extend(ids))
    return sent


def test_low_stock_is_in_app_only_and_out_of_stock_is_pushed(db, dispatched):
    from app.services import stock_service

    f = _firm(db)
    wh = Warehouse(id=str(uuid.uuid4()), organization_id=f.id, name="Main", code=f"W{uuid.uuid4().hex[:5]}")
    product = Product(id=str(uuid.uuid4()), organization_id=f.id, name="Sand", minimum_stock_level=5,
                      inventory_tracking=True)
    db.add_all([wh, product])
    db.commit()
    stock_service.adjust_on_hand(db, f.id, wh.id, product.id, None, 10, "adjustment")
    db.commit()
    stock_service.adjust_on_hand(db, f.id, wh.id, product.id, None, -6, "adjustment")  # -> low
    db.commit()
    low = _inbox(db, f.admin, nt.STOCK_ALERT)
    assert [n.data["status"] for n in low] == ["low_stock"] and dispatched == []

    stock_service.adjust_on_hand(db, f.id, wh.id, product.id, None, -4, "adjustment")  # -> out
    db.commit()
    out = [n for n in _inbox(db, f.admin, nt.STOCK_ALERT) if n.data["status"] == "out_of_stock"]
    assert len(out) == 1 and out[0].id in dispatched


def test_delivery_assignment_is_pushed_and_reassignment_back_notifies_again(db, dispatched):
    from app.services import delivery_service

    f = _firm(db)
    other_partner = _person(db, f.id, system_role="staff", role="delivery_partner", name="Second Partner")
    delivery = SimpleNamespace(id=str(uuid.uuid4()), delivery_number="DL-1", delivery_note_number=None,
                               organization_id=f.id)
    n = delivery_service.notify_delivery_assignment(db, delivery, f.partner.id)
    db.commit()
    assert n.user_id == f.partner.id and n.id in dispatched
    # Saving the same partner again (a retry) is not a new assignment.
    assert delivery_service.notify_delivery_assignment(db, delivery, f.partner.id, f.partner.id) is None
    # Moved away and later back: each real assignment notifies.
    delivery_service.notify_delivery_assignment(db, delivery, other_partner.id, f.partner.id)
    delivery_service.notify_delivery_assignment(db, delivery, f.partner.id, other_partner.id)
    db.commit()
    assert len(_inbox(db, f.partner, nt.DELIVERY_ASSIGNED)) == 2
    assert len(_inbox(db, other_partner, nt.DELIVERY_ASSIGNED)) == 1


def test_in_app_only_event_still_creates_the_row_and_realtime_event(db, dispatched):
    f = _firm(db)
    n = notification_service.notify(db, f.admin.id, "Delivery completed", "DL-1", organization_id=f.id,
                                    data=nt.build_data(nt.DELIVERY_STATUS_CHANGED, delivery_id="d1",
                                                       status="delivered"))
    realtime = [e for e in db.info.get("_pending_realtime_events", []) if e["event"] == "notification.created"]
    db.commit()
    assert _inbox(db, f.admin, nt.DELIVERY_STATUS_CHANGED)[0].id == n.id and dispatched == []
    assert [e["data"]["id"] for e in realtime] == [n.id]  # the websocket event is unchanged


# ------------------------------------------------------------------ expenses (web Add Expense screen)


def test_accountant_has_full_expense_rights_and_admin_expenses_are_auto_approved(db):
    from app.core.permissions import ACTIONS, default_role_matrices

    assert all(default_role_matrices()["Accountant"]["expenses"].get(a) for a in ACTIONS)
    f = _firm(db)
    res = _client(f.admin).post("/expenses", json={"category": "Travel", "amount": 500})
    assert res.status_code in (200, 201), res.text
    assert res.json()["lifecycle_status"] == "approved"
    res = _client(f.accountant).post("/expenses", json={"category": "Travel", "amount": 300})
    assert res.status_code in (200, 201), res.text
    assert res.json()["lifecycle_status"] == "pending_approval"  # only admins are auto-approved


# ------------------------------------------------------------------ expenses


def _submit_expense(f, client=None) -> str:
    res = (client or _client(f.sales)).post("/expenses", json={"category": "Travel", "amount": 1200})
    assert res.status_code in (200, 201), res.text
    return res.json()["id"]


def test_expense_lifecycle_notifications(db):
    f, other = _firm(db), _firm(db)
    expense_id = _submit_expense(f)
    assert {n.user_id for n in _all_for_org(db, f.id) if n.data["event"] == nt.EXPENSE_CREATED} == {
        f.admin.id, f.admin2.id}

    admin = _client(f.admin)
    assert admin.post(f"/expenses/{expense_id}/approve", json={}).status_code == 200
    assert admin.post(f"/expenses/{expense_id}/approve", json={}).status_code == 400  # retry refused

    [approved] = _inbox(db, f.sales, nt.EXPENSE_STATUS_CHANGED)
    assert approved.title == "Expense approved" and "1,200.00" in approved.body and "Travel" in approved.body
    assert approved.data == {"event": "expense_status_changed", "expense_id": expense_id, "status": "approved",
                             "actor_id": f.admin.id}
    ready = [n for n in _all_for_org(db, f.id) if n.data["event"] == nt.EXPENSE_READY_FOR_REIMBURSEMENT]
    # Accountant (expenses:approve) and the other admin -- never the approver or the submitter.
    assert {n.user_id for n in ready} == {f.accountant.id, f.admin2.id}
    assert ready[0].title == "Expense ready for reimbursement"

    acct = _client(f.accountant)
    res = acct.post(f"/expenses/{expense_id}/reimburse", json={"payment_method": "upi"})
    assert res.status_code == 200, res.text
    assert acct.post(f"/expenses/{expense_id}/reimburse", json={"payment_method": "upi"}).status_code == 409
    reimbursed = [n for n in _inbox(db, f.sales, nt.EXPENSE_STATUS_CHANGED) if n.data["status"] == "reimbursed"]
    assert len(reimbursed) == 1 and reimbursed[0].title == "Expense reimbursed" and "upi" in reimbursed[0].body

    rejected_id = _submit_expense(f)
    res = admin.post(f"/expenses/{rejected_id}/reject", json={"reason": "No receipt"})
    assert res.status_code == 200, res.text
    [rej] = [n for n in _inbox(db, f.sales, nt.EXPENSE_STATUS_CHANGED) if n.data["expense_id"] == rejected_id]
    assert rej.title == "Expense rejected" and "No receipt" in rej.body
    assert not [n for n in _all_for_org(db, f.id)
                if n.data["event"] == nt.EXPENSE_READY_FOR_REIMBURSEMENT and n.data["expense_id"] == rejected_id]

    clar_id = _submit_expense(f)
    assert admin.patch(f"/expenses/{clar_id}/request-clarification", json={"reason": "Which trip?"}).status_code == 200
    assert [n.data["expense_id"] for n in _inbox(db, f.sales, nt.EXPENSE_CLARIFICATION_REQUESTED)] == [clar_id]

    assert _all_for_org(db, other.id) == []  # nothing leaks into another firm


def test_push_failure_does_not_break_the_business_transaction(db, monkeypatch):
    monkeypatch.setattr(push_service.settings, "push_notifications_enabled", True)
    monkeypatch.setattr(push_service, "_dispatch", push_service.deliver_notification_pushes)

    def boom(*a, **k):
        raise RuntimeError("firebase down")

    monkeypatch.setattr(push_service, "send_to_user", boom)
    f = _firm(db)
    expense_id = _submit_expense(f)
    res = _client(f.admin).post(f"/expenses/{expense_id}/approve", json={})
    assert res.status_code == 200 and res.json()["lifecycle_status"] == "approved"
    db.expire_all()
    assert db.get(Expense, expense_id).status == "approved"
    assert len(_inbox(db, f.sales, nt.EXPENSE_STATUS_CHANGED)) == 1


# ------------------------------------------------------------------ customer payments


def test_manual_payment_reaches_admins_and_accountant_not_sales(db):
    from app.models import Customer

    f = _firm(db)
    customer = Customer(id=str(uuid.uuid4()), organization_id=f.id, name="Sharma Stores")
    db.add(customer)
    db.commit()
    res = _client(f.admin).post("/payment-receipts", json={"customer_id": customer.id, "amount_received": 500,
                                                           "payment_method": "cash"})
    assert res.status_code in (200, 201), res.text
    rows = [n for n in _all_for_org(db, f.id) if n.data.get("event") == nt.PAYMENT_RECEIVED]
    assert {n.user_id for n in rows} == {f.admin2.id, f.accountant.id}  # recorder excluded
    [acct] = [n for n in rows if n.user_id == f.accountant.id]
    assert acct.title == "Payment received"
    assert "Sharma Stores" in acct.body and "500.00" in acct.body and "cash" in acct.body


def test_payment_notification_is_not_repeated_for_the_same_payment(db):
    f = _firm(db)
    payment = SimpleNamespace(id=str(uuid.uuid4()), organization_id=f.id, amount=999.0, payment_mode="Online – Razorpay",
                              receipt_number="RCPT-9", customer=SimpleNamespace(name="Cust"))
    invoice = SimpleNamespace(id=str(uuid.uuid4()), invoice_number="INV-7", customer=None)
    for _ in range(3):  # webhook redelivered
        notification_service.notify_payment_received(
            db, payment, invoice, admin_title="₹999.00 received for Invoice #INV-7 via Razorpay",
            admin_body="Payment ID: pay_1", link=f"/invoices/{invoice.id}")
        db.commit()
    rows = [n for n in _all_for_org(db, f.id) if n.data.get("payment_id") == payment.id]
    assert sorted(n.user_id for n in rows) == sorted([f.admin.id, f.admin2.id, f.accountant.id])
    assert {n.title for n in rows if n.user_id != f.accountant.id} == {"₹999.00 received for Invoice #INV-7 via Razorpay"}


# ------------------------------------------------------------------ delivery day-end


def test_day_end_submit_review_cycle(db):
    f = _firm(db)
    partner = _client(f.partner)
    res = partner.post("/delivery-day-ends/submit", json={})
    assert res.status_code == 200, res.text
    day_end_id = res.json()["id"]
    assert partner.post("/delivery-day-ends/submit", json={}).status_code == 409  # retry refused

    submitted = [n for n in _all_for_org(db, f.id) if n.data["event"] == nt.DAY_END_SUBMITTED]
    assert {n.user_id for n in submitted} == {f.admin.id, f.admin2.id}
    assert submitted[0].title == "Day-End submitted" and submitted[0].data["has_variance"] == "false"
    assert "Rahul" in submitted[0].body

    admin = _client(f.admin)
    assert admin.post(f"/delivery-day-ends/{day_end_id}/reject", json={"reason": "Count cash again"}).status_code == 200
    [rej] = _inbox(db, f.partner, nt.DAY_END_STATUS_CHANGED)
    assert rej.title == "Day-End rejected" and "Count cash again" in rej.body
    assert rej.link == "/delivery-day-ends/current"

    res = partner.post("/delivery-day-ends/submit", json={"handed_over": {"cash": 500}, "notes": "extra float"})
    assert res.status_code == 200, res.text
    attention = [n for n in _all_for_org(db, f.id) if n.data["event"] == nt.DAY_END_SUBMITTED
                 and n.data["has_variance"] == "true"]
    assert {n.user_id for n in attention} == {f.admin.id, f.admin2.id}  # one notification each, not two
    assert attention[0].title == "Day-End requires attention" and "₹500.00 variance" in attention[0].body

    assert admin.post(f"/delivery-day-ends/{day_end_id}/approve", json={}).status_code == 200
    assert admin.post(f"/delivery-day-ends/{day_end_id}/approve", json={}).status_code == 409
    decisions = _inbox(db, f.partner, nt.DAY_END_STATUS_CHANGED)
    assert [n.data["status"] for n in decisions] == ["rejected", "approved"]
    assert decisions[1].title == "Day-End approved"
    assert _inbox(db, f.sales) == [] and _inbox(db, f.accountant) == []


# ------------------------------------------------------------------ inventory


def test_stock_alerts_fire_on_transitions_only(db):
    from app.services import stock_service

    f = _firm(db)
    wh = Warehouse(id=str(uuid.uuid4()), organization_id=f.id, name="Main", code=f"W{uuid.uuid4().hex[:5]}")
    product = Product(id=str(uuid.uuid4()), organization_id=f.id, name="Cement Bag", minimum_stock_level=5,
                      inventory_tracking=True)
    db.add_all([wh, product])
    db.commit()

    def move(delta):
        stock_service.adjust_on_hand(db, f.id, wh.id, product.id, None, delta, "adjustment")
        db.commit()

    move(10)          # ok
    move(-6)          # 4: ok -> low
    move(-1)          # 3: still low
    stock_service.stock_summary(db, wh.id, product.id, None)  # a read never notifies
    move(-3)          # 0: low -> out
    move(-1)          # -1: still out
    move(20)          # restock
    move(-17)         # 3: ok -> low again

    alerts = [n for n in _inbox(db, f.admin, nt.STOCK_ALERT)]
    assert [n.data["status"] for n in alerts] == ["low_stock", "out_of_stock", "low_stock"]
    assert alerts[0].title == "Low stock" and "Cement Bag" in alerts[0].body and "minimum 5" in alerts[0].body
    assert alerts[0].data["warehouse_id"] == wh.id and alerts[0].data["product_id"] == product.id
    assert len(_inbox(db, f.admin2, nt.STOCK_ALERT)) == 3
    assert _inbox(db, f.sales) == []


# ------------------------------------------------------------------ platform


def test_platform_notifications_stay_out_of_tenant_feeds(db):
    super_admin = _person(db, None, system_role="super_admin", role="super_admin")
    sales_manager = _person(db, None, system_role="sales_manager", role="super_admin")
    email = f"owner_{uuid.uuid4().hex[:8]}@x.com"
    res = TestClient(app).post("/auth/register", json={
        "organization_name": "Brand New Co", "admin_name": "Owner", "email": email, "password": "Str0ng!Passw0rd"})
    assert res.status_code in (200, 201), res.text
    new_org_id = res.json()["organization"]["id"]

    for platform_user in (super_admin, sales_manager):
        [n] = [r for r in _inbox(db, platform_user, nt.ORGANIZATION_CREATED)
               if r.data["target_organization_id"] == new_org_id]
        assert n.organization_id is None and n.title == "New organization created" and n.type == "platform"
    assert _all_for_org(db, new_org_id) == []  # never in the tenant's own feed

    owner = db.query(User).filter(User.email == email).one()
    plan_id = _any_plan(db)
    client = _client(owner)
    body = {"requested_plan_id": plan_id, "billing_cycle": "monthly"}
    assert client.post("/organizations/upgrade-request", json=body).status_code == 200
    assert client.post("/organizations/upgrade-request", json=body).status_code == 200  # double click
    ups = [r for r in _inbox(db, super_admin, nt.UPGRADE_REQUESTED) if r.data["target_organization_id"] == new_org_id]
    assert len(ups) == 1 and ups[0].organization_id is None and ups[0].title == "New upgrade request"
    assert ups[0].link == f"/superadmin/organizations/{new_org_id}"
    assert _all_for_org(db, new_org_id) == []

    feed = _client(super_admin).get("/notifications", params={"page_size": 100}).json()["items"]
    row = next(i for i in feed if i["id"] == ups[0].id)
    assert row["scope"] == "platform" and row["organization_id"] is None
    # The tenant admin's own feed does not show it.
    assert all(i["id"] != ups[0].id for i in client.get("/notifications").json()["items"])


def _any_plan(db) -> str:
    from app.models import Plan
    from app.seed import seed_plans

    plan = db.query(Plan).filter(Plan.is_active.is_(True)).first()
    if plan is None:
        seed_plans(db)
        plan = db.query(Plan).filter(Plan.is_active.is_(True)).first()
    return plan.id


def test_platform_staff_upgrade_request_notifies_super_admin_not_the_requester(db):
    super_admin = _person(db, None, system_role="super_admin", role="super_admin")
    sales_manager = _person(db, None, system_role="sales_manager", role="super_admin")
    f = _firm(db)
    from app.models import Plan

    plan_id = _any_plan(db)
    if f.org.plan_id == plan_id:
        plan_id = db.query(Plan).filter(Plan.id != plan_id, Plan.is_active.is_(True)).first().id
    res = _client(sales_manager).post(f"/platform/organizations/{f.id}/upgrade-requests",
                                      json={"requested_plan_id": plan_id, "reason": "Customer asked"})
    assert res.status_code == 201, res.text
    [n] = [r for r in _inbox(db, super_admin, nt.UPGRADE_REQUESTED) if r.data.get("upgrade_request_id") == res.json()["id"]]
    assert n.link == f"/platform/upgrade-requests/{res.json()['id']}" and n.data["actor_id"] == sales_manager.id
    assert [r for r in _inbox(db, sales_manager, nt.UPGRADE_REQUESTED)
            if r.data.get("upgrade_request_id") == res.json()["id"]] == []
    assert _all_for_org(db, f.id) == []


# ------------------------------------------------------------------ API contract + security


def test_api_exposes_contract_fields_and_is_per_user(db):
    f = _firm(db)
    expense_id = _submit_expense(f)
    _client(f.admin).post(f"/expenses/{expense_id}/approve", json={})
    client = _client(f.sales)
    items = client.get("/notifications").json()["items"]
    row = next(i for i in items if i["event"] == nt.EXPENSE_STATUS_CHANGED)
    assert row["entity_type"] == "expense" and row["entity_id"] == expense_id
    assert row["action_url"] == f"/expenses/{expense_id}"
    assert row["actor_id"] == f.admin.id and row["recipient_user_id"] == f.sales.id
    assert row["organization_id"] == f.id and row["scope"] == "organization"
    assert row["is_read"] is False and row["read_at"] is None
    assert "dedupe_key" not in row

    assert client.get("/notifications/unread-count").json()["unread"] == len(items)
    assert client.patch(f"/notifications/{row['id']}/read").json()["read_at"] is not None
    assert client.get("/notifications/unread-count").json()["unread"] == len(items) - 1

    # Another user (same firm) cannot read or touch it; unauthenticated calls are refused.
    assert _client(f.sales2).patch(f"/notifications/{row['id']}/read").status_code == 404
    assert all(i["id"] != row["id"] for i in _client(f.sales2).get("/notifications").json()["items"])
    assert TestClient(app).get("/notifications").status_code in (401, 403)


def test_describe_and_build_data_contract():
    assert nt.describe({"event": "payment_received", "payment_id": "p"})["action_url"] == "/payment-receipts/p"
    assert nt.describe({"event": "payment_received", "payment_id": "p", "invoice_id": "i"})["entity_type"] == "invoice"
    assert nt.describe({"event": "day_end_status_changed", "day_end_id": "d", "status": "approved"})[
        "action_url"] == "/delivery-day-ends/d"
    assert nt.describe({"event": "push_test"}) == {"entity_type": None, "entity_id": None, "action_url": None}
    assert nt.describe(None)["entity_id"] is None
    assert nt.build_data(nt.LEAD_ASSIGNED, lead_id="l", actor_id="u") == {
        "event": "lead_assigned", "lead_id": "l", "actor_id": "u"}
    with pytest.raises(ValueError):
        nt.build_data(nt.STOCK_ALERT, product_id="p", amount=5)
    assert nt.PLATFORM_EVENTS <= nt.ALL_EVENTS and set(nt.EVENT_DATA_KEYS) == set(nt.ALL_EVENTS) - {nt.PUSH_TEST}


def test_concurrent_duplicate_is_skipped_not_raised(db):
    """Two sessions reporting the same transition: the unique index makes the second a no-op."""
    f = _firm(db)
    other = Session(bind=engine)
    try:
        first = notification_service.notify(db, f.sales.id, "A", event=nt.LEAD_ASSIGNED, dedupe_key="k:race")
        db.commit()
        # The second session's pre-check does not see it (simulated race): the index decides.
        original = notification_service._already_sent
        calls = {"n": 0}

        def blind_first(session, user_id, key):
            calls["n"] += 1
            return False if calls["n"] == 1 else original(session, user_id, key)

        notification_service._already_sent = blind_first
        try:
            second = notification_service.notify(other, f.sales.id, "A", event=nt.LEAD_ASSIGNED, dedupe_key="k:race")
            other.commit()
        finally:
            notification_service._already_sent = original
        assert first is not None and second is None
        assert len([n for n in _inbox(db, f.sales) if n.dedupe_key == "k:race"]) == 1
    finally:
        other.close()


def test_notifications_dedupe_migration_up_and_down(tmp_path):
    import importlib.util
    import pathlib

    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = (pathlib.Path(__file__).resolve().parents[1] / "alembic" / "versions"
            / "e1f2a3b4c5d6_notifications_dedupe_key.py")
    spec = importlib.util.spec_from_file_location("dedupe_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    mig_engine = sa.create_engine(f"sqlite:///{tmp_path / 'd.db'}")
    with mig_engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE notifications (id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36), "
                             "title VARCHAR(200))"))
        conn.execute(sa.text("INSERT INTO notifications (id, user_id, title) VALUES ('n1', 'u1', 'old'), "
                             "('n2', 'u1', 'old2')"))

    def run(step):
        with mig_engine.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                step()

    run(migration.upgrade)
    run(migration.upgrade)  # idempotent
    inspector = sa.inspect(mig_engine)
    assert "dedupe_key" in {c["name"] for c in inspector.get_columns("notifications")}
    [ix] = [i for i in inspector.get_indexes("notifications") if i["name"] == migration._INDEX]
    assert ix["unique"] and ix["column_names"] == ["user_id", "dedupe_key"]
    run(migration.downgrade)
    assert migration._INDEX not in {i["name"] for i in sa.inspect(mig_engine).get_indexes("notifications")}
    mig_engine.dispose()
