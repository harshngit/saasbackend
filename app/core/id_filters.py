"""Multi-value ID query parameters (`?x=A&x=B`, `?x=A,B`, or both)."""

from collections.abc import Iterable


def normalize_ids(values: str | Iterable[str] | None) -> tuple[str, ...]:
    """The distinct IDs in a query parameter, sorted.

    Accepts a single value or the list FastAPI gives for a repeated parameter. Each
    value may itself be comma-separated. Whitespace is trimmed, empty pieces dropped
    and duplicates removed; the result is sorted so equivalent selections ("A,B" and
    "B&A") always come out the same. An empty tuple means "no filter".
    """
    if values is None:
        return ()
    if isinstance(values, str):
        values = [values]
    seen: set[str] = set()
    for value in values:
        if value is None:
            continue
        for piece in str(value).split(","):
            piece = piece.strip()
            if piece:
                seen.add(piece)
    return tuple(sorted(seen))
