from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.models import Product, ProductVariant, StockMovement, User, Warehouse
from app.services import tracking_service
from app.schemas.inventory import (
    ExpiringBatch,
    InventoryDetailOut,
    InventoryItemOut,
    SetStock,
    StockAdjustmentCreate,
)

router = APIRouter(prefix="/inventory", tags=["inventory"])

_view = require_permission("inventory", "view")
_create = require_permission("inventory", "create")
_edit = require_permission("inventory", "edit")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account")
    return user.organization_id


def _owned_product(db: Session, product_id: str, org_id: str) -> Product:
    product = db.get(Product, product_id)
    if product is None or product.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    return product


def _apply_movement(
    db: Session, product: Product, variant_id: str | None, delta: int, movement_type: str, note: str | None, user: User
) -> StockMovement:
    """Apply a signed stock change to a product or variant, and log the movement."""
    if variant_id is not None:
        variant = db.get(ProductVariant, variant_id)
        if variant is None or variant.product_id != product.id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="variant_id is not a variant of this product")
        new_stock = (variant.inventory or 0) + delta
        if new_stock < 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Insufficient stock for this variant")
        variant.inventory = new_stock
    else:
        new_stock = (product.total_inventory or 0) + delta
        if new_stock < 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Insufficient stock")
        product.total_inventory = new_stock

    movement = StockMovement(
        organization_id=product.organization_id,
        product_id=product.id,
        variant_id=variant_id,
        movement_type=movement_type,
        quantity=delta,
        balance_after=new_stock,
        note=note,
        created_by=user.id,
    )
    db.add(movement)
    return movement


@router.get("/expiring", response_model=list[ExpiringBatch])
def expiring_stock(
    user: User = Depends(_view),
    within_days: int = Query(default=30, ge=0, le=3650, description="How far ahead to look"),
    include_expired: bool = Query(default=True),
    db: Session = Depends(get_db),
) -> list[ExpiringBatch]:
    """Batches that are expired or expiring, soonest first.

    Only batch or expiry tracked products have lots, so only their stock appears here.
    """
    org_id = _org_id(user)
    rows = tracking_service.expiring(db, org_id, within_days, include_expired)
    out = []
    for row in rows:
        product = db.get(Product, row.product_id)
        warehouse = db.get(Warehouse, row.warehouse_id)
        out.append(ExpiringBatch(
            **{
                field: getattr(row, field) for field in (
                    "id", "warehouse_id", "product_id", "variant_id", "batch_number",
                    "manufacturing_date", "expiry_date", "quantity", "received_quantity", "mrp",
                )
            },
            product_name=product.name if product is not None else None,
            warehouse_name=warehouse.name if warehouse is not None else None,
        ))
    return out


@router.get("", response_model=list[InventoryItemOut])
def stock_board(
    user: User = Depends(_view),
    search: str | None = Query(default=None, description="matches name / sku / brand"),
    category_id: str | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[Product]:
    """Stock board: every product with its current available stock (and per-variant)."""
    org_id = _org_id(user)
    query = db.query(Product).filter(Product.organization_id == org_id)
    if search:
        like = f"%{search}%"
        query = query.filter(or_(Product.name.ilike(like), Product.sku.ilike(like), Product.brand.ilike(like)))
    if category_id is not None:
        query = query.filter(Product.category_id == category_id)
    if is_active is not None:
        query = query.filter(Product.is_active == is_active)
    return query.order_by(Product.name).all()


@router.get("/{product_id}", response_model=InventoryDetailOut)
def stock_detail(product_id: str, user: User = Depends(_view), db: Session = Depends(get_db)) -> object:
    """A product's stock plus its full movement history."""
    org_id = _org_id(user)
    product = _owned_product(db, product_id, org_id)
    movements = (
        db.query(StockMovement)
        .filter(StockMovement.product_id == product_id)
        .order_by(StockMovement.created_at.desc())
        .all()
    )
    data = InventoryItemOut.model_validate(product).model_dump()
    data["movements"] = movements
    return InventoryDetailOut(**data)


@router.post("/adjustments", response_model=InventoryDetailOut, status_code=status.HTTP_201_CREATED)
def create_adjustment(
    payload: StockAdjustmentCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> object:
    """Record a stock movement (purchase received, damaged, expired, return, manual, …).
    `quantity` is signed: positive adds stock, negative removes it."""
    org_id = _org_id(user)
    product = _owned_product(db, payload.product_id, org_id)
    _apply_movement(db, product, payload.variant_id, payload.quantity, payload.movement_type, payload.note, user)
    db.commit()
    db.refresh(product)
    return stock_detail(product.id, user, db)


@router.patch("/{product_id}", response_model=InventoryDetailOut)
def set_stock(
    product_id: str,
    payload: SetStock,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> object:
    """Set a product/variant's stock to an exact number (records the delta as an adjustment)."""
    org_id = _org_id(user)
    product = _owned_product(db, product_id, org_id)
    if payload.variant_id is not None:
        variant = db.get(ProductVariant, payload.variant_id)
        if variant is None or variant.product_id != product.id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="variant_id is not a variant of this product")
        current = variant.inventory or 0
    else:
        current = product.total_inventory or 0
    delta = payload.quantity - current
    if delta != 0:
        _apply_movement(db, product, payload.variant_id, delta, "adjustment", payload.note, user)
        db.commit()
        db.refresh(product)
    return stock_detail(product.id, user, db)
