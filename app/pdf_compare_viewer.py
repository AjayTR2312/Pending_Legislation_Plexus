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
from typing import List, Dict, Tuple

# Only set page config when running as standalone (not when imported)
if __name__ == "__main__":
    st.set_page_config(
        page_title="PDF ↔ Text Comparator",
        page_icon="📄",
        layout="wide",
        initial_sidebar_state="collapsed",
    )

PDF_RENDER_DPI = 400


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
    # Set up Tesseract path for OCR fallback
    _tess_dir = r"C:\Users\C302602\AppData\Local\Programs\Tesseract-OCR"
    _tessdata = os.path.join(_tess_dir, "tessdata")
    if os.path.isdir(_tessdata):
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
    """Returns dict keyed by 'lineIdx_wordIdx' -> {page, x0, y0, x1, y1}.

    Uses a two-phase search strategy so that reordered text files
    (e.g. hybrid extraction that moves headers / boilerplate) still
    get mapped correctly:

      Phase 1 – Bidirectional local search around the cursor (fast).
      Phase 2 – Global fallback using an inverted index when local
                search fails (handles arbitrary reordering).
    """
    pdf_words = [(re.sub(r"[^\w]", "", w["text"].lower()), w) for w in word_infos]
    pdf_texts = [pw[0] for pw in pdf_words]

    # Inverted index: normalised word → list of positions in pdf_texts
    word_index: Dict[str, List[int]] = {}
    for i, t in enumerate(pdf_texts):
        if t:
            word_index.setdefault(t, []).append(i)

    mapping = {}
    cursor = 0

    def _score_at(start: int, toks: list) -> int:
        """Count how many *toks* match sequentially from *start*."""
        n, pi = 0, start
        for tok in toks:
            span = min(10, len(pdf_texts) - pi)
            for off in range(span):
                if pdf_texts[pi + off] == tok:
                    n += 1
                    pi = pi + off + 1
                    break
        return n

    for li, raw in enumerate(clean_lines):
        stripped = _strip_tags(raw).strip()
        if not stripped:
            continue
        raw_tokens = stripped.split()
        tokens = [re.sub(r"[^\w]", "", rt.lower()) for rt in raw_tokens]
        tokens = [t for t in tokens if t]
        if not tokens:
            continue

        best_pos, best_n = -1, 0
        threshold_good = max(1, int(len(tokens) * 0.7))
        threshold_min  = max(1, int(len(tokens) * 0.3))

        # Phase 1 – bidirectional local search around cursor
        local_lo = max(0, cursor - 500)
        local_hi = min(cursor + 2000, len(pdf_texts))
        for start in range(local_lo, local_hi):
            n = _score_at(start, tokens)
            if n > best_n:
                best_n, best_pos = n, start
            if best_n >= threshold_good:
                break

        # Phase 2 – global fallback via inverted index
        if best_n < threshold_min:
            # Pick the rarest token as anchor so we check fewer candidates
            best_anchor, min_freq = None, float('inf')
            for t in tokens:
                freq = len(word_index.get(t, []))
                if 0 < freq < min_freq:
                    min_freq = freq
                    best_anchor = t
            if best_anchor:
                cands = sorted(word_index[best_anchor],
                               key=lambda p: abs(p - cursor))
                for start in cands[:80]:
                    if local_lo <= start < local_hi:
                        continue            # already scored in phase 1
                    n = _score_at(start, tokens)
                    if n > best_n:
                        best_n, best_pos = n, start
                    if best_n >= threshold_good:
                        break

        if best_pos >= 0 and best_n >= threshold_min:
            orig_words = raw.split(' ')
            vis_idx = 0
            pi = best_pos
            for ow_idx, ow in enumerate(orig_words):
                clean_w = _strip_tags(ow).strip()
                norm_w = re.sub(r"[^\w]", "", clean_w.lower())
                if not norm_w:
                    continue
                if vis_idx < len(tokens):
                    for off in range(min(10, len(pdf_texts) - pi)):
                        if pdf_texts[pi + off] == norm_w:
                            pw = pdf_words[pi + off][1]
                            mapping["{}_{}".format(li, ow_idx)] = {
                                "page": pw["page"],
                                "x0": pw["x0"], "y0": pw["y0"],
                                "x1": pw["x1"], "y1": pw["y1"],
                            }
                            pi = pi + off + 1
                            break
                    vis_idx += 1
            cursor = pi  # advance — allows jumps for reordered text

    return mapping


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

.panels{display:flex;height:calc(100dvh - 44px);min-height:calc(100vh - 44px)}
.panel{flex:1;overflow-y:auto;position:relative}
.panel-pdf{
  background:#fafafa;border-right:1px solid var(--border);
  /* Allow horizontal scroll when the PDF is zoomed beyond panel width.
     overflow:auto on BOTH axes lets the user scroll right to see the
     clipped right edge of a zoomed page.                              */
  overflow:auto;
}
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
          /* margin:auto centres each page in the panel when its width is
             smaller than the panel.  When zoomed beyond panel width, the
             page-ctr grows past the panel and panel's overflow:auto
             provides a horizontal scrollbar.                          */
          margin-left:auto;margin-right:auto}
.page-ctr canvas{width:100%;display:block}
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

/* PDF.js native text layer */
.pdf-text-layer{
  position:absolute;left:0;
  z-index:6;
  overflow:hidden;
  user-select:text;-webkit-user-select:text;
  transform-origin:0 0;
  line-height:1;
  /* Clip content to this element's border-box even when transformed */
  contain:paint;
}
.pdf-text-layer span,.pdf-text-layer br{
  color:transparent;position:absolute;white-space:pre;cursor:text;
  transform-origin:0% 0%;
}
.pdf-text-layer span::selection{background:rgba(9,105,218,.35)}
.pdf-text-layer span::-moz-selection{background:rgba(9,105,218,.35)}

.text-wrap{padding:0;min-height:calc(100dvh - 90px);background:var(--bg)}
.tl{
  padding:0 16px 0 0;
  font-family:'Cascadia Code','JetBrains Mono','Fira Code',Consolas,monospace;
  font-size:13px;line-height:1.75;
  border-left:3px solid transparent;
  white-space:pre-wrap;word-wrap:break-word;display:flex;
  transition:background .08s;cursor:text;
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
/* contentEditable is now on the parent #textWrap (not per-.lc) so that
   selection can span multiple lines.  Caret styling applies whenever
   the parent is in editing mode.                                      */
.text-wrap.editing{caret-color:var(--accent);outline:none}
.text-wrap.editing:focus{outline:none}
.text-wrap.editing .lc{outline:none}
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
</style>
<script src="https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/build/pdf.min.js"></script>
</head>
<body>

<div class="top-bar">
  <span class="logo">📄 PDF ↔ Text Comparator</span>
  <span class="sep"></span>
  <button class="btn" onclick="navDiff(-1)" title="Previous diff">◀</button>
  <button class="btn" onclick="navDiff(1)" title="Next diff">▶</button>
  <label style="font-size:12px;color:var(--text2);display:flex;align-items:center;gap:4px;margin-left:8px;cursor:pointer">
    <input type="checkbox" id="focusCb" onchange="toggleFocus(this.checked)"> Focus on changes
  </label>
  <label style="font-size:12px;color:var(--text2);display:flex;align-items:center;gap:4px;margin-left:4px;cursor:pointer">
    <input type="checkbox" id="collapseCb" onchange="toggleCollapse(this.checked)"> Collapse unchanged
  </label>
  <button class="btn" id="syncBtn" onclick="toggleSyncScroll()" title="Sync scroll between PDF and Text panels">⇔ Sync</button>
  <span class="stat" id="statBar">Ready</span>
  <button class="btn" id="dlBtn" onclick="downloadText()">⬇️ Download</button>
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
var wordMap=__WORDMAP_JS__;
var S=1; // will be set after PDF.js renders
var originalLines=__LINES_JS__;
var changeLines=__CHANGELINES_JS__;
var diffsCount=0;
var _pageCount=__PAGE_COUNT__;

// Build PDF pages — containers first (sync), then render with PDF.js (async)
var wrap=document.getElementById('pdfWrap'),containers=[];
for(var _pi=0;_pi<_pageCount;_pi++){
  var c=document.createElement('div');c.className='page-ctr';c.id='pg_'+_pi;
  var l=document.createElement('div');l.className='page-lbl';l.textContent='Page '+(_pi+1);
  var cv=document.createElement('canvas');cv.id='cv_'+_pi;cv.style.width='100%';cv.style.display='block';
  var tld=document.createElement('div');tld.className='pdf-text-layer';tld.id='tl_'+_pi;
  c.appendChild(l);c.appendChild(cv);c.appendChild(tld);
  wrap.appendChild(c);containers.push(c);
}

// PDF.js async rendering
var _pdfB64='__PDF_B64__';
pdfjsLib.GlobalWorkerOptions.workerSrc='https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/build/pdf.worker.min.js';
var _renderScale=Math.max(2,window.devicePixelRatio||2);
S=_renderScale;

(function renderPdfPages(){
  var raw=atob(_pdfB64);
  var arr=new Uint8Array(raw.length);
  for(var i=0;i<raw.length;i++)arr[i]=raw.charCodeAt(i);
  pdfjsLib.getDocument({data:arr}).promise.then(function(pdfDoc){
    var promises=[];
    for(var p=0;p<pdfDoc.numPages;p++){
      (function(idx){
        promises.push(pdfDoc.getPage(idx+1).then(function(page){
          var vp=page.getViewport({scale:_renderScale});
          var cv=document.getElementById('cv_'+idx);
          cv.width=vp.width;cv.height=vp.height;
          return page.render({canvasContext:cv.getContext('2d'),viewport:vp}).promise.then(function(){
            return page.getTextContent();
          }).then(function(tc){
            var tld=document.getElementById('tl_'+idx);
            tld.style.width=vp.width+'px';tld.style.height=vp.height+'px';
            tld.style.top=cv.offsetTop+'px';
            // Scale text layer from PDF-point size down to displayed canvas size
            var scaleFactor=cv.clientWidth/vp.width;
            tld.style.transform='scale('+scaleFactor+')';
            pdfjsLib.renderTextLayer({textContent:tc,container:tld,viewport:vp});
          });
        }));
      })(p);
    }
    Promise.all(promises).then(function(){
      // All pages rendered — build overlays
      syncTextLayers();
      rebuildPdfOverlays();
      _buildLinePdfPos();
    });
  }).catch(function(err){
    document.getElementById('statBar').textContent='\u26a0\ufe0f PDF render failed: '+err.message;
    document.getElementById('statBar').style.color='#cf222e';
  });
})();

// Sync text layer scale+position to match canvas display size
function syncTextLayers(){
  containers.forEach(function(ctr,idx){
    var cv=ctr.querySelector('canvas');if(!cv)return;
    var tld=ctr.querySelector('.pdf-text-layer');if(!tld)return;
    tld.style.top=cv.offsetTop+'px';
    // Set the text layer to the canvas's RENDERED pixel dimensions,
    // then scale it down to the DISPLAYED size.  This keeps overflow:hidden
    // clipping accurate and prevents bleed across page boundaries.
    tld.style.width=cv.width+'px';
    tld.style.height=cv.height+'px';
    var scaleFactor=cv.clientWidth/cv.width;
    tld.style.transform='scale('+scaleFactor+')';
  });
}

// Recompute and mark changed lines
function refreshChangeMeta(){
  changeLines=[];
  document.querySelectorAll('.tl').forEach(function(tl){
    tl.classList.remove('has-change');
    var li=parseInt(tl.dataset.li||'-1');
    var txt=(tl.querySelector('.lc')?tl.querySelector('.lc').textContent:'');
    if(txt.indexOf('[A>')>=0||txt.indexOf('[D>')>=0||txt.indexOf('<A]')>=0||txt.indexOf('<D]')>=0){
      tl.classList.add('has-change');
      if(li>=0)changeLines.push(li);
    }
  });
  diffsCount=changeLines.length;
}
refreshChangeMeta();

// Pause sync scroll for a duration (prevents sync from fighting with
// programmatic scrolls like highlight-to-PDF, diff nav, page nav, etc.)
var _syncPauseTimer=null;
function pauseSyncScroll(ms){
  _syncScrolling=true;
  if(_syncPauseTimer)clearTimeout(_syncPauseTimer);
  _syncPauseTimer=setTimeout(function(){_syncScrolling=false;},ms||600);
}

// Safe scroll — only scrolls the given panel, not the parent iframe or sibling panels.
function scrollElIntoPanel(el, panel, block){
  if(!el||!panel) return;
  pauseSyncScroll(800);
  var elRect=el.getBoundingClientRect();
  var panelRect=panel.getBoundingClientRect();
  var offset;
  if(block==='start'){
    offset=elRect.top - panelRect.top + panel.scrollTop;
  } else { // 'center'
    offset=elRect.top - panelRect.top + panel.scrollTop - (panel.clientHeight/2) + (el.clientHeight/2);
  }
  panel.scrollTo({top:Math.max(0,offset),behavior:'smooth'});
}

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
    tl.classList.add('active');
    scrollElIntoPanel(tl, document.getElementById('textPanel'), 'center');
    // Also highlight on PDF
    tl.querySelectorAll('.tw').forEach(function(t){
      t.classList.add('sel');
      var k=t.dataset.li+'_'+t.dataset.wi,wd=wordMap[k];
      if(wd)hlWord(wd,t.dataset.li);
    });
    flushHL();
  }
  document.getElementById('statBar').textContent='Diff '+(currentDiffIdx+1)+' / '+changeLines.length+' (Line '+(li+1)+')';
}

// Page nav
var totalPages=_pageCount;
document.getElementById('pageLbl').textContent='1 / '+totalPages;
var pdfPanel=document.getElementById('pdfPanel');
pdfPanel.addEventListener('scroll',function(){
  var scrollTop=pdfPanel.scrollTop;
  var best=0;
  containers.forEach(function(c,i){if(c.offsetTop-60<=scrollTop)best=i;});
  document.getElementById('pageLbl').textContent=(best+1)+' / '+totalPages;
});
function navPage(dir){
  var cur=parseInt(document.getElementById('pageLbl').textContent.split('/')[0].trim())-1;
  var next=Math.max(0,Math.min(totalPages-1,cur+dir));
  scrollElIntoPanel(containers[next], document.getElementById('pdfPanel'), 'start');
}

// ── Bidirectional sync scroll ───────────────────────────────────────
var syncScrollEnabled=false;
var _syncScrolling=false; // guard to prevent feedback loop
function toggleSyncScroll(){
  syncScrollEnabled=!syncScrollEnabled;
  var btn=document.getElementById('syncBtn');
  btn.classList.toggle('on',syncScrollEnabled);
  // Rebuild position map when enabling (in case it wasn't built yet)
  if(syncScrollEnabled && Object.keys(_linePdfPos).length===0) _buildLinePdfPos();
  var n=Object.keys(_linePdfPos).length;
  document.getElementById('statBar').textContent=
    syncScrollEnabled?'⇔ Sync scroll ON ('+n+' lines mapped)':'⇔ Sync scroll OFF';
}

// Build line→page/y lookup for sync: for each text line that has a
// wordMap entry, record its PDF page and average y position.
var _linePdfPos={};  // li → {page, y}
function _buildLinePdfPos(){
  _linePdfPos={};
  Object.keys(lg).forEach(function(li){
    var ws=lg[li];
    if(!ws||!ws.length)return;
    var sumY=0;
    ws.forEach(function(w){sumY+=w.y0;});
    _linePdfPos[li]={page:ws[0].page, y:sumY/ws.length};
  });
}

// Given a scroll fraction (0..1) of one panel, find the corresponding
// line index using the wordMap data.
function _pdfScrollToLine(){
  // Which page is visible at the top of the PDF panel?
  var pp=document.getElementById('pdfPanel');
  var scrollTop=pp.scrollTop;
  var bestPage=0;
  containers.forEach(function(c,i){if(c.offsetTop-60<=scrollTop)bestPage=i;});
  // How far into that page?
  var ctr=containers[bestPage];
  var cv=ctr?ctr.querySelector('canvas'):null;
  if(!cv||!cv.width)return -1;
  var ds=cv.clientWidth/cv.width;
  var yInPage=(scrollTop-ctr.offsetTop-(cv?cv.offsetTop:0))/(ds*S);
  if(yInPage<0)yInPage=0;
  // Find closest line on this page
  var bestLi=-1,bestDist=1e9;
  Object.keys(_linePdfPos).forEach(function(li){
    var lp=_linePdfPos[li];
    if(lp.page===bestPage){
      var d=Math.abs(lp.y-yInPage);
      if(d<bestDist){bestDist=d;bestLi=parseInt(li);}
    }
  });
  return bestLi;
}

function _lineToTextScrollTop(li){
  var tl=document.querySelector('.tl[data-li="'+li+'"]');
  if(!tl)return -1;
  var tp=document.getElementById('textPanel');
  // Use getBoundingClientRect for accurate offset regardless of nesting
  var tlRect=tl.getBoundingClientRect();
  var tpRect=tp.getBoundingClientRect();
  return tlRect.top - tpRect.top + tp.scrollTop;
}

function _lineToPdfScrollTop(li){
  var lp=_linePdfPos[li];
  if(!lp)return -1;
  var ctr=containers[lp.page];
  if(!ctr)return -1;
  var cv=ctr.querySelector('canvas');
  if(!cv||!cv.width)return -1;
  var ds=cv.clientWidth/cv.width;
  return ctr.offsetTop+cv.offsetTop+lp.y*S*ds;
}

function _textScrollToLine(){
  var tp=document.getElementById('textPanel');
  var tpRect=tp.getBoundingClientRect();
  var tls=tp.querySelectorAll('.tl');
  var bestLi=-1;
  for(var i=0;i<tls.length;i++){
    var r=tls[i].getBoundingClientRect();
    // Find the last line whose top is at or above the panel's top edge + small offset
    if(r.top <= tpRect.top+50) bestLi=parseInt(tls[i].dataset.li);
    else break;
  }
  return bestLi;
}

// Attach listeners
var _syncDebounceP=null,_syncDebounceT=null;
document.getElementById('pdfPanel').addEventListener('scroll',function(){
  if(!syncScrollEnabled||_syncScrolling)return;
  if(_syncDebounceP)clearTimeout(_syncDebounceP);
  _syncDebounceP=setTimeout(function(){
    var li=_pdfScrollToLine();
    if(li<0)return;
    var top=_lineToTextScrollTop(li);
    if(top<0)return;
    _syncScrolling=true;
    document.getElementById('textPanel').scrollTo({top:Math.max(0,top),behavior:'auto'});
    requestAnimationFrame(function(){setTimeout(function(){_syncScrolling=false;},60);});
  },16);
},{passive:true});

document.getElementById('textPanel').addEventListener('scroll',function(){
  if(!syncScrollEnabled||_syncScrolling)return;
  if(_syncDebounceT)clearTimeout(_syncDebounceT);
  _syncDebounceT=setTimeout(function(){
    var li=_textScrollToLine();
    if(li<0)return;
    var top=_lineToPdfScrollTop(li);
    if(top<0)return;
    _syncScrolling=true;
    document.getElementById('pdfPanel').scrollTo({top:Math.max(0,top-40),behavior:'auto'});
    requestAnimationFrame(function(){setTimeout(function(){_syncScrolling=false;},60);});
  },16);
},{passive:true});

// Zoom
var zoomLevel=0; // 0=fit, else percentage steps
var zoomSteps=[50,75,100,125,150,200];
function zoomPdf(dir){
  var cvs=document.querySelectorAll('.page-ctr canvas');
  var allCtrs=document.querySelectorAll('.page-ctr');
  var panel=document.getElementById('pdfPanel');
  var panelInner = panel ? Math.max(100, panel.clientWidth - 20) : 800;

  if(dir===0){
    zoomLevel=0;
    allCtrs.forEach(function(c){
      c.style.width='';
      c.style.maxWidth='100%';
    });
    cvs.forEach(function(cv){cv.style.width='100%';});
    document.getElementById('zoomLbl').textContent='Fit';
  } else {
    if(zoomLevel===0)zoomLevel=100;
    var idx=zoomSteps.indexOf(zoomLevel);
    if(idx===-1)idx=2;
    idx=Math.max(0,Math.min(zoomSteps.length-1,idx+dir));
    zoomLevel=zoomSteps[idx];
    var targetPx = Math.round(panelInner * zoomLevel / 100);
    allCtrs.forEach(function(c){
      c.style.width = targetPx + 'px';
      c.style.maxWidth = 'none';
    });
    cvs.forEach(function(cv){cv.style.width='100%';});
    document.getElementById('zoomLbl').textContent=zoomLevel+'%';
  }
  setTimeout(function(){clearHL();syncTextLayers();rebuildPdfOverlays();},200);
}

// Focus on changes
function toggleFocus(on){
  document.getElementById('textWrap').classList.toggle('focus',on);
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

// Show mapping diagnostic in stat bar
(function(){
  var totalTw=document.querySelectorAll('.tw').length;
  var mappedKeys=Object.keys(wordMap).length;
  var mappedLines=Object.keys(lg).length;
  if(totalTw>0 && mappedKeys===0){
    document.getElementById('statBar').textContent='⚠️ No word mappings — PDF↔Text linking unavailable';
    document.getElementById('statBar').style.color='#cf222e';
  } else if(mappedKeys>0){
    document.getElementById('statBar').textContent=
        'PDF → Line '+mappedLines+' lines, '+mappedKeys+' words mapped';
  }
})();

function buildZones(){
  document.querySelectorAll('.cz').forEach(function(z){z.remove();});
  Object.keys(lg).forEach(function(li){
    var ws=lg[li],pg=ws[0].page,ctr=containers[pg],cv=ctr.querySelector('canvas');
    if(!cv||!cv.width)return;
    var ds=cv.clientWidth/cv.width;
    var x0=1e9,y0=1e9,x1=0,y1=0;
    ws.forEach(function(w){if(w.x0<x0)x0=w.x0;if(w.y0<y0)y0=w.y0;if(w.x1>x1)x1=w.x1;if(w.y1>y1)y1=w.y1;});
    var z=document.createElement('div');z.className='cz';
    z.style.left=(x0*S*ds)+'px';z.style.top=(y0*S*ds+cv.offsetTop)+'px';
    z.style.width=((x1-x0)*S*ds)+'px';z.style.height=((y1-y0)*S*ds)+'px';
    z.onclick=function(){clickPdfLine(parseInt(li));};
    ctr.appendChild(z);
  });
}
function rebuildPdfOverlays(){
  buildZones();
}
// PDF.js renders text layers once; on resize we just re-sync transforms
window.addEventListener('resize',function(){clearHL();syncTextLayers();rebuildPdfOverlays();});

// Highlights
var hlEls=[];
function clearHL(){
  document.querySelectorAll('.tw.sel').forEach(function(e){e.classList.remove('sel');});
  document.querySelectorAll('.tl.active').forEach(function(e){e.classList.remove('active');});
  // (PDF.js text layer selection is native — no custom psel class needed)
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
    // Key: page_textline_pdflineY (round y0 to nearest 3pt to group same PDF line)
    var yKey=Math.round(wd.y0/3)*3;
    var gk=wd.page+'_'+wd.lk+'_'+yKey;
    if(!groups[gk])groups[gk]={x0:wd.x0,y0:wd.y0,x1:wd.x1,y1:wd.y1,page:wd.page};
    else{
      var g=groups[gk];
      g.x0=Math.min(g.x0,wd.x0);g.y0=Math.min(g.y0,wd.y0);
      g.x1=Math.max(g.x1,wd.x1);g.y1=Math.max(g.y1,wd.y1);
    }
  });
  // Draw merged rects
  Object.keys(groups).forEach(function(gk){
    var r=groups[gk];
    var ctr=containers[r.page],cv=ctr.querySelector('canvas');if(!cv)return;
    var ds=cv.clientWidth/cv.width;
    var el=document.createElement('div');el.className='hl-overlay';
    el.style.left=(r.x0*S*ds)+'px';el.style.top=(r.y0*S*ds+cv.offsetTop)+'px';
    el.style.width=((r.x1-r.x0)*S*ds)+'px';el.style.height=((r.y1-r.y0)*S*ds)+'px';
    ctr.appendChild(el);hlEls.push(el);
  });
  hlQueue=[];
}
function scrollPdf(wd){
  var ctr=containers[wd.page],cv=ctr.querySelector('canvas');if(!cv)return;
  pauseSyncScroll(800);
  var ds=cv.clientWidth/cv.width;
  var targetY=ctr.offsetTop+wd.y0*S*ds+cv.offsetTop;
  var pdfPanel=document.getElementById('pdfPanel');
  // Centre the target in the panel viewport
  pdfPanel.scrollTo({top:Math.max(0,targetY - pdfPanel.clientHeight/3),behavior:'smooth'});
}

// Clear highlights when clicking empty area
document.getElementById('pdfPanel').addEventListener('mousedown',function(e){
  pauseSyncScroll(1000);
  if(!e.target.closest('.cz') && !e.target.closest('.pdf-text-layer'))clearHL();
});

// ── PDF text-layer selection → bidirectional highlight ──────────────
// PDF.js native text layer provides real text selection.  On mouseup we
// match the selected text back to our wordMap for bidirectional linking.
document.getElementById('pdfPanel').addEventListener('mouseup',function(e){
  if(!e.target.closest('.pdf-text-layer'))return;
  setTimeout(function(){
    var sel=window.getSelection();
    if(!sel||!sel.rangeCount)return;
    clearHL();
    if(sel.isCollapsed){
      // Single click — find the closest wordMap entry by position
      var sp=e.target.closest('.pdf-text-layer span');
      if(sp){
        var pgIdx=_findPageOfTextLayerSpan(sp);
        var bestLi=_findClosestLine(sp,pgIdx);
        if(bestLi>=0) clickPdfLine(bestLi);
      }
      return;
    }
    // Range selection — find PDF.js text spans that intersect
    var range=sel.getRangeAt(0);
    var allSp=document.querySelectorAll('.pdf-text-layer span');
    var lineSet={},n=0,firstWd=null;
    allSp.forEach(function(sp){
      if(!range.intersectsNode(sp))return;
      var pgIdx=_findPageOfTextLayerSpan(sp);
      var bestLi=_findClosestLine(sp,pgIdx);
      if(bestLi>=0){
        lineSet[bestLi]=true;
        // Highlight matching words on PDF overlay
        var keys=Object.keys(wordMap);
        for(var ki=0;ki<keys.length;ki++){
          var k=keys[ki];
          if(parseInt(k.split('_')[0])===bestLi){
            var wd=wordMap[k];
            if(wd.page===pgIdx){hlWord(wd,''+bestLi);n++;if(!firstWd)firstWd=wd;}
          }
        }
      }
    });
    flushHL();
    // Highlight corresponding text lines
    var lineKeys=Object.keys(lineSet);
    lineKeys.forEach(function(li){
      var tl=document.querySelector('.tl[data-li="'+li+'"]');
      if(tl){
        tl.classList.add('active');
        tl.querySelectorAll('.tw').forEach(function(tw){tw.classList.add('sel');});
      }
    });
    if(lineKeys.length>0){
      var firstLi=Math.min.apply(null,lineKeys.map(Number));
      var firstTl=document.querySelector('.tl[data-li="'+firstLi+'"]');
      if(firstTl)scrollElIntoPanel(firstTl,document.getElementById('textPanel'),'center');
      document.getElementById('statBar').textContent=
        'PDF selection \u2192 '+lineKeys.length+' line(s)  (Ctrl+C to copy)';
    }
  },10);
});

// Helper: find which page index a text-layer span belongs to
function _findPageOfTextLayerSpan(sp){
  var layer=sp.closest('.pdf-text-layer');
  if(!layer)return 0;
  var ctr=layer.closest('.page-ctr');
  for(var i=0;i<containers.length;i++){if(containers[i]===ctr)return i;}
  return 0;
}

// Helper: given a PDF.js text span and page, find the closest text line via
// the wordMap by comparing the span's vertical position to mapped word y-coords
function _findClosestLine(sp,pgIdx){
  // Get span's position relative to the page in PDF point coordinates
  var ctr=containers[pgIdx];if(!ctr)return -1;
  var cv=ctr.querySelector('canvas');if(!cv||!cv.width)return -1;
  var ds=cv.clientWidth/cv.width;
  var spRect=sp.getBoundingClientRect();
  var ctrRect=cv.getBoundingClientRect();
  // Convert display pixels back to PDF points
  var pdfY=((spRect.top-ctrRect.top)/ds)/S;
  var pdfX=((spRect.left-ctrRect.left)/ds)/S;
  // Search wordMap for closest match on this page
  var bestLi=-1,bestDist=1e9;
  var keys=Object.keys(lg);
  for(var i=0;i<keys.length;i++){
    var li=keys[i],ws=lg[li];
    if(ws[0].page!==pgIdx)continue;
    for(var j=0;j<ws.length;j++){
      var dy=Math.abs(ws[j].y0-pdfY);
      if(dy<bestDist){bestDist=dy;bestLi=parseInt(li);}
    }
  }
  return bestDist<30?bestLi:-1;
}

// Native text selection → PDF highlight
document.getElementById('textWrap').addEventListener('mousedown',function(){
  pauseSyncScroll(1000);
});
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
  var tl=document.querySelector('.tl[data-li="'+li+'"]');
  if(tl){tl.classList.add('active');scrollElIntoPanel(tl, document.getElementById('textPanel'), 'center');
    tl.querySelectorAll('.tw').forEach(function(t){
      t.classList.add('sel');var k=t.dataset.li+'_'+t.dataset.wi,wd=wordMap[k];if(wd)hlWord(wd,t.dataset.li);
    });
    flushHL();
  }
  document.getElementById('statBar').textContent='PDF → Line '+(parseInt(li)+1);
}

// Always-on inline editing with auto-apply
var autoApplyTimer=null;
function scheduleAutoApply(){
  if(autoApplyTimer)clearTimeout(autoApplyTimer);
  document.getElementById('statBar').textContent='Editing…';
  autoApplyTimer=setTimeout(function(){applyInlineEdits();},700);
}

// ── Undo / Redo history ─────────────────────────────────────────────
// The browser's native Ctrl+Z doesn't work because applyInlineEdits()
// rebuilds the DOM (innerHTML) on every edit, destroying the undo stack.
// We maintain our own snapshot history of originalLines.
var undoStack=[];   // past states (newest last)
var redoStack=[];   // future states after undo
var MAX_UNDO=100;
function _linesSnapshot(){ return originalLines.map(function(l){return l;}); }
function pushUndo(){
  undoStack.push(_linesSnapshot());
  if(undoStack.length>MAX_UNDO) undoStack.shift();
  redoStack=[];  // new edit clears redo
}
function doUndo(){
  if(!undoStack.length){document.getElementById('statBar').textContent='Nothing to undo';return;}
  redoStack.push(_linesSnapshot());
  originalLines=undoStack.pop();
  rebuildEditor(true);
  document.getElementById('statBar').textContent='↩ Undo ('+undoStack.length+' left)';
}
function doRedo(){
  if(!redoStack.length){document.getElementById('statBar').textContent='Nothing to redo';return;}
  undoStack.push(_linesSnapshot());
  originalLines=redoStack.pop();
  rebuildEditor(true);
  document.getElementById('statBar').textContent='↪ Redo ('+redoStack.length+' left)';
}

function enableInlineEditing(){
  var tw=document.getElementById('textWrap');
  tw.classList.add('editing');
  // ── Single editable host on the parent ─────────────────────────────
  // Per-`.lc` contentEditable creates separate editable regions and
  // browsers refuse to drag-select across them.  Hosting on the parent
  // gives us ONE editable region → multi-line selection works.
  tw.contentEditable='true';
  tw.spellcheck=false;
  // Protect the gutter (line numbers) from being edited or selected
  tw.querySelectorAll('.ln').forEach(function(ln){
    ln.contentEditable='false';
  });
  // Strip any leftover per-`.lc` contentEditable (avoids nested-host quirks)
  tw.querySelectorAll('.lc').forEach(function(lc){
    if(lc.hasAttribute('contenteditable')) lc.removeAttribute('contenteditable');
    lc.spellcheck=false;
  });
  // Bind listeners once, on the parent (re-binds are guarded by flags)
  if(!tw._inputBound){
    tw.addEventListener('input',scheduleAutoApply);
    tw._inputBound=true;
  }
  // ── Keyboard interception ─────────────────────────────────────────
  // With parent-level contentEditable the browser's default Enter
  // behaviour creates a stray <div> that breaks our `.tl` structure.
  // Intercept Enter (split current line) and Backspace-at-start
  // (merge with previous) so editing stays consistent with our
  // line-based data model.
  if(!tw._keyBound){
    tw.addEventListener('keydown',function(e){
      // Undo — Ctrl+Z
      if((e.ctrlKey||e.metaKey) && !e.shiftKey && e.key==='z'){
        e.preventDefault(); doUndo(); return;
      }
      // Redo — Ctrl+Y or Ctrl+Shift+Z
      if((e.ctrlKey||e.metaKey) && (e.key==='y' || (e.shiftKey && e.key==='Z') || (e.shiftKey && e.key==='z'))){
        e.preventDefault(); doRedo(); return;
      }
      // Enter — split line at caret
      if(e.key==='Enter' && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey){
        e.preventDefault();
        if(typeof splitAtCursor==='function') splitAtCursor();
        return;
      }
      // Backspace at offset 0 of a line — merge with previous
      if(e.key==='Backspace' && !e.ctrlKey && !e.metaKey && !e.altKey){
        var sel=window.getSelection();
        if(!sel||!sel.rangeCount||!sel.isCollapsed) return; // let default delete a non-collapsed selection
        var range=sel.getRangeAt(0);
        var lc=range.startContainer;
        while(lc && !(lc.classList && lc.classList.contains('lc'))) lc=lc.parentNode;
        if(!lc) return;
        var off=getTextOffset(lc, range.startContainer, range.startOffset);
        if(off===0){
          // At line start — intercept and merge with previous
          var tl=lc.closest('.tl'); if(!tl) return;
          var li=parseInt(tl.dataset.li);
          if(li>0){
            e.preventDefault();
            // Set activeEditorLine so mergeWithPrevious knows which line
            if(typeof setActiveLine==='function') setActiveLine(tl);
            if(typeof mergeWithPrevious==='function') mergeWithPrevious();
          }
        }
      }
    });
    tw._keyBound=true;
  }
  // ── Paste interception ────────────────────────────────────────────
  // Insert pasted content as PLAIN TEXT only (strip HTML / rich
  // formatting) so it doesn't inject stray DOM nodes into our
  // line-based structure.  Triggers auto-apply so edits are saved.
  if(!tw._pasteBound){
    tw.addEventListener('paste',function(e){
      e.preventDefault();
      var text=(e.clipboardData||window.clipboardData).getData('text/plain');
      if(!text)return;
      // Insert at caret position
      var sel=window.getSelection();
      if(!sel||!sel.rangeCount)return;
      var range=sel.getRangeAt(0);
      range.deleteContents();
      range.insertNode(document.createTextNode(text));
      // Move caret to end of inserted text
      range.collapse(false);
      sel.removeAllRanges();
      sel.addRange(range);
      scheduleAutoApply();
    });
    tw._pasteBound=true;
  }
  // ── Selection-change tracking for active line highlight ───────────
  // With parent contentEditable the per-line `focusin` doesn't fire on
  // every caret move, so we listen to selectionchange globally and
  // update activeEditorLine based on where the caret/anchor lives.
  if(!document._selBound){
    document.addEventListener('selectionchange',function(){
      var sel=window.getSelection();
      if(!sel||!sel.rangeCount) return;
      var n=sel.getRangeAt(0).startContainer;
      var tl=null;
      while(n){
        if(n.classList && n.classList.contains('tl')){tl=n;break;}
        n=n.parentNode;
      }
      if(tl && typeof setActiveLine==='function') setActiveLine(tl);
    });
    document._selBound=true;
  }
}
function applyInlineEdits(){
  pushUndo();
  var tw=document.getElementById('textWrap');

  // ── Save cursor position before rebuild ──────────────────────────
  var savedLi=-1, savedOff=0;
  var sel=window.getSelection();
  if(sel && sel.rangeCount && sel.isCollapsed){
    var range=sel.getRangeAt(0);
    var lc=range.startContainer;
    while(lc && !(lc.classList && lc.classList.contains('lc'))) lc=lc.parentNode;
    if(lc){
      var tl=lc.closest('.tl');
      if(tl){
        savedLi=parseInt(tl.dataset.li);
        savedOff=getTextOffset(lc, range.startContainer, range.startOffset);
      }
    }
  }

  var panel=document.getElementById('textPanel');
  var scrollTop=panel?panel.scrollTop:0;

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

  // ── Restore cursor position after rebuild ────────────────────────
  if(panel)panel.scrollTop=scrollTop;
  if(savedLi>=0 && savedLi<originalLines.length){
    var restoredTl=document.querySelector('.tl[data-li="'+savedLi+'"]');
    if(restoredTl){
      setActiveLine(restoredTl);
      var restoredLc=restoredTl.querySelector('.lc');
      if(restoredLc){
        restoredLc.focus({preventScroll:true});
        placeCursorAtOffset(restoredLc, Math.min(savedOff, (restoredLc.textContent||'').length));
      }
    }
  }

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

// Helper: find the `.tl` (line wrapper) that owns a given selection
// endpoint container.  Returns {tl, lc, li, off} or null.
//
// `isEnd` controls fallback behaviour: when the endpoint is NOT inside
// a text node (e.g. the user dragged out of the .lc, into the gutter,
// or released on empty whitespace at end-of-line) we snap to either
// the START of the nearest line (isEnd=false) or the END (isEnd=true).
function _resolveSelectionEndpoint(container, offset, isEnd){
  // Case 1 + 2: walk up looking for an enclosing .lc
  var lc=container;
  while(lc && lc.nodeType && !(lc.classList && lc.classList.contains('lc'))){
    lc=lc.parentNode;
  }
  if(lc && lc.classList && lc.classList.contains('lc')){
    var tl=lc.closest('.tl');
    if(!tl) return null;
    var li=parseInt(tl.dataset.li);
    if(isNaN(li)) return null;
    var off;
    if(container === lc){
      // Endpoint IS the .lc element (no text node) — snap to start/end of line.
      // lc.textContent is the raw text (including tag markers like [A>),
      // matching the indexing used for originalLines[li].
      off = isEnd ? (lc.textContent || '').length : 0;
    } else {
      off = getTextOffset(lc, container, offset);
    }
    return {tl:tl, lc:lc, li:li, off:off};
  }
  // Case 3: endpoint is OUTSIDE any .lc (gutter .ln, wrapper .tl, body, etc.)
  // — walk up to the nearest .tl and snap.
  var tl=container;
  while(tl && tl.nodeType && !(tl.classList && tl.classList.contains('tl'))){
    tl=tl.parentNode;
  }
  if(!tl || !tl.classList || !tl.classList.contains('tl')) return null;
  lc = tl.querySelector('.lc');
  if(!lc) return null;
  var li=parseInt(tl.dataset.li);
  if(isNaN(li)) return null;
  var off = isEnd ? (lc.textContent || '').length : 0;
  return {tl:tl, lc:lc, li:li, off:off};
}

function wrapTag(type){
  var sel=window.getSelection();
  if(!sel||sel.isCollapsed||!sel.rangeCount){
    document.getElementById('statBar').textContent='Select text first, then click '+(type==='a'?'[A> Wrap':'[D> Wrap');
    return;
  }
  var range=sel.getRangeAt(0);
  var startEP=_resolveSelectionEndpoint(range.startContainer, range.startOffset, false);
  var endEP=_resolveSelectionEndpoint(range.endContainer, range.endOffset, true);
  if(!startEP || !endEP){
    document.getElementById('statBar').textContent='Select text inside a line';
    return;
  }

  // Ensure start <= end (selection might be backwards)
  if(startEP.li > endEP.li || (startEP.li === endEP.li && startEP.off > endEP.off)){
    var tmp=startEP; startEP=endEP; endEP=tmp;
  }

  // ── Single-line path: full existing behaviour (toggle / swap / wrap) ──
  if(startEP.li === endEP.li){
    _wrapTagSingleLine(type, startEP.li, startEP.off, endEP.off);
    return;
  }

  // ── Multi-line path: span selection across N lines ───────────────────
  if(startEP.off === endEP.off && endEP.li - startEP.li === 0){
    document.getElementById('statBar').textContent='Select some text first';
    return;
  }
  var openMark = (type==='a') ? '[A>' : '[D>';
  var closeMark = (type==='a') ? '<A]' : '<D]';

  // ── Toggle/Expand detection PASS 1: markers INSIDE the selection ─────
  // The selection contains a balanced set of `[A>`/`<A]` markers.
  // Two sub-cases distinguish what the user wants:
  //
  //   STRIP  — selection ≈ the wrap (no plain text outside the markers).
  //            User is toggling OFF.  Remove the inner markers.
  //
  //   EXPAND — selection extends BEYOND the markers (plain text exists
  //            before the first marker and/or after the last marker).
  //            User wants the wrap to grow to cover the whole selection,
  //            e.g. select whole line including `[A>;or<A]` at the end
  //            → line becomes `[A>(4) Falsely... officer; or<A]`.
  //            Strip the inner markers AND emit a new outer wrap.
  var insideMarkers = _scanMarkersInRange(type, startEP.li, startEP.off, endEP.li, endEP.off);
  if(insideMarkers.length > 0){
    var nOpenIn = 0, nCloseIn = 0;
    insideMarkers.forEach(function(m){if(m.isOpen)nOpenIn++;else nCloseIn++;});
    if(nOpenIn > 0 && nOpenIn === nCloseIn){
      // Detect "extends beyond": is there plain text in the selection
      // BEFORE the first marker or AFTER the last marker?
      var firstMk = insideMarkers[0];
      var lastMk  = insideMarkers[insideMarkers.length - 1];
      var extendsBefore =
          (startEP.li < firstMk.li) ||
          (startEP.li === firstMk.li && startEP.off < firstMk.off);
      var extendsAfter =
          (endEP.li > lastMk.li) ||
          (endEP.li === lastMk.li && endEP.off > lastMk.off + 3);

      if(extendsBefore || extendsAfter){
        // ── EXPAND ──────────────────────────────────────────────────
        // Count markers on the END line whose position is < endEP.off —
        // each strip there shifts endEP.off LEFT by 3.  (Markers on the
        // START line are all >= startEP.off because _scanMarkersInRange
        // filtered to the selection, so startEP.off itself doesn't shift.)
        var markersBeforeEndOff = 0;
        for(var k = 0; k < insideMarkers.length; k++){
          var mk = insideMarkers[k];
          if(mk.li === endEP.li && mk.off < endEP.off) markersBeforeEndOff++;
        }
        // Strip all inner markers (reverse order to preserve offsets).
        for(var im = insideMarkers.length - 1; im >= 0; im--){
          var mm = insideMarkers[im];
          var tt = originalLines[mm.li] || '';
          originalLines[mm.li] = tt.substring(0, mm.off) + tt.substring(mm.off + 3);
        }
        // Compute adjusted end offset, then insert close on END line first
        // (so the open insertion on START line doesn't shift end position
        // when start === end — though we're in the multi-line path here).
        var newEndOff = endEP.off - (markersBeforeEndOff * 3);
        var lastLineText = originalLines[endEP.li] || '';
        originalLines[endEP.li] =
            lastLineText.substring(0, newEndOff) + closeMark +
            lastLineText.substring(newEndOff);
        var firstLineText = originalLines[startEP.li] || '';
        originalLines[startEP.li] =
            firstLineText.substring(0, startEP.off) + openMark +
            firstLineText.substring(startEP.off);
        rebuildEditor();
        var nLinesE = endEP.li - startEP.li + 1;
        document.getElementById('statBar').textContent =
            openMark+' wrap expanded ('+nOpenIn+' inner pair'+(nOpenIn>1?'s':'')+
            ' merged, now spans '+nLinesE+' line'+(nLinesE>1?'s':'')+')';
        return;
      }

      // ── STRIP (snug-fit toggle off) ────────────────────────────────
      for(var im2 = insideMarkers.length - 1; im2 >= 0; im2--){
        var mm2 = insideMarkers[im2];
        var tt2 = originalLines[mm2.li] || '';
        originalLines[mm2.li] = tt2.substring(0, mm2.off) + tt2.substring(mm2.off + 3);
      }
      rebuildEditor();
      document.getElementById('statBar').textContent =
          openMark+' wrap removed ('+nOpenIn+' pair'+(nOpenIn>1?'s':'')+
          ' stripped from selection)';
      return;
    }
  }

  // ── Toggle-OFF detection PASS 2: SURROUNDING wrap ────────────────────
  // The selection sits INSIDE a wider wrap (markers outside endpoints).
  // Handles: snug-fit selection, sub-selection of wrapped region, etc.
  var existing = _findSurroundingTagWrap(type, startEP.li, startEP.off, endEP.li, endEP.off);
  if(existing){
    // TOGGLE OFF — splice out the opener and closer.
    // Process in REVERSE order (closer first) so opener offsets stay valid.
    if(existing.closeLi === existing.openLi){
      // Both on same line — single splice
      var t = originalLines[existing.closeLi];
      originalLines[existing.closeLi] =
          t.substring(0, existing.openOff) +
          t.substring(existing.openOff + 3, existing.closeOff) +
          t.substring(existing.closeOff + 3);
    } else {
      // Closer first (so opener's line index stays valid)
      var closeText = originalLines[existing.closeLi];
      originalLines[existing.closeLi] =
          closeText.substring(0, existing.closeOff) +
          closeText.substring(existing.closeOff + 3);
      var openText = originalLines[existing.openLi];
      originalLines[existing.openLi] =
          openText.substring(0, existing.openOff) +
          openText.substring(existing.openOff + 3);
    }
    rebuildEditor();
    var nLinesR = existing.closeLi - existing.openLi + 1;
    document.getElementById('statBar').textContent =
        openMark+' wrap removed ('+nLinesR+' line'+(nLinesR>1?'s':'')+
        ', '+(existing.openLi+1)+'\u2013'+(existing.closeLi+1)+')';
    return;
  }

  // ── PASS 3: SPLIT or STRIP when selection relates to an OPPOSITE wrap ─
  // _splitOppositeWrap returns:
  //   'split' → surrounding opp wrap was split: [A>before<A][D>sel<D][A>after<A]
  //   'strip' → snug opp wrap inside a same-type outer wrap; just remove
  //             the inner opp markers (text is already in the outer wrap).
  //   false   → not applicable; fall through to default WRAP below.
  var splitRes = _splitOppositeWrap(type, startEP.li, startEP.off, endEP.li, endEP.off);
  if(splitRes){
    rebuildEditor();
    var oppOpenS = (type==='a') ? '[D>' : '[A>';
    var nLinesS = endEP.li - startEP.li + 1;
    if(splitRes === 'strip'){
      document.getElementById('statBar').textContent =
          oppOpenS+' wrap removed (text already inside outer '+openMark+' wrap)';
    } else {
      document.getElementById('statBar').textContent =
          openMark+' tag added (split surrounding '+oppOpenS+' wrap, '+
          nLinesS+' line'+(nLinesS>1?'s':'')+')';
    }
    return;
  }

  // Otherwise: WRAP across lines
  // Strategy: emit ONE tag pair spanning all affected lines.
  //   • On the FIRST line:  insert [A> (or [D>) at startEP.off
  //   • On the LAST line:   insert <A] (or <D]) at endEP.off
  //   • Intermediate lines: left unchanged — they sit inside the open tag
  var firstLineText = originalLines[startEP.li] || '';
  originalLines[startEP.li] =
      firstLineText.substring(0, startEP.off) + openMark +
      firstLineText.substring(startEP.off);
  // Re-read last line (in case startEP.li === endEP.li, though we branched above)
  var lastLineText = originalLines[endEP.li];
  originalLines[endEP.li] =
      lastLineText.substring(0, endEP.off) + closeMark +
      lastLineText.substring(endEP.off);
  rebuildEditor();
  var nLines2 = endEP.li - startEP.li + 1;
  document.getElementById('statBar').textContent =
      openMark+' tag added across '+nLines2+' lines ('+(startEP.li+1)+'\u2013'+(endEP.li+1)+')';
}

// ── Helper: split an enclosing opposite-type wrap around a new tag ─────
// When the user adds a tag of one type (e.g. [D>) while the selection
// sits inside an existing wrap of the OPPOSITE type (e.g. [A>...<A]),
// we DON'T want to nest the new tag inside.  Instead, close the outer
// wrap just before the selection, emit the new tag pair as a sibling,
// then re-open the outer wrap just after.  Net transform:
//   [A>before<A][D>selected<D][A>after<A]
//
// Returns:
//   'split' — surrounding opp wrap was split into siblings around the
//             new tag (the main case described above).
//   'strip' — selection is a snug-fit opp wrap that itself sits inside
//             a SAME-type outer wrap (e.g. clicking [A> on `[D>or<D]`
//             nested inside `[A>...<A]`).  The inner opp wrap is
//             contradictory, so we just remove the inner markers;
//             the outer wrap already marks the text correctly.
//   false  — not applicable (no surrounding opp wrap, OR snug-fit
//             without an outer same-type wrap → let caller's SWAP run).
function _splitOppositeWrap(newType, startLi, startOff, endLi, endOff){
  var oppType = (newType === 'a') ? 'd' : 'a';
  var opp = _findSurroundingTagWrap(oppType, startLi, startOff, endLi, endOff);
  if(!opp) return false;

  // Snug-fit detection: opp opener ends exactly at selection start AND
  // opp closer starts exactly at selection end, all on the same line(s).
  var isSnug =
      (opp.openLi === startLi) &&
      (opp.openOff + 3 === startOff) &&
      (opp.closeLi === endLi) &&
      (opp.closeOff === endOff);

  // ── Snug-fit branch ──────────────────────────────────────────
  // The selection exactly matches the contents of an opposite-type wrap.
  // Two sub-cases:
  //   (a) NO outer same-type wrap → return false so the caller's SWAP
  //       D↔A logic runs (e.g. plain `[D>or<D]` → `[A>or<A]`).
  //   (b) Outer SAME-type wrap exists (e.g. `[A>...[D>or<D]...<A]` and
  //       user clicked [A>) → the inner opposite wrap is contradictory
  //       (text marked as both added AND deleted).  The SWAP would just
  //       create a nested duplicate wrap (`[A>...[A>or<A]...<A]`).
  //       Instead: STRIP the inner opposite markers — the text is now
  //       plain inside the outer wrap, which already marks it correctly.
  if(isSnug){
    // Check for outer SAME-type wrap around the snug opp wrap.
    // We probe OUTSIDE the opp's open/close so the surrounding scan
    // doesn't itself see the snug opp wrap.
    var outerProbeStart = opp.openOff;        // position of `[D>` (just before)
    var outerProbeEnd   = opp.closeOff + 3;   // position right after `<D]`
    var outerSame = _findSurroundingTagWrap(
        newType, opp.openLi, outerProbeStart, opp.closeLi, outerProbeEnd);
    if(!outerSame) return false;  // sub-case (a) — let SWAP run

    // Sub-case (b): strip the inner opp wrap (close first to preserve
    // opener offset on the same line).
    if(opp.closeLi === opp.openLi){
      var ts = originalLines[opp.openLi] || '';
      originalLines[opp.openLi] =
          ts.substring(0, opp.openOff) +
          ts.substring(opp.openOff + 3, opp.closeOff) +
          ts.substring(opp.closeOff + 3);
    } else {
      var closeT = originalLines[opp.closeLi] || '';
      originalLines[opp.closeLi] =
          closeT.substring(0, opp.closeOff) +
          closeT.substring(opp.closeOff + 3);
      var openT = originalLines[opp.openLi] || '';
      originalLines[opp.openLi] =
          openT.substring(0, opp.openOff) +
          openT.substring(opp.openOff + 3);
    }
    return 'strip';
  }

  // ── Non-snug branch: split the surrounding opp wrap ───────────────
  var oppOpen  = (oppType === 'a') ? '[A>' : '[D>';
  var oppClose = (oppType === 'a') ? '<A]' : '<D]';
  var newOpen  = (newType === 'a') ? '[A>' : '[D>';
  var newClose = (newType === 'a') ? '<A]' : '<D]';

  // Insert at the END first so the START insert offsets stay valid
  // (especially critical when start and end are on the SAME line).
  if(startLi === endLi){
    var t = originalLines[startLi] || '';
    originalLines[startLi] =
        t.substring(0, startOff) + oppClose + newOpen +
        t.substring(startOff, endOff) + newClose + oppOpen +
        t.substring(endOff);
  } else {
    var endText = originalLines[endLi] || '';
    originalLines[endLi] =
        endText.substring(0, endOff) + newClose + oppOpen +
        endText.substring(endOff);
    var startText = originalLines[startLi] || '';
    originalLines[startLi] =
        startText.substring(0, startOff) + oppClose + newOpen +
        startText.substring(startOff);
  }
  return 'split';
}

// ── Helper: scan all markers of `type` WITHIN a selection range ────────
// Returns a flat list of {li, off, isOpen} for every [A>/<A] (or [D>/<D])
// marker whose position falls inside the [startLi/startOff, endLi/endOff)
// selection.  Used to detect "user re-selected a wrapped region and
// included the markers in the new selection" — in that case the inside
// markers are balanced and we can strip them all to toggle the wrap off.
function _scanMarkersInRange(type, startLi, startOff, endLi, endOff){
  var openMark = (type==='a') ? '[A>' : '[D>';
  var closeMark = (type==='a') ? '<A]' : '<D]';
  var markers = [];
  for(var li = startLi; li <= endLi; li++){
    var text = originalLines[li] || '';
    var minPos = (li === startLi) ? startOff : 0;
    var maxPos = (li === endLi) ? endOff : text.length;
    var pos = minPos;
    while(pos < maxPos){
      var oP = text.indexOf(openMark, pos);
      var cP = text.indexOf(closeMark, pos);
      if(oP >= maxPos) oP = -1;
      if(cP >= maxPos) cP = -1;
      var nextPos = -1, isOpen = false;
      if(oP >= 0 && (cP < 0 || oP < cP)){nextPos = oP; isOpen = true;}
      else if(cP >= 0){nextPos = cP; isOpen = false;}
      if(nextPos < 0) break;
      markers.push({li: li, off: nextPos, isOpen: isOpen});
      pos = nextPos + 3;
    }
  }
  return markers;
}

// ── Helper: locate an enclosing tag pair around a selection ────────────
// Scans LEFT from the selection start through the same line and earlier
// lines, looking for the most recent unbalanced open-marker of `type`.
// If found, scans RIGHT from the selection end through later lines for
// the matching close-marker.  Returns {openLi, openOff, closeLi, closeOff}
// or null if no surrounding wrap exists.
//
// "Unbalanced" means: when walking backward, each closer we encounter
// nests deeper (increments depth) and each opener decrements.  We stop
// when depth would go below zero — that opener is the surrounding one.
function _findSurroundingTagWrap(type, startLi, startOff, endLi, endOff){
  var openMark = (type==='a') ? '[A>' : '[D>';
  var closeMark = (type==='a') ? '<A]' : '<D]';
  var L = originalLines.length;

  // ── Phase 1: scan LEFT for nearest unbalanced opener ─────────────────
  var openLi = -1, openOff = -1, depth = 0;
  for(var li = startLi; li >= 0; li--){
    var text = originalLines[li] || '';
    var maxPos = (li === startLi) ? startOff : text.length;
    // Collect all marker positions up to maxPos
    var positions = [];
    var pos = 0;
    while(pos < maxPos){
      var oP = text.indexOf(openMark, pos);
      var cP = text.indexOf(closeMark, pos);
      if(oP >= maxPos) oP = -1;
      if(cP >= maxPos) cP = -1;
      var nextPos = -1, isOpen = false;
      if(oP >= 0 && (cP < 0 || oP < cP)){nextPos = oP; isOpen = true;}
      else if(cP >= 0){nextPos = cP; isOpen = false;}
      if(nextPos < 0) break;
      positions.push({pos: nextPos, open: isOpen});
      pos = nextPos + 3;
    }
    // Walk RIGHT-TO-LEFT through markers on this line
    for(var i = positions.length - 1; i >= 0; i--){
      if(positions[i].open){
        if(depth === 0){
          openLi = li;
          openOff = positions[i].pos;
          break;
        }
        depth--;
      } else {
        depth++;
      }
    }
    if(openLi >= 0) break;
  }
  if(openLi < 0) return null;

  // ── Phase 2: scan RIGHT from end for matching closer ─────────────────
  var closeLi = -1, closeOff = -1;
  depth = 0;
  for(var li2 = endLi; li2 < L; li2++){
    var text2 = originalLines[li2] || '';
    var startPos = (li2 === endLi) ? endOff : 0;
    var pos2 = startPos;
    while(pos2 < text2.length){
      var oP2 = text2.indexOf(openMark, pos2);
      var cP2 = text2.indexOf(closeMark, pos2);
      var nextPos2 = -1, isOpen2 = false;
      if(oP2 >= 0 && (cP2 < 0 || oP2 < cP2)){nextPos2 = oP2; isOpen2 = true;}
      else if(cP2 >= 0){nextPos2 = cP2; isOpen2 = false;}
      if(nextPos2 < 0) break;
      if(isOpen2){
        depth++;
      } else {
        if(depth === 0){
          closeLi = li2;
          closeOff = nextPos2;
          break;
        }
        depth--;
      }
      pos2 = nextPos2 + 3;
    }
    if(closeLi >= 0) break;
  }
  if(closeLi < 0) return null;

  return {openLi: openLi, openOff: openOff, closeLi: closeLi, closeOff: closeOff};
}

// Original single-line wrap/toggle/swap logic, factored out so the
// multi-line wrapper can delegate to it when both endpoints are on the
// same line.  startOff / endOff are character offsets into the
// originalLines[li] string (NOT the DOM positions).
function _wrapTagSingleLine(type, li, startOff, endOff){
  var text=originalLines[li]||'';
  if(startOff===endOff){document.getElementById('statBar').textContent='Select some text first';return;}

  // ── SPLIT-or-STRIP check (single-line) ───────────────────────
  // If the selection sits inside an OPPOSITE-type wrap (e.g. user is
  // adding [D> while inside a multi-line [A>...<A] wrap), split the
  // outer wrap into siblings around the new tag instead of nesting.
  //
  // Special case: snug-fit selection inside an opp wrap that is ITSELF
  // inside a SAME-type outer wrap (e.g. user clicks [A> on `[D>or<D]`
  // that sits inside `[A>...<A]`).  In that case we STRIP the inner D
  // markers — the outer A wrap already marks the text correctly, no
  // need to swap D→A (which would create a nested [A>or<A]).
  //
  // _splitOppositeWrap returns 'split', 'strip', or false (snug-fit
  // without outer same-type → fall through to existing SWAP logic).
  var _splitRes = _splitOppositeWrap(type, li, startOff, li, endOff);
  if(_splitRes){
    rebuildEditor();
    var _openMark = (type==='a') ? '[A>' : '[D>';
    var _oppMark = (type==='a') ? '[D>' : '[A>';
    if(_splitRes === 'strip'){
      document.getElementById('statBar').textContent =
          _oppMark+' wrap removed (text already inside outer '+_openMark+' wrap, Line '+(li+1)+')';
    } else {
      document.getElementById('statBar').textContent =
          _openMark+' tag added (split surrounding '+_oppMark+' wrap, Line '+(li+1)+')';
    }
    return;
  }

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
      // Selection contains A tags.  Two sub-cases:
      //   EXPAND — selection extends beyond the inner markers (any
      //            plain text before the first marker or after the
      //            last marker).  User wants the wrap to grow to
      //            cover the whole selection.
      //   STRIP  — selection ≈ the markers (snug fit).  Toggle off.
      var _firstA1=selected.indexOf('[A>'), _firstA2=selected.indexOf('<A]');
      var _firstMk=(_firstA1<0)?_firstA2:(_firstA2<0?_firstA1:Math.min(_firstA1,_firstA2));
      var _lastA1=selected.lastIndexOf('[A>'), _lastA2=selected.lastIndexOf('<A]');
      var _lastMk=Math.max(_lastA1,_lastA2);
      var _stripped=stripA(selected);
      var _extends=(_firstMk>0)||(_lastMk+3<selected.length);
      if(_extends && _stripped.length>0){
        // EXPAND: strip inner markers + wrap whole selection
        result=before+'[A>'+_stripped+'<A]'+after;
        msg='[A> wrap expanded to selection (Line '+(li+1)+')';
      } else {
        // STRIP (toggle off)
        result=before+_stripped+after;
        msg='[A> tags removed (Line '+(li+1)+')';
      }
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
      // Selection contains D tags.  Two sub-cases:
      //   EXPAND — selection extends beyond the inner markers →
      //            grow the wrap to cover the whole selection.
      //   STRIP  — snug fit → toggle off.
      var _firstD1=selected.indexOf('[D>'), _firstD2=selected.indexOf('<D]');
      var _firstMk=(_firstD1<0)?_firstD2:(_firstD2<0?_firstD1:Math.min(_firstD1,_firstD2));
      var _lastD1=selected.lastIndexOf('[D>'), _lastD2=selected.lastIndexOf('<D]');
      var _lastMk=Math.max(_lastD1,_lastD2);
      var _stripped=stripD(selected);
      var _extends=(_firstMk>0)||(_lastMk+3<selected.length);
      if(_extends && _stripped.length>0){
        // EXPAND: strip inner markers + wrap whole selection
        result=before+'[D>'+_stripped+'<D]'+after;
        msg='[D> wrap expanded to selection (Line '+(li+1)+')';
      } else {
        // STRIP (toggle off)
        result=before+_stripped+after;
        msg='[D> tags removed (Line '+(li+1)+')';
      }
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
function rebuildEditor(skipUndo, restoreLi, restoreOff){
  if(!skipUndo) pushUndo();
  var tw=document.getElementById('textWrap');
  var panel=document.getElementById('textPanel');
  var scrollTop=panel?panel.scrollTop:0;

  // ── Save cursor if caller didn't specify a restore point ────────
  var savedLi=restoreLi, savedOff=restoreOff;
  if(savedLi===undefined || savedLi===null || savedLi<0){
    savedLi=-1; savedOff=0;
    var sel=window.getSelection();
    if(sel && sel.rangeCount && sel.isCollapsed){
      var range=sel.getRangeAt(0);
      var lc=range.startContainer;
      while(lc && !(lc.classList && lc.classList.contains('lc'))) lc=lc.parentNode;
      if(lc){
        var tl=lc.closest('.tl');
        if(tl){
          savedLi=parseInt(tl.dataset.li);
          savedOff=getTextOffset(lc, range.startContainer, range.startOffset);
        }
      }
    }
  }

  tw.innerHTML=renderLinesWithTags(originalLines);
  refreshChangeMeta();enableInlineEditing();buildTagIndices();
  if(document.getElementById('focusCb').checked)toggleFocus(true);
  if(document.getElementById('collapseCb').checked)toggleCollapse(true);
  if(panel)panel.scrollTop=scrollTop;

  // ── Restore cursor ─────────────────────────────────────────────
  if(savedLi>=0 && savedLi<originalLines.length){
    var restoredTl=document.querySelector('.tl[data-li="'+savedLi+'"]');
    if(restoredTl){
      setActiveLine(restoredTl);
      var restoredLc=restoredTl.querySelector('.lc');
      if(restoredLc){
        restoredLc.focus({preventScroll:true});
        placeCursorAtOffset(restoredLc, Math.min(savedOff>=0?savedOff:0, (restoredLc.textContent||'').length));
      }
    }
  }
}

// ── Enhanced keyboard: Enter=tag-aware split, Backspace@0=simple merge ──
document.getElementById('textWrap').addEventListener('keydown',function(e){
  var sel=window.getSelection();
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

// ── Dynamic iframe resize ──────────────────────────────────────────
// Streamlit's st.components.v1.html() renders this viewer inside an
// iframe with a STATIC `height=` attribute (from Python).  When the
// browser viewport is taller than that, content gets clipped at the
// bottom because the iframe never grows.  Streamlit exposes a
// `setFrameHeight` API (postMessage to parent) for declared
// components, but `st.components.v1.html()` doesn't auto-wire it.
// We send the message manually — Streamlit's runtime listens for
// "streamlit:setFrameHeight" and will resize the iframe accordingly.
//
// We compute the desired height from the OUTER (parent) viewport,
// not the iframe's own — the iframe should fill all space below
// Streamlit's chrome (header / tabs / etc.).  We subtract a small
// buffer (160px) for the title + tab bar visible above the iframe.
function _reportIframeHeight(){
  try{
    // Compute desired height from the parent viewport.
    var H;
    try{
      H = (window.parent && window.parent.innerHeight)
            ? window.parent.innerHeight : window.innerHeight;
    } catch(_){ H = window.innerHeight; }

    // ── DYNAMIC top-offset measurement ───────────────────────
    // Instead of guessing how much chrome (header, tabs, file
    // pill) sits above the iframe, MEASURE the iframe's actual
    // top position in the parent viewport via getBoundingClientRect.
    // Then compute desired = visibleHeight - top - smallBottomMargin.
    var topOffset = 130;  // fallback if measurement fails
    var fe = null;
    try{
      fe = window.frameElement;
      if(fe){
        var rect = fe.getBoundingClientRect();
        // rect.top is relative to the parent viewport (already
        // accounts for scroll position).  Clamp to >= 0 in case
        // the iframe is briefly above-fold during initial layout.
        topOffset = Math.max(0, rect.top);
      }
    } catch(_){ /* keep fallback */ }

    // 8px = small bottom margin so iframe doesn't kiss the
    // viewport edge (looks cleaner and gives room for scrollbar).
    var desired = Math.max(500, H - topOffset - 8);

    // Strategy 1 — DIRECT: write the measured height onto our own
    // <iframe> element + its wrappers in the parent document.
    try{
      if(fe){
        fe.setAttribute('height', desired);
        fe.style.height = desired + 'px';
        fe.style.minHeight = desired + 'px';
        fe.style.maxHeight = desired + 'px';
        // Walk up the wrapper chain (Streamlit nests:
        // element-container > stIFrame > iframe).
        var wrap = fe.parentElement;
        var depth = 0;
        while(wrap && depth < 6){
          if(wrap.matches && (
             wrap.matches('[data-testid="stIFrame"]') ||
             wrap.matches('[data-testid="stCustomComponentV1Host"]') ||
             wrap.matches('[data-testid="element-container"]'))){
            wrap.style.height = desired + 'px';
            wrap.style.minHeight = desired + 'px';
            wrap.style.maxHeight = desired + 'px';
          }
          wrap = wrap.parentElement;
          depth++;
        }
      }
    } catch(_){ /* cross-origin or detached — ignore */ }

    // Strategy 2 — POSTMESSAGE fallback for declared-component path.
    try{
      window.parent.postMessage({
        type: 'streamlit:setFrameHeight',
        height: desired
      }, '*');
    } catch(_){ /* ignore */ }
  } catch(err){ /* leave static height */ }
}
// Report on load, after paints, and on every resize.
window.addEventListener('load', function(){
  _reportIframeHeight();
  // Re-report after PDF/text rendering has settled (layout shift).
  setTimeout(_reportIframeHeight, 200);
  setTimeout(_reportIframeHeight, 800);
  setTimeout(_reportIframeHeight, 2000);
});
window.addEventListener('resize', _reportIframeHeight);
// Also re-measure when the parent scrolls — chrome height can
// change (e.g. sticky header collapses on scroll).
try{
  if(window.parent){
    window.parent.addEventListener('scroll', _reportIframeHeight, {passive: true});
  }
} catch(_){ /* ignore */ }
// Fire once immediately too in case 'load' has already fired.
_reportIframeHeight();
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
        # Find the earliest tag marker in remaining
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
            # No more markers — rest is plain text
            if remaining:
                segments.append((remaining, False, None))
            break

        # Text before the marker
        if earliest_pos > 0:
            segments.append((remaining[:earliest_pos], False, None))
        # The marker itself
        segments.append((earliest_marker, True, earliest_type))
        remaining = remaining[earliest_pos + len(earliest_marker):]

    return segments


def build_viewer_html(
    pdf_bytes_or_images,
    clean_lines: List[str],
    word_map: Dict[str, dict],
    dpi: int = PDF_RENDER_DPI,
    filename: str = "edited_clean.txt",
    pdf_bytes: bytes = None,
) -> str:
    scale = dpi / 72.0

    # Support both old (list of base64 images) and new (raw pdf_bytes) API
    if pdf_bytes is None:
        if isinstance(pdf_bytes_or_images, bytes):
            pdf_bytes = pdf_bytes_or_images
        else:
            # Legacy: caller passed list of images — we need pdf_bytes for PDF.js
            # This shouldn't happen with updated call sites but just in case,
            # fall back gracefully (caller must update).
            raise ValueError("build_viewer_html now requires pdf_bytes (raw PDF bytes)")
    
    # Compute page count from PDF
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page_count = len(doc)
    doc.close()
    
    pdf_b64 = base64.b64encode(pdf_bytes).decode()

    text_lines_html = []
    in_add = False
    in_del = False
    for li, line in enumerate(clean_lines):
        raw_words = line.split(' ')
        # First pass: build each word's HTML and record state-before / state-after
        word_entries = []  # list of (tw_html, state_before, state_after)
        for wi, rw in enumerate(raw_words):
            if not rw:
                st = 'add' if in_add else ('del' if in_del else 'none')
                word_entries.append(('', st, st))
                continue

            state_before = 'add' if in_add else ('del' if in_del else 'none')
            segments = _split_at_tags(rw)

            # Build inner HTML with per-segment styling
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

        # Second pass: join words with spaces, decorating spaces when inside a tag
        line_parts = []
        for idx, (wh, sb, sa) in enumerate(word_entries):
            if idx > 0:
                # The space sits AFTER the previous word — use previous word's state_after
                prev_sa = word_entries[idx - 1][2]
                if prev_sa == 'add':
                    line_parts.append('<span class="tg-a"> </span>')
                elif prev_sa == 'del':
                    line_parts.append('<span class="tg-d"> </span>')
                else:
                    line_parts.append(' ')
            line_parts.append(wh)

        ln = li + 1
        text_lines_html.append(
            '<div class="tl" data-li="{}"><span class="ln">{}</span>'
            '<span class="lc">{}</span></div>'.format(li, ln, ''.join(line_parts))
        )

    text_block = "\n".join(text_lines_html)
    wordmap_js = json.dumps(word_map)
    lines_js = json.dumps(clean_lines)

    # Compute change lines and stats
    change_lines = []
    for li, line in enumerate(clean_lines):
        if '[A>' in line or '[D>' in line or '<A]' in line or '<D]' in line:
            change_lines.append(li)
    diffs_count = len(change_lines)

    return (_TEMPLATE
            .replace("__TEXT_BLOCK__", text_block)
            .replace("__PDF_B64__", pdf_b64)
            .replace("__PAGE_COUNT__", str(page_count))
            .replace("__WORDMAP_JS__", wordmap_js)
            .replace("__LINES_JS__", lines_js)
            .replace("__FILENAME__", filename)
            .replace("__CHANGELINES_JS__", json.dumps(change_lines)))


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
        /* Clean drop zone styling */
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

    # Check if files are already uploaded
    pdf_file = st.session_state.get("pdf")
    txt_file = st.session_state.get("txt")

    if not pdf_file or not txt_file:
        # Show two drop zones side by side
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

    with st.spinner("Extracting words & mapping…"):
        words = extract_word_bboxes(pdf_bytes)

    # --- Edit mode via session state ---
    if "edit_mode" not in st.session_state:
        st.session_state.edit_mode = False
    if "edited_text" not in st.session_state:
        st.session_state.edited_text = None

    # Use edited text if available, otherwise original
    display_text = st.session_state.edited_text if st.session_state.edited_text is not None else clean_text
    display_lines = display_text.splitlines()

    with st.spinner("Mapping words…"):
        word_map = build_word_level_map(display_lines, words)

    html = build_viewer_html(pdf_bytes, display_lines, word_map, filename=txt_file.name)
    st.components.v1.html(html, height=920, scrolling=False)


if __name__ == "__main__":
    main()
