from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models import Plan
from app.schemas.plan import PlanOut

router = APIRouter(prefix="/plans", tags=["plans"])


@router.get("", response_model=list[PlanOut])
def list_active_plans(
    _user=Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[Plan]:
    """Active plans for the Admin's 'Plans' page. Any authenticated user can view."""
    return db.query(Plan).filter(Plan.is_active.is_(True)).order_by(Plan.price_monthly).all()
