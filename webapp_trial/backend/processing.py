"""
Trial backend — wraps the existing conversion pipeline behind a REST API.

Isolation guarantees:
- Imports app/ and Opus4.6/ modules read-only; nothing in those folders is modified.
- Never touches the Streamlit app, app/pipeline_runner.py, or the real SFTP dirs.
- "Approve" only copies the file into webapp_trial/backend/storage/approved/ —
  it does NOT upload to the production /ftp/keyed SFTP location.
"""

import importlib.util
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
APP_DIR = PROJECT_ROOT / "app"
OPUS_DIR = PROJECT_ROOT / "Opus4.6"
from appconfig import settings

STORAGE_DIR = settings.TRIAL_STORAGE_DIR
JOBS_DIR = STORAGE_DIR / "jobs"
APPROVED_DIR = STORAGE_DIR / "approved"

for p in (APP_DIR, OPUS_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

log = logging.getLogger("trial_backend")

STEP_NAMES = ["upload", "detect_state", "convert", "postprocess", "save"]


def _load_opus_module():
    """Load Claude_Opus_4.6_Test.py the same way pipeline_runner.py does
    (importlib is required because the filename contains dots)."""
    spec = importlib.util.spec_from_file_location(
        "claude_opus_trial", str(OPUS_DIR / "Claude_Opus_4.6_Test.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_state_id_lookup():
    spec = importlib.util.spec_from_file_location(
        "state_id_lookup_trial", str(APP_DIR / "state_id_lookup.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def safe_filename(filename: str) -> str:
    """Strip any directory components from a client-supplied filename."""
    name = Path(filename).name
    if not name or name in (".", ".."):
        return "upload.pdf"
    return name


class Job:
    def __init__(self, job_id: str, filename: str, state_code: Optional[str]):
        self.id = job_id
        self.filename = filename
        self.state_code = state_code
        self.status = "queued"  # queued | processing | done | error
        self.current_step: Optional[str] = None
        self.steps_log: list[dict] = []
        self.error: Optional[str] = None
        # State detection reads the NetScan numeric ID out of the filename, so the
        # original name must be preserved rather than normalised to input.pdf.
        self.pdf_path = str(JOBS_DIR / job_id / safe_filename(filename))
        self.output_path: Optional[str] = None
        self.approved = False
        self.created_at = datetime.now(timezone.utc).isoformat()

    def log_step(self, step: str, status: str, message: str = ""):
        self.current_step = step
        self.steps_log.append({"step": step, "status": status, "message": message})

    def to_dict(self):
        return {
            "id": self.id,
            "filename": self.filename,
            "state_code": self.state_code,
            "status": self.status,
            "current_step": self.current_step,
            "steps_log": self.steps_log,
            "error": self.error,
            "has_output": self.output_path is not None,
            "approved": self.approved,
            "created_at": self.created_at,
        }


# In-memory job store — fine for a single-process trial, not for production scale.
JOBS: dict[str, Job] = {}


def new_job(filename: str, state_code: Optional[str]) -> Job:
    job_id = uuid.uuid4().hex[:12]
    job = Job(job_id, filename, state_code)
    (JOBS_DIR / job_id).mkdir(parents=True, exist_ok=True)
    JOBS[job_id] = job
    return job


def run_conversion(job: Job):
    """Runs the existing hybrid-mode conversion pipeline for one job. Mirrors
    app/pipeline_runner.py's convert_pdf(), reusing the same functions."""
    job.status = "processing"
    try:
        job.log_step("upload", "done", f"Received {job.filename}")

        opus = _load_opus_module()

        job.log_step("detect_state", "running")
        state_code = job.state_code
        if not state_code:
            try:
                id_lookup = _load_state_id_lookup()
                state_code = id_lookup.detect_state_from_numerical_id(job.filename)
            except Exception:
                state_code = None
            if not state_code:
                try:
                    state_code = opus.detect_state_from_filename(job.filename)
                except Exception:
                    state_code = None
        job.state_code = state_code
        job.log_step("detect_state", "done", state_code or "unknown — using default prompt")

        client = opus.get_anthropic_client()
        if not client:
            raise RuntimeError("Anthropic client could not be initialized — check API key configuration")

        state_config = opus.load_state_template(state_code) if state_code else None

        job.log_step("convert", "running", "Running hybrid-mode conversion (PyMuPDF + LLM clean)")
        converted_text = opus.convert_pdf_hybrid_text(client, job.pdf_path, state_config, state_code)
        if not converted_text:
            raise RuntimeError("Conversion returned an empty result")
        job.log_step("convert", "done", f"{len(converted_text):,} characters")

        job.log_step("postprocess", "running")
        converted_text = opus.deterministic_cleanup(converted_text, state_config=state_config, state_code=state_code)
        if state_config:
            converted_text = opus.post_process_output(converted_text, state_config)
        job.log_step("postprocess", "done")

        job.log_step("save", "running")
        output_dir = JOBS_DIR / job.id
        output_path = output_dir / "output.txt"
        opus.save_text_file(converted_text, str(output_path))
        job.output_path = str(output_path)
        job.log_step("save", "done", str(output_path))

        job.status = "done"
    except Exception as exc:  # noqa: BLE001 — surface any failure to the UI
        log.exception("Job %s failed", job.id)
        job.status = "error"
        job.error = str(exc)
        if job.current_step:
            job.log_step(job.current_step, "error", str(exc))
