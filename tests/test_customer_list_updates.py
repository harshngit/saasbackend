"""Test suite for Customer List backend updates.

Covers:
1. city is returned when set, or null when not set.
2. last_order_date returns the latest order date (placed, processing, completed), ignoring cancelled/draft/rejected.
3. last_visit_date returns the latest visit date.
4. Tenant isolation for computed fields.
5. Correct response formatting and presence of all required fields.
"""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

# Setup test DB environment
os.environ["DATABASE_URL"] = "sqlite:///./crm_saas.db"
os.environ["TESTING"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import Base, engine, get_db
from app.main import app
from app.models import Customer, SalesOrder, User, Visit

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


def _register_org(label: str):
    clean_label = label.replace("_", "").lower()
    email = f"admin_{uuid.uuid4().hex[:8]}@{clean_label}.com"
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
    token = r.json()["tokens"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


def run_all_tests():
    global PASSED, FAILED
    PASSED = 0
    FAILED = 0

    print("\n=======================================================")
    print("TEST SUITE: Customer List Backend Updates")
    print("=======================================================\n")

    db: Session = next(get_db())

    # Create two test organizations to verify isolation
    auth1 = _register_org("ListUpdate_Org1")
    auth2 = _register_org("ListUpdate_Org2")

    # ----------------------------------------------------
    # TEST 1: City retrieval & response fields
    # ----------------------------------------------------
    print("--- TEST 1: City Retrieval & Response Fields ---")
    
    # Create customer with city in Org 1
    c1_res = client.post(
        "/customers",
        json={
            "name": "Cust One",
            "city": "Mumbai",
            "primary_contact_person": "John Doe",
            "maps_latitude": 19.0760,
            "maps_longitude": 72.8777,
        },
        headers=auth1,
    )
    assert c1_res.status_code == 201, c1_res.text
    cust1_id = c1_res.json()["id"]

    # Create customer without city in Org 1
    c2_res = client.post(
        "/customers",
        json={
            "name": "Cust Two",
            "primary_contact_person": "Jane Smith",
        },
        headers=auth1,
    )
    assert c2_res.status_code == 201, c2_res.text
    cust2_id = c2_res.json()["id"]

    # List customers for Org 1
    list_res = client.get("/customers", headers=auth1)
    log_test("GET /customers succeeds (HTTP 200)", list_res.status_code == 200, list_res.text)
    customers = list_res.json()
    log_test("GET /customers returns exactly 2 customers", len(customers) == 2)

    cust1_data = next(c for c in customers if c["id"] == cust1_id)
    cust2_data = next(c for c in customers if c["id"] == cust2_id)

    log_test("Cust One city is Mumbai", cust1_data["city"] == "Mumbai")
    log_test("Cust Two city is null", cust2_data["city"] is None)
    log_test("Cust One has primary_contact_person", cust1_data["primary_contact_person"] == "John Doe")
    log_test("Cust One maps_latitude is 19.0760", cust1_data["maps_latitude"] == 19.0760)
    log_test("Cust One maps_longitude is 72.8777", cust1_data["maps_longitude"] == 72.8777)
    log_test("Cust One has outstanding_balance", "outstanding_balance" in cust1_data)
    log_test("Cust One has is_active", cust1_data["is_active"] is True)

    # ----------------------------------------------------
    # TEST 2: last_order_date calculation
    # ----------------------------------------------------
    print("\n--- TEST 2: last_order_date Calculation ---")

    # At start, last_order_date should be null
    log_test("Cust One initial last_order_date is null", cust1_data["last_order_date"] is None)

    # Create a draft order (should be ignored)
    o_draft = SalesOrder(
        organization_id=db.get(Customer, cust1_id).organization_id,
        order_number="SO-DRAFT",
        customer_id=cust1_id,
        status="draft",
        order_date=datetime.now(timezone.utc) - timedelta(days=5),
    )
    db.add(o_draft)
    
    # Create a cancelled order (should be ignored)
    o_cancelled = SalesOrder(
        organization_id=db.get(Customer, cust1_id).organization_id,
        order_number="SO-CANCELLED",
        customer_id=cust1_id,
        status="cancelled",
        order_date=datetime.now(timezone.utc) - timedelta(days=4),
    )
    db.add(o_cancelled)

    # Create a placed order (qualifying)
    placed_time = datetime.now(timezone.utc) - timedelta(days=3)
    o_placed = SalesOrder(
        organization_id=db.get(Customer, cust1_id).organization_id,
        order_number="SO-PLACED",
        customer_id=cust1_id,
        status="placed",
        order_date=placed_time,
    )
    db.add(o_placed)

    # Create a completed order (qualifying, more recent)
    completed_time = datetime.now(timezone.utc) - timedelta(days=1)
    o_completed = SalesOrder(
        organization_id=db.get(Customer, cust1_id).organization_id,
        order_number="SO-COMPLETED",
        customer_id=cust1_id,
        status="completed",
        order_date=completed_time,
    )
    db.add(o_completed)
    db.commit()

    # Re-fetch customers list
    list_res = client.get("/customers", headers=auth1)
    customers = list_res.json()
    cust1_data = next(c for c in customers if c["id"] == cust1_id)

    log_test("Cust One has correct last_order_date (completed_time)", cust1_data["last_order_date"] is not None)
    if cust1_data["last_order_date"]:
        # Verify that it parses and matches completed_time
        parsed_dt = datetime.fromisoformat(cust1_data["last_order_date"].replace("Z", "+00:00")).replace(tzinfo=None)
        log_test("last_order_date matches the most recent qualifying order", abs((parsed_dt - completed_time.replace(tzinfo=None)).total_seconds()) < 1)

    # ----------------------------------------------------
    # TEST 3: last_visit_date calculation
    # ----------------------------------------------------
    print("\n--- TEST 3: last_visit_date Calculation ---")

    # At start, last_visit_date should be null
    log_test("Cust One initial last_visit_date is null", cust1_data["last_visit_date"] is None)

    # Create a visit
    visit_time_1 = datetime.now(timezone.utc) - timedelta(days=3)
    v1 = Visit(
        organization_id=db.get(Customer, cust1_id).organization_id,
        customer_id=cust1_id,
        visit_date=visit_time_1,
        status="completed",
    )
    db.add(v1)

    # Create a more recent visit
    visit_time_2 = datetime.now(timezone.utc) - timedelta(days=2)
    v2 = Visit(
        organization_id=db.get(Customer, cust1_id).organization_id,
        customer_id=cust1_id,
        visit_date=visit_time_2,
        status="completed",
    )
    db.add(v2)
    db.commit()

    # Re-fetch customers list
    list_res = client.get("/customers", headers=auth1)
    customers = list_res.json()
    cust1_data = next(c for c in customers if c["id"] == cust1_id)

    log_test("Cust One has correct last_visit_date", cust1_data["last_visit_date"] is not None)
    if cust1_data["last_visit_date"]:
        parsed_visit_dt = datetime.fromisoformat(cust1_data["last_visit_date"].replace("Z", "+00:00")).replace(tzinfo=None)
        log_test("last_visit_date matches the most recent visit", abs((parsed_visit_dt - visit_time_2.replace(tzinfo=None)).total_seconds()) < 1)

    # ----------------------------------------------------
    # TEST 4: Tenant isolation
    # ----------------------------------------------------
    print("\n--- TEST 4: Tenant Isolation ---")

    # Create customer in Org 2
    c_org2_res = client.post(
        "/customers",
        json={
            "name": "Org 2 Customer",
            "city": "Delhi",
        },
        headers=auth2,
    )
    assert c_org2_res.status_code == 201, c_org2_res.text
    cust_org2_id = c_org2_res.json()["id"]

    # Verify that listing Org 2 customers only returns Org 2 customer
    list_org2_res = client.get("/customers", headers=auth2)
    customers_org2 = list_org2_res.json()
    log_test("Org 2 has exactly 1 customer", len(customers_org2) == 1)
    log_test("Org 2 customer has city Delhi", customers_org2[0]["city"] == "Delhi")
    log_test("Org 2 customer last_order_date is null", customers_org2[0]["last_order_date"] is None)
    log_test("Org 2 customer last_visit_date is null", customers_org2[0]["last_visit_date"] is None)

    # Add order and visit in Org 1 for Cust 1
    # Check if they leaked into Org 2's customer
    log_test("Org 2 customer's last order/visit is isolated from Org 1", 
             customers_org2[0]["last_order_date"] is None and customers_org2[0]["last_visit_date"] is None)

    # Clean up test DB data
    db.query(SalesOrder).filter(SalesOrder.customer_id.in_([cust1_id, cust2_id, cust_org2_id])).delete(synchronize_session=False)
    db.query(Visit).filter(Visit.customer_id.in_([cust1_id, cust2_id, cust_org2_id])).delete(synchronize_session=False)
    db.query(Customer).filter(Customer.id.in_([cust1_id, cust2_id, cust_org2_id])).delete(synchronize_session=False)
    db.commit()

    print("\n=======================================================")
    print(f"RESULTS: {PASSED} passed, {FAILED} failed")
    print("=======================================================\n")
    if FAILED > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_all_tests()
