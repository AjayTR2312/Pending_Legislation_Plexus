"""
S3 backend test for storage.py, using moto to mock AWS S3 (no network, no real
bucket). Proves the ensure_local / persist round-trip that makes the app
stateless on Plexus: a file written and uploaded by one "pod" can be fetched by
another pod whose local cache is empty.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

moto = pytest.importorskip("moto")
boto3 = pytest.importorskip("boto3")

_tmp = Path(__file__).resolve().parent / "_s3_test_storage"
BUCKET = "test-webapp-bucket"


def _load_storage():
    os.environ["TRIAL_STORAGE_DIR"] = str(_tmp)
    os.environ["WEBAPP_S3_BUCKET"] = BUCKET
    os.environ["WEBAPP_S3_PREFIX"] = "wt"
    os.environ["AWS_ACCESS_KEY_ID"] = "test"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "test"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
    for name in ("storage", "deployconfig"):
        if name in sys.modules:
            del sys.modules[name]
    import storage  # noqa: PLC0415

    importlib.reload(storage)
    return storage


@pytest.fixture()
def s3_storage():
    from moto import mock_aws

    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        storage = _load_storage()
        assert storage.s3_enabled()
        yield storage
    for var in ("WEBAPP_S3_BUCKET", "WEBAPP_S3_PREFIX"):
        os.environ.pop(var, None)
    # Restore the shared storage module to its default local-disk state.
    for name in ("storage", "deployconfig"):
        sys.modules.pop(name, None)
    import storage as _storage  # noqa: PLC0415

    importlib.reload(_storage)


def test_persist_then_fetch_from_empty_cache(s3_storage):
    storage = s3_storage
    jobs = storage.STORAGE_ROOT / "jobs" / "job1"
    pdf = jobs / "bill.pdf"
    storage.write_bytes(pdf, b"%PDF-1.7 fake")

    # Simulate another pod: delete the local copy, then ensure_local must pull
    # it back from S3.
    pdf.unlink()
    assert not pdf.exists()
    assert storage.ensure_local(pdf) is True
    assert pdf.read_bytes() == b"%PDF-1.7 fake"


def test_exists_checks_s3(s3_storage):
    storage = s3_storage
    out = storage.STORAGE_ROOT / "jobs" / "job2" / "out.txt"
    storage.write_text(out, "converted text")
    out.unlink()
    assert storage.exists(out) is True
    missing = storage.STORAGE_ROOT / "jobs" / "nope" / "x.txt"
    assert storage.exists(missing) is False


def test_copy_persists_destination(s3_storage):
    storage = s3_storage
    src = storage.STORAGE_ROOT / "jobs" / "job3" / "out.txt"
    storage.write_text(src, "approved content")
    dest = storage.STORAGE_ROOT / "approved" / "job3_out.txt"
    storage.copy(src, dest)
    # Destination must be retrievable from S3 by a fresh pod.
    dest.unlink()
    assert storage.ensure_local(dest) is True
    assert dest.read_text() == "approved content"


def test_persist_tree_uploads_all(s3_storage):
    storage = s3_storage
    cache = storage.STORAGE_ROOT / "compare" / "key1"
    (cache).mkdir(parents=True, exist_ok=True)
    (cache / "meta.json").write_text("{}")
    (cache / "page_0.png").write_bytes(b"PNG")
    storage.persist_tree(cache)
    for name in ("meta.json", "page_0.png"):
        f = cache / name
        f.unlink()
        assert storage.ensure_local(f) is True


def test_read_helpers(s3_storage):
    storage = s3_storage
    f = storage.STORAGE_ROOT / "jobs" / "job4" / "out.txt"
    storage.write_text(f, "hello world")
    f.unlink()
    assert storage.read_text(f) == "hello world"
    f.unlink()
    assert storage.read_bytes(f) == b"hello world"
