import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class StoredFile(Base):
    """An uploaded file, served back over a real URL instead of being inlined.

    Previously every upload was base64'd into a `data:` URL and written straight
    into the record's column, so a single product or employee response could carry
    megabytes of image data. The bytes live here now and the record keeps only
    `/files/{id}`, which is what the API returns.

    Bytes are in the database rather than on disk because Render's filesystem is
    ephemeral — a redeploy would lose every upload. When this moves to S3 or
    Cloudinary only `save_upload` changes; the stored URL stays a URL.
    """

    __tablename__ = "stored_files"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # Nullable so a file can outlive the firm's row order during bootstrapping;
    # every upload path sets it.
    organization_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Exactly one of these is populated per row, never both:
    #   storage_key set, data NULL   -> bytes live in R2 (see app/core/r2.py)
    #   storage_key NULL, data set   -> bytes live here, exactly as before R2 existed
    # Both nullable so existing DB-backed rows (storage_key NULL) stay valid without
    # a data backfill, and a Phase 2 R2 migration can null out `data` once its bytes
    # are safely uploaded and verified (app/scripts/move_files_to_r2.py).
    storage_key: Mapped[str | None] = mapped_column(String(500), nullable=True, index=True)
    data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
