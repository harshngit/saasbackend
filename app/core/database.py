import json
import logging
from collections.abc import Generator
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    LargeBinary,
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    Numeric,
    String,
    Text,
    create_engine,
    event,
    inspect,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

logger = logging.getLogger("crm.db")

DATABASE_URL = settings.sqlalchemy_database_url

# SQLite needs check_same_thread=False for use across FastAPI's threadpool.
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


if DATABASE_URL.startswith("sqlite"):
    # SQLite ignores foreign keys unless told to enforce them. Turn it on so
    # dev matches Postgres (ON DELETE SET NULL / CASCADE actually fire).
    @event.listens_for(engine, "connect")
    def _enable_sqlite_fks(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a request-scoped DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _sql_literal(value: object) -> str | None:
    """Render a Python default as a SQL literal for a backfill DEFAULT clause."""
    if isinstance(value, bool):
        # SQLite only learned the TRUE/FALSE keywords in 3.23 — 1/0 always works.
        return ("TRUE" if value else "FALSE") if engine.dialect.name == "postgresql" else ("1" if value else "0")
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    if isinstance(value, (list, dict)):  # JSON columns
        return "'" + json.dumps(value).replace("'", "''") + "'"
    if isinstance(value, (datetime, date)):
        return "CURRENT_TIMESTAMP"
    return None


def _backfill_default(column) -> str | None:  # noqa: ANN001
    """The DEFAULT to give a NOT NULL column being added to a populated table.

    Uses the model's own default where there is one (so existing rows get the
    same value a new row would), otherwise falls back to the type's zero value.
    Returns None when nothing sensible can be derived — those still need a real
    migration.
    """
    default = column.default
    value = None
    if default is not None:
        if getattr(default, "is_scalar", False):
            value = default.arg
        elif getattr(default, "is_callable", False):
            try:
                value = default.arg(None)  # SQLAlchemy wraps callables to take a context
            except Exception:  # noqa: BLE001
                value = None
    literal = _sql_literal(value) if value is not None else None
    if literal is not None:
        return literal

    if isinstance(column.type, LargeBinary):
        return None
    if isinstance(column.type, Boolean):
        return _sql_literal(False)
    if isinstance(column.type, (Integer, Float, Numeric)):
        return "0"
    if isinstance(column.type, (DateTime, Date)):
        return "CURRENT_TIMESTAMP"
    if isinstance(column.type, JSON):
        return "'{}'"
    if isinstance(column.type, (String, Text)):
        return "''"
    # LargeBinary and anything else exotic: SQLite refuses a non-constant default
    # for these, so leave them to a real migration rather than guessing.
    return None


def auto_add_missing_columns() -> None:
    """Lightweight forward-only migration: add any column the model has but the
    live table lacks, so a model that gained fields doesn't break a live DB.

    NOT NULL columns are added WITH a backfill DEFAULT derived from the model —
    without that they were silently skipped, and every later SELECT of that table
    failed with "column does not exist" (which is exactly how /products broke).

    This is a stopgap for early development (works on SQLite + Postgres). Once the
    schema stabilises, switch to Alembic migrations for anything non-trivial
    (type changes, renames, data backfills).
    """
    inspector = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            col_type = column.type.compile(dialect=engine.dialect)
            ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'
            if not column.nullable:
                backfill = _backfill_default(column)
                if backfill is None:
                    logger.warning(
                        "Skipping auto-add of NOT NULL column %s.%s — no derivable "
                        "default, needs a real migration",
                        table.name,
                        column.name,
                    )
                    continue
                ddl += f" NOT NULL DEFAULT {backfill}"
            # Per-column isolation: one failure must not skip every later column.
            try:
                with engine.begin() as conn:
                    conn.execute(text(ddl))
            except Exception:  # noqa: BLE001
                logger.exception("Could not add column %s.%s", table.name, column.name)
                continue
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
    # An anonymous walk-in payment belongs to no customer, so the column that used to
    # be mandatory no longer is. Making a column nullable in the model is invisible to
    # an existing Postgres database — the constraint has to be dropped here too.
    ("customer_payments", "customer_id"),
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


# Columns whose type must be widened on an existing DB (e.g. VARCHAR -> TEXT).
_WIDEN_TO_TEXT = [
    ("organizations", "logo_url"),  # now holds base64 data: URLs, too big for VARCHAR(500)
    ("users", "identify_proofs"),   # same: holds an uploaded ID document as a data: URL
]


def widen_columns_to_text() -> None:
    """Widen columns to TEXT on an existing Postgres DB (SQLite is typeless already)."""
    if engine.dialect.name != "postgresql":
        return
    for table, column in _WIDEN_TO_TEXT:
        try:
            with engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE TEXT'))
            logger.info("Auto-migrated: widened %s.%s to TEXT", table, column)
        except Exception:  # noqa: BLE001
            logger.exception("Could not widen %s.%s", table, column)


# NOT NULL columns added to already-existing tables — added WITH a default so
# existing rows backfill (auto_add_missing_columns skips NOT NULL columns).
_ADD_WITH_DEFAULT = [
    ("customers", "opening_balance", "0"),
    ("customers", "total_billed", "0"),
    ("customers", "total_received", "0"),
]


def add_columns_with_default() -> None:
    """Add NOT NULL numeric columns to existing tables, backfilling rows with a default."""
    inspector = inspect(engine)
    coltype = "DOUBLE PRECISION" if engine.dialect.name == "postgresql" else "FLOAT"
    for table, column, default in _ADD_WITH_DEFAULT:
        if not inspector.has_table(table):
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        if column in existing:
            continue
        try:
            with engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {coltype} NOT NULL DEFAULT {default}'))
            logger.info("Auto-migrated: added %s.%s (NOT NULL default %s)", table, column, default)
        except Exception:  # noqa: BLE001
            logger.exception("Could not add column %s.%s", table, column)


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
