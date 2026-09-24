"""Per-organization sales workflow and invoice template settings.

Both are partial updates: send only what you are changing. Anything you never set
reads back as the documented default, so a firm that has never opened these screens
still behaves predictably and a setting added later needs no migration.
"""

from typing import Annotated

from pydantic import BaseModel, BeforeValidator, Field, field_validator

from app.core.workflow import (
    ALLOWED_ITEM_COLUMNS,
    FONT_FAMILIES,
    INVOICE_TEMPLATES,
    PAPER_SIZES,
    THERMAL_PAPER_WIDTHS,
)


def _lower(value: object) -> object:
    return value.strip().lower() if isinstance(value, str) else value


def _font_title(value: object) -> object:
    if isinstance(value, str):
        v = value.strip().title()
        return v
    return value


InvoiceTemplate = Annotated[
    str, BeforeValidator(_lower), Field(pattern=f"^({'|'.join(INVOICE_TEMPLATES)})$")
]
# Paper sizes are shown as A4 / A5 / thermal; accept any casing.
PaperSize = Annotated[str, Field(pattern="^([Aa]4|[Aa]5|[Tt]hermal)$")]
FontFamily = Annotated[
    str,
    BeforeValidator(_font_title),
    Field(pattern=f"^({'|'.join(FONT_FAMILIES)})$"),
]
RegularPaperSize = Annotated[str, Field(pattern="^([Aa]4|[Aa]5)$")]
RegularOrientation = Annotated[str, BeforeValidator(_lower), Field(pattern="^(portrait|landscape)$")]
ThermalPaperWidth = Annotated[
    str,
    BeforeValidator(_lower),
    Field(pattern=f"^({'|'.join(THERMAL_PAPER_WIDTHS)})$"),
]


class SalesWorkflowSettings(BaseModel):
    """How this firm wants the sales flow to behave."""

    order_requires_approval: bool = Field(
        default=False,
        description="Legacy setting kept for schema backward compatibility. In the normal "
                    "workflow, every confirmed order moves directly to confirmed.",
    )
    reserve_stock_on_order: bool = Field(
        default=True,
        description="Hold stock when the order is placed. On-hand only drops when a "
                    "vehicle is actually loaded.",
    )
    allow_partial_delivery: bool = True
    allow_backorder: bool = Field(
        default=False, description="Allow placing an order the warehouse cannot cover")
    allow_direct_invoice: bool = Field(
        default=True, description="Allow quick billing with no order or delivery")
    credit_limit_action: Annotated[
        str, BeforeValidator(_lower), Field(pattern="^(warn|block|ignore)$")
    ] = "warn"
    delivery_collection_allowed: bool = True
    draft_orders_enabled: bool = False
    partial_delivery_invoice_mode: Annotated[
        str, BeforeValidator(_lower), Field(pattern="^(per_delivery|after_full_order)$")
    ] = Field(
        default="per_delivery",
        description="per_delivery bills each delivery as it happens; after_full_order "
                    "waits and bills the whole order once",
    )


class SalesWorkflowSettingsUpdate(BaseModel):
    """Partial update — only the settings you send change."""

    order_requires_approval: bool | None = None
    reserve_stock_on_order: bool | None = None
    allow_partial_delivery: bool | None = None
    allow_backorder: bool | None = None
    allow_direct_invoice: bool | None = None
    credit_limit_action: Annotated[
        str, BeforeValidator(_lower), Field(pattern="^(warn|block|ignore)$")
    ] | None = None
    delivery_collection_allowed: bool | None = None
    draft_orders_enabled: bool | None = None
    partial_delivery_invoice_mode: Annotated[
        str, BeforeValidator(_lower), Field(pattern="^(per_delivery|after_full_order)$")
    ] | None = None


class InvoiceBranding(BaseModel):
    """The firm's marks on its invoice."""

    logo_file_id: str | None = Field(
        default=None, description="file_id from POST /files/upload — no separate logo endpoint")
    signature_file_id: str | None = Field(
        default=None,
        description="file_id from POST /files/upload — printed above the signatory line")
    stamp_file_id: str | None = Field(
        default=None,
        description="file_id from POST /files/upload — printed alongside signatory line")
    payment_qr_file_id: str | None = Field(
        default=None,
        description="file_id from POST /files/upload — invoice payment QR code")
    primary_color: str | None = Field(
        default=None, max_length=9, description="Hex, e.g. #166534")


class InvoiceBrandingUpdate(BaseModel):
    """Partial update for invoice branding."""

    logo_file_id: str | None = None
    signature_file_id: str | None = None
    stamp_file_id: str | None = None
    payment_qr_file_id: str | None = None
    primary_color: str | None = Field(default=None, max_length=9)


class InvoiceFields(BaseModel):
    """Which blocks the printed invoice shows."""

    show_company_gstin: bool = True
    show_customer_gstin: bool = True
    show_billing_address: bool = True
    show_shipping_address: bool = True
    show_hsn_sac: bool = True
    show_mrp: bool = False
    show_discount: bool = True
    show_tax_rate: bool = True
    show_tax_amount: bool = True
    show_batch_number: bool = False
    show_expiry_date: bool = False
    show_bank_details: bool = True
    show_upi_qr: bool = True
    show_terms: bool = True
    show_signature: bool = True


class InvoiceFieldsUpdate(BaseModel):
    """Partial update for invoice fields."""

    show_company_gstin: bool | None = None
    show_customer_gstin: bool | None = None
    show_billing_address: bool | None = None
    show_shipping_address: bool | None = None
    show_hsn_sac: bool | None = None
    show_mrp: bool | None = None
    show_discount: bool | None = None
    show_tax_rate: bool | None = None
    show_tax_amount: bool | None = None
    show_batch_number: bool | None = None
    show_expiry_date: bool | None = None
    show_bank_details: bool | None = None
    show_upi_qr: bool | None = None
    show_terms: bool | None = None
    show_signature: bool | None = None


class TypographySettings(BaseModel):
    """Font family and sizing configuration for invoice rendering."""

    font_family: FontFamily = "Helvetica"
    heading_size: int = Field(default=16, ge=10, le=28)
    body_size: int = Field(default=9, ge=7, le=16)
    table_size: int = Field(default=8, ge=6, le=14)


class TypographySettingsUpdate(BaseModel):
    """Partial update for typography."""

    font_family: FontFamily | None = None
    heading_size: int | None = Field(default=None, ge=10, le=28)
    body_size: int | None = Field(default=None, ge=7, le=16)
    table_size: int | None = Field(default=None, ge=6, le=14)


class BusinessDetailsSettings(BaseModel):
    """Visibility toggles for business information."""

    show_business_name: bool = True
    show_logo: bool = True
    show_address: bool = True
    show_phone: bool = True
    show_email: bool = True
    show_gstin: bool = True
    show_pan: bool = False


class BusinessDetailsSettingsUpdate(BaseModel):
    """Partial update for business details."""

    show_business_name: bool | None = None
    show_logo: bool | None = None
    show_address: bool | None = None
    show_phone: bool | None = None
    show_email: bool | None = None
    show_gstin: bool | None = None
    show_pan: bool | None = None


class InvoiceDetailsSettings(BaseModel):
    """Visibility toggles for invoice header details."""

    show_invoice_number: bool = True
    show_invoice_date: bool = True
    show_due_date: bool = True
    show_order_reference: bool = True
    show_po_number: bool = False
    show_eway_bill_number: bool = False
    show_vehicle_number: bool = False


class InvoiceDetailsSettingsUpdate(BaseModel):
    """Partial update for invoice details."""

    show_invoice_number: bool | None = None
    show_invoice_date: bool | None = None
    show_due_date: bool | None = None
    show_order_reference: bool | None = None
    show_po_number: bool | None = None
    show_eway_bill_number: bool | None = None
    show_vehicle_number: bool | None = None


class PartyDetailsSettings(BaseModel):
    """Visibility toggles for customer / party details."""

    show_customer_name: bool = True
    show_customer_gstin: bool = True
    show_billing_address: bool = True
    show_shipping_address: bool = True
    show_customer_phone: bool = True


class PartyDetailsSettingsUpdate(BaseModel):
    """Partial update for party details."""

    show_customer_name: bool | None = None
    show_customer_gstin: bool | None = None
    show_billing_address: bool | None = None
    show_shipping_address: bool | None = None
    show_customer_phone: bool | None = None


class ItemTableSettings(BaseModel):
    """Item table column visibility and column ordering."""

    show_product_image: bool = False
    show_description: bool = False
    columns: list[str] = Field(
        default_factory=lambda: [
            "product",
            "hsn_sac",
            "quantity",
            "rate",
            "amount",
        ]
    )

    @field_validator("columns")
    @classmethod
    def validate_columns(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("Item table columns list cannot be empty")
        if len(v) > 5:
            raise ValueError("Item table cannot have more than 5 columns")
        if len(v) != len(set(v)):
            raise ValueError("Duplicate columns are not allowed in item table")
        for col in v:
            if col not in ALLOWED_ITEM_COLUMNS:
                raise ValueError(
                    f"Invalid item table column '{col}'. Allowed columns: {list(ALLOWED_ITEM_COLUMNS)}"
                )
        if "product" not in v:
            raise ValueError("The 'product' column is mandatory and cannot be removed")
        if "amount" not in v:
            raise ValueError("The 'amount' column is mandatory and cannot be removed")
        return v


class ItemTableSettingsUpdate(BaseModel):
    """Partial update for item table settings."""

    show_product_image: bool | None = None
    show_description: bool | None = None
    columns: list[str] | None = None

    @field_validator("columns")
    @classmethod
    def validate_columns(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        if not v:
            raise ValueError("Item table columns list cannot be empty")
        if len(v) > 5:
            raise ValueError("Item table cannot have more than 5 columns")
        if len(v) != len(set(v)):
            raise ValueError("Duplicate columns are not allowed in item table")
        for col in v:
            if col not in ALLOWED_ITEM_COLUMNS:
                raise ValueError(
                    f"Invalid item table column '{col}'. Allowed columns: {list(ALLOWED_ITEM_COLUMNS)}"
                )
        if "product" not in v:
            raise ValueError("The 'product' column is mandatory and cannot be removed")
        if "amount" not in v:
            raise ValueError("The 'amount' column is mandatory and cannot be removed")
        return v


class PaymentDetailsSettings(BaseModel):
    """Visibility toggles for payment instructions on the invoice."""

    show_bank_details: bool = True
    show_upi_qr: bool = True


class PaymentDetailsSettingsUpdate(BaseModel):
    """Partial update for payment details."""

    show_bank_details: bool | None = None
    show_upi_qr: bool | None = None


class FooterSettings(BaseModel):
    """Footer visibility and text content."""

    show_terms: bool = True
    show_signature: bool = True
    show_stamp: bool = True
    terms: str | None = Field(default=None, max_length=2000)
    footer_text: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=2000)


class FooterSettingsUpdate(BaseModel):
    """Partial update for footer settings."""

    show_terms: bool | None = None
    show_signature: bool | None = None
    show_stamp: bool | None = None
    terms: str | None = Field(default=None, max_length=2000)
    footer_text: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=2000)


class RegularPrintSettings(BaseModel):
    """Page layout, size, orientation, and margin settings for regular printing."""

    layout: str = "standard"
    paper_size: RegularPaperSize = "A4"
    orientation: RegularOrientation = "portrait"
    margin_top: float = Field(default=10.0, ge=0.0, le=50.0)
    margin_right: float = Field(default=10.0, ge=0.0, le=50.0)
    margin_bottom: float = Field(default=10.0, ge=0.0, le=50.0)
    margin_left: float = Field(default=10.0, ge=0.0, le=50.0)


class RegularPrintSettingsUpdate(BaseModel):
    """Partial update for regular print settings."""

    layout: str | None = None
    paper_size: RegularPaperSize | None = None
    orientation: RegularOrientation | None = None
    margin_top: float | None = Field(default=None, ge=0.0, le=50.0)
    margin_right: float | None = Field(default=None, ge=0.0, le=50.0)
    margin_bottom: float | None = Field(default=None, ge=0.0, le=50.0)
    margin_left: float | None = Field(default=None, ge=0.0, le=50.0)


class ThermalPrintSettings(BaseModel):
    """Thermal printer configuration settings."""

    layout: str = "standard"
    paper_width: ThermalPaperWidth = "80mm"
    printing_type: str = "text"
    bold_text: bool = True
    auto_cut: bool = False
    open_cash_drawer: bool = False
    extra_lines: int = Field(default=0, ge=0, le=20)
    copies: int = Field(default=1, ge=1, le=10)


class ThermalPrintSettingsUpdate(BaseModel):
    """Partial update for thermal print settings."""

    layout: str | None = None
    paper_width: ThermalPaperWidth | None = None
    printing_type: str | None = None
    bold_text: bool | None = None
    auto_cut: bool | None = None
    open_cash_drawer: bool | None = None
    extra_lines: int | None = Field(default=None, ge=0, le=20)
    copies: int | None = Field(default=None, ge=1, le=10)


class InvoiceSettings(BaseModel):
    """The firm's invoice look. One record, applied across invoice generation."""

    template: InvoiceTemplate = "classic"
    paper_size: PaperSize = "A4"
    branding: InvoiceBranding = Field(default_factory=InvoiceBranding)
    fields: InvoiceFields = Field(default_factory=InvoiceFields)
    terms: str | None = Field(default=None, max_length=2000)
    footer_text: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=2000)
    typography: TypographySettings = Field(default_factory=TypographySettings)
    business_details: BusinessDetailsSettings = Field(default_factory=BusinessDetailsSettings)
    invoice_details: InvoiceDetailsSettings = Field(default_factory=InvoiceDetailsSettings)
    party_details: PartyDetailsSettings = Field(default_factory=PartyDetailsSettings)
    item_table: ItemTableSettings = Field(default_factory=ItemTableSettings)
    payment_details: PaymentDetailsSettings = Field(default_factory=PaymentDetailsSettings)
    footer: FooterSettings = Field(default_factory=FooterSettings)
    regular_print: RegularPrintSettings = Field(default_factory=RegularPrintSettings)
    thermal_print: ThermalPrintSettings = Field(default_factory=ThermalPrintSettings)


class InvoiceSettingsUpdate(BaseModel):
    """Partial update. Nested blocks merge key by key, so you can flip one
    toggle without resending the rest."""

    template: InvoiceTemplate | None = None
    paper_size: PaperSize | None = None
    branding: InvoiceBrandingUpdate | None = None
    fields: InvoiceFieldsUpdate | None = None
    terms: str | None = Field(default=None, max_length=2000)
    footer_text: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=2000)
    typography: TypographySettingsUpdate | None = None
    business_details: BusinessDetailsSettingsUpdate | None = None
    invoice_details: InvoiceDetailsSettingsUpdate | None = None
    party_details: PartyDetailsSettingsUpdate | None = None
    item_table: ItemTableSettingsUpdate | None = None
    payment_details: PaymentDetailsSettingsUpdate | None = None
    footer: FooterSettingsUpdate | None = None
    regular_print: RegularPrintSettingsUpdate | None = None
    thermal_print: ThermalPrintSettingsUpdate | None = None
