"""Automated test suite for the complete Product module (all 12 spec sections)."""

import os
import sys
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from app.main import app
from app.core.database import SessionLocal
from app.models import Delivery, SalesOrder, SalesOrderItem
from app.seed import main as seed_main

seed_main()
client = TestClient(app)

_passed = 0
_failed = 0


def ok(msg: str):
    global _passed
    _passed += 1
    print(f"  PASS  {msg}")


def fail(msg: str, detail: str = ""):
    global _failed
    _failed += 1
    print(f"  FAIL  {msg}  {detail}")


def assert_eq(actual, expected, msg: str, detail: str = ""):
    if actual == expected:
        ok(msg)
    else:
        fail(msg, f"Expected {expected!r}, got {actual!r}. {detail}")


def assert_true(cond, msg: str, detail: str = ""):
    if cond:
        ok(msg)
    else:
        fail(msg, detail)


def register_org(name: str) -> dict:
    email = f"admin_{uuid.uuid4().hex[:8]}@{name.lower().replace(' ', '')}.com"
    r = client.post("/auth/register", json={
        "organization_name": name,
        "admin_name": "Admin",
        "email": email,
        "password": "Password123!",
        "role": "admin",
    })
    assert r.status_code == 201, r.text
    token = r.json()["tokens"]["access_token"]
    return {"headers": {"Authorization": f"Bearer {token}"}, "org_id": r.json()["organization"]["id"]}


def run_tests():
    print("\n=======================================================")
    print("TEST SUITE: Complete Product Module")
    print("=======================================================")

    org1 = register_org("Product Audit Org1")
    org2 = register_org("Product Audit Org2")
    h1, h2 = org1["headers"], org2["headers"]

    # ---------------- Basic Information + full create payload ----------------
    cat_res = client.post("/categories", json={"name": f"Beverages-{uuid.uuid4().hex[:6]}"}, headers=h1)
    assert cat_res.status_code == 201, cat_res.text
    cat_id = cat_res.json()["id"]

    subcat_res = client.post(
        "/categories", json={"name": f"Soft Drinks-{uuid.uuid4().hex[:6]}", "parent_id": cat_id}, headers=h1
    )
    assert subcat_res.status_code == 201, subcat_res.text
    subcat = subcat_res.json()
    assert_eq(subcat["parent_id"], cat_id, "Subcategory created with parent_id set")

    brand_res = client.post("/brands", json={"name": f"Acme-{uuid.uuid4().hex[:6]}"}, headers=h1)
    assert brand_res.status_code == 201, brand_res.text
    brand = brand_res.json()
    assert_eq(brand["is_active"], True, "Brand created active by default")

    supplier_res = client.post("/suppliers", json={"name": f"Supplier-{uuid.uuid4().hex[:6]}"}, headers=h1)
    assert supplier_res.status_code == 201, supplier_res.text
    supplier_id = supplier_res.json()["id"]

    sku = f"SKU-{uuid.uuid4().hex[:8]}"
    create_payload = {
        "name": "Full Spec Product",
        "sku": sku,
        "barcode": "8901234567890",
        "short_name": "FSP",
        "product_type": "physical",
        "category_id": cat_id,
        "sub_category_id": subcat["id"],
        "brand_id": brand["id"],
        "manufacturer": "Acme Manufacturing Co",
        "model_number": "MDL-100",
        "description": "A product exercising every spec field",
        "status": "active",
        "cover_image": "https://example.com/cover.jpg",
        "images": ["https://example.com/a.jpg", "https://example.com/b.jpg"],
        "product_video": "https://example.com/video.mp4",
        "product_catalog_brochure": "https://example.com/brochure.pdf",
        "product_manual": "https://example.com/manual.pdf",
        "pricing": {
            "purchase_price": 100.0,
            "selling_price": 150.0,
            "mrp": 160.0,
            "wholesale_price": 140.0,
            "dealer_price": 130.0,
            "discount_percent": 5.0,
            "tax_inclusive": False,
            "currency": "INR",
        },
        "inventory_tracking": True,
        "uom": "piece",
        "opening_stock": 50,
        "minimum_stock_level": 10,
        "maximum_stock_level": 500,
        "reorder_level": 20,
        "reorder_quantity": 100,
        "bin_shelf_location": "A1-Rack3",
        "batch_tracking": True,
        "serial_number_tracking": False,
        "expiry_tracking": True,
        "hsn_code": "2202",
        "tax_rate": 18.0,
        "tax_category": "GST",
        "tax_inclusive": False,
        "preferred_supplier_id": supplier_id,
        "supplier_product_code": "SUP-CODE-1",
        "lead_time": "7 days",
        "minimum_order_quantity": 10,
        "purchase_unit": "carton",
        "sales_unit": "piece",
        "commission_eligible": True,
        "commission": 5.0,
        "default_discount": 2.0,
        "weight": 0.5,
        "weight_unit": "kg",
        "length": 10.0,
        "width": 5.0,
        "height": 20.0,
        "volume": 1.0,
        "color": "Red",
        "material": "Plastic",
        "physical_size": "M",
        "has_variants": False,
        "downloadable_product": False,
        "license_key_required": False,
        "warranty_period": "1",
        "warranty_period_unit": "years",
        "shelf_life": "6",
        "shelf_life_unit": "months",
        "country_of_origin": "India",
        "launch_date": "2025-01-01T00:00:00Z",
        "end_of_life_date": "2030-01-01T00:00:00Z",
        "product_tags": ["featured", "new"],
        "notes": "Internal note",
        "product_datasheet": "https://example.com/datasheet.pdf",
        "compliance_certificate": "https://example.com/cert.pdf",
        "warranty_document": "https://example.com/warranty.pdf",
    }
    cr = client.post("/products", json=create_payload, headers=h1)
    assert_eq(cr.status_code, 201, "POST /products with full spec payload succeeds", cr.text if cr.status_code != 201 else "")
    product = cr.json()
    pid = product["id"]

    # Basic info
    assert_eq(product["sku"], sku, "SKU stored and returned")
    assert_eq(product["barcode"], "8901234567890", "Barcode stored and returned")
    assert_eq(product["short_name"], "FSP", "Short name stored and returned")
    assert_eq(product["category_id"], cat_id, "category_id stored")
    assert_eq(product["sub_category_id"], subcat["id"], "sub_category_id stored")
    assert_eq(product["brand_id"], brand["id"], "brand_id stored")
    assert_eq(product["status"], "active", "status defaults/stored as active")
    assert_eq(product["is_active"], True, "is_active synced True from status=active")
    assert_true(product.get("subcategory") is not None and product["subcategory"]["id"] == subcat["id"], "subcategory NamedRef resolved")
    assert_true(product.get("brand_ref") is not None and product["brand_ref"]["id"] == brand["id"], "brand_ref NamedRef resolved")

    # Media
    assert_eq(product["cover_image"], "https://example.com/cover.jpg", "cover_image stored")
    assert_eq(len(product["images"]), 2, "additional images stored")
    assert_eq(product["product_video"], "https://example.com/video.mp4", "product_video stored")
    assert_eq(product["product_catalog_brochure"], "https://example.com/brochure.pdf", "catalog/brochure stored")
    assert_eq(product["product_manual"], "https://example.com/manual.pdf", "manual stored")

    # Pricing (existing behavior preserved)
    assert_eq(product["pricing"]["purchase_price"], 100.0, "purchase_price stored")
    assert_eq(product["pricing"]["selling_price"], 150.0, "selling_price stored")
    assert_eq(product["pricing"]["mrp"], 160.0, "mrp stored")
    assert_eq(product["price"], 150.0, "product.price synced to selling_price")

    # Inventory
    assert_eq(product["uom"], "piece", "uom stored")
    assert_eq(product["minimum_stock_level"], 10, "minimum_stock_level stored")
    assert_eq(product["maximum_stock_level"], 500, "maximum_stock_level stored")
    assert_eq(product["reorder_level"], 20, "reorder_level stored")
    assert_eq(product["reorder_quantity"], 100, "reorder_quantity stored")
    assert_eq(product["bin_shelf_location"], "A1-Rack3", "bin_shelf_location stored")
    assert_eq(product["batch_tracking"], True, "batch_tracking stored")
    assert_eq(product["expiry_tracking"], True, "expiry_tracking stored")

    # Tax
    assert_eq(product["hsn_code"], "2202", "hsn_code stored")
    assert_eq(product["tax_rate"], 18.0, "tax_rate stored")
    assert_eq(product["tax_category"], "GST", "tax_category stored")

    # Purchase
    assert_eq(product["preferred_supplier_id"], supplier_id, "preferred_supplier_id stored")
    assert_true(product.get("supplier") is not None and product["supplier"]["id"] == supplier_id, "supplier NamedRef resolved")
    assert_eq(product["supplier_product_code"], "SUP-CODE-1", "supplier_product_code stored")
    assert_eq(product["lead_time"], "7 days", "lead_time stored")
    assert_eq(product["minimum_order_quantity"], 10, "minimum_order_quantity stored")
    assert_eq(product["purchase_unit"], "carton", "purchase_unit stored")

    # Sales
    assert_eq(product["sales_unit"], "piece", "sales_unit stored")
    assert_eq(product["commission_eligible"], True, "commission_eligible stored")
    assert_eq(product["commission"], 5.0, "commission stored")
    assert_eq(product["default_discount"], 2.0, "default_discount stored")

    # Physical specs
    assert_eq(product["weight"], 0.5, "weight stored")
    assert_eq(product["weight_unit"], "kg", "weight_unit stored")
    assert_eq(product["length"], 10.0, "length stored")
    assert_eq(product["color"], "Red", "color stored")
    assert_eq(product["material"], "Plastic", "material stored")
    assert_eq(product["physical_size"], "M", "physical_size stored")

    # Digital product
    assert_eq(product["downloadable_product"], False, "downloadable_product stored")
    assert_eq(product["license_key_required"], False, "license_key_required stored")

    # Additional info
    assert_eq(product["warranty_period"], "1", "warranty_period stored")
    assert_eq(product["warranty_period_unit"], "years", "warranty_period_unit stored")
    assert_eq(product["shelf_life"], "6", "shelf_life stored")
    assert_eq(product["shelf_life_unit"], "months", "shelf_life_unit stored")
    assert_eq(product["country_of_origin"], "India", "country_of_origin stored")
    assert_eq(set(product["product_tags"]), {"featured", "new"}, "product_tags stored")
    assert_eq(product["notes"], "Internal note", "notes stored")

    # Documents
    assert_eq(product["product_datasheet"], "https://example.com/datasheet.pdf", "datasheet stored")
    assert_eq(product["compliance_certificate"], "https://example.com/cert.pdf", "compliance_certificate stored")
    assert_eq(product["warranty_document"], "https://example.com/warranty.pdf", "warranty_document stored")

    # ---------------- GET detail returns the same info ----------------
    gr = client.get(f"/products/{pid}", headers=h1)
    assert_eq(gr.status_code, 200, "GET /products/{id} succeeds")
    detail = gr.json()
    assert_eq(detail["sub_category_id"], subcat["id"], "GET detail exposes sub_category_id")
    assert_eq(detail["brand_id"], brand["id"], "GET detail exposes brand_id")
    assert_eq(detail["weight_unit"], "kg", "GET detail exposes weight_unit")

    # ---------------- GET list surfaces the new basic-info fields ----------------
    lr = client.get("/products", headers=h1)
    assert_eq(lr.status_code, 200, "GET /products succeeds")
    listed = next((p for p in lr.json() if p["id"] == pid), None)
    assert_true(listed is not None, "Product appears in list")
    if listed:
        assert_eq(listed["status"], "active", "List item exposes status")
        assert_eq(listed["sub_category_id"], subcat["id"], "List item exposes sub_category_id")
        assert_eq(listed["brand_id"], brand["id"], "List item exposes brand_id")
        assert_eq(listed["uom"], "piece", "List item exposes uom")

    # ---------------- PATCH: partial update preserves omitted fields ----------------
    pr = client.patch(f"/products/{pid}", json={"weight": 0.75}, headers=h1)
    assert_eq(pr.status_code, 200, "PATCH with single field succeeds")
    patched = pr.json()
    assert_eq(patched["weight"], 0.75, "PATCH updates the targeted field")
    assert_eq(patched["color"], "Red", "PATCH preserves omitted field (color)")
    assert_eq(patched["pricing"]["selling_price"], 150.0, "PATCH preserves pricing when not sent")
    assert_eq(patched["sub_category_id"], subcat["id"], "PATCH preserves sub_category_id when not sent")
    assert_eq(patched["brand_id"], brand["id"], "PATCH preserves brand_id when not sent")

    # Partial nested pricing update preserves the other pricing fields
    pr2 = client.patch(f"/products/{pid}", json={"pricing": {"mrp": 999.0}}, headers=h1)
    assert_eq(pr2.status_code, 200, "PATCH nested pricing update succeeds")
    p2 = pr2.json()
    assert_eq(p2["pricing"]["mrp"], 999.0, "Nested pricing field updated")
    assert_eq(p2["pricing"]["selling_price"], 150.0, "Nested pricing update preserves other pricing fields")
    assert_eq(p2["pricing"]["purchase_price"], 100.0, "Nested pricing update preserves purchase_price")

    # ---------------- Status <-> is_active sync ----------------
    sr = client.patch(f"/products/{pid}", json={"status": "discontinued"}, headers=h1)
    assert_eq(sr.status_code, 200, "PATCH status=discontinued succeeds")
    assert_eq(sr.json()["status"], "discontinued", "status updated to discontinued")
    assert_eq(sr.json()["is_active"], False, "is_active auto-synced False for discontinued")

    sr2 = client.patch(f"/products/{pid}", json={"is_active": True}, headers=h1)
    assert_eq(sr2.status_code, 200, "PATCH is_active=True succeeds")
    assert_eq(sr2.json()["is_active"], True, "is_active set True")
    assert_eq(sr2.json()["status"], "active", "status auto-synced to active from legacy is_active toggle")

    # ---------------- Pricing: zero / negative purchase & selling price still allowed ----------------
    zp = client.post("/products", json={
        "name": "Zero Price Product", "sku": f"ZP-{uuid.uuid4().hex[:6]}",
        "pricing": {"purchase_price": 0, "selling_price": 0},
    }, headers=h1)
    assert_eq(zp.status_code, 201, "Zero purchase/selling price accepted", zp.text if zp.status_code != 201 else "")

    npn = client.post("/products", json={
        "name": "Negative Price Product", "sku": f"NP-{uuid.uuid4().hex[:6]}",
        "pricing": {"purchase_price": -10, "selling_price": -5},
    }, headers=h1)
    assert_eq(npn.status_code, 201, "Negative purchase/selling price accepted", npn.text if npn.status_code != 201 else "")
    assert_eq(npn.json()["pricing"]["purchase_price"], -10, "Negative purchase_price stored as-is")
    assert_eq(npn.json()["pricing"]["selling_price"], -5, "Negative selling_price stored as-is")

    op = client.post("/products", json={"name": "Omitted Price Product", "sku": f"OP-{uuid.uuid4().hex[:6]}"}, headers=h1)
    assert_eq(op.status_code, 201, "Omitted pricing object accepted (defaults to 0)")

    # ---------------- Variants: existing behavior preserved ----------------
    vp = client.post("/products", json={
        "name": "Variant Product", "sku": f"VP-{uuid.uuid4().hex[:6]}",
        "variations": [{"name": "Red / L", "sku": f"VP-RL-{uuid.uuid4().hex[:4]}", "price": 100}],
    }, headers=h1)
    assert_eq(vp.status_code, 201, "Product with variant created")
    vprod = vp.json()
    assert_eq(vprod["has_variants"], True, "has_variants auto-set True when variants sent")
    assert_eq(len(vprod["variations"]), 1, "Variant present in response")
    assert_eq(vprod["variations"][0]["image_url"], None, "Variant without image_url has image_url=None")
    variant_id = vprod["variations"][0]["id"]

    # NOTE: the existing variant upsert path validates each entry as a full VariantIn
    # (name is required, even for an in-place update) — pre-existing behavior, unchanged.
    vpatch = client.patch(f"/products/{vprod['id']}", json={
        "variations": [{"id": variant_id, "name": "Red / L", "price": 120}]
    }, headers=h1)
    assert_eq(vpatch.status_code, 200, "Variant in-place update via PATCH succeeds")
    assert_eq(vpatch.json()["variations"][0]["price"], 120, "Variant price updated")
    assert_eq(vpatch.json()["variations"][0]["sku"], vprod["variations"][0]["sku"], "Variant SKU unaffected by unrelated field update")
    assert_eq(vpatch.json()["variations"][0]["image_url"], None, "Variant image_url remains None when omitted in update")

    # ---------------- Variant image_url support ----------------
    # TEST 1: Create variant with image_url
    img_url_1 = "https://example.com/variant-red-l.jpg"
    vp_img = client.post("/products", json={
        "name": "Variant Image Product", "sku": f"VIP-{uuid.uuid4().hex[:6]}",
        "variations": [
            {"name": "Red / L", "sku": f"VIP-RL-{uuid.uuid4().hex[:4]}", "price": 100, "inventory": 10, "image_url": img_url_1},
            {"name": "Blue / M", "sku": f"VIP-BM-{uuid.uuid4().hex[:4]}", "price": 90, "inventory": 5},
        ],
    }, headers=h1)
    assert_eq(vp_img.status_code, 201, "Product with variant image created")
    vip_data = vp_img.json()
    assert_eq(len(vip_data["variations"]), 2, "Both variants created")
    v1 = next(v for v in vip_data["variations"] if v["name"] == "Red / L")
    v2 = next(v for v in vip_data["variations"] if v["name"] == "Blue / M")
    assert_eq(v1["image_url"], img_url_1, "Variant 1 image_url returned on create")
    assert_eq(v2["image_url"], None, "Variant 2 image_url is null when omitted on create")

    # TEST 2: GET variant image_url
    vip_get = client.get(f"/products/{vip_data['id']}", headers=h1)
    assert_eq(vip_get.status_code, 200, "GET product with variant images succeeds")
    vip_get_data = vip_get.json()
    v1_get = next(v for v in vip_get_data["variations"] if v["id"] == v1["id"])
    v2_get = next(v for v in vip_get_data["variations"] if v["id"] == v2["id"])
    assert_eq(v1_get["image_url"], img_url_1, "GET returns variant 1 image_url accurately")
    assert_eq(v2_get["image_url"], None, "GET returns variant 2 image_url as null")

    # TEST 3: Update existing variant image_url
    img_url_updated = "https://example.com/variant-red-l-new.jpg"
    vip_patch1 = client.patch(f"/products/{vip_data['id']}", json={
        "variations": [
            {"id": v1["id"], "name": "Red / L", "price": 110, "image_url": img_url_updated},
        ],
    }, headers=h1)
    assert_eq(vip_patch1.status_code, 200, "PATCH existing variant image_url succeeds")
    v1_patched = next(v for v in vip_patch1.json()["variations"] if v["id"] == v1["id"])
    assert_eq(v1_patched["image_url"], img_url_updated, "Existing variant image_url updated")
    assert_eq(v1_patched["price"], 110, "Existing variant price updated")
    assert_eq(v1_patched["sku"], v1["sku"], "Existing variant SKU preserved")

    # TEST 4: Create new variant with image_url during update
    img_url_new_var = "https://example.com/variant-green-s.jpg"
    vip_patch2 = client.patch(f"/products/{vip_data['id']}", json={
        "variations": [
            {"name": "Green / S", "sku": f"VIP-GS-{uuid.uuid4().hex[:4]}", "price": 80, "inventory": 15, "image_url": img_url_new_var},
        ],
    }, headers=h1)
    assert_eq(vip_patch2.status_code, 200, "PATCH adding new variant with image_url succeeds")
    vip_patch2_data = vip_patch2.json()
    assert_eq(len(vip_patch2_data["variations"]), 3, "New variant added alongside existing untouched variants")
    v3_new = next(v for v in vip_patch2_data["variations"] if v["name"] == "Green / S")
    assert_eq(v3_new["image_url"], img_url_new_var, "New variant created during update has image_url persisted")
    assert_eq(v3_new["inventory"], 15, "New variant inventory persisted")

    # TEST 5: Null image_url explicitly and GET check
    vip_get_final = client.get(f"/products/{vip_data['id']}", headers=h1)
    assert_eq(vip_get_final.status_code, 200, "GET product detail after variant updates succeeds")
    for var in vip_get_final.json()["variations"]:
        if var["name"] == "Blue / M":
            assert_eq(var["image_url"], None, "image_url is null for variant created without it")

    # ---------------- Organization isolation ----------------
    iso_get = client.get(f"/products/{pid}", headers=h2)
    assert_eq(iso_get.status_code, 404, "Org 2 cannot GET Org 1's product")

    iso_patch = client.patch(f"/products/{pid}", json={"name": "Hacked"}, headers=h2)
    assert_eq(iso_patch.status_code, 404, "Org 2 cannot PATCH Org 1's product")

    iso_delete = client.delete(f"/products/{pid}", headers=h2)
    assert_eq(iso_delete.status_code, 404, "Org 2 cannot DELETE Org 1's product")

    iso_brand = client.get(f"/brands/{brand['id']}", headers=h2)
    assert_eq(iso_brand.status_code, 404, "Org 2 cannot access Org 1's brand")

    iso_subcat = client.get(f"/categories/{subcat['id']}", headers=h2)
    assert_eq(iso_subcat.status_code, 404, "Org 2 cannot access Org 1's subcategory")

    iso_list = client.get("/products", headers=h2)
    assert_true(all(p["id"] != pid for p in iso_list.json()), "Org 1's product absent from Org 2's product list")

    cross_brand = client.post("/products", json={
        "name": "Cross-org brand test", "sku": f"XB-{uuid.uuid4().hex[:6]}", "brand_id": brand["id"],
    }, headers=h2)
    assert_eq(cross_brand.status_code, 400, "Org 2 cannot create a product referencing Org 1's brand_id")

    # ---------------- Regression: Product -> Quotation -> Order -> Invoice ----------------
    wh_res = client.get("/warehouses", headers=h1)
    warehouses = wh_res.json()
    if warehouses:
        wh_id = warehouses[0]["id"]
    else:
        wh_create = client.post("/warehouses", json={"name": "Main WH", "code": f"WH-{uuid.uuid4().hex[:4]}"}, headers=h1)
        wh_id = wh_create.json()["id"]

    client.post(f"/warehouses/{wh_id}/stock/adjust", json={"product_id": pid, "quantity": 100}, headers=h1)

    cust_res = client.post("/customers", json={
        "name": "Regression Customer", "phone": "9998887777", "billing_address": "Test Address",
    }, headers=h1)
    assert_eq(cust_res.status_code, 201, "Customer created for regression chain")
    cust_id = cust_res.json()["id"]

    quote_res = client.post("/quotations", json={
        "customer_id": cust_id,
        "items": [{"product_id": pid, "quantity": 2, "unit_price": 150.0}],
    }, headers=h1)
    assert_eq(quote_res.status_code, 201, "Quotation created referencing the extended product", quote_res.text if quote_res.status_code != 201 else "")

    order_res = client.post("/orders", json={
        "customer_id": cust_id,
        "warehouse_id": wh_id,
        "items": [{"product_id": pid, "quantity": 2, "unit_price": 150.0, "tax_rate": 18.0, "discount": 0.0}],
    }, headers=h1)
    assert_eq(order_res.status_code, 201, "Sales order created referencing the extended product", order_res.text if order_res.status_code != 201 else "")
    order = order_res.json()
    order_item_id = order["items"][0]["id"]

    deliver_res = client.post("/deliveries", json={
        "order_id": order["id"],
        "warehouse_id": wh_id,
        "items": [{"order_item_id": order_item_id, "planned_quantity": 2}],
    }, headers=h1)
    assert_eq(deliver_res.status_code, 201, "Delivery created for the order", deliver_res.text if deliver_res.status_code != 201 else "")

    if deliver_res.status_code == 201:
        # Mirrors the existing regression suite's approach: mark the delivery
        # delivered directly (the pick/ready/accept/load/confirm HTTP workflow is
        # covered by its own dedicated tests, not the concern of this product audit).
        db_session = SessionLocal()
        deliv_db = db_session.get(Delivery, deliver_res.json()["id"])
        for item in deliv_db.items:
            item.delivered_quantity = 2.0
        deliv_db.status = "delivered"
        order_item_db = db_session.get(SalesOrderItem, order_item_id)
        order_item_db.delivered_quantity = 2.0
        order_db = db_session.get(SalesOrder, order["id"])
        order_db.fulfilment_status = "delivered"
        db_session.commit()
        db_session.close()

        inv_res = client.post(f"/orders/{order['id']}/invoice", json={}, headers=h1)
        if inv_res.status_code == 400 and "delivery_id" in inv_res.text:
            # This organization bills per-delivery rather than after_full_order.
            inv_res = client.post(
                f"/orders/{order['id']}/invoice", json={"delivery_id": deliver_res.json()["id"]}, headers=h1
            )
        assert_eq(inv_res.status_code, 201, "Invoice generated from order referencing the extended product", inv_res.text if inv_res.status_code != 201 else "")
        if inv_res.status_code == 201:
            assert_eq(inv_res.json()["total"], 354.0, "Invoice total correct (2 * 150 * 1.18)")

    # ---------------- Delete: no orphaned pricing/variant rows ----------------
    del_res = client.delete(f"/products/{vprod['id']}", headers=h1)
    assert_eq(del_res.status_code, 204, "Product with variants deletes cleanly")
    refetch = client.get(f"/products/{vprod['id']}", headers=h1)
    assert_eq(refetch.status_code, 404, "Deleted product no longer retrievable")

    print("\n=======================================================")
    print(f"RESULTS: {_passed} passed, {_failed} failed")
    print("=======================================================")
    return _failed


if __name__ == "__main__":
    failed = run_tests()
    sys.exit(1 if failed else 0)
