from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.database import (
    Base,
    auto_add_missing_columns,
    drop_legacy_columns,
    engine,
    extend_pg_enum_types,
    relax_not_null_columns,
    widen_columns_to_text,
)
from app.models import Organization, Plan, RefreshToken, Role, User  # noqa: F401  (register mappers)
from app.routers import auth, organizations, plans, roles, superadmin, users

app = FastAPI(
    title="CRM SaaS API",
    description="Backend for the CRM / Billing / Inventory SaaS. Auth & user management.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


import logging

_log = logging.getLogger("crm.startup")

# Bump when the deployed feature set changes, so /health and logs confirm the build.
BUILD_TAG = "company-settings+username"


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
        ("auto_add_missing_columns", auto_add_missing_columns),
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


app.include_router(auth.router)
app.include_router(users.router)
app.include_router(roles.router)
app.include_router(plans.router)
app.include_router(organizations.router)
app.include_router(superadmin.router)
