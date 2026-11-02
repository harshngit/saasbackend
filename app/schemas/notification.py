from datetime import datetime

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, computed_field

from app.core import notification_types as nt


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: str
    title: str
    body: str | None
    type: str
    link: str | None
    is_read: bool
    created_at: datetime
    # Added for push/in-app parity: the event and its identifiers, and when it was first read.
    data: dict[str, str] | None = None
    read_at: datetime | None = None
    organization_id: str | None = None
    recipient_user_id: str | None = Field(default=None, validation_alias=AliasChoices("recipient_user_id", "user_id"))

    # Derived from `data` (nothing extra is stored), so older rows carry them too.
    @computed_field
    @property
    def event(self) -> str | None:
        return (self.data or {}).get("event")

    @computed_field
    @property
    def scope(self) -> str:
        """"platform" for Super Admin / platform-staff notifications, else "organization"."""
        return "organization" if self.organization_id else "platform"

    @computed_field
    @property
    def entity_type(self) -> str | None:
        return nt.describe(self.data)["entity_type"]

    @computed_field
    @property
    def entity_id(self) -> str | None:
        return nt.describe(self.data)["entity_id"]

    @computed_field
    @property
    def action_url(self) -> str | None:
        return nt.describe(self.data)["action_url"]

    @computed_field
    @property
    def actor_id(self) -> str | None:
        return (self.data or {}).get("actor_id")


class UnreadCount(BaseModel):
    unread: int


class MessageResponse(BaseModel):
    detail: str


from app.schemas.bulk import BulkDelete, BulkDeleteResult  # noqa: E402

__all__ = [
    "NotificationOut",
    "UnreadCount",
    "MessageResponse",
    "BulkDelete",
    "BulkDeleteResult",
]
