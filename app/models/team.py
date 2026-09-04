import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Team(Base):
    """A Sales/field team within one organization — the grouping Team Data
    Scope ("team") uses to decide, at read time, which users' records a
    teammate may see. Membership lives entirely on `User.team_id` (a plain
    FK, not a join table — one user belongs to at most one team) and is
    never copied onto CRM records themselves: a Lead/Customer/Visit/
    Follow-up/Quotation/Order's visibility under team scope always follows
    its owner's *current* team, resolved dynamically by app.core.scoping.
    """

    __tablename__ = "teams"
    __table_args__ = (UniqueConstraint("organization_id", "name", name="uq_team_org_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    # Nullable at the DB level (defensive against the manager user someday
    # being removed) but application logic (team_service) never leaves a team
    # without one -- create requires it, and update refuses to clear it
    # without naming a replacement in the same call.
    manager_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    # post_update=True: Team.manager_id and User.team_id are two separate FKs
    # between the same two tables (a Team points at its manager; a User
    # points at their Team), which otherwise makes SQLAlchemy's flush-order
    # resolution raise CircularDependencyError the moment both a Team row's
    # manager_id and a member User row's team_id need writing in the same
    # transaction (exactly what create_team/update_team do). This tells
    # SQLAlchemy to write manager_id via a separate secondary UPDATE after
    # the main flush, breaking the cycle without changing the data model.
    manager: Mapped["User | None"] = relationship(  # noqa: F821
        foreign_keys=[manager_id], lazy="joined", post_update=True
    )
    # The reverse of User.team_id -- every user currently on this team,
    # manager included (team_service.py enforces manager.team_id == team.id).
    members: Mapped[list["User"]] = relationship(  # noqa: F821
        foreign_keys="User.team_id", back_populates="team", lazy="selectin"
    )
