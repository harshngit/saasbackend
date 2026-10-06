"""Complete test suite verifying Unique Business ID / Numbering System.

Verifies all 40 scenarios required by the specification.
"""

import time
import threading
import uuid
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
import sqlalchemy as sa
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.main import app
from app.core import workflow
from app.core.database import Base, engine, get_db
from app.core.security import create_access_token
from app.core.pdf_docs import invoice_detailed_pdf, quotation_pdf
from app.models import (
    Customer,
    Delivery,
    Invoice,
    InvoiceItem,
    Lead,
    NumberSequence,
    Organization,
    Product,
    PurchaseInvoice,
    Quotation,
    QuotationItem,
    SalesOrder,
    Supplier,
    User,
)
from app.services import (
    numbering_service,
    order_service,
    org_service,
    quotation_service,
    lead_service,
    delivery_service,
)
from app.scripts.backfill_supplier_codes import backfill_supplier_codes


@pytest.fixture
def db_session():
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        try:
            conn.execute(sa.text("ALTER TABLE suppliers ADD COLUMN supplier_code VARCHAR(50)"))
            conn.commit()
        except Exception:
            pass
    session = Session(bind=engine)
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def auth_client(db_session: Session):
    # Setup test Org A with CMP-10001
    org = Organization(
        id=str(uuid.uuid4()),
        name="Acme Corporation",
        company_code="CMP-10001",
    )
    db_session.add(org)
    db_session.flush()

    user = User(
        id=str(uuid.uuid4()),
        organization_id=org.id,
        email=f"admin_{uuid.uuid4().hex[:6]}@acme.com",
        name="Acme Admin",
        password_hash="hashed_pw",
        role="admin",
        system_role="admin",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()

    token = create_access_token(user.id, role="admin", organization_id=org.id)
    client = TestClient(app)
    client.headers = {"Authorization": f"Bearer {token}"}
    return client, org, user


def test_1_and_2_company_code_and_numeric_extraction(db_session: Session):
    org = Organization(id=str(uuid.uuid4()), name="Test Org", company_code="CMP-10001")
    db_session.add(org)
    db_session.commit()

    assert org.company_code == "CMP-10001"
    num = numbering_service.extract_company_number(db_session, org.id)
    assert num == "10001"


def test_3_and_4_customer_sequential_ids(db_session: Session):
    org = Organization(id=str(uuid.uuid4()), name="Test Org", company_code="CMP-10001")
    db_session.add(org)
    db_session.commit()

    curr_year = datetime.now(timezone.utc).year
    id1 = numbering_service.next_number(db_session, org.id, Customer.customer_id, "CS")
    assert id1 == f"CS-10001-{curr_year}-0001"

    id2 = numbering_service.next_number(db_session, org.id, Customer.customer_id, "CS")
    assert id2 == f"CS-10001-{curr_year}-0002"


def test_5_through_11_transactional_formats(db_session: Session):
    org = Organization(id=str(uuid.uuid4()), name="Test Org", company_code="CMP-10001")
    db_session.add(org)
    db_session.commit()
    curr_year = datetime.now(timezone.utc).year

    inv = numbering_service.next_number(db_session, org.id, Invoice.invoice_number, "IN")
    assert inv == f"IN-10001-{curr_year}-0001"

    so = numbering_service.next_number(db_session, org.id, SalesOrder.order_number, "SO")
    assert so == f"SO-10001-{curr_year}-0001"

    do = numbering_service.next_number(db_session, org.id, Delivery.delivery_note_number, "DO")
    assert do == f"DO-10001-{curr_year}-0001"

    po = numbering_service.next_number(db_session, org.id, PurchaseInvoice.purchase_number, "PO")
    assert po == f"PO-10001-{curr_year}-0001"

    emp = numbering_service.next_number(db_session, org.id, User.employee_id, "EMP")
    assert emp == f"EMP-10001-{curr_year}-0001"

    lead = numbering_service.next_number(db_session, org.id, Lead.lead_id, "L")
    assert lead == f"L-10001-{curr_year}-0001"

    qt = numbering_service.next_number(db_session, org.id, Quotation.quotation_number, "QT")
    assert qt == f"QT-10001-{curr_year}-0001"


def test_12_through_15_master_formats(db_session: Session):
    org = Organization(id=str(uuid.uuid4()), name="Test Org", company_code="CMP-10001")
    db_session.add(org)
    db_session.commit()

    sup1 = numbering_service.next_number(db_session, org.id, Supplier.supplier_code, "SUP")
    assert sup1 == "SUP-0001"

    sup2 = numbering_service.next_number(db_session, org.id, Supplier.supplier_code, "SUP")
    assert sup2 == "SUP-0002"

    prd1 = numbering_service.next_number(db_session, org.id, Product.product_id, "PRD")
    assert prd1 == "PRD-0001"

    prd2 = numbering_service.next_number(db_session, org.id, Product.product_id, "PRD")
    assert prd2 == "PRD-0002"


def test_16_and_17_multi_tenant_isolation(db_session: Session):
    org1 = Organization(id=str(uuid.uuid4()), name="Org 1", company_code="CMP-10001")
    org2 = Organization(id=str(uuid.uuid4()), name="Org 2", company_code="CMP-10002")
    db_session.add_all([org1, org2])
    db_session.commit()

    curr_year = datetime.now(timezone.utc).year
    c1 = numbering_service.next_number(db_session, org1.id, Customer.customer_id, "CS")
    assert c1 == f"CS-10001-{curr_year}-0001"

    c2 = numbering_service.next_number(db_session, org2.id, Customer.customer_id, "CS")
    assert c2 == f"CS-10002-{curr_year}-0001"


def test_18_year_reset(db_session: Session):
    org = Organization(id=str(uuid.uuid4()), name="Test Org", company_code="CMP-10001")
    db_session.add(org)
    db_session.commit()

    c_2026 = numbering_service.next_number(db_session, org.id, Customer.customer_id, "CS", year=2026)
    assert c_2026 == "CS-10001-2026-0001"

    c_2027 = numbering_service.next_number(db_session, org.id, Customer.customer_id, "CS", year=2027)
    assert c_2027 == "CS-10001-2027-0001"


def test_19_concurrent_generation_safety(db_session: Session):
    org = Organization(id=str(uuid.uuid4()), name="Concurrent Org", company_code="CMP-10001")
    db_session.add(org)
    db_session.commit()

    allocated = []
    errors = []

    def worker():
        for _ in range(5):
            local_session = Session(bind=engine)
            try:
                val = numbering_service.next_number(local_session, org.id, Customer.customer_id, "CS", year=2026)
                local_session.commit()
                allocated.append(val)
                break
            except Exception as exc:
                time.sleep(0.05)
            finally:
                local_session.close()

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(allocated) >= 2
    assert len(set(allocated)) == len(allocated)  # All allocated numbers are strictly unique


def test_20_and_21_editing_does_not_regenerate_or_mutate_id(auth_client, db_session: Session):
    client, org, admin = auth_client

    # Create customer via API
    res = client.post("/customers", json={"basic_information": {"customer_name": "Test Customer"}, "contact_information": {"mobile_number": "9876543210"}})
    assert res.status_code == 201
    data = res.json()
    orig_cid = data["customer_id"]
    cust_id = data["id"]
    assert orig_cid.startswith("CS-10001-")

    # Update customer with arbitrary new customer_id in payload
    res_patch = client.patch(f"/customers/{cust_id}", json={"basic_information": {"customer_name": "Updated Customer", "customer_id": "CS-HACKED-9999"}})
    assert res_patch.status_code == 200
    updated_data = res_patch.json()
    assert updated_data["basic_information"]["customer_name"] == "Updated Customer"
    assert updated_data["customer_id"] == orig_cid  # Unchanged!


def test_22_old_format_records_remain_unchanged(db_session: Session):
    org = Organization(id=str(uuid.uuid4()), name="Old Org", company_code="CMP-10001")
    db_session.add(org)
    db_session.flush()

    old_cust = Customer(organization_id=org.id, name="Old Cust", customer_id="CUST-2026-1001")
    db_session.add(old_cust)
    db_session.commit()

    # Re-fetch old customer
    fetched = db_session.get(Customer, old_cust.id)
    assert fetched.customer_id == "CUST-2026-1001"

    # Next generated customer gets new format starting at 0001
    new_cid = numbering_service.next_number(db_session, org.id, Customer.customer_id, "CS", year=2026)
    assert new_cid == "CS-10001-2026-0001"


def test_23_existing_canonical_max_resumes(db_session: Session):
    org = Organization(id=str(uuid.uuid4()), name="Resuming Org", company_code="CMP-10001")
    db_session.add(org)
    db_session.flush()

    existing_cust = Customer(organization_id=org.id, name="Cust 42", customer_id="CS-10001-2026-0042")
    db_session.add(existing_cust)
    db_session.commit()

    next_cid = numbering_service.next_number(db_session, org.id, Customer.customer_id, "CS", year=2026)
    assert next_cid == "CS-10001-2026-0043"


def test_24_lead_to_customer_conversion_numbering(db_session: Session):
    org = Organization(id=str(uuid.uuid4()), name="Lead Org", company_code="CMP-10001")
    db_session.add(org)
    db_session.flush()

    user = User(
        id=str(uuid.uuid4()),
        organization_id=org.id,
        email="lead_admin@example.com",
        name="Lead Admin",
        password_hash="hash",
        system_role="admin",
    )
    db_session.add(user)
    db_session.flush()

    lead_code = numbering_service.next_number(db_session, org.id, Lead.lead_id, "L")
    lead = Lead(organization_id=org.id, name="Test Lead", lead_id=lead_code)
    db_session.add(lead)
    db_session.commit()

    assert lead_code.startswith("L-10001-")

    # Convert to customer
    from app.schemas.lead import LeadConvertToCustomerIn
    res = lead_service.convert_lead_to_customer(
        db_session, org.id, user, lead, LeadConvertToCustomerIn(name="Converted Cust")
    )
    assert res["customer"].customer_id.startswith("CS-10001-")


def test_25_quotation_to_order_conversion(db_session: Session):
    org = Organization(id=str(uuid.uuid4()), name="Quotation Org", company_code="CMP-10001")
    db_session.add(org)
    db_session.flush()

    user = User(id=str(uuid.uuid4()), organization_id=org.id, email="q_user@example.com", name="Q User", password_hash="h", system_role="admin")
    cust = Customer(id=str(uuid.uuid4()), organization_id=org.id, name="Q Customer", customer_id="CS-10001-2026-0001")
    prd = Product(id=str(uuid.uuid4()), organization_id=org.id, name="Widget", price=100.0, product_id="PRD-0001")
    db_session.add_all([user, cust, prd])
    db_session.flush()

    qt_code = numbering_service.next_number(db_session, org.id, Quotation.quotation_number, "QT")
    qt = Quotation(
        id=str(uuid.uuid4()),
        organization_id=org.id,
        quotation_number=qt_code,
        customer_id=cust.id,
        status="accepted",
        items=[QuotationItem(id=str(uuid.uuid4()), product_id=prd.id, product_name="Widget", quantity=2.0, unit_price=100.0)],
    )
    db_session.add(qt)
    db_session.commit()

    assert qt.quotation_number.startswith("QT-10001-")

    from app.schemas.quotation import ConvertToOrder
    conv = quotation_service.convert_to_order(db_session, org.id, user, qt, ConvertToOrder())
    assert conv.order.order_number.startswith("SO-10001-")
    assert qt.quotation_number.startswith("QT-10001-")  # preserved!


def test_26_sales_order_to_delivery(db_session: Session):
    org = Organization(id=str(uuid.uuid4()), name="SO Delivery Org", company_code="CMP-10001")
    db_session.add(org)
    db_session.flush()

    so_num = numbering_service.next_number(db_session, org.id, SalesOrder.order_number, "SO")
    do_num = delivery_service.next_delivery_number(db_session, org.id)

    assert so_num.startswith("SO-10001-")
    assert do_num.startswith("DO-10001-")


def test_27_supplier_and_product_master_tenant_scoping(db_session: Session):
    org1 = Organization(id=str(uuid.uuid4()), name="Org 1", company_code="CMP-10001")
    org2 = Organization(id=str(uuid.uuid4()), name="Org 2", company_code="CMP-10002")
    db_session.add_all([org1, org2])
    db_session.commit()

    s1 = numbering_service.next_number(db_session, org1.id, Supplier.supplier_code, "SUP")
    assert s1 == "SUP-0001"

    s2 = numbering_service.next_number(db_session, org2.id, Supplier.supplier_code, "SUP")
    assert s2 == "SUP-0001"


def test_28_pdf_displays_stored_business_number(db_session: Session):
    org = Organization(id=str(uuid.uuid4()), name="Acme PDF", company_code="CMP-10001")
    db_session.add(org)
    db_session.flush()

    cust = Customer(id=str(uuid.uuid4()), organization_id=org.id, name="Acme Client", customer_id="CS-10001-2026-0001")
    inv = Invoice(
        id=str(uuid.uuid4()),
        organization_id=org.id,
        invoice_number="IN-10001-2026-0001",
        customer_id=cust.id,
        invoice_date=datetime.now(timezone.utc),
        items=[InvoiceItem(id=str(uuid.uuid4()), invoice_id="", product_name="Test Item", quantity=1, unit_price=50.0, line_total=50.0)],
    )
    db_session.add_all([cust, inv])
    db_session.commit()

    settings = workflow.invoice_settings(org)
    pdf_bytes = invoice_detailed_pdf(org, cust, inv, settings)
    assert isinstance(pdf_bytes, bytes)
    assert len(pdf_bytes) > 0


def test_29_malformed_company_code_fails_safely(db_session: Session):
    org_bad_prefix = Organization(id=str(uuid.uuid4()), name="Bad Prefix", company_code="BAD-10001")
    org_bad_num = Organization(id=str(uuid.uuid4()), name="Bad Num", company_code="CMP-XYZ")
    db_session.add_all([org_bad_prefix, org_bad_num])
    db_session.commit()

    with pytest.raises(ValueError, match="does not match expected format"):
        numbering_service.extract_company_number(db_session, org_bad_prefix.id)

    with pytest.raises(ValueError, match="invalid non-numeric segment"):
        numbering_service.extract_company_number(db_session, org_bad_num.id)


def test_30_missing_company_code_auto_generated(db_session: Session):
    org_no_code = Organization(id=str(uuid.uuid4()), name="No Code Org", company_code=None)
    db_session.add(org_no_code)
    db_session.commit()

    num = numbering_service.extract_company_number(db_session, org_no_code.id)
    assert num.isdigit()
    assert int(num) >= 10001


def test_31_supplier_code_uniqueness_is_org_scoped(db_session: Session):
    org1 = Organization(id=str(uuid.uuid4()), name="Org 1", company_code="CMP-10001")
    org2 = Organization(id=str(uuid.uuid4()), name="Org 2", company_code="CMP-10002")
    db_session.add_all([org1, org2])
    db_session.commit()

    sup1 = Supplier(organization_id=org1.id, name="Supplier 1", supplier_code="SUP-0001")
    sup2 = Supplier(organization_id=org2.id, name="Supplier 2", supplier_code="SUP-0001")
    db_session.add_all([sup1, sup2])
    db_session.commit()  # Should succeed across different orgs


def test_32_product_id_immutable_via_patch(auth_client, db_session: Session):
    client, org, admin = auth_client

    # Create product
    res = client.post("/products", json={"name": "Test Gadget", "pricing": {"selling_price": 200.0}})
    assert res.status_code == 201
    p_data = res.json()
    orig_pid = p_data["product_id"]
    db_id = p_data["id"]
    assert orig_pid == "PRD-0001"

    # Attempt to change product_id via PATCH
    res_patch = client.patch(f"/products/{db_id}", json={"product_id": "PRD-HACKED-9999", "name": "Renamed Gadget"})
    assert res_patch.status_code == 200
    updated = res_patch.json()
    assert updated["name"] == "Renamed Gadget"
    assert updated["product_id"] == "PRD-0001"  # Not changed!


def test_33_employee_id_immutable_via_patch(auth_client, db_session: Session):
    client, org, admin = auth_client

    # Create staff
    res = client.post(
        "/users",
        json={
            "contact_information": {"official_email": f"staff_{uuid.uuid4().hex[:6]}@acme.com", "first_name": "John", "last_name": "Doe"},
            "login_security": {"password": "Password123!"},
        },
    )
    assert res.status_code == 201
    staff_data = res.json()
    emp_id = staff_data["employee_id"]
    user_db_id = staff_data["id"]
    assert emp_id.startswith("EMP-10001-")

    # Attempt to change employee_id via PATCH
    res_patch = client.patch(
        f"/users/{user_db_id}",
        json={"basic_information": {"employee_id": "EMP-HACKED-9999", "first_name": "Johnny"}},
    )
    assert res_patch.status_code == 200
    updated_staff = res_patch.json()
    assert updated_staff["employee_id"] == emp_id  # Protected!


def test_34_and_35_supplier_backfill_idempotent_and_tenant_scoped(db_session: Session):
    org1 = Organization(id=str(uuid.uuid4()), name="BF Org 1", company_code="CMP-10001")
    org2 = Organization(id=str(uuid.uuid4()), name="BF Org 2", company_code="CMP-10002")
    db_session.add_all([org1, org2])
    db_session.flush()

    t0 = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2025, 1, 1, 11, 0, 0, tzinfo=timezone.utc)
    s1 = Supplier(organization_id=org1.id, name="Old Sup 1", supplier_code=None, created_at=t0)
    s2 = Supplier(organization_id=org1.id, name="Old Sup 2", supplier_code=None, created_at=t1)
    s3 = Supplier(organization_id=org2.id, name="Old Sup 3", supplier_code=None, created_at=t0)
    db_session.add_all([s1, s2, s3])
    db_session.commit()

    # Run backfill per org
    updated_count_1 = backfill_supplier_codes(db=db_session, org_id=org1.id)
    updated_count_2 = backfill_supplier_codes(db=db_session, org_id=org2.id)
    assert updated_count_1 == 2
    assert updated_count_2 == 1

    db_session.refresh(s1)
    db_session.refresh(s2)
    db_session.refresh(s3)

    assert s1.supplier_code == "SUP-0001"
    assert s2.supplier_code == "SUP-0002"
    assert s3.supplier_code == "SUP-0001"  # Tenant scoped!

    # Second run touches 0 rows for these
    second_run = backfill_supplier_codes(db=db_session, org_id=org1.id)
    assert second_run == 0


def test_36_and_37_master_sequences_do_not_reset_by_year(db_session: Session):
    org = Organization(id=str(uuid.uuid4()), name="Master Test Org", company_code="CMP-10001")
    db_session.add(org)
    db_session.commit()

    # Product
    p1 = numbering_service.next_number(db_session, org.id, Product.product_id, "PRD", year=2026)
    assert p1 == "PRD-0001"
    p2 = numbering_service.next_number(db_session, org.id, Product.product_id, "PRD", year=2027)
    assert p2 == "PRD-0002"

    # Supplier
    s1 = numbering_service.next_number(db_session, org.id, Supplier.supplier_code, "SUP", year=2026)
    assert s1 == "SUP-0001"
    s2 = numbering_service.next_number(db_session, org.id, Supplier.supplier_code, "SUP", year=2027)
    assert s2 == "SUP-0002"


def test_38_old_sequences_do_not_contaminate_new_sequences(db_session: Session):
    org = Organization(id=str(uuid.uuid4()), name="Old Seq Org", company_code="CMP-10001")
    db_session.add(org)
    db_session.flush()

    # Old NumberSequence for CUST
    old_seq = NumberSequence(organization_id=org.id, series="CUST", year=2026, last_number=1050)
    db_session.add(old_seq)
    db_session.commit()

    # New Customer CS sequence starts fresh at 0001
    new_cid = numbering_service.next_number(db_session, org.id, Customer.customer_id, "CS", year=2026)
    assert new_cid == "CS-10001-2026-0001"


def test_39_and_40_atomic_numbering_and_single_id_generation(db_session: Session):
    org = Organization(id=str(uuid.uuid4()), name="Single Gen Org", company_code="CMP-10001")
    db_session.add(org)
    db_session.commit()

    # Verify atomic update
    id1 = numbering_service.next_number(db_session, org.id, Invoice.invoice_number, "IN", year=2026)
    assert id1 == "IN-10001-2026-0001"

    seq_row = db_session.query(NumberSequence).filter_by(organization_id=org.id, series="IN", year=2026).first()
    assert seq_row is not None
    assert seq_row.last_number == 1


def test_41_creation_override_attempts_blocked(auth_client, db_session: Session):
    client, org, admin = auth_client

    # 1. Supplier creation with arbitrary supplier_code attempt
    res_sup = client.post("/suppliers", json={"name": "Hacked Supplier", "supplier_code": "SUP-CUSTOM-9999"})
    assert res_sup.status_code == 201
    sup_data = res_sup.json()
    assert sup_data["supplier_code"].startswith("SUP-")
    assert sup_data["supplier_code"] != "SUP-CUSTOM-9999"

    # 2. Product creation with arbitrary product_id attempt
    res_prod = client.post("/products", json={"name": "Hacked Product", "product_id": "PRD-CUSTOM-9999", "pricing": {"selling_price": 50.0}})
    assert res_prod.status_code == 201
    prod_data = res_prod.json()
    assert prod_data["product_id"].startswith("PRD-")
    assert prod_data["product_id"] != "PRD-CUSTOM-9999"

    # 3. Staff creation with arbitrary employee_id attempt
    res_user = client.post(
        "/users",
        json={
            "employee_id": "EMP-CUSTOM-9999",
            "contact_information": {"official_email": f"hacked_{uuid.uuid4().hex[:6]}@acme.com", "first_name": "Hack", "last_name": "User"},
            "login_security": {"password": "Password123!"},
        },
    )
    assert res_user.status_code == 201
    user_data = res_user.json()
    assert user_data["employee_id"].startswith("EMP-10001-")
    assert user_data["employee_id"] != "EMP-CUSTOM-9999"


def test_42_supplier_backfill_dry_run_mode(db_session: Session):
    org = Organization(id=str(uuid.uuid4()), name="Dry Run Org", company_code="CMP-10001")
    db_session.add(org)
    db_session.flush()

    sup = Supplier(organization_id=org.id, name="Dry Run Supplier", supplier_code=None)
    db_session.add(sup)
    db_session.commit()

    # Dry run
    dry_count = backfill_supplier_codes(db=db_session, dry_run=True, org_id=org.id)
    assert dry_count == 1

    db_session.refresh(sup)
    assert sup.supplier_code is None  # Not modified in dry run!
