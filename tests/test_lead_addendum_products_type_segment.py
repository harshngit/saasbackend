"""Lead addendum: multiple interested products, lead_type, segment, the
lost-lead conversion block, and the lost-reopen transition rule.

Exercises app/models/lead.py's LeadInterestedProduct, app/services/lead_service.py's
_sync_interested_products / convert_lead_to_customer, and the updated
LEAD_TRANSITIONS in app/core/workflow.py.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient

from app.main import app
from app.core.database import SessionLocal
from app.models import Lead, LeadInterestedProduct

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


def _create_product(auth, name):
    r = client.post("/products", json={
        "name": name, "sku": f"SKU-{uuid.uuid4().hex[:6].upper()}", "price": 100.0,
    }, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


def _lead(**overrides):
    payload = {"name": "Addendum Lead", "mobile_number": f"9{uuid.uuid4().hex[:9]}", "lead_source": "Website"}
    payload.update(overrides)
    return payload


# ============================================================================
# 1/2/3/4. Multiple interested products, dedup, replace, clear
# ============================================================================

def run_interested_products_tests():
    print("\n=== Interested products: multi-create, GET, dedup, replace, clear ===")
    auth = _register_org("LeadProdA")
    p1 = _create_product(auth, "LED Panel Light 24W")
    p2 = _create_product(auth, "Ceiling Fan")
    p3 = _create_product(auth, "Table Lamp")

    r = client.post("/leads", json=_lead(interested_product_ids=[p1["id"], p2["id"]]), headers=auth)
    check("Create Lead with 2 interested products -> 201", r.status_code == 201, r.text)
    lead = r.json()
    assert_eq(set(lead["interested_product_ids"]), {p1["id"], p2["id"]}, "interested_product_ids has both, in create response")
    check("interested_products briefs present in create response", len(lead["interested_products"]) == 2, lead["interested_products"])
    names = {p["name"] for p in lead["interested_products"]}
    skus = {p["sku"] for p in lead["interested_products"]}
    assert_eq(names, {"LED Panel Light 24W", "Ceiling Fan"}, "product brief names correct")
    check("product brief skus present", all(skus), skus)

    r_get = client.get(f"/leads/{lead['id']}", headers=auth)
    check("GET single Lead returns interested_product_ids", set(r_get.json()["interested_product_ids"]) == {p1["id"], p2["id"]}, r_get.text)
    check("GET single Lead returns interested_products briefs", len(r_get.json()["interested_products"]) == 2)

    r_list = client.get("/leads", headers=auth)
    row = next((x for x in r_list.json() if x["id"] == lead["id"]), None)
    check("GET /leads list row exists", row is not None)
    if row:
        check("GET /leads list returns interested_product_ids", set(row["interested_product_ids"]) == {p1["id"], p2["id"]}, row)
        check("GET /leads list returns interested_products briefs", len(row["interested_products"]) == 2, row)

    # Duplicate IDs -- must not crash, must not create duplicate rows.
    r_dup = client.post("/leads", json=_lead(interested_product_ids=[p1["id"], p1["id"], p1["id"]]), headers=auth)
    check("Create with duplicate product IDs -> 201, no crash", r_dup.status_code == 201, r_dup.text)
    assert_eq(r_dup.json()["interested_product_ids"], [p1["id"]], "Duplicates deduplicated to one entry")
    db = SessionLocal()
    count = db.query(LeadInterestedProduct).filter(LeadInterestedProduct.lead_id == r_dup.json()["id"]).count()
    assert_eq(count, 1, "Exactly one DB row for the deduplicated product")
    db.close()

    # Replace semantics: [A, B] -> PATCH [B, C] -> final [B, C], not [A, B, C].
    r_ab = client.post("/leads", json=_lead(interested_product_ids=[p1["id"], p2["id"]]), headers=auth)
    lead_id = r_ab.json()["id"]
    r_patch = client.patch(f"/leads/{lead_id}", json={"interested_product_ids": [p2["id"], p3["id"]]}, headers=auth)
    check("PATCH replace succeeds", r_patch.status_code == 200, r_patch.text)
    assert_eq(set(r_patch.json()["interested_product_ids"]), {p2["id"], p3["id"]}, "Replace: final set is [B, C], A is gone")

    # Clear: explicit empty list.
    r_clear = client.patch(f"/leads/{lead_id}", json={"interested_product_ids": []}, headers=auth)
    check("PATCH with [] succeeds", r_clear.status_code == 200, r_clear.text)
    assert_eq(r_clear.json()["interested_product_ids"], [], "Explicit [] clears all interested products")
    assert_eq(r_clear.json()["interested_products"], [], "interested_products briefs also empty after clear")

    # PATCH omitting the field entirely must NOT touch existing products.
    r_ab2 = client.post("/leads", json=_lead(interested_product_ids=[p1["id"], p2["id"]]), headers=auth)
    lead_id2 = r_ab2.json()["id"]
    r_untouched = client.patch(f"/leads/{lead_id2}", json={"notes": "unrelated edit"}, headers=auth)
    assert_eq(set(r_untouched.json()["interested_product_ids"]), {p1["id"], p2["id"]}, "Omitting the field leaves existing products untouched")


# ============================================================================
# 5/6. Invalid and cross-tenant product validation
# ============================================================================

def run_product_validation_tests():
    print("\n=== Invalid / cross-tenant product validation ===")
    auth_a = _register_org("LeadProdSecA")
    auth_b = _register_org("LeadProdSecB")
    p_b = _create_product(auth_b, "Org B Product")

    r_invalid = client.post("/leads", json=_lead(interested_product_ids=["nonexistent-product-id"]), headers=auth_a)
    check("Nonexistent product_id -> 400, clean error", r_invalid.status_code == 400, r_invalid.text)

    r_cross = client.post("/leads", json=_lead(interested_product_ids=[p_b["id"]]), headers=auth_a)
    check("Cross-org product_id -> 400, rejected", r_cross.status_code == 400, r_cross.text)

    # Same checks on update.
    lead = client.post("/leads", json=_lead(), headers=auth_a).json()
    r_patch_invalid = client.patch(f"/leads/{lead['id']}", json={"interested_product_ids": ["still-not-real"]}, headers=auth_a)
    check("PATCH nonexistent product_id -> 400", r_patch_invalid.status_code == 400, r_patch_invalid.text)
    r_patch_cross = client.patch(f"/leads/{lead['id']}", json={"interested_product_ids": [p_b["id"]]}, headers=auth_a)
    check("PATCH cross-org product_id -> 400", r_patch_cross.status_code == 400, r_patch_cross.text)

    # Nothing was created despite the rejected attempts.
    db = SessionLocal()
    count = db.query(LeadInterestedProduct).filter(LeadInterestedProduct.lead_id == lead["id"]).count()
    assert_eq(count, 0, "No relationship rows created from rejected attempts")
    db.close()


# ============================================================================
# 7/8. lead_type / segment
# ============================================================================

def run_type_segment_tests():
    print("\n=== lead_type / segment ===")
    auth = _register_org("LeadTypeSeg")

    r = client.post("/leads", json=_lead(lead_type="Distributor", segment="Large"), headers=auth)
    check("Create with lead_type/segment -> 201", r.status_code == 201, r.text)
    assert_eq(r.json()["lead_type"], "Distributor", "lead_type persisted in create response")
    assert_eq(r.json()["segment"], "Large", "segment persisted in create response")

    lead_id = r.json()["id"]
    r_get = client.get(f"/leads/{lead_id}", headers=auth)
    assert_eq(r_get.json()["lead_type"], "Distributor", "GET single returns lead_type")
    assert_eq(r_get.json()["segment"], "Large", "GET single returns segment")

    r_list = client.get("/leads", headers=auth)
    row = next((x for x in r_list.json() if x["id"] == lead_id), None)
    check("GET list row found", row is not None)
    if row:
        assert_eq(row["lead_type"], "Distributor", "GET list returns lead_type")
        assert_eq(row["segment"], "Large", "GET list returns segment")

    r_patch = client.patch(f"/leads/{lead_id}", json={"lead_type": "Retailer", "segment": "Small"}, headers=auth)
    check("PATCH lead_type/segment -> 200", r_patch.status_code == 200, r_patch.text)
    assert_eq(r_patch.json()["lead_type"], "Retailer", "PATCH updates lead_type")
    assert_eq(r_patch.json()["segment"], "Small", "PATCH updates segment")

    # Not confused with lead_status / customer_type.
    check("lead_type is a distinct field from lead_status", "lead_status" in r_patch.json() and r_patch.json()["lead_status"] != r_patch.json()["lead_type"])


# ============================================================================
# 9. Backward compatibility (old interested_product + existing fields)
# ============================================================================

def run_backward_compatibility_tests():
    print("\n=== Backward compatibility ===")
    auth = _register_org("LeadBackCompat")

    r = client.post("/leads", json=_lead(
        interested_product="Product A, Product B, Product C",
        contact_person="Rahul Sharma", email="rahul@example.com", notes="bulk order",
    ), headers=auth)
    check("Create with legacy interested_product text -> 201", r.status_code == 201, r.text)
    assert_eq(r.json()["interested_product"], "Product A, Product B, Product C", "Legacy text field stored verbatim")
    assert_eq(r.json()["contact_person"], "Rahul Sharma", "contact_person still works")
    assert_eq(r.json()["email"], "rahul@example.com", "email still works")
    assert_eq(r.json()["notes"], "bulk order", "notes still works")
    assert_eq(r.json()["interested_product_ids"], [], "New field defaults to empty when not used")

    lead_id = r.json()["id"]
    r_patch = client.patch(f"/leads/{lead_id}", json={"interested_product": "Updated text list"}, headers=auth)
    check("PATCH legacy interested_product -> 200", r_patch.status_code == 200, r_patch.text)
    assert_eq(r_patch.json()["interested_product"], "Updated text list", "Legacy field still writable via PATCH")

    r_get = client.get(f"/leads/{lead_id}", headers=auth)
    assert_eq(r_get.json()["interested_product"], "Updated text list", "GET still returns legacy field")

    # Old and new can coexist on the same Lead without interfering.
    p = _create_product(auth, "Coexistence Product")
    r_both = client.post("/leads", json=_lead(interested_product="Legacy text", interested_product_ids=[p["id"]]), headers=auth)
    check("Legacy text + new IDs coexist -> 201", r_both.status_code == 201, r_both.text)
    assert_eq(r_both.json()["interested_product"], "Legacy text", "Legacy field unaffected by new field's presence")
    assert_eq(r_both.json()["interested_product_ids"], [p["id"]], "New field unaffected by legacy field's presence")


# ============================================================================
# 10/11/12. Lost lead conversion block + reopen + won rules
# ============================================================================

def run_lost_lead_and_status_tests():
    print("\n=== Lost lead conversion block, reopen, and won rules ===")
    auth = _register_org("LeadLostC")

    lead = client.post("/leads", json=_lead(), headers=auth).json()
    client.patch(f"/leads/{lead['id']}", json={"status": "contacted"}, headers=auth)
    r_lost = client.patch(f"/leads/{lead['id']}", json={"status": "lost"}, headers=auth)
    check("new -> contacted -> lost succeeds", r_lost.status_code == 200 and r_lost.json()["status"] == "lost", r_lost.text)

    r_convert = client.post(f"/leads/{lead['id']}/convert-to-customer", json={}, headers=auth)
    check("Convert a lost Lead -> 400", r_convert.status_code == 400, r_convert.text)
    check("Error message matches the required business message",
          r_convert.json().get("detail") == "Lost lead cannot be converted to customer. Reopen the lead first.",
          r_convert.json())

    db = SessionLocal()
    lead_row = db.get(Lead, lead["id"])
    check("Lead was NOT converted (no customer_id)", lead_row.customer_id is None)
    check("Lead status is still 'lost'", lead_row.lead_status == "lost")
    db.close()

    # Reopen: lost -> contacted works.
    r_reopen1 = client.patch(f"/leads/{lead['id']}", json={"status": "contacted"}, headers=auth)
    check("lost -> contacted (reopen) succeeds", r_reopen1.status_code == 200 and r_reopen1.json()["status"] == "contacted", r_reopen1.text)

    # Fresh lead: lost -> qualified works.
    lead2 = client.post("/leads", json=_lead(), headers=auth).json()
    client.patch(f"/leads/{lead2['id']}", json={"status": "lost"}, headers=auth)
    r_reopen2 = client.patch(f"/leads/{lead2['id']}", json={"status": "qualified"}, headers=auth)
    check("lost -> qualified (reopen) succeeds", r_reopen2.status_code == 200 and r_reopen2.json()["status"] == "qualified", r_reopen2.text)

    # lost -> new is NOT allowed (only contacted/qualified).
    lead3 = client.post("/leads", json=_lead(), headers=auth).json()
    client.patch(f"/leads/{lead3['id']}", json={"status": "lost"}, headers=auth)
    r_no_new = client.patch(f"/leads/{lead3['id']}", json={"status": "new"}, headers=auth)
    check("lost -> new blocked (not in the approved reopen set)", r_no_new.status_code == 400, r_no_new.text)

    # A reopened lead converts normally afterward.
    r_convert_after_reopen = client.post(f"/leads/{lead['id']}/convert-to-customer", json={}, headers=auth)
    check("Reopened (now 'contacted') Lead converts successfully", r_convert_after_reopen.status_code == 200, r_convert_after_reopen.text)

    # Won rules: cannot manually set, cannot move backward.
    lead4 = client.post("/leads", json=_lead(), headers=auth).json()
    r_manual_won = client.patch(f"/leads/{lead4['id']}", json={"status": "won"}, headers=auth)
    check("Manual PATCH to 'won' blocked (400)", r_manual_won.status_code == 400, r_manual_won.text)

    conv4 = client.post(f"/leads/{lead4['id']}/convert-to-customer", json={}, headers=auth)
    check("Real conversion sets status to won", conv4.status_code == 200 and conv4.json()["lead_status"] == "won", conv4.text)
    for target in ("new", "contacted", "qualified", "lost"):
        r_backward = client.patch(f"/leads/{lead4['id']}", json={"status": target}, headers=auth)
        check(f"won -> {target} blocked (converted lead is frozen)", r_backward.status_code == 400, r_backward.text)


# ============================================================================
# 13. Quotation independence from interested products
# ============================================================================

def run_quotation_independence_test():
    print("\n=== Quotation independence from interested products ===")
    auth = _register_org("LeadQuoteIndep")
    p1 = _create_product(auth, "Interested Only Product 1")
    p2 = _create_product(auth, "Interested Only Product 2")
    quoted_product = _create_product(auth, "Actually Quoted Product")

    lead = client.post("/leads", json=_lead(interested_product_ids=[p1["id"], p2["id"]]), headers=auth).json()
    check("Lead has 2 interested products, 0 explicitly quoted yet", len(lead["interested_products"]) == 2)

    r_quote = client.post("/quotations", json={
        "lead_id": lead["id"],
        "items": [{"product_id": quoted_product["id"], "quantity": 1, "unit_price": 100.0}],
    }, headers=auth)
    check("Quotation created from Lead with explicit items", r_quote.status_code == 201, r_quote.text)
    quote = r_quote.json()
    assert_eq(len(quote["items"]), 1, "Quotation has exactly 1 item — only what was explicitly sent")
    assert_eq(quote["items"][0]["product_id"], quoted_product["id"], "Quotation item is the explicitly-quoted product")
    quoted_ids = {i["product_id"] for i in quote["items"]}
    check("Neither interested product was auto-added to the quotation",
          p1["id"] not in quoted_ids and p2["id"] not in quoted_ids, quoted_ids)


# ============================================================================
# 14. Lead -> Customer mapping discipline (lead_type/segment NOT auto-copied)
# ============================================================================

def run_conversion_mapping_test():
    print("\n=== Lead -> Customer mapping discipline (lead_type/segment) ===")
    auth = _register_org("LeadMapDiscipline")
    lead = client.post("/leads", json=_lead(lead_type="Distributor", segment="Large"), headers=auth).json()

    conv = client.post(f"/leads/{lead['id']}/convert-to-customer", json={}, headers=auth)
    check("Conversion succeeds", conv.status_code == 200, conv.text)
    customer = conv.json()["customer"]
    check("Customer response has no lead_type field", "lead_type" not in customer, customer)
    check("Customer response has no segment field", "segment" not in customer, customer)
    # customer_type must not have been silently set from lead_type.
    ci = customer.get("classification_information") or customer
    check("customer_type was not silently populated from lead_type ('Distributor')",
          not _find_value(customer, "customer_type") or _find_value(customer, "customer_type") != "Distributor",
          customer)


def _find_value(obj, key):
    """Small recursive lookup — CustomerOut nests fields under section objects."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = _find_value(v, key)
            if found is not None:
                return found
    return None


def run_all_tests():
    run_interested_products_tests()
    run_product_validation_tests()
    run_type_segment_tests()
    run_backward_compatibility_tests()
    run_lost_lead_and_status_tests()
    run_quotation_independence_test()
    run_conversion_mapping_test()

    print("\n=======================================================")
    print(f"RESULTS: {_passed} passed, {_failed} failed")
    print("=======================================================\n")
    if _failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_all_tests()
