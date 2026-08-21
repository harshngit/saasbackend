from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.models import Brand, User
from app.schemas.brand import (
    BrandCreate,
    BrandOut,
    BrandUpdate,
    BulkDelete,
    BulkDeleteResult,
)

router = APIRouter(prefix="/brands", tags=["brands"])

# Brands are part of product management — gated by the `products` module, same as categories.
_view = require_permission("products", "view")
_create = require_permission("products", "create")
_edit = require_permission("products", "edit")
_delete = require_permission("products", "delete")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account")
    return user.organization_id


def _owned(db: Session, brand_id: str, org_id: str) -> Brand:
    brand = db.get(Brand, brand_id)
    if brand is None or brand.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Brand not found")
    return brand


@router.post("", response_model=BrandOut, status_code=status.HTTP_201_CREATED)
def create_brand(
    payload: BrandCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Brand:
    org_id = _org_id(user)
    if db.query(Brand).filter(Brand.organization_id == org_id, Brand.name == payload.name).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A brand with this name already exists")
    brand = Brand(organization_id=org_id, **payload.model_dump())
    db.add(brand)
    db.commit()
    db.refresh(brand)
    return brand


@router.post("/bulk-delete", response_model=BulkDeleteResult)
def bulk_delete_brands(
    payload: BulkDelete,
    user: User = Depends(_delete),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> BulkDeleteResult:
    org_id = _org_id(user)
    deleted = (
        db.query(Brand)
        .filter(Brand.organization_id == org_id, Brand.id.in_(payload.ids))
        .delete(synchronize_session=False)
    )
    db.commit()
    return BulkDeleteResult(deleted=deleted)


@router.get("", response_model=list[BrandOut])
def list_brands(
    user: User = Depends(_view),
    search: str | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[Brand]:
    org_id = _org_id(user)
    query = db.query(Brand).filter(Brand.organization_id == org_id)
    if search:
        like = f"%{search}%"
        query = query.filter(or_(Brand.name.ilike(like), Brand.description.ilike(like)))
    if is_active is not None:
        query = query.filter(Brand.is_active == is_active)
    return query.order_by(Brand.name).all()


@router.get("/{brand_id}", response_model=BrandOut)
def get_brand(brand_id: str, user: User = Depends(_view), db: Session = Depends(get_db)) -> Brand:
    return _owned(db, brand_id, _org_id(user))


@router.patch("/{brand_id}", response_model=BrandOut)
def update_brand(
    brand_id: str,
    payload: BrandUpdate,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Brand:
    org_id = _org_id(user)
    brand = _owned(db, brand_id, org_id)
    data = payload.model_dump(exclude_unset=True)
    if "name" in data and data["name"] != brand.name:
        if db.query(Brand).filter(
            Brand.organization_id == org_id, Brand.name == data["name"], Brand.id != brand.id
        ).first():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A brand with this name already exists")
    for field, value in data.items():
        setattr(brand, field, value)
    db.commit()
    db.refresh(brand)
    return brand


@router.delete("/{brand_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_brand(
    brand_id: str,
    user: User = Depends(_delete),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> None:
    brand = _owned(db, brand_id, _org_id(user))
    db.delete(brand)  # products referencing this brand get brand_id set to NULL (FK ON DELETE SET NULL)
    db.commit()
