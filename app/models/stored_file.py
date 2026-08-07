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
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
