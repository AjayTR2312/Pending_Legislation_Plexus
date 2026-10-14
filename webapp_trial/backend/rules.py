"""
Rule engine: paragraph/tag structure plus the per-jurisdiction conversion rules.

Two layers, both text-only (no PDF needed), so every finding can be pinned to an
exact character range:

GLOBAL rules — the house rules that apply to every state:
  * One paragraph, one tag. A tag never spans a paragraph break or a line break,
    and a paragraph's continuous run of one formatting type is never fragmented
    into several adjacent tags.
  * No merging or splitting of paragraphs (list/section markers glued onto the
    previous paragraph; a paragraph broken mid-sentence; unjoined wrapped lines).
  * Well-formed tags (balanced, same type, never nested, never empty).
  * No leftover artifacts (page markers, line numbers, markdown, chatter).

STATE rules — read from ``Opus4.6/netscan_state_template.json`` (the NetScan
state spec) and the per-state clean prompts in ``data/Prompts``. Each rule is
registered with the states it applies to, so the catalog we return to the UI can
show exactly which rules ran for the document's jurisdiction and which spec
items still need a human to check against the PDF (underline vs bold vs italic
cannot be seen in text).

Known exceptions, taken from the prompts, that are NOT flagged:
  * table rows / cells — each cell or row keeps its own tag (DE, IN, FL tables)
  * adjacent deletion + addition pairs — different types stay separate
  * plain words between two tags of the same type — then they are not adjacent
"""

from __future__ import annotations

import bisect
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
APP_DIR = PROJECT_ROOT / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import settings  # noqa: E402

SPEC_PATH = settings.STATE_SPEC_FILE
MAX_PER_RULE = settings.RULES_MAX_PER_RULE

# ── Spec loading ─────────────────────────────────────────────────────────────

_SPEC: Optional[dict[str, Any]] = None


def load_spec() -> dict[str, Any]:
    global _SPEC
    if _SPEC is None:
        try:
            _SPEC = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _SPEC = {"global_rules": {}, "states": {}}
    return _SPEC


def state_spec(code: Optional[str]) -> Optional[dict[str, Any]]:
    if not code:
        return None
    return load_spec().get("states", {}).get(code.upper())


# ── Text model ───────────────────────────────────────────────────────────────

_DELIM = re.compile(r"\[([AD])>|<([AD])\]")
_TAG_STRIP = re.compile(r"\[[AD]>|<[AD]\]")


class Ctx:
    """The document, tokenised once, shared by every rule."""

    def __init__(self, text: str, state: Optional[str], reference: Optional[str] = None):
        self.text = text
        self.reference = reference  # the source extraction, when available
        self.state = (state or "").upper() or None
        self.lines = text.splitlines(keepends=True)
        self.starts: list[int] = []
        pos = 0
        for raw in self.lines:
            self.starts.append(pos)
            pos += len(raw)
        self.spans, self.problems = parse_tags(text)
        self.paras = self._paragraphs()

    def line_of(self, off: int) -> int:
        return max(bisect.bisect_right(self.starts, off) - 1, 0)

    def line(self, li: int) -> str:
        return self.lines[li].rstrip("\r\n") if 0 <= li < len(self.lines) else ""

    def line_span(self, li: int) -> tuple[int, int]:
        s = self.starts[li]
        return s, s + len(self.line(li))

    def _paragraphs(self) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        start = None
        end = 0
        for li, raw in enumerate(self.lines):
            if raw.strip():
                if start is None:
                    start = self.starts[li]
                end = self.starts[li] + len(raw.rstrip("\r\n"))
            elif start is not None:
                out.append((start, end))
                start = None
        if start is not None:
            out.append((start, end))
        return out


def parse_tags(text: str) -> tuple[list[dict[str, Any]], list[tuple[str, str, int, int]]]:
    """Pair [A>…<A] / [D>…<D]. Returns (spans, problems).

    span: {type, o0, o1 (open delimiter), c0, c1 (close delimiter), depth, parent}
    problem: (kind, type, start, end) with kind in unclosed|unopened|mismatch
    """
    spans: list[dict[str, Any]] = []
    problems: list[tuple[str, str, int, int]] = []
    stack: list[tuple[str, int, int]] = []
    for m in _DELIM.finditer(text):
        if m.group(1):
            stack.append((m.group(1), m.start(), m.end()))
            continue
        t = m.group(2)
        if not stack:
            problems.append(("unopened", t, m.start(), m.end()))
            continue
        ot, o0, o1 = stack[-1]
        if ot == t:
            stack.pop()
            spans.append({"type": t, "o0": o0, "o1": o1, "c0": m.start(), "c1": m.end(),
                          "depth": len(stack), "parent": stack[-1][0] if stack else None})
        elif any(s[0] == t for s in stack):
            while stack and stack[-1][0] != t:
                u = stack.pop()
                problems.append(("unclosed", u[0], u[1], u[2]))
            _, o0, o1 = stack.pop()
            spans.append({"type": t, "o0": o0, "o1": o1, "c0": m.start(), "c1": m.end(),
                          "depth": len(stack), "parent": stack[-1][0] if stack else None})
        else:
            stack.pop()
            problems.append(("mismatch", ot, o0, m.end()))
    for ot, o0, o1 in stack:
        problems.append(("unclosed", ot, o0, o1))
    spans.sort(key=lambda s: s["o0"])
    return spans, problems


# ── Rule registry ────────────────────────────────────────────────────────────

RULES: list[dict[str, Any]] = []


def rule(rid: str, source: str, title: str, requirement: str, states: str = "*",
         covers: tuple[str, ...] = ()) -> Callable:
    """Register a rule. `states` is "*" or a comma list; `covers` are keywords that
    mark the matching NetScan custom_rules entries as automated."""

    def deco(fn: Callable[[Ctx], list[dict[str, Any]]]):
        RULES.append({
            "id": rid, "source": source, "title": title, "requirement": requirement,
            "states": None if states == "*" else set(states.split(",")), "covers": covers, "fn": fn,
        })
        return fn

    return deco


def F(rid: str, source: str, severity: str, title: str, detail: str, start: int, end: int,
      **extra: Any) -> dict[str, Any]:
    return {"rule": rid, "source": source, "severity": severity, "title": title,
            "detail": detail, "start": start, "end": max(end, start + 1), **extra}


def _plain(s: str) -> str:
    return _TAG_STRIP.sub("", s)


# ═══════════════════════════ GLOBAL: tag structure ═══════════════════════════

@rule("tag_unbalanced", "tags", "Unmatched tag delimiter",
      "Every [A> needs a <A] and every [D> needs a <D]; the pair must be the same type.")
def _tag_unbalanced(c: Ctx):
    out = []
    for kind, t, s, e in c.problems:
        if kind == "unclosed":
            out.append(F("tag_unbalanced", "tags", "error", f"{t} tag is never closed",
                         f"[{t}> opens here but there is no <{t}] after it.", s, e))
        elif kind == "unopened":
            out.append(F("tag_unbalanced", "tags", "error", f"{t} close tag has no opener",
                         f"<{t}] closes a tag that was never opened.", s, e))
        else:
            out.append(F("tag_unbalanced", "tags", "error", f"Mismatched tag types",
                         f"This [{t}> tag is closed with a different type.", s, e))
    return out[:MAX_PER_RULE]


@rule("tag_malformed", "tags", "Malformed delimiter",
      "Only [A> <A] [D> <D] are allowed; <A> or <D> and markdown are not valid closers.")
def _tag_malformed(c: Ctx):
    return [F("tag_malformed", "tags", "error", "Malformed close tag",
              f"Found {m.group(0)} — a close tag must be <{m.group(1)}].", m.start(), m.end())
            for m in re.finditer(r"<([AD])>", c.text)][:MAX_PER_RULE]


@rule("tag_nested", "tags", "Nested tags",
      "Tags never nest or overlap (DE, KS). Mixed bold/italic/underline is ONE tag.")
def _tag_nested(c: Ctx):
    out = []
    for s in c.spans:
        if s["depth"] > 0:
            same = s["parent"] == s["type"]
            out.append(F("tag_nested", "tags", "error",
                         f"{s['type']} tag nested inside {s['parent']} tag",
                         "Continuously formatted text is one tag — do not open a new tag "
                         "inside an open one." if same else "Opposite-type nesting is never valid.",
                         s["o0"], s["c1"]))
    return out[:MAX_PER_RULE]


@rule("tag_empty", "tags", "Empty tag", "A tag must wrap at least one visible character.")
def _tag_empty(c: Ctx):
    return [F("tag_empty", "tags", "error", "Empty tag", "Nothing is inside this tag.",
              s["o0"], s["c1"]) for s in c.spans if not _plain(c.text[s["o1"]:s["c0"]]).strip()
            ][:MAX_PER_RULE]


# ═══════════ GLOBAL: one paragraph = one tag, no merge / no split ════════════

_BLANK_RUN = re.compile(r"\n[ \t]*\n")


@rule("para_tag_crosses_paragraph", "paragraph", "Tag spans several paragraphs",
      "A paragraph is tagged on its own: one open/close tag per paragraph. A tag must never "
      "cross a paragraph break (a blank line).")
def _tag_crosses_paragraph(c: Ctx):
    out = []
    for s in c.spans:
        inner = c.text[s["o1"]:s["c0"]]
        if _BLANK_RUN.search(inner):
            n = len(_BLANK_RUN.findall(inner)) + 1
            out.append(F("para_tag_crosses_paragraph", "paragraph", "error",
                         f"One {s['type']} tag covers {n} paragraphs",
                         f"This tag runs across {n - 1} paragraph break(s). Close it at the end "
                         "of each paragraph and open a new tag in the next one.",
                         s["o0"], s["c1"]))
    return out[:MAX_PER_RULE]


@rule("para_tag_crosses_line", "paragraph", "Tag broken across a line break",
      "Within a paragraph the text is one continuous line, so a tag never contains a line break "
      "(CO, DE: remove line breaks inside tags; FL tables: one tag per visual line).")
def _tag_crosses_line(c: Ctx):
    out = []
    for s in c.spans:
        inner = c.text[s["o1"]:s["c0"]]
        if "\n" in inner and not _BLANK_RUN.search(inner):
            out.append(F("para_tag_crosses_line", "paragraph", "error",
                         f"{s['type']} tag contains a line break",
                         "The paragraph is wrapped inside the tag. Join the lines so the tag is one "
                         "continuous span.", s["o0"], s["c1"]))
    return out[:MAX_PER_RULE]


def _tableish(line: str) -> bool:
    return (bool(re.search(r"\S {4,}\S", line)) or "\t" in line or line.startswith("   ")
            or bool(re.search(r"\.{5,}", line)))


@rule("para_tag_fragmented", "paragraph", "Paragraph tagged in fragments",
      "Continuous formatting of one type inside a paragraph is ONE tag, not several adjacent tags "
      "(CO, KS, IA). Exempt: table cells, and tags separated by plain words.")
def _fragmented(c: Ctx):
    out = []
    flat = [s for s in c.spans if s["depth"] == 0]
    for a, b in zip(flat, flat[1:]):
        if a["type"] != b["type"]:
            continue
        gap = c.text[a["c1"]:b["o0"]]
        if gap.strip() or _BLANK_RUN.search(gap):
            continue
        if re.search(r" {6,}|\t", gap):  # column gap: separate cells keep their own tag
            continue
        if "\n" in gap:
            la, lb = c.line_of(a["c0"]), c.line_of(b["o0"])
            if _tableish(c.line(la)) or _tableish(c.line(lb)):
                continue
        out.append(F("para_tag_fragmented", "paragraph", "error",
                     f"Adjacent {a['type']} tags — should be one tag",
                     "These two tags touch with only whitespace between them, so the paragraph is "
                     "tagged in pieces. Merge them into a single tag.", a["o0"], b["c1"]))
    return out[:MAX_PER_RULE]


_LABEL_ANY = r"\([A-Za-z0-9]{1,3}\)|[A-Za-z]\.|\d{1,2}\."
_INLINE_LABEL = re.compile(
    r"(?P<pre>[.;:?!][\"”')]*(?:<[AD]\])?)(?P<sp>[ \t]+)(?P<open>(?:\[[AD]>)?)"
    r"(?P<label>" + _LABEL_ANY + r")(?=[ \t]+(?:\[[AD]>)?[A-Z\"“(\[])"
)
_START_LABEL = re.compile(
    r"^(?:\[[AD]>)*\s*(?:(?:Sec\.|SEC\.|SECTION|Section)\s+\d+[A-Za-z]?\.\s+)?"
    r"(?:[\d§][\w.,\-§]*\.\s+)?(?P<label>" + _LABEL_ANY + r")(?=\s)"
)
_SECTION_INLINE = re.compile(
    r"[.;:?!][\"”')]*(?:<[AD]\])?[ \t]+(?:\[[AD]>)?(?P<h>(?:Section|SECTION|Sec\.|SEC\.)\s+\d+[A-Za-z]?\.)(?=[ \t])"
)
_NUMTOKEN = re.compile(r"[\d§][\w.,\-§()]*$")


def _succ(label: str) -> Optional[str]:
    m = re.fullmatch(r"\(([a-z])\)", label)
    if m and m.group(1) != "z":
        return f"({chr(ord(m.group(1)) + 1)})"
    m = re.fullmatch(r"\(([A-Z])\)", label)
    if m and m.group(1) != "Z":
        return f"({chr(ord(m.group(1)) + 1)})"
    m = re.fullmatch(r"\((\d{1,2})\)", label)
    if m:
        return f"({int(m.group(1)) + 1})"
    m = re.fullmatch(r"([a-z])\.", label)
    if m and m.group(1) != "z":
        return f"{chr(ord(m.group(1)) + 1)}."
    m = re.fullmatch(r"([A-Z])\.", label)
    if m and m.group(1) != "Z":
        return f"{chr(ord(m.group(1)) + 1)}."
    m = re.fullmatch(r"(\d{1,2})\.", label)
    if m:
        return f"{int(m.group(1)) + 1}."
    return None


@rule("para_merged", "paragraph", "Paragraphs merged into one",
      "Each subsection/list item (a), (b), (1), (2) and each Section heading starts its own "
      "paragraph. Never glue the next one onto the end of the previous.")
def _merged(c: Ctx):
    out = []
    for ps, pe in c.paras:
        para = c.text[ps:pe]
        sm = _START_LABEL.match(para)
        last = sm.group("label") if sm else None
        for m in _INLINE_LABEL.finditer(para):
            label = m.group("label")
            before = para[:m.start("pre")]
            expected = _succ(last) if last else None
            if expected and label == expected:
                sev, why = "error", f"{label} follows {last} in the same paragraph"
            elif label in ("(a)", "a.", "(1)", "1.", "(A)", "A."):
                tok = before.split()[-1] if before.split() else ""
                short_heading = m.group("pre").startswith(".") and len(_plain(before).split()) <= 4
                if short_heading or _NUMTOKEN.search(tok.rstrip(".")) or re.search(
                        r"(?:Sec\.|SEC\.|SECTION|Section)\s+\d+[A-Za-z]?$", before.rstrip(". ")):
                    # "Sec. 2. (a)", "38-2201a. (a)", "SECTION 31. Appropriation. (1)", "b. (1)":
                    # the label belongs to a short heading/label prefix on the same line
                    continue
                sev, why = "warning", f"{label} starts mid-paragraph"
            else:
                continue
            last = label
            s = ps + (m.start("open") if m.group("open") else m.start("label"))
            out.append(F("para_merged", "paragraph", sev, f"Paragraph merge: {label} glued to previous text",
                         f"{why}. Start {label} on its own paragraph (blank line before it).",
                         s, ps + m.end("label")))
        for m in _SECTION_INLINE.finditer(para):
            out.append(F("para_merged", "paragraph", "error", "Paragraph merge: Section heading mid-paragraph",
                         f"“{m.group('h')}” starts a new section and must begin its own paragraph.",
                         ps + m.start("h"), ps + m.end("h")))
    return out[:MAX_PER_RULE * 2]


_TERMINAL = ".;:?!\"”')—-"


@rule("para_split", "paragraph", "Paragraph split mid-sentence",
      "A paragraph is one continuous block. A break in the middle of a sentence — a lowercase "
      "continuation after a blank line, or an unjoined wrapped line — is a split paragraph.")
def _split(c: Ctx):
    out = []
    # (1) blank line, then a lowercase continuation of a sentence that never ended
    for (as_, ae), (bs, be) in zip(c.paras, c.paras[1:]):
        prev = _plain(c.text[as_:ae]).rstrip()
        nxt = _plain(c.text[bs:be]).lstrip()
        if not prev or not nxt or len(prev.split()) < 3:
            continue
        if prev[-1] in _TERMINAL or _tableish(c.text[bs:be].split("\n")[0]):
            continue
        if nxt[0].islower() and not _START_LABEL.match(c.text[bs:be]):
            out.append(F("para_split", "paragraph", "warning", "Paragraph split mid-sentence",
                         "The previous paragraph ends without punctuation and this one continues in "
                         "lowercase — they are one paragraph.", bs, min(bs + 40, be)))
    # (2) wrapped lines inside a paragraph that were never joined
    for ps, pe in c.paras:
        ls = c.line_of(ps)
        le = c.line_of(max(pe - 1, ps))
        for li in range(ls, le):
            a, b = c.line(li), c.line(li + 1)
            if not a.strip() or not b.strip() or _tableish(a) or _tableish(b):
                continue
            a_plain, b_plain = _plain(a).rstrip(), _plain(b).lstrip()
            if a_plain and b_plain and a_plain[-1] not in _TERMINAL and b_plain[0].islower():
                _, ae_ = c.line_span(li)
                out.append(F("para_split", "paragraph", "warning", "Wrapped line not joined",
                             "This line breaks in the middle of a sentence. Join it to the next line "
                             "so the paragraph is one block.", ae_ - min(len(a), 25), ae_ + 1))
    return out[:MAX_PER_RULE * 2]


_SPACING_STATES = "FL,CA,CO,CT,DC,IN,IA,DE,AR,KS"


@rule("fmt_spacing", "rules", "Missing blank line between list items",
      "Subsection and list items each sit in their own paragraph with a blank line after "
      "(FL, CA, CO, CT, DC, IN, IA, DE, AR, KS letters).", states=_SPACING_STATES)
def _spacing(c: Ctx):
    out = []
    for li in range(len(c.lines) - 1):
        a, b = c.line(li), c.line(li + 1)
        if not a.strip() or not b.strip():
            continue
        ma, mb = _START_LABEL.match(a), _START_LABEL.match(b)
        if not (ma and mb):
            continue
        la, lb = ma.group("label"), mb.group("label")
        if c.state == "KS" and re.fullmatch(r"\(\d+\)|\d+\.", la) and re.fullmatch(r"\(\d+\)|\d+\.", lb):
            continue  # KS numbered lists are single-spaced
        s, e = c.line_span(li + 1)
        out.append(F("fmt_spacing", "rules", "warning", f"No blank line before {lb}",
                     f"{c.state or 'This state'} puts a blank line between list items ({la} → {lb}).",
                     s, min(e, s + 30)))
    return out[:MAX_PER_RULE]


# ═══════════════════════ GLOBAL: leftovers and format ════════════════════════

@rule("fmt_artifacts", "rules", "Conversion artifacts left in output",
      "Page markers, structural-metadata blocks, code fences, markdown and chatter never belong "
      "in the output.")
def _artifacts(c: Ctx):
    out = []
    for m in re.finditer(r"<<<PAGE_\d+_(?:START|END)>>>", c.text):
        out.append(F("fmt_artifacts", "rules", "error", "Page marker left in output",
                     "Remove every <<<PAGE_N_START/END>>> marker.", m.start(), m.end()))
    for m in re.finditer(r"═══[^\n]*(?:STRUCTURAL METADATA|END METADATA)[^\n]*", c.text):
        out.append(F("fmt_artifacts", "rules", "error", "Structural-metadata block left in output",
                     "The metadata block is input only and must not be emitted.", m.start(), m.end()))
    for m in re.finditer(r"^\s*```[a-z]*\s*$", c.text, re.M):
        out.append(F("fmt_artifacts", "rules", "error", "Code fence in output",
                     "Output must be raw text with no ``` wrappers.", m.start(), m.end()))
    first = c.text.lstrip()[:200]
    m = re.match(r"(?i)(here is|here's|i'll|i will|i have|the following is|after cleaning|sure)", first)
    if m:
        s = len(c.text) - len(c.text.lstrip())
        out.append(F("fmt_artifacts", "rules", "error", "Conversational text at the start",
                     "The output must begin with the first line of the bill, not an explanation.",
                     s, s + 60))
    # The model narrating what it dropped instead of dropping it, e.g. "SJ9609 removed as barcode text"
    for m in re.finditer(r"(?im)^[^\n]*\b(?:removed|omitted|stripped|dropped|ignored|deleted)\s+(?:as|because|since)\b[^\n]*"
                         r"(?:barcode|footer|header|page number|line number|artifact|watermark)[^\n]*$", c.text):
        out.append(F("fmt_artifacts", "rules", "error", "The model's note about a removal is in the output",
                     "A note such as \"removed as barcode text\" is commentary, not bill text. Delete the line: "
                     "page furniture is dropped silently.", m.start(), m.end()))
    for m in re.finditer(r"\*\*[^*\n]+\*\*|~~[^~\n]+~~|(?<![\w_])__[A-Za-z][^_\n]*__(?![\w_])", c.text):
        out.append(F("fmt_artifacts", "rules", "error", "Markdown formatting in output",
                     "Use only [A>…<A] and [D>…<D]; markdown is forbidden.", m.start(), m.end()))
    return out[:MAX_PER_RULE * 2]


_NO_ASTERISK = "DC,IN,DE,KS,IA"


@rule("fmt_asterisk", "rules", "Asterisk in output",
      "Italic text is plain words or a tag — an asterisk must never appear (DC, IN, DE, KS, IA).",
      states=_NO_ASTERISK, covers=("asterisks",))
def _asterisk(c: Ctx):
    return [F("fmt_asterisk", "rules", "error", "Asterisk in output",
              "Remove the markdown asterisk; italic is output as plain text or inside a tag.",
              m.start(), m.end()) for m in re.finditer(r"\*", c.text)][:MAX_PER_RULE]


@rule("fmt_quotes", "rules", "Smart quotes",
      "All conversions use regular quotes, not smart quotes (NetScan global rule).")
def _quotes(c: Ctx):
    out = [F("fmt_quotes", "rules", "warning", "Smart quote", "Replace with a regular \" or ' quote.",
             m.start(), m.end()) for m in re.finditer(r"[“”‘’]", c.text)]
    return out[:MAX_PER_RULE]


@rule("fmt_double_hyphen", "rules", "Em dash instead of --",
      "Punctuation is kept exactly as printed: a source '--' is never converted to an em dash "
      "(FL, KS). Only checked against the source baseline — a printed em dash is fine.",
      states="FL,KS")
def _emdash(c: Ctx):
    ref = c.reference
    if not ref or "--" not in ref:
        return []  # without a source that uses '--' we cannot tell conversion from a printed em dash
    surplus = c.text.count("—") - ref.count("—")
    if surplus <= 0:
        return []
    return [F("fmt_double_hyphen", "rules", "warning", "Em dash where -- is required",
              "The source prints '--' here; keep the double hyphen exactly as printed.",
              m.start(), m.end()) for m in re.finditer(r"—", c.text)][:MAX_PER_RULE]


@rule("fmt_line_numbers", "rules", "Line numbers left in text",
      "Margin line numbers are artifacts and must be removed from every line.")
def _line_numbers(c: Ctx):
    hits = [(li, m) for li in range(len(c.lines))
            if (m := re.match(r"^\s{0,8}(\d{1,3})\s{2,}(?=[A-Za-z\[(\"])", c.line(li)))]
    if len(hits) < 3:
        return []
    return [F("fmt_line_numbers", "rules", "warning", "Line number left in text",
              f"“{m.group(1)}” looks like a margin line number.",
              c.starts[li] + m.start(1), c.starts[li] + m.end(1)) for li, m in hits][:MAX_PER_RULE]


@rule("fmt_trailing", "rules", "Stray character after the end of the bill",
      "Nothing may follow the last line of the bill — not even a lone 'O' (CA).", states="CA",
      covers=("trailing character", "after the last line"))
def _trailing(c: Ctx):
    last = c.text.rstrip().splitlines()[-1] if c.text.strip() else ""
    if re.fullmatch(r"\s*O\.?\s*", last):
        off = c.text.rstrip().rfind(last)
        return [F("fmt_trailing", "rules", "error", "Stray trailing character",
                  "A lone 'O' was appended after the bill text.", off, off + len(last))]
    return []


# Per-state artifact lines that must be gone. (regex on the stripped line, label, nth)
# nth > 0 means "only the 2nd+ occurrence is a repeated page header".
_ART = {
    "*": [(r"(?i)page \d+ of \d+", "Page-number footer", 0)],
    "FL": [
        (r"(?i)(?:CS(?:\s+for|/CS|/)?\s*)*(?:HB|SB|HJR|SJR|HCR|SCR|HM|SM|HR|SR)\s*\d+[a-z]?"
         r"(?:,\s*\d+(?:st|nd|rd|th)\s+Engrossed)?(?:\s+\d{1,4})?", "Bill-number header", 0),
        (r"(?i)F\s?L\s?O\s?R\s?I\s?D\s?A\s+H\s?O\s?U\s?S\s?E.*", "Chamber header", 0),
        (r"(?i)Florida (?:Senate|House of Representatives)\b.*\d{4}.*", "Chamber header", 0),
        (r"\d{2}-\d{5}[A-Z]-\d{2}", "Internal reference number", 0),
        (r"\d{6,}_*", "Internal bill identifier", 0),
        (r"\d{8}er", "Document reference code", 0),
        (r"(?i)CODING:.*", "CODING footer", 0),
        (r"(?:19|20)\d{2}", "Lone year header", 0),
    ],
    "CO": [
        (r"(?i)shading denotes.*", "First-page footer", 0),
        (r"(?i)capital letters or bold.*", "First-page footer", 0),
        (r"(?i)dashes through.*", "First-page footer", 0),
        (r"-\s?\d{1,3}\s?-", "Page-number footer", 0),
        (r"[HS]B\d{2}-\d{4}(?:-\d+)?", "Bill/page footer", 0),
        (r"(?i)COLORADO GENERAL ASSEMBLY", "Repeated header", 2),
    ],
    "CA": [
        (r"(?i)(?:SENATE, |ASSEMBLY, )?CALIFORNIA LEGISLATURE.*REGULAR SESSION", "Repeated header", 2),
        (r"(?i)(?:SENATE|ASSEMBLY) BILL\s+No\.\s*\d+", "Repeated header", 2),
    ],
    "CT": [
        (r"s?[SH]B\s?\d+\s*(?:/\s*File No\.?\s*\d+)?", "Compact bill designator", 0),
        (r"(?i)LCO No\.?\s*\d+\s+\d+ of \d+", "LCO footer", 0),
        (r"(?i)CONNECTICUT LAW JOURNAL", "Header", 0),
        (r"(?i)Raised Bill No\.\s*\d+", "Repeated header", 2),
        (r"(?i)LCO No\.\s*\d+", "Repeated header", 2),
    ],
    "IN": [
        (r"(?:IN|SB|HB|EH|ES|HR|SR)\s*\d+—LS\s*\d+/DI\s*\d+", "Bill reference footer", 0),
        (r"\*[A-Z]{1,3}\d+(?:\.\d+)?\*", "Barcode text", 0),
        (r"(?:19|20)\d{2}", "Year-only footer", 0),
        (r"\d{1,3}", "Standalone page number", 0),
    ],
    "AR": [
        (r"\*[HS][A-Z]{1,3}\d+\*", "Scan barcode", 0),
        (r"\d{2}/\d{2}/\d{4}\s+\d{1,2}:\d{2}:\d{2}\s*[AP]M\s+\w+", "Timestamp footer", 0),
        (r"\d{1,3}\s+\d{2}/\d{2}/\d{4}\s+\d{1,2}:\d{2}:\d{2}\s*[AP]M\s+\w+", "Running header", 0),
        (r"[HS][BJC]?R?\d{2,5}", "Bare bill-ID header", 0),
    ],
    "IA": [
        (r"T?LSB\s+\w+.*", "LSB footer", 0),
        (r"[a-z]{2}/[a-z]{2}(?:\s+\d+/\d+)?", "Drafter initials footer", 0),
    ],
    "DE": [
        (r"(?i)Released:\s*\d.*", "Released footer", 0),
        (r"HD\s*:\s*\w+\s*:\s*\w+", "Document ID", 0),
    ],
    "DC": [
        (r"(?i)Codification District of Columbia Official Code.*", "Coding footer", 0),
        (r"(?i)COUNCIL OF THE DISTRICT OF COLUMBIA", "Repeated header", 2),
    ],
    "KS": [
        (r"(?:HB|SB|HCR|SCR|HR|SR)\s*\d{2,4}\s*[—–-]\s*\d+(?:\s*[—–-])?", "Page header", 0),
    ],
}


@rule("st_artifacts", "rules", "State header/footer left in output",
      "Each state lists the headers, footers and codes that must be removed (NetScan 'What to delete').")
def _state_artifacts(c: Ctx):
    out = []
    pats = _ART["*"] + _ART.get(c.state or "", [])
    seen: dict[str, int] = {}
    for li in range(len(c.lines)):
        raw = c.line(li).strip()
        if not raw:
            continue
        plain = _plain(raw).strip()
        for rx, label, nth in pats:
            if re.fullmatch(rx, plain):
                if nth:
                    seen[label + rx] = seen.get(label + rx, 0) + 1
                    if seen[label + rx] < nth:
                        break
                s, e = c.line_span(li)
                out.append(F("st_artifacts", "rules", "warning" if nth else "error", f"{label} left in output",
                             f"“{plain[:80]}” should have been removed"
                             + (f" ({c.state} rule)." if c.state else "."), s, e))
                break
    return out[:MAX_PER_RULE * 2]


# ═══════════════════════════ STATE: never-tag rules ══════════════════════════

_KS_HEADER = [
    r"Session of \d{4}", r"As Amended by (?:House|Senate) Committee.*",
    r"(?:ENGROSSED |SUBSTITUTE FOR |HOUSE Substitute for |SENATE Substitute for )?"
    r"(?:HOUSE|SENATE)(?: CONCURRENT)?(?: BILL| RESOLUTION)? No\. ?\w+",
    r"By (?:Committee on|Representative|Senator).*", r"Requested by .*", r"AN ACT concerning.*",
]
_KS_SIGN = [
    r"I hereby certify that the above BILL originated.*", r"Speaker of the House", r"President of the Senate",
    r"Chief Clerk of the House", r"Secretary of the Senate", r"APPROVED", r"Governor", r"Secretary of State",
    r"Approved\s*_+.*",
]
_IN_HEADER = [
    r"(?:ENGROSSED |REPRINTED )?(?:HOUSE|SENATE) BILL No\. ?\d+", r"(?:Introduced|Engrossed|Enrolled|Reprinted)(?: Act| Version)?",
    r"DIGEST OF .*", r"A BILL FOR AN ACT.*", r"(?:Citations Affected|Synopsis|Effective):?",
    r"(?:First|Second) Regular Session of the \d+\w* General Assembly.*",
]
_RI_NOTAG = [r"(?i)introduced by:?.*", r"(?i)date introduced:?.*"]
_CT_HEADER = [r"General Assembly.*", r"Raised Bill No\. ?\d+.*", r"LCO No\. ?\d+.*",
              r"(?:January|February) Session, \d{4}", r"AN ACT CONCERNING.*", r"Introduced by:.*"]


def _tagged_line_findings(c: Ctx, rid: str, pats: list[str], title: str, detail: str,
                          ignore_case: bool = True) -> list[dict[str, Any]]:
    out = []
    for s in c.spans:
        inner = _plain(c.text[s["o1"]:s["c0"]]).strip()
        for p in pats:
            if re.fullmatch(p, inner, re.I if ignore_case else 0):
                out.append(F(rid, "rules", "error", title, detail.format(text=inner[:70]), s["o0"], s["c1"]))
                break
    return out


@rule("st_never_tag_clauses", "rules", "Protected clause carries a tag",
      "Enacting / resolving clauses and boilerplate are never tagged, even if printed in bold or "
      "italic (KS, IN, CA, DE).", states="KS,IN,CA,DE", covers=("enacting", "resolving", "never tag"))
def _protected(c: Ctx):
    try:
        from protected_clauses import PROTECTED_CLAUSES, PROTECTED_CLAUSE_PREFIXES
    except Exception:  # noqa: BLE001
        return []
    clauses = PROTECTED_CLAUSES.get(c.state or "", [])
    prefixes = PROTECTED_CLAUSE_PREFIXES.get(c.state or "", [])
    out = []
    for s in c.spans:
        inner = _plain(c.text[s["o1"]:s["c0"]]).strip()
        low = re.sub(r"\s+", " ", inner).lower()
        # also catch a tag that only *contains* the clause or only starts inside it
        hit = next((cl for cl in clauses if cl.lower().rstrip(":,") in low), None) \
            or next((p for p in prefixes if low.startswith(p.lower())), None)
        if hit:
            out.append(F("st_never_tag_clauses", "rules", "error", "Enacting/resolving clause is tagged",
                         f"“{hit[:70]}” must be plain, untagged text in {c.state}.", s["o0"], s["c1"]))
    return out[:MAX_PER_RULE]


@rule("st_ks_header_signature", "rules", "KS header metadata / signature block is tagged",
      "Page-1 header metadata (Session of, bill no., sponsor, AN ACT concerning) and the "
      "certification/signature block are plain text in Kansas.", states="KS",
      covers=("page 1 header metadata", "signature / certification"))
def _ks_header(c: Ctx):
    return _tagged_line_findings(c, "st_ks_header_signature", _KS_HEADER + _KS_SIGN,
                                 "KS boilerplate is tagged", "“{text}” is document metadata or a signing line "
                                 "in Kansas — output it as plain text.", ignore_case=False)[:MAX_PER_RULE]


@rule("st_in_header", "rules", "IN header element is tagged",
      "Bill number, version line, DIGEST/Synopsis labels, session line and 'A BILL FOR AN ACT' are "
      "plain text — never tagged (IN).", states="IN", covers=("bill or resolution number", "must not be tagged"))
def _in_header(c: Ctx):
    return _tagged_line_findings(c, "st_in_header", _IN_HEADER, "IN header element is tagged",
                                 "“{text}” is styled bold in the PDF but is not an addition.")[:MAX_PER_RULE]


@rule("st_ri_intro", "rules", "RI 'introduced by' is tagged",
      "'Introduced by:' and 'Date introduced' are never marked as additions (RI).", states="RI",
      covers=("introduced by",))
def _ri(c: Ctx):
    return _tagged_line_findings(c, "st_ri_intro", _RI_NOTAG, "RI intro line is tagged",
                                 "“{text}” must not be tagged.")[:MAX_PER_RULE]


@rule("st_ct_header", "rules", "CT page-1 header text is tagged",
      "Connecticut header lines (General Assembly, Raised Bill No., LCO No., Session) are decorative "
      "lines misread as underlines — no tags (CT).", states="CT")
def _ct_header(c: Ctx):
    head = c.text[: c.paras[min(8, len(c.paras) - 1)][1]] if c.paras else ""
    out = []
    for s in c.spans:
        if s["o0"] > len(head):
            break
        inner = _plain(c.text[s["o1"]:s["c0"]]).strip()
        if any(re.fullmatch(p, inner, re.I) for p in _CT_HEADER):
            out.append(F("st_ct_header", "rules", "error", "CT header text is tagged",
                         f"“{inner[:60]}” is page-1 header text, not an addition.", s["o0"], s["c1"]))
    return out[:MAX_PER_RULE]


# ═════════════════════ STATE: where tagging may start/stop ═══════════════════

_ENACT = re.compile(r"(?i)be it (?:enacted|resolved)")


@rule("st_tag_start", "rules", "Tags before the allowed start point",
      "Some states only tag from a point onward: IN from 'PRINTING CODE', MO and VA after 'Be it "
      "enacted', CO header block untagged.", states="IN,MO,VA,CO",
      covers=("printing code", "start after", "after be it enacted"))
def _tag_start(c: Ctx):
    markers = {"IN": re.compile(r"PRINTING CODE"), "MO": _ENACT, "VA": _ENACT}
    out = []
    if c.state in markers:
        m = markers[c.state].search(c.text)
        if not m:
            return []
        for s in c.spans:
            if s["o0"] < m.start():
                out.append(F("st_tag_start", "rules", "error",
                             f"{c.state} tags may only start after “{m.group(0)}”",
                             f"This tag appears before “{m.group(0)}”; text up to that point is plain.",
                             s["o0"], s["c1"]))
    elif c.state == "CO":
        hdr = re.compile(r"(?i)^(?:(?:HOUSE|SENATE) BILL \d|BY (?:REPRESENTATIVE|SENATOR)|AN ACT$|CONCERNING\b)")
        for s in c.spans:
            first = _plain(c.text[c.starts[c.line_of(s["o0"])]:][:200].split("\n")[0]).strip()
            if hdr.match(first) and s["depth"] == 0:
                out.append(F("st_tag_start", "rules", "warning", "CO header block is tagged",
                             "Colorado starts mark-up at the first line-numbered body line, not in "
                             "the bill header.", s["o0"], s["c1"]))
    return out[:MAX_PER_RULE]


# ═════════════════════════════ STATE: specifics ══════════════════════════════

@rule("st_co_caps", "rules", "CO additions are ALL CAPS",
      "Colorado marks additions with ALL CAPS. A tag whose words are mostly lowercase is not an "
      "addition; a long ALL-CAPS run in the body should be tagged.", states="CO",
      covers=("all caps", "begin mark up", "subsection label", "single [a> tag"))
def _co(c: Ctx):
    out = []
    for s in c.spans:
        if s["type"] != "A":
            continue
        words = re.findall(r"[A-Za-z]{3,}", _plain(c.text[s["o1"]:s["c0"]]))
        if len(words) >= 4 and sum(w.islower() or w.istitle() for w in words) / len(words) > 0.6:
            out.append(F("st_co_caps", "rules", "warning", "Lowercase text tagged as an addition",
                         "Colorado additions are ALL CAPS; mostly-lowercase text is existing law.",
                         s["o0"], s["c1"]))
    for ps, pe in c.paras:  # label outside the tag
        para = c.text[ps:pe]
        m = re.match(r"^(\([A-Za-z0-9.]+\)(?:\([A-Za-z0-9]+\))*)\s+\[A>", para)
        if m:
            sp = [t for t in c.spans if t["o0"] == ps + m.end() - 3]
            if sp and sp[0]["c1"] >= pe - 1:
                out.append(F("st_co_caps", "rules", "warning", "Subsection label should be inside the tag",
                             "When the whole subsection is new, the label goes inside: "
                             f"[A>{m.group(1)} …<A].", ps, ps + m.end()))
    for m in re.finditer(r"(?<!\[A>)(?<![A-Za-z])((?:[A-Z]{2,}[,;]?\s+){7,}[A-Z]{2,}[.,;]?)", c.text):
        li = c.line_of(m.start())
        if re.match(r"^\s*(SECTION|BE IT|WHEREAS)", c.line(li)) or any(
                s["o0"] <= m.start() < s["c1"] for s in c.spans):
            continue
        out.append(F("st_co_caps", "rules", "info", "Long ALL-CAPS run is not tagged",
                     "In Colorado ALL-CAPS text inside a sentence is an addition. If this is new "
                     "material it should be wrapped in [A>…<A].", m.start(), m.end()))
    return out[:MAX_PER_RULE]


@rule("st_ia_new_section", "rules", "IA NEW SECTION markers",
      "'NEW SECTION', 'NEW SUBSECTION', 'NEW SUBPARAGRAPH' are always tagged as additions; labels "
      "inside such a block are plain; the first line is the file header (IA).", states="IA",
      covers=("new section", "preserve document header", "labels inside"))
def _ia(c: Ctx):
    out = []
    first = c.text.lstrip().split("\n")[0]
    if c.text.strip() and not re.match(r"(?i)^\s*(?:Senate|House) (?:File|Joint Resolution|Concurrent Resolution|Resolution|Study Bill)", first):
        s = len(c.text) - len(c.text.lstrip())
        out.append(F("st_ia_new_section", "rules", "warning", "IA file header is missing",
                     "Iowa output must begin with the title header, e.g. “Senate File 173 - Introduced”.",
                     s, s + max(len(first), 1)))
    for m in re.finditer(r"NEW (?:SECTION|SUBSECTION|SUBPARAGRAPH)", c.text):
        if not any(s["type"] == "A" and s["o1"] <= m.start() and m.end() <= s["c0"] for s in c.spans):
            out.append(F("st_ia_new_section", "rules", "error", f"“{m.group(0)}” is not tagged",
                         "Iowa always tags NEW SECTION / NEW SUBSECTION / NEW SUBPARAGRAPH as an addition.",
                         m.start(), m.end()))
    block_start = None
    for li in range(len(c.lines)):
        ln = c.line(li)
        if "NEW SECTION" in ln or "NEW SUBSECTION" in ln:
            block_start = li
        elif re.match(r"^\s*(?:Sec\.|SEC\.|DIVISION|Section)\s+\d+", ln):
            block_start = None
        if block_start is not None and li > block_start or block_start == li:
            for s in c.spans:
                if c.line_of(s["o0"]) == li and re.fullmatch(r"(?:[a-z]|\d{1,2})\.|\([a-z0-9]{1,2}\)",
                                                             c.text[s["o1"]:s["c0"]].strip()):
                    out.append(F("st_ia_new_section", "rules", "error", "Label tagged inside a NEW SECTION block",
                                 "The NEW SECTION tag already marks the whole block; its labels are plain text.",
                                 s["o0"], s["c1"]))
    return out[:MAX_PER_RULE]


@rule("st_in_new", "rules", "IN: the word NEW must be tagged",
      "In every 'AS A NEW …' introductory clause the bold word NEW is always tagged [A>NEW<A] (IN).",
      states="IN", covers=("word new",))
def _in_new(c: Ctx):
    out = []
    for m in re.finditer(r"(?i)\bADDED TO THE INDIANA CODE AS A (NEW)\b", c.text):
        a, b = m.start(1), m.end(1)
        if not any(s["type"] == "A" and s["o1"] <= a and b <= s["c0"] for s in c.spans):
            out.append(F("st_in_new", "rules", "error", "The word NEW is not tagged",
                         "Indiana bolds NEW in 'AS A NEW SECTION' — it must be [A>NEW<A].", a, b))
    return out[:MAX_PER_RULE]


@rule("st_ks_new_law", "rules", "KS: new-law bill has tags in its body",
      "A Kansas bill that creates entirely new law (no 'hereby amended' / K.S.A. citation) is plain "
      "text with NO tags.", states="KS", covers=("new law bills",))
def _ks_new_law(c: Ctx):
    if not c.spans:
        return []
    body_start = _ENACT.search(c.text)
    body = c.text[body_start.end():] if body_start else c.text
    if re.search(r"hereby amended|K\.S\.A\.", body):
        return []
    s = next((x for x in c.spans if not body_start or x["o0"] >= body_start.end()), None)
    if not s:
        return []
    return [F("st_ks_new_law", "rules", "warning", "New-law bill is tagged",
              "No 'is hereby amended' or K.S.A. citation was found, so this looks like a new-law bill "
              "whose body must be untagged. If it does amend existing law, ignore this.", s["o0"], s["c1"])]


@rule("st_ct_brackets", "rules", "CT: bracketed text must be a deletion",
      "Connecticut marks deletions with [square brackets]; all bracketed text is tagged [D>[…]<D] with "
      "the brackets kept, and (a)/(1) labels are never deleted.", states="CT",
      covers=("bracketed text", "all bracketed"))
def _ct(c: Ctx):
    out = []
    for m in re.finditer(r"(?<![\[<])\[(?![AD]>)[^\[\]\n]{2,400}\](?!>)", c.text):
        if any(s["type"] == "D" and s["o1"] <= m.start() and m.end() <= s["c0"] for s in c.spans):
            continue
        out.append(F("st_ct_brackets", "rules", "error", "Bracketed text is not tagged as a deletion",
                     "Connecticut deletions are tagged [D>[text]<D] with the brackets kept.", m.start(), m.end()))
    for s in c.spans:
        inner = c.text[s["o1"]:s["c0"]].strip()
        if s["type"] == "D" and re.fullmatch(r"\(\w{1,3}\)", inner):
            out.append(F("st_ct_brackets", "rules", "error", "Subsection label tagged as a deletion",
                         "(a)/(1) labels are paragraph markers, not deletions — only [square brackets] delete.",
                         s["o0"], s["c1"]))
        elif s["type"] == "D" and not inner.startswith("[") and "[" not in inner:
            continue
    m = re.search(r"\[?Proposed deletions are enclosed in brackets[^\n]*", c.text)
    if m and not any(s["type"] == "D" and s["o1"] <= m.start() + 1 for s in c.spans if s["c0"] >= m.end() - 2):
        out.append(F("st_ct_brackets", "rules", "error", "CT explanation line is not tagged",
                     "The bracketed explanation line at the end is tagged as a deletion.", m.start(), m.end()))
    for li, raw in enumerate(c.lines):
        if "General Assembly" in raw and re.search(r"Bill No", raw):
            s, e = c.line_span(li)
            out.append(F("st_ct_brackets", "rules", "warning", "CT header elements share a line",
                         "The bill number and “General Assembly” must be on separate lines.", s, e))
        if "|" in raw:
            s, e = c.line_span(li)
            out.append(F("st_ct_brackets", "rules", "warning", "Pipe character in table",
                         "Do not add pipe characters to preserve column alignment — use spacing.", s, e))
    return out[:MAX_PER_RULE]


@rule("st_bracket_policy", "rules", "Bracket deletions: brackets kept or removed",
      "KY and MI remove the brackets once the text is wrapped in [D>; other bracket states keep "
      "unspecified bracketed text under review.", states="KY,MI,MD,PA,UT,NJ,NY,OR,NV,WA,NM,MO",
      covers=("brackets are removed", "remove all brackets"))
def _bracket_policy(c: Ctx):
    out = []
    if c.state in ("KY", "MI"):
        for s in c.spans:
            if s["type"] == "D":
                inner = c.text[s["o1"]:s["c0"]]
                m = re.match(r"\s*\[", inner)
                if m or re.search(r"\]\s*$", inner):
                    out.append(F("st_bracket_policy", "rules", "warning", "Brackets left inside a deletion tag",
                                 f"{c.state} removes the bracket characters when the text is tagged [D>.",
                                 s["o0"], s["c1"]))
    else:
        skip = re.compile(r"(?i)^\[(?:effective|expires|proposed|sic|reserved|note)")
        for m in re.finditer(r"(?<![\[<])\[(?![AD]>)[^\[\]\n]{3,300}\](?!>)", c.text):
            if skip.match(m.group(0)) or any(
                    s["type"] == "D" and s["o1"] <= m.start() and m.end() <= s["c0"] for s in c.spans):
                continue
            out.append(F("st_bracket_policy", "rules", "info", "Bracketed text is not tagged",
                         f"{c.state} marks deletions with brackets; if this is deleted text it should be "
                         "tagged [D>…<D].", m.start(), m.end()))
    return out[:MAX_PER_RULE]


@rule("st_dc_quotes", "rules", "DC: underline tag inside quotation marks",
      "Underline tags are added only when the text is visibly underlined, never inside quotation "
      "marks (DC).", states="DC", covers=("quotation marks",))
def _dc(c: Ctx):
    out = []
    for s in c.spans:
        if s["type"] != "A":
            continue
        line_start = c.starts[c.line_of(s["o0"])]
        before = c.text[line_start:s["o0"]]
        if before.count('"') % 2 == 1:
            out.append(F("st_dc_quotes", "rules", "warning", "Addition tag inside quotation marks",
                         "DC does not underline-tag text inside quotation marks.", s["o0"], s["c1"]))
    return out[:MAX_PER_RULE]


_AR_HDR = [
    r"Stricken language would be deleted from and underlined language would be added to present law\.?",
    r"Act \d+ of the \w+ Session", r"(?:HOUSE|SENATE)(?: JOINT)? (?:BILL|RESOLUTION) \d+", r"A Bill|An Act",
    r"State of Arkansas", r"\d+(?:st|nd|rd|th) General Assembly", r"(?:Regular|Fiscal|First Extraordinary) Session, \d{4}",
]


@rule("st_ar_header", "rules", "AR: page-1 header order and layout",
      "Arkansas page 1 lists, each on its own line, in this order: explanation line, Act #, bill "
      "number, A Bill, State of Arkansas, ##th General Assembly, session. Appropriation-table "
      "underlines (column titles, ALL-CAPS headings, bare dollar amounts) are not additions.",
      states="AR", covers=("header_order", "keep the first line"))
def _ar(c: Ctx):
    out = []
    head = c.text[: c.paras[min(6, len(c.paras) - 1)][1]] if c.paras else ""
    found = []
    for li, raw in enumerate(head.splitlines()):
        plain = _plain(raw).strip()
        # A header element is a WHOLE line ("For An Act To Be Entitled" is not the "An Act" line),
        # but columns that were glued onto one line are split at long whitespace runs first.
        frags = [f for f in re.split(r"\s{8,}", plain) if f]
        hits = [i for f in frags for i, p in enumerate(_AR_HDR) if re.fullmatch(p, f)]
        if len(frags) > 1 and len(hits) > 1:
            s, e = c.line_span(li)
            out.append(F("st_ar_header", "rules", "error", "AR header elements share a line",
                         "Each header element sits on its own line (split at long whitespace runs).", s, e))
        elif len(hits) == 1 and len(frags) == 1:
            found.append((hits[0], li))
    order = [i for i, _ in found]
    if order != sorted(order):
        li = found[[a > b for a, b in zip(order, order[1:])].index(True) + 1][1]
        s, e = c.line_span(li)
        out.append(F("st_ar_header", "rules", "warning", "AR header out of order",
                     "Expected: explanation → Act # → bill no. → A Bill → State → Assembly → Session.", s, e))
    for s in c.spans:
        li = c.line_of(s["o0"])
        if re.search(r"\S {6,}\S", c.line(li)):
            inner = _plain(c.text[s["o1"]:s["c0"]]).strip()
            if re.fullmatch(r"[\$\d,.\s]+", inner) or (inner.isupper() and len(inner) > 12):
                out.append(F("st_ar_header", "rules", "warning", "Decorative underline in appropriation table",
                             "Underline in an appropriation table row is typographic emphasis, not an addition.",
                             s["o0"], s["c1"]))
    return out[:MAX_PER_RULE]


# ═══════════════════════════════ Entry points ════════════════════════════════

# ── Page artifacts inside the source extraction ─────────────────────────────
#
# The raw PDF extraction contains page furniture (timestamps, barcodes, "Page 1 of 4")
# that the conversion is *supposed* to drop — sometimes right in the middle of a word
# ("one-" + footer + "year"). A difference that is only that furniture is not a defect.

_INLINE_ARTIFACTS = [
    # Delaware-style tracking footer: "DLS : MJC : AF 4801530050 Released: 01/16/2025 10:46 AM"
    (r"(?:\b[A-Z]{2,4}\s*:\s*[A-Z]{2,4}\s*:\s*[A-Z]{1,3}\s+\d{6,}\s*)?(?i:Released:)\s*\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}(?::\d{2})?\s*[AP]M",
     "a page footer (tracking id and release timestamp)"),
    (r"\b[A-Z]{2,4}\s*:\s*[A-Z]{2,4}\s*:\s*[A-Z]{1,3}\s+\d{6,}\b", "a page footer (tracking id)"),
    (r"\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}(?::\d{2})?\s*[AP]M(?:\s+[A-Z]{2,5}\d{2,5})?", "a scan timestamp footer"),
    (r"\*[A-Z]{1,4}\d+(?:\.\d+)?\*", "a scan barcode"),
    (r"(?i)\bPage \d+ of \d+\b", "a page-number footer"),
    (r"\b(?:IN|SB|HB|EH|ES|HR|SR)\s*\d+—LS\s*\d+/DI\s*\d+\b", "a bill-reference footer"),
    (r"\b\d{2}-\d{5}[A-Z]-\d{2}\b", "an internal reference number"),
    (r"(?i)CODING:\s*Words stricken are deletions[^\n]*", "a CODING footer"),
    # Colorado page-1 legend (the NetScan spec says to delete it)
    (r"(?i)(?:Shading denotes|Capital letters or bold|Dashes through)[^\n]*", "the Colorado first-page legend footer"),
]


def _squash(s: str) -> str:
    """Compare words, not layout: ignore case, spacing and a hyphen broken across lines/pages."""
    return re.sub(r"\s+", "", re.sub(r"-\s+", "-", s)).lower()


def explain_artifact(expected: str, output: str) -> Optional[str]:
    """If a source-vs-output difference is only page furniture, say which; else None."""
    if not expected:
        return None
    if output.strip() and _squash(expected) == _squash(output):
        return "only spacing differs"  # e.g. "( 4 )( d)" vs "(4)(d)"
    found: list[str] = []
    cleaned = expected
    for rx, label in _INLINE_ARTIFACTS:
        cleaned, n = re.subn(rx, " ", cleaned)
        if n:
            found.append(label)
    if not found:
        return None
    leftover = _squash(cleaned)
    # what is left may be just the margin line number printed next to the footer ("... 10:46 AM 2096")
    if (not leftover or re.fullmatch(r"\d{1,4}", leftover)) and not output.strip():
        return found[0]  # the whole "missing text" is the furniture
    if output.strip() and _squash(cleaned) == _squash(output):
        return found[0] + " inside the text"
    return None


def check_all(text: str, state: Optional[str], reference: Optional[str] = None) -> list[dict[str, Any]]:
    c = Ctx(text, state, reference)
    findings: list[dict[str, Any]] = []
    for r in RULES:
        if r["states"] is not None and c.state not in r["states"]:
            continue
        for f in r["fn"](c):
            f["scope"] = c.state if (r["states"] is not None and c.state) else "ALL"
            findings.append(f)
    findings.sort(key=lambda f: (f["start"], f["rule"]))
    return findings


def overview() -> dict[str, Any]:
    """Every state's rules in one place for the Rules tab: what the spec says, what is checked automatically,
    and what only a person can check against the PDF. Global rules (every state) are listed once."""
    states = []
    for code in sorted(load_spec().get("states", {})):
        cat = catalog(code, [])
        spec = state_spec(code) or {}
        states.append({
            "code": code,
            "spec": cat["spec"],
            "instructions": {
                "add": list((spec.get("add_markup") or {}).get("custom_rules") or []),
                "delete": list((spec.get("delete_markup") or {}).get("custom_rules") or []),
            },
            "rules": [r for r in cat["rules"] if r["scope"] != "ALL"],   # the ones that exist only for this state
            "manual": cat["manual"],
        })
    glob = [{"id": r["id"], "title": r["title"], "requirement": r["requirement"], "scope": "ALL", "source": r["source"]}
            for r in RULES if r["states"] is None]
    return {"global": glob, "states": states}


def catalog(state: Optional[str], findings: list[dict[str, Any]]) -> dict[str, Any]:
    """What ran for this jurisdiction, and what still needs a human."""
    counts: dict[str, int] = {}
    for f in findings:
        counts[f["rule"]] = counts.get(f["rule"], 0) + 1
    st = (state or "").upper()
    spec = state_spec(st)
    ran = [{"id": r["id"], "title": r["title"], "requirement": r["requirement"],
            "scope": "ALL" if r["states"] is None else st, "source": r["source"],
            "findings": counts.get(r["id"], 0)}
           for r in RULES if r["states"] is None or st in r["states"]]
    keywords = [kw.lower() for r in RULES if r["states"] is not None and st in r["states"] for kw in r["covers"]]
    manual: list[dict[str, Any]] = []
    describe = None
    if spec:
        add = spec.get("add_markup", {})
        dele = spec.get("delete_markup", {})
        addk = [k for k in ("underline", "bold", "italic", "all_caps", "brackets") if add.get(k)]
        delk = [k for k in ("strike_out", "brackets", "italic", "bold") if dele.get(k)]
        describe = {
            "name": spec.get("state_name"), "add": addk, "delete": delk,
            "remove": spec.get("deletions", ""), "file_type": spec.get("file_type", ""),
        }
        for kind, mk in (("add", add), ("delete", dele)):
            for text in mk.get("custom_rules", []) or []:
                low = text.lower()
                if not any(kw in low for kw in keywords):
                    manual.append({"kind": kind, "text": text})
        if add.get("all_caps_condition"):
            describe["all_caps_condition"] = add["all_caps_condition"]
        if spec.get("no_tag_clauses"):
            describe["no_tag_clauses"] = spec["no_tag_clauses"]
        if spec.get("tag_scope"):
            describe["tag_scope"] = spec["tag_scope"]
    return {"state": st or None, "spec": describe, "rules": ran, "manual": manual,
            "formatting_note": "Underline vs bold vs italic vs strike-out cannot be read from text; "
                               "verify those against the PDF using the highlighted boxes."}
