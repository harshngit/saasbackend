from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator


class PushDeviceRegister(BaseModel):
    """Register (or refresh) this device's FCM token for the signed-in user. Ownership comes
    from the access token only -- there is no user or organization field to set."""

    token: str = Field(min_length=20, max_length=1024, description="FCM registration token from the client SDK")
    platform: Literal["web", "android"]
    label: str | None = Field(default=None, max_length=100, description='Optional, e.g. "Chrome on Windows"')

    @field_validator("token")
    @classmethod
    def _clean_token(cls, v: str) -> str:
        v = v.strip()
        if any(ch.isspace() for ch in v):
            raise ValueError("token must not contain whitespace")
        return v


class PushDeviceOut(BaseModel):
    """A registered device. The token itself is never echoed back -- only its last 6 characters."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    platform: str
    label: str | None
    created_at: datetime
    updated_at: datetime
    last_seen_at: datetime
    token: str = Field(exclude=True)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def token_suffix(self) -> str:
        return self.token[-6:]


class PushTestResult(BaseModel):
    """Outcome of a push send. `accepted` means Firebase accepted the message for delivery,
    not that a device displayed it."""

    enabled: bool
    attempted: int
    accepted: int
    invalid_tokens_removed: int
    transient_failures: int
    detail: str | None = None
