"""Authorization security gap fixes:

  A. /auth/refresh custom-role crash (user.role is None)
  B. Vehicle Stock / loading-session IDOR
  C. Delivery Partner access to unassigned/other-partner Deliveries
  D. Expense object-level (submitter) scoping
  E. Cross-Sales-Officer Customer -> Visit attachment
  F. Cross-Sales-Officer Customer -> Follow-up attachment (+ Visit-generated)
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


def _register_org(label: str) -> tuple[dict, str]:
    email = f"admin_{uuid.uuid4().hex[:8]}@{label.replace('_', '').lower()}.com"
    r = client.post(
        "/auth/register",
        json={
            "organization_name": f"{label} Org", "admin_name": f"Admin {label}",
            "email": email, "password": "Password123!", "role": "admin",
        },
    )
    assert r.status_code == 201, r.text
    auth = {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}
    return auth, r.json()["tokens"]["refresh_token"]


def _create_staff(admin_auth: dict, name: str, role_name: str) -> tuple[str, dict, str]:
    email = f"{role_name.lower().replace(' ', '_')}_{uuid.uuid4().hex[:8]}@example.com"
    res = client.post(
        "/users",
        json={"name": name, "email": email, "password": "Password123!", "role": role_name},
        headers=admin_auth,
    )
    assert res.status_code == 201, res.text
    user_id = res.json()["id"]
    login = client.post("/auth/login", json={"email": email, "password": "Password123!"})
    assert login.status_code == 200, login.text
    auth = {"Authorization": f"Bearer {login.json()['tokens']['access_token']}"}
    return user_id, auth, login.json()["tokens"]["refresh_token"]


def _create_custom_role_staff(admin_auth: dict, name: str, permissions: dict, data_scope: str = "all"):
    """A user whose Role is a genuine custom role (not one of the 3 legacy
    defaults) -> user.role ends up None, user.role_id set. See
    app.routers.users.create_user's LEGACY_ROLE_BY_NAME.get(role.name)."""
    role_res = client.post(
        "/roles",
        json={
            "name": f"Custom {uuid.uuid4().hex[:6]}", "workspace": "sales",
            "data_scope": data_scope, "permissions": permissions,
        },
        headers=admin_auth,
    )
    assert role_res.status_code == 201, role_res.text
    role_name = role_res.json()["name"]
    return _create_staff(admin_auth, name, role_name)


def _setup_customer_flow(label: str):
    """Org + 2 Sales Officers, each with their own Customer."""
    admin_auth, _ = _register_org(label)
    so1_id, so1_auth, _ = _create_staff(admin_auth, "SO One", "Sales Officer")
    so2_id, so2_auth, _ = _create_staff(admin_auth, "SO Two", "Sales Officer")

    cust1 = client.post(
        "/customers", json={"name": "Cust One", "assigned_sales_officer_id": so1_id}, headers=admin_auth
    ).json()
    cust2 = client.post(
        "/customers", json={"name": "Cust Two", "assigned_sales_officer_id": so2_id}, headers=admin_auth
    ).json()
    return {
        "admin_auth": admin_auth, "so1_id": so1_id, "so1_auth": so1_auth,
        "so2_id": so2_id, "so2_auth": so2_auth, "cust1": cust1, "cust2": cust2,
    }


# ============================================================================
# A. /auth/refresh custom role crash
# ============================================================================

def run_group_a():
    print("\n=== A. /auth/refresh custom role crash ===")
    admin_auth, admin_refresh = _register_org(f"RefreshA_{uuid.uuid4().hex[:6]}")

    # A1: system-role (admin) refresh works
    r = client.post("/auth/refresh", json={"refresh_token": admin_refresh})
    log_test("A1: system-role (admin) refresh succeeds (200)", r.status_code == 200, r.text)

    # A2/A3: custom-role user (user.role is None) refresh works, no crash
    _, _, custom_refresh = _create_custom_role_staff(
        admin_auth, "Custom User", {"visits": {"view": True, "create": True}}, data_scope="own"
    )
    r2 = client.post("/auth/refresh", json={"refresh_token": custom_refresh})
    log_test(
        "A2/A3: custom-role user refresh succeeds, no 500/AttributeError (200)",
        r2.status_code == 200, r2.text,
    )

    # A4: no privilege escalation -- the refreshed token still can't do admin-only things
    new_access = r2.json()["access_token"]
    custom_auth = {"Authorization": f"Bearer {new_access}"}
    r3 = client.get("/roles", headers=custom_auth)
    log_test(
        "A4: refreshed custom-role token still cannot access Admin-only /roles (403)",
        r3.status_code == 403, r3.text,
    )


# ============================================================================
# B. Vehicle Stock / loading-session IDOR
# ============================================================================

def run_group_b():
    print("\n=== B. Vehicle Stock IDOR ===")
    admin_auth, _ = _register_org(f"VStockB_{uuid.uuid4().hex[:6]}")
    dp1_id, dp1_auth, _ = _create_staff(admin_auth, "DP One", "Delivery Partner")
    dp2_id, dp2_auth, _ = _create_staff(admin_auth, "DP Two", "Delivery Partner")

    prod = client.post(
        "/products",
        json={"name": f"P{uuid.uuid4().hex[:4]}", "sku": f"SKU-{uuid.uuid4().hex[:6]}", "price": 10.0, "total_inventory": 100},
        headers=admin_auth,
    ).json()

    load1 = client.post(
        "/vehicle-stock/loading",
        json={"delivery_partner_id": dp1_id, "items": [{"product_id": prod["id"], "loaded_qty": 5}]},
        headers=admin_auth,
    ).json()
    session_id = load1["id"]

    # B1: DP1 accesses their own session
    r = client.get(f"/vehicle-stock/current/{dp1_id}", headers=dp1_auth)
    log_test("B1: DP1 accesses own current stock (200)", r.status_code == 200, r.text)

    # B2: DP2 accesses DP1's session via the delivery_partner_id path param
    r2 = client.get(f"/vehicle-stock/current/{dp1_id}", headers=dp2_auth)
    log_test("B2: DP2 cannot view DP1's current stock via path param (404)", r2.status_code == 404, r2.text)

    # B3: DP2 mutates DP1's session by UUID (extra-load)
    r3 = client.post(
        f"/vehicle-stock/{session_id}/extra-load",
        json={"items": [{"product_id": prod["id"], "quantity": 1}]},
        headers=dp2_auth,
    )
    log_test("B3: DP2 cannot extra-load DP1's session by UUID (404)", r3.status_code == 404, r3.text)

    # B3b: DP2 cannot start a session "for" DP1 (ad-hoc creation naming another partner)
    r3b = client.post(
        "/vehicle-stock/loading",
        json={"delivery_partner_id": dp1_id, "items": [{"product_id": prod["id"], "loaded_qty": 1}]},
        headers=dp2_auth,
    )
    log_test("B3b: DP2 cannot start a loading session for DP1 (403)", r3b.status_code == 403, r3b.text)

    # B4: cross-organization session access denied
    other_admin_auth, _ = _register_org(f"VStockBOther_{uuid.uuid4().hex[:6]}")
    _, other_dp_auth, _ = _create_staff(other_admin_auth, "Other DP", "Delivery Partner")
    r4 = client.get(f"/vehicle-stock/{session_id}/reconciliations", headers=other_dp_auth)
    log_test("B4: cross-org session access denied (404)", r4.status_code == 404, r4.text)

    # B5: Admin (authorized, org-wide) can still extra-load DP1's session
    r5 = client.post(
        f"/vehicle-stock/{session_id}/extra-load",
        json={"items": [{"product_id": prod["id"], "quantity": 2}]},
        headers=admin_auth,
    )
    log_test("B5: Admin extra-load on DP1's session still works (200)", r5.status_code == 200, r5.text)

    # B5b: DP1 can extra-load their own session
    r5b = client.post(
        f"/vehicle-stock/{session_id}/extra-load",
        json={"items": [{"product_id": prod["id"], "quantity": 1}]},
        headers=dp1_auth,
    )
    log_test("B5b: DP1 can extra-load their own session (200)", r5b.status_code == 200, r5b.text)

    # B: Admin list-all still sees every session (org-wide)
    r6 = client.get("/vehicle-stock", headers=admin_auth)
    log_test(
        "B: Admin GET /vehicle-stock sees DP1's session (org-wide)",
        r6.status_code == 200 and any(s["id"] == session_id for s in r6.json()), r6.text,
    )
    # DP2's own-scope list must not include DP1's session
    r7 = client.get("/vehicle-stock", headers=dp2_auth)
    log_test(
        "B: DP2 GET /vehicle-stock does not include DP1's session",
        r7.status_code == 200 and not any(s["id"] == session_id for s in r7.json()), r7.text,
    )


# ============================================================================
# C. Delivery Partner access to unassigned/other-partner Deliveries
# ============================================================================

def run_group_c():
    print("\n=== C. Unassigned/other-partner Delivery access ===")
    admin_auth, _ = _register_org(f"DelC_{uuid.uuid4().hex[:6]}")
    dp1_id, dp1_auth, _ = _create_staff(admin_auth, "DP One", "Delivery Partner")
    dp2_id, dp2_auth, _ = _create_staff(admin_auth, "DP Two", "Delivery Partner")

    wh = client.post("/warehouses", json={"name": "WH", "is_default": True}, headers=admin_auth).json()
    prod = client.post(
        "/products", json={"name": f"P{uuid.uuid4().hex[:4]}", "sku": f"SKU-{uuid.uuid4().hex[:6]}", "price": 10.0},
        headers=admin_auth,
    ).json()
    client.post(f"/warehouses/{wh['id']}/stock/adjust", json={"product_id": prod["id"], "quantity": 50}, headers=admin_auth)
    cust = client.post("/customers", json={"name": "Cust"}, headers=admin_auth).json()

    order = client.post(
        "/orders", json={
            "customer_id": cust["id"], "warehouse_id": wh["id"],
            "items": [{"product_id": prod["id"], "quantity": 5, "unit_price": 10.0}],
        }, headers=admin_auth,
    ).json()

    # Unassigned delivery (no delivery_partner_id at all)
    unassigned = client.post(
        "/deliveries", json={"order_id": order["id"]}, headers=admin_auth,
    ).json()

    # C3: neither DP can see the unassigned delivery
    r = client.get(f"/deliveries/by-id/{unassigned['id']}", headers=dp1_auth)
    log_test("C3: DP1 cannot access unassigned Delivery (404)", r.status_code == 404, r.text)

    # C4: unassigned delivery does not appear in DP1's list
    r_list = client.get("/deliveries", headers=dp1_auth)
    log_test(
        "C4: unassigned Delivery not exposed in DP1's list",
        r_list.status_code == 200 and not any(d["id"] == unassigned["id"] for d in r_list.json()),
        r_list.text,
    )

    # Now assign it to DP1
    order2 = client.post(
        "/orders", json={
            "customer_id": cust["id"], "warehouse_id": wh["id"],
            "items": [{"product_id": prod["id"], "quantity": 5, "unit_price": 10.0}],
        }, headers=admin_auth,
    ).json()
    assigned = client.post(
        "/deliveries", json={"order_id": order2["id"], "delivery_partner_id": dp1_id}, headers=admin_auth,
    ).json()

    # C1: DP1 accesses own assigned Delivery
    r_own = client.get(f"/deliveries/by-id/{assigned['id']}", headers=dp1_auth)
    log_test("C1: DP1 accesses own assigned Delivery (200)", r_own.status_code == 200, r_own.text)

    # C2: DP2 cannot access DP1's assigned Delivery
    r_other = client.get(f"/deliveries/by-id/{assigned['id']}", headers=dp2_auth)
    log_test("C2: DP2 cannot access DP1's assigned Delivery (404)", r_other.status_code == 404, r_other.text)

    # DP2 cannot accept DP1's delivery either
    r_accept = client.post(f"/deliveries/{assigned['id']}/accept", headers=dp2_auth)
    log_test("C2: DP2 cannot accept DP1's Delivery (403/404)", r_accept.status_code in (403, 404), r_accept.text)

    # C5: Admin still has full access
    r_admin = client.get(f"/deliveries/by-id/{assigned['id']}", headers=admin_auth)
    log_test("C5: Admin still accesses any Delivery (200)", r_admin.status_code == 200, r_admin.text)


# ============================================================================
# D. Expense object-level scoping
# ============================================================================

def run_group_d():
    print("\n=== D. Expense object-level scoping ===")
    admin_auth, _ = _register_org(f"ExpD_{uuid.uuid4().hex[:6]}")
    so1_id, so1_auth, _ = _create_custom_role_staff(
        admin_auth, "SO Own1", {"expenses": {"view": True, "create": True, "edit": True, "delete": True}}, "own"
    )
    so2_id, so2_auth, _ = _create_custom_role_staff(
        admin_auth, "SO Own2", {"expenses": {"view": True, "create": True, "edit": True, "delete": True}}, "own"
    )
    acct_id, acct_auth, _ = _create_staff(admin_auth, "Accountant One", "Accountant")

    exp1 = client.post(
        "/expenses", json={"category": "travel", "amount": 100.0, "description": "trip"}, headers=so1_auth
    ).json()
    exp2 = client.post(
        "/expenses", json={"category": "travel", "amount": 200.0, "description": "trip2"}, headers=so2_auth
    ).json()

    # D1: own-scope user sees own expense
    r = client.get(f"/expenses/{exp1['id']}", headers=so1_auth)
    log_test("D1: SO1 sees own Expense (200)", r.status_code == 200, r.text)

    # D2: own-scope user cannot retrieve another's expense
    r2 = client.get(f"/expenses/{exp2['id']}", headers=so1_auth)
    log_test("D2: SO1 cannot retrieve SO2's Expense (404)", r2.status_code == 404, r2.text)

    # D3: own-scope user cannot modify another's expense
    r3 = client.patch(f"/expenses/{exp2['id']}", json={"description": "hacked"}, headers=so1_auth)
    log_test("D3: SO1 cannot modify SO2's Expense (404)", r3.status_code == 404, r3.text)
    r3b = client.delete(f"/expenses/{exp2['id']}", headers=so1_auth)
    log_test("D3: SO1 cannot delete SO2's Expense (404)", r3b.status_code == 404, r3b.text)

    # D4: own-scope list does not expose others' expenses
    r4 = client.get("/expenses", headers=so1_auth)
    ids = [e["id"] for e in r4.json()]
    log_test(
        "D4: SO1 list contains own but not SO2's Expense",
        r4.status_code == 200 and exp1["id"] in ids and exp2["id"] not in ids, r4.text,
    )

    # D5: Accountant (all-scope, expenses:approve) can review both SOs' expenses
    r5 = client.patch(f"/expenses/{exp1['id']}/approve", headers=acct_auth)
    log_test("D5: Accountant can approve SO1's Expense (200)", r5.status_code == 200, r5.text)
    r5b = client.patch(f"/expenses/{exp2['id']}/approve", headers=acct_auth)
    log_test("D5: Accountant can approve SO2's Expense (200)", r5b.status_code == 200, r5b.text)

    # D6: Admin has organization-wide access
    r6 = client.get(f"/expenses/{exp1['id']}", headers=admin_auth)
    log_test("D6: Admin can view any org Expense (200)", r6.status_code == 200, r6.text)
    r6b = client.get("/expenses", headers=admin_auth)
    ids6 = [e["id"] for e in r6b.json()]
    log_test(
        "D6: Admin list includes both SO1 and SO2 Expenses",
        exp1["id"] in ids6 and exp2["id"] in ids6, r6b.text,
    )

    # D7: cross-organization Expense access denied
    other_admin_auth, _ = _register_org(f"ExpDOther_{uuid.uuid4().hex[:6]}")
    r7 = client.get(f"/expenses/{exp1['id']}", headers=other_admin_auth)
    log_test("D7: cross-org Expense access denied (404)", r7.status_code == 404, r7.text)


# ============================================================================
# E. Customer -> Visit scope
# ============================================================================

def run_group_e():
    print("\n=== E. Customer -> Visit scope ===")
    ctx = _setup_customer_flow(f"VisitE_{uuid.uuid4().hex[:6]}")

    # E1: SO1 creates a Visit for their own Customer
    r = client.post("/visits", json={"customer_id": ctx["cust1"]["id"], "purpose": "check-in"}, headers=ctx["so1_auth"])
    log_test("E1: SO1 creates Visit for own Customer (201)", r.status_code == 201, r.text)

    # E2: SO1 attempts a Visit for SO2's Customer
    r2 = client.post("/visits", json={"customer_id": ctx["cust2"]["id"], "purpose": "check-in"}, headers=ctx["so1_auth"])
    log_test("E2: SO1 cannot create Visit for SO2's Customer (400)", r2.status_code == 400, r2.text)

    # E3: Admin can create a Visit for either Customer
    r3 = client.post("/visits", json={"customer_id": ctx["cust2"]["id"], "purpose": "admin visit"}, headers=ctx["admin_auth"])
    log_test("E3: Admin creates Visit for any Customer (201)", r3.status_code == 201, r3.text)

    # E4: cross-org Customer UUID denied
    other_ctx = _setup_customer_flow(f"VisitEOther_{uuid.uuid4().hex[:6]}")
    r4 = client.post(
        "/visits", json={"customer_id": other_ctx["cust1"]["id"], "purpose": "cross-org"}, headers=ctx["so1_auth"]
    )
    log_test("E4: cross-org Customer UUID rejected (400)", r4.status_code == 400, r4.text)

    # E5: existing Lead -> Visit protection still works
    lead1 = client.post(
        "/leads", json={"name": "Lead One", "mobile_number": f"9{uuid.uuid4().hex[:9]}",
                        "lead_source": "Referral", "assigned_salesperson_id": ctx["so1_id"]},
        headers=ctx["admin_auth"],
    ).json()
    r5 = client.post("/visits", json={"lead_id": lead1["id"], "purpose": "lead visit"}, headers=ctx["so2_auth"])
    log_test("E5: SO2 cannot create Visit for SO1's Lead (400) -- existing protection intact", r5.status_code == 400, r5.text)


# ============================================================================
# F. Customer -> Follow-up scope (+ Visit-generated)
# ============================================================================

def run_group_f():
    print("\n=== F. Customer -> Follow-up scope ===")
    ctx = _setup_customer_flow(f"FuF_{uuid.uuid4().hex[:6]}")

    # F1: SO1 creates a Follow-up for own Customer
    r = client.post(
        "/follow-ups", json={"customer_id": ctx["cust1"]["id"], "title": "call", "due_date": "2026-09-10T00:00:00Z"},
        headers=ctx["so1_auth"],
    )
    log_test("F1: SO1 creates Follow-up for own Customer (201)", r.status_code == 201, r.text)

    # F2: SO1 attempts a Follow-up for SO2's Customer
    r2 = client.post(
        "/follow-ups", json={"customer_id": ctx["cust2"]["id"], "title": "call", "due_date": "2026-09-10T00:00:00Z"},
        headers=ctx["so1_auth"],
    )
    log_test("F2: SO1 cannot create Follow-up for SO2's Customer (400)", r2.status_code == 400, r2.text)

    # F3: Admin can create for either Customer
    r3 = client.post(
        "/follow-ups", json={"customer_id": ctx["cust2"]["id"], "title": "admin call", "due_date": "2026-09-10T00:00:00Z"},
        headers=ctx["admin_auth"],
    )
    log_test("F3: Admin creates Follow-up for any Customer (201)", r3.status_code == 201, r3.text)

    # F4: cross-org Customer UUID denied
    other_ctx = _setup_customer_flow(f"FuFOther_{uuid.uuid4().hex[:6]}")
    r4 = client.post(
        "/follow-ups",
        json={"customer_id": other_ctx["cust1"]["id"], "title": "cross-org", "due_date": "2026-09-10T00:00:00Z"},
        headers=ctx["so1_auth"],
    )
    log_test("F4: cross-org Customer UUID rejected (400)", r4.status_code == 400, r4.text)

    # F5: Lead Follow-up ownership protection still works
    lead1 = client.post(
        "/leads", json={"name": "Lead One", "mobile_number": f"9{uuid.uuid4().hex[:9]}",
                        "lead_source": "Referral", "assigned_salesperson_id": ctx["so1_id"]},
        headers=ctx["admin_auth"],
    ).json()
    r5 = client.post(
        "/follow-ups", json={"lead_id": lead1["id"], "title": "lead call", "due_date": "2026-09-10T00:00:00Z"},
        headers=ctx["so2_auth"],
    )
    log_test("F5: SO2 cannot create Follow-up for SO1's Lead (400) -- existing protection intact", r5.status_code == 400, r5.text)

    # F6: Visit-generated Follow-up ownership protection
    visit1 = client.post(
        "/visits", json={"customer_id": ctx["cust1"]["id"], "purpose": "visit"}, headers=ctx["so1_auth"]
    ).json()
    r6 = client.post(
        f"/visits/{visit1['id']}/follow-ups",
        json={"title": "via visit", "due_date": "2026-09-10T00:00:00Z"},
        headers=ctx["so2_auth"],
    )
    log_test(
        "F6: SO2 cannot create a Follow-up on SO1's Visit (400/404) -- Visit-generated protection intact",
        r6.status_code in (400, 404), r6.text,
    )
    # And directly referencing another SO's visit_id from POST /follow-ups
    r6b = client.post(
        "/follow-ups",
        json={"visit_id": visit1["id"], "title": "via visit_id", "due_date": "2026-09-10T00:00:00Z"},
        headers=ctx["so2_auth"],
    )
    log_test("F6: SO2 cannot reference SO1's visit_id directly (400)", r6b.status_code == 400, r6b.text)


def run_all_tests():
    global PASSED, FAILED
    PASSED = 0
    FAILED = 0
    print("\n=======================================================")
    print("TEST SUITE: Authorization Security Gap Fixes")
    print("=======================================================")
    run_group_a()
    run_group_b()
    run_group_c()
    run_group_d()
    run_group_e()
    run_group_f()
    print("\n=======================================================")
    print(f"RESULTS: {PASSED} passed, {FAILED} failed")
    print("=======================================================\n")
    if FAILED > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_all_tests()
