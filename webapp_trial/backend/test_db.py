"""
Verification for the SQLite job store.

Checks the properties that justify replacing the in-memory dict:
  1. Jobs persist across "restarts" (fresh connections)
  2. Concurrent workers never claim the same job
  3. Stale jobs from dead workers are requeued

Run:  python test_db.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="trial_db_test_"))
os.environ["TRIAL_STORAGE_DIR"] = str(_tmp)
os.environ["TRIAL_DB_PATH"] = str(_tmp / "jobs.db")

import db  # noqa: E402  — imported after env vars so it picks up the temp paths


def _fresh_db(name: str) -> None:
    """Point the store at a dedicated file so tests cannot affect each other."""
    db.DB_PATH = _tmp / f"{name}.db"
    db.init_db()


def test_persistence() -> None:
    _fresh_db("persistence")
    job = db.create_job("2026_2026_9_3_2_007030_0_3_1_20260122_0.pdf", None)
    fetched = db.get_job(job["id"])
    assert fetched is not None, "job should be retrievable from a new connection"
    assert fetched["status"] == db.STATUS_PREPARING
    assert fetched["filename"].endswith(".pdf")
    print("  [OK] job persists and is readable via a fresh connection")


def test_no_double_claim() -> None:
    _fresh_db("double_claim")
    for i in range(20):
        db.create_job(
            f"2026_2026_9_3_2_{i:06d}_0_3_1_20260122_0.pdf", None,
            status=db.STATUS_QUEUED,
        )

    claimed: list[str] = []
    lock = threading.Lock()

    def worker(worker_id: str) -> None:
        while True:
            job = db.claim_next_job(worker_id)
            if job is None:
                return
            with lock:
                claimed.append(job["id"])

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(claimed) == len(set(claimed)), (
        f"duplicate claims detected: {len(claimed)} claims, {len(set(claimed))} unique"
    )
    assert len(claimed) == 20, f"expected all 20 jobs claimed, got {len(claimed)}"
    print(f"  [OK] {len(claimed)} jobs claimed by 8 concurrent workers, zero duplicates")


def test_stale_requeue() -> None:
    _fresh_db("stale")
    job = db.create_job(
        "2025_2026_15_3_2_000657_0_4_1_20250512_0.pdf", None, status=db.STATUS_QUEUED
    )
    claimed = db.claim_next_job("dead-worker")
    assert claimed is not None and claimed["id"] == job["id"]

    assert db.requeue_stale_jobs(max_age_seconds=10_000) == 0, "fresh job must not requeue"
    assert db.requeue_stale_jobs(max_age_seconds=-1) == 1, "stale job should requeue"
    assert db.get_job(job["id"])["status"] == db.STATUS_QUEUED
    print("  [OK] stale jobs from dead workers are returned to the queue")


def test_state_detection_preserved() -> None:
    """The filename must reach the store intact — state detection depends on it."""
    _fresh_db("filenames")
    job = db.create_job("2026_2026_9_3_2_007030_0_3_1_20260122_0.pdf", None)
    assert Path(job["pdf_path"]).name == "2026_2026_9_3_2_007030_0_3_1_20260122_0.pdf"

    traversal = db.create_job("../../../etc/passwd.pdf", None)
    assert Path(traversal["pdf_path"]).name == "passwd.pdf", "path traversal must be stripped"
    print("  [OK] filenames preserved for state detection; path traversal stripped")


def test_preparing_jobs_are_not_claimable() -> None:
    """Regression: a job must not be claimable before routing has completed.

    Jobs used to be inserted as 'queued', so a worker could claim one before the
    PDF finished writing and before the mode was set — converting in the wrong
    mode (e.g. hybrid for a 14-page PDF routed to pymupdf_clean_only).
    """
    _fresh_db("preparing")
    job = db.create_job("2026_2026_9_2_2_000299_0_7_1_20251212_0.pdf", None)
    assert job["status"] == db.STATUS_PREPARING, "new jobs must start as preparing"
    assert db.claim_next_job("w1") is None, "preparing jobs must not be claimable"

    db.set_analysis(job["id"], {
        "pdf_type": "digital", "display_type": "digital", "page_count": 14,
        "mode": "pymupdf_clean_only", "mode_reason": "Standard PDF (14 pages)",
        "requires_approval": False,
    })
    db.set_status(job["id"], db.STATUS_QUEUED)

    claimed = db.claim_next_job("w1")
    assert claimed is not None, "job should be claimable once queued"
    assert claimed["mode"] == "pymupdf_clean_only", (
        f"worker must see the routed mode, got {claimed['mode']!r}"
    )
    print("  [OK] jobs are only claimable after routing, and carry their mode")


def main() -> int:
    print("Job store verification")
    print(f"  temp db: {os.environ['TRIAL_DB_PATH']}")
    for fn in (
        test_persistence,
        test_no_double_claim,
        test_stale_requeue,
        test_state_detection_preserved,
        test_preparing_jobs_are_not_claimable,
    ):
        try:
            fn()
        except AssertionError as exc:
            print(f"  [FAIL] {fn.__name__}: {exc}")
            return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
