"""
PostgreSQL backend integration test for db.py.

Spins up a throwaway local PostgreSQL via `pgserver` (bundled binaries, no
system install) and runs the full db.py public interface against it, proving the
SQLite->Postgres migration works for real: schema creation, migrations, the
atomic SKIP LOCKED claim, JSON steps_log read-modify-write, metrics, and the
stale-job requeue.

Skipped automatically if pgserver is unavailable. Also runs against an external
server if TEST_DATABASE_URL is set (no pgserver needed in that case).
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

_tmp = Path(__file__).resolve().parent / "_pg_test_storage"
os.environ["TRIAL_STORAGE_DIR"] = str(_tmp)


def _load_db_with_url(url: str):
    """Import db.py fresh with DATABASE_URL pointing at the given Postgres URL."""
    os.environ["DATABASE_URL"] = url
    for name in ("db", "deployconfig"):
        if name in sys.modules:
            del sys.modules[name]
    import db  # noqa: PLC0415

    importlib.reload(db)
    return db


@pytest.fixture(scope="module")
def pg_url():
    external = os.environ.get("TEST_DATABASE_URL")
    if external:
        yield external
        return

    pgserver = pytest.importorskip("pgserver")
    data_dir = _tmp / "pgdata"
    data_dir.mkdir(parents=True, exist_ok=True)
    server = pgserver.get_server(str(data_dir))
    try:
        # pgserver hands out a postgresql://.../postgres URI with a socket/port.
        yield server.get_uri()
    finally:
        server.cleanup()


@pytest.fixture()
def db(pg_url):
    module = _load_db_with_url(pg_url)
    # Clean slate for each test.
    with module._connect() as conn:  # noqa: SLF001 - test needs raw access
        conn.execute("DROP TABLE IF EXISTS jobs")
    module.init_db()
    yield module
    module.close_pool()
    os.environ.pop("DATABASE_URL", None)
    # Restore the shared db module to its default SQLite state so later test
    # modules in this process are not left pointing at Postgres.
    for name in ("db", "deployconfig"):
        sys.modules.pop(name, None)
    import db as _db  # noqa: PLC0415

    importlib.reload(_db)


def test_backend_is_postgres(db):
    assert db.BACKEND == "postgres"


def test_create_and_get(db):
    job = db.create_job("HB123.pdf", "NC")
    assert job["status"] == db.STATUS_PREPARING
    assert job["filename"] == "HB123.pdf"
    fetched = db.get_job(job["id"])
    assert fetched["id"] == job["id"]
    assert fetched["approved"] is False
    assert fetched["steps_log"] == []


def test_claim_is_atomic(db):
    a = db.create_job("a.pdf", "NC")
    b = db.create_job("b.pdf", "NC")
    db.set_status(a["id"], db.STATUS_QUEUED)
    db.set_status(b["id"], db.STATUS_QUEUED)

    first = db.claim_next_job("w1")
    second = db.claim_next_job("w2")
    third = db.claim_next_job("w3")

    claimed_ids = {first["id"], second["id"]}
    assert claimed_ids == {a["id"], b["id"]}  # oldest-first, each claimed once
    assert first["status"] == db.STATUS_PROCESSING
    assert third is None  # queue drained


def test_steps_log_roundtrip(db):
    job = db.create_job("c.pdf", None)
    db.log_step(job["id"], "convert", "running", "started")
    db.append_logs(job["id"], [
        {"step": "convert", "status": "running", "message": "mid"},
        {"step": "save", "status": "done", "message": "ok"},
    ])
    fetched = db.get_job(job["id"])
    steps = fetched["steps_log"]
    assert [s["step"] for s in steps] == ["convert", "convert", "save"]
    assert fetched["current_step"] == "convert"  # append_logs keeps prior step


def test_metrics_and_done(db):
    job = db.create_job("d.pdf", "NC")
    db.set_metrics(job["id"], conversion_secs=1.25, metrics={"content_accuracy_pct": 99.5})
    db.mark_done(job["id"], "/tmp/out.txt")
    fetched = db.get_job(job["id"])
    assert fetched["status"] == db.STATUS_DONE
    assert fetched["output_path"] == "/tmp/out.txt"
    assert fetched["conversion_secs"] == pytest.approx(1.25)
    assert fetched["metrics"]["content_accuracy_pct"] == pytest.approx(99.5)
    assert fetched["has_output"] is True


def test_pending_same_state_and_queue_depth(db):
    a = db.create_job("a.pdf", "NC")
    b = db.create_job("b.pdf", "NC")
    db.set_status(a["id"], db.STATUS_QUEUED)
    db.set_status(b["id"], db.STATUS_QUEUED)
    assert db.pending_same_state(a["id"], "NC") == 1
    depth = db.queue_depth()
    assert depth.get("queued") == 2


def test_requeue_stale_jobs(db):
    job = db.create_job("e.pdf", "NC")
    db.set_status(job["id"], db.STATUS_QUEUED)
    claimed = db.claim_next_job("w1")
    assert claimed["status"] == db.STATUS_PROCESSING
    # Age the heartbeat far past the staleness window.
    with db._connect() as conn:  # noqa: SLF001
        conn.execute(
            "UPDATE jobs SET heartbeat_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", job["id"]),
        )
    n = db.requeue_stale_jobs(max_age_seconds=1)
    assert n == 1
    assert db.get_job(job["id"])["status"] == db.STATUS_QUEUED


def test_migration_adds_columns(db):
    # Extra columns must all be present after init_db ran migrations.
    with db._connect() as conn:  # noqa: SLF001
        cols = db._existing_columns(conn)  # noqa: SLF001
    for extra in db._EXTRA_COLUMNS:  # noqa: SLF001
        assert extra in cols
