"""Test suite for Delivery Partner Dashboard Company Order Visibility + KPI Feature."""

import os
import sys
import uuid
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.main import app
from app.core.database import SessionLocal
from app.models.sales_order import SalesOrder, SalesOrderItem
from app.models.customer import Customer
from app.models.product import Product
from app.models.user import User

client = TestClient(app)


def _register_org(name_prefix: str = "Test Org"):
    email = f"admin_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post(
        "/auth/register",
        json={
            "organization_name": f"{name_prefix} {uuid.uuid4().hex[:6]}",
            "admin_name": "Admin User",
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


def _create_staff(admin_auth: dict, name: str, role_name: str) -> tuple[dict, dict]:
    email = f"{name.lower().replace(' ', '_')}_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post(
        "/users",
        json={"name": name, "email": email, "password": "Password123!", "role": role_name},
        headers=admin_auth,
    )
    assert r.status_code == 201, r.text
    user_data = r.json()

    # Login to get token
    login_r = client.post(
        "/auth/login",
        json={"email": email, "password": "Password123!"},
    )
    assert login_r.status_code == 200, login_r.text
    token = login_r.json()["tokens"]["access_token"]
    auth = {"Authorization": f"Bearer {token}"}
    return auth, user_data


def _create_customer_and_product(db, org_id: str):
    customer = Customer(
        organization_id=org_id,
        name=f"Customer {uuid.uuid4().hex[:6]}",
        phone=f"+91{uuid.uuid4().int % 10000000000:010d}",
    )
    product = Product(
        organization_id=org_id,
        name=f"Product {uuid.uuid4().hex[:6]}",
        sku=f"SKU-{uuid.uuid4().hex[:6]}",
        price=100.0,
    )
    db.add(customer)
    db.add(product)
    db.commit()
    db.refresh(customer)
    db.refresh(product)
    return customer, product


def test_delivery_partner_dashboard_kpis_and_orders():
    admin_auth, org_id = _register_org("DP Dash Test Org A")
    dp_a_auth, dp_a = _create_staff(admin_auth, "Driver Alpha", "Delivery Partner")
    dp_b_auth, dp_b = _create_staff(admin_auth, "Driver Beta", "Delivery Partner")

    db = SessionLocal()
    try:
        customer, product = _create_customer_and_product(db, org_id)

        # Create 46 orders in Org A
        # Partner A: 15 active assigned deliveries (5 planned, 5 loaded, 3 in_transit, 2 partially_delivered)
        # Partner A: 3 inactive/other (2 delivered, 1 cancelled)
        # Partner B: 5 active assigned deliveries (5 planned)
        # Unassigned: 23 orders
        # Total = 15 + 3 + 5 + 23 = 46 orders

        orders = []
        for i in range(46):
            if i < 5:
                # DP A: planned
                fulfilment = "planned"
                assigned_to = dp_a["id"]
                st = "confirmed"
            elif i < 10:
                # DP A: loaded
                fulfilment = "loaded"
                assigned_to = dp_a["id"]
                st = "confirmed"
            elif i < 13:
                # DP A: in_transit
                fulfilment = "in_transit"
                assigned_to = dp_a["id"]
                st = "confirmed"
            elif i < 15:
                # DP A: partially_delivered
                fulfilment = "partially_delivered"
                assigned_to = dp_a["id"]
                st = "confirmed"
            elif i < 17:
                # DP A: delivered (should NOT be in active count)
                fulfilment = "delivered"
                assigned_to = dp_a["id"]
                st = "completed"
            elif i < 18:
                # DP A: cancelled (should NOT be in active count)
                fulfilment = "cancelled"
                assigned_to = dp_a["id"]
                st = "cancelled"
            elif i < 23:
                # DP B: planned (5 orders)
                fulfilment = "planned"
                assigned_to = dp_b["id"]
                st = "confirmed"
            else:
                # Unassigned / other
                fulfilment = "not_started"
                assigned_to = None
                st = "confirmed"

            order = SalesOrder(
                organization_id=org_id,
                order_number=f"SO-A-{i+1:03d}",
                customer_id=customer.id,
                status=st,
                fulfilment_status=fulfilment,
                assigned_delivery_partner_id=assigned_to,
                total=100.0 * (i + 1),
                subtotal=100.0 * (i + 1),
                discount=0,
                tax=0,
                source="direct",
            )
            item = SalesOrderItem(
                order=order,
                product_id=product.id,
                product_name=product.name,
                quantity=1,
                unit_price=100.0,
                line_total=100.0,
            )
            order.items.append(item)
            orders.append(order)

        db.add_all(orders)
        db.commit()

        # Check A: Delivery Partner A dashboard KPI (46 total company orders, 15 active assigned deliveries)
        r_a = client.get("/dashboard/delivery-partner", headers=dp_a_auth)
        assert r_a.status_code == 200, r_a.text
        data_a = r_a.json()
        assert data_a["total_company_orders"] == 46
        assert data_a["my_assigned_deliveries"] == 15

        # Check B: Delivery Partner B dashboard KPI (46 total company orders, 5 active assigned deliveries)
        r_b = client.get("/dashboard/delivery-partner", headers=dp_b_auth)
        assert r_b.status_code == 200, r_b.text
        data_b = r_b.json()
        assert data_b["total_company_orders"] == 46
        assert data_b["my_assigned_deliveries"] == 5

        # Check C: Company order list for Delivery Partner A
        r_orders = client.get("/dashboard/delivery-partner/orders?limit=100", headers=dp_a_auth)
        assert r_orders.status_code == 200, r_orders.text
        order_list = r_orders.json()
        assert len(order_list) == 46
        assert all(o["organization_id"] == org_id for o in order_list)

        # Verify existing /deliveries/assigned for DP A returns exactly 15 active deliveries
        r_assigned_a = client.get("/deliveries/assigned", headers=dp_a_auth)
        assert r_assigned_a.status_code == 200, r_assigned_a.text
        assigned_list_a = r_assigned_a.json()
        assert len(assigned_list_a) == 15
        assert len(assigned_list_a) == data_a["my_assigned_deliveries"]

        # Verify existing /deliveries/assigned for DP B returns exactly 5 active deliveries
        r_assigned_b = client.get("/deliveries/assigned", headers=dp_b_auth)
        assert r_assigned_b.status_code == 200, r_assigned_b.text
        assigned_list_b = r_assigned_b.json()
        assert len(assigned_list_b) == 5
        assert len(assigned_list_b) == data_b["my_assigned_deliveries"]

        # Check Pagination on /dashboard/delivery-partner/orders
        r_p1 = client.get("/dashboard/delivery-partner/orders?limit=10&offset=0", headers=dp_a_auth)
        assert r_p1.status_code == 200
        p1_list = r_p1.json()
        assert len(p1_list) == 10

        r_p2 = client.get("/dashboard/delivery-partner/orders?limit=10&offset=10", headers=dp_a_auth)
        assert r_p2.status_code == 200
        p2_list = r_p2.json()
        assert len(p2_list) == 10
        # Ensure disjoint pages
        p1_ids = {o["id"] for o in p1_list}
        p2_ids = {o["id"] for o in p2_list}
        assert p1_ids.isdisjoint(p2_ids)

        # Check Filter by search
        r_search = client.get("/dashboard/delivery-partner/orders?search=SO-A-001", headers=dp_a_auth)
        assert r_search.status_code == 200
        search_list = r_search.json()
        assert len(search_list) == 1
        assert search_list[0]["order_number"] == "SO-A-001"

    finally:
        db.close()


def test_tenant_isolation():
    # Setup Org A and Org B
    admin_a_auth, org_a_id = _register_org("Org A Isolation")
    dp_a_auth, dp_a = _create_staff(admin_a_auth, "DP A", "Delivery Partner")

    admin_b_auth, org_b_id = _register_org("Org B Isolation")
    dp_b_auth, dp_b = _create_staff(admin_b_auth, "DP B", "Delivery Partner")

    db = SessionLocal()
    try:
        cust_a, prod_a = _create_customer_and_product(db, org_a_id)
        cust_b, prod_b = _create_customer_and_product(db, org_b_id)

        # Org A: 3 orders (2 assigned to DP A)
        for i in range(3):
            order_a = SalesOrder(
                organization_id=org_a_id,
                order_number=f"SO-ORGA-{i+1}",
                customer_id=cust_a.id,
                status="confirmed",
                fulfilment_status="planned" if i < 2 else "not_started",
                assigned_delivery_partner_id=dp_a["id"] if i < 2 else None,
                total=100.0,
                subtotal=100.0,
                discount=0,
                tax=0,
                source="direct",
            )
            db.add(order_a)

        # Org B: 7 orders (4 assigned to DP B)
        for j in range(7):
            order_b = SalesOrder(
                organization_id=org_b_id,
                order_number=f"SO-ORGB-{j+1}",
                customer_id=cust_b.id,
                status="confirmed",
                fulfilment_status="planned" if j < 4 else "not_started",
                assigned_delivery_partner_id=dp_b["id"] if j < 4 else None,
                total=200.0,
                subtotal=200.0,
                discount=0,
                tax=0,
                source="direct",
            )
            db.add(order_b)

        db.commit()

        # DP A sees only Org A
        kpi_a = client.get("/dashboard/delivery-partner", headers=dp_a_auth).json()
        assert kpi_a["total_company_orders"] == 3
        assert kpi_a["my_assigned_deliveries"] == 2

        orders_a = client.get("/dashboard/delivery-partner/orders", headers=dp_a_auth).json()
        assert len(orders_a) == 3
        assert all(o["organization_id"] == org_a_id for o in orders_a)

        # DP B sees only Org B
        kpi_b = client.get("/dashboard/delivery-partner", headers=dp_b_auth).json()
        assert kpi_b["total_company_orders"] == 7
        assert kpi_b["my_assigned_deliveries"] == 4

        orders_b = client.get("/dashboard/delivery-partner/orders", headers=dp_b_auth).json()
        assert len(orders_b) == 7
        assert all(o["organization_id"] == org_b_id for o in orders_b)

    finally:
        db.close()


def test_zero_counts():
    admin_auth, org_id = _register_org("Zero Orders Org")
    dp_auth, dp = _create_staff(admin_auth, "DP Zero", "Delivery Partner")

    # Org has 0 orders
    kpi = client.get("/dashboard/delivery-partner", headers=dp_auth).json()
    assert kpi["total_company_orders"] == 0
    assert kpi["my_assigned_deliveries"] == 0

    orders = client.get("/dashboard/delivery-partner/orders", headers=dp_auth).json()
    assert orders == []


def test_security_auth_rbac_and_readonly():
    admin_auth, org_id = _register_org("Security Test Org")
    dp_auth, dp = _create_staff(admin_auth, "DP Sec", "Delivery Partner")

    # Unauthenticated / invalid auth rejected
    r_unauth_kpi = client.get("/dashboard/delivery-partner")
    assert r_unauth_kpi.status_code in (401, 403)

    r_invalid_kpi = client.get("/dashboard/delivery-partner", headers={"Authorization": "Bearer invalid_token"})
    assert r_invalid_kpi.status_code == 401

    r_unauth_orders = client.get("/dashboard/delivery-partner/orders")
    assert r_unauth_orders.status_code in (401, 403)

    r_invalid_orders = client.get("/dashboard/delivery-partner/orders", headers={"Authorization": "Bearer invalid_token"})
    assert r_invalid_orders.status_code == 401

    # Read-only verification: POST / PUT / PATCH / DELETE should return 405 Method Not Allowed
    assert client.post("/dashboard/delivery-partner", headers=dp_auth).status_code == 405
    assert client.put("/dashboard/delivery-partner", headers=dp_auth).status_code == 405
    assert client.delete("/dashboard/delivery-partner", headers=dp_auth).status_code == 405

    assert client.post("/dashboard/delivery-partner/orders", headers=dp_auth).status_code == 405
    assert client.patch("/dashboard/delivery-partner/orders", headers=dp_auth).status_code == 405
    assert client.delete("/dashboard/delivery-partner/orders", headers=dp_auth).status_code == 405

    # Existing GET /orders must still enforce permissions (Delivery Partner lacks sales_orders:view -> 403)
    r_existing_orders = client.get("/orders", headers=dp_auth)
    assert r_existing_orders.status_code == 403


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
