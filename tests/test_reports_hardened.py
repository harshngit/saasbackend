import os
import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base, get_db
from app.core.deps import require_permission
from app.main import app
from app.models import (
    Customer,
    CustomerPayment,
    Delivery,
    DeliveryItem,
    Expense,
    Invoice,
    InvoiceItem,
    Organization,
    Product,
    ProductPricing,
    ProductVariant,
    PurchaseInvoice,
    PurchaseReturn,
    ReturnItem,
    SalesOrder,
    SalesOrderItem,
    SalesReturn,
    StockMovement,
    StockReservation,
    Supplier,
    SupplierInvoice,
    SupplierPayment,
    User,
    Vehicle,
    Warehouse,
    WarehouseStock,
)
from app.services import report_service

from sqlalchemy.pool import StaticPool

# Use in-memory SQLite database for testing with StaticPool so all sessions share the memory DB
TEST_DB_URL = "sqlite:///:memory:"

engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(scope="module", autouse=True)
def setup_db():
    Base.metadata.create_all(bind=engine, checkfirst=True)
    yield
    Base.metadata.drop_all(bind=engine, checkfirst=True)


@pytest.fixture
def db():
    session = TestingSessionLocal()
    yield session
    session.close()


from app.core.entitlements import default_entitlements_for_plan
from app.models import Plan


@pytest.fixture
def test_data(db):
    pro_plan = Plan(
        name="Pro",
        price_monthly=999,
        price_yearly=9999,
        entitlements=default_entitlements_for_plan("Pro"),
    )
    db.add(pro_plan)
    db.flush()

    org = Organization(
        id=str(uuid.uuid4()),
        name="Test Reports Org",
        company_code="CMP-10001",
        timezone="Asia/Kolkata",
        plan_id=pro_plan.id,
    )
    db.add(org)

    org2 = Organization(
        id=str(uuid.uuid4()),
        name="Foreign Org",
        company_code="CMP-20002",
        timezone="Asia/Kolkata",
        plan_id=pro_plan.id,
    )
    db.add(org2)

    user = User(
        id=str(uuid.uuid4()),
        email="testuser@example.com",
        name="Test Analyst",
        password_hash="fake_hash_value",
        organization_id=org.id,
        role="admin",
        is_active=True,
    )
    db.add(user)

    warehouse = Warehouse(
        id=str(uuid.uuid4()),
        organization_id=org.id,
        name="Main Hub",
        code="WH-01",
        is_default=True,
        is_active=True,
    )
    db.add(warehouse)

    cust = Customer(
        id=str(uuid.uuid4()),
        organization_id=org.id,
        name="Acme Corp",
        business_name="Acme Retail",
        phone="9876543210",
        credit_limit=50000.0,
    )
    db.add(cust)

    foreign_cust = Customer(
        id=str(uuid.uuid4()),
        organization_id=org2.id,
        name="Foreign Customer",
        phone="9999999999",
    )
    db.add(foreign_cust)

    sup = Supplier(
        id=str(uuid.uuid4()),
        organization_id=org.id,
        name="Global Supplies",
        supplier_code="SUP-0001",
        phone="8765432109",
    )
    db.add(sup)

    prod = Product(
        id=str(uuid.uuid4()),
        organization_id=org.id,
        name="Premium Widget",
        sku="WIDGET-01",
        price=100.0,
        minimum_stock_level=10,
        is_active=True,
    )
    db.add(prod)

    pricing = ProductPricing(
        id=str(uuid.uuid4()),
        product_id=prod.id,
        purchase_price=60.0,
        selling_price=100.0,
    )
    db.add(pricing)

    ws = WarehouseStock(
        id=str(uuid.uuid4()),
        organization_id=org.id,
        warehouse_id=warehouse.id,
        product_id=prod.id,
        on_hand_quantity=50.0,
    )
    db.add(ws)

    db.commit()

    return {
        "org": org,
        "org2": org2,
        "user": user,
        "warehouse": warehouse,
        "customer": cust,
        "foreign_customer": foreign_cust,
        "supplier": sup,
        "product": prod,
        "pricing": pricing,
        "warehouse_stock": ws,
    }


@pytest.fixture
def client(db, test_data):
    from app.routers.reports import _export, _view

    def override_get_db():
        yield db

    def override_user():
        return test_data["user"]

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[_view] = override_user
    app.dependency_overrides[_export] = override_user

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


def test_registry_contains_all_reports():
    assert len(report_service.REPORT_TYPES) == 17
    assert len(report_service.REPORT_REGISTRY) == 17
    expected_reports = {
        "daily-transaction", "sales", "purchase", "customer-outstanding",
        "supplier-outstanding", "payment-collection", "expense", "cash-collection",
        "gst-summary", "sales-return", "purchase-return", "profit-loss",
        "supplier-payment", "inventory-summary", "stock-movement",
        "sales-overview", "cash-flow-sheet",
    }
    assert report_service.REPORT_TYPES == expected_reports


def test_standard_contract_and_backward_compatibility(client, test_data):
    # Call sales report with only date_from & date_to
    res = client.get("/reports/sales?date_from=2026-01-01&date_to=2026-12-31")
    assert res.status_code == 200
    data = res.json()

    # Verify existing contract fields
    assert data["type"] == "sales"
    assert data["date_from"] == "2026-01-01"
    assert data["date_to"] == "2026-12-31"
    assert "summary" in data
    assert "rows" in data

    # Verify additive fields
    assert "pagination" in data
    assert data["pagination"]["page"] == 1
    assert data["pagination"]["page_size"] == 25
    assert "meta" in data
    assert data["meta"]["currency"] == "INR"
    assert len(data["meta"]["columns"]) > 0
    assert "chart" in data
    assert data["chart"]["type"] == "line"


def test_daily_transaction_cash_separation(client, db, test_data):
    org = test_data["org"]
    cust = test_data["customer"]
    sup = test_data["supplier"]

    # 1. Accrual Sale (Invoice) of 10,000 on credit (paid = 0)
    inv = Invoice(
        organization_id=org.id,
        invoice_number="INV-001",
        customer_id=cust.id,
        subtotal=9000.0,
        tax=1000.0,
        total=10000.0,
        amount_paid=0.0,
        status="unpaid",
        is_credit_note=False,
        invoice_date=datetime.now(timezone.utc),
    )
    db.add(inv)

    # 2. Customer payment of 4,000 cash
    pay = CustomerPayment(
        organization_id=org.id,
        customer_id=cust.id,
        amount=4000.0,
        payment_mode="cash",
        reference="RCPT-001",
        received_on=datetime.now(timezone.utc),
    )
    db.add(pay)

    # 3. Accrual Purchase of 6,000 on credit
    pinv = PurchaseInvoice(
        organization_id=org.id,
        supplier_id=sup.id,
        invoice_number="PUR-001",
        subtotal=5500.0,
        tax=500.0,
        total=6000.0,
        status="approved",
        invoice_date=datetime.now(timezone.utc),
    )
    db.add(pinv)
    db.flush()

    sinv = SupplierInvoice(
        organization_id=org.id,
        supplier_id=sup.id,
        purchase_id=pinv.id,
        supplier_invoice_number="SINV-001",
        subtotal=5500.0,
        tax_amount=500.0,
        grand_total=6000.0,
        amount_paid=0.0,
        status="recorded",
        payment_status="unpaid",
        supplier_invoice_date=datetime.now(timezone.utc),
    )
    db.add(sinv)

    # 4. Supplier Payment of 2,000
    spay = SupplierPayment(
        organization_id=org.id,
        supplier_id=sup.id,
        payment_number="SPAY-001",
        amount=2000.0,
        payment_mode="bank_transfer",
        status="recorded",
        paid_on=datetime.now(timezone.utc),
    )
    db.add(spay)

    # 5. Approved Paid Expense of 500
    exp = Expense(
        organization_id=org.id,
        category="Office Supplies",
        amount=500.0,
        status="approved",
        payment_status="paid",
        expense_date=datetime.now(timezone.utc),
    )
    db.add(exp)

    db.commit()

    res = client.get("/reports/daily-transaction")
    assert res.status_code == 200
    data = res.json()
    summary = data["summary"]

    # Invariants:
    # Accrual Sales = 10,000
    # Customer Collections (Cash In) = 4,000 (Sale booking must NOT double count as Cash In)
    # Accrual Purchases = 6,000
    # Supplier Payments = 2,000
    # Expenses = 500
    # Cash Out = 2000 + 500 = 2500
    # Net Cash Flow = 4000 - 2500 = 1500
    assert summary["sales"] == 10000.0
    assert summary["purchases"] == 6000.0
    assert summary["customer_collections"] == 4000.0
    assert summary["supplier_payments"] == 2000.0
    assert summary["expenses"] == 500.0
    assert summary["cash_in"] == 4000.0
    assert summary["cash_out"] == 2500.0
    assert summary["net_cash_flow"] == 1500.0


def test_sales_report_groupings(client, db, test_data):
    org = test_data["org"]
    cust = test_data["customer"]
    prod = test_data["product"]

    # Create SalesOrder and SalesOrderItem with cost_price = 60.0
    so = SalesOrder(
        organization_id=org.id,
        customer_id=cust.id,
        order_number="SO-GRP-01",
        status="completed",
        total=220.0,
    )
    db.add(so)
    db.flush()

    so_item = SalesOrderItem(
        order_id=so.id,
        product_id=prod.id,
        product_name=prod.name,
        quantity=2,
        unit_price=100.0,
        cost_price=60.0,
        line_total=200.0,
    )
    db.add(so_item)
    db.flush()

    # Create Invoice with InvoiceItem
    inv = Invoice(
        organization_id=org.id,
        invoice_number="INV-GRP-01",
        order_id=so.id,
        customer_id=cust.id,
        subtotal=200.0,
        tax=20.0,
        total=220.0,
        amount_paid=100.0,
        status="partially_paid",
        is_credit_note=False,
        invoice_date=datetime.now(timezone.utc),
    )
    db.add(inv)
    db.flush()

    item = InvoiceItem(
        invoice_id=inv.id,
        product_id=prod.id,
        order_item_id=so_item.id,
        product_name=prod.name,
        quantity=2,
        unit_price=100.0,
        tax=20.0,
        line_total=220.0,
    )
    db.add(item)
    db.commit()

    # 1. Customer grouping
    res_cust = client.get("/reports/sales?group_by=customer")
    assert res_cust.status_code == 200
    data_cust = res_cust.json()
    assert data_cust["meta"]["group_by"] == "customer"
    assert len(data_cust["rows"]) >= 1
    assert data_cust["rows"][0]["customer"] == "Acme Retail"

    # 2. Product grouping
    res_prod = client.get("/reports/sales?group_by=product")
    assert res_prod.status_code == 200
    data_prod = res_prod.json()
    assert data_prod["meta"]["group_by"] == "product"
    assert len(data_prod["rows"]) >= 1
    p_row = [r for r in data_prod["rows"] if r["product"] == "Premium Widget"][0]
    assert p_row["quantity_sold"] == 2.0
    # Cost = 2 * 60 = 120, Sales = 220, Gross Profit = 100
    assert p_row["cogs"] == 120.0
    assert p_row["gross_profit"] == 100.0


def test_customer_outstanding_aging(client, db, test_data):
    org = test_data["org"]
    cust = test_data["customer"]

    # Invoice overdue by 45 days (bucket 31_60)
    old_date = datetime.now(timezone.utc) - timedelta(days=45)
    inv = Invoice(
        organization_id=org.id,
        invoice_number="INV-OVERDUE-45",
        customer_id=cust.id,
        subtotal=1000.0,
        total=1000.0,
        amount_paid=0.0,
        status="unpaid",
        is_credit_note=False,
        invoice_date=old_date,
        due_date=old_date,
    )
    db.add(inv)
    db.commit()

    res = client.get("/reports/customer-outstanding")
    assert res.status_code == 200
    data = res.json()
    assert data["summary"]["age_31_60"] >= 1000.0
    assert data["summary"]["total_overdue"] >= 1000.0


def test_supplier_outstanding_aging(client, db, test_data):
    org = test_data["org"]
    sup = test_data["supplier"]

    old_date = datetime.now(timezone.utc) - timedelta(days=70)
    pinv = PurchaseInvoice(
        organization_id=org.id,
        supplier_id=sup.id,
        invoice_number="PUR-OLD-70",
        subtotal=3000.0,
        tax=0.0,
        total=3000.0,
        status="approved",
        invoice_date=old_date,
    )
    db.add(pinv)
    db.flush()

    sinv = SupplierInvoice(
        organization_id=org.id,
        supplier_id=sup.id,
        purchase_id=pinv.id,
        supplier_invoice_number="SINV-OLD-70",
        subtotal=3000.0,
        grand_total=3000.0,
        return_amount=500.0,
        amount_paid=500.0,
        status="recorded",
        payment_status="partially_paid",
        supplier_invoice_date=old_date,
        due_date=old_date,
    )
    db.add(sinv)
    db.commit()

    res = client.get("/reports/supplier-outstanding")
    assert res.status_code == 200
    data = res.json()
    # Outstanding = 3000 - 500 (return) - 500 (paid) = 2000
    assert data["summary"]["total_payable"] >= 2000.0
    assert data["summary"]["age_61_90"] >= 2000.0


def test_cash_collection_split_payment(client, db, test_data):
    org = test_data["org"]
    cust = test_data["customer"]

    # Customer payment with pure cash
    pay = CustomerPayment(
        organization_id=org.id,
        customer_id=cust.id,
        amount=1500.0,
        payment_mode="cash",
        reference="CASH-ONLY",
        received_on=datetime.now(timezone.utc),
    )
    db.add(pay)
    db.commit()

    res = client.get("/reports/cash-collection")
    assert res.status_code == 200
    data = res.json()
    assert data["summary"]["total_cash"] >= 1500.0


def test_profit_loss_cogs_formula(client, db, test_data):
    org = test_data["org"]
    cust = test_data["customer"]
    prod = test_data["product"]

    # Create SalesOrder and SalesOrderItem with stored sale-time cost snapshot = 60.0
    so = SalesOrder(
        organization_id=org.id,
        customer_id=cust.id,
        order_number="SO-PL-01",
        status="completed",
        total=500.0,
    )
    db.add(so)
    db.flush()

    so_item = SalesOrderItem(
        order_id=so.id,
        product_id=prod.id,
        product_name=prod.name,
        quantity=5,
        unit_price=100.0,
        cost_price=60.0,  # Sale-time historical cost snapshot
        line_total=500.0,
    )
    db.add(so_item)
    db.flush()

    # 1. Invoice for 5 items @ 100 = 500 total (COGS = 5 * 60 = 300 from stored snapshot)
    inv = Invoice(
        organization_id=org.id,
        invoice_number="INV-PL-01",
        order_id=so.id,
        customer_id=cust.id,
        subtotal=500.0,
        tax=0.0,
        total=500.0,
        amount_paid=500.0,
        status="paid",
        is_credit_note=False,
        invoice_date=datetime.now(timezone.utc),
    )
    db.add(inv)
    db.flush()

    item = InvoiceItem(
        invoice_id=inv.id,
        product_id=prod.id,
        order_item_id=so_item.id,
        product_name=prod.name,
        quantity=5,
        unit_price=100.0,
        line_total=500.0,
    )
    db.add(item)

    # 2. Approved Expense = 50
    exp = Expense(
        organization_id=org.id,
        category="Logistics",
        amount=50.0,
        status="approved",
        payment_status="paid",
        expense_date=datetime.now(timezone.utc),
    )
    db.add(exp)
    db.commit()

    res = client.get("/reports/profit-loss")
    assert res.status_code == 200
    data = res.json()
    summary = data["summary"]

    assert summary["cogs"] >= 300.0
    assert summary["cogs_basis"] == "historical_cost_snapshots"
    assert summary["gross_profit"] == round(summary["net_sales"] - summary["cogs"], 2)
    assert summary["net_profit"] == round(summary["gross_profit"] - summary["operating_expenses"], 2)


def test_pl_cogs_safety_no_fabricated_historical_cogs(client, db, test_data):
    """Regression test: Sale without reliable cost snapshot does NOT invent COGS from catalog purchase price."""
    org = test_data["org"]
    cust = test_data["customer"]
    prod = test_data["product"]  # has ProductPricing purchase_price = 60.0

    # Invoice without order_item_id / without cost snapshot
    inv_nocost = Invoice(
        organization_id=org.id,
        invoice_number="INV-NOCOST-999",
        customer_id=cust.id,
        subtotal=200.0,
        tax=0.0,
        total=200.0,
        amount_paid=200.0,
        status="paid",
        is_credit_note=False,
        invoice_date=datetime.now(timezone.utc),
    )
    db.add(inv_nocost)
    db.flush()

    item_nocost = InvoiceItem(
        invoice_id=inv_nocost.id,
        product_id=prod.id,
        order_item_id=None,  # NO snapshot
        product_name=prod.name,
        quantity=2,
        unit_price=100.0,
        line_total=200.0,
    )
    db.add(item_nocost)
    db.commit()

    res = client.get("/reports/profit-loss")
    assert res.status_code == 200
    data = res.json()
    # The item with no snapshot must NOT have 2 * 60 = 120 added to COGS
    assert data["summary"]["missing_cogs_lines"] >= 1
    assert "no historical cost snapshot" in data["meta"]["cogs_notes"] or "strictly from" in data["meta"]["cogs_notes"]


def test_daily_transaction_cash_out_approved_unpaid_vs_paid_expense(client, db, test_data):
    """Regression test: Approved unpaid expense appears in expense activity but does NOT increase cash_out."""
    org = test_data["org"]

    # Baseline daily transaction report
    res_before = client.get("/reports/daily-transaction")
    assert res_before.status_code == 200
    cash_out_before = res_before.json()["summary"]["cash_out"]
    expenses_before = res_before.json()["summary"]["expenses"]

    # 1. Add an approved UNPAID expense (payment_status="Pending")
    unpaid_exp = Expense(
        organization_id=org.id,
        category="Office Rent",
        amount=12000.0,
        status="approved",
        payment_status="Pending",
        expense_date=datetime.now(timezone.utc),
    )
    db.add(unpaid_exp)
    db.commit()

    res_after_unpaid = client.get("/reports/daily-transaction")
    assert res_after_unpaid.status_code == 200
    data_unpaid = res_after_unpaid.json()

    # Expenses activity total increased by 12000
    assert data_unpaid["summary"]["expenses"] == round(expenses_before + 12000.0, 2)
    # But cash_out did NOT increase!
    assert data_unpaid["summary"]["cash_out"] == cash_out_before

    # Verify row direction is Accrual Expense
    unpaid_rows = [r for r in data_unpaid["rows"] if r["reference"] == "Office Rent"]
    assert len(unpaid_rows) >= 1
    assert unpaid_rows[0]["direction"] == "Accrual Expense"

    # 2. Add an approved PAID expense (payment_status="Paid")
    paid_exp = Expense(
        organization_id=org.id,
        category="Courier Charges",
        amount=450.0,
        status="approved",
        payment_status="Paid",
        expense_date=datetime.now(timezone.utc),
    )
    db.add(paid_exp)
    db.commit()

    res_after_paid = client.get("/reports/daily-transaction")
    assert res_after_paid.status_code == 200
    data_paid = res_after_paid.json()

    # Both expenses and cash_out increased by 450
    assert data_paid["summary"]["expenses"] == round(expenses_before + 12000.0 + 450.0, 2)
    assert data_paid["summary"]["cash_out"] == round(cash_out_before + 450.0, 2)

    paid_rows = [r for r in data_paid["rows"] if r["reference"] == "Courier Charges"]
    assert len(paid_rows) >= 1
    assert paid_rows[0]["direction"] == "Cash Out"


def test_receivable_and_payable_as_of_date_rejected(client):
    """Regression test: Unsupported historical as_of_date is rejected with clear HTTP 400 error."""
    # Customer outstanding with as_of_date
    res_ar = client.get("/reports/customer-outstanding?as_of_date=2025-01-01")
    assert res_ar.status_code == 400
    assert "as_of_date" in res_ar.json()["detail"]
    assert "not supported" in res_ar.json()["detail"]

    # Supplier outstanding with as_of_date
    res_ap = client.get("/reports/supplier-outstanding?as_of_date=2025-01-01")
    assert res_ap.status_code == 400
    assert "as_of_date" in res_ap.json()["detail"]
    assert "not supported" in res_ap.json()["detail"]


def test_supplier_payment_report(client, db, test_data):
    org = test_data["org"]
    sup = test_data["supplier"]

    sp = SupplierPayment(
        organization_id=org.id,
        supplier_id=sup.id,
        payment_number="SPAY-NEW-01",
        amount=7500.0,
        allocated_amount=5000.0,
        unallocated_amount=2500.0,
        payment_mode="upi",
        status="recorded",
        paid_on=datetime.now(timezone.utc),
    )
    db.add(sp)
    db.commit()

    res = client.get("/reports/supplier-payment")
    assert res.status_code == 200
    data = res.json()
    assert data["type"] == "supplier-payment"
    assert data["summary"]["total_paid"] >= 7500.0
    assert data["summary"]["allocated_amount"] >= 5000.0
    assert data["summary"]["unallocated_amount"] >= 2500.0


def test_inventory_summary_report(client, db, test_data):
    res = client.get("/reports/inventory-summary")
    assert res.status_code == 200
    data = res.json()
    assert data["type"] == "inventory-summary"
    assert data["summary"]["total_on_hand"] >= 50.0
    assert data["meta"]["valuation_basis"] == "Current cost valuation"
    assert len(data["rows"]) >= 1


def test_stock_movement_report(client, db, test_data):
    org = test_data["org"]
    wh = test_data["warehouse"]
    prod = test_data["product"]

    sm = StockMovement(
        organization_id=org.id,
        warehouse_id=wh.id,
        product_id=prod.id,
        movement_type="purchase_in",
        quantity=25,
        balance_after=75,
        note="Received batch B-01",
        created_at=datetime.now(timezone.utc),
    )
    db.add(sm)
    db.commit()

    res = client.get("/reports/stock-movement")
    assert res.status_code == 200
    data = res.json()
    assert data["type"] == "stock-movement"
    assert data["summary"]["stock_in"] >= 25
    assert len(data["rows"]) >= 1


def test_tenant_isolation_foreign_entity_rejected(client, test_data):
    # Pass foreign customer_id belonging to another organization
    foreign_id = test_data["foreign_customer"].id
    res = client.get(f"/reports/sales?customer_id={foreign_id}")
    assert res.status_code == 404
    assert "not found in organization" in res.json()["detail"]


def test_pagination_and_full_summary(client, db, test_data):
    org = test_data["org"]
    cust = test_data["customer"]

    # Seed 30 invoices
    for idx in range(30):
        inv = Invoice(
            organization_id=org.id,
            invoice_number=f"INV-PAG-{idx}",
            customer_id=cust.id,
            subtotal=100.0,
            total=100.0,
            amount_paid=100.0,
            status="paid",
            is_credit_note=False,
            invoice_date=datetime.now(timezone.utc),
        )
        db.add(inv)
    db.commit()

    # Request page 1 with page_size = 10
    res = client.get("/reports/sales?page=1&page_size=10")
    assert res.status_code == 200
    data = res.json()

    assert data["pagination"]["page"] == 1
    assert data["pagination"]["page_size"] == 10
    assert data["pagination"]["total"] >= 30
    assert len(data["rows"]) == 10
    # Summary must reflect all 30+ invoices (not just 10)
    assert data["summary"]["total_sales"] >= 3000.0


def test_exports_excel_and_pdf(client):
    # Excel export
    res_xlsx = client.get("/reports/sales/export?format=excel")
    assert res_xlsx.status_code == 200
    assert res_xlsx.headers["content-type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert len(res_xlsx.content) > 0

    # PDF export
    res_pdf = client.get("/reports/sales/export?format=pdf")
    assert res_pdf.status_code == 200
    assert res_pdf.headers["content-type"] == "application/pdf"
    assert len(res_pdf.content) > 0


def test_screen_pagination_capped_at_100(client):
    """Screen pagination strictly rejects page_size > 100."""
    res = client.get("/reports/sales?page_size=150")
    assert res.status_code == 422  # FastAPI validation constraint le=100


def test_export_more_than_100_rows_not_truncated(client, db, test_data):
    """Export with >100 rows does not get truncated to 100 rows."""
    org = test_data["org"]
    cust = test_data["customer"]

    # Seed 120 invoices
    for idx in range(120):
        inv = Invoice(
            organization_id=org.id,
            invoice_number=f"INV-EXP-{idx}",
            customer_id=cust.id,
            subtotal=50.0,
            total=50.0,
            amount_paid=50.0,
            status="paid",
            is_credit_note=False,
            invoice_date=datetime.now(timezone.utc),
        )
        db.add(inv)
    db.commit()

    report = report_service.build_report(db, org.id, "sales", is_export=True)
    assert len(report["rows"]) >= 120
    assert report["pagination"]["page_size"] == 10000
    assert report["pagination"]["total"] >= 120

    # Excel export works for >100 rows
    res = client.get("/reports/sales/export?format=excel")
    assert res.status_code == 200
    assert len(res.content) > 0


def test_export_at_10000_rows_accepted(db, test_data, monkeypatch):
    """Export dataset containing up to 10,000 rows is accepted."""
    org = test_data["org"]

    mock_rows = [{"invoice_number": f"INV-{i}", "total": 100.0} for i in range(10000)]

    def mock_builder(db, org_id, df, dt, params):
        paged_rows, pagination = report_service._paginate(mock_rows, params.get("page", 1), params.get("page_size", 25))
        return {
            "summary": {"total_sales": 1000000.0},
            "rows": paged_rows,
            "pagination": pagination,
            "meta": {"currency": "INR", "columns": []},
            "chart": {"type": "bar", "data": []},
        }

    monkeypatch.setitem(report_service._BUILDERS, "sales", mock_builder)

    res = report_service.build_report(db, org.id, "sales", is_export=True)
    assert len(res["rows"]) == 10000
    assert res["pagination"]["total"] == 10000


def test_export_more_than_10000_rows_rejected_with_http_400(client, test_data, monkeypatch):
    """Export dataset exceeding 10,000 rows returns HTTP 400 with a clear error message."""
    org = test_data["org"]

    mock_rows = [{"invoice_number": f"INV-{i}", "total": 100.0} for i in range(10005)]

    def mock_builder(db, org_id, df, dt, params):
        paged_rows, pagination = report_service._paginate(mock_rows, params.get("page", 1), params.get("page_size", 25))
        return {
            "summary": {"total_sales": 1000500.0},
            "rows": paged_rows,
            "pagination": pagination,
            "meta": {"currency": "INR", "columns": []},
            "chart": {"type": "bar", "data": []},
        }

    monkeypatch.setitem(report_service._BUILDERS, "sales", mock_builder)

    res = client.get("/reports/sales/export?format=excel")
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "Export limit exceeded" in detail
    assert "10,000 rows" in detail


def test_sales_overview_operational_report(client, db, test_data):
    """Test Sales Overview operational report: root SalesOrder, delivery aggregation, quantities, filters, and summary."""
    org = test_data["org"]
    cust = test_data["customer"]
    salesp = test_data["user"]
    prod = test_data["product"]

    # 1. Driver & Vehicle setup
    driver = User(
        id=str(uuid.uuid4()),
        email="driver@example.com",
        name="Fast Driver",
        password_hash="fake",
        organization_id=org.id,
        role="delivery_partner",
        is_active=True,
    )
    vehicle = Vehicle(
        id=str(uuid.uuid4()),
        organization_id=org.id,
        vehicle_number="MH-01-AB-1234",
        vehicle_type="Tata Ace",
        is_active=True,
    )
    db.add_all([driver, vehicle])
    db.commit()

    # Order 1: No deliveries, not_started, 2 items (qty 5 + 10 = 15)
    so1 = SalesOrder(
        organization_id=org.id,
        order_number="SO-OV-01",
        sales_order_number="SO-10001-2026-00001",
        customer_id=cust.id,
        salesperson_id=salesp.id,
        status="placed",
        fulfilment_status="not_started",
        total=1500.0,
        order_date=datetime.now(timezone.utc),
    )
    db.add(so1)
    db.flush()
    item1_1 = SalesOrderItem(
        order_id=so1.id,
        product_id=prod.id,
        product_name=prod.name,
        quantity=5,
        unit_price=100.0,
        delivered_quantity=0,
        line_total=500.0,
    )
    item1_2 = SalesOrderItem(
        order_id=so1.id,
        product_id=prod.id,
        product_name=prod.name,
        quantity=10,
        unit_price=100.0,
        delivered_quantity=0,
        line_total=1000.0,
    )
    db.add_all([item1_1, item1_2])

    # Order 2: 1 delivery, planned
    so2 = SalesOrder(
        organization_id=org.id,
        order_number="SO-OV-02",
        sales_order_number="SO-10001-2026-00002",
        customer_id=cust.id,
        salesperson_id=salesp.id,
        status="processing",
        fulfilment_status="planned",
        total=800.0,
        order_date=datetime.now(timezone.utc),
    )
    db.add(so2)
    db.flush()
    item2 = SalesOrderItem(
        order_id=so2.id,
        product_id=prod.id,
        product_name=prod.name,
        quantity=8,
        unit_price=100.0,
        delivered_quantity=0,
        line_total=800.0,
    )
    db.add(item2)
    deliv2 = Delivery(
        organization_id=org.id,
        sales_order_id=so2.id,
        customer_id=cust.id,
        delivery_note_number="DN-OV-02",
        delivery_partner_id=driver.id,
        vehicle_id=vehicle.id,
        status="planned",
        delivery_date=datetime.now(timezone.utc),
    )
    db.add(deliv2)

    # Order 3: Multiple deliveries (Deliv A: delivered, Deliv B: in_transit, Deliv C: cancelled)
    so3 = SalesOrder(
        organization_id=org.id,
        order_number="SO-OV-03",
        sales_order_number="SO-10001-2026-00003",
        customer_id=cust.id,
        salesperson_id=salesp.id,
        status="processing",
        fulfilment_status="partially_delivered",
        total=2000.0,
        order_date=datetime.now(timezone.utc),
    )
    db.add(so3)
    db.flush()
    item3 = SalesOrderItem(
        order_id=so3.id,
        product_id=prod.id,
        product_name=prod.name,
        quantity=20,
        unit_price=100.0,
        delivered_quantity=12,
        line_total=2000.0,
    )
    db.add(item3)
    deliv3_a = Delivery(
        organization_id=org.id,
        sales_order_id=so3.id,
        customer_id=cust.id,
        delivery_note_number="DN-OV-03A",
        delivery_partner_id=driver.id,
        vehicle_id=vehicle.id,
        status="delivered",
        delivery_date=datetime.now(timezone.utc) - timedelta(days=2),
    )
    deliv3_b = Delivery(
        organization_id=org.id,
        sales_order_id=so3.id,
        customer_id=cust.id,
        delivery_note_number="DN-OV-03B",
        delivery_partner_id=driver.id,
        vehicle_id=vehicle.id,
        status="in_transit",
        delivery_date=datetime.now(timezone.utc) - timedelta(days=1),
    )
    deliv3_cancelled = Delivery(
        organization_id=org.id,
        sales_order_id=so3.id,
        customer_id=cust.id,
        delivery_note_number="DN-OV-03C-VOID",
        status="cancelled",
        delivery_date=datetime.now(timezone.utc),
    )
    db.add_all([deliv3_a, deliv3_b, deliv3_cancelled])

    # Order 4: Cancelled order
    so4_cancelled = SalesOrder(
        organization_id=org.id,
        order_number="SO-OV-04-CANC",
        sales_order_number="SO-10001-2026-00004",
        customer_id=cust.id,
        status="cancelled",
        fulfilment_status="not_started",
        total=5000.0,
        order_date=datetime.now(timezone.utc),
    )
    db.add(so4_cancelled)
    db.commit()

    # Query Sales Overview
    res = client.get("/reports/sales-overview")
    assert res.status_code == 200
    data = res.json()

    assert data["type"] == "sales-overview"
    summary = data["summary"]
    assert summary["total_orders"] >= 4
    # Cancelled order 4 is excluded from total_sales_value (1500 + 800 + 2000 = 4300)
    assert summary["total_sales_value"] >= 4300.0
    assert summary["cancelled_orders"] >= 1
    assert summary["not_started"] >= 1
    assert summary["planned"] >= 1
    assert summary["partially_delivered"] >= 1

    # Find rows by order_number
    rows = data["rows"]
    r1 = next(r for r in rows if r["order_number"] == "SO-OV-01")
    assert r1["delivery_count"] == 0
    assert r1["ordered_quantity"] == 15.0
    assert r1["delivered_quantity"] == 0.0
    assert r1["remaining_quantity"] == 15.0
    assert r1["latest_delivery"] is None

    r2 = next(r for r in rows if r["order_number"] == "SO-OV-02")
    assert r2["delivery_count"] == 1
    assert r2["latest_delivery"]["delivery_number"] == "DN-OV-02"
    assert r2["latest_delivery"]["delivery_partner"]["name"] == "Fast Driver"
    assert r2["latest_delivery"]["vehicle"]["vehicle_number"] == "MH-01-AB-1234"

    r3 = next(r for r in rows if r["order_number"] == "SO-OV-03")
    # Cancelled delivery is excluded from delivery_count (2 active: 3A and 3B)
    assert r3["delivery_count"] == 2
    assert r3["ordered_quantity"] == 20.0
    assert r3["delivered_quantity"] == 12.0
    assert r3["remaining_quantity"] == 8.0
    # Latest active delivery is 3B (in_transit)
    assert r3["latest_delivery"]["delivery_number"] == "DN-OV-03B"
    assert r3["latest_delivery"]["status"] == "in_transit"

    # Test filtering by search
    res_search = client.get("/reports/sales-overview?search=DN-OV-03A")
    assert res_search.status_code == 200
    search_rows = res_search.json()["rows"]
    assert any(r["order_number"] == "SO-OV-03" for r in search_rows)

    # Test filtering by fulfilment_status
    res_f = client.get("/reports/sales-overview?fulfilment_status=partially_delivered")
    assert res_f.status_code == 200
    assert any(r["order_number"] == "SO-OV-03" for r in res_f.json()["rows"])


def test_cash_flow_sheet_recorded_cash_movements(client, db, test_data):
    """Test Cash Flow Sheet: verifies cash inflow from CustomerPayment, outflow from SupplierPayment & paid Expense,
    non-cash exclusion, matching with Daily Transaction semantics, running balance, and exports."""
    org = test_data["org"]
    cust = test_data["customer"]
    sup = test_data["supplier"]

    # 1. Invoice alone (Accrual - must NOT create cash inflow)
    inv_accrual = Invoice(
        organization_id=org.id,
        invoice_number="INV-CF-ACCRUAL",
        customer_id=cust.id,
        subtotal=10000.0,
        total=10000.0,
        amount_paid=0.0,
        status="issued",
        is_credit_note=False,
        invoice_date=datetime.now(timezone.utc),
    )
    db.add(inv_accrual)

    # 2. Real Customer Payment = +4000.0 (Cash In)
    cp = CustomerPayment(
        organization_id=org.id,
        customer_id=cust.id,
        amount=4000.0,
        payment_mode="upi",
        reference="UPI-CF-001",
        received_on=datetime.now(timezone.utc) - timedelta(hours=3),
    )
    db.add(cp)

    # 3. Non-void Supplier Payment = -1500.0 (Cash Out)
    sp = SupplierPayment(
        organization_id=org.id,
        supplier_id=sup.id,
        amount=1500.0,
        payment_mode="bank_transfer",
        reference="NEFT-CF-001",
        status="recorded",
        paid_on=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    db.add(sp)

    # 4. Void Supplier Payment = -9999.0 (Must be EXCLUDED)
    sp_void = SupplierPayment(
        organization_id=org.id,
        supplier_id=sup.id,
        amount=9999.0,
        payment_mode="cash",
        reference="VOID-PAY",
        status="void",
        paid_on=datetime.now(timezone.utc),
    )
    db.add(sp_void)

    # 5. Unpaid approved Expense = -5000.0 (Must be EXCLUDED from cash outflow)
    exp_unpaid = Expense(
        organization_id=org.id,
        category="Office Rent",
        amount=5000.0,
        status="approved",
        payment_status="pending",
        expense_date=datetime.now(timezone.utc),
    )
    db.add(exp_unpaid)

    # 6. Paid approved Expense = -500.0 (Cash Out)
    exp_paid = Expense(
        organization_id=org.id,
        category="Stationery",
        amount=500.0,
        status="approved",
        payment_status="paid",
        expense_date=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    db.add(exp_paid)
    db.commit()

    # Query Cash Flow Sheet
    res_cf = client.get("/reports/cash-flow-sheet")
    assert res_cf.status_code == 200
    data_cf = res_cf.json()

    # Query Daily Transaction to compare exact matching
    res_dt = client.get("/reports/daily-transaction")
    assert res_dt.status_code == 200
    data_dt = res_dt.json()

    # Cash in = 4000.0
    assert data_cf["summary"]["cash_in"] >= 4000.0
    # Cash out = 1500 (SP) + 500 (Paid Exp) = 2000.0
    assert data_cf["summary"]["cash_out"] >= 2000.0
    assert data_cf["summary"]["net_cash_flow"] == round(data_cf["summary"]["cash_in"] - data_cf["summary"]["cash_out"], 2)

    # Must match Daily Transaction cash_in and cash_out exactly
    assert data_cf["summary"]["cash_in"] == data_dt["summary"]["cash_in"]
    assert data_cf["summary"]["cash_out"] == data_dt["summary"]["cash_out"]
    assert data_cf["summary"]["net_cash_flow"] == data_dt["summary"]["net_cash_flow"]

    # Check rows
    rows = data_cf["rows"]
    assert len(rows) >= 3
    # Check that void supplier payment and unpaid expense are NOT in cash flow rows
    ref_list = [r["reference_number"] for r in rows]
    assert "VOID-PAY" not in ref_list

    # Check export endpoints
    res_xlsx = client.get("/reports/cash-flow-sheet/export?format=excel")
    assert res_xlsx.status_code == 200
    assert len(res_xlsx.content) > 0

    res_pdf = client.get("/reports/cash-flow-sheet/export?format=pdf")
    assert res_pdf.status_code == 200
    assert len(res_pdf.content) > 0




# ---------------------------------------------------------------------------
# GST transaction view, ignored-filter fixes, date validation, export parity
# ---------------------------------------------------------------------------

from io import BytesIO

from app.models import PurchaseReturnItem

IST = ZoneInfo("Asia/Kolkata")


def _ist(y, m, d, hh=12, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=IST).astimezone(timezone.utc)


CUST_A_GSTIN = "27AAACA1111A1Z5"
CUST_B_GSTIN = "29BBBCB2222B1Z6"
SUP_GSTIN = "27SUPPL3333C1Z7"


@pytest.fixture
def gst_data(db, test_data):
    org, org2 = test_data["org"], test_data["org2"]
    cust_a, sup = test_data["customer"], test_data["supplier"]
    cust_a.gst_number = CUST_A_GSTIN
    sup.gst_number = SUP_GSTIN
    cust_b = Customer(id=str(uuid.uuid4()), organization_id=org.id, name="Beta Traders", phone="9000000001", gst_number=CUST_B_GSTIN)
    db.add(cust_b)

    def invoice(number, customer_id, when, subtotal, tax, status, org_id=org.id, walk_in=None):
        inv = Invoice(
            organization_id=org_id, invoice_number=number, customer_id=customer_id, walk_in_name=walk_in,
            subtotal=subtotal, tax=tax, total=subtotal + tax, amount_paid=(subtotal + tax) if status == "paid" else 0.0,
            status=status, is_credit_note=False, invoice_date=when,
        )
        db.add(inv)
        return inv

    invoice("INV-GST-001", cust_a.id, _ist(2026, 3, 5), 1000.0, 180.0, "unpaid")
    inv2 = invoice("INV-GST-002", cust_b.id, _ist(2026, 3, 15), 5000.0, 900.0, "paid")
    invoice("INV-GST-003", None, _ist(2026, 4, 2), 200.0, 10.0, "paid", walk_in="Counter Sale")
    invoice("INV-GST-VOID", cust_a.id, _ist(2026, 3, 6), 999.0, 99.0, "void")
    invoice("INV-GST-FOREIGN", test_data["foreign_customer"].id, _ist(2026, 3, 7), 777.0, 77.0, "paid", org_id=org2.id)
    db.flush()

    sr = SalesReturn(
        organization_id=org.id, return_number="SR-GST-001", return_date=_ist(2026, 3, 20),
        customer_id=cust_b.id, invoice_reference_id=inv2.id, return_status="approved", credit_amount=1180.0,
    )
    db.add(sr)
    db.flush()
    db.add(ReturnItem(return_id=sr.id, product_name="Premium Widget", quantity_returned=10, unit_price=100.0, tax_rate=18.0, line_total=1000.0))

    pinv = PurchaseInvoice(
        organization_id=org.id, supplier_id=sup.id, invoice_number="PUR-GST-001", subtotal=3000.0, tax=540.0,
        total=3540.0, status="approved", invoice_date=_ist(2026, 3, 12), warehouse_id=test_data["warehouse"].id,
    )
    db.add(pinv)
    db.flush()
    db.add(SupplierInvoice(
        organization_id=org.id, supplier_id=sup.id, purchase_id=pinv.id, supplier_invoice_number="SINV-GST-001",
        subtotal=3000.0, tax_amount=540.0, grand_total=3540.0, status="recorded", supplier_invoice_date=_ist(2026, 3, 12),
    ))
    pr = PurchaseReturn(
        organization_id=org.id, return_number="PR-GST-001", purchase_id=pinv.id, supplier_id=sup.id,
        status="confirmed", return_date=_ist(2026, 3, 25),
    )
    db.add(pr)
    db.flush()
    db.add(PurchaseReturnItem(purchase_return_id=pr.id, product_name="Premium Widget", quantity=2, unit_price=100.0, tax_rate=18.0, tax_amount=36.0, line_total=236.0))
    db.commit()
    return {**test_data, "customer_b": cust_b, "purchase": pinv}


def _gst(client, **params):
    params.setdefault("group_by", "transaction")
    params.setdefault("page_size", 100)
    return client.get("/reports/gst-summary", params=params)


def _docs(res):
    assert res.status_code == 200, res.text
    return sorted(r["document_number"] for r in res.json()["rows"])


def test_gst_summary_default_unchanged(client, gst_data):
    res = client.get("/reports/gst-summary")
    assert res.status_code == 200
    data = res.json()
    assert data["meta"]["group_by"] == "summary"
    assert set(data["summary"]) == {"output_gst", "input_gst", "gst_adjustments", "net_gst"}
    # Output 180 + 900 + 10 = 1090 less sales-return tax 180; input 540 less stored purchase-return tax 36.
    assert data["summary"] == {"output_gst": 910.0, "input_gst": 504.0, "gst_adjustments": 216.0, "net_gst": 406.0}
    rows = data["rows"]
    assert len(rows) == 4
    assert (rows[0]["taxable_value"], rows[0]["tax_amount"]) == (6200.0, 1090.0)
    # Sales-return taxable value is the pre-tax line total, not the tax-inclusive credit (1180).
    assert (rows[1]["taxable_value"], rows[1]["tax_amount"]) == (-1000.0, -180.0)
    assert (rows[2]["taxable_value"], rows[2]["tax_amount"]) == (3000.0, 540.0)
    # Net taxable = (6200 - 1000) - (3000 - 200)
    assert (rows[3]["taxable_value"], rows[3]["tax_amount"]) == (2400.0, 406.0)


def test_gst_summary_rejects_transaction_filters(client, gst_data):
    res = client.get("/reports/gst-summary", params={"customer_id": gst_data["customer"].id})
    assert res.status_code == 400
    assert "group_by=transaction" in res.json()["detail"]
    assert client.get("/reports/gst-summary", params={"group_by": "bogus"}).status_code == 400
    # Filters the transaction view cannot honour are rejected, not ignored.
    assert _gst(client, warehouse_id=gst_data["warehouse"].id).status_code == 400


def test_gst_transactions_rows_and_totals_match_summary(client, gst_data):
    res = _gst(client)
    assert _docs(res) == ["INV-GST-001", "INV-GST-002", "INV-GST-003", "PR-GST-001", "SINV-GST-001", "SR-GST-001"]
    data = res.json()
    assert data["meta"]["group_by"] == "transaction"
    by_doc = {r["document_number"]: r for r in data["rows"]}
    assert by_doc["SR-GST-001"] == {
        "document_type": "sales_return", "gst_classification": "output_adjustment", "source_id": by_doc["SR-GST-001"]["source_id"],
        "document_number": "SR-GST-001", "document_date": "2026-03-20", "reference_document": "INV-GST-002",
        "party_type": "customer", "party_id": gst_data["customer_b"].id, "party_name": "Beta Traders",
        "gst_number": CUST_B_GSTIN, "status": "approved", "taxable_value": 1000.0, "tax_amount": 180.0, "total_amount": 1180.0,
    }
    pr = by_doc["PR-GST-001"]
    assert (pr["taxable_value"], pr["tax_amount"], pr["total_amount"]) == (200.0, 36.0, 236.0)
    assert pr["reference_document"] == "PUR-GST-001"
    assert by_doc["INV-GST-003"]["party_name"] == "Counter Sale" and by_doc["INV-GST-003"]["gst_number"] is None

    s = data["summary"]
    aggregate = client.get("/reports/gst-summary").json()
    summary_rows = aggregate["rows"]
    assert s["output_tax_amount"] == summary_rows[0]["tax_amount"] == 1090.0
    assert s["output_taxable_value"] == summary_rows[0]["taxable_value"]
    assert s["output_adjustment_tax_amount"] == -summary_rows[1]["tax_amount"] == 180.0
    assert s["output_adjustment_taxable_value"] == -summary_rows[1]["taxable_value"] == 1000.0
    assert s["input_tax_amount"] == summary_rows[2]["tax_amount"] == 540.0
    assert s["input_adjustment_tax_amount"] == 36.0
    assert s["input_adjustment_taxable_value"] == 200.0
    # Both views reconcile: same net liability, same return-tax treatment.
    assert s["net_tax_amount"] == aggregate["summary"]["net_gst"] == summary_rows[3]["tax_amount"] == 406.0
    assert s["input_tax_amount"] - s["input_adjustment_tax_amount"] == aggregate["summary"]["input_gst"]
    assert s["output_adjustment_tax_amount"] + s["input_adjustment_tax_amount"] == aggregate["summary"]["gst_adjustments"]
    assert s["row_count"] == 6


def test_gst_transactions_date_range(client, gst_data):
    assert "INV-GST-003" not in _docs(_gst(client, date_from="2026-03-01", date_to="2026-03-31"))
    assert _docs(_gst(client, date_from="2026-04-02", date_to="2026-04-02")) == ["INV-GST-003"]
    res = _gst(client, date_from="2026-04-01", date_to="2026-03-01")
    assert res.status_code == 400
    assert "date_from cannot be after date_to" in res.json()["detail"]


def test_gst_transactions_customer_and_supplier_filters(client, gst_data):
    assert _docs(_gst(client, customer_id=gst_data["customer_b"].id)) == ["INV-GST-002", "SR-GST-001"]
    assert _docs(_gst(client, supplier_id=gst_data["supplier"].id)) == ["PR-GST-001", "SINV-GST-001"]
    # Party filters cannot be combined with each other or with the other side's documents.
    assert _gst(client, customer_id=gst_data["customer"].id, supplier_id=gst_data["supplier"].id).status_code == 400
    assert _gst(client, customer_id=gst_data["customer"].id, invoice_type="supplier_invoice").status_code == 400
    assert _gst(client, supplier_id=gst_data["supplier"].id, gst_classification="output").status_code == 400


def test_gst_transactions_document_reference_and_search(client, gst_data):
    # A return matches through the invoice it references.
    assert _docs(_gst(client, invoice_number="inv-gst-002")) == ["INV-GST-002", "SR-GST-001"]
    assert _docs(_gst(client, invoice_number="PR-GST")) == ["PR-GST-001"]
    assert _docs(_gst(client, search="beta")) == ["INV-GST-002", "SR-GST-001"]
    assert _docs(_gst(client, search="global supplies")) == ["PR-GST-001", "SINV-GST-001"]


def test_gst_transactions_gst_number(client, gst_data):
    assert _docs(_gst(client, gst_number=CUST_A_GSTIN.lower())) == ["INV-GST-001"]  # void invoice excluded
    assert _docs(_gst(client, gst_number=SUP_GSTIN)) == ["PR-GST-001", "SINV-GST-001"]
    assert _gst(client, gst_number="27-AA").status_code == 400


def test_gst_transactions_classification_type_and_status(client, gst_data):
    assert _docs(_gst(client, gst_classification="input")) == ["SINV-GST-001"]
    assert _docs(_gst(client, gst_classification="output,output_adjustment")) == ["INV-GST-001", "INV-GST-002", "INV-GST-003", "SR-GST-001"]
    assert _docs(_gst(client, invoice_type="sales_return,purchase_return")) == ["PR-GST-001", "SR-GST-001"]
    assert _docs(_gst(client, status="paid")) == ["INV-GST-002", "INV-GST-003"]
    assert _docs(_gst(client, status="confirmed")) == ["PR-GST-001"]
    for bad in ({"gst_classification": "igst"}, {"invoice_type": "quotation"}, {"status": "void"}, {"status": "draft"}):
        res = _gst(client, **bad)
        assert res.status_code == 400, bad
        assert "Allowed" in res.json()["detail"]


def test_gst_transactions_amount_ranges(client, gst_data):
    assert _docs(_gst(client, min_taxable_value=1000)) == ["INV-GST-001", "INV-GST-002", "SINV-GST-001", "SR-GST-001"]
    assert _docs(_gst(client, max_tax_amount=100)) == ["INV-GST-003", "PR-GST-001"]
    assert _docs(_gst(client, min_amount=500, max_amount=2000)) == ["INV-GST-001", "SR-GST-001"]
    res = _gst(client, min_tax_amount=500, max_tax_amount=100)
    assert res.status_code == 400
    assert "cannot be greater than" in res.json()["detail"]
    assert _gst(client, min_amount=-1).status_code == 422


def test_gst_transactions_combined_filters_sort_and_pagination(client, gst_data):
    res = _gst(client, date_from="2026-03-01", date_to="2026-03-31", gst_classification="output", min_tax_amount=500)
    assert _docs(res) == ["INV-GST-002"]
    assert res.json()["summary"]["output_tax_amount"] == 900.0

    rows = _gst(client, sort_by="tax_amount", sort_order="asc").json()["rows"]
    assert [r["tax_amount"] for r in rows] == sorted(r["tax_amount"] for r in rows)
    rows = _gst(client).json()["rows"]
    assert [r["document_date"] for r in rows] == sorted((r["document_date"] for r in rows), reverse=True)
    assert _gst(client, sort_by="bogus").status_code == 400
    assert _gst(client, sort_order="sideways").status_code == 400

    page = _gst(client, page=2, page_size=4).json()
    assert page["pagination"] == {"page": 2, "page_size": 4, "total": 6, "total_pages": 2}
    assert len(page["rows"]) == 2
    assert page["summary"]["row_count"] == 6


def test_gst_transactions_cross_org_isolation(client, gst_data):
    assert "INV-GST-FOREIGN" not in _docs(_gst(client))
    foreign = gst_data["foreign_customer"].id
    assert _gst(client, customer_id=foreign).status_code == 404
    res = client.get("/reports/gst-summary/export", params={"group_by": "transaction", "customer_id": foreign})
    assert res.status_code == 404


def _xlsx_table(content: bytes, first_header: str) -> list[dict]:
    from openpyxl import load_workbook

    ws = load_workbook(BytesIO(content)).active
    values = [list(r) for r in ws.iter_rows(values_only=True)]
    start = next(i for i, r in enumerate(values) if r and r[0] == first_header)
    headers = values[start]
    return [dict(zip(headers, r)) for r in values[start + 1:] if any(v is not None for v in r)]


GST_COMBINED = {
    "group_by": "transaction", "date_from": "2026-03-01", "date_to": "2026-03-31",
    "gst_classification": "output,output_adjustment,input_adjustment", "max_tax_amount": 900,
}


def test_gst_excel_export_matches_unpaginated_api(client, gst_data):
    # Screen view paginated at 2 rows; the export must still carry every filtered row.
    api_page = client.get("/reports/gst-summary", params={**GST_COMBINED, "page_size": 2}).json()
    api_all = client.get("/reports/gst-summary", params={**GST_COMBINED, "page_size": 100}).json()
    assert api_page["pagination"]["total"] == 4 and len(api_page["rows"]) == 2

    res = client.get("/reports/gst-summary/export", params={**GST_COMBINED, "format": "excel"})
    assert res.status_code == 200
    table = _xlsx_table(res.content, "Date")
    assert sorted(r["Document #"] for r in table) == sorted(r["document_number"] for r in api_all["rows"])
    assert sorted(r["Tax Amount"] for r in table) == sorted(r["tax_amount"] for r in api_all["rows"])
    assert "INV-GST-003" not in {r["Document #"] for r in table}  # outside date range
    assert "SINV-GST-001" not in {r["Document #"] for r in table}  # excluded classification


def test_gst_pdf_export_uses_filtered_dataset(client, gst_data, monkeypatch):
    from app.routers import reports as reports_router

    captured = {}
    real_to_pdf = reports_router._to_pdf

    def spy(report, org_name=None):
        captured["report"] = report
        return real_to_pdf(report, org_name=org_name)

    monkeypatch.setattr(reports_router, "_to_pdf", spy)
    res = client.get("/reports/gst-summary/export", params={**GST_COMBINED, "format": "pdf"})
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/pdf"
    api_all = client.get("/reports/gst-summary", params={**GST_COMBINED, "page_size": 100}).json()
    assert captured["report"]["rows"] == api_all["rows"]
    assert captured["report"]["summary"] == api_all["summary"]


def test_reversed_date_range_rejected_and_boundaries_inclusive(client, db, test_data):
    for path in ("/reports/sales", "/reports/purchase", "/reports/expense", "/reports/gst-summary", "/reports/sales/export"):
        res = client.get(path, params={"date_from": "2026-05-02", "date_to": "2026-05-01"})
        assert res.status_code == 400, path
        assert res.json()["detail"] == "date_from cannot be after date_to"

    org, cust = test_data["org"], test_data["customer"]
    for number, when in (
        ("INV-BND-START", _ist(2026, 5, 1, 0, 0)),     # first instant of the IST day
        ("INV-BND-END", _ist(2026, 5, 1, 23, 59)),     # last minute of the IST day
        ("INV-BND-BEFORE", _ist(2026, 4, 30, 23, 59)),  # still 30 April in Kolkata
        ("INV-BND-AFTER", _ist(2026, 5, 2, 0, 0)),
    ):
        db.add(Invoice(organization_id=org.id, invoice_number=number, customer_id=cust.id, subtotal=10.0, total=10.0,
                       status="unpaid", is_credit_note=False, invoice_date=when))
    db.commit()
    res = client.get("/reports/sales", params={"date_from": "2026-05-01", "date_to": "2026-05-01"})
    assert sorted(r["invoice_number"] for r in res.json()["rows"]) == ["INV-BND-END", "INV-BND-START"]


def test_sales_warehouse_filter(client, db, test_data):
    org, cust, wh1 = test_data["org"], test_data["customer"], test_data["warehouse"]
    wh2 = Warehouse(id=str(uuid.uuid4()), organization_id=org.id, name="Second Hub", code="WH-02", is_active=True)
    foreign_wh = Warehouse(id=str(uuid.uuid4()), organization_id=test_data["org2"].id, name="Foreign Hub", code="WH-F", is_active=True)
    db.add_all([wh2, foreign_wh])
    so_wh1 = SalesOrder(organization_id=org.id, order_number="SO-WH-1", customer_id=cust.id, warehouse_id=wh1.id, total=100.0)
    so_wh2 = SalesOrder(organization_id=org.id, order_number="SO-WH-2", customer_id=cust.id, warehouse_id=wh2.id, total=100.0)
    db.add_all([so_wh1, so_wh2])
    db.flush()
    dl_wh1 = Delivery(organization_id=org.id, delivery_note_number="DN-WH-1", sales_order_id=so_wh2.id, warehouse_id=wh1.id)
    dl_none = Delivery(organization_id=org.id, delivery_note_number="DN-WH-0", sales_order_id=so_wh1.id)
    db.add_all([dl_wh1, dl_none])
    db.flush()

    def inv(number, order_id=None, delivery_id=None):
        db.add(Invoice(organization_id=org.id, invoice_number=number, customer_id=cust.id, order_id=order_id,
                       delivery_id=delivery_id, subtotal=100.0, total=100.0, status="unpaid", is_credit_note=False))

    inv("INV-WH-DELIVERY", order_id=so_wh2.id, delivery_id=dl_wh1.id)   # delivery warehouse wins over order's
    inv("INV-WH-ORDER2", order_id=so_wh2.id)                              # no delivery: order warehouse
    inv("INV-WH-FALLBACK", order_id=so_wh1.id, delivery_id=dl_none.id)   # delivery without warehouse: order's
    inv("INV-WH-NONE")                                                    # no association at all
    db.commit()

    def numbers(wh_id):
        res = client.get("/reports/sales", params={"warehouse_id": wh_id, "page_size": 100})
        assert res.status_code == 200, res.text
        return sorted(r["invoice_number"] for r in res.json()["rows"])

    assert numbers(wh1.id) == ["INV-WH-DELIVERY", "INV-WH-FALLBACK"]
    assert numbers(wh2.id) == ["INV-WH-ORDER2"]
    assert len(client.get("/reports/sales", params={"page_size": 100}).json()["rows"]) == 4
    # Grouped views apply the same filter.
    grouped = client.get("/reports/sales", params={"warehouse_id": wh1.id, "group_by": "salesperson"}).json()
    assert sum(r["transaction_count"] for r in grouped["rows"]) == 2
    assert client.get("/reports/sales", params={"warehouse_id": foreign_wh.id}).status_code == 404


def test_sales_salesperson_group_falls_back_to_creator(client, db, test_data):
    org, user = test_data["org"], test_data["user"]
    for idx in range(3):
        db.add(Invoice(organization_id=org.id, invoice_number=f"INV-CRT-{idx}", subtotal=50.0, total=50.0,
                       status="unpaid", is_credit_note=False, created_by=user.id))
    db.add(Invoice(organization_id=org.id, invoice_number="INV-CRT-NONE", subtotal=5.0, total=5.0,
                   status="unpaid", is_credit_note=False))
    db.commit()
    rows = client.get("/reports/sales", params={"group_by": "salesperson"}).json()["rows"]
    assert {r["salesperson"]: r["transaction_count"] for r in rows} == {"Test Analyst": 3, "Unassigned": 1}


def test_purchase_warehouse_filter(client, db, test_data):
    org, sup, wh = test_data["org"], test_data["supplier"], test_data["warehouse"]
    for number, wh_id in (("PUR-WH-1", wh.id), ("PUR-WH-NONE", None)):
        pinv = PurchaseInvoice(organization_id=org.id, supplier_id=sup.id, invoice_number=number, subtotal=100.0,
                               total=100.0, status="approved", warehouse_id=wh_id)
        db.add(pinv)
        db.flush()
        db.add(SupplierInvoice(organization_id=org.id, supplier_id=sup.id, purchase_id=pinv.id,
                               supplier_invoice_number=f"S{number}", subtotal=100.0, grand_total=100.0, status="recorded"))
    db.commit()
    res = client.get("/reports/purchase", params={"warehouse_id": wh.id})
    assert res.status_code == 200
    assert [r["invoice_number"] for r in res.json()["rows"]] == ["SPUR-WH-1"]
    assert res.json()["summary"]["total_purchases"] == 100.0
    assert len(client.get("/reports/purchase").json()["rows"]) == 2
    foreign_wh = Warehouse(id=str(uuid.uuid4()), organization_id=test_data["org2"].id, name="F", code="WH-F2")
    db.add(foreign_wh)
    db.commit()
    assert client.get("/reports/purchase", params={"warehouse_id": foreign_wh.id}).status_code == 404


def test_expense_submitted_by_filter(client, db, test_data):
    org, user = test_data["org"], test_data["user"]
    other = User(id=str(uuid.uuid4()), email="other@example.com", name="Other", password_hash="x",
                 organization_id=org.id, role="admin", is_active=True)
    foreign = User(id=str(uuid.uuid4()), email="foreign@example.com", name="Foreign", password_hash="x",
                   organization_id=test_data["org2"].id, role="admin", is_active=True)
    db.add_all([other, foreign])
    db.add_all([
        Expense(organization_id=org.id, category="Travel", amount=500.0, status="approved", submitted_by=user.id),
        Expense(organization_id=org.id, category="Food", amount=200.0, status="approved", submitted_by=other.id),
    ])
    db.commit()
    res = client.get("/reports/expense", params={"submitted_by": user.id})
    assert res.status_code == 200
    assert [r["category"] for r in res.json()["rows"]] == ["Travel"]
    assert res.json()["summary"]["approved_expense"] == 500.0
    assert client.get("/reports/expense", params={"submitted_by": foreign.id}).status_code == 404
    export = client.get("/reports/expense/export", params={"submitted_by": user.id, "format": "excel"})
    assert [r["Category"] for r in _xlsx_table(export.content, "Date")] == ["Travel"]


def test_purchase_return_purchase_id_filter(client, db, test_data):
    org, org2, sup = test_data["org"], test_data["org2"], test_data["supplier"]
    p1 = PurchaseInvoice(organization_id=org.id, supplier_id=sup.id, invoice_number="PUR-RET-1", status="approved")
    p2 = PurchaseInvoice(organization_id=org.id, supplier_id=sup.id, invoice_number="PUR-RET-2", status="approved")
    pf = PurchaseInvoice(organization_id=org2.id, invoice_number="PUR-RET-F", status="approved")
    db.add_all([p1, p2, pf])
    db.flush()
    db.add_all([
        PurchaseReturn(organization_id=org.id, return_number="PR-P1", purchase_id=p1.id, supplier_id=sup.id, status="confirmed"),
        PurchaseReturn(organization_id=org.id, return_number="PR-P2", purchase_id=p2.id, supplier_id=sup.id, status="confirmed"),
    ])
    db.commit()
    res = client.get("/reports/purchase-return", params={"purchase_id": p1.id})
    assert res.status_code == 200
    assert [(r["return_number"], r["purchase_reference"]) for r in res.json()["rows"]] == [("PR-P1", "PUR-RET-1")]
    assert len(client.get("/reports/purchase-return").json()["rows"]) == 2
    assert client.get("/reports/purchase-return", params={"purchase_id": pf.id}).status_code == 404


def test_export_rejects_unknown_format(client):
    res = client.get("/reports/sales/export", params={"format": "csv"})
    assert res.status_code == 400
    assert "format must be" in res.json()["detail"]


def test_gst_summary_excel_and_pdf_match_corrected_aggregate(client, gst_data, monkeypatch):
    from app.routers import reports as reports_router

    api = client.get("/reports/gst-summary", params={"date_from": "2026-03-01", "date_to": "2026-03-31"}).json()
    params = {"date_from": "2026-03-01", "date_to": "2026-03-31"}
    table = _xlsx_table(client.get("/reports/gst-summary/export", params={**params, "format": "excel"}).content, "GST Classification")
    assert [(r["Taxable Value"], r["Tax Amount"]) for r in table] == [(r["taxable_value"], r["tax_amount"]) for r in api["rows"]]
    assert api["summary"]["input_gst"] == 504.0

    captured = {}
    real_to_pdf = reports_router._to_pdf
    monkeypatch.setattr(reports_router, "_to_pdf", lambda report, org_name=None: captured.setdefault("r", report) and real_to_pdf(report, org_name))
    assert client.get("/reports/gst-summary/export", params={**params, "format": "pdf"}).status_code == 200
    assert captured["r"]["rows"] == api["rows"] and captured["r"]["summary"] == api["summary"]


@pytest.mark.parametrize("path", ["/reports/{t}", "/reports/{t}/export"])
@pytest.mark.parametrize("report_type,params", [
    ("profit-loss", {"customer_id": "abc"}),
    ("daily-transaction", {"overdue_only": "true"}),
    ("sales", {"submitted_by": "abc"}),
    ("expense", {"warehouse_id": "abc"}),
    ("gst-summary", {"customer_id": "abc"}),
    ("gst-summary", {"group_by": "transaction", "product_id": "abc"}),
])
def test_unsupported_filters_rejected_on_screen_and_export(client, path, report_type, params):
    res = client.get(path.format(t=report_type), params=params)
    assert res.status_code == 400
    assert "not supported" in res.json()["detail"]


def test_supported_and_blank_filters_still_accepted(client):
    # Blank values and default booleans are not "supplied" filters.
    assert client.get("/reports/profit-loss", params={"search": "", "overdue_only": "false"}).status_code == 200
    assert client.get("/reports/sales-overview", params={"status": "placed"}).status_code == 200
    assert client.get("/reports/supplier-payment", params={"payment_method": "cash"}).status_code == 200
    assert client.get("/reports/inventory-summary", params={"brand_id": "none", "category_id": "none"}).status_code == 200


def test_invalid_validation_matches_between_screen_and_export(client):
    for params in ({"group_by": "transaction", "status": "void"}, {"group_by": "transaction", "min_tax_amount": 5, "max_tax_amount": 1}):
        screen = client.get("/reports/gst-summary", params=params)
        export = client.get("/reports/gst-summary/export", params=params)
        assert screen.status_code == export.status_code == 400
        assert screen.json()["detail"] == export.json()["detail"]
    # Bad format is rejected before any report work, even alongside other invalid input.
    res = client.get("/reports/sales/export", params={"format": "csv", "submitted_by": "abc"})
    assert res.status_code == 400
    assert res.json()["detail"] == "format must be 'excel' or 'pdf'"


def test_expense_vendor_filter(client, db, test_data):
    org, sup = test_data["org"], test_data["supplier"]
    other_vendor = Supplier(id=str(uuid.uuid4()), organization_id=org.id, name="Other Vendor", supplier_code="SUP-0002", phone="1")
    foreign_vendor = Supplier(id=str(uuid.uuid4()), organization_id=test_data["org2"].id, name="Foreign Vendor", supplier_code="SUP-F", phone="2")
    db.add_all([other_vendor, foreign_vendor])
    db.add_all([
        Expense(organization_id=org.id, category="Rent", amount=900.0, status="approved", vendor_id=sup.id),
        Expense(organization_id=org.id, category="Power", amount=100.0, status="approved", vendor_id=other_vendor.id),
    ])
    db.commit()
    res = client.get("/reports/expense", params={"vendor_id": sup.id})
    assert res.status_code == 200
    assert [(r["category"], r["vendor"]) for r in res.json()["rows"]] == [("Rent", "Global Supplies")]
    assert client.get("/reports/expense", params={"vendor_id": foreign_vendor.id}).status_code == 404
    # Vendor-linked expenses used to break these reports (Supplier object in rows / ilike on a relationship).
    res = client.get("/reports/expense", params={"search": "other vendor"})
    assert res.status_code == 200
    assert [r["category"] for r in res.json()["rows"]] == ["Power"]
    for report in ("daily-transaction", "cash-flow-sheet"):
        res = client.get(f"/reports/{report}", params={"page_size": 100})
        assert res.status_code == 200, report
