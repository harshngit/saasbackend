from datetime import datetime

from app.schemas.choices import ApprovalStatus, PurchaseStatus, PurchaseType, ReceivingStatus
from pydantic import BaseModel, ConfigDict, Field, field_validator


class PurchaseItemIn(BaseModel):
    product_id: str
    variant_id: str | None = None
    quantity: int = Field(gt=0)
    purchase_price: float = Field(ge=0)
    discount: float = Field(default=0, ge=0)
    tax: float = Field(default=0, ge=0)

    @field_validator("variant_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v


class PurchaseItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    product_id: str | None
    variant_id: str | None
    product_name: str
    quantity: int
    purchase_price: float
    discount: float
    tax: float
    line_total: float


class SupplierBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str


class PurchaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    invoice_number: str
    supplier_id: str | None
    supplier: SupplierBrief | None
    invoice_date: datetime
    status: str
    payment_status: str
    subtotal: float
    discount: float
    tax: float
    total: float
    amount_paid: float
    notes: str | None
    attachment_url: str | None
    items: list[PurchaseItemOut]
    created_at: datetime
    updated_at: datetime

    # Sheet fields
    purchase_id: str | None = None
    purchase_number: str | None = None
    purchase_type: str | None = None
    purchase_date: datetime | None = None
    financial_year: str | None = None
    purchase_status: str | None = None
    billing_address: str | None = None
    warehouse_id: str | None = None
    receiving_status: str | None = None
    purchase_account_id: str | None = None
    approval_status: str | None = None


class PurchaseCreate(BaseModel):
    invoice_number: str = Field(min_length=1, max_length=60)
    supplier_id: str
    invoice_date: datetime | None = None
    discount: float = Field(default=0, ge=0)
    tax: float = Field(default=0, ge=0)
    notes: str | None = None
    attachment_url: str | None = None
    items: list[PurchaseItemIn] = Field(min_length=1)

    # Sheet fields (purchase_id / purchase_number are auto-generated)
    purchase_type: PurchaseType | None = Field(default=None, description="Purchase Order | Direct Purchase | Service Purchase | Asset Purchase")
    purchase_date: datetime | None = None
    financial_year: str | None = Field(default=None, max_length=20)
    purchase_status: PurchaseStatus | None = Field(default=None, description="Draft | Ordered | Received | Invoiced | Paid | Cancelled")
    billing_address: str | None = Field(default=None, description="Defaults to the supplier's address")
    warehouse_id: str | None = Field(default=None, description="Receiving warehouse")
    receiving_status: ReceivingStatus | None = Field(default=None, description="Pending | Partial | Completed")
    purchase_account_id: str | None = Field(default=None, description="Ledger account to post against")
    approval_status: ApprovalStatus | None = Field(default=None, description="Pending | Approved | Rejected")


class PurchaseUpdate(BaseModel):
    invoice_number: str | None = Field(default=None, max_length=60)
    invoice_date: datetime | None = None
    discount: float | None = Field(default=None, ge=0)
    tax: float | None = Field(default=None, ge=0)
    notes: str | None = None
    attachment_url: str | None = None
    items: list[PurchaseItemIn] | None = None


class PaymentStatusUpdate(BaseModel):
    payment_status: str = Field(description="unpaid | partial | paid")
    amount_paid: float | None = Field(default=None, ge=0)


class CancelBody(BaseModel):
    reason: str | None = None


class ReturnItem(BaseModel):
    product_id: str
    variant_id: str | None = None
    quantity: int = Field(gt=0)

    @field_validator("variant_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v


class PurchaseReturnBody(BaseModel):
    items: list[ReturnItem] = Field(min_length=1)
    reason: str | None = None
