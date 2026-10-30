from datetime import date, datetime

from pydantic import BaseModel, Field


class DayEndHandedOver(BaseModel):
    """What the partner actually hands over, per category. Omitted categories are 0."""
    cash: float = Field(default=0, ge=0)
    bank: float = Field(default=0, ge=0)
    upi: float = Field(default=0, ge=0)
    cheque: float = Field(default=0, ge=0)
    other: float = Field(default=0, ge=0)


class DayEndSubmit(BaseModel):
    business_date: date | None = Field(default=None, description="Defaults to today in the organization's timezone")
    handed_over: DayEndHandedOver = Field(default_factory=DayEndHandedOver)
    notes: str | None = Field(
        default=None, max_length=2000,
        description="Required when any handed-over amount differs from the system-expected amount",
    )


class DayEndApprove(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


class DayEndReject(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class DayEndDeliveries(BaseModel):
    assigned_count: int = 0
    delivered_count: int = Field(default=0, description="delivered + partially_delivered")
    partially_delivered_count: int = 0
    returned_count: int = Field(default=0, description="Failed attempts: goods came back")
    pending_count: int = Field(default=0, description="planned / accepted / ready / loaded / in transit")


class DayEndMethodLine(BaseModel):
    method: str = Field(description="cash | bank | upi | cheque | other")
    system_expected: float
    actual_handed_over: float | None = None
    variance: float | None = Field(default=None, description="actual_handed_over - system_expected")


class DayEndCollections(BaseModel):
    breakdown: list[DayEndMethodLine]
    expected_total: float
    handed_over_total: float | None = None
    variance_total: float | None = None
    collection_ids: list[str] = []
    collection_count: int = 0
    live_expected_total: float
    changed_since_submission: bool = False


class DayEndHistoryEntry(BaseModel):
    action: str | None = None
    actor_id: str | None = None
    actor_name: str | None = None
    at: datetime | None = None
    note: str | None = None


class DayEndOut(BaseModel):
    id: str | None = Field(default=None, description="null while the day is still open and never submitted")
    organization_id: str
    delivery_partner_id: str
    delivery_partner_name: str | None = None
    business_date: date
    status: str = Field(description="open | submitted | approved | rejected")
    deliveries: DayEndDeliveries
    collections: DayEndCollections
    notes: str | None = None
    submission_count: int = 0
    submitted_at: datetime | None = None
    reviewed_at: datetime | None = None
    reviewed_by_id: str | None = None
    reviewed_by_name: str | None = None
    review_note: str | None = None
    rejection_reason: str | None = None
    history: list[DayEndHistoryEntry] = []
