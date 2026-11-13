"""Faster container start: ensure_migrated, background startup work, lazy heavy imports."""

import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text  # noqa: E402

from app.core import ensure_migrated as em  # noqa: E402


# ------------------------------------------------------------------ ensure_migrated


def test_script_heads_match_alembic_without_importing_migrations():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    assert em.script_heads() == set(ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_heads())


def test_ensure_migrated_does_not_load_app_models_or_env(tmp_path):
    """The no-op path must stay cheap: no app models, no app engine, no alembic env.py."""
    db = tmp_path / "at_head.db"
    eng = create_engine(f"sqlite:///{db}")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        for head in em.script_heads():
            c.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": head})
    eng.dispose()
    probe = ("import sys; from app.core import ensure_migrated as em; rc = em.main(); "
             "bad = [m for m in sys.modules if m.startswith(('app.models', 'app.routers', 'app.main', 'app.core.database'))]; "
             "print('LOADED', bad); sys.exit(rc)")
    p = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                       env=dict(os.environ, DATABASE_URL=f"sqlite:///{db}", PYTHONPATH=str(ROOT)))
    assert p.returncode == 0, p.stderr
    assert "LOADED []" in p.stdout and "already at head" in p.stderr


def _sqlite_at(tmp_path, version):
    url = f"sqlite:///{tmp_path / f'{uuid.uuid4().hex}.db'}"
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        if version:
            c.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": version})
    eng.dispose()
    return url


def _stamper(url, calls):
    def upgrade():
        calls.append(time.perf_counter())
        eng = create_engine(url)
        with eng.begin() as c:
            c.execute(text("DELETE FROM alembic_version"))
            for head in em.script_heads():
                c.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": head})
        eng.dispose()
    return upgrade


def test_at_head_never_runs_the_upgrade(tmp_path):
    url = _sqlite_at(tmp_path, next(iter(em.script_heads())))
    calls = []
    assert em.ensure_migrated(url, upgrade=_stamper(url, calls)) == "at_head" and calls == []


def test_behind_runs_the_upgrade_once_and_checks_the_result(tmp_path):
    url = _sqlite_at(tmp_path, "d5b8f0c3e2a4")
    calls = []
    assert em.ensure_migrated(url, upgrade=_stamper(url, calls)) == "upgraded" and len(calls) == 1
    assert em.ensure_migrated(url, upgrade=_stamper(url, calls)) == "at_head" and len(calls) == 1


def test_failed_or_incomplete_upgrade_is_an_error_and_exit_code_1(tmp_path, monkeypatch, capsys):
    url = _sqlite_at(tmp_path, "d5b8f0c3e2a4")

    def boom():
        raise RuntimeError("relation already exists")

    with pytest.raises(em.MigrationError, match="relation already exists"):
        em.ensure_migrated(url, upgrade=boom)
    with pytest.raises(em.MigrationError, match="expected"):
        em.ensure_migrated(url, upgrade=lambda: None)  # "succeeded" but did not reach head
    monkeypatch.setattr(em, "ensure_migrated", lambda: (_ for _ in ()).throw(em.MigrationError("x failed")))
    assert em.main() == 1
    assert "migration FAILED" in capsys.readouterr().err


@pytest.mark.skipif(not os.getenv("DATABASE_URL", "").startswith("postgres"), reason="needs PostgreSQL")
def test_two_containers_never_migrate_at_the_same_time():
    """Two concurrent ensure_migrated runs on PostgreSQL: the advisory lock serialises
    them, the second sees head after waiting and does not run the upgrade again."""
    schema = f"em_{uuid.uuid4().hex[:8]}"
    base = os.environ["DATABASE_URL"].replace("postgresql://", "postgresql+psycopg://", 1)
    admin = create_engine(base)
    with admin.begin() as c:
        c.execute(text(f"CREATE SCHEMA {schema}"))
        c.execute(text(f"CREATE TABLE {schema}.alembic_version (version_num VARCHAR(32) NOT NULL)"))
        c.execute(text(f"INSERT INTO {schema}.alembic_version VALUES ('d5b8f0c3e2a4')"))
    url = base + ("&" if "?" in base else "?") + f"options=-csearch_path%3D{schema}"
    running, overlaps, calls = [0], [], []
    guard = threading.Lock()

    def slow_upgrade():
        with guard:
            running[0] += 1
            overlaps.append(running[0])
        time.sleep(1.0)
        _stamper(url, calls)()
        with guard:
            running[0] -= 1

    results = []
    threads = [threading.Thread(target=lambda: results.append(em.ensure_migrated(url, upgrade=slow_upgrade)))
               for _ in range(2)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert sorted(results) == ["at_head", "upgraded"]
        assert len(calls) == 1 and max(overlaps) == 1
    finally:
        with admin.begin() as c:
            c.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        admin.dispose()


# ------------------------------------------------------------------ startup work off the critical path


def test_startup_returns_at_once_and_health_answers_while_work_runs(monkeypatch):
    import app.main as main
    from fastapi.testclient import TestClient

    release = threading.Event()
    monkeypatch.setattr(main.settings, "startup_tasks_in_background", True)
    monkeypatch.setattr(main, "run_startup_tasks", lambda: (main.STARTUP_STATE.update(state="running"),
                                                            release.wait(10),
                                                            main.STARTUP_STATE.update(state="done")))
    started = time.perf_counter()
    try:
        with TestClient(main.app) as client:  # runs the startup event
            assert time.perf_counter() - started < 5
            assert main.STARTUP_STATE["state"] == "running"
            r = client.get("/health")
            assert r.status_code == 200 and r.json()["status"] == "ok"
    finally:
        release.set()


def test_inline_mode_runs_the_work_before_serving(monkeypatch):
    import app.main as main
    from fastapi.testclient import TestClient

    seen = []
    monkeypatch.setattr(main.settings, "startup_tasks_in_background", False)
    monkeypatch.setattr(main, "run_startup_tasks", lambda: seen.append("ran"))
    with TestClient(main.app):
        assert seen == ["ran"]


def test_startup_tasks_still_run_every_step(monkeypatch):
    """The background function keeps the old steps and ends in state done."""
    import app.main as main

    monkeypatch.setattr(main.settings, "run_startup_migrations", False)
    monkeypatch.setattr(main.settings, "seed_on_startup", False)
    monkeypatch.setattr(main.settings, "follow_up_reminders_enabled", False)
    main.run_startup_tasks()
    assert main.STARTUP_STATE["state"] == "done" and main.STARTUP_STATE["seconds"] is not None


# ------------------------------------------------------------------ lazy heavy imports


def test_heavy_libraries_are_not_imported_at_startup():
    probe = ("import sys, app.main; heavy = [m for m in ('fpdf', 'openpyxl', 'firebase_admin', 'fontTools') "
             "if m in sys.modules]; print('HEAVY', heavy)")
    p = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                       env=dict(os.environ, PYTHONPATH=str(ROOT)))
    assert p.returncode == 0, p.stderr
    assert "HEAVY []" in p.stdout


def test_lazy_imports_still_work():
    from app.core.excel_import import generate_xlsx_template, parse_spreadsheet_rows

    content = generate_xlsx_template(["name", "phone"], ["Shop", "999"])
    rows, errors = parse_spreadsheet_rows(content, "t.xlsx", ["name", "phone"])
    assert not errors and rows and rows[0]["name"] == "Shop"
    from app.core import pdf_docs  # importable on demand

    assert callable(pdf_docs.quotation_pdf)
