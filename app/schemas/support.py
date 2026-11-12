"""Support ticket request and response schemas (org: /support, platform: /platform/support)."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

from app.models.support_ticket import ATTACHMENTS_PER_MESSAGE, MESSAGE_MAX, SUBJECT_MAX

TicketCategory = Literal["bug", "billing", "feature_request", "how_to", "other"]
TicketPriority = Literal["low", "medium", "high", "urgent"]
TicketStatus = Literal["open", "in_progress", "waiting_on_customer", "resolved", "closed"]
AuthorType = Literal["customer", "platform"]

Subject = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=SUBJECT_MAX)]
Body = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MESSAGE_MAX)]

_FILE_IDS = (
    "IDs of files uploaded first with POST /files/upload (the `file_id` it returns), at most "
    f"{ATTACHMENTS_PER_MESSAGE}. Each must be an image or PDF uploaded by you, in your "
    "organization, and not attached to any ticket yet."
)


# ------------------------------------------------------------------ requests


class TicketCreate(BaseModel):
    subject: Subject = Field(description=f"Up to {SUBJECT_MAX} characters")
    description: Body = Field(description=f"Up to {MESSAGE_MAX} characters")
    category: TicketCategory
    priority: TicketPriority = "medium"
    attachment_file_ids: list[str] = Field(default_factory=list, max_length=ATTACHMENTS_PER_MESSAGE, description=_FILE_IDS)


class TicketMessageCreate(BaseModel):
    body: Body = Field(description=f"Up to {MESSAGE_MAX} characters")
    attachment_file_ids: list[str] = Field(default_factory=list, max_length=ATTACHMENTS_PER_MESSAGE, description=_FILE_IDS)


class PlatformMessageCreate(BaseModel):
    body: Body = Field(description=f"Up to {MESSAGE_MAX} characters")
    is_internal: bool = Field(
        default=False,
        description="true: an internal note, visible to platform users only, text only, notifies nobody",
    )
    attachment_file_ids: list[str] = Field(
        default_factory=list,
        description="Not supported on platform messages: must be empty (an internal note is text only).",
    )


class PlatformTicketUpdate(BaseModel):
    """Only the fields sent change. `assigned_to_user_id: null` unassigns."""

    status: TicketStatus | None = None
    priority: TicketPriority | None = None
    assigned_to_user_id: str | None = Field(
        default=None, description="A platform user's id; a Sales Manager may only assign themselves"
    )


# ------------------------------------------------------------------ responses


class TicketAttachmentOut(BaseModel):
    id: str
    file_id: str
    message_id: str | None = Field(default=None, description="null: attached when the ticket was raised")
    filename: str
    content_type: str
    size: int
    url: str
    created_at: datetime


class TicketMessageOut(BaseModel):
    id: str
    author_type: AuthorType
    author_user_id: str | None = None
    author_name: str | None = None
    body: str
    is_internal: bool = Field(default=False, description="Always false in organization responses")
    created_at: datetime
    attachments: list[TicketAttachmentOut] = Field(default_factory=list)


class TicketListItem(BaseModel):
    id: str
    ticket_number: str
    subject: str
    category: TicketCategory
    priority: TicketPriority
    status: TicketStatus
    created_by_user_id: str | None = None
    created_by_name: str | None = None
    assigned_to_user_id: str | None = None
    assigned_to_name: str | None = None
    last_activity_at: datetime
    created_at: datetime


class TicketOut(TicketListItem):
    description: str
    resolved_at: datetime | None = None
    closed_at: datetime | None = None
    updated_at: datetime
    attachments: list[TicketAttachmentOut] = Field(
        default_factory=list, description="Files attached when the ticket was raised"
    )
    messages: list[TicketMessageOut] = Field(
        default_factory=list, description="Oldest first. Organization responses never include internal notes."
    )


class PlatformTicketListItem(TicketListItem):
    organization_id: str
    organization_name: str | None = None


class PlatformTicketOut(TicketOut):
    organization_id: str
    organization_name: str | None = None


class SupportSummaryOut(BaseModel):
    total: int
    by_status: dict[str, int] = Field(description="Every status, 0 when none")
    by_priority: dict[str, int] = Field(description="Every priority, 0 when none")
    unassigned: int = Field(description="Not resolved or closed, and assigned to nobody")
    no_public_reply_over_24h: int = Field(
        description="Not resolved or closed; the latest customer activity (their last message, or "
                    "the ticket's creation when they have not written since) is older than 24 hours "
                    "and no public platform reply came after it. Internal notes do not count."
    )
