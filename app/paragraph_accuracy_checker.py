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
        blocks = page.get_text("dict")["blocks"]

        # Find body block (largest text block by height)
        text_blocks = [b for b in blocks if b.get("type") == 0]
        if not text_blocks:
            continue
        body_block = max(text_blocks, key=lambda b: b["bbox"][3] - b["bbox"][1])
        lines = body_block["lines"]

        # Filter out margin line numbers (x0 < 90, text is just digits)
        content_lines = []
        for line in lines:
            spans = line.get("spans", [])
            if not spans:
                continue
            txt = "".join(s["text"] for s in spans).strip()
            x0 = line["bbox"][0]
            if x0 < 90 and txt.isdigit():
                continue
            content_lines.append(line)

        # Analyze indentation patterns to find the two primary indent levels
        x0_values = [line["bbox"][0] for line in content_lines]
        if len(x0_values) < 3:
            continue

        # Determine the dominant indent levels for this page
        from collections import Counter
        x0_rounded = [round(x / 5) * 5 for x in x0_values]
        x0_counts = Counter(x0_rounded)
        most_common = x0_counts.most_common(5)
        if len(most_common) < 2:
            continue

        # Identify continuation level (most common) and paragraph-start level
        continuation_x0 = most_common[0][0]
        paragraph_x0 = None
        for x0_val, count in most_common[1:]:
            if x0_val > continuation_x0:
                paragraph_x0 = x0_val
                break
        if paragraph_x0 is None:
            for x0_val, count in most_common:
                if x0_val > continuation_x0 and count >= 3:
                    paragraph_x0 = x0_val
                    break
        if paragraph_x0 is None:
            continue

        INDENT_TOL = 8  # points tolerance

        # Detect paragraph boundaries
        prev_x0 = content_lines[0]["bbox"][0] if content_lines else 0
        prev_bottom = content_lines[0]["bbox"][3] if content_lines else 0

        for i, line in enumerate(content_lines[1:], 1):
            x0 = line["bbox"][0]
            y_top = line["bbox"][1]
            spans = line.get("spans", [])
            txt = "".join(s["text"] for s in spans).strip()
            font_name = spans[0].get("font", "") if spans else ""
            is_bold = "Bold" in font_name

            gap = y_top - prev_bottom
            signal = None

            # Signal 1: First-line indent after continuation line
            is_at_para_indent = abs(x0 - paragraph_x0) <= INDENT_TOL
            prev_at_continuation = abs(prev_x0 - continuation_x0) <= INDENT_TOL

            if is_at_para_indent and prev_at_continuation and len(txt) > 3:
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
            elif is_bold and i > 0 and len(txt) > 10:
                prev_spans = content_lines[i - 1].get("spans", [])
                prev_font = prev_spans[0].get("font", "") if prev_spans else ""
                if "Bold" not in prev_font:
                    signal = "bold"
            # Signal 3: Large vertical gap
            elif gap > 12.0:
                signal = "gap"

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
            prev_bottom = line["bbox"][3]

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


def _find_text_in_output(needle: str, output_lines: List[str], output_text_norm: str) -> Tuple[Optional[int], bool, int]:
    """Find which output line contains the needle text.

    Returns
    -------
    (line_index, starts_at_line_beginning, char_position) or (None, False, -1)
        line_index: which output line contains the text
        starts_at_line_beginning: whether the needle starts at position 0-2 of that line
        char_position: character offset within the line where needle starts
    """
    needle_norm = _normalize_for_matching(needle)
    search_key = needle_norm[:25].lower()
    if not search_key:
        return None, False, -1

    # Search line by line for more accurate position detection
    for i, line in enumerate(output_lines):
        line_norm = _normalize_for_matching(line).lower()
        pos = line_norm.find(search_key)
        if pos >= 0:
            starts_at_beginning = pos <= 2
            return i, starts_at_beginning, pos

    return None, False, -1


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
        return {
            "accuracy_pct": 100.0,
            "total_boundaries": 0,
            "correct_boundaries": 0,
            "splits_needed": [],
            "merges_needed": [],
            "merge_errors": [],  # backward compat
            "boundaries_checked": 0,
            "skipped": 0,
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
            boundary.first_words, output_lines, output_text_norm
        )

        if line_idx is None:
            skipped += 1
            continue

        if line_idx == 0 or starts_at_beginning:
            correct += 1
        else:
            # SPLIT needed — paragraph text is merged mid-line
            # Find the actual character position in the raw output line
            raw_line = output_lines[line_idx]
            # Search in raw line for the boundary text (with tags)
            raw_search = boundary.first_words[:20]
            raw_pos = raw_line.find(raw_search)
            if raw_pos < 0:
                # Try without tag interference
                raw_pos = char_pos  # fallback to normalized position

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

    # Step 4: Detect OVER-SPLITS (merges needed)
    # Find lines in output that have a break but the PDF says they should
    # be continuous (same paragraph).  Look for short lines followed by
    # continuation text that belongs to the same PDF paragraph.
    merges_needed: List[Dict] = []
    _detect_over_splits(output_lines, boundaries, merges_needed)

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

    # Calculate accuracy
    checked = len(boundaries) - skipped
    total_issues = len(splits_needed) + len(merges_needed)
    total_checkpoints = checked + len(merges_needed)
    accuracy = ((total_checkpoints - total_issues) / total_checkpoints * 100) if total_checkpoints > 0 else 100.0

    result = {
        "accuracy_pct": round(accuracy, 1),
        "total_boundaries": len(boundaries),
        "correct_boundaries": correct,
        "splits_needed": splits_needed,
        "merges_needed": merges_needed,
        "merge_errors": splits_needed,  # backward compat
        "boundaries_checked": checked,
        "skipped": skipped,
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
):
    """Detect lines that are over-split (broken mid-paragraph).

    Heuristics for a false line break (merge needed):
    - Line ends mid-sentence (no period, colon, or section marker)
    - Next line starts with lowercase
    - Line is very short compared to neighbors
    """
    # Build set of expected paragraph-start texts for quick lookup
    boundary_starts = set()
    for b in boundaries:
        norm = _normalize_for_matching(b.first_words[:20]).lower()
        if norm:
            boundary_starts.add(norm)

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

        # Check if next_line is a known paragraph boundary — if so, the break is correct
        next_start = next_norm[:20].lower()
        is_expected_boundary = any(next_start.startswith(bs[:15]) for bs in boundary_starts if bs)
        if is_expected_boundary:
            continue

        # Heuristic: line ends without sentence-ending punctuation AND
        # next line starts with lowercase → likely an over-split
        ends_mid_sentence = not line_norm.rstrip().endswith(('.', ':', ';', '"', ')', ']'))
        next_starts_lower = next_norm[0].islower() if next_norm else False

        if ends_mid_sentence and next_starts_lower:
            # Additional check: skip if line is reasonably long (>100 chars)
            # Short broken lines are more suspicious
            if len(line_norm) < 80:
                merges_needed.append({
                    "line": i + 1,  # 1-based (first line)
                    "merge_with_line": i + 2,  # 1-based (second line)
                    "action": "MERGE",
                    "line_text": line_norm[-50:],
                    "next_line_text": next_norm[:50],
                    "description": (
                        f"Merge lines {i + 1} and {i + 2}: "
                        f"line ends mid-sentence, next starts lowercase"
                    ),
                })


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

