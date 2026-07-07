"""
Standalone test app for bidirectional PDF ↔ Text highlight sync.

Features:
  1. Multi-page search — searches across ALL PDF pages
  2. Fuzzy matching — word-token overlap ≥60% fallback
  3. Accurate textarea scroll — line-count based
  4. Multi-match navigation — prev/next with match count
  5. Highlight persistence — survives page navigation via sessionStorage
  6. Auto page navigation — jumps to the page containing the match
  7. Line-number gutter — synced scroll beside textarea

Usage:
    streamlit run app/test_highlight_sync.py --server.port 8502
"""
import base64
import hashlib
import json
from pathlib import Path

import fitz  # PyMuPDF
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(page_title="PDF ↔ Text Highlight Sync", layout="wide")
st.title("🔗 PDF ↔ Text Highlight Sync")

# ─── File uploaders ──────────────────────────────────────────────
up_col1, up_col2 = st.columns(2)
with up_col1:
    uploaded_pdf = st.file_uploader("📄 Drop PDF here", type=["pdf"])
with up_col2:
    uploaded_txt = st.file_uploader("📝 Drop Text file here", type=["txt", "md", "text"])

if not uploaded_pdf or not uploaded_txt:
    st.markdown(
        '<div style="text-align:center;padding:60px 20px;color:#94a3b8;font-size:1.1rem;">'
        '⬆️ Upload a <b>PDF</b> and a <b>Text file</b> above to compare them side by side.'
        '</div>',
        unsafe_allow_html=True,
    )
    st.stop()

# ─── Save uploads ────────────────────────────────────────────────
pdf_bytes = uploaded_pdf.getvalue()
txt_bytes = uploaded_txt.getvalue()
tmp_pdf = Path("/tmp/_test_hl_sync.pdf")
tmp_pdf.write_bytes(pdf_bytes)
pdf_path = str(tmp_pdf)
text_content = txt_bytes.decode("utf-8")


# ─── Extract text layer + page images ────────────────────────────
@st.cache_data
def extract_pdf_data(pdf_file: str, _file_hash: str = ""):
    doc = fitz.open(pdf_file)
    text_layer = {}
    page_images = {}
    for page_idx in range(len(doc)):
        page = doc[page_idx]
        page_num = page_idx + 1
        pw, ph = page.rect.width, page.rect.height
        if pw <= 0 or ph <= 0:
            continue
        pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
        page_images[page_num] = pix.tobytes("png")
        text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
        spans_data = []
        for block in text_dict.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    txt = span.get("text", "").strip()
                    if not txt:
                        continue
                    bbox = span.get("bbox", [0, 0, 0, 0])
                    spans_data.append({
                        "t": txt,
                        "x": round(bbox[0] / pw * 100, 3),
                        "y": round(bbox[1] / ph * 100, 3),
                        "w": round((bbox[2] - bbox[0]) / pw * 100, 3),
                        "h": round((bbox[3] - bbox[1]) / ph * 100, 3),
                        "fs": round(span.get("size", 10), 1),
                    })
        if spans_data:
            text_layer[page_num] = spans_data
    doc.close()
    return text_layer, page_images


def render_pdf_overlay(img_bytes: bytes, text_spans: list, page_num: int) -> str:
    img_data = base64.b64encode(img_bytes).decode()
    spans_html = ""
    for sp in text_spans:
        txt = (sp["t"].replace("&", "&amp;").replace("<", "&lt;")
               .replace(">", "&gt;").replace('"', "&quot;"))
        spans_html += (
            f'<span class="vr-tl-span" '
            f'style="left:{sp["x"]}%;top:{sp["y"]}%;'
            f'width:{sp["w"]}%;height:{sp["h"]}%;'
            f'font-size:{sp["fs"] * 0.75}px;" '
            f'data-text="{txt}">{txt}</span>'
        )
    return f'''<style>
.vr-pdf-container {{ position:relative; display:inline-block; width:100%; user-select:text; }}
.vr-pdf-img {{ width:100%; display:block; user-select:none; pointer-events:none; }}
.vr-text-overlay {{ position:absolute; top:0; left:0; width:100%; height:100%; z-index:2; }}
.vr-tl-span {{
    position:absolute; color:transparent; white-space:nowrap;
    overflow:hidden; line-height:1.2; cursor:text; user-select:text;
}}
.vr-tl-span::selection {{ background:rgba(59,130,246,0.35); }}
.vr-tl-span.vr-sync-hl {{ background:rgba(250,204,21,0.45) !important; color:transparent; border-radius:2px; }}
</style>
<div class="vr-pdf-container" data-page="{page_num}">
<img src="data:image/png;base64,{img_data}" class="vr-pdf-img" />
<div class="vr-text-overlay">{spans_html}</div>
</div>'''


# ─── Load data ───────────────────────────────────────────────────
_pdf_hash = hashlib.md5(pdf_bytes).hexdigest()
text_layer, page_images = extract_pdf_data(pdf_path, _file_hash=_pdf_hash)
total_pages = len(page_images)

if total_pages == 0:
    st.error("No pages could be extracted from the PDF.")
    st.stop()

_file_key = _pdf_hash + uploaded_txt.name
if st.session_state.get("_test_file_key") != _file_key:
    st.session_state["_test_file_key"] = _file_key
    st.session_state.test_page = 1
if "test_page" not in st.session_state:
    st.session_state.test_page = 1

st.markdown("---")

# ─── Side-by-side layout ────────────────────────────────────────
with st.container(key="test_twocol"):
    col_left, col_right = st.columns([1, 1], gap="medium")

    with col_left:
        st.markdown(
            '<p style="font-size:0.92rem;font-weight:700;margin:0 0 6px;">'
            '📄 PDF Page — Original Reference</p>',
            unsafe_allow_html=True,
        )
        cur = st.session_state.test_page
        cur = max(1, min(cur, total_pages))
        st.session_state.test_page = cur

        if total_pages > 1:
            nc1, nc2, nc3, nc4 = st.columns([1, 5, 1, 3])
            with nc1:
                if st.button("◀", key="pg_prev"):
                    st.session_state.test_page = max(1, cur - 1)
                    st.rerun()
            with nc3:
                if st.button("▶", key="pg_next"):
                    st.session_state.test_page = min(total_pages, cur + 1)
                    st.rerun()
            with nc2:
                st.session_state["pg_slider"] = cur
                new_pg = st.slider(
                    "Page", 1, total_pages,
                    key="pg_slider", label_visibility="collapsed",
                )
                if new_pg != cur:
                    st.session_state.test_page = new_pg
            with nc4:
                st.markdown(
                    f'<p style="font-size:0.82rem;color:#64748b;margin:8px 0 0;text-align:right;">'
                    f'Page <b>{cur}</b> / {total_pages}</p>',
                    unsafe_allow_html=True,
                )
        else:
            st.markdown(
                '<div style="display:flex;align-items:center;justify-content:center;'
                'padding:6px 14px;margin-bottom:8px;min-height:42px;'
                'background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;">'
                '<span style="font-size:0.82rem;color:#64748b;">Page <b>1</b> / 1</span></div>',
                unsafe_allow_html=True,
            )

        cur = st.session_state.test_page
        page_spans = text_layer.get(cur, [])
        if cur in page_images and page_spans:
            st.html(render_pdf_overlay(page_images[cur], page_spans, cur))
        elif cur in page_images:
            st.image(page_images[cur])
        else:
            st.warning("No image for this page.")

    with col_right:
        st.markdown(
            '<p style="font-size:0.92rem;font-weight:700;margin:0 0 6px;">'
            '📝 Converted Text</p>',
            unsafe_allow_html=True,
        )
        st.text_area(
            "Text content",
            value=text_content,
            height=700,
            key="test_text_area",
            label_visibility="collapsed",
        )

# ─── Build cross-page text index for JS ─────────────────────────
all_pages_text = {}
for pg, spans in text_layer.items():
    all_pages_text[pg] = [s["t"] for s in spans]
all_pages_json = json.dumps(all_pages_text).replace("</", "<\\/")
current_page = st.session_state.test_page

# ─── Bidirectional selection sync JS (all 7 features) ───────────
_JS = """<script>
(function() {
    var doc = window.parent.document;
    var container = doc.querySelector('.st-key-test_twocol');
    if (!container) return;

    var CURRENT_PAGE = __CURRENT_PAGE__;
    var TOTAL_PAGES  = __TOTAL_PAGES__;
    var ALL_PAGES    = __ALL_PAGES__;

    /* ── Helpers ───────────────────────────────────────────────── */
    function normalizeWS(s) { return s.replace(/\\s+/g, ' ').trim(); }
    function stripTags(s)   { return s.replace(/\\[[A-Z]>|<[A-Z]\\]/g, '').replace(/\\s+/g, ' ').trim(); }

    /* ── Global text index (all pages concatenated) ────────────── */
    var globalText  = '';
    var globalIndex = [];
    var pageKeys = Object.keys(ALL_PAGES).map(Number).sort(function(a,b){return a-b;});
    pageKeys.forEach(function(pg) {
        ALL_PAGES[pg].forEach(function(txt, i) {
            var t = normalizeWS(txt);
            if (!t) return;
            var s = globalText.length;
            globalText += t + ' ';
            globalIndex.push({page: pg, spanIdx: i, start: s, end: s + t.length});
        });
    });

    /* ── Match state ───────────────────────────────────────────── */
    var MS = { matches: [], idx: -1, query: '', dir: '' };

    /* ── Find all matches in global PDF text ───────────────────── */
    function findAllGlobalMatches(query) {
        var matches = [];
        if (query.length < 2) return matches;

        /* 1. Case-sensitive exact */
        var p = globalText.indexOf(query);
        while (p !== -1) {
            matches.push({start:p, end:p+query.length, fuzzy:false});
            p = globalText.indexOf(query, p+1);
        }

        /* 2. Case-insensitive exact */
        if (!matches.length) {
            var gl = globalText.toLowerCase(), ql = query.toLowerCase();
            p = gl.indexOf(ql);
            while (p !== -1) {
                matches.push({start:p, end:p+ql.length, fuzzy:false});
                p = gl.indexOf(ql, p+1);
            }
        }

        /* 3. Fuzzy word-token (≥60% overlap) */
        if (!matches.length) {
            var qw = query.toLowerCase().split(/\\s+/).filter(function(w){return w.length>0;});
            if (qw.length >= 2) {
                var words = [], re = /\\S+/g, m;
                var gl2 = globalText.toLowerCase();
                while ((m = re.exec(gl2)) !== null)
                    words.push({w:m[0], s:m.index, e:m.index+m[0].length});
                for (var i = 0; i <= words.length - qw.length; i++) {
                    var mc = 0;
                    for (var j = 0; j < qw.length; j++)
                        if (i+j < words.length && words[i+j].w === qw[j]) mc++;
                    if (mc / qw.length >= 0.6)
                        matches.push({start:words[i].s, end:words[i+qw.length-1].e, fuzzy:true});
                }
            }
        }

        /* Annotate each match with its page number */
        matches.forEach(function(m) {
            m.page = null;
            for (var i = 0; i < globalIndex.length; i++) {
                if (globalIndex[i].end > m.start && globalIndex[i].start < m.end) {
                    m.page = globalIndex[i].page; break;
                }
            }
        });
        return matches;
    }

    /* ── Find all matches in textarea value ────────────────────── */
    function findAllTextareaMatches(query, val) {
        var matches = [];
        /* CS */
        var p = val.indexOf(query);
        while (p !== -1) {
            matches.push({start:p, end:p+query.length, fuzzy:false, page:CURRENT_PAGE});
            p = val.indexOf(query, p+1);
        }
        /* CI */
        if (!matches.length) {
            var lv = val.toLowerCase(), lq = query.toLowerCase();
            p = lv.indexOf(lq);
            while (p !== -1) {
                matches.push({start:p, end:p+lq.length, fuzzy:false, page:CURRENT_PAGE});
                p = lv.indexOf(lq, p+1);
            }
        }
        /* Fuzzy */
        if (!matches.length) {
            var qw = query.toLowerCase().split(/\\s+/).filter(function(w){return w.length>0;});
            if (qw.length >= 2) {
                var words = [], re = /\\S+/g, m2;
                var ltv = val.toLowerCase();
                while ((m2 = re.exec(ltv)) !== null)
                    words.push({w:m2[0], s:m2.index, e:m2.index+m2[0].length});
                for (var i = 0; i <= words.length - qw.length; i++) {
                    var mc = 0;
                    for (var j = 0; j < qw.length; j++)
                        if (i+j < words.length && words[i+j].w === qw[j]) mc++;
                    if (mc / qw.length >= 0.6)
                        matches.push({start:words[i].s, end:words[i+qw.length-1].e, fuzzy:true, page:CURRENT_PAGE});
                }
            }
        }
        return matches;
    }

    /* ── Height sync ───────────────────────────────────────────── */
    function doHeightSync() {
        var img = container.querySelector('.vr-pdf-img') || container.querySelector('img');
        if (!img) return;
        function adjust() {
            var h = img.clientHeight;
            if (h <= 100) return;
            var ta = container.querySelector('textarea');
            if (ta) { ta.style.height=h+'px'; ta.style.maxHeight=h+'px'; ta.style.minHeight=h+'px'; }
        }
        if (img.complete && img.clientHeight > 0) adjust();
        else img.addEventListener('load', adjust);
        try { new ResizeObserver(adjust).observe(img); } catch(e) {}
    }

    /* ── Line number gutter ────────────────────────────────────── */
    function addLineNumbers() {
        var ta = container.querySelector('textarea');
        if (!ta || ta.dataset.lnReady === '1') return;
        ta.dataset.lnReady = '1';
        var wrapper = ta.parentElement;
        if (!wrapper) return;
        wrapper.style.display = 'flex';

        var g = doc.createElement('div');
        g.className = 'vr-line-gutter';
        g.style.cssText = 'width:38px;min-width:38px;overflow:hidden;'
            + 'font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;'
            + 'color:#94a3b8;text-align:right;padding:8px 6px 8px 0;user-select:none;'
            + 'background:#f1f5f9;border:1px solid #e2e8f0;border-right:none;'
            + 'border-radius:8px 0 0 8px;box-sizing:border-box;';
        wrapper.insertBefore(g, ta);
        ta.style.borderRadius = '0 8px 8px 0';
        ta.style.flex = '1';

        var cs = doc.defaultView.getComputedStyle(ta);
        function update() {
            g.style.lineHeight = cs.lineHeight;
            g.style.paddingTop = cs.paddingTop;
            g.style.height = ta.clientHeight + 'px';
            g.style.maxHeight = ta.clientHeight + 'px';
            var n = ta.value.split('\\n').length, h = '';
            for (var i = 1; i <= n; i++) h += '<div>' + i + '</div>';
            g.innerHTML = h;
            g.scrollTop = ta.scrollTop;
        }
        ta.addEventListener('scroll', function() { g.scrollTop = ta.scrollTop; });
        ta.addEventListener('input', update);
        setTimeout(update, 200);
        setTimeout(update, 1000);
    }

    /* ── Clear highlights ──────────────────────────────────────── */
    function clearSyncHighlights() {
        container.querySelectorAll('.vr-sync-hl').forEach(function(el) {
            el.classList.remove('vr-sync-hl');
        });
    }

    /* ── Match navigation bar ──────────────────────────────────── */
    function getMatchBar() {
        var bar = container.querySelector('.vr-match-bar');
        if (bar) return bar;
        bar = doc.createElement('div');
        bar.className = 'vr-match-bar';
        bar.style.cssText = 'display:none;align-items:center;gap:10px;padding:7px 14px;'
            + 'background:linear-gradient(135deg,#1e293b,#334155);color:#f8fafc;'
            + 'border-radius:8px;margin:0 0 8px;font-size:0.82rem;'
            + 'box-shadow:0 2px 8px rgba(0,0,0,0.12);z-index:50;';
        bar.innerHTML = '<span class="vr-mi" style="flex:1"></span>'
            + '<button class="vr-mp" title="Previous match" style="background:#475569;color:#f8fafc;border:none;border-radius:4px;padding:3px 10px;cursor:pointer;font-size:0.8rem;">◀ Prev</button>'
            + '<button class="vr-mn" title="Next match" style="background:#475569;color:#f8fafc;border:none;border-radius:4px;padding:3px 10px;cursor:pointer;font-size:0.8rem;">Next ▶</button>'
            + '<button class="vr-mc" title="Close" style="background:transparent;color:#94a3b8;border:none;cursor:pointer;font-size:1.1rem;padding:0 4px;margin-left:6px;">✕</button>';
        container.insertBefore(bar, container.firstChild);
        bar.querySelector('.vr-mp').addEventListener('click', function() { cycleMatch(-1); });
        bar.querySelector('.vr-mn').addEventListener('click', function() { cycleMatch(1); });
        bar.querySelector('.vr-mc').addEventListener('click', function() {
            bar.style.display = 'none';
            clearSyncHighlights();
            MS.matches = []; MS.idx = -1;
        });
        return bar;
    }

    function updateMatchBar() {
        var bar = getMatchBar();
        if (!MS.matches.length) { bar.style.display = 'none'; return; }
        bar.style.display = 'flex';
        var m = MS.matches[MS.idx] || MS.matches[0];
        var info = 'Match <b>' + (MS.idx + 1) + '</b> of <b>' + MS.matches.length + '</b>';
        if (m && m.page && m.page !== CURRENT_PAGE)
            info += ' — <span style="color:#fbbf24">on page ' + m.page + '</span>';
        if (m && m.fuzzy)
            info += ' <span style="color:#a78bfa;font-style:italic">(fuzzy)</span>';
        /* page distribution */
        var pages = {};
        MS.matches.forEach(function(mm) { if (mm.page) pages[mm.page] = (pages[mm.page]||0)+1; });
        var pk = Object.keys(pages).sort(function(a,b){return a-b;});
        if (pk.length > 1)
            info += ' <span style="color:#94a3b8;font-size:0.75rem">| Pages: ' + pk.join(', ') + '</span>';
        bar.querySelector('.vr-mi').innerHTML = info;
    }

    function cycleMatch(dir) {
        if (!MS.matches.length) return;
        MS.idx = (MS.idx + dir + MS.matches.length) % MS.matches.length;
        var m = MS.matches[MS.idx];
        clearSyncHighlights();

        if (MS.dir === 'pdf2text') {
            /* all matches are in textarea */
            var ta = container.querySelector('textarea');
            if (ta) selectInTextarea(ta, m);
        } else {
            /* text2pdf — match might be on a different page */
            if (m.page === CURRENT_PAGE) {
                highlightMatchOnPage(m);
            } else if (m.page) {
                sessionStorage.setItem('_hl_pending', JSON.stringify({
                    query: MS.query, idx: MS.idx, dir: 'text2pdf'
                }));
                navigateToPage(m.page);
            }
        }
        updateMatchBar();
    }

    /* ── Auto page navigation via slider ───────────────────────── */
    function navigateToPage(targetPage) {
        var slider = container.querySelector('input[type="range"]');
        if (!slider) return;
        try {
            var ns = Object.getOwnPropertyDescriptor(
                window.parent.HTMLInputElement.prototype, 'value'
            ).set;
            ns.call(slider, targetPage);
        } catch(e) {
            slider.value = targetPage;
        }
        slider.dispatchEvent(new Event('input', {bubbles: true}));
        slider.dispatchEvent(new Event('change', {bubbles: true}));
    }

    /* ── Highlight match spans on current PDF page ─────────────── */
    function highlightMatchOnPage(match) {
        var spans = container.querySelectorAll('.vr-tl-span');
        if (!spans.length) return;
        var fullText = '', spanMap = [];
        spans.forEach(function(sp) {
            var t = normalizeWS(sp.getAttribute('data-text') || sp.textContent || '');
            if (!t) return;
            var s = fullText.length;
            fullText += t + ' ';
            spanMap.push({span: sp, start: s, end: s + t.length});
        });
        var mt = globalText.substring(match.start, match.end);
        var norm = normalizeWS(mt);
        var mi = fullText.indexOf(norm);
        if (mi === -1) mi = fullText.toLowerCase().indexOf(norm.toLowerCase());
        if (mi === -1) return;
        var me = mi + norm.length, found = false;
        spanMap.forEach(function(sm) {
            if (sm.end > mi && sm.start < me) {
                sm.span.classList.add('vr-sync-hl');
                if (!found) { sm.span.scrollIntoView({behavior:'smooth', block:'center'}); found = true; }
            }
        });
    }

    /* ── Select + accurate scroll in textarea ──────────────────── */
    function selectInTextarea(ta, match) {
        ta.focus();
        ta.setSelectionRange(match.start, match.end);
        var before = ta.value.substring(0, match.start);
        var linesBefore = before.split('\\n').length - 1;
        var totalLines  = ta.value.split('\\n').length;
        if (totalLines > 0)
            ta.scrollTop = Math.max(0,
                (linesBefore / totalLines) * ta.scrollHeight - ta.clientHeight / 3);
    }

    /* ── Text → PDF ────────────────────────────────────────────── */
    function highlightInPDF(selectedText) {
        var norm = stripTags(selectedText);
        if (norm.length < 2) return;
        MS.query = norm;
        MS.dir   = 'text2pdf';
        MS.matches = findAllGlobalMatches(norm);
        if (!MS.matches.length) return;

        /* prefer first match on current page */
        var fp = -1;
        for (var i = 0; i < MS.matches.length; i++) {
            if (MS.matches[i].page === CURRENT_PAGE) { fp = i; break; }
        }
        MS.idx = fp >= 0 ? fp : 0;
        clearSyncHighlights();
        var m = MS.matches[MS.idx];
        if (m.page === CURRENT_PAGE) {
            highlightMatchOnPage(m);
        } else if (m.page) {
            sessionStorage.setItem('_hl_pending', JSON.stringify({
                query: MS.query, idx: MS.idx, dir: 'text2pdf'
            }));
            navigateToPage(m.page);
        }
        updateMatchBar();
    }

    /* ── PDF → Text ────────────────────────────────────────────── */
    function highlightInTextarea(selectedText) {
        var ta = container.querySelector('textarea');
        if (!ta) return;
        var clean = stripTags(selectedText);
        if (clean.length < 2) return;
        MS.query = clean;
        MS.dir   = 'pdf2text';
        MS.matches = findAllTextareaMatches(clean, ta.value);
        if (!MS.matches.length) return;
        MS.idx = 0;
        selectInTextarea(ta, MS.matches[0]);
        updateMatchBar();
    }

    /* ── Restore pending highlights after page navigation ──────── */
    function restorePending() {
        var raw = sessionStorage.getItem('_hl_pending');
        if (!raw) return;
        sessionStorage.removeItem('_hl_pending');
        try {
            var p = JSON.parse(raw);
            if (!p.query) return;
            setTimeout(function() {
                MS.query = p.query;
                MS.dir   = p.dir || 'text2pdf';
                MS.matches = findAllGlobalMatches(p.query);
                if (!MS.matches.length) return;
                /* use stored index or find first on current page */
                if (typeof p.idx === 'number' && p.idx < MS.matches.length)
                    MS.idx = p.idx;
                else {
                    var fp = -1;
                    for (var i = 0; i < MS.matches.length; i++)
                        if (MS.matches[i].page === CURRENT_PAGE) { fp = i; break; }
                    MS.idx = fp >= 0 ? fp : 0;
                }
                clearSyncHighlights();
                if (MS.matches[MS.idx].page === CURRENT_PAGE)
                    highlightMatchOnPage(MS.matches[MS.idx]);
                updateMatchBar();
            }, 600);
        } catch(e) {}
    }

    /* ── Event listeners ───────────────────────────────────────── */
    var _bound = false;
    function setupListeners() {
        if (_bound) return;
        if (!container.querySelector('.vr-pdf-container') && !container.querySelector('textarea')) return;
        _bound = true;

        doc.addEventListener('mouseup', function() {
            setTimeout(function() {
                var _pdfC = container.querySelector('.vr-pdf-container');
                var _ta   = container.querySelector('textarea');

                /* Textarea → PDF */
                if (_ta && _ta === doc.activeElement) {
                    var s = _ta.selectionStart, e = _ta.selectionEnd;
                    if (e - s >= 2) {
                        var t = _ta.value.substring(s, e).trim();
                        clearSyncHighlights();
                        if (t.length >= 2) highlightInPDF(t);
                    }
                    return;
                }

                /* PDF → Textarea */
                var sel = window.parent.getSelection();
                var selText = sel ? sel.toString().trim() : '';
                if (selText.length < 2) return;
                var an = sel.anchorNode;
                if (!an) return;
                clearSyncHighlights();
                if (_pdfC && _pdfC.contains(an)) highlightInTextarea(selText);
            }, 100);
        });

        doc.addEventListener('mousedown', function(e) {
            if (!container.contains(e.target) && !e.target.closest('.vr-match-bar')) {
                clearSyncHighlights();
                var bar = container.querySelector('.vr-match-bar');
                if (bar) bar.style.display = 'none';
                MS.matches = []; MS.idx = -1;
            }
        });
    }

    /* ── Setup poller ──────────────────────────────────────────── */
    var att = 0;
    var poll = setInterval(function() {
        att++;
        doHeightSync();
        addLineNumbers();
        setupListeners();
        if (att === 1) restorePending();
        if (att > 40) clearInterval(poll);
    }, 300);
})();
</script>"""

components.html(
    _JS.replace("__CURRENT_PAGE__", str(current_page))
       .replace("__TOTAL_PAGES__", str(total_pages))
       .replace("__ALL_PAGES__", all_pages_json),
    height=0,
)
