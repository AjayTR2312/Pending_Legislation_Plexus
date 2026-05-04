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

# Tag delimiters (match the project convention)
ADD_OPEN, ADD_CLOSE = "[A>", "<A]"
DEL_OPEN, DEL_CLOSE = "[D>", "<D]"


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

        # --- block break: close tags, insert paragraph gap ---
        if not first_block:
            if in_add:
                output.append(ADD_CLOSE); in_add = False
            if in_del:
                output.append(DEL_CLOSE); in_del = False
            output.append("\n\n")
        first_block = False

        first_line_in_block = True
        for line in block.get("lines", []):
            if not first_line_in_block:
                # Line break within same block — keep tags open if
                # decoration continues on the next line.
                output.append("\n")
            first_line_in_block = False

            for span in line.get("spans", []):
                chars_data = span.get("chars", [])
                font_size  = span.get("size", 12)
                font_name  = span.get("font", "")
                span_text  = span.get("text", "")

                # --- Detect italic (bit 1) and bold (bit 4) from span flags ---
                span_flags = span.get("flags", 0)
                span_is_italic = bool(span_flags & 2)
                span_is_bold   = bool(span_flags & 16)

                # --- Fallback: no char-level data → use span bbox ---
                if not chars_data:
                    if span_text:
                        s_bbox = tuple(span.get("bbox", (0, 0, 0, 0)))
                        s_ul, s_so = _classify_char_decoration(
                            s_bbox, font_size, ul_rects, so_rects,
                            hlines, font_name,
                        )
                        need_add = (s_ul and mark_underline) or (span_is_italic and mark_italic) or (span_is_bold and mark_bold)
                        need_del = s_so and mark_strikethrough
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

                    need_add = (is_ul and mark_underline) or (span_is_italic and mark_italic) or (span_is_bold and mark_bold)
                    need_del = is_so and mark_strikethrough

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
_RE_MERGE_ADD = re.compile(r"<A\]\s*\[A>")
_RE_MERGE_DEL = re.compile(r"<D\]\s*\[D>")
# Matches margin line numbers at start of a line
_RE_LINE_NUMBER = re.compile(r"^\s*line\s+\d+\s*", re.MULTILINE)
# Matches margin line numbers sandwiched between tag boundaries
# e.g.  "<A] line 40 [A>"  or  "<A]\n line 6\n"  (across lines)
_RE_LINE_NUMBER_BETWEEN_TAGS = re.compile(
    r"(<[AD]\])\s*line\s+\d+\s*", re.MULTILINE
)


def _clean_tagged_text(text: str, strip_line_numbers: bool = False) -> str:
    """Remove empty tags, merge adjacent same-type tags, tidy whitespace."""
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
    # Collapse excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


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
    """
    return _RE_BRACKET_DEL.sub(r"[D>[\1]<D]", text)


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
        )
        clean = _clean_tagged_text(raw_text, strip_line_numbers=strip_line_numbers)
        if mark_brackets:
            clean = _tag_brackets(clean)
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
🛡️  PRESERVE section / subsection numbering ("Section 1.", "(a)", "(2)", …).
🛡️  When in doubt whether something is content or artefact → **KEEP IT**.

══════════════════════════════════════════════════════════════
OUTPUT FORMAT
══════════════════════════════════════════════════════════════

• Return ONLY the cleaned text — no commentary, no markdown fences.
• Reflow lines naturally into paragraphs; collapse excessive blank lines
  to a single blank line between paragraphs.
• Do NOT reword, paraphrase, or reorder anything.
"""

# Directory containing state-specific clean prompts
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "data" / "Prompts"


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


def clean_extracted_text(
    raw_text: str,
    *,
    state_code: str | None = None,
    anthropic_client=None,
    model: str = "claude-opus-4-7",
) -> str:
    """
    Use Claude Opus 4.7 to remove PDF artefacts (headers, footers, line
    numbers, page markers) from PyMuPDF-extracted text while preserving
    all legislative content and ``[A>…<A]`` / ``[D>…<D]`` tags.

    Uses **state-specific prompts** from ``data/Prompts/{ST}_Clean_Prompt.txt``
    when available, otherwise falls back to a universal prompt.

    Parameters
    ----------
    raw_text : str
        The raw text produced by ``extract_pdf_with_pymupdf(join_pages=True)``.
    state_code : str, optional
        Two-letter state code (e.g. "CT", "FL", "KS").  Used to load
        the matching prompt file.
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

    logger.info(
        "🧹 Cleaning extracted text with %s (state=%s) …",
        model, state_code or "universal",
    )

    response = anthropic_client.messages.create(
        model=model,
        max_tokens=16384,
        system=system_prompt,
        messages=[
            {"role": "user", "content": raw_text},
        ],
    )

    cleaned = response.content[0].text.strip()

    logger.info(
        "✅ Cleaned text: %d → %d chars",
        len(raw_text), len(cleaned),
    )

    # ── consolidate adjacent tags split by PDF line breaks ────────────
    cleaned = consolidate_adjacent_tags(cleaned)

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
    """
    import re as _re
    # Merge adjacent [A>…<A] tags
    _adj_a = _re.compile(r'<A\]\s+\[A>')
    while _adj_a.search(text):
        text = _adj_a.sub(' ', text)
    # Merge adjacent [D>…<D] tags
    _adj_d = _re.compile(r'<D\]\s+\[D>')
    while _adj_d.search(text):
        text = _adj_d.sub(' ', text)
    return text


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
