"""
PDF ↔ PyMuPDF Clean Text — Side-by-Side Comparison Viewer (v2)
================================================================
Light theme · word-level bidirectional highlighting · inline edit

Run:  streamlit run pdf_compare_viewer.py
"""

import streamlit as st
import fitz  # PyMuPDF
import base64
import json
import re
import os
import difflib
from typing import List, Dict, Tuple

# Only set page config when running as standalone (not when imported)
if __name__ == "__main__":
    st.set_page_config(
        page_title="PDF ↔ Text Comparator",
        page_icon="📄",
        layout="wide",
        initial_sidebar_state="collapsed",
    )

PDF_RENDER_DPI = 150


# ── Caching helpers ──────────────────────────────────────────────────────────

@st.cache_data
def render_pdf_pages(pdf_bytes: bytes, dpi: int = PDF_RENDER_DPI) -> List[str]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    images = []
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    for page in doc:
        pix = page.get_pixmap(matrix=mat)
        images.append(base64.b64encode(pix.tobytes("png")).decode())
    doc.close()
    return images


@st.cache_data
def extract_word_bboxes(pdf_bytes: bytes) -> List[dict]:
    """Extract words from PDF pages; uses OCR for scanned/image-only pages."""
    import os
    import shutil
    # Set up Tesseract path for OCR fallback (Linux / Windows)
    _tess_bin = shutil.which("tesseract")
    if _tess_bin:
        _tess_dir = os.path.dirname(_tess_bin)
        _tessdata = os.path.join(os.path.dirname(_tess_dir), "share", "tessdata")
        if not os.path.isdir(_tessdata):
            _tessdata = os.path.join(_tess_dir, "tessdata")
        if os.path.isdir(_tessdata):
            os.environ.setdefault("TESSDATA_PREFIX", _tessdata)
    else:
        # Windows fallback
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))
        import settings as _settings  # TESSERACT_DIR in .env
        _tess_dir = _settings.tesseract_dir()
        _tessdata = os.path.join(_tess_dir, "tessdata") if _tess_dir else ""
        if _tessdata and os.path.isdir(_tessdata):
            os.environ.setdefault("TESSDATA_PREFIX", _tessdata)

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    words_all = []
    ocr_pages = []
    for pi in range(len(doc)):
        page = doc[pi]
        native_words = page.get_text("words")
        if len(native_words) >= 5:
            # Enough native text — use it directly
            for w in native_words:
                words_all.append(dict(
                    text=w[4], page=pi,
                    x0=w[0], y0=w[1], x1=w[2], y1=w[3],
                    block=w[5], line=w[6], word=w[7],
                ))
        else:
            # Few or no words — likely scanned; try OCR
            try:
                tp = page.get_textpage_ocr(flags=0, full=True)
                ocr_words = page.get_text("words", textpage=tp)
                for w in ocr_words:
                    words_all.append(dict(
                        text=w[4], page=pi,
                        x0=w[0], y0=w[1], x1=w[2], y1=w[3],
                        block=w[5], line=w[6], word=w[7],
                    ))
                if ocr_words:
                    ocr_pages.append(pi + 1)
            except Exception:
                # OCR unavailable — fall back to whatever native text exists
                for w in native_words:
                    words_all.append(dict(
                        text=w[4], page=pi,
                        x0=w[0], y0=w[1], x1=w[2], y1=w[3],
                        block=w[5], line=w[6], word=w[7],
                    ))
    doc.close()
    if ocr_pages:
        st.info(f"🔍 OCR applied on page(s): {', '.join(map(str, ocr_pages))}")
    return words_all


def _strip_tags(t: str) -> str:
    return t.replace("[A>", "").replace("<A]", "").replace("[D>", "").replace("<D]", "")


# ── Build word-level mapping ────────────────────────────────────────────────

def build_word_level_map(
    clean_lines: List[str], word_infos: List[dict]
) -> Dict[str, dict]:
    """Returns dict keyed by 'lineIdx_wordIdx' -> {page, x0, y0, x1, y1}."""
    pdf_words = [(re.sub(r"[^\w]", "", w["text"].lower()), w) for w in word_infos]
    pdf_texts = [pw[0] for pw in pdf_words]

    mapping = {}
    cursor = 0

    for li, raw in enumerate(clean_lines):
        stripped = _strip_tags(raw).strip()
        if not stripped:
            continue
        raw_tokens = stripped.split()
        tokens = [re.sub(r"[^\w]", "", rt.lower()) for rt in raw_tokens]
        tokens = [t for t in tokens if t]
        if not tokens:
            continue

        # Search outward from cursor in expanding windows so the
        # closest match is always preferred.  This prevents common words
        # from producing false hits on distant pages.
        best_pos, best_n = -1, 0
        found = False
        for window, threshold in [(400, 0.7), (2000, 0.6), (6000, 0.5)]:
            for delta in range(window):
                for start in ([cursor + delta] if delta == 0
                              else [cursor + delta, cursor - delta]):
                    if start < 0 or start >= len(pdf_texts):
                        continue
                    n, pi = 0, start
                    for tok in tokens:
                        for off in range(min(8, len(pdf_texts) - pi)):
                            if pi + off < len(pdf_texts) and pdf_texts[pi + off] == tok:
                                n += 1
                                pi = pi + off + 1
                                break
                    if n > best_n:
                        best_n, best_pos = n, start
                    if best_n >= len(tokens) * threshold:
                        found = True
                        break
                if found:
                    break
            if found or (best_pos >= 0 and best_n >= max(1, len(tokens) * 0.5)):
                break

        if best_pos >= 0 and best_n >= max(1, len(tokens) * 0.3):
            orig_words = raw.split(' ')
            vis_idx = 0
            pi = best_pos
            for ow_idx, ow in enumerate(orig_words):
                clean_w = _strip_tags(ow).strip()
                norm_w = re.sub(r"[^\w]", "", clean_w.lower())
                if not norm_w:
                    continue
                if vis_idx < len(tokens):
                    for off in range(min(8, len(pdf_texts) - pi)):
                        if pi + off < len(pdf_texts) and pdf_texts[pi + off] == norm_w:
                            pw = pdf_words[pi + off][1]
                            mapping["{}_{}".format(li, ow_idx)] = {
                                "page": pw["page"],
                                "x0": pw["x0"], "y0": pw["y0"],
                                "x1": pw["x1"], "y1": pw["y1"],
                            }
                            pi = pi + off + 1
                            break
                    vis_idx += 1
            cursor = pi  # advance past matched PDF words
        # If no match at all, don't move cursor — avoids permanent drift

    return mapping


# ── Word-level deviation diff ────────────────────────────────────────────────

def compute_diff_map(
    clean_lines: List[str],
    pdf_word_infos: List[dict],
) -> Tuple[Dict[str, str], Dict[str, int], int]:
    """
    Compare the converted text words against the PDF-extracted word sequence
    using difflib.SequenceMatcher at word level.

    Returns:
        dev_map   : {'li_wi': 'match'|'insert'|'replace'}  — status for each
                    word span in the converted text (keyed the same way as wordMap)
        miss_map  : {'li_wi': count}  — nearest wordMap key to where PDF words
                    were deleted (for drawing red overlays on the PDF)
        n_missing : total count of PDF words not found in converted text
    """
    # ── Build PDF word sequence (normalised) ────────────────────────────────
    pdf_seq: List[str] = []
    for w in pdf_word_infos:
        norm = re.sub(r"[^\w]", "", w["text"].lower())
        if norm:
            pdf_seq.append(norm)

    # ── Build converted-text word sequence + position mapping ───────────────
    # We split each tagged line by single space (same as the viewer's rendering)
    # so that wi indices align with the 'tw' spans in the HTML.
    conv_seq: List[str] = []
    pos_map: List[Tuple[int, int]] = []   # index in conv_seq -> (li, wi)

    for li, line in enumerate(clean_lines):
        raw_words = line.split(" ")
        for wi, rw in enumerate(raw_words):
            clean_w = _strip_tags(rw).strip()
            norm = re.sub(r"[^\w]", "", clean_w.lower())
            if norm:
                conv_seq.append(norm)
                pos_map.append((li, wi))

    # ── SequenceMatcher ──────────────────────────────────────────────────────
    matcher = difflib.SequenceMatcher(None, pdf_seq, conv_seq, autojunk=False)

    dev_map: Dict[str, str] = {}
    # For PDF-delete ops we need to map them to the nearest converted position
    # so we can draw an overlay on the PDF image.
    miss_map: Dict[str, int] = {}
    n_missing = 0
    last_conv_idx = 0   # tracks nearest preceding converted word

    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            for ci in range(j1, j2):
                li, wi = pos_map[ci]
                dev_map[f"{li}_{wi}"] = "match"
            last_conv_idx = j2 - 1 if j2 > j1 else last_conv_idx

        elif op == "insert":
            for ci in range(j1, j2):
                li, wi = pos_map[ci]
                dev_map[f"{li}_{wi}"] = "insert"
            last_conv_idx = j2 - 1 if j2 > j1 else last_conv_idx

        elif op == "replace":
            for ci in range(j1, j2):
                li, wi = pos_map[ci]
                dev_map[f"{li}_{wi}"] = "replace"
            last_conv_idx = j2 - 1 if j2 > j1 else last_conv_idx

        elif op == "delete":
            # These PDF words have no counterpart in converted text.
            n_missing += i2 - i1
            # Anchor to nearest preceding converted word for PDF overlay.
            anchor_ci = min(last_conv_idx, len(pos_map) - 1)
            if anchor_ci >= 0 and anchor_ci < len(pos_map):
                li, wi = pos_map[anchor_ci]
                key = f"{li}_{wi}"
                miss_map[key] = miss_map.get(key, 0) + (i2 - i1)

    return dev_map, miss_map, n_missing


# ── HTML template ────────────────────────────────────────────────────────────

_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<style>
:root {
  --bg:       #ffffff;
  --surface:  #f6f8fa;
  --border:   #d1d9e0;
  --text:     #1f2328;
  --text2:    #656d76;
  --accent:   #0969da;
  --green:    #1a7f37;
  --green-bg: #dafbe1;
  --red:      #cf222e;
  --red-bg:   #ffebe9;
  --hl:       rgba(255,212,59,.38);
  --hl-border:#d4a72c;
  --hover:    #f3f4f6;
}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;
     background:var(--bg);color:var(--text);overflow:hidden}

.top-bar{
  height:44px;background:var(--surface);border-bottom:1px solid var(--border);
  display:flex;align-items:center;padding:0 16px;gap:10px;
}
.top-bar .logo{font-size:15px;font-weight:700;color:var(--accent)}
.top-bar .sep{width:1px;height:20px;background:var(--border)}
.top-bar .hint{font-size:12px;color:var(--text2)}
.top-bar .stat{margin-left:auto;font-size:11px;color:var(--text2);
               background:var(--bg);padding:3px 10px;border-radius:10px;border:1px solid var(--border)}
.top-bar .btn{margin-left:8px;font-size:12px;padding:4px 12px;border-radius:6px;
              border:1px solid var(--border);background:var(--bg);color:var(--text);cursor:pointer}
.top-bar .btn:hover{background:var(--surface);border-color:var(--accent)}
.top-bar .btn.on{background:var(--accent);color:#fff;border-color:var(--accent)}

.panels{display:flex;height:calc(100vh - 44px)}
.panel{flex:1;overflow-y:auto;position:relative}
.panel-pdf{background:#fafafa;border-right:1px solid var(--border)}
.panel-text{background:var(--bg)}

.ph{
  position:sticky;top:0;z-index:10;padding:8px 14px;font-size:13px;font-weight:600;
  display:flex;align-items:center;gap:8px;
  background:rgba(246,248,250,.95);backdrop-filter:blur(8px);
  border-bottom:1px solid var(--border);color:var(--text);
}

.pdf-wrap{padding:10px}
.page-ctr{position:relative;margin-bottom:12px;background:#fff;border-radius:4px;
          border:1px solid var(--border);overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.06);
          content-visibility:auto;contain-intrinsic-size:0 1100px}
.page-ctr img{width:100%;display:block}
.page-lbl{text-align:center;font-size:10px;color:var(--text2);padding:4px 0;
           background:var(--surface);border-bottom:1px solid var(--border)}

.hl-overlay{
  position:absolute;background:var(--hl);
  border:2px solid var(--hl-border);border-radius:2px;
  pointer-events:none;z-index:5;animation:hlP .5s ease-out;
}
@keyframes hlP{0%{opacity:0}100%{opacity:1}}

.cz{position:absolute;cursor:pointer;z-index:4;border:1px solid transparent;transition:all .12s}
.cz:hover{border-color:rgba(9,105,218,.3);background:rgba(9,105,218,.06)}

.text-wrap{padding:0;min-height:calc(100vh - 90px);background:var(--bg)}
.tl{
  padding:0 16px 0 0;
  font-family:'Cascadia Code','JetBrains Mono','Fira Code',Consolas,monospace;
  font-size:13px;line-height:1.75;
  border-left:3px solid transparent;
  white-space:pre-wrap;word-wrap:break-word;display:flex;
  transition:background .08s;cursor:text;
  content-visibility:auto;contain-intrinsic-size:auto 29px;
}
.tl:hover{background:var(--hover)}
.tl.active{background:var(--hl)!important;border-left-color:var(--hl-border)}
.tl.editor-active{background:rgba(9,105,218,.04)!important;border-left-color:var(--accent)!important}
.tl.editor-active .ln{opacity:1;color:var(--text)}

.ln{
  display:inline-block;min-width:48px;text-align:right;padding:0 12px 0 8px;
  color:var(--text2);opacity:.45;font-size:12px;user-select:none;flex-shrink:0;
  background:var(--surface);border-right:1px solid var(--border);margin-right:10px;
}
.lc{display:inline;flex:1;min-width:0;outline:none} /* line content wrapper */
.lc[contenteditable="true"]{caret-color:var(--accent)}
.lc[contenteditable="true"]:focus{outline:none}
.lc .tg-a + .tw .tg-a,
.lc .tg-d + .tw .tg-d {margin-left:-1px}

.tw{border-radius:2px;padding:0 1px}
.tw.sel{background:var(--hl);outline:1px solid var(--hl-border)}
::selection{background:rgba(59,130,246,.3)}

.tg-a{color:var(--green);background:var(--green-bg);border-radius:2px;padding:0 1px;
      text-decoration:underline;text-decoration-color:var(--green);text-underline-offset:2px;
      text-decoration-thickness:1.5px;text-decoration-skip-ink:none}
.tg-d{color:var(--red);background:var(--red-bg);text-decoration:line-through;border-radius:2px;padding:0 1px;
      text-decoration-color:var(--red);text-decoration-thickness:1.5px}
.tag-marker{}

.text-wrap.editing .tl{cursor:text}
.text-wrap.editing .ln{pointer-events:none}

/* Focus on changes — fade unchanged lines */
.text-wrap.focus .tl:not(.has-change){opacity:.3}
.text-wrap.focus .tl.has-change{opacity:1}

/* Collapse block */
.collapse-bar{
  padding:4px 12px;font-size:12px;color:var(--text2);background:var(--surface);
  border:1px dashed var(--border);border-radius:4px;margin:2px 12px 2px 46px;
  cursor:pointer;text-align:center;user-select:none;transition:background .15s;
}
.collapse-bar:hover{background:#e8ecf0}

/* PDF controls */
.pdf-controls{
  display:flex;align-items:center;gap:6px;padding:4px 8px;
  background:var(--surface);border-bottom:1px solid var(--border);
  position:sticky;top:0;z-index:12;
}
.pdf-controls .zbtn{
  font-size:14px;width:28px;height:28px;border-radius:4px;border:1px solid var(--border);
  background:var(--bg);cursor:pointer;display:flex;align-items:center;justify-content:center;
}
.pdf-controls .zbtn:hover{background:var(--surface);border-color:var(--accent)}
.pdf-controls .zlbl{font-size:11px;color:var(--text2);min-width:40px;text-align:center}
.pdf-controls .page-nav{margin-left:auto;display:flex;align-items:center;gap:4px}
.pdf-controls .page-nav .plbl{font-size:11px;color:var(--text2)}

/* Semantic diff badges */
.sem-badge{
  display:inline-block;font-size:9px;font-weight:600;padding:1px 5px;border-radius:3px;
  margin:0 2px;vertical-align:middle;letter-spacing:.3px;font-family:monospace;
}
.sem-badge.ins{background:#c6f0c6;color:#0d5d0d;border:1px solid #82d882}
.sem-badge.del{background:#f8d0d0;color:#8b1a1a;border:1px solid #e08080}

/* Text editor toolbar */
.text-toolbar{
  display:flex;align-items:center;gap:6px;padding:4px 10px;
  background:var(--surface);border-bottom:1px solid var(--border);flex-wrap:wrap;
  position:sticky;top:33px;z-index:9;
}
.text-toolbar .tbtn{
  font-size:11px;padding:3px 10px;border-radius:4px;
  border:1px solid var(--border);background:var(--bg);cursor:pointer;
  display:flex;align-items:center;gap:3px;font-weight:600;
  transition:all .15s;white-space:nowrap;
}
.text-toolbar .tbtn:hover{border-color:var(--accent);background:var(--surface)}
.text-toolbar .tbtn.tag-a{color:var(--green);border-color:rgba(26,127,55,.3)}
.text-toolbar .tbtn.tag-a:hover{background:var(--green-bg);border-color:var(--green)}
.text-toolbar .tbtn.tag-d{color:var(--red);border-color:rgba(207,34,46,.3)}
.text-toolbar .tbtn.tag-d:hover{background:var(--red-bg);border-color:var(--red)}
.text-toolbar .tsep{width:1px;height:18px;background:var(--border)}
.text-toolbar .tlbl{font-size:10px;color:var(--text2);font-weight:500}

::-webkit-scrollbar{width:7px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:#c1c8ce;border-radius:4px}
::-webkit-scrollbar-thumb:hover{background:#9ca3ab}

/* ── Deviation highlighting ─────────────────────────── */
/* Word in converted text that is NOT in the PDF source (inserted) */
.tw.dev-ins{background:rgba(255,163,0,.30);border-bottom:2px solid #cc7a00;border-radius:2px}
/* Word in converted text that differs from the PDF word (substituted) */
.tw.dev-sub{background:rgba(220,53,69,.18);border-bottom:2px dotted #dc3545;border-radius:2px}
/* Annotation badge on dev-ins / dev-sub */
.tw.dev-ins::after,.tw.dev-sub::after{
  font-size:7px;vertical-align:super;margin-left:1px;font-family:monospace;font-weight:700;
  padding:0 2px;border-radius:2px;
}
.tw.dev-ins::after{content:'INS';color:#7c4e00;background:#ffe0a0}
.tw.dev-sub::after{content:'CHG';color:#7a1a22;background:#f8d7da}
/* Suppress badges when annotation mode is off (default) */
.tw.dev-no-badge.dev-ins::after,.tw.dev-no-badge.dev-sub::after{display:none}
/* Sticky deviation summary strip below toolbar */
.dev-strip{
  display:none;padding:5px 14px;font-size:12px;font-weight:500;
  background:#fff8e6;border-bottom:1px solid #ffc107;color:#664d00;
  flex-wrap:wrap;gap:8px;align-items:center;
}
.dev-strip.visible{display:flex}
.dev-pill{display:inline-flex;align-items:center;gap:4px;
          padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600}
.dev-pill.ins{background:#ffe0a0;color:#7c4e00;border:1px solid #cc7a00}
.dev-pill.sub{background:#f8d7da;color:#7a1a22;border:1px solid #dc3545}
.dev-pill.del{background:#e2e3e5;color:#383d41;border:1px solid #adb5bd}
.dev-pill.ok{background:#d4edda;color:#155724;border:1px solid #28a745}
/* Deviation nav arrows */
.top-bar .btn.dev-on{background:#e67e00;color:#fff;border-color:#e67e00}
.top-bar .btn.dev-on:hover{background:#cc6d00}
/* PDF panel: missing-word overlays */
.hl-missing{
  position:absolute;background:rgba(220,53,69,.15);
  border:2px solid rgba(220,53,69,.5);border-radius:2px;
  pointer-events:none;z-index:5;
}
</style>
</head>
<body>

<div class="top-bar">
  <span class="logo">📄 PDF ↔ Text Comparator</span>
  <span class="sep"></span>
  <button class="btn" onclick="navDiff(-1)" title="Previous diff (markup tags)">◀</button>
  <button class="btn" onclick="navDiff(1)" title="Next diff (markup tags)">▶</button>
  <label style="font-size:12px;color:var(--text2);display:flex;align-items:center;gap:4px;margin-left:8px;cursor:pointer">
    <input type="checkbox" id="focusCb" onchange="toggleFocus(this.checked)"> Focus on changes
  </label>
  <label style="font-size:12px;color:var(--text2);display:flex;align-items:center;gap:4px;margin-left:4px;cursor:pointer">
    <input type="checkbox" id="collapseCb" onchange="toggleCollapse(this.checked)"> Collapse unchanged
  </label>
  <span class="sep"></span>
  <button class="btn" id="devBtn" onclick="toggleDevMode()" title="Highlight deviations between PDF source and converted text">⚡ Deviations</button>
  <label style="font-size:12px;color:var(--text2);display:flex;align-items:center;gap:4px;cursor:pointer" id="badgeLbl" title="Show INS/CHG labels on each word">
    <input type="checkbox" id="badgeCb" onchange="toggleBadges(this.checked)" checked> Labels
  </label>
  <span class="stat" id="statBar">Ready</span>
  <button class="btn" id="dlBtn" onclick="downloadText()">⬇️ Download</button>
</div>
<!-- Deviation summary strip (shown when dev mode is on) -->
<div class="dev-strip" id="devStrip">
  <strong>Deviations vs. PDF source:</strong>
  <span class="dev-pill ins" id="devIns">— inserted</span>
  <span class="dev-pill sub" id="devSub">— changed</span>
  <span class="dev-pill del" id="devDel">— PDF words missing</span>
  <span class="dev-pill ok" id="devOk">— matched</span>
  <span style="margin-left:auto;font-size:11px;color:#664d00" id="devAccuracy"></span>
  <button class="btn" style="font-size:11px;padding:2px 8px" onclick="navDevDiff(-1)">◀ prev</button>
  <button class="btn" style="font-size:11px;padding:2px 8px" onclick="navDevDiff(1)">next ▶</button>
</div>

<div class="panels">
  <div class="panel panel-pdf" id="pdfPanel">
    <div class="pdf-controls">
      <button class="zbtn" onclick="zoomPdf(-1)">−</button>
      <span class="zlbl" id="zoomLbl">Fit</span>
      <button class="zbtn" onclick="zoomPdf(1)">+</button>
      <button class="zbtn" onclick="zoomPdf(0)" title="Fit to width">Fit</button>
      <div class="page-nav">
        <button class="zbtn" onclick="navPage(-1)">◀</button>
        <span class="plbl" id="pageLbl">1 / --</span>
        <button class="zbtn" onclick="navPage(1)">▶</button>
      </div>
    </div>
    <div class="pdf-wrap" id="pdfWrap"></div>
  </div>
  <div class="panel panel-text" id="textPanel">
    <div class="ph"><span>CONVERTED TEXT</span></div>
    <div class="text-toolbar">
      <button class="tbtn tag-a" onclick="wrapTag('a')" title="Wrap selected text with [A>...<A] tag">[A&gt;</button>
      <button class="tbtn tag-d" onclick="wrapTag('d')" title="Wrap selected text with [D>...<D] tag">[D&gt;</button>
      <span class="tsep"></span>
      <button class="tbtn" onclick="splitAtCursor()" title="Split line at cursor position">✂ Split</button>
      <button class="tbtn" onclick="mergeWithPrevious()" title="Merge current line with the line above">⊕ Merge ↑</button>
    </div>
    <div class="text-wrap" id="textWrap">__TEXT_BLOCK__</div>
  </div>
</div>

<script>
var pageImages=[__IMGS_JS__];
var wordMap=__WORDMAP_JS__;
var S=__SCALE__;
var originalLines=__LINES_JS__;
var changeLines=__CHANGELINES_JS__;
var diffsCount=0;

// Deviation data: {li_wi: 'match'|'insert'|'replace'}, n_pdf_missing
var devMap=__DEVMAP_JS__;
var devMissMap=__DEVMISSMAP_JS__;   // {li_wi (nearest to miss): count}
var nPdfMissing=__N_PDF_MISSING__;
var devActive=false;
var devDiffLines=[];  // line indices that have deviations
var devDiffIdx=-1;
var showBadges=true;

// Build PDF pages — first 3 eager, rest lazy via IntersectionObserver
var wrap=document.getElementById('pdfWrap'),containers=[];
pageImages.forEach(function(b64,idx){
  var c=document.createElement('div');c.className='page-ctr';c.id='pg_'+idx;
  var l=document.createElement('div');l.className='page-lbl';l.textContent='Page '+(idx+1);
  var i=document.createElement('img');i.id='img_'+idx;i.style.width='100%';
  if(idx<3){
    i.src='data:image/png;base64,'+b64;
    (function(pi){i.addEventListener('load',function(){buildZonesForPage(pi);},{once:true});})(idx);
  } else {
    i.dataset.pgidx=String(idx);
    i.style.minHeight='1050px';  // placeholder keeps scrollbar accurate
  }
  c.appendChild(l);c.appendChild(i);wrap.appendChild(c);containers.push(c);
});
// Load images lazily as they approach the viewport
var _lazyObs=new IntersectionObserver(function(entries){
  entries.forEach(function(e){
    if(!e.isIntersecting)return;
    var img=e.target,pgIdx=parseInt(img.dataset.pgidx);
    img.src='data:image/png;base64,'+pageImages[pgIdx];
    img.style.minHeight='';img.removeAttribute('data-pgidx');
    (function(pi){img.addEventListener('load',function(){buildZonesForPage(pi);},{once:true});})(pgIdx);
    _lazyObs.unobserve(img);
  });
},{root:document.getElementById('pdfPanel'),rootMargin:'600px'});
document.querySelectorAll('img[data-pgidx]').forEach(function(img){_lazyObs.observe(img);});

// Recompute and mark changed lines
function refreshChangeMeta(){
  // Scan the source array — much faster than querying the DOM
  var changeSet={};
  changeLines=[];
  originalLines.forEach(function(line,li){
    if(line.indexOf('[A>')>=0||line.indexOf('[D>')>=0||
       line.indexOf('<A]')>=0||line.indexOf('<D]')>=0){
      changeLines.push(li); changeSet[li]=true;
    }
  });
  diffsCount=changeLines.length;
  // Batch-sync DOM classes without forcing layout on each element
  requestAnimationFrame(function(){
    document.querySelectorAll('.tl').forEach(function(tl){
      var li=parseInt(tl.dataset.li||'-1');
      tl.classList.toggle('has-change',!!changeSet[li]);
    });
  });
}
refreshChangeMeta();

// Diff navigation
var currentDiffIdx=-1;
function navDiff(dir){
  if(!changeLines.length){document.getElementById('statBar').textContent='No diffs found';return;}
  currentDiffIdx+=dir;
  if(currentDiffIdx<0)currentDiffIdx=changeLines.length-1;
  if(currentDiffIdx>=changeLines.length)currentDiffIdx=0;
  var li=changeLines[currentDiffIdx];
  // Clear previous highlights
  clearHL();
  var tl=document.querySelector('.tl[data-li="'+li+'"]');
  if(tl){
    syncLock=true;  // block cross-panel sync during navigation
    tl.classList.add('active');
    tl.scrollIntoView({behavior:'smooth',block:'center'});
    // Also highlight on PDF
    var firstWd=null;
    tl.querySelectorAll('.tw').forEach(function(t){
      t.classList.add('sel');
      var k=t.dataset.li+'_'+t.dataset.wi,wd=wordMap[k];
      if(wd){hlWord(wd,t.dataset.li);if(!firstWd)firstWd=wd;}
    });
    flushHL();
    // Explicitly scroll PDF to the correct page
    if(firstWd){
      var nc=containers[firstWd.page];
      ensurePageLoaded(firstWd.page,function(){
        var ni=nc.querySelector('img');
        if(ni&&ni.naturalWidth){
          var nds=ni.clientWidth/ni.naturalWidth;
          pdfPanel.scrollTo({top:nc.offsetTop+firstWd.y0*S*nds-120,behavior:'smooth'});
        } else {
          pdfPanel.scrollTo({top:nc.offsetTop-60,behavior:'smooth'});
        }
        setTimeout(function(){syncLock=false;},600);
      });
    } else {
      setTimeout(function(){syncLock=false;},600);
    }
  }
  document.getElementById('statBar').textContent='Diff '+(currentDiffIdx+1)+' / '+changeLines.length+' (Line '+(li+1)+')';
}

// Page nav + bidirectional scroll sync
var totalPages=pageImages.length;
document.getElementById('pageLbl').textContent='1 / '+totalPages;
var pdfPanel=document.getElementById('pdfPanel');
var syncLock=false,pdfScrollTmr=null,textScrollTmr=null;

pdfPanel.addEventListener('scroll',function(){
  var scrollTop=pdfPanel.scrollTop;
  var best=0;
  containers.forEach(function(c,i){if(c.offsetTop-60<=scrollTop)best=i;});
  document.getElementById('pageLbl').textContent=(best+1)+' / '+totalPages;
  // PDF→Text sync: scroll text panel to first line of current page
  if(syncLock)return;
  clearTimeout(pdfScrollTmr);
  pdfScrollTmr=setTimeout(function(){
    var li=pageFirstLine[best];
    if(li===undefined)return;
    var tl=document.querySelector('.tl[data-li="'+li+'"]');
    if(!tl)return;
    syncLock=true;
    document.getElementById('textPanel').scrollTop=tl.offsetTop-80;
    setTimeout(function(){syncLock=false;},350);
  },80);
},{passive:true});

// Text→PDF sync: binary-search the line-offset index (O(log n))
document.getElementById('textPanel').addEventListener('scroll',function(){
  if(syncLock)return;
  clearTimeout(textScrollTmr);
  textScrollTmr=setTimeout(function(){
    var scrollTop=document.getElementById('textPanel').scrollTop;
    var li=firstVisibleLine(scrollTop);
    var wd=null;
    for(var la=0;la<8&&!wd;la++){if(lineFirstWord[li+la])wd=lineFirstWord[li+la];}
    if(!wd)return;
    var ctr=containers[wd.page];
    ensurePageLoaded(wd.page,function(){
      var img=ctr.querySelector('img');
      if(!img||!img.naturalWidth){
        syncLock=true;
        pdfPanel.scrollTop=ctr.offsetTop-60;
        setTimeout(function(){syncLock=false;},350);
        return;
      }
      var ds=img.clientWidth/img.naturalWidth;
      syncLock=true;
      pdfPanel.scrollTop=ctr.offsetTop+wd.y0*S*ds-80;
      setTimeout(function(){syncLock=false;},350);
    });
  },80);
},{passive:true});
function navPage(dir){
  var cur=parseInt(document.getElementById('pageLbl').textContent.split('/')[0].trim())-1;
  var next=Math.max(0,Math.min(totalPages-1,cur+dir));
  containers[next].scrollIntoView({behavior:'smooth',block:'start'});
}

// Zoom
var zoomLevel=0; // 0=fit, else percentage steps
var zoomSteps=[50,75,100,125,150,200];
function zoomPdf(dir){
  var imgs=document.querySelectorAll('.page-ctr img');
  if(dir===0){zoomLevel=0;imgs.forEach(function(img){img.style.width='100%';});
    document.getElementById('zoomLbl').textContent='Fit';
  } else {
    if(zoomLevel===0)zoomLevel=100;
    var idx=zoomSteps.indexOf(zoomLevel);
    if(idx===-1)idx=2;
    idx=Math.max(0,Math.min(zoomSteps.length-1,idx+dir));
    zoomLevel=zoomSteps[idx];
    imgs.forEach(function(img){img.style.width=zoomLevel+'%';});
    document.getElementById('zoomLbl').textContent=zoomLevel+'%';
  }
  setTimeout(function(){clearHL();buildZones();},200);
}

// Focus on changes
function toggleFocus(on){
  document.getElementById('textWrap').classList.toggle('focus',on);
}

// ── Deviation Mode ─────────────────────────────────────────────────────────
function toggleDevMode(){
  devActive=!devActive;
  var btn=document.getElementById('devBtn');
  btn.classList.toggle('dev-on',devActive);
  var strip=document.getElementById('devStrip');
  strip.classList.toggle('visible',devActive);
  if(devActive){
    document.getElementById('statBar').textContent='⚡ Applying deviation highlights…';
    applyDevHighlights(function(){
      computeDevStats();
      addMissingWordOverlays();
    });
  } else {
    applyDevHighlights(function(){
      removeMissingWordOverlays();
      document.getElementById('statBar').textContent='Ready';
    });
  }
}

function toggleBadges(on){
  showBadges=on;
  document.querySelectorAll('.tw.dev-ins,.tw.dev-sub').forEach(function(tw){
    tw.classList.toggle('dev-no-badge',!on);
  });
}

function applyDevHighlights(onDone){
  // Pre-build devDiffLines from the map (no DOM needed)
  var devLineSet={};
  devDiffLines=[];
  Object.keys(devMap).forEach(function(k){
    var s=devMap[k];
    if(s==='insert'||s==='replace'){var li=k.split('_')[0];devLineSet[li]=true;}
  });
  devDiffLines=Object.keys(devLineSet).map(Number).sort(function(a,b){return a-b;});
  devDiffIdx=-1;

  // Process .tw spans in chunks so the UI stays responsive
  var allTw=[].slice.call(document.querySelectorAll('.tw'));
  var CHUNK=400, idx=0;
  function processChunk(){
    var end=Math.min(idx+CHUNK,allTw.length);
    for(;idx<end;idx++){
      var tw=allTw[idx];
      tw.classList.remove('dev-ins','dev-sub','dev-no-badge');
      if(devActive){
        var k=tw.dataset.li+'_'+tw.dataset.wi;
        var status=devMap[k];
        if(status==='insert'){
          tw.classList.add('dev-ins');
          if(!showBadges)tw.classList.add('dev-no-badge');
        } else if(status==='replace'){
          tw.classList.add('dev-sub');
          if(!showBadges)tw.classList.add('dev-no-badge');
        }
      }
    }
    if(idx<allTw.length){ requestAnimationFrame(processChunk); }
    else if(onDone){ onDone(); }
  }
  requestAnimationFrame(processChunk);
}

function computeDevStats(){
  var nIns=0,nSub=0,nOk=0;
  Object.values(devMap).forEach(function(v){
    if(v==='insert')nIns++;
    else if(v==='replace')nSub++;
    else if(v==='match')nOk++;
  });
  var total=nIns+nSub+nOk+nPdfMissing;
  var acc=total>0?Math.round(nOk/total*1000)/10:100;
  document.getElementById('devIns').textContent=nIns+' inserted';
  document.getElementById('devSub').textContent=nSub+' changed';
  document.getElementById('devDel').textContent=nPdfMissing+' PDF words missing';
  document.getElementById('devOk').textContent=nOk+' matched';
  document.getElementById('devAccuracy').textContent='Match rate: '+acc+'%';
  document.getElementById('statBar').textContent=
    '⚡ '+nIns+' inserted · '+nSub+' changed · '+nPdfMissing+' missing · '+acc+'% match';
}

// Navigate between deviation lines
function navDevDiff(dir){
  if(!devDiffLines.length){document.getElementById('statBar').textContent='No deviations found';return;}
  devDiffIdx+=dir;
  if(devDiffIdx<0)devDiffIdx=devDiffLines.length-1;
  if(devDiffIdx>=devDiffLines.length)devDiffIdx=0;
  var li=devDiffLines[devDiffIdx];
  var tl=document.querySelector('.tl[data-li="'+li+'"]');
  if(tl){
    tl.scrollIntoView({behavior:'smooth',block:'center'});
    // Briefly flash the row
    tl.style.outline='2px solid #e67e00';
    setTimeout(function(){tl.style.outline='';},800);
  }
  document.getElementById('statBar').textContent=
    '⚡ Dev '+(devDiffIdx+1)+'/'+devDiffLines.length+' — Line '+(li+1);
}

// Draw red overlays on PDF for PDF words that were dropped from the converted text
var missingHlEls=[];
function addMissingWordOverlays(){
  removeMissingWordOverlays();
  Object.keys(devMissMap).forEach(function(k){
    var wd=wordMap[k];
    if(!wd)return;
    var ctr=containers[wd.page],img=ctr.querySelector('img');if(!img)return;
    var ds=img.clientWidth/img.naturalWidth;
    var el=document.createElement('div');el.className='hl-missing';
    el.style.left=(wd.x0*S*ds)+'px';
    el.style.top=(wd.y0*S*ds+img.offsetTop)+'px';
    el.style.width=((wd.x1-wd.x0)*S*ds)+'px';
    el.style.height=((wd.y1-wd.y0)*S*ds)+'px';
    el.title='PDF word possibly missing from converted text';
    ctr.appendChild(el);missingHlEls.push(el);
  });
}
function removeMissingWordOverlays(){
  missingHlEls.forEach(function(e){e.remove();});missingHlEls=[];
}

// Collapse unchanged blocks
var collapseActive=false;
function toggleCollapse(on){
  collapseActive=on;
  var tw=document.getElementById('textWrap');
  // Remove old collapse bars
  tw.querySelectorAll('.collapse-bar').forEach(function(b){b.remove();});
  if(!on){
    // Show all lines
    tw.querySelectorAll('.tl').forEach(function(tl){tl.style.display='';});
    return;
  }
  // Hide unchanged lines and insert collapse bars
  var lines=tw.querySelectorAll('.tl');
  var i=0;
  while(i<lines.length){
    if(!lines[i].classList.contains('has-change')){
      var start=i;
      while(i<lines.length&&!lines[i].classList.contains('has-change'))i++;
      var count=i-start;
      if(count>2){
        // Keep first and last, hide middle
        for(var j=start;j<i;j++) lines[j].style.display='none';
        var bar=document.createElement('div');
        bar.className='collapse-bar';
        bar.textContent='▸ Expand '+count+' unchanged lines';
        bar.dataset.start=start;bar.dataset.end=i;
        bar.onclick=function(){
          var s=parseInt(this.dataset.start),e=parseInt(this.dataset.end);
          for(var k=s;k<e;k++)lines[k].style.display='';
          this.remove();
        };
        lines[start].parentNode.insertBefore(bar,lines[start]);
      }
    } else {
      lines[i].style.display='';
      i++;
    }
  }
}

// Group by line for PDF click zones
var lg={};
Object.keys(wordMap).forEach(function(k){var li=k.split('_')[0];if(!lg[li])lg[li]=[];lg[li].push(wordMap[k]);});
// Precompute lookup tables for scroll sync
var pageFirstLine={},lineFirstWord={};
Object.keys(wordMap).forEach(function(k){
  var li=parseInt(k.split('_')[0]),wd=wordMap[k],pg=wd.page;
  if(!lineFirstWord[li])lineFirstWord[li]=wd;
  if(pageFirstLine[pg]===undefined||li<pageFirstLine[pg])pageFirstLine[pg]=li;
});

// ── Line-offset index for O(log n) text→PDF scroll sync ─────────────────────
var tlOffsets=[];   // sorted array of {top, li}
function buildLineOffsets(){
  tlOffsets=[];
  document.querySelectorAll('.tl').forEach(function(tl){
    tlOffsets.push({top:tl.offsetTop,li:parseInt(tl.dataset.li)});
  });
  // already in DOM order = sorted by top
}
// Binary search: first line whose offsetTop >= scrollTop
function firstVisibleLine(scrollTop){
  if(!tlOffsets.length)return 0;
  var lo=0,hi=tlOffsets.length-1;
  while(lo<hi){
    var mid=(lo+hi)>>1;
    if(tlOffsets[mid].top<scrollTop-44)lo=mid+1;
    else hi=mid;
  }
  return tlOffsets[lo].li;
}
// Build once after initial render; rebuild after any rebuildEditor()
requestAnimationFrame(buildLineOffsets);

// Build click zones for a single page (called after its image loads)
function buildZonesForPage(pgIdx){
  var ctr=containers[pgIdx];
  ctr.querySelectorAll('.cz').forEach(function(z){z.remove();});
  var img=ctr.querySelector('img');
  if(!img||!img.naturalWidth)return;
  var ds=img.clientWidth/img.naturalWidth;
  Object.keys(lg).forEach(function(li){
    var ws=lg[li];
    if(ws[0].page!==pgIdx)return;
    var x0=1e9,y0=1e9,x1=0,y1=0;
    ws.forEach(function(w){if(w.x0<x0)x0=w.x0;if(w.y0<y0)y0=w.y0;if(w.x1>x1)x1=w.x1;if(w.y1>y1)y1=w.y1;});
    var z=document.createElement('div');z.className='cz';
    z.style.left=(x0*S*ds)+'px';z.style.top=(y0*S*ds+img.offsetTop)+'px';
    z.style.width=((x1-x0)*S*ds)+'px';z.style.height=((y1-y0)*S*ds)+'px';
    z.onclick=function(){clickPdfLine(parseInt(li));};
    ctr.appendChild(z);
  });
}
// Rebuild all currently-loaded pages (e.g. on resize)
function buildZones(){
  containers.forEach(function(ctr,idx){
    var img=ctr.querySelector('img');
    if(img&&img.naturalWidth&&!img.dataset.pgidx)buildZonesForPage(idx);
  });
}
window.addEventListener('resize',function(){clearHL();buildZones();});

// ── Force-load a lazy page image, then call back ──────────────────────────
function ensurePageLoaded(pgIdx,cb){
  var ctr=containers[pgIdx],img=ctr.querySelector('img');
  if(!img){if(cb)cb();return;}
  if(img.naturalWidth&&!img.dataset.pgidx){
    if(cb)cb();return;  // already loaded
  }
  // Force eager load
  if(img.dataset.pgidx){
    img.src='data:image/png;base64,'+pageImages[pgIdx];
    img.style.minHeight='';img.removeAttribute('data-pgidx');
    _lazyObs.unobserve(img);
  }
  img.addEventListener('load',function(){
    buildZonesForPage(pgIdx);
    if(cb)cb();
  },{once:true});
  // If already loaded (cached), fire immediately
  if(img.naturalWidth&&cb){cb();}
}

// Highlights
var hlEls=[];
function clearHL(){
  document.querySelectorAll('.tw.sel').forEach(function(e){e.classList.remove('sel');});
  document.querySelectorAll('.tl.active').forEach(function(e){e.classList.remove('active');});
  hlEls.forEach(function(e){e.remove();});hlEls=[];
}
function hlWord(wd,lineKey){
  // Just collect — actual drawing done by flushHL()
  hlQueue.push({x0:wd.x0,y0:wd.y0,x1:wd.x1,y1:wd.y1,page:wd.page,lk:lineKey||''});
}
var hlQueue=[];
function flushHL(){
  if(!hlQueue.length)return;
  // Group by page + text-line-key + PDF-line (same y-range)
  var groups={};
  hlQueue.forEach(function(wd){
    var yKey=Math.round(wd.y0/3)*3;
    var gk=wd.page+'_'+wd.lk+'_'+yKey;
    if(!groups[gk])groups[gk]={x0:wd.x0,y0:wd.y0,x1:wd.x1,y1:wd.y1,page:wd.page};
    else{
      var g=groups[gk];
      g.x0=Math.min(g.x0,wd.x0);g.y0=Math.min(g.y0,wd.y0);
      g.x1=Math.max(g.x1,wd.x1);g.y1=Math.max(g.y1,wd.y1);
    }
  });
  // Collect pages that need loading
  var needPages={};
  Object.keys(groups).forEach(function(gk){needPages[groups[gk].page]=true;});
  var pagesToLoad=Object.keys(needPages).map(Number);
  var loaded=0;
  function drawAfterLoad(){
    loaded++;
    if(loaded<pagesToLoad.length)return;
    Object.keys(groups).forEach(function(gk){
      var r=groups[gk];
      var ctr=containers[r.page],img=ctr.querySelector('img');
      if(!img||!img.naturalWidth)return;
      var ds=img.clientWidth/img.naturalWidth;
      var el=document.createElement('div');el.className='hl-overlay';
      el.style.left=(r.x0*S*ds)+'px';el.style.top=(r.y0*S*ds+img.offsetTop)+'px';
      el.style.width=((r.x1-r.x0)*S*ds)+'px';el.style.height=((r.y1-r.y0)*S*ds)+'px';
      ctr.appendChild(el);hlEls.push(el);
    });
  }
  if(!pagesToLoad.length)return;
  hlQueue=[];
  pagesToLoad.forEach(function(pg){ensurePageLoaded(pg,drawAfterLoad);});
}
function scrollPdf(wd){
  syncLock=true;  // block cross-panel sync during smooth scroll
  var ctr=containers[wd.page];
  ensurePageLoaded(wd.page,function(){
    var img=ctr.querySelector('img');
    if(!img||!img.naturalWidth){
      document.getElementById('pdfPanel').scrollTo({top:ctr.offsetTop-60,behavior:'smooth'});
      setTimeout(function(){syncLock=false;},600);
      return;
    }
    var ds=img.clientWidth/img.naturalWidth;
    document.getElementById('pdfPanel').scrollTo({top:ctr.offsetTop+wd.y0*S*ds-120,behavior:'smooth'});
    setTimeout(function(){syncLock=false;},600);
  });
}

// Clear highlights when clicking empty area
document.getElementById('pdfPanel').addEventListener('mousedown',function(e){
  if(!e.target.closest('.cz'))clearHL();
});

// Native text selection → PDF highlight
document.getElementById('textWrap').addEventListener('mouseup',function(){
  setTimeout(function(){ highlightFromSelection(); },10);
});

function highlightFromSelection(){
  clearHL();
  var sel=window.getSelection();
  if(!sel||sel.isCollapsed||!sel.rangeCount)return;
  // Find all .tw spans that intersect the selection
  var range=sel.getRangeAt(0);
  var allTw=document.querySelectorAll('.tw');
  var matched=[],n=0,first=null;
  allTw.forEach(function(tw){
    if(range.intersectsNode(tw)){
      tw.classList.add('sel');
      var tl=tw.closest('.tl');if(tl)tl.classList.add('active');
      var k=tw.dataset.li+'_'+tw.dataset.wi;
      var wd=wordMap[k];
      if(wd){hlWord(wd,tw.dataset.li);n++;if(!first)first=wd;}
      matched.push(tw);
    }
  });
  flushHL();
  if(first)scrollPdf(first);
  if(matched.length===1){
    var txt=matched[0].textContent.trim();
    document.getElementById('statBar').textContent='"'+txt+'" → Page '+(first?first.page+1:'?');
  } else if(matched.length>1){
    document.getElementById('statBar').textContent=matched.length+' words selected, '+n+' mapped to PDF';
  }
}

// PDF → Text
function clickPdfLine(li){
  clearHL();
  syncLock=true;  // block text→PDF sync from moving the PDF away
  var tl=document.querySelector('.tl[data-li="'+li+'"]');
  if(tl){tl.classList.add('active');tl.scrollIntoView({behavior:'smooth',block:'center'});
    tl.querySelectorAll('.tw').forEach(function(t){
      t.classList.add('sel');var k=t.dataset.li+'_'+t.dataset.wi,wd=wordMap[k];if(wd)hlWord(wd,t.dataset.li);
    });
    flushHL();
  }
  setTimeout(function(){syncLock=false;},600);
  document.getElementById('statBar').textContent='PDF → Line '+(parseInt(li)+1);
}

// Always-on inline editing with auto-apply
var autoApplyTimer=null;
function scheduleAutoApply(){
  if(autoApplyTimer)clearTimeout(autoApplyTimer);
  document.getElementById('statBar').textContent='Editing…';
  autoApplyTimer=setTimeout(function(){applyInlineEdits();},700);
}
function enableInlineEditing(){
  var tw=document.getElementById('textWrap');
  tw.classList.add('editing');
  tw.querySelectorAll('.lc').forEach(function(lc){
    lc.contentEditable='true';
    lc.spellcheck=false;
    lc.addEventListener('input',scheduleAutoApply);
  });
}
function applyInlineEdits(){
  var tw=document.getElementById('textWrap');
  var lines=[];
  tw.querySelectorAll('.tl').forEach(function(tl){
    var lc=tl.querySelector('.lc');
    lines.push(lc?lc.textContent:'');
  });
  originalLines=lines;
  tw.innerHTML=renderLinesWithTags(originalLines);
  refreshChangeMeta();
  enableInlineEditing();
  if(document.getElementById('focusCb').checked)toggleFocus(true);
  if(document.getElementById('collapseCb').checked)toggleCollapse(true);
  document.getElementById('statBar').textContent='✅ Auto-updated';
}
enableInlineEditing();
// Render lines with [A>...<A] and [D>...<D] tag styling
function renderLinesWithTags(lines){
  var html='',inA=false,inD=false;
  lines.forEach(function(line,li){
    var ln=li+1;
    var lineStartA=inA,lineStartD=inD;
    var words=line.split(' ');
    var wordHtmls=[];
    words.forEach(function(rw,wi){
      if(!rw){wordHtmls.push('');return;}
      // Split word at tag boundaries
      var segs=splitAtTags(rw);
      var inner='';
      segs.forEach(function(seg){
        var esc=seg.text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        if(seg.marker){
          if(seg.type==='a_open'){inner+='<span class="tg-a">'+esc+'</span>';inA=true;}
          else if(seg.type==='a_close'){inner+='<span class="tg-a">'+esc+'</span>';inA=false;}
          else if(seg.type==='d_open'){inner+='<span class="tg-d">'+esc+'</span>';inD=true;}
          else if(seg.type==='d_close'){inner+='<span class="tg-d">'+esc+'</span>';inD=false;}
        } else {
          if(inA) inner+='<span class="tg-a">'+esc+'</span>';
          else if(inD) inner+='<span class="tg-d">'+esc+'</span>';
          else inner+=esc;
        }
      });
      var key=li+'_'+wi;
      var has=wordMap[key]?'1':'0';
      wordHtmls.push('<span class="tw" data-li="'+li+'" data-wi="'+wi+'" data-m="'+has+'">'+inner+'</span>');
    });
    // Join with decorated spaces based on current tag state
    var parts=[];
    // Replay tag state per word to know state after each word
    var stateA=false,stateD=false;
    // We need state BEFORE first word — use inA/inD from line start (tracked across lines)
    // Actually inA/inD is already updated by the segment loop above, so let's track per-word states
    // Re-derive: replay from line start
    var wordStatesAfter=[];
    var sA=lineStartA,sD=lineStartD;
    words.forEach(function(rw){
      splitAtTags(rw).forEach(function(seg){
        if(seg.marker){
          if(seg.type==='a_open')sA=true;else if(seg.type==='a_close')sA=false;
          else if(seg.type==='d_open')sD=true;else if(seg.type==='d_close')sD=false;
        }
      });
      wordStatesAfter.push({a:sA,d:sD});
    });
    for(var i=0;i<wordHtmls.length;i++){
      if(i>0){
        var prev=wordStatesAfter[i-1];
        if(prev.a)parts.push('<span class="tg-a"> </span>');
        else if(prev.d)parts.push('<span class="tg-d"> </span>');
        else parts.push(' ');
      }
      parts.push(wordHtmls[i]);
    }
    html+='<div class="tl" data-li="'+li+'"><span class="ln">'+ln+'</span><span class="lc">'+parts.join('')+'</span></div>';
  });
  return html;
}
function splitAtTags(word){
  var markers=[['[A>','a_open'],['<A]','a_close'],['[D>','d_open'],['<D]','d_close']];
  var segs=[],rem=word;
  while(rem.length>0){
    var ep=rem.length,em=null;
    markers.forEach(function(m){var p=rem.indexOf(m[0]);if(p>=0&&p<ep){ep=p;em=m;}});
    if(!em){segs.push({text:rem,marker:false,type:null});break;}
    if(ep>0)segs.push({text:rem.substring(0,ep),marker:false,type:null});
    segs.push({text:em[0],marker:true,type:em[1]});
    rem=rem.substring(ep+em[0].length);
  }
  return segs;
}
function downloadText(){
  var text=originalLines.join('\n');
  var blob=new Blob([text],{type:'text/plain'});
  var a=document.createElement('a');
  a.href=URL.createObjectURL(blob);
  a.download='__FILENAME__';
  document.body.appendChild(a);a.click();document.body.removeChild(a);
  URL.revokeObjectURL(a.href);
  document.getElementById('statBar').textContent='Downloaded';
}

// ── Wrap Selected Text in Tags ────────────────────────────────
function buildTagIndices(){/* no-op — counts removed */}

function wrapTag(type){
  var sel=window.getSelection();
  if(!sel||sel.isCollapsed||!sel.rangeCount){
    document.getElementById('statBar').textContent='Select text first, then click '+(type==='a'?'[A> Wrap':'[D> Wrap');
    return;
  }
  var range=sel.getRangeAt(0);
  var lc=range.startContainer;
  while(lc&&!(lc.classList&&lc.classList.contains('lc')))lc=lc.parentNode;
  if(!lc){document.getElementById('statBar').textContent='Select text inside a line';return;}
  var tl=lc.closest('.tl');if(!tl)return;
  var li=parseInt(tl.dataset.li);
  var text=originalLines[li]||'';
  var startOff=getTextOffset(lc,range.startContainer,range.startOffset);
  var endOff=getTextOffset(lc,range.endContainer,range.endOffset);
  if(startOff===endOff){document.getElementById('statBar').textContent='Select some text first';return;}

  var openA='[A>',closeA='<A]',openD='[D>',closeD='<D]';
  var selected=text.substring(startOff,endOff);

  // Check if selection is exactly inside a tag span (look for open before and close after)
  // Expand search: find the tag boundaries around the selection
  var before=text.substring(0,startOff),after=text.substring(endOff);

  // Check if selected region has tags wrapping it or inside it
  var hasA=selected.indexOf('[A>')>=0||selected.indexOf('<A]')>=0||
           (before.lastIndexOf('[A>')>before.lastIndexOf('<A]')&&after.indexOf('<A]')<after.indexOf('[A>'));
  var hasD=selected.indexOf('[D>')>=0||selected.indexOf('<D]')>=0||
           (before.lastIndexOf('[D>')>before.lastIndexOf('<D]')&&after.indexOf('<D]')<after.indexOf('[D>'));

  // Also check: does the selected region start right after an open tag and end right before close tag?
  var aWrapsBefore=before.length>=3&&before.substring(before.length-3)==='[A>';
  var aWrapsAfter=after.length>=3&&after.substring(0,3)==='<A]';
  var dWrapsBefore=before.length>=3&&before.substring(before.length-3)==='[D>';
  var dWrapsAfter=after.length>=3&&after.substring(0,3)==='<D]';

  // Strip all A tags from selected text
  function stripA(s){return s.replace(/\[A>/g,'').replace(/<A\]/g,'');}
  function stripD(s){return s.replace(/\[D>/g,'').replace(/<D\]/g,'');}

  var result,msg;
  if(type==='a'){
    if(aWrapsBefore&&aWrapsAfter){
      // Toggle OFF: remove the surrounding [A>...<A]
      result=before.substring(0,before.length-3)+selected+after.substring(3);
      msg='[A> tag removed (Line '+(li+1)+')';
    } else if(dWrapsBefore&&dWrapsAfter){
      // Swap D→A: replace surrounding [D>...<D] with [A>...<A]
      result=before.substring(0,before.length-3)+'[A>'+stripD(selected)+'<A]'+after.substring(3);
      msg='[D> → [A> swapped (Line '+(li+1)+')';
    } else if(selected.indexOf('[A>')>=0||selected.indexOf('<A]')>=0){
      // Selection contains A tags — remove them (toggle off)
      result=before+stripA(selected)+after;
      msg='[A> tags removed (Line '+(li+1)+')';
    } else if(selected.indexOf('[D>')>=0||selected.indexOf('<D]')>=0){
      // Selection contains D tags — swap to A
      result=before+'[A>'+stripD(selected)+'<A]'+after;
      msg='[D> → [A> swapped (Line '+(li+1)+')';
    } else {
      // No tags — wrap with A
      result=before+'[A>'+selected+'<A]'+after;
      msg='[A> tag added (Line '+(li+1)+')';
    }
  } else {
    if(dWrapsBefore&&dWrapsAfter){
      // Toggle OFF: remove the surrounding [D>...<D]
      result=before.substring(0,before.length-3)+selected+after.substring(3);
      msg='[D> tag removed (Line '+(li+1)+')';
    } else if(aWrapsBefore&&aWrapsAfter){
      // Swap A→D: replace surrounding [A>...<A] with [D>...<D]
      result=before.substring(0,before.length-3)+'[D>'+stripA(selected)+'<D]'+after.substring(3);
      msg='[A> → [D> swapped (Line '+(li+1)+')';
    } else if(selected.indexOf('[D>')>=0||selected.indexOf('<D]')>=0){
      // Selection contains D tags — remove them (toggle off)
      result=before+stripD(selected)+after;
      msg='[D> tags removed (Line '+(li+1)+')';
    } else if(selected.indexOf('[A>')>=0||selected.indexOf('<A]')>=0){
      // Selection contains A tags — swap to D
      result=before+'[D>'+stripA(selected)+'<D]'+after;
      msg='[A> → [D> swapped (Line '+(li+1)+')';
    } else {
      // No tags — wrap with D
      result=before+'[D>'+selected+'<D]'+after;
      msg='[D> tag added (Line '+(li+1)+')';
    }
  }
  originalLines[li]=result;
  rebuildEditor();
  document.getElementById('statBar').textContent=msg;
}

// ── Active Line Tracking ────────────────────────────────────
var activeEditorLine=null;
function setActiveLine(tl){
  if(activeEditorLine)activeEditorLine.classList.remove('editor-active');
  activeEditorLine=tl;if(tl)tl.classList.add('editor-active');
}
document.getElementById('textWrap').addEventListener('click',function(e){
  var tl=e.target.closest('.tl');if(tl)setActiveLine(tl);
});
document.getElementById('textWrap').addEventListener('focusin',function(e){
  var tl=e.target.closest('.tl');if(tl)setActiveLine(tl);
});

// ── Text Offset Helpers ─────────────────────────────────────
function getTextOffset(container,node,offset){
  if(container===node)return offset;
  var walker=document.createTreeWalker(container,NodeFilter.SHOW_TEXT);
  var total=0;
  while(walker.nextNode()){
    if(walker.currentNode===node)return total+offset;
    total+=walker.currentNode.length;
  }
  return total;
}
function placeCursorAtOffset(container,targetOffset){
  var walker=document.createTreeWalker(container,NodeFilter.SHOW_TEXT);
  var total=0;
  while(walker.nextNode()){
    var nd=walker.currentNode;
    if(total+nd.length>=targetOffset){
      var r=document.createRange();r.setStart(nd,targetOffset-total);r.collapse(true);
      var s=window.getSelection();s.removeAllRanges();s.addRange(r);return;
    }
    total+=nd.length;
  }
}

// ── Tag State Helper ──────────────────────────────────────────
function getTagStateAtOffset(text,off){
  var inA=false,inD=false,i=0;
  while(i<off&&i<text.length){
    if(text.substr(i,3)==='[A>'){inA=true;i+=3;}
    else if(text.substr(i,3)==='<A]'){inA=false;i+=3;}
    else if(text.substr(i,3)==='[D>'){inD=true;i+=3;}
    else if(text.substr(i,3)==='<D]'){inD=false;i+=3;}
    else i++;
  }
  return {inA:inA,inD:inD};
}

// ── Tag-Aware Split ─────────────────────────────────────────
function doSplitAt(li,off){
  var text=originalLines[li]||'';
  var left=text.substring(0,off),right=text.substring(off);
  var state=getTagStateAtOffset(text,off);
  var rightLi;
  if(state.inA){
    left=left.replace(/\s+$/,'')+'<A]';
    right='[A>'+right.replace(/^\s+/,'');
    originalLines.splice(li,1,left,'',right);
    rightLi=li+2;
  } else if(state.inD){
    left=left.replace(/\s+$/,'')+'<D]';
    right='[D>'+right.replace(/^\s+/,'');
    originalLines.splice(li,1,left,'',right);
    rightLi=li+2;
  } else {
    originalLines.splice(li,1,left,right);
    rightLi=li+1;
  }
  rebuildEditor();
  return rightLi;
}

function splitAtCursor(){
  var sel=window.getSelection();
  if(!sel||!sel.rangeCount){document.getElementById('statBar').textContent='Place cursor in a line to split';return;}
  var range=sel.getRangeAt(0);
  var lc=range.startContainer;
  while(lc&&!(lc.classList&&lc.classList.contains('lc')))lc=lc.parentNode;
  if(!lc){document.getElementById('statBar').textContent='Click inside a text line first';return;}
  var tl=lc.closest('.tl');if(!tl)return;
  var li=parseInt(tl.dataset.li);
  var off=getTextOffset(lc,range.startContainer,range.startOffset);
  var rightLi=doSplitAt(li,off);
  document.getElementById('statBar').textContent='\u2702 Split line '+(li+1)+' \u2192 lines '+(li+1)+' & '+(rightLi+1);
}

// ── Tag-Aware Merge ─────────────────────────────────────────
function mergeWithPrevious(){
  var tl=activeEditorLine;
  if(!tl){document.getElementById('statBar').textContent='Click a line first';return;}
  var li=parseInt(tl.dataset.li);
  if(li===0){document.getElementById('statBar').textContent='Cannot merge \u2014 already first line';return;}
  // Find previous non-blank line (skip blank lines between)
  var prevLi=li-1;
  while(prevLi>=0&&originalLines[prevLi].trim()==='')prevLi--;
  if(prevLi<0){
    // Only blank lines above \u2014 just remove them
    originalLines.splice(0,li);
    rebuildEditor();return;
  }
  var prev=originalLines[prevLi],curr=originalLines[li],merged;
  // Detect matching tag boundaries: prev ends with <A] and curr starts with [A> (or same for [D>)
  var prevEndsA=/<A\]\s*$/.test(prev),currStartsA=/^\s*\[A>/.test(curr);
  var prevEndsD=/<D\]\s*$/.test(prev),currStartsD=/^\s*\[D>/.test(curr);
  if(prevEndsA&&currStartsA){
    // Same [A> tag \u2014 remove close from prev + open from curr, join with space
    var lp=prev.replace(/<A\]\s*$/,'').replace(/\s+$/,'');
    var rp=curr.replace(/^\s*\[A>/,'').replace(/^\s+/,'');
    merged=lp+' '+rp;
  } else if(prevEndsD&&currStartsD){
    // Same [D> tag \u2014 remove close from prev + open from curr, join with space
    var lp=prev.replace(/<D\]\s*$/,'').replace(/\s+$/,'');
    var rp=curr.replace(/^\s*\[D>/,'').replace(/^\s+/,'');
    merged=lp+' '+rp;
  } else {
    // No matching tags \u2014 simple merge
    merged=prev;
    if(merged.length>0&&curr.length>0&&!merged.endsWith(' ')&&!curr.startsWith(' '))merged+=' ';
    merged+=curr;
  }
  // Replace from prevLi to li (inclusive), removing any blank lines between
  originalLines.splice(prevLi,li-prevLi+1,merged);
  rebuildEditor();
  var newTl=document.querySelector('.tl[data-li="'+prevLi+'"]');
  if(newTl)setActiveLine(newTl);
  document.getElementById('statBar').textContent='\u2295 Merged \u2192 line '+(prevLi+1);
}

// ── Rebuild editor after split/merge ────────────────────────
function rebuildEditor(){
  var tw=document.getElementById('textWrap');
  var panel=document.getElementById('textPanel');
  var scrollTop=panel?panel.scrollTop:0;
  tw.innerHTML=renderLinesWithTags(originalLines);
  refreshChangeMeta();enableInlineEditing();buildTagIndices();
  if(document.getElementById('focusCb').checked)toggleFocus(true);
  if(document.getElementById('collapseCb').checked)toggleCollapse(true);
  if(panel)panel.scrollTop=scrollTop;
  // Rebuild line-offset index after DOM settles
  requestAnimationFrame(buildLineOffsets);
}

// ── Enhanced keyboard: Enter=tag-aware split, Backspace@0=simple merge ──
document.getElementById('textWrap').addEventListener('keydown',function(e){
;
  if(!sel||!sel.rangeCount)return;
  var range=sel.getRangeAt(0);
  var lc=range.startContainer;
  while(lc&&!(lc.classList&&lc.classList.contains('lc')))lc=lc.parentNode;
  if(!lc)return;
  var tl=lc.closest('.tl');if(!tl)return;
  var li=parseInt(tl.dataset.li);
  if(e.key==='Enter'){
    e.preventDefault();
    var panel=document.getElementById('textPanel');
    var scrollBefore=panel?panel.scrollTop:0;
    var off=getTextOffset(lc,range.startContainer,range.startOffset);
    var rightLi=doSplitAt(li,off);
    // Restore scroll then focus new line without scrolling
    if(panel)panel.scrollTop=scrollBefore;
    var nTl=document.querySelector('.tl[data-li="'+rightLi+'"]');
    if(nTl){
      setActiveLine(nTl);var nLc=nTl.querySelector('.lc');
      if(nLc){
        nLc.focus({preventScroll:true});
        var r=document.createRange();r.setStart(nLc,0);r.collapse(true);
        sel.removeAllRanges();sel.addRange(r);
      }
    }
  } else if(e.key==='Backspace'&&sel.isCollapsed&&!e.ctrlKey){
    var off=getTextOffset(lc,range.startContainer,range.startOffset);
    if(off===0&&li>0){
      e.preventDefault();
      var panel=document.getElementById('textPanel');
      var scrollBefore=panel?panel.scrollTop:0;
      var prevText=originalLines[li-1],cursorPos=prevText.length;
      originalLines[li-1]=prevText+originalLines[li];
      originalLines.splice(li,1);
      rebuildEditor();
      if(panel)panel.scrollTop=scrollBefore;
      var pTl=document.querySelector('.tl[data-li="'+(li-1)+'"]');
      if(pTl){
        setActiveLine(pTl);var pLc=pTl.querySelector('.lc');
        if(pLc){pLc.focus({preventScroll:true});placeCursorAtOffset(pLc,cursorPos);}
      }
    }
  }
});
</script>
</body>
</html>"""


def _split_at_tags(word: str) -> list:
    """
    Split a word at tag boundaries [A>, <A], [D>, <D].
    Returns list of (text, is_marker, marker_type).
    marker_type: 'a_open', 'a_close', 'd_open', 'd_close', or None.
    """
    TAG_PATTERNS = [
        ('[A>', 'a_open'),
        ('<A]', 'a_close'),
        ('[D>', 'd_open'),
        ('<D]', 'd_close'),
    ]
    segments = []
    remaining = word

    while remaining:
        earliest_pos = len(remaining)
        earliest_marker = None
        earliest_type = None
        for marker, mtype in TAG_PATTERNS:
            pos = remaining.find(marker)
            if pos != -1 and pos < earliest_pos:
                earliest_pos = pos
                earliest_marker = marker
                earliest_type = mtype

        if earliest_marker is None:
            if remaining:
                segments.append((remaining, False, None))
            break

        if earliest_pos > 0:
            segments.append((remaining[:earliest_pos], False, None))
        segments.append((earliest_marker, True, earliest_type))
        remaining = remaining[earliest_pos + len(earliest_marker):]

    return segments


def build_viewer_html(
    pdf_images_b64,
    clean_lines,
    word_map,
    dpi=PDF_RENDER_DPI,
    filename="edited_clean.txt",
    dev_map=None,
    miss_map=None,
    n_pdf_missing=0,
):
    import json as _json
    scale = dpi / 72.0

    text_lines_html = []
    in_add = False
    in_del = False
    for li, line in enumerate(clean_lines):
        raw_words = line.split(' ')
        word_entries = []
        for wi, rw in enumerate(raw_words):
            if not rw:
                st_val = 'add' if in_add else ('del' if in_del else 'none')
                word_entries.append(('', st_val, st_val))
                continue

            state_before = 'add' if in_add else ('del' if in_del else 'none')
            segments = _split_at_tags(rw)

            inner_html = ''
            for seg_text, is_marker, marker_type in segments:
                esc = seg_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                if is_marker:
                    if marker_type == 'a_open':
                        inner_html += '<span class="tg-a">{}</span>'.format(esc)
                        in_add = True
                    elif marker_type == 'a_close':
                        inner_html += '<span class="tg-a">{}</span>'.format(esc)
                        in_add = False
                    elif marker_type == 'd_open':
                        inner_html += '<span class="tg-d">{}</span>'.format(esc)
                        in_del = True
                    elif marker_type == 'd_close':
                        inner_html += '<span class="tg-d">{}</span>'.format(esc)
                        in_del = False
                else:
                    if in_add:
                        inner_html += '<span class="tg-a">{}</span>'.format(esc)
                    elif in_del:
                        inner_html += '<span class="tg-d">{}</span>'.format(esc)
                    else:
                        inner_html += esc

            state_after = 'add' if in_add else ('del' if in_del else 'none')
            key = "{}_{}".format(li, wi)
            has = "1" if key in word_map else "0"
            tw = '<span class="tw" data-li="{}" data-wi="{}" data-m="{}">{}</span>'.format(
                li, wi, has, inner_html
            )
            word_entries.append((tw, state_before, state_after))

        line_parts = []
        for idx, (wh, sb, sa) in enumerate(word_entries):
            if idx > 0:
                prev_sa = word_entries[idx - 1][2]
                if prev_sa == 'add':
                    line_parts.append('<span class="tg-a"> </span>')
                elif prev_sa == 'del':
                    line_parts.append('<span class="tg-d"> </span>')
                else:
                    line_parts.append(' ')
            line_parts.append(wh)

        ln = li + 1
        _hc = ' has-change' if ('[A>' in line or '[D>' in line or '<A]' in line or '<D]' in line) else ''
        text_lines_html.append(
            '<div class="tl{}" data-li="{}"><span class="ln">{}</span>'
            '<span class="lc">{}</span></div>'.format(_hc, li, ln, ''.join(line_parts))
        )

    text_block = "\n".join(text_lines_html)
    imgs_js = ",".join('"' + b + '"' for b in pdf_images_b64)
    wordmap_js = _json.dumps(word_map)
    lines_js = _json.dumps(clean_lines)

    change_lines = []
    for li, line in enumerate(clean_lines):
        if '[A>' in line or '[D>' in line or '<A]' in line or '<D]' in line:
            change_lines.append(li)

    devmap_js = _json.dumps(dev_map or {})
    missmap_js = _json.dumps(miss_map or {})

    return (_TEMPLATE
            .replace("__TEXT_BLOCK__", text_block)
            .replace("__IMGS_JS__", imgs_js)
            .replace("__WORDMAP_JS__", wordmap_js)
            .replace("__LINES_JS__", lines_js)
            .replace("__SCALE__", str(scale))
            .replace("__FILENAME__", filename)
            .replace("__CHANGELINES_JS__", _json.dumps(change_lines))
            .replace("__DEVMAP_JS__", devmap_js)
            .replace("__DEVMISSMAP_JS__", missmap_js)
            .replace("__N_PDF_MISSING__", str(n_pdf_missing)))


# ── Streamlit app ────────────────────────────────────────────────────────────

def main():
    st.markdown("""<style>
        #MainMenu, header, footer, .stDeployButton { visibility: hidden; }
        .block-container { padding: 0 !important; max-width: 100% !important; }
        html, body, [class*="css"] { font-size: 13px; }
        h1, h2, h3, h4 { font-size: 1rem !important; margin: 0 !important; padding: 0 !important; }
        .appview-container { padding-top: 0 !important; }
        [data-testid="stAppViewBlockContainer"] { padding: 0 !important; }
        iframe { border: none !important; }
        .drop-zone-row .stFileUploader label { display: none !important; }
        .drop-zone-row [data-testid="stFileUploaderDropzone"] {
            border: 2px dashed #555 !important;
            border-radius: 8px !important;
            background: #1e1e2e !important;
            min-height: 300px !important;
            display: flex !important;
            align-items: center !important;
            justify-content: center !important;
        }
        .drop-zone-row [data-testid="stFileUploaderDropzone"] * {
            color: #ccc !important;
        }
        .drop-zone-row [data-testid="stFileUploaderDropzone"] button {
            background: #333 !important;
            border: 1px solid #555 !important;
            color: #eee !important;
            border-radius: 4px !important;
        }
        .stAlert { padding: 0.3rem 0.5rem !important; font-size: 12px !important; }
    </style>""", unsafe_allow_html=True)

    pdf_file = st.session_state.get("pdf")
    txt_file = st.session_state.get("txt")

    if not pdf_file or not txt_file:
        st.markdown("""
        <div style="text-align:center;padding:10px 0 6px;font-size:14px;font-weight:600;color:#333;">
            📄 PDF ↔ Text Comparator — Drop files below
        </div>
        """, unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        with c1:
            st.markdown('<div class="drop-zone-row">', unsafe_allow_html=True)
            pdf_file = st.file_uploader("PDF", type=["pdf"], key="pdf", label_visibility="collapsed")
            st.markdown("</div>", unsafe_allow_html=True)
            if not pdf_file:
                st.caption("⬆️ Drop PDF here")
        with c2:
            st.markdown('<div class="drop-zone-row">', unsafe_allow_html=True)
            txt_file = st.file_uploader("TXT", type=["txt"], key="txt", label_visibility="collapsed")
            st.markdown("</div>", unsafe_allow_html=True)
            if not txt_file:
                st.caption("⬆️ Drop .txt file here")

        if not pdf_file or not txt_file:
            return

    pdf_bytes = pdf_file.read()
    clean_text = txt_file.read().decode("utf-8", errors="replace")
    clean_lines = clean_text.splitlines()

    with st.spinner("Rendering pages & mapping words…"):
        images = render_pdf_pages(pdf_bytes)
        words = extract_word_bboxes(pdf_bytes)

    if "edit_mode" not in st.session_state:
        st.session_state.edit_mode = False
    if "edited_text" not in st.session_state:
        st.session_state.edited_text = None

    display_text = st.session_state.edited_text if st.session_state.edited_text is not None else clean_text
    display_lines = display_text.splitlines()

    with st.spinner("Mapping words & computing deviations…"):
        word_map = build_word_level_map(display_lines, words)
        dev_map, miss_map, n_pdf_missing = compute_diff_map(display_lines, words)

    html = build_viewer_html(
        images, display_lines, word_map,
        dpi=PDF_RENDER_DPI,
        filename=txt_file.name,
        dev_map=dev_map,
        miss_map=miss_map,
        n_pdf_missing=n_pdf_missing,
    )
    st.components.v1.html(html, height=920, scrolling=False)


if __name__ == "__main__":
    main()
