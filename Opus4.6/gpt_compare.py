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
                     pymupdf_report: Optional[Dict] = None) -> List[Dict]:
    """
    Extract every tagged span from PyMuPDF text and return as a numbered
    checklist.  Each item has: id, tag_type, text, approximate page, context.

    If *pymupdf_report* is provided (the JSON from extract_pdf_json_report),
    each item is enriched with a 'context' field showing surrounding text so
    GPT can pinpoint the exact location of short spans like "." or ",".
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

    # Pre-index report spans by (page, stripped_text) for fast lookup.
    # Falls back to text-only match if page differs (LLM cleaning may shift pages).
    report_spans = []
    if pymupdf_report:
        for span in pymupdf_report.get("formatted_spans", []):
            report_spans.append({
                "text": span.get("text", "").strip(),
                "page": span.get("page"),
                "context": span.get("context", ""),
            })
    # Track which report spans have been used (to handle duplicates)
    used_report = [False] * len(report_spans)

    def _find_context(tag_text: str, page: int) -> tuple:
        """Find best matching context from report for this tagged span.
        Returns (context_str, matched_page) — matched_page is the true page
        from the PyMuPDF report, or the input page if no match."""
        # Pass 1: exact text + same page (unused)
        for i, rs in enumerate(report_spans):
            if not used_report[i] and rs["text"] == tag_text and rs["page"] == page:
                used_report[i] = True
                return rs["context"], rs["page"]
        # Pass 2: text contained in report text or vice-versa, same page
        for i, rs in enumerate(report_spans):
            if not used_report[i] and rs["page"] == page:
                if tag_text in rs["text"] or rs["text"] in tag_text:
                    used_report[i] = True
                    return rs["context"], rs["page"]
        # Pass 3: exact text, any page (report page is authoritative)
        for i, rs in enumerate(report_spans):
            if not used_report[i] and rs["text"] == tag_text:
                used_report[i] = True
                return rs["context"], rs["page"]
        # Pass 4: partial match, any page
        for i, rs in enumerate(report_spans):
            if not used_report[i]:
                if tag_text in rs["text"] or rs["text"] in tag_text:
                    used_report[i] = True
                    return rs["context"], rs["page"]
        return "", page

    idx = 0
    for page_num, chunk in chunks:
        for m in _TAG_RE.finditer(chunk):
            idx += 1
            if m.group(1):   # [A>…<A]
                tag_type = "addition"
                text = m.group(2)
            else:            # [D>…<D]
                tag_type = "deletion"
                text = m.group(4)
            stripped = text.strip()
            ctx, report_pg = _find_context(stripped, page_num)
            items.append({
                "id": idx,
                "tag_type": tag_type,
                "text": stripped,
                "page": report_pg,
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

    def _norm(s: str) -> str:
        """Collapse whitespace for comparison."""
        return re.sub(r"\s+", " ", s.strip())

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
    for item in checklist_items:
        iid = item["id"]
        if iid in results:
            continue
        itxt = _norm(item["text"])
        ityp = item["tag_type"]

        best_j:    Optional[int] = None
        best_overlap: int  = 0
        best_contained: bool = False     # True  → item text ⊂ Claude tag

        for j, ct in enumerate(claude_tags):
            if j in used or ct["type"] != ityp:
                continue
            nc = _norm(ct["text"])
            # Also try bracket-stripped versions for bracket-deletion matching
            # e.g. PyMuPDF: "[thereafter,]" → "thereafter,"
            #      Claude:  "[, thereafter,]" → ", thereafter,"
            itxt_inner = _norm(_strip_brackets(item["text"]))
            nc_inner   = _norm(_strip_brackets(ct["text"]))
            if itxt in nc or itxt_inner in nc_inner:  # item inside Claude tag
                overlap = len(itxt)
                if overlap > best_overlap:
                    best_j, best_overlap, best_contained = j, overlap, True
            elif nc in itxt or nc_inner in itxt_inner:  # Claude tag inside item
                overlap = len(nc)
                if overlap > best_overlap:
                    best_j, best_overlap, best_contained = j, overlap, False

        if best_j is not None and best_overlap >= 2:
            used.add(best_j)
            ct = claude_tags[best_j]
            # Both directions are partial — boundaries differ either way
            results[iid] = {"in_claude": "partial", "claude_text": ct["full"]}
        else:
            results[iid] = {"in_claude": "no", "claude_text": ""}

    return results


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
    checklist_items = _build_checklist(pymupdf_clean_text, pymupdf_report)

    # ── Deterministic Claude pre-check ──────────────────────────────
    precheck = _precheck_claude(checklist_items, claude_converted_text)
    n_pre_yes = sum(1 for v in precheck.values() if v["in_claude"] == "yes")
    n_pre_partial = sum(1 for v in precheck.values() if v["in_claude"] == "partial")
    n_pre_no = sum(1 for v in precheck.values() if v["in_claude"] == "no")
    log.info("🔍 Claude pre-check: %d exact, %d partial, %d not tagged",
             n_pre_yes, n_pre_partial, n_pre_no)

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

    # ── Log full prompt for debugging ───────────────────────────────
    log.info("📝 FULL GPT AUDIT PROMPT (%d chars):\n%s", len(prompt), prompt)

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

    message = client.messages.create(
        model=model,
        max_tokens=128000,
        messages=[{"role": "user", "content": content_parts}],
    )

    raw_response = message.content[0].text.strip()

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

    if n_overridden:
        log.info("🔧 Overrode in_claude/claude_text on %d items (deterministic pre-check)",
                 n_overridden)

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
        "#", "Tag Type", "Page", "PyMuPDF Text", "Context",
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

        # Look up context from original checklist_items by checklist_id
        cid = cr.get("checklist_id", 0)
        ctx_text = ""
        for ci in checklist_items:
            if ci["id"] == cid:
                ctx_text = ci.get("context", "")
                break

        ws1.cell(row=r, column=1,  value=cid)
        ws1.cell(row=r, column=2,  value=cr.get("tag_type", ""))
        ws1.cell(row=r, column=3,  value=cr.get("page", ""))
        ws1.cell(row=r, column=4,  value=cr.get("pymupdf_text", ""))
        ws1.cell(row=r, column=5,  value=ctx_text)
        ws1.cell(row=r, column=6,  value=cr.get("claude_text", ""))
        ws1.cell(row=r, column=7,  value=cr.get("in_image", ""))
        ws1.cell(row=r, column=8,  value=cr.get("in_claude", ""))
        ws1.cell(row=r, column=9,  value=verdict)
        ws1.cell(row=r, column=10, value=classification)
        ws1.cell(row=r, column=11, value=cr.get("notes", ""))
        ws1.cell(row=r, column=12, value=cr.get("suggested_fix", ""))
        _style_row(ws1, r, NCOLS, fill)
        r += 1

    col_widths = [5, 10, 6, 55, 50, 55, 22, 14, 14, 18, 45, 50]
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

    n_af_dropped = sum(1 for a in additional_findings if a.get("issue_type") == "content_dropped")
    n_af_added = sum(1 for a in additional_findings if a.get("issue_type") == "content_added")
    n_af_tag_err = sum(1 for a in additional_findings if a.get("issue_type") == "tag_error")
    n_af_text_err = sum(1 for a in additional_findings if a.get("issue_type") == "text_error")

    total_errors = n_dropped + n_real_miss + n_partial + len(additional_findings)

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
