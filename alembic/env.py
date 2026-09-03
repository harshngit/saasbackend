"""Alembic migration environment.

Deliberately reuses the running application's own database configuration and
model metadata rather than duplicating either:

- The connection URL comes from app.core.config.settings.sqlalchemy_database_url
  — the exact same normalized URL (postgres:// -> postgresql+psycopg://, etc.)
  the FastAPI app itself connects with. There is no second place a database
  URL is configured.
- target_metadata comes from app.core.database.Base.metadata, populated by
  importing app.models (which imports every model module) — the same
  metadata Base.metadata.create_all() already uses at application startup.
  Autogenerate therefore sees the real, live set of models, not a stale copy.
- In online mode this reuses app.core.database.engine directly (the same
  engine instance/pool config the app process itself uses — same
  pool_pre_ping, same SQLite check_same_thread handling) instead of building
  a second engine from alembic.ini.
"""

from logging.config import fileConfig

from alembic import context

from app.core.config import settings
from app.core.database import Base, engine
import app.models  # noqa: F401  -- imports every model module, registering it on Base.metadata

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The single source of truth for both the URL and the schema — see the
# module docstring above.
config.set_main_option("sqlalchemy.url", settings.sqlalchemy_database_url)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode, against the application's own engine."""
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Detect column type changes too, not just add/drop — useful given
            # this project's history of type-widening migrations
            # (relax_not_null_columns / widen_columns_to_text in
            # app/core/database.py) landing as hand-written ALTERs before
            # Alembic existed.
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
