import uuid

from sqlalchemy import Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class NumberSequence(Base):
    """The last auto-number issued for one firm / series / year.

    Kept as its own row rather than derived from the records themselves. Both
    obvious shortcuts reuse a number: counting rows repeats one as soon as
    anything is deleted, and max-of-existing does the same when the newest
    record is the one deleted. A stored counter only ever moves forward, so a
    number is never issued twice — which is the whole point of an invoice or
    receipt number.
    """

    __tablename__ = "number_sequences"
    __table_args__ = (
        UniqueConstraint("organization_id", "series", "year", name="uq_sequence_org_series_year"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    series: Mapped[str] = mapped_column(String(20), nullable=False)   # CUST, QT, INV, …
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    last_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1000)
