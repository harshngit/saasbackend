from datetime import datetime
from app.schemas.choices import InvoicePaymentStatus, SalesStatus, SalesType
from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator


class InvoiceItemIn(BaseModel):
    """One line of a directly billed sale.

    Send `tax_rate` as a percentage and the tax amount is worked out from the line.
    Send neither and the product's own rate applies, so a counter sale is taxed the
    same as an order for the same goods. `tax` as a flat amount is still accepted for
    older clients.
    """

    product_id: str
    variant_id: str | None = None
    quantity: int = Field(gt=0)
    unit_price: float | None = Field(default=None, ge=0)
    discount: float = Field(default=0, ge=0)
    tax_rate: float | None = Field(
        default=None, ge=0, le=100, description="Tax % on this line. Defaults to the product's rate"
    )
    tax: float = Field(default=0, ge=0, description="Flat tax amount. Ignored when tax_rate applies")

    @field_validator("variant_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v


class WalkInCustomer(BaseModel):
    """The buyer at the counter who is not worth a customer record.

    Whatever they gave is snapshotted onto the invoice; no customer is created and no
    receivable ledger is opened. A walk-in sale that is not fully paid still shows its
    own balance on the invoice.
    """

    name: str | None = Field(default=None, max_length=200)
    mobile_number: str | None = Field(default=None, max_length=20)


class InvoicePaymentIn(BaseModel):
    """The money taken at the counter, settled with the invoice in one transaction."""

    payment_method: str | None = Field(default=None, max_length=30, description="cash | upi | card | …")
    amount: float = Field(gt=0)
    transaction_reference: str | None = Field(default=None, max_length=150)
    received_on: datetime | None = None


class InvoiceItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    product_id: str | None
    variant_id: str | None
    product_name: str
    hsn_code: str | None = None
    quantity: int
    unit_price: float
    discount: float
    tax: float = Field(description="Tax amount on the line")
    tax_rate: float | None = Field(default=None, description="The rate it was billed at, as a %")
    line_total: float
    delivery_item_id: str | None = None
    order_item_id: str | None = None


class CustomerBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    business_name: str | None


class InvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    invoice_number: str
    order_id: str | None
    customer_id: str | None
    customer: CustomerBrief | None
    walk_in_name: str | None = None
    walk_in_phone: str | None = None
    delivery_id: str | None = None
    invoice_date: datetime
    due_date: datetime | None = None
    status: str
    subtotal: float
    discount: float
    tax: float
    additional_charges: float | None = None
    round_off: float | None = None
    total: float
    amount_paid: float
    notes: str | None
    is_credit_note: bool
    credit_note_reason: str | None
    items: list[InvoiceItemOut]
    created_at: datetime
    updated_at: datetime

    # Full Invoice sheet fields
    sales_id: str | None = None
    sales_type: str | None = None
    sales_date: datetime | None = None
    sales_status: str | None = None
    invoice_status: str | None = None
    payment_status: str | None = None
    billing_address: str | None = None

    @computed_field(description="What is still owed on this invoice")
    @property
    def outstanding_amount(self) -> float:
        return round((self.total or 0) - (self.amount_paid or 0), 2)


class InvoiceFromDelivery(BaseModel):
    """Bill an order for what a delivery actually handed over.

    Send `delivery_id` and the invoice is built from that delivery's **delivered**
    quantities, at the rates and taxes the order line agreed. Send nothing to bill the
    whole order as ordered — the older behaviour, still there for an order with no
    delivery behind it.

    Which of those a firm wants for a part-delivered order is its
    `partial_delivery_invoice_mode` setting: `per_delivery` bills each delivery as it
    happens, `after_full_order` waits until everything has been delivered and bills
    once.
    """

    delivery_id: str | None = None


class InvoiceCreate(BaseModel):
    """Bill a sale directly — the retail counter, a shop sale, a walk-in buyer.

    No sales order and no delivery are involved: stock leaves the warehouse as the
    invoice is raised. Send `payment` and the sale is settled and receipted in the
    same transaction; leave it out and the invoice stands unpaid as a receivable.

    The buyer is either a `customer_id` or a `walk_in_customer` block. One of the two
    is needed — an invoice with neither belongs to nobody.
    """

    customer_id: str | None = None
    walk_in_customer: WalkInCustomer | None = None
    warehouse_id: str | None = Field(
        default=None, description="Which warehouse the goods leave. Defaults to the firm's default"
    )
    payment: InvoicePaymentIn | None = Field(
        default=None, description="Money taken now. Omit for a credit sale"
    )
    invoice_date: datetime | None = None
    discount: float = Field(default=0, ge=0)
    tax: float = Field(default=0, ge=0)
    additional_charges: float | None = Field(
        default=None, description="Freight, packing or delivery fee added to the total"
    )
    round_off: float | None = Field(
        default=None, description="Paise rounding, positive or negative, applied to the total"
    )
    notes: str | None = None
    items: list[InvoiceItemIn] = Field(min_length=1)

    # Full Invoice sheet fields (sales_id / invoice_number are auto-generated)
    sales_type: SalesType | None = Field(default=None, description="Quotation | Sales Order | Invoice | POS Sale | Return | Credit Note")
    sales_date: datetime | None = None
    sales_status: SalesStatus | None = Field(default=None, description="Draft | Confirmed | Completed | Cancelled | Returned")
    invoice_status: str | None = Field(default=None, max_length=30, description="Draft, Issued, Paid, …")
    payment_status: InvoicePaymentStatus | None = Field(default=None, description="Unpaid | Partial | Paid | Refunded")
    billing_address: str | None = Field(default=None, description="Defaults to the customer's billing address")


class CreditNoteItem(BaseModel):
    product_id: str
    variant_id: str | None = None
    quantity: int = Field(gt=0)

    @field_validator("variant_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v


class CreditNoteBody(BaseModel):
    items: list[CreditNoteItem] | None = None
    reason: str | None = Field(default=None, max_length=500)
