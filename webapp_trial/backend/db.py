"""
Durable job store with two interchangeable backends.

Replaces the in-memory ``JOBS`` dict in processing.py. Jobs survive process
restarts and can be shared across multiple API workers and converter processes.

Backend selection (see deployconfig.py):
    * DATABASE_URL unset               -> SQLite (default; local dev and tests).
      WAL mode plus ``BEGIN IMMEDIATE`` claiming lets several processes poll the
      same database file without two of them picking up the same job.
    * DATABASE_URL = postgres[ql]://.. -> PostgreSQL via psycopg, required for
      Plexus where pods are ephemeral/replicated. Atomic claiming uses
      ``SELECT ... FOR UPDATE SKIP LOCKED`` so many workers never collide.

Every public function keeps the same signature and return shape for both
backends, so callers (api.py, worker.py, validation.py, ...) are unchanged.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import deployconfig
from appconfig import settings

STORAGE_DIR = settings.TRIAL_STORAGE_DIR
JOBS_DIR = STORAGE_DIR / "jobs"
APPROVED_DIR = STORAGE_DIR / "approved"
DB_PATH = settings.TRIAL_DB_PATH

BACKEND = deployconfig.db_backend()  # "sqlite" | "postgres"
_IS_PG = BACKEND == "postgres"

# psycopg connection pool (Postgres only), built lazily.
_pg_pool = None
_pg_pool_lock = threading.Lock()


def _normalise_pg_url(url: str) -> str:
    """psycopg accepts postgresql://; also accept the common postgres:// form."""
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url[len("postgresql+psycopg://"):]
    return url


def _get_pool():
    global _pg_pool
    if _pg_pool is None:
        with _pg_pool_lock:
            if _pg_pool is None:
                from psycopg_pool import ConnectionPool

                _pg_pool = ConnectionPool(
                    _normalise_pg_url(deployconfig.database_url()),
                    min_size=1,
                    max_size=int(os.environ.get("DATABASE_POOL_MAX", "10")),
                    kwargs={"autocommit": True},
                    open=True,
                )
    return _pg_pool


def close_pool() -> None:
    """Close the Postgres connection pool, if one was opened. Safe to call on
    shutdown or between test reloads; a no-op for the SQLite backend."""
    global _pg_pool
    with _pg_pool_lock:
        if _pg_pool is not None:
            try:
                _pg_pool.close()
            finally:
                _pg_pool = None


class _Conn:
    """Thin DB-API wrapper so the same hand-written SQL runs on both backends.

    * Translates ``?`` placeholders to ``%s`` for psycopg.
    * Yields dict-like rows from both backends (sqlite3.Row / psycopg dict_row).
    * ``with _connect() as conn:`` commits on success, rolls back on error, and
      always releases the connection (returns it to the pool for Postgres).
    """

    def __init__(self) -> None:
        self.backend = BACKEND
        if _IS_PG:
            from psycopg.rows import dict_row

            self._raw = _get_pool().getconn()
            self._raw.row_factory = dict_row
        else:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                DB_PATH, timeout=settings.DB_TIMEOUT_S, isolation_level=None
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(f"PRAGMA busy_timeout={int(settings.DB_TIMEOUT_S * 1000)}")
            conn.execute("PRAGMA foreign_keys=ON")
            self._raw = conn

    def execute(self, sql: str, params: tuple = ()):  # noqa: ANN401
        if _IS_PG:
            sql = sql.replace("?", "%s")
            cur = self._raw.cursor()
            cur.execute(sql, params)
            return cur
        return self._raw.execute(sql, params)

    def begin_immediate(self) -> None:
        if _IS_PG:
            # Switch out of autocommit for an explicit transaction block.
            self._raw.autocommit = False
        else:
            self._raw.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self._raw.commit()
        if _IS_PG:
            self._raw.autocommit = True

    def rollback(self) -> None:
        self._raw.rollback()
        if _IS_PG:
            self._raw.autocommit = True

    def close(self) -> None:
        if _IS_PG:
            _get_pool().putconn(self._raw)
        else:
            self._raw.close()

    def __enter__(self) -> "_Conn":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is not None:
                if _IS_PG:
                    self._raw.rollback()
            else:
                # SQLite runs in autocommit (isolation_level=None); nothing to
                # flush for the simple one-shot statements used with `with`.
                pass
        finally:
            self.close()

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

_SCHEMA_STATEMENTS = [
    """
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
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jobs_status  ON jobs(status)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at)",
]

# Columns added after the first release. Applied by _migrate() so existing
# databases keep working without a manual rebuild. REAL is mapped to DOUBLE
# PRECISION on Postgres (see _column_decl).
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


def _column_decl(decl: str) -> str:
    """Translate SQLite column types to their Postgres equivalents."""
    if _IS_PG:
        return decl.replace("REAL", "DOUBLE PRECISION")
    return decl


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> _Conn:
    return _Conn()


def _existing_columns(conn: _Conn) -> set[str]:
    if _IS_PG:
        rows = conn.execute(
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_name = 'jobs'"
        ).fetchall()
        return {row["name"] for row in rows}
    return {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}


def _migrate(conn: _Conn) -> None:
    existing = _existing_columns(conn)
    for column, decl in _EXTRA_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {_column_decl(decl)}")


def init_db() -> None:
    for d in (STORAGE_DIR, JOBS_DIR, APPROVED_DIR):
        d.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.begin_immediate()
        try:
            for statement in _SCHEMA_STATEMENTS:
                conn.execute(statement)
            _migrate(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def safe_filename(filename: str) -> str:
    """Strip directory components from a client-supplied filename."""
    name = Path(filename).name
    if not name or name in (".", ".."):
        return "upload.pdf"
    return name


def _row_to_dict(row: Any) -> dict[str, Any]:
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
                "SELECT COUNT(*) AS n FROM jobs WHERE id != ? AND status = 'queued' AND (state_code = ? OR state_code IS NULL)",
                (job_id, state_code)).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE id != ? AND status = 'queued'", (job_id,)).fetchone()
    return int(row["n"])


def claim_next_job(worker_id: str) -> Optional[dict[str, Any]]:
    """Atomically take the oldest queued job. Returns None if none available."""
    now = _utcnow()
    if _IS_PG:
        # One statement: SKIP LOCKED lets concurrent workers each grab a
        # different row without blocking, which beats a global write lock.
        with _connect() as conn:
            row = conn.execute(
                """UPDATE jobs
                      SET status = ?, worker_id = ?, started_at = ?, heartbeat_at = ?,
                          attempts = attempts + 1, error = NULL
                    WHERE id = (
                        SELECT id FROM jobs WHERE status = ?
                        ORDER BY created_at
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    RETURNING id""",
                (STATUS_PROCESSING, worker_id, now, now, STATUS_QUEUED),
            ).fetchone()
        if row is None:
            return None
        return get_job(row["id"])

    conn = _connect()
    try:
        # SQLite: IMMEDIATE takes the write lock up front so two workers cannot
        # read the same candidate row before either of them updates it.
        conn.begin_immediate()
        row = conn.execute(
            "SELECT id FROM jobs WHERE status = ? ORDER BY created_at LIMIT 1",
            (STATUS_QUEUED,),
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        job_id = row["id"]
        conn.execute(
            """UPDATE jobs
                  SET status = ?, worker_id = ?, started_at = ?, heartbeat_at = ?,
                      attempts = attempts + 1, error = NULL
                WHERE id = ?""",
            (STATUS_PROCESSING, worker_id, now, now, job_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return get_job(job_id)


def heartbeat(job_id: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE jobs SET heartbeat_at = ? WHERE id = ?", (_utcnow(), job_id))


def _append_steps(job_id: str, entries: list[dict[str, Any]], current_step: Optional[str]) -> None:
    """Read-modify-write the steps_log JSON under a row lock on both backends."""
    conn = _connect()
    try:
        conn.begin_immediate()
        # Postgres locks the row with FOR UPDATE; SQLite relies on the IMMEDIATE
        # write lock taken above.
        select_sql = "SELECT steps_log FROM jobs WHERE id = ?"
        if _IS_PG:
            select_sql += " FOR UPDATE"
        row = conn.execute(select_sql, (job_id,)).fetchone()
        if row is None:
            conn.commit()
            return
        steps = json.loads(row["steps_log"] or "[]")
        steps.extend(entries)
        if current_step is not None:
            conn.execute(
                "UPDATE jobs SET steps_log = ?, current_step = ?, heartbeat_at = ? WHERE id = ?",
                (json.dumps(steps), current_step, _utcnow(), job_id),
            )
        else:
            conn.execute(
                "UPDATE jobs SET steps_log = ?, heartbeat_at = ? WHERE id = ?",
                (json.dumps(steps), _utcnow(), job_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def log_step(job_id: str, step: str, status: str, message: str = "") -> None:
    entry = {"step": step, "status": status, "message": message, "at": _utcnow()}
    _append_steps(job_id, [entry], current_step=step)


def append_logs(job_id: str, entries: list[dict[str, Any]]) -> None:
    """Append several log entries in one read-modify-write."""
    if not entries:
        return
    _append_steps(job_id, entries, current_step=None)


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
