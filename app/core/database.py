import logging
from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

logger = logging.getLogger("crm.db")

DATABASE_URL = settings.sqlalchemy_database_url

# SQLite needs check_same_thread=False for use across FastAPI's threadpool.
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a request-scoped DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def auto_add_missing_columns() -> None:
    """Lightweight forward-only migration: add any missing *nullable* columns to
    existing tables so a model that gained fields doesn't break a live DB.

    This is a stopgap for early development (works on SQLite + Postgres). Once the
    schema stabilises, switch to Alembic migrations for anything non-trivial
    (NOT NULL columns, type changes, renames, data backfills).
    """
    inspector = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            if not column.nullable:
                # Can't safely add a NOT NULL column to a populated table here.
                logger.warning(
                    "Skipping auto-add of NOT NULL column %s.%s — needs a real migration",
                    table.name,
                    column.name,
                )
                continue
            col_type = column.type.compile(dialect=engine.dialect)
            ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'
            with engine.begin() as conn:
                conn.execute(text(ddl))
            logger.info("Auto-migrated: added column %s.%s", table.name, column.name)


# Enum columns (table, column) whose Postgres ENUM type may need new labels added.
_ENUM_COLUMNS = [
    ("organizations", "status"),
    ("organizations", "plan"),
    ("users", "role"),
]


def extend_pg_enum_types() -> None:
    """Postgres-only: add any missing labels to the native ENUM types backing our
    enum columns (e.g. a new 'LOCKED' status or 'BASIC' plan).

    SQLAlchemy stores enum *names* (uppercase). We look up the real type name from
    the catalog (so we don't hard-code it) and `ALTER TYPE ... ADD VALUE` for any
    label the Python enum has but the DB type lacks. Fresh DBs already have them,
    so this is a no-op there. On SQLite (dev) enums are plain text — nothing to do.
    """
    if engine.dialect.name != "postgresql":
        return

    from app.models.enums import OrganizationStatus, UserRole

    wanted = {
        ("organizations", "status"): [e.name for e in OrganizationStatus],
        ("users", "role"): [e.name for e in UserRole],
    }

    # ADD VALUE cannot run inside a transaction block — use autocommit.
    with engine.connect() as conn:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
        for (table, column), labels in wanted.items():
            type_name = conn.execute(
                text(
                    "SELECT t.typname FROM pg_attribute a "
                    "JOIN pg_class c ON c.oid = a.attrelid "
                    "JOIN pg_type t ON t.oid = a.atttypid "
                    "WHERE c.relname = :table AND a.attname = :column"
                ),
                {"table": table, "column": column},
            ).scalar()
            if not type_name:
                continue
            existing = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT e.enumlabel FROM pg_enum e "
                        "JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname = :t"
                    ),
                    {"t": type_name},
                )
            }
            for label in labels:
                if label not in existing:
                    conn.execute(text(f'ALTER TYPE "{type_name}" ADD VALUE IF NOT EXISTS \'{label}\''))
                    logger.info("Auto-migrated enum %s: added value %s", type_name, label)


# Columns removed from the model that must be dropped from an existing DB.
# (Postgres only; on SQLite the dev file is simply recreated.)
_DROPPED_COLUMNS = [
    ("organizations", "plan"),           # replaced by plan_id (FK to plans)
    ("organizations", "requested_plan"),  # replaced by requested_plan_id (FK to plans)
]


# Columns whose NOT NULL constraint must be relaxed (model made them nullable).
_RELAX_NOT_NULL = [
    ("users", "role"),  # legacy role enum is now optional (staff use role_id)
]


def relax_not_null_columns() -> None:
    """Drop NOT NULL on columns the model no longer requires (Postgres only)."""
    if engine.dialect.name != "postgresql":
        return
    for table, column in _RELAX_NOT_NULL:
        try:
            with engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" DROP NOT NULL'))
            logger.info("Auto-migrated: dropped NOT NULL on %s.%s", table, column)
        except Exception:  # noqa: BLE001
            logger.exception("Could not drop NOT NULL on %s.%s", table, column)


def drop_legacy_columns() -> None:
    """Drop columns that the model no longer defines (Postgres only).

    Needed when a NOT NULL enum column like `plan` is replaced by a FK — otherwise
    new ORM inserts (which omit it) would violate the old NOT NULL constraint.
    """
    if engine.dialect.name != "postgresql":
        return
    inspector = inspect(engine)
    for table, column in _DROPPED_COLUMNS:
        if not inspector.has_table(table):
            continue
        existing = {col["name"] for col in inspector.get_columns(table)}
        if column in existing:
            with engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE "{table}" DROP COLUMN IF EXISTS "{column}"'))
            logger.info("Auto-migrated: dropped legacy column %s.%s", table, column)
