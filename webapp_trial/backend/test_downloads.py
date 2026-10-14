"""SFTP downloads and Submit All. No network: the SFTP step and the upload are replaced by fakes, and every test
runs against throw-away folders and a throw-away database (never the real ones)."""
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import api  # noqa: E402
import db  # noqa: E402
import downloads  # noqa: E402
import pipeline_runner  # noqa: E402
import submit  # noqa: E402
import validation  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "STORAGE_DIR", tmp_path / "store")
    monkeypatch.setattr(db, "JOBS_DIR", tmp_path / "store" / "jobs")
    monkeypatch.setattr(db, "APPROVED_DIR", tmp_path / "store" / "approved")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "store" / "jobs.db")
    monkeypatch.setattr(validation, "EDITS_DIR", tmp_path / "store" / "edits")
    assert str(db.DB_PATH).startswith(str(tmp_path)), "refusing to touch the real database"
    db.init_db()
    dl = tmp_path / "downloads"
    dl.mkdir()
    monkeypatch.setattr(downloads.settings, "DOWNLOAD_DEST_DIR", dl, raising=False)
    monkeypatch.setattr(pipeline_runner, "PROCESSED_LOG", tmp_path / "processed.json")
    monkeypatch.setattr(api, "_analyse_and_route", lambda *a, **k: None)
    return dl


def make_zip(folder: Path, name: str, pdfs: dict[str, bytes]):
    with zipfile.ZipFile(folder / name, "w") as zf:
        for n, data in pdfs.items():
            zf.writestr(n, data)


def test_list_shows_state_size_and_whether_it_was_queued(sandbox):
    make_zip(sandbox, "NC_ABC_keying.zip", {"a.pdf": b"%PDF-1"})
    make_zip(sandbox, "weird.zip", {"a.pdf": b"%PDF-1"})
    rows = {r["name"]: r for r in downloads.list_zips()}
    assert rows["NC_ABC_keying.zip"]["state"] == "NC" and rows["NC_ABC_keying.zip"]["queued"] is False
    assert rows["weird.zip"]["state"] is None


def test_queue_makes_one_job_per_pdf_in_one_batch_and_remembers_the_zip(sandbox):
    make_zip(sandbox, "NC_ABC_keying.zip", {"one.pdf": b"%PDF-1 one", "sub/two.pdf": b"%PDF-1 two", "notes.txt": b"x"})
    out = downloads.queue_zips(["NC_ABC_keying.zip"], lambda *a: None, "batch1")
    names = sorted(j["filename"] for j in out["jobs"])
    assert names == ["one.pdf", "two.pdf"]                                 # folders flattened, non-PDFs ignored
    assert all(j["state_code"] == "NC" and j["batch_id"] == "batch1" for j in out["jobs"])
    assert Path(out["jobs"][0]["pdf_path"]).read_bytes().startswith(b"%PDF")
    again = downloads.queue_zips(["NC_ABC_keying.zip"], lambda *a: None, "batch2")
    assert again["jobs"] == [] and "already queued" in again["skipped"][0]["reason"]


def test_a_zip_with_an_unreadable_state_or_a_bad_name_is_skipped_not_queued(sandbox):
    make_zip(sandbox, "weird.zip", {"a.pdf": b"%PDF-1"})
    out = downloads.queue_zips(["weird.zip", "../../etc/passwd", "missing.zip"], lambda *a: None, "b")
    assert out["jobs"] == [] and len(out["skipped"]) == 3
    assert "state" in out["skipped"][0]["reason"]


def test_too_many_pdfs_are_refused_before_anything_is_queued(sandbox, monkeypatch):
    monkeypatch.setattr(downloads.settings, "WEBAPP_MAX_BATCH_FILES", 1, raising=False)
    make_zip(sandbox, "NC_ABC_keying.zip", {"a.pdf": b"1", "b.pdf": b"2"})
    with pytest.raises(ValueError):
        downloads.queue_zips(["NC_ABC_keying.zip"], lambda *a: None, "b")
    assert db.list_jobs() == []


def test_check_sftp_without_a_password_says_so_and_does_not_connect(monkeypatch):
    monkeypatch.setattr(downloads.settings, "FTP_PASSWORD", "", raising=False)
    monkeypatch.setattr(pipeline_runner, "download_new_files", lambda: pytest.fail("must not connect"))
    with pytest.raises(downloads.SftpError, match="FTP_PASSWORD"):
        downloads.check_sftp()


def test_check_sftp_reports_what_was_downloaded(sandbox, monkeypatch):
    monkeypatch.setattr(downloads.settings, "FTP_PASSWORD", "x", raising=False)
    def fake():
        make_zip(sandbox, "CO_Z_keying.zip", {"a.pdf": b"1"})
        return [sandbox / "CO_Z_keying.zip"]
    monkeypatch.setattr(pipeline_runner, "download_new_files", fake)
    assert downloads.check_sftp() == {"downloaded": ["CO_Z_keying.zip"], "total": 1}


def test_a_failing_server_becomes_a_readable_error(monkeypatch):
    monkeypatch.setattr(downloads.settings, "FTP_PASSWORD", "x", raising=False)
    def boom():
        raise OSError("timed out")
    monkeypatch.setattr(pipeline_runner, "download_new_files", boom)
    with pytest.raises(downloads.SftpError, match="timed out"):
        downloads.check_sftp()


# ── Submit all ───────────────────────────────────────────────────────────────

def doc(tmp_path, name, approved):
    """A reviewed document: working copy, and an approval when asked."""
    original = db.JOBS_DIR / "j" / name
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_text("text of " + name, encoding="utf-8")
    key, _ = validation.working_copy(original)
    if approved:
        validation.approve(key)
    return key


def test_submit_all_sends_only_approved_files_and_never_the_rest(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(submit, "submit_file", lambda local, name: (sent.append(name), {"host": "h", "remote_path": "/r/" + name})[1])
    ok, not_ok = doc(tmp_path, "ok.txt", True), doc(tmp_path, "draft.txt", False)
    stale = doc(tmp_path, "stale.txt", True)
    validation._working_path(stale).write_text("edited afterwards", encoding="utf-8")
    c = TestClient(api.app)
    dry = c.post("/api/submit-all", json={"keys": [ok, not_ok, stale], "dry_run": True}).json()
    assert dry["ready"] == 1 and sent == []                                    # a dry run sends nothing
    res = c.post("/api/submit-all", json={"keys": [ok, not_ok, stale]}).json()
    assert sent == ["ok.txt"] and res["submitted"] == 1 and res["skipped"] == 2
    reasons = {r["name"]: r["reason"] for r in res["results"] if r["status"] == "skipped"}
    assert "not approved" in reasons["draft.txt"] and "approve it again" in reasons["stale.txt"]
    again = c.post("/api/submit-all", json={"keys": [ok]}).json()
    assert again["submitted"] == 0 and again["results"][0]["reason"] == "already submitted" and sent == ["ok.txt"]


def test_one_failed_upload_does_not_stop_the_others(tmp_path, monkeypatch):
    def flaky(local, name):
        if name == "a.txt":
            raise submit.SubmitError("server said no")
        return {"host": "h", "remote_path": "/r/" + name}
    monkeypatch.setattr(submit, "submit_file", flaky)
    keys = [doc(tmp_path, "a.txt", True), doc(tmp_path, "b.txt", True)]
    res = TestClient(api.app).post("/api/submit-all", json={"keys": keys}).json()
    assert (res["failed"], res["submitted"]) == (1, 1)


# ── the real download routine, against a fake SFTP server (nothing connects) ─────────────

class _Entry:
    def __init__(self, filename, regular=True):
        import stat
        self.filename, self.st_mode = filename, (stat.S_IFREG if regular else stat.S_IFDIR) | 0o644


class _FakeSftp:
    def __init__(self, files, broken=()):
        self.files, self.broken, self.closed, self.asked = files, set(broken), False, []

    def listdir_attr(self, remote_dir):
        self.asked.append(remote_dir)
        return [_Entry(n, regular) for n, regular in self.files]

    def get(self, remote, local):
        name = remote.rsplit("/", 1)[-1]
        Path(local).write_bytes(b"PK partial")                  # a failed transfer leaves a partial file behind
        if name in self.broken:
            raise OSError("connection reset")

    def close(self):
        self.closed = True


class _FakeTransport:
    closed = False

    def close(self):
        self.closed = True


def test_check_sftp_downloads_only_new_files_and_cleans_up_a_failed_one(sandbox, monkeypatch):
    import ftp_download

    monkeypatch.setattr(pipeline_runner, "DOWNLOADS_DIR", sandbox)
    monkeypatch.setattr(downloads.settings, "FTP_PASSWORD", "x", raising=False)
    (sandbox / "NC_HAVE_keying.zip").write_bytes(b"already here")
    sftp, transport = _FakeSftp([("NC_HAVE_keying.zip", True), ("CO_NEW_keying.zip", True),
                                 ("OH_BAD_keying.zip", True), ("subfolder", False)], broken={"OH_BAD_keying.zip"}), _FakeTransport()
    monkeypatch.setattr(ftp_download, "connect_sftp", lambda user, pw: (transport, sftp))
    out = downloads.check_sftp()
    assert out["downloaded"] == ["CO_NEW_keying.zip"]                      # not the one it had, not the folder, not the failed one
    assert not (sandbox / "OH_BAD_keying.zip").exists()                    # no half-downloaded file left to be queued
    assert (sandbox / "NC_HAVE_keying.zip").read_bytes() == b"already here"  # an existing file is never overwritten
    assert sftp.closed and transport.closed                                # the connection is always closed
    assert sftp.asked == [ftp_download.REMOTE_DIR]


def test_each_file_reports_where_it_stands_without_creating_folders(tmp_path):
    ok, draft, stale = doc(tmp_path, "ok.txt", True), doc(tmp_path, "draft.txt", False), doc(tmp_path, "stale.txt", True)
    validation._working_path(stale).write_text("edited afterwards", encoding="utf-8")
    fresh = db.JOBS_DIR / "j" / "never_opened.txt"
    fresh.write_text("x", encoding="utf-8")
    before = {p.name for p in validation.EDITS_DIR.iterdir()}
    paths = [str(db.JOBS_DIR / "j" / n) for n in ("ok.txt", "draft.txt", "stale.txt", "never_opened.txt")] + [str(tmp_path / "nowhere.txt")]
    states = TestClient(api.app).post("/api/approval-states", json={"text_paths": paths}).json()["states"]
    got = {Path(p).name: s["state"] for p, s in states.items()}
    assert got == {"ok.txt": "approved", "draft.txt": "not_approved", "stale.txt": "edited",
                   "never_opened.txt": "not_reviewed", "nowhere.txt": "missing"}
    assert {p.name for p in validation.EDITS_DIR.iterdir()} == before          # nothing was created by asking
    submit_ok = TestClient(api.app)
    assert submit_ok.post("/api/submit-all", json={"keys": [ok], "dry_run": True}).json()["ready"] == 1
