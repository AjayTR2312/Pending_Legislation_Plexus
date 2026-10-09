"""
Submit an approved file to the Keyed SFTP landing strip.

Mirrors the Streamlit app's `_submit_to_keyed_sftp` (same server, same remote
folder, same "verify the size landed" check) but is driven by the project `.env`
so the target is visible and configurable:

    SFTP_HOST, SFTP_PORT, SFTP_USER, SFTP_UPLOAD_DIR, FTP_PASSWORD

Settings come from app/settings.py (environment > .env > default). The password is only ever
read from there — it is never returned to the browser or written to a log.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "app"))
import settings  # noqa: E402

LOG_FILE = Path(__file__).resolve().parent / "storage" / "submit.log"


class SubmitError(RuntimeError):
    """A submit failed for a reason the reviewer can act on."""


def config() -> dict[str, Any]:
    """Where a submit would go, and whether it can run. Never includes the password."""
    return {
        "host": settings.SFTP_HOST,
        "port": settings.SFTP_PORT,
        "user": settings.SFTP_USER,
        "remote_dir": settings.SFTP_UPLOAD_DIR.rstrip("/"),
        "password_set": bool(settings.FTP_PASSWORD),
    }


def _log(line: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass  # a logging failure must never fail the submit


def submit_file(local: Path, remote_name: str) -> dict[str, Any]:
    """Upload `local` as `remote_name` into the Keyed folder and verify it landed."""
    cfg = config()
    password = settings.FTP_PASSWORD
    if not password:
        raise SubmitError("FTP_PASSWORD is not set in the project .env — cannot connect.")
    if not local.is_file():
        raise SubmitError(f"Approved file not found: {local.name}")

    name = Path(remote_name).name  # never let a caller choose a path
    remote_path = f"{cfg['remote_dir']}/{name}"

    import paramiko

    try:
        transport = paramiko.Transport((cfg["host"], cfg["port"]))
        transport.connect(username=cfg["user"], password=password)
    except Exception as exc:  # noqa: BLE001 — surface network/auth problems plainly
        raise SubmitError(f"Could not connect to {cfg['host']}:{cfg['port']} — {exc}") from exc

    sftp = None
    try:
        sftp = paramiko.SFTPClient.from_transport(transport)
        current = ""
        for part in cfg["remote_dir"].strip("/").split("/"):
            current += f"/{part}"
            try:
                sftp.stat(current)
            except IOError:
                sftp.mkdir(current)

        sftp.put(str(local), remote_path)

        try:
            remote_size = sftp.stat(remote_path).st_size
        except IOError as exc:
            raise SubmitError(f"Upload not found on the server after put: {remote_path} ({exc})") from exc
        local_size = local.stat().st_size
        if remote_size != local_size:
            raise SubmitError(
                f"Size mismatch after upload: local {local_size} bytes, remote {remote_size} bytes ({remote_path})"
            )
    except SubmitError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SubmitError(f"Upload failed: {exc}") from exc
    finally:
        try:
            if sftp is not None:
                sftp.close()
        finally:
            transport.close()

    _log(f"UPLOADED {name} -> {cfg['host']}:{remote_path} ({remote_size} bytes)")
    return {"host": cfg["host"], "remote_path": remote_path, "bytes": remote_size, "name": name}
