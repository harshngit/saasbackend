r"""End-to-end smoke test of the auth + user-management flows.

Run with:  .\.venv\Scripts\python.exe test_smoke.py
Uses a throwaway SQLite DB so it never touches your dev data.
"""

import os
import uuid

os.environ["DATABASE_URL"] = "sqlite:///./_smoke_test.db"
os.environ["EXPOSE_RESET_TOKEN"] = "true"  # dev-only: get reset token in the response

# Fresh DB file each run.
if os.path.exists("_smoke_test.db"):
    os.remove("_smoke_test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.seed import main as seed_main  # noqa: E402

seed_main()
client = TestClient(app)

passed = 0
failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


print("\n== health ==")
r = client.get("/health")
check("health returns ok", r.status_code == 200 and r.json()["status"] == "ok", r.text)

print("\n== admin self-registration (new firm) ==")
firm_email = f"owner_{uuid.uuid4().hex[:8]}@firm.com"
r = client.post("/auth/register", json={
    "organization_name": "BlueWave Water Co.",
    "business_type": "Distributor",
    "gst_number": "29AABCU9603R1ZX",
    "pan_number": "AABCU9603R",
    "address": "12 MG Road, Bengaluru",
    "phone": "9876543210",
    "email": firm_email,
    "financial_year": "2025-2026",
    "admin_name": "Vikram Patel",
    "password": "Secret@123",
})
check("register returns 201", r.status_code == 201, r.text)
body = r.json()
check("register returns admin role (default)", body["user"]["role"] == "admin", body)
check("register creates organization", body["organization"]["name"] == "BlueWave Water Co.", body)
org_out = body["organization"]
check("org persists business_type", org_out["business_type"] == "Distributor", org_out)
check("org persists pan_number", org_out["pan_number"] == "AABCU9603R", org_out)
check("org persists address", org_out["address"] == "12 MG Road, Bengaluru", org_out)
check("org persists financial_year", org_out["financial_year"] == "2025-2026", org_out)
check("register returns token pair", "access_token" in body["tokens"] and "refresh_token" in body["tokens"], body)
admin_access = body["tokens"]["access_token"]
admin_refresh = body["tokens"]["refresh_token"]

print("\n== registration role rules ==")
# Explicit role=admin is fine.
r = client.post("/auth/register", json={
    "organization_name": "RoleOk Co", "admin_name": "A", "email": f"roleok_{uuid.uuid4().hex[:8]}@f.com",
    "password": "Secret@123", "role": "admin"})
check("explicit role=admin -> 201", r.status_code == 201, r.text)
# Any non-admin role must be rejected (staff are created by the Admin, not self-registered).
r = client.post("/auth/register", json={
    "organization_name": "RoleBad Co", "admin_name": "A", "email": f"rolebad_{uuid.uuid4().hex[:8]}@f.com",
    "password": "Secret@123", "role": "sales_officer"})
check("role=sales_officer at register -> 422", r.status_code == 422, r.text)

print("\n== duplicate registration rejected ==")
r = client.post("/auth/register", json={
    "organization_name": "Dup", "admin_name": "Dup", "email": firm_email, "password": "Secret@123",
})
check("duplicate email -> 409", r.status_code == 409, r.text)

print("\n== login (seeded demo admin) ==")
r = client.post("/auth/login", json={"email": "admin@demo.com", "password": "Admin@123"})
check("login returns 200", r.status_code == 200, r.text)
check("login wrong password -> 401",
      client.post("/auth/login", json={"email": "admin@demo.com", "password": "nope"}).status_code == 401)

print("\n== /auth/me with access token ==")
r = client.get("/auth/me", headers={"Authorization": f"Bearer {admin_access}"})
check("me returns the admin", r.status_code == 200 and r.json()["email"] == firm_email, r.text)
check("me without token -> 403", client.get("/auth/me").status_code == 403)
check("me with junk token -> 401",
      client.get("/auth/me", headers={"Authorization": "Bearer garbage"}).status_code == 401)

print("\n== admin creates staff ==")
staff_email = f"sales_{uuid.uuid4().hex[:8]}@firm.com"
r = client.post("/users",
    headers={"Authorization": f"Bearer {admin_access}"},
    json={"name": "Ramesh", "email": staff_email, "password": "Staff@123", "role": "sales_officer"})
check("create sales_officer -> 201", r.status_code == 201, r.text)
staff_id = r.json().get("id")
check("staff belongs to admin's org", r.json().get("organization_id") == body["organization"]["id"], r.text)

print("\n== admin cannot create another admin/super_admin ==")
r = client.post("/users",
    headers={"Authorization": f"Bearer {admin_access}"},
    json={"name": "X", "email": f"x_{uuid.uuid4().hex[:6]}@f.com", "password": "Staff@123", "role": "admin"})
check("create admin role -> 400", r.status_code == 400, r.text)

print("\n== staff cannot access admin-only endpoints ==")
r = client.post("/auth/login", json={"email": staff_email, "password": "Staff@123"})
staff_access = r.json()["tokens"]["access_token"]
check("staff login ok", r.status_code == 200, r.text)
r = client.get("/users", headers={"Authorization": f"Bearer {staff_access}"})
check("staff listing users -> 403", r.status_code == 403, r.text)

print("\n== admin lists firm users ==")
r = client.get("/users", headers={"Authorization": f"Bearer {admin_access}"})
check("list users -> 200", r.status_code == 200, r.text)
check("firm has 2 users (admin + staff)", len(r.json()) == 2, r.text)

print("\n== deactivate staff, then blocked from login ==")
r = client.patch(f"/users/{staff_id}/status",
    headers={"Authorization": f"Bearer {admin_access}"}, json={"is_active": False})
check("deactivate -> 200", r.status_code == 200 and r.json()["is_active"] is False, r.text)
r = client.post("/auth/login", json={"email": staff_email, "password": "Staff@123"})
check("deactivated staff login -> 403", r.status_code == 403, r.text)

print("\n== refresh token flow ==")
r = client.post("/auth/refresh", json={"refresh_token": admin_refresh})
check("refresh -> 200 new access", r.status_code == 200 and "access_token" in r.json(), r.text)

print("\n== logout revokes refresh ==")
r = client.post("/auth/logout", json={"refresh_token": admin_refresh})
check("logout -> 200", r.status_code == 200, r.text)
r = client.post("/auth/refresh", json={"refresh_token": admin_refresh})
check("refresh after logout -> 401", r.status_code == 401, r.text)

print("\n== change-password (authenticated) ==")
# Log the demo admin in, change password, verify old fails / new works.
r = client.post("/auth/login", json={"email": "admin@demo.com", "password": "Admin@123"})
demo_access = r.json()["tokens"]["access_token"]
r = client.post("/auth/change-password",
    headers={"Authorization": f"Bearer {demo_access}"},
    json={"current_password": "Admin@123", "new_password": "NewPass@123"})
check("change-password -> 200", r.status_code == 200, r.text)
check("wrong current password -> 400",
      client.post("/auth/change-password", headers={"Authorization": f"Bearer {demo_access}"},
                  json={"current_password": "WRONG", "new_password": "Whatever@123"}).status_code == 400)
check("old password no longer works -> 401",
      client.post("/auth/login", json={"email": "admin@demo.com", "password": "Admin@123"}).status_code == 401)
check("new password works -> 200",
      client.post("/auth/login", json={"email": "admin@demo.com", "password": "NewPass@123"}).status_code == 200)

print("\n== forgot-password / reset-password flow ==")
r = client.post("/auth/forgot-password", json={"email": firm_email})
check("forgot-password -> 200", r.status_code == 200, r.text)
reset_token = r.json().get("reset_token")
check("reset token issued (dev expose)", bool(reset_token), r.text)
check("forgot-password unknown email still 200 (no leak)",
      client.post("/auth/forgot-password", json={"email": "nobody@nowhere.com"}).status_code == 200)
r = client.post("/auth/reset-password", json={"token": reset_token, "new_password": "Reset@123"})
check("reset-password -> 200", r.status_code == 200, r.text)
check("login with new password -> 200",
      client.post("/auth/login", json={"email": firm_email, "password": "Reset@123"}).status_code == 200)
check("reusing the same reset token -> 400",
      client.post("/auth/reset-password", json={"token": reset_token, "new_password": "Again@123"}).status_code == 400)
check("bogus reset token -> 400",
      client.post("/auth/reset-password", json={"token": "not-a-real-token", "new_password": "Again@123"}).status_code == 400)
# The reset must have re-fetched a fresh admin token; get one for the isolation test below.
admin_access = client.post("/auth/login", json={"email": firm_email, "password": "Reset@123"}).json()["tokens"]["access_token"]

print("\n== admin resets a staff member's password ==")
staff2_email = f"deliv_{uuid.uuid4().hex[:8]}@firm.com"
r = client.post("/users", headers={"Authorization": f"Bearer {admin_access}"},
    json={"name": "Suresh", "email": staff2_email, "password": "Staff@123", "role": "delivery_partner"})
staff2_id = r.json()["id"]
r = client.post(f"/users/{staff2_id}/reset-password",
    headers={"Authorization": f"Bearer {admin_access}"}, json={"new_password": "FreshPass@123"})
check("admin reset staff password -> 200", r.status_code == 200, r.text)
check("staff logs in with admin-set password -> 200",
      client.post("/auth/login", json={"email": staff2_email, "password": "FreshPass@123"}).status_code == 200)

print("\n== tenant isolation: admin cannot touch another firm's user ==")
# Register a second firm and try to deactivate its admin from the first firm.
other_email = f"other_{uuid.uuid4().hex[:8]}@firm.com"
r2 = client.post("/auth/register", json={
    "organization_name": "Other Co", "admin_name": "Other", "email": other_email, "password": "Secret@123"})
other_user_id = r2.json()["user"]["id"]
r = client.patch(f"/users/{other_user_id}/status",
    headers={"Authorization": f"Bearer {admin_access}"}, json={"is_active": False})
check("cross-firm status change -> 404", r.status_code == 404, r.text)

print("\n== auto-migration adds missing columns to an existing table ==")
# Simulate an OLD database: a table created before `address` existed, then verify
# auto_add_missing_columns() brings it up to date without dropping data.
import os as _os  # noqa: E402

from sqlalchemy import create_engine as _create_engine, inspect as _inspect, text as _text  # noqa: E402

_mig_db = "_migrate_test.db"
if _os.path.exists(_mig_db):
    _os.remove(_mig_db)
_eng = _create_engine(f"sqlite:///./{_mig_db}")
with _eng.begin() as _c:
    # Old-style organizations table: missing business_type/address/financial_year/logo_url.
    _c.execute(_text("CREATE TABLE organizations (id VARCHAR(36) PRIMARY KEY, name VARCHAR(200))"))
    _c.execute(_text("INSERT INTO organizations (id, name) VALUES ('o1', 'Legacy Co')"))

import app.core.database as _dbmod  # noqa: E402

_orig_engine = _dbmod.engine
_dbmod.engine = _eng  # point the migrator at our simulated old DB
try:
    _dbmod.auto_add_missing_columns()
    _cols = {c["name"] for c in _inspect(_eng).get_columns("organizations")}
    check("migration added address column", "address" in _cols, _cols)
    check("migration added business_type column", "business_type" in _cols, _cols)
    with _eng.connect() as _c:
        _row = _c.execute(_text("SELECT name FROM organizations WHERE id='o1'")).fetchone()
    check("existing row preserved after migration", _row is not None and _row[0] == "Legacy Co", _row)
finally:
    _dbmod.engine = _orig_engine
    _eng.dispose()
    if _os.path.exists(_mig_db):
        try:
            _os.remove(_mig_db)
        except PermissionError:
            pass

print(f"\n==================\n  {passed} passed, {failed} failed\n==================")

# Release the SQLite file handle before deleting it (Windows locks open files).
from app.core.database import engine  # noqa: E402

client.close()
engine.dispose()
if os.path.exists("_smoke_test.db"):
    try:
        os.remove("_smoke_test.db")
    except PermissionError:
        pass  # leftover file is harmless; next run recreates it

raise SystemExit(1 if failed else 0)
