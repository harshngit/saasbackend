"""Comprehensive test suite for Sales Order Single Delete and Bulk Delete APIs."""

import os
import sys
import uuid
from datetime import datetime, timezone

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from app.main import app
from app.core.database import SessionLocal
from app.models import (
    Customer,
    CustomerPayment,
    Delivery,
    DeliveryCollection,
    DeliveryItem,
    Invoice,
    InvoiceItem,
    Organization,
    Product,
    ProductVariant,
    Role,
    SalesOrder,
    SalesOrderItem,
    StockMovement,
    StockReservation,
    User,
    Warehouse,
    WarehouseStock,
)
from app.models.enums import OrganizationStatus, UserRole
from app.core.security import create_access_token, hash_password
from app.core import workflow
from app.core.realtime import manager


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


def _setup_test_environment(db, suffix: str):
    org = Organization(
        id=str(uuid.uuid4()),
        name=f"Delete Test Firm {suffix}",
        company_code=f"DEL{suffix[:4].upper()}",
        status=OrganizationStatus.ACTIVE,
    )
    db.add(org)
    db.flush()

    admin = User(
        id=str(uuid.uuid4()),
        organization_id=org.id,
        email=f"admin_{suffix}@test.com",
        name=f"Admin {suffix}",
        password_hash=hash_password("admin123"),
        role=UserRole.ADMIN,
        is_active=True,
    )
    db.add(admin)

    # Sales Officer with view/create/edit but NO delete permission
    sales_officer = User(
        id=str(uuid.uuid4()),
        organization_id=org.id,
        email=f"sales_{suffix}@test.com",
        name=f"Sales {suffix}",
        password_hash=hash_password("sales123"),
        role=UserRole.SALES_OFFICER,
        is_active=True,
    )
    db.add(sales_officer)


    # Create warehouse and products for order placement
    warehouse = Warehouse(
        id=str(uuid.uuid4()),
        organization_id=org.id,
        name="Main Warehouse",
        code=f"WH-{suffix[:4].upper()}",
        is_default=True,
        is_active=True,
    )
    db.add(warehouse)

    product = Product(
        id=str(uuid.uuid4()),
        organization_id=org.id,
        name="Standard Widget",
        sku=f"WID-{suffix[:4].upper()}",
        price=100.0,
        tax_rate=18.0,
        is_active=True,
    )

    db.add(product)

    wh_stock = WarehouseStock(
        id=str(uuid.uuid4()),
        organization_id=org.id,
        warehouse_id=warehouse.id,
        product_id=product.id,
        on_hand_quantity=1000.0,
    )
    db.add(wh_stock)

    customer = Customer(
        id=str(uuid.uuid4()),
        organization_id=org.id,
        name="Test Customer",
        phone="+919876543210",
        billing_address="123 Test Street",
    )
    db.add(customer)

    db.commit()

    admin_token = create_access_token(
        user_id=admin.id,
        role=admin.role.value,
        organization_id=org.id,
    )
    sales_token = create_access_token(
        user_id=sales_officer.id,
        role=sales_officer.role.value,
        organization_id=org.id,
    )


    org_id = str(org.id)
    admin_id = str(admin.id)
    sales_id = str(sales_officer.id)
    warehouse_id = str(warehouse.id)
    product_id = str(product.id)
    product_name = str(product.name)
    customer_id = str(customer.id)

    return {
        "org_id": org_id,
        "admin_id": admin_id,
        "sales_id": sales_id,
        "warehouse_id": warehouse_id,
        "product_id": product_id,
        "product_name": product_name,
        "customer_id": customer_id,
        "admin_headers": {"Authorization": f"Bearer {admin_token}"},
        "sales_headers": {"Authorization": f"Bearer {sales_token}"},
    }


def _create_order(env, status="placed", fulfilment_status="not_started", num_items=2):
    db = SessionLocal()
    try:
        order = SalesOrder(
            id=str(uuid.uuid4()),
            organization_id=env["org_id"],
            order_number=f"SO-{uuid.uuid4().hex[:6].upper()}",
            customer_id=env["customer_id"],
            warehouse_id=env["warehouse_id"],
            status=status,
            fulfilment_status=fulfilment_status,
            subtotal=200.0,
            tax=36.0,
            total=236.0,
        )
        db.add(order)
        db.flush()

        for i in range(num_items):
            item = SalesOrderItem(
                id=str(uuid.uuid4()),
                order_id=order.id,
                product_id=env["product_id"],
                product_name=env["product_name"],
                quantity=10,
                unit_price=100.0,
                line_total=1000.0,
                tax_rate=18.0,
                reserved_quantity=10.0,
            )
            db.add(item)
            db.flush()

            # Add a StockReservation
            res = StockReservation(
                id=str(uuid.uuid4()),
                organization_id=env["org_id"],
                warehouse_id=env["warehouse_id"],
                order_id=order.id,
                order_item_id=item.id,
                product_id=env["product_id"],
                reserved_quantity=10.0,
                consumed_quantity=0.0,
            )
            db.add(res)

        db.commit()
        db.refresh(order)
        return order.id, order.order_number
    finally:
        db.close()



def run_tests():
    print("\n=======================================================")
    print("TEST SUITE: Sales Order Single Delete + Bulk Delete")
    print("=======================================================\n")

    db = SessionLocal()
    suffix_a = uuid.uuid4().hex[:6]
    suffix_b = uuid.uuid4().hex[:6]
    env_a = _setup_test_environment(db, suffix_a)
    env_b = _setup_test_environment(db, suffix_b)
    db.close()

    print("--- 1. SINGLE DELETE: PERMISSION & SCOPING TESTS ---")
    order_id_1, _ = _create_order(env_a, status="draft")

    # Sales Officer without delete permission -> 403 Forbidden
    res_sales = client.delete(f"/orders/{order_id_1}", headers=env_a["sales_headers"])
    assert_eq(res_sales.status_code, 403, "Sales Officer without delete permission receives 403 Forbidden")

    # Cross-tenant attempt: Org B admin attempts to delete Org A order -> 404 Not Found
    res_cross = client.delete(f"/orders/{order_id_1}", headers=env_b["admin_headers"])
    assert_eq(res_cross.status_code, 404, "Cross-tenant delete attempt returns 404 Not Found")

    # Nonexistent order -> 404 Not Found
    res_404 = client.delete(f"/orders/{uuid.uuid4()}", headers=env_a["admin_headers"])
    assert_eq(res_404.status_code, 404, "Deleting nonexistent order returns 404 Not Found")

    print("\n--- 2. SINGLE DELETE: SUCCESSFUL DELETION OF DRAFT & CANCELLED ORDERS ---")
    # Clean draft order deletion
    res_del_draft = client.delete(f"/orders/{order_id_1}", headers=env_a["admin_headers"])
    assert_eq(res_del_draft.status_code, 204, "Draft sales order deleted successfully with 204 No Content")

    # Verify order is gone from GET /orders/{id}
    res_get = client.get(f"/orders/{order_id_1}", headers=env_a["admin_headers"])
    assert_eq(res_get.status_code, 404, "Deleted sales order is no longer accessible via GET /orders/{id}")

    # Verify child items and reservations are removed from DB
    db = SessionLocal()
    so_items = db.query(SalesOrderItem).filter(SalesOrderItem.order_id == order_id_1).all()
    assert_eq(len(so_items), 0, "SalesOrderItems were cascade removed on order deletion")
    res_rows = db.query(StockReservation).filter(StockReservation.order_id == order_id_1).all()
    assert_eq(len(res_rows), 0, "StockReservations were cleaned up on order deletion")
    db.close()

    # Cancelled order deletion
    order_id_cancelled, _ = _create_order(env_a, status="cancelled")
    res_del_canc = client.delete(f"/orders/{order_id_cancelled}", headers=env_a["admin_headers"])
    assert_eq(res_del_canc.status_code, 204, "Cancelled sales order deleted successfully with 204 No Content")

    # Rejected order deletion
    order_id_rejected, _ = _create_order(env_a, status="rejected")
    res_del_rej = client.delete(f"/orders/{order_id_rejected}", headers=env_a["admin_headers"])
    assert_eq(res_del_rej.status_code, 204, "Rejected sales order deleted successfully with 204 No Content")

    print("\n--- 3. SINGLE DELETE: DEPENDENCY & FULFILMENT GUARDS ---")
    # A. Dispatched / Loaded order -> 400 Bad Request
    order_id_loaded, _ = _create_order(env_a, status="processing", fulfilment_status="loaded")
    res_loaded = client.delete(f"/orders/{order_id_loaded}", headers=env_a["admin_headers"])
    assert_eq(res_loaded.status_code, 400, "Deleting loaded order blocked with 400 Bad Request")

    # In-transit order -> 400 Bad Request
    order_id_transit, _ = _create_order(env_a, status="processing", fulfilment_status="in_transit")
    res_transit = client.delete(f"/orders/{order_id_transit}", headers=env_a["admin_headers"])
    assert_eq(res_transit.status_code, 400, "Deleting in-transit order blocked with 400 Bad Request")

    # Delivered / Completed order -> 400 Bad Request
    order_id_delivered, _ = _create_order(env_a, status="completed", fulfilment_status="delivered")
    res_deliv = client.delete(f"/orders/{order_id_delivered}", headers=env_a["admin_headers"])
    assert_eq(res_deliv.status_code, 400, "Deleting completed/delivered order blocked with 400 Bad Request")

    # B. Order with active Invoice -> 400 Bad Request
    order_id_inv, _ = _create_order(env_a, status="placed")
    db = SessionLocal()
    inv = Invoice(
        id=str(uuid.uuid4()),
        organization_id=env_a["org_id"],
        order_id=order_id_inv,
        customer_id=env_a["customer_id"],
        invoice_number="INV-DEL-TEST-001",
        status="unpaid",
        subtotal=200.0,
        tax=36.0,
        total=236.0,
        is_credit_note=False,
    )
    db.add(inv)
    db.commit()
    db.close()

    res_inv = client.delete(f"/orders/{order_id_inv}", headers=env_a["admin_headers"])
    assert_eq(res_inv.status_code, 400, "Deleting order with active invoice blocked with 400 Bad Request")
    assert_eq("invoices" in res_inv.json().get("detail", "").lower(), True, "Error detail explains invoice conflict")

    # C. Order with active Delivery -> 400 Bad Request
    order_id_deliv, _ = _create_order(env_a, status="placed")
    db = SessionLocal()
    deliv = Delivery(
        id=str(uuid.uuid4()),
        organization_id=env_a["org_id"],
        sales_order_id=order_id_deliv,
        customer_id=env_a["customer_id"],
        delivery_note_number="DN-DEL-TEST-001",
        status="planned",
    )
    db.add(deliv)
    db.commit()
    db.close()

    res_deliv_active = client.delete(f"/orders/{order_id_deliv}", headers=env_a["admin_headers"])
    assert_eq(res_deliv_active.status_code, 400, "Deleting order with active delivery blocked with 400 Bad Request")

    # D. Order with Customer Payment -> 400 Bad Request
    order_id_pay, _ = _create_order(env_a, status="placed")
    db = SessionLocal()
    payment = CustomerPayment(
        id=str(uuid.uuid4()),
        organization_id=env_a["org_id"],
        customer_id=env_a["customer_id"],
        order_id=order_id_pay,
        amount=100.0,
        payment_mode="cash",
    )
    db.add(payment)
    db.commit()
    db.close()

    res_pay = client.delete(f"/orders/{order_id_pay}", headers=env_a["admin_headers"])
    assert_eq(res_pay.status_code, 400, "Deleting order with recorded customer payment blocked with 400 Bad Request")

    # E. Order with Delivery Collection -> 400 Bad Request
    order_id_col, _ = _create_order(env_a, status="placed")
    db = SessionLocal()
    col = DeliveryCollection(
        id=str(uuid.uuid4()),
        organization_id=env_a["org_id"],
        sales_order_id=order_id_col,
        amount=50.0,
        payment_mode="cash",
    )
    db.add(col)
    db.commit()
    db.close()


    res_col = client.delete(f"/orders/{order_id_col}", headers=env_a["admin_headers"])
    assert_eq(res_col.status_code, 400, "Deleting order with delivery collection blocked with 400 Bad Request")

    print("\n--- 4. SINGLE DELETE: REALTIME EVENT VERIFICATION ---")
    captured_events = []
    orig_emit = manager.emit

    def spy_emit(org_id, event_name, data, user_id=None, required_permission=None):
        captured_events.append({
            "org_id": org_id,
            "event_name": event_name,
            "data": data,
            "user_id": user_id,
            "required_permission": required_permission,
        })
        return orig_emit(org_id, event_name, data, user_id, required_permission)

    manager.emit = spy_emit

    order_id_event, order_num_event = _create_order(env_a, status="draft")
    
    # Delete order
    del_ev_res = client.delete(f"/orders/{order_id_event}", headers=env_a["admin_headers"])
    assert_eq(del_ev_res.status_code, 204, "Order for event test deleted with 204")

    matched_events = [
        e for e in captured_events
        if e["event_name"] == "order.deleted" and e["data"].get("order_id") == order_id_event
    ]
    assert_eq(len(matched_events), 1, "Realtime event 'order.deleted' was emitted on commit")
    if matched_events:
        evt = matched_events[0]
        assert_eq(evt["org_id"], env_a["org_id"], "Event org_id matches tenant")
        assert_eq(evt["data"].get("order_number"), order_num_event, "Event payload contains correct order_number")
        assert_eq(evt["required_permission"], "sales_orders:view", "Event requires sales_orders:view permission")

    manager.emit = orig_emit

    print("\n--- 5. BULK DELETE: VALIDATION & PERMISSIONS ---")

    # Empty IDs -> 422 Unprocessable Entity
    res_bulk_empty = client.request(
        "DELETE", "/orders/bulk-delete", json={"ids": []}, headers=env_a["admin_headers"])
    assert_eq(res_bulk_empty.status_code, 422, "Empty ids list rejected with 422 Unprocessable Entity")

    # Sales Officer without delete permission -> 403 Forbidden
    res_bulk_sales = client.request(
        "DELETE", "/orders/bulk-delete", json={"ids": ["dummy-id"]}, headers=env_a["sales_headers"])
    assert_eq(res_bulk_sales.status_code, 403, "Bulk delete with unauthorized role returns 403 Forbidden")

    # Nonexistent ID in batch -> 404 Not Found
    res_bulk_404 = client.request(
        "DELETE", "/orders/bulk-delete", json={"ids": [str(uuid.uuid4())]}, headers=env_a["admin_headers"]
    )
    assert_eq(res_bulk_404.status_code, 404, "Bulk delete with nonexistent ID returns 404 Not Found")

    print("\n--- 6. BULK DELETE: SUCCESSFUL ATOMIC BATCH ---")
    b_id1, _ = _create_order(env_a, status="draft")
    b_id2, _ = _create_order(env_a, status="placed")
    b_id3, _ = _create_order(env_a, status="cancelled")

    # Bulk delete 3 orders
    res_bulk_ok = client.request(
        "DELETE", "/orders/bulk-delete",
        json={"ids": [b_id1, b_id2, b_id3]},
        headers=env_a["admin_headers"],
    )
    assert_eq(res_bulk_ok.status_code, 200, "Bulk delete 3 valid orders returns 200 OK")
    assert_eq(res_bulk_ok.json().get("deleted"), 3, "Bulk delete response reports deleted count = 3")

    # Verify all 3 are deleted from database
    db = SessionLocal()
    remaining = db.query(SalesOrder).filter(SalesOrder.id.in_([b_id1, b_id2, b_id3])).all()
    assert_eq(len(remaining), 0, "All 3 orders removed from database")
    rem_items = db.query(SalesOrderItem).filter(SalesOrderItem.order_id.in_([b_id1, b_id2, b_id3])).all()
    assert_eq(len(rem_items), 0, "All items for the 3 orders removed from database")
    db.close()

    print("\n--- 7. BULK DELETE: DUPLICATE IDS HANDLING ---")
    d_id1, _ = _create_order(env_a, status="draft")
    d_id2, _ = _create_order(env_a, status="placed")

    res_dup = client.request(
        "DELETE", "/orders/bulk-delete",
        json={"ids": [d_id1, d_id1, d_id2, d_id2, d_id1]},
        headers=env_a["admin_headers"],
    )
    assert_eq(res_dup.status_code, 200, "Bulk delete with duplicate IDs succeeds with 200 OK")
    assert_eq(res_dup.json().get("deleted"), 2, "Bulk delete counts unique deleted orders (2 instead of 5)")

    print("\n--- 8. BULK DELETE: CROSS-TENANT ISOLATION ---")
    org_b_order, _ = _create_order(env_b, status="draft")
    org_a_order, _ = _create_order(env_a, status="draft")

    # Org A tries to bulk delete a batch containing Org B's order
    res_cross_bulk = client.request(
        "DELETE", "/orders/bulk-delete",
        json={"ids": [org_a_order, org_b_order]},
        headers=env_a["admin_headers"],
    )
    assert_eq(res_cross_bulk.status_code, 404, "Bulk delete containing cross-tenant ID rejected with 404")

    # Verify neither order was deleted (atomicity / tenant isolation)
    db = SessionLocal()
    order_a_exists = db.query(SalesOrder).filter(SalesOrder.id == org_a_order).first() is not None
    order_b_exists = db.query(SalesOrder).filter(SalesOrder.id == org_b_order).first() is not None
    assert_eq(order_a_exists, True, "Org A order was preserved after aborted cross-tenant bulk delete")
    assert_eq(order_b_exists, True, "Org B order was preserved after aborted cross-tenant bulk delete")
    db.close()

    print("\n--- 9. BULK DELETE: ATOMICITY & ROLLBACK ON DEPENDENCY CONFLICT ---")
    valid_id_1, _ = _create_order(env_a, status="draft")
    valid_id_2, _ = _create_order(env_a, status="placed")
    conflict_id, _ = _create_order(env_a, status="placed")

    # Add an active invoice to conflict_id
    db = SessionLocal()
    inv_conflict = Invoice(
        id=str(uuid.uuid4()),
        organization_id=env_a["org_id"],
        order_id=conflict_id,
        customer_id=env_a["customer_id"],
        invoice_number="INV-CONFLICT-001",
        status="unpaid",
        subtotal=200.0,
        tax=36.0,
        total=236.0,
        is_credit_note=False,
    )
    db.add(inv_conflict)
    db.commit()
    db.close()


    # Attempt to bulk delete all 3
    res_conflict_batch = client.request(
        "DELETE", "/orders/bulk-delete",
        json={"ids": [valid_id_1, conflict_id, valid_id_2]},
        headers=env_a["admin_headers"],
    )
    assert_eq(res_conflict_batch.status_code, 400, "Bulk delete with 1 conflicting order rejected with 400")

    # Verify NO orders were deleted (100% atomic rollback)
    db = SessionLocal()
    v1_exists = db.query(SalesOrder).filter(SalesOrder.id == valid_id_1).first() is not None
    v2_exists = db.query(SalesOrder).filter(SalesOrder.id == valid_id_2).first() is not None
    conf_exists = db.query(SalesOrder).filter(SalesOrder.id == conflict_id).first() is not None
    assert_eq(v1_exists, True, "Valid order 1 was rolled back and preserved")
    assert_eq(v2_exists, True, "Valid order 2 was rolled back and preserved")
    assert_eq(conf_exists, True, "Conflicting order was rolled back and preserved")
    db.close()

    print("\n=======================================================")
    print(f"RESULTS: {_passed} PASSED, {_failed} FAILED")
    print("=======================================================\n")
    if _failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_tests()
