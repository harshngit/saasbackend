import os
import sys
from datetime import date, datetime, timedelta, timezone

# Ensure backend root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.core.database import Base, engine, get_db
from app.core.security import hash_password
from app.models import (
    Customer,
    Delivery,
    Expense,
    FollowUp,
    GoodsReceiptNote,
    Invoice,
    Lead,
    Notification,
    Organization,
    OrganizationStatus,
    Plan,
    Product,
    PurchaseInvoice,
    Quotation,
    Role,
    SalesOrder,
    SalesReturn,
    Supplier,
    SupplierInvoice,
    SystemRole,
    User,
    UserRole,
    Vehicle,
    Visit,
    Warehouse,
    WarehouseTransfer,
)
from app.core.deps import get_current_user

client = TestClient(app)

def test_pagination_suite():
    db = next(get_db())
    
    from app.core.entitlements import ALL_ENTITLEMENT_KEYS

    plan_all = db.query(Plan).filter(Plan.name == "Pagination All Features Plan").first()
    if not plan_all:
        plan_all = Plan(
            name="Pagination All Features Plan",
            price_monthly=999,
            price_yearly=9999,
            is_active=True,
            entitlements={k: True for k in ALL_ENTITLEMENT_KEYS},
        )
        db.add(plan_all)
        db.flush()

    # 1. Setup two test organizations for tenant isolation tests
    org_a = db.query(Organization).filter(Organization.name == "Pagination Org A").first()
    if not org_a:
        org_a = Organization(
            name="Pagination Org A",
            company_code="CMP-PAG-A",
            status=OrganizationStatus.ACTIVE.value,
            plan_id=plan_all.id,
        )
        db.add(org_a)
        db.flush()
    else:
        org_a.plan_id = plan_all.id

    org_b = db.query(Organization).filter(Organization.name == "Pagination Org B").first()
    if not org_b:
        org_b = Organization(
            name="Pagination Org B",
            company_code="CMP-PAG-B",
            status=OrganizationStatus.ACTIVE.value,
            plan_id=plan_all.id,
        )
        db.add(org_b)
        db.flush()
    else:
        org_b.plan_id = plan_all.id

    # Create admin user for Org A
    user_a = db.query(User).filter(User.email == "admin_a@pagination.test").first()
    if not user_a:
        user_a = User(
            email="admin_a@pagination.test",
            name="Admin A",
            password_hash=hash_password("password123"),
            organization_id=org_a.id,
            system_role=SystemRole.ADMIN.value,
            is_active=True,
        )
        db.add(user_a)
        db.flush()

    # Create admin user for Org B
    user_b = db.query(User).filter(User.email == "admin_b@pagination.test").first()
    if not user_b:
        user_b = User(
            email="admin_b@pagination.test",
            name="Admin B",
            password_hash=hash_password("password123"),
            organization_id=org_b.id,
            system_role=SystemRole.ADMIN.value,
            is_active=True,
        )
        db.add(user_b)
        db.flush()

    db.commit()

    # Seed 25 customers in Org A and 5 customers in Org B
    db.query(Customer).filter(Customer.organization_id == org_a.id).delete()
    db.query(Customer).filter(Customer.organization_id == org_b.id).delete()
    db.commit()

    customers_a = []
    for i in range(1, 26):
        # 15 active, 10 inactive
        # Customer 25 specifically has name "SearchTarget Customer"
        name = f"SearchTarget Customer {i}" if i == 25 else f"Customer A {i:02d}"
        c = Customer(
            organization_id=org_a.id,
            customer_id=f"CUSTA-{i:03d}",
            name=name,
            email=f"cust_a_{i}@test.com",
            phone=f"+919876543{i:03d}",
            is_active=(i <= 15),
            category="retail" if i % 2 == 0 else "wholesale",
        )
        customers_a.append(c)
        db.add(c)

    for i in range(1, 6):
        c = Customer(
            organization_id=org_b.id,
            customer_id=f"CUSTB-{i:03d}",
            name=f"Customer B {i:02d}",
            email=f"cust_b_{i}@test.com",
            phone=f"+918876543{i:03d}",
            is_active=True,
            category="retail",
        )
        db.add(c)

    db.commit()

    # Override current user to user_a
    app.dependency_overrides[get_current_user] = lambda: user_a

    print("--- Test 1: Default page returns at most 10 records ---")
    resp = client.get("/customers")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert isinstance(data, dict), "Response must be a PaginatedResponse dict"
    assert "items" in data and "total" in data and "page" in data and "page_size" in data and "total_pages" in data
    assert data["page"] == 1
    assert data["page_size"] == 10
    assert len(data["items"]) == 10
    assert data["total"] == 25
    assert data["total_pages"] == 3
    print("[PASS] Test 1 passed.")

    print("--- Test 2: Explicit page size works and respects maximum ---")
    resp = client.get("/customers?page_size=5")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 5
    assert data["page_size"] == 5
    assert data["total_pages"] == 5

    # Page size > 100 should be capped or validated
    resp = client.get("/customers?page_size=150")
    assert resp.status_code == 422 or (resp.status_code == 200 and resp.json()["page_size"] <= 100)
    print("[PASS] Test 2 passed.")

    print("--- Test 3: Page 2 returns next records without duplicates ---")
    resp1 = client.get("/customers?page=1&page_size=10")
    resp2 = client.get("/customers?page=2&page_size=10")
    assert resp1.status_code == 200 and resp2.status_code == 200
    items1 = resp1.json()["items"]
    items2 = resp2.json()["items"]
    assert len(items1) == 10
    assert len(items2) == 10
    ids1 = {item["id"] for item in items1}
    ids2 = {item["id"] for item in items2}
    assert ids1.isdisjoint(ids2), "Page 1 and Page 2 must not have overlapping IDs"
    print("[PASS] Test 3 passed.")

    print("--- Test 4: Total count reflects complete matching dataset ---")
    resp = client.get("/customers?is_active=true")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 15
    assert len(data["items"]) == 10
    assert data["total_pages"] == 2
    print("[PASS] Test 4 passed.")

    print("--- Test 5: Search finds records beyond page 1 ---")
    # Customer 25 is on page 3 of default list
    resp = client.get("/customers?search=SearchTarget")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["name"] == "SearchTarget Customer 25"
    print("[PASS] Test 5 passed.")

    print("--- Test 6: Search and filters work together ---")
    # Search for customer with category=retail
    resp = client.get("/customers?search=Customer&category=retail")
    assert resp.status_code == 200
    data = resp.json()
    # 24 numbered customers, even ones are retail -> 12 retail customers
    assert data["total"] == 12
    assert len(data["items"]) == 10
    assert data["total_pages"] == 2
    for item in data["items"]:
        assert item["category"] == "retail"
    print("[PASS] Test 6 passed.")

    print("--- Test 7: Empty results return valid empty-page metadata ---")
    resp = client.get("/customers?search=NonExistentCustomerNameXYZ")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert len(data["items"]) == 0
    assert data["total_pages"] == 0
    print("[PASS] Test 7 passed.")

    print("--- Test 8: Tenant isolation on results and count ---")
    # Org A has 25 customers, Org B has 5 customers
    resp_a = client.get("/customers")
    assert resp_a.json()["total"] == 25

    app.dependency_overrides[get_current_user] = lambda: user_b
    resp_b = client.get("/customers")
    assert resp_b.status_code == 200
    assert resp_b.json()["total"] == 5
    assert len(resp_b.json()["items"]) == 5
    for item in resp_b.json()["items"]:
        assert "Customer B" in item["name"]
    print("[PASS] Test 8 passed.")

    # Switch back to user_a
    app.dependency_overrides[get_current_user] = lambda: user_a

    print("--- Test 9: Seed & test other paginated endpoints ---")
    # 1. Suppliers
    db.query(Supplier).filter(Supplier.organization_id == org_a.id).delete()
    for i in range(1, 15):
        s = Supplier(
            organization_id=org_a.id,
            name=f"Supplier {i:02d}",
            contact_person=f"Contact {i}",
            email=f"supp{i}@test.com",
            phone=f"+919999000{i:02d}",
            is_active=True,
        )
        db.add(s)
    db.commit()

    resp = client.get("/suppliers?page=1&page_size=10")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 14
    assert len(data["items"]) == 10
    assert data["total_pages"] == 2

    # 2. Products
    db.query(Product).filter(Product.organization_id == org_a.id).delete()
    for i in range(1, 16):
        p = Product(
            organization_id=org_a.id,
            name=f"Product {i:02d}",
            sku=f"SKU-{i:03d}",
            brand="BrandA",
            is_active=True,
            uom="pcs",
        )
        db.add(p)
    db.commit()

    resp = client.get("/products?page=1&page_size=10")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 15
    assert len(data["items"]) == 10

    # 3. Sales Orders
    db.query(SalesOrder).filter(SalesOrder.organization_id == org_a.id).delete()
    for i in range(1, 13):
        so = SalesOrder(
            organization_id=org_a.id,
            order_number=f"SO-2026-{i:04d}",
            customer_id=customers_a[0].id,
            status="draft",
            total=100.0 * i,
        )
        db.add(so)
    db.commit()

    resp = client.get("/orders?page=1&page_size=10")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 12
    assert len(data["items"]) == 10

    # 4. Invoices
    db.query(Invoice).filter(Invoice.organization_id == org_a.id).delete()
    for i in range(1, 12):
        inv = Invoice(
            organization_id=org_a.id,
            invoice_number=f"INV-2026-{i:04d}",
            customer_id=customers_a[0].id,
            total=500.0 * i,
            status="issued",
        )
        db.add(inv)
    db.commit()

    resp = client.get("/invoices?page=1&page_size=10")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 11
    assert len(data["items"]) == 10

    # 5. Purchases
    db.query(PurchaseInvoice).filter(PurchaseInvoice.organization_id == org_a.id).delete()
    supp = db.query(Supplier).filter(Supplier.organization_id == org_a.id).first()
    for i in range(1, 14):
        pi = PurchaseInvoice(
            organization_id=org_a.id,
            invoice_number=f"PINV-2026-{i:04d}",
            supplier_id=supp.id if supp else None,
            total=250.0 * i,
            status="confirmed",
        )
        db.add(pi)
    db.commit()

    resp = client.get("/purchases?page=1&page_size=10")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 13
    assert len(data["items"]) == 10

    # 6. Expenses
    db.query(Expense).filter(Expense.organization_id == org_a.id).delete()
    for i in range(1, 15):
        exp = Expense(
            organization_id=org_a.id,
            expense_number=f"EXP-2026-{i:04d}",
            expense_date=datetime.now(timezone.utc),
            amount=50.0 * i,
            category="Office",
            status="approved",
        )
        db.add(exp)
    db.commit()

    resp = client.get("/expenses?page=1&page_size=10")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 14
    assert len(data["items"]) == 10

    # 7. Leads
    db.query(Lead).filter(Lead.organization_id == org_a.id).delete()
    for i in range(1, 13):
        lead = Lead(
            organization_id=org_a.id,
            lead_id=f"LEAD-2026-{i:04d}",
            name=f"Lead {i:02d}",
            contact_person=f"Lead Contact {i}",
            mobile_number=f"+91999999{i:04d}",
            lead_source="website",
            lead_status="new",
        )
        db.add(lead)
    db.commit()

    resp = client.get("/leads?page=1&page_size=10")
    assert resp.status_code == 200, f"Leads failed with status {resp.status_code}: {resp.text}"
    data = resp.json()
    assert data["total"] == 12
    assert len(data["items"]) == 10

    # 8. Follow-ups
    db.query(FollowUp).filter(FollowUp.organization_id == org_a.id).delete()
    for i in range(1, 14):
        fu = FollowUp(
            organization_id=org_a.id,
            customer_id=customers_a[0].id,
            assigned_to_id=user_a.id,
            title=f"Follow-up {i:02d}",
            due_date=datetime.now(timezone.utc) + timedelta(days=i),
            status="pending",
        )
        db.add(fu)
    db.commit()

    resp = client.get("/follow-ups?page=1&page_size=10")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 13
    assert len(data["items"]) == 10

    # 9. Notifications
    db.query(Notification).filter(Notification.user_id == user_a.id).delete()
    for i in range(1, 15):
        notif = Notification(
            organization_id=org_a.id,
            user_id=user_a.id,
            title=f"Notification {i:02d}",
            body=f"Message content {i}",
            is_read=False,
        )
        db.add(notif)
    db.commit()

    resp = client.get("/notifications?page=1&page_size=10")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 14
    assert len(data["items"]) == 10

    print("[PASS] Test 9 passed.")

    print("--- Test 10: Verify Complete Data Endpoints & Dropdowns are preserved Unpaginated ---")
    # Notifications unread-count must return raw dict
    resp = client.get("/notifications/unread-count")
    assert resp.status_code == 200
    data = resp.json()
    assert "unread" in data
    assert data["unread"] == 14

    # Assignable staff dropdown must return raw list
    resp = client.get("/users/assignable")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list), "Dropdown /users/assignable must remain a list"

    # Warehouses dropdown must return raw list
    resp = client.get("/warehouses")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list), "Dropdown /warehouses must remain a list"

    # Admin dashboard must return complete dashboard object
    resp = client.get("/dashboard/admin")
    assert resp.status_code == 200, f"Dashboard failed: {resp.text}"
    data = resp.json()
    assert "summary" in data and "orders" in data and "cashflow" in data

    print("[PASS] Test 10 passed.")

    print("\n=======================================================")
    print("ALL 10 TEST SUITE CATEGORIES PASSED SUCCESSFULLY!")
    print("=======================================================\n")

if __name__ == "__main__":
    test_pagination_suite()
