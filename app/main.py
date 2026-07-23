from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.database import Base, auto_add_missing_columns, engine, extend_pg_enum_types
from app.models import Organization, RefreshToken, User  # noqa: F401  (register mappers)
from app.routers import auth, organizations, superadmin, users

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


@app.on_event("startup")
def on_startup() -> None:
    # For local dev we auto-create tables. In production, use Alembic migrations instead.
    Base.metadata.create_all(bind=engine)
    # Add new enum values to existing Postgres ENUM types (e.g. 'locked', 'basic').
    extend_pg_enum_types()
    # Add any newly-introduced nullable columns to already-existing tables.
    auto_add_missing_columns()

    # On hosts without a shell/pre-deploy step, seed the Super Admin on boot.
    if settings.seed_on_startup:
        from app.seed import main as seed_main

        seed_main()


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(auth.router)
app.include_router(users.router)
app.include_router(organizations.router)
app.include_router(superadmin.router)
