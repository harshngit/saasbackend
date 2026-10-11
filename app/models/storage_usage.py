import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.organization import Organization


def _now() -> datetime:
    return datetime.now(timezone.utc)


class OrganizationStorageUsage(Base):
    """Snapshot table tracking persistent storage usage by category for each organization.

    Maintains accounted byte counts for:
    - files_documents_bytes: sum of StoredFile.size for persistent tenant files.
    - stored_reports_bytes: reserved for persisted reports/exports (currently 0 for ephemeral).
    - application_data_bytes: logical approximation of organization-owned database records.
    - total_used_bytes: combined total bytes.
    """

    __tablename__ = "organization_storage_usage"

    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )

    files_documents_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    stored_reports_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    application_data_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    total_used_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    organization: Mapped["Organization"] = relationship(
        "Organization", backref="storage_usage", uselist=False
    )
