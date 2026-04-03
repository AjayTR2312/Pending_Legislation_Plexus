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


def _extract_core_text(raw: str) -> str:
    """Extract the core formatted text from PyMuPDF context markers.

    Input may be:  ``…some prefix >>>core text<<< suffix…``
    Returns just ``core text`` (stripped).  Falls back to stripping
    leading/trailing ``…`` if no ``>>>`` markers exist.
    """
    m = re.search(r">>>(.+?)<<<", raw, flags=re.DOTALL)
    if m:
        return m.group(1).strip()
    return raw.replace("\u2026", "").strip()


def _merge_bboxes(bboxes: list[list]) -> list:
    """Merge multiple bboxes into one encompassing rectangle."""
    if not bboxes:
        return []
    x0 = min(b[0] for b in bboxes)
    y0 = min(b[1] for b in bboxes)
    x1 = max(b[2] for b in bboxes)
    y1 = max(b[3] for b in bboxes)
    return [x0, y0, x1, y1]


def _build_page_annotations(checklist: list[dict], json_report: dict,
                            fixed_ids: set[int]) -> dict[int, list[dict]]:
    """
    Match checklist items to JSON spans by page + text.
    Returns { page_num: [ {bbox, vtype, cid, text} ] }

    Improvements:
      1. Extracts core text from ``>>>``/``<<<`` context markers
      2. Uses full PyMuPDF Text (stripped of markers) for broader matching
      3. Finds ALL matching spans (multi-line errors) and merges bboxes
      4. Falls back to word-overlap fuzzy matching
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

        if cid in fixed_ids:
            vtype = "fixed"
        elif verdict.startswith("✅"):
            vtype = "pass"
        elif verdict.startswith("⚠"):
            vtype = "warn"
        else:
            vtype = "err"

        if vtype in ("pass", "warn"):
            continue

        # ── Extract the best search text ──
        context_raw = str(row.get("Context", "") or "")
        pymupdf_raw = str(row.get("PyMuPDF Text", "") or "")

        # Core = the formatted portion between >>> <<<
        core = ""
        if ">>>" in context_raw:
            core = _extract_core_text(context_raw)
        if not core and ">>>" in pymupdf_raw:
            core = _extract_core_text(pymupdf_raw)
        # Fallback: use full PyMuPDF text cleaned of markers
        full_search = pymupdf_raw.replace("\u2026", "").replace(">>>", "").replace("<<<", "").strip()
        core_norm = _norm_ws(core) if core else _norm_ws(full_search)

        if not core_norm or page <= 0:
            continue

        # Cleaned PyMuPDF text (the actual formatted text, often short for warnings)
        full_norm = _norm_ws(full_search)
        # For chaining (multi-line errors), prefer the longer text
        chain_norm = full_norm if len(full_norm) > len(core_norm) else core_norm

        # ── Find ALL matching spans on this page ──
        page_spans = [sp for sp in spans if sp.get("page") == page]
        matched_bboxes: list[list] = []

        # ── Short-text strategy (typical for warnings: "the", "or", ".") ──
        # When the actual PyMuPDF text is very short, use the Context to
        # narrow the region, then pinpoint the exact short text within it.
        if len(full_norm) <= 10 and core_norm != full_norm and len(core_norm) > len(full_norm):
            # Step 1: find spans matching the context core to locate the area
            context_spans: list[dict] = []
            for sp in page_spans:
                sp_text = _norm_ws(sp.get("text", ""))
                if not sp_text or not sp.get("bbox"):
                    continue
                if sp_text in core_norm or core_norm in sp_text:
                    context_spans.append(sp)
            # Step 2: among those context spans, find the one containing
            #         the actual short formatted text
            for sp in context_spans:
                sp_text = _norm_ws(sp.get("text", ""))
                if full_norm in sp_text or sp_text == full_norm:
                    matched_bboxes.append(sp["bbox"])
                    break
            # Step 3: if context spans found but none contain the short text,
            #         try ALL page spans that have the short text and overlap
            #         the context region vertically
            if not matched_bboxes and context_spans:
                ctx_bbox = _merge_bboxes([s["bbox"] for s in context_spans])
                ctx_y0, ctx_y1 = ctx_bbox[1] - 2, ctx_bbox[3] + 2
                for sp in page_spans:
                    sp_text = _norm_ws(sp.get("text", ""))
                    if not sp_text or not sp.get("bbox"):
                        continue
                    if full_norm in sp_text or sp_text == full_norm:
                        sp_y = (sp["bbox"][1] + sp["bbox"][3]) / 2
                        if ctx_y0 <= sp_y <= ctx_y1:
                            matched_bboxes.append(sp["bbox"])
                            break
            # Step 4: last resort for short text — just grab the context region
            if not matched_bboxes and context_spans:
                matched_bboxes = [s["bbox"] for s in context_spans]
        else:
            # ── Normal strategy: seed + chain for longer text ──
            # Find a "seed" span that matches the START of the search,
            # then collect consecutive spans that continue the match.
            for idx, sp in enumerate(page_spans):
                sp_text = _norm_ws(sp.get("text", ""))
                if not sp_text or not sp.get("bbox"):
                    continue

                # Check if this span starts or contains the search text
                is_start = (chain_norm.startswith(sp_text)
                            or sp_text.startswith(chain_norm[:min(len(chain_norm), 30)]))
                is_exact = sp_text == chain_norm
                is_contained = (sp_text in chain_norm
                                and len(sp_text) > max(15, len(chain_norm) * 0.3))

                if is_exact or is_start or is_contained:
                    matched_bboxes.append(sp["bbox"])
                    # Collect subsequent spans that continue the match
                    if len(sp_text) < len(chain_norm):
                        remaining = chain_norm
                        if sp_text in remaining:
                            remaining = remaining[remaining.index(sp_text) + len(sp_text):]
                        for sp2 in page_spans[idx + 1:]:
                            sp2_text = _norm_ws(sp2.get("text", ""))
                            if not sp2_text or not sp2.get("bbox"):
                                continue
                            if (sp2_text in remaining
                                    or remaining.startswith(
                                        sp2_text[:min(len(sp2_text), 20)])):
                                matched_bboxes.append(sp2["bbox"])
                                if sp2_text in remaining:
                                    remaining = remaining[
                                        remaining.index(sp2_text) + len(sp2_text):]
                                if not remaining.strip():
                                    break
                            else:
                                break
                    break  # found our seed, stop looking

        # Also try matching against the full (un-truncated) PyMuPDF text
        if not matched_bboxes:
            full_norm = _norm_ws(full_search)
            for sp in page_spans:
                sp_text = _norm_ws(sp.get("text", ""))
                if not sp_text:
                    continue
                if sp_text in full_norm or full_norm in sp_text:
                    if sp.get("bbox"):
                        matched_bboxes.append(sp["bbox"])

        # Fuzzy fallback: word-overlap matching
        if not matched_bboxes and len(core_norm) > 10:
            core_words = set(core_norm.split())
            for sp in page_spans:
                sp_words = set(_norm_ws(sp.get("text", "")).split())
                if not sp_words:
                    continue
                overlap = len(core_words & sp_words) / max(len(sp_words), 1)
                if overlap >= 0.5 and sp.get("bbox"):
                    matched_bboxes.append(sp["bbox"])

        if matched_bboxes:
            merged = _merge_bboxes(matched_bboxes)
            annotations.setdefault(page, []).append({
                "bbox": merged, "vtype": vtype,
                "cid": cid,
                "text": pymupdf_raw[:60],
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
    """Render converted text as styled HTML with tag colouring + span highlights.

    Key fix: highlight search runs on the *raw* escaped text **before**
    [A>/[D> tag wrapping, so Claude Text values match their exact file
    content.  Falls back to fuzzy substring search for truncated Excel
    values.
    """
    fixed_ids = fixed_ids or set()
    highlights: list[dict] = []
    for row in checklist:
        verdict = str(row.get("Verdict", ""))
        cid = int(row.get("#", 0))

        if verdict.startswith("⚠"):
            continue   # warnings are not highlighted
        if filter_mode == "errors" and not verdict.startswith("❌"):
            continue

        claude_text = str(row.get("Claude Text", "") or "").strip()

        if cid in fixed_ids:
            css = "span-highlight-pass"
        elif verdict.startswith("✅"):
            css = "span-highlight-pass"
        elif verdict.startswith("⚠"):
            css = "span-highlight-warn"
        else:
            css = "span-highlight-err"

        # Determine search needle — fall back to PyMuPDF Text / Context
        # for warnings which typically have empty Claude Text
        search_text = claude_text
        if not search_text:
            # Try Context core (between >>> <<<)
            ctx_raw = str(row.get("Context", "") or "")
            pm_raw = str(row.get("PyMuPDF Text", "") or "")
            core = ""
            if ">>>" in ctx_raw:
                core = _extract_core_text(ctx_raw)
            if not core and ">>>" in pm_raw:
                core = _extract_core_text(pm_raw)
            # Use core if available, otherwise cleaned PyMuPDF text
            if core:
                search_text = core
            elif pm_raw:
                search_text = pm_raw.replace("\u2026", "").replace(">>>", "").replace("<<<", "").strip()

        if not search_text:
            continue

        highlights.append({
            "search": search_text,
            "css": css, "cid": cid,
            "selected": (cid == selected_id),
        })

    safe = _html_escape(text)

    # ── Insert highlights BEFORE tag-wrapping so [A>…<A] text is intact ──
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

        if needle in safe:
            safe = safe.replace(needle, f"{tag}{needle}{end_tag}", 1)
        else:
            # Fuzzy fallback: strip tag markers, search content only
            plain = needle
            for tk in ["[A&gt;", "&lt;A]", "[D&gt;", "&lt;D]"]:
                plain = plain.replace(tk, "")
            plain = plain.strip()
            if plain and len(plain) > 15 and plain in safe:
                safe = safe.replace(plain, f"{tag}{plain}{end_tag}", 1)

    # ── Now apply tag colouring (wraps remaining unwrapped tags) ──
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

    return safe


# ═══════════════════════════════════════════════════════════════════
# STATE DETECTION & MARKUP RULES
# ═══════════════════════════════════════════════════════════════════

def _detect_state_from_text(text: str) -> str | None:
    """Detect state code from converted text content."""
    patterns = {
        "KS": r"State of Kansas|Kansas Legislature",
        "AR": r"State of Arkansas|Arkansas General Assembly",
        "CO": r"State of Colorado|Colorado General Assembly",
        "FL": r"State of Florida|Florida Legislature",
        "TX": r"State of Texas|Texas Legislature",
        "NY": r"State of New York|New York Legislature",
        "CA": r"State of California|California Legislature",
        "OH": r"State of Ohio|Ohio General Assembly",
        "IL": r"State of Illinois|Illinois General Assembly",
        "IN": r"State of Indiana|Indiana General Assembly",
        "IA": r"State of Iowa|Iowa General Assembly",
        "NE": r"State of Nebraska|Nebraska Legislature",
        "OK": r"State of Oklahoma|Oklahoma Legislature",
        "TN": r"State of Tennessee|Tennessee General Assembly",
        "WI": r"State of Wisconsin|Wisconsin Legislature",
        "MN": r"State of Minnesota|Minnesota Legislature",
        "NC": r"State of North Carolina|North Carolina General Assembly",
        "ND": r"State of North Dakota|North Dakota Legislative Assembly",
        "SD": r"State of South Dakota|South Dakota Legislature",
        "WY": r"State of Wyoming|Wyoming Legislature",
        "ID": r"State of Idaho|Idaho Legislature",
        "HI": r"State of Hawaii|Hawaii Legislature",
        "VT": r"State of Vermont|Vermont General Assembly",
        "ME": r"State of Maine|Maine Legislature",
        "DE": r"State of Delaware|Delaware General Assembly",
        "CT": r"State of Connecticut|Connecticut General Assembly",
        "GA": r"State of Georgia|Georgia General Assembly",
        "DC": r"District of Columbia",
    }
    for code, pattern in patterns.items():
        if re.search(pattern, text, re.IGNORECASE):
            return code
    return None


def _load_state_config(state_code: str | None) -> dict | None:
    """Load state config from netscan_state_template.json."""
    if not state_code:
        return None
    template_path = OPUS_DIR / "netscan_state_template.json"
    if not template_path.exists():
        return None
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            template = json.load(f)
        return template.get("states", {}).get(state_code)
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════
# ERROR NAVIGATION HELPERS
# ═══════════════════════════════════════════════════════════════════

def _get_error_ids(checklist: list[dict], fixed_ids: set[int]) -> list[int]:
    """Return ordered list of error item IDs (❌ only) for navigation."""
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


def _unmark_fixed(cid: int):
    """Remove a single item from the fixed list."""
    fixed = list(st.session_state.get("fixed_ids", []))
    if cid in fixed:
        fixed.remove(cid)
    st.session_state["fixed_ids"] = fixed


def _save_undo_history(item_ids: list[int], original_context: str,
                       new_text: str, file_path: str):
    """Push an undo record so the user can revert a fix."""
    history = list(st.session_state.get("undo_history", []))
    history.append({
        "item_ids": item_ids,
        "original_context": original_context,
        "new_text": new_text,
        "file_path": file_path,
    })
    st.session_state["undo_history"] = history


def _get_undo_record(cid: int) -> dict | None:
    """Find the most recent undo record that includes *cid*."""
    for rec in reversed(st.session_state.get("undo_history", [])):
        if cid in rec["item_ids"]:
            return rec
    return None


def _get_effective_verdict(row: dict, fixed_ids: set[int]) -> str:
    cid = int(row.get("#", 0))
    if cid in fixed_ids:
        return "✅ fixed"
    return str(row.get("Verdict", ""))


def _trunc(s, n=80):
    s = str(s) if s else ""
    return s if len(s) <= n else s[:n] + "…"


def _extract_edit_context(full_text: str, search_text: str,
                          context_chars: int = 200) -> str:
    """Return *search_text* plus surrounding context from *full_text*.

    This lets the user see (and edit) text around a tagged span so they
    can adjust tag boundaries — e.g. move ``[A>`` to include or exclude
    nearby words.  Snaps to word boundaries for a clean edit box.
    """
    if not search_text:
        return search_text
    pos = full_text.find(search_text)
    if pos == -1:
        return search_text          # not found → return as-is

    # ── expand backwards ──
    raw_start = max(0, pos - context_chars)
    if raw_start > 0:
        sp = full_text.find(' ', raw_start)
        start = (sp + 1) if sp != -1 and sp < pos else raw_start
    else:
        start = 0

    # ── expand forwards ──
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
    """Return item IDs of *all* errors whose Claude Text was in the
    original context window **and** was modified by the user's edit.

    This handles the case where two errors sit on the same line:
    the user fixes both in one edit, so both should be marked fixed.
    """
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
        # Was this item's text inside the context window?
        if ct in original_context:
            # Was it changed (no longer identical in the new text)?
            if ct not in new_text or cid == current_cid:
                affected.append(cid)
    # Always include the current item
    if current_cid not in affected:
        affected.append(current_cid)
    return affected


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
    # Re-initialize when bill changes or on first load
    current_bill_key = selected_bill["stem"]
    bill_changed = (st.session_state.get("_active_bill") != current_bill_key)
    if bill_changed:
        st.session_state["_active_bill"] = current_bill_key
        st.session_state.pop("selected_item", None)
        st.session_state.pop("current_page", None)
        st.session_state.pop("_last_sel", None)
        st.session_state.pop("fixed_ids", None)
        st.session_state.pop("undo_history", None)

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

    # Also validate that selected_item exists in THIS bill's checklist
    all_ids = {int(r.get("#", 0)) for r in checklist}
    if st.session_state.selected_item not in all_ids:
        errors = [r for r in checklist if str(r.get("Verdict", "")).startswith("❌")]
        if errors:
            st.session_state.selected_item = int(errors[0].get("#", 1))
        elif checklist:
            st.session_state.selected_item = int(checklist[0].get("#", 1))

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

                # ★ Render annotated page image directly (no HTML overlay)
                st.image(annotated, use_container_width=True)

            # Page summary
            on_page = [r for r in checklist
                       if _safe_int(r.get("Page", 0)) == cur]
            if on_page:
                e = sum(1 for r in on_page
                        if str(r.get("Verdict", "")).startswith("❌")
                        and int(r.get("#", 0)) not in fixed_ids)
                w = sum(1 for r in on_page if str(r.get("Verdict", "")).startswith("⚠"))
                st.caption(f"This page: {len(on_page)} items  •  ❌ {e} errors  •  ⚠️ {w} warnings")

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

        # ★ STATE INFO & MARKUP RULES panel
        detected_state = _detect_state_from_text(text)
        state_config = _load_state_config(detected_state)
        with st.expander("📋 State Info & Markup Rules", expanded=False):
            if state_config:
                st.markdown(f"**State:** {state_config.get('state_name', detected_state)} ({detected_state})")
                add_mk = state_config.get("add_markup", {})
                del_mk = state_config.get("delete_markup", {})
                # Build addition rules
                add_rules = []
                if add_mk.get("underline"): add_rules.append("Underline")
                if add_mk.get("italic"): add_rules.append("Italic")
                if add_mk.get("bold"): add_rules.append("Bold")
                if add_mk.get("all_caps"): add_rules.append("ALL CAPS")
                if add_mk.get("brackets"): add_rules.append("Brackets")
                for cr in add_mk.get("custom_rules", []):
                    add_rules.append(str(cr))
                # Build deletion rules
                del_rules = []
                if del_mk.get("strike_out"): del_rules.append("Strikethrough")
                if del_mk.get("brackets"): del_rules.append("Brackets")
                for cr in del_mk.get("custom_rules", []):
                    del_rules.append(str(cr))
                st.markdown(f"""
                | Tag | Meaning | PDF Formatting |
                |-----|---------|----------------|
                | `[A>...<A]` | **Added** text | {', '.join(add_rules) or 'N/A'} |
                | `[D>...<D]` | **Deleted** text | {', '.join(del_rules) or 'N/A'} |
                """)
                deletions = state_config.get("deletions", "")
                if deletions:
                    st.markdown(f"**Deletions:** {deletions}")
            else:
                st.markdown("""
                | Tag | Meaning | Typical PDF Formatting |
                |-----|---------|------------------------|
                | `[A>...<A]` | **Added** text | Underline / Italic |
                | `[D>...<D]` | **Deleted** text | Strikethrough |
                """)
                if detected_state:
                    st.caption(f"Detected state: {detected_state} (no config found)")
                else:
                    st.caption("State not detected — showing default rules")

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
            undo_rec = _get_undo_record(cid)
            if undo_rec:
                if st.button("↩️ Undo This Fix", key=f"undo_{cid}"):
                    # Revert: replace new_text back to original_context in file
                    current_text = _load_converted_text(selected_bill["converted_txt"])
                    reverted = current_text.replace(
                        undo_rec["new_text"], undo_rec["original_context"], 1
                    )
                    if reverted != current_text:
                        selected_bill["converted_txt"].write_text(
                            reverted, encoding="utf-8")
                    # Unmark all items that were part of this fix
                    for aid in undo_rec["item_ids"]:
                        _unmark_fixed(aid)
                    # Remove this undo record
                    history = list(st.session_state.get("undo_history", []))
                    history = [r for r in history if r is not undo_rec]
                    st.session_state["undo_history"] = history
                    st.success(f"↩️ Reverted! Items {', '.join(f'#{a}' for a in undo_rec['item_ids'])} unmarked.")
                    st.rerun()

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
                # Show wider context so user can adjust tag boundaries
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
                    st.caption(
                        "✏️ Edit the text below. "
                        "Click **Apply Fix** to save your change."
                    )
            else:
                original_context = ""
                prefill = f"{tag_open}{pymupdf_text}{tag_close}"
                st.caption(
                    "⚠️ This text is **missing** from the output. "
                    "Edit below and use **Copy & Mark Fixed** to handle manually."
                )

            fc1, fc2 = st.columns([3, 1])
            with fc1:
                new_text = st.text_area(
                    "Edit text", value=prefill, height=200,
                    key=f"edit_{cid}", label_visibility="collapsed",
                )
            with fc2:
                st.markdown("")
                st.markdown("")
                if claude_text:
                    # ── Two-step confirmation flow ──
                    pending_key = f"pending_apply_{cid}"
                    is_pending = st.session_state.get(pending_key, False)

                    if not is_pending:
                        # Step 1: user clicks Apply Fix → show confirmation
                        if st.button("✅ Apply Fix", type="primary", key=f"apply_{cid}"):
                            if new_text.strip() and new_text != original_context:
                                st.session_state[pending_key] = True
                                st.rerun()
                            elif new_text == original_context:
                                st.warning("No changes made.")
                    else:
                        # Step 2: show Confirm + Cancel
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
                            if st.button("✅ Confirm", type="primary", key=f"confirm_{cid}"):
                                updated = text.replace(original_context, new_text, 1)
                                if updated != text:
                                    selected_bill["converted_txt"].write_text(
                                        updated, encoding="utf-8")
                                    affected = _find_affected_items(
                                        checklist, fixed_ids,
                                        original_context, new_text, cid,
                                    )
                                    for aid in affected:
                                        _mark_fixed(aid)
                                    _save_undo_history(
                                        affected, original_context,
                                        new_text,
                                        str(selected_bill["converted_txt"]),
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
                            if st.button("❌ Cancel", key=f"cancel_{cid}"):
                                st.session_state.pop(pending_key, None)
                                st.rerun()
                else:
                    if st.button("📋 Copy & Mark Fixed", key=f"copy_{cid}"):
                        _mark_fixed(cid)
                        _save_undo_history(
                            [cid], "", "",
                            str(selected_bill["converted_txt"]),
                        )
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

        # Sync selectbox state with current selection to prevent stale value reverting
        id_opts = [int(r.get("#", 0)) for r in display_cl]
        if "jump_to" in st.session_state and selected_id in id_opts:
            st.session_state["jump_to"] = selected_id

        with nc[1]:
            if st.button("⬆️ Prev Item", key="prev_item_btn"):
                if selected_id in id_opts:
                    ci = id_opts.index(selected_id)
                    st.session_state.selected_item = id_opts[max(0, ci - 1)]
                elif id_opts:
                    st.session_state.selected_item = id_opts[0]
                st.rerun()
        with nc[2]:
            current_idx = id_opts.index(selected_id) if selected_id in id_opts else 0
            jump = st.selectbox(
                "Go to #", id_opts,
                index=current_idx,
                key="jump_to", label_visibility="collapsed",
            )
        with nc[3]:
            if st.button("⬇️ Next Item", key="next_item_btn"):
                if selected_id in id_opts:
                    ci = id_opts.index(selected_id)
                    st.session_state.selected_item = id_opts[min(len(id_opts) - 1, ci + 1)]
                elif id_opts:
                    st.session_state.selected_item = id_opts[0]
                st.rerun()

        # Handle selectbox jump separately (only when user actively changes it)
        if jump != selected_id and jump in id_opts:
            st.session_state.selected_item = jump
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
