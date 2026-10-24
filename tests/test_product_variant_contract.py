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

    # Existing variant stock is not editable through the product: it moves via stock adjustments
    res = client.patch(f"/products/{product['id']}", json={"variations": [{"id": small["id"], "name": "Small", "inventory": 3}]})
    assert res.status_code == 422 and "stock adjustment" in res.json()["detail"]
    after = client.get(f"/products/{product['id']}").json()
    assert _variant(after, "Small")["inventory"] == 10 and after["total_stock"] == 20

    neg = client.patch(f"/products/{product['id']}", json={"variations": [{"id": medium["id"], "name": "Medium", "inventory": -1}]})
    assert neg.status_code == 422

    assert client.delete(f"/products/{product['id']}/variants/{small['id']}").status_code == 204
    after = client.get(f"/products/{product['id']}").json()
    assert after["total_stock"] == 10 and [v["id"] for v in after["variations"]] == [medium["id"]]


# --------------------------- Stock is not edited via PATCH ---------------------------


def _stock_state(db_session: Session, product_id: str):
    from app.models import StockMovement, WarehouseStock

    db_session.expire_all()
    product = db_session.get(Product, product_id)
    variants = sorted((v.id, v.name, v.inventory) for v in
                      db_session.query(ProductVariant).filter(ProductVariant.product_id == product_id))
    warehouse = sorted((w.warehouse_id, w.variant_id, w.on_hand_quantity) for w in
                       db_session.query(WarehouseStock).filter(WarehouseStock.product_id == product_id))
    moves = db_session.query(StockMovement).filter(StockMovement.product_id == product_id).count()
    return product.total_inventory, variants, warehouse, moves


def _with_warehouse_stock(db_session: Session, org: Organization, product_id: str, variant_id=None):
    from app.services import stock_service

    warehouse = stock_service.default_warehouse(db_session, org.id)
    stock_service.adjust_on_hand(db_session, org.id, warehouse.id, product_id, variant_id, 2, "adjustment")
    db_session.commit()
    return warehouse


def test_product_stock_is_set_on_create_and_not_by_patch(ctx, db_session: Session):
    client, org, _ = ctx
    created = client.post("/products", json={"name": "Counted", "total_inventory": 10})
    assert created.status_code == 201 and created.json()["total_inventory"] == 10  # opening stock kept
    product_id = created.json()["id"]
    warehouse = _with_warehouse_stock(db_session, org, product_id)  # the first movement adopts the opening stock
    before = _stock_state(db_session, product_id)
    assert before[0] == 12 and before[2][0][2] == 12 and before[3] == 1

    for value in (500, 0, None):
        res = client.patch(f"/products/{product_id}", json={"total_inventory": value})
        assert res.status_code == 422, value
        assert "stock adjustment" in res.json()["detail"]
        assert _stock_state(db_session, product_id) == before
    # Nothing else in a rejected request is applied either
    res = client.patch(f"/products/{product_id}", json={"name": "Renamed", "total_inventory": 1})
    assert res.status_code == 422
    db_session.expire_all()
    assert db_session.get(Product, product_id).name == "Counted"

    # Ordinary edits still work and leave stock alone
    res = client.patch(f"/products/{product_id}", json={"name": "Renamed", "description": "d"})
    assert res.status_code == 200 and res.json()["name"] == "Renamed"
    assert _stock_state(db_session, product_id) == before

    # The supported path still moves warehouse stock, the movement log and the counter together
    res = client.post(f"/warehouses/{warehouse.id}/stock/adjust",
                      json={"product_id": product_id, "quantity": 3, "movement_type": "adjustment"})
    assert res.status_code in (200, 201), res.text
    after = _stock_state(db_session, product_id)
    assert after[0] == 15 and sum(row[2] for row in after[2]) == 15 and after[3] == 2


def test_existing_variant_stock_is_not_edited_by_patch(ctx, db_session: Session):
    client, org, _ = ctx
    product = client.post("/products", json={
        "name": "Shirt", "variations": [{"name": "S", "inventory": 5}, {"name": "M", "inventory": 4}],
    }).json()
    small, medium = _variant(product, "S"), _variant(product, "M")
    _with_warehouse_stock(db_session, org, product["id"], small["id"])
    before = _stock_state(db_session, product["id"])

    res = client.patch(f"/products/{product['id']}", json={"variations": [
        {"id": medium["id"], "name": "Medium"},                       # a valid edit alongside...
        {"id": small["id"], "name": "S", "inventory": 50},            # ...a stock change
    ]})
    assert res.status_code == 422 and "existing variant 'S'" in res.json()["detail"]
    assert _stock_state(db_session, product["id"]) == before          # nothing applied, nothing recreated

    # Other variant fields still update; re-sending the unchanged stock is not a change
    res = client.patch(f"/products/{product['id']}", json={"variations": [
        {"id": small["id"], "name": "Small", "sku": "SH-S", "inventory": 7},
        {"id": medium["id"], "name": "Medium", "price": 120},
    ]})
    assert res.status_code == 200, res.text
    assert _variant(res.json(), "Small")["sku"] == "SH-S" and _variant(res.json(), "Medium")["price"] == 120
    after = _stock_state(db_session, product["id"])
    assert after[2] == before[2] and after[3] == before[3]
    assert sorted((v[0], v[2]) for v in after[1]) == sorted((v[0], v[2]) for v in before[1])  # same ids, same stock

    # A new variant added through PATCH still takes its opening stock
    res = client.patch(f"/products/{product['id']}", json={"variations": [{"name": "L", "inventory": 9}]})
    assert res.status_code == 200 and _variant(res.json(), "L")["inventory"] == 9
    assert {v["id"] for v in res.json()["variations"]} >= {small["id"], medium["id"]}


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


# ------------------- Sub category Product Types (Category -> Sub -> Type) -------------------


def _footwear(client):
    tag = uuid.uuid4().hex[:6]
    root = client.post("/categories", json={"name": f"Footwear-{tag}"}).json()
    casual = client.post("/categories", json={"name": f"Casual-{tag}", "parent_id": root["id"]}).json()
    sports = client.post("/categories", json={"name": f"Sports-{tag}", "parent_id": root["id"]}).json()
    return root, casual, sports


def _types_url(sub_id: str) -> str:
    return f"/categories/{sub_id}/variants"


def _add_types(client, sub_id: str, *names: str) -> dict:
    out = {}
    for name in names:
        res = client.post(_types_url(sub_id), json={"name": name})
        assert res.status_code == 201, res.text
        out[name] = res.json()
    return out


def test_subcategory_product_types_crud(ctx):
    client, _, _ = ctx
    root, casual, sports = _footwear(client)
    assert client.get(_types_url(casual["id"])).json() == []  # nothing configured: nothing invented

    types = _add_types(client, casual["id"], "Shoe", "Cheppal", "Sandal")
    listed = client.get(_types_url(casual["id"])).json()
    assert sorted(t["name"] for t in listed) == ["Cheppal", "Sandal", "Shoe"]
    assert all(t["subcategory_id"] == casual["id"] and t["is_active"] for t in listed)

    # Case-insensitive duplicates in one sub category are refused; another sub category may reuse a name
    for dup in ("shoe", " SHOE "):
        assert client.post(_types_url(casual["id"]), json={"name": dup}).status_code == 409
    assert client.post(_types_url(sports["id"]), json={"name": "Shoe"}).status_code == 201
    assert client.patch(f"{_types_url(casual['id'])}/{types['Sandal']['id']}", json={"name": "cheppal"}).status_code == 409

    # Rename, reorder, deactivate (no delete endpoint)
    res = client.patch(f"{_types_url(casual['id'])}/{types['Sandal']['id']}", json={"name": "Slipper", "sort_order": 5})
    assert res.status_code == 200 and res.json()["name"] == "Slipper"
    res = client.patch(f"{_types_url(casual['id'])}/{types['Cheppal']['id']}", json={"is_active": False})
    assert res.status_code == 200 and res.json()["is_active"] is False
    assert sorted(t["name"] for t in client.get(_types_url(casual["id"])).json()) == ["Shoe", "Slipper"]
    all_types = client.get(_types_url(casual["id"]), params={"include_inactive": True}).json()
    assert sorted(t["name"] for t in all_types) == ["Cheppal", "Shoe", "Slipper"]
    assert client.delete(f"{_types_url(casual['id'])}/{types['Shoe']['id']}").status_code == 405

    # Only a sub category carries Product Types; unknown ids are 404
    assert client.get(_types_url(root["id"])).status_code == 422
    assert client.post(_types_url(root["id"]), json={"name": "Shoe"}).status_code == 422
    assert client.get(_types_url(str(uuid.uuid4()))).status_code == 404
    assert client.patch(f"{_types_url(sports['id'])}/{types['Shoe']['id']}", json={"name": "x"}).status_code == 404
    assert client.post(_types_url(casual["id"]), json={"name": "  "}).status_code == 422


def test_subcategory_product_types_are_tenant_scoped(ctx, db_session: Session):
    client, _, _ = ctx
    other, _, _ = _org_client(db_session, "Product Type Other Org")
    _, foreign_sub, _ = _footwear(other)
    foreign = _add_types(other, foreign_sub["id"], "Shoe")["Shoe"]
    root, casual, _ = _footwear(client)

    assert client.get(_types_url(foreign_sub["id"])).status_code == 404
    assert client.post(_types_url(foreign_sub["id"]), json={"name": "Boot"}).status_code == 404
    assert client.patch(f"{_types_url(foreign_sub['id'])}/{foreign['id']}", json={"is_active": False}).status_code == 404
    assert client.patch(f"{_types_url(casual['id'])}/{foreign['id']}", json={"name": "Mine"}).status_code == 404
    # The same name in my own sub category is fine; the other firm's type stays untouched
    assert client.post(_types_url(casual["id"]), json={"name": "Shoe"}).status_code == 201
    assert [t["name"] for t in other.get(_types_url(foreign_sub["id"])).json()] == ["Shoe"]
    # Its id cannot be put on my product
    res = client.post("/products", json={"name": "x", "category_id": root["id"], "sub_category_id": casual["id"],
                                         "product_type_id": foreign["id"]})
    assert res.status_code == 422


def test_category_parent_must_be_own_top_level_category(ctx, db_session: Session):
    client, _, _ = ctx
    other, _, _ = _org_client(db_session, "Parent Other Org")
    foreign_root, _, _ = _footwear(other)
    root, casual, sports = _footwear(client)
    tag = uuid.uuid4().hex[:6]

    assert client.post("/categories", json={"name": f"A-{tag}", "parent_id": foreign_root["id"]}).status_code == 400
    assert client.post("/categories", json={"name": f"B-{tag}", "parent_id": str(uuid.uuid4())}).status_code == 400
    assert client.post("/categories", json={"name": f"C-{tag}", "parent_id": casual["id"]}).status_code == 400  # no 3rd level
    assert client.patch(f"/categories/{root['id']}", json={"parent_id": root["id"]}).status_code == 400  # self
    assert client.patch(f"/categories/{casual['id']}", json={"parent_id": foreign_root["id"]}).status_code == 400
    lone = client.post("/categories", json={"name": f"Lone-{tag}"}).json()
    assert client.patch(f"/categories/{root['id']}", json={"parent_id": lone["id"]}).status_code == 400  # has children

    # Valid operations keep working
    assert client.patch(f"/categories/{lone['id']}", json={"parent_id": root["id"]}).status_code == 200
    assert client.patch(f"/categories/{lone['id']}", json={"parent_id": None}).status_code == 200
    assert client.patch(f"/categories/{casual['id']}", json={"description": "Everyday"}).status_code == 200
    assert client.get(f"/categories/{casual['id']}").json()["parent_id"] == root["id"]
    # Organization-wide name uniqueness is unchanged
    assert client.post("/categories", json={"name": casual["name"], "parent_id": root["id"]}).status_code == 409


def test_product_type_id_on_create(ctx):
    client, _, _ = ctx
    root, casual, sports = _footwear(client)
    casual_types = _add_types(client, casual["id"], "Shoe", "Cheppal")
    sports_shoe = _add_types(client, sports["id"], "Shoe")["Shoe"]
    base = {"category_id": root["id"], "sub_category_id": casual["id"]}

    res = client.post("/products", json={"name": "Loafer", **base, "product_type_id": casual_types["Shoe"]["id"],
                                         "product_type": "physical", "variations": [{"name": "UK 8"}]})
    assert res.status_code == 201, res.text
    product = res.json()
    assert product["product_type_id"] == casual_types["Shoe"]["id"]
    assert product["product_type_ref"] == {"id": casual_types["Shoe"]["id"], "name": "Shoe"}
    assert product["product_type"] == "physical"  # legacy text kept as sent, never overwritten
    assert [v["name"] for v in product["variations"]] == ["UK 8"]  # product variants unaffected
    listed = client.get("/products", params={"search": "Loafer"}).json()["items"]
    assert listed[0]["product_type_ref"]["name"] == "Shoe" and listed[0]["product_type_id"] == product["product_type_id"]

    # Without a Product Type, and with only legacy text, products work as before
    plain = client.post("/products", json={"name": "Plain", **base}).json()
    assert plain["product_type_id"] is None and plain["product_type_ref"] is None
    assert client.post("/products", json={"name": "Legacy", "product_type": "service"}).status_code == 201

    for bad in (sports_shoe["id"], str(uuid.uuid4())):  # another sub category's type, or none at all
        assert client.post("/products", json={"name": "x", **base, "product_type_id": bad}).status_code == 422
    assert client.post("/products", json={"name": "x", "category_id": root["id"],
                                          "product_type_id": casual_types["Shoe"]["id"]}).status_code == 422
    client.patch(f"{_types_url(casual['id'])}/{casual_types['Cheppal']['id']}", json={"is_active": False})
    assert client.post("/products", json={"name": "x", **base, "product_type_id": casual_types["Cheppal"]["id"]}).status_code == 422

    # A sub category is not a category_id (existing 400 convention)
    assert client.post("/products", json={"name": "x", "category_id": casual["id"]}).status_code == 400


def test_product_type_id_on_patch_uses_the_final_combination(ctx):
    client, _, _ = ctx
    root, casual, sports = _footwear(client)
    shoe = _add_types(client, casual["id"], "Shoe")["Shoe"]
    sneaker = _add_types(client, sports["id"], "Sneaker")["Sneaker"]
    product = client.post("/products", json={"name": "Runner", "category_id": root["id"], "sub_category_id": casual["id"],
                                             "product_type_id": shoe["id"], "product_type": "legacy-text"}).json()
    url = f"/products/{product['id']}"

    # Sub category changes but the old Product Type would remain: refused, nothing cleared silently
    assert client.patch(url, json={"sub_category_id": sports["id"]}).status_code == 422
    assert client.patch(url, json={"category_id": root["id"], "sub_category_id": None}).status_code == 422
    # Only the Product Type changes: checked against the stored sub category
    assert client.patch(url, json={"product_type_id": sneaker["id"]}).status_code == 422
    after = client.get(url).json()
    assert (after["sub_category_id"], after["product_type_id"]) == (casual["id"], shoe["id"])

    # Changing both together, then clearing, works; unrelated fields are untouched
    res = client.patch(url, json={"sub_category_id": sports["id"], "product_type_id": sneaker["id"]})
    assert res.status_code == 200 and res.json()["product_type_ref"]["name"] == "Sneaker"
    res = client.patch(url, json={"product_type_id": None})
    assert res.status_code == 200 and res.json()["product_type_id"] is None
    assert res.json()["product_type"] == "legacy-text" and res.json()["sub_category_id"] == sports["id"]
    assert client.patch(url, json={"category_id": casual["id"]}).status_code == 400  # sub category as category

    # A legacy product (text only, no product_type_id) edits normally
    legacy = client.post("/products", json={"name": "Old", "category_id": root["id"], "sub_category_id": casual["id"],
                                            "product_type": "physical"}).json()
    res = client.patch(f"/products/{legacy['id']}", json={"name": "Old 2", "sub_category_id": sports["id"]})
    assert res.status_code == 200 and res.json()["product_type"] == "physical" and res.json()["product_type_id"] is None


def test_product_type_history_survives_deactivation_and_subcategory_removal(ctx):
    client, _, _ = ctx
    root, casual, _ = _footwear(client)
    shoe = _add_types(client, casual["id"], "Shoe")["Shoe"]
    product = client.post("/products", json={"name": "Classic", "category_id": root["id"],
                                             "sub_category_id": casual["id"], "product_type_id": shoe["id"]}).json()
    url = f"/products/{product['id']}"

    assert client.patch(f"{_types_url(casual['id'])}/{shoe['id']}", json={"is_active": False}).status_code == 200
    assert client.get(url).json()["product_type_ref"] == {"id": shoe["id"], "name": "Shoe"}
    assert client.patch(url, json={"name": "Classic 2"}).status_code == 200  # unrelated edit
    assert client.patch(url, json={"product_type_id": shoe["id"]}).status_code == 200  # kept, not newly chosen
    assert client.patch(url, json={"sub_category_id": casual["id"]}).status_code == 200

    # Deleting the sub category keeps the product's Product Type and its name
    assert client.delete(f"/categories/{casual['id']}").status_code == 204
    after = client.get(url).json()
    assert after["sub_category_id"] is None
    assert after["product_type_id"] == shoe["id"] and after["product_type_ref"]["name"] == "Shoe"
