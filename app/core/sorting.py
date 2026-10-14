from typing import Any, Callable, Mapping, Sequence
from fastapi import HTTPException, status
from sqlalchemy import asc, desc
from sqlalchemy.orm import Query
from sqlalchemy.sql.elements import ColumnElement


def validate_sort_params(
    sort_by: str | None,
    sort_order: str | None,
    allowed_fields: Mapping[str, Any],
    default_sort_order: str = "desc",
) -> tuple[str | None, str]:
    """Validate sort_by and sort_order parameters against allowed whitelist.

    Raises HTTP 400 if sort_order is not 'asc'/'desc' or sort_by is not whitelisted.
    """
    order = (sort_order or default_sort_order).lower()
    if order not in ("asc", "desc"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid sort_order '{sort_order}'. Allowed fields: 'asc', 'desc'",
        )

    if sort_by is not None:
        sort_by_clean = sort_by.strip()
        if not sort_by_clean:
            return None, order
        if sort_by_clean not in allowed_fields:
            allowed = sorted(list(allowed_fields.keys()))
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid sort_by '{sort_by_clean}'. Allowed fields: {allowed}",
            )
        return sort_by_clean, order

    return None, order


def apply_sorting(
    query: Query,
    sort_by: str | None,
    sort_order: str | None,
    allowed_fields: Mapping[str, Any] | None = None,
    default_order: Sequence[Any] | None = None,
    secondary_key: Any = None,
    default_sort_order: str = "desc",
    *,
    sortable_fields: Mapping[str, Any] | None = None,
    default_sort: Any = None,
    secondary_sort: Any = None,
) -> Query:
    """Apply validated database-level sorting to an existing SQLAlchemy query.

    - When sort_by is omitted, preserves exact default_order, optionally appending secondary_key.
    - When sort_by is specified, maps to whitelisted ColumnElement / expression / subquery,
      applies asc/desc with nullslast(), and adds secondary_key for deterministic pagination.
    """
    fields = allowed_fields if allowed_fields is not None else sortable_fields
    if fields is None:
        raise ValueError("allowed_fields or sortable_fields must be provided to apply_sorting")

    if default_order is not None:
        if isinstance(default_order, (list, tuple)):
            defaults = list(default_order)
        else:
            defaults = [default_order]
    elif default_sort is not None:
        if isinstance(default_sort, (list, tuple)):
            defaults = list(default_sort)
        else:
            defaults = [default_sort]
    else:
        defaults = []

    sec_key = secondary_key if secondary_key is not None else secondary_sort

    clean_field, order = validate_sort_params(
        sort_by, sort_order, fields, default_sort_order=default_sort_order
    )

    if clean_field is None:
        clauses = list(defaults)
        if sec_key is not None and sec_key not in clauses:
            clauses.append(sec_key)
        return query.order_by(*clauses)

    target = fields[clean_field]

    # Target can be a callable taking (order: str) -> list[Clause] or Clause
    if callable(target):
        try:
            resolved = target(order)
        except TypeError:
            resolved = target()
    else:
        resolved = target

    order_func = asc if order == "asc" else desc

    order_clauses = []
    if isinstance(resolved, (list, tuple)):
        for item in resolved:
            if hasattr(item, "asc") and hasattr(item, "desc"):
                clause = order_func(item)
                if hasattr(clause, "nullslast"):
                    clause = clause.nullslast()
                order_clauses.append(clause)
            else:
                # Already an ordered clause (e.g. asc(Col) or BinaryExpression)
                order_clauses.append(item)
    else:
        if hasattr(resolved, "asc") and hasattr(resolved, "desc"):
            clause = order_func(resolved)
            if hasattr(clause, "nullslast"):
                clause = clause.nullslast()
            order_clauses.append(clause)
        else:
            order_clauses.append(resolved)

    if sec_key is not None:
        order_clauses.append(sec_key)

    return query.order_by(*order_clauses)
