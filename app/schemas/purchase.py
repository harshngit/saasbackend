from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from app.schemas.choices import ApprovalStatus, PurchaseStatus, PurchaseType, ReceivingStatus


class PurchaseItemIn(BaseModel):
    product_id: str
    variant_id: str | None = None
    product_code: str | None = None
    barcode: str | None = None
    description: str | None = None
    warehouse_id: str | None = None
    unit_of_measure_uom: str | None = None
    quantity: int = Field(gt=0)
    purchase_price: float = Field(ge=0, description="Unit cost")
    discount_percent: float = Field(default=0.0, ge=0, le=100)
    discount: float = Field(default=0.0, ge=0)
    tax_rate: float = Field(default=0.0, ge=0, le=100)
    tax: float = Field(default=0.0, ge=0)
    batch_number: str | None = None
    serial_numbers: list[str] | None = None
    expiry_date: datetime | None = None

    @field_validator("variant_id", "warehouse_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v


class PurchaseItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    product_id: str | None
    variant_id: str | None
    product_code: str | None = None
    barcode: str | None = None
    description: str | None = None
    warehouse_id: str | None = None
    product_name: str
    quantity: int
    purchase_price: float
    discount_percent: float = 0.0
    discount: float
    tax_rate: float = 0.0
    tax: float
    line_total: float
    unit_of_measure_uom: str | None = None
    batch_number: str | None = None
    serial_numbers: list[str] | None = None
    expiry_date: datetime | None = None


class SupplierBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    contact_person: str | None = None
    phone: str | None = None
    email: str | None = None
    gst_number: str | None = None
    address: str | None = None


class PurchaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    invoice_number: str
    supplier_id: str | None
    supplier: SupplierBrief | None = None
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
    items: list[PurchaseItemOut] = []
    created_at: datetime
    updated_at: datetime

    # 1. Basic Information
    purchase_id: str | None = None
    purchase_number: str | None = None
    purchase_type: str | None = None
    purchase_date: datetime | None = None
    financial_year: str | None = None
    purchase_status: str | None = None
    reference_number: str | None = None

    # 2. Supplier Details
    contact_person: str | None = None
    mobile_number: str | None = None
    email_address: str | None = None
    payee_gstin: str | None = None
    billing_address: str | None = None
    shipping_address: str | None = None

    # 4. Totals (Additional charges)
    freight_charges: float = 0.0
    packing_charges: float = 0.0
    insurance_charges: float = 0.0
    other_charges: float = 0.0
    round_off: float = 0.0

    # 5. Goods Receipt
    grn_number: str | None = None
    received_date: datetime | None = None
    warehouse_id: str | None = None
    received_by: str | None = None
    receiving_status: str | None = None

    # 6. Payment Details
    payment_method: str | None = None
    payment_terms: str | None = None
    due_date: datetime | None = None
    payment_reference: str | None = None

    # 7. Accounting
    purchase_account_id: str | None = None
    tax_category: str | None = None
    cost_center_id: str | None = None
    project_id: str | None = None

    # 8. Approval
    requested_by: str | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    approval_status: str | None = None
    approval_remarks: str | None = None

    # 9. Documents
    supplier_quotation_url: str | None = None
    purchase_order_url: str | None = None
    supplier_invoice_url: str | None = None
    delivery_challan_url: str | None = None
    supporting_documents: list[dict] | None = None

    # 10. Additional Info
    terms_and_conditions: str | None = None
    internal_remarks: str | None = None
    tags: list[str] | None = None

    @computed_field
    def outstanding_balance(self) -> float:
        return round(max(self.total - (self.amount_paid or 0.0), 0.0), 2)


class PurchaseCreate(BaseModel):
    invoice_number: str = Field(min_length=1, max_length=60)
    supplier_id: str
    invoice_date: datetime | None = None
    discount: float = Field(default=0, ge=0)
    tax: float = Field(default=0, ge=0)
    notes: str | None = None
    attachment_url: str | None = None
    items: list[PurchaseItemIn] = Field(min_length=1)

    # 1. Basic Information
    purchase_type: PurchaseType | None = Field(
        default=None,
        description="Purchase Order | Direct Purchase | Service Purchase | Asset Purchase",
    )
    purchase_date: datetime | None = None
    financial_year: str | None = Field(default=None, max_length=20)
    purchase_status: PurchaseStatus | None = Field(
        default=None, description="Draft | Ordered | Received | Invoiced | Paid | Cancelled"
    )
    reference_number: str | None = Field(default=None, max_length=100)

    # 2. Supplier Details
    contact_person: str | None = None
    mobile_number: str | None = None
    email_address: str | None = None
    payee_gstin: str | None = None
    billing_address: str | None = Field(default=None, description="Defaults to the supplier's address")
    shipping_address: str | None = None

    # 4. Totals (Additional charges)
    freight_charges: float = Field(default=0.0, ge=0)
    packing_charges: float = Field(default=0.0, ge=0)
    insurance_charges: float = Field(default=0.0, ge=0)
    other_charges: float = Field(default=0.0, ge=0)
    round_off: float = Field(default=0.0)

    # 5. Goods Receipt
    grn_number: str | None = None
    received_date: datetime | None = None
    warehouse_id: str | None = Field(default=None, description="Receiving warehouse")
    received_by: str | None = None
    receiving_status: ReceivingStatus | None = Field(
        default=None, description="Pending | Partial | Completed"
    )

    # 6. Payment Details
    payment_method: str | None = None
    payment_terms: str | None = None
    due_date: datetime | None = None
    amount_paid: float = Field(default=0.0, ge=0)
    payment_reference: str | None = None

    # 7. Accounting
    purchase_account_id: str | None = Field(default=None, description="Ledger account to post against")
    tax_category: str | None = None
    cost_center_id: str | None = None
    project_id: str | None = None

    # 8. Approval
    requested_by: str | None = None
    approval_status: ApprovalStatus | None = Field(default=None, description="Pending | Approved | Rejected")

    # 9. Documents
    supplier_quotation_url: str | None = None
    purchase_order_url: str | None = None
    supplier_invoice_url: str | None = None
    delivery_challan_url: str | None = None
    supporting_documents: list[dict] | None = None

    # 10. Additional Info
    terms_and_conditions: str | None = None
    internal_remarks: str | None = None
    tags: list[str] | None = None


class PurchaseUpdate(BaseModel):
    invoice_number: str | None = Field(default=None, max_length=60)
    invoice_date: datetime | None = None
    discount: float | None = Field(default=None, ge=0)
    tax: float | None = Field(default=None, ge=0)
    notes: str | None = None
    attachment_url: str | None = None
    items: list[PurchaseItemIn] | None = None

    # Optional updates for extended fields
    purchase_type: str | None = None
    purchase_date: datetime | None = None
    financial_year: str | None = None
    purchase_status: str | None = None
    reference_number: str | None = None
    contact_person: str | None = None
    mobile_number: str | None = None
    email_address: str | None = None
    payee_gstin: str | None = None
    billing_address: str | None = None
    shipping_address: str | None = None
    freight_charges: float | None = Field(default=None, ge=0)
    packing_charges: float | None = Field(default=None, ge=0)
    insurance_charges: float | None = Field(default=None, ge=0)
    other_charges: float | None = Field(default=None, ge=0)
    round_off: float | None = None
    warehouse_id: str | None = None
    receiving_status: str | None = None
    payment_method: str | None = None
    payment_terms: str | None = None
    due_date: datetime | None = None
    payment_reference: str | None = None
    purchase_account_id: str | None = None
    tax_category: str | None = None
    cost_center_id: str | None = None
    project_id: str | None = None
    supplier_quotation_url: str | None = None
    purchase_order_url: str | None = None
    supplier_invoice_url: str | None = None
    delivery_challan_url: str | None = None
    supporting_documents: list[dict] | None = None
    terms_and_conditions: str | None = None
    internal_remarks: str | None = None
    tags: list[str] | None = None


class PaymentStatusUpdate(BaseModel):
    payment_status: str = Field(description="unpaid | partial | paid")
    amount_paid: float | None = Field(default=None, ge=0)
    payment_method: str | None = None
    payment_reference: str | None = None


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
