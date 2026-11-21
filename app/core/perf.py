"""Per-request performance line: method, path, status, total ms, SQL query count, SQL ms.

On only when PERF_LOG=true. When the database is in another region every query is a
network round trip (~60-90 ms), so the query count is the number that explains a slow
endpoint. Nothing else is logged: no SQL parameters, no bodies, no headers.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import event, text

logger = logging.getLogger("crm.perf")

# One mutable counter per request. Starlette copies the context into the threadpool that
# runs sync endpoints, so the same dict is updated from there.
_request_stats: ContextVar[dict | None] = ContextVar("crm_request_stats", default=None)


def install_query_counter(engine) -> None:  # noqa: ANN001
    @event.listens_for(engine, "before_cursor_execute")
    def _start(conn, _cursor, _statement, _params, _context, _executemany):  # noqa: ANN001
        if _request_stats.get() is not None:
            conn.info.setdefault("crm_query_start", []).append(time.perf_counter())

    @event.listens_for(engine, "after_cursor_execute")
    def _stop(conn, _cursor, _statement, _params, _context, _executemany):  # noqa: ANN001
        stats = _request_stats.get()
        starts = conn.info.get("crm_query_start")
        if stats is None or not starts:
            return
        stats["queries"] += 1
        stats["sql_ms"] += (time.perf_counter() - starts.pop()) * 1000


def server_timing(stats: dict, total_ms: float) -> str:
    """The Server-Timing header value: numbers only (query count, SQL ms, total ms) -- no
    SQL text, parameters or data. Standard syntax, so browser DevTools show it under the
    request's Timing tab: `db;dur=<sql ms>;desc="queries=<count>", total;dur=<ms>`."""
    value = f'db;dur={stats["sql_ms"]:.1f};desc="queries={stats["queries"]}", total;dur={total_ms:.1f}'
    for name, (ms, queries) in (stats.get("stages") or {}).items():
        value += f', {name};dur={ms:.1f};desc="queries={queries}"'
    return value


# A request slower than this logs one WARNING line with its stage timings, whatever
# PERF_LOG says (production cannot rely on PERF_LOG being set).
SLOW_REQUEST_MS = 5000


@contextmanager
def stage(name: str):  # noqa: ANN201
    """Time one stage of a request: wall ms and queries, added to the request's
    Server-Timing header (`<name>;dur=<ms>;desc="queries=<n>"`) and to the slow-request
    log line. `name` must be a plain token (letters, digits, '_'). A no-op outside a request."""
    stats = _request_stats.get()
    if stats is None:
        yield
        return
    started, queries = time.perf_counter(), stats["queries"]
    try:
        yield
    finally:
        stages = stats.setdefault("stages", {})
        ms, n = stages.get(name, (0.0, 0))
        stages[name] = (ms + (time.perf_counter() - started) * 1000, n + stats["queries"] - queries)


def install_middleware(app, log_lines: bool = True) -> None:  # noqa: ANN001
    """Count queries per request and add a Server-Timing header to every response.
    `log_lines` (PERF_LOG) also writes one `perf:` log line per request."""

    @app.middleware("http")
    async def _perf_log(request, call_next):  # noqa: ANN001, ANN202
        stats = {"queries": 0, "sql_ms": 0.0}
        token = _request_stats.set(stats)
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["Server-Timing"] = server_timing(stats, (time.perf_counter() - started) * 1000)
            return response
        finally:
            _request_stats.reset(token)
            total_ms = (time.perf_counter() - started) * 1000
            if log_lines:
                logger.info(
                    "perf: %s %s %s %.0fms sql=%d sql_ms=%.0f",
                    request.method, request.url.path, status, total_ms, stats["queries"], stats["sql_ms"],
                )
            if total_ms > SLOW_REQUEST_MS:
                logger.warning(
                    "slow request: %s %s %s %.0fms sql=%d sql_ms=%.0f stages=%s",
                    request.method, request.url.path, status, total_ms, stats["queries"], stats["sql_ms"],
                    " ".join(f"{k}={ms:.0f}ms/{n}q" for k, (ms, n) in (stats.get("stages") or {}).items()) or "-",
                )


def log_round_trip(engine) -> None:  # noqa: ANN001
    """One line at startup: how long a single `SELECT 1` takes from this container."""
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))  # opens the connection; not what we measure
        started = time.perf_counter()
        conn.execute(text("SELECT 1"))
        logger.info("db: SELECT 1 round trip %.1f ms", (time.perf_counter() - started) * 1000)
