"""Text-accuracy helpers — classify intentional LLM removals.

Background
----------
The text-accuracy comparison (``compare_texts``) finds all words that exist
in the raw PyMuPDF extraction but are missing from the LLM-cleaned output.
Many of those "missing" words are **intentional removals** dictated by the
state cleaning prompt:

* page markers (``<<<PAGE_3_START>>>``)
* line numbers (``"    23  commissioner shall..."``)
* repeated headers on pages 2+ (e.g. ``"Raised Bill No. 6849"``)
* page footers (page X of Y, LCO 1234, date stamps)
* decorative fill (``"_______________________________"``)

When the comparison flags these as "missing", text accuracy drops below 100 %
even though the pipeline behaved exactly as instructed.  This module provides
a pure-Python classifier that lets the comparison decide *was that removal
expected?* before counting it against accuracy.

API
---
``classify_removed_text(text, state_code)``
    Take any candidate "missing" text block and return either ``None`` (looks
    like real content) or a dict with ``category``, ``rule``, ``confidence``,
    ``matched_pattern`` describing the kind of artifact it matched.

``build_removals_report(raw_text, clean_text, state_code)``
    Use SequenceMatcher to find every line removed during cleaning, classify
    each one, and return a structured summary suitable for JSON persistence.

The patterns are deliberately conservative: when in doubt, return ``None``
so the diff is still counted as a real-content drop.  Better to undercount
allowed removals than to mask a genuine content loss.
"""

from __future__ import annotations

import difflib
import re
from typing import Any


# ─────────────────────────────────────────────────────────────────────
# Category constants
# ─────────────────────────────────────────────────────────────────────
CAT_PAGE_MARKER  = "page_marker"
CAT_LINE_NUMBER  = "line_number"
CAT_HEADER       = "header"
CAT_FOOTER       = "footer"
CAT_PAGE_NUMBER  = "page_number"
CAT_DATE_STAMP   = "date_stamp"
CAT_DECORATIVE   = "decorative"
CAT_WHITESPACE   = "whitespace"
CAT_TAG_ARTIFACT = "tag_artifact"

# Human-readable rule labels (matches what the prompts say)
RULE_LABELS = {
    CAT_PAGE_MARKER:  "Page conversion markers (<<<PAGE_N_START/END>>>)",
    CAT_LINE_NUMBER:  "Line numbers at start of line",
    CAT_HEADER:       "Repeated headers on pages 2+",
    CAT_FOOTER:       "Page footers (page numbers, file IDs)",
    CAT_PAGE_NUMBER:  "Standalone page numbers",
    CAT_DATE_STAMP:   "Date/version stamps",
    CAT_DECORATIVE:   "Decorative fill (underscores, dashes)",
    CAT_WHITESPACE:   "Whitespace-only differences",
    CAT_TAG_ARTIFACT: "Tag/markup artifact",
}


# ─────────────────────────────────────────────────────────────────────
# Universal patterns (apply to all states)
# ─────────────────────────────────────────────────────────────────────

# Page markers inserted by PyMuPDF / pdfplumber extractors
_RE_PAGE_MARKER = re.compile(
    r"""
    ^\s*
    (?:
        <{2,4}\s*PAGE[_\s]*\d+[_\s]*(?:START|END)\s*>{2,4}   # <<<PAGE_3_START>>>
        |
        ---\s*Page\s+\d+\s*---                            # --- Page 3 ---
        |
        \[Page\s+\d+\s+of\s+\d+\]                          # [Page 3 of 12]
    )
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Line numbers: leading spaces + 1-3 digit number + whitespace + content
# We only match when the whole STRIPPED line is just a number (the comparison
# already breaks lines into words so each line-number ends up as its own
# missing-word candidate).
_RE_LINE_NUMBER_ALONE = re.compile(r"^\s*\d{1,3}\s*$")

# Letter-prefixed line/tab markers: "T1", "T2", "S3", etc.
# CT bills use these as column/tab markers in the PDF.  They appear as
# single uppercase-letter + 1-3 digits (e.g. "T1", "T12", "S3").
_RE_LETTER_LINE_NUMBER = re.compile(r"^\s*(?:[A-Z]\d{1,3}\s*)+$")

# Trailing line numbers: right-aligned 1-3 digit number at end of a content
# line.  Used by ``strip_trailing_line_numbers`` (sequence-validated, so a
# stray "rate of 5" at end of paragraph is never stripped).
#   - Group 1: kept prefix (content up to the number, no trailing whitespace)
#   - Group 2: the candidate number itself
#   - Group 3: optional trailing newline (preserved on rewrite)
# Uses [ \t] (NOT \s) on both sides of the number so it never eats the
# trailing newline that delimits the next line.
_RE_TRAILING_LINE_NUMBER = re.compile(r"^(.+?\S)[ \t]+(\d{1,3})[ \t]*(\r?\n)?$")

# "X of Y" tail used inside page footers (e.g. "LCO No. 3866   1 of 3").
# We skip lines that match this so the trailing-number stripper doesn't
# chop the "3" off the page count and break later footer classification.
_RE_PAGE_OF_TAIL = re.compile(r"\b\d+\s+of\s*$", re.IGNORECASE)

# Header / identifier tails — the trailing number here is the bill / file
# / LCO / public-act number, NOT a leaked body-line number.  Lines whose
# *prefix* (the content before the trailing number) ends with any of
# these tokens are excluded as line-number candidates so the strip
# function doesn't latch onto e.g. "Raised Bill No. 514" and refuse to
# work because the bill number is wildly above the expected "starts near
# 1" line-number anchor.
#   Matches: "...Raised Bill No.", "...H.B. No.", "...S.B. No.",
#            "...LCO No.", "...File No.", "...Public Act No.",
#            "...Substitute No.", "...Sec.", "...Section",
#            "...Subsec.", "...Chapter", "...Title"
_RE_PREFIX_IDENTIFIER_TAIL = re.compile(
    r"(?:"
    r"\bNo\.?"                      # "No" or "No."  (covers Bill No., LCO No., File No., etc.)
    r"|\bAct"                        # "...Act 5"
    r"|\bSec(?:tion|\.)?"            # "Sec.", "Sec", "Section"
    r"|\bSubsec(?:tion|\.)?"         # "Subsec.", "Subsection"
    r"|\bChapter"
    r"|\bTitle"
    r"|\bPart"
    r"|\bArticle"
    r"|\bRule"
    r")\s*$",
    re.IGNORECASE,
)

# Header-wrap continuation: short, all-caps fragment that PyMuPDF emits as
# its own line when a long header overflows the printed page width.  Used
# by ``merge_wrapped_artifact_lines`` to fuse split headers back together.
#   "Referred to Committee on EXECUTIVE AND LEGISLATIVE"
#   "NOMINATIONS"                                          ← this pattern
# Rules (conservative — avoid swallowing body content):
#   - 1 to 3 uppercase tokens (optionally hyphenated)
#   - no digits, no terminating period / colon / semicolon
#   - line must be the entire content of the row (anchored)
_RE_HEADER_WRAP_CONTINUATION = re.compile(
    r"^\s*[A-Z][A-Z\-]+(?:\s+[A-Z][A-Z\-]+){0,2}\s*$"
)

# PyMuPDF table row markers — ``T1``, ``T2``, … ``T36`` that prefix each
# table cell line when PyMuPDF extracts a tabular layout.  CT bills (and
# any state with tax-rate schedules / tabular fee structures) hit this
# heavily: a 28-row tax table emits 28 "Tx ..." cell lines.  The LLM
# correctly strips the markers, but the word-level diff then sees them
# as MISSING_IN_CONVERTED.  Used by ``strip_table_row_markers``.
#
# Two variants are recognised:
#   1. ``T<n> <cell content>``       — marker + non-empty cell payload
#   2. ``T<n>``                       — bare marker on its own line
#                                       (PyMuPDF emits these when the
#                                       cell payload wrapped to the next
#                                       line or the cell is empty)
# Both feed the same sequence-validation gate so a body line that happens
# to start with ``T<n>`` cannot be touched in isolation.
#   - Group 1: leading whitespace (preserved)
#   - Group 2: the row number
#   - Group 3: the cell content (may be empty for variant 2)
#   - Group 4: optional trailing newline (preserved)
_RE_TABLE_ROW_MARKER = re.compile(
    r"^([ \t]*)T(\d{1,3})(?:[ \t]+(\S.*?))?[ \t]*(\r?\n)?$"
)

# CT title-block 2-column artifact patterns.  CT PDFs render the page-1
# title block in two visual columns; PyMuPDF reads them in row-major
# order and emits a single jammed line such as:
#
#   "General Assembly Proposed Bill No. 1200"          (3-line block — proposed/raised/committee)
#   "General Assembly    Raised Bill No. 6851"         (3+ spaces variant)
#   "General Assembly [A>File No. 629 <A]"             (5-line block — post-committee substitute)
#
# The LLM cleaning step correctly reorders the words into the canonical
# CT title-block order (bill / file identifier first on its own line,
# then "General Assembly" on its own line).  The word-level diff would
# otherwise flag "General Assembly" as repositioned and lose ~3 pp of
# accuracy on every CT bill.  Used by ``normalise_ct_title_block`` to
# rewrite the PyMuPDF side back into canonical order before the diff.
#
# Both patterns optionally accept ``[A>...<A]`` tag wrappers because the
# CT extractor's underline detection sometimes wraps the entire title
# row in addition / deletion markup.
#
# Pattern 1 — Bill identifier (Raised / Proposed / Committee / Substitute):
#   Group 1: leading whitespace (preserved)
#   Group 2: the bill identifier ("Proposed Bill No. 1200", etc.)
_RE_CT_TITLE_BLOCK_BILL = re.compile(
    r"""
    ^([ \t]*)                                            # 1: leading ws
    (?:\[A>\s*)?                                          # optional [A> open tag
    General\s+Assembly                                    # the "General Assembly" label
    [ \t]+                                                # at least one space
    (?:\[A>\s*)?                                          # optional [A> wrapper around identifier
    (                                                     # 2: bill-identifier group
        (?:Raised|Proposed|Committee|Substitute\s+(?:Senate|House))
        \s+(?:Bill|Resolution)\s+No\.?\s*\d+
    )
    \s*(?:<A\])?                                          # optional <A] close tag
    [ \t]*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Pattern 2 — File No. identifier (post-committee substitute bills):
#   Group 1: leading whitespace (preserved)
#   Group 2: the file identifier ("File No. 629", etc.)
_RE_CT_TITLE_BLOCK_FILE = re.compile(
    r"""
    ^([ \t]*)                                            # 1: leading ws
    (?:\[A>\s*)?                                          # optional [A> open tag
    General\s+Assembly                                    # the "General Assembly" label
    [ \t]+                                                # at least one space
    (?:\[A>\s*)?                                          # optional [A> wrapper around identifier
    (File\s+No\.?\s*\d+)                                  # 2: "File No. 629"
    \s*(?:<A\])?                                          # optional <A] close tag
    [ \t]*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Whole line is a small number (page number footer)
_RE_PAGE_NUMBER = re.compile(r"^\s*-?\s*\d{1,3}\s*-?\s*$")

# Decorative underscore / dash fill
_RE_DECORATIVE = re.compile(r"^\s*[_\-=*]{3,}\s*$")

# Pure whitespace
_RE_WHITESPACE = re.compile(r"^\s*$")

# Tag delimiter fragments left over from boundary mismatches
_RE_TAG_FRAGMENT = re.compile(r"^\s*(?:\[[AD]>|<[AD]\])\s*$")

# Date / version stamps commonly seen in footers
#   "Jan 23, 2025", "January 23, 2025", "01/23/2025", "2025-01-23",
#   "LCO 3952  v1", "Rev. 2"
_RE_DATE_STAMP = re.compile(
    r"""
    ^\s*
    (?:
        (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)
            (?:uary|ruary|ch|il|ust|tember|ober|ember|ember)?
        \s+\d{1,2}(?:,\s*|,?\s+)\d{4}
        |
        \d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}            # 01/23/2025
        |
        \d{4}-\d{2}-\d{2}                            # 2025-01-23
        |
        (?:Rev\.?|v\.?)\s*\d+(?:\.\d+)?              # Rev. 2 / v1
    )
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


# ─────────────────────────────────────────────────────────────────────
# Per-state header/footer patterns
# ─────────────────────────────────────────────────────────────────────

# Each state's patterns are kept tight enough that real legislative content
# never matches.  Add new states by adding entries here.
STATE_PATTERNS: dict[str, dict[str, list[re.Pattern]]] = {
    "CT": {
        CAT_HEADER: [
            # Single-token CT headers (start of line)
            re.compile(r"^\s*Raised\s+Bill\s+No\.\s*\d+", re.IGNORECASE),
            re.compile(r"^\s*Substitute\s+Bill\s+No\.\s*\d+", re.IGNORECASE),
            re.compile(r"^\s*LCO\s+No\.?\s*\d+", re.IGNORECASE),
            re.compile(r"^\s*General\s+Assembly\s*$", re.IGNORECASE),
            re.compile(r"^\s*(?:January|February|May|June|July)\s+Session,\s*\d{4}", re.IGNORECASE),
            re.compile(r"^\s*Committee\s+on\b", re.IGNORECASE),
            re.compile(r"^\s*Referred\s+to\s+Committee\s+on\b", re.IGNORECASE),
            # Compact bill designators on pages 2+:
            #   "sSB508 / File No. 620"     ← plain page header (with slash)
            #   "[A>SB506 File No. 619 <A]"  ← wrapped in A-tag, no slash
            # PyMuPDF emits both forms on the same document depending on
            # whether the decorative underline overlapped the slash, so we
            # make the ``/`` optional here.
            re.compile(r"^\s*[sH]?[SH]B\s*\d+\s*/?\s*File\s+No\.\s*\d+", re.IGNORECASE),
            # Two-column CT page-1 headers: "General Assembly      Raised Bill No. 6849"
            # We allow whitespace-only separators (3+ spaces) between fragments.
            re.compile(
                r"^\s*General\s+Assembly\s{3,}.*?"
                r"(?:Raised|Substitute|Proposed|Committee)\s+(?:Bill|Resolution)\s+No\.\s*\d+",
                re.IGNORECASE,
            ),
            re.compile(
                r"^\s*(?:January|February|May|June|July)\s+Session,\s*\d{4}\s{3,}.*?LCO\s+No\.?\s*\d+",
                re.IGNORECASE,
            ),
        ],
        CAT_FOOTER: [
            re.compile(r"^\s*LCO\s+\d+\b", re.IGNORECASE),                       # LCO 3952
            re.compile(r"^\s*LCO\s+No\.?\s*\d+\s+\d+\s+of\s+\d+", re.IGNORECASE), # LCO No. 3952  1 of 15
            re.compile(r"^\s*\d+\s+of\s+\d+\s*$", re.IGNORECASE),                 # 1 of 15
        ],
    },
    "DE": {
        CAT_HEADER: [
            re.compile(r"^\s*HOUSE\s+(?:BILL|RESOLUTION)\s+NO\.\s*\d+", re.IGNORECASE),
            re.compile(r"^\s*SENATE\s+(?:BILL|RESOLUTION)\s+NO\.\s*\d+", re.IGNORECASE),
            re.compile(r"^\s*\d+(?:st|nd|rd|th)\s+GENERAL\s+ASSEMBLY", re.IGNORECASE),
            re.compile(r"^\s*Page\s+\d+\s+of\s+\d+", re.IGNORECASE),
        ],
        CAT_FOOTER: [
            re.compile(r"^\s*Page\s+\d+\s+of\s+\d+", re.IGNORECASE),
        ],
    },
    "KS": {
        CAT_HEADER: [
            re.compile(r"^\s*Session\s+of\s+\d{4}", re.IGNORECASE),
            re.compile(r"^\s*(?:HOUSE|SENATE)\s+BILL\s+No\.\s*\d+", re.IGNORECASE),
            re.compile(r"^\s*HB\s*\d+", re.IGNORECASE),
            re.compile(r"^\s*SB\s*\d+", re.IGNORECASE),
        ],
        CAT_FOOTER: [],
    },
    "CA": {
        CAT_HEADER: [
            re.compile(r"^\s*CALIFORNIA\s+LEGISLATURE", re.IGNORECASE),
            re.compile(r"^\s*\d{4}-\d{2}\s+REGULAR\s+SESSION", re.IGNORECASE),
            re.compile(r"^\s*(?:ASSEMBLY|SENATE)\s+BILL\s+No\.\s*\d+", re.IGNORECASE),
            # Match CA page headers like "AB 223       — 2 —" but NOT
            # digest captions like "AB 223, Pacheco. Jury selection: ..."
            # Require the line to be ONLY the bill ref (optionally with
            # dash-page-dash suffix) — no comma or prose after.
            re.compile(r"^\s*AB\s*\d+\s*(?:[\u2014\u2013\-]\s*\d+\s*[\u2014\u2013\-])?\s*$", re.IGNORECASE),
        ],
        CAT_FOOTER: [
            re.compile(r"^\s*\d+\s*$"),  # bare page numbers
        ],
    },
    "IN": {
        CAT_HEADER: [
            re.compile(r"^\s*(?:HOUSE|SENATE)\s+BILL\s+No\.\s*\d+", re.IGNORECASE),
            re.compile(r"^\s*Introduced\s+Version", re.IGNORECASE),
            re.compile(r"^\s*\d+(?:st|nd|rd|th)\s+GENERAL\s+ASSEMBLY", re.IGNORECASE),
        ],
        CAT_FOOTER: [],
    },
    "FL": {
        CAT_HEADER: [
            re.compile(r"^\s*FLORIDA\s+(?:HOUSE|SENATE)\s+OF", re.IGNORECASE),
            re.compile(r"^\s*(?:HB|SB|HR|SR)\s*\d+", re.IGNORECASE),
        ],
        CAT_FOOTER: [
            re.compile(r"^\s*CODING:", re.IGNORECASE),
            re.compile(r"^\s*Page\s+\d+\s+of\s+\d+", re.IGNORECASE),
        ],
    },
    "AR": {
        CAT_HEADER: [
            re.compile(r"^\s*State\s+of\s+Arkansas", re.IGNORECASE),
            re.compile(r"^\s*\d+(?:st|nd|rd|th)\s+General\s+Assembly", re.IGNORECASE),
            re.compile(r"^\s*(?:HOUSE|SENATE)\s+BILL\s+\d+", re.IGNORECASE),
        ],
        CAT_FOOTER: [],
    },
    "IA": {
        CAT_HEADER: [
            re.compile(r"^\s*(?:HOUSE|SENATE)\s+FILE\s+\d+", re.IGNORECASE),
            re.compile(r"^\s*(?:HF|SF)\s*\d+", re.IGNORECASE),
            re.compile(r"^\s*\d+(?:st|nd|rd|th)\s+General\s+Assembly", re.IGNORECASE),
        ],
        CAT_FOOTER: [
            # Iowa bill footers (full line):
            #   "TLSB 1895YH (4) da/ns H.F. 1"
            #   "LSB 1895YH (4) -1- lh/ko 1/32 H.F. 1"
            re.compile(r"^\s*T?LSB\s+\w+\s*\(\d+\)", re.IGNORECASE),
            # Footer fragments (word-level diff remnants):
            #   "da/ns H.F. 1"   "-1- lh/ko 1/32 1"   "lh/ko 1"
            #   Drafter initials are 2-4 lowercase letters separated by /
            #   e.g. da/ns, lh/ko, jm/jh, rn/md
            re.compile(r"^\s*-?\d*-?\s*[a-z]{1,4}/[a-z]{1,4}\b", re.IGNORECASE),
            re.compile(r"^\s*\d+/\d+\s+[HS]\.F\.\s*\d+", re.IGNORECASE),    # "1/15 H.F. 1"
            re.compile(r"^\s*[HS]\.F\.\s*\d+\s*$", re.IGNORECASE),           # "H.F. 1"
            # Bare page refs like "House File 310, p. 2"
            re.compile(r"^\s*(?:House|Senate)\s+File\s+\d+,\s*p\.\s*\d+", re.IGNORECASE),
        ],
    },
    "CO": {
        CAT_HEADER: [
            re.compile(r"^\s*(?:HOUSE|SENATE)\s+BILL\s+\d+-\d+", re.IGNORECASE),
        ],
        CAT_FOOTER: [],
    },
    "DC": {
        CAT_HEADER: [
            re.compile(r"^\s*COUNCIL\s+OF\s+THE\s+DISTRICT", re.IGNORECASE),
        ],
        CAT_FOOTER: [],
    },
}


# ─────────────────────────────────────────────────────────────────────
# PDF-metadata-based allowlist
# ─────────────────────────────────────────────────────────────────────
# When we have actual header/footer text extracted from the PDF via
# PyMuPDF, we can use those exact strings as an allowlist — far more
# accurate than regex patterns alone.

_pdf_header_footer_texts: list[str] = []  # module-level cache


def set_pdf_metadata_allowlist(metadata: dict | None) -> None:
    """Populate the header/footer allowlist from PDF metadata.

    Call this BEFORE running text accuracy checks.  The metadata dict
    should come from ``paragraph_accuracy_checker.extract_full_metadata()``.

    Parameters
    ----------
    metadata : dict or None
        Dict with "headers" and "footers" keys (page→text mappings).
        Pass None to clear the allowlist.
    """
    global _pdf_header_footer_texts
    _pdf_header_footer_texts = []

    if not metadata:
        return

    # Collect all unique header/footer text fragments
    seen = set()
    for zone_key in ("headers", "footers"):
        zone = metadata.get(zone_key, {})
        for _page, text in zone.items():
            # Split multi-line zone text into individual lines
            for line in text.splitlines():
                stripped = line.strip()
                if stripped and stripped not in seen and len(stripped) > 1:
                    seen.add(stripped)
                    _pdf_header_footer_texts.append(stripped)


def _classify_against_pdf_metadata(text: str) -> dict | None:
    """Check if text matches any header/footer extracted from the PDF."""
    if not _pdf_header_footer_texts:
        return None

    stripped = text.strip()
    if not stripped:
        return None

    # Normalize: remove tags, collapse whitespace
    normalized = re.sub(r'\[([AD])>(.*?)<\1\]', r'\2', stripped, flags=re.DOTALL)
    normalized = re.sub(r'\s+', ' ', normalized).strip().lower()

    for hf_text in _pdf_header_footer_texts:
        hf_norm = re.sub(r'\s+', ' ', hf_text).strip().lower()
        # Exact match
        if normalized == hf_norm:
            return _hit(CAT_HEADER, f"pdf_metadata_exact: {hf_text[:40]}", "high")
        # Substring match (the diff text is part of a header/footer)
        if len(normalized) >= 3 and normalized in hf_norm:
            return _hit(CAT_HEADER, f"pdf_metadata_substring: {hf_text[:40]}", "high")
        # Reverse: header/footer text is part of the diff text
        if len(hf_norm) >= 5 and hf_norm in normalized:
            return _hit(CAT_FOOTER, f"pdf_metadata_contains: {hf_text[:40]}", "high")

    return None


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────

def classify_removed_text(
    text: str,
    state_code: str | None = None,
) -> dict[str, Any] | None:
    """Classify a removed text block as a known artifact type.

    Parameters
    ----------
    text
        The text that exists in the raw extraction but is missing from the
        cleaned output.  Can be a single line or several whitespace-joined
        words.  The function inspects the entire string.
    state_code
        Optional two-letter state code (``"CT"``, ``"DE"``, ...).  When
        supplied, state-specific header/footer patterns are also consulted.

    Returns
    -------
    None
        The text does not match any known artifact pattern — caller should
        treat it as a real content drop.
    dict
        ``{"category": <CAT_*>, "rule": <human label>,
           "confidence": "high"|"medium"|"low",
           "matched_pattern": <regex source>}``
    """
    if text is None:
        return None
    stripped = text.strip()
    if not stripped:
        return _hit(CAT_WHITESPACE, _RE_WHITESPACE.pattern, "high")

    # ── 0. normalise common artifact noise (tags + stray line numbers) ──
    # We try a series of progressively more aggressive normalisations and
    # return the first one that yields a hit.  This is intentionally
    # ordered from "least destructive" to "most destructive" so genuine
    # data (like the digits IN the bill number "Raised Bill No. 514")
    # are never sacrificed to a more permissive pattern when a tighter
    # one already matched.
    #
    #   variant 0 — original text
    #   variant 1 — tag wrappers stripped only
    #               rescues: "[A>Raised Bill No. 514<A]"
    #   variant 2 — variant 1 + leading 1-3 digit line number stripped
    #               rescues: "15 Raised Bill No. 6845" (CT line-number leak)
    #   variant 3 — variant 1 + trailing 1-3 digit line number stripped
    #               rescues: "Raised Bill No. 6845 15"
    #   variant 4 — variant 1 + both leading AND trailing stripped
    #               rescues: "15 Raised Bill No. 6845 16"  (rare)
    #
    # Variants 3 and 4 deliberately do NOT run on the unmodified text:
    # without the tag wrapper context, stripping trailing digits would
    # destroy legitimate bill numbers like "Raised Bill No. 514".
    candidates: list[str] = [text]
    tag_stripped = _strip_tag_wrappers(text)
    if tag_stripped != text and tag_stripped.strip():
        candidates.append(tag_stripped)
    lead_stripped = _RE_LEAD_LINE_DIGITS.sub("", tag_stripped, count=1).strip()
    if lead_stripped and lead_stripped not in candidates:
        candidates.append(lead_stripped)
    trail_stripped = _RE_TRAIL_LINE_DIGITS.sub("", tag_stripped, count=1).strip()
    if trail_stripped and trail_stripped not in candidates:
        candidates.append(trail_stripped)
    both_stripped = _RE_TRAIL_LINE_DIGITS.sub(
        "",
        _RE_LEAD_LINE_DIGITS.sub("", tag_stripped, count=1),
        count=1,
    ).strip()
    if both_stripped and both_stripped not in candidates:
        candidates.append(both_stripped)

    for cand in candidates:
        verdict = _classify_one(cand, state_code)
        if verdict:
            return verdict
    return None


def _classify_one(text: str, state_code: str | None) -> dict[str, Any] | None:
    """Run the pattern battery against a single candidate string."""
    # ── 1. universal patterns (state-independent) ─────────────────
    if _RE_PAGE_MARKER.match(text):
        return _hit(CAT_PAGE_MARKER, _RE_PAGE_MARKER.pattern, "high")
    if _RE_DECORATIVE.match(text):
        return _hit(CAT_DECORATIVE, _RE_DECORATIVE.pattern, "high")
    if _RE_TAG_FRAGMENT.match(text):
        return _hit(CAT_TAG_ARTIFACT, _RE_TAG_FRAGMENT.pattern, "high")
    if _RE_LINE_NUMBER_ALONE.match(text):
        # Standalone tiny number — most often a line number, but on some bills
        # also a page number.  Caller treats both the same way.
        return _hit(CAT_LINE_NUMBER, _RE_LINE_NUMBER_ALONE.pattern, "high")
    if _RE_LETTER_LINE_NUMBER.match(text):
        # Letter-prefixed tab/line marker (CT "T1", "T2", etc.)
        return _hit(CAT_LINE_NUMBER, _RE_LETTER_LINE_NUMBER.pattern, "high")
    if _RE_PAGE_NUMBER.match(text):
        return _hit(CAT_PAGE_NUMBER, _RE_PAGE_NUMBER.pattern, "medium")
    if _RE_DATE_STAMP.match(text):
        return _hit(CAT_DATE_STAMP, _RE_DATE_STAMP.pattern, "high")

    # ── 2. per-state header / footer patterns ─────────────────────
    # Check FOOTER patterns before HEADER patterns: footer regexes are
    # typically stricter ("LCO No. 3952  1 of 15") and would otherwise be
    # swallowed by looser header patterns ("LCO No. 3952").
    if state_code:
        state_patterns = STATE_PATTERNS.get(state_code.upper(), {})
        for category in (CAT_FOOTER, CAT_HEADER):
            for pat in state_patterns.get(category, []):
                if pat.search(text):
                    return _hit(category, pat.pattern, "high")

    # ── 3. PDF-metadata-based header/footer allowlist ─────────────
    # When actual header/footer text has been extracted from the PDF,
    # match against those exact strings.  This catches state-specific
    # headers/footers that don't have regex patterns defined.
    pdf_verdict = _classify_against_pdf_metadata(text)
    if pdf_verdict:
        return pdf_verdict

    return None


# Compiled once at import time — used by classify_removed_text.
_RE_TAG_OPEN  = re.compile(r"\[[AD]>")
_RE_TAG_CLOSE = re.compile(r"<[AD]\]")
_RE_LEAD_LINE_DIGITS  = re.compile(r"^\s*\d{1,3}\s+")
_RE_TRAIL_LINE_DIGITS = re.compile(r"\s+\d{1,3}\s*$")


def _strip_tag_wrappers(text: str) -> str:
    """Remove ``[A>``/``<A]``/``[D>``/``<D]`` tag markup from a string.

    Preserves all other content, including any digits or whitespace.
    Used by :func:`classify_removed_text` as the first normalisation
    step before retrying header/footer patterns.
    """
    out = _RE_TAG_OPEN.sub("", text)
    out = _RE_TAG_CLOSE.sub("", out)
    return out


def _normalise_for_classification(text: str) -> str:
    """Strip tag wrappers and any single leading/trailing line-number digit.

    .. deprecated::
        Retained for backward compatibility; new code should use
        :func:`classify_removed_text` directly, which now tries multiple
        progressively-aggressive normalisations internally.  This wrapper
        applies all of them at once and may over-strip in some cases
        (e.g. ``"Raised Bill No. 514"`` becomes ``"Raised Bill No."``).
    """
    out = _strip_tag_wrappers(text)
    out = _RE_LEAD_LINE_DIGITS.sub("", out, count=1)
    out = _RE_TRAIL_LINE_DIGITS.sub("", out, count=1)
    return out.strip()


# ─────────────────────────────────────────────────────────────────────
# Trailing line-number stripper
# ─────────────────────────────────────────────────────────────────────
# States known to print line numbers right-aligned at the end of every
# content line in their bill PDFs.  When ``state_code`` is in this set we
# always attempt the strip (still gated on sequence validation so it's
# safe).  For any other / missing state code we auto-detect: if the trailing
# numbers in the document genuinely form a monotonic sequence the strip
# fires, otherwise the text is returned unchanged.
TRAILING_LINE_NUMBER_STATES: set[str] = {"CT"}


def strip_trailing_line_numbers(
    text: str,
    state_code: str | None = None,
    *,
    min_candidates: int = 5,
    monotonic_ratio: float = 0.70,
    max_starting_number: int = 20,
) -> str:
    """Remove right-aligned line numbers that follow content on each line.

    Several state bill PDFs (Connecticut is the canonical example) print
    line numbers in a right-aligned margin column rather than the more
    common left margin.  PyMuPDF extracts them as ordinary trailing words
    on every body line::

        "Section 1. Subsection (d) of section 5-192f of the general statutes is 1"
        "repealed and the following is substituted in lieu thereof (Effective October 2"
        "1, 2025): 3"

    The LLM cleaning prompt correctly removes them, but the word-level
    accuracy diff then flags them as missing content — driving accuracy
    well below 100 % even though nothing real was lost.

    This function detects the pattern by checking that the trailing 1-3
    digit numbers across the document form a near-monotonic sequence and,
    only then, strips them.  Outside that pattern nothing is touched.

    Parameters
    ----------
    text
        Input text.  May still contain ``[A>…<A]`` / ``[D>…<D]`` tag
        markup — the stripper preserves it.
    state_code
        Optional two-letter state code.  States in
        :data:`TRAILING_LINE_NUMBER_STATES` always go through the
        detection; for other states the function still works in
        auto-detect mode (governed entirely by sequence validation).
    min_candidates
        Refuse to strip if fewer than this many lines end with a 1-3
        digit number.  Default 5.
    monotonic_ratio
        Require this fraction of consecutive candidate pairs to be
        strictly increasing.  Default 0.70.
    max_starting_number
        Refuse to strip if the first detected number is larger than
        this.  Line-number columns essentially always start at 1-5.

    Returns
    -------
    str
        Text with the trailing line numbers removed (line breaks and
        tag markup preserved).  Returns the input unchanged whenever
        the safety checks fail.
    """
    if not text:
        return text

    # Per-state opt-out hook: if a state ever needs to forbid this strip,
    # short-circuit here.  Today every state benefits from auto-detect so
    # we don't actually filter anything out.
    _state_upper = (state_code or "").upper()
    # (No exclusions yet — auto-detect is safe for every known state.)
    _ = _state_upper  # retained for future per-state policy switches

    lines = text.splitlines(keepends=True)

    candidates: list[tuple[int, int, str, str]] = []  # (idx, num, prefix, eol)
    for idx, line in enumerate(lines):
        m = _RE_TRAILING_LINE_NUMBER.match(line)
        if not m:
            continue
        prefix, num_s, eol = m.group(1), m.group(2), m.group(3) or ""
        # Reject if the prefix is too short to be body text — protects against
        # over-stripping in short list/table entries like "Sec. 2".
        if len(prefix.strip()) < 10:
            continue
        # Reject footer-style "X of Y" lines so we don't chop the page count.
        if _RE_PAGE_OF_TAIL.search(prefix):
            continue
        # Reject identifier tails (Bill No., LCO No., File No., Section, …):
        # the trailing number there is the bill / file / section number, not
        # a leaked body-line number.  Without this guard a header like
        # "General Assembly  Raised Bill No. 514" gets captured as candidate
        # 0 with num=514 and the >max_starting_number safety check then
        # aborts the entire strip pass, leaving real line numbers in place.
        if _RE_PREFIX_IDENTIFIER_TAIL.search(prefix):
            continue
        candidates.append((idx, int(num_s), prefix, eol))

    if len(candidates) < min_candidates:
        return text

    nums = [c[1] for c in candidates]
    if nums[0] > max_starting_number:
        return text

    pair_count = len(nums) - 1
    increasing = sum(1 for a, b in zip(nums, nums[1:]) if b > a)
    if pair_count == 0 or increasing / pair_count < monotonic_ratio:
        return text

    out_lines = list(lines)
    for idx, _num, prefix, eol in candidates:
        out_lines[idx] = prefix + eol
    return "".join(out_lines)


# ─────────────────────────────────────────────────────────────────────
# Leading line-number stripper (KS / NE / MO style)
# ─────────────────────────────────────────────────────────────────────

# Leading line number: 1-3 digits at start of line, followed by 1+ spaces,
# then actual content.  Group 1 = number, Group 2 = content, Group 3 = EOL.
_RE_LEADING_LINE_NUMBER = re.compile(
    r"^[ \t]*(\d{1,5})([ \t]+)(\S.+?)[ \t]*(\r?\n)?$"
)

# Bare leading line number: 1-4 digits on an otherwise empty/whitespace line.
# Group 1 = the number, Group 2 = optional trailing newline.
# These are orphaned line numbers on blank lines (e.g. "2   \n") that the
# content-bearing regex above cannot match because there is no \S content.
_RE_BARE_LEADING_LINE_NUMBER = re.compile(
    r"^[ \t]*(\d{1,5})[ \t]*(\r?\n)?$"
)


def strip_leading_line_numbers(
    text: str,
    state_code: str | None = None,
    *,
    min_candidates: int = 5,
    monotonic_ratio: float = 0.70,
    max_starting_number: int = 20,
) -> str:
    """Remove left-margin line numbers that precede content on each line.

    Kansas and several other states print line numbers in a left-margin
    column.  PyMuPDF sometimes extracts them as leading digits on content
    lines::

        "1 AN ACT concerning education; relating to the Kansas promise"
        "2 scholarship program; expanding the postsecondary educational"
        "3 institutions eligible to participate in such program."

    Detection uses the same monotonic-sequence validation as
    :func:`strip_trailing_line_numbers` — numbers must form a mostly
    increasing sequence across the document to be stripped.

    Bare line numbers on otherwise-empty lines (e.g. ``"2   \\n"``) are
    also removed once stripping is confirmed, provided their value falls
    within the range of the validated monotonic sequence.

    Parameters
    ----------
    text
        Input text (may contain tag markup).
    state_code
        Optional two-letter state code (reserved for future per-state
        policy).
    min_candidates
        Minimum lines with leading numbers before stripping activates.
    monotonic_ratio
        Required fraction of consecutive pairs that are strictly
        increasing.
    max_starting_number
        First detected number must be ≤ this value.

    Returns
    -------
    str
        Text with leading line numbers removed, or unchanged if safety
        checks fail.
    """
    if not text:
        return text

    lines = text.splitlines(keepends=True)

    # Phase 1: collect content-bearing candidates for sequence validation
    candidates: list[tuple[int, int, str, str, int]] = []  # (idx, num, content, eol, gap)
    for idx, line in enumerate(lines):
        m = _RE_LEADING_LINE_NUMBER.match(line)
        if not m:
            continue
        num_s, gap_ws, content, eol = m.group(1), m.group(2), m.group(3), m.group(4) or ""
        # Skip very short content — protects numbered list items like "1. "
        if len(content.strip()) < 5:
            continue
        candidates.append((idx, int(num_s), content, eol, len(gap_ws)))

    # Phase 1a: collect bare (orphaned) line numbers on blank lines.
    # These are lines like "2   \n" or "14  \n" with no content payload.
    bare_candidates: list[tuple[int, int, str]] = []  # (idx, num, eol)
    for idx, line in enumerate(lines):
        m_bare = _RE_BARE_LEADING_LINE_NUMBER.match(line)
        if m_bare:
            bare_candidates.append((idx, int(m_bare.group(1)), m_bare.group(2) or ""))

    if len(candidates) < min_candidates:
        # No content-bearing line numbers — fall through to bare-only mode
        # below.  This happens when pre-cleaning already stripped content
        # line numbers but left behind orphaned bare numbers.
        if not bare_candidates:
            return text
    else:
        nums = [c[1] for c in candidates]
        if nums[0] > max_starting_number:
            return text

        pair_count = len(nums) - 1
        increasing = sum(1 for a, b in zip(nums, nums[1:]) if b > a)
        if pair_count == 0 or increasing / pair_count < monotonic_ratio:
            return text

        # Sequence confirmed — strip content-bearing line numbers
        # Compute the typical gap between line number and content so we
        # can preserve excess indentation (e.g. table headers like
        # "22                  Number" → "                 Number").
        gaps = [g for _, _, _, _, g in candidates]
        gaps_sorted = sorted(gaps)
        _median_gap = gaps_sorted[len(gaps_sorted) // 2]

        out_lines = list(lines)
        for idx, _num, content, eol, gap in candidates:
            if gap > _median_gap + 4:
                # Excess indentation — preserve the difference
                indent = " " * (gap - _median_gap)
                out_lines[idx] = indent + content + eol
            else:
                out_lines[idx] = content + eol

        # Also strip bare line numbers within the confirmed range.
        max_num = max(nums)
        for idx, bare_num, eol in bare_candidates:
            if 1 <= bare_num <= max_num:
                out_lines[idx] = eol

        # ── Insert blank lines where line-number gaps indicate ───────
        # paragraph breaks.  E.g. line numbers 3→5 means line 4 was
        # blank in the PDF (paragraph break).  Build a combined sorted
        # sequence of (line_index, line_number) from both content and
        # bare candidates, then detect gaps.
        all_numbered: list[tuple[int, int]] = []
        for idx, num, _c, _e, _g in candidates:
            all_numbered.append((idx, num))
        for idx, num, _e in bare_candidates:
            if 1 <= num <= max_num:
                all_numbered.append((idx, num))
        all_numbered.sort(key=lambda x: x[0])

        # Walk the sequence looking for gaps within the same page
        # (line numbers reset at page boundaries, so only flag gaps
        # where the second number > first number and diff > 1).
        insert_after: set[int] = set()  # line indices after which to insert blank
        for j in range(len(all_numbered) - 1):
            idx_a, num_a = all_numbered[j]
            idx_b, num_b = all_numbered[j + 1]
            if num_b > num_a and num_b - num_a > 1:
                # Gap detected — insert blank line after idx_a
                insert_after.add(idx_a)

        if insert_after:
            final_lines: list[str] = []
            for idx, line in enumerate(out_lines):
                final_lines.append(line)
                if idx in insert_after:
                    final_lines.append("\n")
            out_lines = final_lines

        return "".join(out_lines)

    # ── Bare-only mode ──────────────────────────────────────────────
    # Pre-cleaning already stripped content-bearing line numbers but
    # left behind scattered bare numbers on blank lines (e.g. "2\n",
    # "4\n", "7\n").  Validate that these form a plausible line-number
    # sequence: values 1-36 (typical page-line range), repeating across
    # pages in a mostly-increasing pattern that resets near 1.
    bare_nums = [c[1] for c in bare_candidates]
    if not bare_nums or bare_nums[0] > max_starting_number:
        return text

    # Check that enough bare numbers exist (at least min_candidates)
    if len(bare_nums) < min_candidates:
        return text

    # All values must be in the typical line-number range (1-40)
    if max(bare_nums) > 40:
        return text

    # Check monotonic-with-resets pattern: each pair is either increasing
    # or a page-boundary reset (drop back to a small number ≤ max_starting_number).
    pair_count = len(bare_nums) - 1
    good_pairs = sum(
        1 for a, b in zip(bare_nums, bare_nums[1:])
        if b > a or b <= max_starting_number
    )
    if pair_count == 0 or good_pairs / pair_count < monotonic_ratio:
        return text

    # Confirmed — strip all bare line numbers
    out_lines = list(lines)
    for idx, _num, eol in bare_candidates:
        out_lines[idx] = eol
    return "".join(out_lines)


# ─────────────────────────────────────────────────────────────────────
# Bare line-number block stripper (MA / CT page-boundary style)
# ─────────────────────────────────────────────────────────────────────

_RE_BARE_NUM_LINE = re.compile(r"^\s*(\d{1,3})\s*$")


def strip_bare_line_number_blocks(
    text: str,
    state_code: str | None = None,
    *,
    min_consecutive: int = 3,
) -> str:
    """Remove blocks of bare line numbers (each on its own line).

    Massachusetts and some Connecticut extractions produce a block of
    sequential bare numbers at the end of each page::

        "curettage of a chalazion, removal and biopsy of skin lesions without"
        "known malignancy, laser capsulotomy, laser trabeculoplasty and laser"
        ""
        "1"
        "2"
        "3"
        ...
        "34"

    This function detects runs of ≥ *min_consecutive* bare-number lines
    that form a monotonic sequence and removes them.

    Parameters
    ----------
    text
        Input text.
    state_code
        Reserved for future per-state policy.
    min_consecutive
        Minimum length of a consecutive bare-number run to strip.
        Default 3.

    Returns
    -------
    str
        Text with bare number blocks removed.
    """
    if not text:
        return text

    lines = text.splitlines(keepends=True)
    remove_indices: set[int] = set()

    # Scan for runs of consecutive bare-number lines
    i = 0
    while i < len(lines):
        m = _RE_BARE_NUM_LINE.match(lines[i])
        if not m:
            i += 1
            continue
        # Start of a potential run
        run_start = i
        prev_num = int(m.group(1))
        i += 1
        while i < len(lines):
            m2 = _RE_BARE_NUM_LINE.match(lines[i])
            if not m2:
                break
            cur_num = int(m2.group(1))
            # Allow non-decreasing (some pages restart at 1)
            if cur_num <= prev_num and cur_num != 1:
                break
            prev_num = cur_num
            i += 1
        run_length = i - run_start
        if run_length >= min_consecutive:
            for idx in range(run_start, i):
                remove_indices.add(idx)

    if not remove_indices:
        return text

    out_lines = [line for idx, line in enumerate(lines) if idx not in remove_indices]
    return "".join(out_lines)


# ─────────────────────────────────────────────────────────────────────
# Wrapped-header merger
# ─────────────────────────────────────────────────────────────────────
def merge_wrapped_artifact_lines(
    text: str,
    state_code: str | None = None,
) -> str:
    """Fuse header/footer lines that PyMuPDF split across two text lines.

    Many state bills have headers long enough to wrap when PyMuPDF extracts
    them.  A typical Connecticut example::

        "Referred to Committee on EXECUTIVE AND LEGISLATIVE"   ← classifies as HEADER
        ""                                                      ← blank gap (optional)
        "NOMINATIONS"                                           ← lonely all-caps tail

    The line-level classifier in :func:`classify_removed_text` strips the
    first line but the lonely continuation falls through to the body word
    stream and shows up as a fake "missing" word.  This function detects
    those wrap pairs and joins them onto a single line so the classifier
    treats the whole header as one record.

    Detection is conservative — a wrap is only merged when **all** of the
    following hold:

    1. The current line classifies as HEADER or FOOTER.
    2. The next non-empty line matches
       :data:`_RE_HEADER_WRAP_CONTINUATION` (1-3 uppercase tokens, no
       digits, no terminating punctuation).
    3. The joined line *still* classifies in the same category.  This
       last guard prevents merging when the all-caps fragment is actually
       unrelated body text (e.g. ``"SECTION"`` on the line right after a
       header).

    The intervening blank lines are preserved in their original positions
    so per-line bookkeeping elsewhere in the pipeline is unaffected
    (whitespace-only lines are still classified as
    :data:`CAT_WHITESPACE`).

    Parameters
    ----------
    text
        Raw or cleaned bill text — may still contain ``[A>…<A]`` /
        ``[D>…<D]`` tag markup, which is preserved verbatim.
    state_code
        Two-letter state code passed through to
        :func:`classify_removed_text`.  Without it nothing is merged
        because no state-specific header pattern can match.

    Returns
    -------
    str
        Text with wrapped headers fused.  When no wraps are detected the
        input is returned unchanged.
    """
    if not text or "\n" not in text:
        return text

    lines = text.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]
        ln_stripped = ln.rstrip("\r\n")

        verdict = classify_removed_text(ln_stripped, state_code)
        if not verdict or verdict["category"] not in (CAT_HEADER, CAT_FOOTER):
            out.append(ln)
            i += 1
            continue

        # Look ahead past any blank lines for a possible wrap continuation.
        j = i + 1
        while j < n and not lines[j].strip():
            j += 1
        if j >= n:
            out.append(ln)
            i += 1
            continue

        cont = lines[j].rstrip("\r\n")
        if not _RE_HEADER_WRAP_CONTINUATION.match(cont):
            out.append(ln)
            i += 1
            continue

        joined = ln_stripped.rstrip() + " " + cont.strip()
        joined_verdict = classify_removed_text(joined, state_code)
        if not joined_verdict or joined_verdict["category"] != verdict["category"]:
            # Joining changed the classification — refuse to merge so we
            # don't accidentally absorb genuine body text.
            out.append(ln)
            i += 1
            continue

        # Wrap confirmed.  Replace `ln` with the joined version, keep any
        # blank lines between them as-is, and **drop** the continuation
        # row (its content is now folded into the joined header).
        eol = "\n" if ln.endswith("\n") or lines[j].endswith("\n") else ""
        out.append(joined + eol)
        out.extend(lines[i + 1:j])      # preserve any blank lines untouched
        # Skip the continuation line we just absorbed
        i = j + 1

    return "".join(out)


# ─────────────────────────────────────────────────────────────────────
# PyMuPDF table row marker stripper (``T1 ...``, ``T2 ...``, ...)
# ─────────────────────────────────────────────────────────────────────
def strip_table_row_markers(
    text: str,
    state_code: str | None = None,
    *,
    min_candidates: int = 3,
    max_starting_number: int = 5,
    monotonic_ratio: float = 0.70,
) -> str:
    """Remove PyMuPDF table-row prefixes like ``T1``, ``T2``, … ``T36``.

    When PyMuPDF extracts a tabular layout (e.g. tax-rate schedules in
    CT bills) it prefixes every cell line with a ``T<n>`` row marker::

        "T1 Electing Covered Employee's Rate of Payroll Tax"
        "T2 Payroll Expense"
        "T3 Over $50,000 but not over $100,000"
        ...

    The LLM cleaning step correctly drops the markers, but the word-level
    accuracy diff then flags every ``T<n>`` as a MISSING_IN_CONVERTED
    false positive — driving accuracy below 100 % even though no real
    content was lost.

    This stripper detects the pattern by validating that the ``T<n>``
    numbers form a near-monotonic sequence starting near 1, then removes
    only the marker (keeping the cell content intact).  Outside that
    pattern nothing is touched, so a body line that happens to start
    with ``T<n>`` (unlikely in legislative text) is safe.

    Parameters
    ----------
    text
        Input text.  Tag markup is preserved.
    state_code
        Two-letter state code — accepted for symmetry with the other
        strippers; auto-detect handles every state today.
    min_candidates
        Refuse to strip if fewer than this many lines start with the
        ``T<n>`` pattern.  Default 3.
    max_starting_number
        Refuse to strip if the first detected number is larger than
        this.  PyMuPDF table markers essentially always start at T1
        (occasionally T2 if the first row was a header that got
        promoted), so 5 is a generous safety bound.
    monotonic_ratio
        Require this fraction of consecutive candidate pairs to be
        strictly increasing.  Default 0.70.

    Returns
    -------
    str
        Text with the ``T<n>`` prefixes removed (everything else
        preserved).  Returns the input unchanged whenever the safety
        checks fail.
    """
    if not text:
        return text

    _ = state_code  # reserved for future per-state policy switches

    lines = text.splitlines(keepends=True)

    candidates: list[tuple[int, int, str, str, str]] = []
    # (idx, num, leading_ws, cell_content, eol)
    for idx, line in enumerate(lines):
        m = _RE_TABLE_ROW_MARKER.match(line)
        if not m:
            continue
        leading = m.group(1) or ""
        num_s = m.group(2)
        cell = (m.group(3) or "").rstrip()  # may be empty for bare markers
        eol = m.group(4) or ""
        # Reject lines whose cell content is suspiciously short *and*
        # non-empty (e.g. "T1 x") — those are unlikely to be real table
        # rows.  Bare markers (cell == "") are allowed because PyMuPDF
        # emits them when a cell payload wraps onto the next line.
        if cell and len(cell) < 2:
            continue
        candidates.append((idx, int(num_s), leading, cell, eol))

    if len(candidates) < min_candidates:
        return text

    nums = [c[1] for c in candidates]
    if nums[0] > max_starting_number:
        return text

    pair_count = len(nums) - 1
    if pair_count == 0:
        return text
    increasing = sum(1 for a, b in zip(nums, nums[1:]) if b > a)
    if increasing / pair_count < monotonic_ratio:
        return text

    out_lines = list(lines)
    for idx, _num, leading, cell, eol in candidates:
        if cell:
            # Variant 1: keep cell content, drop the ``T<n>`` prefix.
            out_lines[idx] = leading + cell + eol
        else:
            # Variant 2 (bare ``T<n>`` marker): drop the entire line
            # contents but preserve the line break so the surrounding
            # paragraph structure is unchanged.
            out_lines[idx] = eol
    return "".join(out_lines)


# ─────────────────────────────────────────────────────────────────────
# CT title-block normaliser (2-column header artifact)
# ─────────────────────────────────────────────────────────────────────
def normalise_ct_title_block(
    text: str,
    state_code: str | None = None,
) -> str:
    """Rewrite CT 2-column title-block artifact into canonical order.

    Connecticut bills print the page-1 title block in **two visual
    columns** but PyMuPDF reads them in row-major order, producing a
    single physical line that fuses two distinct title-block fields::

        # Proposed / Raised / Committee bills (3-line title block)
        "General Assembly Proposed Bill No. 1200"
        "General Assembly    Raised Bill No. 6851"

        # Post-committee Substitute bills (5-line title block)
        "General Assembly [A>File No. 629 <A]"

    The LLM cleaning step correctly reorders the words into the
    canonical CT title-block order::

        # 3-line block
        Proposed Bill No. 1200
        General Assembly
        January Session, 2025

        # 5-line block
        House of Representatives
        File No. 629
        General Assembly
        February Session, 2026
        Substitute House Bill No. 5422

    The word-level diff would otherwise see "General Assembly" out of
    position and either flag it as MISSING_IN_CONVERTED (caught by the
    ``repositioned`` rescue at best, real_diff at worst) or cost the bill
    2-3 pp of accuracy.

    This helper detects the artifact pattern and rewrites it into two
    lines in canonical order, so the downstream diff sees identical
    word streams on both sides.  Tag markup (``[A>...<A]``) is permitted
    and the identifier text is preserved verbatim in the rewrite.

    **CT-only**: when ``state_code`` is set to anything other than
    ``"CT"`` the input is returned unchanged.  This matches the API
    shape of the other artifact strippers in this module.

    Parameters
    ----------
    text
        Input text (typically tag-preserving raw PyMuPDF output, or the
        LLM-cleaned output — both are safe).
    state_code
        Two-letter state code.  Only ``"CT"`` activates the rewrite;
        all other values are no-ops.

    Returns
    -------
    str
        Text with title-block artifact lines rewritten into canonical
        order.  All other lines are returned unchanged.  Line endings
        (``\\n`` / ``\\r\\n``) are preserved.
    """
    if not text:
        return text
    if state_code is not None and state_code.upper() != "CT":
        return text

    out_lines: list[str] = []
    for raw_line in text.splitlines(keepends=True):
        # Split off the trailing EOL so the regex can use ``$`` cleanly,
        # then restore the original EOL after the rewrite.
        if raw_line.endswith("\r\n"):
            content, eol = raw_line[:-2], "\r\n"
        elif raw_line.endswith("\n"):
            content, eol = raw_line[:-1], "\n"
        else:
            content, eol = raw_line, ""

        m = _RE_CT_TITLE_BLOCK_BILL.match(content)
        if m:
            leading = m.group(1) or ""
            identifier = re.sub(r"\s+", " ", m.group(2)).strip()
            out_lines.append(f"{leading}{identifier}{eol}")
            out_lines.append(f"{leading}General Assembly{eol}")
            continue

        m = _RE_CT_TITLE_BLOCK_FILE.match(content)
        if m:
            leading = m.group(1) or ""
            identifier = re.sub(r"\s+", " ", m.group(2)).strip()
            out_lines.append(f"{leading}{identifier}{eol}")
            out_lines.append(f"{leading}General Assembly{eol}")
            continue

        out_lines.append(raw_line)

    return "".join(out_lines)


# ─────────────────────────────────────────────────────────────────────
# California inline "line N" wrap-marker
# ─────────────────────────────────────────────────────────────────────
# California's official LegInfo PDFs print body line numbers as the
# literal token ``line 2``, ``line 3``, ... at the START of each
# wrapped continuation line, e.g.
#
#     SECTION 1. The heading of Title 1.81.49 (commencing with
#      line 2 Section 1798.99.90) of Part 4 of Division 3 of the Civil Code is
#      line 3 amended to read:
#
# PyMuPDF extracts these markers verbatim into the raw text.  The LLM
# cleaning step correctly strips them.  When the text-accuracy
# comparison runs the raw side against the cleaned side, every single
# ``line N`` marker shows up as a missing-words diff — costing 10-15 pp
# of accuracy on a typical CA bill.
#
# This regex is intentionally anchored to line-start (with optional
# leading whitespace) so it CANNOT match legitimate sentence-internal
# uses such as DE bill 219's "AMEND ... by inserting the following
# after line 57:" — there "line 57" is preceded by the word "after"
# and the regex therefore won't fire.
_RE_CA_INLINE_LINE_MARKER = re.compile(
    r"^[ \t]*line[ \t]+\d{1,3}[ \t]+",
    re.MULTILINE | re.IGNORECASE,
)


def strip_ca_inline_line_numbers(
    text: str,
    state_code: str | None = None,
) -> str:
    """Strip CA's ``line N`` continuation markers from the start of wrapped lines.

    Connecticut bills put line numbers as bare digits at the END of
    each body line; California puts them as the literal token
    ``line N`` at the START of each WRAPPED continuation line.  PyMuPDF
    extracts them verbatim; the LLM cleaning step correctly removes
    them.  Without this pre-filter the text-accuracy comparison sees
    every ``line N`` token as a missing word on the raw side, costing
    10-15 pp of accuracy on every CA bill.

    The match is anchored to line-start (allowing only horizontal
    whitespace before ``line``) so legitimate sentence-internal uses
    like ``"after line 57"`` or ``"see line 3 below"`` are never
    touched.

    **CA-only**: when ``state_code`` is set to anything other than
    ``"CA"`` the input is returned unchanged.  This matches the
    API shape of :func:`normalise_ct_title_block` and the other
    artifact strippers in this module.

    Parameters
    ----------
    text
        Input text (typically the raw PyMuPDF output, but safe to
        apply to the cleaned text too — the regex is a no-op there
        because the LLM has already stripped the markers).
    state_code
        Two-letter state code.  Only ``"CA"`` activates the strip;
        all other values are no-ops.

    Returns
    -------
    str
        Text with all leading ``line N`` continuation markers removed.
        All other characters (including the line-break before each
        match) are preserved.
    """
    if not text:
        return text
    if state_code is not None and state_code.upper() != "CA":
        return text
    return _RE_CA_INLINE_LINE_MARKER.sub("", text)


# ─────────────────────────────────────────────────────────────────────
# California running page header / footer
# ─────────────────────────────────────────────────────────────────────
# Every printed page of a CA bill has a running header in one of two
# alternating forms (em-dash U+2014, but tolerate en-dash and ASCII
# hyphen for robustness):
#
#     — 2 — AB 45     (left form, even pages)
#     AB 45 — 3 —     (right form, odd pages)
#
# **Enacted bills** (already signed into law) use the chapter number
# instead of the bill identifier:
#
#     — 2 — Ch. 723   (left form, even pages)
#     Ch. 723 — 3 —   (right form, odd pages)
#
# PyMuPDF extracts these as standalone lines sandwiched between page
# breaks.  The LLM cleaning step correctly drops them, so they show
# up as ~6-12 false missing-words diffs per CA bill in the
# text-accuracy comparison.
#
# The regex requires:
#   • A CA bill identifier ``[A-Z]{1,4}\s*\d{1,5}`` (AB, SB, ACA, SCA,
#     ACR, SCR, HR, ACR, ...) OR the literal ``Ch.`` followed by the
#     chapter number for enacted bills.
#   • Sandwiched between dashes around a 1-4 digit page number
#   • Each variant must occupy the WHOLE LINE (anchored ^...$ with
#     re.MULTILINE) — so it can never match in-body uses of those
#     tokens.
#
# This pattern is so distinctive that false-positive risk is
# effectively zero.
_RE_CA_PAGE_HEADER = re.compile(
    r"^[ \t]*(?:"
    # Left form (bill):     — 2 — AB 45
    r"[\u2014\u2013\-][ \t]*\d{1,4}[ \t]*[\u2014\u2013\-][ \t]*[A-Z]{1,4}[ \t]*\d{1,5}"
    r"|"
    # Right form (bill):    AB 45 — 3 —
    r"[A-Z]{1,4}[ \t]*\d{1,5}[ \t]*[\u2014\u2013\-][ \t]*\d{1,4}[ \t]*[\u2014\u2013\-]"
    r"|"
    # Left form (chapter):  — 2 — Ch. 723   (regular enacted bills)
    #                  or   — 2 — Res. Ch. 177   (enacted resolutions)
    # Enacted CA bills use the chapter number in place of the bill
    # identifier.  Resolutions add a "Res. " prefix before "Ch.".
    r"[\u2014\u2013\-][ \t]*\d{1,4}[ \t]*[\u2014\u2013\-][ \t]*(?:Res\.[ \t]*)?Ch\.[ \t]*\d{1,5}"
    r"|"
    # Right form (chapter): Ch. 723 — 3 —
    #                  or   Res. Ch. 177 — 3 —
    r"(?:Res\.[ \t]*)?Ch\.[ \t]*\d{1,5}[ \t]*[\u2014\u2013\-][ \t]*\d{1,4}[ \t]*[\u2014\u2013\-]"
    r")[ \t]*$",
    re.MULTILINE,
)

# California "Authenticated Electronic Legal Material" certification
# stamp.  Every **enacted** CA bill (Chapter bill) carries a three-line
# certification block at the bottom of page 1, printed in small caps
# as a vertical block::
#
#     STATE OF CALIFORNIA
#     AUTHENTICATED
#     ELECTRONIC LEGAL MATERIAL
#
# PyMuPDF extracts these as three standalone lines.  The LLM cleaning
# step correctly drops them, so they appear as 3 false missing-words
# diffs against the cleaned text.
#
# The pattern requires each line to contain ONLY the exact stamp
# phrase (with optional surrounding whitespace).  Body text can never
# match — these phrases never appear as standalone single-line
# fragments in legislative prose.  CA-only.
_RE_CA_AUTHENTICATED_STAMP = re.compile(
    r"^[ \t]*(?:"
    r"STATE OF CALIFORNIA"
    r"|"
    r"AUTHENTICATED"
    r"|"
    r"ELECTRONIC LEGAL MATERIAL"
    r")[ \t]*$",
    re.MULTILINE,
)

# California's traditional "end-of-bill" mark — a standalone capital
# letter ``O`` on its own line.  This is a printer convention going
# back to typeset days where the oval ``O`` glyph signalled the end of
# the bill text.  It appears exactly ONCE per CA bill, near the very
# end, immediately before the final running page header sequence::
#
#     ... California Constitution.
#
#     O
#
#     96
#
#     AB 45 — 13 —
#
# The LLM cleaning step sometimes preserves and sometimes drops it,
# producing a false ``MISSING_IN_CONVERTED`` diff that costs 1 word of
# accuracy.  The regex requires the line to contain ONLY the letter
# ``O`` (with optional surrounding whitespace and an optional trailing
# period) — a shape that body text in a legislative bill cannot
# plausibly take.  CA-only.
_RE_CA_END_MARK = re.compile(
    r"^[ \t]*O\.?[ \t]*$",
    re.MULTILINE,
)


def strip_ca_page_headers(
    text: str,
    state_code: str | None = None,
) -> str:
    """Strip CA's running page header / footer lines.

    Every printed page of a California bill has a running header line
    in one of two alternating forms (using Unicode em-dash U+2014, but
    en-dash and ASCII hyphen are tolerated for robustness):

    * Left form (even pages):  ``— 2 — AB 45``
    * Right form (odd pages):  ``AB 45 — 3 —``

    PyMuPDF extracts these verbatim as standalone lines.  The LLM
    cleaning step correctly drops them, so without this pre-filter
    every such header appears as a false missing-words diff in the
    text-accuracy comparison.

    The pattern requires a CA bill identifier (e.g. ``AB 45``,
    ``SB 1217``, ``ACA 5``) sandwiched by dashes around a page number,
    AND the match must occupy the entire line.  This makes false
    positives effectively impossible — body text can never present in
    this exact shape on its own line.

    **CA-only**: returns input unchanged when ``state_code`` is not
    ``"CA"``.

    Parameters
    ----------
    text
        Input text (typically the raw PyMuPDF output).
    state_code
        Two-letter state code.  Only ``"CA"`` activates the strip.

    Returns
    -------
    str
        Text with every CA running page header removed (the line
        becomes empty; surrounding line breaks are preserved so word
        offsets stay stable).
    """
    if not text:
        return text
    if state_code is not None and state_code.upper() != "CA":
        return text
    # Strip running page headers/footers in both alternating forms
    # (covers both regular bills and enacted Chapter bills).
    text = _RE_CA_PAGE_HEADER.sub("", text)
    # Strip the "Authenticated Electronic Legal Material" certification
    # stamp printed on page 1 of every enacted CA bill (3 lines).  Each
    # line of the stamp is replaced with an empty string but the line
    # break is preserved so word offsets stay stable.
    text = _RE_CA_AUTHENTICATED_STAMP.sub("", text)
    # Strip the traditional CA end-of-bill "O" mark — but only when it
    # appears within the last 25% of the text, where the end-mark
    # convention places it.  This protects against a hypothetical body
    # line that just happens to be the letter ``O`` (extremely rare in
    # legislative text, but we guard against it for safety).
    if len(text) > 200:
        split_at = int(len(text) * 0.75)
        head, tail = text[:split_at], text[split_at:]
        tail = _RE_CA_END_MARK.sub("", tail)
        text = head + tail
    else:
        # Short bill — apply the strip to the whole text.
        text = _RE_CA_END_MARK.sub("", text)
    return text


# ──────────────────────────────────────────────────────────────────
# California Legislative Counsel's Digest caption wrap
# ──────────────────────────────────────────────────────────────────
# The first line of every CA Legislative Counsel's Digest is a caption
# of the form::
#
#     AB 45, as amended, Bauer-Kahan. Privacy: health data: location and
#     research.
#
# When the subject phrase is long enough, PyMuPDF soft-wraps it onto a
# second physical line.  The classifier in :func:`classify_removed_text`
# strips the first line (it matches the CA ``^\s*AB\s*\d+`` header
# rule), but the orphaned continuation falls through to the body word
# stream and shows up as a false ``MISSING_IN_CONVERTED`` diff against
# the cleaned text (where the LLM has joined the wrap onto one line and
# the whole caption is stripped as a unit).
#
# This regex matches a CA caption line that does NOT terminate with a
# sentence-ending punctuation mark (period, exclamation, question).  We
# only merge when the continuation is short (≤ 4 words) and itself ends
# with a sentence terminator — a conservative shape that body text in
# the digest virtually never satisfies.
_RE_CA_CAPTION_HEAD = re.compile(
    # Bill identifier (AB / SB / ACA / SCA / ACR / SCR / AJR / SJR /
    # ABX# / SBX# special-session variants) followed by number and the
    # signature ``, as amended,`` phrase.
    r"^\s*(?:AB|SB|ACA|SCA|ACR|SCR|AJR|SJR|ABX\d*|SBX\d*|HR|SR|HCR|HJR)\s*\d+\s*,\s*as\s+amended\b.*",
    re.IGNORECASE,
)

_CA_CAPTION_TERMINATORS = (".", "?", "!")
_CA_CAPTION_MAX_CONT_WORDS = 4   # ``research.`` = 1, ``and minors.`` = 2, etc.
_CA_CAPTION_MAX_MERGE_ITERATIONS = 3   # cap runaway merging defensively


def merge_ca_caption_wrap(
    text: str,
    state_code: str | None = None,
) -> str:
    """Fuse wrapped CA Legislative Counsel's Digest caption lines.

    California Legislative Counsel's Digest opens every bill with a
    caption such as::

        AB 45, as amended, Bauer-Kahan. Privacy: health data: location and
        research.

    When the subject phrase is long enough PyMuPDF soft-wraps it onto
    two lines.  The line-level classifier in
    :func:`classify_removed_text` strips the first line (it matches the
    CA ``^\\s*AB\\s*\\d+`` header rule) but the orphaned continuation
    falls through to the body word stream and shows up as a false
    ``MISSING_IN_CONVERTED`` diff against the cleaned text — because
    the LLM correctly joined the wrap onto one line in the cleaned
    output and the whole caption is stripped as one unit there.

    This helper joins the wrap pair back together so the classifier sees
    the complete caption as a single line on both sides.  CA-only.

    Detection is conservative — a wrap is only merged when **all** of
    the following hold:

    1. The line matches :data:`_RE_CA_CAPTION_HEAD` (starts with a CA
       bill identifier followed by ``, as amended,``).
    2. The line does NOT end with a sentence terminator
       (``.``, ``?``, ``!``).
    3. The next non-empty line is short (≤ 4 words) and DOES end with
       a sentence terminator.

    Up to 3 successive continuations are merged so unusually long
    captions that wrap onto three lines (rare but possible) are also
    handled.

    Parameters
    ----------
    text
        Input text (typically the raw PyMuPDF output, but safe to apply
        to the cleaned text too — the no-wrap precondition (rule 2)
        means cleaned text is a no-op).
    state_code
        Two-letter state code.  Only ``"CA"`` activates the merge; all
        other values are no-ops.

    Returns
    -------
    str
        Text with wrapped CA captions fused onto a single line.  When
        no wraps are detected the input is returned unchanged.
    """
    if not text or "\n" not in text:
        return text
    if state_code is not None and state_code.upper() != "CA":
        return text

    lines = text.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]
        stripped = ln.rstrip("\r\n").rstrip()

        if (
            stripped
            and _RE_CA_CAPTION_HEAD.match(stripped)
            and not stripped.endswith(_CA_CAPTION_TERMINATORS)
        ):
            merged_text = stripped
            tail_eol = "\n" if ln.endswith("\n") else ""
            blanks_to_preserve: list[str] = []
            consumed_through = i
            success = False
            for _ in range(_CA_CAPTION_MAX_MERGE_ITERATIONS):
                # Find next non-empty line
                j = consumed_through + 1
                while j < n and not lines[j].strip():
                    j += 1
                if j >= n:
                    break
                cont = lines[j].rstrip("\r\n").strip()
                # Continuation must be short and end with a sentence terminator
                if (
                    not cont
                    or len(cont.split()) > _CA_CAPTION_MAX_CONT_WORDS
                    or not cont.endswith(_CA_CAPTION_TERMINATORS)
                ):
                    break
                # Accumulate blank lines between current cursor and j
                blanks_to_preserve.extend(lines[consumed_through + 1: j])
                merged_text = merged_text + " " + cont
                if lines[j].endswith("\n"):
                    tail_eol = "\n"
                consumed_through = j
                success = True
                # If continuation ended with a terminator we're done; the
                # outer loop guard already prevents running indefinitely
                break

            if success:
                out.append(merged_text + tail_eol)
                out.extend(blanks_to_preserve)
                i = consumed_through + 1
                continue

        out.append(ln)
        i += 1

    return "".join(out)


# ──────────────────────────────────────────────────────────────────
# Indiana running page footer
# ──────────────────────────────────────────────────────────────────
# Every printed page of an Indiana bill PDF carries a single-line
# running footer at the very bottom of the page that identifies the
# bill plus its drafter codes::
#
#     2025 IN 1701—LS 7775/DI 92          (with year prefix, page 1)
#     IN 1701—LS 7775/DI 92               (without year, pages 2+)
#     SB 85—LS 6422/DI 104                (Senate Bill variant)
#     HJR 1—LS 1234/DI 567                (Joint Resolution variant)
#
# Decomposition:
#   • Optional 4-digit year prefix  (e.g. ``2025``)
#   • Bill identifier               (1–4 capital letters + bill number,
#                                    e.g. ``IN 1701`` or ``SB 85``)
#   • Em-dash / en-dash / hyphen    (U+2014, U+2013, ``-``)
#   • ``LS`` + numeric service code (e.g. ``LS 7775``)
#   • Slash separator               (``/``)
#   • ``DI`` + numeric drafter id   (e.g. ``DI 92``)
#
# PyMuPDF extracts the footer verbatim on every page; the LLM cleaning
# step correctly drops every occurrence.  Without this filter every
# footer surfaces as a false ``MISSING_IN_CONVERTED`` diff in the
# text-accuracy comparison (typically 1 per page → 2-15 per bill).
#
# Note: ``app/pymupdf_extractor.py`` already has a sibling regex
# (``_RE_IN_FOOTER``) that strips the *bare* form during the converted
# text post-processing pipeline, but it does NOT cover the year-prefixed
# variant AND it never touches the raw PyMuPDF text used by the
# accuracy comparator.  This helper closes both gaps for the accuracy
# pipeline by stripping the footer symmetrically from both sides.
#
# Safety: the literal token sequence ``LS \d+/DI \d+`` is highly
# specific to IN bill drafting metadata.  Combined with the bill
# identifier prefix AND the requirement that the whole match occupy
# the entire line, false positives in legislative body text are
# effectively impossible.  IN-only.
_RE_IN_PAGE_HEADER = re.compile(
    r"^[ \t]*"
    r"(?:\d{4}[ \t]+)?"                          # optional year prefix
    r"[A-Z]{1,4}[ \t]+\d{1,6}"                   # bill identifier (IN/HB/SB/HR/SR/HJR/SJR/HCR/SCR + number)
    r"[ \t]*[\u2014\u2013\-][ \t]*"              # em-dash / en-dash / hyphen
    r"LS[ \t]+\d{1,6}"                           # LS service code
    r"[ \t]*/[ \t]*"                             # slash separator
    r"DI[ \t]+\d{1,4}"                           # DI drafter id
    r"[ \t]*$",
    re.MULTILINE,
)


def strip_in_page_headers(
    text: str,
    state_code: str | None = None,
) -> str:
    """Strip Indiana's running page footer lines.

    Every printed page of an Indiana bill carries a single-line footer
    that identifies the bill plus its Legislative Service (LS) and
    Drafter Identifier (DI) codes, e.g.::

        2025 IN 1701—LS 7775/DI 92         (with year prefix)
        IN 1701—LS 7775/DI 92              (without year)
        SB 85—LS 6422/DI 104               (Senate Bill variant)

    PyMuPDF extracts these verbatim on every page; the LLM cleaning
    step correctly drops them.  Without this filter every footer
    surfaces as a false ``MISSING_IN_CONVERTED`` diff in the
    text-accuracy comparison.

    The pattern requires:

    * an optional 4-digit year prefix;
    * a 1–4 letter bill identifier (e.g. ``IN``, ``HB``, ``SB``,
      ``HJR``) followed by a bill number;
    * an em-dash / en-dash / hyphen;
    * the literal token ``LS`` plus a numeric service code;
    * a slash separator;
    * the literal token ``DI`` plus a numeric drafter id;
    * the whole match must occupy the entire line.

    Combined, these constraints make false positives in legislative
    body text effectively impossible.

    **IN-only**: returns input unchanged when ``state_code`` is not
    ``"IN"``.

    Parameters
    ----------
    text
        Input text (typically the raw PyMuPDF output).
    state_code
        Two-letter state code.  Only ``"IN"`` activates the strip.

    Returns
    -------
    str
        Text with every IN running page footer removed (the line
        becomes empty; surrounding line breaks are preserved so word
        offsets stay stable).
    """
    if not text:
        return text
    if state_code is not None and state_code.upper() != "IN":
        return text
    return _RE_IN_PAGE_HEADER.sub("", text)


# ─────────────────────────────────────────────────────────────────────
# Repeating artifact detection from raw PyMuPDF text
# ─────────────────────────────────────────────────────────────────────

_RE_PAGE_SPLIT = re.compile(r"---\s*Page\s+\d+\s*---")

# Number of lines from top/bottom of each page to scan for artifacts
_ARTIFACT_SCAN_LINES = 5
# Max words for a line to be considered a candidate artifact
_ARTIFACT_MAX_WORDS = 15


def _normalise_for_repeat(text: str) -> str:
    """Normalise text for fuzzy matching of repeating artifacts.

    Strips digits, punctuation, extra whitespace, and lowercases — so that
    variant footers like these collapse to the same key:
        "TLSB 1895YH (4) da/ns H.F. 1"   → "lsb yh dans hf"
        "LSB 1895YH (4) -1- da/ns 1/15 H.F. 1" → "lsb yh dans hf"

    Page 1 footers (TLSB vs LSB, no page number) and page 2+ footers
    (with -N- page counter and N/M fraction) share enough skeleton
    tokens to match once digits are removed.
    """
    # Strip tag wrappers
    t = re.sub(r"\[[AD]>|<[AD]\]", "", text)
    # Remove all digits
    t = re.sub(r"\d+", "", t)
    # Remove punctuation except letters and spaces
    t = re.sub(r"[^a-zA-Z\s]", "", t)
    # Collapse whitespace and lowercase
    t = " ".join(t.lower().split())
    # Normalise common prefix variants (TLSB→LSB, etc.)
    t = re.sub(r"\btlsb\b", "lsb", t)
    return t


def _detect_repeating_artifacts(raw_text: str) -> list[dict[str, Any]]:
    """Scan raw PyMuPDF text for short lines that repeat across pages.

    Splits by ``--- Page N ---`` markers, takes the top/bottom N lines
    of each page, and finds lines whose normalised form appears on ≥2
    pages.  These are almost certainly headers/footers.

    Returns a list of dicts with ``text`` (original), ``pages`` (list of
    page numbers), and ``position`` ("top" or "bottom").
    """
    pages = _RE_PAGE_SPLIT.split(raw_text)
    if len(pages) < 2:
        return []

    # Collect candidates: (normalised_key, original_text, page_idx, position)
    candidates: list[tuple[str, str, int, str]] = []

    for page_idx, page_text in enumerate(pages):
        lines = [ln for ln in page_text.splitlines() if ln.strip()]
        if not lines:
            continue

        # Top lines (headers)
        top_lines = lines[:_ARTIFACT_SCAN_LINES]
        # Bottom lines (footers)
        bottom_lines = lines[-_ARTIFACT_SCAN_LINES:] if len(lines) > _ARTIFACT_SCAN_LINES else []

        for ln in top_lines:
            stripped = ln.strip()
            if not stripped or len(stripped.split()) > _ARTIFACT_MAX_WORDS:
                continue
            key = _normalise_for_repeat(stripped)
            if key and len(key) >= 3:  # skip trivially short keys
                candidates.append((key, stripped, page_idx, "top"))

        for ln in bottom_lines:
            stripped = ln.strip()
            if not stripped or len(stripped.split()) > _ARTIFACT_MAX_WORDS:
                continue
            key = _normalise_for_repeat(stripped)
            if key and len(key) >= 3:
                candidates.append((key, stripped, page_idx, "bottom"))

    # Group by normalised key, count distinct pages
    from collections import defaultdict
    key_pages: dict[str, set[int]] = defaultdict(set)
    key_texts: dict[str, list[str]] = defaultdict(list)
    key_positions: dict[str, set[str]] = defaultdict(set)

    for key, text, page_idx, position in candidates:
        key_pages[key].add(page_idx)
        if text not in key_texts[key]:
            key_texts[key].append(text)
        key_positions[key].add(position)

    # Lines appearing on ≥2 pages = repeating artifacts
    artifacts: list[dict[str, Any]] = []
    seen_texts: set[str] = set()
    for key, page_set in key_pages.items():
        if len(page_set) >= 2:
            for text in key_texts[key]:
                if text not in seen_texts:
                    seen_texts.add(text)
                    artifacts.append({
                        "text": text,
                        "pages": sorted(page_set),
                        "positions": sorted(key_positions[key]),
                        "occurrences": len(page_set),
                    })

    return artifacts


def build_removals_report(
    raw_text: str,
    clean_text: str,
    state_code: str | None = None,
    *,
    max_text_chars: int = 200,
) -> dict[str, Any]:
    """Diff raw vs clean line-by-line and classify every removed line.

    Parameters
    ----------
    raw_text
        Tag-stripped raw PyMuPDF extraction.
    clean_text
        Tag-stripped LLM-cleaned output.
    state_code
        Two-letter state code for state-specific classification.
    max_text_chars
        Per-line text is truncated to this many characters in the report so
        JSON payloads stay small.

    Returns
    -------
    dict
        Structured report::

            {
              "state":          "CT",
              "raw_lines":      143,
              "clean_lines":     97,
              "removed_lines":   46,
              "added_lines":      0,
              "summary": {
                "by_category":      {"line_number": 32, "header": 8, ...},
                "classified":       40,
                "unclassified":      6,    # ← REAL content drops
                "classified_pct":   86.96,
              },
              "removals": [
                {"line": 4, "text": "...", "category": "page_marker",
                 "rule": "Page conversion markers", "confidence": "high"},
                ...
              ],
              "unclassified_removals": [
                # the suspicious ones — likely real content drops
                {"line": 73, "text": "..."},
                ...
              ],
              "additions": [
                # lines present in clean but not in raw (rare — hallucination check)
                {"line": 12, "text": "..."},
              ],
            }
    """
    raw_lines = raw_text.splitlines() if raw_text else []
    clean_lines = clean_text.splitlines() if clean_text else []

    sm = difflib.SequenceMatcher(None, raw_lines, clean_lines, autojunk=False)
    removed: list[tuple[int, str]] = []
    added: list[tuple[int, str]] = []
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

    by_category: dict[str, int] = {}
    removals: list[dict[str, Any]] = []
    unclassified: list[dict[str, Any]] = []
    for line_no, txt in removed:
        snippet = (txt[:max_text_chars] + "…") if len(txt) > max_text_chars else txt
        verdict = classify_removed_text(txt, state_code)
        if verdict:
            by_category[verdict["category"]] = by_category.get(verdict["category"], 0) + 1
            removals.append({
                "line":            line_no,
                "text":            snippet,
                "category":        verdict["category"],
                "rule":            verdict["rule"],
                "confidence":      verdict["confidence"],
                "matched_pattern": verdict["matched_pattern"],
            })
        else:
            unclassified.append({"line": line_no, "text": snippet})

    classified = len(removals)
    unclassified_n = len(unclassified)
    total = classified + unclassified_n
    classified_pct = (classified / total * 100.0) if total else 100.0

    additions_out = [
        {"line": ln, "text": (t[:max_text_chars] + "…") if len(t) > max_text_chars else t}
        for ln, t in added if t.strip()
    ]

    # ── Detect repeating header/footer artifacts from RAW text ──────────
    # Scan the PyMuPDF raw text (which always has the artifacts) to discover
    # repeating short lines across page boundaries.  This catches unknown
    # patterns that aren't in STATE_PATTERNS yet.
    #
    # Strategy:
    #   1. Split raw text by page separators (--- Page N ---)
    #   2. For each page, collect short lines (≤15 words) from the top/bottom
    #   3. Lines that repeat across ≥2 pages (exact OR fuzzy) = artifacts
    #   4. Check if those artifacts survived in the clean output → warning
    _detected_artifacts = _detect_repeating_artifacts(raw_text)

    # Flag additions that look like footers/headers/page numbers that should
    # have been removed.  Checks state-specific, generic, AND repeated patterns.
    _footer_warnings: list[dict[str, Any]] = []
    _state_pats = STATE_PATTERNS.get((state_code or "").upper(), {})
    _footer_pats = _state_pats.get(CAT_FOOTER, []) + _state_pats.get(CAT_HEADER, [])

    # Generic patterns that indicate artifacts in ANY state
    _generic_artifact_pats: list[tuple[re.Pattern, str]] = [
        (_RE_PAGE_NUMBER,        "page_number"),
        (_RE_PAGE_MARKER,        "page_marker"),
        (_RE_LINE_NUMBER_ALONE,  "line_number"),
        (_RE_DECORATIVE,         "decorative_rule"),
        (_RE_DATE_STAMP,         "date_stamp"),
    ]

    # Build a set of normalised artifact strings for fast lookup
    _artifact_normals = {_normalise_for_repeat(a["text"]) for a in _detected_artifacts}

    # ── Also scan full output for retained artifacts (not just additions) ──
    # Lines that exist in BOTH source and output won't appear in additions_out,
    # but if they match a detected repeating artifact, they should be flagged.
    for ln_idx, cline in enumerate(clean_lines, start=1):
        _ctxt = cline.strip()
        if not _ctxt or len(_ctxt) < 3:
            continue
        if _normalise_for_repeat(_ctxt) in _artifact_normals:
            _footer_warnings.append({
                "line": ln_idx,
                "text": _ctxt,
                "warning": "artifact_not_removed",
                "category": "detected_repeating",
            })

    for item in additions_out:
        _txt = item["text"].strip()
        if not _txt:
            continue
        _matched = False
        # Check state-specific patterns first
        for pat in _footer_pats:
            if pat.search(_txt):
                _footer_warnings.append({
                    "line": item["line"],
                    "text": item["text"],
                    "warning": "footer_not_removed",
                    "category": "header_footer",
                })
                _matched = True
                break
        if _matched:
            continue
        # Check generic artifact patterns
        for pat, cat in _generic_artifact_pats:
            if pat.match(_txt):
                _footer_warnings.append({
                    "line": item["line"],
                    "text": item["text"],
                    "warning": "artifact_not_removed",
                    "category": cat,
                })
                _matched = True
                break
        if _matched:
            continue
        # Check against repeating artifacts detected from raw text
        if _normalise_for_repeat(_txt) in _artifact_normals:
            _footer_warnings.append({
                "line": item["line"],
                "text": item["text"],
                "warning": "artifact_not_removed",
                "category": "detected_repeating",
            })

    return {
        "state":          (state_code or "").upper() or None,
        "raw_lines":      len(raw_lines),
        "clean_lines":    len(clean_lines),
        "removed_lines":  total,
        "added_lines":    len(additions_out),
        "footer_warnings": _footer_warnings,
        "detected_artifacts": _detected_artifacts,
        "summary": {
            "by_category":      by_category,
            "classified":       classified,
            "unclassified":     unclassified_n,
            "classified_pct":   round(classified_pct, 2),
        },
        "removals":              removals,
        "unclassified_removals": unclassified,
        "additions":             additions_out,
    }


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def _hit(category: str, pattern: str, confidence: str) -> dict[str, str]:
    """Build a hit dict — keeps classify_removed_text return shape uniform."""
    return {
        "category":        category,
        "rule":            RULE_LABELS.get(category, category),
        "confidence":      confidence,
        "matched_pattern": pattern,
    }


__all__ = [
    "classify_removed_text",
    "build_removals_report",
    "strip_trailing_line_numbers",
    "strip_leading_line_numbers",
    "strip_bare_line_number_blocks",
    "merge_wrapped_artifact_lines",
    "strip_table_row_markers",
    "normalise_ct_title_block",
    "strip_ca_inline_line_numbers",
    "strip_ca_page_headers",
    "merge_ca_caption_wrap",
    "strip_in_page_headers",
    "TRAILING_LINE_NUMBER_STATES",
    "STATE_PATTERNS",
    "RULE_LABELS",
    "set_pdf_metadata_allowlist",
    # category constants (re-exported for callers building UI badges)
    "CAT_PAGE_MARKER",
    "CAT_LINE_NUMBER",
    "CAT_HEADER",
    "CAT_FOOTER",
    "CAT_PAGE_NUMBER",
    "CAT_DATE_STAMP",
    "CAT_DECORATIVE",
    "CAT_WHITESPACE",
    "CAT_TAG_ARTIFACT",
]
