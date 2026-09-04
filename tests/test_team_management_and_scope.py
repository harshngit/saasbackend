"""Team Management + Team Data Scope:

  A. Team CRUD
  B. Manager rules
  C. Member rules
  D. Team Data Scope across the six confirmed CRM modules
  E. Dynamic membership (move teams, visibility follows immediately)
  F. User-without-team fallback
  G. Security (cross-org, IDOR)
"""

import os
import sys
import uuid

os.environ["DATABASE_URL"] = "sqlite:///./crm_saas.db"
os.environ["TESTING"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

PASSED = 0
FAILED = 0


def log_test(name: str, condition: bool, extra: str = ""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name} {extra}".rstrip())


def _register_org(label: str) -> dict:
    email = f"admin_{uuid.uuid4().hex[:8]}@{label.replace('_', '').lower()}.com"
    r = client.post(
        "/auth/register",
        json={
            "organization_name": f"{label} Org", "admin_name": f"Admin {label}",
            "email": email, "password": "Password123!", "role": "admin",
        },
    )
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}


def _create_role(admin_auth: dict, data_scope: str, permissions: dict, name: str | None = None) -> dict:
    r = client.post(
        "/roles",
        json={"name": name or f"Role{uuid.uuid4().hex[:6]}", "workspace": "sales", "data_scope": data_scope, "permissions": permissions},
        headers=admin_auth,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _create_staff(admin_auth: dict, name: str, role_name: str) -> tuple[dict, dict]:
    email = f"{name.lower().replace(' ', '_')}_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post(
        "/users", json={"name": name, "email": email, "password": "Password123!", "role": role_name}, headers=admin_auth
    )
    assert r.status_code == 201, r.text
    user = r.json()
    login = client.post("/auth/login", json={"email": email, "password": "Password123!"})
    assert login.status_code == 200, login.text
    return user, {"Authorization": f"Bearer {login.json()['tokens']['access_token']}"}


_CRM_PERMS = {
    "leads": {"view": True, "create": True, "edit": True},
    "customers": {"view": True, "create": True, "edit": True},
    "visits": {"view": True, "create": True, "edit": True},
    "follow_ups": {"view": True, "create": True, "edit": True},
    "quotations": {"view": True, "create": True, "edit": True},
    "sales_orders": {"view": True, "create": True, "edit": True},
}


def _team_scope_setup(label: str):
    """Org, a team-scope role, Team A (manager + teammate), an outsider on
    Team B, and an admin. Everyone can use the six CRM modules."""
    admin_auth = _register_org(label)
    role = _create_role(admin_auth, "team", _CRM_PERMS)
    manager, manager_auth = _create_staff(admin_auth, "Manager", role["name"])
    teammate, teammate_auth = _create_staff(admin_auth, "Teammate", role["name"])
    outsider, outsider_auth = _create_staff(admin_auth, "Outsider", role["name"])

    team_a = client.post(
        "/teams", json={"name": "Team A", "manager_id": manager["id"], "member_ids": [teammate["id"]]}, headers=admin_auth
    ).json()
    team_b = client.post(
        "/teams", json={"name": "Team B", "manager_id": outsider["id"], "member_ids": []}, headers=admin_auth
    ).json()
    return {
        "admin_auth": admin_auth, "role": role,
        "manager": manager, "manager_auth": manager_auth,
        "teammate": teammate, "teammate_auth": teammate_auth,
        "outsider": outsider, "outsider_auth": outsider_auth,
        "team_a": team_a, "team_b": team_b,
    }


# ============================================================================
# A. Team CRUD
# ============================================================================

def run_group_a():
    print("\n=== A. Team CRUD ===")
    admin_auth = _register_org(f"TeamCRUD_{uuid.uuid4().hex[:6]}")
    mgr, mgr_auth = _create_staff(admin_auth, "Mgr", "Sales Officer")
    staff, staff_auth = _create_staff(admin_auth, "Staff", "Sales Officer")

    r = client.post("/teams", json={"name": "Sales Team A", "manager_id": mgr["id"], "member_ids": []}, headers=admin_auth)
    log_test("A: Org Admin can create a Team (201)", r.status_code == 201, r.text)
    team = r.json()

    r2 = client.post("/teams", json={"name": "Nope", "manager_id": mgr["id"]}, headers=staff_auth)
    log_test("A: Non-admin cannot create a Team (403)", r2.status_code == 403, r2.text)

    other_admin_auth = _register_org(f"TeamCRUDOther_{uuid.uuid4().hex[:6]}")
    r3 = client.get("/teams", headers=other_admin_auth)
    log_test(
        "A: Team list is organization-scoped (other org sees none of these)",
        r3.status_code == 200 and not any(t["id"] == team["id"] for t in r3.json()),
        r3.text,
    )
    r4 = client.get(f"/teams/{team['id']}", headers=other_admin_auth)
    log_test("A: Team detail cannot be accessed cross-org (404)", r4.status_code == 404, r4.text)

    r5 = client.patch(f"/teams/{team['id']}", json={"name": "Sales Team A Renamed"}, headers=admin_auth)
    log_test("A: Admin can update Team name (200)", r5.status_code == 200 and r5.json()["name"] == "Sales Team A Renamed", r5.text)

    dup = client.post("/teams", json={"name": "Sales Team A Renamed", "manager_id": mgr["id"]}, headers=admin_auth)
    log_test("A: Duplicate Team name in same org rejected (409)", dup.status_code == 409, dup.text)

    other_org_dup = client.post(
        "/teams", json={"name": "Sales Team A Renamed", "manager_id": (_create_staff(other_admin_auth, "M2", "Sales Officer")[0])["id"]},
        headers=other_admin_auth,
    )
    log_test("A: Same Team name in a different org is allowed (201)", other_org_dup.status_code == 201, other_org_dup.text)

    r6 = client.delete(f"/teams/{team['id']}", headers=admin_auth)
    log_test("A: Admin can delete a Team (204)", r6.status_code == 204, r6.text)

    r7 = client.get(f"/users/{staff['id']}", headers=admin_auth)
    log_test("A: Deleting Team preserves Users (staff still fetchable)", r7.status_code == 200, r7.text)
    r8 = client.get(f"/users/{mgr['id']}", headers=admin_auth)
    log_test(
        "A: Former manager's team_id cleared after Team delete",
        r8.status_code == 200 and r8.json()["employment_information"]["team_id"] is None,
        r8.text,
    )


# ============================================================================
# B. Manager rules
# ============================================================================

def run_group_b():
    print("\n=== B. Manager rules ===")
    admin_auth = _register_org(f"TeamMgr_{uuid.uuid4().hex[:6]}")
    mgr, _ = _create_staff(admin_auth, "Mgr", "Sales Officer")
    mem, _ = _create_staff(admin_auth, "Mem", "Sales Officer")
    new_mgr, _ = _create_staff(admin_auth, "NewMgr", "Sales Officer")

    team = client.post(
        "/teams", json={"name": "MgrTeam", "manager_id": mgr["id"], "member_ids": [mem["id"]]}, headers=admin_auth
    ).json()
    log_test("B: Team has exactly one manager", team["manager_id"] == mgr["id"])
    log_test(
        "B: Manager becomes a Team member",
        any(m["id"] == mgr["id"] for m in team["members"]), team,
    )

    other_admin_auth = _register_org(f"TeamMgrOther_{uuid.uuid4().hex[:6]}")
    cross_mgr, _ = _create_staff(other_admin_auth, "CrossMgr", "Sales Officer")
    r = client.post("/teams", json={"name": "CrossMgrTeam", "manager_id": cross_mgr["id"]}, headers=admin_auth)
    log_test("B: Cross-org manager_id rejected (400)", r.status_code == 400, r.text)

    r2 = client.patch(f"/teams/{team['id']}", json={"manager_id": new_mgr["id"]}, headers=admin_auth)
    log_test("B: Manager change works (200)", r2.status_code == 200, r2.text)
    updated = r2.json()
    log_test("B: New manager is team.manager_id", updated["manager_id"] == new_mgr["id"])
    log_test("B: New manager becomes a member", any(m["id"] == new_mgr["id"] for m in updated["members"]), updated)
    log_test("B: Previous manager remains a member (not auto-removed)", any(m["id"] == mgr["id"] for m in updated["members"]), updated)

    r3 = client.patch(f"/teams/{team['id']}", json={"manager_id": None}, headers=admin_auth)
    log_test("B: Manager cannot be removed without a valid replacement (400)", r3.status_code == 400, r3.text)


# ============================================================================
# C. Member rules
# ============================================================================

def run_group_c():
    print("\n=== C. Member rules ===")
    admin_auth = _register_org(f"TeamMem_{uuid.uuid4().hex[:6]}")
    mgr_a, _ = _create_staff(admin_auth, "MgrA", "Sales Officer")
    mgr_b, _ = _create_staff(admin_auth, "MgrB", "Sales Officer")
    user_x, _ = _create_staff(admin_auth, "UserX", "Sales Officer")

    team_a = client.post(
        "/teams", json={"name": "TeamAlpha", "manager_id": mgr_a["id"], "member_ids": [user_x["id"]]}, headers=admin_auth
    ).json()
    team_b = client.post("/teams", json={"name": "TeamBeta", "manager_id": mgr_b["id"], "member_ids": []}, headers=admin_auth).json()

    r_check = client.get(f"/users/{user_x['id']}", headers=admin_auth)
    log_test("C: User belongs to Team A", r_check.json()["employment_information"]["team_id"] == team_a["id"])

    r_move = client.patch(f"/teams/{team_b['id']}", json={"member_ids": [user_x["id"]]}, headers=admin_auth)
    log_test("C: User can move Team A -> Team B (200)", r_move.status_code == 200, r_move.text)
    r_check2 = client.get(f"/users/{user_x['id']}", headers=admin_auth)
    log_test("C: User's team_id now Team B", r_check2.json()["employment_information"]["team_id"] == team_b["id"])
    r_team_a_after = client.get(f"/teams/{team_a['id']}", headers=admin_auth)
    log_test(
        "C: Team A no longer lists the moved user",
        not any(m["id"] == user_x["id"] for m in r_team_a_after.json()["members"]),
        r_team_a_after.text,
    )

    other_admin_auth = _register_org(f"TeamMemOther_{uuid.uuid4().hex[:6]}")
    cross_user, _ = _create_staff(other_admin_auth, "CrossUser", "Sales Officer")
    r_cross = client.patch(f"/teams/{team_a['id']}", json={"member_ids": [cross_user["id"]]}, headers=admin_auth)
    log_test("C: Cross-org member assignment rejected (400)", r_cross.status_code == 400, r_cross.text)

    r_remove = client.patch(f"/teams/{team_b['id']}", json={"member_ids": []}, headers=admin_auth)
    log_test("C: Removing a member clears team_id (200)", r_remove.status_code == 200, r_remove.text)
    r_check3 = client.get(f"/users/{user_x['id']}", headers=admin_auth)
    log_test("C: Removed member's team_id is now None", r_check3.json()["employment_information"]["team_id"] is None, r_check3.text)
    r_team_b_after = client.get(f"/teams/{team_b['id']}", headers=admin_auth)
    log_test(
        "C: Team Manager remains a valid Team member even after an empty member_ids replace",
        any(m["id"] == mgr_b["id"] for m in r_team_b_after.json()["members"]),
        r_team_b_after.text,
    )


# ============================================================================
# D. Team Data Scope across the six CRM modules
# ============================================================================

def run_group_d():
    print("\n=== D. Team Data Scope -- Leads, Customers, Visits, Follow-ups, Quotations, Orders ===")
    ctx = _team_scope_setup(f"TeamScopeD_{uuid.uuid4().hex[:6]}")
    admin_auth = ctx["admin_auth"]

    # --- Leads ---
    lead_mgr = client.post(
        "/leads", json={"name": "L-mgr", "mobile_number": f"9{uuid.uuid4().hex[:9]}", "lead_source": "Referral",
                        "assigned_salesperson_id": ctx["manager"]["id"]},
        headers=admin_auth,
    ).json()
    lead_out = client.post(
        "/leads", json={"name": "L-out", "mobile_number": f"9{uuid.uuid4().hex[:9]}", "lead_source": "Referral",
                        "assigned_salesperson_id": ctx["outsider"]["id"]},
        headers=admin_auth,
    ).json()
    r = client.get("/leads", headers=ctx["teammate_auth"])
    ids = [x["id"] for x in r.json()]
    log_test("D-Leads: Team A teammate sees manager's Lead via team scope", lead_mgr["id"] in ids)
    log_test("D-Leads: Team A teammate does NOT see Team B's Lead", lead_out["id"] not in ids)
    r_detail = client.get(f"/leads/{lead_out['id']}", headers=ctx["teammate_auth"])
    log_test("D-Leads: object-level -- teammate cannot fetch outsider's Lead (404)", r_detail.status_code == 404, r_detail.text)

    # --- Customers ---
    cust_mgr = client.post(
        "/customers", json={"name": "C-mgr", "assigned_sales_officer_id": ctx["manager"]["id"]}, headers=admin_auth
    ).json()
    cust_out = client.post(
        "/customers", json={"name": "C-out", "assigned_sales_officer_id": ctx["outsider"]["id"]}, headers=admin_auth
    ).json()
    r = client.get("/customers", headers=ctx["teammate_auth"])
    ids = [x["id"] for x in r.json()]
    log_test("D-Customers: teammate sees manager's Customer via team scope", cust_mgr["id"] in ids)
    log_test("D-Customers: teammate does NOT see outsider's Customer", cust_out["id"] not in ids)

    # --- Visits ---
    visit_mgr = client.post("/visits", json={"customer_id": cust_mgr["id"], "user_id": ctx["manager"]["id"]}, headers=admin_auth).json()
    visit_out = client.post("/visits", json={"customer_id": cust_out["id"], "user_id": ctx["outsider"]["id"]}, headers=admin_auth).json()
    r = client.get("/visits", headers=ctx["teammate_auth"])
    ids = [x["id"] for x in r.json()]
    log_test("D-Visits: teammate sees manager's Visit via team scope", visit_mgr["id"] in ids)
    log_test("D-Visits: teammate does NOT see outsider's Visit", visit_out["id"] not in ids)

    # --- Follow-ups ---
    fu_mgr = client.post(
        "/follow-ups", json={"customer_id": cust_mgr["id"], "title": "call", "due_date": "2026-09-10T00:00:00Z",
                             "assigned_to_id": ctx["manager"]["id"]},
        headers=admin_auth,
    ).json()
    fu_out = client.post(
        "/follow-ups", json={"customer_id": cust_out["id"], "title": "call", "due_date": "2026-09-10T00:00:00Z",
                             "assigned_to_id": ctx["outsider"]["id"]},
        headers=admin_auth,
    ).json()
    r = client.get("/follow-ups", headers=ctx["teammate_auth"])
    ids = [x["id"] for x in r.json()]
    log_test("D-FollowUps: teammate sees manager's Follow-up via team scope", fu_mgr["id"] in ids)
    log_test("D-FollowUps: teammate does NOT see outsider's Follow-up", fu_out["id"] not in ids)

    # --- Quotations ---
    quo_prod = client.post(
        "/products", json={"name": f"QP{uuid.uuid4().hex[:4]}", "sku": f"QSKU-{uuid.uuid4().hex[:6]}", "price": 20.0},
        headers=admin_auth,
    ).json()
    quo_mgr = client.post(
        "/quotations",
        json={
            "customer_id": cust_mgr["id"], "salesperson_id": ctx["manager"]["id"],
            "items": [{"product_id": quo_prod["id"], "quantity": 1, "unit_price": 20.0}],
        },
        headers=admin_auth,
    ).json()
    quo_out = client.post(
        "/quotations",
        json={
            "customer_id": cust_out["id"], "salesperson_id": ctx["outsider"]["id"],
            "items": [{"product_id": quo_prod["id"], "quantity": 1, "unit_price": 20.0}],
        },
        headers=admin_auth,
    ).json()
    r = client.get("/quotations", headers=ctx["teammate_auth"])
    ids = [x["id"] for x in r.json()]
    log_test("D-Quotations: teammate sees manager's Quotation via team scope", quo_mgr["id"] in ids, str(ids)[:200])
    log_test("D-Quotations: teammate does NOT see outsider's Quotation", quo_out["id"] not in ids)

    # --- Orders ---
    wh = client.post("/warehouses", json={"name": "WH", "is_default": True}, headers=admin_auth).json()
    prod = client.post(
        "/products", json={"name": f"P{uuid.uuid4().hex[:4]}", "sku": f"SKU-{uuid.uuid4().hex[:6]}", "price": 10.0, "total_inventory": 50},
        headers=admin_auth,
    ).json()
    order_mgr = client.post(
        "/orders", json={"customer_id": cust_mgr["id"], "warehouse_id": wh["id"], "salesperson_id": ctx["manager"]["id"],
                         "items": [{"product_id": prod["id"], "quantity": 1, "unit_price": 10.0}]},
        headers=admin_auth,
    ).json()
    order_out = client.post(
        "/orders", json={"customer_id": cust_out["id"], "warehouse_id": wh["id"], "salesperson_id": ctx["outsider"]["id"],
                         "items": [{"product_id": prod["id"], "quantity": 1, "unit_price": 10.0}]},
        headers=admin_auth,
    ).json()
    r = client.get("/orders", headers=ctx["teammate_auth"])
    ids = [x["id"] for x in r.json()]
    log_test("D-Orders: teammate sees manager's Order via team scope (salesperson_id)", order_mgr["id"] in ids)
    log_test("D-Orders: teammate does NOT see outsider's Order", order_out["id"] not in ids)

    # Scenario 1 -- own scope regression (unaffected by Team Scope existing)
    own_role = _create_role(ctx["admin_auth"], "own", _CRM_PERMS)
    own_user, own_auth = _create_staff(ctx["admin_auth"], "OwnUser", own_role["name"])
    own_lead = client.post(
        "/leads", json={"name": "L-own", "mobile_number": f"9{uuid.uuid4().hex[:9]}", "lead_source": "Referral",
                        "assigned_salesperson_id": own_user["id"]},
        headers=ctx["admin_auth"],
    ).json()
    r_own = client.get("/leads", headers=own_auth)
    own_ids = [x["id"] for x in r_own.json()]
    log_test("D-Own regression: own-scope user sees only their own Lead", own_ids == [own_lead["id"]], own_ids)

    # Scenario 4 -- all scope regression
    r_all = client.get("/leads", headers=ctx["admin_auth"])
    all_ids = [x["id"] for x in r_all.json()]
    log_test(
        "D-All regression: all-scope Admin sees every Lead in the org",
        all(lid in all_ids for lid in (lead_mgr["id"], lead_out["id"], own_lead["id"])),
        str(all_ids)[:300],
    )


# ============================================================================
# E. Dynamic membership
# ============================================================================

def run_group_e():
    print("\n=== E. Dynamic membership ===")
    ctx = _team_scope_setup(f"TeamDyn_{uuid.uuid4().hex[:6]}")
    admin_auth = ctx["admin_auth"]

    lead = client.post(
        "/leads", json={"name": "DynLead", "mobile_number": f"9{uuid.uuid4().hex[:9]}", "lead_source": "Referral",
                        "assigned_salesperson_id": ctx["manager"]["id"]},
        headers=admin_auth,
    ).json()

    r1 = client.get("/leads", headers=ctx["teammate_auth"])
    log_test("E: Team A initially sees the manager's Lead", any(l["id"] == lead["id"] for l in r1.json()))
    r1b = client.get("/leads", headers=ctx["outsider_auth"])
    log_test("E: Team B initially does NOT see it", not any(l["id"] == lead["id"] for l in r1b.json()))

    mv = client.patch(f"/teams/{ctx['team_b']['id']}", json={"member_ids": [ctx["manager"]["id"]]}, headers=admin_auth)
    log_test("E: Move manager Team A -> Team B succeeds (200)", mv.status_code == 200, mv.text)

    r2 = client.get("/leads", headers=ctx["teammate_auth"])
    log_test("E: Team A LOSES visibility after the move (no record edited)", not any(l["id"] == lead["id"] for l in r2.json()))
    r2b = client.get("/leads", headers=ctx["outsider_auth"])
    log_test("E: Team B GAINS visibility after the move", any(l["id"] == lead["id"] for l in r2b.json()))

    r3 = client.get(f"/leads/{lead['id']}", headers=admin_auth)
    log_test("E: Lead's assigned_salesperson_id itself is unchanged by the move", r3.json()["assigned_salesperson_id"] == ctx["manager"]["id"], r3.text)


# ============================================================================
# F. User without Team
# ============================================================================

def run_group_f():
    print("\n=== F. User without Team ===")
    admin_auth = _register_org(f"TeamNoTeam_{uuid.uuid4().hex[:6]}")
    role = _create_role(admin_auth, "team", _CRM_PERMS)
    lone_user, lone_auth = _create_staff(admin_auth, "Lone", role["name"])
    other_user, _ = _create_staff(admin_auth, "Other", role["name"])

    own_lead = client.post(
        "/leads", json={"name": "OwnLead", "mobile_number": f"9{uuid.uuid4().hex[:9]}", "lead_source": "Referral",
                        "assigned_salesperson_id": lone_user["id"]},
        headers=admin_auth,
    ).json()
    other_lead = client.post(
        "/leads", json={"name": "OtherLead", "mobile_number": f"9{uuid.uuid4().hex[:9]}", "lead_source": "Referral",
                        "assigned_salesperson_id": other_user["id"]},
        headers=admin_auth,
    ).json()

    r = client.get("/leads", headers=lone_auth)
    log_test("F: team-scope user with no Team, no crash (200)", r.status_code == 200, r.text)
    ids = [x["id"] for x in r.json()]
    log_test("F: sees own Lead", own_lead["id"] in ids)
    log_test("F: does NOT see another user's Lead (no org-wide fallback, no leak)", other_lead["id"] not in ids)


# ============================================================================
# G. Security
# ============================================================================

def run_group_g():
    print("\n=== G. Security ===")
    org_a_admin = _register_org(f"TeamSecA_{uuid.uuid4().hex[:6]}")
    org_b_admin = _register_org(f"TeamSecB_{uuid.uuid4().hex[:6]}")
    mgr_a, _ = _create_staff(org_a_admin, "MgrA", "Sales Officer")
    team_a = client.post("/teams", json={"name": "SecTeamA", "manager_id": mgr_a["id"]}, headers=org_a_admin).json()

    # Org B admin cannot manage Org A's team
    r = client.get(f"/teams/{team_a['id']}", headers=org_b_admin)
    log_test("G: Org B Admin cannot view Org A's Team (404)", r.status_code == 404, r.text)
    r2 = client.patch(f"/teams/{team_a['id']}", json={"name": "hijacked"}, headers=org_b_admin)
    log_test("G: Org B Admin cannot update Org A's Team (404)", r2.status_code == 404, r2.text)
    r3 = client.delete(f"/teams/{team_a['id']}", headers=org_b_admin)
    log_test("G: Org B Admin cannot delete Org A's Team (404)", r3.status_code == 404, r3.text)

    # IDOR: team-scope user cannot manually access another team's record by id
    ctx = _team_scope_setup(f"TeamSecIDOR_{uuid.uuid4().hex[:6]}")
    cust_out = client.post(
        "/customers", json={"name": "IDOR-cust", "assigned_sales_officer_id": ctx["outsider"]["id"]}, headers=ctx["admin_auth"]
    ).json()
    r4 = client.get(f"/customers/{cust_out['id']}", headers=ctx["teammate_auth"])
    log_test("G: IDOR -- Team A user cannot fetch Team B's Customer by known UUID (404)", r4.status_code == 404, r4.text)
    r5 = client.patch(f"/customers/{cust_out['id']}", json={"name": "hacked"}, headers=ctx["teammate_auth"])
    log_test("G: IDOR -- Team A user cannot edit Team B's Customer by known UUID (404)", r5.status_code == 404, r5.text)


def run_all_tests():
    global PASSED, FAILED
    PASSED = 0
    FAILED = 0
    print("\n=======================================================")
    print("TEST SUITE: Team Management + Team Data Scope")
    print("=======================================================")
    run_group_a()
    run_group_b()
    run_group_c()
    run_group_d()
    run_group_e()
    run_group_f()
    run_group_g()
    print("\n=======================================================")
    print(f"RESULTS: {PASSED} passed, {FAILED} failed")
    print("=======================================================\n")
    if FAILED > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_all_tests()
