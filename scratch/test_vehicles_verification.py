"""Comprehensive test script to verify all 12 edge cases for Vehicles Module verification."""
import sys
import os
import uuid
sys.path.insert(0, os.path.abspath("."))

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.core.database import Base, engine, get_db
from app.core.security import create_access_token
from app.models import User, Organization, Vehicle, VehicleLoading, Delivery, Warehouse, Product, SystemRole, UserRole, Role

client = TestClient(app)

def run_tests():
    suffix = uuid.uuid4().hex[:6]
    print(f"--- STARTING VEHICLE MODULE EDGE CASE VERIFICATION ({suffix}) ---")
    
    with Session(engine) as db:
        # Setup test org 1
        org1 = Organization(name=f"VehTest Org 1 {suffix}")
        db.add(org1)
        db.commit()
        db.refresh(org1)
            
        org2 = Organization(name=f"VehTest Org 2 {suffix}")
        db.add(org2)
        db.commit()
        db.refresh(org2)

        # Create test role with vehicle_stock permissions for staff
        veh_role = Role(
            organization_id=org1.id,
            name=f"VehRole_{suffix}",
            description="Vehicle test role",
            permissions={
                "vehicle_stock": {"view": True, "create": True, "edit": True, "delete": True},
                "deliveries": {"view": True, "create": True, "edit": True, "delete": True},
            }
        )
        db.add(veh_role)
        db.commit()
        db.refresh(veh_role)
            
        admin1 = User(
            organization_id=org1.id,
            email=f"admin1_{suffix}@vehtest.com",
            password_hash="dummy",
            name="Admin One",
            role=UserRole.ADMIN,
            system_role=SystemRole.ADMIN,
            is_active=True,
        )
        db.add(admin1)
        db.commit()
        db.refresh(admin1)

        dp1 = User(
            organization_id=org1.id,
            email=f"dp1_{suffix}@vehtest.com",
            password_hash="dummy",
            name="Driver One",
            role=UserRole.DELIVERY_PARTNER,
            role_id=veh_role.id,
            system_role=SystemRole.STAFF,
            is_active=True,
        )
        db.add(dp1)

        dp2 = User(
            organization_id=org1.id,
            email=f"dp2_{suffix}@vehtest.com",
            password_hash="dummy",
            name="Driver Two",
            role=UserRole.DELIVERY_PARTNER,
            role_id=veh_role.id,
            system_role=SystemRole.STAFF,
            is_active=True,
        )
        db.add(dp2)

        dp_org2 = User(
            organization_id=org2.id,
            email=f"dp_org2_{suffix}@vehtest.com",
            password_hash="dummy",
            name="Org2 Driver",
            role=UserRole.DELIVERY_PARTNER,
            role_id=veh_role.id,
            system_role=SystemRole.STAFF,
            is_active=True,
        )
        db.add(dp_org2)
        db.commit()
        db.refresh(dp1)
        db.refresh(dp2)
        db.refresh(dp_org2)

        admin1_token = create_access_token(admin1.id, admin1.effective_system_role, admin1.organization_id)
        headers1 = {"Authorization": f"Bearer {admin1_token}"}

        dp1_token = create_access_token(dp1.id, dp1.effective_system_role, dp1.organization_id)
        headers_dp1 = {"Authorization": f"Bearer {dp1_token}"}

        dp2_token = create_access_token(dp2.id, dp2.effective_system_role, dp2.organization_id)
        headers_dp2 = {"Authorization": f"Bearer {dp2_token}"}

        vnum = f"MH12_{suffix}"

        # Case 8: Duplicate vehicle number same organization -> block
        res = client.post("/vehicles", json={"vehicle_number": vnum, "vehicle_type": "Truck"}, headers=headers1)
        print("Case 8 Create Vehicle 1 Status:", res.status_code)
        
        res_dup = client.post("/vehicles", json={"vehicle_number": vnum, "vehicle_type": "Van"}, headers=headers1)
        print("Case 8 (Duplicate vehicle same org): Status =", res_dup.status_code, "Detail =", res_dup.json().get("detail"))

        # Case 9: Same vehicle number different organization -> tenant scoped behavior
        admin2 = User(
            organization_id=org2.id,
            email=f"admin2_{suffix}@vehtest.com",
            password_hash="dummy",
            name="Admin Two",
            role=UserRole.ADMIN,
            system_role=SystemRole.ADMIN,
            is_active=True,
        )
        db.add(admin2)
        db.commit()
        db.refresh(admin2)
        admin2_token = create_access_token(admin2.id, admin2.effective_system_role, admin2.organization_id)
        headers2 = {"Authorization": f"Bearer {admin2_token}"}

        res_diff_org = client.post("/vehicles", json={"vehicle_number": vnum, "vehicle_type": "Van"}, headers=headers2)
        print("Case 9 (Same vehicle diff org): Status =", res_diff_org.status_code)

        # Case 3: Assign DP from another organization -> must block
        v1_id = res.json()["id"]
        res_cross_driver = client.patch(f"/vehicles/{v1_id}", json={"default_driver_id": dp_org2.id}, headers=headers1)
        print("Case 3 (Assign DP from another org): Status =", res_cross_driver.status_code, "Detail =", res_cross_driver.json().get("detail"))

        # Case 2: Assign inactive vehicle / Assign inactive driver
        res_inactive_veh = client.post("/vehicles", json={"vehicle_number": f"INACT_{suffix}", "is_active": False}, headers=headers1)
        inact_v_id = res_inactive_veh.json()["id"]

        # Setup test product and warehouse stock
        p1 = Product(organization_id=org1.id, name="Test Product", sku=f"PROD-{suffix}", total_inventory=100, is_active=True)
        db.add(p1)
        db.commit()
        db.refresh(p1)

        wh = Warehouse(organization_id=org1.id, name="Test WH", code=f"WH-{suffix}", is_default=True, is_active=True)
        db.add(wh)
        db.commit()
        db.refresh(wh)

        # Case 10: Inactive vehicle used for new loading
        res_load_inact = client.post("/vehicle-stock/loading", json={
            "delivery_partner_id": dp1.id,
            "vehicle_id": inact_v_id,
            "warehouse_id": wh.id,
            "items": [{"product_id": p1.id, "loaded_qty": 5}]
        }, headers=headers1)
        print("Case 10 (Inactive vehicle used for new loading): Status =", res_load_inact.status_code, "Detail =", res_load_inact.json().get("detail"))

        # Case 1 & 11: Check if maintenance status exists in Vehicle model or API
        res_maint = client.patch(f"/vehicles/{v1_id}", json={"status": "maintenance"}, headers=headers1)
        print("Case 1 & 11 (Check Maintenance status in PATCH /vehicles): Status =", res_maint.status_code, "Response =", res_maint.json())

        # Case 4 & 5: Simultaneous assignments
        # Assign dp1 to v1
        client.patch(f"/vehicles/{v1_id}", json={"default_driver_id": dp1.id}, headers=headers1)
        # Create vehicle 2
        res_v2 = client.post("/vehicles", json={"vehicle_number": f"V2_{suffix}"}, headers=headers1)
        v2_id = res_v2.json()["id"]
        # Assign dp1 to v2 as well
        res_v2_dp1 = client.patch(f"/vehicles/{v2_id}", json={"default_driver_id": dp1.id}, headers=headers1)
        print("Case 5 (DP already assigned another vehicle): Status =", res_v2_dp1.status_code, "Allowed=", res_v2_dp1.status_code == 200)

        # Case 7: Delete vehicle with delivery history
        # Create delivery with v1_id
        d1 = Delivery(
            organization_id=org1.id,
            delivery_note_number=f"DN-{suffix}",
            vehicle_id=v1_id,
            delivery_partner_id=dp1.id,
            status="planned"
        )
        db.add(d1)
        db.commit()

        res_del = client.delete(f"/vehicles/{v1_id}", headers=headers1)
        print("Case 7 (Delete vehicle with delivery history): Status =", res_del.status_code, "Detail =", res_del.json().get("detail") if res_del.status_code != 204 else None)

        # Case 12: DP2 access DP1 loading session
        # Start session for DP1
        load_res = client.post("/vehicle-stock/loading", json={
            "delivery_partner_id": dp1.id,
            "vehicle_id": v1_id,
            "warehouse_id": wh.id,
            "items": [{"product_id": p1.id, "loaded_qty": 5}]
        }, headers=headers_dp1)
        loading_id = load_res.json().get("id") if load_res.status_code == 201 else None
        print("DP1 Session created: Status =", load_res.status_code, "Loading ID =", loading_id)

        # DP2 attempts to read DP1 current session
        res_dp2_read = client.get(f"/vehicle-stock/current/{dp1.id}", headers=headers_dp2)
        print("Case 12 (DP2 gets DP1 current stock): Status =", res_dp2_read.status_code, "Detail =", res_dp2_read.json().get("detail"))

if __name__ == "__main__":
    run_tests()
