"""Test suite for designation field exposure across GET/list APIs."""

import os
import sys
from datetime import datetime, timezone, date
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.main import app
from app.core.database import SessionLocal, auto_add_missing_columns
from app.models.enums import UserRole
from app.models import (
    Customer,
    CustomerPayment,
    Delivery,
    Invoice,
    Lead,
    Leave,
    Organization,
    Product,
    Quotation,
    Role,
    SalesOrder,
    Team,
    User,
    Vehicle,
    Visit,
    FollowUp,
    Warehouse,
)
from app.core.security import create_access_token, hash_password

client = TestClient(app)


def _auth_headers(user: User) -> dict[str, str]:
    uid = str(user.id)
    role = str(user.system_role or "admin")
    org_id = str(user.organization_id) if user.organization_id else None
    token = create_access_token(uid, role, org_id)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def designation_setup():
    auto_add_missing_columns()
    db = SessionLocal()
    try:
        ts = int(datetime.now(timezone.utc).timestamp())
        org = Organization(name=f"Designation Org {ts}", company_code=f"CMP-{ts % 90000 + 10000}")
        db.add(org)
        db.flush()

        admin = User(
            email=f"admin_desig_{ts}@test.com",
            name="Admin User",
            designation="Chief Executive Officer",
            organization_id=org.id,
            system_role="admin",
            role=UserRole.ADMIN,
            password_hash=hash_password("password123"),
            is_active=True,
        )
        sales_officer = User(
            email=f"sales_desig_{ts}@test.com",
            name="Sales Officer 1",
            designation="Senior Sales Manager",
            organization_id=org.id,
            system_role="staff",
            role=UserRole.SALES_OFFICER,
            password_hash=hash_password("password123"),
            is_active=True,
        )
        delivery_partner = User(
            email=f"delivery_desig_{ts}@test.com",
            name="Delivery Driver 1",
            designation="Lead Fleet Driver",
            organization_id=org.id,
            system_role="staff",
            role=UserRole.DELIVERY_PARTNER,
            password_hash=hash_password("password123"),
            is_active=True,
        )
        unassigned_user = User(
            email=f"no_desig_{ts}@test.com",
            name="No Designation User",
            designation=None,
            organization_id=org.id,
            system_role="staff",
            role=UserRole.SALES_OFFICER,
            password_hash=hash_password("password123"),
            is_active=True,
        )
        db.add_all([admin, sales_officer, delivery_partner, unassigned_user])
        db.flush()

        warehouse = Warehouse(
            name="Designation WH",
            code=f"WH-D-{ts}",
            organization_id=org.id,
            is_default=True,
            is_active=True,
        )
        db.add(warehouse)
        db.flush()

        # Customer with contact designation
        cust1 = Customer(
            name="ABC Enterprises",
            business_name="ABC Corp",
            designation="Purchasing Director",
            primary_contact_person="John Doe",
            assigned_sales_officer_id=sales_officer.id,
            organization_id=org.id,
            opening_balance=0.0,
            total_billed=1000.0,
            total_received=0.0,
            outstanding_balance=1000.0,
            is_active=True,
        )
        cust_null = Customer(
            name="XYZ Retail",
            business_name="XYZ LLC",
            designation=None,
            assigned_sales_officer_id=unassigned_user.id,
            organization_id=org.id,
            opening_balance=0.0,
            total_billed=0.0,
            total_received=0.0,
            outstanding_balance=0.0,
            is_active=True,
        )
        db.add_all([cust1, cust_null])
        db.flush()

        # Sales Order
        order1 = SalesOrder(
            organization_id=org.id,
            customer_id=cust1.id,
            order_number=f"SO-D-{ts}",
            salesperson_id=sales_officer.id,
            assigned_delivery_partner_id=delivery_partner.id,
            created_by=admin.id,
            status="confirmed",
            total=1000.0,
            subtotal=1000.0,
            tax=0.0,
            discount=0.0,
            source="direct",
        )
        db.add(order1)
        db.flush()

        inv1 = Invoice(
            organization_id=org.id,
            customer_id=cust1.id,
            order_id=order1.id,
            invoice_number=f"INV-D-{ts}",
            invoice_date=datetime.now(timezone.utc),
            total=1000.0,
            subtotal=1000.0,
            tax=0.0,
            amount_paid=0.0,
            status="unpaid",
            invoice_status="Issued",
            is_credit_note=False,
        )
        db.add(inv1)
        db.flush()

        # Delivery
        del1 = Delivery(
            organization_id=org.id,
            delivery_note_number=f"DEL-D-{ts}",
            sales_order_id=order1.id,
            customer_id=cust1.id,
            delivery_partner_id=delivery_partner.id,
            warehouse_id=warehouse.id,
            status="planned",
        )
        db.add(del1)
        db.flush()

        # Vehicle
        vehicle1 = Vehicle(
            organization_id=org.id,
            vehicle_number=f"VH-D-{ts}",
            default_driver_id=delivery_partner.id,
            status="active",
        )
        db.add(vehicle1)
        db.flush()

        # Team
        team1 = Team(
            organization_id=org.id,
            name=f"Sales Team {ts}",
            manager_id=admin.id,
        )
        team1.members.extend([admin, sales_officer, delivery_partner])
        db.add(team1)
        db.flush()

        # Quotation
        quot1 = Quotation(
            organization_id=org.id,
            quotation_number=f"QT-D-{ts}",
            customer_id=cust1.id,
            salesperson_id=sales_officer.id,
            status="draft",
        )
        db.add(quot1)

        # Lead
        lead1 = Lead(
            organization_id=org.id,
            name="Prospective Lead",
            customer_id=cust1.id,
            assigned_salesperson_id=sales_officer.id,
            lead_status="new",
        )
        db.add(lead1)

        # Visit
        visit1 = Visit(
            organization_id=org.id,
            user_id=sales_officer.id,
            customer_id=cust1.id,
            visit_type="meeting",
            status="planned",
        )
        db.add(visit1)

        # Follow Up
        fu1 = FollowUp(
            organization_id=org.id,
            title=f"Follow Up {ts}",
            assigned_to_id=sales_officer.id,
            customer_id=cust1.id,
            due_date=datetime.now(timezone.utc),
            status="pending",
        )
        db.add(fu1)

        # Leave
        leave1 = Leave(
            organization_id=org.id,
            user_id=sales_officer.id,
            approved_by=admin.id,
            leave_type="casual",
            start_date=date.today(),
            end_date=date.today(),
            days_count=1.0,
            status="approved",
        )
        db.add(leave1)

        # Customer Payment
        pay1 = CustomerPayment(
            organization_id=org.id,
            customer_id=cust1.id,
            invoice_id=inv1.id,
            order_id=order1.id,
            receipt_number=f"RCPT-D-{ts}",
            amount=200.0,
            payment_mode="cash",
            collected_by_user_id=delivery_partner.id,
        )
        db.add(pay1)

        db.commit()

        yield {
            "db": db,
            "org": org,
            "admin": admin,
            "admin_headers": _auth_headers(admin),
            "sales_officer": sales_officer,
            "delivery_partner": delivery_partner,
            "unassigned_user": unassigned_user,
            "cust1": cust1,
            "cust_null": cust_null,
            "order1": order1,
            "inv1": inv1,
            "del1": del1,
            "vehicle1": vehicle1,
            "team1": team1,
            "quot1": quot1,
            "lead1": lead1,
            "visit1": visit1,
            "fu1": fu1,
            "leave1": leave1,
            "pay1": pay1,
        }
    finally:
        db.close()


def test_users_assignable_and_overview_designation(designation_setup):
    s = designation_setup
    res = client.get("/users/assignable", headers=s["admin_headers"])
    assert res.status_code == 200
    items = res.json()
    assert len(items) >= 1
    for item in items:
        assert "designation" in item
        if item["id"] == s["sales_officer"].id:
            assert item["designation"] == "Senior Sales Manager"

    # Overview
    ov_res = client.get(f"/users/{s['sales_officer'].id}/overview", headers=s["admin_headers"])
    assert ov_res.status_code == 200
    ov_data = ov_res.json()
    assert ov_data["designation"] == "Senior Sales Manager"

    ov_null = client.get(f"/users/{s['unassigned_user'].id}/overview", headers=s["admin_headers"])
    assert ov_null.status_code == 200
    assert ov_null.json()["designation"] is None


def test_customers_designation_and_assignee_brief(designation_setup):
    s = designation_setup
    # List
    list_res = client.get("/customers", headers=s["admin_headers"])
    assert list_res.status_code == 200
    c1 = next(c for c in list_res.json() if c["id"] == s["cust1"].id)
    assert c1["designation"] == "Purchasing Director"
    assert c1["assigned_sales_officer"]["designation"] == "Senior Sales Manager"

    c_null = next(c for c in list_res.json() if c["id"] == s["cust_null"].id)
    assert c_null["designation"] is None
    assert c_null["assigned_sales_officer"]["designation"] is None

    # Detail (GET /customers/{id} returns CustomerProfileOut)
    det_res = client.get(f"/customers/{s['cust1'].id}", headers=s["admin_headers"])
    assert det_res.status_code == 200
    assert det_res.json()["contact_information"]["designation"] == "Purchasing Director"
    assert det_res.json()["sales_crm_information"]["sales_representative"]["designation"] == "Senior Sales Manager"


def test_orders_salesperson_creator_partner_designation(designation_setup):
    s = designation_setup
    res = client.get(f"/orders/{s['order1'].id}", headers=s["admin_headers"])
    assert res.status_code == 200
    data = res.json()
    assert data["salesperson"]["designation"] == "Senior Sales Manager"
    assert data["created_by_user"]["designation"] == "Chief Executive Officer"
    assert data["delivery_partner"]["designation"] == "Lead Fleet Driver"


def test_teams_manager_and_members_designation(designation_setup):
    s = designation_setup
    res = client.get(f"/teams/{s['team1'].id}", headers=s["admin_headers"])
    assert res.status_code == 200
    data = res.json()
    assert data["manager"]["designation"] == "Chief Executive Officer"
    member_map = {m["id"]: m["designation"] for m in data["members"]}
    assert member_map[s["admin"].id] == "Chief Executive Officer"
    assert member_map[s["sales_officer"].id] == "Senior Sales Manager"
    assert member_map[s["delivery_partner"].id] == "Lead Fleet Driver"


def test_delivery_and_vehicle_designation(designation_setup):
    s = designation_setup
    # GET /deliveries/by-id/{id}
    del_res = client.get(f"/deliveries/by-id/{s['del1'].id}", headers=s["admin_headers"])
    assert del_res.status_code == 200
    assert del_res.json()["delivery_partner"]["designation"] == "Lead Fleet Driver"

    # GET /deliveries list
    del_list = client.get("/deliveries", headers=s["admin_headers"])
    assert del_list.status_code == 200
    d1 = next(d for d in del_list.json() if d["id"] == s["del1"].id)
    assert d1["delivery_partner"]["designation"] == "Lead Fleet Driver"

    veh_res = client.get(f"/vehicles/{s['vehicle1'].id}", headers=s["admin_headers"])
    assert veh_res.status_code == 200
    assert veh_res.json()["assigned_delivery_partner"]["designation"] == "Lead Fleet Driver"


def test_crm_quotations_leads_visits_followups_leaves_designation(designation_setup):
    s = designation_setup

    q_res = client.get(f"/quotations/{s['quot1'].id}", headers=s["admin_headers"])
    assert q_res.status_code == 200
    assert q_res.json()["salesperson"]["designation"] == "Senior Sales Manager"

    ld_res = client.get(f"/leads/{s['lead1'].id}", headers=s["admin_headers"])
    assert ld_res.status_code == 200
    assert ld_res.json()["assigned_salesperson"]["designation"] == "Senior Sales Manager"

    vs_res = client.get(f"/visits/{s['visit1'].id}", headers=s["admin_headers"])
    assert vs_res.status_code == 200
    assert vs_res.json()["user"]["designation"] == "Senior Sales Manager"

    fu_res = client.get(f"/follow-ups/{s['fu1'].id}", headers=s["admin_headers"])
    assert fu_res.status_code == 200
    assert fu_res.json()["assigned_to"]["designation"] == "Senior Sales Manager"

    lv_res = client.get(f"/leaves/{s['leave1'].id}", headers=s["admin_headers"])
    assert lv_res.status_code == 200
    assert lv_res.json()["user"]["designation"] == "Senior Sales Manager"
    assert lv_res.json()["approver"]["designation"] == "Chief Executive Officer"


def test_payment_and_receipt_collector_designation(designation_setup):
    s = designation_setup
    pay_res = client.get(f"/customers/{s['cust1'].id}/payments", headers=s["admin_headers"])
    assert pay_res.status_code == 200
    assert pay_res.json()[0]["collector"]["designation"] == "Lead Fleet Driver"

    rcpt_res = client.get("/payment-receipts", headers=s["admin_headers"])
    assert rcpt_res.status_code == 200
    r1 = next(r for r in rcpt_res.json() if r["id"] == s["pay1"].id)
    assert r1["collector"]["designation"] == "Lead Fleet Driver"
