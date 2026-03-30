"""
Validation Review UI v2 — Enhanced Streamlit app for business-user-friendly
visual review of GPT audit results.

Features:
  ★ Yellow-highlighted errors on PDF page images
  ★ Clickable error cards with one-click navigation
  ★ Error-by-error navigator (← Prev Error | Next Error →)
  ★ Zoomed crop view of the selected error region
  ★ Side-by-side PDF page + Claude converted text
  ★ Inline fix editor for errors
  ★ Business-friendly language throughout

Launch:
    streamlit run validation_review.py --server.port 8503
"""
from __future__ import annotations

import base64
import io
import json
import re
from pathlib import Path

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

# ── Constants ───────────────────────────────────────────────────────
OPUS_DIR = Path(__file__).resolve().parent
EXCEL_GLOB = "*_gpt_comparison.xlsx"
PDF_PTS_PER_PAGE = (612.0, 792.0)  # standard US Letter in points


# ═══════════════════════════════════════════════════════════════════
# CUSTOM CSS — business-friendly, modern, accessible
# ═══════════════════════════════════════════════════════════════════
st.markdown("""
<style>
/* --- global --- */
.stApp { font-family: 'Segoe UI', sans-serif; }
section[data-testid="stSidebar"] { background: #1e293b; }
section[data-testid="stSidebar"] * { color: #e2e8f0 !important; }
section[data-testid="stSidebar"] .stSelectbox label,
section[data-testid="stSidebar"] .stRadio label { color: #94a3b8 !important; font-size:0.85rem; }

/* --- summary cards --- */
.metric-row { display:flex; gap:12px; margin-bottom:16px; flex-wrap:wrap; }
.metric-card {
    flex:1; min-width:100px; padding:16px 20px; border-radius:10px;
    text-align:center; box-shadow: 0 1px 4px rgba(0,0,0,.08);
}
.metric-card h2 { margin:0; font-size:2rem; line-height:1.2; }
.metric-card p  { margin:4px 0 0; font-size:0.8rem; text-transform:uppercase; letter-spacing:.05em; }
.mc-pass   { background:#dcfce7; color:#166534; }
.mc-warn   { background:#fef9c3; color:#854d0e; }
.mc-err    { background:#fee2e2; color:#991b1b; }
.mc-total  { background:#e0e7ff; color:#3730a3; }
.mc-fixed  { background:#dbeafe; color:#1e40af; }

/* ★ ERROR NAVIGATION BAR — prominent, always visible */
.error-nav-bar {
    display:flex; align-items:center; justify-content:center; gap:16px;
    background: linear-gradient(135deg, #fef3c7, #fde68a);
    border: 2px solid #f59e0b;
    border-radius: 12px; padding: 12px 24px; margin: 12px 0 16px;
    box-shadow: 0 2px 8px rgba(245,158,11,.2);
}
.error-nav-bar .nav-label {
    font-weight: 700; font-size: 1.05rem; color: #92400e;
}
.error-nav-bar .nav-page {
    font-size: 0.85rem; color: #78350f; opacity:0.8;
}

/* ★ PER-PAGE ERROR CARDS — clickable, yellow-highlighted */
.error-cards-row {
    display: flex; gap: 8px; flex-wrap: wrap; margin: 10px 0;
}
.error-card {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 8px 14px; border-radius: 8px;
    font-size: 0.82rem; font-weight: 600;
    cursor: pointer; transition: all 0.15s ease;
    border: 2px solid transparent;
    box-shadow: 0 1px 3px rgba(0,0,0,.08);
}
.error-card:hover { transform: translateY(-1px); box-shadow: 0 3px 8px rgba(0,0,0,.15); }
.error-card-err {
    background: #fef9c3; color: #92400e; border-color: #f59e0b;
}
.error-card-err:hover { background: #fde68a; }
.error-card-warn {
    background: #fff7ed; color: #9a3412; border-color: #fb923c;
}
.error-card-warn:hover { background: #fed7aa; }
.error-card-fixed {
    background: #dbeafe; color: #1e40af; border-color: #60a5fa;
}
.error-card-selected {
    border-color: #2563eb !important;
    background: #bfdbfe !important;
    color: #1e3a8a !important;
    box-shadow: 0 0 0 3px rgba(37,99,235,.3) !important;
}

/* ★ ZOOM VIEW — magnified crop of the error region */
.zoom-container {
    border: 2px solid #f59e0b; border-radius: 10px;
    overflow: hidden; background: white;
    box-shadow: 0 2px 8px rgba(245,158,11,.2);
    margin: 8px 0;
}
.zoom-header {
    background: #fef3c7; padding: 6px 12px;
    font-size: 0.8rem; font-weight: 600; color: #92400e;
    border-bottom: 1px solid #fde68a;
}
.zoom-container img { width: 100%; display:block; }

/* ★ INTERACTIVE IMAGE OVERLAY */
.overlay-container {
    position: relative; display: inline-block; width: 100%;
    border: 1px solid #e2e8f0; border-radius: 8px;
    overflow: hidden; background: white;
}
.overlay-container img { width: 100%; display: block; }
.overlay-hotspot {
    position: absolute;
    border: 3px solid #f59e0b;
    background: rgba(255, 235, 59, 0.25);
    border-radius: 4px;
    cursor: pointer;
    transition: all 0.15s ease;
    z-index: 10;
}
.overlay-hotspot:hover {
    background: rgba(255, 235, 59, 0.50);
    border-color: #d97706;
    box-shadow: 0 0 12px rgba(245,158,11,.4);
}
.overlay-hotspot .hotspot-label {
    position: absolute; top: -22px; left: 0;
    background: #f59e0b; color: white;
    font-size: 11px; font-weight: 700;
    padding: 2px 8px; border-radius: 4px;
    white-space: nowrap;
    box-shadow: 0 1px 3px rgba(0,0,0,.2);
}
.overlay-hotspot.hotspot-selected {
    border-color: #2563eb;
    background: rgba(59, 130, 246, 0.30);
    box-shadow: 0 0 0 4px rgba(37,99,235,.3);
    animation: pulse-blue 1.5s ease-in-out infinite alternate;
}
.overlay-hotspot.hotspot-selected .hotspot-label {
    background: #2563eb;
}
@keyframes pulse-blue {
    from { box-shadow: 0 0 0 4px rgba(37,99,235,.3); }
    to   { box-shadow: 0 0 0 8px rgba(37,99,235,0); }
}
.overlay-hotspot.hotspot-warn {
    border-color: #fb923c;
    background: rgba(251,146,60,0.20);
}
.overlay-hotspot.hotspot-warn .hotspot-label {
    background: #fb923c;
}

/* --- checklist table --- */
.cl-table { width:100%; border-collapse:collapse; font-size:0.82rem; }
.cl-table th {
    background:#1e293b; color:white; padding:8px 10px;
    text-align:left; position:sticky; top:0; z-index:2;
    border-bottom: 2px solid #334155;
}
.cl-table td { padding:7px 10px; border-bottom:1px solid #e2e8f0; vertical-align:top; }
.cl-table tr:hover { background:#f0f9ff !important; cursor:pointer; }
.cl-table tr.row-pass  { background:#f0fdf4; }
.cl-table tr.row-warn  { background:#fefce8; }
.cl-table tr.row-err   { background:#fef2f2; }
.cl-table tr.row-fixed { background:#dbeafe; }
.cl-table tr.row-selected { outline: 2px solid #3b82f6; background:#eff6ff !important; }
.badge {
    display:inline-block; padding:2px 8px; border-radius:12px;
    font-weight:600; font-size:0.75rem; white-space: nowrap;
}
.badge-pass  { background:#bbf7d0; color:#166534; }
.badge-warn  { background:#fde68a; color:#854d0e; }
.badge-err   { background:#fecaca; color:#991b1b; }
.badge-fixed { background:#93c5fd; color:#1e3a8a; }

/* --- text viewer --- */
.text-viewer {
    font-family: 'Cascadia Code','Consolas', monospace;
    font-size: 0.82rem; line-height: 1.65;
    white-space: pre-wrap; word-break: break-word;
    background: #fafafa; border: 1px solid #e2e8f0;
    border-radius: 8px; padding: 16px;
    max-height: 620px; overflow-y: auto;
}
.tag-add { background:#bbf7d0; color:#166534; padding:1px 2px; border-radius:3px; }
.tag-del { background:#fecaca; color:#991b1b; padding:1px 2px; border-radius:3px; text-decoration:line-through; }
.span-highlight-pass { background:#86efac; padding:1px 3px; border-radius:3px; }
.span-highlight-warn { background:#fde047; padding:1px 3px; border-radius:3px; }
.span-highlight-err  { background:#fca5a5; padding:1px 3px; border-radius:3px; }
mark.current-span {
    background:#60a5fa !important; color:white !important;
    padding: 2px 4px; border-radius: 4px;
    animation: pulse 1.5s ease-in-out infinite alternate;
}
@keyframes pulse { from {box-shadow: 0 0 0 0 rgba(96,165,250,.5)} to {box-shadow: 0 0 0 6px rgba(96,165,250,0)} }

/* --- page image (fallback non-overlay) --- */
.page-img-container {
    border: 1px solid #e2e8f0; border-radius: 8px;
    overflow: hidden; background: white;
    max-height: 700px; overflow-y: auto;
}
.page-img-container img { width: 100%; }

/* --- additional findings --- */
.af-card {
    border-left: 4px solid; border-radius: 6px;
    padding: 12px 16px; margin-bottom: 10px;
    background: white; box-shadow: 0 1px 3px rgba(0,0,0,.06);
}
.af-error  { border-color: #ef4444; }
.af-warn   { border-color: #f59e0b; }

/* --- inline edit --- */
div[data-testid="stTextArea"] textarea {
    font-family: 'Cascadia Code','Consolas', monospace !important;
    font-size: 0.85rem !important; line-height: 1.6 !important;
}
</style>
""", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════
# DATA LOADING
# ═══════════════════════════════════════════════════════════════════

def _discover_bills(directory: Path) -> dict[str, dict]:
    """Scan directory for sets of pipeline output files."""
    bills: dict[str, dict] = {}
    for xlsx in sorted(directory.glob(EXCEL_GLOB)):
        stem = xlsx.stem.replace("_converted_gpt_comparison", "")
        base = directory / f"{stem}_converted"
        info = {
            "stem": stem,
            "excel": xlsx,
            "converted_txt": base.with_suffix(".txt"),
            "pymupdf_report": Path(str(base) + "_pymupdf_report.json"),
            "pymupdf_clean": Path(str(base) + "_pymupdf_clean.txt"),
            "highlighted_dir": Path(str(base.parent / stem) + "_highlighted"),
        }
        if info["converted_txt"].exists():
            bills[stem] = info
    return bills


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
    n_err = sum(1 for r in checklist if str(r.get("Verdict", "")).startswith("❌"))
    summary = {
        "total": len(checklist), "pass": n_pass, "warn": n_warn,
        "error": n_err, "additional": len(findings),
    }
    wb.close()
    return checklist, findings, summary


def _load_json_report(json_path: Path) -> dict:
    if json_path.exists():
        with open(json_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _get_page_images(highlighted_dir: Path) -> list[Path]:
    if not highlighted_dir.exists():
        return []
    return sorted(highlighted_dir.glob("page_*.png"))


def _load_converted_text(txt_path: Path) -> str:
    if txt_path.exists():
        return txt_path.read_text(encoding="utf-8")
    return ""


# ═══════════════════════════════════════════════════════════════════
# IMAGE ANNOTATION — YELLOW error highlights on page images
# ═══════════════════════════════════════════════════════════════════

def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def _build_page_annotations(checklist: list[dict], json_report: dict,
                            fixed_ids: set[int]) -> dict[int, list[dict]]:
    """
    Match checklist items to JSON spans by page + text.
    Returns { page_num: [ {bbox, vtype, cid, text} ] }
    """
    spans = json_report.get("formatted_spans", [])
    annotations: dict[int, list[dict]] = {}

    for row in checklist:
        cid = int(row.get("#", 0))
        verdict = str(row.get("Verdict", ""))
        try:
            page = int(row.get("Page", 0))
        except (ValueError, TypeError):
            continue
        pymupdf_text = _norm_ws(str(row.get("PyMuPDF Text", "")))

        if cid in fixed_ids:
            vtype = "fixed"
        elif verdict.startswith("✅"):
            vtype = "pass"
        elif verdict.startswith("⚠"):
            vtype = "warn"
        else:
            vtype = "err"

        # Only annotate errors, warnings, and fixed items (skip pass)
        if vtype == "pass":
            continue

        # Find matching span bbox
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
    """Draw bright YELLOW error highlights on page image for business users."""
    img = Image.open(img_path).convert("RGBA")
    if not annotations:
        return img.convert("RGB")

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    iw, ih = img.size
    scale_x = iw / PDF_PTS_PER_PAGE[0]
    scale_y = ih / PDF_PTS_PER_PAGE[1]

    # ★ YELLOW for errors — highly visible for business users
    COLORS = {
        "err":   ((184, 134, 11),  (255, 255, 0, 80),   (255, 249, 196)),    # BRIGHT YELLOW
        "warn":  ((234, 138, 0),   (255, 165, 0, 50),   (255, 237, 200)),    # ORANGE
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

        # Filled highlight box
        draw.rectangle([x0, y0, x1, y1], fill=fill, outline=outline, width=width)

        # Label above box — large and clear
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

        # Label background + text
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

        # Selected item gets extra blue glow border
        if is_selected:
            for offset in range(1, 5):
                draw.rectangle(
                    [x0 - offset, y0 - offset, x1 + offset, y1 + offset],
                    outline=(59, 130, 246, 180 - offset * 30), width=2,
                )

    return Image.alpha_composite(img, overlay).convert("RGB")


# ═══════════════════════════════════════════════════════════════════
# ZOOMED CROP — magnified view of the selected error region
# ═══════════════════════════════════════════════════════════════════

def _crop_error_region(img_path: Path, bbox: list, margin: int = 60) -> Image.Image | None:
    """Crop and return a zoomed-in view of the error region."""
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

    # Ensure minimum size
    if (x1 - x0) < 100 or (y1 - y0) < 40:
        return None

    cropped = img.crop((x0, y0, x1, y1))

    # Draw yellow highlight border on the actual error area (within crop)
    draw = ImageDraw.Draw(cropped)
    inner_x0 = int(bbox[0] * scale_x) - x0 - 3
    inner_y0 = int(bbox[1] * scale_y) - y0 - 3
    inner_x1 = int(bbox[2] * scale_x) - x0 + 3
    inner_y1 = int(bbox[3] * scale_y) - y0 + 3
    draw.rectangle([inner_x0, inner_y0, inner_x1, inner_y1],
                   outline=(245, 158, 11), width=3)

    return cropped


# ═══════════════════════════════════════════════════════════════════
# INTERACTIVE HTML OVERLAY — clickable yellow regions on the image
# ═══════════════════════════════════════════════════════════════════

def _render_interactive_overlay(img: Image.Image,
                                annotations: list[dict],
                                selected_id: int | None) -> str:
    """
    Build an HTML string with the annotated image as background and
    clickable <div> overlays positioned over each error region.
    Returns HTML string for st.components.v1.html() or st.markdown().
    """
    # Encode image as base64
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()

    iw, ih = img.size
    # Build overlay hotspots
    hotspots_html = ""
    for ann in annotations:
        bbox = ann["bbox"]
        vtype = ann["vtype"]
        cid = ann["cid"]
        text = ann["text"][:40]
        is_selected = (cid == selected_id)

        # Convert bbox (PDF points) to percentage positions
        left_pct = (bbox[0] / PDF_PTS_PER_PAGE[0]) * 100
        top_pct = (bbox[1] / PDF_PTS_PER_PAGE[1]) * 100
        width_pct = ((bbox[2] - bbox[0]) / PDF_PTS_PER_PAGE[0]) * 100
        height_pct = ((bbox[3] - bbox[1]) / PDF_PTS_PER_PAGE[1]) * 100

        # Add some padding
        left_pct = max(0, left_pct - 0.5)
        top_pct = max(0, top_pct - 0.5)
        width_pct = min(100 - left_pct, width_pct + 1.0)
        height_pct = min(100 - top_pct, height_pct + 1.0)

        cls = "overlay-hotspot"
        if is_selected:
            cls += " hotspot-selected"
        elif vtype == "warn":
            cls += " hotspot-warn"

        emoji = "❌" if vtype == "err" else ("⚠" if vtype == "warn" else "✅")
        label = f"#{cid} {emoji}"

        # title tooltip for hover
        tooltip = f"Item #{cid}: {text}"

        hotspots_html += f"""
        <div class="{cls}" title="{tooltip}"
             style="left:{left_pct:.1f}%; top:{top_pct:.1f}%;
                    width:{width_pct:.1f}%; height:{height_pct:.1f}%;"
             onclick="window.open('?item={cid}', '_self')">
            <span class="hotspot-label">{label}</span>
        </div>"""

    html = f"""
    <div class="overlay-container" style="max-height:700px; overflow-y:auto;">
        <img src="data:image/png;base64,{b64}" alt="PDF Page" />
        {hotspots_html}
    </div>"""
    return html


# ═══════════════════════════════════════════════════════════════════
# TEXT RENDERING
# ═══════════════════════════════════════════════════════════════════

def _html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _render_text_html(text: str, checklist: list[dict],
                      selected_id: int | None = None,
                      filter_mode: str = "all",
                      fixed_ids: set[int] | None = None) -> str:
    """Render converted text as styled HTML with tag colouring + span highlights."""
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
            css = "span-highlight-pass"
        elif verdict.startswith("✅"):
            css = "span-highlight-pass"
        elif verdict.startswith("⚠"):
            css = "span-highlight-warn"
        else:
            css = "span-highlight-err"

        highlights.append({
            "search": claude_text or pymupdf_text,
            "css": css, "cid": cid,
            "selected": (cid == selected_id),
        })

    safe = _html_escape(text)
    safe = re.sub(
        r"\[A&gt;(.*?)&lt;A\]",
        r'<span class="tag-add">[A&gt;\1&lt;A]</span>',
        safe, flags=re.DOTALL,
    )
    safe = re.sub(
        r"\[D&gt;(.*?)&lt;D\]",
        r'<span class="tag-del">[D&gt;\1&lt;D]</span>',
        safe, flags=re.DOTALL,
    )

    sorted_hl = sorted(highlights, key=lambda h: h["selected"])
    for hl in sorted_hl:
        needle = _html_escape(hl["search"])
        if not needle:
            continue
        if hl["selected"]:
            tag = f'<mark class="current-span" id="span-{hl["cid"]}">'
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
    """Return ordered list of error+warning item IDs for navigation."""
    ids = []
    for r in checklist:
        verdict = str(r.get("Verdict", ""))
        cid = int(r.get("#", 0))
        if cid in fixed_ids:
            continue
        if verdict.startswith("❌") or verdict.startswith("⚠"):
            ids.append(cid)
    return ids


def _get_error_position(error_ids: list[int], selected_id: int) -> tuple[int, int]:
    """Return (current_index_1based, total) for the selected error."""
    if selected_id in error_ids:
        idx = error_ids.index(selected_id) + 1
        return idx, len(error_ids)
    return 0, len(error_ids)


# ═══════════════════════════════════════════════════════════════════
# SIDEBAR
# ═══════════════════════════════════════════════════════════════════

def _sidebar():
    with st.sidebar:
        st.markdown("## 🔍 Validation Review")
        st.caption("Visual review of audit results for business users")
        st.markdown("---")

        bills = _discover_bills(OPUS_DIR)
        phase_dirs = list(OPUS_DIR.parent.glob("PHASE_*"))
        for pd in phase_dirs:
            bills.update(_discover_bills(pd))

        if not bills:
            st.error("No audit results found in Opus4.6/")
            st.stop()

        bill_names = list(bills.keys())
        labels = []
        for name in bill_names:
            parts = name.split("_")
            bill_num = next((p for p in parts if len(p) >= 5 and p.isdigit()), name)
            labels.append(f"Bill {bill_num}")

        st.markdown("### 📁 Select Bill")
        idx = st.selectbox(
            "Bill file",
            range(len(bill_names)),
            format_func=lambda i: f"{labels[i]}  ({bill_names[i][:30]}…)",
            key="bill_idx",
        )
        selected_bill = bills[bill_names[idx]]

        st.markdown("---")
        st.markdown("### 🔎 Filter")
        filter_mode = st.radio(
            "Show items",
            ["all", "errors", "warnings"],
            format_func=lambda x: {
                "all": "📋 All items",
                "errors": "❌ Errors only",
                "warnings": "⚠️ Warnings only",
            }[x],
            key="filter_mode",
        )

        st.markdown("---")
        st.markdown("### 💡 How to Use")
        st.markdown("""
        1. **Yellow boxes** on PDF = errors to review
        2. Click **error cards** below the image to navigate
        3. Use **← Prev / Next →** to step through errors
        4. **Zoom view** shows the error area close-up
        5. Fix errors in the **edit section** below
        """)

        st.markdown("---")
        st.caption(f"📂 {selected_bill['excel'].parent.name}/")

    return selected_bill, filter_mode


# ═══════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════

def _get_fixed_ids() -> set[int]:
    return set(st.session_state.get("fixed_ids", []))


def _mark_fixed(cid: int):
    fixed = list(st.session_state.get("fixed_ids", []))
    if cid not in fixed:
        fixed.append(cid)
    st.session_state["fixed_ids"] = fixed


def _get_effective_verdict(row: dict, fixed_ids: set[int]) -> str:
    cid = int(row.get("#", 0))
    if cid in fixed_ids:
        return "✅ fixed"
    return str(row.get("Verdict", ""))


def _trunc(s, n=80):
    s = str(s) if s else ""
    return s if len(s) <= n else s[:n] + "…"


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    # ── Handle ?item=N query params from interactive overlay clicks ──
    qp = st.query_params
    if "item" in qp:
        try:
            st.session_state.selected_item = int(qp["item"])
        except (ValueError, TypeError):
            pass
        # Clear the query param to avoid sticky state
        st.query_params.clear()

    selected_bill, filter_mode = _sidebar()
    fixed_ids = _get_fixed_ids()

    # ── Load data ───────────────────────────────────────────────────
    checklist, findings, summary = _load_excel(selected_bill["excel"])
    text = _load_converted_text(selected_bill["converted_txt"])
    page_images = _get_page_images(selected_bill.get("highlighted_dir", Path()))
    json_report = _load_json_report(selected_bill.get("pymupdf_report", Path()))
    total_pages = len(page_images)

    # ── Header ──────────────────────────────────────────────────────
    bill_num = ""
    for p in selected_bill["stem"].split("_"):
        if len(p) >= 5 and p.isdigit():
            bill_num = p
            break

    st.markdown(f"# 🔍 Validation Review — Bill {bill_num}")

    # ── Summary cards ───────────────────────────────────────────────
    n_fixed = sum(1 for r in checklist if int(r.get("#", 0)) in fixed_ids)
    orig_err = summary.get("error", 0)
    eff_err = max(0, orig_err - n_fixed)
    eff_pass = summary.get("pass", 0) + n_fixed

    st.markdown(f"""
    <div class="metric-row">
        <div class="metric-card mc-total">
            <h2>{summary.get('total', 0)}</h2><p>Total Items</p>
        </div>
        <div class="metric-card mc-pass">
            <h2>{eff_pass}</h2><p>✅ Pass</p>
        </div>
        <div class="metric-card mc-warn">
            <h2>{summary.get('warn', 0)}</h2><p>⚠️ Warnings</p>
        </div>
        <div class="metric-card mc-err">
            <h2>{eff_err}</h2><p>❌ Errors</p>
        </div>
        {"<div class='metric-card mc-fixed'><h2>" + str(n_fixed) + "</h2><p>🔧 Fixed</p></div>" if n_fixed else ""}
    </div>
    """, unsafe_allow_html=True)

    total = summary.get("total", 1) or 1
    st.progress(eff_pass / total, text=f"Pass rate: {eff_pass/total:.0%}  ({eff_pass}/{total})")

    # ── Selected item state ─────────────────────────────────────────
    if "selected_item" not in st.session_state:
        errors = [r for r in checklist if str(r.get("Verdict", "")).startswith("❌")]
        warnings = [r for r in checklist if str(r.get("Verdict", "")).startswith("⚠")]
        if errors:
            st.session_state.selected_item = int(errors[0].get("#", 1))
        elif warnings:
            st.session_state.selected_item = int(warnings[0].get("#", 1))
        elif checklist:
            st.session_state.selected_item = int(checklist[0].get("#", 1))
        else:
            st.session_state.selected_item = 0

    selected_id = st.session_state.selected_item

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
        display_cl = [r for r in checklist if str(r.get("Verdict", "")).startswith("⚠")]
    else:
        display_cl = checklist

    # ── Page state ──────────────────────────────────────────────────
    sel_item_page = 1
    if selected_row:
        try:
            sel_item_page = int(selected_row.get("Page", 1))
        except (ValueError, TypeError):
            sel_item_page = 1

    if "current_page" not in st.session_state:
        st.session_state.current_page = sel_item_page
    if st.session_state.get("_last_sel") != selected_id:
        st.session_state.current_page = sel_item_page
        st.session_state["_last_sel"] = selected_id

    # ── Build annotations ───────────────────────────────────────────
    page_anns = _build_page_annotations(checklist, json_report, fixed_ids)

    # ══════════════════════════════════════════════════════════════
    # ★ ERROR NAVIGATION BAR — step through errors easily
    # ══════════════════════════════════════════════════════════════
    error_ids = _get_error_ids(checklist, fixed_ids)
    err_pos, err_total = _get_error_position(error_ids, selected_id)

    if error_ids:
        nav_c1, nav_c2, nav_c3, nav_c4, nav_c5 = st.columns([2, 1, 3, 1, 2])
        with nav_c2:
            if st.button("◀ Prev Error", key="prev_err", use_container_width=True):
                if err_pos > 1:
                    st.session_state.selected_item = error_ids[err_pos - 2]
                else:
                    st.session_state.selected_item = error_ids[-1]  # wrap around
                st.rerun()
        with nav_c3:
            if err_pos > 0:
                st.markdown(f"""
                <div class="error-nav-bar">
                    <span class="nav-label">❌ Error {err_pos} of {err_total}  (Item #{selected_id})</span>
                    <span class="nav-page">Page {sel_item_page}</span>
                </div>
                """, unsafe_allow_html=True)
            else:
                st.markdown(f"""
                <div class="error-nav-bar">
                    <span class="nav-label">📋 Item #{selected_id}</span>
                    <span class="nav-page">Page {sel_item_page}  •  {err_total} errors remaining</span>
                </div>
                """, unsafe_allow_html=True)
        with nav_c4:
            if st.button("Next Error ▶", key="next_err", use_container_width=True):
                if err_pos > 0 and err_pos < err_total:
                    st.session_state.selected_item = error_ids[err_pos]
                else:
                    st.session_state.selected_item = error_ids[0]  # wrap around
                st.rerun()
    else:
        st.success("🎉 **All errors have been resolved!** No remaining issues.")

    # ══════════════════════════════════════════════════════════════
    # ★ TWO-COLUMN LAYOUT: PDF Page | Claude Text
    # ══════════════════════════════════════════════════════════════
    st.markdown("---")
    col_left, col_right = st.columns([1, 1], gap="medium")

    # ──────── LEFT: PDF Page with Yellow Highlights ─────────────
    with col_left:
        st.markdown("#### 📄 PDF Page — Yellow = Errors to Review")

        if total_pages > 0:
            cur = st.session_state.current_page
            cur = max(1, min(cur, total_pages))

            nc1, nc2, nc3 = st.columns([1, 6, 1])
            with nc1:
                if st.button("◀", key="pg_prev"):
                    st.session_state.current_page = max(1, cur - 1)
                    st.rerun()
            with nc3:
                if st.button("▶", key="pg_next"):
                    st.session_state.current_page = min(total_pages, cur + 1)
                    st.rerun()
            with nc2:
                new_pg = st.slider(
                    "Page", 1, total_pages, cur,
                    key="pg_slider", label_visibility="collapsed",
                )
                if new_pg != cur:
                    st.session_state.current_page = new_pg

            cur = st.session_state.current_page
            st.caption(f"📄 Page **{cur}** / {total_pages}")

            img_idx = cur - 1
            if 0 <= img_idx < len(page_images):
                anns = page_anns.get(cur, [])
                annotated = _annotate_page_image(page_images[img_idx], anns, selected_id)

                # ★ Interactive HTML overlay with clickable regions
                overlay_html = _render_interactive_overlay(annotated, anns, selected_id)
                st.markdown(overlay_html, unsafe_allow_html=True)

            # Page summary
            on_page = [r for r in checklist
                       if _safe_int(r.get("Page", 0)) == cur]
            if on_page:
                e = sum(1 for r in on_page
                        if str(r.get("Verdict", "")).startswith("❌")
                        and int(r.get("#", 0)) not in fixed_ids)
                w = sum(1 for r in on_page if str(r.get("Verdict", "")).startswith("⚠"))
                st.caption(f"This page: {len(on_page)} items  •  ❌ {e} errors  •  ⚠️ {w} warnings")

            # ★ CLICKABLE ERROR CARDS — per-page quick navigation
            page_errs = [a for a in page_anns.get(cur, []) if a["vtype"] in ("err", "warn", "fixed")]
            if page_errs:
                st.markdown("##### 👆 Click an error to navigate:")
                # Render as Streamlit buttons in a row
                btn_cols = st.columns(min(len(page_errs), 4))
                for i, ann in enumerate(page_errs):
                    col_idx = i % len(btn_cols)
                    with btn_cols[col_idx]:
                        emoji = "❌" if ann["vtype"] == "err" else ("⚠️" if ann["vtype"] == "warn" else "✅")
                        is_sel = ann["cid"] == selected_id
                        btn_label = f"{emoji} #{ann['cid']}: {ann['text'][:25]}"
                        btn_type = "primary" if is_sel else "secondary"
                        if st.button(btn_label, key=f"ecard_{ann['cid']}",
                                     type=btn_type, use_container_width=True):
                            st.session_state.selected_item = ann["cid"]
                            st.rerun()

            # ★ ZOOMED CROP of selected error
            if selected_row and anns:
                sel_ann = next((a for a in anns if a["cid"] == selected_id), None)
                if sel_ann:
                    cropped = _crop_error_region(page_images[img_idx], sel_ann["bbox"])
                    if cropped:
                        st.markdown(f"""
                        <div class="zoom-container">
                            <div class="zoom-header">🔍 Zoomed View — Item #{selected_id}</div>
                        </div>""", unsafe_allow_html=True)
                        st.image(cropped, use_container_width=True)
        else:
            st.info("No highlighted page images found for this bill.")

        hl_dir = selected_bill.get("highlighted_dir")
        if hl_dir:
            legend = Path(hl_dir) / "legend.png"
            if legend.exists():
                with st.expander("🎨 Legend"):
                    st.image(str(legend), width=400)

    # ──────── RIGHT: Converted Text ─────────────────────────────
    with col_right:
        st.markdown("#### 📝 Claude Converted Text")
        html = _render_text_html(text, checklist, selected_id, filter_mode, fixed_ids)
        st.markdown(f'<div class="text-viewer">{html}</div>', unsafe_allow_html=True)

    # ══════════════════════════════════════════════════════════════
    # ★ ITEM DETAIL + INLINE FIX
    # ══════════════════════════════════════════════════════════════
    if selected_row:
        st.markdown("---")
        cid = int(selected_row.get("#", 0))
        verdict = _get_effective_verdict(selected_row, fixed_ids)

        if cid in fixed_ids:
            dc, ic = "#dbeafe", "🔧"
        elif verdict.startswith("✅"):
            dc, ic = "#dcfce7", "✅"
        elif verdict.startswith("⚠"):
            dc, ic = "#fef9c3", "⚠️"
        else:
            dc, ic = "#fee2e2", "❌"

        st.markdown(f"#### 🔎 Item #{cid} — Detail")

        d1, d2 = st.columns(2)
        with d1:
            st.markdown(f"""
            <div style="background:{dc}; border-radius:10px; padding:16px;">
                <h4 style="margin:0 0 8px">{ic} {verdict}</h4>
                <p><strong>Page:</strong> {selected_row.get('Page', '')}</p>
                <p><strong>Tag Type:</strong> {selected_row.get('Tag Type', '')}</p>
                <p><strong>Classification:</strong> {selected_row.get('Classification', '')}</p>
                <p><strong>In Image:</strong> {selected_row.get('In Image?', '')}</p>
                <p><strong>In Claude:</strong> {selected_row.get('In Claude?', '')}</p>
            </div>
            """, unsafe_allow_html=True)
        with d2:
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

        elif is_error:
            st.markdown(f"#### ✏️ Fix Item #{cid}")

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
                prefill = claude_text
                st.caption(
                    "✏️ Edit the text below. "
                    "Click **Apply Fix** to save your change."
                )
            else:
                prefill = f"{tag_open}{pymupdf_text}{tag_close}"
                st.caption(
                    "⚠️ This text is **missing** from the output. "
                    "Edit below and use **Copy & Mark Fixed** to handle manually."
                )

            fc1, fc2 = st.columns([3, 1])
            with fc1:
                new_text = st.text_area(
                    "Edit text", value=prefill, height=100,
                    key=f"edit_{cid}", label_visibility="collapsed",
                )
            with fc2:
                st.markdown("")
                st.markdown("")
                if claude_text:
                    if st.button("✅ Apply Fix", type="primary", key=f"apply_{cid}"):
                        if new_text.strip() and new_text != claude_text:
                            updated = text.replace(claude_text, new_text, 1)
                            if updated != text:
                                selected_bill["converted_txt"].write_text(
                                    updated, encoding="utf-8")
                                _mark_fixed(cid)
                                st.success("✅ Saved & updated!")
                                st.rerun()
                            else:
                                st.error("Text not found in file.")
                        elif new_text == claude_text:
                            st.warning("No changes made.")
                else:
                    if st.button("📋 Copy & Mark Fixed", key=f"copy_{cid}"):
                        _mark_fixed(cid)
                        st.success("Marked as fixed!")
                        st.rerun()

            # Preview diff
            if claude_text and new_text != claude_text:
                with st.expander("👁️ Preview change"):
                    p1, p2 = st.columns(2)
                    with p1:
                        st.markdown("**Before:**")
                        st.code(claude_text, language=None)
                    with p2:
                        st.markdown("**After:**")
                        st.code(new_text, language=None)
        else:
            st.markdown("**Claude Text:**")
            st.code(str(selected_row.get("Claude Text", "")), language=None)
            notes = str(selected_row.get("Notes", ""))
            fix = str(selected_row.get("Suggested Fix", ""))
            if notes:
                st.info(f"📝 **Notes:** {notes}")
            if fix:
                st.warning(f"🔧 **Suggested Fix:** {fix}")

    # ══════════════════════════════════════════════════════════════
    # CHECKLIST TABLE
    # ══════════════════════════════════════════════════════════════
    st.markdown("---")
    st.markdown("#### 📋 Full Audit Checklist")

    if not display_cl:
        st.info("No items match the current filter.")
    else:
        tbl = '<table class="cl-table"><thead><tr>'
        for c in ["#", "Status", "Page", "Tag Type", "Expected Text (PDF)",
                   "Found?", "Output Text", "Category", "Notes"]:
            tbl += f"<th>{c}</th>"
        tbl += "</tr></thead><tbody>"

        for row in display_cl:
            cid_int = int(row.get("#", 0))
            verd = _get_effective_verdict(row, fixed_ids)

            if cid_int in fixed_ids:
                rc, bc = "row-fixed", "badge-fixed"
            elif verd.startswith("✅"):
                rc, bc = "row-pass", "badge-pass"
            elif verd.startswith("⚠"):
                rc, bc = "row-warn", "badge-warn"
            else:
                rc, bc = "row-err", "badge-err"

            if cid_int == selected_id:
                rc += " row-selected"

            tbl += f'<tr class="{rc}">'
            tbl += f'<td><strong>{cid_int}</strong></td>'
            tbl += f'<td><span class="badge {bc}">{_html_escape(verd)}</span></td>'
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
        with nc[1]:
            if st.button("⬆️ Prev Item"):
                ids = [int(r.get("#", 0)) for r in display_cl]
                ci = ids.index(selected_id) if selected_id in ids else 0
                st.session_state.selected_item = ids[max(0, ci - 1)]
                st.rerun()
        with nc[2]:
            id_opts = [int(r.get("#", 0)) for r in display_cl]
            jump = st.selectbox(
                "Go to #", id_opts,
                index=id_opts.index(selected_id) if selected_id in id_opts else 0,
                key="jump_to", label_visibility="collapsed",
            )
            if jump != selected_id:
                st.session_state.selected_item = jump
                st.rerun()
        with nc[3]:
            if st.button("⬇️ Next Item"):
                ids = [int(r.get("#", 0)) for r in display_cl]
                ci = ids.index(selected_id) if selected_id in ids else 0
                st.session_state.selected_item = ids[min(len(ids) - 1, ci + 1)]
                st.rerun()

    # ══════════════════════════════════════════════════════════════
    # ADDITIONAL FINDINGS
    # ══════════════════════════════════════════════════════════════
    if findings:
        st.markdown("---")
        st.markdown(f"#### 🔔 Additional Findings ({len(findings)})")
        for af in findings:
            it = str(af.get("Issue Type", ""))
            cls = "af-error" if it in ("content_dropped", "tag_error", "text_error") else "af-warn"
            st.markdown(f"""
            <div class="af-card {cls}">
                <strong>Page {af.get("Page","")}</strong> — <em>{_html_escape(it)}</em><br>
                {_html_escape(str(af.get("Description", "")))}
            </div>
            """, unsafe_allow_html=True)

    # ── Footer ──────────────────────────────────────────────────────
    st.markdown("---")
    st.caption(
        f"📊 {selected_bill['excel'].name}  •  "
        f"Items: {summary.get('total',0)}  •  "
        f"Fixed: {n_fixed}"
    )


def _safe_int(val, default=0) -> int:
    """Safely convert to int, returning default on failure."""
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


if __name__ == "__main__":
    main()
