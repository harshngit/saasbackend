"""Tests for Quotation & Delivery product image consistency:
- Q1-Q8: QuotationItemOut product_image_url (cover image, variant precedence, variant fallback, no image, list, detail, fields preserved, tenant isolation)
- D1-D8: DeliveryLineOut product_image_url (cover image, variant precedence, variant fallback, no image, detail, assigned list, fields preserved, tenant isolation)
"""

import os
import sys
import uuid

os.environ["DATABASE_URL"] = "sqlite:///./crm_saas.db"
os.environ["TESTING"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

PASSED = 0
FAILED = 0


def log_test(name: str, condition: bool, extra: str = ""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name} {extra}".rstrip())


def _register_org(label: str) -> dict:
    email = f"admin_{uuid.uuid4().hex[:8]}@{label.replace('_', '').lower()}.com"
    r = client.post(
        "/auth/register",
        json={
            "organization_name": f"{label} Org",
            "admin_name": f"Admin {label}",
            "email": email,
            "password": "Password123!",
            "role": "admin",
        },
    )
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}


def _create_staff(admin_auth: dict, name: str, role_name: str) -> tuple[dict, dict]:
    email = f"{name.lower().replace(' ', '_')}_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post(
        "/users",
        json={"name": name, "email": email, "password": "Password123!", "role": role_name},
        headers=admin_auth,
    )
    assert r.status_code == 201, r.text
    user = r.json()
    login = client.post("/auth/login", json={"email": email, "password": "Password123!"})
    assert login.status_code == 200, login.text
    return user, {"Authorization": f"Bearer {login.json()['tokens']['access_token']}"}


def run_quotation_image_tests():
    print("\n=== QUOTATION PRODUCT IMAGE CONSISTENCY ===")
    admin_auth = _register_org("QuotImg")

    # Setup Customer
    cust = client.post("/customers", json={"name": "Quot Customer"}, headers=admin_auth).json()

    # Product 1: Cover image only
    prod_cover = client.post(
        "/products",
        json={
            "name": "Product With Cover",
            "sku": f"SKU-C-{uuid.uuid4().hex[:6]}",
            "price": 100.0,
            "cover_image": "/files/cov_prod_123.jpg",
        },
        headers=admin_auth,
    ).json()

    # Product 2: Cover image AND Variant with image_url
    prod_variant_with_img = client.post(
        "/products",
        json={
            "name": "Product With Variant Img",
            "sku": f"SKU-V-{uuid.uuid4().hex[:6]}",
            "price": 150.0,
            "cover_image": "/files/cov_prod_fallback.jpg",
            "variations": [
                {
                    "name": "Blue / XL",
                    "sku": f"VAR-B-{uuid.uuid4().hex[:6]}",
                    "price": 150.0,
                    "image_url": "/files/var_blue_xl.jpg",
                }
            ],
        },
        headers=admin_auth,
    ).json()
    var_with_img_id = prod_variant_with_img["variations"][0]["id"]

    # Product 3: Cover image AND Variant WITHOUT image_url (fallback to cover)
    prod_variant_no_img = client.post(
        "/products",
        json={
            "name": "Product With Variant No Img",
            "sku": f"SKU-VN-{uuid.uuid4().hex[:6]}",
            "price": 120.0,
            "cover_image": "/files/cov_prod_should_fallback.jpg",
            "variations": [
                {
                    "name": "Red / S",
                    "sku": f"VAR-R-{uuid.uuid4().hex[:6]}",
                    "price": 120.0,
                    "image_url": None,
                }
            ],
        },
        headers=admin_auth,
    ).json()
    var_no_img_id = prod_variant_no_img["variations"][0]["id"]

    # Product 4: No image at all
    prod_no_img = client.post(
        "/products",
        json={
            "name": "Product Plain No Img",
            "sku": f"SKU-P-{uuid.uuid4().hex[:6]}",
            "price": 80.0,
            "cover_image": None,
        },
        headers=admin_auth,
    ).json()

    # Create Quotation with all 4 cases
    quot_res = client.post(
        "/quotations",
        json={
            "customer_id": cust["id"],
            "items": [
                {
                    "product_id": prod_cover["id"],
                    "quantity": 2,
                    "unit_price": 100.0,
                },
                {
                    "product_id": prod_variant_with_img["id"],
                    "variant_id": var_with_img_id,
                    "quantity": 1,
                    "unit_price": 150.0,
                },
                {
                    "product_id": prod_variant_no_img["id"],
                    "variant_id": var_no_img_id,
                    "quantity": 3,
                    "unit_price": 120.0,
                },
                {
                    "product_id": prod_no_img["id"],
                    "quantity": 5,
                    "unit_price": 80.0,
                },
            ],
        },
        headers=admin_auth,
    )
    assert quot_res.status_code == 201, quot_res.text
    quot = quot_res.json()
    items = quot["items"]
    assert len(items) == 4

    # Q1 — Product cover image
    log_test(
        "Q1. QuotationItem returns Product.cover_image",
        items[0]["product_image_url"] == "/files/cov_prod_123.jpg",
        f"got {items[0].get('product_image_url')}",
    )

    # Q2 — Variant image precedence
    log_test(
        "Q2. Variant image takes precedence over Product.cover_image",
        items[1]["product_image_url"] == "/files/var_blue_xl.jpg",
        f"got {items[1].get('product_image_url')}",
    )

    # Q3 — Variant fallback to Product cover image
    log_test(
        "Q3. Variant with no image falls back to Product.cover_image",
        items[2]["product_image_url"] == "/files/cov_prod_should_fallback.jpg",
        f"got {items[2].get('product_image_url')}",
    )

    # Q4 — No image returns None/null
    log_test(
        "Q4. Product with no image returns None/null",
        items[3]["product_image_url"] is None,
        f"got {items[3].get('product_image_url')}",
    )

    # Q6 — Quotation detail endpoint returns product_image_url
    detail_res = client.get(f"/quotations/{quot['id']}", headers=admin_auth)
    assert detail_res.status_code == 200, detail_res.text
    d_items = detail_res.json()["items"]
    log_test(
        "Q6. GET /quotations/{id} detail returns product_image_url on each item",
        d_items[0]["product_image_url"] == "/files/cov_prod_123.jpg"
        and d_items[1]["product_image_url"] == "/files/var_blue_xl.jpg"
        and d_items[2]["product_image_url"] == "/files/cov_prod_should_fallback.jpg"
        and d_items[3]["product_image_url"] is None,
    )

    # Q7 — Existing fields preserved on QuotationItemOut
    log_test(
        "Q7. Existing QuotationItemOut fields preserved (id, product_name, quantity, line_total, tax_amount)",
        all(
            "id" in it
            and "product_name" in it
            and "quantity" in it
            and "unit_price" in it
            and "line_total" in it
            and "tax_amount" in it
            for it in d_items
        ),
    )

    # Q8 — Tenant isolation
    admin_auth_other = _register_org("QuotOther")
    other_get = client.get(f"/quotations/{quot['id']}", headers=admin_auth_other)
    log_test(
        "Q8. Cross-tenant quotation access returns 404",
        other_get.status_code == 404,
        f"got status {other_get.status_code}",
    )


def run_delivery_image_tests():
    print("\n=== DELIVERY PRODUCT IMAGE CONSISTENCY ===")
    admin_auth = _register_org("DelivImg")

    # Setup Warehouse & Partner
    wh = client.post("/warehouses", json={"name": "Main WH", "is_default": True}, headers=admin_auth).json()
    partner, partner_auth = _create_staff(admin_auth, "Dave Driver", "delivery_partner")
    cust = client.post("/customers", json={"name": "Deliv Customer"}, headers=admin_auth).json()

    # Product 1: Cover image only
    prod_cover = client.post(
        "/products",
        json={
            "name": "Deliv Cover Prod",
            "sku": f"SKU-DC-{uuid.uuid4().hex[:6]}",
            "price": 50.0,
            "cover_image": "/files/deliv_cov_123.jpg",
        },
        headers=admin_auth,
    ).json()
    client.post(f"/warehouses/{wh['id']}/stock/adjust", json={"product_id": prod_cover["id"], "quantity": 100}, headers=admin_auth)

    # Product 2: Cover image AND Variant with image_url
    prod_variant_with_img = client.post(
        "/products",
        json={
            "name": "Deliv Variant Img Prod",
            "sku": f"SKU-DV-{uuid.uuid4().hex[:6]}",
            "price": 75.0,
            "cover_image": "/files/deliv_fallback.jpg",
            "variations": [
                {
                    "name": "Green / M",
                    "sku": f"VAR-G-{uuid.uuid4().hex[:6]}",
                    "price": 75.0,
                    "image_url": "/files/deliv_var_green.jpg",
                }
            ],
        },
        headers=admin_auth,
    ).json()
    var_with_img_id = prod_variant_with_img["variations"][0]["id"]
    client.post(f"/warehouses/{wh['id']}/stock/adjust", json={"product_id": prod_variant_with_img["id"], "variant_id": var_with_img_id, "quantity": 100}, headers=admin_auth)

    # Product 3: Cover image AND Variant WITHOUT image_url (fallback)
    prod_variant_no_img = client.post(
        "/products",
        json={
            "name": "Deliv Variant No Img",
            "sku": f"SKU-DVN-{uuid.uuid4().hex[:6]}",
            "price": 90.0,
            "cover_image": "/files/deliv_variant_fallback.jpg",
            "variations": [
                {
                    "name": "Yellow / L",
                    "sku": f"VAR-Y-{uuid.uuid4().hex[:6]}",
                    "price": 90.0,
                    "image_url": None,
                }
            ],
        },
        headers=admin_auth,
    ).json()
    var_no_img_id = prod_variant_no_img["variations"][0]["id"]
    client.post(f"/warehouses/{wh['id']}/stock/adjust", json={"product_id": prod_variant_no_img["id"], "variant_id": var_no_img_id, "quantity": 100}, headers=admin_auth)

    # Product 4: Plain no image
    prod_no_img = client.post(
        "/products",
        json={
            "name": "Deliv No Img Prod",
            "sku": f"SKU-DNP-{uuid.uuid4().hex[:6]}",
            "price": 40.0,
            "cover_image": None,
        },
        headers=admin_auth,
    ).json()
    client.post(f"/warehouses/{wh['id']}/stock/adjust", json={"product_id": prod_no_img["id"], "quantity": 100}, headers=admin_auth)

    # Create & Confirm Order
    order_res = client.post(
        "/orders",
        json={
            "customer_id": cust["id"],
            "warehouse_id": wh["id"],
            "assigned_delivery_partner_id": partner["id"],
            "items": [
                {"product_id": prod_cover["id"], "quantity": 2, "unit_price": 50.0},
                {"product_id": prod_variant_with_img["id"], "variant_id": var_with_img_id, "quantity": 1, "unit_price": 75.0},
                {"product_id": prod_variant_no_img["id"], "variant_id": var_no_img_id, "quantity": 3, "unit_price": 90.0},
                {"product_id": prod_no_img["id"], "quantity": 4, "unit_price": 40.0},
            ],
        },
        headers=admin_auth,
    )
    assert order_res.status_code == 201, order_res.text
    order = order_res.json()
    conf = client.post(f"/orders/{order['id']}/confirm", headers=admin_auth)
    assert conf.status_code == 200, conf.text

    # Plan Delivery
    deliv_res = client.post(
        "/deliveries",
        json={
            "order_id": order["id"],
            "delivery_partner_id": partner["id"],
            "warehouse_id": wh["id"],
        },
        headers=admin_auth,
    )
    assert deliv_res.status_code == 201, deliv_res.text
    deliv = deliv_res.json()
    d_lines = deliv["items"]
    assert len(d_lines) == 4

    # D1 — Product cover image on DeliveryLineOut
    log_test(
        "D1. DeliveryLineOut returns Product.cover_image",
        d_lines[0]["product_image_url"] == "/files/deliv_cov_123.jpg",
        f"got {d_lines[0].get('product_image_url')}",
    )

    # D2 — Variant image precedence on DeliveryLineOut
    log_test(
        "D2. Variant image takes precedence on DeliveryLineOut",
        d_lines[1]["product_image_url"] == "/files/deliv_var_green.jpg",
        f"got {d_lines[1].get('product_image_url')}",
    )

    # D3 — Product fallback when variant has no image
    log_test(
        "D3. Variant with no image falls back to Product.cover_image on DeliveryLineOut",
        d_lines[2]["product_image_url"] == "/files/deliv_variant_fallback.jpg",
        f"got {d_lines[2].get('product_image_url')}",
    )

    # D4 — No image returns null
    log_test(
        "D4. Delivery line with no image returns null",
        d_lines[3]["product_image_url"] is None,
        f"got {d_lines[3].get('product_image_url')}",
    )

    # D5 — Delivery detail by id
    detail = client.get(f"/deliveries/by-id/{deliv['id']}", headers=admin_auth)
    assert detail.status_code == 200, detail.text
    detail_lines = detail.json()["items"]
    log_test(
        "D5. GET /deliveries/by-id/{id} exposes product_image_url on all delivery lines",
        detail_lines[0]["product_image_url"] == "/files/deliv_cov_123.jpg"
        and detail_lines[1]["product_image_url"] == "/files/deliv_var_green.jpg"
        and detail_lines[2]["product_image_url"] == "/files/deliv_variant_fallback.jpg"
        and detail_lines[3]["product_image_url"] is None,
    )

    # D6 — Assigned deliveries for partner
    assigned = client.get("/deliveries/assigned", headers=partner_auth)
    assert assigned.status_code == 200, assigned.text
    assigned_orders = assigned.json()
    log_test(
        "D6. GET /deliveries/assigned returns assigned order items with product_image_url",
        len(assigned_orders) > 0
        and assigned_orders[0]["items"][0]["product_image_url"] == "/files/deliv_cov_123.jpg",
    )

    # D7 — Existing fields preserved on DeliveryLineOut
    log_test(
        "D7. Existing DeliveryLineOut fields preserved (id, planned_quantity, delivered_quantity, pending_quantity)",
        all(
            "id" in line
            and "product_name" in line
            and "planned_quantity" in line
            and "delivered_quantity" in line
            and "pending_quantity" in line
            for line in detail_lines
        ),
    )

    # D8 — Tenant isolation on Deliveries
    admin_auth_other = _register_org("DelivOther")
    other_deliv = client.get(f"/deliveries/by-id/{deliv['id']}", headers=admin_auth_other)
    log_test(
        "D8. Cross-tenant delivery access returns 404",
        other_deliv.status_code == 404,
        f"got status {other_deliv.status_code}",
    )


def main():
    print("=======================================================")
    print("TEST SUITE: Quotation & Delivery Product Image Consistency")
    print("=======================================================")
    run_quotation_image_tests()
    run_delivery_image_tests()
    print("\n=======================================================")
    print(f"RESULTS: {PASSED} passed, {FAILED} failed")
    print("=======================================================")
    if FAILED > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
