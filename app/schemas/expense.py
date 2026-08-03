from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ExpenseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    category: str
    amount: float
    description: str | None
    expense_date: datetime
    payment_mode: str | None
    receipt_url: str | None
    status: str
    submitted_by: str | None
    approved_by: str | None
    reject_reason: str | None
    created_at: datetime
    updated_at: datetime


class ExpenseCreate(BaseModel):
    category: str = Field(min_length=1, max_length=100)
    amount: float = Field(gt=0)
    description: str | None = None
    expense_date: datetime | None = None
    payment_mode: str | None = Field(default=None, max_length=30)
    receipt_url: str | None = None


class ExpenseUpdate(BaseModel):
    category: str | None = Field(default=None, max_length=100)
    amount: float | None = Field(default=None, gt=0)
    description: str | None = None
    expense_date: datetime | None = None
    payment_mode: str | None = Field(default=None, max_length=30)
    receipt_url: str | None = None


class RejectBody(BaseModel):
    reason: str | None = Field(default=None, max_length=500)
