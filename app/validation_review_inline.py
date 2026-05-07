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
from pathlib import Path
from typing import Optional

import streamlit as st
import streamlit.components.v1 as components
import openpyxl

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
        claude_text = str(row.get("Claude Text", "")).strip()

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
                          total_pages: int = 0) -> str:
    if not search_text:
        return search_text

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

    if len(positions) == 1 or target_page <= 0 or total_pages <= 0:
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
        ct = str(row.get("Claude Text", "") or "").strip()
        if not ct or len(ct) < 10:
            continue  # skip very short text — too many false positives
        # Only consider items on the same or adjacent page
        item_page = int(row.get("Page", 0) or 0)
        if current_page > 0 and item_page > 0 and abs(item_page - current_page) > 1:
            continue
        if ct in original_context:
            if ct not in new_text:
                affected.append(cid)
    # Always include the current item
    affected.append(current_cid)
    return affected


def _find_anchor_context_multi(text: str, row: dict, total_pages: int = 0) -> str:
    """Try multiple strategies to find anchor context in the converted text.

    Returns the anchor context string (a substring of *text* that can be
    used as the search-and-replace target), or empty string if nothing found.
    """
    target_page = int(row.get("Page", 0) or 0)
    _pg = {"target_page": target_page, "total_pages": total_pages}

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
            if len(inner) >= 5 and inner in text:
                return _extract_edit_context(text, inner, **_pg)
            before_marker = ctx_col[:marker_match.start()].replace("…", "").strip()
            after_marker = ctx_col[marker_match.end():].replace("…", "").strip()

            # When inner is short (e.g. "(a)"), try inner + after_marker
            if after_marker and len(inner) < 10:
                combined_fwd = inner + " " + after_marker[:50].strip()
                combined_fwd_ns = inner + after_marker[:50].strip()
                if combined_fwd in text:
                    return _extract_edit_context(text, combined_fwd, **_pg)
                if combined_fwd_ns in text:
                    return _extract_edit_context(text, combined_fwd_ns, **_pg)

            # Try combining tail of before + inner + head of after
            if before_marker:
                tail = before_marker[-40:].strip()
                combined = tail + inner
                if combined in text:
                    return _extract_edit_context(text, combined, **_pg)
            # For missing text: search using surrounding context WITHOUT the
            # inner text (it may not exist in the converted file).
            # Combine tail of before + head of after as the anchor.
            if before_marker and after_marker:
                tail = before_marker[-30:].strip()
                head = after_marker[:30].strip()
                surround = tail + " " + head
                if surround in text:
                    return _extract_edit_context(text, surround, **_pg)
                # Try without space (text may be adjacent)
                surround_nospace = tail + head
                if surround_nospace in text:
                    return _extract_edit_context(text, surround_nospace, **_pg)
            # Try just the tail of before-marker text
            if before_marker:
                tail = before_marker[-40:].strip()
                if len(tail) >= 10 and tail in text:
                    return _extract_edit_context(text, tail, **_pg)
            # Try just the head of after-marker text
            if after_marker:
                head = after_marker[:40].strip()
                if len(head) >= 10 and head in text:
                    return _extract_edit_context(text, head, **_pg)
        # Try full cleaned context
        ctx_clean = ctx_col.replace(">>>", "").replace("<<<", "").replace("…", "").strip()
        if ctx_clean and len(ctx_clean) >= 10 and ctx_clean in text:
            return _extract_edit_context(text, ctx_clean, **_pg)

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
    n_diffs = accuracy_report["real_diff_count"]
    n_repos = accuracy_report["repositioned_count"]
    n_trans = accuracy_report.get("transposed_count", 0)
    n_punct = accuracy_report.get("punctuation_count", 0)
    n_word  = accuracy_report.get("word_diff_count", n_diffs)
    status = accuracy_report["status"]

    # If identical, no expander needed
    if status == "IDENTICAL" and n_diffs == 0 and n_repos == 0:
        return

    # Build breakdown chips for the label
    _chips = []
    if n_word:  _chips.append(f"{n_word} word")
    if n_trans: _chips.append(f"{n_trans} transposed")
    if n_punct: _chips.append(f"{n_punct} punctuation")
    _chip_str = ", ".join(_chips) if _chips else f"{n_diffs} difference{'s' if n_diffs != 1 else ''}"

    label = (
        f"📊 Text Accuracy Report — {acc:.1f}% "
        f"({_chip_str}"
        f"{f', {n_repos} repositioned' if n_repos else ''})"
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
        if n_diffs > 0 and (n_trans > 0 or n_punct > 0):
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
            if n_repos:
                _pills_html += (f'<span style="{_pill} background:#f0fdf4; color:#166534;">'
                                f'↔️ {n_repos} Repositioned</span>')
            st.markdown(f'<div style="margin:8px 0 12px;">{_pills_html}</div>',
                        unsafe_allow_html=True)

        if status == "CONTENT_MATCH":
            st.success(
                f"✅ All content present in both files. "
                f"{n_repos} block(s) repositioned but no missing content."
            )

        # Real differences — only show MISSING in Claude output
        missing_diffs = [d for d in accuracy_report["real_diffs"]
                         if d["type"] == "MISSING_IN_CONVERTED"]
        if missing_diffs:
            st.markdown("#### ⚠️ Missing in Claude Output")
            for idx, d in enumerate(missing_diffs, 1):
                st.markdown(
                    f"**#{idx} MISSING in Claude output**\n\n"
                    f"- **Missing:** {d['missing_text']}\n\n"
                    f"> *Sentence:* {d['sentence']}"
                )
                if idx < len(missing_diffs):
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
            st.markdown(
                '<p style="font-size:0.92rem;font-weight:700;margin:0 0 6px;">'
                '📄 PDF Page — Original Reference</p>',
                unsafe_allow_html=True,
            )

            if total_pages > 0:
                cur = st.session_state.vri_current_page
                cur = max(1, min(cur, total_pages))
                st.session_state.vri_current_page = cur  # clamp into session

                _nav_bar_style = (
                    'display:flex; align-items:center; gap:8px; '
                    'padding:6px 14px; margin-bottom:8px; min-height:42px; '
                    'background:#f8fafc; border:1px solid #e2e8f0; border-radius:8px;'
                )
                if total_pages > 1:
                    nc1, nc2, nc3, nc4 = st.columns([1, 5, 1, 3])
                    with nc1:
                        if st.button("◀", key="vri_pg_prev"):
                            st.session_state.vri_current_page = max(1, cur - 1)
                            st.rerun()
                    with nc3:
                        if st.button("▶", key="vri_pg_next"):
                            st.session_state.vri_current_page = min(total_pages, cur + 1)
                            st.rerun()
                    with nc2:
                        # Sync slider widget key to session state so external
                        # page changes (item nav, ◀/▶ buttons) are reflected
                        st.session_state["vri_pg_slider"] = cur
                        new_pg = st.slider(
                            "Page", 1, total_pages,
                            key="vri_pg_slider", label_visibility="collapsed",
                        )
                        if new_pg != cur:
                            st.session_state.vri_current_page = new_pg
                    with nc4:
                        st.markdown(
                            f'<p style="font-size:0.82rem;color:#64748b;margin:8px 0 0;text-align:right;">'
                            f'Page <b>{cur}</b> / {total_pages}</p>',
                            unsafe_allow_html=True,
                        )
                else:
                    st.markdown(
                        f'<div style="{_nav_bar_style} justify-content:center;">'
                        '<span style="font-size:0.82rem;color:#64748b;">'
                        'Page <b>1</b> / 1</span></div>',
                        unsafe_allow_html=True,
                    )

                cur = st.session_state.vri_current_page
                img_idx = cur - 1
                if 0 <= img_idx < len(page_images):
                    st.image(str(page_images[img_idx]), width="stretch")

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
                all_errors_resolved
                and converted_txt_path is not None
                and st.session_state.get("vri_edit_mode", False)
            )

            if all_errors_resolved and converted_txt_path is not None:
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

    # JS: sync text-viewer / edit textarea height to the PDF image height
    components.html("""
    <script>
    (function syncHeight() {
        const doc = window.parent.document;
        const container = doc.querySelector('.st-key-vri_twocol');
        if (!container) return;
        const img = container.querySelector('img');
        if (!img) return;
        function adjust() {
            const h = img.clientHeight;
            if (h <= 100) return;
            // Sync read-only viewer
            const viewer = container.querySelector('.vr-text-viewer');
            if (viewer) { viewer.style.maxHeight = h + 'px'; }
            // Sync edit-mode textarea: measure all sibling elements in the
            // right column except the textarea container, then fill the rest.
            const ta = container.querySelector('textarea');
            if (ta) {
                // Walk up to the stVerticalBlock that holds all right-column widgets
                var block = ta.closest('[data-testid="stVerticalBlock"]');
                if (block) {
                    var usedHeight = 0;
                    for (var i = 0; i < block.children.length; i++) {
                        var child = block.children[i];
                        // Skip the element that contains the textarea itself
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
        if (img.complete && img.clientHeight > 0) {
            adjust();
        } else {
            img.addEventListener('load', adjust);
        }
        new ResizeObserver(adjust).observe(img);
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
    _current_file_key = str(converted_txt_path)
    _prev_file_key = st.session_state.get("vri_file_key", "")
    _vri_state_keys = ("vri_fixed_ids", "vri_ignored_ids", "vri_undo_history",
                       "vri_selected_item", "vri_current_page", "vri_filter_mode",
                       "vri_last_sel", "vri_edit_mode")
    if _current_file_key != _prev_file_key:
        # Save current state under the previous file key
        if _prev_file_key:
            _saved = {}
            for _k in _vri_state_keys:
                if _k in st.session_state:
                    _saved[_k] = st.session_state[_k]
            if _saved:
                _all_saved = st.session_state.get("vri_per_file_state", {})
                _all_saved[_prev_file_key] = _saved
                st.session_state["vri_per_file_state"] = _all_saved

        # Restore saved state for the new file (if any), otherwise clear
        _all_saved = st.session_state.get("vri_per_file_state", {})
        _restored = _all_saved.get(_current_file_key, {})
        for _k in _vri_state_keys:
            if _k in _restored:
                st.session_state[_k] = _restored[_k]
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

        # ── Still show the side-by-side PDF ↔ Claude text view ──────
        page_images = _get_page_images(highlighted_dir)
        total_pages = len(page_images)
        text = ""
        if converted_txt_path.exists():
            text = converted_txt_path.read_text(encoding="utf-8")

        if page_images or text:
            if "vri_current_page" not in st.session_state:
                st.session_state.vri_current_page = 1
            st.markdown("---")
            if gpt_audit_failed:
                st.markdown("### 📝 Manual Review — Original PDF vs Converted Text")
                st.caption("Compare the highlighted PDF pages on the left with the Claude converted text on the right. "
                           "You can edit the converted text directly.")
            else:
                st.markdown("### 📑 Side-by-Side Comparison")
            _render_twocol_view(
                page_images=page_images,
                total_pages=total_pages,
                text=text,
                checklist=[],
                selected_id=None,
                filter_mode="all",
                fixed_ids=set(),
                ignored_ids=set(),
                all_errors_resolved=True,
                converted_txt_path=converted_txt_path,
            )

        # ── Download button — show when no checklist items ─────────────
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
    if selected_row:
        try:
            sel_item_page = int(selected_row.get("Page", 1))
        except (ValueError, TypeError):
            sel_item_page = 1

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
            if err_pos > 0:
                st.markdown(f"""
                <div class="vr-error-nav-bar">
                    <span class="nav-label">❌ Error {err_pos} of {err_total}  (Item #{selected_id})</span>
                    <span class="nav-page">Page {sel_item_page}</span>
                </div>
                """, unsafe_allow_html=True)
            else:
                st.markdown(f"""
                <div class="vr-error-nav-bar">
                    <span class="nav-label">📋 Item #{selected_id}</span>
                    <span class="nav-page">Page {sel_item_page}  •  {err_total} errors remaining</span>
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
    # TWO-COLUMN LAYOUT: PDF Page | Claude Text
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
            claude_text = str(selected_row.get("Claude Text", "") or "")
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
            claude_text = str(selected_row.get("Claude Text", "") or "")
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

            claude_text = str(selected_row.get("Claude Text", "") or "")
            pymupdf_text = str(selected_row.get("PyMuPDF Text", ""))
            tag_type = str(selected_row.get("Tag Type", "addition"))
            suggested_fix = str(selected_row.get("Suggested Fix", ""))

            notes = str(selected_row.get("Notes", ""))
            if notes:
                st.info(f"📝 **Auditor Notes:** {notes}")
            if suggested_fix and suggested_fix.lower() not in ("none", "n/a", ""):
                st.warning(f"🔧 **Suggested Fix:** {suggested_fix}")
            elif not suggested_fix or suggested_fix.lower() in ("none", "n/a"):
                st.info("🔍 **No fix suggestion available.** Please review the expected text and surrounding context above, then compare the PDF with the Claude output to identify the issue.")

            st.info("💡 Always verify the correction against the original PDF before applying. The suggested fix is a recommendation — confirm it matches the source document.")

            tag_open = "[D>" if tag_type == "deletion" else "[A>"
            tag_close = "<D]" if tag_type == "deletion" else "<A]"

            _item_page = int(selected_row.get("Page", 0) or 0)

            if claude_text:
                # For very short claude_text (e.g. "(c)"), use Context column
                # to build a better search phrase with surrounding text
                _search = claude_text
                if len(claude_text.strip()) < 15:
                    import re as _re
                    _ctx = str(selected_row.get("Context", "") or "").strip()
                    if _ctx:
                        # Try to extract text around >>> <<< markers
                        _mm = _re.search(r'>>>\s*(.+?)\s*<<<', _ctx)
                        if _mm:
                            _after = _ctx[_mm.end():].replace("…", "").strip()
                            if _after and len(_after) >= 5:
                                # Search for inner + after_text as combined anchor
                                _combined = _mm.group(1).strip() + " " + _after[:40].strip()
                                # Also try without space
                                _combined_ns = _mm.group(1).strip() + _after[:40].strip()
                                if _combined in text:
                                    _search = _combined
                                elif _combined_ns in text:
                                    _search = _combined_ns
                            _before = _ctx[:_mm.start()].replace("…", "").strip()
                            if _search == claude_text and _before and len(_before) >= 5:
                                _combined = _before[-40:].strip() + " " + _mm.group(1).strip()
                                _combined_ns = _before[-40:].strip() + _mm.group(1).strip()
                                if _combined in text:
                                    _search = _combined
                                elif _combined_ns in text:
                                    _search = _combined_ns
                        elif len(_ctx) >= 15 and _ctx in text:
                            _search = _ctx

                original_context = _extract_edit_context(text, _search,
                    target_page=_item_page, total_pages=total_pages)
                prefill = original_context
                has_extra_context = (original_context != claude_text)
                if has_extra_context:
                    st.caption(
                        "✏️ Surrounding context shown — you can **move tag "
                        "boundaries** (e.g. shift `[A>` or `<A]`).  "
                        "Click **Apply Fix** to save."
                    )
                else:
                    st.caption("✏️ Edit the text below. Click **Apply Fix** to save your change.")
            else:
                # Missing text — try multiple strategies to find anchor context
                original_context = _find_anchor_context_multi(text, selected_row, total_pages=total_pages)
                if original_context:
                    prefill = original_context
                    st.caption(
                        "⚠️ This text is **missing** from the Claude output. "
                        "Nearby context is shown below. Edit the text to add "
                        "the missing content at the correct position, then "
                        "click **Apply Fix** to save."
                    )
                else:
                    prefill = ""
                    st.caption(
                        "⚠️ This text is **missing** from the Claude output. "
                        "It was found by PyMuPDF but not included in the conversion. "
                        "Use the **search box** below the edit area to locate nearby "
                        "text and apply the fix, or **Mark as Fixed** / **Ignore**."
                    )

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
                    # when we have an anchor context to locate the insertion point
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
                                        st.rerun()
                                    else:
                                        st.error("Anchor context not found in file — text may have changed.")
                                        st.session_state.pop(pending_key, None)
                            with cc2:
                                if st.button("❌ Cancel", key=f"vri_cancel_{cid}"):
                                    st.session_state.pop(pending_key, None)
                                    st.rerun()
                    else:
                        # No automatic anchor — let the user search for nearby text
                        st.caption(
                            "⚠️ No automatic anchor found in the file. "
                            "**Search** for nearby text below to locate where the fix should go, "
                            "then click **Apply Fix**. Or use **Mark as Fixed** / **Ignore**."
                        )
                        search_term = st.text_input(
                            "🔍 Search for nearby text in the file",
                            key=f"vri_search_{cid}",
                            placeholder="Type text that appears near where the fix should be inserted…",
                        )
                        _search_anchor = ""
                        if search_term and search_term.strip() in text:
                            _search_anchor = _extract_edit_context(text, search_term.strip())
                        elif search_term:
                            st.error("❌ Text not found in the file. Try a different search term.")

                        if _search_anchor:
                            # User found an anchor — show Apply Fix flow
                            st.success("✅ Found! Edit the text below to add the missing content, then click **Apply Fix**.")
                            search_prefill = _search_anchor
                            search_new_text = st.text_area(
                                "Edit text", value=search_prefill, height=200,
                                key=f"vri_search_edit_{cid}", label_visibility="collapsed",
                            )
                            s_pending_key = f"vri_pending_search_apply_{cid}"
                            s_is_pending = st.session_state.get(s_pending_key, False)

                            sc1, sc2 = st.columns([1, 1])
                            if not s_is_pending:
                                with sc1:
                                    if st.button("✅ Apply Fix", type="primary", key=f"vri_search_apply_{cid}"):
                                        if search_new_text.strip() and search_new_text != _search_anchor:
                                            st.session_state[s_pending_key] = True
                                            st.rerun()
                                        elif search_new_text == _search_anchor:
                                            st.warning("No changes made — edit the text to include the missing content.")
                                with sc2:
                                    if st.button("🔇 Ignore", key=f"vri_ignore_search_{cid}",
                                                 help="Mark as false positive"):
                                        _mark_ignored(cid)
                                        st.rerun()
                            else:
                                st.info(f"⚡ **1 item** will be inserted & marked fixed: #{cid}")
                                sc_c1, sc_c2 = st.columns(2)
                                with sc_c1:
                                    if st.button("✅ Confirm", type="primary", key=f"vri_search_confirm_{cid}"):
                                        updated = text.replace(_search_anchor, search_new_text, 1)
                                        if updated != text:
                                            converted_txt_path.write_text(updated, encoding="utf-8")
                                            _mark_fixed(cid)
                                            _save_undo_history(
                                                [cid], _search_anchor,
                                                search_new_text,
                                                str(converted_txt_path),
                                            )
                                            st.session_state.pop(s_pending_key, None)
                                            st.success(f"✅ Saved! Item #{cid} inserted & marked fixed!")
                                            st.rerun()
                                        else:
                                            st.error("Anchor context not found — text may have changed.")
                                            st.session_state.pop(s_pending_key, None)
                                with sc_c2:
                                    if st.button("❌ Cancel", key=f"vri_search_cancel_{cid}"):
                                        st.session_state.pop(s_pending_key, None)
                                        st.rerun()

                            # Preview diff
                            if _search_anchor and search_new_text != _search_anchor:
                                with st.expander("👁️ Preview change", expanded=True):
                                    sp1, sp2 = st.columns(2)
                                    with sp1:
                                        st.markdown("**Before:**")
                                        st.code(_search_anchor, language=None)
                                    with sp2:
                                        st.markdown("**After:**")
                                        st.code(search_new_text, language=None)
                        else:
                            # Fallback — Mark as Fixed / Ignore (no file edit)
                            fb1, fb2 = st.columns([1, 1])
                            with fb1:
                                if st.button("✅ Mark as Fixed", type="primary", key=f"vri_copy_{cid}",
                                             help="Mark this item as manually resolved"):
                                    _mark_fixed(cid)
                                    _save_undo_history([cid], "", "", str(converted_txt_path))
                                    st.success(f"✅ Item #{cid} marked as fixed!")
                                    st.rerun()
                            with fb2:
                                if st.button("🔇 Ignore", key=f"vri_ignore_missing_{cid}",
                                             help="Mark as false positive — no action needed"):
                                    _mark_ignored(cid)
                                    st.rerun()

            # Preview diff — show only after user clicks Apply Fix (pending confirmation)
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
            # ── Warning / pass items ────────────────────────────────
            is_warning = str(selected_row.get("Verdict", "")).startswith("⚠")
            is_ignored = cid in ignored_ids

            st.markdown("**Claude Text:**")
            st.code(str(selected_row.get("Claude Text", "")), language=None)
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
    # CHECKLIST TABLE
    # ══════════════════════════════════════════════════════════════
    st.markdown("---")
    st.markdown("#### 📋 Full Audit Checklist")

    if not display_cl:
        st.info("No items match the current filter.")
    else:
        tbl = '<table class="vr-cl-table"><thead><tr>'
        for c in ["#", "Status", "Page", "Tag Type", "Expected Text (PDF)",
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
            _claude_full = _html_escape(str(row.get("Claude Text", "")))
            _claude_disp = _html_escape(_trunc(row.get("Claude Text", "")))
            _notes_full = _html_escape(str(row.get("Notes", "")))
            _notes_disp = _html_escape(_trunc(row.get("Notes", ""), 100))

            tbl += f'<td><strong>{cid_int}</strong></td>'
            tbl += f'<td><span class="vr-badge {bc}">{_html_escape(verd)}</span></td>'
            tbl += f'<td>{row.get("Page", "")}</td>'
            tbl += f'<td>{_html_escape(str(row.get("Tag Type", "")))}</td>'
            tbl += f'<td title="{_pymupdf_full}">{_pymupdf_disp}</td>'
            tbl += f'<td>{_html_escape(str(row.get("In Claude?", "")))}</td>'
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
                # Force the selectbox to reflect the current selected_id.
                # Streamlit ignores the `index` param when a keyed widget already
                # has a browser-submitted value, so we write directly.
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
    # ADDITIONAL FINDINGS
    # ══════════════════════════════════════════════════════════════
    if findings:
        st.markdown("---")
        st.markdown(f"#### 🔔 Additional Findings ({len(findings)})")
        for af in findings:
            it = str(af.get("Issue Type", ""))
            cls = "vr-af-error" if it in ("content_dropped", "tag_error", "text_error") else "vr-af-warn"
            st.markdown(f"""
            <div class="vr-af-card {cls}">
                <strong>Page {af.get("Page","")}</strong> — <em>{_html_escape(it)}</em><br>
                {_html_escape(str(af.get("Description", "")))}
            </div>
            """, unsafe_allow_html=True)

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
