"""
PDF ↔ Text Diff Viewer — Draftable-style (Accurate)
=====================================================
- Left panel: PDF rendered as pages
- Right panel: Text file shown EXACTLY as-is (never altered)
- Content-based sync: each text line mapped to its source PDF page
- Scroll PDF page N → text scrolls to matching content

Run:  streamlit run pdf_text_diff.py
"""

import streamlit as st
import fitz  # PyMuPDF
import difflib
import re
import json
import base64
from typing import List, Dict


st.set_page_config(
    page_title="PDF ↔ Text Diff Viewer",
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


def _normalize(text: str) -> str:
    """Normalize text for fuzzy matching."""
    return re.sub(r'[^a-z0-9]', '', text.lower())


def map_text_lines_to_pages(pdf_page_texts: List[str], text_lines: List[str]) -> tuple:
    """Map each text file line to its most likely source PDF page and match status.

    Two-pass approach:
    1. Page splitting: for each text line, find the best-matching PDF page using
       a sliding window of normalized content similarity.
    2. Match status: compare each text line against the best-matching line from
       its assigned page using SequenceMatcher ratio for similarity scoring.

    Returns:
        (page_assignments, match_statuses) where:
        - page_assignments: list of page indices (int) for each text line
        - match_statuses: list of 'match'|'modified'|'added' for each text line
    """
    num_pages = len(pdf_page_texts)
    if num_pages == 0:
        return [0] * len(text_lines), ['added'] * len(text_lines)

    # Strip tags for matching purposes
    tag_re = re.compile(r'<[^>]+>')

    # Build normalized content per PDF page (join all lines, collapse whitespace)
    pdf_page_norm = []
    pdf_page_lines = []  # list of stripped lines per page for similarity check
    for page_text in pdf_page_texts:
        lines = [l.strip() for l in page_text.splitlines() if l.strip()]
        pdf_page_lines.append(lines)
        # Normalized: lowercase, no extra whitespace
        norm = ' '.join(lines).lower()
        pdf_page_norm.append(norm)

    # Normalize text lines (strip tags, strip whitespace)
    text_stripped = [tag_re.sub('', l).strip() for l in text_lines]

    # --- PASS 1: Page splitting using sliding content window ---
    # Strategy: accumulate text lines into chunks and compare against PDF pages
    # sequentially. When accumulated content similarity shifts to the next page,
    # we split.

    text_page_assign = [0] * len(text_lines)
    current_page = 0

    # Build cumulative text content per candidate page boundary
    # Use a greedy approach: for each text line, check if it fits better
    # on the current page or the next page
    for li, tline in enumerate(text_stripped):
        if not tline:
            # Blank lines stay on current page
            text_page_assign[li] = current_page
            continue

        if current_page >= num_pages - 1:
            # Last page — everything goes here
            text_page_assign[li] = current_page
            continue

        # Check similarity of this line against current page vs next page
        current_score = _best_line_similarity(tline, pdf_page_lines[current_page])
        next_score = _best_line_similarity(tline, pdf_page_lines[current_page + 1])

        # Look ahead: check next few non-blank lines too for context
        lookahead_current = 0
        lookahead_next = 0
        lookahead_count = 0
        for la in range(li + 1, min(li + 4, len(text_stripped))):
            if text_stripped[la]:
                lookahead_current += _best_line_similarity(text_stripped[la], pdf_page_lines[current_page])
                lookahead_next += _best_line_similarity(text_stripped[la], pdf_page_lines[current_page + 1])
                lookahead_count += 1

        # Combined score with lookahead
        if lookahead_count > 0:
            combined_current = current_score + lookahead_current / lookahead_count
            combined_next = next_score + lookahead_next / lookahead_count
        else:
            combined_current = current_score
            combined_next = next_score

        # Switch to next page if next page clearly fits better
        if combined_next > combined_current + 0.3:
            current_page += 1
            # Check if we should skip pages (for cases where text jumps multiple pages)
            while current_page < num_pages - 1:
                skip_score = _best_line_similarity(tline, pdf_page_lines[current_page + 1])
                if skip_score > _best_line_similarity(tline, pdf_page_lines[current_page]) + 0.4:
                    current_page += 1
                else:
                    break

        text_page_assign[li] = current_page

    # --- PASS 2: Match status using similarity scoring ---
    match_status = ['added'] * len(text_lines)

    for li, tline in enumerate(text_stripped):
        if not tline:
            match_status[li] = 'match'  # blank lines are fine
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


def _best_line_similarity(text_line: str, page_lines: List[str]) -> float:
    """Find the best similarity ratio between text_line and any line in page_lines."""
    if not text_line or not page_lines:
        return 0.0

    text_lower = text_line.lower()
    best = 0.0
    for pline in page_lines:
        pline_lower = pline.lower()
        # Quick check: if one is substring of the other, high score
        if text_lower in pline_lower or pline_lower in text_lower:
            ratio = max(len(text_lower), 1) / max(len(pline_lower), len(text_lower), 1)
            if ratio > 0.5:
                return max(0.9, best)

        # Use SequenceMatcher for similarity
        ratio = difflib.SequenceMatcher(None, pline_lower, text_lower).ratio()
        if ratio > best:
            best = ratio
        # Early exit if perfect match
        if best >= 0.95:
            return best

    return best


def build_viewer_html(page_images: List[str], pdf_page_texts: List[str],
                      text_lines: List[str], pdf_name: str, text_name: str) -> str:
    """Build the viewer HTML. Text file is shown exactly as-is."""

    num_pages = len(page_images)

    # Map text lines to PDF pages and get match status
    line_page_map, match_statuses = map_text_lines_to_pages(pdf_page_texts, text_lines)

    # Count matches for stats
    match_count = sum(1 for s in match_statuses if s == 'match')
    modified_count = sum(1 for s in match_statuses if s == 'modified')
    added_count = sum(1 for s in match_statuses if s == 'added')

    # Map text lines to PDF pages and get match status
    line_page_map, match_statuses = map_text_lines_to_pages(pdf_page_texts, text_lines)

    # Count matches for stats
    match_count = sum(1 for s in match_statuses if s == 'match')
    modified_count = sum(1 for s in match_statuses if s == 'modified')
    added_count = sum(1 for s in match_statuses if s == 'added')

    # Find first text line index for each PDF page (for scroll sync)
    page_first_line = {}
    for li, pi in enumerate(line_page_map):
        if pi not in page_first_line:
            page_first_line[pi] = li

    page_first_line_js = json.dumps(page_first_line)

    # Build right panel: text file lines exactly as-is, with page markers
    right_lines = []
    prev_page = -1
    for li, line in enumerate(text_lines):
        page = line_page_map[li]
        status = match_statuses[li]
        # Insert page divider when page changes
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

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<style>
:root {{
  --bg: #fff; --surface: #f5f6f8; --border: #dde1e6;
  --text: #1a1a2e; --text2: #6c757d; --accent: #2563eb;
  --red: #dc2626; --red-bg: #fef2f2;
  --green: #16a34a; --green-bg: #f0fdf4;
  --blue-bg: #eff6ff; --orange: #ea580c;
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: var(--bg); color: var(--text); overflow: hidden; height: 100vh; }}

/* TOOLBAR */
.toolbar {{
  height: 42px; background: #1e293b; display: flex; align-items: center;
  padding: 0 12px; gap: 8px; color: #e2e8f0;
}}
.toolbar .logo {{ font-size: 14px; font-weight: 700; color: #60a5fa; }}
.toolbar .sep {{ width: 1px; height: 20px; background: #475569; }}
.toolbar .btn {{
  font-size: 11px; padding: 4px 10px; border-radius: 4px; border: 1px solid #475569;
  background: #334155; color: #e2e8f0; cursor: pointer; font-weight: 500;
}}
.toolbar .btn:hover {{ background: #475569; }}
.toolbar .stat {{ font-size: 11px; color: #94a3b8; }}
.toolbar .stat b {{ color: #f1f5f9; }}
.toolbar .nav-group {{ margin-left: auto; display: flex; align-items: center; gap: 6px; }}
.toolbar .nav-label {{ font-size: 11px; color: #94a3b8; min-width: 70px; text-align: center; }}

/* MAIN */
.main {{ display: flex; height: calc(100vh - 42px); }}

/* LEFT: PDF */
.pdf-panel {{
  flex: 1; overflow-y: auto; background: #374151; position: relative;
  border-right: 2px solid #1e293b;
}}
.pdf-panel .panel-header {{
  position: sticky; top: 0; z-index: 20; padding: 6px 12px;
  background: rgba(30,41,59,.97); backdrop-filter: blur(6px);
  display: flex; align-items: center; gap: 8px; border-bottom: 1px solid #475569;
}}
.pdf-panel .panel-header .title {{ font-size: 12px; font-weight: 600; color: #e2e8f0; }}
.pdf-panel .panel-header .page-info {{ margin-left: auto; font-size: 11px; color: #94a3b8; }}
.pdf-panel .panel-header .zbtn {{
  width: 24px; height: 24px; border-radius: 3px; border: 1px solid #475569;
  background: #334155; color: #e2e8f0; cursor: pointer; font-size: 13px;
  display: flex; align-items: center; justify-content: center;
}}
.pdf-panel .panel-header .zbtn:hover {{ background: #475569; }}
.pdf-wrap {{ padding: 12px; }}
.page-ctr {{
  margin-bottom: 10px; background: #fff; border-radius: 3px;
  box-shadow: 0 2px 8px rgba(0,0,0,.3); overflow: hidden;
}}
.page-ctr img {{ width: 100%; display: block; }}
.page-lbl {{ text-align: center; font-size: 10px; color: #94a3b8; padding: 3px; background: #1e293b; }}

/* RIGHT: Text (exact copy of uploaded file) */
.text-panel {{
  flex: 1; overflow-y: auto; background: var(--bg); position: relative;
}}
.text-panel .panel-header {{
  position: sticky; top: 0; z-index: 20; padding: 6px 12px;
  background: rgba(245,246,248,.97); backdrop-filter: blur(6px);
  display: flex; align-items: center; gap: 8px; border-bottom: 1px solid var(--border);
}}
.text-panel .panel-header .title {{ font-size: 12px; font-weight: 600; color: var(--text); }}
.text-panel .panel-header .info {{ margin-left: auto; font-size: 11px; color: var(--text2); }}

/* Page dividers in text panel */
.page-divider {{
  background: #e2e8f0; color: var(--text2); font-size: 11px; font-weight: 700;
  padding: 4px 14px; border-top: 2px solid var(--accent); border-bottom: 1px solid var(--border);
  letter-spacing: 0.5px;
}}

/* Text lines — shown exactly as in the file */
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

/* Highlight active page's lines */
.line.page-active {{ background: #fefce8; border-left-color: #eab308; }}

/* Match highlighting */
.line-match {{ background: #f0fdf4; border-left-color: #22c55e; }}
.line-match .lc {{ color: #166534; }}
.line-modified {{ background: #fff7ed; border-left-color: #f97316; }}
.line-modified .lc {{ color: #9a3412; }}
.line-added {{ background: #fef2f2; border-left-color: #ef4444; }}
.line-added .lc {{ color: #991b1b; }}

/* When highlighting is off */
.no-match-hl .line-match,
.no-match-hl .line-modified,
.no-match-hl .line-added {{
  background: transparent; border-left-color: transparent;
}}
.no-match-hl .line-match .lc,
.no-match-hl .line-modified .lc,
.no-match-hl .line-added .lc {{
  color: var(--text);
}}

/* Scrollbar */
::-webkit-scrollbar {{ width: 7px; }}
::-webkit-scrollbar-track {{ background: transparent; }}
::-webkit-scrollbar-thumb {{ background: #c1c8ce; border-radius: 4px; }}
.pdf-panel::-webkit-scrollbar-thumb {{ background: #4b5563; }}
</style>
</head>
<body>

<div class="toolbar">
  <span class="logo">🔍 PDF ↔ Text Compare</span>
  <span class="sep"></span>
  <span class="stat"><b>{num_pages}</b> pages</span>
  <span class="stat"><b>{len(text_lines)}</b> text lines</span>
  <span class="sep"></span>
  <span class="stat" style="color:#4ade80">●</span><span class="stat"><b>{match_count}</b> match</span>
  <span class="stat" style="color:#fb923c">●</span><span class="stat"><b>{modified_count}</b> modified</span>
  <span class="stat" style="color:#f87171">●</span><span class="stat"><b>{added_count}</b> added</span>
  <span class="sep"></span>
  <label style="font-size:11px;color:#94a3b8;cursor:pointer;display:flex;align-items:center;gap:4px">
    <input type="checkbox" id="syncScroll" checked> Scroll Sync
  </label>
  <label style="font-size:11px;color:#94a3b8;cursor:pointer;display:flex;align-items:center;gap:4px;margin-left:8px">
    <input type="checkbox" id="hlPage" checked onchange="togglePageHL(this.checked)"> Highlight Page
  </label>
  <label style="font-size:11px;color:#94a3b8;cursor:pointer;display:flex;align-items:center;gap:4px;margin-left:8px">
    <input type="checkbox" id="hlMatch" checked onchange="toggleMatchHL(this.checked)"> Match Colors
  </label>
  <div class="nav-group">
    <button class="btn" onclick="goToPage(-1)">◀ Prev Page</button>
    <span class="nav-label" id="navLabel">Page 1 / {num_pages}</span>
    <button class="btn" onclick="goToPage(1)">Next Page ▶</button>
  </div>
</div>

<div class="main">
  <!-- LEFT: PDF rendered pages -->
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

  <!-- RIGHT: Text file exactly as-is -->
  <div class="text-panel" id="textPanel">
    <div class="panel-header">
      <span class="title">📝 {_esc(text_name)}</span>
      <span class="info">Exact file content (unmodified)</span>
    </div>
    <div id="textWrap">
{right_block}
    </div>
  </div>
</div>

<script>
// === Config ===
var totalPages = {num_pages};
var pageFirstLine = {page_first_line_js};  // page_idx -> first text line index

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

// === Panel header heights (sticky headers offset) ===
var PDF_HEADER_H = 38;  // pdf panel sticky header
var TEXT_HEADER_H = 35; // text panel sticky header

// === Detect current PDF page ===
function getCurrentPdfPage() {{
  var best = 0;
  var scrollRef = pdfPanel.scrollTop + PDF_HEADER_H;
  pdfContainers.forEach(function(c, i) {{
    if (c.offsetTop <= scrollRef) best = i;
  }});
  return best;
}}

// === How far through the current PDF page (0-1) ===
function getPdfPageProgress(pageIdx) {{
  var ctr = pdfContainers[pageIdx];
  if (!ctr) return 0;
  var pageTop = ctr.offsetTop;
  var pageH = ctr.offsetHeight;
  if (pageH === 0) return 0;
  return Math.max(0, Math.min(1, (pdfPanel.scrollTop + PDF_HEADER_H - pageTop) / pageH));
}}

// === Scroll text panel to align with a specific page + progress ===
function scrollTextToPage(pageIdx, progress) {{
  var divider = document.getElementById('textPageStart' + pageIdx);
  if (!divider) return;

  var nextDivider = document.getElementById('textPageStart' + (pageIdx + 1));
  var sectionTop = divider.offsetTop;
  var sectionBottom = nextDivider ? nextDivider.offsetTop : (textPanel.scrollHeight);
  var sectionH = sectionBottom - sectionTop;

  // Align: when progress=0, page divider sits right below the sticky header
  textPanel.scrollTop = sectionTop - TEXT_HEADER_H + (sectionH * progress);
}}

// === Scroll PDF to align with a specific page + progress ===
function scrollPdfToPage(pageIdx, progress) {{
  var ctr = pdfContainers[pageIdx];
  if (!ctr) return;
  // Align: when progress=0, page top sits right below the sticky header
  pdfPanel.scrollTop = ctr.offsetTop - PDF_HEADER_H + (ctr.offsetHeight * progress);
}}

// === Detect which page the text panel is showing ===
function getCurrentTextPage() {{
  var dividers = document.querySelectorAll('.page-divider');
  var best = 0;
  var scrollRef = textPanel.scrollTop + TEXT_HEADER_H;
  dividers.forEach(function(d) {{
    var pi = parseInt(d.dataset.page);
    if (d.offsetTop <= scrollRef) best = pi;
  }});
  return best;
}}

function getTextPageProgress(pageIdx) {{
  var divider = document.getElementById('textPageStart' + pageIdx);
  if (!divider) return 0;
  var nextDivider = document.getElementById('textPageStart' + (pageIdx + 1));
  var sectionTop = divider.offsetTop;
  var sectionBottom = nextDivider ? nextDivider.offsetTop : (textPanel.scrollHeight);
  var sectionH = sectionBottom - sectionTop;
  if (sectionH === 0) return 0;
  return Math.max(0, Math.min(1, (textPanel.scrollTop + TEXT_HEADER_H - sectionTop) / sectionH));
}}

// === Highlight text lines for current page ===
var lastHlPage = -1;
function highlightPageLines(pageIdx) {{
  if (pageIdx === lastHlPage) return;
  lastHlPage = pageIdx;
  document.querySelectorAll('.line.page-active').forEach(function(el) {{ el.classList.remove('page-active'); }});
  if (!document.getElementById('hlPage').checked) return;
  document.querySelectorAll('.line[data-page="' + pageIdx + '"]').forEach(function(el) {{
    el.classList.add('page-active');
  }});
}}

function togglePageHL(on) {{
  if (!on) {{
    document.querySelectorAll('.line.page-active').forEach(function(el) {{ el.classList.remove('page-active'); }});
    lastHlPage = -1;
  }} else {{
    highlightPageLines(getCurrentPdfPage());
  }}
}}

function toggleMatchHL(on) {{
  var textWrap = document.getElementById('textWrap');
  if (!on) {{
    textWrap.classList.add('no-match-hl');
  }} else {{
    textWrap.classList.remove('no-match-hl');
  }}
}}

// === Scroll sync ===
var scrolling = false;

pdfPanel.addEventListener('scroll', function() {{
  var page = getCurrentPdfPage();
  document.getElementById('pageInfo').textContent = 'Page ' + (page+1) + ' of ' + totalPages;
  document.getElementById('navLabel').textContent = 'Page ' + (page+1) + ' / ' + totalPages;
  highlightPageLines(page);

  if (document.getElementById('syncScroll').checked && !scrolling) {{
    scrolling = true;
    var progress = getPdfPageProgress(page);
    scrollTextToPage(page, progress);
    setTimeout(function() {{ scrolling = false; }}, 80);
  }}
}});

textPanel.addEventListener('scroll', function() {{
  if (document.getElementById('syncScroll').checked && !scrolling) {{
    scrolling = true;
    var page = getCurrentTextPage();
    var progress = getTextPageProgress(page);
    scrollPdfToPage(page, progress);
    document.getElementById('pageInfo').textContent = 'Page ' + (page+1) + ' of ' + totalPages;
    document.getElementById('navLabel').textContent = 'Page ' + (page+1) + ' / ' + totalPages;
    highlightPageLines(page);
    setTimeout(function() {{ scrolling = false; }}, 80);
  }}
}});

// === Page navigation ===
var currentPage = 0;
function goToPage(dir) {{
  currentPage = getCurrentPdfPage() + dir;
  if (currentPage < 0) currentPage = 0;
  if (currentPage >= totalPages) currentPage = totalPages - 1;
  // Align page top with the panel's visible area (below sticky header)
  pdfPanel.scrollTop = pdfContainers[currentPage].offsetTop - PDF_HEADER_H;
}}

// === Keyboard ===
document.addEventListener('keydown', function(e) {{
  if (e.altKey && e.key === 'ArrowDown') {{ e.preventDefault(); goToPage(1); }}
  if (e.altKey && e.key === 'ArrowUp') {{ e.preventDefault(); goToPage(-1); }}
}});

// Initial highlight
highlightPageLines(0);
</script>
</body>
</html>"""
    return html


# ── Streamlit UI ─────────────────────────────────────────────────────────────

st.markdown("""
<style>
    .block-container { padding-top: 1rem; }
    [data-testid="stSidebar"] { display: none; }
    iframe { border: none !important; }
</style>
""", unsafe_allow_html=True)

st.title("🔍 PDF ↔ Text Diff Viewer")
st.caption("PDF on left, exact text file on right — page-synced scrolling")

col1, col2 = st.columns(2)
with col1:
    pdf_file = st.file_uploader("📄 Upload PDF (original)", type=["pdf"], key="pdf")
with col2:
    text_file = st.file_uploader("📝 Upload Text File (converted)", type=["txt"], key="txt")

if pdf_file and text_file:
    pdf_bytes = pdf_file.read()
    text_content = text_file.read().decode("utf-8", errors="replace")
    text_lines = text_content.splitlines()

    with st.spinner("Rendering PDF & mapping content..."):
        page_images = render_pdf_pages(pdf_bytes, dpi=150)
        pdf_page_texts = extract_pdf_text_per_page(pdf_bytes)

    viewer_html = build_viewer_html(
        page_images, pdf_page_texts, text_lines,
        pdf_file.name, text_file.name
    )
    st.components.v1.html(viewer_html, height=800, scrolling=False)

else:
    st.info("👆 Upload a PDF and its converted text file to compare.")
    st.markdown("""
    ### Features:
    - **Left panel** — PDF pages rendered as images
    - **Right panel** — Text file shown exactly as uploaded (never altered)
    - **Page-synced scrolling** — PDF page N ↔ text content from page N
    - **Page highlighting** — current page's text lines highlighted in yellow
    - **Navigation** — Prev/Next page buttons + Alt+↑/↓
    - **Zoom** — +/−/Fit controls for PDF
    """)
