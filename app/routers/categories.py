from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_entitlement, require_permission, require_unlocked_org
from app.models import Brand, Category, SubcategoryVariant, User
from app.models.subcategory_variant import name_key
from app.schemas.brand import BrandOut
from app.schemas.catalog_hierarchy import CategoryBrandsReplace
from app.services import catalog_hierarchy_service
from app.schemas.category import (
    BulkDelete,
    BulkDeleteResult,
    CategoryCreate,
    CategoryOut,
    CategoryUpdate,
)
from app.schemas.subcategory_variant import (
    SubcategoryVariantCreate,
    SubcategoryVariantOut,
    SubcategoryVariantUpdate,
)

router = APIRouter(
    prefix="/categories",
    tags=["categories"],
    dependencies=[Depends(require_entitlement("erp.categories"))],
)

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


def _validate_parent(db: Session, org_id: str, parent_id: str | None, category: Category | None = None) -> None:
    """Category -> Sub Category is one level deep: a parent must be a top-level category of
    this firm, never the category itself, and a category that already has sub categories
    cannot become one."""
    if parent_id is None:
        return
    parent = db.get(Category, parent_id)
    if parent is None or parent.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="parent_id is not a category in your firm")
    if category is not None and parent.id == category.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A category cannot be its own parent")
    if parent.parent_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="parent_id must be a top-level category; a sub category cannot have sub categories",
        )
    if category is not None and db.query(Category.id).filter(Category.parent_id == category.id).first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This category has sub categories, so it cannot become a sub category itself",
        )


@router.post("", response_model=CategoryOut, status_code=status.HTTP_201_CREATED)
def create_category(
    payload: CategoryCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Category:
    org_id = _org_id(user)
    _validate_parent(db, org_id, payload.parent_id)
    if db.query(Category).filter(Category.organization_id == org_id, Category.name == payload.name).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A category with this name already exists")
    cat = Category(organization_id=org_id, **payload.model_dump())
    db.add(cat)
    db.commit()
    db.refresh(cat)
    return cat


@router.post("/bulk-delete", response_model=BulkDeleteResult)
@router.delete("/bulk-delete", response_model=BulkDeleteResult)
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
    if "parent_id" in data:
        _validate_parent(db, org_id, data["parent_id"], cat)
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


# ------------------------------ Category Brands ------------------------------
# Which brands apply to a top-level category -- the same brand_categories mapping that
# /brands/{brand_id}/categories manages from the brand side.


def _top_level(db: Session, category_id: str, org_id: str) -> Category:
    cat = _owned(db, category_id, org_id)
    if cat.parent_id is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=catalog_hierarchy_service.SUBCATEGORY_MAPPING_DETAIL,
        )
    return cat


@router.get("/{category_id}/brands", response_model=list[BrandOut])
def list_category_brands(
    category_id: str,
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> list[Brand]:
    """The active brands a product in this category can be given, by name: the mapped
    ones, or every active brand of the firm while the category has no mappings."""
    org_id = _org_id(user)
    cat = _top_level(db, category_id, org_id)
    return catalog_hierarchy_service.selectable_brands(db, org_id, cat.id)


@router.put("/{category_id}/brands", response_model=list[BrandOut])
def replace_category_brands(
    category_id: str,
    payload: CategoryBrandsReplace,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> list[Brand]:
    """Replace the category's complete brand mapping set. Repeated ids count once; an empty
    list removes every mapping, so the category offers every active brand again. Returns
    the mapped brands by name, inactive ones included. Products are never changed."""
    org_id = _org_id(user)
    cat = _top_level(db, category_id, org_id)
    try:
        brands = catalog_hierarchy_service.replace_category_brands(db, org_id, cat, payload.brand_ids)
        db.commit()
    except IntegrityError:  # a concurrent request mapped the same brand first
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The category's brands were changed by another request; retry",
        )
    return brands


# ------------------------- Sub category Product Types -------------------------
# The options a sub category offers as a product's Product Type (Casual Footwear ->
# Shoe / Cheppal / Sandal). Not ProductVariant: these are category master data.


def _subcategory(db: Session, subcategory_id: str, org_id: str) -> Category:
    """The firm's own sub category -- a category whose parent is one of its top-level ones."""
    sub = _owned(db, subcategory_id, org_id)
    parent = db.get(Category, sub.parent_id) if sub.parent_id else None
    if parent is None or parent.organization_id != org_id or parent.parent_id is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Product Types can only be configured on a sub category",
        )
    return sub


def _owned_variant(db: Session, sub: Category, variant_id: str) -> SubcategoryVariant:
    variant = db.get(SubcategoryVariant, variant_id)
    if variant is None or variant.subcategory_id != sub.id or variant.organization_id != sub.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product Type not found in this sub category")
    return variant


_DUPLICATE = "A Product Type with this name already exists in this sub category"


def _reject_duplicate(db: Session, sub: Category, name: str, exclude_id: str | None = None) -> None:
    query = db.query(SubcategoryVariant.id).filter(
        SubcategoryVariant.organization_id == sub.organization_id,
        SubcategoryVariant.subcategory_id == sub.id,
        SubcategoryVariant.name_key == name_key(name),
    )
    if exclude_id is not None:
        query = query.filter(SubcategoryVariant.id != exclude_id)
    if query.first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_DUPLICATE)


def _commit_variant(db: Session, variant: SubcategoryVariant) -> SubcategoryVariant:
    try:
        db.commit()
    except IntegrityError:  # a concurrent request saved the same name first
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_DUPLICATE)
    db.refresh(variant)
    return variant


@router.get("/{subcategory_id}/variants", response_model=list[SubcategoryVariantOut])
def list_subcategory_variants(
    subcategory_id: str,
    include_inactive: bool = Query(default=False, description="Also list deactivated Product Types"),
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> list[SubcategoryVariant]:
    """The sub category's Product Types, in display order -- an empty list when none are set up."""
    sub = _subcategory(db, subcategory_id, _org_id(user))
    query = db.query(SubcategoryVariant).filter(
        SubcategoryVariant.organization_id == sub.organization_id, SubcategoryVariant.subcategory_id == sub.id
    )
    if not include_inactive:
        query = query.filter(SubcategoryVariant.is_active.is_(True))
    return query.order_by(SubcategoryVariant.sort_order, SubcategoryVariant.name).all()


@router.post("/{subcategory_id}/variants", response_model=SubcategoryVariantOut, status_code=status.HTTP_201_CREATED)
def create_subcategory_variant(
    subcategory_id: str,
    payload: SubcategoryVariantCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> SubcategoryVariant:
    """Add a Product Type to a sub category. Names are unique within the sub category,
    ignoring case (409 otherwise); another sub category may reuse one."""
    sub = _subcategory(db, subcategory_id, _org_id(user))
    _reject_duplicate(db, sub, payload.name)
    variant = SubcategoryVariant(
        organization_id=sub.organization_id, subcategory_id=sub.id, name=payload.name,
        name_key=name_key(payload.name), sort_order=payload.sort_order,
    )
    db.add(variant)
    return _commit_variant(db, variant)


@router.patch("/{subcategory_id}/variants/{variant_id}", response_model=SubcategoryVariantOut)
def update_subcategory_variant(
    subcategory_id: str,
    variant_id: str,
    payload: SubcategoryVariantUpdate,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> SubcategoryVariant:
    """Rename, reorder, or deactivate (`is_active: false`) a Product Type. A deactivated
    one can no longer be chosen for a product; products that already use it keep it."""
    sub = _subcategory(db, subcategory_id, _org_id(user))
    variant = _owned_variant(db, sub, variant_id)
    data = payload.model_dump(exclude_unset=True)
    if data.get("name") is not None:
        _reject_duplicate(db, sub, data["name"], exclude_id=variant.id)
        variant.name, variant.name_key = data["name"], name_key(data["name"])
    if data.get("is_active") is not None:
        variant.is_active = data["is_active"]
    if data.get("sort_order") is not None:
        variant.sort_order = data["sort_order"]
    return _commit_variant(db, variant)
