from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.models import (
    GoodsReceiptNote,
    GoodsReceiptNoteItem,
    PurchaseInvoice,
    Supplier,
    User,
    Warehouse,
)
from app.schemas.grn import GRNCreate, GRNItemIn, GRNOut, GRNUpdate
from app.services import grn_service, lookup_service, numbering_service, purchase_service, stock_service

router = APIRouter(prefix="/grns", tags=["grn"])

_view = require_permission("grn", "view")
_create = require_permission("grn", "create")
_edit = require_permission("grn", "edit")
_approve = require_permission("grn", "approve")
_delete = require_permission("grn", "delete")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account")
    return user.organization_id


def _owned(db: Session, id: str, org_id: str) -> GoodsReceiptNote:
    record = lookup_service.by_id_or_code(
        db,
        GoodsReceiptNote,
        id,
        org_id,
        GoodsReceiptNote.grn_number,
    )
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="GRN not found")
    return record


@router.post("", response_model=GRNOut, status_code=status.HTTP_201_CREATED)
def create_grn(
    payload: GRNCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> GoodsReceiptNote:
    org_id = _org_id(user)
    purchase = db.get(PurchaseInvoice, payload.purchase_id)
    if purchase is None or purchase.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Purchase order not found in your firm")

    if purchase.status in ("draft", "pending"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Purchase order must be confirmed before creating a goods receipt note",
        )
    if purchase.status == "cancelled":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot create goods receipt note for a cancelled purchase order",
        )

    supplier_id = payload.supplier_id or purchase.supplier_id
    if supplier_id:
        purchase_service.validate_supplier(db, org_id, supplier_id)

    warehouse_id = payload.warehouse_id or purchase.warehouse_id
    if warehouse_id:
        purchase_service.validate_warehouse(db, org_id, warehouse_id)

    built_items = grn_service.build_and_calculate_grn_items(db, org_id, purchase, payload.items)
    if not built_items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="GRN must contain at least one item",
        )

    grn_num = payload.grn_number or numbering_service.next_number(db, org_id, GoodsReceiptNote.grn_number, "GRN")

    grn = GoodsReceiptNote(
        organization_id=org_id,
        grn_number=grn_num,
        purchase_id=purchase.id,
        supplier_id=supplier_id,
        warehouse_id=warehouse_id,
        status="draft",
        received_date=payload.received_date or datetime.now(timezone.utc),
        notes=payload.notes,
        created_by=user.id,
    )
    grn.items = built_items
    db.add(grn)
    db.commit()
    db.refresh(grn)
    return grn


@router.get("", response_model=list[GRNOut])
def list_grns(
    user: User = Depends(_view),
    purchase_id: str | None = Query(default=None),
    supplier_id: str | None = Query(default=None),
    warehouse_id: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    search: str | None = Query(default=None, description="matches grn_number"),
    db: Session = Depends(get_db),
) -> list[GoodsReceiptNote]:
    org_id = _org_id(user)
    q = db.query(GoodsReceiptNote).filter(GoodsReceiptNote.organization_id == org_id)
    if purchase_id:
        q = q.filter(GoodsReceiptNote.purchase_id == purchase_id)
    if supplier_id:
        q = q.filter(GoodsReceiptNote.supplier_id == supplier_id)
    if warehouse_id:
        q = q.filter(GoodsReceiptNote.warehouse_id == warehouse_id)
    if status_filter:
        q = q.filter(GoodsReceiptNote.status == status_filter.lower())
    if search:
        s = f"%{search}%"
        q = q.filter(GoodsReceiptNote.grn_number.ilike(s))

    return q.order_by(GoodsReceiptNote.created_at.desc()).all()


@router.get("/{id}", response_model=GRNOut)
def get_grn(id: str, user: User = Depends(_view), db: Session = Depends(get_db)) -> GoodsReceiptNote:
    return _owned(db, id, _org_id(user))


@router.patch("/{id}", response_model=GRNOut)
def update_grn(
    id: str,
    payload: GRNUpdate,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> GoodsReceiptNote:
    org_id = _org_id(user)
    grn = _owned(db, id, org_id)
    if grn.status != "draft":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Only draft GRNs can be edited (current status: '{grn.status}')",
        )

    data = payload.model_dump(exclude_unset=True)
    items_raw = data.pop("items", None)

    for field, value in data.items():
        setattr(grn, field, value)

    if items_raw is not None:
        purchase = db.get(PurchaseInvoice, grn.purchase_id)
        built_items = grn_service.build_and_calculate_grn_items(
            db, org_id, purchase, [GRNItemIn(**i) for i in items_raw]
        )
        grn.items = built_items

    db.commit()
    db.refresh(grn)
    return grn


@router.post("/{id}/confirm", response_model=GRNOut)
def confirm_grn_endpoint(
    id: str,
    user: User = Depends(_approve),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> GoodsReceiptNote:
    """Confirm GRN: Atomically inward accepted_qty to warehouse stock & update Purchase receiving progress."""
    org_id = _org_id(user)
    grn = _owned(db, id, org_id)
    return grn_service.confirm_grn(db, grn, org_id, user.id)


@router.post("/{id}/cancel", response_model=GRNOut)
def cancel_grn(
    id: str,
    user: User = Depends(_approve),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> GoodsReceiptNote:
    """Cancel Draft GRN (0 stock movement). Confirmed GRNs cannot be cancelled."""
    org_id = _org_id(user)
    grn = _owned(db, id, org_id)
    if grn.status == "confirmed":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot cancel a confirmed GRN",
        )
    grn.status = "cancelled"
    db.commit()
    db.refresh(grn)
    return grn


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_grn(
    id: str,
    user: User = Depends(_delete),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> None:
    org_id = _org_id(user)
    grn = _owned(db, id, org_id)
    if grn.status == "confirmed":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete a confirmed GRN",
        )
    db.delete(grn)
    db.commit()
