"""
Verification for the pipeline sessions browser.

The important check is path containment: /api/sessions/file/* takes a client
supplied path, so anything outside tmp/pending_processing must be rejected.

Run:  python test_sessions.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="trial_sessions_test_"))
os.environ["APP_TMP_DIR"] = str(_tmp)

import sessions  # noqa: E402 — imported after APP_TMP_DIR so it picks up the temp root


def _build_fixture() -> Path:
    session_dir = _tmp / "pending_processing" / "10052026" / "FL_20261005_093300_FL_ABC"
    (session_dir / "input").mkdir(parents=True, exist_ok=True)
    (session_dir / "output").mkdir(parents=True, exist_ok=True)

    stem = "2026_2026_9_3_2_007030_0_3_1_20260122_0"
    (session_dir / "input" / f"{stem}.pdf").write_bytes(b"%PDF-1.4 fake")
    (session_dir / "output" / f"{stem}.txt").write_text("[A>added<A] text", encoding="utf-8")
    # Diagnostic siblings that must not appear as reviewable outputs.
    (session_dir / "output" / f"{stem}_hybrid_raw.txt").write_text("raw", encoding="utf-8")
    (session_dir / "output" / f"{stem}_clean_diff.txt").write_text("diff", encoding="utf-8")
    return session_dir


def test_lists_sessions() -> None:
    found = sessions.list_sessions()
    assert len(found) == 1, f"expected 1 session, got {len(found)}"
    s = found[0]
    assert s["state"] == "FL", f"expected FL, got {s['state']}"
    assert s["date_folder"] == "10052026"
    assert s["pdf_count"] == 1
    assert s["converted_count"] == 1, "diagnostic .txt siblings must not be counted"
    print("  [OK] session listed with correct state, date and counts")


def test_lists_files_and_pairs_pdf() -> None:
    s = sessions.list_sessions()[0]
    files = sessions.list_files(s["session_id"])
    assert files is not None and len(files) == 1, f"expected 1 reviewable file, got {files}"
    f = files[0]
    assert f["pdf_path"] is not None, "output should be paired with its source PDF"
    assert Path(f["pdf_path"]).exists()
    print("  [OK] output file paired with its source PDF; diagnostics excluded")


def test_path_traversal_blocked() -> None:
    s = sessions.list_sessions()[0]
    good = sessions.list_files(s["session_id"])[0]["text_path"]
    resolved = sessions.resolve_within_processing(good)
    assert resolved.exists(), "legitimate pipeline file should resolve"

    secret = _tmp / "secret.txt"
    secret.write_text("credentials", encoding="utf-8")

    for attempt in (
        str(secret),
        str(Path(good).parent / ".." / ".." / ".." / ".." / "secret.txt"),
        "C:\\Windows\\win.ini" if os.name == "nt" else "/etc/passwd",
    ):
        try:
            sessions.resolve_within_processing(attempt)
        except (PermissionError, FileNotFoundError):
            continue
        raise AssertionError(f"path traversal NOT blocked for: {attempt}")
    print("  [OK] paths outside pending_processing are rejected")


def main() -> int:
    print("Pipeline sessions verification")
    print(f"  temp root: {_tmp}")
    _build_fixture()
    for fn in (test_lists_sessions, test_lists_files_and_pairs_pdf, test_path_traversal_blocked):
        try:
            fn()
        except AssertionError as exc:
            print(f"  [FAIL] {fn.__name__}: {exc}")
            return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
