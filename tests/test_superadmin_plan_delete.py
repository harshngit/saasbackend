"""Focused test suite for Super Admin Delete Plan API.

Covers:
  - Super Admin can delete an unused plan (HTTP 204).
  - Deleted plan no longer appears in GET /superadmin/plans or GET /plans.
  - Non-Super-Admin receives 403 Forbidden.
  - Unauthenticated request receives 401/403.
  - Non-existent plan returns 404 Not Found.
  - Default plan deletion is rejected (HTTP 400 Bad Request).
  - Plan referenced by an organization (plan_id) cannot be deleted (HTTP 409 Conflict).
  - Plan requested by an organization (requested_plan_id) cannot be deleted (HTTP 409 Conflict).
  - Deletion rejection preserves existing organization subscription data intact.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from app.main import app
from app.core.config import settings
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


def _super_admin_auth():
    r = client.post("/auth/login", json={
        "email": settings.super_admin_email, "password": settings.super_admin_password,
    })
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}


def _normal_admin_auth():
    email = f"admin_{uuid.uuid4().hex[:8]}@orgtest.com"
    r = client.post("/auth/register", json={
        "organization_name": f"Org_{uuid.uuid4().hex[:6]}",
        "admin_name": "Org Admin",
        "email": email,
        "password": "Password123!",
        "role": "admin",
    })
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}


def run_tests():
    print("\n=======================================================")
    print("TEST SUITE: Super Admin Delete Plan API")
    print("=======================================================")

    root_auth = _super_admin_auth()
    admin_auth = _normal_admin_auth()

    # --- 1. Create a disposable plan for delete tests ---
    print("\n--- 1. Create and Delete Unused Plan ---")
    plan_name = f"Custom Test Plan {uuid.uuid4().hex[:6]}"
    create_res = client.post("/superadmin/plans", json={
        "name": plan_name,
        "price_monthly": 49.0,
        "price_yearly": 490.0,
        "features": ["Feature 1", "Feature 2"],
        "is_default": False,
    }, headers=root_auth)
    assert_eq(create_res.status_code, 201, "Super Admin creates custom plan")
    plan_id = create_res.json()["id"]

    # Verify plan appears in both lists
    sa_plans = client.get("/superadmin/plans", headers=root_auth).json()
    assert any(p["id"] == plan_id for p in sa_plans), "New plan listed in GET /superadmin/plans"

    pub_plans = client.get("/plans", headers=admin_auth).json()
    assert any(p["id"] == plan_id for p in pub_plans), "New plan listed in GET /plans"

    # Delete unused plan as Super Admin
    del_res = client.delete(f"/superadmin/plans/{plan_id}", headers=root_auth)
    assert_eq(del_res.status_code, 204, "Super Admin successfully deletes unused plan (204 No Content)")

    # Verify plan no longer appears in GET /superadmin/plans
    sa_plans_after = client.get("/superadmin/plans", headers=root_auth).json()
    assert not any(p["id"] == plan_id for p in sa_plans_after), "Deleted plan no longer appears in GET /superadmin/plans"

    # Verify plan no longer appears in GET /plans
    pub_plans_after = client.get("/plans", headers=admin_auth).json()
    assert not any(p["id"] == plan_id for p in pub_plans_after), "Deleted plan no longer appears in GET /plans"

    # --- 2. Error handling: Non-existent plan returns 404 ---
    print("\n--- 2. Non-existent Plan Deletion ---")
    random_id = str(uuid.uuid4())
    r = client.delete(f"/superadmin/plans/{random_id}", headers=root_auth)
    assert_eq(r.status_code, 404, "Deleting non-existent plan returns 404 Not Found")
    assert_eq(r.json().get("detail"), "Plan not found", "404 detail message is 'Plan not found'")

    # --- 3. Authorization guards ---
    print("\n--- 3. Authorization Guards ---")
    # Create another test plan to test auth
    p2_res = client.post("/superadmin/plans", json={
        "name": f"Auth Test Plan {uuid.uuid4().hex[:6]}",
        "price_monthly": 29.0,
        "price_yearly": 290.0,
        "features": ["Feature A"],
        "is_default": False,
    }, headers=root_auth)
    p2_id = p2_res.json()["id"]

    # Non-Super-Admin (normal org admin) returns 403
    r = client.delete(f"/superadmin/plans/{p2_id}", headers=admin_auth)
    assert_eq(r.status_code, 403, "Non-Super-Admin (firm admin) cannot delete plan (403 Forbidden)")

    # Unauthenticated returns 401 or 403
    r = client.delete(f"/superadmin/plans/{p2_id}")
    assert r.status_code in (401, 403), f"Unauthenticated delete returns 401/403 (got {r.status_code})"
    ok(f"Unauthenticated request rejected with HTTP {r.status_code}")

    # Clean up p2_id
    del_p2 = client.delete(f"/superadmin/plans/{p2_id}", headers=root_auth)
    assert_eq(del_p2.status_code, 204, "Clean up auth test plan succeeded")

    # --- 4. Default plan protection ---
    print("\n--- 4. Default Plan Protection ---")
    all_plans = client.get("/superadmin/plans", headers=root_auth).json()
    default_plan = next((p for p in all_plans if p.get("is_default")), None)
    assert default_plan is not None, "Default plan exists"

    del_default = client.delete(f"/superadmin/plans/{default_plan['id']}", headers=root_auth)
    assert_eq(del_default.status_code, 400, "Deleting default plan returns 400 Bad Request")

    # --- 5. Dependency protection: Plan in use by an Organization (plan_id) ---
    print("\n--- 5. Dependency Protection: In-Use Plan ---")
    in_use_plan_res = client.post("/superadmin/plans", json={
        "name": f"Assigned Plan {uuid.uuid4().hex[:6]}",
        "price_monthly": 99.0,
        "price_yearly": 990.0,
        "features": ["Heavy Support"],
        "is_default": False,
    }, headers=root_auth)
    in_use_plan_id = in_use_plan_res.json()["id"]

    # Register an org and upgrade it to this plan
    org_admin_email = f"cust_{uuid.uuid4().hex[:8]}@example.com"
    reg_res = client.post("/auth/register", json={
        "organization_name": f"Subscribed Org {uuid.uuid4().hex[:6]}",
        "admin_name": "Org Boss",
        "email": org_admin_email,
        "password": "Password123!",
        "role": "admin",
    })
    assert_eq(reg_res.status_code, 201, "Registered organization for dependency testing")
    org_admin_auth = {"Authorization": f"Bearer {reg_res.json()['tokens']['access_token']}"}

    # Request upgrade to in_use_plan_id
    upg_req = client.post("/organizations/upgrade-request", json={
        "requested_plan_id": in_use_plan_id,
        "billing_cycle": "monthly",
    }, headers=org_admin_auth)
    assert_eq(upg_req.status_code, 200, "Organization requested upgrade to plan")
    org_id = upg_req.json()["id"]

    # Plan is now referenced via requested_plan_id -> delete must fail with 409
    del_requested = client.delete(f"/superadmin/plans/{in_use_plan_id}", headers=root_auth)
    assert_eq(del_requested.status_code, 409, "Deleting plan with pending upgrade request returns 409 Conflict")

    # Super Admin approves the upgrade -> plan is now active on org.plan_id
    appr_res = client.patch(f"/superadmin/organizations/{org_id}/approve-upgrade", headers=root_auth)
    assert_eq(appr_res.status_code, 200, "Super Admin approved upgrade")
    assert_eq(appr_res.json()["plan_id"], in_use_plan_id, "Org is now subscribed to in_use_plan_id")

    # Attempt delete again -> must fail with 409 Conflict
    del_subscribed = client.delete(f"/superadmin/plans/{in_use_plan_id}", headers=root_auth)
    assert_eq(del_subscribed.status_code, 409, "Deleting plan currently subscribed by an org returns 409 Conflict")

    # Verify org subscription is intact and not corrupted
    org_check = client.get(f"/superadmin/organizations/{org_id}", headers=root_auth)
    assert_eq(org_check.status_code, 200, "Organization profile fetched successfully")
    assert_eq(org_check.json()["plan_id"], in_use_plan_id, "Org plan_id remains unchanged and intact")
    assert org_check.json()["plan"]["name"].startswith("Assigned Plan"), "Joined plan object intact"

    print("\n=======================================================")
    print(f"RESULTS: {_passed} passed, {_failed} failed")
    print("=======================================================\n")
    if _failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_tests()
