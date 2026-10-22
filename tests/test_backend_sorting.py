import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.security import hash_password
from app.models import (
    Attendance,
    CashReconciliation,
    Customer,
    Delivery,
    DeliveryCollection,
    Expense,
    FollowUp,
    GoodsReceiptNote,
    Invoice,
    Lead,
    Leave,
    Notification,
    Organization,
    OrganizationStatus,
    Plan,
    Product,
    ProductVariant,
    PurchaseInvoice,
    PurchaseReturn,
    Quotation,
    Role,
    SalesOrder,
    SalesReturn,
    SubscriptionPayment,
    Supplier,
    SupplierInvoice,
    SupplierPayment,
    SystemRole,
    Team,
    User,
    UserRole,
    Vehicle,
    VehicleLoading,
    Visit,
    Warehouse,
    WarehouseStock,
    WarehouseTransfer,
)

client = TestClient(app)


@pytest.fixture(scope="module")
def setup_test_data():
    db = next(get_db())

    # 1. Setup Plan
    from app.core.entitlements import ALL_ENTITLEMENT_KEYS
    plan = db.query(Plan).filter(Plan.name == "Sorting Test Plan").first()
    if not plan:
        plan = Plan(
            name="Sorting Test Plan",
            price_monthly=999,
            price_yearly=9999,
            is_active=True,
            entitlements={k: True for k in ALL_ENTITLEMENT_KEYS},
        )
        db.add(plan)
        db.flush()

    # 2. Setup Organizations (Org A and Org B for tenant isolation)
    org_a = db.query(Organization).filter(Organization.company_code == "CMP-SORT-A").first()
    if not org_a:
        org_a = Organization(
            name="Sorting Org A",
            company_code="CMP-SORT-A",
            status=OrganizationStatus.ACTIVE.value,
            plan_id=plan.id,
        )
        db.add(org_a)
        db.flush()

    org_b = db.query(Organization).filter(Organization.company_code == "CMP-SORT-B").first()
    if not org_b:
        org_b = Organization(
            name="Sorting Org B",
            company_code="CMP-SORT-B",
            status=OrganizationStatus.ACTIVE.value,
            plan_id=plan.id,
        )
        db.add(org_b)
        db.flush()

    # 3. Setup Users with valid RFC email domains
    db.query(User).filter(User.organization_id.in_([org_a.id, org_b.id])).delete()
    db.commit()

    user_a = User(
        email="admin@sorting-a.example.com",
        name="Admin A",
        password_hash=hash_password("password123"),
        organization_id=org_a.id,
        system_role=SystemRole.ADMIN.value,
        is_active=True,
    )
    db.add(user_a)
    db.flush()

    user_b = db.query(User).filter(User.email == "admin@sorting-b.example.com").first()
    if not user_b:
        user_b = User(
            email="admin@sorting-b.example.com",
            name="Admin B",
            password_hash=hash_password("password123"),
            organization_id=org_b.id,
            system_role=SystemRole.ADMIN.value,
            is_active=True,
        )
        db.add(user_b)
        db.flush()

    super_admin = db.query(User).filter(User.email == "superadmin@sorting.example.com").first()
    if not super_admin:
        super_admin = User(
            email="superadmin@sorting.example.com",
            name="Super Admin",
            password_hash=hash_password("password123"),
            organization_id=None,
            system_role=SystemRole.SUPER_ADMIN.value,
            role=UserRole.SUPER_ADMIN,
            is_active=True,
        )
        db.add(super_admin)
        db.flush()

    # Setup Warehouse for Org A
    wh = db.query(Warehouse).filter(Warehouse.organization_id == org_a.id).first()
    if not wh:
        wh = Warehouse(
            organization_id=org_a.id,
            name="Main Warehouse A",
            code="WH-MAIN-A",
            is_active=True,
        )
        db.add(wh)
        db.flush()

    db.commit()

    return {
        "db": db,
        "org_a": org_a,
        "org_b": org_b,
        "user_a": user_a,
        "user_b": user_b,
        "super_admin": super_admin,
        "warehouse": wh,
        "plan": plan,
    }


def test_customers_sorting_and_validation(setup_test_data):
    db: Session = setup_test_data["db"]
    org_a = setup_test_data["org_a"]
    org_b = setup_test_data["org_b"]
    user_a = setup_test_data["user_a"]

    app.dependency_overrides[get_current_user] = lambda: user_a

    # Clean existing customers
    db.query(Customer).filter(Customer.organization_id.in_([org_a.id, org_b.id])).delete()
    db.commit()

    # Seed 15 customers in Org A with deterministic names & balances
    for i in range(15):
        char = chr(ord('A') + i)
        c = Customer(
            organization_id=org_a.id,
            customer_id=f"CUST-{i:03d}",
            name=f"Customer {char}",
            email=f"cust_{char.lower()}@example.com",
            phone=f"+919876543{i:03d}",
            outstanding_balance=float(100 * (15 - i)),  # 1500, 1400, ..., 100
            created_at=datetime.now(timezone.utc) - timedelta(days=15 - i),
            is_active=True,
        )
        db.add(c)

    # Seed 3 customers in Org B
    for i in range(3):
        c = Customer(
            organization_id=org_b.id,
            customer_id=f"CUST-B-{i:03d}",
            name=f"Customer OrgB {i}",
            email=f"cust_b_{i}@example.com",
            phone=f"+918876543{i:03d}",
            is_active=True,
        )
        db.add(c)
    db.commit()

    # 1. Test Ascending sort by name
    res_asc = client.get("/customers?sort_by=name&sort_order=asc&page_size=10")
    assert res_asc.status_code == 200, res_asc.text
    data_asc = res_asc.json()
    assert data_asc["total"] == 15
    names_asc = [item["name"] for item in data_asc["items"]]
    assert names_asc == sorted(names_asc)
    assert names_asc[0] == "Customer A"

    # 2. Test Descending sort by name
    res_desc = client.get("/customers?sort_by=name&sort_order=desc&page_size=10")
    assert res_desc.status_code == 200
    data_desc = res_desc.json()
    names_desc = [item["name"] for item in data_desc["items"]]
    assert names_desc == sorted(names_desc, reverse=True)
    assert names_desc[0] == "Customer O"

    # 3. Test Sorting with pagination across page boundaries (page 1 vs page 2)
    p1 = client.get("/customers?sort_by=name&sort_order=asc&page=1&page_size=10").json()
    p2 = client.get("/customers?sort_by=name&sort_order=asc&page=2&page_size=10").json()
    assert len(p1["items"]) == 10
    assert len(p2["items"]) == 5
    assert p1["items"][-1]["name"] < p2["items"][0]["name"]

    # 4. Test Sorting by numeric field (outstanding_balance desc)
    res_bal = client.get("/customers?sort_by=outstanding_balance&sort_order=desc&page_size=5")
    assert res_bal.status_code == 200
    bals = [item["outstanding_balance"] for item in res_bal.json()["items"]]
    assert bals == sorted(bals, reverse=True)
    assert bals[0] == 1500.0

    # 5. Whitelist validation: invalid sort_by
    res_inv_field = client.get("/customers?sort_by=unsupported_field_xyz")
    assert res_inv_field.status_code == 400
    assert "Invalid sort_by 'unsupported_field_xyz'" in res_inv_field.json()["detail"]
    assert "Allowed fields" in res_inv_field.json()["detail"]

    # 6. Whitelist validation: invalid sort_order
    res_inv_order = client.get("/customers?sort_by=name&sort_order=sideways")
    assert res_inv_order.status_code == 400
    assert "Invalid sort_order 'sideways'" in res_inv_order.json()["detail"]

    # 7. Tenant isolation: Org A cannot see Org B customers
    all_names = [item["name"] for item in client.get("/customers?page_size=50").json()["items"]]
    assert not any("OrgB" in n for n in all_names)


def test_leads_sorting_and_search(setup_test_data):
    db: Session = setup_test_data["db"]
    org_a = setup_test_data["org_a"]
    user_a = setup_test_data["user_a"]

    app.dependency_overrides[get_current_user] = lambda: user_a

    db.query(Lead).filter(Lead.organization_id == org_a.id).delete()
    db.commit()

    leads_data = [
        ("Alpha Lead", "new", "web"),
        ("Beta Lead", "contacted", "phone"),
        ("Gamma Lead", "qualified", "referral"),
        ("Delta Lead", "new", "web"),
    ]
    for name, st, src in leads_data:
        lead = Lead(
            organization_id=org_a.id,
            name=name,
            lead_status=st,
            lead_source=src,
            mobile_number="+919876543210",
        )
        db.add(lead)
    db.commit()

    # Sort by name asc
    res = client.get("/leads?sort_by=name&sort_order=asc")
    assert res.status_code == 200
    items = res.json()["items"]
    assert [i["name"] for i in items] == ["Alpha Lead", "Beta Lead", "Delta Lead", "Gamma Lead"]

    # Sort by name desc
    res_desc = client.get("/leads?sort_by=name&sort_order=desc")
    assert res_desc.status_code == 200
    items_desc = res_desc.json()["items"]
    assert [i["name"] for i in items_desc] == ["Gamma Lead", "Delta Lead", "Beta Lead", "Alpha Lead"]

    # Combine search + filter + sorting
    res_search = client.get("/leads?search=Lead&status=new&sort_by=name&sort_order=desc")
    assert res_search.status_code == 200
    search_items = res_search.json()["items"]
    assert len(search_items) == 2
    assert search_items[0]["name"] == "Delta Lead"
    assert search_items[1]["name"] == "Alpha Lead"

    # Invalid sort_by returns 400
    res_err = client.get("/leads?sort_by=malicious_col")
    assert res_err.status_code == 400


def test_quotations_sorting(setup_test_data):
    db: Session = setup_test_data["db"]
    org_a = setup_test_data["org_a"]
    user_a = setup_test_data["user_a"]

    app.dependency_overrides[get_current_user] = lambda: user_a

    customer = db.query(Customer).filter(Customer.organization_id == org_a.id).first()
    db.query(Quotation).filter(Quotation.organization_id == org_a.id).delete()
    db.commit()

    now = datetime.now(timezone.utc)
    for i in range(3):
        q = Quotation(
            organization_id=org_a.id,
            quotation_number=f"QT-2026-{100 + i}",
            customer_id=customer.id,
            quotation_date=now - timedelta(days=10 - i),
            status="draft",
            salesperson_id=user_a.id,
        )
        db.add(q)
    db.commit()

    res_asc = client.get("/quotations?sort_by=quotation_number&sort_order=asc")
    assert res_asc.status_code == 200, res_asc.text
    assert [x["quotation_number"] for x in res_asc.json()["items"]] == [
        "QT-2026-100", "QT-2026-101", "QT-2026-102"
    ]

    res_desc = client.get("/quotations?sort_by=quotation_number&sort_order=desc")
    assert res_desc.status_code == 200
    assert [x["quotation_number"] for x in res_desc.json()["items"]] == [
        "QT-2026-102", "QT-2026-101", "QT-2026-100"
    ]

    # Verify invalid sort_by is rejected with 400
    res_invalid = client.get("/quotations?sort_by=invalid_field")
    assert res_invalid.status_code == 400
    assert "Invalid sort_by 'invalid_field'" in res_invalid.json()["detail"]


def test_products_sorting(setup_test_data):
    db: Session = setup_test_data["db"]
    org_a = setup_test_data["org_a"]
    user_a = setup_test_data["user_a"]

    app.dependency_overrides[get_current_user] = lambda: user_a

    db.query(Product).filter(Product.organization_id == org_a.id).delete()
    db.commit()

    p_data = [
        ("Widget A", "SKU-A", 150.0),
        ("Widget B", "SKU-B", 50.0),
        ("Widget C", "SKU-C", 200.0),
    ]
    for name, sku, price in p_data:
        p = Product(
            organization_id=org_a.id,
            name=name,
            sku=sku,
            price=price,
            is_active=True,
        )
        db.add(p)
    db.commit()

    # Sort by price asc
    res = client.get("/products?sort_by=price&sort_order=asc")
    assert res.status_code == 200
    assert [x["price"] for x in res.json()["items"]] == [50.0, 150.0, 200.0]

    # Sort by price desc
    res_desc = client.get("/products?sort_by=price&sort_order=desc")
    assert res_desc.status_code == 200
    assert [x["price"] for x in res_desc.json()["items"]] == [200.0, 150.0, 50.0]

    # Sort by name asc
    res_name = client.get("/products?sort_by=name&sort_order=asc")
    assert res_name.status_code == 200
    assert [x["name"] for x in res_name.json()["items"]] == ["Widget A", "Widget B", "Widget C"]


def test_suppliers_sorting(setup_test_data):
    db: Session = setup_test_data["db"]
    org_a = setup_test_data["org_a"]
    user_a = setup_test_data["user_a"]

    app.dependency_overrides[get_current_user] = lambda: user_a

    db.query(SupplierInvoice).filter(SupplierInvoice.organization_id == org_a.id).delete()
    db.query(PurchaseInvoice).filter(PurchaseInvoice.organization_id == org_a.id).delete()
    db.query(Supplier).filter(Supplier.organization_id == org_a.id).delete()
    db.commit()

    for i in range(3):
        sup = Supplier(
            organization_id=org_a.id,
            name=f"Supplier {chr(ord('Z') - i)}",
            company_name=f"Company {chr(ord('Z') - i)}",
            email=f"sup_{i}@example.com",
            phone=f"+919876500{i:03d}",
        )
        db.add(sup)
    db.commit()

    res_asc = client.get("/suppliers?sort_by=name&sort_order=asc")
    assert res_asc.status_code == 200
    names = [x["name"] for x in res_asc.json()["items"]]
    assert names == ["Supplier X", "Supplier Y", "Supplier Z"]


def test_sales_orders_sorting(setup_test_data):
    db: Session = setup_test_data["db"]
    org_a = setup_test_data["org_a"]
    user_a = setup_test_data["user_a"]

    app.dependency_overrides[get_current_user] = lambda: user_a

    customer = db.query(Customer).filter(Customer.organization_id == org_a.id).first()
    db.query(SalesOrder).filter(SalesOrder.organization_id == org_a.id).delete()
    db.commit()

    order_data = [
        ("SO-101", 1000.0, date(2026, 2, 1)),
        ("SO-102", 500.0, date(2026, 2, 5)),
        ("SO-103", 2500.0, date(2026, 2, 3)),
    ]
    for num, total, o_date in order_data:
        so = SalesOrder(
            organization_id=org_a.id,
            order_number=num,
            customer_id=customer.id,
            order_date=o_date,
            total=total,
            status="pending_approval",
            created_by=user_a.id,
        )
        db.add(so)
    db.commit()

    # Sort by total asc
    res = client.get("/orders?sort_by=total&sort_order=asc")
    assert res.status_code == 200
    assert [x["total"] for x in res.json()["items"]] == [500.0, 1000.0, 2500.0]

    # Sort by order_number desc
    res_num = client.get("/orders?sort_by=order_number&sort_order=desc")
    assert res_num.status_code == 200
    assert [x["order_number"] for x in res_num.json()["items"]] == ["SO-103", "SO-102", "SO-101"]


def test_invoices_receivables_sorting(setup_test_data):
    db: Session = setup_test_data["db"]
    org_a = setup_test_data["org_a"]
    user_a = setup_test_data["user_a"]

    app.dependency_overrides[get_current_user] = lambda: user_a

    customer = db.query(Customer).filter(Customer.organization_id == org_a.id).first()
    db.query(Invoice).filter(Invoice.organization_id == org_a.id).delete()
    db.commit()

    now = datetime.now(timezone.utc)
    inv_data = [
        ("INV-001", 3000.0, 2000.0, now - timedelta(days=10)),
        ("INV-002", 1500.0, 0.0, now - timedelta(days=5)),
        ("INV-003", 5000.0, 4800.0, now - timedelta(days=1)),
    ]
    for num, total, paid, idate in inv_data:
        inv = Invoice(
            organization_id=org_a.id,
            invoice_number=num,
            customer_id=customer.id,
            invoice_date=idate,
            due_date=idate + timedelta(days=15),
            total=total,
            amount_paid=paid,
            status="unpaid" if paid < total else "paid",
            created_by=user_a.id,
        )
        db.add(inv)
    db.commit()

    # Sort by total desc
    res_total = client.get("/invoices?sort_by=total&sort_order=desc")
    assert res_total.status_code == 200, res_total.text
    assert [x["total"] for x in res_total.json()["items"]] == [5000.0, 3000.0, 1500.0]

    # Sort by outstanding_amount asc (INV-003 has 200.0, INV-001 has 1000.0, INV-002 has 1500.0)
    res_due = client.get("/invoices?sort_by=outstanding_amount&sort_order=asc")
    assert res_due.status_code == 200
    outstandings = [x["total"] - x["amount_paid"] for x in res_due.json()["items"]]
    assert outstandings == [200.0, 1000.0, 1500.0]

    # Invalid sort_by returns 400
    res_inv = client.get("/invoices?sort_by=non_existent_column")
    assert res_inv.status_code == 400


def test_accounts_payable_sorting_and_global_aging_summary(setup_test_data):
    """Verify accounts payable sorting at database level while preserving global aging summary!"""
    db: Session = setup_test_data["db"]
    org_a = setup_test_data["org_a"]
    user_a = setup_test_data["user_a"]
    wh = setup_test_data["warehouse"]

    app.dependency_overrides[get_current_user] = lambda: user_a

    supplier = db.query(Supplier).filter(Supplier.organization_id == org_a.id).first()
    db.query(SupplierInvoice).filter(SupplierInvoice.organization_id == org_a.id).delete()
    db.commit()

    # Create baseline purchase invoice
    purchase = db.query(PurchaseInvoice).filter(PurchaseInvoice.organization_id == org_a.id).first()
    if not purchase:
        purchase = PurchaseInvoice(
            organization_id=org_a.id,
            invoice_number=f"PO-TEST-{uuid.uuid4().hex[:6]}",
            supplier_id=supplier.id,
            warehouse_id=wh.id,
            total=25000.0,
            status="confirmed",
        )
        db.add(purchase)
        db.flush()

    now = datetime.now(timezone.utc)
    invoices = [
        ("SINV-10", 1000.0, now - timedelta(days=5)),   # 1-30 days overdue
        ("SINV-20", 2000.0, now - timedelta(days=35)),  # 31-60 days overdue
        ("SINV-30", 5000.0, now + timedelta(days=10)),  # current / not overdue
        ("SINV-40", 1500.0, now - timedelta(days=65)),  # 61-90 days overdue
        ("SINV-50", 3000.0, now - timedelta(days=95)),  # 90+ days overdue
    ]
    for num, amt, ddate in invoices:
        sinv = SupplierInvoice(
            organization_id=org_a.id,
            supplier_id=supplier.id,
            purchase_id=purchase.id,
            supplier_invoice_number=num,
            supplier_invoice_date=ddate - timedelta(days=15),
            due_date=ddate,
            grand_total=amt,
            amount_paid=0.0,
            return_amount=0.0,
            status="recorded",
            payment_status="unpaid",
        )
        db.add(sinv)
    db.commit()

    # 1. Paginated page of 2 items, sorted by grand_total asc
    res = client.get("/accounts-payable?sort_by=grand_total&sort_order=asc&page=1&page_size=2")
    assert res.status_code == 200, res.text
    data = res.json()

    # Check page items are sorted and sliced
    assert len(data["items"]) == 2
    assert data["items"][0]["supplier_invoice_number"] == "SINV-10"
    assert data["items"][0]["outstanding_amount"] == 1000.0
    assert data["items"][1]["supplier_invoice_number"] == "SINV-40"
    assert data["items"][1]["outstanding_amount"] == 1500.0

    # CRITICAL: Check that global summary reflects ALL 5 invoices (total = 12500), NOT just the 2 on this page!
    summary = data["summary"]
    assert summary["total_payable"] == 12500.0
    assert summary["open_invoice_count"] == 5

    ageing = data["ageing"]
    assert ageing["bucket_0_30"] == 1000.0
    assert ageing["bucket_31_60"] == 2000.0
    assert ageing["bucket_61_90"] == 1500.0
    assert ageing["bucket_90_plus"] == 3000.0

    # 2. Page 2 returns the next sorted items
    res_p2 = client.get("/accounts-payable?sort_by=grand_total&sort_order=asc&page=2&page_size=2")
    assert res_p2.status_code == 200
    p2_data = res_p2.json()
    assert len(p2_data["items"]) == 2
    assert p2_data["items"][0]["outstanding_amount"] == 2000.0
    assert p2_data["items"][1]["outstanding_amount"] == 3000.0
    # Global summary still identical on page 2!
    assert p2_data["summary"]["total_payable"] == 12500.0


def test_expenses_sorting(setup_test_data):
    db: Session = setup_test_data["db"]
    org_a = setup_test_data["org_a"]
    user_a = setup_test_data["user_a"]

    app.dependency_overrides[get_current_user] = lambda: user_a

    db.query(Expense).filter(Expense.organization_id == org_a.id).delete()
    db.commit()

    for i in range(3):
        exp = Expense(
            organization_id=org_a.id,
            category="Office Expenses",
            payee_name=f"Vendor {chr(ord('A') + i)}",
            amount=float(100 * (i + 1)),
            expense_date=datetime.now(timezone.utc) - timedelta(days=i),
            status="approved",
            submitted_by=user_a.id,
        )
        db.add(exp)
    db.commit()

    res_desc = client.get("/expenses?sort_by=amount&sort_order=desc")
    assert res_desc.status_code == 200, res_desc.text
    assert [x["amount"] for x in res_desc.json()["items"]] == [300.0, 200.0, 100.0]


def test_deliveries_and_vehicles_sorting(setup_test_data):
    db: Session = setup_test_data["db"]
    org_a = setup_test_data["org_a"]
    user_a = setup_test_data["user_a"]

    app.dependency_overrides[get_current_user] = lambda: user_a

    db.query(Vehicle).filter(Vehicle.organization_id == org_a.id).delete()
    db.commit()

    for i in range(3):
        v = Vehicle(
            organization_id=org_a.id,
            vehicle_number=f"MH12-AB-{1000 + i}",
            vehicle_type=f"Van {chr(ord('A') + i)}",
            capacity_kg=1000.0,
            status="active",
            is_active=True,
        )
        db.add(v)
    db.commit()

    res_v = client.get("/vehicles?sort_by=vehicle_number&sort_order=desc")
    assert res_v.status_code == 200, res_v.text
    assert [x["vehicle_number"] for x in res_v.json()["items"]] == [
        "MH12-AB-1002", "MH12-AB-1001", "MH12-AB-1000"
    ]


def test_staff_users_sorting(setup_test_data):
    user_a = setup_test_data["user_a"]
    app.dependency_overrides[get_current_user] = lambda: user_a

    res_asc = client.get("/users?sort_by=name&sort_order=asc")
    assert res_asc.status_code == 200, res_asc.text
    items = res_asc.json()["items"]
    names = [x["name"] for x in items]
    assert names == sorted(names)

    # Invalid sort_by returns 400
    res_bad = client.get("/users?sort_by=invalid_col")
    assert res_bad.status_code == 400
    assert "Invalid sort_by" in res_bad.json()["detail"]


def test_attendance_and_leaves_sorting(setup_test_data):
    db: Session = setup_test_data["db"]
    org_a = setup_test_data["org_a"]
    user_a = setup_test_data["user_a"]

    app.dependency_overrides[get_current_user] = lambda: user_a

    db.query(Attendance).filter(Attendance.organization_id == org_a.id).delete()
    db.commit()

    for i in range(3):
        att = Attendance(
            organization_id=org_a.id,
            user_id=user_a.id,
            day=date(2026, 5, 10 + i),
        )
        db.add(att)
    db.commit()

    res_att = client.get("/attendance?sort_by=date&sort_order=desc")
    assert res_att.status_code == 200, res_att.text
    assert [x["date"] for x in res_att.json()["items"]] == ["2026-05-12", "2026-05-11", "2026-05-10"]


def test_superadmin_organizations_and_payments_sorting(setup_test_data):
    super_admin = setup_test_data["super_admin"]
    app.dependency_overrides[get_current_user] = lambda: super_admin

    res_orgs = client.get("/superadmin/organizations?sort_by=name&sort_order=asc")
    assert res_orgs.status_code == 200, res_orgs.text
    org_names = [x["name"] for x in res_orgs.json()["items"]]
    assert org_names == sorted(org_names)

    # Invalid sort_by returns 400
    res_bad = client.get("/superadmin/organizations?sort_by=non_existent")
    assert res_bad.status_code == 400


def test_notifications_sorting(setup_test_data):
    db: Session = setup_test_data["db"]
    org_a = setup_test_data["org_a"]
    user_a = setup_test_data["user_a"]

    app.dependency_overrides[get_current_user] = lambda: user_a

    db.query(Notification).filter(Notification.user_id == user_a.id).delete()
    db.commit()

    now = datetime.now(timezone.utc)
    for i in range(3):
        notif = Notification(
            user_id=user_a.id,
            organization_id=org_a.id,
            title=f"Notification {chr(ord('A') + i)}",
            created_at=now - timedelta(minutes=10 - i),
        )
        db.add(notif)
    db.commit()

    res = client.get("/notifications?sort_by=created_at&sort_order=desc")
    assert res.status_code == 200, res.text
    titles = [x["title"] for x in res.json()["items"]]
    assert titles == ["Notification C", "Notification B", "Notification A"]


def test_visits_and_followups_sorting(setup_test_data):
    db: Session = setup_test_data["db"]
    org_a = setup_test_data["org_a"]
    user_a = setup_test_data["user_a"]

    app.dependency_overrides[get_current_user] = lambda: user_a

    customer = db.query(Customer).filter(Customer.organization_id == org_a.id).first()
    db.query(FollowUp).filter(FollowUp.organization_id == org_a.id).delete()
    db.query(Visit).filter(Visit.organization_id == org_a.id).delete()
    db.commit()

    now = datetime.now(timezone.utc)
    for i in range(3):
        visit = Visit(
            organization_id=org_a.id,
            customer_id=customer.id,
            user_id=user_a.id,
            visit_date=now - timedelta(days=10 - i),
            purpose=f"Visit {chr(ord('A') + i)}",
        )
        db.add(visit)

        fu = FollowUp(
            organization_id=org_a.id,
            customer_id=customer.id,
            due_date=now + timedelta(days=i + 1),
            title=f"Followup {chr(ord('A') + i)}",
            status="pending",
        )
        db.add(fu)
    db.commit()

    # Sort visits by visit_date asc
    res_v = client.get("/visits?sort_by=visit_date&sort_order=asc")
    assert res_v.status_code == 200, res_v.text
    purposes = [x["purpose"] for x in res_v.json()["items"]]
    assert purposes == ["Visit A", "Visit B", "Visit C"]

    # Sort followups by due_date desc
    res_fu = client.get("/follow-ups?sort_by=due_date&sort_order=desc")
    assert res_fu.status_code == 200, res_fu.text
    titles = [x["title"] for x in res_fu.json()["items"]]
    assert titles == ["Followup C", "Followup B", "Followup A"]


def test_cash_reconciliation_sorting(setup_test_data):
    db: Session = setup_test_data["db"]
    org_a = setup_test_data["org_a"]
    user_a = setup_test_data["user_a"]

    app.dependency_overrides[get_current_user] = lambda: user_a

    db.query(CashReconciliation).filter(CashReconciliation.organization_id == org_a.id).delete()
    db.commit()

    for i in range(3):
        cr = CashReconciliation(
            organization_id=org_a.id,
            reconciliation_date=date(2026, 7, 10 + i),
            status="closed",
        )
        db.add(cr)
    db.commit()

    res = client.get("/cash-reconciliation?sort_by=reconciliation_date&sort_order=desc")
    assert res.status_code == 200, res.text
    dates = [x["reconciliation_date"] for x in res.json()["items"]]
    assert dates == ["2026-07-12", "2026-07-11", "2026-07-10"]


def test_gap_fields_customers_and_leads(setup_test_data):
    db: Session = setup_test_data["db"]
    org_a = setup_test_data["org_a"]
    user_a = setup_test_data["user_a"]

    app.dependency_overrides[get_current_user] = lambda: user_a

    # Customers: code, contact_person, balance, sales_officer
    res = client.get("/customers?sort_by=contact_person&sort_order=asc")
    assert res.status_code == 200, res.text

    res = client.get("/customers?sort_by=code&sort_order=desc")
    assert res.status_code == 200, res.text

    res = client.get("/customers?sort_by=balance&sort_order=desc")
    assert res.status_code == 200, res.text

    res = client.get("/customers?sort_by=sales_officer&sort_order=asc")
    assert res.status_code == 200, res.text

    # Leads: company, company_name, contact_person, assigned_sales_officer, next_follow_up
    res = client.get("/leads?sort_by=company&sort_order=asc")
    assert res.status_code == 200, res.text

    res = client.get("/leads?sort_by=company_name&sort_order=desc")
    assert res.status_code == 200, res.text

    res = client.get("/leads?sort_by=contact_person&sort_order=asc")
    assert res.status_code == 200, res.text

    res = client.get("/leads?sort_by=assigned_sales_officer&sort_order=asc")
    assert res.status_code == 200, res.text

    res = client.get("/leads?sort_by=next_follow_up&sort_order=asc")
    assert res.status_code == 200, res.text


def test_gap_fields_quotations_and_products(setup_test_data):
    db: Session = setup_test_data["db"]
    org_a = setup_test_data["org_a"]
    user_a = setup_test_data["user_a"]

    app.dependency_overrides[get_current_user] = lambda: user_a

    # Quotations: customer, prospect, date, amount, total
    res = client.get("/quotations?sort_by=amount&sort_order=desc")
    assert res.status_code == 200, res.text

    res = client.get("/quotations?sort_by=total&sort_order=asc")
    assert res.status_code == 200, res.text

    res = client.get("/quotations?sort_by=customer&sort_order=asc")
    assert res.status_code == 200, res.text

    res = client.get("/quotations?sort_by=date&sort_order=desc")
    assert res.status_code == 200, res.text

    # Products & Inventory: available_stock, reserved_stock, total_stock
    res = client.get("/products?sort_by=available_stock&sort_order=desc")
    assert res.status_code == 200, res.text

    res = client.get("/products?sort_by=reserved_stock&sort_order=asc")
    assert res.status_code == 200, res.text

    res = client.get("/products?sort_by=total_stock&sort_order=desc")
    assert res.status_code == 200, res.text

    res = client.get("/inventory?sort_by=available_stock&sort_order=desc")
    assert res.status_code == 200, res.text

    res = client.get("/inventory?sort_by=reorder_level&sort_order=asc")
    assert res.status_code == 200, res.text

    # Warehouses stock
    res = client.get("/warehouses/stock?sort_by=on_hand&sort_order=desc")
    assert res.status_code == 200, res.text

    res = client.get("/warehouses/stock?sort_by=available&sort_order=desc")
    assert res.status_code == 200, res.text


def test_gap_fields_purchases_orders_deliveries(setup_test_data):
    db: Session = setup_test_data["db"]
    org_a = setup_test_data["org_a"]
    user_a = setup_test_data["user_a"]

    app.dependency_overrides[get_current_user] = lambda: user_a

    # Suppliers: outstanding_balance, balance
    res = client.get("/suppliers?sort_by=outstanding_balance&sort_order=desc")
    assert res.status_code == 200, res.text

    res = client.get("/suppliers?sort_by=balance&sort_order=asc")
    assert res.status_code == 200, res.text

    # Purchases: supplier_name, date
    res = client.get("/purchases?sort_by=supplier_name&sort_order=asc")
    assert res.status_code == 200, res.text

    res = client.get("/purchases?sort_by=date&sort_order=desc")
    assert res.status_code == 200, res.text

    # Purchase returns: supplier_name, date, purchase_number, amount
    res = client.get("/purchase-returns?sort_by=supplier_name&sort_order=asc")
    assert res.status_code == 200, res.text

    res = client.get("/purchase-returns?sort_by=date&sort_order=desc")
    assert res.status_code == 200, res.text

    res = client.get("/purchase-returns?sort_by=purchase_number&sort_order=asc")
    assert res.status_code == 200, res.text

    # Sales Orders: delivery_partner, vehicle, date
    res = client.get("/orders?sort_by=delivery_partner&sort_order=asc")
    assert res.status_code == 200, res.text

    res = client.get("/orders?sort_by=vehicle&sort_order=asc")
    assert res.status_code == 200, res.text

    res = client.get("/orders?sort_by=date&sort_order=desc")
    assert res.status_code == 200, res.text

    # Deliveries: sales_officer, date
    res = client.get("/deliveries?sort_by=sales_officer&sort_order=asc")
    assert res.status_code == 200, res.text

    res = client.get("/deliveries?sort_by=date&sort_order=desc")
    assert res.status_code == 200, res.text

    # Vehicle Stock: vehicle_number, updated_time, session_date
    res = client.get("/vehicle-stock?sort_by=vehicle_number&sort_order=asc")
    assert res.status_code == 200, res.text

    res = client.get("/vehicle-stock?sort_by=updated_time&sort_order=desc")
    assert res.status_code == 200, res.text

    # Verify 400 for item-only field on vehicle stock sessions
    res_bad = client.get("/vehicle-stock?sort_by=sku&sort_order=asc")
    assert res_bad.status_code == 400, res_bad.text
    assert "Invalid sort_by 'sku'" in res_bad.json()["detail"]


def test_gap_fields_finance_and_hr(setup_test_data):
    db: Session = setup_test_data["db"]
    org_a = setup_test_data["org_a"]
    user_a = setup_test_data["user_a"]

    app.dependency_overrides[get_current_user] = lambda: user_a

    # Supplier Payments: unallocated_amount, outstanding_amount
    res = client.get("/supplier-payments?sort_by=unallocated_amount&sort_order=desc")
    assert res.status_code == 200, res.text

    res = client.get("/supplier-payments?sort_by=outstanding_amount&sort_order=desc")
    assert res.status_code == 200, res.text

    # Delivery Collections: collector, business_date
    res = client.get("/deliveries/collections?sort_by=collector&sort_order=asc")
    assert res.status_code == 200, res.text

    res = client.get("/deliveries/collections?sort_by=business_date&sort_order=desc")
    assert res.status_code == 200, res.text

    # Cash Reconciliation: amount, created_by, closing_cash
    res = client.get("/cash-reconciliation?sort_by=amount&sort_order=desc")
    assert res.status_code == 200, res.text

    res = client.get("/cash-reconciliation?sort_by=created_by&sort_order=asc")
    assert res.status_code == 200, res.text

    res = client.get("/cash-reconciliation?sort_by=closing_cash&sort_order=desc")
    assert res.status_code == 200, res.text

    # Attendance: employee_name, name
    res = client.get("/attendance?sort_by=employee_name&sort_order=asc")
    assert res.status_code == 200, res.text

    res = client.get("/attendance?sort_by=name&sort_order=desc")
    assert res.status_code == 200, res.text

    # Leaves: employee_name, name, date
    res = client.get("/leaves?sort_by=employee_name&sort_order=asc")
    assert res.status_code == 200, res.text

    res = client.get("/leaves?sort_by=name&sort_order=desc")
    assert res.status_code == 200, res.text

    res = client.get("/leaves?sort_by=date&sort_order=desc")
    assert res.status_code == 200, res.text


def test_scope_reconciliation_fields(setup_test_data):
    db: Session = setup_test_data["db"]
    org_a = setup_test_data["org_a"]
    user_a = setup_test_data["user_a"]

    app.dependency_overrides[get_current_user] = lambda: user_a

    # 1. Suppliers: contact_person and status
    res = client.get("/suppliers?sort_by=contact_person&sort_order=asc")
    assert res.status_code == 200, res.text

    res = client.get("/suppliers?sort_by=status&sort_order=desc")
    assert res.status_code == 200, res.text

    res = client.get("/suppliers?sort_by=is_active&sort_order=asc")
    assert res.status_code == 200, res.text

    # 2. Purchase Returns: related purchase number and aliases
    res = client.get("/purchase-returns?sort_by=purchase_number&sort_order=asc")
    assert res.status_code == 200, res.text

    res = client.get("/purchase-returns?sort_by=related_purchase_number&sort_order=desc")
    assert res.status_code == 200, res.text

    res = client.get("/purchase-returns?sort_by=purchase_invoice_number&sort_order=asc")
    assert res.status_code == 200, res.text

    # 3. Sales Returns: return_number, customer, invoice_number, date, amount, total_amount, status
    res = client.get("/sales-returns?sort_by=return_number&sort_order=asc")
    assert res.status_code == 200, res.text

    res = client.get("/sales-returns?sort_by=customer&sort_order=desc")
    assert res.status_code == 200, res.text

    res = client.get("/sales-returns?sort_by=invoice_number&sort_order=asc")
    assert res.status_code == 200, res.text

    res = client.get("/sales-returns?sort_by=date&sort_order=desc")
    assert res.status_code == 200, res.text

    res = client.get("/sales-returns?sort_by=total_amount&sort_order=asc")
    assert res.status_code == 200, res.text

    res = client.get("/sales-returns?sort_by=status&sort_order=desc")
    assert res.status_code == 200, res.text

    # 4. Sales Invoices / Receivables: invoice_number, customer, date, due_date, total, amount_paid, outstanding_amount, status
    res = client.get("/invoices?sort_by=invoice_number&sort_order=asc")
    assert res.status_code == 200, res.text

    res = client.get("/invoices?sort_by=customer&sort_order=desc")
    assert res.status_code == 200, res.text

    res = client.get("/invoices?sort_by=date&sort_order=desc")
    assert res.status_code == 200, res.text

    res = client.get("/invoices?sort_by=due_date&sort_order=asc")
    assert res.status_code == 200, res.text

    res = client.get("/invoices?sort_by=amount&sort_order=desc")
    assert res.status_code == 200, res.text

    res = client.get("/invoices?sort_by=amount_paid&sort_order=asc")
    assert res.status_code == 200, res.text

    res = client.get("/invoices?sort_by=outstanding&sort_order=desc")
    assert res.status_code == 200, res.text

    res = client.get("/invoices?sort_by=status&sort_order=asc")
    assert res.status_code == 200, res.text

    # 5. Staff: name, designation, department, team, status, phone, date_of_joining
    res = client.get("/users?sort_by=designation&sort_order=asc")
    assert res.status_code == 200, res.text

    res = client.get("/users?sort_by=department&sort_order=desc")
    assert res.status_code == 200, res.text

    res = client.get("/users?sort_by=team&sort_order=asc")
    assert res.status_code == 200, res.text

    res = client.get("/users?sort_by=phone&sort_order=desc")
    assert res.status_code == 200, res.text

    res = client.get("/users?sort_by=date_of_joining&sort_order=asc")
    assert res.status_code == 200, res.text

    # 6. Cash Reconciliation: expected_amount, actual_amount, variance, closed_at
    res = client.get("/cash-reconciliation?sort_by=expected_amount&sort_order=desc")
    assert res.status_code == 200, res.text

    res = client.get("/cash-reconciliation?sort_by=actual_amount&sort_order=asc")
    assert res.status_code == 200, res.text

    res = client.get("/cash-reconciliation?sort_by=variance&sort_order=desc")
    assert res.status_code == 200, res.text

    res = client.get("/cash-reconciliation?sort_by=closed_at&sort_order=asc")
    assert res.status_code == 200, res.text

    # 7. Payment Receipts / Delivery Collections: collector, business_date
    res = client.get("/payment-receipts?sort_by=collector&sort_order=asc")
    assert res.status_code == 200, res.text

    res = client.get("/payment-receipts?sort_by=business_date&sort_order=desc")
    assert res.status_code == 200, res.text

    # 8. Validation Rejections (HTTP 400)
    for endpoint in [
        "/suppliers?sort_by=bogus_col",
        "/purchase-returns?sort_by=bogus_col",
        "/sales-returns?sort_by=bogus_col",
        "/invoices?sort_by=bogus_col",
        "/users?sort_by=bogus_col",
        "/cash-reconciliation?sort_by=bogus_col",
        "/payment-receipts?sort_by=bogus_col",
    ]:
        res_bad = client.get(endpoint)
        assert res_bad.status_code == 400, f"Expected 400 for {endpoint}, got {res_bad.status_code}"


def test_inventory_exposes_product_cover_image(setup_test_data, monkeypatch):
    """GET /inventory and /inventory/{id} carry the product's cover image, normalised like
    the product API, so the stock screens need no /products call (or products.view)."""
    from app.core import files

    db: Session = setup_test_data["db"]
    org_a, org_b = setup_test_data["org_a"], setup_test_data["org_b"]
    monkeypatch.setattr(files.settings, "public_base_url", "https://api.example.com", raising=False)

    tag = uuid.uuid4().hex[:6]
    file_id = str(uuid.uuid4())
    covers = {
        "bare": file_id,                                  # a stored bare file id
        "relative": f"/files/{file_id}",                  # a relative files path
        "absolute": "https://cdn.example.com/p.png",      # already absolute: unchanged
        "none": None,                                     # no image
    }
    products = {}
    for key, cover in covers.items():
        product = Product(organization_id=org_a.id, name=f"ImgStock {tag} {key}", sku=f"IMG-{tag}-{key}",
                          cover_image=cover, total_inventory=7)
        db.add(product)
        products[key] = product
    # Filler rows so some image products land beyond page one
    for i in range(12):
        db.add(Product(organization_id=org_a.id, name=f"ImgStock {tag} filler {i:02d}", sku=f"IMG-{tag}-f{i}",
                       cover_image=f"https://cdn.example.com/f{i}.png"))
    other = Product(organization_id=org_b.id, name=f"ImgStock {tag} other", cover_image="https://cdn.example.com/b.png")
    db.add(other)
    db.commit()

    expected = {
        "bare": f"https://api.example.com/files/{file_id}",
        "relative": f"https://api.example.com/files/{file_id}",
        "absolute": "https://cdn.example.com/p.png",
        "none": None,
    }
    for key, product in products.items():  # the helper the product API's schemas use
        assert files.normalize_file_url(product.cover_image) == expected[key]

    # A staff role with inventory.view but no products permission
    role = Role(organization_id=org_a.id, name=f"Stock viewer {tag}", permissions={"inventory": {"view": True}})
    db.add(role)
    db.flush()
    staff = User(email=f"stock-{tag}@sorting-a.example.com", name="Stock Viewer", password_hash="x",
                 organization_id=org_a.id, system_role=SystemRole.STAFF.value, role_id=role.id, is_active=True)
    db.add(staff)
    db.commit()

    try:
        app.dependency_overrides[get_current_user] = lambda: staff
        assert client.get("/products").status_code == 403  # the old image source is out of reach

        seen = {}
        for page in (1, 2, 3):
            res = client.get("/inventory", params={"search": f"ImgStock {tag}", "page": page, "page_size": 6,
                                                   "sort_by": "name", "sort_order": "asc"})
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["total"] == 16
            for row in body["items"]:
                seen[row["id"]] = row
        assert len(seen) == 16  # pagination covers every product, beyond page one too
        for key, product in products.items():
            row = seen[product.id]
            assert row["cover_image"] == expected[key], key
            assert row["total_stock"] == 7 and row["sku"] == f"IMG-{tag}-{key}"  # existing fields unchanged
            assert set(row) >= {"id", "name", "sku", "product_type", "brand", "category_id", "total_inventory",
                                "total_stock", "is_active", "variations", "cover_image"}
        assert other.id not in seen  # tenant isolation

        detail = client.get(f"/inventory/{products['bare'].id}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["cover_image"] == expected["bare"] and "movements" in detail.json()
        assert client.get(f"/inventory/{products['none'].id}").json()["cover_image"] is None
        assert client.get(f"/inventory/{other.id}").status_code == 404  # other firm's product
    finally:
        app.dependency_overrides.pop(get_current_user, None)


