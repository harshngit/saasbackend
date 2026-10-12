from datetime import date, datetime
from decimal import Decimal
import math
from pydantic import BaseModel, ConfigDict, Field, field_validator


def _validate_decimal(val: Decimal | None) -> Decimal | None:
    if val is None:
        return None
    if isinstance(val, (float, Decimal)):
        if math.isnan(val) or math.isinf(val):
            raise ValueError("Amount cannot be NaN or infinite")
    if val < Decimal("0.00"):
        raise ValueError("Amount cannot be negative")
    return round(val, 2)


class MethodDetailIn(BaseModel):
    payment_method: str = Field(min_length=1, max_length=30)
    actual_amount: Decimal = Field(default=Decimal("0.00"))
    notes: str | None = Field(default=None, max_length=255)

    @field_validator("actual_amount")
    @classmethod
    def validate_amount(cls, v: Decimal) -> Decimal:
        res = _validate_decimal(v)
        return res if res is not None else Decimal("0.00")

    @field_validator("payment_method")
    @classmethod
    def normalize_method(cls, v: str) -> str:
        s = v.strip().lower()
        if not s:
            raise ValueError("payment_method cannot be empty")
        return s


class MethodDetailOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str | None = None
    payment_method: str
    expected_amount: Decimal
    actual_amount: Decimal
    variance_amount: Decimal
    notes: str | None = None


class UserBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    email: str


class ExpectedCollectionsPreview(BaseModel):
    reconciliation_date: date
    total_expected_collections: Decimal
    details: list[MethodDetailOut]

    # Cash movement breakdown
    opening_balance: Decimal
    cash_sales: Decimal
    cash_received: Decimal
    cash_paid: Decimal
    expenses_paid: Decimal
    expected_closing_cash: Decimal


class CashReconciliationCreate(BaseModel):
    reconciliation_date: date | None = None
    opening_balance: Decimal = Field(default=Decimal("0.00"))
    actual_cash_on_hand: Decimal = Field(default=Decimal("0.00"))
    details: list[MethodDetailIn] | None = None
    variance_reason: str | None = None
    notes: str | None = None

    @field_validator("opening_balance", "actual_cash_on_hand")
    @classmethod
    def validate_non_negative(cls, v: Decimal) -> Decimal:
        res = _validate_decimal(v)
        return res if res is not None else Decimal("0.00")


class CashReconciliationUpdate(BaseModel):
    opening_balance: Decimal | None = None
    actual_cash_on_hand: Decimal | None = None
    details: list[MethodDetailIn] | None = None
    variance_reason: str | None = None
    notes: str | None = None

    @field_validator("opening_balance", "actual_cash_on_hand")
    @classmethod
    def validate_non_negative(cls, v: Decimal | None) -> Decimal | None:
        return _validate_decimal(v)


class CashReconciliationCloseIn(BaseModel):
    opening_balance: Decimal | None = None
    actual_cash_on_hand: Decimal | None = None
    details: list[MethodDetailIn] | None = None
    variance_reason: str | None = None
    notes: str | None = None

    @field_validator("opening_balance", "actual_cash_on_hand")
    @classmethod
    def validate_non_negative(cls, v: Decimal | None) -> Decimal | None:
        return _validate_decimal(v)


class CashReconciliationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    reconciliation_date: date
    status: str

    # Collections
    total_expected_collections: Decimal
    total_actual_collections: Decimal
    collections_variance: Decimal

    # Cash Movements
    opening_balance: Decimal
    cash_sales: Decimal
    cash_received: Decimal
    cash_paid: Decimal
    expenses_paid: Decimal
    expected_closing_cash: Decimal
    actual_cash_on_hand: Decimal
    cash_balance_variance: Decimal

    # Metadata & Auditing
    variance_reason: str | None = None
    notes: str | None = None

    created_by_id: str | None = None
    closed_by_id: str | None = None
    created_by: UserBrief | None = None
    closed_by: UserBrief | None = None

    closed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    details: list[MethodDetailOut] = []
