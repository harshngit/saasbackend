import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_entitlement, require_permission, require_unlocked_org
from app.core.files import safe_delete_unreferenced_file, save_upload
from app.models import (
    Brand,
    Category,
    Organization,
    Product,
    ProductPricing,
    ProductSerial,
    ProductStatus,
    ProductVariant,
    StockBatch,
    Supplier,
    User,
)
from app.services import numbering_service, lookup_service, catalog_hierarchy_service
from app.models.catalog_hierarchy import SupplierBrand
from app.models.subcategory_variant import SubcategoryVariant
from app.schemas.bulk import BulkDelete, BulkDeleteResult
from app.schemas.inventory import BatchOut, SerialOut

from app.core.pagination import paginate
from app.schemas.pagination import PaginatedResponse
from app.schemas.product import (
    ProductAttachment,
    ProductCreate,
    ProductFileField,
    ProductListItem,
    ProductOut,
    ProductUpdate,
    VariantIn,
)

router = APIRouter(
    prefix="/products",
    tags=["products"],
    dependencies=[Depends(require_entitlement("erp.products"))],
)

_view = require_permission("products", "view")
_create = require_permission("products", "create")
_edit = require_permission("products", "edit")
_delete = require_permission("products", "delete")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account")
    return user.organization_id


def _owned(db: Session, product_id: str, org_id: str) -> Product:
    """Accepts the UUID or the human-facing code (product_id, sku, barcode)."""
    record = lookup_service.by_id_or_code(
        db, Product, product_id, org_id, Product.product_id, Product.sku, Product.barcode
    )
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    return record


def _validate_category(db: Session, org_id: str, category_id: str | None) -> None:
    if category_id is None:
        return
    cat = db.get(Category, category_id)
    if cat is None or cat.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="category_id is not a category in your firm")


def _validate_supplier(db: Session, org_id: str, supplier_id: str | None) -> None:
    if supplier_id is None:
        return
    supplier = db.get(Supplier, supplier_id)
    if supplier is None or supplier.organization_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="preferred_supplier_id is not a supplier in your firm",
        )


def _validate_brand(db: Session, org_id: str, brand_id: str | None) -> None:
    if brand_id is None:
        return
    brand = db.get(Brand, brand_id)
    if brand is None or brand.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="brand_id is not a brand in your firm")


def _validate_classification(
    db: Session, org_id: str, category_id: str | None, sub_category_id: str | None
) -> None:
    """Category -> Sub Category: a sub category is a child Category of the chosen one."""
    if sub_category_id is None:
        return
    sub = db.get(Category, sub_category_id)
    if category_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="sub_category_id requires category_id"
        )
    if sub is None or sub.organization_id != org_id or sub.parent_id != category_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="sub_category_id is not a sub category of category_id",
        )


def _validate_top_level_category(db: Session, category_id: str | None) -> None:
    """category_id is a top-level category; a sub category goes in sub_category_id.
    (Called after _validate_category, so the category is this firm's own.)"""
    if category_id is None:
        return
    cat = db.get(Category, category_id)
    if cat is not None and cat.parent_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="category_id must be a top-level category; send a sub category as sub_category_id",
        )


def _validate_product_type_ref(
    db: Session, org_id: str, product_type_id: str | None, sub_category_id: str | None, newly_assigned: bool
) -> None:
    """product_type_id is one of this firm's Product Types configured under the product's
    sub category. A newly chosen one must be active; one a product already has stays valid
    after it is deactivated. A Product Type whose sub category was deleted (subcategory_id
    NULL) matches no sub category, so it can never be chosen again."""
    if product_type_id is None:
        return
    product_type = db.get(SubcategoryVariant, product_type_id)
    if product_type is None or product_type.organization_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="product_type_id is not a Product Type in your firm",
        )
    if sub_category_id is None or product_type.subcategory_id != sub_category_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="product_type_id is not a Product Type of the product's sub category "
                   "(change or clear product_type_id together with sub_category_id)",
        )
    if newly_assigned and not product_type.is_active:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This Product Type is inactive and cannot be chosen",
        )


def _validate_product_type(db: Session, org_id: str, product_type: str | None) -> None:
    """Product Type is its own value, never a Category id -- category and sub category
    go in category_id / sub_category_id."""
    if not product_type:
        return
    is_category = (
        db.query(Category.id)
        .filter(Category.organization_id == org_id, Category.id == product_type.strip())
        .first()
    )
    if is_category is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="product_type must be a product type, not a category id; send categories as "
            "category_id / sub_category_id",
        )


def _variant_columns(variant: dict) -> dict:
    """A VariantIn dump as ProductVariant column values: the nested pricing object is
    flattened (selling_price is the `price` column) and the internal id is dropped."""
    fields = {k: v for k, v in variant.items() if k not in ("id", "pricing")}
    for key, value in (variant.get("pricing") or {}).items():
        if key == "selling_price":
            if value is not None:
                fields["price"] = value
        else:
            fields[key] = value
    return fields


def _new_variant(db: Session, org_id: str, fields: dict, **extra: object) -> ProductVariant:
    """A new variant with its business ID drawn from the owning firm's VAR sequence."""
    return ProductVariant(
        organization_id=org_id,
        variant_code=numbering_service.next_variant_code(db, org_id),
        **extra,
        **fields,
    )


def _build_variants(db: Session, org_id: str, variations: list[VariantIn]) -> list[ProductVariant]:
    return [_new_variant(db, org_id, _variant_columns(v.model_dump())) for v in variations]


_VARIANT_REFERENCES: list[tuple[str, str]] = [
    ("SalesOrderItem", "an order"),
    ("QuotationItem", "a quotation"),
    ("DeliveryItem", "a delivery"),
    ("InvoiceItem", "an invoice"),
    ("PurchaseInvoiceItem", "a purchase invoice"),
    ("ReturnItem", "a sales return"),
    ("VehicleLoadingItem", "a vehicle loading"),
    ("StockReservation", "a stock reservation"),
    ("StockMovement", "a stock movement"),
    ("StockBatch", "a batch"),
    ("ProductSerial", "a serial number"),
]


def _variant_in_use(db: Session, variant_id: str) -> list[str]:
    """Which kinds of record still point at this variant."""
    from app import models

    found = []
    for model_name, label in _VARIANT_REFERENCES:
        model = getattr(models, model_name, None)
        if model is None or not hasattr(model, "variant_id"):
            continue
        if db.query(model).filter(model.variant_id == variant_id).first() is not None:
            found.append(label)
    return found


_PRODUCT_REFERENCES: list[tuple[str, str]] = [
    ("SalesOrderItem", "an order"),
    ("QuotationItem", "a quotation"),
    ("DeliveryItem", "a delivery"),
    ("InvoiceItem", "an invoice"),
    ("PurchaseInvoiceItem", "a purchase invoice"),
    ("GoodsReceiptNoteItem", "a goods receipt note"),
    ("ReturnItem", "a sales return"),
    ("PurchaseReturnItem", "a purchase return"),
    ("VehicleLoadingItem", "a vehicle loading"),
    ("StockReservation", "a stock reservation"),
    ("StockMovement", "a stock movement"),
    ("StockBatch", "a batch"),
    ("ProductSerial", "a serial number"),
    ("WarehouseTransferItem", "a warehouse transfer"),
]


def _product_in_use(db: Session, product_id: str) -> list[str]:
    """Which kinds of record still point at this product or its variants."""
    from app import models

    found = []
    for model_name, label in _PRODUCT_REFERENCES:
        model = getattr(models, model_name, None)
        if model is None or not hasattr(model, "product_id"):
            continue
        if db.query(model).filter(model.product_id == product_id).first() is not None:
            found.append(label)

    # Check warehouse on-hand stock holding
    holding = (
        db.query(models.WarehouseStock)
        .filter(
            models.WarehouseStock.product_id == product_id,
            models.WarehouseStock.on_hand_quantity != 0,
        )
        .first()
    )
    if holding is not None:
        found.append("physical stock on hand")

    # Check all variants of this product
    variants = (
        db.query(models.ProductVariant)
        .filter(models.ProductVariant.product_id == product_id)
        .all()
    )
    for v in variants:
        found.extend(_variant_in_use(db, v.id))

    return list(dict.fromkeys(found))



@router.post("", response_model=ProductOut, status_code=status.HTTP_201_CREATED)
def create_product(
    payload: ProductCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Product:
    org_id = _org_id(user)
    _validate_category(db, org_id, payload.category_id)
    _validate_category(db, org_id, payload.sub_category_id)
    _validate_top_level_category(db, payload.category_id)
    _validate_classification(db, org_id, payload.category_id, payload.sub_category_id)
    _validate_product_type(db, org_id, payload.product_type)
    _validate_product_type_ref(db, org_id, payload.product_type_id, payload.sub_category_id, newly_assigned=True)
    _validate_supplier(db, org_id, payload.preferred_supplier_id)
    _validate_brand(db, org_id, payload.brand_id)
    catalog_hierarchy_service.validate_product_catalog_hierarchy(
        db, org_id, payload.preferred_supplier_id, payload.brand_id, payload.category_id
    )
    data = payload.model_dump()
    variations = data.pop("variations")
    has_variants = data.pop("has_variants")
    pricing_data = data.pop("pricing", None) or {}
    # Plain string in the DB regardless of the enum wrapper Pydantic hands back.
    data["status"] = ProductStatus(data["status"]).value

    # Default currency from organization if not provided
    if not pricing_data.get("currency"):
        org = db.get(Organization, org_id)
        pricing_data["currency"] = (org.currency if org and org.currency else "INR")

    data["product_id"] = numbering_service.next_master_number(db, org_id, Product.product_id, "PRD")

    # Sync price on Product model with selling_price for backward compatibility
    selling_p = pricing_data.get("selling_price", 0.0)
    purchase_p = pricing_data.get("purchase_price", 0.0)
    data["price"] = selling_p
    product = Product(organization_id=org_id, **data)
    product.variations = _build_variants(db, org_id, payload.variations)
    # Left unset, the toggle just follows whether variants were actually sent.
    product.has_variants = bool(variations) if has_variants is None else has_variants
    # Keep the legacy is_active boolean in sync with the new status field.
    product.is_active = product.status == ProductStatus.ACTIVE.value

    product.pricing = ProductPricing(
        purchase_price=purchase_p,
        selling_price=selling_p,
        mrp=pricing_data.get("mrp"),
        wholesale_price=pricing_data.get("wholesale_price"),
        dealer_price=pricing_data.get("dealer_price"),
        discount_percent=pricing_data.get("discount_percent"),
        tax_inclusive=pricing_data.get("tax_inclusive", False),
        currency=pricing_data.get("currency"),
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


@router.post("/bulk-delete", response_model=BulkDeleteResult)
@router.delete("/bulk-delete", response_model=BulkDeleteResult)
def bulk_delete_products(
    payload: BulkDelete,
    user: User = Depends(_delete),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> BulkDeleteResult:
    org_id = _org_id(user)
    unique_ids = list(dict.fromkeys(payload.ids))
    if not unique_ids:
        return BulkDeleteResult(deleted=0)

    products: list[Product] = []
    for pid in unique_ids:
        products.append(_owned(db, pid, org_id))

    for p in products:
        used_by = _product_in_use(db, p.id)
        if used_by:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Product '{p.name}' is already referenced by "
                    + ", ".join(sorted(set(used_by)))
                    + ". Deleting it would compromise historical records. Deactivate it instead."
                ),
            )

    for p in products:
        db.delete(p)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Could not delete one or more products due to referencing records. Deactivate them instead.",
        )

    return BulkDeleteResult(deleted=len(products))



from app.core.sorting import apply_sorting
from app.models.warehouse import StockReservation
from sqlalchemy import func

_product_reserved_subquery = select(
    func.coalesce(func.sum(StockReservation.reserved_quantity - StockReservation.consumed_quantity), 0.0)
).where(
    StockReservation.product_id == Product.id,
    StockReservation.status == "active",
).scalar_subquery()
_product_available_subquery = Product.total_inventory - _product_reserved_subquery

PRODUCT_SORTABLE_FIELDS = {
    "name": Product.name,
    "product_name": Product.name,
    "sku": Product.sku,
    "product_id": Product.product_id,
    "brand": func.coalesce(
        select(Brand.name).where(Brand.id == Product.brand_id).scalar_subquery(),
        Product.brand,
    ),
    "category": select(Category.name).where(Category.id == Product.category_id).scalar_subquery(),
    "price": Product.price,
    "selling_price": Product.price,
    "wholesale_price": Product.wholesale_price,
    "mrp": Product.msrp_mrp,
    "cost_price": Product.price,
    "total_stock": Product.total_inventory,
    "total_inventory": Product.total_inventory,
    "stock": Product.total_inventory,
    "total": Product.total_inventory,
    "reserved_stock": _product_reserved_subquery,
    "reserved": _product_reserved_subquery,
    "available_stock": _product_available_subquery,
    "available": _product_available_subquery,
    "status": Product.status,
    "is_active": Product.is_active,
    "created_at": Product.created_at,
}


@router.get("", response_model=PaginatedResponse[ProductListItem])
def list_products(
    user: User = Depends(_view),
    search: str | None = Query(default=None, description="matches product code / name / sku / barcode / brand / vendor"),
    barcode: str | None = Query(
        default=None, description="Exact barcode — what a scanner sends. Matches a variant's too"
    ),
    category_id: str | None = Query(default=None),
    sub_category_id: str | None = Query(default=None),
    brand_id: str | None = Query(default=None),
    preferred_supplier_id: str | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status", description="active | inactive | discontinued"),
    page: int = Query(default=1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(default=10, ge=1, le=100, description="Items per page (default 10)"),
    sort_by: str | None = Query(default=None, description="Field to sort by"),
    sort_order: str | None = Query(default=None, description="Sort order: asc or desc"),
    db: Session = Depends(get_db),
) -> PaginatedResponse[ProductListItem]:
    org_id = _org_id(user)
    query = db.query(Product).filter(Product.organization_id == org_id)
    if search:
        like = f"%{search}%"
        query = query.filter(
            or_(
                Product.name.ilike(like),
                Product.sku.ilike(like),
                Product.barcode.ilike(like),
                Product.brand.ilike(like),
                Product.vendor.ilike(like),
                Product.product_id.ilike(like),
            )
        )
    if barcode:
        # A scanned code is an exact match, on the product or on one of its variants —
        # scanning a variant's barcode has to find the product it belongs to.
        scanned = barcode.strip()
        variant_products = (
            db.query(ProductVariant.product_id)
            .filter(ProductVariant.barcode == scanned)
            .subquery()
        )
        query = query.filter(
            or_(Product.barcode == scanned, Product.id.in_(select(variant_products)))
        )
    if category_id is not None:
        query = query.filter(Product.category_id == category_id)
    if sub_category_id is not None:
        query = query.filter(Product.sub_category_id == sub_category_id)
    if brand_id is not None:
        query = query.filter(Product.brand_id == brand_id)
    if preferred_supplier_id is not None:
        query = query.filter(Product.preferred_supplier_id == preferred_supplier_id)
    if is_active is not None:
        query = query.filter(Product.is_active == is_active)
    if status_filter is not None:
        query = query.filter(Product.status == status_filter)

    query = apply_sorting(
        query,
        sort_by=sort_by,
        sort_order=sort_order,
        allowed_fields=PRODUCT_SORTABLE_FIELDS,
        default_order=[Product.created_at.desc(), Product.id.desc()],
        secondary_key=Product.id.desc(),
    )
    items, total, page, page_size, total_pages = paginate(query, page=page, page_size=page_size)
    return PaginatedResponse(
        items=[ProductListItem.model_validate(p) for p in items],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.get("/{product_id}", response_model=ProductOut)
def get_product(product_id: str, user: User = Depends(_view), db: Session = Depends(get_db)) -> Product:
    return _owned(db, product_id, _org_id(user))


@router.get("/{product_id}/batches", response_model=list[BatchOut])
def product_batches(
    product_id: str,
    user: User = Depends(_view),
    warehouse_id: str | None = Query(default=None),
    in_stock_only: bool = Query(default=True, description="Hide lots that are used up"),
    db: Session = Depends(get_db),
) -> list[StockBatch]:
    """The lots this product is in stock as, earliest expiry first.

    Empty for a product that is not batch or expiry tracked — its stock is just a count.
    """
    org_id = _org_id(user)
    product = _owned(db, product_id, org_id)
    query = db.query(StockBatch).filter(
        StockBatch.organization_id == org_id, StockBatch.product_id == product.id
    )
    if warehouse_id:
        query = query.filter(StockBatch.warehouse_id == warehouse_id)
    if in_stock_only:
        query = query.filter(StockBatch.quantity > 0)
    rows = query.all()
    return sorted(
        rows,
        key=lambda row: (row.expiry_date is None, row.expiry_date or row.created_at),
    )


@router.get("/{product_id}/serials", response_model=list[SerialOut])
def product_serials(
    product_id: str,
    user: User = Depends(_view),
    status_filter: str | None = Query(
        default=None, alias="status", description="in_stock | sold | returned"
    ),
    db: Session = Depends(get_db),
) -> list[ProductSerial]:
    """Every unit of this product on record, and where each one is.

    A unit that has gone out keeps the invoice line it went out on, which is what makes
    a warranty claim traceable back to the sale.
    """
    org_id = _org_id(user)
    product = _owned(db, product_id, org_id)
    query = db.query(ProductSerial).filter(
        ProductSerial.organization_id == org_id, ProductSerial.product_id == product.id
    )
    if status_filter:
        query = query.filter(ProductSerial.status == status_filter)
    return query.order_by(ProductSerial.created_at, ProductSerial.serial_number).all()


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
    # Stock changes only through the stock workflows (adjustments, purchases, returns,
    # deliveries), which move warehouse stock and the movement log together and keep these
    # counters in step. Writing a counter here would report stock that orders cannot see.
    # Creating a product or a new variant still sets its opening stock (stock_service.on_hand).
    if "total_inventory" in data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="total_inventory cannot be changed by editing the product. Record a stock "
                   "adjustment instead (POST /warehouses/{warehouse_id}/stock/adjust).",
        )
    for v in data.get("variations") or []:
        existing = db.get(ProductVariant, v["id"]) if v.get("id") else None
        if (existing is not None and existing.product_id == product.id and "inventory" in v
                and v["inventory"] != (existing.inventory or 0)):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"The stock of existing variant '{existing.name}' cannot be changed by "
                       "editing the product. Record a stock adjustment for that variant instead "
                       "(POST /warehouses/{warehouse_id}/stock/adjust with its variant_id).",
            )
    if "category_id" in data:
        _validate_category(db, org_id, data["category_id"])
        _validate_top_level_category(db, data["category_id"])
    if "sub_category_id" in data:
        _validate_category(db, org_id, data["sub_category_id"])
    if "preferred_supplier_id" in data:
        _validate_supplier(db, org_id, data["preferred_supplier_id"])
    if "brand_id" in data:
        _validate_brand(db, org_id, data["brand_id"])
    if "category_id" in data or "sub_category_id" in data:
        _validate_classification(
            db,
            org_id,
            data.get("category_id", product.category_id),
            data.get("sub_category_id", product.sub_category_id),
        )
    if "product_type" in data:
        _validate_product_type(db, org_id, data["product_type"])
    # The Product Type must fit the sub category the product ends up with -- checked when
    # either of the two changes. Re-sending them unchanged leaves a product's existing
    # reference alone, including one whose sub category has since been deleted.
    new_type = data.get("product_type_id", product.product_type_id)
    new_sub = data.get("sub_category_id", product.sub_category_id)
    if new_type != product.product_type_id or new_sub != product.sub_category_id:
        _validate_product_type_ref(
            db, org_id, new_type, new_sub, newly_assigned=new_type != product.product_type_id,
        )

    effective_supplier_id = data.get("preferred_supplier_id", product.preferred_supplier_id)
    effective_brand_id = data.get("brand_id", product.brand_id)
    effective_category_id = data.get("category_id", product.category_id)

    if ("preferred_supplier_id" in data or "brand_id" in data) and effective_supplier_id and effective_brand_id:
        link = (
            db.query(SupplierBrand)
            .filter(
                SupplierBrand.organization_id == org_id,
                SupplierBrand.supplier_id == effective_supplier_id,
                SupplierBrand.brand_id == effective_brand_id,
            )
            .first()
        )
        if link is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Brand is not associated with the selected supplier.",
            )

    # Like the Product Type above: the brand must fit the category the product ends up
    # with, checked only when either actually changes -- re-sending them unchanged keeps a
    # historical (since unmapped or deactivated) brand valid for unrelated edits.
    if effective_brand_id != product.brand_id or effective_category_id != product.category_id:
        catalog_hierarchy_service.validate_category_brand(
            db, org_id, effective_category_id, effective_brand_id,
            newly_assigned_brand=effective_brand_id != product.brand_id,
        )

    variations = data.pop("variations", None)
    pricing_data = data.pop("pricing", None)
    data.pop("product_id", None)
    if data.get("has_variants") is None:
        data.pop("has_variants", None)  # NOT NULL column — never write a null into it
    if "status" in data:
        data["status"] = ProductStatus(data["status"]).value
    for field, value in data.items():
        setattr(product, field, value)

    # Keep is_active and status in sync when only one of the two was supplied.
    if "status" in data and "is_active" not in data:
        product.is_active = product.status == ProductStatus.ACTIVE.value
    elif "is_active" in data and "status" not in data:
        product.status = ProductStatus.ACTIVE.value if product.is_active else ProductStatus.INACTIVE.value

    if pricing_data is not None:
        if product.pricing is None:
            p_price = pricing_data.get("purchase_price", 0.0) if pricing_data.get("purchase_price") is not None else 0.0
            s_price = pricing_data.get("selling_price", 0.0) if pricing_data.get("selling_price") is not None else product.price or 0.0
            if not pricing_data.get("currency"):
                org = db.get(Organization, org_id)
                pricing_data["currency"] = (org.currency if org and org.currency else "INR")
            product.pricing = ProductPricing(
                product_id=product.id,
                purchase_price=p_price,
                selling_price=s_price,
                mrp=pricing_data.get("mrp"),
                wholesale_price=pricing_data.get("wholesale_price"),
                dealer_price=pricing_data.get("dealer_price"),
                discount_percent=pricing_data.get("discount_percent"),
                tax_inclusive=pricing_data.get("tax_inclusive", False),
                currency=pricing_data.get("currency"),
            )
        else:
            for p_field, p_value in pricing_data.items():
                if p_value is not None:
                    setattr(product.pricing, p_field, p_value)
        if product.pricing and product.pricing.selling_price is not None:
            product.price = product.pricing.selling_price

    # Variants are UPSERTED, never replaced: a variant named by id is updated in place
    # and keeps that id, one with no id is created, and one simply left out of the
    # payload is left alone. Editing a product must not silently delete a variant that
    # inventory, reservations, orders, deliveries, invoices or serial/batch records
    # already point at -- deleting one is its own explicit, guarded action below.
    if variations is not None:
        seen_ids: set[str] = set()
        new_variants_list: list[ProductVariant] = []
        for v in variations:
            # Validate payload shape using VariantIn
            vobj = VariantIn(**v)
            vid = vobj.id
            if vid:
                if vid in seen_ids:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="duplicate variant id in payload")
                seen_ids.add(vid)
                variant = db.get(ProductVariant, vid)
                if variant is None or variant.product_id != product.id:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="variant id not found or does not belong to this product")
                # Update only fields provided in the incoming dict. variant_code is
                # not a VariantIn field, so the business ID is never rewritten here, and
                # stock (checked unchanged above) is never written from here.
                columns = _variant_columns(v)
                columns.pop("inventory", None)
                for key, val in columns.items():
                    setattr(variant, key, val)
                new_variants_list.append(variant)
            else:
                # Create new variant (new UUID, next VAR code) and attach to the product
                variant = _new_variant(db, org_id, _variant_columns(v), product_id=product.id)
                new_variants_list.append(variant)

        # Whatever the payload did not mention stays exactly as it was: the variants
        # it named are in new_variants_list (updated in place, same ids), and these are
        # the rest.
        untouched = [v for v in product.variations if v.id not in seen_ids]
        product.variations = untouched + new_variants_list

        if "has_variants" not in data:
            product.has_variants = bool(product.variations)

    db.commit()
    db.refresh(product)
    return product


@router.delete("/{product_id}/variants/{variant_id}", status_code=status.HTTP_204_NO_CONTENT)

def delete_product_variant(
    product_id: str,
    variant_id: str,
    user: User = Depends(_delete),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> None:
    """Delete one variant of a product -- deliberately its own action.

    Editing a product never removes a variant, however short the `variations` list is,
    because a variant id is referenced by inventory, reservations, orders, deliveries,
    invoices and serial/batch records. Removing one that anything still points at would
    orphan those records, so this refuses with 409 and names what is holding it. A
    variant that is no longer wanted but already has history belongs deactivated on the
    product, not deleted.
    """
    org_id = _org_id(user)
    product = _owned(db, product_id, org_id)
    variant = db.get(ProductVariant, variant_id)
    if variant is None or variant.product_id != product.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Variant not found on this product"
        )

    used_by = _variant_in_use(db, variant.id)
    if used_by:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This variant is already referenced by "
                + ", ".join(sorted(set(used_by)))
                + ". Deleting it would orphan those records."
            ),
        )

    db.delete(variant)
    db.flush()
    product.has_variants = bool(
        [v for v in product.variations if v.id != variant.id]
    )
    db.commit()


@router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_product(
    product_id: str,
    user: User = Depends(_delete),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> None:
    product = _owned(db, product_id, _org_id(user))
    used_by = _product_in_use(db, product.id)
    if used_by:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Product '{product.name}' is already referenced by "
                + ", ".join(sorted(set(used_by)))
                + ". Deleting it would compromise historical records. Deactivate it instead."
            ),
        )
    db.delete(product)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Could not delete product '{product.name}' due to referencing records. Deactivate it instead.",
        )



# ------------------------------ Media & documents ------------------------------
# Every file slot is a plain column holding a URL, so it can also be set through
# PATCH /products/{id} with an external URL — preferable for anything large,
# since uploads here are inlined as base64 data: URLs (max 10 MB, no S3 yet).

_FILE_RULES: dict[ProductFileField, dict[str, bool]] = {
    ProductFileField.cover_image: {"allow_pdf": False},
    ProductFileField.product_video: {"allow_pdf": False, "allow_video": True},
    ProductFileField.product_catalog_brochure: {},
    ProductFileField.product_manual: {},
    ProductFileField.download_file: {"allow_any": True},
    ProductFileField.product_datasheet: {},
    ProductFileField.compliance_certificate: {},
    ProductFileField.warranty_document: {},
}

_MAX_ATTACHMENTS = 10


@router.post("/{product_id}/files/{field}", response_model=ProductOut)
def upload_product_file(
    request: Request,
    product_id: str,
    field: ProductFileField,
    file: UploadFile = File(...),
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Product:
    """Upload one of the product's single-file slots — cover image, video,
    brochure, manual, datasheet, compliance certificate, warranty document, or a
    digital product's download file. Replaces whatever was in that slot."""
    org_id = _org_id(user)
    product = _owned(db, product_id, org_id)
    old_url = getattr(product, field.value, None)
    url, _size = save_upload(db, org_id, file, request, **_FILE_RULES[field])
    setattr(product, field.value, url)
    if old_url and old_url != url:
        safe_delete_unreferenced_file(db, old_url, org_id)
    db.commit()
    db.refresh(product)
    return product


@router.delete("/{product_id}/files/{field}", response_model=ProductOut)
def clear_product_file(
    product_id: str,
    field: ProductFileField,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Product:
    """Empty one of the product's single-file slots."""
    org_id = _org_id(user)
    product = _owned(db, product_id, org_id)
    old_url = getattr(product, field.value, None)
    setattr(product, field.value, None)
    if old_url:
        safe_delete_unreferenced_file(db, old_url, org_id)
    db.commit()
    db.refresh(product)
    return product


def _attachments(product: Product) -> list[dict]:
    return list(product.other_attachments or [])


def _save_attachments(db: Session, product: Product, attachments: list[dict]) -> list[dict]:
    """Persist the attachment list. Reassigned wholesale — SQLAlchemy does not
    track in-place mutation of a JSON column."""
    product.other_attachments = attachments
    db.commit()
    db.refresh(product)
    return _attachments(product)


@router.get("/{product_id}/attachments", response_model=list[ProductAttachment])
def list_attachments(
    product_id: str, user: User = Depends(_view), db: Session = Depends(get_db)
) -> list[dict]:
    """Every "other attachment" on file for this product."""
    return _attachments(_owned(db, product_id, _org_id(user)))


@router.post(
    "/{product_id}/attachments",
    response_model=list[ProductAttachment],
    status_code=status.HTTP_201_CREATED,
)
def upload_attachments(
    request: Request,
    product_id: str,
    files: list[UploadFile] = File(..., description="One or more image / PDF files"),
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Upload one or more "other attachments" — appended to what is already on
    file, so uploading again does not replace the earlier ones. Returns the full list."""
    product = _owned(db, product_id, _org_id(user))
    attachments = _attachments(product)
    if len(attachments) + len(files) > _MAX_ATTACHMENTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"At most {_MAX_ATTACHMENTS} attachments per product "
                   f"({len(attachments)} already uploaded). Delete some first.",
        )
    for file in files:
        url, size = save_upload(db, _org_id(user), file, request)
        attachments.append(
            {
                "id": str(uuid.uuid4()),
                "name": file.filename or "attachment",
                "url": url,
                "content_type": file.content_type,
                "size": size,
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    return _save_attachments(db, product, attachments)


@router.delete("/{product_id}/attachments/{attachment_id}", response_model=list[ProductAttachment])
def delete_attachment(
    product_id: str,
    attachment_id: str,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Remove one attachment. Returns the remaining list."""
    org_id = _org_id(user)
    product = _owned(db, product_id, org_id)
    attachments = _attachments(product)
    att_to_delete = next((a for a in attachments if a.get("id") == attachment_id), None)
    if not att_to_delete:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found")
    remaining = [a for a in attachments if a.get("id") != attachment_id]
    result = _save_attachments(db, product, remaining)
    safe_delete_unreferenced_file(db, att_to_delete.get("url"), org_id)
    return result


@router.delete("/{product_id}/attachments", status_code=status.HTTP_204_NO_CONTENT)
def clear_attachments(
    product_id: str,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> None:
    """Remove every attachment on this product."""
    org_id = _org_id(user)
    product = _owned(db, product_id, org_id)
    atts_to_delete = list(_attachments(product))
    _save_attachments(db, product, [])
    for a in atts_to_delete:
        safe_delete_unreferenced_file(db, a.get("url"), org_id)
