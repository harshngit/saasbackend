from datetime import datetime
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class VehicleLoadingItemIn(BaseModel):
    product_id: str | None = None
    variant_id: str | None = None
    loaded_qty: int = Field(
        gt=0, validation_alias=AliasChoices("loaded_qty", "loaded_quantity"),
        description="Also accepted as `loaded_quantity`",
    )
    delivery_item_id: str | None = Field(
        default=None,
        description="Load against a planned delivery line. Given this, the product is "
                    "taken from the line and the delivery's loaded quantity moves too.",
    )

    @field_validator("variant_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v


class VehicleLoadingItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    product_id: str | None
    variant_id: str | None
    product_name: str
    loaded_qty: int
    extra_qty: int
    returned_qty: int
    delivered_qty: int
    remaining_qty: int = 0
    expected_closing_qty: int = 0

    @model_validator(mode="after")
    def _compute_quantities(self) -> "VehicleLoadingItemOut":
        l_qty = self.loaded_qty or 0
        e_qty = self.extra_qty or 0
        d_qty = self.delivered_qty or 0
        r_qty = self.returned_qty or 0
        self.remaining_qty = max((l_qty + e_qty) - d_qty, 0)
        self.expected_closing_qty = max((l_qty + e_qty) - d_qty - r_qty, 0)
        return self


class VehiclePartnerBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str


class VehicleBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    vehicle_number: str
    vehicle_type: str | None = None
    capacity_kg: float | None = None


class VehicleLoadingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    delivery_partner_id: str
    delivery_partner: VehiclePartnerBrief | None = None
    vehicle_id: str | None = None
    vehicle: VehicleBrief | None = None
    date: datetime
    status: str
    items: list[VehicleLoadingItemOut]
    created_at: datetime
    updated_at: datetime


class VehicleLoadingCreate(BaseModel):
    """Load a vehicle.

    Two ways to use it. Give a `delivery_id` and per-item `delivery_item_id`s to load
    against a planned delivery — the delivery's loaded quantities move, its
    reservations are consumed and the delivery becomes `loaded`. Or give plain
    product quantities to stock a vehicle with no delivery behind it (a van doing
    ad-hoc field sales).
    """

    delivery_partner_id: str | None = Field(
        default=None, description="Required unless delivery_id is given, which names the partner")
    delivery_id: str | None = None
    vehicle_id: str | None = None
    date: datetime | None = None
    items: list[VehicleLoadingItemIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "VehicleLoadingCreate":
        if not self.delivery_id and not self.delivery_partner_id:
            raise ValueError("Give delivery_partner_id, or delivery_id to take it from the delivery")
        if not self.delivery_id and not self.items:
            raise ValueError("Give the items to load")
        for item in self.items:
            if not item.delivery_item_id and not item.product_id:
                raise ValueError("Each item needs a product_id, or a delivery_item_id to take it from")
        return self


class LoadedLineOut(BaseModel):
    """What one line's load did to the warehouse and the vehicle."""

    delivery_item_id: str | None = None
    loaded_quantity: float
    warehouse_on_hand_after: float
    vehicle_stock_after: float


class DeliveryLoadOut(BaseModel):
    """The result of loading against a delivery."""

    loading_id: str
    delivery_id: str
    status: str = Field(description="The delivery's status after loading")
    items: list[LoadedLineOut]


class ExtraLoadItem(BaseModel):
    product_id: str
    variant_id: str | None = None
    quantity: int = Field(gt=0)

    @field_validator("variant_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v


class ExtraLoadBody(BaseModel):
    items: list[ExtraLoadItem] = Field(min_length=1)


class EndOfDayItem(BaseModel):
    product_id: str
    variant_id: str | None = None
    returned_qty: int = Field(ge=0)

    @field_validator("variant_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v


class EndOfDayBody(BaseModel):
    items: list[EndOfDayItem] = Field(min_length=1)


class ReconcileItemIn(BaseModel):
    product_id: str | None = None
    variant_id: str | None = None
    loading_item_id: str | None = None
    physical_qty: int = Field(ge=0, description="Actual physical count counted on vehicle")
    notes: str | None = Field(default=None, max_length=300)

    @field_validator("variant_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v


class ReconcileBody(BaseModel):
    items: list[ReconcileItemIn] = Field(min_length=1)
    notes: str | None = Field(default=None, max_length=500)


class VehicleReconciliationItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    loading_item_id: str | None = None
    product_id: str | None = None
    variant_id: str | None = None
    product_name: str
    loaded_qty: int
    extra_qty: int
    delivered_qty: int
    returned_qty: int
    expected_closing_qty: int
    physical_qty: int
    variance_qty: int
    notes: str | None = None


class VehicleReconciliationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    loading_id: str
    reconciled_by_id: str
    status: str
    notes: str | None = None
    created_at: datetime
    updated_at: datetime
    items: list[VehicleReconciliationItemOut] = Field(default_factory=list)

