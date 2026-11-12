import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission
from app.core.id_filters import normalize_ids
from app.models import Customer, SalesOrder, Supplier, User, Warehouse
from app.schemas.dashboard import (
    AdminDashboardOut,
    DeliveryPartnerDashboardOut,
    MyDeliveryDashboardOut,
    MySalesDashboardOut,
)
from app.schemas.staff_overview import DELIVERY, SALES
from app.schemas.sales_order import OrderOut
from app.services import dashboard_service, role_service, staff_overview_service

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

_view = require_permission("dashboard", "view")
_log = logging.getLogger("crm.dashboard")


def _firm_wide_view(user: User = Depends(_view), db: Session = Depends(get_db)) -> User:
    """The admin dashboard shows the whole firm's figures, so besides dashboard:view the
    caller's role must see all records (data scope "all") -- admins always do. An own- or
    team-scope role (Sales Officer, Delivery Partner by default) gets 403 and uses its own
    dashboard (/dashboard/me/sales, /dashboard/me/delivery) instead. Decided by permission
    and scope, never by role name."""
    if role_service.is_full_access(user) or role_service.data_scope(db, user) == "all":
        return user
    role = role_service.role_for(db, user)
    _log.info("dashboard/admin refused: role %r has data scope %r",
              getattr(role, "name", None), role_service.data_scope(db, user))
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="The admin dashboard shows the whole firm's figures; your role only sees its own records",
    )


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account"
        )
    return user.organization_id


def _all_in_org(db: Session, model, ids: tuple[str, ...], org_id: str, label: str) -> None:  # noqa: ANN001
    """Every ID in a filter must belong to the firm. One that does not -- unknown, or
    another firm's -- is a mistake worth reporting rather than silently returning an
    empty dashboard, and it rejects the whole request. One query for all of them."""
    if not ids:
        return
    found = {
        row[0]
        for row in db.query(model.id).filter(model.id.in_(ids), model.organization_id == org_id)
    }
    if len(found) != len(ids):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"{label} is not in your firm"
        )


_MULTI = (
    "One or more IDs: repeat the parameter (?{name}=A&{name}=B), separate with commas "
    "(?{name}=A,B), or both. "
)


@router.get("/admin", response_model=AdminDashboardOut)
def admin_dashboard(
    date_from: str | None = Query(default=None, description="YYYY-MM-DD; defaults to the 1st of this month"),
    date_to: str | None = Query(default=None, description="YYYY-MM-DD; defaults to today"),
    company_id: list[str] | None = Query(
        default=None, description=_MULTI.format(name="company_id") + "Must be your own organization."
    ),
    warehouse_id: list[str] | None = Query(
        default=None,
        description=_MULTI.format(name="warehouse_id") + "Narrows purchases and warehouse metrics; "
        "Stock Watch flags a product that is low in ANY selected warehouse.",
    ),
    customer_id: list[str] | None = Query(
        default=None, description=_MULTI.format(name="customer_id") + "Narrows sales, orders, receivables and cashflow-in"
    ),
    supplier_id: list[str] | None = Query(
        default=None, description=_MULTI.format(name="supplier_id") + "Narrows purchases, payables and cashflow-out"
    ),
    user: User = Depends(_firm_wide_view),
    db: Session = Depends(get_db),
) -> AdminDashboardOut:
    """Every Admin Dashboard widget in one call, for the logged-in user's firm."""
    org_id = _org_id(user)
    company_ids = normalize_ids(company_id)
    warehouse_ids = normalize_ids(warehouse_id)
    customer_ids = normalize_ids(customer_id)
    supplier_ids = normalize_ids(supplier_id)
    if any(cid != org_id for cid in company_ids):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="company_id is not in your firm"
        )
    _all_in_org(db, Warehouse, warehouse_ids, org_id, "warehouse_id")
    _all_in_org(db, Customer, customer_ids, org_id, "customer_id")
    _all_in_org(db, Supplier, supplier_ids, org_id, "supplier_id")

    return dashboard_service.build_admin_dashboard(
        db, org_id,
        date_from=date_from, date_to=date_to, company_ids=company_ids,
        warehouse_ids=warehouse_ids, customer_ids=customer_ids, supplier_ids=supplier_ids,
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

