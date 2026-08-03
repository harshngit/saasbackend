from datetime import datetime
from pydantic import BaseModel, ConfigDict


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
    quotation_number: str
    quotation_date: datetime | None = None
    valid_until: datetime | None = None
    customer_id: str | None = None
    billing_address: str | None = None
    salesperson_id: str | None = None
    currency: str | None = "INR"


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
