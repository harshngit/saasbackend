"""Warehouse Transfers router — MVP implementation.

Lifecycle:
  draft -> in_transit -> received
  draft -> cancelled
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.models import NumberSequence, Product, ProductVariant, User, WarehouseTransfer, WarehouseTransferItem
from app.schemas.transfer import (
    WarehouseTransferCreate,
    WarehouseTransferOut,
)
from app.services import stock_service

router = APIRouter(prefix="/transfers", tags=["transfers"])

_view = require_permission("inventory", "view")
_create = require_permission("inventory", "create")
_edit = require_permission("inventory", "edit")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account"
        )
    return user.organization_id


def _next_transfer_number(db: Session, org_id: str) -> str:
    count = (
        db.query(WarehouseTransfer)
        .filter(WarehouseTransfer.organization_id == org_id)
        .count()
    )
    return f"TRF-{count + 1:05d}"


def _owned_transfer(db: Session, transfer_id: str, org_id: str) -> WarehouseTransfer:
    transfer = db.get(WarehouseTransfer, transfer_id)
    if transfer is None or transfer.organization_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Warehouse transfer not found"
        )
    return transfer


@router.post("", response_model=WarehouseTransferOut, status_code=status.HTTP_201_CREATED)
def create_transfer(
    payload: WarehouseTransferCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> WarehouseTransfer:
    """Create a draft warehouse transfer. Zero physical stock movement occurs on draft creation."""
    org_id = _org_id(user)

    if payload.source_warehouse_id == payload.destination_warehouse_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Source and destination warehouses cannot be the same",
        )

    source_wh = stock_service.owned_warehouse(db, payload.source_warehouse_id, org_id, require_active=True)
    if source_wh is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or unowned source warehouse"
        )

    dest_wh = stock_service.owned_warehouse(db, payload.destination_warehouse_id, org_id, require_active=True)
    if dest_wh is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or unowned destination warehouse"
        )

    transfer_num = _next_transfer_number(db, org_id)

    transfer = WarehouseTransfer(
        organization_id=org_id,
        transfer_number=transfer_num,
        source_warehouse_id=source_wh.id,
        destination_warehouse_id=dest_wh.id,
        status="draft",
        notes=payload.notes,
        created_by=user.id,
    )

    for item in payload.items:
        if item.quantity <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Transfer quantity must be greater than zero"
            )
        product = db.get(Product, item.product_id)
        if product is None or product.organization_id != org_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=f"Product {item.product_id} not found in organization"
            )
        if item.variant_id:
            variant = db.get(ProductVariant, item.variant_id)
            if variant is None or variant.product_id != product.id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail=f"Variant {item.variant_id} is invalid for product"
                )

        transfer.items.append(
            WarehouseTransferItem(
                product_id=product.id,
                variant_id=item.variant_id,
                quantity=item.quantity,
            )
        )

    db.add(transfer)
    db.commit()
    db.refresh(transfer)
    return transfer


@router.get("", response_model=list[WarehouseTransferOut])
def list_transfers(
    user: User = Depends(_view),
    status_filter: str | None = Query(default=None, alias="status"),
    source_warehouse_id: str | None = Query(default=None),
    destination_warehouse_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[WarehouseTransfer]:
    """List warehouse transfers in this organization."""
    org_id = _org_id(user)
    query = db.query(WarehouseTransfer).filter(WarehouseTransfer.organization_id == org_id)

    if status_filter:
        query = query.filter(WarehouseTransfer.status == status_filter)
    if source_warehouse_id:
        query = query.filter(WarehouseTransfer.source_warehouse_id == source_warehouse_id)
    if destination_warehouse_id:
        query = query.filter(WarehouseTransfer.destination_warehouse_id == destination_warehouse_id)

    return query.order_by(WarehouseTransfer.created_at.desc()).all()


@router.get("/{id}", response_model=WarehouseTransferOut)
def get_transfer(
    id: str,
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> WarehouseTransfer:
    """Get details of a specific warehouse transfer."""
    return _owned_transfer(db, id, _org_id(user))


@router.post("/{id}/dispatch", response_model=WarehouseTransferOut)
def dispatch_transfer(
    id: str,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> WarehouseTransfer:
    """Dispatch transfer (draft -> in_transit).

    Deducts available stock from source warehouse and logs transfer_out stock movement.
    Destination stock remains unchanged.
    """
    org_id = _org_id(user)
    transfer = _owned_transfer(db, id, org_id)

    if transfer.status != "draft":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Only draft transfers can be dispatched (current status: '{transfer.status}')",
        )

    source_wh = stock_service.owned_warehouse(db, transfer.source_warehouse_id, org_id, require_active=True)
    dest_wh = stock_service.owned_warehouse(db, transfer.destination_warehouse_id, org_id, require_active=True)

    # 1. Validate available stock for all items
    for item in transfer.items:
        free_stock = stock_service.available(db, source_wh.id, item.product_id, item.variant_id)
        if item.quantity > free_stock:
            product = db.get(Product, item.product_id)
            p_name = product.name if product else item.product_id
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Insufficient available stock for '{p_name}' at source warehouse (requested {item.quantity}, available {free_stock})",
            )

    # 2. Lock stock rows deterministically
    stock_service.lock_stock_items(
        db, org_id, source_wh.id,
        [{"product_id": i.product_id, "variant_id": i.variant_id} for i in transfer.items]
    )

    # 3. Deduct stock from source warehouse
    for item in transfer.items:
        stock_service.adjust_on_hand(
            db,
            org_id,
            source_wh.id,
            item.product_id,
            item.variant_id,
            -item.quantity,
            movement_type="transfer_out",
            note=f"Transfer Dispatch {transfer.transfer_number}",
            created_by=user.id,
        )

    transfer.status = "in_transit"
    transfer.dispatched_by = user.id
    transfer.dispatched_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(transfer)
    return transfer


@router.post("/{id}/receive", response_model=WarehouseTransferOut)
def receive_transfer(
    id: str,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> WarehouseTransfer:
    """Receive transfer (in_transit -> received).

    Credits stock to destination warehouse and logs transfer_in stock movement.
    """
    org_id = _org_id(user)
    transfer = _owned_transfer(db, id, org_id)

    if transfer.status != "in_transit":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Only in_transit transfers can be received (current status: '{transfer.status}')",
        )

    dest_wh = stock_service.owned_warehouse(db, transfer.destination_warehouse_id, org_id, require_active=True)

    # Lock stock rows at destination
    stock_service.lock_stock_items(
        db, org_id, dest_wh.id,
        [{"product_id": i.product_id, "variant_id": i.variant_id} for i in transfer.items]
    )

    # Credit stock to destination warehouse
    for item in transfer.items:
        stock_service.adjust_on_hand(
            db,
            org_id,
            dest_wh.id,
            item.product_id,
            item.variant_id,
            item.quantity,
            movement_type="transfer_in",
            note=f"Transfer Receipt {transfer.transfer_number}",
            created_by=user.id,
        )

    transfer.status = "received"
    transfer.received_by = user.id
    transfer.received_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(transfer)
    return transfer


@router.post("/{id}/cancel", response_model=WarehouseTransferOut)
def cancel_transfer(
    id: str,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> WarehouseTransfer:
    """Cancel transfer (draft -> cancelled).

    Only draft transfers can be cancelled. In-transit and received transfers cannot be cancelled.
    """
    org_id = _org_id(user)
    transfer = _owned_transfer(db, id, org_id)

    if transfer.status != "draft":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cancel transfer in '{transfer.status}' status (only draft transfers can be cancelled)",
        )

    transfer.status = "cancelled"
    db.commit()
    db.refresh(transfer)
    return transfer
