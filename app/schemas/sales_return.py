from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field


class ReturnCustomerBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    business_name: str | None = None


class ReturnInvoiceBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    invoice_number: str


class ReturnItemBase(BaseModel):
    product_id: str | None = None
    variant_id: str | None = None
    quantity_returned: float


class ReturnItemCreate(ReturnItemBase):
    pass


class ReturnItemOut(ReturnItemBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    product_name: str


class SalesReturnBase(BaseModel):
    return_number: str | None = Field(default=None, description="Auto-generated when omitted")
    return_date: datetime | None = None
    customer_id: str | None = None
    invoice_reference_id: str | None = None
    return_reason: str | None = None
    return_type: str | None = "Credit Note"
    return_status: str | None = "requested"


class SalesReturnCreate(SalesReturnBase):
    items: list[ReturnItemCreate]


class SalesReturnOut(SalesReturnBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    created_at: datetime

    items: list[ReturnItemOut]
    customer: ReturnCustomerBrief | None = None
    invoice: ReturnInvoiceBrief | None = None
