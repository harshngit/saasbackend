"""Comprehensive test suite for Dashboard Filters and Companies API contract verification."""

import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from app.main import app
from app.core.database import SessionLocal
from app.models.organization import Organization
from app.models.customer import Customer
from app.models.supplier import Supplier
from app.models.warehouse import Warehouse

from app.models.user import User

client = TestClient(app)

passed_count = 0
failed_count = 0


def check(description: str, condition: bool):
    global passed_count, failed_count
    if condition:
        print(f"  PASS  {description}")
        passed_count += 1
    else:
        print(f"  FAIL  {description}")
        failed_count += 1


def register_org(name_prefix: str = "Dashboard Firm"):
    email = f"dash_admin_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post(
        "/auth/register",
        json={
            "organization_name": f"{name_prefix} {uuid.uuid4().hex[:6]}",
            "admin_name": "Dashboard Admin",
            "email": email,
            "password": "Password123!",
            "role": "admin",
        },
    )
    assert r.status_code == 201, r.text
    token = r.json()["tokens"]["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        org_id = user.organization_id
    finally:
        db.close()

    return headers, org_id


def main():
    print("\n=======================================================")
    print("TEST SUITE: Dashboard Filters & Companies API Contract")
    print("=======================================================\n")

    auth1, org1_id = register_org("Firm A")
    auth2, org2_id = register_org("Firm B")

    db = SessionLocal()
    try:
        # Seed Customer, Supplier, Warehouse in Org 1
        cust1 = Customer(organization_id=org1_id, name="Customer One", business_name="Cust 1 Corp")
        sup1 = Supplier(organization_id=org1_id, name="Supplier One")
        wh1 = Warehouse(organization_id=org1_id, name="Warehouse One", code="WH-01")
        
        # Seed Customer, Supplier, Warehouse in Org 2
        cust2 = Customer(organization_id=org2_id, name="Customer Two", business_name="Cust 2 Corp")
        sup2 = Supplier(organization_id=org2_id, name="Supplier Two")
        wh2 = Warehouse(organization_id=org2_id, name="Warehouse Two", code="WH-02")
        
        db.add_all([cust1, sup1, wh1, cust2, sup2, wh2])
        db.commit()

        c1_id = cust1.id
        s1_id = sup1.id
        w1_id = wh1.id

        c2_id = cust2.id
        s2_id = sup2.id
        w2_id = wh2.id
    finally:
        db.close()

    # 1. Dashboard without filters
    print("--- 1. Dashboard without filters ---")
    r1 = client.get("/dashboard/admin", headers=auth1)
    check("GET /dashboard/admin returns HTTP 200", r1.status_code == 200)
    data1 = r1.json()
    filters1 = data1["filters"]
    check("Response contains filters object", "filters" in data1)
    check("Summary block present", "summary" in data1)

    # 2. Dashboard with company_id
    print("\n--- 2. Dashboard with company_id ---")
    r2 = client.get(f"/dashboard/admin?company_id={org1_id}", headers=auth1)
    check("GET /dashboard/admin?company_id={org1_id} returns HTTP 200", r2.status_code == 200)
    check("filters.company_id matches org1_id", r2.json()["filters"]["company_id"] == org1_id)

    # 3. Dashboard with warehouse_id
    print("\n--- 3. Dashboard with warehouse_id ---")
    r3 = client.get(f"/dashboard/admin?warehouse_id={w1_id}", headers=auth1)
    check("GET /dashboard/admin?warehouse_id={w1_id} returns HTTP 200", r3.status_code == 200)
    check("filters.warehouse_id matches w1_id", r3.json()["filters"]["warehouse_id"] == w1_id)

    # 4. Dashboard with customer_id
    print("\n--- 4. Dashboard with customer_id ---")
    r4 = client.get(f"/dashboard/admin?customer_id={c1_id}", headers=auth1)
    check("GET /dashboard/admin?customer_id={c1_id} returns HTTP 200", r4.status_code == 200)
    check("filters.customer_id matches c1_id", r4.json()["filters"]["customer_id"] == c1_id)

    # 5. Dashboard with supplier_id
    print("\n--- 5. Dashboard with supplier_id ---")
    r5 = client.get(f"/dashboard/admin?supplier_id={s1_id}", headers=auth1)
    check("GET /dashboard/admin?supplier_id={s1_id} returns HTTP 200", r5.status_code == 200)
    check("filters.supplier_id matches s1_id", r5.json()["filters"]["supplier_id"] == s1_id)

    # 6. Dashboard with date_from
    print("\n--- 6. Dashboard with date_from ---")
    r6 = client.get("/dashboard/admin?date_from=2026-01-01", headers=auth1)
    check("GET /dashboard/admin?date_from=2026-01-01 returns HTTP 200", r6.status_code == 200)
    check("filters.date_from is 2026-01-01", r6.json()["filters"]["date_from"] == "2026-01-01")

    # 7. Dashboard with date_to
    print("\n--- 7. Dashboard with date_to ---")
    r7 = client.get("/dashboard/admin?date_to=2026-12-31", headers=auth1)
    check("GET /dashboard/admin?date_to=2026-12-31 returns HTTP 200", r7.status_code == 200)
    check("filters.date_to is 2026-12-31", r7.json()["filters"]["date_to"] == "2026-12-31")

    # 8. Dashboard with date_from + date_to
    print("\n--- 8. Dashboard with date_from + date_to ---")
    r8 = client.get("/dashboard/admin?date_from=2026-01-01&date_to=2026-06-30", headers=auth1)
    check("GET with date range returns HTTP 200", r8.status_code == 200)
    check("filters.date_from is 2026-01-01", r8.json()["filters"]["date_from"] == "2026-01-01")
    check("filters.date_to is 2026-06-30", r8.json()["filters"]["date_to"] == "2026-06-30")

    # 9. Multiple filters are AND-combined
    print("\n--- 9. Multiple filters are AND-combined ---")
    r9 = client.get(f"/dashboard/admin?company_id={org1_id}&warehouse_id={w1_id}&customer_id={c1_id}", headers=auth1)
    check("Multiple combined filters returns HTTP 200", r9.status_code == 200)
    f9 = r9.json()["filters"]
    check("company_id matches", f9["company_id"] == org1_id)
    check("warehouse_id matches", f9["warehouse_id"] == w1_id)
    check("customer_id matches", f9["customer_id"] == c1_id)

    # 10. All filters together
    print("\n--- 10. All filters together ---")
    all_url = (
        f"/dashboard/admin?company_id={org1_id}&warehouse_id={w1_id}"
        f"&customer_id={c1_id}&supplier_id={s1_id}"
        f"&date_from=2026-01-01&date_to=2026-12-31"
    )
    r10 = client.get(all_url, headers=auth1)
    check("All 6 filters combined returns HTTP 200", r10.status_code == 200)
    f10 = r10.json()["filters"]
    check("All 6 canonical filters populated in response", (
        f10["company_id"] == org1_id and
        f10["warehouse_id"] == w1_id and
        f10["customer_id"] == c1_id and
        f10["supplier_id"] == s1_id and
        f10["date_from"] == "2026-01-01" and
        f10["date_to"] == "2026-12-31"
    ))

    # 11, 12, 13. branch_id not accepted/processed, filters contain only 6 canonical keys
    print("\n--- 11-13. branch_id removal and response key verification ---")
    r11 = client.get("/dashboard/admin?branch_id=some_branch_123", headers=auth1)
    check("GET with branch_id query parameter returns HTTP 200 (ignored)", r11.status_code == 200)
    f11 = r11.json()["filters"]
    check("filters object does NOT contain branch_id key", "branch_id" not in f11)
    canonical_keys = {"date_from", "date_to", "company_id", "warehouse_id", "customer_id", "supplier_id"}
    check("filters object contains exactly the 6 canonical keys", set(f11.keys()) == canonical_keys)

    # 14. GET /companies?active=true works
    print("\n--- 14. GET /companies?active=true ---")
    rc1 = client.get("/companies?active=true", headers=auth1)
    check("GET /companies?active=true returns HTTP 200", rc1.status_code == 200)
    c_list = rc1.json()
    check("Response is a list", isinstance(c_list, list))
    check("Contains org1 company item", len(c_list) >= 1 and c_list[0]["id"] == org1_id)
    check("Item contains id, name, is_active", "id" in c_list[0] and "name" in c_list[0] and "is_active" in c_list[0])

    # 15. Inactive companies are excluded when active=true
    print("\n--- 15. Inactive company filtering ---")
    db = SessionLocal()
    try:
        org_obj = db.get(Organization, org1_id)
        org_obj.company_status = "inactive"
        db.commit()
    finally:
        db.close()

    rc_inact = client.get("/companies?active=true", headers=auth1)
    check("GET /companies?active=true returns empty list for inactive company", len(rc_inact.json()) == 0)

    rc_all = client.get("/companies", headers=auth1)
    check("GET /companies without active filter returns company item", len(rc_all.json()) == 1 and rc_all.json()[0]["is_active"] is False)

    # Restore org status
    db = SessionLocal()
    try:
        org_obj = db.get(Organization, org1_id)
        org_obj.company_status = "active"
        db.commit()
    finally:
        db.close()

    # 16. Cross-tenant IDs rejected
    print("\n--- 16. Cross-tenant filter access rejected ---")
    r_cross_comp = client.get(f"/dashboard/admin?company_id={org2_id}", headers=auth1)
    check("Cross-tenant company_id rejected with HTTP 400", r_cross_comp.status_code == 400)

    r_cross_wh = client.get(f"/dashboard/admin?warehouse_id={w2_id}", headers=auth1)
    check("Cross-tenant warehouse_id rejected with HTTP 400", r_cross_wh.status_code == 400)

    r_cross_cust = client.get(f"/dashboard/admin?customer_id={c2_id}", headers=auth1)
    check("Cross-tenant customer_id rejected with HTTP 400", r_cross_cust.status_code == 400)

    r_cross_sup = client.get(f"/dashboard/admin?supplier_id={s2_id}", headers=auth1)
    check("Cross-tenant supplier_id rejected with HTTP 400", r_cross_sup.status_code == 400)

    # 17. Existing unfiltered Dashboard behavior remains unchanged
    print("\n--- 17. Unfiltered Dashboard behavior remains unchanged ---")
    r17 = client.get("/dashboard/admin", headers=auth1)
    check("Unfiltered GET /dashboard/admin returns HTTP 200", r17.status_code == 200)
    check("Contains all expected sections", all(k in r17.json() for k in [
        "filters", "summary", "orders", "cashflow", "receivables_payables",
        "top_customers", "top_products", "expense_breakdown", "sales_trend",
        "stock_watch", "recent_orders"
    ]))

    print("\n=======================================================")
    print(f"DASHBOARD FILTER RESULTS: Passed={passed_count}, Failed={failed_count}")
    print("=======================================================\n")

    if failed_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
