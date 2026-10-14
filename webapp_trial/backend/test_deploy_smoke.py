"""
Deployment smoke tests: the containerised app must boot and expose its health
and deployment-config endpoints, and the SFTP feature guard must engage when
WEBAPP_DISABLE_SFTP is set (the Plexus configuration).

Env vars are set with monkeypatch so they are restored after each test and never
leak into other test modules (pytest shares one process).
"""

from __future__ import annotations

import importlib
import sys

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def make_app(tmp_path, monkeypatch):
    """Return a factory that builds a fresh app in local mode, optionally with
    the SFTP features disabled. All env changes auto-revert after the test."""

    def _factory(disable_sftp: bool):
        monkeypatch.setenv("TRIAL_STORAGE_DIR", str(tmp_path / "storage"))
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("WEBAPP_S3_BUCKET", raising=False)
        if disable_sftp:
            monkeypatch.setenv("WEBAPP_DISABLE_SFTP", "true")
        else:
            monkeypatch.delenv("WEBAPP_DISABLE_SFTP", raising=False)
        # Rebuild the module graph so db/storage pick up local-mode defaults.
        for name in ("deployconfig", "storage", "db", "api"):
            sys.modules.pop(name, None)
        import api  # noqa: PLC0415

        importlib.reload(api)
        return api.app

    yield _factory

    # Leave the shared modules back in their default (local) state for the rest
    # of the suite, regardless of what this test configured.
    for name in ("deployconfig", "storage", "db", "api"):
        sys.modules.pop(name, None)
    import db  # noqa: PLC0415

    importlib.reload(db)


def test_health_and_info(make_app):
    with TestClient(make_app(disable_sftp=False)) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        assert client.get("/health").json()["status"] == "ok"
        info = client.get("/api/info").json()
        assert info["deployment"]["db_backend"] == "sqlite"
        assert info["deployment"]["storage_backend"] == "local"
        assert info["deployment"]["sftp_features_enabled"] is True


def test_sftp_guard_blocks_when_disabled(make_app):
    with TestClient(make_app(disable_sftp=True)) as client:
        info = client.get("/api/info").json()
        assert info["deployment"]["sftp_features_enabled"] is False

        # Download listing degrades to "unavailable" rather than erroring.
        downloads = client.get("/api/downloads").json()
        assert downloads["available"] is False

        # Active SFTP actions are blocked with 503.
        assert client.post("/api/downloads/check").status_code == 503
        assert client.post("/api/validation/submit", json={"key": "x"}).status_code == 503

        # A real submit via submit-all is blocked; a dry-run is still allowed.
        blocked = client.post("/api/submit-all", json={"keys": [], "dry_run": False})
        assert blocked.status_code == 503
        dry = client.post("/api/submit-all", json={"keys": [], "dry_run": True})
        assert dry.status_code == 200


def test_core_upload_flow_still_works(make_app):
    """Upload -> job created -> listed, with SFTP disabled (Plexus config)."""
    pdf = b"%PDF-1.4\n%fake minimal pdf\n"
    with TestClient(make_app(disable_sftp=True)) as client:
        resp = client.post(
            "/api/jobs",
            files={"file": ("HB1.pdf", pdf, "application/pdf")},
            data={"state_code": "NC"},
        )
        assert resp.status_code == 200, resp.text
        job = resp.json()
        assert job["filename"] == "HB1.pdf"
        listed = client.get("/api/jobs").json()
        assert any(j["id"] == job["id"] for j in listed)
