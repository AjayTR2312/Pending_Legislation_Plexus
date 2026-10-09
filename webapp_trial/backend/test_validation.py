"""
Verification for the validation review layer.

The critical property: Apply Fix must NEVER modify the original pipeline file.
Corrections go to a trial-owned working copy only.

Run:  python test_validation.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="trial_validation_test_"))
os.environ["APP_TMP_DIR"] = str(_tmp)
os.environ["TRIAL_STORAGE_DIR"] = str(_tmp / "storage")
os.environ["TRIAL_DB_PATH"] = str(_tmp / "storage" / "jobs.db")

import db  # noqa: E402
import validation  # noqa: E402

ORIGINAL_TEXT = "Section 1. The [A>new provision<A] applies to all persons.\n"


def _build_fixture() -> Path:
    db.init_db()
    out_dir = _tmp / "pending_processing" / "10052026" / "FL_20261005_x" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    txt = out_dir / "2026_2026_9_3_2_007030_0_3_1_20260122_0.txt"
    txt.write_text(ORIGINAL_TEXT, encoding="utf-8")
    return txt


def test_original_never_modified(txt: Path) -> None:
    report = validation.build_report(str(txt), None, "FL")
    key = report["key"]

    assert Path(report["working_path"]) != txt, "working copy must be a separate file"

    validation.apply_fix(key, "new provision", "revised provision")

    assert txt.read_text(encoding="utf-8") == ORIGINAL_TEXT, (
        "ORIGINAL PIPELINE FILE WAS MODIFIED — this must never happen"
    )
    working = Path(report["working_path"]).read_text(encoding="utf-8")
    assert "revised provision" in working, "fix should appear in the working copy"
    print("  [OK] fix applied to working copy; original pipeline file untouched")


def test_undo_restores(txt: Path) -> None:
    report = validation.build_report(str(txt), None, "FL")
    key = report["key"]
    before = Path(report["working_path"]).read_text(encoding="utf-8")

    validation.apply_fix(key, "applies to all persons", "applies to some persons")
    after = Path(report["working_path"]).read_text(encoding="utf-8")
    assert "some persons" in after

    validation.undo_last(key)
    restored = Path(report["working_path"]).read_text(encoding="utf-8")
    assert restored == before, "undo should restore the previous content exactly"
    print("  [OK] undo restores previous content")


def test_reset_discards_all(txt: Path) -> None:
    report = validation.build_report(str(txt), None, "FL")
    key = report["key"]
    validation.apply_fix(key, "Section 1.", "Section 2.")
    validation.reset(key)
    restored = Path(report["working_path"]).read_text(encoding="utf-8")
    assert restored == ORIGINAL_TEXT, "reset should restore the pristine original"
    assert validation._load_history(key) == []
    print("  [OK] reset discards all edits")


def test_missing_text_rejected(txt: Path) -> None:
    report = validation.build_report(str(txt), None, "FL")
    try:
        validation.apply_fix(report["key"], "text that does not exist", "x")
    except ValueError:
        print("  [OK] fix with non-matching original text is rejected")
        return
    raise AssertionError("expected ValueError for non-matching original text")


def test_path_traversal_blocked() -> None:
    secret = _tmp / "secret.txt"
    secret.write_text("credentials", encoding="utf-8")
    for attempt in (str(secret), "C:\\Windows\\win.ini" if os.name == "nt" else "/etc/passwd"):
        try:
            validation.resolve_readable(attempt)
        except (PermissionError, FileNotFoundError):
            continue
        raise AssertionError(f"path traversal NOT blocked for: {attempt}")
    print("  [OK] reads outside permitted directories are rejected")


def test_report_shape(txt: Path) -> None:
    validation.reset(validation.build_report(str(txt), None, "FL")["key"])
    report = validation.build_report(str(txt), None, "FL")
    for field in ("key", "name", "integrity", "issues", "content_summary", "history"):
        assert field in report, f"report missing field: {field}"
    assert report["integrity"]["counts"].get("open_a") == 1, "should count one [A> tag"
    print("  [OK] report contains accuracy, integrity and issue fields")


def main() -> int:
    print("Validation review verification")
    print(f"  temp root: {_tmp}")
    txt = _build_fixture()
    checks = [
        (test_original_never_modified, txt),
        (test_undo_restores, txt),
        (test_reset_discards_all, txt),
        (test_missing_text_rejected, txt),
        (test_path_traversal_blocked, None),
        (test_report_shape, txt),
    ]
    for fn, arg in checks:
        try:
            fn(arg) if arg is not None else fn()
        except AssertionError as exc:
            print(f"  [FAIL] {fn.__name__}: {exc}")
            return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
