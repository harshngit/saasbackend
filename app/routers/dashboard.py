from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission
from app.models import Customer, SalesOrder, Supplier, User, Warehouse
from app.schemas.dashboard import (
    AdminDashboardOut,
    DeliveryPartnerDashboardOut,
    MyDeliveryDashboardOut,
    MySalesDashboardOut,
)
from app.schemas.staff_overview import DELIVERY, SALES
from app.schemas.sales_order import OrderOut
from app.services import dashboard_service, staff_overview_service

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


# ------------------------- mobile "my dashboard" --------------------------
# The field app's dashboards: the logged-in employee's own figures, nothing firm-wide.
# There is deliberately no user id parameter -- the token says whose figures these are.


def _require_workspace(user: User, workspace: str) -> None:
    role = user.role_detail
    if (role.workspace if role is not None else None) != workspace:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"This dashboard is for staff in the {workspace} workspace",
        )


_PERIOD = Query(default="today", description="today | week (last 7 days) | month (last 30 days)")
_FROM = Query(default=None, description="YYYY-MM-DD; a custom range instead of `period`")
_TO = Query(default=None, description="YYYY-MM-DD; defaults to today")


@router.get("/me/sales", response_model=MySalesDashboardOut)
def my_sales_dashboard(
    period: str = _PERIOD,
    date_from: str | None = _FROM,
    date_to: str | None = _TO,
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> MySalesDashboardOut:
    """The salesperson's own dashboard for the field app: period sales and orders, today's
    sales, assigned customers, a per-day series and the 5 latest orders. Same definitions
    as the sales block of GET /users/{id}/overview. Staff in the sales workspace only."""
    _org_id(user)
    _require_workspace(user, SALES)
    return staff_overview_service.build_my_sales_dashboard(db, user, period, date_from, date_to)


@router.get("/me/delivery", response_model=MyDeliveryDashboardOut)
def my_delivery_dashboard(
    period: str = _PERIOD,
    date_from: str | None = _FROM,
    date_to: str | None = _TO,
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> MyDeliveryDashboardOut:
    """The delivery partner's own dashboard for the field app: deliveries by outcome, value,
    collected and receivable, a per-day series, open deliveries (count + 5 latest) and the
    van. Same definitions as the delivery block of GET /users/{id}/overview. Staff in the
    delivery workspace only."""
    _org_id(user)
    _require_workspace(user, DELIVERY)
    return staff_overview_service.build_my_delivery_dashboard(db, user, period, date_from, date_to)


@router.get("/delivery-partner", response_model=DeliveryPartnerDashboardOut)
def delivery_partner_dashboard(
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> DeliveryPartnerDashboardOut:
    """Delivery partner dashboard KPIs: company-wide total orders and partner-assigned deliveries."""
    org_id = _org_id(user)
    return dashboard_service.build_delivery_partner_dashboard(db, org_id, user.id)


from app.schemas.pagination import PaginatedResponse


@router.get("/delivery-partner/orders", response_model=PaginatedResponse[OrderOut])
def list_delivery_partner_company_orders(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    status_filter: str | None = Query(
        default=None,
        alias="status",
        description="draft | confirmed | completed | cancelled. Filter orders by status.",
    ),
    fulfilment_status: str | None = Query(
        default=None,
        description="not_started | reserved | planned | loaded | in_transit | "
                    "partially_delivered | delivered | failed",
    ),
    customer_id: str | None = Query(default=None),
    search: str | None = Query(default=None, description="matches order_number"),
    sort_by: str | None = Query(default=None, description="Field to sort by"),
    sort_order: str | None = Query(default=None, description="Sort order: asc or desc"),
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> PaginatedResponse[SalesOrder]:
    """Read-only company-wide orders list for the authenticated delivery partner."""
    org_id = _org_id(user)
    return dashboard_service.list_delivery_partner_company_orders(
        db,
        org_id,
        page=page,
        page_size=page_size,
        status_filter=status_filter,
        fulfilment_status=fulfilment_status,
        customer_id=customer_id,
        search=search,
        sort_by=sort_by,
        sort_order=sort_order,
    )

