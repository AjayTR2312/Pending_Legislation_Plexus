"""
Durable job store backed by SQLite.

Replaces the in-memory ``JOBS`` dict in processing.py. Jobs survive process
restarts and can be shared across multiple API workers and converter processes.

WAL mode plus ``BEGIN IMMEDIATE`` claiming lets several processes poll the same
database without two of them picking up the same job.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from appconfig import settings

STORAGE_DIR = settings.TRIAL_STORAGE_DIR
JOBS_DIR = STORAGE_DIR / "jobs"
APPROVED_DIR = STORAGE_DIR / "approved"
DB_PATH = settings.TRIAL_DB_PATH

STATUS_PREPARING = "preparing"
STATUS_QUEUED = "queued"
STATUS_PROCESSING = "processing"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_AWAITING_APPROVAL = "awaiting_approval"
STATUS_SKIPPED = "skipped"

# Any job left in 'processing' longer than this is assumed to belong to a dead
# worker and is returned to the queue.
STALE_JOB_SECONDS = settings.JOB_STALE_SECONDS

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    filename      TEXT NOT NULL,
    state_code    TEXT,
    status        TEXT NOT NULL,
    current_step  TEXT,
    steps_log     TEXT NOT NULL DEFAULT '[]',
    error         TEXT,
    pdf_path      TEXT NOT NULL,
    output_path   TEXT,
    approved      INTEGER NOT NULL DEFAULT 0,
    worker_id     TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    heartbeat_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status  ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
"""

# Columns added after the first release. Applied by _migrate() so existing
# databases keep working without a manual rebuild.
_EXTRA_COLUMNS = {
    "batch_id": "TEXT",
    "pdf_type": "TEXT",
    "display_type": "TEXT",
    "page_count": "INTEGER",
    "mode": "TEXT",
    "mode_reason": "TEXT",
    "requires_approval": "INTEGER NOT NULL DEFAULT 0",
    "conversion_secs": "REAL",
    "validation_secs": "REAL",
    "output_bytes": "INTEGER",
    "metrics": "TEXT",
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=settings.DB_TIMEOUT_S, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={int(settings.DB_TIMEOUT_S * 1000)}")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    for column, decl in _EXTRA_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {decl}")


def init_db() -> None:
    for d in (STORAGE_DIR, JOBS_DIR, APPROVED_DIR):
        d.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        _migrate(conn)


def safe_filename(filename: str) -> str:
    """Strip directory components from a client-supplied filename."""
    name = Path(filename).name
    if not name or name in (".", ".."):
        return "upload.pdf"
    return name


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["steps_log"] = json.loads(d.get("steps_log") or "[]")
    d["metrics"] = json.loads(d["metrics"]) if d.get("metrics") else None
    d["approved"] = bool(d["approved"])
    d["requires_approval"] = bool(d.get("requires_approval"))
    d["has_output"] = bool(d.get("output_path"))
    return d


def create_job(
    filename: str,
    state_code: Optional[str],
    *,
    batch_id: Optional[str] = None,
    status: str = STATUS_PREPARING,
) -> dict[str, Any]:
    """Create a job. Defaults to 'preparing', which workers never claim, so the
    upload can finish writing and the routing mode can be decided first."""
    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = job_dir / safe_filename(filename)

    with _connect() as conn:
        conn.execute(
            """INSERT INTO jobs (id, filename, state_code, status, pdf_path, created_at, batch_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (job_id, filename, state_code, status, str(pdf_path), _utcnow(), batch_id),
        )
    return get_job(job_id)


def set_analysis(job_id: str, analysis: dict[str, Any]) -> None:
    """Store upload-time PDF facts and the routing decision."""
    with _connect() as conn:
        conn.execute(
            """UPDATE jobs
                  SET pdf_type = ?, display_type = ?, page_count = ?,
                      mode = ?, mode_reason = ?, requires_approval = ?
                WHERE id = ?""",
            (
                analysis.get("pdf_type"),
                analysis.get("display_type"),
                analysis.get("page_count"),
                analysis.get("mode"),
                analysis.get("mode_reason"),
                1 if analysis.get("requires_approval") else 0,
                job_id,
            ),
        )


def set_status(job_id: str, status: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))


def set_current_step(job_id: str, step: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE jobs SET current_step = ?, heartbeat_at = ? WHERE id = ?",
            (step, _utcnow(), job_id),
        )


def set_metrics(
    job_id: str,
    *,
    conversion_secs: Optional[float] = None,
    validation_secs: Optional[float] = None,
    output_bytes: Optional[int] = None,
    metrics: Optional[dict[str, Any]] = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            """UPDATE jobs
                  SET conversion_secs = COALESCE(?, conversion_secs),
                      validation_secs = COALESCE(?, validation_secs),
                      output_bytes    = COALESCE(?, output_bytes),
                      metrics         = COALESCE(?, metrics)
                WHERE id = ?""",
            (
                conversion_secs,
                validation_secs,
                output_bytes,
                json.dumps(metrics) if metrics is not None else None,
                job_id,
            ),
        )


def get_job(job_id: str) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_jobs(limit: int = 100, batch_id: Optional[str] = None) -> list[dict[str, Any]]:
    with _connect() as conn:
        if batch_id:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE batch_id = ? ORDER BY created_at LIMIT ?",
                (batch_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
    return [_row_to_dict(r) for r in rows]


def pending_same_state(job_id: str, state_code: Optional[str]) -> int:
    """Other jobs still waiting for a worker that will use the same state prompt (unknown state counts)."""
    with _connect() as conn:
        if state_code:
            row = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE id != ? AND status = 'queued' AND (state_code = ? OR state_code IS NULL)",
                (job_id, state_code)).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM jobs WHERE id != ? AND status = 'queued'", (job_id,)).fetchone()
    return int(row[0])


def claim_next_job(worker_id: str) -> Optional[dict[str, Any]]:
    """Atomically take the oldest queued job. Returns None if none available."""
    now = _utcnow()
    conn = _connect()
    try:
        # IMMEDIATE takes the write lock up front so two workers cannot read the
        # same candidate row before either of them updates it.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id FROM jobs WHERE status = ? ORDER BY created_at LIMIT 1",
            (STATUS_QUEUED,),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        job_id = row["id"]
        conn.execute(
            """UPDATE jobs
                  SET status = ?, worker_id = ?, started_at = ?, heartbeat_at = ?,
                      attempts = attempts + 1, error = NULL
                WHERE id = ?""",
            (STATUS_PROCESSING, worker_id, now, now, job_id),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return get_job(job_id)


def heartbeat(job_id: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE jobs SET heartbeat_at = ? WHERE id = ?", (_utcnow(), job_id))


def log_step(job_id: str, step: str, status: str, message: str = "") -> None:
    entry = {"step": step, "status": status, "message": message, "at": _utcnow()}
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT steps_log FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return
        steps = json.loads(row["steps_log"] or "[]")
        steps.append(entry)
        conn.execute(
            "UPDATE jobs SET steps_log = ?, current_step = ?, heartbeat_at = ? WHERE id = ?",
            (json.dumps(steps), step, _utcnow(), job_id),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def append_logs(job_id: str, entries: list[dict[str, Any]]) -> None:
    """Append several log entries in one read-modify-write."""
    if not entries:
        return
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT steps_log FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return
        steps = json.loads(row["steps_log"] or "[]")
        steps.extend(entries)
        conn.execute(
            "UPDATE jobs SET steps_log = ?, heartbeat_at = ? WHERE id = ?",
            (json.dumps(steps), _utcnow(), job_id),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def set_state_code(job_id: str, state_code: Optional[str]) -> None:
    with _connect() as conn:
        conn.execute("UPDATE jobs SET state_code = ? WHERE id = ?", (state_code, job_id))


def mark_done(job_id: str, output_path: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE jobs SET status = ?, output_path = ?, finished_at = ? WHERE id = ?",
            (STATUS_DONE, output_path, _utcnow(), job_id),
        )


def mark_error(job_id: str, error: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE jobs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
            (STATUS_ERROR, error, _utcnow(), job_id),
        )


def mark_approved(job_id: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE jobs SET approved = 1 WHERE id = ?", (job_id,))


def requeue_stale_jobs(max_age_seconds: int = STALE_JOB_SECONDS) -> int:
    """Return jobs abandoned by dead workers to the queue. Returns the count."""
    cutoff = datetime.now(timezone.utc).timestamp() - max_age_seconds
    requeued = 0
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, heartbeat_at FROM jobs WHERE status = ?", (STATUS_PROCESSING,)
        ).fetchall()
        for row in rows:
            hb = row["heartbeat_at"]
            if not hb:
                continue
            try:
                hb_ts = datetime.fromisoformat(hb).timestamp()
            except ValueError:
                continue
            if hb_ts < cutoff:
                conn.execute(
                    "UPDATE jobs SET status = ?, worker_id = NULL WHERE id = ?",
                    (STATUS_QUEUED, row["id"]),
                )
                requeued += 1
    return requeued


def queue_depth() -> dict[str, int]:
    with _connect() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}
