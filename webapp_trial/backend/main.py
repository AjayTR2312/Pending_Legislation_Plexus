"""
Trial API server — isolated from the production Streamlit app and pipeline.

Run (after installing requirements-trial.txt into the project .venv):
    uvicorn main:app --reload --port 8800
"""

import shutil
import threading
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse

from appconfig import settings
from processing import APPROVED_DIR, JOBS, JOBS_DIR, STORAGE_DIR, Job, new_job, run_conversion

STORAGE_DIR.mkdir(parents=True, exist_ok=True)
JOBS_DIR.mkdir(parents=True, exist_ok=True)
APPROVED_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Pending Legislation — Trial API")

# Vite dev server default origin. The dev proxy (vite.config.ts) makes this
# unnecessary in practice, but it's kept for when the frontend is served separately.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.UI_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _get_job(job_id: str) -> Job:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/")
async def root():
    return {
        "service": "Pending Legislation — Trial API",
        "status": "ok",
        "ui": settings.UI_URL,
        "docs": "/docs",
        "endpoints": ["/api/jobs", "/api/jobs/{id}", "/api/jobs/{id}/pdf", "/api/jobs/{id}/text", "/api/jobs/{id}/approve"],
    }


@app.post("/api/jobs")
async def create_job(file: UploadFile = File(...), state_code: str | None = Form(None)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")

    job = new_job(file.filename, (state_code or "").strip().upper() or None)
    pdf_path = Path(job.pdf_path)
    with pdf_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    thread = threading.Thread(target=run_conversion, args=(job,), daemon=True)
    thread.start()

    return job.to_dict()


@app.get("/api/jobs")
async def list_jobs():
    return [j.to_dict() for j in sorted(JOBS.values(), key=lambda j: j.created_at, reverse=True)]


@app.get("/api/jobs/{job_id}")
async def get_job_status(job_id: str):
    return _get_job(job_id).to_dict()


@app.get("/api/jobs/{job_id}/pdf")
async def get_job_pdf(job_id: str):
    job = _get_job(job_id)
    if not Path(job.pdf_path).exists():
        raise HTTPException(status_code=404, detail="PDF not found")
    return FileResponse(job.pdf_path, media_type="application/pdf")


@app.get("/api/jobs/{job_id}/text", response_class=PlainTextResponse)
async def get_job_text(job_id: str):
    job = _get_job(job_id)
    if not job.output_path or not Path(job.output_path).exists():
        raise HTTPException(status_code=404, detail="Output not ready yet")
    return Path(job.output_path).read_text(encoding="utf-8")


@app.post("/api/jobs/{job_id}/approve")
async def approve_job(job_id: str):
    """Trial-safe approval: copies the output locally only.

    Intentionally does NOT upload to the production /ftp/keyed SFTP location —
    this is a sandbox for UI evaluation, not a replacement for the real submit flow.
    """
    job = _get_job(job_id)
    if job.status != "done" or not job.output_path:
        raise HTTPException(status_code=400, detail="Job is not ready for approval")

    dest = APPROVED_DIR / f"{job_id}_{Path(job.filename).stem}.txt"
    shutil.copyfile(job.output_path, dest)
    job.approved = True
    return {"approved": True, "saved_to": str(dest)}
