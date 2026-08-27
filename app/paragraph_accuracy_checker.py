"""
paragraph_accuracy_checker.py — Structural/Semantic Paragraph Accuracy Validator
=================================================================================

Compares the final output .txt file against the PDF's paragraph structure
(detected via PyMuPDF line-level x0 indentation) to find places where the
LLM cleaning merged paragraphs that should be separate.

This is a POST-LLM validation step that runs on the final output file
and reports paragraph merge errors so they can be flagged in the UI.

Usage:
    from paragraph_accuracy_checker import check_paragraph_accuracy

    result = check_paragraph_accuracy(pdf_path, final_text, state_code="IA")
    print(f"Paragraph accuracy: {result['accuracy_pct']:.1f}%")
    for issue in result['merge_errors']:
        print(f"  Line {issue['line']}: {issue['description']}")

How it works:
    1. Extract paragraph boundaries from the PDF using x0 indentation
       (first-line indent vs continuation indent).
    2. For each PDF paragraph boundary, check if the final .txt also
       has a paragraph break (blank line or newline) at that point.
    3. Report missing breaks as "merge errors".
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import fitz
except ImportError:
    fitz = None

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# PDF Paragraph Boundary Extraction
# ═══════════════════════════════════════════════════════════════════

@dataclass
class ParagraphBoundary:
    """A detected paragraph start in the PDF."""
    page: int               # 0-based page index
    line_index: int         # line index within page content
    x0: float              # left indent of this line
    prev_x0: float         # left indent of previous line
    first_words: str       # first ~60 chars of this paragraph
    signal: str            # what triggered detection: "indent", "bold", "sec", "gap"


# Vertical tolerance (points) for treating two text segments as the same row.
_ROW_TOL = 3.0

# One or more enumeration markers opening a line: "6. ", "a. (1) ", "(c) ", "127482. "
_LIST_MARKER_START_RE = re.compile(
    r'^(?:[0-9]{1,7}[A-Za-z]?\.|\([0-9A-Za-z]{1,4}\)|[A-Za-z]{1,4}\.)+\s'
)


def _page_content_rows(page) -> List[Dict[str, Any]]:
    """Flatten a PDF page into y-ordered content rows, minus margin numbering.

    Word-level geometry is used rather than the block/line tree because many
    bill PDFs emit one block per physical line, and some (California) fuse the
    margin line label into the same span as the body text
    (``" line "`` + ``"21 shall not charge..."``), which corrupts both the
    indentation analysis and the text used for matching.
    """
    words = page.get_text("words")  # (x0, y0, x1, y1, word, block, line, word_no)
    if not words:
        return []

    words = sorted(words, key=lambda w: (w[1], w[0]))

    raw_rows: List[List[Any]] = []
    group: List[Any] = []
    for w in words:
        if group and w[1] - group[0][1] > _ROW_TOL:
            raw_rows.append(sorted(group, key=lambda g: g[0]))
            group = []
        group.append(w)
    if group:
        raw_rows.append(sorted(group, key=lambda g: g[0]))

    raw_rows = _strip_margin_numbering(raw_rows)

    bold_starts = _bold_row_starts(page)

    rows: List[Dict[str, Any]] = []
    for row in raw_rows:
        if not row:
            continue
        x0 = row[0][0]
        y0 = min(w[1] for w in row)
        y1 = max(w[3] for w in row)
        is_bold = any(
            abs(bx - x0) <= 2.0 and y0 - _ROW_TOL <= by <= y1 + _ROW_TOL
            for bx, by in bold_starts
        )
        rows.append({
            "x0": x0,
            "x1": max(w[2] for w in row),
            "y0": y0,
            "y1": y1,
            "text": " ".join(w[4] for w in row),
            "font": "Bold" if is_bold else "",
        })
    return rows


def _strip_margin_numbering(raw_rows: List[List[Any]]) -> List[List[Any]]:
    """Drop left-margin line numbering, e.g. "76" or "line 21".

    Detection is relative rather than positional: right-aligned numbering
    shifts x0 by digit count, so a fixed margin threshold splits a page into
    stripped and unstripped halves. A column is only removed when it prefixes
    most rows and its numbers increase down the page, so a body row that
    happens to start with a number ("127481.") is never truncated.
    """
    rows = [r for r in raw_rows if r]
    if len(rows) < 5:
        return raw_rows

    label_rows = sum(1 for r in rows if r[0][4].lower().strip() == "line")
    if label_rows >= len(rows) * 0.5:
        out = []
        for row in raw_rows:
            if row and row[0][4].lower().strip() == "line":
                row = row[1:]
                if row and row[0][4].rstrip('.').isdigit():
                    row = row[1:]
            out.append(row)
        return out

    def _is_margin_number(row) -> bool:
        # A bare number separated from the body by a visible gap.
        return (
            len(row) >= 2
            and row[0][4].isdigit()
            and len(row[0][4]) <= 4
            and row[1][0] - row[0][2] >= 6.0
        )

    numbered = [r for r in rows if _is_margin_number(r)]
    if len(numbered) < len(rows) * 0.5:
        return raw_rows

    values = [int(r[0][4]) for r in numbered]
    ascending = sum(1 for a, b in zip(values, values[1:]) if b > a)
    if len(values) > 1 and ascending < (len(values) - 1) * 0.8:
        return raw_rows  # not a line-number column

    return [row[1:] if row and _is_margin_number(row) else row for row in raw_rows]


def _bold_row_starts(page) -> List[Tuple[float, float]]:
    """(x0, y-centre) of every bold span, used to flag bold-leading rows."""
    out: List[Tuple[float, float]] = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                if "Bold" in span.get("font", "") and span["text"].strip():
                    bbox = span["bbox"]
                    out.append((bbox[0], (bbox[1] + bbox[3]) / 2))
    return out


def extract_paragraph_boundaries(
    pdf_path: str | Path,
    *,
    pages: Optional[List[int]] = None,
) -> List[ParagraphBoundary]:
    """Extract paragraph boundary positions from a PDF using layout analysis.

    Detects paragraph starts by looking at:
    - x0 indentation jumps (continuation line → first-line indent)
    - Bold font changes (section titles)
    - "Sec." / numbered subsection prefixes
    - Vertical gaps larger than normal line spacing

    Parameters
    ----------
    pdf_path : str or Path
        Path to the PDF file.
    pages : list[int], optional
        0-based page indices to analyze. None → all pages.

    Returns
    -------
    list[ParagraphBoundary]
        Detected paragraph boundaries in document order.
    """
    if fitz is None:
        logger.warning("PyMuPDF not available — cannot extract paragraph boundaries")
        return []

    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        return []

    doc = fitz.open(str(pdf_path))
    page_indices = pages if pages is not None else range(len(doc))
    boundaries: List[ParagraphBoundary] = []

    for pg_idx in page_indices:
        if pg_idx < 0 or pg_idx >= len(doc):
            continue
        page = doc[pg_idx]

        content_lines = _page_content_rows(page)

        # Analyze indentation patterns to find the two primary indent levels
        if len(content_lines) < 3:
            continue

        # Determine the dominant indent levels for this page. Only rows with
        # real body text vote, so running heads, page numbers and stray margin
        # labels cannot outweigh the actual text column.
        from collections import Counter
        body_rows = [r for r in content_lines if len(r["text"].split()) >= 3]
        if len(body_rows) < 3:
            continue
        x0_counts = Counter(round(r["x0"] / 5) * 5 for r in body_rows)
        levels = sorted(x0 for x0, count in x0_counts.items() if count >= 2)
        if len(levels) < 2:
            continue

        # The continuation level is the LEFTMOST well-supported level, not the
        # most frequent one: a long indented title block can otherwise make the
        # first-line indent look like the body column.
        continuation_x0 = levels[0]
        paragraph_x0 = next((x0 for x0 in levels[1:] if x0 - continuation_x0 >= 5), None)
        if paragraph_x0 is None:
            continue

        # Drop leftover margin numbering now that the body column is known.
        content_lines = [
            r for r in content_lines
            if not (r["x0"] < continuation_x0 - 5 and r["text"].strip().isdigit())
        ]
        if len(content_lines) < 3:
            continue

        # A line counts as a paragraph start once it is indented meaningfully
        # past the continuation level. A fixed tolerance is unusable because
        # the indent step ranges from ~10pt (CA) to ~40pt (FL).
        INDENT_STEP = max((paragraph_x0 - continuation_x0) * 0.6, 5.0)
        # A rarer indent (e.g. a one-off "effective date" proviso sentence)
        # is invisible to `levels` because it only occurs once on the page,
        # so it never anchors INDENT_STEP. Recognize it too, but require the
        # previous line to be a genuinely ended sentence sitting right at the
        # continuation indent, so ordinary wraps are never misflagged.
        MINOR_INDENT_STEP = max(INDENT_STEP * 0.35, 8.0)
        RAGGED_TOL = 25  # points short of the right margin = forced break

        # Right margin of the body text column. Running heads and page
        # numbers sit outside the column and would inflate it.
        _body_x1 = [
            row["x1"] for row in content_lines
            if continuation_x0 - 8 <= row["x0"] <= paragraph_x0 + 8
        ]
        right_margin = max(_body_x1) if _body_x1 else 0.0

        # Vertical-gap threshold adapted to this page's line pitch, so that
        # double-spaced bills do not report every wrapped line as a boundary.
        _gaps = sorted(
            content_lines[i]["y0"] - content_lines[i - 1]["y1"]
            for i in range(1, len(content_lines))
        )
        _median_gap = _gaps[len(_gaps) // 2] if _gaps else 0.0
        gap_threshold = max(_median_gap * 1.8, _median_gap + 6.0, 12.0)

        # Detect paragraph boundaries
        prev_x0 = content_lines[0]["x0"]
        prev_x1 = content_lines[0]["x1"]
        prev_bottom = content_lines[0]["y1"]
        prev_text = content_lines[0]["text"]

        for i, line in enumerate(content_lines[1:], 1):
            x0 = line["x0"]
            y_top = line["y0"]
            txt = line["text"]
            is_bold = "Bold" in line["font"]

            gap = y_top - prev_bottom
            signal = None

            # Signal 1: First-line indent after a line that ended a paragraph.
            # A wrapped line always returns to the continuation indent, so a
            # previous line still at the first-line indent means two separate
            # paragraphs — but only when it also *reads* like an ended one,
            # otherwise every line of an indented title block gets flagged.
            is_at_para_indent = x0 - continuation_x0 >= INDENT_STEP
            prev_at_continuation = prev_x0 - continuation_x0 < INDENT_STEP
            prev_is_ragged = prev_x1 < right_margin - RAGGED_TOL
            paragraph_ended = (
                prev_text.rstrip().endswith(('.', ':', ';', '!', '?', '—', '–'))
                or bool(_LIST_MARKER_START_RE.match(txt))
            )
            # Minor-indent fallback for single-occurrence indent levels that
            # `levels` excludes (see MINOR_INDENT_STEP above).
            is_at_minor_indent = (
                MINOR_INDENT_STEP <= (x0 - continuation_x0) < INDENT_STEP
                and prev_at_continuation
                and paragraph_ended
            )
            is_at_para_indent = is_at_para_indent or is_at_minor_indent

            # A title block set with a hanging indent runs several unmarked
            # lines at the deeper level; those are wraps, not paragraph starts.
            _next = content_lines[i + 1] if i + 1 < len(content_lines) else None
            in_hanging_block = (
                _next is not None
                and _next["x0"] - continuation_x0 >= INDENT_STEP
                and not _LIST_MARKER_START_RE.match(_next["text"])
                and not txt.rstrip().endswith(('.', ':', ';'))
            )

            if (
                is_at_para_indent
                and not in_hanging_block
                and (prev_at_continuation or ((prev_is_ragged or not prev_at_continuation) and paragraph_ended))
                and len(txt) > 3
            ):
                _txt_stripped = txt.strip()
                # Skip short section numbers (e.g. "89.9", "103.25") that are
                # intentionally merged with their title on the same output line
                if re.match(r'^\d+[A-Z]?\.\d+[A-Z]?$', _txt_stripped):
                    pass  # not a real paragraph boundary
                # Skip bare subsection/enumeration markers that are always
                # merged with the following content line in the output:
                #   "8."  "1."  "12."       — numbered subsections
                #   "(a)" "(1)" "(iv)"      — parenthesised enumerations
                #   "a."  "b."  "ii."       — lettered/roman enumerations
                elif re.match(
                    r'^(?:'
                    r'\d{1,3}\.'                     # "8.", "12."
                    r'|'
                    r'\([a-z]{1,4}\)'                # "(a)", "(iv)"
                    r'|'
                    r'\(\d{1,3}\)'                   # "(1)", "(12)"
                    r'|'
                    r'[a-z]{1,4}\.'                  # "a.", "ii."
                    r')$',
                    _txt_stripped, re.IGNORECASE,
                ):
                    pass  # subsection marker, not a separate paragraph
                else:
                    signal = "indent"
            # Signal 2: Bold line after non-bold (section title)
            # NOTE: Bold section numbers like "89.9" or "103.5" followed by
            # a bold title on the same y-coordinate are intentionally merged
            # into one line in output (e.g. "89.9 Disposal of fees.").
            # Only flag bold as a boundary if it's NOT a short section number.
            elif is_bold and i > 0 and len(txt) > 10 and prev_is_ragged:
                prev_font = content_lines[i - 1]["font"]
                if "Bold" not in prev_font:
                    signal = "bold"
            # Signal 3: Vertical gap noticeably larger than this page's line
            # pitch, after a line that already ended short of the right margin.
            # Requiring a ragged predecessor avoids flagging the first body
            # line of a page, where the gap is just the running-header offset.
            elif gap > gap_threshold and prev_is_ragged:
                signal = "gap"
            # Signal 4: previous line is a short, standalone all-caps heading
            # (e.g. "NOTICE TO EMPLOYEES") sitting well short of the right
            # margin, and the current line drops back down to the ordinary
            # continuation indent (real body text resuming). Requiring the
            # return to continuation_x0 keeps this from firing inside a
            # wrapped all-caps title block, where every wrap stays at the
            # SAME elevated indent as the heading itself.
            elif (
                prev_is_ragged
                and prev_text.strip().isupper()
                and 1 <= len(prev_text.split()) <= 8
                and not bool(_LIST_MARKER_START_RE.match(prev_text))
                and (x0 - continuation_x0) < INDENT_STEP
            ):
                signal = "heading_break"

            if signal:
                boundaries.append(ParagraphBoundary(
                    page=pg_idx,
                    line_index=i,
                    x0=x0,
                    prev_x0=prev_x0,
                    first_words=txt[:60],
                    signal=signal,
                ))

            prev_x0 = x0
            prev_x1 = line["x1"]
            prev_bottom = line["y1"]
            prev_text = txt

    doc.close()
    return boundaries


# ═══════════════════════════════════════════════════════════════════
# Paragraph Accuracy Check (compare PDF boundaries vs final text)
# ═══════════════════════════════════════════════════════════════════

def _normalize_for_matching(text: str) -> str:
    """Strip tags and normalize whitespace for fuzzy matching."""
    text = re.sub(r'\[([AD])>(.*?)<\1\]', r'\2', text, flags=re.DOTALL)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


_TAG_DELIM_RE = re.compile(r'\[[AD]>|<[AD]\]')


def _norm_offset_to_raw(raw_line: str, norm_pos: int) -> int:
    """Map a character offset in the normalized line back to the raw line.

    The normalized form drops [A>/<A] tag delimiters, collapses whitespace runs
    to a single space and strips the leading whitespace, so offsets computed on
    it cannot be used directly against the tagged output line.
    """
    if norm_pos <= 0:
        return 0

    count = 0          # normalized characters emitted so far
    i = 0
    pending_space = False
    started = False
    while i < len(raw_line):
        m = _TAG_DELIM_RE.match(raw_line, i)
        if m:
            i = m.end()
            continue
        ch = raw_line[i]
        if ch.isspace():
            pending_space = started
            i += 1
            continue
        if pending_space:
            if count == norm_pos:
                return i
            count += 1
            pending_space = False
        if count == norm_pos:
            return i
        started = True
        count += 1
        i += 1
    return len(raw_line)


def _find_text_in_output(
    needle: str, output_lines: List[str], output_text_norm: str,
    *, prefer_long_key: bool = False,
) -> Tuple[Optional[int], bool, int]:
    """Find which output line contains the needle text.

    Parameters
    ----------
    prefer_long_key : bool
        Try a longer (40-char) key before the default 25-char one. Only
        needed for boundary text prone to short shared-phrase collisions
        (e.g. "heading_break" signals); left off elsewhere so existing
        signal matching is unaffected.

    Returns
    -------
    (line_index, starts_at_line_beginning, char_position) or (None, False, -1)
        line_index: which output line contains the text
        starts_at_line_beginning: whether the needle starts at position 0-2 of that line
        char_position: character offset within the line where needle starts
    """
    needle_norm = _normalize_for_matching(needle)
    if not needle_norm:
        return None, False, -1

    key_lens = (40, 25) if prefer_long_key else (25,)
    for key_len in key_lens:
        search_key = needle_norm[:key_len].lower()
        if not search_key:
            continue

        # Anchor on a word boundary so a fragment like "975, provides" cannot
        # match inside "Act of 1975, provides".
        key_re = re.compile(r'(?<![0-9A-Za-z])' + re.escape(search_key))

        for i, line in enumerate(output_lines):
            line_norm = _normalize_for_matching(line).lower()
            m = key_re.search(line_norm)
            if m:
                pos = m.start()
                starts_at_beginning = pos <= 2
                return i, starts_at_beginning, pos

    return None, False, -1


def _is_list_marker_prefix(prefix: str) -> bool:
    """Return True if *prefix* looks like a list/enumeration marker.

    Examples: "(v)", "(IV)", "1.", "12)", "(a)", "A."

    Numeric markers are capped at three digits so that a statute section
    number left on its own PDF line (e.g. "127481.") is not excused as an
    inline bullet when the output merges it into the following section.
    """
    p = (prefix or "").strip()
    if not p:
        return False
    return bool(re.match(r"^(?:\(?[ivxlcdmIVXLCDM]+\)?[\.)]?|\(?\d{1,3}[A-Za-z]?\)?[\.)]?|\(?[A-Za-z]\)?[\.)]?)$", p))


@dataclass
class MergeError:
    """A paragraph that should start on a new line but was merged."""
    output_line: int          # line index in the output .txt
    expected_break_before: str  # text that should start a new paragraph
    signal: str              # what the PDF says (indent/bold/sec/gap)
    page: int               # PDF page where this boundary was found
    context: str             # surrounding text in output


@dataclass
class ParagraphAccuracyResult:
    """Result of paragraph accuracy check."""
    accuracy_pct: float
    total_boundaries: int
    correct_boundaries: int
    merge_errors: List[MergeError]
    boundaries_checked: int
    skipped: int  # boundaries we couldn't locate in output


def check_paragraph_accuracy(
    pdf_path: str | Path,
    final_text: str,
    *,
    state_code: Optional[str] = None,
    pages: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Check if the final output text preserves paragraph boundaries from the PDF.

    Produces actionable recommendations:
    - SPLIT: line X needs a paragraph break at position Y
    - MERGE: lines X and X+1 should be one paragraph (over-split)

    Parameters
    ----------
    pdf_path : str or Path
        Path to the source PDF.
    final_text : str
        The final output .txt content (after LLM cleaning).
    state_code : str, optional
        Two-letter state code (for future state-specific tuning).
    pages : list[int], optional
        Pages to check. None → all pages.

    Returns
    -------
    dict with keys:
        accuracy_pct : float — overall paragraph structure accuracy
        total_boundaries : int — paragraph boundaries found in PDF
        correct_boundaries : int — boundaries correctly preserved
        splits_needed : list[dict] — lines that need splitting
        merges_needed : list[dict] — lines that are over-split
        boundaries_checked : int
        skipped : int
    """
    # Step 1: Extract paragraph boundaries from PDF
    boundaries = extract_paragraph_boundaries(pdf_path, pages=pages)

    if not boundaries:
        logger.warning(
            "📐 Paragraph accuracy: no PDF paragraph boundaries detected for %s — "
            "layout accuracy is unverified, not 100%%.",
            Path(pdf_path).name,
        )
        return {
            "accuracy_pct": 100.0,
            "total_boundaries": 0,
            "correct_boundaries": 0,
            "splits_needed": [],
            "merges_needed": [],
            "merge_errors": [],  # backward compat
            "boundaries_checked": 0,
            "merge_checkpoints": 0,
            "skipped": 0,
            "boundary_detection_failed": True,
        }

    # Step 2: Split output into lines and build normalized version
    output_lines = final_text.split('\n')
    output_text_norm = _normalize_for_matching(final_text)

    # Step 3: For each boundary, check if output has a break there
    correct = 0
    splits_needed: List[Dict] = []
    skipped = 0

    for boundary in boundaries:
        line_idx, starts_at_beginning, char_pos = _find_text_in_output(
            boundary.first_words, output_lines, output_text_norm,
            prefer_long_key=(boundary.signal == "heading_break"),
        )

        if line_idx is None:
            skipped += 1
            continue

        line_norm_for_pos = _normalize_for_matching(output_lines[line_idx])
        prefix_before = line_norm_for_pos[:char_pos].strip() if char_pos > 0 else ""

        if line_idx == 0 or starts_at_beginning or _is_list_marker_prefix(prefix_before):
            correct += 1
        else:
            # SPLIT needed — paragraph text is merged mid-line
            # Find the actual character position in the raw output line
            raw_line = output_lines[line_idx]
            # Search in raw line for the boundary text (with tags)
            raw_search = boundary.first_words[:20]
            raw_pos = raw_line.find(raw_search)
            if raw_pos < 0:
                # Tags interrupt the literal text — map the normalized offset
                raw_pos = _norm_offset_to_raw(raw_line, char_pos)

            # False-positive guard: many PDF wraps get detected as boundaries
            # by indentation, but in output they appear as lowercase clause
            # continuations inside the same sentence. Do not flag these as
            # split-needed. The check runs on the normalized line so a tag
            # delimiter ("...<A]") is not mistaken for the preceding text.
            first_token = _normalize_for_matching(boundary.first_words).split(' ', 1)[0].strip()
            looks_lowercase_continuation = bool(first_token) and first_token[0].islower()
            if looks_lowercase_continuation and char_pos > 0:
                left_text = line_norm_for_pos[:char_pos].rstrip()
                left_tail = left_text[-1:] if left_text else ""
                # If left side does not end a sentence and the candidate
                # boundary starts lowercase, this is almost always a wrap.
                if left_tail and left_tail not in '.:;!?':
                    correct += 1
                    continue

            # Get the text BEFORE the split point (end of previous para)
            text_before = raw_line[:raw_pos].rstrip() if raw_pos > 0 else ""
            text_after = raw_line[raw_pos:].strip() if raw_pos > 0 else raw_line

            splits_needed.append({
                "line": line_idx + 1,  # 1-based
                "char_position": raw_pos,
                "action": "SPLIT",
                "text_before_split": text_before[-60:] if text_before else "",
                "text_after_split": text_after[:60],
                "signal": boundary.signal,
                "pdf_page": boundary.page + 1,
                "description": (
                    f"Split required at line {line_idx + 1}, position {raw_pos}: "
                    f"new paragraph starts with \"{boundary.first_words[:50]}...\""
                ),
            })

    # AR/IA page-1 header rule:
    # Treat the title/header block as intentionally line-oriented. Do not
    # request MERGE for these lines; instead, raise SPLIT only when known
    # header elements are compacted onto one line.
    if state_code and state_code.upper() == "AR":
        _detect_ar_page1_header_compaction(output_lines, splits_needed)
    if state_code and state_code.upper() == "IA":
        _detect_ia_page1_header_compaction(output_lines, splits_needed)

    # Step 4: Detect OVER-SPLITS (merges needed)
    # Find lines in output that have a break but the PDF says they should
    # be continuous (same paragraph).  Look for short lines followed by
    # continuation text that belongs to the same PDF paragraph.
    merges_needed: List[Dict] = []
    merge_checkpoints = _detect_over_splits(
        output_lines, boundaries, merges_needed, state_code=state_code
    )

    # ── Deduplicate splits that were detected on multiple PDF pages ──
    # When a paragraph spans a page break, the same text may be detected
    # as a boundary on both pages.  Keep only the first occurrence.
    _seen_splits: set = set()
    _deduped_splits: List[Dict] = []
    for sp in splits_needed:
        key = (sp["line"], sp["char_position"])
        if key not in _seen_splits:
            _seen_splits.add(key)
            _deduped_splits.append(sp)
    splits_needed = _deduped_splits

    # Calculate accuracy.
    # Two independent populations are scored together:
    #   - split checkpoints  = PDF boundaries actually located in the output
    #   - merge checkpoints   = adjacent output line pairs examined for an
    #                           over-split (i.e. every real break opportunity)
    # Counting only the merge *failures* in the denominator used to make a
    # single merge finding halve the score on documents with few boundaries.
    checked = len(boundaries) - skipped
    total_issues = len(splits_needed) + len(merges_needed)
    total_checkpoints = checked + merge_checkpoints
    accuracy = ((total_checkpoints - total_issues) / total_checkpoints * 100) if total_checkpoints > 0 else 100.0
    accuracy = max(0.0, accuracy)

    result = {
        "accuracy_pct": round(accuracy, 1),
        "total_boundaries": len(boundaries),
        "correct_boundaries": correct,
        "splits_needed": splits_needed,
        "merges_needed": merges_needed,
        "merge_errors": splits_needed,  # backward compat
        "boundaries_checked": checked,
        "merge_checkpoints": merge_checkpoints,
        "skipped": skipped,
        "boundary_detection_failed": False,
    }

    logger.info(
        "📐 Paragraph accuracy: %.1f%% | %d splits needed, %d merges needed | (%d/%d boundaries correct)",
        accuracy, len(splits_needed), len(merges_needed), correct, checked,
    )

    return result


def _detect_over_splits(
    output_lines: List[str],
    boundaries: List[ParagraphBoundary],
    merges_needed: List[Dict],
    *,
    state_code: Optional[str] = None,
) -> int:
    """Detect lines that are over-split (broken mid-paragraph).

        Heuristics for a false line break (merge needed):
        - Line ends mid-sentence (no period, colon, or section marker)
        - Next line starts like a continuation (lowercase, number, or
            uppercase word), but NOT like a new list/section marker
        - Line is very short compared to neighbors

    Returns the number of adjacent line pairs actually examined, which is the
    denominator for the merge half of the accuracy score.
    """
    # Build set of expected paragraph-start texts for quick lookup
    boundary_starts = set()
    for b in boundaries:
        norm = _normalize_for_matching(b.first_words[:20]).lower()
        if norm:
            boundary_starts.add(norm)

    checkpoints = 0

    for i in range(len(output_lines) - 1):
        line = output_lines[i].strip()
        next_line = output_lines[i + 1].strip()

        # Skip blank lines (they're intentional separators)
        if not line or not next_line:
            continue

        # Normalize for checking
        line_norm = _normalize_for_matching(line)
        next_norm = _normalize_for_matching(next_line)

        if not line_norm or not next_norm:
            continue

        # A genuine break opportunity — counts toward the merge denominator
        # whether or not it turns out to be an over-split.
        checkpoints += 1

        # AR page-1 title/header block is expected to remain as separate lines.
        # Suppress false MERGE recommendations there.
        if (
            state_code
            and state_code.upper() == "AR"
            and _is_ar_page1_header_line(line)
            and _is_ar_page1_header_line(next_line)
        ):
            continue
        if (
            state_code
            and state_code.upper() == "IA"
            and _is_ia_page1_header_pair(line, next_line)
        ):
            continue

        # Check if next_line is a known paragraph boundary — if so, the break is correct
        next_start = next_norm[:20].lower()
        is_expected_boundary = any(next_start.startswith(bs[:15]) for bs in boundary_starts if bs)
        if is_expected_boundary:
            continue

        def _is_list_marker_start(s: str) -> bool:
            s = (s or "").strip()
            if not s:
                return False
            # New item starts we should preserve as true boundaries:
            #   (a) foo, (IV) foo, 1) foo, 2. foo, a) foo
            return bool(re.match(r'^(?:\([A-Za-z0-9ivxlcdmIVXLCDM]{1,6}\)|[A-Za-z0-9ivxlcdmIVXLCDM]{1,6}[\.)])\s+', s))

        def _is_continuation_start(s: str) -> bool:
            s = (s or "").strip()
            if not s:
                return False

            # Strip leading tag opener(s) when a continuation line starts
            # with tagged text: [A>foo<A] or [D>bar<D].
            s_for_check = re.sub(r'^(?:\[[AD]>\s*)+', '', s)
            s_for_check = s_for_check.strip()
            if not s_for_check or _is_list_marker_start(s_for_check):
                return False
            if re.match(r'^\((?:commencing|including|pursuant|under|as|with)\b', s_for_check, re.IGNORECASE):
                return True
            ch = s_for_check[0]
            # Continuation-like starts we want to catch:
            #   lowercase clause, numeric carryover ("2019." / "10 units"),
            #   uppercase lexical continuation ("Administration", "Preservation")
            return ch.islower() or ch.isdigit() or ch.isupper()

        # Heuristic: line ends without sentence-ending punctuation AND
        # next line starts like a continuation → likely an over-split
        ends_mid_sentence = not line_norm.rstrip().endswith(('.', ':', ';', '"', ')', ']'))
        next_is_continuation = _is_continuation_start(next_norm)

        _next_raw = (next_line or "").strip()
        _strong_continuation = bool(
            re.match(r'^\((?:commencing|including|pursuant|under|as|with)\b', _next_raw, re.IGNORECASE)
            or re.match(r'^\[[AD]>', _next_raw)
        )

        if ends_mid_sentence and next_is_continuation:
            # Additional check: skip if line is reasonably long (>100 chars)
            # Short broken lines are more suspicious
            if len(line_norm) < 80 or _strong_continuation:
                merges_needed.append({
                    "line": i + 1,  # 1-based (first line)
                    "merge_with_line": i + 2,  # 1-based (second line)
                    "action": "MERGE",
                    "line_text": line_norm[-50:],
                    "next_line_text": next_norm[:50],
                    "description": (
                        f"Merge lines {i + 1} and {i + 2}: "
                        f"line ends mid-sentence, next looks like continuation"
                    ),
                })

    return checkpoints


def _is_ar_page1_header_line(text: str) -> bool:
    """Return True for AR page-1 title/header lines that should stay split."""
    s = _normalize_for_matching(text)
    if not s:
        return False

    patterns = [
        r'^Act\s+\d+\s+of\s+the\s+Regular\s+Session$',
        r'^State\s+of\s+Arkansas\b',
        r'^\d{1,3}(?:st|nd|rd|th)\s+General\s+Assembly\b',
        r'^Regular\s+Session,\s*\d{4}$',
        r'^As\s+Engrossed:\b',
        r'^HOUSE\s+BILL\b',
        r'^SENATE\s+BILL\b',
        r'^A\s+Bill$',
        r'^By:\b',
        r'^For\s+An\s+Act\s+To\s+Be\s+Entitled\b',
        r'^Subtitle\b',
    ]
    return any(re.search(p, s, flags=re.IGNORECASE) for p in patterns)


def _detect_ar_page1_header_compaction(output_lines: List[str], splits_needed: List[Dict]) -> None:
    """Add split-needed issues when AR page-1 header elements are merged."""
    max_scan = min(len(output_lines), 120)
    stop_idx = max_scan
    for i in range(max_scan):
        if re.search(r'\bBE\s+IT\s+ENACTED\s+BY\s+THE\s+GENERAL\s+ASSEMBLY\b', output_lines[i], re.IGNORECASE):
            stop_idx = i
            break

    compact_markers = [
        (r'\bHOUSE\s+BILL\s+\d+\b.*\bA\s+Bill\b', "HOUSE BILL line merged with 'A Bill'"),
        (r'\bState\s+of\s+Arkansas\b.*\bGeneral\s+Assembly\b.*\bRegular\s+Session,\s*\d{4}\b', "State/Assembly/Session merged into one line"),
        (r'\bBy:\b.*\bBy:\b', "Multiple sponsor lines merged into one line"),
        (r'\bFor\s+An\s+Act\s+To\s+Be\s+Entitled\b.*\bCONCERNING\b', "Title preface merged with body title text"),
        (r'\bSubtitle\b.*\bTO\s+AMEND\b', "Subtitle label merged with subtitle body"),
    ]

    existing_keys = {(sp.get("line"), sp.get("char_position"), sp.get("action")) for sp in splits_needed}

    for idx in range(stop_idx):
        raw = output_lines[idx].strip()
        if not raw:
            continue
        norm = _normalize_for_matching(raw)
        if not norm:
            continue

        for pattern, reason in compact_markers:
            m = re.search(pattern, norm, flags=re.IGNORECASE)
            if not m:
                continue

            # Choose split point near the second semantic token when possible.
            split_at = -1
            for tok in [" A Bill", " Regular Session", " By:", " CONCERNING", " TO AMEND"]:
                split_at = norm.find(tok)
                if split_at > 0:
                    break
            if split_at <= 0:
                split_at = m.start() + max(1, len(m.group(0)) // 2)

            key = (idx + 1, split_at, "SPLIT")
            if key in existing_keys:
                continue

            splits_needed.append({
                "line": idx + 1,
                "char_position": split_at,
                "action": "SPLIT",
                "text_before_split": norm[max(0, split_at - 60):split_at].strip(),
                "text_after_split": norm[split_at:split_at + 60].strip(),
                "signal": "ar_page1_header",
                "pdf_page": 1,
                "description": (
                    f"Split required at line {idx + 1}, position {split_at}: "
                    f"AR page-1 header compaction detected ({reason})"
                ),
            })
            existing_keys.add(key)
            break


def _is_ia_page1_header_pair(line: str, next_line: str) -> bool:
    """Return True when an Iowa page-1 title pair should remain split."""
    left = _normalize_for_matching(line)
    right = _normalize_for_matching(next_line)
    if not left or not right:
        return False

    if re.fullmatch(r'(?:Senate|House)\s+File\s+\d+(?:\s*[-–—]\s*Enrolled)?', left, re.IGNORECASE):
        return True
    if re.fullmatch(r'AN\s+ACT', left, re.IGNORECASE) and re.fullmatch(r'[A-Z][A-Z0-9,;:\-()&/\.\' ]{20,}', right):
        return True
    return False


def _detect_ia_page1_header_compaction(output_lines: List[str], splits_needed: List[Dict]) -> None:
    """Add split-needed issues when Iowa page-1 title lines are merged."""
    max_scan = min(len(output_lines), 80)
    stop_idx = max_scan
    for i in range(max_scan):
        if re.search(r'\bBE\s+IT\s+ENACTED\s+BY\s+THE\s+GENERAL\s+ASSEMBLY\s+OF\s+THE\s+STATE\s+OF\s+IOWA\b', output_lines[i], re.IGNORECASE):
            stop_idx = i
            break

    existing_keys = {(sp.get("line"), sp.get("char_position"), sp.get("action")) for sp in splits_needed}

    for idx in range(stop_idx):
        raw = output_lines[idx].strip()
        if not raw:
            continue
        norm = _normalize_for_matching(raw)
        if not norm:
            continue

        m = re.search(r'\bAN\s+ACT\b\s+(?P<title>[A-Z][A-Z0-9,;:\-()&/\.\' ]{20,})$', norm, re.IGNORECASE)
        if not m:
            continue

        split_at = norm.upper().find(" AN ACT ")
        if split_at >= 0:
            split_at += 1
        else:
            split_at = norm.upper().find("AN ACT ") + len("AN ACT")
        if split_at <= 0:
            continue

        key = (idx + 1, split_at, "SPLIT")
        if key in existing_keys:
            continue

        splits_needed.append({
            "line": idx + 1,
            "char_position": split_at,
            "action": "SPLIT",
            "text_before_split": norm[max(0, split_at - 60):split_at].strip(),
            "text_after_split": norm[split_at:split_at + 60].strip(),
            "signal": "ia_page1_header",
            "pdf_page": 1,
            "description": (
                f"Split required at line {idx + 1}, position {split_at}: "
                "Iowa page-1 title compaction detected (AN ACT merged with title)"
            ),
        })
        existing_keys.add(key)
        break


# ═══════════════════════════════════════════════════════════════════
# Enhanced PDF Metadata Extraction
# ═══════════════════════════════════════════════════════════════════

def extract_full_metadata(pdf_path: str | Path) -> Dict[str, Any]:
    """Extract comprehensive metadata from a PDF.

    Returns a dict with:
        document : dict — format, creator, producer, dates, page_count
        pages : list[dict] — per-page size, rotation, fonts
        headers : dict — header text per page (top 80pt zone)
        footers : dict — footer text per page (bottom 80pt zone)
        paragraph_boundaries : list — detected paragraph starts

    Parameters
    ----------
    pdf_path : str or Path
        Path to the PDF.

    Returns
    -------
    dict
    """
    if fitz is None:
        return {}

    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        return {}

    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        return {}

    # ── Document-level metadata ──────────────────────────────────
    doc_meta = doc.metadata or {}
    document_info = {
        "format": doc_meta.get("format", ""),
        "title": doc_meta.get("title", ""),
        "author": doc_meta.get("author", ""),
        "creator": doc_meta.get("creator", ""),
        "producer": doc_meta.get("producer", ""),
        "creation_date": doc_meta.get("creationDate", ""),
        "mod_date": doc_meta.get("modDate", ""),
        "page_count": len(doc),
    }

    # ── Per-page info + headers/footers ──────────────────────────
    headers: Dict[int, str] = {}
    footers: Dict[int, str] = {}
    page_info: List[Dict] = []
    HEADER_ZONE = 80  # points from top
    FOOTER_ZONE = 80  # points from bottom

    for pg_idx in range(len(doc)):
        page = doc[pg_idx]
        pg_num = pg_idx + 1
        width = page.rect.width
        height = page.rect.height

        page_info.append({
            "page": pg_num,
            "width": round(width, 1),
            "height": round(height, 1),
            "rotation": page.rotation,
        })

        # Header zone text
        header_rect = fitz.Rect(0, 0, width, HEADER_ZONE)
        header_text = page.get_text("text", clip=header_rect).strip()
        if header_text:
            headers[pg_num] = header_text

        # Footer zone text
        footer_rect = fitz.Rect(0, height - FOOTER_ZONE, width, height)
        footer_text = page.get_text("text", clip=footer_rect).strip()
        if footer_text:
            footers[pg_num] = footer_text

    doc.close()

    # ── Deduplicate repeating headers/footers ────────────────────
    header_summary = _summarize_repeating(headers)
    footer_summary = _summarize_repeating(footers)

    return {
        "document": document_info,
        "pages": page_info,
        "headers": headers,
        "footers": footers,
        "header_summary": header_summary,
        "footer_summary": footer_summary,
    }


def _summarize_repeating(zone_texts: Dict[int, str]) -> List[Dict]:
    """Group pages with identical/similar zone text."""
    if not zone_texts:
        return []

    groups: List[Dict] = []
    seen: Dict[str, List[int]] = {}

    for pg, txt in zone_texts.items():
        # Normalize for grouping (strip page numbers)
        norm = re.sub(r'\d+/\d+|\bpage\s*\d+\b|-\d+-', '', txt.lower()).strip()
        if norm in seen:
            seen[norm].append(pg)
        else:
            seen[norm] = [pg]

    for norm, pages in seen.items():
        sample_pg = pages[0]
        groups.append({
            "text": zone_texts[sample_pg],
            "pages": pages,
            "count": len(pages),
        })

    return sorted(groups, key=lambda g: -g["count"])


def format_metadata_for_llm(metadata: Dict[str, Any]) -> str:
    """Format extracted metadata as a text block for prepending to LLM input.

    Parameters
    ----------
    metadata : dict
        Output from extract_full_metadata().

    Returns
    -------
    str
        Human-readable metadata block.
    """
    if not metadata:
        return ""

    sections = []
    doc = metadata.get("document", {})

    # Document info
    doc_parts = []
    if doc.get("format"):
        doc_parts.append(f"format={doc['format']}")
    if doc.get("creator"):
        doc_parts.append(f"creator={doc['creator']}")
    if doc.get("creation_date"):
        doc_parts.append(f"created={doc['creation_date']}")
    if doc.get("page_count"):
        doc_parts.append(f"pages={doc['page_count']}")
    if doc_parts:
        sections.append("DOCUMENT: " + " | ".join(doc_parts))

    # Page size
    pages = metadata.get("pages", [])
    if pages:
        p = pages[0]
        sections.append(f"PAGE SIZE: {p['width']}x{p['height']}pt")

    # Header summary
    for group in metadata.get("header_summary", []):
        if group["count"] == 1:
            sections.append(f"HEADER page {group['pages'][0]}: \"{group['text']}\"")
        else:
            pg_range = f"pages {group['pages'][0]}-{group['pages'][-1]}"
            sections.append(f"HEADER {pg_range} ({group['count']} pages): \"{group['text'][:80]}\"")

    # Footer summary
    for group in metadata.get("footer_summary", []):
        if group["count"] == 1:
            sections.append(f"FOOTER page {group['pages'][0]}: \"{group['text']}\"")
        else:
            pg_range = f"pages {group['pages'][0]}-{group['pages'][-1]}"
            sections.append(f"FOOTER {pg_range} ({group['count']} pages): \"{group['text'][:80]}\"")

    if not sections:
        return ""

    return (
        "═══ PDF STRUCTURAL METADATA (detected by PyMuPDF) ═══\n"
        + "\n".join(sections)
        + "\n═══ END METADATA ═══\n\n"
    )


# ═══════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Check paragraph accuracy of PDF→text conversion")
    parser.add_argument("pdf", help="Path to source PDF")
    parser.add_argument("txt", help="Path to final output .txt file")
    parser.add_argument("--state", default=None, help="State code (e.g. IA)")
    parser.add_argument("--metadata", action="store_true", help="Print full metadata")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    if args.metadata:
        meta = extract_full_metadata(args.pdf)
        print(format_metadata_for_llm(meta))
        print("\n--- RAW METADATA ---")
        meta_summary = {k: v for k, v in meta.items() if k not in ("headers", "footers")}
        print(json.dumps(meta_summary, indent=2, default=str))
    else:
        txt_content = Path(args.txt).read_text(encoding="utf-8")
        result = check_paragraph_accuracy(args.pdf, txt_content, state_code=args.state)

        print(f"\n{'=' * 60}")
        print(f"PARAGRAPH STRUCTURE ACCURACY: {result['accuracy_pct']:.1f}%")
        print(f"{'=' * 60}")
        print(f"  Boundaries in PDF:    {result['total_boundaries']}")
        print(f"  Checked:              {result['boundaries_checked']}")
        print(f"  Correct:              {result['correct_boundaries']}")
        print(f"  Splits needed:        {len(result['splits_needed'])}")
        print(f"  Merges needed:        {len(result['merges_needed'])}")
        print(f"  Skipped (not found):  {result['skipped']}")

        if result['splits_needed']:
            print(f"\n{'─' * 60}")
            print("✂️  SPLITS NEEDED (line has merged paragraphs):")
            print(f"{'─' * 60}")
            for s in result['splits_needed']:
                print(f"\n  {s['description']}")
                print(f"    Before split: \"...{s['text_before_split'][-50:]}\"")
                print(f"    After split:  \"{s['text_after_split'][:50]}...\"")

        if result['merges_needed']:
            print(f"\n{'─' * 60}")
            print("🔗  MERGES NEEDED (lines are over-split):")
            print(f"{'─' * 60}")
            for m in result['merges_needed'][:20]:
                print(f"\n  {m['description']}")
                print(f"    Line {m['line']}: \"...{m['line_text'][-50:]}\"")
                print(f"    Line {m['merge_with_line']}: \"{m['next_line_text'][:50]}\"")

