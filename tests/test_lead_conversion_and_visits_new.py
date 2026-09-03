import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from app.main import app
from app.core.database import SessionLocal
from app.models import Customer, Lead, Visit

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


def assert_eq(actual, expected, msg: str):
    if actual == expected:
        ok(msg)
    else:
        fail(msg, f"Expected {expected!r}, got {actual!r}")


def _register_org(name_prefix: str = "Org"):
    email = f"admin_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={
        "organization_name": f"{name_prefix} {uuid.uuid4().hex[:6]}",
        "admin_name": "Admin User",
        "email": email,
        "password": "Password123!",
        "role": "admin",
    })
    assert r.status_code == 201, r.text
    token = r.json()["tokens"]["access_token"]
    auth = {"Authorization": f"Bearer {token}"}
    return auth, email


def run_tests():
    print("\n=======================================================")
    print("TEST SUITE: Lead -> Customer Conversion & Lead Visits")
    print("=======================================================")

    auth1, email1 = _register_org("Org A")
    auth2, email2 = _register_org("Org B")

    # Let's get database sessions to verify direct DB values
    db = SessionLocal()

    # Create Lead for Org A
    lead_res = client.post("/leads", json={
        "name": "Acme Corp Lead",
        "contact_person": "Jane Doe",
        "mobile": "1234567890",
        "email": "jane@acme.com",
        "source": "Google",
        "interested_product": "SaaS Platform",
        "notes": "Interested in premium tier",
        "lead_status": "new",
    }, headers=auth1)
    assert lead_res.status_code == 201, lead_res.text
    lead_id1 = lead_res.json()["id"]

    # =========================================================================
    # 1. Lead -> Customer Conversion with all five new fields
    # =========================================================================
    print("\n--- TEST 1: Convert Lead with 5 New Fields ---")
    conv_since = "2026-08-30T10:00:00Z"
    conv_res = client.post(f"/leads/{lead_id1}/convert-to-customer", json={
        "customer_type": "business",
        "customer_since": conv_since,
        "status": "active",
        "maps_latitude": 19.2183,
        "maps_longitude": 72.9781
    }, headers=auth1)
    assert_eq(conv_res.status_code, 200, "Lead converted successfully")
    conv_data = conv_res.json()
    assert_eq(conv_data["lead_status"], "won", "Lead status updated to 'won'")
    assert_eq(conv_data["converted"], True, "Converted flag is True")
    
    cust_data = conv_data["customer"]
    assert cust_data is not None, "Customer data returned in response"
    assert_eq(cust_data["customer_type"], "business", "Response customer_type matches")
    assert_eq(cust_data["status"], "active", "Response status matches")
    assert_eq(cust_data["maps_latitude"], 19.2183, "Response maps_latitude matches")
    assert_eq(cust_data["maps_longitude"], 72.9781, "Response maps_longitude matches")

    # Verify directly in DB
    db_cust = db.query(Customer).filter(Customer.id == cust_data["id"]).first()
    assert db_cust is not None, "Customer created in database"
    assert_eq(db_cust.customer_type, "business", "DB customer_type matches")
    assert_eq(db_cust.status, "active", "DB status matches")
    assert_eq(db_cust.maps_latitude, 19.2183, "DB maps_latitude matches")
    assert_eq(db_cust.maps_longitude, 72.9781, "DB maps_longitude matches")
    
    expected_dt = datetime.fromisoformat(conv_since.replace("Z", "+00:00"))
    actual_dt = db_cust.customer_since
    if actual_dt is not None and actual_dt.tzinfo is None:
        expected_dt = expected_dt.replace(tzinfo=None)
    assert_eq(actual_dt, expected_dt, "DB customer_since matches")

    # =========================================================================
    # 2. Existing Lead -> Customer Conversion without optional fields
    # =========================================================================
    print("\n--- TEST 2: Convert Lead Without Optional Fields ---")
    lead_res2 = client.post("/leads", json={
        "name": "Unstructured Lead",
        "contact_person": "Bob Smith",
        "mobile": "9222233334",
        "source": "Referral",
    }, headers=auth1)
    assert_eq(lead_res2.status_code, 201, "Lead created for TEST 2")
    lead_id2 = lead_res2.json()["id"]

    conv_res2 = client.post(f"/leads/{lead_id2}/convert-to-customer", json={}, headers=auth1)
    assert_eq(conv_res2.status_code, 200, "Lead conversion without optional payload works")
    cust_data2 = conv_res2.json()["customer"]
    assert_eq(cust_data2["customer_type"], None, "customer_type is None")
    assert_eq(cust_data2["status"], None, "status is None")

    # =========================================================================
    # 3. Create Visit using customer_id only
    # =========================================================================
    print("\n--- TEST 3: Create Visit with customer_id Only ---")
    visit_res1 = client.post("/visits", json={
        "customer_id": cust_data["id"],
        "visit_type": "site_visit",
        "purpose": "First introduction",
        "notes": "Met Jane, setup demo",
    }, headers=auth1)
    assert_eq(visit_res1.status_code, 201, "Visit created successfully with customer_id only")
    visit_data1 = visit_res1.json()
    assert_eq(visit_data1["customer_id"], cust_data["id"], "Visit linked to correct customer")
    assert_eq(visit_data1["lead_id"], None, "Visit lead_id is None")

    # =========================================================================
    # 4. Create Visit using lead_id only
    # =========================================================================
    print("\n--- TEST 4: Create Visit with lead_id Only ---")
    lead_res3 = client.post("/leads", json={
        "name": "Visit-only Lead",
        "contact_person": "Charlie",
        "mobile": "9333344445",
        "source": "Referral",
    }, headers=auth1)
    assert_eq(lead_res3.status_code, 201, "Lead created for TEST 4")
    lead_id3 = lead_res3.json()["id"]

    visit_res2 = client.post("/visits", json={
        "lead_id": lead_id3,
        "visit_type": "meeting",
        "purpose": "Requirement gathering",
    }, headers=auth1)
    assert_eq(visit_res2.status_code, 201, "Visit created successfully with lead_id only")
    visit_data2 = visit_res2.json()
    assert_eq(visit_data2["lead_id"], lead_id3, "Visit linked to correct lead")
    assert_eq(visit_data2["customer_id"], None, "Visit customer_id is NULL/None")

    # Re-fetch Visit and confirm persistence
    refetch_res = client.get(f"/visits/{visit_data2['id']}", headers=auth1)
    assert_eq(refetch_res.status_code, 200, "Refetch lead-only visit succeeded")
    assert_eq(refetch_res.json()["lead_id"], lead_id3, "Refetched visit lead_id matches")
    assert_eq(refetch_res.json()["customer_id"], None, "Refetched visit customer_id is None")

    # =========================================================================
    # 5. Create Visit without customer_id and lead_id
    # =========================================================================
    print("\n--- TEST 5: Create Visit Without customer_id and lead_id ---")
    visit_res3 = client.post("/visits", json={
        "visit_type": "meeting",
    }, headers=auth1)
    assert_eq(visit_res3.status_code, 422, "Orphan visit creation rejected by Pydantic validation")

    # =========================================================================
    # 6. GET /visits?lead_id=<lead_id>
    # =========================================================================
    print("\n--- TEST 6: List Visits by lead_id ---")
    list_res = client.get(f"/visits?lead_id={lead_id3}", headers=auth1)
    assert_eq(list_res.status_code, 200, "GET /visits?lead_id list succeeded")
    visits_list = list_res.json()
    assert_eq(len(visits_list), 1, "Correct number of visits returned for lead")
    assert_eq(visits_list[0]["id"], visit_data2["id"], "Returned visit matches the created lead-only visit")

    # =========================================================================
    # 7. Cross-organization Isolation
    # =========================================================================
    print("\n--- TEST 7: Cross-organization Isolation ---")
    # Cross-org Customer Visit
    visit_cross_cust = client.post("/visits", json={
        "customer_id": cust_data["id"],
    }, headers=auth2)
    assert_eq(visit_cross_cust.status_code, 400, "Cross-org Customer visit creation rejected")

    # Cross-org Lead Visit
    visit_cross_lead = client.post("/visits", json={
        "lead_id": lead_id3,
    }, headers=auth2)
    assert_eq(visit_cross_lead.status_code, 400, "Cross-org Lead visit creation rejected")

    db.close()

    print("\n=======================================================")
    print(f"RESULTS: {_passed} passed, {_failed} failed")
    print("=======================================================\n")
    if _failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_tests()
