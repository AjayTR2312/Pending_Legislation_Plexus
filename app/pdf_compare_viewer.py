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
from typing import List, Dict, Tuple

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
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    words_all = []
    for pi in range(len(doc)):
        for w in doc[pi].get_text("words"):
            words_all.append(dict(
                text=w[4], page=pi,
                x0=w[0], y0=w[1], x1=w[2], y1=w[3],
                block=w[5], line=w[6], word=w[7],
            ))
    doc.close()
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

        best_pos, best_n = -1, 0
        for start in range(cursor, min(cursor + 2000, len(pdf_texts))):
            n, pi = 0, start
            for tok in tokens:
                for off in range(min(8, len(pdf_texts) - pi)):
                    if pi + off < len(pdf_texts) and pdf_texts[pi + off] == tok:
                        n += 1
                        pi = pi + off + 1
                        break
            if n > best_n:
                best_n, best_pos = n, start
            if best_n >= len(tokens) * 0.7:
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

.panels{display:flex;height:calc(100vh - 44px)}
.panel{flex:1;overflow-y:auto;position:relative}
.panel-pdf{background:#fafafa;border-right:1px solid var(--border)}
.panel-text{background:var(--bg)}

.ph{
  position:sticky;top:0;z-index:10;padding:8px 14px;font-size:12px;font-weight:600;
  letter-spacing:.3px;display:flex;align-items:center;gap:8px;
  background:rgba(246,248,250,.95);backdrop-filter:blur(8px);
  border-bottom:1px solid var(--border);text-transform:uppercase;color:var(--text2);
}
.ph .badge{font-size:10px;padding:2px 7px;border-radius:8px;font-weight:500;
           background:var(--surface);border:1px solid var(--border);color:var(--text2)}

.pdf-wrap{padding:10px}
.page-ctr{position:relative;margin-bottom:12px;background:#fff;border-radius:4px;
          border:1px solid var(--border);overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.06)}
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

.text-wrap{padding:2px 0}
.tl{
  padding:1px 12px 1px 0;
  font-family:'Cascadia Code','JetBrains Mono','Fira Code',Consolas,monospace;
  font-size:13px;line-height:1.7;
  border-left:3px solid transparent;
  white-space:pre-wrap;word-wrap:break-word;display:flex;
  transition:background .1s;
}
.tl:hover{background:var(--hover)}
.tl.active{background:var(--hl)!important;border-left-color:var(--hl-border)}

.ln{
  display:inline-block;min-width:36px;text-align:right;padding-right:10px;
  color:var(--text2);opacity:.4;font-size:11px;user-select:none;flex-shrink:0;
}
.lc{display:inline;} /* line content wrapper — keeps words inline */
.lc .tg-a + .tw .tg-a,
.lc .tg-d + .tw .tg-d {margin-left:-1px}

.tw{border-radius:2px;padding:0 1px}
.tw[data-m="0"]{background:#fff3cd;border-bottom:2px dashed #d4a017;border-radius:2px}
.tw[data-m="0"] .tg-a{background:#fde68a}
.tw[data-m="0"] .tg-d{background:#fde68a}
.tw.sel{background:var(--hl);outline:1px solid var(--hl-border)}
::selection{background:rgba(59,130,246,.3)}

.tg-a{color:var(--green);background:var(--green-bg);border-radius:2px;padding:0 1px;
      text-decoration:underline;text-decoration-color:var(--green);text-underline-offset:2px;
      text-decoration-thickness:1.5px;text-decoration-skip-ink:none}
.tg-d{color:var(--red);background:var(--red-bg);text-decoration:line-through;border-radius:2px;padding:0 1px;
      text-decoration-color:var(--red);text-decoration-thickness:1.5px}
.tag-marker{}

/* Edit mode */
.edit-area{
  display:none;width:100%;height:calc(100vh - 86px);
  font-family:'Cascadia Code',Consolas,monospace;font-size:13px;line-height:1.7;
  padding:8px 12px;border:none;outline:none;resize:none;
  background:var(--bg);color:var(--text);
}
.edit-area.vis{display:block}
.text-wrap.hid{display:none}

::-webkit-scrollbar{width:7px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:#c1c8ce;border-radius:4px}
::-webkit-scrollbar-thumb:hover{background:#9ca3ab}
</style>
</head>
<body>

<div class="top-bar">
  <span class="logo">📄 PDF ↔ Text Comparator</span>
  <span class="sep"></span>
  <span class="hint">Click or drag words to highlight on PDF · Click PDF regions to jump to text</span>
  <span class="stat" id="statBar">Ready</span>
  <button class="btn" id="editBtn" onclick="toggleEdit()">✏️ Edit</button>
  <button class="btn" id="saveBtn" onclick="saveEdit()" style="display:none">💾 Save</button>
  <button class="btn" id="cancelBtn" onclick="cancelEdit()" style="display:none">❌ Cancel</button>
  <button class="btn" id="dlBtn" onclick="downloadText()">⬇️ Download</button>
</div>

<div class="panels">
  <div class="panel panel-pdf" id="pdfPanel">
    <div class="ph"><span>ORIGINAL PDF</span><span class="badge" id="pageBadge"></span></div>
    <div class="pdf-wrap" id="pdfWrap"></div>
  </div>
  <div class="panel panel-text" id="textPanel">
    <div class="ph"><span>PYMUPDF CLEAN TEXT</span><span class="badge" id="lineBadge"></span></div>
    <div class="text-wrap" id="textWrap">__TEXT_BLOCK__</div>
    <textarea class="edit-area" id="editArea"></textarea>
  </div>
</div>

<script>
var pageImages=[__IMGS_JS__];
var wordMap=__WORDMAP_JS__;
var S=__SCALE__;
var originalLines=__LINES_JS__;

// Build PDF pages
var wrap=document.getElementById('pdfWrap'),containers=[];
pageImages.forEach(function(b64,idx){
  var c=document.createElement('div');c.className='page-ctr';c.id='pg_'+idx;
  var l=document.createElement('div');l.className='page-lbl';l.textContent='Page '+(idx+1);
  var i=document.createElement('img');i.src='data:image/png;base64,'+b64;i.id='img_'+idx;
  c.appendChild(l);c.appendChild(i);wrap.appendChild(c);containers.push(c);
});
document.getElementById('pageBadge').textContent=pageImages.length+' pages';
var tw=document.querySelectorAll('.tw').length;
document.getElementById('lineBadge').textContent=Object.keys(wordMap).length+'/'+tw+' words mapped';

// Group by line for PDF click zones
var lg={};
Object.keys(wordMap).forEach(function(k){var li=k.split('_')[0];if(!lg[li])lg[li]=[];lg[li].push(wordMap[k]);});

function buildZones(){
  document.querySelectorAll('.cz').forEach(function(z){z.remove();});
  Object.keys(lg).forEach(function(li){
    var ws=lg[li],pg=ws[0].page,ctr=containers[pg],img=ctr.querySelector('img');
    if(!img||!img.naturalWidth)return;
    var ds=img.clientWidth/img.naturalWidth;
    var x0=1e9,y0=1e9,x1=0,y1=0;
    ws.forEach(function(w){if(w.x0<x0)x0=w.x0;if(w.y0<y0)y0=w.y0;if(w.x1>x1)x1=w.x1;if(w.y1>y1)y1=w.y1;});
    var z=document.createElement('div');z.className='cz';
    z.style.left=(x0*S*ds)+'px';z.style.top=(y0*S*ds+img.offsetTop)+'px';
    z.style.width=((x1-x0)*S*ds)+'px';z.style.height=((y1-y0)*S*ds)+'px';
    z.onclick=function(){clickPdfLine(parseInt(li));};
    ctr.appendChild(z);
  });
}
var fi=document.querySelector('.page-ctr img');
if(fi){if(fi.complete)setTimeout(buildZones,100);else fi.onload=function(){setTimeout(buildZones,100);};}
window.addEventListener('resize',function(){clearHL();buildZones();});

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
    var ctr=containers[r.page],img=ctr.querySelector('img');if(!img)return;
    var ds=img.clientWidth/img.naturalWidth;
    var el=document.createElement('div');el.className='hl-overlay';
    el.style.left=(r.x0*S*ds)+'px';el.style.top=(r.y0*S*ds+img.offsetTop)+'px';
    el.style.width=((r.x1-r.x0)*S*ds)+'px';el.style.height=((r.y1-r.y0)*S*ds)+'px';
    ctr.appendChild(el);hlEls.push(el);
  });
  hlQueue=[];
}
function scrollPdf(wd){
  var ctr=containers[wd.page],img=ctr.querySelector('img');if(!img)return;
  var ds=img.clientWidth/img.naturalWidth;
  document.getElementById('pdfPanel').scrollTo({top:ctr.offsetTop+wd.y0*S*ds-120,behavior:'smooth'});
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
  var tl=document.querySelector('.tl[data-li="'+li+'"]');
  if(tl){tl.classList.add('active');tl.scrollIntoView({behavior:'smooth',block:'center'});
    tl.querySelectorAll('.tw').forEach(function(t){
      t.classList.add('sel');var k=t.dataset.li+'_'+t.dataset.wi,wd=wordMap[k];if(wd)hlWord(wd,t.dataset.li);
    });
    flushHL();
  }
  document.getElementById('statBar').textContent='PDF → Line '+(parseInt(li)+1);
}

// Edit mode
function toggleEdit(){
  var tw=document.getElementById('textWrap'),ea=document.getElementById('editArea');
  ea.value=originalLines.join('\n');
  tw.classList.add('hid');ea.classList.add('vis');
  document.getElementById('editBtn').style.display='none';
  document.getElementById('saveBtn').style.display='';
  document.getElementById('cancelBtn').style.display='';
  document.getElementById('statBar').textContent='Editing…';
}
function saveEdit(){
  var ea=document.getElementById('editArea');
  var newText=ea.value;
  originalLines=newText.split('\n');
  // Rebuild the text panel WITH tag styling
  var tw=document.getElementById('textWrap');
  tw.innerHTML=renderLinesWithTags(originalLines);
  tw.classList.remove('hid');
  document.getElementById('editArea').classList.remove('vis');
  document.getElementById('editBtn').style.display='';
  document.getElementById('saveBtn').style.display='none';
  document.getElementById('cancelBtn').style.display='none';
  document.getElementById('statBar').textContent='✅ Changes applied';
}
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
function cancelEdit(){
  document.getElementById('textWrap').classList.remove('hid');
  document.getElementById('editArea').classList.remove('vis');
  document.getElementById('editBtn').style.display='';
  document.getElementById('saveBtn').style.display='none';
  document.getElementById('cancelBtn').style.display='none';
  document.getElementById('statBar').textContent='Edit cancelled';
}
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
    pdf_images_b64: List[str],
    clean_lines: List[str],
    word_map: Dict[str, dict],
    dpi: int = PDF_RENDER_DPI,
    filename: str = "edited_clean.txt",
) -> str:
    scale = dpi / 72.0

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
    imgs_js = ",".join('"' + b + '"' for b in pdf_images_b64)
    wordmap_js = json.dumps(word_map)
    lines_js = json.dumps(clean_lines)

    return (_TEMPLATE
            .replace("__TEXT_BLOCK__", text_block)
            .replace("__IMGS_JS__", imgs_js)
            .replace("__WORDMAP_JS__", wordmap_js)
            .replace("__LINES_JS__", lines_js)
            .replace("__SCALE__", str(scale))
            .replace("__FILENAME__", filename))


# ── Streamlit app ────────────────────────────────────────────────────────────

def main():
    st.markdown("""<style>
        #MainMenu, header, footer, .stDeployButton { visibility: hidden; }
        .block-container { padding: 0.5rem 1rem 0; max-width: 100%; }
    </style>""", unsafe_allow_html=True)

    st.markdown("## 📄 PDF ↔ PyMuPDF Clean Text Comparator")

    c1, c2 = st.columns(2)
    with c1:
        pdf_file = st.file_uploader("Upload PDF", type=["pdf"], key="pdf")
    with c2:
        txt_file = st.file_uploader("Upload PyMuPDF Clean Text (.txt)", type=["txt"], key="txt")

    if not pdf_file or not txt_file:
        st.info("Upload both a **PDF** and the corresponding **\\_pymupdf\\_clean.txt** to begin.")
        return

    pdf_bytes = pdf_file.read()
    clean_text = txt_file.read().decode("utf-8", errors="replace")
    clean_lines = clean_text.splitlines()

    with st.spinner("Rendering pages & mapping words…"):
        images = render_pdf_pages(pdf_bytes)
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

    st.success(
        "✅  {} pages · {} PDF words · {} text words mapped".format(
            len(images), len(words), len(word_map)
        )
    )

    html = build_viewer_html(images, display_lines, word_map, filename=txt_file.name)
    st.components.v1.html(html, height=850)


if __name__ == "__main__":
    main()
