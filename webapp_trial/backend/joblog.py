"""
Capture the conversion modules' own logging into a job's activity log.

pymupdf_extractor and Claude_Opus_4.6_Test already log their work in detail
(span counts, char progressions, guard results). The worker previously only
recorded its own coarse steps, so that detail went to the terminal and was lost
to the UI. This handler forwards those records to the job store.

Records are buffered and flushed in batches — the job log is a single JSON
column, so writing per record would mean one read-modify-write per log line.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Optional

import db
from appconfig import settings

# Loggers whose output is useful to a reviewer. Anything else (httpx, anthropic
# wire logs) is noise in the UI.
INCLUDED_PREFIXES = (
    "root",
    "pymupdf_extractor",
    "claude_opus_trial",
    "trial.conversion",
)
EXCLUDED_PREFIXES = ("httpx", "httpcore", "anthropic", "urllib3", "PIL")

FLUSH_EVERY = 25
MAX_ENTRIES = settings.JOBLOG_MAX_ENTRIES


def _level_status(levelno: int) -> str:
    if levelno >= logging.ERROR:
        return "error"
    if levelno >= logging.WARNING:
        return "warning"
    return "info"


class JobLogHandler(logging.Handler):
    """Forwards log records to a job's activity log in batches."""

    def __init__(self, job_id: str, step_provider) -> None:
        super().__init__(level=logging.INFO)
        self._job_id = job_id
        self._step_provider = step_provider
        self._buffer: list[dict[str, Any]] = []
        self._written = 0
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        name = record.name
        if any(name.startswith(p) for p in EXCLUDED_PREFIXES):
            return
        if not any(name == p or name.startswith(p) for p in INCLUDED_PREFIXES):
            return
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — never let logging break conversion
            return
        if not message.strip():
            return

        entry = {
            "step": self._step_provider() or "convert",
            "status": _level_status(record.levelno),
            "message": message.strip(),
            "at": datetime.now(timezone.utc).isoformat(),
            "detail": True,
        }
        with self._lock:
            if self._written >= MAX_ENTRIES:
                return
            self._buffer.append(entry)
            self._written += 1
            should_flush = len(self._buffer) >= FLUSH_EVERY
        if should_flush:
            self.flush()

    def flush(self) -> None:
        with self._lock:
            pending, self._buffer = self._buffer, []
        if pending:
            try:
                db.append_logs(self._job_id, pending)
            except Exception:  # noqa: BLE001
                pass


class capture_logs:
    """Context manager attaching a JobLogHandler to the root logger."""

    def __init__(self, job_id: str, step_provider) -> None:
        self._handler: Optional[JobLogHandler] = None
        self._job_id = job_id
        self._step_provider = step_provider

    def __enter__(self) -> "capture_logs":
        self._handler = JobLogHandler(self._job_id, self._step_provider)
        root = logging.getLogger()
        root.addHandler(self._handler)
        if root.level > logging.INFO:
            root.setLevel(logging.INFO)
        return self

    def __exit__(self, *exc) -> None:
        if self._handler is not None:
            self._handler.flush()
            logging.getLogger().removeHandler(self._handler)
        return None
