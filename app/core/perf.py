"""Per-request performance line: method, path, status, total ms, SQL query count, SQL ms.

On only when PERF_LOG=true. When the database is in another region every query is a
network round trip (~60-90 ms), so the query count is the number that explains a slow
endpoint. Nothing else is logged: no SQL parameters, no bodies, no headers.
"""

from __future__ import annotations

import logging
import time
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


def install_middleware(app) -> None:  # noqa: ANN001
    @app.middleware("http")
    async def _perf_log(request, call_next):  # noqa: ANN001, ANN202
        stats = {"queries": 0, "sql_ms": 0.0}
        token = _request_stats.set(stats)
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            _request_stats.reset(token)
            logger.info(
                "perf: %s %s %s %.0fms sql=%d sql_ms=%.0f",
                request.method, request.url.path, status, (time.perf_counter() - started) * 1000,
                stats["queries"], stats["sql_ms"],
            )


def log_round_trip(engine) -> None:  # noqa: ANN001
    """One line at startup: how long a single `SELECT 1` takes from this container."""
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))  # opens the connection; not what we measure
        started = time.perf_counter()
        conn.execute(text("SELECT 1"))
        logger.info("db: SELECT 1 round trip %.1f ms", (time.perf_counter() - started) * 1000)
