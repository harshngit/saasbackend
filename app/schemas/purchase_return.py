from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator


class PurchaseReturnSupplierBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    contact_person: str | None = None
    phone: str | None = None
    email: str | None = None
    gst_number: str | None = None


class PurchaseReturnPurchaseBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    invoice_number: str
    purchase_number: str | None = None
    invoice_date: datetime | None = None


class PurchaseReturnGRNBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    grn_number: str
    received_date: datetime | None = None


class PurchaseReturnWarehouseBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    code: str | None = None


class PurchaseReturnItemCreate(BaseModel):
    purchase_item_id: str | None = None
    product_id: str | None = None
    variant_id: str | None = None
    quantity: int = Field(gt=0, description="Quantity to return (must be positive)")
    unit_price: float | None = Field(default=None, ge=0)
    tax_rate: float | None = Field(default=None, ge=0)
    reason: str | None = None
    batch_number: str | None = None
    serial_numbers: list[str] | None = None
    expiry_date: datetime | None = None

    @field_validator("purchase_item_id", "product_id", "variant_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v

    @model_validator(mode="before")
    @classmethod
    def _unify_item_aliases(cls, data: object) -> object:
        if isinstance(data, dict):
            if "purchaseItemId" in data and "purchase_item_id" not in data:
                data["purchase_item_id"] = data["purchaseItemId"]
            if "productId" in data and "product_id" not in data:
                data["product_id"] = data["productId"]
            if "variantId" in data and "variant_id" not in data:
                data["variant_id"] = data["variantId"]
            if "unitPrice" in data and "unit_price" not in data:
                data["unit_price"] = data["unitPrice"]
            if "taxRate" in data and "tax_rate" not in data:
                data["tax_rate"] = data["taxRate"]
            if "batchNumber" in data and "batch_number" not in data:
                data["batch_number"] = data["batchNumber"]
            if "serialNumbers" in data and "serial_numbers" not in data:
                data["serial_numbers"] = data["serialNumbers"]
            if "expiryDate" in data and "expiry_date" not in data:
                data["expiry_date"] = data["expiryDate"]
        return data


class PurchaseReturnItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    purchase_return_id: str
    purchase_item_id: str | None = None
    product_id: str | None = None
    variant_id: str | None = None
    product_code: str | None = None
    barcode: str | None = None
    product_name: str
    unit_of_measure_uom: str | None = None
    quantity: int
    unit_price: float = 0.0
    tax_rate: float = 0.0
    tax_amount: float = 0.0
    line_total: float = 0.0
    reason: str | None = None
    batch_number: str | None = None
    serial_numbers: list[str] | None = None
    expiry_date: datetime | None = None

    @computed_field
    def purchaseItemId(self) -> str | None:
        return self.purchase_item_id

    @computed_field
    def productId(self) -> str | None:
        return self.product_id

    @computed_field
    def variantId(self) -> str | None:
        return self.variant_id

    @computed_field
    def productName(self) -> str:
        return self.product_name

    @computed_field
    def unitPrice(self) -> float:
        return self.unit_price

    @computed_field
    def taxRate(self) -> float:
        return self.tax_rate

    @computed_field
    def taxAmount(self) -> float:
        return self.tax_amount

    @computed_field
    def lineTotal(self) -> float:
        return self.line_total


class PurchaseReturnCreate(BaseModel):
    purchase_id: str = Field(description="The source Purchase Invoice ID")
    supplier_id: str | None = None
    grn_id: str | None = None
    warehouse_id: str | None = None
    return_date: datetime | None = None
    reason: str | None = None
    notes: str | None = None
    items: list[PurchaseReturnItemCreate] = Field(min_length=1)

    @field_validator("purchase_id", "supplier_id", "grn_id", "warehouse_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v

    @model_validator(mode="before")
    @classmethod
    def _unify_aliases(cls, data: object) -> object:
        if isinstance(data, dict):
            if "purchaseId" in data and "purchase_id" not in data:
                data["purchase_id"] = data["purchaseId"]
            if "supplierId" in data and "supplier_id" not in data:
                data["supplier_id"] = data["supplierId"]
            if "grnId" in data and "grn_id" not in data:
                data["grn_id"] = data["grnId"]
            if "warehouseId" in data and "warehouse_id" not in data:
                data["warehouse_id"] = data["warehouseId"]
            if "returnDate" in data and "return_date" not in data:
                data["return_date"] = data["returnDate"]
        return data


class PurchaseReturnUpdate(BaseModel):
    return_date: datetime | None = None
    reason: str | None = None
    notes: str | None = None
    warehouse_id: str | None = None
    items: list[PurchaseReturnItemCreate] | None = None

    @model_validator(mode="before")
    @classmethod
    def _unify_aliases(cls, data: object) -> object:
        if isinstance(data, dict):
            if "returnDate" in data and "return_date" not in data:
                data["return_date"] = data["returnDate"]
            if "warehouseId" in data and "warehouse_id" not in data:
                data["warehouse_id"] = data["warehouseId"]
        return data


class PurchaseReturnCancel(BaseModel):
    reason: str | None = None
    cancel_reason: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _unify_aliases(cls, data: object) -> object:
        if isinstance(data, dict):
            if "cancelReason" in data and "cancel_reason" not in data:
                data["cancel_reason"] = data["cancelReason"]
            if "reason" in data and "cancel_reason" not in data:
                data["cancel_reason"] = data["reason"]
        return data


class PurchaseReturnOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    return_number: str
    purchase_id: str | None = None
    supplier_id: str | None = None
    grn_id: str | None = None
    warehouse_id: str | None = None
    status: str
    return_date: datetime
    reason: str | None = None
    notes: str | None = None
    cancel_reason: str | None = None
    stock_deducted: bool = False
    created_by: str | None = None
    confirmed_at: datetime | None = None
    dispatched_at: datetime | None = None
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    total_return_qty: int = 0
    total_amount: float = 0.0

    items: list[PurchaseReturnItemOut] = []
    supplier: PurchaseReturnSupplierBrief | None = None
    purchase: PurchaseReturnPurchaseBrief | None = None
    grn: PurchaseReturnGRNBrief | None = None
    warehouse: PurchaseReturnWarehouseBrief | None = None

    @computed_field
    def returnNumber(self) -> str:
        return self.return_number

    @computed_field
    def supplierName(self) -> str | None:
        return self.supplier.name if self.supplier else None

    @computed_field
    def purchaseNumber(self) -> str | None:
        if not self.purchase:
            return None
        return self.purchase.purchase_number or self.purchase.invoice_number

    @computed_field
    def grnNumber(self) -> str | None:
        return self.grn.grn_number if self.grn else None

    @computed_field
    def returnDate(self) -> datetime:
        return self.return_date

    @computed_field
    def totalReturnQty(self) -> int:
        return self.total_return_qty

    @computed_field
    def totalAmount(self) -> float:
        return self.total_amount

    @computed_field
    def createdAt(self) -> datetime:
        return self.created_at

    @computed_field
    def confirmedAt(self) -> datetime | None:
        return self.confirmed_at

    @computed_field
    def dispatchedAt(self) -> datetime | None:
        return self.dispatched_at

    @computed_field
    def completedAt(self) -> datetime | None:
        return self.completed_at

    @computed_field
    def cancelledAt(self) -> datetime | None:
        return self.cancelled_at

    @computed_field
    def cancelReason(self) -> str | None:
        return self.cancel_reason


class PurchaseReturnListResponse(BaseModel):
    items: list[PurchaseReturnOut]
    total: int
    page: int | None = None
    page_size: int | None = None
