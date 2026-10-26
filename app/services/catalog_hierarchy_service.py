from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models import Brand, Category, Supplier
from app.models.catalog_hierarchy import BrandCategory, SupplierBrand

SUBCATEGORY_MAPPING_DETAIL = "Brands can only be mapped to a top-level category, not a sub category"


def ensure_supplier_brand(
    db: Session, org_id: str, supplier_id: str, brand_id: str
) -> SupplierBrand:
    """Idempotently link a Supplier to a Brand in an organization.
    
    Verifies tenant ownership for both records and prevents duplicate association rows.
    """
    supplier = db.get(Supplier, supplier_id)
    if supplier is None or supplier.organization_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="supplier_id is not a supplier in your firm",
        )

    brand = db.get(Brand, brand_id)
    if brand is None or brand.organization_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="brand_id is not a brand in your firm",
        )

    existing = (
        db.query(SupplierBrand)
        .filter(
            SupplierBrand.organization_id == org_id,
            SupplierBrand.supplier_id == supplier_id,
            SupplierBrand.brand_id == brand_id,
        )
        .first()
    )
    if existing:
        return existing

    link = SupplierBrand(
        organization_id=org_id,
        supplier_id=supplier.id,
        brand_id=brand.id,
    )
    db.add(link)
    db.flush()
    return link


def ensure_brand_category(
    db: Session, org_id: str, brand_id: str, category_id: str
) -> BrandCategory:
    """Idempotently link a Brand to a Category in an organization.
    
    Verifies tenant ownership for both records and prevents duplicate association rows.
    """
    brand = db.get(Brand, brand_id)
    if brand is None or brand.organization_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="brand_id is not a brand in your firm",
        )

    category = db.get(Category, category_id)
    if category is None or category.organization_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="category_id is not a category in your firm",
        )
    if category.parent_id is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=SUBCATEGORY_MAPPING_DETAIL,
        )

    existing = (
        db.query(BrandCategory)
        .filter(
            BrandCategory.organization_id == org_id,
            BrandCategory.brand_id == brand_id,
            BrandCategory.category_id == category_id,
        )
        .first()
    )
    if existing:
        return existing

    link = BrandCategory(
        organization_id=org_id,
        brand_id=brand.id,
        category_id=category.id,
    )
    db.add(link)
    db.flush()
    return link


def validate_product_catalog_hierarchy(
    db: Session,
    org_id: str,
    preferred_supplier_id: str | None,
    brand_id: str | None,
    category_id: str | None,
) -> None:
    """Validate Supplier -> Brand -> Category hierarchy for a new product.

    - If preferred_supplier_id and brand_id are both present, SupplierBrand must exist.
    - The brand must be applicable to the category -- see validate_category_brand.
    """
    if preferred_supplier_id and brand_id:
        link = (
            db.query(SupplierBrand)
            .filter(
                SupplierBrand.organization_id == org_id,
                SupplierBrand.supplier_id == preferred_supplier_id,
                SupplierBrand.brand_id == brand_id,
            )
            .first()
        )
        if link is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Brand is not associated with the selected supplier.",
            )

    validate_category_brand(db, org_id, category_id, brand_id, newly_assigned_brand=True)


def mapped_brand_ids(db: Session, org_id: str, category_id: str) -> set[str]:
    """The firm's brands explicitly mapped to a category -- active or not. An empty set
    means the category was never restricted, so every active brand applies to it."""
    rows = (
        db.query(BrandCategory.brand_id)
        .join(Brand, Brand.id == BrandCategory.brand_id)
        .filter(
            BrandCategory.organization_id == org_id,
            BrandCategory.category_id == category_id,
            Brand.organization_id == org_id,
        )
        .all()
    )
    return {row[0] for row in rows}


def selectable_brands(db: Session, org_id: str, category_id: str) -> list[Brand]:
    """The active brands a product in this top-level category can be given, by name:
    the mapped ones, or every active brand of the firm while the category has no mappings."""
    query = db.query(Brand).filter(Brand.organization_id == org_id, Brand.is_active.is_(True))
    mapped = mapped_brand_ids(db, org_id, category_id)
    if mapped:
        query = query.filter(Brand.id.in_(list(mapped)))
    return query.order_by(Brand.name).all()


def replace_category_brands(db: Session, org_id: str, category: Category, brand_ids: list[str]) -> list[Brand]:
    """Make `brand_ids` the category's complete mapping set (repeated ids count once; an
    empty list removes every mapping). Flushes only -- the caller commits. Returns the
    mapped brands by name, inactive ones included."""
    wanted = list(dict.fromkeys(brand_ids))
    brands = (
        db.query(Brand).filter(Brand.organization_id == org_id, Brand.id.in_(wanted)).all()
        if wanted else []
    )
    missing = set(wanted) - {brand.id for brand in brands}
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"brand_ids contains ids that are not brands in your firm: {sorted(missing)}",
        )

    existing = {
        link.brand_id: link
        for link in db.query(BrandCategory).filter(
            BrandCategory.organization_id == org_id, BrandCategory.category_id == category.id
        )
    }
    for brand_id, link in existing.items():
        if brand_id not in wanted:
            db.delete(link)
    for brand_id in wanted:
        if brand_id not in existing:
            db.add(BrandCategory(organization_id=org_id, brand_id=brand_id, category_id=category.id))
    db.flush()
    return sorted(brands, key=lambda brand: brand.name)


def categories_by_brand(db: Session, org_id: str, brand_ids_query) -> dict[str, list[dict]]:
    """{brand_id: [{id, name}, ...]} of the top-level categories each brand is mapped to,
    by name, in one query. `brand_ids_query` selects the brand ids to cover."""
    rows = (
        db.query(BrandCategory.brand_id, Category.id, Category.name)
        .join(Category, Category.id == BrandCategory.category_id)
        .filter(
            BrandCategory.organization_id == org_id,
            Category.organization_id == org_id,
            Category.parent_id.is_(None),
            BrandCategory.brand_id.in_(brand_ids_query),
        )
        .order_by(Category.name)
        .all()
    )
    result: dict[str, list[dict]] = {}
    for brand_id, category_id, name in rows:
        result.setdefault(brand_id, []).append({"id": category_id, "name": name})
    return result


def validate_category_brand(
    db: Session,
    org_id: str,
    category_id: str | None,
    brand_id: str | None,
    newly_assigned_brand: bool,
) -> None:
    """A product's brand must be applicable to its category (422 otherwise).

    - A newly assigned brand must be active; one a product already has stays valid once
      deactivated.
    - A top-level category with mappings only takes a mapped brand; one with none takes
      any of the firm's brands. Mappings exist on top-level categories only, so a legacy
      sub category in category_id restricts nothing.
    Callers have already checked both ids belong to the firm.
    """
    if brand_id is None:
        return
    brand = db.get(Brand, brand_id)
    if newly_assigned_brand and brand is not None and not brand.is_active:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This brand is inactive and cannot be chosen",
        )
    if category_id is None:
        return
    category = db.get(Category, category_id)
    if category is None or category.parent_id is not None:
        return
    mapped = mapped_brand_ids(db, org_id, category_id)
    if mapped and brand_id not in mapped:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Brand is not mapped to the selected category.",
        )
