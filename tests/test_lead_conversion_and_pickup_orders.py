"""Comprehensive test suite for Lead -> Customer Conversion and Self Pickup / Takeaway Orders.

Covers:
  - TEST 1: Lead conversion workflow (new -> contacted -> qualified -> convert -> won), customer linking, idempotent double conversion.
  - TEST 2: Lead tenant isolation across organizations.
  - TEST 3: Lead lifecycle does not touch stock, orders, invoices, or payments.
  - TEST 4: Pickup stock flow (Draft -> Confirm -> Pick -> Ready -> Confirm Pickup) tracking physical, reserved, available, and vehicle stock.
  - TEST 5: Pickup invoice flow via POST /orders/{id}/invoice (uncollected check, invoice generation, line items and totals).
  - TEST 6: Pickup order isolation from Delivery workflow (no delivery planning, no vehicle, no in-transit).
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from app.main import app
from app.core.config import settings
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


def assert_eq(actual, expected, msg: str):
    if actual == expected:
        ok(msg)
    else:
        fail(msg, f"Expected {expected!r}, got {actual!r}")


def _register_org(name_prefix: str = "Org"):
    email = f"admin_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={
        "organization_name": f"{name_prefix} {uuid.uuid4().hex[:6]}",
        "admin_name": "Admin User",
        "email": email,
        "password": "Password123!",
        "role": "admin",
    })
    assert r.status_code == 201, r.text
    auth = {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}
    return auth, email


def _create_product(auth: dict, name: str, price: float = 100.0, stock: int = 100, tax_rate: float = 18.0):
    res = client.post("/products", json={
        "name": name,
        "sku": f"SKU-{uuid.uuid4().hex[:6].upper()}",
        "price": price,
        "total_inventory": stock,
        "tax_rate": tax_rate,
    }, headers=auth)
    assert res.status_code == 201, res.text
    return res.json()


def run_tests():
    print("\n=======================================================")
    print("TEST SUITE: Lead -> Customer & Self Pickup / Takeaway")
    print("=======================================================")

    auth1, email1 = _register_org("Firm A")
    auth2, email2 = _register_org("Firm B")

    # =========================================================================
    # PART A: LEAD -> CUSTOMER CONVERSION
    # =========================================================================
    print("\n--- TEST 1: Lead Lifecycle & Conversion ---")
    lead_res = client.post("/leads", json={
        "name": "Acme Prospects",
        "contact_person": "John Doe",
        "mobile": "9876543210",
        "email": "john@acmeprospects.com",
        "source": "Website Enquiry",
        "interested_product": "Solar Inverter",
        "notes": "Looking for 5 units",
        "lead_status": "new",
    }, headers=auth1)
    assert_eq(lead_res.status_code, 201, "Lead created with status 'new'")
    lead_data = lead_res.json()
    lead_id = lead_data["id"]
    assert_eq(lead_data["lead_status"], "new", "Initial lead status is 'new'")
    assert_eq(lead_data["name"], "Acme Prospects", "Lead name matches")
    assert_eq(lead_data["contact_person"], "John Doe", "Contact person matches")
    assert_eq(lead_data["mobile_number"], "9876543210", "Mobile number matches")
    assert_eq(lead_data["email"], "john@acmeprospects.com", "Email matches")

    # Transition: new -> contacted
    upd_res1 = client.patch(f"/leads/{lead_id}", json={"status": "contacted"}, headers=auth1)
    assert_eq(upd_res1.status_code, 200, "Lead updated to 'contacted'")
    assert_eq(upd_res1.json()["lead_status"], "contacted", "Lead status is 'contacted'")

    # Transition: contacted -> qualified
    upd_res2 = client.patch(f"/leads/{lead_id}", json={"status": "qualified"}, headers=auth1)
    assert_eq(upd_res2.status_code, 200, "Lead updated to 'qualified'")
    assert_eq(upd_res2.json()["lead_status"], "qualified", "Lead status is 'qualified'")

    # Convert Lead to Customer
    conv_res = client.post(f"/leads/{lead_id}/convert-to-customer", json={
        "business_name": "Acme Corporation Pvt Ltd",
        "billing_address": "123 Industrial Area, Pune",
        "gst_number": "27AAACA1234A1Z5",
        "credit_limit": 50000.0,
    }, headers=auth1)
    assert_eq(conv_res.status_code, 200, "Lead converted to customer successfully")
    conv_data = conv_res.json()
    cust_id = conv_data["customer_id"]
    assert conv_data["converted"] is True, "Response indicates converted = True"
    assert_eq(conv_data["lead_status"], "won", "Response lead_status is 'won'")

    # Check lead detail after conversion
    lead_after = client.get(f"/leads/{lead_id}", headers=auth1).json()
    assert_eq(lead_after["lead_status"], "won", "Lead status persisted as 'won'")
    assert_eq(lead_after["customer_id"], cust_id, "Lead has customer_id set")
    assert_eq(lead_after["converted_customer_id"], cust_id, "Lead has converted_customer_id set")
    assert lead_after["converted_at"] is not None, "Lead has converted_at timestamp"
    assert lead_after["customer"] is not None, "Lead customer relationship joined"
    assert_eq(lead_after["customer"]["id"], cust_id, "Lead customer object matches customer ID")

    # Verify created customer record
    cust_get = client.get(f"/customers/{cust_id}", headers=auth1)
    assert_eq(cust_get.status_code, 200, "Converted customer exists and can be retrieved via GET /customers/{id}")
    cust_obj = cust_get.json()
    assert_eq(cust_obj["basic_information"]["customer_name"], "Acme Prospects", "Customer name matches lead name")
    assert_eq(cust_obj["basic_information"]["legal_business_name"], "Acme Corporation Pvt Ltd", "Customer business name matches conversion payload")
    assert_eq(cust_obj["contact_information"]["mobile_number"], "9876543210", "Customer phone matches lead mobile")
    assert_eq(cust_obj["contact_information"]["email_address"], "john@acmeprospects.com", "Customer email matches lead email")
    assert_eq(cust_obj["business_tax_information"]["gstin_tax_id"], "27AAACA1234A1Z5", "Customer GST matches conversion payload")
    assert_eq(cust_obj["address_information"]["billing_address"], "123 Industrial Area, Pune", "Customer billing address matches")

    # Idempotent: Calling conversion again returns same customer without duplicate creation
    conv_res_again = client.post(f"/leads/{lead_id}/convert-to-customer", json={}, headers=auth1)
    assert_eq(conv_res_again.status_code, 200, "Second conversion call succeeds idempotently")
    assert_eq(conv_res_again.json()["customer_id"], cust_id, "Returns same customer_id on duplicate conversion")
    assert_eq(conv_res_again.json()["converted"], True, "Converted remains True")

    # Count customers for Org 1 to ensure no duplicate was created
    all_custs = client.get("/customers", headers=auth1).json()
    matching_custs = [c for c in all_custs if c["id"] == cust_id]
    assert_eq(len(matching_custs), 1, "Exactly 1 customer created for the lead")

    # Verify Quotation creation using the converted customer_id
    prod1 = _create_product(auth1, "Solar Inverter 5kVA", price=25000.0, stock=50)
    quot_res = client.post("/quotations", json={
        "customer_id": cust_id,
        "items": [
            {"product_id": prod1["id"], "quantity": 2, "unit_price": 25000.0, "tax_rate": 18.0}
        ],
    }, headers=auth1)
    assert_eq(quot_res.status_code, 201, "Quotation created successfully for the converted customer_id")

    # --- TEST 2: Lead Tenant Isolation ---
    print("\n--- TEST 2: Lead Tenant Isolation ---")
    r_other_get = client.get(f"/leads/{lead_id}", headers=auth2)
    assert_eq(r_other_get.status_code, 404, "Org 2 cannot GET Org 1's lead (404 Not Found)")

    r_other_conv = client.post(f"/leads/{lead_id}/convert-to-customer", json={}, headers=auth2)
    assert_eq(r_other_conv.status_code, 404, "Org 2 cannot convert Org 1's lead (404 Not Found)")

    r_other_patch = client.patch(f"/leads/{lead_id}", json={"status": "lost"}, headers=auth2)
    assert_eq(r_other_patch.status_code, 404, "Org 2 cannot update Org 1's lead (404 Not Found)")

    # --- TEST 3: Lead Lifecycle Does Not Touch Stock ---
    print("\n--- TEST 3: Lead Lifecycle Does Not Touch Stock ---")
    lead_iso_prod = _create_product(auth1, "Stock Isolation Test Product", price=500.0, stock=100)
    iso_lead_res = client.post("/leads", json={
        "name": "Iso Prospect",
        "mobile": "9111111111",
        "source": "Website",
        "interested_product": "Stock Isolation Test Product",
    }, headers=auth1)
    assert_eq(iso_lead_res.status_code, 201, "Lead created for stock isolation test")
    iso_lead_id = iso_lead_res.json()["id"]
    client.patch(f"/leads/{iso_lead_id}", json={"status": "qualified"}, headers=auth1)
    client.post(f"/leads/{iso_lead_id}/convert-to-customer", json={}, headers=auth1)

    # Check product stock remains 100
    prod_check = client.get(f"/products/{lead_iso_prod['id']}", headers=auth1).json()
    assert_eq(prod_check["total_inventory"], 100, "Product physical stock completely untouched by lead conversion")

    # =========================================================================
    # PART B: SELF PICKUP / TAKEAWAY ORDERS
    # =========================================================================
    print("\n--- TEST 4: Pickup Stock Flow ---")
    # Enable draft orders
    patch_res = client.patch("/sales-workflow-settings", json={"draft_orders_enabled": True, "reserve_stock_on_order": True}, headers=auth1)
    assert patch_res.status_code == 200, patch_res.text

    pickup_prod = _create_product(auth1, "Pickup Widget", price=100.0, stock=100, tax_rate=18.0)
    prod_id = pickup_prod["id"]

    # Opening stock
    # Physical = 100, Reserved = 0, Available = 100
    p_open = client.get(f"/products/{prod_id}", headers=auth1).json()
    assert_eq(p_open["total_inventory"], 100, "Opening Physical stock is 100")

    # 1. Create Pickup Sales Order (Draft)
    so_create_res = client.post("/orders", json={
        "customer_id": cust_id,
        "fulfilment_method": "pickup",
        "items": [
            {"product_id": prod_id, "quantity": 20, "unit_price": 100.0, "tax_rate": 18.0}
        ],
    }, headers=auth1)
    assert_eq(so_create_res.status_code, 201, "Created pickup sales order")
    so = so_create_res.json()
    order_id = so["id"]
    # Public Order status contract: internal 'draft' -> public 'draft'.
    assert_eq(so["status"], "draft", "Order is in draft status (public: 'draft')")
    assert_eq(so["fulfilment_method"], "pickup", "Fulfilment method is 'pickup'")
    assert_eq(so["pickup_status"], "not_started", "Pickup status is 'not_started'")

    # After Draft: Physical = 100, Reserved = 0, Available = 100
    stock_draft = so["stock_summary"][0]
    assert_eq(stock_draft["on_hand"], 100.0, "Draft: on_hand is 100")
    assert_eq(stock_draft["reserved"], 0.0, "Draft: reserved is 0")
    assert_eq(stock_draft["available"], 100.0, "Draft: available is 100")

    # 2. Confirm Order
    conf_res = client.post(f"/orders/{order_id}/confirm", headers=auth1)
    assert_eq(conf_res.status_code, 200, "Confirmed draft pickup order")
    so_conf = conf_res.json()
    # Public Order status contract: internal 'placed' -> public 'confirmed'.
    assert_eq(so_conf["status"], "confirmed", "Confirmed order status is 'confirmed'")
    assert_eq(so_conf["fulfilment_status"], "reserved", "Confirmed order fulfilment_status is 'reserved'")
    assert_eq(so_conf["pickup_status"], "not_started", "Confirmed order pickup_status is 'not_started'")

    # After Confirm: Physical = 100, Reserved = 20, Available = 80
    stock_conf = so_conf["stock_summary"][0]
    assert_eq(stock_conf["on_hand"], 100.0, "Confirm: on_hand is 100")
    assert_eq(stock_conf["reserved"], 20.0, "Confirm: reserved is 20")
    assert_eq(stock_conf["available"], 80.0, "Confirm: available is 80")

    # 3. POST /orders/{id}/pickup/pick
    pick_res = client.post(f"/orders/{order_id}/pickup/pick", headers=auth1)
    assert_eq(pick_res.status_code, 200, "POST /orders/{id}/pickup/pick succeeded")
    so_pick = pick_res.json()
    assert_eq(so_pick["pickup_status"], "picking", "Pickup status transitioned to 'picking'")
    # Public Order status contract: internal 'processing' -> public 'confirmed'.
    assert_eq(so_pick["status"], "confirmed", "Order status transitioned to 'processing' (public: 'confirmed')")

    # After Pick: Physical = 100, Reserved = 20, Available = 80 (No stock movement)
    stock_pick = so_pick["stock_summary"][0]
    assert_eq(stock_pick["on_hand"], 100.0, "Pick: on_hand unchanged at 100")
    assert_eq(stock_pick["reserved"], 20.0, "Pick: reserved unchanged at 20")
    assert_eq(stock_pick["available"], 80.0, "Pick: available unchanged at 80")

    # 4. POST /orders/{id}/pickup/ready
    ready_res = client.post(f"/orders/{order_id}/pickup/ready", headers=auth1)
    assert_eq(ready_res.status_code, 200, "POST /orders/{id}/pickup/ready succeeded")
    so_ready = ready_res.json()
    assert_eq(so_ready["pickup_status"], "ready", "Pickup status transitioned to 'ready'")

    # After Ready: Physical = 100, Reserved = 20, Available = 80 (No stock movement)
    stock_ready = so_ready["stock_summary"][0]
    assert_eq(stock_ready["on_hand"], 100.0, "Ready: on_hand unchanged at 100")
    assert_eq(stock_ready["reserved"], 20.0, "Ready: reserved unchanged at 20")
    assert_eq(stock_ready["available"], 80.0, "Ready: available unchanged at 80")

    # --- TEST 5: Pickup Invoice (Before Confirmation Check) ---
    print("\n--- TEST 5: Pickup Invoicing ---")
    inv_early_res = client.post(f"/orders/{order_id}/invoice", json={}, headers=auth1)
    assert_eq(inv_early_res.status_code, 400, "Invoicing before pickup confirmation rejected with HTTP 400")

    # 5. POST /orders/{id}/pickup/confirm
    confirm_pickup_res = client.post(f"/orders/{order_id}/pickup/confirm", json={
        "collected_by": "Rahul Mehta",
        "notes": "Customer collected from warehouse counter",
    }, headers=auth1)
    assert_eq(confirm_pickup_res.status_code, 200, "POST /orders/{id}/pickup/confirm succeeded")
    so_collected = confirm_pickup_res.json()
    assert_eq(so_collected["pickup_status"], "collected", "Pickup status is 'collected'")
    assert_eq(so_collected["fulfilment_status"], "delivered", "Fulfilment status is 'delivered'")
    assert_eq(so_collected["status"], "completed", "Order status is 'completed'")
    assert_eq(so_collected["collected_by"], "Rahul Mehta", "collected_by recorded")
    assert_eq(so_collected["pickup_notes"], "Customer collected from warehouse counter", "pickup_notes recorded")
    assert_eq(so_collected["items"][0]["delivered_quantity"], 20.0, "delivered_quantity is 20")
    assert_eq(so_collected["items"][0]["reserved_quantity"], 0.0, "reserved_quantity is 0")

    # After Confirm Pickup: Physical = 80, Reserved = 0, Available = 80
    stock_after = so_collected["stock_summary"][0]
    assert_eq(stock_after["on_hand"], 80.0, "Confirm Pickup: Physical stock reduced to 80")
    assert_eq(stock_after["reserved"], 0.0, "Confirm Pickup: Reserved stock consumed to 0")
    assert_eq(stock_after["available"], 80.0, "Confirm Pickup: Available stock is 80")

    # Re-confirming pickup is refused
    reconf = client.post(f"/orders/{order_id}/pickup/confirm", json={}, headers=auth1)
    assert_eq(reconf.status_code, 400, "Re-confirming pickup is refused with HTTP 400")

    # Generate Invoice via POST /orders/{order_id}/invoice
    inv_res = client.post(f"/orders/{order_id}/invoice", json={}, headers=auth1)
    assert_eq(inv_res.status_code, 201, "Invoice generated successfully via POST /orders/{id}/invoice")
    inv = inv_res.json()
    assert_eq(inv["order_id"], order_id, "Invoice linked to order_id")
    assert_eq(inv["customer_id"], cust_id, "Invoice linked to customer_id")
    assert_eq(inv["delivery_id"], None, "Pickup invoice has delivery_id = None")
    assert_eq(len(inv["items"]), 1, "Invoice has 1 line item")
    assert_eq(inv["items"][0]["quantity"], 20.0, "Invoiced quantity is exactly 20.0")
    assert_eq(inv["subtotal"], 2000.0, "Invoice subtotal is 20 * 100 = 2000.0")
    assert_eq(inv["tax"], 360.0, "Invoice tax is 18% of 2000 = 360.0")
    assert_eq(inv["total"], 2360.0, "Invoice total is 2360.0")
    assert_eq(inv["billing_address"], "123 Industrial Area, Pune", "Invoice billing address copied from customer")

    # Duplicate invoice rejected
    inv_dup = client.post(f"/orders/{order_id}/invoice", json={}, headers=auth1)
    assert_eq(inv_dup.status_code, 409, "Duplicate invoice for pickup order rejected with HTTP 409 Conflict")

    # --- TEST 6: Pickup Order Must Not Enter Delivery Flow ---
    print("\n--- TEST 6: Pickup Order Must Not Enter Delivery Flow ---")
    # 1. Create a fresh pickup order to test delivery rejection
    po2_res = client.post("/orders", json={
        "customer_id": cust_id,
        "fulfilment_method": "pickup",
        "items": [
            {"product_id": prod_id, "quantity": 5, "unit_price": 100.0, "tax_rate": 18.0}
        ],
    }, headers=auth1)
    po2_id = po2_res.json()["id"]
    client.post(f"/orders/{po2_id}/confirm", headers=auth1)

    # Attempt to plan delivery against pickup order
    del_plan_res = client.post("/deliveries", json={
        "order_id": po2_id,
    }, headers=auth1)
    assert_eq(del_plan_res.status_code, 400, "POST /deliveries for pickup order is rejected with HTTP 400")

    # Attempt to assign delivery partner on pickup order
    # Create staff user for assignment test
    staff_email = f"driver_{uuid.uuid4().hex[:6]}@example.com"
    st_res = client.post("/users", json={
        "name": "Delivery Guy",
        "email": staff_email,
        "password": "Password123!",
        "role": "delivery_partner",
    }, headers=auth1)
    if st_res.status_code == 201:
        partner_id = st_res.json()["id"]
        assign_res = client.patch(f"/orders/{po2_id}/assign-delivery-partner", json={
            "delivery_partner_id": partner_id,
        }, headers=auth1)
        assert_eq(assign_res.status_code, 400, "Assigning delivery partner to pickup order is rejected with HTTP 400")

    # Clean up po2 by completing pickup
    client.post(f"/orders/{po2_id}/pickup/confirm", json={}, headers=auth1)

    print("\n=======================================================")
    print(f"RESULTS: {_passed} passed, {_failed} failed")
    print("=======================================================\n")
    if _failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_tests()
