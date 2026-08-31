"""
Content Accuracy Checker — Word-level comparison between precleaned and final output
====================================================================================

Compares the hybrid precleaned input (artifacts already stripped) against the
final LLM-cleaned output to measure true content accuracy.

Unlike the pipeline's built-in ``compare_texts`` (which compares raw PyMuPDF
extraction vs output and penalises intentional artifact removal), this checker
compares **content vs content** — giving a true measure of whether the LLM
preserved all legislative text.

Every difference is **classified** into a category:
  - content_drop     : real content missing from output (word/paragraph lost)
  - content_added    : extra text in output not in source
  - hyphen_rejoin    : "export- limiting" → "export-limiting" (expected fix)
  - header_reorder   : header lines rearranged (cosmetic, no content loss)
  - artifact_removed : page markers/timestamps stripped (expected cleanup)
  - formatting_fix   : minor formatting normalization

Usage:
    # CLI — compare two files:
    python content_accuracy_checker.py precleaned.txt output.txt

    # CLI — auto-detect from session folder:
    python content_accuracy_checker.py --session <session_path>

    # Python API:
    from content_accuracy_checker import check_content_accuracy
    result = check_content_accuracy(precleaned_text, output_text)
    print(f"Accuracy: {result['accuracy_pct']:.2f}%")
"""

from __future__ import annotations

import argparse
import difflib
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Page artifact patterns to strip from precleaned text
# ---------------------------------------------------------------------------
_ARTIFACT_PATTERNS = [
    re.compile(r"^\*[A-Z]{3}\d+\*.*$", re.MULTILINE),           # *KMW004* ...
    re.compile(r"^\d{2}-\d{2}-\d{4} \d{2}:\d{2}:\d{2} \w+$", re.MULTILINE),  # 03-12-2025 15:44:32 KMW004
    re.compile(r"^--- Page \d+ ---$", re.MULTILINE),              # --- Page 2 ---
    re.compile(r"^\s*Page\s+\d+\s+of\s+\d+\s*$", re.MULTILINE),  # Page 1 of 19
    re.compile(r"^\s*(?:ENROLLED|ENGROSSED)\s+\w+\s*$", re.MULTILINE),  # ENROLLED 20267026er
    re.compile(r"^\s*CODING:\s*Words\s+.+?additions\.\s*$", re.MULTILINE),  # FL CODING line
    re.compile(r"^\s*As Engrossed:.*?[A-Z]{2}\d+\s*$", re.MULTILINE),  # As Engrossed: H3/12/25  HB1163
    re.compile(r"^\s*HB\d+\s*$", re.MULTILINE),                  # Standalone bill numbers
    re.compile(r"^\s*SB\d+\s*$", re.MULTILINE),                  # Standalone bill numbers
    # CA page headers: "AB 827 — 2 —" or "— 3 — AB 827" or "92 Ch. 574 — 2 —"
    re.compile(
        r"^\s*(?:"
        r"(?:(?:AB|SB|ACA|SCA|ACR|SCR|AJR|SJR|HR|SR)\s*\d+|\d+\s*Ch\.?\s*\d+)\s*(?:\u2014|--|\u2015)\s*\d+\s*(?:\u2014|--|\u2015)"
        r"|"
        r"(?:\u2014|--|\u2015)\s*\d+\s*(?:\u2014|--|\u2015)\s*(?:(?:AB|SB|ACA|SCA|ACR|SCR|AJR|SJR|HR|SR)\s*\d+|\d+\s*Ch\.?\s*\d+)"
        r")\s*$",
        re.MULTILINE | re.IGNORECASE,
    ),
    # CA page footer: bare number repeated on every page (e.g. "98") or trailing "O"
    re.compile(r"^\s*O\s*$", re.MULTILINE),                       # trailing O ornament
    # CT page headers: "Public Act No. 26-142 12 of 46", "LCO 1 of 29"
    re.compile(r"^\s*Public\s+Act\s+No\.\s*\d+[-–]\d+\s+\d+\s+of\s+\d+\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*LCO\s+\d+\s+of\s+\d+\s*$", re.MULTILINE | re.IGNORECASE),
    # CT: "LCO No. 1234"
    re.compile(r"^\s*LCO\s+No\.?\s*\d+\s*$", re.MULTILINE | re.IGNORECASE),
    # Generic state bill page headers: "HB 1234 — 5" or "SB 2-D" with page num
    re.compile(r"^\s*(?:House|Senate)\s+Bill\s+No\.?\s*\d+[-–]?\w*\s*$", re.MULTILINE | re.IGNORECASE),
]

# Patterns used for classifying differences
_RE_ARTIFACT = re.compile(
    r"(?:^\*[A-Z]{3}\d+\*|\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2}:\d{2}|"
    r"---\s*Page\s+\d+\s*---|Page\s+\d+\s+of\s+\d+|"
    r"CODING:\s*Words\s+.+?additions\.|"
    r"As\s+Engrossed:|^HB\d+$|^SB\d+$|"
    r"^(?:EH|SH|HB|SB|HJ|SJ)\s*\d+|"
    r"^T?LSB\s+\d+\w*|"
    r"^-\d+-\s+[a-z/]+\s+\d+/\d+|"
    r"Public\s+Act\s+No\.\s*\d+[-\u2013]\d+\s+\d+\s+of\s+\d+|"
    r"^LCO\s+\d+\s+of\s+\d+|"
    r"^LCO\s+No\.?\s*\d+)",  # CT page headers
    re.IGNORECASE,
)

# ── CA page header / footer patterns ──────────────────────────────
# CA bills have repeating page footers (a bare number like "98") and
# alternating page headers such as "AB 827 — 2 —" or "— 3 — AB 827".
# The trailing "O" on the last page is also a footer ornament.
_RE_CA_PAGE_HEADER = re.compile(
    r"^\s*(?:"
    # "AB 827 — 2 —" or "92 Ch. 574 — 2 —" (even pages)
    r"(?:(?:AB|SB|ACA|SCA|ACR|SCR|AJR|SJR|HR|SR)\s*\d+|\d+\s*Ch\.?\s*\d+)\s*(?:\u2014|--|\u2015)\s*\d+\s*(?:\u2014|--|\u2015)"
    r"|"
    # "— 3 — AB 827" or "— 3 — 92 Ch. 574" (odd pages)
    r"(?:\u2014|--|\u2015)\s*\d+\s*(?:\u2014|--|\u2015)\s*(?:(?:AB|SB|ACA|SCA|ACR|SCR|AJR|SJR|HR|SR)\s*\d+|\d+\s*Ch\.?\s*\d+)"
    r")\s*$",
    re.IGNORECASE,
)
_RE_CA_PAGE_FOOTER = re.compile(
    r"^\s*(?:\d{1,4}|O)\s*$",  # bare number like "98" or trailing "O"
)
# Spaced-out decorative headers (e.g. "F L O R I D A H O U S E ...")
# Matches text where most "words" are single letters separated by spaces.
_RE_SPACED_HEADER = re.compile(
    r"(?:[A-Z]\s+){4,}",  # at least 4 single letters in a row
    re.IGNORECASE,
)
_RE_HYPHEN_REJOIN = re.compile(r"^(\S+)-$")  # word ending with hyphen
_RE_LINE_MARKER_STD = re.compile(r"^\s*line\s+\d{1,3}\s*$", re.IGNORECASE)
_RE_LINE_MARKER_REV = re.compile(r"^\s*\d{1,3}\s+line\s*$", re.IGNORECASE)
_RE_BARE_PAGE_NUM_LINE = re.compile(r"^\s*\d{1,4}\s*$")
_RE_HEADER_WORDS = re.compile(
    r"(?:State\s+of|HOUSE\s+BILL|SENATE\s+BILL|General\s+Assembly|"
    r"Regular\s+Session|A\s+Bill|AN\s+ACT|Subtitle)",
    re.IGNORECASE,
)
_LARGE_DROP_THRESHOLD = 10  # words — anything ≥10 words dropped = paragraph drop


def _strip_artifacts(text: str) -> str:
    """Remove page-level artifacts from precleaned text."""
    for pattern in _ARTIFACT_PATTERNS:
        text = pattern.sub("", text)
    return text


def _strip_repeated_bare_number_lines(text: str) -> str:
    """Strip repeated standalone number lines (typical page footers).

    We only remove a bare-number line when the same number appears 2+ times
    in the same document, which is the common page-footer pattern (e.g. "95"
    at the bottom of every page). Single-occurrence numeric lines are kept.
    """
    lines = text.split("\n")
    counts: Dict[str, int] = {}
    for ln in lines:
        s = ln.strip()
        if _RE_BARE_PAGE_NUM_LINE.match(s):
            counts[s] = counts.get(s, 0) + 1

    repeated = {n for n, c in counts.items() if c >= 2}
    if not repeated:
        return text

    out_lines = []
    for ln in lines:
        s = ln.strip()
        if s in repeated and _RE_BARE_PAGE_NUM_LINE.match(s):
            continue
        out_lines.append(ln)
    return "\n".join(out_lines)


def _strip_tags(text: str) -> str:
    """Remove [A>...<A] and [D>...<D] tag markers (keep content)."""
    text = re.sub(r"\[([AD])>", "", text)
    text = re.sub(r"<([AD])\]", "", text)
    return text


def _to_words(text: str) -> List[str]:
    """Extract all non-whitespace tokens from text."""
    return re.findall(r"\S+", text)


def _classify_difference(
    diff_type: str,
    pre_text: Optional[str],
    out_text: Optional[str],
    words_affected: int,
    repeated_texts: Optional[set] = None,
    metadata_hf_texts: Optional[set] = None,
) -> Dict[str, Any]:
    """Classify a difference block into a category with explanation.

    Categories:
        content_drop      — real content missing from output
        paragraph_drop    — large block of content missing (≥10 words)
        content_added     — extra text in output not in source
        hyphen_rejoin     — line-break hyphenation fix (expected)
        header_reorder    — header lines rearranged (cosmetic)
        artifact_removed  — page markers/timestamps stripped (expected)
        formatting_fix    — minor formatting normalization

    Each result includes a ``content_type`` field describing what kind of
    content this is: "page_header", "page_footer", "page_number",
    "page_artifact", "legislative_text", "header_block", or "formatting".
    """
    pre = pre_text or ""
    out = out_text or ""

    # ── Artifact removal ──────────────────────────────────────────
    if diff_type == "delete" and _RE_ARTIFACT.search(pre):
        return {
            "category": "artifact_removed",
            "severity": "info",
            "explanation": f"Page artifact correctly stripped: \"{pre[:80]}\"",
            "is_expected": True,
            "content_type": "page_artifact",
        }

    # ── Repeated text detection (page headers/footers) ────────────
    # If the deleted text (or a base form of it) appears multiple times
    # in the precleaned source, it's a repeating page header/footer that
    # the LLM correctly removed.
    if diff_type == "delete" and repeated_texts and words_affected <= 20:
        # Normalize all standalone numbers to # (matches how we built the set)
        base = re.sub(r"(?<!\w)\d+(?!\w)", "#", pre).strip()
        if base in repeated_texts or pre in repeated_texts:
            # Determine content_type
            has_number = bool(re.search(r"\d+$", pre.strip()))
            _ctype = "page_header_with_page_number" if has_number else "page_header"
            return {
                "category": "artifact_removed",
                "severity": "info",
                "explanation": (
                    f"Repeating page header/footer correctly stripped: \"{pre[:80]}\""
                ),
                "is_expected": True,
                "content_type": _ctype,
            }
        # Also catch decorative lines (underscores, dashes)
        if re.match(r"^[_\-=~*.]{3,}$", pre.strip()):
            return {
                "category": "artifact_removed",
                "severity": "info",
                "explanation": f"Decorative line correctly stripped: \"{pre[:40]}\"",
                "is_expected": True,
                "content_type": "decorative_line",
            }

    # ── Metadata-based header/footer detection ────────────────────
    # If the deleted text matches known header/footer text from PDF
    # metadata, classify as expected artifact removal.
    if diff_type == "delete" and metadata_hf_texts and pre.strip():
        pre_lower = pre.strip().lower()
        for hf_text in metadata_hf_texts:
            hf_lower = hf_text.lower()
            # Exact match or precleaned text is a substring of a known header/footer
            if (pre_lower == hf_lower
                    or pre_lower in hf_lower
                    or hf_lower in pre_lower):
                return {
                    "category": "artifact_removed",
                    "severity": "info",
                    "explanation": f"PDF metadata header/footer correctly stripped: \"{pre[:80]}\"",
                    "is_expected": True,
                    "content_type": "page_header",
                }

    # ── Hyphen rejoin ─────────────────────────────────────────────
    if diff_type == "replace":
        # "export- limiting" (2 words) → "export-limiting" (1 word)
        pre_stripped = pre.replace(" ", "")
        out_stripped = out.replace(" ", "")
        if pre_stripped == out_stripped and "-" in pre:
            return {
                "category": "hyphen_rejoin",
                "severity": "info",
                "explanation": f"Hyphen rejoin fix: \"{pre}\" → \"{out}\"",
                "is_expected": True,
                "content_type": "formatting",
            }
        # Also catch "- " removal: "2025- 2026" → "2025-2026"
        if re.sub(r"-\s+", "-", pre) == out:
            return {
                "category": "hyphen_rejoin",
                "severity": "info",
                "explanation": f"Hyphen rejoin fix: \"{pre}\" → \"{out}\"",
                "is_expected": True,
                "content_type": "formatting",
            }

    # ── Header reorder ────────────────────────────────────────────
    if diff_type in ("replace", "insert", "delete"):
        text_to_check = pre or out
        if _RE_HEADER_WORDS.search(text_to_check) and words_affected <= 10:
            return {
                "category": "header_reorder",
                "severity": "info",
                "explanation": f"Header lines rearranged: \"{text_to_check[:80]}\"",
                "is_expected": True,
                "content_type": "header_block",
            }

    # ── Table/column line-number markers (CT "T1", "T2", "T8 T9", etc.) ─
    # Single or multiple uppercase letter + 1-3 digits — tab/column markers
    # in PDF tables that the LLM correctly strips during cleanup.
    if diff_type == "delete" and re.match(r"^(?:[A-Z]\d{1,3}\s*)+$", pre.strip()):
        return {
            "category": "artifact_removed",
            "severity": "info",
            "explanation": f"Table line-number marker correctly stripped: \"{pre}\"",
            "is_expected": True,
            "content_type": "page_artifact",
        }

    # ── Standalone line numbers (bare digits) ─────────────────────
    if diff_type == "delete" and re.match(r"^\d{1,5}$", pre.strip()):
        return {
            "category": "artifact_removed",
            "severity": "info",
            "explanation": f"Line number correctly stripped: \"{pre}\"",
            "is_expected": True,
            "content_type": "page_artifact",
        }

    # ── CA/legacy line markers: "line 22" or "22 line" ──────────
    if diff_type == "delete" and (_RE_LINE_MARKER_STD.match(pre) or _RE_LINE_MARKER_REV.match(pre)):
        return {
            "category": "artifact_removed",
            "severity": "info",
            "explanation": f"Line marker correctly stripped: \"{pre.strip()}\"",
            "is_expected": True,
            "content_type": "line_number_marker",
        }

    if diff_type == "insert" and (_RE_LINE_MARKER_STD.match(out) or _RE_LINE_MARKER_REV.match(out)):
        return {
            "category": "artifact_removed",
            "severity": "info",
            "explanation": f"Line marker insertion treated as artifact: \"{out.strip()}\"",
            "is_expected": True,
            "content_type": "line_number_marker",
        }

    # ── Page number patterns (e.g. "Page 1 of 2") ────────────────
    if diff_type == "delete" and re.match(
        r"^(?:Page\s+\d+\s+of\s+\d+|\d+\s+of\s+\d+)$", pre.strip(), re.IGNORECASE
    ):
        return {
            "category": "artifact_removed",
            "severity": "info",
            "explanation": f"Page number correctly stripped: \"{pre.strip()}\"",
            "is_expected": True,
            "content_type": "page_number",
        }

    # ── CA page headers / footers ─────────────────────────────────
    # Catch alternating CA headers like "AB 827 — 2 —" / "— 3 — AB 827"
    # and repeating footers like "98" or the trailing "O".
    if diff_type == "delete":
        _pre_s = pre.strip()
        if _RE_CA_PAGE_HEADER.match(_pre_s):
            return {
                "category": "artifact_removed",
                "severity": "info",
                "explanation": f"CA page header correctly stripped: \"{_pre_s}\"",
                "is_expected": True,
                "content_type": "page_header",
            }
        if _RE_CA_PAGE_FOOTER.match(_pre_s):
            return {
                "category": "artifact_removed",
                "severity": "info",
                "explanation": f"CA page footer correctly stripped: \"{_pre_s}\"",
                "is_expected": True,
                "content_type": "page_footer",
            }
        # Spaced-out decorative headers (e.g. "F L O R I D A H O U S E ...")
        # or combined with page number: "Page 1 of 3 F L O R I D A ..."
        if _RE_SPACED_HEADER.search(_pre_s):
            return {
                "category": "artifact_removed",
                "severity": "info",
                "explanation": f"Spaced-out page header correctly stripped: \"{_pre_s[:80]}\"",
                "is_expected": True,
                "content_type": "page_header",
            }

    # ── Content drop (paragraph or word level) ────────────────────
    if diff_type == "delete":
        if words_affected >= _LARGE_DROP_THRESHOLD:
            return {
                "category": "paragraph_drop",
                "severity": "critical",
                "explanation": (
                    f"PARAGRAPH DROP: {words_affected} words missing from output. "
                    f"Starts with: \"{pre[:120]}...\""
                ),
                "is_expected": False,
                "content_type": "legislative_text",
            }
        return {
            "category": "content_drop",
            "severity": "warning",
            "explanation": f"Content missing: \"{pre[:120]}\"",
            "is_expected": False,
            "content_type": "legislative_text",
        }

    # ── Content added ─────────────────────────────────────────────
    if diff_type == "insert":
        if words_affected >= _LARGE_DROP_THRESHOLD:
            return {
                "category": "content_added",
                "severity": "critical",
                "explanation": (
                    f"EXTRA CONTENT: {words_affected} words added. "
                    f"Starts with: \"{out[:120]}...\""
                ),
                "is_expected": False,
                "content_type": "legislative_text",
            }
        return {
            "category": "content_added",
            "severity": "warning",
            "explanation": f"Extra text added: \"{out[:120]}\"",
            "is_expected": False,
            "content_type": "legislative_text",
        }

    # ── Replace — could be content change or formatting fix ───────
    if diff_type == "replace":
        # Alignment artifact: one side is a standalone page/line-number marker.
        # SequenceMatcher can align these against nearby legal text and report
        # a false content change. Treat as expected artifact drift.
        _pre_s = pre.strip()
        _out_s = out.strip()
        _pre_art = (
            bool(_RE_BARE_PAGE_NUM_LINE.match(_pre_s))
            or bool(_RE_LINE_MARKER_STD.match(_pre_s))
            or bool(_RE_LINE_MARKER_REV.match(_pre_s))
        )
        _out_art = (
            bool(_RE_BARE_PAGE_NUM_LINE.match(_out_s))
            or bool(_RE_LINE_MARKER_STD.match(_out_s))
            or bool(_RE_LINE_MARKER_REV.match(_out_s))
        )
        if _pre_art or _out_art:
            return {
                "category": "artifact_removed",
                "severity": "info",
                "explanation": f"Artifact alignment shift: \"{_pre_s[:60]}\" → \"{_out_s[:60]}\"",
                "is_expected": True,
                "content_type": "page_artifact",
            }

        # Smart/curly quote normalization — PDFs use \u201c \u201d \u2018 \u2019
        # but the LLM may output straight quotes.  If that is the ONLY
        # difference, treat it as an expected formatting fix.
        _QUOTE_MAP = str.maketrans('\u201c\u201d\u2018\u2019\u00ab\u00bb', '""\'\'""')
        if pre.translate(_QUOTE_MAP) == out.translate(_QUOTE_MAP):
            return {
                "category": "formatting_fix",
                "severity": "info",
                "explanation": f"Quote normalization: \"{pre}\" → \"{out}\"",
                "is_expected": True,
                "content_type": "formatting",
            }
        # Small replacements (1-2 words) that look like formatting
        if words_affected <= 2:
            return {
                "category": "formatting_fix",
                "severity": "info",
                "explanation": f"Minor change: \"{pre}\" → \"{out}\"",
                "is_expected": True,
                "content_type": "formatting",
            }
        # Larger replacements — content was changed
        if words_affected >= _LARGE_DROP_THRESHOLD:
            return {
                "category": "content_changed",
                "severity": "critical",
                "explanation": (
                    f"CONTENT CHANGED: {words_affected} words replaced. "
                    f"\"{pre[:80]}...\" → \"{out[:80]}...\""
                ),
                "is_expected": False,
                "content_type": "legislative_text",
            }
        return {
            "category": "content_changed",
            "severity": "warning",
            "explanation": f"Content changed: \"{pre[:80]}\" → \"{out[:80]}\"",
            "is_expected": False,
            "content_type": "legislative_text",
        }

    return {
        "category": "unknown",
        "severity": "warning",
        "explanation": f"Unclassified difference",
        "is_expected": False,
        "content_type": "unknown",
    }


def check_content_accuracy(
    precleaned_text: str,
    output_text: str,
    *,
    strip_artifacts: bool = True,
    strip_tags: bool = True,
    metadata: Optional[Dict[str, Any]] = None,
    detected_artifacts: Optional[list] = None,
) -> Dict[str, Any]:
    """Compare precleaned input vs final output at word level.

    Parameters
    ----------
    precleaned_text : str
        The hybrid precleaned text (before LLM cleaning).
    output_text : str
        The final output text (after LLM cleaning).
    strip_artifacts : bool
        If True, strip page artifacts from precleaned text before comparing.
    strip_tags : bool
        If True, strip [A>/<A] and [D>/<D] tags from both texts.

    Returns
    -------
    dict with keys:
        accuracy_pct        : float — word-level accuracy percentage
        precleaned_words    : int   — total words in precleaned
        output_words        : int   — total words in output
        matching_words      : int   — words that match
        missing_words       : int   — words in precleaned but not in output
        extra_words         : int   — words in output but not in precleaned
        changed_words       : int   — words that were replaced
        difference_blocks   : int   — number of difference regions
        differences         : list  — detailed list of each difference with classification
        category_summary    : dict  — count of differences by category
        has_content_issues  : bool  — True if any non-expected differences found
        content_issues      : list  — only the non-expected (real) issues
        similarity_ratio    : float — raw SequenceMatcher ratio (0-1)
    """
    # Prepare texts
    pre = precleaned_text
    out = output_text

    if strip_artifacts:
        pre = _strip_artifacts(pre)
        pre = _strip_repeated_bare_number_lines(pre)
        out = _strip_repeated_bare_number_lines(out)

    if strip_tags:
        pre = _strip_tags(pre)
        out = _strip_tags(out)

    # ── Normalize quotes: curly apostrophes/quotes → straight ──────────
    # These are typographically equivalent and should never count as
    # a content difference.
    pre = pre.replace("\u2018", "'").replace("\u2019", "'")
    pre = pre.replace("\u201c", '"').replace("\u201d", '"')
    out = out.replace("\u2018", "'").replace("\u2019", "'")
    out = out.replace("\u201c", '"').replace("\u201d", '"')

    # ── Normalize dashes: em-dash/en-dash/double-hyphen → single hyphen ──
    # These are typographically equivalent in legislative text and should
    # never count as a content difference.
    pre = pre.replace("\u2014", "-").replace("\u2013", "-")
    pre = re.sub(r"--+", "-", pre)
    out = out.replace("\u2014", "-").replace("\u2013", "-")
    out = re.sub(r"--+", "-", out)

    # ── Normalize line-break hyphenations: "full- time" / "full-\ntime" → "full-time" ──
    # PDF text often has a space or newline after a hyphen at a line break.
    # The LLM rejoins them, so both sides should match.
    pre = re.sub(r'(\w)-\s+(\w)', r'\1-\2', pre)
    out = re.sub(r'(\w)-\s+(\w)', r'\1-\2', out)

    # ── Build repeated-text set ───────────────────────────────────
    # Detect lines that appear multiple times in precleaned text.
    # These are page headers/footers the LLM is expected to remove.
    # We strip trailing page numbers to find the repeating base pattern.
    # We also normalize ALL standalone numbers (surrounded by non-alnum)
    # to catch CA-style headers where the page number is embedded:
    #   "AB 827 — 2 —" / "AB 827 — 3 —" → base "AB 827 — # —"
    #   "— 2 — AB 827" / "— 3 — AB 827" → base "— # — AB 827"
    #   "92 Ch. 574 — 2 —" → base "# Ch. # — # —"
    #   "Page 1 of 2" / "Page 2 of 2" → base "Page # of #"
    _line_counts: Dict[str, int] = {}
    _line_to_base: Dict[str, str] = {}  # map original stripped → base
    for line in pre.split('\n'):
        stripped = line.strip()
        if not stripped or len(stripped) < 3:
            continue
        # Base form: replace all standalone numbers with #
        base = re.sub(r"(?<!\w)\d+(?!\w)", "#", stripped).strip()
        if base:
            _line_counts[base] = _line_counts.get(base, 0) + 1
            _line_to_base[stripped] = base

    # Lines appearing 2+ times are repeating headers/footers
    repeated_texts = {base for base, count in _line_counts.items() if count >= 2}

    # Build metadata header/footer text set for classification
    metadata_hf_texts: Optional[set] = None
    if metadata:
        _hf_set: set = set()
        for _pg, _txt in (metadata.get("headers") or {}).items():
            for _line in _txt.split("\n"):
                _s = _line.strip()
                if _s and len(_s) >= 3:
                    _hf_set.add(_s)
        for _pg, _txt in (metadata.get("footers") or {}).items():
            for _line in _txt.split("\n"):
                _s = _line.strip()
                if _s and len(_s) >= 3:
                    _hf_set.add(_s)
        if _hf_set:
            metadata_hf_texts = _hf_set

    # Merge detected repeating artifacts (from removals report)
    if detected_artifacts:
        if metadata_hf_texts is None:
            metadata_hf_texts = set()
        for _art in detected_artifacts:
            _art_text = _art.get("text", "") if isinstance(_art, dict) else str(_art)
            # Strip [A>...<A] tags if present
            _art_clean = re.sub(r"\[A>|<A\]", "", _art_text).strip()
            if _art_clean and len(_art_clean) >= 3:
                metadata_hf_texts.add(_art_clean)

    # Tokenize
    pre_words = _to_words(pre)
    out_words = _to_words(out)

    # Build a joined version of precleaned for substring checks
    # (used to detect false "content_added" from alignment disruption)
    _pre_joined = " ".join(pre_words)

    # Compare
    matcher = difflib.SequenceMatcher(None, pre_words, out_words)
    ratio = matcher.ratio()

    matching_words = sum(
        i2 - i1
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
        if tag == "equal"
    )

    max_words = max(len(pre_words), len(out_words))
    accuracy = (matching_words / max_words * 100) if max_words > 0 else 100.0

    # Categorize differences
    missing = 0
    extra = 0
    changed = 0
    differences: List[Dict[str, Any]] = []
    category_counts: Dict[str, int] = {}
    content_issues: List[Dict[str, Any]] = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue

        pre_text: Optional[str] = None
        out_text: Optional[str] = None
        words_affected = 0

        if tag == "delete":
            missing += (i2 - i1)
            pre_text = " ".join(pre_words[i1:i2])
            words_affected = i2 - i1
        elif tag == "insert":
            extra += (j2 - j1)
            out_text = " ".join(out_words[j1:j2])
            words_affected = j2 - j1
        elif tag == "replace":
            changed += max(i2 - i1, j2 - j1)
            pre_text = " ".join(pre_words[i1:i2])
            out_text = " ".join(out_words[j1:j2])
            words_affected = max(i2 - i1, j2 - j1)

        # Classify the difference
        classification = _classify_difference(tag, pre_text, out_text, words_affected, repeated_texts=repeated_texts, metadata_hf_texts=metadata_hf_texts)

        # ── False-positive guard for "content_added" ──────────────────
        # When repeated page headers disrupt SequenceMatcher alignment,
        # real text that exists in BOTH precleaned and output can appear
        # as an "insert". If the "added" text exists verbatim in the
        # precleaned, it's not truly added — it's an alignment artifact.
        if (classification["category"] == "content_added"
                and out_text
                and len(out_text) >= 10
                and out_text in _pre_joined):
            classification = {
                "category": "alignment_match",
                "severity": "info",
                "explanation": f"Text exists in source (alignment shift): \"{out_text[:80]}\"",
                "is_expected": True,
                "content_type": "legislative_text",
            }

        diff_entry = {
            "type": tag,
            "precleaned": pre_text,
            "output": out_text,
            "words_affected": words_affected,
            "output_word_start": j1,
            "output_word_end": j2,
            **classification,
        }
        differences.append(diff_entry)

        # Track category counts
        cat = classification["category"]
        category_counts[cat] = category_counts.get(cat, 0) + 1

        # Track content issues (non-expected)
        if not classification["is_expected"]:
            content_issues.append(diff_entry)

    # ── Post-diff: check for repeated artifacts still present in output ──
    # If a known repeated text (page header/footer) survived into the output,
    # flag it as a content issue even though the diff sees it as "equal".
    # Only flag if the line appears MORE THAN ONCE in the output — a single
    # occurrence is the legitimate page-1 header, not a repeated artifact.
    #
    # IMPORTANT: Only consider lines that actually look like page artifacts
    # (page numbers, bill identifiers, CODING lines, short header patterns).
    # Repeated legislative content (table headers, section titles) must NOT
    # be flagged as artifacts.
    _all_artifact_texts = set()

    # Only include repeated_texts entries that look like real page artifacts
    _RE_ARTIFACT_LIKE = re.compile(
        r"(?:"
        r"Page\s+\d+\s+of\s+\d+"          # "Page 1 of 29"
        r"|CODING:\s*Words"                 # FL CODING line
        r"|^\s*(?:ENROLLED|ENGROSSED)\s"   # ENROLLED marker
        r"|^\d{1,4}\s*$"                    # bare page number
        r"|^\s*O\s*$"                       # trailing O ornament
        r"|(?:\u2014|--)\s*\d+\s*(?:\u2014|--)"  # CA page headers
        r"|Public\s+Act\s+No\."            # CT public act
        r"|LCO\s+\d+\s+of\s+\d+"          # CT LCO
        r"|^\s*(?:AB|SB|HB|SB|CS\s+for)\s+(?:SB|HB)\s+\d+"  # bill identifiers
        r")",
        re.IGNORECASE,
    )

    if repeated_texts:
        for rt in repeated_texts:
            # Only add to artifact set if it matches artifact-like patterns
            if _RE_ARTIFACT_LIKE.search(rt):
                _all_artifact_texts.add(rt)
    if metadata_hf_texts:
        _all_artifact_texts.update(metadata_hf_texts)

    if _all_artifact_texts:
        # Count occurrences of each output line
        from collections import Counter
        _out_line_counts: Counter = Counter()
        for out_line in out.split('\n'):
            out_stripped = out_line.strip()
            if out_stripped and len(out_stripped) >= 3:
                _out_line_counts[out_stripped] += 1

        for out_stripped, count in _out_line_counts.items():
            # Only flag if the artifact appears more than once in output
            if count <= 1:
                continue
            # Check if this output line matches a known artifact
            out_base = re.sub(r"\s+\d+\s*$", "", out_stripped).strip()
            if out_base in _all_artifact_texts or out_stripped in _all_artifact_texts:
                _words_in_line = len(out_stripped.split())
                # Flag each extra occurrence (keep one as legitimate)
                for _ in range(count - 1):
                    _issue = {
                        "type": "artifact_retained",
                        "precleaned": None,
                        "output": out_stripped,
                        "words_affected": _words_in_line,
                        "category": "artifact_not_removed",
                        "severity": "medium",
                        "explanation": f"Repeated artifact still present in output ({count}x): \"{out_stripped[:80]}\"",
                        "is_expected": False,
                        "content_type": "page_header",
                    }
                    content_issues.append(_issue)
                    differences.append(_issue)
                cat = "artifact_not_removed"
                category_counts[cat] = category_counts.get(cat, 0) + (count - 1)
                extra += _words_in_line * (count - 1)

    # Calculate adjusted accuracy: exclude expected differences from the
    # denominator so they don't penalize the score.  The numerator is simply
    # the count of words that matched (already excludes all diffs).
    expected_words_removed = sum(
        d["words_affected"] for d in differences if d.get("is_expected")
    )
    issue_words = sum(
        d["words_affected"] for d in differences if not d.get("is_expected")
    )
    # Post-diff issues (like artifact_retained) were counted as "equal" by
    # SequenceMatcher because the text exists on both sides.  Subtract them
    # from matching_words so they penalize accuracy correctly.
    post_diff_issue_words = sum(
        d["words_affected"] for d in differences
        if not d.get("is_expected") and d.get("type") == "artifact_retained"
    )
    adjusted_matching = matching_words - post_diff_issue_words
    adjusted_max = max_words - expected_words_removed
    # If there are real content issues, ensure accuracy reflects them.
    # The issue_words are already excluded from matching_words by
    # SequenceMatcher (they appear as delete/insert/replace, not equal),
    # but we must also exclude expected_words_removed from the denominator
    # WITHOUT letting the ratio exceed 1.0 when the removed words and
    # issue words happen to cancel out numerically.
    if issue_words > 0 and adjusted_max > 0 and adjusted_matching >= adjusted_max:
        # Force accuracy below 100% — use the raw ratio instead
        adjusted_accuracy = (matching_words / max_words * 100) if max_words > 0 else 100.0
    else:
        adjusted_accuracy = (adjusted_matching / adjusted_max * 100) if adjusted_max > 0 else 100.0
    adjusted_accuracy = max(min(adjusted_accuracy, 100.0), 0.0)  # Clamp 0-100%

    return {
        "accuracy_pct": round(adjusted_accuracy, 2),
        "raw_accuracy_pct": round(accuracy, 2),
        "precleaned_words": len(pre_words),
        "output_words": len(out_words),
        "matching_words": matching_words,
        "missing_words": missing,
        "extra_words": extra,
        "changed_words": changed,
        "expected_removals": expected_words_removed,
        "difference_blocks": len(differences),
        "differences": differences,
        "category_summary": category_counts,
        "has_content_issues": len(content_issues) > 0,
        "content_issues": content_issues,
        "content_issue_count": len(content_issues),
        "similarity_ratio": round(ratio, 6),
    }


def check_content_accuracy_from_files(
    precleaned_path: str | Path,
    output_path: str | Path,
    **kwargs,
) -> Dict[str, Any]:
    """Load files and run content accuracy check."""
    pre_text = Path(precleaned_path).read_text(encoding="utf-8")
    out_text = Path(output_path).read_text(encoding="utf-8")
    result = check_content_accuracy(pre_text, out_text, **kwargs)
    result["precleaned_file"] = str(precleaned_path)
    result["output_file"] = str(output_path)
    return result


def find_session_files(session_path: str | Path) -> Optional[Dict[str, Path]]:
    """Auto-detect precleaned and output files from a session folder.

    Looks for:
        {session}/input/*_hybrid_precleaned.txt
        {session}/output/*.txt (excluding accuracy/diff/chunk/removal files)
    """
    session = Path(session_path)
    input_dir = session / "input"
    output_dir = session / "output"

    if not input_dir.exists() or not output_dir.exists():
        return None

    # Find precleaned file
    precleaned_files = list(input_dir.glob("*_hybrid_precleaned.txt"))
    if not precleaned_files:
        return None

    # Find output file (exclude accuracy/diff reports)
    output_files = [
        f for f in output_dir.glob("*.txt")
        if not any(x in f.name for x in ("accuracy", "diff", "chunk", "removal"))
    ]
    if not output_files:
        return None

    return {
        "precleaned": precleaned_files[0],
        "output": output_files[0],
    }


def print_report(result: Dict[str, Any]) -> None:
    """Print a formatted accuracy report to stdout."""
    sep = "=" * 70
    print(sep)
    print("CONTENT ACCURACY REPORT")
    print(sep)

    if "precleaned_file" in result:
        print(f"Precleaned: {Path(result['precleaned_file']).name}")
        print(f"Output:     {Path(result['output_file']).name}")
        print()

    icon = (
        "OK" if result["accuracy_pct"] >= 99
        else "WARN" if result["accuracy_pct"] >= 95
        else "FAIL"
    )
    print(f"[{icon}] Word Accuracy: {result['accuracy_pct']:.2f}%")
    print(f"  Similarity ratio: {result['similarity_ratio']:.4f}")
    print()
    print(f"  Precleaned words: {result['precleaned_words']:,}")
    print(f"  Output words:     {result['output_words']:,}")
    print(f"  Matching words:   {result['matching_words']:,}")
    print()
    print(f"  Missing words:    {result['missing_words']}")
    print(f"  Extra words:      {result['extra_words']}")
    print(f"  Changed words:    {result['changed_words']}")
    print(f"  Difference blocks: {result['difference_blocks']}")

    # Category summary
    cats = result.get("category_summary", {})
    if cats:
        print()
        print("-" * 70)
        print("CATEGORY BREAKDOWN")
        print("-" * 70)
        for cat, count in sorted(cats.items()):
            print(f"  {cat}: {count}")

    # Content issues (non-expected)
    issues = result.get("content_issues", [])
    if issues:
        print()
        print("-" * 70)
        print(f"CONTENT ISSUES ({len(issues)} found)")
        print("-" * 70)
        for i, diff in enumerate(issues, 1):
            sev = diff.get("severity", "").upper()
            cat = diff.get("category", "")
            expl = diff.get("explanation", "")
            print(f"\n  [{i}] [{sev}] {cat}")
            print(f"       {expl}")
    elif result.get("difference_blocks", 0) > 0:
        print()
        print("  No content issues — all differences are expected (fixes/cleanup)")

    if result.get("differences"):
        print()
        print("-" * 70)
        print("ALL DIFFERENCES (detailed)")
        print("-" * 70)
        for i, diff in enumerate(result["differences"], 1):
            dtype = diff["type"].upper()
            cat = diff.get("category", "unknown")
            sev = diff.get("severity", "")
            pre = diff.get("precleaned") or ""
            out = diff.get("output") or ""
            affected = diff.get("words_affected", 0)
            expected = "expected" if diff.get("is_expected") else "UNEXPECTED"

            print(f"\n  [{i}] {dtype} | {cat} | {sev} | {expected} ({affected} words)")
            if pre:
                print(f"       Source: {pre[:120]}")
            if out:
                print(f"       Output: {out[:120]}")
            expl = diff.get("explanation", "")
            if expl:
                print(f"       -> {expl}")

    print()
    print(sep)


def main():
    parser = argparse.ArgumentParser(
        description="Content accuracy checker — word-level comparison"
    )
    parser.add_argument(
        "files", nargs="*",
        help="Two files: precleaned.txt output.txt"
    )
    parser.add_argument(
        "--session", "-s",
        help="Session folder path (auto-detects input/output files)"
    )
    parser.add_argument(
        "--json", "-j", action="store_true",
        help="Output result as JSON"
    )
    parser.add_argument(
        "--save", "-o",
        help="Save JSON report to this file path"
    )

    args = parser.parse_args()

    # Determine file paths
    if args.session:
        files = find_session_files(args.session)
        if not files:
            print(f"ERROR: Could not find precleaned/output files in {args.session}")
            sys.exit(1)
        precleaned_path = files["precleaned"]
        output_path = files["output"]
    elif len(args.files) == 2:
        precleaned_path = Path(args.files[0])
        output_path = Path(args.files[1])
    else:
        parser.print_help()
        print("\nERROR: Provide two file paths or --session <path>")
        sys.exit(1)

    if not precleaned_path.exists():
        print(f"ERROR: File not found: {precleaned_path}")
        sys.exit(1)
    if not output_path.exists():
        print(f"ERROR: File not found: {output_path}")
        sys.exit(1)

    # Run check
    result = check_content_accuracy_from_files(precleaned_path, output_path)

    # Output
    if args.json:
        # Remove full differences list for cleaner JSON output
        json_result = {k: v for k, v in result.items() if k != "differences"}
        json_result["difference_summary"] = [
            {k: v for k, v in d.items() if k != "words_affected"}
            for d in result["differences"]
        ]
        print(json.dumps(json_result, indent=2))
    else:
        print_report(result)

    # Save if requested
    if args.save:
        save_path = Path(args.save)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        json_result = {k: v for k, v in result.items()}
        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(json_result, f, indent=2)
        print(f"\nReport saved to: {save_path}")


if __name__ == "__main__":
    main()
