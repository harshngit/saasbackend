from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class OrderItemIn(BaseModel):
    product_id: str
    variant_id: str | None = None
    quantity: int = Field(gt=0)
    unit_price: float | None = Field(default=None, ge=0)  # defaults to product/variant price
    discount: float = Field(default=0, ge=0)              # per-line
    tax_rate: float | None = Field(
        default=None, ge=0, le=100,
        description="Per-line tax %. Falls back to the product's own rate — never a "
                    "hardcoded figure. Snapshotted on the line so an invoice raised "
                    "later bills the agreed rate.",
    )

    @field_validator("variant_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v


class OrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    product_id: str | None
    variant_id: str | None
    product_name: str
    quantity: int
    ordered_quantity: int = Field(default=0, description="Same as quantity, named as the flow does")
    reserved_quantity: float = 0
    delivered_quantity: float = 0
    unit_price: float
    discount: float
    tax_rate: float | None = None
    tax_amount: float = 0
    line_total: float


class CustomerBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    business_name: str | None


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    order_number: str
    customer_id: str | None
    customer: CustomerBrief | None
    status: str = Field(
        description="draft | placed | awaiting_approval | processing | completed | cancelled")
    fulfilment_status: str = Field(
        default="not_started",
        description="not_started | reserved | planned | loaded | in_transit | "
                    "partially_delivered | delivered | failed",
    )
    warehouse_id: str | None = None
    quotation_id: str | None = None
    delivery_date: datetime | None = None
    fulfilment_method: str | None = None
    pickup_status: str | None = None
    collected_by: str | None = None
    collected_at: datetime | None = None
    pickup_notes: str | None = None
    payment_type: str | None = None
    payment_terms_days: int | None = None
    source: str
    assigned_delivery_partner_id: str | None
    created_by: str | None
    subtotal: float
    discount: float
    tax: float
    total: float
    notes: str | None
    reject_reason: str | None
    items: list[OrderItemOut]
    approved_at: datetime | None
    created_at: datetime
    updated_at: datetime

    # Sheet fields
    sales_order_number: str | None = None
    order_date: datetime | None = None
    salesperson_id: str | None = None
    order_status: str | None = None

    # What the warehouse holds for this order, reported so the sales screen can show
    # the effect of placing it without a second call.
    stock_summary: list[dict] = Field(default_factory=list)
    # Non-blocking notices, e.g. the order taking a customer past their credit limit
    # while the firm's credit_limit_action is "warn".
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _mirror_quantities(self) -> "OrderOut":
        """`ordered_quantity` is the flow's name for `quantity` — fill it in rather
        than making every client know both."""
        for item in self.items:
            if not item.ordered_quantity:
                item.ordered_quantity = item.quantity
        return self


class OrderCreate(BaseModel):
    customer_id: str
    warehouse_id: str | None = Field(
        default=None, description="Which warehouse to reserve from. Defaults to the firm's default.")
    quotation_id: str | None = None
    delivery_date: datetime | None = None
    fulfilment_method: str | None = Field(
        default=None, max_length=30, description="delivery | pickup")
    payment_type: str | None = Field(default=None, max_length=20, description="cash | credit | upi | …")
    payment_terms_days: int | None = Field(default=None, ge=0, le=365)
    source: str = Field(default="office", description="office | delivery_vehicle")
    discount: float = Field(default=0, ge=0)  # order-level
    tax: float = Field(default=0, ge=0)
    notes: str | None = None
    items: list[OrderItemIn] = Field(min_length=1)

    @field_validator("source")
    @classmethod
    def _valid_source(cls, v: str) -> str:
        if v not in ("office", "delivery_vehicle"):
            raise ValueError("source must be 'office' or 'delivery_vehicle'")
        return v

    # Sheet fields (sales_order_number is auto-generated)
    order_date: datetime | None = None
    salesperson_id: str | None = Field(default=None, description="Assigned salesperson")
    order_status: str | None = Field(default=None, max_length=30, description="Draft, Confirmed, Processing, Completed, Cancelled")


class RejectBody(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class AssignDeliveryBody(BaseModel):
    delivery_partner_id: str


class CancelBody(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class PickupItemConfirm(BaseModel):
    order_item_id: str
    collected_quantity: float = Field(gt=0)


class PickupConfirmRequest(BaseModel):
    items: list[PickupItemConfirm] | None = None
    collected_by: str | None = Field(default=None, max_length=150)
    notes: str | None = Field(default=None, max_length=500)
