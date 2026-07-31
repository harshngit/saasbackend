import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# Reasons a product's stock changes. Stored as strings (no native DB enum).
STOCK_MOVEMENT_TYPES = {
    "opening",          # initial stock
    "purchase_in",      # received through a purchase (+)
    "sale_out",         # deducted by a sale (-)
    "delivery_out",     # deducted by a delivery (-)
    "sales_return",     # customer returned goods (+)
    "purchase_return",  # returned to supplier (-)
    "damaged",          # damaged stock removed (-)
    "expired",          # expired stock removed (-)
    "adjustment",       # manual correction (+/-)
}


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class StockMovement(Base):
    """An append-only ledger row recording a change to a product/variant's stock."""

    __tablename__ = "stock_movements"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    variant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("product_variants.id", ondelete="CASCADE"), nullable=True, index=True
    )

    movement_type: Mapped[str] = mapped_column(String(30), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)   # signed: + adds, - removes
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)  # stock after this movement
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)  # user id

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
