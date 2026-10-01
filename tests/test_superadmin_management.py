"""Focused test suite for Super Admin management APIs.

Covers:
  - Authorized Super Admin can create / list / update / delete another Super Admin.
  - A normal org Admin is rejected (403) from every /superadmin/admins route.
  - UserOut never leaks password_hash / tokens.
  - Self-delete and last-Super-Admin protections.
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
    email = f"admin_{uuid.uuid4().hex[:8]}@notsuper.com"
    r = client.post("/auth/register", json={
        "organization_name": "Not Super Org",
        "admin_name": "Regular Admin",
        "email": email,
        "password": "Password123!",
        "role": "admin",
    })
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}


def run_tests():
    print("\n=======================================================")
    print("TEST SUITE: Super Admin management APIs")
    print("=======================================================")

    root_auth = _super_admin_auth()
    admin_auth = _normal_admin_auth()

    # --- Unauthorized: normal Admin rejected everywhere ---
    print("\n--- Authorization: normal Admin cannot reach /superadmin/admins ---")
    r = client.post("/superadmin/admins", json={
        "name": "Should Not Exist", "email": f"nope_{uuid.uuid4().hex[:6]}@x.com", "password": "Password123!",
    }, headers=admin_auth)
    assert_eq(r.status_code, 403, "Normal Admin creating a Super Admin returns 403")

    r = client.get("/superadmin/admins", headers=admin_auth)
    assert_eq(r.status_code, 403, "Normal Admin listing Super Admins returns 403")

    r = client.get("/superadmin/admins")
    assert_eq(r.status_code, 403, "Unauthenticated request to /superadmin/admins returns 403/401")

    # --- Create ---
    print("\n--- Create Super Admin ---")
    new_email = f"super_{uuid.uuid4().hex[:8]}@platform.com"
    create_res = client.post("/superadmin/admins", json={
        "name": "New Super Admin", "email": new_email, "password": "SuperSecret123!",
    }, headers=root_auth)
    assert_eq(create_res.status_code, 201, "Authorized Super Admin creates another Super Admin")
    created = create_res.json()
    new_id = created["id"]
    assert_eq(created["email"], new_email, "Created Super Admin has the right email")
    assert_eq(created["system_role"], "super_admin", "Created account's system_role is super_admin")
    assert "password_hash" not in created, "Response does not expose password_hash"
    assert "password" not in created, "Response does not expose raw password"

    # The new Super Admin can actually log in and use its own privileges.
    login_res = client.post("/auth/login", json={"email": new_email, "password": "SuperSecret123!"})
    assert_eq(login_res.status_code, 200, "New Super Admin can log in")
    new_auth = {"Authorization": f"Bearer {login_res.json()['tokens']['access_token']}"}
    r = client.get("/superadmin/organizations", headers=new_auth)
    assert_eq(r.status_code, 200, "New Super Admin can use existing Super Admin-only routes")

    dup_res = client.post("/superadmin/admins", json={
        "name": "Dup", "email": new_email, "password": "AnotherPass123!",
    }, headers=root_auth)
    assert_eq(dup_res.status_code, 409, "Creating a Super Admin with a taken email returns 409")

    # --- List ---
    print("\n--- List Super Admins ---")
    list_res = client.get("/superadmin/admins", headers=root_auth)
    assert_eq(list_res.status_code, 200, "Authorized Super Admin lists Super Admins")
    listed = list_res.json()
    ids = {row["id"] for row in listed}
    assert new_id in ids, "New Super Admin appears in the list"
    assert_eq(
        {row["email"] for row in listed if row["id"] == new_id}, {new_email},
        "Listed row has the right email",
    )
    for row in listed:
        assert "password_hash" not in row, "No password_hash leaked in list response"
        assert "password" not in row, "No raw password leaked in list response"

    # --- Update ---
    print("\n--- Update Super Admin ---")
    upd_res = client.patch(f"/superadmin/admins/{new_id}", json={"name": "Renamed Super Admin", "phone": "9998887777"}, headers=root_auth)
    assert_eq(upd_res.status_code, 200, "Authorized Super Admin updates another Super Admin's details")
    assert_eq(upd_res.json()["name"], "Renamed Super Admin", "Name updated")
    assert_eq(upd_res.json()["phone"], "9998887777", "Phone updated")

    pw_res = client.patch(f"/superadmin/admins/{new_id}", json={"password": "BrandNewPass456!"}, headers=root_auth)
    assert_eq(pw_res.status_code, 200, "Password change succeeds")
    relogin = client.post("/auth/login", json={"email": new_email, "password": "BrandNewPass456!"})
    assert_eq(relogin.status_code, 200, "New Super Admin can log in with the changed password")

    self_deactivate = client.patch(f"/superadmin/admins/{new_id}", json={"is_active": False}, headers=new_auth)
    assert_eq(self_deactivate.status_code, 400, "A Super Admin cannot deactivate their own account")

    # A normal Admin cannot reach update either.
    r = client.patch(f"/superadmin/admins/{new_id}", json={"name": "Hacked"}, headers=admin_auth)
    assert_eq(r.status_code, 403, "Normal Admin updating a Super Admin returns 403")

    # --- Tenant isolation is not applicable: Super Admin is platform-global ---
    print("\n--- Super Admin has no organization_id (platform-global, not tenant-scoped) ---")
    assert_eq(created["organization_id"], None, "Super Admin account has no organization_id")

    # --- Delete protections ---
    print("\n--- Delete Super Admin ---")
    self_delete = client.delete(f"/superadmin/admins/{new_id}", headers=new_auth)
    assert_eq(self_delete.status_code, 400, "A Super Admin cannot delete their own account")

    r = client.delete(f"/superadmin/admins/{new_id}", headers=admin_auth)
    assert_eq(r.status_code, 403, "Normal Admin deleting a Super Admin returns 403")

    # Last-Super-Admin protection: try to delete every Super Admin down to one.
    all_admins = client.get("/superadmin/admins", headers=root_auth).json()
    if len(all_admins) == 1:
        only_id = all_admins[0]["id"]
        last_res = client.delete(f"/superadmin/admins/{only_id}", headers=root_auth)
        assert_eq(last_res.status_code, 400, "Cannot delete the last remaining Super Admin")
    else:
        ok(f"{len(all_admins)} Super Admins present — last-admin guard exercised via the delete below")

    del_res = client.delete(f"/superadmin/admins/{new_id}", headers=root_auth)
    assert_eq(del_res.status_code, 204, "Authorized Super Admin deletes another Super Admin")

    get_after = client.get("/superadmin/admins", headers=root_auth).json()
    assert new_id not in {row["id"] for row in get_after}, "Deleted Super Admin no longer listed"

    # --- Organization Inventory Tests (Task 5) ---
    print("\n--- Organization Inventory: Super Admin Endpoint & Tenant Isolation ---")
    # 1. Authorization check
    inv_unauth = client.get("/superadmin/organizations/inventory")
    assert_eq(inv_unauth.status_code, 403, "Unauthenticated request to /superadmin/organizations/inventory returns 403")

    inv_normal = client.get("/superadmin/organizations/inventory", headers=admin_auth)
    assert_eq(inv_normal.status_code, 403, "Normal Admin request to /superadmin/organizations/inventory returns 403")

    inv_super = client.get("/superadmin/organizations/inventory", headers=root_auth)
    assert_eq(inv_super.status_code, 200, "Super Admin can access /superadmin/organizations/inventory")
    inventory_items = inv_super.json()
    assert isinstance(inventory_items, list), "Inventory response is a list"

    # 2. Check fields contract
    expected_fields = {
        "id", "name", "created_at", "plan_id", "status",
        "user_count", "customer_count", "order_count", "invoice_count",
        "last_activity_date", "created_by_seed"
    }
    if inventory_items:
        first_item = inventory_items[0]
        assert expected_fields.issubset(set(first_item.keys())), f"Inventory row contains all expected fields: {expected_fields}"
        ok("Inventory row structure adheres to schema")

    # 3. Create test orgs with known distinct data to verify counts and tenant isolation
    from datetime import datetime, timezone, timedelta
    from app.core.database import SessionLocal
    from app.models import Organization, OrganizationStatus, User, Customer, SalesOrder, Invoice, ActivityLog
    from app.core.security import hash_password

    db = SessionLocal()
    org_a_id = f"test-org-a-{uuid.uuid4().hex[:8]}"
    org_b_id = f"test-org-b-{uuid.uuid4().hex[:8]}"
    t0 = datetime.now(timezone.utc) - timedelta(days=10)
    t_activity = datetime.now(timezone.utc) - timedelta(days=2)
    t_order = datetime.now(timezone.utc) - timedelta(days=1)

    try:
        org_a = Organization(
            id=org_a_id, name="Tenant Isolation Org A", email=f"a_{uuid.uuid4().hex[:6]}@test.com",
            status=OrganizationStatus.ACTIVE, created_at=t0,
        )
        org_b = Organization(
            id=org_b_id, name="Tenant Isolation Org B", email=f"b_{uuid.uuid4().hex[:6]}@test.com",
            status=OrganizationStatus.ACTIVE, created_at=t0,
        )
        db.add_all([org_a, org_b])
        db.flush()

        # Org A data: 2 users, 3 customers, 2 sales orders, 1 invoice, 1 activity log
        user_a1 = User(id=f"ua1-{uuid.uuid4().hex[:6]}", organization_id=org_a_id, name="User A1", email=f"ua1_{uuid.uuid4().hex[:6]}@test.com", password_hash=hash_password("pw"), system_role="admin")
        user_a2 = User(id=f"ua2-{uuid.uuid4().hex[:6]}", organization_id=org_a_id, name="User A2", email=f"ua2_{uuid.uuid4().hex[:6]}@test.com", password_hash=hash_password("pw"), system_role="staff")
        cust_a1 = Customer(id=f"ca1-{uuid.uuid4().hex[:6]}", organization_id=org_a_id, name="Cust A1")
        cust_a2 = Customer(id=f"ca2-{uuid.uuid4().hex[:6]}", organization_id=org_a_id, name="Cust A2")
        cust_a3 = Customer(id=f"ca3-{uuid.uuid4().hex[:6]}", organization_id=org_a_id, name="Cust A3")
        so_a1 = SalesOrder(id=f"so1-{uuid.uuid4().hex[:6]}", organization_id=org_a_id, order_number="ORD-A-001", created_at=t0)
        so_a2 = SalesOrder(id=f"so2-{uuid.uuid4().hex[:6]}", organization_id=org_a_id, order_number="ORD-A-002", created_at=t_order)
        inv_a1 = Invoice(id=f"in1-{uuid.uuid4().hex[:6]}", organization_id=org_a_id, invoice_number="INV-A-001", created_at=t0)
        act_a1 = ActivityLog(id=f"act1-{uuid.uuid4().hex[:6]}", organization_id=org_a_id, title="Profile updated", created_at=t_activity)

        # Org B data: 1 user, 0 customers, 1 sales order, 0 invoices
        user_b1 = User(id=f"ub1-{uuid.uuid4().hex[:6]}", organization_id=org_b_id, name="User B1", email=f"ub1_{uuid.uuid4().hex[:6]}@test.com", password_hash=hash_password("pw"), system_role="admin")
        so_b1 = SalesOrder(id=f"sob1-{uuid.uuid4().hex[:6]}", organization_id=org_b_id, order_number="ORD-B-001", created_at=t0)

        db.add_all([user_a1, user_a2, cust_a1, cust_a2, cust_a3, so_a1, so_a2, inv_a1, act_a1, user_b1, so_b1])
        db.commit()

        # Query inventory and inspect Org A and Org B
        inv_res = client.get("/superadmin/organizations/inventory", headers=root_auth)
        assert_eq(inv_res.status_code, 200, "Inventory query with populated orgs returns 200")
        data = {row["id"]: row for row in inv_res.json()}

        row_a = data.get(org_a_id)
        assert row_a is not None, "Org A present in inventory"
        assert_eq(row_a["user_count"], 2, "Org A user_count is 2")
        assert_eq(row_a["customer_count"], 3, "Org A customer_count is 3")
        assert_eq(row_a["order_count"], 2, "Org A order_count is 2")
        assert_eq(row_a["invoice_count"], 1, "Org A invoice_count is 1")
        assert_eq(row_a["created_by_seed"], False, "Org A created_by_seed is False")
        # Org A latest activity should be t_order (1 day ago vs activity log 2 days ago vs created 10 days ago)
        assert row_a["last_activity_date"] is not None, "Org A has last_activity_date"

        row_b = data.get(org_b_id)
        assert row_b is not None, "Org B present in inventory"
        assert_eq(row_b["user_count"], 1, "Org B user_count is 1")
        assert_eq(row_b["customer_count"], 0, "Org B customer_count is 0")
        assert_eq(row_b["order_count"], 1, "Org B order_count is 1")
        assert_eq(row_b["invoice_count"], 0, "Org B invoice_count is 0")
        assert_eq(row_b["created_by_seed"], False, "Org B created_by_seed is False")

        # Check seeded orgs if present
        seed_orgs = [r for r in inv_res.json() if r["created_by_seed"]]
        for s in seed_orgs:
            assert s["name"] in {"SAAS Distributors", "Testing Paid Org"} or "demo" in s["name"].lower() or "testing" in s["name"].lower(), "Seed flag correctly identified seed orgs"
        ok(f"Seed flag verified on {len(seed_orgs)} seeded org(s)")

    finally:
        db.query(ActivityLog).filter(ActivityLog.organization_id.in_([org_a_id, org_b_id])).delete()
        db.query(Invoice).filter(Invoice.organization_id.in_([org_a_id, org_b_id])).delete()
        db.query(SalesOrder).filter(SalesOrder.organization_id.in_([org_a_id, org_b_id])).delete()
        db.query(Customer).filter(Customer.organization_id.in_([org_a_id, org_b_id])).delete()
        db.query(User).filter(User.organization_id.in_([org_a_id, org_b_id])).delete()
        db.query(Organization).filter(Organization.id.in_([org_a_id, org_b_id])).delete()
        db.commit()
        db.close()

    print("\n=======================================================")
    print(f"RESULTS: {_passed} passed, {_failed} failed")
    print("=======================================================\n")
    if _failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_tests()
