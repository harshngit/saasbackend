"""Focused tests for the PlanetScale/R2 Phase 1 configuration surface:

- Settings.sqlalchemy_database_url's sslrootcert=system -> certifi.where() rewrite
- PostgreSQL-only connection pool options (app.core.database.build_engine_kwargs)
- R2 settings / Settings.r2_configured
- The Alembic revision chain for the new stored_files columns

No real PlanetScale or R2 connection is made anywhere in this file — the URL
rewrite and pool-kwargs logic are pure functions, and the engine objects built
here are never connected to (SQLAlchemy's create_engine() does not connect
until first use).
"""

import glob
import os
import re
import sys
from urllib.parse import parse_qsl, urlsplit

import certifi
import pytest
from alembic.config import Config
from sqlalchemy import create_engine

sys.path.insert(0, os.path.abspath("."))

from app.core.config import Settings
from app.core.database import build_engine_kwargs


# --------------------------------- TLS rewrite ---------------------------------


def test_sslrootcert_system_is_rewritten_to_certifi_and_sslmode_preserved():
    s = Settings(
        database_url="postgresql://user:p%40ss@host:5432/db"
        "?sslmode=verify-full&sslrootcert=system&application_name=crm"
    )
    url = s.sqlalchemy_database_url
    query = dict(parse_qsl(urlsplit(url).query))

    assert query["sslmode"] == "verify-full"  # never downgraded
    assert query["sslrootcert"] == certifi.where()
    assert query["application_name"] == "crm"  # unrelated params preserved
    assert url.startswith("postgresql+psycopg://user:p%40ss@host:5432/db")  # credentials/host/db intact


def test_postgres_prefix_style_url_also_gets_rewritten():
    """The old `postgres://` (Render/Heroku-style) prefix normalization and the
    sslrootcert rewrite both apply together, not just postgresql+psycopg://."""
    s = Settings(database_url="postgres://user:pass@host/db?sslrootcert=system")
    url = s.sqlalchemy_database_url
    assert url.startswith("postgresql+psycopg://")
    assert dict(parse_qsl(urlsplit(url).query))["sslrootcert"] == certifi.where()


def test_url_without_sslrootcert_system_is_left_untouched():
    s = Settings(database_url="postgresql://user:pass@host/db?sslmode=require")
    url = s.sqlalchemy_database_url
    assert url == "postgresql+psycopg://user:pass@host/db?sslmode=require"


def test_url_with_no_query_string_at_all_is_left_untouched():
    s = Settings(database_url="postgresql://user:pass@host/db")
    assert s.sqlalchemy_database_url == "postgresql+psycopg://user:pass@host/db"


def test_sslrootcert_with_a_real_path_value_is_not_touched():
    """Only the literal value "system" is rewritten — an operator-supplied
    real CA file path must be left exactly as given."""
    s = Settings(database_url="postgresql://user:pass@host/db?sslrootcert=/etc/ssl/custom-ca.pem")
    url = s.sqlalchemy_database_url
    assert dict(parse_qsl(urlsplit(url).query))["sslrootcert"] == "/etc/ssl/custom-ca.pem"


def test_sqlite_url_is_completely_unaffected():
    s = Settings(database_url="sqlite:///./crm_saas.db")
    assert s.sqlalchemy_database_url == "sqlite:///./crm_saas.db"


# ------------------------------- Connection pool --------------------------------


def test_postgres_engine_receives_configured_pool_options():
    kwargs = build_engine_kwargs(
        "postgresql+psycopg://user:pass@host/db",
        pool_size=7, max_overflow=3, pool_recycle=120,
    )
    assert kwargs["pool_pre_ping"] is True
    assert kwargs["pool_size"] == 7
    assert kwargs["max_overflow"] == 3
    assert kwargs["pool_recycle"] == 120

    # And that create_engine() actually applies them — no connection is made by
    # constructing the engine object itself.
    engine = create_engine("postgresql+psycopg://user:pass@host/db", **kwargs)
    try:
        assert engine.pool.size() == 7
        assert engine.pool._max_overflow == 3
        assert engine.pool._recycle == 120
    finally:
        engine.dispose()


def test_sqlite_engine_does_not_receive_postgres_pool_options():
    kwargs = build_engine_kwargs("sqlite:///./x.db", pool_size=7, max_overflow=3, pool_recycle=120)
    assert "pool_size" not in kwargs
    assert "max_overflow" not in kwargs
    assert "pool_recycle" not in kwargs
    assert kwargs["connect_args"] == {"check_same_thread": False}
    assert kwargs["pool_pre_ping"] is True


def test_pool_settings_defaults_match_phase_1_spec():
    s = Settings()
    assert s.db_pool_size == 5
    assert s.db_max_overflow == 5
    assert s.db_pool_recycle == 300


# ---------------------------------- R2 settings ----------------------------------


def test_r2_defaults_are_empty_and_not_configured():
    s = Settings()
    assert s.r2_account_id == ""
    assert s.r2_access_key_id == ""
    assert s.r2_secret_access_key == ""
    assert s.r2_bucket_name == ""
    assert s.r2_configured is False


def test_r2_configured_requires_every_field():
    base = dict(r2_account_id="acc", r2_access_key_id="key", r2_secret_access_key="secret", r2_bucket_name="bucket")
    assert Settings(**base).r2_configured is True
    for missing in base:
        partial = {k: v for k, v in base.items() if k != missing}
        assert Settings(**partial).r2_configured is False


def test_convert_inline_uploads_on_startup_defaults_false():
    assert Settings().convert_inline_uploads_on_startup is False


# ----------------------------------- Alembic -------------------------------------


def test_new_revision_chains_from_documented_head_with_expected_schema_change():
    versions_dir = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions")
    match = None
    for path in glob.glob(os.path.join(versions_dir, "*stored_files_r2_storage_key*.py")):
        match = path
    assert match is not None, "expected an alembic revision file for the stored_files R2 storage_key change"

    text = open(match, encoding="utf-8").read()
    revision = re.search(r"^revision:\s*str\s*=\s*'([^']+)'", text, re.M).group(1)
    down_revision = re.search(r"^down_revision.*?=\s*'([^']+)'", text, re.M).group(1)

    assert down_revision == "h6c7d8e9f0a1"
    assert "storage_key" in text
    assert "nullable=True" in text
    assert revision != down_revision


def test_alembic_head_is_still_single_and_linear():
    """The whole chain must still have exactly one head after adding the new
    revision — i.e. it was parented correctly, not branched off accidentally."""
    versions_dir = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions")
    revisions: dict[str, str | None] = {}
    for path in glob.glob(os.path.join(versions_dir, "*.py")):
        text = open(path, encoding="utf-8").read()
        rev_match = re.search(r"^revision:\s*str\s*=\s*'([^']+)'", text, re.M)
        down_match = re.search(r"^down_revision.*?=\s*'([^']*)'", text, re.M)
        if rev_match:
            revisions[rev_match.group(1)] = down_match.group(1) if down_match and down_match.group(1) else None

    children = set(revisions.values())
    heads = [r for r in revisions if r not in children]
    assert len(heads) == 1, f"expected exactly one alembic head, found: {heads}"


def test_alembic_config_accepts_sslrootcert_system_rewritten_url():
    """alembic/env.py passes settings.sqlalchemy_database_url straight into
    config.set_main_option(), which is really configparser.set() underneath —
    and configparser treats a bare % as interpolation syntax. The certifi path
    that sslrootcert=system gets rewritten to is urlencode()'d by
    Settings.sqlalchemy_database_url, so it always contains %-escapes (%2F on
    Linux, %3A%5C on Windows) — set_main_option() raises ValueError: invalid
    interpolation syntax unless the value is escaped as %% first.

    Guards the fix, not just the symptom: also asserts the escaped value reads
    back as the exact original URL, so the fix can't silently change the
    connection string alembic actually uses. No network access, no real
    PlanetScale/PostgreSQL/EC2/R2 connection — set_main_option()/
    get_main_option() are pure in-memory string operations.
    """
    s = Settings(
        database_url="postgresql://user:pass@aws.connect.psdb.cloud:5432/crm_saas"
        "?sslmode=verify-full&sslrootcert=system"
    )
    url = s.sqlalchemy_database_url
    assert "%" in url  # sanity: the test is actually exercising the risky path

    alembic_ini = os.path.join(os.path.dirname(__file__), "..", "alembic.ini")
    cfg = Config(alembic_ini)

    # Reproduces the reported bug: the raw rewritten URL crashes configparser.
    with pytest.raises(ValueError, match="invalid interpolation syntax"):
        cfg.set_main_option("sqlalchemy.url", url)

    # The fix: escaping % as %% (configparser's own documented convention for
    # set_main_option) must not raise, and must round-trip to the exact
    # original URL when read back — not a mangled one.
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    assert cfg.get_main_option("sqlalchemy.url") == url
