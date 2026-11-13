"""Every error response carries CORS headers, so the browser shows the real status
instead of "No 'Access-Control-Allow-Origin' header" (ERR_FAILED).

CORSMiddleware must be the outermost app middleware, and an unhandled exception must
become a JSON 500 *inside* it (app.core.error_responses.UnhandledErrorMiddleware).
"""

import logging
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.core.error_responses import SERVER_ERROR_DETAIL, UnhandledErrorMiddleware  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Organization, User  # noqa: E402
from app.models.enums import UserRole  # noqa: E402
from app.routers import notifications as notifications_router  # noqa: E402
from app.services import rate_limit_service  # noqa: E402

ALLOWED = next((o for o in settings.cors_origin_list if o != "*"), "http://localhost:5173")
FOREIGN = "https://evil.example.com"


@app.get("/__test__/cors-boom", include_in_schema=False)
def _boom():  # a forced, unhandled 500
    raise RuntimeError("forced failure for the CORS test")


@app.get("/__test__/cors-429", include_in_schema=False)
def _throttled():  # a forced 429, the way every rate limit in this app raises it
    raise HTTPException(status_code=429, detail="Too many requests")


client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture(scope="module", autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)


@pytest.fixture
def org_admin():
    db = SessionLocal()
    try:
        org = Organization(name=f"CORS {uuid.uuid4().hex[:6]}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}")
        db.add(org)
        db.flush()
        user = User(email=f"cors.{uuid.uuid4().hex[:8]}@example.com", name="Cors Admin", organization_id=org.id,
                    system_role="admin", role=UserRole.ADMIN, password_hash=hash_password("Password123!"),
                    is_active=True)
        db.add(user)
        db.commit()
        token = create_access_token(user.id, user.system_role, user.organization_id)
        return user.id, token
    finally:
        db.close()


def _cors_ok(response, origin=ALLOWED):
    return (response.headers.get("access-control-allow-origin") == origin
            and response.headers.get("access-control-allow-credentials") == "true")


def test_cors_is_the_outermost_app_middleware_with_the_error_middleware_inside_it():
    classes = [m.cls for m in app.user_middleware]
    assert classes[0] is CORSMiddleware  # user_middleware[0] = outermost
    assert classes[1] is UnhandledErrorMiddleware


def test_forced_500_is_json_and_carries_cors_headers():
    r = client.get("/__test__/cors-boom", headers={"Origin": ALLOWED})
    assert r.status_code == 500
    assert r.json() == {"detail": SERVER_ERROR_DETAIL}
    assert _cors_ok(r)


def test_forced_429_carries_cors_headers():
    r = client.get("/__test__/cors-429", headers={"Origin": ALLOWED})
    assert r.status_code == 429 and _cors_ok(r)


def test_real_endpoint_500_carries_cors_and_logs_route_and_user_but_never_the_token(org_admin, monkeypatch, caplog):
    user_id, token = org_admin

    def broken(**_kw):
        raise RuntimeError("database fell over")

    monkeypatch.setattr(notifications_router, "UnreadCount", broken)
    with caplog.at_level(logging.ERROR, logger="crm.errors"):
        r = client.get("/notifications/unread-count",
                       headers={"Origin": ALLOWED, "Authorization": f"Bearer {token}"})
    assert r.status_code == 500 and _cors_ok(r)
    logged = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "GET /notifications/unread-count" in logged and f"user={user_id}" in logged
    assert token not in caplog.text and "Bearer" not in caplog.text


def test_real_rate_limit_429_carries_cors(org_admin, monkeypatch):
    _, token = org_admin
    monkeypatch.setattr(rate_limit_service, "exceeded", lambda *a, **k: True)
    r = client.post("/support/tickets", json={"subject": "s", "description": "d", "category": "bug"},
                    headers={"Origin": ALLOWED, "Authorization": f"Bearer {token}"})
    assert r.status_code == 429 and _cors_ok(r)


def test_ordinary_4xx_carry_cors():
    for r in (client.get("/auth/me", headers={"Origin": ALLOWED}),  # no token
              client.get("/organizations/me/entitlements", headers={"Origin": ALLOWED,
                                                                     "Authorization": "Bearer not-a-token"})):
        assert r.status_code in (401, 403) and _cors_ok(r), r.status_code


def test_preflight_for_the_reported_endpoints():
    for path in ("/notifications/unread-count", "/organizations/me/entitlements", "/auth/me",
                 "/deliveries/collections/summary"):
        r = client.options(path, headers={"Origin": ALLOWED, "Access-Control-Request-Method": "GET",
                                          "Access-Control-Request-Headers": "authorization"})
        assert r.status_code == 200 and r.headers.get("access-control-allow-origin") == ALLOWED, path


@pytest.mark.skipif("*" in settings.cors_origin_list, reason="CORS_ORIGINS=* allows every origin")
def test_a_500_never_echoes_a_disallowed_origin():
    r = client.get("/__test__/cors-boom", headers={"Origin": FOREIGN})
    assert r.status_code == 500 and "access-control-allow-origin" not in r.headers
