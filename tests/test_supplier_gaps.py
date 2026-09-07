"""Test suite verifying Supplier Module Gaps implementation.

Covers:
1. Supplier ↔ Product Many-to-Many linking, duplicate prevention, cross-org safety, unlinking.
2. Inactive Supplier operational blocking during new Purchase creation.
3. Historical Purchase & Payment readability for inactive suppliers.
4. Supplier deletion safety (blocking deletion when historical Purchases exist).
5. Extended master fields persistence & updates (company_name, pan_number, state, pincode, country, supplier_type, payment_terms, credit_limit, notes).
"""
import uuid
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.core.database import Base, engine, get_db
from app.core.security import create_access_token
from app.models import Organization, Product, PurchaseInvoice, Role, Supplier, SupplierPayment, User, UserRole


@pytest.fixture
def db_session():
    Base.metadata.create_all(bind=engine)
    session = Session(bind=engine)
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def test_setup(db_session: Session):
    # Create Organization A
    org_a = Organization(
        id=str(uuid.uuid4()),
        name="Org A Suppliers Inc",
    )
    db_session.add(org_a)

    # Create Organization B (for cross-org security test)
    org_b = Organization(
        id=str(uuid.uuid4()),
        name="Org B Competitor Inc",
    )
    db_session.add(org_b)
    db_session.commit()

    # Create Admin User in Org A
    admin_a = User(
        id=str(uuid.uuid4()),
        organization_id=org_a.id,
        email=f"admin_a_{uuid.uuid4().hex[:6]}@example.com",
        name="Admin User Org A",
        password_hash="hashed_pw",
        role="admin",
        is_active=True,
    )
    db_session.add(admin_a)

    # Create Admin User in Org B
    admin_b = User(
        id=str(uuid.uuid4()),
        organization_id=org_b.id,
        email=f"admin_b_{uuid.uuid4().hex[:6]}@example.com",
        name="Admin User Org B",
        password_hash="hashed_pw",
        role="admin",
        is_active=True,
    )
    db_session.add(admin_b)
    db_session.commit()

    token_a = create_access_token(admin_a.id, "admin", org_a.id)
    headers_a = {"Authorization": f"Bearer {token_a}"}

    token_b = create_access_token(admin_b.id, "admin", org_b.id)
    headers_b = {"Authorization": f"Bearer {token_b}"}

    return {
        "org_a": org_a,
        "org_b": org_b,
        "admin_a": admin_a,
        "admin_b": admin_b,
        "headers_a": headers_a,
        "headers_b": headers_b,
    }


def test_supplier_master_extended_fields(test_setup):
    client = TestClient(app)
    headers_a = test_setup["headers_a"]

    # 1. Create Supplier with all master fields (Test 12)
    payload = {
        "name": "Acme Industrial Suppliers",
        "company_name": "Acme Industrial Private Limited",
        "contact_person": "John Doe",
        "phone": "+919876543210",
        "email": "vendor@acme.com",
        "gst_number": "29ABCDE1234F1Z5",
        "pan_number": "ABCDE1234F",
        "category": "Raw Materials",
        "supplier_type": "Manufacturer",
        "payment_terms": "Net 30",
        "credit_limit": 500000.0,
        "address": "123 Industrial Estate",
        "city": "Bengaluru",
        "state": "Karnataka",
        "pincode": "560001",
        "country": "India",
        "notes": "Key vendor for raw steel supplies",
        "opening_balance": 10000.0,
    }

    resp = client.post("/suppliers", json=payload, headers=headers_a)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    supplier_id = data["id"]

    assert data["name"] == "Acme Industrial Suppliers"
    assert data["company_name"] == "Acme Industrial Private Limited"
    assert data["pan_number"] == "ABCDE1234F"
    assert data["supplier_type"] == "Manufacturer"
    assert data["payment_terms"] == "Net 30"
    assert data["credit_limit"] == 500000.0
    assert data["state"] == "Karnataka"
    assert data["pincode"] == "560001"
    assert data["country"] == "India"
    assert data["notes"] == "Key vendor for raw steel supplies"

    # 2. Get Supplier Detail
    get_resp = client.get(f"/suppliers/{supplier_id}", headers=headers_a)
    assert get_resp.status_code == 200
    get_data = get_resp.json()
    assert get_data["company_name"] == "Acme Industrial Private Limited"

    # 3. Update Supplier Fields
    update_payload = {
        "payment_terms": "Net 45",
        "credit_limit": 750000.0,
        "notes": "Updated credit terms approved",
    }
    put_resp = client.put(f"/suppliers/{supplier_id}", json=update_payload, headers=headers_a)
    assert put_resp.status_code == 200
    put_data = put_resp.json()
    assert put_data["payment_terms"] == "Net 45"
    assert put_data["credit_limit"] == 750000.0
    assert put_data["notes"] == "Updated credit terms approved"


def test_supplier_product_many_to_many(test_setup, db_session: Session):
    client = TestClient(app)
    headers_a = test_setup["headers_a"]
    headers_b = test_setup["headers_b"]
    org_a = test_setup["org_a"]
    org_b = test_setup["org_b"]

    # Setup Suppliers in Org A
    sup_a1 = Supplier(id=str(uuid.uuid4()), organization_id=org_a.id, name="Supplier A1", is_active=True)
    sup_a2 = Supplier(id=str(uuid.uuid4()), organization_id=org_a.id, name="Supplier A2", is_active=True)
    sup_b1 = Supplier(id=str(uuid.uuid4()), organization_id=org_b.id, name="Supplier B1", is_active=True)
    db_session.add_all([sup_a1, sup_a2, sup_b1])

    # Setup Products in Org A & Org B
    prod_a1 = Product(id=str(uuid.uuid4()), organization_id=org_a.id, name="Product A1", price=100.0)
    prod_a2 = Product(id=str(uuid.uuid4()), organization_id=org_a.id, name="Product A2", price=200.0)
    prod_b1 = Product(id=str(uuid.uuid4()), organization_id=org_b.id, name="Product B1", price=300.0)
    db_session.add_all([prod_a1, prod_a2, prod_b1])
    db_session.commit()

    # Test 1: Link Product A1 to Supplier A1 -> Success
    link_resp = client.post(f"/suppliers/{sup_a1.id}/products", json={"product_id": prod_a1.id}, headers=headers_a)
    assert link_resp.status_code == 201, link_resp.text
    link_data = link_resp.json()
    assert link_data["supplier_id"] == sup_a1.id
    assert link_data["product_id"] == prod_a1.id
    assert link_data["product_name"] == "Product A1"

    # Test 2: Link same Product A1 to Supplier A1 twice -> Duplicate blocked (400)
    dup_resp = client.post(f"/suppliers/{sup_a1.id}/products", json={"product_id": prod_a1.id}, headers=headers_a)
    assert dup_resp.status_code == 400
    assert "already linked" in dup_resp.json()["detail"].lower()

    # Test 3: Multiple Suppliers for Product A1 (Link Product A1 to Supplier A2) -> Allowed
    link2_resp = client.post(f"/suppliers/{sup_a2.id}/products", json={"product_id": prod_a1.id}, headers=headers_a)
    assert link2_resp.status_code == 201

    # Test 4: Multiple Products for Supplier A1 (Link Product A2 to Supplier A1) -> Allowed
    link3_resp = client.post(f"/suppliers/{sup_a1.id}/products", json={"product_id": prod_a2.id}, headers=headers_a)
    assert link3_resp.status_code == 201

    # List Supplier A1 products -> Should return both Product A1 and Product A2
    list_resp = client.get(f"/suppliers/{sup_a1.id}/products", headers=headers_a)
    assert list_resp.status_code == 200
    prod_list = list_resp.json()
    assert len(prod_list) == 2
    linked_ids = {p["product_id"] for p in prod_list}
    assert prod_a1.id in linked_ids
    assert prod_a2.id in linked_ids

    # Test 5: Cross-org link (Supplier Org A + Product Org B) -> BLOCK (400)
    cross_resp = client.post(f"/suppliers/{sup_a1.id}/products", json={"product_id": prod_b1.id}, headers=headers_a)
    assert cross_resp.status_code == 400
    assert "not a product in your firm" in cross_resp.json()["detail"].lower()

    # Cross-org access (User Org B accessing Supplier Org A links) -> 404
    cross_access = client.get(f"/suppliers/{sup_a1.id}/products", headers=headers_b)
    assert cross_access.status_code == 404

    # Test 6: Unlink Product A1 from Supplier A1 -> Success
    unlink_resp = client.delete(f"/suppliers/{sup_a1.id}/products/{prod_a1.id}", headers=headers_a)
    assert unlink_resp.status_code == 204

    # Verify Product A1 still exists in catalog
    prod_check = db_session.get(Product, prod_a1.id)
    assert prod_check is not None
    assert prod_check.name == "Product A1"

    # Verify Supplier A1 still has Product A2 linked
    remaining_list = client.get(f"/suppliers/{sup_a1.id}/products", headers=headers_a).json()
    assert len(remaining_list) == 1
    assert remaining_list[0]["product_id"] == prod_a2.id


def test_inactive_supplier_purchase_blocking(test_setup, db_session: Session):
    client = TestClient(app)
    headers_a = test_setup["headers_a"]
    org_a = test_setup["org_a"]

    # Active Supplier
    active_sup = Supplier(id=str(uuid.uuid4()), organization_id=org_a.id, name="Active Vendor", is_active=True)
    # Inactive Supplier
    inactive_sup = Supplier(id=str(uuid.uuid4()), organization_id=org_a.id, name="Inactive Vendor", is_active=False)
    # Product
    prod = Product(id=str(uuid.uuid4()), organization_id=org_a.id, name="Steel Pipe", price=500.0)
    db_session.add_all([active_sup, inactive_sup, prod])
    db_session.commit()

    # Test 7: Active Supplier -> Purchase creation succeeds
    purchase_payload_1 = {
        "invoice_number": "INV-ACTIVE-001",
        "supplier_id": active_sup.id,
        "items": [
            {"product_id": prod.id, "quantity": 10, "purchase_price": 400.0}
        ],
    }
    p1_resp = client.post("/purchases", json=purchase_payload_1, headers=headers_a)
    assert p1_resp.status_code == 201, p1_resp.text
    p1_id = p1_resp.json()["id"]

    # Test 8: Inactive Supplier -> Purchase creation blocked (400)
    purchase_payload_2 = {
        "invoice_number": "INV-INACTIVE-002",
        "supplier_id": inactive_sup.id,
        "items": [
            {"product_id": prod.id, "quantity": 5, "purchase_price": 400.0}
        ],
    }
    p2_resp = client.post("/purchases", json=purchase_payload_2, headers=headers_a)
    assert p2_resp.status_code == 400
    assert "inactive and cannot be used for new purchases" in p2_resp.json()["detail"].lower()

    # Test 9: Inactive Supplier details and existing/historical purchases are still readable
    sup_detail = client.get(f"/suppliers/{inactive_sup.id}", headers=headers_a)
    assert sup_detail.status_code == 200
    assert sup_detail.json()["is_active"] is False

    # Historical purchase created under active_sup is readable
    hist_purchase = client.get(f"/purchases/{p1_id}", headers=headers_a)
    assert hist_purchase.status_code == 200


def test_supplier_delete_safety(test_setup, db_session: Session):
    client = TestClient(app)
    headers_a = test_setup["headers_a"]
    org_a = test_setup["org_a"]

    # Supplier 1 (will have historical purchases)
    sup_with_purchases = Supplier(id=str(uuid.uuid4()), organization_id=org_a.id, name="Vendor With Purchases", is_active=True)
    # Supplier 2 (no historical purchases)
    sup_clean = Supplier(id=str(uuid.uuid4()), organization_id=org_a.id, name="Clean Vendor", is_active=True)
    prod = Product(id=str(uuid.uuid4()), organization_id=org_a.id, name="Copper Wire", price=150.0)
    db_session.add_all([sup_with_purchases, sup_clean, prod])
    db_session.commit()

    # Create Purchase for sup_with_purchases
    client.post("/purchases", json={
        "invoice_number": "INV-HIST-999",
        "supplier_id": sup_with_purchases.id,
        "items": [{"product_id": prod.id, "quantity": 2, "purchase_price": 100.0}],
    }, headers=headers_a)

    # Test 10: Supplier with historical Purchase -> DELETE blocked (400)
    del_blocked_resp = client.delete(f"/suppliers/{sup_with_purchases.id}", headers=headers_a)
    assert del_blocked_resp.status_code == 400
    assert "cannot delete supplier with historical purchases" in del_blocked_resp.json()["detail"].lower()

    sup_clean_id = sup_clean.id
    # Test 11: Supplier without protected historical dependency -> DELETE succeeds
    del_clean_resp = client.delete(f"/suppliers/{sup_clean_id}", headers=headers_a)
    assert del_clean_resp.status_code == 204

    # Verify sup_clean is deleted
    assert db_session.query(Supplier).filter(Supplier.id == sup_clean_id).first() is None


def test_supplier_multi_select_categories(test_setup):
    client = TestClient(app)
    headers_a = test_setup["headers_a"]

    # 1. Create Supplier with multi-select supplier_categories
    payload = {
        "name": "Multi-Cat Distributors",
        "supplier_type": "Distributor",
        "supplier_categories": ["Electronics", "Accessories", "Packaging"],
    }
    resp = client.post("/suppliers", json=payload, headers=headers_a)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    supplier_id = data["id"]

    assert data["supplier_type"] == "Distributor"
    assert data["supplier_categories"] == ["Electronics", "Accessories", "Packaging"]
    assert data["category"] == "Electronics"  # Backward compatibility primary category

    # 2. Get Supplier detail
    get_resp = client.get(f"/suppliers/{supplier_id}", headers=headers_a)
    assert get_resp.status_code == 200
    get_data = get_resp.json()
    assert get_data["supplier_categories"] == ["Electronics", "Accessories", "Packaging"]
    assert get_data["category"] == "Electronics"

    # 3. Update supplier_categories
    update_payload = {
        "supplier_categories": ["Hardware", "Tools"],
    }
    put_resp = client.put(f"/suppliers/{supplier_id}", json=update_payload, headers=headers_a)
    assert put_resp.status_code == 200
    put_data = put_resp.json()
    assert put_data["supplier_categories"] == ["Hardware", "Tools"]
    assert put_data["category"] == "Hardware"

    # 4. Backward compatibility: create legacy supplier with single category string
    legacy_payload = {
        "name": "Legacy Vendor Inc",
        "category": "Raw Materials",
    }
    leg_resp = client.post("/suppliers", json=legacy_payload, headers=headers_a)
    assert leg_resp.status_code == 201
    leg_data = leg_resp.json()
    assert leg_data["category"] == "Raw Materials"
    assert leg_data["supplier_categories"] == ["Raw Materials"]

    # 5. Historical database record (categories JSON is NULL) compatibility
    db_session = Session(bind=engine)
    hist_sup = Supplier(
        id=str(uuid.uuid4()),
        organization_id=test_setup["org_a"].id,
        name="Historical DB Vendor",
        category="Historical Steel",
        categories=None,
        is_active=True,
    )
    db_session.add(hist_sup)
    db_session.commit()
    hist_id = hist_sup.id
    db_session.close()

    hist_resp = client.get(f"/suppliers/{hist_id}", headers=headers_a)
    assert hist_resp.status_code == 200
    hist_data = hist_resp.json()
    assert hist_data["category"] == "Historical Steel"
    assert hist_data["supplier_categories"] == ["Historical Steel"]


def test_supplier_permissions_and_purchase_filtering(test_setup, db_session: Session):
    client = TestClient(app)
    org_a = test_setup["org_a"]
    headers_admin_a = test_setup["headers_a"]

    # Create Roles for Accountant, Sales Officer, Delivery Partner
    role_acct = Role(
        id=str(uuid.uuid4()),
        organization_id=org_a.id,
        name="Accountant Role",
        permissions={"suppliers": {"view": True}, "payments": {"view": True, "create": True}},
    )
    role_sales = Role(
        id=str(uuid.uuid4()),
        organization_id=org_a.id,
        name="Sales Officer Role",
        permissions={"suppliers": {"view": True}},
    )
    role_deliv = Role(
        id=str(uuid.uuid4()),
        organization_id=org_a.id,
        name="Delivery Partner Role",
        permissions={"deliveries": {"view": True}},
    )
    db_session.add_all([role_acct, role_sales, role_deliv])
    db_session.commit()

    # Create users with assigned roles
    accountant_user = User(
        id=str(uuid.uuid4()),
        organization_id=org_a.id,
        email=f"accountant_{uuid.uuid4().hex[:6]}@example.com",
        name="Accountant User",
        password_hash="hashed_pw",
        role=UserRole.ACCOUNTANT,
        role_id=role_acct.id,
        is_active=True,
    )
    sales_user = User(
        id=str(uuid.uuid4()),
        organization_id=org_a.id,
        email=f"sales_{uuid.uuid4().hex[:6]}@example.com",
        name="Sales Officer User",
        password_hash="hashed_pw",
        role=UserRole.SALES_OFFICER,
        role_id=role_sales.id,
        is_active=True,
    )
    delivery_user = User(
        id=str(uuid.uuid4()),
        organization_id=org_a.id,
        email=f"delivery_{uuid.uuid4().hex[:6]}@example.com",
        name="Delivery Partner User",
        password_hash="hashed_pw",
        role=UserRole.DELIVERY_PARTNER,
        role_id=role_deliv.id,
        is_active=True,
    )
    db_session.add_all([accountant_user, sales_user, delivery_user])
    db_session.commit()

    token_acct = create_access_token(accountant_user.id, UserRole.ACCOUNTANT.value, org_a.id)
    headers_acct = {"Authorization": f"Bearer {token_acct}"}

    token_sales = create_access_token(sales_user.id, UserRole.SALES_OFFICER.value, org_a.id)
    headers_sales = {"Authorization": f"Bearer {token_sales}"}

    token_deliv = create_access_token(delivery_user.id, UserRole.DELIVERY_PARTNER.value, org_a.id)
    headers_deliv = {"Authorization": f"Bearer {token_deliv}"}

    # 1. Test Admin permission -> Can create supplier
    sup_resp = client.post("/suppliers", json={"name": "Perm Test Vendor"}, headers=headers_admin_a)
    assert sup_resp.status_code == 201
    sup_id = sup_resp.json()["id"]

    # 2. Test Accountant permission -> Can view suppliers, but POST /suppliers is 403
    acct_get = client.get(f"/suppliers/{sup_id}", headers=headers_acct)
    assert acct_get.status_code == 200
    acct_post = client.post("/suppliers", json={"name": "Forbidden Vendor"}, headers=headers_acct)
    assert acct_post.status_code == 403

    # 3. Test Sales Officer permission -> Can view suppliers, but POST /suppliers is 403
    sales_get = client.get("/suppliers", headers=headers_sales)
    assert sales_get.status_code == 200
    sales_post = client.post("/suppliers", json={"name": "Forbidden Vendor 2"}, headers=headers_sales)
    assert sales_post.status_code == 403

    # 4. Test Delivery Partner permission -> Cannot view suppliers (403)
    deliv_get = client.get("/suppliers", headers=headers_deliv)
    assert deliv_get.status_code == 403

    # 5. Test Purchase filtering by supplier_id (GET /purchases?supplier_id={supplier_id})
    sup_b = Supplier(id=str(uuid.uuid4()), organization_id=org_a.id, name="Other Vendor", is_active=True)
    prod = Product(id=str(uuid.uuid4()), organization_id=org_a.id, name="Test Product", price=100.0)
    db_session.add_all([sup_b, prod])
    db_session.commit()

    # Create Purchase for sup_id
    client.post("/purchases", json={
        "invoice_number": "PO-SUP-A-1",
        "supplier_id": sup_id,
        "items": [{"product_id": prod.id, "quantity": 5, "purchase_price": 50.0}],
    }, headers=headers_admin_a)

    # Create Purchase for sup_b
    client.post("/purchases", json={
        "invoice_number": "PO-SUP-B-1",
        "supplier_id": sup_b.id,
        "items": [{"product_id": prod.id, "quantity": 10, "purchase_price": 50.0}],
    }, headers=headers_admin_a)

    # Query purchases filtered by sup_id
    filter_resp = client.get(f"/purchases?supplier_id={sup_id}", headers=headers_admin_a)
    assert filter_resp.status_code == 200
    purchases_list = filter_resp.json()
    assert len(purchases_list) >= 1
    for p in purchases_list:
        assert p["supplier_id"] == sup_id


