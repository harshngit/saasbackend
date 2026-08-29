"""Test suite for GET /users/assignable — the minimal Follow-up assignee picker.

Covers:
1. Admin gets the assignable staff list.
2. Sales Officer (has follow_ups:create) gets the assignable staff list too.
3. Cross-organization users are never returned.
4. Inactive users are excluded.
5. Existing GET /users behaviour (admin-only, full profile) is unchanged.
"""

import os
import sys
import uuid

os.environ["DATABASE_URL"] = "sqlite:///./crm_saas.db"
os.environ["TESTING"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

client = TestClient(__import__("app.main", fromlist=["app"]).app)

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
    return {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}, email


def _create_staff(admin_auth: dict, name: str, role_name: str, is_active: bool = True):
    email = f"{role_name.lower().replace(' ', '_')}_{uuid.uuid4().hex[:6]}@example.com"
    res = client.post(
        "/users",
        json={"name": name, "email": email, "password": "Password123!", "role": role_name, "status": "active" if is_active else "inactive"},
        headers=admin_auth,
    )
    assert res.status_code == 201, res.text
    user_data = res.json()
    if is_active:
        login_res = client.post("/auth/login", json={"email": email, "password": "Password123!"})
        assert login_res.status_code == 200, login_res.text
        staff_auth = {"Authorization": f"Bearer {login_res.json()['tokens']['access_token']}"}
    else:
        staff_auth = None
    return user_data, staff_auth


def run_all_tests():
    global PASSED, FAILED
    PASSED = 0
    FAILED = 0

    print("\n=======================================================")
    print("TEST SUITE: GET /users/assignable")
    print("=======================================================\n")

    admin_auth, admin_email = _register_org(f"Assignable1_{uuid.uuid4().hex[:6]}")
    org2_auth, _ = _register_org(f"Assignable2_{uuid.uuid4().hex[:6]}")

    sales_data, sales_auth = _create_staff(admin_auth, "Rahul Sharma", "Sales Officer")
    deliv_data, deliv_auth = _create_staff(admin_auth, "Dan Delivery", "Delivery Partner")
    inactive_data, _ = _create_staff(admin_auth, "Inactive Sales", "Sales Officer", is_active=False)

    org2_sales_data, org2_sales_auth = _create_staff(org2_auth, "Other Org Sales", "Sales Officer")

    # ----------------------------------------------------
    # TEST 1: Admin gets the assignable staff list
    # ----------------------------------------------------
    print("--- TEST 1: Admin access ---")
    r = client.get("/users/assignable", headers=admin_auth)
    log_test("Admin: GET /users/assignable succeeds (200)", r.status_code == 200, r.text)
    admin_list = r.json()
    names = {u["name"] for u in admin_list}
    log_test("Admin list includes the Admin themself", any(u["role"] == "admin" for u in admin_list))
    log_test("Admin list includes Sales Officer (has follow_ups access)", "Rahul Sharma" in names)
    log_test("Admin list excludes Delivery Partner (no follow_ups access by default)", "Dan Delivery" not in names)
    log_test("Response rows are minimal (only id/name/role)", set(admin_list[0].keys()) == {"id", "name", "role"})
    sales_row = next(u for u in admin_list if u["name"] == "Rahul Sharma")
    log_test("Sales Officer role label is 'sales_officer'", sales_row["role"] == "sales_officer")

    # ----------------------------------------------------
    # TEST 2: Sales Officer (permitted role) gets the list too
    # ----------------------------------------------------
    print("\n--- TEST 2: Sales Officer access ---")
    r2 = client.get("/users/assignable", headers=sales_auth)
    log_test("Sales Officer: GET /users/assignable succeeds (200)", r2.status_code == 200, r2.text)
    sales_view_names = {u["name"] for u in r2.json()}
    log_test("Sales Officer sees themself in the list", "Rahul Sharma" in sales_view_names)

    print("\n--- TEST 2b: Delivery Partner (no follow_ups permission) is forbidden ---")
    r2b = client.get("/users/assignable", headers=deliv_auth)
    log_test("Delivery Partner: GET /users/assignable forbidden (403)", r2b.status_code == 403, r2b.text)

    # ----------------------------------------------------
    # TEST 3: Cross-organization users are never returned
    # ----------------------------------------------------
    print("\n--- TEST 3: Cross-organization isolation ---")
    r3 = client.get("/users/assignable", headers=admin_auth)
    org1_names = {u["name"] for u in r3.json()}
    log_test("Org 1 list does not include Org 2's Sales Officer", "Other Org Sales" not in org1_names)

    r3b = client.get("/users/assignable", headers=org2_sales_auth)
    log_test("Org 2 Sales Officer: GET /users/assignable succeeds (200)", r3b.status_code == 200)
    org2_names = {u["name"] for u in r3b.json()}
    log_test("Org 2 list does not include Org 1's Sales Officer", "Rahul Sharma" not in org2_names)
    log_test("Org 2 list includes its own Sales Officer", "Other Org Sales" in org2_names)
    log_test("Org 2 list does not include Org 1's Delivery Partner", "Dan Delivery" not in org2_names)

    # ----------------------------------------------------
    # TEST 4: Inactive users are excluded
    # ----------------------------------------------------
    print("\n--- TEST 4: Inactive users excluded ---")
    r4 = client.get("/users/assignable", headers=admin_auth)
    names4 = {u["name"] for u in r4.json()}
    log_test("Inactive staff member is excluded from assignable list", "Inactive Sales" not in names4)

    # ----------------------------------------------------
    # TEST 5: Existing GET /users behaviour unchanged
    # ----------------------------------------------------
    print("\n--- TEST 5: GET /users unchanged (admin-only, full profile) ---")
    r5_admin = client.get("/users", headers=admin_auth)
    log_test("Admin: GET /users still succeeds (200)", r5_admin.status_code == 200)
    full_row = next(u for u in r5_admin.json() if u["name"] == "Rahul Sharma")
    log_test("GET /users still returns full profile (has 'email' field)", "email" in full_row)
    log_test("GET /users still returns full profile (has 'system_role' field)", "system_role" in full_row)

    r5_sales = client.get("/users", headers=sales_auth)
    log_test("Sales Officer: GET /users still forbidden (403)", r5_sales.status_code == 403, r5_sales.text)

    r5_deliv = client.get("/users", headers=deliv_auth)
    log_test("Delivery Partner: GET /users still forbidden (403)", r5_deliv.status_code == 403, r5_deliv.text)

    print("\n=======================================================")
    print(f"RESULTS: {PASSED} passed, {FAILED} failed")
    print("=======================================================\n")
    if FAILED > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_all_tests()
