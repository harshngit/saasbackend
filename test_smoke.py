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
check("new org starts on trial", org_out["status"] == "trial", org_out)
check("trial_ends_at is set", org_out["trial_ends_at"] is not None, org_out)
check("trial_days_left ~7", org_out["trial_days_left"] in (6, 7), org_out)
check("upgrade_status none at register", org_out["upgrade_status"] == "none", org_out)
check("new org on default Free plan", (org_out.get("plan") or {}).get("name") == "Free", org_out)
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

print("\n== /auth/me returns user + organization ==")
r = client.get("/auth/me", headers={"Authorization": f"Bearer {admin_access}"})
check("me returns the admin", r.status_code == 200 and r.json()["user"]["email"] == firm_email, r.text)
check("me includes organization with status", r.json().get("organization", {}).get("status") == "trial", r.text)
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

print("\n== trial expiry -> lock -> upgrade -> super-admin approve ==")
from datetime import datetime, timedelta, timezone  # noqa: E402

from app.core.database import SessionLocal  # noqa: E402
from app.models import Organization  # noqa: E402

# Fresh firm for the lifecycle test.
life_email = f"life_{uuid.uuid4().hex[:8]}@firm.com"
reg = client.post("/auth/register", json={
    "organization_name": "Lifecycle Co", "admin_name": "L", "email": life_email, "password": "Secret@123"}).json()
life_org_id = reg["organization"]["id"]
life_access = reg["tokens"]["access_token"]
life_hdr = {"Authorization": f"Bearer {life_access}"}

# During trial, a mutation works.
r = client.post("/users", headers=life_hdr,
    json={"name": "S", "email": f"s_{uuid.uuid4().hex[:6]}@f.com", "password": "Staff@123", "role": "sales_officer"})
check("trial: staff creation allowed", r.status_code == 201, r.text)

# Force the trial into the past, then a fresh login must flip it to locked.
_db = SessionLocal()
_org = _db.get(Organization, life_org_id)
_org.trial_ends_at = datetime.now(timezone.utc) - timedelta(days=1)
_db.commit()
_db.close()

r = client.post("/auth/login", json={"email": life_email, "password": "Secret@123"})
check("expired trial login -> org locked", r.json()["organization"]["status"] == "locked", r.text)
life_access = r.json()["tokens"]["access_token"]
life_hdr = {"Authorization": f"Bearer {life_access}"}

# Locked: mutation blocked, read-only + upgrade-request still allowed.
r = client.post("/users", headers=life_hdr,
    json={"name": "S2", "email": f"s2_{uuid.uuid4().hex[:6]}@f.com", "password": "Staff@123", "role": "sales_officer"})
check("locked: staff creation -> 403", r.status_code == 403, r.text)
check("locked: GET /organizations/me still works",
      client.get("/organizations/me", headers=life_hdr).status_code == 200)
check("locked: GET /users (read-only) still works",
      client.get("/users", headers=life_hdr).status_code == 200)
# Look up plan ids from the seeded catalog.
_plans = client.get("/plans", headers=life_hdr).json()
pro_id = next(p["id"] for p in _plans if p["name"] == "Pro")
basic_id = next(p["id"] for p in _plans if p["name"] == "Basic")
check("upgrade-request unknown plan -> 400",
      client.post("/organizations/upgrade-request", headers=life_hdr,
                  json={"requested_plan_id": "nope", "billing_cycle": "monthly"}).status_code == 400)
r = client.post("/organizations/upgrade-request", headers=life_hdr,
                json={"requested_plan_id": pro_id, "billing_cycle": "yearly"})
check("locked: upgrade-request allowed", r.status_code == 200 and r.json()["upgrade_status"] == "pending", r.text)
check("upgrade-request set requested plan (Pro)", (r.json().get("requested_plan") or {}).get("name") == "Pro", r.text)
check("upgrade-request set billing_cycle", r.json()["billing_cycle"] == "yearly", r.text)

print("\n== super admin approval flow ==")
sa = client.post("/auth/login", json={"email": "superadmin@demo.com", "password": "Admin@123"})
check("super admin login", sa.status_code == 200, sa.text)
sa_hdr = {"Authorization": f"Bearer {sa.json()['tokens']['access_token']}"}

check("super admin lists organizations", client.get("/superadmin/organizations", headers=sa_hdr).status_code == 200)
r = client.get("/superadmin/organizations", headers=sa_hdr, params={"status": "locked"})
check("filter ?status=locked includes our org", any(o["id"] == life_org_id for o in r.json()), r.text)

# A non-super-admin (the firm admin) must not reach /superadmin/*.
r = client.get("/superadmin/organizations", headers=life_hdr)
check("non-super-admin blocked from /superadmin -> 403", r.status_code == 403, r.text)

# Approve the upgrade → active on the requested plan, and the admin is unlocked.
r = client.patch(f"/superadmin/organizations/{life_org_id}/approve-upgrade", headers=sa_hdr)
check("approve-upgrade -> active", r.status_code == 200 and r.json()["status"] == "active", r.text)
check("approve set plan to requested (Pro)", (r.json().get("plan") or {}).get("name") == "Pro", r.text)
check("approve cleared requested_plan_id", r.json().get("requested_plan_id") is None, r.text)
r = client.post("/users", headers=life_hdr,
    json={"name": "S3", "email": f"s3_{uuid.uuid4().hex[:6]}@f.com", "password": "Staff@123", "role": "accountant"})
check("after approval: mutation unlocked -> 201", r.status_code == 201, r.text)

print("\n== super admin reject + manual suspend ==")
rej_email = f"rej_{uuid.uuid4().hex[:8]}@firm.com"
rej = client.post("/auth/register", json={
    "organization_name": "Reject Co", "admin_name": "R", "email": rej_email, "password": "Secret@123"}).json()
rej_id = rej["organization"]["id"]
client.post("/organizations/upgrade-request",
    headers={"Authorization": f"Bearer {rej['tokens']['access_token']}"},
    json={"requested_plan_id": basic_id, "billing_cycle": "monthly"})
r = client.patch(f"/superadmin/organizations/{rej_id}/reject-upgrade", headers=sa_hdr, json={"reason": "Payment not verified"})
check("reject-upgrade -> rejected + reason", r.status_code == 200 and r.json()["upgrade_status"] == "rejected"
      and r.json()["upgrade_reject_reason"] == "Payment not verified", r.text)

r = client.patch(f"/superadmin/organizations/{life_org_id}/status", headers=sa_hdr, json={"status": "suspended"})
check("manual status override -> suspended", r.status_code == 200 and r.json()["status"] == "suspended", r.text)
r = client.post("/users", headers=life_hdr,
    json={"name": "X", "email": f"x_{uuid.uuid4().hex[:6]}@f.com", "password": "Staff@123", "role": "accountant"})
check("suspended: mutation blocked -> 403", r.status_code == 403, r.text)

print("\n== plan catalog (GET /plans + super admin management) ==")
r = client.get("/plans", headers=sa_hdr)
names = [p["name"] for p in r.json()]
check("GET /plans returns seeded catalog", r.status_code == 200 and {"Free", "Basic", "Pro", "Enterprise"} <= set(names), names)
check("GET /plans only active plans", all(p["is_active"] for p in r.json()), r.text)
# Super admin creates a plan.
r = client.post("/superadmin/plans", headers=sa_hdr,
    json={"name": "Starter", "price_monthly": 199, "price_yearly": 1999, "features": ["Basic stuff"], "max_users": 5})
check("super admin create plan -> 201", r.status_code == 201, r.text)
starter_id = r.json()["id"]
check("new plan appears in GET /plans", any(p["name"] == "Starter" for p in client.get("/plans", headers=sa_hdr).json()))
# Edit its price.
r = client.put(f"/superadmin/plans/{starter_id}", headers=sa_hdr, json={"price_monthly": 149})
check("update plan price -> 200", r.status_code == 200 and r.json()["price_monthly"] == 149, r.text)
# Non-super-admin cannot manage plans.
check("non-super-admin create plan -> 403",
      client.post("/superadmin/plans", headers=life_hdr, json={"name": "X", "price_monthly": 1, "price_yearly": 1}).status_code == 403)
# Deactivate -> hidden from GET /plans but still in the super admin's full list.
r = client.patch(f"/superadmin/plans/{starter_id}/deactivate", headers=sa_hdr)
check("deactivate plan -> is_active false", r.status_code == 200 and r.json()["is_active"] is False, r.text)
check("deactivated plan hidden from GET /plans",
      all(p["name"] != "Starter" for p in client.get("/plans", headers=sa_hdr).json()))
check("deactivated plan still in super admin list",
      any(p["name"] == "Starter" for p in client.get("/superadmin/plans", headers=sa_hdr).json()))
check("upgrade-request to a deactivated plan -> 400",
      client.post("/organizations/upgrade-request", headers=life_hdr,
                  json={"requested_plan_id": starter_id, "billing_cycle": "monthly"}).status_code == 400)

print("\n== roles: default seeding + CRUD + catalog ==")
# A fresh firm should auto-get the 3 default roles.
roles_email = f"roles_{uuid.uuid4().hex[:8]}@firm.com"
rr = client.post("/auth/register", json={
    "organization_name": "Roles Co", "admin_name": "R", "email": roles_email, "password": "Secret@123"}).json()
roles_hdr = {"Authorization": f"Bearer {rr['tokens']['access_token']}"}
r = client.get("/roles", headers=roles_hdr)
default_names = {x["name"] for x in r.json() if x["is_default"]}
check("register auto-seeds 3 default roles",
      r.status_code == 200 and {"Sales Officer", "Delivery Partner", "Accountant"} <= default_names, r.text)
so = next(x for x in r.json() if x["name"] == "Sales Officer")
check("Sales Officer has full customers access", so["permissions"].get("customers", {}).get("create") is True, so)
check("Sales Officer has no payments access", "payments" not in so["permissions"], so)

# Catalog
r = client.get("/roles/catalog", headers=roles_hdr)
check("catalog returns modules + actions",
      r.status_code == 200 and len(r.json()["modules"]) == 17 and len(r.json()["actions"]) == 7, r.text)

# Create custom role
r = client.post("/roles", headers=roles_hdr, json={
    "name": "Senior Sales Officer",
    "permissions": {"customers": {"view": True, "create": True, "edit": True},
                    "reports": {"view": True, "export": True},
                    "settings": {}}})  # all-false module should be dropped
check("create custom role -> 201", r.status_code == 201, r.text)
custom = r.json()
custom_id = custom["id"]
check("custom role is not default", custom["is_default"] is False, custom)
check("all-false module dropped (deny-by-default)", "settings" not in custom["permissions"], custom)
check("granted actions normalized (7 keys)", set(custom["permissions"]["customers"].keys()) ==
      {"view", "create", "edit", "delete", "approve", "export", "download"}, custom)
check("duplicate role name -> 409",
      client.post("/roles", headers=roles_hdr, json={"name": "Senior Sales Officer", "permissions": {}}).status_code == 409)

# Update (edit a default role's permissions + rename custom)
r = client.put(f"/roles/{custom_id}", headers=roles_hdr, json={"permissions": {"customers": {"view": True}}})
check("update role permissions (full replace) -> 200",
      r.status_code == 200 and r.json()["permissions"] == {"customers": {"view": True, "create": False, "edit": False, "delete": False, "approve": False, "export": False, "download": False}}, r.text)
so_id = so["id"]
r = client.put(f"/roles/{so_id}", headers=roles_hdr, json={"permissions": {"customers": {"view": True}}})
check("editing a default role is allowed -> 200", r.status_code == 200, r.text)

# Delete rules
check("delete a default role -> 400",
      client.delete(f"/roles/{so_id}", headers=roles_hdr).status_code == 400)
check("delete custom role -> 204",
      client.delete(f"/roles/{custom_id}", headers=roles_hdr).status_code == 204)

# Staff (non-admin) cannot access /roles at all.
staff_reg = client.post("/users", headers=roles_hdr, json={
    "name": "St", "email": f"st_{uuid.uuid4().hex[:6]}@f.com", "password": "Staff@123", "role": "sales_officer"})
st_tok = client.post("/auth/login", json={
    "email": staff_reg.json()["email"], "password": "Staff@123"}).json()["tokens"]["access_token"]
check("staff blocked from /roles -> 403",
      client.get("/roles", headers={"Authorization": f"Bearer {st_tok}"}).status_code == 403)
# Cross-org: another firm's admin cannot see this firm's roles by id
other_reg = client.post("/auth/register", json={
    "organization_name": "OtherRoles", "admin_name": "O", "email": f"or_{uuid.uuid4().hex[:8]}@f.com", "password": "Secret@123"}).json()
other_hdr = {"Authorization": f"Bearer {other_reg['tokens']['access_token']}"}
check("cross-org role fetch -> 404",
      client.get(f"/roles/{so_id}", headers=other_hdr).status_code == 404)

print("\n== DEMO direct password reset (check-email + reset-password-direct) ==")
demo_reset_email = f"dr_{uuid.uuid4().hex[:8]}@firm.com"
client.post("/auth/register", json={
    "organization_name": "DR Co", "admin_name": "D", "email": demo_reset_email, "password": "Old@12345"})
check("check-email existing -> true", client.post("/auth/check-email", json={"email": demo_reset_email}).json()["exists"] is True)
check("check-email unknown -> false", client.post("/auth/check-email", json={"email": "nobody@x.com"}).json()["exists"] is False)
check("direct reset -> 200", client.post("/auth/reset-password-direct",
      json={"email": demo_reset_email, "new_password": "New@12345"}).status_code == 200)
check("old password fails after direct reset -> 401",
      client.post("/auth/login", json={"email": demo_reset_email, "password": "Old@12345"}).status_code == 401)
check("new password works after direct reset -> 200",
      client.post("/auth/login", json={"email": demo_reset_email, "password": "New@12345"}).status_code == 200)
check("direct reset unknown email -> 404",
      client.post("/auth/reset-password-direct", json={"email": "nobody@x.com", "new_password": "New@12345"}).status_code == 404)

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
