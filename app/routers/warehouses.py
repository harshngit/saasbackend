"""Warehouses and their stock positions.

Gated by the `inventory` module permission, so a firm can let a warehouse or
dispatch role manage stock without making them an Admin.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.models import Product, ProductVariant, StockReservation, User, Warehouse, WarehouseStock
from app.schemas.warehouse import (
    StockAdjustment,
    StockRow,
    WarehouseCreate,
    WarehouseOut,
    WarehouseUpdate,
)
from app.services import stock_service

router = APIRouter(prefix="/warehouses", tags=["warehouses"])

_view = require_permission("inventory", "view")
_create = require_permission("inventory", "create")
_edit = require_permission("inventory", "edit")
_delete = require_permission("inventory", "delete")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account"
        )
    return user.organization_id


def _owned(db: Session, warehouse_id: str, org_id: str) -> Warehouse:
    warehouse = db.get(Warehouse, warehouse_id)
    if warehouse is None or warehouse.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Warehouse not found")
    return warehouse


def _next_code(db: Session, org_id: str) -> str:
    used = {
        code for (code,) in db.query(Warehouse.code).filter(Warehouse.organization_id == org_id)
    }
    number = len(used) + 1
    while f"WH-{number:03d}" in used:
        number += 1
    return f"WH-{number:03d}"


def _clear_other_defaults(db: Session, org_id: str, keep_id: str | None) -> None:
    """Exactly one warehouse per firm is the default."""
    for other in db.query(Warehouse).filter(
        Warehouse.organization_id == org_id, Warehouse.is_default.is_(True)
    ):
        if other.id != keep_id:
            other.is_default = False


@router.get("", response_model=list[WarehouseOut])
def list_warehouses(
    user: User = Depends(_view),
    is_active: bool | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[Warehouse]:
    """This firm's warehouses. The default one is created on first read, so the list
    is never empty and every stock figure has somewhere to live."""
    org_id = _org_id(user)
    stock_service.default_warehouse(db, org_id)
    db.commit()
    query = db.query(Warehouse).filter(Warehouse.organization_id == org_id)
    if is_active is not None:
        query = query.filter(Warehouse.is_active == is_active)
    return query.order_by(Warehouse.is_default.desc(), Warehouse.code).all()


@router.post("", response_model=WarehouseOut, status_code=status.HTTP_201_CREATED)
def create_warehouse(
    payload: WarehouseCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Warehouse:
    org_id = _org_id(user)
    code = payload.code or _next_code(db, org_id)
    if db.query(Warehouse).filter(
        Warehouse.organization_id == org_id, Warehouse.code == code
    ).first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="A warehouse with this code already exists"
        )
    warehouse = Warehouse(
        organization_id=org_id,
        **payload.model_dump(exclude={"code"}),
        code=code,
    )
    db.add(warehouse)
    db.flush()
    if payload.is_default:
        _clear_other_defaults(db, org_id, warehouse.id)
    db.commit()
    db.refresh(warehouse)
    return warehouse


@router.get("/stock", response_model=list[StockRow])
def stock_positions(
    user: User = Depends(_view),
    warehouse_id: str | None = Query(default=None, description="Defaults to the firm's default warehouse"),
    product_id: str | None = Query(default=None),
    low_stock_only: bool = Query(default=False, description="Only items at or below their minimum level"),
    db: Session = Depends(get_db),
) -> list[StockRow]:
    """On hand, reserved and available for every item in a warehouse.

    `available = on_hand − outstanding reservations` — that is the figure an order is
    validated against, and the reason placing an order never moves physical stock.
    """
    org_id = _org_id(user)
    warehouse = stock_service.owned_warehouse(db, warehouse_id, org_id)
    if warehouse is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Warehouse not found")
    db.commit()

    # Walk the catalog, not the stock table: an item nobody has tracked per warehouse
    # yet still has stock on its product record, and leaving it out would show an
    # empty stock screen to a firm that has never opened a warehouse.
    products = db.query(Product).filter(Product.organization_id == org_id)
    if product_id:
        products = products.filter(Product.id == product_id)

    rows = []
    for product in products.all():
        items: list[tuple[str | None, str | None]] = [
            (variant.id, variant.name) for variant in (product.variations or [])
        ] or [(None, None)]
        for variant_id, variant_name in items:
            summary = stock_service.stock_summary(db, warehouse.id, product.id, variant_id)
            minimum = product.minimum_stock_level
            if low_stock_only and not (minimum and summary["available"] <= minimum):
                continue
            rows.append(
                StockRow(
                    warehouse_id=warehouse.id,
                    warehouse_name=warehouse.name,
                    product_id=product.id,
                    product_name=product.name,
                    variant_id=variant_id,
                    variant_name=variant_name,
                    minimum_stock_level=minimum,
                    **{k: summary[k] for k in ("on_hand", "reserved", "available")},
                )
            )
    rows.sort(key=lambda r: (r.product_name or "", r.variant_name or ""))
    return rows


@router.get("/{warehouse_id}", response_model=WarehouseOut)
def get_warehouse(
    warehouse_id: str, user: User = Depends(_view), db: Session = Depends(get_db)
) -> Warehouse:
    return _owned(db, warehouse_id, _org_id(user))


@router.patch("/{warehouse_id}", response_model=WarehouseOut)
def update_warehouse(
    warehouse_id: str,
    payload: WarehouseUpdate,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Warehouse:
    org_id = _org_id(user)
    warehouse = _owned(db, warehouse_id, org_id)
    data = payload.model_dump(exclude_unset=True)
    if data.get("code") and data["code"] != warehouse.code:
        if db.query(Warehouse).filter(
            Warehouse.organization_id == org_id,
            Warehouse.code == data["code"],
            Warehouse.id != warehouse.id,
        ).first():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A warehouse with this code already exists",
            )
    for field, value in data.items():
        setattr(warehouse, field, value)
    if data.get("is_default"):
        _clear_other_defaults(db, org_id, warehouse.id)
    db.commit()
    db.refresh(warehouse)
    return warehouse


@router.post("/{warehouse_id}/stock/adjust", response_model=StockRow)
def adjust_stock(
    warehouse_id: str,
    payload: StockAdjustment,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> StockRow:
    """Correct a warehouse's physical stock — a stock take, a write-off, an opening
    balance. Recorded in the stock ledger like any other movement.

    Reservations are untouched: a correction says what is on the shelf, not what has
    been promised. Taking stock below what is already reserved is refused, since
    those units are committed to orders.
    """
    org_id = _org_id(user)
    warehouse = _owned(db, warehouse_id, org_id)
    product = db.get(Product, payload.product_id)
    if product is None or product.organization_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="product_id is not a product in your firm"
        )
    if payload.variant_id:
        variant = db.get(ProductVariant, payload.variant_id)
        if variant is None or variant.product_id != product.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="variant_id is not a variant of that product"
            )

    current = stock_service.on_hand(db, warehouse.id, payload.product_id, payload.variant_id)
    held = stock_service.reserved(db, warehouse.id, payload.product_id, payload.variant_id)
    if current + payload.quantity < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"That would take stock below zero (on hand {current})",
        )
    if current + payload.quantity < held:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{held} unit(s) are reserved for open orders — cannot reduce below that",
        )

    stock_service.adjust_on_hand(
        db, org_id, warehouse.id, payload.product_id, payload.variant_id,
        payload.quantity, payload.movement_type,
        note=payload.note or f"Manual {payload.movement_type}", created_by=user.id,
    )
    db.commit()
    summary = stock_service.stock_summary(db, warehouse.id, payload.product_id, payload.variant_id)
    variant = db.get(ProductVariant, payload.variant_id) if payload.variant_id else None
    return StockRow(
        warehouse_id=warehouse.id,
        warehouse_name=warehouse.name,
        product_id=payload.product_id,
        product_name=product.name,
        variant_id=payload.variant_id,
        variant_name=variant.name if variant else None,
        minimum_stock_level=product.minimum_stock_level,
        **{k: summary[k] for k in ("on_hand", "reserved", "available")},
    )


@router.delete("/{warehouse_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_warehouse(
    warehouse_id: str,
    user: User = Depends(_delete),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> None:
    """Remove a warehouse. Refused while it still holds stock or has open
    reservations, and the firm's default cannot be deleted — deleting either would
    silently lose a stock position."""
    org_id = _org_id(user)
    warehouse = _owned(db, warehouse_id, org_id)
    if warehouse.is_default:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The default warehouse cannot be deleted. Make another one default first.",
        )
    holding = (
        db.query(WarehouseStock)
        .filter(WarehouseStock.warehouse_id == warehouse.id, WarehouseStock.on_hand_quantity != 0)
        .count()
    )
    if holding:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{holding} item(s) still hold stock here. Move or write them off first.",
        )
    open_holds = (
        db.query(StockReservation)
        .filter(
            StockReservation.warehouse_id == warehouse.id, StockReservation.status == "active"
        )
        .count()
    )
    if open_holds:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{open_holds} open reservation(s) point here. Cancel or fulfil those orders first.",
        )
    db.delete(warehouse)
    db.commit()
