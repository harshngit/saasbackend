from datetime import datetime
from pydantic import BaseModel, ConfigDict


class APItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    supplier_invoice_id: str
    supplier_invoice_number: str
    supplier_id: str
    supplier_name: str
    invoice_date: datetime
    due_date: datetime | None = None
    grand_total: float
    amount_paid: float
    outstanding_amount: float
    payment_status: str
    verification_status: str
    status: str
    is_overdue: bool = False
    days_overdue: int = 0
    ageing_bucket: str | None = None


class APSummaryOut(BaseModel):
    total_payable: float = 0.0
    total_overdue: float = 0.0
    total_due_today: float = 0.0
    open_invoice_count: int = 0
    supplier_count: int = 0


class APAgeingBucketOut(BaseModel):
    bucket_0_30: float = 0.0
    bucket_31_60: float = 0.0
    bucket_61_90: float = 0.0
    bucket_90_plus: float = 0.0


class APListResponse(BaseModel):
    summary: APSummaryOut
    ageing: APAgeingBucketOut
    items: list[APItemOut] = []


class SupplierAPStatementOut(BaseModel):
    supplier_id: str
    supplier_name: str
    total_open_payable: float = 0.0
    total_overdue: float = 0.0
    open_invoice_count: int = 0
    invoices: list[APItemOut] = []
