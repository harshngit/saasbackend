import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.core.files import read_upload
from app.models import Category, Product, ProductVariant, Supplier, User
from app.schemas.category import BulkDelete, BulkDeleteResult
from app.schemas.product import (
    ProductAttachment,
    ProductCreate,
    ProductFileField,
    ProductListItem,
    ProductOut,
    ProductUpdate,
    VariantIn,
)

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


def _validate_supplier(db: Session, org_id: str, supplier_id: str | None) -> None:
    if supplier_id is None:
        return
    supplier = db.get(Supplier, supplier_id)
    if supplier is None or supplier.organization_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="preferred_supplier_id is not a supplier in your firm",
        )


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
    _validate_supplier(db, org_id, payload.preferred_supplier_id)
    data = payload.model_dump()
    variations = data.pop("variations")
    has_variants = data.pop("has_variants")
    product = Product(organization_id=org_id, **data)
    product.variations = _build_variants([VariantIn(**v) for v in variations])
    # Left unset, the toggle just follows whether variants were actually sent.
    product.has_variants = bool(variations) if has_variants is None else has_variants
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
    search: str | None = Query(default=None, description="matches name / sku / barcode / brand / vendor"),
    category_id: str | None = Query(default=None),
    preferred_supplier_id: str | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[Product]:
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
            )
        )
    if category_id is not None:
        query = query.filter(Product.category_id == category_id)
    if preferred_supplier_id is not None:
        query = query.filter(Product.preferred_supplier_id == preferred_supplier_id)
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
    if "preferred_supplier_id" in data:
        _validate_supplier(db, org_id, data["preferred_supplier_id"])
    variations = data.pop("variations", None)
    if data.get("has_variants") is None:
        data.pop("has_variants", None)  # NOT NULL column — never write a null into it
    for field, value in data.items():
        setattr(product, field, value)
    if variations is not None:  # full replace of the variant set
        product.variations = _build_variants([VariantIn(**v) for v in variations])
        if "has_variants" not in data:
            product.has_variants = bool(variations)
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
    product = _owned(db, product_id, _org_id(user))
    url, _size = read_upload(file, **_FILE_RULES[field])
    setattr(product, field.value, url)
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
    product = _owned(db, product_id, _org_id(user))
    setattr(product, field.value, None)
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
        url, size = read_upload(file)
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
    product = _owned(db, product_id, _org_id(user))
    attachments = _attachments(product)
    remaining = [a for a in attachments if a.get("id") != attachment_id]
    if len(remaining) == len(attachments):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found")
    return _save_attachments(db, product, remaining)


@router.delete("/{product_id}/attachments", status_code=status.HTTP_204_NO_CONTENT)
def clear_attachments(
    product_id: str,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> None:
    """Remove every attachment on this product."""
    _save_attachments(db, _owned(db, product_id, _org_id(user)), [])
