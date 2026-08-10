from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.database import (
    Base,
    add_columns_with_default,
    auto_add_missing_columns,
    drop_legacy_columns,
    engine,
    extend_pg_enum_types,
    relax_not_null_columns,
    widen_columns_to_text,
)
from app.models import (  # noqa: F401  (register mappers)
    ActivityLog,
    Attendance,
    Category,
    Customer,
    Notification,
    NumberSequence,
    Organization,
    StoredFile,
    Plan,
    Product,
    ProductVariant,
    RefreshToken,
    Role,
    SalesOrder,
    SalesOrderItem,
    StockMovement,
    Supplier,
    SupplierPayment,
    User,
)
from app.services.file_migration_service import convert_inline_uploads
from app.services.numbering_service import backfill_missing_numbers
from app.routers import (
    attendance,
    auth,
    categories,
    customers,
    deliveries,
    expenses,
    files,
    inventory,
    invoices,
    notifications,
    organizations,
    plans,
    products,
    purchases,
    reports,
    roles,
    sales_orders,
    suppliers,
    superadmin,
    users,
    vehicle_stock,
    leads,
    quotations,
    payment_receipts,
    sales_returns,
)

app = FastAPI(
    title="CRM SaaS API",
    description="Backend for the CRM / Billing / Inventory SaaS. Auth & user management.",
    version="0.1.0",
)

# CORS: with credentials enabled, the browser rejects a wildcard "*" origin — the
# response must echo the *specific* request origin. So when CORS_ORIGINS is "*",
# use an allow-all regex (Starlette then echoes the caller's origin) instead of a
# literal "*". Otherwise use the explicit allow-list.
_cors_kwargs = dict(allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
if "*" in settings.cors_origin_list:
    app.add_middleware(CORSMiddleware, allow_origin_regex=".*", **_cors_kwargs)
else:
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, **_cors_kwargs)


import logging

_log = logging.getLogger("crm.startup")

# Bump when the deployed feature set changes, so /health and logs confirm the build.
BUILD_TAG = "sectioned-employee-and-company-overview"


@app.on_event("startup")
def on_startup() -> None:
    _log.info("Starting CRM API — build: %s", BUILD_TAG)

    # For local dev we auto-create tables. In production, use Alembic migrations instead.
    Base.metadata.create_all(bind=engine)

    # Each migration step is isolated: a failure is logged but must NOT crash the
    # app (a crash loop makes Render revert to the previous deploy). SQLite dev is
    # unaffected; these mainly matter for the live Postgres.
    for label, step in (
        ("extend_pg_enum_types", extend_pg_enum_types),
        ("drop_legacy_columns", drop_legacy_columns),
        ("relax_not_null_columns", relax_not_null_columns),
        ("widen_columns_to_text", widen_columns_to_text),
        ("add_columns_with_default", add_columns_with_default),
        ("auto_add_missing_columns", auto_add_missing_columns),
        ("backfill_missing_numbers", backfill_missing_numbers),
        ("convert_inline_uploads", convert_inline_uploads),
    ):
        try:
            step()
        except Exception:  # noqa: BLE001
            _log.exception("Startup migration step failed: %s", label)

    if settings.seed_on_startup:
        try:
            from app.seed import main as seed_main

            seed_main()
        except Exception:  # noqa: BLE001
            _log.exception("Startup seed failed")

    _log.info("CRM API startup complete — build: %s", BUILD_TAG)


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok", "build": BUILD_TAG}


app.include_router(files.router)
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(roles.router)
app.include_router(customers.router)
app.include_router(categories.router)
app.include_router(products.router)
app.include_router(inventory.router)
app.include_router(suppliers.router)
app.include_router(sales_orders.router)
app.include_router(purchases.router, prefix="/purchase-invoices")
app.include_router(purchases.router, prefix="/purchases")
app.include_router(invoices.router)
app.include_router(invoices.orders_router)  # POST /orders/{id}/invoice
app.include_router(vehicle_stock.router)
app.include_router(deliveries.router)
app.include_router(expenses.router)
app.include_router(attendance.router)
app.include_router(reports.router)
app.include_router(notifications.router)
app.include_router(plans.router)
app.include_router(organizations.router)
app.include_router(superadmin.router)
app.include_router(leads.router)
app.include_router(quotations.router)
app.include_router(payment_receipts.router)
app.include_router(sales_returns.router)

