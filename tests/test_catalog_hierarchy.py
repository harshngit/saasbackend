"""Comprehensive test suite for Catalog Hierarchy Extension (Supplier -> Brand -> Category -> Product -> ProductVariant).

Covers all 16 required test scenarios:
1. Supplier -> Brand linking and retrieval
2. Supplier multiple Brands
3. Same Brand multiple Suppliers
4. Brand multiple Categories
5. Same Category multiple Brands
6. Product creation with valid Brand + Category + Supplier hierarchy
7. Product Variants functionality preservation
8. Invalid Supplier -> Brand validation error on Product create/update
9. Invalid Brand -> Category validation error on Product create/update
10. SupplierProduct regression & automatic hierarchy synchronization
11. Historical Product compatibility (read, unrelated update, clearing fields)
12. Cross-organization SupplierBrand isolation
13. Cross-organization BrandCategory isolation
14. Duplicate association prevention and idempotency
15. Migration backfill logic verification
16. Product PATCH effective-state validation (partial updates)
"""
import os
import sys
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.main import app
from app.core.database import Base, engine
from app.models import (
    Brand,
    Category,
    Organization,
    Product,
    ProductVariant,
    Supplier,
    SupplierProduct,
    User,
)
from app.models.catalog_hierarchy import BrandCategory, SupplierBrand
from app.core.security import hash_password, create_access_token


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


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
    org_a = Organization(id=str(uuid.uuid4()), name="Org A Catalog Inc")
    # Create Organization B
    org_b = Organization(id=str(uuid.uuid4()), name="Org B Competitor Inc")
    db_session.add_all([org_a, org_b])
    db_session.commit()

    # Create Admin Users
    admin_a = User(
        id=str(uuid.uuid4()),
        organization_id=org_a.id,
        email=f"admin_a_{uuid.uuid4().hex[:6]}@example.com",
        name="Admin User Org A",
        password_hash=hash_password("Password123!"),
        role="admin",
        is_active=True,
    )
    admin_b = User(
        id=str(uuid.uuid4()),
        organization_id=org_b.id,
        email=f"admin_b_{uuid.uuid4().hex[:6]}@example.com",
        name="Admin User Org B",
        password_hash=hash_password("Password123!"),
        role="admin",
        is_active=True,
    )
    db_session.add_all([admin_a, admin_b])
    db_session.commit()

    token_a = create_access_token(admin_a.id, "admin", org_a.id)
    token_b = create_access_token(admin_b.id, "admin", org_b.id)

    return {
        "org_a": org_a,
        "org_b": org_b,
        "admin_a": admin_a,
        "admin_b": admin_b,
        "headers_a": {"Authorization": f"Bearer {token_a}"},
        "headers_b": {"Authorization": f"Bearer {token_b}"},
    }


def test_supplier_brand_crud(client, test_setup, db_session: Session):
    """Scenario 1, 2, 3, 14: Supplier -> Brand association, multiple brands, multiple suppliers, deduplication."""
    headers_a = test_setup["headers_a"]
    org_a = test_setup["org_a"]

    # Create Suppliers and Brands in Org A
    sup1 = Supplier(id=str(uuid.uuid4()), organization_id=org_a.id, name="Supplier Alpha", is_active=True)
    sup2 = Supplier(id=str(uuid.uuid4()), organization_id=org_a.id, name="Supplier Beta", is_active=True)
    b1 = Brand(id=str(uuid.uuid4()), organization_id=org_a.id, name="Brand Sony", is_active=True)
    b2 = Brand(id=str(uuid.uuid4()), organization_id=org_a.id, name="Brand Samsung", is_active=True)
    db_session.add_all([sup1, sup2, b1, b2])
    db_session.commit()

    # 1. Supplier 1 -> Brand 1
    r1 = client.post(f"/suppliers/{sup1.id}/brands", json={"brand_id": b1.id}, headers=headers_a)
    assert r1.status_code == 201, r1.text
    data1 = r1.json()
    assert data1["supplier_id"] == sup1.id
    assert data1["brand_id"] == b1.id
    assert data1["brand_name"] == "Brand Sony"

    # Scenario 14: Duplicate association idempotent handling
    r1_dup = client.post(f"/suppliers/{sup1.id}/brands", json={"brand_id": b1.id}, headers=headers_a)
    assert r1_dup.status_code == 201
    assert r1_dup.json()["id"] == data1["id"]
    count_sb = db_session.query(SupplierBrand).filter(
        SupplierBrand.supplier_id == sup1.id, SupplierBrand.brand_id == b1.id
    ).count()
    assert count_sb == 1

    # 2. Supplier 1 multiple brands: Supplier 1 -> Brand 2
    r2 = client.post(f"/suppliers/{sup1.id}/brands", json={"brand_id": b2.id}, headers=headers_a)
    assert r2.status_code == 201

    list_sup1 = client.get(f"/suppliers/{sup1.id}/brands", headers=headers_a)
    assert list_sup1.status_code == 200
    brand_ids_sup1 = {item["brand_id"] for item in list_sup1.json()}
    assert b1.id in brand_ids_sup1
    assert b2.id in brand_ids_sup1

    # 3. Same brand multiple suppliers: Supplier 2 -> Brand 1
    r3 = client.post(f"/suppliers/{sup2.id}/brands", json={"brand_id": b1.id}, headers=headers_a)
    assert r3.status_code == 201

    list_sup2 = client.get(f"/suppliers/{sup2.id}/brands", headers=headers_a)
    assert list_sup2.status_code == 200
    brand_ids_sup2 = {item["brand_id"] for item in list_sup2.json()}
    assert b1.id in brand_ids_sup2
    assert b2.id not in brand_ids_sup2

    # Test Delete SupplierBrand
    del_r = client.delete(f"/suppliers/{sup1.id}/brands/{b1.id}", headers=headers_a)
    assert del_r.status_code == 204
    # Verify Brand Sony still exists
    assert db_session.get(Brand, b1.id) is not None
    # Verify Supplier Alpha still exists
    assert db_session.get(Supplier, sup1.id) is not None


def test_brand_category_crud(client, test_setup, db_session: Session):
    """Scenario 4, 5, 14: Brand -> Category association, multiple categories, multiple brands, deduplication."""
    headers_a = test_setup["headers_a"]
    org_a = test_setup["org_a"]

    b1 = Brand(id=str(uuid.uuid4()), organization_id=org_a.id, name="Brand Apple", is_active=True)
    b2 = Brand(id=str(uuid.uuid4()), organization_id=org_a.id, name="Brand Dell", is_active=True)
    c1 = Category(id=str(uuid.uuid4()), organization_id=org_a.id, name="Laptops", is_active=True)
    c2 = Category(id=str(uuid.uuid4()), organization_id=org_a.id, name="Smartphones", is_active=True)
    db_session.add_all([b1, b2, c1, c2])
    db_session.commit()

    # 4. Brand 1 -> Category 1 and Category 2
    r1 = client.post(f"/brands/{b1.id}/categories", json={"category_id": c1.id}, headers=headers_a)
    assert r1.status_code == 201, r1.text
    assert r1.json()["category_name"] == "Laptops"

    # Duplicate check
    r1_dup = client.post(f"/brands/{b1.id}/categories", json={"category_id": c1.id}, headers=headers_a)
    assert r1_dup.status_code == 201
    count_bc = db_session.query(BrandCategory).filter(
        BrandCategory.brand_id == b1.id, BrandCategory.category_id == c1.id
    ).count()
    assert count_bc == 1

    r2 = client.post(f"/brands/{b1.id}/categories", json={"category_id": c2.id}, headers=headers_a)
    assert r2.status_code == 201

    list_b1 = client.get(f"/brands/{b1.id}/categories", headers=headers_a)
    assert list_b1.status_code == 200
    cat_ids_b1 = {item["category_id"] for item in list_b1.json()}
    assert c1.id in cat_ids_b1
    assert c2.id in cat_ids_b1

    # 5. Same Category multiple Brands: Brand 2 -> Category 1
    r3 = client.post(f"/brands/{b2.id}/categories", json={"category_id": c1.id}, headers=headers_a)
    assert r3.status_code == 201

    list_b2 = client.get(f"/brands/{b2.id}/categories", headers=headers_a)
    assert list_b2.status_code == 200
    cat_ids_b2 = {item["category_id"] for item in list_b2.json()}
    assert c1.id in cat_ids_b2
    assert c2.id not in cat_ids_b2

    # Delete BrandCategory
    del_r = client.delete(f"/brands/{b1.id}/categories/{c1.id}", headers=headers_a)
    assert del_r.status_code == 204
    assert db_session.get(Brand, b1.id) is not None
    assert db_session.get(Category, c1.id) is not None


def test_product_hierarchy_validation_on_create(client, test_setup, db_session: Session):
    """Scenario 6, 8, 9: Product creation with valid vs invalid hierarchy."""
    headers_a = test_setup["headers_a"]
    org_a = test_setup["org_a"]

    sup = Supplier(id=str(uuid.uuid4()), organization_id=org_a.id, name="Tech Distro", is_active=True)
    brand_linked = Brand(id=str(uuid.uuid4()), organization_id=org_a.id, name="Asus", is_active=True)
    brand_unlinked = Brand(id=str(uuid.uuid4()), organization_id=org_a.id, name="Acer", is_active=True)
    cat_linked = Category(id=str(uuid.uuid4()), organization_id=org_a.id, name="Gaming Laptops", is_active=True)
    cat_unlinked = Category(id=str(uuid.uuid4()), organization_id=org_a.id, name="Accessories", is_active=True)

    db_session.add_all([sup, brand_linked, brand_unlinked, cat_linked, cat_unlinked])
    db_session.commit()

    # Link Tech Distro -> Asus
    client.post(f"/suppliers/{sup.id}/brands", json={"brand_id": brand_linked.id}, headers=headers_a)
    # Link Asus -> Gaming Laptops
    client.post(f"/brands/{brand_linked.id}/categories", json={"category_id": cat_linked.id}, headers=headers_a)

    # Scenario 8: Invalid Supplier -> Brand
    bad_sup_payload = {
        "name": "Invalid Supplier Product",
        "price": 50000.0,
        "preferred_supplier_id": sup.id,
        "brand_id": brand_unlinked.id,
    }
    r_bad_sup = client.post("/products", json=bad_sup_payload, headers=headers_a)
    assert r_bad_sup.status_code == 400
    assert "Brand is not associated with the selected supplier" in r_bad_sup.json()["detail"]

    # Scenario 9: Invalid Brand -> Category
    bad_cat_payload = {
        "name": "Invalid Category Product",
        "price": 60000.0,
        "brand_id": brand_linked.id,
        "category_id": cat_unlinked.id,
    }
    r_bad_cat = client.post("/products", json=bad_cat_payload, headers=headers_a)
    assert r_bad_cat.status_code == 400
    assert "Category is not associated with the selected brand" in r_bad_cat.json()["detail"]

    # Scenario 6: Valid Product create with all 3
    valid_payload = {
        "name": "Asus ROG Strix",
        "price": 120000.0,
        "preferred_supplier_id": sup.id,
        "brand_id": brand_linked.id,
        "category_id": cat_linked.id,
    }
    r_valid = client.post("/products", json=valid_payload, headers=headers_a)
    assert r_valid.status_code == 201, r_valid.text
    prod_data = r_valid.json()
    assert prod_data["preferred_supplier_id"] == sup.id
    assert prod_data["brand_id"] == brand_linked.id
    assert prod_data["category_id"] == cat_linked.id


def test_product_variants_preservation(client, test_setup, db_session: Session):
    """Scenario 7: Product variants work normally with hierarchy."""
    headers_a = test_setup["headers_a"]
    org_a = test_setup["org_a"]

    sup = Supplier(id=str(uuid.uuid4()), organization_id=org_a.id, name="Shoe Distro", is_active=True)
    brand = Brand(id=str(uuid.uuid4()), organization_id=org_a.id, name="Nike", is_active=True)
    cat = Category(id=str(uuid.uuid4()), organization_id=org_a.id, name="Footwear", is_active=True)
    db_session.add_all([sup, brand, cat])
    db_session.commit()

    client.post(f"/suppliers/{sup.id}/brands", json={"brand_id": brand.id}, headers=headers_a)
    client.post(f"/brands/{brand.id}/categories", json={"category_id": cat.id}, headers=headers_a)

    prod_payload = {
        "name": "Air Jordan 1",
        "price": 15000.0,
        "preferred_supplier_id": sup.id,
        "brand_id": brand.id,
        "category_id": cat.id,
        "has_variants": True,
        "variations": [
            {"name": "Size 9", "sku": "AJ1-9", "price": 15000.0, "inventory": 10},
            {"name": "Size 10", "sku": "AJ1-10", "price": 15000.0, "inventory": 5},
        ],
    }
    r = client.post("/products", json=prod_payload, headers=headers_a)
    assert r.status_code == 201
    data = r.json()
    assert len(data["variations"]) == 2
    assert data["total_stock"] == 15


def test_supplier_product_auto_sync(client, test_setup, db_session: Session):
    """Scenario 10: SupplierProduct linking auto-syncs SupplierBrand and BrandCategory."""
    headers_a = test_setup["headers_a"]
    org_a = test_setup["org_a"]

    sup = Supplier(id=str(uuid.uuid4()), organization_id=org_a.id, name="Auto Vendor", is_active=True)
    brand = Brand(id=str(uuid.uuid4()), organization_id=org_a.id, name="Auto Brand", is_active=True)
    cat = Category(id=str(uuid.uuid4()), organization_id=org_a.id, name="Auto Cat", is_active=True)
    db_session.add_all([sup, brand, cat])
    db_session.commit()

    # Create Product directly (simulating product having brand and category)
    prod = Product(
        id=str(uuid.uuid4()),
        organization_id=org_a.id,
        name="Auto Synced Product",
        price=500.0,
        brand_id=brand.id,
        category_id=cat.id,
    )
    db_session.add(prod)
    db_session.commit()

    # Link product to supplier via POST /suppliers/{id}/products
    link_resp = client.post(f"/suppliers/{sup.id}/products", json={"product_id": prod.id}, headers=headers_a)
    assert link_resp.status_code == 201

    # Verify SupplierProduct exists
    sp = db_session.query(SupplierProduct).filter(
        SupplierProduct.supplier_id == sup.id, SupplierProduct.product_id == prod.id
    ).first()
    assert sp is not None

    # Verify SupplierBrand was automatically created
    sb = db_session.query(SupplierBrand).filter(
        SupplierBrand.supplier_id == sup.id, SupplierBrand.brand_id == brand.id
    ).first()
    assert sb is not None

    # Verify BrandCategory was automatically created
    bc = db_session.query(BrandCategory).filter(
        BrandCategory.brand_id == brand.id, BrandCategory.category_id == cat.id
    ).first()
    assert bc is not None


def test_historical_product_compatibility_and_patch(client, test_setup, db_session: Session):
    """Scenario 11, 16: Historical products without links are readable & editable for unrelated fields; relationship updates enforce effective state."""
    headers_a = test_setup["headers_a"]
    org_a = test_setup["org_a"]

    sup1 = Supplier(id=str(uuid.uuid4()), organization_id=org_a.id, name="Legacy Sup 1", is_active=True)
    sup2 = Supplier(id=str(uuid.uuid4()), organization_id=org_a.id, name="Legacy Sup 2", is_active=True)
    brand1 = Brand(id=str(uuid.uuid4()), organization_id=org_a.id, name="Legacy Brand 1", is_active=True)
    brand2 = Brand(id=str(uuid.uuid4()), organization_id=org_a.id, name="Legacy Brand 2", is_active=True)
    cat1 = Category(id=str(uuid.uuid4()), organization_id=org_a.id, name="Legacy Cat 1", is_active=True)
    cat2 = Category(id=str(uuid.uuid4()), organization_id=org_a.id, name="Legacy Cat 2", is_active=True)
    db_session.add_all([sup1, sup2, brand1, brand2, cat1, cat2])
    db_session.commit()

    # Create historical product directly in DB without associations in supplier_brands or brand_categories
    hist_prod = Product(
        id=str(uuid.uuid4()),
        organization_id=org_a.id,
        name="Historical Product",
        price=100.0,
        preferred_supplier_id=sup1.id,
        brand_id=brand1.id,
        category_id=cat1.id,
    )
    db_session.add(hist_prod)
    db_session.commit()

    # 1. GET historical product must succeed
    get_r = client.get(f"/products/{hist_prod.id}", headers=headers_a)
    assert get_r.status_code == 200
    assert get_r.json()["name"] == "Historical Product"

    # 2. Unrelated PATCH (pricing & description) must succeed without throwing hierarchy errors
    unrelated_patch = client.patch(
        f"/products/{hist_prod.id}",
        json={"pricing": {"selling_price": 150.0}, "description": "Updated description only"},
        headers=headers_a,
    )
    assert unrelated_patch.status_code == 200
    assert unrelated_patch.json()["price"] == 150.0
    assert unrelated_patch.json()["description"] == "Updated description only"

    # 3. Changing Brand to Brand 2 (which is unlinked) must fail against effective Supplier 1 and effective Category 1
    bad_brand_patch = client.patch(
        f"/products/{hist_prod.id}",
        json={"brand_id": brand2.id},
        headers=headers_a,
    )
    assert bad_brand_patch.status_code == 400
    assert "Brand is not associated with the selected supplier" in bad_brand_patch.json()["detail"]

    # Now link Sup 1 -> Brand 2, but do NOT link Brand 2 -> Cat 1
    client.post(f"/suppliers/{sup1.id}/brands", json={"brand_id": brand2.id}, headers=headers_a)
    bad_brand_cat_patch = client.patch(
        f"/products/{hist_prod.id}",
        json={"brand_id": brand2.id},
        headers=headers_a,
    )
    assert bad_brand_cat_patch.status_code == 400
    assert "Category is not associated with the selected brand" in bad_brand_cat_patch.json()["detail"]

    # Link Brand 2 -> Cat 1, now brand_id patch succeeds
    client.post(f"/brands/{brand2.id}/categories", json={"category_id": cat1.id}, headers=headers_a)
    good_brand_patch = client.patch(
        f"/products/{hist_prod.id}",
        json={"brand_id": brand2.id},
        headers=headers_a,
    )
    assert good_brand_patch.status_code == 200
    assert good_brand_patch.json()["brand_id"] == brand2.id

    # 4. Clearing fields to null must succeed without requiring links
    clear_patch = client.patch(
        f"/products/{hist_prod.id}",
        json={"preferred_supplier_id": None, "brand_id": None, "category_id": None},
        headers=headers_a,
    )
    assert clear_patch.status_code == 200
    assert clear_patch.json()["preferred_supplier_id"] is None
    assert clear_patch.json()["brand_id"] is None
    assert clear_patch.json()["category_id"] is None


def test_cross_tenant_isolation(client, test_setup, db_session: Session):
    """Scenario 12, 13: Cross-tenant associations are rejected."""
    headers_a = test_setup["headers_a"]
    headers_b = test_setup["headers_b"]
    org_a = test_setup["org_a"]
    org_b = test_setup["org_b"]

    sup_a = Supplier(id=str(uuid.uuid4()), organization_id=org_a.id, name="Org A Supplier", is_active=True)
    brand_a = Brand(id=str(uuid.uuid4()), organization_id=org_a.id, name="Org A Brand", is_active=True)
    cat_a = Category(id=str(uuid.uuid4()), organization_id=org_a.id, name="Org A Category", is_active=True)

    sup_b = Supplier(id=str(uuid.uuid4()), organization_id=org_b.id, name="Org B Supplier", is_active=True)
    brand_b = Brand(id=str(uuid.uuid4()), organization_id=org_b.id, name="Org B Brand", is_active=True)
    cat_b = Category(id=str(uuid.uuid4()), organization_id=org_b.id, name="Org B Category", is_active=True)

    db_session.add_all([sup_a, brand_a, cat_a, sup_b, brand_b, cat_b])
    db_session.commit()

    # Scenario 12: Org A user tries to link Org A Supplier with Org B Brand
    r1 = client.post(f"/suppliers/{sup_a.id}/brands", json={"brand_id": brand_b.id}, headers=headers_a)
    assert r1.status_code == 400

    # Org A user tries to link Org B Supplier with Org A Brand
    r2 = client.post(f"/suppliers/{sup_b.id}/brands", json={"brand_id": brand_a.id}, headers=headers_a)
    assert r2.status_code == 404

    # Scenario 13: Org A user tries to link Org A Brand with Org B Category
    r3 = client.post(f"/brands/{brand_a.id}/categories", json={"category_id": cat_b.id}, headers=headers_a)
    assert r3.status_code == 400

    # Org A user tries to link Org B Brand with Org A Category
    r4 = client.post(f"/brands/{brand_b.id}/categories", json={"category_id": cat_a.id}, headers=headers_a)
    assert r4.status_code == 404


def test_migration_backfill_logic(db_session: Session):
    """Scenario 15: Migration backfill logic correctly populates supplier_brands and brand_categories."""
    org_id = str(uuid.uuid4())
    org = Organization(id=org_id, name="Backfill Test Org")
    db_session.add(org)
    db_session.commit()

    sup = Supplier(id=str(uuid.uuid4()), organization_id=org_id, name="Backfill Sup", is_active=True)
    brand = Brand(id=str(uuid.uuid4()), organization_id=org_id, name="Backfill Brand", is_active=True)
    cat = Category(id=str(uuid.uuid4()), organization_id=org_id, name="Backfill Cat", is_active=True)
    db_session.add_all([sup, brand, cat])
    db_session.commit()

    prod = Product(
        id=str(uuid.uuid4()),
        organization_id=org_id,
        name="Backfill Product",
        price=10.0,
        brand_id=brand.id,
        category_id=cat.id,
    )
    db_session.add(prod)
    db_session.commit()

    sp = SupplierProduct(
        id=str(uuid.uuid4()),
        organization_id=org_id,
        supplier_id=sup.id,
        product_id=prod.id,
    )
    db_session.add(sp)
    db_session.commit()

    # Clear any existing association rows for this test
    db_session.query(SupplierBrand).filter(SupplierBrand.organization_id == org_id).delete()
    db_session.query(BrandCategory).filter(BrandCategory.organization_id == org_id).delete()
    db_session.commit()

    # Simulate backfill queries
    sb_candidates = (
        db_session.query(SupplierProduct.organization_id, SupplierProduct.supplier_id, Product.brand_id)
        .join(Product, SupplierProduct.product_id == Product.id)
        .join(Brand, Product.brand_id == Brand.id)
        .filter(
            Product.brand_id.isnot(None),
            SupplierProduct.organization_id == Product.organization_id,
            Product.organization_id == Brand.organization_id,
            SupplierProduct.organization_id == org_id,
        )
        .distinct()
        .all()
    )
    assert len(sb_candidates) == 1
    row = sb_candidates[0]
    sb_new = SupplierBrand(organization_id=row[0], supplier_id=row[1], brand_id=row[2])
    db_session.add(sb_new)

    bc_candidates = (
        db_session.query(Product.organization_id, Product.brand_id, Product.category_id)
        .join(Brand, Product.brand_id == Brand.id)
        .join(Category, Product.category_id == Category.id)
        .filter(
            Product.brand_id.isnot(None),
            Product.category_id.isnot(None),
            Product.organization_id == Brand.organization_id,
            Product.organization_id == Category.organization_id,
            Product.organization_id == org_id,
        )
        .distinct()
        .all()
    )
    assert len(bc_candidates) == 1
    row_bc = bc_candidates[0]
    bc_new = BrandCategory(organization_id=row_bc[0], brand_id=row_bc[1], category_id=row_bc[2])
    db_session.add(bc_new)
    db_session.commit()

    # Verify rows created
    sb_db = db_session.query(SupplierBrand).filter(SupplierBrand.organization_id == org_id).first()
    assert sb_db is not None
    assert sb_db.supplier_id == sup.id
    assert sb_db.brand_id == brand.id

    bc_db = db_session.query(BrandCategory).filter(BrandCategory.organization_id == org_id).first()
    assert bc_db is not None
    assert bc_db.brand_id == brand.id
    assert bc_db.category_id == cat.id


if __name__ == "__main__":
    pytest.main(["-v", __file__])
