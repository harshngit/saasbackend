"""Unhandled errors become a JSON 500 that still carries CORS headers.

Starlette runs `@app.exception_handler(Exception)` in ServerErrorMiddleware, which is
always the OUTERMOST layer -- outside CORSMiddleware. A 500 produced there never gets
`Access-Control-Allow-Origin`, so the browser reports a CORS failure (ERR_FAILED)
instead of the 500, and the frontend cannot read the error.

`UnhandledErrorMiddleware` is installed directly inside CORSMiddleware: it catches the
exception, logs it once (method, route, user id -- never tokens, passwords, bodies or
query strings) and sends the JSON 500 itself, so CORSMiddleware adds its headers on the
way out exactly as for any other response.
"""

from __future__ import annotations

import logging

from fastapi.responses import JSONResponse
from starlette.requests import Request

logger = logging.getLogger("crm.errors")

SERVER_ERROR_DETAIL = "An unexpected server error occurred. Please try again later or contact support."


def _user_id(request: Request) -> str | None:
    """The `sub` of a valid bearer token, for the log line. The token itself is never logged."""
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        return None
    try:
        from app.core.security import decode_token

        return str(decode_token(auth[7:].strip()).get("sub") or "") or None
    except Exception:  # noqa: BLE001 -- an invalid token just means "unknown user"
        return None


def _route(request: Request) -> str:
    """The route template (e.g. /deliveries/{delivery_id}) when known, else the path."""
    route = request.scope.get("route")
    return getattr(route, "path", None) or request.url.path


def _trace_id(request: Request) -> str:
    """The W3C trace id from a `traceparent` header (00-<trace id>-<span id>-<flags>), if any."""
    parts = (request.headers.get("traceparent") or "").split("-")
    return parts[1] if len(parts) >= 3 else "-"


def log_unhandled(request: Request, exc: BaseException) -> None:
    """One log line per unhandled error, traceback included, with the Cloudflare Ray ID and
    trace id so it can be found from the 500 seen in the Worker's request log.

    The traceback is folded onto the same line (newlines -> " | "): container stdout/stderr
    reaches Cloudflare Observability line by line, so a multi-line traceback would become
    many separate, unsearchable events. Content is the same as the old exc_info traceback
    (a SQLAlchemy error message includes its [SQL: ...] and [parameters: ...]); only the
    last 4000 characters are kept, which is where the raising frame and message are."""
    import traceback

    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    tb = tb.strip().replace("\r", "").replace("\n", " | ")[-4000:]
    logger.error(
        "Unhandled exception: %s %s (path %s) user=%s ray=%s trace=%s: %s :: %s",
        request.method,
        _route(request),
        request.url.path,
        _user_id(request) or "-",
        request.headers.get("cf-ray") or "-",
        _trace_id(request),
        type(exc).__name__,
        tb,
    )


def server_error_response(headers: dict | None = None) -> JSONResponse:
    return JSONResponse(status_code=500, content={"detail": SERVER_ERROR_DETAIL}, headers=headers or None)


class UnhandledErrorMiddleware:
    """Pure ASGI. Must be added just before CORSMiddleware (so it sits inside it)."""

    def __init__(self, app) -> None:  # noqa: ANN001
        self.app = app

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message) -> None:  # noqa: ANN001
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception as exc:  # noqa: BLE001
            if started:  # headers already sent: nothing valid can be sent any more
                raise
            log_unhandled(Request(scope), exc)
            await server_error_response()(scope, receive, send)
