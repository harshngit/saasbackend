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
