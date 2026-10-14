"""
Validation reports and human corrections for the trial UI.

Wraps the existing checkers (content_accuracy_checker, pymupdf_extractor) without
modifying them.

SAFETY: corrections are never written to the original file. Opening a document for
review creates a trial-owned working copy under storage/edits/; Apply Fix and Undo
operate on that copy only. Pipeline output under tmp/pending_processing/ is treated
as read-only, so a trial session can never corrupt real pipeline results.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Optional

import db
import rules
import sessions
import storage

# Extractor messages that compare tag *fragments* against the raw PDF extraction.
# Under the one-tag-per-paragraph rule these differences are expected, so they are
# shown as notes. The word-level "Tag boundary drift" check stays a real finding.
_BASELINE_NOTES = (
    "Suspiciously long tags found",
    "Missing reference tags vs pre-cleaned input",
    "New tag payloads not in pre-cleaned input",
)
# Reported precisely (with a location) by rules.py instead.
_COVERED_BY_RULES = (
    "Mismatched A delimiters", "Mismatched D delimiters", "Malformed A close", "Malformed D close",
    "Opposite-type nested tags",
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
APP_DIR = PROJECT_ROOT / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

EDITS_DIR = db.STORAGE_DIR / "edits"

log = logging.getLogger("trial.validation")


# ── Path safety ──────────────────────────────────────────────────────────────

def resolve_readable(path_str: str) -> Path:
    """Allow reads only from the pipeline directory or the trial job storage."""
    candidate = Path(path_str).resolve()
    roots = [sessions.processing_dir().resolve(), db.JOBS_DIR.resolve(), EDITS_DIR.resolve()]
    for root in roots:
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        # On Plexus the file may live only in S3 (written by another pod); pull
        # it down before the existence check. No-op in local mode.
        storage.ensure_local(candidate)
        if not candidate.is_file():
            raise FileNotFoundError(path_str)
        return candidate
    raise PermissionError(f"Path is outside permitted directories: {path_str}")


def _key_for(path: Path) -> str:
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]


def _edit_dir(key: str) -> Path:
    d = EDITS_DIR / key
    d.mkdir(parents=True, exist_ok=True)
    return d


def working_copy(original: Path) -> tuple[str, Path]:
    """Return (key, working copy path), creating the copy on first use."""
    key = _key_for(original)
    d = _edit_dir(key)
    copy_path = d / original.name
    if not copy_path.exists():
        storage.ensure_local(original)
        shutil.copyfile(original, copy_path)
        (d / "meta.json").write_text(
            json.dumps({"original": str(original), "name": original.name}, indent=2),
            encoding="utf-8",
        )
    return key, copy_path


def _history_path(key: str) -> Path:
    return _edit_dir(key) / "history.json"


def _load_history(key: str) -> list[dict[str, Any]]:
    p = _history_path(key)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def _save_history(key: str, history: list[dict[str, Any]]) -> None:
    _history_path(key).write_text(json.dumps(history, indent=2), encoding="utf-8")


# ── Report building ──────────────────────────────────────────────────────────

def _preclean(raw_text: str, state_code: Optional[str]) -> str:
    """Apply the same pre-LLM cleaning the hybrid pipeline does.

    Without this the baseline still contains line numbers and page footers, so
    the content checker reports them as dropped content — hundreds of false
    positives that bury the real issues.
    """
    try:
        from text_accuracy_helpers import (
            strip_bare_line_number_blocks,
            strip_leading_line_numbers,
            strip_trailing_line_numbers,
        )

        raw_text = strip_trailing_line_numbers(raw_text, state_code=state_code)
        raw_text = strip_leading_line_numbers(raw_text, state_code=state_code)
        raw_text = strip_bare_line_number_blocks(raw_text, state_code=state_code)
    except Exception as exc:  # noqa: BLE001
        log.warning("Pre-cleaning failed, using raw baseline: %s", exc)
    return raw_text


def _find_precleaned(
    original: Path, pdf_path: Optional[Path], state_code: Optional[str] = None
) -> Optional[str]:
    """Locate the pre-LLM text the content checker expects as its baseline."""
    candidates = []
    if pdf_path is not None:
        candidates.append(pdf_path.with_suffix("").parent / f"{pdf_path.stem}_hybrid_precleaned.txt")
    candidates.append(original.parent / f"{original.stem}_hybrid_precleaned.txt")
    candidates.append(original.parent.parent / "input" / f"{original.stem}_hybrid_precleaned.txt")

    for c in candidates:
        if c.exists():
            return c.read_text(encoding="utf-8")

    if pdf_path is not None and pdf_path.exists():
        try:
            from pymupdf_extractor import extract_pdf_with_pymupdf

            raw = extract_pdf_with_pymupdf(
                str(pdf_path), join_pages=True, state_code=state_code
            )
            return _preclean(raw, state_code) if raw else None
        except Exception as exc:  # noqa: BLE001
            log.warning("Fallback extraction failed for %s: %s", pdf_path, exc)
    return None


def _content_report(precleaned: Optional[str], output_text: str) -> Optional[dict[str, Any]]:
    if not precleaned:
        return None
    try:
        from content_accuracy_checker import check_content_accuracy

        return check_content_accuracy(precleaned, output_text)
    except Exception as exc:  # noqa: BLE001
        log.warning("Content accuracy check failed: %s", exc)
        return None


def _integrity_report(output_text: str, precleaned: Optional[str], state_code: Optional[str]):
    try:
        from pymupdf_extractor import build_tag_integrity_report

        return build_tag_integrity_report(
            output_text, state_code=state_code, reference_text=precleaned
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Tag integrity report failed: %s", exc)
        return None


def _layout_report(pdf_path: Optional[Path], output_text: str, state_code: Optional[str]):
    """Paragraph-boundary fidelity — needs the source PDF for layout geometry."""
    if pdf_path is None or not pdf_path.exists():
        return None
    try:
        from paragraph_accuracy_checker import check_paragraph_accuracy

        return check_paragraph_accuracy(str(pdf_path), output_text, state_code=state_code)
    except Exception as exc:  # noqa: BLE001
        log.warning("Paragraph accuracy check failed: %s", exc)
        return None


def _short_lines_kept_apart(lines: list[str], line_no: int) -> bool:
    """A "merge these two lines" suggestion for two short lines that each stand alone (an address or signature
    block, a heading over a name) is almost always intentional: the document is line-oriented there."""
    if not (1 <= line_no < len(lines)):
        return False
    a, b = lines[line_no - 1].strip(), lines[line_no].strip()
    return bool(a) and bool(b) and len(a) <= 60 and not a.endswith((",", ";")) and (b[:1].isupper() or b[:1].isdigit())


def _layout_rows(layout: Optional[dict[str, Any]], output_lines: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """Every paragraph-boundary problem the checker counted, so the list and the score agree.

    The checker's score counts two kinds: paragraphs merged in the output that should be split
    ("merge_errors"/"splits_needed") and paragraphs over-split that should be merged ("merges_needed")."""
    if not layout:
        return []
    rows = []
    for key in ("merge_errors", "split_errors", "issues"):
        for item in layout.get(key) or []:
            if isinstance(item, dict):
                rows.append({
                    "kind": item.get("type") or key.replace("_errors", ""),
                    "line": item.get("line"),
                    "related_line": item.get("related_line"),
                    "description": item.get("description") or item.get("reason") or "",
                    "context": item.get("context", ""),
                    "page": item.get("page"),
                })
    for item in layout.get("merges_needed") or []:
        if isinstance(item, dict) and item.get("line") is not None:
            rows.append({
                "kind": "split",  # the output paragraph was split where the PDF has one paragraph
                "line": item.get("line"),
                "related_line": int(item["line"]) + 1,
                "description": item.get("description") or "",
                "context": "",
                "page": item.get("page"),
                "likely_ok": _short_lines_kept_apart(output_lines or [], int(item["line"])),
            })
    return rows


def adjusted_layout_accuracy(layout: Optional[dict[str, Any]], rows: list[dict[str, Any]]) -> Optional[float]:
    """Layout accuracy without the "over-split" flags that are just short lines kept apart on purpose."""
    if not layout:
        return None
    raw = layout.get("accuracy_pct")
    checkpoints = (layout.get("boundaries_checked") or 0) + (layout.get("merge_checkpoints") or 0)
    if raw is None or not checkpoints or layout.get("boundary_detection_failed"):
        return raw
    ignored = sum(1 for r in rows if r.get("likely_ok"))
    counted = len(layout.get("splits_needed") or []) + len(layout.get("merges_needed") or []) - ignored
    return max(round(max(0.0, (checkpoints - counted) / checkpoints * 100), 1), raw)


_WHY = {
    "content_drop": "Words that are in the text extracted from the PDF are not in the converted text.",
    "paragraph_drop": "A whole paragraph from the PDF extraction is missing from the converted text.",
    "content_added": "These words are in the converted text but were not in the text extracted from the "
                     "PDF — check the model did not invent them.",
}
_WHY["content_reordered"] = (
    "The same words are in both the PDF extraction and the converted text, but in a different order. "
    "This usually happens where struck-through or underlined text sits between wrapped lines. Check "
    "that the converted order matches the PDF; if it does, mark it reviewed."
)
_WHY_CHANGED = (
    "The converted text reads differently from the text extracted from the PDF here. If the converted "
    "text is right when you check it against the PDF, mark it reviewed."
)


def _words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", s.lower())


def _merge_reordered(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Text that was "added" in one place and "missing" in another, with the same words, is one reordered block,
    not an invention plus a loss. Merge the pair into a single finding that shows both."""
    added = [r for r in rows if not r["expected"].strip() and r["output"].strip()]
    dropped = [r for r in rows if r["expected"].strip() and not r["output"].strip()]
    used: set[int] = set()
    for a in added:
        ta = Counter(_words(a["output"]))
        na = sum(ta.values())
        if na < 8:
            continue
        best: Optional[tuple[float, dict[str, Any]]] = None
        for d in dropped:
            if id(d) in used:
                continue
            td = Counter(_words(d["expected"]))
            overlap = sum((ta & td).values()) / max(na, sum(td.values()))
            if overlap >= 0.85 and (best is None or overlap > best[0]):
                best = (overlap, d)
        if best:
            d = best[1]
            used.add(id(d))
            a.update(expected=d["expected"], category="content_reordered", type="reordered", severity="warning",
                     words_affected=max(na, sum(Counter(_words(d["expected"])).values())),
                     explanation="Same words, different order.", why=_WHY["content_reordered"])
    return [r for r in rows if id(r) not in used]


def adjusted_content_accuracy(content: Optional[dict[str, Any]], issue_rows: list[dict[str, Any]]) -> Optional[float]:
    """Content accuracy that matches what the findings list shows.

    The checker's own number is plain text similarity (matching words / the longer text), so it is also pulled
    down by things that are not defects: page footers the conversion correctly drops, and a block of text that
    only moved. Here only the words in the findings that are still listed count against the score (a reordered
    block counts as moved, not lost). It is never lower than the checker's own number."""
    if not content:
        return None
    raw = content.get("accuracy_pct")
    base = content.get("precleaned_words") or 0
    if raw is None or not base:
        return raw
    unexplained = sum(int(r.get("words_affected") or 0) for r in issue_rows if r.get("category") != "content_reordered")
    adjusted = round(max(0.0, 100.0 * (1 - unexplained / base)), 2)
    return max(adjusted, raw)


def _issue_rows(content: Optional[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Flatten the checker's differences into table rows for the UI.

    Returns (rows, hidden). `hidden` are differences that are only page furniture in the
    source extraction (a timestamp footer, a barcode…) — the conversion is meant to drop
    those, so they are not defects; they are listed in the Summary for transparency.
    """
    if not content:
        return [], []
    rows: list[dict[str, Any]] = []
    hidden: list[dict[str, Any]] = []
    for idx, diff in enumerate(content.get("differences", [])):
        if diff.get("is_expected"):
            continue
        expected = diff.get("precleaned") or ""
        output = diff.get("output") or ""
        artifact = rules.explain_artifact(expected, output)
        if artifact:
            hidden.append({"id": idx, "expected": expected, "output": output, "reason": artifact})
            continue
        category = diff.get("category")
        rows.append({
            "id": idx,
            "type": diff.get("type"),
            "category": category,
            "severity": diff.get("severity", "warning"),
            "expected": expected,
            "output": output,
            "words_affected": diff.get("words_affected", 0),
            "explanation": diff.get("explanation", ""),
            "why": _WHY.get(category or "", _WHY_CHANGED),
        })
    return _merge_reordered(rows), hidden


def build_report(text_path: str, pdf_path: Optional[str], state_code: Optional[str]) -> dict[str, Any]:
    original = resolve_readable(text_path)
    pdf = resolve_readable(pdf_path) if pdf_path else None

    key, copy_path = working_copy(original)
    output_text = copy_path.read_text(encoding="utf-8")
    precleaned = _find_precleaned(original, pdf, state_code)

    content = _content_report(precleaned, output_text)
    integrity = _integrity_report(output_text, precleaned, state_code)
    layout = _layout_report(pdf, output_text, state_code)
    history = _load_history(key)

    # Persist the baseline and the report so locate.py can pin every finding to
    # a position without re-running the (slow) checkers.
    edit_dir = _edit_dir(key)
    if precleaned is not None:
        (edit_dir / "precleaned.txt").write_text(precleaned, encoding="utf-8")

    # House rules: one-tag-per-paragraph, no merged/split paragraphs and the
    # per-jurisdiction spec (rules.py). Error-level findings are real defects and
    # count against Tag Integrity; the extractor's baseline-comparison notes below
    # are informational — the source extraction fragments tags per line, so a
    # paragraph-level tag will always "differ" from that baseline.
    issue_rows, hidden_artifacts = _issue_rows(content)
    findings = rules.check_all(output_text, state_code, precleaned)
    by_rule: dict[str, dict[str, Any]] = {}
    for f in findings:
        slot = by_rule.setdefault(f["rule"], {"title": f["title"], "error": 0, "warning": 0, "info": 0})
        slot[f["severity"]] += 1
    rule_issues = [f"{v['error']}× {v['title']}" for v in by_rule.values() if v["error"]]
    raw_issues = integrity.get("issues", []) if integrity else []
    hard_issues = [m for m in raw_issues if not m.startswith(_BASELINE_NOTES)]
    notes = [m for m in raw_issues if m.startswith(_BASELINE_NOTES)]
    # The structural duplicates of what rules.py reports precisely.
    hard_issues = [m for m in hard_issues if not m.startswith(_COVERED_BY_RULES)]
    all_hard = hard_issues + rule_issues

    layout_rows = _layout_rows(layout, output_text.split("\n"))

    report = {
        "key": key,
        "state_code": state_code,
        "name": original.name,
        "original_path": str(original),
        "working_path": str(copy_path),
        "pdf_path": str(pdf) if pdf else None,
        "edited": bool(history),
        "has_precleaned_baseline": precleaned is not None,
        "content_accuracy_pct": adjusted_content_accuracy(content, issue_rows),
        "content_accuracy_raw_pct": content.get("accuracy_pct") if content else None,  # plain text similarity
        "layout_accuracy_pct": adjusted_layout_accuracy(layout, layout_rows),
        "layout_accuracy_raw_pct": layout.get("accuracy_pct") if layout else None,  # the checker's own number
        "content_summary": {
            "precleaned_words": content.get("precleaned_words") if content else None,
            "output_words": content.get("output_words") if content else None,
            "missing_words": content.get("missing_words") if content else None,
            "extra_words": content.get("extra_words") if content else None,
            "changed_words": content.get("changed_words") if content else None,
            "category_summary": content.get("category_summary") if content else {},
        },
        "integrity": {
            "severity": ("error" if all_hard else "pass") if integrity or findings is not None else None,
            "issues": all_hard,
            "notes": notes,
            "counts": integrity.get("counts", {}) if integrity else {},
            "boundary_drift": integrity.get("boundary_drift") if integrity else None,
        },
        "rules": {
            "errors": sum(1 for f in findings if f["severity"] == "error"),
            "warnings": sum(1 for f in findings if f["severity"] == "warning"),
            "info": sum(1 for f in findings if f["severity"] == "info"),
            "by_rule": by_rule,
        },
        "issues": issue_rows,
        "hidden_artifacts": hidden_artifacts,
        "layout_issues": layout_rows,
        "history": history,
    }
    (edit_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    return report


def load_saved_report(key: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """The last built report and pre-cleaned baseline for an edit session."""
    d = _edit_dir(key)
    report = precleaned = None
    try:
        report = json.loads((d / "report.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    try:
        precleaned = (d / "precleaned.txt").read_text(encoding="utf-8")
    except OSError:
        pass
    return report, precleaned


# ── Approval and submit state ────────────────────────────────────────────────
#
# Save -> Approve -> Submit. Edits are saved to the working copy as they happen.
# Approving snapshots that copy and records its hash; if the text changes after
# that, the approval is stale and Submit is blocked until it is approved again,
# so what reaches Keyed is always exactly what a reviewer approved.

def _working_path(key: str) -> Path:
    meta = json.loads((_edit_dir(key) / "meta.json").read_text(encoding="utf-8"))
    return _edit_dir(key) / meta["name"]


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _approval_file(key: str) -> Path:
    return _edit_dir(key) / "approval.json"


def _read_approval(key: str) -> dict[str, Any]:
    try:
        return json.loads(_approval_file(key).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def approval_status(key: str) -> dict[str, Any]:
    """Where this document is in Save -> Approve -> Submit."""
    if not (_edit_dir(key) / "meta.json").exists():
        raise FileNotFoundError("Unknown edit session")
    rec = _read_approval(key)
    current = _hash_file(_working_path(key))
    approved = bool(rec.get("hash")) and rec["hash"] == current
    submitted = bool(rec.get("submitted_hash")) and rec["submitted_hash"] == current
    return {
        "approved": approved,
        "stale": bool(rec.get("hash")) and not approved,  # approved once, edited since
        "approved_at": rec.get("approved_at") if approved else None,
        "submitted": submitted,
        "submitted_at": rec.get("submitted_at") if submitted else None,
        "submitted_to": rec.get("submitted_to") if submitted else None,
    }


def approve(key: str, job_id: Optional[str] = None) -> dict[str, Any]:
    """Snapshot the current working copy as the approved version."""
    import time

    work = _working_path(key)
    snap_dir = db.APPROVED_DIR / key
    snap_dir.mkdir(parents=True, exist_ok=True)
    snapshot = snap_dir / work.name
    shutil.copyfile(work, snapshot)
    # The approved snapshot is a durable output of review — keep it in S3 so it
    # survives pod restarts and is visible to every replica.
    storage.persist(snapshot)

    rec = _read_approval(key)
    rec.update({
        "hash": _hash_file(snapshot),
        "approved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "snapshot": str(snapshot),
    })
    _approval_file(key).write_text(json.dumps(rec, indent=2), encoding="utf-8")
    if job_id:
        db.mark_approved(job_id)
    return {**approval_status(key), "saved_to": str(snapshot)}


def approved_snapshot(key: str) -> Path:
    """The approved file, only if it still matches the working copy."""
    status = approval_status(key)
    if not status["approved"]:
        raise PermissionError(
            "Approve the latest version first — the text changed since it was approved."
            if status["stale"] else "Approve this document before submitting it."
        )
    snapshot = Path(_read_approval(key)["snapshot"])
    storage.ensure_local(snapshot)
    return snapshot


def mark_submitted(key: str, target: str) -> dict[str, Any]:
    import time

    rec = _read_approval(key)
    rec.update({
        "submitted_hash": rec["hash"],
        "submitted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "submitted_to": target,
    })
    _approval_file(key).write_text(json.dumps(rec, indent=2), encoding="utf-8")
    return approval_status(key)


def original_name(key: str) -> str:
    """The pipeline file name Keyed expects (e.g. 2026_..._0.txt)."""
    return _working_path(key).name


# ── Corrections ──────────────────────────────────────────────────────────────

def apply_fix(key: str, original_text: str, new_text: str) -> dict[str, Any]:
    """Replace the first occurrence of original_text in the working copy."""
    d = _edit_dir(key)
    meta_file = d / "meta.json"
    if not meta_file.exists():
        raise FileNotFoundError("Unknown edit session")
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    copy_path = d / meta["name"]

    text = copy_path.read_text(encoding="utf-8")
    if original_text not in text:
        raise ValueError("Original text not found — the file may have changed since review started")

    updated = text.replace(original_text, new_text, 1)
    copy_path.write_text(updated, encoding="utf-8")

    history = _load_history(key)
    history.append({"original": original_text, "replacement": new_text})
    _save_history(key, history)

    return {"applied": True, "edits": len(history)}


def save_text(key: str, new_text: str) -> dict[str, Any]:
    """Replace the whole working copy (inline editor). Recorded as one undo step."""
    d = _edit_dir(key)
    meta_file = d / "meta.json"
    if not meta_file.exists():
        raise FileNotFoundError("Unknown edit session")
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    copy_path = d / meta["name"]

    previous = copy_path.read_text(encoding="utf-8")
    if previous == new_text:
        return {"saved": False, "edits": len(_load_history(key))}

    copy_path.write_text(new_text, encoding="utf-8")
    history = _load_history(key)
    history.append({"original": previous, "replacement": new_text, "whole_file": True})
    _save_history(key, history)
    return {"saved": True, "edits": len(history)}


def undo_last(key: str) -> dict[str, Any]:
    d = _edit_dir(key)
    meta_file = d / "meta.json"
    if not meta_file.exists():
        raise FileNotFoundError("Unknown edit session")
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    copy_path = d / meta["name"]

    history = _load_history(key)
    if not history:
        return {"undone": False, "edits": 0}

    last = history.pop()
    text = copy_path.read_text(encoding="utf-8")
    if last.get("whole_file"):
        copy_path.write_text(last["original"], encoding="utf-8")
    elif last["replacement"] in text:
        text = text.replace(last["replacement"], last["original"], 1)
        copy_path.write_text(text, encoding="utf-8")
    _save_history(key, history)

    return {"undone": True, "edits": len(history)}


def reset(key: str) -> dict[str, Any]:
    """Discard all edits by restoring the working copy from the original."""
    d = _edit_dir(key)
    meta_file = d / "meta.json"
    if not meta_file.exists():
        raise FileNotFoundError("Unknown edit session")
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    shutil.copyfile(meta["original"], d / meta["name"])
    _save_history(key, [])
    return {"reset": True, "edits": 0}
