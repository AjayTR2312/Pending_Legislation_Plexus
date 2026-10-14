"""
Deployment configuration for the Plexus / cloud build.

This module centralises the *new* environment variables the containerised
deployment needs, without touching app/settings.py (which the trial is designed
to read unmodified). Everything here degrades to the existing local behaviour
when the variables are unset, so local development and the test-suite keep
working exactly as before:

    * DATABASE_URL unset  -> db.py uses local SQLite (default)
    * WEBAPP_S3_BUCKET unset -> storage.py uses the local filesystem (default)

Plexus deployment sets these to switch the job store to Postgres and file
storage to S3, which is what makes the app stateless (a hard requirement for
the Plexus Kubernetes cluster where pods are ephemeral and replicated).

Read the effective configuration with:

    python -c "import deployconfig; print(deployconfig.describe())"
"""

from __future__ import annotations

import os
from typing import Optional


def _env(name: str) -> Optional[str]:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


# ── Job database ──────────────────────────────────────────────────────────────
# A SQLAlchemy-style URL. When it starts with postgres:// or postgresql:// the
# db.py layer uses psycopg (Postgres); otherwise SQLite is used.
def database_url() -> Optional[str]:
    return _env("DATABASE_URL")


def db_backend() -> str:
    url = database_url()
    if url and url.split("://", 1)[0] in ("postgres", "postgresql", "postgresql+psycopg"):
        return "postgres"
    return "sqlite"


# ── File storage ──────────────────────────────────────────────────────────────
# Uploaded PDFs, converted outputs, approved copies and the compare cache are
# mirrored to S3 so any replica (or a restarted pod) can serve them. The local
# filesystem is still used as an ephemeral working/cache area.
#
# PLACEHOLDER bucket name — change this (or set WEBAPP_S3_BUCKET) once the real
# bucket exists. Having a default here does NOT turn S3 on by itself: S3 is only
# used when explicitly enabled (WEBAPP_S3_ENABLED=true) or when WEBAPP_S3_BUCKET
# is provided, so local development and the test-suite stay on local disk.
DEFAULT_S3_BUCKET = "a209457-ml-workspace-xmlconversiof3jr-use1"
DEFAULT_S3_REGION = "us-east-1"


def s3_bucket() -> str:
    """The bucket name to use (env override, else the placeholder default)."""
    return _env("WEBAPP_S3_BUCKET") or DEFAULT_S3_BUCKET


def s3_region() -> str:
    return _env("WEBAPP_S3_REGION") or DEFAULT_S3_REGION


def aws_access_key_id() -> Optional[str]:
    """Optional — boto3 falls back to the instance IAM role when unset."""
    return _env("AWS_ACCESS_KEY_ID")


def aws_secret_access_key() -> Optional[str]:
    """Optional — boto3 falls back to the instance IAM role when unset."""
    return _env("AWS_SECRET_ACCESS_KEY")


def s3_prefix() -> str:
    # Objects are stored under <prefix>/<relative-key>. Trailing slash stripped.
    return (_env("WEBAPP_S3_PREFIX") or "webapp_trial").strip("/")


def s3_endpoint_url() -> Optional[str]:
    # Optional override for S3-compatible stores / local MinIO in tests.
    return _env("WEBAPP_S3_ENDPOINT_URL")


def s3_enabled() -> bool:
    """S3 is active only when explicitly enabled or a bucket is set via env.
    This keeps local dev and tests on local disk even though a default bucket
    name exists above."""
    if _bool("WEBAPP_S3_ENABLED"):
        return True
    return _env("WEBAPP_S3_BUCKET") is not None


def storage_backend() -> str:
    return "s3" if s3_enabled() else "local"


# ── Feature toggles ───────────────────────────────────────────────────────────
# The SFTP-dependent features (Pipeline Sessions view, SFTP Downloads, SFTP
# Submit) need an internal SFTP server and host-mounted pipeline folders that do
# not exist in a Plexus pod. Set WEBAPP_DISABLE_SFTP=true on Plexus to turn them
# off cleanly; the core upload->convert->review->approve flow is unaffected.
def sftp_features_enabled() -> bool:
    return not _bool("WEBAPP_DISABLE_SFTP", default=False)


# ── Observability ─────────────────────────────────────────────────────────────
def datadog_logging_enabled() -> bool:
    """ddtrace log integration — only patched when explicitly enabled on Plexus."""
    return _bool("DD_LOGS_INJECTION") or _bool("WEBAPP_DATADOG_LOGGING")


def describe() -> dict[str, object]:
    """Effective deploy configuration (no secrets)."""
    url = database_url()
    return {
        "db_backend": db_backend(),
        "database_url_set": bool(url),
        "storage_backend": storage_backend(),
        "s3_bucket": s3_bucket(),
        "s3_region": s3_region(),
        "s3_prefix": s3_prefix(),
        "s3_endpoint_url": s3_endpoint_url(),
        "aws_credentials": "explicit" if aws_access_key_id() else "iam-role",
        "sftp_features_enabled": sftp_features_enabled(),
        "datadog_logging": datadog_logging_enabled(),
    }


if __name__ == "__main__":
    import json

    print(json.dumps(describe(), indent=2))
