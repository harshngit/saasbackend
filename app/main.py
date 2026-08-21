from fastapi import FastAPI
from sqlalchemy import text as sa_text
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
    Brand,
    Category,
    Customer,
    Notification,
    NumberSequence,
    Organization,
    StoredFile,
    Plan,
    Product,
    ProductPricing,
    ProductSerial,
    ProductVariant,
    RefreshToken,
    Role,
    SalesOrder,
    SalesOrderItem,
    StockBatch,
    StockMovement,
    StockReservation,
    Supplier,
    SupplierPayment,
    User,
    Vehicle,
    Warehouse,
    WarehouseStock,
)
from app.services.file_migration_service import convert_inline_uploads
from app.services.numbering_service import backfill_missing_numbers
from app.services.product_pricing_service import backfill_product_pricing
from app.services.stock_service import migrate_order_statuses
from app.routers import (
    attendance,
    auth,
    brands,
    categories,
    customers,
    dashboard,
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
    settings as settings_router,
    suppliers,
    superadmin,
    users,
    vehicle_stock,
    vehicles,
    warehouses,
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
BUILD_TAG = "variant-upsert-and-guarded-delete"


@app.on_event("startup")
def on_startup() -> None:
    _log.info("Starting CRM API — build: %s", BUILD_TAG)

    # Every startup step is isolated, including creating the tables: a database that
    # is unreachable at boot must not kill the process. An exception raised here fails
    # the ASGI lifespan, uvicorn exits, the platform restarts it, and the service sits
    # in a crash loop answering nothing at all -- so an outage in the database becomes
    # a total outage with no way to see why. Booting anyway keeps /health answering,
    # and it says which half is broken.
    def _create_tables() -> None:
        # For local dev we auto-create tables. In production, use Alembic migrations.
        Base.metadata.create_all(bind=engine)

    for label, step in (
        ("create_all", _create_tables),
        ("extend_pg_enum_types", extend_pg_enum_types),
        ("drop_legacy_columns", drop_legacy_columns),
        ("relax_not_null_columns", relax_not_null_columns),
        ("widen_columns_to_text", widen_columns_to_text),
        ("add_columns_with_default", add_columns_with_default),
        ("auto_add_missing_columns", auto_add_missing_columns),
        ("backfill_missing_numbers", backfill_missing_numbers),
        ("migrate_order_statuses", migrate_order_statuses),
        ("convert_inline_uploads", convert_inline_uploads),
        ("backfill_product_pricing", backfill_product_pricing),
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


def _database_state() -> tuple[str, str | None]:
    """Whether the database answers, and what went wrong when it does not."""
    try:
        with engine.connect() as connection:
            connection.execute(sa_text("SELECT 1"))
        return "ok", None
    except Exception as exc:  # noqa: BLE001 -- reported, never raised
        return "unreachable", f"{type(exc).__name__}: {exc}"[:300]


@app.get("/health", tags=["health"])
def health() -> dict[str, str | None]:
    """Liveness, plus whether the database behind it is actually reachable.

    Deliberately still 200 when the database is down: this is what the platform
    restarts the instance on, and restarting cannot fix a database outage -- it only
    produces a crash loop that answers nothing. `database` carries the truth, so an
    outage is visible from outside without needing the platform logs.
    """
    state, detail = _database_state()
    body: dict[str, str | None] = {"status": "ok", "build": BUILD_TAG, "database": state}
    if detail:
        body["database_error"] = detail
    return body


app.include_router(files.router)
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(roles.router)
app.include_router(customers.router)
app.include_router(categories.router)
app.include_router(brands.router)
app.include_router(products.router)
app.include_router(inventory.router)
app.include_router(warehouses.router)
app.include_router(suppliers.router)
app.include_router(sales_orders.router)
app.include_router(purchases.router, prefix="/purchase-invoices")
app.include_router(purchases.router, prefix="/purchases")
app.include_router(invoices.router)
app.include_router(invoices.orders_router)  # POST /orders/{id}/invoice
app.include_router(vehicle_stock.router)
app.include_router(vehicles.router)
app.include_router(deliveries.router)
app.include_router(expenses.router)
app.include_router(attendance.router)
app.include_router(reports.router)
app.include_router(dashboard.router)
app.include_router(notifications.router)
app.include_router(plans.router)
app.include_router(organizations.router)
app.include_router(settings_router.router)
app.include_router(superadmin.router)
app.include_router(leads.router)
app.include_router(quotations.router)
app.include_router(payment_receipts.router)
app.include_router(sales_returns.router)

