"""Tests for two backend fixes:

FIX 1 — Follow-up creation from a lead-only Visit (no customer_id) must succeed
        when the Visit has a valid lead_id, instead of forcing customer_id.

FIX 2 — Method-specific payment details (upi_id, card_type, card_last_four,
        collection_instructions) must persist and be readable on re-fetch, for
        both POST /customers/{id}/payments and POST /payment-receipts.
"""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

os.environ["DATABASE_URL"] = "sqlite:///./crm_saas.db"
os.environ["TESTING"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app.main import app

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


def _register_org(label: str) -> dict:
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
    return {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}


def _create_sales_officer(admin_auth: dict) -> tuple[str, dict]:
    email = f"so_{uuid.uuid4().hex[:8]}@example.com"
    res = client.post(
        "/users",
        json={"name": "Sales Officer", "email": email, "password": "Password123!", "role": "Sales Officer"},
        headers=admin_auth,
    )
    assert res.status_code == 201, res.text
    user_id = res.json()["id"]
    login = client.post("/auth/login", json={"email": email, "password": "Password123!"})
    assert login.status_code == 200, login.text
    return user_id, {"Authorization": f"Bearer {login.json()['tokens']['access_token']}"}


# ==========================================================================
# FIX 1: Follow-up on a lead-only Visit
# ==========================================================================


def run_fix1_tests():
    print("\n=======================================================")
    print("FIX 1: Follow-up on Lead-only Visit")
    print("=======================================================\n")

    admin_auth = _register_org(f"LeadFU_{uuid.uuid4().hex[:6]}")
    so_id, so_auth = _create_sales_officer(admin_auth)
    other_admin_auth = _register_org(f"LeadFUOther_{uuid.uuid4().hex[:6]}")

    # ---- TEST A: Customer Visit -> Follow-up (existing behavior unchanged) ----
    print("--- TEST A: Customer Visit -> Follow-up ---")
    cust_res = client.post(
        "/customers", json={"name": "Cust A", "assigned_sales_officer_id": so_id}, headers=admin_auth
    )
    assert cust_res.status_code == 201, cust_res.text
    cust_id = cust_res.json()["id"]

    cust_visit = client.post(
        "/visits", json={"customer_id": cust_id, "purpose": "Site visit"}, headers=so_auth
    )
    assert cust_visit.status_code == 201, cust_visit.text
    cust_visit_id = cust_visit.json()["id"]

    cust_fu = client.post(
        f"/visits/{cust_visit_id}/follow-ups",
        json={
            "title": "Call customer",
            "due_date": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
            "assigned_to_id": so_id,
        },
        headers=so_auth,
    )
    log_test("Customer-visit follow-up still succeeds (201)", cust_fu.status_code == 201, cust_fu.text)
    log_test("Customer-visit follow-up carries customer_id", cust_fu.json()["customer_id"] == cust_id)
    log_test("Customer-visit follow-up carries visit_id", cust_fu.json()["visit_id"] == cust_visit_id)

    # ---- TEST B: Lead-only Visit -> Follow-up (the fix) ----
    print("\n--- TEST B: Lead-only Visit -> Follow-up ---")
    lead_res = client.post(
        "/leads",
        json={"name": "Prospect Co", "contact_person": "Raj", "assigned_salesperson_id": so_id},
        headers=admin_auth,
    )
    assert lead_res.status_code == 201, lead_res.text
    lead_id = lead_res.json()["id"]

    lead_visit = client.post(
        "/visits", json={"lead_id": lead_id, "purpose": "First meeting"}, headers=so_auth
    )
    log_test("Visit created for lead with no customer_id (201)", lead_visit.status_code == 201, lead_visit.text)
    lead_visit_id = lead_visit.json()["id"]
    log_test("Visit's customer_id is null", lead_visit.json()["customer_id"] is None)
    log_test("Visit's lead_id matches", lead_visit.json()["lead_id"] == lead_id)

    lead_fu = client.post(
        f"/visits/{lead_visit_id}/follow-ups",
        json={
            "title": "Call lead for quotation confirmation",
            "description": "Follow up regarding final quantity.",
            "due_date": "2026-09-02T00:00:00Z",
            "priority": "medium",
            "assigned_to_id": so_id,
        },
        headers=so_auth,
    )
    log_test("Lead-only visit follow-up succeeds (201)", lead_fu.status_code == 201, lead_fu.text)
    fu_data = lead_fu.json()
    log_test("Follow-up customer_id is null", fu_data.get("customer_id") is None)
    log_test("Follow-up visit_id matches", fu_data["visit_id"] == lead_visit_id)
    log_test("Follow-up assigned_to_id matches", fu_data["assigned_to_id"] == so_id)
    log_test("Follow-up title matches", fu_data["title"] == "Call lead for quotation confirmation")
    log_test("Follow-up status defaults to pending", fu_data["status"] == "pending")

    # Re-fetch to confirm persistence (not just the POST response).
    refetch = client.get(f"/follow-ups/{fu_data['id']}", headers=so_auth)
    log_test("Re-fetch of lead-only follow-up succeeds (200)", refetch.status_code == 200)
    log_test("Re-fetched follow-up still has customer_id null", refetch.json().get("customer_id") is None)
    log_test("Re-fetched follow-up still linked to visit", refetch.json()["visit_id"] == lead_visit_id)

    # ---- TEST C: Visit with neither customer_id nor lead_id ----
    print("\n--- TEST C: Visit with neither customer nor lead ---")
    # Visits require one of customer_id/lead_id at creation; simulate the
    # "neither" case directly against the follow-up endpoint the same way the
    # requirement describes it — via a visit whose customer_id/lead_id are both
    # absent. We reach this by asking for a standalone follow-up (no visit_id,
    # no customer_id) — same underlying "no valid parent" rule.
    standalone_fu = client.post(
        "/follow-ups",
        json={
            "title": "Orphan follow-up",
            "due_date": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        },
        headers=so_auth,
    )
    log_test("Follow-up with no customer/lead/visit context -> 400", standalone_fu.status_code == 400)
    log_test(
        "Error message is 'customer_id is required' (no visit context)",
        standalone_fu.json()["detail"] == "customer_id is required",
    )

    # ---- TEST D: Cross-organization Visit ----
    print("\n--- TEST D: Cross-organization Visit ---")
    cross_fu = client.post(
        f"/visits/{lead_visit_id}/follow-ups",
        json={
            "title": "Should not work",
            "due_date": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        },
        headers=other_admin_auth,
    )
    log_test(
        "Cross-org visit follow-up rejected (400/403/404)",
        cross_fu.status_code in (400, 403, 404),
        cross_fu.text,
    )

    # ---- TEST E: Invalid assigned_to_id ----
    print("\n--- TEST E: Invalid assigned_to_id ---")
    bad_assignee_fu = client.post(
        f"/visits/{lead_visit_id}/follow-ups",
        json={
            "title": "Bad assignee",
            "due_date": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            "assigned_to_id": "not-a-real-user-id",
        },
        headers=so_auth,
    )
    log_test("Invalid assigned_to_id rejected (400)", bad_assignee_fu.status_code == 400, bad_assignee_fu.text)


# ==========================================================================
# FIX 2: Method-specific payment details
# ==========================================================================


def run_fix2_tests():
    print("\n=======================================================")
    print("FIX 2: UPI / Card / COD payment details")
    print("=======================================================\n")

    admin_auth = _register_org(f"PayDetails_{uuid.uuid4().hex[:6]}")
    cust_res = client.post("/customers", json={"name": "Pay Customer"}, headers=admin_auth)
    assert cust_res.status_code == 201, cust_res.text
    cust_id = cust_res.json()["id"]

    # ---- 1. Cash ----
    print("--- Cash payment ---")
    cash_res = client.post(
        f"/customers/{cust_id}/payments",
        json={"amount": 1500, "payment_mode": "cash"},
        headers=admin_auth,
    )
    log_test("Cash payment succeeds (201)", cash_res.status_code == 201, cash_res.text)

    # ---- 2. UPI ----
    print("\n--- UPI payment ---")
    upi_res = client.post(
        f"/customers/{cust_id}/payments",
        json={
            "amount": 2500,
            "payment_mode": "upi",
            "upi_id": "customer@upi",
            "reference": "UPI123456",
        },
        headers=admin_auth,
    )
    log_test("UPI payment succeeds (201)", upi_res.status_code == 201, upi_res.text)

    # ---- 3. Card ----
    print("\n--- Card payment ---")
    card_res = client.post(
        f"/customers/{cust_id}/payments",
        json={
            "amount": 4000,
            "payment_mode": "card",
            "card_type": "Visa",
            "card_last_four": "4242",
            "reference": "CARD987654",
        },
        headers=admin_auth,
    )
    log_test("Card payment succeeds (201)", card_res.status_code == 201, card_res.text)

    # ---- 4. COD ----
    print("\n--- COD payment ---")
    cod_res = client.post(
        f"/customers/{cust_id}/payments",
        json={
            "amount": 5000,
            "payment_mode": "cod",
            "collection_instructions": "Collect at delivery from accounts desk",
        },
        headers=admin_auth,
    )
    log_test("COD payment succeeds (201)", cod_res.status_code == 201, cod_res.text)

    # ---- Card validation: last-four must be exactly 4 digits ----
    print("\n--- Card validation ---")
    bad_card_res = client.post(
        f"/customers/{cust_id}/payments",
        json={"amount": 100, "payment_mode": "card", "card_last_four": "42"},
        headers=admin_auth,
    )
    log_test("card_last_four with wrong length rejected (422)", bad_card_res.status_code == 422, bad_card_res.text)

    bad_card_res2 = client.post(
        f"/customers/{cust_id}/payments",
        json={"amount": 100, "payment_mode": "card", "card_last_four": "42ab"},
        headers=admin_auth,
    )
    log_test("card_last_four with non-digits rejected (422)", bad_card_res2.status_code == 422)

    # ---- Persistence: create -> GET/re-fetch -> verify (not just the POST response) ----
    print("\n--- Persistence via GET /customers/{id}/payments ---")
    history = client.get(f"/customers/{cust_id}/payments", headers=admin_auth)
    log_test("GET payment history succeeds (200)", history.status_code == 200, history.text)
    rows = history.json()
    log_test("History has 4 payments", len(rows) == 4, len(rows))

    by_mode = {row["payment_mode"]: row for row in rows}

    cash_row = by_mode.get("cash")
    log_test("Cash row present", cash_row is not None)
    if cash_row:
        log_test("Cash: upi_id is null", cash_row["upi_id"] is None)
        log_test("Cash: card_type is null", cash_row["card_type"] is None)
        log_test("Cash: card_last_four is null", cash_row["card_last_four"] is None)
        log_test("Cash: collection_instructions is null", cash_row["collection_instructions"] is None)

    upi_row = by_mode.get("upi")
    log_test("UPI row present", upi_row is not None)
    if upi_row:
        log_test("UPI: upi_id persisted", upi_row["upi_id"] == "customer@upi")
        log_test("UPI: reference (transaction reference) persisted", upi_row["reference"] == "UPI123456")

    card_row = by_mode.get("card")
    log_test("Card row present", card_row is not None)
    if card_row:
        log_test("Card: card_type persisted", card_row["card_type"] == "Visa")
        log_test("Card: card_last_four persisted", card_row["card_last_four"] == "4242")
        log_test("Card: reference (transaction reference) persisted", card_row["reference"] == "CARD987654")
        log_test("Card: no full card number field exists", "card_number" not in card_row)
        log_test("Card: no CVV field exists", "cvv" not in card_row)

    cod_row = by_mode.get("cod")
    log_test("COD row present", cod_row is not None)
    if cod_row:
        log_test(
            "COD: collection_instructions persisted",
            cod_row["collection_instructions"] == "Collect at delivery from accounts desk",
        )

    # ---- Re-fetch again via GET /customers/{id}/payments/receipt (unaffected) &
    #      confirm totals/collection endpoints still work ----
    print("\n--- Payment/collection totals unaffected ---")
    cust_after = client.get(f"/customers/{cust_id}", headers=admin_auth)
    log_test("GET /customers/{id} still succeeds after new payment fields", cust_after.status_code == 200)
    financial = cust_after.json().get("financial_summary", {})
    log_test(
        "total_received reflects all 4 payments (1500+2500+4000+5000=13000)",
        financial.get("total_received") == 13000,
        financial.get("total_received"),
    )

    # ---- Same coverage via POST /payment-receipts (sibling endpoint) ----
    print("\n--- POST /payment-receipts also persists method-specific details ---")
    receipt_res = client.post(
        "/payment-receipts",
        json={
            "customer_id": cust_id,
            "amount_received": 999,
            "payment_method": "upi",
            "upi_id": "receipt@upi",
            "transaction_reference": "RCPT-UPI-1",
        },
        headers=admin_auth,
    )
    log_test("POST /payment-receipts with upi_id succeeds (201)", receipt_res.status_code == 201, receipt_res.text)
    receipt_id = receipt_res.json()["id"]
    log_test("Receipt response includes upi_id", receipt_res.json()["upi_id"] == "receipt@upi")

    receipt_refetch = client.get(f"/payment-receipts/{receipt_id}", headers=admin_auth)
    log_test("GET /payment-receipts/{id} succeeds (200)", receipt_refetch.status_code == 200)
    log_test("Re-fetched receipt still has upi_id", receipt_refetch.json()["upi_id"] == "receipt@upi")
    log_test(
        "Re-fetched receipt still has transaction_reference",
        receipt_refetch.json()["transaction_reference"] == "RCPT-UPI-1",
    )


if __name__ == "__main__":
    run_fix1_tests()
    run_fix2_tests()
    print("\n=======================================================")
    print(f"RESULTS: {PASSED} passed, {FAILED} failed")
    print("=======================================================\n")
    if FAILED > 0:
        sys.exit(1)
