from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

# A quotation's life. `converted` is set by the conversion endpoint, never sent in.
QUOTATION_STATUSES = ("draft", "sent", "accepted", "rejected", "expired", "converted")


def _lower(value: object) -> object:
    return value.strip().lower() if isinstance(value, str) else value


QuotationStatus = Annotated[
    str, BeforeValidator(_lower), Field(pattern=f"^({'|'.join(QUOTATION_STATUSES)})$")
]


class QuotationCustomerBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    business_name: str | None = None


class QuotationSalespersonBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str


class QuotationItemBase(BaseModel):
    product_id: str | None = None
    variant_id: str | None = None
    quantity: float = Field(gt=0)
    uom: str | None = None
    unit_price: float = Field(ge=0)
    discount: float = Field(default=0, ge=0, description="Per line")
    discount_percent: float | None = Field(default=0, ge=0, le=100, description="Per line discount %")
    tax_rate: float | None = Field(
        default=None, ge=0, le=100,
        description="Per-line tax %. Falls back to the product's own rate; carried "
                    "through to the order on conversion.",
    )


class QuotationItemCreate(QuotationItemBase):
    pass


class QuotationItemOut(QuotationItemBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    product_name: str
    line_total: float = 0
    tax_amount: float = 0


class QuotationBase(BaseModel):
    quotation_number: str | None = Field(default=None, description="Auto-generated when omitted")
    quotation_date: datetime | None = None
    valid_until: datetime | None = None
    customer_id: str | None = None
    billing_address: str | None = None
    salesperson_id: str | None = None
    currency: str | None = "INR"
    shipping_address: str | None = None
    payment_terms: str | None = None
    delivery_terms: str | None = None
    notes: str | None = None
    terms_conditions: str | None = None
    status: QuotationStatus | None = Field(
        default=None,
        description="draft | sent | accepted | rejected | expired. `converted` is set "
                    "by POST /quotations/{id}/convert-to-order, not sent in.",
    )


class QuotationCreate(QuotationBase):
    items: list[QuotationItemCreate] = Field(min_length=1)


class QuotationUpdate(QuotationBase):
    """Partial update — only the fields you send change. Sending `items` replaces the
    whole line set, which is what the edit screen holds. A converted quotation is
    frozen: its order already exists."""

    items: list[QuotationItemCreate] | None = None


class ConvertToOrder(BaseModel):
    """Turn an accepted quotation into a sales order.

    Nothing about the lines is sent: the customer, products, variants, quantities,
    rates, discounts, taxes, terms and salesperson are all copied from the quotation.
    Only the fulfilment terms the order needs are given here.
    """

    warehouse_id: str | None = Field(
        default=None, description="Which warehouse to reserve from. Defaults to the firm's default.")
    delivery_date: datetime | None = None
    fulfilment_method: str | None = Field(default=None, max_length=30, description="delivery | pickup")
    payment_type: str | None = Field(default=None, max_length=20, description="cash | credit | upi | …")
    payment_terms_days: int | None = Field(default=None, ge=0, le=365)
    billing_address: str | None = None
    shipping_address: str | None = None
    delivery_address: str | None = None
    payment_terms: str | None = None
    delivery_terms: str | None = None


class ConvertedOrderBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    order_number: str
    status: str
    fulfilment_status: str | None = None
    total: float = 0
    billing_address: str | None = None
    shipping_address: str | None = None
    delivery_address: str | None = None
    payment_terms: str | None = None
    delivery_terms: str | None = None
    currency: str | None = None


class ConversionOut(BaseModel):
    quotation_id: str
    quotation_number: str | None = None
    quotation_status: str
    order: ConvertedOrderBrief


class QuotationOut(QuotationBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    created_at: datetime
    updated_at: datetime

    items: list[QuotationItemOut]
    customer: QuotationCustomerBrief | None = None
    salesperson: QuotationSalespersonBrief | None = None
    subtotal: float = 0
    tax_total: float = 0
    total: float = 0
    item_count: int = 0
    converted_order_id: str | None = None
    converted_at: datetime | None = None


class QuotationListItem(BaseModel):
    """What the quotations table needs — number, customer, salesperson, dates,
    amount and status. The full quotation (items, addresses, terms) comes from
    GET /quotations/{id}."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    quotation_number: str | None = None
    quotation_date: datetime | None = None
    valid_until: datetime | None = None
    currency: str | None = None
    status: str | None = None
    customer: QuotationCustomerBrief | None = None
    salesperson: QuotationSalespersonBrief | None = None
    total: float = 0
    item_count: int = 0
    created_at: datetime
