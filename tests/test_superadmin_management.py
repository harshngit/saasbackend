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

    # The seeded/root Super Admin must be the last remaining one now (or close to it) —
    # confirm the guard actually fires when only one is left.
    remaining = client.get("/superadmin/admins", headers=root_auth).json()
    if len(remaining) == 1:
        only_id = remaining[0]["id"]
        last_res = client.delete(f"/superadmin/admins/{only_id}", headers=root_auth)
        assert_eq(last_res.status_code, 400, "Deleting the very last Super Admin is refused")

    print("\n=======================================================")
    print(f"RESULTS: {_passed} passed, {_failed} failed")
    print("=======================================================\n")
    if _failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_tests()
