"""The firm's delivery vehicles.

Gated by the `vehicle_stock` module permission, so a dispatch or warehouse role can
manage the fleet without being an Admin.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.models import Delivery, User, Vehicle
from app.schemas.vehicle import VehicleCreate, VehicleOut, VehicleUpdate

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


@router.get("", response_model=list[VehicleOut])
def list_vehicles(
    user: User = Depends(_view),
    is_active: bool | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[Vehicle]:
    query = db.query(Vehicle).filter(Vehicle.organization_id == _org_id(user))
    if is_active is not None:
        query = query.filter(Vehicle.is_active == is_active)
    return query.order_by(Vehicle.vehicle_number).all()


@router.post("", response_model=VehicleOut, status_code=status.HTTP_201_CREATED)
def create_vehicle(
    payload: VehicleCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Vehicle:
    org_id = _org_id(user)
    if _number_taken(db, org_id, payload.vehicle_number):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="A vehicle with this number already exists"
        )
    if payload.default_driver_id:
        driver = db.get(User, payload.default_driver_id)
        if driver is None or driver.organization_id != org_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="default_driver_id is not an employee in your firm",
            )
    vehicle = Vehicle(organization_id=org_id, **payload.model_dump())
    db.add(vehicle)
    db.commit()
    db.refresh(vehicle)
    return vehicle


@router.get("/{vehicle_id}", response_model=VehicleOut)
def get_vehicle(
    vehicle_id: str, user: User = Depends(_view), db: Session = Depends(get_db)
) -> Vehicle:
    return _owned(db, vehicle_id, _org_id(user))


@router.patch("/{vehicle_id}", response_model=VehicleOut)
def update_vehicle(
    vehicle_id: str,
    payload: VehicleUpdate,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Vehicle:
    org_id = _org_id(user)
    vehicle = _owned(db, vehicle_id, org_id)
    data = payload.model_dump(exclude_unset=True)
    if data.get("vehicle_number") and _number_taken(
        db, org_id, data["vehicle_number"], exclude_id=vehicle.id
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="A vehicle with this number already exists"
        )
    if data.get("default_driver_id"):
        driver = db.get(User, data["default_driver_id"])
        if driver is None or driver.organization_id != org_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="default_driver_id is not an employee in your firm",
            )
    for field, value in data.items():
        setattr(vehicle, field, value)
    db.commit()
    db.refresh(vehicle)
    return vehicle


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
