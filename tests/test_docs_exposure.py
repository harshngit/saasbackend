"""Tests for Swagger / ReDoc / OpenAPI documentation exposure configuration."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import app


def test_docs_enabled_by_default():
    """Verify that by default (ENABLE_DOCS=True), documentation endpoints are accessible."""
    client = TestClient(app)

    r_docs = client.get("/docs")
    assert r_docs.status_code == 200

    r_openapi = client.get("/openapi.json")
    assert r_openapi.status_code == 200


def test_docs_disabled_when_enable_docs_is_false():
    """Verify that when ENABLE_DOCS=False, /docs, /redoc, and /openapi.json return 404."""
    # Test setting model parsing
    settings_disabled = Settings(enable_docs=False)
    assert settings_disabled.enable_docs is False

    # Instantiate FastAPI with settings disabled (mirroring app/main.py logic)
    custom_app = FastAPI(
        title="CRM SaaS API",
        description="Backend for the CRM / Billing / Inventory SaaS. Auth & user management.",
        version="0.1.0",
        docs_url="/docs" if settings_disabled.enable_docs else None,
        redoc_url="/redoc" if settings_disabled.enable_docs else None,
        openapi_url="/openapi.json" if settings_disabled.enable_docs else None,
    )

    client = TestClient(custom_app)

    r_docs = client.get("/docs")
    assert r_docs.status_code == 404

    r_redoc = client.get("/redoc")
    assert r_redoc.status_code == 404

    r_openapi = client.get("/openapi.json")
    assert r_openapi.status_code == 404
