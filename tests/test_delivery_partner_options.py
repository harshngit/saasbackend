"""Comprehensive test suite for Delivery Partner lookup endpoint: GET /deliveries/partners.

Covers:
  - TEST 1: Non-admin staff (e.g. Sales Officer) access to GET /deliveries/partners (HTTP 200).
  - TEST 2: Admin access to GET /deliveries/partners (HTTP 200).
  - TEST 3: Role filtering — only Delivery Partner users returned (Admin, Sales, Accountant excluded).
  - TEST 4: Minimal response verification — response objects contain only 'id' and 'name' (no sensitive fields).
  - TEST 5: Tenant / organization isolation — Org 1 cannot see Org 2's delivery partners and vice-versa.
  - TEST 6: Inactive user filtering — deactivated delivery partners are excluded from dropdown results.
  - TEST 7: Unauthenticated request rejected (HTTP 401).
  - TEST 8: Security regression — GET /users remains strictly Admin-only (non-admin receives HTTP 403).
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from app.main import app
from app.seed import main as seed_main

seed_main()
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


def assert_true(condition: bool, msg: str):
    if condition:
        ok(msg)
    else:
        fail(msg, "Condition was False")


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
    auth = {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}
    return auth, email


def _create_staff(admin_auth: dict, name: str, role_name: str, password: str = "Password123!"):
    email = f"{role_name.lower().replace(' ', '_')}_{uuid.uuid4().hex[:6]}@example.com"
    res = client.post("/users", json={
        "name": name,
        "email": email,
        "password": password,
        "role": role_name,
    }, headers=admin_auth)
    assert res.status_code == 201, res.text
    user_data = res.json()
    # Login to get staff token
    login_res = client.post("/auth/login", json={
        "email": email,
        "password": password,
    })
    assert login_res.status_code == 200, login_res.text
    token = login_res.json()["tokens"]["access_token"]
    staff_auth = {"Authorization": f"Bearer {token}"}
    return user_data, staff_auth


def run_tests():
    print("\n=======================================================")
    print("TEST SUITE: GET /deliveries/partners (Dropdown Endpoint)")
    print("=======================================================\n")

    # 1. Setup Organization 1 with multiple roles
    auth_admin1, _ = _register_org("Firm1")

    # Create Sales Officer, Accountant, and 2 Delivery Partners
    sales_user, auth_sales = _create_staff(auth_admin1, "Alice Sales", "Sales Officer")
    acct_user, auth_acct = _create_staff(auth_admin1, "Bob Accountant", "Accountant")
    dp1_user, auth_dp1 = _create_staff(auth_admin1, "Dave Driver", "Delivery Partner")
    dp2_user, auth_dp2 = _create_staff(auth_admin1, "Dan Delivery", "Delivery Partner")

    # 2. Setup Organization 2 with a Delivery Partner
    auth_admin2, _ = _register_org("Firm2")
    dp_org2_user, auth_dp_org2 = _create_staff(auth_admin2, "Frank OtherOrg Driver", "Delivery Partner")

    # -------------------------------------------------------------------------
    # TEST 1: Non-admin access (Sales Officer calling GET /deliveries/partners)
    # -------------------------------------------------------------------------
    print("--- TEST 1: Non-Admin Staff Access ---")
    res_sales = client.get("/deliveries/partners", headers=auth_sales)
    assert_eq(res_sales.status_code, 200, "Sales Officer can access GET /deliveries/partners (HTTP 200)")
    data_sales = res_sales.json()
    assert_true(isinstance(data_sales, list), "Response is a list")
    assert_eq(len(data_sales), 2, "Returns exactly 2 delivery partners for Org 1")

    # -------------------------------------------------------------------------
    # TEST 2: Admin access
    # -------------------------------------------------------------------------
    print("\n--- TEST 2: Admin Access ---")
    res_admin = client.get("/deliveries/partners", headers=auth_admin1)
    assert_eq(res_admin.status_code, 200, "Admin can access GET /deliveries/partners (HTTP 200)")
    data_admin = res_admin.json()
    assert_eq(len(data_admin), 2, "Admin receives the exact same 2 delivery partners")

    # -------------------------------------------------------------------------
    # TEST 3: Only Delivery Partners returned
    # -------------------------------------------------------------------------
    print("\n--- TEST 3: Role Filtering ---")
    partner_ids = {p["id"] for p in data_sales}

    assert_true(dp1_user["id"] in partner_ids, "Delivery Partner 1 (Dave Driver) is in list")
    assert_true(dp2_user["id"] in partner_ids, "Delivery Partner 2 (Dan Delivery) is in list")
    assert_true(sales_user["id"] not in partner_ids, "Sales Officer (Alice Sales) is EXCLUDED")
    assert_true(acct_user["id"] not in partner_ids, "Accountant (Bob Accountant) is EXCLUDED")

    # -------------------------------------------------------------------------
    # TEST 4: Minimal response structure (No sensitive fields)
    # -------------------------------------------------------------------------
    print("\n--- TEST 4: Minimal Response Payload & Privacy ---")
    for item in data_sales:
        assert_true("id" in item, "Item has 'id'")
        assert_true("name" in item, "Item has 'name'")
        # Explicit check for prohibited fields
        assert_true("email" not in item, "Sensitive 'email' field is NOT present")
        assert_true("phone" not in item, "Sensitive 'phone' field is NOT present")
        assert_true("basic_salary" not in item, "Sensitive 'basic_salary' field is NOT present")
        assert_true("bank_name" not in item, "Sensitive 'bank_name' field is NOT present")
        assert_true("account_number" not in item, "Sensitive 'account_number' field is NOT present")
        assert_true("ifsc_swift_code" not in item, "Sensitive 'ifsc_swift_code' field is NOT present")
        assert_true("personal_email" not in item, "Sensitive 'personal_email' field is NOT present")
        assert_true("permissions" not in item, "Sensitive 'permissions' field is NOT present")
        assert_true("password_hash" not in item, "Sensitive 'password_hash' field is NOT present")
        assert_true("role_detail" not in item, "Sensitive 'role_detail' field is NOT present")

    # -------------------------------------------------------------------------
    # TEST 5: Tenant / Organization Isolation
    # -------------------------------------------------------------------------
    print("\n--- TEST 5: Organization Isolation ---")
    assert_true(dp_org2_user["id"] not in partner_ids, "Org 2 Delivery Partner is NOT visible to Org 1")

    res_org2 = client.get("/deliveries/partners", headers=auth_admin2)
    assert_eq(res_org2.status_code, 200, "Org 2 can access GET /deliveries/partners")
    org2_ids = {p["id"] for p in res_org2.json()}
    assert_eq(len(org2_ids), 1, "Org 2 sees exactly 1 delivery partner")
    assert_true(dp_org2_user["id"] in org2_ids, "Org 2 sees Frank OtherOrg Driver")
    assert_true(dp1_user["id"] not in org2_ids, "Org 2 CANNOT see Org 1 Delivery Partner 1")
    assert_true(dp2_user["id"] not in org2_ids, "Org 2 CANNOT see Org 1 Delivery Partner 2")

    # -------------------------------------------------------------------------
    # TEST 6: Inactive Delivery Partner Filtering
    # -------------------------------------------------------------------------
    print("\n--- TEST 6: Inactive Delivery Partner Filtering ---")
    # Deactivate dp2_user
    deactivate_res = client.patch(
        f"/users/{dp2_user['id']}",
        json={"status": "inactive"},
        headers=auth_admin1,
    )
    assert_eq(deactivate_res.status_code, 200, "Admin deactivates Delivery Partner 2")

    res_after_deact = client.get("/deliveries/partners", headers=auth_sales)
    assert_eq(res_after_deact.status_code, 200, "GET /deliveries/partners succeeds after deactivation")
    active_partner_ids = {p["id"] for p in res_after_deact.json()}
    assert_eq(len(active_partner_ids), 1, "Only 1 active delivery partner remains in list")
    assert_true(dp1_user["id"] in active_partner_ids, "Active partner (Dave Driver) remains in list")
    assert_true(dp2_user["id"] not in active_partner_ids, "Deactivated partner (Dan Delivery) is EXCLUDED")

    # -------------------------------------------------------------------------
    # TEST 7: Unauthenticated Request
    # -------------------------------------------------------------------------
    print("\n--- TEST 7: Unauthenticated Request ---")
    res_unauth = client.get("/deliveries/partners")
    assert_true(
        res_unauth.status_code in (401, 403),
        f"Unauthenticated request is rejected (got HTTP {res_unauth.status_code})",
    )

    # -------------------------------------------------------------------------
    # TEST 8: GET /users Security Regression (Must remain Admin-Only)
    # -------------------------------------------------------------------------
    print("\n--- TEST 8: GET /users Security Regression ---")
    res_users_sales = client.get("/users", headers=auth_sales)
    assert_eq(
        res_users_sales.status_code,
        403,
        "GET /users is STILL forbidden (HTTP 403) for non-admin Sales Officer",
    )
    res_users_admin = client.get("/users", headers=auth_admin1)
    assert_eq(
        res_users_admin.status_code,
        200,
        "GET /users remains accessible (HTTP 200) for Admin",
    )

    print("\n=======================================================")
    print(f"RESULTS: {_passed} passed, {_failed} failed")
    print("=======================================================\n")
    return _failed


if __name__ == "__main__":
    failed = run_tests()
    if failed > 0:
        sys.exit(1)
