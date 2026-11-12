"""Support tickets: an organization user raises a ticket, platform staff answer it.

Three tables (alembic e7f1a2b3c4d5):
  support_tickets             one per ticket, numbered TKT-YYYY-NNNN across every firm
  support_ticket_messages     the conversation; `is_internal` notes are platform-only
  support_ticket_attachments  uploaded files (stored_files) on the ticket or on a message

Values are plain strings checked against the tuples below, like every other status
column in this codebase (no native PostgreSQL enums).
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

TICKET_CATEGORIES = ("bug", "billing", "feature_request", "how_to", "other")
TICKET_PRIORITIES = ("low", "medium", "high", "urgent")
TICKET_STATUSES = ("open", "in_progress", "waiting_on_customer", "resolved", "closed")
AUTHOR_TYPES = ("customer", "platform")

SUBJECT_MAX = 200
MESSAGE_MAX = 5000
ATTACHMENTS_PER_MESSAGE = 3
ATTACHMENT_FILE_UNIQUE = "uq_support_ticket_attachments_stored_file"


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SupportTicket(Base):
    __tablename__ = "support_tickets"
    __table_args__ = (
        Index("ix_support_tickets_org_status", "organization_id", "status"),
        Index("ix_support_tickets_status_priority", "status", "priority"),
        UniqueConstraint("ticket_number", name="uq_support_tickets_ticket_number"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    ticket_number: Mapped[str] = mapped_column(String(30), nullable=False)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # SET NULL keeps the ticket (and its history) if the user is deleted.
    created_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    subject: Mapped[str] = mapped_column(String(SUBJECT_MAX), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    priority: Mapped[str] = mapped_column(String(20), default="medium", nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="open", nullable=False)
    # A platform user (no organization), validated by the service.
    assigned_to_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False, index=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    organization = relationship("Organization")
    created_by = relationship("User", foreign_keys=[created_by_user_id])
    assigned_to = relationship("User", foreign_keys=[assigned_to_user_id])
    messages: Mapped[list["SupportTicketMessage"]] = relationship(
        back_populates="ticket", cascade="all, delete-orphan", order_by="SupportTicketMessage.created_at"
    )
    attachments: Mapped[list["SupportTicketAttachment"]] = relationship(
        back_populates="ticket", cascade="all, delete-orphan", order_by="SupportTicketAttachment.created_at"
    )


class SupportTicketMessage(Base):
    __tablename__ = "support_ticket_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    ticket_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("support_tickets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    author_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    author_type: Mapped[str] = mapped_column(String(20), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)  # length checked by the schema (5000)
    # Platform-only, text-only notes: never returned to the organization, never notified.
    is_internal: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    ticket: Mapped[SupportTicket] = relationship(back_populates="messages")
    author = relationship("User", foreign_keys=[author_user_id])
    attachments: Mapped[list["SupportTicketAttachment"]] = relationship(back_populates="message")


class SupportTicketAttachment(Base):
    """One uploaded file on a ticket (message_id NULL: attached when the ticket was
    raised) or on one of its messages. A stored file can be attached only once."""

    __tablename__ = "support_ticket_attachments"
    __table_args__ = (UniqueConstraint("stored_file_id", name=ATTACHMENT_FILE_UNIQUE),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    ticket_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("support_tickets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    message_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("support_ticket_messages.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # RESTRICT: a file still attached to a ticket cannot be deleted from under it.
    stored_file_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("stored_files.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    ticket: Mapped[SupportTicket] = relationship(back_populates="attachments")
    message: Mapped[SupportTicketMessage | None] = relationship(back_populates="attachments")
    stored_file = relationship("StoredFile")
