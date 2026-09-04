"""Comprehensive Division 4 Test Suite — Visit & Follow-up Lifecycle, Persistence & Isolation

Covers:
1. Visit creation for existing customer without any order
2. Confirming no SalesOrder or Invoice is created
3. Visit persistence and GET /visits/{id} retrieval
4. Visit fields (customer, lead, salesperson, date, purpose, notes, outcome, location)
5. Visit status updates and transitions (planned -> completed, planned -> cancelled, invalid rejection)
6. Follow-up task creation linked to visit (POST /visits/{id}/follow-ups and POST /follow-ups)
7. Follow-up persistence and GET /follow-ups/{id} retrieval
8. Multiple follow-ups associated with a single visit
9. Follow-up status update and completion (POST /follow-ups/{id}/complete)
10. Customer visit and follow-up history (GET /customers/{id}/visits, GET /customers/{id}/follow-ups)
11. Customer with NO order lifecycle
12. Visit and follow-up integrity after subsequent Sales Order creation
13. Multi-tenant isolation (Org A vs Org B)
14. RBAC Permission enforcement (Staff without visits/follow-ups permission rejected)
15. Staff Overview integration (visits_today, pending_followups, last_visit, next_followup)
16. Database schema and table creation integrity
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
from app.models import Customer, FollowUp, Lead, Product, Role, SalesOrder, User, Visit

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


def _create_staff(admin_auth: dict, name: str, role_name: str, password: str = "Password123!"):
    email = f"{role_name.lower().replace(' ', '_')}_{uuid.uuid4().hex[:6]}@example.com"
    res = client.post(
        "/users",
        json={
            "name": name,
            "email": email,
            "password": password,
            "role": role_name,
        },
        headers=admin_auth,
    )
    assert res.status_code == 201, res.text
    user_data = res.json()
    login_res = client.post(
        "/auth/login",
        json={
            "email": email,
            "password": password,
        },
    )
    assert login_res.status_code == 200, login_res.text
    token = login_res.json()["tokens"]["access_token"]
    staff_auth = {"Authorization": f"Bearer {token}"}
    return user_data, staff_auth


def run_all_tests():
    global PASSED, FAILED
    PASSED = 0
    FAILED = 0

    print("\n=======================================================")
    print("TEST SUITE: Division 4 - Visit & Follow-Up Lifecycle")
    print("=======================================================\n")

    db: Session = next(get_db())

    # ----------------------------------------------------
    # FIXTURES: Organizations & Users via API
    # ----------------------------------------------------
    admin_auth = _register_org("Div4_Firm1")
    org2_auth = _register_org("Div4_Firm2")

    # Sales Officer Org 1
    sales_data, sales_auth = _create_staff(admin_auth, "Sam Sales", "Sales Officer")
    sales_user_id = sales_data["id"]

    # Delivery Partner Org 1 (has no visits/follow_ups permissions)
    deliv_data, deliv_auth = _create_staff(admin_auth, "Dan Delivery", "Delivery Partner")

    # Create Customer in Org 1
    cust_res = client.post(
        "/customers",
        json={
            "name": "Alice Buyer",
            "business_name": "Acme Corp",
            "phone": "9876543210",
            "assigned_sales_officer_id": sales_user_id,
        },
        headers=admin_auth,
    )
    assert cust_res.status_code == 201, cust_res.text
    cust_a_id = cust_res.json()["id"]

    # Create Customer in Org 2
    cust2_res = client.post(
        "/customers",
        json={
            "name": "Bob OtherOrg",
            "business_name": "Beta Org Inc",
            "phone": "9123456789",
        },
        headers=org2_auth,
    )
    assert cust2_res.status_code == 201, cust2_res.text
    cust_b_id = cust2_res.json()["id"]

    # Create Lead in Org 1
    lead_res = client.post(
        "/leads",
        json={
            "name": "Prospect Lead",
            "contact_person": "Alice Buyer",
            "customer_id": cust_a_id,
            "assigned_salesperson_id": sales_user_id,
            "mobile_number": "9555566667",
            "lead_source": "Referral",
        },
        headers=admin_auth,
    )
    assert lead_res.status_code == 201, lead_res.text
    lead_a_id = lead_res.json()["id"]

    # ----------------------------------------------------
    # TEST 1: Visit Creation Without Any Order
    # ----------------------------------------------------
    print("--- TEST 1: Visit Creation Without Any Order ---")
    visit_time = datetime.now(timezone.utc).isoformat()
    visit_payload = {
        "customer_id": cust_a_id,
        "lead_id": lead_a_id,
        "salesperson_id": sales_user_id,
        "visit_date": visit_time,
        "visit_type": "site_visit",
        "purpose": "Quarterly requirement discussion",
        "notes": "Discussed bulk supply of components",
        "outcome": "follow_up_required",
        "status": "completed",
        "location": "Acme Headquarters, Floor 4",
    }
    r = client.post("/visits", json=visit_payload, headers=sales_auth)
    log_test("POST /visits succeeds (HTTP 201)", r.status_code == 201, r.text)
    visit_data = r.json()
    visit_id = visit_data["id"]

    log_test("Visit ID returned", bool(visit_id))
    log_test("Visit customer matches Customer A", visit_data["customer_id"] == cust_a_id)
    log_test("Visit lead matches Lead A", visit_data["lead_id"] == lead_a_id)
    log_test("Visit user matches Sales User", visit_data["user_id"] == sales_user_id)
    log_test("Visit type is 'site_visit'", visit_data["visit_type"] == "site_visit")
    log_test("Visit status is 'completed'", visit_data["status"] == "completed")
    log_test("Visit purpose matches", visit_data["purpose"] == "Quarterly requirement discussion")
    log_test("Visit outcome matches", visit_data["outcome"] == "follow_up_required")
    log_test("Visit location matches", visit_data["location"] == "Acme Headquarters, Floor 4")
    log_test("Customer brief populated", visit_data.get("customer", {}).get("name") == "Alice Buyer")

    # Confirm NO Sales Order or Invoice was created
    orders_count = db.query(SalesOrder).filter(SalesOrder.customer_id == cust_a_id).count()
    log_test("No SalesOrder was created automatically", orders_count == 0)

    # ----------------------------------------------------
    # TEST 2: Visit Persistence & Re-fetch (GET /visits/{id})
    # ----------------------------------------------------
    print("\n--- TEST 2: Visit Persistence & Re-fetch ---")
    r_get = client.get(f"/visits/{visit_id}", headers=sales_auth)
    log_test("GET /visits/{id} succeeds (HTTP 200)", r_get.status_code == 200)
    fetched_visit = r_get.json()
    log_test("Fetched visit id matches", fetched_visit["id"] == visit_id)
    log_test("Fetched visit status matches", fetched_visit["status"] == "completed")
    log_test("Fetched notes match", fetched_visit["notes"] == "Discussed bulk supply of components")

    # ----------------------------------------------------
    # TEST 3: Visit List & Filtering (GET /visits)
    # ----------------------------------------------------
    print("\n--- TEST 3: Visit List & Filtering ---")
    r_list = client.get(f"/visits?customer_id={cust_a_id}", headers=sales_auth)
    log_test("GET /visits?customer_id={id} succeeds", r_list.status_code == 200)
    visits_list = r_list.json()
    log_test("Returns list with 1 visit", len(visits_list) == 1)
    log_test("Returned visit ID matches", visits_list[0]["id"] == visit_id)

    # ----------------------------------------------------
    # TEST 4: Visit Status Lifecycle & Update (PATCH /visits/{id})
    # ----------------------------------------------------
    print("\n--- TEST 4: Visit Status Lifecycle & Update ---")
    # Create a planned visit
    v2_payload = {
        "customer_id": cust_a_id,
        "purpose": "Follow-up Demo",
        "status": "planned",
    }
    r_v2 = client.post("/visits", json=v2_payload, headers=sales_auth)
    log_test("Planned visit created", r_v2.status_code == 201)
    v2_id = r_v2.json()["id"]

    # Update to completed
    r_patch = client.patch(
        f"/visits/{v2_id}",
        json={"status": "completed", "outcome": "meeting_completed"},
        headers=sales_auth,
    )
    log_test("PATCH /visits/{id} to completed succeeds", r_patch.status_code == 200)
    updated_v2 = r_patch.json()
    log_test("Status updated to completed", updated_v2["status"] == "completed")
    log_test("Outcome updated", updated_v2["outcome"] == "meeting_completed")

    # Invalid status rejected
    r_bad_status = client.patch(f"/visits/{v2_id}", json={"status": "invalid_status"}, headers=sales_auth)
    log_test("Invalid status update rejected with HTTP 400", r_bad_status.status_code == 400)

    # ----------------------------------------------------
    # TEST 5: Follow-Up Creation Linked to Visit (POST /visits/{id}/follow-ups)
    # ----------------------------------------------------
    print("\n--- TEST 5: Follow-Up Task Linked to Visit ---")
    due_date_1 = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    fu_payload = {
        "title": "Send formal quotation",
        "description": "Send proposal for 500 units discussed during visit",
        "due_date": due_date_1,
        "priority": "high",
        "status": "pending",
        "assigned_to_id": sales_user_id,
    }
    r_fu = client.post(f"/visits/{visit_id}/follow-ups", json=fu_payload, headers=sales_auth)
    log_test("POST /visits/{id}/follow-ups succeeds (HTTP 201)", r_fu.status_code == 201, r_fu.text)
    fu_data = r_fu.json()
    fu_id_1 = fu_data["id"]

    log_test("Follow-up ID generated", bool(fu_id_1))
    log_test("Follow-up linked to visit_id", fu_data["visit_id"] == visit_id)
    log_test("Follow-up inherited customer_id from visit", fu_data["customer_id"] == cust_a_id)
    log_test("Follow-up title matches", fu_data["title"] == "Send formal quotation")
    log_test("Follow-up priority is 'high'", fu_data["priority"] == "high")
    log_test("Follow-up status is 'pending'", fu_data["status"] == "pending")
    log_test("Follow-up completed_at is None initially", fu_data["completed_at"] is None)

    # ----------------------------------------------------
    # TEST 6: Multiple Follow-Ups for One Visit
    # ----------------------------------------------------
    print("\n--- TEST 6: Multiple Follow-Ups for One Visit ---")
    due_date_2 = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    fu_payload_2 = {
        "title": "Call procurement officer",
        "description": "Confirm quotation receipt",
        "due_date": due_date_2,
        "priority": "medium",
        "status": "pending",
        "assigned_to_id": sales_user_id,
    }
    r_fu_2 = client.post(f"/visits/{visit_id}/follow-ups", json=fu_payload_2, headers=sales_auth)
    log_test("Second follow-up created under same visit", r_fu_2.status_code == 201)
    fu_id_2 = r_fu_2.json()["id"]

    # Re-fetch visit to check populated follow_ups list
    r_visit_populated = client.get(f"/visits/{visit_id}", headers=sales_auth)
    visit_pop_data = r_visit_populated.json()
    log_test("Visit includes both follow-up tasks in 'follow_ups'", len(visit_pop_data["follow_ups"]) == 2)

    # ----------------------------------------------------
    # TEST 7: Standalone Follow-Up Creation (POST /follow-ups)
    # ----------------------------------------------------
    print("\n--- TEST 7: Standalone Follow-Up Creation ---")
    standalone_fu_payload = {
        "customer_id": cust_a_id,
        "title": "Annual contract review",
        "due_date": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
        "priority": "low",
        "status": "pending",
    }
    r_s_fu = client.post("/follow-ups", json=standalone_fu_payload, headers=sales_auth)
    log_test("POST /follow-ups succeeds for standalone task", r_s_fu.status_code == 201)
    s_fu_data = r_s_fu.json()
    log_test("Standalone follow-up has visit_id = None", s_fu_data["visit_id"] is None)

    # ----------------------------------------------------
    # TEST 8: Follow-Up Status Lifecycle & Completion
    # ----------------------------------------------------
    print("\n--- TEST 8: Follow-Up Status Lifecycle & Completion ---")
    # Mark task 1 as completed via POST /follow-ups/{id}/complete
    r_complete = client.post(f"/follow-ups/{fu_id_1}/complete", headers=sales_auth)
    log_test("POST /follow-ups/{id}/complete succeeds", r_complete.status_code == 200)
    comp_fu = r_complete.json()
    log_test("Follow-up status is now 'completed'", comp_fu["status"] == "completed")
    log_test("Follow-up completed_at timestamp is populated", comp_fu["completed_at"] is not None)

    # Re-fetch to ensure persistence
    r_fu_check = client.get(f"/follow-ups/{fu_id_1}", headers=sales_auth)
    log_test(
        "GET /follow-ups/{id} confirms persisted 'completed' status",
        r_fu_check.json()["status"] == "completed",
    )

    # ----------------------------------------------------
    # TEST 9: Customer Visit & Follow-Up History Endpoints
    # ----------------------------------------------------
    print("\n--- TEST 9: Customer History Endpoints ---")
    r_cust_visits = client.get(f"/customers/{cust_a_id}/visits", headers=sales_auth)
    log_test("GET /customers/{id}/visits succeeds (HTTP 200)", r_cust_visits.status_code == 200)
    cust_visits = r_cust_visits.json()
    log_test("Customer has 2 visits recorded", len(cust_visits) == 2)

    r_cust_fus = client.get(f"/customers/{cust_a_id}/follow-ups", headers=sales_auth)
    log_test("GET /customers/{id}/follow-ups succeeds (HTTP 200)", r_cust_fus.status_code == 200)
    cust_fus = r_cust_fus.json()
    log_test("Customer has 3 follow-ups recorded", len(cust_fus) == 3)

    # ----------------------------------------------------
    # TEST 10: Visit & Follow-Up Integrity After Later Sales Order
    # ----------------------------------------------------
    print("\n--- TEST 10: Later Sales Order Creation ---")
    wh_res = client.post("/warehouses", json={"name": "Main Warehouse", "is_default": True}, headers=admin_auth)
    wh_id = wh_res.json()["id"]

    prod_res = client.post(
        "/products",
        json={
            "name": "Widget Div4",
            "sku": f"SKU-{uuid.uuid4().hex[:4]}",
            "price": 100.0,
        },
        headers=admin_auth,
    )
    prod_id = prod_res.json()["id"]
    prod = db.get(Product, prod_id)
    prod.total_inventory = 200
    db.commit()

    # Adjust warehouse stock
    client.post(
        "/inventory/adjust",
        json={"warehouse_id": wh_id, "product_id": prod_id, "quantity": 100, "reason": "Initial stock"},
        headers=admin_auth,
    )

    order_payload = {
        "customer_id": cust_a_id,
        "warehouse_id": wh_id,
        "items": [{"product_id": prod_id, "quantity": 10, "unit_price": 100.0}],
    }
    r_order = client.post("/orders", json=order_payload, headers=admin_auth)
    log_test("Sales Order created successfully", r_order.status_code == 201, r_order.text)

    # Verify visit and follow-ups remain completely intact
    r_v_after = client.get(f"/visits/{visit_id}", headers=sales_auth)
    log_test("Visit remains intact after sales order creation", r_v_after.status_code == 200)
    log_test("Visit notes unchanged", r_v_after.json()["notes"] == "Discussed bulk supply of components")

    r_fu_after = client.get(f"/follow-ups/{fu_id_1}", headers=sales_auth)
    log_test("Follow-up remains intact after sales order creation", r_fu_after.status_code == 200)
    log_test("Follow-up status still 'completed'", r_fu_after.json()["status"] == "completed")

    # ----------------------------------------------------
    # TEST 11: Multi-Tenant Organization Isolation
    # ----------------------------------------------------
    print("\n--- TEST 11: Multi-Tenant Organization Isolation ---")
    # Org 2 cannot read Org 1 Visit
    r_cross_v = client.get(f"/visits/{visit_id}", headers=org2_auth)
    log_test("Org 2 cannot GET Org 1 visit (HTTP 404)", r_cross_v.status_code == 404)

    # Org 2 cannot patch Org 1 Visit
    r_cross_patch = client.patch(f"/visits/{visit_id}", json={"status": "cancelled"}, headers=org2_auth)
    log_test("Org 2 cannot PATCH Org 1 visit (HTTP 404)", r_cross_patch.status_code == 404)

    # Org 2 cannot read Org 1 Follow-up
    r_cross_fu = client.get(f"/follow-ups/{fu_id_1}", headers=org2_auth)
    log_test("Org 2 cannot GET Org 1 follow-up (HTTP 404)", r_cross_fu.status_code == 404)

    # Org 2 cannot create follow-up against Org 1 visit
    r_cross_fu_create = client.post(f"/visits/{visit_id}/follow-ups", json=fu_payload, headers=org2_auth)
    log_test(
        "Org 2 cannot create follow-up for Org 1 visit (HTTP 400/404)",
        r_cross_fu_create.status_code in (400, 404),
    )

    # Org 1 cannot create visit for Org 2 customer
    r_cross_cust_visit = client.post(
        "/visits",
        json={"customer_id": cust_b_id, "purpose": "Cross org"},
        headers=admin_auth,
    )
    log_test(
        "Cannot create visit for other organization's customer (HTTP 400)",
        r_cross_cust_visit.status_code == 400,
    )

    # ----------------------------------------------------
    # TEST 12: RBAC Permission Enforcement
    # ----------------------------------------------------
    print("\n--- TEST 12: RBAC Permission Enforcement ---")
    # User with no visit/follow_up permissions (Delivery Partner role)
    r_no_perm_v = client.get("/visits", headers=deliv_auth)
    log_test("User without 'visits:view' forbidden (HTTP 403)", r_no_perm_v.status_code == 403)

    r_no_perm_create_v = client.post("/visits", json=visit_payload, headers=deliv_auth)
    log_test("User without 'visits:create' forbidden (HTTP 403)", r_no_perm_create_v.status_code == 403)

    r_no_perm_fu = client.get("/follow-ups", headers=deliv_auth)
    log_test("User without 'follow_ups:view' forbidden (HTTP 403)", r_no_perm_fu.status_code == 403)

    r_no_perm_create_fu = client.post("/follow-ups", json=fu_payload, headers=deliv_auth)
    log_test("User without 'follow_ups:create' forbidden (HTTP 403)", r_no_perm_create_fu.status_code == 403)

    # ----------------------------------------------------
    # TEST 13: Staff Overview Operational Integration
    # ----------------------------------------------------
    print("\n--- TEST 13: Staff Overview Operational Integration ---")
    # Query staff overview for sales_user
    r_overview = client.get(f"/users/{sales_user_id}/overview?period=month", headers=admin_auth)
    log_test("GET /users/{id}/overview succeeds (HTTP 200)", r_overview.status_code == 200, r_overview.text)
    overview_data = r_overview.json()

    summary = overview_data.get("summary", {})
    log_test("Staff overview 'visits' reflects actual count (>= 2)", summary.get("visits", 0) >= 2)
    log_test(
        "Staff overview 'pending_followups' reflects actual count (>= 1)",
        summary.get("pending_followups", 0) >= 1,
    )

    assigned_customers = overview_data.get("assigned_customers", [])
    cust_row = next((c for c in assigned_customers if c["id"] == cust_a_id), None)
    log_test("Customer row present in assigned_customers", cust_row is not None)
    if cust_row:
        log_test("Customer row has populated last_visit", cust_row["last_visit"] is not None)
        log_test("Customer row has populated next_followup", cust_row["next_followup"] is not None)

    # ----------------------------------------------------
    # TEST 14: Database Table Metadata & Integrity
    # ----------------------------------------------------
    print("\n--- TEST 14: Database Table Metadata & Integrity ---")
    from sqlalchemy import inspect

    inspector = inspect(engine)
    tables = inspector.get_table_names()
    log_test("'visits' table exists in database", "visits" in tables)
    log_test("'follow_ups' table exists in database", "follow_ups" in tables)

    visit_cols = [c["name"] for c in inspector.get_columns("visits")]
    log_test("visits has 'organization_id'", "organization_id" in visit_cols)
    log_test("visits has 'customer_id'", "customer_id" in visit_cols)
    log_test("visits has 'visit_date'", "visit_date" in visit_cols)
    log_test("visits has 'status'", "status" in visit_cols)

    fu_cols = [c["name"] for c in inspector.get_columns("follow_ups")]
    log_test("follow_ups has 'organization_id'", "organization_id" in fu_cols)
    log_test("follow_ups has 'visit_id'", "visit_id" in fu_cols)
    log_test("follow_ups has 'due_date'", "due_date" in fu_cols)
    log_test("follow_ups has 'status'", "status" in fu_cols)

    # ----------------------------------------------------
    # SUMMARY
    # ----------------------------------------------------
    print("\n=======================================================")
    print(f"RESULTS: {PASSED} passed, {FAILED} failed")
    print("=======================================================\n")
    if FAILED > 0:
        sys.exit(1)


def run_visit_lifecycle_ownership_tests():
    """Visit lifecycle (planned -> in_progress -> completed / cancelled),
    lifecycle timestamps, controlled outcomes (including ready_to_convert
    NOT auto-converting a Lead), and Lead Visit default-ownership behavior.
    Self-contained: its own org/fixtures, own PASSED/FAILED counters, same
    style as run_all_tests() above.
    """
    global PASSED, FAILED
    PASSED = 0
    FAILED = 0

    print("\n=======================================================")
    print("TEST SUITE: Visit Lifecycle, Timestamps, Outcomes & Lead Visit Ownership")
    print("=======================================================\n")

    admin_auth = _register_org("Div4_Lifecycle")
    org2_auth = _register_org("Div4_Lifecycle_Org2")

    so_a, so_a_auth = _create_staff(admin_auth, "Officer A", "Sales Officer")
    so_x, so_x_auth = _create_staff(admin_auth, "Officer X", "Sales Officer")
    so_b, _so_b_auth = _create_staff(org2_auth, "Officer B", "Sales Officer")

    cust_res = client.post(
        "/customers", json={"name": "Lifecycle Customer", "phone": "9000000001"}, headers=admin_auth
    )
    assert cust_res.status_code == 201, cust_res.text
    cust_id = cust_res.json()["id"]

    lead_res = client.post(
        "/leads",
        json={
            "name": "Lifecycle Lead",
            "mobile_number": "9111111111",
            "lead_source": "Referral",
            "assigned_salesperson_id": so_a["id"],
        },
        headers=admin_auth,
    )
    assert lead_res.status_code == 201, lead_res.text
    lead_id = lead_res.json()["id"]

    # ----------------------------------------------------
    # Required Test 1: planned -> in_progress sets checked_in_at
    # ----------------------------------------------------
    print("--- Required Test 1: planned -> in_progress ---")
    r = client.post("/visits", json={"lead_id": lead_id, "visit_type": "site_visit"}, headers=admin_auth)
    log_test("Visit created (201)", r.status_code == 201, r.text)
    v_data = r.json()
    v_id = v_data["id"]
    log_test("Initial status is 'planned'", v_data["status"] == "planned")
    log_test("Initial checked_in_at is null", v_data["checked_in_at"] is None)

    r = client.patch(f"/visits/{v_id}", json={"status": "in_progress"}, headers=admin_auth)
    log_test("PATCH to in_progress succeeds (200)", r.status_code == 200, r.text)
    v_data = r.json()
    log_test("Status is 'in_progress'", v_data["status"] == "in_progress")
    log_test("checked_in_at populated", v_data["checked_in_at"] is not None)
    first_checked_in_at = v_data["checked_in_at"]

    # ----------------------------------------------------
    # Required Test 2: repeated in_progress update doesn't overwrite checked_in_at
    # ----------------------------------------------------
    print("--- Required Test 2: checked_in_at not overwritten ---")
    r = client.patch(
        f"/visits/{v_id}", json={"status": "in_progress", "notes": "still on site"}, headers=admin_auth
    )
    log_test("Repeated in_progress PATCH succeeds", r.status_code == 200, r.text)
    log_test("checked_in_at unchanged", r.json()["checked_in_at"] == first_checked_in_at)

    # ----------------------------------------------------
    # Required Test 3: in_progress -> completed sets checked_out_at/completed_at
    # ----------------------------------------------------
    print("--- Required Test 3: completion timestamps ---")
    r = client.patch(
        f"/visits/{v_id}", json={"status": "completed", "outcome": "meeting_completed"}, headers=admin_auth
    )
    log_test("PATCH to completed succeeds", r.status_code == 200, r.text)
    v_data = r.json()
    log_test("Status is 'completed'", v_data["status"] == "completed")
    log_test("checked_out_at populated", v_data["checked_out_at"] is not None)
    log_test("completed_at populated", v_data["completed_at"] is not None)
    log_test("checked_in_at preserved from earlier check-in", v_data["checked_in_at"] == first_checked_in_at)

    # ----------------------------------------------------
    # Required Test 4: direct planned -> completed compatibility
    # ----------------------------------------------------
    print("--- Required Test 4: direct planned -> completed ---")
    r = client.post("/visits", json={"customer_id": cust_id, "visit_type": "call"}, headers=admin_auth)
    log_test("Second visit created (201)", r.status_code == 201, r.text)
    v2_id = r.json()["id"]
    r = client.patch(f"/visits/{v2_id}", json={"status": "completed"}, headers=admin_auth)
    log_test("Direct planned -> completed succeeds", r.status_code == 200, r.text)
    v2_data = r.json()
    log_test("checked_in_at NOT invented", v2_data["checked_in_at"] is None)
    log_test("checked_out_at populated", v2_data["checked_out_at"] is not None)
    log_test("completed_at populated", v2_data["completed_at"] is not None)

    # ----------------------------------------------------
    # Required Test 5: cancellation
    # ----------------------------------------------------
    print("--- Required Test 5: cancellation with reason ---")
    r = client.post("/visits", json={"customer_id": cust_id, "visit_type": "call"}, headers=admin_auth)
    v3_id = r.json()["id"]
    r = client.patch(
        f"/visits/{v3_id}",
        json={"status": "cancelled", "cancellation_reason": "Customer requested rescheduling"},
        headers=admin_auth,
    )
    log_test("PATCH to cancelled succeeds", r.status_code == 200, r.text)
    v3_data = r.json()
    log_test("Status is 'cancelled'", v3_data["status"] == "cancelled")
    log_test("cancelled_at populated", v3_data["cancelled_at"] is not None)
    log_test("cancellation_reason persisted", v3_data["cancellation_reason"] == "Customer requested rescheduling")

    # ----------------------------------------------------
    # Required Test 6: controlled outcomes
    # ----------------------------------------------------
    print("--- Required Test 6: controlled Visit outcomes ---")
    approved_outcomes = [
        "interested", "follow_up_required", "ready_to_convert",
        "not_interested", "meeting_completed", "other",
    ]
    for outcome in approved_outcomes:
        r = client.post(
            "/visits", json={"customer_id": cust_id, "visit_type": "call", "outcome": outcome}, headers=admin_auth
        )
        log_test(f"Outcome '{outcome}' accepted (201)", r.status_code == 201, r.text)

    r = client.post(
        "/visits",
        json={"customer_id": cust_id, "visit_type": "call", "outcome": "not_a_real_outcome"},
        headers=admin_auth,
    )
    log_test("Invalid outcome rejected (400)", r.status_code == 400, r.text)

    # ----------------------------------------------------
    # Required Test 7: ready_to_convert does NOT auto-convert the Lead
    # ----------------------------------------------------
    print("--- Required Test 7: ready_to_convert does not auto-convert Lead ---")
    r = client.post(
        "/visits",
        json={"lead_id": lead_id, "visit_type": "site_visit", "outcome": "ready_to_convert"},
        headers=admin_auth,
    )
    log_test("Visit with outcome='ready_to_convert' created", r.status_code == 201, r.text)
    r_lead = client.get(f"/leads/{lead_id}", headers=admin_auth)
    log_test(
        "Lead.lead_status is still not 'won'",
        r_lead.status_code == 200 and r_lead.json().get("lead_status") != "won",
        r_lead.text,
    )

    # ----------------------------------------------------
    # Required Test 8: Lead Visit default ownership
    # ----------------------------------------------------
    print("--- Required Test 8: Lead Visit defaults to lead.assigned_salesperson_id ---")
    r = client.post("/visits", json={"lead_id": lead_id, "visit_type": "site_visit"}, headers=admin_auth)
    log_test("Admin-created Lead visit succeeds", r.status_code == 201, r.text)
    v8_data = r.json()
    log_test("visit.user_id defaults to assigned Sales Officer A", v8_data["user_id"] == so_a["id"])

    r_own = client.get(f"/visits?lead_id={lead_id}", headers=so_a_auth)
    log_test(
        "Sales Officer A (data_scope=own) sees the visit",
        r_own.status_code == 200 and any(x["id"] == v8_data["id"] for x in r_own.json()),
        r_own.text,
    )

    r_other = client.get(f"/visits?lead_id={lead_id}", headers=so_x_auth)
    log_test(
        "Unrelated Sales Officer X does NOT see the visit",
        r_other.status_code == 200 and not any(x["id"] == v8_data["id"] for x in r_other.json()),
        r_other.text,
    )

    # ----------------------------------------------------
    # Required Test 9: explicit ownership override preserved
    # ----------------------------------------------------
    print("--- Required Test 9: explicit user_id override preserved ---")
    r = client.post(
        "/visits",
        json={"lead_id": lead_id, "visit_type": "site_visit", "user_id": so_x["id"]},
        headers=admin_auth,
    )
    log_test("Explicit user_id visit created", r.status_code == 201, r.text)
    log_test("visit.user_id honors explicit override", r.json()["user_id"] == so_x["id"])

    # ----------------------------------------------------
    # Required Test 10: Customer Visit ownership regression
    # ----------------------------------------------------
    print("--- Required Test 10: Customer-only visit ownership unchanged ---")
    r = client.post("/visits", json={"customer_id": cust_id, "visit_type": "call"}, headers=so_a_auth)
    log_test("Customer-only visit created by Sales Officer A", r.status_code == 201, r.text)
    log_test("Defaults to the creating user (unaffected by Lead defaulting)", r.json()["user_id"] == so_a["id"])

    # ----------------------------------------------------
    # Required Test 11: cross-organization protection
    # ----------------------------------------------------
    print("--- Required Test 11: cross-organization Lead/User rejected ---")
    r = client.post("/visits", json={"lead_id": lead_id, "visit_type": "call"}, headers=org2_auth)
    log_test("Org 2 cannot create a Visit against Org 1's Lead (400)", r.status_code == 400, r.text)

    r = client.post(
        "/visits",
        json={"lead_id": lead_id, "visit_type": "call", "user_id": so_b["id"]},
        headers=admin_auth,
    )
    log_test("Org 1 cannot assign a Visit to Org 2's user (400)", r.status_code == 400, r.text)

    # ----------------------------------------------------
    # Required Test 12: existing Lead Follow-up flows regression
    # ----------------------------------------------------
    print("--- Required Test 12: existing Lead Follow-up flows unaffected ---")
    r = client.post(
        "/follow-ups",
        json={"lead_id": lead_id, "title": "Regression check", "due_date": "2026-09-10T00:00:00Z"},
        headers=admin_auth,
    )
    log_test("POST /follow-ups with lead_id still works", r.status_code == 201, r.text)

    r = client.get(f"/follow-ups?lead_id={lead_id}", headers=admin_auth)
    log_test("GET /follow-ups?lead_id= still works", r.status_code == 200 and len(r.json()) >= 1, r.text)

    r = client.post(
        f"/visits/{v_id}/follow-ups",
        json={"title": "Via visit", "due_date": "2026-09-11T00:00:00Z"},
        headers=admin_auth,
    )
    log_test("POST /visits/{visit_id}/follow-ups still works", r.status_code == 201, r.text)

    print("\n=======================================================")
    print(f"RESULTS: {PASSED} passed, {FAILED} failed")
    print("=======================================================\n")
    if FAILED > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_all_tests()
    run_visit_lifecycle_ownership_tests()
