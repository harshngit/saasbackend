"""Phase 1 Lead workflow tests: creation validation, lead_source validation,
status transition rules (incl. manual 'won' prevention), Sales-Officer
self-assignment, converted-Lead protection (edit/delete), conversion
concurrency/atomicity, and GET /leads pagination + filters.

These exercise the fixes in app/services/lead_service.py, app/schemas/lead.py,
app/core/workflow.py (LEAD_TRANSITIONS) and app/core/reference_data.py
(LEAD_SOURCES) — see saas_changes.md for the corresponding write-up.
"""

import os
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient

from app.main import app
from app.core.database import SessionLocal
from app.models import Customer, Lead

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
    auth = {"Authorization": f"Bearer {login.json()['tokens']['access_token']}"}
    return user, auth


def _lead(**overrides):
    payload = {"name": "Test Lead", "mobile_number": "9000000000", "lead_source": "Website"}
    payload.update(overrides)
    return payload


# ============================================================================
# A. Creation validation
# ============================================================================

def run_creation_tests():
    print("\n=== A. Lead creation validation ===")
    auth = _register_org("LeadCreateA")

    check("empty body rejected (422)", client.post("/leads", json={}, headers=auth).status_code == 422)
    check("missing lead_source/mobile rejected (422)",
          client.post("/leads", json={"name": "ABC"}, headers=auth).status_code == 422)
    check("missing mobile_number rejected (422)",
          client.post("/leads", json={"name": "ABC", "lead_source": "Website"}, headers=auth).status_code == 422)
    check("missing name rejected (422)",
          client.post("/leads", json={"mobile_number": "9000000001", "lead_source": "Website"}, headers=auth).status_code == 422)

    r = client.post("/leads", json=_lead(), headers=auth)
    check("valid Lead creation succeeds (201)", r.status_code == 201, r.text)
    assert_eq(r.json()["lead_status"], "new", "Initial status is always 'new'")

    r_alias = client.post("/leads", json={"name": "Alias Lead", "mobile": "9000000002", "source": "Referral"}, headers=auth)
    check("mobile/source aliases satisfy the required fields (201)", r_alias.status_code == 201, r_alias.text)
    assert_eq(r_alias.json()["mobile_number"], "9000000002", "Aliased mobile_number persisted")
    assert_eq(r_alias.json()["lead_source"], "Referral", "Aliased lead_source persisted")

    r_bad_source = client.post("/leads", json=_lead(lead_source="Not A Real Source"), headers=auth)
    check("invalid lead_source rejected (422)", r_bad_source.status_code == 422, r_bad_source.text)

    for src in ("Website", "referral", "GOOGLE", "cold call", "walk-in"):
        r_src = client.post("/leads", json=_lead(mobile_number=f"90000{uuid.uuid4().hex[:5]}", lead_source=src), headers=auth)
        check(f"lead_source '{src}' accepted (case/separator-insensitive)", r_src.status_code == 201, r_src.text)

    for bad_status in ("won", "contacted", "qualified", "lost"):
        r_status = client.post("/leads", json=_lead(mobile_number=f"91000{uuid.uuid4().hex[:5]}", lead_status=bad_status), headers=auth)
        check(f"create directly as '{bad_status}' rejected (422)", r_status.status_code == 422, r_status.text)

    r_explicit_new = client.post("/leads", json=_lead(mobile_number="9111111111", lead_status="new"), headers=auth)
    check("explicit lead_status='new' at creation is fine", r_explicit_new.status_code == 201, r_explicit_new.text)


# ============================================================================
# B. Status workflow
# ============================================================================

def run_status_workflow_tests():
    print("\n=== B. Status transition workflow ===")
    auth = _register_org("LeadStatusB")

    def new_lead(mobile):
        return client.post("/leads", json=_lead(mobile_number=mobile), headers=auth).json()["id"]

    # Allowed forward transitions
    lid = new_lead("9200000001")
    check("new -> contacted allowed", client.patch(f"/leads/{lid}", json={"status": "contacted"}, headers=auth).status_code == 200)
    check("contacted -> qualified allowed", client.patch(f"/leads/{lid}", json={"status": "qualified"}, headers=auth).status_code == 200)
    check("qualified -> lost allowed", client.patch(f"/leads/{lid}", json={"status": "lost"}, headers=auth).status_code == 200)

    lid2 = new_lead("9200000002")
    check("new -> qualified allowed (skip contacted)", client.patch(f"/leads/{lid2}", json={"status": "qualified"}, headers=auth).status_code == 200)

    lid3 = new_lead("9200000003")
    check("new -> lost allowed", client.patch(f"/leads/{lid3}", json={"status": "lost"}, headers=auth).status_code == 200)

    # Manual 'won' forbidden from every non-terminal state
    for label, mobile, path in (
        ("new", "9200000010", []),
        ("contacted", "9200000011", ["contacted"]),
        ("qualified", "9200000012", ["contacted", "qualified"]),
    ):
        lid_won = new_lead(mobile)
        for step in path:
            client.patch(f"/leads/{lid_won}", json={"status": step}, headers=auth)
        r_won = client.patch(f"/leads/{lid_won}", json={"status": "won"}, headers=auth)
        check(f"PATCH status='won' from '{label}' is rejected (400)", r_won.status_code == 400, r_won.text)

    # Invalid arbitrary status value
    lid4 = new_lead("9200000020")
    r_bad = client.patch(f"/leads/{lid4}", json={"status": "banana"}, headers=auth)
    check("invalid status value rejected (400)", r_bad.status_code == 400, r_bad.text)

    # lost is terminal: no transition out (documented, conservative default)
    lid5 = new_lead("9200000021")
    client.patch(f"/leads/{lid5}", json={"status": "lost"}, headers=auth)
    r_reopen = client.patch(f"/leads/{lid5}", json={"status": "new"}, headers=auth)
    check("lost -> new rejected (terminal, no reopen flow defined)", r_reopen.status_code == 400, r_reopen.text)

    # won -> anything, via a real conversion, is covered in run_converted_protection_tests()


# ============================================================================
# C. Assignment permissions
# ============================================================================

def run_assignment_tests():
    print("\n=== C. Assignment permissions ===")
    admin_auth = _register_org("LeadAssignC")
    so1, so1_auth = _create_staff(admin_auth, "Officer One", "Sales Officer")
    so2, so2_auth = _create_staff(admin_auth, "Officer Two", "Sales Officer")
    acct, acct_auth = _create_staff(admin_auth, "Acc One", "Accountant")
    delp, delp_auth = _create_staff(admin_auth, "Del One", "Delivery Partner")

    # Sales Officer: no assignee -> self
    r = client.post("/leads", json=_lead(mobile_number="9300000001"), headers=so1_auth)
    check("SO create without assignee -> self-assigned (201)", r.status_code == 201, r.text)
    assert_eq(r.json()["assigned_salesperson_id"], so1["id"], "assigned_salesperson_id is the creating SO")

    # Sales Officer: explicit self -> allowed
    r = client.post("/leads", json=_lead(mobile_number="9300000002", assigned_salesperson_id=so1["id"]), headers=so1_auth)
    check("SO create assigned to self explicitly -> allowed (201)", r.status_code == 201, r.text)

    # Sales Officer: explicit another SO -> rejected
    r = client.post("/leads", json=_lead(mobile_number="9300000003", assigned_salesperson_id=so2["id"]), headers=so1_auth)
    check("SO create assigned to another SO -> rejected (403)", r.status_code == 403, r.text)

    # Sales Officer: update to reassign -> rejected
    own_lead_id = client.post("/leads", json=_lead(mobile_number="9300000004"), headers=so1_auth).json()["id"]
    r = client.patch(f"/leads/{own_lead_id}", json={"assigned_salesperson_id": so2["id"]}, headers=so1_auth)
    check("SO PATCH reassign to another user -> rejected (403)", r.status_code == 403, r.text)
    r = client.patch(f"/leads/{own_lead_id}", json={"assigned_salesperson_id": so1["id"]}, headers=so1_auth)
    check("SO PATCH reassign to self (no-op) -> allowed (200)", r.status_code == 200, r.text)

    # Admin: unassigned lead
    r = client.post("/leads", json=_lead(mobile_number="9300000005"), headers=admin_auth)
    check("Admin create unassigned Lead -> allowed (201)", r.status_code == 201, r.text)
    assert_eq(r.json()["assigned_salesperson_id"], None, "Admin-created Lead is unassigned by default")

    # Admin: assign to a Sales Officer
    admin_lead_id = r.json()["id"]
    r = client.patch(f"/leads/{admin_lead_id}", json={"assigned_salesperson_id": so1["id"]}, headers=admin_auth)
    check("Admin assigns Lead to a Sales Officer -> allowed (200)", r.status_code == 200, r.text)

    # Admin: reassign freely
    r = client.patch(f"/leads/{admin_lead_id}", json={"assigned_salesperson_id": so2["id"]}, headers=admin_auth)
    check("Admin reassigns Lead to a different Sales Officer -> allowed (200)", r.status_code == 200, r.text)

    # Invalid assignee: nonexistent user
    r = client.post("/leads", json=_lead(mobile_number="9300000006", assigned_salesperson_id="not-a-real-id"), headers=admin_auth)
    check("Nonexistent assignee rejected (400)", r.status_code == 400, r.text)

    # Invalid assignee: cross-org user
    other_admin_auth = _register_org("LeadAssignCOther")
    other_so, _ = _create_staff(other_admin_auth, "Foreign Officer", "Sales Officer")
    r = client.post("/leads", json=_lead(mobile_number="9300000007", assigned_salesperson_id=other_so["id"]), headers=admin_auth)
    check("Cross-org assignee rejected (400)", r.status_code == 400, r.text)

    # Invalid assignee: role without Leads access
    r = client.post("/leads", json=_lead(mobile_number="9300000008", assigned_salesperson_id=acct["id"]), headers=admin_auth)
    check("Accountant (no Leads access) as assignee rejected (400)", r.status_code == 400, r.text)
    r = client.post("/leads", json=_lead(mobile_number="9300000009", assigned_salesperson_id=delp["id"]), headers=admin_auth)
    check("Delivery Partner (no Leads access) as assignee rejected (400)", r.status_code == 400, r.text)


# ============================================================================
# D. Converted Lead protection
# ============================================================================

def run_converted_protection_tests():
    print("\n=== D. Converted Lead protection ===")
    auth = _register_org("LeadConvertD")
    lid = client.post("/leads", json=_lead(mobile_number="9400000001"), headers=auth).json()["id"]
    conv = client.post(f"/leads/{lid}/convert-to-customer", json={}, headers=auth)
    check("Conversion succeeds", conv.status_code == 200, conv.text)

    r = client.patch(f"/leads/{lid}", json={"status": "new"}, headers=auth)
    check("won -> new via PATCH rejected (400)", r.status_code == 400, r.text)
    r = client.patch(f"/leads/{lid}", json={"status": "contacted"}, headers=auth)
    check("won -> contacted via PATCH rejected (400)", r.status_code == 400, r.text)
    r = client.patch(f"/leads/{lid}", json={"status": "qualified"}, headers=auth)
    check("won -> qualified via PATCH rejected (400)", r.status_code == 400, r.text)
    r = client.patch(f"/leads/{lid}", json={"status": "lost"}, headers=auth)
    check("won -> lost via PATCH rejected (400)", r.status_code == 400, r.text)

    r = client.patch(f"/leads/{lid}", json={"name": "Renamed"}, headers=auth)
    check("PATCH core field on converted Lead rejected (400)", r.status_code == 400, r.text)
    r = client.patch(f"/leads/{lid}", json={"customer_id": "someone-else"}, headers=auth)
    check("PATCH customer_id on converted Lead rejected (400)", r.status_code == 400, r.text)

    r = client.delete(f"/leads/{lid}", headers=auth)
    check("DELETE converted Lead rejected (400)", r.status_code == 400, r.text)

    still_there = client.get(f"/leads/{lid}", headers=auth)
    check("Converted Lead still exists after the rejected delete", still_there.status_code == 200)

    # customer_id is always protected, even pre-conversion.
    lid2 = client.post("/leads", json=_lead(mobile_number="9400000002"), headers=auth).json()["id"]
    r = client.patch(f"/leads/{lid2}", json={"customer_id": "bogus-id"}, headers=auth)
    check("PATCH customer_id on an unconverted Lead is also rejected (400)", r.status_code == 400, r.text)


# ============================================================================
# E. Conversion correctness + rollback
# ============================================================================

def run_conversion_tests():
    print("\n=== E. Conversion correctness & atomic rollback ===")
    auth = _register_org("LeadConvertE")
    lid = client.post("/leads", json=_lead(mobile_number="9500000001", email="prospect@example.com"), headers=auth).json()["id"]

    r = client.post(f"/leads/{lid}/convert-to-customer", json={"billing_address": "1 Main St"}, headers=auth)
    check("Normal conversion succeeds (200)", r.status_code == 200, r.text)
    data = r.json()
    check("Customer created", data.get("customer") is not None, data)
    cust_id = data["customer_id"]

    lead_after = client.get(f"/leads/{lid}", headers=auth).json()
    assert_eq(lead_after["customer_id"], cust_id, "Lead.customer_id populated")
    check("converted_at populated", lead_after["converted_at"] is not None, lead_after)
    assert_eq(lead_after["lead_status"], "won", "Lead status becomes 'won'")

    r_again = client.post(f"/leads/{lid}/convert-to-customer", json={}, headers=auth)
    check("Sequential repeat conversion succeeds idempotently (200)", r_again.status_code == 200, r_again.text)
    assert_eq(r_again.json()["customer_id"], cust_id, "Repeat conversion returns the same customer_id")

    db = SessionLocal()
    matching = db.query(Customer).filter(Customer.id == cust_id).count()
    assert_eq(matching, 1, "Exactly one Customer row exists for this Lead")
    db.close()

    # --- Rollback: force a failure after the Customer has been flushed but
    # before the transaction commits, and verify nothing partial persists. ---
    lid_fail = client.post("/leads", json=_lead(mobile_number="9500000002"), headers=auth).json()["id"]

    import app.services.lead_service as lead_service_module

    class _BoomDatetime:
        @staticmethod
        def now(tz=None):
            raise RuntimeError("Simulated failure after Customer flush, before commit")

    failing_client = TestClient(app, raise_server_exceptions=False)
    original_datetime = lead_service_module.datetime
    lead_service_module.datetime = _BoomDatetime
    try:
        r_fail = failing_client.post(f"/leads/{lid_fail}/convert-to-customer", json={}, headers=auth)
    finally:
        lead_service_module.datetime = original_datetime

    check("Simulated mid-conversion failure surfaces as a server error, not a 200", r_fail.status_code >= 500, r_fail.text[:200])

    db = SessionLocal()
    lead_row = db.get(Lead, lid_fail)
    check("Lead.customer_id is still None after the rollback", lead_row.customer_id is None)
    check("Lead.lead_status is still 'new' after the rollback", lead_row.lead_status == "new")
    check("Lead.converted_at is still None after the rollback", lead_row.converted_at is None)
    orphan_customers = db.query(Customer).filter(Customer.name == f"Lead {lead_row.lead_id}").count()
    check("No orphan Customer row was left behind by the failed conversion", orphan_customers == 0)
    db.close()

    # A normal conversion still works on the same Lead afterwards — proves
    # the failed attempt left no partial state blocking a real retry.
    r_retry = client.post(f"/leads/{lid_fail}/convert-to-customer", json={}, headers=auth)
    check("Lead is still convertible after the rolled-back attempt (200)", r_retry.status_code == 200, r_retry.text)


# ============================================================================
# F. Concurrent conversion — critical
# ============================================================================

def run_concurrent_conversion_test():
    print("\n=== F. Concurrent conversion (critical) ===")
    auth = _register_org("LeadConcurrentF")
    lid = client.post("/leads", json=_lead(mobile_number="9600000001"), headers=auth).json()["id"]

    results = {}
    errors = {}

    def fire(label):
        try:
            r = client.post(f"/leads/{lid}/convert-to-customer", json={}, headers=auth)
            results[label] = r.status_code
        except Exception as exc:  # TestClient re-raises unhandled server exceptions
            errors[label] = f"{type(exc).__name__}: {exc}"

    threads = [threading.Thread(target=fire, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    print(f"  Concurrent conversion attempts: 8, results={results}, errors={errors}")
    check("No concurrent request raised an unhandled exception", len(errors) == 0, errors)
    check("No concurrent request returned HTTP 500", all(v < 500 for v in results.values()), results)
    check("Every concurrent request got a 200 (idempotent convert)", all(v == 200 for v in results.values()), results)

    db = SessionLocal()
    lead_row = db.get(Lead, lid)
    customers_for_lead = db.query(Customer).filter(Customer.id == lead_row.customer_id).count() if lead_row.customer_id else 0
    total_matching_name_customers = db.query(Customer).filter(Customer.organization_id == lead_row.organization_id).count()
    check("Lead.customer_id is set after the race", lead_row.customer_id is not None, lead_row.customer_id)
    check("Lead.lead_status is 'won' after the race", lead_row.lead_status == "won")
    check("Exactly one Customer row exists for the converted Lead", customers_for_lead == 1, customers_for_lead)
    print(f"  Customers created: {total_matching_name_customers} (expected 1); Lead.status={lead_row.lead_status}")
    db.close()


# ============================================================================
# G. Pagination and filters
# ============================================================================

def run_pagination_and_filter_tests():
    print("\n=== G. Pagination & filters ===")
    admin_auth = _register_org("LeadPageG")
    so1, so1_auth = _create_staff(admin_auth, "Page Officer", "Sales Officer")

    for i in range(12):
        client.post("/leads", json=_lead(
            mobile_number=f"97000000{i:02d}",
            name=f"Page Lead {i}",
            lead_source="Website" if i % 2 == 0 else "Referral",
        ), headers=admin_auth)

    r = client.get("/leads", headers=admin_auth)
    check("Default GET /leads (no params) still works", r.status_code == 200 and len(r.json()) >= 12, r.text[:200])

    r = client.get("/leads", headers=admin_auth, params={"limit": 5, "offset": 0})
    check("limit=5 returns exactly 5", r.status_code == 200 and len(r.json()) == 5, r.text[:200])
    page1_ids = [x["id"] for x in r.json()]

    r2 = client.get("/leads", headers=admin_auth, params={"limit": 5, "offset": 5})
    page2_ids = [x["id"] for x in r2.json()]
    check("Second page doesn't repeat the first page's rows", not set(page1_ids) & set(page2_ids), (page1_ids, page2_ids))

    r_over = client.get("/leads", headers=admin_auth, params={"limit": 5000})
    check("limit beyond the maximum is rejected (422)", r_over.status_code == 422, r_over.text[:200])

    r_status = client.get("/leads", headers=admin_auth, params={"status": "new"})
    check("status filter returns only 'new' leads",
          r_status.status_code == 200 and all(x["lead_status"] == "new" for x in r_status.json()), r_status.text[:200])

    r_bad_status = client.get("/leads", headers=admin_auth, params={"status": "not-a-status"})
    check("invalid status filter value rejected (400)", r_bad_status.status_code == 400, r_bad_status.text[:200])

    r_assignee = client.get("/leads", headers=admin_auth, params={"assigned_salesperson_id": "nobody"})
    check("assignee filter with no matches returns an empty list", r_assignee.status_code == 200 and r_assignee.json() == [], r_assignee.text[:200])

    r_source = client.get("/leads", headers=admin_auth, params={"lead_source": "Website"})
    check("lead_source filter returns only matching leads",
          r_source.status_code == 200 and all(x["lead_source"] == "Website" for x in r_source.json()) and len(r_source.json()) >= 6,
          r_source.text[:200])

    r_search = client.get("/leads", headers=admin_auth, params={"search": "Page Lead 3"})
    check("search matches by name", r_search.status_code == 200 and any(x["name"] == "Page Lead 3" for x in r_search.json()), r_search.text[:200])

    r_search_mobile = client.get("/leads", headers=admin_auth, params={"search": "9700000005"})
    check("search matches by mobile_number", r_search_mobile.status_code == 200 and len(r_search_mobile.json()) == 1, r_search_mobile.text[:200])

    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    r_future = client.get("/leads", headers=admin_auth, params={"created_from": future})
    check("created_from in the future returns no leads", r_future.status_code == 200 and r_future.json() == [], r_future.text[:200])

    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    r_past = client.get("/leads", headers=admin_auth, params={"created_from": past})
    check("created_from in the past still returns leads", r_past.status_code == 200 and len(r_past.json()) >= 12, r_past.text[:200])

    # Organization isolation still holds with the new query params.
    other_admin_auth = _register_org("LeadPageGOther")
    r_other = client.get("/leads", headers=other_admin_auth, params={"limit": 500})
    check("A different org sees none of these leads", r_other.status_code == 200 and r_other.json() == [], r_other.text[:200])

    # Sales Officer own-scope filtering remains correct alongside pagination.
    client.post("/leads", json=_lead(mobile_number="9800000001"), headers=so1_auth)
    r_own = client.get("/leads", headers=so1_auth, params={"limit": 500})
    check("Sales Officer only sees their own leads even with pagination params",
          r_own.status_code == 200 and all(x["assigned_salesperson_id"] == so1["id"] for x in r_own.json()), r_own.text[:200])


def run_all_tests():
    run_creation_tests()
    run_status_workflow_tests()
    run_assignment_tests()
    run_converted_protection_tests()
    run_conversion_tests()
    run_concurrent_conversion_test()
    run_pagination_and_filter_tests()

    print("\n=======================================================")
    print(f"RESULTS: {_passed} passed, {_failed} failed")
    print("=======================================================\n")
    if _failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_all_tests()
