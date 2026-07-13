"""
pymupdf_compare.py  —  PyMuPDF Clean-Text Audit via Claude Opus 4.7
====================================================================

Validates the LLM-cleaned PyMuPDF text against two ground-truth sources:

  GROUND TRUTH (1):  Highlighted PDF page images
                     — visual evidence of underline / strikethrough / italic / bold
  GROUND TRUTH (2):  PyMuPDF JSON report (`formatted_spans`)
                     — deterministic tag list extracted from PDF metadata

  VALIDATION TARGET: PyMuPDF clean text (after LLM cleaning)
                     — the *_pymupdf_clean.txt file

Two-phase validation, mirroring gpt_compare.py:

  Phase 1 — CHECKLIST WALK
    Every span in pymupdf_report["formatted_spans"] becomes a checklist
    item.  For each item we ask:
      • Is the formatting REAL in the image? (PyMuPDF may produce false
        positives — table borders, page-separator rules, etc.)
      • Did the LLM PRESERVE the tag in the cleaned text? (Or did
        cleaning drop / shift / mis-type it?)

  Phase 2 — OPEN-ENDED AUDIT
    Claude Opus 4.7 scans images vs cleaned text for anything else:
      • Tags in cleaned text with NO matching report span (hallucinated)
      • Visible formatting in images that PyMuPDF MISSED
      • Content the LLM dropped or invented during cleaning

Output: styled Excel workbook with three sheets
        (PyMuPDF Span Audit, Additional Findings, Summary).

Usage:
    from pymupdf_compare import pymupdf_compare_tags
    result = pymupdf_compare_tags(
        highlighted_images_b64=hi_imgs,
        pymupdf_report=report,
        pymupdf_clean_text=clean_txt,
        state_code="AR",
        output_xlsx="pymupdf_audit.xlsx",
    )
"""

from __future__ import annotations

import json
import logging
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

# Reuse helpers from gpt_compare so we have ONE implementation of the
# trickier text-matching, JSON-repair, and Excel-styling logic.
from gpt_compare import (
    _HEADER_FILL,
    _HEADER_FONT,
    _INFO_FILL,
    _MISS_FILL,
    _OK_FILL,
    _TAG_RE,
    _WARN_FILL,
    _WRAP,
    _get_anthropic_client,
    _norm,
    _norm_loose,
    _repair_json,
    _style_header,
    _style_row,
    _token_similarity,
    _word_diff_fix,
)

# Optional import of protected-clauses module from the sibling ``app/``
# package.  When present, items whose text overlaps a state-specific
# protected clause (e.g. "Be it enacted by the Legislature of the State
# of Kansas:") get classified as ``protected_clause`` — the LLM is
# *supposed* to drop the tag for those, so dropping it is ✅ pass, not
# ❌ tag_dropped.
try:
    import sys as _sys
    from pathlib import Path as _Path
    _app_dir = str(_Path(__file__).resolve().parent.parent / "app")
    if _app_dir not in _sys.path:
        _sys.path.insert(0, _app_dir)
    from protected_clauses import PROTECTED_CLAUSES as _PROTECTED_CLAUSES
except Exception:                                           # pragma: no cover
    _PROTECTED_CLAUSES: Dict[str, List[str]] = {}

log = logging.getLogger("pymupdf_compare")


def _protected_clauses_for(state_code: Optional[str]) -> List[str]:
    """Return the list of protected-clause phrases for *state_code* (or [])."""
    if not state_code:
        return []
    return list(_PROTECTED_CLAUSES.get(state_code.upper(), []))


def _is_protected_text(text: str, protected: List[str]) -> bool:
    """True when *text* is (mostly) a protected clause.

    We compare a normalised, casefolded form so trailing punctuation /
    spacing / case differences are ignored.  The check is bidirectional:
    item text may BE a clause, CONTAIN a clause, or be CONTAINED by one
    (PyMuPDF often emits the clause as several adjacent fragments).

    Word-boundary safeguard
    -----------------------
    Substring matches (``norm_c in norm_t``) must be **word-bounded** so a
    short clause like ``"Resolved,"`` (CA) cannot match the middle of an
    unrelated word like ``"time-resolved"`` in an energy bill, or
    ``"Effective:"`` (IN) cannot match inside ``"ineffective"``.  Word
    boundary in this context means: the character on each side of the
    match (if present) must NOT be a letter, digit, hyphen, or
    underscore — i.e. the match must align with whole-word edges in
    the bill text.
    """
    if not text or not protected:
        return False
    norm_t = _norm(text).lower().rstrip(".,:;")
    if not norm_t:
        return False

    def _is_word_char(ch: str) -> bool:
        # Treat hyphen as a word character so ``time-resolved`` is one
        # token from the perspective of this check, preventing the bug
        # where ``resolved`` would otherwise match its tail.
        return bool(ch) and (ch.isalnum() or ch in ("-", "_"))

    def _word_bounded_in(needle: str, haystack: str) -> bool:
        """``needle in haystack`` but require whole-word alignment."""
        if not needle or needle not in haystack:
            return False
        # Scan every occurrence; accept if ANY one has clean word edges.
        start = 0
        nlen = len(needle)
        hlen = len(haystack)
        while True:
            idx = haystack.find(needle, start)
            if idx < 0:
                return False
            left_ok = idx == 0 or not _is_word_char(haystack[idx - 1])
            right_ok = (idx + nlen >= hlen
                        or not _is_word_char(haystack[idx + nlen]))
            if left_ok and right_ok:
                return True
            start = idx + 1

    for clause in protected:
        norm_c = _norm(clause).lower().rstrip(".,:;")
        if not norm_c:
            continue
        if norm_t == norm_c:
            return True
        # Fragment of the clause (≥3 chars to avoid false hits on "by"),
        # word-bounded on both sides so partial matches inside larger
        # tokens (e.g. ``"resolved"`` inside ``"time-resolved"``) are
        # rejected.
        if len(norm_t) >= 3 and _word_bounded_in(norm_t, norm_c):
            return True
        if len(norm_c) >= 3 and _word_bounded_in(norm_c, norm_t):
            return True
    return False


# ════════════════════════════════════════════════════════════════════
# Indiana decorative-header detection — expected-tag-drop classifier
# ════════════════════════════════════════════════════════════════════
# The IN clean-prompt instructs the LLM to STRIP [A>...<A] tags from
# decorative header/structural elements (bill title, version label,
# digest heading, section labels, committee name, etc.) because the
# bold/underlined styling in IN PDFs is decorative typography — NOT
# legislative markup.  When the audit sees an item where the text
# survived but the tag was dropped, and that text matches one of the
# enumerated IN header patterns, this is the EXPECTED behavior per
# the prompt — NOT a ``tag_dropped`` error.
#
# See ``data/Prompts/IN_Clean_Prompt.txt`` (lines 95–117) for the
# canonical enumeration of patterns where the LLM is told to:
#     "REMOVE the [A>...<A] tags but KEEP the text"

# Hard-matched IN decorative-header patterns.  Matches the WHOLE inner
# text (no tag wrappers); anchored with ^ ... $ on the trimmed text.
_RE_IN_DECORATIVE_HEADER = re.compile(
    r"^(?:"
    # 1. Decorative horizontal rule (3+ underscores / em-dashes / en-dashes / hyphens)
    r"[_\u2014\u2013\-][_\u2014\u2013\-\s]{2,}"
    r"|"
    # 2. Bill number with optional prefix
    r"(?:ENGROSSED\s+|ENROLLED\s+|INTRODUCED\s+|REPRINTED\s+|SUBSTITUTE\s+|AMENDED\s+)?"
    r"(?:HOUSE|SENATE)\s+"
    r"(?:BILL|RESOLUTION|JOINT\s+RESOLUTION|CONCURRENT\s+RESOLUTION)"
    r"\s+No\.?\s*\d+"
    r"|"
    # 3. Digest heading variants (DIGEST OF INTRODUCED BILL / DIGEST OF HB 1234)
    r"DIGEST\s+OF\s+(?:[A-Z][A-Z\s]*BILL|(?:HB|SB|HR|SR|HJR|SJR|HCR|SCR)\s+\d+)"
    r"|"
    # 4. "A BILL FOR AN ACT" title (with optional subject continuation)
    r"A\s+BILL\s+FOR\s+AN\s+ACT(?:\s+concerning\b.*)?"
    r"|"
    # 5. Session lines (First/Second/Third/Fourth Regular/Special Session ...)
    r"(?:First|Second|Third|Fourth)\s+(?:Regular|Special)\s+Session"
    r"(?:\s+of\s+the\s+\d+\w*\s+General\s+Assembly(?:\s*\(\d{4}\))?)?"
    r"|"
    # 6. Version / engrossment labels
    r"(?:Introduced|Enrolled|Reprinted|Engrossed)\s+(?:Version|Act)"
    r"|"
    # 7. Standalone "Introduced" label
    r"Introduced"
    r"|"
    # 8. Enactment clause
    r"Be\s+it\s+enacted\s+by\s+the\s+General\s+Assembly\s+of\s+the\s+State\s+of\s+Indiana:?"
    r"|"
    # 9. Section labels (with or without trailing colon; bare label only)
    r"(?:Citations\s+Affected|Synopsis|Effective|Author|Sponsor|Co-?Author|Co-?Sponsor):?"
    r"|"
    # 10. PRINTING CODE section heading
    r"PRINTING\s+CODE"
    r")\s*\.?$",
    re.IGNORECASE,
)

# Regex to extract committee names from body text — "Committee on X".
# Used by :func:`_committee_headers_from_clean_text` to enumerate the
# committee names that appear in a bill so the classifier can recognise
# short title-cased standalone lines as committee-name headers (which
# the IN prompt enumerates among "strip the tag, keep the text"
# elements like "Rules and Legislative Procedures").
_RE_IN_COMMITTEE_REF = re.compile(
    r"Committee\s+on\s+([A-Z][A-Za-z\s,&'\-]+?)"
    # Stop at: end of sentence/punctuation, end of line, or next
    # subordinating word (referral chain: "Committee on A, then to B" /
    # "Committee on A and to B" / "Committee on A; thence to B").
    r"(?=\.|;|,\s+(?:then|to|and|which|that|of\s+(?:the|which))\b|$|\n)",
    re.MULTILINE,
)


def _committee_headers_from_clean_text(cleaned_text: str) -> set:
    """Extract the set of committee names referenced in *cleaned_text*.

    Looks for the canonical IN body phrase ``Committee on <Name>`` and
    returns the normalised set of committee names found (whitespace
    collapsed, trailing punctuation stripped, casefolded).  Used to
    cross-validate short title-cased standalone lines as committee-name
    headers — the IN clean-prompt instructs the LLM to strip ``[A>...<A]``
    tags from these.

    The cross-validation is safe: a bold-styled standalone line is
    treated as an expected tag drop ONLY when the same text also
    appears later in the body after ``Committee on``.
    """
    if not cleaned_text:
        return set()
    out: set = set()
    for m in _RE_IN_COMMITTEE_REF.finditer(cleaned_text):
        name = re.sub(r"\s+", " ", m.group(1)).strip(" .,;:—–-").lower()
        # Trim trailing connectors that might have leaked in
        for tail in (" and", " to", " which", " that"):
            if name.endswith(tail):
                name = name[: -len(tail)].strip()
        # Accept single-word committee names like "Insurance", "Judiciary",
        # "Appropriations", "Education" \u2014 these are common in real IN bills.
        # Upper bound prevents runaway captures.
        if 1 <= len(name.split()) <= 12:
            out.add(name)
    return out


def _is_in_decorative_header(
    text: str,
    committee_names: Optional[set] = None,
) -> bool:
    """True when *text* matches an Indiana decorative-header pattern.

    Per the IN clean-prompt (``data/Prompts/IN_Clean_Prompt.txt``), the
    LLM is instructed to strip ``[A>...<A]`` tags from
    header/structural elements at the top of pages — these are styled
    with decorative underlines that PyMuPDF misreads as legislative
    markup.  When the audit sees a ``text_only`` (text preserved, tag
    dropped) item matching one of these patterns, that is the EXPECTED
    behavior per the prompt — NOT a ``tag_dropped`` error.

    Two-layer detection:

    1. **Hard-match regex** (:data:`_RE_IN_DECORATIVE_HEADER`) covers
       the patterns enumerated in the IN prompt:
         * Decorative horizontal rules (``_____``, em-dash runs)
         * Bill number lines (``HOUSE BILL No. XXXX``)
         * Version labels (``Introduced Version``, ``Enrolled Act``)
         * Digest headings (``DIGEST OF INTRODUCED BILL``)
         * Title (``A BILL FOR AN ACT``)
         * Session lines (``First Regular Session of the 124th …``)
         * Enactment clause (``Be it enacted by the General Assembly
           of the State of Indiana:``)
         * Section labels (``Citations Affected:``, ``Synopsis:``)
         * ``PRINTING CODE`` section heading
         * Standalone ``Introduced`` label

    2. **Committee-name cross-validation**: when *committee_names* is
       supplied (the set returned by
       :func:`_committee_headers_from_clean_text`), short title-cased
       text matching one of those committee names is also recognised
       as a decorative header (catches IN PDF committee-of-referral
       lines like ``Rules and Legislative Procedures``).

    Parameters
    ----------
    text
        The inner text of the checklist item (PyMuPDF span, no tag
        wrappers).
    committee_names
        Optional pre-computed set of body committee references for
        cross-validation.  When omitted, only the hard-match regex is
        used.

    Returns
    -------
    bool
        True when the text matches a known IN decorative-header
        pattern and the LLM is correctly expected to have dropped the
        ``[A>...<A]`` tag while preserving the inner text.
    """
    if not text:
        return False
    s = text.strip().rstrip(".,;:")
    if not s:
        return False
    # Layer 1: hard-match the canonical enumerated patterns
    if _RE_IN_DECORATIVE_HEADER.match(s):
        return True
    # Layer 2: committee-name cross-validation
    if committee_names:
        # Conservative shape guard: short text, no internal punctuation,
        # starts with a capital letter.
        words = s.split()
        if not (1 <= len(words) <= 12):
            return False
        if not s[0].isupper():
            return False
        if any(c in s[:-1] for c in ".?!"):
            return False
        norm = re.sub(r"\s+", " ", s).lower()
        if norm in committee_names:
            return True
    return False


# ════════════════════════════════════════════════════════════════════
# Build the checklist from the PyMuPDF JSON report
# ════════════════════════════════════════════════════════════════════


# Detect adjacent same-type tags split across a page break in the same
# paragraph (a conversion bug — they should have been merged).

# Subsection-marker regexes used by ``_find_unmerged_split_tags``.
_RE_SUB_PAREN  = re.compile(r'^\([A-Za-z0-9]{1,4}\)\s')
_RE_SUB_LETTER = re.compile(r'^[a-z]\.\d*\.?\s')
_RE_SUB_DIGIT  = re.compile(r'^\d+\.\s')
# Full tag regex: capture tag-letter (group 1) and inner content (group 2).
_RE_TAG_FULL = re.compile(r'\[([AD])>(.*?)<\1\]', re.DOTALL)
# Paragraph-break gap: 2+ newlines with only whitespace.
_RE_PARA_GAP = re.compile(r'[ \t]*\n[ \t]*\n[ \t\n]*')


def _find_unmerged_split_tags(cleaned_text: str) -> List[Dict]:
    """Detect adjacent same-type tags that should have been merged.

    When the LLM/PyMuPDF emits a same-paragraph span as TWO separate tags
    across a page break, the cleaned text contains an artifact like::

        [D>...A local<D]

        [D>authority may erect a sign...<D]

    This is a tag-accuracy bug — the underline / strikethrough in the
    source PDF is continuous across the page break and should be ONE
    tag.  The deterministic post-processor ``merge_cross_page_break_tags``
    handles most of these automatically, but this audit-side detector is
    a defensive net: it walks the cleaned text and flags any pair of
    adjacent same-type tags that match the "should have been merged"
    shape, so the audit XLSX surfaces them even if the merger missed an
    edge case.

    The detection rules (must ALL hold):

    1. Two adjacent tags of the SAME type.
    2. Separated ONLY by a paragraph-break gap (2+ newlines, only
       whitespace).
    3. First tag's last non-whitespace character is NOT a sentence /
       clause terminator (``.``, ``!``, ``?``, ``:``, ``;``).
    4. Second tag's first non-whitespace character is a lowercase ASCII
       letter.  In legal text a new paragraph virtually always starts
       with a capital, a digit, a parenthesised label, or a section
       header.  A lowercase first letter is a strong continuation
       signal.
    5. Second tag does NOT start with a subsection marker (``(a)``,
       ``(1)``, ``a.``, ``1.``, …) — belt-and-braces guard layered on
       top of rule 4.

    Returns
    -------
    list of dict
        One entry per detected unmerged split, with keys:
        ``type`` ("addition"/"deletion"), ``first_full``,
        ``second_full``, ``first_text``, ``second_text``,
        ``first_offset``, ``second_offset``.
    """
    findings: List[Dict] = []
    if not cleaned_text:
        return findings
    tags = list(_RE_TAG_FULL.finditer(cleaned_text))
    for i in range(len(tags) - 1):
        t1 = tags[i]
        t2 = tags[i + 1]
        # Rule 1: same tag type
        if t1.group(1) != t2.group(1):
            continue
        # Rule 2: paragraph-break gap (2+ newlines, only whitespace)
        between = cleaned_text[t1.end():t2.start()]
        if not _RE_PARA_GAP.fullmatch(between):
            continue
        first_text = t1.group(2)
        second_text = t2.group(2)
        # Rule 3: first tag must not end with sentence/clause terminator
        first_rstrip = first_text.rstrip()
        if not first_rstrip:
            continue
        if first_rstrip[-1] in ".!?:;":
            continue
        # Rule 4: second tag must start with lowercase ASCII letter
        second_lstrip = second_text.lstrip()
        if not second_lstrip:
            continue
        if not (second_lstrip[0].isascii() and second_lstrip[0].islower()):
            continue
        # Rule 5: second tag must not start with a subsection marker
        if _RE_SUB_PAREN.match(second_lstrip):
            continue
        if _RE_SUB_LETTER.match(second_lstrip):
            continue
        if _RE_SUB_DIGIT.match(second_lstrip):
            continue
        findings.append({
            "type":          "addition" if t1.group(1) == "A" else "deletion",
            "first_full":    t1.group(0),
            "second_full":   t2.group(0),
            "first_text":    first_text,
            "second_text":   second_text,
            "first_offset":  t1.start(),
            "second_offset": t2.start(),
        })
    return findings


def _format_types_to_tag(format_types: List[str]) -> str:
    """Map PyMuPDF ``format_types`` → tag type used in the cleaned text.

    Convention matches the rest of the pipeline:
      strikethrough  →  deletion   [D>…<D]
      brackets       →  deletion   [D>…<D]   (text-convention deletion)
      anything else  →  addition   [A>…<A]   (underline / italic / bold)

    When a span has BOTH strikethrough AND another format, strikethrough
    wins (the cleaning convention) — so we always emit "deletion".
    """
    if not format_types:
        return "addition"
    if "strikethrough" in format_types:
        return "deletion"
    if "brackets" in format_types:
        return "deletion"
    return "addition"


def _build_checklist_from_report(pymupdf_report: Dict) -> List[Dict]:
    """Turn ``pymupdf_report['formatted_spans']`` into a numbered checklist.

    Each item has: id, tag_type, text, page, bill_line, format_types,
    bbox, context.  No fuzzy matching needed here — the report itself
    is the deterministic source of truth (it came from PDF metadata).
    """
    items: List[Dict] = []
    for i, span in enumerate(pymupdf_report.get("formatted_spans", []), start=1):
        fmt = span.get("format_types", []) or []
        tag_type = _format_types_to_tag(fmt)
        text = (span.get("text", "") or "").strip()
        items.append({
            "id": i,
            "tag_type": tag_type,
            "text": text,
            "page": span.get("page"),
            "bill_line": span.get("bill_line"),
            "format_types": fmt,
            "bbox": span.get("bbox", []),
            "context": span.get("context", ""),
        })
    return items


def _format_checklist(items: List[Dict],
                      precheck: Optional[Dict] = None) -> str:
    """Render the checklist for the Claude prompt.

    Each line shows: id, page, tag type, preview text, format types,
    surrounding context, and the deterministic ⟨CLEAN CHECK⟩ annotation
    that tells Claude exactly what the cleaned text contains (so it
    cannot hallucinate that field).
    """
    lines: List[str] = []
    for item in items:
        tag_label = "[A>…<A]" if item["tag_type"] == "addition" else "[D>…<D]"
        preview = item["text"][:120].replace("\n", " ↵ ")
        if len(item["text"]) > 120:
            preview += "…"
        fmt_str = "+".join(item.get("format_types", []) or ["?"])
        line = (
            f"  {item['id']:3d}. Page {item['page']}  {tag_label}  "
            f"({fmt_str})  \"{preview}\""
        )
        ctx = item.get("context", "")
        if ctx:
            line += f"  — context: {ctx}"
        if precheck and item["id"] in precheck:
            pc = precheck[item["id"]]
            status = pc["in_clean"]
            ct = pc.get("clean_text", "")
            if status == "yes":
                line += f"\n       ⟨CLEAN CHECK: ✅ EXACT TAG FOUND → {ct}⟩"
            elif status == "merged":
                line += f"\n       ⟨CLEAN CHECK: ✅ ABSORBED INTO LARGER TAG → {ct}⟩"
            elif status == "partial":
                line += f"\n       ⟨CLEAN CHECK: ⚠️ PARTIAL/BOUNDARY DIFF → {ct}⟩"
            elif status == "text_only":
                line += "\n       ⟨CLEAN CHECK: ❌ TEXT PRESENT BUT NOT TAGGED⟩"
            elif status == "wrong_type":
                line += f"\n       ⟨CLEAN CHECK: ❌ WRONG TAG TYPE → {ct}⟩"
            elif status == "protected":
                line += ("\n       ⟨CLEAN CHECK: 🛡️ PROTECTED CLAUSE — "
                        "LLM correctly stripped the tag (✅ pass)⟩")
            elif status == "brackets_preserved":
                line += (f"\n       ⟨CLEAN CHECK: ✅ BRACKETS PRESERVED — "
                        f"literal {ct!r} kept in clean text (bracket-deletion "
                        f"convention satisfied)⟩")
            else:
                line += "\n       ⟨CLEAN CHECK: ❌ TEXT MISSING FROM CLEAN OUTPUT⟩"
        lines.append(line)
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════
# Deterministic pre-check against the cleaned text
# ════════════════════════════════════════════════════════════════════

def _strip_tags(s: str) -> str:
    """Return s with all [A>…<A] / [D>…<D] markers removed (keeps inner text)."""
    return re.sub(r"\[([AD])>(.*?)<\1\]", r"\2", s, flags=re.DOTALL)


def _precheck_clean_text(
    checklist_items: List[Dict],
    pymupdf_clean_text: str,
    state_code: Optional[str] = None,
) -> Tuple[Dict[int, Dict], List[Dict]]:
    """
    Deterministic check: for every PyMuPDF span, was its tag preserved
    in the LLM-cleaned text?

    Returns:
      results: ``{item_id: {"in_clean": status, "clean_text": <tag-or-empty>}}``
        status is one of:
          "yes"        – exact tag with correct type found
          "merged"     – the item text is FULLY contained in a larger
                         clean tag (LLM merged this span with neighbours);
                         no characters lost, just different boundary.
          "partial"    – tag found but boundaries differ in BOTH directions
                         (some chars missing AND some extra).
          "wrong_type" – tag found but wrong type (e.g. [D>…<D] vs [A>…<A])
          "text_only"  – text appears in cleaned output but with NO tag
          "protected"  – text is a state-specific protected clause that
                         must never carry tags; the LLM correctly stripped
                         it (✅ pass, no fix needed)
          "brackets_preserved" – bracket-deletion convention (CT, MD, KY):
                         the literal bracketed text (e.g. ``[having]``)
                         appears verbatim in the cleaned output.  The
                         brackets ARE the deletion marker for these states,
                         so this is a ✅ pass even without a ``[D>…<D]``
                         wrapper.
          "no"         – text is missing entirely (likely a header/footer the
                         LLM correctly removed)

      extra_tags: list of tags found in the cleaned text that did NOT
                  match any checklist item.  These become "extra_tag"
                  rows in the audit (potential hallucinations / over-tagging).
    """
    protected = _protected_clauses_for(state_code)
    # ── Extract every tag in the cleaned text ───────────────────────
    clean_tags: List[Dict] = []
    for m in _TAG_RE.finditer(pymupdf_clean_text):
        if m.group(1):                    # [A>…<A]
            clean_tags.append({
                "type": "addition",
                "text": m.group(2).strip(),
                "full": m.group(0),
            })
        else:                             # [D>…<D]
            clean_tags.append({
                "type": "deletion",
                "text": m.group(4).strip(),
                "full": m.group(0),
            })

    plain_clean = _strip_tags(pymupdf_clean_text)
    plain_lower = plain_clean.lower()

    results: Dict[int, Dict] = {}
    used: set = set()         # tags exclusively consumed by one item
    shared: set = set()       # tags claimed by ≥1 "merged" item (not extras)

    # ── Document-order monotonic walk ───────────────────────────────
    # Both `checklist_items` (from the PyMuPDF report) and `clean_tags`
    # (from regex over the cleaned text) are in document order.  We
    # walk items left-to-right and maintain a cursor into clean_tags
    # that only ever moves forward.  This correctly handles:
    #
    #   1. Duplicate text in different sections (Section 1 vs Section 2
    #      of the same bill).  Greedy/longest-first matching would map
    #      both occurrences of an item to the FIRST clean tag, leaving
    #      the second clean tag as a phantom "extra".  The cursor
    #      naturally advances past the first match so the second item
    #      finds the second tag.
    #
    #   2. Merged tags (LLM joined several adjacent PyMuPDF spans into
    #      one cleaned tag).  We track the "active merged parent" — the
    #      most recent shared tag — and let consecutive items keep
    #      attaching to it as long as their text is contained.
    #
    #   3. Items the LLM correctly removed (page headers / footers).
    #      The cursor does NOT advance for these — so the next item
    #      will still find the next available clean tag.

    def _try_match(item, j):
        """Score how item matches clean_tags[j].
        Returns (status, consume) tuple, or None if no match."""
        ct = clean_tags[j]
        if ct["type"] != item["tag_type"]:
            return None
        itxt = _norm(item["text"])
        ntxt = _norm(ct["text"])
        if not itxt or not ntxt:
            return None
        if itxt == ntxt:
            return ("yes", True)          # exact: consume
        # ─ Bracket-deletion convention (CT / MD / KY / etc.) ─
        # When PyMuPDF reports ``[having]`` (literal brackets) as a
        # bracket-format deletion span and the LLM cleaned it into a
        # ``[D>having<D]`` tag (i.e. stripped the brackets and wrapped
        # with deletion tags) the texts differ by exactly the outer
        # ``[ ]`` — treat that as an exact match, not "partial".
        if "brackets" in (item.get("format_types") or []):
            itxt_inner = _norm(item["text"].strip().lstrip("[").rstrip("]"))
            if itxt_inner and itxt_inner == ntxt:
                return ("yes", True)
        if itxt in ntxt:
            # Item fits inside clean tag → merged parent.
            # Guard: don't absorb tiny items into huge tags
            # (e.g. "(1)" absorbed by a 500-char span).
            # Exception: if the item text starts the clean tag, it's
            # clearly a merge (e.g. "If" → "[A>If the ballot cannot...")
            if len(itxt) <= 5 and len(ntxt) > len(itxt) * 4:
                if not ntxt.startswith(itxt):
                    return None
            return ("merged", False)      # shareable: don't consume
        if ntxt in itxt:
            # Tiny clean tag inside a much longer item is NOT a real
            # partial match — it's a coincidental fragment hit.  Without
            # this guard a single period clean-tag ``[A>.<A]`` will
            # absorb a 50-char item like ``"Raised Bill No."`` purely
            # because the period appears in both.  That consumes the
            # tag and shoots the cursor far past the real parent tag,
            # breaking the merged-into-parent detection for every
            # subsequent item.
            #
            # A real boundary-mismatch partial match is one where the
            # clean tag covers a SUBSTANTIAL fraction of the item
            # (≥50 % of the chars), so the LLM clearly intended to tag
            # this span but got the boundaries wrong.  Anything shorter
            # than that is a coincidence, not a partial match.
            if len(ntxt) < 3 or len(ntxt) < len(itxt) * 0.5:
                return None
            return ("partial", True)      # clean fragment of bigger item: consume
        if _token_similarity(itxt, ntxt) >= 0.85:
            return ("partial", True)
        return None

    cursor = 0                 # next "fresh" clean tag to consider
    active_parent: Optional[int] = None    # current merged-parent index

    for item in checklist_items:
        iid = item["id"]
        itxt = _norm(item["text"])
        if not itxt:
            results[iid] = {"in_clean": "no", "clean_text": ""}
            continue

        # ─ Strategy 1: extend the active merged parent ─
        # When the LLM merged a run of adjacent PyMuPDF spans into one
        # bigger tag, consecutive items all fit inside the SAME parent.
        if active_parent is not None:
            m = _try_match(item, active_parent)
            if m and m[0] == "merged":
                results[iid] = {"in_clean": "merged",
                                "clean_text": clean_tags[active_parent]["full"]}
                shared.add(active_parent)
                continue
            # If we can't extend the parent, fall through — the parent
            # is now stale and a new search begins.

        # ─ Strategy 2: scan forward from cursor for the next fitting tag ─
        match_j: Optional[int] = None
        match_status: str = ""
        match_consume: bool = True
        j = cursor
        while j < len(clean_tags):
            if j in used:
                j += 1
                continue
            m = _try_match(item, j)
            if m is not None:
                match_j, (match_status, match_consume) = j, m
                break
            j += 1

        if match_j is None:
            # No forward match.  Don't advance the cursor (item didn't
            # consume anything); move on to the next item.
            continue

        ct = clean_tags[match_j]
        results[iid] = {"in_clean": match_status, "clean_text": ct["full"]}
        if match_consume:
            used.add(match_j)
            cursor = match_j + 1
            active_parent = None            # merged-parent chain broken
        else:
            shared.add(match_j)
            # cursor stays — next item might still attach to this parent
            cursor = match_j
            active_parent = match_j

    # ── Fallback Pass A: wrong-tag-type detection ───────────────────
    # If text matches a clean tag but the tag TYPE is wrong (e.g.
    # PyMuPDF says strikethrough → [D], LLM emitted [A]), flag it.
    #
    # IMPORTANT: this pass scans EVERY remaining clean tag, including
    # ones far away in the document, so we must apply the same
    # length-similarity guard used in ``_try_match`` to prevent a tiny
    # clean tag (single punctuation, short word) from coincidentally
    # matching a long unrelated item.  Real wrong-tag-type cases have
    # texts of roughly comparable length.
    for item in checklist_items:
        iid = item["id"]
        if iid in results:
            continue
        itxt = _norm(item["text"])
        ityp = item["tag_type"]
        if len(itxt) < 2:
            continue
        for j, ct in enumerate(clean_tags):
            if j in used or ct["type"] == ityp:
                continue
            nc = _norm(ct["text"])
            if not nc:
                continue
            # Length-similarity gate: skip coincidental tiny-fragment
            # hits where the clean tag is a small substring of a much
            # bigger item (or vice versa).  We require either an exact
            # match or that the shorter side covers at least 50 % of
            # the longer side.
            if nc == itxt:
                pass  # exact match — obvious wrong-type
            elif itxt in nc or nc in itxt:
                shorter = min(len(itxt), len(nc))
                longer = max(len(itxt), len(nc))
                if shorter < 3 or shorter < longer * 0.5:
                    continue
            else:
                continue
            used.add(j)
            results[iid] = {"in_clean": "wrong_type", "clean_text": ct["full"]}
            break

    # ── Fallback Pass B: loose match (strip § / curly quotes) ───────
    # Mirrors ``_try_match`` but using ``_norm_loose`` (strips section
    # marks, curly quotes, etc.) so items the strict normaliser missed
    # still get a chance to attach to a clean tag.
    #
    # Two sub-cases, mirroring ``_try_match``:
    #   * ``itxt_l in nc_l``  → ITEM fits inside CLEAN (LLM merged the
    #                           span into a bigger neighbouring tag) →
    #                           ``merged`` (✅ pass).
    #   * ``nc_l in itxt_l``  → CLEAN fits inside ITEM (LLM kept a
    #                           shorter fragment with wrong boundary) →
    #                           ``partial`` (❌ error).
    #
    # The length-similarity guard prevents tiny coincidental hits
    # (e.g. a single-character clean tag matching every long item that
    # happens to contain that character).
    for item in checklist_items:
        iid = item["id"]
        if iid in results:
            continue
        itxt_l = _norm_loose(item["text"])
        ityp = item["tag_type"]
        if len(itxt_l) < 2:
            continue
        for j, ct in enumerate(clean_tags):
            if j in used or ct["type"] != ityp:
                continue
            nc_l = _norm_loose(ct["text"])
            if not nc_l:
                continue
            if itxt_l == nc_l:
                # Exact match (modulo loose normalisation) → exact pass.
                used.add(j)
                results[iid] = {"in_clean": "yes", "clean_text": ct["full"]}
                break
            if itxt_l in nc_l:
                # Item absorbed by a larger clean tag → merged (✅ pass).
                # Tiny-item guard: a 1-2 char item could coincidentally
                # match anywhere, so require ≥3 chars.  PyMuPDF
                # highlight artifacts often produce short fragments
                # (e.g. "Each", ", as") that the LLM correctly merges
                # into a larger parent tag — those must reach this
                # branch.
                if len(itxt_l) < 3:
                    continue
                # Don't consume — the clean tag may belong to another
                # PyMuPDF item as the primary owner.
                results[iid] = {"in_clean": "merged", "clean_text": ct["full"]}
                break
            if nc_l in itxt_l:
                # Clean tag is a shorter fragment of the item → real
                # partial-match (❌ error).  Tiny-clean guard prevents
                # coincidental hits.
                shorter = len(nc_l)
                longer = len(itxt_l)
                if shorter < 3 or shorter < longer * 0.5:
                    continue
                used.add(j)
                results[iid] = {"in_clean": "partial", "clean_text": ct["full"]}
                break

    # ── Fallback Pass C: text-only (text in plain output, no tag) ───
    # Distinguishes "tag was dropped but text survived" from "entire
    # span was removed (probably a header/footer)".
    for item in checklist_items:
        iid = item["id"]
        if iid in results:
            continue
        itxt = item["text"].strip()
        if len(itxt) >= 3 and itxt.lower() in plain_lower:
            results[iid] = {"in_clean": "text_only", "clean_text": ""}

    # Anything still unrecorded → "no"
    for item in checklist_items:
        if item["id"] not in results:
            results[item["id"]] = {"in_clean": "no", "clean_text": ""}

    # ── Bracket-deletion convention reclassification ──────────────
    # For states whose deletion convention is square brackets (CT, MD,
    # KY, etc.) PyMuPDF reports the bracketed span (e.g. ``[having]``)
    # with ``format_types=['brackets']``.  The LLM cleaning step
    # typically preserves the LITERAL bracketed text in the output
    # rather than wrapping it in a ``[D>…<D]`` tag — the brackets ARE
    # the visible deletion marker.  When this is the case we must NOT
    # flag the item as ``tag_dropped`` / ``partial_match``; it is a ✅
    # pass.  We override the in_clean status to ``brackets_preserved``
    # so the post-processing layer surfaces it correctly.
    #
    # NORMALISATION: in CT bills (and any state with line-numbered
    # body text) PyMuPDF embeds the body line numbers INSIDE the
    # bracketed span, e.g.
    #     ``[may \n9 \nbe fixed by such boards ...]``
    # The LLM cleaning step correctly strips those line numbers, so
    # the cleaned text contains
    #     ``[may be fixed by such boards ...]``
    # A raw substring check fails because the embedded ``9`` / ``10``
    # line numbers don't appear in the clean side.  Normalising both
    # sides (strip line-number-only lines, collapse whitespace) makes
    # the comparison robust.
    _RE_INLINE_LINE_NUMBERS = re.compile(r"\n\s*\d{1,3}\s*\n")
    # Inter-page header artifacts that appear INSIDE cross-page bracket
    # spans.  When a ``[...]`` deletion span starts on page N and closes
    # on page N+1, PyMuPDF's raw extraction includes page N+1's running
    # header (``Raised Bill No. 1217``, ``LCO No. 3862``, ``3 of 5``,
    # ``General Assembly``, etc.) in the middle of the bracket text.
    # The LLM correctly strips those headers when cleaning, so a
    # substring check between the raw item text and the cleaned text
    # fails.  These patterns scrub the most common headers from the
    # normalised text used for the brackets_preserved post-pass — they
    # are a no-op for bills that don't contain those phrases (i.e.
    # non-CT states) so the change is universally safe.
    _RE_PAGE_HEADERS = re.compile(
        r"\b(?:"
        r"Raised\s+Bill\s+No\.?\s*\d+"
        r"|Substitute\s+(?:Senate|House)?\s*Bill\s+No\.?\s*\d+"
        r"|Proposed\s+Bill\s+No\.?\s*\d+"
        r"|Committee\s+Bill\s+No\.?\s*\d+"
        r"|(?:Senate|House)\s+Bill\s+No\.?\s*\d+"
        r"|LCO\s+No\.?\s*\d+"
        r"|File\s+No\.?\s*\d+"
        r"|General\s+Assembly"
        r"|(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+Session,?\s*\d{4}"
        r"|\d+\s+of\s+\d+"          # "3 of 5" page-of-pages indicator
        r")\b",
        re.IGNORECASE,
    )

    def _norm_brackets(s: str) -> str:
        """Strip embedded line numbers, page headers + collapse whitespace.

        Used to compare PyMuPDF-raw bracket spans against the cleaned
        text.  The raw side has body line numbers and (for cross-page
        spans) running page headers embedded in the middle of the
        bracketed text; the cleaned side does not.  This normalisation
        removes both kinds of artifacts so a simple substring check
        can determine whether the bracketed content survived cleaning.
        """
        if not s:
            return s
        # Remove blank-line + digits + blank-line (the CT line-number
        # pattern that PyMuPDF leaves inside bracketed multi-line spans).
        s = _RE_INLINE_LINE_NUMBERS.sub("\n", s)
        # Strip inter-page running headers.
        s = _RE_PAGE_HEADERS.sub(" ", s)
        return " ".join(s.split())

    _normalised_clean = _norm_brackets(pymupdf_clean_text)

    # Pre-normalise every clean_tag's inner text once, so the post-pass
    # below can cheaply link a brackets_preserved item to its wrapping
    # ``[D>[text]<D]`` tag (if the LLM kept the literal brackets AND
    # wrapped them — both representations of the same span).  Without
    # this link the phantom-detection pass at the end would flag the
    # wrapper as an "extra_tag", producing a duplicate ERROR row that
    # describes the same content as the brackets_preserved PASS row.
    _normalised_ct_text: List[str] = [
        _norm_brackets(ct["text"]) for ct in clean_tags
    ]

    n_bracket_pass = 0
    for item in checklist_items:
        if "brackets" not in (item.get("format_types") or []):
            continue
        iid = item["id"]
        cur = results[iid]["in_clean"]
        if cur in ("yes", "merged"):
            # Already counted as a pass — don't override.
            continue
        item_txt = (item.get("text") or "").strip()
        if len(item_txt) < 2:
            continue
        # Literal occurrence in the cleaned text.  Brackets are part of
        # the markup, so the comparison is case-sensitive and intact.
        normalised_item = _norm_brackets(item_txt)
        if item_txt in pymupdf_clean_text or (
            normalised_item and normalised_item in _normalised_clean
        ):
            results[iid] = {"in_clean": "brackets_preserved",
                            "clean_text": item_txt}
            n_bracket_pass += 1
            # Link the wrapping [D>[...]<D] / [A>[...]<A] tag (if any)
            # so phantom-detection doesn't double-count it.  The LLM
            # sometimes keeps the literal ``[text]`` AND wraps it in
            # the deletion/addition marker — both refer to the same
            # span, so consuming the wrapping clean_tag here prevents
            # a redundant "extra_tag" error row for the same content.
            if normalised_item:
                for j, n_ct in enumerate(_normalised_ct_text):
                    if j in used:
                        continue
                    if clean_tags[j]["type"] != item["tag_type"]:
                        continue
                    if n_ct and (
                        n_ct == normalised_item
                        or normalised_item in n_ct
                        or n_ct in normalised_item
                    ):
                        used.add(j)
                        break
    if n_bracket_pass:
        log.info("⌫️  Marked %d bracket-format item(s) as brackets_preserved",
                 n_bracket_pass)

    # ── Protected-clause reclassification ──────────────────────
    # State-specific phrases (e.g. "Be it enacted by the Legislature of
    # the State of Kansas:") must never carry [A>…<A] / [D>…<D] tags.
    # When the LLM correctly strips the tag, the item ends up as
    # ``text_only`` or ``no`` — promote those to ``protected`` so the
    # post-processing layer marks them as ✅ pass.
    if protected:
        n_promoted = 0
        for item in checklist_items:
            iid = item["id"]
            cur = results[iid]["in_clean"]
            if cur in ("yes", "merged", "partial", "wrong_type"):
                # Tag survived in the cleaned text — that's the BUG case
                # (protected clause should NOT be tagged) — leave as-is
                # so the post-processor can flag it.
                continue
            if _is_protected_text(item["text"], protected):
                results[iid] = {"in_clean": "protected", "clean_text": ""}
                n_promoted += 1
        if n_promoted:
            log.info("🛡️  Marked %d item(s) as protected-clause (state=%s)",
                     n_promoted, state_code)

    # ── Collect unmatched clean tags as potential hallucinations ────
    # A tag is "extra" only if no item claimed it AND no merged-parent
    # item absorbed it.
    extra_tags: List[Dict] = []
    for j, ct in enumerate(clean_tags):
        if j in used or j in shared:
            continue
        extra_tags.append({
            "type": ct["type"],
            "text": ct["text"],
            "full": ct["full"],
        })

    return results, extra_tags


# ════════════════════════════════════════════════════════════════════
# Claude audit prompt
# ════════════════════════════════════════════════════════════════════

_AUDIT_PROMPT = """\
You are a quality-assurance auditor validating an LLM-cleaned version of a
PyMuPDF-extracted legislative text.

══════════════════════════════════════
  GROUND TRUTH (1) — Highlighted PDF Page Images
══════════════════════════════════════
The {n_images} highlighted images attached are the AUTHORITATIVE source of
truth for what is actually formatted in the PDF.  Coloured overlays mark
formatting detected in the PDF:
  🟢 Green   = UNDERLINED (typically additions / new text)
  🔴 Red     = STRIKETHROUGH (typically deletions)
  🔵 Blue    = ITALIC
  🟠 Orange  = BOLD
What you SEE in these images is the truth about the document.

⚠️ STRIKETHROUGH PRIORITY: text that is BOTH struck-through AND
underlined / italic / bold must be tagged ``[D>…<D]`` only.

══════════════════════════════════════
  GROUND TRUTH (2) — PyMuPDF JSON Report
══════════════════════════════════════
PyMuPDF deterministically extracted these {n_checklist} formatted spans
from the PDF metadata (per-character bbox classification — very precise
about WHAT was formatted, occasionally wrong about WHETHER it really is
formatting versus a false positive such as a table border).

══════════════════════════════════════
  VALIDATION TARGET — LLM-Cleaned PyMuPDF Text
══════════════════════════════════════
After PyMuPDF produced its raw tagged text, an LLM cleaning pass:
  • removed page headers / footers / line numbers
  • normalised whitespace
  • merged split tags
  • removed nested tags
  • stripped tags from state-specific protected clauses (intentional!)

The cleaned text uses:
  [A>…<A]  = additions / new text
  [D>…<D]  = deletions

YOUR JOB: audit the cleaned text against BOTH ground-truth sources.

CLEANED TEXT (this is what we are validating):
{clean_text}

══════════════════════════════════════
  CHECKLIST — PyMuPDF Formatted Spans
══════════════════════════════════════
Each item: ``id. Page N  [tag-label]  (format-types)  "text"  — context: …``

{checklist}

STATE: {state_code}
STATE RULES: {state_rules}

══════════════════════════════════════
  YOUR TASK — TWO PHASES
══════════════════════════════════════

━━━ PHASE 1: WALK THE CHECKLIST ━━━
For EACH of the {n_checklist} items above, decide:

1. **in_image**: Is this formatting REAL in the highlighted image?
   - "yes" — the text IS underlined / struck-through / italic / bold in
     the image, exactly as PyMuPDF said.
   - "false_positive_header" — this came from a repeated page header or
     footer (bill number, "File No.", running page-id rule).
   - "false_positive_table" — table cell border that PyMuPDF mistook for
     an underline (effective-date tables, fiscal-note tables, etc.).
   - "false_positive_other" — any other PyMuPDF false positive
     (decorative rule, signature line, etc.).

   Note: ALL underlined punctuation is real — periods, commas, semicolons
   that carry a green highlight overlay must be treated as "yes".

2. **in_clean**: USE the ⟨CLEAN CHECK⟩ annotation — it is the result of
   a DETERMINISTIC regex scan of the cleaned text. Do NOT guess.
   - ✅ EXACT TAG FOUND        →  in_clean="yes",        clean_text=<tag>
   - ✅ ABSORBED INTO LARGER   →  in_clean="merged",     clean_text=<tag>
        (the LLM merged this span with neighbours — no content lost)
   - ✅ PROTECTED CLAUSE       →  in_clean="protected",  clean_text=""
        (state-specific phrase that must never carry tags; the LLM
        correctly stripped it — do NOT flag this as an error)
   - ✅ BRACKETS PRESERVED     →  in_clean="brackets_preserved", clean_text=<bracketed text>
        (CT/MD/KY bracket-deletion convention: the literal ``[…]``
        bracketed text was kept in the cleaned output — the brackets
        ARE the deletion marker for this state, so this is a ✅ pass
        even though there is no ``[D>…<D]`` tag wrapping)
   - ⚠️ PARTIAL/BOUNDARY       →  in_clean="partial",    clean_text=<tag>
   - ❌ WRONG TAG TYPE          →  in_clean="wrong_type", clean_text=<tag>
   - ❌ TEXT PRESENT BUT NOT TAGGED → in_clean="text_only", clean_text=""
   - ❌ TEXT MISSING            →  in_clean="no",         clean_text=""

3. **classification**: One of:
   - "match"                — real formatting, tag preserved correctly. ✅
   - "merged_into_parent"   — real formatting, LLM merged with a
                              neighbouring span (item text fully inside a
                              bigger tag, no chars lost). ✅ pass.
   - "protected_clause"     — state-specific phrase that must never
                              carry tags (e.g. "Be it enacted by the
                              Legislature of the State of Kansas:");
                              the LLM correctly stripped the tag. ✅ pass.
   - "brackets_preserved"   — bracket-deletion convention (CT/MD/KY):
                              the literal ``[…]`` bracketed text was
                              kept as-is in the cleaned output.  The
                              brackets themselves ARE the deletion
                              marker for these states, so this is ✅ pass.
   - "header_dropped"       — false-positive header/footer, LLM correctly
                              removed both text AND tag. ✅ (no fix needed)
   - "decorative_header_dropped" — Indiana-specific: bold/underlined
                              decorative header element (bill title,
                              version label, digest heading, committee
                              name, A BILL FOR AN ACT, session line,
                              section label, decorative rule) where the
                              IN clean-prompt explicitly instructs the
                              LLM to STRIP the tag while keeping the
                              text.  Expected behavior, NOT an error.
                              ✅ pass.
   - "table_false_positive" — PyMuPDF flagged a table border; LLM
                              correctly omitted it. ✅ pass
   - "other_false_positive" — any other PyMuPDF false positive that the
                              LLM correctly stripped (e.g. CT's bracketed
                              boilerplate footer "[Proposed deletions are
                              enclosed in brackets and italicized.]"). ✅ pass
   - "tag_dropped"          — REAL formatting (in_image=yes) but the LLM
                              kept the text and DROPPED the tag. ❌ ERROR
                              (the cleaning lost the formatting markup).
   - "content_dropped"      — REAL formatting in image, but the LLM
                              removed BOTH the tag AND the text. ❌ ERROR
                              if the text is body content (vs ✅ if it
                              was correctly identified as a header).
   - "partial_match"        — tag exists but boundaries are wrong
                              (extra/missing characters). ❌ ERROR
   - "wrong_tag_type"       — tag exists but with the WRONG type
                              (e.g. [A>…<A] for a strikethrough span).
                              ❌ ERROR.
   - "linked_fragment"      — short fragment (≤3 chars like "." or "/")
                              already absorbed into a neighbouring tag.
                              ✅ pass.

4. **verdict**: "✅ pass", "⚠️ warning", or "❌ error" — must match
   the classification above.  Pass classes:
     match, header_dropped, merged_into_parent, protected_clause,
     brackets_preserved, linked_fragment, decorative_header_dropped,
     table_false_positive, other_false_positive       ← LLM correctly
         stripped a PyMuPDF false positive, so the cleaned output is
         right and the row should be ✅ pass (NOT a warning).
   Error classes: tag_dropped, content_dropped, partial_match,
     wrong_tag_type, extra_tag, protected_clause_tagged.
   Warnings are reserved for genuine partial / boundary cases
   (``in_clean="partial"``) where the cleaned output kept the tag but
   with imperfect boundaries — i.e. the user has to actually look.

5. **clean_text**: copy verbatim from the ⟨CLEAN CHECK⟩ annotation.
   Do NOT invent text that is not in the cleaned output.

6. **notes**: one short sentence of reasoning.

7. **suggested_fix**: concrete instruction or "" (empty when no fix needed).
   - tag_dropped:      "Restore [A>text<A] around 'text' on page N"
   - content_dropped:  "Re-add the dropped text '<…>' on page N"
   - partial_match:    "Change [A>X<A] to [A>Y<A] on page N"
   - wrong_tag_type:   "Re-tag as [D>text<D] (strikethrough), not [A>…<A]"
   - header_dropped / *_false_positive: "" (no fix needed)

━━━ PHASE 2: OPEN-ENDED AUDIT ━━━
Look BEYOND the checklist.  Issues to flag in ``additional_findings``:
  • **extra_tag**          — tag in cleaned text with NO matching report
                             span AND not visible in any image.
  • **visible_missed**     — formatting CLEARLY visible in the image but
                             absent from both report and cleaned text
                             (PyMuPDF detection failure).
  • **content_added**      — cleaned text contains words / sentences that
                             do NOT appear in the images (LLM hallucinated).
  • **content_lost**       — body-text content visible in the image but
                             missing from the cleaned text (LLM over-zealous
                             header removal).

Each additional finding needs: page, issue_type, description,
clean_text (if any), image_text (if any), suggested_fix.

══════════════════════════════════════
  OUTPUT FORMAT — JSON ONLY
══════════════════════════════════════
Respond with ONLY valid JSON (no markdown fences, no commentary):

{{
  "checklist_results": [
    {{
      "checklist_id": 1,
      "tag_type": "addition",
      "page": 1,
      "pymupdf_text": "Section 1.",
      "in_image": "yes",
      "in_clean": "yes",
      "clean_text": "[A>Section 1.<A]",
      "verdict": "✅ pass",
      "classification": "match",
      "notes": "Underlined in image, tag preserved by LLM",
      "suggested_fix": ""
    }}
  ],
  "additional_findings": [
    {{
      "id": 1,
      "page": 2,
      "issue_type": "extra_tag",
      "description": "Cleaned text has [A>foo<A] but neither the report nor the image shows underline on 'foo'",
      "clean_text": "[A>foo<A]",
      "image_text": "",
      "suggested_fix": "Remove the [A>…<A] tags around 'foo' on page 2"
    }}
  ],
  "summary": {{
    "total_checklist_items": {n_checklist},
    "passes": <int>,
    "warnings": <int>,
    "errors": <int>,
    "additional_findings_count": <int>
  }}
}}
"""


# ════════════════════════════════════════════════════════════════════
# Main entry point
# ════════════════════════════════════════════════════════════════════

# Classifications that ALWAYS resolve to ❌ error
_ERROR_CLASSES = {
    "tag_dropped",
    "partial_match",
    "wrong_tag_type",
    "content_dropped",
    "extra_tag",
    "protected_clause_tagged",
    # Same-paragraph span split into two separate same-type tags across a
    # page break.  Detected by :func:`_find_unmerged_split_tags`.  The
    # deterministic merger ``merge_cross_page_break_tags`` should have
    # combined these into one tag; if the audit still sees the split it
    # means the merger missed an edge case.
    "unmerged_split_tag",
}
# Classifications that ALWAYS resolve to ⚠️ warning.
#
# NOTE: ``table_false_positive`` and ``other_false_positive`` used to live
# here, but both mean "PyMuPDF over-detected formatting, the LLM correctly
# stripped it".  Surfacing those as warnings produced a steady stream of
# false alarms in the validation review tab (e.g. CT's boilerplate footer
# "[Proposed deletions are enclosed in brackets and italicized.]" was
# repeatedly flagged even though the LLM did exactly the right thing by
# omitting it).  They now resolve to ✅ pass — see ``_PASS_CLASSES`` below.
_WARNING_CLASSES: set[str] = set()
# Classifications that ALWAYS resolve to ✅ pass
_PASS_CLASSES = {
    "match",
    "header_dropped",
    "linked_fragment",
    "merged_into_parent",
    "protected_clause",
    "brackets_preserved",
    # PyMuPDF false positives that the LLM correctly stripped — the cleaned
    # output is right, so this is a pass (not a warning the user has to
    # manually clear).
    "table_false_positive",
    "other_false_positive",
    # Indiana-specific: decorative bold/underlined headers (bill title,
    # version label, digest heading, A BILL FOR AN ACT title, session
    # line, committee name, section labels, decorative rules) where the
    # IN clean-prompt explicitly instructs the LLM to STRIP the
    # ``[A>...<A]`` tag while preserving the inner text.  Detected by
    # :func:`_is_in_decorative_header`.  This is the EXPECTED behavior
    # per the IN prompt — NOT a ``tag_dropped`` error.
    "decorative_header_dropped",
}


def pymupdf_compare_tags(
    highlighted_images_b64: List[str],
    pymupdf_report: Dict,
    pymupdf_clean_text: str,
    state_code: str,
    state_rules: str = "",
    output_xlsx: str = "pymupdf_clean_audit.xlsx",
    model: str = "claude-opus-4-7",
) -> Dict:
    """
    Audit the LLM-cleaned PyMuPDF text using Claude Opus 4.7 vision.

    Parameters
    ----------
    highlighted_images_b64
        Base64-encoded PNGs of every page with PyMuPDF's formatting
        overlay (output of ``highlight_pdf_formatting``).
    pymupdf_report
        The JSON dict from ``extract_pdf_json_report`` — deterministic
        ground truth for tag locations.
    pymupdf_clean_text
        The cleaned text after ``clean_extracted_text`` + post-processing
        (this is the validation target).
    state_code, state_rules
        Passed verbatim into the prompt so Claude knows the state's
        markup conventions.
    output_xlsx
        Path of the Excel report to write.
    model
        Anthropic model id (default ``claude-opus-4-7``).

    Returns
    -------
    dict
        Summary counts plus the path of the saved Excel workbook.
    """
    # ── Build the checklist directly from the report ────────────────
    checklist_items = _build_checklist_from_report(pymupdf_report)
    log.info("📋 Built checklist: %d formatted spans from PyMuPDF report",
             len(checklist_items))

    # ── Deterministic pre-check against the cleaned text ────────────
    precheck, extra_clean_tags = _precheck_clean_text(
        checklist_items, pymupdf_clean_text, state_code=state_code
    )
    n_yes        = sum(1 for v in precheck.values() if v["in_clean"] == "yes")
    n_partial    = sum(1 for v in precheck.values() if v["in_clean"] == "partial")
    n_text_only  = sum(1 for v in precheck.values() if v["in_clean"] == "text_only")
    n_wrong_typ  = sum(1 for v in precheck.values() if v["in_clean"] == "wrong_type")
    n_no         = sum(1 for v in precheck.values() if v["in_clean"] == "no")
    log.info(
        "🔍 Clean pre-check: %d exact, %d partial, %d text-only, "
        "%d wrong-type, %d missing, %d extra tags in cleaned text",
        n_yes, n_partial, n_text_only, n_wrong_typ, n_no, len(extra_clean_tags),
    )

    checklist_str = _format_checklist(checklist_items, precheck)

    # ── Build prompt ────────────────────────────────────────────────
    prompt = _AUDIT_PROMPT.format(
        state_code=state_code,
        state_rules=state_rules or f"State: {state_code}",
        n_images=len(highlighted_images_b64),
        clean_text=pymupdf_clean_text,
        n_checklist=len(checklist_items),
        checklist=checklist_str,
    )
    log.debug("📝 FULL AUDIT PROMPT (%d chars):\n%s", len(prompt), prompt)
    log.info("📝 PyMuPDF-clean audit prompt: %d chars", len(prompt))

    # ── Build messages with images first ────────────────────────────
    content_parts: List[Dict] = []
    for i, img_b64 in enumerate(highlighted_images_b64):
        content_parts.append({
            "type": "text",
            "text": f"[Highlighted page {i + 1} of {len(highlighted_images_b64)}]",
        })
        content_parts.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": img_b64,
            },
        })
    content_parts.append({"type": "text", "text": prompt})

    # ── Call Claude Opus 4.7 ────────────────────────────────────────
    log.info("🤖 Sending %d images + %d-item checklist to %s ...",
             len(highlighted_images_b64), len(checklist_items), model)
    client = _get_anthropic_client()

    raw_response = ""
    with client.messages.stream(
        model=model,
        max_tokens=128000,
        messages=[{"role": "user", "content": content_parts}],
    ) as stream:
        for text in stream.text_stream:
            raw_response += text
        final_msg = stream.get_final_message()
    raw_response = raw_response.strip()

    # ── Token tracking (best-effort — uses the main app's tracker) ──
    try:
        import sys as _sys
        _main_mod = _sys.modules.get("__main__")
        if _main_mod and hasattr(_main_mod, "_track_usage"):
            _main_mod._track_usage("PyMuPDF Clean Audit", final_msg.usage, model)
        else:
            raise AttributeError("no tracker")
    except Exception:
        if hasattr(final_msg, "usage") and final_msg.usage:
            u = final_msg.usage
            log.info("💰 [PyMuPDF Clean Audit] Tokens: %s in / %s out",
                     getattr(u, "input_tokens", "?"),
                     getattr(u, "output_tokens", "?"))

    # ── Parse JSON (with repair fallback) ───────────────────────────
    if raw_response.startswith("```"):
        raw_response = "\n".join(
            ln for ln in raw_response.split("\n") if not ln.strip().startswith("```")
        )
    try:
        result = json.loads(raw_response)
    except json.JSONDecodeError:
        log.warning("JSON parse failed, attempting repair...")
        raw_response = _repair_json(raw_response)
        result = json.loads(raw_response)

    checklist_results: List[Dict] = result.get("checklist_results", [])
    additional_findings: List[Dict] = result.get("additional_findings", [])
    log.info("✅ Audit returned %d checklist results, %d additional findings",
             len(checklist_results), len(additional_findings))

    # ── Inject deterministic extra-tag findings as checklist rows ───
    if extra_clean_tags:
        log.info("➕ Injecting %d extra clean tags as checklist rows",
                 len(extra_clean_tags))
        max_cid = max(
            (cr.get("checklist_id", 0) for cr in checklist_results),
            default=len(checklist_items),
        )
        protected_for_state = _protected_clauses_for(state_code)
        for i, et in enumerate(extra_clean_tags, start=max_cid + 1):
            tag_label = "[A>...<A]" if et["type"] == "addition" else "[D>...<D]"
            # Did the LLM forget to strip a protected clause?  If the
            # phantom tag wraps protected-clause text, this is a
            # specific bug ("protected_clause_tagged") — the LLM should
            # have removed the tag because state policy forbids it.
            is_prot = _is_protected_text(et["text"], protected_for_state)
            if is_prot:
                cr_row = {
                    "checklist_id": i,
                    "tag_type": et["type"],
                    "page": "",
                    "pymupdf_text": "",
                    "clean_text": et["full"],
                    "in_image": "yes",          # the text IS in the document
                    "in_clean": "tagged",       # tag survived (the bug)
                    "verdict": "❌ error",
                    "classification": "protected_clause_tagged",
                    "notes": (
                        f"PROTECTED CLAUSE TAGGED: the cleaned text has "
                        f"{tag_label} wrapping a protected clause for "
                        f"{state_code} — state policy forbids tagging this "
                        f"phrase. The LLM cleaning step should have stripped "
                        f"the tag but didn't."
                    ),
                    "suggested_fix": (
                        f"Remove the {tag_label} around "
                        f"'{et['text'][:120]}' — it is a protected "
                        f"{state_code} clause and must never be tagged."
                    ),
                }
            else:
                cr_row = {
                    "checklist_id": i,
                    "tag_type": et["type"],
                    "page": "",
                    "pymupdf_text": "",          # no source span exists
                    "clean_text": et["full"],
                    "in_image": "no",            # no report ⇒ not in highlighted image
                    "in_clean": "phantom",       # clearer than "yes" for a hallucinated tag
                    "verdict": "❌ error",
                    "classification": "extra_tag",
                    "notes": (
                        f"EXTRA TAG: cleaned text has {tag_label} but no matching "
                        f"span exists in the PyMuPDF report — the LLM added this "
                        f"formatting on its own."
                    ),
                    "suggested_fix": (
                        f"Verify the {tag_label} around '{et['text'][:120]}' "
                        f"— remove if not visible in the highlighted image."
                    ),
                }
            checklist_results.append(cr_row)
            checklist_items.append({
                "id": i,
                "tag_type": et["type"],
                "text": "",                 # leave PyMuPDF Text column blank for phantoms
                "page": "",
                "bill_line": "",
                "format_types": [],
                "bbox": [],
                "context": f"Extra tag added by LLM: {et['full'][:80]}",
            })

    # ── Inject deterministic unmerged-split-tag findings as rows ────
    # Scan the cleaned text for adjacent same-type tag pairs that look
    # like a single paragraph that was split across a page break (first
    # tag ends mid-sentence with no terminator, second tag starts with
    # a lowercase letter — clear continuation).  The deterministic
    # post-processor ``merge_cross_page_break_tags`` should have merged
    # these into a single tag at conversion time; any pair still visible
    # here means the merger missed an edge case and the audit must
    # surface it as a tag-accuracy error.
    unmerged_splits = _find_unmerged_split_tags(pymupdf_clean_text)
    if unmerged_splits:
        log.info("➕ Injecting %d unmerged-split-tag finding(s) as checklist rows",
                 len(unmerged_splits))
        max_cid = max(
            (cr.get("checklist_id", 0) for cr in checklist_results),
            default=len(checklist_items),
        )
        for i, sp in enumerate(unmerged_splits, start=max_cid + 1):
            tag_label = "[A>…<A]" if sp["type"] == "addition" else "[D>…<D]"
            # Trim long previews for readability in the audit XLSX.
            first_preview  = sp["first_text"].rstrip()
            second_preview = sp["second_text"].lstrip()
            if len(first_preview) > 80:
                first_preview = "…" + first_preview[-77:]
            if len(second_preview) > 80:
                second_preview = second_preview[:77] + "…"
            cr_row = {
                "checklist_id":  i,
                "tag_type":      sp["type"],
                "page":          "",
                "pymupdf_text":  "",
                "clean_text":    f"{sp['first_full']}  …  {sp['second_full']}",
                "in_image":      "yes",        # the formatting IS in the PDF
                "in_clean":      "split",      # but cleaned text has it as TWO tags
                "verdict":       "❌ error",
                "classification": "unmerged_split_tag",
                "notes": (
                    f"UNMERGED SPLIT TAG: two adjacent {tag_label} tags in the "
                    f"cleaned text should have been merged into one. The first "
                    f"ends with '{first_preview}' (no sentence terminator) and "
                    f"the second starts with '{second_preview}' (lowercase "
                    f"continuation) — this is the shape of a paragraph that "
                    f"wrapped across a page break in the source PDF. The "
                    f"deterministic merger ``merge_cross_page_break_tags`` "
                    f"should have combined them into a single tag."
                ),
                "suggested_fix": (
                    f"Merge the two adjacent {tag_label} tags into one: "
                    f"'{first_preview} {second_preview}'."
                ),
            }
            checklist_results.append(cr_row)
            checklist_items.append({
                "id":          i,
                "tag_type":    sp["type"],
                "text":        "",
                "page":        "",
                "bill_line":   "",
                "format_types": [],
                "bbox":        [],
                "context":     (
                    f"Unmerged split: {sp['first_full'][:40]}…  …  "
                    f"…{sp['second_full'][-40:]}"
                ),
            })

    # ── Lookups from the deterministic checklist ────────────────────
    _full_text = {ci["id"]: ci["text"]      for ci in checklist_items}
    _item_page = {ci["id"]: ci["page"]      for ci in checklist_items}
    _item_line = {ci["id"]: ci["bill_line"] for ci in checklist_items}
    _item_ctx  = {ci["id"]: ci["context"]   for ci in checklist_items}

    # ── Override page / pymupdf_text with deterministic values ──────
    for cr in checklist_results:
        try:
            cid = int(cr.get("checklist_id"))
        except (ValueError, TypeError):
            continue
        if cid in _item_page and _item_page[cid] not in (None, ""):
            cr["page"] = _item_page[cid]
        if cid in _full_text and _full_text[cid]:
            cr["pymupdf_text"] = _full_text[cid]

    # ── Override in_clean / clean_text with deterministic pre-check ──
    n_overridden = 0
    for cr in checklist_results:
        try:
            cid = int(cr.get("checklist_id"))
        except (ValueError, TypeError):
            continue
        if cid not in precheck:
            continue
        pc = precheck[cid]
        old_in = cr.get("in_clean", "")
        old_ct = cr.get("clean_text", "")
        new_in = pc["in_clean"]
        new_ct = pc["clean_text"]
        if old_in != new_in or old_ct != new_ct:
            n_overridden += 1
        cr["in_clean"] = new_in
        cr["clean_text"] = new_ct
    if n_overridden:
        log.info("🔧 Overrode in_clean/clean_text on %d items (deterministic)",
                 n_overridden)

    # ── Auto-correct classification when pre-check disagrees with GPT ──
    # Build a set of checklist-ids whose text is a state-protected clause —
    # used below to flag the bug case where the LLM forgot to strip the tag.
    protected_for_state = _protected_clauses_for(state_code)
    protected_item_ids: set = {
        ci["id"] for ci in checklist_items
        if _is_protected_text(ci.get("text", ""), protected_for_state)
    }
    if protected_item_ids:
        log.info("🛡️  %d checklist item(s) are protected clauses for %s",
                 len(protected_item_ids), state_code)

    # ── Indiana decorative-header pre-compute ────────────────────
    # The IN clean-prompt instructs the LLM to STRIP [A>...<A] tags from
    # decorative header/structural elements (bill title, version label,
    # digest heading, A BILL FOR AN ACT, session line, committee name,
    # section labels, decorative rules) while preserving the inner text.
    # When the audit sees a ``text_only`` (tag dropped, text preserved)
    # item matching one of these patterns, that is the EXPECTED behavior
    # per the IN prompt — NOT a ``tag_dropped`` error.  Pre-compute the
    # committee-name set once so the classifier can cross-validate short
    # title-cased standalone lines against the body's ``Committee on X``
    # references.
    in_committee_names: set = set()
    if state_code and state_code.upper() == "IN":
        in_committee_names = _committee_headers_from_clean_text(
            pymupdf_clean_text
        )
        if in_committee_names:
            log.info(
                "🏛️  IN audit: pre-computed %d committee-name reference(s) for "
                "decorative-header cross-validation",
                len(in_committee_names),
            )

    for cr in checklist_results:
        in_img = cr.get("in_image", "")
        in_clean = cr.get("in_clean", "")
        cls = cr.get("classification", "")
        is_real = in_img == "yes"
        is_fp_header = (in_img == "false_positive_header"
                        or cls == "header_dropped")
        is_fp_table = (in_img == "false_positive_table"
                       or cls == "table_false_positive")
        is_fp_other = (in_img == "false_positive_other"
                       or cls == "other_false_positive")

        try:
            cid_int = int(cr.get("checklist_id", 0))
        except (ValueError, TypeError):
            cid_int = 0
        is_protected_item = cid_int in protected_item_ids

        # ── Protected-clause cases take precedence ───────────────
        # If the item's text IS a protected clause, the LLM is supposed
        # to drop the tag.  Two sub-cases:
        #   (a) tag IS gone (in_clean=protected) → ✅ pass
        #   (b) tag STILL there (in_clean in yes/partial/wrong_type)
        #       → ❌ error: the LLM forgot to strip it.
        #   (c) tag is "merged" — the span was absorbed into a LARGER
        #       parent tag.  The parent tag wraps more than just the
        #       protected text, so this is a legitimate merge, NOT a
        #       protected-clause violation.  Let it fall through to the
        #       normal merged_into_parent logic below.
        if is_protected_item and in_clean in ("yes", "partial",
                                              "wrong_type"):
            cr["classification"] = "protected_clause_tagged"
            cr["verdict"] = "❌ error"
            tag_label = ("[D>…<D]" if cr.get("tag_type") == "deletion"
                         else "[A>…<A]")
            pymupdf_txt = _full_text.get(cid_int, cr.get("pymupdf_text", ""))
            cr["notes"] = (
                f"PROTECTED CLAUSE TAGGED: '{pymupdf_txt[:80]}' is a "
                f"protected clause for {state_code} — state policy forbids "
                f"tagging this phrase.  The LLM cleaning step should have "
                f"stripped the {tag_label} tag but didn't."
            )
            cr["suggested_fix"] = (
                f"Remove the {tag_label} tag around '{pymupdf_txt[:120]}' "
                f"— it is a protected {state_code} clause and must never "
                f"be tagged."
            )
            continue

        # Real formatting + tag preserved → match
        if is_real and in_clean == "yes" and cls != "match":
            cr["classification"] = "match"
            cr["verdict"] = "✅ pass"
            cr["suggested_fix"] = ""
        # Real formatting + absorbed into bigger neighbouring tag → pass
        elif is_real and in_clean == "merged":
            cr["classification"] = "merged_into_parent"
            cr["verdict"] = "✅ pass"
            cr["notes"] = (
                cr.get("notes", "")
                + " [LLM merged this span into a larger neighbouring tag — "
                  "no content lost]"
            ).strip()
            cr["suggested_fix"] = ""
        # Real formatting + tag has wrong boundaries → partial_match
        elif is_real and in_clean == "partial" and cls != "partial_match":
            cr["classification"] = "partial_match"
            cr["verdict"] = "❌ error"
            pymupdf_txt = _full_text.get(int(cr.get("checklist_id", 0)),
                                          cr.get("pymupdf_text", ""))
            clean_inner = _strip_tags(cr.get("clean_text", ""))
            if pymupdf_txt and clean_inner:
                fix = _word_diff_fix(pymupdf_txt, clean_inner)
                if fix:
                    cr["suggested_fix"] = fix
        # Real formatting + wrong tag type
        elif is_real and in_clean == "wrong_type":
            cr["classification"] = "wrong_tag_type"
            cr["verdict"] = "❌ error"
            want = ("[D>…<D]" if cr.get("tag_type") == "deletion"
                    else "[A>…<A]")
            cr["suggested_fix"] = (
                f"Re-tag as {want} — current tag uses the opposite type"
            )
        # Indiana-specific decorative-header tag drop (✅ EXPECTED behavior)
        # The IN clean-prompt instructs the LLM to STRIP [A>...<A] tags
        # from decorative header elements (bill title, version label,
        # digest heading, A BILL FOR AN ACT, session line, committee
        # name, section labels, decorative rules) while preserving the
        # inner text.  When the audit sees a ``text_only`` item matching
        # one of these patterns, that is the EXPECTED behavior per the
        # prompt — NOT a ``tag_dropped`` error.  This branch fires only
        # for IN addition tags (decorative styling never produces
        # strikethroughs); deletion tags fall through to the regular
        # tag_dropped branch.
        elif (
            is_real
            and in_clean == "text_only"
            and state_code
            and state_code.upper() == "IN"
            and cr.get("tag_type") == "addition"
            and _is_in_decorative_header(
                _full_text.get(int(cr.get("checklist_id", 0) or 0),
                               cr.get("pymupdf_text", "")),
                committee_names=in_committee_names,
            )
        ):
            cr["classification"] = "decorative_header_dropped"
            cr["verdict"] = "✅ pass"
            pymupdf_txt = _full_text.get(
                int(cr.get("checklist_id", 0) or 0),
                cr.get("pymupdf_text", ""),
            )
            cr["notes"] = (
                f"IN decorative header — the bold/underlined styling on "
                f"'{pymupdf_txt[:80]}' is decorative typography, NOT "
                f"legislative markup.  The IN clean-prompt instructs the "
                f"LLM to STRIP the [A>...<A] tag while preserving the "
                f"text; this is the expected behavior, not a tag-drop "
                f"error."
            )
            cr["suggested_fix"] = ""
        # Real formatting + text survived but lost its tag
        elif is_real and in_clean == "text_only":
            cr["classification"] = "tag_dropped"
            cr["verdict"] = "❌ error"
            tag_label = ("[D>…<D]" if cr.get("tag_type") == "deletion"
                         else "[A>…<A]")
            pymupdf_txt = _full_text.get(int(cr.get("checklist_id", 0)),
                                          cr.get("pymupdf_text", ""))
            cr["suggested_fix"] = (
                f"Wrap the text in {tag_label} on page {cr.get('page', '?')}: "
                f"'{pymupdf_txt[:150]}'"
            )
        # Protected clause — LLM correctly stripped the tag.
        # This wins over text_only / no, regardless of in_image, because
        # the cleaning rule is to NEVER tag these phrases.
        elif in_clean == "protected":
            cr["classification"] = "protected_clause"
            cr["verdict"] = "✅ pass"
            cr["notes"] = (
                "Protected clause for this state — must never carry "
                "[A>…<A] / [D>…<D] tags; the LLM correctly stripped it."
            )
            cr["suggested_fix"] = ""
        # Bracket-deletion convention (CT / MD / KY): the literal
        # bracketed text is preserved in the cleaned output — brackets
        # ARE the deletion marker, so this is a ✅ pass even though
        # there is no [D>…<D] wrapper.  Wins over partial / text_only.
        elif in_clean == "brackets_preserved":
            cr["classification"] = "brackets_preserved"
            cr["verdict"] = "✅ pass"
            cr["notes"] = (
                f"Bracket-deletion convention for {state_code} — the LLM "
                f"preserved the literal bracketed text in the cleaned "
                f"output.  The brackets themselves are the visible "
                f"deletion marker, so no [D>…<D] wrapper is required."
            )
            cr["suggested_fix"] = ""
        # Real formatting + everything missing
        elif is_real and in_clean == "no":
            cr["classification"] = "content_dropped"
            cr["verdict"] = "❌ error"
            pymupdf_txt = _full_text.get(int(cr.get("checklist_id", 0)),
                                          cr.get("pymupdf_text", ""))
            cr["suggested_fix"] = (
                f"Restore the dropped text and re-tag it on page "
                f"{cr.get('page', '?')}: '{pymupdf_txt[:150]}'"
            )
        # PyMuPDF false-positive header → LLM correctly cleaned it
        elif is_fp_header:
            cr["classification"] = "header_dropped"
            cr["verdict"] = "✅ pass"
            cr["suggested_fix"] = ""
        elif is_fp_table:
            cr["classification"] = "table_false_positive"
            # LLM correctly omitted a PyMuPDF table-border false positive →
            # the cleaned output is right, so pass (was ⚠️ warning).
            cr["verdict"] = "✅ pass"
            cr["suggested_fix"] = ""
        elif is_fp_other:
            cr["classification"] = "other_false_positive"
            # LLM correctly omitted a PyMuPDF false positive (e.g. CT's
            # bracketed boilerplate footer) → the cleaned output is right,
            # so pass (was ⚠️ warning).
            cr["verdict"] = "✅ pass"
            cr["suggested_fix"] = ""

    # ── Auto-link short fragments already absorbed into parent tags ──
    _claude_texts_by_page: Dict[int, List[str]] = {}
    for cr in checklist_results:
        ct = cr.get("clean_text", "") or ""
        try:
            pg = int(cr.get("page", 0) or 0)
        except (ValueError, TypeError):
            pg = 0
        if ct and pg:
            _claude_texts_by_page.setdefault(pg, []).append(ct)
    n_frag = 0
    for cr in checklist_results:
        pmtxt = (cr.get("pymupdf_text", "") or "").strip()
        if not pmtxt or len(pmtxt) > 3:
            continue
        if cr.get("classification") in _PASS_CLASSES:
            continue
        try:
            pg = int(cr.get("page", 0) or 0)
        except (ValueError, TypeError):
            pg = 0
        if pg <= 0:
            continue
        item_typ = cr.get("tag_type", "")
        for ct in _claude_texts_by_page.get(pg, []):
            if pmtxt not in ct:
                continue
            ct_is_add = "[A>" in ct
            ct_is_del = "[D>" in ct
            if item_typ == "addition" and ct_is_del and not ct_is_add:
                continue
            if item_typ == "deletion" and ct_is_add and not ct_is_del:
                continue
            cr["classification"] = "linked_fragment"
            cr["verdict"] = "✅ pass"
            cr["in_clean"] = "yes"
            cr["clean_text"] = ct[:200]
            cr["notes"] = (
                f"Short fragment '{pmtxt}' already captured within parent tag. "
                f"{cr.get('notes', '')}"
            ).strip()
            cr["suggested_fix"] = ""
            n_frag += 1
            break
    if n_frag:
        log.info("🔗 Auto-linked %d short fragment(s) into parent tags", n_frag)

    # ── Normalize verdict / classification consistency ──────────────
    for cr in checklist_results:
        cls = cr.get("classification", "")
        if cls in _ERROR_CLASSES and not cr.get("verdict", "").startswith("❌"):
            cr["verdict"] = "❌ error"
        elif cls in _WARNING_CLASSES and not cr.get("verdict", "").startswith("⚠"):
            cr["verdict"] = "⚠️ warning"
        elif cls in _PASS_CLASSES and not cr.get("verdict", "").startswith("✅"):
            cr["verdict"] = "✅ pass"

    # ── Counts for summary ──────────────────────────────────────────
    n_pass    = sum(1 for r in checklist_results if r.get("verdict", "").startswith("✅"))
    n_warn    = sum(1 for r in checklist_results if r.get("verdict", "").startswith("⚠"))
    n_error   = sum(1 for r in checklist_results if r.get("verdict", "").startswith("❌"))

    n_match       = sum(1 for r in checklist_results if r.get("classification") == "match")
    n_merged      = sum(1 for r in checklist_results if r.get("classification") == "merged_into_parent")
    n_header_drop = sum(1 for r in checklist_results if r.get("classification") == "header_dropped")
    n_decor_drop  = sum(1 for r in checklist_results if r.get("classification") == "decorative_header_dropped")
    n_protected   = sum(1 for r in checklist_results if r.get("classification") == "protected_clause")
    n_brackets    = sum(1 for r in checklist_results if r.get("classification") == "brackets_preserved")
    n_tbl_fp      = sum(1 for r in checklist_results if r.get("classification") == "table_false_positive")
    n_oth_fp      = sum(1 for r in checklist_results if r.get("classification") == "other_false_positive")
    n_tag_drop    = sum(1 for r in checklist_results if r.get("classification") == "tag_dropped")
    n_cont_drop   = sum(1 for r in checklist_results if r.get("classification") == "content_dropped")
    n_partial     = sum(1 for r in checklist_results if r.get("classification") == "partial_match")
    n_wrong_typ   = sum(1 for r in checklist_results if r.get("classification") == "wrong_tag_type")
    n_extra       = sum(1 for r in checklist_results if r.get("classification") == "extra_tag")
    n_prot_tag    = sum(1 for r in checklist_results if r.get("classification") == "protected_clause_tagged")
    n_link        = sum(1 for r in checklist_results if r.get("classification") == "linked_fragment")

    # ── Stash context / bill_line / format_types for the Excel writer ──
    for cr in checklist_results:
        try:
            cid = int(cr.get("checklist_id"))
        except (ValueError, TypeError):
            continue
        cr["_context"] = _item_ctx.get(cid, "")
        cr["_bill_line"] = _item_line.get(cid, "")
        # format_types — re-derive from the original report
        for ci in checklist_items:
            if ci["id"] == cid:
                cr["_format_types"] = ",".join(ci.get("format_types", []))
                break

    # ── Write Excel ─────────────────────────────────────────────────
    _write_excel(
        checklist_results=checklist_results,
        additional_findings=additional_findings,
        checklist_items=checklist_items,
        state_code=state_code,
        output_xlsx=output_xlsx,
    )

    return {
        "total_checklist": len(checklist_items),
        "passes": n_pass,
        "warnings": n_warn,
        "errors": n_error,
        "matches": n_match,
        "merged_into_parent": n_merged,
        "header_dropped": n_header_drop,
        "decorative_header_dropped": n_decor_drop,
        "table_false_positive": n_tbl_fp,
        "other_false_positive": n_oth_fp,
        "tag_dropped": n_tag_drop,
        "content_dropped": n_cont_drop,
        "partial_matches": n_partial,
        "wrong_tag_type": n_wrong_typ,
        "extra_tags": n_extra,
        "linked_fragments": n_link,
        "additional_findings": len(additional_findings),
        "excel_path": str(output_xlsx),
        "raw_result": result,
    }


# ════════════════════════════════════════════════════════════════════
# Excel writer
# ════════════════════════════════════════════════════════════════════

def _write_excel(
    checklist_results: List[Dict],
    additional_findings: List[Dict],
    checklist_items: List[Dict],
    state_code: str,
    output_xlsx: str,
) -> None:
    """Write a 3-sheet styled workbook:
        1) Checklist Audit   (compatible with validation_review_inline.py)
        2) Additional Findings
        3) Summary

    Column naming differs from ``gpt_compare.py`` in two places to better
    reflect what is actually being audited here:
      • "Clean Text"       — the tag found in the LLM-cleaned PyMuPDF
                              output file (this is the VALIDATION TARGET,
                              not text Claude wrote during the audit).
      • "In Clean Output?" — whether the cleaning step preserved the tag.
    The UI (validation_review_inline.py) accepts either old or new names
    via small backward-compatibility helpers.
    """
    wb = Workbook()

    # ───── Sheet 1: Checklist Audit (UI-compatible) ─────────────────
    ws1 = wb.active
    ws1.title = "Checklist Audit"

    headers = [
        "#", "Tag Type", "Formats", "Page", "Line",
        "PyMuPDF Text", "Context", "Clean Text",
        "In Image?", "In Clean Output?",
        "Verdict", "Classification", "Notes", "Suggested Fix",
    ]
    NCOLS = len(headers)

    ws1.merge_cells(start_row=1, start_column=1, end_row=1, end_column=NCOLS)
    title_cell = ws1.cell(
        row=1, column=1,
        value=f"PyMuPDF Clean Audit — {state_code} (Claude Opus 4.7)",
    )
    title_cell.font = Font(name="Calibri", bold=True, size=14, color="2F5496")
    title_cell.alignment = Alignment(horizontal="center")

    n_pass  = sum(1 for r in checklist_results if r.get("verdict", "").startswith("✅"))
    n_warn  = sum(1 for r in checklist_results if r.get("verdict", "").startswith("⚠"))
    n_error = sum(1 for r in checklist_results if r.get("verdict", "").startswith("❌"))

    ws1.merge_cells(start_row=2, start_column=1, end_row=2, end_column=NCOLS)
    ws1.cell(
        row=2, column=1,
        value=(
            f"Checklist items: {len(checklist_items)}  |  "
            f"✅ Pass: {n_pass}  |  ⚠️ Warning: {n_warn}  |  ❌ Error: {n_error}  |  "
            f"Additional findings: {len(additional_findings)}"
        ),
    ).font = Font(name="Calibri", size=11, italic=True)

    for c, h in enumerate(headers, 1):
        ws1.cell(row=4, column=c, value=h)
    _style_header(ws1, 4, NCOLS)

    r = 5
    for cr in checklist_results:
        verdict = cr.get("verdict", "")
        if verdict.startswith("✅"):
            fill = _OK_FILL
        elif verdict.startswith("⚠"):
            fill = _WARN_FILL
        elif verdict.startswith("❌"):
            fill = _MISS_FILL
        else:
            fill = _INFO_FILL

        cid = cr.get("checklist_id", 0)
        ctx_text  = cr.get("_context", "")
        bill_line = cr.get("_bill_line", "") or ""
        fmt_str   = cr.get("_format_types", "")
        full_pymupdf = cr.get("pymupdf_text", "")
        if not full_pymupdf:
            for ci in checklist_items:
                if ci["id"] == cid:
                    full_pymupdf = ci.get("text", "")
                    break

        ws1.cell(row=r, column=1,  value=cid)
        ws1.cell(row=r, column=2,  value=cr.get("tag_type", ""))
        ws1.cell(row=r, column=3,  value=fmt_str)
        ws1.cell(row=r, column=4,  value=cr.get("page", ""))
        ws1.cell(row=r, column=5,  value=bill_line)
        ws1.cell(row=r, column=6,  value=full_pymupdf)
        ws1.cell(row=r, column=7,  value=ctx_text)
        ws1.cell(row=r, column=8,  value=cr.get("clean_text", ""))
        ws1.cell(row=r, column=9,  value=cr.get("in_image", ""))
        ws1.cell(row=r, column=10, value=cr.get("in_clean", ""))
        ws1.cell(row=r, column=11, value=verdict)
        ws1.cell(row=r, column=12, value=cr.get("classification", ""))
        ws1.cell(row=r, column=13, value=cr.get("notes", ""))
        ws1.cell(row=r, column=14, value=cr.get("suggested_fix", ""))
        _style_row(ws1, r, NCOLS, fill)
        r += 1

    col_widths = [5, 10, 14, 6, 6, 55, 50, 55, 22, 14, 14, 22, 45, 50]
    for i, w in enumerate(col_widths, 1):
        ws1.column_dimensions[get_column_letter(i)].width = w
    ws1.freeze_panes = "A5"
    if r > 5:
        ws1.auto_filter.ref = f"A4:{get_column_letter(NCOLS)}{r - 1}"

    # ───── Sheet 2: Additional Findings ─────────────────────────────
    ws2 = wb.create_sheet("Additional Findings")
    h2 = ["#", "Page", "Issue Type", "Description",
          "Clean Text", "Image Text", "Suggested Fix"]
    NC2 = len(h2)

    ws2.merge_cells(start_row=1, start_column=1, end_row=1, end_column=NC2)
    ws2.cell(
        row=1, column=1,
        value="Additional Findings — Beyond the PyMuPDF Report",
    ).font = Font(name="Calibri", bold=True, size=14, color="2F5496")

    row2 = 3
    for c, h in enumerate(h2, 1):
        ws2.cell(row=row2, column=c, value=h)
    _style_header(ws2, row2, NC2)
    row2 += 1

    if additional_findings:
        for af in additional_findings:
            itype = af.get("issue_type", "")
            if itype in ("extra_tag", "content_added"):
                fill = _WARN_FILL
            elif itype in ("visible_missed", "content_lost"):
                fill = _MISS_FILL
            else:
                fill = _INFO_FILL
            ws2.cell(row=row2, column=1, value=af.get("id", ""))
            ws2.cell(row=row2, column=2, value=af.get("page", ""))
            ws2.cell(row=row2, column=3, value=itype)
            ws2.cell(row=row2, column=4, value=af.get("description", ""))
            ws2.cell(row=row2, column=5, value=af.get("clean_text", ""))
            ws2.cell(row=row2, column=6, value=af.get("image_text", ""))
            ws2.cell(row=row2, column=7, value=af.get("suggested_fix", ""))
            _style_row(ws2, row2, NC2, fill)
            row2 += 1
    else:
        ws2.cell(row=row2, column=1, value="—")
        ws2.cell(row=row2, column=3, value="No additional findings")
        ws2.cell(row=row2, column=4,
                 value="The cleaned text passed the open-ended audit")
        _style_row(ws2, row2, NC2, _OK_FILL)

    for i, w in enumerate([5, 6, 20, 55, 40, 40, 50], 1):
        ws2.column_dimensions[get_column_letter(i)].width = w
    ws2.freeze_panes = "A4"

    # ───── Sheet 3: Summary ─────────────────────────────────────────
    ws3 = wb.create_sheet("Summary")
    ws3.merge_cells("A1:D1")
    ws3.cell(
        row=1, column=1,
        value="PyMuPDF Clean Audit Summary",
    ).font = Font(name="Calibri", bold=True, size=14, color="2F5496")

    n_match       = sum(1 for r in checklist_results if r.get("classification") == "match")
    n_merged      = sum(1 for r in checklist_results if r.get("classification") == "merged_into_parent")
    n_header_drop = sum(1 for r in checklist_results if r.get("classification") == "header_dropped")
    n_decor_drop  = sum(1 for r in checklist_results if r.get("classification") == "decorative_header_dropped")
    n_protected   = sum(1 for r in checklist_results if r.get("classification") == "protected_clause")
    n_brackets    = sum(1 for r in checklist_results if r.get("classification") == "brackets_preserved")
    n_tbl_fp      = sum(1 for r in checklist_results if r.get("classification") == "table_false_positive")
    n_oth_fp      = sum(1 for r in checklist_results if r.get("classification") == "other_false_positive")
    n_tag_drop    = sum(1 for r in checklist_results if r.get("classification") == "tag_dropped")
    n_cont_drop   = sum(1 for r in checklist_results if r.get("classification") == "content_dropped")
    n_partial     = sum(1 for r in checklist_results if r.get("classification") == "partial_match")
    n_wrong_typ   = sum(1 for r in checklist_results if r.get("classification") == "wrong_tag_type")
    n_extra       = sum(1 for r in checklist_results if r.get("classification") == "extra_tag")
    n_prot_tag    = sum(1 for r in checklist_results if r.get("classification") == "protected_clause_tagged")
    n_link        = sum(1 for r in checklist_results if r.get("classification") == "linked_fragment")

    n_af_extra   = sum(1 for a in additional_findings if a.get("issue_type") == "extra_tag")
    n_af_missed  = sum(1 for a in additional_findings if a.get("issue_type") == "visible_missed")
    n_af_added   = sum(1 for a in additional_findings if a.get("issue_type") == "content_added")
    n_af_lost    = sum(1 for a in additional_findings if a.get("issue_type") == "content_lost")

    total_errors = (n_tag_drop + n_cont_drop + n_partial + n_wrong_typ
                    + n_extra + n_prot_tag + len(additional_findings))

    # Tag-accuracy calculation: % of checklist items that ended up as ✅ pass.
    # Phase-2 additional_findings are NOT in the checklist denominator, but
    # we still treat their presence as a sign of inaccuracy when deciding
    # whether to show the "100% TAG ACCURACY" badge.
    total_items = len(checklist_items) or 1   # guard divide-by-zero
    accuracy_pct = (n_pass / total_items) * 100.0
    accuracy_str = f"{accuracy_pct:.1f}%"
    is_perfect = (n_error == 0
                  and n_warn == 0
                  and len(additional_findings) == 0
                  and len(checklist_items) > 0)

    summary_rows = [
        ("", ""),
        ("PHASE 1: CHECKLIST AUDIT", ""),
        ("Total checklist items (PyMuPDF spans)", len(checklist_items)),
        ("", ""),
        ("✅ Pass — exact tag preserved", n_match),
        ("✅ Pass — span merged into larger neighbouring tag", n_merged),
        ("✅ Pass — header/footer correctly dropped", n_header_drop),
        ("✅ Pass — IN decorative header (bold styling, not markup)", n_decor_drop),
        ("✅ Pass — protected clause tag correctly stripped", n_protected),
        ("✅ Pass — bracket-deletion convention preserved (CT / MD / KY)", n_brackets),
        ("✅ Pass — short fragment linked to parent tag", n_link),
        ("⚠️ Warning — table false positive (LLM dropped — OK)", n_tbl_fp),
        ("⚠️ Warning — other false positive (LLM dropped — OK)", n_oth_fp),
        ("❌ Error — tag dropped by LLM (text kept, formatting lost)", n_tag_drop),
        ("❌ Error — content dropped by LLM (text AND tag missing)", n_cont_drop),
        ("❌ Error — partial match (boundary off)", n_partial),
        ("❌ Error — wrong tag type", n_wrong_typ),
        ("❌ Error — extra tag in cleaned text (no report span)", n_extra),
        ("❌ Error — protected clause TAGGED (LLM forgot to strip)", n_prot_tag),
        ("", ""),
        ("PHASE 2: ADDITIONAL FINDINGS", ""),
        ("Total additional findings", len(additional_findings)),
        ("  — Extra tag (no matching span)", n_af_extra),
        ("  — Visible formatting PyMuPDF missed", n_af_missed),
        ("  — Content added by LLM (hallucinated)", n_af_added),
        ("  — Content lost by over-zealous cleaning", n_af_lost),
        ("", ""),
        ("OVERALL", ""),
        ("✅ Total passes", n_pass),
        ("⚠️ Total warnings (review, no fix needed)", n_warn),
        ("❌ Total issues requiring fix", total_errors),
        ("📈 Tag accuracy (passes ÷ total checklist items)", accuracy_str),
    ]
    if is_perfect:
        summary_rows.append(("", ""))
        summary_rows.append(("🎉 100% TAG ACCURACY — no errors, no warnings", ""))

    for i, (label, val) in enumerate(summary_rows, 3):
        c1 = ws3.cell(row=i, column=1, value=label)
        c2 = ws3.cell(row=i, column=2, value=val)
        if label and val == "" and label.strip() and not label.startswith("  "):
            c1.font = Font(bold=True, size=12)
        if label.startswith("  —"):
            c1.font = Font(italic=True, size=11)
        if label.startswith("❌"):
            c1.font = Font(bold=True, color="C00000", size=11)
            c2.font = Font(bold=True, color="C00000", size=11)
        # Highlight the new tag-accuracy + perfect-score rows
        if label.startswith("📈"):
            c1.font = Font(bold=True, color="0070C0", size=11)
            c2.font = Font(bold=True, color="0070C0", size=11)
            c2.fill = _OK_FILL if is_perfect else _INFO_FILL
        if label.startswith("🎉"):
            c1.font = Font(bold=True, color="006100", size=14)
            c1.fill = _OK_FILL
            ws3.merge_cells(start_row=i, start_column=1, end_row=i, end_column=2)
            c1.alignment = Alignment(horizontal="center", vertical="center",
                                     wrap_text=True)
            ws3.row_dimensions[i].height = 28
            continue   # skip the generic wrap-alignment below
        c1.alignment = _WRAP
        c2.alignment = _WRAP

    ws3.column_dimensions["A"].width = 60
    ws3.column_dimensions["B"].width = 15

    Path(output_xlsx).parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_xlsx)
    log.info("📊 Excel saved to: %s", output_xlsx)


# ─────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    print("pymupdf_compare.py — PyMuPDF Clean-Text Audit via Claude Opus 4.7")
    print("Use as module: from pymupdf_compare import pymupdf_compare_tags")
    sys.exit(0)
