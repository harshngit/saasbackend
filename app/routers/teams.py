from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_system_role
from app.models import SystemRole, Team, User
from app.schemas.team import TeamCreate, TeamOut, TeamUpdate
from app.services import team_service

router = APIRouter(prefix="/teams", tags=["teams"])

# Same admin-only dependency pattern as app/routers/roles.py — Team
# management is an org-management action, not a permission-matrix one.
_ADMIN = require_system_role(SystemRole.ADMIN)


def _out(team: Team) -> TeamOut:
    out = TeamOut.model_validate(team)
    out.member_count = len(team.members)
    return out


@router.post("", response_model=TeamOut, status_code=status.HTTP_201_CREATED)
def create_team(
    payload: TeamCreate,
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> TeamOut:
    team = team_service.create_team(db, admin.organization_id, payload)
    return _out(team)


@router.get("", response_model=list[TeamOut])
def list_teams(
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> list[TeamOut]:
    return [_out(t) for t in team_service.list_teams(db, admin.organization_id)]


@router.get("/{team_id}", response_model=TeamOut)
def get_team(
    team_id: str,
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> TeamOut:
    return _out(team_service.get_team(db, admin.organization_id, team_id))


@router.patch("/{team_id}", response_model=TeamOut)
def update_team(
    team_id: str,
    payload: TeamUpdate,
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> TeamOut:
    team = team_service.get_team(db, admin.organization_id, team_id)
    team = team_service.update_team(db, admin.organization_id, team, payload)
    return _out(team)


@router.delete("/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_team(
    team_id: str,
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> None:
    team = team_service.get_team(db, admin.organization_id, team_id)
    team_service.delete_team(db, team)
