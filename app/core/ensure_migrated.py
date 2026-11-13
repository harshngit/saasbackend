"""Run `alembic upgrade head` only when the database is not already at head.

    python -m app.core.ensure_migrated && exec uvicorn app.main:app --host 0.0.0.0 --port 8000

Why: `alembic upgrade head` always loads alembic/env.py, which imports every app model
(and the app's engine) before it can find out there is nothing to do. On a container
cold start that costs seconds before uvicorn can even begin. This module instead:

1. reads the head revision(s) by PARSING alembic/versions/*.py (ast, nothing imported);
2. reads `alembic_version` with one small query (its own short-lived engine; the app's
   models, engine and env.py are not loaded);
3. if they are equal: exits 0 immediately;
4. otherwise takes a PostgreSQL advisory lock (so two containers never migrate at the
   same time), re-reads the version (another container may have just finished), and
   runs exactly what the old CMD ran -- alembic's `upgrade head` through alembic.ini and
   env.py -- so a needed migration behaves identically. The lock is held on a separate
   connection for the whole upgrade and released afterwards.

Exit codes: 0 = at head (already, or after upgrading); 1 = failed (message on stderr).
"""

from __future__ import annotations

import ast
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = ROOT / "alembic.ini"
VERSIONS_DIR = ROOT / "alembic" / "versions"
# Arbitrary fixed 64-bit key, shared by every container of this app.
ADVISORY_LOCK_KEY = 0x43524D_4D494752  # "CRM" "MIGR"

log = logging.getLogger("crm.ensure_migrated")


class MigrationError(Exception):
    pass


def _literal(node: ast.AST):  # noqa: ANN202
    try:
        return ast.literal_eval(node)
    except ValueError:
        return None


def script_heads(versions_dir: Path = VERSIONS_DIR) -> set[str]:
    """Head revision ids, read from the migration files without importing them."""
    revisions: set[str] = set()
    parents: set[str] = set()
    for path in sorted(versions_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found: dict[str, object] = {}
        for node in tree.body:
            targets = []
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            for target in targets:
                if isinstance(target, ast.Name) and target.id in ("revision", "down_revision"):
                    found[target.id] = _literal(value)
        revision = found.get("revision")
        if not isinstance(revision, str):
            continue  # not a migration module
        revisions.add(revision)
        down = found.get("down_revision")
        if isinstance(down, str):
            parents.add(down)
        elif isinstance(down, (tuple, list)):
            parents.update(d for d in down if isinstance(d, str))
    heads = revisions - parents
    if not heads:
        raise MigrationError(f"no alembic revisions found in {versions_dir}")
    return heads


def _database_url() -> str:
    from app.core.config import settings  # settings only: no models, no app engine

    return settings.sqlalchemy_database_url


def database_versions(connection) -> set[str]:  # noqa: ANN001
    """The revision(s) stamped in alembic_version; empty when the table does not exist."""
    from sqlalchemy import inspect, text

    if not inspect(connection).has_table("alembic_version"):
        return set()
    return {row[0] for row in connection.execute(text("SELECT version_num FROM alembic_version"))}


def run_alembic_upgrade() -> None:
    """Exactly what `alembic upgrade head` does (alembic.ini + env.py)."""
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config(str(ALEMBIC_INI)), "head")


def ensure_migrated(url: str | None = None, upgrade=run_alembic_upgrade) -> str:  # noqa: ANN001
    """Returns "at_head" or "upgraded"; raises MigrationError on failure."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import NullPool

    heads = script_heads()
    engine = create_engine(url or _database_url(), poolclass=NullPool)
    try:
        with engine.connect() as conn:
            current = database_versions(conn)
            conn.rollback()
        if current == heads:
            log.info("Database already at head %s; nothing to migrate", ",".join(sorted(heads)))
            return "at_head"

        is_pg = engine.dialect.name == "postgresql"
        with engine.connect() as lock_conn:
            if is_pg:
                log.info("Database at %s, head is %s: waiting for the migration lock",
                         ",".join(sorted(current)) or "<empty>", ",".join(sorted(heads)))
                lock_conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY})
                lock_conn.commit()  # session-level lock survives; do not sit idle in a transaction
            try:
                with engine.connect() as conn:
                    current = database_versions(conn)
                    conn.rollback()
                if current == heads:
                    log.info("Another instance migrated the database to head meanwhile")
                    return "at_head"
                started = time.perf_counter()
                upgrade()
                with engine.connect() as conn:
                    after = database_versions(conn)
                    conn.rollback()
                if after != heads:
                    raise MigrationError(f"after upgrade the database is at {sorted(after)}, expected {sorted(heads)}")
                log.info("Migrated to head in %.1fs", time.perf_counter() - started)
                return "upgraded"
            finally:
                if is_pg:
                    lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY})
                    lock_conn.commit()
    except MigrationError:
        raise
    except Exception as exc:  # noqa: BLE001 -- any failure is reported the same way
        raise MigrationError(f"{type(exc).__name__}: {exc}") from exc
    finally:
        engine.dispose()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
    started = time.perf_counter()
    try:
        outcome = ensure_migrated()
    except MigrationError as exc:
        print(f"ensure_migrated: database migration FAILED, not starting the app: {exc}", file=sys.stderr)
        return 1
    log.info("ensure_migrated: %s (%.2fs)", outcome, time.perf_counter() - started)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
