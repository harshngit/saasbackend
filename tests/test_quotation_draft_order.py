"""Focused test suite for Quotation -> Draft Order -> Confirm workflow.

Covers:
  TEST 1: draft_orders_enabled=true  -> conversion creates a draft order with no
          stock reservation; confirming it reserves stock exactly once.
  TEST 2: draft_orders_enabled=false -> conversion keeps the existing direct-
          placement behaviour (placed immediately, stock reserved on conversion).
  TEST 3: No double reservation across convert + confirm.
  TEST 4: A quotation alone (draft/sent/accepted) never touches stock.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from app.main import app
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


def _register_org(label: str):
    email = f"admin_{uuid.uuid4().hex[:8]}@{label}.com"
    r = client.post("/auth/register", json={
        "organization_name": f"{label} Traders",
        "admin_name": f"Admin {label}",
        "email": email,
        "password": "Password123!",
        "role": "admin",
    })
    assert r.status_code == 201, r.text
    token = r.json()["tokens"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _setup_org(label: str):
    auth = _register_org(label)
    wh_res = client.post("/warehouses", json={"name": "Main WH", "code": f"WH-{uuid.uuid4().hex[:6]}"}, headers=auth)
    wh_id = wh_res.json()["id"]

    p_res = client.post("/products", json={
        "name": "Test Widget",
        "sku": f"WID-{uuid.uuid4().hex[:6]}",
        "price": 50.0,
        "tax_rate": 10.0,
        "pricing": {"purchase_price": 30.0, "selling_price": 50.0, "currency": "INR"},
    }, headers=auth)
    assert p_res.status_code == 201, p_res.text
    prod_id = p_res.json()["id"]

    client.post(f"/warehouses/{wh_id}/stock/adjust", json={
        "product_id": prod_id, "quantity": 100,
    }, headers=auth)

    c_res = client.post("/customers", json={
        "name": "Test Customer",
        "phone": f"9{uuid.uuid4().int % 10**9:09d}",
        "billing_address": "1 Test Lane",
    }, headers=auth)
    assert c_res.status_code == 201, c_res.text
    cust_id = c_res.json()["id"]

    return auth, wh_id, prod_id, cust_id


def _stock(auth, wh_id, prod_id):
    r = client.get(f"/warehouses/stock?warehouse_id={wh_id}&product_id={prod_id}", headers=auth)
    assert r.status_code == 200, r.text
    rows = r.json()
    assert rows, "expected a stock row for the seeded product"
    row = rows[0]
    return row["on_hand"], row["reserved"], row["available"]


def _create_send_accept_quotation(auth, cust_id, prod_id, quantity=20):
    q_res = client.post("/quotations", json={
        "customer_id": cust_id,
        "items": [{"product_id": prod_id, "quantity": quantity, "unit_price": 50.0, "tax_rate": 10.0}],
    }, headers=auth)
    assert q_res.status_code == 201, q_res.text
    q = q_res.json()
    q_id = q["id"]

    client.patch(f"/quotations/{q_id}", json={"status": "sent"}, headers=auth)
    r = client.patch(f"/quotations/{q_id}", json={"status": "accepted"}, headers=auth)
    assert r.status_code == 200, r.text
    return q_id


def test_1_draft_enabled():
    print("\n--- TEST 1: draft_orders_enabled = true ---")
    auth, wh_id, prod_id, cust_id = _setup_org("draftco")
    client.patch("/sales-workflow-settings", json={
        "draft_orders_enabled": True, "reserve_stock_on_order": True,
    }, headers=auth)

    q_id = _create_send_accept_quotation(auth, cust_id, prod_id, quantity=20)

    on_hand, reserved, available = _stock(auth, wh_id, prod_id)
    assert_eq(on_hand, 100.0, "Before conversion: physical stock is 100")
    assert_eq(reserved, 0.0, "Before conversion: reserved is 0")
    assert_eq(available, 100.0, "Before conversion: available is 100")

    conv_res = client.post(f"/quotations/{q_id}/convert-to-order", json={"warehouse_id": wh_id}, headers=auth)
    assert_eq(conv_res.status_code, 201, "Convert-to-order succeeds")
    conv = conv_res.json()
    order_id = conv["order"]["id"]
    assert_eq(conv["order"]["status"], "draft", "Converted order status is 'draft'")
    assert_eq(conv["quotation_status"], "converted", "Quotation status becomes 'converted'")

    on_hand, reserved, available = _stock(auth, wh_id, prod_id)
    assert_eq(on_hand, 100.0, "After convert-to-draft: physical stock unchanged at 100")
    assert_eq(reserved, 0.0, "After convert-to-draft: reserved is still 0 (no reservation on draft)")
    assert_eq(available, 100.0, "After convert-to-draft: available is still 100")

    confirm_res = client.post(f"/orders/{order_id}/confirm", headers=auth)
    assert_eq(confirm_res.status_code, 200, "Confirm draft order succeeds")
    confirmed = confirm_res.json()
    # Public Order status contract: internal 'placed' -> public 'confirmed'.
    assert_eq(confirmed["status"], "confirmed", "Confirmed order status is 'confirmed'")
    assert_eq(confirmed["fulfilment_status"], "reserved", "Confirmed order fulfilment_status is 'reserved'")

    on_hand, reserved, available = _stock(auth, wh_id, prod_id)
    assert_eq(on_hand, 100.0, "After confirm: physical stock still 100 (no warehouse deduction)")
    assert_eq(reserved, 20.0, "After confirm: reserved is exactly 20")
    assert_eq(available, 80.0, "After confirm: available is 80")


def test_2_draft_disabled():
    print("\n--- TEST 2: draft_orders_enabled = false (now vestigial -- every conversion still starts Draft) ---")
    auth, wh_id, prod_id, cust_id = _setup_org("directco")
    client.patch("/sales-workflow-settings", json={"draft_orders_enabled": False}, headers=auth)

    q_id = _create_send_accept_quotation(auth, cust_id, prod_id, quantity=15)

    conv_res = client.post(f"/quotations/{q_id}/convert-to-order", json={"warehouse_id": wh_id}, headers=auth)
    assert_eq(conv_res.status_code, 201, "Convert-to-order succeeds")
    conv = conv_res.json()
    order_id = conv["order"]["id"]
    # Finalized business rule: draft_orders_enabled no longer gates this --
    # every conversion starts as an unreserved Draft regardless of the
    # (now vestigial) setting.
    assert_eq(conv["order"]["status"], "draft", "Converted order still starts as Draft even with the setting off")
    assert_eq(conv["order"]["fulfilment_status"], "not_started", "Draft order is not reserved")

    on_hand, reserved, available = _stock(auth, wh_id, prod_id)
    assert_eq(on_hand, 100.0, "Physical stock unchanged at 100")
    assert_eq(reserved, 0.0, "Nothing reserved while still Draft")
    assert_eq(available, 100.0, "Available stays 100 while still Draft")

    confirm_res = client.post(f"/orders/{order_id}/confirm", headers=auth)
    assert_eq(confirm_res.status_code, 200, "Confirming the converted order succeeds")
    # Public Order status contract: internal 'placed' -> public 'confirmed'.
    assert_eq(confirm_res.json()["status"], "confirmed", "Confirmed order status is 'confirmed'")
    assert_eq(confirm_res.json()["fulfilment_status"], "reserved", "Confirmed order is reserved")

    on_hand, reserved, available = _stock(auth, wh_id, prod_id)
    assert_eq(on_hand, 100.0, "Physical stock still unchanged at 100")
    assert_eq(reserved, 15.0, "Stock reserved on confirm: 15")
    assert_eq(available, 85.0, "Available drops to 85 after confirm")


def test_3_no_double_reservation():
    print("\n--- TEST 3: No double reservation across convert + confirm ---")
    auth, wh_id, prod_id, cust_id = _setup_org("noduplico")
    client.patch("/sales-workflow-settings", json={
        "draft_orders_enabled": True, "reserve_stock_on_order": True,
    }, headers=auth)

    q_id = _create_send_accept_quotation(auth, cust_id, prod_id, quantity=20)
    conv = client.post(f"/quotations/{q_id}/convert-to-order", json={"warehouse_id": wh_id}, headers=auth).json()
    order_id = conv["order"]["id"]

    _, reserved_after_draft, _ = _stock(auth, wh_id, prod_id)
    assert_eq(reserved_after_draft, 0.0, "No reservation right after draft conversion")

    client.post(f"/orders/{order_id}/confirm", headers=auth)
    _, reserved_after_confirm, _ = _stock(auth, wh_id, prod_id)
    assert_eq(reserved_after_confirm, 20.0, "Reserved is 20 after one confirm (not 40)")

    # Confirming again must be refused — an already-placed order is not a draft.
    second_confirm = client.post(f"/orders/{order_id}/confirm", headers=auth)
    assert_eq(second_confirm.status_code, 400, "Re-confirming an already-placed order is refused")

    _, reserved_final, _ = _stock(auth, wh_id, prod_id)
    assert_eq(reserved_final, 20.0, "Reserved remains exactly 20 — reservation happened exactly once")


def test_4_quotation_alone_never_touches_stock():
    print("\n--- TEST 4: Quotation alone (draft/sent/accepted) never changes stock ---")
    auth, wh_id, prod_id, cust_id = _setup_org("quotonlyco")

    on_hand, reserved, available = _stock(auth, wh_id, prod_id)
    assert_eq((on_hand, reserved, available), (100.0, 0.0, 100.0), "Stock at baseline before any quotation")

    q_res = client.post("/quotations", json={
        "customer_id": cust_id,
        "items": [{"product_id": prod_id, "quantity": 20, "unit_price": 50.0, "tax_rate": 10.0}],
    }, headers=auth)
    q_id = q_res.json()["id"]
    on_hand, reserved, available = _stock(auth, wh_id, prod_id)
    assert_eq((on_hand, reserved, available), (100.0, 0.0, 100.0), "Stock unchanged after quotation created (draft)")

    client.patch(f"/quotations/{q_id}", json={"status": "sent"}, headers=auth)
    on_hand, reserved, available = _stock(auth, wh_id, prod_id)
    assert_eq((on_hand, reserved, available), (100.0, 0.0, 100.0), "Stock unchanged after quotation sent")

    client.patch(f"/quotations/{q_id}", json={"status": "accepted"}, headers=auth)
    on_hand, reserved, available = _stock(auth, wh_id, prod_id)
    assert_eq((on_hand, reserved, available), (100.0, 0.0, 100.0), "Stock unchanged after quotation accepted")


def run_tests():
    print("\n=======================================================")
    print("TEST SUITE: Quotation -> Draft Order -> Confirm")
    print("=======================================================")
    test_1_draft_enabled()
    test_2_draft_disabled()
    test_3_no_double_reservation()
    test_4_quotation_alone_never_touches_stock()

    print("\n=======================================================")
    print(f"RESULTS: {_passed} passed, {_failed} failed")
    print("=======================================================\n")
    if _failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_tests()
