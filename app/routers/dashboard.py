from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission
from app.models import Customer, Supplier, User
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
    branch_id: str | None = Query(
        default=None,
        description="Accepted and validated against the firm's branches, but no "
                    "transaction carries a branch yet, so it does not narrow the figures",
    ),
    warehouse_id: str | None = Query(default=None, description="Narrows purchases"),
    customer_id: str | None = Query(
        default=None, description="Narrows sales, orders, receivables and cashflow-in"
    ),
    supplier_id: str | None = Query(
        default=None, description="Narrows purchases, payables and cashflow-out"
    ),
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> AdminDashboardOut:
    """Every Admin Dashboard widget in one call, for the logged-in user's firm.

    The organization comes from the token — a client cannot ask for another firm's
    numbers, and does not send an organization id at all.

    Blocks map one-to-one onto the page: `summary` (stat cards), `orders`,
    `cashflow`, `receivables_payables`, `top_customers`, `top_products`,
    `expense_breakdown`, `sales_trend`, `stock_watch` and `recent_orders`, plus a
    `filters` echo of the window the figures cover.

    Summarised only — "View Sales Report", "View All Orders" and the other drill-ins
    keep using /reports/{type} and the module list endpoints.

    Definitions worth knowing: a sale is an order past approval, purchases and
    expenses count only approved ones, gross profit is sales − purchases and net is
    gross − expenses (the same arithmetic as the profit-loss report). Balances
    (`receivables_payables`) and `stock_watch` are a position as of now, so the date
    range does not apply to them.
    """
    org_id = _org_id(user)
    _in_org(db, Customer, customer_id, org_id, "customer_id")
    _in_org(db, Supplier, supplier_id, org_id, "supplier_id")
    if branch_id is not None:
        branches = (user.organization.branch_addresses or []) if user.organization else []
        if not any(isinstance(b, dict) and b.get("id") == branch_id for b in branches):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="branch_id is not a branch of your firm"
            )
    return dashboard_service.build_admin_dashboard(
        db, org_id,
        date_from=date_from, date_to=date_to, branch_id=branch_id,
        warehouse_id=warehouse_id, customer_id=customer_id, supplier_id=supplier_id,
    )
