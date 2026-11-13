import logging
import os
import time

# Python prints only WARNING and above until logging is configured, so without this the
# INFO lines (push / email / reminder status, "N accepted") never reach the container logs.
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(levelname)s [%(name)s] %(message)s")

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.database import (
    Base,
    add_columns_with_default,
    auto_add_missing_columns,
    drop_legacy_columns,
    engine,
    enforce_unique_email,
    enforce_unique_google_id,
    extend_pg_enum_types,
    relax_not_null_columns,
    widen_columns_to_text,
)
from app.models import (  # noqa: F401  (register mappers)
    ActivityLog,
    Attendance,
    Brand,
    BrandCategory,
    Category,
    Customer,
    DeliveryCollection,
    Notification,
    NumberSequence,
    OAuthExchangeTicket,
    OAuthRegistrationTicket,
    Organization,
    OrganizationStorageUsage,
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
    SupplierBrand,
    Team,
    Vehicle,
    Visit,
    FollowUp,
    Leave,
    Warehouse,
    WarehouseStock,
)
from app.services import email_service
from app.services.file_migration_service import convert_inline_uploads
from app.services.numbering_service import backfill_missing_numbers
from app.services.product_pricing_service import backfill_product_pricing
from app.services.stock_service import migrate_order_statuses
from app.routers import (
    attendance,
    auth,
    billing,
    brands,
    categories,
    customers,
    dashboard,
    deliveries,
    expenses,
    files,
    follow_ups,
    inventory,
    invoices,
    leaves,
    notifications,
    organizations,
    plans,
    platform,
    products,
    purchases,
    grns,
    supplier_invoices,
    accounts_payable,
    supplier_payments,
    reports,
    roles,
    sales_orders,
    settings as settings_router,
    suppliers,
    superadmin,
    support,
    teams,
    users,
    vehicle_stock,
    vehicles,
    visits,
    warehouses,
    leads,
    quotations,
    payment_receipts,
    sales_returns,
    purchase_returns,
    transfers,
    companies,
    websocket,
    payment_webhooks,
    cash_reconciliation,
    delivery_day_end,
)

app = FastAPI(
    title="CRM SaaS API",
    description="Backend for the CRM / Billing / Inventory SaaS. Auth & user management.",
    version="0.1.0",
    docs_url="/docs" if settings.enable_docs else None,
    redoc_url="/redoc" if settings.enable_docs else None,
    openapi_url="/openapi.json" if settings.enable_docs else None,
)

if settings.perf_log:
    from app.core import perf

    perf.install_query_counter(engine)
    perf.install_middleware(app)

from app.core.database import install_disconnect_retry  # noqa: E402

install_disconnect_retry(app)

from app.core import request_guard  # noqa: E402

# Outside every other app middleware except the two below; CORS and the error
# middleware pass `receive` through untouched, so it still sees the client's disconnect.
request_guard.install(app, engine, settings.read_request_timeout_seconds)

from app.core.error_responses import (  # noqa: E402
    UnhandledErrorMiddleware,
    log_unhandled,
    server_error_response,
)

# An unhandled exception becomes a JSON 500 here, INSIDE CORS, so the browser can read it.
app.add_middleware(UnhandledErrorMiddleware)

# CORS is added LAST so it is the outermost app middleware: every response -- 4xx, 429,
# 500, 503 from the request guard -- passes through it and gets its CORS headers.
# With credentials enabled, browsers reject a wildcard "*" origin, so "*" becomes a
# regex; explicit production origins (e.g. https://crm-saas.asynk.in) sit alongside
# regex matching for localhost / 127.0.0.1 development ports.
_cors_kwargs = dict(allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
if "*" in settings.cors_origin_list:
    _cors_kwargs.update(allow_origin_regex=".*")
else:
    _cors_kwargs.update(
        allow_origins=settings.cors_origin_list,
        allow_origin_regex=settings.cors_origin_regex if settings.cors_origin_regex else None,
    )
app.add_middleware(CORSMiddleware, **_cors_kwargs)
# Same rules, for the last-resort handler below (which runs outside CORS).
_cors_rules = CORSMiddleware(app=None, **_cors_kwargs)


_log = logging.getLogger("crm.startup")


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last resort, for an exception raised outside UnhandledErrorMiddleware (e.g. in
    CORS itself). Runs outside CORS, so it adds CORS headers itself -- only for an
    origin CORS would allow (it used to echo back any origin)."""
    log_unhandled(request, exc)
    origin = request.headers.get("origin")
    headers = {}
    if origin and _cors_rules.is_allowed_origin(origin):
        headers = {"access-control-allow-origin": origin, "access-control-allow-credentials": "true",
                   "vary": "Origin"}
    return server_error_response(headers)


# Shown by /health and the startup log, so a deploy can be confirmed. The BUILD_TAG
# environment variable (set per release, e.g. to the git commit) wins; otherwise this
# label -- bump it in every release.
BUILD_TAG = settings.build_tag.strip() or "2026-11-04-deliveries-n-plus-one"


# What the startup work is doing: pending -> running -> done. Read by tests and logs only;
# /health never waits for it.
STARTUP_STATE: dict[str, object] = {"state": "pending", "seconds": None}


@app.on_event("startup")
def on_startup() -> None:
    """Returns at once (by default) so uvicorn opens the port and /health answers while
    the startup work below runs in a background thread. Nothing in it is needed to
    serve a request: the schema is migrated by `python -m app.core.ensure_migrated`
    before uvicorn starts, and the rest are logs, idempotent backfills and the
    reminder sweep. STARTUP_TASKS_IN_BACKGROUND=false runs it inline, as before."""
    _log.info("Starting CRM API — build: %s", BUILD_TAG)
    if settings.startup_tasks_in_background:
        import threading

        threading.Thread(target=run_startup_tasks, name="startup-tasks", daemon=True).start()
    else:
        run_startup_tasks()


def run_startup_tasks() -> None:
    STARTUP_STATE["state"] = "running"
    begun = time.perf_counter()
    try:
        email_service.log_startup_config()
    except Exception:  # noqa: BLE001
        _log.exception("Email config summary failed")
    try:
        from app.core import firebase

        firebase.log_startup_status()
    except Exception:  # noqa: BLE001
        _log.exception("Push config summary failed")

    # Every startup step is isolated, including creating the tables: a database that
    # is unreachable at boot must not kill the process. An exception raised here fails
    # the ASGI lifespan, uvicorn exits, the platform restarts it, and the service sits
    # in a crash loop answering nothing at all -- so an outage in the database becomes
    # a total outage with no way to see why. Booting anyway keeps /health answering,
    # and it says which half is broken.
    def _create_tables() -> None:
        # For local dev we auto-create tables. In production, use Alembic migrations.
        Base.metadata.create_all(bind=engine)

    def _convert_inline_uploads_if_enabled() -> None:
        # Off by default (CONVERT_INLINE_UPLOADS_ON_STARTUP=false): this is a full
        # table scan across 8 models on every boot, which is unnecessary once a
        # firm's inline uploads have already been converted once. Run it
        # explicitly instead: `python -m app.scripts.convert_inline_uploads`.
        if settings.convert_inline_uploads_on_startup:
            convert_inline_uploads()

    try:
        from app.core import perf

        perf.log_round_trip(engine)
    except Exception:  # noqa: BLE001
        _log.exception("Database round-trip check failed")

    steps = (
        ("create_all", _create_tables),
        ("extend_pg_enum_types", extend_pg_enum_types),
        ("drop_legacy_columns", drop_legacy_columns),
        ("relax_not_null_columns", relax_not_null_columns),
        ("widen_columns_to_text", widen_columns_to_text),
        ("add_columns_with_default", add_columns_with_default),
        ("auto_add_missing_columns", auto_add_missing_columns),
        # Constraint-level migrations run last, after every table/column exists,
        # and are self-auditing: each skips (with a logged warning) instead of
        # failing startup if it finds data that isn't safe to constrain yet.
        ("enforce_unique_email", enforce_unique_email),
        ("enforce_unique_google_id", enforce_unique_google_id),
        ("backfill_missing_numbers", backfill_missing_numbers),
        ("migrate_order_statuses", migrate_order_statuses),
        ("convert_inline_uploads", _convert_inline_uploads_if_enabled),
        ("backfill_product_pricing", backfill_product_pricing),
    )
    if not settings.run_startup_migrations:
        # Schema is kept current by `alembic upgrade head` in the deploy instead.
        _log.info("Startup schema steps skipped (RUN_STARTUP_MIGRATIONS=false)")
        steps = ()
    started = time.perf_counter()
    for label, step in steps:
        try:
            step()
        except Exception:  # noqa: BLE001
            _log.exception("Startup migration step failed: %s", label)
    if steps:
        _log.info("Startup schema steps took %.0f ms", (time.perf_counter() - started) * 1000)

    if settings.seed_on_startup:
        try:
            from app.seed import main as seed_main

            seed_main()
        except Exception:  # noqa: BLE001
            _log.exception("Startup seed failed")

    try:
        from app.services import reminder_service

        reminder_service.start_background_sweeps()
    except Exception:  # noqa: BLE001
        _log.exception("Follow-up reminder sweep could not start")

    STARTUP_STATE.update(state="done", seconds=round(time.perf_counter() - begun, 3))
    _log.info("CRM API startup tasks complete in %.1fs — build: %s", STARTUP_STATE["seconds"], BUILD_TAG)


def _database_target() -> str:
    """Which server and database this instance is configured to talk to.

    Host, port and database name only -- never the user or the password, so this is
    safe to return from an unauthenticated endpoint. Without it, a dead database looks
    like "some host does not resolve"; with it, the host can be compared against the
    one in the platform dashboard.
    """
    try:
        url = engine.url
        if url.get_backend_name() == "sqlite":
            return f"sqlite:{url.database}"
        return f"{url.host or '?'}:{url.port or 5432}/{url.database or '?'}"
    except Exception:  # noqa: BLE001
        return "unknown"


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
        body["database_target"] = _database_target()
        body["database_error"] = detail
    return body


app.include_router(files.router)
app.include_router(auth.router)
app.include_router(billing.router)
app.include_router(users.router)
app.include_router(roles.router)
app.include_router(teams.router)
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
app.include_router(grns.router)
app.include_router(supplier_invoices.router)
app.include_router(accounts_payable.router)
app.include_router(supplier_payments.router)
app.include_router(invoices.router)
app.include_router(invoices.orders_router)  # POST /orders/{id}/invoice
app.include_router(vehicle_stock.router)
app.include_router(vehicles.router)
app.include_router(deliveries.router)
app.include_router(deliveries.customer_payments_router)
app.include_router(expenses.router)
app.include_router(attendance.router)
app.include_router(reports.router)
app.include_router(dashboard.router)
app.include_router(notifications.router)
app.include_router(plans.router)
app.include_router(organizations.router)
app.include_router(settings_router.router)
app.include_router(superadmin.router)
app.include_router(platform.router)
app.include_router(support.org_router)
app.include_router(support.platform_router)
app.include_router(leads.router)
app.include_router(quotations.router)
app.include_router(payment_receipts.router)
app.include_router(sales_returns.router)
app.include_router(purchase_returns.router)
app.include_router(visits.router)
app.include_router(follow_ups.router)
app.include_router(leaves.router)
app.include_router(transfers.router)
app.include_router(companies.router)
app.include_router(websocket.router)
app.include_router(payment_webhooks.router)
app.include_router(cash_reconciliation.router)
app.include_router(delivery_day_end.router)





