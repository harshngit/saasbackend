from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, field_validator


class SupplierInvoiceItemIn(BaseModel):
    purchase_item_id: str
    product_id: str | None = None
    variant_id: str | None = None
    description: str | None = None
    billed_qty: int = Field(..., ge=1, description="Quantity billed on vendor invoice")
    unit_price: float = Field(..., ge=0.0, description="Unit price on vendor invoice")
    tax_rate: float = Field(default=0.0, ge=0.0)
    tax_amount: float = Field(default=0.0, ge=0.0)
    discount_amount: float = Field(default=0.0, ge=0.0)

    @field_validator("product_id", "variant_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v


class SupplierInvoiceItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    supplier_invoice_id: str
    purchase_item_id: str
    product_id: str | None = None
    variant_id: str | None = None
    description: str | None = None
    billed_qty: int
    unit_price: float
    tax_rate: float = 0.0
    tax_amount: float = 0.0
    discount_amount: float = 0.0
    line_total: float = 0.0


class SupplierInvoiceCreate(BaseModel):
    supplier_id: str
    purchase_id: str
    supplier_invoice_number: str = Field(..., min_length=1, max_length=100)
    supplier_invoice_date: datetime | None = None
    due_date: datetime | None = None
    notes: str | None = None
    attachment_url: str | None = None
    tax_amount: float = Field(default=0.0, ge=0.0)
    discount_amount: float = Field(default=0.0, ge=0.0)
    items: list[SupplierInvoiceItemIn]

    @field_validator("supplier_id", "purchase_id", "supplier_invoice_number", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v


class SupplierInvoiceUpdate(BaseModel):
    supplier_invoice_number: str | None = Field(default=None, max_length=100)
    supplier_invoice_date: datetime | None = None
    due_date: datetime | None = None
    notes: str | None = None
    attachment_url: str | None = None
    tax_amount: float | None = Field(default=None, ge=0.0)
    discount_amount: float | None = Field(default=None, ge=0.0)
    items: list[SupplierInvoiceItemIn] | None = None


class SupplierInvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    supplier_id: str
    purchase_id: str
    supplier_invoice_number: str
    supplier_invoice_date: datetime
    due_date: datetime | None = None
    status: str
    verification_status: str
    payment_status: str
    subtotal: float
    tax_amount: float
    discount_amount: float
    grand_total: float
    amount_paid: float
    outstanding_amount: float
    notes: str | None = None
    attachment_url: str | None = None
    created_by: str | None = None
    recorded_at: datetime | None = None
    recorded_by: str | None = None
    created_at: datetime
    updated_at: datetime
    items: list[SupplierInvoiceItemOut] = []
