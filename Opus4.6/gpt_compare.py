"""
gpt_compare.py  —  Checklist-Driven Claude Audit via Claude Opus 4.7
=====================================================================

Two-phase validation of Claude's PDF conversion output:

  Phase 1 — CHECKLIST WALK
    PyMuPDF deterministically extracted tagged spans → numbered checklist.
    Claude Opus 4.7 checks each item against the highlighted page images and
    Claude's converted text.  Is it real?  Did Claude capture it?

  Phase 2 — OPEN-ENDED AUDIT
    Beyond the checklist, GPT scans images vs Claude for anything else:
    dropped content, added content, text accuracy, tagging errors.

Inputs:
  1. Highlighted PDF page images (ground truth)
  2. Claude's converted text (validation target)
  3. PyMuPDF tagged spans (checklist — helper only)

Output: styled Excel workbook with Checklist Audit, Additional Findings,
        and Summary sheets.

Usage:
    from gpt_compare import gpt_compare_tags
    result = gpt_compare_tags(images_b64, pymupdf_text, claude_text,
                               state_code="CT", output_xlsx="audit.xlsx")
"""

import re
import json
import logging
from difflib import SequenceMatcher
from pathlib import Path
from typing import List, Optional, Dict

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

log = logging.getLogger("gpt_compare")

# ── Styles ──────────────────────────────────────────────────────────
_HEADER_FONT = Font(name="Calibri", bold=True, color="FFFFFF", size=11)
_HEADER_FILL = PatternFill("solid", fgColor="2F5496")
_OK_FILL     = PatternFill("solid", fgColor="C6EFCE")   # green  — match / pass
_MISS_FILL   = PatternFill("solid", fgColor="FFC7CE")   # red    — error
_INFO_FILL   = PatternFill("solid", fgColor="D9E2F3")   # blue   — informational
_WARN_FILL   = PatternFill("solid", fgColor="FFD966")   # yellow — warning
_THIN_BORDER = Border(
    left=Side(style="thin"), right=Side(style="thin"),
    top=Side(style="thin"), bottom=Side(style="thin"),
)
_WRAP = Alignment(wrap_text=True, vertical="top")

# ── TR Anthropic gateway ────────────────────────────────────────────
_TR_WORKSPACE_ID       = "AgentTraininJaH4"
_TR_ANTHROPIC_TOKEN_URL = "https://aiplatform.gcs.int.thomsonreuters.com/v1/anthropic/token"


def _get_anthropic_client():
    """Obtain an Anthropic client via the TR AI Platform gateway."""
    import requests as _requests
    import anthropic

    payload = {"workspace_id": _TR_WORKSPACE_ID}
    resp = _requests.post(_TR_ANTHROPIC_TOKEN_URL, json=payload, timeout=30, verify=False)
    resp.raise_for_status()
    creds = resp.json()

    if "anthropic_api_key" in creds:
        return anthropic.Anthropic(api_key=creds["anthropic_api_key"])
    raise RuntimeError("No anthropic_api_key in gateway response")


# ════════════════════════════════════════════════════════════════════
# Build the PyMuPDF checklist from tagged text
# ════════════════════════════════════════════════════════════════════

_TAG_RE = re.compile(
    r"\[(A)>(.*?)<A\]"           # additions  [A>…<A]
    r"|\[(D)>(.*?)<D\]",         # deletions  [D>…<D]
    re.DOTALL,
)


def _build_checklist(pymupdf_text: str,
                     pymupdf_report: Optional[Dict] = None,
                     pymupdf_raw_text: Optional[str] = None) -> List[Dict]:
    """
    Extract every tagged span from PyMuPDF text and return as a numbered
    checklist.  Each item has: id, tag_type, text, approximate page, context.

    If *pymupdf_report* is provided (the JSON from extract_pdf_json_report),
    each item is enriched with a 'context' field showing surrounding text so
    GPT can pinpoint the exact location of short spans like "." or ",".

    If *pymupdf_raw_text* is provided (the original PyMuPDF text with page
    markers before LLM cleaning), it is used to build a reliable
    character-offset → page mapping.  This avoids the fragile span-matching
    approach for page assignment when the clean text has no page markers.
    """
    items = []
    # Split into pages (PAGE markers or simple heuristic)
    # PyMuPDF produces "--- Page N ---"; accept any casing.
    page_splits = re.split(r"---\s*PAGE\s+(\d+)\s*---", pymupdf_text, flags=re.IGNORECASE)

    # If no page markers, treat as single page
    if len(page_splits) == 1:
        chunks = [(1, pymupdf_text)]
    else:
        chunks = []
        # page_splits: [before, "1", text1, "2", text2, ...]
        for i in range(1, len(page_splits), 2):
            pg = int(page_splits[i])
            text = page_splits[i + 1] if i + 1 < len(page_splits) else ""
            chunks.append((pg, text))
        # Include any text before first page marker as page 1
        if page_splits[0].strip():
            chunks.insert(0, (1, page_splits[0]))

    # ── Build a page-lookup from the RAW pymupdf text (with page markers) ──
    # When the clean text has no page markers but the raw text does, we
    # build a helper that finds a tag's page by locating it in the raw text.
    _raw_page_chunks: List[tuple] = []  # (page_num, raw_chunk_text)
    if pymupdf_raw_text and len(page_splits) == 1:
        _raw_splits = re.split(
            r"---\s*PAGE\s+(\d+)\s*---", pymupdf_raw_text, flags=re.IGNORECASE
        )
        if len(_raw_splits) > 1:
            for ri in range(1, len(_raw_splits), 2):
                rpg = int(_raw_splits[ri])
                rtxt = _raw_splits[ri + 1] if ri + 1 < len(_raw_splits) else ""
                # Strip tags to get plain text for matching
                _raw_page_chunks.append((rpg, rtxt))
            if _raw_splits[0].strip():
                _raw_page_chunks.insert(0, (1, _raw_splits[0]))
            log.info("📄 Built raw-text page lookup: %d pages", len(_raw_page_chunks))

    # Track which position in each raw page chunk we've consumed,
    # so identical spans on the same page are matched in order.
    _raw_search_offsets = {pg: 0 for pg, _ in _raw_page_chunks}

    def _find_page_from_raw(tag_text: str, surrounding: str) -> Optional[int]:
        """Find the page of a tag by locating it in the raw pymupdf text.
        Uses surrounding plain text (from the clean text) to disambiguate
        identical short spans. Returns the page number or None.
        """
        if not _raw_page_chunks:
            return None

        # Get a short context snippet from surrounding text (before + after tag)
        # to help disambiguate identical tags on different pages.
        # Use 40 chars before and after the tag in the surrounding text.
        surr_lower = surrounding.lower() if surrounding else ""

        # Try each page in order, preferring forward progress
        best_page = None
        for rpg, rtxt in _raw_page_chunks:
            # Search from last consumed position on this page
            search_from = _raw_search_offsets.get(rpg, 0)
            # Strip tags from raw chunk for plain-text search
            rtxt_plain = re.sub(r'\[([AD])>(.*?)<\1\]', r'\2', rtxt, flags=re.DOTALL)
            pos = rtxt_plain.lower().find(tag_text.lower(), search_from)
            if pos >= 0:
                # Verify with surrounding context
                if surr_lower:
                    # Get surrounding text in raw chunk
                    raw_surr = rtxt_plain[max(0, pos-60):pos+len(tag_text)+60].lower()
                    raw_surr_norm = re.sub(r'\s+', ' ', raw_surr)
                    surr_norm = re.sub(r'\s+', ' ', surr_lower)
                    # Check overlap: at least 15 chars of surrounding should match
                    # Use the last 20 chars before the tag
                    before_tag = surr_norm[:surr_norm.find(tag_text.lower())] if tag_text.lower() in surr_norm else ""
                    if before_tag and len(before_tag) >= 10:
                        check_frag = before_tag[-20:].strip()
                        if check_frag and check_frag in raw_surr_norm:
                            _raw_search_offsets[rpg] = pos + len(tag_text)
                            return rpg
                    else:
                        # No good surrounding text, accept first positional match
                        _raw_search_offsets[rpg] = pos + len(tag_text)
                        return rpg
                else:
                    _raw_search_offsets[rpg] = pos + len(tag_text)
                    return rpg
                # Track as fallback if context didn't match
                if best_page is None:
                    best_page = rpg

        return best_page

    # Pre-index report spans by (page, stripped_text) for fast lookup.
    # Falls back to text-only match if page differs (LLM cleaning may shift pages).
    # Also parse surrounding text from context field for disambiguation.
    report_spans = []
    _ctx_marker_re = re.compile(r'>>>(.*?)<<<', re.DOTALL)
    if pymupdf_report:
        for span in pymupdf_report.get("formatted_spans", []):
            fmt = span.get("format_types", [])
            # Map format_types to tag_type: strikethrough → deletion, else → addition
            span_tag_type = "deletion" if "strikethrough" in fmt else "addition"
            # Parse surrounding text from context: "…before >>>span<<< after…"
            raw_ctx = span.get("context", "")
            ctx_before = ""
            ctx_after = ""
            cm = _ctx_marker_re.search(raw_ctx)
            if cm:
                # Text before >>> (strip leading ellipsis)
                ctx_before = raw_ctx[:cm.start()].strip().lstrip("…").strip()
                # Text after <<< (strip trailing ellipsis)
                ctx_after = raw_ctx[cm.end():].strip().rstrip("…").strip()
            report_spans.append({
                "text": span.get("text", "").strip(),
                "page": span.get("page"),
                "bill_line": span.get("bill_line"),
                "context": raw_ctx,
                "tag_type": span_tag_type,
                "ctx_before": ctx_before,
                "ctx_after": ctx_after,
            })
    # Track which report spans have been used (to handle duplicates)
    used_report = [False] * len(report_spans)

    # ── Quote normalisation helper ─────────────────────────────────
    # Claude LLM cleaning often normalises smart quotes (\u201c\u201d\u2018\u2019) to
    # straight ASCII quotes, but the report keeps the originals from PyMuPDF.
    _QUOTE_TABLE = str.maketrans({
        '\u201c': '"', '\u201d': '"',   # smart double → straight double
        '\u2018': "'", '\u2019': "'",   # smart single → straight single
    })

    def _norm_quotes(s: str) -> str:
        return s.translate(_QUOTE_TABLE)

    # ── Strip tags from clean text to build a plain-text index ────────
    # This lets us look up surrounding text at each tag's position.
    _plain_text = re.sub(r'\[([AD])>(.*?)<\1\]', r'\2', pymupdf_text, flags=re.DOTALL)

    def _surrounding_text(offset: int, radius: int = 60) -> str:
        """Get plain text around a character offset (for context verification)."""
        start = max(0, offset - radius)
        end = min(len(_plain_text), offset + radius)
        return _plain_text[start:end]

    def _context_score(rs: dict, tag_offset: int) -> float:
        """Score how well a report span's surrounding context matches the
        clean text around the tag's position. Returns 0.0–1.0.
        
        Uses the before/after text from the report context field and checks
        if it appears near the tag offset in the plain (tag-stripped) text.
        """
        if tag_offset < 0:
            return 0.0
        surround = _surrounding_text(tag_offset, radius=80)
        if not surround:
            return 0.0
        surround_lower = surround.lower()
        # Normalise whitespace in surrounding text for comparison
        surround_norm = re.sub(r'\s+', ' ', surround_lower)

        score = 0.0
        checks = 0

        cb = rs.get("ctx_before", "")
        ca = rs.get("ctx_after", "")

        # Check before-text (last N chars of it)
        if cb and len(cb) >= 3:
            # Use last 30 chars of before-text (closest to the span)
            cb_tail = cb[-30:].strip().lower()
            cb_tail_norm = re.sub(r'\s+', ' ', cb_tail)
            checks += 1
            if cb_tail_norm in surround_norm:
                score += 1.0
            elif len(cb_tail) >= 8:
                # Try shorter fragment (last 15 chars)
                cb_short = cb_tail[-15:]
                if cb_short in surround_norm:
                    score += 0.5

        # Check after-text (first N chars of it)
        if ca and len(ca) >= 3:
            ca_head = ca[:30].strip().lower()
            ca_head_norm = re.sub(r'\s+', ' ', ca_head)
            checks += 1
            if ca_head_norm in surround_norm:
                score += 1.0
            elif len(ca_head) >= 8:
                ca_short = ca_head[:15]
                if ca_short in surround_norm:
                    score += 0.5

        if checks == 0:
            return 0.0
        return score / checks

    # Track the index of the last matched report span so we can add a
    # monotonicity bias: when context scores tie, prefer the span whose
    # index is closest to the last match (soft preference, not hard block).
    _last_matched_idx = [0]   # mutable so nested function can update it

    def _find_context(tag_text: str, page: int, tag_type: str = "",
                      tag_offset: int = -1) -> tuple:
        """Find best matching context from report for this tagged span.
        Returns (context_str, matched_page) — matched_page is the true page
        from the PyMuPDF report, or the input page if no match.
        
        When tag_type is provided and text is short (≤3 chars), require
        matching tag_type to avoid matching a deletion comma to an
        italic/addition comma.
        
        *tag_offset* is the character position of this tag in the plain
        (tag-stripped) text.  When provided, surrounding text from the report
        context field is verified against the clean text to disambiguate
        identical short spans like commas.
        """
        # For short text, require tag_type match to avoid cross-type confusion
        _require_type = (len(tag_text) <= 3 and tag_type)

        # Normalise quotes for comparison (Claude cleaning may straighten curly quotes)
        tag_text_nq = _norm_quotes(tag_text)

        def _text_eq(span_text: str) -> bool:
            return _norm_quotes(span_text) == tag_text_nq

        def _text_contains(span_text: str) -> bool:
            st = _norm_quotes(span_text)
            return tag_text_nq in st or st in tag_text_nq

        # Pass 1: exact text + same page
        for i, rs in enumerate(report_spans):
            if used_report[i]:
                continue
            if _text_eq(rs["text"]) and rs["page"] == page:
                if _require_type and rs.get("tag_type") != tag_type:
                    continue
                used_report[i] = True
                _last_matched_idx[0] = i
                return rs["context"], rs["page"], rs.get("bill_line")

        # Pass 2: text contained in report text or vice-versa, same page
        # Skip containment for very short text (≤3 chars like ',' or '.')
        # because they match inside longer spans incorrectly
        if len(tag_text) > 3:
            for i, rs in enumerate(report_spans):
                if used_report[i]:
                    continue
                if rs["page"] == page:
                    # Require the shorter side to be ≥30% of the longer side
                    # to prevent 'a' matching inside 'Military status means any...'
                    shorter = min(len(tag_text), len(rs["text"]))
                    longer = max(len(tag_text), len(rs["text"]))
                    if shorter / longer < 0.3:
                        continue
                    if _text_contains(rs["text"]):
                        used_report[i] = True
                        _last_matched_idx[0] = i
                        return rs["context"], rs["page"], rs.get("bill_line")

        # Pass 3: exact text, any page — CONTEXT-VERIFIED
        # When tag_offset is available, use surrounding text to pick the
        # correct span among multiple identical candidates.
        candidates = []
        for i, rs in enumerate(report_spans):
            if used_report[i]:
                continue
            if _text_eq(rs["text"]):
                if _require_type and rs.get("tag_type") != tag_type:
                    continue
                ctx_sc = _context_score(rs, tag_offset) if tag_offset >= 0 else 0.0
                page_dist = abs((rs["page"] or 0) - page)
                same_page = 0 if (rs["page"] or 0) == page else 1
                index_dist = abs(i - _last_matched_idx[0])
                candidates.append((-ctx_sc, same_page, index_dist, page_dist, i, rs))
        if candidates:
            candidates.sort()
            _, _, _, _, best_i, best_rs = candidates[0]
            used_report[best_i] = True
            _last_matched_idx[0] = best_i
            return best_rs["context"], best_rs["page"], best_rs.get("bill_line")

        # Pass 4: partial match (containment), any page — CONTEXT-VERIFIED
        # Skip for very short text (≤3 chars) to avoid false containment matches
        candidates = []
        if len(tag_text) > 3:
            for i, rs in enumerate(report_spans):
                if used_report[i]:
                    continue
                # Require the shorter side to be ≥30% of the longer side
                shorter = min(len(tag_text), len(rs["text"]))
                longer = max(len(tag_text), len(rs["text"]))
                if shorter / longer < 0.3:
                    continue
                if _text_contains(rs["text"]):
                    ctx_sc = _context_score(rs, tag_offset) if tag_offset >= 0 else 0.0
                    page_dist = abs((rs["page"] or 0) - page)
                    same_page = 0 if (rs["page"] or 0) == page else 1
                    index_dist = abs(i - _last_matched_idx[0])
                    candidates.append((-ctx_sc, same_page, index_dist, page_dist, i, rs))
        if candidates:
            candidates.sort()
            _, _, _, _, best_i, best_rs = candidates[0]
            used_report[best_i] = True
            _last_matched_idx[0] = best_i
            return best_rs["context"], best_rs["page"], best_rs.get("bill_line")

        return "", page, None

    # ── Build a mapping from clean-text tag offsets to plain-text offsets ──
    # We need to know where each tag sits in the PLAIN text so _context_score
    # can look up surrounding words.
    def _compute_plain_offset(match_start: int, raw_text: str) -> int:
        """Given a regex match start in the tagged text, compute the
        corresponding offset in the tag-stripped plain text."""
        # Count how many characters of plain text appear before match_start
        # by stripping tags from text[:match_start]
        prefix = raw_text[:match_start]
        plain_prefix = re.sub(r'\[([AD])>(.*?)<\1\]', r'\2', prefix, flags=re.DOTALL)
        return len(plain_prefix)

    idx = 0
    # When there are no page markers in the clean text (len(chunks)==1),
    # page_num is a meaningless 1 for all tags.  Pass page=0 to
    # _find_context so that Pass 1/2 (same-page matching) never fire
    # and every span goes through context-verified Passes 3/4 instead
    # of greedily consuming page-1 report spans.
    _no_page_markers = (len(chunks) == 1 and len(report_spans) > 0)

    # Track cumulative offset to avoid str.find() returning the wrong
    # position when chunk text appears multiple times in pymupdf_text.
    _search_from = 0
    for page_num, chunk in chunks:
        # Compute the chunk's offset within the full pymupdf_text
        if len(chunks) > 1:
            chunk_start_in_full = pymupdf_text.find(chunk, _search_from)
            if chunk_start_in_full < 0:
                chunk_start_in_full = pymupdf_text.find(chunk)  # fallback
            _search_from = chunk_start_in_full + len(chunk)
        else:
            chunk_start_in_full = 0

        # When no page markers, use 0 so Pass 1/2 won't match
        _lookup_page = 0 if _no_page_markers else page_num

        for m in _TAG_RE.finditer(chunk):
            idx += 1
            if m.group(1):   # [A>…<A]
                tag_type = "addition"
                text = m.group(2)
            else:            # [D>…<D]
                tag_type = "deletion"
                text = m.group(4)
            stripped = text.strip()
            # Compute plain-text offset for context verification
            abs_match_start = chunk_start_in_full + m.start()
            plain_offset = _compute_plain_offset(abs_match_start, pymupdf_text)
            ctx, report_pg, bill_line = _find_context(stripped, _lookup_page, tag_type,
                                                        tag_offset=plain_offset)
            # ── Determine page number ──────────────────────────────
            # Priority:
            # 1. Raw pymupdf text page lookup (most reliable — uses
            #    page markers + surrounding text in original document)
            # 2. Report page from _find_context match
            # 3. page_num from clean text page markers (fallback)
            raw_pg = None
            if _raw_page_chunks and len(stripped) > 3:
                # Only use raw-text lookup for spans > 3 chars; very short
                # spans like "a", "," match too many places in the raw text.
                surround = _surrounding_text(plain_offset, radius=60)
                raw_pg = _find_page_from_raw(stripped, surround)
            final_page = raw_pg or report_pg or page_num
            items.append({
                "id": idx,
                "tag_type": tag_type,
                "text": stripped,
                "page": final_page,
                "bill_line": bill_line,
                "context": ctx,
            })
    return items


def _format_checklist(items: List[Dict],
                      precheck: Optional[Dict] = None) -> str:
    """Format the checklist as a numbered list for the prompt.

    When context is available from the PyMuPDF report, it is appended so
    GPT can pinpoint where short spans (like '.' or ',') sit in the text.

    When *precheck* is provided (from ``_precheck_claude``), each item
    gets a deterministic ``⟨CLAUDE CHECK⟩`` annotation so GPT cannot
    hallucinate about what is or isn't tagged in Claude's output.
    """
    lines = []
    for item in items:
        tag_label = "[A>…<A]" if item["tag_type"] == "addition" else "[D>…<D]"
        preview = item["text"][:120].replace("\n", " ↵ ")
        if len(item["text"]) > 120:
            preview += "…"
        line = f"  {item['id']:3d}. Page {item['page']}  {tag_label}  \"{preview}\""
        ctx = item.get("context", "")
        if ctx:
            line += f"  — context: {ctx}"
        # Deterministic Claude pre-check annotation
        if precheck and item["id"] in precheck:
            pc = precheck[item["id"]]
            status = pc["in_claude"]
            ct = pc.get("claude_text", "")
            if status == "yes":
                line += f"\n       ⟨CLAUDE CHECK: ✅ EXACT MATCH → {ct}⟩"
            elif status == "partial":
                line += f"\n       ⟨CLAUDE CHECK: ⚠️ PARTIAL MATCH → {ct}⟩"
            else:
                line += f"\n       ⟨CLAUDE CHECK: ❌ NOT TAGGED in Claude output⟩"
        lines.append(line)
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════
# Text normalisation helpers (used by pre-check and dedup)
# ════════════════════════════════════════════════════════════════════

def _norm(s: str) -> str:
    """Collapse whitespace and normalise quotes for comparison."""
    s = re.sub(r"\s+", " ", s.strip())
    s = s.replace("\u201c", '"').replace("\u201d", '"')   # \u201c \u201d
    s = s.replace("\u2018", "'").replace("\u2019", "'")   # \u2018 \u2019
    return s


def _norm_loose(s: str) -> str:
    """Aggressive normalisation: strip \u00a7, quotes, extra spaces."""
    s = s.replace("\u00a7", "")
    s = s.replace("\u201c", '"').replace("\u201d", '"')
    s = s.replace("\u2018", "'").replace("\u2019", "'")
    return re.sub(r"\s+", " ", s.strip())


def _token_similarity(a: str, b: str) -> float:
    """Return similarity ratio (0..1) using two tiers:
    Tier 1: Jaccard token overlap (fast, handles word drops).
      Uses BOTH min-denominator (recall) and max-denominator (precision)
      to avoid false matches when one text is much longer than the other.
    Tier 2: SequenceMatcher (character-level, handles typos/rewordings).
    Returns the higher of the two scores."""
    wa = set(a.lower().split())
    wb = set(b.lower().split())
    if not wa or not wb:
        return 0.0
    intersection = wa & wb
    shorter = min(len(wa), len(wb))
    longer  = max(len(wa), len(wb))
    recall_ratio    = len(intersection) / shorter if shorter else 0.0
    precision_ratio = len(intersection) / longer  if longer  else 0.0
    # Require high overlap relative to BOTH texts — prevents a 9-token phrase
    # from matching a 35-token phrase just because all 9 words appear in it.
    # Use geometric mean to balance recall and precision.
    token_ratio = (recall_ratio * precision_ratio) ** 0.5
    if token_ratio >= 0.85:
        return token_ratio
    # Tier 2: character-level SequenceMatcher for borderline cases
    if len(a) < 2000 and len(b) < 2000:
        seq_ratio = SequenceMatcher(None, a.lower(), b.lower()).ratio()
        return max(token_ratio, seq_ratio)
    return token_ratio


def _word_diff_fix(pymupdf_text: str, claude_text: str) -> str:
    """Generate a human-readable suggested fix showing word-level differences.
    Returns a string like: "Missing word 'as' — expected: '...premium as required...'
    but Claude wrote: '...premium required...'"
    """
    if not pymupdf_text or not claude_text:
        return ""
    # Strip tag markers from claude_text for comparison
    ct_clean = re.sub(r'\[([AD])>', '', claude_text)
    ct_clean = re.sub(r'<([AD])\]', '', ct_clean).strip()

    pw = pymupdf_text.split()
    cw = ct_clean.split()

    sm = SequenceMatcher(None, pw, cw)
    missing_in_claude: list[str] = []
    extra_in_claude: list[str] = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == 'delete':
            missing_in_claude.extend(pw[i1:i2])
        elif op == 'insert':
            extra_in_claude.extend(cw[j1:j2])
        elif op == 'replace':
            missing_in_claude.extend(pw[i1:i2])
            extra_in_claude.extend(cw[j1:j2])

    # ── Detect simple character-level diffs (e.g. "program" vs "program.") ──
    # When the only difference is added/removed punctuation or a few chars,
    # give a clearer message instead of "Missing word / Extra word".
    if len(missing_in_claude) <= 2 and len(extra_in_claude) <= 2:
        pairs = list(zip(missing_in_claude, extra_in_claude))
        char_diffs = []
        for mw, ew in pairs:
            if mw in ew:
                extra_chars = ew.replace(mw, '', 1)
                char_diffs.append(f"'{extra_chars}' should not be included after '{mw}' — Claude wrote '{ew}' but expected '{mw}'")
            elif ew in mw:
                missing_chars = mw.replace(ew, '', 1)
                char_diffs.append(f"'{missing_chars}' is missing from '{ew}' — expected '{mw}'")
        unpaired_missing = missing_in_claude[len(pairs):]
        unpaired_extra = extra_in_claude[len(pairs):]
        if char_diffs and not unpaired_missing and not unpaired_extra:
            return ". ".join(char_diffs)

    parts = []
    if missing_in_claude:
        words = "', '".join(missing_in_claude[:5])
        parts.append(f"Missing word(s) in Claude output: '{words}'")
    if extra_in_claude:
        words = "', '".join(extra_in_claude[:5])
        parts.append(f"Extra word(s) in Claude output: '{words}'")

    if not parts:
        return ""

    fix = ". ".join(parts)
    fix += f". Expected: '{pymupdf_text[:150]}'"
    return fix# ════════════════════════════════════════════════════════════════════
# Deterministic Claude pre-check
# ════════════════════════════════════════════════════════════════════

def _precheck_claude(checklist_items: List[Dict],
                     claude_text: str) -> Dict[int, Dict]:
    """
    Deterministic pre-check: extract every tagged span from Claude's
    converted text and match each PyMuPDF checklist item against them.

    Returns ``{item_id: {"in_claude": "yes"|"partial"|"no",
                         "claude_text": "<full tag or empty>"}}``

    Matching strategy (two passes to avoid order-dependence):
      Pass 1 — exact match   (normalised whitespace, same tag type)
      Pass 2 — containment   (item text ⊂ Claude tag  OR  Claude tag ⊂ item text)

    This removes any possibility of GPT hallucinating ``in_claude`` or
    ``claude_text``: the answer is a deterministic regex search.
    """
    # ── Extract all Claude tags ─────────────────────────────────
    claude_tags: List[Dict] = []
    for m in _TAG_RE.finditer(claude_text):
        if m.group(1):          # [A>…<A]
            claude_tags.append({
                "type": "addition",
                "text": m.group(2).strip(),
                "full": m.group(0),
            })
        else:                   # [D>…<D]
            claude_tags.append({
                "type": "deletion",
                "text": m.group(4).strip(),
                "full": m.group(0),
            })

    def _strip_brackets(s: str) -> str:
        """Remove outer square brackets for bracket-deletion comparison."""
        s = s.strip()
        if s.startswith("[") and s.endswith("]"):
            return s[1:-1].strip()
        return s

    results: Dict[int, Dict] = {}
    used: set = set()           # indices of Claude tags already claimed

    # ── Pass 1: exact matches (unambiguous) ─────────────────────
    for item in checklist_items:
        iid  = item["id"]
        itxt = _norm(item["text"])
        ityp = item["tag_type"]
        for j, ct in enumerate(claude_tags):
            if j not in used and ct["type"] == ityp and _norm(ct["text"]) == itxt:
                used.add(j)
                results[iid] = {"in_claude": "yes", "claude_text": ct["full"]}
                break

    # ── Pass 2: containment matches for remaining items ─────────
    # Sort unmatched items by descending text length so longer/more-specific
    # items match first, preventing short items (e.g. "c. The") from stealing
    # Claude tags that should match longer items (e.g. "c. The Department...").
    unmatched_items = [item for item in checklist_items if item["id"] not in results]
    unmatched_items.sort(key=lambda it: len(it["text"]), reverse=True)

    # Build a mapping from checklist item id → expected positional rank
    # so we can prefer Claude tags that are closest in document order.
    _item_rank = {it["id"]: i for i, it in enumerate(checklist_items)}
    _n_claude = len(claude_tags) if claude_tags else 1

    for item in unmatched_items:
        iid = item["id"]
        itxt = _norm(item["text"])
        ityp = item["tag_type"]

        # Expected Claude tag index (proportional position in Claude's output)
        expected_j = (_item_rank.get(iid, 0) / max(len(checklist_items), 1)) * _n_claude

        best_j:    Optional[int] = None
        best_score: tuple  = (-1, 0)  # (overlap, -distance) — higher is better

        for j, ct in enumerate(claude_tags):
            if j in used or ct["type"] != ityp:
                continue
            nc = _norm(ct["text"])
            # Also try bracket-stripped versions for bracket-deletion matching
            # e.g. PyMuPDF: "[thereafter,]" → "thereafter,"
            #      Claude:  "[, thereafter,]" → ", thereafter,"
            itxt_inner = _norm(_strip_brackets(item["text"]))
            nc_inner   = _norm(_strip_brackets(ct["text"]))
            overlap = 0
            if itxt in nc or itxt_inner in nc_inner:  # item inside Claude tag
                # For very short items (≤5 chars), require Claude tag to be
                # similarly short — prevents "(1)" matching a 100-word span
                # that merely starts with "(1)".
                if len(itxt) <= 5 and len(nc) > len(itxt) * 4:
                    overlap = 0  # skip — too disproportionate
                else:
                    overlap = len(itxt)
            elif nc in itxt or nc_inner in itxt_inner:  # Claude tag inside item
                overlap = len(nc)
            else:
                # Fuzzy: token overlap ≥ 85% catches single-word drops
                sim = _token_similarity(itxt, nc)
                if sim >= 0.85:
                    overlap = len(itxt)

            if overlap >= 2:
                # Tiebreaker: prefer Claude tag closest in document position
                distance = abs(j - expected_j)
                score = (overlap, -distance)
                if score > best_score:
                    best_j, best_score = j, score

        if best_j is not None:
            used.add(best_j)
            ct = claude_tags[best_j]
            # Both directions are partial — boundaries differ either way
            results[iid] = {"in_claude": "partial", "claude_text": ct["full"]}
        else:
            results[iid] = {"in_claude": "no", "claude_text": ""}

    # ── Pass 3: loose matching for remaining "no" items ─────────
    # Handles cases where only special characters (§, etc.) differ.
    for item in checklist_items:
        iid = item["id"]
        if results.get(iid, {}).get("in_claude") != "no":
            continue
        itxt_loose = _norm_loose(item["text"])
        ityp = item["tag_type"]
        for j, ct in enumerate(claude_tags):
            if j in used or ct["type"] != ityp:
                continue
            nc_loose = _norm_loose(ct["text"])
            if itxt_loose in nc_loose or nc_loose in itxt_loose:
                used.add(j)
                results[iid] = {"in_claude": "partial", "claude_text": ct["full"]}
                break

    # ── Collect extra Claude tags not matched to any checklist item ──
    extra_tags = []
    for j, ct in enumerate(claude_tags):
        if j not in used:
            extra_tags.append({
                "type": ct["type"],
                "text": ct["text"],
                "full": ct["full"],
            })

    return results, extra_tags


# ════════════════════════════════════════════════════════════════════
# GPT audit prompt (checklist-driven)
# ════════════════════════════════════════════════════════════════════

_AUDIT_PROMPT = """\
You are a quality-assurance auditor validating a Claude-generated text conversion of a legislative PDF.

══════════════════════════════════════
  GROUND TRUTH — Highlighted PDF Page Images
══════════════════════════════════════
The {n_images} highlighted images attached are the AUTHORITATIVE source of truth.
Coloured overlays mark formatting detected in the PDF:
  🟢 Green = UNDERLINED (new/added text)
  🔴 Red = STRIKETHROUGH (deleted text)
  🔵 Blue = ITALIC
  🟠 Orange = BOLD
What you SEE in these images is the truth about the document.

⚠️ STRIKETHROUGH PRIORITY RULE — CRITICAL: If text has BOTH strikethrough (red) AND any
other formatting (green/blue/orange — i.e. underline/italic/bold), strikethrough ALWAYS wins.
Tag such text as [D>…<D] ONLY, NEVER as [A>…<A]. Deletion overrides addition without exception.
Example: text that is italic+strikethrough → [D>text<D], NOT [A>text<A].

══════════════════════════════════════
  VALIDATION TARGET — Claude Converted Text
══════════════════════════════════════
Claude (visual LLM) read the PDF images and produced this tagged text:
  [A>…<A] = additions/new text (underlined in the PDF)
  [D>…<D] = deletions (struck-through or bracket-enclosed in the PDF)

YOUR JOB: Audit Claude's output for correctness against the images.

{claude_text}

══════════════════════════════════════
  CHECKLIST — PyMuPDF Tagged Spans
══════════════════════════════════════
PyMuPDF deterministically extracted these tagged spans from the PDF metadata.
Use this as a CHECKLIST of things to verify — but the IMAGES are the final authority.
PyMuPDF is known to produce false positives (table borders detected as underlines,
page headers tagged, etc.) so do NOT blindly trust it.

{n_checklist} items:
{checklist}

STATE: {state_code}
STATE RULES: {state_rules}

══════════════════════════════════════
  YOUR TASK — TWO PHASES
══════════════════════════════════════

━━━ PHASE 1: WALK THE CHECKLIST ━━━
For EACH of the {n_checklist} checklist items above, determine:

1. **in_image**: Is this formatting REAL in the image? Look at the highlighted images.
   - "yes" — genuinely underlined/struck-through text (including punctuation like periods, commas, semicolons)
   - "false_positive_header" — this is a repeated page header/footer (e.g. bill number, "File No.")
   - "false_positive_table" — table cell borders that look like underlines (effective-date tables,
     fiscal note tables, column headers like "Agency Affected", "FY 27 $")

   ⚠️ CRITICAL: If a character has green highlighting (underline) in the image, it IS underlined.
   Do NOT dismiss underlined punctuation (periods, commas, etc.) as "artifacts" or "formatting artifacts".
   Every underlined character — including "." "," ";" — is a real underline that must be tagged.

   ⚠️ HYPERLINKS: Some text may look like a hyperlink (blue/coloured text). If it ALSO has a green
   underline overlay in the highlighted image, it IS underlined and MUST be tagged with [A>…<A].
   Do NOT dismiss underlined text as "hyperlink-style emphasis" or "non-legislative formatting".
   The green highlight = underline = must be tagged. If Claude missed it, it is a real_miss ❌ error.

   ⚠️ CROSS-PAGE SPANS: Underlined text often starts on one page and continues onto the next page.
   When checking a checklist item, look at ALL pages — not just the page listed. If the underlined
   span starts on page 1 and continues on page 2, you MUST check page 2 as well to verify that
   Claude tagged the FULL extent of the underlined text across both pages. If Claude's [A>…<A] tag
   stops partway through (e.g. stops at end of page 1 but the underline continues on page 2),
   that is a partial_match ❌ error — the suggested_fix must specify the full correct text
   including the continuation on the next page.

   ⚠️ RELATED / DUPLICATE ITEMS: PyMuPDF sometimes splits one continuous underlined span into
   multiple checklist items (e.g., item #8 is "Commencing with … the General Fund." and item #3
   is just the trailing period "."). When you see this:
   - Identify which item is the MAIN SPAN (the larger text) and which is the FRAGMENT (1-3 chars).
   - For the FRAGMENT item (e.g., the standalone "."):
     • Set the appropriate classification (real_miss, partial_match, etc.)
     • Set suggested_fix to ONLY: "See item #N — include this character in that span's tag"
       where N is the checklist_id of the main span item.
     • Do NOT repeat the full span replacement text — that belongs on the main span item only.
   - For the MAIN SPAN item:
     • Give the full corrected tag text in suggested_fix (e.g., "Change [A>…Fund<A] to [A>…Fund.<A]")
   - This prevents duplicate fixes. Each issue gets ONE primary fix location.

   ⚠️ DETERMINISTIC CLAUDE CHECK: Each checklist item below has a ⟨CLAUDE CHECK⟩
   annotation that was computed by DETERMINISTIC REGEX search of Claude's output.
   These are FACTS — not guesses, not approximations. The code extracted every
   [A>…<A] and [D>…<D] tag from Claude's text and matched them against the
   checklist. You MUST use these values for the in_claude and claude_text fields:
     ⟨CLAUDE CHECK: ✅ EXACT MATCH → [A>text<A]⟩  →  in_claude="yes",  claude_text="text"
     ⟨CLAUDE CHECK: ⚠️ PARTIAL MATCH → [A>text<A]⟩ →  in_claude="partial", claude_text="text"
     ⟨CLAUDE CHECK: ❌ NOT TAGGED in Claude output⟩ →  in_claude="no",  claude_text=""
   Exception: If the item is a false positive (header/table) set in_claude="n/a", claude_text="".
   Do NOT fabricate tag text. Do NOT claim Claude has [A>word<A] when the code says NOT TAGGED.

2. **in_claude**: Did Claude capture this content with the correct tag?
   USE THE ⟨CLAUDE CHECK⟩ ANNOTATION — it is a deterministic fact.
   - "yes" — ⟨CLAUDE CHECK: ✅ EXACT MATCH⟩
   - "partial" — ⟨CLAUDE CHECK: ⚠️ PARTIAL MATCH⟩ (THIS IS AN ERROR — wrong boundaries)
   - "no" — ⟨CLAUDE CHECK: ❌ NOT TAGGED⟩
   - "n/a" — not applicable (false positive header/table, Claude was right to skip it)

3. **verdict**: Overall assessment
   - "✅ pass" — Claude correctly captured it with exact text, OR correctly skipped a false positive
   - "⚠️ warning" — false positive from PyMuPDF (header/table only), no action needed
   - "❌ error" — Claude missed content, has wrong tag boundaries, or incorrectly handled real content.
     partial_match = ❌ error (wrong boundaries must be fixed).
     content_dropped = ❌ error. real_miss = ❌ error.

4. **classification**: One of:
   - "match" — real formatting, Claude captured it correctly with exact text. ✅ PASS
   - "header" — false positive: page header/footer. Claude correctly omitted it. ⚠️ WARNING
   - "table_content" — false positive: table borders/formatting, not legislative underline. ⚠️ WARNING
   - "content_dropped" — Claude DROPPED content visible in image (ANY content: underlined text,
     plain text, punctuation, words, numbers — anything visible but missing from Claude's output). ❌ ERROR
   - "real_miss" — genuinely underlined/struck-through text in legislative body that Claude
     failed to tag. This is ONLY for real formatting in the body text. ❌ ERROR
   - "partial_match" — Claude tagged it but with WRONG text or WRONG tag boundaries
     (e.g. including extra characters like a comma, or missing characters). ❌ ERROR — must be fixed.
     The suggested_fix MUST specify exactly what the correct tag boundaries should be.

5. **claude_text**: MUST match the ⟨CLAUDE CHECK⟩ annotation.
   - If ✅ EXACT MATCH or ⚠️ PARTIAL MATCH: copy the tag text shown in the annotation.
   - If ❌ NOT TAGGED or n/a: use "" (empty string).
   Do NOT fabricate tag text that does not exist in Claude's output.

6. **notes**: Brief explanation of your reasoning

7. **suggested_fix**: Concrete fix instruction (empty string if pass/warning)
   - For content_dropped: "Add [A>text here<A] after 'context' on page X"
   - For real_miss: "Wrap 'text' in [A>…<A] tags on page X"
   - For partial_match: "Change [A>wrong text<A] to [A>correct text<A] on page X" — specify exact boundaries
   - For table_content: "No fix needed — table formatting, not legislative underline"
   - For header: "No fix needed — page header correctly omitted"

━━━ PHASE 2: OPEN-ENDED AUDIT ━━━
Now look BEYOND the checklist. Compare the images against Claude's full output and find
ANY other issues. These go in the "additional_findings" array.

Look for:
- **content_dropped** — ANY content visible in the image that Claude omitted
  (paragraphs, sentences, words, punctuation, numbers — anything missing)
- **content_added** — ANY content in Claude's output that is NOT in the image
  (hallucinated text, extra words, fabricated content)
- **tag_error** — Claude tagged something that isn't actually formatted
  (e.g. plain text wrapped in [A>…<A] when it's not underlined)
- **text_error** — Claude's text doesn't match what's in the image
  (typos, wrong words, OCR-style errors)

Each additional finding needs: page, issue_type, description, suggested_fix.

If Claude's output is perfect beyond the checklist, return an empty additional_findings array.

══════════════════════════════════════
  OUTPUT FORMAT — JSON ONLY
══════════════════════════════════════

Respond with ONLY valid JSON (no markdown fences, no commentary):

{{
  "checklist_results": [
    {{
      "checklist_id": 1,
      "tag_type": "addition",
      "pymupdf_text": "until June 30, 2025,",
      "page": 1,
      "in_image": "yes",
      "in_claude": "yes",      "claude_text": "until June 30, 2025,",      "verdict": "✅ pass",
      "classification": "match",
      "notes": "Underlined in image, correctly tagged by Claude",
      "suggested_fix": ""
    }},
    {{
      "checklist_id": 2,
      "tag_type": "addition",
      "pymupdf_text": "sHB5164\\nFile No. 21",
      "page": 2,
      "in_image": "false_positive_header",
      "in_claude": "n/a",      "claude_text": "",      "verdict": "⚠️ warning",
      "classification": "header",
      "notes": "Repeated bill number header on every page",
      "suggested_fix": "No fix needed — page header correctly omitted"
    }},
    {{
      "checklist_id": 3,
      "tag_type": "addition",
      "pymupdf_text": "Section 1 / July 1, 2026",
      "page": 3,
      "in_image": "false_positive_table",
      "in_claude": "n/a",      "claude_text": "",      "verdict": "⚠️ warning",
      "classification": "table_content",
      "notes": "Effective-date table with cell borders that look like underlines",
      "suggested_fix": "No fix needed — table formatting, not legislative underline"
    }},
    {{
      "checklist_id": 4,
      "tag_type": "addition",
      "pymupdf_text": "estimated",
      "page": 3,
      "in_image": "yes",
      "in_claude": "no",      "claude_text": "",      "verdict": "❌ error",
      "classification": "real_miss",
      "notes": "Word 'estimated' is underlined in body text, Claude missed it",
      "suggested_fix": "Add [A>estimated<A] before '$90.2 million' on page 3"
    }}
  ],
  "additional_findings": [
    {{
      "id": 1,
      "page": 2,
      "issue_type": "content_dropped",
      "description": "Image shows paragraph about fiscal impact but Claude omitted it entirely",
      "claude_text": "",
      "image_text": "The fiscal impact is estimated at...",
      "suggested_fix": "Add the missing paragraph after line 'appropriated funds' on page 2"
    }},
    {{
      "id": 2,
      "page": 1,
      "issue_type": "content_added",
      "description": "Claude has 'pursuant to section 4-28e' but image shows 'pursuant to section 4-28f'",
      "claude_text": "4-28e",
      "image_text": "4-28f",
      "suggested_fix": "Change '4-28e' to '4-28f' on page 1"
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
# Main function
# ════════════════════════════════════════════════════════════════════

def gpt_compare_tags(
    highlighted_images_b64: List[str],
    pymupdf_clean_text: str,
    claude_converted_text: str,
    state_code: str,
    state_rules: str = "",
    output_xlsx: str = "gpt_tag_comparison.xlsx",
    model: str = "claude-opus-4-7",
    pymupdf_report: Optional[Dict] = None,
    pymupdf_raw_text: Optional[str] = None,
) -> Dict:
    """
    Checklist-driven audit of Claude's output using Claude Opus 4.7 vision.

    Phase 1: Walk PyMuPDF checklist against images + Claude output.
    Phase 2: Open-ended audit for anything the checklist missed.

    *pymupdf_report*: The JSON dict from extract_pdf_json_report().
    If provided, each checklist item is enriched with surrounding-text
    context so GPT can locate short spans (".", ",") precisely.

    Returns a summary dict and writes an Excel report.
    """
    # ── Build checklist from PyMuPDF text ───────────────────────────
    checklist_items = _build_checklist(pymupdf_clean_text, pymupdf_report, pymupdf_raw_text)

    # ── Deterministic Claude pre-check ──────────────────────────────
    precheck, extra_claude_tags = _precheck_claude(checklist_items, claude_converted_text)
    n_pre_yes = sum(1 for v in precheck.values() if v["in_claude"] == "yes")
    n_pre_partial = sum(1 for v in precheck.values() if v["in_claude"] == "partial")
    n_pre_no = sum(1 for v in precheck.values() if v["in_claude"] == "no")
    log.info("🔍 Claude pre-check: %d exact, %d partial, %d not tagged, %d extra Claude tags",
             n_pre_yes, n_pre_partial, n_pre_no, len(extra_claude_tags))

    checklist_str = _format_checklist(checklist_items, precheck)
    log.info("� Built checklist: %d tagged spans from PyMuPDF", len(checklist_items))

    # ── Build prompt ────────────────────────────────────────────────
    prompt = _AUDIT_PROMPT.format(
        state_code=state_code,
        state_rules=state_rules or f"State: {state_code}",
        n_images=len(highlighted_images_b64),
        claude_text=claude_converted_text,
        n_checklist=len(checklist_items),
        checklist=checklist_str,
    )

    # ── Log full prompt for debugging (DEBUG only — too verbose for terminal) ──
    log.debug("📝 FULL GPT AUDIT PROMPT (%d chars):\n%s", len(prompt), prompt)
    log.info("📝 GPT audit prompt built: %d chars (use DEBUG level to see full prompt)", len(prompt))

    # ── Build messages with images ──────────────────────────────────
    content_parts = []

    # Add highlighted images first (ground truth)
    for i, img_b64 in enumerate(highlighted_images_b64):
        content_parts.append({
            "type": "text",
            "text": f"[Highlighted page {i+1} of {len(highlighted_images_b64)}]",
        })
        content_parts.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": img_b64,
            },
        })

    # Add the prompt text
    content_parts.append({
        "type": "text",
        "text": prompt,
    })

    # ── Call Claude Opus 4.7 ────────────────────────────────────────
    log.info("🤖 Sending %d images + checklist (%d items) to %s ...",
             len(highlighted_images_b64), len(checklist_items), model)
    client = _get_anthropic_client()
    log.info("✅ Anthropic client obtained")

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

    # ── Track tokens if tracker is available ────────────────────────
    try:
        import sys as _sys
        _main_mod = _sys.modules.get('__main__')
        if _main_mod and hasattr(_main_mod, '_track_usage'):
            _main_mod._track_usage("GPT Validation Report", final_msg.usage, model)
        else:
            raise AttributeError("no tracker")
    except Exception:
        if hasattr(final_msg, 'usage') and final_msg.usage:
            u = final_msg.usage
            log.info("💰 [GPT Validation Report] Tokens: %s in / %s out",
                     getattr(u, 'input_tokens', '?'), getattr(u, 'output_tokens', '?'))

    # ── Parse JSON ──────────────────────────────────────────────────
    if raw_response.startswith("```"):
        lines = raw_response.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        raw_response = "\n".join(lines)

    try:
        result = json.loads(raw_response)
    except json.JSONDecodeError:
        log.warning("JSON parse failed, attempting repair...")
        raw_response = _repair_json(raw_response)
        result = json.loads(raw_response)

    checklist_results = result.get("checklist_results", [])
    additional_findings = result.get("additional_findings", [])
    gpt_summary = result.get("summary", {})

    log.info("✅ GPT audit complete: %d checklist results, %d additional findings",
             len(checklist_results), len(additional_findings))

    # ── Inject deterministic extra-tag findings into checklist ──────
    # Claude tags not matched to any PyMuPDF checklist item are extras.
    # Add them as checklist error rows so they appear in the main audit table.

    if extra_claude_tags:
        log.info("🔍 Injecting %d extra Claude tags into checklist (deterministic)",
                 len(extra_claude_tags))
        max_cid = max((cr.get("checklist_id", 0) for cr in checklist_results), default=len(checklist_items))
        for i, et in enumerate(extra_claude_tags, start=max_cid + 1):
            tag_label = "[A>...<A]" if et["type"] == "addition" else "[D>...<D]"
            tag_type = "addition" if et["type"] == "addition" else "deletion"
            et_page = ""
            checklist_results.append({
                "checklist_id": i,
                "tag_type": tag_type,
                "page": et_page,
                "pymupdf_text": "",
                "claude_text": et["full"],
                "in_image": "no",
                "in_claude": "yes",
                "verdict": "❌ error",
                "classification": "extra_tag",
                "notes": (f"EXTRA TAG: Claude added {tag_label} to text that has "
                          f"no matching formatting in PyMuPDF"),
                "suggested_fix": f"Remove the {tag_label} tags from: {et['text'][:120]}",
            })
            # Also add to checklist_items so Excel context lookup works
            checklist_items.append({
                "id": i,
                "tag_type": tag_type,
                "text": et["text"],
                "page": et_page,
                "context": f"Extra tag added by Claude: {et['full'][:80]}",
            })

    # ── Build full-text lookup from checklist_items (GPT truncates) ──
    _full_pymupdf = {ci["id"]: ci["text"] for ci in checklist_items}
    _item_page = {ci["id"]: ci["page"] for ci in checklist_items}

    # ── Override page with deterministic value from checklist_items ──
    # GPT often returns page 1 for everything. The PAGE markers in
    # PyMuPDF text are ground truth.
    _page_overrides = 0
    for cr in checklist_results:
        cid = cr.get("checklist_id")
        try:
            cid = int(cid)
        except (ValueError, TypeError):
            continue
        if cid in _item_page:
            old_page = cr.get("page")
            cr["page"] = _item_page[cid]
            if old_page != _item_page[cid]:
                _page_overrides += 1
    log.info("📄 Page override: corrected %d/%d items", _page_overrides, len(checklist_results))

    # ── Override in_claude / claude_text with deterministic values ───
    # GPT can hallucinate these fields. The pre-check is ground truth.
    n_overridden = 0
    for cr in checklist_results:
        cid = cr.get("checklist_id")
        if cid not in precheck:
            continue
        pc = precheck[cid]
        # Let GPT keep "n/a" for false positives (header/table)
        if cr.get("in_claude") == "n/a":
            cr["claude_text"] = ""
            continue
        old_in = cr.get("in_claude", "")
        old_ct = cr.get("claude_text", "")
        new_in = pc["in_claude"]
        new_ct = pc["claude_text"]
        if old_in != new_in or old_ct != new_ct:
            n_overridden += 1
            log.debug("  Override #%d: in_claude %r→%r  claude_text %r→%r",
                       cid, old_in, new_in, old_ct[:40], new_ct[:40])
        cr["in_claude"] = new_in
        cr["claude_text"] = new_ct

        # If GPT said "match" but pre-check says "no" → fix verdict too
        if new_in == "no" and cr.get("classification") == "match":
            cr["classification"] = "real_miss"
            cr["verdict"] = "❌ error"
            cr["notes"] = (cr.get("notes", "") +
                           " [AUTO-CORRECTED: pre-check shows NOT TAGGED]").strip()
        # If GPT said "match" but pre-check says "partial" → fix
        elif new_in == "partial" and cr.get("classification") == "match":
            cr["classification"] = "partial_match"
            cr["verdict"] = "❌ error"
            cr["notes"] = (cr.get("notes", "") +
                           " [AUTO-CORRECTED: pre-check shows PARTIAL MATCH]").strip()
            # Generate word-diff suggested fix
            pymupdf_txt = _full_pymupdf.get(cid, cr.get("pymupdf_text", ""))
            claude_txt = cr.get("claude_text", "")
            if pymupdf_txt and claude_txt:
                diff_fix = _word_diff_fix(pymupdf_txt, claude_txt)
                if diff_fix:
                    cr["suggested_fix"] = diff_fix

    if n_overridden:
        log.info("🔧 Overrode in_claude/claude_text on %d items (deterministic pre-check)",
                 n_overridden)

    # ── Generate word-diff fix for any partial_match missing a suggested_fix ──
    for cr in checklist_results:
        if cr.get("classification") != "partial_match":
            continue
        if cr.get("suggested_fix", "").strip():
            continue
        cid = cr.get("checklist_id", 0)
        pymupdf_txt = _full_pymupdf.get(cid, cr.get("pymupdf_text", ""))
        claude_txt = cr.get("claude_text", "")
        if pymupdf_txt and claude_txt:
            diff_fix = _word_diff_fix(pymupdf_txt, claude_txt)
            if diff_fix:
                cr["suggested_fix"] = diff_fix

    # ── Deduplicate real_miss ↔ extra_tag with overlapping text ─────
    # When PyMuPDF and Claude segment the same content differently,
    # the same text can appear as a real_miss (PyMuPDF span not in Claude)
    # AND an extra_tag (Claude tag not in PyMuPDF).  Cross-reference and
    # merge them into a single "segmentation_diff" warning instead.
    miss_indices  = [i for i, cr in enumerate(checklist_results)
                     if cr.get("classification") == "real_miss"]
    extra_indices = [i for i, cr in enumerate(checklist_results)
                     if cr.get("classification") == "extra_tag"]
    paired_extras: set[int] = set()
    n_deduped = 0
    for mi in miss_indices:
        miss_row = checklist_results[mi]
        miss_txt = _norm(miss_row.get("pymupdf_text", "") or miss_row.get("claude_text", ""))
        if not miss_txt or len(miss_txt) < 3:
            continue
        for ei in extra_indices:
            if ei in paired_extras:
                continue
            extra_row = checklist_results[ei]
            extra_txt = _norm(extra_row.get("claude_text", ""))
            if not extra_txt or len(extra_txt) < 3:
                continue
            # Check overlap: substring containment, token overlap, or char-level similarity
            miss_words = set(miss_txt.lower().split())
            extra_words = set(extra_txt.lower().split())
            shorter = min(len(miss_words), len(extra_words))
            token_ratio = len(miss_words & extra_words) / shorter if shorter else 0.0
            seq_ratio = 0.0
            if 0.70 <= token_ratio < 0.85 and len(miss_txt) < 2000 and len(extra_txt) < 2000:
                seq_ratio = SequenceMatcher(None, miss_txt.lower(), extra_txt.lower()).ratio()
            best_ratio = max(token_ratio, seq_ratio)
            if miss_txt in extra_txt or extra_txt in miss_txt or best_ratio >= 0.85:
                # Merge: upgrade miss to segmentation_diff (warning), remove extra
                miss_row["classification"] = "segmentation_diff"
                miss_row["verdict"] = "⚠️ warning"
                miss_row["claude_text"] = extra_row.get("claude_text", "")
                miss_row["notes"] = (
                    f"Segmentation difference: PyMuPDF and Claude split "
                    f"the same text at different boundaries"
                )
                miss_row["suggested_fix"] = ""
                paired_extras.add(ei)
                n_deduped += 1
                break
    # Remove the paired extra_tag rows (iterate in reverse to preserve indices)
    for ei in sorted(paired_extras, reverse=True):
        checklist_results.pop(ei)
    if n_deduped:
        log.info("🔗 Merged %d real_miss + extra_tag pairs into segmentation_diff warnings",
                 n_deduped)

    # ── Deduplicate partial_match ↔ extra_tag ───────────────────────
    # When a partial_match's PyMuPDF text contains the extra_tag text (i.e.
    # the extra tag is the "missing portion" of the partial), absorb the
    # extra tag into the partial_match row and note the wrong tag type.
    partial_indices = [i for i, cr in enumerate(checklist_results)
                       if cr.get("classification") == "partial_match"]
    extra_indices2 = [i for i, cr in enumerate(checklist_results)
                      if cr.get("classification") == "extra_tag"]
    paired_extras2: set[int] = set()
    n_absorbed = 0
    for pi in partial_indices:
        p_row = checklist_results[pi]
        p_pymupdf = _norm(p_row.get("pymupdf_text", ""))
        p_claude = _norm(re.sub(r'\[([AD])>', '',
                     re.sub(r'<([AD])\]', '', p_row.get("claude_text", ""))).strip())
        if not p_pymupdf or len(p_pymupdf) < 5:
            continue
        for ei in extra_indices2:
            if ei in paired_extras2:
                continue
            e_row = checklist_results[ei]
            e_txt = _norm(re.sub(r'\[([AD])>', '',
                      re.sub(r'<([AD])\]', '', e_row.get("claude_text", ""))).strip())
            if not e_txt or len(e_txt) < 3:
                continue
            # Check: is the extra tag text part of the PyMuPDF expected text?
            if e_txt in p_pymupdf or _token_similarity(e_txt, p_pymupdf) >= 0.50:
                # Determine if tag type mismatch (e.g. A vs D — strikethrough priority violation)
                p_tag_type = p_row.get("tag_type", "")
                e_tag_type = e_row.get("tag_type", "")
                tag_mismatch = (p_tag_type != e_tag_type)
                # Absorb: add note to partial_match, remove extra
                extra_note = (
                    f" | Absorbed extra tag: Claude also tagged '{e_txt[:80]}' "
                    f"but used wrong tag type [{e_tag_type[0].upper() if e_tag_type else '?'}>"
                    f"…<{e_tag_type[0].upper() if e_tag_type else '?'}] "
                    f"instead of [{p_tag_type[0].upper() if p_tag_type else '?'}>"
                    f"…<{p_tag_type[0].upper() if p_tag_type else '?'}]"
                ) if tag_mismatch else (
                    f" | Absorbed extra tag: '{e_txt[:80]}' "
                    f"(same text, different segmentation)"
                )
                p_row["notes"] = (p_row.get("notes", "") + extra_note).strip()
                # Update suggested fix to mention the full expected text
                if tag_mismatch:
                    tag_label = "[D>...<D]" if p_tag_type == "deletion" else "[A>...<A]"
                    p_row["suggested_fix"] = (
                        f"Tag the full text as {tag_label}: '{p_pymupdf[:150]}'. "
                        f"Claude incorrectly split it and used wrong tag type "
                        f"for '{e_txt[:80]}'"
                    )
                paired_extras2.add(ei)
                n_absorbed += 1
                break
    for ei in sorted(paired_extras2, reverse=True):
        checklist_results.pop(ei)
    if n_absorbed:
        log.info("🔗 Absorbed %d extra_tag(s) into partial_match items", n_absorbed)

    # ── Auto-resolve short fragments already captured in a parent span ──
    # PyMuPDF sometimes splits fractions (e.g. "1/2") or short tokens into
    # separate checklist items ("1", "/", "2").  If a short item (≤3 chars)
    # is already contained within a Claude-tagged span on the same page,
    # it is not a real miss or table_content — reclassify as pass.
    # Build a lookup of Claude-tagged texts per page from passing/partial items.
    _claude_texts_by_page: dict[int, list[str]] = {}
    for cr in checklist_results:
        ct = cr.get("claude_text", "") or ""
        pg = int(cr.get("page", 0) or 0)
        if ct and pg:
            _claude_texts_by_page.setdefault(pg, []).append(ct)
    n_frag_resolved = 0
    for cr in checklist_results:
        pymupdf_txt = (cr.get("pymupdf_text", "") or "").strip()
        if len(pymupdf_txt) > 3 or len(pymupdf_txt) == 0:
            continue
        cls = cr.get("classification", "")
        if cls in ("match", "linked_fragment", "segmentation_diff"):
            continue  # already resolved
        pg = int(cr.get("page", 0) or 0)
        if pg <= 0:
            continue
        # Check if this short text is contained in any Claude span on same page
        # Only match if the Claude span is similarly short — prevents "(4)"
        # matching a 100-word span that merely starts with "(4)".
        # Also verify tag type matches — addition must be in [A>…<A], deletion in [D>…<D].
        item_tag_type = (cr.get("tag_type", "") or "").lower()
        for ct in _claude_texts_by_page.get(pg, []):
            if pymupdf_txt in ct and len(ct) <= len(pymupdf_txt) * 6:
                # Tag-type guard: reject if item is addition but Claude span is deletion, or vice versa
                ct_is_add = ct.startswith("[A>") or "[A>" in ct
                ct_is_del = ct.startswith("[D>") or "[D>" in ct
                if item_tag_type == "addition" and ct_is_del and not ct_is_add:
                    continue
                if item_tag_type == "deletion" and ct_is_add and not ct_is_del:
                    continue
                cid = cr.get("checklist_id", 0)
                cr["classification"] = "linked_fragment"
                cr["verdict"] = "✅ pass"
                cr["in_claude"] = "yes"
                cr["claude_text"] = ct[:200]
                cr["notes"] = (
                    f"Short fragment '{pymupdf_txt}' already captured within "
                    f"parent Claude span. {cr.get('notes', '')}"
                ).strip()
                cr["suggested_fix"] = ""
                n_frag_resolved += 1
                log.debug("  Resolved short fragment #%d '%s' → already in Claude span",
                          cid, pymupdf_txt)
                break
    if n_frag_resolved:
        log.info("🔗 Auto-resolved %d short fragment(s) already captured in Claude spans",
                 n_frag_resolved)

    # ── Auto-link "See item #N" fragment items ──────────────────────
    # When GPT marks a fragment (e.g. a standalone ".") with suggested_fix
    # "See item #N — include this character in that span's tag", the fix
    # is tracked on the parent item.  Convert these fragments from errors
    # to passes so they don't require separate user action.
    _see_item_re = re.compile(r"See item #(\d+)", re.IGNORECASE)
    n_linked = 0
    # Build page lookup for validation
    _cr_page_lookup = {}
    for cr in checklist_results:
        _cr_page_lookup[cr.get("checklist_id", 0)] = cr.get("page", 0)
    for cr in checklist_results:
        if not cr.get("verdict", "").startswith("❌"):
            continue
        fix_text = cr.get("suggested_fix", "") or ""
        m = _see_item_re.search(fix_text)
        if m:
            parent_id = int(m.group(1))
            cid = cr.get("checklist_id", 0)
            # Validate: only link if parent item is on same or adjacent page
            child_page = int(cr.get("page", 0) or 0)
            parent_page = int(_cr_page_lookup.get(parent_id, 0) or 0)
            if child_page > 0 and parent_page > 0 and abs(child_page - parent_page) > 1:
                # Wrong linkage — pages too far apart, don't auto-link
                log.debug("  Skipping link #%d → #%d: pages %d vs %d too far apart",
                          cid, parent_id, child_page, parent_page)
                continue
            cr["classification"] = "linked_fragment"
            cr["verdict"] = "✅ pass"
            cr["notes"] = (
                f"Fragment linked to item #{parent_id} — "
                f"fix is tracked there. {cr.get('notes', '')}"
            ).strip()
            n_linked += 1
            log.debug("  Linked fragment #%d → parent #%d", cid, parent_id)
    if n_linked:
        log.info("🔗 Auto-linked %d fragment item(s) to their parent items (no separate fix needed)",
                 n_linked)

    # ── Normalize verdicts based on classification ────────────────────
    # Ensure error classifications always have ❌ error verdict,
    # even if GPT returned ⚠️ warning for them.
    _ERROR_CLASSIFICATIONS = {"partial_match", "real_miss", "content_dropped", "extra_tag"}
    _WARNING_CLASSIFICATIONS = {"header", "table_content", "segmentation_diff"}
    n_verdict_fixed = 0
    for cr in checklist_results:
        cls = cr.get("classification", "")
        verdict = cr.get("verdict", "")
        if cls in _ERROR_CLASSIFICATIONS and not verdict.startswith("❌"):
            cr["verdict"] = "❌ error"
            cr["notes"] = (cr.get("notes", "") +
                           f" [AUTO-CORRECTED: {cls} must be ❌ error]").strip()
            n_verdict_fixed += 1
        elif cls in _WARNING_CLASSIFICATIONS:
            # Special case: if it's table_content/header BUT Claude actually
            # tagged it, that's an error — the tag needs to be removed.
            in_claude = cr.get("in_claude", "")
            claude_text = (cr.get("claude_text", "") or "").strip()
            if in_claude in ("yes", "partial") or claude_text:
                cr["verdict"] = "❌ error"
                cr["classification"] = "extra_tag"
                tag_label = "[A>...<A]" if cr.get("tag_type") == "addition" else "[D>...<D]"
                cr["suggested_fix"] = (
                    f"Remove the {tag_label} tags from: {claude_text[:150]}. "
                    f"This is {cls} (table/header formatting), not legislative markup."
                )
                cr["notes"] = (cr.get("notes", "") +
                               f" [AUTO-CORRECTED: {cls} but Claude tagged it → extra_tag error]").strip()
                n_verdict_fixed += 1
            elif not verdict.startswith("⚠"):
                cr["verdict"] = "⚠️ warning"
                cr["notes"] = (cr.get("notes", "") +
                               f" [AUTO-CORRECTED: {cls} is ⚠️ warning]").strip()
                n_verdict_fixed += 1
    if n_verdict_fixed:
        log.info("🔧 Auto-corrected %d mismatched verdict↔classification pairs", n_verdict_fixed)

    # ── Compute counts ──────────────────────────────────────────────
    n_pass = sum(1 for r in checklist_results if r.get("verdict", "").startswith("✅"))
    n_warn = sum(1 for r in checklist_results if r.get("verdict", "").startswith("⚠️"))
    n_error = sum(1 for r in checklist_results if r.get("verdict", "").startswith("❌"))

    n_match = sum(1 for r in checklist_results if r.get("classification") == "match")
    n_header = sum(1 for r in checklist_results if r.get("classification") == "header")
    n_table = sum(1 for r in checklist_results if r.get("classification") == "table_content")
    n_dropped = sum(1 for r in checklist_results if r.get("classification") == "content_dropped")
    n_real_miss = sum(1 for r in checklist_results if r.get("classification") == "real_miss")
    n_partial = sum(1 for r in checklist_results if r.get("classification") == "partial_match")
    n_extra_tag = sum(1 for r in checklist_results if r.get("classification") == "extra_tag")
    n_seg_diff = sum(1 for r in checklist_results if r.get("classification") == "segmentation_diff")

    # ── Build Excel ─────────────────────────────────────────────────
    _write_excel(checklist_results, additional_findings, checklist_items,
                 state_code, output_xlsx)

    # ── Return summary ──────────────────────────────────────────────
    return {
        "total_checklist": len(checklist_items),
        "passes": n_pass,
        "warnings": n_warn,
        "errors": n_error,
        "matches": n_match,
        "headers": n_header,
        "table_content": n_table,
        "content_dropped": n_dropped,
        "real_misses": n_real_miss,
        "partial_matches": n_partial,
        "extra_tags": n_extra_tag,
        "segmentation_diffs": n_seg_diff,
        "additional_findings": len(additional_findings),
        "excel_path": str(output_xlsx),
        "raw_result": result,
    }


# ════════════════════════════════════════════════════════════════════
# Excel writer
# ════════════════════════════════════════════════════════════════════

def _write_excel(checklist_results, additional_findings, checklist_items,
                 state_code, output_xlsx):
    """Write the checklist audit + additional findings to a styled Excel."""
    wb = Workbook()

    # ──── Sheet 1: Checklist Audit ──────────────────────────────────
    ws1 = wb.active
    ws1.title = "Checklist Audit"

    headers = [
        "#", "Tag Type", "Page", "Line", "PyMuPDF Text", "Context",
        "Claude Text", "In Image?", "In Claude?",
        "Verdict", "Classification", "Notes", "Suggested Fix",
    ]
    NCOLS = len(headers)

    # Title
    ws1.merge_cells(start_row=1, start_column=1, end_row=1, end_column=NCOLS)
    title_cell = ws1.cell(
        row=1, column=1,
        value=f"GPT 5.4 Checklist Audit — {state_code}",
    )
    title_cell.font = Font(name="Calibri", bold=True, size=14, color="2F5496")
    title_cell.alignment = Alignment(horizontal="center")

    # Subtitle — counts
    n_pass = sum(1 for r in checklist_results if r.get("verdict", "").startswith("✅"))
    n_warn = sum(1 for r in checklist_results if r.get("verdict", "").startswith("⚠️"))
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

    # Headers
    for c, h in enumerate(headers, 1):
        ws1.cell(row=4, column=c, value=h)
    _style_header(ws1, 4, NCOLS)

    # Data rows
    r = 5
    for cr in checklist_results:
        verdict = cr.get("verdict", "")
        classification = cr.get("classification", "")

        # Choose fill
        if verdict.startswith("✅"):
            fill = _OK_FILL
        elif verdict.startswith("⚠️"):
            fill = _WARN_FILL
        elif verdict.startswith("❌"):
            fill = _MISS_FILL
        else:
            fill = _INFO_FILL

        # Look up context and bill_line from original checklist_items by checklist_id
        cid = cr.get("checklist_id", 0)
        ctx_text = ""
        bill_line = ""
        full_pymupdf_text = cr.get("pymupdf_text", "")
        for ci in checklist_items:
            if ci["id"] == cid:
                ctx_text = ci.get("context", "")
                bill_line = ci.get("bill_line", "") or ""
                # Use full text from checklist item, not GPT's truncated echo
                full_pymupdf_text = ci.get("text", full_pymupdf_text)
                break

        ws1.cell(row=r, column=1,  value=cid)
        ws1.cell(row=r, column=2,  value=cr.get("tag_type", ""))
        ws1.cell(row=r, column=3,  value=cr.get("page", ""))
        ws1.cell(row=r, column=4,  value=bill_line)
        ws1.cell(row=r, column=5,  value=full_pymupdf_text)
        ws1.cell(row=r, column=6,  value=ctx_text)
        ws1.cell(row=r, column=7,  value=cr.get("claude_text", ""))
        ws1.cell(row=r, column=8,  value=cr.get("in_image", ""))
        ws1.cell(row=r, column=9,  value=cr.get("in_claude", ""))
        ws1.cell(row=r, column=10, value=verdict)
        ws1.cell(row=r, column=11, value=classification)
        ws1.cell(row=r, column=12, value=cr.get("notes", ""))
        ws1.cell(row=r, column=13, value=cr.get("suggested_fix", ""))
        _style_row(ws1, r, NCOLS, fill)
        r += 1

    col_widths = [5, 10, 6, 6, 55, 50, 55, 22, 14, 14, 18, 45, 50]
    for i, w in enumerate(col_widths, 1):
        ws1.column_dimensions[get_column_letter(i)].width = w
    ws1.freeze_panes = "A5"
    if r > 5:
        ws1.auto_filter.ref = f"A4:{get_column_letter(NCOLS)}{r-1}"

    # ──── Sheet 2: Additional Findings ──────────────────────────────
    ws2 = wb.create_sheet("Additional Findings")
    h2 = ["#", "Page", "Issue Type", "Description",
          "Claude Text", "Image Text", "Suggested Fix"]
    NC2 = len(h2)

    ws2.merge_cells(start_row=1, start_column=1, end_row=1, end_column=NC2)
    ws2.cell(
        row=1, column=1,
        value="Additional Findings — Beyond the Checklist",
    ).font = Font(name="Calibri", bold=True, size=14, color="2F5496")

    row2 = 3
    for c, h in enumerate(h2, 1):
        ws2.cell(row=row2, column=c, value=h)
    _style_header(ws2, row2, NC2)
    row2 += 1

    if additional_findings:
        for af in additional_findings:
            issue_type = af.get("issue_type", "")
            if issue_type in ("content_dropped", "tag_error", "text_error"):
                fill = _MISS_FILL
            elif issue_type == "content_added":
                fill = _WARN_FILL
            else:
                fill = _INFO_FILL

            ws2.cell(row=row2, column=1, value=af.get("id", ""))
            ws2.cell(row=row2, column=2, value=af.get("page", ""))
            ws2.cell(row=row2, column=3, value=issue_type)
            ws2.cell(row=row2, column=4, value=af.get("description", ""))
            ws2.cell(row=row2, column=5, value=af.get("claude_text", ""))
            ws2.cell(row=row2, column=6, value=af.get("image_text", ""))
            ws2.cell(row=row2, column=7, value=af.get("suggested_fix", ""))
            _style_row(ws2, row2, NC2, fill)
            row2 += 1
    else:
        ws2.cell(row=row2, column=1, value="—")
        ws2.cell(row=row2, column=2, value="")
        ws2.cell(row=row2, column=3, value="No additional findings")
        ws2.cell(row=row2, column=4, value="Claude's output passed the open-ended audit")
        _style_row(ws2, row2, NC2, _OK_FILL)

    for i, w in enumerate([5, 6, 18, 55, 40, 40, 50], 1):
        ws2.column_dimensions[get_column_letter(i)].width = w
    ws2.freeze_panes = "A4"

    # ──── Sheet 3: Summary ──────────────────────────────────────────
    ws3 = wb.create_sheet("Summary")
    ws3.merge_cells("A1:D1")
    ws3.cell(row=1, column=1,
             value="GPT 5.4 Checklist Audit Summary").font = Font(
        name="Calibri", bold=True, size=14, color="2F5496")

    n_match = sum(1 for r in checklist_results if r.get("classification") == "match")
    n_header = sum(1 for r in checklist_results if r.get("classification") == "header")
    n_table = sum(1 for r in checklist_results if r.get("classification") == "table_content")
    n_dropped = sum(1 for r in checklist_results if r.get("classification") == "content_dropped")
    n_real_miss = sum(1 for r in checklist_results if r.get("classification") == "real_miss")
    n_partial = sum(1 for r in checklist_results if r.get("classification") == "partial_match")
    n_extra_tag = sum(1 for r in checklist_results if r.get("classification") == "extra_tag")
    n_seg_diff = sum(1 for r in checklist_results if r.get("classification") == "segmentation_diff")

    n_af_dropped = sum(1 for a in additional_findings if a.get("issue_type") == "content_dropped")
    n_af_added = sum(1 for a in additional_findings if a.get("issue_type") == "content_added")
    n_af_tag_err = sum(1 for a in additional_findings if a.get("issue_type") == "tag_error")
    n_af_text_err = sum(1 for a in additional_findings if a.get("issue_type") == "text_error")

    total_errors = n_dropped + n_real_miss + n_partial + n_extra_tag + len(additional_findings)

    summary_rows = [
        ("", ""),
        ("PHASE 1: CHECKLIST AUDIT", ""),
        ("Total checklist items (PyMuPDF tags)", len(checklist_items)),
        ("", ""),
        ("✅ Pass — matches", n_match),
        ("⚠️ Warning — page headers (false positive)", n_header),
        ("⚠️ Warning — table content (false positive)", n_table),
        ("❌ Error — partial match (wrong tag boundaries)", n_partial),
        ("❌ Error — content dropped by Claude", n_dropped),
        ("❌ Error — real miss (untagged formatting)", n_real_miss),
        ("❌ Error — extra tag (Claude tagged plain text)", n_extra_tag),
        ("⚠️ Warning — segmentation diff (text split differently)", n_seg_diff),
        ("", ""),
        ("PHASE 2: ADDITIONAL FINDINGS", ""),
        ("Total additional findings", len(additional_findings)),
        ("  — Content dropped", n_af_dropped),
        ("  — Content added", n_af_added),
        ("  — Tag errors", n_af_tag_err),
        ("  — Text errors", n_af_text_err),
        ("", ""),
        ("OVERALL VERDICT", ""),
        ("⚠️ Total warnings (review, no fix needed)", n_warn),
        ("❌ Total issues requiring fix", total_errors),
    ]

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
        c1.alignment = _WRAP
        c2.alignment = _WRAP

    ws3.column_dimensions["A"].width = 50
    ws3.column_dimensions["B"].width = 15

    # ── Save ────────────────────────────────────────────────────────
    Path(output_xlsx).parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_xlsx)
    log.info("📊 Excel saved to: %s", output_xlsx)


# ── Helpers ─────────────────────────────────────────────────────────

def _style_header(ws, row, ncols):
    for c in range(1, ncols + 1):
        cell = ws.cell(row=row, column=c)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center",
                                   wrap_text=True)
        cell.border = _THIN_BORDER


def _style_row(ws, row, ncols, fill=None):
    for c in range(1, ncols + 1):
        cell = ws.cell(row=row, column=c)
        cell.alignment = _WRAP
        cell.border = _THIN_BORDER
        if fill:
            cell.fill = fill


def _repair_json(raw: str) -> str:
    """Attempt to fix truncated JSON from GPT.
    
    Handles deep truncation by rebuilding the closing brackets/braces
    based on what's actually open in the JSON string.
    """
    # Quick check — maybe it's already valid
    try:
        json.loads(raw)
        return raw
    except json.JSONDecodeError:
        pass

    # Strategy 1: try common simple suffixes first
    for suffix in ["", "]}", "]}}", "]}}",
                   '"]}', '"]}}', '"}]}}',
                   '"}]}', '"]}]}', '"}]}}'  ]:
        try:
            json.loads(raw + suffix)
            log.info("JSON repaired with simple suffix: %r", suffix)
            return raw + suffix
        except json.JSONDecodeError:
            continue

    # Strategy 2: structural repair — close all open brackets/braces
    # Strip trailing partial tokens (incomplete string, trailing comma)
    trimmed = raw.rstrip()
    # If we're inside an unterminated string, close it
    in_string = False
    escape = False
    for ch in trimmed:
        if escape:
            escape = False
            continue
        if ch == '\\':
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
    
    if in_string:
        trimmed += '"'

    # Remove any trailing comma
    trimmed = trimmed.rstrip().rstrip(',')

    # Count unclosed brackets/braces
    stack = []
    in_str = False
    esc = False
    for ch in trimmed:
        if esc:
            esc = False
            continue
        if ch == '\\':
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in ('{', '['):
            stack.append(ch)
        elif ch == '}' and stack and stack[-1] == '{':
            stack.pop()
        elif ch == ']' and stack and stack[-1] == '[':
            stack.pop()

    # Build closing sequence
    closing = ''.join(']' if c == '[' else '}' for c in reversed(stack))
    repaired = trimmed + closing

    try:
        json.loads(repaired)
        log.info("JSON repaired with structural close (%d brackets added)", len(closing))
        return repaired
    except json.JSONDecodeError as e:
        log.warning("Structural JSON repair also failed: %s", e)
        return raw


# ── CLI entry point ─────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    print("gpt_compare.py — Checklist-Driven Claude Audit via GPT 5.4")
    print("Use as module: from gpt_compare import gpt_compare_tags")
    sys.exit(0)
