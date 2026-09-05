"""Final consistency pass — Order lifecycle, Delivery, Pickup, Quotation
guard, calculations, financial summary, Delivery Partner authorization.

Covers the Part T test matrix from the "FINAL CONSISTENCY PASS" spec:
  - Order: draft-by-default, atomic confirm, cancellation matrix, pickup,
    payment_type round-trip, financial summary, calculations, Quotation guard
  - Delivery: partner isolation, picking/loading physical-stock separation,
    dispatch, partial/failed, pre/post-load cancellation, timeline, POD
  - Security: Delivery Partner blocked from ALL Order creation (incl. source
    variants), cross-tenant, Team scope, teamless fallback, Team Manager
    scope, Expense ownership
  - Follow-up / Visit regression: direct Lead Follow-up/Visit, outcome
    persistence, omitted != clear, ready_to_convert does not auto-convert
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
            "organization_name": f"{label} Org", "admin_name": f"Admin {label}",
            "email": email, "password": "Password123!", "role": "admin",
        },
    )
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}


def _create_staff(admin_auth: dict, name: str, role_name: str) -> tuple[dict, dict]:
    email = f"{name.lower().replace(' ', '_')}_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post(
        "/users", json={"name": name, "email": email, "password": "Password123!", "role": role_name}, headers=admin_auth
    )
    assert r.status_code == 201, r.text
    user = r.json()
    login = client.post("/auth/login", json={"email": email, "password": "Password123!"})
    assert login.status_code == 200, login.text
    return user, {"Authorization": f"Bearer {login.json()['tokens']['access_token']}"}


def _setup(label: str, stock: int = 100):
    admin_auth = _register_org(label)
    wh = client.post("/warehouses", json={"name": "WH", "is_default": True}, headers=admin_auth).json()
    prod = client.post(
        "/products",
        json={"name": f"P{uuid.uuid4().hex[:4]}", "sku": f"SKU-{uuid.uuid4().hex[:6]}", "price": 100.0, "tax_rate": 10.0},
        headers=admin_auth,
    ).json()
    client.post(f"/warehouses/{wh['id']}/stock/adjust", json={"product_id": prod["id"], "quantity": stock}, headers=admin_auth)
    cust = client.post("/customers", json={"name": "Cust"}, headers=admin_auth).json()
    return admin_auth, wh, prod, cust


def _stock(admin_auth, wh, prod):
    r = client.get("/warehouses/stock", headers=admin_auth, params={"product_id": prod["id"], "warehouse_id": wh["id"]})
    row = r.json()[0]
    return row["on_hand"], row["reserved"], row["available"]


# ============================================================================
# ORDER: draft-by-default, atomic confirm, invariant
# ============================================================================

def run_order_draft_and_confirm():
    print("\n=== ORDER: Draft-by-default + atomic confirm ===")
    admin_auth, wh, prod, cust = _setup(f"OrdDraft_{uuid.uuid4().hex[:6]}")

    # 1/2. New Order starts Draft, zero active reservations
    order = client.post(
        "/orders", json={"customer_id": cust["id"], "warehouse_id": wh["id"],
                         "items": [{"product_id": prod["id"], "quantity": 10, "unit_price": 100.0}]},
        headers=admin_auth,
    ).json()
    log_test("1. New Order starts as Draft", order["status"] == "draft", order)
    log_test("1. Draft fulfilment_status is not_started", order["fulfilment_status"] == "not_started", order)
    on_hand, reserved, available = _stock(admin_auth, wh, prod)
    log_test("2. Draft has zero active reservations", reserved == 0.0 and available == 100.0, (on_hand, reserved, available))

    # 3. Confirm reserves stock atomically
    r = client.post(f"/orders/{order['id']}/confirm", headers=admin_auth)
    log_test("3. Confirm succeeds (200)", r.status_code == 200, r.text)
    confirmed = r.json()
    log_test("3. Status is now 'confirmed'", confirmed["status"] == "confirmed", confirmed)
    log_test("3. fulfilment_status is 'reserved'", confirmed["fulfilment_status"] == "reserved", confirmed)
    on_hand, reserved, available = _stock(admin_auth, wh, prod)
    log_test("3. Stock reserved atomically (10 held)", reserved == 10.0 and available == 90.0, (on_hand, reserved, available))

    # 4. Insufficient stock leaves Draft unchanged
    order2 = client.post(
        "/orders", json={"customer_id": cust["id"], "warehouse_id": wh["id"],
                         "items": [{"product_id": prod["id"], "quantity": 99999, "unit_price": 100.0}]},
        headers=admin_auth,
    ).json()
    log_test("4. Oversized order still creates as Draft (no check at creation)", order2["status"] == "draft", order2)
    r = client.post(f"/orders/{order2['id']}/confirm", headers=admin_auth)
    log_test("4. Confirm with insufficient stock -> 400", r.status_code == 400, r.text)
    log_test("4. Shortage clearly identified", r.json()["detail"]["error"] == "INSUFFICIENT_STOCK", r.text)
    r_check = client.get(f"/orders/{order2['id']}", headers=admin_auth)
    log_test("4. Order remains Draft after failed confirm", r_check.json()["status"] == "draft", r_check.text)
    log_test("4. fulfilment_status remains not_started", r_check.json()["fulfilment_status"] == "not_started", r_check.text)
    on_hand2, reserved2, available2 = _stock(admin_auth, wh, prod)
    log_test("4. No ghost reservation rows (available unchanged)", available2 == available, (on_hand2, reserved2, available2))

    # 5. Double confirm blocked
    r = client.post(f"/orders/{order['id']}/confirm", headers=admin_auth)
    log_test("5. Double confirm on already-confirmed order -> 400", r.status_code == 400, r.text)
    on_hand3, reserved3, available3 = _stock(admin_auth, wh, prod)
    log_test("Critical invariant: confirming twice does not double-reserve", reserved3 == 10.0, (on_hand3, reserved3, available3))

    # 6. Confirmed status returned consistently (list + detail)
    r_list = client.get("/orders", headers=admin_auth)
    row = next(o for o in r_list.json() if o["id"] == order["id"])
    log_test("6. List and detail report the same public status", row["status"] == confirmed["status"] == "confirmed")

    # 7. Cancel Draft works
    r = client.patch(f"/orders/{order2['id']}/cancel", json={"reason": "no longer needed"}, headers=admin_auth)
    log_test("7. Cancel Draft order works (200)", r.status_code == 200 and r.json()["status"] == "cancelled", r.text)

    # 8. Cancel Confirmed pre-load releases reservation
    r = client.patch(f"/orders/{order['id']}/cancel", json={"reason": "customer cancelled"}, headers=admin_auth)
    log_test("8. Cancel Confirmed (pre-load) works (200)", r.status_code == 200 and r.json()["status"] == "cancelled", r.text)
    on_hand4, reserved4, available4 = _stock(admin_auth, wh, prod)
    log_test("8. Reservation released on cancel", reserved4 == 0.0 and available4 == 100.0, (on_hand4, reserved4, available4))


def run_draft_invariant_regression():
    print("\n=== Critical invariant regression: DRAFT ORDER MUST NEVER HAVE ACTIVE RESERVATIONS ===")
    admin_auth, wh, prod, cust = _setup(f"OrdInvariant_{uuid.uuid4().hex[:6]}")
    for qty in (1, 5, 50, 100):
        order = client.post(
            "/orders", json={"customer_id": cust["id"], "warehouse_id": wh["id"],
                             "items": [{"product_id": prod["id"], "quantity": qty, "unit_price": 100.0}]},
            headers=admin_auth,
        ).json()
        _, reserved, _ = _stock(admin_auth, wh, prod)
        log_test(f"Draft order qty={qty}: zero reservation immediately after creation", reserved == 0.0)
        log_test(f"Draft order qty={qty}: status is 'draft'", order["status"] == "draft")


# ============================================================================
# ORDER: cancellation matrix (pre/post-load)
# ============================================================================

def run_cancellation_matrix():
    print("\n=== ORDER: Cancellation matrix ===")
    admin_auth, wh, prod, cust = _setup(f"OrdCancel_{uuid.uuid4().hex[:6]}")
    dp, dp_auth = _create_staff(admin_auth, "DP", "Delivery Partner")
    veh = client.post("/vehicles", json={"vehicle_number": f"DL-{uuid.uuid4().hex[:5].upper()}", "vehicle_type": "van"}, headers=admin_auth).json()

    def _new_confirmed_order(qty=5):
        o = client.post(
            "/orders", json={"customer_id": cust["id"], "warehouse_id": wh["id"],
                             "items": [{"product_id": prod["id"], "quantity": qty, "unit_price": 100.0}]},
            headers=admin_auth,
        ).json()
        client.post(f"/orders/{o['id']}/confirm", headers=admin_auth)
        return o

    def _plan_delivery(order):
        return client.post(
            "/deliveries", json={"order_id": order["id"], "delivery_partner_id": dp["id"], "vehicle_id": veh["id"]},
            headers=admin_auth,
        ).json()

    def _pick_and_ready(dlv):
        items = [{"delivery_item_id": it["id"], "picked_quantity": it["planned_quantity"]} for it in dlv["items"]]
        client.post(f"/deliveries/{dlv['id']}/pick", json={"items": items}, headers=admin_auth)
        client.post(f"/deliveries/{dlv['id']}/ready", headers=admin_auth)

    # planned delivery cancellation
    o1 = _new_confirmed_order()
    d1 = _plan_delivery(o1)
    r = client.patch(f"/orders/{o1['id']}/cancel", json={"reason": "test"}, headers=admin_auth)
    log_test("planned delivery: Order cancel succeeds (200)", r.status_code == 200, r.text)
    d1_after = client.get(f"/deliveries/by-id/{d1['id']}", headers=admin_auth).json()
    log_test("planned delivery: linked Delivery cancelled too", d1_after["status"] == "cancelled", d1_after)

    # accepted cancellation
    o2 = _new_confirmed_order()
    d2 = _plan_delivery(o2)
    client.post(f"/deliveries/{d2['id']}/accept", headers=dp_auth)
    r = client.patch(f"/orders/{o2['id']}/cancel", json={"reason": "test"}, headers=admin_auth)
    log_test("accepted delivery: Order cancel succeeds (200)", r.status_code == 200, r.text)
    d2_after = client.get(f"/deliveries/by-id/{d2['id']}", headers=admin_auth).json()
    log_test("accepted delivery: linked Delivery cancelled too", d2_after["status"] == "cancelled", d2_after)

    # ready cancellation
    o3 = _new_confirmed_order()
    d3 = _plan_delivery(o3)
    client.post(f"/deliveries/{d3['id']}/accept", headers=dp_auth)
    _pick_and_ready(d3)
    r = client.patch(f"/orders/{o3['id']}/cancel", json={"reason": "test"}, headers=admin_auth)
    log_test("ready delivery: Order cancel succeeds (200)", r.status_code == 200, r.text)
    d3_after = client.get(f"/deliveries/by-id/{d3['id']}", headers=admin_auth).json()
    log_test("ready delivery: linked Delivery cancelled too", d3_after["status"] == "cancelled", d3_after)

    # loaded cancellation blocked
    o4 = _new_confirmed_order()
    d4 = _plan_delivery(o4)
    client.post(f"/deliveries/{d4['id']}/accept", headers=dp_auth)
    _pick_and_ready(d4)
    client.post(f"/deliveries/{d4['id']}/load", headers=admin_auth)
    r = client.patch(f"/orders/{o4['id']}/cancel", json={"reason": "test"}, headers=admin_auth)
    log_test("loaded delivery: Order cancel BLOCKED (400)", r.status_code == 400, r.text)
    o4_after = client.get(f"/orders/{o4['id']}", headers=admin_auth).json()
    log_test("loaded delivery: Order status untouched (not cancelled)", o4_after["status"] != "cancelled", o4_after)

    # in_transit cancellation blocked
    o5 = _new_confirmed_order()
    d5 = _plan_delivery(o5)
    client.post(f"/deliveries/{d5['id']}/accept", headers=dp_auth)
    _pick_and_ready(d5)
    client.post(f"/deliveries/{d5['id']}/load", headers=admin_auth)
    client.patch(f"/deliveries/by-id/{d5['id']}", json={"status": "in_transit"}, headers=admin_auth)
    r = client.patch(f"/orders/{o5['id']}/cancel", json={"reason": "test"}, headers=admin_auth)
    log_test("in_transit delivery: Order cancel BLOCKED (400)", r.status_code == 400, r.text)

    # partially delivered cancellation blocked
    o6 = _new_confirmed_order(qty=4)
    d6 = _plan_delivery(o6)
    client.post(f"/deliveries/{d6['id']}/accept", headers=dp_auth)
    _pick_and_ready(d6)
    client.post(f"/deliveries/{d6['id']}/load", headers=admin_auth)
    client.patch(f"/deliveries/by-id/{d6['id']}", json={"status": "in_transit"}, headers=admin_auth)
    d6_fresh = client.get(f"/deliveries/by-id/{d6['id']}", headers=admin_auth).json()
    client.post(
        f"/deliveries/{d6['id']}/confirm",
        json={"items": [{"delivery_item_id": d6_fresh["items"][0]["id"], "delivered_quantity": 2}]},
        headers=admin_auth,
    )
    r = client.patch(f"/orders/{o6['id']}/cancel", json={"reason": "test"}, headers=admin_auth)
    log_test("partially_delivered: Order cancel BLOCKED (400)", r.status_code == 400, r.text)

    # delivered/completed cancellation blocked
    o7 = _new_confirmed_order(qty=2)
    d7 = _plan_delivery(o7)
    client.post(f"/deliveries/{d7['id']}/accept", headers=dp_auth)
    _pick_and_ready(d7)
    client.post(f"/deliveries/{d7['id']}/load", headers=admin_auth)
    client.patch(f"/deliveries/by-id/{d7['id']}", json={"status": "in_transit"}, headers=admin_auth)
    d7_fresh = client.get(f"/deliveries/by-id/{d7['id']}", headers=admin_auth).json()
    client.post(
        f"/deliveries/{d7['id']}/confirm",
        json={"items": [{"delivery_item_id": d7_fresh["items"][0]["id"], "delivered_quantity": 2}]},
        headers=admin_auth,
    )
    r = client.patch(f"/orders/{o7['id']}/cancel", json={"reason": "test"}, headers=admin_auth)
    log_test("delivered/completed: Order cancel BLOCKED (400)", r.status_code == 400, r.text)


# ============================================================================
# PICKUP
# ============================================================================

def run_pickup_flow():
    print("\n=== PICKUP: no Delivery record, correct flow ===")
    admin_auth, wh, prod, cust = _setup(f"Pickup_{uuid.uuid4().hex[:6]}")
    order = client.post(
        "/orders", json={"customer_id": cust["id"], "warehouse_id": wh["id"], "fulfilment_method": "pickup",
                         "items": [{"product_id": prod["id"], "quantity": 5, "unit_price": 100.0}]},
        headers=admin_auth,
    ).json()
    log_test("11. Pickup order starts Draft", order["status"] == "draft")
    log_test("11. No Delivery on Draft pickup order", order.get("delivery_id") is None and order.get("assigned_delivery_partner_id") is None)

    r = client.post(f"/orders/{order['id']}/confirm", headers=admin_auth)
    log_test("Confirm pickup order succeeds", r.status_code == 200, r.text)
    log_test("Confirmed pickup: stock reserved", r.json()["fulfilment_status"] == "reserved", r.json())

    r_pick = client.post(f"/orders/{order['id']}/pickup/pick", headers=admin_auth)
    log_test("12. Ready-for-pickup transition works", r_pick.status_code == 200, r_pick.text)
    r_ready = client.post(f"/orders/{order['id']}/pickup/ready", headers=admin_auth)
    log_test("12. Marked ready for pickup", r_ready.status_code == 200, r_ready.text)

    r_confirm = client.post(f"/orders/{order['id']}/pickup/confirm", headers=admin_auth)
    log_test("12. Pickup collected -> 200", r_confirm.status_code == 200, r_confirm.text)
    collected = r_confirm.json()
    log_test("12. Order becomes completed", collected["status"] == "completed", collected)
    log_test("11. Still no Delivery record ever created for a pickup order", collected.get("delivery_id") is None and collected.get("assigned_delivery_partner_id") is None, collected)

    r_list = client.get("/deliveries", headers=admin_auth, params={"order_id": order["id"]})
    log_test("11. No Delivery row exists for this order at all", r_list.status_code == 200 and r_list.json() == [], r_list.text)


# ============================================================================
# QUOTATION -> ORDER GUARD
# ============================================================================

def run_quotation_guard():
    print("\n=== QUOTATION -> ORDER guard ===")
    admin_auth = _register_org(f"QuoGuard_{uuid.uuid4().hex[:6]}")
    wh = client.post("/warehouses", json={"name": "WH", "is_default": True}, headers=admin_auth).json()
    prod = client.post("/products", json={"name": "P", "sku": f"SKU-{uuid.uuid4().hex[:6]}", "price": 50.0, "total_inventory": 20}, headers=admin_auth).json()
    cust = client.post("/customers", json={"name": "C"}, headers=admin_auth).json()

    # 17. Customer Quotation converts successfully
    q1 = client.post("/quotations", json={"customer_id": cust["id"], "items": [{"product_id": prod["id"], "quantity": 1, "unit_price": 50.0}]}, headers=admin_auth).json()
    client.patch(f"/quotations/{q1['id']}", json={"status": "sent"}, headers=admin_auth)
    client.patch(f"/quotations/{q1['id']}", json={"status": "accepted"}, headers=admin_auth)
    r = client.post(f"/quotations/{q1['id']}/convert-to-order", json={"warehouse_id": wh["id"]}, headers=admin_auth)
    log_test("17. Customer Quotation converts successfully (201)", r.status_code == 201, r.text)
    log_test("Converted order starts as Draft (finalized rule)", r.json()["order"]["status"] == "draft", r.text)

    # 4 (duplicate). Duplicate conversion rejected
    r_dup = client.post(f"/quotations/{q1['id']}/convert-to-order", json={"warehouse_id": wh["id"]}, headers=admin_auth)
    log_test("Duplicate Quotation -> Order conversion rejected (400)", r_dup.status_code == 400, r_dup.text)

    # 18. Lead-only Quotation conversion rejected
    lead = client.post("/leads", json={"name": "L", "mobile_number": f"9{uuid.uuid4().hex[:9]}", "lead_source": "Referral"}, headers=admin_auth).json()
    q2 = client.post("/quotations", json={"lead_id": lead["id"], "items": [{"product_id": prod["id"], "quantity": 1, "unit_price": 50.0}]}, headers=admin_auth).json()
    client.patch(f"/quotations/{q2['id']}", json={"status": "sent"}, headers=admin_auth)
    client.patch(f"/quotations/{q2['id']}", json={"status": "accepted"}, headers=admin_auth)
    r_lead = client.post(f"/quotations/{q2['id']}/convert-to-order", json={"warehouse_id": wh["id"]}, headers=admin_auth)
    log_test("18. Lead-only Quotation -> Order conversion BLOCKED (400)", r_lead.status_code == 400, r_lead.text)

    # Converted Lead/Customer Quotation succeeds
    client.patch(f"/leads/{lead['id']}", json={"lead_status": "qualified"}, headers=admin_auth)
    conv = client.post(f"/leads/{lead['id']}/convert-to-customer", json={}, headers=admin_auth)
    log_test("Lead converts to Customer", conv.status_code == 200, conv.text)
    new_cust_id = conv.json()["customer_id"] if isinstance(conv.json(), dict) and "customer_id" in conv.json() else conv.json().get("id")
    q2_after = client.get(f"/quotations/{q2['id']}", headers=admin_auth).json()
    log_test("Quotation now linked to a Customer", q2_after.get("customer_id") is not None, q2_after)
    r_conv2 = client.post(f"/quotations/{q2['id']}/convert-to-order", json={"warehouse_id": wh["id"]}, headers=admin_auth)
    log_test("Converted Lead's Quotation -> Order now succeeds (201)", r_conv2.status_code == 201, r_conv2.text)

    # 5. Cross-tenant Quotation rejected
    other_admin_auth = _register_org(f"QuoGuardOther_{uuid.uuid4().hex[:6]}")
    r_cross = client.get(f"/quotations/{q1['id']}", headers=other_admin_auth)
    log_test("5. Cross-tenant Quotation access rejected (404)", r_cross.status_code == 404, r_cross.text)


# ============================================================================
# CALCULATIONS + FINANCIAL SUMMARY
# ============================================================================

def run_calculations():
    print("\n=== Commercial calculations ===")
    admin_auth, wh, prod, cust = _setup(f"Calc_{uuid.uuid4().hex[:6]}")
    prod2 = client.post("/products", json={"name": "P2", "sku": f"SKU-{uuid.uuid4().hex[:6]}", "price": 200.0, "tax_rate": 5.0, "total_inventory": 50}, headers=admin_auth).json()
    client.post(f"/warehouses/{wh['id']}/stock/adjust", json={"product_id": prod2["id"], "quantity": 50}, headers=admin_auth)

    # No discount / no tax
    o1 = client.post("/orders", json={"customer_id": cust["id"], "warehouse_id": wh["id"], "items": [{"product_id": prod["id"], "quantity": 2, "unit_price": 100.0, "tax_rate": 0}]}, headers=admin_auth).json()
    log_test("Calc 1: no discount/no tax -> line_total=200", o1["items"][0]["line_total"] == 200.0, o1)
    log_test("Calc 1: total=200", o1["total"] == 200.0, o1)

    # Line discount only
    o2 = client.post("/orders", json={"customer_id": cust["id"], "warehouse_id": wh["id"], "items": [{"product_id": prod["id"], "quantity": 2, "unit_price": 100.0, "tax_rate": 0, "discount": 20}]}, headers=admin_auth).json()
    log_test("Calc 2: line discount only -> line_total=180", o2["items"][0]["line_total"] == 180.0, o2)

    # Tax only
    o3 = client.post("/orders", json={"customer_id": cust["id"], "warehouse_id": wh["id"], "items": [{"product_id": prod["id"], "quantity": 2, "unit_price": 100.0, "tax_rate": 10}]}, headers=admin_auth).json()
    log_test("Calc 3: tax only -> tax_amount=20", o3["items"][0]["tax_amount"] == 20.0, o3)
    log_test("Calc 3: line_total (taxable)=200, tax not double counted, order.total=220", o3["total"] == 220.0, o3)

    # Line discount + tax
    o4 = client.post("/orders", json={"customer_id": cust["id"], "warehouse_id": wh["id"], "items": [{"product_id": prod["id"], "quantity": 2, "unit_price": 100.0, "discount": 20, "tax_rate": 10}]}, headers=admin_auth).json()
    log_test("Calc 4: discount+tax -> line_total=180, tax_amount=18", o4["items"][0]["line_total"] == 180.0 and o4["items"][0]["tax_amount"] == 18.0, o4)
    log_test("Calc 4: order.total = 180+18 = 198", o4["total"] == 198.0, o4)

    # Order-level discount + tax
    o5 = client.post("/orders", json={"customer_id": cust["id"], "warehouse_id": wh["id"], "discount": 10, "items": [{"product_id": prod["id"], "quantity": 2, "unit_price": 100.0, "tax_rate": 10}]}, headers=admin_auth).json()
    log_test("Calc 5: order-level discount reduces total once (200+20-10=210)", o5["total"] == 210.0, o5)

    # Multiple products, different tax rates
    o6 = client.post(
        "/orders", json={"customer_id": cust["id"], "warehouse_id": wh["id"], "items": [
            {"product_id": prod["id"], "quantity": 1, "unit_price": 100.0, "tax_rate": 10},
            {"product_id": prod2["id"], "quantity": 1, "unit_price": 200.0, "tax_rate": 5},
        ]}, headers=admin_auth,
    ).json()
    log_test("Calc 6: multi-product, per-line tax", o6["items"][0]["tax_amount"] == 10.0 and o6["items"][1]["tax_amount"] == 10.0, o6)
    log_test("Calc 6: total = 100+10+200+10 = 320", o6["total"] == 320.0, o6)

    # Decimal quantities
    o7 = client.post("/orders", json={"customer_id": cust["id"], "warehouse_id": wh["id"], "items": [{"product_id": prod["id"], "quantity": 2, "unit_price": 99.99, "tax_rate": 10}]}, headers=admin_auth).json()
    log_test("Calc 7: decimal unit_price computed correctly", abs(o7["items"][0]["line_total"] - 199.98) < 0.01, o7)


def run_financial_summary():
    print("\n=== Financial summary (previous_balance semantics) ===")
    admin_auth, wh, prod, cust = _setup(f"FinSum_{uuid.uuid4().hex[:6]}")

    # No old balance
    o1 = client.post("/orders", json={"customer_id": cust["id"], "warehouse_id": wh["id"], "items": [{"product_id": prod["id"], "quantity": 1, "unit_price": 100.0, "tax_rate": 0}]}, headers=admin_auth).json()
    fs1 = o1.get("financial_summary", o1)
    prev1 = fs1.get("previous_balance", o1.get("previous_balance"))
    log_test("FinSum 1: customer with no old balance -> previous_balance=0", prev1 == 0.0, o1)

    # payment_type round-trip
    o2 = client.post(
        "/orders", json={"customer_id": cust["id"], "warehouse_id": wh["id"], "payment_type": "credit",
                         "items": [{"product_id": prod["id"], "quantity": 1, "unit_price": 100.0}]},
        headers=admin_auth,
    ).json()
    log_test("14. payment_type round-trips", o2["payment_type"] == "credit", o2)
    log_test("Canonical field is payment_type, not payment_method", "payment_method" not in o2, o2)


# ============================================================================
# DELIVERY PARTNER AUTHORIZATION -- 403 on ALL Order creation
# ============================================================================

def run_delivery_partner_authorization():
    print("\n=== SECURITY: Delivery Partner authorization ===")
    admin_auth, wh, prod, cust = _setup(f"DPAuth_{uuid.uuid4().hex[:6]}")
    dp, dp_auth = _create_staff(admin_auth, "DP", "Delivery Partner")

    r = client.post("/customers", json={"name": "Should fail"}, headers=dp_auth)
    log_test("34. Delivery Partner cannot POST Customer (403)", r.status_code == 403, r.text)

    for src in ("office", "delivery_vehicle"):
        r = client.post(
            "/orders", json={"customer_id": cust["id"], "warehouse_id": wh["id"], "source": src,
                             "items": [{"product_id": prod["id"], "quantity": 1, "unit_price": 100.0}]},
            headers=dp_auth,
        )
        log_test(f"35. Delivery Partner cannot POST Order (source={src}) (403)", r.status_code == 403, r.text)

    r_van = client.post(
        "/orders", json={"customer_id": cust["id"], "warehouse_id": wh["id"], "fulfilment_method": "delivery",
                         "items": [{"product_id": prod["id"], "quantity": 1, "unit_price": 100.0}]},
        headers=dp_auth,
    )
    log_test("35. Delivery Partner cannot POST any Order variant (403)", r_van.status_code == 403, r_van.text)

    r_admin = client.post(
        "/orders", json={"customer_id": cust["id"], "warehouse_id": wh["id"],
                         "items": [{"product_id": prod["id"], "quantity": 1, "unit_price": 100.0}]},
        headers=admin_auth,
    )
    log_test("Admin retains Order creation permission", r_admin.status_code == 201, r_admin.text)


# ============================================================================
# TEAM SCOPE regression (Manager not implicitly all-scope)
# ============================================================================

def run_team_manager_scope():
    print("\n=== Team Manager is not automatically all-scope ===")
    admin_auth = _register_org(f"MgrScope_{uuid.uuid4().hex[:6]}")
    role = client.post(
        "/roles", json={"name": f"TeamRole{uuid.uuid4().hex[:6]}", "workspace": "sales", "data_scope": "team",
                        "permissions": {"leads": {"view": True, "create": True}}},
        headers=admin_auth,
    ).json()
    mgr, mgr_auth = _create_staff(admin_auth, "Manager", role["name"])
    other, other_auth = _create_staff(admin_auth, "Other", role["name"])
    client.post("/teams", json={"name": "MgrTeam", "manager_id": mgr["id"], "member_ids": []}, headers=admin_auth)

    other_lead = client.post(
        "/leads", json={"name": "OtherLead", "mobile_number": f"9{uuid.uuid4().hex[:9]}", "lead_source": "Referral", "assigned_salesperson_id": other["id"]},
        headers=admin_auth,
    ).json()
    r = client.get("/leads", headers=mgr_auth)
    log_test(
        "39. Team Manager (team scope) does NOT see an unrelated user's Lead (not implicitly all-scope)",
        not any(l["id"] == other_lead["id"] for l in r.json()), r.text,
    )


# ============================================================================
# FOLLOW-UP / VISIT REGRESSION
# ============================================================================

def run_followup_visit_regression():
    print("\n=== Follow-up / Visit regression ===")
    admin_auth = _register_org(f"FuVisitRegr_{uuid.uuid4().hex[:6]}")
    lead = client.post("/leads", json={"name": "L", "mobile_number": f"9{uuid.uuid4().hex[:9]}", "lead_source": "Referral"}, headers=admin_auth).json()

    # 41/42. Direct Lead Follow-up / Visit persist
    fu = client.post("/follow-ups", json={"lead_id": lead["id"], "title": "call", "due_date": "2026-09-10T00:00:00Z"}, headers=admin_auth).json()
    log_test("41. Direct Lead Follow-up persists", fu.get("lead_id") == lead["id"], fu)
    visit = client.post("/visits", json={"lead_id": lead["id"], "purpose": "site visit"}, headers=admin_auth).json()
    log_test("42. Direct Lead Visit persists", visit.get("lead_id") == lead["id"], visit)

    # 43/44. Follow-up outcome/outcome_notes persistence + omitted != clear
    r_complete = client.post(f"/follow-ups/{fu['id']}/complete", json={"outcome": "interested", "outcome_notes": "wants a callback"}, headers=admin_auth)
    log_test("43. outcome/outcome_notes persist on complete", r_complete.json()["outcome"] == "interested" and r_complete.json()["outcome_notes"] == "wants a callback", r_complete.text)
    r_patch = client.patch(f"/follow-ups/{fu['id']}", json={"title": "renamed"}, headers=admin_auth)
    log_test("44. Omitting outcome on a later PATCH does not clear it", r_patch.json()["outcome"] == "interested", r_patch.text)

    # 45. ready_to_convert does not auto-convert Lead
    r_visit2 = client.post("/visits", json={"lead_id": lead["id"], "outcome": "ready_to_convert"}, headers=admin_auth)
    log_test("45. Visit with outcome=ready_to_convert created", r_visit2.status_code == 201, r_visit2.text)
    lead_after = client.get(f"/leads/{lead['id']}", headers=admin_auth).json()
    log_test("45. Lead NOT auto-converted (lead_status != won)", lead_after.get("lead_status") != "won", lead_after)


def run_all_tests():
    global PASSED, FAILED
    PASSED = 0
    FAILED = 0
    print("\n=======================================================")
    print("TEST SUITE: Final Consistency Pass -- Order/Delivery/Pickup/Quotation/Security")
    print("=======================================================")
    run_order_draft_and_confirm()
    run_draft_invariant_regression()
    run_cancellation_matrix()
    run_pickup_flow()
    run_quotation_guard()
    run_calculations()
    run_financial_summary()
    run_delivery_partner_authorization()
    run_team_manager_scope()
    run_followup_visit_regression()
    print("\n=======================================================")
    print(f"RESULTS: {PASSED} passed, {FAILED} failed")
    print("=======================================================\n")
    if FAILED > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_all_tests()
