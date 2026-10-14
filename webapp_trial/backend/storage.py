"""
File-storage abstraction for the trial backend.

Why this exists
---------------
On the Plexus Kubernetes cluster pods are ephemeral and may be replicated, so
anything written to the container's local disk is lost on restart and is not
visible to other replicas. The app must therefore keep its files in shared
storage (S3). PyMuPDF (``pymupdf.open(path)``) and FastAPI ``FileResponse``
both need a real local file, so we cannot simply pass S3 URLs around.

Design
------
The local filesystem under ``STORAGE_ROOT`` is treated as an *ephemeral working
cache*; S3 (when configured) is the *source of truth*. Files keep the same
deterministic local paths everywhere in the codebase, so no caller or database
column has to change. Two hooks bridge to S3:

    * ``persist(path)``      — after writing a file locally, upload it to S3.
    * ``ensure_local(path)`` — before reading/serving, download it from S3 into
                               the same local path if it is not already present.

When ``WEBAPP_S3_BUCKET`` is unset (local development, tests) every S3 hook is a
no-op and behaviour is identical to the original local-disk implementation.

Keys
----
An object's S3 key is ``<prefix>/<path relative to STORAGE_ROOT>`` using forward
slashes, so the same logical file maps to the same object from any pod.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
from pathlib import Path
from typing import Optional

import deployconfig
from appconfig import settings

log = logging.getLogger("trial.storage")

# The ephemeral working root. Everything the app reads/writes lives under here;
# it mirrors to/from S3 when a bucket is configured.
STORAGE_ROOT = Path(settings.TRIAL_STORAGE_DIR).resolve()

_ENABLED = deployconfig.s3_enabled()
_BUCKET = deployconfig.s3_bucket()
_PREFIX = deployconfig.s3_prefix()
_ENDPOINT = deployconfig.s3_endpoint_url()
_REGION = deployconfig.s3_region()

_client_lock = threading.Lock()
_s3_client = None


def s3_enabled() -> bool:
    return _ENABLED


def _client():
    """Lazily build a thread-safe boto3 S3 client (only when S3 is enabled).

    Credentials are optional: when WEBAPP has no explicit AWS keys, boto3 falls
    back to the instance IAM role (the Plexus cross-account storage role).
    """
    global _s3_client
    if _s3_client is None:
        with _client_lock:
            if _s3_client is None:
                import boto3  # imported lazily so local mode needs no boto3

                _s3_client = boto3.client(
                    "s3",
                    region_name=_REGION,
                    endpoint_url=_ENDPOINT,
                    aws_access_key_id=deployconfig.aws_access_key_id(),
                    aws_secret_access_key=deployconfig.aws_secret_access_key(),
                )
    return _s3_client


def _rel(path: Path | str) -> str:
    """Relative-to-root POSIX key for a local path (raises if outside the root)."""
    resolved = Path(path).resolve()
    rel = resolved.relative_to(STORAGE_ROOT)
    return rel.as_posix()


def _s3_key(path: Path | str) -> str:
    rel = _rel(path)
    return f"{_PREFIX}/{rel}" if _PREFIX else rel


def within_root(path: Path | str) -> bool:
    try:
        Path(path).resolve().relative_to(STORAGE_ROOT)
        return True
    except ValueError:
        return False


# ── Write path ────────────────────────────────────────────────────────────────

def persist(path: Path | str) -> None:
    """Upload a locally-written file to S3. No-op in local mode.

    Call this immediately after a file under STORAGE_ROOT has been fully written.
    """
    if not s3_enabled():
        return
    p = Path(path)
    if not p.is_file():
        return
    if not within_root(p):
        return
    key = _s3_key(p)
    try:
        _client().upload_file(str(p), _BUCKET, key)
    except Exception:  # noqa: BLE001 — surface but never crash the request
        log.exception("Failed to upload %s to s3://%s/%s", p, _BUCKET, key)
        raise


def persist_tree(directory: Path | str) -> None:
    """Upload every file under a directory to S3 (used for cache folders)."""
    if not s3_enabled():
        return
    root = Path(directory)
    if not root.is_dir():
        return
    for file in root.rglob("*"):
        if file.is_file():
            persist(file)


# ── Read path ─────────────────────────────────────────────────────────────────

def ensure_local(path: Path | str) -> bool:
    """Make sure a file exists locally, pulling it from S3 if necessary.

    Returns True if the file is present locally afterwards. No-op (returns the
    local existence) in local mode.
    """
    p = Path(path)
    if p.is_file():
        return True
    if not s3_enabled() or not within_root(p):
        return p.is_file()
    key = _s3_key(p)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        _client().download_file(_BUCKET, key, str(p))
        return True
    except Exception:  # noqa: BLE001 — missing object is a normal "not found"
        log.debug("Object not found in S3: s3://%s/%s", _BUCKET, key)
        return False


def exists(path: Path | str) -> bool:
    """True if the file is available locally or in S3."""
    p = Path(path)
    if p.is_file():
        return True
    if not s3_enabled() or not within_root(p):
        return False
    try:
        _client().head_object(Bucket=_BUCKET, Key=_s3_key(p))
        return True
    except Exception:  # noqa: BLE001
        return False


def read_text(path: Path | str, encoding: str = "utf-8") -> str:
    ensure_local(path)
    return Path(path).read_text(encoding=encoding)


def read_bytes(path: Path | str) -> bytes:
    ensure_local(path)
    return Path(path).read_bytes()


# ── Mutating helpers that keep S3 in sync ─────────────────────────────────────

def write_bytes(path: Path | str, data: bytes) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    persist(p)


def write_text(path: Path | str, text: str, encoding: str = "utf-8") -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding=encoding)
    persist(p)


def copy(src: Path | str, dest: Path | str) -> None:
    """Copy a (possibly S3-backed) file to a new local path and persist it."""
    ensure_local(src)
    d = Path(dest)
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, d)
    persist(d)


def delete(path: Path | str) -> None:
    p = Path(path)
    try:
        if p.is_file():
            p.unlink()
    finally:
        if s3_enabled() and within_root(p):
            try:
                _client().delete_object(Bucket=_BUCKET, Key=_s3_key(p))
            except Exception:  # noqa: BLE001
                log.debug("Could not delete S3 object for %s", p)
