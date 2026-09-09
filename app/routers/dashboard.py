from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission
from app.models import Customer, Supplier, User, Warehouse
from app.schemas.dashboard import AdminDashboardOut
from app.services import dashboard_service

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

_view = require_permission("dashboard", "view")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account"
        )
    return user.organization_id


def _in_org(db: Session, model, record_id: str | None, org_id: str, label: str) -> None:  # noqa: ANN001
    """A filter naming something outside the firm is a mistake worth reporting,
    rather than silently returning an empty dashboard."""
    if record_id is None:
        return
    record = db.get(model, record_id)
    if record is None or record.organization_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"{label} is not in your firm"
        )


@router.get("/admin", response_model=AdminDashboardOut)
def admin_dashboard(
    date_from: str | None = Query(default=None, description="YYYY-MM-DD; defaults to the 1st of this month"),
    date_to: str | None = Query(default=None, description="YYYY-MM-DD; defaults to today"),
    company_id: str | None = Query(default=None, description="Filter by company/organization ID"),
    warehouse_id: str | None = Query(default=None, description="Narrows purchases and warehouse metrics"),
    customer_id: str | None = Query(
        default=None, description="Narrows sales, orders, receivables and cashflow-in"
    ),
    supplier_id: str | None = Query(
        default=None, description="Narrows purchases, payables and cashflow-out"
    ),
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> AdminDashboardOut:
    """Every Admin Dashboard widget in one call, for the logged-in user's firm."""
    org_id = _org_id(user)
    if company_id is not None and company_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="company_id is not in your firm"
        )
    _in_org(db, Warehouse, warehouse_id, org_id, "warehouse_id")
    _in_org(db, Customer, customer_id, org_id, "customer_id")
    _in_org(db, Supplier, supplier_id, org_id, "supplier_id")

    return dashboard_service.build_admin_dashboard(
        db, org_id,
        date_from=date_from, date_to=date_to, company_id=company_id,
        warehouse_id=warehouse_id, customer_id=customer_id, supplier_id=supplier_id,
    )
