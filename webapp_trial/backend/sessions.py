"""
Read-only browser for pipeline sessions produced by the SFTP automation.

Mirrors the folder contract used by app/pipeline_runner.list_pending_sessions():

    tmp/pending_processing/<MMddyyyy>/<SESSION>/input/*.pdf
                                               /output/*.txt

Deliberately re-implemented rather than importing app/pipeline_runner.py, because
importing that module runs load_dotenv(), logging.basicConfig() and directory
creation at import time, which would reconfigure logging inside worker processes.
If the folder layout there changes, update this file to match.

Everything here is read-only — no file in tmp/ is created, modified or deleted.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from appconfig import settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _tmp_root() -> Path:
    # Read-only module: the plain configured path, never creating folders (settings.tmp_root() would).
    return settings.APP_TMP_DIR


def processing_dir() -> Path:
    return _tmp_root() / "pending_processing"


def _describe(session_dir: Path, date_folder: str = "") -> Optional[dict[str, Any]]:
    output_dir = session_dir / "output"
    input_dir = session_dir / "input"

    txt_files = sorted(output_dir.glob("*.txt")) if output_dir.is_dir() else []
    # Exclude the diagnostic siblings the pipeline writes next to the real output.
    txt_files = [
        f for f in txt_files
        if not any(
            marker in f.name
            for marker in ("_hybrid_raw", "_hybrid_precleaned", "_hybrid_chunks",
                           "_clean_diff", "_pymupdf", "_removals")
        )
    ]
    pdf_files = (
        sorted(input_dir.glob("*.pdf")) if input_dir.is_dir()
        else sorted(session_dir.glob("*.pdf"))
    )

    if not txt_files and not pdf_files:
        return None

    return {
        "session_id": session_dir.name,
        "date_folder": date_folder,
        "state": session_dir.name.split("_")[0] if "_" in session_dir.name else "??",
        "path": str(session_dir),
        "pdf_count": len(pdf_files),
        "converted_count": len(txt_files),
        "created": datetime.fromtimestamp(session_dir.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
    }


def list_sessions() -> list[dict[str, Any]]:
    root = processing_dir()
    if not root.is_dir():
        return []

    sessions: list[dict[str, Any]] = []
    for entry in sorted(root.iterdir(), reverse=True):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        # Date-stamped folder (MMddyyyy) containing session subdirectories.
        if len(entry.name) == 8 and entry.name.isdigit():
            for session_dir in sorted(entry.iterdir(), reverse=True):
                if session_dir.is_dir():
                    info = _describe(session_dir, date_folder=entry.name)
                    if info:
                        sessions.append(info)
        else:
            info = _describe(entry)
            if info:
                sessions.append(info)
    return sessions


def _find_session(session_id: str) -> Optional[Path]:
    for session in list_sessions():
        if session["session_id"] == session_id:
            return Path(session["path"])
    return None


def list_files(session_id: str) -> Optional[list[dict[str, Any]]]:
    session_dir = _find_session(session_id)
    if session_dir is None:
        return None

    output_dir = session_dir / "output"
    input_dir = session_dir / "input"
    files: list[dict[str, Any]] = []

    if output_dir.is_dir():
        for txt in sorted(output_dir.glob("*.txt")):
            if any(
                marker in txt.name
                for marker in ("_hybrid_raw", "_hybrid_precleaned", "_hybrid_chunks",
                               "_clean_diff", "_pymupdf", "_removals")
            ):
                continue
            pdf = None
            if input_dir.is_dir():
                candidate = input_dir / (txt.stem + ".pdf")
                if candidate.exists():
                    pdf = str(candidate)
            files.append({
                "name": txt.name,
                "stem": txt.stem,
                "text_path": str(txt),
                "pdf_path": pdf,
                "size_bytes": txt.stat().st_size,
            })
    return files


def resolve_within_processing(path_str: str) -> Path:
    """Resolve a client-supplied path and reject anything outside pending_processing.

    Without this, a crafted ?path= value could read arbitrary files from disk.
    """
    root = processing_dir().resolve()
    candidate = Path(path_str).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise PermissionError(f"Path is outside the pipeline directory: {path_str}") from exc
    if not candidate.is_file():
        raise FileNotFoundError(path_str)
    return candidate
