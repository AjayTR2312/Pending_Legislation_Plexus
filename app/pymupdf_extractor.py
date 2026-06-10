"""
PyMuPDF-based PDF-to-Text Extractor with Underline & Strikethrough Detection
=============================================================================

Converts PDF pages to plain text while marking:
  • Underlined text   →  [A>…<A]
  • Strikethrough text →  [D>…<D]

Detection strategy — **character-level precision** (v2):
  1. PDF annotations   – /Underline and /StrikeOut annotation types
  2. Drawing analysis   – horizontal vector lines checked against each
                          character's bounding box individually
  3. Font-name heuristic – fonts named *Underline* or *StrikeOut*

Uses PyMuPDF's "rawdict" mode which returns per-character bboxes,
enabling precise tag boundaries even when a decoration line covers
only part of a text span.

Usage:
    from pymupdf_extractor import extract_pdf_with_pymupdf

    pages = extract_pdf_with_pymupdf("bill.pdf")
    for pg in pages:
        print(pg)

CLI:
    python pymupdf_extractor.py  input.pdf  [-o output.txt]  [--page 3]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import fitz  # PyMuPDF
except ImportError:
    raise ImportError(
        "PyMuPDF is required.  Install with:  pip install PyMuPDF"
    )

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------
# Tolerance (in points) when matching a drawing-line to a character
UNDERLINE_TOLERANCE_PT  = 4.0   # line within N pt below baseline
STRIKETHROUGH_TOL_PT    = 3.0   # line within N pt of vertical centre
HORIZONTAL_SLOPE_TOL    = 0.5   # max abs(dy) for a line to count as horizontal
CHAR_OVERLAP_RATIO      = 0.30  # line must overlap ≥30 % of char width

# --- Geometric paragraph-break detection (within a single text block) ------
# PyMuPDF often groups every line on a page into a single block, destroying
# paragraph boundaries.  We recover them by looking at the left-x of each
# line: in legislative PDFs, the FIRST line of a paragraph is "first-line
# indented" by ~3 ems relative to the wrapped continuation lines.
#
# A line is treated as a paragraph start if its left-x exceeds the block's
# continuation baseline by more than PARA_INDENT_DELTA_PT points.
PARA_INDENT_DELTA_PT     = 20.0  # min jump (pt) above continuation x for a new para
LINE_NUMBER_MAX_WIDTH_PT = 50.0  # narrow lines (e.g. "line 12") that aren't text
# When PyMuPDF splits ONE physical PDF line into two `tl` dicts because of a
# big horizontal gap (e.g. "5200.<gap>(a) Not less than…"), the two dicts
# share the same y-coordinate.  We treat consecutive lines whose y-mids
# differ by ≤ SAME_ROW_Y_TOL points as a single physical row.
SAME_ROW_Y_TOL_PT        = 2.0   # max y-mid delta for same-physical-row

# Tag delimiters (match the project convention)
ADD_OPEN, ADD_CLOSE = "[A>", "<A]"
DEL_OPEN, DEL_CLOSE = "[D>", "<D]"

# California margin line-number marker, e.g. "line 1", " line 27"
# Used to skip these narrow lines entirely during paragraph-break detection
# so they don't pollute the continuation-x baseline calculation.
_RE_CA_LINE_MARKER = re.compile(r"^\s*line\s+\d+\s*$", re.IGNORECASE)

# Small-caps font detection: fonts that render lowercase as visual capitals
# e.g. MinionPC (Petite Caps), *-SC (Small Caps)
_RE_SMALLCAPS_FONT = re.compile(
    r"(?:SC|SmallCap|SmCap|PetiteCap|PC)(?:[,-]|$)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Logo / letterhead exclusion
# ---------------------------------------------------------------------------
# Bounding box for state seal + letterhead text on page 1 (top-left region).
# Any text block whose bbox falls entirely within this zone is skipped.
# Coordinates in PDF points (1pt = 1/72 inch). Covers typical state seal area.
LOGO_EXCLUSION_ZONE = (0, 0, 350, 110)  # (x0, y0, x1, y1)

# Regex fallback: remove known logo letterhead text that may slip through
_RE_LOGO_TEXT = re.compile(
    r"(?:Legislative\s+Council,?\s*\n?\s*General\s+Assembly\s*\n?\s*State\s+of\s+Delaware)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class TextSpan:
    """One contiguous run of text with uniform formatting (used for summary)."""
    text: str
    bbox: Tuple[float, float, float, float]
    font: str
    size: float
    flags: int
    color: int
    origin: Tuple[float, float]
    underline: bool = False
    strikethrough: bool = False
    line_no: int = 0
    block_no: int = 0


@dataclass
class HLine:
    """A horizontal line segment extracted from page drawings."""
    x0: float
    y:  float
    x1: float
    width: float  # stroke width


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def _extract_hlines_from_drawings(page: fitz.Page) -> List[HLine]:
    """Return all ~horizontal line segments drawn on *page*."""
    hlines: List[HLine] = []
    try:
        drawings = page.get_drawings()
    except Exception:
        return hlines

    for path in drawings:
        stroke_width = path.get("width", 0.5)
        for item in path.get("items", []):
            kind = item[0]
            if kind == "l":
                p1, p2 = fitz.Point(item[1]), fitz.Point(item[2])
                dy = abs(p1.y - p2.y)
                if dy <= HORIZONTAL_SLOPE_TOL:
                    x0 = min(p1.x, p2.x)
                    x1 = max(p1.x, p2.x)
                    y  = (p1.y + p2.y) / 2
                    if x1 - x0 > 2:
                        hlines.append(HLine(x0=x0, y=y, x1=x1, width=stroke_width))
            elif kind == "re":
                rect = fitz.Rect(item[1])
                height = rect.height
                if height < 3.0 and rect.width > 2:
                    y = (rect.y0 + rect.y1) / 2
                    hlines.append(HLine(x0=rect.x0, y=y, x1=rect.x1, width=height))
    return hlines


# ---------------------------------------------------------------------------
# Table detection — identify table regions to avoid false underline tags
# ---------------------------------------------------------------------------

def _detect_table_zones(page: fitz.Page) -> List[Tuple[float, float, float, float]]:
    """Return bounding boxes ``(x0, y0, x1, y1)`` of tables found on *page*.

    Uses PyMuPDF's built-in ``find_tables()`` API.  Returns an empty list
    if no tables are detected or the API is unavailable.
    """
    try:
        tabs = page.find_tables()
        return [tuple(t.bbox) for t in tabs.tables]  # type: ignore[union-attr]
    except Exception:
        return []


def _filter_table_border_hlines(
    hlines: List[HLine],
    table_zones: List[Tuple[float, float, float, float]],
    *,
    tolerance: float = 3.0,
) -> List[HLine]:
    """Remove horizontal lines that are likely table borders.

    A line is classified as a **table border** (and removed) when:
      1. It falls inside (or on the edge of) a detected table bbox, **and**
      2. Its y-coordinate is close to the top/bottom edge of the table or
         of any internal row boundary — i.e. it spans most of the table
         width (≥ 60 %).  Short segments (e.g. a genuine underline on
         one word inside a cell) are kept.

    Lines completely outside every table zone are always kept.
    """
    if not table_zones:
        return hlines

    kept: List[HLine] = []
    for hl in hlines:
        is_border = False
        for zx0, zy0, zx1, zy1 in table_zones:
            # Is the line vertically inside (or on edge of) the table?
            if not (zy0 - tolerance <= hl.y <= zy1 + tolerance):
                continue
            # Is the line horizontally overlapping the table?
            overlap = _horizontal_overlap(hl.x0, hl.x1, zx0, zx1)
            table_width = zx1 - zx0
            if table_width > 0 and overlap / table_width >= 0.60:
                # Long line spanning most of the table width → border
                is_border = True
                break
            # Even shorter lines: if the line sits exactly on a row
            # boundary (top / bottom edge), treat as border.
            if (abs(hl.y - zy0) <= tolerance or abs(hl.y - zy1) <= tolerance):
                if overlap > 0:
                    is_border = True
                    break
        if not is_border:
            kept.append(hl)

    if len(kept) < len(hlines):
        logger.debug(
            "Filtered %d table-border hlines (kept %d)",
            len(hlines) - len(kept), len(kept),
        )
    return kept


def _horizontal_overlap(ax0: float, ax1: float, bx0: float, bx1: float) -> float:
    """Return the length of horizontal overlap between two intervals."""
    return max(0.0, min(ax1, bx1) - max(ax0, bx0))


def _extract_annot_rects(page: fitz.Page) -> Tuple[List[fitz.Rect], List[fitz.Rect]]:
    """Return (underline_rects, strikeout_rects) from PDF annotations."""
    ul_rects: List[fitz.Rect] = []
    so_rects: List[fitz.Rect] = []
    for annot in page.annots():
        atype = annot.type[0]
        if atype == 8:
            ul_rects.append(annot.rect)
        elif atype == 6:
            so_rects.append(annot.rect)
    return ul_rects, so_rects


def _bbox_overlaps_any_rect(bbox: Tuple[float, ...],
                             rects: List[fitz.Rect],
                             tolerance: float = 2.0) -> bool:
    """True if *bbox* overlaps with any annotation rect."""
    bx0, by0, bx1, by1 = bbox
    for r in rects:
        if (bx0 < r.x1 + tolerance and bx1 > r.x0 - tolerance and
                by0 < r.y1 + tolerance and by1 > r.y0 - tolerance):
            return True
    return False


# ---------------------------------------------------------------------------
# Character-level decoration classifier
# ---------------------------------------------------------------------------

def _classify_char_decoration(
    char_bbox: Tuple[float, float, float, float],
    font_size: float,
    ul_rects: List[fitz.Rect],
    so_rects: List[fitz.Rect],
    hlines: List[HLine],
    font_name: str = "",
) -> Tuple[bool, bool]:
    """
    Determine if a single character is underlined and/or struck-through.

    Returns (is_underline, is_strikethrough).
    """
    cx0, cy0, cx1, cy1 = char_bbox
    char_width = cx1 - cx0
    is_ul = False
    is_so = False

    # --- Layer 1: annotation rectangles ---
    if ul_rects and _bbox_overlaps_any_rect(char_bbox, ul_rects):
        is_ul = True
    if so_rects and _bbox_overlaps_any_rect(char_bbox, so_rects):
        is_so = True

    # --- Layer 2: drawing lines (most common for legislative PDFs) ---
    if (not is_ul or not is_so) and char_width >= 0.5:
        for hl in hlines:
            overlap = _horizontal_overlap(hl.x0, hl.x1, cx0, cx1)
            if char_width > 0 and overlap / char_width < CHAR_OVERLAP_RATIO:
                continue

            baseline_y = cy1
            midline_y  = (cy0 + cy1) / 2

            dy_baseline = hl.y - baseline_y
            dy_midline  = abs(hl.y - midline_y)

            if -3.0 <= dy_baseline <= UNDERLINE_TOLERANCE_PT and not is_ul:
                is_ul = True
            elif dy_midline <= STRIKETHROUGH_TOL_PT and not is_so:
                is_so = True
            else:
                relative_pos = (hl.y - cy0) / (cy1 - cy0) if cy1 > cy0 else 0.5
                if 0.30 <= relative_pos <= 0.70 and not is_so:
                    is_so = True

            if is_ul and is_so:
                break

    # --- Layer 3: font-name heuristic ---
    fl = font_name.lower()
    if "underline" in fl:
        is_ul = True
    if "strikeout" in fl or "strikethrough" in fl or "strike" in fl:
        is_so = True

    return is_ul, is_so


# ---------------------------------------------------------------------------
# Page-level extraction  (character-level precision)
# ---------------------------------------------------------------------------

def _extract_page_tagged_text(
    page: fitz.Page,
    mark_underline: bool = True,
    mark_strikethrough: bool = True,
    mark_italic: bool = False,
    mark_bold: bool = False,
    exclude_zones: Optional[List[Tuple[float, float, float, float]]] = None,
) -> str:
    """
    Build tagged text for *page* using per-character decoration detection.

    Uses ``page.get_text("rawdict")`` which provides individual character
    bounding boxes, so tag boundaries land exactly where the decoration
    line starts/ends — not at span boundaries.

    When *mark_italic* is True, italic spans are wrapped with [A>…<A]
    (PyMuPDF span flags bit 1 = italic).
    When *mark_bold* is True, bold spans are wrapped with [A>…<A]
    (PyMuPDF span flags bit 4 = bold).
    """
    # -- 1. Gather decoration sources --
    try:
        ul_rects, so_rects = _extract_annot_rects(page)
    except Exception:
        ul_rects, so_rects = [], []

    hlines = _extract_hlines_from_drawings(page)

    # -- 1b. Filter out table-border lines to avoid false underline tags --
    table_zones = _detect_table_zones(page)
    if table_zones:
        hlines = _filter_table_border_hlines(hlines, table_zones)

    # -- 2. Get text with per-character positions --
    # Try rawdict first for char-level precision; fall back to dict if
    # rawdict yields no usable character data (some PDFs have text in
    # structures that rawdict can't parse).
    text_dict = page.get_text("rawdict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
    total_chars = sum(
        len(s.get("chars", []))
        for b in text_dict.get("blocks", []) if b.get("type") == 0
        for l in b.get("lines", [])
        for s in l.get("spans", [])
    )
    if total_chars == 0:
        logger.debug("rawdict returned 0 chars — falling back to dict mode")
        text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)

    output: List[str] = []
    in_add = False
    in_del = False
    first_block = True

    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:       # skip image blocks
            continue

        # --- Skip blocks inside exclusion zones (e.g. state seal/logo) ---
        if exclude_zones:
            blk_bbox = block.get("bbox", (0, 0, 0, 0))
            skip_block = False
            for zone in exclude_zones:
                zx0, zy0, zx1, zy1 = zone
                # Block is inside zone if its bbox is fully contained
                if (blk_bbox[0] >= zx0 - 5 and blk_bbox[1] >= zy0 - 5 and
                    blk_bbox[2] <= zx1 + 5 and blk_bbox[3] <= zy1 + 5):
                    skip_block = True
                    break
            if skip_block:
                logger.debug("Skipping block in exclusion zone: bbox=%s", blk_bbox)
                continue

        # --- block break: close tags, insert paragraph gap ---
        if not first_block:
            if in_add:
                output.append(ADD_CLOSE); in_add = False
            if in_del:
                output.append(DEL_CLOSE); in_del = False
            output.append("\n\n")
        first_block = False

        # ── Determine this block's "continuation x" baseline ─────────────
        # Wrapped lines align at the block's leftmost x; the first line of
        # a paragraph is indented ~30pt to the right.  We compute the
        # baseline from the lines that LOOK like wrapped continuations:
        # full-width (so they are real text, not narrow line-number
        # markers) and not a California margin marker.
        block_lines = block.get("lines", [])
        candidate_xs: List[float] = []
        for _ln in block_lines:
            _lbb = _ln.get("bbox", (0, 0, 0, 0))
            _lwidth = _lbb[2] - _lbb[0]
            if _lwidth < LINE_NUMBER_MAX_WIDTH_PT:
                continue  # skip narrow margin markers
            # Collect text from BOTH rawdict (chars) and dict (text) so
            # the marker regex works regardless of which mode was used.
            _ltxt = ""
            for _s in _ln.get("spans", []):
                _cd = _s.get("chars", [])
                if _cd:
                    _ltxt += "".join(c.get("c", "") for c in _cd)
                else:
                    _ltxt += _s.get("text", "")
            if _RE_CA_LINE_MARKER.match(_ltxt):
                continue  # skip "line N" markers
            candidate_xs.append(_lbb[0])
        # Continuation baseline = minimum left-x of the real text lines.
        # If a block has no qualifying lines (e.g. a single-line header),
        # the threshold will never trigger and behaviour matches the old
        # \n-only path.
        continuation_x = min(candidate_xs) if candidate_xs else None
        para_indent_threshold = (
            continuation_x + PARA_INDENT_DELTA_PT
            if continuation_x is not None else None
        )

        first_line_in_block = True
        prev_line_y_mid: Optional[float] = None
        for line in block_lines:
            lbbox_full = line.get("bbox", (0, 0, 0, 0))
            line_x0 = lbbox_full[0]
            line_width = lbbox_full[2] - lbbox_full[0]
            line_y_mid = (lbbox_full[1] + lbbox_full[3]) / 2

            # --- Skip California margin "line N" markers entirely ---
            # They sit on the same y-row as the real text line they
            # number, so emitting them would (a) double the line count
            # and (b) destroy the paragraph-indent signal for the line
            # that follows on the same row.
            #
            # IMPORTANT: collect text from BOTH rawdict (chars) AND dict
            # (span text) so the marker regex works regardless of which
            # mode was used.  In rawdict, span["text"] is empty.
            line_raw_text = ""
            for _sp in line.get("spans", []):
                _cd = _sp.get("chars", [])
                if _cd:
                    line_raw_text += "".join(c.get("c", "") for c in _cd)
                else:
                    line_raw_text += _sp.get("text", "")
            if (line_width < LINE_NUMBER_MAX_WIDTH_PT
                    and _RE_CA_LINE_MARKER.match(line_raw_text)):
                continue

            if not first_line_in_block:
                # Detect same-physical-row split: PyMuPDF sometimes splits
                # ONE PDF line into multiple `tl` dicts when there's a
                # large horizontal gap between text runs (e.g. "5200."
                # followed by a wide indent then "(a) Not less than…").
                # Both dicts share the same y-coordinate but have very
                # different x-coordinates.  We must NOT treat the second
                # one as a new paragraph — it's literally the same line.
                same_physical_row = (
                    prev_line_y_mid is not None
                    and abs(line_y_mid - prev_line_y_mid) <= SAME_ROW_Y_TOL_PT
                )

                if same_physical_row:
                    # Close tags + emit a single space.  The downstream
                    # `consolidate_adjacent_tags` (Phase 1a) will merge
                    # the resulting "<T] [T>" pair into one tag span.
                    if in_add:
                        output.append(ADD_CLOSE); in_add = False
                    if in_del:
                        output.append(DEL_CLOSE); in_del = False
                    output.append(" ")
                else:
                    # Decide separator BEFORE emitting this line.
                    # A first-line-indented line starts a NEW paragraph
                    # (\n\n); otherwise it's a wrapped continuation of
                    # the prior line (single \n, same as before).
                    is_para_start = (
                        para_indent_threshold is not None
                        and line_x0 >= para_indent_threshold
                    )
                    # Close tags before the break — they will be reopened
                    # if the decoration continues on the next line.
                    if in_add:
                        output.append(ADD_CLOSE); in_add = False
                    if in_del:
                        output.append(DEL_CLOSE); in_del = False
                    output.append("\n\n" if is_para_start else "\n")
            first_line_in_block = False
            prev_line_y_mid = line_y_mid

            # --- Skip ALL decoration tagging for lines inside a table zone ---
            # Table cell borders are often short horizontal lines that slip
            # past the width-based hline filter but still trigger false
            # underline detection.  Suppress tagging entirely for table text.
            line_in_table = False
            if table_zones:
                lbbox = line.get("bbox", (0, 0, 0, 0))
                for zx0, zy0, zx1, zy1 in table_zones:
                    if (lbbox[1] >= zy0 - 2 and lbbox[3] <= zy1 + 2 and
                        lbbox[0] >= zx0 - 5 and lbbox[2] <= zx1 + 5):
                        line_in_table = True
                        break

            for span in line.get("spans", []):
                chars_data = span.get("chars", [])
                font_size  = span.get("size", 12)
                font_name  = span.get("font", "")
                span_text  = span.get("text", "")

                # --- Detect italic (bit 1) and bold (bit 4) from span flags ---
                span_flags = span.get("flags", 0)
                span_is_italic = bool(span_flags & 2)
                span_is_bold   = bool(span_flags & 16)

                # --- Detect small-caps font → uppercase extracted text ---
                span_is_smallcaps = bool(_RE_SMALLCAPS_FONT.search(font_name))

                # --- Fallback: no char-level data → use span bbox ---
                if not chars_data:
                    if span_text:
                        if span_is_smallcaps:
                            span_text = span_text.upper()
                        s_bbox = tuple(span.get("bbox", (0, 0, 0, 0)))
                        s_ul, s_so = _classify_char_decoration(
                            s_bbox, font_size, ul_rects, so_rects,
                            hlines, font_name,
                        )
                        span_is_whitespace = span_text.strip() == ""
                        need_add = (s_ul and mark_underline) or (span_is_italic and mark_italic and not span_is_whitespace) or (span_is_bold and mark_bold and not span_is_whitespace)
                        need_del = s_so and mark_strikethrough
                        # Suppress all tagging inside table zones
                        if line_in_table:
                            need_add = False
                            need_del = False
                        # Delete (strikethrough) takes precedence — suppress add to avoid nested tags
                        if need_del and need_add:
                            need_add = False
                        if in_add and not need_add:
                            output.append(ADD_CLOSE); in_add = False
                        if in_del and not need_del:
                            output.append(DEL_CLOSE); in_del = False
                        if need_del and not in_del:
                            output.append(DEL_OPEN); in_del = True
                        if need_add and not in_add:
                            output.append(ADD_OPEN); in_add = True
                        output.append(span_text)
                    continue

                # --- Character-by-character classification ---
                for ch_info in chars_data:
                    c = ch_info.get("c", "")
                    if not c:
                        continue

                    # Uppercase chars from small-caps fonts
                    if span_is_smallcaps:
                        c = c.upper()

                    bbox = tuple(ch_info.get("bbox", (0, 0, 0, 0)))
                    cw   = bbox[2] - bbox[0]

                    # Zero-width chars (some spaces): not decorated
                    if cw < 0.5:
                        is_ul, is_so = False, False
                    else:
                        is_ul, is_so = _classify_char_decoration(
                            bbox, font_size, ul_rects, so_rects,
                            hlines, font_name,
                        )

                    # For italic/bold (span-level flags), don't apply to whitespace —
                    # only underline/strikethrough (physical decorations) should tag spaces.
                    char_is_space = c.strip() == ""
                    need_add = (is_ul and mark_underline) or (span_is_italic and mark_italic and not char_is_space) or (span_is_bold and mark_bold and not char_is_space)
                    need_del = is_so and mark_strikethrough
                    # Suppress all tagging inside table zones
                    if line_in_table:
                        need_add = False
                        need_del = False

                    # Delete (strikethrough) takes precedence — suppress add to avoid nested tags
                    if need_del and need_add:
                        need_add = False

                    # -- close tags no longer needed --
                    if in_add and not need_add:
                        output.append(ADD_CLOSE); in_add = False
                    if in_del and not need_del:
                        output.append(DEL_CLOSE); in_del = False

                    # -- open tags now needed --
                    if need_del and not in_del:
                        output.append(DEL_OPEN); in_del = True
                    if need_add and not in_add:
                        output.append(ADD_OPEN); in_add = True

                    output.append(c)

    # close trailing tags
    if in_add:
        output.append(ADD_CLOSE)
    if in_del:
        output.append(DEL_CLOSE)

    return "".join(output)


# ---------------------------------------------------------------------------
# Span-level extraction (kept for summary / diagnostics)
# ---------------------------------------------------------------------------

def _extract_page_spans(page: fitz.Page) -> List[TextSpan]:
    """
    Span-level extraction — used only for ``extract_pdf_summary()``.
    """
    try:
        ul_rects, so_rects = _extract_annot_rects(page)
    except Exception:
        ul_rects, so_rects = [], []

    hlines = _extract_hlines_from_drawings(page)

    # Filter out table-border lines
    table_zones = _detect_table_zones(page)
    if table_zones:
        hlines = _filter_table_border_hlines(hlines, table_zones)

    text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
    spans: List[TextSpan] = []
    block_idx = 0

    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line_idx, line in enumerate(block.get("lines", [])):
            for sp in line.get("spans", []):
                txt = sp.get("text", "")
                if not txt:
                    continue
                bbox  = tuple(sp["bbox"])
                font  = sp.get("font", "")
                size  = sp.get("size", 12)
                ts = TextSpan(
                    text=txt, bbox=bbox, font=font, size=size,
                    flags=sp.get("flags", 0), color=sp.get("color", 0),
                    origin=tuple(sp.get("origin", (bbox[0], bbox[3]))),
                    line_no=line_idx, block_no=block_idx,
                )
                if ul_rects and _bbox_overlaps_any_rect(bbox, ul_rects):
                    ts.underline = True
                if so_rects and _bbox_overlaps_any_rect(bbox, so_rects):
                    ts.strikethrough = True
                if not ts.underline or not ts.strikethrough:
                    for hl in hlines:
                        res = _classify_char_decoration(bbox, size, [], [], [hl], font)
                        if res[0]:
                            ts.underline = True
                        if res[1]:
                            ts.strikethrough = True
                        if ts.underline and ts.strikethrough:
                            break
                spans.append(ts)
        block_idx += 1
    return spans


# ---------------------------------------------------------------------------
# Post-processing
# ---------------------------------------------------------------------------

_RE_EMPTY_ADD = re.compile(r"\[A>\s*<A\]")
_RE_EMPTY_DEL = re.compile(r"\[D>\s*<D\]")
# Merge adjacent same-type tags that are separated only by intra-paragraph
# whitespace (spaces, tabs, or a single soft line wrap).  CRITICALLY this
# pattern must NOT eat a paragraph break (\n\n) — two italic paragraphs that
# happen to sit adjacent in a digest must remain separate, not be welded
# together into one giant [A>…<A] span.
#
# Matches: "<A] [A>", "<A]\t[A>", "<A]\n[A>" (with optional surrounding spaces)
# Rejects: "<A]\n\n[A>"  (paragraph boundary — left alone)
_RE_MERGE_ADD = re.compile(r"<A\][ \t]*\n?[ \t]*\[A>")
_RE_MERGE_DEL = re.compile(r"<D\][ \t]*\n?[ \t]*\[D>")
# Matches margin line numbers at start of a line.
# IMPORTANT: trailing class is [ \t]* (NOT \s*) so it doesn't consume the
# trailing newlines.  Eating newlines would collapse paragraph breaks that
# the extractor emits as \n\n right after a "line N" marker line.
_RE_LINE_NUMBER = re.compile(r"^[ \t]*line[ \t]+\d+[ \t]*", re.MULTILINE)
# Matches margin line numbers sandwiched between tag boundaries
# e.g.  "<A] line 40 [A>"  or  "<A]\n line 6\n"  (across lines)
# Same rationale as above: keep [ \t]* on the right to preserve \n.
_RE_LINE_NUMBER_BETWEEN_TAGS = re.compile(
    r"(<[AD]\])[ \t]*line[ \t]+\d+[ \t]*", re.MULTILINE
)


def _clean_tagged_text(text: str, strip_line_numbers: bool = False) -> str:
    """Remove empty tags, merge adjacent same-type tags, tidy whitespace."""
    # Normalize non-breaking spaces to regular spaces
    text = text.replace("\xa0", " ")
    # Strip line numbers FIRST — before tag merging so that
    # "<A] line 40 [A>" becomes "<A] [A>" and can then merge.
    if strip_line_numbers:
        text = _RE_LINE_NUMBER.sub("", text)
        text = _RE_LINE_NUMBER_BETWEEN_TAGS.sub(r"\1 ", text)
    text = _RE_EMPTY_ADD.sub("", text)
    text = _RE_EMPTY_DEL.sub("", text)
    # Merge adjacent same-type tags separated only by whitespace
    text = _RE_MERGE_ADD.sub(" ", text)
    text = _RE_MERGE_DEL.sub(" ", text)
    # Optionally strip margin line numbers (e.g. California bills)
    if strip_line_numbers:
        text = _RE_LINE_NUMBER.sub("", text)
    # Remove logo/letterhead text that may have slipped through
    text = _RE_LOGO_TEXT.sub("", text)
    # Normalize small runs of multiple spaces (2-3) to single space
    # but preserve larger gaps (4+) which are intentional layout formatting
    text = re.sub(r"(?<=\S) {2,3}(?=\S)", " ", text)
    # Collapse excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# CT header tag stripping — remove false [A>…<A] from page-1 header lines
# ---------------------------------------------------------------------------
# CT bill headers often have decorative horizontal lines that the underline
# detector misreads.  These regexes match the header patterns and strip tags.
_RE_CT_HEADER_PATTERNS = re.compile(
    r"\[A>(.*?(?:"
    r"General\s+Assembly"
    r"|(?:Raised|Substitute|File|House|Senate)\s+Bill\s+No\.\s*\d+"
    r"|File\s+No\.\s*\d+"
    r"|(?:s[SH]B|[SH]\.?B\.?)\s*\d+(?:\s*/\s*File\s+No\.\s*\d+)?"
    r"|LCO\s+No\.\s*\d+"
    r"|(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+Session,?\s*\d{4}"
    r"|Referred\s+to\s+Committee"
    r"|Introduced\s+by"
    r"|AN\s+ACT\s+CONCERNING"
    r").*?)<A\]",
    re.IGNORECASE,
)


def _strip_ct_header_tags(text: str) -> str:
    """Remove false ``[A>…<A]`` tags from CT bill header lines.

    Connecticut PDFs have decorative lines near the page-1 header that
    the underline detector picks up as underlines.  This function strips
    those tags from header-pattern lines while leaving genuine legislative
    tags untouched.

    Only operates on the first ~15 lines (the header area).
    """
    lines = text.split("\n")
    # Only process the header area (first 15 lines)
    header_end = min(15, len(lines))
    changed = False
    for i in range(header_end):
        new_line = _RE_CT_HEADER_PATTERNS.sub(r"\1", lines[i])
        if new_line != lines[i]:
            lines[i] = new_line
            changed = True
    if changed:
        logger.debug("Stripped false [A> tags from CT header lines")
        return "\n".join(lines)
    return text


# ---------------------------------------------------------------------------
# CT page-1 header restructuring
# ---------------------------------------------------------------------------
# PyMuPDF extracts "General Assembly" and the bill number on the same line
# because they sit side-by-side in the PDF.  The correct output structure
# puts the bill number on line 1, then "General Assembly" on a separate line.

_RE_CT_HEADER_COMBINED = re.compile(
    r"^((?:Senate|House)\s+)?"                                    # optional chamber
    r"General\s+Assembly\s+"                                      # "General Assembly"
    r"((?:(?:Raised|Substitute|Committee|File)\s+)?(?:(?:Senate|House)\s+)?Bill\s+No\.\s*\d+"  # bill identifier
    r"|File\s+No\.\s*\d+)",                                       # or "File No. NNN"
    re.IGNORECASE,
)


def _restructure_ct_header(text: str) -> str:
    """Split 'General Assembly Raised Bill No. XXXX' into separate lines.

    PyMuPDF places these side-by-side elements on one line.  The correct
    output structure is::

        Raised Bill No. 6846

        General Assembly

    Also handles variants like ``Senate  File No. 620`` and
    ``Substitute Senate Bill No. 508``.

    Only operates on the first few lines of the document.
    """
    lines = text.split("\n")
    # Only check the first 5 non-empty lines
    for i in range(min(10, len(lines))):
        m = _RE_CT_HEADER_COMBINED.match(lines[i].strip())
        if m:
            chamber = (m.group(1) or "").strip()
            bill_id = m.group(2).strip()
            # Rebuild: bill number first, then General Assembly
            new_lines = []
            if chamber:
                new_lines.append(chamber)
                new_lines.append("")
            new_lines.append(bill_id)
            new_lines.append("")
            new_lines.append("General Assembly")
            # Preserve any trailing text on the same line after the match
            remainder = lines[i].strip()[m.end():].strip()
            if remainder:
                new_lines.append("")
                new_lines.append(remainder)
            lines[i] = "\n".join(new_lines)
            logger.debug("Restructured CT header: bill number + General Assembly split")
            return "\n".join(lines)
    return text


# ---------------------------------------------------------------------------
# CT compact bill designator removal (sSB508 / File No. 620, etc.)
# ---------------------------------------------------------------------------
# These are running headers that appear on every page — they duplicate the
# official identifiers and should always be removed.
_RE_CT_COMPACT_DESIGNATOR = re.compile(
    r"^\s*(?:\[A>)?\s*"
    r"(?:s[SH]B|[SH]\.?B\.?)\s*\d+"
    r"\s*/\s*File\s+No\.\s*\d+"
    r"\s*(?:<A\])?\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _strip_ct_compact_designator(text: str) -> str:
    """Remove compact bill designator lines like ``sSB508 / File No. 620``.

    These are running headers that appear on every page of CT bills.
    They duplicate the official identifiers (``Substitute Senate Bill No. 508``
    and ``File No. 620``) and should be removed from the clean output.
    """
    new_text = _RE_CT_COMPACT_DESIGNATOR.sub("", text)
    if new_text != text:
        # Clean up blank lines left behind
        new_text = re.sub(r"\n{3,}", "\n\n", new_text)
        logger.debug("Stripped CT compact bill designator lines")
    return new_text


# ---------------------------------------------------------------------------
# Kansas (KS) header tag stripping — remove false [A>…<A] from bill title
# ---------------------------------------------------------------------------
# Kansas bill PDFs have decorative italic header text that the
# underline/italic detector picks up, producing false [A> tags.
_RE_KS_HEADER_PATTERNS = re.compile(
    r"\[A>(.*?(?:"
    r"Session\s+of\s+\d{4}"
    r"|(?:HOUSE|SENATE)\s+(?:BILL|RESOLUTION|CONCURRENT\s+RESOLUTION|SUBSTITUTE\s+FOR)\s+(?:No\.?\s*)?\d*"
    r"|(?:SUBSTITUTE\s+FOR\s+)?(?:HOUSE|SENATE)\s+BILL\s+(?:No\.?\s*)?\d+"
    r"|By\s+Committee\s+on"
    r"|By\s+(?:Representative|Senator)s?"
    r"|Requested\s+by\s+(?:Representative|Senator|Governor)"
    r"|AN\s+ACT\s+concerning"
    r").*?)<A\]",
    re.IGNORECASE,
)


def _strip_ks_header_tags(text: str) -> str:
    """Remove false ``[A>…<A]`` tags from KS bill header lines.

    Kansas PDFs have decorative/italic header text that the detector
    picks up as underlines.  This strips those tags from header-pattern
    lines while leaving genuine legislative tags untouched.

    Only operates on the first ~15 lines (the header area).
    """
    lines = text.split("\n")
    header_end = min(15, len(lines))
    changed = False
    for i in range(header_end):
        new_line = _RE_KS_HEADER_PATTERNS.sub(r"\1", lines[i])
        if new_line != lines[i]:
            lines[i] = new_line
            changed = True
    if changed:
        logger.debug("Stripped false [A> tags from KS header lines")
        return "\n".join(lines)
    return text


# ---------------------------------------------------------------------------
# Indiana (IN) deterministic post-processing
# ---------------------------------------------------------------------------

# Regex: IN bill reference footer — "IN 1703—LS 7777/DI 92", "SB 85—LS 6422/DI 104"
_RE_IN_FOOTER = re.compile(
    r"^\s*(?:[A-Z]{1,3}\s+\d+\s*[—–-]\s*LS\s+\d+\s*/\s*DI\s+\d+)\s*$",
    re.MULTILINE,
)

# ---------------------------------------------------------------------------
# IN header tag stripping — remove false [A>…<A] from bill title lines
# ---------------------------------------------------------------------------
# Indiana bill PDFs have a decorative horizontal line near the top that the
# underline detector misreads, producing false [A> tags on the bill title.
_RE_IN_HEADER_PATTERNS = re.compile(
    r"\[A>(.*?(?:"
    r"(?:ENGROSSED|ENROLLED|INTRODUCED|REPRINTED)\s+"
    r"(?:HOUSE|SENATE)\s+(?:BILL|RESOLUTION|CONCURRENT\s+RESOLUTION|JOINT\s+RESOLUTION)"
    r"\s+No\.\s*\d+"
    r"|(?:HOUSE|SENATE)\s+(?:BILL|RESOLUTION)\s+No\.\s*\d+"
    r"|DIGEST\s+OF\s+(?:HB|SB|HR|SR)\s+\d+"
    r"|(?:First|Second|Third)\s+Regular\s+Session"
    r"|(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s*\d{4}"
    r").*?)<A\]",
    re.IGNORECASE,
)


def _strip_in_header_tags(text: str) -> str:
    """Remove false ``[A>…<A]`` tags from IN bill header lines.

    Indiana PDFs have decorative lines near the page-1 header that the
    underline detector picks up as underlines.  This strips those tags
    from header-pattern lines (bill title, date, digest) while leaving
    genuine legislative tags untouched.

    Only operates on the first ~15 lines (the header area).
    """
    lines = text.split("\n")
    header_end = min(15, len(lines))
    changed = False
    for i in range(header_end):
        new_line = _RE_IN_HEADER_PATTERNS.sub(r"\1", lines[i])
        if new_line != lines[i]:
            lines[i] = new_line
            changed = True
    if changed:
        logger.debug("Stripped false [A> tags from IN header lines")
        return "\n".join(lines)
    return text


# Regex: Barcode/scan text — "*EH1204.1*", "*HB1690.1*", "*SB85.1*"
_RE_IN_BARCODE = re.compile(
    r"^\s*\*[A-Z]{1,3}\d+\.\d+\*\s*$",
    re.MULTILINE,
)

# Regex: Standalone year footer — "2025" or "2026" alone on a line
_RE_IN_YEAR_FOOTER = re.compile(
    r"^\s*(20\d{2})\s*$",
    re.MULTILINE,
)

# Regex: Decorative divider lines tagged as underline — [A>____<A] or [A>———<A]
# These are horizontal rules in the PDF that the underline detector wraps.
_RE_IN_DECORATIVE_LINE = re.compile(
    r"^\s*\[A>[_\s\u2014\u2013\-]+<A\]\s*$",
    re.MULTILINE,
)


def _strip_in_artifacts(text: str) -> str:
    """Remove Indiana-specific artifacts: LS/DI footers, barcode text, year footers,
    decorative divider lines.

    These appear on every page of Indiana bills and are not legislative content.
    """
    # Remove barcode text first (most distinctive)
    new_text = _RE_IN_BARCODE.sub("", text)

    # Remove LS/DI footer lines
    new_text = _RE_IN_FOOTER.sub("", new_text)

    # Remove decorative divider lines ([A>_____<A])
    new_text = _RE_IN_DECORATIVE_LINE.sub("", new_text)

    # Remove standalone year lines ONLY when adjacent to where a footer was
    # (i.e. we already removed something above). This avoids removing "2025"
    # if it appears in actual legislative content like "effective 2025".
    # Safe heuristic: if we already removed footer lines, also remove year lines.
    if new_text != text:
        new_text = _RE_IN_YEAR_FOOTER.sub("", new_text)

    if new_text != text:
        # Clean up excess blank lines left behind
        new_text = re.sub(r"\n{3,}", "\n\n", new_text)
        logger.debug("Stripped IN artifacts (footer/barcode/year/decorative lines)")

    return new_text


# Regex: detect false [D>] tags around parenthesized subsection markers.
# The LLM sometimes wraps (a), (b), (j), (1) etc. with deletion tags.
_RE_FALSE_D_SUBSECTION = re.compile(
    r"\[D>\(([a-zA-Z0-9]{1,3})\)<D\]",
)


def _strip_false_subsection_d_tags(text: str) -> str:
    """Remove false ``[D>(x)<D]`` tags around subsection markers.

    The LLM sometimes wraps parenthesized labels like ``(a)``, ``(j)``,
    ``(1)`` with ``[D>…<D]`` tags.  These are paragraph markers, not
    legislative deletions (which use ``[square brackets]``).
    """
    new_text = _RE_FALSE_D_SUBSECTION.sub(r"(\1)", text)
    if new_text != text:
        logger.debug("Stripped false [D>] tags from subsection markers")
    return new_text


# Regex: match [...] that are NOT our [A>...<A] or [D>...<D] tags.
# Negative lookahead ensures we skip [A> and [D> openings.
_RE_BRACKET_DEL = re.compile(
    r"\[(?![AD]>)"       # opening [ not followed by A> or D>
    r"((?:(?!\[(?:[AD]>)).)*?)"  # content (non-greedy, no nested tag opens)
    r"\]",              # closing ]
    re.DOTALL,
)


def _tag_brackets(text: str) -> str:
    """Wrap bracket-enclosed text ``[…]`` with ``[D>[…]<D]`` tags.

    Used for states (e.g. CT, WA) where square brackets denote
    proposed deletions.  Existing ``[A>…<A]`` and ``[D>…<D]`` tags
    are left untouched.

    Only brackets in *untagged* segments are wrapped — brackets that
    already sit inside a ``[D>…<D]`` or ``[A>…<A]`` span (e.g. from
    strikethrough detection) are skipped to prevent double-nesting.
    """
    # Split the text into (tagged-span, gap-between-spans) segments so
    # that we only apply bracket-wrapping to the untagged gaps.
    _tag_re = re.compile(r"\[([AD])>(.*?)<\1\]", re.DOTALL)
    parts: list[str] = []
    last_end = 0
    for m in _tag_re.finditer(text):
        # Untagged segment before this tag — wrap any bare brackets
        segment = text[last_end : m.start()]
        parts.append(_RE_BRACKET_DEL.sub(r"[D>[\1]<D]", segment))
        # Keep the existing tag as-is
        parts.append(m.group())
        last_end = m.end()
    # Remaining untagged tail
    parts.append(_RE_BRACKET_DEL.sub(r"[D>[\1]<D]", text[last_end:]))
    return "".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_pdf_with_pymupdf(
    pdf_path: str | Path,
    *,
    pages: Optional[List[int]] = None,
    mark_underline: bool = True,
    mark_strikethrough: bool = True,
    mark_italic: bool = False,
    mark_bold: bool = False,
    mark_brackets: bool = False,
    strip_line_numbers: bool = False,
    join_pages: bool = False,
    page_separator: str = "\n\n--- Page {n} ---\n\n",
) -> List[str] | str:
    """
    Extract text from a PDF, tagging underline and strikethrough spans.

    Parameters
    ----------
    pdf_path : str or Path
        Path to the input PDF file.
    pages : list[int], optional
        0-based page indices to extract.  ``None`` → all pages.
    mark_underline : bool
        Wrap underlined text with ``[A>…<A]``.
    mark_strikethrough : bool
        Wrap strikethrough text with ``[D>…<D]``.
    mark_italic : bool
        Wrap italic text with ``[A>…<A]``.
    mark_bold : bool
        Wrap bold text with ``[A>…<A]``.
    strip_line_numbers : bool
        Remove margin line numbers (e.g. ``line 1``, ``line 23``).
    join_pages : bool
        If True, return a single string instead of a list.
    page_separator : str
        Separator inserted between pages when *join_pages* is True.
        ``{n}`` is replaced with the 1-based page number.

    Returns
    -------
    list[str] or str
        Per-page text (list) or concatenated text (str).
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    doc = fitz.open(str(pdf_path))
    page_indices = pages if pages is not None else range(len(doc))
    results: List[str] = []

    for idx in page_indices:
        if idx < 0 or idx >= len(doc):
            logger.warning("Skipping out-of-range page index %d", idx)
            results.append("")
            continue

        page = doc[idx]
        logger.info("Processing page %d / %d …", idx + 1, len(doc))

        raw_text = _extract_page_tagged_text(
            page,
            mark_underline=mark_underline,
            mark_strikethrough=mark_strikethrough,
            mark_italic=mark_italic,
            mark_bold=mark_bold,
            exclude_zones=None,
        )
        clean = _clean_tagged_text(raw_text, strip_line_numbers=strip_line_numbers)
        # NOTE: _tag_brackets() is NOT called here.  It runs AFTER the
        # LLM clean step (in clean_extracted_text) so the LLM never sees
        # confusing nested [D>[…]<D] structures.
        results.append(clean)

    doc.close()

    if join_pages:
        parts = []
        for i, txt in enumerate(results):
            if page_separator and i > 0:
                parts.append(page_separator.replace("{n}", str(
                    (pages[i] if pages else i) + 1
                )))
            parts.append(txt)
        return "\n".join(parts)

    return results


# ---------------------------------------------------------------------------
# LLM-based text cleaner — uses Claude Opus 4.6 + state-specific prompts
# ---------------------------------------------------------------------------

# Fallback universal prompt (used when no state-specific prompt exists)
_CLEAN_SYSTEM_PROMPT_FALLBACK = """\
You are a **legislative-document text cleaner**.  Your ONLY job is to remove
PDF extraction artefacts from the text below while preserving **100 %** of the
legislative content.  You must work for ANY state, ANY bill format.

══════════════════════════════════════════════════════════════
STRUCTURAL METADATA (if present at start of input)
══════════════════════════════════════════════════════════════

The input text may begin with a metadata block delimited by:
  ═══ PDF STRUCTURAL METADATA (detected by PyMuPDF) ═══
  ...
  ═══ END METADATA ═══

USE THIS METADATA to understand the document structure:
- **Tables**: The metadata shows table rows and columns extracted from the PDF.
  Render each table row on a SINGLE line with large spacing between columns.
  Example: "Section 1          October 1, 2025          5-192f(d)"
  Do NOT split table rows across multiple lines.
- **Table tags**: Strip any [A>...<A] or [D>...<D] tags that appear on table
  cell content — table borders are NOT underlines. They are false positives.
- **Do NOT** include the metadata block itself in your output.

══════════════════════════════════════════════════════════════
MANDATORY REMOVALS  (remove these and NOTHING else)
══════════════════════════════════════════════════════════════

1. **Page-break markers**
   Lines like  `--- Page 2 ---`, `--- Page N ---`, etc.

2. **Repeated headers / footers on pages 2+**
   Any block of text that appears identically (or near-identically) at the
   top or bottom of every page AFTER page 1.  Common examples:
     • Bill number header  ("Raised Bill No. 1193", "House Bill 4321", …)
     • LCO / LC / LSO / Session codes  ("LCO No. 2482", "LC 1234", …)
     • Chamber / session labels  ("General Assembly", "January Session, 2025")
     • Page fractions  ("1 of 7", "2 of 7", …)
     • Sponsor / committee repeats  ("Referred to …", "(ET)")
   ➜  **Keep** the very first occurrence (page 1 header) — it is the official
      bill identifier.

3. **Margin line numbers**
   Bare numbers that appear at the START or END of content lines and
   represent line numbering (1, 2, 3 … running sequentially).
   ➜  Only strip them if the pattern is consistent (most or all lines
      have a sequential number).

4. **Page-position markers**
   `<<<PAGE_N_START>>>`, `<<<PAGE_N_END>>>`, or similar.

══════════════════════════════════════════════════════════════
ABSOLUTE PRESERVATION RULES  (never violate)
══════════════════════════════════════════════════════════════

🛡️  PRESERVE every `[A>…<A]` and `[D>…<D]` tag that wraps **legislative
     content** (bill text, sections, clauses) — these are **sacred**.
🛡️  When you remove a repeated header/footer that happens to be wrapped
     in `[A>…<A]` tags, remove the ENTIRE tagged block (tag + text).
     This is expected — header tags are artefacts just like the header text.
🛡️  PRESERVE every word of legislative text — acts, sections, clauses,
     enacting formulas, sponsor names, effective-date tables, statements
     of purpose, explanatory notes.
🛡️  PRESERVE the bill header block on page 1 (title, bill number, etc.)
    — clean it, but do NOT delete it (even if it has [A>…<A] tags).
🛡️  **FALSE HEADER TAGS**: Bill number lines (e.g. "HOUSE BILL No. 1690",
     "Raised Bill No. 6846"), committee names (e.g. "Rules and Legislative
     Procedures"), version labels (e.g. "Introduced Version"), digest headings,
     and other header/structural elements are often bold or sit near decorative
     lines — the extractor may falsely wrap them in [A>…<A] tags.
     ➜  REMOVE the [A>…<A] tags from these header elements, but KEEP the text.
     ➜  This applies to ALL pages, not just page 1.
🛡️  PRESERVE section / subsection numbering ("Section 1.", "(a)", "(2)", …).
🛡️  When in doubt whether something is content or artefact → **KEEP IT**.

══════════════════════════════════════════════════════════════
OUTPUT FORMAT
══════════════════════════════════════════════════════════════

• Return ONLY the cleaned text — no commentary, no markdown fences.
• Reflow lines naturally into paragraphs; collapse excessive blank lines
  to a single blank line between paragraphs.
• Do NOT reword, paraphrase, or reorder anything.

══════════════════════════════════════════════════════════════
STRUCTURAL METADATA  (if present)
══════════════════════════════════════════════════════════════

The text may begin with a metadata block delimited by
``═══ PDF STRUCTURAL METADATA ═══`` … ``═══ END METADATA ═══``.

• Use this metadata to understand table layouts, header structure, etc.
• **Tables**: Render each table row on a SINGLE line with large spacing
  between columns (e.g. ``Section 1          October 1, 2025          5-192f(d)``).
  Do NOT split table rows across multiple lines.
• **Table tags**: Strip any ``[A>…<A]`` or ``[D>…<D]`` tags that wrap table
  cell content — table borders are NOT underlines.
• Do NOT include the metadata block itself in your output.
"""

# Directory containing state-specific clean prompts
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "data" / "Prompts"


# ---------------------------------------------------------------------------
# Structured page metadata extraction
# ---------------------------------------------------------------------------

def extract_page_metadata(pdf_path: str | Path) -> str:
    """Extract structural metadata from a PDF for LLM context.

    Returns a concise text block describing:
      • Tables found on each page (rows × cols, cell contents)
      • Page-1 header elements detected

    This metadata is prepended to the raw text sent to the LLM so it
    can make informed decisions about layout without seeing images.

    Parameters
    ----------
    pdf_path : str or Path
        Path to the source PDF.

    Returns
    -------
    str
        Human-readable metadata block, or empty string if extraction fails.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        return ""

    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        return ""

    sections: List[str] = []

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        page_num = page_idx + 1

        # ── Table detection ──────────────────────────────────────────
        try:
            tabs = page.find_tables()
            tables = tabs.tables if tabs else []
        except Exception:
            tables = []

        if tables:
            for t_idx, table in enumerate(tables):
                try:
                    data = table.extract()  # list of rows, each row is list of cell strings
                except Exception:
                    continue
                if not data:
                    continue

                rows_desc: List[str] = []
                for row in data:
                    # Join cells with tab-like spacing
                    cells = [str(c).strip() if c else "" for c in row]
                    rows_desc.append("  │ " + "  │  ".join(cells) + "  │")

                sections.append(
                    f"TABLE on page {page_num} ({len(data)} rows × {len(data[0]) if data else 0} cols):\n"
                    + "\n".join(rows_desc)
                )

    doc.close()

    if not sections:
        return ""

    metadata = (
        "═══ PDF STRUCTURAL METADATA (detected by PyMuPDF) ═══\n"
        + "\n\n".join(sections)
        + "\n═══ END METADATA ═══\n\n"
        "INSTRUCTION: Use the metadata above to understand the document structure.\n"
        "- TABLE rows must be rendered on a SINGLE line with large spacing between columns.\n"
        "  Example: \"Section 1          October 1, 2025          5-192f(d)\"\n"
        "- Do NOT split table rows across multiple lines.\n"
        "- Do NOT add [A>...<A] or [D>...<D] tags to table content — table borders\n"
        "  are NOT underlines. Strip any tags that appear on table cell content.\n"
        "- The raw text below may have table cells split across lines — merge them\n"
        "  back into single-line rows using the table structure above as reference.\n\n"
    )

    logger.info(
        "📋 Extracted page metadata: %d structural elements, %d chars",
        len(sections), len(metadata),
    )
    return metadata


def _load_state_clean_prompt(state_code: str | None) -> str:
    """
    Load a state-specific clean prompt from ``data/Prompts/{ST}_Clean_Prompt.txt``.

    Falls back to the universal ``_CLEAN_SYSTEM_PROMPT_FALLBACK`` if:
      - no state_code is provided, or
      - the state-specific file does not exist.

    Returns
    -------
    str
        The system prompt text.
    """
    if state_code:
        prompt_file = _PROMPTS_DIR / f"{state_code.upper()}_Clean_Prompt.txt"
        if prompt_file.exists():
            prompt_text = prompt_file.read_text(encoding="utf-8").strip()
            if prompt_text:
                logger.info(
                    "📋 Loaded state-specific clean prompt: %s (%d chars)",
                    prompt_file.name, len(prompt_text),
                )
                return prompt_text
        logger.info(
            "ℹ️  No clean prompt for state '%s' at %s — using fallback",
            state_code, prompt_file,
        )
    return _CLEAN_SYSTEM_PROMPT_FALLBACK


def _get_anthropic_client():
    """
    Obtain an Anthropic client via the TR AI Platform gateway.
    Returns an ``anthropic.Anthropic`` instance.
    """
    import requests as _requests
    import anthropic as _anthropic

    workspace_id = "AgentTraininJaH4"
    url = "https://aiplatform.gcs.int.thomsonreuters.com/v1/anthropic/token"
    payload = {"workspace_id": workspace_id}
    resp = _requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()
    creds = resp.json()

    if "anthropic_api_key" in creds:
        return _anthropic.Anthropic(api_key=creds["anthropic_api_key"])
    raise RuntimeError("No anthropic_api_key in gateway response")


# ---------------------------------------------------------------------------
# LLM diff tracking — what did the LLM change?
# ---------------------------------------------------------------------------

def _generate_clean_diff_report(
    raw_text: str,
    cleaned_text: str,
    *,
    stage: str = "LLM Clean",
) -> str:
    """Generate a human-readable report of what changed between raw and clean.

    Returns a structured text report with:
    - Summary statistics (chars/lines removed/added)
    - Lines REMOVED by the LLM (likely headers, footers, line numbers)
    - Lines ADDED by the LLM (should be rare — flags potential hallucinations)
    - Suspicious removals (legislative content words that shouldn't be removed)
    """
    import difflib

    raw_lines = raw_text.splitlines()
    clean_lines = cleaned_text.splitlines()

    # Use SequenceMatcher for line-level diff
    sm = difflib.SequenceMatcher(None, raw_lines, clean_lines)

    removed: list[tuple[int, str]] = []   # (line_no, text)
    added: list[tuple[int, str]] = []     # (line_no, text)

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "delete":
            for idx in range(i1, i2):
                removed.append((idx + 1, raw_lines[idx]))
        elif tag == "insert":
            for idx in range(j1, j2):
                added.append((idx + 1, clean_lines[idx]))
        elif tag == "replace":
            for idx in range(i1, i2):
                removed.append((idx + 1, raw_lines[idx]))
            for idx in range(j1, j2):
                added.append((idx + 1, clean_lines[idx]))

    # Filter out blank/whitespace-only lines for meaningful counts
    removed_nonblank = [(n, t) for n, t in removed if t.strip()]
    added_nonblank = [(n, t) for n, t in added if t.strip()]

    # Suspicious removals: lines containing legislative keywords
    _SUSPICIOUS_WORDS = re.compile(
        r"\b(?:General Assembly|Sec\.\s*\d|Section\s+\d|Be it enacted|"
        r"\[A>|\[D>|<A\]|<D\]|AN ACT|statute|subsection|"
        r"shall|amend|repeal|effective|enacted)\b",
        re.IGNORECASE,
    )
    suspicious = [
        (n, t) for n, t in removed_nonblank
        if _SUSPICIOUS_WORDS.search(t)
        and not re.match(r"^\s*---\s*Page\s+\d+\s*---\s*$", t)  # skip page markers
    ]

    # Build report
    lines: list[str] = []
    lines.append(f"{'=' * 70}")
    lines.append(f"  LLM CLEAN DIFF REPORT — {stage}")
    lines.append(f"{'=' * 70}")
    lines.append(f"")
    lines.append(f"  Raw input:     {len(raw_text):>8,} chars | {len(raw_lines):>5,} lines")
    lines.append(f"  Clean output:  {len(cleaned_text):>8,} chars | {len(clean_lines):>5,} lines")
    change_pct = ((len(raw_text) - len(cleaned_text)) / len(raw_text) * 100) if raw_text else 0
    lines.append(f"  Reduction:     {len(raw_text) - len(cleaned_text):>8,} chars ({change_pct:.1f}%)")
    lines.append(f"")
    lines.append(f"  Lines removed (non-blank): {len(removed_nonblank)}")
    lines.append(f"  Lines added   (non-blank): {len(added_nonblank)}")
    lines.append(f"")

    # ── Suspicious removals ──
    if suspicious:
        lines.append(f"{'─' * 70}")
        lines.append(f"  ⚠️  SUSPICIOUS REMOVALS ({len(suspicious)} lines with legislative content):")
        lines.append(f"{'─' * 70}")
        for n, t in suspicious[:30]:  # cap at 30
            lines.append(f"  LINE {n:>4d}: {t.rstrip()[:120]}")
        if len(suspicious) > 30:
            lines.append(f"  ... and {len(suspicious) - 30} more")
        lines.append(f"")

    # ── All removed lines ──
    lines.append(f"{'─' * 70}")
    lines.append(f"  REMOVED LINES ({len(removed_nonblank)} non-blank):")
    lines.append(f"{'─' * 70}")
    for n, t in removed_nonblank[:100]:
        lines.append(f"  LINE {n:>4d}: {t.rstrip()[:150]}")
    if len(removed_nonblank) > 100:
        lines.append(f"  ... and {len(removed_nonblank) - 100} more")
    lines.append(f"")

    # ── Added lines ──
    if added_nonblank:
        lines.append(f"{'─' * 70}")
        lines.append(f"  ADDED LINES ({len(added_nonblank)} non-blank):")
        lines.append(f"{'─' * 70}")
        for n, t in added_nonblank[:50]:
            lines.append(f"  LINE {n:>4d}: {t.rstrip()[:150]}")
        if len(added_nonblank) > 50:
            lines.append(f"  ... and {len(added_nonblank) - 50} more")
        lines.append(f"")

    lines.append(f"{'=' * 70}")
    lines.append(f"  END OF DIFF REPORT")
    lines.append(f"{'=' * 70}")

    report = "\n".join(lines)
    logger.info(
        "📋 Diff report: %d lines removed, %d added, %d suspicious",
        len(removed_nonblank), len(added_nonblank), len(suspicious),
    )
    return report


def clean_extracted_text(
    raw_text: str,
    *,
    state_code: str | None = None,
    pdf_path: str | Path | None = None,
    anthropic_client=None,
    model: str = "claude-opus-4-7",
    mark_brackets: bool = False,
) -> str:
    """
    Use Claude Opus 4.7 to remove PDF artefacts (headers, footers, line
    numbers, page markers) from PyMuPDF-extracted text while preserving
    all legislative content and ``[A>…<A]`` / ``[D>…<D]`` tags.

    Uses **state-specific prompts** from ``data/Prompts/{ST}_Clean_Prompt.txt``
    when available, otherwise falls back to a universal prompt.

    When *pdf_path* is provided, structural metadata (tables, header zones)
    is extracted from the PDF and prepended to the text sent to the LLM,
    giving it layout context it cannot infer from text alone.

    Parameters
    ----------
    raw_text : str
        The raw text produced by ``extract_pdf_with_pymupdf(join_pages=True)``.
    state_code : str, optional
        Two-letter state code (e.g. "CT", "FL", "KS").  Used to load
        the matching prompt file.
    pdf_path : str or Path, optional
        Path to the source PDF.  When provided, structural metadata
        (tables, header layout) is extracted and passed to the LLM.
    anthropic_client : anthropic.Anthropic, optional
        An existing Anthropic client.  If *None*, one is created via
        the TR AI Platform gateway.
    model : str
        Anthropic model name.  Default: ``claude-opus-4-7``.

    Returns
    -------
    str
        Cleaned text with artefacts removed.
    """
    if anthropic_client is None:
        anthropic_client = _get_anthropic_client()

    # Load the appropriate prompt
    system_prompt = _load_state_clean_prompt(state_code)

    # ── Extract structural metadata from PDF if available ─────────
    metadata_prefix = ""
    if pdf_path:
        metadata_prefix = extract_page_metadata(pdf_path)

    # ── Pre-clean raw text BEFORE sending to LLM ──────────────────
    # The raw _pymupdf.txt may contain:
    #   • Margin line numbers ("line 1", "line 2", …) for CA bills
    #   • Adjacent same-type tags split across line wraps:
    #       [A>SECTION 1.<A]\n line 2 [A>to read:<A]
    # The LLM is unreliable at merging these split tags — it sometimes
    # drops the second tag entirely (case observed: AB 502 page 3 lost
    # the `[A>to read:<A]` wrapper).  By pre-merging deterministically
    # with `_clean_tagged_text`, the LLM only sees:
    #       [A>SECTION 1. ... to read:<A]
    # which it just has to preserve verbatim.
    #
    # This is safe for ALL states — `_RE_LINE_NUMBER` only matches the
    # literal "line N" pattern (CA-specific) and is a no-op elsewhere.
    # The tag-merge regexes only collapse adjacent same-type tags
    # separated by a single newline (within-paragraph), preserving
    # paragraph breaks (\n\n).
    pre_cleaned = _clean_tagged_text(raw_text, strip_line_numbers=True)
    if pre_cleaned != raw_text:
        logger.info(
            "🧹 Pre-cleaned raw text before LLM: %d → %d chars (line numbers stripped, split tags merged)",
            len(raw_text), len(pre_cleaned),
        )

    # Build the user message: metadata (if any) + pre-cleaned text
    user_content = metadata_prefix + pre_cleaned

    logger.info(
        "🧹 Cleaning extracted text with %s (state=%s, metadata=%d chars) …",
        model, state_code or "universal", len(metadata_prefix),
    )

    import time as _time
    max_retries = 5
    for attempt in range(1, max_retries + 1):
        try:
            response = anthropic_client.messages.create(
                model=model,
                max_tokens=16384,
                system=system_prompt,
                messages=[
                    {"role": "user", "content": user_content},
                ],
            )
            break
        except Exception as _api_err:
            if "529" in str(_api_err) or "overloaded" in str(_api_err).lower():
                wait = min(2 ** attempt, 30)
                logger.warning(
                    "⏳ Anthropic overloaded (attempt %d/%d), retrying in %ds…",
                    attempt, max_retries, wait,
                )
                _time.sleep(wait)
                if attempt == max_retries:
                    raise
            else:
                raise

    cleaned = response.content[0].text.strip()

    # Expose usage data for callers that want cost tracking
    _usage = getattr(response, 'usage', None)
    clean_extracted_text._last_usage = {
        "input_tokens": getattr(_usage, 'input_tokens', 0) if _usage else 0,
        "output_tokens": getattr(_usage, 'output_tokens', 0) if _usage else 0,
        "model": model,
    }

    # ── Generate diff report BEFORE any post-processing ──────────────
    # This captures what the LLM itself changed.  Compare against the
    # pre-cleaned text (what the LLM actually saw), not the original raw
    # text — that way the report measures only the LLM's edits, not the
    # deterministic line-number/tag-merge cleanup.
    diff_report = _generate_clean_diff_report(pre_cleaned, cleaned, stage="LLM Clean")

    logger.info(
        "✅ Cleaned text: %d → %d → %d chars (raw → pre-cleaned → LLM)",
        len(raw_text), len(pre_cleaned), len(cleaned),
    )

    # ── bracket-to-[D>] tagging (runs AFTER LLM to avoid confusing it) ──
    if mark_brackets:
        cleaned = _tag_brackets(cleaned)

    # ── strip false header tags for CT ────────────────────────────────
    if state_code and state_code.upper() == "CT":
        cleaned = _strip_ct_header_tags(cleaned)
        cleaned = _strip_ct_compact_designator(cleaned)
        cleaned = _strip_false_subsection_d_tags(cleaned)
        cleaned = _restructure_ct_header(cleaned)

    # ── strip IN-specific artifacts (footers, barcodes, false headers) ─
    if state_code and state_code.upper() == "IN":
        cleaned = _strip_in_header_tags(cleaned)
        cleaned = _strip_in_artifacts(cleaned)

    # ── strip KS-specific false header tags ────────────────────────
    if state_code and state_code.upper() == "KS":
        cleaned = _strip_ks_header_tags(cleaned)

    # ── consolidate, clean, and split tagged blocks ───────────────────
    cleaned = consolidate_adjacent_tags(cleaned)
    cleaned = remove_nested_tags(cleaned)
    cleaned = merge_split_tags(cleaned)
    cleaned = split_tagged_subsections(cleaned)
    # Restore paragraph breaks at renumber-only [D>(N)<D] [A>(M)<A] pairs
    # that the LLM tends to collapse onto a single line.  See the docstring
    # of `split_renumber_paragraph_breaks` for the detection rules.
    cleaned = split_renumber_paragraph_breaks(cleaned)

    # Store the diff report for the caller to retrieve and save
    clean_extracted_text._last_diff_report = diff_report

    return cleaned


def consolidate_adjacent_tags(text: str) -> str:
    """
    Merge adjacent ``[A>…<A]`` (and ``[D>…<D]``) tags that are separated
    only by whitespace.  This handles the common case where PyMuPDF creates
    one tag per physical line even though the underline/strikethrough is
    continuous across the line break.

    Example::

        [A>three utility commissioners,<A] [A>each of whom shall be…<A]
        →  [A>three utility commissioners, each of whom shall be…<A]

    Repeats until no more merges remain (handles 3+ consecutive tags).

    Uses a two-phase approach:

    **Phase 1** — same-line merging (space-separated, no newlines): always
    merge.  Same-line splits only happen from decoration changes within a
    PDF line, never from subsection boundaries.

    **Phase 2** — cross-line merging (newline-separated): merge UNLESS the
    next tag starts with a sub-item or section marker:

    - period-style: ``a. ``, ``1. ``, ``A. ``
    - parenthesized: ``(a) ``, ``(1) ``, ``(iv) ``
    - section header: ``Sec.``, ``SECTION``
    - statute number: ``72-3404.`` (digit-dash-digit pattern)
    """
    import re as _re
    # All markers (for cross-line protection)
    # Sec\. has no trailing \s so it also blocks [A>Sec.<A] (end-of-tag)
    # \d+-\d+ matches statute numbers like "72-3404." that follow "read as follows:"
    _NO_MERGE = r'(?![a-cA-C1-9]\.\s|\([a-zA-Z0-9]{1,4}\)\s|Sec\.|SECTION\s|\d+-\d)'
    # Strong section starters only (for same-line protection)
    _NO_MERGE_INLINE = r'(?!Sec\.|SECTION\s)'

    # ── Phase 0: fix orphaned "Sec." tags ─────────────────────────────
    # PyMuPDF sometimes splits "Sec." into its own tag ([A>Sec.<A])
    # followed by the section number in the next tag ([A>1. As used…<A]).
    # Re-join them BEFORE merging so Phase 2 sees the complete section.
    # Also handles "Sec.\n8.7." where PyMuPDF put a newline inside the tag.
    _orphan_sec_a = _re.compile(r'Sec\.\s*<A\]\s*\n?\s*\[A>')
    text = _orphan_sec_a.sub('Sec. ', text)
    _orphan_sec_d = _re.compile(r'Sec\.\s*<D\]\s*\n?\s*\[D>')
    text = _orphan_sec_d.sub('Sec. ', text)
    # Also fix "Sec.\nNUMBER" within a single tag (newline inside content)
    text = _re.sub(r'Sec\.\s*\n\s*(\d)', r'Sec. \1', text)

    # ── Phase 0b: rejoin "Sec. X.Y." with following "(a)" subsection ──
    # When PyMuPDF splits:  [A>Sec. 8.7.<A]  [A>(a) As used…<A]
    # The "(a)" is the first subsection label of that section and must
    # stay with "Sec. 8.7." — it is NOT a standalone subsection break.
    _orphan_sec_num_a = _re.compile(r'(Sec\.\s+[\d.]+)<A\]\s*\n?\s*\[A>')
    text = _orphan_sec_num_a.sub(r'\1 ', text)
    _orphan_sec_num_d = _re.compile(r'(Sec\.\s+[\d.]+)<D\]\s*\n?\s*\[D>')
    text = _orphan_sec_num_d.sub(r'\1 ', text)

    # ── Phase 1a: same-line merging — merge unless Sec./SECTION ───────
    # Parenthesized refs like (5) on the same line are inline, not
    # subsection markers, so they should still merge.
    _adj_a_inline = _re.compile(r'<A\][ \t]+\[A>' + _NO_MERGE_INLINE)
    while _adj_a_inline.search(text):
        text = _adj_a_inline.sub(' ', text)
    _adj_d_inline = _re.compile(r'<D\][ \t]+\[D>' + _NO_MERGE_INLINE)
    while _adj_d_inline.search(text):
        text = _adj_d_inline.sub(' ', text)

    # ── Phase 1b: same-line split — break Sec./SECTION onto new lines ─
    # When PyMuPDF places e.g. "Chapter...<A] [A>Sec. 1..." on the
    # same line, split them with a blank line instead of merging.
    _split_a = _re.compile(r'(<A\])[ \t]+(\[A>(?:Sec\.|SECTION\s))')
    text = _split_a.sub(r'\1\n\n\2', text)
    _split_d = _re.compile(r'(<D\])[ \t]+(\[D>(?:Sec\.|SECTION\s))')
    text = _split_d.sub(r'\1\n\n\2', text)

    # ── Phase 2: cross-line merging (has newline), respecting markers ─
    # IMPORTANT: pattern is [ \t]*\n[ \t]*  (exactly ONE newline) — NOT
    # \s*\n\s* which would also match paragraph breaks (\n\n) and weld
    # together two adjacent digest paragraphs (e.g. the italic LCD digest
    # in California bills where every paragraph starts with "Existing law…"
    # or "This bill would…" — neither matches _NO_MERGE, so they would all
    # collapse into one giant [A>…<A] block without this guard).
    _adj_a_cross = _re.compile(r'<A\][ \t]*\n[ \t]*\[A>' + _NO_MERGE)
    while _adj_a_cross.search(text):
        text = _adj_a_cross.sub(' ', text)
    _adj_d_cross = _re.compile(r'<D\][ \t]*\n[ \t]*\[D>' + _NO_MERGE)
    while _adj_d_cross.search(text):
        text = _adj_d_cross.sub(' ', text)

    # ── Phase 3: ensure blank line before subsection boundaries ───────
    # After merging, adjacent tags separated by a single \n that start
    # with a marker should have a blank line (\n\n) for readability.
    _MARKER = r'(\([a-zA-Z0-9]{1,4}\)\s|[a-cA-C1-9]\.\s|Sec\.\s|SECTION\s|\d+-\d)'
    _gap_a = _re.compile(r'(<A\])\n(\[A>' + _MARKER + r')')
    text = _gap_a.sub(r'\1\n\n\2', text)
    _gap_d = _re.compile(r'(<D\])\n(\[D>' + _MARKER + r')')
    text = _gap_d.sub(r'\1\n\n\2', text)

    return text


def remove_nested_tags(text: str) -> str:
    """Remove nested/duplicate opening tags within the same tag type.

    Example::

        [A>text one[A>text two<A]  →  [A>text one text two<A]
    """
    for tag_type in ['A', 'D']:
        open_tag = '[' + tag_type + '>'
        close_tag = '<' + tag_type + ']'
        changed = True
        while changed:
            changed = False
            result = []
            depth = 0
            i = 0
            while i < len(text):
                if text[i:i+len(open_tag)] == open_tag:
                    if depth == 0:
                        result.append(open_tag)
                        depth = 1
                    else:
                        changed = True
                    i += len(open_tag)
                elif text[i:i+len(close_tag)] == close_tag:
                    result.append(close_tag)
                    depth = max(0, depth - 1)
                    i += len(close_tag)
                else:
                    result.append(text[i])
                    i += 1
            text = ''.join(result)
    return text


def merge_split_tags(text: str) -> str:
    """Merge adjacent same-type tags split mid-sentence.

    Merges ``[A>text<A] [A>continuation<A]`` into ``[A>text continuation<A]``
    ONLY when the second tag does **not** start with a subsection marker.
    """
    for tag_type in ['A', 'D']:
        _no_sub = (
            r'(?!\s*\(\d+\))'
            r'(?!\s*\([A-Z]+\))'
            r'(?!\s*\([a-z]+\))'
            r'(?!\s*[a-z]\.\d*\.?\s)'
            r'(?!\s*\d+\.\s)'
        )
        # 0) Inline joins: <T][T>text on same line — always merge
        inline_cont = r'<' + tag_type + r'\]\[' + tag_type + r'>' + _no_sub
        text = re.sub(inline_cont, '', text)
        # 0b) Inline subsection boundary: <T][T>(1) → <T]\n\n[T>(1)
        inline_sub = r'<' + tag_type + r'\]\[' + tag_type + r'>'
        text = re.sub(inline_sub, '<' + tag_type + ']\n\n[' + tag_type + '>', text)
        # 1) Continuations: \n<T][T>text (NOT a subsection) → merge
        same_cont = r'\n<' + tag_type + r'\]\[' + tag_type + r'>' + _no_sub
        text = re.sub(same_cont, ' ', text)
        # 2) Subsection boundaries: \n<T][T>(1) → <T]\n\n[T>(1)
        same_sub = r'\n<' + tag_type + r'\]\[' + tag_type + r'>'
        text = re.sub(same_sub, '<' + tag_type + ']\n\n[' + tag_type + '>', text)
        # 3) Cross-line joins: <T]\n[T> continuation
        # IMPORTANT: gap is [ \t]*\n[ \t]* (exactly ONE newline) — NOT
        # \s*\n\s* which would also swallow paragraph breaks (\n\n) and
        # merge two distinct same-type paragraphs into one block.
        cross_line = (
            r'<' + tag_type + r'\]'
            r'([ \t]*\n[ \t]*)'
            r'\[' + tag_type + r'>' + _no_sub
        )
        text = re.sub(cross_line, ' ', text)
    return text


def split_tagged_subsections(text: str) -> str:
    """Split long tagged blocks at subsection markers.

    Only splits at markers after sentence-ending punctuation (``.``, ``;``,
    ``:``) or after ``and``/``or``, avoiding false splits on inline
    references like "seventy-two (72) hour".

    Inline-list blocks (items separated by ``;`` rather than ``.``) are
    kept intact.
    """
    subsection_re = re.compile(
        r'(?:(?<=[.:;])|(?<=\band\b)|(?<=\bor\b))\s*'
        r'('
        r'\(\d+\)[a-z]\.\d*\.?'
        r'|\(\d+\)|\([A-Z]+\)|\([a-z]+\)'
        r'|[a-z]\.\d*\.?'
        r'|\d+\.'
        r')(?:\s|$)',
        re.IGNORECASE,
    )
    _nosplit_re = re.compile(
        r'(?:^|\n)\s*(?:Sec|Section|Art|Article)\.\s*$'
        r'|(?<!\$)(?<!\d)(?:\d{2,}\.\d*\.?\s*)$',
        re.IGNORECASE,
    )

    def _is_inline_list(content, markers):
        if not markers:
            return False
        list_sep_count = 0
        period_count = 0
        for m in markers:
            preceding = content[:m.start()].rstrip()
            if preceding.endswith(';'):
                list_sep_count += 1
            elif preceding.endswith(('.', ':')):
                period_count += 1
            else:
                list_sep_count += 1
        return list_sep_count > period_count

    def _split_block(match):
        tag_open = match.group(1)
        content = match.group(2)
        tag_close = match.group(3)
        markers = list(subsection_re.finditer(content))
        if len(markers) < 1:
            return match.group(0)
        markers = [m for m in markers
                   if not _nosplit_re.search(content[:m.start()])]
        if len(markers) < 1:
            return match.group(0)
        if _is_inline_list(content, markers):
            return match.group(0)
        pieces = []
        prev_end = 0
        for m in markers:
            split_at = m.start()
            before = content[prev_end:split_at].rstrip()
            if before:
                pieces.append(before)
            prev_end = split_at
        last = content[prev_end:].strip()
        if last:
            pieces.append(last)
        tagged_pieces = [f"{tag_open}{p.strip()}{tag_close}" for p in pieces]
        return '\n\n'.join(tagged_pieces)

    text = re.sub(r'(\[A>)(.*?)(<A\])', _split_block, text, flags=re.DOTALL)
    text = re.sub(r'(\[D>)(.*?)(<D\])', _split_block, text, flags=re.DOTALL)
    return text


def split_renumber_paragraph_breaks(text: str) -> str:
    """
    Restore paragraph breaks at renumber-only deletion/addition pairs.

    California legislative bills often have **paragraph numbering changes**
    where the original paragraph marker is shown as deleted (strikethrough)
    on its own line, followed by the new paragraph marker as an addition
    (italic) at the start of the renumbered paragraph::

        [D>(7)<D]                           ← deleted marker, alone on line
                                            ← (blank line in PDF layout)
        [A>(8)<A] "Malice" means …          ← new paragraph

    Or for **section renumbering** (when entire SEC. blocks are inserted
    or removed, shifting subsequent SEC. numbers)::

        [D>SEC. 5.<D]                       ← deleted SEC. number
        [A>SEC. 8.<A] Section 7334 of the Business and Professions Code …

    The pre-clean step preserves this layout (``\\n\\n`` between the two
    tags), but the LLM cleanup pass tends to **collapse** the gap into a
    single space::

        [D>(7)<D] [A>(8)<A] "Malice" means …
        [D>SEC. 5.<D] [A>SEC. 8.<A] Section 7334 of …

    This function detects that pattern and **restores the paragraph
    break** (``\\n\\n``) between the markers, so the output mirrors the
    PDF's visual layout — exactly what the user sees on the printed page.

    Detection criteria (ALL must hold):

    1. A deletion tag containing **only** a recognised marker:
       - parenthesised: ``(7)``, ``(ii)``, ``(B)``
       - section header: ``SEC. 5.``, ``SECTION 1.`` (with trailing period)
    2. Followed by spaces/tabs only (no newline) — i.e. the LLM has
       already collapsed the original ``\\n\\n``.
    3. Followed by an addition tag containing **only** a recognised marker.
    4. Followed by at least one non-whitespace character (paragraph
       content must follow).

    Inline phrase replacements are **not** affected because their tags
    contain full phrases (not just markers), e.g.::

        Fiscal committee: [D>yes <D][A>no<A].
        ↑ tag contents "yes "/"no" — no match

        paragraph[D> (2) or (3).<D] [A>(2).<A]
        ↑ deletion has trailing text "or (3)." after marker — no match

        Introduced by Assembly[D> Member Pellerin<D] [A>Members …<A]
        ↑ deletion is a multi-word phrase — no match
    """
    # Marker patterns (alternation):
    #   • Parenthesised ID: (1)  (10)  (a)  (B)  (i)  (vii)  (iii)
    #   • Section header:   SEC. 5.   SEC. 10.   SECTION 1.
    marker_paren   = r"\(\s*[a-zA-Z0-9]{1,6}\s*\)"
    marker_section = r"(?:SEC\.|SECTION)\s+\d{1,4}\.?"
    marker = r"(?:" + marker_paren + r"|" + marker_section + r")"

    pattern = re.compile(
        r"(\[D>\s*" + marker + r"\s*<D\])"   # \1: [D>(marker)<D]
        r"[ \t]+"                             # spaces/tabs only — no newline
        r"(\[A>\s*" + marker + r"\s*<A\])"   # \2: [A>(marker)<A]
        r"[ \t]+"                             # whitespace before content
        r"(?=\S)"                             # at least one non-ws char follows
    )

    return pattern.sub(r"\1\n\n\2 ", text)


def extract_pdf_summary(pdf_path: str | Path) -> dict:
    """
    Return metadata + per-page statistics (useful for diagnostics).
    """
    pdf_path = Path(pdf_path)
    doc = fitz.open(str(pdf_path))
    summary = {
        "filename": pdf_path.name,
        "total_pages": len(doc),
        "pages": [],
    }

    for idx in range(len(doc)):
        page = doc[idx]
        spans = _extract_page_spans(page)
        ul_count = sum(1 for s in spans if s.underline)
        so_count = sum(1 for s in spans if s.strikethrough)
        plain    = len(spans) - ul_count - so_count
        sample   = " ".join(s.text for s in spans[:5])[:200]

        summary["pages"].append({
            "page": idx + 1,
            "total_spans": len(spans),
            "underline_spans": ul_count,
            "strikethrough_spans": so_count,
            "plain_spans": plain,
            "sample_text": sample,
        })

    doc.close()
    return summary


def extract_pdf_json_report(
    pdf_path: str | Path,
    *,
    pages: Optional[List[int]] = None,
    mark_underline: bool = True,
    mark_strikethrough: bool = True,
    mark_italic: bool = False,
    mark_bold: bool = False,
    mark_brackets: bool = False,
    strip_line_numbers: bool = False,
) -> Dict[str, Any]:
    """
    Generate a JSON-serialisable report for a PDF.

    Uses **character-level** decoration detection (same as the tagged-text
    extractor) so that tag boundaries land exactly where the underline or
    strikethrough drawing starts/ends — not at PyMuPDF span boundaries.

    Returns a flat list of formatted spans, each with:
      * ``text``           – the decorated text
      * ``format_types``   – list of detected formats (underline, strikethrough…)
      * ``page``           – 1-based page number
      * ``bill_line``      – the bill's own printed margin line number (or null
                             if the span is in a header/footer area without one)
      * ``context``        – surrounding text on the same line for easy lookup

    A top-level ``summary`` section aggregates counts.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    doc = fitz.open(str(pdf_path))
    page_indices = pages if pages is not None else range(len(doc))

    CONTEXT_CHARS = 50   # chars of surrounding text on each side
    MARGIN_X_MAX  = 120  # margin numbers are at x0 < this threshold
    Y_MATCH_TOL   = 5.0  # max y-distance (pts) to pair a margin number

    report: Dict[str, Any] = {
        "filename": pdf_path.name,
        "total_pages": len(doc),
        "formatted_spans": [],
        "summary": {
            "total_formatted_spans": 0,
            "underline_spans": 0,
            "strikethrough_spans": 0,
            "italic_spans": 0,
            "bold_spans": 0,
        },
    }

    _re_margin_num = re.compile(r"^\d{1,4}$")

    for idx in page_indices:
        if idx < 0 or idx >= len(doc):
            continue
        page = doc[idx]
        page_num = idx + 1

        # --- decoration sources ---
        try:
            ul_rects, so_rects = _extract_annot_rects(page)
        except Exception:
            ul_rects, so_rects = [], []
        hlines = _extract_hlines_from_drawings(page)

        # --- filter out table-border lines ---
        table_zones = _detect_table_zones(page)
        if table_zones:
            hlines = _filter_table_border_hlines(hlines, table_zones)

        # --- text with per-character positions (rawdict → dict fallback) ---
        text_dict = page.get_text("rawdict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
        total_chars = sum(
            len(s.get("chars", []))
            for b in text_dict.get("blocks", []) if b.get("type") == 0
            for l in b.get("lines", [])
            for s in l.get("spans", [])
        )
        if total_chars == 0:
            text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)

        # -- Pass 1: collect margin line numbers --
        margin_nums: List[Tuple[float, int]] = []
        line_infos: List[Tuple[float, dict]] = []  # (y_mid, line_dict)

        for block in text_dict.get("blocks", []):
            if block.get("type") != 0:
                continue
            for tl in block.get("lines", []):
                lbbox = tl.get("bbox", (0, 0, 0, 0))
                y_mid = (lbbox[1] + lbbox[3]) / 2
                spans = tl.get("spans", [])

                # Build full-line text from chars (rawdict) or span text (dict)
                line_text_chars = ""
                for sp in spans:
                    cdata = sp.get("chars", [])
                    if cdata:
                        line_text_chars += "".join(ch.get("c", "") for ch in cdata)
                    else:
                        line_text_chars += sp.get("text", "")
                stripped = line_text_chars.strip()

                if _re_margin_num.match(stripped) and lbbox[0] < MARGIN_X_MAX:
                    margin_nums.append((y_mid, int(stripped)))
                    continue

                line_infos.append((y_mid, tl))

        margin_nums.sort(key=lambda m: m[0])

        def _find_bill_line(y: float) -> Optional[int]:
            if not margin_nums:
                return None
            best_num = None
            best_dy  = float("inf")
            for my, mn in margin_nums:
                dy = abs(my - y)
                if dy < best_dy:
                    best_dy = dy
                    best_num = mn
            return best_num if best_dy <= Y_MATCH_TOL else None

        # -- Pass 2: character-level classification per line --
        for line_y, tl in line_infos:
            spans = tl.get("spans", [])

            # Build list of (char, format_set, char_bbox) for the entire line
            char_entries: List[Tuple[str, frozenset, Tuple[float, float, float, float]]] = []

            for sp in spans:
                chars_data = sp.get("chars", [])
                font_name  = sp.get("font", "")
                font_size  = sp.get("size", 12)
                sp_flags   = sp.get("flags", 0)
                sp_italic  = bool(sp_flags & 2)
                sp_bold    = bool(sp_flags & 16)

                if not chars_data:
                    # dict-mode fallback: classify entire span as one unit
                    s_bbox = tuple(sp.get("bbox", (0, 0, 0, 0)))
                    s_ul, s_so = _classify_char_decoration(
                        s_bbox, font_size, ul_rects, so_rects, hlines, font_name,
                    )
                    fmt: set = set()
                    if s_ul and mark_underline:       fmt.add("underline")
                    if s_so and mark_strikethrough:   fmt.add("strikethrough")
                    if sp_italic and mark_italic:     fmt.add("italic")
                    if sp_bold and mark_bold:         fmt.add("bold")
                    fs = frozenset(fmt)
                    for c in sp.get("text", ""):
                        char_entries.append((c, fs, s_bbox))
                    continue

                # rawdict: per-character classification
                for ch_info in chars_data:
                    c = ch_info.get("c", "")
                    if not c:
                        continue
                    bbox = tuple(ch_info.get("bbox", (0, 0, 0, 0)))
                    cw = bbox[2] - bbox[0]
                    if cw < 0.5:
                        is_ul, is_so = False, False
                    else:
                        is_ul, is_so = _classify_char_decoration(
                            bbox, font_size, ul_rects, so_rects,
                            hlines, font_name,
                        )
                    fmt = set()
                    if is_ul and mark_underline:       fmt.add("underline")
                    if is_so and mark_strikethrough:   fmt.add("strikethrough")
                    if sp_italic and mark_italic:      fmt.add("italic")
                    if sp_bold and mark_bold:          fmt.add("bold")
                    char_entries.append((c, frozenset(fmt), bbox))

            if not char_entries:
                continue

            # -- Group consecutive chars by decoration state into runs --
            runs: List[Tuple[str, frozenset, Tuple[float, float, float, float]]] = []
            cur_chars = [char_entries[0][0]]
            cur_fmt   = char_entries[0][1]
            cur_bboxes = [char_entries[0][2]]
            for c, fmt, cb in char_entries[1:]:
                if fmt == cur_fmt:
                    cur_chars.append(c)
                    cur_bboxes.append(cb)
                else:
                    run_bbox = (
                        min(b[0] for b in cur_bboxes),
                        min(b[1] for b in cur_bboxes),
                        max(b[2] for b in cur_bboxes),
                        max(b[3] for b in cur_bboxes),
                    )
                    runs.append(("".join(cur_chars), cur_fmt, run_bbox))
                    cur_chars = [c]
                    cur_fmt = fmt
                    cur_bboxes = [cb]
            run_bbox = (
                min(b[0] for b in cur_bboxes),
                min(b[1] for b in cur_bboxes),
                max(b[2] for b in cur_bboxes),
                max(b[3] for b in cur_bboxes),
            )
            runs.append(("".join(cur_chars), cur_fmt, run_bbox))

            # Full line text for context building
            full_line_text = "".join(c for c, _, _ in char_entries)
            bill_line = _find_bill_line(line_y)

            # -- Emit entries for each decorated run --
            char_pos = 0
            for run_text, run_fmt, run_bbox in runs:
                run_start = char_pos
                char_pos += len(run_text)

                if not run_fmt:
                    continue
                # Keep the raw text including spaces (matches the text
                # extractor output), but skip runs that are *only* whitespace.
                if not run_text.strip():
                    continue

                format_types = sorted(run_fmt)

                # Context snippet (use stripped version for readability)
                txt_display = run_text.strip()
                before_start = max(0, run_start - CONTEXT_CHARS)
                after_end = min(len(full_line_text), char_pos + CONTEXT_CHARS)
                before = full_line_text[before_start:run_start].strip()
                after  = full_line_text[char_pos:after_end].strip()
                context = ""
                if before:
                    context += f"…{before} "
                context += f">>>{txt_display}<<<"
                if after:
                    context += f" {after}…"

                span_entry = {
                    "text": run_text,
                    "format_types": format_types,
                    "page": page_num,
                    "bill_line": bill_line,
                    "bbox": list(run_bbox),
                    "context": context,
                }
                report["formatted_spans"].append(span_entry)

                if "underline" in format_types:
                    report["summary"]["underline_spans"] += 1
                if "strikethrough" in format_types:
                    report["summary"]["strikethrough_spans"] += 1
                if "italic" in format_types:
                    report["summary"]["italic_spans"] += 1
                if "bold" in format_types:
                    report["summary"]["bold_spans"] += 1

    report["summary"]["total_formatted_spans"] = len(report["formatted_spans"])

    # -- Bracket-enclosed deletions (text convention, not visual formatting) --
    if mark_brackets:
        report["summary"]["bracket_spans"] = 0
        for idx in page_indices:
            if idx < 0 or idx >= len(doc):
                continue
            page = doc[idx]
            page_num = idx + 1
            # Get the full plain text for this page
            full_page_text = page.get_text("text")
            for bm in _RE_BRACKET_DEL.finditer(full_page_text):
                inner = bm.group(1)
                if not inner.strip():
                    continue
                # Build context snippet
                start, end = bm.start(), bm.end()
                before = full_page_text[max(0, start - 50):start].strip()
                after  = full_page_text[end:min(len(full_page_text), end + 50)].strip()
                ctx = ""
                if before:
                    ctx += f"…{before} "
                ctx += f">>>[{inner}]<<<"
                if after:
                    ctx += f" {after}…"
                span_entry = {
                    "text": f"[{inner}]",
                    "format_types": ["brackets"],
                    "page": page_num,
                    "bill_line": None,
                    "bbox": [0, 0, 0, 0],
                    "context": ctx,
                }
                report["formatted_spans"].append(span_entry)
                report["summary"]["bracket_spans"] += 1
        # Recount total
        report["summary"]["total_formatted_spans"] = len(report["formatted_spans"])

    doc.close()
    return report


# ---------------------------------------------------------------------------
# Highlight / annotated-image generation
# ---------------------------------------------------------------------------

# Colour map: format_type → RGBA fill colour (semi-transparent)
_HIGHLIGHT_COLOURS = {
    "underline":     (0, 180, 0, 60),      # green
    "strikethrough": (220, 40, 40, 60),     # red
    "italic":        (0, 100, 220, 60),     # blue
    "bold":          (200, 140, 0, 60),     # orange
}
# Border colour (fully opaque) for the rectangle outline
_BORDER_COLOURS = {
    "underline":     (0, 180, 0),
    "strikethrough": (220, 40, 40),
    "italic":        (0, 100, 220),
    "bold":          (200, 140, 0),
}


def highlight_pdf_formatting(
    pdf_path: str | Path,
    report: Dict[str, Any] | None = None,
    *,
    output_dir: str | Path | None = None,
    dpi: int = 200,
    mark_underline: bool = True,
    mark_strikethrough: bool = True,
    mark_italic: bool = False,
    mark_bold: bool = False,
    mark_brackets: bool = False,
    strip_line_numbers: bool = False,
) -> List[Path]:
    """
    Render each PDF page as an image and draw coloured highlight
    rectangles over every detected formatted span.

    If *report* is provided it is used directly; otherwise one is
    generated on the fly via ``extract_pdf_json_report()``.

    Colours:
      • **Green**  — underline  (additions)
      • **Red**    — strikethrough (deletions)
      • **Blue**   — italic
      • **Orange** — bold

    Parameters
    ----------
    pdf_path : str or Path
        Path to the source PDF.
    report : dict, optional
        Pre-computed JSON report from ``extract_pdf_json_report()``.
    output_dir : str or Path, optional
        Directory for output images. Defaults to ``<pdf_stem>_highlighted/``.
    dpi : int
        Render resolution (default 200 — good balance of quality & size).
    mark_underline, mark_strikethrough, mark_italic, mark_bold : bool
        Which format types to highlight (must match the report).
    strip_line_numbers : bool
        Passed through if a report needs to be generated.

    Returns
    -------
    list[Path]
        Paths to the generated PNG images (one per page).
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        raise ImportError("Pillow is required for highlighting.  pip install Pillow")

    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    # Generate report if not provided
    if report is None:
        report = extract_pdf_json_report(
            pdf_path,
            mark_underline=mark_underline,
            mark_strikethrough=mark_strikethrough,
            mark_italic=mark_italic,
            mark_bold=mark_bold,
            strip_line_numbers=strip_line_numbers,
        )

    # Organise spans by page number
    spans_by_page: Dict[int, list] = {}
    for sp in report.get("formatted_spans", []):
        pg = sp.get("page", 0)
        spans_by_page.setdefault(pg, []).append(sp)

    # Setup output directory
    if output_dir is None:
        output_dir = pdf_path.parent / f"{pdf_path.stem}_highlighted"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    scale = dpi / 72.0  # PyMuPDF coords are 72 DPI
    doc = fitz.open(str(pdf_path))
    output_files: List[Path] = []

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        page_num = page_idx + 1

        # Render page to PIL image
        pix = page.get_pixmap(dpi=dpi)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

        # Create a transparent overlay for semi-transparent fills
        overlay = Image.new("RGBA", img.size, (255, 255, 255, 0))
        draw_overlay = ImageDraw.Draw(overlay)
        draw_border  = ImageDraw.Draw(img)

        page_spans = spans_by_page.get(page_num, [])
        for sp in page_spans:
            bbox = sp.get("bbox")
            if not bbox or len(bbox) < 4:
                continue

            # Scale PDF coords → pixel coords
            x0 = bbox[0] * scale
            y0 = bbox[1] * scale
            x1 = bbox[2] * scale
            y1 = bbox[3] * scale

            # Determine colour from the highest-priority format type
            fmt_types = sp.get("format_types", [])
            # Priority: strikethrough > underline > italic > bold
            colour_key = None
            for prio in ("strikethrough", "underline", "italic", "bold"):
                if prio in fmt_types:
                    colour_key = prio
                    break
            if colour_key is None:
                continue

            fill_rgba = _HIGHLIGHT_COLOURS.get(colour_key, (200, 200, 0, 60))
            border_rgb = _BORDER_COLOURS.get(colour_key, (200, 200, 0))

            # Draw semi-transparent fill on overlay
            draw_overlay.rectangle([x0, y0, x1, y1], fill=fill_rgba)
            # Draw 1px border on main image
            draw_border.rectangle([x0, y0, x1, y1], outline=border_rgb, width=1)

        # Composite overlay onto image
        img = img.convert("RGBA")
        img = Image.alpha_composite(img, overlay)
        img = img.convert("RGB")

        out_path = output_dir / f"page_{page_num:03d}.png"
        img.save(str(out_path), "PNG", optimize=True)
        output_files.append(out_path)
        logger.info("Highlighted page %d → %s (%d spans)", page_num, out_path.name, len(page_spans))

    doc.close()

    # Also create a legend image
    _create_legend(output_dir, mark_underline, mark_strikethrough, mark_italic, mark_bold)

    logger.info("Highlighted %d pages → %s", len(output_files), output_dir)
    return output_files


def _create_legend(
    output_dir: Path,
    mark_underline: bool,
    mark_strikethrough: bool,
    mark_italic: bool,
    mark_bold: bool,
) -> None:
    """Create a small legend image showing the colour key."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return

    entries = []
    if mark_strikethrough: entries.append(("Strikethrough [D>...<D]", _BORDER_COLOURS["strikethrough"]))
    if mark_underline:     entries.append(("Underline [A>...<A]", _BORDER_COLOURS["underline"]))
    if mark_italic:        entries.append(("Italic [A>...<A]", _BORDER_COLOURS["italic"]))
    if mark_bold:          entries.append(("Bold [A>...<A]", _BORDER_COLOURS["bold"]))

    if not entries:
        return

    w, h = 320, 30 + 28 * len(entries)
    img = Image.new("RGB", (w, h), "white")
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype("arial.ttf", 16)
    except Exception:
        font = ImageFont.load_default()

    draw.text((10, 5), "LEGEND", fill="black", font=font)
    y = 30
    for label, colour in entries:
        draw.rectangle([10, y, 30, y + 18], fill=colour, outline=colour)
        draw.text((38, y), label, fill="black", font=font)
        y += 28

    img.save(str(output_dir / "legend.png"), "PNG")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract PDF text with underline/strikethrough tags (PyMuPDF)"
    )
    parser.add_argument("pdf", help="Path to the input PDF file")
    parser.add_argument(
        "-o", "--output", default=None,
        help="Write output to this file (default: stdout)"
    )
    parser.add_argument(
        "--page", type=int, default=None,
        help="Extract only this page (1-based)"
    )
    parser.add_argument(
        "--no-underline", action="store_true",
        help="Do NOT mark underlined text"
    )
    parser.add_argument(
        "--no-strikethrough", action="store_true",
        help="Do NOT mark strikethrough text"
    )
    parser.add_argument(
        "--mark-italic", action="store_true",
        help="Wrap italic text with [A>…<A] (e.g. California new-law text)"
    )
    parser.add_argument(
        "--mark-bold", action="store_true",
        help="Wrap bold text with [A>…<A]"
    )
    parser.add_argument(
        "--no-line-numbers", action="store_true",
        help="Strip margin line numbers (e.g. 'line 1', 'line 23')"
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output a JSON report with text, formatted text, bbox, and page numbers"
    )
    parser.add_argument(
        "--highlight", action="store_true",
        help="Generate annotated page images with coloured highlights on formatted text"
    )
    parser.add_argument(
        "--summary", action="store_true",
        help="Print per-page span statistics instead of text"
    )
    parser.add_argument(
        "--clean", action="store_true",
        help="Use an LLM to remove headers, footers, line numbers, "
             "and page markers (universal — works for any PDF layout)"
    )
    parser.add_argument(
        "--clean-model", default=None,
        help="Override the LLM model used for --clean (default: claude-opus-4-6)"
    )
    parser.add_argument(
        "--state", default=None,
        help="Two-letter state code (e.g. CT, FL, KS) for state-specific clean prompt"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s  %(message)s",
    )

    pdf_path = Path(args.pdf)

    if args.summary:
        info = extract_pdf_summary(pdf_path)
        print(f"File: {info['filename']}  ({info['total_pages']} pages)\n")
        for pg in info["pages"]:
            print(
                f"  Page {pg['page']:>3d}:  {pg['total_spans']:>4d} spans  "
                f"({pg['underline_spans']} underline, "
                f"{pg['strikethrough_spans']} strikethrough, "
                f"{pg['plain_spans']} plain)"
            )
            if pg["sample_text"]:
                print(f"           Sample: {pg['sample_text'][:120]}")
        return

    pages = [args.page - 1] if args.page else None

    # Shared flags for all extraction modes
    _flags = dict(
        mark_underline=not args.no_underline,
        mark_strikethrough=not args.no_strikethrough,
        mark_italic=args.mark_italic,
        mark_bold=args.mark_bold,
        strip_line_numbers=args.no_line_numbers,
    )

    if args.json or args.highlight:
        report = extract_pdf_json_report(pdf_path, pages=pages, **_flags)

        if args.json:
            json_str = json.dumps(report, indent=2, ensure_ascii=False)
            if args.output:
                out = Path(args.output)
                out.write_text(json_str, encoding="utf-8")
                logger.info("JSON report written to %s", out)
            else:
                print(json_str)

        if args.highlight:
            out_dir = None
            if args.output:
                # If -o points to a file, put highlights next to it
                out_dir = Path(args.output).parent / f"{pdf_path.stem}_highlighted"
            imgs = highlight_pdf_formatting(
                pdf_path, report=report, output_dir=out_dir, **_flags,
            )
            summary = report.get("summary", {})
            print(f"\n✅ Highlighted {len(imgs)} pages → {imgs[0].parent}")
            print(f"   {summary.get('total_formatted_spans', 0)} formatted spans")
            print(f"   Legend: {imgs[0].parent / 'legend.png'}")

        if args.json or args.highlight:
            return

    result = extract_pdf_with_pymupdf(
        pdf_path,
        pages=pages,
        join_pages=True,
        **_flags,
    )

    if args.clean:
        clean_kwargs = {"state_code": args.state}
        if args.clean_model:
            clean_kwargs["model"] = args.clean_model
        result = clean_extracted_text(result, **clean_kwargs)

    if args.output:
        out = Path(args.output)
        out.write_text(result, encoding="utf-8")
        logger.info("Written to %s", out)
    else:
        print(result)


if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════════
# Post-clean context rebuilder
# ═══════════════════════════════════════════════════════════════════════════════

def rebuild_report_context(report: Dict[str, Any], clean_text: str,
                           context_chars: int = 100) -> Dict[str, Any]:
    """Rebuild the ``context`` field for each formatted_span using the cleaned text.

    The original context is built from raw PyMuPDF output which has extra
    whitespace, line numbers, and PDF artifacts.  After LLM cleaning, the text
    matches Claude's conversion output much more closely, so context derived
    from it produces better anchor matching in the validation UI.

    For each span, we search for its text in the cleaned output (whitespace-
    flexible) and rebuild ``>>>text<<<`` with surrounding characters from the
    clean text.  If the span text can't be found, the original context is kept.

    Args:
        report: The PyMuPDF JSON report dict (modified in-place and returned)
        clean_text: The LLM-cleaned PyMuPDF tagged text
        context_chars: Number of surrounding characters on each side

    Returns:
        The same report dict with updated ``context`` fields
    """
    if not report or not clean_text:
        return report

    spans = report.get("formatted_spans", [])
    if not spans:
        return report

    # Strip tags from clean_text for plain-text searching
    plain = re.sub(r'\[([AD])>', '', clean_text)
    plain = re.sub(r'<([AD])\]', '', plain)

    # Track search position per page to handle duplicates in order
    page_positions: Dict[int, int] = {}

    for span in spans:
        span_text = span.get("text", "").strip()
        if not span_text:
            continue

        page = span.get("page", 0)
        start_from = page_positions.get(page, 0)

        # Try literal match first
        pos = plain.find(span_text, start_from)

        # If not found, try whitespace-flexible search
        if pos == -1:
            tokens = span_text.split()
            if len(tokens) >= 2:
                pattern = r'\s+'.join(re.escape(t) for t in tokens)
                m = re.search(pattern, plain[start_from:])
                if m:
                    pos = start_from + m.start()
                    span_text = m.group(0)  # use matched text for context

        if pos == -1:
            # Can't find in clean text — keep original context
            continue

        # Update search position for this page (advance past this match)
        page_positions[page] = pos + len(span_text)

        # Build new context
        before_start = max(0, pos - context_chars)
        after_end = min(len(plain), pos + len(span_text) + context_chars)
        before = plain[before_start:pos].strip()
        after = plain[pos + len(span_text):after_end].strip()

        new_context = ""
        if before:
            new_context += f"…{before} "
        new_context += f">>>{span_text.strip()}<<<"
        if after:
            new_context += f" {after}…"

        span["context"] = new_context

    logger.info("🔄 Rebuilt context for %d spans using cleaned text", len(spans))
    return report
