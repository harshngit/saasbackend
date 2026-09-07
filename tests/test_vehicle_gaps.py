"""Regression test suite for Vehicle Module Gap implementation."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.core.database import Base, auto_add_missing_columns, engine
from app.core.security import create_access_token
from app.models import User, Organization, Vehicle, VehicleLoading, Delivery, Warehouse, Product, SystemRole, UserRole, Role, StockMovement, VehicleAssignmentHistory

client = TestClient(app)


def test_vehicle_gap_implementation():
    Base.metadata.create_all(bind=engine)
    auto_add_missing_columns()

    with Session(engine) as db:
        # 1. Setup Test Organizations & Roles
        org = Organization(name="VehGap Test Org")
        db.add(org)
        org2 = Organization(name="VehGap Org 2")
        db.add(org2)
        db.commit()
        db.refresh(org)
        db.refresh(org2)

        veh_role = Role(
            organization_id=org.id,
            name="VehGap Admin Role",
            permissions={
                "vehicle_stock": {"view": True, "create": True, "edit": True, "delete": True},
                "deliveries": {"view": True, "create": True, "edit": True, "delete": True},
                "inventory": {"view": True, "create": True, "edit": True, "delete": True},
            }
        )
        db.add(veh_role)
        db.commit()
        db.refresh(veh_role)

        admin = User(
            organization_id=org.id,
            email="admin_vehgap@test.com",
            password_hash="dummy",
            name="Admin VehGap",
            role=UserRole.ADMIN,
            system_role=SystemRole.ADMIN,
            is_active=True,
        )
        db.add(admin)

        dp1 = User(
            organization_id=org.id,
            email="dp1_vehgap@test.com",
            password_hash="dummy",
            name="Driver One",
            role=UserRole.DELIVERY_PARTNER,
            role_id=veh_role.id,
            system_role=SystemRole.STAFF,
            is_active=True,
        )
        db.add(dp1)

        dp2 = User(
            organization_id=org.id,
            email="dp2_vehgap@test.com",
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
            email="dp_org2_vehgap@test.com",
            password_hash="dummy",
            name="Org2 Driver",
            role=UserRole.DELIVERY_PARTNER,
            role_id=veh_role.id,
            system_role=SystemRole.STAFF,
            is_active=True,
        )
        db.add(dp_org2)
        db.commit()

        token = create_access_token(admin.id, admin.effective_system_role, admin.organization_id)
        headers = {"Authorization": f"Bearer {token}"}

        # 2. Test Invalid Status Payload Validation
        res_invalid = client.post("/vehicles", json={
            "vehicle_number": "MH12 INVALID",
            "status": "broken"
        }, headers=headers)
        assert res_invalid.status_code == 422, "Invalid vehicle status must be rejected"

        # 3. Test Canonical Statuses (Active, Inactive, Maintenance)
        res_active = client.post("/vehicles", json={
            "vehicle_number": "MH12 ACTIVE",
            "status": "active"
        }, headers=headers)
        assert res_active.status_code == 201
        v_active = res_active.json()
        assert v_active["status"] == "active"
        assert v_active["is_active"] is True

        res_inact = client.post("/vehicles", json={
            "vehicle_number": "MH12 INACTIVE",
            "status": "inactive"
        }, headers=headers)
        assert res_inact.status_code == 201
        v_inact = res_inact.json()
        assert v_inact["status"] == "inactive"
        assert v_inact["is_active"] is False

        res_maint = client.post("/vehicles", json={
            "vehicle_number": "MH12 MAINT",
            "status": "maintenance"
        }, headers=headers)
        assert res_maint.status_code == 201
        v_maint = res_maint.json()
        assert v_maint["status"] == "maintenance"
        assert v_maint["is_active"] is False

        # 4. Test Operational Driver Assignment Rules
        # A. Assign active vehicle -> Allowed
        res_assign_active = client.patch(f"/vehicles/{v_active['id']}", json={
            "default_driver_id": dp1.id
        }, headers=headers)
        assert res_assign_active.status_code == 200
        assert res_assign_active.json()["assigned_delivery_partner"]["id"] == dp1.id

        # B. Assign inactive vehicle -> Blocked
        res_assign_inact = client.patch(f"/vehicles/{v_inact['id']}", json={
            "default_driver_id": dp1.id
        }, headers=headers)
        assert res_assign_inact.status_code == 400
        assert "must be active" in res_assign_inact.json()["detail"].lower()

        # C. Assign maintenance vehicle -> Blocked
        res_assign_maint = client.patch(f"/vehicles/{v_maint['id']}", json={
            "default_driver_id": dp1.id
        }, headers=headers)
        assert res_assign_maint.status_code == 400
        assert "must be active" in res_assign_maint.json()["detail"].lower()

        # D. Cross-org driver assignment -> Blocked
        res_assign_cross = client.patch(f"/vehicles/{v_active['id']}", json={
            "default_driver_id": dp_org2.id
        }, headers=headers)
        assert res_assign_cross.status_code == 400

        # 5. Test Driver Reassignment and History Persistence
        # Reassign v_active from dp1 to dp2
        res_reassign = client.patch(f"/vehicles/{v_active['id']}", json={
            "default_driver_id": dp2.id
        }, headers=headers)
        assert res_reassign.status_code == 200

        # Unassign driver from v_active
        res_unassign = client.patch(f"/vehicles/{v_active['id']}", json={
            "default_driver_id": None
        }, headers=headers)
        assert res_unassign.status_code == 200
        assert res_unassign.json()["assigned_delivery_partner"] is None

        # Fetch Assignment History API
        res_history = client.get(f"/vehicles/{v_active['id']}/assignments", headers=headers)
        assert res_history.status_code == 200
        history_items = res_history.json()
        assert len(history_items) == 2, "Previous assignments must be preserved in history"
        assert history_items[0]["delivery_partner_id"] == dp2.id
        assert history_items[0]["unassigned_at"] is not None
        assert history_items[1]["delivery_partner_id"] == dp1.id
        assert history_items[1]["unassigned_at"] is not None

        # 6. Test Vehicle Loading Operational Safety (P0 Gap)
        # Create product & warehouse
        wh = Warehouse(organization_id=org.id, name="VehGap WH", code="WH-VG1", is_default=True, is_active=True)
        db.add(wh)
        p = Product(organization_id=org.id, name="VehGap Product", sku="PROD-VG1", total_inventory=100, is_active=True)
        db.add(p)
        db.commit()

        initial_movements_count = db.query(StockMovement).count()

        # Attempt loading with inactive vehicle -> Must be blocked
        res_load_inact = client.post("/vehicle-stock/loading", json={
            "delivery_partner_id": dp1.id,
            "vehicle_id": v_inact["id"],
            "warehouse_id": wh.id,
            "items": [{"product_id": p.id, "loaded_qty": 10}]
        }, headers=headers)
        assert res_load_inact.status_code == 400
        assert "cannot be used for new vehicle loading" in res_load_inact.json()["detail"]

        # Attempt loading with maintenance vehicle -> Must be blocked
        res_load_maint = client.post("/vehicle-stock/loading", json={
            "delivery_partner_id": dp1.id,
            "vehicle_id": v_maint["id"],
            "warehouse_id": wh.id,
            "items": [{"product_id": p.id, "loaded_qty": 10}]
        }, headers=headers)
        assert res_load_maint.status_code == 400
        assert "cannot be used for new vehicle loading" in res_load_maint.json()["detail"]

        # Verify NO stock movement or loading session occurred
        final_movements_count = db.query(StockMovement).count()
        assert final_movements_count == initial_movements_count, "No stock movement must occur on blocked loading"

        # 7. Test Active Loading Session Safety
        # Re-assign dp1 to v_active
        client.patch(f"/vehicles/{v_active['id']}", json={"default_driver_id": dp1.id}, headers=headers)

        # Start valid loading session with active vehicle
        res_valid_load = client.post("/vehicle-stock/loading", json={
            "delivery_partner_id": dp1.id,
            "vehicle_id": v_active["id"],
            "warehouse_id": wh.id,
            "items": [{"product_id": p.id, "loaded_qty": 5}]
        }, headers=headers)
        assert res_valid_load.status_code == 201

        # Attempt to set v_active to maintenance while session is active -> Must be blocked
        res_maint_active_session = client.patch(f"/vehicles/{v_active['id']}", json={
            "status": "maintenance"
        }, headers=headers)
        assert res_maint_active_session.status_code == 400
        assert "active loading session" in res_maint_active_session.json()["detail"]

        # 8. Test Vehicle Activity Timeline API
        res_activity = client.get(f"/vehicles/{v_active['id']}/activity", headers=headers)
        assert res_activity.status_code == 200
        activities = res_activity.json()
        assert len(activities) >= 3, "Activity endpoint must aggregate assignments and loadings"
        activity_types = {act["type"] for act in activities}
        assert "vehicle_assigned" in activity_types
        assert "vehicle_loaded" in activity_types

        # 9. Test Cross-Tenant Isolation on History & Activity
        token_org2 = create_access_token(dp_org2.id, dp_org2.effective_system_role, dp_org2.organization_id)
        headers2 = {"Authorization": f"Bearer {token_org2}"}

        res_cross_hist = client.get(f"/vehicles/{v_active['id']}/assignments", headers=headers2)
        assert res_cross_hist.status_code == 404, "Cross-tenant history access must return 404"

        res_cross_act = client.get(f"/vehicles/{v_active['id']}/activity", headers=headers2)
        assert res_cross_act.status_code == 404, "Cross-tenant activity access must return 404"

    print("--- ALL VEHICLE GAP REGRESSION TESTS PASSED CLEANLY ---")
