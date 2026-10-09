"""
Trial API v2 — durable job store, no in-process conversion.

Differences from main.py:
  * Jobs live in SQLite, so they survive restarts and are visible to every worker.
  * Conversion runs in separate worker.py processes, so this app can be run with
    multiple uvicorn workers.

Run:
    uvicorn api:app --reload --port 8800          # single worker (dev)  — API_PORT in .env is where the UI proxy looks
    uvicorn api:app --port 8800 --workers 4       # multi-worker (now safe)

Conversion only happens while at least one worker.py process is running.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from appconfig import settings

import analysis
import compare
import db
import sessions
import tag_audit
import validation

MAX_BATCH_FILES = settings.WEBAPP_MAX_BATCH_FILES

# Built frontend (npm run build), served by this same process in production/service deployments.
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"


class DecisionRequest(BaseModel):
    approve: bool

app = FastAPI(title="Pending Legislation — Trial API v2")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.UI_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


def _require_job(job_id: str) -> dict:
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/api/info")
async def info():
    return {
        "service": "Pending Legislation — Trial API v2",
        "status": "ok",
        "ui": settings.UI_URL,
        "docs": "/docs",
        "queue": db.queue_depth(),
    }


@app.get("/api/health")
async def health():
    return {"status": "ok", "queue": db.queue_depth()}


@app.get("/api/stream/jobs")
async def stream_jobs(request: Request):
    """Server-sent events: one connection replaces per-client polling.

    The server still reads SQLite on an interval, but only pushes when the
    payload actually changes, so idle clients cost nothing.
    """

    async def event_source():
        last_hash = None
        while True:
            if await request.is_disconnected():
                return
            jobs = db.list_jobs(limit=100)
            payload = json.dumps(jobs, default=str)
            digest = hashlib.md5(payload.encode("utf-8")).hexdigest()
            if digest != last_hash:
                last_hash = digest
                yield f"event: jobs\ndata: {payload}\n\n"
            else:
                yield ": keep-alive\n\n"
            await asyncio.sleep(1.5)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/jobs")
async def create_job(file: UploadFile = File(...), state_code: str | None = Form(None)):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")

    job = db.create_job(file.filename, (state_code or "").strip().upper() or None)
    with Path(job["pdf_path"]).open("wb") as fh:
        shutil.copyfileobj(file.file, fh)

    db.log_step(job["id"], "upload", "done", f"Received {file.filename}")
    _analyse_and_route(job["id"], job["pdf_path"], job["state_code"])
    return db.get_job(job["id"])


def _analyse_and_route(job_id: str, pdf_path: str, state_code: str | None) -> dict:
    """Detect PDF type, pick a processing mode, then release the job to workers.

    The job stays in 'preparing' until this completes: a worker that claimed it
    earlier would see mode=NULL and could read a partially written PDF.
    """
    info = analysis.analyse(pdf_path, state_code)
    db.set_analysis(job_id, info)
    db.log_step(
        job_id, "analyse", "done",
        f"{info['display_type']} · {info['page_count'] or '?'} pages · {info['mode_reason']}",
    )
    if info["requires_approval"]:
        db.set_status(job_id, db.STATUS_AWAITING_APPROVAL)
        db.log_step(job_id, "analyse", "running", "Awaiting human approval (scanned PDF)")
    else:
        db.set_status(job_id, db.STATUS_QUEUED)
    return info


@app.post("/api/batches")
async def create_batch(
    files: list[UploadFile] = File(...),
    state_code: str | None = Form(None),
):
    """Upload several PDFs as one batch. Workers process them in parallel."""
    if not files:
        raise HTTPException(status_code=400, detail="No files supplied")
    if len(files) > MAX_BATCH_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {MAX_BATCH_FILES} files per batch; received {len(files)}",
        )

    batch_id = uuid.uuid4().hex[:12]
    created, rejected = [], []
    seen: set[tuple[str, int]] = set()

    for upload in files:
        if not upload.filename or not upload.filename.lower().endswith(".pdf"):
            rejected.append({"filename": upload.filename, "reason": "Not a PDF"})
            continue

        job = db.create_job(upload.filename, (state_code or "").strip().upper() or None,
                            batch_id=batch_id)
        target = Path(job["pdf_path"])
        with target.open("wb") as fh:
            shutil.copyfileobj(upload.file, fh)

        key = (upload.filename, target.stat().st_size)
        if key in seen:
            db.set_status(job["id"], db.STATUS_SKIPPED)
            db.log_step(job["id"], "upload", "error", "Duplicate of another file in this batch")
            rejected.append({"filename": upload.filename, "reason": "Duplicate"})
            continue
        seen.add(key)

        db.log_step(job["id"], "upload", "done", f"Received {upload.filename}")
        _analyse_and_route(job["id"], job["pdf_path"], job["state_code"])
        created.append(db.get_job(job["id"]))

    return {"batch_id": batch_id, "jobs": created, "rejected": rejected}


@app.get("/api/batches/{batch_id}")
async def get_batch(batch_id: str):
    jobs = db.list_jobs(limit=500, batch_id=batch_id)
    if not jobs:
        raise HTTPException(status_code=404, detail="Batch not found")
    done = sum(1 for j in jobs if j["status"] == db.STATUS_DONE)
    failed = sum(1 for j in jobs if j["status"] == db.STATUS_ERROR)
    return {
        "batch_id": batch_id,
        "total": len(jobs),
        "completed": done,
        "failed": failed,
        "jobs": jobs,
    }


@app.post("/api/jobs/{job_id}/decision")
async def job_decision(job_id: str, req: DecisionRequest):
    """Resolve the scanned-PDF approval gate."""
    job = _require_job(job_id)
    if job["status"] != db.STATUS_AWAITING_APPROVAL:
        raise HTTPException(status_code=409, detail="Job is not awaiting approval")

    if req.approve:
        db.set_status(job_id, db.STATUS_QUEUED)
        db.log_step(job_id, "analyse", "done", "Approved by reviewer — queued for conversion")
        return {"approved": True}

    db.set_status(job_id, db.STATUS_SKIPPED)
    db.log_step(job_id, "analyse", "done", "Skipped by reviewer")
    return {"approved": False}


def _output_present(job: dict) -> bool:
    path = job.get("output_path")
    return bool(path) and Path(path).exists()


@app.get("/api/jobs")
async def list_jobs(limit: int = 100, batch_id: str | None = None):
    jobs = db.list_jobs(limit=min(max(limit, 1), 500), batch_id=batch_id)
    # A finished job whose converted file is gone (another machine, cleared storage) has nothing to review.
    return [j for j in jobs if not (j["status"] == "done" and not _output_present(j))]


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    return _require_job(job_id)


@app.get("/api/jobs/{job_id}/pdf")
async def get_job_pdf(job_id: str):
    job = _require_job(job_id)
    pdf_path = Path(job["pdf_path"])
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="PDF not found")
    return FileResponse(pdf_path, media_type="application/pdf")


@app.get("/api/jobs/{job_id}/text", response_class=PlainTextResponse)
async def get_job_text(job_id: str):
    job = _require_job(job_id)
    if not job["output_path"] or not Path(job["output_path"]).exists():
        raise HTTPException(status_code=404, detail="Output not ready yet")
    return Path(job["output_path"]).read_text(encoding="utf-8")


@app.post("/api/jobs/{job_id}/approve")
async def approve_job(job_id: str):
    """Trial-safe approval: copies the output into storage/approved/ only.

    Deliberately does NOT upload to the production /ftp/keyed SFTP location.
    """
    job = _require_job(job_id)
    if job["status"] != db.STATUS_DONE or not job["output_path"]:
        raise HTTPException(status_code=400, detail="Job is not ready for approval")

    dest = db.APPROVED_DIR / f"{job_id}_{Path(job['filename']).stem}.txt"
    shutil.copyfile(job["output_path"], dest)
    db.mark_approved(job_id)
    return {"approved": True, "saved_to": str(dest)}


# ── Pipeline sessions (read-only view of SFTP pipeline output) ────────────────

@app.get("/api/sessions")
async def list_sessions():
    return sessions.list_sessions()


@app.get("/api/sessions/{session_id}/files")
async def list_session_files(session_id: str):
    files = sessions.list_files(session_id)
    if files is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return files


def _safe_pipeline_file(path: str) -> Path:
    try:
        return sessions.resolve_within_processing(path)
    except PermissionError:
        raise HTTPException(status_code=403, detail="Path outside pipeline directory")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")


@app.get("/api/sessions/file/text", response_class=PlainTextResponse)
async def get_session_file_text(path: str):
    return _safe_pipeline_file(path).read_text(encoding="utf-8")


@app.get("/api/sessions/file/pdf")
async def get_session_file_pdf(path: str):
    return FileResponse(_safe_pipeline_file(path), media_type="application/pdf")


# ── Validation review ────────────────────────────────────────────────────────

class FixRequest(BaseModel):
    key: str
    original: str
    replacement: str


class KeyRequest(BaseModel):
    key: str


class SaveRequest(BaseModel):
    key: str
    text: str


@app.get("/api/validation")
async def get_validation(text_path: str, pdf_path: str | None = None, state_code: str | None = None):
    try:
        return validation.build_report(text_path, pdf_path, state_code)
    except PermissionError:
        raise HTTPException(status_code=403, detail="Path outside permitted directories")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")


@app.get("/api/validation/text", response_class=PlainTextResponse)
async def get_working_text(key: str):
    """Current working-copy text, including any applied fixes."""
    try:
        meta = json.loads((validation.EDITS_DIR / key / "meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise HTTPException(status_code=404, detail="Unknown edit session")
    return (validation.EDITS_DIR / key / meta["name"]).read_text(encoding="utf-8")


@app.post("/api/validation/fix")
async def post_fix(req: FixRequest):
    try:
        return validation.apply_fix(req.key, req.original, req.replacement)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown edit session")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/api/validation/undo")
async def post_undo(req: KeyRequest):
    try:
        return validation.undo_last(req.key)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown edit session")


@app.post("/api/validation/save")
async def post_save(req: SaveRequest):
    try:
        return validation.save_text(req.key, req.text)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown edit session")


@app.post("/api/validation/reset")
async def post_reset(req: KeyRequest):
    try:
        return validation.reset(req.key)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown edit session")


# ── Approve and submit to Keyed ──────────────────────────────────────────────

class ApproveRequest(BaseModel):
    key: str
    job_id: str | None = None


@app.get("/api/validation/approval")
async def get_approval(key: str):
    try:
        return validation.approval_status(key)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown edit session")


@app.post("/api/validation/approve")
async def post_approve(req: ApproveRequest):
    """Snapshot the current (saved) text as the approved version."""
    try:
        return validation.approve(req.key, req.job_id)
    except (FileNotFoundError, KeyError):
        raise HTTPException(status_code=404, detail="Unknown edit session")


@app.get("/api/validation/download")
async def get_download(key: str):
    """The current working copy (with every edit applied) as a .txt download."""
    try:
        work = validation._working_path(key)
    except (OSError, KeyError, json.JSONDecodeError):
        raise HTTPException(status_code=404, detail="Unknown edit session")
    return FileResponse(work, filename=work.name, media_type="text/plain; charset=utf-8")


@app.get("/api/llm/config")
async def get_llm_config():
    """Which model conversions use, what it falls back to, and what it costs (USD per 1M tokens)."""
    import llm_models

    return {
        "primary": llm_models.PRIMARY_MODEL,
        "fallback": llm_models.FALLBACK_MODEL or None,
        "pricing": llm_models.pricing_for(llm_models.PRIMARY_MODEL),
    }


@app.get("/api/submit/config")
async def get_submit_config():
    """Where Submit to Keyed would send the file (never includes the password)."""
    import submit

    return submit.config()


@app.post("/api/validation/submit")
async def post_submit(req: KeyRequest):
    """Upload the approved version to the Keyed SFTP folder. Blocked until approved."""
    import submit

    try:
        snapshot = validation.approved_snapshot(req.key)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown edit session")
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    try:
        result = submit.submit_file(snapshot, validation.original_name(req.key))
    except submit.SubmitError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    status = validation.mark_submitted(req.key, f"{result['host']}:{result['remote_path']}")
    return {**status, **result}


# ── Tag audit (Claude — costs tokens, opt-in only) ──────────────────

class AuditRequest(BaseModel):
    text_path: str
    pdf_path: str
    state_code: str | None = None


@app.get("/api/tag-audit")
async def get_cached_tag_audit(text_path: str, pdf_path: str):
    """Cached audit result, or null if it has never been run for this pair."""
    return tag_audit.cached_result(text_path, pdf_path)


@app.post("/api/tag-audit")
async def post_tag_audit(req: AuditRequest):
    try:
        return tag_audit.run_audit(req.text_path, req.pdf_path, req.state_code)
    except Exception as exc:  # noqa: BLE001 — surface the real reason to the UI
        raise HTTPException(status_code=500, detail=str(exc))


# ── Word-level compare viewer ────────────────────────────────────────────────

@app.get("/api/compare")
async def get_compare(text_path: str, pdf_path: str, key: str | None = None):
    """Page geometry plus word-level PDF↔text alignment."""
    try:
        if key:
            meta = json.loads((validation.EDITS_DIR / key / "meta.json").read_text(encoding="utf-8"))
            text = (validation.EDITS_DIR / key / meta["name"]).read_text(encoding="utf-8")
        else:
            text = validation.resolve_readable(text_path).read_text(encoding="utf-8")
        resolved_pdf = validation.resolve_readable(pdf_path)
    except PermissionError:
        raise HTTPException(status_code=403, detail="Path outside permitted directories")
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        raise HTTPException(status_code=404, detail="File not found")

    try:
        return compare.build_compare(str(resolved_pdf), text_path, text)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/validation/locate")
async def get_validation_locate(key: str, text_path: str, pdf_path: str):
    """Every finding pinned to a text range and/or PDF boxes (see locate.py)."""
    import locate

    report, precleaned = validation.load_saved_report(key)
    if report is None:
        raise HTTPException(status_code=404, detail="Run /api/validation first")
    try:
        meta = json.loads((validation.EDITS_DIR / key / "meta.json").read_text(encoding="utf-8"))
        text = (validation.EDITS_DIR / key / meta["name"]).read_text(encoding="utf-8")
        resolved_pdf = validation.resolve_readable(pdf_path)
    except PermissionError:
        raise HTTPException(status_code=403, detail="Path outside permitted directories")
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        raise HTTPException(status_code=404, detail="File not found")
    try:
        return locate.locate_all(text, str(resolved_pdf), text_path, report, precleaned,
                                 report.get("state_code"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/compare/page")
async def get_compare_page(key: str, page: int):
    image = compare.page_image(key, page)
    if image is None:
        raise HTTPException(status_code=404, detail="Page image not found")
    return FileResponse(image, media_type="image/png")


# ── Artifacts ───────────────────────────────────────────────────────────────

@app.get("/api/jobs/{job_id}/artifacts")
async def list_artifacts(job_id: str):
    """Diagnostic files the conversion wrote next to the output."""
    job = _require_job(job_id)
    job_dir = Path(job["pdf_path"]).parent
    if not job_dir.is_dir():
        return []
    return [
        {"name": f.name, "size_bytes": f.stat().st_size, "path": str(f)}
        for f in sorted(job_dir.iterdir())
        if f.is_file()
    ]


@app.get("/api/jobs/{job_id}/artifacts/{name}")
async def download_artifact(job_id: str, name: str):
    job = _require_job(job_id)
    job_dir = Path(job["pdf_path"]).parent.resolve()
    target = (job_dir / Path(name).name).resolve()
    try:
        target.relative_to(job_dir)
    except ValueError:
        raise HTTPException(status_code=403, detail="Invalid artifact path")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found")
    return FileResponse(target, filename=target.name, media_type="application/octet-stream")


# Mounted last so it never shadows the /api/* routes above; falls back to index.html
# for client-side routing (React Router / unknown paths) when the build exists.
if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
