from datetime import datetime

from app.schemas.choices import CustomerStatus
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator


class AssigneeBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str


class CustomerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    name: str
    business_name: str | None
    phone: str | None
    email: str | None
    gst_number: str | None
    billing_address: str | None
    delivery_address: str | None
    assigned_sales_officer_id: str | None
    assigned_sales_officer: AssigneeBrief | None
    credit_limit: float
    opening_balance: float
    total_billed: float
    total_received: float
    outstanding_balance: float
    category: str | None
    notes: str | None
    is_active: bool

    # Customer profile. These columns existed but were never exposed, so
    # `customer_id` looked absent to every caller.
    customer_id: str | None = None
    customer_since: datetime | None = None
    status: str | None = None
    customer_type: str | None = None
    primary_contact_person: str | None = None
    maps_latitude: float | None = None
    maps_longitude: float | None = None
    profile_image_id: str | None = None

    city: str | None = None
    last_order_date: datetime | None = None
    last_visit_date: datetime | None = None

    created_at: datetime
    updated_at: datetime


class CustomerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    business_name: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=20)
    email: EmailStr | None = None
    gst_number: str | None = Field(default=None, max_length=20)
    billing_address: str | None = None
    delivery_address: str | None = None
    assigned_sales_officer_id: str | None = None
    credit_limit: float = Field(default=0, ge=0)
    opening_balance: float = Field(default=0, ge=0)  # prior dues, if any
    category: str | None = Field(default=None, max_length=100)
    notes: str | None = None

    @field_validator("assigned_sales_officer_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v

    # Customer profile (customer_id is auto-generated, so not settable)
    customer_since: datetime | None = None
    status: CustomerStatus | None = Field(default=None, description="Active | Inactive | Blacklisted | Prospect")
    primary_contact_person: str | None = Field(default=None, max_length=150)
    maps_latitude: float | None = Field(default=None, ge=-90, le=90)
    maps_longitude: float | None = Field(default=None, ge=-180, le=180)


from app.schemas.payment_receipt import PaymentSplitIn, PaymentSplitOut


class CustomerPaymentCreate(BaseModel):
    amount: float = Field(gt=0)
    payment_mode: str = Field(default="cash", max_length=30)
    reference: str | None = Field(default=None, max_length=150)
    note: str | None = None
    order_id: str | None = None
    invoice_id: str | None = Field(
        default=None,
        description="Settle this invoice. Omit for an advance / on-account payment.",
    )
    received_on: datetime | None = None

    # Optional split payments breakdown
    splits: list[PaymentSplitIn] | None = Field(
        default=None, description="Optional multi-tender split payment breakdown"
    )

    # Method-specific details — which ones are relevant depends on payment_mode
    # (cash uses none of them). Never send the full card number or CVV.
    upi_id: str | None = Field(default=None, max_length=100, description="Payer's UPI id, for payment_mode='upi'")
    card_type: str | None = Field(default=None, max_length=30, description="e.g. Visa, Mastercard — for payment_mode='card'")
    card_last_four: str | None = Field(
        default=None, max_length=4, description="Last 4 digits only — never the full card number"
    )
    collection_instructions: str | None = Field(
        default=None, description="For payment_mode='cod' — where/how to collect"
    )

    @field_validator("card_last_four")
    @classmethod
    def _valid_card_last_four(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not (v.isdigit() and len(v) == 4):
            raise ValueError("card_last_four must be exactly 4 digits")
        return v


class PaymentInvoiceBrief(BaseModel):
    """The invoice a payment settled, resolved from invoice_id."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    invoice_number: str
    total: float
    amount_paid: float
    status: str


class CollectorBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    role: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _from_user_obj(cls, data: object) -> object:
        if hasattr(data, "id") and hasattr(data, "name"):
            user = data
            role_str = None
            role_val = getattr(user, "role", None)
            if role_val is not None:
                if hasattr(role_val, "value"):
                    role_val = role_val.value
                if role_val == "delivery_partner":
                    role_str = "Delivery Partner"
                elif role_val == "sales_officer":
                    role_str = "Sales Officer"
                elif role_val == "accountant":
                    role_str = "Accountant"
                elif role_val == "admin":
                    role_str = "Admin"
                elif role_val == "super_admin":
                    role_str = "Super Admin"
                else:
                    role_str = str(role_val)
            elif getattr(user, "role_detail", None) and getattr(user.role_detail, "name", None):
                role_str = user.role_detail.name
            elif getattr(user, "system_role", None):
                role_str = str(user.system_role)

            return {
                "id": str(user.id),
                "name": str(getattr(user, "display_name", None) or user.name),
                "role": role_str,
            }
        return data


class CustomerPaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    customer_id: str | None
    # Every payment is receipted, whichever surface took it, so the same row can be
    # handed to the customer as RCPT-0001 and shows up under /payment-receipts.
    receipt_number: str | None = None
    order_id: str | None
    invoice_id: str | None = None
    invoice: PaymentInvoiceBrief | None = None
    amount: float
    payment_mode: str
    amount_collected: float | None = None
    payment_method: str | None = None
    reference: str | None
    note: str | None
    received_on: datetime
    created_at: datetime
    date: datetime | None = None
    order_amount: float | None = None
    previous_pending: float | None = None
    remaining_receivable: float | None = None

    collected_by_user_id: str | None = None
    collector: CollectorBrief | None = None
    source: str | None = None
    status: str | None = None

    # Method-specific details, persisted alongside the payment.
    upi_id: str | None = None
    card_type: str | None = None
    card_last_four: str | None = None
    collection_instructions: str | None = None
    splits: list[PaymentSplitOut] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _derive_fields(cls, data: object) -> object:
        if hasattr(data, "id"):
            deliv_coll = getattr(data, "delivery_collection", None)
            if deliv_coll is not None:
                derived_status = getattr(deliv_coll, "reconciliation_status", "reconciled")
                if getattr(deliv_coll, "delivery_id", None):
                    derived_source = "delivery_cod"
                else:
                    derived_source = "field_collection"
            else:
                derived_status = "recorded"
                if getattr(data, "order_id", None):
                    derived_source = "sales_order"
                elif getattr(data, "invoice_id", None):
                    derived_source = "invoice"
                elif getattr(data, "receipt_number", None):
                    derived_source = "payment_receipt"
                else:
                    derived_source = "direct_payment"

            collector_obj = getattr(data, "collector", None)
            rec_on = getattr(data, "received_on", getattr(data, "created_at", None))

            return {
                "id": data.id,
                "customer_id": getattr(data, "customer_id", None),
                "receipt_number": getattr(data, "receipt_number", None),
                "order_id": getattr(data, "order_id", None),
                "invoice_id": getattr(data, "invoice_id", None),
                "invoice": getattr(data, "invoice", None),
                "amount": data.amount,
                "payment_mode": data.payment_mode,
                "amount_collected": getattr(data, "amount_collected", data.amount),
                "payment_method": getattr(data, "payment_method", data.payment_mode),
                "reference": getattr(data, "reference", None),
                "note": getattr(data, "note", None),
                "received_on": rec_on,
                "created_at": getattr(data, "created_at", None),
                "date": rec_on,
                "order_amount": getattr(data, "order_amount", None),
                "previous_pending": getattr(data, "previous_pending", None),
                "remaining_receivable": getattr(data, "remaining_receivable", None),
                "collected_by_user_id": getattr(data, "collected_by_user_id", None),
                "collector": collector_obj,
                "source": derived_source,
                "status": derived_status,
                "upi_id": getattr(data, "upi_id", None),
                "card_type": getattr(data, "card_type", None),
                "card_last_four": getattr(data, "card_last_four", None),
                "collection_instructions": getattr(data, "collection_instructions", None),
                "splits": getattr(data, "splits", []),
            }
        return data

    @model_validator(mode="after")
    def _populate_aliases(self) -> "CustomerPaymentOut":
        if self.amount_collected is None:
            self.amount_collected = self.amount
        if self.payment_method is None:
            self.payment_method = self.payment_mode
        if self.date is None:
            self.date = self.received_on
        return self


class CustomerUpdate(BaseModel):
    """Partial update — send only changed fields."""

    name: str | None = Field(default=None, min_length=1, max_length=150)
    business_name: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=20)
    email: EmailStr | None = None
    gst_number: str | None = Field(default=None, max_length=20)
    billing_address: str | None = None
    delivery_address: str | None = None
    assigned_sales_officer_id: str | None = None
    credit_limit: float | None = Field(default=None, ge=0)
    category: str | None = Field(default=None, max_length=100)
    notes: str | None = None
    is_active: bool | None = None

    @field_validator("assigned_sales_officer_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v

    # Customer profile (customer_id is auto-generated, so not settable)
    customer_since: datetime | None = None
    status: CustomerStatus | None = Field(default=None, description="Active | Inactive | Blacklisted | Prospect")
    primary_contact_person: str | None = Field(default=None, max_length=150)
    maps_latitude: float | None = Field(default=None, ge=-90, le=90)
    maps_longitude: float | None = Field(default=None, ge=-180, le=180)




class CustomerDocumentOut(BaseModel):
    """One uploaded customer document."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    document_type: str
    name: str
    content_type: str
    size: int
    url: str
    uploaded_at: datetime
