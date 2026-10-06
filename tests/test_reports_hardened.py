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


@pytest.fixture
def test_data(db):
    org = Organization(
        id=str(uuid.uuid4()),
        name="Test Reports Org",
        company_code="CMP-10001",
        timezone="Asia/Kolkata",
    )
    db.add(org)

    org2 = Organization(
        id=str(uuid.uuid4()),
        name="Foreign Org",
        company_code="CMP-20002",
        timezone="Asia/Kolkata",
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


def test_registry_contains_all_15_reports():
    assert len(report_service.REPORT_TYPES) == 15
    assert len(report_service.REPORT_REGISTRY) == 15
    expected_reports = {
        "daily-transaction", "sales", "purchase", "customer-outstanding",
        "supplier-outstanding", "payment-collection", "expense", "cash-collection",
        "gst-summary", "sales-return", "purchase-return", "profit-loss",
        "supplier-payment", "inventory-summary", "stock-movement",
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
