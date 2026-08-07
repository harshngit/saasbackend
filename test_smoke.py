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

# The employee form makes these mandatory on POST /users. Nearly every staff
# creation below is really testing roles / permissions / lifecycle rather than
# the HR form, so fill the mandatory fields in centrally instead of repeating
# them at ~30 call sites. A body that sets one of them keeps its own value, and
# `_raw_post` bypasses this for the checks that exercise the contract itself.
_STAFF_REQUIRED = {
    "first_name": "Test",
    "last_name": "Staff",
    "phone": "9800000000",
    "designation": "Staff",
    "employment_type": "full_time",
    "date_of_joining": "2026-01-01T00:00:00Z",
    "employee_status": "active",
    "identity_proof_type": "Aadhaar",
    "identity_proof_file": "data:image/png;base64,AAAA",
    "status": "active",
}

_raw_post = client.post


def _post_with_staff_defaults(url, *args, **kwargs):
    body = kwargs.get("json")
    if url == "/users" and isinstance(body, dict):
        body = {**_STAFF_REQUIRED, **body}
        body.setdefault("confirm_password", body.get("password"))
        kwargs["json"] = body
    return _raw_post(url, *args, **kwargs)


client.post = _post_with_staff_defaults

def is_file_url(value):
    """Uploads now return a link to GET /files/{id}, never an inline data: URL."""
    return isinstance(value, str) and "/files/" in value and "base64," not in value


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
    json={"name": "Ramesh", "email": staff_email, "password": "Staff@123", "username": f"u_{uuid.uuid4().hex[:8]}", "role": "sales_officer"})
check("create sales_officer -> 201", r.status_code == 201, r.text)
staff_id = r.json().get("id")
check("staff belongs to admin's org", r.json().get("organization_id") == body["organization"]["id"], r.text)

print("\n== admin cannot create another admin/super_admin ==")
r = client.post("/users",
    headers={"Authorization": f"Bearer {admin_access}"},
    json={"name": "X", "email": f"x_{uuid.uuid4().hex[:6]}@f.com", "password": "Staff@123", "username": f"u_{uuid.uuid4().hex[:8]}", "role": "admin"})
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
    json={"name": "Suresh", "email": staff2_email, "password": "Staff@123", "username": f"u_{uuid.uuid4().hex[:8]}", "role": "delivery_partner"})
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
    json={"name": "S", "email": f"s_{uuid.uuid4().hex[:6]}@f.com", "password": "Staff@123", "username": f"u_{uuid.uuid4().hex[:8]}", "role": "sales_officer"})
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
    json={"name": "S2", "email": f"s2_{uuid.uuid4().hex[:6]}@f.com", "password": "Staff@123", "username": f"u_{uuid.uuid4().hex[:8]}", "role": "sales_officer"})
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
    json={"name": "S3", "email": f"s3_{uuid.uuid4().hex[:6]}@f.com", "password": "Staff@123", "username": f"u_{uuid.uuid4().hex[:8]}", "role": "accountant"})
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

# Super Admin delete organization (cascades users etc.)
del_reg = client.post("/auth/register", json={
    "organization_name": "Delete Me Co", "admin_name": "D", "email": f"del_{uuid.uuid4().hex[:8]}@f.com", "password": "Secret@123"}).json()
del_org_id = del_reg["organization"]["id"]
del_user_id = del_reg["user"]["id"]
check("super admin delete org -> 204", client.delete(f"/superadmin/organizations/{del_org_id}", headers=sa_hdr).status_code == 204)
check("deleted org gone -> 404", client.get(f"/superadmin/organizations/{del_org_id}", headers=sa_hdr).status_code == 404)
from app.models import User as _User  # noqa: E402
_vdb = SessionLocal()
check("org delete cascaded its users", _vdb.get(_User, del_user_id) is None)
_vdb.close()
check("non-super-admin cannot delete org -> 403", client.delete(f"/superadmin/organizations/{life_org_id}", headers=life_hdr).status_code == 403)
r = client.post("/users", headers=life_hdr,
    json={"name": "X", "email": f"x_{uuid.uuid4().hex[:6]}@f.com", "password": "Staff@123", "username": f"u_{uuid.uuid4().hex[:8]}", "role": "accountant"})
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
# Status toggle: reactivate the deactivated plan, then it shows up again
r = client.patch(f"/superadmin/plans/{starter_id}/status", headers=sa_hdr, json={"is_active": True})
check("status toggle -> active", r.status_code == 200 and r.json()["is_active"] is True, r.text)
check("reactivated plan visible in GET /plans",
      any(p["name"] == "Starter" for p in client.get("/plans", headers=sa_hdr).json()))
r = client.patch(f"/superadmin/plans/{starter_id}/status", headers=sa_hdr, json={"is_active": False})
check("status toggle -> inactive", r.status_code == 200 and r.json()["is_active"] is False, r.text)

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
    "name": "St", "email": f"st_{uuid.uuid4().hex[:6]}@f.com", "password": "Staff@123", "username": f"u_{uuid.uuid4().hex[:8]}", "role": "sales_officer"})
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

print("\n== Phase 2: users wired to roles (role_id, system_role, permissions) ==")
p2_email = f"p2_{uuid.uuid4().hex[:8]}@firm.com"
p2 = client.post("/auth/register", json={
    "organization_name": "P2 Co", "admin_name": "P2 Admin", "email": p2_email, "password": "Secret@123"}).json()
p2_hdr = {"Authorization": f"Bearer {p2['tokens']['access_token']}"}
check("admin has system_role=admin", p2["user"]["system_role"] == "admin", p2["user"])

# admin /auth/me → full_access true, empty permissions
me = client.get("/auth/me", headers=p2_hdr).json()
check("admin me full_access=true", me["full_access"] is True and me["permissions"] == {}, me)

# Get role ids
roles = client.get("/roles", headers=p2_hdr).json()
sales_role = next(r for r in roles if r["name"] == "Sales Officer")

# Create staff via role_id
st_email = f"p2s_{uuid.uuid4().hex[:6]}@f.com"
r = client.post("/users", headers=p2_hdr, json={
    "name": "Staffy", "email": st_email, "password": "Staff@123", "username": f"u_{uuid.uuid4().hex[:8]}", "role_id": sales_role["id"]})
check("create staff via role_id -> 201", r.status_code == 201, r.text)
staff = r.json()
check("staff system_role=staff", staff["system_role"] == "staff", staff)
check("staff role_id set", staff["role_id"] == sales_role["id"], staff)
check("staff role_detail has permissions", staff["role_detail"]["permissions"].get("customers", {}).get("create") is True, staff)
check("staff legacy role mapped (sales_officer)", staff["role"] == "sales_officer", staff)
p2_staff_id = staff["id"]

# role_id from another org → rejected
other = client.post("/auth/register", json={
    "organization_name": "P2 Other", "admin_name": "O", "email": f"p2o_{uuid.uuid4().hex[:6]}@f.com", "password": "Secret@123"}).json()
other_roles = client.get("/roles", headers={"Authorization": f"Bearer {other['tokens']['access_token']}"}).json()
foreign_role_id = other_roles[0]["id"]
check("create staff with cross-org role_id -> 400",
      client.post("/users", headers=p2_hdr, json={"name": "X", "email": f"x_{uuid.uuid4().hex[:6]}@f.com", "password": "Staff@123", "username": f"u_{uuid.uuid4().hex[:8]}", "role_id": foreign_role_id}).status_code == 400)

# Staff login → me shows their permissions + full_access false
st_login = client.post("/auth/login", json={"email": st_email, "password": "Staff@123"}).json()
st_hdr = {"Authorization": f"Bearer {st_login['tokens']['access_token']}"}
st_me = client.get("/auth/me", headers=st_hdr).json()
check("staff me full_access=false", st_me["full_access"] is False, st_me)
check("staff me exposes permission matrix", st_me["permissions"].get("customers", {}).get("view") is True, st_me)
check("staff blocked from /users -> 403", client.get("/users", headers=st_hdr).status_code == 403)

# Filters
check("filter ?role_id returns the staff",
      any(u["id"] == p2_staff_id for u in client.get("/users", headers=p2_hdr, params={"role_id": sales_role["id"]}).json()))
check("filter ?is_active=false empty (none deactivated yet)",
      len(client.get("/users", headers=p2_hdr, params={"is_active": "false"}).json()) == 0)

# GET one / PATCH profile / PATCH role
check("GET /users/{id} -> 200", client.get(f"/users/{p2_staff_id}", headers=p2_hdr).status_code == 200)
r = client.patch(f"/users/{p2_staff_id}", headers=p2_hdr, json={"phone": "9000000000", "name": "Staffy Renamed"})
check("PATCH /users/{id} profile -> 200", r.status_code == 200 and r.json()["phone"] == "9000000000" and r.json()["name"] == "Staffy Renamed", r.text)
deliv_role = next(rr for rr in roles if rr["name"] == "Delivery Partner")
r = client.patch(f"/users/{p2_staff_id}/role", headers=p2_hdr, json={"role_id": deliv_role["id"]})
check("PATCH /users/{id}/role -> 200 new role", r.status_code == 200 and r.json()["role_id"] == deliv_role["id"] and r.json()["role"] == "delivery_partner", r.text)

# Cross-org: another admin can't fetch this firm's user
check("cross-org GET /users/{id} -> 404", client.get(f"/users/{p2_staff_id}", headers={"Authorization": f"Bearer {other['tokens']['access_token']}"}).status_code == 404)

# Delete role rules with assignment
# Sales Officer no longer has the staff (moved to Delivery Partner) but is default → 400
check("delete default role -> 400", client.delete(f"/roles/{sales_role['id']}", headers=p2_hdr).status_code == 400)
# Custom role with an assigned user → 400
cr = client.post("/roles", headers=p2_hdr, json={"name": "Temp Role", "permissions": {"customers": {"view": True}}}).json()
client.patch(f"/users/{p2_staff_id}/role", headers=p2_hdr, json={"role_id": cr["id"]})
check("delete custom role with assigned user -> 400",
      client.delete(f"/roles/{cr['id']}", headers=p2_hdr).status_code == 400)

print("\n== staff creation accepts custom roles from the Roles page ==")
hr_role = client.post("/roles", headers=p2_hdr, json={
    "name": "HR", "permissions": {"customers": {"view": True}}}).json()
check("create custom role HR -> id", "id" in hr_role, hr_role)

# By role_id — what the frontend should send after loading GET /roles.
r = client.post("/users", headers=p2_hdr, json={
    "name": "Rahul Sharma", "email": f"hr1_{uuid.uuid4().hex[:6]}@f.com",
    "username": f"u_{uuid.uuid4().hex[:8]}", "password": "Rahul@12345", "role_id": hr_role["id"]})
check("create staff with custom role_id -> 201", r.status_code == 201, r.text)
check("custom-role staff has no legacy enum role", r.json()["role"] is None, r.text)
check("custom-role staff carries the role name", r.json()["role_detail"]["name"] == "HR", r.text)

# By name — "hr" used to 422 against the fixed enum.
r = client.post("/users", headers=p2_hdr, json={
    "name": "Priya", "email": f"hr2_{uuid.uuid4().hex[:6]}@f.com",
    "username": f"u_{uuid.uuid4().hex[:8]}", "password": "Priya@12345", "role": "hr"})
check("create staff with custom role name -> 201", r.status_code == 201, r.text)
check("role name resolved to the HR role", r.json()["role_id"] == hr_role["id"], r.text)

# Legacy enum values still resolve, via the default role of the same name.
r = client.post("/users", headers=p2_hdr, json={
    "name": "Legacy", "email": f"hr3_{uuid.uuid4().hex[:6]}@f.com",
    "username": f"u_{uuid.uuid4().hex[:8]}", "password": "Legacy@12345", "role": "sales_officer"})
check("legacy role=sales_officer still -> 201", r.status_code == 201, r.text)
check("legacy role maps to the Sales Officer role", r.json()["role_detail"]["name"] == "Sales Officer", r.text)

r = client.post("/users", headers=p2_hdr, json={
    "name": "Ghost", "email": f"hr4_{uuid.uuid4().hex[:6]}@f.com",
    "username": f"u_{uuid.uuid4().hex[:8]}", "password": "Ghost@12345", "role": "no_such_role"})
check("unknown role name -> 400 (not 422)", r.status_code == 400, r.text)
check("400 lists the firm's roles", "HR" in r.json()["detail"], r.text)

# Cross-org role name must not leak in either.
check("cross-org role name -> 400",
      client.post("/users", headers=p2_hdr, json={
          "name": "X", "email": f"hr5_{uuid.uuid4().hex[:6]}@f.com",
          "username": f"u_{uuid.uuid4().hex[:8]}", "password": "Staff@123",
          "role": "Senior Sales Officer"}).status_code == 400)

# PATCH /role accepts a name too.
r = client.patch(f"/users/{p2_staff_id}/role", headers=p2_hdr, json={"role": "HR"})
check("PATCH /users/{id}/role by name -> 200", r.status_code == 200 and r.json()["role_id"] == hr_role["id"], r.text)
check("PATCH /role with neither id nor name -> 422",
      client.patch(f"/users/{p2_staff_id}/role", headers=p2_hdr, json={}).status_code == 422)

print("\n== Categories + Products module (CRUD, variants, bulk-delete, permissions) ==")
cp_email = f"cp_{uuid.uuid4().hex[:8]}@firm.com"
cpr = client.post("/auth/register", json={
    "organization_name": "Catalog Co", "admin_name": "K", "email": cp_email, "password": "Secret@123"}).json()
cp_hdr = {"Authorization": f"Bearer {cpr['tokens']['access_token']}"}

# --- Categories ---
r = client.post("/categories", headers=cp_hdr, json={"name": "Beverages", "description": "Drinks", "image": "data:image/png;base64,AAA"})
check("create category -> 201", r.status_code == 201, r.text)
cat_id = r.json()["id"]
check("duplicate category name -> 409", client.post("/categories", headers=cp_hdr, json={"name": "Beverages"}).status_code == 409)
r2 = client.post("/categories", headers=cp_hdr, json={"name": "Snacks"})
cat2_id = r2.json()["id"]
check("list categories -> 2", len(client.get("/categories", headers=cp_hdr).json()) == 2)
check("get category -> 200", client.get(f"/categories/{cat_id}", headers=cp_hdr).status_code == 200)
r = client.patch(f"/categories/{cat_id}", headers=cp_hdr, json={"description": "All drinks"})
check("update category -> 200", r.status_code == 200 and r.json()["description"] == "All drinks", r.text)

# --- Products with variants ---
r = client.post("/products", headers=cp_hdr, json={
    "name": "Thums Up", "description": "Soft drink", "price": "40", "brand": "Coca-Cola", "sku": "TU-001",
    "category_id": cat_id, "cover_image": "data:image/png;base64,BBB", "images": ["data:image/png;base64,CCC"],
    "variations": [
        {"name": "200ml", "price": "20", "inventory": 100, "weight": 0.2},
        {"name": "500ml", "price": "40", "inventory": 50},
        {"name": "1L", "price": "70", "inventory": 30},
    ]})
check("create product with variants -> 201", r.status_code == 201, r.text)
prod = r.json()
prod_id = prod["id"]
check("price coerced from string to number", prod["price"] == 40.0, prod)
check("product has 3 variants", len(prod["variations"]) == 3, prod)
check("variant price coerced", prod["variations"][0]["price"] == 20.0, prod)
check("total_stock = sum of variant inventory", prod["total_stock"] == 180, prod)
check("product linked to category", prod["category_id"] == cat_id, prod)

# no-variant product uses total_inventory
r = client.post("/products", headers=cp_hdr, json={"name": "Water Bottle", "price": 10, "total_inventory": 500})
check("no-variant product total_stock=total_inventory", r.json()["total_stock"] == 500, r.text)
prod2_id = r.json()["id"]

# list omits images (light), detail includes them
lst = client.get("/products", headers=cp_hdr).json()
check("product list -> 2 items", len(lst) == 2, lst)
check("list item has NO images field", "images" not in lst[0], lst[0])
check("detail has images field", "images" in client.get(f"/products/{prod_id}", headers=cp_hdr).json())
check("search product by brand", any(p["id"] == prod_id for p in client.get("/products", headers=cp_hdr, params={"search": "Coca"}).json()))
check("filter by category", all(p["category_id"] == cat_id for p in client.get("/products", headers=cp_hdr, params={"category_id": cat_id}).json()))

# update product: replace variants
r = client.patch(f"/products/{prod_id}", headers=cp_hdr, json={"price": 45, "variations": [{"name": "2L", "price": 120, "inventory": 10}]})
check("update replaces variants -> 1 variant", r.status_code == 200 and len(r.json()["variations"]) == 1 and r.json()["total_stock"] == 10, r.text)
check("invalid category_id on product -> 400",
      client.patch(f"/products/{prod_id}", headers=cp_hdr, json={"category_id": "nope"}).status_code == 400)

# deleting a category nulls its products' category_id (SET NULL)
client.delete(f"/categories/{cat_id}", headers=cp_hdr)
check("product category_id nulled after category delete", client.get(f"/products/{prod_id}", headers=cp_hdr).json()["category_id"] is None)

# bulk delete products
r = client.post("/products/bulk-delete", headers=cp_hdr, json={"ids": [prod_id, prod2_id]})
check("bulk-delete products -> 2", r.status_code == 200 and r.json()["deleted"] == 2, r.text)
check("products gone after bulk delete", len(client.get("/products", headers=cp_hdr).json()) == 0)
# bulk delete categories
r = client.post("/categories/bulk-delete", headers=cp_hdr, json={"ids": [cat2_id]})
check("bulk-delete categories -> 1", r.json()["deleted"] == 1, r.text)

# --- permission enforcement + tenant isolation ---
cp_roles = client.get("/roles", headers=cp_hdr).json()
acc_r = next(x for x in cp_roles if x["name"] == "Accountant")
acc_em = f"cpacc_{uuid.uuid4().hex[:6]}@f.com"
client.post("/users", headers=cp_hdr, json={"name": "A", "email": acc_em, "username": f"cpa_{uuid.uuid4().hex[:6]}", "password": "Staff@123", "role_id": acc_r["id"]})
acc_h = {"Authorization": f"Bearer {client.post('/auth/login', json={'email': acc_em, 'password': 'Staff@123'}).json()['tokens']['access_token']}"}
check("accountant (no products access) view products -> 403", client.get("/products", headers=acc_h).status_code == 403)
so_r = next(x for x in cp_roles if x["name"] == "Sales Officer")
so_em = f"cpso_{uuid.uuid4().hex[:6]}@f.com"
client.post("/users", headers=cp_hdr, json={"name": "S", "email": so_em, "username": f"cps_{uuid.uuid4().hex[:6]}", "password": "Staff@123", "role_id": so_r["id"]})
so_h = {"Authorization": f"Bearer {client.post('/auth/login', json={'email': so_em, 'password': 'Staff@123'}).json()['tokens']['access_token']}"}
check("sales officer (view-only products) view -> 200", client.get("/products", headers=so_h).status_code == 200)
check("sales officer CANNOT create product -> 403", client.post("/products", headers=so_h, json={"name": "x"}).status_code == 403)

print("\n== Notifications + receipts + uploads + purchase-return ==")
import io  # noqa: E402
nt_email = f"nt_{uuid.uuid4().hex[:8]}@firm.com"
ntr = client.post("/auth/register", json={
    "organization_name": "Notif Co", "admin_name": "N", "email": nt_email, "password": "Secret@123"}).json()
nt_hdr = {"Authorization": f"Bearer {ntr['tokens']['access_token']}"}

# order creation notifies the admin
ncust = client.post("/customers", headers=nt_hdr, json={"name": "C"}).json()
nprod = client.post("/products", headers=nt_hdr, json={"name": "P", "total_inventory": 50}).json()
client.post("/orders", headers=nt_hdr, json={"customer_id": ncust["id"], "items": [{"product_id": nprod["id"], "quantity": 2, "unit_price": 100}]})
check("unread-count after order = 1", client.get("/notifications/unread-count", headers=nt_hdr).json()["unread"] == 1)
notes = client.get("/notifications", headers=nt_hdr).json()
check("notifications list has the order note", len(notes) == 1 and notes[0]["type"] == "order", notes)
nid = notes[0]["id"]
check("mark read -> is_read true", client.patch(f"/notifications/{nid}/read", headers=nt_hdr).json()["is_read"] is True)
check("unread-count now 0", client.get("/notifications/unread-count", headers=nt_hdr).json()["unread"] == 0)
# submit an expense -> another notification, then read-all
client.post("/expenses", headers=nt_hdr, json={"category": "Rent", "amount": 100})
check("unread 1 after expense", client.get("/notifications/unread-count", headers=nt_hdr).json()["unread"] == 1)
check("read-all works", client.patch("/notifications/read-all", headers=nt_hdr).status_code == 200)
check("unread 0 after read-all", client.get("/notifications/unread-count", headers=nt_hdr).json()["unread"] == 0)

# customer payment + receipt PDF
client.post(f"/customers/{ncust['id']}/payments", headers=nt_hdr, json={"amount": 150})
pid_ = client.get(f"/customers/{ncust['id']}/payments", headers=nt_hdr).json()[0]["id"]
rc = client.get(f"/customers/{ncust['id']}/payments/receipt/{pid_}", headers=nt_hdr)
check("payment receipt PDF", rc.status_code == 200 and rc.content[:4] == b"%PDF", rc.status_code)

# expense receipt upload (PDF allowed) + request-clarification
ex = client.post("/expenses", headers=nt_hdr, json={"category": "Fuel", "amount": 500}).json()
r = client.post(f"/expenses/{ex['id']}/receipt", headers=nt_hdr, files={"file": ("bill.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")})
check("expense receipt upload (PDF) -> 200", r.status_code == 200 and is_file_url(r.json()["receipt_url"]), r.text[:200])
check("upload non-allowed type -> 400",
      client.post(f"/expenses/{ex['id']}/receipt", headers=nt_hdr, files={"file": ("x.txt", io.BytesIO(b"hi"), "text/plain")}).status_code == 400)
r = client.patch(f"/expenses/{ex['id']}/request-clarification", headers=nt_hdr, json={"reason": "attach GST bill"})
check("request-clarification -> status", r.status_code == 200 and r.json()["status"] == "clarification_requested", r.text)

# purchase document upload + return
nsup = client.post("/suppliers", headers=nt_hdr, json={"name": "Sup"}).json()
pinv = client.post("/purchases", headers=nt_hdr, json={"invoice_number": "PX", "supplier_id": nsup["id"], "items": [{"product_id": nprod["id"], "quantity": 20, "purchase_price": 10}]}).json()
r = client.post(f"/purchases/{pinv['id']}/documents", headers=nt_hdr, files={"file": ("scan.jpg", io.BytesIO(b"\xff\xd8\xff"), "image/jpeg")})
check("purchase document upload -> 200", r.status_code == 200 and is_file_url(r.json()["attachment_url"]), r.text[:200])
client.patch(f"/purchases/{pinv['id']}/approve", headers=nt_hdr)  # stock 50 -> 70, supplier purchases +200
check("stock after purchase approve = 70", client.get(f"/inventory/{nprod['id']}", headers=nt_hdr).json()["total_stock"] == 70)
r = client.post(f"/purchases/{pinv['id']}/returns", headers=nt_hdr, json={"items": [{"product_id": nprod["id"], "quantity": 5}], "reason": "damaged"})
check("purchase return -> 200", r.status_code == 200, r.text)
check("stock after return = 65", client.get(f"/inventory/{nprod['id']}", headers=nt_hdr).json()["total_stock"] == 65)
check("supplier payable reduced by 50 (200-50=150)", client.get(f"/suppliers/{nsup['id']}", headers=nt_hdr).json()["total_purchases"] == 150)

print("\n== Reports module (aggregations + export) ==")
rep_email = f"rep_{uuid.uuid4().hex[:8]}@firm.com"
rpr = client.post("/auth/register", json={
    "organization_name": "Reports Co", "admin_name": "R", "email": rep_email, "password": "Secret@123"}).json()
rep_hdr = {"Authorization": f"Bearer {rpr['tokens']['access_token']}"}
# seed some data: customer + product + order (approved = sale) + expense + supplier + purchase
rc = client.post("/customers", headers=rep_hdr, json={"name": "Hotel X"}).json()
rp = client.post("/products", headers=rep_hdr, json={"name": "Item", "total_inventory": 100}).json()
ro = client.post("/orders", headers=rep_hdr, json={"customer_id": rc["id"], "tax": 90, "items": [{"product_id": rp["id"], "quantity": 5, "unit_price": 200}]}).json()
client.patch(f"/orders/{ro['id']}/approve", headers=rep_hdr)
client.post(f"/customers/{rc['id']}/payments", headers=rep_hdr, json={"amount": 400, "payment_mode": "cash"})
client.post("/expenses", headers=rep_hdr, json={"category": "Rent", "amount": 300})
rs = client.post("/suppliers", headers=rep_hdr, json={"name": "Sup"}).json()
ri = client.post("/purchases", headers=rep_hdr, json={"invoice_number": "P1", "supplier_id": rs["id"], "tax": 40, "items": [{"product_id": rp["id"], "quantity": 10, "purchase_price": 50}]}).json()
client.patch(f"/purchases/{ri['id']}/approve", headers=rep_hdr)

# sales report
r = client.get("/reports/sales", headers=rep_hdr)
check("sales report -> summary+rows", r.status_code == 200 and r.json()["summary"]["total_sales"] == 1090 and len(r.json()["rows"]) == 1, r.text)
# customer-outstanding (order billed 1000+90tax=1090, paid 400 -> 690)
r = client.get("/reports/customer-outstanding", headers=rep_hdr)
check("customer-outstanding report", r.status_code == 200 and r.json()["summary"]["total_outstanding"] == 690, r.text)
# supplier-outstanding (purchase 500+40=540)
r = client.get("/reports/supplier-outstanding", headers=rep_hdr)
check("supplier-outstanding report", r.json()["summary"]["total_payable"] == 540, r.text)
# payment-collection / cash-collection
check("payment-collection total 400", client.get("/reports/payment-collection", headers=rep_hdr).json()["summary"]["total_collected"] == 400)
check("cash-collection total 400", client.get("/reports/cash-collection", headers=rep_hdr).json()["summary"]["total_cash"] == 400)
# expense
check("expense report entries", client.get("/reports/expense", headers=rep_hdr).json()["summary"]["entries"] == 1)
# gst-summary (output 90 from order tax, input 40 from purchase tax)
r = client.get("/reports/gst-summary", headers=rep_hdr).json()
check("gst-summary output/input/net", r["summary"]["output_gst"] == 90 and r["summary"]["input_gst"] == 40 and r["summary"]["net_gst"] == 50, r)
# profit-loss (sales 1000 - purchases 540 - expenses(approved=0, expense is pending) )
r = client.get("/reports/profit-loss", headers=rep_hdr).json()
check("profit-loss computes net", r["summary"]["total_sales"] == 1090 and "net_profit" in r["summary"], r)
# daily-transaction
check("daily-transaction has rows", len(client.get("/reports/daily-transaction", headers=rep_hdr).json()["rows"]) >= 3)
# purchase report
check("purchase report total 540", client.get("/reports/purchase", headers=rep_hdr).json()["summary"]["total_purchases"] == 540)
# unknown type -> 404, bad dates -> 400
check("unknown report type -> 404", client.get("/reports/bogus", headers=rep_hdr).status_code == 404)
check("bad date range -> 400", client.get("/reports/sales", headers=rep_hdr, params={"date_from": "2026-12-31", "date_to": "2026-01-01"}).status_code == 400)
# export
xl = client.get("/reports/sales/export", headers=rep_hdr, params={"format": "excel"})
check("export excel -> xlsx bytes", xl.status_code == 200 and xl.headers["content-type"].startswith("application/vnd.openxml") and xl.content[:2] == b"PK", xl.status_code)
pf = client.get("/reports/sales/export", headers=rep_hdr, params={"format": "pdf"})
check("export pdf -> pdf bytes", pf.status_code == 200 and pf.content[:4] == b"%PDF", pf.status_code)
check("export bad format -> 400", client.get("/reports/sales/export", headers=rep_hdr, params={"format": "word"}).status_code == 400)

print("\n== Purchases + Expenses + Customer Payments (financial modules) ==")
fin_email = f"fin_{uuid.uuid4().hex[:8]}@firm.com"
fpr = client.post("/auth/register", json={
    "organization_name": "Finance Co", "admin_name": "F", "email": fin_email, "password": "Secret@123"}).json()
fin_hdr = {"Authorization": f"Bearer {fpr['tokens']['access_token']}"}

# --- Purchases: approve adds stock + bumps supplier.total_purchases ---
fsup = client.post("/suppliers", headers=fin_hdr, json={"name": "Bulk Supplier", "opening_balance": 0}).json()
fprod = client.post("/products", headers=fin_hdr, json={"name": "Sugar", "total_inventory": 20}).json()
r = client.post("/purchases", headers=fin_hdr, json={
    "invoice_number": "INV-100", "supplier_id": fsup["id"], "discount": 100, "tax": 50,
    "items": [{"product_id": fprod["id"], "quantity": 30, "purchase_price": 40, "tax": 10}]})
check("create purchase -> 201", r.status_code == 201, r.text)
pur = r.json()
pur_id = pur["id"]
check("purchase total = 30*40-100+50 = 1150", pur["total"] == 1150, pur)
check("purchase pending, stock not added (20)", client.get(f"/inventory/{fprod['id']}", headers=fin_hdr).json()["total_stock"] == 20)
r = client.patch(f"/purchases/{pur_id}/approve", headers=fin_hdr)
check("approve purchase -> approved", r.status_code == 200 and r.json()["status"] == "approved", r.text)
check("stock added after approve (20+30=50)", client.get(f"/inventory/{fprod['id']}", headers=fin_hdr).json()["total_stock"] == 50)
check("supplier.total_purchases bumped to 1150",
      client.get(f"/suppliers/{fsup['id']}", headers=fin_hdr).json()["total_purchases"] == 1150)
check("supplier outstanding = 1150 (0 opening + 1150 purchases - 0 paid)",
      client.get(f"/suppliers/{fsup['id']}", headers=fin_hdr).json()["outstanding_payable"] == 1150)
# cancel reverses
r = client.patch(f"/purchases/{pur_id}/cancel", headers=fin_hdr, json={"reason": "wrong order"})
check("cancel purchase reverses stock (back to 20)", client.get(f"/inventory/{fprod['id']}", headers=fin_hdr).json()["total_stock"] == 20)
check("cancel reverses supplier.total_purchases to 0",
      client.get(f"/suppliers/{fsup['id']}", headers=fin_hdr).json()["total_purchases"] == 0)

# --- Expenses: submit -> approve/reject ---
r = client.post("/expenses", headers=fin_hdr, json={"category": "Petrol/Diesel", "amount": 800, "description": "Fuel", "payment_mode": "cash"})
check("create expense -> pending", r.status_code == 201 and r.json()["status"] == "pending", r.text)
exp_id = r.json()["id"]
check("expense categories endpoint", len(client.get("/expenses/categories", headers=fin_hdr).json()["categories"]) > 5)
r = client.patch(f"/expenses/{exp_id}/approve", headers=fin_hdr)
check("approve expense -> approved", r.status_code == 200 and r.json()["status"] == "approved", r.text)
check("approve again -> 400", client.patch(f"/expenses/{exp_id}/approve", headers=fin_hdr).status_code == 400)
r2 = client.post("/expenses", headers=fin_hdr, json={"category": "Food and Travel", "amount": 200}).json()
r = client.patch(f"/expenses/{r2['id']}/reject", headers=fin_hdr, json={"reason": "no receipt"})
check("reject expense -> rejected + reason", r.status_code == 200 and r.json()["status"] == "rejected" and r.json()["reject_reason"] == "no receipt", r.text)
check("filter expenses by status=approved", all(e["status"] == "approved" for e in client.get("/expenses", headers=fin_hdr, params={"status": "approved"}).json()))

# --- Customer Payments + receivables from orders ---
fcust = client.post("/customers", headers=fin_hdr, json={"name": "Regular Buyer", "opening_balance": 500}).json()
check("customer opening_balance -> outstanding 500", fcust["outstanding_balance"] == 500, fcust)
# order for this customer, approve -> billed
o = client.post("/orders", headers=fin_hdr, json={"customer_id": fcust["id"], "items": [{"product_id": fprod["id"], "quantity": 2, "unit_price": 100}]}).json()
client.patch(f"/orders/{o['id']}/approve", headers=fin_hdr)
after_order = client.get(f"/customers/{fcust['id']}", headers=fin_hdr).json()
check("order approve bills customer (outstanding 500+200=700)", after_order["outstanding_balance"] == 700, after_order)
# record payment
r = client.post(f"/customers/{fcust['id']}/payments", headers=fin_hdr, json={"amount": 300, "payment_mode": "upi"})
check("customer payment -> received 300, outstanding 400", r.status_code == 201 and r.json()["total_received"] == 300 and r.json()["outstanding_balance"] == 400, r.text)
pays = client.get(f"/customers/{fcust['id']}/payments", headers=fin_hdr).json()
check("customer payment history -> 1", len(pays) == 1, pays)
r = client.delete(f"/customers/{fcust['id']}/payments/{pays[0]['id']}", headers=fin_hdr)
check("void customer payment -> outstanding back to 700", r.status_code == 200 and r.json()["outstanding_balance"] == 700, r.text)

print("\n== Sales Orders (lifecycle + stock deduction/restore) ==")
so_email = f"so_{uuid.uuid4().hex[:8]}@firm.com"
sopr = client.post("/auth/register", json={
    "organization_name": "Orders Co", "admin_name": "O", "email": so_email, "password": "Secret@123"}).json()
so_hdr = {"Authorization": f"Bearer {sopr['tokens']['access_token']}"}
# a customer + a product with stock
cust = client.post("/customers", headers=so_hdr, json={"name": "Hotel Grand"}).json()
prod = client.post("/products", headers=so_hdr, json={"name": "Rice Bag", "price": 500, "total_inventory": 100}).json()

# create order
r = client.post("/orders", headers=so_hdr, json={
    "customer_id": cust["id"], "discount": 50, "tax": 20,
    "items": [{"product_id": prod["id"], "quantity": 10, "unit_price": 500, "discount": 100}]})
check("create order -> 201", r.status_code == 201, r.text)
order = r.json()
order_id = order["id"]
check("order_number generated", order["order_number"].startswith("SO-"), order)
check("order status pending", order["status"] == "pending", order)
check("line_total = 10*500-100 = 4900", order["items"][0]["line_total"] == 4900, order)
check("order total = 4900-50+20 = 4870", order["total"] == 4870, order)
check("stock NOT yet deducted (still 100)", client.get(f"/inventory/{prod['id']}", headers=so_hdr).json()["total_stock"] == 100)

# reject flow blocked after approve; test approve deducts stock
r = client.patch(f"/orders/{order_id}/approve", headers=so_hdr)
check("approve order -> confirmed", r.status_code == 200 and r.json()["status"] == "confirmed", r.text)
check("stock deducted after approve (100-10=90)", client.get(f"/inventory/{prod['id']}", headers=so_hdr).json()["total_stock"] == 90)
check("approve again -> 400", client.patch(f"/orders/{order_id}/approve", headers=so_hdr).status_code == 400)

# assign delivery partner
dp_email = f"dp_{uuid.uuid4().hex[:6]}@f.com"
roles_so = client.get("/roles", headers=so_hdr).json()
dp_role = next(x for x in roles_so if x["name"] == "Delivery Partner")["id"]
dp = client.post("/users", headers=so_hdr, json={"name": "DP", "email": dp_email, "username": f"dp_{uuid.uuid4().hex[:6]}", "password": "Staff@123", "role_id": dp_role}).json()
r = client.patch(f"/orders/{order_id}/assign-delivery-partner", headers=so_hdr, json={"delivery_partner_id": dp["id"]})
check("assign delivery partner -> out_for_delivery", r.status_code == 200 and r.json()["status"] == "out_for_delivery" and r.json()["assigned_delivery_partner_id"] == dp["id"], r.text)

# cancel restores stock
r = client.patch(f"/orders/{order_id}/cancel", headers=so_hdr, json={"reason": "customer changed mind"})
check("cancel order -> cancelled", r.status_code == 200 and r.json()["status"] == "cancelled", r.text)
check("stock restored after cancel (back to 100)", client.get(f"/inventory/{prod['id']}", headers=so_hdr).json()["total_stock"] == 100)

# reject a fresh pending order
r2 = client.post("/orders", headers=so_hdr, json={"customer_id": cust["id"], "items": [{"product_id": prod["id"], "quantity": 5}]})
oid2 = r2.json()["id"]
check("item unit_price defaults to product price", r2.json()["items"][0]["unit_price"] == 500, r2.text)
r = client.patch(f"/orders/{oid2}/reject", headers=so_hdr, json={"reason": "out of area"})
check("reject pending order -> rejected", r.status_code == 200 and r.json()["status"] == "rejected", r.text)
# filters + tenant isolation
check("list orders by status=cancelled", any(o["id"] == order_id for o in client.get("/orders", headers=so_hdr, params={"status": "cancelled"}).json()))
o_other = client.post("/auth/register", json={"organization_name": "O2", "admin_name": "X", "email": f"o2_{uuid.uuid4().hex[:6]}@f.com", "password": "Secret@123"}).json()
check("cross-org order get -> 404", client.get(f"/orders/{order_id}", headers={"Authorization": f"Bearer {o_other['tokens']['access_token']}"}).status_code == 404)
# insufficient stock on approve
big = client.post("/orders", headers=so_hdr, json={"customer_id": cust["id"], "items": [{"product_id": prod["id"], "quantity": 99999}]}).json()
check("approve with insufficient stock -> 400", client.patch(f"/orders/{big['id']}/approve", headers=so_hdr).status_code == 400)

print("\n== Attendance (4 checkpoints + me + admin view) ==")
att_email = f"att_{uuid.uuid4().hex[:8]}@firm.com"
apr = client.post("/auth/register", json={
    "organization_name": "Att Co", "admin_name": "A", "email": att_email, "password": "Secret@123"}).json()
att_admin_hdr = {"Authorization": f"Bearer {apr['tokens']['access_token']}"}
# create a sales officer staff (has attendance perm)
att_roles = client.get("/roles", headers=att_admin_hdr).json()
so_role_id = next(x for x in att_roles if x["name"] == "Sales Officer")["id"]
staff_att_email = f"atts_{uuid.uuid4().hex[:6]}@f.com"
staff_att = client.post("/users", headers=att_admin_hdr, json={"name": "Field Staff", "email": staff_att_email, "username": f"atts_{uuid.uuid4().hex[:6]}", "password": "Staff@123", "role_id": so_role_id}).json()
staff_att_hdr = {"Authorization": f"Bearer {client.post('/auth/login', json={'email': staff_att_email, 'password': 'Staff@123'}).json()['tokens']['access_token']}"}

# out-of-order rejected
check("departure before check-in -> 400", client.post("/attendance/check-in", headers=staff_att_hdr, json={"type": "departure"}).status_code == 400)
r = client.post("/attendance/check-in", headers=staff_att_hdr, json={"type": "office_check_in"})
check("office_check_in -> 201", r.status_code == 201 and r.json()["office_check_in"] is not None, r.text)
check("duplicate check-in same type -> 400", client.post("/attendance/check-in", headers=staff_att_hdr, json={"type": "office_check_in"}).status_code == 400)
check("departure -> 201", client.post("/attendance/check-in", headers=staff_att_hdr, json={"type": "departure"}).status_code == 201)
check("invalid type -> 422", client.post("/attendance/check-in", headers=staff_att_hdr, json={"type": "lunch"}).status_code == 422)
# me
me_att = client.get("/attendance/me", headers=staff_att_hdr).json()
check("attendance/me -> 1 row with checkpoints", len(me_att) == 1 and me_att[0]["departure"] is not None, me_att)
# admin view
adm_att = client.get("/attendance", headers=att_admin_hdr, params={"user_id": staff_att["id"]}).json()
check("admin attendance view sees staff row", len(adm_att) == 1 and adm_att[0]["user_id"] == staff_att["id"], adm_att)
check("staff cannot access admin attendance -> 403", client.get("/attendance", headers=staff_att_hdr).status_code == 403)
check("bad date range -> 400", client.get("/attendance/me", headers=staff_att_hdr, params={"date_from": "2026-12-31", "date_to": "2026-01-01"}).status_code == 400)

print("\n== Suppliers module (CRUD + payments + balance sync) ==")
sup_email = f"sup_{uuid.uuid4().hex[:8]}@firm.com"
spr = client.post("/auth/register", json={
    "organization_name": "Supplier Co", "admin_name": "S", "email": sup_email, "password": "Secret@123"}).json()
sup_hdr = {"Authorization": f"Bearer {spr['tokens']['access_token']}"}

r = client.post("/suppliers", headers=sup_hdr, json={
    "name": "Global Traders", "contact_person": "Ravi", "phone": "9811111111",
    "gst_number": "29AAAAA1111A1Z5", "category": "Raw Material", "city": "Delhi", "opening_balance": 5000})
check("create supplier -> 201", r.status_code == 201, r.text)
sup = r.json()
sup_id = sup["id"]
check("opening_balance sets outstanding", sup["outstanding_payable"] == 5000, sup)
check("list suppliers -> 200", client.get("/suppliers", headers=sup_hdr).status_code == 200)
check("get supplier detail -> 200", client.get(f"/suppliers/{sup_id}", headers=sup_hdr).status_code == 200)
r = client.put(f"/suppliers/{sup_id}", headers=sup_hdr, json={"category": "Packaging"})
check("edit supplier (PUT) -> 200", r.status_code == 200 and r.json()["category"] == "Packaging", r.text)

# Record payments -> balances stay in sync
r = client.post(f"/suppliers/{sup_id}/payments", headers=sup_hdr, json={"amount": 2000, "payment_mode": "upi", "reference": "TXN1"})
check("record payment -> total_paid 2000, outstanding 3000",
      r.status_code == 201 and r.json()["total_paid"] == 2000 and r.json()["outstanding_payable"] == 3000, r.text)
r = client.post(f"/suppliers/{sup_id}/payments", headers=sup_hdr, json={"amount": 1000, "payment_mode": "cash"})
check("second payment -> outstanding 2000", r.json()["outstanding_payable"] == 2000, r.text)
pays = client.get(f"/suppliers/{sup_id}/payments", headers=sup_hdr).json()
check("payment history -> 2", len(pays) == 2, pays)

# Void a payment -> balance restored
void_pid = pays[0]["id"]
r = client.delete(f"/suppliers/{sup_id}/payments/{void_pid}", headers=sup_hdr)
check("void payment -> balance restored", r.status_code == 200 and r.json()["total_paid"] == 2000, r.text)  # 3000 - 1000
check("payment history -> 1 after void", len(client.get(f"/suppliers/{sup_id}/payments", headers=sup_hdr).json()) == 1)

# status toggle + delete
r = client.patch(f"/suppliers/{sup_id}/status", headers=sup_hdr, json={"is_active": False})
check("deactivate supplier -> 200", r.status_code == 200 and r.json()["is_active"] is False, r.text)
# tenant isolation
sup_other = client.post("/auth/register", json={
    "organization_name": "Sup Other", "admin_name": "O", "email": f"so_{uuid.uuid4().hex[:6]}@f.com", "password": "Secret@123"}).json()
so_hdr2 = {"Authorization": f"Bearer {sup_other['tokens']['access_token']}"}
check("cross-org supplier get -> 404", client.get(f"/suppliers/{sup_id}", headers=so_hdr2).status_code == 404)
check("delete supplier -> 204", client.delete(f"/suppliers/{sup_id}", headers=sup_hdr).status_code == 204)
check("get deleted supplier -> 404", client.get(f"/suppliers/{sup_id}", headers=sup_hdr).status_code == 404)

print("\n== Inventory module (stock board + adjustments + history) ==")
inv_email = f"inv_{uuid.uuid4().hex[:8]}@firm.com"
ipr = client.post("/auth/register", json={
    "organization_name": "Inv Co", "admin_name": "I", "email": inv_email, "password": "Secret@123"}).json()
inv_hdr = {"Authorization": f"Bearer {ipr['tokens']['access_token']}"}

# a no-variant product and a variant product
p_novar = client.post("/products", headers=inv_hdr, json={"name": "Salt", "total_inventory": 100}).json()
p_var = client.post("/products", headers=inv_hdr, json={"name": "Cola", "variations": [
    {"name": "500ml", "inventory": 50}, {"name": "1L", "inventory": 20}]}).json()
var_id = p_var["variations"][0]["id"]

# stock board
board = client.get("/inventory", headers=inv_hdr).json()
check("stock board -> 2 products", len(board) == 2, board)
salt = next(x for x in board if x["name"] == "Salt")
check("board shows available stock", salt["total_stock"] == 100, salt)

# purchase received (+50) on no-variant product
r = client.post("/inventory/adjustments", headers=inv_hdr, json={
    "product_id": p_novar["id"], "movement_type": "purchase_in", "quantity": 50, "note": "PO#1"})
check("purchase_in +50 -> stock 150", r.status_code == 201 and r.json()["total_stock"] == 150, r.text)
# damaged (-10)
r = client.post("/inventory/adjustments", headers=inv_hdr, json={
    "product_id": p_novar["id"], "movement_type": "damaged", "quantity": -10})
check("damaged -10 -> stock 140", r.json()["total_stock"] == 140, r.text)
# insufficient stock guard
check("remove more than available -> 400",
      client.post("/inventory/adjustments", headers=inv_hdr, json={"product_id": p_novar["id"], "movement_type": "sale_out", "quantity": -9999}).status_code == 400)
# invalid movement type -> 422
check("invalid movement_type -> 422",
      client.post("/inventory/adjustments", headers=inv_hdr, json={"product_id": p_novar["id"], "movement_type": "bogus", "quantity": 1}).status_code == 422)

# variant adjustment
r = client.post("/inventory/adjustments", headers=inv_hdr, json={
    "product_id": p_var["id"], "variant_id": var_id, "movement_type": "sale_out", "quantity": -5})
check("variant sale_out -5 -> total_stock 65", r.json()["total_stock"] == 65, r.text)  # (50-5)+20

# movement history
detail = client.get(f"/inventory/{p_novar['id']}", headers=inv_hdr).json()
check("stock detail has movement history", len(detail["movements"]) == 2, detail)
check("movement records balance_after", detail["movements"][0]["balance_after"] in (140, 150), detail)

# PATCH set absolute stock
r = client.patch(f"/inventory/{p_novar['id']}", headers=inv_hdr, json={"quantity": 200, "note": "stock count"})
check("set stock to 200 -> 200 total_stock", r.status_code == 200 and r.json()["total_stock"] == 200, r.text)

print("\n== Customers module (CRUD + tenant isolation + PERMISSION enforcement) ==")
cust_email = f"cust_{uuid.uuid4().hex[:8]}@firm.com"
cr = client.post("/auth/register", json={
    "organization_name": "Cust Co", "admin_name": "C", "email": cust_email, "password": "Secret@123"}).json()
cust_hdr = {"Authorization": f"Bearer {cr['tokens']['access_token']}"}
cust_org = cr["organization"]["id"]

r = client.post("/customers", headers=cust_hdr, json={
    "name": "Acme contact", "business_name": "Acme Corp", "phone": "9812345678",
    "gst_number": "29AAAAA0000A1Z5", "credit_limit": 50000, "category": "Wholesale"})
check("admin create customer -> 201", r.status_code == 201, r.text)
cust_id = r.json()["id"]
check("customer scoped to org", r.json()["organization_id"] == cust_org, r.text)
check("list customers -> 200", client.get("/customers", headers=cust_hdr).status_code == 200)
check("search by name finds it", any(c["id"] == cust_id for c in client.get("/customers", headers=cust_hdr, params={"search": "Acme"}).json()))
check("search miss -> empty", len(client.get("/customers", headers=cust_hdr, params={"search": "zzznope"}).json()) == 0)
check("get customer -> 200", client.get(f"/customers/{cust_id}", headers=cust_hdr).status_code == 200)
r = client.patch(f"/customers/{cust_id}", headers=cust_hdr, json={"credit_limit": 75000, "category": "Retail"})
check("update customer -> 200", r.status_code == 200 and r.json()["credit_limit"] == 75000 and r.json()["category"] == "Retail", r.text)

# Assign a sales officer (must be a same-org user)
c_roles = client.get("/roles", headers=cust_hdr).json()
so_role = next(x for x in c_roles if x["name"] == "Sales Officer")
acc_role = next(x for x in c_roles if x["name"] == "Accountant")
so_email = f"cso_{uuid.uuid4().hex[:6]}@f.com"
so_user = client.post("/users", headers=cust_hdr, json={
    "name": "SO", "email": so_email, "username": f"cso_{uuid.uuid4().hex[:6]}", "password": "Staff@123", "role_id": so_role["id"]}).json()
r = client.patch(f"/customers/{cust_id}", headers=cust_hdr, json={"assigned_sales_officer_id": so_user["id"]})
check("assign sales officer -> nested name", r.status_code == 200 and r.json()["assigned_sales_officer"]["name"] == "SO", r.text)
check("assign cross-firm user -> 400",
      client.patch(f"/customers/{cust_id}", headers=cust_hdr, json={"assigned_sales_officer_id": "nonexistent"}).status_code == 400)

# PERMISSION enforcement — Accountant is view-only, Sales Officer is full on customers
acc_email = f"cacc_{uuid.uuid4().hex[:6]}@f.com"
client.post("/users", headers=cust_hdr, json={
    "name": "Acc", "email": acc_email, "username": f"cacc_{uuid.uuid4().hex[:6]}", "password": "Staff@123", "role_id": acc_role["id"]})
acc_hdr = {"Authorization": f"Bearer {client.post('/auth/login', json={'email': acc_email, 'password': 'Staff@123'}).json()['tokens']['access_token']}"}
check("accountant CAN view customers -> 200", client.get("/customers", headers=acc_hdr).status_code == 200)
check("accountant CANNOT create customer -> 403", client.post("/customers", headers=acc_hdr, json={"name": "x"}).status_code == 403)
so_hdr = {"Authorization": f"Bearer {client.post('/auth/login', json={'email': so_email, 'password': 'Staff@123'}).json()['tokens']['access_token']}"}
check("sales officer CAN create customer -> 201", client.post("/customers", headers=so_hdr, json={"name": "SO made"}).status_code == 201)

# Tenant isolation
c_other = client.post("/auth/register", json={
    "organization_name": "Cust Other", "admin_name": "O", "email": f"cothr_{uuid.uuid4().hex[:6]}@f.com", "password": "Secret@123"}).json()
c_other_hdr = {"Authorization": f"Bearer {c_other['tokens']['access_token']}"}
check("cross-org get customer -> 404", client.get(f"/customers/{cust_id}", headers=c_other_hdr).status_code == 404)
check("cross-org list excludes it", all(c["id"] != cust_id for c in client.get("/customers", headers=c_other_hdr).json()))
check("delete customer -> 204", client.delete(f"/customers/{cust_id}", headers=cust_hdr).status_code == 204)
check("get deleted customer -> 404", client.get(f"/customers/{cust_id}", headers=cust_hdr).status_code == 404)

print("\n== minimal register + Company Settings + staff username ==")
import io  # noqa: E402

# Register with ONLY the 5 basic fields (company profile fields omitted).
min_email = f"min_{uuid.uuid4().hex[:8]}@firm.com"
mr = client.post("/auth/register", json={
    "organization_name": "Minimal Co", "admin_name": "M", "email": min_email,
    "phone": "9998887776", "password": "Secret@123"})
check("register with only basic fields -> 201", mr.status_code == 201, mr.text)
min_hdr = {"Authorization": f"Bearer {mr.json()['tokens']['access_token']}"}
check("company fields empty after minimal register", mr.json()["organization"]["gst_number"] in (None, ""), mr.json()["organization"])

# Company Settings GET / PUT
r = client.get("/organizations/settings", headers=min_hdr)
check("GET /organizations/settings -> 200", r.status_code == 200 and r.json()["name"] == "Minimal Co", r.text)
r = client.put("/organizations/settings", headers=min_hdr,
               json={"gst_number": "29ABCDE1234F1Z5", "business_type": "Retailer", "financial_year": "2025-2026"})
check("PUT settings partial update -> 200",
      r.status_code == 200 and r.json()["gst_number"] == "29ABCDE1234F1Z5" and r.json()["business_type"] == "Retailer", r.text)
check("staff blocked from settings -> 403", client.get("/organizations/settings", headers=st_hdr).status_code == 403)

# Logo upload -> data URL
png = b"\x89PNG\r\n\x1a\n" + b"0" * 40
r = client.post("/organizations/settings/logo", headers=min_hdr, files={"file": ("logo.png", io.BytesIO(png), "image/png")})
check("upload logo -> file URL", r.status_code == 200 and is_file_url(r.json()["url"]), r.text[:200])
check("upload non-image -> 400",
      client.post("/organizations/settings/logo", headers=min_hdr, files={"file": ("x.txt", io.BytesIO(b"hi"), "text/plain")}).status_code == 400)
check("settings reflects uploaded logo",
      is_file_url(client.get("/organizations/settings", headers=min_hdr).json()["logo_url"]))

# Staff username: required + unique
uname = f"staffuser_{uuid.uuid4().hex[:6]}"
r = client.post("/users", headers=min_hdr, json={
    "name": "U", "email": f"u1_{uuid.uuid4().hex[:6]}@f.com", "username": uname, "password": "Staff@123", "role": "accountant"})
check("create staff with username -> 201", r.status_code == 201 and r.json()["username"] == uname, r.text)
check("duplicate username -> 409",
      client.post("/users", headers=min_hdr, json={"name": "U2", "email": f"u2_{uuid.uuid4().hex[:6]}@f.com", "username": uname, "password": "Staff@123", "role": "accountant"}).status_code == 409)
check("missing username -> 422",
      client.post("/users", headers=min_hdr, json={"name": "U3", "email": f"u3_{uuid.uuid4().hex[:6]}@f.com", "password": "Staff@123", "role": "accountant"}).status_code == 422)

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

print("\n== Phase 3: New Modules - Purchase Invoices, Sales Invoices, Vehicle Stock, Deliveries, Notifications ==")

# 1. Purchase Invoices PUT support and list
r = client.get("/purchase-invoices", headers=fin_hdr)
check("list purchase-invoices -> 200", r.status_code == 200, r.text)
# Create a purchase invoice to edit
pur_new = client.post("/purchase-invoices", headers=fin_hdr, json={
    "invoice_number": f"PI-{uuid.uuid4().hex[:6]}",
    "supplier_id": fsup["id"],
    "items": [{"product_id": fprod["id"], "quantity": 5, "purchase_price": 50, "tax": 5}]
}).json()
r = client.put(f"/purchase-invoices/{pur_new['id']}", headers=fin_hdr, json={
    "invoice_number": pur_new["invoice_number"],
    "notes": "Updated notes"
})
check("PUT edit purchase invoice -> 200", r.status_code == 200 and r.json()["notes"] == "Updated notes", r.text)

# 2. Sales Invoices
# Create an order
o_inv = client.post("/orders", headers=fin_hdr, json={
    "customer_id": fcust["id"],
    "items": [{"product_id": fprod["id"], "quantity": 3, "unit_price": 100}]
}).json()
# Approve order
client.patch(f"/orders/{o_inv['id']}/approve", headers=fin_hdr)
# Generate invoice from order
r = client.post(f"/invoices/orders/{o_inv['id']}/invoice", headers=fin_hdr)
check("generate invoice from order -> 201", r.status_code == 201, r.text)
invoice_obj = r.json()
# Duplicate generation should fail
check("duplicate generate invoice -> 400", client.post(f"/invoices/orders/{o_inv['id']}/invoice", headers=fin_hdr).status_code == 400)
# Get and List
check("GET invoice detail -> 200", client.get(f"/invoices/{invoice_obj['id']}", headers=fin_hdr).status_code == 200)
check("GET invoices list -> 200", len(client.get("/invoices", headers=fin_hdr).json()) >= 1)
# PDF download
r = client.get(f"/invoices/{invoice_obj['id']}/pdf", headers=fin_hdr)
check("GET invoice PDF -> 200", r.status_code == 200 and r.headers.get("content-type") == "application/pdf", r.headers)

# Direct Sales Invoice
# First check stock of product
old_stock = client.get(f"/inventory/{fprod['id']}", headers=fin_hdr).json()["total_stock"]
direct_inv = client.post("/invoices", headers=fin_hdr, json={
    "customer_id": fcust["id"],
    "items": [{"product_id": fprod["id"], "quantity": 2, "unit_price": 120, "tax": 10}],
    "discount": 10,
    "tax": 10
}).json()
new_stock = client.get(f"/inventory/{fprod['id']}", headers=fin_hdr).json()["total_stock"]
check("direct invoice deducts stock", new_stock == old_stock - 2, f"Old: {old_stock}, New: {new_stock}")

# Credit Note
r = client.post(f"/invoices/{direct_inv['id']}/credit-note", headers=fin_hdr, json={"reason": "defective"})
check("create credit note -> 200", r.status_code == 200 and r.json()["status"] == "returned", r.text)
stock_after_cn = client.get(f"/inventory/{fprod['id']}", headers=fin_hdr).json()["total_stock"]
check("credit note returns stock back", stock_after_cn == old_stock, f"Expected: {old_stock}, Got: {stock_after_cn}")

# 3. Vehicle Stock
dp_email = f"dp_{uuid.uuid4().hex[:8]}@firm.com"
r = client.post("/users",
    headers=fin_hdr,
    json={"name": "Delivery Partner User", "email": dp_email, "password": "Partner@123", "username": f"u_{uuid.uuid4().hex[:8]}", "role": "delivery_partner"})
check("create delivery partner -> 201", r.status_code == 201, r.text)
dp_user = r.json()
# log them in
r = client.post("/auth/login", json={"email": dp_email, "password": "Partner@123"})
dp_access = r.json()["tokens"]["access_token"]
dp_hdr = {"Authorization": f"Bearer {dp_access}"}

# Load Vehicle (start of day)
# Restock first
client.post("/purchase-invoices", headers=fin_hdr, json={
    "invoice_number": f"PI-{uuid.uuid4().hex[:6]}",
    "supplier_id": fsup["id"],
    "items": [{"product_id": fprod["id"], "quantity": 50, "purchase_price": 50}]
})
# Find and approve the last purchase
last_p = client.get("/purchase-invoices", headers=fin_hdr).json()[0]
client.patch(f"/purchase-invoices/{last_p['id']}/approve", headers=fin_hdr)

r = client.post("/vehicle-stock/loading", headers=fin_hdr, json={
    "delivery_partner_id": dp_user["id"],
    "items": [{"product_id": fprod["id"], "loaded_qty": 10}]
})
check("load vehicle stock -> 201", r.status_code == 201, r.text)
loading_obj = r.json()

# Current Stock
r = client.get(f"/vehicle-stock/current/{dp_user['id']}", headers=fin_hdr)
check("current vehicle stock -> 200", r.status_code == 200 and r.json()["status"] == "active", r.text)

# Extra Load mid-day
r = client.post(f"/vehicle-stock/{loading_obj['id']}/extra-load", headers=fin_hdr, json={
    "items": [{"product_id": fprod["id"], "quantity": 5}]
})
check("extra load vehicle -> 200", r.status_code == 200 and r.json()["items"][0]["extra_qty"] == 5, r.text)

# End of Day returns
r = client.post(f"/vehicle-stock/{loading_obj['id']}/end-of-day", headers=fin_hdr, json={
    "items": [{"product_id": fprod["id"], "returned_qty": 3}]
})
check("end of day returns -> 200 closed", r.status_code == 200 and r.json()["status"] == "closed", r.text)

# 4. Deliveries
# Create order, assign delivery partner
o_del = client.post("/orders", headers=fin_hdr, json={
    "customer_id": fcust["id"],
    "items": [{"product_id": fprod["id"], "quantity": 1, "unit_price": 100}]
}).json()
client.patch(f"/orders/{o_del['id']}/approve", headers=fin_hdr)
client.patch(f"/orders/{o_del['id']}/assign-delivery-partner", headers=fin_hdr, json={"delivery_partner_id": dp_user["id"]})

# GET assigned deliveries
r = client.get("/deliveries/assigned", headers=dp_hdr)
check("list assigned deliveries -> 200", r.status_code == 200 and len(r.json()) >= 1, r.text)

# GET delivery details
r = client.get(f"/deliveries/{o_del['id']}", headers=dp_hdr)
check("get delivery details -> 200", r.status_code == 200 and r.json()["customer"]["name"] == "Regular Buyer", r.text)

# Update delivery status
r = client.patch(f"/deliveries/{o_del['id']}/status", headers=dp_hdr, json={"status": "Delivered"})
check("update delivery status to Delivered -> 200", r.status_code == 200 and r.json()["order_status"] == "delivered", r.text)

# Download delivery receipt
r = client.get(f"/deliveries/{o_del['id']}/receipt", headers=dp_hdr)
check("GET delivery receipt PDF -> 200", r.status_code == 200 and r.headers.get("content-type") == "application/pdf", r.headers)

# 5. Notifications
r = client.get("/notifications", headers=fin_hdr)
check("list notifications -> 200", r.status_code == 200, r.text)
notifs = r.json()
if notifs:
    notif_id = notifs[0]["id"]
    r = client.get("/notifications/unread-count", headers=fin_hdr)
    check("GET notifications unread-count", r.status_code == 200 and "unread" in r.json(), r.text)
    r = client.patch(f"/notifications/{notif_id}/read", headers=fin_hdr)
    check("PATCH read single notification -> 200", r.status_code == 200 and r.json()["is_read"] is True, r.text)
    r = client.patch("/notifications/read-all", headers=fin_hdr)
    check("PATCH read all notifications -> 200", r.status_code == 200, r.text)

# 6. Configurable Fields Settings by Admin
r = client.get("/organizations/settings/fields", headers=fin_hdr)
check("GET field settings -> 200", r.status_code == 200, r.text)
field_data = r.json()
check("field settings defaults company.date_of_incorporation to False", field_data["field_settings"]["company"]["date_of_incorporation"] is False)
check("available_fields metadata has customer optional fields", "gst_number" in field_data["available_fields"]["customer"]["optional"])

# Update field settings
r = client.put("/organizations/settings/fields", headers=fin_hdr, json={
    "field_settings": {
        "company": {"date_of_incorporation": True},
        "customer": {"gst_number": True, "phone": True}
    }
})
check("PUT field settings -> 200", r.status_code == 200, r.text)
updated_data = r.json()
check("updated field settings reflects company.date_of_incorporation as True", updated_data["field_settings"]["company"]["date_of_incorporation"] is True)
check("updated field settings reflects customer.gst_number as True", updated_data["field_settings"]["customer"]["gst_number"] is True)
check("updated field settings reflects customer.email as False (default)", updated_data["field_settings"]["customer"]["email"] is False)

# Fetch again to verify persistence
r = client.get("/organizations/settings/fields", headers=fin_hdr)
check("persisted GET field settings returns correct values", r.json()["field_settings"]["customer"]["gst_number"] is True)

# Test updating new Company Master fields
r = client.put("/organizations/settings", headers=fin_hdr, json={
    "legal_name": "Finance Legal Private Limited",
    "website": "https://financeco.com",
    "auth_person_name": "Mr. Vikram Patel",
    "auth_person_email": "vikram@financeco.com",
    "currency": "INR"
})
check("update Company Settings with extended fields -> 200", r.status_code == 200 and r.json()["legal_name"] == "Finance Legal Private Limited" and r.json()["website"] == "https://financeco.com" and r.json()["auth_person_name"] == "Mr. Vikram Patel" and r.json()["currency"] == "INR", r.text)

# Test website validation rule (must start with https://)
r = client.put("/organizations/settings", headers=fin_hdr, json={
    "website": "http://invalid-website.com"
})
check("website validation error -> 422", r.status_code == 422, r.text)

# Test email validation rule
r = client.put("/organizations/settings", headers=fin_hdr, json={
    "email": "invalidemail"
})
check("email validation error -> 422", r.status_code == 422, r.text)

# Generic file upload uploader test
import io
r = client.post("/organizations/settings/upload-file", headers=fin_hdr, files={"file": ("certificate.pdf", io.BytesIO(b"%PDF-1.4 ..."), "application/pdf")})
check("generic upload PDF file -> 200", r.status_code == 200 and is_file_url(r.json()["url"]), r.text[:200])

print("\n== Company Master: Status field ==")
r = client.get("/organizations/settings", headers=fin_hdr)
check("company_status defaults to active", r.json()["company_status"] == "active", r.text)
check("subscription_status exposed read-only", r.json()["subscription_status"] == "trial", r.text)

r = client.put("/organizations/settings", headers=fin_hdr, json={"company_status": "inactive"})
check("set company_status=inactive -> 200", r.status_code == 200 and r.json()["company_status"] == "inactive", r.text)
check("subscription status untouched by the toggle", r.json()["subscription_status"] == "trial", r.text)

# The sheet calls the field plain "Status", and it's a toggle/dropdown — accept both
# the alias and the capitalised label the UI shows.
r = client.put("/organizations/settings", headers=fin_hdr, json={"status": "Active"})
check("alias 'status' -> company_status", r.status_code == 200 and r.json()["company_status"] == "active", r.text)
check("'status' cannot move the subscription state", r.json()["subscription_status"] == "trial", r.text)
check("GET /organizations/me still reports trial",
      client.get("/organizations/me", headers=fin_hdr).json()["status"] == "trial")

r = client.put("/organizations/settings", headers=fin_hdr, json={"status": "suspended"})
check("subscription value rejected as a company status -> 422", r.status_code == 422, r.text)

print("\n== Company Master: Other Business Documents (multi-file) ==")
check("other documents start empty",
      client.get("/organizations/settings/documents/other", headers=fin_hdr).json() == [])

r = client.post("/organizations/settings/documents/other", headers=fin_hdr, files=[
    ("files", ("license.pdf", io.BytesIO(b"%PDF-1.4 license"), "application/pdf")),
    ("files", ("noc.png", io.BytesIO(png), "image/png")),
])
check("upload 2 documents at once -> 201", r.status_code == 201 and len(r.json()) == 2, r.text)
docs = r.json()
check("document keeps its filename", docs[0]["name"] == "license.pdf", docs)
check("document stored as a file URL", is_file_url(docs[0]["url"]), str(docs)[:200])
check("document records its size", docs[0]["size"] > 0, docs)

# A second upload appends — it must not wipe the first batch.
r = client.post("/organizations/settings/documents/other", headers=fin_hdr, files=[
    ("files", ("affidavit.pdf", io.BytesIO(b"%PDF-1.4 affidavit"), "application/pdf")),
])
check("second upload appends -> 3 documents", r.status_code == 201 and len(r.json()) == 3, r.text)
check("documents have unique ids", len({d["id"] for d in r.json()}) == 3, r.text)

settings = client.get("/organizations/settings", headers=fin_hdr).json()
check("documents visible on the company profile", len(settings["doc_other_files"]) == 3, settings["doc_other_files"])
check("doc_other_url mirrors the first document", settings["doc_other_url"] == docs[0]["url"])

# A bad file in the batch must not half-write the profile.
r = client.post("/organizations/settings/documents/other", headers=fin_hdr, files=[
    ("files", ("ok.pdf", io.BytesIO(b"%PDF-1.4 ok"), "application/pdf")),
    ("files", ("virus.exe", io.BytesIO(b"MZ"), "application/x-msdownload")),
])
check("unsupported format in batch -> 400", r.status_code == 400, r.text)
check("rejected batch left the list untouched",
      len(client.get("/organizations/settings/documents/other", headers=fin_hdr).json()) == 3)

r = client.post("/organizations/settings/documents/other", headers=fin_hdr, files=[
    # Document cap is 10 MB (Company Master sheet), so 11 MB must be refused.
    ("files", ("huge.pdf", io.BytesIO(b"x" * (11 * 1024 * 1024)), "application/pdf")),
])
check("oversized document -> 413", r.status_code == 413, r.text[:200])

doc_id = client.get("/organizations/settings/documents/other", headers=fin_hdr).json()[1]["id"]
r = client.delete(f"/organizations/settings/documents/other/{doc_id}", headers=fin_hdr)
check("delete one document -> 200 with 2 left", r.status_code == 200 and len(r.json()) == 2, r.text)
check("the deleted document is gone", all(d["id"] != doc_id for d in r.json()), r.text)
check("delete unknown document -> 404",
      client.delete("/organizations/settings/documents/other/nope", headers=fin_hdr).status_code == 404)

check("staff blocked from other documents -> 403",
      client.get("/organizations/settings/documents/other", headers=st_hdr).status_code == 403)

check("clear all documents -> 204",
      client.delete("/organizations/settings/documents/other", headers=fin_hdr).status_code == 204)
cleared = client.get("/organizations/settings", headers=fin_hdr).json()
check("list empty after clear", cleared["doc_other_files"] == [], cleared["doc_other_files"])
check("doc_other_url cleared with the list", cleared["doc_other_url"] is None, cleared["doc_other_url"])

print("\n== Single business document slots ==")
_SLOT_COLUMN = {
    "gst_certificate": "doc_gst_url",
    "pan_card": "doc_pan_url",
    "certificate_of_incorporation": "doc_coi_url",
    "trade_license": "doc_trade_license_url",
    "msme_certificate": "doc_msme_url",
    "fssai_license": "doc_fssai_url",
}
for slot, column in _SLOT_COLUMN.items():
    r = client.post(f"/organizations/settings/documents/{slot}", headers=fin_hdr,
                    files={"file": (f"{slot}.pdf", io.BytesIO(b"%PDF-1.4 doc"), "application/pdf")})
    check(f"upload {slot} -> {column}",
          r.status_code == 200 and is_file_url(r.json()[column]), r.text[:200])
settings_now = client.get("/organizations/settings", headers=fin_hdr).json()
check("every single-document slot is filled and independent",
      all(is_file_url(settings_now[c]) for c in _SLOT_COLUMN.values()), settings_now.get("doc_gst_url"))
check("'other' still routes to the multi-file slot, not the enum",
      client.get("/organizations/settings/documents/other", headers=fin_hdr).status_code == 200)
check("multi-file upload still appends to 'other'",
      len(client.post("/organizations/settings/documents/other", headers=fin_hdr, files=[
          ("files", ("a.pdf", io.BytesIO(b"%PDF a"), "application/pdf")),
          ("files", ("b.pdf", io.BytesIO(b"%PDF b"), "application/pdf"))]).json()) == 2)
check("unknown document slot -> 422",
      client.post("/organizations/settings/documents/not_a_slot", headers=fin_hdr,
                  files={"file": ("x.pdf", io.BytesIO(b"%PDF"), "application/pdf")}).status_code == 422)
check("single-document slot rejects an unsupported format -> 400",
      client.post("/organizations/settings/documents/pan_card", headers=fin_hdr,
                  files={"file": ("x.txt", io.BytesIO(b"hi"), "text/plain")}).status_code == 400)
check("staff cannot upload business documents -> 403",
      client.post("/organizations/settings/documents/gst_certificate", headers=st_hdr,
                  files={"file": ("x.pdf", io.BytesIO(b"%PDF"), "application/pdf")}).status_code == 403)
r = client.delete("/organizations/settings/documents/gst_certificate", headers=fin_hdr)
check("clear a single-document slot",
      r.status_code == 200 and r.json()["doc_gst_url"] is None, r.text[:200])
check("clearing one slot leaves the others",
      is_file_url(client.get("/organizations/settings", headers=fin_hdr).json()["doc_pan_url"]))
client.delete("/organizations/settings/documents/other", headers=fin_hdr)

print("\n== Auto-numbers, embedded refs, list/detail split ==")
import re as _re  # noqa: E402

_ycust = client.post("/customers", headers=fin_hdr,
                     json={"name": "Auto Number Co", "phone": "9800001111"}).json()
check("customer gets an auto CUST-YYYY-#### id",
      bool(_re.fullmatch(r"CUST-\d{4}-\d+", _ycust.get("customer_id") or "")), _ycust.get("customer_id"))
_yprod = client.post("/products", headers=fin_hdr,
                     json={"name": "Auto Number Widget", "price": 100, "total_inventory": 50}).json()
check("product gets an auto PROD id",
      bool(_re.fullmatch(r"PROD-\d{4}-\d+", _yprod.get("product_id") or "")), _yprod.get("product_id"))

_ncust = client.post("/customers", headers=fin_hdr,
                     json={"name": "Doomed Co", "phone": "9800002222"}).json()
_doomed = _ncust["customer_id"]
client.delete(f"/customers/{_ncust['id']}", headers=fin_hdr)
_after = client.post("/customers", headers=fin_hdr,
                     json={"name": "After Delete", "phone": "9800003333"}).json()
check("deleting a record never frees its number for reuse",
      _after["customer_id"] != _doomed
      and int(_after["customer_id"].split("-")[-1]) > int(_doomed.split("-")[-1]),
      f"deleted {_doomed}, next {_after['customer_id']}")

_yquote = client.post("/quotations", headers=fin_hdr, json={
    "customer_id": _ycust["id"],
    "items": [{"product_id": _yprod["id"], "quantity": 2, "unit_price": 100}]})
check("quotation number auto-generated in QT-YYYY-#### form",
      _yquote.status_code == 201
      and bool(_re.fullmatch(r"QT-\d{4}-\d+", _yquote.json().get("quotation_number") or "")),
      _yquote.text[:250])
check("a client-supplied quotation number is still honoured",
      client.post("/quotations", headers=fin_hdr, json={
          "quotation_number": "QT-MANUAL-9", "customer_id": _ycust["id"],
          "items": [{"product_id": _yprod["id"], "quantity": 1, "unit_price": 10}]}
      ).json()["quotation_number"] == "QT-MANUAL-9")
_ylead = client.post("/leads", headers=fin_hdr,
                     json={"customer_id": _ycust["id"], "mobile_number": "9800004444"})
check("lead gets an auto LEAD id",
      _ylead.status_code == 201 and bool(_re.fullmatch(r"LEAD-\d{4}-\d+", _ylead.json().get("lead_id") or "")),
      _ylead.text[:200])

_ycat = client.post("/categories", headers=fin_hdr, json={"name": "Auto Ref Category"}).json()
_ysup = client.post("/suppliers", headers=fin_hdr, json={"name": "Auto Ref Supplier"}).json()
_yp2 = client.post("/products", headers=fin_hdr, json={
    "name": "Linked Widget", "price": 20, "total_inventory": 10,
    "category_id": _ycat["id"], "preferred_supplier_id": _ysup["id"]}).json()
check("product resolves category + supplier from their ids",
      _yp2["category"]["name"] == "Auto Ref Category"
      and _yp2["supplier"]["name"] == "Auto Ref Supplier", (_yp2.get("category"), _yp2.get("supplier")))
check("product list carries the resolved refs",
      next(x for x in client.get("/products", headers=fin_hdr).json()
           if x["id"] == _yp2["id"])["supplier"]["name"] == "Auto Ref Supplier")
check("a product with no category/supplier reports null, not an error",
      client.get(f"/products/{_yprod['id']}", headers=fin_hdr).json()["category"] is None)

_yinv = client.post("/invoices", headers=fin_hdr, json={
    "customer_id": _ycust["id"],
    "items": [{"product_id": _yprod["id"], "quantity": 1, "unit_price": 100}]}).json()
client.post(f"/customers/{_ycust['id']}/payments", headers=fin_hdr,
            json={"amount": 40, "invoice_id": _yinv["id"]})
_yhist = client.get(f"/customers/{_ycust['id']}/payments", headers=fin_hdr).json()
check("payment history resolves the invoice it settled",
      _yhist[0]["invoice"]["invoice_number"] == _yinv["invoice_number"], _yhist[0].get("invoice"))

_qlist = client.get("/quotations", headers=fin_hdr).json()
_qrow = next(x for x in _qlist if x["id"] == _yquote.json()["id"])
check("quotation list shows what the table needs",
      _qrow["customer"]["name"] == "Auto Number Co" and _qrow["status"] == "draft"
      and _qrow["total"] == 200 and _qrow["item_count"] == 1, _qrow)
check("quotation list omits the heavy detail",
      "items" not in _qrow and "terms_conditions" not in _qrow, list(_qrow))
_qdetail = client.get(f"/quotations/{_yquote.json()['id']}", headers=fin_hdr).json()
check("quotation detail carries items and the sheet's extra fields",
      len(_qdetail["items"]) == 1 and all(
          k in _qdetail for k in ("shipping_address", "payment_terms", "delivery_terms",
                                  "notes", "terms_conditions", "status", "total")), sorted(_qdetail))
check("quotation detail total matches the list", _qdetail["total"] == _qrow["total"])

print("\n== Generic upload endpoint (no record id needed) ==")
_g = client.post("/files/upload", headers=fin_hdr,
                 files={"file": ("profile.jpg", io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"0" * 64), "image/jpeg")})
check("POST /files/upload -> 201", _g.status_code == 201, _g.text[:200])
_gj = _g.json()
check("returns a usable file URL", is_file_url(_gj["url"]), _gj.get("url"))
check("returns filename / type / size",
      _gj["filename"] == "profile.jpg" and _gj["content_type"] == "image/jpeg" and _gj["size"] == 72, _gj)
check("that URL serves the file", client.get("/files/" + _gj["file_id"]).status_code == 200)
check("the URL can be sent straight into a record",
      client.post("/products", headers=fin_hdr,
                  json={"name": "Generic Upload Product", "price": 5,
                        "cover_image": _gj["url"]}).json()["cover_image"] == _gj["url"])
check("generic upload accepts any type",
      client.post("/files/upload", headers=fin_hdr,
                  files={"file": ("a.csv", io.BytesIO(b"a,b"), "text/csv")}).status_code == 201)
check("generic upload rejects over 10 MB",
      client.post("/files/upload", headers=fin_hdr,
                  files={"file": ("big.bin", io.BytesIO(b"x" * (11 * 1024 * 1024)),
                                  "application/octet-stream")}).status_code == 413)
check("generic upload needs a token",
      client.post("/files/upload",
                  files={"file": ("x.png", io.BytesIO(b"x"), "image/png")}).status_code in (401, 403))

print("\n== Fetch by the human-facing code, not just the UUID ==")
_lcust = client.post("/customers", headers=fin_hdr,
                     json={"name": "Code Lookup Co", "phone": "9800005555"}).json()
_lprod = client.post("/products", headers=fin_hdr, json={
    "name": "Code Lookup Widget", "price": 80, "total_inventory": 20, "sku": "CL-SKU-1"}).json()
_lquote = client.post("/quotations", headers=fin_hdr, json={
    "customer_id": _lcust["id"],
    "items": [{"product_id": _lprod["id"], "quantity": 1, "unit_price": 80}]}).json()
_linv = client.post("/invoices", headers=fin_hdr, json={
    "customer_id": _lcust["id"],
    "items": [{"product_id": _lprod["id"], "quantity": 1, "unit_price": 80}]}).json()

for _label, _path, _rec, _code in [
    ("customer", "/customers", _lcust, _lcust["customer_id"]),
    ("product", "/products", _lprod, _lprod["product_id"]),
    ("quotation", "/quotations", _lquote, _lquote["quotation_number"]),
    ("invoice", "/invoices", _linv, _linv["invoice_number"]),
]:
    check(f"{_label} still fetches by UUID",
          client.get(f"{_path}/{_rec['id']}", headers=fin_hdr).status_code == 200)
    _r = client.get(f"{_path}/{_code}", headers=fin_hdr)
    check(f"{_label} fetches by its code ({_code})",
          _r.status_code == 200 and _r.json()["id"] == _rec["id"], f"{_r.status_code} {_r.text[:150]}")

check("code lookup is case-insensitive",
      client.get(f"/customers/{_lcust['customer_id'].lower()}", headers=fin_hdr).status_code == 200)
check("product also resolves by sku",
      client.get("/products/CL-SKU-1", headers=fin_hdr).json()["id"] == _lprod["id"])
check("unknown code still 404s",
      client.get("/customers/CUST-1999-9999", headers=fin_hdr).status_code == 404)
check("garbage id 404s rather than 500",
      client.get("/products/!!!", headers=fin_hdr).status_code == 404)
check("customer search matches the code",
      [c["id"] for c in client.get(f"/customers?search={_lcust['customer_id']}",
                                   headers=fin_hdr).json()] == [_lcust["id"]])
check("product search matches the code",
      [p["id"] for p in client.get(f"/products?search={_lprod['product_id']}",
                                   headers=fin_hdr).json()] == [_lprod["id"]])
check("another firm never reaches our record by code",
      client.get(f"/customers/{_lcust['customer_id']}", headers=min_hdr).status_code == 404
      or client.get(f"/customers/{_lcust['customer_id']}",
                    headers=min_hdr).json()["id"] != _lcust["id"])

print("\n== Company Master: options, validation, branches, config ==")
r = client.get("/organizations/settings/options", headers=fin_hdr)
check("GET /settings/options -> 200", r.status_code == 200, r.text[:200])
copts = r.json()
check("options: business types from the sheet",
      copts["business_types"][:6] == ["Private Limited", "Public Limited", "LLP", "Partnership",
                                      "Proprietorship", "NGO"], copts["business_types"][:6])
check("options: IANA time zones", "Asia/Kolkata" in copts["time_zones"] and len(copts["time_zones"]) > 100)
check("options: currencies / languages / banks",
      "INR" in copts["currencies"] and "Hindi" in copts["languages"]
      and "HDFC Bank" in copts["bank_names"])
check("options: countries and states", "India" in copts["countries"] and "Maharashtra" in copts["states"])
check("staff cannot read company options",
      client.get("/organizations/settings/options", headers=st_hdr).status_code == 403)

for _field, _bad, _label in [
    ("gstin_pan", "BADGST", "GSTIN/PAN"), ("bank_ifsc", "HD1", "IFSC"),
    ("pin_code", "12", "PIN"), ("primary_mobile", "not-a-phone", "phone"),
    ("name", "N" * 101, "name > 100"), ("legal_name", "L" * 151, "legal name > 150"),
    ("description", "D" * 501, "description > 500"), ("cin_number", "C" * 31, "CIN > 30"),
]:
    check(f"settings rejects invalid {_label}",
          client.put("/organizations/settings", headers=fin_hdr,
                     json={_field: _bad}).status_code == 422)
r = client.put("/organizations/settings", headers=fin_hdr, json={
    "gstin_pan": "27aapfu0939f1zv", "bank_ifsc": "hdfc0001234", "pin_code": "411001",
    "primary_mobile": "+91 98765 43210"})
check("valid values accepted and upper-cased",
      r.status_code == 200 and r.json()["gstin_pan"] == "27AAPFU0939F1ZV"
      and r.json()["bank_ifsc"] == "HDFC0001234", r.text[:250])
check("bare domain upgraded to https://",
      client.put("/organizations/settings", headers=fin_hdr,
                 json={"website": "acme.com"}).json()["website"] == "https://acme.com")
check("http:// website rejected",
      client.put("/organizations/settings", headers=fin_hdr,
                 json={"website": "http://acme.com"}).status_code == 422)

for _sheet, _api, _val in [
    ("pin_zip_code", "pin_code", "400001"), ("company_logo", "logo_url", "data:image/png;base64,AA"),
    ("authorized_signature", "signature_url", "data:image/png;base64,BB"),
    ("google_pay_phonepe_paytm_qr_code", "payment_qr_url", "data:image/png;base64,CC"),
    ("time_zone", "timezone", "Asia/Kolkata"),
    ("owner_director_name", "auth_person_name", "Sushil Shinde"),
    ("designation", "auth_person_designation", "Director"),
    ("mobile_number", "auth_person_mobile", "+919812345678"),
]:
    check(f"sheet name '{_sheet}' accepted for {_api}",
          client.put("/organizations/settings", headers=fin_hdr,
                     json={_sheet: _val}).json()[_api] == _val)

r = client.put("/organizations/settings", headers=fin_hdr, json={"branch_addresses": [
    {"label": "Pune Warehouse", "address": "Plot 12, MIDC", "city": "Pune", "pin_code": "411018"},
    {"label": "Delhi Office", "address": "22 Nehru Place", "city": "New Delhi"}]})
check("repeatable branch addresses -> 200", r.status_code == 200 and len(r.json()["branch_addresses"]) == 2,
      r.text[:250])
check("each branch gets an id", all(b["id"] for b in r.json()["branch_addresses"]))
check("legacy branch_address mirrors the first", r.json()["branch_address"] == "Plot 12, MIDC")
check("branch without an address -> 422",
      client.put("/organizations/settings", headers=fin_hdr,
                 json={"branch_addresses": [{"label": "No address"}]}).status_code == 422)

_cfg = {"numbering_series": "INV", "prefix": "INV-", "next_number": 1001, "footer": "Thank you",
        "terms": "Payable in 30 days", "logo_placement": "left", "signature_placement": "right"}
check("invoice_settings accepts and returns an object",
      client.put("/organizations/settings", headers=fin_hdr,
                 json={"invoice_settings": _cfg}).json()["invoice_settings"] == _cfg)
check("tax_configuration accepts an object",
      client.put("/organizations/settings", headers=fin_hdr,
                 json={"tax_configuration": {"regime": "gst", "default_gst_rate": 18}}
                 ).json()["tax_configuration"]["regime"] == "gst")
check("legacy plain-string config still works",
      client.put("/organizations/settings", headers=fin_hdr,
                 json={"invoice_settings": "legacy string"}).json()["invoice_settings"] == "legacy string")

r = client.get("/organizations/settings/completeness", headers=fin_hdr)
check("GET /settings/completeness -> 200", r.status_code == 200, r.text[:200])
comp = r.json()
check("completeness covers the sheet's 24 mandatory fields", comp["total"] == 24, comp["total"])
check("filled + missing add up", comp["filled"] + len(comp["missing"]) == comp["total"], comp)
check("partial update of one field still allowed",
      client.put("/organizations/settings", headers=fin_hdr,
                 json={"city": "Mumbai"}).json()["city"] == "Mumbai")

print("\n== HSN codes and payment allocation ==")
hsn_prod = client.post("/products", headers=fin_hdr, json={
    "name": "HSN Test Pipe", "price": 500, "hsn_code": "7306", "total_inventory": 50}).json()
check("product stores hsn_code", hsn_prod.get("hsn_code") == "7306", hsn_prod.get("hsn_code"))
hsn_cust = client.post("/customers", headers=fin_hdr, json={
    "name": "HSN Traders", "phone": "9800000123"}).json()
r = client.post("/invoices", headers=fin_hdr, json={
    "customer_id": hsn_cust["id"],
    "items": [{"product_id": hsn_prod["id"], "quantity": 2, "unit_price": 500, "tax": 180}]})
check("direct invoice -> 201", r.status_code == 201, r.text[:300])
hsn_inv = r.json()
check("invoice line carries the HSN code", hsn_inv["items"][0]["hsn_code"] == "7306", hsn_inv["items"][0])
check("invoice PDF still renders with the HSN column",
      client.get(f"/invoices/{hsn_inv['id']}/pdf", headers=fin_hdr).content[:4] == b"%PDF")

hsn_order = client.post("/orders", headers=fin_hdr, json={
    "customer_id": hsn_cust["id"],
    "items": [{"product_id": hsn_prod["id"], "quantity": 1, "unit_price": 500}]}).json()
client.patch(f"/orders/{hsn_order['id']}/approve", headers=fin_hdr)
r = client.post(f"/orders/{hsn_order['id']}/invoice", headers=fin_hdr)
check("POST /orders/{id}/invoice -> 201", r.status_code == 201, f"{r.status_code} {r.text[:250]}")
check("order-generated invoice carries the HSN code",
      r.json()["items"][0]["hsn_code"] == "7306", r.json()["items"][0])

r = client.post(f"/customers/{hsn_cust['id']}/payments", headers=fin_hdr,
                json={"amount": 500, "invoice_id": hsn_inv["id"], "payment_mode": "upi"})
check("payment against an invoice -> 201", r.status_code == 201, r.text[:250])
paid = client.get(f"/invoices/{hsn_inv['id']}", headers=fin_hdr).json()
check("part payment marks the invoice partial",
      paid["status"] == "partial" and paid["amount_paid"] == 500, f"{paid['status']} {paid['amount_paid']}")
client.post(f"/customers/{hsn_cust['id']}/payments", headers=fin_hdr,
            json={"amount": 680, "invoice_id": hsn_inv["id"]})
check("settling in full marks it paid",
      client.get(f"/invoices/{hsn_inv['id']}", headers=fin_hdr).json()["status"] == "paid")
hsn_pays = client.get(f"/customers/{hsn_cust['id']}/payments", headers=fin_hdr).json()
check("payment history records invoice_id",
      all(p["invoice_id"] == hsn_inv["id"] for p in hsn_pays), hsn_pays)
client.delete(f"/customers/{hsn_cust['id']}/payments/{hsn_pays[0]['id']}", headers=fin_hdr)
check("voiding a payment reverses the invoice",
      client.get(f"/invoices/{hsn_inv['id']}", headers=fin_hdr).json()["status"] != "paid")
check("invoice belonging to another customer -> 400",
      client.post(f"/customers/{fcust['id']}/payments", headers=fin_hdr,
                  json={"amount": 100, "invoice_id": hsn_inv["id"]}).status_code == 400)
check("unknown invoice_id -> 404",
      client.post(f"/customers/{hsn_cust['id']}/payments", headers=fin_hdr,
                  json={"amount": 100, "invoice_id": "nope"}).status_code == 404)
check("advance payment without invoice_id still works",
      client.post(f"/customers/{hsn_cust['id']}/payments", headers=fin_hdr,
                  json={"amount": 250}).status_code == 201)

print("\n== Leads CRUD ==")
lead_payload = {
    "lead_id": "L-12345",
    "lead_source": "Website",
    "customer_id": fcust["id"],
    "mobile_number": "9876543210",
    "lead_status": "new"
}
r = client.post("/leads", headers=fin_hdr, json=lead_payload)
check("POST /leads -> 201", r.status_code == 201, r.text)
lead_obj = r.json()
check("lead_id matches", lead_obj["lead_id"] == "L-12345")
check("lead customer name", lead_obj["customer"]["name"] == "Regular Buyer")

r = client.get("/leads", headers=fin_hdr)
check("GET /leads list -> 200", r.status_code == 200 and len(r.json()) >= 1, r.text)

r = client.get(f"/leads/{lead_obj['id']}", headers=fin_hdr)
check("GET /leads/{id} detail -> 200", r.status_code == 200, r.text)

r = client.put(f"/leads/{lead_obj['id']}", headers=fin_hdr, json={"lead_status": "contacted"})
check("PUT /leads/{id} edit -> 200", r.status_code == 200 and r.json()["lead_status"] == "contacted", r.text)

r = client.delete(f"/leads/{lead_obj['id']}", headers=fin_hdr)
check("DELETE /leads/{id} -> 204", r.status_code == 204)
check("GET deleted lead -> 404", client.get(f"/leads/{lead_obj['id']}", headers=fin_hdr).status_code == 404)


print("\n== Quotations CRUD ==")
quot_payload = {
    "quotation_number": "Q-2026-001",
    "customer_id": fcust["id"],
    "currency": "INR",
    "items": [{"product_id": fprod["id"], "quantity": 10, "uom": "boxes", "unit_price": 95}]
}
r = client.post("/quotations", headers=fin_hdr, json=quot_payload)
check("POST /quotations -> 201", r.status_code == 201, r.text)
quot_obj = r.json()
check("quotation_number matches", quot_obj["quotation_number"] == "Q-2026-001")
check("quotation item size is 1", len(quot_obj["items"]) == 1)

r = client.get("/quotations", headers=fin_hdr)
check("GET /quotations list -> 200", r.status_code == 200 and len(r.json()) >= 1, r.text)

r = client.get(f"/quotations/{quot_obj['id']}", headers=fin_hdr)
check("GET /quotations/{id} detail -> 200", r.status_code == 200, r.text)

r = client.delete(f"/quotations/{quot_obj['id']}", headers=fin_hdr)
check("DELETE /quotations/{id} -> 204", r.status_code == 204)


print("\n== Delivery Notes CRUD ==")
del_note_payload = {
    "delivery_note_number": "DN-2026-001",
    "customer_id": fcust["id"],
    "warehouse": "Main WH",
    "delivery_address": "Test street",
    "items": [{"product_id": fprod["id"], "delivered_quantity": 5}]
}
r = client.post("/deliveries/notes", headers=fin_hdr, json=del_note_payload)
check("POST /deliveries/notes -> 201", r.status_code == 201, r.text)
dn_obj = r.json()
check("delivery_note_number matches", dn_obj["delivery_note_number"] == "DN-2026-001")

r = client.get("/deliveries/notes", headers=fin_hdr)
check("GET /deliveries/notes list -> 200", r.status_code == 200 and len(r.json()) >= 1, r.text)

r = client.get(f"/deliveries/notes/{dn_obj['id']}", headers=fin_hdr)
check("GET /deliveries/notes/{id} detail -> 200", r.status_code == 200, r.text)


print("\n== Payment Receipts CRUD ==")
inv_ref = client.post("/invoices", headers=fin_hdr, json={
    "customer_id": fcust["id"],
    "items": [{"product_id": fprod["id"], "quantity": 1, "unit_price": 100}]
}).json()

receipt_payload = {
    "receipt_number": "REC-2026-001",
    "customer_id": fcust["id"],
    "invoice_reference_id": inv_ref["id"],
    "amount_received": 100,
    "payment_method": "UPI"
}
r = client.post("/payment-receipts", headers=fin_hdr, json=receipt_payload)
check("POST /payment-receipts -> 201", r.status_code == 201, r.text)
rec_obj = r.json()
check("receipt_number matches", rec_obj["receipt_number"] == "REC-2026-001")

r = client.get("/payment-receipts", headers=fin_hdr)
check("GET /payment-receipts list -> 200", r.status_code == 200 and len(r.json()) >= 1, r.text)

r = client.get(f"/payment-receipts/{rec_obj['id']}", headers=fin_hdr)
check("GET /payment-receipts/{id} detail -> 200", r.status_code == 200, r.text)


print("\n== Sales Returns / Credit Notes CRUD ==")
old_stock = client.get(f"/inventory/{fprod['id']}", headers=fin_hdr).json()["total_stock"]

sales_ret_payload = {
    "return_number": "RET-2026-001",
    "customer_id": fcust["id"],
    "invoice_reference_id": inv_ref["id"],
    "return_reason": "Defective product",
    "return_status": "Approved",
    "items": [{"product_id": fprod["id"], "quantity_returned": 2}]
}
r = client.post("/sales-returns", headers=fin_hdr, json=sales_ret_payload)
check("POST /sales-returns -> 201", r.status_code == 201, r.text)
ret_obj = r.json()
check("return_number matches", ret_obj["return_number"] == "RET-2026-001")

new_stock = client.get(f"/inventory/{fprod['id']}", headers=fin_hdr).json()["total_stock"]
check("sales return reverses stock", new_stock == old_stock + 2, f"Old: {old_stock}, New: {new_stock}")

r = client.get("/sales-returns", headers=fin_hdr)
check("GET /sales-returns list -> 200", r.status_code == 200 and len(r.json()) >= 1, r.text)

r = client.get(f"/sales-returns/{ret_obj['id']}", headers=fin_hdr)
check("GET /sales-returns/{id} detail -> 200", r.status_code == 200, r.text)

print("\n== Employee profile on users ==")
emp_email = f"emp_{uuid.uuid4().hex[:8]}@firm.com"
r = client.post("/users", headers=fin_hdr, json={
    "name": "Ravi Kumar", "email": emp_email, "username": f"emp_{uuid.uuid4().hex[:8]}",
    "password": "Staff@123", "role": "sales_officer",
    "employee_id": "EMP-9001", "first_name": "Ravi", "last_name": "Kumar",
    "designation": "Field Sales Executive", "employment_type": "Full Time",
    "date_of_joining": "2026-01-15T00:00:00Z", "employee_status": "On-Leave",
})
check("create employee with profile -> 201", r.status_code == 201, r.text)
emp = r.json()
check("employee_id stored", emp["employee_id"] == "EMP-9001", emp)
check("first/last name stored", emp["first_name"] == "Ravi" and emp["last_name"] == "Kumar", emp)
check("designation stored", emp["designation"] == "Field Sales Executive", emp)
check("employment_type normalized 'Full Time' -> full_time", emp["employment_type"] == "full_time", emp)
check("employee_status normalized 'On-Leave' -> on_leave", emp["employee_status"] == "on_leave", emp)
check("date_of_joining stored", (emp["date_of_joining"] or "").startswith("2026-01-15"), emp)

# employee_id is auto-assigned when omitted, and unique per firm
r = client.post("/users", headers=fin_hdr, json={
    "name": "Auto Coded", "email": f"auto_{uuid.uuid4().hex[:8]}@firm.com",
    "username": f"auto_{uuid.uuid4().hex[:8]}", "password": "Staff@123", "role": "accountant"})
auto_emp = r.json()
check("employee_id auto-assigned EMP-####",
      r.status_code == 201 and (auto_emp["employee_id"] or "").startswith("EMP-"), r.text)
check("employee_status defaults to active", auto_emp["employee_status"] == "active", auto_emp)
check("duplicate employee_id -> 409", client.post("/users", headers=fin_hdr, json={
    "name": "Dupe", "email": f"dup_{uuid.uuid4().hex[:8]}@firm.com",
    "username": f"dup_{uuid.uuid4().hex[:8]}", "password": "Staff@123", "role": "accountant",
    "employee_id": "EMP-9001"}).status_code == 409)
check("invalid employment_type -> 422", client.post("/users", headers=fin_hdr, json={
    "name": "Bad", "email": f"bad_{uuid.uuid4().hex[:8]}@firm.com",
    "username": f"bad_{uuid.uuid4().hex[:8]}", "password": "Staff@123", "role": "accountant",
    "employment_type": "freelance-ish"}).status_code == 422)

# Filters
check("filter by employee_status=on_leave",
      any(u["id"] == emp["id"] for u in client.get("/users", headers=fin_hdr, params={"employee_status": "on_leave"}).json()))
check("filter by employment_type=full_time",
      any(u["id"] == emp["id"] for u in client.get("/users", headers=fin_hdr, params={"employment_type": "full_time"}).json()))
check("filter by designation",
      [u["id"] for u in client.get("/users", headers=fin_hdr, params={"designation": "Field Sales Executive"}).json()] == [emp["id"]])
check("search by employee_id",
      any(u["id"] == emp["id"] for u in client.get("/users", headers=fin_hdr, params={"search": "EMP-9001"}).json()))
check("search by name",
      any(u["id"] == emp["id"] for u in client.get("/users", headers=fin_hdr, params={"search": "Ravi"}).json()))

# Edit the profile
r = client.patch(f"/users/{emp['id']}", headers=fin_hdr,
                 json={"designation": "Senior Sales Executive", "employee_status": "active", "phone": "9812345678"})
check("PATCH employee profile -> 200",
      r.status_code == 200 and r.json()["designation"] == "Senior Sales Executive"
      and r.json()["employee_status"] == "active" and r.json()["phone"] == "9812345678", r.text)
check("PATCH to a taken employee_id -> 409",
      client.patch(f"/users/{auto_emp['id']}", headers=fin_hdr, json={"employee_id": "EMP-9001"}).status_code == 409)
check("PATCH keeping own employee_id -> 200",
      client.patch(f"/users/{emp['id']}", headers=fin_hdr, json={"employee_id": "EMP-9001"}).status_code == 200)

# Options endpoint for the employee form
r = client.get("/users/meta/employee-options", headers=fin_hdr)
opts = r.json()
check("GET employee-options -> 200", r.status_code == 200, r.text)
check("options list employment types", "full_time" in opts["employment_types"] and "contract" in opts["employment_types"], opts)
check("options list employee statuses", "on_leave" in opts["employee_statuses"], opts)
check("options include designations in use", "Senior Sales Executive" in opts["designations"], opts)

# Identity proof upload
r = client.post(f"/users/{emp['id']}/identity-proof", headers=fin_hdr,
                files={"file": ("aadhaar.pdf", io.BytesIO(b"%PDF-1.4 id"), "application/pdf")})
check("upload identity proof -> file URL",
      r.status_code == 200 and is_file_url(r.json()["url"]), r.text[:200])
check("user detail reflects identity proof",
      is_file_url(client.get(f"/users/{emp['id']}", headers=fin_hdr).json()["identify_proofs"]))
check("identity proof rejects non-document -> 400",
      client.post(f"/users/{emp['id']}/identity-proof", headers=fin_hdr,
                  files={"file": ("x.txt", io.BytesIO(b"hi"), "text/plain")}).status_code == 400)
check("employee profile visible on /auth/me",
      "employee_id" in client.get("/auth/me", headers=fin_hdr).json()["user"])

print("\n== Employee profile: mandatory fields ==")
# These go through _raw_post so the central defaults above do not mask the check.
_min_staff = {"name": "Needs Fields", "password": "Staff@123", "confirm_password": "Staff@123",
              "role": "accountant"}
for field in ("first_name", "last_name", "phone", "designation", "employment_type",
              "date_of_joining", "employee_status", "identity_proof_type",
              "identity_proof_file", "status"):
    body = {**_STAFF_REQUIRED, **_min_staff,
            "email": f"req_{uuid.uuid4().hex[:8]}@firm.com", "username": f"req_{uuid.uuid4().hex[:8]}"}
    body.pop(field)
    check(f"POST /users without {field} -> 422",
          _raw_post("/users", headers=fin_hdr, json=body).status_code == 422)
check("mismatched confirm_password -> 422",
      _raw_post("/users", headers=fin_hdr, json={
          **_STAFF_REQUIRED, **_min_staff, "confirm_password": "Different@123",
          "email": f"cp_{uuid.uuid4().hex[:8]}@firm.com",
          "username": f"cp_{uuid.uuid4().hex[:8]}"}).status_code == 422)

print("\n== Employee profile: extended fields ==")
ext_email = f"ext_{uuid.uuid4().hex[:8]}@firm.com"
r = client.post("/users", headers=fin_hdr, json={
    "first_name": "Asha", "last_name": "Verma", "email": ext_email,
    "username": f"ext_{uuid.uuid4().hex[:8]}", "password": "Staff@123", "role": "accountant",
    "gender": "Female", "blood_group": "B+", "date_of_birth": "1996-03-04T00:00:00Z",
    "personal_email": "asha@example.com", "emergency_contact_name": "R Verma",
    "emergency_contact_number": "9800000000", "emergency_contact_relationship": "Father",
    "current_address": "12 MG Road", "city": "Pune", "state": "Maharashtra",
    "country": "India", "pin_zip_code": "411001",
    "work_location": "Pune Office", "shift": "Morning",
    "basic_salary": 42000, "bank_name": "SBI", "account_number": "9988776655",
    "ifsc_swift_code": "SBIN0001", "upi_id": "asha@upi",
    "skills": ["Tally", "GST"], "language": "en", "time_zone": "Asia/Kolkata",
})
check("create with the extended profile -> 201", r.status_code == 201, r.text)
ext = r.json()
check("name composed from first/last when omitted", ext["name"] == "Asha Verma", ext["name"])
check("address fields stored", ext["city"] == "Pune" and ext["pin_zip_code"] == "411001"
      and (ext["current_address"] or "").startswith("12 MG"), ext)
check("emergency contact stored", ext["emergency_contact_relationship"] == "Father", ext)
check("payroll fields stored", ext["basic_salary"] == 42000 and ext["upi_id"] == "asha@upi", ext)
check("skills stored as a list", ext["skills"] == ["Tally", "GST"], ext["skills"])
check("identify_proofs mirrors identity_proof_file",
      ext["identify_proofs"] == ext["identity_proof_file"], ext["identify_proofs"])

# Reporting manager
r = client.patch(f"/users/{ext['id']}", headers=fin_hdr, json={"reporting_manager_id": emp["id"]})
check("set reporting manager -> 200", r.status_code == 200
      and r.json()["reporting_manager_id"] == emp["id"], r.text)
check("self as reporting manager -> 400",
      client.patch(f"/users/{ext['id']}", headers=fin_hdr,
                   json={"reporting_manager_id": ext["id"]}).status_code == 400)
check("reporting loop -> 400",
      client.patch(f"/users/{emp['id']}", headers=fin_hdr,
                   json={"reporting_manager_id": ext["id"]}).status_code == 400)
check("filter by reporting_manager_id",
      [u["id"] for u in client.get("/users", headers=fin_hdr,
                                   params={"reporting_manager_id": emp["id"]}).json()] == [ext["id"]])

# Account status drives is_active
r = client.patch(f"/users/{ext['id']}/account-status", headers=fin_hdr, json={"status": "Suspended"})
check("account-status suspends and blocks login",
      r.status_code == 200 and r.json()["status"] == "suspended" and r.json()["is_active"] is False, r.text)
check("suspended user cannot log in",
      client.post("/auth/login", json={"email": ext_email, "password": "Staff@123"}).status_code == 403)
r = client.patch(f"/users/{ext['id']}", headers=fin_hdr, json={"status": "active"})
check("PATCH status back to active restores is_active", r.json()["is_active"] is True, r.text)
check("filter by status=active",
      any(u["id"] == ext["id"] for u in client.get("/users", headers=fin_hdr,
                                                   params={"status": "active"}).json()))

# Employee file slots and document collections
r = client.post(f"/users/{ext['id']}/files/profile_photo", headers=fin_hdr,
                files={"file": ("p.png", io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"0" * 20), "image/png")})
check("upload profile_photo -> data URL",
      r.status_code == 200 and is_file_url(r.json()["profile_photo"]), r.text[:200])
check("profile_photo rejects a PDF -> 400",
      client.post(f"/users/{ext['id']}/files/profile_photo", headers=fin_hdr,
                  files={"file": ("p.pdf", io.BytesIO(b"%PDF"), "application/pdf")}).status_code == 400)
r = client.post(f"/users/{ext['id']}/documents/experience_certificates", headers=fin_hdr,
                files=[("files", ("e1.pdf", io.BytesIO(b"%PDF-1.4 a"), "application/pdf")),
                       ("files", ("e2.pdf", io.BytesIO(b"%PDF-1.4 b"), "application/pdf"))])
check("upload 2 experience certificates -> 201", r.status_code == 201 and len(r.json()) == 2, r.text)
doc_id = r.json()[0]["id"]
check("documents append rather than replace",
      len(client.post(f"/users/{ext['id']}/documents/experience_certificates", headers=fin_hdr,
                      files=[("files", ("e3.pdf", io.BytesIO(b"%PDF"), "application/pdf"))]).json()) == 3)
check("delete one document -> 200",
      len(client.delete(f"/users/{ext['id']}/documents/experience_certificates/{doc_id}",
                        headers=fin_hdr).json()) == 2)
check("other collections untouched",
      client.get(f"/users/{ext['id']}", headers=fin_hdr).json()["educational_certificates"] == [])
check("clear a collection -> 204",
      client.delete(f"/users/{ext['id']}/documents/experience_certificates",
                    headers=fin_hdr).status_code == 204)

# Extended options
opts = client.get("/users/meta/employee-options", headers=fin_hdr).json()
check("options list account statuses",
      opts["account_statuses"] == ["active", "inactive", "suspended", "locked"], opts["account_statuses"])
check("options suggest genders / blood groups",
      "Female" in opts["genders"] and "B+" in opts["blood_groups"], opts)
check("options include work locations in use", "Pune Office" in opts["work_locations"], opts["work_locations"])
check("options list preset designations",
      opts["designations"][:6] == ["Admin", "Manager", "HR", "Sales", "Finance", "Employee"], opts["designations"])
check("options keep designations in use after the presets",
      "Senior Sales Executive" in opts["designations"], opts["designations"])
check("options serve countries / states / relationships",
      "India" in opts["countries"] and "India" in opts["nationalities"]
      and "Maharashtra" in opts["states"] and "Father" in opts["emergency_contact_relationships"], opts)

print("\n== Employee ID prefix (Company Settings) ==")
check("default employee_id_prefix is EMP-", opts["employee_id_prefix"] == "EMP-", opts["employee_id_prefix"])
r = client.put("/organizations/settings", headers=fin_hdr, json={"employee_id_prefix": "ACME-"})
check("admin can set the prefix -> 200",
      r.status_code == 200 and r.json().get("employee_id_prefix") == "ACME-", r.text[:300])
check("employee-options reports the new prefix",
      client.get("/users/meta/employee-options", headers=fin_hdr).json()["employee_id_prefix"] == "ACME-")
r = client.post("/users", headers=fin_hdr, json={
    "first_name": "Pre", "last_name": "Fixed", "email": f"pre_{uuid.uuid4().hex[:8]}@firm.com",
    "username": f"pre_{uuid.uuid4().hex[:8]}", "password": "Staff@123", "role": "accountant"})
check("new employee uses the firm's prefix",
      r.status_code == 201 and (r.json()["employee_id"] or "").startswith("ACME-"), r.text[:300])
client.put("/organizations/settings", headers=fin_hdr, json={"employee_id_prefix": "EMP-"})

print("\n== DELETE user ==")
check("delete own account -> 400",
      client.delete(f"/users/{client.get('/auth/me', headers=fin_hdr).json()['user']['id']}", headers=fin_hdr).status_code == 400)
check("cross-org delete -> 404",
      client.delete(f"/users/{emp['id']}", headers=min_hdr).status_code == 404)
check("staff cannot delete users -> 403", client.delete(f"/users/{emp['id']}", headers=st_hdr).status_code == 403)

r = client.delete(f"/users/{auto_emp['id']}", headers=fin_hdr)
check("DELETE /users/{id} -> 204", r.status_code == 204, r.text)
check("GET deleted user -> 404", client.get(f"/users/{auto_emp['id']}", headers=fin_hdr).status_code == 404)
check("deleted user gone from list",
      all(u["id"] != auto_emp["id"] for u in client.get("/users", headers=fin_hdr).json()))

# A delivery partner holding stock cannot be deleted until the loading is closed.
dp2_email = f"dp2_{uuid.uuid4().hex[:8]}@firm.com"
dp2 = client.post("/users", headers=fin_hdr, json={
    "name": "DP Two", "email": dp2_email, "username": f"dp2_{uuid.uuid4().hex[:8]}",
    "password": "Partner@123", "role": "delivery_partner"}).json()
client.post("/purchase-invoices", headers=fin_hdr, json={
    "invoice_number": f"PI-{uuid.uuid4().hex[:6]}", "supplier_id": fsup["id"],
    "items": [{"product_id": fprod["id"], "quantity": 20, "purchase_price": 50}]})
client.patch(f"/purchase-invoices/{client.get('/purchase-invoices', headers=fin_hdr).json()[0]['id']}/approve", headers=fin_hdr)
dp2_load = client.post("/vehicle-stock/loading", headers=fin_hdr, json={
    "delivery_partner_id": dp2["id"], "items": [{"product_id": fprod["id"], "loaded_qty": 4}]}).json()
check("delete partner with open loading -> 409",
      client.delete(f"/users/{dp2['id']}", headers=fin_hdr).status_code == 409)
client.post(f"/vehicle-stock/{dp2_load['id']}/end-of-day", headers=fin_hdr,
            json={"items": [{"product_id": fprod["id"], "returned_qty": 4}]})
check("delete partner after closing loading -> 204",
      client.delete(f"/users/{dp2['id']}", headers=fin_hdr).status_code == 204)

# Deleting a user keeps the business history, with the link nulled out.
check("order survives its delivery partner's deletion",
      client.get(f"/orders/{o_del['id']}", headers=fin_hdr).status_code == 200)
r = client.delete(f"/users/{dp_user['id']}", headers=fin_hdr)
check("delete the original delivery partner -> 204", r.status_code == 204, r.text)
o_after = client.get(f"/orders/{o_del['id']}", headers=fin_hdr)
check("order still readable after partner deleted", o_after.status_code == 200, o_after.text)
check("order's assigned_delivery_partner_id nulled",
      o_after.json().get("assigned_delivery_partner_id") is None, o_after.text)
check("deleted user can no longer log in -> 401",
      client.post("/auth/login", json={"email": dp_email, "password": "Partner@123"}).status_code == 401)

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

    # Old-style products table: missing the NOT NULL `inventory_tracking` column.
    # This is what broke GET /products on prod — the migration used to skip NOT
    # NULL columns, so every later SELECT hit "column does not exist".
    _c.execute(_text(
        "CREATE TABLE products (id VARCHAR(36) PRIMARY KEY, organization_id VARCHAR(36), "
        "name VARCHAR(200), price FLOAT)"))
    _c.execute(_text("INSERT INTO products (id, organization_id, name, price) VALUES ('p1','o1','Legacy Bottle',99)"))

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

    _pcols = {c["name"] for c in _inspect(_eng).get_columns("products")}
    check("migration added NOT NULL column products.inventory_tracking", "inventory_tracking" in _pcols, _pcols)
    check("migration added NOT NULL column products.total_inventory", "total_inventory" in _pcols, _pcols)
    with _eng.connect() as _c:
        _prow = _c.execute(_text("SELECT name, inventory_tracking, total_inventory FROM products WHERE id='p1'")).fetchone()
    check("existing product row preserved", _prow is not None and _prow[0] == "Legacy Bottle", _prow)
    check("NOT NULL backfill uses the model default (inventory_tracking=True)", bool(_prow[1]) is True, _prow)
    check("NOT NULL backfill uses the model default (total_inventory=0)", _prow[2] == 0, _prow)
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
