from pydantic import BaseModel, Field, field_validator, model_validator


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
