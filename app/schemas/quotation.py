from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field


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
    quantity: float
    uom: str | None = None
    unit_price: float


class QuotationItemCreate(QuotationItemBase):
    pass


class QuotationItemOut(QuotationItemBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    product_name: str


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
    status: str | None = Field(default=None, description="draft | sent | accepted | rejected | expired")


class QuotationCreate(QuotationBase):
    items: list[QuotationItemCreate]


class QuotationOut(QuotationBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    created_at: datetime
    updated_at: datetime

    items: list[QuotationItemOut]
    customer: QuotationCustomerBrief | None = None
    salesperson: QuotationSalespersonBrief | None = None
    total: float = 0
    item_count: int = 0


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
