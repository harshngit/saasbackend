from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models import Organization, User

router = APIRouter(prefix="/companies", tags=["companies"])


class CompanyFilterOut(BaseModel):
    id: str
    name: str
    is_active: bool


@router.get("", response_model=list[CompanyFilterOut])
def list_companies_for_filter(
    active: bool | None = Query(default=None, description="When true, return only active companies"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[CompanyFilterOut]:
    """GET /companies?active=true: List companies available for Dashboard filtering."""
    if not user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account"
        )

    org = user.organization or db.get(Organization, user.organization_id)
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found"
        )

    is_act = (org.company_status != "inactive")
    item = CompanyFilterOut(
        id=org.id,
        name=org.name,
        is_active=is_act,
    )

    if active is True and not is_act:
        return []
    if active is False and is_act:
        return []

    return [item]
