import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.database import SessionLocal
from app.models import (
    Organization,
    Product,
    ProductVariant,
    Role,
    User,
    Warehouse,
    WarehouseStock,
    StockMovement,
    WarehouseTransfer,
    VehicleLoading,
)
from app.services import stock_service

client = TestClient(app)


def test_warehouse_gaps():
    from app.core.database import Base, auto_add_missing_columns, engine
    Base.metadata.create_all(bind=engine)
    auto_add_missing_columns()

    db = SessionLocal()
    try:
        # Create test organization
        org = Organization(name="Warehouse Gap Test Org")
        db.add(org)
        db.flush()

        # Create admin user
        admin = User(
            organization_id=org.id,
            email="admin@warehousegap.com",
            name="WH Gap Admin",
            password_hash="testpass",
            role="ADMIN",
            is_active=True,
        )
        db.add(admin)

        # Create delivery partner user
        dp_user = User(
            organization_id=org.id,
            email="dp@warehousegap.com",
            name="WH Gap DP",
            password_hash="testpass",
            role="DELIVERY_PARTNER",
            is_active=True,
        )
        db.add(dp_user)

        # Create warehouses
        wh_source = Warehouse(
            organization_id=org.id,
            name="Source Warehouse",
            code="WH-SRC",
            is_active=True,
            is_default=True,
            state="Maharashtra",
            pincode="400001",
            country="India",
            contact_person="John Source",
            email="src@wh.com",
            notes="Primary source warehouse",
        )
        db.add(wh_source)

        wh_dest = Warehouse(
            organization_id=org.id,
            name="Destination Warehouse",
            code="WH-DST",
            is_active=True,
            is_default=False,
            state="Maharashtra",
            pincode="400002",
            country="India",
            contact_person="Jane Dest",
            email="dst@wh.com",
            notes="Secondary dest warehouse",
        )
        db.add(wh_dest)

        wh_inactive = Warehouse(
            organization_id=org.id,
            name="Inactive Warehouse",
            code="WH-INA",
            is_active=False,
            is_default=False,
        )
        db.add(wh_inactive)

        # Create product
        product = Product(
            organization_id=org.id,
            name="Test Widget",
            sku="WIDGET-01",
            total_inventory=100,
            is_active=True,
        )
        db.add(product)
        db.commit()

        # Auth headers helper
        from app.core.security import create_access_token
        admin_token = create_access_token(admin.id, admin.role, admin.organization_id)
        headers = {"Authorization": f"Bearer {admin_token}"}

        # Initialize stock at Source Warehouse: 100 units (seeded from product.total_inventory)
        db.commit()

        print("\n--- 1. Testing Warehouse Master Fields ---")
        res = client.get(f"/warehouses/{wh_source.id}", headers=headers)
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["state"] == "Maharashtra"
        assert data["pincode"] == "400001"
        assert data["country"] == "India"
        assert data["contact_person"] == "John Source"
        assert data["email"] == "src@wh.com"
        assert data["notes"] == "Primary source warehouse"
        print("  [OK] Warehouse master extra fields verified successfully")

        print("\n--- 2. Testing Inactive Warehouse Operational Blocking ---")
        # Attempt stock adjustment on inactive warehouse
        res = client.post(
            f"/warehouses/{wh_inactive.id}/stock/adjust",
            json={"product_id": product.id, "movement_type": "adjustment", "quantity": 10},
            headers=headers,
        )
        assert res.status_code == 400
        assert "inactive" in res.json()["detail"].lower()
        print("  [OK] Inactive warehouse stock adjustment blocked")

        # Historical stock read on inactive warehouse remains available
        res = client.get(f"/warehouses/stock?warehouse_id={wh_inactive.id}", headers=headers)
        assert res.status_code == 200
        print("  [OK] Historical stock read on inactive warehouse remains readable")

        print("\n--- 3. Testing Vehicle Loading & EOD Return to Source Warehouse ---")
        # Vehicle loading from wh_source
        load_res = client.post(
            "/vehicle-stock/loading",
            json={
                "delivery_partner_id": dp_user.id,
                "warehouse_id": wh_source.id,
                "items": [{"product_id": product.id, "loaded_qty": 30}],
            },
            headers=headers,
        )
        assert load_res.status_code in (200, 201), load_res.text
        loading_id = load_res.json()["id"]

        # Check stock on hand at source warehouse after loading: 100 - 30 = 70
        db.expire_all()
        on_hand_after_load = stock_service.on_hand(db, wh_source.id, product.id, None)
        assert on_hand_after_load == 70.0, f"Expected 70.0 but got {on_hand_after_load}"
        print("  [OK] Vehicle Loading deducted 30 from source warehouse (On hand: 70)")

        # EOD Return: return 10 items to SAME source warehouse
        eod_res = client.post(
            f"/vehicle-stock/{loading_id}/end-of-day",
            json={"items": [{"product_id": product.id, "returned_qty": 10}]},
            headers=headers,
        )
        assert eod_res.status_code == 200, eod_res.text
        db.expire_all()
        on_hand_after_return = stock_service.on_hand(db, wh_source.id, product.id, None)
        assert on_hand_after_return == 80.0, f"Expected 80.0 but got {on_hand_after_return}"
        print("  [OK] EOD Return credited 10 back to correct source warehouse (On hand: 80)")

        # Duplicate EOD Return on closed session should fail
        dup_eod = client.post(
            f"/vehicle-stock/{loading_id}/end-of-day",
            json={"items": [{"product_id": product.id, "returned_qty": 10}]},
            headers=headers,
        )
        assert dup_eod.status_code == 400
        print("  [OK] Duplicate EOD Return blocked on closed session")

        print("\n--- 4. Testing Warehouse Stock Movement Audit Ledger & API ---")
        mov_res = client.get(f"/warehouses/{wh_source.id}/movements", headers=headers)
        assert mov_res.status_code == 200, mov_res.text
        movements = mov_res.json()
        assert len(movements) >= 2
        # Check all movements belong to wh_source
        for m in movements:
            assert m["warehouse_id"] == wh_source.id
        print(f"  [OK] Movement read API GET /warehouses/{wh_source.id}/movements returned {len(movements)} records")

        print("\n--- 5. Testing Warehouse Transfers (MVP Workflow) ---")
        # 5.1 Same source and destination blocked
        bad_trf = client.post(
            "/transfers",
            json={
                "source_warehouse_id": wh_source.id,
                "destination_warehouse_id": wh_source.id,
                "items": [{"product_id": product.id, "quantity": 5}],
            },
            headers=headers,
        )
        assert bad_trf.status_code == 400
        print("  [OK] Same source & destination transfer creation blocked")

        # 5.2 Create valid draft transfer (wh_source -> wh_dest, 20 units)
        create_res = client.post(
            "/transfers",
            json={
                "source_warehouse_id": wh_source.id,
                "destination_warehouse_id": wh_dest.id,
                "items": [{"product_id": product.id, "quantity": 20}],
                "notes": "Test stock transfer",
            },
            headers=headers,
        )
        assert create_res.status_code == 201, create_res.text
        trf_data = create_res.json()
        trf_id = trf_data["id"]
        assert trf_data["status"] == "draft"

        # Check stock movement during draft: zero movement
        db.expire_all()
        assert stock_service.on_hand(db, wh_source.id, product.id, None) == 80.0
        assert stock_service.on_hand(db, wh_dest.id, product.id, None) == 0.0
        print("  [OK] Draft transfer created (Status: draft, zero stock movement)")

        # 5.3 Dispatch transfer (wh_source -> wh_dest)
        dispatch_res = client.post(f"/transfers/{trf_id}/dispatch", headers=headers)
        assert dispatch_res.status_code == 200, dispatch_res.text
        assert dispatch_res.json()["status"] == "in_transit"

        # Source on_hand deducted by 20 -> 60. Dest on_hand unchanged -> 0.
        db.expire_all()
        assert stock_service.on_hand(db, wh_source.id, product.id, None) == 60.0
        assert stock_service.on_hand(db, wh_dest.id, product.id, None) == 0.0
        print("  [OK] Transfer dispatched (Source on-hand: 60, Dest on-hand: 0, Status: in_transit)")

        # Duplicate dispatch blocked
        dup_disp = client.post(f"/transfers/{trf_id}/dispatch", headers=headers)
        assert dup_disp.status_code == 400
        print("  [OK] Duplicate dispatch blocked")

        # In-transit cancellation blocked
        bad_cancel = client.post(f"/transfers/{trf_id}/cancel", headers=headers)
        assert bad_cancel.status_code == 400
        print("  [OK] In-transit cancellation blocked")

        # 5.4 Receive transfer (in_transit -> received)
        receive_res = client.post(f"/transfers/{trf_id}/receive", headers=headers)
        assert receive_res.status_code == 200, receive_res.text
        assert receive_res.json()["status"] == "received"

        # Dest on_hand increased by 20 -> 20. Source on_hand remains 60.
        db.expire_all()
        assert stock_service.on_hand(db, wh_source.id, product.id, None) == 60.0
        assert stock_service.on_hand(db, wh_dest.id, product.id, None) == 20.0
        print("  [OK] Transfer received (Dest on-hand: 20, Status: received)")

        # Duplicate receive blocked
        dup_recv = client.post(f"/transfers/{trf_id}/receive", headers=headers)
        assert dup_recv.status_code == 400
        print("  [OK] Duplicate receive blocked")

        # 5.5 Test draft cancellation
        draft2_res = client.post(
            "/transfers",
            json={
                "source_warehouse_id": wh_source.id,
                "destination_warehouse_id": wh_dest.id,
                "items": [{"product_id": product.id, "quantity": 5}],
            },
            headers=headers,
        )
        trf2_id = draft2_res.json()["id"]
        cancel_res = client.post(f"/transfers/{trf2_id}/cancel", headers=headers)
        assert cancel_res.status_code == 200
        assert cancel_res.json()["status"] == "cancelled"
        print("  [OK] Draft cancellation verified")

        print("\nALL WAREHOUSE GAP VERIFICATIONS PASSED SUCCESSFULLY!")

    finally:
        db.close()


if __name__ == "__main__":
    test_warehouse_gaps()
