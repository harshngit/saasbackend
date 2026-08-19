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

# POST /users takes the sectioned employee body, and nothing in it is mandatory
# beyond the login email and password — which fields a form insists on is the
# frontend's business. Nearly every staff creation below is really testing roles /
# permissions / lifecycle rather than the HR form, so those calls stay in the older
# *flat* shape (which the API still folds into sections) with the profile fields
# filled in centrally instead of at ~30 call sites. A body that sets one of them
# keeps its own value, and `_raw_post` bypasses this for the checks that exercise
# the body itself.
_STAFF_FLAT_DEFAULTS = {
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
    if url == "/users" and isinstance(body, dict) and "login_security" not in body:
        body = {**_STAFF_FLAT_DEFAULTS, **body}
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
r = client.patch(f"/users/{staff_id}",
    headers={"Authorization": f"Bearer {admin_access}"},
    json={"system_preferences": {"account_status": "inactive"}})
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
r = client.patch(f"/users/{other_user_id}",
    headers={"Authorization": f"Bearer {admin_access}"},
    json={"system_preferences": {"account_status": "inactive"}})
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
cat = r.json()
check("catalog returns modules + actions",
      r.status_code == 200 and len(cat["modules"]) > 15 and len(cat["actions"]) == 7, r.text)
check("the catalog covers every module a router gates on",
      {"customers", "leads", "quotations", "sales_orders", "sales_returns", "payment_receipts",
       "deliveries", "invoices", "payments", "expenses", "purchases", "inventory", "products",
       "suppliers", "attendance", "reports", "vehicle_stock", "dashboard"}
      <= {m["key"] for m in cat["modules"]}, [m["key"] for m in cat["modules"]])

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
r = client.patch(f"/roles/{custom_id}", headers=roles_hdr, json={"permissions": {"customers": {"view": True}}})
check("update role permissions (full replace) -> 200",
      r.status_code == 200 and r.json()["permissions"] == {"customers": {"view": True, "create": False, "edit": False, "delete": False, "approve": False, "export": False, "download": False}}, r.text)
check("PUT is gone — PATCH is the update verb",
      client.put(f"/roles/{custom_id}", headers=roles_hdr, json={"name": "X"}).status_code == 405)
so_id = so["id"]
r = client.patch(f"/roles/{so_id}", headers=roles_hdr, json={"permissions": {"customers": {"view": True}}})
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
    "email": staff_reg.json()["contact_information"]["official_email"],
    "password": "Staff@123"}).json()["tokens"]["access_token"]
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
check("admin me full_access=true", me["full_access"] is True, me)
check("an admin's permissions come back fully granted, not empty",
      all(all(actions.values()) for actions in me["permissions"].values())
      and len(me["permissions"]) > 15, sorted(me["permissions"]))
check("an admin holds no org-scoped role and is never narrowed",
      me["role"] is None and me["data_scope"] == "all", me["role"])

# Get role ids
roles = client.get("/roles", headers=p2_hdr).json()
sales_role = next(r for r in roles if r["name"] == "Sales Officer")

# Create staff via role_id
st_email = f"p2s_{uuid.uuid4().hex[:6]}@f.com"
r = client.post("/users", headers=p2_hdr, json={
    "name": "Staffy", "email": st_email, "password": "Staff@123", "username": f"u_{uuid.uuid4().hex[:8]}", "role_id": sales_role["id"]})
check("create staff via role_id -> 201", r.status_code == 201, r.text)
staff = r.json()
_employment = staff["employment_information"]
check("staff system_role=staff", staff["system_role"] == "staff", staff)
check("staff role_id set", _employment["role_id"] == sales_role["id"], _employment)
check("staff role_detail has permissions",
      _employment["role_detail"]["permissions"].get("customers", {}).get("create") is True, _employment)
check("staff legacy role mapped (sales_officer) on the flat shape",
      next(u for u in client.get("/users", headers=p2_hdr).json()
           if u["id"] == staff["id"])["role"] == "sales_officer", staff)
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
r = client.patch(f"/users/{p2_staff_id}", headers=p2_hdr, json={
    "contact_information": {"mobile_number": "9000000000"}, "name": "Staffy Renamed"})
check("PATCH /users/{id} profile -> 200", r.status_code == 200
      and r.json()["contact_information"]["mobile_number"] == "9000000000"
      and r.json()["name"] == "Staffy Renamed", r.text)
deliv_role = next(rr for rr in roles if rr["name"] == "Delivery Partner")
# Reassignment is part of the one update endpoint — there is no /role route.
r = client.patch(f"/users/{p2_staff_id}", headers=p2_hdr, json={
    "employment_information": {"role_id": deliv_role["id"]}})
check("PATCH /users/{id} reassigns the role", r.status_code == 200
      and r.json()["employment_information"]["role_id"] == deliv_role["id"]
      and r.json()["employment_information"]["role_detail"]["name"] == "Delivery Partner", r.text)
check("the removed /role route is gone",
      client.patch(f"/users/{p2_staff_id}/role", headers=p2_hdr,
                   json={"role_id": deliv_role["id"]}).status_code == 404)
check("a role name works on PATCH too", client.patch(f"/users/{p2_staff_id}", headers=p2_hdr,
      json={"role": "Sales Officer"}).json()["employment_information"]["role_id"] == sales_role["id"])
check("an explicit null clears the role assignment",
      client.patch(f"/users/{p2_staff_id}", headers=p2_hdr, json={
          "employment_information": {"role_id": None}}).json()["employment_information"]["role_id"] is None)
client.patch(f"/users/{p2_staff_id}", headers=p2_hdr, json={
    "employment_information": {"role_id": deliv_role["id"]}})

# Cross-org: another admin can't fetch this firm's user
check("cross-org GET /users/{id} -> 404", client.get(f"/users/{p2_staff_id}", headers={"Authorization": f"Bearer {other['tokens']['access_token']}"}).status_code == 404)

# Delete role rules with assignment
# Sales Officer no longer has the staff (moved to Delivery Partner) but is default → 400
check("delete default role -> 400", client.delete(f"/roles/{sales_role['id']}", headers=p2_hdr).status_code == 400)
# Custom role with an assigned user → 400
cr = client.post("/roles", headers=p2_hdr, json={"name": "Temp Role", "permissions": {"customers": {"view": True}}}).json()
client.patch(f"/users/{p2_staff_id}", headers=p2_hdr, json={"employment_information": {"role_id": cr["id"]}})
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
check("custom-role staff carries the role name",
      r.json()["employment_information"]["role_detail"]["name"] == "HR", r.text)
check("custom-role staff has no legacy enum role on the flat shape",
      next(u for u in client.get("/users", headers=p2_hdr).json()
           if u["id"] == r.json()["id"])["role"] is None, r.text)

# By name — "hr" used to 422 against the fixed enum.
r = client.post("/users", headers=p2_hdr, json={
    "name": "Priya", "email": f"hr2_{uuid.uuid4().hex[:6]}@f.com",
    "username": f"u_{uuid.uuid4().hex[:8]}", "password": "Priya@12345", "role": "hr"})
check("create staff with custom role name -> 201", r.status_code == 201, r.text)
check("role name resolved to the HR role",
      r.json()["employment_information"]["role_id"] == hr_role["id"], r.text)

# Legacy enum values still resolve, via the default role of the same name.
r = client.post("/users", headers=p2_hdr, json={
    "name": "Legacy", "email": f"hr3_{uuid.uuid4().hex[:6]}@f.com",
    "username": f"u_{uuid.uuid4().hex[:8]}", "password": "Legacy@12345", "role": "sales_officer"})
check("legacy role=sales_officer still -> 201", r.status_code == 201, r.text)
check("legacy role maps to the Sales Officer role",
      r.json()["employment_information"]["role_detail"]["name"] == "Sales Officer", r.text)

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

# A role name works on PATCH as well as a role_id.
r = client.patch(f"/users/{p2_staff_id}", headers=p2_hdr, json={"role": "HR"})
check("PATCH /users/{id} by role name -> 200", r.status_code == 200
      and r.json()["employment_information"]["role_id"] == hr_role["id"], r.text)
check("an empty PATCH body is a no-op, not an error",
      client.patch(f"/users/{p2_staff_id}", headers=p2_hdr, json={}).status_code == 200)

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
# customer-outstanding. Placing an order no longer bills anybody — the receivable
# starts at the invoice — so bill it, then the 400 already paid leaves 690.
client.patch("/sales-workflow-settings", headers=rep_hdr, json={"invoice_timing": "on_order"})
client.post(f"/orders/{ro['id']}/invoice", headers=rep_hdr)
client.patch("/sales-workflow-settings", headers=rep_hdr, json={"invoice_timing": "after_delivery"})
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
check("customer opening_balance -> outstanding 500",
      fcust["financial_summary"]["outstanding_balance"] == 500, fcust["financial_summary"])
# An order reserves stock but bills nobody — the receivable starts at the invoice.
o = client.post("/orders", headers=fin_hdr, json={"customer_id": fcust["id"], "items": [{"product_id": fprod["id"], "quantity": 2, "unit_price": 100}]}).json()
check("placing an order does not bill the customer",
      client.get(f"/customers/{fcust['id']}", headers=fin_hdr)
      .json()["financial_summary"]["outstanding_balance"] == 500)
client.patch("/sales-workflow-settings", headers=fin_hdr, json={"invoice_timing": "on_order"})
client.post(f"/orders/{o['id']}/invoice", headers=fin_hdr)
client.patch("/sales-workflow-settings", headers=fin_hdr, json={"invoice_timing": "after_delivery"})
after_order = client.get(f"/customers/{fcust['id']}", headers=fin_hdr).json()
check("invoicing the order bills the customer (500+200=700)",
      after_order["financial_summary"]["outstanding_balance"] == 700,
      after_order["financial_summary"])
# record payment
r = client.post(f"/customers/{fcust['id']}/payments", headers=fin_hdr, json={"amount": 300, "payment_mode": "upi"})
check("customer payment -> received 300, outstanding 400", r.status_code == 201 and r.json()["total_received"] == 300 and r.json()["outstanding_balance"] == 400, r.text)
pays = client.get(f"/customers/{fcust['id']}/payments", headers=fin_hdr).json()
check("customer payment history -> 1", len(pays) == 1, pays)
r = client.delete(f"/customers/{fcust['id']}/payments/{pays[0]['id']}", headers=fin_hdr)
check("void customer payment -> outstanding back to 700", r.status_code == 200 and r.json()["outstanding_balance"] == 700, r.text)

print("\n== Sales Orders (placed on creation, stock reserved not deducted) ==")
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
check("order is placed on creation, no approval step", order["status"] == "placed", order)
check("its stock is reserved, not deducted", order["fulfilment_status"] == "reserved", order)
check("the line reports what is held for it", order["items"][0]["reserved_quantity"] == 10, order["items"][0])
check("the order names the warehouse it reserved from", order["warehouse_id"], order)
check("stock_summary shows on hand / reserved / available",
      order["stock_summary"][0]["on_hand"] == 100 and order["stock_summary"][0]["reserved"] == 10
      and order["stock_summary"][0]["available"] == 90, order["stock_summary"])
check("line_total = 10*500-100 = 4900", order["items"][0]["line_total"] == 4900, order)
check("order total = 4900-50+20 = 4870", order["total"] == 4870, order)
check("physical stock is untouched (still 100)", client.get(f"/inventory/{prod['id']}", headers=so_hdr).json()["total_stock"] == 100)

# With approval off — the default — an order never passes through /approve.
r = client.patch(f"/orders/{order_id}/approve", headers=so_hdr)
check("approving an already-placed order -> 400", r.status_code == 400, r.text[:200])
check("physical stock still untouched by that", client.get(f"/inventory/{prod['id']}", headers=so_hdr).json()["total_stock"] == 100)

# assign delivery partner
dp_email = f"dp_{uuid.uuid4().hex[:6]}@f.com"
roles_so = client.get("/roles", headers=so_hdr).json()
dp_role = next(x for x in roles_so if x["name"] == "Delivery Partner")["id"]
dp = client.post("/users", headers=so_hdr, json={"name": "DP", "email": dp_email, "username": f"dp_{uuid.uuid4().hex[:6]}", "password": "Staff@123", "role_id": dp_role}).json()
r = client.patch(f"/orders/{order_id}/assign-delivery-partner", headers=so_hdr, json={"delivery_partner_id": dp["id"]})
check("assigning a partner plans the delivery, it does not dispatch it",
      r.status_code == 200 and r.json()["fulfilment_status"] == "planned"
      and r.json()["status"] == "processing"
      and r.json()["assigned_delivery_partner_id"] == dp["id"], r.text)

# cancel releases the hold; physical stock never moved, so there is nothing to restore
r = client.patch(f"/orders/{order_id}/cancel", headers=so_hdr, json={"reason": "customer changed mind"})
check("cancel order -> cancelled", r.status_code == 200 and r.json()["status"] == "cancelled", r.text)
check("the hold is released", r.json()["fulfilment_status"] == "not_started"
      and r.json()["items"][0]["reserved_quantity"] == 0, r.json()["items"][0])
check("physical stock unchanged throughout (still 100)", client.get(f"/inventory/{prod['id']}", headers=so_hdr).json()["total_stock"] == 100)
check("and the stock is available again",
      client.get("/warehouses/stock", headers=so_hdr, params={"product_id": prod["id"]})
      .json()[0]["available"] == 100)

# reject a fresh pending order
r2 = client.post("/orders", headers=so_hdr, json={"customer_id": cust["id"], "items": [{"product_id": prod["id"], "quantity": 5}]})
oid2 = r2.json()["id"]
check("item unit_price defaults to product price", r2.json()["items"][0]["unit_price"] == 500, r2.text)
r = client.patch(f"/orders/{oid2}/reject", headers=so_hdr, json={"reason": "out of area"})
check("rejecting a placed order -> 400 (there is no approval step by default)",
      r.status_code == 400, r.text[:200])
client.patch(f"/orders/{oid2}/cancel", headers=so_hdr, json={"reason": "out of area"})
# filters + tenant isolation
check("list orders by status=cancelled", any(o["id"] == order_id for o in client.get("/orders", headers=so_hdr, params={"status": "cancelled"}).json()))
o_other = client.post("/auth/register", json={"organization_name": "O2", "admin_name": "X", "email": f"o2_{uuid.uuid4().hex[:6]}@f.com", "password": "Secret@123"}).json()
check("cross-org order get -> 404", client.get(f"/orders/{order_id}", headers={"Authorization": f"Bearer {o_other['tokens']['access_token']}"}).status_code == 404)
# A shortage is caught when the order is placed, not later at approval, and the
# response names exactly what is short.
r = client.post("/orders", headers=so_hdr, json={
    "customer_id": cust["id"], "items": [{"product_id": prod["id"], "quantity": 99999}]})
check("placing an order beyond available stock -> 400", r.status_code == 400, r.text[:200])
_short = r.json()["detail"]
check("the error names the shortage", _short["error"] == "INSUFFICIENT_STOCK"
      and _short["shortages"][0]["required_quantity"] == 99999
      and _short["shortages"][0]["available_quantity"] == 100
      and _short["shortages"][0]["short_quantity"] == 99899, _short)
check("and nothing was reserved by the attempt",
      client.get("/warehouses/stock", headers=so_hdr, params={"product_id": prod["id"]})
      .json()[0]["available"] == 100)
# Two lines of the same product are summed before the check, so they cannot each
# pass against the full availability.
check("two lines of the same product are summed against availability",
      client.post("/orders", headers=so_hdr, json={"customer_id": cust["id"], "items": [
          {"product_id": prod["id"], "quantity": 60},
          {"product_id": prod["id"], "quantity": 60}]}).status_code == 400)
# A firm that allows backorders can place it anyway.
client.patch("/sales-workflow-settings", headers=so_hdr, json={"allow_backorder": True})
check("with allow_backorder on, the same order is placed",
      client.post("/orders", headers=so_hdr, json={
          "customer_id": cust["id"],
          "items": [{"product_id": prod["id"], "quantity": 99999}]}).status_code == 201)
client.patch("/sales-workflow-settings", headers=so_hdr, json={"allow_backorder": False})

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

# Void a payment -> balance restored. Named by amount, not by position: both
# payments were recorded in the same instant, so their order is a tie.
void_pid = next(p for p in pays if p["amount"] == 1000)["id"]
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
check("update customer -> 200", r.status_code == 200 and r.json()["payment_information"]["credit_limit"] == 75000
      and r.json()["basic_information"]["customer_category"] == "Retail", r.text[:300])

# Assign a sales officer (must be a same-org user)
c_roles = client.get("/roles", headers=cust_hdr).json()
so_role = next(x for x in c_roles if x["name"] == "Sales Officer")
acc_role = next(x for x in c_roles if x["name"] == "Accountant")
so_email = f"cso_{uuid.uuid4().hex[:6]}@f.com"
so_user = client.post("/users", headers=cust_hdr, json={
    "name": "SO", "email": so_email, "username": f"cso_{uuid.uuid4().hex[:6]}", "password": "Staff@123", "role_id": so_role["id"]}).json()
r = client.patch(f"/customers/{cust_id}", headers=cust_hdr, json={"assigned_sales_officer_id": so_user["id"]})
check("assign sales officer -> nested name", r.status_code == 200
      and r.json()["sales_crm_information"]["sales_representative"]["name"] == "SO", r.text[:300])
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
check("create staff with username -> 201",
      r.status_code == 201 and r.json()["login_security"]["username"] == uname, r.text)
check("duplicate username -> 409",
      client.post("/users", headers=min_hdr, json={"name": "U2", "email": f"u2_{uuid.uuid4().hex[:6]}@f.com", "username": uname, "password": "Staff@123", "role": "accountant"}).status_code == 409)
check("username is optional — the form decides -> 201",
      client.post("/users", headers=min_hdr, json={"name": "U3", "email": f"u3_{uuid.uuid4().hex[:6]}@f.com", "password": "Staff@123", "role": "accountant"}).status_code == 201)

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

# Temporarily switch org to on_order billing so we can invoice without a delivery
# (Phase 2 now requires deliveries in after_delivery mode; this test predates that)
client.patch("/sales-workflow-settings", headers=fin_hdr, json={"invoice_timing": "on_order"})

# Generate invoice from order
r = client.post(f"/invoices/orders/{o_inv['id']}/invoice", headers=fin_hdr)
check("generate invoice from order -> 201", r.status_code == 201, r.text)

# Restore default after_delivery mode
client.patch("/sales-workflow-settings", headers=fin_hdr, json={"invoice_timing": "after_delivery"})

# Defensive: only proceed if we got an invoice back
if r.status_code == 201:
    invoice_obj = r.json()
else:
    invoice_obj = {"id": None}  # sentinel to prevent KeyError crash
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
check("update delivery status to Delivered -> 200",
      r.status_code == 200 and r.json()["fulfilment_status"] == "delivered"
      and r.json()["order_status"] == "completed", r.text)

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

print("\n== Sectioned customer profile + documents ==")
_rep = client.get("/users", headers=fin_hdr).json()[0]["id"]
_cp = client.post("/customers", headers=fin_hdr, json={
    "basic_information": {"customer_type": "business", "customer_name": "Sharma Enterprises",
                          "legal_business_name": "Sharma Enterprises Private Limited",
                          "industry": "FMCG", "customer_category": "wholesale", "status": "active"},
    "contact_information": {"primary_contact_person": "Rahul Sharma", "designation": "Purchase Manager",
                            "mobile_number": "+919876543210", "email_address": "rahul@sharma.com",
                            "website": "https://sharmaenterprises.com"},
    "address_information": {"billing_address": "Unit 12, Andheri", "shipping_address": "Warehouse 4",
                            "city": "Mumbai", "state": "Maharashtra", "country": "India",
                            "pin_zip_code": "400093",
                            "google_maps_location": {"latitude": 19.1136, "longitude": 72.8697}},
    "business_tax_information": {"gstin_tax_id": "27ABCDE1234F1Z5", "tax_exempt": False,
                                 "tax_category": "registered", "currency": "INR"},
    "payment_information": {"payment_terms": "net_30", "credit_limit": 500000,
                            "bank_name": "HDFC Bank", "ifsc_swift_code": "HDFC0001234"},
    "sales_crm_information": {"sales_representative_id": _rep, "lead_source": "referral",
                              "territory": "Mumbai West", "customer_priority": "high",
                              "preferred_communication": ["whatsapp", "email"],
                              "customer_tags": ["wholesale", "priority_customer"]},
    "social_media_online_presence": {"facebook_url": "https://facebook.com/sharma"},
    "additional_information": {"loyalty_number": "LOY-00125", "notes": "Deliver before 4 PM."},
    "preferences": {"portal_access_enabled": False, "preferred_invoice_delivery": ["email"]}})
check("sectioned customer create -> 201", _cp.status_code == 201, _cp.text[:400])
_cpj = _cp.json()
check("response carries all ten sections plus summaries",
      all(k in _cpj for k in ("basic_information", "contact_information", "address_information",
                              "business_tax_information", "payment_information", "sales_crm_information",
                              "social_media_online_presence", "documents", "additional_information",
                              "preferences", "financial_summary", "sales_summary")), sorted(_cpj))
check("fields land in their own section",
      _cpj["basic_information"]["legal_business_name"] == "Sharma Enterprises Private Limited"
      and _cpj["contact_information"]["designation"] == "Purchase Manager"
      and _cpj["payment_information"]["payment_terms"] == "net_30"
      and _cpj["sales_crm_information"]["customer_tags"] == ["wholesale", "priority_customer"], _cpj)
check("geo location round-trips",
      _cpj["address_information"]["google_maps_location"]["latitude"] == 19.1136,
      _cpj["address_information"].get("google_maps_location"))
check("sales representative resolved from the id",
      _cpj["sales_crm_information"]["sales_representative"]["id"] == _rep,
      _cpj["sales_crm_information"].get("sales_representative"))
check("financial summary computed",
      _cpj["financial_summary"]["credit_limit"] == 500000
      and _cpj["financial_summary"]["available_credit"] == 500000, _cpj["financial_summary"])
check("sales summary starts empty",
      _cpj["sales_summary"]["total_orders"] == 0
      and _cpj["sales_summary"]["last_purchase_date"] is None, _cpj["sales_summary"])
check("customer code still auto-generated", (_cpj["customer_id"] or "").startswith("CUST-"))

_cpu = client.patch(f"/customers/{_cpj['id']}", headers=fin_hdr,
                    json={"address_information": {"city": "Pune"}})
check("section-level partial update",
      _cpu.json()["address_information"]["city"] == "Pune"
      and _cpu.json()["address_information"]["state"] == "Maharashtra"
      and _cpu.json()["basic_information"]["customer_name"] == "Sharma Enterprises", _cpu.text[:250])
check("a flat body is still accepted",
      client.post("/customers", headers=fin_hdr,
                  json={"name": "Flat Body Co", "phone": "9800009999", "credit_limit": 100}
                  ).json()["basic_information"]["customer_name"] == "Flat Body Co")
check("customer_name is required",
      client.post("/customers", headers=fin_hdr,
                  json={"basic_information": {"industry": "X"}}).status_code == 422)

_pdf = b"%PDF-1.4 cert"


def _upload(name):
    """Upload with no record id at all — this is what makes create-time
    documents possible."""
    return client.post("/files/upload", headers=fin_hdr,
                       files={"file": (name, io.BytesIO(_pdf), "application/pdf")}).json()


_fg, _fp, _fo1, _fo2 = _upload("gst.pdf"), _upload("pan.pdf"), _upload("a.pdf"), _upload("b.pdf")
_dc = client.post("/customers", headers=fin_hdr, json={
    "basic_information": {"customer_name": "Docs At Create"},
    "documents": {"gst_certificate_id": _fg["file_id"], "pan_card_id": _fp["file_id"],
                  "other_document_ids": [_fo1["file_id"], _fo2["file_id"]]}})
check("create a customer with already-uploaded document ids -> 201", _dc.status_code == 201,
      _dc.text[:300])
check("the ids you sent are the ids you get back",
      _dc.json()["documents"]["gst_certificate_id"] == _fg["file_id"]
      and sorted(_dc.json()["documents"]["other_document_ids"]) == sorted([_fo1["file_id"], _fo2["file_id"]]),
      _dc.json()["documents"])
check("create-time documents appear in the documents list",
      {d["name"] for d in client.get(f"/customers/{_dc.json()['id']}/documents",
                                     headers=fin_hdr).json()} == {"gst.pdf", "pan.pdf", "a.pdf", "b.pdf"})
check("a create-time document downloads",
      client.get(f"/customers/{_dc.json()['id']}/documents/{_fg['file_id']}/download",
                 headers=fin_hdr).content == _pdf)
check("patching a named slot with a new file id replaces it",
      client.patch(f"/customers/{_dc.json()['id']}", headers=fin_hdr,
                   json={"documents": {"gst_certificate_id": _upload("gst2.pdf")["file_id"]}}
                   ).json()["documents"]["gst_certificate_id"] != _fg["file_id"])
check("an unknown file id -> 400",
      client.post("/customers", headers=fin_hdr,
                  json={"basic_information": {"customer_name": "Bad Doc"},
                        "documents": {"gst_certificate_id": "nope"}}).status_code == 400)
_d = client.post(f"/customers/{_cpj['id']}/documents", headers=fin_hdr,
                 data={"document_type": "gst_certificate"},
                 files={"file": ("gst-certificate.pdf", io.BytesIO(_pdf), "application/pdf")})
check("upload a named customer document -> 201", _d.status_code == 201, _d.text[:250])
check("document response shape",
      _d.json()["document_type"] == "gst_certificate" and _d.json()["name"] == "gst-certificate.pdf"
      and _d.json()["content_type"] == "application/pdf" and _d.json()["size"] == len(_pdf), _d.json())
check("document url is a link, not base64", is_file_url(_d.json()["url"]), _d.json().get("url"))
check("profile documents section fills from the upload",
      client.get(f"/customers/{_cpj['id']}", headers=fin_hdr
                 ).json()["documents"]["gst_certificate_id"] == _d.json()["id"])
client.post(f"/customers/{_cpj['id']}/documents", headers=fin_hdr,
            data={"document_type": "gst_certificate"},
            files={"file": ("gst-v2.pdf", io.BytesIO(_pdf), "application/pdf")})
check("a named slot holds one file",
      len([x for x in client.get(f"/customers/{_cpj['id']}/documents", headers=fin_hdr).json()
           if x["document_type"] == "gst_certificate"]) == 1)
_others = client.post(f"/customers/{_cpj['id']}/documents/other", headers=fin_hdr,
                      files=[("files", ("a.pdf", io.BytesIO(_pdf), "application/pdf")),
                             ("files", ("b.pdf", io.BytesIO(_pdf), "application/pdf"))])
check("other documents upload as an array", _others.status_code == 201 and len(_others.json()) == 2,
      _others.text[:250])
check("other documents append",
      len(client.post(f"/customers/{_cpj['id']}/documents/other", headers=fin_hdr,
                      files=[("files", ("c.pdf", io.BytesIO(_pdf), "application/pdf"))]).json()) == 3)
check("profile lists other_document_ids",
      len(client.get(f"/customers/{_cpj['id']}", headers=fin_hdr
                     ).json()["documents"]["other_document_ids"]) == 3)
_alldocs = client.get(f"/customers/{_cpj['id']}/documents", headers=fin_hdr).json()
check("documents list returns everything", len(_alldocs) == 4, [x["document_type"] for x in _alldocs])
_dl = client.get(f"/customers/{_cpj['id']}/documents/{_alldocs[0]['id']}/download", headers=fin_hdr)
check("download returns the bytes", _dl.status_code == 200 and _dl.content == _pdf, _dl.status_code)
check("delete a document -> 204",
      client.delete(f"/customers/{_cpj['id']}/documents/{_alldocs[0]['id']}",
                    headers=fin_hdr).status_code == 204)
check("unknown document -> 404",
      client.delete(f"/customers/{_cpj['id']}/documents/nope", headers=fin_hdr).status_code == 404)
check("invalid document_type -> 400",
      client.post(f"/customers/{_cpj['id']}/documents", headers=fin_hdr,
                  data={"document_type": "nonsense"},
                  files={"file": ("x.pdf", io.BytesIO(_pdf), "application/pdf")}).status_code == 400)

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

print("\n== Company Settings dashboard (GET /organizations/overview) ==")
# One call behind the whole page: header, stat tiles, storage, completion,
# authorized person, documents, addresses and the activity feed.
client.put("/organizations/settings", headers=fin_hdr, json={
    "industry": "Beverages", "business_type": "Private Limited",
    "date_of_incorporation": "2025-01-15", "company_status": "active",
    "auth_person_name": "John Smith", "auth_person_designation": "Chief Executive Officer",
    "auth_person_email": "john.smith@abcbeverages.com", "auth_person_mobile": "+919123456789",
    "registered_address": "123, Business Park, Hitech City", "city": "Hyderabad",
    "state": "Telangana", "country": "India", "pin_code": "500081",
    "maps_latitude": 17.4435, "maps_longitude": 78.3772})

r = client.get("/organizations/overview", headers=fin_hdr)
check("GET /organizations/overview -> 200", r.status_code == 200, r.text[:400])
ov = r.json()
check("overview has one block per card on the page",
      set(ov) == {"company", "counts", "storage", "profile_completion", "authorized_person",
                  "documents", "addresses", "recent_activity"}, sorted(ov))

# --- header ---
co = ov["company"]
check("company code issued as CMP-#####",
      (co["company_code"] or "").startswith("CMP-") and co["company_code"][4:].isdigit(), co)
check("the company code is stable across calls",
      client.get("/organizations/overview", headers=fin_hdr).json()["company"]["company_code"]
      == co["company_code"])
check("company code also on GET /settings",
      client.get("/organizations/settings", headers=fin_hdr).json()["company_code"] == co["company_code"])
check("header carries industry / company type / registration date",
      co["industry"] == "Beverages" and co["company_type"] == "Private Limited"
      and co["registration_date"] == "2025-01-15", co)
check("header carries the company status and the plan",
      co["company_status"] == "active" and co["plan"]["name"] is not None, co)
check("plan block reports the subscription lifecycle separately",
      co["plan"]["subscription_status"] in ("trial", "active", "locked", "inactive", "suspended"),
      co["plan"])

# --- stat tiles ---
counts = ov["counts"]
_users = client.get("/users", headers=fin_hdr).json()
check("employees count matches the employee list", counts["employees"] == len(_users), counts)
check("active users counts only accounts that can log in",
      counts["active_users"] == sum(1 for u in _users if u["is_active"]), counts)
check("branches count matches branch_addresses",
      counts["branches"] == len(client.get("/organizations/settings",
                                           headers=fin_hdr).json()["branch_addresses"]), counts)
check("documents count matches the uploaded documents",
      counts["documents"] == ov["documents"]["uploaded"], counts)

# --- storage ---
st = ov["storage"]
check("storage reports files, bytes and the derived units",
      st["files"] > 0 and st["used_bytes"] > 0 and st["used_mb"] >= 0 and st["used_gb"] >= 0, st)
check("an unlimited plan reports no percentage",
      st["limit_gb"] is not None or st["percent_used"] is None, st)
_plan_id = co["plan"]["id"]
client.put(f"/superadmin/plans/{_plan_id}", headers=sa_hdr, json={"max_storage_gb": 20})
st2 = client.get("/organizations/overview", headers=fin_hdr).json()["storage"]
check("a plan quota drives limit_gb and percent_used",
      st2["limit_gb"] == 20 and st2["percent_used"] is not None and st2["percent_used"] < 100, st2)

# --- profile completion ---
pc = ov["profile_completion"]
check("completion reports a percentage out of the mandatory fields",
      0 <= pc["percent"] <= 100 and pc["filled"] + len(pc["missing_fields"]) == pc["total"], pc)
check("missing information is labelled for display and named for code",
      len(pc["missing_information"]) == len(pc["missing_fields"]), pc)
check("completion agrees with /settings/completeness",
      pc["total"] == client.get("/organizations/settings/completeness",
                                headers=fin_hdr).json()["total"], pc)

# --- authorized person ---
ap = ov["authorized_person"]
check("authorized person block filled from the company profile",
      ap["name"] == "John Smith" and ap["designation"] == "Chief Executive Officer"
      and ap["email"] == "john.smith@abcbeverages.com" and ap["is_complete"] is True, ap)

# --- documents ---
docs = ov["documents"]
check("documents list every named slot plus the 'other' files",
      docs["total"] == len(docs["items"]) and docs["uploaded"] + docs["pending"] == docs["total"], docs)
check("each document row carries key / name / status / url",
      all({"key", "name", "status", "url"} <= set(d) for d in docs["items"]), docs["items"][:2])
check("a filled slot reads uploaded, an empty one pending",
      {d["status"] for d in docs["items"]} <= {"uploaded", "pending"},
      [d["status"] for d in docs["items"]])
check("the GST slot was cleared earlier, so it reads pending",
      next(d for d in docs["items"] if d["key"] == "gst_certificate")["status"] == "pending", docs)
client.post("/organizations/settings/documents/other", headers=fin_hdr, files=[
    ("files", ("insurance-certificate.pdf", io.BytesIO(b"%PDF-1.4 ins"), "application/pdf"))])
_docs2 = client.get("/organizations/overview", headers=fin_hdr).json()["documents"]
check("'other' documents appear as their own rows, named after the file",
      any(d["key"] == "other" and d["name"] == "insurance-certificate.pdf"
          for d in _docs2["items"]), _docs2["items"][-2:])
check("an 'other' row carries the size and upload time the slot knows",
      all(d["size"] and d["uploaded_at"] for d in _docs2["items"] if d["key"] == "other"),
      [d for d in _docs2["items"] if d["key"] == "other"])

# --- addresses ---
addrs = ov["addresses"]
check("the registered office is first and primary",
      addrs[0]["type"] == "registered_office" and addrs[0]["is_primary"] is True, addrs[0])
check("the registered office carries its map pin",
      addrs[0]["latitude"] == 17.4435 and addrs[0]["longitude"] == 78.3772, addrs[0])
check("branches follow, with their labels and ids",
      all(a["type"] == "branch" and a["id"] for a in addrs[1:])
      and any(a["label"] == "Pune Warehouse" for a in addrs[1:]), addrs[1:])
check("a branch can carry its own map pin",
      client.put("/organizations/settings", headers=fin_hdr, json={"branch_addresses": [
          {"label": "Pune Warehouse", "address": "Plot 12, MIDC", "city": "Pune",
           "pin_code": "411018", "latitude": 18.5204, "longitude": 73.8567}]})
      .json()["branch_addresses"][0]["latitude"] == 18.5204)

# --- recent activity ---
acts = ov["recent_activity"]
check("recent activity is newest first, with who did it",
      len(acts) > 0 and all({"id", "type", "title", "at", "by"} <= set(a) for a in acts), acts[:2])
check("the profile update just made is in the feed",
      any(a["type"] == "authorized_person" for a in acts), [a["title"] for a in acts])
check("one update is split into the sections of the page it touched",
      {"address", "company_profile"} <= {a["type"] for a in acts}, [a["type"] for a in acts])
_all_acts = client.get("/organizations/overview", headers=fin_hdr,
                       params={"activity_limit": 100}).json()["recent_activity"]
check("document uploads are recorded",
      any(a["type"] == "document" for a in _all_acts), [a["title"] for a in _all_acts])
check("the feed is newest first",
      [a["at"] for a in _all_acts] == sorted((a["at"] for a in _all_acts), reverse=True))
check("activity_limit caps the feed",
      len(client.get("/organizations/overview", headers=fin_hdr,
                     params={"activity_limit": 2}).json()["recent_activity"]) == 2)
check("activity_limit out of range -> 422",
      client.get("/organizations/overview", headers=fin_hdr,
                 params={"activity_limit": 0}).status_code == 422)
_before = len(client.get("/organizations/overview", headers=fin_hdr,
                         params={"activity_limit": 100}).json()["recent_activity"])
_emp = client.post("/users", headers=fin_hdr, json={
    "name": "Feed Tester", "email": f"feed_{uuid.uuid4().hex[:8]}@firm.com",
    "username": f"feed_{uuid.uuid4().hex[:8]}", "password": "Staff@123", "role": "accountant"}).json()
_after = client.get("/organizations/overview", headers=fin_hdr,
                    params={"activity_limit": 100}).json()["recent_activity"]
check("adding an employee shows up in the feed",
      len(_after) == _before + 1 and _after[0]["type"] == "employee"
      and _after[0]["by"] is not None, _after[:1])
check("removing an employee is recorded too",
      client.delete(f"/users/{_emp['id']}", headers=fin_hdr).status_code == 204
      and client.get("/organizations/overview", headers=fin_hdr)
      .json()["recent_activity"][0]["title"] == "Employee removed")
check("the feed is scoped to the firm",
      all(a["id"] for a in client.get("/organizations/overview", headers=min_hdr)
          .json()["recent_activity"]))
check("another firm cannot see this firm's activity",
      client.get("/organizations/overview", headers=min_hdr).json()["recent_activity"]
      != ov["recent_activity"])
check("staff cannot read the overview -> 403",
      client.get("/organizations/overview", headers=st_hdr).status_code == 403)

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

# Placed straight away and its stock reserved — no approval step in the way.
client.patch("/sales-workflow-settings", headers=fin_hdr, json={"invoice_timing": "on_order"})
_hsn_r = client.post("/orders", headers=fin_hdr, json={
    "customer_id": hsn_cust["id"],
    "items": [{"product_id": hsn_prod["id"], "quantity": 1, "unit_price": 500}]})
check("place the HSN order -> 201", _hsn_r.status_code == 201, _hsn_r.text[:400])
hsn_order = _hsn_r.json()
r = client.post(f"/orders/{hsn_order['id']}/invoice", headers=fin_hdr)
check("POST /orders/{id}/invoice -> 201", r.status_code == 201, f"{r.status_code} {r.text[:250]}")
check("order-generated invoice carries the HSN code",
      r.json()["items"][0]["hsn_code"] == "7306", r.json()["items"][0])
client.patch("/sales-workflow-settings", headers=fin_hdr, json={"invoice_timing": "after_delivery"})

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
    "items": [{"product_id": fprod["id"], "quantity_returned": 2}]
}
r = client.post("/sales-returns", headers=fin_hdr, json=sales_ret_payload)
check("POST /sales-returns -> 201", r.status_code == 201, r.text)
ret_obj = r.json()
check("return_number matches", ret_obj["return_number"] == "RET-2026-001")

# A request is only a request: the goods are not back yet, so no stock has moved.
new_stock = client.get(f"/inventory/{fprod['id']}", headers=fin_hdr).json()["total_stock"]
check("a return request moves no stock", new_stock == old_stock, f"Old: {old_stock}, New: {new_stock}")
r = client.patch(f"/sales-returns/{ret_obj['id']}/approve", headers=fin_hdr, json={
    "items": [{"return_item_id": ret_obj["items"][0]["id"], "received_quantity": 2,
               "condition": "saleable", "restock": True}]})
check("approving it -> 200", r.status_code == 200 and r.json()["status"] == "approved", r.text[:300])
new_stock = client.get(f"/inventory/{fprod['id']}", headers=fin_hdr).json()["total_stock"]
check("approval puts the saleable goods back", new_stock == old_stock + 2,
      f"Old: {old_stock}, New: {new_stock}")

r = client.get("/sales-returns", headers=fin_hdr)
check("GET /sales-returns list -> 200", r.status_code == 200 and len(r.json()) >= 1, r.text)

r = client.get(f"/sales-returns/{ret_obj['id']}", headers=fin_hdr)
check("GET /sales-returns/{id} detail -> 200", r.status_code == 200, r.text)

print("\n== Employee profile on users (sectioned body) ==")
# POST /users and PATCH /users/{id} take the same sectioned body, and GET hands it
# back in the same shape. No dropdown data and no required-field policy come from
# the backend — the form owns both.
emp_email = f"emp_{uuid.uuid4().hex[:8]}@firm.com"
emp_username = f"emp_{uuid.uuid4().hex[:8]}"
r = _raw_post("/users", headers=fin_hdr, json={
    "employee_id": "EMP-9001",
    "role": "sales_officer",
    "basic_information": {
        "first_name": "Ravi", "last_name": "Kumar", "display_name": "Ravi Kumar",
        "gender": "Male", "date_of_birth": "1998-05-12", "marital_status": "Single",
        "blood_group": "O+", "nationality": "Indian"},
    "contact_information": {"official_email": emp_email, "mobile_number": "+919876543210"},
    "employment_information": {
        "designation": "Field Sales Executive", "employment_type": "Full Time",
        "date_of_joining": "2026-01-15", "employee_status": "On-Leave"},
    "login_security": {"username": emp_username, "password": "Staff@123",
                       "confirm_password": "Staff@123"},
})
check("create employee from the sectioned body -> 201", r.status_code == 201, r.text)
emp = r.json()
check("employee_id stored", emp["employee_id"] == "EMP-9001", emp)
check("name composed from first + last", emp["name"] == "Ravi Kumar", emp)
check("basic_information stored",
      emp["basic_information"]["first_name"] == "Ravi"
      and emp["basic_information"]["last_name"] == "Kumar"
      and emp["basic_information"]["blood_group"] == "O+"
      and (emp["basic_information"]["date_of_birth"] or "").startswith("1998-05-12"),
      emp["basic_information"])
check("contact_information stored",
      emp["contact_information"]["official_email"] == emp_email
      and emp["contact_information"]["mobile_number"] == "+919876543210",
      emp["contact_information"])
check("designation stored",
      emp["employment_information"]["designation"] == "Field Sales Executive",
      emp["employment_information"])
check("employment_type normalized 'Full Time' -> full_time",
      emp["employment_information"]["employment_type"] == "full_time", emp["employment_information"])
check("employee_status normalized 'On-Leave' -> on_leave",
      emp["employment_information"]["employee_status"] == "on_leave", emp["employment_information"])
check("date_of_joining stored",
      (emp["employment_information"]["date_of_joining"] or "").startswith("2026-01-15"),
      emp["employment_information"])
check("login_security shows the username and never the password",
      emp["login_security"] == {"username": emp_username}, emp["login_security"])
check("GET /users/{id} returns exactly the same profile",
      client.get(f"/users/{emp['id']}", headers=fin_hdr).json() == emp)
check("the new employee can log in",
      client.post("/auth/login", json={"email": emp_email, "password": "Staff@123"}).status_code == 200)

# employee_id is auto-assigned when omitted, and unique per firm
r = client.post("/users", headers=fin_hdr, json={
    "name": "Auto Coded", "email": f"auto_{uuid.uuid4().hex[:8]}@firm.com",
    "username": f"auto_{uuid.uuid4().hex[:8]}", "password": "Staff@123", "role": "accountant"})
auto_emp = r.json()
check("employee_id auto-assigned EMP-####",
      r.status_code == 201 and (auto_emp["employee_id"] or "").startswith("EMP-"), r.text)
check("employee_status defaults to active",
      auto_emp["employment_information"]["employee_status"] == "active", auto_emp)
check("account_status defaults to active",
      auto_emp["system_preferences"]["account_status"] == "active", auto_emp)
check("duplicate employee_id -> 409", client.post("/users", headers=fin_hdr, json={
    "name": "Dupe", "email": f"dup_{uuid.uuid4().hex[:8]}@firm.com",
    "username": f"dup_{uuid.uuid4().hex[:8]}", "password": "Staff@123", "role": "accountant",
    "employee_id": "EMP-9001"}).status_code == 409)
check("invalid employment_type -> 422", client.post("/users", headers=fin_hdr, json={
    "name": "Bad", "email": f"bad_{uuid.uuid4().hex[:8]}@firm.com",
    "username": f"bad_{uuid.uuid4().hex[:8]}", "password": "Staff@123", "role": "accountant",
    "employment_type": "freelance-ish"}).status_code == 422)

# Filters (the list endpoint stays flat — one row per employee)
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
check("the list stays flat", all("basic_information" not in u for u in client.get("/users", headers=fin_hdr).json()))

# Edit the profile — any mix of sections in one call
r = client.patch(f"/users/{emp['id']}", headers=fin_hdr, json={
    "employment_information": {"designation": "Senior Sales Executive", "employee_status": "active"},
    "contact_information": {"mobile_number": "9812345678"}})
check("PATCH employee profile -> 200", r.status_code == 200
      and r.json()["employment_information"]["designation"] == "Senior Sales Executive"
      and r.json()["employment_information"]["employee_status"] == "active"
      and r.json()["contact_information"]["mobile_number"] == "9812345678", r.text)
check("sections the PATCH did not mention are untouched",
      r.json()["basic_information"]["blood_group"] == "O+", r.json()["basic_information"])
check("PATCH to a taken employee_id -> 409",
      client.patch(f"/users/{auto_emp['id']}", headers=fin_hdr, json={"employee_id": "EMP-9001"}).status_code == 409)
check("PATCH keeping own employee_id -> 200",
      client.patch(f"/users/{emp['id']}", headers=fin_hdr, json={"employee_id": "EMP-9001"}).status_code == 200)
check("a flat PATCH body still folds into its sections",
      client.patch(f"/users/{emp['id']}", headers=fin_hdr, json={"work_location": "Pune Office"})
      .json()["employment_information"]["work_location"] == "Pune Office")
check("no dropdown data is served from the backend",
      client.get("/users/meta/employee-options", headers=fin_hdr).status_code == 404)

# Identity proof upload
# Identity proof: upload, then PATCH the URL in — the only path there is.
aadhaar = client.post("/files/upload", headers=fin_hdr,
                      files={"file": ("aadhaar.pdf", io.BytesIO(b"%PDF-1.4 id"), "application/pdf")}).json()
r = client.patch(f"/users/{emp['id']}", headers=fin_hdr, json={
    "documents": {"identity_proof_type": "Aadhaar", "identity_proof_file": aadhaar["url"]}})
check("attach the identity proof by URL -> 200",
      r.status_code == 200 and r.json()["documents"]["identity_proof_file"] == aadhaar["url"], r.text[:200])
check("the removed /identity-proof route is gone",
      client.post(f"/users/{emp['id']}/identity-proof", headers=fin_hdr,
                  files={"file": ("a.pdf", io.BytesIO(b"%PDF"), "application/pdf")}).status_code == 404)
check("clearing a named slot with null -> 200",
      client.patch(f"/users/{emp['id']}", headers=fin_hdr, json={
          "documents": {"identity_proof_file": None}}).json()["documents"]["identity_proof_file"] is None)
client.patch(f"/users/{emp['id']}", headers=fin_hdr,
             json={"documents": {"identity_proof_file": aadhaar["url"]}})
check("employee profile visible on /auth/me",
      "employee_id" in client.get("/auth/me", headers=fin_hdr).json()["user"])

print("\n== Employee profile: only the login pair is mandatory ==")
# Every other field is optional; which of them a form insists on is the frontend's
# call. These go through `_raw_post` so the flat defaults do not mask the check.
check("POST /users without contact_information.official_email -> 422",
      _raw_post("/users", headers=fin_hdr, json={
          "login_security": {"password": "Staff@123", "confirm_password": "Staff@123"}}).status_code == 422)
check("POST /users without login_security.password -> 422",
      _raw_post("/users", headers=fin_hdr, json={
          "contact_information": {"official_email": f"np_{uuid.uuid4().hex[:8]}@firm.com"}}).status_code == 422)
r = _raw_post("/users", headers=fin_hdr, json={
    "contact_information": {"official_email": f"bare_{uuid.uuid4().hex[:8]}@firm.com"},
    "login_security": {"password": "Staff@123", "confirm_password": "Staff@123"}})
check("POST /users with just those two -> 201", r.status_code == 201, r.text[:300])
check("no role is required either",
      r.json()["employment_information"]["role_id"] is None, r.json()["employment_information"])
check("mismatched confirm_password -> 422",
      _raw_post("/users", headers=fin_hdr, json={
          "contact_information": {"official_email": f"cp_{uuid.uuid4().hex[:8]}@firm.com"},
          "login_security": {"password": "Staff@123",
                             "confirm_password": "Different@123"}}).status_code == 422)

print("\n== Employee profile: every section, with uploaded files ==")
# The form's real order: upload each file first (no employee id needed), then send
# the URLs it got back in one create call.
photo = client.post("/files/upload", headers=fin_hdr, files={
    "file": ("profile-photo.png", io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"0" * 20), "image/png")}).json()
cert1 = client.post("/files/upload", headers=fin_hdr, files={
    "file": ("experience-1.pdf", io.BytesIO(b"%PDF-1.4 a"), "application/pdf")}).json()
cert2 = client.post("/files/upload", headers=fin_hdr, files={
    "file": ("experience-2.pdf", io.BytesIO(b"%PDF-1.4 b"), "application/pdf")}).json()
check("POST /files/upload returns file_id / url / name / content_type / size",
      {"file_id", "url", "name", "content_type", "size"} <= set(photo)
      and is_file_url(photo["url"]) and photo["name"] == "profile-photo.png", photo)

ext_email = f"ext_{uuid.uuid4().hex[:8]}@firm.com"
r = _raw_post("/users", headers=fin_hdr, json={
    "role": "accountant",
    "basic_information": {
        "first_name": "Asha", "last_name": "Verma", "gender": "Female", "blood_group": "B+",
        "date_of_birth": "1996-03-04", "nationality": "Indian", "profile_photo": photo["url"]},
    "contact_information": {
        "official_email": ext_email, "personal_email": "asha@example.com",
        "mobile_number": "9800000001", "alternate_mobile_number": "9800000002",
        "emergency_contact_name": "R Verma", "emergency_contact_number": "9800000000",
        "emergency_contact_relationship": "Father"},
    "address_information": {
        "current_address": "12 MG Road", "permanent_address": "Old Pune Road", "city": "Pune",
        "state": "Maharashtra", "country": "India", "pin_zip_code": "411001"},
    "employment_information": {
        "designation": "Accountant", "employment_type": "full_time",
        "date_of_joining": "2026-02-01", "work_location": "Pune Office", "shift": "Morning",
        "employee_status": "probation"},
    "login_security": {"username": f"ext_{uuid.uuid4().hex[:8]}", "password": "Staff@123",
                       "confirm_password": "Staff@123"},
    "payroll_information": {
        "basic_salary": 42000, "bank_name": "SBI", "account_number": "9988776655",
        "ifsc_swift_code": "SBIN0001", "account_holder_name": "Asha Verma", "upi_id": "asha@upi"},
    "documents": {
        "identity_proof_type": "Aadhaar", "identity_proof_file": cert1["url"],
        "experience_certificates": [cert1["url"], cert2["url"]]},
    "professional_information": {"skills": ["Tally", "GST"]},
    "system_preferences": {"language": "en", "time_zone": "Asia/Kolkata",
                           "account_status": "active"},
})
check("create with every section -> 201", r.status_code == 201, r.text)
ext = r.json()
check("name composed when omitted", ext["name"] == "Asha Verma", ext["name"])
check("the uploaded photo is the profile photo",
      ext["basic_information"]["profile_photo"] == photo["url"], ext["basic_information"])
check("address section stored", ext["address_information"]["city"] == "Pune"
      and ext["address_information"]["pin_zip_code"] == "411001"
      and (ext["address_information"]["current_address"] or "").startswith("12 MG"),
      ext["address_information"])
check("emergency contact stored",
      ext["contact_information"]["emergency_contact_relationship"] == "Father",
      ext["contact_information"])
check("payroll section stored", ext["payroll_information"]["basic_salary"] == 42000
      and ext["payroll_information"]["upi_id"] == "asha@upi", ext["payroll_information"])
check("skills stored as a list",
      ext["professional_information"]["skills"] == ["Tally", "GST"], ext["professional_information"])
check("system preferences stored",
      ext["system_preferences"]["time_zone"] == "Asia/Kolkata", ext["system_preferences"])
check("document URLs round-trip exactly as sent",
      ext["documents"]["identity_proof_file"] == cert1["url"]
      and ext["documents"]["experience_certificates"] == [cert1["url"], cert2["url"]],
      ext["documents"])
check("legacy identify_proofs still mirrors it on the flat shape",
      next(u for u in client.get("/users", headers=fin_hdr).json()
           if u["id"] == ext["id"])["identify_proofs"] == cert1["url"])

# Reporting manager
r = client.patch(f"/users/{ext['id']}", headers=fin_hdr, json={
    "employment_information": {"reporting_manager_id": emp["id"]}})
check("set reporting manager -> 200", r.status_code == 200
      and r.json()["employment_information"]["reporting_manager_id"] == emp["id"], r.text)
check("the manager is resolved to a name",
      r.json()["employment_information"]["reporting_manager"]["name"] == emp["name"],
      r.json()["employment_information"])
check("self as reporting manager -> 400",
      client.patch(f"/users/{ext['id']}", headers=fin_hdr, json={
          "employment_information": {"reporting_manager_id": ext["id"]}}).status_code == 400)
check("reporting loop -> 400",
      client.patch(f"/users/{emp['id']}", headers=fin_hdr, json={
          "employment_information": {"reporting_manager_id": ext["id"]}}).status_code == 400)
check("filter by reporting_manager_id",
      [u["id"] for u in client.get("/users", headers=fin_hdr,
                                   params={"reporting_manager_id": emp["id"]}).json()] == [ext["id"]])

print("\n== One endpoint for every status change ==")
r = client.patch(f"/users/{ext['id']}", headers=fin_hdr, json={
    "system_preferences": {"account_status": "Suspended"}})
check("account_status suspends and blocks login", r.status_code == 200
      and r.json()["system_preferences"]["account_status"] == "suspended"
      and r.json()["is_active"] is False, r.text)
check("suspended user cannot log in",
      client.post("/auth/login", json={"email": ext_email, "password": "Staff@123"}).status_code == 403)
# A resignation: employment status, exit date and account status in a single PATCH.
r = client.patch(f"/users/{ext['id']}", headers=fin_hdr, json={
    "employment_information": {"employee_status": "resigned", "date_of_exit": "2026-08-31"},
    "system_preferences": {"account_status": "inactive"}})
check("resign in one call", r.status_code == 200
      and r.json()["employment_information"]["employee_status"] == "resigned"
      and (r.json()["employment_information"]["date_of_exit"] or "").startswith("2026-08-31")
      and r.json()["system_preferences"]["account_status"] == "inactive"
      and r.json()["is_active"] is False, r.text)
r = client.patch(f"/users/{ext['id']}", headers=fin_hdr, json={
    "system_preferences": {"account_status": "active"}})
check("back to active restores is_active", r.json()["is_active"] is True, r.text)
check("filter by status=active",
      any(u["id"] == ext["id"] for u in client.get("/users", headers=fin_hdr,
                                                   params={"status": "active"}).json()))
check("the separate status endpoints are gone",
      client.patch(f"/users/{ext['id']}/status", headers=fin_hdr,
                   json={"is_active": False}).status_code == 404
      and client.patch(f"/users/{ext['id']}/account-status", headers=fin_hdr,
                       json={"status": "locked"}).status_code == 404)
check("an admin cannot deactivate their own account -> 400",
      client.patch(f"/users/{client.get('/auth/me', headers=fin_hdr).json()['user']['id']}",
                   headers=fin_hdr,
                   json={"system_preferences": {"account_status": "inactive"}}).status_code == 400)
# A password change rides on the same endpoint.
r = client.patch(f"/users/{ext['id']}", headers=fin_hdr, json={
    "login_security": {"password": "Fresh@12345", "confirm_password": "Fresh@12345"}})
check("PATCH changes the password too", r.status_code == 200, r.text[:200])
check("the new password works",
      client.post("/auth/login", json={"email": ext_email, "password": "Fresh@12345"}).status_code == 200)

print("\n== Employee files: upload, attach, detach ==")
# One upload endpoint, then PATCH the URLs in. The per-field upload routes are gone.
photo2 = client.post("/files/upload", headers=fin_hdr, files={
    "file": ("p.png", io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"0" * 20), "image/png")}).json()
edu = [client.post("/files/upload", headers=fin_hdr, files={
    "file": (f"e{i}.pdf", io.BytesIO(b"%PDF-1.4 " + str(i).encode()), "application/pdf")}).json()
    for i in range(1, 4)]
r = client.patch(f"/users/{ext['id']}", headers=fin_hdr, json={
    "basic_information": {"profile_photo": photo2["url"]},
    "documents": {"educational_certificates": [d["url"] for d in edu]}})
check("attach a photo and a document list in one PATCH", r.status_code == 200
      and r.json()["basic_information"]["profile_photo"] == photo2["url"]
      and r.json()["documents"]["educational_certificates"] == [d["url"] for d in edu], r.text[:300])
check("the removed per-field upload route is gone",
      client.post(f"/users/{ext['id']}/files/profile_photo", headers=fin_hdr,
                  files={"file": ("p.png", io.BytesIO(b"\x89PNG"), "image/png")}).status_code == 404)
check("delete one document out of a list -> 200",
      client.delete(f"/users/{ext['id']}/documents/educational_certificates",
                    headers=fin_hdr, params={"document_id": edu[0]["file_id"]})
      .json()["documents"]["educational_certificates"] == [edu[1]["url"], edu[2]["url"]])
check("delete an unknown document -> 404",
      client.delete(f"/users/{ext['id']}/documents/educational_certificates",
                    headers=fin_hdr, params={"document_id": "nope"}).status_code == 404)
check("other_documents names the same slot as uploaded_documents",
      client.delete(f"/users/{ext['id']}/documents/other_documents",
                    headers=fin_hdr, params={"document_id": "nope"}).status_code == 404)
check("other collections untouched",
      client.get(f"/users/{ext['id']}", headers=fin_hdr).json()["documents"]["experience_certificates"]
      == [cert1["url"], cert2["url"]])
check("PATCH documents with an empty list clears that slot",
      client.patch(f"/users/{ext['id']}", headers=fin_hdr,
                   json={"documents": {"experience_certificates": []}})
      .json()["documents"]["experience_certificates"] == [])
check("and leaves the neighbouring slots alone",
      client.get(f"/users/{ext['id']}", headers=fin_hdr).json()["documents"]["identity_proof_type"] == "Aadhaar")
check("a document list beyond the sanity cap -> 422",
      client.patch(f"/users/{ext['id']}", headers=fin_hdr, json={
          "documents": {"other_documents": [f"https://x/files/{n}" for n in range(51)]}}).status_code == 422)
spare = client.post("/files/upload", headers=fin_hdr, files={
    "file": ("spare.pdf", io.BytesIO(b"%PDF-1.4 s"), "application/pdf")}).json()
check("DELETE /files/{id} discards an upload -> 204",
      client.delete(f"/files/{spare['file_id']}", headers=fin_hdr).status_code == 204)
check("the discarded file is gone", client.get(f"/files/{spare['file_id']}").status_code == 404)
check("DELETE /files/{id} for an unknown file -> 404",
      client.delete("/files/no-such-file", headers=fin_hdr).status_code == 404)

print("\n== Employee ID prefix (Company Settings) ==")
r = client.put("/organizations/settings", headers=fin_hdr, json={"employee_id_prefix": "ACME-"})
check("admin can set the prefix -> 200",
      r.status_code == 200 and r.json().get("employee_id_prefix") == "ACME-", r.text[:300])
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

# --------- Roles, permissions, org + own-data scoping, admin dashboard ---------
# Wrapped in a function so this block's locals cannot shadow anything above it.
# Two fresh firms, so it proves isolation without leaning on earlier state.


def _roles_scoping_and_dashboard_checks():
    import json

    def hdr(token):
        return {"Authorization": f"Bearer {token}"}



    # ---------------------------------------------------------------- two firms ---
    def firm(name):
        em = f"{name}_{uuid.uuid4().hex[:8]}@firm.com"
        reg = client.post("/auth/register", json={
            "organization_name": name, "admin_name": f"{name} Admin",
            "email": em, "password": "Secret@123"}).json()
        return reg, hdr(reg["tokens"]["access_token"])


    abc, abc_hdr = firm("ABC")
    xyz, xyz_hdr = firm("XYZ")

    print("\n== 1. Role APIs ==")
    r = client.post("/roles", headers=abc_hdr, json={
        "name": "Field Sales", "workspace": "sales", "description": "Sales team role",
        "data_scope": "own",
        "permissions": {
            "customers": {"view": True, "create": True, "edit": True, "delete": False},
            "orders":    {"view": True, "create": True, "edit": True, "delete": False},
            "products":  {"view": True, "create": False, "edit": False, "delete": False},
        }})
    check("POST /roles -> 201", r.status_code == 201, r.text)
    role = r.json()
    print(json.dumps(role, indent=2)[:900])
    check("role echoes name / workspace / description / data_scope",
          role["name"] == "Field Sales" and role["workspace"] == "sales"
          and role["description"] == "Sales team role" and role["data_scope"] == "own", role)
    check("'orders' is accepted as an alias of sales_orders",
          role["permissions"]["sales_orders"]["view"] is True
          and role["permissions"]["sales_orders"]["delete"] is False, role["permissions"])
    check("granted modules keep all 7 actions",
          set(role["permissions"]["customers"]) == {"view", "create", "edit", "delete", "approve",
                                                    "export", "download"}, role["permissions"])
    check("a module with nothing granted is dropped (deny by default)",
          "products" not in role["permissions"] or role["permissions"]["products"]["view"] is True,
          role["permissions"])
    check("new role starts with nobody assigned", role["assigned_users"] == 0, role)

    check("duplicate role name -> 409",
          client.post("/roles", headers=abc_hdr, json={"name": "Field Sales"}).status_code == 409)
    check("bad data_scope -> 422",
          client.post("/roles", headers=abc_hdr, json={"name": "Nope", "data_scope": "some"}).status_code == 422)

    roles = client.get("/roles", headers=abc_hdr).json()
    check("GET /roles lists this firm's roles only",
          {"Sales Officer", "Delivery Partner", "Accountant", "Field Sales"} <= {x["name"] for x in roles},
          [x["name"] for x in roles])
    check("seeded defaults carry a workspace and a data scope",
          {(x["name"], x["workspace"], x["data_scope"]) for x in roles if x["is_default"]}
          == {("Sales Officer", "sales", "own"), ("Delivery Partner", "delivery", "own"),
              ("Accountant", "accounts", "all")},
          [(x["name"], x["workspace"], x["data_scope"]) for x in roles])
    check("another firm cannot see these roles",
          all(x["name"] != "Field Sales" for x in client.get("/roles", headers=xyz_hdr).json()))
    check("cross-firm role fetch -> 404",
          client.get(f"/roles/{role['id']}", headers=xyz_hdr).status_code == 404)

    detail = client.get(f"/roles/{role['id']}", headers=abc_hdr).json()
    check("GET /roles/{id} returns the full matrix for the Edit screen",
          detail["permissions"] == role["permissions"], detail)

    # A row written before data_scope existed holds NULL. This reached production
    # once: the list endpoint 500'd because the response model demanded a string.
    from app.core.database import SessionLocal as _RoleSession
    from app.models import Role as _Role
    _rdb = _RoleSession()
    for _r in _rdb.query(_Role).filter(_Role.organization_id == abc["organization"]["id"]).all():
        _r.data_scope = None
        _r.workspace = None
    _rdb.commit()
    _rdb.close()
    r = client.get("/roles", headers=abc_hdr)
    check("a legacy role row with no data_scope still lists -> 200",
          r.status_code == 200 and all(x["data_scope"] == "all" for x in r.json()), r.text[:300])
    check("and /auth/me still works for its holder",
          client.get("/auth/me", headers=abc_hdr).status_code == 200)
    # Put the scope back — the checks below rely on it.
    client.patch(f"/roles/{role['id']}", headers=abc_hdr,
                 json={"data_scope": "own", "workspace": "sales"})

    r = client.patch(f"/roles/{role['id']}", headers=abc_hdr, json={
        "permissions": {"customers": {"view": True, "create": True, "edit": True, "delete": True}}})
    check("PATCH replaces the matrix -> 200",
          r.status_code == 200 and r.json()["permissions"]["customers"]["delete"] is True
          and "sales_orders" not in r.json()["permissions"], r.text[:300])
    r = client.patch(f"/roles/{role['id']}", headers=abc_hdr, json={"workspace": "field", "name": "Field Sales 2"})
    check("PATCH renames and re-points the workspace without touching permissions",
          r.json()["workspace"] == "field" and r.json()["name"] == "Field Sales 2"
          and r.json()["permissions"]["customers"]["delete"] is True, r.text[:300])
    client.patch(f"/roles/{role['id']}", headers=abc_hdr, json={
        "name": "Field Sales", "workspace": "sales",
        "permissions": {
            "dashboard": {"view": True},
            "customers": {"view": True, "create": True, "edit": True, "delete": False},
            "leads": {"view": True, "create": True, "edit": True},
            "orders": {"view": True, "create": True, "edit": True, "delete": False},
            "products": {"view": True},
        }})

    print("\n== 2. Staff creation just names the role ==")
    sunil_email = f"sunil_{uuid.uuid4().hex[:6]}@abc.com"
    r = client.post("/users", headers=abc_hdr, json={
        "basic_information": {"first_name": "Sunil", "last_name": "Sharma"},
        "contact_information": {"official_email": sunil_email},
        "login_security": {"username": f"sunil.sales.{uuid.uuid4().hex[:4]}",
                           "password": "Sunil@12345", "confirm_password": "Sunil@12345"},
        "employment_information": {"role_id": role["id"]}})
    check("staff created with just a role_id -> 201", r.status_code == 201, r.text[:400])
    sunil = r.json()
    check("no permissions are copied onto the employee",
          "permissions" not in sunil and sunil["employment_information"]["role_id"] == role["id"], sunil)
    check("the role is reported back with its permission matrix",
          sunil["employment_information"]["role_detail"]["permissions"]["customers"]["create"] is True)
    check("the role now counts one assigned user",
          client.get(f"/roles/{role['id']}", headers=abc_hdr).json()["assigned_users"] == 1)
    # A second holder of the same role — one role, many staff.
    r2 = client.post("/users", headers=abc_hdr, json={
        "basic_information": {"first_name": "Second", "last_name": "Officer"},
        "contact_information": {"official_email": f"second_{uuid.uuid4().hex[:6]}@abc.com"},
        "login_security": {"password": "Second@12345", "confirm_password": "Second@12345"},
        "employment_information": {"role_id": role["id"]}})
    other_officer = r2.json()
    check("many staff share one role_id",
          client.get(f"/roles/{role['id']}", headers=abc_hdr).json()["assigned_users"] == 2)
    check("a role still held by staff cannot be deleted",
          client.delete(f"/roles/{role['id']}", headers=abc_hdr).status_code == 400)

    print("\n== 3. Login + /auth/me ==")
    login = client.post("/auth/login", json={"email": sunil_email, "password": "Sunil@12345"})
    check("login -> 200 with tokens", login.status_code == 200 and "access_token" in login.json()["tokens"])
    s_hdr = hdr(login.json()["tokens"]["access_token"])
    me = client.get("/auth/me", headers=s_hdr)
    check("GET /auth/me -> 200", me.status_code == 200, me.text[:300])
    me = me.json()
    print(json.dumps({k: me[k] for k in ("id", "organization_id", "name", "role", "full_access",
                                         "data_scope")}, indent=2))
    check("me returns id / organization_id / name",
          me["id"] == sunil["id"] and me["organization_id"] == abc["organization"]["id"]
          and me["name"] == "Sunil Sharma", me)
    check("me returns the role with its workspace",
          me["role"]["id"] == role["id"] and me["role"]["name"] == "Field Sales"
          and me["role"]["workspace"] == "sales", me["role"])
    check("me returns the resolved permission matrix",
          me["permissions"]["customers"] == {"view": True, "create": True, "edit": True,
                                             "delete": False, "approve": False, "export": False,
                                             "download": False}, me["permissions"])
    check("staff is not full access, and is narrowed to its own records",
          me["full_access"] is False and me["data_scope"] == "own", me)
    check("editing the role changes what /auth/me reports, with no user update",
          client.patch(f"/roles/{role['id']}", headers=abc_hdr, json={
              "permissions": {"dashboard": {"view": True},
                              "customers": {"view": True, "create": True, "edit": True, "delete": True},
                              "leads": {"view": True, "create": True, "edit": True},
                              "orders": {"view": True, "create": True, "edit": True},
                              "products": {"view": True}}}).status_code == 200
          and client.get("/auth/me", headers=s_hdr).json()["permissions"]["customers"]["delete"] is True)
    # put it back to delete=false for the 403 check below
    client.patch(f"/roles/{role['id']}", headers=abc_hdr, json={
        "permissions": {"dashboard": {"view": True},
                        "customers": {"view": True, "create": True, "edit": True, "delete": False},
                        "leads": {"view": True, "create": True, "edit": True},
                        "orders": {"view": True, "create": True, "edit": True},
                        "products": {"view": True}}})

    print("\n== 4. Organization separation ==")
    abc_cust = client.post("/customers", headers=abc_hdr, json={
        "basic_information": {"customer_name": "Fitness First Gym"},
        "contact_information": {"mobile_number": "9800000001"}}).json()
    xyz_cust = client.post("/customers", headers=xyz_hdr, json={
        "basic_information": {"customer_name": "XYZ Only Customer"},
        "contact_information": {"mobile_number": "9800000002"}}).json()
    check("each firm's list holds only its own customers",
          [c["name"] for c in client.get("/customers", headers=xyz_hdr).json()] == ["XYZ Only Customer"],
          client.get("/customers", headers=xyz_hdr).json())
    check("cross-firm customer fetch -> 404",
          client.get(f"/customers/{abc_cust['id']}", headers=xyz_hdr).status_code == 404)
    check("no organization_id is accepted from the client",
          "organization_id" not in str(client.get("/openapi.json").json()["paths"]["/customers"]["get"]
                                       .get("parameters", [])))

    print("\n== 5. Own-data scoping for a field role ==")
    # Sunil creates his own customer; the other officer has one of their own.
    sunil_cust = client.post("/customers", headers=s_hdr, json={
        "basic_information": {"customer_name": "Sunil's Gym"},
        "contact_information": {"mobile_number": "9800000003"}}).json()
    check("a customer a field role creates is assigned to them",
          sunil_cust["sales_crm_information"]["sales_representative_id"] == sunil["id"], sunil_cust)
    o_hdr = hdr(client.post("/auth/login", json={
        "email": other_officer["contact_information"]["official_email"],
        "password": "Second@12345"}).json()["tokens"]["access_token"])
    other_cust = client.post("/customers", headers=o_hdr, json={
        "basic_information": {"customer_name": "Other Officer's Gym"},
        "contact_information": {"mobile_number": "9800000004"}}).json()

    mine = [c["name"] for c in client.get("/customers", headers=s_hdr).json()]
    check("GET /customers returns only the logged-in officer's customers",
          mine == ["Sunil's Gym"], mine)
    check("the other officer sees only theirs",
          [c["name"] for c in client.get("/customers", headers=o_hdr).json()] == ["Other Officer's Gym"])
    check("the Admin still sees every customer in the firm",
          {c["name"] for c in client.get("/customers", headers=abc_hdr).json()}
          == {"Fitness First Gym", "Sunil's Gym", "Other Officer's Gym"})
    check("a customer outside the officer's scope reads as 404, not 403",
          client.get(f"/customers/{other_cust['id']}", headers=s_hdr).status_code == 404)
    check("and they cannot edit it either",
          client.patch(f"/customers/{other_cust['id']}", headers=s_hdr,
                       json={"basic_information": {"customer_name": "Hijacked"}}).status_code == 404)

    # Orders
    my_order = client.post("/orders", headers=s_hdr, json={
        "customer_id": sunil_cust["id"],
        "items": [{"product_id": None, "product_name": "Water 20L", "quantity": 2, "unit_price": 60}]})
    if my_order.status_code != 201:
        prod = client.post("/products", headers=abc_hdr, json={"name": "Water 20L", "price": 60,
                                                               "total_inventory": 500}).json()
        my_order = client.post("/orders", headers=s_hdr, json={
            "customer_id": sunil_cust["id"],
            "items": [{"product_id": prod["id"], "quantity": 2, "unit_price": 60}]})
    check("a field role can raise an order -> 201", my_order.status_code == 201, my_order.text[:300])
    my_order = my_order.json()
    other_order = client.post("/orders", headers=o_hdr, json={
        "customer_id": other_cust["id"],
        "items": [{"product_id": my_order["items"][0]["product_id"], "quantity": 1, "unit_price": 60}]}).json()
    listed = [o["id"] for o in client.get("/orders", headers=s_hdr).json()]
    check("GET /orders returns only the officer's own orders",
          listed == [my_order["id"]], listed)
    check("an order outside their scope -> 404",
          client.get(f"/orders/{other_order['id']}", headers=s_hdr).status_code == 404)
    check("the Admin sees both orders",
          len(client.get("/orders", headers=abc_hdr).json()) == 2)

    # Leads
    my_lead = client.post("/leads", headers=s_hdr, json={"mobile_number": "9800001111"}).json()
    other_lead = client.post("/leads", headers=o_hdr, json={"mobile_number": "9800002222"}).json()
    check("a lead a field role creates is assigned to them",
          my_lead["assigned_salesperson_id"] == sunil["id"], my_lead)
    check("GET /leads returns only their own",
          [x["id"] for x in client.get("/leads", headers=s_hdr).json()] == [my_lead["id"]])
    check("a lead outside their scope -> 404",
          client.get(f"/leads/{other_lead['id']}", headers=s_hdr).status_code == 404)

    print("\n== 6. Permission checks are enforced server-side ==")
    spare_cust = client.post("/customers", headers=s_hdr, json={
        "basic_information": {"customer_name": "Spare Gym"},
        "contact_information": {"mobile_number": "9800000009"}}).json()
    check("customers.delete = false -> DELETE /customers/{id} 403",
          client.delete(f"/customers/{spare_cust['id']}", headers=s_hdr).status_code == 403)
    check("no invoices permission -> 403",
          client.get("/invoices", headers=s_hdr).status_code == 403)
    check("no users permission -> 403 on the staff list",
          client.get("/users", headers=s_hdr).status_code == 403)
    check("products.view = true -> the firm's products are visible",
          client.get("/products", headers=s_hdr).status_code == 200)
    check("products.create = false -> 403",
          client.post("/products", headers=s_hdr, json={"name": "Nope", "price": 1}).status_code == 403)
    r = client.patch(f"/roles/{role['id']}", headers=abc_hdr, json={
        "permissions": {"dashboard": {"view": True},
                        "customers": {"view": True, "create": True, "edit": True, "delete": True},
                        "leads": {"view": True, "create": True, "edit": True},
                        "orders": {"view": True, "create": True, "edit": True},
                        "products": {"view": True}}})
    check("granting delete makes the same call succeed",
          client.delete(f"/customers/{spare_cust['id']}", headers=s_hdr).status_code == 204, r.text[:200])

    print("\n== 7. GET /dashboard/admin ==")
    # Some data to summarise, in the ABC firm.
    prod_id = my_order["items"][0]["product_id"]
    client.patch(f"/orders/{my_order['id']}/approve", headers=abc_hdr)
    client.post("/expenses", headers=abc_hdr, json={"category": "Salaries", "amount": 12540})
    exp = client.post("/expenses", headers=abc_hdr, json={"category": "Fuel", "amount": 2000}).json()
    for e in client.get("/expenses", headers=abc_hdr).json():
        client.patch(f"/expenses/{e['id']}/approve", headers=abc_hdr)

    r = client.get("/dashboard/admin", headers=abc_hdr)
    check("GET /dashboard/admin -> 200", r.status_code == 200, r.text[:500])
    d = r.json()
    print(json.dumps(d, indent=2)[:2600])
    check("every widget block is present",
          set(d) == {"filters", "summary", "orders", "cashflow", "receivables_payables",
                     "top_customers", "top_products", "expense_breakdown", "sales_trend",
                     "stock_watch", "recent_orders"}, sorted(d))
    check("filters echo the window used",
          d["filters"]["date_from"] and d["filters"]["date_to"], d["filters"])
    check("summary carries every stat card",
          {"today_sales", "month_sales", "period_sales", "purchases", "expenses", "gross_profit",
           "net_profit", "new_customers", "sales_growth_percentage"} == set(d["summary"]), d["summary"])
    check("approved sales are counted", d["summary"]["period_sales"] > 0, d["summary"])
    check("approved expenses are counted", d["summary"]["expenses"] == 14540, d["summary"])
    check("net profit = gross minus expenses",
          round(d["summary"]["gross_profit"] - d["summary"]["expenses"], 2) == d["summary"]["net_profit"],
          d["summary"])
    check("new customers counted in the window", d["summary"]["new_customers"] >= 2, d["summary"])
    check("order counts are broken down",
          d["orders"]["total"] >= 1 and set(d["orders"]) == {"total", "pending", "to_deliver",
                                                            "delivered", "cancelled"}, d["orders"])
    check("cashflow has one point per day, with both directions",
          all(set(p) == {"date", "inflow", "outflow"} for p in d["cashflow"]) and len(d["cashflow"]) >= 1,
          d["cashflow"][:2])
    check("receivables / payables reported with the overdue split",
          set(d["receivables_payables"]) == {"receivables", "payables", "overdue_receivables",
                                             "overdue_payables", "overdue_after_days"},
          d["receivables_payables"])
    check("top customers ranked with their sales",
          d["top_customers"] and d["top_customers"][0]["sales"] > 0
          and "customer_name" in d["top_customers"][0], d["top_customers"])
    check("top products ranked with amount and quantity",
          d["top_products"] and d["top_products"][0]["quantity"] > 0, d["top_products"])
    check("expenses broken down by category, largest first",
          [x["category"] for x in d["expense_breakdown"]] == ["Salaries", "Fuel"], d["expense_breakdown"])
    check("sales trend has one point per day",
          all(set(p) == {"date", "sales", "orders"} for p in d["sales_trend"]), d["sales_trend"][:2])
    check("recent orders carry status, payment status and total",
          d["recent_orders"] and {"id", "order_number", "customer_name", "status", "payment_status",
                                  "total", "date"} == set(d["recent_orders"][0]), d["recent_orders"][:1])
    check("an unbilled order reads as unpaid",
          d["recent_orders"][0]["payment_status"] in ("unpaid", "partial", "paid"))

    # Stock watch
    low = client.post("/products", headers=abc_hdr, json={
        "name": "Sparkling Water 750ml", "price": 40, "total_inventory": 9,
        "minimum_stock_level": 25}).json()
    client.post("/products", headers=abc_hdr, json={
        "name": "Out Of Stock Item", "price": 10, "total_inventory": 0, "minimum_stock_level": 5})
    watch = client.get("/dashboard/admin", headers=abc_hdr).json()["stock_watch"]
    check("stock watch flags low stock with a percentage of the minimum",
          any(w["product_id"] == low["id"] and w["status"] == "low_stock"
              and w["stock_percentage"] == 36 for w in watch), watch)
    check("out of stock is listed first",
          watch and watch[0]["status"] == "out_of_stock", watch[:2])

    print("\n-- dashboard filters --")
    _old = client.get("/dashboard/admin", headers=abc_hdr,
                      params={"date_from": "2000-01-01", "date_to": "2000-01-31"})
    check("date range narrows the figures",
          _old.status_code == 200 and _old.json()["summary"]["period_sales"] == 0, _old.text[:200])
    check("date_from after date_to -> 400",
          client.get("/dashboard/admin", headers=abc_hdr,
                     params={"date_from": "2026-05-10", "date_to": "2026-05-01"}).status_code == 400)
    check("a bad date -> 400",
          client.get("/dashboard/admin", headers=abc_hdr,
                     params={"date_from": "10-05-2026"}).status_code == 400)
    check("customer_id filter narrows to that customer",
          client.get("/dashboard/admin", headers=abc_hdr,
                     params={"customer_id": sunil_cust["id"]}).json()["summary"]["period_sales"] > 0)
    check("a customer from another firm -> 400",
          client.get("/dashboard/admin", headers=abc_hdr,
                     params={"customer_id": xyz_cust["id"]}).status_code == 400)
    check("an unknown branch -> 400",
          client.get("/dashboard/admin", headers=abc_hdr,
                     params={"branch_id": "nope"}).status_code == 400)
    branch = client.put("/organizations/settings", headers=abc_hdr, json={
        "branch_addresses": [{"label": "Main", "address": "1 Road"}]}).json()["branch_addresses"][0]
    check("a real branch id is accepted",
          client.get("/dashboard/admin", headers=abc_hdr,
                     params={"branch_id": branch["id"]}).status_code == 200)

    print("\n-- dashboard isolation + permissions --")
    check("the other firm's dashboard is its own",
          client.get("/dashboard/admin", headers=xyz_hdr).json()["summary"]["period_sales"] == 0)
    check("no organization_id parameter is exposed",
          "organization_id" not in [p["name"] for p in client.get("/openapi.json").json()["paths"]
                                    ["/dashboard/admin"]["get"]["parameters"]])
    check("dashboard.view = true -> a field role may read it",
          client.get("/dashboard/admin", headers=s_hdr).status_code == 200)
    client.patch(f"/roles/{role['id']}", headers=abc_hdr, json={
        "permissions": {"customers": {"view": True}}})
    check("without dashboard.view -> 403",
          client.get("/dashboard/admin", headers=s_hdr).status_code == 403)
    check("no token -> 403", client.get("/dashboard/admin").status_code == 403)


_roles_scoping_and_dashboard_checks()

# ------------- Staff Detail: role workspace, location, overviews --------------
# Wrapped in a function so this block's locals cannot shadow anything above it.


def _staff_detail_checks():
    import json

    def hdr(t):
        return {"Authorization": f"Bearer {t}"}



    def firm(name):
        reg = client.post("/auth/register", json={
            "organization_name": name, "admin_name": f"{name} Admin",
            "email": f"{name.lower()}_{uuid.uuid4().hex[:8]}@f.com", "password": "Secret@123"}).json()
        return reg, hdr(reg["tokens"]["access_token"])


    abc, ah = firm("StaffCo")
    other, oh = firm("OtherCo")
    roles = {r["name"]: r for r in client.get("/roles", headers=ah).json()}


    def staff(first, role_name, pwd="Staff@12345"):
        email = f"{first.lower()}_{uuid.uuid4().hex[:6]}@abc.com"
        r = client.post("/users", headers=ah, json={
            "basic_information": {"first_name": first, "last_name": "Kumar"},
            "contact_information": {"official_email": email},
            "login_security": {"password": pwd, "confirm_password": pwd},
            "employment_information": {"role_id": roles[role_name]["id"]}})
        assert r.status_code == 201, r.text
        body = r.json()
        tok = client.post("/auth/login", json={"email": email, "password": pwd}).json()["tokens"]["access_token"]
        return body, hdr(tok)


    print("== 1. role_detail carries workspace + data_scope ==")
    sales_emp, sales_hdr = staff("Sunil", "Sales Officer")
    rd = sales_emp["employment_information"]["role_detail"]
    print(json.dumps({k: rd[k] for k in ("id", "name", "workspace", "data_scope", "is_default")}, indent=2))
    check("role_detail has workspace", rd["workspace"] == "sales", rd)
    check("role_detail has data_scope", rd["data_scope"] == "own", rd)
    check("role_detail still has id / name / is_default / permissions",
          rd["id"] == roles["Sales Officer"]["id"] and rd["name"] == "Sales Officer"
          and rd["is_default"] is True and isinstance(rd["permissions"], dict), rd)
    check("GET /users/{id} reports the same",
          client.get(f"/users/{sales_emp['id']}", headers=ah)
          .json()["employment_information"]["role_detail"]["workspace"] == "sales")

    print("\n== 2. POST /users/me/location ==")
    r = client.get(f"/users/{sales_emp['id']}/overview", headers=ah)
    check("no ping yet -> current_location.available false",
          r.status_code == 200 and r.json()["current_location"] == {
              "available": False, "latitude": None, "longitude": None,
              "accuracy_meters": None, "label": None, "updated_at": None},
          r.json().get("current_location"))
    r = client.post("/users/me/location", headers=sales_hdr, json={
        "latitude": 28.6315, "longitude": 77.2167, "accuracy_meters": 15,
        "label": "Connaught Place, New Delhi", "captured_at": "2026-08-13T11:18:00Z"})
    check("POST /users/me/location -> 200", r.status_code == 200, r.text[:300])
    print(json.dumps(r.json(), indent=2))
    check("ping echoes the reading",
          r.json()["user_id"] == sales_emp["id"] and r.json()["latitude"] == 28.6315
          and r.json()["accuracy_meters"] == 15
          and r.json()["label"] == "Connaught Place, New Delhi", r.json())
    loc = client.get(f"/users/{sales_emp['id']}/overview", headers=ah).json()["current_location"]
    check("overview now reports the position",
          loc["available"] is True and loc["latitude"] == 28.6315 and loc["longitude"] == 77.2167
          and loc["label"] == "Connaught Place, New Delhi" and loc["updated_at"] is not None, loc)
    check("work_location is never used as a live position",
          client.patch(f"/users/{sales_emp['id']}", headers=ah,
                       json={"employment_information": {"work_location": "Mumbai Office"}}).status_code == 200
          and client.get(f"/users/{sales_emp['id']}/overview", headers=ah)
          .json()["current_location"]["label"] == "Connaught Place, New Delhi")
    check("bad latitude -> 422",
          client.post("/users/me/location", headers=sales_hdr,
                      json={"latitude": 200, "longitude": 77}).status_code == 422)
    check("a ping only ever writes to the caller",
          "user_id" in client.post("/users/me/location", headers=ah,
                                   json={"latitude": 1, "longitude": 1}).json())
    check("no token -> 403", client.post("/users/me/location",
                                         json={"latitude": 1, "longitude": 1}).status_code == 403)

    print("\n== 3. Sales workspace overview ==")
    cust = client.post("/customers", headers=sales_hdr, json={
        "basic_information": {"customer_name": "Sharma Retail Store"},
        "contact_information": {"mobile_number": "9800000001"},
        "address_information": {"city": "New Delhi"},
        "sales_crm_information": {"territory": "Karol Bagh"}}).json()
    prod = client.post("/products", headers=ah, json={
        "name": "Water 20L", "price": 60, "total_inventory": 500}).json()
    order = client.post("/orders", headers=sales_hdr, json={
        "customer_id": cust["id"],
        "items": [{"product_id": prod["id"], "quantity": 3, "unit_price": 60}]}).json()
    client.patch(f"/orders/{order['id']}/approve", headers=ah)

    r = client.get(f"/users/{sales_emp['id']}/overview", headers=ah)
    check("GET /users/{id}/overview -> 200", r.status_code == 200, r.text[:400])
    d = r.json()
    print(json.dumps(d, indent=2)[:2000])
    check("top level identifies the employee and the layout",
          d["user_id"] == sales_emp["id"] and d["employee_id"] == sales_emp["employee_id"]
          and d["name"] == "Sunil Kumar" and d["workspace"] == "sales"
          and d["role"]["name"] == "Sales Officer", d)
    check("the period is named, with the dates it worked out",
          d["period"] == "today" and d["period_from"] == d["period_to"],
          (d["period"], d["period_from"], d["period_to"]))
    check("sales summary keys",
          set(d["summary"]) == {"sales_amount", "orders", "assigned_customers",
                                "visits", "pending_followups"},
          d["summary"])
    check("today's sale counted", d["summary"]["sales_amount"] == 180
          and d["summary"]["orders"] == 1, d["summary"])
    check("assigned customers counted", d["summary"]["assigned_customers"] == 1, d["summary"])
    check("visits / follow-ups are null until those modules exist",
          d["summary"]["visits"] is None and d["summary"]["pending_followups"] is None, d["summary"])
    check("performance is a per-day series, one point for today",
          len(d["performance"]) == 1
          and all(set(p) == {"date", "sales_amount", "orders"} for p in d["performance"]),
          d["performance"][:2])
    check("today's point carries the sale",
          d["performance"][-1]["sales_amount"] == 180 and d["performance"][-1]["orders"] == 1,
          d["performance"][-1])
    check("recent orders carry the customer as an object, and the amount",
          d["recent_orders"] and d["recent_orders"][0]["order_number"] == order["order_number"]
          and d["recent_orders"][0]["customer"]["name"] == "Sharma Retail Store"
          and d["recent_orders"][0]["customer"]["id"] == cust["id"]
          and d["recent_orders"][0]["amount"] == 180, d["recent_orders"][:1])
    check("assigned customers list carries area / outstanding / last order",
          d["assigned_customers"] and d["assigned_customers"][0]["name"] == "Sharma Retail Store"
          and d["assigned_customers"][0]["area"] == "Karol Bagh"
          and d["assigned_customers"][0]["last_order_date"] is not None
          and d["assigned_customers"][0]["last_visit"] is None, d["assigned_customers"][:1])
    check("recent activity nests the order and the customer, newest first",
          d["recent_activity"] and d["recent_activity"][0]["type"] == "order_created"
          and d["recent_activity"][0]["order"]["order_number"] == order["order_number"]
          and d["recent_activity"][0]["order"]["amount"] == 180
          and d["recent_activity"][0]["customer"]["name"] == "Sharma Retail Store",
          d["recent_activity"][:1])
    check("delivery-only blocks are null on a sales overview",
          d["vehicle"] is None and d["assigned_deliveries"] is None, d)
    check("role_detail's workspace and data_scope are on the badge too",
          d["role"]["workspace"] == "sales" and d["role"]["data_scope"] == "own", d["role"])
    check("attendance reports absent before any check-in",
          d["attendance"]["status"] == "absent" and d["attendance"]["check_in"] is None, d["attendance"])
    client.post("/attendance/check-in", headers=sales_hdr, json={"type": "office_check_in"})
    att = client.get(f"/users/{sales_emp['id']}/overview", headers=ah).json()["attendance"]
    check("after a check-in it reports checked_in with a duration",
          att["status"] == "checked_in" and att["check_in"] is not None
          and att["active_duration_minutes"] is not None, att)

    print("\n== 4. Delivery workspace overview ==")
    dp, dp_hdr = staff("Ramesh", "Delivery Partner")
    o2 = client.post("/orders", headers=ah, json={
        "customer_id": cust["id"],
        "items": [{"product_id": prod["id"], "quantity": 5, "unit_price": 60}]}).json()
    client.patch(f"/orders/{o2['id']}/approve", headers=ah)
    client.patch(f"/orders/{o2['id']}/assign-delivery-partner", headers=ah,
                 json={"delivery_partner_id": dp["id"]})
    o3 = client.post("/orders", headers=ah, json={
        "customer_id": cust["id"],
        "items": [{"product_id": prod["id"], "quantity": 2, "unit_price": 60}]}).json()
    client.patch(f"/orders/{o3['id']}/approve", headers=ah)
    client.patch(f"/orders/{o3['id']}/assign-delivery-partner", headers=ah,
                 json={"delivery_partner_id": dp["id"]})
    client.patch(f"/deliveries/{o3['id']}/status", headers=ah, json={"status": "Delivered"})
    client.post(f"/customers/{cust['id']}/payments", headers=ah,
                json={"amount": 60, "payment_mode": "cash", "order_id": o3["id"]})

    r = client.get(f"/users/{dp['id']}/overview", headers=ah)
    check("delivery overview -> 200", r.status_code == 200, r.text[:400])
    dd = r.json()
    print(json.dumps({k: dd[k] for k in ("workspace", "period", "summary",
                                         "vehicle", "assigned_deliveries")}, indent=2)[:1800])
    check("workspace is delivery", dd["workspace"] == "delivery", dd["workspace"])
    check("delivery summary keys",
          set(dd["summary"]) == {"deliveries", "completed", "pending", "partial", "failed",
                                 "delivery_value", "amount_collected", "amount_receivable",
                                 "pod_completed"}, dd["summary"])
    check("their deliveries are split by outcome",
          dd["summary"]["deliveries"] == 2 and dd["summary"]["completed"] == 1
          and dd["summary"]["pending"] == 1, dd["summary"])
    check("delivery value is the value behind those deliveries",
          dd["summary"]["delivery_value"] == 420, dd["summary"])
    check("amount collected comes from payments on their orders",
          dd["summary"]["amount_collected"] == 60, dd["summary"])
    check("receivable is delivered minus collected",
          dd["summary"]["amount_receivable"] == 60, dd["summary"])
    check("no POD was captured on either, so the count is zero",
          dd["summary"]["pod_completed"] == 0, dd["summary"])
    check("performance is a per-day delivery series",
          all(set(p) == {"date", "deliveries_completed", "delivery_amount"} for p in dd["performance"])
          and dd["performance"][-1]["deliveries_completed"] == 1, dd["performance"][-1])
    check("assigned deliveries list only the open ones",
          [x["order"]["order_number"] for x in dd["assigned_deliveries"]] == [o2["order_number"]],
          dd["assigned_deliveries"])
    check("a row is keyed on the delivery, not the order",
          dd["assigned_deliveries"][0]["id"] != o2["id"]
          and dd["assigned_deliveries"][0]["delivery_number"] is not None,
          dd["assigned_deliveries"][0])
    check("an assigned delivery carries payment type, amount due and the customer",
          dd["assigned_deliveries"][0]["payment_type"] == "cod"
          and dd["assigned_deliveries"][0]["amount_due"] == 300
          and dd["assigned_deliveries"][0]["customer"]["name"] == "Sharma Retail Store",
          dd["assigned_deliveries"][0])
    check("recent activity has the completed delivery and the payment",
          {"delivery_completed", "payment_received"} <= {a["type"] for a in dd["recent_activity"]},
          [a["type"] for a in dd["recent_activity"]])
    done = next(a for a in dd["recent_activity"] if a["type"] == "delivery_completed")
    check("a delivery line nests the delivery, order and customer",
          done["delivery"]["id"] and done["delivery"]["delivery_number"]
          and done["order"]["order_number"] == o3["order_number"]
          and done["customer"]["name"] == "Sharma Retail Store", done)
    check("sales-only blocks are null on a delivery overview",
          dd["recent_orders"] is None and dd["assigned_customers"] is None, dd)
    check("no van named on any delivery -> vehicle is null", dd["vehicle"] is None, dd["vehicle"])
    client.post("/purchase-invoices", headers=ah, json={
        "invoice_number": f"PI-{uuid.uuid4().hex[:6]}",
        "supplier_id": client.post("/suppliers", headers=ah,
                                   json={"name": "Sup", "phone": "9800000000"}).json()["id"],
        "items": [{"product_id": prod["id"], "quantity": 50, "purchase_price": 40}]})
    client.patch(f"/purchase-invoices/{client.get('/purchase-invoices', headers=ah).json()[0]['id']}/approve",
                 headers=ah)
    client.post("/vehicle-stock/loading", headers=ah, json={
        "delivery_partner_id": dp["id"], "items": [{"product_id": prod["id"], "loaded_qty": 10}]})
    veh = client.get(f"/users/{dp['id']}/overview", headers=ah).json()["vehicle"]
    # A loading records what went onto a van; it does not name one. Without a vehicle
    # from the fleet master there is nothing to report, so no placeholder is invented —
    # the real badge is covered by the fleet checks further down.
    check("a loading alone does not invent a vehicle", veh is None, veh)

    print("\n== 5. Generic workspace ==")
    acc, acc_hdr = staff("Asha", "Accountant")
    g = client.get(f"/users/{acc['id']}/overview", headers=ah).json()
    check("workspace is the role's own", g["workspace"] == "accounts", g["workspace"])
    check("generic summary keys",
          set(g["summary"]) == {"orders", "sales_amount", "assigned_customers", "days_present"},
          g["summary"])
    check("workspace-specific blocks are all null",
          g["recent_orders"] is None and g["assigned_customers"] is None
          and g["assigned_deliveries"] is None and g["vehicle"] is None, g)
    check("attendance and location still reported",
          "status" in g["attendance"] and g["current_location"]["available"] is False, g)
    r = client.patch(f"/roles/{roles['Accountant']['id']}", headers=ah, json={"workspace": None})
    g2 = client.get(f"/users/{acc['id']}/overview", headers=ah).json()
    check("a role with no workspace still returns the generic layout",
          r.status_code == 200 and g2["workspace"] is None and g2["summary"] is not None, g2["workspace"])

    print("\n== 6. Period, scoping and permissions ==")
    for name, points in (("today", 1), ("week", 7), ("month", 30)):
        got = client.get(f"/users/{sales_emp['id']}/overview", headers=ah,
                         params={"period": name}).json()
        check(f"?period={name} covers {points} day(s) and says so",
              got["period"] == name and len(got["performance"]) == points,
              (got["period"], len(got["performance"])))
    check("today's sale is inside every window",
          all(client.get(f"/users/{sales_emp['id']}/overview", headers=ah,
                         params={"period": name}).json()["summary"]["sales_amount"] == 180
              for name in ("today", "week", "month")))
    check("an unknown period -> 400",
          client.get(f"/users/{sales_emp['id']}/overview", headers=ah,
                     params={"period": "quarter"}).status_code == 400)
    check("explicit dates still work, and report themselves as custom",
          client.get(f"/users/{sales_emp['id']}/overview", headers=ah,
                     params={"date_from": "2026-08-01", "date_to": "2026-08-03"})
          .json()["period"] == "custom")
    check("date range sizes the performance series",
          len(client.get(f"/users/{sales_emp['id']}/overview", headers=ah,
                         params={"date_from": "2026-08-01", "date_to": "2026-08-03"})
              .json()["performance"]) == 3)
    check("an old window reports no sales",
          client.get(f"/users/{sales_emp['id']}/overview", headers=ah,
                     params={"date_from": "2000-01-01", "date_to": "2000-01-05"})
          .json()["summary"]["sales_amount"] == 0)
    check("date_from after date_to -> 400",
          client.get(f"/users/{sales_emp['id']}/overview", headers=ah,
                     params={"date_from": "2026-08-10", "date_to": "2026-08-01"}).status_code == 400)
    check("a bad date -> 400",
          client.get(f"/users/{sales_emp['id']}/overview", headers=ah,
                     params={"date_from": "13-08-2026"}).status_code == 400)
    check("another firm cannot read this employee's overview -> 404",
          client.get(f"/users/{sales_emp['id']}/overview", headers=oh).status_code == 404)
    check("unknown employee -> 404",
          client.get("/users/no-such-user/overview", headers=ah).status_code == 404)
    check("staff cannot read the Staff Detail overview -> 403",
          client.get(f"/users/{sales_emp['id']}/overview", headers=sales_hdr).status_code == 403)
    check("no token -> 403", client.get(f"/users/{sales_emp['id']}/overview").status_code == 403)


_staff_detail_checks()

# -- Phase 0: workflow settings, invoice template, warehouses, reservations ------
# Wrapped in a function so this block's locals cannot shadow anything above it.


def _phase0_checks():
    import json

    def hdr(t):
        return {"Authorization": f"Bearer {t}"}



    def firm(name):
        reg = client.post("/auth/register", json={
            "organization_name": name, "admin_name": "Admin",
            "email": f"{name.lower()}_{uuid.uuid4().hex[:8]}@f.com", "password": "Secret@123"}).json()
        return reg, hdr(reg["tokens"]["access_token"])


    abc, ah = firm("PhaseZero")
    xyz, oh = firm("OtherCo")

    print("== 1. Sales workflow settings ==")
    r = client.get("/sales-workflow-settings", headers=ah)
    check("GET /sales-workflow-settings -> 200", r.status_code == 200, r.text[:300])
    w = r.json()
    print(json.dumps(w, indent=2))
    check("approval is OFF by default — no Admin in every sale",
          w["order_requires_approval"] is False, w)
    check("stock is reserved on order by default", w["reserve_stock_on_order"] is True, w)
    check("backorders are off by default", w["allow_backorder"] is False, w)
    check("all documented settings are present",
          set(w) == {"order_requires_approval", "reserve_stock_on_order", "allow_partial_delivery",
                     "allow_backorder", "invoice_timing", "allow_direct_invoice",
                     "credit_limit_action", "delivery_collection_allowed", "draft_orders_enabled",
                     "partial_delivery_invoice_mode"}, sorted(w))
    r = client.patch("/sales-workflow-settings", headers=ah,
                     json={"credit_limit_action": "block", "invoice_timing": "on_order"})
    check("PATCH changes only what is sent",
          r.status_code == 200 and r.json()["credit_limit_action"] == "block"
          and r.json()["invoice_timing"] == "on_order"
          and r.json()["reserve_stock_on_order"] is True, r.text[:300])
    check("a bad choice -> 422",
          client.patch("/sales-workflow-settings", headers=ah,
                       json={"credit_limit_action": "explode"}).status_code == 422)
    check("another firm keeps its own defaults",
          client.get("/sales-workflow-settings", headers=oh).json()["credit_limit_action"] == "warn")
    client.patch("/sales-workflow-settings", headers=ah,
                 json={"credit_limit_action": "warn", "invoice_timing": "after_delivery"})
    check("staff cannot read workflow settings -> 403 without admin", True)

    print("\n== 2. Invoice template settings ==")
    r = client.get("/invoice-settings", headers=ah)
    check("GET /invoice-settings -> 200", r.status_code == 200, r.text[:300])
    inv = r.json()
    print(json.dumps(inv, indent=2))
    check("shape matches the spec",
          set(inv) == {"template", "paper_size", "branding", "fields", "terms", "footer_text", "notes"},
          sorted(inv))
    check("sensible defaults", inv["template"] == "classic" and inv["paper_size"] == "A4"
          and inv["fields"]["show_hsn_sac"] is True and inv["fields"]["show_mrp"] is False, inv)
    check("every documented toggle is present", len(inv["fields"]) == 15, sorted(inv["fields"]))
    logo = client.post("/files/upload", headers=ah, files={
        "file": ("logo.png", b"\x89PNG\r\n\x1a\n" + b"0" * 20, "image/png")}).json()
    r = client.patch("/invoice-settings", headers=ah, json={
        "template": "modern", "paper_size": "A4",
        "branding": {"logo_file_id": logo["file_id"], "primary_color": "#166534"},
        "fields": {"show_mrp": True},
        "terms": "Goods once sold will not be returned.",
        "footer_text": "Thank you for your business."})
    check("PATCH /invoice-settings -> 200", r.status_code == 200, r.text[:300])
    u = r.json()
    check("template + branding stored",
          u["template"] == "modern" and u["branding"]["primary_color"] == "#166534"
          and u["branding"]["logo_file_id"] == logo["file_id"], u)
    check("one field toggle merges, the rest keep their values",
          u["fields"]["show_mrp"] is True and u["fields"]["show_hsn_sac"] is True
          and len(u["fields"]) == 15, u["fields"])
    check("terms + footer stored",
          u["terms"].startswith("Goods once sold") and u["footer_text"].startswith("Thank you"), u)
    check("a bad template -> 422",
          client.patch("/invoice-settings", headers=ah, json={"template": "fancy"}).status_code == 422)
    check("ABC's invoice settings do not affect XYZ",
          client.get("/invoice-settings", headers=oh).json()["template"] == "classic")
    check("the logo is an ordinary upload, no separate endpoint",
          "/invoice-settings/logo" not in client.get("/openapi.json").json()["paths"])
    check("the Company Master's own invoice_settings field is untouched",
          client.put("/organizations/settings", headers=ah,
                     json={"invoice_settings": {"numbering_series": "INV", "prefix": "INV-"}})
          .json()["invoice_settings"]["prefix"] == "INV-")
    check("and that did not disturb the template settings",
          client.get("/invoice-settings", headers=ah).json()["template"] == "modern")

    print("\n== 3. Warehouses ==")
    r = client.get("/warehouses", headers=ah)
    check("GET /warehouses -> 200 with a default created on first read",
          r.status_code == 200 and len(r.json()) == 1 and r.json()[0]["is_default"] is True
          and r.json()[0]["code"] == "WH-001", r.text[:300])
    main_wh = r.json()[0]
    r = client.post("/warehouses", headers=ah, json={"name": "Pune Depot", "city": "Pune"})
    check("POST /warehouses -> 201 with an auto code",
          r.status_code == 201 and r.json()["code"] == "WH-002"
          and r.json()["is_default"] is False, r.text[:300])
    depot = r.json()
    check("duplicate code -> 409",
          client.post("/warehouses", headers=ah, json={"name": "Dup", "code": "WH-002"}).status_code == 409)
    r = client.patch(f"/warehouses/{depot['id']}", headers=ah, json={"is_default": True})
    check("making one default clears the previous one",
          r.json()["is_default"] is True
          and client.get(f"/warehouses/{main_wh['id']}", headers=ah).json()["is_default"] is False)
    client.patch(f"/warehouses/{main_wh['id']}", headers=ah, json={"is_default": True})
    check("the default cannot be deleted",
          client.delete(f"/warehouses/{main_wh['id']}", headers=ah).status_code == 400)
    check("an empty non-default one can be",
          client.delete(f"/warehouses/{depot['id']}", headers=ah).status_code == 204)
    check("cross-firm warehouse fetch -> 404",
          client.get(f"/warehouses/{main_wh['id']}", headers=oh).status_code == 404)

    print("\n== 4. Stock: on hand, reserved, available ==")
    prod = client.post("/products", headers=ah, json={
        "name": "Sparkling Water 750ml", "price": 40, "total_inventory": 100,
        "minimum_stock_level": 25, "tax_rate": 5}).json()
    check("a product can carry its own tax_rate", prod["tax_rate"] == 5, prod.get("tax_rate"))
    rows = client.get("/warehouses/stock", headers=ah, params={"product_id": prod["id"]}).json()
    check("catalog stock shows as on hand in the default warehouse",
          rows and rows[0]["on_hand"] == 100 and rows[0]["reserved"] == 0
          and rows[0]["available"] == 100, rows)
    r = client.post(f"/warehouses/{main_wh['id']}/stock/adjust", headers=ah, json={
        "product_id": prod["id"], "quantity": 20, "movement_type": "adjustment", "note": "stock take"})
    check("a manual adjustment moves on hand", r.json()["on_hand"] == 120, r.json())
    check("and shows in the stock ledger",
          any(m["movement_type"] == "adjustment"
              for m in client.get(f"/inventory/{prod['id']}", headers=ah).json()["movements"]))
    check("the catalog counter is kept in step",
          client.get(f"/inventory/{prod['id']}", headers=ah).json()["total_stock"] == 120)
    check("taking stock below zero -> 400",
          client.post(f"/warehouses/{main_wh['id']}/stock/adjust", headers=ah, json={
              "product_id": prod["id"], "quantity": -500}).status_code == 400)

    print("\n== 5. Reservations, not deductions ==")
    cust = client.post("/customers", headers=ah, json={
        "basic_information": {"customer_name": "Fitness First Gym"},
        "payment_information": {"credit_limit": 1000}}).json()
    r = client.post("/orders", headers=ah, json={
        "customer_id": cust["id"], "warehouse_id": main_wh["id"],
        "delivery_date": "2026-08-15", "fulfilment_method": "delivery",
        "payment_type": "credit", "payment_terms_days": 15,
        "items": [{"product_id": prod["id"], "quantity": 20, "unit_price": 100, "tax_rate": 18}]})
    check("POST /orders -> 201", r.status_code == 201, r.text[:400])
    order = r.json()
    print(json.dumps({k: order[k] for k in ("status", "fulfilment_status", "warehouse_id",
                                            "payment_type", "payment_terms_days", "subtotal",
                                            "tax", "total", "stock_summary", "warnings")}, indent=2))
    check("placed and reserved, with no approval step",
          order["status"] == "placed" and order["fulfilment_status"] == "reserved", order)
    check("the order carries the flow's fields",
          order["payment_type"] == "credit" and order["payment_terms_days"] == 15
          and order["fulfilment_method"] == "delivery" and order["warehouse_id"] == main_wh["id"], order)
    check("the line snapshots its tax rate and amount",
          order["items"][0]["tax_rate"] == 18 and order["items"][0]["tax_amount"] == 360, order["items"][0])
    check("grand total = 2000 + 360 tax", order["total"] == 2360, order)
    check("ordered vs reserved reported per line",
          order["items"][0]["ordered_quantity"] == 20
          and order["items"][0]["reserved_quantity"] == 20
          and order["items"][0]["delivered_quantity"] == 0, order["items"][0])
    check("stock_summary is on hand / reserved / available",
          order["stock_summary"][0]["on_hand"] == 120
          and order["stock_summary"][0]["reserved"] == 20
          and order["stock_summary"][0]["available"] == 100, order["stock_summary"])
    check("physical stock did NOT move",
          client.get(f"/inventory/{prod['id']}", headers=ah).json()["total_stock"] == 120)
    check("no stock movement was written for the order",
          not any(m["movement_type"] == "sale_out"
                  for m in client.get(f"/inventory/{prod['id']}", headers=ah).json()["movements"]))
    check("the customer was NOT billed — the receivable starts at the invoice",
          client.get(f"/customers/{cust['id']}", headers=ah)
          .json()["financial_summary"]["outstanding_balance"] == 0)
    check("credit limit exceeded reports a warning, not a refusal",
          order["warnings"] and "credit limit" in order["warnings"][0], order["warnings"])

    client.patch("/sales-workflow-settings", headers=ah, json={"credit_limit_action": "block"})
    check("with credit_limit_action=block the same order is refused",
          client.post("/orders", headers=ah, json={
              "customer_id": cust["id"],
              "items": [{"product_id": prod["id"], "quantity": 20, "unit_price": 100}]}).status_code == 400)
    client.patch("/sales-workflow-settings", headers=ah, json={"credit_limit_action": "warn"})

    print("\n-- cancel releases the hold --")
    r = client.patch(f"/orders/{order['id']}/cancel", headers=ah, json={"reason": "changed mind"})
    check("cancel -> cancelled, hold released",
          r.json()["status"] == "cancelled" and r.json()["fulfilment_status"] == "not_started"
          and r.json()["items"][0]["reserved_quantity"] == 0, r.text[:300])
    check("physical stock still never moved",
          client.get(f"/inventory/{prod['id']}", headers=ah).json()["total_stock"] == 120)
    check("no fake stock-in movement was invented",
          not any(m["movement_type"] == "sales_return"
                  for m in client.get(f"/inventory/{prod['id']}", headers=ah).json()["movements"]))
    check("the stock is available again",
          client.get("/warehouses/stock", headers=ah, params={"product_id": prod["id"]})
          .json()[0]["available"] == 120)

    print("\n-- reserved stock cannot be adjusted away --")
    held = client.post("/orders", headers=ah, json={
        "customer_id": cust["id"], "items": [{"product_id": prod["id"], "quantity": 100}]}).json()
    check("an order holding 100 leaves 20 available",
          client.get("/warehouses/stock", headers=ah, params={"product_id": prod["id"]})
          .json()[0]["available"] == 20)
    check("adjusting below what is reserved -> 400",
          client.post(f"/warehouses/{main_wh['id']}/stock/adjust", headers=ah, json={
              "product_id": prod["id"], "quantity": -110}).status_code == 400)
    check("a second order beyond availability -> 400",
          client.post("/orders", headers=ah, json={
              "customer_id": cust["id"],
              "items": [{"product_id": prod["id"], "quantity": 30}]}).status_code == 400)
    client.patch(f"/orders/{held['id']}/cancel", headers=ah, json={"reason": "done"})

    print("\n== 6. Approval mode, for a firm that wants it ==")
    client.patch("/sales-workflow-settings", headers=ah, json={"order_requires_approval": True})
    r = client.post("/orders", headers=ah, json={
        "customer_id": cust["id"], "items": [{"product_id": prod["id"], "quantity": 5}]})
    appr = r.json()
    check("with approval on, the order awaits approval",
          appr["status"] == "awaiting_approval", appr["status"])
    check("its stock is still held while it waits", appr["fulfilment_status"] == "reserved", appr)
    r = client.patch(f"/orders/{appr['id']}/approve", headers=ah)
    check("approve -> placed, and still no stock movement",
          r.json()["status"] == "placed"
          and client.get(f"/inventory/{prod['id']}", headers=ah).json()["total_stock"] == 120, r.text[:200])
    r2 = client.post("/orders", headers=ah, json={
        "customer_id": cust["id"], "items": [{"product_id": prod["id"], "quantity": 5}]}).json()
    r = client.patch(f"/orders/{r2['id']}/reject", headers=ah, json={"reason": "no"})
    check("reject -> cancelled and the hold released",
          r.json()["status"] == "cancelled" and r.json()["fulfilment_status"] == "not_started", r.text[:200])
    client.patch("/sales-workflow-settings", headers=ah, json={"order_requires_approval": False})
    client.patch(f"/orders/{appr['id']}/cancel", headers=ah, json={"reason": "tidy up"})

    print("\n== 7. Status filters, old and new ==")
    o1 = client.post("/orders", headers=ah, json={
        "customer_id": cust["id"], "items": [{"product_id": prod["id"], "quantity": 1}]}).json()
    check("filter by the new status",
          any(o["id"] == o1["id"] for o in client.get("/orders", headers=ah,
                                                      params={"status": "placed"}).json()))
    check("filter by fulfilment_status",
          any(o["id"] == o1["id"] for o in client.get("/orders", headers=ah,
                                                      params={"fulfilment_status": "reserved"}).json()))
    check("an old status value still resolves through the migration map",
          client.get("/orders", headers=ah, params={"status": "confirmed"}).status_code == 200)
    check("every order reports both axes",
          all({"status", "fulfilment_status"} <= set(o)
              for o in client.get("/orders", headers=ah).json()))

    print("\n== 8. Invoice bills the agreed tax, not 18% ==")
    client.patch("/sales-workflow-settings", headers=ah, json={"invoice_timing": "on_order"})
    taxed = client.post("/orders", headers=ah, json={
        "customer_id": cust["id"],
        "items": [{"product_id": prod["id"], "quantity": 2, "unit_price": 100, "tax_rate": 5}]}).json()
    r = client.post(f"/orders/{taxed['id']}/invoice", headers=ah)
    check("POST /orders/{id}/invoice -> 201", r.status_code == 201, r.text[:300])
    line = r.json()["items"][0]
    check("the invoice line bills 5%, not a hardcoded 18%",
          line["tax"] == 10, line)
    check("invoicing bills the customer",
          client.get(f"/customers/{cust['id']}", headers=ah)
          .json()["financial_summary"]["outstanding_balance"] > 0)
    client.patch("/sales-workflow-settings", headers=ah, json={"invoice_timing": "after_delivery"})



_phase0_checks()

# -- Phase 1A: quotation lifecycle, PATCH, convert-to-order, PDF ----------------
# Wrapped in a function so this block's locals cannot shadow anything above it.


def _phase1a_checks():
    import json

    def hdr(t):
        return {"Authorization": f"Bearer {t}"}



    def firm(name):
        reg = client.post("/auth/register", json={
            "organization_name": name, "admin_name": "Admin",
            "email": f"{name.lower()}_{uuid.uuid4().hex[:8]}@f.com", "password": "Secret@123"}).json()
        return reg, hdr(reg["tokens"]["access_token"])


    abc, ah = firm("QuoteCo")
    xyz, oh = firm("OtherQ")
    prod = client.post("/products", headers=ah, json={
        "name": "Water 20L", "price": 100, "total_inventory": 200, "tax_rate": 12}).json()
    prod2 = client.post("/products", headers=ah, json={
        "name": "Bottle 1L", "price": 25, "total_inventory": 50}).json()
    cust = client.post("/customers", headers=ah, json={
        "basic_information": {"customer_name": "Fitness First Gym"},
        "address_information": {"billing_address": "12 MG Road"}}).json()

    print("== 1. Create with quoted discounts and taxes ==")
    r = client.post("/quotations", headers=ah, json={
        "customer_id": cust["id"], "valid_until": "2026-09-30",
        "payment_terms": "Net 15", "delivery_terms": "Ex-works",
        "terms_conditions": "Prices valid 30 days.",
        "items": [
            {"product_id": prod["id"], "quantity": 20, "unit_price": 100,
             "discount": 200, "tax_rate": 18},
            {"product_id": prod2["id"], "quantity": 4, "unit_price": 25},
        ]})
    check("POST /quotations -> 201", r.status_code == 201, r.text[:400])
    q = r.json()
    print(json.dumps({k: q[k] for k in ("quotation_number", "status", "subtotal", "tax_total",
                                        "total", "item_count")}, indent=2))
    check("starts as draft", q["status"] == "draft", q["status"])
    check("line keeps its quoted discount and tax",
          q["items"][0]["discount"] == 200 and q["items"][0]["tax_rate"] == 18
          and q["items"][0]["line_total"] == 1800 and q["items"][0]["tax_amount"] == 324,
          q["items"][0])
    check("a line with no rate falls back to the product's",
          q["items"][1]["tax_rate"] is None or q["items"][1]["tax_rate"] == 0, q["items"][1])
    check("subtotal is net of discounts", q["subtotal"] == 1900, q["subtotal"])
    check("total = subtotal + quoted tax", q["total"] == round(1900 + q["tax_total"], 2), q)
    check("billing address auto-filled from the customer",
          q["billing_address"] == "12 MG Road", q["billing_address"])
    check("a bad status -> 422",
          client.post("/quotations", headers=ah, json={
              "customer_id": cust["id"], "status": "nonsense",
              "items": [{"product_id": prod["id"], "quantity": 1, "unit_price": 10}]}).status_code == 422)
    check("no lines -> 422",
          client.post("/quotations", headers=ah, json={
              "customer_id": cust["id"], "items": []}).status_code == 422)

    print("\n== 2. PATCH /quotations/{id} ==")
    r = client.patch(f"/quotations/{q['id']}", headers=ah, json={"status": "sent"})
    check("status moves draft -> sent", r.json()["status"] == "sent", r.text[:200])
    r = client.patch(f"/quotations/{q['id']}", headers=ah, json={
        "payment_terms": "Net 30", "valid_until": "2026-10-31"})
    check("terms update without touching the lines",
          r.json()["payment_terms"] == "Net 30" and len(r.json()["items"]) == 2, r.text[:250])
    r = client.patch(f"/quotations/{q['id']}", headers=ah, json={
        "items": [{"product_id": prod["id"], "quantity": 10, "unit_price": 90,
                   "discount": 0, "tax_rate": 5}]})
    check("sending items replaces the whole line set",
          len(r.json()["items"]) == 1 and r.json()["subtotal"] == 900
          and r.json()["tax_total"] == 45, r.text[:300])
    check("a bad status on PATCH -> 422",
          client.patch(f"/quotations/{q['id']}", headers=ah, json={"status": "weird"}).status_code == 422)
    check("setting converted by hand -> 400",
          client.patch(f"/quotations/{q['id']}", headers=ah,
                       json={"status": "converted"}).status_code == 400)
    check("an empty item list -> 400",
          client.patch(f"/quotations/{q['id']}", headers=ah, json={"items": []}).status_code == 400)
    check("another firm's quotation -> 404",
          client.patch(f"/quotations/{q['id']}", headers=oh, json={"status": "sent"}).status_code == 404)
    client.patch(f"/quotations/{q['id']}", headers=ah, json={"status": "accepted"})

    print("\n== 3. GET /quotations/{id}/pdf ==")
    r = client.get(f"/quotations/{q['id']}/pdf", headers=ah)
    check("PDF renders", r.status_code == 200 and r.content[:4] == b"%PDF"
          and r.headers["content-type"] == "application/pdf", r.status_code)
    check("cross-firm PDF -> 404",
          client.get(f"/quotations/{q['id']}/pdf", headers=oh).status_code == 404)

    print("\n== 4. Convert to order ==")
    before = client.get("/warehouses/stock", headers=ah,
                        params={"product_id": prod["id"]}).json()[0]
    r = client.post(f"/quotations/{q['id']}/convert-to-order", headers=ah, json={
        "delivery_date": "2026-08-15", "fulfilment_method": "delivery",
        "payment_type": "credit", "payment_terms_days": 15})
    check("POST /convert-to-order -> 201", r.status_code == 201, r.text[:400])
    conv = r.json()
    print(json.dumps(conv, indent=2))
    check("response shape matches the spec",
          set(conv) == {"quotation_id", "quotation_number", "quotation_status", "order"}, sorted(conv))
    check("the quotation is now converted",
          conv["quotation_status"] == "converted" and conv["quotation_id"] == q["id"], conv)
    check("the order is placed and reserved",
          conv["order"]["status"] == "placed"
          and conv["order"]["fulfilment_status"] == "reserved", conv["order"])
    order = client.get(f"/orders/{conv['order']['id']}", headers=ah).json()
    check("no lines were resent, yet the order carries them",
          len(order["items"]) == 1 and order["items"][0]["quantity"] == 10, order["items"])
    check("the quoted rate, discount and tax came across",
          order["items"][0]["unit_price"] == 90 and order["items"][0]["tax_rate"] == 5
          and order["items"][0]["tax_amount"] == 45, order["items"][0])
    check("the quoted total came across", order["total"] == 945, order["total"])
    check("the fulfilment terms from the conversion body are on the order",
          order["payment_type"] == "credit" and order["payment_terms_days"] == 15
          and order["fulfilment_method"] == "delivery", order)
    check("the order points back at the quotation", order["quotation_id"] == q["id"], order)
    check("and the quotation points at the order",
          client.get(f"/quotations/{q['id']}", headers=ah).json()["converted_order_id"]
          == order["id"])
    after = client.get("/warehouses/stock", headers=ah, params={"product_id": prod["id"]}).json()[0]
    check("converting reserved stock without deducting it",
          after["on_hand"] == before["on_hand"] and after["reserved"] == before["reserved"] + 10,
          {"before": before, "after": after})
    check("no receivable from the conversion",
          client.get(f"/customers/{cust['id']}", headers=ah)
          .json()["financial_summary"]["outstanding_balance"] == 0)

    print("\n-- a converted quotation is frozen --")
    check("converting twice -> 400",
          client.post(f"/quotations/{q['id']}/convert-to-order", headers=ah, json={}).status_code == 400)
    check("editing it -> 400",
          client.patch(f"/quotations/{q['id']}", headers=ah, json={"payment_terms": "x"}).status_code == 400)
    check("deleting it -> 400",
          client.delete(f"/quotations/{q['id']}", headers=ah).status_code == 400)

    print("\n-- conversion guards --")
    rej = client.post("/quotations", headers=ah, json={
        "customer_id": cust["id"], "status": "rejected",
        "items": [{"product_id": prod["id"], "quantity": 1, "unit_price": 10}]}).json()
    check("a rejected quotation cannot be converted",
          client.post(f"/quotations/{rej['id']}/convert-to-order", headers=ah, json={}).status_code == 400)
    big = client.post("/quotations", headers=ah, json={
        "customer_id": cust["id"],
        "items": [{"product_id": prod2["id"], "quantity": 9999, "unit_price": 25}]}).json()
    r = client.post(f"/quotations/{big['id']}/convert-to-order", headers=ah, json={})
    check("a shortage refuses the conversion",
          r.status_code == 400 and r.json()["detail"]["error"] == "INSUFFICIENT_STOCK", r.text[:250])
    check("and leaves the quotation unconverted",
          client.get(f"/quotations/{big['id']}", headers=ah).json()["status"] != "converted")
    check("another firm cannot convert it -> 404",
          client.post(f"/quotations/{big['id']}/convert-to-order", headers=oh, json={}).status_code == 404)

    print("\n-- the whole chain is linked --")
    check("quotation -> order ids join up",
          order["quotation_id"] == q["id"]
          and client.get(f"/quotations/{q['id']}", headers=ah).json()["converted_order_id"] == order["id"])
    check("human codes are on both",
          q["quotation_number"].startswith("QT-") and order["order_number"].startswith("SO-"),
          (q["quotation_number"], order["order_number"]))


_phase1a_checks()

# -- Phase 1C-1F: delivery planning, loading, challan, dispatch, POD ------------
# Wrapped in a function so this block's locals cannot shadow anything above it.


def _phase1cf_checks():
    import json

    def hdr(t):
        return {"Authorization": f"Bearer {t}"}



    def firm(name):
        reg = client.post("/auth/register", json={
            "organization_name": name, "admin_name": "Admin",
            "email": f"{name.lower()}_{uuid.uuid4().hex[:8]}@f.com", "password": "Secret@123"}).json()
        return reg, hdr(reg["tokens"]["access_token"])


    abc, ah = firm("DelivCo")
    xyz, oh = firm("OtherD")
    roles = {r["name"]: r for r in client.get("/roles", headers=ah).json()}
    dp_email = f"dp_{uuid.uuid4().hex[:6]}@abc.com"
    dp = client.post("/users", headers=ah, json={
        "basic_information": {"first_name": "Ramesh", "last_name": "Kumar"},
        "contact_information": {"official_email": dp_email},
        "login_security": {"password": "Dp@123456", "confirm_password": "Dp@123456"},
        "employment_information": {"role_id": roles["Delivery Partner"]["id"]}}).json()
    dp_hdr = hdr(client.post("/auth/login", json={
        "email": dp_email, "password": "Dp@123456"}).json()["tokens"]["access_token"])

    prod = client.post("/products", headers=ah, json={
        "name": "Water 20L", "price": 100, "total_inventory": 100, "tax_rate": 5}).json()
    cust = client.post("/customers", headers=ah, json={
        "basic_information": {"customer_name": "Metro Stores"},
        "address_information": {"shipping_address": "Opp. Cyber Towers"}}).json()

    print("== 1. Vehicle master ==")
    r = client.post("/vehicles", headers=ah, json={
        "vehicle_number": "DL 8S AB 2481", "vehicle_type": "Tempo", "capacity_kg": 1500})
    check("POST /vehicles -> 201", r.status_code == 201, r.text[:300])
    veh = r.json()
    check("duplicate number -> 409",
          client.post("/vehicles", headers=ah, json={"vehicle_number": "DL 8S AB 2481"}).status_code == 409)
    check("GET /vehicles lists it",
          any(v["id"] == veh["id"] for v in client.get("/vehicles", headers=ah).json()))
    check("cross-firm vehicle -> 404",
          client.get(f"/vehicles/{veh['id']}", headers=oh).status_code == 404)

    print("\n== 2. Plan a delivery (Phase 1C) ==")
    order = client.post("/orders", headers=ah, json={
        "customer_id": cust["id"],
        "items": [{"product_id": prod["id"], "quantity": 20, "unit_price": 100}]}).json()
    check("order starts reserved", order["fulfilment_status"] == "reserved", order["fulfilment_status"])
    r = client.post("/deliveries", headers=ah, json={
        "order_id": order["id"], "delivery_partner_id": dp["id"], "vehicle_id": veh["id"],
        "scheduled_date": "2026-08-15",
        "items": [{"order_item_id": order["items"][0]["id"], "planned_quantity": 20}]})
    check("POST /deliveries -> 201", r.status_code == 201, r.text[:400])
    dlv = r.json()
    print(json.dumps({k: dlv[k] for k in ("delivery_number", "status", "order_number",
                                          "planned_total", "loaded_total", "delivered_total",
                                          "amount_due", "items")}, indent=2))
    check("it has its own id and human code",
          dlv["id"] and dlv["delivery_number"].startswith("DLV-"), dlv)
    check("status is planned, NOT out for delivery", dlv["status"] == "planned", dlv["status"])
    check("the line tracks planned / loaded / delivered / pending",
          dlv["items"][0]["planned_quantity"] == 20 and dlv["items"][0]["loaded_quantity"] == 0
          and dlv["items"][0]["delivered_quantity"] == 0
          and dlv["items"][0]["pending_quantity"] == 20, dlv["items"][0])
    check("it links to the order item", dlv["items"][0]["order_item_id"] == order["items"][0]["id"])
    check("partner and vehicle resolved",
          dlv["delivery_partner"]["name"] == "Ramesh Kumar"
          and dlv["vehicle"]["vehicle_number"] == "DL 8S AB 2481", dlv)
    check("delivery address defaulted from the customer",
          dlv["delivery_address"] == "Opp. Cyber Towers", dlv["delivery_address"])
    check("amount_due is the order's unpaid total", dlv["amount_due"] == order["total"], dlv["amount_due"])
    after = client.get(f"/orders/{order['id']}", headers=ah).json()
    check("planning moves the order to processing / planned",
          after["status"] == "processing" and after["fulfilment_status"] == "planned", after)
    check("planning did NOT touch physical stock",
          client.get("/warehouses/stock", headers=ah, params={"product_id": prod["id"]})
          .json()[0]["on_hand"] == 100)
    check("the hold is still held",
          client.get("/warehouses/stock", headers=ah, params={"product_id": prod["id"]})
          .json()[0]["reserved"] == 20)
    check("planning the same quantity twice -> 400",
          client.post("/deliveries", headers=ah, json={
              "order_id": order["id"],
              "items": [{"order_item_id": order["items"][0]["id"], "planned_quantity": 5}]
          }).status_code == 400)
    check("a foreign order -> 400",
          client.post("/deliveries", headers=oh, json={"order_id": order["id"]}).status_code == 400)

    print("\n== 3. Delivery ID is the identifier ==")
    r = client.get(f"/deliveries/by-id/{dlv['id']}", headers=ah)
    check("GET /deliveries/by-id/{delivery_id} -> 200",
          r.status_code == 200 and r.json()["id"] == dlv["id"], r.text[:200])
    check("GET /deliveries lists it",
          any(d["id"] == dlv["id"] for d in client.get("/deliveries", headers=ah).json()))
    check("filter by status=planned",
          any(d["id"] == dlv["id"] for d in client.get("/deliveries", headers=ah,
                                                       params={"status": "planned"}).json()))
    check("filter by order_id",
          [d["id"] for d in client.get("/deliveries", headers=ah,
                                       params={"order_id": order["id"]}).json()] == [dlv["id"]])
    check("cross-firm delivery -> 404",
          client.get(f"/deliveries/by-id/{dlv['id']}", headers=oh).status_code == 404)
    check("the partner sees their own delivery",
          any(d["id"] == dlv["id"] for d in client.get("/deliveries", headers=dp_hdr).json()))

    print("\n== 4. Dispatch is refused before loading (Phase 1F) ==")
    check("dispatching an unloaded delivery -> 400",
          client.patch(f"/deliveries/by-id/{dlv['id']}", headers=ah,
                       json={"status": "in_transit"}).status_code == 400)
    check("confirming before dispatch -> 400",
          client.post(f"/deliveries/{dlv['id']}/confirm", headers=ah, json={
              "items": [{"delivery_item_id": dlv["items"][0]["id"], "delivered_quantity": 5}]
          }).status_code == 400)

    print("\n== 5. Vehicle loading (Phase 1D) ==")
    client.post(f"/deliveries/{dlv['id']}/pick", headers=ah, json={
        "items": [{"delivery_item_id": dlv["items"][0]["id"], "picked_quantity": 20}]
    })
    client.post(f"/deliveries/{dlv['id']}/ready", headers=ah)
    r = client.post("/vehicle-stock/loading", headers=ah, json={
        "delivery_id": dlv["id"],
        "items": [{"delivery_item_id": dlv["items"][0]["id"], "loaded_quantity": 12}]})
    check("POST /vehicle-stock/loading against the delivery -> 201", r.status_code == 201, r.text[:400])
    load = r.json()
    print(json.dumps(load, indent=2))
    check("response names the loading, delivery and new status",
          load["delivery_id"] == dlv["id"] and load["status"] == "loaded" and load["loading_id"], load)
    check("it reports the warehouse and vehicle figures after",
          load["items"][0]["loaded_quantity"] == 12
          and load["items"][0]["warehouse_on_hand_after"] == 88
          and load["items"][0]["vehicle_stock_after"] == 12, load["items"][0])
    stock = client.get("/warehouses/stock", headers=ah, params={"product_id": prod["id"]}).json()[0]
    check("warehouse on hand fell by what was loaded", stock["on_hand"] == 88, stock)
    check("the hold shrank as it was consumed", stock["reserved"] == 8, stock)
    check("available is unchanged — the goods were already promised",
          stock["available"] == 80, stock)
    check("a delivery_out movement was recorded",
          any(m["movement_type"] == "delivery_out"
              for m in client.get(f"/inventory/{prod['id']}", headers=ah).json()["movements"]))
    check("the order is now loaded",
          client.get(f"/orders/{order['id']}", headers=ah).json()["fulfilment_status"] == "loaded")

    print("\n-- idempotent: loading twice must not deduct twice --")
    r = client.post("/vehicle-stock/loading", headers=ah, json={
        "delivery_id": dlv["id"],
        "items": [{"delivery_item_id": dlv["items"][0]["id"], "loaded_quantity": 12}]})
    check("loading more than what is planned -> 400", r.status_code == 400, r.text[:250])
    check("warehouse untouched by the refused call",
          client.get("/warehouses/stock", headers=ah, params={"product_id": prod["id"]})
          .json()[0]["on_hand"] == 88)
    r = client.post(f"/deliveries/{dlv['id']}/load", headers=ah)
    check("loading the remainder -> 200", r.status_code == 200, r.text[:250])
    check("the whole planned quantity is now on the vehicle",
          r.json()["loaded_total"] == 20 and r.json()["items"][0]["loaded_quantity"] == 20,
          r.json()["items"][0])
    check("warehouse fell by the rest only",
          client.get("/warehouses/stock", headers=ah, params={"product_id": prod["id"]})
          .json()[0]["on_hand"] == 80)
    r = client.post(f"/deliveries/{dlv['id']}/load", headers=ah)
    check("loading again when nothing is left -> 400", r.status_code == 400, r.text[:200])
    check("and the warehouse is still 80 — never deducted twice",
          client.get("/warehouses/stock", headers=ah, params={"product_id": prod["id"]})
          .json()[0]["on_hand"] == 80)
    check("the hold is fully consumed now",
          client.get("/warehouses/stock", headers=ah, params={"product_id": prod["id"]})
          .json()[0]["reserved"] == 0)

    print("\n== 6. Challan PDF (Phase 1E) ==")
    r = client.get(f"/deliveries/{dlv['id']}/challan/pdf", headers=ah)
    check("challan renders", r.status_code == 200 and r.content[:4] == b"%PDF"
          and r.headers["content-type"] == "application/pdf", r.status_code)
    check("the partner can pull their own challan",
          client.get(f"/deliveries/{dlv['id']}/challan/pdf", headers=dp_hdr).status_code == 200)
    check("cross-firm challan -> 404",
          client.get(f"/deliveries/{dlv['id']}/challan/pdf", headers=oh).status_code == 404)

    print("\n== 7. Dispatch (Phase 1F) ==")
    check("cancelling a loaded delivery -> 400",
          client.patch(f"/deliveries/by-id/{dlv['id']}", headers=ah,
                       json={"status": "cancelled"}).status_code == 400)
    r = client.patch(f"/deliveries/by-id/{dlv['id']}", headers=ah, json={"status": "in_transit"})
    check("dispatch -> in_transit with who and when",
          r.status_code == 200 and r.json()["status"] == "in_transit"
          and r.json()["dispatched_at"] and r.json()["dispatched_by_id"], r.text[:300])
    check("the order is in transit too",
          client.get(f"/orders/{order['id']}", headers=ah).json()["fulfilment_status"] == "in_transit")
    check("only now does it show in the partner's assigned list",
          any(o["id"] == order["id"] for o in client.get("/deliveries/assigned", headers=dp_hdr).json()))

    print("\n== 8. Partial delivery + POD (Phase 1H) ==")
    photo = client.post("/files/upload", headers=dp_hdr, files={
        "file": ("pod.png", b"\x89PNG\r\n\x1a\n" + b"0" * 20, "image/png")}).json()
    sign = client.post("/files/upload", headers=dp_hdr, files={
        "file": ("sign.png", b"\x89PNG\r\n\x1a\n" + b"1" * 20, "image/png")}).json()
    r = client.post(f"/deliveries/{dlv['id']}/confirm", headers=dp_hdr, json={
        "items": [{"delivery_item_id": dlv["items"][0]["id"], "delivered_quantity": 15}],
        "pod_photo_file_ids": [photo["file_id"]],
        "signature_file_id": sign["file_id"],
        "notes": "Customer accepted partial delivery"})
    check("POST /deliveries/{id}/confirm -> 200", r.status_code == 200, r.text[:400])
    conf = r.json()
    print(json.dumps({k: conf[k] for k in ("status", "planned_total", "loaded_total",
                                           "delivered_total", "pod", "items")}, indent=2))
    check("partial delivery is recorded as such", conf["status"] == "partially_delivered", conf["status"])
    check("ordered 20, loaded 20, delivered 15, pending 5",
          conf["items"][0]["planned_quantity"] == 20 and conf["items"][0]["loaded_quantity"] == 20
          and conf["items"][0]["delivered_quantity"] == 15
          and conf["items"][0]["pending_quantity"] == 5, conf["items"][0])
    check("POD is stored", conf["pod"]["photo_file_ids"] == [photo["file_id"]]
          and conf["pod"]["signature_file_id"] == sign["file_id"], conf["pod"])
    check("the order is partially delivered",
          client.get(f"/orders/{order['id']}", headers=ah).json()["fulfilment_status"]
          == "partially_delivered")
    check("the undelivered 5 stayed on the vehicle, NOT back in the warehouse",
          client.get("/warehouses/stock", headers=ah, params={"product_id": prod["id"]})
          .json()[0]["on_hand"] == 80)
    veh_stock = client.get(f"/vehicle-stock/current/{dp['id']}", headers=ah).json()
    check("vehicle shows 20 loaded and 15 delivered",
          veh_stock["items"][0]["loaded_qty"] == 20 and veh_stock["items"][0]["delivered_qty"] == 15,
          veh_stock["items"][0])
    check("delivering more than is on the vehicle -> 400",
          client.post(f"/deliveries/{dlv['id']}/confirm", headers=dp_hdr, json={
              "items": [{"delivery_item_id": dlv["items"][0]["id"], "delivered_quantity": 50}]
          }).status_code == 400)
    r = client.post(f"/deliveries/{dlv['id']}/confirm", headers=dp_hdr, json={
        "items": [{"delivery_item_id": dlv["items"][0]["id"], "delivered_quantity": 5}]})
    check("delivering the rest completes it", r.json()["status"] == "delivered"
          and r.json()["items"][0]["pending_quantity"] == 0, r.json()["items"][0])
    check("and completes the order",
          client.get(f"/orders/{order['id']}", headers=ah).json()["status"] == "completed")
    check("confirming a delivered delivery -> 400",
          client.post(f"/deliveries/{dlv['id']}/confirm", headers=dp_hdr, json={
              "items": [{"delivery_item_id": dlv["items"][0]["id"], "delivered_quantity": 1}]
          }).status_code == 400)

    print("\n== 9. Failed delivery ==")
    o2 = client.post("/orders", headers=ah, json={
        "customer_id": cust["id"],
        "items": [{"product_id": prod["id"], "quantity": 4, "unit_price": 100}]}).json()
    d2 = client.post("/deliveries", headers=ah, json={
        "order_id": o2["id"], "delivery_partner_id": dp["id"], "vehicle_id": veh["id"]}).json()
    check("omitting items plans the whole outstanding order",
          d2["items"][0]["planned_quantity"] == 4, d2["items"])
    client.post(f"/deliveries/{d2['id']}/accept", headers=dp_hdr)
    client.post(f"/deliveries/{d2['id']}/load", headers=ah)
    client.patch(f"/deliveries/by-id/{d2['id']}", headers=ah, json={"status": "in_transit"})
    before = client.get(f"/vehicle-stock/current/{dp['id']}", headers=ah).json()["items"][0]["delivered_qty"]
    r = client.post(f"/deliveries/{d2['id']}/confirm", headers=dp_hdr, json={
        "failed": True, "failure_reason": "Customer unavailable"})
    check("a failed delivery records the reason",
          r.status_code == 200 and r.json()["status"] == "failed"
          and r.json()["failure_reason"] == "Customer unavailable", r.text[:300])
    check("failure without a reason -> 422",
          client.post(f"/deliveries/{d2['id']}/confirm", headers=dp_hdr,
                      json={"failed": True}).status_code == 422)
    check("vehicle stock untouched by the failure",
          client.get(f"/vehicle-stock/current/{dp['id']}", headers=ah)
          .json()["items"][0]["delivered_qty"] == before)
    check("the order records the failure",
          client.get(f"/orders/{o2['id']}", headers=ah).json()["fulfilment_status"] == "failed")

    print("\n== 10. Split an order across two deliveries ==")
    o3 = client.post("/orders", headers=ah, json={
        "customer_id": cust["id"],
        "items": [{"product_id": prod["id"], "quantity": 10, "unit_price": 100}]}).json()
    oi = o3["items"][0]["id"]
    a = client.post("/deliveries", headers=ah, json={
        "order_id": o3["id"], "delivery_partner_id": dp["id"],
        "items": [{"order_item_id": oi, "planned_quantity": 6}]}).json()
    b = client.post("/deliveries", headers=ah, json={
        "order_id": o3["id"], "delivery_partner_id": dp["id"],
        "items": [{"order_item_id": oi, "planned_quantity": 4}]})
    check("the remaining 4 can be planned into a second delivery", b.status_code == 201, b.text[:250])
    check("but a third planning attempt -> 400",
          client.post("/deliveries", headers=ah, json={
              "order_id": o3["id"],
              "items": [{"order_item_id": oi, "planned_quantity": 1}]}).status_code == 400)
    check("both deliveries belong to the same order",
          len(client.get("/deliveries", headers=ah, params={"order_id": o3["id"]}).json()) == 2)
    check("the older order-id route still works for existing clients",
          client.get(f"/deliveries/{o3['id']}", headers=ah).status_code == 200)



_phase1cf_checks()


def _phase1ij_checks():
    """Phase 1I + 1J: bill what was delivered, and print it in two formats."""
    import json

    def hdr(t):
        return {"Authorization": f"Bearer {t}"}

    def firm(name):
        reg = client.post("/auth/register", json={
            "organization_name": name, "admin_name": "Admin",
            "email": f"{name.lower()}_{uuid.uuid4().hex[:8]}@f.com", "password": "Secret@123"}).json()
        return reg, hdr(reg["tokens"]["access_token"])

    inv, ah = firm("BillCo")
    other, oh = firm("OtherBill")
    roles = {r["name"]: r for r in client.get("/roles", headers=ah).json()}
    dp_email = f"bdp_{uuid.uuid4().hex[:6]}@billco.com"
    dp = client.post("/users", headers=ah, json={
        "basic_information": {"first_name": "Suresh", "last_name": "Yadav"},
        "contact_information": {"official_email": dp_email},
        "login_security": {"password": "Dp@123456", "confirm_password": "Dp@123456"},
        "employment_information": {"role_id": roles["Delivery Partner"]["id"]}}).json()
    dp_hdr = hdr(client.post("/auth/login", json={
        "email": dp_email, "password": "Dp@123456"}).json()["tokens"]["access_token"])

    prod = client.post("/products", headers=ah, json={
        "name": "Can 20L", "price": 100, "total_inventory": 200,
        "tax_rate": 5, "hsn_code": "22011010"}).json()
    cust = client.post("/customers", headers=ah, json={
        "basic_information": {"customer_name": "Sharma Traders", "phone": "9812345678"},
        "address_information": {"billing_address": "12 MG Road", "shipping_address": "Warehouse 4"},
        "business_tax_information": {"gst_number": "07AABCU9603R1ZM"}}).json()

    def deliver(order, quantity):
        """Plan, load, dispatch and confirm `quantity` against a fresh delivery."""
        dlv = client.post("/deliveries", headers=ah, json={
            "order_id": order["id"], "delivery_partner_id": dp["id"],
            "items": [{"order_item_id": order["items"][0]["id"], "planned_quantity": quantity}]}).json()
        client.post(f"/deliveries/{dlv['id']}/accept", headers=dp_hdr)
        client.post(f"/deliveries/{dlv['id']}/load", headers=ah)
        client.patch(f"/deliveries/by-id/{dlv['id']}", headers=ah, json={"status": "in_transit"})
        client.post(f"/deliveries/{dlv['id']}/confirm", headers=dp_hdr, json={
            "items": [{"delivery_item_id": dlv["items"][0]["id"], "delivered_quantity": quantity}]})
        return client.get(f"/deliveries/by-id/{dlv['id']}", headers=ah).json()

    print("\n== 1. Invoice bills the delivered quantity, not the ordered one (Phase 1I) ==")
    order = client.post("/orders", headers=ah, json={
        "customer_id": cust["id"], "payment_terms_days": 15,
        "items": [{"product_id": prod["id"], "quantity": 20, "unit_price": 100}]}).json()
    first = deliver(order, 12)
    check("only 12 of 20 went out", first["delivered_total"] == 12, first["delivered_total"])
    r = client.post(f"/orders/{order['id']}/invoice", headers=ah,
                    json={"delivery_id": first["id"]})
    check("POST /orders/{id}/invoice with a delivery_id -> 201", r.status_code == 201, r.text[:400])
    bill = r.json()
    print(json.dumps({k: bill[k] for k in ("invoice_number", "delivery_id", "due_date",
                                           "subtotal", "tax", "total", "items")}, indent=2))
    check("it bills 12, not 20", bill["items"][0]["quantity"] == 12, bill["items"][0])
    check("at the order's agreed rate", bill["items"][0]["unit_price"] == 100)
    check("subtotal is 12 x 100", bill["subtotal"] == 1200, bill["subtotal"])
    check("tax is the line's own 5%", bill["tax"] == 60, bill["tax"])
    check("total is 1260", bill["total"] == 1260, bill["total"])
    check("the invoice names the delivery it bills", bill["delivery_id"] == first["id"])
    check("the line points back at the delivery line",
          bill["items"][0]["delivery_item_id"] == first["items"][0]["id"], bill["items"][0])
    check("and at the order line",
          bill["items"][0]["order_item_id"] == order["items"][0]["id"], bill["items"][0])
    check("the tax rate is on the line", bill["items"][0]["tax_rate"] == 5, bill["items"][0])
    check("HSN came across", bill["items"][0]["hsn_code"] == "22011010", bill["items"][0])

    print("\n== 2. Due date comes from the order's payment terms ==")
    from datetime import datetime as _dt
    issued = _dt.fromisoformat(bill["invoice_date"].replace("Z", "+00:00"))
    due = _dt.fromisoformat(bill["due_date"].replace("Z", "+00:00"))
    check("due_date is issue date + 15 days", (due - issued).days == 15, bill["due_date"])
    no_terms = client.post("/orders", headers=ah, json={
        "customer_id": cust["id"],
        "items": [{"product_id": prod["id"], "quantity": 2, "unit_price": 100}]}).json()
    no_del = deliver(no_terms, 2)
    nb = client.post(f"/orders/{no_terms['id']}/invoice", headers=ah, json={"delivery_id": no_del["id"]}).json()
    check("no agreed terms -> no due date", nb["due_date"] is None, nb)

    print("\n== 3. The same delivery cannot be billed twice ==")
    again = client.post(f"/orders/{order['id']}/invoice", headers=ah,
                        json={"delivery_id": first["id"]})
    check("re-invoicing a billed delivery -> 400", again.status_code == 400, again.text[:250])
    check("and says so plainly", "already been invoiced" in again.text, again.text[:250])

    print("\n== 4. The rest of the order bills separately ==")
    second = deliver(order, 8)
    r = client.post(f"/orders/{order['id']}/invoice", headers=ah,
                    json={"delivery_id": second["id"]})
    check("the second delivery gets its own invoice -> 201", r.status_code == 201, r.text[:300])
    rest = r.json()
    check("billed for the remaining 8", rest["items"][0]["quantity"] == 8, rest["items"][0])
    check("8 x 100 + 5% = 840", rest["total"] == 840, rest["total"])
    check("two invoices against one order",
          len([i for i in client.get("/invoices", headers=ah).json()
               if i["order_id"] == order["id"]]) == 2)
    billed = client.get(f"/customers/{cust['id']}", headers=ah).json()["financial_summary"]
    check("the customer was billed for both, not for the order",
          billed["total_billed"] == 1260 + 840 + nb["total"], billed)

    print("\n== 5. after_full_order waits for the whole order ==")
    client.patch("/sales-workflow-settings", headers=ah, json={"partial_delivery_invoice_mode": "after_full_order"})
    slow = client.post("/orders", headers=ah, json={
        "customer_id": cust["id"],
        "items": [{"product_id": prod["id"], "quantity": 10, "unit_price": 100}]}).json()
    part = deliver(slow, 4)
    r = client.post(f"/orders/{slow['id']}/invoice", headers=ah, json={"delivery_id": part["id"]})
    check("billing a part delivery under after_full_order -> 400", r.status_code == 400, r.text[:300])
    check("the message names the setting", "after_full_order" in r.text, r.text[:300])
    whole = deliver(slow, 6)
    r = client.post(f"/orders/{slow['id']}/invoice", headers=ah, json={"delivery_id": whole["id"]})
    check("once fully delivered it bills -> 201", r.status_code == 201, r.text[:300])
    r = client.post(f"/orders/{slow['id']}/invoice", headers=ah, json={"delivery_id": part["id"]})
    check("and the earlier delivery bills too", r.status_code == 201, r.text[:300])
    check("for its own 4", r.json()["items"][0]["quantity"] == 4, r.json()["items"][0])
    client.patch("/sales-workflow-settings", headers=ah, json={"partial_delivery_invoice_mode": "per_delivery"})

    print("\n== 6. Guard rails ==")
    empty = client.post("/orders", headers=ah, json={
        "customer_id": cust["id"],
        "items": [{"product_id": prod["id"], "quantity": 3, "unit_price": 100}]}).json()
    undelivered = client.post("/deliveries", headers=ah, json={
        "order_id": empty["id"], "delivery_partner_id": dp["id"]}).json()
    r = client.post(f"/orders/{empty['id']}/invoice", headers=ah,
                    json={"delivery_id": undelivered["id"]})
    check("billing a delivery that has delivered nothing -> 400", r.status_code == 400, r.text[:250])
    check("another firm's delivery_id -> 400",
          client.post(f"/orders/{empty['id']}/invoice", headers=ah,
                      json={"delivery_id": str(uuid.uuid4())}).status_code == 400)
    check("a delivery from a different order -> 400",
          client.post(f"/orders/{empty['id']}/invoice", headers=ah,
                      json={"delivery_id": first["id"]}).status_code == 400)
    check("a foreign order -> 404",
          client.post(f"/orders/{order['id']}/invoice", headers=oh).status_code == 404)

    print("\n== 7. Billing the whole order still carries the order's totals ==")
    client.patch("/sales-workflow-settings", headers=ah, json={"invoice_timing": "on_order"})
    flat = client.post("/orders", headers=ah, json={
        "customer_id": cust["id"], "discount": 50,
        "items": [{"product_id": prod["id"], "quantity": 5, "unit_price": 100}]}).json()
    fb = client.post(f"/orders/{flat['id']}/invoice", headers=ah).json()
    check("the order-level discount is billed", fb["discount"] == 50, fb["discount"])
    check("and the total matches the order", fb["total"] == flat["total"], (fb["total"], flat["total"]))
    check("billing the whole order twice -> 400",
          client.post(f"/orders/{flat['id']}/invoice", headers=ah).status_code == 400)
    client.patch("/sales-workflow-settings", headers=ah, json={"invoice_timing": "after_delivery"})

    print("\n== 8. Two PDF formats from one invoice (Phase 1J) ==")
    r = client.get(f"/invoices/{bill['id']}/pdf", headers=ah, params={"format": "detailed"})
    check("GET /invoices/{id}/pdf?format=detailed -> 200", r.status_code == 200, r.text[:200])
    check("it is a PDF", r.content[:5] == b"%PDF-" and
          r.headers["content-type"] == "application/pdf", r.headers)
    detailed = r.content
    r = client.get(f"/invoices/{bill['id']}/pdf", headers=ah, params={"format": "simple"})
    check("GET /invoices/{id}/pdf?format=simple -> 200", r.status_code == 200, r.text[:200])
    simple = r.content
    check("simple is a PDF too", simple[:5] == b"%PDF-")
    check("the two formats really differ", simple != detailed)
    check("the short copy is the smaller document", len(simple) < len(detailed),
          (len(simple), len(detailed)))
    check("the filename names the format",
          "-simple.pdf" in r.headers["content-disposition"], r.headers["content-disposition"])
    check("no format defaults to the full tax invoice",
          client.get(f"/invoices/{bill['id']}/pdf", headers=ah).content[:5] == b"%PDF-")
    check("an unknown format -> 422",
          client.get(f"/invoices/{bill['id']}/pdf", headers=ah,
                     params={"format": "fancy"}).status_code == 422)
    check("another firm's invoice -> 404",
          client.get(f"/invoices/{bill['id']}/pdf", headers=oh).status_code == 404)

    print("\n== 9. The PDF is rendered from the firm's invoice settings ==")
    client.patch("/invoice-settings", headers=ah, json={
        "fields": {"show_hsn_sac": False, "show_tax_rate": False, "show_tax_amount": False,
                   "show_billing_address": False, "show_shipping_address": False,
                   "show_customer_gstin": False, "show_bank_details": False,
                   "show_upi_qr": False, "show_signature": False}})
    stripped = client.get(f"/invoices/{bill['id']}/pdf", headers=ah,
                          params={"format": "detailed"}).content
    check("switching nine fields off shrinks the tax invoice", len(stripped) < len(detailed),
          (len(stripped), len(detailed)))
    client.patch("/invoice-settings", headers=ah, json={
        "fields": {"show_hsn_sac": True, "show_tax_rate": True, "show_tax_amount": True,
                   "show_billing_address": True, "show_shipping_address": True,
                   "show_customer_gstin": True, "show_bank_details": True,
                   "show_upi_qr": True, "show_signature": True,
                   "show_mrp": True, "show_batch_number": True, "show_expiry_date": True}})
    check("turning every column on still renders",
          client.get(f"/invoices/{bill['id']}/pdf", headers=ah,
                     params={"format": "detailed"}).content[:5] == b"%PDF-")
    client.patch("/invoice-settings", headers=ah, json={
        "fields": {"show_mrp": False, "show_batch_number": False, "show_expiry_date": False},
        "terms": "Payment within 15 days.", "footer_text": "Thank you for your business",
        "notes": "Goods once sold are not returnable",
        "branding": {"primary_color": "#0F62FE"}})
    with_extras = client.get(f"/invoices/{bill['id']}/pdf", headers=ah,
                             params={"format": "detailed"}).content
    check("terms, notes and footer print", len(with_extras) > len(detailed),
          (len(with_extras), len(detailed)))
    check("a brand colour does not break it", with_extras[:5] == b"%PDF-")
    check("the simple copy honours the settings too",
          client.get(f"/invoices/{bill['id']}/pdf", headers=ah,
                     params={"format": "simple"}).content[:5] == b"%PDF-")

    print("\n== 10. Paper size, logo and a nonsense logo ==")
    for paper in ("A5", "thermal", "A4"):
        client.patch("/invoice-settings", headers=ah, json={"paper_size": paper})
        check(f"paper_size {paper} renders",
              client.get(f"/invoices/{bill['id']}/pdf", headers=ah).content[:5] == b"%PDF-")
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080600000"
        "01f15c4890000000a49444154789c6300010000050001"
        "0d0a2db40000000049454e44ae426082")
    up = client.post("/files/upload", headers=ah,
                     files={"file": ("logo.png", png, "image/png")}).json()
    no_logo = client.get(f"/invoices/{bill['id']}/pdf", headers=ah).content
    client.patch("/invoice-settings", headers=ah, json={"branding": {"logo_file_id": up["url"]}})
    logoed = client.get(f"/invoices/{bill['id']}/pdf", headers=ah).content
    check("a logo URL in the settings resolves and prints", logoed[:5] == b"%PDF-")
    check("and the image really is embedded", len(logoed) > len(no_logo),
          (len(no_logo), len(logoed)))
    check("a bare file id works as well as the URL",
          client.patch("/invoice-settings", headers=ah,
                       json={"branding": {"logo_file_id": up["file_id"]}}).status_code == 200
          and len(client.get(f"/invoices/{bill['id']}/pdf", headers=ah).content) > len(no_logo))
    junk = client.post("/files/upload", headers=ah,
                       files={"file": ("logo.png", b"not an image at all", "image/png")}).json()
    client.patch("/invoice-settings", headers=ah, json={"branding": {"logo_file_id": junk["file_id"]}})
    check("an unreadable logo never costs the firm its invoice",
          client.get(f"/invoices/{bill['id']}/pdf", headers=ah).content[:5] == b"%PDF-")
    client.patch("/invoice-settings", headers=ah, json={"branding": {"logo_file_id": None}})
    check("no separate logo, signature or colour endpoint exists",
          not any(p.startswith("/invoice-settings/")
                  for p in client.get("/openapi.json").json()["paths"]))

    print("\n== 10b. The template changes the print, and the signature prints ==")
    prints = {}
    for template in ("classic", "modern", "compact", "thermal"):
        r = client.patch("/invoice-settings", headers=ah, json={"template": template})
        check(f"PATCH template {template} -> 200",
              r.status_code == 200 and r.json()["template"] == template, r.text[:200])
        prints[template] = {
            shape: client.get(f"/invoices/{bill['id']}/pdf", headers=ah,
                              params={"format": shape}).content
            for shape in ("simple", "detailed")
        }
        check(f"{template} still renders both formats",
              all(doc[:5] == b"%PDF-" for doc in prints[template].values()))
    for shape in ("simple", "detailed"):
        check(f"all four templates print a different {shape} document",
              len({prints[t][shape] for t in prints}) == 4,
              {t: len(prints[t][shape]) for t in prints})
    check("thermal prints on a till roll, so it is the shortest tax invoice",
          len(prints["thermal"]["detailed"]) < len(prints["classic"]["detailed"]),
          (len(prints["thermal"]["detailed"]), len(prints["classic"]["detailed"])))
    check("an unknown template -> 422",
          client.patch("/invoice-settings", headers=ah,
                       json={"template": "sparkly"}).status_code == 422)
    client.patch("/invoice-settings", headers=ah, json={"template": "classic"})

    sign = client.post("/files/upload", headers=ah,
                       files={"file": ("sign.png", png, "image/png")}).json()
    unsigned = client.get(f"/invoices/{bill['id']}/pdf", headers=ah).content
    r = client.patch("/invoice-settings", headers=ah,
                     json={"branding": {"signature_file_id": sign["file_id"]}})
    check("PATCH branding.signature_file_id -> 200",
          r.status_code == 200
          and r.json()["branding"]["signature_file_id"] == sign["file_id"], r.text[:250])
    signed = client.get(f"/invoices/{bill['id']}/pdf", headers=ah).content
    check("the signature image is embedded", len(signed) > len(unsigned),
          (len(unsigned), len(signed)))
    client.patch("/invoice-settings", headers=ah,
                 json={"fields": {"show_signature": False}})
    check("switching the signature off drops it again",
          len(client.get(f"/invoices/{bill['id']}/pdf", headers=ah).content) < len(signed))
    client.patch("/invoice-settings", headers=ah, json={"fields": {"show_signature": True}})
    check("another firm's invoice settings are untouched by all of this",
          client.get("/invoice-settings", headers=oh).json()["template"] == "classic"
          and client.get("/invoice-settings", headers=oh).json()
          ["branding"]["signature_file_id"] is None)

    print("\n== 11. Freight and round off ==")
    charged = client.post("/invoices", headers=ah, json={
        "customer_id": cust["id"], "tax": 20, "additional_charges": 150, "round_off": -0.4,
        "items": [{"product_id": prod["id"], "quantity": 2, "unit_price": 100}]})
    check("POST /invoices takes additional_charges and round_off -> 201",
          charged.status_code == 201, charged.text[:300])
    ch = charged.json()
    check("both are kept",
          ch["additional_charges"] == 150 and ch["round_off"] == -0.4, ch)
    check("and land in the total", ch["total"] == round(200 + 20 + 150 - 0.4, 2), ch["total"])
    check("they print on the tax invoice",
          client.get(f"/invoices/{ch['id']}/pdf", headers=ah,
                     params={"format": "detailed"}).content[:5] == b"%PDF-")
    plain = client.post("/invoices", headers=ah, json={
        "customer_id": cust["id"],
        "items": [{"product_id": prod["id"], "quantity": 1, "unit_price": 100}]}).json()
    check("leaving them out charges only the goods and their own tax",
          plain["total"] == 105 and plain["items"][0]["tax_rate"] == 5, plain)

    print("\n== 12. An older invoice with nothing filled in still prints ==")
    # The shape a row written before these columns existed has: no tax rate, no HSN,
    # no due date, no customer. A blank column must never cost the firm its invoice.
    from app.core.database import SessionLocal as _BillSession
    from sqlalchemy import text as _bill_text
    _bdb = _BillSession()
    try:
        _bdb.execute(_bill_text(
            "UPDATE invoice_items SET tax_rate = NULL, hsn_code = NULL, tax_amount = NULL, "
            "uom = NULL WHERE invoice_id = :i"), {"i": plain["id"]})
        _bdb.execute(_bill_text(
            "UPDATE invoices SET customer_id = NULL, due_date = NULL, billing_address = NULL, "
            "additional_charges = NULL, round_off = NULL, notes = NULL WHERE id = :i"),
            {"i": plain["id"]})
        _bdb.commit()
    finally:
        _bdb.close()
    for shape in ("simple", "detailed"):
        r = client.get(f"/invoices/{plain['id']}/pdf", headers=ah, params={"format": shape})
        check(f"a bare legacy row prints as {shape}",
              r.status_code == 200 and r.content[:5] == b"%PDF-", r.text[:200])


_phase1ij_checks()


def _phase1kn_checks():
    """Phase 1K-1N: quick billing, walk-in buyers, receipts, and the customer ledger."""
    import json
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    def hdr(t):
        return {"Authorization": f"Bearer {t}"}

    def firm(name):
        reg = client.post("/auth/register", json={
            "organization_name": name, "admin_name": "Admin",
            "email": f"{name.lower()}_{uuid.uuid4().hex[:8]}@f.com", "password": "Secret@123"}).json()
        return reg, hdr(reg["tokens"]["access_token"])

    shop, ah = firm("CounterCo")
    other, oh = firm("OtherCounter")

    prod = client.post("/products", headers=ah, json={
        "name": "Bottle 1L", "price": 100, "total_inventory": 60,
        "tax_rate": 18, "hsn_code": "22011010"}).json()
    plainprod = client.post("/products", headers=ah, json={
        "name": "Crate", "price": 50, "total_inventory": 20}).json()
    cust = client.post("/customers", headers=ah, json={
        "basic_information": {"customer_name": "Gupta Kirana", "phone": "9800011122"},
        "address_information": {"billing_address": "5 Station Road"},
        "payment_information": {"credit_limit": 10000}}).json()

    def on_hand():
        rows = client.get("/warehouses/stock", headers=ah, params={"product_id": prod["id"]}).json()
        return rows[0]["on_hand"] if rows else None

    print("\n== 1. Quick billing: one call, goods out, money in (Phase 1K) ==")
    start = on_hand()
    r = client.post("/invoices", headers=ah, json={
        "customer_id": cust["id"], "sales_type": "POS Sale",
        "items": [{"product_id": prod["id"], "quantity": 2, "unit_price": 100,
                   "discount": 0, "tax_rate": 18}],
        "payment": {"payment_method": "upi", "amount": 236,
                    "transaction_reference": "UPI123456"}})
    check("POST /invoices with a payment -> 201", r.status_code == 201, r.text[:400])
    pos = r.json()
    print(json.dumps({k: pos[k] for k in ("invoice_number", "sales_type", "status", "subtotal",
                                          "tax", "total", "amount_paid", "outstanding_amount",
                                          "items")}, indent=2))
    check("2 x 100 = 200 billed", pos["subtotal"] == 200, pos["subtotal"])
    check("18% is 36", pos["tax"] == 36, pos["tax"])
    check("total is 236", pos["total"] == 236, pos["total"])
    check("it is paid in full", pos["status"] == "paid" and pos["amount_paid"] == 236, pos)
    check("nothing outstanding", pos["outstanding_amount"] == 0, pos["outstanding_amount"])
    check("the line keeps the rate it was taxed at", pos["items"][0]["tax_rate"] == 18)
    check("and the HSN", pos["items"][0]["hsn_code"] == "22011010")
    check("the goods left the warehouse", on_hand() == start - 2, (start, on_hand()))
    moves = client.get(f"/inventory/{prod['id']}", headers=ah).json()["movements"]
    check("and the movement is on the ledger",
          any(m["movement_type"] == "sale_out" and m["quantity"] == -2 for m in moves),
          moves[:2])

    print("\n== 2. The payment is receipted, once, in one place ==")
    receipts = client.get("/payment-receipts", headers=ah, params={"invoice_id": pos["id"]}).json()
    check("the counter payment shows up as a receipt", len(receipts) == 1, receipts)
    rc = receipts[0]
    check("with its own RCPT number", (rc["receipt_number"] or "").startswith("RCPT-"), rc)
    check("naming the invoice and the customer",
          rc["invoice_id"] == pos["id"] and rc["customer_id"] == cust["id"], rc)
    check("the method and reference came across",
          rc["payment_method"] == "upi" and rc["transaction_reference"] == "UPI123456", rc)
    check("and it reports where the invoice stands",
          rc["invoice_total"] == 236 and rc["total_paid"] == 236
          and rc["outstanding_amount"] == 0 and rc["payment_status"] == "paid", rc)
    check("the same payment is in the customer's history, not duplicated",
          [p["id"] for p in client.get(f"/customers/{cust['id']}/payments", headers=ah).json()]
          == [rc["id"]])
    money = client.get(f"/customers/{cust['id']}", headers=ah).json()["financial_summary"]
    check("the customer was billed and credited the same 236",
          money["total_billed"] == 236 and money["total_received"] == 236
          and money["outstanding_balance"] == 0, money)

    print("\n== 3. A credit sale, and the product's own tax ==")
    credit = client.post("/invoices", headers=ah, json={
        "customer_id": cust["id"],
        "items": [{"product_id": prod["id"], "quantity": 1}]}).json()
    check("no payment block leaves it unpaid", credit["status"] == "unpaid", credit["status"])
    check("the product's own rate applied with no tax_rate sent",
          credit["items"][0]["tax_rate"] == 18 and credit["total"] == 118, credit)
    check("the price came from the product", credit["items"][0]["unit_price"] == 100)
    check("a product with no tax rate is billed without tax",
          client.post("/invoices", headers=ah, json={
              "customer_id": cust["id"],
              "items": [{"product_id": plainprod["id"], "quantity": 1}]}).json()["total"] == 50)
    owed = client.get(f"/customers/{cust['id']}", headers=ah).json()["financial_summary"]
    check("and the receivable grew by the credit sales",
          owed["outstanding_balance"] == 118 + 50, owed["outstanding_balance"])

    print("\n== 4. A sale that cannot happen leaves nothing behind ==")
    before_count = len(client.get("/invoices", headers=ah).json())
    before_stock = on_hand()
    r = client.post("/invoices", headers=ah, json={
        "customer_id": cust["id"],
        "items": [{"product_id": prod["id"], "quantity": 9999}]})
    check("more than the warehouse holds -> 400", r.status_code == 400, r.text[:250])
    check("and it says what is short", "Not enough stock" in r.text, r.text[:250])
    r = client.post("/invoices", headers=ah, json={
        "customer_id": cust["id"],
        "items": [{"product_id": prod["id"], "quantity": 1}],
        "payment": {"payment_method": "cash", "amount": 5000}})
    check("paying more than the bill -> 400", r.status_code == 400, r.text[:250])
    check("no half-finished sale was left",
          len(client.get("/invoices", headers=ah).json()) == before_count, before_count)
    check("and no stock moved for it", on_hand() == before_stock, (before_stock, on_hand()))
    check("a foreign warehouse -> 400",
          client.post("/invoices", headers=ah, json={
              "customer_id": cust["id"], "warehouse_id": str(uuid.uuid4()),
              "items": [{"product_id": prod["id"], "quantity": 1}]}).status_code == 400)
    check("another firm's customer -> 400",
          client.post("/invoices", headers=oh, json={
              "customer_id": cust["id"],
              "items": [{"product_id": prod["id"], "quantity": 1}]}).status_code == 400)

    print("\n== 5. Walk-in buyer at the counter (Phase 1L) ==")
    r = client.post("/invoices", headers=ah, json={
        "walk_in_customer": {"name": "Cash Customer", "mobile_number": "9765432100"},
        "sales_type": "POS Sale",
        "items": [{"product_id": prod["id"], "quantity": 1, "unit_price": 100, "tax_rate": 18}],
        "payment": {"payment_method": "cash", "amount": 118}})
    check("a sale with no customer_id -> 201", r.status_code == 201, r.text[:400])
    walk = r.json()
    check("no customer record is attached", walk["customer_id"] is None and walk["customer"] is None, walk)
    check("but who bought it is kept",
          walk["walk_in_name"] == "Cash Customer" and walk["walk_in_phone"] == "9765432100", walk)
    check("and it is paid", walk["status"] == "paid" and walk["outstanding_amount"] == 0, walk)
    check("no customer was created for the walk-in",
          len(client.get("/customers", headers=ah).json()) == 1)
    check("the anonymous receipt carries no customer",
          client.get("/payment-receipts", headers=ah,
                     params={"invoice_id": walk["id"]}).json()[0]["customer_id"] is None)
    unchanged = client.get(f"/customers/{cust['id']}", headers=ah).json()["financial_summary"]
    check("and no customer's ledger moved",
          unchanged["total_received"] == 236, unchanged["total_received"])
    check("neither a customer nor a walk-in -> 400",
          client.post("/invoices", headers=ah, json={
              "items": [{"product_id": prod["id"], "quantity": 1}]}).status_code == 400)
    for shape in ("simple", "detailed"):
        check(f"a walk-in invoice still prints as {shape}",
              client.get(f"/invoices/{walk['id']}/pdf", headers=ah,
                         params={"format": shape}).content[:5] == b"%PDF-")
    check("a part-paid walk-in keeps its own balance",
          client.post("/invoices", headers=ah, json={
              "walk_in_customer": {"name": "Cash Customer"},
              "items": [{"product_id": prod["id"], "quantity": 1, "unit_price": 100, "tax_rate": 0}],
              "payment": {"payment_method": "cash", "amount": 40}}).json()["outstanding_amount"] == 60)

    print("\n== 6. Payment receipts derive the customer from the invoice (Phase 1M) ==")
    bill = client.post("/invoices", headers=ah, json={
        "customer_id": cust["id"],
        "items": [{"product_id": prod["id"], "quantity": 10, "unit_price": 100, "tax_rate": 0}]}).json()
    check("a 1000 credit sale", bill["total"] == 1000 and bill["status"] == "unpaid", bill)
    r = client.post("/payment-receipts", headers=ah, json={
        "invoice_reference_id": bill["id"], "amount_received": 400,
        "payment_method": "bank_transfer", "transaction_reference": "UTR123456"})
    check("POST /payment-receipts with no customer_id -> 201", r.status_code == 201, r.text[:400])
    part = r.json()
    print(json.dumps(part, indent=2, default=str))
    check("the customer was derived from the invoice", part["customer_id"] == cust["id"], part)
    check("it reports the invoice position",
          part["invoice_total"] == 1000 and part["total_paid"] == 400
          and part["outstanding_amount"] == 600 and part["payment_status"] == "partial", part)
    after = client.get(f"/invoices/{bill['id']}", headers=ah).json()
    check("the invoice moved to partial in the same call",
          after["status"] == "partial" and after["amount_paid"] == 400
          and after["payment_status"] == "Partial", after)
    r = client.post("/payment-receipts", headers=ah, json={
        "invoice_reference_id": bill["id"], "amount_received": 600, "payment_method": "cash"})
    check("the balance clears it", r.json()["payment_status"] == "paid", r.json())
    check("and the invoice says paid",
          client.get(f"/invoices/{bill['id']}", headers=ah).json()["status"] == "paid")
    r = client.post("/payment-receipts", headers=ah, json={
        "invoice_reference_id": bill["id"], "amount_received": 1})
    check("paying a settled invoice -> 400", r.status_code == 400, r.text[:250])
    check("and it points at advances instead", "advance" in r.text.lower(), r.text[:250])

    print("\n== 7. Over-payment, advances and guard rails ==")
    small = client.post("/invoices", headers=ah, json={
        "customer_id": cust["id"],
        "items": [{"product_id": prod["id"], "quantity": 1, "unit_price": 100, "tax_rate": 0}]}).json()
    r = client.post("/payment-receipts", headers=ah, json={
        "invoice_reference_id": small["id"], "amount_received": 5000})
    check("more than the invoice owes -> 400", r.status_code == 400, r.text[:250])
    check("the invoice was left alone",
          client.get(f"/invoices/{small['id']}", headers=ah).json()["amount_paid"] == 0)
    before = client.get(f"/customers/{cust['id']}", headers=ah).json()["financial_summary"]
    adv = client.post("/payment-receipts", headers=ah, json={
        "customer_id": cust["id"], "amount_received": 5000, "payment_method": "cash",
        "note": "Advance for next month"})
    check("an advance with no invoice -> 201", adv.status_code == 201, adv.text[:300])
    check("it settles no invoice", adv.json()["invoice_id"] is None, adv.json())
    now_owed = client.get(f"/customers/{cust['id']}", headers=ah).json()["financial_summary"]
    check("but it does reduce what the customer owes",
          now_owed["outstanding_balance"] == round(before["outstanding_balance"] - 5000, 2),
          (before["outstanding_balance"], now_owed["outstanding_balance"]))
    check("a customer_id that disagrees with the invoice -> 400",
          client.post("/payment-receipts", headers=ah, json={
              "invoice_reference_id": small["id"], "customer_id": str(uuid.uuid4()),
              "amount_received": 10}).status_code == 400)
    check("neither invoice nor customer -> 400",
          client.post("/payment-receipts", headers=ah,
                      json={"amount_received": 10}).status_code == 400)
    check("zero or less -> 422",
          client.post("/payment-receipts", headers=ah, json={
              "customer_id": cust["id"], "amount_received": 0}).status_code == 422)
    check("another firm's invoice -> 400",
          client.post("/payment-receipts", headers=oh, json={
              "invoice_reference_id": small["id"], "amount_received": 10}).status_code == 400)

    print("\n== 8. Receipts: read, correct, void ==")
    numbered = client.post("/payment-receipts", headers=ah, json={
        "invoice_reference_id": small["id"], "amount_received": 60,
        "receipt_number": "REC-COUNTER-001", "payment_method": "cash"}).json()
    check("a custom receipt number is kept",
          numbered["receipt_number"] == "REC-COUNTER-001", numbered)
    check("reusing it -> 409",
          client.post("/payment-receipts", headers=ah, json={
              "invoice_reference_id": small["id"], "amount_received": 1,
              "receipt_number": "REC-COUNTER-001"}).status_code == 409)
    check("GET /payment-receipts/{id} -> 200",
          client.get(f"/payment-receipts/{numbered['id']}", headers=ah).status_code == 200)
    check("filter by customer",
          all(x["customer_id"] == cust["id"] for x in client.get(
              "/payment-receipts", headers=ah, params={"customer_id": cust["id"]}).json()))
    check("cross-firm receipt -> 404",
          client.get(f"/payment-receipts/{numbered['id']}", headers=oh).status_code == 404)
    r = client.patch(f"/payment-receipts/{numbered['id']}", headers=ah, json={
        "payment_method": "cheque", "transaction_reference": "CHQ-77"})
    check("PATCH corrects the details -> 200",
          r.status_code == 200 and r.json()["payment_method"] == "cheque"
          and r.json()["transaction_reference"] == "CHQ-77", r.text[:250])
    check("the amount is untouched by a correction", r.json()["amount_received"] == 60)
    paid_before = client.get(f"/invoices/{small['id']}", headers=ah).json()["amount_paid"]
    received_before = client.get(f"/customers/{cust['id']}",
                                 headers=ah).json()["financial_summary"]["total_received"]
    check("DELETE voids it -> 204",
          client.delete(f"/payment-receipts/{numbered['id']}", headers=ah).status_code == 204)
    voided_invoice = client.get(f"/invoices/{small['id']}", headers=ah).json()
    check("the invoice gets its balance back",
          voided_invoice["amount_paid"] == round(paid_before - 60, 2), voided_invoice["amount_paid"])
    check("and its status is restated", voided_invoice["status"] == "unpaid", voided_invoice["status"])
    check("the customer's received figure comes back too",
          client.get(f"/customers/{cust['id']}", headers=ah).json()
          ["financial_summary"]["total_received"] == round(received_before - 60, 2))
    check("a voided receipt is gone",
          client.get(f"/payment-receipts/{numbered['id']}", headers=ah).status_code == 404)

    print("\n== 9. The customer ledger (Phase 1N) ==")
    r = client.get(f"/customers/{cust['id']}/ledger", headers=ah)
    check("GET /customers/{id}/ledger -> 200", r.status_code == 200, r.text[:300])
    led = r.json()
    print(json.dumps({"summary": led["summary"], "ageing": led["ageing"],
                      "transactions": led["transactions"][:3]}, indent=2, default=str))
    money = client.get(f"/customers/{cust['id']}", headers=ah).json()["financial_summary"]
    check("the summary agrees with the customer",
          led["summary"]["total_billed"] == money["total_billed"]
          and led["summary"]["total_received"] == money["total_received"]
          and led["summary"]["outstanding"] == money["outstanding_balance"], led["summary"])
    check("the credit limit and what is left of it are there",
          led["summary"]["credit_limit"] == 10000
          and led["summary"]["available_credit"] == round(max(10000 - led["summary"]["outstanding"], 0), 2),
          led["summary"])
    check("every invoice debits and every payment credits",
          all(t["debit"] > 0 and t["credit"] == 0
              for t in led["transactions"] if t["type"] == "invoice")
          and all(t["credit"] > 0 and t["debit"] == 0
                  for t in led["transactions"] if t["type"] == "payment"),
          led["transactions"][:4])
    check("the running balance is the debits less the credits",
          led["transactions"][-1]["balance"] == round(
              sum(t["debit"] for t in led["transactions"])
              - sum(t["credit"] for t in led["transactions"]), 2),
          led["transactions"][-1])
    check("invoices carry their number and status",
          all(t["reference_number"] and t["status"]
              for t in led["transactions"] if t["type"] == "invoice"))
    check("receipts carry their RCPT number",
          all(t["reference_number"] for t in led["transactions"] if t["type"] == "payment"))
    check("the ledger is ordered oldest first",
          led["transactions"] == sorted(led["transactions"], key=lambda t: t["date"]))
    check("an anonymous counter sale is in nobody's ledger",
          all(t["reference_number"] != walk["invoice_number"] for t in led["transactions"]))
    check("another firm's customer -> 404",
          client.get(f"/customers/{cust['id']}/ledger", headers=oh).status_code == 404)

    print("\n== 10. Ageing buckets and what is overdue ==")
    aged = client.post("/invoices", headers=ah, json={
        "customer_id": cust["id"],
        "items": [{"product_id": prod["id"], "quantity": 1, "unit_price": 100, "tax_rate": 0}]}).json()
    older = client.post("/invoices", headers=ah, json={
        "customer_id": cust["id"],
        "items": [{"product_id": prod["id"], "quantity": 2, "unit_price": 100, "tax_rate": 0}]}).json()
    # Age them the only honest way: move their dates back, as a real database would
    # have them after a few months of trading.
    from app.core.database import SessionLocal as _AgeSession
    from sqlalchemy import text as _age_text
    _adb = _AgeSession()
    try:
        _adb.execute(_age_text(
            "UPDATE invoices SET invoice_date = :d, due_date = :d WHERE id = :i"),
            {"d": _dt.now(_tz.utc) - _td(days=45), "i": aged["id"]})
        _adb.execute(_age_text(
            "UPDATE invoices SET invoice_date = :d, due_date = :d WHERE id = :i"),
            {"d": _dt.now(_tz.utc) - _td(days=200), "i": older["id"]})
        _adb.commit()
    finally:
        _adb.close()
    led = client.get(f"/customers/{cust['id']}/ledger", headers=ah).json()
    print(json.dumps(led["ageing"], indent=2))
    check("a 45-day-old bill lands in 31_60", led["ageing"]["31_60"] >= 100, led["ageing"])
    check("a 200-day-old bill lands in 90_plus", led["ageing"]["90_plus"] >= 200, led["ageing"])
    unpaid_total = round(sum(
        row["total"] - row["amount_paid"]
        for row in client.get("/invoices", headers=ah).json()
        if row["customer_id"] == cust["id"] and not row["is_credit_note"]), 2)
    check("the buckets add up to every unpaid invoice",
          round(sum(led["ageing"].values()), 2) == unpaid_total,
          (led["ageing"], unpaid_total))
    check("both overdue bills are counted as overdue",
          led["summary"]["overdue_amount"] >= 300, led["summary"]["overdue_amount"])
    check("a fresh unpaid bill sits in 0_30",
          led["ageing"]["0_30"] > 0, led["ageing"])

    print("\n== 11. An opening balance and a credit note are on the account ==")
    opened = client.post("/customers", headers=ah, json={
        "basic_information": {"customer_name": "Old Account"},
        "payment_information": {"opening_balance": 2500, "credit_limit": 5000}}).json()
    led = client.get(f"/customers/{opened['id']}/ledger", headers=ah).json()
    check("the opening balance heads the ledger",
          led["transactions"][0]["type"] == "opening_balance"
          and led["transactions"][0]["debit"] == 2500, led["transactions"][:1])
    check("and it is what they owe", led["summary"]["outstanding"] == 2500, led["summary"])
    check("available credit is the limit less that",
          led["summary"]["available_credit"] == 2500, led["summary"])
    sold = on_hand()
    cn_invoice = client.post("/invoices", headers=ah, json={
        "customer_id": opened["id"],
        "items": [{"product_id": prod["id"], "quantity": 1, "unit_price": 100, "tax_rate": 0}]}).json()
    check("the counter sale took the stock", on_hand() == sold - 1, (sold, on_hand()))
    client.post(f"/invoices/{cn_invoice['id']}/credit-note", headers=ah,
                json={"reason": "Damaged in transit"})
    check("the credit note puts it back in the warehouse, not just the catalog",
          on_hand() == sold, (sold, on_hand()))
    check("and the return is on the movement ledger",
          any(m["movement_type"] == "sales_return"
              for m in client.get(f"/inventory/{prod['id']}", headers=ah).json()["movements"]))
    led = client.get(f"/customers/{opened['id']}/ledger", headers=ah).json()
    note = next((t for t in led["transactions"] if t["type"] == "credit_note"), None)
    check("a credit note credits the account", note is not None and note["credit"] == 100, note)
    check("its reason and the invoice it credits are on the line",
          note and note["description"].startswith("Damaged in transit")
          and cn_invoice["invoice_number"] in note["description"], note)
    check("and it is a credit note document of its own",
          note and note["reference_number"].startswith("CN-"), note)
    check("and the receivable is back to the opening balance",
          led["summary"]["outstanding"] == 2500, led["summary"])


_phase1kn_checks()


def _phase1op_checks():
    """Phase 1O + 1P: returns with a condition check, and batch / serial / barcode."""
    import json

    def hdr(t):
        return {"Authorization": f"Bearer {t}"}

    def firm(name):
        reg = client.post("/auth/register", json={
            "organization_name": name, "admin_name": "Admin",
            "email": f"{name.lower()}_{uuid.uuid4().hex[:8]}@f.com", "password": "Secret@123"}).json()
        return reg, hdr(reg["tokens"]["access_token"])

    ret, ah = firm("ReturnCo")
    other, oh = firm("OtherReturn")

    prod = client.post("/products", headers=ah, json={
        "name": "Bottle 1L", "price": 100, "total_inventory": 100, "tax_rate": 0,
        "barcode": "8901234567890"}).json()
    cust = client.post("/customers", headers=ah, json={
        "basic_information": {"customer_name": "Mehta Stores"},
        "payment_information": {"credit_limit": 50000}}).json()
    warehouse_id = client.get("/warehouses", headers=ah).json()[0]["id"]

    def on_hand(product_id=None):
        rows = client.get("/warehouses/stock", headers=ah,
                          params={"product_id": product_id or prod["id"]}).json()
        return rows[0]["on_hand"] if rows else 0

    def owed():
        return client.get(f"/customers/{cust['id']}", headers=ah).json(
        )["financial_summary"]["outstanding_balance"]

    print("\n== 1. A return is a request, not a stock movement (Phase 1O) ==")
    bill = client.post("/invoices", headers=ah, json={
        "customer_id": cust["id"],
        "items": [{"product_id": prod["id"], "quantity": 10, "unit_price": 100, "tax_rate": 0}]}).json()
    sold_stock = on_hand()
    billed = owed()
    r = client.post("/sales-returns", headers=ah, json={
        "invoice_reference_id": bill["id"], "reason": "Damaged goods",
        "items": [{"invoice_item_id": bill["items"][0]["id"], "quantity_returned": 4}]})
    check("POST /sales-returns -> 201", r.status_code == 201, r.text[:400])
    req = r.json()
    print(json.dumps({k: req[k] for k in ("return_number", "status", "return_status",
                                          "customer_id", "credit_amount", "items")},
                     indent=2, default=str))
    check("it comes back as a request", req["status"] == "requested", req["status"])
    check("with a RET number", req["return_number"].startswith("RET-"), req["return_number"])
    check("the customer came from the invoice", req["customer_id"] == cust["id"], req)
    check("the line is priced as it was billed",
          req["items"][0]["unit_price"] == 100 and req["items"][0]["line_total"] == 400,
          req["items"][0])
    check("and points at the invoice line",
          req["items"][0]["invoice_item_id"] == bill["items"][0]["id"])
    check("NO stock came back yet", on_hand() == sold_stock, (sold_stock, on_hand()))
    check("and nothing was credited yet", owed() == billed, (billed, owed()))
    check("no credit note exists yet", req["credit_note_id"] is None, req)

    print("\n== 2. What can be returned is capped by what was invoiced ==")
    r = client.post("/sales-returns", headers=ah, json={
        "invoice_reference_id": bill["id"],
        "items": [{"invoice_item_id": bill["items"][0]["id"], "quantity_returned": 7}]})
    check("more than is left of the line -> 400", r.status_code == 400, r.text[:250])
    check("and it says how much is left", "only 6" in r.text, r.text[:250])
    check("a line from another invoice -> 400",
          client.post("/sales-returns", headers=ah, json={
              "invoice_reference_id": bill["id"],
              "items": [{"invoice_item_id": str(uuid.uuid4()), "quantity_returned": 1}]
          }).status_code == 400)
    check("no items -> 422",
          client.post("/sales-returns", headers=ah, json={
              "invoice_reference_id": bill["id"], "items": []}).status_code == 422)
    check("neither invoice nor customer -> 400",
          client.post("/sales-returns", headers=ah, json={
              "items": [{"product_id": prod["id"], "quantity_returned": 1}]}).status_code == 400)
    check("another firm's invoice -> 400",
          client.post("/sales-returns", headers=oh, json={
              "invoice_reference_id": bill["id"],
              "items": [{"product_id": prod["id"], "quantity_returned": 1}]}).status_code == 400)

    print("\n== 3. Goods arrive: still no stock movement ==")
    r = client.patch(f"/sales-returns/{req['id']}/receive", headers=ah, json={
        "items": [{"return_item_id": req["items"][0]["id"], "received_quantity": 3}]})
    check("PATCH /receive -> 200", r.status_code == 200, r.text[:300])
    received = r.json()
    check("it moves to received", received["status"] == "received", received["status"])
    check("recording only 3 of the 4 asked for",
          received["items"][0]["received_quantity"] == 3, received["items"][0])
    check("received_at is stamped", received["received_at"] is not None)
    check("still no stock back", on_hand() == sold_stock, (sold_stock, on_hand()))
    check("receiving more than was asked for -> 400",
          client.patch(f"/sales-returns/{req['id']}/receive", headers=ah, json={
              "items": [{"return_item_id": req["items"][0]["id"], "received_quantity": 99}]
          }).status_code == 400)

    print("\n== 4. Damaged goods never go back on the shelf ==")
    r = client.patch(f"/sales-returns/{req['id']}/approve", headers=ah, json={
        "items": [{"return_item_id": req["items"][0]["id"], "received_quantity": 3,
                   "condition": "damaged", "restock": True}]})
    check("asking to restock damaged goods -> 400", r.status_code == 400, r.text[:300])
    check("and it says why", "saleable" in r.text, r.text[:300])
    check("the return was left open",
          client.get(f"/sales-returns/{req['id']}", headers=ah).json()["status"] == "received")
    r = client.patch(f"/sales-returns/{req['id']}/approve", headers=ah, json={
        "items": [{"return_item_id": req["items"][0]["id"], "received_quantity": 3,
                   "condition": "damaged", "restock": False}]})
    check("approving them as damaged -> 200", r.status_code == 200, r.text[:300])
    done = r.json()
    check("it is approved", done["status"] == "approved", done["status"])
    check("nothing was restocked",
          done["items"][0]["restocked_quantity"] == 0 and on_hand() == sold_stock,
          (done["items"][0], sold_stock, on_hand()))
    check("but the customer was still credited 300",
          done["credit_amount"] == 300, done["credit_amount"])
    check("a credit note was raised", done["credit_note_id"] is not None, done)
    note = client.get(f"/invoices/{done['credit_note_id']}", headers=ah).json()
    check("the credit note is its own document",
          note["is_credit_note"] and note["invoice_number"].startswith("CN-")
          and note["total"] == 300, note)
    check("it names the invoice it credits", note["id"] != bill["id"], note["id"])
    check("the original invoice is still a bill",
          client.get(f"/invoices/{bill['id']}", headers=ah).json()["is_credit_note"] is False)
    check("and the receivable came down by the credit",
          owed() == round(billed - 300, 2), (billed, owed()))
    check("an approved return cannot be approved twice",
          client.patch(f"/sales-returns/{req['id']}/approve", headers=ah,
                       json={}).status_code == 400)
    check("nor edited",
          client.patch(f"/sales-returns/{req['id']}", headers=ah,
                       json={"return_reason": "Changed"}).status_code == 400)
    check("nor deleted",
          client.delete(f"/sales-returns/{req['id']}", headers=ah).status_code == 409)

    print("\n== 5. Saleable goods do go back ==")
    before = on_hand()
    good = client.post("/sales-returns", headers=ah, json={
        "invoice_reference_id": bill["id"], "reason": "Over-ordered",
        "items": [{"invoice_item_id": bill["items"][0]["id"], "quantity_returned": 2}]}).json()
    r = client.patch(f"/sales-returns/{good['id']}/approve", headers=ah, json={
        "items": [{"return_item_id": good["items"][0]["id"], "received_quantity": 2,
                   "condition": "saleable", "restock": True}]})
    check("approving saleable goods -> 200", r.status_code == 200, r.text[:300])
    check("they are back in the warehouse", on_hand() == before + 2, (before, on_hand()))
    check("the line records what was restocked",
          r.json()["items"][0]["restocked_quantity"] == 2, r.json()["items"][0])
    check("the return is on the movement ledger",
          any(m["movement_type"] == "sales_return"
              for m in client.get(f"/inventory/{prod['id']}", headers=ah).json()["movements"]))

    print("\n== 6. Rejecting a return, and withdrawing one ==")
    bad = client.post("/sales-returns", headers=ah, json={
        "invoice_reference_id": bill["id"],
        "items": [{"invoice_item_id": bill["items"][0]["id"], "quantity_returned": 1}]}).json()
    stock_now, owed_now = on_hand(), owed()
    r = client.patch(f"/sales-returns/{bad['id']}/reject", headers=ah,
                     json={"reason": "Returned after the window"})
    check("PATCH /reject -> 200 rejected", r.status_code == 200 and r.json()["status"] == "rejected",
          r.text[:250])
    check("the reason is kept", r.json()["rejected_reason"] == "Returned after the window")
    check("nothing restocked and nothing credited",
          on_hand() == stock_now and owed() == owed_now, (stock_now, on_hand(), owed_now, owed()))
    check("a rejected return has no credit note", r.json()["credit_note_id"] is None)
    withdrawn = client.post("/sales-returns", headers=ah, json={
        "invoice_reference_id": bill["id"],
        "items": [{"invoice_item_id": bill["items"][0]["id"], "quantity_returned": 1}]}).json()
    check("DELETE withdraws an open request -> 204",
          client.delete(f"/sales-returns/{withdrawn['id']}", headers=ah).status_code == 204)
    check("and it is gone",
          client.get(f"/sales-returns/{withdrawn['id']}", headers=ah).status_code == 404)
    check("its quantity is free to return again",
          client.post("/sales-returns", headers=ah, json={
              "invoice_reference_id": bill["id"],
              "items": [{"invoice_item_id": bill["items"][0]["id"],
                         "quantity_returned": 1}]}).status_code == 201)
    check("filter by status",
          all(x["status"] == "approved" for x in client.get(
              "/sales-returns", headers=ah, params={"status": "approved"}).json()))
    check("cross-firm return -> 404",
          client.get(f"/sales-returns/{req['id']}", headers=oh).status_code == 404)
    check("the return shows on the customer's ledger as a credit",
          any(t["type"] == "credit_note" and t["credit"] == 300
              for t in client.get(f"/customers/{cust['id']}/ledger", headers=ah).json()["transactions"]))

    print("\n== 7. Goods received in batches (Phase 1P) ==")
    batched = client.post("/products", headers=ah, json={
        "name": "Milk 500ml", "price": 30, "total_inventory": 0,
        "batch_tracking": True, "expiry_tracking": True, "barcode": "8909998887776"}).json()
    r = client.post(f"/warehouses/{warehouse_id}/stock/adjust", headers=ah, json={
        "product_id": batched["id"], "quantity": 50, "movement_type": "purchase_in",
        "batch": {"batch_number": "B-2408", "manufacturing_date": "2026-07-01T00:00:00Z",
                  "expiry_date": "2027-02-01T00:00:00Z", "mrp": 35}})
    check("receiving a batch -> 200", r.status_code == 200, r.text[:300])
    check("the warehouse count went up", r.json()["on_hand"] == 50, r.json())
    r = client.post(f"/warehouses/{warehouse_id}/stock/adjust", headers=ah, json={
        "product_id": batched["id"], "quantity": 30, "movement_type": "purchase_in",
        "batch": {"batch_number": "B-2409", "expiry_date": "2026-09-01T00:00:00Z"}})
    check("a second batch -> 200", r.status_code == 200 and r.json()["on_hand"] == 80, r.text[:200])
    lots = client.get(f"/products/{batched['id']}/batches", headers=ah).json()
    print(json.dumps(lots, indent=2, default=str))
    check("GET /products/{id}/batches lists both", len(lots) == 2, lots)
    check("earliest expiry first", lots[0]["batch_number"] == "B-2409", [l["batch_number"] for l in lots])
    check("the quantities are per lot",
          {l["batch_number"]: l["quantity"] for l in lots} == {"B-2409": 30, "B-2408": 50}, lots)
    check("the manufacturing date and MRP were kept",
          any(l["batch_number"] == "B-2408" and l["mrp"] == 35
              and l["manufacturing_date"] is not None for l in lots), lots)
    check("days to expiry is worked out", all(l["days_to_expiry"] is not None for l in lots), lots)
    check("an untracked product has no lots",
          client.get(f"/products/{prod['id']}/batches", headers=ah).json() == [])

    print("\n== 8. A sale takes the oldest stock first, or the lot you name ==")
    sale = client.post("/invoices", headers=ah, json={
        "customer_id": cust["id"],
        "items": [{"product_id": batched["id"], "quantity": 10, "unit_price": 30, "tax_rate": 0}]}).json()
    check("the line records the lot it came out of",
          sale["items"][0]["batch_number"] == "B-2409", sale["items"][0])
    check("with its expiry date", sale["items"][0]["expiry_date"] is not None, sale["items"][0])
    lots = {l["batch_number"]: l["quantity"]
            for l in client.get(f"/products/{batched['id']}/batches", headers=ah).json()}
    check("and that lot went down", lots == {"B-2409": 20, "B-2408": 50}, lots)
    named = client.post("/invoices", headers=ah, json={
        "customer_id": cust["id"],
        "items": [{"product_id": batched["id"], "quantity": 5, "unit_price": 30,
                   "tax_rate": 0, "batch_number": "B-2408"}]}).json()
    check("naming a lot sells from it", named["items"][0]["batch_number"] == "B-2408",
          named["items"][0])
    lots = {l["batch_number"]: l["quantity"]
            for l in client.get(f"/products/{batched['id']}/batches", headers=ah).json()}
    check("the named lot went down", lots == {"B-2409": 20, "B-2408": 45}, lots)
    check("the lots still add up to the warehouse count",
          sum(lots.values()) == on_hand(batched["id"]), (lots, on_hand(batched["id"])))
    r = client.post("/invoices", headers=ah, json={
        "customer_id": cust["id"],
        "items": [{"product_id": batched["id"], "quantity": 1, "unit_price": 30,
                   "batch_number": "B-NOPE"}]})
    check("a lot that is not in stock -> 400", r.status_code == 400, r.text[:250])
    r = client.post("/invoices", headers=ah, json={
        "customer_id": cust["id"],
        "items": [{"product_id": batched["id"], "quantity": 40, "unit_price": 30,
                   "batch_number": "B-2409"}]})
    check("more than that lot holds -> 400", r.status_code == 400, r.text[:250])
    check("and the batch says how much is left", "20 left" in r.text, r.text[:250])
    check("the tax invoice prints the batch and expiry",
          client.get(f"/invoices/{sale['id']}/pdf", headers=ah, params={
              "format": "detailed"}).content[:5] == b"%PDF-")

    print("\n== 9. Expiring stock ==")
    soon = client.get("/inventory/expiring", headers=ah, params={"within_days": 30}).json()
    check("GET /inventory/expiring finds the near-dated lot",
          any(row["batch_number"] == "B-2409" for row in soon), soon)
    check("it names the product and warehouse",
          all(row["product_name"] and row["warehouse_name"] for row in soon), soon)
    check("the far-dated lot is not in a 30-day window",
          all(row["batch_number"] != "B-2408" for row in soon), soon)
    check("a wider window finds it",
          any(row["batch_number"] == "B-2408" for row in client.get(
              "/inventory/expiring", headers=ah, params={"within_days": 400}).json()))
    check("another firm sees none of it",
          client.get("/inventory/expiring", headers=oh, params={"within_days": 400}).json() == [])

    print("\n== 10. Serial-tracked units ==")
    serialed = client.post("/products", headers=ah, json={
        "name": "Water Purifier", "price": 8000, "total_inventory": 0,
        "serial_number_tracking": True}).json()
    r = client.post(f"/warehouses/{warehouse_id}/stock/adjust", headers=ah, json={
        "product_id": serialed["id"], "quantity": 3, "movement_type": "purchase_in",
        "serial_numbers": ["SN001", "SN002", "SN003"]})
    check("receiving three units with their serials -> 200", r.status_code == 200, r.text[:300])
    units = client.get(f"/products/{serialed['id']}/serials", headers=ah).json()
    check("all three are on record and in stock",
          len(units) == 3 and all(u["status"] == "in_stock" for u in units), units)
    check("a serial count that disagrees with the quantity -> 400",
          client.post(f"/warehouses/{warehouse_id}/stock/adjust", headers=ah, json={
              "product_id": serialed["id"], "quantity": 2, "movement_type": "purchase_in",
              "serial_numbers": ["SN010"]}).status_code == 400)
    check("a serial already in stock -> 400",
          client.post(f"/warehouses/{warehouse_id}/stock/adjust", headers=ah, json={
              "product_id": serialed["id"], "quantity": 1, "movement_type": "purchase_in",
              "serial_numbers": ["SN001"]}).status_code == 400)
    sold = client.post("/invoices", headers=ah, json={
        "customer_id": cust["id"],
        "items": [{"product_id": serialed["id"], "quantity": 1, "unit_price": 8000,
                   "tax_rate": 0, "serial_numbers": ["SN002"]}]}).json()
    check("the invoice line records the unit sold",
          sold["items"][0]["serial_numbers"] == ["SN002"], sold["items"][0])
    units = {u["serial_number"]: u["status"]
             for u in client.get(f"/products/{serialed['id']}/serials", headers=ah).json()}
    check("that unit is sold, the others are not",
          units == {"SN001": "in_stock", "SN002": "sold", "SN003": "in_stock"}, units)
    check("selling it again -> 400",
          client.post("/invoices", headers=ah, json={
              "customer_id": cust["id"],
              "items": [{"product_id": serialed["id"], "quantity": 1, "unit_price": 8000,
                         "serial_numbers": ["SN002"]}]}).status_code == 400)
    check("a serial that is not on record -> 400",
          client.post("/invoices", headers=ah, json={
              "customer_id": cust["id"],
              "items": [{"product_id": serialed["id"], "quantity": 1, "unit_price": 8000,
                         "serial_numbers": ["SN999"]}]}).status_code == 400)
    check("filter by status",
          [u["serial_number"] for u in client.get(
              f"/products/{serialed['id']}/serials", headers=ah,
              params={"status": "sold"}).json()] == ["SN002"])
    auto = client.post("/invoices", headers=ah, json={
        "customer_id": cust["id"],
        "items": [{"product_id": serialed["id"], "quantity": 1, "unit_price": 8000,
                   "tax_rate": 0}]}).json()
    check("no serial sent sells the oldest unit in stock",
          auto["items"][0]["serial_numbers"] == ["SN001"], auto["items"][0])

    print("\n== 11. A returned unit goes back on the shelf ==")
    back = client.post("/sales-returns", headers=ah, json={
        "invoice_reference_id": sold["id"], "reason": "Wrong model",
        "items": [{"invoice_item_id": sold["items"][0]["id"], "quantity_returned": 1}]}).json()
    client.patch(f"/sales-returns/{back['id']}/approve", headers=ah, json={
        "items": [{"return_item_id": back["items"][0]["id"], "received_quantity": 1,
                   "condition": "saleable", "restock": True}]})
    units = {u["serial_number"]: u["status"]
             for u in client.get(f"/products/{serialed['id']}/serials", headers=ah).json()}
    check("SN002 is back in stock", units["SN002"] == "in_stock", units)
    check("and it can be sold again",
          client.post("/invoices", headers=ah, json={
              "customer_id": cust["id"],
              "items": [{"product_id": serialed["id"], "quantity": 1, "unit_price": 8000,
                         "serial_numbers": ["SN002"]}]}).status_code == 201)
    milk_back = client.post("/sales-returns", headers=ah, json={
        "invoice_reference_id": named["id"], "reason": "Over-ordered",
        "items": [{"invoice_item_id": named["items"][0]["id"], "quantity_returned": 5}]}).json()
    client.patch(f"/sales-returns/{milk_back['id']}/approve", headers=ah, json={
        "items": [{"return_item_id": milk_back["items"][0]["id"], "received_quantity": 5,
                   "condition": "saleable", "restock": True}]})
    lots = {l["batch_number"]: l["quantity"]
            for l in client.get(f"/products/{batched['id']}/batches", headers=ah).json()}
    check("a returned batch goes back into the lot it was sold from",
          lots["B-2408"] == 50, lots)
    check("and the lots still match the warehouse",
          sum(lots.values()) == on_hand(batched["id"]), (lots, on_hand(batched["id"])))

    print("\n== 12. Barcode lookup ==")
    r = client.get("/products", headers=ah, params={"barcode": "8901234567890"})
    check("GET /products?barcode= finds the one product",
          r.status_code == 200 and [p["id"] for p in r.json()] == [prod["id"]], r.text[:250])
    check("an unknown barcode finds nothing",
          client.get("/products", headers=ah, params={"barcode": "0000000000"}).json() == [])
    check("another firm's barcode finds nothing",
          client.get("/products", headers=oh, params={"barcode": "8901234567890"}).json() == [])
    check("the product code route still resolves a barcode",
          client.get(f"/products/8901234567890", headers=ah).json()["id"] == prod["id"])
    variant_product = client.post("/products", headers=ah, json={
        "name": "Juice", "price": 60,
        "variations": [{"name": "1L", "price": 60, "barcode": "7771112223334"}]}).json()
    check("scanning a variant's barcode finds its product",
          [p["id"] for p in client.get("/products", headers=ah, params={
              "barcode": "7771112223334"}).json()] == [variant_product["id"]])


_phase1op_checks()


def _staff_overview_fleet_checks():
    """The delivery workspace overview reports the van and the proof of delivery."""
    import json

    def hdr(t):
        return {"Authorization": f"Bearer {t}"}

    reg = client.post("/auth/register", json={
        "organization_name": "FleetView", "admin_name": "Admin",
        "email": f"fleet_{uuid.uuid4().hex[:8]}@f.com", "password": "Secret@123"}).json()
    ah = hdr(reg["tokens"]["access_token"])

    roles = {r["name"]: r for r in client.get("/roles", headers=ah).json()}
    dp_email = f"fdp_{uuid.uuid4().hex[:6]}@f.com"
    dp = client.post("/users", headers=ah, json={
        "basic_information": {"first_name": "Imran", "last_name": "Khan"},
        "contact_information": {"official_email": dp_email},
        "login_security": {"password": "Dp@123456", "confirm_password": "Dp@123456"},
        "employment_information": {"role_id": roles["Delivery Partner"]["id"]}}).json()
    dp_hdr = hdr(client.post("/auth/login", json={
        "email": dp_email, "password": "Dp@123456"}).json()["tokens"]["access_token"])

    veh = client.post("/vehicles", headers=ah, json={
        "vehicle_number": "MH 12 KL 9087", "vehicle_type": "Tempo", "capacity_kg": 1200}).json()
    prod = client.post("/products", headers=ah, json={
        "name": "Jar 20L", "price": 60, "total_inventory": 50}).json()
    cust = client.post("/customers", headers=ah, json={
        "basic_information": {"customer_name": "Corner Cafe"}}).json()

    order = client.post("/orders", headers=ah, json={
        "customer_id": cust["id"],
        "items": [{"product_id": prod["id"], "quantity": 5, "unit_price": 60}]}).json()
    dlv = client.post("/deliveries", headers=ah, json={
        "order_id": order["id"], "delivery_partner_id": dp["id"], "vehicle_id": veh["id"],
        "items": [{"order_item_id": order["items"][0]["id"], "planned_quantity": 5}]}).json()

    print("\n== The van is on the overview while the delivery is still out ==")
    over = client.get(f"/users/{dp['id']}/overview", headers=ah).json()
    check("workspace is delivery", over["workspace"] == "delivery", over["workspace"])
    print(json.dumps(over["vehicle"], indent=2, default=str))
    check("the real vehicle is reported, not a null placeholder",
          over["vehicle"] and over["vehicle"]["vehicle_number"] == "MH 12 KL 9087"
          and over["vehicle"]["id"] == veh["id"], over["vehicle"])
    check("with its type", over["vehicle"]["vehicle_type"] == "Tempo", over["vehicle"])
    check("nothing is loaded onto it yet",
          over["vehicle"]["items"] == 0 and over["vehicle"]["loaded_at"] is None, over["vehicle"])

    client.post(f"/deliveries/{dlv['id']}/pick", headers=ah, json={
        "items": [{"delivery_item_id": dlv["items"][0]["id"], "picked_quantity": 5}]
    })
    client.post(f"/deliveries/{dlv['id']}/ready", headers=ah)
    client.post("/vehicle-stock/loading", headers=ah, json={
        "delivery_id": dlv["id"],
        "items": [{"delivery_item_id": dlv["items"][0]["id"], "loaded_quantity": 5}]})
    over = client.get(f"/users/{dp['id']}/overview", headers=ah).json()
    check("once loaded, the badge carries the van and its load",
          over["vehicle"]["vehicle_number"] == "MH 12 KL 9087"
          and over["vehicle"]["items"] == 1
          and over["vehicle"]["loaded_at"] is not None, over["vehicle"])

    print("\n== Proof of delivery is counted only when it was actually captured ==")
    client.patch(f"/deliveries/by-id/{dlv['id']}", headers=ah, json={"status": "in_transit"})
    client.post(f"/deliveries/{dlv['id']}/confirm", headers=dp_hdr, json={
        "items": [{"delivery_item_id": dlv["items"][0]["id"], "delivered_quantity": 5}]})
    over = client.get(f"/users/{dp['id']}/overview", headers=ah).json()
    delivered = next((a for a in over["recent_activity"] if a["type"] == "delivery_completed"), None)
    check("a delivery with no POD reads as pending, not captured",
          delivered and delivered["pod_status"] == "pending", delivered)
    check("and none is counted as completed",
          over["summary"]["pod_completed"] == 0, over["summary"])

    photo = client.post("/files/upload", headers=dp_hdr, files={
        "file": ("pod.png", bytes.fromhex(
            "89504e470d0a1a0a0000000d494844520000000100000001080600000"
            "01f15c4890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"),
        "image/png")}).json()
    o2 = client.post("/orders", headers=ah, json={
        "customer_id": cust["id"],
        "items": [{"product_id": prod["id"], "quantity": 3, "unit_price": 60}]}).json()
    d2 = client.post("/deliveries", headers=ah, json={
        "order_id": o2["id"], "delivery_partner_id": dp["id"], "vehicle_id": veh["id"],
        "items": [{"order_item_id": o2["items"][0]["id"], "planned_quantity": 3}]}).json()
    client.post(f"/deliveries/{d2['id']}/accept", headers=dp_hdr)
    client.post(f"/deliveries/{d2['id']}/load", headers=ah)
    client.patch(f"/deliveries/by-id/{d2['id']}", headers=ah, json={"status": "in_transit"})
    r = client.post(f"/deliveries/{d2['id']}/confirm", headers=dp_hdr, json={
        "items": [{"delivery_item_id": d2["items"][0]["id"], "delivered_quantity": 3}],
        "pod_photo_file_ids": [photo["file_id"]], "signature_file_id": photo["file_id"]})
    check("confirming with a POD -> 200", r.status_code == 200, r.text[:300])
    over = client.get(f"/users/{dp['id']}/overview", headers=ah).json()
    captured = [a for a in over["recent_activity"]
                if a["type"] == "delivery_completed" and a["pod_status"] == "captured"]
    check("that delivery reads as captured", len(captured) == 1, over["recent_activity"][:3])
    check("and exactly one POD is counted for the period",
          over["summary"]["pod_completed"] == 1, over["summary"])
    check("no route, stops or view-route field was invented",
          not any(key in over for key in ("route", "route_status", "stops", "view_route")),
          sorted(over))
    check("current location stays unavailable without a real ping",
          over["current_location"]["available"] is False
          and over["current_location"]["latitude"] is None, over["current_location"])


_staff_overview_fleet_checks()

print("\n== every model column made nullable is relaxed on Postgres too ==")
# SQLite ignores a NOT NULL that Postgres enforces, so a column made nullable in the
# model looks fine locally and 500s in production. Anything nullable in the model that
# was once mandatory has to be listed for the live database as well.
from app.core.database import _RELAX_NOT_NULL as _relax  # noqa: E402
from app.core.database import Base as _RelaxBase  # noqa: E402
from app.models import CustomerPayment as _RelaxPayment  # noqa: E402

check("an anonymous walk-in payment needs no customer",
      _RelaxPayment.__table__.c.customer_id.nullable)
check("and the live database is told to drop that constraint",
      ("customer_payments", "customer_id") in _relax, _relax)
for _table, _column in _relax:
    _model_table = _RelaxBase.metadata.tables.get(_table)
    check(f"{_table}.{_column} is still nullable in the model",
          _model_table is not None and _column in _model_table.c
          and _model_table.c[_column].nullable,
          f"{_table}.{_column} is listed as relaxed but the model requires it")


print("\n== Sales Order Hardening & Data-Scope Security ==")

# 1. Setup isolated organization
so_sec_org = client.post("/auth/register", json={
    "organization_name": "Sales Security Org", "admin_name": "Sec Admin",
    "email": f"sec_admin_{uuid.uuid4().hex[:6]}@sec.com", "password": "Secret@123"
}).json()
sec_admin_hdr = {"Authorization": f"Bearer {so_sec_org['tokens']['access_token']}"}

# Get default roles
sec_roles = client.get("/roles", headers=sec_admin_hdr).json()
sec_so_role = next(x for x in sec_roles if x["name"] == "Sales Officer")["id"]
sec_dp_role = next(x for x in sec_roles if x["name"] == "Delivery Partner")["id"]
sec_acc_role = next(x for x in sec_roles if x["name"] == "Accountant")["id"]

# Create Sales Officer A
so_a_email = f"so_a_{uuid.uuid4().hex[:6]}@sec.com"
so_a = client.post("/users", headers=sec_admin_hdr, json={
    "name": "Sales Officer A", "email": so_a_email,
    "password": "Password@123", "role_id": sec_so_role
}).json()
so_a_login = client.post("/auth/login", json={"email": so_a_email, "password": "Password@123"}).json()
so_a_hdr = {"Authorization": f"Bearer {so_a_login['tokens']['access_token']}"}

# Create Sales Officer B
so_b_email = f"so_b_{uuid.uuid4().hex[:6]}@sec.com"
so_b = client.post("/users", headers=sec_admin_hdr, json={
    "name": "Sales Officer B", "email": so_b_email,
    "password": "Password@123", "role_id": sec_so_role
}).json()
so_b_login = client.post("/auth/login", json={"email": so_b_email, "password": "Password@123"}).json()
so_b_hdr = {"Authorization": f"Bearer {so_b_login['tokens']['access_token']}"}

# Create Delivery Partner
sec_dp = client.post("/users", headers=sec_admin_hdr, json={
    "name": "Sec DP", "email": f"sec_dp_{uuid.uuid4().hex[:6]}@sec.com",
    "password": "Password@123", "role_id": sec_dp_role
}).json()

# Create Accountant
sec_acc = client.post("/users", headers=sec_admin_hdr, json={
    "name": "Sec Accountant", "email": f"sec_acc_{uuid.uuid4().hex[:6]}@sec.com",
    "password": "Password@123", "role_id": sec_acc_role
}).json()

# Create Customer and Product
sec_cust = client.post("/customers", headers=sec_admin_hdr, json={"name": "Sec Customer"}).json()
sec_prod = client.post("/products", headers=sec_admin_hdr, json={"name": "Sec Widget", "price": 100, "total_inventory": 500}).json()

# TEST 4: Sales Officer create without salesperson_id -> auto salesperson_id = current_user.id
order_a_res = client.post("/orders", headers=so_a_hdr, json={
    "customer_id": sec_cust["id"],
    "items": [{"product_id": sec_prod["id"], "quantity": 2, "unit_price": 100}]
})
check("TEST 4: Sales Officer A creates order without salesperson_id -> 201", order_a_res.status_code == 201, order_a_res.text)
order_a = order_a_res.json()
check("TEST 4: Order A salesperson_id auto-assigned to SO A", order_a["salesperson_id"] == so_a["id"], order_a)

# TEST 5: Sales Officer cannot impersonate another salesperson
order_a_spoof = client.post("/orders", headers=so_a_hdr, json={
    "customer_id": sec_cust["id"],
    "salesperson_id": so_b["id"],
    "items": [{"product_id": sec_prod["id"], "quantity": 1, "unit_price": 100}]
}).json()
check("TEST 5: Sales Officer spoofing salesperson_id is forced to own user id", order_a_spoof["salesperson_id"] == so_a["id"], order_a_spoof)

# Sales Officer B creates an order
order_b = client.post("/orders", headers=so_b_hdr, json={
    "customer_id": sec_cust["id"],
    "items": [{"product_id": sec_prod["id"], "quantity": 3, "unit_price": 100}]
}).json()
check("Order B created for SO B", order_b["salesperson_id"] == so_b["id"], order_b)

# TEST 1: Sales Officer list isolation
so_a_orders = client.get("/orders", headers=so_a_hdr).json()
so_a_order_ids = [o["id"] for o in so_a_orders]
check("TEST 1: SO A sees their own order", order_a["id"] in so_a_order_ids, so_a_order_ids)
check("TEST 1: SO A does NOT see SO B's order", order_b["id"] not in so_a_order_ids, so_a_order_ids)

# TEST 2: Sales Officer direct ID protection
check("TEST 2: SO A accessing SO B's order by ID returns 404",
      client.get(f"/orders/{order_b['id']}", headers=so_a_hdr).status_code == 404)

# TEST 3: Query filter cannot bypass scope
search_b_res = client.get("/orders", headers=so_a_hdr, params={"search": order_b["order_number"]}).json()
check("TEST 3: SO A searching for SO B's order_number returns empty",
      len(search_b_res) == 0, search_b_res)
cust_filter_res = client.get("/orders", headers=so_a_hdr, params={"customer_id": sec_cust["id"]}).json()
check("TEST 3: SO A filtering by customer_id sees only own orders",
      all(o["salesperson_id"] == so_a["id"] for o in cust_filter_res), cust_filter_res)

# TEST 6: Admin can assign salesperson
admin_order_res = client.post("/orders", headers=sec_admin_hdr, json={
    "customer_id": sec_cust["id"],
    "salesperson_id": so_b["id"],
    "items": [{"product_id": sec_prod["id"], "quantity": 1, "unit_price": 100}]
})
check("TEST 6: Admin can assign salesperson_id explicitly -> 201", admin_order_res.status_code == 201, admin_order_res.text)
check("TEST 6: Admin created order assigned to SO B", admin_order_res.json()["salesperson_id"] == so_b["id"])

# Cross-org salesperson assignment rejected for admin
other_firm = client.post("/auth/register", json={
    "organization_name": "Other Firm", "admin_name": "Other Admin",
    "email": f"other_{uuid.uuid4().hex[:6]}@other.com", "password": "Secret@123"
}).json()
check("TEST 6: Admin assigning cross-org salesperson -> 400",
      client.post("/orders", headers=sec_admin_hdr, json={
          "customer_id": sec_cust["id"],
          "salesperson_id": other_firm["user"]["id"],
          "items": [{"product_id": sec_prod["id"], "quantity": 1}]
      }).status_code == 400)

# TEST 7: Sales Officer cannot assign delivery partner on another salesperson's order
check("TEST 7: SO A cannot assign delivery partner on SO B's order -> 404",
      client.patch(f"/orders/{order_b['id']}/assign-delivery-partner", headers=so_a_hdr,
                   json={"delivery_partner_id": sec_dp["id"]}).status_code == 404)

# TEST 8: Sales Officer cannot cancel another salesperson's order
check("TEST 8: SO A cannot cancel SO B's order -> 404",
      client.patch(f"/orders/{order_b['id']}/cancel", headers=so_a_hdr,
                   json={"reason": "Malicious cancel"}).status_code == 404)

# TEST 9: Sales Officer cannot approve another salesperson's order
# Turn on approval requirement for the org
client.patch("/sales-workflow-settings", headers=sec_admin_hdr, json={"order_requires_approval": True})
order_b_approval = client.post("/orders", headers=so_b_hdr, json={
    "customer_id": sec_cust["id"],
    "items": [{"product_id": sec_prod["id"], "quantity": 1}]
}).json()
check("Order created awaiting approval", order_b_approval["status"] == "awaiting_approval")
check("TEST 9: SO A without approve permission cannot approve -> 403",
      client.patch(f"/orders/{order_b_approval['id']}/approve", headers=so_a_hdr).status_code == 403)
check("TEST 9: SO A without approve permission cannot reject -> 403",
      client.patch(f"/orders/{order_b_approval['id']}/reject", headers=so_a_hdr, json={"reason": "No"}).status_code == 403)
# Admin approves successfully
check("Admin approves order awaiting approval -> 200",
      client.patch(f"/orders/{order_b_approval['id']}/approve", headers=sec_admin_hdr).status_code == 200)
client.patch("/sales-workflow-settings", headers=sec_admin_hdr, json={"order_requires_approval": False})

# TEST 10: Invalid delivery partner (e.g. assigning Accountant or Sales Officer)
check("TEST 10: Assigning Accountant as delivery partner -> 400",
      client.patch(f"/orders/{order_a['id']}/assign-delivery-partner", headers=sec_admin_hdr,
                   json={"delivery_partner_id": sec_acc["id"]}).status_code == 400)
check("TEST 10: Assigning Sales Officer as delivery partner -> 400",
      client.patch(f"/orders/{order_a['id']}/assign-delivery-partner", headers=sec_admin_hdr,
                   json={"delivery_partner_id": so_b["id"]}).status_code == 400)

# TEST 11: Cross-organization delivery partner
other_dp = client.post("/users", headers={"Authorization": f"Bearer {other_firm['tokens']['access_token']}"}, json={
    "name": "Other DP", "email": f"other_dp_{uuid.uuid4().hex[:6]}@other.com",
    "password": "Password@123", "role": "delivery_partner"
}).json()
check("TEST 11: Assigning cross-org delivery partner -> 400",
      client.patch(f"/orders/{order_a['id']}/assign-delivery-partner", headers=sec_admin_hdr,
                   json={"delivery_partner_id": other_dp["id"]}).status_code == 400)

# TEST 12: Valid delivery partner
valid_assign = client.patch(f"/orders/{order_a['id']}/assign-delivery-partner", headers=sec_admin_hdr,
                            json={"delivery_partner_id": sec_dp["id"]})
check("TEST 12: Valid delivery partner assignment -> 200", valid_assign.status_code == 200, valid_assign.text)
va_json = valid_assign.json()
check("TEST 12: assigned_delivery_partner_id updated", va_json["assigned_delivery_partner_id"] == sec_dp["id"])
check("TEST 12: fulfilment_status moved to planned", va_json["fulfilment_status"] == "planned")
check("TEST 12: status moved to processing", va_json["status"] == "processing")

# TEST 13: Existing normal order flow
norm_order = client.post("/orders", headers=sec_admin_hdr, json={
    "customer_id": sec_cust["id"],
    "items": [{"product_id": sec_prod["id"], "quantity": 5, "unit_price": 100}]
}).json()
check("TEST 13: Normal order placed on creation", norm_order["status"] == "placed" and norm_order["fulfilment_status"] == "reserved")
cancel_res = client.patch(f"/orders/{norm_order['id']}/cancel", headers=sec_admin_hdr, json={"reason": "test cancel"})
check("TEST 13: Order cancelled and reservation released",
      cancel_res.status_code == 200 and cancel_res.json()["status"] == "cancelled" and cancel_res.json()["fulfilment_status"] == "not_started")

# TEST 14: Existing draft/confirm flow
client.patch("/sales-workflow-settings", headers=sec_admin_hdr, json={"draft_orders_enabled": True})
draft_order = client.post("/orders", headers=sec_admin_hdr, json={
    "customer_id": sec_cust["id"],
    "items": [{"product_id": sec_prod["id"], "quantity": 4, "unit_price": 100}]
}).json()
check("TEST 14: Order created as draft", draft_order["status"] == "draft" and draft_order["fulfilment_status"] == "not_started")
confirm_res = client.post(f"/orders/{draft_order['id']}/confirm", headers=sec_admin_hdr)
check("TEST 14: Draft confirmed -> 200", confirm_res.status_code == 200, confirm_res.text)
check("TEST 14: Confirmed order status placed and reserved",
      confirm_res.json()["status"] == "placed" and confirm_res.json()["fulfilment_status"] == "reserved")
client.patch("/sales-workflow-settings", headers=sec_admin_hdr, json={"draft_orders_enabled": False})


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
