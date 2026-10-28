"""Platform staff: Super Admin manages Sales Managers (create, edit, deactivate, delete); Sales
Managers read organizations and raise plan-upgrade / feature-change requests that only a Super
Admin can approve. Also: every signed-in user edits their own profile, password and photo.
"""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient

from app.core.database import Base, SessionLocal, auto_add_missing_columns, engine
from app.core.security import create_access_token, hash_password
from app.main import app
from app.models import Organization, OrganizationFeatureOverride, Plan, User, UserRole
from app.services import entitlement_service, org_service

Base.metadata.create_all(bind=engine)
auto_add_missing_columns()

client = TestClient(app)
PASSWORD = "Password@123"
FEATURE = "employee.roles_permissions"


# ------------------------------------------------------------------ helpers


def _org(plan: Plan | None = None) -> Organization:
    db = SessionLocal()
    try:
        org = Organization(name=f"Org {uuid.uuid4().hex[:8]}", email=f"org_{uuid.uuid4().hex[:8]}@example.com")
        org_service.start_trial(db, org)
        if plan is not None:
            org.plan_id = plan.id
        db.add(org)
        db.commit()
        db.refresh(org)
        return org
    finally:
        db.close()


def _plan(name: str | None = None, active: bool = True) -> Plan:
    db = SessionLocal()
    try:
        plan = Plan(name=name or f"Plan {uuid.uuid4().hex[:6]}", price_monthly=100, price_yearly=1000,
                    is_active=active, entitlements={}, features=[])
        db.add(plan)
        db.commit()
        db.refresh(plan)
        return plan
    finally:
        db.close()


def _user(kind: str, org: Organization | None = None) -> User:
    role = {"staff": None, "admin": UserRole.ADMIN, "super_admin": UserRole.SUPER_ADMIN, "sales_manager": None}[kind]
    db = SessionLocal()
    try:
        user = User(
            organization_id=org.id if org else None,
            name=f"{kind} {uuid.uuid4().hex[:6]}",
            email=f"{kind}_{uuid.uuid4().hex[:10]}@example.com",
            password_hash=hash_password(PASSWORD),
            role=role,
            system_role=kind,
            is_active=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user
    finally:
        db.close()


def _auth(user: User) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user.id, user.system_role, user.organization_id)}"}


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _org_row(org_id: str) -> Organization:
    db = SessionLocal()
    try:
        return db.get(Organization, org_id)
    finally:
        db.close()


def _has(org_id: str, key: str) -> bool:
    db = SessionLocal()
    try:
        return entitlement_service.has_entitlement(db, db.get(Organization, org_id), key)
    finally:
        db.close()


@pytest.fixture
def sa() -> User:
    return _user("super_admin")


@pytest.fixture
def sm(sa) -> dict:
    """A Sales Manager created through the API, signed in with a password."""
    email = f"sm_{uuid.uuid4().hex[:10]}@example.com"
    r = client.post("/platform/staff", json={"name": "Sales One", "email": email, "password": PASSWORD,
                                            "role": "sales_manager"}, headers=_auth(sa))
    assert r.status_code == 201, r.text
    login = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return {"id": r.json()["id"], "email": email, "headers": _bearer(login.json()["tokens"]["access_token"]),
            "refresh": login.json()["tokens"]["refresh_token"]}


# ------------------------------------------------------------------ staff management


def test_super_admin_creates_three_sales_managers_who_log_in_independently(sa):
    sessions = []
    for i in range(3):
        email = f"sm{i}_{uuid.uuid4().hex[:8]}@example.com"
        r = client.post("/platform/staff", json={"name": f"SM {i}", "email": email, "password": PASSWORD},
                        headers=_auth(sa))
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["role"] == "sales_manager" and body["is_active"] is True and body["last_login"] is None
        assert "password" not in str(body).lower()
        login = client.post("/auth/login", json={"email": email, "password": PASSWORD})
        assert login.status_code == 200
        sessions.append((body["id"], login.json()["tokens"]["access_token"]))

    for staff_id, token in sessions:
        me = client.get("/auth/me", headers=_bearer(token))
        assert me.status_code == 200
        assert me.json()["id"] == staff_id
        assert me.json()["user"]["system_role"] == "sales_manager"
        assert me.json()["organization_id"] is None and me.json()["full_access"] is False

    listed = client.get("/platform/staff", headers=_auth(sa)).json()
    ids = {s["id"] for s in listed}
    assert {s for s, _ in sessions} <= ids
    assert all(s["last_login"] for s in listed if s["id"] in ids and s["id"] in {x for x, _ in sessions})

    dup = client.post("/platform/staff", json={"name": "x", "email": listed[0]["email"], "password": PASSWORD},
                      headers=_auth(sa))
    assert dup.status_code == 409
    bad_role = client.post("/platform/staff", json={"name": "x", "email": "x_" + uuid.uuid4().hex[:6] + "@example.com",
                                                   "password": PASSWORD, "role": "super_admin"}, headers=_auth(sa))
    assert bad_role.status_code == 422


def test_staff_edit_and_scope(sa, sm):
    r = client.patch(f"/platform/staff/{sm['id']}", json={"name": "Renamed", "phone": "9876543210"}, headers=_auth(sa))
    assert r.status_code == 200 and r.json()["name"] == "Renamed"
    # A Super Admin or a tenant user is not "platform staff" here.
    assert client.get(f"/platform/staff/{sa.id}", headers=_auth(sa)).status_code == 404
    tenant = _user("admin", _org())
    assert client.patch(f"/platform/staff/{tenant.id}", json={"is_active": False}, headers=_auth(sa)).status_code == 404


def test_sales_manager_cannot_manage_staff_or_use_super_admin_routes(sm):
    h = sm["headers"]
    org = _org()
    assert client.get("/platform/staff", headers=h).status_code == 403
    assert client.post("/platform/staff", json={"name": "x", "email": "x@example.com", "password": PASSWORD},
                       headers=h).status_code == 403
    assert client.patch(f"/platform/staff/{sm['id']}", json={"is_active": True}, headers=h).status_code == 403
    # Destructive / direct-change routes stay Super Admin only.
    assert client.get("/superadmin/organizations", headers=h).status_code == 403
    assert client.delete(f"/superadmin/organizations/{org.id}", headers=h).status_code == 403
    assert client.patch(f"/superadmin/organizations/{org.id}/status", json={"status": "suspended"},
                        headers=h).status_code == 403
    assert client.post(f"/superadmin/organizations/{org.id}/overrides/features",
                       json={"entitlement_key": FEATURE, "effect": "ALLOW"}, headers=h).status_code == 403
    assert client.post("/superadmin/email/test", json={"to": "x@example.com"}, headers=h).status_code == 403
    # No tenant powers either.
    assert client.get("/customers", headers=h).status_code == 403
    assert _org_row(org.id).status is not None


@pytest.mark.parametrize("kind", ["admin", "staff"])
def test_tenant_users_cannot_reach_platform(kind):
    user = _user(kind, _org())
    for path in ("/platform/organizations", "/platform/summary", "/platform/upgrade-requests", "/platform/staff"):
        assert client.get(path, headers=_auth(user)).status_code == 403


def test_deactivated_sales_manager_is_locked_out_immediately(sa, sm):
    assert client.get("/platform/organizations", headers=sm["headers"]).status_code == 200
    r = client.patch(f"/platform/staff/{sm['id']}", json={"is_active": False}, headers=_auth(sa))
    assert r.status_code == 200 and r.json()["is_active"] is False
    assert client.get("/platform/organizations", headers=sm["headers"]).status_code == 401
    assert client.post("/auth/refresh", json={"refresh_token": sm["refresh"]}).status_code == 401
    assert client.post("/auth/login", json={"email": sm["email"], "password": PASSWORD}).status_code == 403

    assert client.patch(f"/platform/staff/{sm['id']}", json={"is_active": True}, headers=_auth(sa)).status_code == 200
    assert client.post("/auth/login", json={"email": sm["email"], "password": PASSWORD}).status_code == 200


# ------------------------------------------------------------------ read-only organization views


def test_sales_manager_reads_organizations(sm):
    h = sm["headers"]
    plan = _plan()
    org = _org(plan)
    listed = client.get("/platform/organizations", params={"search": org.name, "page_size": 5}, headers=h)
    assert listed.status_code == 200
    assert [o["id"] for o in listed.json()["items"]] == [org.id]
    detail = client.get(f"/platform/organizations/{org.id}", headers=h)
    assert detail.status_code == 200 and detail.json()["plan"]["id"] == plan.id
    ent = client.get(f"/platform/organizations/{org.id}/entitlements", headers=h)
    assert ent.status_code == 200 and FEATURE in ent.json()["features"]
    pays = client.get(f"/platform/organizations/{org.id}/subscription-payments", headers=h)
    assert pays.status_code == 200 and pays.json()["items"] == []
    plans = client.get("/platform/plans", headers=h).json()
    assert plan.id in {p["id"] for p in plans} and all(p["is_active"] for p in plans)
    features = client.get("/platform/features", headers=h).json()
    assert {"key", "name", "category", "category_name", "description"} <= set(features[0])
    assert FEATURE in {f["key"] for f in features}
    summary = client.get("/platform/summary", headers=h).json()
    assert summary["organizations"] >= 1
    assert client.get("/platform/organizations/does-not-exist", headers=h).status_code == 404


# ------------------------------------------------------------------ upgrade requests


def test_upgrade_request_flow(sa, sm):
    h = sm["headers"]
    current, target = _plan(), _plan()
    org = _org(current)

    r = client.post(f"/platform/organizations/{org.id}/upgrade-requests",
                    json={"requested_plan_id": target.id, "billing_cycle": "yearly",
                          "reason": "Customer wants more users", "notes": "Call on Monday"}, headers=h)
    assert r.status_code == 201, r.text
    req = r.json()
    assert req["status"] == "pending"
    assert req["current_plan"]["id"] == current.id and req["requested_plan"]["id"] == target.id
    assert req["organization"]["id"] == org.id and req["requested_by"]["id"] == sm["id"]
    assert _org_row(org.id).plan_id == current.id  # nothing changes yet

    again = client.post(f"/platform/organizations/{org.id}/upgrade-requests",
                        json={"requested_plan_id": target.id, "reason": "again"}, headers=h)
    assert again.status_code == 409

    # The Sales Manager cannot decide it.
    assert client.post(f"/platform/upgrade-requests/{req['id']}/approve", headers=h).status_code == 403
    assert client.post(f"/platform/upgrade-requests/{req['id']}/reject", headers=h).status_code == 403

    pending = client.get("/platform/upgrade-requests", params={"status": "pending", "organization_id": org.id},
                         headers=_auth(sa)).json()
    assert [x["id"] for x in pending["items"]] == [req["id"]]
    mine = client.get("/platform/upgrade-requests", params={"mine": "true"}, headers=h).json()
    assert req["id"] in {x["id"] for x in mine["items"]}

    ok = client.post(f"/platform/upgrade-requests/{req['id']}/approve", json={"note": "Approved"}, headers=_auth(sa))
    assert ok.status_code == 200
    assert ok.json()["status"] == "approved" and ok.json()["decided_by"]["id"] == sa.id
    row = _org_row(org.id)
    assert row.plan_id == target.id and row.billing_cycle == "yearly"
    assert client.get(f"/platform/organizations/{org.id}", headers=h).json()["plan"]["id"] == target.id

    for action in ("approve", "reject"):
        twice = client.post(f"/platform/upgrade-requests/{req['id']}/{action}", headers=_auth(sa))
        assert twice.status_code == 409 and twice.json()["detail"] == "Request already approved"


def test_upgrade_rejection_changes_nothing_and_validation(sa, sm):
    h = sm["headers"]
    current, target = _plan(), _plan()
    org = _org(current)
    assert client.post(f"/platform/organizations/{org.id}/upgrade-requests",
                       json={"requested_plan_id": current.id, "reason": "same"}, headers=h).status_code == 400
    assert client.post(f"/platform/organizations/{org.id}/upgrade-requests",
                       json={"requested_plan_id": _plan(active=False).id, "reason": "x"}, headers=h).status_code == 400
    assert client.post(f"/platform/organizations/{org.id}/upgrade-requests",
                       json={"requested_plan_id": target.id, "reason": ""}, headers=h).status_code == 422
    assert client.post("/platform/organizations/nope/upgrade-requests",
                       json={"requested_plan_id": target.id, "reason": "x"}, headers=h).status_code == 404

    req = client.post(f"/platform/organizations/{org.id}/upgrade-requests",
                      json={"requested_plan_id": target.id, "reason": "x"}, headers=h).json()
    r = client.post(f"/platform/upgrade-requests/{req['id']}/reject", json={"note": "Budget"}, headers=_auth(sa))
    assert r.status_code == 200 and r.json()["status"] == "rejected" and r.json()["decision_note"] == "Budget"
    assert _org_row(org.id).plan_id == current.id
    assert client.post(f"/platform/upgrade-requests/{req['id']}/approve", headers=_auth(sa)).status_code == 409
    # After a decision a new request may be raised.
    assert client.post(f"/platform/organizations/{org.id}/upgrade-requests",
                       json={"requested_plan_id": target.id, "reason": "retry"}, headers=h).status_code == 201


# ------------------------------------------------------------------ feature requests


def test_feature_unlock_and_restrict_change_effective_access(sa, sm):
    h = sm["headers"]
    org = _org(_plan())
    before = _has(org.id, FEATURE)
    action = "restrict" if before else "unlock"

    r = client.post(f"/platform/organizations/{org.id}/feature-requests",
                    json={"entitlement_key": FEATURE, "action": action, "reason": "Pilot"}, headers=h)
    assert r.status_code == 201, r.text
    req = r.json()
    assert req["status"] == "pending" and req["feature_name"] == "Roles & Permissions"
    assert _has(org.id, FEATURE) is before
    assert client.post(f"/platform/organizations/{org.id}/feature-requests",
                       json={"entitlement_key": FEATURE, "action": action, "reason": "dup"},
                       headers=h).status_code == 409
    assert client.post(f"/platform/feature-requests/{req['id']}/approve", headers=h).status_code == 403

    ok = client.post(f"/platform/feature-requests/{req['id']}/approve", headers=_auth(sa))
    assert ok.status_code == 200 and ok.json()["status"] == "approved"
    assert _has(org.id, FEATURE) is (not before)
    ent = client.get(f"/platform/organizations/{org.id}/entitlements", headers=h).json()
    assert ent["features"][FEATURE] is (not before)

    back = "unlock" if action == "restrict" else "restrict"
    req2 = client.post(f"/platform/organizations/{org.id}/feature-requests",
                       json={"entitlement_key": FEATURE, "action": back, "reason": "Pilot over"}, headers=h).json()
    assert client.post(f"/platform/feature-requests/{req2['id']}/approve", headers=_auth(sa)).status_code == 200
    assert _has(org.id, FEATURE) is before
    assert client.post(f"/platform/feature-requests/{req2['id']}/reject", headers=_auth(sa)).status_code == 409


def test_feature_rejection_changes_nothing(sa, sm):
    h = sm["headers"]
    org = _org(_plan())
    before = _has(org.id, FEATURE)
    req = client.post(f"/platform/organizations/{org.id}/feature-requests",
                      json={"entitlement_key": FEATURE, "action": "unlock" if not before else "restrict",
                            "reason": "x"}, headers=h).json()
    r = client.post(f"/platform/feature-requests/{req['id']}/reject", json={"note": "No"}, headers=_auth(sa))
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert _has(org.id, FEATURE) is before
    db = SessionLocal()
    try:
        assert db.query(OrganizationFeatureOverride).filter_by(organization_id=org.id).count() == 0
    finally:
        db.close()
    listed = client.get("/platform/feature-requests", params={"organization_id": org.id}, headers=h).json()
    assert [x["status"] for x in listed["items"]] == ["rejected"]


def test_feature_request_validation(sm):
    h = sm["headers"]
    org = _org()
    assert client.post(f"/platform/organizations/{org.id}/feature-requests",
                       json={"entitlement_key": "not.a.feature", "action": "unlock", "reason": "x"},
                       headers=h).status_code == 422
    assert client.post(f"/platform/organizations/{org.id}/feature-requests",
                       json={"entitlement_key": FEATURE, "action": "delete", "reason": "x"},
                       headers=h).status_code == 422
    assert client.get("/platform/feature-requests", params={"status": "weird"}, headers=h).status_code == 422
    assert client.get("/platform/feature-requests/nope", headers=h).status_code == 404


# ------------------------------------------------------------------ staff deletion


def _new_sm(sa) -> dict:
    email = f"sm_{uuid.uuid4().hex[:10]}@example.com"
    r = client.post("/platform/staff", json={"name": "Sales X", "email": email, "password": PASSWORD},
                    headers=_auth(sa))
    assert r.status_code == 201, r.text
    return {"id": r.json()["id"], "email": email}


def _exists(user_id: str) -> bool:
    db = SessionLocal()
    try:
        return db.get(User, user_id) is not None
    finally:
        db.close()


def test_super_admin_deletes_a_sales_manager_and_their_sessions_end(sa, sm):
    org = _org(_plan())
    raised = client.post(f"/platform/organizations/{org.id}/upgrade-requests",
                         json={"requested_plan_id": _plan().id, "reason": "growth"}, headers=sm["headers"])
    assert raised.status_code == 201

    assert client.delete(f"/platform/staff/{sm['id']}", headers=_auth(sa)).status_code == 204
    assert not _exists(sm["id"])
    assert client.get("/platform/summary", headers=sm["headers"]).status_code == 401
    assert client.post("/auth/refresh", json={"refresh_token": sm["refresh"]}).status_code == 401
    assert client.post("/auth/login", json={"email": sm["email"], "password": PASSWORD}).status_code == 401
    # The request they raised stays, unattributed.
    kept = client.get(f"/platform/upgrade-requests/{raised.json()['id']}", headers=_auth(sa))
    assert kept.status_code == 200 and kept.json()["requested_by"] is None
    assert client.delete(f"/platform/staff/{sm['id']}", headers=_auth(sa)).status_code == 404


def test_bulk_delete_is_all_or_nothing_and_counts_duplicates_once(sa):
    a, b, c = _new_sm(sa), _new_sm(sa), _new_sm(sa)
    tenant_admin = _user("admin", _org())
    other_sa = _user("super_admin")

    r = client.post("/platform/staff/bulk-delete", json={"ids": [a["id"], b["id"], a["id"]]}, headers=_auth(sa))
    assert r.status_code == 200 and r.json() == {"deleted": 2}
    assert not _exists(a["id"]) and not _exists(b["id"])

    for bad in (other_sa.id, sa.id, tenant_admin.id, "not-a-uuid", a["id"]):
        r = client.post("/platform/staff/bulk-delete", json={"ids": [c["id"], bad]}, headers=_auth(sa))
        assert r.status_code == 404, bad
        assert r.json()["detail"]["code"] == "STAFF_NOT_FOUND" and r.json()["detail"]["ids"] == [bad]
        assert _exists(c["id"])
    assert _exists(other_sa.id) and _exists(sa.id) and _exists(tenant_admin.id)

    assert client.post("/platform/staff/bulk-delete", json={"ids": []}, headers=_auth(sa)).status_code == 422
    r = client.request("DELETE", "/platform/staff/bulk-delete", json={"ids": [c["id"]]}, headers=_auth(sa))
    assert r.status_code == 200 and r.json() == {"deleted": 1}


def test_only_a_super_admin_deletes_and_never_protected_accounts(sa, sm):
    victim = _new_sm(sa)
    tenant_admin = _user("admin", _org())
    for headers in (sm["headers"], _auth(tenant_admin)):
        assert client.delete(f"/platform/staff/{victim['id']}", headers=headers).status_code == 403
        assert client.post("/platform/staff/bulk-delete", json={"ids": [victim["id"]]},
                           headers=headers).status_code == 403
    assert client.delete(f"/platform/staff/{victim['id']}").status_code in (401, 403)
    assert _exists(victim["id"])

    other_sa = _user("super_admin")
    for target in (sa.id, other_sa.id, tenant_admin.id, _user("staff", _org()).id):
        assert client.delete(f"/platform/staff/{target}", headers=_auth(sa)).status_code == 404
        assert _exists(target)


# ------------------------------------------------------------------ self-service profile


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def _photo(headers: dict, content: bytes, content_type: str, name: str = "me.png"):  # noqa: ANN202
    return client.post("/auth/me/photo", files={"file": (name, content, content_type)}, headers=headers)


def _file_id(url: str) -> str:
    return url.rsplit("/", 1)[-1]


@pytest.mark.parametrize("kind", ["staff", "sales_manager", "admin"])
def test_users_edit_their_own_profile(kind, sm):
    headers = sm["headers"] if kind == "sales_manager" else _auth(_user(kind, _org()))
    r = client.patch("/auth/me", json={"name": "  New Name ", "city": "Pune", "personal_email": "",
                                       "date_of_birth": "1990-05-01"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "New Name" and body["city"] == "Pune" and body["personal_email"] is None
    assert "password_hash" not in body and "password" not in body
    assert client.get("/auth/me", headers=headers).json()["user"]["city"] == "Pune"

    r = client.patch("/auth/me", json={"first_name": "Asha", "last_name": "Rao"}, headers=headers)
    assert r.json()["name"] == "Asha Rao"
    assert client.patch("/auth/me", json={"name": ""}, headers=headers).status_code == 422
    assert client.patch("/auth/me", json={"name": None}, headers=headers).status_code == 422


def test_self_service_cannot_touch_privileged_fields(sm):
    staff = _user("staff", _org())
    other_org = _org()
    before = client.get("/auth/me", headers=_auth(staff)).json()["user"]
    for field, value in [
        ("system_role", "super_admin"), ("role", "admin"), ("role_id", "x"), ("organization_id", other_org.id),
        ("is_active", False), ("status", "inactive"), ("email", "new@example.com"), ("phone", "9876543210"),
        ("password", "Another@123"), ("password_hash", "x"), ("profile_photo", "/files/abc"), ("id", "x"),
        ("designation", "CEO"), ("basic_salary", 1), ("phone_verified", True), ("google_id", "g"),
        ("employee_id", "EMP-1"), ("username", "boss"),
    ]:
        r = client.patch("/auth/me", json={"name": "Hacker", field: value}, headers=_auth(staff))
        assert r.status_code == 422, field
    after = client.get("/auth/me", headers=_auth(staff)).json()["user"]
    assert after == before
    assert client.patch("/auth/me", json={"name": "x"}).status_code in (401, 403)
    # A Sales Manager editing themselves stays a Sales Manager with no organization.
    me = client.patch("/auth/me", json={"name": "Still Sales"}, headers=sm["headers"]).json()
    assert me["system_role"] == "sales_manager" and me["organization_id"] is None


def test_change_password_requires_the_current_one_and_ends_sessions(sm):
    h = sm["headers"]
    wrong = client.post("/auth/change-password", json={"current_password": "nope", "new_password": "Brand@New1"},
                        headers=h)
    assert wrong.status_code == 400
    assert client.post("/auth/change-password", json={"current_password": PASSWORD, "new_password": "short"},
                       headers=h).status_code == 422
    ok = client.post("/auth/change-password", json={"current_password": PASSWORD, "new_password": "Brand@New1"},
                     headers=h)
    assert ok.status_code == 200
    assert client.post("/auth/refresh", json={"refresh_token": sm["refresh"]}).status_code == 401
    assert client.post("/auth/login", json={"email": sm["email"], "password": PASSWORD}).status_code == 401
    assert client.post("/auth/login", json={"email": sm["email"], "password": "Brand@New1"}).status_code == 200


@pytest.mark.parametrize("kind", ["staff", "sales_manager"])
def test_profile_photo_upload_replace_and_remove(kind, sm):
    headers = sm["headers"] if kind == "sales_manager" else _auth(_user(kind, _org()))
    first = _photo(headers, PNG, "image/png", name='evil"name.png')
    assert first.status_code == 200, first.text
    url1 = first.json()["profile_photo"]
    served = client.get(f"/files/{_file_id(url1)}", follow_redirects=False)
    assert served.status_code == 200 and served.content == PNG
    assert 'filename="profile-photo.png"' in served.headers["content-disposition"]

    second = _photo(headers, JPEG, "image/jpeg", name="me.jpg")
    assert second.status_code == 200
    url2 = second.json()["profile_photo"]
    assert url2 != url1
    assert client.get(f"/files/{_file_id(url1)}").status_code == 404  # replaced and unreferenced: removed

    removed = client.delete("/auth/me/photo", headers=headers)
    assert removed.status_code == 200 and removed.json()["profile_photo"] is None
    assert client.get(f"/files/{_file_id(url2)}").status_code == 404


def test_a_shared_photo_is_never_deleted_on_replace():
    org = _org()
    me, colleague = _user("staff", org), _user("staff", org)
    url = _photo(_auth(me), PNG, "image/png").json()["profile_photo"]
    db = SessionLocal()
    try:
        db.get(User, colleague.id).profile_photo = url
        db.commit()
    finally:
        db.close()
    assert _photo(_auth(me), JPEG, "image/jpeg").status_code == 200
    assert client.get(f"/files/{_file_id(url)}").status_code == 200


def test_invalid_and_oversized_photos_are_rejected():
    headers = _auth(_user("staff", _org()))
    cases = [
        (b"just text, not an image", "image/png", 400),
        (PNG, "image/jpeg", 400),  # declared type does not match the bytes
        (b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", "image/svg+xml", 400),
        (b"GIF89a" + b"\x00" * 16, "image/gif", 400),
        (b"%PDF-1.4", "application/pdf", 400),
        (PNG + b"\x00" * (5 * 1024 * 1024), "image/png", 413),
    ]
    for content, content_type, expected in cases:
        assert _photo(headers, content, content_type).status_code == expected, content_type
    assert client.get("/auth/me", headers=headers).json()["user"]["profile_photo"] is None
    assert client.post("/auth/me/photo", files={"file": ("a.png", PNG, "image/png")}).status_code in (401, 403)
