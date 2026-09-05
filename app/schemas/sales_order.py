from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.workflow import public_order_status


class OrderItemIn(BaseModel):
    product_id: str
    variant_id: str | None = None
    quantity: int = Field(gt=0)
    unit_price: float | None = Field(default=None, ge=0)  # defaults to product/variant price
    discount: float = Field(default=0, ge=0)              # per-line flat discount
    discount_percent: float | None = Field(default=0, ge=0, le=100, description="Per-line discount %")
    tax_rate: float | None = Field(
        default=None, ge=0, le=100,
        description="Per-line tax %. Falls back to the product's own rate — never a "
                    "hardcoded figure. Snapshotted on the line so an invoice raised "
                    "later bills the agreed rate.",
    )
    uom: str | None = None
    cost_price: float | None = None

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
    remaining_quantity: float = Field(
        default=0,
        description="Authoritative remaining quantity: max(ordered_quantity - delivered_quantity, 0)",
    )
    unit_price: float
    discount: float
    discount_percent: float | None = 0
    cost_price: float | None = None
    tax_rate: float | None = None
    tax_amount: float = 0
    line_total: float
    uom: str | None = None


class CustomerBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    business_name: str | None = None
    phone: str | None = None
    email: str | None = None
    delivery_address: str | None = None
    billing_address: str | None = None
    gst_number: str | None = None


class SalespersonBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    email: str | None = None


class UserBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    email: str | None = None


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    order_number: str
    customer_id: str | None
    customer: CustomerBrief | None
    status: str = Field(
        description="Client-facing Order status: draft | confirmed | completed | cancelled. "
                    "(Internally the order may be draft/awaiting_approval/placed/processing/"
                    "completed/cancelled — see app.core.workflow.public_order_status.)")
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
    payment_terms: str | None = None
    delivery_terms: str | None = None
    currency: str | None = "INR"
    billing_address: str | None = None
    shipping_address: str | None = None
    delivery_address: str | None = None
    source: str
    assigned_delivery_partner_id: str | None
    created_by: str | None = None
    created_by_user: UserBrief | None = None
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
    salesperson: SalespersonBrief | None = None
    order_status: str | None = None

    # Financial Summary
    previous_balance: float = 0.0
    current_order_amount: float = 0.0
    total_due: float = 0.0
    paid_amount: float = 0.0
    remaining_balance: float = 0.0

    # Delivery & Invoice references
    delivery_id: str | None = None
    delivery_number: str | None = None
    invoice_id: str | None = None
    invoice_number: str | None = None

    # What the warehouse holds for this order, reported so the sales screen can show
    # the effect of placing it without a second call.
    stock_summary: list[dict] = Field(default_factory=list)
    # Non-blocking notices, e.g. the order taking a customer past their credit limit
    # while the firm's credit_limit_action is "warn".
    warnings: list[str] = Field(default_factory=list)

    @field_validator("status", mode="before")
    @classmethod
    def _public_status(cls, v: str) -> str:
        """Normalize the internal `status` to the client-facing value at the API
        boundary — the stored value on the order is never touched. Applies to
        every endpoint that returns OrderOut (list, detail, create, update)."""
        return public_order_status(v)

    @model_validator(mode="after")
    def _mirror_quantities(self) -> "OrderOut":
        """`ordered_quantity` is the flow's name for `quantity` and `remaining_quantity` is
        max(ordered_quantity - delivered_quantity, 0)."""
        for item in self.items:
            if not item.ordered_quantity:
                item.ordered_quantity = item.quantity
            ordered = float(item.ordered_quantity or item.quantity or 0)
            delivered = float(item.delivered_quantity or 0)
            item.remaining_quantity = round(max(ordered - delivered, 0.0), 3)
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
    payment_terms: str | None = None
    delivery_terms: str | None = None
    currency: str | None = "INR"
    billing_address: str | None = None
    shipping_address: str | None = None
    delivery_address: str | None = None
    source: str = Field(default="direct", description="direct | quotation | office | delivery_vehicle")
    discount: float = Field(default=0, ge=0)  # order-level
    tax: float = Field(default=0, ge=0)
    notes: str | None = None
    items: list[OrderItemIn] = Field(min_length=1)

    @field_validator("source")
    @classmethod
    def _valid_source(cls, v: str) -> str:
        if v not in ("direct", "quotation", "office", "delivery_vehicle"):
            raise ValueError("source must be 'direct', 'quotation', 'office', or 'delivery_vehicle'")
        return v

    # Sheet fields (sales_order_number is auto-generated)
    order_date: datetime | None = None
    salesperson_id: str | None = Field(default=None, description="Assigned salesperson")
    order_status: str | None = Field(default=None, max_length=30, description="Draft, Confirmed, Processing, Completed, Cancelled")


class OrderUpdate(BaseModel):
    customer_id: str | None = None
    warehouse_id: str | None = Field(
        default=None, description="Which warehouse to reserve from."
    )
    quotation_id: str | None = None
    delivery_date: datetime | None = None
    fulfilment_method: str | None = Field(
        default=None, max_length=30, description="delivery | pickup"
    )
    payment_type: str | None = Field(
        default=None, max_length=20, description="cash | credit | upi | …"
    )
    payment_terms_days: int | None = Field(default=None, ge=0, le=365)
    payment_terms: str | None = None
    delivery_terms: str | None = None
    currency: str | None = None
    billing_address: str | None = None
    shipping_address: str | None = None
    delivery_address: str | None = None
    source: str | None = Field(default=None, description="direct | quotation | office | delivery_vehicle")
    discount: float | None = Field(default=None, ge=0)
    tax: float | None = Field(default=None, ge=0)
    notes: str | None = None
    items: list[OrderItemIn] | None = None
    order_date: datetime | None = None
    salesperson_id: str | None = Field(default=None, description="Assigned salesperson")
    order_status: str | None = Field(
        default=None, max_length=30, description="Draft, Confirmed, Processing, Completed, Cancelled"
    )

    @field_validator("source")
    @classmethod
    def _valid_source(cls, v: str | None) -> str | None:
        if v is not None and v not in ("direct", "quotation", "office", "delivery_vehicle"):
            raise ValueError("source must be 'direct', 'quotation', 'office', or 'delivery_vehicle'")
        return v


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
