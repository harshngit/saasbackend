from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DeliveryStatusUpdate(BaseModel):
    status: str = Field(description="Delivered | Partial | Failed | Rescheduled")
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("status")
    @classmethod
    def _valid_status(cls, v: str) -> str:
        choices = {"Delivered", "Partial", "Failed", "Rescheduled"}
        if v not in choices:
            raise ValueError(f"status must be one of {choices}")
        return v

    @model_validator(mode="after")
    def _check_reason(self) -> "DeliveryStatusUpdate":
        if self.status != "Delivered" and not self.reason:
            raise ValueError("reason is required for statuses other than 'Delivered'")
        return self


class DeliveryItemBase(BaseModel):
    product_id: str | None = None
    variant_id: str | None = None
    delivered_quantity: float


class DeliveryItemCreate(DeliveryItemBase):
    pass


class DeliveryItemOut(DeliveryItemBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    product_name: str


class DeliveryNoteBase(BaseModel):
    delivery_note_number: str | None = Field(default=None, description="Auto-generated when omitted")
    delivery_date: datetime | None = None
    sales_order_id: str | None = None
    customer_id: str | None = None
    warehouse: str | None = None
    delivery_address: str | None = None
    delivery_status: str | None = "pending"


class DeliveryNoteCreate(DeliveryNoteBase):
    items: list[DeliveryItemCreate]


class DeliveryCustomerBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    business_name: str | None = None


class DeliveryOrderBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    order_number: str


class DeliveryNoteOut(DeliveryNoteBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    created_at: datetime
    updated_at: datetime

    items: list[DeliveryItemOut]
    customer: DeliveryCustomerBrief | None = None
    sales_order: DeliveryOrderBrief | None = None


# --------------------------- the unified Delivery ---------------------------
# A Delivery is planned against a Sales Order, loaded onto a vehicle, dispatched, and
# then confirmed. Its own id is the identifier every endpoint below takes:
#
#     Sales Order ord_001 → Delivery del_001 → Delivery Item di_001 → Invoice inv_001


class DeliveryPlanItem(BaseModel):
    """How much of one order line this delivery will take out."""

    order_item_id: str
    planned_quantity: float = Field(gt=0)


class DeliveryPlanCreate(BaseModel):
    """Plan a delivery for an order.

    Anyone with the `deliveries` create permission may plan one — a Dispatch or
    Warehouse role, not only an Admin. Naming a partner is planning, not dispatch:
    the delivery starts `planned` and only goes in transit once it has been loaded
    and dispatched.

    Omit `items` to plan the whole outstanding quantity of every order line.
    """

    order_id: str
    delivery_partner_id: str | None = None
    vehicle_id: str | None = None
    warehouse_id: str | None = Field(
        default=None, description="Defaults to the order's own warehouse")
    scheduled_date: datetime | None = None
    delivery_address: str | None = None
    notes: str | None = Field(default=None, max_length=1000)
    items: list[DeliveryPlanItem] | None = None


class DeliveryPickItem(BaseModel):
    delivery_item_id: str
    picked_quantity: float = Field(gt=0)


class DeliveryPickBody(BaseModel):
    items: list[DeliveryPickItem]


class DeliveryPlanUpdate(BaseModel):
    """Re-plan or dispatch a delivery. Only the fields you send change.

    Setting `status` to `in_transit` is the dispatch step: it stamps `dispatched_at`
    and `dispatched_by_id`, and only then is the delivery live for the partner.
    Confirming the outcome is POST /deliveries/{id}/confirm, not this.
    """

    delivery_partner_id: str | None = None
    vehicle_id: str | None = None
    scheduled_date: datetime | None = None
    delivery_address: str | None = None
    notes: str | None = Field(default=None, max_length=1000)
    status: str | None = Field(
        default=None, pattern="^(planned|in_transit|cancelled)$",
        description="in_transit dispatches it; cancelled abandons the plan",
    )


class DeliveryLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    order_item_id: str | None = None
    product_id: str | None = None
    variant_id: str | None = None
    product_name: str
    planned_quantity: float = 0
    loaded_quantity: float = 0
    delivered_quantity: float = 0
    pending_quantity: float = 0


class DeliveryPartnerBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str


class VehicleBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    vehicle_number: str


class DeliveryPod(BaseModel):
    photo_file_ids: list[str] = Field(default_factory=list)
    signature_file_id: str | None = None


class DeliveryOut(BaseModel):
    """One delivery, keyed by its own id."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    delivery_number: str = Field(description="Human code, DLV-… / DN-…")
    order_id: str | None = None
    order_number: str | None = None
    customer_id: str | None = None
    customer: DeliveryCustomerBrief | None = None
    delivery_partner_id: str | None = None
    delivery_partner: DeliveryPartnerBrief | None = None
    vehicle_id: str | None = None
    vehicle: VehicleBrief | None = None
    warehouse_id: str | None = None
    delivery_address: str | None = None
    scheduled_date: datetime | None = None
    status: str = Field(
        description="planned | loaded | in_transit | partially_delivered | delivered | "
                    "failed | cancelled")
    dispatched_at: datetime | None = None
    dispatched_by_id: str | None = None
    confirmed_at: datetime | None = None
    failure_reason: str | None = None
    notes: str | None = None
    items: list[DeliveryLineOut]
    planned_total: float = 0
    loaded_total: float = 0
    delivered_total: float = 0
    pod: DeliveryPod = Field(default_factory=DeliveryPod)
    amount_due: float = Field(
        default=0, description="Order total less what has been received against it")
    created_at: datetime
    updated_at: datetime


class DeliveryConfirmItem(BaseModel):
    delivery_item_id: str
    delivered_quantity: float = Field(ge=0)


class DeliveryConfirm(BaseModel):
    """Record the outcome: what was actually handed over, plus the proof.

    Delivering less than was loaded leaves the rest **on the vehicle** — it is not
    put back into the warehouse. It stays there until a re-attempt or the
    end-of-day return.
    """

    items: list[DeliveryConfirmItem] = Field(default_factory=list)
    pod_photo_file_ids: list[str] = Field(
        default_factory=list, description="file_ids from POST /files/upload")
    signature_file_id: str | None = None
    notes: str | None = Field(default=None, max_length=1000)
    failed: bool = Field(
        default=False,
        description="Nothing was handed over. Vehicle stock is untouched and "
                    "failure_reason is required.",
    )
    failure_reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _check(self) -> "DeliveryConfirm":
        if self.failed and not self.failure_reason:
            raise ValueError("failure_reason is required when failed is true")
        if not self.failed and not self.items:
            raise ValueError("Send the delivered quantities, or set failed with a reason")
        return self
