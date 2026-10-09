"""
Standalone conversion worker.

Polls the SQLite job store for queued work. Run as many instances as your Claude
API rate limit allows — claiming is atomic, so workers never collide.

    python worker.py                 # one worker
    python worker.py --poll 2.0      # custom poll interval
    python worker.py --once          # drain the queue then exit (useful in tests)
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import sys
import time
import uuid
from pathlib import Path

import analysis
from appconfig import settings
import conversion
import db
import joblog
import validation

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [worker] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("trial.worker")

_shutdown = False


def _handle_signal(signum, _frame):
    global _shutdown
    log.info("Signal %s received — finishing current job then exiting", signum)
    _shutdown = True


def process_job(job: dict, worker_id: str) -> None:
    job_id = job["id"]
    log.info("Claimed job %s (%s)", job_id, job["filename"])

    current_step = {"name": "convert"}

    def on_step(step: str, status: str, message: str = "") -> None:
        current_step["name"] = step
        # "running" with no message adds a blank row to the UI; the detailed
        # module logs already show what each phase is doing.
        if status == "running" and not message:
            db.set_current_step(job_id, step)
            return
        db.log_step(job_id, step, status, message)
        if status == "running":
            log.info("  %s: %s", step, message)

    with joblog.capture_logs(job_id, lambda: current_step["name"]):
        _convert_and_validate(job, job_id, on_step)


def _llm():
    """app/llm_models.py — the cache decision and cost ledger live there (app/ is not on sys.path by default)."""
    try:
        import llm_models
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app"))
        import llm_models
    return llm_models


def _convert_and_validate(job: dict, job_id: str, on_step) -> None:
    conversion_start = time.monotonic()
    try:
        routed = job.get("mode")
        if not routed:
            # Should not happen now that jobs are released only after routing,
            # but deriving beats silently converting in the wrong mode.
            info = analysis.analyse(job["pdf_path"], job["state_code"])
            db.set_analysis(job_id, info)
            routed = info["mode"]
            on_step("convert", "running", f"Mode was unset — derived as {routed}")
        if routed not in conversion.SUPPORTED_MODES:
            on_step("convert", "running", f"Unknown mode '{routed}' — using hybrid")
            routed = conversion.MODE_HYBRID

        llm = _llm()
        more = db.pending_same_state(job_id, job["state_code"])
        with llm.cache_hint(more > 0):
            wanted, why = llm.cache_decision()
            on_step("convert", "running", f"Prompt cache {'on' if wanted else 'off'} — {why}"
                    + (f" ({more} queued)" if more else ""))
            result = conversion.convert_pdf(
                pdf_path=job["pdf_path"],
                filename=job["filename"],
                state_code=job["state_code"],
                output_dir=Path(job["pdf_path"]).parent,
                on_step=on_step,
                mode=routed,
            )
        conversion_secs = time.monotonic() - conversion_start

        db.set_state_code(job_id, result["state_code"])
        output_path = Path(result["output_path"])
        db.set_metrics(
            job_id,
            conversion_secs=round(conversion_secs, 2),
            output_bytes=output_path.stat().st_size if output_path.exists() else None,
        )

        _run_validation(job_id, result, job, on_step)

        db.mark_done(job_id, result["output_path"])
        usage = result.get("usage") or {}
        log.info(
            "Job %s done — %s chars in %.1fs (%s) cost $%.4f",
            job_id, f"{result['char_count']:,}", conversion_secs, routed,
            usage.get("cost_usd", 0.0),
        )
    except Exception as exc:  # noqa: BLE001 — any failure must be recorded, not raised
        log.exception("Job %s failed", job_id)
        db.log_step(job_id, job.get("current_step") or "convert", "error", str(exc))
        db.mark_error(job_id, str(exc))


def _run_validation(job_id: str, result: dict, job: dict, on_step) -> None:
    """Score the output so the UI has metrics without a second round trip.

    Validation failure must not fail the job — the conversion output is still
    valid and reviewable without scores.
    """
    on_step("validate", "running", "Scoring accuracy and tag integrity")
    started = time.monotonic()
    try:
        report = validation.build_report(
            result["output_path"], job["pdf_path"], result["state_code"]
        )
        metrics = {
            "content_accuracy_pct": report.get("content_accuracy_pct"),
            "layout_accuracy_pct": report.get("layout_accuracy_pct"),
            "integrity_issues": len(report.get("integrity", {}).get("issues", [])),
            "flagged_issues": len(report.get("issues", [])),
            "validation_key": report.get("key"),
            "usage": result.get("usage") or {},
        }
        db.set_metrics(
            job_id,
            validation_secs=round(time.monotonic() - started, 2),
            metrics=metrics,
        )
        acc = metrics["content_accuracy_pct"]
        on_step(
            "validate", "done",
            f"Accuracy {acc:.2f}%" if acc is not None else "No baseline available",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Validation failed for %s: %s", job_id, exc)
        on_step("validate", "error", str(exc))


def run(poll_interval: float, once: bool) -> None:
    worker_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    db.init_db()
    log.info("Worker %s started (poll=%.1fs)", worker_id, poll_interval)

    requeued = db.requeue_stale_jobs()
    if requeued:
        log.warning("Requeued %d stale job(s) from dead workers", requeued)

    idle_logged = False
    while not _shutdown:
        job = db.claim_next_job(worker_id)
        if job is None:
            if once:
                log.info("Queue empty — exiting (--once)")
                return
            if not idle_logged:
                log.info("Queue empty — waiting")
                idle_logged = True
            time.sleep(poll_interval)
            continue

        idle_logged = False
        process_job(job, worker_id)

    log.info("Worker %s stopped", worker_id)


def main() -> int:
    parser = argparse.ArgumentParser(description="Trial conversion worker")
    parser.add_argument("--poll", type=float, default=settings.WORKER_POLL_SECONDS,
                        help=f"Poll interval in seconds (default {settings.WORKER_POLL_SECONDS}, WORKER_POLL_SECONDS)")
    parser.add_argument("--once", action="store_true", help="Drain the queue then exit")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    run(args.poll, args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
