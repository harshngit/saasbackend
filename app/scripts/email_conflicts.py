"""Read-only report of login-email conflicts: accounts in `users` that share an email
once it is trimmed and lowercased — what blocks ux_users_email_ci (alembic revision
b7c8d9e0f1a2) and makes those addresses unable to log in.

    python -m app.scripts.email_conflicts                      # the app's DATABASE_URL
    python -m app.scripts.email_conflicts --database-url "$URL"

Never prints an email address or a connection string: each conflict is shown as a
masked address (s***@gmail.com), the number of accounts, what kinds of conflict it is,
and 8-character id prefixes with each account's scope. Read-only: on PostgreSQL the
transaction is SET READ ONLY; nothing is written anywhere.

Resolving is an authorized owner's decision, account by account (change one address,
or remove a stray/test account) — this script and the migration never merge, rename,
deactivate or delete anything.

Kinds of conflict reported per group:
    same_firm          two accounts in one firm
    cross_firm         accounts in different firms
    firm_and_platform  a firm account and a platform account (Super Admin / Sales Manager)
    platform           two platform accounts
    case_or_spaces     the stored spellings differ only by case or surrounding spaces
                       (otherwise they are exact duplicates)
Plus a count of rows whose email is not stored normalized (no conflict needed), and
whether the ux_users_email_ci index already exists.

Exit codes: 0 no conflicts, 1 conflicts found, 2 could not connect.
"""
import argparse
import sys
from collections import defaultdict

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

KINDS = ("same_firm", "cross_firm", "firm_and_platform", "platform", "case_or_spaces")


def find_conflicts(conn: Connection) -> dict:
    """{"groups": [...], "counts": {kind: n}, "accounts": n, "unnormalized_rows": n}.
    Each group: {"masked", "accounts", "kinds", "members": [(id_prefix, scope)]} — no
    full address or full id leaves this function."""
    from app.services.email_service import mask_email

    rows = conn.execute(text(
        "SELECT id, organization_id, system_role, email FROM users WHERE LOWER(TRIM(email)) IN ("
        "SELECT LOWER(TRIM(email)) FROM users GROUP BY LOWER(TRIM(email)) HAVING COUNT(*) > 1)"
    )).fetchall()
    by_email: dict[str, list] = defaultdict(list)
    for row in rows:
        by_email[row.email.strip().lower()].append(row)

    groups, counts = [], dict.fromkeys(KINDS, 0)
    for normalized, members in by_email.items():
        firms = [m.organization_id for m in members if m.organization_id]
        platform = [m for m in members if not m.organization_id]
        kinds = []
        if len(firms) != len(set(firms)):
            kinds.append("same_firm")
        if len(set(firms)) > 1:
            kinds.append("cross_firm")
        if firms and platform:
            kinds.append("firm_and_platform")
        if len(platform) > 1:
            kinds.append("platform")
        if len({m.email for m in members}) > 1:
            kinds.append("case_or_spaces")
        for kind in kinds:
            counts[kind] += 1
        groups.append({
            "masked": mask_email(normalized),
            "accounts": len(members),
            "kinds": kinds,
            "members": [
                (str(m.id)[:8], f"firm {str(m.organization_id)[:8]}" if m.organization_id
                 else f"platform {m.system_role or '?'}")
                for m in members
            ],
        })
    unnormalized = conn.execute(
        text("SELECT COUNT(*) FROM users WHERE email <> LOWER(TRIM(email))")
    ).scalar()
    groups.sort(key=lambda g: (-g["accounts"], g["masked"]))
    return {"groups": groups, "counts": counts, "accounts": len(rows), "unnormalized_rows": unnormalized}


def index_present(conn: Connection) -> bool:
    """Whether ux_users_email_ci exists in the current schema (read-only catalog query)."""
    if conn.dialect.name == "postgresql":
        query = ("SELECT COUNT(*) FROM pg_indexes WHERE schemaname = current_schema() "
                 "AND indexname = 'ux_users_email_ci'")
    else:
        query = "SELECT COUNT(*) FROM sqlite_master WHERE type = 'index' AND name = 'ux_users_email_ci'"
    return bool(conn.execute(text(query)).scalar())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database-url", help="defaults to the app's DATABASE_URL (never printed)")
    args = parser.parse_args()
    if args.database_url:
        engine = create_engine(args.database_url)
    else:
        from app.core.database import engine
    try:
        with engine.connect() as conn:
            if conn.dialect.name == "postgresql":
                conn.execute(text("SET TRANSACTION READ ONLY"))
            report = find_conflicts(conn)
            has_index = index_present(conn)
            conn.rollback()
    except Exception as exc:  # noqa: BLE001
        print(f"Could not read the database: {type(exc).__name__}", file=sys.stderr)
        return 2

    groups = report["groups"]
    print(f"ux_users_email_ci present: {'yes' if has_index else 'no'}")
    print(f"Conflicting emails: {len(groups)} covering {report['accounts']} accounts")
    for kind in KINDS:
        print(f"  {kind:<18} {report['counts'][kind]}")
    print(f"Rows not stored trimmed+lowercased (conflict or not): {report['unnormalized_rows']}")
    for group in groups:
        members = ", ".join(f"{prefix} ({scope})" for prefix, scope in group["members"])
        print(f"- {group['masked']}: {group['accounts']} accounts [{', '.join(group['kinds'])}] {members}")
    if groups:
        print("Resolve each with the account owners, then run `alembic upgrade head`.")
    return 1 if groups else 0


if __name__ == "__main__":
    sys.exit(main())
