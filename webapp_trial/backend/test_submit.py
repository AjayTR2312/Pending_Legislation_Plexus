"""Save -> Approve -> Submit. The SFTP client is faked: these tests never touch a real server."""

import json
import sys
import types
from pathlib import Path

import pytest

import db
import submit
import validation


@pytest.fixture
def doc(tmp_path, monkeypatch):
    """An edit session with a working copy, isolated under tmp_path."""
    monkeypatch.setattr(validation, "EDITS_DIR", tmp_path / "edits")
    monkeypatch.setattr(db, "APPROVED_DIR", tmp_path / "approved")
    key = "abc123"
    d = validation.EDITS_DIR / key
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"original": "x", "name": "2026_bill_0.txt"}), encoding="utf-8")
    (d / "2026_bill_0.txt").write_text("[A>One paragraph.<A]\n", encoding="utf-8")
    return key, d / "2026_bill_0.txt"


def test_not_approved_until_approved(doc):
    key, _ = doc
    s = validation.approval_status(key)
    assert not s["approved"] and not s["stale"] and not s["submitted"]
    with pytest.raises(PermissionError, match="Approve this document"):
        validation.approved_snapshot(key)


def test_approve_snapshots_and_edit_makes_it_stale(doc):
    key, work = doc
    out = validation.approve(key)
    assert out["approved"] and Path(out["saved_to"]).read_text(encoding="utf-8") == work.read_text(encoding="utf-8")

    work.write_text("[A>One paragraph, edited.<A]\n", encoding="utf-8")  # edit after approval
    s = validation.approval_status(key)
    assert s["stale"] and not s["approved"]
    with pytest.raises(PermissionError, match="changed since it was approved"):
        validation.approved_snapshot(key)

    validation.approve(key)  # approving again accepts the new text
    assert validation.approval_status(key)["approved"]


def test_submitted_flag_clears_after_an_edit(doc):
    key, work = doc
    validation.approve(key)
    validation.mark_submitted(key, "host:/dir/file")
    assert validation.approval_status(key)["submitted"]
    work.write_text("changed\n", encoding="utf-8")
    assert not validation.approval_status(key)["submitted"]


# ── submit_file against a fake paramiko ──────────────────────────────────────

class _FakeSFTP:
    def __init__(self, store, mismatch=False):
        self.store, self.mismatch, self.dirs = store, mismatch, set()

    def stat(self, path):
        if path in self.store:
            return types.SimpleNamespace(st_size=self.store[path] + (1 if self.mismatch else 0))
        if path in self.dirs:
            return types.SimpleNamespace(st_size=0)
        raise IOError(path)

    def mkdir(self, path):
        self.dirs.add(path)

    def put(self, local, remote):
        self.store[remote] = Path(local).stat().st_size

    def close(self):
        pass


def _install_fake_paramiko(monkeypatch, mismatch=False, connect_error=None):
    store: dict = {}
    sftp = _FakeSFTP(store, mismatch)

    class Transport:
        def __init__(self, addr):
            self.addr = addr

        def connect(self, username, password):
            if connect_error:
                raise connect_error
            self.creds = (username, password)

        def close(self):
            pass

    fake = types.SimpleNamespace(Transport=Transport, SFTPClient=types.SimpleNamespace(from_transport=lambda t: sftp))
    monkeypatch.setitem(sys.modules, "paramiko", fake)
    return store


def _env(monkeypatch, password="secret"):
    monkeypatch.setenv("SFTP_HOST", "uat.example")
    monkeypatch.setenv("SFTP_UPLOAD_DIR", "/apps/Codes/keyed/")
    if password is None:
        monkeypatch.delenv("FTP_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("FTP_PASSWORD", password)
    monkeypatch.setattr(submit, "LOG_FILE", Path("submit-test.log"))


def test_config_never_exposes_the_password(monkeypatch):
    _env(monkeypatch)
    cfg = submit.config()
    assert cfg["host"] == "uat.example" and cfg["remote_dir"] == "/apps/Codes/keyed" and cfg["password_set"]
    assert "secret" not in json.dumps(cfg)


def test_submit_uploads_with_the_pipeline_filename_and_verifies(tmp_path, monkeypatch):
    _env(monkeypatch)
    store = _install_fake_paramiko(monkeypatch)
    f = tmp_path / "snapshot.txt"
    f.write_text("hello", encoding="utf-8")
    monkeypatch.setattr(submit, "_log", lambda line: None)
    out = submit.submit_file(f, "../../evil/2026_bill_0.txt")  # path components are stripped
    assert out["remote_path"] == "/apps/Codes/keyed/2026_bill_0.txt" and out["bytes"] == 5
    assert store == {"/apps/Codes/keyed/2026_bill_0.txt": 5}


def test_submit_fails_clearly(tmp_path, monkeypatch):
    f = tmp_path / "a.txt"
    f.write_text("hello", encoding="utf-8")
    monkeypatch.setattr(submit, "_log", lambda line: None)

    _env(monkeypatch, password=None)
    with pytest.raises(submit.SubmitError, match="FTP_PASSWORD"):
        submit.submit_file(f, "a.txt")

    _env(monkeypatch)
    _install_fake_paramiko(monkeypatch, mismatch=True)
    with pytest.raises(submit.SubmitError, match="Size mismatch"):
        submit.submit_file(f, "a.txt")

    _install_fake_paramiko(monkeypatch, connect_error=OSError("timed out"))
    with pytest.raises(submit.SubmitError, match="Could not connect"):
        submit.submit_file(f, "a.txt")


def test_api_blocks_submit_until_approved(doc, monkeypatch):
    from fastapi.testclient import TestClient

    import api

    key, _ = doc
    client = TestClient(api.app)
    r = client.post("/api/validation/submit", json={"key": key})
    assert r.status_code == 409 and "Approve" in r.json()["detail"]

    _env(monkeypatch)
    _install_fake_paramiko(monkeypatch)
    monkeypatch.setattr(submit, "_log", lambda line: None)
    assert client.post("/api/validation/approve", json={"key": key}).json()["approved"]
    r = client.post("/api/validation/submit", json={"key": key})
    assert r.status_code == 200 and r.json()["submitted"] and r.json()["remote_path"].endswith("/2026_bill_0.txt")
    assert client.get("/api/validation/approval", params={"key": key}).json()["submitted"]
