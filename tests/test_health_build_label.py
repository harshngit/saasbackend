"""/health build label: the deploy marker file first, then BUILD_TAG, then the default."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

client = TestClient(main.app)


def test_build_comes_from_the_deploy_marker_first_word(tmp_path, monkeypatch):
    marker = tmp_path / ".deploy-marker"
    marker.write_text("a1b2c3d deployed 2026-11-15T10:00Z\nsecond line\n", encoding="utf-8")
    monkeypatch.setattr(main, "DEPLOY_MARKER_PATH", str(marker))
    monkeypatch.setenv("BUILD_TAG", "old-tag")
    body = client.get("/health").json()
    assert body["build"] == "a1b2c3d"
    assert set(body) >= {"status", "build", "database"}


def test_build_falls_back_to_env_when_marker_missing_or_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DEPLOY_MARKER_PATH", str(tmp_path / "missing"))
    monkeypatch.setenv("BUILD_TAG", "env-tag")
    assert client.get("/health").json()["build"] == "env-tag"
    empty = tmp_path / "empty"
    empty.write_text("\n", encoding="utf-8")
    monkeypatch.setattr(main, "DEPLOY_MARKER_PATH", str(empty))
    assert client.get("/health").json()["build"] == "env-tag"
