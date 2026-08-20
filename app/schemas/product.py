from datetime import datetime
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator


class VariantIn(BaseModel):
    id: str | None = None
    name: str = Field(min_length=1, max_length=150)
    sku: str | None = Field(default=None, max_length=100)
    barcode: str | None = Field(default=None, max_length=100)
    length: float | None = None
    width: float | None = None
    height: float | None = None
    weight: float | None = None
    price: float = Field(default=0, ge=0)
    inventory: int = Field(default=0, ge=0)


class VariantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    sku: str | None
    barcode: str | None
    length: float | None
    width: float | None
    height: float | None
    weight: float | None
    price: float
    inventory: int


class NamedRef(BaseModel):
    """A related record resolved from its id, so callers never look up a name."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str


class ProductAttachment(BaseModel):
    """One file in the product's "other attachments" multi-upload slot."""

    id: str
    name: str
    url: str
    content_type: str | None = None
    size: int | None = None
    uploaded_at: datetime | None = None


class ProductFileField(str, Enum):
    """The single-file slots on a product, uploadable via POST /products/{id}/files/{field}."""

    cover_image = "cover_image"
    product_video = "product_video"
    product_catalog_brochure = "product_catalog_brochure"
    product_manual = "product_manual"
    download_file = "download_file"
    product_datasheet = "product_datasheet"
    compliance_certificate = "compliance_certificate"
    warranty_document = "warranty_document"


class ProductPricingIn(BaseModel):
    purchase_price: float = 0.0
    selling_price: float = 0.0
    mrp: float | None = None
    wholesale_price: float | None = None
    dealer_price: float | None = None
    discount_percent: float | None = None
    tax_inclusive: bool = False
    currency: str | None = Field(default=None, max_length=10)


class ProductPricingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    purchase_price: float = 0.0
    selling_price: float = 0.0
    mrp: float | None = None
    wholesale_price: float | None = None
    dealer_price: float | None = None
    discount_percent: float | None = None
    tax_inclusive: bool = False
    currency: str | None = None


class ProductPricingUpdate(BaseModel):
    purchase_price: float | None = None
    selling_price: float | None = None
    mrp: float | None = None
    wholesale_price: float | None = None
    dealer_price: float | None = None
    discount_percent: float | None = None
    tax_inclusive: bool | None = None
    currency: str | None = Field(default=None, max_length=10)


class ProductProfileIn(BaseModel):
    """The Product Profile block — shared by create and update so both accept
    exactly the same fields. Everything here is optional; toggles default to a
    concrete value rather than null.

    `other_attachments` is deliberately absent: that slot is managed through
    /products/{id}/attachments, not by writing the whole list.
    """

    product_id: str | None = Field(default=None, max_length=50)

    # 1. Basic information
    barcode: str | None = Field(default=None, max_length=100)
    short_name: str | None = Field(default=None, max_length=100)
    sub_category: str | None = Field(default=None, max_length=150)
    manufacturer: str | None = Field(default=None, max_length=150)
    model_number: str | None = Field(default=None, max_length=100)

    # 2. Images & media — pass a URL / data: URL, or upload via /files/{field}
    product_video: str | None = None
    product_catalog_brochure: str | None = None
    product_manual: str | None = None

    # 3. Inventory
    inventory_tracking: bool = True
    opening_stock: int | None = Field(default=None, ge=0)
    minimum_stock_level: int | None = Field(default=None, ge=0)
    maximum_stock_level: int | None = Field(default=None, ge=0)
    reorder_level: int | None = Field(default=None, ge=0)
    reorder_quantity: int | None = Field(default=None, ge=0)
    bin_shelf_location: str | None = Field(default=None, max_length=100)
    batch_tracking: bool = False
    serial_number_tracking: bool = False
    expiry_tracking: bool = False

    # 4. Tax
    hsn_code: str | None = Field(
        default=None, max_length=20, description="HSN (goods) / SAC (services) code for GST invoices"
    )
    tax_rate: float | None = Field(
        default=None, ge=0, le=100,
        description="Tax %, snapshotted onto order and invoice lines")
    tax_category: str | None = Field(default=None, max_length=100)

    # 5. Purchase
    preferred_supplier_id: str | None = None
    supplier_product_code: str | None = Field(default=None, max_length=100)
    lead_time: str | None = Field(default=None, max_length=50, description='free text, e.g. "7 days"')
    minimum_order_quantity: int | None = Field(default=None, ge=0)
    purchase_unit: str | None = Field(default=None, max_length=50)

    # 6. Sales
    sales_unit: str | None = Field(default=None, max_length=50)
    commission_eligible: bool = False
    commission: float | None = Field(default=None, ge=0, le=100, description="percent")
    default_discount: float | None = Field(default=None, ge=0, le=100, description="percent")

    # 7. Physical specifications
    weight: float | None = Field(default=None, ge=0)
    length: float | None = Field(default=None, ge=0)
    width: float | None = Field(default=None, ge=0)
    height: float | None = Field(default=None, ge=0)
    volume: float | None = Field(default=None, ge=0)
    color: str | None = Field(default=None, max_length=50)
    material: str | None = Field(default=None, max_length=100)
    physical_size: str | None = Field(
        default=None,
        max_length=50,
        description='Physical size band (S/M/L). Not the variant "size" (250ml/1L) — that stays on the variant.',
    )

    # 8. Variants & attributes
    has_variants: bool | None = Field(
        default=None, description="Defaults to whether the product has variants"
    )
    variant_attributes: list[str] | None = Field(
        default=None, description='Attribute types the variants vary by, e.g. ["Color", "Size"]'
    )

    # 9. Digital product
    downloadable_product: bool = False
    download_file: str | None = None
    license_key_required: bool = False
    download_limit: int | None = Field(default=None, ge=0)

    # 10. Additional information
    warranty_period: str | None = Field(default=None, max_length=50)
    shelf_life: str | None = Field(default=None, max_length=50)
    country_of_origin: str | None = Field(default=None, max_length=100)
    launch_date: datetime | None = None
    end_of_life_date: datetime | None = None
    product_tags: list[str] | None = None
    notes: str | None = None

    # 11. Documents
    product_datasheet: str | None = None
    compliance_certificate: str | None = None
    warranty_document: str | None = None

    @field_validator("preferred_supplier_id", mode="before")
    @classmethod
    def _blank_supplier_to_none(cls, v: object) -> object:
        return None if v == "" else v


def _as_list(value: object) -> object:
    """JSON columns added to an already-populated DB come back null — read those as []."""
    return value or []


StringList = Annotated[list[str], BeforeValidator(_as_list)]
AttachmentList = Annotated[list[ProductAttachment], BeforeValidator(_as_list)]


class ProductOut(ProductProfileIn):
    """Full product detail (create/update/get-one responses)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    name: str
    description: str | None
    price: float
    pricing: ProductPricingOut | None = None
    cover_image: str | None
    images: StringList = Field(default_factory=list)
    product_type: str | None
    vendor: str | None
    brand: str | None
    sku: str | None
    category_id: str | None
    total_inventory: int
    total_stock: int
    is_active: bool
    variations: list[VariantOut]
    # Resolved from category_id / preferred_supplier_id.
    category: NamedRef | None = None
    supplier: NamedRef | None = None
    created_at: datetime
    updated_at: datetime

    # Nullable JSON columns, always surfaced as lists
    variant_attributes: StringList = Field(default_factory=list)
    product_tags: StringList = Field(default_factory=list)
    other_attachments: AttachmentList = Field(default_factory=list)
    has_variants: bool = False


class ProductListItem(BaseModel):
    """Lighter product shape for list responses. Omits `images` and every
    file slot (video / brochure / manual / datasheet / certificates /
    attachments) — those hold base64 data: URLs and would make a list of a few
    hundred products enormous. Fetch one product to get them."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    name: str
    description: str | None
    price: float
    pricing: ProductPricingOut | None = None
    cover_image: str | None
    product_type: str | None
    vendor: str | None
    brand: str | None
    sku: str | None
    category_id: str | None
    total_inventory: int
    total_stock: int
    is_active: bool
    variations: list[VariantOut]
    category: NamedRef | None = None
    supplier: NamedRef | None = None
    created_at: datetime

    # Profile fields that are cheap enough to carry in a list
    product_id: str | None = None
    barcode: str | None = None
    short_name: str | None = None
    sub_category: str | None = None
    manufacturer: str | None = None
    model_number: str | None = None
    inventory_tracking: bool = True
    minimum_stock_level: int | None = None
    reorder_level: int | None = None
    bin_shelf_location: str | None = None
    hsn_code: str | None = None
    tax_rate: float | None = None
    tax_category: str | None = None
    preferred_supplier_id: str | None = None
    sales_unit: str | None = None
    has_variants: bool = False
    physical_size: str | None = None
    country_of_origin: str | None = None
    product_tags: StringList = Field(default_factory=list)


class ProductCreate(ProductProfileIn):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    pricing: ProductPricingIn = Field(default_factory=ProductPricingIn, description="Pricing information object")
    cover_image: str | None = None
    images: list[str] = Field(default_factory=list)
    product_type: str | None = Field(default=None, max_length=100)
    vendor: str | None = Field(default=None, max_length=150)
    brand: str | None = Field(default=None, max_length=150)
    sku: str | None = Field(default=None, max_length=100)
    category_id: str | None = None
    total_inventory: int = Field(default=0, ge=0)
    variations: list[VariantIn] = Field(default_factory=list)

    @field_validator("category_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v


class ProductUpdate(ProductProfileIn):
    """Partial update. If `variations` is provided, it fully replaces the variant set."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    pricing: ProductPricingUpdate | None = None
    cover_image: str | None = None
    images: list[str] | None = None
    product_type: str | None = Field(default=None, max_length=100)
    vendor: str | None = Field(default=None, max_length=150)
    brand: str | None = Field(default=None, max_length=150)
    sku: str | None = Field(default=None, max_length=100)
    category_id: str | None = None
    total_inventory: int | None = Field(default=None, ge=0)
    is_active: bool | None = None
    variations: list[VariantIn] | None = None

    @field_validator("category_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v
