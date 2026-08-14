from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator


class ReturnCustomerBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    business_name: str | None = None


class ReturnInvoiceBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    invoice_number: str


class ReturnCreditNoteBrief(BaseModel):
    """The credit note a return produced."""

    model_config = ConfigDict(from_attributes=True)
    id: str
    invoice_number: str
    total: float
    credit_note_reason: str | None = None


class ReturnItemCreate(BaseModel):
    """One line the customer wants to send back.

    Name the `invoice_item_id` and the credit is worked out at the price it was billed
    at. A `product_id` is still accepted for goods taken back without an invoice line
    behind them.
    """

    invoice_item_id: str | None = None
    product_id: str | None = None
    variant_id: str | None = None
    quantity_returned: float = Field(gt=0)

    @field_validator("invoice_item_id", "product_id", "variant_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v


class ReturnItemDecision(BaseModel):
    """What the condition check found on one line.

    `condition` is free text — whatever the firm writes on the goods. Only `saleable`
    puts them back on the shelf, and asking to restock anything else is refused.
    """

    return_item_id: str
    received_quantity: float | None = Field(default=None, ge=0)
    condition: str | None = Field(default=None, max_length=50, description="saleable | damaged | expired | …")
    restock: bool | None = None


class ReturnItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    product_id: str | None = None
    variant_id: str | None = None
    product_name: str
    quantity_returned: float
    invoice_item_id: str | None = None
    unit_price: float | None = None
    tax_rate: float | None = None
    line_total: float | None = None
    received_quantity: float | None = None
    condition: str | None = None
    restock: bool = False
    restocked_quantity: float | None = None


class SalesReturnCreate(BaseModel):
    """Ask to send goods back. This creates a **request** and nothing else.

    No stock moves and no credit is given here: the goods have not arrived yet. Once
    they have, `PATCH /sales-returns/{id}/receive` records what turned up, and
    `PATCH /sales-returns/{id}/approve` checks their condition, restocks whatever is
    saleable and raises the credit note.
    """

    invoice_reference_id: str | None = Field(
        default=None, description="The invoice the goods were sold on. The customer is derived from it"
    )
    customer_id: str | None = Field(default=None, description="Only needed with no invoice behind it")
    return_reason: str | None = Field(default=None, max_length=100)
    reason: str | None = Field(default=None, max_length=100, description="Same as return_reason")
    return_type: str | None = None
    return_date: datetime | None = None
    return_number: str | None = Field(default=None, description="Auto-generated when omitted")
    warehouse_id: str | None = Field(default=None, description="Where the goods will come back to")
    notes: str | None = None
    items: list[ReturnItemCreate] = Field(min_length=1)


class SalesReturnUpdate(BaseModel):
    """Correct a return while it is still open. Its items can be resent as a set."""

    return_reason: str | None = Field(default=None, max_length=100)
    return_date: datetime | None = None
    warehouse_id: str | None = None
    notes: str | None = None
    items: list[ReturnItemCreate] | None = None


class ReturnReceiveBody(BaseModel):
    """The goods are physically back. Say what actually arrived, if it differs."""

    items: list[ReturnItemDecision] | None = None
    notes: str | None = None


class ReturnApproveBody(BaseModel):
    """Accept the return: restock what is saleable, credit the customer.

    Any line left out is taken as fully received. A line's goods go back into sellable
    stock only when its `condition` is saleable **and** `restock` is true.
    """

    items: list[ReturnItemDecision] | None = None
    warehouse_id: str | None = Field(default=None, description="Where the saleable goods go back")
    credit_note: bool = Field(default=True, description="Raise a credit note for what came back")
    notes: str | None = None


class ReturnRejectBody(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class SalesReturnOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    return_number: str
    return_date: datetime
    return_status: str | None = None
    customer_id: str | None = None
    invoice_reference_id: str | None = None
    return_reason: str | None = None
    return_type: str | None = None
    warehouse_id: str | None = None
    received_at: datetime | None = None
    approved_at: datetime | None = None
    approved_by: str | None = None
    rejected_reason: str | None = None
    notes: str | None = None
    credit_note_id: str | None = None
    credit_amount: float | None = None
    created_at: datetime

    items: list[ReturnItemOut]
    customer: ReturnCustomerBrief | None = None
    invoice: ReturnInvoiceBrief | None = None
    credit_note: ReturnCreditNoteBrief | None = None

    @computed_field(description="Where the return stands: requested | received | approved | rejected")
    @property
    def status(self) -> str | None:
        return self.return_status
