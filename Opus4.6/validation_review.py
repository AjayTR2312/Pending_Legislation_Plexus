"""
Validation Review UI v3 — Clean, professional Streamlit app for business users.

Design principles:
  - Tabs to organise content (no endless scrolling)
  - Clean visual hierarchy (summary -> review -> checklist)
  - Native Streamlit widgets (no heavy base64 HTML overlays)
  - Graceful error handling (file locks, missing data)

Launch:
    streamlit run validation_review.py --server.port 8503
"""
from __future__ import annotations

import json, re
from pathlib import Path
from typing import Optional

import streamlit as st
import openpyxl
from PIL import Image, ImageDraw, ImageFont

# ── Page config ─────────────────────────────────────────────────────
st.set_page_config(
    page_title="Validation Review",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="expanded",
)

OPUS_DIR = Path(__file__).resolve().parent
EXCEL_GLOB = "*_gpt_comparison.xlsx"
PDF_PTS = (612.0, 792.0)


# ═══════════════════════════════════════════════════════════════════
# CLEAN CSS
# ═══════════════════════════════════════════════════════════════════
st.markdown("""<style>
.stApp { font-family: 'Segoe UI', system-ui, sans-serif; }
section[data-testid="stSidebar"] { background: #0f172a; }
section[data-testid="stSidebar"] * { color: #e2e8f0 !important; }

/* summary cards */
.sstrip { display:flex; gap:10px; flex-wrap:wrap; margin:4px 0 12px; }
.sc { flex:1; min-width:90px; padding:12px 16px; border-radius:8px;
      text-align:center; box-shadow:0 1px 3px rgba(0,0,0,.06); }
.sc b { display:block; font-size:1.6rem; line-height:1.2; }
.sc small { font-size:.7rem; text-transform:uppercase; letter-spacing:.04em; opacity:.85; }
.sc-t { background:#e0e7ff; color:#3730a3; }
.sc-p { background:#dcfce7; color:#166534; }
.sc-w { background:#fef9c3; color:#854d0e; }
.sc-e { background:#fee2e2; color:#991b1b; }
.sc-f { background:#dbeafe; color:#1e40af; }

/* error nav pill */
.epill { display:inline-flex; align-items:center; gap:8px;
         background:#fef3c7; border:1.5px solid #f59e0b; border-radius:20px;
         padding:6px 18px; font-weight:600; font-size:.88rem; color:#92400e; }

/* detail card */
.dc { border-radius:8px; padding:14px 18px; margin-bottom:10px;
      box-shadow:0 1px 3px rgba(0,0,0,.06); }
.dc h5 { margin:0 0 6px; font-size:.95rem; }
.dc p  { margin:2px 0; font-size:.84rem; }
.dc-e { background:#fef2f2; border-left:4px solid #ef4444; }
.dc-w { background:#fffbeb; border-left:4px solid #f59e0b; }
.dc-p { background:#f0fdf4; border-left:4px solid #22c55e; }
.dc-f { background:#eff6ff; border-left:4px solid #3b82f6; }

/* text viewer */
.tv { font-family:'Cascadia Code','Consolas',monospace; font-size:.8rem;
      line-height:1.6; white-space:pre-wrap; word-break:break-word;
      background:#fafafa; border:1px solid #e2e8f0; border-radius:8px;
      padding:14px; max-height:600px; overflow-y:auto; }
.ta { background:#bbf7d0; color:#166534; padding:1px 2px; border-radius:3px; }
.td { background:#fecaca; color:#991b1b; padding:1px 2px; border-radius:3px; text-decoration:line-through; }
.hp { background:#86efac; padding:1px 3px; border-radius:3px; }
.hw { background:#fde047; padding:1px 3px; border-radius:3px; }
.he { background:#fca5a5; padding:1px 3px; border-radius:3px; }
mark.sel { background:#3b82f6!important; color:#fff!important; padding:2px 4px; border-radius:4px; }

/* table */
.ct { width:100%; border-collapse:collapse; font-size:.8rem; }
.ct th { background:#0f172a; color:#fff; padding:8px 10px; text-align:left;
         position:sticky; top:0; z-index:2; }
.ct td { padding:6px 10px; border-bottom:1px solid #e5e7eb; vertical-align:top; }
.ct tr:hover { background:#f0f9ff!important; }
.ct .rp { background:#f0fdf4; } .ct .rw { background:#fffbeb; }
.ct .re { background:#fef2f2; } .ct .rf { background:#eff6ff; }
.ct .rs { outline:2px solid #3b82f6; background:#eff6ff!important; }
.bdg { display:inline-block; padding:2px 8px; border-radius:10px;
       font-weight:600; font-size:.72rem; white-space:nowrap; }
.bp { background:#bbf7d0; color:#166534; } .bw { background:#fde68a; color:#854d0e; }
.be { background:#fecaca; color:#991b1b; } .bf { background:#93c5fd; color:#1e3a8a; }

/* findings */
.af { border-left:4px solid; border-radius:6px; padding:10px 14px;
      margin-bottom:8px; background:#fff; box-shadow:0 1px 2px rgba(0,0,0,.04); }
.af-e { border-color:#ef4444; } .af-w { border-color:#f59e0b; }

/* context preview */
.ctx-pre { font-family:'Cascadia Code','Consolas',monospace; font-size:.75rem;
           line-height:1.5; background:#1e293b; color:#e2e8f0; border-radius:6px;
           padding:8px 10px; max-height:200px; overflow-y:auto; margin-bottom:6px; }
.ctx-ln { color:#64748b; user-select:none; display:inline-block; width:36px;
          text-align:right; margin-right:8px; }
.ctx-err { background:#7f1d1d; color:#fecaca; border-radius:3px;
           display:block; margin:0 -10px; padding:0 10px; }
</style>""", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════
# DATA HELPERS
# ═══════════════════════════════════════════════════════════════════
def _discover(directory: Path) -> dict[str, dict]:
    bills: dict[str, dict] = {}
    for xlsx in sorted(directory.glob(EXCEL_GLOB)):
        stem = xlsx.stem.replace("_converted_gpt_comparison", "")
        base = directory / f"{stem}_converted"
        info = {"stem": stem, "excel": xlsx,
                "converted_txt": base.with_suffix(".txt"),
                "pymupdf_report": Path(str(base) + "_pymupdf_report.json"),
                "highlighted_dir": Path(str(base.parent / stem) + "_highlighted")}
        if info["converted_txt"].exists():
            bills[stem] = info
    return bills


@st.cache_data(ttl=30)
def _load_excel(path_str: str):
    p = Path(path_str)
    empty = [], [], {"total":0,"pass":0,"warn":0,"error":0,"additional":0}
    try:
        wb = openpyxl.load_workbook(str(p), read_only=True, data_only=True)
    except (PermissionError, Exception):
        return empty
    ws = wb["Checklist Audit"]
    rows = list(ws.iter_rows(min_row=4, values_only=True))
    if not rows: wb.close(); return empty
    hdr = [str(h).strip() if h else f"c{i}" for i, h in enumerate(rows[0])]
    cl = []
    for r in rows[1:]:
        if r[0] is None: continue
        cl.append({h: (r[i] if i < len(r) else "") for i, h in enumerate(hdr)})
    fd = []
    if "Additional Findings" in wb.sheetnames:
        ws2 = wb["Additional Findings"]
        r2 = list(ws2.iter_rows(min_row=3, values_only=True))
        if r2:
            h2 = [str(h).strip() if h else f"c{i}" for i, h in enumerate(r2[0])]
            for r in r2[1:]:
                if r[0] is None or str(r[0]).strip() == "—": continue
                fd.append({h: (r[i] if i < len(r) else "") for i, h in enumerate(h2)})
    np_ = sum(1 for x in cl if str(x.get("Verdict","")).startswith("✅"))
    nw = sum(1 for x in cl if str(x.get("Verdict","")).startswith("⚠"))
    ne = sum(1 for x in cl if str(x.get("Verdict","")).startswith("❌"))
    wb.close()
    return cl, fd, {"total":len(cl),"pass":np_,"warn":nw,"error":ne,"additional":len(fd)}


def _load_json(p: Path) -> dict:
    if p.exists():
        with open(p, "r", encoding="utf-8") as f: return json.load(f)
    return {}

def _page_imgs(d: Path) -> list[Path]:
    return sorted(d.glob("page_*.png")) if d.exists() else []

def _load_text(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""


# ═══════════════════════════════════════════════════════════════════
# ANNOTATIONS
# ═══════════════════════════════════════════════════════════════════
def _nw(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())

def _build_anns(cl, jr, fi) -> dict[int, list[dict]]:
    spans = jr.get("formatted_spans", [])
    out: dict[int, list[dict]] = {}
    for row in cl:
        cid = _si(row.get("#", 0)); v = str(row.get("Verdict", ""))
        pg = _si(row.get("Page", 0)); txt = _nw(str(row.get("PyMuPDF Text", "")))
        if cid in fi:            vt = "fixed"
        elif v.startswith("✅"): vt = "pass"
        elif v.startswith("⚠"):  vt = "warn"
        else:                     vt = "err"
        if vt == "pass": continue
        bbox = None
        for sp in spans:
            if sp.get("page") != pg: continue
            st2 = _nw(sp.get("text", ""))
            if st2 == txt or txt in st2 or st2 in txt: bbox = sp.get("bbox"); break
        if bbox and pg > 0:
            out.setdefault(pg, []).append(
                {"bbox": bbox, "vtype": vt, "cid": cid,
                 "text": str(row.get("PyMuPDF Text", ""))[:50]})
    return out


def _annotate(img_path: Path, anns: list[dict], sel: Optional[int]) -> Image.Image:
    img = Image.open(img_path).convert("RGBA")
    if not anns: return img.convert("RGB")
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    sx, sy = img.size[0] / PDF_PTS[0], img.size[1] / PDF_PTS[1]
    C = {"err": ((184,134,11),(255,255,0,70)),
         "warn": ((234,138,0),(255,165,0,40)),
         "fixed": ((37,99,235),(59,130,246,30))}
    try:    fn = ImageFont.truetype("arial.ttf", 15)
    except: fn = ImageFont.load_default()
    for a in anns:
        b = a["bbox"]; vt = a["vtype"]; ci = a["cid"]; issel = ci == sel
        x0,y0,x1,y1 = int(b[0]*sx)-4, int(b[1]*sy)-4, int(b[2]*sx)+4, int(b[3]*sy)+4
        ol, fl = C.get(vt, C["err"])
        d.rectangle([x0,y0,x1,y1], fill=fl, outline=ol, width=4 if issel else 2)
        lab = f"#{ci}" + (" ❌" if vt=="err" else " ⚠" if vt=="warn" else " ✅")
        lw,lh = len(lab)*9+8, 20; ly = max(0, y0-lh-2)
        bg = (245,158,11,230) if vt=="err" else (251,146,60,210) if vt=="warn" else (59,130,246,210)
        d.rectangle([x0,ly,x0+lw,ly+lh], fill=bg)
        d.text((x0+4,ly+2), lab, fill=(255,255,255), font=fn)
        if issel:
            for o in (1,2,3):
                d.rectangle([x0-o,y0-o,x1+o,y1+o], outline=(59,130,246,200-o*50), width=2)
    return Image.alpha_composite(img, ov).convert("RGB")





# ═══════════════════════════════════════════════════════════════════
# TEXT RENDERING
# ═══════════════════════════════════════════════════════════════════
def _esc(t: str) -> str:
    return t.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def _render_text(text, cl, sel_id, filt, fi):
    hls = []
    for r in cl:
        v = str(r.get("Verdict","")); cid = _si(r.get("#",0))
        if filt=="errors" and not v.startswith("❌"): continue
        if filt=="warnings" and not v.startswith("⚠"): continue
        pt = str(r.get("PyMuPDF Text","")).strip()
        ct = str(r.get("Claude Text","")).strip()
        pg = _si(r.get("Page", 0))
        if cid in fi:            css = "hp"
        elif v.startswith("✅"): css = "hp"
        elif v.startswith("⚠"):  css = "hw"
        else:                     css = "he"
        hls.append({"s": ct or pt, "css": css, "cid": cid, "sel": cid==sel_id, "pg": pg})
    s = _esc(text)

    # Build page-offset map for accurate positioning
    page_starts: dict[int, int] = {}
    for m in re.finditer(r'---\s*Page\s+(\d+)\s*---', s, re.IGNORECASE):
        page_starts[int(m.group(1))] = m.start()

    # Apply highlights BEFORE tag wrapping so [A>…<A] text still matches
    for h in sorted(hls, key=lambda x: x["sel"]):
        n = _esc(h["s"])
        if not n: continue
        t1 = f'<mark class="sel" id="sp-{h["cid"]}">' if h["sel"] else f'<span class="{h["css"]}">'
        t2 = "</mark>" if h["sel"] else "</span>"
        # Search from the correct page position
        start = page_starts.get(h["pg"], 0)
        idx = s.find(n, start)
        if idx >= 0:
            s = s[:idx] + f"{t1}{n}{t2}" + s[idx + len(n):]
        else:
            # Fallback: first occurrence anywhere
            s = s.replace(n, f"{t1}{n}{t2}", 1)

    # Tag wrapping AFTER highlights (nests inside highlight spans)
    s = re.sub(r"\[A&gt;(.*?)&lt;A\]", r'<span class="ta">[A&gt;\1&lt;A]</span>', s, flags=re.DOTALL)
    s = re.sub(r"\[D&gt;(.*?)&lt;D\]", r'<span class="td">[D&gt;\1&lt;D]</span>', s, flags=re.DOTALL)
    return s


# ═══════════════════════════════════════════════════════════════════
# SMALL HELPERS
# ═══════════════════════════════════════════════════════════════════
def _si(v, d=0):
    try: return int(v)
    except: return d

def _tr(s, n=80):
    s = str(s) if s else ""; return s if len(s)<=n else s[:n]+"…"

def _fixed(): return set(st.session_state.get("fixed_ids", []))

def _mark_fixed(cid):
    f = list(st.session_state.get("fixed_ids", []))
    if cid not in f: f.append(cid)
    st.session_state["fixed_ids"] = f

def _ev(row, fi):
    return "✅ fixed" if _si(row.get("#",0)) in fi else str(row.get("Verdict",""))

def _eids(cl, fi):
    return [_si(r.get("#",0)) for r in cl
            if _si(r.get("#",0)) not in fi
            and str(r.get("Verdict","")).startswith("❌")]


def _find_error_context(text: str, srow: dict, margin: int = 7):
    """Find error text in file and return (ctx_start, ctx_end, error_line, lines).
    Returns (-1, -1, -1, []) if not found."""
    lines = text.split('\n')
    ct = str(srow.get("Claude Text", "") or "").strip()
    pt = str(srow.get("PyMuPDF Text", "") or "").strip()
    pg = _si(srow.get("Page", 0))

    # Build page→line_index map
    page_line: dict[int, int] = {}
    for i, ln in enumerate(lines):
        m = re.match(r'---\s*Page\s+(\d+)\s*---', ln, re.IGNORECASE)
        if m:
            page_line[int(m.group(1))] = i

    # Search start: begin at the right page if possible
    search_from = page_line.get(pg, 0)

    def _find_line(needle: str, start: int = 0) -> int:
        for i in range(start, len(lines)):
            if needle in lines[i]:
                return i
        # Fallback: search from beginning
        for i in range(0, start):
            if needle in lines[i]:
                return i
        return -1

    # Try multiple search strategies
    idx = -1
    if ct:
        idx = _find_line(ct, search_from)
    if idx < 0 and pt:
        # Try tagged forms
        for tag in [f"[A>{pt}", f"[D>{pt}", pt]:
            idx = _find_line(tag, search_from)
            if idx >= 0:
                break

    if idx < 0:
        return -1, -1, -1, lines

    cs = max(0, idx - margin)
    ce = min(len(lines), idx + margin + 1)
    return cs, ce, idx, lines


def _pg_prev():
    st.session_state.psl = max(1, st.session_state.get("psl", 1) - 1)

def _pg_next(npg):
    st.session_state.psl = min(npg, st.session_state.get("psl", 1) + 1)


# ═══════════════════════════════════════════════════════════════════
# SIDEBAR
# ═══════════════════════════════════════════════════════════════════
def _sidebar():
    with st.sidebar:
        st.markdown("## 🔍 Validation Review")
        st.caption("Review audit results  •  Fix errors")
        st.divider()

        bills = _discover(OPUS_DIR)
        for pd in OPUS_DIR.parent.glob("PHASE_*"):
            bills.update(_discover(pd))
        if not bills: st.error("No audit results found."); st.stop()

        names = list(bills.keys())
        labels = [f"Bill {next((p for p in n.split('_') if len(p)>=5 and p.isdigit()), n)}" for n in names]

        st.markdown("#### 📁 Select Bill")
        idx = st.selectbox("Bill", range(len(names)),
                           format_func=lambda i: labels[i], key="bill_idx")
        bill = bills[names[idx]]

        st.divider()
        st.markdown("#### 🔎 Filter")
        filt = st.radio("Show", ["all","errors","warnings"],
                        format_func={"all":"📋 All","errors":"❌ Errors","warnings":"⚠️ Warnings"}.get,
                        key="filt", horizontal=True)

        st.divider()
        with st.expander("💡 How to use", expanded=False):
            st.markdown("""
1. Pick a bill above
2. Go to **📄 Review** tab
3. Use **◀ Prev / Next ▶** to step through errors
4. Yellow boxes on the PDF show where each error is
5. Edit & apply fixes in the panel below the image
""")
        st.caption(f"📂 {bill['excel'].parent.name}")
    return bill, filt


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════
def main():
    qp = st.query_params
    if "item" in qp:
        try: st.session_state.selected_item = int(qp["item"])
        except: pass
        st.query_params.clear()

    bill, filt = _sidebar()
    fi = _fixed()

    # Load data
    cl, fd, su = _load_excel(str(bill["excel"]))
    text = _load_text(bill["converted_txt"])
    imgs = _page_imgs(bill.get("highlighted_dir", Path()))
    jr = _load_json(bill.get("pymupdf_report", Path()))
    npg = len(imgs)

    # Header
    bnum = next((p for p in bill["stem"].split("_") if len(p)>=5 and p.isdigit()), "")
    st.markdown(f"# 🔍 Bill {bnum}")

    # Summary
    nf = sum(1 for r in cl if _si(r.get("#",0)) in fi)
    ep = su["pass"]+nf; ee = max(0, su["error"]-nf)
    fixed_card = f'<div class="sc sc-f"><b>{nf}</b><small>Fixed</small></div>' if nf else ''
    st.markdown(
        f'<div class="sstrip">'
        f'<div class="sc sc-t"><b>{su["total"]}</b><small>Total</small></div>'
        f'<div class="sc sc-p"><b>{ep}</b><small>Pass</small></div>'
        f'<div class="sc sc-w"><b>{su["warn"]}</b><small>Warn</small></div>'
        f'<div class="sc sc-e"><b>{ee}</b><small>Errors</small></div>'
        f'{fixed_card}</div>',
        unsafe_allow_html=True)
    tot = su["total"] or 1
    st.progress(ep/tot, text=f"Pass rate {ep/tot:.0%}  ({ep}/{tot})")

    # Selected item
    if "selected_item" not in st.session_state:
        errs = [r for r in cl if str(r.get("Verdict","")).startswith("❌")]
        wrns = [r for r in cl if str(r.get("Verdict","")).startswith("⚠")]
        first = errs or wrns or cl
        st.session_state.selected_item = _si(first[0].get("#",1)) if first else 0
    sid = st.session_state.selected_item
    srow = next((r for r in cl if _si(r.get("#",0))==sid), None)

    # Error nav
    eids = _eids(cl, fi)
    epos = (eids.index(sid)+1) if sid in eids else 0
    etot = len(eids)
    if eids:
        _,nc2,nc3,nc4,_ = st.columns([1,2,4,2,1])
        with nc2:
            if st.button("◀  Prev Error", key="pe", width="stretch"):
                st.session_state.selected_item = eids[(epos-2)%etot]; st.rerun()
        with nc3:
            ptxt = f"  •  Page {_si(srow.get('Page',1))}" if srow else ""
            st.markdown(f'<div style="text-align:center;padding:6px"><span class="epill">'
                        f'Error {epos}/{etot} — Item #{sid}{ptxt}</span></div>',
                        unsafe_allow_html=True)
        with nc4:
            if st.button("Next Error  ▶", key="ne", width="stretch"):
                st.session_state.selected_item = eids[epos%etot]; st.rerun()
    elif cl:
        st.success("🎉  All errors resolved!")

    # Annotations + page state
    pa = _build_anns(cl, jr, fi)
    spg = _si(srow.get("Page",1),1) if srow else 1
    if "psl" not in st.session_state: st.session_state.psl = spg
    if st.session_state.get("_ls") != sid:
        st.session_state.psl = spg; st.session_state["_ls"] = sid

    # Filter
    if filt=="errors":
        disp = [r for r in cl if str(r.get("Verdict","")).startswith("❌") and _si(r.get("#",0)) not in fi]
    elif filt=="warnings":
        disp = [r for r in cl if str(r.get("Verdict","")).startswith("⚠")]
    else:
        disp = cl

    # ═════════════════════════════════════════════════════════
    # TABS
    # ═════════════════════════════════════════════════════════
    t1, t2, t3 = st.tabs([
        f"📄 Review ({ee} errors)" if ee else "📄 Review",
        f"📋 Checklist ({su['total']})",
        f"🔔 Findings ({len(fd)})" if fd else "🔔 Findings"])

    # ── TAB 1: REVIEW ──────────────────────────────────────
    with t1:
        lc, rc = st.columns([1, 1], gap="medium")

        # LEFT — PDF
        with lc:
            st.markdown("##### 📄 Highlighted PDF Page")
            if npg:
                cur = max(1, min(st.session_state.psl, npg))
                p1,p2,p3,p4 = st.columns([1,1,6,1])
                with p1:
                    st.button("◀", key="ppv", help="Previous page",
                              on_click=_pg_prev)
                with p2: st.markdown(f"**{cur}** / {npg}")
                with p3:
                    st.slider("Page",1,npg,key="psl",label_visibility="collapsed")
                with p4:
                    st.button("▶", key="pnx", help="Next page",
                              on_click=_pg_next, args=(npg,))
                cur = st.session_state.psl
                ix = cur - 1
                if 0 <= ix < len(imgs):
                    anns = pa.get(cur, [])
                    st.image(_annotate(imgs[ix], anns, sid), width="stretch")

                    # Per-page error buttons
                    perrs = [a for a in anns if a["vtype"] == "err"]
                    if perrs:
                        st.caption("👆 Jump to error on this page:")
                        cols = st.columns(min(len(perrs), 4))
                        for i, a in enumerate(perrs):
                            with cols[i%len(cols)]:
                                tp = "primary" if a["cid"]==sid else "secondary"
                                if st.button(f'❌ #{a["cid"]}', key=f'ec_{a["cid"]}',
                                             type=tp, width="stretch"):
                                    st.session_state.selected_item = a["cid"]; st.rerun()
            else:
                st.info("No highlighted page images available.")

        # RIGHT — Text + Inline Editor
        with rc:
            cid = _si(srow.get("#", 0)) if srow else 0
            ov = str(srow.get("Verdict", "")) if srow else ""
            is_err = ov.startswith("❌") and cid not in fi

            if is_err and srow:
                # ── Error detail card (compact) ──
                notes = str(srow.get("Notes", ""))
                sf = str(srow.get("Suggested Fix", ""))
                pt = str(srow.get("PyMuPDF Text", ""))
                ttype = str(srow.get("Tag Type", ""))
                card_html = (
                    f'<div class="dc dc-e">'
                    f'<h5>❌ Item #{cid} — Page {_esc(str(srow.get("Page","")))}</h5>'
                    f'<p><b>Type:</b> {_esc(ttype)}&emsp;'
                    f'<b>Expected:</b> <code>{_esc(_tr(pt, 60))}</code></p>'
                    f'{"<p>📝 " + _esc(notes) + "</p>" if notes else ""}'
                    f'{"<p>🔧 " + _esc(sf) + "</p>" if sf else ""}'
                    f'</div>'
                )
                st.markdown(card_html, unsafe_allow_html=True)

                # ── Context editor ──
                cs, ce, err_ln, lines = _find_error_context(text, srow)
                if cs >= 0:
                    ctx_text = '\n'.join(lines[cs:ce])

                    # Highlighted preview showing which line has the error
                    preview_parts = []
                    for li in range(cs, ce):
                        ln_num = f'<span class="ctx-ln">{li+1}</span>'
                        ln_text = _esc(lines[li])
                        if li == err_ln:
                            preview_parts.append(
                                f'<span class="ctx-err">{ln_num}{ln_text}</span>')
                        else:
                            preview_parts.append(f'{ln_num}{ln_text}')
                    preview_html = '\n'.join(preview_parts)
                    st.markdown(
                        f'##### ✏️ Edit in context  ·  error on line {err_ln+1}')
                    st.markdown(
                        f'<div class="ctx-pre">{preview_html}</div>',
                        unsafe_allow_html=True)
                    st.caption("👆 Red line = error location  ·  Edit below 👇")
                    edited = st.text_area(
                        "edit", value=ctx_text, height=300,
                        key=f"ctx_{cid}", label_visibility="collapsed")

                    bc1, bc2, bc3 = st.columns([2, 2, 1])
                    with bc1:
                        if st.button("💾 Save & Mark Fixed", type="primary",
                                     key=f"sv_{cid}", width="stretch"):
                            edited_lines = edited.split('\n')
                            new_lines = lines[:cs] + edited_lines + lines[ce:]
                            bill["converted_txt"].write_text(
                                '\n'.join(new_lines), encoding="utf-8")
                            _load_excel.clear()
                            _mark_fixed(cid); st.rerun()
                    with bc2:
                        if st.button("💾 Save Only", key=f"so_{cid}",
                                     width="stretch"):
                            edited_lines = edited.split('\n')
                            new_lines = lines[:cs] + edited_lines + lines[ce:]
                            bill["converted_txt"].write_text(
                                '\n'.join(new_lines), encoding="utf-8")
                            _load_excel.clear(); st.rerun()
                    with bc3:
                        if st.button("⏭️ Skip", key=f"sk_{cid}",
                                     width="stretch"):
                            if eids and epos <= etot:
                                nxt = eids[epos % etot]
                            else:
                                nxt = sid
                            st.session_state.selected_item = nxt
                            st.rerun()
                else:
                    # Error text not found — show full viewer + manual mark
                    st.markdown("##### 📝 Converted Output")
                    html = _render_text(text, cl, sid, filt, fi)
                    st.markdown(f'<div class="tv">{html}</div>',
                                unsafe_allow_html=True)
                    st.warning("⚠️ Could not locate error text in file.")
                    if st.button("📋 Mark Fixed Manually", key=f"mf_{cid}",
                                 width="stretch"):
                        _mark_fixed(cid); st.rerun()

            else:
                # ── No error / pass / fixed / warning — read-only viewer ──
                st.markdown("##### 📝 Converted Output")
                if srow and cid in fi:
                    st.success(f"✅ Item #{cid} fixed.")
                html = _render_text(text, cl, sid, filt, fi)
                st.markdown(f'<div class="tv">{html}</div>',
                            unsafe_allow_html=True)

    # ── TAB 2: CHECKLIST ───────────────────────────────────
    with t2:
        if not disp:
            st.info("No items match the filter.")
        else:
            nc1,nc2,nc3,nc4 = st.columns([1,2,2,1])
            with nc2:
                if st.button("⬆️ Previous", key="tp", width="stretch"):
                    ids=[_si(r.get("#",0)) for r in disp]
                    ci=ids.index(sid) if sid in ids else 0
                    st.session_state.selected_item=ids[max(0,ci-1)]; st.rerun()
            with nc3:
                if st.button("⬇️ Next", key="tn", width="stretch"):
                    ids=[_si(r.get("#",0)) for r in disp]
                    ci=ids.index(sid) if sid in ids else 0
                    st.session_state.selected_item=ids[min(len(ids)-1,ci+1)]; st.rerun()

            tb = '<table class="ct"><thead><tr>'
            for h in ["#","Status","Pg","Type","Expected Text","Found?","Output Text","Category","Notes"]:
                tb += f"<th>{h}</th>"
            tb += "</tr></thead><tbody>"
            for r in disp:
                ci = _si(r.get("#",0)); v = _ev(r, fi)
                if ci in fi:             rc,bc="rf","bf"
                elif v.startswith("✅"): rc,bc="rp","bp"
                elif v.startswith("⚠"):  rc,bc="rw","bw"
                else:                     rc,bc="re","be"
                if ci==sid: rc+=" rs"
                tb+=f'<tr class="{rc}">'
                tb+=f'<td><b>{ci}</b></td>'
                tb+=f'<td><span class="bdg {bc}">{_esc(v)}</span></td>'
                tb+=f'<td>{r.get("Page","")}</td>'
                tb+=f'<td>{_esc(str(r.get("Tag Type","")))}</td>'
                tb+=f'<td>{_esc(_tr(r.get("PyMuPDF Text","")))}</td>'
                tb+=f'<td>{_esc(str(r.get("In Claude?","")))}</td>'
                tb+=f'<td>{_esc(_tr(r.get("Claude Text","")))}</td>'
                tb+=f'<td>{_esc(str(r.get("Classification","")))}</td>'
                tb+=f'<td>{_esc(_tr(r.get("Notes",""),100))}</td></tr>'
            tb+="</tbody></table>"
            st.markdown(tb, unsafe_allow_html=True)

            _,jc,_ = st.columns([3,2,3])
            with jc:
                opts=[_si(r.get("#",0)) for r in disp]
                j=st.selectbox("Jump to #", opts,
                               index=opts.index(sid) if sid in opts else 0, key="jto")
                if j!=sid: st.session_state.selected_item=j; st.rerun()

    # ── TAB 3: FINDINGS ────────────────────────────────────
    with t3:
        if not fd: st.info("No additional findings.")
        else:
            for af in fd:
                it=str(af.get("Issue Type",""))
                c="af-e" if it in ("content_dropped","tag_error","text_error") else "af-w"
                st.markdown(f'<div class="af {c}"><b>Page {af.get("Page","")}</b> — '
                            f'<em>{_esc(it)}</em><br>{_esc(str(af.get("Description","")))}</div>',
                            unsafe_allow_html=True)

    # Footer
    st.divider()
    st.caption(f"📊 {bill['excel'].name}  •  Items: {su['total']}  •  Fixed: {nf}")


if __name__ == "__main__":
    main()
