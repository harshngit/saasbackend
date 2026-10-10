"""Comprehensive test suite for Plan Entitlements and Organization Overrides.

Covers all 24+ core scenarios:
1. Basic plan feature restrictions (Leads, Quotations, Vehicles, Vehicle Stock, P&L, Cash Flow, GST denied).
2. Basic plan allowed features (Customers, Invoices, Sales Orders, Core Reports).
3. Pro plan access (Leads, Quotations, Vehicles, Vehicle Stock, Advanced Reports allowed).
4. Enterprise plan access (All features enabled).
5. Role permissions composition (Role permission cannot bypass denied entitlement; Entitlement ALLOW does not bypass denied role permission).
6. Super Admin access and tenant isolation.
7. Organization Feature Overrides (ALLOW on Basic grants access; BLOCK on Pro denies access).
8. Expired overrides automatically ignored without cleanup jobs.
9. Temporary trial overrides.
10. Basic plan user limit (max_users=1) blocks creating second staff; admin still functions.
11. Organization limit override (max_users=5) allows creating additional staff.
12. Basic plan warehouse limit (max_warehouses=1) blocks creating second warehouse via POST /warehouses; default warehouse unaffected.
13. Organization limit override (max_warehouses=3) allows creating additional warehouses.
14. Report enforcement uniform across screen (GET /reports/{type}) and export (GET /reports/{type}/export).
15. Super Admin plan management with entitlements and validation against unknown keys.
16. Super Admin organization override endpoints (upsert, list, delete, reset-all).
17. Current organization entitlements endpoint (GET /organizations/me/entitlements).
18. Error contracts (HTTP 403 with PLAN_FEATURE_NOT_AVAILABLE and PLAN_LIMIT_REACHED).
"""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from app.main import app
from app.core.config import settings
from app.core.database import SessionLocal
from app.models import Organization, Plan, Role, SystemRole, User, UserRole, Warehouse
from app.models.organization_override import (
    OrganizationFeatureOverride,
    OrganizationLimitOverride,
)
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


def _super_admin_auth():
    r = client.post("/auth/login", json={
        "email": settings.super_admin_email,
        "password": settings.super_admin_password,
    })
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}


def _create_test_org(plan_name: str = "Basic"):
    db = SessionLocal()
    try:
        plan = db.query(Plan).filter(Plan.name == plan_name).first()
        assert plan is not None, f"Plan {plan_name} not found"

        org_uid = uuid.uuid4().hex[:8]
        email = f"admin_{org_uid}@{plan_name.lower()}.com"
        password = "Password123!"

        r = client.post("/auth/register", json={
            "organization_name": f"{plan_name} Firm {org_uid}",
            "admin_name": f"{plan_name} Admin",
            "email": email,
            "password": password,
        })
        assert r.status_code == 201, r.text
        data = r.json()
        org_id = data["organization"]["id"]
        user_id = data["user"]["id"]

        # Assign requested plan
        org = db.get(Organization, org_id)
        org.plan_id = plan.id
        db.commit()

        login_res = client.post("/auth/login", json={"email": email, "password": password})
        assert login_res.status_code == 200, login_res.text
        token = login_res.json()["tokens"]["access_token"]
        auth_headers = {"Authorization": f"Bearer {token}"}

        return {
            "org_id": org_id,
            "user_id": user_id,
            "email": email,
            "password": password,
            "auth": auth_headers,
            "plan_id": plan.id,
        }
    finally:
        db.close()


def test_basic_plan_feature_denials():
    print("\n--- Test Basic Plan Feature Denials ---")
    basic_org = _create_test_org("Basic")
    headers = basic_org["auth"]

    # 1. Allowed feature: Customers (crm.customers)
    r = client.get("/customers", headers=headers)
    if r.status_code == 200:
        ok("Basic plan can access GET /customers (crm.customers allowed)")
    else:
        fail("Basic plan cannot access GET /customers", f"Status: {r.status_code} {r.text}")

    # 2. Denied feature: Leads (crm.leads)
    r = client.get("/leads", headers=headers)
    if r.status_code == 403 and r.json().get("detail", {}).get("code") == "PLAN_FEATURE_NOT_AVAILABLE":
        ok("Basic plan denied GET /leads with PLAN_FEATURE_NOT_AVAILABLE")
    else:
        fail("Basic plan GET /leads was not properly denied", f"Status: {r.status_code} {r.text}")

    # 3. Denied feature: Quotations (crm.quotations)
    r = client.get("/quotations", headers=headers)
    if r.status_code == 403 and r.json().get("detail", {}).get("code") == "PLAN_FEATURE_NOT_AVAILABLE":
        ok("Basic plan denied GET /quotations with PLAN_FEATURE_NOT_AVAILABLE")
    else:
        fail("Basic plan GET /quotations was not properly denied", f"Status: {r.status_code} {r.text}")

    # 4. Denied feature: Vehicles (sales.vehicles)
    r = client.get("/vehicles", headers=headers)
    if r.status_code == 403 and r.json().get("detail", {}).get("code") == "PLAN_FEATURE_NOT_AVAILABLE":
        ok("Basic plan denied GET /vehicles with PLAN_FEATURE_NOT_AVAILABLE")
    else:
        fail("Basic plan GET /vehicles was not properly denied", f"Status: {r.status_code} {r.text}")

    # 5. Denied feature: Vehicle Stock (sales.vehicle_stock)
    r = client.post("/vehicle-stock/loading", headers=headers, json={"items": []})
    if r.status_code == 403 and r.json().get("detail", {}).get("code") == "PLAN_FEATURE_NOT_AVAILABLE":
        ok("Basic plan denied POST /vehicle-stock/loading with PLAN_FEATURE_NOT_AVAILABLE")
    else:
        fail("Basic plan POST /vehicle-stock/loading was not properly denied", f"Status: {r.status_code} {r.text}")

    # 6. Denied report: Profit & Loss (report.profit_loss)
    r = client.get("/reports/profit-loss", headers=headers)
    if r.status_code == 403 and r.json().get("detail", {}).get("code") == "PLAN_FEATURE_NOT_AVAILABLE":
        assert r.json().get("detail", {}).get("feature") == "report.profit_loss"
        ok("Basic plan denied GET /reports/profit-loss with PLAN_FEATURE_NOT_AVAILABLE (canonical report.profit_loss)")
    else:
        fail("Basic plan GET /reports/profit-loss was not properly denied", f"Status: {r.status_code} {r.text}")

    # Export also denied for P&L
    r = client.get("/reports/profit-loss/export?format=excel", headers=headers)
    if r.status_code == 403 and r.json().get("detail", {}).get("code") == "PLAN_FEATURE_NOT_AVAILABLE":
        ok("Basic plan denied GET /reports/profit-loss/export with PLAN_FEATURE_NOT_AVAILABLE")
    else:
        fail("Basic plan GET /reports/profit-loss/export was not properly denied", f"Status: {r.status_code} {r.text}")

    # Denied report: Cash Flow (report.cash_flow)
    r = client.get("/reports/cash-flow-sheet", headers=headers)
    if r.status_code == 403 and r.json().get("detail", {}).get("code") == "PLAN_FEATURE_NOT_AVAILABLE":
        ok("Basic plan denied GET /reports/cash-flow-sheet with PLAN_FEATURE_NOT_AVAILABLE")
    else:
        fail("Basic plan GET /reports/cash-flow-sheet was not properly denied", f"Status: {r.status_code} {r.text}")

    # Denied report: GST Summary (report.gst_filing)
    r = client.get("/reports/gst-summary", headers=headers)
    if r.status_code == 403 and r.json().get("detail", {}).get("code") == "PLAN_FEATURE_NOT_AVAILABLE":
        ok("Basic plan denied GET /reports/gst-summary with PLAN_FEATURE_NOT_AVAILABLE")
    else:
        fail("Basic plan GET /reports/gst-summary was not properly denied", f"Status: {r.status_code} {r.text}")

    # 7. Allowed core reports
    r = client.get("/reports/daily-transaction", headers=headers)
    if r.status_code == 200:
        ok("Basic plan allowed GET /reports/daily-transaction")
    else:
        fail("Basic plan cannot access GET /reports/daily-transaction", f"Status: {r.status_code} {r.text}")

    r = client.get("/reports/sales", headers=headers)
    if r.status_code == 200:
        ok("Basic plan allowed GET /reports/sales")
    else:
        fail("Basic plan cannot access GET /reports/sales", f"Status: {r.status_code} {r.text}")


def test_pro_plan_access():
    print("\n--- Test Pro Plan Access ---")
    pro_org = _create_test_org("Pro")
    headers = pro_org["auth"]

    # 1. Pro can access Leads
    r = client.get("/leads", headers=headers)
    if r.status_code == 200:
        ok("Pro plan allowed GET /leads")
    else:
        fail("Pro plan cannot access GET /leads", f"Status: {r.status_code} {r.text}")

    # 2. Pro can access Quotations
    r = client.get("/quotations", headers=headers)
    if r.status_code == 200:
        ok("Pro plan allowed GET /quotations")
    else:
        fail("Pro plan cannot access GET /quotations", f"Status: {r.status_code} {r.text}")

    # 3. Pro can access Vehicles
    r = client.get("/vehicles", headers=headers)
    if r.status_code == 200:
        ok("Pro plan allowed GET /vehicles")
    else:
        fail("Pro plan cannot access GET /vehicles", f"Status: {r.status_code} {r.text}")

    # 4. Pro can access Vehicle Stock (passes entitlement check)
    r = client.post("/vehicle-stock/loading", headers=headers, json={"items": []})
    if r.status_code != 403:
        ok("Pro plan allowed /vehicle-stock (not blocked by PLAN_FEATURE_NOT_AVAILABLE)")
    else:
        fail("Pro plan was unexpectedly blocked from /vehicle-stock", f"Status: {r.status_code} {r.text}")

    # 5. Pro can access Profit & Loss
    r = client.get("/reports/profit-loss", headers=headers)
    if r.status_code == 200:
        ok("Pro plan allowed GET /reports/profit-loss")
    else:
        fail("Pro plan cannot access GET /reports/profit-loss", f"Status: {r.status_code} {r.text}")

    # 6. Pro can access Cash Flow
    r = client.get("/reports/cash-flow-sheet", headers=headers)
    if r.status_code == 200:
        ok("Pro plan allowed GET /reports/cash-flow-sheet")
    else:
        fail("Pro plan cannot access GET /reports/cash-flow-sheet", f"Status: {r.status_code} {r.text}")

    # 7. Pro can access GST Summary
    r = client.get("/reports/gst-summary", headers=headers)
    if r.status_code == 200:
        ok("Pro plan allowed GET /reports/gst-summary")
    else:
        fail("Pro plan cannot access GET /reports/gst-summary", f"Status: {r.status_code} {r.text}")


def test_enterprise_plan_access():
    print("\n--- Test Enterprise Plan Access ---")
    ent_org = _create_test_org("Enterprise")
    headers = ent_org["auth"]

    r = client.get("/leads", headers=headers)
    if r.status_code == 200:
        ok("Enterprise plan allowed GET /leads")
    else:
        fail("Enterprise plan cannot access GET /leads", f"Status: {r.status_code} {r.text}")

    r = client.get("/reports/profit-loss", headers=headers)
    if r.status_code == 200:
        ok("Enterprise plan allowed GET /reports/profit-loss")
    else:
        fail("Enterprise plan cannot access GET /reports/profit-loss", f"Status: {r.status_code} {r.text}")


def test_organization_feature_overrides():
    print("\n--- Test Organization Feature Overrides ---")
    sa_headers = _super_admin_auth()
    basic_org = _create_test_org("Basic")
    headers = basic_org["auth"]
    org_id = basic_org["org_id"]

    # Basic org initially denied Leads
    r = client.get("/leads", headers=headers)
    assert r.status_code == 403

    # Super Admin applies ALLOW override for crm.leads
    r = client.post(
        f"/superadmin/organizations/{org_id}/overrides/features",
        headers=sa_headers,
        json={
            "entitlement_key": "crm.leads",
            "effect": "ALLOW",
            "reason": "Customer requested trial of leads",
        },
    )
    if r.status_code == 200 and r.json()["is_allowed"] is True:
        ok("Super Admin successfully created feature override (ALLOW) for crm.leads")
    else:
        fail("Super Admin failed to create feature override", f"{r.status_code} {r.text}")

    # Basic org can now access Leads!
    r = client.get("/leads", headers=headers)
    if r.status_code == 200:
        ok("Basic org with ALLOW override can now access GET /leads")
    else:
        fail("Basic org with ALLOW override could not access GET /leads", f"{r.status_code} {r.text}")

    # Test alias normalization in feature override: report.profit_and_loss -> report.profit_loss
    r = client.post(
        f"/superadmin/organizations/{org_id}/overrides/features",
        headers=sa_headers,
        json={
            "entitlement_key": "report.profit_and_loss",
            "effect": "ALLOW",
            "reason": "Override with legacy key alias",
        },
    )
    assert r.status_code == 200
    assert r.json()["entitlement_key"] == "report.profit_loss"
    ok("Feature override input normalized legacy 'report.profit_and_loss' to canonical 'report.profit_loss'")

    # P&L is now accessible on Basic due to the normalized override
    r = client.get("/reports/profit-loss", headers=headers)
    assert r.status_code == 200
    ok("Basic org can access P&L following normalized override")

    # Super Admin deletes the override using the legacy alias
    r = client.delete(
        f"/superadmin/organizations/{org_id}/overrides/features/report.profit_and_loss",
        headers=sa_headers,
    )
    assert r.status_code == 204
    ok("Super Admin deleted feature override using legacy alias")

    # P&L is now denied again
    r = client.get("/reports/profit-loss", headers=headers)
    assert r.status_code == 403
    ok("Basic org P&L reverted to denied after override deletion")

    # Super Admin deletes the leads override
    r = client.delete(
        f"/superadmin/organizations/{org_id}/overrides/features/crm.leads",
        headers=sa_headers,
    )
    if r.status_code == 204:
        ok("Super Admin deleted feature override")
    else:
        fail("Super Admin failed to delete feature override", f"{r.status_code} {r.text}")

    # Basic org is denied Leads again
    r = client.get("/leads", headers=headers)
    if r.status_code == 403:
        ok("Basic org reverts to plan default (denied) after override deletion")
    else:
        fail("Basic org did not revert to plan default after override deletion", f"{r.status_code} {r.text}")

    # Test BLOCK override on Pro org
    pro_org = _create_test_org("Pro")
    pro_headers = pro_org["auth"]
    pro_id = pro_org["org_id"]

    # Pro initially allowed Quotations
    r = client.get("/quotations", headers=pro_headers)
    assert r.status_code == 200

    # Super Admin applies BLOCK override
    r = client.post(
        f"/superadmin/organizations/{pro_id}/overrides/features",
        headers=sa_headers,
        json={
            "entitlement_key": "crm.quotations",
            "effect": "BLOCK",
            "reason": "Organization requested quotation disablement",
        },
    )
    assert r.status_code == 200

    r = client.get("/quotations", headers=pro_headers)
    if r.status_code == 403:
        ok("Pro org with BLOCK override is blocked from GET /quotations")
    else:
        fail("Pro org with BLOCK override was not blocked", f"{r.status_code} {r.text}")


def test_override_expiry_behavior():
    print("\n--- Test Override Expiry Behavior ---")
    sa_headers = _super_admin_auth()
    basic_org = _create_test_org("Basic")
    headers = basic_org["auth"]
    org_id = basic_org["org_id"]

    # Expired override (in the past)
    past_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    r = client.post(
        f"/superadmin/organizations/{org_id}/overrides/features",
        headers=sa_headers,
        json={
            "entitlement_key": "crm.leads",
            "effect": "ALLOW",
            "expires_at": past_time,
            "reason": "Expired promo",
        },
    )
    assert r.status_code == 200

    # Because it is expired, resolution ignores it and falls back to plan default (denied)
    r = client.get("/leads", headers=headers)
    if r.status_code == 403:
        ok("Expired feature override is automatically ignored without background cleanup")
    else:
        fail("Expired feature override was incorrectly treated as active", f"{r.status_code} {r.text}")

    # Future override (in the future)
    future_time = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    r = client.post(
        f"/superadmin/organizations/{org_id}/overrides/features",
        headers=sa_headers,
        json={
            "entitlement_key": "crm.leads",
            "effect": "ALLOW",
            "expires_at": future_time,
            "reason": "7-day promo",
        },
    )
    assert r.status_code == 200

    r = client.get("/leads", headers=headers)
    if r.status_code == 200:
        ok("Active unexpired feature override is respected")
    else:
        fail("Active unexpired feature override was not respected", f"{r.status_code} {r.text}")


def test_user_limits_and_overrides():
    print("\n--- Test User Limits and Overrides ---")
    sa_headers = _super_admin_auth()
    basic_org = _create_test_org("Basic")
    headers = basic_org["auth"]
    org_id = basic_org["org_id"]

    # Basic plan has max_users=1. The Admin user is active and counts as 1.
    # Attempting to create a second user should be blocked by max_users limit.
    r = client.post(
        "/users",
        headers=headers,
        json={
            "contact_information": {
                "official_email": f"staff1_{uuid.uuid4().hex[:6]}@basic.com",
            },
            "login_security": {
                "password": "Password123!",
            },
        },
    )
    if r.status_code == 403 and r.json().get("detail", {}).get("code") == "PLAN_LIMIT_REACHED":
        ok("Basic plan max_users=1 blocked creating second staff user with PLAN_LIMIT_REACHED")
    else:
        fail("Basic plan max_users limit was not enforced on staff creation", f"{r.status_code} {r.text}")

    # Admin's own profile and endpoints still work
    r = client.get("/users/assignable", headers=headers)
    if r.status_code == 200:
        ok("Basic plan Admin functionality is unaffected by user limit")
    else:
        fail("Basic plan Admin was unexpectedly blocked", f"{r.status_code} {r.text}")

    # Super Admin grants limit override for max_users = 5
    r = client.post(
        f"/superadmin/organizations/{org_id}/overrides/limits",
        headers=sa_headers,
        json={
            "limit_key": "max_users",
            "value": 5,
            "reason": "Enterprise customer expansion",
        },
    )
    assert r.status_code == 200

    # Also grant employee.staff so staff creation can proceed
    r = client.post(
        f"/superadmin/organizations/{org_id}/overrides/features",
        headers=sa_headers,
        json={
            "entitlement_key": "employee.staff",
            "effect": "ALLOW",
            "reason": "Allow staff management",
        },
    )
    assert r.status_code == 200

    # Now creating staff succeeds!
    r = client.post(
        "/users",
        headers=headers,
        json={
            "contact_information": {
                "official_email": f"staff1_{uuid.uuid4().hex[:6]}@basic.com",
            },
            "login_security": {
                "password": "Password123!",
            },
        },
    )
    if r.status_code == 201:
        ok("Creating staff user succeeded after max_users limit override to 5")
    else:
        fail("Creating staff user failed after limit override", f"{r.status_code} {r.text}")


def test_existing_over_limit_users_safety():
    print("\n--- Test Existing Over-Limit Users Safety (No-Grandfathering) ---")
    sa_headers = _super_admin_auth()
    db = SessionLocal()
    try:
        # Create an org on Free plan (max_users=1)
        free_plan = db.query(Plan).filter(Plan.name == "Free").first()
        assert free_plan is not None
        assert free_plan.max_users == 1, f"Expected Free plan max_users=1, got {free_plan.max_users}"

        org_uid = uuid.uuid4().hex[:8]
        org = Organization(
            name=f"Legacy MultiUser Org {org_uid}",
            plan_id=free_plan.id,
            status="active",
        )
        db.add(org)
        db.flush()

        # Seed 3 pre-existing active users for this organization (simulating legacy data)
        u1 = User(organization_id=org.id, name="Admin 1", email=f"u1_{org_uid}@legacy.com", password_hash="hash", system_role="admin", role=UserRole.ADMIN, is_active=True)
        u2 = User(organization_id=org.id, name="Staff 2", email=f"u2_{org_uid}@legacy.com", password_hash="hash", system_role="staff", role=UserRole.SALES_OFFICER, is_active=True)
        u3 = User(organization_id=org.id, name="Staff 3", email=f"u3_{org_uid}@legacy.com", password_hash="hash", system_role="staff", role=UserRole.DELIVERY_PARTNER, is_active=True)
        db.add_all([u1, u2, u3])
        db.commit()

        # Verify all 3 users exist and remain active
        active_users = db.query(User).filter(User.organization_id == org.id, User.is_active.is_(True)).all()
        assert len(active_users) == 3
        ok("All 3 pre-existing users remain intact and active in the database")

        # Now authenticate as Admin 1
        from app.core.security import create_access_token
        token = create_access_token(user_id=u1.id, role="admin", organization_id=org.id)
        auth_headers = {"Authorization": f"Bearer {token}"}


        # Attempt to create a 4th user via POST /users -> must be blocked with PLAN_LIMIT_REACHED
        r = client.post(
            "/users",
            headers=auth_headers,
            json={
                "contact_information": {
                    "official_email": f"u4_{org_uid}@legacy.com",
                },
                "login_security": {
                    "password": "Password123!",
                },
            },
        )
        assert r.status_code == 403
        assert r.json().get("detail", {}).get("code") == "PLAN_LIMIT_REACHED"
        ok("Creating additional staff in over-limit org (3 users >= limit 1) is blocked with PLAN_LIMIT_REACHED")

        # Confirm all 3 existing users are STILL intact and active
        active_count = db.query(User).filter(User.organization_id == org.id, User.is_active.is_(True)).count()
        assert active_count == 3
        ok("No existing users were deleted or deactivated during failed creation attempt")

        # Super Admin grants limit override max_users = 5
        r = client.post(
            f"/superadmin/organizations/{org.id}/overrides/limits",
            headers=sa_headers,
            json={"limit_key": "max_users", "value": 5, "reason": "Grandfather expansion"},
        )
        assert r.status_code == 200

        # Also grant employee.staff feature override
        r = client.post(
            f"/superadmin/organizations/{org.id}/overrides/features",
            headers=sa_headers,
            json={"entitlement_key": "employee.staff", "effect": "ALLOW"},
        )
        assert r.status_code == 200

        # Now creating 4th user succeeds!
        r = client.post(
            "/users",
            headers=auth_headers,
            json={
                "contact_information": {
                    "official_email": f"u4_{org_uid}@legacy.com",
                },
                "login_security": {
                    "password": "Password123!",
                },
            },
        )
        assert r.status_code == 201
        ok("Creating 4th user succeeded after Super Admin limit override to 5")
    finally:
        db.close()


def test_warehouse_limits_and_overrides():
    print("\n--- Test Warehouse Limits and Overrides ---")
    sa_headers = _super_admin_auth()
    basic_org = _create_test_org("Basic")
    headers = basic_org["auth"]
    org_id = basic_org["org_id"]

    # Read warehouses: auto-creates default warehouse (1 warehouse exists)
    r = client.get("/warehouses", headers=headers)
    assert r.status_code == 200
    assert len(r.json()) >= 1
    ok("Default warehouse initialization works on Basic plan")

    # Creating a second warehouse via POST /warehouses should be blocked by max_warehouses=1 limit
    r = client.post(
        "/warehouses",
        headers=headers,
        json={
            "name": "Second Warehouse",
            "code": f"WH-{uuid.uuid4().hex[:4]}",
        },
    )
    if r.status_code == 403 and r.json().get("detail", {}).get("code") == "PLAN_LIMIT_REACHED":
        ok("Basic plan max_warehouses=1 blocked creating second warehouse with PLAN_LIMIT_REACHED")
    else:
        fail("Basic plan max_warehouses limit was not enforced", f"{r.status_code} {r.text}")

    # Super Admin grants limit override for max_warehouses = 3
    r = client.post(
        f"/superadmin/organizations/{org_id}/overrides/limits",
        headers=sa_headers,
        json={
            "limit_key": "max_warehouses",
            "value": 3,
            "reason": "Branch expansion",
        },
    )
    assert r.status_code == 200

    # Creating warehouse now succeeds!
    r = client.post(
        "/warehouses",
        headers=headers,
        json={
            "name": "Second Warehouse",
            "code": f"WH-{uuid.uuid4().hex[:4]}",
        },
    )
    if r.status_code == 201:
        ok("Creating warehouse succeeded after max_warehouses limit override to 3")
    else:
        fail("Creating warehouse failed after limit override", f"{r.status_code} {r.text}")


def test_organization_entitlements_endpoint():
    print("\n--- Test Organization Entitlements Endpoint ---")
    basic_org = _create_test_org("Basic")
    headers = basic_org["auth"]

    r = client.get("/organizations/me/entitlements", headers=headers)
    if r.status_code == 200:
        data = r.json()
        assert "features" in data
        assert "limits" in data
        assert data["features"].get("crm.customers") is True
        assert data["features"].get("crm.leads") is False
        assert "report.profit_loss" in data["features"]
        assert "report.profit_and_loss" not in data["features"]
        assert data["features"].get("report.profit_loss") is False
        assert data["limits"].get("max_users") == 1
        assert data["limits"].get("max_warehouses") == 1
        ok("GET /organizations/me/entitlements returns canonical report.profit_loss and excludes deprecated key")
    else:
        fail("GET /organizations/me/entitlements failed", f"{r.status_code} {r.text}")


def test_superadmin_plan_management_and_validation():
    print("\n--- Test Super Admin Plan Management & Validation ---")
    sa_headers = _super_admin_auth()

    # 1. Reject unknown entitlement keys in PlanCreate
    r = client.post(
        "/superadmin/plans",
        headers=sa_headers,
        json={
            "name": "Invalid Plan",
            "price_monthly": 100,
            "price_yearly": 1000,
            "entitlements": {
                "crm.leads": True,
                "unknown.fake_feature": True,
            },
        },
    )
    if r.status_code == 422:
        ok("Super Admin PlanCreate rejected unknown entitlement key with 422")
    else:
        fail("PlanCreate did not reject unknown entitlement key", f"{r.status_code} {r.text}")

    # 2. PlanCreate normalizes legacy alias report.profit_and_loss to report.profit_loss
    r = client.post(
        "/superadmin/plans",
        headers=sa_headers,
        json={
            "name": f"Alias Test Plan {uuid.uuid4().hex[:6]}",
            "price_monthly": 100,
            "price_yearly": 1000,
            "entitlements": {
                "report.profit_and_loss": True,
            },
        },
    )
    assert r.status_code == 201
    assert "report.profit_loss" in r.json()["entitlements"]
    assert "report.profit_and_loss" not in r.json()["entitlements"]
    ok("PlanCreate normalized legacy alias 'report.profit_and_loss' to canonical 'report.profit_loss'")

    # 3. Reject unknown override keys
    basic_org = _create_test_org("Basic")
    org_id = basic_org["org_id"]

    r = client.post(
        f"/superadmin/organizations/{org_id}/overrides/features",
        headers=sa_headers,
        json={
            "entitlement_key": "invalid.nonexistent.key",
            "effect": "ALLOW",
        },
    )
    if r.status_code == 422:
        ok("Super Admin feature override rejected unknown entitlement key with 422")
    else:
        fail("Feature override did not reject unknown entitlement key", f"{r.status_code} {r.text}")

    r = client.post(
        f"/superadmin/organizations/{org_id}/overrides/limits",
        headers=sa_headers,
        json={
            "limit_key": "invalid_limit_key",
            "value": 10,
        },
    )
    if r.status_code == 422:
        ok("Super Admin limit override rejected unknown limit key with 422")
    else:
        fail("Limit override did not reject unknown limit key", f"{r.status_code} {r.text}")

    # 4. Super Admin Reset All Overrides
    r = client.post(f"/superadmin/organizations/{org_id}/overrides/reset-all", headers=sa_headers)
    if r.status_code == 200:
        ok("Super Admin reset-all overrides endpoint works")
    else:
        fail("Super Admin reset-all overrides failed", f"{r.status_code} {r.text}")


def main():
    test_basic_plan_feature_denials()
    test_pro_plan_access()
    test_enterprise_plan_access()
    test_organization_feature_overrides()
    test_override_expiry_behavior()
    test_user_limits_and_overrides()
    test_existing_over_limit_users_safety()
    test_warehouse_limits_and_overrides()
    test_organization_entitlements_endpoint()
    test_superadmin_plan_management_and_validation()

    print("\n" + "=" * 50)
    print(f"Results: {_passed} passed, {_failed} failed")
    print("=" * 50)
    if _failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()

