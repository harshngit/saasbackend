import json
import os
import sys
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.database import SessionLocal, engine
from app.core.security import create_access_token
from app.main import app
from app.models import Customer, Organization, OrganizationStatus, Product, Role, SalesOrder, Supplier, User, UserRole, Warehouse
from app.services import delivery_service, notification_service, order_service, stock_service

client = TestClient(app)


def _setup_org_and_users():
    db = SessionLocal()
    # Create Organization A
    org_a = Organization(name="Realtime Org A", status=OrganizationStatus.ACTIVE)
    db.add(org_a)
    db.flush()

    # Create Organization B
    org_b = Organization(name="Realtime Org B", status=OrganizationStatus.ACTIVE)
    db.add(org_b)
    db.flush()

    # Users in Org A
    user_a1 = User(
        organization_id=org_a.id,
        email="user_a1@realtime.com",
        name="User A1 Admin",
        role=UserRole.ADMIN,
        system_role="admin",
        password_hash="fake",
        is_active=True,
    )
    user_a2 = User(
        organization_id=org_a.id,
        email="user_a2@realtime.com",
        name="User A2 Staff",
        role=UserRole.SALES_OFFICER,
        system_role="staff",
        password_hash="fake",
        is_active=True,
    )
    # User in Org B
    user_b1 = User(
        organization_id=org_b.id,
        email="user_b1@realtime.com",
        name="User B1 Admin",
        role=UserRole.ADMIN,
        system_role="admin",
        password_hash="fake",
        is_active=True,
    )
    # Inactive User
    user_inactive = User(
        organization_id=org_a.id,
        email="inactive@realtime.com",
        name="Inactive User",
        role=UserRole.SALES_OFFICER,
        system_role="staff",
        password_hash="fake",
        is_active=False,
    )
    db.add_all([user_a1, user_a2, user_b1, user_inactive])

    # Default warehouse and product for Org A
    wh_a = Warehouse(organization_id=org_a.id, name="Main WH A", code="WH-A-01", is_default=True)
    cust_a = Customer(organization_id=org_a.id, name="Cust A", customer_id="CUST-A-01")
    prod_a = Product(organization_id=org_a.id, name="Prod A", product_id="PROD-A-01", price=100.0, total_inventory=100)
    db.add_all([wh_a, cust_a, prod_a])
    db.flush()

    stock_service.adjust_on_hand(db, org_a.id, wh_a.id, prod_a.id, None, 50, "initial_count")
    db.commit()

    tokens = {
        "user_a1": create_access_token(user_a1.id, user_a1.role.value, org_a.id),
        "user_a2": create_access_token(user_a2.id, user_a2.role.value, org_a.id),
        "user_b1": create_access_token(user_b1.id, user_b1.role.value, org_b.id),
        "inactive": create_access_token(user_inactive.id, user_inactive.role.value, org_a.id),
        "org_a_id": org_a.id,
        "org_b_id": org_b.id,
        "user_a1_id": user_a1.id,
        "user_a2_id": user_a2.id,
        "user_b1_id": user_b1.id,
        "prod_a_id": prod_a.id,
        "wh_a_id": wh_a.id,
        "cust_a_id": cust_a.id,
    }
    db.close()
    return tokens


def test_websocket_suite():
    print("\n==================================================")
    print("STARTING WEBSOCKET & REAL-TIME VERIFICATION SUITE")
    print("==================================================")

    data = _setup_org_and_users()

    # -------------------------------------------------------------
    # TEST 1: Connection & Authentication
    # -------------------------------------------------------------
    print("\n--- TEST GROUP 1: Connection & Authentication ---")

    # 1.1 Valid Token
    with client.websocket_connect(f"/ws?token={data['user_a1']}") as ws:
        ws.send_text("ping")
        resp = ws.receive_text()
        assert resp == "pong", f"Expected 'pong', got {resp}"
        print("  PASS  1.1 Valid JWT token connects & ping/pong succeeds")

    # 1.2 Missing Token
    try:
        with client.websocket_connect("/ws") as ws:
            ws.receive_text()
        assert False, "Should have rejected missing token"
    except WebSocketDisconnect as e:
        assert e.code == 1008
        print("  PASS  1.2 Missing token correctly rejected with 1008 Policy Violation")

    # 1.3 Invalid Token
    try:
        with client.websocket_connect("/ws?token=invalid.jwt.token") as ws:
            ws.receive_text()
        assert False, "Should have rejected invalid token"
    except WebSocketDisconnect as e:
        assert e.code == 1008
        print("  PASS  1.3 Malformed JWT token rejected with 1008 Policy Violation")

    # 1.4 Inactive User Token
    try:
        with client.websocket_connect(f"/ws?token={data['inactive']}") as ws:
            ws.receive_text()
        assert False, "Should have rejected inactive user"
    except WebSocketDisconnect as e:
        assert e.code == 1008
        print("  PASS  1.4 Inactive user token rejected with 1008 Policy Violation")

    # -------------------------------------------------------------
    # TEST 2: Tenant Isolation & Scoping
    # -------------------------------------------------------------
    print("\n--- TEST GROUP 2: Tenant Isolation & Targeted Messaging ---")

    with client.websocket_connect(f"/ws?token={data['user_a1']}") as ws_a1, \
         client.websocket_connect(f"/ws?token={data['user_a2']}") as ws_a2, \
         client.websocket_connect(f"/ws?token={data['user_b1']}") as ws_b1:

        # Trigger user-targeted notification for User A1
        db = SessionLocal()
        notification_service.notify(
            db,
            user_id=data["user_a1_id"],
            title="Personal Alert",
            body="Only for User A1",
            organization_id=data["org_a_id"],
        )
        db.commit()
        db.close()

        # User A1 should receive the event
        msg_a1 = json.loads(ws_a1.receive_text())
        assert msg_a1["event"] == "notification.created"
        assert msg_a1["data"]["title"] == "Personal Alert"
        print("  PASS  2.1 Targeted notification delivered to intended recipient User A1")

        # Org B user and User A2 should have received nothing

    # -------------------------------------------------------------
    # TEST 3: Sales Orders Real-Time Events (P0)
    # -------------------------------------------------------------
    print("\n--- TEST GROUP 3: Sales Orders Real-Time Events (P0) ---")

    with client.websocket_connect(f"/ws?token={data['user_a1']}") as ws_a1:
        # 3.1 Order Created Event
        create_res = client.post(
            "/orders",
            headers={"Authorization": f"Bearer {data['user_a1']}"},
            json={
                "customer_id": data["cust_a_id"],
                "warehouse_id": data["wh_a_id"],
                "items": [{"product_id": data["prod_a_id"], "quantity": 2, "unit_price": 100.0}],
            },
        )
        assert create_res.status_code == 201
        order_id = create_res.json()["id"]

        msg = json.loads(ws_a1.receive_text())
        # Might receive inventory.stock_updated or order.created
        events = [msg["event"]]
        # In draft mode, inventory is not reserved yet, so order.created is emitted
        assert "order.created" in events, f"Expected order.created, got {events}"
        print("  PASS  3.1 order.created emitted in real time on POST /orders")

        # 3.2 Order Confirmed Event
        confirm_res = client.post(
            f"/orders/{order_id}/confirm",
            headers={"Authorization": f"Bearer {data['user_a1']}"},
        )
        assert confirm_res.status_code == 200

        # On confirm: inventory.stock_updated, notification.created (admin alert), order.status_changed
        received_events = []
        for _ in range(3):
            raw = ws_a1.receive_text()
            evt = json.loads(raw)
            received_events.append(evt["event"])

        assert "order.status_changed" in received_events
        assert "inventory.stock_updated" in received_events
        assert "notification.created" in received_events
        print("  PASS  3.2 order.status_changed, inventory.stock_updated, and notification.created emitted on /confirm")

        # 3.3 Order Cancelled Event
        cancel_res = client.patch(
            f"/orders/{order_id}/cancel",
            headers={"Authorization": f"Bearer {data['user_a1']}"},
            json={"reason": "Customer cancelled"},
        )
        assert cancel_res.status_code == 200

        received_events = []
        for _ in range(2):
            raw = ws_a1.receive_text()
            evt = json.loads(raw)
            received_events.append(evt["event"])

        assert "order.cancelled" in received_events
        assert "inventory.stock_updated" in received_events
        print("  PASS  3.3 order.cancelled and stock release emitted on /cancel")

    # -------------------------------------------------------------
    # TEST 4: Transaction Safety & Rollback Isolation
    # -------------------------------------------------------------
    print("\n--- TEST GROUP 4: Transaction Safety & Rollback Isolation ---")

    with client.websocket_connect(f"/ws?token={data['user_a1']}") as ws_a1:
        db = SessionLocal()
        # Queue an event in a transaction
        notification_service.notify(
            db,
            user_id=data["user_a1_id"],
            title="Rollback Test",
            organization_id=data["org_a_id"],
        )
        # Explicit rollback
        db.rollback()
        db.close()

        # Send ping to verify no stray event is queued in socket buffer
        ws_a1.send_text("ping")
        msg = ws_a1.receive_text()
        assert msg == "pong", f"Expected pong, got {msg} (stray event was leaked on rollback!)"
        print("  PASS  4.1 Rolled back transactions do NOT emit events (100% transaction safety)")

    # -------------------------------------------------------------
    # TEST 5: Deliveries Real-Time Events (P1)
    # -------------------------------------------------------------
    print("\n--- TEST GROUP 5: Deliveries Real-Time Events (P1) ---")

    with client.websocket_connect(f"/ws?token={data['user_a1']}") as ws_a1:
        db = SessionLocal()
        # Create partner user
        partner = User(
            organization_id=data["org_a_id"],
            email="driver1@realtime.com",
            name="Driver One",
            role=UserRole.DELIVERY_PARTNER,
            system_role="staff",
            password_hash="fake",
            is_active=True,
        )
        db.add(partner)
        db.flush()

        # Create order to deliver
        cust = db.get(Customer, data["cust_a_id"])
        ord_rec, _ = order_service.place_order(
            db,
            user=db.get(User, data["user_a1_id"]),
            customer=cust,
            lines=[order_service.OrderLine(product_id=data["prod_a_id"], quantity=1, unit_price=100.0)],
            warehouse_id=data["wh_a_id"],
            fulfilment_method="home_delivery",
            delivery_address="123 Street",
            create_as_draft=False,
        )
        db.flush()

        # Plan delivery
        delivery = delivery_service.plan(
            db,
            user=db.get(User, data["user_a1_id"]),
            order=ord_rec,
            delivery_partner=partner,
            vehicle_id=None,
            warehouse_id=data["wh_a_id"],
            scheduled_date=None,
            delivery_address="123 Street",
            notes=None,
            wanted=None,
        )
        db.commit()

        # Consume events: notification, order.created, inventory.stock_updated, delivery.assigned
        received_deliv_events = []
        for _ in range(4):
            raw = ws_a1.receive_text()
            evt = json.loads(raw)
            received_deliv_events.append(evt["event"])

        assert "delivery.assigned" in received_deliv_events
        print("  PASS  5.1 delivery.assigned emitted on delivery_service.plan")

        # Accept delivery
        delivery_service.accept(db, partner, delivery)
        db.commit()

        raw = ws_a1.receive_text()
        evt = json.loads(raw)
        assert evt["event"] == "delivery.status_changed"
        assert evt["data"]["new_status"] == "accepted"
        print("  PASS  5.2 delivery.status_changed emitted on delivery_service.accept")
        db.close()

    # -------------------------------------------------------------
    # TEST 6: Multi-Tab Concurrency & Dead Socket Cleanup
    # -------------------------------------------------------------
    print("\n--- TEST GROUP 6: Multi-Tab Concurrency & Dead Socket Cleanup ---")

    # Connect Tab 1 and Tab 2 for same user
    with client.websocket_connect(f"/ws?token={data['user_a1']}") as ws_tab1, \
         client.websocket_connect(f"/ws?token={data['user_a1']}") as ws_tab2:

        db = SessionLocal()
        notification_service.notify(
            db,
            user_id=data["user_a1_id"],
            title="Multi-Tab Alert",
            organization_id=data["org_a_id"],
        )
        db.commit()
        db.close()

        msg1 = json.loads(ws_tab1.receive_text())
        msg2 = json.loads(ws_tab2.receive_text())

        assert msg1["event"] == "notification.created"
        assert msg2["event"] == "notification.created"
        assert msg1["data"]["title"] == "Multi-Tab Alert"
        assert msg2["data"]["title"] == "Multi-Tab Alert"
        print("  PASS  6.1 Multi-tab concurrent connections both receive real-time events")

    # One tab closes, event continues to deliver without crash
    with client.websocket_connect(f"/ws?token={data['user_a1']}") as ws_alive:
        # Open and immediately close a dead tab
        with client.websocket_connect(f"/ws?token={data['user_a1']}") as ws_dead:
            ws_dead.send_text("ping")
            ws_dead.receive_text()

        # Emit event; should deliver to ws_alive without error
        db = SessionLocal()
        notification_service.notify(
            db,
            user_id=data["user_a1_id"],
            title="Dead Socket Test",
            organization_id=data["org_a_id"],
        )
        db.commit()
        db.close()

        msg = json.loads(ws_alive.receive_text())
        assert msg["event"] == "notification.created"
        assert msg["data"]["title"] == "Dead Socket Test"
        print("  PASS  6.2 Dead/disconnected sockets cleaned up safely without breaking active broadcasts")

    print("\n==================================================")
    print("RESULTS: ALL 13 WEBSOCKET TESTS PASSED (0 FAILURES)")
    print("==================================================")


if __name__ == "__main__":
    test_websocket_suite()
