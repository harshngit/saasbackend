"""Team management: an Organization Admin's grouping of staff into Sales/field
teams, exactly one manager each. Membership lives entirely on `User.team_id` —
a user belongs to at most one Team, and moving them between Teams never
touches any CRM record (see app.core.scoping for how Team Data Scope reads
that membership dynamically at request time).
"""

from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models import Team, User
from app.schemas.team import TeamCreate, TeamUpdate


def _now() -> datetime:
    return datetime.now(timezone.utc)


def name_taken(db: Session, organization_id: str, name: str, exclude_id: str | None = None) -> bool:
    query = db.query(Team).filter(Team.organization_id == organization_id, Team.name == name)
    if exclude_id is not None:
        query = query.filter(Team.id != exclude_id)
    return db.query(query.exists()).scalar()


def get_team(db: Session, organization_id: str, team_id: str) -> Team:
    """The team, only if it belongs to this org — no cross-org access."""
    team = db.get(Team, team_id)
    if team is None or team.organization_id != organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")
    return team


def list_teams(db: Session, organization_id: str) -> list[Team]:
    return (
        db.query(Team)
        .filter(Team.organization_id == organization_id)
        .order_by(Team.name)
        .all()
    )


def _validate_org_user(db: Session, organization_id: str, user_id: str, field: str) -> User:
    user = db.get(User, user_id)
    if user is None or user.organization_id != organization_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{field} is not a user in your firm")
    return user


def create_team(db: Session, organization_id: str, payload: TeamCreate) -> Team:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Team name is required")
    if name_taken(db, organization_id, name):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A team with this name already exists")

    manager = _validate_org_user(db, organization_id, payload.manager_id, "manager_id")

    # The manager is always a member, whether or not the caller repeated
    # their id in member_ids.
    member_ids = set(payload.member_ids or [])
    member_ids.add(manager.id)
    members = [
        manager if uid == manager.id else _validate_org_user(db, organization_id, uid, "member_ids")
        for uid in member_ids
    ]

    team = Team(organization_id=organization_id, name=name, manager_id=manager.id)
    db.add(team)
    db.flush()  # assign team.id before pointing members at it

    # Reassignment is implicit and safe: setting team_id here simply moves a
    # user out of whatever team they previously belonged to (if any) — their
    # historical Leads/Customers/Visits/Follow-ups/Quotations/Orders are
    # never touched, only which team's roster they currently appear on.
    for member in members:
        member.team_id = team.id

    db.commit()
    db.refresh(team)
    return team


def update_team(db: Session, organization_id: str, team: Team, payload: TeamUpdate) -> Team:
    data = payload.model_dump(exclude_unset=True)
    changed = False

    if "name" in data:
        new_name = (data["name"] or "").strip()
        if not new_name:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Team name is required")
        if name_taken(db, organization_id, new_name, exclude_id=team.id):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A team with this name already exists")
        team.name = new_name
        changed = True

    if "manager_id" in data:
        new_manager_id = data["manager_id"]
        if not new_manager_id:
            # A Team must always have exactly one manager — refuse clearing
            # it without naming a replacement in the same request.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="manager_id is required — a Team must always have exactly one manager. "
                       "Name a replacement instead of removing the current one.",
            )
        new_manager = _validate_org_user(db, organization_id, new_manager_id, "manager_id")
        new_manager.team_id = team.id  # the new manager must be a member
        team.manager_id = new_manager.id
        changed = True
        # The previous manager is deliberately left as an ordinary member —
        # only an explicit member_ids in this same request removes them.

    if "member_ids" in data:
        # Full replace of membership. The (possibly just-updated) manager is
        # always kept, whether or not the caller repeated their id.
        member_ids = set(data["member_ids"] or [])
        member_ids.add(team.manager_id)
        new_members = [
            _validate_org_user(db, organization_id, uid, "member_ids") for uid in member_ids
        ]
        keep_ids = {u.id for u in new_members}

        # Clear team_id for whoever is currently on the team but not in the
        # new roster — they are not deleted, only detached from this team.
        current_members = db.query(User).filter(User.team_id == team.id).all()
        for member in current_members:
            if member.id not in keep_ids:
                member.team_id = None
        for member in new_members:
            member.team_id = team.id
        changed = True

    if changed:
        team.updated_at = _now()
    db.commit()
    db.refresh(team)
    return team


def delete_team(db: Session, team: Team) -> None:
    """Delete the Team. Every user currently on it is detached (team_id set
    to NULL) — explicitly, not left to the FK's ON DELETE SET NULL alone (the
    same defensive reasoning used elsewhere in this project for FK gaps on
    already-deployed databases). No User, and no CRM record — Lead, Customer,
    Visit, Follow-up, Quotation, Order — is ever touched: their ownership
    fields never referenced the Team to begin with.
    """
    db.query(User).filter(User.team_id == team.id).update({"team_id": None})
    db.delete(team)
    db.commit()
