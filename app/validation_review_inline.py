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
import openpyxl
from PIL import Image, ImageDraw, ImageFont

# ── Constants ───────────────────────────────────────────────────────
PDF_PTS_PER_PAGE = (612.0, 792.0)  # standard US Letter in points


# ═══════════════════════════════════════════════════════════════════
# CUSTOM CSS
# ═══════════════════════════════════════════════════════════════════
_CSS = """
<style>
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
}
.vr-error-nav-bar .nav-label { font-weight: 700; font-size: 1.05rem; color: #92400e; }
.vr-error-nav-bar .nav-page  { font-size: 0.85rem; color: #78350f; opacity:0.8; }

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

/* text viewer */
.vr-text-viewer {
    font-family: 'Cascadia Code','Consolas', monospace;
    font-size: 0.82rem; line-height: 1.65;
    white-space: pre-wrap; word-break: break-word;
    background: #fafafa; border: 1px solid #e2e8f0;
    border-radius: 8px; padding: 16px;
    max-height: 620px; overflow-y: auto;
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
# IMAGE ANNOTATION — YELLOW error highlights on page images
# ═══════════════════════════════════════════════════════════════════

def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def _build_page_annotations(checklist: list[dict], json_report: dict,
                            fixed_ids: set[int],
                            ignored_ids: set[int] | None = None) -> dict[int, list[dict]]:
    spans = json_report.get("formatted_spans", [])
    annotations: dict[int, list[dict]] = {}
    ignored_ids = ignored_ids or set()

    for row in checklist:
        cid = int(row.get("#", 0))
        verdict = str(row.get("Verdict", ""))
        try:
            page = int(row.get("Page", 0))
        except (ValueError, TypeError):
            continue
        pymupdf_text = _norm_ws(str(row.get("PyMuPDF Text", "")))

        if cid in fixed_ids or cid in ignored_ids:
            vtype = "fixed"
        elif verdict.startswith("✅"):
            vtype = "pass"
        elif verdict.startswith("⚠"):
            vtype = "warn"
        else:
            vtype = "err"

        if vtype == "pass":
            continue

        best_bbox = None
        for sp in spans:
            if sp.get("page") != page:
                continue
            sp_text = _norm_ws(sp.get("text", ""))
            if sp_text == pymupdf_text or pymupdf_text in sp_text or sp_text in pymupdf_text:
                best_bbox = sp.get("bbox")
                break

        if best_bbox and page > 0:
            annotations.setdefault(page, []).append({
                "bbox": best_bbox, "vtype": vtype,
                "cid": cid,
                "text": str(row.get("PyMuPDF Text", ""))[:60],
            })

    return annotations


def _annotate_page_image(img_path: Path, annotations: list[dict],
                         selected_id: int | None) -> Image.Image:
    img = Image.open(img_path).convert("RGBA")
    if not annotations:
        return img.convert("RGB")

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    iw, ih = img.size
    scale_x = iw / PDF_PTS_PER_PAGE[0]
    scale_y = ih / PDF_PTS_PER_PAGE[1]

    COLORS = {
        "err":   ((184, 134, 11),  (255, 255, 0, 80),   (255, 249, 196)),
        "warn":  ((234, 138, 0),   (255, 165, 0, 50),   (255, 237, 200)),
        "pass":  ((22, 163, 74),   (22, 163, 74, 20),   (200, 247, 220)),
        "fixed": ((37, 99, 235),   (37, 99, 235, 30),   (200, 220, 253)),
    }

    try:
        font_sm = ImageFont.truetype("arial.ttf", 16)
        font_lg = ImageFont.truetype("arialbd.ttf", 18)
    except OSError:
        font_sm = ImageFont.load_default()
        font_lg = font_sm

    for ann in annotations:
        bbox = ann["bbox"]
        vtype = ann["vtype"]
        cid = ann["cid"]
        is_selected = (cid == selected_id)

        x0 = int(bbox[0] * scale_x) - 6
        y0 = int(bbox[1] * scale_y) - 6
        x1 = int(bbox[2] * scale_x) + 6
        y1 = int(bbox[3] * scale_y) + 6

        outline, fill, label_bg = COLORS.get(vtype, COLORS["err"])
        width = 4 if is_selected else 3

        draw.rectangle([x0, y0, x1, y1], fill=fill, outline=outline, width=width)

        if vtype == "err":
            label = f"#{cid} ❌ ERROR"
        elif vtype == "warn":
            label = f"#{cid} ⚠ WARNING"
        elif vtype == "fixed":
            label = f"#{cid} ✅ FIXED"
        else:
            label = f"#{cid}"

        lbl_w = len(label) * 10 + 16
        lbl_h = 24
        lbl_x = x0
        lbl_y = max(0, y0 - lbl_h - 3)

        if vtype == "err":
            draw.rectangle([lbl_x, lbl_y, lbl_x + lbl_w, lbl_y + lbl_h],
                           fill=(245, 158, 11, 240))
            draw.text((lbl_x + 6, lbl_y + 3), label, fill=(255, 255, 255), font=font_lg)
        elif vtype == "warn":
            draw.rectangle([lbl_x, lbl_y, lbl_x + lbl_w, lbl_y + lbl_h],
                           fill=(251, 146, 60, 220))
            draw.text((lbl_x + 6, lbl_y + 3), label, fill=(255, 255, 255), font=font_sm)
        else:
            draw.rectangle([lbl_x, lbl_y, lbl_x + lbl_w, lbl_y + lbl_h],
                           fill=(*label_bg, 230))
            draw.text((lbl_x + 6, lbl_y + 3), label, fill=outline, font=font_sm)

        if is_selected:
            for offset in range(1, 5):
                draw.rectangle(
                    [x0 - offset, y0 - offset, x1 + offset, y1 + offset],
                    outline=(59, 130, 246, 180 - offset * 30), width=2,
                )

    return Image.alpha_composite(img, overlay).convert("RGB")


# ═══════════════════════════════════════════════════════════════════
# ZOOMED CROP
# ═══════════════════════════════════════════════════════════════════

def _crop_error_region(img_path: Path, bbox: list, margin: int = 60) -> Image.Image | None:
    try:
        img = Image.open(img_path).convert("RGB")
    except Exception:
        return None

    iw, ih = img.size
    scale_x = iw / PDF_PTS_PER_PAGE[0]
    scale_y = ih / PDF_PTS_PER_PAGE[1]

    x0 = max(0, int(bbox[0] * scale_x) - margin)
    y0 = max(0, int(bbox[1] * scale_y) - margin)
    x1 = min(iw, int(bbox[2] * scale_x) + margin)
    y1 = min(ih, int(bbox[3] * scale_y) + margin)

    if (x1 - x0) < 100 or (y1 - y0) < 40:
        return None

    cropped = img.crop((x0, y0, x1, y1))
    draw = ImageDraw.Draw(cropped)
    inner_x0 = int(bbox[0] * scale_x) - x0 - 3
    inner_y0 = int(bbox[1] * scale_y) - y0 - 3
    inner_x1 = int(bbox[2] * scale_x) - x0 + 3
    inner_y1 = int(bbox[3] * scale_y) - y0 + 3
    draw.rectangle([inner_x0, inner_y0, inner_x1, inner_y1],
                   outline=(245, 158, 11), width=3)
    return cropped


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
        safe = safe.replace(needle, f"{tag}{needle}{end_tag}", 1)

    return safe


# ═══════════════════════════════════════════════════════════════════
# ERROR NAVIGATION HELPERS
# ═══════════════════════════════════════════════════════════════════

def _get_error_ids(checklist: list[dict], fixed_ids: set[int]) -> list[int]:
    ids = []
    for r in checklist:
        verdict = str(r.get("Verdict", ""))
        cid = int(r.get("#", 0))
        if cid in fixed_ids:
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


def _trunc(s, n=80):
    s = str(s) if s else ""
    return s if len(s) <= n else s[:n] + "…"


def _safe_int(val, default=0) -> int:
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _extract_edit_context(full_text: str, search_text: str,
                          context_chars: int = 200) -> str:
    if not search_text:
        return search_text
    pos = full_text.find(search_text)
    if pos == -1:
        return search_text

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
                         current_cid: int) -> list[int]:
    affected = []
    for row in checklist:
        cid = int(row.get("#", 0))
        if cid in fixed_ids:
            continue
        verdict = str(row.get("Verdict", ""))
        if not verdict.startswith("❌"):
            continue
        ct = str(row.get("Claude Text", "") or "").strip()
        if not ct:
            continue
        if ct in original_context:
            if ct not in new_text or cid == current_cid:
                affected.append(cid)
    if current_cid not in affected:
        affected.append(current_cid)
    return affected


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
        f"<h2>{acc:.1f}%</h2><p>📊 Content Accuracy</p></div>"
    )


def _render_accuracy_expander(accuracy_report: dict | None):
    """Render a collapsible section with word-level diff details."""
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
        f"📊 Content Accuracy Report — {acc:.1f}% "
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

        # Real differences
        if accuracy_report["real_diffs"]:
            st.markdown("#### ⚠️ Real Differences")
            for idx, d in enumerate(accuracy_report["real_diffs"], 1):
                dtype = d["type"]
                sub = d.get("sub_type", "")

                if dtype == "REPLACE":
                    if sub == "TRANSPOSED":
                        st.markdown(
                            f"**#{idx} 🔀 Transposed Words**\n\n"
                            f"- **Expected order:** {d['pymupdf_text']}\n"
                            f"- **Got order:** {d['converted_text']}"
                        )
                        moved = d.get("moved_words", [])
                        if moved:
                            _rows = "".join(
                                f"<tr><td style='padding:2px 8px;text-align:center;'>{m['position']}</td>"
                                f"<td style='padding:2px 8px;'><code>{m['expected']}</code></td>"
                                f"<td style='padding:2px 8px;'>→</td>"
                                f"<td style='padding:2px 8px;'><code>{m['got']}</code></td></tr>"
                                for m in moved
                            )
                            st.markdown(
                                f"<table style='font-size:0.85rem;margin:4px 0 8px 20px;'>"
                                f"<tr><th style='padding:2px 8px;'>Pos</th><th>Expected</th>"
                                f"<th></th><th>Got</th></tr>{_rows}</table>",
                                unsafe_allow_html=True,
                            )
                    elif sub == "PUNCTUATION":
                        st.markdown(
                            f"**#{idx} ✂️ Punctuation Difference**\n\n"
                            f"- **Expected:** `{d['pymupdf_text']}`\n"
                            f"- **Got:** `{d['converted_text']}`"
                        )
                        char_diffs = d.get("char_diffs", [])
                        if char_diffs:
                            for cd in char_diffs:
                                kind = cd["kind"]
                                ch = cd["char"]
                                ctx = cd.get("context", "")
                                if kind == "MISSING":
                                    st.markdown(f"  - ❌ **Missing** {ch}  `…{ctx}…`")
                                elif kind == "EXTRA":
                                    st.markdown(f"  - ➕ **Extra** {ch}  `…{ctx}…`")
                                elif kind == "CHANGED":
                                    st.markdown(f"  - 🔄 **Changed** {ch}  `…{ctx}…`")
                    else:
                        st.markdown(
                            f"**#{idx} REPLACE**\n\n"
                            f"- **PyMuPDF:** {d['pymupdf_text']}\n"
                            f"- **Claude:** {d['converted_text']}"
                        )

                    # Sentence context for all REPLACE sub-types
                    st.markdown(
                        f"> *PyMuPDF sentence:* {d['sentence_pymupdf']}\n\n"
                        f"> *Claude sentence:* {d['sentence_converted']}"
                    )

                elif dtype == "MISSING_IN_CONVERTED":
                    st.markdown(
                        f"**#{idx} MISSING in Claude output**\n\n"
                        f"- **Missing:** {d['missing_text']}\n\n"
                        f"> *Sentence:* {d['sentence']}"
                    )
                elif dtype == "EXTRA_IN_CONVERTED":
                    st.markdown(
                        f"**#{idx} EXTRA in Claude output**\n\n"
                        f"- **Extra:** {d['extra_text']}\n\n"
                        f"> *Sentence:* {d['sentence']}"
                    )
                if idx < len(accuracy_report["real_diffs"]):
                    st.divider()

        # Repositioned blocks (informational)
        if accuracy_report["repositioned"]:
            st.markdown("#### ℹ️ Repositioned Blocks")
            st.caption("Content exists in both files but at different positions — not real errors.")
            for idx, rp in enumerate(accuracy_report["repositioned"], 1):
                text = rp.get("pymupdf_text") or rp.get("converted_text", "")
                display = text if len(text) <= 150 else text[:147] + "..."
                st.text(f"  #{idx}: {display}")


# ═══════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT — call this from streamlit_app.py
# ═══════════════════════════════════════════════════════════════════

def render_inline_validation_review(
    excel_path: str | Path,
    converted_txt_path: str | Path,
    highlighted_dir: str | Path | None = None,
    json_report: dict | None = None,
    accuracy_report: dict | None = None,
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
    """
    excel_path = Path(excel_path)
    converted_txt_path = Path(converted_txt_path)
    highlighted_dir = Path(highlighted_dir) if highlighted_dir else None

    if not excel_path.exists():
        st.warning("⚠️ GPT comparison Excel not found. Validation review unavailable.")
        return

    # Inject CSS
    st.markdown(_CSS, unsafe_allow_html=True)

    # ── Load data ───────────────────────────────────────────────────
    checklist, findings, summary = _load_excel(excel_path)
    if not checklist:
        st.info("No checklist items found in the audit Excel.")
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
    n_ignored = sum(1 for r in checklist
                    if int(r.get("#", 0)) in ignored_ids
                    and str(r.get("Verdict", "")).startswith("⚠"))
    orig_err = summary.get("error", 0)
    orig_warn = summary.get("warn", 0)
    eff_err = max(0, orig_err - n_fixed)
    eff_warn = max(0, orig_warn - n_ignored)
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
    st.progress(eff_pass / total, text=f"Pass rate: {eff_pass/total:.0%}  ({eff_pass}/{total})")

    # ── Content Accuracy Report (collapsible) ─────────────────
    _render_accuracy_expander(accuracy_report)

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
                      and int(r.get("#", 0)) not in fixed_ids]
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

    # ── Build annotations ───────────────────────────────────────────
    page_anns = _build_page_annotations(checklist, json_rep, fixed_ids, ignored_ids)

    # ══════════════════════════════════════════════════════════════
    # ERROR NAVIGATION BAR
    # ══════════════════════════════════════════════════════════════
    error_ids = _get_error_ids(checklist, fixed_ids)
    err_pos, err_total = _get_error_position(error_ids, selected_id)

    if error_ids:
        nav_c1, nav_c2, nav_c3, nav_c4, nav_c5 = st.columns([2, 1, 3, 1, 2])
        with nav_c2:
            if st.button("◀ Prev Error", key="vri_prev_err", use_container_width=True):
                if err_pos > 1:
                    st.session_state.vri_selected_item = error_ids[err_pos - 2]
                else:
                    st.session_state.vri_selected_item = error_ids[-1]
                st.rerun()
        with nav_c3:
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
        with nav_c4:
            if st.button("Next Error ▶", key="vri_next_err", use_container_width=True):
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
    col_left, col_right = st.columns([1, 1], gap="medium")

    # ──────── LEFT: PDF Page with Yellow Highlights ─────────────
    with col_left:
        st.markdown("#### 📄 PDF Page — Yellow = Errors to Review")

        if total_pages > 0:
            cur = st.session_state.vri_current_page
            cur = max(1, min(cur, total_pages))

            nc1, nc2, nc3 = st.columns([1, 6, 1])
            with nc1:
                if st.button("◀", key="vri_pg_prev"):
                    st.session_state.vri_current_page = max(1, cur - 1)
                    st.rerun()
            with nc3:
                if st.button("▶", key="vri_pg_next"):
                    st.session_state.vri_current_page = min(total_pages, cur + 1)
                    st.rerun()
            with nc2:
                new_pg = st.slider(
                    "Page", 1, total_pages, cur,
                    key="vri_pg_slider", label_visibility="collapsed",
                )
                if new_pg != cur:
                    st.session_state.vri_current_page = new_pg

            cur = st.session_state.vri_current_page
            st.caption(f"📄 Page **{cur}** / {total_pages}")

            img_idx = cur - 1
            if 0 <= img_idx < len(page_images):
                anns = page_anns.get(cur, [])
                annotated = _annotate_page_image(page_images[img_idx], anns, selected_id)
                st.image(annotated, use_container_width=True)

            # Page summary
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
        st.markdown("#### 📝 Claude Converted Text")
        html = _render_text_html(text, checklist, selected_id, filter_mode, fixed_ids | ignored_ids)
        st.markdown(f'<div class="vr-text-viewer">{html}</div>', unsafe_allow_html=True)

        # Download converted text file
        if converted_txt_path.exists():
            try:
                _dl_bytes = converted_txt_path.read_bytes()
                _dl_name = converted_txt_path.name
                _dl_b64 = base64.b64encode(_dl_bytes).decode()
                _dl_size = len(_dl_bytes)
                _dl_label = f"{_dl_size / 1024:.1f} KB" if _dl_size < 1024 * 1024 else f"{_dl_size / (1024*1024):.1f} MB"
                st.markdown(
                    f'<a href="data:text/plain;base64,{_dl_b64}" download="{_dl_name}" '
                    f'style="display:inline-block; margin-top:8px; font-size:0.84rem; '
                    f'color:#1e40af; text-decoration:none; background:#eff6ff; '
                    f'padding:6px 14px; border-radius:6px; border:1px solid #bfdbfe;">'
                    f'📥 Download converted text ({_dl_label})</a>',
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

        st.markdown("**Expected text (from PDF):**")
        st.code(str(selected_row.get("PyMuPDF Text", "")), language=None)
        ctx = str(selected_row.get("Context", ""))
        if ctx:
            st.markdown("**Surrounding context:**")
            st.code(ctx, language=None)

        # ── Inline edit for errors ──────────────────────────────────
        orig_verdict = str(selected_row.get("Verdict", ""))
        is_error = orig_verdict.startswith("❌")
        is_fixed = cid in fixed_ids

        st.markdown("---")

        if is_fixed:
            st.success(f"✅ Item #{cid} has been fixed! The text file is updated.")
            undo_rec = _get_undo_record(cid)
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

        elif is_error:
            st.markdown(f"#### 🛠️ Apply Correction — Item #{cid}")

            claude_text = str(selected_row.get("Claude Text", "") or "")
            pymupdf_text = str(selected_row.get("PyMuPDF Text", ""))
            tag_type = str(selected_row.get("Tag Type", "addition"))
            suggested_fix = str(selected_row.get("Suggested Fix", ""))

            notes = str(selected_row.get("Notes", ""))
            if notes:
                st.info(f"📝 **Auditor Notes:** {notes}")
            if suggested_fix:
                st.warning(f"🔧 **Suggested Fix:** {suggested_fix}")

            tag_open = "[D>" if tag_type == "deletion" else "[A>"
            tag_close = "<D]" if tag_type == "deletion" else "<A]"

            if claude_text:
                original_context = _extract_edit_context(text, claude_text)
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
                original_context = ""
                prefill = f"{tag_open}{pymupdf_text}{tag_close}"
                st.caption("⚠️ This text is **missing** from the output. Edit below and use **Copy & Mark Fixed** to handle manually.")

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
                    else:
                        affected_preview = _find_affected_items(
                            checklist, fixed_ids,
                            original_context, new_text, cid,
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
                    if st.button("📋 Copy & Mark Fixed", key=f"vri_copy_{cid}"):
                        _mark_fixed(cid)
                        _save_undo_history([cid], "", "", str(converted_txt_path))
                        st.success("Marked as fixed!")
                        st.rerun()

            # Preview diff
            if claude_text and new_text != original_context:
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
            if fix:
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
                                     use_container_width=True,
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

            tbl += f'<tr class="{rc}">'
            tbl += f'<td><strong>{cid_int}</strong></td>'
            tbl += f'<td><span class="vr-badge {bc}">{_html_escape(verd)}</span></td>'
            tbl += f'<td>{row.get("Page", "")}</td>'
            tbl += f'<td>{_html_escape(str(row.get("Tag Type", "")))}</td>'
            tbl += f'<td>{_html_escape(_trunc(row.get("PyMuPDF Text", "")))}</td>'
            tbl += f'<td>{_html_escape(str(row.get("In Claude?", "")))}</td>'
            tbl += f'<td>{_html_escape(_trunc(row.get("Claude Text", "")))}</td>'
            tbl += f'<td>{_html_escape(str(row.get("Classification", "")))}</td>'
            tbl += f'<td>{_html_escape(_trunc(row.get("Notes", ""), 100))}</td>'
            tbl += "</tr>"

        tbl += "</tbody></table>"
        st.markdown(tbl, unsafe_allow_html=True)

        # Navigation buttons
        st.markdown("")
        nc = st.columns([2, 1, 1, 1, 2])

        id_opts = [int(r.get("#", 0)) for r in display_cl]
        if "vri_jump_to" in st.session_state and selected_id in id_opts:
            st.session_state["vri_jump_to"] = selected_id

        with nc[1]:
            if st.button("⬆️ Prev Item", key="vri_prev_item_btn"):
                if selected_id in id_opts:
                    ci = id_opts.index(selected_id)
                    st.session_state.vri_selected_item = id_opts[max(0, ci - 1)]
                elif id_opts:
                    st.session_state.vri_selected_item = id_opts[0]
                st.rerun()
        with nc[2]:
            current_idx = id_opts.index(selected_id) if selected_id in id_opts else 0
            jump = st.selectbox(
                "Go to #", id_opts,
                index=current_idx,
                key="vri_jump_to", label_visibility="collapsed",
            )
        with nc[3]:
            if st.button("⬇️ Next Item", key="vri_next_item_btn"):
                if selected_id in id_opts:
                    ci = id_opts.index(selected_id)
                    st.session_state.vri_selected_item = id_opts[min(len(id_opts) - 1, ci + 1)]
                elif id_opts:
                    st.session_state.vri_selected_item = id_opts[0]
                st.rerun()

        if jump != selected_id and jump in id_opts:
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
            use_container_width=True,
        )
    except Exception:
        pass

    # ── Footer ──────────────────────────────────────────────────────
    st.caption(
        f"📊 {excel_path.name}  •  "
        f"Items: {summary.get('total',0)}  •  "
        f"Fixed: {n_fixed}"
    )
