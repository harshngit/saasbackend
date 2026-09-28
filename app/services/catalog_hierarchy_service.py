from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models import Brand, Category, Supplier
from app.models.catalog_hierarchy import BrandCategory, SupplierBrand


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
    """Validate Supplier -> Brand -> Category hierarchy for a product.
    
    - If preferred_supplier_id and brand_id are both present, SupplierBrand must exist.
    - If brand_id and category_id are both present, BrandCategory must exist.
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

    if brand_id and category_id:
        link = (
            db.query(BrandCategory)
            .filter(
                BrandCategory.organization_id == org_id,
                BrandCategory.brand_id == brand_id,
                BrandCategory.category_id == category_id,
            )
            .first()
        )
        if link is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Category is not associated with the selected brand.",
            )
