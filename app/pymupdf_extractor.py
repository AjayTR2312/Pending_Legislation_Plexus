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
import os
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

# --- Page-separator (header/footer rule) detection ------------------------
# Many bills (e.g. Connecticut SB506 / File No. 619) draw a long horizontal
# rule across the page just above the page footer (page number + bill ID).
# That rule sits within UNDERLINE_TOLERANCE_PT of the last body-text
# baseline and gets misread as an underline on the final word(s) of the
# page (producing "[A>fficers.<A]" etc.).  We strip these rules before
# the underline classifier runs.
PAGE_SEPARATOR_ZONE_PT      = 100.0  # top/bottom margin (pt) considered header/footer
PAGE_SEPARATOR_MIN_RATIO    = 0.40   # colinear segments must span ≥ 40 % of page width
PAGE_SEPARATOR_Y_TOL_PT     = 1.5    # max y-delta for segments treated as colinear
# Distinctive signature of CT decorative rules: a single visual rule is
# emitted by the PDF authoring tool as several colinear segments separated
# by sub-point gaps (e.g. 5 segments with 0.5 / 1.4 pt gaps).  A genuine
# underline drawn under text is always a single continuous segment.  We
# therefore also drop a colinear band if it has ≥ N segments AND the sum
# of segment widths covers ≥ P of the band's total horizontal span (i.e.
# the gaps are tiny).  This catches title-block separators (e.g. the
# y≈202 rule on CT joint resolutions) that fall outside the margin zone.
PAGE_SEPARATOR_MIN_SEGMENTS = 3      # min segments in band to qualify as decorative rule
PAGE_SEPARATOR_COVERAGE     = 0.95   # min sum-of-widths / span (i.e. gaps must be tiny)

# --- CT stroke-width-based footer/header separator removal ----------------
# CT bill PDFs draw footer separator rules (above the bill-ID / page-number
# footer) and header separator rules (below the header block on pages 2+)
# with a stroke width of ~1.44 pt, whereas genuine legislative underlines
# use ~0.72 pt (exactly 2× thinner).  Surveyed across 16 CT PDFs (650+
# horizontal lines):
#
#   Footer separators:  stroke = 1.440 pt   (180 / 186 footer-zone lines)
#   Header separators:  stroke = 1.464 pt   ( 88 /  90 header-zone lines)
#   Genuine underlines: stroke = 0.720 pt   (all body lines + 6 footer-zone)
#   Title decorative:   stroke = 0.480 pt   (page-1 title block only)
#
# A threshold of 1.0 pt gives a 0.28 pt safety margin on each side.
# This pre-filter runs BEFORE any other hline processing so downstream
# code never sees footer/header separator drawings at all — eliminating
# both false [A>…<A] tags on the last body line AND the problem where
# genuine underlines near the footer were incorrectly suppressed by the
# old text-above heuristic in _filter_page_separator_hlines.
CT_FOOTER_SEPARATOR_MIN_STROKE_PT = 1.0   # lines with stroke ≥ this in margin zone → separator
CT_FOOTER_SEPARATOR_ZONE_PT       = 100.0 # top/bottom margin zone (pt)

# --- Arkansas-specific leader-line filter ----------------------------------
# Arkansas appropriation/fiscal bills (e.g. JAR104 — SB 8 of the 2026 Fiscal
# Session) draw horizontal "leader" lines as typographic decoration: a thin
# rule sits at the baseline of a tabular row and runs from one column across
# whitespace to another (think TOC dot-leaders, but as a continuous line).
# These rules incidentally overlap a few characters at one end (e.g. the "2"
# in the "Maximum No. of Employees" column of the ADMINISTRATIVE SPECIALIST
# salary row) and get misread by :func:`_classify_char_decoration` as
# legitimate underlines, producing false ``[A>2<A]`` tags.
#
# A genuine legislative underline always has text directly above ≥ half of
# the line's horizontal extent — the line was *drawn under* that text.
# Leader lines have text covering < half of their extent because most of
# the line sits in the whitespace gap between columns.  In the JAR104
# survey the two populations were perfectly separable: real underlines
# 69–100% coverage, leaders 0–35% coverage.  The 50% threshold leaves a
# 34-point margin on each side.
#
# This filter is intentionally AR-only.  Other states (DE, CT, CA, …) draw
# underlines that hug the text width, so the discriminator isn't needed
# and isn't applied.  See the call sites for the state-code guard.
AR_LEADER_LINE_COVERAGE_MAX  = 0.50  # drop lines with < this fraction of text coverage
AR_LEADER_LINE_BASELINE_TOL  = 4.0   # chars within this pt above the line count as "on baseline"
AR_LEADER_LINE_MIN_WIDTH_PT  = 10.0  # ignore very short lines (genuine 1-char underlines)

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

# --- Visual column-gap padding (same physical row, big x-gap) --------------
# When the visual x-gap between two same-row sibling lines is large enough
# that it is clearly a TABLE COLUMN gap (not a normal word break), we pad
# so column structure survives extraction.
#
# Background.  Many tabular bills are authored in two distinct styles:
#
#   * AR-style ("self-padded"): the PDF authoring tool draws several
#     whitespace-only "spacer" lines between cells.  Each spacer's content
#     (`' '` ≈ 1 char) plus the default 1-char joiner produces ~2 chars of
#     padding per ~30pt gap, giving an effective ~15 pt/padding-char that
#     reads as a columnar layout in the raw extraction.
#
#   * DE-style ("sparse"): the PDF authoring tool emits only the cell
#     contents — no spacer lines at all.  A 310pt visual gap between
#     ``"Absalom Jones"`` and the dollar column collapses to a single
#     joiner space, jamming the columns together and making the row look
#     like prose to downstream LLM cleaning.
#
# Padding strategy: POSITION-BASED.  When the gap exceeds the threshold we
# compute the *target output column* from the next sibling's PDF x-position
# (``target_col = round(line_x0 / POINTS_PER_PADDING_SPACE)``) and emit
# exactly enough spaces to land there.  This makes every row in a tabular
# block put its data cells at the *same* output column regardless of how
# wide the row's label happens to be — e.g. "Absalom Jones" (13 chars) and
# "Chesapeake & Delaware" (21 chars) both anchor the dollar column at
# column 28, so the table reads as a true grid in plain-text view.
#
# Gap-based fallback never engages below the threshold, so AR-style bills
# (where same-row gaps are ~30pt) keep the 1-space joiner and remain
# byte-identical to pre-padding output.  The pt-per-char ratio of 16 is
# empirical: it puts a typical 8.5×11" page (~510pt wide content area)
# inside a ~32-column output, which matches the natural density of AR's
# self-padded rows so AR and DE layouts read at the same visual scale.
#
# Lowered from 60 → 35 to catch IN-style borderless tables where the
# gap between description and dollar-amount columns is 39–50 pt.  AR
# self-padded bills (~30 pt per-spacer gap) stay below the threshold.
COLUMN_GAP_PAD_MIN_PT    = 35.0  # below this, keep current 1-space joiner
POINTS_PER_PADDING_SPACE = 16.0  # pt per output column (defines target_col)
COLUMN_GAP_MAX_SPACES    = 80    # safety cap on padding insertion
COLUMN_GAP_MIN_SPACES    = 6     # minimum cell separator even when target
                                 # column is already passed (long label row).
                                 # MUST be ≥ 4 so it survives the "2-3 spaces
                                 # → 1 space" collapse in _clean_tagged_text.
                                 # Raised from 4 → 6 to give wider visual
                                 # gap for long organisation names in
                                 # DE-style appropriation tables.

# --- Superscript footnote-reference detection ----------------------------
# Legislative bills (especially CT OFA Fiscal Notes) place footnote markers
# as typographic superscripts: a smaller font + raised baseline.  Without
# special handling, "$3,300²" is extracted as the digits "$3,3002", which
# changes the meaning of the number; "fines¹" becomes "fines.1", etc.
#
# A span is treated as a superscript when BOTH:
#   • its font size ≤ SUPERSCRIPT_SIZE_RATIO of the line's dominant size, AND
#   • its bbox bottom is raised ≥ SUPERSCRIPT_RAISE_PT above the line bottom.
#
# The raised-bottom check distinguishes inline superscripts from footnote
# *bodies* at the bottom of the page (where the entire line is small but
# sits on its own baseline, so it has zero raise relative to its own line).
#
# Detected superscript chars are converted to Unicode superscript glyphs
# (¹²³…) so the visual distinction is preserved in plain text — and they
# are excluded from underline / italic / bold tagging since footnote refs
# are pure typography, not legal content.
SUPERSCRIPT_SIZE_RATIO   = 0.80  # span font ≤ 80 % of line dominant size
SUPERSCRIPT_RAISE_PT     = 1.0   # span bottom ≥ 1 pt above line bottom

# Tag delimiters (match the project convention)
ADD_OPEN, ADD_CLOSE = "[A>", "<A]"
DEL_OPEN, DEL_CLOSE = "[D>", "<D]"

# California margin line-number marker, e.g. "line 1", " line 27"
# Used to skip these narrow lines entirely during paragraph-break detection
# so they don't pollute the continuation-x baseline calculation.
_RE_CA_LINE_MARKER = re.compile(r"^\s*line\s+\d+\s*$", re.IGNORECASE)

# Bare margin line-number marker — many states (IN, IA, …) number every
# line of the bill body with a bare 1–3 digit number ("1", "37", "42")
# placed in a narrow column at the left margin.  These sit at the SAME
# y-coordinate as the adjacent body-text line, so PyMuPDF treats them as
# same-physical-row siblings.  Without filtering they prevent the
# paragraph-indent detector from firing: the line-number's x0 (~114 pt)
# anchors ``prev_line_y_mid``, and the body line at x0=180 pt that
# follows on the same row gets a space-joiner instead of a paragraph
# break.  Skipping bare line-number lines (just like "line N" markers)
# restores correct paragraph detection.
_RE_BARE_LINE_NUMBER = re.compile(r"^\s*\d{1,3}\s*$")

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
    """Return bounding boxes ``(x0, y0, x1, y1)`` of *real* tables found on *page*.

    Uses PyMuPDF's built-in ``find_tables()`` API, then **validates** each
    detected zone by requiring evidence of actual table structure — at
    least one stroked horizontal or vertical line segment inside (or on
    the edge of) the zone.

    Why validate?  ``find_tables()`` infers tables from text alignment
    alone and frequently produces false positives on legislative bills
    where definitions or numbered subsections create aligned columns of
    text decorated only with underlines (filled rectangles, not stroked
    lines).  Without validation, every underlined definition on such a
    page would be misidentified as a table cell and its underline tag
    would be suppressed by the downstream filters.

    A zone is accepted only when ``get_drawings()`` reports at least one
    **stroked** path (``item[0] == "l"``) — either a horizontal line
    spanning ≥ 40 % of the table width, or any vertical line — whose
    coordinates fall inside the zone's bbox (with a small tolerance).
    Filled rectangles (``"re"`` items, the usual rendering for
    underlines and strike-throughs) do NOT count as structural evidence
    because they are exactly the kind of decoration the downstream
    filters are trying to *preserve*, not suppress.

    Stroked horizontal lines that look like UNDERLINES (sitting within
    ``UNDERLINE_TOLERANCE_PT`` below a text baseline and substantially
    overlapping that text's x-range) are also excluded from evidence —
    they are decoration, not structure.  This matters for Delaware-style
    bills whose underlines are emitted as stroked ``"l"`` items rather
    than the more common filled ``"re"`` rectangles; without this guard
    a single long underlined paragraph fools ``find_tables()`` into
    treating the surrounding text as tabular and suppresses ALL
    underline tagging in that region (the page-7 SB295 bug, 2026-06-29).

    Returns an empty list if no tables are detected, no detected zones
    pass validation, or the API is unavailable.
    """
    try:
        tabs = page.find_tables()
        raw_zones = [tuple(t.bbox) for t in tabs.tables]  # type: ignore[union-attr]
    except Exception:
        return []

    if not raw_zones:
        return []

    # Collect text-line bboxes once so we can identify stroked hlines
    # that are actually underlines (and therefore must NOT count as
    # table-structure evidence).
    try:
        text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
        text_line_bboxes: List[Tuple[float, float, float, float]] = []
        for blk in text_dict.get("blocks", []):
            if blk.get("type") != 0:
                continue
            for ln in blk.get("lines", []):
                bb = ln.get("bbox")
                if bb:
                    text_line_bboxes.append(tuple(bb))
    except Exception:
        text_line_bboxes = []

    def _looks_like_underline(y: float, x0: float, x1: float) -> bool:
        """True if this stroked horizontal line sits under a text line and
        substantially overlaps it horizontally — i.e. it's a hand-drawn
        underline, not a table border.
        """
        line_width = x1 - x0
        if line_width <= 0:
            return False
        for tx0, ty0, tx1, ty1 in text_line_bboxes:
            # Underline sits below baseline → y is within tolerance of
            # the text bbox bottom (a small slack above the bottom is
            # allowed because the underline can be drawn at the
            # baseline rather than below the descender).
            if not (ty1 - 1.0 <= y <= ty1 + UNDERLINE_TOLERANCE_PT):
                continue
            # Horizontal overlap must cover most of the stroked line.
            overlap = _horizontal_overlap(x0, x1, tx0, tx1)
            if overlap / line_width >= 0.80:
                return True
        return False

    # Collect stroked-line evidence from drawings.  We look at the raw
    # path items rather than the post-processed hlines because we need
    # to distinguish stroked lines (real borders) from filled rectangles
    # (underlines).
    try:
        drawings = page.get_drawings()
    except Exception:
        # If we can't introspect the drawings, fall back to the legacy
        # behaviour (trust find_tables).
        return raw_zones

    stroked_hlines: List[Tuple[float, float, float]] = []  # (y, x0, x1)
    stroked_vlines: List[Tuple[float, float, float]] = []  # (x, y0, y1)
    for path in drawings:
        for item in path.get("items", []):
            if not item or item[0] != "l":
                continue  # only stroked lines, not filled rects
            try:
                p1 = fitz.Point(item[1])
                p2 = fitz.Point(item[2])
            except Exception:
                continue
            dx = abs(p1.x - p2.x)
            dy = abs(p1.y - p2.y)
            if dy <= HORIZONTAL_SLOPE_TOL and dx > 2:
                y = (p1.y + p2.y) / 2
                x0 = min(p1.x, p2.x)
                x1 = max(p1.x, p2.x)
                # Skip stroked horizontals that look like text underlines —
                # they're decoration, not structural borders.
                if _looks_like_underline(y, x0, x1):
                    continue
                stroked_hlines.append((y, x0, x1))
            elif dx <= HORIZONTAL_SLOPE_TOL and dy > 2:
                x = (p1.x + p2.x) / 2
                y0 = min(p1.y, p2.y)
                y1 = max(p1.y, p2.y)
                stroked_vlines.append((x, y0, y1))

    if not stroked_hlines and not stroked_vlines:
        # No stroked geometry anywhere on the page → no zone can have
        # structural evidence.  Treat all detected zones as false
        # positives (typical of legislative bills whose only graphics
        # are underline rectangles).
        logger.debug(
            "Rejected %d find_tables() zones: page has no stroked lines",
            len(raw_zones),
        )
        return []

    tol = 3.0  # pt tolerance for "inside the zone"
    validated: List[Tuple[float, float, float, float]] = []
    for zx0, zy0, zx1, zy1 in raw_zones:
        zwidth = zx1 - zx0
        has_evidence = False

        # Horizontal stroke spanning ≥ 40 % of the table width and
        # sitting within the table's vertical span.
        if zwidth > 0:
            for y, x0, x1 in stroked_hlines:
                if not (zy0 - tol <= y <= zy1 + tol):
                    continue
                overlap = _horizontal_overlap(x0, x1, zx0, zx1)
                if overlap / zwidth >= 0.40:
                    has_evidence = True
                    break

        # Any vertical stroke inside the zone counts (column border).
        if not has_evidence:
            for x, y0, y1 in stroked_vlines:
                if not (zx0 - tol <= x <= zx1 + tol):
                    continue
                # Require the vertical line to cover a meaningful
                # fraction of the zone's height (≥ 30 %) so we don't
                # accept stray vertical ticks (e.g. tally marks).
                v_overlap = max(0.0, min(y1, zy1) - max(y0, zy0))
                zheight = zy1 - zy0
                if zheight > 0 and v_overlap / zheight >= 0.30:
                    has_evidence = True
                    break

        if has_evidence:
            validated.append((zx0, zy0, zx1, zy1))
        else:
            logger.debug(
                "Rejected find_tables() zone (no stroked border evidence): "
                "bbox=(%.1f, %.1f, %.1f, %.1f)",
                zx0, zy0, zx1, zy1,
            )

    return validated


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


# ---------------------------------------------------------------------------
# Page-separator (header/footer rule) filtering
# ---------------------------------------------------------------------------
# NOTE: this filter is currently invoked **only for Connecticut bills**.  The
# call sites (`_extract_page_tagged_text`, `_extract_page_spans`,
# `extract_pdf_json_report`) are gated by `state_code == "CT"` so other
# states' extraction behaviour is unchanged.  See the call sites for the
# guard.

def _filter_page_separator_hlines(
    hlines: List[HLine],
    page: fitz.Page,
    *,
    zone_pt: float = PAGE_SEPARATOR_ZONE_PT,
    min_extent_ratio: float = PAGE_SEPARATOR_MIN_RATIO,
    y_tolerance: float = PAGE_SEPARATOR_Y_TOL_PT,
    min_segments: int = PAGE_SEPARATOR_MIN_SEGMENTS,
    coverage_ratio: float = PAGE_SEPARATOR_COVERAGE,
) -> List[HLine]:
    """Remove horizontal rules that delimit page headers / footers.

    Many legislative PDFs (notably Connecticut bills) draw long horizontal
    rules across the page — one above the body to separate the title
    block (e.g. ``Substitute Senate Joint Resolution No. 52`` at
    y≈202) and one above the page footer (page number + bill ID at
    y≈714).  These rules sit within ``UNDERLINE_TOLERANCE_PT`` of an
    adjacent text baseline and get misclassified as underlines by
    :func:`_classify_char_decoration`, producing false ``[A>…<A]`` tags
    on the bill title or final word(s) of a body page.

    A colinear band of horizontal segments is treated as a separator
    rule when **either** of these holds:

    1. **Margin-zone rule.**  The band's ``y`` falls in the top
       *zone_pt* or bottom *zone_pt* of the page **and** its combined
       extent is ≥ *min_extent_ratio* of the page width **and** there
       is no text line whose baseline sits within
       ``UNDERLINE_TOLERANCE_PT`` directly above the band.  The
       text-above check is critical: the last body-text line on a CT
       bill page (e.g. line 13 of sHB5164) commonly carries a genuine
       legislative underline that falls inside the bottom-margin zone.
       Without this check, those underlines were incorrectly stripped
       alongside the actual footer separator, producing missing
       ``[A>…<A]`` tags on the final line.

    2. **Multi-segment decorative rule.**  The band contains
       ≥ *min_segments* colinear pieces, the sum of their widths is
       ≥ *coverage_ratio* of the band's total horizontal span (i.e.
       only sub-point gaps separate them), **and** that span is
       ≥ *min_extent_ratio* of the page width.  This is the distinctive
       signature of CT title-block / footer rules — a real underline
       under text is always a single continuous segment, so this rule
       cannot match genuine underlined legislative content.  We do not
       apply the text-above check here because some CT footer rules
       (e.g. SB506) are deliberately drawn within underline tolerance
       of the last body line; the multi-segment shape is what
       distinguishes them from real underlines.

    Bands that fail both criteria are kept untouched.
    """
    if not hlines:
        return hlines

    page_height = page.rect.height
    page_width  = page.rect.width
    if page_height <= 0 or page_width <= 0:
        return hlines

    header_y_max = zone_pt
    footer_y_min = page_height - zone_pt

    # --- Pre-compute text-line bboxes for the text-above check.
    # We collect every visible text line on the page so we can ask:
    # "does any line's baseline sit within underline tolerance above
    # this band?"  Real underlines always have such a line; footer /
    # header separators don't (the page number / bill ID appears in a
    # separate text line further down, not directly above the rule).
    text_line_bboxes: List[Tuple[float, float, float, float]] = []
    try:
        td = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
        for blk in td.get("blocks", []):
            if blk.get("type") != 0:
                continue
            for ln in blk.get("lines", []):
                bb = ln.get("bbox")
                if bb and len(bb) == 4 and (bb[2] > bb[0]) and (bb[3] > bb[1]):
                    text_line_bboxes.append(tuple(bb))
    except Exception:
        # If text extraction fails, fall back to the old behaviour
        # (margin-zone rule dropped without text-above check).
        text_line_bboxes = []

    def _text_baseline_just_above(band_y: float, band_x0: float, band_x1: float) -> bool:
        """True if some text-line baseline sits within UNDERLINE_TOLERANCE_PT
        directly above (or at) *band_y*, with horizontal overlap."""
        for tx0, ty0, tx1, ty1 in text_line_bboxes:
            # Horizontal overlap required (≥ 1pt is enough — single chars
            # at a line's edge are still genuine underlined content).
            if min(tx1, band_x1) - max(tx0, band_x0) < 1.0:
                continue
            # Baseline ≈ line bbox bottom (y1).  An underline draws at
            # baseline ± a couple of points.  We accept any band whose
            # y is within UNDERLINE_TOLERANCE_PT below y1 OR up to ~3pt
            # above y1 (some fonts draw the underline slightly above
            # the descender line).
            dy = band_y - ty1
            if -3.0 <= dy <= UNDERLINE_TOLERANCE_PT:
                return True
        return False

    # Group ALL hlines into colinear bands (sorted by y, glued by y_tolerance).
    sorted_hl = sorted(hlines, key=lambda h: h.y)
    bands: List[List[HLine]] = []
    for hl in sorted_hl:
        if bands and abs(hl.y - bands[-1][-1].y) <= y_tolerance:
            bands[-1].append(hl)
        else:
            bands.append([hl])

    drop_ids: set = set()
    for band in bands:
        x_min = min(h.x0 for h in band)
        x_max = max(h.x1 for h in band)
        span  = x_max - x_min
        if span <= 0:
            continue
        width_ratio = span / page_width
        if width_ratio < min_extent_ratio:
            continue   # too short to be a separator regardless of position

        band_y = band[0].y
        in_margin = (band_y <= header_y_max) or (band_y >= footer_y_min)

        # Criterion 1: wide rule in margin zone — but ONLY when no text
        # line baseline sits just above the band.  Genuine underlines
        # on the last body-text line of a page will fall inside the
        # bottom margin zone but always have their text directly
        # above; we must preserve them.
        is_separator = False
        if in_margin and not _text_baseline_just_above(band_y, x_min, x_max):
            is_separator = True

        # Criterion 2: multi-segment decorative rule (anywhere on page)
        # — applied without text-above check because CT footer rules
        # are deliberately drawn within underline tolerance of the
        # last body line; the multi-segment colinear shape is what
        # distinguishes them from real underlines.
        #
        # HOWEVER: skip thick bands (stroke ≥ 1.0pt) because these are
        # footer/header separator lines already handled by the stroke-
        # based filter (_strip_ct_footer_separator_hlines).  If that
        # filter preserved them (because body text sits above), we
        # must NOT remove them here.  Thin multi-segment bands (e.g.
        # title-block decorative rules at stroke=0.48pt) are still
        # caught by this criterion.
        if not is_separator and len(band) >= min_segments:
            # Check if ALL segments in the band are thick (stroke >= 1.0)
            # — if so, they were already evaluated by the stroke filter
            band_is_thick = all(h.width >= CT_FOOTER_SEPARATOR_MIN_STROKE_PT for h in band)
            if not band_is_thick:
                sum_widths = sum(h.x1 - h.x0 for h in band)
                if sum_widths / span >= coverage_ratio:
                    is_separator = True

        if is_separator:
            for h in band:
                drop_ids.add(id(h))

    if not drop_ids:
        return hlines

    kept = [hl for hl in hlines if id(hl) not in drop_ids]
    if len(kept) < len(hlines):
        logger.debug(
            "Filtered %d page-separator hlines (header/footer/title rules) — kept %d",
            len(hlines) - len(kept), len(kept),
        )
    return kept


def _strip_ct_footer_separator_hlines(
    hlines: List[HLine],
    page: fitz.Page,
    *,
    min_stroke: float = CT_FOOTER_SEPARATOR_MIN_STROKE_PT,
    zone_pt: float = CT_FOOTER_SEPARATOR_ZONE_PT,
) -> List[HLine]:
    """Remove CT footer/header separator lines based on stroke width.

    CT legislative PDFs draw thick horizontal rules (~1.44 pt stroke)
    at the top and bottom of each page as visual separators between
    the body text and the header/footer blocks.  Genuine underlines
    use ~0.72 pt stroke — exactly half the thickness.

    This function **unconditionally** removes thick horizontal lines
    (stroke ≥ *min_stroke*) that fall inside the margin zone (top or
    bottom *zone_pt* of the page).

    Why unconditional?  Geometric analysis across 16 CT PDFs (650+
    horizontal lines) shows that the CT footer separator (stroke
    ~1.44 pt) is NEVER a genuine underline — it is purely a
    decorative rule.  When body text on the last line of a page IS
    genuinely underlined, the PDF always draws a SEPARATE thin
    underline rect (stroke ~0.72 pt) underneath that text.  The thick
    footer separator sits further below, within the margin zone, and
    merely *happens* to fall within ``UNDERLINE_TOLERANCE_PT`` of the
    last text baseline.

    Diagnostic evidence (sSB294 pp. 1/3/5):
      • Page 1 line 12 "State in the office…": NO genuine 0.72 pt
        underline exists — the ONLY line beneath it is the footer
        separator at 1.44 pt.  The text is NOT underlined.
      • Genuinely underlined text on the same pages (lines 9–11)
        each has its own 0.72 pt underline rect that is unaffected
        by this removal.

    By stripping these thick lines unconditionally, downstream
    ``_classify_char_decoration`` never sees them, so no false
    ``[A>…<A]`` tags are produced on the last body line of each page.

    Returns a new list with separator lines removed.
    """
    if not hlines:
        return hlines

    page_height = page.rect.height
    if page_height <= 0:
        return hlines

    footer_y_min = page_height - zone_pt
    header_y_max = zone_pt

    kept: List[HLine] = []
    dropped = 0
    for hl in hlines:
        in_header = hl.y <= header_y_max
        in_footer = hl.y >= footer_y_min
        in_margin = in_header or in_footer
        if in_margin and hl.width >= min_stroke:
            # Thick line in margin zone → footer/header separator.
            # ALWAYS remove — genuine underlines use ~0.72 pt stroke
            # and are drawn as separate rects that remain in the list.
            dropped += 1
            logger.debug(
                "Stripped CT footer/header separator: y=%.1f "
                "x=[%.1f,%.1f] stroke=%.3f zone=%s",
                hl.y, hl.x0, hl.x1, hl.width,
                "header" if in_header else "footer",
            )
        else:
            kept.append(hl)

    if dropped:
        logger.info(
            "CT footer/header separator filter: removed %d — %d hlines remain",
            dropped, len(kept),
        )
    return kept



# ---------------------------------------------------------------------------
# Arkansas-only: leader / decorative-rule filter
# ---------------------------------------------------------------------------
# NOTE: invoked **only for Arkansas bills**.  The call sites
# (`_extract_page_tagged_text`, `_extract_page_spans`,
# `extract_pdf_json_report`) are gated by `state_code == "AR"` so other
# states' extraction behaviour is unchanged.

def _filter_ar_leader_lines(
    hlines: List[HLine],
    page: fitz.Page,
    *,
    coverage_max: float = AR_LEADER_LINE_COVERAGE_MAX,
    baseline_tol: float = AR_LEADER_LINE_BASELINE_TOL,
    min_width_pt: float = AR_LEADER_LINE_MIN_WIDTH_PT,
) -> List[HLine]:
    """Remove Arkansas-style typographic leader/decorative rules.

    AR appropriation/fiscal bills (JAR104, etc.) draw thin horizontal
    rules that sit at the baseline of a row but extend across whitespace
    between columns — TOC-style leaders, page-wide ruled lines under
    table headers, dollar-column rules, and similar decoration.  These
    rules incidentally overlap a handful of characters at one end
    (e.g. the "2" in the *Maximum No. of Employees* column of the
    ``(16) ADMINISTRATIVE SPECIALIST`` salary row) and are misread by
    :func:`_classify_char_decoration` as underlines, producing false
    ``[A>2<A]`` tags on isolated digits/whitespace.

    A genuine legislative underline or strikethrough always has text
    directly along ≥ ``coverage_max`` of the line's horizontal extent —
    the line was drawn *under* that text (underline) or *through* it
    (strikethrough).  A leader/decorative rule has text covering
    ≤ ``coverage_max`` of its extent (most of it sits in whitespace
    between columns).  Empirically on JAR104, real decoration lines
    covered 69–100 % of the line while leaders covered 0–35 %; the
    default 50 % threshold leaves a 34-point margin on each side.

    Coverage criterion (covers BOTH underline and strikethrough
    geometries — important so we don't accidentally strip
    strikethrough rules on stricken years like ``[D>2025<D]``):

      A character is treated as "text-bearing" for this hline when
      either holds:
        (a) the hline y falls anywhere within the character's vertical
            bounding box ``[cy_top, cy_bottom]`` — this captures
            strikethroughs that sit at the character's midline; OR
        (b) the hline y is within ``baseline_tol`` points BELOW the
            baseline (``cy_bottom``) — this captures underlines.

    A line is **dropped** when:
      1. Its width is ≥ ``min_width_pt`` (very short lines under a
         single character are skipped to avoid removing real 1-char
         underlines), **and**
      2. The union of text-bearing characters' x-ranges, clipped to
         the hline's x-range, covers strictly less than
         ``coverage_max`` of the line's horizontal extent.

    Lines without any nearby text (e.g. floating decorative borders)
    have 0 % coverage and are also dropped — that is harmless because
    those lines can't be misclassified as underlines anyway (the
    classifier requires nearby text), so removing them is a no-op for
    correctness and reduces work downstream.

    This filter is intentionally AR-only.  Other states (DE, CT, CA, …)
    draw underlines that hug the text width and do not exhibit leader
    rules in tabular layouts, so the heuristic isn't needed and isn't
    applied.
    """
    if not hlines:
        return hlines

    # Collect non-space character bboxes from rawdict.  We need char-level
    # precision (not line-level) because a tabular row may have widely
    # spaced cells — a line-bbox would falsely span the entire row width.
    try:
        raw = page.get_text("rawdict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
    except Exception:
        return hlines

    # (cx0, cx1, cy_top, cy_bottom) — we need the FULL vertical extent
    # (not just the baseline) so strikethrough lines that sit at the
    # character's midline qualify as "text-bearing".  Pre-2026-06-29
    # this stored only cy_bottom and the baseline-only check caused
    # strikethroughs on stricken years (e.g. ``[D>2025<D]``) to be
    # incorrectly dropped as leader lines.
    char_records: List[Tuple[float, float, float, float]] = []
    for blk in raw.get("blocks", []):
        if blk.get("type") != 0:
            continue
        for ln in blk.get("lines", []):
            for sp in ln.get("spans", []):
                for ch in sp.get("chars", []):
                    bb = ch.get("bbox")
                    c  = ch.get("c", "")
                    if bb and c and not c.isspace():
                        char_records.append((bb[0], bb[2], bb[1], bb[3]))

    # If we have no character data at all (e.g. an image-only page), every
    # hline would have 0 % coverage.  Leave hlines untouched in that case
    # so we don't strip decoration the classifier wouldn't have used either.
    if not char_records:
        return hlines

    drop_ids: set = set()
    for hl in hlines:
        hl_width = hl.x1 - hl.x0
        if hl_width < min_width_pt:
            continue  # too short — keep (could be a real 1-char underline)

        # Clip every "text-bearing" char's x-range to the hline's x-range
        # then merge the overlapping intervals to compute the union
        # coverage in points.  "Text-bearing" means the hline y sits
        # either within the character's vertical bbox (strikethrough
        # case) OR within ``baseline_tol`` pt below its baseline
        # (underline case).  Both bands together cover every decoration
        # geometry we care about; lines further below the text are
        # decorative leaders.
        clipped: List[Tuple[float, float]] = []
        for cx0, cx1, cy_top, cy_bot in char_records:
            in_bbox      = (cy_top - 1.0) <= hl.y <= (cy_bot + 0.5)
            below_baseline = cy_bot < hl.y <= (cy_bot + baseline_tol)
            if not (in_bbox or below_baseline):
                continue
            lo = max(cx0, hl.x0)
            hi = min(cx1, hl.x1)
            if hi > lo:
                clipped.append((lo, hi))
        if not clipped:
            # No text on this line's baseline at all → harmless to drop.
            drop_ids.add(id(hl))
            continue
        clipped.sort()
        union_pts = 0.0
        cur_lo, cur_hi = clipped[0]
        for lo, hi in clipped[1:]:
            if lo <= cur_hi:
                cur_hi = max(cur_hi, hi)
            else:
                union_pts += cur_hi - cur_lo
                cur_lo, cur_hi = lo, hi
        union_pts += cur_hi - cur_lo

        if union_pts / hl_width < coverage_max:
            drop_ids.add(id(hl))

    if not drop_ids:
        return hlines

    kept = [hl for hl in hlines if id(hl) not in drop_ids]
    logger.debug(
        "AR leader-line filter dropped %d hlines (kept %d) on page",
        len(hlines) - len(kept), len(kept),
    )
    return kept


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
# Superscript footnote-reference helpers
# ---------------------------------------------------------------------------

# Unicode superscript translation tables.  We map digits, the lowercase
# alphabet (sans 'q' which has no Unicode superscript), and the most
# common math operators.  Characters without a superscript form are
# left unchanged.
_SUPERSCRIPT_DIGIT_MAP = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")
_SUPERSCRIPT_LETTER_MAP = str.maketrans(
    "abcdefghijklmnoprstuvwxyz",
    "ᵃᵇᶜᵈᵉᶠᵍʰⁱʲᵏˡᵐⁿᵒᵖʳˢᵗᵘᵛʷˣʸᶻ",
)
_SUPERSCRIPT_SYM_MAP = str.maketrans({
    "+": "⁺", "-": "⁻", "=": "⁼", "(": "⁽", ")": "⁾",
})

# Canonical footnote-reference shapes.  We only convert a small + raised
# span to Unicode superscripts when its text matches one of these forms:
#   • 1-3 digits          ("1", "12", "123")
#   • 1-3 letters         ("a", "th", "st", "nd", "rd")
#   • 1-3 footnote symbols ("*", "**", "†", "‡", "∗", "§", "¶", "#")
# Spans that pass the size+raise check but DON'T match the pattern (e.g.
# "46.4%" chart labels, "$3,300" body text accidentally split into a
# small-bbox span) are left as-is to avoid mangling legal content.
_FOOTNOTE_REF_RE = re.compile(
    r"^\s*("
    r"[0-9]{1,3}"
    r"|[a-z]{1,3}"
    r"|[*†‡§¶#∗]{1,3}"
    r")\s*$",
    re.IGNORECASE,
)


def _to_superscript(s: str) -> str:
    """Convert digits / lowercase letters / +-=() in *s* to Unicode
    superscript glyphs.  Characters without a mapping are returned as-is.

    Examples:
        _to_superscript("2")   == "²"
        _to_superscript("12")  == "¹²"
        _to_superscript("a")   == "ᵃ"
        _to_superscript("*")   == "*"   # no superscript form
    """
    return (
        s.translate(_SUPERSCRIPT_DIGIT_MAP)
         .translate(_SUPERSCRIPT_LETTER_MAP)
         .translate(_SUPERSCRIPT_SYM_MAP)
    )


def _line_dominant_font_size(line: dict) -> float:
    """Return the most common font size on *line* weighted by character count.

    Used as the reference body-text size when deciding whether a particular
    span is rendered as a superscript footnote reference.  Returns 12.0
    when no text is present so callers don't divide by zero.
    """
    sizes: Dict[float, int] = {}
    for sp in line.get("spans", []):
        try:
            sz = round(float(sp.get("size", 0) or 0), 1)
        except (TypeError, ValueError):
            continue
        ntxt = len(sp.get("text", "") or "")
        if sz > 0 and ntxt > 0:
            sizes[sz] = sizes.get(sz, 0) + ntxt
    if not sizes:
        return 12.0
    return max(sizes, key=lambda k: sizes[k])


def _span_is_superscript(
    span: dict,
    line_dominant_size: float,
    line_bbox: Tuple[float, float, float, float],
) -> bool:
    """Return ``True`` when *span* is rendered as a footnote-reference superscript.

    A span qualifies only when **all three** conditions hold:

      1. ``span.size ≤ SUPERSCRIPT_SIZE_RATIO * line_dominant_size`` — the
         span is noticeably smaller than the surrounding body text.
      2. ``line_bbox.y1 - span.bbox.y1 ≥ SUPERSCRIPT_RAISE_PT`` — the span's
         bottom edge sits at least one point above the line's bottom edge,
         i.e. it is **raised** relative to the body baseline.
      3. The span's text matches the canonical footnote-reference shape
         (``_FOOTNOTE_REF_RE``): a short run of digits, letters, or
         footnote symbols.  This last guard prevents the conversion from
         mangling chart labels like ``"46.4%"`` or accidentally-split
         body-text fragments that happen to be small and raised.

    Condition (2) is what separates an inline superscript from a footnote
    *body* line at the bottom of a page: the body line is small in absolute
    size but its bbox bottom equals the line bbox bottom (zero raise).
    """
    try:
        sz = float(span.get("size", 0) or 0)
    except (TypeError, ValueError):
        return False
    if sz <= 0 or line_dominant_size <= 0:
        return False
    if sz / line_dominant_size > SUPERSCRIPT_SIZE_RATIO:
        return False
    sbbox = span.get("bbox") or (0, 0, 0, 0)
    if not sbbox or len(sbbox) < 4 or (sbbox[2] - sbbox[0]) <= 0:
        return False
    if (line_bbox[3] - sbbox[3]) < SUPERSCRIPT_RAISE_PT:
        return False
    # Text-shape guard: confirm the span looks like a footnote marker.
    # Read text from `chars` (rawdict) first, falling back to `text`.
    chars_data = span.get("chars") or []
    if chars_data:
        text = "".join(ch.get("c", "") for ch in chars_data)
    else:
        text = span.get("text", "") or ""
    if not text or not _FOOTNOTE_REF_RE.match(text):
        return False
    return True


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
# Line-order helpers
# ---------------------------------------------------------------------------

def _sort_block_lines_visually(
    lines: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Reorder a block's text "lines" so spans on the same physical row
    appear in left-to-right visual order.

    PyMuPDF returns text fragments in PDF *drawing* order rather than
    visual reading order.  When a tabular row was authored with the
    number drawn first and a "$" sigil drawn afterwards but positioned
    to the LEFT of the number (a common pattern in PDFs generated from
    accounting templates — e.g. Delaware Grants-in-Aid bills), the raw
    line order is:

        Line N:   "3,880,543"   x0 = 528.9
        Line N+1: "$"           x0 = 506.0   ← visually LEFT of number

    Without reordering, the extractor renders that row as
    ``"3,880,543 $"``, scrambling the currency notation.  This helper
    groups consecutive lines whose y-midpoints fall within
    ``SAME_ROW_Y_TOL_PT`` of each other and sorts each group by the
    line's leftmost x-coordinate so the same row renders as
    ``"$ 3,880,543"``.

    Lines on different physical rows keep their original relative order,
    preserving PyMuPDF's top-to-bottom flow and any multi-column layout
    decisions baked into the block structure.
    """
    if len(lines) <= 1:
        return list(lines)

    result: List[Dict[str, Any]] = []
    pending: List[Tuple[float, int, Dict[str, Any]]] = []  # (x0, idx, line)
    group_y: Optional[float] = None

    def _flush() -> None:
        if not pending:
            return
        # Sort the same-physical-row group by leftmost x-coordinate.
        # The original index is a tie-breaker so identical x0 values
        # preserve PyMuPDF's drawing order (stable sort).
        pending.sort(key=lambda t: (t[0], t[1]))
        result.extend(p[2] for p in pending)
        pending.clear()

    for idx, ln in enumerate(lines):
        bbox = ln.get("bbox", (0.0, 0.0, 0.0, 0.0))
        y_mid = (bbox[1] + bbox[3]) / 2
        x0 = bbox[0]
        if group_y is None or abs(y_mid - group_y) <= SAME_ROW_Y_TOL_PT:
            pending.append((x0, idx, ln))
            if group_y is None:
                group_y = y_mid
        else:
            _flush()
            pending.append((x0, idx, ln))
            group_y = y_mid

    _flush()
    return result


def _count_chars_since_last_newline(output_pieces: List[str]) -> int:
    """
    Count how many characters have been emitted on the current physical
    output row since the most recent ``'\\n'`` in *output_pieces*.

    Used by ``_extract_page_tagged_text`` to compute *position-based*
    column padding: when we are about to emit a same-row sibling line at
    PDF x-position ``X``, we want it to land at output column
    ``round(X / POINTS_PER_PADDING_SPACE)``.  To pad exactly that many
    spaces we need to know how many characters are already on the row.

    The walk is O(pieces-since-last-newline) and runs at most once per
    same-row sibling join (which itself is rare \u2014 only triggered when
    the visual x-gap exceeds COLUMN_GAP_PAD_MIN_PT), so the cost is
    negligible compared to the rest of the extraction pipeline.
    """
    total = 0
    for piece in reversed(output_pieces):
        nl_idx = piece.rfind("\n")
        if nl_idx != -1:
            # Everything after this newline counts towards the current row.
            total += len(piece) - nl_idx - 1
            return total
        total += len(piece)
    # No newline found in any prior piece \u2014 every char counts.
    return total


# ---------------------------------------------------------------------------
# Cross-block line merging
# ---------------------------------------------------------------------------
# Some PDFs (notably DE Grants-in-Aid bills) split a single tabular page
# into multiple text blocks: Block 0 has line numbers and dollar amounts,
# Block 1 has the organisation names at the same y-coordinates.  When the
# blocks are processed sequentially, the org names appear after the dollar
# amounts \u2014 destroying the tabular layout.
#
# ``_merge_overlapping_blocks`` detects pairs of text blocks whose
# y-ranges overlap and folds the smaller block's lines into the larger one,
# interleaved by y-coordinate.  The existing same-row detector
# (``SAME_ROW_Y_TOL_PT``) and column-gap padding then correctly place the
# org name next to its dollar amount on the same output line.

def _merge_overlapping_blocks(text_dict: dict) -> dict:
    """Merge text blocks that share overlapping y-ranges.

    For each pair of text blocks whose vertical extents overlap, the
    smaller block's lines are inserted into the larger block's line
    list at the correct y-sorted position, and the smaller block is
    removed.  Non-text blocks (images) are left untouched.

    Returns the (possibly mutated) *text_dict*.
    """
    blocks = text_dict.get("blocks", [])
    if len(blocks) < 2:
        return text_dict

    # Collect text-block indices with their y-ranges
    text_blocks: list[tuple[int, float, float, int]] = []
    # (index_in_blocks, y_min, y_max, line_count)
    for i, blk in enumerate(blocks):
        if blk.get("type") != 0:
            continue
        lines = blk.get("lines", [])
        if not lines:
            continue
        bb = blk.get("bbox", (0, 0, 0, 0))
        text_blocks.append((i, bb[1], bb[3], len(lines)))

    if len(text_blocks) < 2:
        return text_dict

    # Sort by line count descending \u2014 we merge smaller blocks INTO larger
    text_blocks.sort(key=lambda t: -t[3])

    merged_indices: set[int] = set()

    for idx_a in range(len(text_blocks)):
        i_a, ya_min, ya_max, cnt_a = text_blocks[idx_a]
        if i_a in merged_indices:
            continue
        for idx_b in range(idx_a + 1, len(text_blocks)):
            i_b, yb_min, yb_max, cnt_b = text_blocks[idx_b]
            if i_b in merged_indices:
                continue
            # Check y-range overlap
            overlap = min(ya_max, yb_max) - max(ya_min, yb_min)
            if overlap <= 0:
                continue
            # The blocks overlap.  Merge B's lines into A.
            host_lines = blocks[i_a].get("lines", [])
            guest_lines = blocks[i_b].get("lines", [])

            # Interleave by y-midpoint
            combined = []
            for ln in host_lines:
                lb = ln.get("bbox", (0, 0, 0, 0))
                combined.append((lb[1], lb[0], ln))
            for ln in guest_lines:
                lb = ln.get("bbox", (0, 0, 0, 0))
                combined.append((lb[1], lb[0], ln))
            # Sort by y first, then x for same-row
            combined.sort(key=lambda t: (t[0], t[1]))
            blocks[i_a]["lines"] = [c[2] for c in combined]

            # Update the host bbox to encompass both blocks
            bb_a = list(blocks[i_a].get("bbox", (0, 0, 0, 0)))
            bb_b = blocks[i_b].get("bbox", (0, 0, 0, 0))
            bb_a[0] = min(bb_a[0], bb_b[0])
            bb_a[1] = min(bb_a[1], bb_b[1])
            bb_a[2] = max(bb_a[2], bb_b[2])
            bb_a[3] = max(bb_a[3], bb_b[3])
            blocks[i_a]["bbox"] = tuple(bb_a)

            # Update ya_max for further overlap checks
            ya_max = bb_a[3]

            merged_indices.add(i_b)
            logger.debug(
                "Merged block %d (%d lines) into block %d (%d lines) \u2014 "
                "y-overlap %.1fpt",
                i_b, cnt_b, i_a, cnt_a, overlap,
            )

    # Remove merged (absorbed) blocks
    if merged_indices:
        text_dict["blocks"] = [
            blk for i, blk in enumerate(blocks) if i not in merged_indices
        ]

    return text_dict


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
    state_code: Optional[str] = None,
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

    # -- 1b. CT-only: PRE-STRIP footer/header separator rules by stroke width.
    # This runs FIRST — before table-border filtering and everything else —
    # so downstream code never sees the thick separator drawings at all.
    # CT footer separators use ~1.44 pt stroke vs 0.72 pt for underlines.
    if state_code and state_code.upper() == "CT":
        hlines = _strip_ct_footer_separator_hlines(hlines, page)

    # -- 1c. Filter out table-border lines to avoid false underline tags --
    # EXCEPTION: DE and IN bills use tabular layouts where the dollar-
    # column band is misidentified as a "table" by find_tables().  The
    # border filter then strips legitimate underline hlines that happen
    # to sit near a zone edge (the "DE HB 230 page-15/17" bug).  Skip
    # the border filter for these states — their underlines are genuine
    # decorations, not table borders.
    _skip_table_filter = (
        state_code is not None
        and state_code.upper() in ("DE", "IN")
    )
    table_zones = _detect_table_zones(page)
    if table_zones and not _skip_table_filter:
        hlines = _filter_table_border_hlines(hlines, table_zones)

    # -- 1d. CT-only: legacy multi-segment / text-above separator filter.
    # Most CT separators are already removed by the stroke-width filter
    # above; this catches any remaining edge cases (e.g. title-block
    # decorative rules with normal stroke width but multi-segment shape).
    if state_code and state_code.upper() == "CT":
        hlines = _filter_page_separator_hlines(hlines, page)

    # -- 1d. AR-only: filter typographic leader / decorative rules --
    # AR appropriation bills draw thin rules running across whitespace
    # between columns (TOC-style leaders + dollar-column underrules).
    # These incidentally clip a few characters at one end and get
    # misread as underlines, producing false [A>2<A] tags on isolated
    # digits.  Real legislative underlines hug the text width;
    # leaders extend mostly through whitespace.  See
    # _filter_ar_leader_lines for the geometric discriminator.
    if state_code and state_code.upper() == "AR":
        hlines = _filter_ar_leader_lines(hlines, page)

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

    # -- 2b. Merge overlapping blocks --
    # Some PDFs (notably DE Grants-in-Aid bills) split tabular rows
    # across multiple text blocks: one block has line numbers and dollar
    # amounts, another has organization names.  Both share the same
    # y-coordinates but sit in different blocks.  Processing blocks
    # sequentially would emit the org names AFTER the amounts, losing
    # the tabular association.
    #
    # Fix: find pairs of text blocks whose y-ranges overlap and merge
    # the smaller block's lines into the larger block, interleaved by
    # y-coordinate.  This lets the existing same-row detection
    # (SAME_ROW_Y_TOL_PT) and column-gap padding correctly place the
    # org names next to their amounts.
    text_dict = _merge_overlapping_blocks(text_dict)

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
        # NOTE: sort lines into visual reading order FIRST so that tabular
        # rows authored as "<number> then <$>" (with the $ positioned to
        # the LEFT of the number) emit as "$ <number>" rather than
        # "<number> $".  See _sort_block_lines_visually() for details.
        block_lines = _sort_block_lines_visually(block.get("lines", []))
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
        prev_line_x1:    Optional[float] = None
        prev_row_had_column_gap = False  # tabular-row tracker

        # ── Pre-scan: identify bare-number lines that sit on the same
        # physical row as a non-numeric neighbor (e.g. vote tallies
        # "Yea  41  Nay  0").  These are INLINE numbers, not margin
        # line-number markers, and must NOT be skipped.
        _inline_number_indices: set = set()
        for _i, _ln in enumerate(block_lines):
            _lbb = _ln.get("bbox", (0, 0, 0, 0))
            _lw = _lbb[2] - _lbb[0]
            if _lw >= LINE_NUMBER_MAX_WIDTH_PT:
                continue
            _ltxt = ""
            for _s in _ln.get("spans", []):
                _cd = _s.get("chars", [])
                if _cd:
                    _ltxt += "".join(c.get("c", "") for c in _cd)
                else:
                    _ltxt += _s.get("text", "")
            if not _RE_BARE_LINE_NUMBER.match(_ltxt):
                continue
            # This is a narrow bare-number line — check if any other
            # line on the same physical row is NOT a bare number
            _ym = (_lbb[1] + _lbb[3]) / 2
            for _j, _ln2 in enumerate(block_lines):
                if _j == _i:
                    continue
                _lbb2 = _ln2.get("bbox", (0, 0, 0, 0))
                _ym2 = (_lbb2[1] + _lbb2[3]) / 2
                if abs(_ym - _ym2) > SAME_ROW_Y_TOL_PT:
                    continue
                # Same row — check if neighbor is non-numeric
                _ltxt2 = ""
                for _s2 in _ln2.get("spans", []):
                    _cd2 = _s2.get("chars", [])
                    if _cd2:
                        _ltxt2 += "".join(c.get("c", "") for c in _cd2)
                    else:
                        _ltxt2 += _s2.get("text", "")
                if not _RE_BARE_LINE_NUMBER.match(_ltxt2):
                    _inline_number_indices.add(_i)
                    break

        for _line_idx, line in enumerate(block_lines):
            lbbox_full = line.get("bbox", (0, 0, 0, 0))
            line_x0 = lbbox_full[0]
            line_x1 = lbbox_full[2]
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
            if (_line_idx not in _inline_number_indices
                    and line_width < LINE_NUMBER_MAX_WIDTH_PT
                    and (_RE_CA_LINE_MARKER.match(line_raw_text)
                         or (_RE_BARE_LINE_NUMBER.match(line_raw_text)
                             and continuation_x is not None
                             and line_x0 < continuation_x))):
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
                    # Close tags + emit a joiner.  The downstream
                    # `consolidate_adjacent_tags` (Phase 1a) will merge
                    # the resulting "<T] [T>" pair into one tag span.
                    if in_add:
                        output.append(ADD_CLOSE); in_add = False
                    if in_del:
                        output.append(DEL_CLOSE); in_del = False
                    # Position-based column padding.  When the visual
                    # x-gap exceeds COLUMN_GAP_PAD_MIN_PT this is a
                    # tabular column gap (e.g. DE Grants-in-Aid: 310pt
                    # between "Absalom Jones" and the dollar column).
                    # We compute the TARGET output column from the
                    # next sibling's PDF x-position and pad so it lands
                    # there — giving every row in the table the same
                    # column for its data cells, regardless of label
                    # width.  Smaller gaps keep the single-space joiner
                    # so AR / CT / CA tabular bills (whose authoring
                    # tool already drew spacer lines) remain
                    # byte-identical.
                    gap_pt = (
                        line_x0 - prev_line_x1
                        if prev_line_x1 is not None else 0.0
                    )
                    if gap_pt > COLUMN_GAP_PAD_MIN_PT:
                        target_col = int(round(
                            line_x0 / POINTS_PER_PADDING_SPACE
                        ))
                        current_col = _count_chars_since_last_newline(output)
                        pad_n = target_col - current_col
                        pad_n = max(
                            COLUMN_GAP_MIN_SPACES,
                            min(pad_n, COLUMN_GAP_MAX_SPACES),
                        )
                        output.append(" " * pad_n)
                        prev_row_had_column_gap = True  # mark as tabular row
                    else:
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
                    # Force paragraph break after tabular rows so the
                    # tag merger (_RE_MERGE_ADD cross-line pattern)
                    # does NOT collapse consecutive table rows into a
                    # single run-on tag.  The merger respects \n\n as
                    # a paragraph boundary and won't merge across it.
                    if prev_row_had_column_gap:
                        is_para_start = True
                        prev_row_had_column_gap = False
                    # Close tags before the break — they will be reopened
                    # if the decoration continues on the next line.
                    if in_add:
                        output.append(ADD_CLOSE); in_add = False
                    if in_del:
                        output.append(DEL_CLOSE); in_del = False
                    output.append("\n\n" if is_para_start else "\n")
            first_line_in_block = False
            prev_line_y_mid = line_y_mid
            prev_line_x1    = line_x1

            # --- Skip ALL decoration tagging for lines inside a table zone ---
            # Table cell borders are often short horizontal lines that slip
            # past the width-based hline filter but still trigger false
            # underline detection.  Suppress tagging entirely for table text.
            #
            # EXCEPTION: DE and IN — see ``_skip_table_filter`` above.
            # The same false-positive table zones that caused border-
            # filter removal also trigger blanket tagging suppression.
            # Both guards are disabled together for these states.
            line_in_table = False
            if table_zones and not _skip_table_filter:
                lbbox = line.get("bbox", (0, 0, 0, 0))
                for zx0, zy0, zx1, zy1 in table_zones:
                    if (lbbox[1] >= zy0 - 2 and lbbox[3] <= zy1 + 2 and
                        lbbox[0] >= zx0 - 5 and lbbox[2] <= zx1 + 5):
                        line_in_table = True
                        break

            # --- Line-level metrics for superscript detection ---
            # Computed once per line and reused for every span below so we
            # only have to walk the spans-by-size once.  See
            # SUPERSCRIPT_SIZE_RATIO / SUPERSCRIPT_RAISE_PT for thresholds.
            line_bbox_for_super  = line.get("bbox", (0, 0, 0, 0))
            line_dominant_size   = _line_dominant_font_size(line)

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

                # --- Detect superscript footnote reference ---
                # When a span's font is markedly smaller than the line's
                # dominant size AND its bbox sits raised above the line
                # baseline, treat it as a footnote-marker superscript.
                # Such spans are emitted as Unicode superscripts (¹²³…)
                # and are excluded from underline / italic / bold tagging.
                span_is_superscript = _span_is_superscript(
                    span, line_dominant_size, line_bbox_for_super,
                )

                # --- Fallback: no char-level data → use span bbox ---
                if not chars_data:
                    if span_text:
                        if span_is_smallcaps:
                            span_text = span_text.upper()
                        # Superscript footnote ref: convert to Unicode
                        # superscripts and skip all decoration tagging.
                        if span_is_superscript:
                            if in_add:
                                output.append(ADD_CLOSE); in_add = False
                            if in_del:
                                output.append(DEL_CLOSE); in_del = False
                            output.append(_to_superscript(span_text))
                            continue
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

                    # Superscript footnote ref: emit as Unicode superscript
                    # and skip all decoration tagging.  Close any open tag
                    # before the marker (it will reopen on the next char
                    # if the surrounding decoration continues).
                    if span_is_superscript:
                        if in_add:
                            output.append(ADD_CLOSE); in_add = False
                        if in_del:
                            output.append(DEL_CLOSE); in_del = False
                        output.append(_to_superscript(c))
                        continue

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

    result_text = "".join(output)

    # ─── DC-only: recover chairman signature line from raster image ───
    # DC SBOE bills bake the chairman's signature + "Chairman Phil
    # Mendelson" into a raster image block on the bill page, sitting
    # just above the "at the request of the State Board of Education"
    # text.  Native text extraction can't see image content, so the
    # chairman line is otherwise lost.  The helper OCRs the image
    # region (best-effort — silently no-ops if Tesseract is absent or
    # no matching layout is found) and splices the recovered line into
    # the output just above the anchor text.
    if state_code and state_code.upper() == "DC":
        chairman_line = _recover_dc_chairman_from_image(page)
        if chairman_line:
            result_text = _splice_dc_chairman_line(result_text, chairman_line)
            logger.debug(
                "DC: recovered chairman line via OCR — %r", chairman_line
            )

    return result_text


# ---------------------------------------------------------------------------
# Span-level extraction (kept for summary / diagnostics)
# ---------------------------------------------------------------------------

def _extract_page_spans(page: fitz.Page, state_code: Optional[str] = None) -> List[TextSpan]:
    """
    Span-level extraction — used only for ``extract_pdf_summary()``.
    """
    try:
        ul_rects, so_rects = _extract_annot_rects(page)
    except Exception:
        ul_rects, so_rects = [], []

    hlines = _extract_hlines_from_drawings(page)

    # CT-only: PRE-STRIP footer/header separator rules by stroke width
    if state_code and state_code.upper() == "CT":
        hlines = _strip_ct_footer_separator_hlines(hlines, page)

    # Filter out table-border lines
    table_zones = _detect_table_zones(page)
    if table_zones:
        hlines = _filter_table_border_hlines(hlines, table_zones)

    # CT-only: legacy multi-segment separator filter (catches edge cases)
    if state_code and state_code.upper() == "CT":
        hlines = _filter_page_separator_hlines(hlines, page)

    # AR-only: filter typographic leader / decorative rules
    if state_code and state_code.upper() == "AR":
        hlines = _filter_ar_leader_lines(hlines, page)

    text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
    spans: List[TextSpan] = []
    block_idx = 0

    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:
            continue
        # Mirror the visual-order sort applied in _extract_page_tagged_text
        # so summary spans (used for diagnostics + Acrobat overlay) match the
        # left-to-right reading order of the emitted text.
        for line_idx, line in enumerate(
            _sort_block_lines_visually(block.get("lines", []))
        ):
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
# pattern must NOT eat:
#   • a paragraph break (\n\n) — two italic paragraphs that happen to sit
#     adjacent in a digest must remain separate, not be welded together
#     into one giant [A>…<A] span.
#   • a single \n that precedes a fresh list-marker tag like [A>(2)<A],
#     [A>(F)<A], [A>(iv)<A].  In that case the \n is a real list-item
#     transition (e.g. "(F) body of F.<A]\n[A>(2)<A] body of 2…") and
#     merging would weld two unrelated list items into one.  This is the
#     pattern that emerges in Kansas amendment text where each list item
#     marker is typeset in italic by the bill author.
#
# Matches: "<A] [A>", "<A]\t[A>"            (inline — same physical row)
#          "<A]\n[A>foo<A]"                  (cross-line, body text continues)
# Rejects: "<A]\n\n[A>"                     (paragraph break)
#          "<A]\n[A>(2)<A]" / "<A]\n[A>(F)<A]" (cross-line, fresh list item)
_RE_MERGE_ADD = re.compile(
    r"<A\]"
    r"(?:"
    r"[ \t]{1,5}\[A>"                                      # inline (same row, ≤5 spaces)
    r"|"
    r"[ \t]*\n[ \t]*\[A>(?!\([A-Za-z0-9]{1,6}\))"        # cross-line, NOT list marker
    r")"
)
_RE_MERGE_DEL = re.compile(
    r"<D\]"
    r"(?:"
    r"[ \t]{1,5}\[D>"                                      # inline (same row, ≤5 spaces)
    r"|"
    r"[ \t]*\n[ \t]*\[D>(?!\([A-Za-z0-9]{1,6}\))"
    r")"
)
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

# Currency right-alignment padding: a "$" followed by 2+ whitespace
# characters before a digit.  PDF authoring tools draw "$" and the
# number as separate text runs and pad the gap so the column right-
# aligns; once the row is re-flowed into a single text stream, that
# padding becomes meaningless noise inside tagged amounts like
# ``[D>$          236,308<D]``.  This regex normalises any such run
# to a single space ("$ 236,308").
#
# Safe by construction:
#   * "$NNN"        — no whitespace, no match
#   * "$ NNN"       — single space, no match (requires 2+ to fire)
#   * "$  NNN"      — 2+ spaces, collapsed to "$ NNN"
#   * "$ - 5,000"   — char after spaces is "-", no match
#   * "money$ tree" — char after spaces is "t", no match
#
# Only sequences of regular space + tab are eaten; newlines are
# excluded so a "$" at end-of-line stays separated from the digit
# on the next line.
_RE_CURRENCY_PADDING = re.compile(r"(\$)[ \t]{2,}(\d)")


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
    # Collapse right-alignment padding inside currency: "$<2+ spaces>NNN"
    # → "$ NNN".  Runs at this point (BEFORE the generic 2-3 space
    # collapse below) so it also catches "$<4+ spaces>NNN" which the
    # general collapse intentionally preserves for column layout.
    text = _RE_CURRENCY_PADDING.sub(r"\1 \2", text)
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
    r"\s*(?:/\s*)?File\s+No\.\s*\d+"
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
# CT false-underline heading removal (bill analysis section headings)
# ---------------------------------------------------------------------------
# In the bill analysis / fiscal note section at the end of CT bills,
# section headings (e.g. "VICTIM NOTIFICATION", "COMMITTEE ACTION") are
# printed with a decorative underline.  The underline detector picks
# these up as [A>...<A] tags, but they are NOT legislative additions.
#
# Detection: standalone line containing ONLY [A>TEXT IN ALL CAPS<A].
_RE_CT_FALSE_HEADING_TAG = re.compile(
    r"^\[A>([A-Z][A-Z ]{2,})<A\]$",
    re.MULTILINE,
)


def _strip_ct_false_underline_headings(text: str) -> str:
    """Remove false ``[A>…<A]`` tags from CT bill analysis section headings.

    These are all-caps headings (e.g. "VICTIM NOTIFICATION",
    "COMMITTEE ACTION") with decorative underlines that the detector
    misidentifies as legislative additions.  They appear on standalone
    lines in the bill analysis section at the end of the document.

    Only strips tags when the ENTIRE line content is uppercase/spaces
    (3+ chars) to avoid false-positive removal of legitimate short
    additions.
    """
    new_text = _RE_CT_FALSE_HEADING_TAG.sub(r"\1", text)
    if new_text != text:
        logger.debug("Stripped false [A>] tags from CT all-caps headings")
    return new_text


# ---------------------------------------------------------------------------
# Mid-word page-break false-underline fix
# ---------------------------------------------------------------------------
# When a word spans a page break and the underline is detected only on the
# continuation page, the PyMuPDF extractor emits e.g. ``e[A>stimated`` or
# ``a[A>ny injuries``.  This is a false tag: the underline on the next page
# is just the continuation of the same formatting that was on the previous
# page (or a decorative line near the footer).
#
# Pattern: lowercase_letter + [A> + lowercase_continuation (same word)
# Fix: strip the [A> opener and the matching <A] closer so the word is whole.
_RE_MIDWORD_PAGE_BREAK_TAG = re.compile(
    r"([a-z])"           # Group 1: letter before the tag (end of prev page)
    r"\[A>"
    r"([a-z]+)"          # Group 2: lowercase continuation (same word)
)


def _fix_midword_page_break_tags(text: str) -> str:
    """Fix false ``[A>`` tags that start mid-word due to page breaks.

    When a word straddles a page boundary and the underline is only
    detected on the continuation page, the extractor emits patterns like
    ``e[A>stimated annual fringe benefits cost...<A]`` where the tag starts
    inside a word.

    Detection: a lowercase letter immediately before ``[A>`` followed by
    a lowercase continuation (no space) — this is ALWAYS a mid-word split
    because legitimate tags after a lowercase letter always have punctuation
    or space before the tag content (e.g. ``54-91c[A>, as amended...``).

    Fix: for each mid-word hit, find the matching ``<A]`` closer and strip
    both tag markers, making the word whole again.
    """
    # Find all mid-word tag starts
    result = text
    offset = 0
    matches = list(_RE_MIDWORD_PAGE_BREAK_TAG.finditer(text))
    if not matches:
        return text

    fix_count = 0
    for m in matches:
        # Verify this isn't inside a table cell or legitimate tag
        # by checking the tag content doesn't start with a comma/space
        # (already guaranteed by regex: group 2 is [a-z]+)
        # Find the matching <A] closer
        tag_start = m.start() + 1  # position of '[' in [A>
        # Search for <A] after the tag opener
        close_pos = result.find('<A]', tag_start + offset + 3)
        if close_pos < 0:
            continue

        # Check: the content between opener and closer shouldn't contain
        # another [A> or [D> (would mean nested/different tag)
        content_between = result[tag_start + offset + 3:close_pos + offset]
        if '[A>' in content_between or '[D>' in content_between:
            continue

        fix_count += 1

    # Apply all fixes at once using a simpler approach:
    # Replace each "letter[A>...content...<A]" with "letter + content"
    if fix_count == 0:
        return text

    def _replace_midword(m_inner):
        # m_inner matches the full pattern from the letter through to content
        # But we only have the opener here; we need to find/strip the closer
        return m_inner.group(1) + m_inner.group(2)

    # Two-pass: first replace the openers, then clean up orphaned <A] closers.
    # Actually, let's do it properly with a single scan.
    parts = []
    last_end = 0
    fixed = 0
    for m in _RE_MIDWORD_PAGE_BREAK_TAG.finditer(text):
        # Position of [A> in the original text
        open_pos = m.start() + 1  # the '[' of [A>
        # Find the matching <A] after this opener
        close_pos = text.find('<A]', open_pos + 3)
        if close_pos < 0:
            continue
        # Check no nested tags between
        segment = text[open_pos + 3:close_pos]
        if '[A>' in segment or '[D>' in segment:
            continue

        # Build replacement: letter + tag_content (without markers)
        parts.append(text[last_end:m.start()])
        tag_content = text[open_pos + 3:close_pos]
        parts.append(m.group(1) + tag_content)  # letter + rest of word + content
        last_end = close_pos + 3  # skip past <A]
        fixed += 1

    if fixed == 0:
        return text

    parts.append(text[last_end:])
    logger.debug(f"Fixed {fixed} mid-word page-break false [A>] tag(s)")
    return ''.join(parts)


# ---------------------------------------------------------------------------
# CT raw footer-line tag stripping (PRE-LLM)
# ---------------------------------------------------------------------------
# In CT bill PDFs, a horizontal rule separates page content from the footer.
# PyMuPDF sometimes interprets this rule as an underline on the last content
# line before the page break, causing the ENTIRE line to be wrapped in
# [A>…<A] tags — even though the text is not actually underlined.
#
# This must be fixed BEFORE the LLM sees the text, because:
#   1. The false full-line tag confuses the LLM about what is truly added text
#   2. The LLM cannot distinguish footer-line artifacts from legitimate tags
#   3. After LLM processing, page markers are removed so we lose the signal
#
# Detection (all must be true):
#   1. The line is the last numbered content line before ``--- Page N ---``
#   2. ``[A>…<A]`` wraps the entire line content (starts at beginning or mid-word)
#   3. There is a trailing space before ``<A]`` — the signature of the footer
#      line extending to the full page margin width
#
# Fix: strip the [A> opener and ` <A]` closer, preserving the text content.

_RE_CT_RAW_FOOTER_TAG_LINE = re.compile(
    r'^(\d+\s+)(\w*)\[A>(.+?) <A\]\s*$'
)
_RE_CT_PAGE_MARKER = re.compile(r'^---\s*Page\s+\d+\s*---$')


def _strip_ct_raw_footer_line_tags(text: str) -> str:
    """Strip false ``[A>…<A]`` tags caused by footer separator lines in raw CT text.

    In CT bill PDFs, the footer separator line (horizontal rule) is detected
    by PyMuPDF as an underline on the last content line before the page break.
    This causes the entire last line to be falsely wrapped in ``[A>...<A]`` tags.

    The signature of these false tags is:
      1. Last numbered content line before ``--- Page N ---``
      2. ``[A>...<A]`` wraps the entire line content (or starts mid-word)
      3. A trailing space before ``<A]`` (footer line extends to margin width)

    This function operates on the RAW text BEFORE LLM processing, preventing
    the LLM from being confused by incorrect markup.
    """
    lines = text.split('\n')
    fixed = 0

    for i, line in enumerate(lines):
        if not _RE_CT_PAGE_MARKER.match(line.strip()):
            continue
        # Found a page marker — walk backwards to find last numbered line
        for j in range(i - 1, max(-1, i - 6), -1):
            m = _RE_CT_RAW_FOOTER_TAG_LINE.match(lines[j])
            if m:
                # Found a false footer-line tag — strip it
                line_num_part = m.group(1)   # e.g. "78 "
                pre_chars = m.group(2)       # e.g. "" or "p" (mid-word)
                content = m.group(3)         # tagged text (without trailing space)
                lines[j] = f"{line_num_part}{pre_chars}{content}"
                fixed += 1
                break
            # If we hit a non-empty line that's not a match, stop searching
            if lines[j].strip():
                break

    if fixed:
        logger.debug(
            f"Stripped {fixed} false raw footer-line tag(s) (pre-LLM)"
        )
        return '\n'.join(lines)
    return text


# ---------------------------------------------------------------------------
# CT false footer-line underline removal
# ---------------------------------------------------------------------------
# The horizontal rule separating page content from the footer (e.g. the line
# above "sHB5564 / File No. 633    8") is sometimes detected as an underline
# on the last text line before the footer.  After the LLM merges paragraphs
# across page breaks, this produces a false ``[A>…<A]`` tag that covers only
# the portion of text that was on the previous page — the sentence continues
# UNTAGGED after the closing ``<A]``.
#
# Detection (all must be true):
#   1. ``[A>…<A]`` starts at a paragraph boundary (after ``\n\n`` or SOF)
#   2. Content is 40+ chars (not a short insertion)
#   3. ``<A]`` is immediately followed by SPACE + lowercase letter (sentence
#      continues untagged)
#   4. BOTH the previous and next paragraphs contain NO ``[A>`` tags — i.e.
#      the tagged paragraph is isolated (surrounded by plain/existing text).
#      Legitimate additions always appear in blocks where adjacent paragraphs
#      also contain tags.
#
# Fix: strip the ``[A>`` opener and ``<A]`` closer, preserving the text.
_RE_FALSE_FOOTER_UNDERLINE = re.compile(
    r"(?:(?<=\n\n)|(?<=\A))\[A>([^\n]{40,}?)<A\](?= [a-z])"
)


def _strip_false_footer_line_underline(text: str) -> str:
    """Strip false ``[A>…<A]`` tags caused by footer separator lines.

    The footer's horizontal rule is detected as an underline on the last
    content line before the page break.  After the LLM joins paragraphs
    across pages, the result is a tag that ends mid-sentence with the
    sentence continuing untagged.

    Only strips when the tagged paragraph is SURROUNDED by untagged
    paragraphs (no ``[A>`` in prev/next) — legitimate additions always
    appear in blocks where adjacent paragraphs also contain tags.

    Two passes:
      Pass 1 — Original regex: ``[A>…<A] lowercase`` (sentence continues
               on the same line, no page break between).
      Pass 2 — Paragraph-level: paragraph ends with ``<A]`` AND contains
               a single ``[A>…<A]`` tag of 30+ chars AND both real
               neighbouring content paragraphs (skipping page markers /
               headers) are untagged.  This catches tags before page
               breaks where ``--- Page N ---`` separates tag from the
               sentence continuation.
    """
    _RE_PAGE_MARKER = re.compile(
        r'^---\s*Page\s+\d+\s*---$|'            # --- Page N ---
        r'^s[SH]B\d+\s*/?\s*File\s+No\.\s*\d+', # sSB294 / File No. 615  10
        re.IGNORECASE,
    )

    def _is_noise_para(p: str) -> bool:
        """True for empty/whitespace-only or page-marker paragraphs."""
        stripped = p.strip()
        if not stripped:
            return True
        if _RE_PAGE_MARKER.match(stripped):
            return True
        return False

    paras = text.split('\n\n')
    if len(paras) <= 2:
        return text

    # Build para index for fast lookup
    para_offsets: list[int] = []
    offset = 0
    for p in paras:
        para_offsets.append(offset)
        offset += len(p) + 2  # +2 for \n\n

    fixed = 0

    # --- Pass 1: regex-based (tag followed immediately by space+lowercase) ---
    for m in _RE_FALSE_FOOTER_UNDERLINE.finditer(text):
        pos = m.start()
        # Determine which paragraph this is in
        para_idx = 0
        for i, off in enumerate(para_offsets):
            if off > pos:
                break
            para_idx = i

        # Check surrounding paragraphs for tags
        prev_has_tag = '[A>' in paras[para_idx - 1] if para_idx > 0 else True
        next_idx = para_idx + 1
        next_has_tag = '[A>' in paras[next_idx] if next_idx < len(paras) else True

        if not prev_has_tag and not next_has_tag:
            # This is an isolated tagged paragraph surrounded by plain text
            # → false footer-line underline. Strip the tag markers.
            tagged_content = m.group(1)
            paras[para_idx] = paras[para_idx].replace(
                f'[A>{tagged_content}<A]', tagged_content, 1
            )
            fixed += 1

    # --- Pass 2: paragraph-level (tag ends at paragraph boundary, page break follows) ---
    # Regex: matches a paragraph that is a SINGLE [A>...<A] tag.
    # The content group explicitly excludes <A] to prevent non-greedy
    # backtracking from matching across multiple tag segments.
    _RE_SINGLE_TAG_PARA = re.compile(
        r'^(\d+\s+)?\[A>((?:(?!<A\]).){30,})<A\]\s*$'
    )
    for idx, para in enumerate(paras):
        if '[A>' not in para:
            continue
        # Skip paragraphs with multiple [A>...<A] segments — these are
        # legitimate multi-segment additions, not footer-line artifacts
        if para.count('[A>') > 1:
            continue
        m2 = _RE_SINGLE_TAG_PARA.match(para)
        if not m2:
            continue
        # Already fixed in pass 1?
        if '[A>' not in paras[idx]:
            continue
        # Find real previous content paragraph (skip noise)
        prev_real_idx = idx - 1
        while prev_real_idx >= 0 and _is_noise_para(paras[prev_real_idx]):
            prev_real_idx -= 1
        # Find real next content paragraph (skip noise)
        next_real_idx = idx + 1
        while next_real_idx < len(paras) and _is_noise_para(paras[next_real_idx]):
            next_real_idx += 1

        prev_has_tag = '[A>' in paras[prev_real_idx] if prev_real_idx >= 0 else True
        next_has_tag = (
            '[A>' in paras[next_real_idx] if next_real_idx < len(paras) else True
        )

        if not prev_has_tag and not next_has_tag:
            tagged_content = m2.group(2)
            # Safety check: if tagged content ends with sentence-ending
            # punctuation, it's likely a complete clause (legitimate
            # addition), not a mid-sentence fragment from a page break.
            content_stripped = tagged_content.rstrip()
            if content_stripped and content_stripped[-1] in '.;:!?)':
                continue
            prefix = m2.group(1) or ''
            paras[idx] = f'{prefix}{tagged_content}'
            fixed += 1

    if fixed:
        logger.debug(
            f"Stripped {fixed} false footer-line underline tag(s)"
        )
        return '\n\n'.join(paras)
    return text


# ---------------------------------------------------------------------------
# CT analysis/fiscal-note section tag stripping
# ---------------------------------------------------------------------------
# CT bills end with explanatory sections (Statement of Legislative
# Commissioners, OFA Fiscal Note, OLR Bill Analysis, COMMITTEE ACTION)
# that are NOT legislation.  These sections never have underline/strike
# markup, yet page-break footer-line detection can produce false [A>…<A]
# tags there.  Stripping ALL tags from these sections is safe.
#
# Additionally, CT bills end with an "effective date" table:
#   "This act shall take effect as follows and shall amend the following
#    sections:"
# In Substitute Bills the entire table is underlined (formatting artifact),
# producing false [A>…<A] tags.  This table is administrative metadata, not
# legislative text, so tags are always false.
_CT_ANALYSIS_SECTION_MARKERS = (
    "This act shall take effect as follows",
    "Statement of Legislative Commissioners",
    "OFA Fiscal Note",
    "OLR Bill Analysis",
)


def _strip_ct_analysis_section_tags(text: str) -> str:
    """Remove all ``[A>…<A]`` and ``[D>…<D]`` tags from CT analysis sections.

    CT bills end with explanatory sections (fiscal note, bill analysis,
    committee action) that never have legislative markup.  Any tags in
    these sections are artifacts of page-break underline detection.
    """
    # Find the earliest analysis-section marker
    earliest_pos = -1
    for marker in _CT_ANALYSIS_SECTION_MARKERS:
        pos = text.find(marker)
        if pos >= 0 and (earliest_pos < 0 or pos < earliest_pos):
            earliest_pos = pos

    if earliest_pos < 0:
        return text  # No analysis section found

    # Split text at the marker boundary
    legislation = text[:earliest_pos]
    analysis = text[earliest_pos:]

    # Strip all tags from the analysis section
    cleaned_analysis = re.sub(r'\[A>(.*?)<A\]', r'\1', analysis)
    cleaned_analysis = re.sub(r'\[D>(.*?)<D\]', r'\1', cleaned_analysis)

    if cleaned_analysis != analysis:
        tag_count = analysis.count('[A>') + analysis.count('[D>')
        logger.debug(
            f"Stripped {tag_count} tag(s) from CT analysis/fiscal section"
        )
        return legislation + cleaned_analysis
    return text


# ---------------------------------------------------------------------------
# District of Columbia (DC) letterhead-footer removal
# ---------------------------------------------------------------------------
# DC State Board of Education bills are formatted as a transmittal letter
# whose page-1 footer (a 3-line letterhead with address / phone / website /
# email / Twitter handle) sits at y≈745 on a 792-pt page.  PyMuPDF returns
# this footer as the FIRST text block on the page, so the converted text
# starts with the letterhead instead of the actual letter date.  Removing
# this block also drops the false [A>sboe.dc.gov<A] / [A>sboe@dc.gov<A]
# hyperlink tags that come with it.
#
# Real-world example (from SBOE transmittal letter for the "Universal
# Public School Nurses for Safer Students Amendment Act of 2025"):
#
#   District of Columbia State Board of Education
#   441 4th Street NW, Suites 530S & 723N | Washington, DC 20001
#
#   (202) 741-0888 | [A>sboe.dc.gov<A] | [A>sboe@dc.gov<A] | @DCSBOE
#
# We anchor the regex on "District of Columbia State Board of Education"
# so other DC bill formats remain untouched.
_RE_DC_LETTERHEAD_BLOCK = re.compile(
    r"^[ \t]*District\s+of\s+Columbia\s+State\s+Board\s+of\s+Education[ \t]*\n"
    r"(?:.*\n){0,4}?"                       # 0–4 follow-up lines (address, phone)
    r".*?@DCSBOE[ \t]*\n",                  # always ends with the @DCSBOE handle
    re.IGNORECASE,
)


def _strip_dc_letterhead_footer(text: str) -> str:
    """Remove the DC State Board of Education letterhead block.

    DC SBOE transmittal letters embed a footer-style letterhead block
    (organisation name, address, phone, website, email, ``@DCSBOE``)
    that PyMuPDF places at the top of the converted text because the
    PDF emits that block first, even though it sits at y≈745 on the
    page.  This function strips the entire block — its content is
    document metadata, not legislative language, and it should never
    appear in the final output.

    Operates on the whole document; the regex anchor makes it a no-op
    for any text that doesn't contain ``District of Columbia State
    Board of Education`` followed (within a few lines) by ``@DCSBOE``.
    """
    new_text = _RE_DC_LETTERHEAD_BLOCK.sub("", text)
    if new_text != text:
        # Tidy excessive blank lines left behind
        new_text = re.sub(r"\n{3,}", "\n\n", new_text)
        new_text = new_text.lstrip("\n")
        logger.debug("Stripped DC SBOE letterhead-footer block")
    return new_text


# ---------------------------------------------------------------------------
# DC hyperlink-underline tag stripping
# ---------------------------------------------------------------------------
# DC SBOE letters embed hyperlinks (mailto: and http(s)://) that the PDF
# renders as underlined text.  The underline detector treats those rule
# lines as "added text" decorations, producing false [A>…<A] tags around
# every email address and URL in the document.  Hyperlink underlines are
# never legislative additions, so we strip those tags whenever the tag
# content matches an email or URL shape.
#
# Patterns matched (inside a [A>…<A] tag):
#   • email:  user@host.tld         (e.g. bernice.butler@dc.gov)
#   • domain: host.tld              (e.g. sboe.dc.gov)
#   • URL:    http://… or https://…
_RE_DC_LINK_TAG = re.compile(
    r"\[A>\s*("
    r"(?:https?://\S+)"
    r"|(?:[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})"
    r"|(?:[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+){1,3}\.(?:gov|com|org|net|edu|us))"
    r")\s*<A\]"
)


def _strip_dc_link_underline_tags(text: str) -> str:
    """Strip ``[A>…<A]`` tags whose content is an email address or URL.

    DC bills underline hyperlinks in the source PDF; the underline is
    purely a hyperlink rendering convention, not a legislative addition.
    The regex above matches only tag contents whose shape is clearly an
    email, bare domain, or full URL, so genuine underlined legislative
    text containing "@" or "." (extremely rare) is unaffected.

    Scoped to DC because other states use underlined text legitimately
    for legislative additions; DC bills do not, and any underline that
    matches a hyperlink shape is always a false positive there.
    """
    new_text = _RE_DC_LINK_TAG.sub(r"\1", text)
    if new_text != text:
        logger.debug("Stripped DC hyperlink-underline [A>…<A] tags")
    return new_text


# ---------------------------------------------------------------------------
# DC chairman-signature OCR recovery
# ---------------------------------------------------------------------------
# DC SBOE bills bake the chairman's signature + printed name into a raster
# IMAGE block at the top of the bill page (typically page 2), sitting just
# above the "at the request of the State Board of Education" text line.
# PyMuPDF can extract the surrounding native text but not the image, so the
# chairman line ("Chairman Phil Mendelson") is dropped from the output
# entirely.
#
# Layout we anchor on:
#   ┌──────────────────────────────────────────────┐
#   │  [IMAGE: signature scribble + "Chairman Phil │
#   │          Mendelson" baked into one bitmap]   │
#   │  at the request of the State Board of Edu... │
#   │  A BILL                                      │
#   └──────────────────────────────────────────────┘
#
# Strategy:
#   1. Find the y-coordinate of "at the request of the State Board of
#      Education" via page.search_for().
#   2. Locate a wide image block sitting directly above it.
#   3. Run page-level OCR (PyMuPDF's get_textpage_ocr → Tesseract).
#   4. Filter OCR words by bbox-membership inside the image rect.
#   5. Group surviving words into y-bands → reconstruct lines.
#   6. Find the line beginning with "Chairman" or "airman" — the leading
#      "Ch" is often clipped by the overlapping handwritten signature
#      scribble, so we accept either prefix and normalise to "Chairman".
#
# The whole helper is best-effort: any failure (Tesseract missing, OCR
# error, no candidate image, no chairman match) becomes a no-op so the
# extractor's behaviour degrades gracefully to today's output.

# Standard Tesseract install path on Windows used elsewhere in the app
# (mirrors pdf_compare_viewer.extract_word_bboxes).
_DC_TESSERACT_DIR = r"C:\Users\C302602\AppData\Local\Programs\Tesseract-OCR"
_DC_OCR_TESSERACT_CONFIGURED = False

# Anchor for splice — matches the "at the request of the State Board of
# Education" line at the start of a line (allowing leading whitespace).
_RE_DC_REQUEST_LINE = re.compile(
    r"^([ \t]*)at\s+the\s+request\s+of\s+the\s+State\s+Board\s+of\s+Education",
    re.MULTILINE | re.IGNORECASE,
)

# Match either the canonical "Chairman" prefix or the OCR-clipped "airman"
# prefix at the start of a line.  Used both to recognise the chairman
# line in OCR output and to normalise it back to "Chairman".
_RE_DC_AIRMAN_PREFIX = re.compile(r"^(?:Ch)?airman\b", re.IGNORECASE)


def _ensure_tesseract_path() -> bool:
    """Configure Tesseract environment variables for the current process.

    Sets ``TESSDATA_PREFIX`` and prepends the Tesseract install dir to
    ``PATH`` so PyMuPDF's :func:`Page.get_textpage_ocr` can locate the
    binary and language data.  Idempotent — runs at most once per
    process.

    Returns ``True`` if a Tesseract install was located, ``False``
    otherwise (in which case OCR will not be attempted).
    """
    global _DC_OCR_TESSERACT_CONFIGURED
    if _DC_OCR_TESSERACT_CONFIGURED:
        return True
    if os.path.isdir(_DC_TESSERACT_DIR):
        os.environ.setdefault(
            "TESSDATA_PREFIX",
            os.path.join(_DC_TESSERACT_DIR, "tessdata"),
        )
        if _DC_TESSERACT_DIR not in os.environ.get("PATH", ""):
            os.environ["PATH"] = (
                _DC_TESSERACT_DIR + os.pathsep + os.environ.get("PATH", "")
            )
        _DC_OCR_TESSERACT_CONFIGURED = True
        return True
    return False


def _ocr_words_inside_bbox(
    page: "fitz.Page",
    bbox: Tuple[float, float, float, float],
    *,
    dpi: int = 300,
    y_band_pt: float = 5.0,
) -> List[str]:
    """Run OCR on *page* and return text lines reconstructed from words
    whose centres fall inside *bbox*.

    Lines are sorted top-to-bottom; words inside each line are sorted
    left-to-right.  Single-character non-letter tokens are discarded as
    OCR noise (these are typically signature-scribble artefacts).

    Returns ``[]`` on any failure (Tesseract missing, OCR error, etc.).
    """
    if not _ensure_tesseract_path():
        return []
    try:
        tp = page.get_textpage_ocr(flags=0, full=True, dpi=dpi)
        words = page.get_text("words", textpage=tp)
    except Exception as e:                                 # pragma: no cover
        logger.debug("DC OCR failed on page %s: %s", page.number, e)
        return []

    bx0, by0, bx1, by1 = bbox
    bands: Dict[int, List[Tuple[float, str]]] = {}
    for w in words:
        x0, y0, x1, y1, text = w[0], w[1], w[2], w[3], w[4]
        cx = (x0 + x1) / 2.0
        cy = (y0 + y1) / 2.0
        if not (bx0 <= cx <= bx1 and by0 <= cy <= by1):
            continue
        # Discard junk tokens (lone punctuation / single non-letter chars)
        if len(text) <= 1 and not text.isalpha():
            continue
        key = int(round(y0 / y_band_pt) * y_band_pt)
        bands.setdefault(key, []).append((x0, text))

    out_lines: List[str] = []
    for y in sorted(bands.keys()):
        line_words = [w for _, w in sorted(bands[y])]
        out_lines.append(" ".join(line_words))
    return out_lines


def _recover_dc_chairman_from_image(page: "fitz.Page") -> Optional[str]:
    """Recover the chairman-signature line from a DC SBOE raster image.

    Returns the cleaned chairman line (e.g. ``"Chairman Phil Mendelson"``)
    on success, or ``None`` if no matching pattern is found / OCR fails.

    The page must contain the literal text "at the request of the State
    Board of Education" AND a wide image block directly above that line;
    otherwise this function is a no-op.
    """
    request_hits = page.search_for("at the request of the State Board of Education")
    if not request_hits:
        return None
    request_y_top = min(r.y0 for r in request_hits)

    # Find candidate image blocks above the request line, wide & tall
    # enough to contain a printed signature line.
    raw = page.get_text("rawdict")
    candidates: List[Tuple[float, float, float, float]] = []
    for blk in raw.get("blocks", []):
        if blk.get("type") != 1:               # 1 = image block
            continue
        bbox = tuple(blk.get("bbox", (0, 0, 0, 0)))
        x0, y0, x1, y1 = bbox
        width  = x1 - x0
        height = y1 - y0
        if width < 80 or height < 20:
            continue
        if y1 > request_y_top:                 # must sit above the line
            continue
        if (request_y_top - y1) > 80:          # must be near it (≤ 80 pt)
            continue
        candidates.append(bbox)

    if not candidates:
        return None

    # Largest candidate first (signature image is the dominant one)
    candidates.sort(
        key=lambda b: (b[2] - b[0]) * (b[3] - b[1]),
        reverse=True,
    )
    sig_bbox = candidates[0]

    lines = _ocr_words_inside_bbox(page, sig_bbox, dpi=300)
    if not lines:
        return None

    # Look for a line starting with "Chairman" or "airman" (Ch clipped by
    # the overlapping handwritten signature).  Normalise to "Chairman".
    for raw_line in lines:
        line = raw_line.strip()
        if not _RE_DC_AIRMAN_PREFIX.match(line):
            continue
        return _RE_DC_AIRMAN_PREFIX.sub("Chairman", line, count=1)

    return None


def _splice_dc_chairman_line(text: str, chairman_line: str) -> str:
    """Insert *chairman_line* into *text* immediately before the first
    line containing "at the request of the State Board of Education".

    Returns *text* unchanged if no anchor match is found.  The anchor's
    own indentation is preserved on the new line.
    """
    m = _RE_DC_REQUEST_LINE.search(text)
    if not m:
        return text
    indent = m.group(1) or ""
    insert_at = m.start()
    return text[:insert_at] + f"{indent}{chairman_line} \n" + text[insert_at:]


# ---------------------------------------------------------------------------
# Kansas (KS) header tag stripping — remove false [A>…<A] from bill title
# ---------------------------------------------------------------------------
# Kansas bill PDFs have decorative italic / bold-italic header text on page 1
# that the underline/italic detector picks up, producing false [A> tags.
#
# These header elements must NEVER carry [A>…<A] tags even when they appear
# in bold/italic in the source PDF — they are document metadata, not added
# legislative language.  The regex below covers every header variant we have
# seen in real Kansas bills:
#
#   Session line ............. "Session of 2025"
#   Amendment status ......... "As Amended by House Committee"
#                              "As Amended by Senate Committee"
#   Engrossed prefix ......... "ENGROSSED HOUSE BILL No. 1204"
#                              "ENGROSSED SENATE BILL No. 13"
#   Substitute prefix ........ "SUBSTITUTE FOR HOUSE BILL No. 2207"
#                              "SUBSTITUTE FOR SENATE BILL No. 45"
#                              "HOUSE SUBSTITUTE FOR SENATE BILL No. 113"
#                              "SENATE SUBSTITUTE FOR HOUSE BILL No. 113"
#   Bill identifier .......... "HOUSE BILL No. 2207" / "SENATE BILL No. 45"
#   Resolution identifier .... "HOUSE RESOLUTION No. 5042"
#                              "SENATE RESOLUTION No. 1234"
#   Concurrent resolution .... "HOUSE CONCURRENT RESOLUTION No. 5042"
#                              "SENATE CONCURRENT RESOLUTION No. 1604"
#   Committee origin ......... "By Committee on Child Welfare and Foster Care"
#   Sponsor line ............. "By Representative Howerton"
#                              "By Representatives Smith and Jones"
#                              "By Senator Lee" / "By Senators ..."
#   Requester line ........... "Requested by Representative McDonald"
#                              "Requested by Senator …" / "Requested by Governor …"
#   Act title ................ "AN ACT concerning records; relating to..."
_RE_KS_HEADER_PATTERNS = re.compile(
    r"\[A>(.*?(?:"
    # Session line: "Session of 2025"
    r"Session\s+of\s+\d{4}"
    # Amendment status line on page 1
    r"|As\s+Amended\s+by\s+(?:House|Senate)\s+Committee"
    # Engrossed bill / resolution prefix
    r"|Engrossed\s+(?:HOUSE|SENATE)\s+(?:BILL|(?:CONCURRENT\s+)?RESOLUTION)"
    # House Substitute for Senate ... / Senate Substitute for House ...
    r"|(?:HOUSE|SENATE)\s+SUBSTITUTE\s+FOR\s+(?:HOUSE|SENATE)\s+(?:BILL|(?:CONCURRENT\s+)?RESOLUTION)"
    # Substitute for House/Senate bill or resolution
    r"|SUBSTITUTE\s+FOR\s+(?:HOUSE|SENATE)\s+(?:BILL|(?:CONCURRENT\s+)?RESOLUTION)"
    # House/Senate (Concurrent) Resolution No. XXXX
    r"|(?:HOUSE|SENATE)\s+(?:CONCURRENT\s+)?RESOLUTION(?:\s+(?:No\.?\s*)?\d+)?"
    # House/Senate Bill No. XXXX
    r"|(?:HOUSE|SENATE)\s+BILL(?:\s+(?:No\.?\s*)?\d+)?"
    # Committee origin
    r"|By\s+Committee\s+on"
    # Sponsor line
    r"|By\s+(?:Representative|Senator)s?"
    # Requester line
    r"|Requested\s+by\s+(?:Representative|Senator|Governor)"
    # Act title
    r"|AN\s+ACT\s+concerning"
    r").*?)<A\]",
    re.IGNORECASE,
)


def _strip_ks_header_tags(text: str) -> str:
    """Remove false ``[A>…<A]`` tags from KS bill header lines.

    Kansas PDFs have decorative bold / italic / bold-italic header text
    that the formatting detector picks up as added language, producing
    false ``[A>…<A]`` tags around the bill identifier, session line,
    committee line, sponsor line, etc.  These are document metadata and
    must never carry add-language tags — same treatment as
    ``"Session of 2025"`` regardless of how they are rendered in the PDF.

    This function strips those tags from header-pattern lines (keeping
    the text intact) while leaving genuine legislative tags untouched.

    Only operates on the first ~15 lines (the header area).  Body text
    further down the document is processed by the LLM clean step and
    other deterministic post-processors.
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
# Kansas (KS) resolving / enacting clause tag stripping
# ---------------------------------------------------------------------------
# Kansas bills, resolutions, and concurrent resolutions all introduce their
# operative section with a standard enacting / resolving clause.  These are
# document boilerplate, NOT added legislative language, so they MUST never
# carry [A>…<A] tags even when the source PDF renders them in italic / bold-
# italic (which is the standard typographic convention for these clauses).
#
# Variants covered:
#
#   Bill enacting clause:
#     "Be it enacted by the Legislature of the State of Kansas:"
#
#   Senate Concurrent Resolution (constitutional amendment form):
#     "Be it resolved by the Legislature of the State of Kansas, two-thirds
#      of the members elected (or appointed) and qualified to the Senate and
#      two-thirds of the members elected (or appointed) and qualified to the
#      House of Representatives concurring therein:"
#
#   House Concurrent Resolution:
#     "Be it resolved by the House of Representatives of the State of
#      Kansas, the Senate concurring therein:"
#
#   Senate Resolution (ceremonial / honorary):
#     "Be it resolved by the Senate of the State of Kansas: That ..."
#
#   House Resolution:
#     "Be it resolved by the House of Representatives of the State of
#      Kansas: That ..."
#
#   Additional resolving clauses inside resolutions:
#     "Be it further resolved: That ..."
#     "Be it further resolved by the Senate of the State of Kansas: That ..."
#
# All variants share the structure:
#     Be it [further ] (enacted | resolved) [by ...] : [body...]
#
# The regex matches an entire [A>…<A] tag whose content begins (after
# optional leading whitespace) with this clause structure, then strips the
# tag wrapper while preserving the clause text verbatim.  The character
# class [^\[\]] prevents the match from crossing into adjacent tags.
_RE_KS_RESOLVING_CLAUSE = re.compile(
    r"\[A>"                                            # opening [A>
    r"(\s*Be\s+it\s+(?:further\s+)?(?:enacted|resolved)\b"  # boilerplate start
    r"[^\[\]]*?)"                                       # content up to <A]
    r"<A\]",                                            # closing <A]
    re.IGNORECASE,
)


def _strip_ks_resolving_clause_tags(text: str) -> str:
    """Remove ``[A>…<A]`` tags wrapping Kansas enacting / resolving clauses.

    Kansas bills, resolutions, and concurrent resolutions all begin their
    operative section with a standard enacting / resolving clause::

        Be it enacted by the Legislature of the State of Kansas:
        Be it resolved by the Senate of the State of Kansas: That ...
        Be it resolved by the House of Representatives of the State of
            Kansas, the Senate concurring therein:
        Be it resolved by the Legislature of the State of Kansas,
            two-thirds of the members ... concurring therein:
        Be it further resolved: That ...
        Be it further resolved by ...: That ...

    These are document boilerplate, NOT added legislative language, so they
    must never carry ``[A>…<A]`` tags even if the source PDF renders them
    in italic or bold-italic.  This function strips the tag wrapper while
    keeping the clause text verbatim.

    Operates on the full text (resolving clauses can appear anywhere in
    the document — for example, "Be it further resolved" can be in the
    middle of a resolution — unlike header lines which are first-15-lines
    only).
    """
    n_matches = len(_RE_KS_RESOLVING_CLAUSE.findall(text))
    if n_matches == 0:
        return text
    new_text = _RE_KS_RESOLVING_CLAUSE.sub(r"\1", text)
    logger.debug(
        "Stripped [A>…<A] tags from %d KS resolving/enacting clause(s)",
        n_matches,
    )
    return new_text


# ---------------------------------------------------------------------------
# Kansas (KS) signature / certification block tag stripping
# ---------------------------------------------------------------------------
# Enrolled Kansas bills end with a certification and signing block that is
# document boilerplate — it MUST NEVER carry [A>…<A] tags even when the
# source PDF renders the text in italic or bold-italic.
#
# Typical lines:
#   "I hereby certify that the above BILL originated in the House..."
#   "Speaker of the House"
#   "President of the Senate"
#   "Chief Clerk of the House"
#   "Secretary of the Senate"
#   "APPROVED"
#   "Governor"
#   "Secretary of State"
#
# Strategy: only operate on the **tail** of the document (last ~25 lines)
# to avoid touching genuine legislative body text.  Within that tail,
# strip [A>…<A] tags from lines matching known signature-block patterns.

_KS_SIGNATURE_PATTERNS = re.compile(
    r"^\[A>"
    r"("
    # Certification statement
    r"I\s+hereby\s+certify\s+that\s+the\s+above\b.*"
    r"|"
    # Chamber officers
    r"Speaker\s+of\s+the\s+House"
    r"|President\s+of\s+the\s+Senate"
    r"|Chief\s+Clerk\s+of\s+the\s+House"
    r"|Secretary\s+of\s+the\s+Senate"
    r"|Secretary\s+of\s+State"
    r"|"
    # Governor approval
    r"APPROVED"
    r"|Governor"
    r"|Approved\s+[_,\s\d]+"
    r")"
    r"\.?\s*<A\]$",
    re.IGNORECASE | re.MULTILINE,
)


def _strip_ks_signature_block_tags(text: str) -> str:
    """Remove ``[A>…<A]`` tags from the KS certification / signing block.

    Enrolled Kansas bills end with a certification / signing block at the
    bottom.  The source PDF often renders these lines in italic, causing
    the formatting detector to wrap them with ``[A>…<A]`` tags.  These
    are document boilerplate, not added legislative language.

    This function scans **only the last ~25 lines** to avoid accidentally
    stripping genuine body tags that happen to mention "Governor" or
    similar words inside legislative text.
    """
    lines = text.split("\n")
    tail_start = max(0, len(lines) - 25)
    changed = False

    for i in range(tail_start, len(lines)):
        new_line = _KS_SIGNATURE_PATTERNS.sub(r"\1", lines[i])
        if new_line != lines[i]:
            lines[i] = new_line
            changed = True

    if changed:
        logger.debug("Stripped [A>…<A] tags from KS signature/certification block")
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

# IN sponsor/author line — appears on page 1 between the "Effective:" line
# and the "January X, 2026, read first time…" history block.  Decorative
# horizontal rules above and below the author name cause false [A>…<A]
# tagging.  The pattern matches a line whose ENTIRE content is a single
# [A>…<A] tag containing only name-like characters (letters, commas,
# spaces, periods, hyphens — no digits, no parentheses, no IC references).
# Guard: content must be 3–80 chars and start with an uppercase letter.
_RE_IN_SPONSOR_TAG = re.compile(
    r"^\[A>([A-Z][A-Za-z \t,.\-']{2,79})<A\]$"
)


def _strip_in_header_tags(text: str) -> str:
    """Remove false ``[A>…<A]`` tags from IN bill header lines.

    Indiana PDFs have decorative lines near the page-1 header that the
    underline detector picks up as underlines.  This strips those tags
    from header-pattern lines (bill title, date, digest, sponsor names)
    while leaving genuine legislative tags untouched.

    Only operates on the first ~20 lines (the header area).
    """
    lines = text.split("\n")
    header_end = min(20, len(lines))
    changed = False
    for i in range(header_end):
        new_line = _RE_IN_HEADER_PATTERNS.sub(r"\1", lines[i])
        if new_line != lines[i]:
            lines[i] = new_line
            changed = True
            continue
        # Sponsor/author line: entire line is [A>Names<A]
        m = _RE_IN_SPONSOR_TAG.match(lines[i].strip())
        if m:
            lines[i] = m.group(1)
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


# ---------------------------------------------------------------------------
# _split_in_midline_items  (IN-specific)
# ---------------------------------------------------------------------------

# Regex: "; and (N)" or "; or (N)" mid-line where (N) is a list item marker.
# The semicolon + conjunction is a very strong signal that (N) is a new
# list item, not a subsection reference.  Safe for aggressive splitting.
_RE_IN_MIDLINE_ITEM = re.compile(
    r'(;\s*(?:and|or))\s+'
    r'(\([a-zA-Z0-9]{1,3}\))'
)


def _split_in_midline_items(text: str) -> str:
    """Split numbered/lettered items that got merged mid-line in IN text.

    The LLM sometimes merges items like::

        [D>(1) passes the exam; and (2)<D] meets requirements

    into a single tagged line.  This function detects ``; and (N)`` or
    ``; or (N)`` patterns and splits them onto separate lines **with
    correct tag boundaries**::

        [D>(1) passes the exam; and<D]

        [D>(2)<D] meets requirements

    Tag-aware: if the split point falls inside a ``[A>…<A]`` or
    ``[D>…<D]`` tag, the tag is closed before the newline and re-opened
    after it.

    Scoped to IN only by callers.

    Safety: inline enumerations in prose (e.g. Synopsis text like
    ``(1) teachers; (2) police; and (3) firefighters``) are detected
    by looking for an earlier ``; (N)`` pattern on the same line — the
    semicolon before a parenthesised marker is the hallmark of an inline
    list.  If found, the ``; and (N)`` is part of a flowing sentence and
    must NOT be split.  Standalone ``(N)`` references (like
    ``subsection (b)``) are ignored because they lack the leading ``;``.
    """
    # Detects inline list markers: "; (N)" or ": (N)" where N is a
    # number/letter.  A colon-introduced marker (e.g. "entitled to:
    # (1) testify ...; and (2) compensation") is just as strong a
    # signal of an inline enumeration as a semicolon separator.
    _RE_EARLIER_ENUM_ITEM = re.compile(r'[;:]\s+\([a-zA-Z0-9]{1,3}\)')

    def _tag_aware_replace(m: re.Match) -> str:
        full = m.group(0)
        conjunction = m.group(1)   # "; and" or "; or"
        marker = m.group(2)        # "(2)"

        # Find what position this match is at in the original text
        pos = m.start()

        # --- Inline-enumeration guard ---
        # If the same line already contains an earlier "; (N)" pattern,
        # this is an inline enumeration ("(1)...; (2)...; and (3)...")
        # and must NOT be split.  The semicolon prefix distinguishes
        # inline list items from references like "subsection (b)".
        line_start = text.rfind('\n', 0, pos)
        line_before = text[line_start + 1 : pos]
        if _RE_EARLIER_ENUM_ITEM.search(line_before):
            return full   # leave the inline enumeration intact

        # Look backwards for an unclosed [A> or [D> tag
        text_before = text[:pos + len(conjunction)]
        open_tag = None
        for t in ('A', 'D'):
            opener = f'[{t}>'
            closer = f'<{t}]'
            last_open = text_before.rfind(opener)
            last_close = text_before.rfind(closer)
            if last_open > last_close:
                open_tag = t
                break

        if open_tag:
            # Split point is inside a tag — close before, re-open after
            return f'{conjunction}<{open_tag}]\n\n[{open_tag}>{marker}'
        else:
            # Not inside a tag — just insert newline
            return f'{conjunction}\n{marker}'

    result = _RE_IN_MIDLINE_ITEM.sub(_tag_aware_replace, text)
    if result != text:
        logger.debug("Split mid-line items at '; and/or (N)' boundaries in IN text")
    return result


# Regex: line that starts with a numbered/lettered item marker.
# Covers plain text and tagged versions:
#   (a), (b), (1), (2), (A), (B), (i), (ii), etc.
#   [A>(a), [D>(1), [A>(A), etc.
_RE_IN_ITEM_START = re.compile(
    r'^\s*(?:\[[AD]>)?\s*'
    r'\('
    r'(?:[a-z]|[A-Z]|\d{1,3}|[ivxlIVXL]+)'
    r'\)'
)


def _ensure_in_item_spacing(text: str) -> str:
    """Ensure blank lines before numbered/lettered items in IN output.

    The IN prompt instructs the LLM to add blank lines between items
    like ``(a)``, ``(b)``, ``(1)``, ``(2)`` etc., but the LLM doesn't
    always comply — especially for existing (untagged) text in
    amended sections.

    This deterministic pass inserts a blank line before any line that
    starts with an item marker when the preceding line is non-blank.

    Scoped to IN only by callers.
    """
    lines = text.split('\n')
    if len(lines) < 2:
        return text

    new_lines: list[str] = [lines[0]]
    changed = False

    for i in range(1, len(lines)):
        curr = lines[i]
        prev = lines[i - 1]

        # If current line starts with an item marker and previous
        # line is not blank → insert a blank line before.
        if (curr.strip()
                and prev.strip()
                and _RE_IN_ITEM_START.match(curr)):
            new_lines.append('')
            changed = True

        new_lines.append(curr)

    if changed:
        logger.debug("Inserted blank lines before IN item markers")
        return '\n'.join(new_lines)
    return text


def _strip_in_leading_indent(text: str) -> str:
    """Strip leading whitespace from IN output lines that carry tags or sub-items.

    The LLM sometimes preserves the PDF's visual indentation, producing
    lines like::

        (4) [A>in a county:<A]
                 [A>(A) without a consolidated city,<A] two (2) judges ...

    All IN output should start at the left margin::

        (4) [A>in a county:<A]
        [A>(A) without a consolidated city,<A] two (2) judges ...

    This function strips leading spaces/tabs from every non-blank line.
    Blank lines (paragraph separators) are left untouched.
    """
    lines = text.split('\n')
    changed = False
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped and stripped != line:
            lines[i] = stripped
            changed = True
    if changed:
        logger.debug("Stripped leading indentation from IN output lines")
        return '\n'.join(lines)
    return text


# ---------------------------------------------------------------------------
# Pre-LLM paragraph-aware tag merge  (all states)
# ---------------------------------------------------------------------------
# The PyMuPDF extraction produces per-LINE tags because tags are closed at
# every line boundary and re-opened on the next line.  The extraction code
# already knows the paragraph structure from the PDF layout:
#
#   \n   = same paragraph, wrapped continuation line
#   \n\n = paragraph boundary (new block, or indented first line)
#
# This function merges per-line tags within the SAME paragraph into a
# single paragraph-level tag BEFORE the text goes to the LLM.  This means:
#   - The LLM receives correct paragraph-level tags
#   - No post-LLM merge/split regex heuristics are needed
#   - Paragraph boundaries are 100% PDF-derived, not guessed
#
# Example input  (per-line tags, same paragraph — single \n):
#   [A>line one of paragraph<A]
#   [A>line two of paragraph<A]
#   [A>line three ends here.<A]
#
# Example output (paragraph-level tag):
#   [A>line one of paragraph line two of paragraph line three ends here.<A]
#
# Paragraph breaks (\n\n) are NEVER crossed:
#   [A>end of paragraph one.<A]
#
#   [A>start of paragraph two.<A]
#   → stays as two separate tags

def pre_merge_paragraph_tags(text: str) -> str:
    """Merge per-line same-type tags within paragraphs using PDF structure.

    Uses the extraction-time ``\\n`` vs ``\\n\\n`` signals as ground truth:
    - ``\\n`` between tags = same paragraph → merge
    - ``\\n\\n`` between tags = paragraph boundary → keep separate

    This runs PRE-LLM so the LLM receives paragraph-level tags with
    correct boundaries.  Eliminates the need for most post-LLM merge
    heuristics (``consolidate_adjacent_tags`` Phase 2, ``merge_split_tags``
    cross-line).

    Safe for all states because the merge decision is structural (PDF
    layout), not text-based (regex patterns).
    """
    import re as _re

    # Process each paragraph (split on \n\n) independently
    paragraphs = text.split('\n\n')
    result_paragraphs = []

    for para in paragraphs:
        # Within a paragraph, merge same-type tags across single \n
        # Phase 1: merge [A>...<A]\n[A>... → [A>... ...<A]
        # Phase 2: merge [D>...<D]\n[D>... → [D>... ...<D]
        for tag_type in ('A', 'D'):
            close = f'<{tag_type}]'
            opn   = f'[{tag_type}>'
            # Pattern: close-tag, optional trailing whitespace, exactly
            # one newline, optional leading whitespace, open-tag
            pat = _re.compile(
                _re.escape(close) + r'[ \t]*\n[ \t]*' + _re.escape(opn)
            )
            # Replace with a space (joining the two lines)
            while pat.search(para):
                para = pat.sub(' ', para)

        # Also merge same-line adjacent tags (space-separated)
        for tag_type in ('A', 'D'):
            close = f'<{tag_type}]'
            opn   = f'[{tag_type}>'
            pat = _re.compile(
                _re.escape(close) + r'[ \t]+' + _re.escape(opn)
            )
            while pat.search(para):
                para = pat.sub(' ', para)

        result_paragraphs.append(para)

    merged = '\n\n'.join(result_paragraphs)
    logger.debug("Pre-merged per-line tags into paragraph-level tags")
    return merged


# ---------------------------------------------------------------------------
# Florida (FL) deterministic post-processing — repeated header/footer strip
# ---------------------------------------------------------------------------
# FL bill PDFs repeat the same header/footer/reference-code lines on every
# page.  The LLM prompt instructs removal, but header formats vary across
# bill types (CS, CS for CS, enrolled, engrossed, etc.).  This deterministic
# pass catches ALL variations via broad regex so the prompt never needs to
# be updated for new header styles.
#
# Patterns are anchored to standalone lines (^ ... $) so they can never
# accidentally match bill numbers or years embedded in legislative prose.

# 1. Bill-number header: any depth of "CS for" or "CS/" nesting + HB/SB + number
#    Optional trailing suffixes: page number, year, ", 1st Engrossed", etc.
#    Examples:  "HB 385         2025"
#               "CS for SB 1332"
#               "CS for CS for SB 1028, 1st Engrossed"
#               "CS/CS/SB 7031"
#               "SB 2-D"
_RE_FL_BILL_HEADER = re.compile(
    r"^\s*(?:(?:CS\s+for\s+|CS/)*)"          # any nesting of CS for / CS/
    r"[HS]B\s+\d+(?:-[A-Z])?"                # HB 385, SB 1028, SB 2-D
    r"(?:\s*[a-z])?"                          # optional engrossed letter: "HB 385e"
    r"(?:,?\s*\d+(?:st|nd|rd|th)\s+Engrossed)?"  # ", 1st Engrossed"
    r"(?:\s+\d{1,4})?"                        # trailing page number or year
    r"\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# 2. Document reference code: "20261028er", "20257031er" (8-10 digits + "er")
_RE_FL_DOC_REF = re.compile(
    r"^\s*\d{6,10}er\s*$",
    re.MULTILINE,
)

# 3. ENROLLED standalone
_RE_FL_ENROLLED = re.compile(
    r"^\s*ENROLLED\s*$",
    re.MULTILINE,
)

# 4. Session marker: "2026 Legislature", "2025 Legislature"
#    Also matches when bill header shares the same line (FL puts session
#    on left, bill ID on right):
#      "2026 Legislature                CS for CS for SB 1028, 1st Engrossed"
_RE_FL_SESSION = re.compile(
    r"^\s*\d{4}\s+Legislature"
    r"(?:\s+(?:(?:CS\s+for\s+|CS/)*)[HS]B\s+\d+.*)?"  # optional bill ID on same line
    r"\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# 5. Spaced-out chamber header: "F L O R I D A   H O U S E   O F ..."
_RE_FL_SPACED_CHAMBER = re.compile(
    r"^\s*F\s+L\s+O\s+R\s+I\s+D\s+A\s+H\s+O\s+U\s+S\s+E"
    r"\s+O\s+F\s+R\s+E\s+P\s+R\s+E\s+S\s+E\s+N\s+T\s+A\s+T\s+I\s+V\s+E\s+S\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# 6. Normal chamber header: "FLORIDA HOUSE OF REPRESENTATIVES"
_RE_FL_CHAMBER = re.compile(
    r"^\s*FLORIDA\s+HOUSE\s+OF\s+REPRESENTATIVES\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# 7. Senate header: "Florida Senate - 2025"
_RE_FL_SENATE = re.compile(
    r"^\s*Florida\s+Senate\s*[-–—]\s*\d{4}\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# 8. Internal identifiers: "2025470__", "20251234__"
_RE_FL_INTERNAL_ID = re.compile(
    r"^\s*\d{7,}__\s*$",
    re.MULTILINE,
)

# 9. LRB/bill draft numbers: "40-00657B-25"
_RE_FL_DRAFT_NUM = re.compile(
    r"^\s*\d{2}-\d{5}[A-Z]-\d{2}\s*$",
    re.MULTILINE,
)

# 10. CODING footer: "CODING: Words stricken are deletions..."
_RE_FL_CODING_FOOTER = re.compile(
    r"^\s*CODING:\s+Words\s+(?:stricken|underlined).*$",
    re.IGNORECASE | re.MULTILINE,
)

# 11. Standalone 4-digit year as page header (e.g., "2025", "2026")
_RE_FL_YEAR_HEADER = re.compile(
    r"^\s*(20\d{2})\s*$",
    re.MULTILINE,
)

# Bundle all patterns for a single pass
_FL_HEADER_PATTERNS = [
    _RE_FL_BILL_HEADER,
    _RE_FL_DOC_REF,
    _RE_FL_ENROLLED,
    _RE_FL_SESSION,
    _RE_FL_SPACED_CHAMBER,
    _RE_FL_CHAMBER,
    _RE_FL_SENATE,
    _RE_FL_INTERNAL_ID,
    _RE_FL_DRAFT_NUM,
    _RE_FL_CODING_FOOTER,
    _RE_FL_YEAR_HEADER,
]


def _strip_fl_repeated_headers(text: str) -> str:
    """Deterministically strip FL bill page headers, footers, and reference codes.

    Runs post-LLM to catch any repeated header/footer lines the LLM missed.
    Uses broad regex patterns anchored to standalone lines so new formatting
    variants are caught automatically — no prompt updates needed.

    Safe because every pattern is ``^…$`` anchored: bill numbers, years, and
    reference codes that appear inside legislative prose sentences are never
    matched.
    """
    original = text
    for pat in _FL_HEADER_PATTERNS:
        text = pat.sub("", text)

    if text != original:
        # Clean up excess blank lines left behind by removals
        text = re.sub(r"\n{3,}", "\n\n", text)
        logger.debug("Stripped FL repeated headers/footers/reference codes")

    return text


# ---------------------------------------------------------------------------
# Arkansas (AR) deterministic post-processing
# ---------------------------------------------------------------------------
# AR bill PDFs include scan/print artifacts on every page that are NOT
# legislative content:
#
#   • Scan barcode line:         *HAR003*    (or *HSR12*, *SAR123*, etc.)
#   • Page-1 timestamp footer:   01/27/2025 11:03:27 AM HAR003
#   • Pages 2+ running header:   2 01/27/2025 11:03:27 AM HAR003
#                                HB1249  (bare bill-ID line)
#
# These appear consistently in every AR bill PDF and slip past the LLM
# because the AR clean-prompt instructs the model to keep page-1 content,
# so the LLM conservatively retains the timestamp/barcode block that sits
# above the real header.
#
# IMPORTANT: this stripper deliberately does NOT touch the post-passage
# stamp "Act # of the Regular Session" — that text is legitimate metadata
# that users want to retain in the cleaned output.

# AR scan barcode — short alphanumeric code wrapped in asterisks alone
# on its own line.  Pattern: 2-4 capital letters + 2-5 digits.
_RE_AR_BARCODE = re.compile(
    r"^\s*\*[A-Z]{2,4}\d{2,5}\*\s*$",
    re.MULTILINE,
)

# AR timestamp footer — "MM/DD/YYYY HH:MM:SS AM|PM <barcode>".
# Optional leading 1-3 digit page number captures the pages-2+ running
# header form ("2 01/27/2025 11:03:27 AM HAR003").
_RE_AR_TIMESTAMP = re.compile(
    r"^\s*(?:\d{1,3}\s+)?"
    r"\d{1,2}/\d{1,2}/\d{4}\s+"
    r"\d{1,2}:\d{2}:\d{2}\s+(?:AM|PM)\s+"
    r"[A-Z]{2,4}\d{2,5}\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# AR bare bill-ID running header — "HB1249", "SB85", "HJR12" alone on
# a line.  Common chamber prefixes: HB/SB (bills), HJR/SJR (joint
# resolutions), HR/SR (resolutions), HCR/SCR (concurrent resolutions).
# IMPORTANT: only matches when the bill ID is the ENTIRE line content;
# inline references like "Section 5 of HB1249 shall…" are preserved.
_RE_AR_BILL_ID_HEADER = re.compile(
    r"^\s*(?:HB|SB|HJR|SJR|HR|SR|HCR|SCR)\s*\d{1,5}\s*$",
    re.MULTILINE | re.IGNORECASE,
)


def _strip_ar_artifacts(text: str) -> str:
    """Remove Arkansas-specific PDF/scan artifacts from cleaned text.

    Strips three classes of running headers/footers that AR bill PDFs
    embed on every page:

    1. **Scan barcode line** — ``*HAR003*`` / ``*HSR###*`` / ``*SAR###*``
    2. **Timestamp footer / running header** — ``01/27/2025 11:03:27 AM HAR003``
       (page-1 footer) and ``2 01/27/2025 11:03:27 AM HAR003`` (pages 2+)
    3. **Bare bill-ID running header** — ``HB1249`` / ``SB85`` when the
       bill ID is the entire line content (inline references inside a
       sentence are NOT stripped)

    Does NOT touch ``Act # of the Regular Session`` — that's a legitimate
    post-passage metadata stamp users want preserved.

    Mirrors the pattern of :func:`_strip_in_artifacts`.
    """
    new_text = _RE_AR_BARCODE.sub("", text)
    new_text = _RE_AR_TIMESTAMP.sub("", new_text)
    new_text = _RE_AR_BILL_ID_HEADER.sub("", new_text)

    if new_text != text:
        # Collapse excess blank lines left behind by the substitutions
        new_text = re.sub(r"\n{3,}", "\n\n", new_text)
        logger.debug("Stripped AR artifacts (barcode/timestamp/bill-ID running headers)")

    return new_text


# ---------------------------------------------------------------------------
# AR page-1 header normalization — reorder into canonical vertical layout
# ---------------------------------------------------------------------------
# AR bill PDFs place several page-1 header elements in a multi-column visual
# layout where "A Bill" is centered and "HOUSE BILL XXXX" is right-aligned
# next to "Regular Session, YYYY".  PyMuPDF + LLM cleaning preserves this
# side-by-side layout with long runs of whitespace, e.g.:
#
#     State of Arkansas
#     95th General Assembly                                A Bill
#     Regular Session, 2025                          HOUSE BILL 1249
#
# The canonical AR cleaned-text convention puts these on separate lines in
# a fixed vertical order.  The patterns below match each element regardless
# of position or side-by-side concatenation; the normalizer extracts them
# and re-emits in the canonical order.

# "HOUSE BILL 1249", "SENATE BILL 85", "HOUSE JOINT RESOLUTION 12",
# "HOUSE CONCURRENT RESOLUTION 5", "SENATE RESOLUTION 12"
_RE_AR_HEADER_BILL_ID = re.compile(
    r"(?:HOUSE|SENATE)\s+"
    r"(?:BILL|JOINT\s+RESOLUTION|CONCURRENT\s+RESOLUTION|RESOLUTION)\s+"
    r"\d+",
    re.IGNORECASE,
)

# "A Bill" or "An Act" (must be a standalone phrase, not part of larger text)
_RE_AR_HEADER_A_BILL = re.compile(
    r"(?<![A-Za-z])A\s+Bill(?![A-Za-z])",
)

# "State of Arkansas"
_RE_AR_HEADER_STATE = re.compile(
    r"State\s+of\s+Arkansas",
    re.IGNORECASE,
)

# "95th General Assembly", "94th General Assembly", etc.
_RE_AR_HEADER_ASSEMBLY = re.compile(
    r"\d{1,3}(?:st|nd|rd|th)\s+General\s+Assembly",
    re.IGNORECASE,
)

# "Regular Session, 2025", "First Extraordinary Session, 2025",
# "Second Extraordinary Session, 2025", "Fiscal Session, 2024"
_RE_AR_HEADER_SESSION = re.compile(
    r"(?:Regular|First\s+Extraordinary|Second\s+Extraordinary|Fiscal)\s+Session,?\s+\d{4}",
    re.IGNORECASE,
)

# "Act 42 of the Regular Session" — post-passage stamp (legit content,
# always rendered first below the "Stricken language" anchor).
_RE_AR_HEADER_ACT_STAMP = re.compile(
    r"Act\s+\d+\s+of\s+the\s+(?:Regular|First\s+Extraordinary|Second\s+Extraordinary|Fiscal)\s+Session",
    re.IGNORECASE,
)

# Anchor lines: page 1 header starts at "Stricken language…" and ends at
# the first occurrence of any of these "real content begins here" markers.
_RE_AR_HEADER_START = re.compile(
    r"^[ \t]*Stricken\s+language\s+would\s+be\s+deleted",
    re.IGNORECASE | re.MULTILINE,
)
_RE_AR_HEADER_END = re.compile(
    r"^[ \t]*(?:By:|For\s+An\s+Act\s+To\s+Be\s+Entitled|Subtitle|BE\s+IT\s+ENACTED)",
    re.IGNORECASE | re.MULTILINE,
)


def _normalize_ar_page1_header(text: str) -> str:
    """Reorder AR page-1 header elements into a canonical vertical layout.

    Canonical order (each element on its own line, blank lines between
    distinct groups):

        Stricken language would be deleted from and underlined…
        <blank>
        Act # of the Regular Session     (only if present — post-passage stamp)
        <blank>
        HOUSE BILL 1249                  (bill identifier)
        A Bill                           (or "An Act")
        <blank>
        State of Arkansas
        95th General Assembly
        Regular Session, 2025

    Detection bounds:
      • Region starts after the "Stricken language…" anchor line.
      • Region ends at the first of: "By:", "For An Act To Be Entitled",
        "Subtitle", "BE IT ENACTED".
      • If either anchor is missing, the function is a no-op.

    Safety guarantees:
      • Only operates on the bounded header region — body-text references
        to bill IDs, sessions, or assemblies are untouched.
      • Requires at least State + Assembly + Session to be present;
        otherwise treats the text as not a recognizable AR header and
        returns unchanged.
      • Any non-matching text inside the header region (e.g. an unexpected
        line the LLM produced) is preserved at the end of the canonical
        block, so nothing is silently dropped.
    """
    # Locate the header region
    start_m = _RE_AR_HEADER_START.search(text)
    if not start_m:
        return text

    # End of the "Stricken language…" line is the splice point
    stricken_line_end = text.find("\n", start_m.start())
    if stricken_line_end == -1:
        return text

    end_m = _RE_AR_HEADER_END.search(text, stricken_line_end)
    if not end_m:
        return text

    header_block = text[stricken_line_end + 1 : end_m.start()]

    # Find each known element (first occurrence in the region)
    patterns = [
        ("act_stamp", _RE_AR_HEADER_ACT_STAMP),
        ("bill_id",   _RE_AR_HEADER_BILL_ID),
        ("a_bill",    _RE_AR_HEADER_A_BILL),
        ("state",     _RE_AR_HEADER_STATE),
        ("assembly",  _RE_AR_HEADER_ASSEMBLY),
        ("session",   _RE_AR_HEADER_SESSION),
    ]
    found: dict[str, str] = {}
    matched_spans: list[tuple[int, int]] = []
    for key, pat in patterns:
        m = pat.search(header_block)
        if m:
            found[key] = m.group(0).strip()
            matched_spans.append((m.start(), m.end()))

    # Sanity gate: only proceed if the three required core elements are
    # present.  This avoids reordering text that doesn't look like an
    # AR page-1 header (e.g. resolutions with a different format).
    required = {"state", "assembly", "session"}
    if not required.issubset(found.keys()):
        return text

    # Remove matched spans from the region (reverse order to preserve
    # indices), then collapse whitespace-only lines to detect leftovers.
    matched_spans.sort(reverse=True)
    remainder = header_block
    for s, e in matched_spans:
        remainder = remainder[:s] + remainder[e:]
    remainder_lines = [ln for ln in remainder.split("\n") if ln.strip()]
    remainder_clean = "\n".join(remainder_lines)

    # Build canonical block
    canonical_lines: list[str] = []
    if "act_stamp" in found:
        canonical_lines.append(found["act_stamp"])
        canonical_lines.append("")  # blank line after Act stamp
    if "bill_id" in found:
        canonical_lines.append(found["bill_id"])
    if "a_bill" in found:
        canonical_lines.append(found["a_bill"])
    if "bill_id" in found or "a_bill" in found:
        canonical_lines.append("")  # blank line after bill ID / A Bill group
    canonical_lines.append(found["state"])
    canonical_lines.append(found["assembly"])
    canonical_lines.append(found["session"])

    # Preserve any unrecognized leftover content (defensive — usually empty)
    if remainder_clean:
        canonical_lines.append("")
        canonical_lines.append(remainder_clean)
        logger.debug(
            "AR header normalizer preserved %d unrecognized line(s) in header region",
            len(remainder_lines),
        )

    canonical_block = "\n".join(canonical_lines)

    # Splice: keep "Stricken language…" line, blank line, canonical block,
    # blank line, then the rest of the document starting at end_m.
    new_text = (
        text[: stricken_line_end + 1]
        + "\n"
        + canonical_block
        + "\n\n"
        + text[end_m.start() :]
    )
    # Collapse any triple+ blank lines introduced by the splice
    new_text = re.sub(r"\n{3,}", "\n\n", new_text)

    if new_text != text:
        logger.debug(
            "Normalized AR page-1 header (elements: %s)",
            ", ".join(found.keys()),
        )
    return new_text


# ---------------------------------------------------------------------------
# Arkansas (AR) intra-tag paragraph splitter
# ---------------------------------------------------------------------------
# The Claude clean step occasionally fuses what should be two distinct
# paragraphs into a single ``[A>...<A]`` tag.  Two patterns recur on AR
# bills:
#
#   1. Statute heading welded onto its lead-in / first subsection ::
#
#          [A>6-81-2101. Definitions. As used in this subchapter:<A]
#          → [A>6-81-2101. Definitions.<A]
#            [A>As used in this subchapter:<A]
#
#   2. One subsection welded onto the next ::
#
#          [A>(b) ...educational loans. (c)(1) Before receiving...<A]
#          → [A>(b) ...educational loans.<A]
#            [A>(c)(1) Before receiving...<A]
#
# Detection requires a sentence terminator (``.!?:;``) immediately before
# the gap, which guards against:
#   • citation parentheses like ``§ 6-81-2104(b)`` (no terminator before
#     the paren — never split)
#   • inline numerics like ``one (1) eligible profession`` /
#     ``fifteen percent (15%)`` (no terminator before the paren — never
#     split)

# Pattern A: split inside an [A>...<A] tag at a sentence-terminator
# followed by a parenthesised subsection marker.
_RE_AR_SPLIT_AT_SUBSECTION = re.compile(
    r"(?<=[.!?:;])"                   # terminator before the gap
    r"\s+"                            # whitespace consumed
    r"(?="                            # lookahead: subsection marker
    r"\((?:[a-zA-Z]+|\d+)\)"          # (a) / (1) / (D) / (15)
    r"(?:\([a-zA-Z\d]+\))*"           # optional nested marker like (c)(1)
    r"\s"                             # space after the marker
    r")"
)

# Pattern B: at the start of a tag, statute heading like
# "6-81-2101. Definitions." followed by a capital-letter sentence.
# Title text must not contain internal periods (rules out citations).
_RE_AR_SPLIT_AT_STATUTE_HEAD = re.compile(
    r"^\s*(\d+-\d+-\d+\.\s+[A-Z][^.<>]*?\.)"  # group 1: statute heading
    r"\s+"
    r"([A-Z][^<]+)$"                            # group 2: trailing sentence
)

# Tag-extraction regex.  ``[^<]*?`` stops at the first ``<`` so we never
# straddle nested ``[D>...<D]`` deletions inside additions (rare in AR).
_RE_AR_ADD_TAG = re.compile(r"\[A>([^<]*?)<A\]")


def _split_ar_welded_paragraphs(text: str) -> str:
    """Split AR ``[A>...<A]`` tags that wrap multiple paragraphs.

    Handles two LLM-introduced welds that are characteristic of AR
    bills (see module-level comment for examples):

    1. **Statute heading welded onto its body** — split after the
       title's terminating period.
    2. **Subsection welded onto the next subsection** — split before
       a ``(a)`` / ``(1)`` / ``(c)(1)`` marker that follows a sentence
       terminator.

    Both patterns are conservative: a sentence terminator must precede
    the split point, so citation patterns (``§ 6-81-2104(b)``) and
    inline parentheticals (``one (1)``, ``fifteen percent (15%)``) are
    NEVER split — there's no terminator before their parens.

    The function is idempotent: running it on already-split text is a
    no-op because each emitted tag contains a single paragraph and
    therefore matches neither split pattern.
    """

    def _process_tag(m: re.Match[str]) -> str:
        content = m.group(1)
        # Pattern A: split at every sentence-terminator → subsection
        # boundary inside the tag.  ``re.split`` keeps the segments in
        # order; we re-wrap each non-empty segment in fresh tags.
        parts = _RE_AR_SPLIT_AT_SUBSECTION.split(content)
        if len(parts) > 1:
            return "\n\n".join(
                f"[A>{p.strip()}<A]" for p in parts if p.strip()
            )
        # Pattern B: statute-heading split (only fires when Pattern A
        # found nothing — a heading welded onto a list item is already
        # caught by Pattern A above).
        sm = _RE_AR_SPLIT_AT_STATUTE_HEAD.match(content)
        if sm:
            return (
                f"[A>{sm.group(1).strip()}<A]\n\n"
                f"[A>{sm.group(2).strip()}<A]"
            )
        return m.group(0)

    new_text = _RE_AR_ADD_TAG.sub(_process_tag, text)
    if new_text != text:
        logger.debug("Split AR welded paragraphs (heading / subsection boundaries)")
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
# KS signature-line injection — recover vector-drawn horizontal rules
# ---------------------------------------------------------------------------
# Enrolled Kansas bills have a certification / signing block at the end
# with handwritten-signature placeholders drawn as horizontal vector lines.
# PyMuPDF's text extraction skips these because they are path objects, not
# text.  This function detects them and inserts underscore text at the
# correct vertical position among the text lines.

def _inject_ks_signature_lines(page: fitz.Page, page_text: str) -> str:
    """Inject ``______________________________`` for vector-drawn signature lines.

    Only operates on KS pages whose text contains signature-block markers
    (e.g. "Speaker of the House", "Governor", "APPROVED").  This avoids
    touching ordinary legislative pages.

    Handles two types of signature lines:

    **Type 1 — Standalone lines** (not near any text):
        Horizontal rules that sit between text lines, used as handwritten-
        signature placeholders.  Injected as a separate underscore line
        BEFORE the text line immediately below.

    **Type 2 — Fill-in lines** (same Y as text, but extending to the right):
        Lines like "Conference Committee Report________" or "APPROVED ____".
        The drawing starts where the text content ends and extends further
        right.  Detected by checking that the drawing's x0 is ≥ 30pt to
        the right of the text's x0 (real underlines span the full text
        width, so their x0 ≈ text x0).  Underscores are APPENDED to the
        text line.
    """
    # Quick bail: only fire when the page has signature-block text
    _SIG_MARKERS = re.compile(
        r"Speaker of the House|President of the Senate|"
        r"Chief Clerk|Secretary of the Senate|Secretary of State|"
        r"\bAPPROVED\b|\bGovernor\b",
        re.IGNORECASE,
    )
    if not _SIG_MARKERS.search(page_text):
        return page_text

    # 1. Get horizontal line drawings
    hlines = _extract_hlines_from_drawings(page)
    sig_lines = [h for h in hlines if (h.x1 - h.x0) >= 80]
    if not sig_lines:
        return page_text

    # 2. Get text-line data: bottom_y, x0, text
    text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
    text_entries: list[tuple[float, float, str]] = []  # (bottom_y, x0, text)
    for blk in text_dict.get("blocks", []):
        if blk.get("type") != 0:
            continue
        for ln in blk.get("lines", []):
            bb = ln.get("bbox")
            if not bb:
                continue
            txt = ""
            for sp in ln.get("spans", []):
                txt += sp.get("text", "")
            txt = txt.strip()
            if txt:
                text_entries.append((bb[3], bb[0], txt))
    text_entries.sort(key=lambda t: t[0])

    # 3. Classify each line as standalone, fill-in, or true underline
    UNDERLINE_TOL = 6.0
    FILL_IN_GAP = 30.0  # if line x0 is ≥ 30pt right of text x0 → fill-in
    UNDERSCORE = "______________________________"

    # Build a unified list of actions sorted by Y-position
    # Each action: (y, type, target_text)
    #   type="insert" → insert underscore line BEFORE target_text
    #   type="append" → append underscores TO target_text
    actions: list[tuple[float, str, str]] = []

    for h in sig_lines:
        # Find the nearest text line by y
        near_entry = None
        for ey, ex0, etxt in text_entries:
            if abs(h.y - ey) < UNDERLINE_TOL:
                near_entry = (ey, ex0, etxt)
                break

        if near_entry is None:
            # Standalone signature line → insert before next text
            for ey, _, etxt in text_entries:
                if ey > h.y + 3:
                    actions.append((h.y, "insert", etxt))
                    break
        else:
            _, text_x0, text_content = near_entry
            if h.x0 >= text_x0 + FILL_IN_GAP:
                # Fill-in line → append to the text
                actions.append((h.y, "append", text_content))
            # else: true underline → skip

    if not actions:
        return page_text

    # Sort actions by Y so we process them top-to-bottom
    actions.sort(key=lambda a: a[0])

    # 4. Walk through page_text and apply actions in order
    lines = page_text.split('\n')
    new_lines: list[str] = []
    action_idx = 0
    count = 0

    for line in lines:
        stripped = line.strip()
        if stripped and action_idx < len(actions):
            _, atype, atarget = actions[action_idx]
            # Check if this line matches the current action's target
            if atarget[:25] in stripped or stripped[:25] in atarget:
                if atype == "insert":
                    new_lines.append(UNDERSCORE)
                elif atype == "append":
                    line = line.rstrip() + UNDERSCORE
                action_idx += 1
                count += 1

        new_lines.append(line)

    if count:
        logger.debug("Injected %d signature/fill-in lines on KS page", count)
    return '\n'.join(new_lines)


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
    state_code: Optional[str] = None,
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
            state_code=state_code,
        )
        clean = _clean_tagged_text(raw_text, strip_line_numbers=strip_line_numbers)
        # NOTE: _tag_brackets() is NOT called here.  It runs AFTER the
        # LLM clean step (in clean_extracted_text) so the LLM never sees
        # confusing nested [D>[…]<D] structures.

        # KS-only: inject underscore text for vector-drawn signature lines
        # on pages that contain the certification/signing block.
        if state_code and state_code.upper() == "KS":
            clean = _inject_ks_signature_lines(page, clean)

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
🛡️  **TAG MERGING — NEVER DROP CONTENT**: When merging per-line tags into
     paragraph-level tags, you must preserve ALL tagged content.  Every
     `[D>…<D]` block in the input must appear in the output.  If two
     consecutively numbered items (e.g. item 6 and item 7) have separate
     tags, they are SEPARATE items — do NOT combine them into one, even
     if the new text reads as a continuation of the previous item.

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

    # ── strip DC SBOE letterhead-footer + hyperlink-underline tags ────
    # DC State Board of Education transmittal letters open with a
    # 3-line letterhead block (org name / address / phone / website /
    # email / @DCSBOE handle) that PyMuPDF places at the top of the
    # converted text even though it sits at y≈745 on the PDF page.
    # The same block also produces false [A>…<A] tags around the
    # hyperlinked URL and emails (sboe.dc.gov, sboe@dc.gov,
    # bernice.butler@dc.gov, etc.).  Both issues are scoped to DC.
    if state_code and state_code.upper() == "DC":
        cleaned = _strip_dc_letterhead_footer(cleaned)
        cleaned = _strip_dc_link_underline_tags(cleaned)

    # ── strip IN-specific artifacts (footers, barcodes, false headers) ─
    if state_code and state_code.upper() == "IN":
        cleaned = _strip_in_header_tags(cleaned)
        cleaned = _strip_in_artifacts(cleaned)

    # ── strip AR-specific artifacts (barcode, timestamp footer, bill-ID
    #    running headers).  These are scan/print artifacts that appear on
    #    every page of AR bill PDFs and slip past the LLM because the AR
    #    clean-prompt instructs the model to retain page-1 content.  See
    #    `_strip_ar_artifacts` for the full pattern list.  Preserves the
    #    "Act # of the Regular Session" post-passage stamp.
    if state_code and state_code.upper() == "AR":
        cleaned = _strip_ar_artifacts(cleaned)
        # Reorder page-1 header elements into canonical vertical layout.
        # AR PDFs render "A Bill" / "HOUSE BILL XXXX" side-by-side with
        # "State of Arkansas" / "##th General Assembly" / "Regular Session,
        # YYYY" via long whitespace runs; the LLM preserves the visual
        # layout instead of emitting them as separate lines.  This
        # normalizer extracts the known header elements and re-emits them
        # in canonical order.  See `_normalize_ar_page1_header` for the
        # full algorithm and safety guarantees.
        cleaned = _normalize_ar_page1_header(cleaned)

    # ── strip KS-specific false header tags ────────────────────────
    if state_code and state_code.upper() == "KS":
        cleaned = _strip_ks_header_tags(cleaned)
        # Strip false [A>…<A] tags from enacting / resolving clauses
        # ("Be it enacted by …", "Be it resolved by …", "Be it further
        # resolved …").  These are document boilerplate and must never
        # be tagged as added language, even when the source PDF renders
        # them in italic / bold-italic.  Operates on the full text since
        # "Be it further resolved" can appear anywhere in a resolution.
        cleaned = _strip_ks_resolving_clause_tags(cleaned)
        # Strip false [A>…<A] tags from the certification / signing block
        # at the end of enrolled KS bills ("Speaker of the House",
        # "President of the Senate", "APPROVED", "Governor", etc.).
        # These are document boilerplate and must never be tagged.
        # Only scans the last ~25 lines to avoid touching body text.
        cleaned = _strip_ks_signature_block_tags(cleaned)

    # ── consolidate, clean, and split tagged blocks ───────────────────
    cleaned = consolidate_adjacent_tags(cleaned)
    cleaned = remove_nested_tags(cleaned)
    cleaned = merge_split_tags(cleaned)
    cleaned = split_tagged_subsections(cleaned)
    # Restore paragraph breaks at renumber-only [D>(N)<D] [A>(M)<A] pairs
    # that the LLM tends to collapse onto a single line.  See the docstring
    # of `split_renumber_paragraph_breaks` for the detection rules.
    cleaned = split_renumber_paragraph_breaks(cleaned)
    # ── IN-specific: split tagged lines where a short numbered item
    #    (e.g. "(2) interview each member.") got welded to the
    #    following paragraph by the LLM.  Scoped to IN only because
    #    other states format numbered sub-items differently (e.g. CA
    #    keeps them as standalone header lines).  See the docstring of
    #    `split_numbered_item_paragraph_welds` for the detection rules.
    # ── DE/IN-specific: split tags that span description + dollar amount
    #    columns, and header:data merged tags (same as Hybrid pipeline).
    if state_code and state_code.upper() in ("DE", "IN"):
        cleaned = _split_tabular_header_from_data(cleaned)
        cleaned = _split_tabular_tag_at_dollar(cleaned)
    if state_code and state_code.upper() == "IN":
        cleaned = _split_in_merged_table_rows(cleaned)
        cleaned = _split_in_section_heading_welds(cleaned)
        cleaned = split_numbered_item_paragraph_welds(cleaned)
    # ── KS-specific: re-join LLM-inserted paragraph breaks between a
    #    "K.S.A. <num> is hereby amended to read as follows:" intro and
    #    the statute body that immediately follows.  The PDF/raw PyMuPDF
    #    text has these on the same paragraph; the LLM tends to split
    #    them.  Scoped to KS only because the function relies on KS
    #    statute-numbering conventions; running it elsewhere would be a
    #    no-op but the explicit guard keeps state-specific logic in one
    #    obvious place (mirrors the `_strip_ks_header_tags` pattern
    #    above).  See `rejoin_amendment_intro_breaks` for details.
    if state_code and state_code.upper() == "KS":
        cleaned = rejoin_amendment_intro_breaks(cleaned)

    # ── IA / DE / IN-specific: re-join LLM-inserted paragraph breaks
    #    between [D>marker<D] and [A>marker<A] when the addition
    #    marker is followed by inline content on the same line.
    #    Iowa (numeric subsection renumbers like [D>1.<D] [A>2.<A]),
    #    Delaware (parenthesised paragraph renumbers like
    #    [D>(6)<D] [A>(4)<A]), and Indiana (parenthesised list
    #    renumbers like [D>(14)<D] [A>(15)<A] or roman-numeral
    #    [D>(i)<D] [A>(A)<A]) all keep their renumber pair on the
    #    same paragraph as the body prose; the LLM sometimes
    #    misreads this as a header line and adds a false \n\n.
    #    Scoped to these states only because California uses the
    #    opposite convention (renumber sits alone on a header line)
    #    and we explicitly create that layout via
    #    `split_renumber_paragraph_breaks` for CA bills.  See
    #    `rejoin_inline_renumber_list_breaks` for details.
    if state_code and state_code.upper() in {"IA", "DE", "IN"}:
        cleaned = rejoin_inline_renumber_list_breaks(cleaned)

    # ── AR-specific: split welded paragraphs from LLM clean step.
    #    Runs LAST in the AR post-processing chain so that any cross-tag
    #    welds that survived `merge_split_tags` (e.g. when the LLM
    #    output two paragraphs as a single ``[A>...<A]`` tag) get
    #    split back into one tag per paragraph.  See the docstring of
    #    `_split_ar_welded_paragraphs` for the detection rules and the
    #    citation / inline-numeric guards that prevent false splits.
    if state_code and state_code.upper() == "AR":
        cleaned = _split_ar_welded_paragraphs(cleaned)

    # ── IN-specific: split mid-line item merges like "; and (2)"
    #    so that each item starts on its own line.
    if state_code and state_code.upper() == "IN":
        cleaned = _split_in_midline_items(cleaned)

    # ── IN-specific: ensure blank lines between (a)/(1) items.
    #    Must run BEFORE _strip_in_leading_indent so that indentation
    #    is still available for pattern matching (not critical here,
    #    but keeps ordering logical).
    if state_code and state_code.upper() == "IN":
        cleaned = _ensure_in_item_spacing(cleaned)

    # ── IN-specific: strip PDF indentation as the VERY LAST step.
    #    Must run AFTER all merge/split/rejoin steps because those can
    #    re-introduce leading whitespace.  Every IN output line should
    #    start at the left margin — no leading spaces or tabs.
    if state_code and state_code.upper() == "IN":
        cleaned = _strip_in_leading_indent(cleaned)

    # Store the diff report for the caller to retrieve and save
    clean_extracted_text._last_diff_report = diff_report

    # ── Passive watchdog: scan the final cleaned text for known
    #    extraction anti-patterns (trailing "$", split numbers, empty
    #    tags, etc.).  Read-only — never mutates `cleaned`.  Results are
    #    attached to the function for the UI to retrieve and logged as
    #    structured warnings so CloudWatch / metric filters can alarm on
    #    sustained regressions.  See extraction_validator.py for the
    #    catalogue of patterns and the rationale for each.
    try:
        from extraction_validator import validate_extraction, log_anomalies
        _pdf_basename = (
            Path(pdf_path).name if pdf_path else None
        )
        anomalies = validate_extraction(
            cleaned,
            state_code=state_code,
            pdf_basename=_pdf_basename,
        )
        clean_extracted_text._last_anomalies = anomalies
        if anomalies:
            log_anomalies(anomalies, log=logger)
            logger.warning(
                "🚨 %d extraction anomalies detected (state=%s, pdf=%s)",
                len(anomalies), state_code or "?", _pdf_basename or "?",
            )
    except Exception as _vexc:
        # The validator must never break extraction.  Log and continue.
        logger.debug("extraction_validator failed: %s", _vexc, exc_info=True)
        clean_extracted_text._last_anomalies = []

    return cleaned


def consolidate_adjacent_tags(text: str, state_code: str = None) -> str:
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

    Args:
        text: The text to process.
        state_code: Two-letter state code (e.g. "FL").  When ``"FL"``,
            an additional terminal-punctuation guard prevents cross-line
            merging when the prior tag ends with ``.``, ``!``, ``?``,
            ``:``, or ``;``.  FL bills have long continuous underlined
            paragraphs where the LLM collapses blank lines between
            separate paragraphs to single newlines; without this guard
            those paragraphs get welded into one giant tag.
    """
    import re as _re
    # All markers (for cross-line protection)
    # Sec\. has no trailing \s so it also blocks [A>Sec.<A] (end-of-tag)
    # \d+-\d+ matches statute numbers like "72-3404." that follow "read as follows:"
    # [\s<] instead of \s so standalone items in their own tag
    # (e.g. [D>2.<D], [A>(a)<A]) are also recognised — the close-tag
    # opener '<' immediately follows the marker in those cases.
    _NO_MERGE = r'(?![a-cA-C1-9]\.[\s<]|\([a-zA-Z0-9]{1,4}\)[\s<]|Sec\.|SECTION\s|\d+-\d)'
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
    _adj_a_inline = _re.compile(r'<A\][ \t]{1,5}\[A>' + _NO_MERGE_INLINE)
    while _adj_a_inline.search(text):
        text = _adj_a_inline.sub(' ', text)
    _adj_d_inline = _re.compile(r'<D\][ \t]{1,5}\[D>' + _NO_MERGE_INLINE)
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
    # IMPORTANT: pattern is [ \t]*\n[ \t]{0,15}  (exactly ONE newline,
    # AT MOST 15 chars of indent) — NOT \s*\n\s* which would also match
    # paragraph breaks (\n\n) and weld together two adjacent digest
    # paragraphs (e.g. the italic LCD digest in California bills where
    # every paragraph starts with "Existing law…" or "This bill would…"
    # — neither matches _NO_MERGE, so they would all collapse into one
    # giant [A>…<A] block without this guard).
    #
    # Indent cap of 15 chars also protects column-aligned TABLE CELLS
    # whose continuation lines indent to a column (e.g. Florida 921.0022
    # offence-severity chart description column at offset 66): the
    # 66-space indent exceeds the {0,15} bound so the cross-line regex
    # does NOT match, leaving each visual line with its OWN tag pair.
    # Normal paragraph wrapping (≤ 8 spaces) still merges as before.
    #
    # FL-specific terminal-punctuation guard: a 1-char lookbehind
    # (?<![.!?:;]) prevents merging when the prior tag closes
    # immediately after a sentence terminator.  FL bills have long
    # continuous underlined paragraphs where the LLM collapses blank
    # lines to single newlines; without this guard those separate
    # paragraphs get welded into one giant tag.  Only applied for FL
    # because other states rely on cross-line merging at sentence
    # boundaries (the LLM wraps mid-sentence for them).
    _is_fl = state_code and state_code.upper() == "FL"
    _no_term = r'(?<![.!?:;])' if _is_fl else ''
    _adj_a_cross = _re.compile(_no_term + r'<A\][ \t]*\n[ \t]{0,15}\[A>' + _NO_MERGE)
    while _adj_a_cross.search(text):
        text = _adj_a_cross.sub(' ', text)
    _adj_d_cross = _re.compile(_no_term + r'<D\][ \t]*\n[ \t]{0,15}\[D>' + _NO_MERGE)
    while _adj_d_cross.search(text):
        text = _adj_d_cross.sub(' ', text)

    # ── Phase 3: ensure blank line before subsection boundaries ───────
    # After merging, adjacent tags separated by a single \n that start
    # with a marker should have a blank line (\n\n) for readability.
    _MARKER = r'(\([a-zA-Z0-9]{1,4}\)[\s<]|[a-cA-C1-9]\.[\s<]|Sec\.\s|SECTION\s|\d+-\d)'
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

    Cross-sentence guard
    --------------------
    A negative lookbehind ``(?<![.!?:;])`` is applied to every merging
    pattern so we never weld two tags when the prior tag closes
    *immediately after* a sentence terminator (``.``, ``!``, ``?``,
    ``:``, ``;``).  Without this guard, AR section headings like
    ``[A>6-81-2101. Definitions.<A]`` were getting fused onto the lead-in
    paragraph ``[A>As used in this subchapter:<A]`` because the LLM
    sometimes outputs them on adjacent lines (single ``\n``) and the
    cross-line pattern below would silently merge them.  PDF line wraps
    that legitimately need to be merged never end with a sentence
    terminator (the wrap point is mid-clause), so this guard is safe.
    """
    # 1-char fixed-width negative lookbehind: prior tag must NOT close
    # right after a sentence/clause terminator.  Placed before <T] so it
    # checks the last character of the prior tag's content.
    _no_terminator = r'(?<![.!?:;])'
    for tag_type in ['A', 'D']:
        _no_sub = (
            r'(?!\s*\(\d+\))'
            r'(?!\s*\([A-Z]+\))'
            r'(?!\s*\([a-z]+\))'
            r'(?!\s*[a-z]\.\d*\.?[\s<])'
            r'(?!\s*\d+\.[\s<])'
        )
        # 0) Inline joins: <T][T>text on same line — merge unless prior
        #    closed at a sentence boundary.
        inline_cont = (
            _no_terminator
            + r'<' + tag_type + r'\]\[' + tag_type + r'>'
            + _no_sub
        )
        text = re.sub(inline_cont, '', text)
        # 0b) Inline subsection boundary: <T][T>(1) → <T]\n\n[T>(1)
        inline_sub = r'<' + tag_type + r'\]\[' + tag_type + r'>'
        text = re.sub(inline_sub, '<' + tag_type + ']\n\n[' + tag_type + '>', text)
        # 1) Continuations: \n<T][T>text (NOT a subsection) → merge
        #    unless the prior content ends with a sentence terminator.
        same_cont = (
            _no_terminator
            + r'\n<' + tag_type + r'\]\[' + tag_type + r'>'
            + _no_sub
        )
        text = re.sub(same_cont, ' ', text)
        # 2) Subsection boundaries: \n<T][T>(1) → <T]\n\n[T>(1)
        same_sub = r'\n<' + tag_type + r'\]\[' + tag_type + r'>'
        text = re.sub(same_sub, '<' + tag_type + ']\n\n[' + tag_type + '>', text)
        # 3) Cross-line joins: <T]\n[T> continuation
        # IMPORTANT: gap is [ \t]*\n[ \t]{0,15} (exactly ONE newline,
        # AT MOST 15 chars of indent) — NOT \s*\n\s* which would also
        # swallow paragraph breaks (\n\n) and merge two distinct
        # same-type paragraphs into one block.
        #
        # Indent cap of 15 chars also protects column-aligned TABLE
        # CELLS whose continuation lines indent to a column (e.g.
        # Florida offence-severity chart description column at offset
        # 66): the 66-space indent exceeds the {0,15} bound so the
        # cross-line regex does NOT match, leaving each visual line
        # with its OWN tag pair.  Normal paragraph wrapping (≤ 8
        # spaces) still merges as before.
        #
        # The leading ``_no_terminator`` lookbehind additionally blocks
        # cross-paragraph welds where the prior tag ends with ``.``,
        # ``!``, ``?``, ``:`` or ``;`` (i.e. a complete sentence /
        # clause).  Such pairs are never PDF-wrap continuations.
        cross_line = (
            _no_terminator
            + r'<' + tag_type + r'\]'
            r'([ \t]*\n[ \t]{0,15})'
            r'\[' + tag_type + r'>' + _no_sub
        )
        text = re.sub(cross_line, ' ', text)
    return text


def merge_cross_page_break_tags(text: str) -> str:
    """Merge adjacent same-type tags split across a page break.

    Background
    ----------
    The standard :func:`merge_split_tags` only merges across a SINGLE
    newline gap (``[ \\t]*\\n[ \\t]{0,15}``) because a double newline
    (``\\n\\n``) almost always marks a real paragraph boundary that must
    be preserved.

    But when a paragraph wraps across a **page break**, the PyMuPDF text
    extraction and the LLM cleaning step often emit the two halves as
    SEPARATE tags separated by a paragraph break::

        [D>(2) When passing a senior center ... A local<D]

        [D>authority may erect a sign ... shall apply.<D]

    Both halves should logically be ONE tag because the strikethrough /
    underline in the source PDF is continuous across the page break.

    This function detects and merges such pairs using three guards that
    together make false-positive merging effectively impossible:

    1. **First tag must NOT end with a sentence/clause terminator**
       (``.``, ``!``, ``?``, ``:``, ``;``).  A paragraph that ended
       with a terminator is genuinely complete; the next tag is a NEW
       paragraph.
    2. **Second tag must START with a lowercase ASCII letter**
       (``[a-z]``).  In legal text new paragraphs almost always start
       with a capital (``The court shall…``), a digit (``1. As used…``),
       a parenthesised label (``(a) The board…``), or a section header
       (``SECTION 1.``).  Starting with a lowercase letter is a strong
       continuation signal — the only realistic case is a word that was
       wrapped from the previous page.
    3. **Second tag must NOT start with a subsection marker** (``(a)``,
       ``(1)``, ``a.``, ``1.``, …).  Belt-and-braces guard layered on
       top of #2 in case a subsection ever started with a lowercase
       letter.

    Only tags of the same type are considered (``[A>…<A]`` with another
    ``[A>…<A]``, or ``[D>…<D]`` with another ``[D>…<D]``).  The merge is
    iterative — three consecutive paragraphs that all wrap across page
    breaks are merged into one in successive passes.
    """
    tag_pat = re.compile(r'\[([AD])>(.*?)<\1\]', re.DOTALL)
    # Subsection-marker regexes (second tag must not start with any of these)
    _re_paren_label = re.compile(r'^\([A-Za-z0-9]{1,4}\)\s')
    _re_letter_dot = re.compile(r'^[a-z]\.\d*\.?\s')
    _re_digit_dot  = re.compile(r'^\d+\.\s')

    changed = True
    safety_iters = 0
    while changed and safety_iters < 50:
        changed = False
        safety_iters += 1
        tags = list(tag_pat.finditer(text))
        for i in range(len(tags) - 1):
            t1 = tags[i]
            t2 = tags[i + 1]
            # Same tag type?
            if t1.group(1) != t2.group(1):
                continue
            # Gap between the two tags must be a paragraph break
            # (2+ newlines, only whitespace otherwise).
            between = text[t1.end():t2.start()]
            if not re.fullmatch(r'[ \t]*\n[ \t]*\n[ \t\n]*', between):
                continue
            first_text  = t1.group(2)
            second_text = t2.group(2)
            # Guard 1: first tag must NOT end with sentence/clause terminator
            first_rstrip = first_text.rstrip()
            if not first_rstrip:
                continue
            if first_rstrip[-1] in ".!?:;":
                continue
            # Guard 2: second tag must START with lowercase ASCII letter
            second_lstrip = second_text.lstrip()
            if not second_lstrip:
                continue
            if not (second_lstrip[0].isascii()
                    and second_lstrip[0].islower()):
                continue
            # Guard 3: second tag must NOT start with a subsection marker
            if _re_paren_label.match(second_lstrip):
                continue
            if _re_letter_dot.match(second_lstrip):
                continue
            if _re_digit_dot.match(second_lstrip):
                continue
            # All guards passed — merge the two tags into one.
            merged_inner = first_rstrip + ' ' + second_lstrip
            tag_letter = t1.group(1)
            replacement = f'[{tag_letter}>{merged_inner}<{tag_letter}]'
            text = text[:t1.start()] + replacement + text[t2.end():]
            changed = True
            break  # positions have shifted; restart the scan
    return text


def split_tagged_subsections(text: str) -> str:
    """No-op (kept for backwards compatibility with existing call sites).

    **Disabled by design.**  Earlier this function tried to split long
    ``[A>…<A]`` / ``[D>…<D]`` blocks at internal subsection markers
    (``(a)``, ``(1)``, ``a.``, ``1.``…) so each subsection became its
    own tag.  In practice it caused more problems than it solved:

    1. **Citation truncation** — ``K.S.A. 65-1501(a)(4)``,
       ``IC 36-1-3-1.8``, ``U.S.C. 1234`` all matched ``[a-z]\\.`` /
       ``\\d+\\.`` markers and got chopped mid-citation, producing
       garbage tags like ``[A>…K.<A] [A>S.<A] [A>A. 65-1501(a)(4)…<A]``.
    2. **Section-header bleed** — ``Sec. 1.``, ``Sec. 2.``… got pulled
       off the end of the previous tag, leaving fragments like
       ``[A>…United States. Sec.<A] [A>1. As used in this chapter…<A]``.
    3. **Edit fragmentation** — even on clean input, splitting one
       coherent legislative edit (``(2) Infrastructure: (A)…; (B)…``)
       into 3 separate tags makes review harder, not easier, because the
       reviewer wants to see the full edit as one unit.
    4. **Double-execution cascade** — the function is called both inside
       ``clean_extracted_text`` and again by Streamlit/Opus post-process
       chains.  Any partial split from round 1 became fully fragmented
       in round 2 (``[A>…K.<A][A>S. A.…<A]`` → ``[A>…K.<A][A>S.<A]
       [A>A. …<A]``).

    Empirical survey across **133 real session files**:
      - 94 % of files: function did nothing useful
      - of the 6 % it changed, **50 % had garbage fragments**
      - 14 % of all new tags it created were corrupt

    Conclusion: the function is more harmful than helpful.  It is
    retained as a no-op so that the four call sites in this module,
    ``streamlit_app.py``, and ``Claude_Opus_4.6_Test.py`` continue to
    work without modification.  If a real subsection-splitting need
    arises in future, write a new function with explicit safeguards
    rather than re-enabling this regex-only approach.
    """
    return text


def _legacy_split_tagged_subsections_DISABLED(text: str) -> str:  # pragma: no cover
    """Original implementation — kept for archival reference only.

    **Do not call.**  See ``split_tagged_subsections`` above for the
    rationale behind disabling this logic.
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
        r'|(?<!\$)(?<!\d)(?:\d{2,}\.\d*\.?\s*)$'
        r'|(?:^|\W)(?:Sec|Section|Art|Article)\.?\s+\d+\.\s*$'
        r'|(?:^|\W)[A-Za-z]\.\s*$',
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

    Or for **statute number renumbering** (when a code section number is
    shifted because earlier sections were inserted/removed)::

        [D>2182.<D]                         ← deleted statute number
        [A>2183.<A] This chapter shall become operative …

    The pre-clean step preserves this layout (``\\n\\n`` between the two
    tags), but the LLM cleanup pass tends to **collapse** the gap into a
    single space::

        [D>(7)<D] [A>(8)<A] "Malice" means …
        [D>SEC. 5.<D] [A>SEC. 8.<A] Section 7334 of …
        [D>2182.<D] [A>2183.<A] This chapter shall become operative …

    This function detects that pattern and **restores the paragraph
    break** (``\\n\\n``) between the markers, so the output mirrors the
    PDF's visual layout — exactly what the user sees on the printed page.

    Detection criteria (ALL must hold):

    1. A deletion tag containing **only** a recognised marker:
       - parenthesised:  ``(7)``, ``(ii)``, ``(B)``
       - section header: ``SEC. 5.``, ``SECTION 1.`` (with trailing period)
       - statute number: ``2182.``, ``2181.5.`` (bare digits with trailing period)
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

    **Inline-marker edits are also protected**.  Some legislative styles
    use inline renumbers where the section keyword is *outside* the tag
    and only the digit is edited, e.g. Kansas bills::

        Sec.[D> 2.<D] [A>3.<A] K.S.A. 44-810 is hereby amended …
        ↑ "Sec." precedes the [D> tag → inline edit, not a paragraph break

        Section [D>5.<D] [A>6.<A] of the Code …
        subsection [D>(a)<D] [A>(b)<A] of …

    These must NOT be split because the layout in the source PDF has the
    pair on a single line — splitting them would break the grammatical
    flow of the sentence.  The function detects this by checking whether
    a section keyword (``Sec.``/``Section``/``Art.``/``Article``/
    ``subsection``/``paragraph``…) appears immediately before the
    ``[D>`` tag *within the current paragraph*.
    """
    # Marker patterns (alternation):
    #   • Parenthesised ID: (1)  (10)  (a)  (B)  (i)  (vii)  (iii)
    #   • Section header:   SEC. 5.   SEC. 10.   SECTION 1.
    #   • Statute number:   2182.   5301.   2181.5.   (bare digits + trailing dot)
    marker_paren   = r"\(\s*[a-zA-Z0-9]{1,6}\s*\)"
    marker_section = r"(?:SEC\.|SECTION)\s+\d{1,4}\.?"
    marker_statute = r"\d{1,5}(?:\.\d{1,4})?\."
    marker = r"(?:" + marker_paren + r"|" + marker_section + r"|" + marker_statute + r")"

    pattern = re.compile(
        r"(\[D>\s*" + marker + r"\s*<D\])"   # \1: [D>(marker)<D]
        r"[ \t]+"                             # spaces/tabs only — no newline
        r"(\[A>\s*" + marker + r"\s*<A\])"   # \2: [A>(marker)<A]
        r"[ \t]+"                             # whitespace before content
        r"(?=\S)"                             # at least one non-ws char follows
    )

    # Inline-prefix guard: if any of these keywords appears immediately
    # before the [D> tag *within the current paragraph*, the renumber is
    # an inline edit (e.g. "Sec. 2. → Sec. 3.") — keep tags joined.
    _inline_prefix_re = re.compile(
        r'(?:^|\W)(?:Sec|Section|SEC|SECTION|Art|Article|ARTICLE|'
        r'subsection|paragraph|subparagraph|clause|item|division|chapter)'
        r'\.?\s*$',
        re.IGNORECASE,
    )

    def _maybe_split(m):
        start = m.start()
        # Find start of the current paragraph (after the most recent \n\n)
        para_break = text.rfind('\n\n', 0, start)
        ctx_start = (para_break + 2) if para_break >= 0 else 0
        preceding = text[ctx_start:start]

        # If a section keyword precedes the [D> tag within this paragraph,
        # it's an inline edit (not a paragraph-level renumber) → leave joined.
        if _inline_prefix_re.search(preceding):
            return m.group(0)

        # If the preceding paragraph text ends with a word character
        # (letter/digit — not sentence-terminal punctuation like . : ; ! ?)
        # the renumber is mid-sentence, e.g.:
        #   "...the institution has[D>(1)<D] [A>(A)<A] obtained authorization"
        #   "...Pell Grant program, and [D>(2)<D] [A>(B)<A] obtained approvals"
        # In these cases the (old)→(new) markers are inline edits within
        # a running sentence, NOT paragraph-level subsection renumbers.
        # Splitting them with \n\n would break the grammatical flow.
        stripped_pre = preceding.rstrip()
        if stripped_pre and not re.search(r'[.;:!?]\s*$', stripped_pre):
            return m.group(0)

        # Otherwise it's a standalone block renumber that the LLM merged →
        # restore the paragraph break.
        return f"{m.group(1)}\n\n{m.group(2)} "

    return pattern.sub(_maybe_split, text)


# ---------------------------------------------------------------------------
# _split_in_merged_table_rows  (IN-specific deterministic safety net)
# ---------------------------------------------------------------------------

# Regex: a dollar amount followed by spaces before the next row text.
# Multi-space variant (preserved from PDF).
_RE_TABLE_ROW_BOUNDARY_MULTI = re.compile(
    r'(\$\d[\d,]*)\s{2,}(?=[A-Z])'
)
# Single-space variant — only safe when the line has 3+ dollar amounts
# (strong signal it's a merged table, not prose).
_RE_TABLE_ROW_BOUNDARY_SINGLE = re.compile(
    r'(\$\d[\d,]*)\s+(?=[A-Z])'
)


def _fix_leaked_dollar_tags(line: str) -> str:
    """Re-split a multi-tag line where dollar amounts leaked across tags.

    Input pattern (tags split at wrong boundary)::

        [A>header<A]   [A>The Amount Is: At least 5 years...<A]   [A>$150 At least 10...<A]   [A>$275 ...<A]   [A>$450<A]

    Output (each row on its own line, each cell in its own tag)::

        [A>header<A]          [A>The Amount Is:<A]
        [A>At least 5 years, but less than 10 years<A]          [A>$150<A]
        [A>At least 10 years, but less than 20 years<A>          [A>$275<A]
        ...

    Works by extracting all tag-interior strings, reassembling the
    description/amount pairs, and re-emitting as properly formatted
    table rows.
    """
    # Extract all tagged segments: list of (tag_type, content)
    segments = re.findall(r'\[([AD])>([^<]*)<[AD]\]', line)
    if not segments:
        return line

    tag_type = segments[0][0]  # use the tag type from the first segment

    # Flatten all content into a single stream, preserving order
    all_content: list[str] = []
    for _tag, content in segments:
        all_content.append(content.strip())

    # Join all content, then split on dollar-amount boundaries.
    # The dollar amount always starts a new "cell" (right column).
    # Text after the dollar amount on the same piece starts a new "row"
    # (left column of the next row).
    joined = ' '.join(all_content)

    # Split into: [header_text, $amt1, row2_text, $amt2, row3_text, ...]
    # Pattern: split on ($NNN) keeping the separator
    parts = re.split(r'(\$\d[\d,.]*)', joined)
    # parts = [header_or_desc, '$150', ' desc2 ', '$275', ' desc3 ', ...]

    if len(parts) < 3:
        return line  # no dollar splits → bail

    # Build rows: pair each description with the NEXT dollar amount.
    # The first element is the header(s) / first description.
    rows: list[tuple[str, str | None]] = []
    # First part may be "header_col1 The Amount Is: desc1" — we need
    # to split the header row from the first data row.
    first_part = parts[0].strip()

    # Process in pairs: (desc_text, $amount)
    i = 0
    pending_desc = first_part
    while i < len(parts):
        if i == 0:
            pending_desc = parts[0].strip()
            i += 1
            continue
        # parts[i] should be a dollar amount, parts[i+1] (if exists)
        # is the next description
        dollar = parts[i].strip()
        next_desc = parts[i + 1].strip() if i + 1 < len(parts) else ''
        rows.append((pending_desc, dollar))
        pending_desc = next_desc
        i += 2

    # If there's trailing text without a dollar amount
    if pending_desc:
        rows.append((pending_desc, None))

    # Now check for multi-part headers. The first row's "description"
    # might contain the column headers merged together.
    # E.g. "If a Member's Creditable Service Is: The Amount Is:"
    # We need to keep the first row intact if it looks like a header.

    # Emit rows as properly formatted lines.
    # CRITICAL: join with \n\n (paragraph break) so that a subsequent
    # ``consolidate_adjacent_tags`` call (which merges ``<A]\n[A>`` but
    # respects ``<A]\n\n[A>`` as a paragraph boundary) does NOT re-merge
    # the split table rows back into a single run-on line.
    result_lines: list[str] = []
    for desc, amount in rows:
        if not desc and not amount:
            continue
        if amount:
            result_lines.append(
                f'[{tag_type}>{desc}<{tag_type}]          [{tag_type}>{amount}<{tag_type}]'
            )
        elif desc:
            result_lines.append(f'[{tag_type}>{desc}<{tag_type}]')

    if result_lines:
        return '\n\n'.join(result_lines)
    return line


def _split_in_merged_table_rows(text: str) -> str:
    """Split table rows the LLM collapsed and give each cell its own tag.

    Indiana pension/benefit tables have rows like::

        [A>At least 5 years, but less than 10 years<A]          [A>$150<A]
        [A>At least 10 years, but less than 20 years<A]          [A>$275<A]

    The LLM sometimes (a) merges all rows into one paragraph inside a
    single tag, and/or (b) keeps each row in one tag instead of giving
    the description and dollar amount separate tags, and/or (c) splits
    tags at the wrong boundaries so dollar amounts leak across tags::

        [A>header<A]  [A>$150 At least 10 years...<A]  [A>$275 ...<A]

    This function fixes all three:

    1. Tags with 3+ dollar amounts → re-split into separate rows.
       Works with both multi-space AND single-space gaps (the LLM
       often normalises whitespace to single spaces).
    2. ``[A>description   $150<A]`` → ``[A>description<A>  [A>$150<A]``.
    3. Multi-tag lines where dollar amounts leaked across tag
       boundaries → re-split into proper rows.

    Scoped to IN only by callers.
    """
    lines = text.split('\n')
    changed = False
    new_lines: list[str] = []

    for line in lines:
        stripped = line.strip()

        # ── Case 3: multi-tag line with dollar leakage ──────────────
        # Pattern: [A>header<A]   [A>$150 desc2<A]   [A>$275 desc3<A]  [A>$450<A]
        # The LLM split tags at the wrong place so each tag STARTS with
        # a $ amount that belongs to the PREVIOUS row.  Detect by
        # counting tags and checking if any tag interior starts with $.
        tag_count_a = stripped.count('[A>') + stripped.count('[D>')
        if tag_count_a >= 3:
            # Count dollar amounts across ALL tags on this line
            all_dollars = re.findall(r'\$\d[\d,]*', stripped)
            # Check for the leaked-dollar pattern: a tag whose content
            # starts with "$NNN followed by text" (i.e. "$150 At least")
            leaked = re.findall(
                r'\[[AD]>(\$\d[\d,]*)\s+([A-Z][^<]*)<[AD]\]', stripped
            )
            if len(all_dollars) >= 3 and leaked:
                result = _fix_leaked_dollar_tags(stripped)
                if result != stripped:
                    new_lines.append(result)
                    changed = True
                    continue

        # Detect single-tag lines: [A>content<A] or [D>content<D]
        tag_info = None
        for t in ('A', 'D'):
            opener = f'[{t}>'
            closer = f'<{t}]'
            if (stripped.startswith(opener) and stripped.endswith(closer)
                    and stripped.count(opener) == 1
                    and stripped.count(closer) == 1):
                tag_info = (t, stripped[len(opener):-len(closer)])
                break

        if tag_info is None:
            new_lines.append(line)
            continue

        tag, content = tag_info
        dollar_count = len(re.findall(r'\$\d[\d,]*', content))

        if dollar_count >= 3:
            # Merged table: split into rows, cell-tag each.
            # Try multi-space first; fall back to single-space.
            parts = re.split(_RE_TABLE_ROW_BOUNDARY_MULTI.pattern, content)
            if len(parts) < 3:
                # Multi-space didn't split — LLM normalised to single spaces
                parts = re.split(_RE_TABLE_ROW_BOUNDARY_SINGLE.pattern, content)

            rows: list[tuple[str, str | None]] = []
            i = 0
            while i < len(parts) - 1:
                desc = parts[i].rstrip()
                amount = parts[i + 1]
                rows.append((desc, amount))
                i += 2
            # Trailing segment (last row whose $amount had nothing after)
            if i < len(parts):
                last = parts[i].strip()
                m = re.search(r'^(.*?)\s{2,}(\$\d[\d,.]*)$', last)
                if not m:
                    # Try single-space for the last cell too
                    m = re.search(r'^(.*?)\s+(\$\d[\d,.]*)$', last)
                if m:
                    rows.append((m.group(1).rstrip(), m.group(2)))
                elif last:
                    rows.append((last, None))

            row_lines = []
            for desc, amount in rows:
                if amount:
                    row_lines.append(
                        f'[{tag}>{desc}<{tag}]          [{tag}>{amount}<{tag}]')
                else:
                    row_lines.append(f'[{tag}>{desc}<{tag}]')
            # Join with \n\n (paragraph break) so a subsequent
            # consolidate_adjacent_tags call cannot re-merge the rows.
            new_lines.append('\n\n'.join(row_lines))
            changed = True

        elif dollar_count == 1:
            # Single row: split desc from $amount if multi-space gap
            m = re.search(r'^(.*?)\s{3,}(\$\d[\d,.]*)$', content)
            if m:
                desc = m.group(1).rstrip()
                amount = m.group(2)
                new_lines.append(
                    f'[{tag}>{desc}<{tag}]          [{tag}>{amount}<{tag}]')
                changed = True
            else:
                new_lines.append(line)

        else:
            new_lines.append(line)

    if changed:
        logger.debug("Split merged table rows / cell tags in IN text")
        return '\n'.join(new_lines)
    return text


# ---------------------------------------------------------------------------
# _split_in_section_heading_welds
# ---------------------------------------------------------------------------

def _split_in_section_heading_welds(text: str) -> str:
    """Split tagged blocks at every ``Sec. N.`` boundary in IN text.

    The LLM sometimes merges multiple section definitions into a
    **single** ``[A>…<A]`` tag::

        [A>Sec. 5. … program. Sec. 6. … chapter. Sec. 7. (a) …<A]

    Or a chapter heading welded to the first section::

        [A>Chapter 16. Hoosier Homesteads Sec. 1. As used in …<A]

    The correct output keeps each ``Sec.`` as its own tagged paragraph.

    **Detection criteria** for each split point (ALL must hold):

    1. The tagged content contains ``Sec. N.`` (where N is one or more
       digits, optionally followed by a letter like ``13a``).
    2. There is meaningful text **before** ``Sec.`` (≥ 5 chars) — so
       the very first ``Sec.`` at the start of a tag is left alone.
    3. There is meaningful text **after** ``Sec.`` (≥ 20 chars).
    4. ``Sec.`` is **not** preceded by a cross-reference word like
       ``under``, ``in``, ``of``, ``by``, ``see``, ``per``, ``to``,
       ``from``, ``described``, ``provided``, ``pursuant``, ``section``,
       ``subsection`` — those indicate an inline reference, not a new
       section definition.

    The function splits at **all** valid ``Sec.`` boundaries within a
    single tag, not just the first one.

    Scoped to IN only by callers.
    """
    _REF_WORDS = re.compile(
        r'\b(?:under|in|of|by|see|per|to|from|and|or|described|provided|'
        r'amended|established|specified|mentioned|referenced|pursuant|'
        r'subsection|subdivision|section|chapter)\s*$',
        re.IGNORECASE,
    )
    _SEC_PATTERN = re.compile(r'Sec\.\s+\d+[a-zA-Z]?\.\s')

    lines = text.split('\n')
    new_lines: list[str] = []

    for line in lines:
        m_tag = re.match(r'^\[([AD])>', line)
        if not m_tag or 'Sec.' not in line:
            new_lines.append(line)
            continue

        tag_type = m_tag.group(1)
        tag_open = f'[{tag_type}>'
        tag_close = f'<{tag_type}]'

        close_idx = line.rfind(tag_close)
        if close_idx < 0:
            new_lines.append(line)
            continue

        trailing = line[close_idx + len(tag_close):]
        content = line[len(tag_open):close_idx]

        # Collect ALL valid split positions
        split_positions: list[int] = []
        for m in _SEC_PATTERN.finditer(content):
            before = content[:m.start()].rstrip()
            after = content[m.start():].strip()

            if len(before) < 5 or len(after) < 20:
                continue
            if _REF_WORDS.search(before):
                continue

            split_positions.append(m.start())

        if not split_positions:
            new_lines.append(line)
            continue

        # Split content into pieces at each valid Sec. boundary
        pieces: list[str] = []
        prev = 0
        for pos in split_positions:
            piece = content[prev:pos].rstrip()
            if piece:
                pieces.append(piece)
            prev = pos
        # Last piece: from last split to end
        last = content[prev:].strip()
        if last:
            pieces.append(last)

        # Emit each piece as its own tagged paragraph
        for i, piece in enumerate(pieces):
            if i > 0:
                new_lines.append('')  # blank line separator
            if i == len(pieces) - 1:
                new_lines.append(f"{tag_open}{piece}{tag_close}{trailing}")
            else:
                new_lines.append(f"{tag_open}{piece}{tag_close}")

        logger.debug(
            "Split section-heading weld into %d pieces at Sec. markers",
            len(pieces),
        )

    return '\n'.join(new_lines)


# ---------------------------------------------------------------------------
# split_numbered_item_paragraph_welds
# ---------------------------------------------------------------------------

def split_numbered_item_paragraph_welds(text: str) -> str:
    """Split tagged lines where a numbered item got welded to the next paragraph.

    The LLM sometimes merges a short numbered-item clause with the
    substantive paragraph that follows it into a **single** ``[A>…<A]``
    (or ``[D>…<D]``) tag::

        [A>(2) interview each new executive committee member. The chair
        of the judicial selection committee shall consider the feedback
        of incumbent executive committee members …<A]

    Or with a semicolon boundary (common in legal lists)::

        [A>(2) nonresidential onsite sewage systems (as defined in
        IC 16-19-3.5-3.5); since 2023 shall continue to allow …<A]

        [A>(C) specific location; of a grease trap or grease
        interceptor if a regulated entity has the properly sized …<A]

    The correct output keeps them as separate tagged paragraphs.

    **Detection criteria** (ALL must hold):

    1. The tagged content starts with a parenthesised marker —
       ``(1)``, ``(a)``, ``(iv)``, ``(B)`` — identifying it as a
       numbered or lettered item.
    2. A clause-ending punctuation mark appears inside the content:
       - **Period** followed by spaces + capitalised word ``[A-Z][a-z]``
         (guards against citation periods like ``IC 35-43-5-3.8``).
       - **Semicolon** followed by spaces + any letter ``[a-zA-Z]``
         (semicolons don't appear in citation numbers, so lowercase
         continuation is safe to match — e.g. ``; since``, ``; of``).
    3. The text *before* the split (the numbered item) is short
       (≤ 120 characters).
    4. The text *after* the split (the following paragraph) is long
       enough — **≥ 100 chars** for period splits, **≥ 40 chars** for
       semicolon splits (semicolons are much stronger list-item
       delimiters in legal text).
    5. The total tagged content exceeds **200 chars** for period splits
       or **80 chars** for semicolon splits.
    6. For semicolon splits, the continuation must NOT start with a
       conjunction (and/or/but/nor) — that signals a clause
       continuation, not a new paragraph.

    These guards prevent false splits on:

    * Multi-sentence numbered items where the second sentence is still
      part of the same item (total length < 200, or item part > 120).
    * Citation periods like ``IC 35-43-5-3.8`` — the character after
      the dot is a digit, not ``[A-Z][a-z]``.
    * Semicolons between list sub-items like ``; (1)`` or ``; and`` —
      the ``(`` is not a letter, and ``and`` is typically short enough
      that the length guards prevent a split.
    * Inline numeric parentheticals like ``one (1) member`` — no
      period or semicolon precedes the paren.

    The function is idempotent: once a tag is split, neither fragment
    matches the combined criteria, so re-running is a no-op.
    """
    # Split-point patterns:
    #   1. Period + capital letter (sentence boundary):  ". The", ". A"
    #   2. Semicolon + any letter (clause boundary):     "; since", "; of", "; A"
    # Both use lookahead so sp.start() lands on the . or ;
    _SPLIT_POINT = re.compile(
        r'\.\s+(?=[A-Z][a-z])'    # period → require capital letter start
        r'|'
        r';\s+(?=[a-zA-Z])'       # semicolon → any letter is OK
    )

    lines = text.split('\n')
    new_lines: list[str] = []

    for line in lines:
        # Quick check: must start with a tag opener + parenthesised marker
        m_tag = re.match(r'^\[([AD])>\(([a-zA-Z0-9]{1,4})\)\s+', line)
        if not m_tag or len(line) < 80:
            new_lines.append(line)
            continue

        # Guard: single lowercase letter markers — (a), (b), … (z) — are
        # *subsection* identifiers in IN legal formatting, not list items.
        # Their content is a complete multi-sentence provision and must
        # never be split at sentence boundaries.  Numbered markers (1),
        # (2), … and uppercase letters (A), (B), … are genuine list items
        # and remain eligible for splitting.
        marker = m_tag.group(2)
        if re.match(r'^[a-z]$', marker):
            new_lines.append(line)
            continue

        tag_type = m_tag.group(1)          # 'A' or 'D'
        tag_open = f'[{tag_type}>'
        tag_close = f'<{tag_type}]'

        # Find the LAST occurrence of the close tag — allows trailing
        # punctuation like <A]". in committee-report quoted text.
        close_idx = line.rfind(tag_close)
        if close_idx < 0:
            new_lines.append(line)
            continue

        trailing = line[close_idx + len(tag_close):]   # e.g. '".', '"', ''
        content = line[len(tag_open):close_idx]

        # Walk candidate split points (first match wins)
        split_done = False
        for sp in _SPLIT_POINT.finditer(content):
            item_part = content[: sp.start() + 1]    # up to and including the . or ;
            para_part = content[sp.start() + 1 :].lstrip()  # skip whitespace

            # Semicolons are strong list-item delimiters in legal text,
            # so use relaxed thresholds.  Periods need stricter guards
            # because they appear in citations and abbreviations.
            is_semicolon = content[sp.start()] == ';'
            if is_semicolon:
                min_para = 40
                # Guard: if continuation starts with a conjunction, it's
                # a clause continuation, not a new paragraph.
                if re.match(r'(?:and|or|but|nor)\s', para_part, re.IGNORECASE):
                    continue
            else:
                min_para = 100
                # Period splits require longer total content
                if len(line) < 200:
                    continue

            if len(item_part) <= 120 and len(para_part) >= min_para:
                new_lines.append(f"{tag_open}{item_part}{tag_close}")
                new_lines.append("")                  # blank-line separator
                new_lines.append(f"{tag_open}{para_part}{tag_close}{trailing}")
                split_done = True
                logger.debug(
                    "Split numbered-item (%s) weld: item=%d chars, para=%d chars",
                    m_tag.group(2), len(item_part), len(para_part),
                )
                break

        if not split_done:
            new_lines.append(line)

    return '\n'.join(new_lines)


def rejoin_amendment_intro_breaks(text: str) -> str:
    """
    Re-join LLM-inserted paragraph breaks between a **Kansas** amendment
    intro phrase and the statute body that follows.

    Background
    ----------
    Kansas bills introduce a code section being modified with a phrase
    like::

        Sec. 3. K.S.A. 40-2,118 is hereby amended to read as follows:
        40-2,118. (a) For purposes of this act ...

    In the PDF (and in the raw PyMuPDF extraction) these two parts sit
    on consecutive **lines of the same paragraph** — the second line is
    just a soft wrap, not a paragraph break.  But when Claude's clean
    pass reformats the text, it often *misreads* the structure and
    inserts a real paragraph break (``\\n\\n``) before the statute
    number, producing::

        Sec. 3. K.S.A. 40-2,118 is hereby amended to read as follows:

        40-2,118. (a) For purposes of this act ...

    That extra blank line breaks the visual flow and doesn't match the
    PDF.

    Scope: Kansas only
    ------------------
    This function is **deliberately scoped to Kansas-style intros** so
    that it cannot disturb the existing, working output for California,
    Connecticut or any other state.  The two KS-specific anchors are:

    1. The token ``"K.S.A."`` (Kansas Statutes Annotated) must appear in
       the same line as ``"amended to read"`` — that is the canonical
       way every KS bill identifies the section being amended.
    2. The statute number that follows must be in **KS hyphenated
       format** ``\\d+-\\d+(?:,\\d+)?(?:[a-z])?\\.`` — for example
       ``40-2,118.``, ``65-1501.``, ``65-1509a.`` or ``72-3404.``.
       CA-style decimal numbers like ``234.7.`` or ``2181.5.`` are
       intentionally **not** matched.

    Detection
    ---------
    The pattern requires **all** of:

    1. ``K.S.A.`` somewhere on the same line as the intro phrase.
    2. ``"amended (and reenacted) to read [as follows]:"`` (case-
       insensitive) at the end of that line.
    3. A paragraph break (``\\n\\n``, possibly with surrounding
       whitespace).
    4. A KS-format statute number (hyphenated, mandatory trailing
       ``.``).
    5. A single whitespace (so the body must have content after the
       statute number — we never merge into an empty line).

    Replacement: collapse ``"…follows:\\n\\n40-2,118. ..."`` →
    ``"…follows: 40-2,118. ..."`` (single space).

    Why this is safe for non-Kansas output
    --------------------------------------
    - California / Connecticut / federal bills do **not** use
      ``K.S.A.`` — that token is a Kansas exclusive.
    - The statute-number regex requires a hyphen, which CA decimal
      numbers (``234.7.``) don't have.
    - Both anchors must match *together* — there is no realistic way
      for a non-KS document to satisfy both.
    """
    pattern = re.compile(
        # KS-specific intro anchor: "K.S.A." appears on the same line
        # as the "amended to read..." phrase, ending in a colon.
        r'(K\.S\.A\.[^\n]*?'
        r'amended\s+(?:and\s+reenacted\s+)?to\s+read'
        r'(?:\s+as\s+follows)?\s*:)'
        # The false paragraph break we want to remove.
        r'\s*\n\s*\n\s*'
        # KS-style statute number — hyphenated, mandatory trailing ".".
        # Examples: 40-2,118.  65-1501.  65-1509a.  72-3404.
        # CA-style decimal numbers like 234.7. are intentionally NOT
        # matched (no hyphen).
        r'(\d{1,4}-\d{1,4}[a-zA-Z]?(?:,\d{1,4})?\.)(\s)',
        re.IGNORECASE,
    )
    return pattern.sub(r'\1 \2\3', text)


def rejoin_inline_renumber_list_breaks(text: str) -> str:
    """
    Re-join LLM-inserted false paragraph breaks between a deletion-marker
    tag (``[D>…<D]``) and an addition-marker tag (``[A>…<A]``) when the
    addition marker is followed by **inline content** on the same line.

    Used by states whose drafting convention keeps the renumber pair on
    the same line as the body prose:

    - **Iowa** — numeric subsection renumbers like ``[D>1.<D] [A>2.<A]``.
    - **Delaware** — parenthesised paragraph renumbers like
      ``[D>(6)<D] [A>(4)<A]`` or ``[D>(b)<D] [A>(c)<A]``.

    Background
    ----------
    Both Iowa and Delaware bills place the renumber pair *inline* with
    the running text::

        Raw PyMuPDF (correct, IA):
            [D>1.<D] [A>2.<A] A person who is the parent, guardian, …

        Raw PyMuPDF (correct, DE):
            [D>(6)<D] [A>(4)<A] Reimbursement of any unreimbursed costs, …

    The LLM clean pass routinely misreads these tag pairs as header
    lines and inserts a real paragraph break (``\\n\\n``)::

        After LLM (wrong):
            [D>1.<D]

            [A>2.<A] A person who is the parent, guardian, …

            [D>(6)<D]

            [A>(4)<A] Reimbursement of any unreimbursed costs, …

    Detection
    ---------
    The pattern matches **only** when:

    1. ``[D>…<D]`` containing either:
       * purely-numeric marker  ``\\d+\\.``                e.g. ``1.``
       * parenthesised marker   ``\\(…\\)`` (1-4 alphanumeric chars)
                                                            e.g. ``(6)``,
                                                            ``(b)``, ``(iv)``
    2. A paragraph break (``\\n\\n``).
    3. ``[A>…<A]`` with the **same** marker shape (numeric *or*
       parenthesised — they don't have to match each other; e.g.
       ``[D>(6)<D]\\n\\n[A>(4)<A]`` is a legitimate DE renumber).
    4. **Inline** whitespace and content immediately follow — i.e. the
       lookahead ``(?=[ \\t]+\\S)`` requires a space/tab plus a non-
       whitespace character on the same line.

    The lookahead is the key safety property.  It distinguishes inline
    list renumbers from California-style standalone code-section
    renumbers::

        [D>2182.<D]

        [A>2183.<A] This chapter shall become …

    where the layout in the source PDF puts the section number on its
    own header line and a blank line separates the addition tag from
    the body.  But the lookahead alone wouldn't be sufficient — when
    inline parenthesised renumbers like ``[D>(c)<D]\\n\\n[A>(d)<A] A``
    appear in a CA file, the regex would match.  That's why the **call
    site is also gated** by ``state_code in {"IA", "DE"}`` — the
    function is never even invoked for CA text.  Belt-and-suspenders.

    Why state-scoping matters
    -------------------------
    California uses the *opposite* convention: each renumber pair sits
    alone on its own paragraph above the body (created on purpose by
    ``split_renumber_paragraph_breaks``).  Running this rejoin against
    CA / TX / CT bills would silently undo that layout.  IA and DE are
    the two states that keep the renumber pair inline; we add other
    states only after empirical confirmation.
    """
    # Marker shapes (alternation):
    #   • Numeric:        1.  10.  42.            (IA convention)
    #   • Parenthesised:  (a)  (b)  (6)  (iv)     (DE convention)
    marker = r'(?:\d+\.|\(\s*[a-zA-Z0-9]{1,4}\s*\))'

    pattern = re.compile(
        r'(\[D>\s*' + marker + r'\s*<D\])'   # \1: deletion marker tag
        r'\s*\n\s*\n\s*'                      # paragraph break to remove
        r'(\[A>\s*' + marker + r'\s*<A\])'   # \2: addition marker tag
        r'(?=[ \t]+\S)',                      # lookahead: inline whitespace + content
    )
    return pattern.sub(r'\1 \2', text)


# ---------------------------------------------------------------------------
# Split merged grant / appropriation tag rows
# ---------------------------------------------------------------------------
_RE_DOLLAR_AMT_SPLIT = re.compile(r'\$\s*[\d,]+(?:\.\d{2})?')


def _split_merged_grant_tag_rows(text: str) -> str:
    """Split ``[D>…<D]`` / ``[A>…<A]`` tags that contain multiple line items.

    Problem
    -------
    When the LLM cleans a tabular appropriations bill (e.g. DE Grants-in-
    Aid), it sometimes merges **consecutive same-tag rows** into a single
    tag span::

        [D>Adult Basic Education $ 40,000 Child Care (95-01-06) $ 750,000<D]

    The raw extraction had them on separate lines with separate tags, but
    the LLM treated consecutive deletions/additions as one paragraph.

    Fix
    ---
    Detect tags whose content contains **one or more** ``$ amount``
    patterns.  At each ``$ amount``, if the next non-whitespace
    character is an uppercase letter or ``(`` (the start of a new entity
    name or account code), place a split there and give each piece its
    own tag on its own line::

        [D>Adult Basic Education $ 40,000<D]
        [D>Child Care (95-01-06) $ 750,000<D]

    Also handles dollar-at-start tags (IN pension tables)::

        [A>$150 At least 10 years, but less than 20 years<A]
        → [A>$150<A]
          [A>At least 10 years, but less than 20 years<A]

    Safety
    ------
    - Only splits when the text after a ``$ amount`` starts with an
      uppercase letter or ``(`` — never in mid-sentence lowercase
      continuations like ``$ 500 per year``.
    - Pure dollar tags (``[A>$450<A]``) have no text after the
      amount, so the loop skips them.
    - Legislative prose tags almost never START with a dollar amount
      followed by a new entity name.
    """

    _TAG_RE = re.compile(r'\[([AD])>(.*?)<\1\]')

    def _split_tag(m: re.Match) -> str:
        tag_type = m.group(1)   # 'A' or 'D'
        content  = m.group(2)

        amounts = list(_RE_DOLLAR_AMT_SPLIT.finditer(content))
        if len(amounts) < 1:
            return m.group(0)           # no dollar amounts at all

        pieces: list[str] = []
        last_cut = 0

        for i in range(len(amounts)):
            after = amounts[i].end()
            tail  = content[after:]
            stripped = tail.lstrip()
            if not stripped:
                continue
            ws   = len(tail) - len(stripped)
            cut  = after + ws           # first non-WS char position

            # Split only when the next text starts a new entity
            if stripped[0].isupper() or stripped[0] == '(':
                pieces.append(content[last_cut:cut].rstrip())
                last_cut = cut

        # Remainder after the last split
        remainder = content[last_cut:].strip()
        if remainder:
            pieces.append(remainder)

        if len(pieces) < 2:
            return m.group(0)

        return '\n'.join(f'[{tag_type}>{p}<{tag_type}]' for p in pieces)

    return _TAG_RE.sub(_split_tag, text)


# ---------------------------------------------------------------------------
# Split tabular tags at column boundaries (description + dollar amount)
# ---------------------------------------------------------------------------
# Regex: text label (letters/spaces/punctuation) followed by a dollar amount
# at the END of the tag content.  The text label must be ≥ 4 characters
# (avoids splitting tags that are purely dollar amounts like "$ 3,880,543").
_RE_TABULAR_LABEL_THEN_DOLLAR = re.compile(
    r'^([A-Za-z][\w\s,.\-:;\u2013\u2014\'&/()\[\]]{3,}?)'  # text label (≥4 chars, non-greedy; includes : and ;)
    r'\s+'                                                   # whitespace gap
    r'(\$\s*[\d,]+(?:\.\d{2})?)\s*$'                        # trailing dollar amount
)

# Target output column where dollar amounts should start in split tabular
# tags.  Chosen so the longest typical DE description (~85 chars with tags)
# still gets a visible gap, while shorter labels get generous padding that
# makes the column alignment obvious.
TABULAR_AMOUNT_TARGET_COL = 90

# Prose-safety guard for _split_tabular_tag_at_dollar.  If the LAST word
# of the label text (before the dollar amount) is a common function word
# / verb / preposition, the tag is almost certainly narrative prose
# ("a penalty not to exceed $5,000") rather than a tabular cell
# ("At least 30 years $375").  Prevents false-positive splits on IN
# statutory text while still working for DE appropriation table labels
# (which always end with nouns like "Secretary", "Education", "Package").
_PROSE_BEFORE_DOLLAR = frozenset({
    'exceed', 'exceeding', 'of', 'pay', 'pays', 'paying', 'paid',
    'is', 'was', 'were', 'be', 'been', 'being',
    'than', 'to', 'at', 'for', 'from', 'not', 'over', 'under',
    'about', 'approximately', 'totaling', 'costing', 'worth',
})


def _split_tabular_tag_at_dollar(text: str) -> str:
    """Split ``[A>…<A]`` / ``[D>…<D]`` tags that span a description + dollar amount.

    Problem
    -------
    When an entire table row is underlined / struck through, PyMuPDF wraps
    the *full row* in a single tag::

        [A>TOTAL - Office of the Secretary $ 3,880,543<A]

    The text label (column 1–2) and the dollar amount (column 3) are
    conceptually in different table columns and should have separate tags::

        [A>TOTAL - Office of the Secretary<A]                                [A>$ 3,880,543<A]

    Alignment
    ---------
    The dollar-amount tag is placed at ``TABULAR_AMOUNT_TARGET_COL`` (90)
    from the start of the line, with a minimum gap of
    ``COLUMN_GAP_MIN_SPACES`` (6).  This ensures:

    - Short labels get generous padding so the column is obvious.
    - Long labels still get at least 6 spaces of separation.
    - Dollar amounts roughly align across rows.

    Safety
    ------
    - Only matches when the tag ends with a dollar amount preceded by
      a text label — never splits "$ 3,000 per year" (no leading label).
    - Tags that are purely dollar amounts ("$ 3,880,543") are untouched.
    - The gap is always ≥ ``COLUMN_GAP_MIN_SPACES`` (6) so the downstream
      merge passes (which respect the 6-space boundary) will NOT
      re-merge them.
    """
    _TAG_RE = re.compile(r'\[([AD])>(.*?)<\1\]', re.DOTALL)

    def _split(m: re.Match) -> str:
        tag_type = m.group(1)
        content  = m.group(2).strip()

        match = _RE_TABULAR_LABEL_THEN_DOLLAR.match(content)
        if not match:
            return m.group(0)

        label  = match.group(1).rstrip()
        amount = match.group(2)

        # Prose safety: if the label ends with a function word (verb,
        # preposition, article), the tag is likely narrative prose
        # ("not to exceed $5,000") rather than a table cell.  Skip.
        last_word = label.rsplit(None, 1)[-1].lower() if label else ''
        if last_word in _PROSE_BEFORE_DOLLAR:
            return m.group(0)

        label_tag  = f'[{tag_type}>{label}<{tag_type}]'
        amount_tag = f'[{tag_type}>{amount}<{tag_type}]'

        # Pad to target column, with a minimum gap
        gap_size = max(
            COLUMN_GAP_MIN_SPACES,
            TABULAR_AMOUNT_TARGET_COL - len(label_tag),
        )
        return f'{label_tag}{" " * gap_size}{amount_tag}'

    return _TAG_RE.sub(_split, text)


# ---------------------------------------------------------------------------
# Split tabular header-colon-data tags (e.g. IN pension table headers)
# ---------------------------------------------------------------------------
# Matches a tag where a short header (≤30 chars) ending with ':' has been
# merged with data row text (≥8 chars, starts uppercase).  Only triggers
# when followed on the SAME LINE by a same-type dollar-amount tag (via
# lookahead) — prevents false positives on prose tags like
# [A>Citations Affected: IC 5-10; noncode.<A].
_RE_HEADER_COLON_BEFORE_DOLLAR = re.compile(
    r'\[([AD])>'
    r'([^<]{3,30}?)'
    r'\s*:\s+'
    r'([A-Z][^<]{8,}?)\s*'
    r'<\1\]'
    r'(?=\s{2,}\[\1>\$)'   # lookahead: same-type dollar tag with 2+ space gap
)


def _split_tabular_header_from_data(text: str) -> str:
    """Split ``[T>Header: Data Row Text<T]`` tags when followed by a dollar tag.

    Problem
    -------
    In IN pension tables, the column header ending with ``:`` gets merged
    with the first data row's text::

        [A>The Amount Is: At least 5 years, but less than 10 years<A]  [A>$150<A]

    Fix
    ---
    Split at the colon boundary::

        [A>The Amount Is:<A]
        [A>At least 5 years, but less than 10 years<A]  [A>$150<A]

    Safety
    ------
    - Only splits when the SAME LINE has a same-type dollar-amount tag
      after the colon tag (lookahead).  Prose tags like
      ``[A>Effective: July 1, 2026<A]`` are NOT followed by dollar tags,
      so the lookahead fails and they stay unchanged.
    - Header text is limited to ≤ 30 chars to avoid matching long prose.
    - Data text must start with uppercase and be ≥ 8 chars.
    """
    def _split(m: re.Match) -> str:
        tag = m.group(1)     # 'A' or 'D'
        header = m.group(2).rstrip()
        data = m.group(3).rstrip()
        # Use \n\n (paragraph break) so the downstream
        # consolidate_adjacent_tags / merge_split_tags calls in
        # streamlit_app.py cannot re-merge header and data.  Those
        # functions only merge across a single \n, never \n\n.
        return f'[{tag}>{header}:<{tag}]\n\n[{tag}>{data}<{tag}]'

    return _RE_HEADER_COLON_BEFORE_DOLLAR.sub(_split, text)


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
    state_code: Optional[str] = None,
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

        # --- CT-only: PRE-STRIP footer/header separator rules by stroke width ---
        if state_code and state_code.upper() == "CT":
            hlines = _strip_ct_footer_separator_hlines(hlines, page)

        # --- filter out table-border lines ---
        table_zones = _detect_table_zones(page)
        if table_zones:
            hlines = _filter_table_border_hlines(hlines, table_zones)

        # --- CT-only: legacy multi-segment separator filter (edge cases) ---
        if state_code and state_code.upper() == "CT":
            hlines = _filter_page_separator_hlines(hlines, page)

        # --- AR-only: filter typographic leader / decorative rules ---
        if state_code and state_code.upper() == "AR":
            hlines = _filter_ar_leader_lines(hlines, page)

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
            # Mirror the visual-order sort applied in _extract_page_tagged_text
            # so the JSON report's formatted_spans + context strings match the
            # left-to-right reading order (e.g. "$ 3,880,543" not
            # "3,880,543 $" for currency cells in tabular rows).
            for tl in _sort_block_lines_visually(block.get("lines", [])):
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

            # Line-level metrics for superscript detection (see
            # SUPERSCRIPT_SIZE_RATIO / SUPERSCRIPT_RAISE_PT for thresholds).
            line_bbox_for_super  = tl.get("bbox", (0, 0, 0, 0))
            line_dominant_size   = _line_dominant_font_size(tl)

            # Build list of (char, format_set, char_bbox) for the entire line
            char_entries: List[Tuple[str, frozenset, Tuple[float, float, float, float]]] = []

            for sp in spans:
                chars_data = sp.get("chars", [])
                font_name  = sp.get("font", "")
                font_size  = sp.get("size", 12)
                sp_flags   = sp.get("flags", 0)
                sp_italic  = bool(sp_flags & 2)
                sp_bold    = bool(sp_flags & 16)

                # Footnote-reference superscript: emit characters as Unicode
                # superscripts and drop all format flags — these are pure
                # typography, not legal additions/deletions/italics.
                sp_is_super = _span_is_superscript(
                    sp, line_dominant_size, line_bbox_for_super,
                )

                if not chars_data:
                    # dict-mode fallback: classify entire span as one unit
                    s_bbox = tuple(sp.get("bbox", (0, 0, 0, 0)))
                    if sp_is_super:
                        for c in sp.get("text", ""):
                            char_entries.append(
                                (_to_superscript(c), frozenset(), s_bbox)
                            )
                        continue
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
                    if sp_is_super:
                        char_entries.append(
                            (_to_superscript(c), frozenset(), bbox)
                        )
                        continue
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
    state_code: Optional[str] = None,
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
            state_code=state_code,
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
