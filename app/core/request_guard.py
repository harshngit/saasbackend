"""Stop a read request's work once nobody is waiting for it.

Endpoints here are plain `def` functions: Starlette runs each one in a worker thread,
and a thread cannot be killed from outside. So when the browser cancels a request (a
page change, a frontend timeout) the endpoint used to run to the end anyway -- holding
a database connection and the container's CPU while the next requests queued behind it.

For GET/HEAD requests this middleware keeps a small per-request state:
- `cancelled` -- set the moment the client disconnects before the response was sent;
- `deadline`  -- READ_REQUEST_TIMEOUT_SECONDS after the request started (0 = none).

A SQLAlchemy hook checks that state before every query and raises RequestStopped
instead of running it, so the endpoint stops at its next database call, the session
rolls back, and the connection goes back to the pool. Writes (POST/PATCH/PUT/DELETE)
are never stopped half-way: a write that reached the database runs to its commit.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextvars import ContextVar

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import event

logger = logging.getLogger("crm.requests")

_GUARDED_METHODS = {"GET", "HEAD"}
_state: ContextVar[dict | None] = ContextVar("crm_request_guard", default=None)


class RequestStopped(Exception):
    """Raised instead of the next query of a read request nobody waits for any more."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def install_query_check(engine) -> None:  # noqa: ANN001
    @event.listens_for(engine, "before_cursor_execute")
    def _stop_if_abandoned(_conn, _cursor, _statement, _params, _context, _executemany):  # noqa: ANN001
        state = _state.get()
        if state is None or state["done"]:
            return
        state["queries"] += 1
        if state["cancelled"]:
            raise RequestStopped("client disconnected")
        if state["deadline"] is not None and time.monotonic() > state["deadline"]:
            raise RequestStopped("time limit reached")


class RequestGuardMiddleware:
    """Pure ASGI middleware (outermost), so it sees the client's own disconnect message."""

    def __init__(self, app, timeout_seconds: float) -> None:  # noqa: ANN001
        self.app = app
        self.timeout_seconds = timeout_seconds

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope["type"] != "http" or scope.get("method") not in _GUARDED_METHODS:
            await self.app(scope, receive, send)
            return

        started = time.monotonic()
        state = {
            "cancelled": False,
            "done": False,
            "queries": 0,
            "deadline": started + self.timeout_seconds if self.timeout_seconds > 0 else None,
        }
        token = _state.set(state)
        messages: asyncio.Queue = asyncio.Queue()

        async def watch_client() -> None:
            # The only reader of the real `receive`: every message is passed on to the
            # app unchanged; a disconnect before the response finished also marks the
            # request cancelled.
            while True:
                message = await receive()
                if message["type"] == "http.disconnect" and not state["done"]:
                    state["cancelled"] = True
                await messages.put(message)
                if message["type"] == "http.disconnect":
                    return

        async def app_receive():  # noqa: ANN202
            return await messages.get()

        async def app_send(message) -> None:  # noqa: ANN001
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                state["done"] = True
            await send(message)

        watcher = asyncio.create_task(watch_client())
        try:
            await self.app(scope, app_receive, app_send)
        finally:
            state["done"] = True
            watcher.cancel()
            _state.reset(token)


async def request_stopped_handler(request: Request, exc: RequestStopped) -> JSONResponse:
    """One INFO line per stopped request (no parameters, no user data). For a cancelled
    request nobody reads the response; for a timed-out one the client gets a 503."""
    state = _state.get() or {}
    logger.info("request stopped (%s): %s %s after %d queries",
                exc.reason, request.method, request.url.path, state.get("queries", 0))
    return JSONResponse(
        status_code=503,
        content={"detail": "The request took too long and was stopped. Please try again or narrow the filters."},
    )


def install(app, engine, timeout_seconds: float) -> None:  # noqa: ANN001
    install_query_check(engine)
    app.add_exception_handler(RequestStopped, request_stopped_handler)
    app.add_middleware(RequestGuardMiddleware, timeout_seconds=timeout_seconds)
