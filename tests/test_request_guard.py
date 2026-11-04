"""A read request nobody waits for any more stops at its next database query.

- the client disconnects mid-request  -> the endpoint stops, the connection is released;
- the time limit passes               -> the endpoint stops and answers 503;
- a write (POST) is never stopped half-way, even if its client disconnects;
- work after the response (background tasks) is not affected.
"""

import asyncio
import logging
import time

from fastapi import BackgroundTasks, Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core import request_guard
from app.core.database import engine, get_db

QUERIES = 60
PAUSE = 0.02  # seconds between queries: the whole loop takes ~1.2 s


def _app(timeout_seconds: float) -> tuple[FastAPI, dict]:
    app = FastAPI()
    request_guard.install(app, engine, timeout_seconds)
    seen = {"done": 0, "background": 0}

    def work(db: Session) -> None:
        for _ in range(QUERIES):
            db.execute(text("SELECT 1"))
            seen["done"] += 1
            time.sleep(PAUSE)

    @app.get("/slow")
    def slow(db: Session = Depends(get_db)) -> dict:
        work(db)
        return {"ok": True}

    @app.post("/slow")
    def slow_write(db: Session = Depends(get_db)) -> dict:
        work(db)
        return {"ok": True}

    @app.get("/quick")
    def quick(background: BackgroundTasks, db: Session = Depends(get_db)) -> dict:
        db.execute(text("SELECT 1"))

        def after() -> None:
            with Session(bind=engine) as later:
                later.execute(text("SELECT 1"))
                seen["background"] += 1

        background.add_task(after)
        return {"ok": True}

    return app, seen


async def _call(app, method: str, path: str, disconnect_after: float | None = None,
                disconnect_when=None) -> list[dict]:  # noqa: ANN001
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "http", "path": path, "raw_path": path.encode(), "root_path": "", "query_string": b"",
             "headers": [(b"host", b"test")], "client": ("127.0.0.1", 1), "server": ("test", 80)}
    sent: list[dict] = []
    first = True
    finished = asyncio.Event()

    async def receive():  # noqa: ANN202
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": b"", "more_body": False}
        if disconnect_when is not None:
            while not disconnect_when():
                await asyncio.sleep(0.005)
        elif disconnect_after is not None:
            await asyncio.sleep(disconnect_after)
        else:
            await finished.wait()
        return {"type": "http.disconnect"}

    async def send(message) -> None:  # noqa: ANN001
        sent.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body"):
            finished.set()

    await app(scope, receive, send)
    return sent


def _status(sent: list[dict]) -> int | None:
    return next((m["status"] for m in sent if m["type"] == "http.response.start"), None)


def test_disconnected_read_stops_at_its_next_query(caplog):
    app, seen = _app(timeout_seconds=0)
    with caplog.at_level(logging.INFO, logger="crm.requests"):
        # The client goes away once the endpoint has run 5 of its 60 queries.
        asyncio.run(_call(app, "GET", "/slow", disconnect_when=lambda: seen["done"] >= 5))
    assert 5 <= seen["done"] < QUERIES / 2, seen  # stopped soon after the disconnect
    assert any("request stopped (client disconnected): GET /slow" in r.getMessage() for r in caplog.records)
    # The connection went back to the pool: a new request runs normally.
    seen["done"] = 0
    sent = asyncio.run(_call(app, "GET", "/slow", disconnect_after=None))
    assert _status(sent) == 200 and seen["done"] == QUERIES


def test_read_past_the_time_limit_answers_503():
    app, seen = _app(timeout_seconds=0.2)
    sent = asyncio.run(_call(app, "GET", "/slow", disconnect_after=None))
    assert _status(sent) == 503 and seen["done"] < QUERIES / 2


def test_a_write_is_never_stopped_half_way():
    app, seen = _app(timeout_seconds=0.1)
    sent = asyncio.run(_call(app, "POST", "/slow", disconnect_after=0.1))
    assert seen["done"] == QUERIES and _status(sent) == 200


def test_work_after_the_response_is_not_stopped():
    app, seen = _app(timeout_seconds=0)
    sent = asyncio.run(_call(app, "GET", "/quick", disconnect_after=None))
    assert _status(sent) == 200 and seen["background"] == 1
