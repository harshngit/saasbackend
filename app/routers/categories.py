from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.models import Category, User
from app.schemas.category import (
    BulkDelete,
    BulkDeleteResult,
    CategoryCreate,
    CategoryOut,
    CategoryUpdate,
)

router = APIRouter(prefix="/categories", tags=["categories"])

# Categories are part of product management — gated by the `products` module.
_view = require_permission("products", "view")
_create = require_permission("products", "create")
_edit = require_permission("products", "edit")
_delete = require_permission("products", "delete")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account")
    return user.organization_id


def _owned(db: Session, category_id: str, org_id: str) -> Category:
    cat = db.get(Category, category_id)
    if cat is None or cat.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")
    return cat


@router.post("", response_model=CategoryOut, status_code=status.HTTP_201_CREATED)
def create_category(
    payload: CategoryCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Category:
    org_id = _org_id(user)
    if db.query(Category).filter(Category.organization_id == org_id, Category.name == payload.name).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A category with this name already exists")
    cat = Category(organization_id=org_id, **payload.model_dump())
    db.add(cat)
    db.commit()
    db.refresh(cat)
    return cat


@router.post("/bulk-delete", response_model=BulkDeleteResult)
def bulk_delete_categories(
    payload: BulkDelete,
    user: User = Depends(_delete),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> BulkDeleteResult:
    org_id = _org_id(user)
    deleted = (
        db.query(Category)
        .filter(Category.organization_id == org_id, Category.id.in_(payload.ids))
        .delete(synchronize_session=False)
    )
    db.commit()
    return BulkDeleteResult(deleted=deleted)


@router.get("", response_model=list[CategoryOut])
def list_categories(
    user: User = Depends(_view),
    search: str | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[Category]:
    org_id = _org_id(user)
    query = db.query(Category).filter(Category.organization_id == org_id)
    if search:
        like = f"%{search}%"
        query = query.filter(or_(Category.name.ilike(like), Category.description.ilike(like)))
    if is_active is not None:
        query = query.filter(Category.is_active == is_active)
    return query.order_by(Category.name).all()


@router.get("/{category_id}", response_model=CategoryOut)
def get_category(category_id: str, user: User = Depends(_view), db: Session = Depends(get_db)) -> Category:
    return _owned(db, category_id, _org_id(user))


@router.patch("/{category_id}", response_model=CategoryOut)
def update_category(
    category_id: str,
    payload: CategoryUpdate,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Category:
    org_id = _org_id(user)
    cat = _owned(db, category_id, org_id)
    data = payload.model_dump(exclude_unset=True)
    if "name" in data and data["name"] != cat.name:
        if db.query(Category).filter(
            Category.organization_id == org_id, Category.name == data["name"], Category.id != cat.id
        ).first():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A category with this name already exists")
    for field, value in data.items():
        setattr(cat, field, value)
    db.commit()
    db.refresh(cat)
    return cat


@router.delete("/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_category(
    category_id: str,
    user: User = Depends(_delete),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> None:
    cat = _owned(db, category_id, _org_id(user))
    db.delete(cat)  # products in this category get category_id set to NULL (FK ON DELETE SET NULL)
    db.commit()
