"""Test suite for Product Image URL in Order Item Responses."""

import os
import sys
import uuid
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.main import app
from app.core.database import SessionLocal
from app.models.sales_order import SalesOrder, SalesOrderItem
from app.models.customer import Customer
from app.models.product import Product, ProductVariant
from app.models.user import User

client = TestClient(app)


def _register_org(name_prefix: str = "Image Test Org"):
    email = f"admin_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post(
        "/auth/register",
        json={
            "organization_name": f"{name_prefix} {uuid.uuid4().hex[:6]}",
            "admin_name": "Admin User",
            "email": email,
            "password": "Password123!",
            "role": "admin",
        },
    )
    assert r.status_code == 201, r.text
    token = r.json()["tokens"]["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        org_id = user.organization_id
    finally:
        db.close()

    return headers, org_id


def test_order_item_product_image_resolution():
    headers, org_id = _register_org("Order Image Org")

    db = SessionLocal()
    try:
        # Customer
        customer = Customer(
            organization_id=org_id,
            name=f"Customer {uuid.uuid4().hex[:6]}",
            phone=f"+91{uuid.uuid4().int % 10000000000:010d}",
        )
        db.add(customer)

        # 1. Product with cover image only
        prod_1 = Product(
            organization_id=org_id,
            name="Product With Cover Image",
            sku=f"SKU-1-{uuid.uuid4().hex[:6]}",
            price=150.0,
            cover_image="/files/prod-cover-123.png",
        )
        db.add(prod_1)

        # 2. Product with cover image + Variant with image
        prod_2 = Product(
            organization_id=org_id,
            name="Product With Variant Image",
            sku=f"SKU-2-{uuid.uuid4().hex[:6]}",
            price=200.0,
            cover_image="/files/prod-cover-456.png",
        )
        db.add(prod_2)
        db.flush()

        var_2_with_img = ProductVariant(
            product_id=prod_2.id,
            name="Red - 1L",
            sku=f"VAR-2A-{uuid.uuid4().hex[:6]}",
            price=200.0,
            image_url="/files/var-red-789.png",
        )
        var_2_no_img = ProductVariant(
            product_id=prod_2.id,
            name="Blue - 1L",
            sku=f"VAR-2B-{uuid.uuid4().hex[:6]}",
            price=200.0,
            image_url=None,
        )
        db.add(var_2_with_img)
        db.add(var_2_no_img)

        # 3. Product with NO image + Variant with NO image
        prod_3 = Product(
            organization_id=org_id,
            name="Product Without Image",
            sku=f"SKU-3-{uuid.uuid4().hex[:6]}",
            price=80.0,
            cover_image=None,
        )
        db.add(prod_3)
        db.flush()

        var_3_no_img = ProductVariant(
            product_id=prod_3.id,
            name="Standard",
            sku=f"VAR-3-{uuid.uuid4().hex[:6]}",
            price=80.0,
            image_url=None,
        )
        db.add(var_3_no_img)

        # Commit catalog setup
        db.commit()
        db.refresh(customer)
        db.refresh(prod_1)
        db.refresh(prod_2)
        db.refresh(var_2_with_img)
        db.refresh(var_2_no_img)
        db.refresh(prod_3)
        db.refresh(var_3_no_img)

        # Create Order with 5 line items:
        # Item 1: Product 1 (cover image)
        # Item 2: Product 2 + Variant with image (should use variant image)
        # Item 3: Product 2 + Variant without image (should fallback to product cover image)
        # Item 4: Product 3 + Variant without image (should be null)
        # Item 5: Unlinked / custom product item (should be null)
        order = SalesOrder(
            organization_id=org_id,
            order_number=f"SO-IMG-{uuid.uuid4().hex[:6]}",
            customer_id=customer.id,
            status="confirmed",
            fulfilment_status="not_started",
            total=700.0,
            subtotal=700.0,
            discount=0,
            tax=0,
            source="direct",
        )
        item_1 = SalesOrderItem(
            order=order,
            product_id=prod_1.id,
            variant_id=None,
            product_name=prod_1.name,
            quantity=1,
            unit_price=150.0,
            line_total=150.0,
            uom="pcs",
        )
        item_2 = SalesOrderItem(
            order=order,
            product_id=prod_2.id,
            variant_id=var_2_with_img.id,
            product_name=f"{prod_2.name} - {var_2_with_img.name}",
            quantity=1,
            unit_price=200.0,
            line_total=200.0,
            uom="pcs",
        )
        item_3 = SalesOrderItem(
            order=order,
            product_id=prod_2.id,
            variant_id=var_2_no_img.id,
            product_name=f"{prod_2.name} - {var_2_no_img.name}",
            quantity=1,
            unit_price=200.0,
            line_total=200.0,
            uom="pcs",
        )
        item_4 = SalesOrderItem(
            order=order,
            product_id=prod_3.id,
            variant_id=var_3_no_img.id,
            product_name=prod_3.name,
            quantity=1,
            unit_price=80.0,
            line_total=80.0,
            uom="pcs",
        )
        item_5 = SalesOrderItem(
            order=order,
            product_id=None,
            variant_id=None,
            product_name="Custom Service Item",
            quantity=1,
            unit_price=70.0,
            line_total=70.0,
            uom="hrs",
        )
        order.items.extend([item_1, item_2, item_3, item_4, item_5])
        db.add(order)
        db.commit()
        db.refresh(order)

        order_id = order.id

        # Verify Detail Endpoint: GET /orders/{order_id}
        res_detail = client.get(f"/orders/{order_id}", headers=headers)
        assert res_detail.status_code == 200, res_detail.text
        detail_data = res_detail.json()
        items = detail_data["items"]
        assert len(items) == 5

        # Test 1: Product cover image
        assert items[0]["product_id"] == prod_1.id
        assert items[0]["product_image_url"] == "/files/prod-cover-123.png"

        # Test 2: Variant image takes precedence
        assert items[1]["product_id"] == prod_2.id
        assert items[1]["variant_id"] == var_2_with_img.id
        assert items[1]["product_image_url"] == "/files/var-red-789.png"

        # Test 3: Variant without image falls back to product cover image
        assert items[2]["product_id"] == prod_2.id
        assert items[2]["variant_id"] == var_2_no_img.id
        assert items[2]["product_image_url"] == "/files/prod-cover-456.png"

        # Test 4: No image -> null
        assert items[3]["product_id"] == prod_3.id
        assert items[3]["product_image_url"] is None

        # Test 5: Unlinked product -> null
        assert items[4]["product_id"] is None
        assert items[4]["product_image_url"] is None

        # Test 6: Verify all existing fields preserved
        for it in items:
            assert "id" in it
            assert "product_id" in it
            assert "variant_id" in it
            assert "product_name" in it
            assert "quantity" in it
            assert "ordered_quantity" in it
            assert "reserved_quantity" in it
            assert "delivered_quantity" in it
            assert "remaining_quantity" in it
            assert "unit_price" in it
            assert "discount" in it
            assert "discount_percent" in it
            assert "cost_price" in it
            assert "tax_rate" in it
            assert "tax_amount" in it
            assert "line_total" in it
            assert "uom" in it
            assert "product_image_url" in it

        # Verify List Endpoint: GET /orders
        res_list = client.get("/orders", headers=headers)
        assert res_list.status_code == 200, res_list.text
        list_data = res_list.json()
        target_order = next((o for o in list_data if o["id"] == order_id), None)
        assert target_order is not None
        list_items = target_order["items"]
        assert len(list_items) == 5

        assert list_items[0]["product_image_url"] == "/files/prod-cover-123.png"
        assert list_items[1]["product_image_url"] == "/files/var-red-789.png"
        assert list_items[2]["product_image_url"] == "/files/prod-cover-456.png"
        assert list_items[3]["product_image_url"] is None
        assert list_items[4]["product_image_url"] is None

    finally:
        db.close()


def test_tenant_isolation_product_image():
    # Org A and Org B
    headers_a, org_a = _register_org("Org A Image Iso")
    headers_b, org_b = _register_org("Org B Image Iso")

    db = SessionLocal()
    try:
        # Org A product & order
        cust_a = Customer(organization_id=org_a, name="Cust A", phone="+919999999901")
        prod_a = Product(
            organization_id=org_a,
            name="Org A Product",
            sku=f"SKU-A-{uuid.uuid4().hex[:6]}",
            price=100.0,
            cover_image="/files/org-a-cover.png",
        )
        db.add_all([cust_a, prod_a])
        db.flush()

        order_a = SalesOrder(
            organization_id=org_a,
            order_number=f"SO-A-{uuid.uuid4().hex[:6]}",
            customer_id=cust_a.id,
            status="confirmed",
            fulfilment_status="not_started",
            total=100.0,
            subtotal=100.0,
            discount=0,
            tax=0,
            source="direct",
        )
        item_a = SalesOrderItem(
            order=order_a,
            product_id=prod_a.id,
            variant_id=None,
            product_name=prod_a.name,
            quantity=1,
            unit_price=100.0,
            line_total=100.0,
        )
        order_a.items.append(item_a)
        db.add(order_a)

        # Org B product & order
        cust_b = Customer(organization_id=org_b, name="Cust B", phone="+919999999902")
        prod_b = Product(
            organization_id=org_b,
            name="Org B Product",
            sku=f"SKU-B-{uuid.uuid4().hex[:6]}",
            price=200.0,
            cover_image="/files/org-b-cover.png",
        )
        db.add_all([cust_b, prod_b])
        db.flush()

        order_b = SalesOrder(
            organization_id=org_b,
            order_number=f"SO-B-{uuid.uuid4().hex[:6]}",
            customer_id=cust_b.id,
            status="confirmed",
            fulfilment_status="not_started",
            total=200.0,
            subtotal=200.0,
            discount=0,
            tax=0,
            source="direct",
        )
        item_b = SalesOrderItem(
            order=order_b,
            product_id=prod_b.id,
            variant_id=None,
            product_name=prod_b.name,
            quantity=1,
            unit_price=200.0,
            line_total=200.0,
        )
        order_b.items.append(item_b)
        db.add(order_b)

        db.commit()

        # Org A user cannot access Org B order
        r_cross = client.get(f"/orders/{order_b.id}", headers=headers_a)
        assert r_cross.status_code == 404

        # Org A sees only Org A order with Org A image
        r_a = client.get(f"/orders/{order_a.id}", headers=headers_a)
        assert r_a.status_code == 200
        assert r_a.json()["items"][0]["product_image_url"] == "/files/org-a-cover.png"

        # Org B sees only Org B order with Org B image
        r_b = client.get(f"/orders/{order_b.id}", headers=headers_b)
        assert r_b.status_code == 200
        assert r_b.json()["items"][0]["product_image_url"] == "/files/org-b-cover.png"

    finally:
        db.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
