"""
PDF Margin Detector & Cropper  (standalone pre-processing step)
================================================================

Detects PDFs with excessive margins (e.g. Kansas-style legislative bills
where the text block is centred in a large page) and crops every page to
the actual content area.  Run this *before* the conversion pipeline so the
extractor sees a tightly-framed page and produces more accurate text.

How it works
------------
1. For every page the script computes the union bounding-box of all text
   and image blocks (the "content rect").
2. It compares the content rect to the full page size.  If the content
   occupies less than ~65 % of the page width **or** height the page is
   flagged as "broad-margin".
3. If the majority of pages are flagged, a unified crop-box is computed
   across all pages (with configurable padding) and written to a new PDF.

Usage — CLI
-----------
    # Analyse only (prints a report)
    python pdf_margin_cropper.py  input.pdf

    # Analyse + crop → saves <stem>_cropped.pdf next to the original
    python pdf_margin_cropper.py  input.pdf  --crop

    # Custom output path and padding
    python pdf_margin_cropper.py  input.pdf  --crop -o out.pdf -p 15

Usage — Python
--------------
    from pdf_margin_cropper import auto_crop_if_needed

    effective_path = auto_crop_if_needed("bill.pdf")
    # returns the original path if margins are fine,
    # or a new *_cropped.pdf path if it was cropped.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

try:
    import fitz  # PyMuPDF
except ImportError:
    raise ImportError("PyMuPDF is required.  Install with:  pip install PyMuPDF")

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────
# Content-to-page ratio below which a page is considered "broad-margin"
CONTENT_RATIO_THRESHOLD = 0.65
# Points of padding kept around the content when cropping
CROP_PADDING_PT = 10.0
# Minimum characters on a page for it to count in the analysis
MIN_CHARS_FOR_ANALYSIS = 20
# Fraction of analysed pages that must be broad-margin to trigger a crop
PAGE_VOTE_THRESHOLD = 0.50


# ── Data classes ─────────────────────────────────────────────────────────
@dataclass
class PageMarginInfo:
    """Margin metrics for one page."""
    page_index: int
    page_width: float
    page_height: float
    content_x0: float
    content_y0: float
    content_x1: float
    content_y1: float
    left_margin: float
    right_margin: float
    top_margin: float
    bottom_margin: float
    content_width_ratio: float
    content_height_ratio: float
    char_count: int

    @property
    def is_broad_margin(self) -> bool:
        return (self.content_width_ratio < CONTENT_RATIO_THRESHOLD
                or self.content_height_ratio < CONTENT_RATIO_THRESHOLD)


@dataclass
class DocumentMarginInfo:
    """Margin metrics aggregated over the whole document."""
    pdf_path: str
    total_pages: int
    analyzed_pages: int
    broad_margin_pages: int
    is_broad_margin: bool
    median_content_width_ratio: float
    median_content_height_ratio: float
    median_left_margin: float
    median_right_margin: float
    median_top_margin: float
    median_bottom_margin: float
    unified_content_bbox: Tuple[float, float, float, float]
    page_details: List[PageMarginInfo]


# ── Helpers ──────────────────────────────────────────────────────────────

def _median(vals: List[float]) -> float:
    s = sorted(vals)
    n = len(s)
    if n == 0:
        return 0.0
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


def _analyze_page(page: fitz.Page, page_index: int) -> Optional[PageMarginInfo]:
    """Return margin info for a single page, or None if too little content."""
    pw, ph = page.rect.width, page.rect.height

    text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
    bboxes: List[Tuple[float, float, float, float]] = []
    char_count = 0

    for block in text_dict.get("blocks", []):
        if block.get("type") == 0:  # text block
            block_text = ""
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    block_text += span.get("text", "")
            stripped = block_text.strip()
            if len(stripped) < 2:
                continue
            char_count += len(stripped)
            bboxes.append(tuple(block["bbox"]))
        elif block.get("type") == 1:  # image block
            bboxes.append(tuple(block["bbox"]))

    if char_count < MIN_CHARS_FOR_ANALYSIS or not bboxes:
        return None

    cx0 = min(b[0] for b in bboxes)
    cy0 = min(b[1] for b in bboxes)
    cx1 = max(b[2] for b in bboxes)
    cy1 = max(b[3] for b in bboxes)

    return PageMarginInfo(
        page_index=page_index,
        page_width=pw,
        page_height=ph,
        content_x0=cx0, content_y0=cy0, content_x1=cx1, content_y1=cy1,
        left_margin=cx0,
        right_margin=pw - cx1,
        top_margin=cy0,
        bottom_margin=ph - cy1,
        content_width_ratio=(cx1 - cx0) / pw if pw else 1.0,
        content_height_ratio=(cy1 - cy0) / ph if ph else 1.0,
        char_count=char_count,
    )


# ── Public API ───────────────────────────────────────────────────────────

def detect_margins(pdf_path: str | Path) -> DocumentMarginInfo:
    """Analyse every page in *pdf_path* and return margin statistics."""
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    doc = fitz.open(str(pdf_path))
    total_pages = len(doc)
    page_infos: List[PageMarginInfo] = []

    for idx in range(total_pages):
        info = _analyze_page(doc[idx], idx)
        if info is not None:
            page_infos.append(info)
    doc.close()

    if not page_infos:
        return DocumentMarginInfo(
            pdf_path=str(pdf_path), total_pages=total_pages,
            analyzed_pages=0, broad_margin_pages=0, is_broad_margin=False,
            median_content_width_ratio=1.0, median_content_height_ratio=1.0,
            median_left_margin=0, median_right_margin=0,
            median_top_margin=0, median_bottom_margin=0,
            unified_content_bbox=(0, 0, 0, 0), page_details=[],
        )

    broad_count = sum(1 for p in page_infos if p.is_broad_margin)

    # Unified content bbox — use robust min/max (5th / 95th percentile
    # when there are enough pages to ignore outliers like title pages).
    def _robust(vals: List[float], low_pct: float, high_pct: float):
        s = sorted(vals)
        n = len(s)
        return s[max(0, int(n * low_pct))], s[min(n - 1, int(n * high_pct))]

    if len(page_infos) >= 4:
        x0_lo, _ = _robust([p.content_x0 for p in page_infos], 0.0, 0.10)
        _, x1_hi = _robust([p.content_x1 for p in page_infos], 0.90, 1.0)
        y0_lo, _ = _robust([p.content_y0 for p in page_infos], 0.0, 0.10)
        _, y1_hi = _robust([p.content_y1 for p in page_infos], 0.90, 1.0)
    else:
        x0_lo = min(p.content_x0 for p in page_infos)
        x1_hi = max(p.content_x1 for p in page_infos)
        y0_lo = min(p.content_y0 for p in page_infos)
        y1_hi = max(p.content_y1 for p in page_infos)

    return DocumentMarginInfo(
        pdf_path=str(pdf_path),
        total_pages=total_pages,
        analyzed_pages=len(page_infos),
        broad_margin_pages=broad_count,
        is_broad_margin=(broad_count / len(page_infos)) >= PAGE_VOTE_THRESHOLD,
        median_content_width_ratio=_median([p.content_width_ratio for p in page_infos]),
        median_content_height_ratio=_median([p.content_height_ratio for p in page_infos]),
        median_left_margin=_median([p.left_margin for p in page_infos]),
        median_right_margin=_median([p.right_margin for p in page_infos]),
        median_top_margin=_median([p.top_margin for p in page_infos]),
        median_bottom_margin=_median([p.bottom_margin for p in page_infos]),
        unified_content_bbox=(x0_lo, y0_lo, x1_hi, y1_hi),
        page_details=page_infos,
    )


def crop_pdf_margins(
    pdf_path: str | Path,
    output_path: Optional[str | Path] = None,
    padding_pt: float = CROP_PADDING_PT,
    force: bool = False,
) -> Optional[str]:
    """
    Crop a PDF to its content area and save a new file.

    Returns the path to the cropped PDF, or None if no crop was needed
    (and *force* is False).
    """
    pdf_path = Path(pdf_path)
    margin_info = detect_margins(pdf_path)

    if not force and not margin_info.is_broad_margin:
        logger.info(
            "%s — margins OK (w=%.0f%%, h=%.0f%%) — skipping crop.",
            pdf_path.name,
            margin_info.median_content_width_ratio * 100,
            margin_info.median_content_height_ratio * 100,
        )
        return None

    ux0, uy0, ux1, uy1 = margin_info.unified_content_bbox
    logger.info(
        "Cropping %s  content bbox=(%.1f, %.1f, %.1f, %.1f)  "
        "w=%.0f%%  h=%.0f%%",
        pdf_path.name, ux0, uy0, ux1, uy1,
        margin_info.median_content_width_ratio * 100,
        margin_info.median_content_height_ratio * 100,
    )

    doc = fitz.open(str(pdf_path))
    for page in doc:
        pw, ph = page.rect.width, page.rect.height
        crop = fitz.Rect(
            max(0, ux0 - padding_pt),
            max(0, uy0 - padding_pt),
            min(pw, ux1 + padding_pt),
            min(ph, uy1 + padding_pt),
        )
        page.set_cropbox(crop)

    if output_path is None:
        output_path = pdf_path.with_name(f"{pdf_path.stem}_cropped{pdf_path.suffix}")
    output_path = Path(output_path)

    doc.save(str(output_path))
    doc.close()
    logger.info("Cropped PDF → %s", output_path)
    return str(output_path)


def auto_crop_if_needed(
    pdf_path: str | Path,
    padding_pt: float = CROP_PADDING_PT,
) -> str:
    """
    If *pdf_path* has broad margins, crop it and return the new path.
    Otherwise return the original path unchanged.

    This is the recommended single-call entry-point for pipelines.
    """
    pdf_path = Path(pdf_path)
    margin_info = detect_margins(pdf_path)

    if not margin_info.is_broad_margin:
        logger.info(
            "%s — no broad margins (w=%.0f%%, h=%.0f%%).",
            pdf_path.name,
            margin_info.median_content_width_ratio * 100,
            margin_info.median_content_height_ratio * 100,
        )
        return str(pdf_path)

    logger.info(
        "%s — broad margins detected (w=%.0f%%, h=%.0f%%, "
        "broad pages %d/%d) — cropping.",
        pdf_path.name,
        margin_info.median_content_width_ratio * 100,
        margin_info.median_content_height_ratio * 100,
        margin_info.broad_margin_pages,
        margin_info.analyzed_pages,
    )
    cropped = crop_pdf_margins(pdf_path, padding_pt=padding_pt, force=True)
    return cropped  # type: ignore[return-value]


# ── Pretty-print report ─────────────────────────────────────────────────

def print_report(info: DocumentMarginInfo) -> None:
    """Print a human-readable margin analysis to stdout."""
    print(f"\n{'=' * 64}")
    print(f"  PDF Margin Analysis: {Path(info.pdf_path).name}")
    print(f"{'=' * 64}")
    print(f"  Total pages:           {info.total_pages}")
    print(f"  Analysed pages:        {info.analyzed_pages}")
    print(f"  Broad-margin pages:    {info.broad_margin_pages}")
    print(f"  Is broad-margin PDF:   {'YES' if info.is_broad_margin else 'no'}")
    print(f"  Median content width:  {info.median_content_width_ratio:.1%}")
    print(f"  Median content height: {info.median_content_height_ratio:.1%}")
    b = info.unified_content_bbox
    print(f"  Unified content bbox:  ({b[0]:.1f}, {b[1]:.1f}, {b[2]:.1f}, {b[3]:.1f})")
    print(f"  Median margins (pt):   L={info.median_left_margin:.1f}  "
          f"R={info.median_right_margin:.1f}  "
          f"T={info.median_top_margin:.1f}  "
          f"B={info.median_bottom_margin:.1f}")

    if info.page_details:
        print(f"\n  {'─' * 60}")
        print(f"  {'Pg':>3} {'Chars':>6} {'W%':>6} {'H%':>6} "
              f"{'L':>6} {'R':>6} {'T':>6} {'B':>6} {'Broad':>6}")
        print(f"  {'─' * 60}")
        for p in info.page_details:
            flag = "  YES" if p.is_broad_margin else "   no"
            print(f"  {p.page_index + 1:>3} {p.char_count:>6} "
                  f"{p.content_width_ratio:>5.0%} {p.content_height_ratio:>5.0%} "
                  f"{p.left_margin:>6.1f} {p.right_margin:>6.1f} "
                  f"{p.top_margin:>6.1f} {p.bottom_margin:>6.1f} {flag}")
    print()


# ── CLI ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Detect and crop broad margins from legislative PDFs",
    )
    parser.add_argument("pdf", help="Path to the input PDF")
    parser.add_argument("--crop", "-c", action="store_true",
                        help="Crop the PDF and save a new file")
    parser.add_argument("--output", "-o", default=None,
                        help="Output path for cropped PDF")
    parser.add_argument("--padding", "-p", type=float, default=CROP_PADDING_PT,
                        help=f"Padding in pt around content (default {CROP_PADDING_PT})")
    parser.add_argument("--force", "-f", action="store_true",
                        help="Force crop even if margins look fine")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    info = detect_margins(args.pdf)
    print_report(info)

    if args.crop:
        out = crop_pdf_margins(args.pdf, output_path=args.output,
                               padding_pt=args.padding, force=args.force)
        if out:
            print(f"✅ Cropped PDF saved → {out}")
        else:
            print("ℹ️  No cropping needed (use --force to crop anyway).")
