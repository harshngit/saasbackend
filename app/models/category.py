import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Name uniqueness, compared trimmed and case-insensitively (alembic revision
# c4a7e9b2d1f3). A top-level name is unique in its organization; a sub category name is
# unique only under its own parent, so Electronics > Accessories and Footwear >
# Accessories can both exist. Two partial indexes, not one unique constraint over
# (organization_id, parent_id, name): an ordinary unique constraint treats NULL
# parent_ids as distinct, which would let duplicate top-level names through.
TOP_LEVEL_NAME_INDEX = "ux_categories_org_top_name"
SUBCATEGORY_NAME_INDEX = "ux_categories_org_parent_name"
NORMALIZED_NAME_SQL = "lower(trim(name))"


class Category(Base):
    """A product category, scoped to one organization."""

    __tablename__ = "categories"
    __table_args__ = (
        Index(
            TOP_LEVEL_NAME_INDEX, "organization_id", text(NORMALIZED_NAME_SQL), unique=True,
            postgresql_where=text("parent_id IS NULL"), sqlite_where=text("parent_id IS NULL"),
        ),
        Index(
            SUBCATEGORY_NAME_INDEX, "organization_id", "parent_id", text(NORMALIZED_NAME_SQL), unique=True,
            postgresql_where=text("parent_id IS NOT NULL"), sqlite_where=text("parent_id IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    image: Mapped[str | None] = mapped_column(Text, nullable=True)        # data: URL or external URL
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Self-referential: lets a category be a subcategory of another category in
    # the same organization. NULL = a top-level category.
    parent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("categories.id", ondelete="SET NULL"), nullable=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    # Joined (one level) so a sub category's parent name comes with it, without a
    # query per row -- a sub category name alone no longer identifies it.
    parent: Mapped["Category | None"] = relationship(
        remote_side=[id], back_populates="children", lazy="joined", join_depth=1
    )
    children: Mapped[list["Category"]] = relationship(back_populates="parent")

    @property
    def parent_name(self) -> str | None:
        return self.parent.name if self.parent_id and self.parent is not None else None
