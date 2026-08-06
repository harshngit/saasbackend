"""Fetch a record by whichever id the caller has.

Every record carries two identifiers: the UUID primary key that other tables
foreign-key to, and the human-facing code the sheet calls an Auto Number
(CUST-2026-1005, PROD-2026-1008, INV-2026-1001 …). The UUID is what the app
passes around internally, but the code is what a user reads off a document and
types into a search box — so both have to work in the URL.
"""

import re
from typing import TypeVar

from sqlalchemy import or_
from sqlalchemy.orm import Session

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

T = TypeVar("T")


def looks_like_uuid(value: str) -> bool:
    return bool(_UUID_RE.match(value or ""))


def by_id_or_code(db: Session, model: type[T], ident: str, org_id: str, *code_columns) -> T | None:
    """The record with this UUID, or with any of `code_columns` equal to it.

    Tries the primary key first when the value looks like a UUID, so the common
    path stays a single `db.get`. Code matching is case-insensitive — a code read
    off a printed document should not fail because it was typed in lower case.
    """
    if not ident:
        return None

    if looks_like_uuid(ident):
        record = db.get(model, ident)
        if record is not None and getattr(record, "organization_id", None) == org_id:
            return record
        # A UUID that is not this table's key can still be somebody's code.

    if not code_columns:
        return None
    return (
        db.query(model)
        .filter(
            model.organization_id == org_id,
            or_(*[column.ilike(ident) for column in code_columns]),
        )
        .first()
    )


def code_filters(ident: str, *code_columns):
    """Search clauses for the same code columns, for use in a list endpoint's
    `or_(...)` so `?search=CUST-2026-1005` (or just `1005`) finds the record."""
    like = f"%{ident}%"
    return [column.ilike(like) for column in code_columns]
