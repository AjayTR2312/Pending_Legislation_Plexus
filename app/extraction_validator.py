"""
Extraction-anomaly detector (production safety net)
====================================================

Scans cleaned PyMuPDF/LLM extraction output for textual patterns that
should *never* legally exist in a well-formed legislative bill text.

Every anomaly the validator returns represents a class of layout-extraction
failure that has happened in production at least once.  The validator is
intentionally **read-only** — it never mutates the text — so we can hook it
into the cleaning pipeline as a passive watchdog: it logs warnings, raises
metrics, and surfaces a banner in the Streamlit UI, but it never alters
output.  The cost of a false positive is therefore zero (the user sees an
extra info banner); the value of catching a true positive is high (we
discover layout bugs before users do).

Typical wiring::

    from extraction_validator import validate_extraction
    anomalies = validate_extraction(cleaned, state_code="DE",
                                    pdf_basename="bill.pdf")
    if anomalies:
        for a in anomalies:
            logger.warning("extraction_anomaly", extra=a.to_log_extra())

Each anomaly kind has:
  * ``kind``       — short snake_case identifier (used as a metric label)
  * ``pattern``    — compiled regex that detects the symptom
  * ``description``— human-readable explanation for triage / Streamlit
  * ``severity``   — "error" | "warning" | "info"

Adding a new anti-pattern is a 4-line change: append one ``_AntiPattern``
to ``_ANTI_PATTERNS`` and a unit test in ``tests/test_validator.py``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Pattern

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public data shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractionAnomaly:
    """One occurrence of a known extraction anti-pattern in the output text.

    Frozen so instances are hashable and safe to cache / dedupe.
    """

    kind: str                     # snake_case identifier, e.g. "trailing_dollar"
    severity: str                 # "error" | "warning" | "info"
    description: str              # human-readable summary
    snippet: str                  # the matched substring + small context
    char_offset: int              # 0-based char offset into the cleaned text
    page_number: Optional[int] = None
    state_code: Optional[str] = None
    pdf_basename: Optional[str] = None

    def to_log_extra(self) -> Dict[str, Any]:
        """Structured fields for ``logger.warning(..., extra=...)``."""
        return {
            "anomaly_kind": self.kind,
            "anomaly_severity": self.severity,
            "anomaly_snippet": self.snippet,
            "anomaly_offset": self.char_offset,
            "page_number": self.page_number,
            "state_code": self.state_code,
            "pdf_basename": self.pdf_basename,
        }


# ---------------------------------------------------------------------------
# Anti-pattern catalogue
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _AntiPattern:
    """Internal: one detection rule."""
    kind: str
    severity: str
    description: str
    pattern: Pattern[str]
    # Optional callable returning False to suppress a match as a known
    # benign case (e.g. trailing "$" inside a Markdown URL).  Receives the
    # full text and the regex Match object.
    is_false_positive: Optional[Any] = None


# --- helpers used by some patterns -----------------------------------------

def _looks_like_url_or_money_word(text: str, m: "re.Match[str]") -> bool:
    """Suppress matches where the "$" is part of a literal string like
    ``\\$`` (Markdown escape) or a programming context.  Legislative bills
    don't contain code, but keep the door open for false-positive damping
    if real benign hits surface.  Currently always returns False (no
    suppression) — left here so future tuning is a one-line change.
    """
    return False


# --- the catalogue ---------------------------------------------------------
#
# IMPORTANT: when adding a new pattern, prefer a regex that is *unlikely*
# to match valid legislative text.  Each false-positive class costs us a
# user-visible warning, so the bias is toward narrow / specific patterns
# even at the cost of missing rare variants.

_ANTI_PATTERNS: List[_AntiPattern] = [
    # ── 1. Trailing currency symbol (the bug we just fixed) ──────────
    # A digit run followed by spaces + "$" + EOL/tag-close.  Genuine US
    # bills always print "$<amount>", never "<amount> $".  This catches
    # any future regression in the same family.
    _AntiPattern(
        kind="trailing_dollar",
        severity="warning",
        description=(
            "Currency symbol appears AFTER the amount (e.g. '3,880,543 $'). "
            "Indicates PDF column spans were emitted in drawing order, not "
            "visual order. See _sort_block_lines_visually() in "
            "pymupdf_extractor.py."
        ),
        pattern=re.compile(r"[\d,]{2,}\s+\$\s*(?=$|[<\n])", re.MULTILINE),
    ),

    # ── 2. Number split on the thousands separator ───────────────────
    # E.g. "3 ,880" or "3\n,880" — PyMuPDF occasionally returns the
    # digit-run as two separate spans.  Real bills never put whitespace
    # between the thousands separator and the digits around it.
    _AntiPattern(
        kind="split_thousands_separator",
        severity="warning",
        description=(
            "Number split across the thousands separator (e.g. '3 ,880'). "
            "PDF span boundary fell inside a number; check the line/span "
            "merge logic for the affected page."
        ),
        pattern=re.compile(r"\b\d{1,3}\s+,\d{3}\b"),
    ),

    # ── 3. Number split on the decimal point ─────────────────────────
    # E.g. "12 .5" or "12\n.5".  Same root cause as #2.
    _AntiPattern(
        kind="split_decimal_point",
        severity="warning",
        description=(
            "Number split across the decimal point (e.g. '12 .5'). "
            "PDF span boundary fell inside a number."
        ),
        pattern=re.compile(r"\b\d+\s+\.\d+\b"),
    ),

    # ── 4. Empty addition / deletion tags ────────────────────────────
    # ``[A><A]`` or ``[D>   <D]`` — pure noise that should have been
    # stripped by ``_RE_EMPTY_ADD`` / ``_RE_EMPTY_DEL`` in the cleaner.
    _AntiPattern(
        kind="empty_add_tag",
        severity="warning",
        description=(
            "Empty [A>…<A] addition tag survived cleanup. "
            "_RE_EMPTY_ADD in pymupdf_extractor.py should have removed it."
        ),
        pattern=re.compile(r"\[A>\s*<A\]"),
    ),
    _AntiPattern(
        kind="empty_del_tag",
        severity="warning",
        description=(
            "Empty [D>…<D] deletion tag survived cleanup. "
            "_RE_EMPTY_DEL in pymupdf_extractor.py should have removed it."
        ),
        pattern=re.compile(r"\[D>\s*<D\]"),
    ),

    # ── 5. Unmerged adjacent same-type tags (no whitespace between) ──
    # ``<A][A>`` or ``<D][D>`` directly touching means the consolidator
    # missed a merge opportunity.  Adjacent A/D mix (``<A][D>``) is fine.
    _AntiPattern(
        kind="unmerged_adjacent_add_tags",
        severity="info",
        description=(
            "Two [A>…<A] tags meet with no whitespace between them. "
            "consolidate_adjacent_tags() should have merged them."
        ),
        pattern=re.compile(r"<A\]\[A>"),
    ),
    _AntiPattern(
        kind="unmerged_adjacent_del_tags",
        severity="info",
        description=(
            "Two [D>…<D] tags meet with no whitespace between them. "
            "consolidate_adjacent_tags() should have merged them."
        ),
        pattern=re.compile(r"<D\]\[D>"),
    ),

    # ── 6. Failed dehyphenation across line break ─────────────────────
    # E.g. "appropri-\nated" or "appropri- ated" survives in the output.
    # The cleaner is expected to rejoin word-hyphen-newline-word.  We
    # require at least 3 letters on each side of the hyphen to avoid
    # matching legitimate compound words like "high-speed".
    _AntiPattern(
        kind="failed_dehyphenation",
        severity="warning",
        description=(
            "Word appears split with hyphen + newline (e.g. 'appropri-\\nated'). "
            "Dehyphenation step missed this token."
        ),
        pattern=re.compile(r"[A-Za-z]{3,}-\s*\n\s*[a-z]{2,}"),
    ),

    # ── 7. Section header split from its number ──────────────────────
    # ``Sec.\n123.`` or ``Sec.   \n   123.`` — the cleaner is supposed
    # to rejoin these (see ``_orphan_sec_a`` in consolidate_adjacent_tags).
    # If we still see them, that rejoin is failing for some new variant.
    _AntiPattern(
        kind="split_section_header",
        severity="warning",
        description=(
            "Section header split from its number (e.g. 'Sec.\\n  123.'). "
            "Phase 0 of consolidate_adjacent_tags should have rejoined them."
        ),
        pattern=re.compile(r"\bSec\s*\.\s*\n\s*\d"),
    ),

    # ── 8. Orphaned tag delimiters ───────────────────────────────────
    # A tag opener with no closer anywhere on the rest of the line, or
    # vice versa.  We only flag the case where the orphan sits on a line
    # by itself — that's the strongest signal that a tag boundary got
    # mangled during cleanup (the normal mid-text case is too noisy to
    # detect with a simple regex).
    _AntiPattern(
        kind="orphan_tag_opener",
        severity="info",
        description=(
            "A tag opener ([A> or [D>) appears at end of line with no closer "
            "on the same line."
        ),
        pattern=re.compile(r"\[[AD]>\s*$", re.MULTILINE),
    ),
    _AntiPattern(
        kind="orphan_tag_closer",
        severity="info",
        description=(
            "A tag closer (<A] or <D]) appears at start of line with no "
            "opener on the same line."
        ),
        pattern=re.compile(r"^\s*<[AD]\]", re.MULTILINE),
    ),

    # ── 9. DIVISION header missing blank-line separator ──────────────
    # "DIVISION X\nSUBTITLE" with no blank line between them means the
    # deterministic fix in `deterministic_cleanup` (Fix 9) missed a case.
    _AntiPattern(
        kind="division_missing_blank_line",
        severity="warning",
        description=(
            "DIVISION header immediately followed by subtitle with no "
            "blank line separator. Fix 9 in deterministic_cleanup should "
            "have inserted one."
        ),
        pattern=re.compile(
            r"^DIVISION\s+[IVXLCDM]+\n(?!\n)[A-Z][A-Z]",
            re.MULTILINE,
        ),
    ),

    # ── 10. Duplicate paragraph run (chunk merge failure) ────────────
    # Two identical paragraphs (50+ chars) appearing back-to-back signals
    # the chunk stitcher failed to deduplicate overlap.  We detect a
    # repeated sentence-length substring separated by 0-2 blank lines.
    _AntiPattern(
        kind="duplicate_paragraph_run",
        severity="error",
        description=(
            "A paragraph of 50+ characters appears duplicated back-to-back. "
            "Indicates a chunk-merge overlap deduplication failure in "
            "_stitch_two_chunks()."
        ),
        pattern=re.compile(
            r"(\n[^\n]{50,})\s*\n{1,3}\1",
        ),
    ),

    # ── 11. Signature line merged with Approved date ─────────────────
    # "Approved ___, YYYY ______" on same line means Fix 10 missed it.
    _AntiPattern(
        kind="signature_line_merged",
        severity="info",
        description=(
            "Governor signature line merged on same line as 'Approved' date. "
            "Fix 10 in deterministic_cleanup should have split them."
        ),
        pattern=re.compile(
            r"^Approved\s+_{3,},\s*\d{4}[^\S\n]+_{10,}\s*$",
            re.MULTILINE,
        ),
    ),
]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

_CONTEXT_CHARS = 40   # chars of context on each side of the match


def validate_extraction(
    text: str,
    *,
    state_code: Optional[str] = None,
    pdf_basename: Optional[str] = None,
    max_per_kind: int = 50,
) -> List[ExtractionAnomaly]:
    """Scan *text* for known extraction anti-patterns.

    Parameters
    ----------
    text :
        Cleaned extraction output (post-LLM clean, post-consolidate).
    state_code :
        Two-letter state code (e.g. ``"DE"``) for log routing / filtering.
    pdf_basename :
        Source filename for log routing / filtering.
    max_per_kind :
        Cap on anomalies per pattern to keep logs manageable when a
        catastrophic regression produces thousands of hits.  Pages can
        still be re-examined manually; the cap only limits the log
        firehose.

    Returns
    -------
    list[ExtractionAnomaly]
        Empty when the text is clean.  Order is stable: anomalies appear
        sorted by ``char_offset`` so callers can correlate with the
        source text and a paginated viewer.
    """
    if not text:
        return []

    out: List[ExtractionAnomaly] = []

    for ap in _ANTI_PATTERNS:
        hits = 0
        for m in ap.pattern.finditer(text):
            if ap.is_false_positive and ap.is_false_positive(text, m):
                continue
            start = max(0, m.start() - _CONTEXT_CHARS)
            end = min(len(text), m.end() + _CONTEXT_CHARS)
            snippet = text[start:end].replace("\n", "\\n")
            page_number = _approximate_page_number(text, m.start())
            out.append(
                ExtractionAnomaly(
                    kind=ap.kind,
                    severity=ap.severity,
                    description=ap.description,
                    snippet=snippet,
                    char_offset=m.start(),
                    page_number=page_number,
                    state_code=state_code,
                    pdf_basename=pdf_basename,
                )
            )
            hits += 1
            if hits >= max_per_kind:
                # Add one synthetic marker so downstream observers know
                # the cap was hit and the true count is higher.
                out.append(
                    ExtractionAnomaly(
                        kind=ap.kind + "_TRUNCATED",
                        severity="info",
                        description=(
                            f"More than {max_per_kind} '{ap.kind}' matches "
                            "found; output truncated."
                        ),
                        snippet="(truncated)",
                        char_offset=m.start(),
                        state_code=state_code,
                        pdf_basename=pdf_basename,
                    )
                )
                break

    out.sort(key=lambda a: a.char_offset)
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Pattern for the page-separator marker emitted by extract_pdf_with_pymupdf
# when join_pages=True with the default separator "--- Page {n} ---".
_RE_PAGE_MARKER = re.compile(r"---\s*Page\s+(\d+)\s*---")


def _approximate_page_number(text: str, offset: int) -> Optional[int]:
    """Return the page number that contains *offset*, or ``None`` if the
    text has no ``--- Page N ---`` separators (i.e. it was joined with a
    custom separator or the LLM clean step stripped the markers).

    We walk backwards from *offset* looking for the nearest marker; this
    is O(n) per call but the caller invokes it at most a few hundred
    times per document, so the cost is negligible.
    """
    # Search the slice [0:offset] from the right for the last page marker.
    region = text[:offset]
    last = None
    for m in _RE_PAGE_MARKER.finditer(region):
        last = m
    if last is None:
        return None
    try:
        return int(last.group(1))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Convenience: log a batch of anomalies in one call
# ---------------------------------------------------------------------------


def log_anomalies(
    anomalies: List[ExtractionAnomaly],
    *,
    log: Optional[logging.Logger] = None,
) -> None:
    """Emit a ``logger.warning`` per anomaly with structured ``extra``.

    Centralised so log shippers (CloudWatch, Datadog, etc.) see a
    consistent shape and a single message format to grep for.
    """
    if not anomalies:
        return
    _log = log or logger
    for a in anomalies:
        _log.warning(
            "extraction_anomaly kind=%s page=%s snippet=%r",
            a.kind, a.page_number, a.snippet,
            extra=a.to_log_extra(),
        )
