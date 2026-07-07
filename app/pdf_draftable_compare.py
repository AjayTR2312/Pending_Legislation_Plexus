"""
PDF ↔ Text Draftable-Style Comparison Viewer
==============================================
Inspired by: https://api.draftable.com/compare/JVVgphMMuJWj

Features:
- Side-by-side view: PDF (left) ↔ Converted Text (right)
- Highlighted text diff view (radio button toggle)
- Word-level diff highlighting (additions/deletions)
- Clickable filter badges (match/mod/new) with navigation
- Scroll sync between panels
- Prev/Next navigation between differences

Run:  streamlit run app/pdf_draftable_compare.py
"""

import streamlit as st
import fitz  # PyMuPDF
import difflib
import re
import json
import base64
from typing import List, Dict, Tuple

st.set_page_config(
    page_title="PDF Draftable Compare",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="collapsed",
)


@st.cache_data
def extract_pdf_text_per_page(pdf_bytes: bytes) -> List[str]:
    """Extract text from each PDF page separately."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages = []
    for page in doc:
        text = page.get_text("text")
        if len(text.strip()) < 10:
            try:
                tp = page.get_textpage_ocr(flags=0, full=True)
                text = page.get_text("text", textpage=tp)
            except Exception:
                pass
        pages.append(text)
    doc.close()
    return pages


@st.cache_data
def render_pdf_pages(pdf_bytes: bytes, dpi: int = 150) -> List[str]:
    """Render PDF pages as base64 PNG images."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    images = []
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    for page in doc:
        pix = page.get_pixmap(matrix=mat)
        images.append(base64.b64encode(pix.tobytes("png")).decode())
    doc.close()
    return images


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def compute_line_diff(pdf_lines: List[str], text_lines: List[str]) -> List[Dict]:
    """Compute line-level diff for the highlighted diff view."""
    sm = difflib.SequenceMatcher(None, pdf_lines, text_lines)
    result = []

    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == 'equal':
            for k in range(i1, i2):
                result.append({'type': 'equal', 'pdf': pdf_lines[k], 'text': text_lines[j1 + (k - i1)]})
        elif op == 'replace':
            max_len = max(i2 - i1, j2 - j1)
            for k in range(max_len):
                pdf_line = pdf_lines[i1 + k] if (i1 + k) < i2 else ''
                text_line = text_lines[j1 + k] if (j1 + k) < j2 else ''
                result.append({'type': 'replace', 'pdf': pdf_line, 'text': text_line})
        elif op == 'delete':
            for k in range(i1, i2):
                result.append({'type': 'delete', 'pdf': pdf_lines[k], 'text': ''})
        elif op == 'insert':
            for k in range(j1, j2):
                result.append({'type': 'insert', 'pdf': '', 'text': text_lines[k]})

    return result


def build_highlighted_diff_html(line_diffs: List[Dict]) -> str:
    """Build HTML for the highlighted inline diff view."""
    lines_html = []
    ln_left = 0
    ln_right = 0

    for entry in line_diffs:
        dtype = entry['type']
        pdf_line = entry.get('pdf', '')
        text_line = entry.get('text', '')

        if dtype == 'equal':
            ln_left += 1
            ln_right += 1
            esc = _esc(pdf_line)
            lines_html.append(
                f'<div class="diff-line diff-equal">'
                f'<span class="dl-num dl-left-num">{ln_left}</span>'
                f'<span class="dl-num dl-right-num">{ln_right}</span>'
                f'<span class="dl-content">{esc}</span></div>'
            )
        elif dtype == 'delete':
            ln_left += 1
            esc = _esc(pdf_line)
            lines_html.append(
                f'<div class="diff-line diff-delete">'
                f'<span class="dl-num dl-left-num">{ln_left}</span>'
                f'<span class="dl-num dl-right-num"></span>'
                f'<span class="dl-content"><del>{esc}</del></span></div>'
            )
        elif dtype == 'insert':
            ln_right += 1
            esc = _esc(text_line)
            lines_html.append(
                f'<div class="diff-line diff-insert">'
                f'<span class="dl-num dl-left-num"></span>'
                f'<span class="dl-num dl-right-num">{ln_right}</span>'
                f'<span class="dl-content"><ins>{esc}</ins></span></div>'
            )
        elif dtype == 'replace':
            ln_left += 1 if pdf_line else 0
            ln_right += 1 if text_line else 0
            word_html = _build_word_level_line_diff(pdf_line, text_line)
            left_num = ln_left if pdf_line else ''
            right_num = ln_right if text_line else ''
            lines_html.append(
                f'<div class="diff-line diff-replace">'
                f'<span class="dl-num dl-left-num">{left_num}</span>'
                f'<span class="dl-num dl-right-num">{right_num}</span>'
                f'<span class="dl-content">{word_html}</span></div>'
            )

    return "\n".join(lines_html)


def _build_word_level_line_diff(pdf_line: str, text_line: str) -> str:
    """Build word-level highlighted diff for a single replaced line."""
    pdf_words = pdf_line.split() if pdf_line else []
    text_words = text_line.split() if text_line else []

    if not pdf_words and not text_words:
        return ''

    sm = difflib.SequenceMatcher(None, pdf_words, text_words)
    parts = []

    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == 'equal':
            parts.append(_esc(' '.join(pdf_words[i1:i2])))
        elif op == 'delete':
            text = _esc(' '.join(pdf_words[i1:i2]))
            parts.append(f'<span class="wd-del">{text}</span>')
        elif op == 'insert':
            text = _esc(' '.join(text_words[j1:j2]))
            parts.append(f'<span class="wd-ins">{text}</span>')
        elif op == 'replace':
            del_text = _esc(' '.join(pdf_words[i1:i2]))
            ins_text = _esc(' '.join(text_words[j1:j2]))
            parts.append(f'<span class="wd-del">{del_text}</span>')
            parts.append(f'<span class="wd-ins">{ins_text}</span>')

    return ' '.join(parts)


def _best_line_similarity(text_line: str, page_lines: List[str]) -> float:
    """Find the best similarity ratio between text_line and any line in page_lines."""
    if not text_line or not page_lines:
        return 0.0
    text_lower = text_line.lower()
    best = 0.0
    for pline in page_lines:
        pline_lower = pline.lower()
        if text_lower in pline_lower or pline_lower in text_lower:
            ratio = max(len(text_lower), 1) / max(len(pline_lower), len(text_lower), 1)
            if ratio > 0.5:
                return max(0.9, best)
        ratio = difflib.SequenceMatcher(None, pline_lower, text_lower).ratio()
        if ratio > best:
            best = ratio
        if best >= 0.95:
            return best
    return best


def map_text_lines_to_pages(pdf_page_texts: List[str], text_lines: List[str]) -> Tuple[List[int], List[str]]:
    """Map each text line to its source PDF page and match status."""
    num_pages = len(pdf_page_texts)
    if num_pages == 0:
        return [0] * len(text_lines), ['added'] * len(text_lines)

    pdf_page_lines = []
    for page_text in pdf_page_texts:
        lines = [l.strip() for l in page_text.splitlines() if l.strip()]
        pdf_page_lines.append(lines)

    text_stripped = [re.sub(r'<[^>]+>', '', l).strip() for l in text_lines]

    text_page_assign = [0] * len(text_lines)
    current_page = 0

    for li, tline in enumerate(text_stripped):
        if not tline:
            text_page_assign[li] = current_page
            continue
        if current_page >= num_pages - 1:
            text_page_assign[li] = current_page
            continue

        current_score = _best_line_similarity(tline, pdf_page_lines[current_page])
        next_score = _best_line_similarity(tline, pdf_page_lines[current_page + 1])

        lookahead_current = 0
        lookahead_next = 0
        lookahead_count = 0
        for la in range(li + 1, min(li + 4, len(text_stripped))):
            if text_stripped[la]:
                lookahead_current += _best_line_similarity(text_stripped[la], pdf_page_lines[current_page])
                lookahead_next += _best_line_similarity(text_stripped[la], pdf_page_lines[current_page + 1])
                lookahead_count += 1

        if lookahead_count > 0:
            combined_current = current_score + lookahead_current / lookahead_count
            combined_next = next_score + lookahead_next / lookahead_count
        else:
            combined_current = current_score
            combined_next = next_score

        if combined_next > combined_current + 0.3:
            current_page += 1

        text_page_assign[li] = current_page

    # Match status
    match_status = ['added'] * len(text_lines)
    for li, tline in enumerate(text_stripped):
        if not tline:
            match_status[li] = 'match'
            continue
        page = text_page_assign[li]
        sim = _best_line_similarity(tline, pdf_page_lines[page])
        if sim >= 0.85:
            match_status[li] = 'match'
        elif sim >= 0.5:
            match_status[li] = 'modified'
        else:
            match_status[li] = 'added'

    return text_page_assign, match_status


def build_viewer_html(page_images: List[str], pdf_page_texts: List[str],
                      text_lines: List[str], pdf_name: str, text_name: str,
                      view_mode: str = "side_by_side") -> str:
    """Build the Draftable-style comparison viewer HTML."""

    num_pages = len(page_images)
    line_page_map, match_statuses = map_text_lines_to_pages(pdf_page_texts, text_lines)

    match_count = sum(1 for s in match_statuses if s == 'match')
    modified_count = sum(1 for s in match_statuses if s == 'modified')
    added_count = sum(1 for s in match_statuses if s == 'added')

    # Page first-line mapping for scroll sync
    page_first_line = {}
    for li, pi in enumerate(line_page_map):
        if pi not in page_first_line:
            page_first_line[pi] = li
    page_first_line_js = json.dumps(page_first_line)

    # Build highlighted diff view content
    pdf_full_lines = []
    for pt in pdf_page_texts:
        pdf_full_lines.extend([l for l in pt.splitlines()])

    line_diffs = compute_line_diff(pdf_full_lines, text_lines)
    diff_html_content = build_highlighted_diff_html(line_diffs)

    # Diff stats
    total_changes = sum(1 for d in line_diffs if d['type'] != 'equal')

    # Build right panel text lines
    right_lines = []
    prev_page = -1
    for li, line in enumerate(text_lines):
        page = line_page_map[li]
        status = match_statuses[li]
        if page != prev_page:
            right_lines.append(
                f'<div class="page-divider" data-page="{page}" id="textPageStart{page}">'
                f'Page {page + 1}</div>'
            )
            prev_page = page

        esc_line = _esc(line) if line.strip() else "&nbsp;"
        status_cls = f' line-{status}' if line.strip() else ''
        right_lines.append(
            f'<div class="line{status_cls}" data-li="{li}" data-page="{page}">'
            f'<span class="ln">{li + 1}</span>'
            f'<span class="lc">{esc_line}</span></div>'
        )

    right_block = "\n".join(right_lines)
    imgs_js = ",".join([f'"{img}"' for img in page_images])

    # Determine initial visibility based on view_mode
    side_by_side_display = "flex" if view_mode == "side_by_side" else "none"
    diff_display = "flex" if view_mode == "highlighted_diff" else "none"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<style>
:root {{
  --bg: #ffffff; --surface: #f8fafc; --border: #e2e8f0;
  --text: #1e293b; --text2: #64748b; --accent: #2563eb;
  --red: #dc2626; --red-bg: #fef2f2; --red-light: #fecaca;
  --green: #16a34a; --green-bg: #f0fdf4; --green-light: #bbf7d0;
  --orange: #ea580c; --orange-bg: #fff7ed;
  --blue-bg: #eff6ff;
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: var(--bg); color: var(--text); overflow: hidden; height: 100vh; }}

/* === TOOLBAR === */
.toolbar {{
  height: 48px; background: #0f172a; display: flex; align-items: center;
  padding: 0 16px; gap: 10px; color: #e2e8f0; border-bottom: 2px solid var(--accent);
}}
.toolbar .logo {{ font-size: 15px; font-weight: 700; color: #60a5fa; letter-spacing: -0.3px; }}
.toolbar .sep {{ width: 1px; height: 22px; background: #334155; }}
.toolbar .stat {{ font-size: 11px; color: #94a3b8; }}
.toolbar .stat b {{ color: #f1f5f9; }}
.toolbar .btn {{
  font-size: 11px; padding: 5px 12px; border-radius: 5px; border: 1px solid #475569;
  background: #1e293b; color: #e2e8f0; cursor: pointer; font-weight: 500;
  transition: all .15s;
}}
.toolbar .btn:hover {{ background: #334155; border-color: var(--accent); }}
.toolbar .btn.active {{ background: var(--accent); border-color: var(--accent); color: #fff; }}

/* Filter stat buttons */
.filter-btn {{
  font-size: 11px; padding: 4px 10px; border-radius: 12px; border: 1.5px solid transparent;
  background: #1e293b; color: #94a3b8; cursor: pointer; font-weight: 600;
  transition: all .15s; display: flex; align-items: center; gap: 5px;
  user-select: none;
}}
.filter-btn:hover {{ background: #334155; }}
.filter-btn.selected {{ border-color: currentColor; background: #334155; color: #f1f5f9; }}
.filter-btn.selected.flt-match {{ border-color: #4ade80; background: rgba(74,222,128,.12); }}
.filter-btn.selected.flt-modified {{ border-color: #fb923c; background: rgba(251,146,60,.12); }}
.filter-btn.selected.flt-added {{ border-color: #f87171; background: rgba(248,113,113,.12); }}
.filter-btn .dot {{ width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }}
.filter-btn .cnt {{ font-weight: 700; color: #f1f5f9; }}

/* Radio toggle group */
.view-toggle {{
  display: flex; border: 1px solid #475569; border-radius: 6px; overflow: hidden;
  margin-left: 12px;
}}
.view-toggle label {{
  font-size: 11px; padding: 5px 14px; cursor: pointer; color: #94a3b8;
  background: #1e293b; border-right: 1px solid #475569; font-weight: 500;
  transition: all .15s; display: flex; align-items: center; gap: 4px;
}}
.view-toggle label:last-child {{ border-right: none; }}
.view-toggle input {{ display: none; }}
.view-toggle label:has(input:checked) {{
  background: var(--accent); color: #fff;
}}

.toolbar .nav-group {{ margin-left: auto; display: flex; align-items: center; gap: 6px; }}
.toolbar .nav-label {{ font-size: 11px; color: #94a3b8; min-width: 100px; text-align: center; }}

/* === MAIN LAYOUT === */
.main {{ height: calc(100vh - 48px); position: relative; }}

/* Side-by-side view */
.side-by-side {{ display: {side_by_side_display}; height: 100%; }}

/* PDF Panel (left) */
.pdf-panel {{
  flex: 1; overflow-y: auto; background: #1e293b; position: relative;
  border-right: 2px solid #0f172a;
}}
.pdf-panel .panel-header {{
  position: sticky; top: 0; z-index: 20; padding: 8px 14px;
  background: rgba(15,23,42,.97); backdrop-filter: blur(6px);
  display: flex; align-items: center; gap: 8px; border-bottom: 1px solid #334155;
}}
.pdf-panel .panel-header .title {{ font-size: 12px; font-weight: 600; color: #e2e8f0; }}
.pdf-panel .panel-header .page-info {{ margin-left: auto; font-size: 11px; color: #94a3b8; }}
.pdf-panel .panel-header .zbtn {{
  width: 26px; height: 26px; border-radius: 4px; border: 1px solid #475569;
  background: #334155; color: #e2e8f0; cursor: pointer; font-size: 13px;
  display: flex; align-items: center; justify-content: center;
}}
.pdf-panel .panel-header .zbtn:hover {{ background: #475569; }}
.pdf-wrap {{ padding: 12px; }}
.page-ctr {{
  margin-bottom: 10px; background: #fff; border-radius: 4px;
  box-shadow: 0 2px 12px rgba(0,0,0,.4); overflow: hidden;
}}
.page-ctr img {{ width: 100%; display: block; }}
.page-lbl {{ text-align: center; font-size: 10px; color: #94a3b8; padding: 4px; background: #0f172a; }}

/* Text Panel (right) */
.text-panel {{
  flex: 1; overflow-y: auto; background: var(--bg); position: relative;
}}
.text-panel .panel-header {{
  position: sticky; top: 0; z-index: 20; padding: 8px 14px;
  background: rgba(248,250,252,.97); backdrop-filter: blur(6px);
  display: flex; align-items: center; gap: 8px; border-bottom: 1px solid var(--border);
}}
.text-panel .panel-header .title {{ font-size: 12px; font-weight: 600; color: var(--text); }}
.text-panel .panel-header .info {{ margin-left: auto; font-size: 11px; color: var(--text2); }}

/* Page dividers */
.page-divider {{
  background: #e2e8f0; color: var(--text2); font-size: 11px; font-weight: 700;
  padding: 4px 14px; border-top: 2px solid var(--accent); border-bottom: 1px solid var(--border);
}}

/* Text lines */
.line {{
  display: flex; padding: 0; border-left: 3px solid transparent;
  font-family: 'JetBrains Mono', 'Fira Code', Consolas, monospace;
  font-size: 12px; line-height: 1.75;
}}
.line:hover {{ background: #f8fafc; }}
.line .ln {{
  min-width: 44px; text-align: right; padding: 0 8px;
  color: var(--text2); opacity: .35; font-size: 10px;
  user-select: none; flex-shrink: 0; background: var(--surface);
  border-right: 1px solid var(--border);
}}
.line .lc {{ flex: 1; padding: 0 10px; white-space: pre-wrap; word-wrap: break-word; }}
.line.page-active {{ background: #fefce8; border-left-color: #eab308; }}
.line-match {{ border-left-color: #22c55e; }}
.line-match .lc {{ color: #166534; }}
.line-modified {{ background: var(--orange-bg); border-left-color: #f97316; }}
.line-modified .lc {{ color: #9a3412; }}
.line-added {{ background: var(--red-bg); border-left-color: #ef4444; }}
.line-added .lc {{ color: #991b1b; }}

/* Navigation highlight */
.line.nav-active, .diff-line.nav-active {{
  outline: 2px solid var(--accent); outline-offset: -2px;
  animation: navPulse 0.4s ease-out;
}}
.line.nav-active {{
  background: #dbeafe !important;
}}
@keyframes navPulse {{ 0% {{ opacity: 0.6; }} 100% {{ opacity: 1; }} }}

/* Dim non-filtered lines in filter mode */
.text-panel.filter-active .line:not(.filter-visible) {{
  opacity: 0.15; transition: opacity .15s;
}}
.text-panel.filter-active .line.filter-visible {{
  opacity: 1;
}}
.text-panel.filter-active .page-divider {{
  opacity: 0.3;
}}

/* === HIGHLIGHTED DIFF VIEW === */
.diff-view {{
  display: {diff_display}; flex-direction: column; height: 100%;
  overflow-y: auto; background: var(--bg);
}}
.diff-view .diff-header {{
  position: sticky; top: 0; z-index: 20; padding: 8px 16px;
  background: rgba(248,250,252,.97); backdrop-filter: blur(6px);
  border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 12px;
}}
.diff-view .diff-header .title {{ font-size: 13px; font-weight: 600; color: var(--text); }}
.diff-view .diff-header .badge {{
  font-size: 10px; padding: 2px 8px; border-radius: 10px; font-weight: 600;
}}
.diff-view .diff-header .badge-del {{ background: var(--red-light); color: var(--red); }}
.diff-view .diff-header .badge-ins {{ background: var(--green-light); color: var(--green); }}

.diff-content {{
  flex: 1; overflow-y: auto; padding: 0;
  font-family: 'JetBrains Mono', 'Fira Code', Consolas, monospace;
  font-size: 12px; line-height: 1.8;
}}

.diff-line {{
  display: flex; border-left: 3px solid transparent; min-height: 22px;
}}
.diff-line:hover {{ filter: brightness(0.97); }}

.diff-equal {{ background: var(--bg); }}
.diff-delete {{
  background: var(--red-bg); border-left-color: var(--red);
}}
.diff-insert {{
  background: var(--green-bg); border-left-color: var(--green);
}}
.diff-replace {{
  background: #fffbeb; border-left-color: var(--orange);
}}

.dl-num {{
  min-width: 40px; text-align: right; padding: 0 6px;
  color: var(--text2); opacity: .4; font-size: 10px;
  user-select: none; flex-shrink: 0; background: var(--surface);
  border-right: 1px solid var(--border);
}}
.dl-left-num {{ }}
.dl-right-num {{ border-right: 1px solid var(--border); }}

.dl-content {{
  flex: 1; padding: 0 12px; white-space: pre-wrap; word-wrap: break-word;
}}

.diff-delete .dl-content del {{
  text-decoration: none; background: var(--red-light); color: var(--red);
  border-radius: 2px; padding: 0 2px;
}}
.diff-insert .dl-content ins {{
  text-decoration: none; background: var(--green-light); color: var(--green);
  border-radius: 2px; padding: 0 2px;
}}

/* Word-level inline highlights */
.wd-del {{
  background: var(--red-light); color: var(--red); text-decoration: line-through;
  border-radius: 2px; padding: 0 2px; text-decoration-thickness: 1.5px;
}}
.wd-ins {{
  background: var(--green-light); color: var(--green); text-decoration: underline;
  border-radius: 2px; padding: 0 2px; text-decoration-thickness: 1.5px;
  text-underline-offset: 2px;
}}

/* Scrollbar */
::-webkit-scrollbar {{ width: 7px; }}
::-webkit-scrollbar-track {{ background: transparent; }}
::-webkit-scrollbar-thumb {{ background: #cbd5e1; border-radius: 4px; }}
::-webkit-scrollbar-thumb:hover {{ background: #94a3b8; }}
.pdf-panel::-webkit-scrollbar-thumb {{ background: #4b5563; }}
</style>
</head>
<body>

<div class="toolbar">
  <span class="logo">🔍 Draftable Compare</span>
  <span class="sep"></span>
  <span class="stat"><b>{num_pages}</b> pages</span>
  <span class="stat"><b>{len(text_lines)}</b> lines</span>
  <span class="sep"></span>

  <!-- CLICKABLE FILTER BUTTONS -->
  <button class="filter-btn flt-match" id="filterMatch" onclick="toggleFilter('match')" title="Click to filter & navigate matched lines">
    <span class="dot" style="background:#4ade80"></span><span class="cnt">{match_count}</span> match
  </button>
  <button class="filter-btn flt-modified" id="filterModified" onclick="toggleFilter('modified')" title="Click to filter & navigate modified lines">
    <span class="dot" style="background:#fb923c"></span><span class="cnt">{modified_count}</span> mod
  </button>
  <button class="filter-btn flt-added" id="filterAdded" onclick="toggleFilter('added')" title="Click to filter & navigate new/added lines">
    <span class="dot" style="background:#f87171"></span><span class="cnt">{added_count}</span> new
  </button>
  <span class="sep"></span>

  <!-- VIEW MODE RADIO BUTTONS -->
  <div class="view-toggle">
    <label><input type="radio" name="viewMode" value="sideBySide" {'checked' if view_mode == 'side_by_side' else ''} onchange="switchView('sideBySide')"><span>📄 Side-by-Side</span></label>
    <label><input type="radio" name="viewMode" value="highlightedDiff" {'checked' if view_mode == 'highlighted_diff' else ''} onchange="switchView('highlightedDiff')"><span>🖍 Highlighted Diff</span></label>
  </div>

  <span class="sep"></span>
  <label style="font-size:11px;color:#94a3b8;cursor:pointer;display:flex;align-items:center;gap:4px">
    <input type="checkbox" id="syncScroll" checked> Sync
  </label>

  <div class="nav-group">
    <button class="btn" onclick="navDiff(-1)" title="Previous difference (Alt+↑)">◀ Prev</button>
    <span class="nav-label" id="navLabel">0 / {total_changes} diffs</span>
    <button class="btn" onclick="navDiff(1)" title="Next difference (Alt+↓)">Next ▶</button>
  </div>
</div>

<div class="main">
  <!-- SIDE-BY-SIDE VIEW -->
  <div class="side-by-side" id="sideBySideView">
    <div class="pdf-panel" id="pdfPanel">
      <div class="panel-header">
        <span class="title">📄 {_esc(pdf_name)}</span>
        <button class="zbtn" onclick="zoomPdf(-1)">−</button>
        <button class="zbtn" onclick="zoomPdf(1)">+</button>
        <button class="zbtn" onclick="zoomPdf(0)" style="font-size:9px">Fit</button>
        <span class="page-info" id="pageInfo">Page 1 of {num_pages}</span>
      </div>
      <div class="pdf-wrap" id="pdfWrap"></div>
    </div>
    <div class="text-panel" id="textPanel">
      <div class="panel-header">
        <span class="title">📝 {_esc(text_name)}</span>
        <span class="info">Converted text with diff markers</span>
      </div>
      <div id="textWrap">{right_block}</div>
    </div>
  </div>

  <!-- HIGHLIGHTED DIFF VIEW -->
  <div class="diff-view" id="diffView">
    <div class="diff-header">
      <span class="title">Highlighted Text Differences</span>
      <span class="badge badge-del">− Deleted from PDF</span>
      <span class="badge badge-ins">+ Added in Text</span>
      <span style="margin-left:auto;font-size:11px;color:var(--text2)">
        {total_changes} difference(s) found
      </span>
    </div>
    <div class="diff-content" id="diffContent">
{diff_html_content}
    </div>
  </div>
</div>

<script>
// === Config ===
var totalPages = {num_pages};
var totalChanges = {total_changes};
var pageFirstLine = {page_first_line_js};
var currentDiffIdx = -1;

// === View switching ===
function switchView(mode) {{
  var sbs = document.getElementById('sideBySideView');
  var dv = document.getElementById('diffView');
  if (mode === 'sideBySide') {{
    sbs.style.display = 'flex';
    dv.style.display = 'none';
  }} else {{
    sbs.style.display = 'none';
    dv.style.display = 'flex';
  }}
  // Reset navigation when switching views
  currentDiffIdx = -1;
  document.querySelectorAll('.nav-active').forEach(function(el) {{ el.classList.remove('nav-active'); }});
  updateNavLabel();
}}

// === PDF pages ===
var pageImages = [{imgs_js}];
var pdfContainers = [];
var pdfWrap = document.getElementById('pdfWrap');
var pdfPanel = document.getElementById('pdfPanel');
var textPanel = document.getElementById('textPanel');

pageImages.forEach(function(b64, idx) {{
  var c = document.createElement('div'); c.className = 'page-ctr'; c.id = 'pdfPg' + idx;
  var lbl = document.createElement('div'); lbl.className = 'page-lbl'; lbl.textContent = 'Page ' + (idx+1);
  var img = document.createElement('img'); img.src = 'data:image/png;base64,' + b64;
  c.appendChild(lbl); c.appendChild(img); pdfWrap.appendChild(c); pdfContainers.push(c);
}});

// === Zoom ===
var zoomLevel = 0, zoomSteps = [50, 75, 100, 125, 150, 200];
function zoomPdf(dir) {{
  var imgs = document.querySelectorAll('.page-ctr img');
  if (dir === 0) {{ zoomLevel = 0; imgs.forEach(function(i){{ i.style.width='100%'; }}); }}
  else {{
    if (zoomLevel === 0) zoomLevel = 100;
    var idx = zoomSteps.indexOf(zoomLevel); if (idx === -1) idx = 2;
    idx = Math.max(0, Math.min(zoomSteps.length-1, idx + dir));
    zoomLevel = zoomSteps[idx];
    imgs.forEach(function(i){{ i.style.width = zoomLevel + '%'; }});
  }}
}}

// === Scroll sync ===
var PDF_HEADER_H = 42, TEXT_HEADER_H = 40;
var scrolling = false;

function getCurrentPdfPage() {{
  var best = 0;
  pdfContainers.forEach(function(c, i) {{
    if (c.offsetTop - PDF_HEADER_H <= pdfPanel.scrollTop) best = i;
  }});
  return best;
}}

function getPdfPageProgress(pageIdx) {{
  var ctr = pdfContainers[pageIdx];
  if (!ctr) return 0;
  return Math.max(0, Math.min(1, (pdfPanel.scrollTop + PDF_HEADER_H - ctr.offsetTop) / ctr.offsetHeight));
}}

function scrollTextToPage(pageIdx, progress) {{
  var divider = document.getElementById('textPageStart' + pageIdx);
  if (!divider) return;
  var nextDivider = document.getElementById('textPageStart' + (pageIdx + 1));
  var sectionTop = divider.offsetTop;
  var sectionBottom = nextDivider ? nextDivider.offsetTop : textPanel.scrollHeight;
  textPanel.scrollTop = sectionTop - TEXT_HEADER_H + ((sectionBottom - sectionTop) * progress);
}}

pdfPanel.addEventListener('scroll', function() {{
  var page = getCurrentPdfPage();
  document.getElementById('pageInfo').textContent = 'Page ' + (page+1) + ' of ' + totalPages;
  if (document.getElementById('syncScroll').checked && !scrolling) {{
    scrolling = true;
    scrollTextToPage(page, getPdfPageProgress(page));
    setTimeout(function() {{ scrolling = false; }}, 80);
  }}
}});

textPanel.addEventListener('scroll', function() {{
  if (document.getElementById('syncScroll').checked && !scrolling) {{
    scrolling = true;
    var dividers = document.querySelectorAll('.page-divider');
    var best = 0;
    dividers.forEach(function(d) {{
      if (d.offsetTop - TEXT_HEADER_H <= textPanel.scrollTop) best = parseInt(d.dataset.page);
    }});
    var ctr = pdfContainers[best];
    if (ctr) pdfPanel.scrollTop = ctr.offsetTop - PDF_HEADER_H;
    document.getElementById('pageInfo').textContent = 'Page ' + (best+1) + ' of ' + totalPages;
    setTimeout(function() {{ scrolling = false; }}, 80);
  }}
}});

// === Filter state ===
var activeFilter = null; // null, 'match', 'modified', 'added'
var filterMap = {{
  'match': {{ btn: 'filterMatch', lineClass: 'line-match', diffClass: 'diff-equal' }},
  'modified': {{ btn: 'filterModified', lineClass: 'line-modified', diffClass: 'diff-replace' }},
  'added': {{ btn: 'filterAdded', lineClass: 'line-added', diffClass: 'diff-insert' }}
}};

function toggleFilter(type) {{
  currentDiffIdx = -1;
  // Clear highlights
  document.querySelectorAll('.nav-active').forEach(function(el) {{ el.classList.remove('nav-active'); }});
  document.querySelectorAll('.filter-visible').forEach(function(el) {{ el.classList.remove('filter-visible'); }});
  var tp = document.getElementById('textPanel');

  // Deselect all buttons
  document.querySelectorAll('.filter-btn').forEach(function(b) {{ b.classList.remove('selected'); }});

  if (activeFilter === type) {{
    // Toggle off
    activeFilter = null;
    tp.classList.remove('filter-active');
    updateNavLabel();
    return;
  }}

  // Activate filter
  activeFilter = type;
  var info = filterMap[type];
  document.getElementById(info.btn).classList.add('selected');

  // Mark visible lines in side-by-side
  var lines = document.querySelectorAll('.' + info.lineClass);
  lines.forEach(function(l) {{ l.classList.add('filter-visible'); }});
  tp.classList.add('filter-active');

  updateNavLabel();

  // Auto-navigate to first match
  if (lines.length > 0) navDiff(1);
}}

function updateNavLabel() {{
  var diffs = getDiffLines();
  var label = activeFilter ? activeFilter : 'diffs';
  if (currentDiffIdx < 0) {{
    document.getElementById('navLabel').textContent = '0 / ' + diffs.length + ' ' + label;
  }} else {{
    document.getElementById('navLabel').textContent = (currentDiffIdx + 1) + ' / ' + diffs.length + ' ' + label;
  }}
}}

// === Diff navigation ===
function getDiffLines() {{
  var sbs = document.getElementById('sideBySideView');
  if (activeFilter) {{
    var info = filterMap[activeFilter];
    if (sbs.style.display !== 'none') {{
      return document.querySelectorAll('.' + info.lineClass);
    }} else {{
      return document.querySelectorAll('.' + info.diffClass);
    }}
  }}
  if (sbs.style.display !== 'none') {{
    return document.querySelectorAll('.line-modified, .line-added');
  }} else {{
    return document.querySelectorAll('.diff-delete, .diff-insert, .diff-replace');
  }}
}}

function navDiff(dir) {{
  var diffs = getDiffLines();
  if (!diffs.length) {{
    document.getElementById('navLabel').textContent = 'No matches';
    return;
  }}
  // Clear previous highlight
  document.querySelectorAll('.nav-active').forEach(function(el) {{ el.classList.remove('nav-active'); }});

  currentDiffIdx += dir;
  if (currentDiffIdx < 0) currentDiffIdx = diffs.length - 1;
  if (currentDiffIdx >= diffs.length) currentDiffIdx = 0;

  var target = diffs[currentDiffIdx];
  target.classList.add('nav-active');
  target.scrollIntoView({{ behavior: 'smooth', block: 'center' }});

  var label = activeFilter ? activeFilter : 'diffs';
  document.getElementById('navLabel').textContent = (currentDiffIdx + 1) + ' / ' + diffs.length + ' ' + label;
}}

// === Keyboard shortcuts ===
document.addEventListener('keydown', function(e) {{
  if (e.altKey && e.key === 'ArrowDown') {{ e.preventDefault(); navDiff(1); }}
  if (e.altKey && e.key === 'ArrowUp') {{ e.preventDefault(); navDiff(-1); }}
}});

// Initial nav label
document.getElementById('navLabel').textContent = '0 / ' + totalChanges + ' diffs';
</script>
</body>
</html>"""
    return html


# ── Streamlit UI ─────────────────────────────────────────────────────────────

st.markdown("""
<style>
    .block-container { padding-top: 0.5rem; }
    [data-testid="stSidebar"] { display: none; }
    iframe { border: none !important; }
    .stRadio > div { flex-direction: row; gap: 12px; }
</style>
""", unsafe_allow_html=True)

st.markdown("""
<div style="text-align:center;padding:4px 0;font-size:15px;font-weight:700;color:#1e293b;">
    🔍 PDF Draftable-Style Comparison
</div>
<div style="text-align:center;font-size:11px;color:#64748b;margin-bottom:8px;">
    Reference: <a href="https://api.draftable.com/compare/JVVgphMMuJWj" target="_blank">Draftable Compare</a>
</div>
""", unsafe_allow_html=True)

col1, col2 = st.columns(2)
with col1:
    pdf_file = st.file_uploader("📄 Upload PDF (original)", type=["pdf"], key="pdf")
with col2:
    text_file = st.file_uploader("📝 Upload Text File (converted)", type=["txt"], key="txt")

if pdf_file and text_file:
    # View mode radio button
    view_mode = st.radio(
        "View Mode",
        options=["Side-by-Side", "Highlighted Diff"],
        index=0,
        horizontal=True,
        help="Switch between side-by-side comparison and highlighted text diff view"
    )

    mode_key = "side_by_side" if view_mode == "Side-by-Side" else "highlighted_diff"

    pdf_bytes = pdf_file.read()
    text_content = text_file.read().decode("utf-8", errors="replace")
    text_lines = text_content.splitlines()

    with st.spinner("Rendering PDF & computing differences..."):
        page_images = render_pdf_pages(pdf_bytes, dpi=150)
        pdf_page_texts = extract_pdf_text_per_page(pdf_bytes)

    viewer_html = build_viewer_html(
        page_images, pdf_page_texts, text_lines,
        pdf_file.name, text_file.name, view_mode=mode_key
    )
    st.components.v1.html(viewer_html, height=850, scrolling=False)

else:
    st.info("👆 Upload a PDF and its converted text file to compare.")
    st.markdown("""
    ### Features
    - **Side-by-Side** — PDF pages (left) with converted text (right), scroll-synced
    - **Highlighted Diff** — Unified diff view with word-level highlighting
        - 🟥 Red = deleted from PDF original
        - 🟩 Green = added in converted text
        - 🟧 Orange = modified (word-level changes shown inline)
    - **Clickable Filters** — Click `171 match` / `1 mod` / `5 new` to filter & navigate only that type
    - **Navigation** — ◀ ▶ buttons or Alt+↑/↓ to jump between differences
    - **Scroll Sync** — PDF and text panels scroll together
    - **Zoom** — +/−/Fit controls for the PDF panel
    
    *Inspired by [Draftable](https://api.draftable.com/compare/JVVgphMMuJWj)*
    """)
