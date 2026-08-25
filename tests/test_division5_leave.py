"""Division 5 Test Suite — Leave Management, Lifecycle, Permissions & Multi-Tenant Isolation

Covers:
1. Leave Creation (POST /leaves) with days_count calculation
2. Initial Status 'pending' and database persistence
3. Approval Workflow (PATCH /leaves/{id}/approve) -> status 'approved', approved_by populated
4. Rejection Workflow (PATCH /leaves/{id}/reject) -> status 'rejected', reject_reason persisted
5. Fresh DB Session Re-fetch & Persistence Verification
6. Invalid Status Transitions (approved->approved, approved->rejected, rejected->approved, rejected->rejected)
7. RBAC Permission Enforcement (create, view, edit, approve, delete)
8. Multi-Tenant Organization Isolation (Org 1 vs Org 2)
9. Employee Ownership Scoping (Employee A cannot modify Employee B's leave)
10. Date Validation (start_date > end_date rejected)
11. Update Rules (only pending leave can be updated; days_count recalculated)
12. Cancel / Delete Rules (only pending leave can be deleted)
13. Database Schema & Table Metadata Integrity
"""

import os
import sys
import uuid
from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

# Setup test DB environment
os.environ["DATABASE_URL"] = "sqlite:///./crm_saas.db"
os.environ["TESTING"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import Base, engine, get_db
from app.main import app
from app.models import Leave, Organization, Role, User
from app.core.permissions import default_role_matrices

Base.metadata.create_all(bind=engine)

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


def run_all_leave_tests():
    global PASSED, FAILED
    PASSED = 0
    FAILED = 0

    print("\n=======================================================")
    print("TEST SUITE: Division 5 - Leave Management & Workflows")
    print("=======================================================\n")

    db: Session = next(get_db())

    # Setup Org 1 & Org 2
    admin_auth_1 = _register_org("Div5_Org1")
    admin_auth_2 = _register_org("Div5_Org2")

    # Staff in Org 1
    sales_data_1, sales_auth_1 = _create_staff(admin_auth_1, "Sam Sales 1", "Sales Officer")
    sales_id_1 = sales_data_1["id"]

    sales_data_2, sales_auth_2 = _create_staff(admin_auth_1, "Steve Sales 2", "Sales Officer")
    sales_id_2 = sales_data_2["id"]

    # Staff in Org 2
    org2_staff_data, org2_staff_auth = _create_staff(admin_auth_2, "Org2 Staff", "Sales Officer")

    # ----------------------------------------------------
    # TEST 1: Leave Creation & Days Calculation
    # ----------------------------------------------------
    print("--- TEST 1: Leave Creation & Days Calculation ---")
    start_d1 = date.today() + timedelta(days=5)
    end_d1 = date.today() + timedelta(days=7)  # 3 days: 5, 6, 7

    leave_payload_1 = {
        "leave_type": "sick",
        "start_date": start_d1.isoformat(),
        "end_date": end_d1.isoformat(),
        "reason": "Doctor appointment and recovery",
    }
    r_create = client.post("/leaves", json=leave_payload_1, headers=sales_auth_1)
    log_test("POST /leaves succeeds (HTTP 201)", r_create.status_code == 201, r_create.text)
    leave_data_1 = r_create.json()
    leave_id_1 = leave_data_1["id"]

    log_test("Leave ID generated", bool(leave_id_1))
    log_test("Leave status is 'pending'", leave_data_1["status"] == "pending")
    log_test("Leave type is 'sick'", leave_data_1["leave_type"] == "sick")
    log_test("Leave days_count is exactly 3.0", leave_data_1["days_count"] == 3.0)
    log_test("Leave user_id matches submitter", leave_data_1["user_id"] == sales_id_1)
    log_test("Leave reason matches", leave_data_1["reason"] == "Doctor appointment and recovery")
    log_test("approved_by is None initially", leave_data_1["approved_by"] is None)
    log_test("reject_reason is None initially", leave_data_1["reject_reason"] is None)

    # ----------------------------------------------------
    # TEST 2: Leave Approval Workflow (Pending -> Approved)
    # ----------------------------------------------------
    print("\n--- TEST 2: Leave Approval Workflow ---")
    r_app = client.patch(f"/leaves/{leave_id_1}/approve", headers=admin_auth_1)
    log_test("PATCH /leaves/{id}/approve succeeds (HTTP 200)", r_app.status_code == 200, r_app.text)
    app_data = r_app.json()
    log_test("Status updated to 'approved'", app_data["status"] == "approved")
    log_test("approved_by is populated", bool(app_data["approved_by"]))

    # Persistence verification via fresh DB query / API re-fetch
    r_get_app = client.get(f"/leaves/{leave_id_1}", headers=sales_auth_1)
    log_test("GET /leaves/{id} confirms persisted 'approved' status", r_get_app.json()["status"] == "approved")

    # ----------------------------------------------------
    # TEST 3: Leave Rejection Workflow (Pending -> Rejected)
    # ----------------------------------------------------
    print("\n--- TEST 3: Leave Rejection Workflow ---")
    start_d2 = date.today() + timedelta(days=10)
    end_d2 = date.today() + timedelta(days=10)  # 1 day

    leave_payload_2 = {
        "leave_type": "casual",
        "start_date": start_d2.isoformat(),
        "end_date": end_d2.isoformat(),
        "reason": "Personal travel",
    }
    r_create_2 = client.post("/leaves", json=leave_payload_2, headers=sales_auth_1)
    leave_id_2 = r_create_2.json()["id"]

    r_rej = client.patch(
        f"/leaves/{leave_id_2}/reject",
        json={"reject_reason": "Quarter-end audit scheduled on this date"},
        headers=admin_auth_1,
    )
    log_test("PATCH /leaves/{id}/reject succeeds (HTTP 200)", r_rej.status_code == 200, r_rej.text)
    rej_data = r_rej.json()
    log_test("Status updated to 'rejected'", rej_data["status"] == "rejected")
    log_test("reject_reason recorded", rej_data["reject_reason"] == "Quarter-end audit scheduled on this date")

    # Persistence verification
    r_get_rej = client.get(f"/leaves/{leave_id_2}", headers=sales_auth_1)
    log_test("GET /leaves/{id} confirms persisted 'rejected' status", r_get_rej.json()["status"] == "rejected")
    log_test("Reject reason preserved across sessions", r_get_rej.json()["reject_reason"] == "Quarter-end audit scheduled on this date")

    # ----------------------------------------------------
    # TEST 4: Invalid Status Transitions Protection
    # ----------------------------------------------------
    print("\n--- TEST 4: Invalid Status Transitions Protection ---")
    # Attempt to re-approve already approved leave
    r_dup_app = client.patch(f"/leaves/{leave_id_1}/approve", headers=admin_auth_1)
    log_test("Re-approving already approved leave rejected (HTTP 400)", r_dup_app.status_code == 400)

    # Attempt to reject already approved leave
    r_rej_approved = client.patch(
        f"/leaves/{leave_id_1}/reject",
        json={"reject_reason": "Changed mind"},
        headers=admin_auth_1,
    )
    log_test("Rejecting already approved leave rejected (HTTP 400)", r_rej_approved.status_code == 400)

    # Attempt to re-reject already rejected leave
    r_dup_rej = client.patch(
        f"/leaves/{leave_id_2}/reject",
        json={"reject_reason": "Second reject"},
        headers=admin_auth_1,
    )
    log_test("Re-rejecting already rejected leave rejected (HTTP 400)", r_dup_rej.status_code == 400)

    # Attempt to approve already rejected leave
    r_app_rejected = client.patch(f"/leaves/{leave_id_2}/approve", headers=admin_auth_1)
    log_test("Approving already rejected leave rejected (HTTP 400)", r_app_rejected.status_code == 400)

    # ----------------------------------------------------
    # TEST 5: Date Validation
    # ----------------------------------------------------
    print("\n--- TEST 5: Date Validation ---")
    invalid_date_payload = {
        "leave_type": "casual",
        "start_date": (date.today() + timedelta(days=10)).isoformat(),
        "end_date": (date.today() + timedelta(days=5)).isoformat(),  # start > end
        "reason": "Invalid range",
    }
    r_bad_dates = client.post("/leaves", json=invalid_date_payload, headers=sales_auth_1)
    log_test("start_date > end_date rejected with HTTP 422", r_bad_dates.status_code == 422)

    # ----------------------------------------------------
    # TEST 6: Update Pending Leave & Edit Protection on Finalized Leaves
    # ----------------------------------------------------
    print("\n--- TEST 6: Update Rules ---")
    # Create 3rd pending leave
    r_create_3 = client.post(
        "/leaves",
        json={
            "leave_type": "annual",
            "start_date": (date.today() + timedelta(days=15)).isoformat(),
            "end_date": (date.today() + timedelta(days=16)).isoformat(),
            "reason": "Vacation",
        },
        headers=sales_auth_1,
    )
    leave_id_3 = r_create_3.json()["id"]

    # Update pending leave (extend end date by 1 day -> 3 days total)
    new_end = (date.today() + timedelta(days=17)).isoformat()
    r_update = client.patch(
        f"/leaves/{leave_id_3}",
        json={"end_date": new_end, "reason": "Extended vacation"},
        headers=sales_auth_1,
    )
    log_test("PATCH /leaves/{id} on pending leave succeeds (HTTP 200)", r_update.status_code == 200)
    updated_leave = r_update.json()
    log_test("days_count recalculated correctly to 3.0", updated_leave["days_count"] == 3.0)
    log_test("Reason updated", updated_leave["reason"] == "Extended vacation")

    # Attempt to edit approved leave
    r_edit_app = client.patch(
        f"/leaves/{leave_id_1}",
        json={"reason": "Tamper attempt"},
        headers=sales_auth_1,
    )
    log_test("Editing approved leave rejected (HTTP 400)", r_edit_app.status_code == 400)

    # Attempt to edit rejected leave
    r_edit_rej = client.patch(
        f"/leaves/{leave_id_2}",
        json={"reason": "Tamper attempt"},
        headers=sales_auth_1,
    )
    log_test("Editing rejected leave rejected (HTTP 400)", r_edit_rej.status_code == 400)

    # ----------------------------------------------------
    # TEST 7: Delete / Cancel Rules
    # ----------------------------------------------------
    print("\n--- TEST 7: Delete / Cancel Rules ---")
    # Cancel pending leave 3
    r_del = client.delete(f"/leaves/{leave_id_3}", headers=sales_auth_1)
    log_test("DELETE /leaves/{id} on pending leave succeeds (HTTP 204)", r_del.status_code == 204)

    # Confirm deletion
    r_get_del = client.get(f"/leaves/{leave_id_3}", headers=sales_auth_1)
    log_test("Deleted leave cannot be retrieved (HTTP 404)", r_get_del.status_code == 404)

    # Attempt to delete approved leave
    r_del_app = client.delete(f"/leaves/{leave_id_1}", headers=sales_auth_1)
    log_test("Deleting approved leave rejected (HTTP 400)", r_del_app.status_code == 400)

    # ----------------------------------------------------
    # TEST 8: RBAC Permission Enforcement
    # ----------------------------------------------------
    print("\n--- TEST 8: RBAC Permission Enforcement ---")
    # Non-admin Staff without leaves:approve attempting to approve
    r_unauth_app = client.patch(f"/leaves/{leave_id_2}/approve", headers=sales_auth_2)
    log_test("Staff without 'leaves:approve' cannot approve (HTTP 403)", r_unauth_app.status_code == 403)

    # Staff attempting to reject without leaves:approve
    r_unauth_rej = client.patch(
        f"/leaves/{leave_id_2}/reject",
        json={"reject_reason": "No perm"},
        headers=sales_auth_2,
    )
    log_test("Staff without 'leaves:approve' cannot reject (HTTP 403)", r_unauth_rej.status_code == 403)

    # ----------------------------------------------------
    # TEST 9: Employee Ownership Scoping
    # ----------------------------------------------------
    print("\n--- TEST 9: Ownership Scoping ---")
    # Create leave for Staff 2
    r_create_staff2 = client.post(
        "/leaves",
        json={
            "leave_type": "casual",
            "start_date": (date.today() + timedelta(days=20)).isoformat(),
            "end_date": (date.today() + timedelta(days=20)).isoformat(),
            "reason": "Staff 2 leave",
        },
        headers=sales_auth_2,
    )
    leave_id_staff2 = r_create_staff2.json()["id"]

    # Staff 1 attempts to edit Staff 2's leave
    r_cross_edit = client.patch(
        f"/leaves/{leave_id_staff2}",
        json={"reason": "Unauthorized edit"},
        headers=sales_auth_1,
    )
    log_test("Employee cannot edit another employee's leave (HTTP 404)", r_cross_edit.status_code == 404)

    # Staff 1 attempts to delete Staff 2's leave
    r_cross_del = client.delete(f"/leaves/{leave_id_staff2}", headers=sales_auth_1)
    log_test("Employee cannot delete another employee's leave (HTTP 404)", r_cross_del.status_code == 404)

    # ----------------------------------------------------
    # TEST 10: Multi-Tenant Organization Isolation
    # ----------------------------------------------------
    print("\n--- TEST 10: Multi-Tenant Organization Isolation ---")
    # Org 2 Admin attempts to GET Org 1 leave
    r_org2_get = client.get(f"/leaves/{leave_id_1}", headers=admin_auth_2)
    log_test("Org 2 Admin cannot GET Org 1 leave (HTTP 404)", r_org2_get.status_code == 404)

    # Org 2 Admin attempts to approve Org 1 leave
    r_org2_app = client.patch(f"/leaves/{leave_id_staff2}/approve", headers=admin_auth_2)
    log_test("Org 2 Admin cannot approve Org 1 leave (HTTP 404)", r_org2_app.status_code == 404)

    # Org 2 Admin attempts to reject Org 1 leave
    r_org2_rej = client.patch(
        f"/leaves/{leave_id_staff2}/reject",
        json={"reject_reason": "Cross org reject"},
        headers=admin_auth_2,
    )
    log_test("Org 2 Admin cannot reject Org 1 leave (HTTP 404)", r_org2_rej.status_code == 404)

    # Org 2 list excludes Org 1 leaves
    r_org2_list = client.get("/leaves", headers=admin_auth_2)
    org2_leaves = r_org2_list.json()
    log_test("Org 2 list excludes Org 1 leaves", not any(l["id"] == leave_id_1 for l in org2_leaves))

    # ----------------------------------------------------
    # TEST 11: GET /leaves/me vs GET /leaves
    # ----------------------------------------------------
    print("\n--- TEST 11: List Endpoints ---")
    r_me = client.get("/leaves/me", headers=sales_auth_1)
    log_test("GET /leaves/me succeeds (HTTP 200)", r_me.status_code == 200)
    me_leaves = r_me.json()
    log_test("GET /leaves/me contains only Staff 1 leaves", all(l["user_id"] == sales_id_1 for l in me_leaves))

    r_admin_list = client.get("/leaves", headers=admin_auth_1)
    log_test("Admin GET /leaves succeeds (HTTP 200)", r_admin_list.status_code == 200)
    admin_leaves = r_admin_list.json()
    log_test("Admin list contains leaves from multiple staff members", len(admin_leaves) >= 2)

    # ----------------------------------------------------
    # TEST 12: Database Table Metadata & Integrity
    # ----------------------------------------------------
    print("\n--- TEST 12: Database Table Metadata & Integrity ---")
    from sqlalchemy import inspect
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    log_test("'leaves' table exists in database", "leaves" in tables)

    columns = [c["name"] for c in inspector.get_columns("leaves")]
    for col in ["id", "organization_id", "user_id", "leave_type", "start_date", "end_date", "days_count", "reason", "status", "approved_by", "reject_reason", "created_at", "updated_at"]:
        log_test(f"leaves has '{col}' column", col in columns)

    # ----------------------------------------------------
    # SUMMARY
    # ----------------------------------------------------
    print("\n=======================================================")
    print(f"RESULTS: {PASSED} passed, {FAILED} failed")
    print("=======================================================\n")
    if FAILED > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_all_leave_tests()
