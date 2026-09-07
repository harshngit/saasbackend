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


def test_warehouse_final_verification():
    from app.core.database import Base, auto_add_missing_columns, engine
    Base.metadata.create_all(bind=engine)
    auto_add_missing_columns()

    db = SessionLocal()
    try:
        # Create test organization 1
        org1 = Organization(name="Warehouse Org 1")
        db.add(org1)

        # Create test organization 2 (for tenant isolation checks)
        org2 = Organization(name="Warehouse Org 2")
        db.add(org2)
        db.flush()

        # Create admin user for Org 1
        admin1 = User(
            organization_id=org1.id,
            email="admin1@whverification.com",
            name="WH Admin 1",
            password_hash="testpass",
            role="ADMIN",
            is_active=True,
        )
        db.add(admin1)

        # Create admin user for Org 2
        admin2 = User(
            organization_id=org2.id,
            email="admin2@whverification.com",
            name="WH Admin 2",
            password_hash="testpass",
            role="ADMIN",
            is_active=True,
        )
        db.add(admin2)

        # Create delivery partner user for Org 1
        dp_user1 = User(
            organization_id=org1.id,
            email="dp1@whverification.com",
            name="WH DP 1",
            password_hash="testpass",
            role="DELIVERY_PARTNER",
            is_active=True,
        )
        db.add(dp_user1)

        # Create delivery partner 2 for Org 1
        dp_user2 = User(
            organization_id=org1.id,
            email="dp2@whverification.com",
            name="WH DP 2",
            password_hash="testpass",
            role="DELIVERY_PARTNER",
            is_active=True,
        )
        db.add(dp_user2)

        # Create warehouses for Org 1
        wh_A = Warehouse(
            organization_id=org1.id,
            name="Warehouse A (Default)",
            code="WH-A",
            is_active=True,
            is_default=True,
            state="Maharashtra",
            pincode="400001",
            country="India",
            contact_person="Alice A",
            email="a@wh.com",
            notes="Default warehouse A",
        )
        db.add(wh_A)

        wh_B = Warehouse(
            organization_id=org1.id,
            name="Warehouse B (Non-Default)",
            code="WH-B",
            is_active=True,
            is_default=False,
            state="Karnataka",
            pincode="560001",
            country="India",
            contact_person="Bob B",
            email="b@wh.com",
            notes="Secondary warehouse B",
        )
        db.add(wh_B)

        wh_inactive = Warehouse(
            organization_id=org1.id,
            name="Inactive Warehouse",
            code="WH-INA",
            is_active=False,
            is_default=False,
        )
        db.add(wh_inactive)

        # Warehouse for Org 2
        wh_org2 = Warehouse(
            organization_id=org2.id,
            name="Org 2 Warehouse",
            code="WH-ORG2",
            is_active=True,
            is_default=True,
        )
        db.add(wh_org2)

        # Create products
        prod1 = Product(
            organization_id=org1.id,
            name="Product 1",
            sku="PROD-01",
            total_inventory=0,
            is_active=True,
        )
        db.add(prod1)

        prod_org2 = Product(
            organization_id=org2.id,
            name="Org 2 Product",
            sku="PROD-ORG2",
            total_inventory=50,
            is_active=True,
        )
        db.add(prod_org2)
        db.commit()

        # Auth headers
        from app.core.security import create_access_token
        hdr1 = {"Authorization": f"Bearer {create_access_token(admin1.id, admin1.role, admin1.organization_id)}"}
        hdr2 = {"Authorization": f"Bearer {create_access_token(admin2.id, admin2.role, admin2.organization_id)}"}

        # Explicitly seed stock in Warehouse A (100) and Warehouse B (50)
        stock_service.adjust_on_hand(db, org1.id, wh_A.id, prod1.id, None, 100.0, "opening", note="Seed WH A")
        stock_service.adjust_on_hand(db, org1.id, wh_B.id, prod1.id, None, 50.0, "opening", note="Seed WH B")
        db.commit()

        print("\n==================================================")
        print("STARTING COMPREHENSIVE WAREHOUSE VERIFICATION SUITE")
        print("==================================================")

        print("\n--- TEST 1: WAREHOUSE MASTER EXTRA FIELDS ---")
        res = client.get(f"/warehouses/{wh_A.id}", headers=hdr1)
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["state"] == "Maharashtra"
        assert data["pincode"] == "400001"
        assert data["country"] == "India"
        assert data["contact_person"] == "Alice A"
        assert data["email"] == "a@wh.com"
        assert data["notes"] == "Default warehouse A"
        print("  [OK] All master fields (state, pincode, country, contact_person, email, notes) correctly persisted and returned")

        print("\n--- TEST 2: INACTIVE WAREHOUSE OPERATIONAL BLOCKING ---")
        # Stock adjustment on inactive warehouse -> 400
        res = client.post(
            f"/warehouses/{wh_inactive.id}/stock/adjust",
            json={"product_id": prod1.id, "movement_type": "adjustment", "quantity": 10},
            headers=hdr1,
        )
        assert res.status_code == 400
        assert "inactive" in res.json()["detail"].lower()
        print("  [OK] Manual stock adjustment on inactive warehouse blocked (400)")

        # Vehicle loading on inactive warehouse -> 400
        res = client.post(
            "/vehicle-stock/loading",
            json={
                "delivery_partner_id": dp_user1.id,
                "warehouse_id": wh_inactive.id,
                "items": [{"product_id": prod1.id, "loaded_qty": 5}],
            },
            headers=hdr1,
        )
        assert res.status_code == 400
        assert "inactive" in res.json()["detail"].lower()
        print("  [OK] Vehicle loading on inactive warehouse blocked (400)")

        # Historical stock read on inactive warehouse remains available -> 200
        res = client.get(f"/warehouses/stock?warehouse_id={wh_inactive.id}", headers=hdr1)
        assert res.status_code == 200
        print("  [OK] Historical stock read on inactive warehouse allowed (200)")

        print("\n--- TEST 3: MANDATORY MULTI-WAREHOUSE EOD RETURN SCENARIO ---")
        # wh_A is DEFAULT (on-hand: 100), wh_B is NON-DEFAULT (on-hand: 50)
        # Load from Warehouse B: 25 units
        load_b = client.post(
            "/vehicle-stock/loading",
            json={
                "delivery_partner_id": dp_user2.id,
                "warehouse_id": wh_B.id,
                "items": [{"product_id": prod1.id, "loaded_qty": 25}],
            },
            headers=hdr1,
        )
        assert load_b.status_code in (200, 201), load_b.text
        load_b_id = load_b.json()["id"]

        db.expire_all()
        on_hand_A_after_load = stock_service.on_hand(db, wh_A.id, prod1.id, None)
        on_hand_B_after_load = stock_service.on_hand(db, wh_B.id, prod1.id, None)
        assert on_hand_A_after_load == 100.0, f"WH A should remain 100.0, got {on_hand_A_after_load}"
        assert on_hand_B_after_load == 25.0, f"WH B should be 25.0, got {on_hand_B_after_load}"
        print("  [OK] Vehicle Loading deducted 25 from NON-DEFAULT Warehouse B (WH A: 100, WH B: 25)")

        # EOD Return: return 15 units
        eod_b = client.post(
            f"/vehicle-stock/{load_b_id}/end-of-day",
            json={"items": [{"product_id": prod1.id, "returned_qty": 15}]},
            headers=hdr1,
        )
        assert eod_b.status_code == 200, eod_b.text

        db.expire_all()
        on_hand_A_after_return = stock_service.on_hand(db, wh_A.id, prod1.id, None)
        on_hand_B_after_return = stock_service.on_hand(db, wh_B.id, prod1.id, None)
        assert on_hand_A_after_return == 100.0, f"WH A should remain 100.0, got {on_hand_A_after_return}"
        assert on_hand_B_after_return == 40.0, f"WH B should be restored to 40.0, got {on_hand_B_after_return}"
        print("  [OK] EOD Return restored 15 units to EXACT Warehouse B, NOT Default Warehouse A (WH A: 100, WH B: 40)")

        # Duplicate return blocked
        dup_eod = client.post(
            f"/vehicle-stock/{load_b_id}/end-of-day",
            json={"items": [{"product_id": prod1.id, "returned_qty": 15}]},
            headers=hdr1,
        )
        assert dup_eod.status_code == 400
        print("  [OK] Duplicate EOD Return blocked on closed session")

        # Return exceeding available stock blocked
        # Open new session to test return > available
        load_b2 = client.post(
            "/vehicle-stock/loading",
            json={
                "delivery_partner_id": dp_user1.id,
                "warehouse_id": wh_B.id,
                "items": [{"product_id": prod1.id, "loaded_qty": 10}],
            },
            headers=hdr1,
        )
        load_b2_id = load_b2.json()["id"]
        excess_eod = client.post(
            f"/vehicle-stock/{load_b2_id}/end-of-day",
            json={"items": [{"product_id": prod1.id, "returned_qty": 15}]},
            headers=hdr1,
        )
        assert excess_eod.status_code == 400
        assert "cannot exceed" in excess_eod.json()["detail"].lower()
        print("  [OK] Return quantity exceeding vehicle available stock blocked (400)")

        # Close load_b2 properly
        client.post(
            f"/vehicle-stock/{load_b2_id}/end-of-day",
            json={"items": [{"product_id": prod1.id, "returned_qty": 10}]},
            headers=hdr1,
        )

        print("\n--- TEST 4: WAREHOUSE MOVEMENT LEDGER & CANONICAL API ---")
        mov_res = client.get(f"/warehouses/{wh_B.id}/movements", headers=hdr1)
        assert mov_res.status_code == 200, mov_res.text
        movements = mov_res.json()
        assert len(movements) >= 4
        for m in movements:
            assert m["warehouse_id"] == wh_B.id
            assert "product_name" in m
        print(f"  [OK] GET /warehouses/{wh_B.id}/movements returned {len(movements)} warehouse-linked movements")

        # Cross-tenant movement audit access blocked -> 404
        cross_mov = client.get(f"/warehouses/{wh_B.id}/movements", headers=hdr2)
        assert cross_mov.status_code == 404
        print("  [OK] Cross-tenant movement audit query blocked (404)")

        print("\n--- TEST 5: WAREHOUSE TRANSFERS MVP WORKFLOW & GUARDS ---")
        # Same source/destination blocked -> 400
        same_trf = client.post(
            "/transfers",
            json={
                "source_warehouse_id": wh_A.id,
                "destination_warehouse_id": wh_A.id,
                "items": [{"product_id": prod1.id, "quantity": 5}],
            },
            headers=hdr1,
        )
        assert same_trf.status_code == 400
        print("  [OK] Transfer to same warehouse blocked (400)")

        # Cross-tenant transfer creation blocked -> 400
        cross_trf = client.post(
            "/transfers",
            json={
                "source_warehouse_id": wh_A.id,
                "destination_warehouse_id": wh_org2.id,
                "items": [{"product_id": prod1.id, "quantity": 5}],
            },
            headers=hdr1,
        )
        assert cross_trf.status_code == 400
        print("  [OK] Cross-organization transfer blocked (400)")

        # Create valid draft transfer (WH A -> WH B, 10 units)
        trf_res = client.post(
            "/transfers",
            json={
                "source_warehouse_id": wh_A.id,
                "destination_warehouse_id": wh_B.id,
                "items": [{"product_id": prod1.id, "quantity": 10}],
                "notes": "Verification transfer",
            },
            headers=hdr1,
        )
        assert trf_res.status_code == 201, trf_res.text
        trf_id = trf_res.json()["id"]

        # Verify zero stock movement during draft
        db.expire_all()
        assert stock_service.on_hand(db, wh_A.id, prod1.id, None) == 100.0
        assert stock_service.on_hand(db, wh_B.id, prod1.id, None) == 40.0
        print("  [OK] Draft transfer created (Status: draft, zero stock movement)")

        # Dispatch transfer (WH A -> WH B)
        disp_res = client.post(f"/transfers/{trf_id}/dispatch", headers=hdr1)
        assert disp_res.status_code == 200, disp_res.text
        assert disp_res.json()["status"] == "in_transit"

        # WH A on-hand: 100 - 10 = 90. WH B on-hand: unchanged at 40.
        db.expire_all()
        assert stock_service.on_hand(db, wh_A.id, prod1.id, None) == 90.0
        assert stock_service.on_hand(db, wh_B.id, prod1.id, None) == 40.0
        print("  [OK] Transfer dispatched (WH A: 90, WH B: 40, Status: in_transit)")

        # Duplicate dispatch blocked -> 400
        dup_disp = client.post(f"/transfers/{trf_id}/dispatch", headers=hdr1)
        assert dup_disp.status_code == 400
        print("  [OK] Duplicate dispatch blocked (400)")

        # Cancel in-transit transfer blocked -> 400
        cancel_in_transit = client.post(f"/transfers/{trf_id}/cancel", headers=hdr1)
        assert cancel_in_transit.status_code == 400
        print("  [OK] In-transit cancellation blocked (400)")

        # Receive transfer (WH A -> WH B)
        recv_res = client.post(f"/transfers/{trf_id}/receive", headers=hdr1)
        assert recv_res.status_code == 200, recv_res.text
        assert recv_res.json()["status"] == "received"

        # WH B on-hand: 40 + 10 = 50. WH A on-hand: 90.
        db.expire_all()
        assert stock_service.on_hand(db, wh_A.id, prod1.id, None) == 90.0
        assert stock_service.on_hand(db, wh_B.id, prod1.id, None) == 50.0
        print("  [OK] Transfer received (WH A: 90, WH B: 50, Status: received)")

        # Duplicate receive blocked -> 400
        dup_recv = client.post(f"/transfers/{trf_id}/receive", headers=hdr1)
        assert dup_recv.status_code == 400
        print("  [OK] Duplicate receive blocked (400)")

        # Cross-tenant dispatch/receive access blocked -> 404
        cross_read = client.get(f"/transfers/{trf_id}", headers=hdr2)
        assert cross_read.status_code == 404
        print("  [OK] Cross-tenant transfer lookup blocked (404)")

        print("\n==================================================")
        print("ALL FINAL WAREHOUSE GAP VERIFICATIONS PASSED SUCCESSFULLY!")
        print("==================================================")

    finally:
        db.close()


if __name__ == "__main__":
    test_warehouse_final_verification()
