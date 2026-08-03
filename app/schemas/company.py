from pydantic import BaseModel, ConfigDict, EmailStr, Field


class CompanySettingsOut(BaseModel):
    """Full company profile shown/edited on the Company Settings page."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    business_type: str | None
    gst_number: str | None
    pan_number: str | None
    address: str | None
    phone: str | None
    email: str | None
    financial_year: str | None
    logo_url: str | None
    signature_url: str | None
    field_settings: dict | None = None


class CompanySettingsUpdate(BaseModel):
    """Partial update — send only the fields being changed."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    business_type: str | None = Field(default=None, max_length=100)
    gst_number: str | None = Field(default=None, max_length=20)
    pan_number: str | None = Field(default=None, max_length=20)
    address: str | None = Field(default=None, max_length=500)
    phone: str | None = Field(default=None, max_length=20)
    email: EmailStr | None = None
    financial_year: str | None = Field(default=None, max_length=20)
    # logo/signature are normally set via the upload endpoints, but can be set/cleared here too.
    logo_url: str | None = None
    signature_url: str | None = None


class UploadResponse(BaseModel):
    url: str


class FieldSettingsOut(BaseModel):
    field_settings: dict[str, dict[str, bool]]
    available_fields: dict[str, dict[str, list[str]]]


class FieldSettingsUpdate(BaseModel):
    field_settings: dict[str, dict[str, bool]]

