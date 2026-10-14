"""
SFTP downloads for the React app: the counterpart of Streamlit's "Downloaded Files" tab.

  1. check_sftp()  - fetch any new ZIPs from the SFTP server into the downloads folder (DOWNLOAD_DEST_DIR)
  2. list_zips()   - what is in that folder, and which ZIPs were already queued
  3. queue_zips()  - unpack the PDFs of the chosen ZIPs and queue each one as a normal job

One deliberate difference from Streamlit: Streamlit converts and uploads to Keyed in one go. Here the PDFs become
jobs, so each file is reviewed and approved first, and only approved files are submitted.
"""

from __future__ import annotations

import logging
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, Callable

import db
from appconfig import settings

APP_DIR = Path(__file__).resolve().parent.parent.parent / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

log = logging.getLogger("trial.downloads")


class SftpError(RuntimeError):
    """The SFTP step failed in a way the reviewer can act on (no password, server unreachable, ...)."""


def downloads_dir() -> Path:
    d = Path(settings.DOWNLOAD_DEST_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pipeline():
    import pipeline_runner  # app/pipeline_runner.py: the same download/ZIP-naming logic Streamlit uses
    return pipeline_runner


def list_zips() -> list[dict[str, Any]]:
    pr = _pipeline()
    done = set(pr.load_processed_log().get("converted", []))
    rows = []
    for z in sorted(downloads_dir().glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True):
        st = z.stat()
        rows.append({
            "name": z.name,
            "size_kb": max(1, st.st_size // 1024),
            "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
            "state": pr.detect_state_from_zip(z.name),
            "queued": z.name in done,
        })
    return rows


def check_sftp() -> dict[str, Any]:
    if not settings.FTP_PASSWORD:
        raise SftpError("FTP_PASSWORD is not set in the project .env, so the SFTP server cannot be reached.")
    try:
        new = _pipeline().download_new_files()
    except Exception as exc:  # noqa: BLE001 - paramiko/network errors: tell the reviewer, do not crash
        raise SftpError(f"Could not download from the SFTP server: {exc}") from exc
    return {"downloaded": [p.name for p in new], "total": len(list_zips())}


def _pdfs_in(zip_path: Path) -> list[str]:
    with zipfile.ZipFile(zip_path) as zf:
        return [n for n in zf.namelist() if n.lower().endswith(".pdf") and not n.endswith("/")]


def queue_zips(names: list[str], analyse: Callable[[str, str, str | None], Any], batch_id: str) -> dict[str, Any]:
    """Unpack the PDFs of the chosen ZIPs and queue each as a job in one batch. `analyse` routes a job to the workers."""
    pr = _pipeline()
    folder = downloads_dir().resolve()
    log_data = pr.load_processed_log()
    already = set(log_data.get("converted", []))

    plan: list[tuple[Path, str, list[str]]] = []
    skipped: list[dict[str, str]] = []
    for raw in names:
        name = Path(raw).name                      # never a path: a ZIP must be directly in the downloads folder
        path = (folder / name).resolve()
        if path.parent != folder or not path.is_file():
            skipped.append({"zip": name, "reason": "not found in the downloads folder"})
        elif name in already:
            skipped.append({"zip": name, "reason": "already queued earlier"})
        elif not pr.detect_state_from_zip(name):
            skipped.append({"zip": name, "reason": "the state cannot be read from the file name (expected e.g. NC_..._keying.zip)"})
        else:
            try:
                pdfs = _pdfs_in(path)
            except zipfile.BadZipFile:
                skipped.append({"zip": name, "reason": "not a valid ZIP file"})
                continue
            if not pdfs:
                skipped.append({"zip": name, "reason": "no PDFs inside"})
                continue
            plan.append((path, pr.detect_state_from_zip(name), pdfs))

    total = sum(len(p[2]) for p in plan)
    if total > settings.WEBAPP_MAX_BATCH_FILES:
        raise ValueError(f"{total} PDFs selected; the limit is {settings.WEBAPP_MAX_BATCH_FILES} per batch. Select fewer ZIPs.")

    created: list[dict[str, Any]] = []
    for path, state, pdfs in plan:
        seen: set[tuple[str, int]] = set()
        with zipfile.ZipFile(path) as zf:
            for member in pdfs:
                data = zf.read(member)
                filename = Path(member).name
                if (filename, len(data)) in seen:
                    continue                       # the same file twice inside one ZIP
                seen.add((filename, len(data)))
                job = db.create_job(filename, state, batch_id=batch_id)
                Path(job["pdf_path"]).write_bytes(data)
                db.log_step(job["id"], "upload", "done", f"Received {filename} from {path.name}")
                analyse(job["id"], job["pdf_path"], job["state_code"])
                created.append(db.get_job(job["id"]))
        already.add(path.name)
        log_data["converted"] = sorted(already)
        pr.save_processed_log(log_data)            # remembered, so the same ZIP is not queued twice
    return {"batch_id": batch_id, "jobs": created, "skipped": skipped}
