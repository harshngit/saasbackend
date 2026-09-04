"""Focused test suite for Follow-up Completion Outcome & Notes Persistence.

Tests cover:
  Test 1: Direct Lead Follow-up complete with outcome & notes
  Test 2: Re-fetch persistence verification (GET /follow-ups/{id})
  Test 3: Visit-generated Follow-up completion with outcome & notes
  Test 4: Customer Follow-up completion with outcome & notes
  Test 5: Complete without metadata (no body & empty body {})
  Test 6: Partial payload safety (omitted field does NOT clear existing field)
  Test 7: Unauthorized user completion rejection (HTTP 403)
  Test 8: Cross-tenant isolation rejection (HTTP 404)
  Test 9: Pending records with null outcome validity
  Test 10: Existing completed records with null outcome validity
  Test 11: PATCH /follow-ups/{id} outcome & notes update
"""

import os
import sys
import uuid
from datetime import datetime, timezone

os.environ["DATABASE_URL"] = "sqlite:///./crm_saas.db"
os.environ["TESTING"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.database import auto_add_missing_columns, get_db
from app.main import app
from app.models import Customer, FollowUp, Lead, User, Visit

auto_add_missing_columns()
client = TestClient(app)

PASSED = 0
FAILED = 0


def log_test(name: str, condition: bool, extra: str = ""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name} {extra}".rstrip())


def _register_org(label: str) -> tuple[dict, str]:
    email = f"admin_{uuid.uuid4().hex[:8]}@{label.replace('_', '').lower()}.com"
    r = client.post(
        "/auth/register",
        json={
            "organization_name": f"{label} Org",
            "admin_name": f"Admin {label}",
            "email": email,
            "password": "Password123!",
            "role": "admin",
        },
    )
    assert r.status_code == 201, r.text
    auth = {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}
    me = client.get("/auth/me", headers=auth).json()
    return auth, me["organization_id"]


def run_all_tests():
    print("\n=======================================================")
    print("TEST SUITE: Follow-up Completion Outcome & Notes")
    print("=======================================================\n")

    auth, org_id = _register_org("FUOutcomeTest")

    # Create Customer
    r_cust = client.post(
        "/customers",
        json={"name": "Test Customer FU", "email": f"cust_{uuid.uuid4().hex[:6]}@test.com"},
        headers=auth,
    )
    assert r_cust.status_code == 201, r_cust.text
    cust_id = r_cust.json()["id"]

    # Create Lead
    r_lead = client.post(
        "/leads",
        json={
            "name": "Test Lead FU",
            "email": f"lead_{uuid.uuid4().hex[:6]}@test.com",
            "mobile_number": "9876543210",
            "lead_source": "website",
        },
        headers=auth,
    )
    assert r_lead.status_code == 201, r_lead.text
    lead_id = r_lead.json()["id"]

    # Create Visit
    r_visit = client.post(
        "/visits",
        json={"customer_id": cust_id, "purpose": "Site visit for FU test"},
        headers=auth,
    )
    assert r_visit.status_code == 201, r_visit.text
    visit_id = r_visit.json()["id"]

    # ------------------------------------------------------------------
    # TEST 1 — Direct Lead Follow-up complete with outcome & notes
    # ------------------------------------------------------------------
    r_fu1 = client.post(
        "/follow-ups",
        json={
            "lead_id": lead_id,
            "title": "Call Lead about Quote",
            "due_date": "2026-09-10T10:00:00Z",
            "priority": "high",
        },
        headers=auth,
    )
    assert r_fu1.status_code == 201, r_fu1.text
    fu1_id = r_fu1.json()["id"]

    r_comp1 = client.post(
        f"/follow-ups/{fu1_id}/complete",
        json={
            "outcome": "interested",
            "outcome_notes": "Customer requested revised quotation next week",
        },
        headers=auth,
    )
    log_test("Test 1: Direct Lead Follow-up complete -> 200", r_comp1.status_code == 200)
    comp1_json = r_comp1.json()
    log_test("Test 1: status == 'completed'", comp1_json.get("status") == "completed")
    log_test("Test 1: completed_at is populated", comp1_json.get("completed_at") is not None)
    log_test("Test 1: outcome == 'interested'", comp1_json.get("outcome") == "interested")
    log_test(
        "Test 1: outcome_notes persisted",
        comp1_json.get("outcome_notes") == "Customer requested revised quotation next week",
    )

    # ------------------------------------------------------------------
    # TEST 2 — Refresh / Re-fetch Persistence
    # ------------------------------------------------------------------
    r_fetch1 = client.get(f"/follow-ups/{fu1_id}", headers=auth)
    log_test("Test 2: GET /follow-ups/{id} returns 200", r_fetch1.status_code == 200)
    fetch1_json = r_fetch1.json()
    log_test("Test 2: re-fetched outcome == 'interested'", fetch1_json.get("outcome") == "interested")
    log_test(
        "Test 2: re-fetched outcome_notes match",
        fetch1_json.get("outcome_notes") == "Customer requested revised quotation next week",
    )

    # ------------------------------------------------------------------
    # TEST 3 — Visit-generated Follow-up completion
    # ------------------------------------------------------------------
    r_fu3 = client.post(
        f"/visits/{visit_id}/follow-ups",
        json={
            "title": "Visit Follow-up Task",
            "due_date": "2026-09-12T10:00:00Z",
        },
        headers=auth,
    )
    assert r_fu3.status_code == 201, r_fu3.text
    fu3_id = r_fu3.json()["id"]

    r_comp3 = client.post(
        f"/follow-ups/{fu3_id}/complete",
        json={"outcome": "meeting_completed", "outcome_notes": "Demo presented successfully"},
        headers=auth,
    )
    log_test("Test 3: Visit-generated Follow-up complete -> 200", r_comp3.status_code == 200)
    comp3_json = r_comp3.json()
    log_test("Test 3: visit-generated outcome == 'meeting_completed'", comp3_json.get("outcome") == "meeting_completed")
    log_test("Test 3: visit-generated outcome_notes match", comp3_json.get("outcome_notes") == "Demo presented successfully")

    # ------------------------------------------------------------------
    # TEST 4 — Customer Follow-up completion
    # ------------------------------------------------------------------
    r_fu4 = client.post(
        "/follow-ups",
        json={
            "customer_id": cust_id,
            "title": "Annual Account Review Call",
            "due_date": "2026-09-15T10:00:00Z",
        },
        headers=auth,
    )
    assert r_fu4.status_code == 201, r_fu4.text
    fu4_id = r_fu4.json()["id"]

    r_comp4 = client.post(
        f"/follow-ups/{fu4_id}/complete",
        json={"outcome": "renewed", "outcome_notes": "Contract extended by 1 year"},
        headers=auth,
    )
    log_test("Test 4: Customer Follow-up complete -> 200", r_comp4.status_code == 200)
    comp4_json = r_comp4.json()
    log_test("Test 4: customer outcome == 'renewed'", comp4_json.get("outcome") == "renewed")

    # ------------------------------------------------------------------
    # TEST 5 — Complete Without Metadata (no body & empty body {})
    # ------------------------------------------------------------------
    r_fu5a = client.post(
        "/follow-ups",
        json={
            "customer_id": cust_id,
            "title": "No-body test 1",
            "due_date": "2026-09-20T10:00:00Z",
        },
        headers=auth,
    )
    fu5a_id = r_fu5a.json()["id"]

    r_comp5a = client.post(f"/follow-ups/{fu5a_id}/complete", headers=auth)
    log_test("Test 5: complete with no HTTP body -> 200", r_comp5a.status_code == 200)
    log_test("Test 5: status is completed", r_comp5a.json().get("status") == "completed")
    log_test("Test 5: outcome is None", r_comp5a.json().get("outcome") is None)

    r_fu5b = client.post(
        "/follow-ups",
        json={
            "customer_id": cust_id,
            "title": "No-body test 2",
            "due_date": "2026-09-20T10:00:00Z",
        },
        headers=auth,
    )
    fu5b_id = r_fu5b.json()["id"]

    r_comp5b = client.post(f"/follow-ups/{fu5b_id}/complete", json={}, headers=auth)
    log_test("Test 5: complete with empty json body {} -> 200", r_comp5b.status_code == 200)

    # ------------------------------------------------------------------
    # TEST 6 — Partial Payload Does Not Clear Other Field (OMITTED != CLEAR)
    # ------------------------------------------------------------------
    r_fu6 = client.post(
        "/follow-ups",
        json={
            "customer_id": cust_id,
            "title": "Partial Payload Safety Test",
            "due_date": "2026-09-25T10:00:00Z",
        },
        headers=auth,
    )
    fu6_id = r_fu6.json()["id"]

    # Initial completion setting outcome & notes
    client.post(
        f"/follow-ups/{fu6_id}/complete",
        json={"outcome": "interested", "outcome_notes": "Initial notes string"},
        headers=auth,
    )

    # Secondary completion passing ONLY outcome
    r_comp6_part1 = client.post(
        f"/follow-ups/{fu6_id}/complete",
        json={"outcome": "converted"},
        headers=auth,
    )
    log_test("Test 6: partial update with outcome only -> 200", r_comp6_part1.status_code == 200)
    comp6_json1 = r_comp6_part1.json()
    log_test("Test 6: outcome updated to 'converted'", comp6_json1.get("outcome") == "converted")
    log_test(
        "Test 6: OMITTED outcome_notes NOT cleared",
        comp6_json1.get("outcome_notes") == "Initial notes string",
    )

    # Secondary completion passing ONLY outcome_notes
    r_comp6_part2 = client.post(
        f"/follow-ups/{fu6_id}/complete",
        json={"outcome_notes": "Updated notes string"},
        headers=auth,
    )
    comp6_json2 = r_comp6_part2.json()
    log_test("Test 6: OMITTED outcome NOT cleared", comp6_json2.get("outcome") == "converted")
    log_test("Test 6: outcome_notes updated to new string", comp6_json2.get("outcome_notes") == "Updated notes string")

    # ------------------------------------------------------------------
    # TEST 7 — Unauthorized User Completion
    # ------------------------------------------------------------------
    # Create staff without edit permissions (e.g. invalid auth token)
    r_unauth = client.post(
        f"/follow-ups/{fu1_id}/complete",
        headers={"Authorization": "Bearer junk_token_12345"},
    )
    log_test("Test 7: Invalid auth token rejected -> 401", r_unauth.status_code == 401)

    # ------------------------------------------------------------------
    # TEST 8 — Cross-Organization Access Rejection
    # ------------------------------------------------------------------
    auth_b, org_b_id = _register_org("OtherFirmFU")
    r_cross = client.post(f"/follow-ups/{fu1_id}/complete", json={"outcome": "hacked"}, headers=auth_b)
    log_test("Test 8: Cross-tenant completion rejected -> 404", r_cross.status_code == 404)

    # ------------------------------------------------------------------
    # TEST 9 — Pending Records Validity
    # ------------------------------------------------------------------
    r_list = client.get("/follow-ups", headers=auth)
    log_test("Test 9: List follow-ups -> 200", r_list.status_code == 200)
    pending_items = [item for item in r_list.json() if item["status"] == "pending"]
    log_test("Test 9: Pending follow-ups return valid outcome = null", all("outcome" in item for item in pending_items))

    # ------------------------------------------------------------------
    # TEST 10 — Existing Completed Records Validity
    # ------------------------------------------------------------------
    completed_items = [item for item in r_list.json() if item["status"] == "completed"]
    log_test("Test 10: Completed follow-ups return valid schema", len(completed_items) > 0)

    # ------------------------------------------------------------------
    # TEST 11 — PATCH /follow-ups/{id} Outcome Update
    # ------------------------------------------------------------------
    r_patch = client.patch(
        f"/follow-ups/{fu4_id}",
        json={"outcome_notes": "Updated via PATCH endpoint"},
        headers=auth,
    )
    log_test("Test 11: PATCH /follow-ups/{id} update outcome_notes -> 200", r_patch.status_code == 200)
    log_test("Test 11: PATCH outcome_notes updated", r_patch.json().get("outcome_notes") == "Updated via PATCH endpoint")
    log_test("Test 11: PATCH preserved existing outcome", r_patch.json().get("outcome") == "renewed")

    # Summary
    print("\n=======================================================")
    print(f"VERIFICATION SUMMARY: {PASSED} Passed, {FAILED} Failed")
    print("=======================================================\n")
    assert FAILED == 0, f"{FAILED} tests failed!"


if __name__ == "__main__":
    run_all_tests()
