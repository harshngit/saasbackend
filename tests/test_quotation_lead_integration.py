"""Phase 2 — Quotations & Lead Integration.

Covers:
  A. Database relationship: lead_id persists, FK works, ON DELETE SET NULL,
     survives a fresh DB session.
  B. Customer quotation regression: create/get/list/calculations unchanged.
  C. Lead quotation: create without a Customer, get, list, Lead brief returned.
  D. Party validation: exactly one of customer_id/lead_id, clean 4xx never 500.
  E. Calculation security: server remains the source of truth for totals.
  F. Multi-tenant security: cross-org lead_id (and a light re-check of
     customer/product/salesperson) rejected.
  G. PDF: Customer quotation PDF unaffected; Lead quotation PDF succeeds.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient

from app.main import app
from app.core.database import SessionLocal
from app.models import Lead, Quotation

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


def _setup_org(label: str):
    auth = _register_org(label)
    prod = client.post("/products", json={
        "name": "Quotable Widget",
        "sku": f"QW-{uuid.uuid4().hex[:6]}",
        "price": 200.0,
        "tax_rate": 18.0,
        "uom": "unit",
        "pricing": {"purchase_price": 100.0, "selling_price": 200.0, "currency": "INR"},
    }, headers=auth)
    assert prod.status_code == 201, prod.text
    prod_id = prod.json()["id"]

    cust = client.post("/customers", json={
        "name": "Regular Customer Co",
        "business_name": "Regular Customer Pvt Ltd",
        "phone": "9111122223",
        "email": "buyer@regular.com",
        "gst_number": "27AAACA1234A1Z5",
        "billing_address": "1 Billing Rd",
        "delivery_address": "1 Delivery Rd",
    }, headers=auth)
    assert cust.status_code == 201, cust.text
    cust_id = cust.json()["id"]

    lead = client.post("/leads", json={
        "name": "Prospect Lead", "contact_person": "Priya Prospect",
        "mobile_number": "9222233334", "email": "priya@prospect.com",
        "lead_source": "Website",
    }, headers=auth)
    assert lead.status_code == 201, lead.text
    lead_id = lead.json()["id"]

    return auth, prod_id, cust_id, lead_id


def _item(prod_id, **overrides):
    line = {"product_id": prod_id, "quantity": 3, "unit_price": 200.0, "discount_percent": 10, "tax_rate": 18.0}
    line.update(overrides)
    return line


# ============================================================================
# A. Database relationship
# ============================================================================

def run_db_relationship_tests():
    print("\n=== A. Database relationship ===")
    auth, prod_id, cust_id, lead_id = _setup_org("QuoteDbA")

    r = client.post("/quotations", json={"lead_id": lead_id, "items": [_item(prod_id)]}, headers=auth)
    check("Lead quotation created (201)", r.status_code == 201, r.text)
    quote_id = r.json()["id"]
    assert_eq(r.json()["lead_id"], lead_id, "lead_id persisted in the create response")

    db = SessionLocal()
    row = db.get(Quotation, quote_id)
    assert_eq(row.lead_id, lead_id, "lead_id column persisted in the row")
    check("Row's lead relationship resolves to the right Lead", row.lead is not None and row.lead.id == lead_id)
    db.close()

    # Fresh session — proves this isn't an in-memory/identity-map artifact.
    db2 = SessionLocal()
    row2 = db2.get(Quotation, quote_id)
    assert_eq(row2.lead_id, lead_id, "lead_id survives a brand new DB session")
    db2.close()

    # ON DELETE SET NULL: deleting the (unconverted) Lead must not delete the quotation.
    del_r = client.delete(f"/leads/{lead_id}", headers=auth)
    check("Unconverted Lead can be deleted", del_r.status_code == 204, del_r.text)

    still_there = client.get(f"/quotations/{quote_id}", headers=auth)
    check("Quotation still exists after its Lead is deleted", still_there.status_code == 200, still_there.text)
    assert_eq(still_there.json()["lead_id"], None, "quotation.lead_id is NULLed, not left dangling")
    assert_eq(still_there.json()["lead"], None, "quotation.lead brief is None after the Lead is gone")

    db3 = SessionLocal()
    row3 = db3.get(Quotation, quote_id)
    assert_eq(row3.lead_id, None, "DB row itself shows lead_id = NULL (real FK behavior, not just API-layer masking)")
    db3.close()


# ============================================================================
# B. Customer quotation regression
# ============================================================================

def run_customer_regression_tests():
    print("\n=== B. Customer quotation regression ===")
    auth, prod_id, cust_id, lead_id = _setup_org("QuoteCustB")

    r = client.post("/quotations", json={
        "customer_id": cust_id,
        "items": [_item(prod_id, quantity=2, unit_price=200.0, discount_percent=0, tax_rate=18.0)],
    }, headers=auth)
    check("Customer quotation still creates fine (201)", r.status_code == 201, r.text)
    q = r.json()
    assert_eq(q["customer_id"], cust_id, "customer_id persisted")
    assert_eq(q["lead_id"], None, "lead_id is None for a Customer quotation")
    check("customer brief present", q["customer"] is not None and q["customer"]["id"] == cust_id)
    check("lead brief absent", q["lead"] is None)

    # 2 * 200 = 400 gross, 0 discount, 18% tax = 72, total = 472
    assert_eq(q["subtotal"], 400.0, "subtotal unchanged (400.0)")
    assert_eq(q["tax_total"], 72.0, "tax_total unchanged (72.0)")
    assert_eq(q["total"], 472.0, "total unchanged (472.0)")

    r_get = client.get(f"/quotations/{q['id']}", headers=auth)
    check("GET single Customer quotation still works", r_get.status_code == 200, r_get.text)

    r_list = client.get("/quotations", headers=auth)
    check("GET list still works and includes the Customer quotation",
          r_list.status_code == 200 and any(x["id"] == q["id"] for x in r_list.json()), r_list.text[:200])


# ============================================================================
# C. Lead quotation
# ============================================================================

def run_lead_quotation_tests():
    print("\n=== C. Lead quotation ===")
    auth, prod_id, cust_id, lead_id = _setup_org("QuoteLeadC")

    r = client.post("/quotations", json={
        "lead_id": lead_id,
        "items": [_item(prod_id, quantity=1, unit_price=200.0, discount_percent=0, tax_rate=18.0)],
    }, headers=auth)
    check("Lead quotation created with no Customer (201)", r.status_code == 201, r.text)
    q = r.json()
    assert_eq(q["lead_id"], lead_id, "lead_id persisted")
    assert_eq(q["customer_id"], None, "customer_id stays None")
    check("lead brief present", q["lead"] is not None)
    assert_eq(q["lead"]["id"], lead_id, "lead brief id matches")
    assert_eq(q["lead"]["name"], "Prospect Lead", "lead brief name matches")
    assert_eq(q["lead"]["mobile_number"], "9222233334", "lead brief mobile_number matches")
    assert_eq(q["lead"]["email"], "priya@prospect.com", "lead brief email matches")
    check("customer brief absent", q["customer"] is None)

    r_get = client.get(f"/quotations/{q['id']}", headers=auth)
    check("GET single Lead quotation works", r_get.status_code == 200, r_get.text)
    assert_eq(r_get.json()["lead"]["id"], lead_id, "GET single still shows the Lead brief")

    r_list = client.get("/quotations", headers=auth)
    check("GET list works and includes the Lead quotation", r_list.status_code == 200, r_list.text[:200])
    row = next((x for x in r_list.json() if x["id"] == q["id"]), None)
    check("List row exists", row is not None)
    if row:
        check("List row's lead brief present", row["lead"] is not None and row["lead"]["id"] == lead_id)
        check("List row's customer brief absent", row["customer"] is None)


# ============================================================================
# D. Party validation
# ============================================================================

def run_party_validation_tests():
    print("\n=== D. Party validation (exactly one of customer_id / lead_id) ===")
    auth, prod_id, cust_id, lead_id = _setup_org("QuotePartyD")
    items = [_item(prod_id)]

    r_none = client.post("/quotations", json={"items": items}, headers=auth)
    check("Neither customer_id nor lead_id -> clean 4xx, not 500", 400 <= r_none.status_code < 500, r_none.text)

    r_both = client.post("/quotations", json={"customer_id": cust_id, "lead_id": lead_id, "items": items}, headers=auth)
    check("Both customer_id and lead_id -> clean 4xx, not 500", 400 <= r_both.status_code < 500, r_both.text)

    r_cust = client.post("/quotations", json={"customer_id": cust_id, "items": items}, headers=auth)
    check("customer_id only -> 201", r_cust.status_code == 201, r_cust.text)

    r_lead = client.post("/quotations", json={"lead_id": lead_id, "items": items}, headers=auth)
    check("lead_id only -> 201", r_lead.status_code == 201, r_lead.text)

    # Update-time: setting BOTH to new truthy values in the SAME request is
    # still rejected (this is the "create-shaped" ambiguity Phase 2 blocked).
    r_none2 = client.post("/quotations", json={"customer_id": cust_id, "items": items}, headers=auth)
    quote_id_fresh = r_none2.json()["id"]
    r_patch_both_same_request = client.patch(
        f"/quotations/{quote_id_fresh}", json={"lead_id": lead_id, "customer_id": cust_id}, headers=auth
    )
    check("PATCH setting customer_id AND lead_id together in one request -> clean 4xx",
          400 <= r_patch_both_same_request.status_code < 500, r_patch_both_same_request.text)

    # Clearing the only party set (on a quotation with just ONE party) -> neither -> rejected.
    r_patch_none = client.patch(f"/quotations/{quote_id_fresh}", json={"customer_id": None}, headers=auth)
    check("PATCH clearing the only party to null -> clean 4xx (would result in neither)",
          400 <= r_patch_none.status_code < 500, r_patch_none.text)

    # Phase 3 (Lead -> Customer conversion history): adding lead_id onto an
    # already customer_id-having quotation via a single-field PATCH is now
    # ALLOWED -- a quotation legitimately ends up with both set (see
    # lead_service.convert_lead_to_customer's automatic linking, which
    # preserves lead_id while adding customer_id the same way, just system-
    # driven instead of a manual PATCH). Only setting both from scratch in one
    # request (tested above) remains blocked.
    quote_id = r_cust.json()["id"]
    r_patch_add_lead = client.patch(f"/quotations/{quote_id}", json={"lead_id": lead_id}, headers=auth)
    check("PATCH adding lead_id onto a Customer quotation -> now allowed (both may coexist)",
          r_patch_add_lead.status_code == 200, r_patch_add_lead.text)
    check("Resulting quotation carries both customer_id and lead_id",
          r_patch_add_lead.json()["customer_id"] == cust_id and r_patch_add_lead.json()["lead_id"] == lead_id,
          r_patch_add_lead.text)

    # Clearing customer_id on that now-both-set quotation succeeds, since lead_id remains.
    r_patch_clear_customer = client.patch(f"/quotations/{quote_id}", json={"customer_id": None}, headers=auth)
    check("PATCH clearing customer_id while lead_id remains -> 200 (still has a party)",
          r_patch_clear_customer.status_code == 200, r_patch_clear_customer.text)

    # Switching parties in one PATCH (old cleared, new set) is a valid resulting state.
    quote_id2 = r_lead.json()["id"]
    r_switch = client.patch(f"/quotations/{quote_id2}", json={"lead_id": None, "customer_id": cust_id}, headers=auth)
    check("PATCH switching Lead -> Customer in one request -> 200", r_switch.status_code == 200, r_switch.text)


# ============================================================================
# E. Calculation security
# ============================================================================

def run_calculation_tests():
    print("\n=== E. Calculation security (server is the source of truth) ===")
    auth, prod_id, cust_id, lead_id = _setup_org("QuoteCalcE")

    # quantity=4, unit_price=200 -> gross 800; discount_percent=25 -> discount 200,
    # line_total 600; tax_rate=18 -> tax 108; subtotal 600, tax_total 108, total 708.
    r = client.post("/quotations", json={
        "lead_id": lead_id,
        "items": [_item(prod_id, quantity=4, unit_price=200.0, discount_percent=25, tax_rate=18.0)],
    }, headers=auth)
    check("Quotation for calculation check created", r.status_code == 201, r.text)
    q = r.json()
    item = q["items"][0]
    assert_eq(item["line_total"], 600.0, "line_total = gross(800) - discount(200) = 600")
    assert_eq(item["tax_amount"], 108.0, "tax_amount = 600 * 18% = 108")
    assert_eq(q["subtotal"], 600.0, "subtotal = 600")
    assert_eq(q["tax_total"], 108.0, "tax_total = 108")
    assert_eq(q["total"], 708.0, "total = subtotal + tax_total = 708")

    # Flat discount takes precedence over discount_percent when both given.
    r2 = client.post("/quotations", json={
        "lead_id": lead_id,
        "items": [{"product_id": prod_id, "quantity": 2, "unit_price": 100.0, "discount": 30, "discount_percent": 50, "tax_rate": 10.0}],
    }, headers=auth)
    item2 = r2.json()["items"][0]
    # gross 200, flat discount 30 wins over 50% (which would be 100) -> line_total 170
    assert_eq(item2["line_total"], 170.0, "flat discount (30) takes precedence over discount_percent (50%)")

    # Malicious frontend totals: sent, but never trusted or persisted.
    r3 = client.post("/quotations", json={
        "lead_id": lead_id,
        "subtotal": 1.0, "tax_total": 1.0, "total": 1.0, "grand_total": 1.0,
        "items": [_item(prod_id, quantity=1, unit_price=200.0, discount_percent=0, tax_rate=18.0)],
    }, headers=auth)
    check("Payload with forged totals is still accepted (unknown fields ignored, not 500)", r3.status_code == 201, r3.text)
    real_total = r3.json()["total"]
    check("Forged 'total': 1.0 was NOT persisted — server recomputed the real value", real_total != 1.0, real_total)
    assert_eq(real_total, 236.0, "Server-computed total is correct (200*1.18=236) regardless of the forged input")


# ============================================================================
# F. Multi-tenant security
# ============================================================================

def run_security_tests():
    print("\n=== F. Multi-tenant security ===")
    auth_a, prod_a, cust_a, lead_a = _setup_org("QuoteSecA")
    auth_b, prod_b, cust_b, lead_b = _setup_org("QuoteSecB")

    r_lead = client.post("/quotations", json={"lead_id": lead_b, "items": [_item(prod_a)]}, headers=auth_a)
    check("Cross-org lead_id rejected, not 500", r_lead.status_code == 400, r_lead.text)

    r_cust = client.post("/quotations", json={"customer_id": cust_b, "items": [_item(prod_a)]}, headers=auth_a)
    check("Cross-org customer_id still rejected (existing protection intact)", r_cust.status_code == 400, r_cust.text)

    r_prod = client.post("/quotations", json={"customer_id": cust_a, "items": [_item(prod_b)]}, headers=auth_a)
    check("Cross-org product_id still rejected (existing protection intact)", r_prod.status_code == 400, r_prod.text)

    # Cross-org salesperson.
    admin_b_me = client.get("/auth/me", headers=auth_b).json()
    r_sp = client.post("/quotations", json={
        "customer_id": cust_a, "salesperson_id": admin_b_me["id"], "items": [_item(prod_a)],
    }, headers=auth_a)
    check("Cross-org salesperson_id still rejected (existing protection intact)", r_sp.status_code == 400, r_sp.text)

    # A org cannot even see B's lead-linked quotation.
    q_b = client.post("/quotations", json={"lead_id": lead_b, "items": [_item(prod_b)]}, headers=auth_b).json()
    r_cross_get = client.get(f"/quotations/{q_b['id']}", headers=auth_a)
    check("Org A cannot GET Org B's Lead quotation (404)", r_cross_get.status_code == 404, r_cross_get.text)


# ============================================================================
# G. PDF
# ============================================================================

def run_pdf_tests():
    print("\n=== G. Quotation PDF ===")
    auth, prod_id, cust_id, lead_id = _setup_org("QuotePdfG")

    q_cust = client.post("/quotations", json={"customer_id": cust_id, "items": [_item(prod_id)]}, headers=auth).json()
    r_cust_pdf = client.get(f"/quotations/{q_cust['id']}/pdf", headers=auth)
    check("Customer quotation PDF still works (200)", r_cust_pdf.status_code == 200, r_cust_pdf.text[:200])
    check("Customer quotation PDF has the right content-type",
          r_cust_pdf.headers.get("content-type") == "application/pdf", r_cust_pdf.headers)
    check("Customer quotation PDF has real content", len(r_cust_pdf.content) > 500)

    q_lead = client.post("/quotations", json={"lead_id": lead_id, "items": [_item(prod_id)]}, headers=auth).json()
    r_lead_pdf = client.get(f"/quotations/{q_lead['id']}/pdf", headers=auth)
    check("Lead quotation PDF generates successfully, no 500", r_lead_pdf.status_code == 200, r_lead_pdf.text[:200])
    check("Lead quotation PDF has the right content-type",
          r_lead_pdf.headers.get("content-type") == "application/pdf", r_lead_pdf.headers)
    check("Lead quotation PDF has real content", len(r_lead_pdf.content) > 500)


def run_all_tests():
    run_db_relationship_tests()
    run_customer_regression_tests()
    run_lead_quotation_tests()
    run_party_validation_tests()
    run_calculation_tests()
    run_security_tests()
    run_pdf_tests()

    print("\n=======================================================")
    print(f"RESULTS: {_passed} passed, {_failed} failed")
    print("=======================================================\n")
    if _failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_all_tests()
