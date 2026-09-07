"""The firm's delivery vehicles.

Gated by the `vehicle_stock` module permission, so a dispatch or warehouse role can
manage the fleet without being an Admin.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.models import Delivery, User, Vehicle, VehicleAssignmentHistory, VehicleLoading
from app.schemas.vehicle import (
    DeliveryPartnerBrief,
    VehicleActivityOut,
    VehicleAssignmentHistoryOut,
    VehicleCreate,
    VehicleOut,
    VehicleUpdate,
)

router = APIRouter(prefix="/vehicles", tags=["vehicles"])

_view = require_permission("vehicle_stock", "view")
_create = require_permission("vehicle_stock", "create")
_edit = require_permission("vehicle_stock", "edit")
_delete = require_permission("vehicle_stock", "delete")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account"
        )
    return user.organization_id


def _owned(db: Session, vehicle_id: str, org_id: str) -> Vehicle:
    vehicle = db.get(Vehicle, vehicle_id)
    if vehicle is None or vehicle.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vehicle not found")
    return vehicle


def _number_taken(db: Session, org_id: str, number: str, exclude_id: str | None = None) -> bool:
    query = db.query(Vehicle).filter(
        Vehicle.organization_id == org_id, Vehicle.vehicle_number == number
    )
    if exclude_id is not None:
        query = query.filter(Vehicle.id != exclude_id)
    return db.query(query.exists()).scalar()


def _vehicle_out(vehicle: Vehicle) -> VehicleOut:
    out = VehicleOut.model_validate(vehicle)
    if vehicle.default_driver:
        out.assigned_delivery_partner = DeliveryPartnerBrief.model_validate(vehicle.default_driver)
    else:
        out.assigned_delivery_partner = None
    return out


@router.get("", response_model=list[VehicleOut])
def list_vehicles(
    user: User = Depends(_view),
    is_active: bool | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
) -> list[VehicleOut]:
    query = db.query(Vehicle).filter(Vehicle.organization_id == _org_id(user))
    if is_active is not None:
        query = query.filter(Vehicle.is_active == is_active)
    if status_filter:
        query = query.filter(Vehicle.status == status_filter)
    vehicles = query.order_by(Vehicle.vehicle_number).all()
    return [_vehicle_out(v) for v in vehicles]


@router.post("", response_model=VehicleOut, status_code=status.HTTP_201_CREATED)
def create_vehicle(
    payload: VehicleCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> VehicleOut:
    org_id = _org_id(user)
    if _number_taken(db, org_id, payload.vehicle_number):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="A vehicle with this number already exists"
        )

    # Compute status and is_active alignment
    vehicle_status = payload.status or ("active" if payload.is_active else "inactive")
    is_active = (vehicle_status == "active")

    if payload.default_driver_id:
        driver = db.get(User, payload.default_driver_id)
        if driver is None or driver.organization_id != org_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="default_driver_id is not an employee in your firm",
            )
        # Operational restriction: only active vehicles can be assigned a driver
        if vehicle_status != "active":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot assign a driver to a vehicle in '{vehicle_status}' status. Vehicle must be active.",
            )

    vehicle = Vehicle(
        organization_id=org_id,
        vehicle_number=payload.vehicle_number,
        vehicle_type=payload.vehicle_type,
        capacity_kg=payload.capacity_kg,
        default_driver_id=payload.default_driver_id,
        status=vehicle_status,
        is_active=is_active,
    )
    db.add(vehicle)
    db.flush()

    if vehicle.default_driver_id:
        assignment = VehicleAssignmentHistory(
            organization_id=org_id,
            vehicle_id=vehicle.id,
            delivery_partner_id=vehicle.default_driver_id,
            assigned_by_id=user.id,
            assigned_at=datetime.now(timezone.utc),
        )
        db.add(assignment)

    db.commit()
    db.refresh(vehicle)
    return _vehicle_out(vehicle)


@router.get("/{vehicle_id}", response_model=VehicleOut)
def get_vehicle(
    vehicle_id: str, user: User = Depends(_view), db: Session = Depends(get_db)
) -> VehicleOut:
    vehicle = _owned(db, vehicle_id, _org_id(user))
    return _vehicle_out(vehicle)


@router.patch("/{vehicle_id}", response_model=VehicleOut)
def update_vehicle(
    vehicle_id: str,
    payload: VehicleUpdate,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> VehicleOut:
    org_id = _org_id(user)
    vehicle = _owned(db, vehicle_id, org_id)
    data = payload.model_dump(exclude_unset=True)

    if data.get("vehicle_number") and _number_taken(
        db, org_id, data["vehicle_number"], exclude_id=vehicle.id
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="A vehicle with this number already exists"
        )

    # Resolve new status and is_active alignment
    new_status = vehicle.status
    if "status" in data and data["status"] is not None:
        new_status = data["status"]
    elif "is_active" in data and data["is_active"] is not None:
        new_status = "active" if data["is_active"] else "inactive"

    new_is_active = (new_status == "active")

    # Safety check: block status change to maintenance/inactive if an active loading session exists
    if new_status in ("maintenance", "inactive") and vehicle.status == "active":
        active_loading = (
            db.query(VehicleLoading)
            .filter(
                VehicleLoading.vehicle_id == vehicle.id,
                VehicleLoading.organization_id == org_id,
                VehicleLoading.status == "active",
            )
            .first()
        )
        if active_loading:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot set vehicle to '{new_status}' status while it has an active loading session. Complete EOD return first.",
            )

    # Driver assignment handling
    driver_changed = False
    old_driver_id = vehicle.default_driver_id
    new_driver_id = old_driver_id

    if "default_driver_id" in data:
        new_driver_id = data["default_driver_id"]
        if new_driver_id is not None:
            driver = db.get(User, new_driver_id)
            if driver is None or driver.organization_id != org_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="default_driver_id is not an employee in your firm",
                )
            # Operational restriction: only active vehicles can be assigned a driver
            if new_status != "active":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Cannot assign a driver to a vehicle in '{new_status}' status. Vehicle must be active.",
                )
        if new_driver_id != old_driver_id:
            driver_changed = True

    # Update model attributes
    for field, value in data.items():
        setattr(vehicle, field, value)

    vehicle.status = new_status
    vehicle.is_active = new_is_active

    # Record assignment history if driver changed
    if driver_changed:
        now = datetime.now(timezone.utc)
        # Close existing active assignment
        open_assignments = (
            db.query(VehicleAssignmentHistory)
            .filter(
                VehicleAssignmentHistory.vehicle_id == vehicle.id,
                VehicleAssignmentHistory.organization_id == org_id,
                VehicleAssignmentHistory.unassigned_at.is_(None),
            )
            .all()
        )
        for open_a in open_assignments:
            open_a.unassigned_at = now

        # Create new active assignment if driver is set
        if new_driver_id:
            db.add(
                VehicleAssignmentHistory(
                    organization_id=org_id,
                    vehicle_id=vehicle.id,
                    delivery_partner_id=new_driver_id,
                    assigned_by_id=user.id,
                    assigned_at=now,
                )
            )

    db.commit()
    db.refresh(vehicle)
    return _vehicle_out(vehicle)


@router.get("/{vehicle_id}/assignments", response_model=list[VehicleAssignmentHistoryOut])
def list_vehicle_assignments(
    vehicle_id: str,
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> list[VehicleAssignmentHistoryOut]:
    """Retrieve historical driver assignments for a vehicle."""
    org_id = _org_id(user)
    vehicle = _owned(db, vehicle_id, org_id)

    history = (
        db.query(VehicleAssignmentHistory)
        .filter(
            VehicleAssignmentHistory.vehicle_id == vehicle.id,
            VehicleAssignmentHistory.organization_id == org_id,
        )
        .order_by(VehicleAssignmentHistory.assigned_at.desc())
        .all()
    )

    results = []
    for h in history:
        out = VehicleAssignmentHistoryOut.model_validate(h)
        if h.delivery_partner:
            out.assigned_delivery_partner = DeliveryPartnerBrief.model_validate(h.delivery_partner)
        else:
            out.assigned_delivery_partner = None
        results.append(out)
    return results


@router.get("/{vehicle_id}/activity", response_model=list[VehicleActivityOut])
def get_vehicle_activity(
    vehicle_id: str,
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> list[VehicleActivityOut]:
    """Lightweight aggregation API for Vehicle Detail Activity timeline."""
    org_id = _org_id(user)
    vehicle = _owned(db, vehicle_id, org_id)
    activities = []

    # 1. Driver Assignments / Unassignments
    assignments = (
        db.query(VehicleAssignmentHistory)
        .filter(
            VehicleAssignmentHistory.vehicle_id == vehicle.id,
            VehicleAssignmentHistory.organization_id == org_id,
        )
        .all()
    )
    for a in assignments:
        driver_name = a.delivery_partner.name if a.delivery_partner else "Driver"
        activities.append(
            VehicleActivityOut(
                type="vehicle_assigned",
                timestamp=a.assigned_at,
                description=f"Assigned driver: {driver_name}",
                reference_type="user",
                reference_id=a.delivery_partner_id,
            )
        )
        if a.unassigned_at:
            activities.append(
                VehicleActivityOut(
                    type="vehicle_unassigned",
                    timestamp=a.unassigned_at,
                    description=f"Unassigned driver: {driver_name}",
                    reference_type="user",
                    reference_id=a.delivery_partner_id,
                )
            )

    # 2. Vehicle Loading Sessions
    loadings = (
        db.query(VehicleLoading)
        .filter(
            VehicleLoading.vehicle_id == vehicle.id,
            VehicleLoading.organization_id == org_id,
        )
        .all()
    )
    for l in loadings:
        dp_name = l.delivery_partner.name if l.delivery_partner else "Partner"
        activities.append(
            VehicleActivityOut(
                type="vehicle_loaded",
                timestamp=l.date or l.created_at,
                description=f"Vehicle loading session ({l.status}) for {dp_name}",
                reference_type="vehicle_loading",
                reference_id=l.id,
            )
        )

    # 3. Linked Deliveries
    deliveries = (
        db.query(Delivery)
        .filter(
            Delivery.vehicle_id == vehicle.id,
            Delivery.organization_id == org_id,
        )
        .all()
    )
    for d in deliveries:
        activities.append(
            VehicleActivityOut(
                type="delivery_linked",
                timestamp=d.created_at,
                description=f"Delivery {d.delivery_note_number} linked ({d.status})",
                reference_type="delivery",
                reference_id=d.id,
            )
        )

    activities.sort(key=lambda x: x.timestamp, reverse=True)
    return activities


@router.delete("/{vehicle_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_vehicle(
    vehicle_id: str,
    user: User = Depends(_delete),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> None:
    """Remove a vehicle. Refused while a delivery still names it — a challan already
    records that this van carried the goods. Deactivate it instead."""
    org_id = _org_id(user)
    vehicle = _owned(db, vehicle_id, org_id)
    used = db.query(Delivery).filter(Delivery.vehicle_id == vehicle.id).count()
    if used:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{used} delivery(s) name this vehicle. Set is_active false instead.",
        )
    db.delete(vehicle)
    db.commit()
