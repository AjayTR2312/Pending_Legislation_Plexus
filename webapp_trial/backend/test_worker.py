"""The worker's own wiring (it once crashed every job by reaching for a module that wasn't there).

These tests create jobs, so every one of them runs against a throw-away database: the fixture repoints db's paths
at tmp_path and refuses to continue if they still point at the real storage.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import db  # noqa: E402
import worker  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(db, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(db, "APPROVED_DIR", tmp_path / "approved")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "jobs.db")
    assert str(db.DB_PATH).startswith(str(tmp_path)), "refusing to touch the real job database"
    db.init_db()


def test_worker_can_reach_the_llm_module_and_cache_decision():
    llm = worker._llm()
    with llm.cache_hint(False):
        ok, why = llm.cache_decision()
    assert isinstance(ok, bool) and why


def _job(name, state, status="queued"):
    j = db.create_job(name, state)
    with db._connect() as c:
        c.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, j["id"]))
    return j["id"]


def test_pending_same_state_counts_only_waiting_jobs_for_that_state_or_unknown():
    me = _job("me.pdf", "FL")
    assert db.pending_same_state(me, "FL") == 0
    _job("a.pdf", "FL")
    _job("b.pdf", "CO")                       # other state: shares nothing
    _job("c.pdf", None)                       # state not detected yet: might be FL
    _job("d.pdf", "FL", status="processing")  # already running, not waiting
    assert db.pending_same_state(me, "FL") == 2
    assert db.pending_same_state(me, None) == 3


def test_a_job_runs_through_the_worker_and_records_the_cache_decision(monkeypatch, tmp_path):
    out = tmp_path / "x.txt"
    out.write_text("<L>hello</L>\n", encoding="utf-8")
    seen = {}

    def fake_convert(**kw):
        seen["decision"] = worker._llm().cache_decision()
        return {"output_path": str(out), "state_code": "FL", "char_count": 13, "usage": {}}

    monkeypatch.setattr(worker.conversion, "convert_pdf", fake_convert)
    monkeypatch.setattr(worker, "_run_validation", lambda *a, **k: None)
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    job = db.create_job("x.pdf", "FL")
    _job("next.pdf", "FL")                    # a second FL job is waiting -> caching should be requested
    messages = []
    full = db.get_job(job["id"])
    full["mode"] = "hybrid"
    full["pdf_path"] = str(pdf)
    worker._convert_and_validate(full, job["id"], lambda step, status, msg="": messages.append((step, msg)))

    assert db.get_job(job["id"])["status"] == "done", db.get_job(job["id"])["error"]
    assert any("Prompt cache" in (m or "") for _, m in messages)
    assert seen["decision"][0] is True        # the hint reached the call
