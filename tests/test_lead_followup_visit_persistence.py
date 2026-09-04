"""Leads backend addendum: Lead Follow-up + Visit persistence before Customer
conversion.

Exercises the new FollowUp.lead_id column (app/models/follow_up.py),
follow_up_service.create_follow_up's direct-lead support, visit_service's
Lead-ownership/default-assignee fix, and lead_service.convert_lead_to_customer's
extended history propagation to Visit/FollowUp (mirroring the existing
Quotation propagation).
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient

from app.main import app
from app.core.database import SessionLocal
from app.models import FollowUp, Visit

client = TestClient(app)

_passed = 0
_failed = 0


def ok(msg: str):
    global _passed
    _passed += 1
    print(f"  PASS  {msg}")


def fail(msg: str, detail: str = ""):
    global _failed
    _failed += 1
    print(f"  FAIL  {msg}  {detail}")


def check(msg: str, condition: bool, detail: str = ""):
    if condition:
        ok(msg)
    else:
        fail(msg, detail)


def assert_eq(actual, expected, msg: str):
    check(msg, actual == expected, f"Expected {expected!r}, got {actual!r}")


def _register_org(label: str):
    email = f"admin_{uuid.uuid4().hex[:8]}@{label.lower()}.com"
    r = client.post("/auth/register", json={
        "organization_name": f"{label} {uuid.uuid4().hex[:6]}",
        "admin_name": "Admin User",
        "email": email,
        "password": "Password123!",
        "role": "admin",
    })
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}


def _create_staff(admin_auth: dict, name: str, role_name: str):
    email = f"{role_name.lower().replace(' ', '_')}_{uuid.uuid4().hex[:6]}@example.com"
    r = client.post("/users", json={
        "name": name, "email": email, "password": "Password123!", "role": role_name,
    }, headers=admin_auth)
    assert r.status_code == 201, r.text
    user = r.json()
    login = client.post("/auth/login", json={"email": email, "password": "Password123!"})
    assert login.status_code == 200, login.text
    return user, {"Authorization": f"Bearer {login.json()['tokens']['access_token']}"}


def _lead(auth, **overrides):
    payload = {"name": "FU Lead", "mobile_number": f"9{uuid.uuid4().hex[:9]}", "lead_source": "Website"}
    payload.update(overrides)
    r = client.post("/leads", json=payload, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


# ============================================================================
# 1/2/3. Admin creates Follow-up on Lead; assigned SO retrieves it; persistence
# ============================================================================

def run_admin_creates_followup_so_retrieves():
    print("\n=== Admin creates Lead Follow-up; assigned Sales Officer retrieves it ===")
    admin_auth = _register_org("FuLeadA")
    so, so_auth = _create_staff(admin_auth, "Officer One", "Sales Officer")
    lead = _lead(admin_auth, assigned_salesperson_id=so["id"])
    assert_eq(lead["assigned_salesperson_id"], so["id"], "Lead assigned to SO")

    r = client.post("/follow-ups", json={
        "lead_id": lead["id"],
        "title": "Call regarding pricing",
        "due_date": "2026-09-05T00:00:00Z",
        "notes": "n/a",
    }, headers=admin_auth)
    check("Admin creates Follow-up directly on Lead (no customer_id, no visit_id) -> 201", r.status_code == 201, r.text)
    fu = r.json()
    assert_eq(fu["lead_id"], lead["id"], "Follow-up.lead_id persisted")
    check("assigned_to_id defaults to the Lead's own salesperson, not the Admin",
          fu["assigned_to_id"] == so["id"], fu)

    # Session-independent persistence: a fresh DB read (not the same request/session).
    db = SessionLocal()
    row = db.get(FollowUp, fu["id"])
    assert_eq(row.lead_id, lead["id"], "DB row: lead_id persisted (fresh session)")
    assert_eq(row.customer_id, None, "DB row: customer_id is None (no customer required)")
    db.close()

    r_so_get = client.get(f"/follow-ups/{fu['id']}", headers=so_auth)
    check("Assigned Sales Officer (different login/session) can GET the Follow-up", r_so_get.status_code == 200, r_so_get.text)

    r_so_list = client.get(f"/follow-ups?lead_id={lead['id']}", headers=so_auth)
    check("Assigned Sales Officer can list Follow-ups by lead_id", r_so_list.status_code == 200 and len(r_so_list.json()) == 1, r_so_list.text)

    r_admin_list = client.get(f"/follow-ups?lead_id={lead['id']}", headers=admin_auth)
    check("Admin can list the same Follow-up by lead_id", r_admin_list.status_code == 200 and len(r_admin_list.json()) == 1, r_admin_list.text)


# ============================================================================
# 4/5. Lead follow-up without customer_id; cross-org lead_id rejected
# ============================================================================

def run_validation_tests():
    print("\n=== Follow-up validation: no customer_id required, cross-org rejected ===")
    auth_a = _register_org("FuLeadValA")
    auth_b = _register_org("FuLeadValB")
    lead_b = _lead(auth_b)

    r_no_cust = client.post("/follow-ups", json={
        "lead_id": _lead(auth_a)["id"], "title": "No customer needed", "due_date": "2026-09-05T00:00:00Z",
    }, headers=auth_a)
    check("Follow-up on a Lead with no customer_id succeeds (201)", r_no_cust.status_code == 201, r_no_cust.text)

    r_cross = client.post("/follow-ups", json={
        "lead_id": lead_b["id"], "title": "Cross-org attempt", "due_date": "2026-09-05T00:00:00Z",
    }, headers=auth_a)
    check("Cross-org lead_id rejected (400, same 'Invalid lead_id' contract every other referenced-FK check uses)",
          r_cross.status_code == 400, r_cross.text)

    r_neither = client.post("/follow-ups", json={"title": "No parent", "due_date": "2026-09-05T00:00:00Z"}, headers=auth_a)
    check("Neither customer_id nor lead_id -> 400", r_neither.status_code == 400, r_neither.text)


# ============================================================================
# 6. Unauthorized Sales Officer cannot access another Lead's Follow-up
# ============================================================================

def run_cross_officer_security_test():
    print("\n=== Unauthorized Sales Officer cannot access another Lead's Follow-up ===")
    admin_auth = _register_org("FuLeadSecA")
    so1, so1_auth = _create_staff(admin_auth, "Officer One", "Sales Officer")
    so2, so2_auth = _create_staff(admin_auth, "Officer Two", "Sales Officer")
    lead = _lead(admin_auth, assigned_salesperson_id=so1["id"])

    fu = client.post("/follow-ups", json={
        "lead_id": lead["id"], "title": "SO1 only", "due_date": "2026-09-05T00:00:00Z",
    }, headers=admin_auth).json()
    assert_eq(fu["assigned_to_id"], so1["id"], "Defaulted to SO1")

    check("SO2 GET SO1's Lead Follow-up -> 404", client.get(f"/follow-ups/{fu['id']}", headers=so2_auth).status_code == 404)
    r_so2_list = client.get(f"/follow-ups?lead_id={lead['id']}", headers=so2_auth)
    check("SO2 list filtered by lead_id returns nothing (data-scope excludes it)",
          r_so2_list.status_code == 200 and r_so2_list.json() == [], r_so2_list.text)

    # SO2 cannot even create a Lead-linked follow-up for a Lead they don't own.
    r_so2_create = client.post("/follow-ups", json={
        "lead_id": lead["id"], "title": "SO2 tries", "due_date": "2026-09-05T00:00:00Z",
    }, headers=so2_auth)
    check("SO2 cannot create a Follow-up against SO1's Lead (400, 'Invalid lead_id')",
          r_so2_create.status_code == 400, r_so2_create.text)


# ============================================================================
# 7/8/9. Visit on a Lead: admin creates, SO retrieves, no customer_id required
# ============================================================================

def run_visit_tests():
    print("\n=== Visit on a Lead ===")
    admin_auth = _register_org("FuLeadVisitA")
    so, so_auth = _create_staff(admin_auth, "Visit Officer", "Sales Officer")
    lead = _lead(admin_auth, assigned_salesperson_id=so["id"])

    r = client.post("/visits", json={
        "lead_id": lead["id"], "visit_date": "2026-09-06T00:00:00Z", "notes": "Initial site visit",
    }, headers=admin_auth)
    check("Admin creates Visit on Lead with no customer_id -> 201", r.status_code == 201, r.text)
    visit = r.json()
    assert_eq(visit["lead_id"], lead["id"], "Visit.lead_id persisted")
    assert_eq(visit["customer_id"], None, "Visit.customer_id is None")
    check("Visit user_id defaults to the Lead's own salesperson", visit["user_id"] == so["id"], visit)

    r_so_get = client.get(f"/visits/{visit['id']}", headers=so_auth)
    check("Assigned Sales Officer can GET the Visit", r_so_get.status_code == 200, r_so_get.text)

    r_list = client.get(f"/visits?lead_id={lead['id']}", headers=so_auth)
    check("Assigned Sales Officer can list Visits by lead_id", r_list.status_code == 200 and len(r_list.json()) == 1, r_list.text)


# ============================================================================
# 10. Follow-up linked to a Visit still works
# ============================================================================

def run_visit_followup_link_test():
    print("\n=== Follow-up linked to a Visit still works ===")
    admin_auth = _register_org("FuLeadVisitLinkA")
    lead = _lead(admin_auth)
    visit = client.post("/visits", json={"lead_id": lead["id"]}, headers=admin_auth).json()

    r = client.post(f"/visits/{visit['id']}/follow-ups", json={
        "title": "Follow up after visit", "due_date": "2026-09-07T00:00:00Z",
    }, headers=admin_auth)
    check("POST /visits/{id}/follow-ups still works -> 201", r.status_code == 201, r.text)
    fu = r.json()
    assert_eq(fu["visit_id"], visit["id"], "Follow-up.visit_id set")
    assert_eq(fu["lead_id"], lead["id"], "Follow-up.lead_id derived from the Visit's lead_id")
    assert_eq(fu["customer_id"], None, "Follow-up.customer_id still None (lead-only visit)")


# ============================================================================
# 11/12. Lead converts to Customer -> old Follow-ups/Visits remain
# ============================================================================

def run_conversion_preserves_activity_test():
    print("\n=== Lead conversion preserves Follow-ups and Visits ===")
    admin_auth = _register_org("FuLeadConvA")
    lead = _lead(admin_auth)

    fu = client.post("/follow-ups", json={
        "lead_id": lead["id"], "title": "Pre-conversion follow-up", "due_date": "2026-09-05T00:00:00Z",
    }, headers=admin_auth).json()
    visit = client.post("/visits", json={"lead_id": lead["id"], "notes": "Pre-conversion visit"}, headers=admin_auth).json()

    conv = client.post(f"/leads/{lead['id']}/convert-to-customer", json={}, headers=admin_auth)
    check("Lead converts to Customer", conv.status_code == 200, conv.text)
    new_customer_id = conv.json()["customer_id"]

    fu_after = client.get(f"/follow-ups/{fu['id']}", headers=admin_auth)
    check("Follow-up still exists and is readable after conversion (200)", fu_after.status_code == 200, fu_after.text)
    assert_eq(fu_after.json()["lead_id"], lead["id"], "Follow-up.lead_id preserved after conversion")
    assert_eq(fu_after.json()["customer_id"], new_customer_id, "Follow-up.customer_id auto-linked to the new Customer")

    visit_after = client.get(f"/visits/{visit['id']}", headers=admin_auth)
    check("Visit still exists and is readable after conversion (200)", visit_after.status_code == 200, visit_after.text)
    assert_eq(visit_after.json()["lead_id"], lead["id"], "Visit.lead_id preserved after conversion")
    assert_eq(visit_after.json()["customer_id"], new_customer_id, "Visit.customer_id auto-linked to the new Customer")

    # Fresh DB session — proves real persistence, not identity-map residue.
    db = SessionLocal()
    fu_row = db.get(FollowUp, fu["id"])
    visit_row = db.get(Visit, visit["id"])
    assert_eq(fu_row.customer_id, new_customer_id, "DB row: Follow-up.customer_id set")
    assert_eq(fu_row.lead_id, lead["id"], "DB row: Follow-up.lead_id still set")
    assert_eq(visit_row.customer_id, new_customer_id, "DB row: Visit.customer_id set")
    assert_eq(visit_row.lead_id, lead["id"], "DB row: Visit.lead_id still set")
    db.close()

    # Now also retrievable via the Customer-centric filters, without losing lead_id.
    r_by_customer = client.get(f"/follow-ups?customer_id={new_customer_id}", headers=admin_auth)
    check("Follow-up now also findable via customer_id filter", any(x["id"] == fu["id"] for x in r_by_customer.json()), r_by_customer.text)


# ============================================================================
# 13/14. Existing Customer Follow-up / Visit flow regression
# ============================================================================

def run_customer_flow_regression_test():
    print("\n=== Existing Customer Follow-up/Visit flow regression ===")
    admin_auth = _register_org("FuCustRegA")
    cust = client.post("/customers", json={"name": "Regular Customer", "phone": "9888877776"}, headers=admin_auth).json()

    r_fu = client.post("/follow-ups", json={
        "customer_id": cust["id"], "title": "Customer follow-up", "due_date": "2026-09-05T00:00:00Z",
    }, headers=admin_auth)
    check("Customer-only Follow-up creation still works (201)", r_fu.status_code == 201, r_fu.text)
    assert_eq(r_fu.json()["lead_id"], None, "lead_id is None for a customer follow-up")
    assert_eq(r_fu.json()["customer_id"], cust["id"], "customer_id set as before")

    r_visit = client.post("/visits", json={"customer_id": cust["id"], "purpose": "Regular visit"}, headers=admin_auth)
    check("Customer-only Visit creation still works (201)", r_visit.status_code == 201, r_visit.text)
    assert_eq(r_visit.json()["lead_id"], None, "Visit.lead_id is None for a customer visit")

    r_list = client.get(f"/follow-ups?customer_id={cust['id']}", headers=admin_auth)
    check("Customer follow-up list filter still works", len(r_list.json()) == 1, r_list.text)

    r_complete = client.post(f"/follow-ups/{r_fu.json()['id']}/complete", headers=admin_auth)
    check("Complete follow-up still works", r_complete.status_code == 200 and r_complete.json()["status"] == "completed", r_complete.text)


def run_all_tests():
    run_admin_creates_followup_so_retrieves()
    run_validation_tests()
    run_cross_officer_security_test()
    run_visit_tests()
    run_visit_followup_link_test()
    run_conversion_preserves_activity_test()
    run_customer_flow_regression_test()

    print("\n=======================================================")
    print(f"RESULTS: {_passed} passed, {_failed} failed")
    print("=======================================================\n")
    if _failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_all_tests()
