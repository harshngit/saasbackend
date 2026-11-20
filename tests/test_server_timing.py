"""Every response carries Server-Timing with the request's query count and SQL / total ms --
numbers only, never SQL text or data -- and CORS exposes it to the frontend."""

import os
import re
import sys
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Organization, User  # noqa: E402
from app.models.enums import UserRole  # noqa: E402
from app.services import role_service  # noqa: E402

client = TestClient(app)
HEADER = re.compile(r'^db;dur=(\d+\.\d);desc="queries=(\d+)", total;dur=(\d+\.\d)$')


@pytest.fixture(scope="module")
def auth():
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        org = Organization(name=f"ST {uuid.uuid4().hex[:6]}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}",
                           state="Maharashtra", currency="INR")
        db.add(org)
        db.flush()
        role_service.seed_default_roles(db, org.id)
        admin = User(email=f"st.{uuid.uuid4().hex[:8]}@example.com", name="A", organization_id=org.id,
                     system_role="admin", role=UserRole.ADMIN, password_hash=hash_password("Password123!"),
                     is_active=True)
        db.add(admin)
        db.commit()
        return {"Authorization": f"Bearer {create_access_token(admin.id, 'admin', org.id)}"}


def _queries_during(fn):
    seen = {"n": 0}

    def _count(*_a):
        seen["n"] += 1

    event.listen(engine, "before_cursor_execute", _count)
    try:
        r = fn()
    finally:
        event.remove(engine, "before_cursor_execute", _count)
    return r, seen["n"]


def test_header_counts_the_requests_queries(auth):
    r, n = _queries_during(lambda: client.get("/warehouses", headers=auth))
    assert r.status_code == 200
    m = HEADER.match(r.headers["server-timing"])
    assert m, r.headers["server-timing"]
    sql_ms, queries, total_ms = float(m.group(1)), int(m.group(2)), float(m.group(3))
    assert queries == n and queries > 0
    assert 0 <= sql_ms <= total_ms


@pytest.mark.parametrize("call", [
    lambda h: client.get("/health"),
    lambda h: client.get("/warehouses"),                          # 401
    lambda h: client.get(f"/supplier-invoices/{uuid.uuid4()}", headers=h),   # 404
    lambda h: client.post("/orders", json={"items": "x"}, headers=h),        # 422
])
def test_header_on_every_kind_of_response(auth, call):
    r = call(auth)
    assert HEADER.match(r.headers.get("server-timing", "")), (r.status_code, r.headers.get("server-timing"))


def test_header_holds_numbers_only(auth):
    r = client.get("/warehouses", params={"search": "secret-value"}, headers=auth)
    value = r.headers["server-timing"]
    assert "secret" not in value and "SELECT" not in value.upper() and "warehouse" not in value.lower()


def test_cors_exposes_the_header(auth):
    origin = settings.cors_origin_list[0]
    r = client.get("/health", headers={"Origin": origin})
    assert "server-timing" in r.headers.get("access-control-expose-headers", "").lower()
