from datetime import datetime
from pydantic import BaseModel, ConfigDict


class ReceiptCustomerBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    business_name: str | None = None


class ReceiptInvoiceBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    invoice_number: str
    total: float


class PaymentReceiptBase(BaseModel):
    receipt_number: str
    receipt_date: datetime | None = None
    customer_id: str | None = None
    invoice_reference_id: str | None = None
    amount_received: float
    payment_method: str | None = None
    payment_status: str | None = "completed"


class PaymentReceiptCreate(PaymentReceiptBase):
    pass


class PaymentReceiptOut(PaymentReceiptBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    created_at: datetime

    customer: ReceiptCustomerBrief | None = None
    invoice: ReceiptInvoiceBrief | None = None
