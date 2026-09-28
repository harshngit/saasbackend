"""Read-only schema/row-count parity check between two databases — built for the
PlanetScale migration rehearsal ("does the restored target actually match the
source?"), but works against any two SQLAlchemy-supported URLs.

    SOURCE_DATABASE_URL=... TARGET_DATABASE_URL=... python -m app.scripts.compare_postgres_databases
    python -m app.scripts.compare_postgres_databases --source "$SRC" --target "$TGT"

Connection strings are read from --source/--target or the SOURCE_DATABASE_URL /
TARGET_DATABASE_URL environment variables and are never printed, logged, or
included in any output this script produces — only the comparison results are.

Read-only: every operation is either a SQLAlchemy inspector call (schema
introspection) or a plain SELECT (row counts, alembic_version, pg_extension).
Nothing is created, altered, or deleted on either database.

For each table registered on the application's Base.metadata (i.e. every table
the app actually owns — not every table that happens to exist in the database),
compares: existence, row count, columns (name/type/nullable), primary key,
foreign keys, unique constraints, and indexes. Also compares `alembic_version`
and, on PostgreSQL targets, `pg_extension`.

Exit codes:
    0  full parity — every table matched, alembic_version matched
    1  at least one mismatch was found (see printed detail — never swallowed)
    2  could not connect to one or both databases, or no URLs were given
"""
import argparse
import os
import sys

from sqlalchemy import create_engine, inspect, text


def _resolve_url(cli_value: str | None, env_name: str) -> str | None:
    return cli_value or os.environ.get(env_name)


def _app_table_names() -> list[str]:
    from app.core.database import Base
    import app.models  # noqa: F401 — registers every model on Base.metadata

    return sorted(t.name for t in Base.metadata.sorted_tables)


def _row_count(engine, table: str) -> int:
    with engine.connect() as conn:
        return conn.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar()


def _columns(inspector, table: str) -> dict[str, tuple[str, bool]]:
    return {c["name"]: (str(c["type"]), bool(c["nullable"])) for c in inspector.get_columns(table)}


def _primary_key(inspector, table: str) -> tuple[str, ...]:
    return tuple(sorted(inspector.get_pk_constraint(table).get("constrained_columns") or []))


def _foreign_keys(inspector, table: str) -> list[tuple[str | None, str]]:
    return sorted(
        (
            fk["constrained_columns"][0] if fk["constrained_columns"] else None,
            fk["referred_table"],
        )
        for fk in inspector.get_foreign_keys(table)
    )


def _unique_constraints(inspector, table: str) -> list[tuple[str, ...]]:
    return sorted(tuple(sorted(u["column_names"])) for u in inspector.get_unique_constraints(table))


def _indexes(inspector, table: str) -> list[tuple[str, ...]]:
    try:
        return sorted(tuple(sorted(i["column_names"])) for i in inspector.get_indexes(table))
    except Exception:  # noqa: BLE001 — index introspection is best-effort, never fatal
        return []


def _alembic_version(engine) -> str | None:
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT version_num FROM alembic_version")).fetchone()
            return row[0] if row else None
    except Exception:  # noqa: BLE001 — table may not exist yet; that's a reportable state, not a crash
        return None


def _extensions(engine) -> list[tuple[str, str]]:
    if engine.dialect.name != "postgresql":
        return []
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT extname, extversion FROM pg_extension ORDER BY extname")).fetchall()
            return [(r[0], r[1]) for r in rows]
    except Exception:  # noqa: BLE001
        return []


def compare(source_url: str, target_url: str) -> int:
    source_engine = create_engine(source_url)
    target_engine = create_engine(target_url)

    try:
        source_engine.connect().close()
    except Exception as exc:  # noqa: BLE001
        print(f"Could not connect to SOURCE database: {type(exc).__name__}")
        return 2
    try:
        target_engine.connect().close()
    except Exception as exc:  # noqa: BLE001
        print(f"Could not connect to TARGET database: {type(exc).__name__}")
        return 2

    source_inspector = inspect(source_engine)
    target_inspector = inspect(target_engine)
    source_tables = set(source_inspector.get_table_names())
    target_tables = set(target_inspector.get_table_names())

    any_failure = False

    print(f"{'table':<34}{'source':>10}{'target':>10}{'diff':>8}   status")
    print("-" * 74)

    for table in _app_table_names():
        if table not in source_tables:
            print(f"{table:<34}{'MISSING':>10}{'':>10}{'':>8}   FAIL (missing on source)")
            any_failure = True
            continue
        if table not in target_tables:
            print(f"{table:<34}{'':>10}{'MISSING':>10}{'':>8}   FAIL (missing on target)")
            any_failure = True
            continue

        source_count = _row_count(source_engine, table)
        target_count = _row_count(target_engine, table)
        diff = target_count - source_count
        status = "PASS" if diff == 0 else "FAIL"
        any_failure = any_failure or status == "FAIL"
        print(f"{table:<34}{source_count:>10}{target_count:>10}{diff:>8}   {status}")

        reasons = []
        source_cols = _columns(source_inspector, table)
        target_cols = _columns(target_inspector, table)
        if source_cols != target_cols:
            missing_in_target = sorted(set(source_cols) - set(target_cols))
            missing_in_source = sorted(set(target_cols) - set(source_cols))
            changed = sorted(k for k in set(source_cols) & set(target_cols) if source_cols[k] != target_cols[k])
            if missing_in_target:
                reasons.append(f"columns missing on target: {missing_in_target}")
            if missing_in_source:
                reasons.append(f"columns missing on source: {missing_in_source}")
            if changed:
                reasons.append(f"columns with type/nullable mismatch: {changed}")

        if _primary_key(source_inspector, table) != _primary_key(target_inspector, table):
            reasons.append("primary key mismatch")
        if _foreign_keys(source_inspector, table) != _foreign_keys(target_inspector, table):
            reasons.append("foreign key mismatch")
        if _unique_constraints(source_inspector, table) != _unique_constraints(target_inspector, table):
            reasons.append("unique constraint mismatch")
        if _indexes(source_inspector, table) != _indexes(target_inspector, table):
            reasons.append("index mismatch")

        if reasons:
            any_failure = True
            print(f"    SCHEMA MISMATCH on {table}: {'; '.join(reasons)}")

    print("-" * 74)

    source_alembic = _alembic_version(source_engine)
    target_alembic = _alembic_version(target_engine)
    alembic_status = "PASS" if source_alembic == target_alembic else "FAIL"
    any_failure = any_failure or alembic_status == "FAIL"
    print(f"alembic_version: source={source_alembic!r} target={target_alembic!r}  {alembic_status}")

    source_ext = _extensions(source_engine)
    target_ext = _extensions(target_engine)
    if source_ext or target_ext:
        ext_status = "MATCH" if source_ext == target_ext else "MISMATCH"
        any_failure = any_failure or ext_status == "MISMATCH"
        print(f"pg_extension: source={source_ext} target={target_ext}  {ext_status}")

    source_engine.dispose()
    target_engine.dispose()

    print()
    print("PARITY: FAIL — see mismatches above" if any_failure else "PARITY: PASS — every application table matched")
    return 1 if any_failure else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", help="Source DB URL. Never printed. Defaults to $SOURCE_DATABASE_URL.")
    parser.add_argument("--target", help="Target DB URL. Never printed. Defaults to $TARGET_DATABASE_URL.")
    args = parser.parse_args(argv)

    source_url = _resolve_url(args.source, "SOURCE_DATABASE_URL")
    target_url = _resolve_url(args.target, "TARGET_DATABASE_URL")

    if not source_url or not target_url:
        print(
            "Both a source and target database URL are required — pass --source/--target "
            "or set SOURCE_DATABASE_URL / TARGET_DATABASE_URL. Values are never printed by this tool."
        )
        return 2

    return compare(source_url, target_url)


if __name__ == "__main__":
    sys.exit(main())
