import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings, parse_bool_env


def test_docs_disabled_by_default():
    """Verify that by default (ENABLE_DOCS=False), documentation endpoints are not exposed."""
    fresh_settings = Settings(_env_file=None)
    assert fresh_settings.enable_docs is False

    custom_app = FastAPI(
        title="CRM SaaS API",
        version="0.1.0",
        docs_url="/docs" if fresh_settings.enable_docs else None,
        redoc_url="/redoc" if fresh_settings.enable_docs else None,
        openapi_url="/openapi.json" if fresh_settings.enable_docs else None,
    )
    client = TestClient(custom_app)

    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_docs_enabled_when_enable_docs_is_true():
    """Verify that when ENABLE_DOCS=True, /docs, /redoc, and /openapi.json return 200."""
    settings_enabled = Settings(_env_file=None, enable_docs=True)
    assert settings_enabled.enable_docs is True

    custom_app = FastAPI(
        title="CRM SaaS API",
        version="0.1.0",
        docs_url="/docs" if settings_enabled.enable_docs else None,
        redoc_url="/redoc" if settings_enabled.enable_docs else None,
        openapi_url="/openapi.json" if settings_enabled.enable_docs else None,
    )
    client = TestClient(custom_app)

    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_boolean_parsing_accepted_true_values():
    """Verify true, 1, yes (case-insensitive with surrounding whitespace) parse as True."""
    true_inputs = ["true", " TRUE ", "True", "1", "yes", " YES ", " Yes ", 1, True]

    for val in true_inputs:
        s = Settings(_env_file=None, enable_docs=val, convert_inline_uploads_on_startup=val, expose_reset_token=val, smtp_use_tls=val, seed_on_startup=val)
        assert s.enable_docs is True, f"Failed for {val!r}"
        assert s.convert_inline_uploads_on_startup is True, f"Failed for {val!r}"
        assert s.expose_reset_token is True, f"Failed for {val!r}"
        assert s.smtp_use_tls is True, f"Failed for {val!r}"
        assert s.seed_on_startup is True, f"Failed for {val!r}"


def test_boolean_parsing_accepted_false_values():
    """Verify false, 0, no (case-insensitive with surrounding whitespace) parse as False."""
    false_inputs = ["false", " FALSE ", "False", "0", "no", " NO ", " No ", 0, False]

    for val in false_inputs:
        s = Settings(_env_file=None, enable_docs=val, convert_inline_uploads_on_startup=val, expose_reset_token=val, smtp_use_tls=val, seed_on_startup=val)
        assert s.enable_docs is False, f"Failed for {val!r}"
        assert s.convert_inline_uploads_on_startup is False, f"Failed for {val!r}"
        assert s.expose_reset_token is False, f"Failed for {val!r}"
        assert s.smtp_use_tls is False, f"Failed for {val!r}"
        assert s.seed_on_startup is False, f"Failed for {val!r}"


def test_boolean_parsing_invalid_values_fallback_and_logging(caplog):
    """Verify unrecognized values do NOT raise ValidationError, fall back to safe defaults, and log an error naming the env var."""
    with caplog.at_level(logging.ERROR, logger="crm.config"):
        # Test enable_docs fallback to False on invalid input
        s1 = Settings(_env_file=None, enable_docs="garbage")
        assert s1.enable_docs is False
        assert any("ENABLE_DOCS" in record.message for record in caplog.records)

        # Test smtp_use_tls fallback to True on invalid input
        caplog.clear()
        s2 = Settings(_env_file=None, smtp_use_tls="invalid_tls")
        assert s2.smtp_use_tls is True
        assert any("SMTP_USE_TLS" in record.message for record in caplog.records)

        # Test seed_on_startup fallback to False on invalid input
        caplog.clear()
        s3 = Settings(_env_file=None, seed_on_startup="not_a_bool")
        assert s3.seed_on_startup is False
        assert any("SEED_ON_STARTUP" in record.message for record in caplog.records)


def test_standalone_parse_bool_env_helper(caplog):
    """Test parse_bool_env helper function directly."""
    assert parse_bool_env(None, "my_var", True) is True
    assert parse_bool_env(None, "my_var", False) is False
    assert parse_bool_env(True, "my_var", False) is True
    assert parse_bool_env(False, "my_var", True) is False
    assert parse_bool_env("true", "my_var", False) is True
    assert parse_bool_env(" 1 ", "my_var", False) is True
    assert parse_bool_env("YES", "my_var", False) is True
    assert parse_bool_env("false", "my_var", True) is False
    assert parse_bool_env("0", "my_var", True) is False
    assert parse_bool_env("no", "my_var", True) is False

    with caplog.at_level(logging.ERROR, logger="crm.config"):
        res = parse_bool_env("unsupported", "custom_bool", False)
        assert res is False
        assert any("CUSTOM_BOOL" in record.message for record in caplog.records)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))


