"""Product variant API contract: SKU, pricing, inventory, Order/Quotation UUID
references, and the Category -> Sub Category -> Product Type hierarchy.

Variant business-ID numbering itself is covered in test_unique_business_id_system.py.
"""

import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.database import Base, engine
from app.core.security import create_access_token
from app.main import app
from app.models import Customer, Organization, Product, ProductVariant, User
from app.schemas.quotation import QuotationItemCreate
from app.services import numbering_service, order_service, quotation_service


@pytest.fixture
def db_session():
    Base.metadata.create_all(bind=engine)
    session = Session(bind=engine)
    try:
        yield session
    finally:
        session.close()


def _org_client(db_session: Session, name: str = "Variant Contract Org"):
    org = Organization(id=str(uuid.uuid4()), name=name, company_code="CMP-10001")
    db_session.add(org)
    db_session.flush()
    user = User(
        id=str(uuid.uuid4()), organization_id=org.id, email=f"vc_{uuid.uuid4().hex[:8]}@x.com",
        name="Admin", password_hash="h", role="admin", system_role="admin", is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    client = TestClient(app)
    client.headers = {"Authorization": f"Bearer {create_access_token(user.id, role='admin', organization_id=org.id)}"}
    return client, org, user


@pytest.fixture
def ctx(db_session: Session):
    return _org_client(db_session)


def _variant(product: dict, name: str) -> dict:
    return next(v for v in product["variations"] if v["name"] == name)


# ------------------------------------ SKU ------------------------------------


def test_variant_sku_persists_separately_from_product_sku(ctx, db_session: Session):
    client, org, _ = ctx
    res = client.post("/products", json={
        "name": "Havells Battery", "sku": "HAV-BAT",
        "variations": [{"name": "Small", "sku": "HAV-BAT-20W-S"}, {"name": "Plain"}],
    })
    assert res.status_code == 201, res.text
    product = res.json()
    assert product["sku"] == "HAV-BAT"
    assert _variant(product, "Small")["sku"] == "HAV-BAT-20W-S"
    assert _variant(product, "Plain")["sku"] is None  # stays null, never the parent SKU

    small = _variant(product, "Small")
    res = client.patch(f"/products/{product['id']}", json={"variations": [{"id": small["id"], "name": "Small", "sku": "HAV-BAT-20W-S2"}]})
    patched = res.json()
    assert _variant(patched, "Small")["sku"] == "HAV-BAT-20W-S2"
    assert patched["sku"] == "HAV-BAT"
    detail = client.get(f"/products/{product['id']}").json()
    assert _variant(detail, "Small")["sku"] == "HAV-BAT-20W-S2"
    assert _variant(detail, "Small")["variant_code"] != "HAV-BAT-20W-S2"


def test_repeated_variant_skus_keep_existing_behaviour(ctx):
    # SKU had no uniqueness rule before this change and still has none.
    client, _, _ = ctx
    res = client.post("/products", json={"name": "Dup", "variations": [{"name": "a", "sku": "SAME"}, {"name": "b", "sku": "SAME"}]})
    assert res.status_code == 201
    codes = {v["variant_code"] for v in res.json()["variations"]}
    assert len(codes) == 2  # business IDs stay unique regardless


# ---------------------------------- Pricing ----------------------------------


def test_variant_pricing_persists_and_is_returned(ctx):
    client, _, _ = ctx
    res = client.post("/products", json={
        "name": "Priced", "pricing": {"selling_price": 500, "purchase_price": 300, "mrp": 600},
        "variations": [
            {"name": "S", "pricing": {"purchase_price": 80, "selling_price": 120, "mrp": 150,
                                      "wholesale_price": 100, "dealer_price": 95, "discount_percent": 5,
                                      "tax_inclusive": True, "currency": "INR"}},
            {"name": "Legacy", "price": 70},
        ],
    })
    assert res.status_code == 201, res.text
    product = res.json()
    s = _variant(product, "S")
    assert s["price"] == 120  # legacy field mirrors selling_price
    assert s["pricing"] == {
        "purchase_price": 80, "selling_price": 120, "mrp": 150, "wholesale_price": 100,
        "dealer_price": 95, "discount_percent": 5, "tax_inclusive": True, "currency": "INR",
    }
    legacy = _variant(product, "Legacy")
    assert legacy["price"] == 70 and legacy["pricing"]["selling_price"] == 70
    # Nothing invented for a variant that only sent a price
    assert all(legacy["pricing"][k] is None for k in
               ("purchase_price", "mrp", "wholesale_price", "dealer_price", "discount_percent", "tax_inclusive", "currency"))
    # Parent pricing untouched
    assert product["price"] == 500 and product["pricing"]["purchase_price"] == 300 and product["pricing"]["mrp"] == 600

    # Partial pricing update touches only what was sent
    res = client.patch(f"/products/{product['id']}", json={"variations": [{"id": s["id"], "name": "S", "pricing": {"mrp": 160}}]})
    s2 = _variant(res.json(), "S")
    assert s2["pricing"]["mrp"] == 160 and s2["pricing"]["purchase_price"] == 80 and s2["price"] == 120
    res = client.patch(f"/products/{product['id']}", json={"variations": [{"id": s["id"], "name": "S", "price": 130}]})
    s3 = _variant(res.json(), "S")
    assert s3["price"] == 130 and s3["pricing"]["selling_price"] == 130 and s3["pricing"]["mrp"] == 160
    assert res.json()["price"] == 500


@pytest.mark.parametrize("bad", [
    {"price": 10, "pricing": {"selling_price": 12}},  # conflicting
    {"pricing": {"mrp": -1}},
    {"pricing": {"discount_percent": 101}},
    {"price": -5},
])
def test_invalid_variant_pricing_rejected(ctx, bad):
    client, _, _ = ctx
    res = client.post("/products", json={"name": "Bad", "variations": [{"name": "v", **bad}]})
    assert res.status_code == 422


# --------------------------------- Inventory ---------------------------------


def test_variant_inventory_is_independent_and_totals_correctly(ctx, db_session: Session):
    client, _, _ = ctx
    product = client.post("/products", json={
        "name": "Stocked", "variations": [{"name": "Small", "inventory": 10}, {"name": "Medium", "inventory": 10}],
    }).json()
    assert product["total_stock"] == 20  # not double-counted
    small, medium = _variant(product, "Small"), _variant(product, "Medium")

    res = client.patch(f"/products/{product['id']}", json={"variations": [{"id": small["id"], "name": "Small", "inventory": 3}]})
    patched = res.json()
    assert _variant(patched, "Small")["inventory"] == 3
    assert _variant(patched, "Medium")["inventory"] == 10
    assert patched["total_stock"] == 13

    neg = client.patch(f"/products/{product['id']}", json={"variations": [{"id": medium["id"], "name": "Medium", "inventory": -1}]})
    assert neg.status_code == 422

    assert client.delete(f"/products/{product['id']}/variants/{small['id']}").status_code == 204
    after = client.get(f"/products/{product['id']}").json()
    assert after["total_stock"] == 10 and [v["id"] for v in after["variations"]] == [medium["id"]]


# ------------------------- Order / Quotation (UUID refs) -------------------------


def _catalog(db_session: Session, org: Organization):
    product = Product(organization_id=org.id, name="Sellable", price=100.0)
    other_product = Product(organization_id=org.id, name="Other", price=50.0)
    db_session.add_all([product, other_product])
    db_session.flush()
    variant = ProductVariant(
        product_id=product.id, name="Large", sku="SELL-L", price=150.0, inventory=5,
        variant_code=numbering_service.next_variant_code(db_session, org.id),
    )
    db_session.add(variant)
    customer = Customer(organization_id=org.id, name="Buyer", customer_id=f"CS-T-{uuid.uuid4().hex[:6]}")
    db_session.add(customer)
    db_session.commit()
    return product, other_product, variant, customer


def _quote_line(product_id, variant_id):
    return QuotationItemCreate(product_id=product_id, variant_id=variant_id, quantity=1, unit_price=150)


def test_quotation_items_use_variant_uuid_and_validate_ownership(ctx, db_session: Session):
    _, org, _ = ctx
    product, other_product, variant, _ = _catalog(db_session, org)
    _, other_org, _ = _org_client(db_session, "Quote Other Org")
    foreign_product, _, foreign_variant, _ = _catalog(db_session, other_org)

    items = quotation_service._build_items(db_session, org.id, [_quote_line(product.id, variant.id)])
    assert items[0].variant_id == variant.id  # the internal UUID, not variant_code
    assert "Large" in items[0].product_name

    for line in (
        _quote_line(other_product.id, variant.id),          # variant of a different product
        _quote_line(product.id, foreign_variant.id),        # cross-tenant variant
        _quote_line(product.id, "not-a-uuid"),              # malformed id
        _quote_line(product.id, variant.variant_code),      # business ID is not a reference
        _quote_line(foreign_product.id, foreign_variant.id),  # cross-tenant product
    ):
        with pytest.raises(HTTPException) as exc:
            quotation_service._build_items(db_session, org.id, [line])
        assert exc.value.status_code == 400
    db_session.rollback()


def test_order_items_use_variant_uuid_price_and_validate_ownership(ctx, db_session: Session):
    _, org, user = ctx
    product, other_product, variant, customer = _catalog(db_session, org)
    _, other_org, _ = _org_client(db_session, "Order Other Org")
    _, _, foreign_variant, _ = _catalog(db_session, other_org)
    user = db_session.get(User, user.id)

    order, _ = order_service.place_order(
        db_session, user, customer, [order_service.OrderLine(product.id, 2, variant_id=variant.id)],
        create_as_draft=True,
    )
    item = order.items[0]
    assert item.variant_id == variant.id
    assert item.unit_price == 150.0  # variant selling price, not the parent's 100
    db_session.rollback()

    for bad_variant, bad_product in (
        (variant.id, other_product.id),
        (foreign_variant.id, product.id),
        ("not-a-uuid", product.id),
    ):
        with pytest.raises(HTTPException) as exc:
            order_service.place_order(
                db_session, user, customer, [order_service.OrderLine(bad_product, 1, variant_id=bad_variant)],
                create_as_draft=True,
            )
        assert exc.value.status_code == 400
        db_session.rollback()


# ------------------------------- Classification -------------------------------


def _categories(client):
    cat = client.post("/categories", json={"name": f"Batteries-{uuid.uuid4().hex[:6]}"}).json()
    sub = client.post("/categories", json={"name": f"Inverter-{uuid.uuid4().hex[:6]}", "parent_id": cat["id"]}).json()
    other = client.post("/categories", json={"name": f"Cables-{uuid.uuid4().hex[:6]}"}).json()
    return cat, sub, other


def test_category_subcategory_product_type_stay_separate(ctx):
    client, _, _ = ctx
    cat, sub, other = _categories(client)
    res = client.post("/products", json={
        "name": "Classified", "category_id": cat["id"], "sub_category_id": sub["id"], "product_type": "physical",
    })
    assert res.status_code == 201, res.text
    product = res.json()
    assert (product["category_id"], product["sub_category_id"], product["product_type"]) == (cat["id"], sub["id"], "physical")
    assert product["category"]["id"] == cat["id"] and product["subcategory"]["id"] == sub["id"]

    # Untouched classification is preserved by an unrelated PATCH
    res = client.patch(f"/products/{product['id']}", json={"name": "Renamed"})
    assert (res.json()["category_id"], res.json()["sub_category_id"], res.json()["product_type"]) == (cat["id"], sub["id"], "physical")
    # A product without a category is still fine
    assert client.post("/products", json={"name": "Loose", "product_type": "service"}).status_code == 201


def test_invalid_classification_rejected(ctx):
    client, _, _ = ctx
    cat, sub, other = _categories(client)
    assert client.post("/products", json={"name": "x", "category_id": other["id"], "sub_category_id": sub["id"]}).status_code == 400
    assert client.post("/products", json={"name": "x", "sub_category_id": sub["id"]}).status_code == 400
    assert client.post("/products", json={"name": "x", "category_id": cat["id"], "sub_category_id": other["id"]}).status_code == 400
    # Product Type is never a category id
    for bad in (cat["id"], sub["id"]):
        res = client.post("/products", json={"name": "x", "category_id": cat["id"], "product_type": bad})
        assert res.status_code == 400

    product = client.post("/products", json={"name": "ok", "category_id": cat["id"], "sub_category_id": sub["id"]}).json()
    # Moving to another category while keeping a sub category of the old one is rejected
    assert client.patch(f"/products/{product['id']}", json={"category_id": other["id"]}).status_code == 400
    assert client.patch(f"/products/{product['id']}", json={"category_id": other["id"], "sub_category_id": None}).status_code == 200
    assert client.patch(f"/products/{product['id']}", json={"product_type": other["id"]}).status_code == 400
    assert client.get(f"/products/{product['id']}").json()["product_type"] is None


def test_cross_tenant_category_rejected(ctx, db_session: Session):
    client, _, _ = ctx
    other_client, _, _ = _org_client(db_session, "Category Other Org")
    foreign_cat, foreign_sub, _ = _categories(other_client)
    assert client.post("/products", json={"name": "x", "category_id": foreign_cat["id"]}).status_code == 400
    assert client.post("/products", json={"name": "x", "category_id": foreign_cat["id"], "sub_category_id": foreign_sub["id"]}).status_code == 400
