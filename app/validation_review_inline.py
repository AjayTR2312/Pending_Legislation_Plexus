"""
validation_review_inline.py
============================
Embeddable Streamlit component for displaying GPT audit results inline
after processing a single file.  Extracted and adapted from the standalone
``Opus4.6/validation_review.py`` so it can be called from
``streamlit_app.py``'s ``show_results_section()``.

Usage:
    from validation_review_inline import render_inline_validation_review
    render_inline_validation_review(
        excel_path      = "…_gpt_comparison.xlsx",
        converted_txt   = "…the converted text string…",
        highlighted_dir = Path("…_highlighted/"),
        json_report     = { … },           # from extract_pdf_json_report()
    )
"""
from __future__ import annotations

import base64
import io
import json
import re
import sys
from pathlib import Path
from typing import Optional

import streamlit as st
import streamlit.components.v1 as components
import openpyxl

# Optional import of IN decorative-header detector — used by the
# retroactive verdict normalization in :func:`_load_excel` to promote
# stale ``tag_dropped`` rows that are actually expected IN tag-drops
# (bill title / decorative rule / committee name / etc.) per the IN
# clean-prompt.  See :func:`_is_in_decorative_header` in
# ``Opus4.6/pymupdf_compare.py``.  The import is wrapped so missing
# sibling files (e.g. when the audit module isn't installed in the
# current Python path) don't break the validation-review UI.
try:
    _opus_dir = (Path(__file__).resolve().parent.parent / "Opus4.6").resolve()
    if _opus_dir.is_dir() and str(_opus_dir) not in sys.path:
        sys.path.insert(0, str(_opus_dir))
    from pymupdf_compare import (  # type: ignore
        _is_in_decorative_header as _in_is_decorative_header,
        _committee_headers_from_clean_text as _in_committee_headers,
    )
except Exception:  # pragma: no cover — defensive
    _in_is_decorative_header = None  # type: ignore
    _in_committee_headers = None  # type: ignore

# Bidirectional HTML component — enables JS-to-Python communication for clickable table rows
try:
    _VRI_CLICK_DIR = Path(__file__).parent / "vri_click_component"
    _vri_click_bridge = components.declare_component("vri_click_bridge", path=str(_VRI_CLICK_DIR))
except Exception:
    _vri_click_bridge = None

# ═══════════════════════════════════════════════════════════════════
# CUSTOM CSS
# ═══════════════════════════════════════════════════════════════════
_CSS = """
<style>
/* ── Equal-height two-column layout (PDF page ↔ Claude text) ── */
.st-key-vri_twocol [data-testid="stHorizontalBlock"] {
    align-items: stretch !important;
}

/* ── Compact page-nav buttons ── */
.st-key-vri_twocol button[kind="secondary"] {
    padding: 0.25rem 0.7rem !important;
    min-height: 2rem !important;
    font-size: 1rem !important;
}

/* --- summary cards --- */
.vr-metric-row { display:flex; gap:12px; margin-bottom:16px; flex-wrap:wrap; }
.vr-metric-card {
    flex:1; min-width:100px; padding:16px 20px; border-radius:10px;
    text-align:center; box-shadow: 0 1px 4px rgba(0,0,0,.08);
}
.vr-metric-card h2 { margin:0; font-size:2rem; line-height:1.2; }
.vr-metric-card p  { margin:4px 0 0; font-size:0.8rem; text-transform:uppercase; letter-spacing:.05em; }
.vr-mc-pass   { background:#dcfce7; color:#166534; }
.vr-mc-warn   { background:#fef9c3; color:#854d0e; }
.vr-mc-err    { background:#fee2e2; color:#991b1b; }
.vr-mc-total  { background:#e0e7ff; color:#3730a3; }
.vr-mc-fixed  { background:#dbeafe; color:#1e40af; }
.vr-mc-acc-hi { background:#dcfce7; color:#166534; }
.vr-mc-acc-md { background:#fef9c3; color:#854d0e; }
.vr-mc-acc-lo { background:#fee2e2; color:#991b1b; }

/* error nav */
.vr-error-nav-bar {
    display:flex; align-items:center; justify-content:center; gap:16px;
    background: linear-gradient(135deg, #fef3c7, #fde68a);
    border: 2px solid #f59e0b;
    border-radius: 12px; padding: 12px 24px; margin: 12px 0 16px;
    box-shadow: 0 2px 8px rgba(245,158,11,.2);
    flex-wrap: wrap;
}
.vr-error-nav-bar .nav-label { font-weight: 700; font-size: 1.05rem; color: #92400e; white-space:nowrap; }
.vr-error-nav-bar .nav-page  { font-size: 0.85rem; color: #78350f; opacity:0.8; white-space:nowrap; }

/* ── Responsive: small screens ─────────────────────────────── */
@media (max-width: 900px) {
    /* Stack the two-column PDF/Claude layout vertically */
    .st-key-vri_twocol [data-testid="stHorizontalBlock"] {
        flex-direction: column !important;
    }
    .st-key-vri_twocol .stColumn {
        width: 100% !important;
        flex: none !important;
    }
    .vr-text-viewer {
        font-size: 0.78rem;
        line-height: 1.5;
    }
    /* Metric cards: allow 2-per-row */
    .vr-metric-card {
        min-width: 80px;
        padding: 10px 12px;
    }
    .vr-metric-card h2 { font-size: 1.5rem; }
    /* Error nav bar */
    .vr-error-nav-bar {
        padding: 8px 12px;
        gap: 8px;
    }
    .vr-error-nav-bar .nav-label { font-size: 0.9rem; }
}

/* checklist table */
.vr-cl-table { width:100%; border-collapse:collapse; font-size:0.82rem; }
.vr-cl-table th {
    background:#1e293b; color:white; padding:8px 10px;
    text-align:left; position:sticky; top:0; z-index:2;
    border-bottom: 2px solid #334155;
}
.vr-cl-table td { padding:7px 10px; border-bottom:1px solid #e2e8f0; vertical-align:top; }
.vr-cl-table tr:hover { background:#f0f9ff !important; cursor:pointer; }
.vr-cl-table tr.row-pass  { background:#f0fdf4; }
.vr-cl-table tr.row-warn  { background:#fefce8; }
.vr-cl-table tr.row-err   { background:#fef2f2; }
.vr-cl-table tr.row-fixed { background:#dbeafe; }
.vr-cl-table tr.row-selected { outline: 2px solid #3b82f6; background:#eff6ff !important; }
.vr-badge {
    display:inline-block; padding:2px 8px; border-radius:12px;
    font-weight:600; font-size:0.75rem; white-space: nowrap;
}
.vr-badge-pass  { background:#bbf7d0; color:#166534; }
.vr-badge-warn  { background:#fde68a; color:#854d0e; }
.vr-badge-err   { background:#fecaca; color:#991b1b; }
.vr-badge-fixed { background:#93c5fd; color:#1e3a8a; }

/* text viewer — scrollable, capped height; JS refines to match the PDF image */
.vr-text-viewer {
    font-family: 'Cascadia Code','Consolas', monospace;
    font-size: 0.82rem; line-height: 1.65;
    white-space: pre-wrap; word-break: break-word;
    background: #fafafa; border: 1px solid #e2e8f0;
    border-radius: 8px; padding: 16px;
    overflow-y: auto;
    min-height: 400px;
    max-height: 80vh;   /* fallback cap — JS will refine to exact image height */
}
.vr-tag-add { background:#bbf7d0; color:#166534; padding:1px 2px; border-radius:3px; }
.vr-tag-del { background:#fecaca; color:#991b1b; padding:1px 2px; border-radius:3px; text-decoration:line-through; }
.vr-span-highlight-pass { background:#86efac; padding:1px 3px; border-radius:3px; }
.vr-span-highlight-warn { background:#fde047; padding:1px 3px; border-radius:3px; }
.vr-span-highlight-err  { background:#fca5a5; padding:1px 3px; border-radius:3px; }
mark.vr-current-span {
    background:#60a5fa !important; color:white !important;
    padding: 2px 4px; border-radius: 4px;
    animation: vr-pulse 1.5s ease-in-out infinite alternate;
}
@keyframes vr-pulse { from {box-shadow: 0 0 0 0 rgba(96,165,250,.5)} to {box-shadow: 0 0 0 6px rgba(96,165,250,0)} }

/* zoom container */
.vr-zoom-container {
    border: 2px solid #f59e0b; border-radius: 10px;
    overflow: hidden; background: white;
    box-shadow: 0 2px 8px rgba(245,158,11,.2);
    margin: 8px 0;
}
.vr-zoom-header {
    background: #fef3c7; padding: 6px 12px;
    font-size: 0.8rem; font-weight: 600; color: #92400e;
    border-bottom: 1px solid #fde68a;
}

/* additional findings */
.vr-af-card {
    border-left: 4px solid; border-radius: 6px;
    padding: 12px 16px; margin-bottom: 10px;
    background: white; box-shadow: 0 1px 3px rgba(0,0,0,.06);
}
.vr-af-error  { border-color: #ef4444; }
.vr-af-warn   { border-color: #f59e0b; }

/* inline edit text area */
div[data-testid="stTextArea"] textarea {
    font-family: 'Cascadia Code','Consolas', monospace !important;
    font-size: 0.85rem !important; line-height: 1.6 !important;
}
</style>
"""


# ═══════════════════════════════════════════════════════════════════
# DATA LOADING
# ═══════════════════════════════════════════════════════════════════

def _load_excel(xlsx_path: Path) -> tuple[list[dict], list[dict], dict]:
    """Load checklist rows + additional findings + summary from the Excel."""
    wb = openpyxl.load_workbook(str(xlsx_path), read_only=True, data_only=True)

    ws1 = wb["Checklist Audit"]
    rows = list(ws1.iter_rows(min_row=4, values_only=True))
    if not rows:
        wb.close()
        return [], [], {}
    headers = [str(h).strip() if h else f"col{i}" for i, h in enumerate(rows[0])]
    checklist = []
    for row in rows[1:]:
        if row[0] is None:
            continue
        d = {}
        for i, h in enumerate(headers):
            d[h] = row[i] if i < len(row) else ""
        checklist.append(d)

    findings = []
    if "Additional Findings" in wb.sheetnames:
        ws2 = wb["Additional Findings"]
        rows2 = list(ws2.iter_rows(min_row=3, values_only=True))
        if rows2:
            h2 = [str(h).strip() if h else f"col{i}" for i, h in enumerate(rows2[0])]
            for row in rows2[1:]:
                if row[0] is None or str(row[0]).strip() == "—":
                    continue
                d = {}
                for i, h in enumerate(h2):
                    d[h] = row[i] if i < len(row) else ""
                findings.append(d)

    # ── Retroactive verdict normalization ───────────────────────────
    # PyMuPDF false-positive classifications used to resolve to
    # "⚠️ warning"; the audit policy now treats them as "✅ pass" because
    # the LLM correctly stripped the false positive (the cleaned output
    # is right, nothing for the user to fix).  Promote any legacy rows
    # in older Excel files so the user sees the same verdicts everywhere
    # without having to re-run the audit.
    _LEGACY_FALSE_POSITIVE_TO_PASS = {
        "other_false_positive",
        "table_false_positive",
    }
    for r in checklist:
        cls = str(r.get("Classification", "") or "").strip().lower()
        verd = str(r.get("Verdict", "") or "")
        if cls in _LEGACY_FALSE_POSITIVE_TO_PASS and verd.startswith("⚠"):
            r["Verdict"] = "✅ pass"
            existing_notes = str(r.get("Notes", "") or "").strip()
            promotion_tag = "[Auto-promoted to pass: LLM correctly stripped a PyMuPDF false positive.]"
            if promotion_tag not in existing_notes:
                r["Notes"] = (
                    f"{existing_notes}  {promotion_tag}".strip()
                    if existing_notes else promotion_tag
                )

    # ── Retroactive: IN decorative-header tag drops are EXPECTED ────
    # The IN clean-prompt instructs the LLM to STRIP [A>...<A] tags
    # from decorative headers (bill title, version label, A BILL FOR AN
    # ACT, digest heading, decorative rules, section labels, committee
    # names) while preserving the inner text.  Older Excel audits
    # classified these as ``tag_dropped`` (❌ error) — the new pipeline
    # classifies them as ``decorative_header_dropped`` (✅ pass).  This
    # retroactive pass promotes the stale rows so the user sees the
    # corrected verdicts without re-running the audit.
    #
    # State detection: the bill PDF / audit Excel both live in a
    # session folder named after the filename schema
    # ``YYYY_YYYY_<state>_N_N_NNNNNN_...`` where ``<state>`` is the
    # numeric state code.  IN = 14.
    if _in_is_decorative_header is not None:
        state_num: str = ""
        name_parts = xlsx_path.name.split("_")
        if len(name_parts) >= 4:
            state_num = name_parts[2]
        if state_num == "14":   # Indiana
            # Try to load the sibling cleaned text for committee-name
            # cross-validation.  The audit Excel is named
            # ``<stem>_clean_audit.xlsx`` and the cleaned text is
            # ``<stem>_clean.txt`` in the same folder.
            committee_names: set = set()
            try:
                cleaned_path = Path(
                    str(xlsx_path).replace("_clean_audit.xlsx", "_clean.txt")
                )
                if cleaned_path.is_file() and _in_committee_headers is not None:
                    cleaned_text = cleaned_path.read_text(
                        encoding="utf-8", errors="ignore"
                    )
                    committee_names = _in_committee_headers(cleaned_text)
            except Exception:  # pragma: no cover — defensive
                committee_names = set()

            n_promoted = 0
            for r in checklist:
                cls = str(r.get("Classification", "") or "").strip().lower()
                verd = str(r.get("Verdict", "") or "")
                if cls != "tag_dropped" or not verd.startswith("❌"):
                    continue
                tag_type = str(r.get("Tag Type", "") or "").strip().lower()
                if tag_type and tag_type != "addition":
                    continue   # decorative styling never produces strikethroughs
                pymupdf_text = str(r.get("PyMuPDF Text", "") or "")
                if not pymupdf_text:
                    continue
                try:
                    if _in_is_decorative_header(
                        pymupdf_text, committee_names=committee_names
                    ):
                        r["Verdict"] = "✅ pass"
                        r["Classification"] = "decorative_header_dropped"
                        existing_notes = str(r.get("Notes", "") or "").strip()
                        promotion_tag = (
                            "[Auto-promoted to pass: IN decorative header "
                            "— bold/underlined styling is decorative "
                            "typography, NOT legislative markup; the IN "
                            "clean-prompt instructs the LLM to strip the "
                            "[A>...<A] tag while preserving the text.]"
                        )
                        if promotion_tag not in existing_notes:
                            r["Notes"] = (
                                f"{existing_notes}  {promotion_tag}".strip()
                                if existing_notes else promotion_tag
                            )
                        n_promoted += 1
                except Exception:  # pragma: no cover — defensive
                    pass
            if n_promoted:
                import logging as _log
                _log.info(
                    "🏛️  IN audit: retroactively promoted %d tag_dropped "
                    "row(s) to ✅ pass (decorative header per IN prompt)",
                    n_promoted,
                )

    n_pass = sum(1 for r in checklist if str(r.get("Verdict", "")).startswith("✅"))
    n_warn = sum(1 for r in checklist if str(r.get("Verdict", "")).startswith("⚠"))
    n_err  = sum(1 for r in checklist if str(r.get("Verdict", "")).startswith("❌"))
    summary = {
        "total": len(checklist), "pass": n_pass, "warn": n_warn,
        "error": n_err, "additional": len(findings),
    }
    wb.close()
    return checklist, findings, summary


def _get_page_images(highlighted_dir: Path) -> list[Path]:
    if not highlighted_dir or not highlighted_dir.exists():
        return []
    return sorted(highlighted_dir.glob("page_*.png"))



# ═══════════════════════════════════════════════════════════════════
# TEXT RENDERING
# ═══════════════════════════════════════════════════════════════════

def _html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ─── Backward-compatible column readers ────────────────────────────
# The PyMuPDF Clean Audit (pymupdf_compare.py) renamed two columns to
# better reflect what they hold:
#     "Claude Text"   →  "Clean Text"        (the PyMuPDF Clean output tag)
#     "In Claude?"    →  "In Clean Output?"  (was the tag preserved?)
# Older audits (and gpt_compare.py) still use the legacy names, so these
# helpers try the new name first and fall back to the old one.
def _row_clean_text(row: dict) -> str:
    """Return the tag from the cleaned output (was 'Claude Text')."""
    val = row.get("Clean Text", "")
    if val:
        return str(val)
    return str(row.get("Claude Text", "") or "")


def _row_in_clean(row: dict) -> str:
    """Return the in-clean status (was 'In Claude?')."""
    val = row.get("In Clean Output?", "")
    if val:
        return str(val)
    return str(row.get("In Claude?", "") or "")


def _render_text_html(text: str, checklist: list[dict],
                      selected_id: int | None = None,
                      filter_mode: str = "all",
                      fixed_ids: set[int] | None = None) -> str:
    fixed_ids = fixed_ids or set()
    highlights: list[dict] = []
    for row in checklist:
        verdict = str(row.get("Verdict", ""))
        cid = int(row.get("#", 0))

        if filter_mode == "errors" and not verdict.startswith("❌"):
            continue
        if filter_mode == "warnings" and not verdict.startswith("⚠"):
            continue

        pymupdf_text = str(row.get("PyMuPDF Text", "")).strip()
        claude_text = _row_clean_text(row).strip()

        if cid in fixed_ids:
            css = "vr-span-highlight-pass"
        elif verdict.startswith("✅"):
            css = "vr-span-highlight-pass"
        elif verdict.startswith("⚠"):
            css = "vr-span-highlight-warn"
        else:
            css = "vr-span-highlight-err"

        highlights.append({
            "search": claude_text or pymupdf_text,
            "css": css, "cid": cid,
            "selected": (cid == selected_id),
        })

    safe = _html_escape(text)
    safe = re.sub(
        r"\[A&gt;(.*?)&lt;A\]",
        r'<span class="vr-tag-add">[A&gt;\1&lt;A]</span>',
        safe, flags=re.DOTALL,
    )
    safe = re.sub(
        r"\[D&gt;(.*?)&lt;D\]",
        r'<span class="vr-tag-del">[D&gt;\1&lt;D]</span>',
        safe, flags=re.DOTALL,
    )

    sorted_hl = sorted(highlights, key=lambda h: h["selected"])
    for hl in sorted_hl:
        needle = _html_escape(hl["search"])
        if not needle:
            continue
        if hl["selected"]:
            tag = f'<mark class="vr-current-span" id="span-{hl["cid"]}">'
            end_tag = "</mark>"
        else:
            tag = f'<span class="{hl["css"]}">'
            end_tag = "</span>"

        # Try to match the needle within an existing tag span first, so we
        # wrap the *whole* tag span (e.g. <span class="vr-tag-add">[A>…<A]</span>)
        # instead of just the inner text — which would create broken nested HTML.
        wrapped_add = f'<span class="vr-tag-add">[A&gt;{needle}&lt;A]</span>'
        wrapped_del = f'<span class="vr-tag-del">[D&gt;{needle}&lt;D]</span>'

        if wrapped_add in safe:
            safe = safe.replace(wrapped_add, f"{tag}{wrapped_add}{end_tag}", 1)
        elif wrapped_del in safe:
            safe = safe.replace(wrapped_del, f"{tag}{wrapped_del}{end_tag}", 1)
        else:
            # Fallback: plain text match (for items not inside tag spans)
            safe = safe.replace(needle, f"{tag}{needle}{end_tag}", 1)

    return safe


# ═══════════════════════════════════════════════════════════════════
# ERROR NAVIGATION HELPERS
# ═══════════════════════════════════════════════════════════════════

def _get_error_ids(checklist: list[dict], fixed_ids: set[int],
                   ignored_ids: set[int] | None = None) -> list[int]:
    ignored_ids = ignored_ids or set()
    ids = []
    for r in checklist:
        verdict = str(r.get("Verdict", ""))
        cid = int(r.get("#", 0))
        if cid in fixed_ids or cid in ignored_ids:
            continue
        if verdict.startswith("❌"):
            ids.append(cid)
    return ids


def _get_error_position(error_ids: list[int], selected_id: int) -> tuple[int, int]:
    if selected_id in error_ids:
        idx = error_ids.index(selected_id) + 1
        return idx, len(error_ids)
    return 0, len(error_ids)


# ═══════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════

def _get_fixed_ids() -> set[int]:
    return set(st.session_state.get("vri_fixed_ids", []))


def _mark_fixed(cid: int):
    fixed = list(st.session_state.get("vri_fixed_ids", []))
    if cid not in fixed:
        fixed.append(cid)
    st.session_state["vri_fixed_ids"] = fixed


def _unmark_fixed(cid: int):
    fixed = list(st.session_state.get("vri_fixed_ids", []))
    if cid in fixed:
        fixed.remove(cid)
    st.session_state["vri_fixed_ids"] = fixed


def _get_ignored_ids() -> set[int]:
    return set(st.session_state.get("vri_ignored_ids", []))


def _mark_ignored(cid: int):
    ignored = list(st.session_state.get("vri_ignored_ids", []))
    if cid not in ignored:
        ignored.append(cid)
    st.session_state["vri_ignored_ids"] = ignored


def _unmark_ignored(cid: int):
    ignored = list(st.session_state.get("vri_ignored_ids", []))
    if cid in ignored:
        ignored.remove(cid)
    st.session_state["vri_ignored_ids"] = ignored


def _save_undo_history(item_ids: list[int], original_context: str,
                       new_text: str, file_path: str):
    history = list(st.session_state.get("vri_undo_history", []))
    history.append({
        "item_ids": item_ids,
        "original_context": original_context,
        "new_text": new_text,
        "file_path": file_path,
    })
    st.session_state["vri_undo_history"] = history


def _get_undo_record(cid: int) -> dict | None:
    for rec in reversed(st.session_state.get("vri_undo_history", [])):
        if cid in rec["item_ids"]:
            return rec
    return None


def _get_effective_verdict(row: dict, fixed_ids: set[int],
                          ignored_ids: set[int] | None = None) -> str:
    cid = int(row.get("#", 0))
    if cid in fixed_ids:
        return "✅ fixed"
    if ignored_ids and cid in ignored_ids:
        return "✅ ignored"
    return str(row.get("Verdict", ""))


def _trunc(s, n=120):
    s = str(s) if s else ""
    return s if len(s) <= n else s[:n] + "…"


def _safe_int(val, default=0) -> int:
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _extract_edit_context(full_text: str, search_text: str,
                          context_chars: int = 600,
                          target_page: int = 0,
                          total_pages: int = 0,
                          hint_pos: int = -1) -> str:
    if not search_text:
        return search_text

    # If caller already knows the exact position, use it directly
    if hint_pos >= 0 and full_text[hint_pos:hint_pos + len(search_text)] == search_text:
        pos = hint_pos
    else:
        # Find ALL occurrences and pick the one nearest to the estimated page position
        positions = []
        start_search = 0
        while True:
            pos = full_text.find(search_text, start_search)
            if pos == -1:
                break
            positions.append(pos)
            start_search = pos + 1

        if not positions:
            return search_text

        if hint_pos >= 0:
            # hint_pos given but exact match failed — pick nearest to hint
            pos = min(positions, key=lambda p: abs(p - hint_pos))
        elif len(positions) == 1 or target_page <= 0 or total_pages <= 0:
            pos = positions[0]
        else:
            # Estimate where the target page starts in the text
            estimated_pos = int((target_page - 1) / total_pages * len(full_text))
            # Pick the occurrence nearest to the estimated position
            pos = min(positions, key=lambda p: abs(p - estimated_pos))

    raw_start = max(0, pos - context_chars)
    if raw_start > 0:
        sp = full_text.find(' ', raw_start)
        start = (sp + 1) if sp != -1 and sp < pos else raw_start
    else:
        start = 0

    end_of_match = pos + len(search_text)
    raw_end = min(len(full_text), end_of_match + context_chars)
    if raw_end < len(full_text):
        sp = full_text.rfind(' ', end_of_match, raw_end + 1)
        end = sp if sp != -1 and sp > end_of_match else raw_end
    else:
        end = len(full_text)

    return full_text[start:end]


def _find_affected_items(checklist: list[dict], fixed_ids: set[int],
                         original_context: str, new_text: str,
                         current_cid: int, current_page: int = 0) -> list[int]:
    affected = []
    for row in checklist:
        cid = int(row.get("#", 0))
        if cid == current_cid:
            continue  # always added at the end
        if cid in fixed_ids:
            continue
        verdict = str(row.get("Verdict", ""))
        if not verdict.startswith("❌"):
            continue
        ct = _row_clean_text(row).strip()
        if not ct or len(ct) < 10:
            continue  # skip very short text — too many false positives
        # Only consider items on the same page
        item_page = int(row.get("Page", 0) or 0)
        if current_page > 0 and item_page > 0 and item_page != current_page:
            continue
        if ct in original_context:
            if ct not in new_text:
                affected.append(cid)
    # Always include the current item
    affected.append(current_cid)
    return affected


def _ws_find(needle: str, haystack: str, unique: bool = False):
    """Find *needle* in *haystack* allowing flexible whitespace.
    Returns (matched_text, position) or ('', -1) if not found.
    If unique=True, only returns a match if it appears exactly once."""
    _EMPTY = ("", -1)
    if not needle or len(needle) < 3:
        return _EMPTY
    # Try literal first (fast path)
    if needle in haystack:
        if unique and haystack.count(needle) > 1:
            return _EMPTY
        return (needle, haystack.find(needle))
    # Build regex: collapse each whitespace run in needle to \s+
    import re as _re
    tokens = needle.split()
    if len(tokens) >= 2:
        pattern = r'\s+'.join(_re.escape(t) for t in tokens)
        matches = list(_re.finditer(pattern, haystack))
        if matches:
            if unique and len(matches) > 1:
                return _EMPTY
            return (matches[0].group(0), matches[0].start())
    # Try Unicode-normalized: replace common lookalikes
    _norm = needle.replace("\u00a7", "§").replace("\u2019", "'").replace("\u2018", "'")
    _norm = _norm.replace("\u201c", '"').replace("\u201d", '"').replace("\u2014", "-")
    _norm = _norm.replace("\u2013", "-").replace("\u00a0", " ")
    if _norm != needle and _norm in haystack:
        if unique and haystack.count(_norm) > 1:
            return _EMPTY
        return (_norm, haystack.find(_norm))
    # Try with all non-ASCII stripped from both sides
    _ascii_needle = ''.join(c if ord(c) < 128 else '.' for c in needle)
    if len(_ascii_needle) >= 10:
        _pat = _re.escape(_ascii_needle).replace(r'\.', '.')
        _m = list(_re.finditer(_pat, haystack))
        if _m:
            if unique and len(_m) > 1:
                return _EMPTY
            return (_m[0].group(0), _m[0].start())
    return _EMPTY


def _find_anchor_context_multi(text: str, row: dict, total_pages: int = 0) -> str:
    """Try multiple strategies to find anchor context in the converted text.

    Returns the anchor context string (a substring of *text* that can be
    used as the search-and-replace target), or empty string if nothing found.
    """
    target_page = int(row.get("Page", 0) or 0)
    _pg = {"target_page": target_page, "total_pages": total_pages}

    def _ws_find_context(needle, haystack, unique=False, **pg_kwargs):
        """Combine _ws_find + _extract_edit_context: find needle in haystack
        with flexible whitespace and return the edit context, or '' if not found."""
        hit, hit_pos = _ws_find(needle, haystack, unique=unique)
        if hit:
            return _extract_edit_context(haystack, hit, hint_pos=hit_pos, **pg_kwargs)
        return ""

    # Strategy 1: Full Context column
    ctx_col = str(row.get("Context", "") or "").strip()
    if ctx_col and ctx_col in text:
        return ctx_col

    # Strategy 1b: Context column with >>> <<< markers stripped
    if ctx_col:
        import re as _re
        # Extract text between >>> and <<< markers as focused search phrase
        marker_match = _re.search(r'>>>\s*(.+?)\s*<<<', ctx_col)
        if marker_match:
            inner = marker_match.group(1).strip()
            before_marker = ctx_col[:marker_match.start()].replace("…", "").strip()
            after_marker = ctx_col[marker_match.end():].replace("…", "").strip()

            # For very short inner text (≤3 chars like ",", ")", "."),
            # always use surrounding context to disambiguate — never search by inner alone
            if len(inner) <= 3:
                _after_full = after_marker.strip()
                _before_full = before_marker.strip()

                # Try: before_tail + inner + after — prefer UNIQUE matches
                if _before_full and _after_full:
                    tail = _before_full[-40:].strip()
                    for after_len in [len(_after_full), 80, 60, 45, 30]:
                        head = _after_full[:after_len].strip()
                        if not head:
                            continue
                        for combo in [tail + inner + head, tail + " " + inner + " " + head]:
                            r = _ws_find_context(combo, text, unique=True, **_pg)
                            if r: return r
                    for after_len in [len(_after_full), 60, 45, 30]:
                        head = _after_full[:after_len].strip()
                        if not head:
                            continue
                        for combo in [tail + inner + head, tail + " " + inner + " " + head]:
                            r = _ws_find_context(combo, text, **_pg)
                            if r: return r
                if _after_full:
                    for after_len in [len(_after_full), 80, 60, 45, 30]:
                        head = _after_full[:after_len].strip()
                        if not head:
                            continue
                        r = _ws_find_context(inner + " " + head, text, unique=True, **_pg)
                        if r: return r
                    for after_len in [len(_after_full), 60, 45, 30]:
                        head = _after_full[:after_len].strip()
                        if not head:
                            continue
                        for combo in [inner + head, inner + " " + head]:
                            r = _ws_find_context(combo, text, **_pg)
                            if r: return r
                if _before_full:
                    tail = _before_full[-40:].strip()
                    for combo in [tail + inner, tail + " " + inner]:
                        r = _ws_find_context(combo, text, **_pg)
                        if r: return r
                if _before_full and _after_full:
                    tail = _before_full[-40:].strip()
                    for after_len in [len(_after_full), 80, 60, 40, 30]:
                        head = _after_full[:after_len].strip()
                        if not head:
                            continue
                        r = _ws_find_context(tail + " " + head, text, unique=True, **_pg)
                        if r: return r
                    for after_len in [len(_after_full), 60, 40, 30]:
                        head = _after_full[:after_len].strip()
                        if not head:
                            continue
                        for combo in [tail + " " + head, tail + head]:
                            r = _ws_find_context(combo, text, **_pg)
                            if r: return r
            else:
                # Normal-length inner text
                if len(inner) >= 5:
                    r = _ws_find_context(inner, text, **_pg)
                    if r: return r

            # When inner is short-to-medium (< 10 chars), try inner + after_marker
            if after_marker and len(inner) < 10:
                for combo in [inner + " " + after_marker[:50].strip(),
                              inner + after_marker[:50].strip()]:
                    r = _ws_find_context(combo, text, **_pg)
                    if r: return r

            # Try combining tail of before + inner + head of after
            if before_marker:
                tail = before_marker[-40:].strip()
                r = _ws_find_context(tail + inner, text, **_pg)
                if r: return r
            # For missing text: use surrounding context WITHOUT inner
            if before_marker and after_marker:
                tail = before_marker[-30:].strip()
                head = after_marker[:30].strip()
                for combo in [tail + " " + head, tail + head]:
                    r = _ws_find_context(combo, text, **_pg)
                    if r: return r
                # Try shorter tails/heads in case of char mismatch at boundary
                for t_len in [25, 20, 15]:
                    for h_len in [25, 20, 15]:
                        t = before_marker[-t_len:].strip()
                        h = after_marker[:h_len].strip()
                        if len(t) >= 8 and len(h) >= 8:
                            r = _ws_find_context(t + " " + h, text, **_pg)
                            if r: return r
            if before_marker:
                tail = before_marker[-40:].strip()
                if len(tail) >= 10:
                    r = _ws_find_context(tail, text, **_pg)
                    if r: return r
                # Try progressively shorter
                for size in [30, 20, 15]:
                    chunk = before_marker[-size:].strip()
                    if len(chunk) >= 10:
                        r = _ws_find_context(chunk, text, **_pg)
                        if r: return r
            if after_marker:
                head = after_marker[:40].strip()
                if len(head) >= 10:
                    r = _ws_find_context(head, text, **_pg)
                    if r: return r
                # Try progressively shorter
                for size in [30, 20, 15]:
                    chunk = after_marker[:size].strip()
                    if len(chunk) >= 10:
                        r = _ws_find_context(chunk, text, **_pg)
                        if r: return r
        # Try full cleaned context
        ctx_clean = ctx_col.replace(">>>", "").replace("<<<", "").replace("…", "").strip()
        if ctx_clean and len(ctx_clean) >= 10:
            r = _ws_find_context(ctx_clean, text, **_pg)
            if r: return r

    # Strategy 2: Partial Context — first 80 chars
    if ctx_col:
        partial = ctx_col[:80].strip()
        if partial and partial in text:
            return _extract_edit_context(text, partial, **_pg)

    # Strategy 3: Extract quoted strings from Suggested Fix
    suggested_fix = str(row.get("Suggested Fix", "") or "")
    if suggested_fix:
        # Match single-quotes, double-quotes, and smart quotes
        quoted = re.findall(
            r"['\'\u2018\u2019\"\u201c\u201d]([^'\'\u2018\u2019\"\u201c\u201d]{3,})"
            r"['\'\u2018\u2019\"\u201c\u201d]",
            suggested_fix,
        )
        for q in quoted:
            if q in text:
                return _extract_edit_context(text, q, **_pg)

    # Strategy 4: Extract tag patterns from Suggested Fix  e.g. [A>or policies.<A]
    if suggested_fix:
        tag_pats = re.findall(r'\[([AD])>([^\[]{2,}?)<\1\]', suggested_fix)
        for _tag_letter, inner in tag_pats:
            if inner.strip() and inner.strip() in text:
                return _extract_edit_context(text, inner.strip(), **_pg)

    # Strategy 5: Key-phrase patterns from Suggested Fix
    if suggested_fix:
        for pat in [
            r"after\s+['\"\'\u2018](.{3,40}?)['\"\'\u2019]\s",
            r"before\s+['\"\'\u2018](.{3,40}?)['\"\'\u2019]\s",
            r"Change\s+(.{3,60}?)\s+to\s+",
            r"to\s+(.{3,60}?)\s+on\s+page",
        ]:
            m = re.search(pat, suggested_fix, re.IGNORECASE)
            if m:
                phrase = m.group(1).strip().strip("'\"")
                if phrase and phrase in text:
                    return _extract_edit_context(text, phrase, **_pg)

    # Strategy 6: Context sliding window (smaller chunks)
    if ctx_col and len(ctx_col) > 20:
        for size in [len(ctx_col) // 2, 60, 40, 20]:
            chunk = ctx_col[:size].strip()
            if len(chunk) >= 10 and chunk in text:
                return _extract_edit_context(text, chunk, **_pg)

    # Strategy 7: Notes column — quoted text
    notes = str(row.get("Notes", "") or "").strip()
    if notes:
        quoted = re.findall(
            r"['\'\u2018\u2019\"\u201c\u201d]([^'\'\u2018\u2019\"\u201c\u201d]{3,})"
            r"['\'\u2018\u2019\"\u201c\u201d]",
            notes,
        )
        for q in quoted:
            if q in text:
                return _extract_edit_context(text, q, **_pg)

    # Strategy 8: PyMuPDF Text column — search for the expected text itself
    pymupdf_text = str(row.get("PyMuPDF Text", "") or "").strip()
    if pymupdf_text and len(pymupdf_text) >= 10:
        # Try full text first
        if pymupdf_text in text:
            return _extract_edit_context(text, pymupdf_text, **_pg)
        # Try sliding windows of decreasing size
        for size in [min(len(pymupdf_text), 80), 60, 40, 20]:
            chunk = pymupdf_text[:size].strip()
            if len(chunk) >= 10 and chunk in text:
                return _extract_edit_context(text, chunk, **_pg)

    return ""


# ═══════════════════════════════════════════════════════════════════
# CONTENT ACCURACY HELPERS
# ═══════════════════════════════════════════════════════════════════


def _build_accuracy_card_html(accuracy_report: dict | None) -> str:
    """Return HTML for the accuracy metric card (fits inside vr-metric-row)."""
    if not accuracy_report:
        return ""
    acc = accuracy_report["accuracy"]
    if acc >= 99:
        cls = "vr-mc-acc-hi"
    elif acc >= 95:
        cls = "vr-mc-acc-md"
    else:
        cls = "vr-mc-acc-lo"
    return (
        f"<div class='vr-metric-card {cls}'>"
        f"<h2>{acc:.1f}%</h2><p>📊 Text Accuracy</p></div>"
    )


def _render_accuracy_expander(accuracy_report: dict | None,
                              converted_txt_path: Path | None = None):
    """Render a collapsible section with word-level diff details.

    If *converted_txt_path* is supplied the user gets an inline **Apply Fix**
    button next to each diff so they can correct the converted text file.
    """
    if not accuracy_report:
        return
    acc = accuracy_report["accuracy"]
    raw_acc = accuracy_report.get("raw_accuracy", acc)
    n_diffs = accuracy_report["real_diff_count"]
    n_repos = accuracy_report["repositioned_count"]
    n_trans = accuracy_report.get("transposed_count", 0)
    n_punct = accuracy_report.get("punctuation_count", 0)
    n_spacing = accuracy_report.get("spacing_count", 0)
    n_word  = accuracy_report.get("word_diff_count", n_diffs)
    n_allowed = accuracy_report.get("allowed_diff_count", 0)
    allowed_diffs = accuracy_report.get("allowed_diffs", [])
    allowed_cats = accuracy_report.get("allowed_categories", {})
    status = accuracy_report["status"]

    # If identical, no expander needed
    if status == "IDENTICAL" and n_diffs == 0 and n_repos == 0 and n_spacing == 0 and n_allowed == 0:
        return

    # Build breakdown chips for the label
    _chips = []
    if n_word:  _chips.append(f"{n_word} word")
    if n_trans: _chips.append(f"{n_trans} transposed")
    if n_punct: _chips.append(f"{n_punct} punctuation")
    if n_spacing: _chips.append(f"{n_spacing} spacing (ignored)")
    _chip_str = ", ".join(_chips) if _chips else f"{n_diffs} difference{'s' if n_diffs != 1 else ''}"

    # Show "raw accuracy → adjusted accuracy" delta when the allowlist
    # actually rescued some false positives.
    _acc_label = f"{acc:.1f}%"
    if n_allowed and raw_acc < acc:
        _acc_label = f"{acc:.1f}% (raw {raw_acc:.1f}%)"

    label = (
        f"📊 Text Accuracy Report — {_acc_label} "
        f"({_chip_str}"
        f"{f', {n_repos} repositioned' if n_repos else ''}"
        f"{f', {n_allowed} allowed' if n_allowed else ''})"
    )

    # Auto-expand if accuracy is below 98%
    expanded = acc < 98

    with st.expander(label, expanded=expanded):
        # Summary stats
        c1, c2, c3 = st.columns(3)
        c1.metric("PyMuPDF Words", f"{accuracy_report['pymupdf_word_count']:,}")
        c2.metric("Claude Words", f"{accuracy_report['converted_word_count']:,}")
        c3.metric("Matching", f"{accuracy_report['matching_words']:,}")

        # Sub-type breakdown pills
        if n_diffs > 0 or n_spacing > 0 or n_allowed > 0:
            _pill = ('display:inline-block; padding:3px 10px; border-radius:12px; '
                     'font-size:0.78rem; font-weight:600; margin:2px 4px;')
            _pills_html = ""
            if n_word:
                _pills_html += (f'<span style="{_pill} background:#fef2f2; color:#991b1b;">'
                                f'⚠️ {n_word} Word Diff{"s" if n_word != 1 else ""}</span>')
            if n_trans:
                _pills_html += (f'<span style="{_pill} background:#fefce8; color:#854d0e;">'
                                f'🔀 {n_trans} Transposed</span>')
            if n_punct:
                _pills_html += (f'<span style="{_pill} background:#eff6ff; color:#1e40af;">'
                                f'✂️ {n_punct} Punctuation</span>')
            if n_spacing:
                _pills_html += (f'<span style="{_pill} background:#f5f5f5; color:#6b7280;">'
                                f'📏 {n_spacing} Spacing (ignored)</span>')
            if n_repos:
                _pills_html += (f'<span style="{_pill} background:#f0fdf4; color:#166534;">'
                                f'↔️ {n_repos} Repositioned</span>')
            if n_allowed:
                _pills_html += (f'<span style="{_pill} background:#ecfeff; color:#155e75;">'
                                f'🛡️ {n_allowed} Allowed Removal{"s" if n_allowed != 1 else ""}</span>')
            st.markdown(f'<div style="margin:8px 0 12px;">{_pills_html}</div>',
                        unsafe_allow_html=True)

        # ── Allowed-removals panel ──────────────────────────────────────
        # These are diffs that classify as known artifact categories (line
        # numbers, headers, footers, page markers, decorative fill).  They
        # are EXCLUDED from the accuracy score because the cleaning prompt
        # explicitly tells the LLM to remove them.  Surfaced here so users
        # can audit *what* was filtered and confirm no real content slipped
        # into the allowlist.
        if n_allowed:
            with st.container():
                _cat_chip = ('display:inline-block; padding:2px 10px; border-radius:10px; '
                             'font-size:0.75rem; font-weight:600; margin:2px 4px; '
                             'background:#ecfeff; color:#155e75; border:1px solid #a5f3fc;')
                _cat_pills = "".join(
                    f'<span style="{_cat_chip}">{n} {k.replace("_", " ")}</span>'
                    for k, n in sorted(allowed_cats.items(), key=lambda kv: -kv[1])
                )
                _delta = (
                    f' Without the allowlist, accuracy would have been '
                    f'<b>{raw_acc:.1f}%</b> — the {n_allowed} intentional '
                    f'removal{"s" if n_allowed != 1 else ""} below '
                    f'rescued <b>{acc - raw_acc:+.1f} percentage points</b>.'
                ) if raw_acc < acc else ""
                st.markdown(
                    f'<div style="background:#ecfeff; border:1px solid #a5f3fc; '
                    f'border-radius:10px; padding:12px 16px; margin:8px 0 12px;">'
                    f'<div style="font-weight:700; color:#155e75; margin-bottom:6px;">'
                    f'🛡️ {n_allowed} intentional cleanup removal{"s" if n_allowed != 1 else ""} '
                    f'(not counted against accuracy)</div>'
                    f'<div style="margin-bottom:6px;">{_cat_pills}</div>'
                    f'<div style="font-size:0.82rem; color:#0c4a6e;">{_delta}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

                with st.expander(f"View all {n_allowed} allowed removal{'s' if n_allowed != 1 else ''}", expanded=False):
                    # Show the first 200 to keep the page snappy
                    _limit = 200
                    for idx, d in enumerate(allowed_diffs[:_limit], 1):
                        _txt = (
                            d.get("missing_text")
                            or d.get("extra_text")
                            or d.get("pymupdf_text")
                            or d.get("converted_text")
                            or ""
                        )
                        _cat = d.get("allowed_category", "?")
                        _rule = d.get("allowed_rule", "")
                        _txt_disp = _txt if len(_txt) <= 200 else _txt[:197] + "..."
                        # Mark LLM auto-demoted entries with a robot icon so the
                        # reviewer can distinguish regex-classified vs LLM-classified
                        # allowed removals at a glance.
                        _icon = "🤖" if d.get("auto_demoted") else "🛡️"
                        st.markdown(
                            f"`{idx:>3}` {_icon} **[{_cat}]** {_txt_disp}  \n"
                            f"&nbsp;&nbsp;&nbsp;&nbsp;<span style='color:#64748b; font-size:0.78rem;'>"
                            f"_Rule: {_rule}_</span>",
                            unsafe_allow_html=True,
                        )
                    if len(allowed_diffs) > _limit:
                        st.caption(f"... and {len(allowed_diffs) - _limit} more (full list in the removals.json report)")

        if status == "CONTENT_MATCH":
            st.success(
                f"✅ All content present in both files. "
                f"{n_repos} block(s) repositioned but no missing content."
            )

        # ── LLM-verifier summary banner (when applied) ───────────
        # When the user enabled "LLM-verify residual text diffs" in the
        # sidebar, llm_diff_verifier.apply_llm_verifications_to_report()
        # stamps these fields onto the accuracy report.  Surface them so
        # the reviewer knows the second-opinion layer ran.
        if accuracy_report.get("llm_verification_applied"):
            _llm_demoted = accuracy_report.get("llm_auto_demoted_count", 0)
            _llm_review  = accuracy_report.get("llm_review_aid_count", 0)
            _llm_total   = accuracy_report.get("llm_verified_count", 0)
            _llm_cats    = accuracy_report.get("llm_categories", {})
            _cat_chip = ('display:inline-block; padding:1px 8px; border-radius:8px; '
                         'font-size:0.72rem; font-weight:600; margin:1px 3px; '
                         'background:#f1f5f9; color:#334155; border:1px solid #cbd5e1;')
            _cat_pills = "".join(
                f'<span style="{_cat_chip}">{n} {k.replace("_", " ")}</span>'
                for k, n in sorted(_llm_cats.items(), key=lambda kv: -kv[1])
            ) or '<span style="color:#64748b; font-size:0.78rem;">no diffs to verify</span>'
            _llm_msg = (
                f'🤖 <b>LLM diff verifier:</b> classified {_llm_total} residual '
                f'real_diff{"s" if _llm_total != 1 else ""} — '
                f'<b>{_llm_demoted}</b> auto-demoted to allowed '
                f'(silent, all four safety guards passed), '
                f'<b>{_llm_review}</b> kept as real_diff with review-aid suggestion '
                f'(LLM thinks they’re artifacts but at least one guard failed: '
                f'medium/low confidence, non-whitelist category, or &gt;15 words).'
            )
            st.markdown(
                f'<div style="background:#f0f9ff; border:1px solid #bae6fd; '
                f'border-radius:10px; padding:12px 16px; margin:10px 0 14px;">'
                f'<div style="margin-bottom:6px;">{_llm_msg}</div>'
                f'<div>{_cat_pills}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

        # Real differences — show all types
        real_diffs = accuracy_report["real_diffs"]
        if real_diffs:
            st.markdown("#### ⚠️ Real Differences")
            for idx, d in enumerate(real_diffs, 1):
                dtype = d["type"]
                sub = d.get("sub_type", "")

                if dtype == "REPLACE":
                    if sub == "TRANSPOSED":
                        lines = (
                            f"**#{idx} 🔀 TRANSPOSED WORDS**\n\n"
                            f"- **Expected order:** {d['pymupdf_text']}\n"
                            f"- **Got order:** {d['converted_text']}"
                        )
                        moved = d.get("moved_words", [])
                        if moved:
                            lines += "\n- **Moved:** " + ", ".join(
                                f'pos {m["position"]}: "{m["expected"]}" → "{m["got"]}"'
                                for m in moved
                            )
                    elif sub == "PUNCTUATION":
                        lines = (
                            f"**#{idx} ✂️ PUNCTUATION DIFFERENCE**\n\n"
                            f"- **Expected:** {d['pymupdf_text']}\n"
                            f"- **Got:** {d['converted_text']}"
                        )
                        char_diffs = d.get("char_diffs", [])
                        if char_diffs:
                            lines += "\n- **Details:** " + ", ".join(
                                f'{cd["kind"]} `{cd["char"]}`' for cd in char_diffs
                            )
                    else:
                        lines = (
                            f"**#{idx} 🔄 REPLACE**\n\n"
                            f"- **PyMuPDF:** {d['pymupdf_text']}\n"
                            f"- **Claude:** {d['converted_text']}\n\n"
                            f"> *PyMuPDF sentence:* {d.get('sentence_pymupdf', '')}\n\n"
                            f"> *Claude sentence:* {d.get('sentence_converted', '')}"
                        )
                elif dtype == "MISSING_IN_CONVERTED":
                    lines = (
                        f"**#{idx} ❌ MISSING in Claude output**\n\n"
                        f"- **Missing:** {d['missing_text']}\n\n"
                        f"> *Sentence:* {d['sentence']}"
                    )
                elif dtype == "EXTRA_IN_CONVERTED":
                    lines = (
                        f"**#{idx} ➕ EXTRA in Claude output**\n\n"
                        f"- **Extra:** {d['extra_text']}\n\n"
                        f"> *Sentence:* {d['sentence']}"
                    )
                else:
                    lines = f"**#{idx} {dtype}**\n\n- {d}"

                st.markdown(lines)

                # ── LLM review-aid suggestion (when present) ────────────
                # The diff stayed as ``real_diff`` because at least one
                # of the four auto-demote safety guards failed.  We surface
                # the LLM's hypothesis so the reviewer has context for the
                # one-click "mark as allowed" decision.
                _llm_v = d.get("llm_verdict")
                _llm_s = d.get("llm_suggestion")
                if _llm_s or (_llm_v and _llm_v != "real_content"):
                    _conf = d.get("llm_confidence", "low")
                    _cat  = d.get("llm_category", "unclear")
                    _why  = d.get("llm_reasoning", "")
                    _conf_color = {"high": "#16a34a", "medium": "#d97706", "low": "#64748b"}.get(_conf, "#64748b")
                    _verdict_label = "likely artifact" if _llm_v == "artifact" else _llm_v
                    st.markdown(
                        f'<div style="background:#fef9c3; border-left:3px solid #eab308; '
                        f'border-radius:6px; padding:8px 12px; margin:6px 0 4px; font-size:0.85rem;">'
                        f'<b>🤖 LLM second opinion:</b> '
                        f'<span style="color:{_conf_color}; font-weight:600;">{_verdict_label}</span> '
                        f'— category <code>{_cat}</code>, confidence <code>{_conf}</code>.<br>'
                        f'<span style="color:#713f12; font-size:0.82rem;">{_why}</span>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )
                elif _llm_v == "real_content":
                    # LLM independently confirmed this is real content — reinforce
                    # the existing verdict so the reviewer trusts it.
                    st.caption(
                        f"🤖 LLM second opinion: confirmed real content — "
                        f"{d.get('llm_reasoning', 'no reasoning provided')}"
                    )

                if idx < len(real_diffs):
                    st.divider()

        # Repositioned blocks
        repositioned = accuracy_report.get("repositioned", [])
        if repositioned:
            st.markdown("#### ↔️ Repositioned Blocks")
            for idx, rp in enumerate(repositioned, 1):
                text_val = rp.get("pymupdf_text") or rp.get("converted_text", "")
                display = text_val if len(text_val) <= 150 else text_val[:147] + "..."
                st.markdown(f"**#{idx}** {display}")
                if idx < len(repositioned):
                    st.divider()


# ═══════════════════════════════════════════════════════════════════
# REUSABLE TWO-COLUMN VIEW — PDF page ↔ Claude text
# ═══════════════════════════════════════════════════════════════════

def _render_twocol_view(
    page_images: list[Path],
    total_pages: int,
    text: str,
    checklist: list[dict],
    selected_id: int | None,
    filter_mode: str,
    fixed_ids: set[int],
    ignored_ids: set[int],
    all_errors_resolved: bool = False,
    converted_txt_path: Path | None = None,
):
    """Render the side-by-side PDF page + Claude converted text columns."""

    with st.container(key="vri_twocol"):
        col_left, col_right = st.columns([1, 1], gap="medium")

        # ──────── LEFT: PDF Page (reference) ────────────────────────
        with col_left:
            if total_pages > 0:
                cur = st.session_state.vri_current_page
                cur = max(1, min(cur, total_pages))
                st.session_state.vri_current_page = cur

                st.markdown(
                    f'<p style="font-size:0.82rem;color:#64748b;margin:0 0 6px;">'
                    f'📍 Scrolled to <b>Page {cur}</b> / {total_pages}</p>',
                    unsafe_allow_html=True,
                )

                # Build all pages in a scrollable container with anchors
                _pdf_images_html = ""
                for pg_idx, pg_path in enumerate(page_images):
                    pg_num = pg_idx + 1
                    import base64 as _b64_mod
                    _img_bytes = pg_path.read_bytes()
                    _img_b64 = _b64_mod.b64encode(_img_bytes).decode()
                    _border = "3px solid #3b82f6" if pg_num == cur else "1px solid #e2e8f0"
                    _pdf_images_html += (
                        f'<div id="vri-pdf-page-{pg_num}" style="margin-bottom:12px; border:{_border}; border-radius:6px; overflow:hidden;">'
                        f'<div style="background:#f1f5f9; padding:4px 10px; font-size:0.75rem; font-weight:600; color:#475569;">Page {pg_num}</div>'
                        f'<img src="data:image/png;base64,{_img_b64}" style="width:100%; display:block;" />'
                        f'</div>'
                    )

                st.html(
                    f'<div class="vr-pdf-scroll-container" style="'
                    f'overflow-y:auto; max-height:80vh; min-height:400px; '
                    f'border:1px solid #e2e8f0; border-radius:8px; padding:8px; background:#fafafa;">'
                    f'{_pdf_images_html}'
                    f'</div>'
                    f'<script>'
                    f'(function() {{'
                    f'  var target = {cur};'
                    f'  function doScroll() {{'
                    f'    var el = document.getElementById("vri-pdf-page-" + target);'
                    f'    if (!el) return false;'
                    f'    var container = el.closest(".vr-pdf-scroll-container");'
                    f'    if (container) {{'
                    f'      var pos = el.offsetTop - container.offsetTop;'
                    f'      if (pos > 0) {{ container.scrollTop = pos; return true; }}'
                    f'    }}'
                    f'    return false;'
                    f'  }}'
                    f'  var attempts = 0;'
                    f'  var timer = setInterval(function() {{'
                    f'    attempts++;'
                    f'    if (doScroll() || attempts > 50) clearInterval(timer);'
                    f'  }}, 100);'
                    f'}})();'
                    f'</script>'
                )

                # Page summary (only when checklist data is available)
                on_page = [r for r in checklist if _safe_int(r.get("Page", 0)) == cur]
                if on_page:
                    e = sum(1 for r in on_page
                            if str(r.get("Verdict", "")).startswith("❌")
                            and int(r.get("#", 0)) not in fixed_ids)
                    w = sum(1 for r in on_page if str(r.get("Verdict", "")).startswith("⚠"))
                    st.caption(f"This page: {len(on_page)} items  •  ❌ {e} errors  •  ⚠️ {w} warnings")

            else:
                st.info("No highlighted page images found.")

        # ──────── RIGHT: Converted Text ─────────────────────────────
        with col_right:
            # Determine if edit mode is active
            _edit_mode_active = (
                converted_txt_path is not None
                and st.session_state.get("vri_edit_mode", False)
            )

            if converted_txt_path is not None:
                # Header with Edit toggle
                _hdr_col1, _hdr_col2 = st.columns([3, 1])
                with _hdr_col1:
                    st.markdown(
                        '<p style="font-size:0.92rem;font-weight:700;margin:0 0 6px;">📝 Claude Converted Text</p>',
                        unsafe_allow_html=True,
                    )
                with _hdr_col2:
                    if _edit_mode_active:
                        if st.button("🔒 Exit Edit", key="vri_exit_edit", width="stretch"):
                            st.session_state["vri_edit_mode"] = False
                            st.session_state.pop("vri_edit_text_buffer", None)
                            st.rerun()
                    else:
                        if st.button("✏️ Edit Text", key="vri_enter_edit", width="stretch"):
                            st.session_state["vri_edit_mode"] = True
                            st.rerun()
            else:
                st.markdown(
                    '<p style="font-size:0.92rem;font-weight:700;margin:0 0 6px;">📝 Claude Converted Text</p>',
                    unsafe_allow_html=True,
                )

            if _edit_mode_active:
                # ── Editable text area ──────────────────────────────
                if total_pages > 0:
                    st.markdown('<div style="height:16px;"></div>', unsafe_allow_html=True)
                st.markdown(
                    '<div style="background:#fffbeb; border:1px solid #fde68a; border-radius:8px; '
                    'padding:8px 12px; margin-bottom:8px; font-size:0.82rem; color:#92400e;">'
                    '✏️ <b>Edit Mode</b> — Make your changes below, then click <b>Save</b> to update the file.</div>',
                    unsafe_allow_html=True,
                )
                _edit_buffer = st.text_area(
                    "Edit converted text",
                    value=text,
                    height=500,
                    key="vri_edit_text_buffer",
                    label_visibility="collapsed",
                )
                _save_col, _cancel_col = st.columns(2)
                with _save_col:
                    if st.button("💾 Save Changes", type="primary", key="vri_save_edit", width="stretch"):
                        if _edit_buffer != text:
                            converted_txt_path.write_text(_edit_buffer, encoding="utf-8")
                            st.session_state["vri_edit_mode"] = False
                            st.session_state.pop("vri_edit_text_buffer", None)
                            st.success("✅ File saved successfully!")
                            st.rerun()
                        else:
                            st.info("No changes to save.")
                with _cancel_col:
                    if st.button("❌ Cancel", type="primary", key="vri_cancel_edit", width="stretch"):
                        st.session_state["vri_edit_mode"] = False
                        st.session_state.pop("vri_edit_text_buffer", None)
                        st.rerun()
            else:
                # ── Read-only view ──────────────────────────────────
                if total_pages > 0:
                    st.markdown(
                        '<div style="height:58px;"></div>',
                        unsafe_allow_html=True,
                    )
                html = _render_text_html(text, checklist, selected_id, filter_mode, fixed_ids | ignored_ids)
                # Use st.html() instead of st.markdown() because Streamlit
                # >=1.45 sanitises <span class="…"> in markdown, stripping
                # the styled highlight tags and showing raw HTML to the user.
                _viewer_css = """
                <style>
                .vr-text-viewer {
                    font-family: 'Cascadia Code','Consolas', monospace;
                    font-size: 0.82rem; line-height: 1.65;
                    white-space: pre-wrap; word-break: break-word;
                    background: #fafafa; border: 1px solid #e2e8f0;
                    border-radius: 8px; padding: 16px;
                    overflow-y: auto;
                    min-height: 400px;
                    max-height: 80vh;
                }
                .vr-tag-add { background:#bbf7d0; color:#166534; padding:1px 2px; border-radius:3px; }
                .vr-tag-del { background:#fecaca; color:#991b1b; padding:1px 2px; border-radius:3px; text-decoration:line-through; }
                .vr-span-highlight-pass { background:#86efac; padding:1px 3px; border-radius:3px; }
                .vr-span-highlight-warn { background:#fde047; padding:1px 3px; border-radius:3px; }
                .vr-span-highlight-err  { background:#fca5a5; padding:1px 3px; border-radius:3px; }
                mark.vr-current-span {
                    background:#60a5fa !important; color:white !important;
                    padding: 2px 4px; border-radius: 4px;
                    animation: vr-pulse 1.5s ease-in-out infinite alternate;
                }
                @keyframes vr-pulse { from {box-shadow: 0 0 0 0 rgba(96,165,250,.5)} to {box-shadow: 0 0 0 6px rgba(96,165,250,0)} }
                </style>
                """
                st.html(
                    f'{_viewer_css}<div class="vr-text-viewer">{html}</div>'
                )

    # JS: sync text-viewer / edit textarea height to the PDF scroll container height
    components.html("""
    <script>
    (function syncHeight() {
        const doc = window.parent.document;
        const container = doc.querySelector('.st-key-vri_twocol');
        if (!container) return;
        const pdfScroll = container.querySelector('.vr-pdf-scroll-container');
        if (!pdfScroll) return;
        function adjust() {
            const h = pdfScroll.clientHeight;
            if (h <= 100) return;
            // Sync read-only viewer
            const viewer = container.querySelector('.vr-text-viewer');
            if (viewer) { viewer.style.maxHeight = h + 'px'; }
            // Sync edit-mode textarea
            const ta = container.querySelector('textarea');
            if (ta) {
                var block = ta.closest('[data-testid="stVerticalBlock"]');
                if (block) {
                    var usedHeight = 0;
                    for (var i = 0; i < block.children.length; i++) {
                        var child = block.children[i];
                        if (child.contains(ta)) continue;
                        usedHeight += child.offsetHeight || 0;
                    }
                    var editH = Math.max(300, h - usedHeight - 16);
                    ta.style.height = editH + 'px';
                    ta.style.maxHeight = editH + 'px';
                    ta.style.minHeight = editH + 'px';
                }
            }
        }
        // Wait for images to load then adjust
        var images = pdfScroll.querySelectorAll('img');
        var loaded = 0;
        if (images.length === 0) { adjust(); return; }
        images.forEach(function(img) {
            if (img.complete) { loaded++; } else {
                img.addEventListener('load', function() { loaded++; if (loaded >= images.length) adjust(); });
            }
        });
        if (loaded >= images.length) adjust();
        new ResizeObserver(adjust).observe(pdfScroll);
    })();
    </script>
    """, height=0)


# ═══════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT — call this from streamlit_app.py
# ═══════════════════════════════════════════════════════════════════

def render_inline_validation_review(
    excel_path: str | Path,
    converted_txt_path: str | Path,
    highlighted_dir: str | Path | None = None,
    json_report: dict | None = None,
    accuracy_report: dict | None = None,
    gpt_audit_failed: bool = False,
):
    """
    Render the full validation review UI inline in the Streamlit app.

    Parameters
    ----------
    excel_path : path
        Path to the ``*_gpt_comparison.xlsx`` file.
    converted_txt_path : path
        Path to the Claude converted text file (for inline editing).
    highlighted_dir : path, optional
        Directory containing ``page_*.png`` highlighted images.
    json_report : dict, optional
        The PyMuPDF JSON report dict (for bbox-based annotations).
    accuracy_report : dict, optional
        Structured dict from ``compare_texts()`` with content accuracy data.
    gpt_audit_failed : bool
        If True, the GPT audit step failed — show manual review UI.
    """
    excel_path = Path(str(excel_path)) if excel_path else Path("")
    converted_txt_path = Path(converted_txt_path)
    highlighted_dir = Path(highlighted_dir) if highlighted_dir else None

    # ── Save/restore fixed/ignored state per file ────────────────────
    import copy as _copy

    _current_file_key = str(Path(str(converted_txt_path)).resolve())
    _prev_file_key = st.session_state.get("vri_file_key", "")
    _vri_state_keys = ("vri_fixed_ids", "vri_ignored_ids", "vri_undo_history",
                       "vri_selected_item", "vri_current_page", "vri_filter_mode",
                       "vri_last_sel", "vri_edit_mode")

    # Always persist current vri_ state under the CURRENT file key on every rerun.
    # This ensures that any fix/ignore made since the last rerun is captured
    # BEFORE we potentially switch to a different file.
    if _prev_file_key:
        _all_saved = st.session_state.get("vri_per_file_state", {})
        _saved = {}
        for _k in _vri_state_keys:
            if _k in st.session_state:
                _saved[_k] = _copy.deepcopy(st.session_state[_k])
        if _saved:
            _all_saved[_prev_file_key] = _saved
            st.session_state["vri_per_file_state"] = _all_saved

    if _current_file_key != _prev_file_key:
        # Restore saved state for the new file (if any), otherwise clear
        _all_saved = st.session_state.get("vri_per_file_state", {})
        _restored = _all_saved.get(_current_file_key, {})
        for _k in _vri_state_keys:
            if _k in _restored:
                st.session_state[_k] = _copy.deepcopy(_restored[_k])
            else:
                st.session_state.pop(_k, None)

        st.session_state["vri_file_key"] = _current_file_key

    # Inject CSS
    st.markdown(_CSS, unsafe_allow_html=True)

    # ── Load data ───────────────────────────────────────────────────
    checklist, findings, summary = [], [], {}
    if excel_path.exists():
        try:
            checklist, findings, summary = _load_excel(excel_path)
        except Exception as _xl_err:
            import logging as _log
            _log.warning("Could not load Excel audit file %s: %s", excel_path, _xl_err)
            st.warning(
                f"⚠️ The audit Excel file could not be opened (it may be corrupted). "
                f"Re-running the GPT audit step should fix this.\n\n"
                f"Details: {_xl_err}"
            )

    if not checklist:
        # ── GPT audit failed banner ────────────────────────────────────
        if gpt_audit_failed:
            st.markdown(
                '<div style="background:#fef2f2; border-radius:10px; padding:16px 24px; '
                'border-left:4px solid #dc2626; margin-bottom:16px;">'
                '<div style="display:flex; align-items:center; gap:10px; margin-bottom:8px;">'
                '<span style="font-size:1.4rem;">⚠️</span>'
                '<strong style="color:#991b1b; font-size:1.05rem;">Automatic Validation Report Unavailable</strong>'
                '</div>'
                '<p style="color:#7f1d1d; font-size:0.88rem; margin:0 0 8px;">'
                'The automated GPT audit could not generate a validation report for this file. '
                'This can happen when the file produces a response that exceeds processing limits.</p>'
                '<p style="color:#991b1b; font-size:0.88rem; font-weight:600; margin:0;">'
                '📝 Please review the converted text manually by comparing it against the '
                'original PDF pages shown below. You can edit the converted text directly.</p>'
                '</div>',
                unsafe_allow_html=True,
            )

        # Show accuracy details if available
        if accuracy_report:
            st.markdown("---")
            st.markdown("## 🔍 Validation Review — Text Accuracy")
            if not gpt_audit_failed:
                if excel_path.exists():
                    st.info("No checklist items found in the GPT audit Excel, "
                            "but text accuracy details are available below.")
                else:
                    st.info("GPT checklist audit was not available for this file, "
                            "but text accuracy details are available below.")
            _render_accuracy_expander(accuracy_report, converted_txt_path)
        elif not gpt_audit_failed:
            st.warning("⚠️ No audit data or accuracy report available for this file.")

        # ── Additional findings section removed (duplicates checklist) ────
        # ── Side-by-side PDF ↔ Converted text view removed per user request ─
        # ── Download button removed per user request ──────────────────────

        return

    text = ""
    if converted_txt_path.exists():
        text = converted_txt_path.read_text(encoding="utf-8")

    page_images = _get_page_images(highlighted_dir)
    json_rep = json_report or {}
    total_pages = len(page_images)
    fixed_ids = _get_fixed_ids()
    ignored_ids = _get_ignored_ids()

    # ── Header ──────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("## 🔍 Validation Review — GPT Audit Results")

    # ── Summary cards ───────────────────────────────────────────────
    n_fixed = sum(1 for r in checklist if int(r.get("#", 0)) in fixed_ids)
    n_ignored_warn = sum(1 for r in checklist
                    if int(r.get("#", 0)) in ignored_ids
                    and str(r.get("Verdict", "")).startswith("⚠"))
    n_ignored_err = sum(1 for r in checklist
                    if int(r.get("#", 0)) in ignored_ids
                    and str(r.get("Verdict", "")).startswith("❌"))
    n_ignored = n_ignored_warn + n_ignored_err
    orig_err = summary.get("error", 0)
    orig_warn = summary.get("warn", 0)
    eff_err = max(0, orig_err - n_fixed - n_ignored_err)
    eff_warn = max(0, orig_warn - n_ignored_warn)
    eff_pass = summary.get("pass", 0) + n_fixed + n_ignored

    # Resolved count (fixed + ignored)
    n_resolved = n_fixed + n_ignored

    # Build optional metric cards (fixed + accuracy) to avoid blank-line markdown code-block glitch
    _extra_cards_html = ""
    if n_resolved:
        _extra_cards_html += f'<div class="vr-metric-card vr-mc-fixed"><h2>{n_resolved}</h2><p>🔧 Fixed / Ignored</p></div>'
    _extra_cards_html += _build_accuracy_card_html(accuracy_report)

    st.markdown(f"""
    <div class="vr-metric-row">
        <div class="vr-metric-card vr-mc-total">
            <h2>{summary.get('total', 0)}</h2><p>Total Items</p>
        </div>
        <div class="vr-metric-card vr-mc-pass">
            <h2>{eff_pass}</h2><p>✅ Pass</p>
        </div>
        <div class="vr-metric-card vr-mc-warn">
            <h2>{eff_warn}</h2><p>⚠️ Warnings</p>
        </div>
        <div class="vr-metric-card vr-mc-err">
            <h2>{eff_err}</h2><p>❌ Errors</p>
        </div>
        {_extra_cards_html}
    </div>
    """, unsafe_allow_html=True)

    total = summary.get("total", 1) or 1
    _pass_rate = min(eff_pass / total, 1.0)
    st.progress(_pass_rate, text=f"Pass rate: {_pass_rate:.0%}  ({eff_pass}/{total})")

    # ── Content Accuracy Report (collapsible) ─────────────────
    _render_accuracy_expander(accuracy_report, converted_txt_path)

    # ── Filter mode ─────────────────────────────────────────────────
    filter_mode = st.radio(
        "Filter items",
        ["all", "errors", "warnings"],
        format_func=lambda x: {
            "all": "📋 All items",
            "errors": "❌ Errors only",
            "warnings": "⚠️ Warnings only",
        }[x],
        key="vri_filter_mode",
        horizontal=True,
    )

    # ── Selected item state ─────────────────────────────────────────
    if "vri_selected_item" not in st.session_state:
        errors = [r for r in checklist if str(r.get("Verdict", "")).startswith("❌")]
        warnings = [r for r in checklist if str(r.get("Verdict", "")).startswith("⚠")]
        if errors:
            st.session_state.vri_selected_item = int(errors[0].get("#", 1))
        elif warnings:
            st.session_state.vri_selected_item = int(warnings[0].get("#", 1))
        elif checklist:
            st.session_state.vri_selected_item = int(checklist[0].get("#", 1))
        else:
            st.session_state.vri_selected_item = 0

    # ── Handle table row click (from JS component) ──────────────────
    _table_click_raw = st.session_state.get("vri_table_click")
    if _table_click_raw is not None:
        try:
            _click_cid = int(str(_table_click_raw).split('_')[0])
            _click_uid = str(_table_click_raw)
            if _click_uid != st.session_state.get("_vri_last_table_click"):
                st.session_state["_vri_last_table_click"] = _click_uid
                st.session_state.vri_selected_item = _click_cid
                st.session_state["_vri_nav_btn_fired"] = True  # sync selectbox too
        except (ValueError, IndexError):
            pass

    selected_id = st.session_state.vri_selected_item

    selected_row = None
    for r in checklist:
        try:
            if int(r.get("#", 0)) == selected_id:
                selected_row = r
                break
        except (ValueError, TypeError):
            pass

    # ── Filter ──────────────────────────────────────────────────────
    if filter_mode == "errors":
        display_cl = [r for r in checklist
                      if str(r.get("Verdict", "")).startswith("❌")
                      and int(r.get("#", 0)) not in fixed_ids
                      and int(r.get("#", 0)) not in ignored_ids]
    elif filter_mode == "warnings":
        display_cl = [r for r in checklist
                      if str(r.get("Verdict", "")).startswith("⚠")
                      and int(r.get("#", 0)) not in ignored_ids]
    else:
        display_cl = checklist

    # ── Page state ──────────────────────────────────────────────────
    sel_item_page = 1
    sel_item_line = None
    if selected_row:
        try:
            sel_item_page = int(selected_row.get("Page", 1))
        except (ValueError, TypeError):
            sel_item_page = 1
        try:
            _line_val = selected_row.get("Line", None)
            sel_item_line = int(_line_val) if _line_val else None
        except (ValueError, TypeError):
            sel_item_line = None

    if "vri_current_page" not in st.session_state:
        st.session_state.vri_current_page = sel_item_page
    if st.session_state.get("vri_last_sel") != selected_id:
        st.session_state.vri_current_page = sel_item_page
        st.session_state["vri_last_sel"] = selected_id

    # ══════════════════════════════════════════════════════════════
    # ERROR NAVIGATION BAR
    # ══════════════════════════════════════════════════════════════
    error_ids = _get_error_ids(checklist, fixed_ids, ignored_ids)
    err_pos, err_total = _get_error_position(error_ids, selected_id)

    if error_ids:
        nav_c1, nav_c2, nav_c3 = st.columns([1, 3, 1])
        with nav_c1:
            if st.button("◀ Prev Error", key="vri_prev_err", width="stretch"):
                if err_pos > 1:
                    st.session_state.vri_selected_item = error_ids[err_pos - 2]
                else:
                    st.session_state.vri_selected_item = error_ids[-1]
                st.rerun()
        with nav_c2:
            _line_info = f", Line {sel_item_line}" if sel_item_line else ""
            if err_pos > 0:
                st.markdown(f"""
                <div class="vr-error-nav-bar">
                    <span class="nav-label">❌ Error {err_pos} of {err_total}  (Item #{selected_id})</span>
                    <span class="nav-page">Page {sel_item_page}{_line_info}</span>
                </div>
                """, unsafe_allow_html=True)
            else:
                st.markdown(f"""
                <div class="vr-error-nav-bar">
                    <span class="nav-label">📋 Item #{selected_id}</span>
                    <span class="nav-page">Page {sel_item_page}{_line_info}  •  {err_total} errors remaining</span>
                </div>
                """, unsafe_allow_html=True)
        with nav_c3:
            if st.button("Next Error ▶", key="vri_next_err", width="stretch"):
                if err_pos > 0 and err_pos < err_total:
                    st.session_state.vri_selected_item = error_ids[err_pos]
                else:
                    st.session_state.vri_selected_item = error_ids[0]
                st.rerun()
    else:
        # Check if there are still unresolved warnings
        remaining_warnings = sum(
            1 for r in checklist
            if str(r.get("Verdict", "")).startswith("⚠")
            and int(r.get("#", 0)) not in ignored_ids
        )
        if remaining_warnings:
            st.info(f"✅ **All errors resolved!** {remaining_warnings} warning(s) remain — review or ignore them below.")
        else:
            st.success("🎉 **All issues resolved!** All errors are fixed and all warnings addressed.")

    # ══════════════════════════════════════════════════════════════
    # CHECKLIST TABLE (moved to top of review area)
    # ══════════════════════════════════════════════════════════════
    st.markdown("---")
    st.markdown("#### 📋 Full Audit Checklist")

    # Detect if any items have line numbers
    _has_line_numbers = any(
        r.get("Line") not in (None, "", 0, "0")
        for r in checklist
    )
    if not _has_line_numbers and checklist:
        st.caption("ℹ️ This bill does not have printed line numbers — line references are not available.")

    if not display_cl:
        st.info("No items match the current filter.")
    else:
        tbl = '<table class="vr-cl-table"><thead><tr>'
        for c in ["#", "Status", "Page", "Line", "Tag Type", "Expected Text (PDF)",
                   "Found?", "Output Text", "Category", "Notes"]:
            tbl += f"<th>{c}</th>"
        tbl += "</tr></thead><tbody>"

        for row in display_cl:
            cid_int = int(row.get("#", 0))
            verd = _get_effective_verdict(row, fixed_ids, ignored_ids)

            if cid_int in fixed_ids:
                rc, bc = "row-fixed", "vr-badge-fixed"
            elif cid_int in ignored_ids:
                rc, bc = "row-fixed", "vr-badge-fixed"
            elif verd.startswith("✅"):
                rc, bc = "row-pass", "vr-badge-pass"
            elif verd.startswith("⚠"):
                rc, bc = "row-warn", "vr-badge-warn"
            else:
                rc, bc = "row-err", "vr-badge-err"

            if cid_int == selected_id:
                rc += " row-selected"

            tbl += f'<tr class="{rc}" data-cid="{cid_int}">'
            _pymupdf_full = _html_escape(str(row.get("PyMuPDF Text", "")))
            _pymupdf_disp = _html_escape(_trunc(row.get("PyMuPDF Text", "")))
            _claude_full = _html_escape(_row_clean_text(row))
            _claude_disp = _html_escape(_trunc(_row_clean_text(row)))
            _notes_full = _html_escape(str(row.get("Notes", "")))
            _notes_disp = _html_escape(_trunc(row.get("Notes", ""), 100))

            tbl += f'<td><strong>{cid_int}</strong></td>'
            tbl += f'<td><span class="vr-badge {bc}">{_html_escape(verd)}</span></td>'
            tbl += f'<td>{row.get("Page", "")}</td>'
            _line_val = row.get("Line", "") or ""
            tbl += f'<td>{_line_val if _line_val else ("—" if not _has_line_numbers else "")}</td>'
            tbl += f'<td>{_html_escape(str(row.get("Tag Type", "")))}</td>'
            tbl += f'<td title="{_pymupdf_full}">{_pymupdf_disp}</td>'
            tbl += f'<td>{_html_escape(_row_in_clean(row))}</td>'
            tbl += f'<td title="{_claude_full}">{_claude_disp}</td>'
            tbl += f'<td>{_html_escape(str(row.get("Classification", "")))}</td>'
            tbl += f'<td title="{_notes_full}">{_notes_disp}</td>'
            tbl += "</tr>"

        tbl += "</tbody></table>"
        _tbl_css = """<style>
.vr-cl-table { width:100%; border-collapse:collapse; font-size:0.82rem; }
.vr-cl-table th { background:#1e293b; color:white; padding:8px 10px; text-align:left; position:sticky; top:0; z-index:2; border-bottom:2px solid #334155; }
.vr-cl-table td { padding:7px 10px; border-bottom:1px solid #e2e8f0; vertical-align:top; }
.vr-cl-table tr:hover { background:#f0f9ff !important; cursor:pointer; }
.vr-cl-table tr.row-pass  { background:#f0fdf4; }
.vr-cl-table tr.row-warn  { background:#fefce8; }
.vr-cl-table tr.row-err   { background:#fef2f2; }
.vr-cl-table tr.row-fixed { background:#dbeafe; }
.vr-cl-table tr.row-selected { outline:2px solid #3b82f6; background:#eff6ff !important; }
.vr-badge { display:inline-block; padding:2px 8px; border-radius:12px; font-weight:600; font-size:0.75rem; white-space:nowrap; }
.vr-badge-pass  { background:#bbf7d0; color:#166534; }
.vr-badge-warn  { background:#fde68a; color:#854d0e; }
.vr-badge-err   { background:#fecaca; color:#991b1b; }
.vr-badge-fixed { background:#93c5fd; color:#1e3a8a; }
</style>"""
        st.html(_tbl_css + tbl)

        # ── Clickable rows: bidirectional component bridge ─────────────
        if _vri_click_bridge is not None:
            _vri_click_bridge(default=None, key="vri_table_click", height=0)

        # Navigation buttons
        st.markdown("")
        nc = st.columns([2, 1, 1, 1, 2])

        id_opts = [int(r.get("#", 0)) for r in display_cl]

        # If current selection isn't in the filtered list, find the nearest valid item
        if id_opts and selected_id not in id_opts:
            # Pick the first item in the filtered list as a sane default
            _nearest = id_opts[0]
            for _oid in id_opts:
                if _oid >= selected_id:
                    _nearest = _oid
                    break
            st.session_state.vri_selected_item = _nearest
            st.rerun()

        with nc[1]:
            if st.button("⬆️ Prev Item", key="vri_prev_item_btn"):
                if id_opts:
                    if selected_id in id_opts:
                        ci = id_opts.index(selected_id)
                        st.session_state.vri_selected_item = id_opts[ci - 1] if ci > 0 else id_opts[-1]
                    else:
                        st.session_state.vri_selected_item = id_opts[-1]
                    st.session_state["_vri_nav_btn_fired"] = True
                    st.rerun()
        with nc[2]:
            if id_opts:
                current_idx = id_opts.index(selected_id) if selected_id in id_opts else 0
                st.session_state["vri_jump_to"] = id_opts[current_idx]
                jump = st.selectbox(
                    "Go to #", id_opts,
                    index=current_idx,
                    key="vri_jump_to", label_visibility="collapsed",
                )
            else:
                jump = selected_id
                st.selectbox("Go to #", [selected_id], index=0,
                             key="vri_jump_to", label_visibility="collapsed", disabled=True)
        with nc[3]:
            if st.button("⬇️ Next Item", key="vri_next_item_btn"):
                if id_opts:
                    if selected_id in id_opts:
                        ci = id_opts.index(selected_id)
                        st.session_state.vri_selected_item = id_opts[ci + 1] if ci < len(id_opts) - 1 else id_opts[0]
                    else:
                        st.session_state.vri_selected_item = id_opts[0]
                    st.session_state["_vri_nav_btn_fired"] = True
                    st.rerun()

        # Only honour the selectbox jump if it wasn't a button-driven navigation
        if st.session_state.pop("_vri_nav_btn_fired", False):
            pass  # button already set vri_selected_item — ignore stale selectbox value
        elif id_opts and jump != selected_id and jump in id_opts:
            st.session_state.vri_selected_item = jump
            st.rerun()

    # ══════════════════════════════════════════════════════════════
    # ITEM DETAIL + INLINE FIX
    # ══════════════════════════════════════════════════════════════
    if selected_row:
        st.markdown("---")
        cid = int(selected_row.get("#", 0))
        verdict = _get_effective_verdict(selected_row, fixed_ids, ignored_ids)

        st.markdown(f"#### 📋 Review — Item #{cid}")

        _tag_type = str(selected_row.get("Tag Type", "addition"))
        _tag_badge = "🟢 Addition needed" if _tag_type == "addition" else "🔴 Deletion needed"
        _tag_example = "`[A>...<A]`" if _tag_type == "addition" else "`[D>...<D]`"
        st.markdown(f"**Expected text (from PDF):** &nbsp; {_tag_badge} &nbsp; {_tag_example}")
        _expected_text = str(selected_row.get("PyMuPDF Text", ""))
        st.code(_expected_text, language=None)
        ctx = str(selected_row.get("Context", ""))
        if ctx:
            st.markdown("**Surrounding context:**")
            st.code(ctx, language=None)

        # ── Inline edit for errors ──────────────────────────────────
        orig_verdict = str(selected_row.get("Verdict", ""))
        is_error = orig_verdict.startswith("❌")
        is_fixed = cid in fixed_ids
        is_ignored = cid in ignored_ids

        st.markdown("---")

        if is_fixed:
            st.success(f"✅ Item #{cid} has been fixed! The text file is updated.")

            # Show content details (same as Apply Fix / warning views)
            claude_text = _row_clean_text(selected_row)
            pymupdf_text = str(selected_row.get("PyMuPDF Text", "") or "")
            notes = str(selected_row.get("Notes", "") or "")
            fix = str(selected_row.get("Suggested Fix", "") or "")

            if claude_text:
                st.markdown("**Claude Text:**")
                st.code(claude_text, language=None)
            if notes:
                st.info(f"📝 **Notes:** {notes}")
            if fix and fix.lower() not in ("none", "n/a", ""):
                st.warning(f"🔧 **Suggested Fix:** {fix}")
            elif not fix or fix.lower() in ("none", "n/a"):
                st.info("🔍 **No fix suggestion available.** Please review the expected text above, then compare the PDF with the Claude output to identify the issue.")
            undo_rec = _get_undo_record(cid)
            if undo_rec and undo_rec.get("original_context") and undo_rec.get("new_text"):
                with st.expander("👁️ Applied change (before → after)", expanded=False):
                    p1, p2 = st.columns(2)
                    with p1:
                        st.markdown("**Before:**")
                        st.code(undo_rec["original_context"], language=None)
                    with p2:
                        st.markdown("**After:**")
                        st.code(undo_rec["new_text"], language=None)

            if undo_rec:
                if st.button("↩️ Undo This Fix", key=f"vri_undo_{cid}"):
                    current_text = converted_txt_path.read_text(encoding="utf-8") if converted_txt_path.exists() else ""
                    reverted = current_text.replace(
                        undo_rec["new_text"], undo_rec["original_context"], 1
                    )
                    if reverted != current_text:
                        converted_txt_path.write_text(reverted, encoding="utf-8")
                    for aid in undo_rec["item_ids"]:
                        _unmark_fixed(aid)
                    history = list(st.session_state.get("vri_undo_history", []))
                    history = [r for r in history if r is not undo_rec]
                    st.session_state["vri_undo_history"] = history
                    st.success(f"↩️ Reverted! Items {', '.join(f'#{a}' for a in undo_rec['item_ids'])} unmarked.")
                    st.rerun()

        elif is_error and is_ignored:
            # ── Ignored error — show undo option, no edit box ───────
            st.success(
                f"🔇 Item #{cid} has been **ignored** (marked as false positive). "
                "It no longer counts as an error."
            )
            claude_text = _row_clean_text(selected_row)
            notes = str(selected_row.get("Notes", "") or "")
            fix = str(selected_row.get("Suggested Fix", "") or "")

            if claude_text:
                st.markdown("**Claude Text:**")
                st.code(claude_text, language=None)
            if notes:
                st.info(f"📝 **Notes:** {notes}")
            if fix and fix.lower() not in ("none", "n/a", ""):
                st.warning(f"🔧 **Suggested Fix:** {fix}")
            elif not fix or fix.lower() in ("none", "n/a"):
                st.info("🔍 **No fix suggestion available.** Please review the expected text and surrounding context above, then compare the PDF with the Claude output to identify the issue.")

            if st.button("↩️ Undo Ignore", key=f"vri_unignore_{cid}"):
                _unmark_ignored(cid)
                st.rerun()

        elif is_error:
            st.markdown(f"#### 🛠️ Apply Correction — Item #{cid}")

            st.info("💡 Always verify the correction against the original PDF before applying. The suggested fix is a recommendation — confirm it matches the source document.")

            # Extra-tag items with no page/line: warn that location is approximate
            _cat = str(selected_row.get("Category", "")).lower()
            _has_page = bool(selected_row.get("Page"))
            if _cat == "extra_tag" and not _has_page:
                st.markdown(
                    '<p style="color:#8b0000; background:#fde8e8; padding:8px 12px; '
                    'border-radius:6px; font-size:0.92rem; margin:4px 0;">'
                    '🔁 The flagged issue involves a character that appears in '
                    'multiple locations &mdash; please verify manually for '
                    '100% accuracy.</p>',
                    unsafe_allow_html=True
                )

            claude_text = _row_clean_text(selected_row)
            pymupdf_text = str(selected_row.get("PyMuPDF Text", ""))
            tag_type = str(selected_row.get("Tag Type", "addition"))
            suggested_fix = str(selected_row.get("Suggested Fix", ""))

            notes = str(selected_row.get("Notes", ""))
            if notes:
                st.info(f"📝 **Auditor Notes:** {notes}")
            if suggested_fix and suggested_fix.lower() not in ("none", "n/a", ""):
                classification = str(selected_row.get("Category", "")).lower()
                # Strip any existing trailing "on page N" (possibly followed by parenthetical) from GPT's fix
                clean_fix = re.sub(r'\s+on\s+page\s+\d+(\s*\([^)]*\))?\s*$', '', suggested_fix, flags=re.IGNORECASE).strip()
                loc_parts = []
                page_info = selected_row.get("Page", "")
                line_info = selected_row.get("Line", "")
                if page_info:
                    loc_parts.append(f"page {page_info}")
                # Only show line number for real_miss — partial_match already has context
                if line_info and classification == "real_miss":
                    loc_parts.append(f"line {line_info}")
                loc_suffix = f" on {', '.join(loc_parts)}" if loc_parts else ""
                st.warning(f"🔧 **Suggested Fix:** {clean_fix}{loc_suffix}")
            elif not suggested_fix or suggested_fix.lower() in ("none", "n/a"):
                st.info("🔍 **No fix suggestion available.** Please review the expected text and surrounding context above, then compare the PDF with the Claude output to identify the issue.")

            tag_open = "[D>" if tag_type == "deletion" else "[A>"
            tag_close = "<D]" if tag_type == "deletion" else "<A]"

            _item_page = int(selected_row.get("Page", 0) or 0)

            if claude_text:
                # For very short claude_text (e.g. "(c)"), use Context column
                # to build a better search phrase with surrounding text
                _search = claude_text
                _hint_pos = -1
                _pg_kw = {"target_page": _item_page, "total_pages": total_pages}
                if len(claude_text.strip()) < 15:
                    import re as _re
                    _ctx = str(selected_row.get("Context", "") or "").strip()
                    _is_addition = (tag_type == "addition")
                    if _ctx:
                        # Try to extract text around >>> <<< markers
                        _mm = _re.search(r'>>>\s*(.+?)\s*<<<', _ctx)
                        if _mm:
                            _inner = _mm.group(1).strip()
                            _after = _ctx[_mm.end():].replace("…", "").strip()
                            _before = _ctx[:_mm.start()].replace("…", "").strip()

                            # For ADDITION items the inner text is MISSING from
                            # Claude output — search surrounding context first
                            # (before + after without the inner) exactly like
                            # the Review section shows.
                            if _is_addition and _before and _after:
                                _tail = _before[-40:].strip()
                                for _alen in [len(_after), 60, 40, 30]:
                                    _head = _after[:_alen].strip()
                                    if not _head:
                                        continue
                                    for _c in [_tail + " " + _head, _tail + _head]:
                                        _h, _p = _ws_find(_c, text)
                                        if _h:
                                            _search, _hint_pos = _h, _p
                                            break
                                    if _hint_pos >= 0:
                                        break

                            # For ADDITION: try before alone, then after alone
                            if _is_addition and _hint_pos < 0 and _before:
                                _tail = _before[-40:].strip()
                                if len(_tail) >= 10:
                                    _h, _p = _ws_find(_tail, text)
                                    if _h:
                                        _search, _hint_pos = _h, _p
                            if _is_addition and _hint_pos < 0 and _after:
                                _head = _after[:40].strip()
                                if len(_head) >= 10:
                                    _h, _p = _ws_find(_head, text)
                                    if _h:
                                        _search, _hint_pos = _h, _p

                            # Try: before_tail + inner + after (works for deletions
                            # or when text IS present in Claude output)
                            if _hint_pos < 0 and _before and _after:
                                _tail = _before[-40:].strip()
                                for _alen in [len(_after), 60, 40, 30]:
                                    _head = _after[:_alen].strip()
                                    if not _head:
                                        continue
                                    for _c in [_tail + _inner + _head, _tail + " " + _inner + " " + _head]:
                                        _h, _p = _ws_find(_c, text)
                                        if _h:
                                            _search, _hint_pos = _h, _p
                                            break
                                    if _hint_pos >= 0:
                                        break

                            # Try: inner + after
                            if _hint_pos < 0 and _after:
                                for _alen in [len(_after), 60, 40, 30]:
                                    _head = _after[:_alen].strip()
                                    if not _head:
                                        continue
                                    for _c in [_inner + " " + _head, _inner + _head]:
                                        _h, _p = _ws_find(_c, text)
                                        if _h:
                                            _search, _hint_pos = _h, _p
                                            break
                                    if _hint_pos >= 0:
                                        break

                            # Try: before_tail + inner
                            if _hint_pos < 0 and _before:
                                _tail = _before[-40:].strip()
                                for _c in [_tail + " " + _inner, _tail + _inner]:
                                    _h, _p = _ws_find(_c, text)
                                    if _h:
                                        _search, _hint_pos = _h, _p
                                        break

                            # Fallback: before + after without inner (non-addition)
                            if _hint_pos < 0 and not _is_addition and _before and _after:
                                _tail = _before[-40:].strip()
                                for _alen in [len(_after), 60, 40, 30]:
                                    _head = _after[:_alen].strip()
                                    if not _head:
                                        continue
                                    for _c in [_tail + " " + _head, _tail + _head]:
                                        _h, _p = _ws_find(_c, text)
                                        if _h:
                                            _search, _hint_pos = _h, _p
                                            break
                                    if _hint_pos >= 0:
                                        break

                            # Last resort: before or after alone
                            if _hint_pos < 0 and _before:
                                _tail = _before[-40:].strip()
                                if len(_tail) >= 10:
                                    _h, _p = _ws_find(_tail, text)
                                    if _h:
                                        _search, _hint_pos = _h, _p
                            if _hint_pos < 0 and _after:
                                _head = _after[:40].strip()
                                if len(_head) >= 10:
                                    _h, _p = _ws_find(_head, text)
                                    if _h:
                                        _search, _hint_pos = _h, _p

                        elif len(_ctx) >= 15:
                            _h, _p = _ws_find(_ctx, text)
                            if _h:
                                _search, _hint_pos = _h, _p

                original_context = _extract_edit_context(text, _search,
                    target_page=_item_page, total_pages=total_pages,
                    hint_pos=_hint_pos)
                prefill = original_context
                has_extra_context = (original_context != claude_text)

                # Check if the search text appears multiple times — may show wrong location
                _search_count = text.count(_search) if len(_search.strip()) <= 5 else 0
                if _search_count <= 1 and len(_search.strip()) <= 5:
                    # For very short text, check if original_context appears multiple times
                    _search_count = text.count(original_context) if original_context else 0

                if has_extra_context:
                    st.caption(
                        "✏️ Surrounding context shown — you can **move tag "
                        "boundaries** (e.g. shift `[A>` or `<A]`).  "
                        "Click **Apply Fix** to save."
                    )
                else:
                    st.caption("✏️ Edit the text below. Click **Apply Fix** to save your change.")

                # Warn if the same short text repeats in the PDF (e.g. commas, parentheses)
                if len(claude_text.strip()) <= 3 and _search_count > 1:
                    st.markdown(
                        '<p style="color:#8b0000; background:#fde8e8; padding:8px 12px; '
                        'border-radius:6px; font-size:0.92rem; margin:4px 0;">'
                        '🔁 The flagged issue involves a character that appears in '
                        'multiple locations &mdash; please verify manually for '
                        '100% accuracy.</p>',
                        unsafe_allow_html=True
                    )
            else:
                # Missing text — use surrounding context from Review section
                # (the >>>...<<< markers in Context column) to find the
                # correct location in Claude output, same approach as Review.
                import re as _re
                _ctx = str(selected_row.get("Context", "") or "").strip()
                _search = pymupdf_text
                _hint_pos = -1
                _pg_kw = {"target_page": _item_page, "total_pages": total_pages}

                if _ctx:
                    _mm = _re.search(r'>>>\s*(.+?)\s*<<<', _ctx)
                    if _mm:
                        _inner = _mm.group(1).strip()
                        _after = _ctx[_mm.end():].replace("…", "").strip()
                        _before = _ctx[:_mm.start()].replace("…", "").strip()

                        # For missing additions: inner not in Claude, search before+after
                        # For missing deletions: inner IS in Claude (untagged), search before+inner+after
                        _is_addition = (tag_type == "addition")

                        # Strategy 1: before + inner + after (deletions — text exists untagged)
                        if not _is_addition and _before and _after:
                            _tail = _before[-40:].strip()
                            for _alen in [len(_after), 60, 40, 30]:
                                _head = _after[:_alen].strip()
                                if not _head: continue
                                for _c in [_tail + " " + _inner + " " + _head,
                                           _tail + _inner + _head]:
                                    _h, _p = _ws_find(_c, text)
                                    if _h:
                                        _search, _hint_pos = _h, _p
                                        break
                                if _hint_pos >= 0: break

                        # Strategy 2: before + after (without inner) — works for both
                        if _hint_pos < 0 and _before and _after:
                            _tail = _before[-40:].strip()
                            for _alen in [len(_after), 60, 40, 30]:
                                _head = _after[:_alen].strip()
                                if not _head: continue
                                for _c in [_tail + " " + _head, _tail + _head]:
                                    _h, _p = _ws_find(_c, text)
                                    if _h:
                                        _search, _hint_pos = _h, _p
                                        break
                                if _hint_pos >= 0: break

                        # Strategy 3: before alone
                        if _hint_pos < 0 and _before:
                            _tail = _before[-40:].strip()
                            if len(_tail) >= 10:
                                _h, _p = _ws_find(_tail, text)
                                if _h:
                                    _search, _hint_pos = _h, _p

                        # Strategy 4: after alone
                        if _hint_pos < 0 and _after:
                            _head = _after[:40].strip()
                            if len(_head) >= 10:
                                _h, _p = _ws_find(_head, text)
                                if _h:
                                    _search, _hint_pos = _h, _p

                        # Strategy 5: inner alone (for deletions)
                        if _hint_pos < 0 and not _is_addition and len(_inner) >= 5:
                            _h, _p = _ws_find(_inner, text)
                            if _h:
                                _search, _hint_pos = _h, _p

                if _hint_pos >= 0:
                    original_context = _extract_edit_context(text, _search,
                        target_page=_item_page, total_pages=total_pages,
                        hint_pos=_hint_pos)
                else:
                    # Fallback to old multi-strategy search
                    original_context = _find_anchor_context_multi(text, selected_row, total_pages=total_pages)

                if original_context:
                    prefill = original_context
                    st.caption(
                        "⚠️ This text is **missing** from the Claude output. "
                        "Nearby context is shown below. Edit the text to add "
                        "the missing content at the correct position, then "
                        "click **Apply Fix** to save."
                    )
                    # Warn for short repeated text in PDF
                    if len(pymupdf_text.strip()) <= 3:
                        st.markdown(
                            '<p style="color:#8b0000; background:#fde8e8; padding:8px 12px; '
                            'border-radius:6px; font-size:0.92rem; margin:4px 0;">'
                            '🔁 The flagged issue involves a character that appears in '
                            'multiple locations &mdash; please verify manually for '
                            '100% accuracy.</p>',
                            unsafe_allow_html=True
                        )
                else:
                    prefill = ""
                    # No anchor found — don't show text box, just show info + buttons
                    _no_anchor = True

            if not locals().get('_no_anchor', False):
                fc1, fc2 = st.columns([3, 1])
                with fc1:
                    new_text = st.text_area(
                        "Edit text", value=prefill, height=200,
                        key=f"vri_edit_{cid}", label_visibility="collapsed",
                    )
                with fc2:
                    st.markdown("")
                    st.markdown("")
                    if claude_text:
                        pending_key = f"vri_pending_apply_{cid}"
                        is_pending = st.session_state.get(pending_key, False)

                        if not is_pending:
                            if st.button("✅ Apply Fix", type="primary", key=f"vri_apply_{cid}"):
                                if new_text.strip() and new_text != original_context:
                                    st.session_state[pending_key] = True
                                    st.rerun()
                                elif new_text == original_context:
                                    st.warning("No changes made.")
                            if st.button("🔇 Ignore", key=f"vri_ignore_nochange_{cid}",
                                         help="Mark this item as a false positive — no edit needed"):
                                _mark_ignored(cid)
                                st.rerun()
                        else:
                            affected_preview = _find_affected_items(
                                checklist, fixed_ids,
                                original_context, new_text, cid,
                                current_page=_item_page,
                            )
                            st.info(
                                f"⚡ **{len(affected_preview)} item(s)** will be "
                                f"marked fixed: {', '.join(f'#{a}' for a in affected_preview)}"
                            )
                            cc1, cc2 = st.columns(2)
                            with cc1:
                                if st.button("✅ Confirm", type="primary", key=f"vri_confirm_{cid}"):
                                    updated = text.replace(original_context, new_text, 1)
                                    if updated != text:
                                        converted_txt_path.write_text(updated, encoding="utf-8")
                                        affected = _find_affected_items(
                                            checklist, fixed_ids,
                                            original_context, new_text, cid,
                                            current_page=_item_page,
                                        )
                                        for aid in affected:
                                            _mark_fixed(aid)
                                        _save_undo_history(
                                            affected, original_context,
                                            new_text,
                                            str(converted_txt_path),
                                        )
                                        st.session_state.pop(pending_key, None)
                                        if len(affected) > 1:
                                            st.success(
                                                f"✅ Saved! **{len(affected)} items** "
                                                f"marked fixed: {', '.join(f'#{a}' for a in affected)}"
                                            )
                                        else:
                                            st.success("✅ Saved & updated!")
                                        # Auto-advance to next unfixed error
                                        _next_errors = _get_error_ids(checklist,
                                                                      _get_fixed_ids(),
                                                                      _get_ignored_ids())
                                        if _next_errors:
                                            st.session_state.vri_selected_item = _next_errors[0]
                                        st.rerun()
                                    else:
                                        st.error("Text not found in file.")
                                        st.session_state.pop(pending_key, None)
                            with cc2:
                                if st.button("❌ Cancel", key=f"vri_cancel_{cid}"):
                                    st.session_state.pop(pending_key, None)
                                    st.rerun()
                    else:
                        # Missing text — use the same Apply Fix → Confirm flow
                        if original_context:
                            pending_key = f"vri_pending_apply_{cid}"
                            is_pending = st.session_state.get(pending_key, False)

                            if not is_pending:
                                if st.button("✅ Apply Fix", type="primary", key=f"vri_apply_{cid}"):
                                    if new_text.strip() and new_text != original_context:
                                        st.session_state[pending_key] = True
                                        st.rerun()
                                    elif new_text == original_context:
                                        st.warning("No changes made — edit the text to include the missing content.")
                                if st.button("🔇 Ignore", key=f"vri_ignore_missing_{cid}",
                                             help="Mark this as a false positive — no action needed"):
                                    _mark_ignored(cid)
                                    st.rerun()
                            else:
                                st.info(
                                    f"⚡ **1 item** will be inserted & "
                                    f"marked fixed: #{cid}"
                                )
                                cc1, cc2 = st.columns(2)
                                with cc1:
                                    if st.button("✅ Confirm", type="primary", key=f"vri_confirm_{cid}"):
                                        updated = text.replace(original_context, new_text, 1)
                                        if updated != text:
                                            converted_txt_path.write_text(updated, encoding="utf-8")
                                            _mark_fixed(cid)
                                            _save_undo_history(
                                                [cid], original_context,
                                                new_text,
                                                str(converted_txt_path),
                                            )
                                            st.session_state.pop(pending_key, None)
                                            st.success(f"✅ Saved! Item #{cid} inserted & marked fixed!")
                                            _next_errors = _get_error_ids(checklist,
                                                                          _get_fixed_ids(),
                                                                          _get_ignored_ids())
                                            if _next_errors:
                                                st.session_state.vri_selected_item = _next_errors[0]
                                            st.rerun()
                                        else:
                                            st.error("Anchor context not found in file — text may have changed.")
                                            st.session_state.pop(pending_key, None)
                                with cc2:
                                    if st.button("❌ Cancel", key=f"vri_cancel_{cid}"):
                                        st.session_state.pop(pending_key, None)
                                        st.rerun()

                # Preview diff
                if original_context and new_text != original_context:
                    _any_pending = st.session_state.get(f"vri_pending_apply_{cid}", False)
                    if _any_pending:
                        with st.expander("👁️ Preview change", expanded=True):
                            p1, p2 = st.columns(2)
                            with p1:
                                st.markdown("**Before:**")
                                st.code(original_context, language=None)
                            with p2:
                                st.markdown("**After:**")
                                st.code(new_text, language=None)
            else:
                # No anchor — show info message and Mark as Fixed / Ignore only
                st.info(
                    "📌 **Text not found** due to the internal structure of the PDF. "
                    "This item needs to be handled manually once all other errors and "
                    "warnings are **Fixed / Ignored**."
                )
                _defer_c1, _defer_c2 = st.columns([1, 1])
                with _defer_c1:
                    if st.button("✅ Mark as Fixed", type="primary", key=f"vri_copy_na_{cid}",
                                 help="Mark this item as manually resolved"):
                        _mark_fixed(cid)
                        _save_undo_history([cid], "", "", str(converted_txt_path))
                        _next_errors = _get_error_ids(checklist,
                                                      _get_fixed_ids(),
                                                      _get_ignored_ids())
                        if _next_errors:
                            st.session_state.vri_selected_item = _next_errors[0]
                        st.rerun()
                with _defer_c2:
                    if st.button("🔇 Ignore", key=f"vri_ignore_na_{cid}",
                                 help="Mark as false positive — no action needed"):
                        _mark_ignored(cid)
                        st.rerun()
        else:
            # ── Warning / pass items ────────────────────────────────
            is_warning = str(selected_row.get("Verdict", "")).startswith("⚠")
            is_ignored = cid in ignored_ids

            st.markdown("**Clean Output Text:**")
            st.code(_row_clean_text(selected_row), language=None)
            notes = str(selected_row.get("Notes", ""))
            fix = str(selected_row.get("Suggested Fix", ""))
            if notes:
                st.info(f"📝 **Notes:** {notes}")
            if fix and fix.lower() not in ("none", "n/a", ""):
                st.warning(f"🔧 **Suggested Fix:** {fix}")

            # Ignore / Undo-Ignore for warnings
            if is_warning:
                if is_ignored:
                    st.success(f"🔇 Item #{cid} has been **ignored** (false positive). It now counts as passing.")
                    if st.button("↩️ Undo Ignore", key=f"vri_unignore_{cid}"):
                        _unmark_ignored(cid)
                        st.rerun()
                else:
                    ig1, ig2 = st.columns([3, 1])
                    with ig2:
                        if st.button("🔇 Ignore (false positive)", key=f"vri_ignore_{cid}",
                                     width="stretch",
                                     help="Mark this warning as a false positive — it will count as passing"):
                            _mark_ignored(cid)
                            st.rerun()

    # ══════════════════════════════════════════════════════════════
    # TWO-COLUMN LAYOUT: PDF Page | Claude Text (at the bottom)
    # ══════════════════════════════════════════════════════════════
    st.markdown("---")

    # Edit mode only available when ALL errors are fixed AND all warnings are ignored
    _remaining_warnings = sum(
        1 for r in checklist
        if str(r.get("Verdict", "")).startswith("\u26a0")
        and int(r.get("#", 0)) not in ignored_ids
    )
    _all_issues_resolved = (len(error_ids) == 0) and (_remaining_warnings == 0)

    _render_twocol_view(
        page_images=page_images,
        total_pages=total_pages,
        text=text,
        checklist=checklist,
        selected_id=selected_id,
        filter_mode=filter_mode,
        fixed_ids=fixed_ids,
        ignored_ids=ignored_ids,
        all_errors_resolved=_all_issues_resolved,
        converted_txt_path=converted_txt_path,
    )

    # ── Download button — centered below both columns ──────────────
    if converted_txt_path.exists():
        try:
            _dl_bytes = converted_txt_path.read_bytes()
            _dl_name = converted_txt_path.name
            _dl_b64 = base64.b64encode(_dl_bytes).decode()
            _dl_size = len(_dl_bytes)
            _dl_label = f"{_dl_size / 1024:.1f} KB" if _dl_size < 1024 * 1024 else f"{_dl_size / (1024*1024):.1f} MB"
            st.markdown(
                f'<div style="text-align:center; margin:12px 0 4px;">'
                f'<a href="data:text/plain;base64,{_dl_b64}" download="{_dl_name}" '
                f'style="display:inline-block; font-size:0.84rem; '
                f'color:#1e40af; text-decoration:none; background:#eff6ff; '
                f'padding:8px 20px; border-radius:8px; border:1px solid #bfdbfe;">'
                f'📥 Download converted text ({_dl_label})</a></div>',
                unsafe_allow_html=True,
            )
        except Exception:
            pass

    # ── Download Excel ──────────────────────────────────────────
    st.markdown("---")
    try:
        with open(excel_path, "rb") as f:
            xl_bytes = f.read()
        st.download_button(
            label="📊 Download GPT Audit Excel",
            data=xl_bytes,
            file_name=excel_path.name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
        )
    except Exception:
        pass

    # ── Footer ──────────────────────────────────────────────────────
    st.caption(
        f"📊 {excel_path.name}  •  "
        f"Items: {summary.get('total',0)}  •  "
        f"Fixed: {n_fixed}"
    )
