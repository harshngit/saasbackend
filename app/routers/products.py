from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.models import Category, Product, ProductVariant, User
from app.schemas.category import BulkDelete, BulkDeleteResult
from app.schemas.product import ProductCreate, ProductListItem, ProductOut, ProductUpdate, VariantIn

router = APIRouter(prefix="/products", tags=["products"])

_view = require_permission("products", "view")
_create = require_permission("products", "create")
_edit = require_permission("products", "edit")
_delete = require_permission("products", "delete")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account")
    return user.organization_id


def _owned(db: Session, product_id: str, org_id: str) -> Product:
    product = db.get(Product, product_id)
    if product is None or product.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    return product


def _validate_category(db: Session, org_id: str, category_id: str | None) -> None:
    if category_id is None:
        return
    cat = db.get(Category, category_id)
    if cat is None or cat.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="category_id is not a category in your firm")


def _build_variants(variations: list[VariantIn]) -> list[ProductVariant]:
    return [ProductVariant(**v.model_dump()) for v in variations]


@router.post("", response_model=ProductOut, status_code=status.HTTP_201_CREATED)
def create_product(
    payload: ProductCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Product:
    org_id = _org_id(user)
    _validate_category(db, org_id, payload.category_id)
    data = payload.model_dump()
    variations = data.pop("variations")
    product = Product(organization_id=org_id, **data)
    product.variations = _build_variants([VariantIn(**v) for v in variations])
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


@router.post("/bulk-delete", response_model=BulkDeleteResult)
def bulk_delete_products(
    payload: BulkDelete,
    user: User = Depends(_delete),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> BulkDeleteResult:
    org_id = _org_id(user)
    products = (
        db.query(Product)
        .filter(Product.organization_id == org_id, Product.id.in_(payload.ids))
        .all()
    )
    for p in products:  # ORM delete so variant cascade fires
        db.delete(p)
    db.commit()
    return BulkDeleteResult(deleted=len(products))


@router.get("", response_model=list[ProductListItem])
def list_products(
    user: User = Depends(_view),
    search: str | None = Query(default=None, description="matches name / sku / brand / vendor"),
    category_id: str | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[Product]:
    org_id = _org_id(user)
    query = db.query(Product).filter(Product.organization_id == org_id)
    if search:
        like = f"%{search}%"
        query = query.filter(
            or_(Product.name.ilike(like), Product.sku.ilike(like), Product.brand.ilike(like), Product.vendor.ilike(like))
        )
    if category_id is not None:
        query = query.filter(Product.category_id == category_id)
    if is_active is not None:
        query = query.filter(Product.is_active == is_active)
    return query.order_by(Product.created_at.desc()).all()


@router.get("/{product_id}", response_model=ProductOut)
def get_product(product_id: str, user: User = Depends(_view), db: Session = Depends(get_db)) -> Product:
    return _owned(db, product_id, _org_id(user))


@router.patch("/{product_id}", response_model=ProductOut)
def update_product(
    product_id: str,
    payload: ProductUpdate,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Product:
    org_id = _org_id(user)
    product = _owned(db, product_id, org_id)
    data = payload.model_dump(exclude_unset=True)
    if "category_id" in data:
        _validate_category(db, org_id, data["category_id"])
    variations = data.pop("variations", None)
    for field, value in data.items():
        setattr(product, field, value)
    if variations is not None:  # full replace of the variant set
        product.variations = _build_variants([VariantIn(**v) for v in variations])
    db.commit()
    db.refresh(product)
    return product


@router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_product(
    product_id: str,
    user: User = Depends(_delete),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> None:
    product = _owned(db, product_id, _org_id(user))
    db.delete(product)
    db.commit()
