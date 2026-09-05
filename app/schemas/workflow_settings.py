"""Per-organization sales workflow and invoice template settings.

Both are partial updates: send only what you are changing. Anything you never set
reads back as the documented default, so a firm that has never opened these screens
still behaves predictably and a setting added later needs no migration.
"""

from typing import Annotated

from pydantic import BaseModel, BeforeValidator, Field

from app.core.workflow import INVOICE_TEMPLATES, PAPER_SIZES


def _lower(value: object) -> object:
    return value.strip().lower() if isinstance(value, str) else value


InvoiceTemplate = Annotated[
    str, BeforeValidator(_lower), Field(pattern=f"^({'|'.join(INVOICE_TEMPLATES)})$")
]
# Paper sizes are shown as A4 / A5 / thermal; accept any casing.
PaperSize = Annotated[str, Field(pattern="^([Aa]4|[Aa]5|[Tt]hermal)$")]


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
    """The firm's marks on its invoice.

    Both files are uploaded through the universal `POST /files/upload` and stored here
    by the id (or URL) it hands back. There is deliberately no separate logo or
    signature endpoint.
    """

    logo_file_id: str | None = Field(
        default=None, description="file_id from POST /files/upload — no separate logo endpoint")
    signature_file_id: str | None = Field(
        default=None,
        description="file_id from POST /files/upload — printed above the signatory line")
    primary_color: str | None = Field(
        default=None, max_length=9, description="Hex, e.g. #166534")


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


class InvoiceSettings(BaseModel):
    """The firm's invoice look. One record, applied to both PDF formats."""

    template: InvoiceTemplate = "classic"
    paper_size: PaperSize = "A4"
    branding: InvoiceBranding = Field(default_factory=InvoiceBranding)
    fields: InvoiceFields = Field(default_factory=InvoiceFields)
    terms: str | None = Field(default=None, max_length=2000)
    footer_text: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=2000)


class InvoiceSettingsUpdate(BaseModel):
    """Partial update. `branding` and `fields` merge key by key, so you can flip one
    toggle without resending the rest."""

    template: InvoiceTemplate | None = None
    paper_size: PaperSize | None = None
    branding: InvoiceBranding | None = None
    fields: InvoiceFields | None = None
    terms: str | None = Field(default=None, max_length=2000)
    footer_text: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=2000)
