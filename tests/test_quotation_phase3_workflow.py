"""Phase 3 — Quotation status workflow, editing rules, Lead->Customer->Quotation
linking, Order conversion (accepted+customer required, atomic, race-safe),
deletion rules, Sales Officer ownership security, multi-tenant isolation, PDF.

Exercises app/services/quotation_service.py, app/core/workflow.py's
QUOTATION_TRANSITIONS, and lead_service.convert_lead_to_customer's new
quotation auto-linking step.
"""

import os
import sys
import threading
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient

from app.main import app
from app.core.database import SessionLocal
from app.models import Quotation, SalesOrder

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
    return user, {"Authorization": f"Bearer {login.json()['tokens']['access_token']}"}


def _setup_org(label: str, stock: int = 100):
    auth = _register_org(label)
    wh = client.post("/warehouses", json={"name": "WH", "code": f"WH-{uuid.uuid4().hex[:6]}"}, headers=auth).json()
    prod = client.post("/products", json={
        "name": "P3 Widget", "sku": f"P3W-{uuid.uuid4().hex[:6]}", "price": 100.0, "tax_rate": 18.0, "uom": "unit",
        "pricing": {"purchase_price": 50.0, "selling_price": 100.0, "currency": "INR"},
    }, headers=auth).json()
    client.post(f"/warehouses/{wh['id']}/stock/adjust", json={"product_id": prod["id"], "quantity": stock}, headers=auth)
    cust = client.post("/customers", json={"name": "P3 Cust", "phone": f"9{uuid.uuid4().hex[:9]}"}, headers=auth).json()
    lead = client.post("/leads", json={
        "name": "P3 Lead", "mobile_number": f"9{uuid.uuid4().hex[:9]}", "lead_source": "Website",
    }, headers=auth).json()
    return auth, wh["id"], prod["id"], cust["id"], lead["id"]


def _item(prod_id, **overrides):
    line = {"product_id": prod_id, "quantity": 2, "unit_price": 100.0}
    line.update(overrides)
    return line


def _new_quotation(auth, cust_id, prod_id):
    r = client.post("/quotations", json={"customer_id": cust_id, "items": [_item(prod_id)]}, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _advance_to_accepted(auth, quote_id):
    r1 = client.patch(f"/quotations/{quote_id}", json={"status": "sent"}, headers=auth)
    assert r1.status_code == 200, r1.text
    r2 = client.patch(f"/quotations/{quote_id}", json={"status": "accepted"}, headers=auth)
    assert r2.status_code == 200, r2.text


# ============================================================================
# A. Status workflow transitions
# ============================================================================

def run_transition_tests():
    print("\n=== A. Status workflow transitions ===")
    auth, wh_id, prod_id, cust_id, lead_id = _setup_org("Qp3TransA")

    # Valid forward path.
    q1 = _new_quotation(auth, cust_id, prod_id)
    check("draft -> sent allowed", client.patch(f"/quotations/{q1}", json={"status": "sent"}, headers=auth).status_code == 200)
    check("sent -> accepted allowed", client.patch(f"/quotations/{q1}", json={"status": "accepted"}, headers=auth).status_code == 200)

    q2 = _new_quotation(auth, cust_id, prod_id)
    client.patch(f"/quotations/{q2}", json={"status": "sent"}, headers=auth)
    check("sent -> rejected allowed", client.patch(f"/quotations/{q2}", json={"status": "rejected"}, headers=auth).status_code == 200)
    check("rejected -> draft allowed", client.patch(f"/quotations/{q2}", json={"status": "draft"}, headers=auth).status_code == 200)

    q3 = _new_quotation(auth, cust_id, prod_id)
    client.patch(f"/quotations/{q3}", json={"status": "sent"}, headers=auth)
    check("sent -> draft allowed (explicit)", client.patch(f"/quotations/{q3}", json={"status": "draft"}, headers=auth).status_code == 200)

    # accepted -> converted only via the real endpoint.
    q4 = _new_quotation(auth, cust_id, prod_id)
    _advance_to_accepted(auth, q4)
    conv = client.post(f"/quotations/{q4}/convert-to-order", json={"warehouse_id": wh_id}, headers=auth)
    check("accepted -> converted via convert-to-order succeeds", conv.status_code == 201, conv.text)

    # Invalid transitions, each on a fresh quotation.
    invalid_cases = [
        ("draft", "converted", []),
        ("draft", "accepted", []),
        ("draft", "rejected", []),
        ("rejected", "accepted", ["sent", "rejected"]),
        ("rejected", "converted", ["sent", "rejected"]),
        ("accepted", "draft", ["sent", "accepted"]),
        ("accepted", "sent", ["sent", "accepted"]),
        ("accepted", "rejected", ["sent", "accepted"]),
    ]
    for start_label, target, path in invalid_cases:
        qid = _new_quotation(auth, cust_id, prod_id)
        for step in path:
            client.patch(f"/quotations/{qid}", json={"status": step}, headers=auth)
        r = client.patch(f"/quotations/{qid}", json={"status": target}, headers=auth)
        check(f"{start_label} -> {target} blocked (400)", r.status_code == 400, r.text)

    # converted is terminal in every direction.
    q5 = _new_quotation(auth, cust_id, prod_id)
    _advance_to_accepted(auth, q5)
    client.post(f"/quotations/{q5}/convert-to-order", json={"warehouse_id": wh_id}, headers=auth)
    for target in ("draft", "sent", "accepted", "rejected"):
        r = client.patch(f"/quotations/{q5}", json={"status": target}, headers=auth)
        check(f"converted -> {target} blocked (400)", r.status_code == 400, r.text)

    # Invalid arbitrary status value.
    q6 = _new_quotation(auth, cust_id, prod_id)
    r_bad = client.patch(f"/quotations/{q6}", json={"status": "banana"}, headers=auth)
    check("invalid status value rejected (422, schema-level)", r_bad.status_code == 422, r_bad.text)

    # Cannot create directly with a non-draft status; a client-sent status is
    # simply not honoured for anything but a same-value resend.
    r_create_accepted = client.post("/quotations", json={
        "customer_id": cust_id, "status": "accepted", "items": [_item(prod_id)],
    }, headers=auth)
    check("Create ignores a non-draft status and starts at draft",
          r_create_accepted.status_code == 201 and r_create_accepted.json()["status"] == "draft", r_create_accepted.text)


# ============================================================================
# B. Editing rules
# ============================================================================

def run_editing_rules_tests():
    print("\n=== B. Editing rules ===")
    auth, wh_id, prod_id, cust_id, lead_id = _setup_org("Qp3EditB")

    # sent + meaningful edit -> draft
    q1 = _new_quotation(auth, cust_id, prod_id)
    client.patch(f"/quotations/{q1}", json={"status": "sent"}, headers=auth)
    r1 = client.patch(f"/quotations/{q1}", json={"notes": "changed my mind"}, headers=auth)
    assert_eq(r1.json()["status"], "draft", "sent + meaningful edit -> draft")

    # rejected + meaningful edit -> draft
    q2 = _new_quotation(auth, cust_id, prod_id)
    client.patch(f"/quotations/{q2}", json={"status": "sent"}, headers=auth)
    client.patch(f"/quotations/{q2}", json={"status": "rejected"}, headers=auth)
    r2 = client.patch(f"/quotations/{q2}", json={"notes": "revised offer"}, headers=auth)
    assert_eq(r2.json()["status"], "draft", "rejected + meaningful edit -> draft")

    # expired (derived) + meaningful edit -> draft. valid_until in the past,
    # status still 'sent' in the DB (never persisted as 'expired').
    q3 = _new_quotation(auth, cust_id, prod_id)
    client.patch(f"/quotations/{q3}", json={"status": "sent", "valid_until": "2020-01-01T00:00:00Z"}, headers=auth)
    detail_before = client.get(f"/quotations/{q3}", headers=auth).json()
    assert_eq(detail_before["status"], "sent", "Expired quotation's real stored status is still 'sent'")
    assert_eq(detail_before["effective_status"], "expired", "effective_status correctly reads 'expired'")
    r3 = client.patch(f"/quotations/{q3}", json={"notes": "still interested?"}, headers=auth)
    assert_eq(r3.json()["status"], "draft", "expired (derived) + meaningful edit -> draft")

    # A status-only resend (no other field) must NOT trigger the reset —
    # nothing meaningful changed.
    q4 = _new_quotation(auth, cust_id, prod_id)
    client.patch(f"/quotations/{q4}", json={"status": "sent"}, headers=auth)
    r4 = client.patch(f"/quotations/{q4}", json={"status": "sent"}, headers=auth)
    assert_eq(r4.json()["status"], "sent", "No-op status resend does not trigger a reset")

    # Reassigning salesperson_id alone is administrative, not meaningful.
    q5 = _new_quotation(auth, cust_id, prod_id)
    client.patch(f"/quotations/{q5}", json={"status": "sent"}, headers=auth)
    other_admin, other_auth = _create_staff(auth, "Other Officer", "Sales Officer")
    r5 = client.patch(f"/quotations/{q5}", json={"salesperson_id": other_admin["id"]}, headers=auth)
    assert_eq(r5.json()["status"], "sent", "Reassigning salesperson_id alone does not reset to draft")

    # accepted: normal editing blocked entirely.
    q6 = _new_quotation(auth, cust_id, prod_id)
    _advance_to_accepted(auth, q6)
    r6_items = client.patch(f"/quotations/{q6}", json={"items": [_item(prod_id, quantity=99, unit_price=1.0)]}, headers=auth)
    check("accepted: item/price edit blocked (400)", r6_items.status_code == 400, r6_items.text)
    r6_notes = client.patch(f"/quotations/{q6}", json={"notes": "sneaky edit"}, headers=auth)
    check("accepted: notes edit blocked (400)", r6_notes.status_code == 400, r6_notes.text)
    r6_party = client.patch(f"/quotations/{q6}", json={"customer_id": cust_id}, headers=auth)
    check("accepted: customer/lead edit blocked (400)", r6_party.status_code == 400, r6_party.text)
    still_accepted = client.get(f"/quotations/{q6}", headers=auth).json()
    assert_eq(still_accepted["status"], "accepted", "Quotation is still 'accepted' after all blocked attempts")
    assert_eq(still_accepted["items"][0]["quantity"], 2.0, "Original item data untouched")

    # converted: editing blocked entirely.
    conv = client.post(f"/quotations/{q6}/convert-to-order", json={"warehouse_id": wh_id}, headers=auth)
    check("accepted -> converted succeeds", conv.status_code == 201, conv.text)
    r7 = client.patch(f"/quotations/{q6}", json={"notes": "too late"}, headers=auth)
    check("converted: any edit blocked (400)", r7.status_code == 400, r7.text)


# ============================================================================
# C. Lead -> Customer -> Quotation linking
# ============================================================================

def run_lead_linking_tests():
    print("\n=== C. Lead -> Customer -> Quotation linking ===")
    auth, wh_id, prod_id, cust_id, lead_id = _setup_org("Qp3LinkC")

    r_q = client.post("/quotations", json={"lead_id": lead_id, "items": [_item(prod_id)]}, headers=auth)
    check("Lead quotation created", r_q.status_code == 201, r_q.text)
    q_id = r_q.json()["id"]
    assert_eq(r_q.json()["customer_id"], None, "customer_id starts None")

    r_conv = client.post(f"/leads/{lead_id}/convert-to-customer", json={}, headers=auth)
    check("Lead converted to Customer", r_conv.status_code == 200, r_conv.text)
    new_customer_id = r_conv.json()["customer_id"]

    q_after = client.get(f"/quotations/{q_id}", headers=auth).json()
    assert_eq(q_after["customer_id"], new_customer_id, "quotation.customer_id automatically set to the new Customer")
    assert_eq(q_after["lead_id"], lead_id, "quotation.lead_id preserved for history")
    check("customer brief now present", q_after["customer"] is not None and q_after["customer"]["id"] == new_customer_id)
    check("lead brief still present", q_after["lead"] is not None and q_after["lead"]["id"] == lead_id)

    # Fresh DB session — proves this is real persistence, not identity-map residue.
    db = SessionLocal()
    row = db.get(Quotation, q_id)
    assert_eq(row.customer_id, new_customer_id, "DB row: customer_id set")
    assert_eq(row.lead_id, lead_id, "DB row: lead_id preserved")
    db.close()

    # The now-linked quotation can proceed through the normal workflow to a real Order.
    _advance_to_accepted(auth, q_id)
    r_order = client.post(f"/quotations/{q_id}/convert-to-order", json={"warehouse_id": wh_id}, headers=auth)
    check("Linked quotation converts to a real Order", r_order.status_code == 201, r_order.text)

    # A quotation NOT belonging to the converted Lead is untouched.
    other_lead = client.post("/leads", json={
        "name": "Untouched Lead", "mobile_number": f"9{uuid.uuid4().hex[:9]}", "lead_source": "Website",
    }, headers=auth).json()
    r_other_q = client.post("/quotations", json={"lead_id": other_lead["id"], "items": [_item(prod_id)]}, headers=auth)
    other_q_id = r_other_q.json()["id"]
    still_lead_only = client.get(f"/quotations/{other_q_id}", headers=auth).json()
    assert_eq(still_lead_only["customer_id"], None, "An unrelated Lead's quotation is untouched by someone else's conversion")


# ============================================================================
# D/E. Order conversion requirements
# ============================================================================

def run_conversion_requirement_tests():
    print("\n=== D/E. Order conversion requirements ===")
    auth, wh_id, prod_id, cust_id, lead_id = _setup_org("Qp3ConvDE")

    for start_status, path in (("draft", []), ("sent", ["sent"]), ("rejected", ["sent", "rejected"])):
        qid = _new_quotation(auth, cust_id, prod_id)
        for step in path:
            client.patch(f"/quotations/{qid}", json={"status": step}, headers=auth)
        r = client.post(f"/quotations/{qid}/convert-to-order", json={"warehouse_id": wh_id}, headers=auth)
        check(f"convert-to-order from '{start_status}' blocked (400)", r.status_code == 400, r.text)
        db = SessionLocal()
        n_orders = db.query(SalesOrder).filter(SalesOrder.quotation_id == qid).count()
        db.close()
        assert_eq(n_orders, 0, f"No Order created from a '{start_status}' quotation")

    # Already converted.
    q_conv = _new_quotation(auth, cust_id, prod_id)
    _advance_to_accepted(auth, q_conv)
    client.post(f"/quotations/{q_conv}/convert-to-order", json={"warehouse_id": wh_id}, headers=auth)
    r_again = client.post(f"/quotations/{q_conv}/convert-to-order", json={"warehouse_id": wh_id}, headers=auth)
    check("convert-to-order on already-converted quotation blocked (400)", r_again.status_code == 400, r_again.text)

    # Lead-only quotation, even if somehow accepted, cannot convert directly.
    r_lead_q = client.post("/quotations", json={"lead_id": lead_id, "items": [_item(prod_id)]}, headers=auth)
    lead_q_id = r_lead_q.json()["id"]
    _advance_to_accepted(auth, lead_q_id)
    r_lead_convert = client.post(f"/quotations/{lead_q_id}/convert-to-order", json={"warehouse_id": wh_id}, headers=auth)
    check("Lead-only accepted quotation -> convert-to-order blocked (400)", r_lead_convert.status_code == 400, r_lead_convert.text)
    db = SessionLocal()
    n_orders = db.query(SalesOrder).filter(SalesOrder.quotation_id == lead_q_id).count()
    db.close()
    assert_eq(n_orders, 0, "No Order created from a Lead-only quotation")
    still_accepted = client.get(f"/quotations/{lead_q_id}", headers=auth).json()
    assert_eq(still_accepted["status"], "accepted", "Lead-only quotation remains 'accepted', not silently converted")


# ============================================================================
# F. Concurrency (critical)
# ============================================================================

def run_concurrency_test(label: str, n: int = 10):
    print(f"\n=== F. Concurrent conversion (critical) — {label} ===")
    auth, wh_id, prod_id, cust_id, lead_id = _setup_org(f"Qp3RaceF{uuid.uuid4().hex[:4]}", stock=1000)
    qid = _new_quotation(auth, cust_id, prod_id)
    _advance_to_accepted(auth, qid)

    results = {}
    errors = {}

    def fire(i):
        try:
            r = client.post(f"/quotations/{qid}/convert-to-order", json={"warehouse_id": wh_id}, headers=auth)
            results[i] = r.status_code
        except Exception as exc:
            errors[i] = f"{type(exc).__name__}: {exc}"

    threads = [threading.Thread(target=fire, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    print(f"  {n} concurrent attempts, results={results}, errors={errors}")
    check("No concurrent request raised an unhandled exception", len(errors) == 0, errors)
    check("No concurrent request returned HTTP 500", all(v < 500 for v in results.values()), results)
    successes = [v for v in results.values() if v == 201]
    check("Exactly one request returned 201 (created the Order)", len(successes) == 1, results)
    check("All other requests were safely rejected (400), not 500", all(v in (201, 400) for v in results.values()), results)

    db = SessionLocal()
    q_row = db.get(Quotation, qid)
    orders = db.query(SalesOrder).filter(SalesOrder.quotation_id == qid).all()
    check("Quotation.status is 'converted' after the race", q_row.status == "converted", q_row.status)
    check("Exactly one SalesOrder row exists for this quotation", len(orders) == 1, [o.id for o in orders])
    check("Quotation.converted_order_id points at that one Order",
          len(orders) == 1 and q_row.converted_order_id == orders[0].id)
    db.close()
    print(f"  Orders created: {len(orders)} (expected 1)")


# ============================================================================
# G. Rollback
# ============================================================================

def run_rollback_test():
    print("\n=== G. Rollback on mid-conversion failure ===")
    auth, wh_id, prod_id, cust_id, lead_id = _setup_org("Qp3RollbackG")
    qid = _new_quotation(auth, cust_id, prod_id)
    _advance_to_accepted(auth, qid)

    import app.services.quotation_service as quotation_service_module

    class _BoomDatetime:
        @staticmethod
        def now(tz=None):
            raise RuntimeError("Simulated failure after Order creation, before commit")

    failing_client = TestClient(app, raise_server_exceptions=False)
    original_datetime = quotation_service_module.datetime
    quotation_service_module.datetime = _BoomDatetime
    try:
        r_fail = failing_client.post(f"/quotations/{qid}/convert-to-order", json={"warehouse_id": wh_id}, headers=auth)
    finally:
        quotation_service_module.datetime = original_datetime

    check("Simulated failure surfaces as a server error, not 201", r_fail.status_code >= 500, r_fail.text[:200])

    db = SessionLocal()
    q_row = db.get(Quotation, qid)
    check("Quotation.status is still 'accepted' after rollback", q_row.status == "accepted", q_row.status)
    check("Quotation.converted_order_id is still None", q_row.converted_order_id is None)
    check("Quotation.converted_at is still None", q_row.converted_at is None)
    orphan_orders = db.query(SalesOrder).filter(SalesOrder.quotation_id == qid).count()
    check("No Order was left behind by the failed conversion", orphan_orders == 0, orphan_orders)
    db.close()

    r_retry = client.post(f"/quotations/{qid}/convert-to-order", json={"warehouse_id": wh_id}, headers=auth)
    check("Quotation is still convertible after the rolled-back attempt (201)", r_retry.status_code == 201, r_retry.text)


# ============================================================================
# H. Deletion rules
# ============================================================================

def run_deletion_tests():
    print("\n=== H. Deletion rules ===")
    auth, wh_id, prod_id, cust_id, lead_id = _setup_org("Qp3DeleteH")

    q_draft = _new_quotation(auth, cust_id, prod_id)
    check("draft: delete allowed (204)", client.delete(f"/quotations/{q_draft}", headers=auth).status_code == 204)

    q_sent = _new_quotation(auth, cust_id, prod_id)
    client.patch(f"/quotations/{q_sent}", json={"status": "sent"}, headers=auth)
    check("sent: delete allowed (204)", client.delete(f"/quotations/{q_sent}", headers=auth).status_code == 204)

    q_rej = _new_quotation(auth, cust_id, prod_id)
    client.patch(f"/quotations/{q_rej}", json={"status": "sent"}, headers=auth)
    client.patch(f"/quotations/{q_rej}", json={"status": "rejected"}, headers=auth)
    check("rejected: delete allowed (204)", client.delete(f"/quotations/{q_rej}", headers=auth).status_code == 204)

    q_acc = _new_quotation(auth, cust_id, prod_id)
    _advance_to_accepted(auth, q_acc)
    r_acc_del = client.delete(f"/quotations/{q_acc}", headers=auth)
    check("accepted: delete BLOCKED (400)", r_acc_del.status_code == 400, r_acc_del.text)
    check("accepted quotation still exists", client.get(f"/quotations/{q_acc}", headers=auth).status_code == 200)

    q_conv = _new_quotation(auth, cust_id, prod_id)
    _advance_to_accepted(auth, q_conv)
    client.post(f"/quotations/{q_conv}/convert-to-order", json={"warehouse_id": wh_id}, headers=auth)
    r_conv_del = client.delete(f"/quotations/{q_conv}", headers=auth)
    check("converted: delete BLOCKED (400)", r_conv_del.status_code == 400, r_conv_del.text)
    check("converted quotation still exists", client.get(f"/quotations/{q_conv}", headers=auth).status_code == 200)


# ============================================================================
# I. Sales Officer ownership security (incl. the DELETE bug fix)
# ============================================================================

def run_ownership_security_tests():
    print("\n=== I. Sales Officer ownership security ===")
    admin_auth, wh_id, prod_id, cust_id, lead_id = _setup_org("Qp3OwnI")
    so1, so1_auth = _create_staff(admin_auth, "Owner One", "Sales Officer")
    so2, so2_auth = _create_staff(admin_auth, "Owner Two", "Sales Officer")

    r = client.post("/quotations", json={
        "customer_id": cust_id, "salesperson_id": so1["id"], "items": [_item(prod_id)],
    }, headers=admin_auth)
    assert r.status_code == 201, r.text
    q_id = r.json()["id"]

    check("SO2 GET SO1's quotation -> 404", client.get(f"/quotations/{q_id}", headers=so2_auth).status_code == 404)
    check("SO2 PATCH SO1's quotation -> 404",
          client.patch(f"/quotations/{q_id}", json={"notes": "hijack"}, headers=so2_auth).status_code == 404)
    check("SO2 PDF of SO1's quotation -> 404", client.get(f"/quotations/{q_id}/pdf", headers=so2_auth).status_code == 404)
    check("SO2 convert SO1's quotation -> 404",
          client.post(f"/quotations/{q_id}/convert-to-order", json={}, headers=so2_auth).status_code == 404)

    # The specific previously-confirmed bug: DELETE bypassing ownership.
    r_delete = client.delete(f"/quotations/{q_id}", headers=so2_auth)
    check("SO2 DELETE SO1's quotation -> 404 (previously a confirmed bug: was 204)",
          r_delete.status_code == 404, r_delete.text)
    check("Quotation still exists after SO2's blocked delete attempt (admin view)",
          client.get(f"/quotations/{q_id}", headers=admin_auth).status_code == 200)

    # SO1 (the owner) can access all of the above normally.
    check("SO1 GET own quotation -> 200", client.get(f"/quotations/{q_id}", headers=so1_auth).status_code == 200)
    check("SO1 PATCH own quotation -> 200",
          client.patch(f"/quotations/{q_id}", json={"notes": "my own note"}, headers=so1_auth).status_code == 200)

    # Admin bypasses ownership scoping entirely, still confined to its own org.
    check("Admin GET any quotation in org -> 200", client.get(f"/quotations/{q_id}", headers=admin_auth).status_code == 200)
    check("Admin DELETE any quotation in org -> 204", client.delete(f"/quotations/{q_id}", headers=admin_auth).status_code == 204)


# ============================================================================
# J. Multi-tenant isolation
# ============================================================================

def run_multitenant_tests():
    print("\n=== J. Multi-tenant isolation ===")
    auth_a, wh_a, prod_a, cust_a, lead_a = _setup_org("Qp3MtA")
    auth_b, wh_b, prod_b, cust_b, lead_b = _setup_org("Qp3MtB")

    q_b = client.post("/quotations", json={"customer_id": cust_b, "items": [_item(prod_b)]}, headers=auth_b).json()

    check("Org A GET Org B's quotation -> 404", client.get(f"/quotations/{q_b['id']}", headers=auth_a).status_code == 404)
    check("Org A PATCH Org B's quotation -> 404",
          client.patch(f"/quotations/{q_b['id']}", json={"notes": "x"}, headers=auth_a).status_code == 404)
    check("Org A DELETE Org B's quotation -> 404", client.delete(f"/quotations/{q_b['id']}", headers=auth_a).status_code == 404)
    check("Org A convert Org B's quotation -> 404",
          client.post(f"/quotations/{q_b['id']}/convert-to-order", json={}, headers=auth_a).status_code == 404)
    check("Org A PDF of Org B's quotation -> 404", client.get(f"/quotations/{q_b['id']}/pdf", headers=auth_a).status_code == 404)

    r_cross_lead = client.post("/quotations", json={"lead_id": lead_b, "items": [_item(prod_a)]}, headers=auth_a)
    check("Org A cannot use Org B's lead_id (400)", r_cross_lead.status_code == 400, r_cross_lead.text)
    r_cross_cust = client.post("/quotations", json={"customer_id": cust_b, "items": [_item(prod_a)]}, headers=auth_a)
    check("Org A cannot use Org B's customer_id (400)", r_cross_cust.status_code == 400, r_cross_cust.text)
    r_cross_prod = client.post("/quotations", json={"customer_id": cust_a, "items": [_item(prod_b)]}, headers=auth_a)
    check("Org A cannot use Org B's product_id (400)", r_cross_prod.status_code == 400, r_cross_prod.text)


# ============================================================================
# K. PDF
# ============================================================================

def run_pdf_tests():
    print("\n=== K. PDF ===")
    auth, wh_id, prod_id, cust_id, lead_id = _setup_org("Qp3PdfK")

    q_cust = client.post("/quotations", json={"customer_id": cust_id, "items": [_item(prod_id)]}, headers=auth).json()
    r1 = client.get(f"/quotations/{q_cust['id']}/pdf", headers=auth)
    check("Customer quotation PDF -> 200", r1.status_code == 200 and r1.headers.get("content-type") == "application/pdf", r1.text[:200])

    q_lead = client.post("/quotations", json={"lead_id": lead_id, "items": [_item(prod_id)]}, headers=auth).json()
    r2 = client.get(f"/quotations/{q_lead['id']}/pdf", headers=auth)
    check("Lead quotation PDF -> 200", r2.status_code == 200 and r2.headers.get("content-type") == "application/pdf", r2.text[:200])


def run_all_tests():
    run_transition_tests()
    run_editing_rules_tests()
    run_lead_linking_tests()
    run_conversion_requirement_tests()
    run_concurrency_test("SQLite", n=10)
    run_rollback_test()
    run_deletion_tests()
    run_ownership_security_tests()
    run_multitenant_tests()
    run_pdf_tests()

    print("\n=======================================================")
    print(f"RESULTS: {_passed} passed, {_failed} failed")
    print("=======================================================\n")
    if _failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_all_tests()
