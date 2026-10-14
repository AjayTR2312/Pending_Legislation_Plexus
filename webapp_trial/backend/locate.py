"""
Pin every validation finding to an exact position.

The checkers report *what* is wrong (a dropped phrase, a malformed tag, a layout
break) but not *where*. For human review that is the expensive part, so this
module resolves each finding to:

  * a word range in the converted text   (line, word) .. (end_line, end_word)
  * and/or boxes on the source PDF       page + normalised x/y/w/h

Content drops exist only in the source, so they are found in the PDF word list
and anchored to the nearest aligned output word. Everything else is found in the
output text and mapped to the PDF through the compare alignment.
"""

from __future__ import annotations

import bisect
import re
from typing import Any, Optional

import compare
import rules
from appconfig import settings

_TAG_RE = re.compile(r"\[A>|<A\]|\[D>|<D\]")

MAX_BOXES = settings.REVIEW_MAX_BOXES
MAX_REVISIONS = settings.REVIEW_MAX_REVISIONS


class _Doc:
    """Output text tokenised the same way compare.py keys its alignment."""

    def __init__(self, text: str):
        self.lines = text.splitlines(keepends=True)
        self.line_starts: list[int] = []
        pos = 0
        for raw in self.lines:
            self.line_starts.append(pos)
            pos += len(raw)
        self.norm: list[str] = []
        self.keys: list[tuple[int, int]] = []
        self.index: dict[str, list[int]] = {}
        for li, raw in enumerate(self.lines):
            for wi, word in enumerate(raw.rstrip("\r\n").split(" ")):
                n = compare._normalise(_TAG_RE.sub("", word))
                if n:
                    self.index.setdefault(n, []).append(len(self.norm))
                    self.norm.append(n)
                    self.keys.append((li, wi))

    def offset_to_pos(self, offset: int) -> tuple[int, int]:
        li = max(bisect.bisect_right(self.line_starts, offset) - 1, 0)
        col = offset - self.line_starts[li]
        return li, self.lines[li][:col].count(" ") if self.lines else 0


def _norm_tokens(text: str) -> list[str]:
    return [n for n in (compare._normalise(_TAG_RE.sub("", w)) for w in text.split()) if n]


def _find(hay: list[str], index: dict[str, list[int]], needle: list[str], after: int = 0
          ) -> tuple[int, int]:
    """First occurrence of needle (or its longest usable prefix) at/after `after`."""
    if not needle:
        return -1, 0
    takes = sorted({len(needle), min(len(needle), 12), min(len(needle), 6),
                    min(len(needle), 3), min(len(needle), 1)}, reverse=True)
    for take in takes:
        sub = needle[:take]
        positions = index.get(sub[0], [])
        for start in (after, 0):
            for pos in positions:
                if pos < start:
                    continue
                if hay[pos:pos + take] == sub:
                    return pos, take
            if after == 0:
                break
    return -1, 0


def _pdf_index(words: list[list[Any]]) -> dict[str, list[int]]:
    idx: dict[str, list[int]] = {}
    for i, w in enumerate(words):
        idx.setdefault(w[0], []).append(i)
    return idx


def _boxes(words: list[list[Any]], start: int, count: int) -> list[dict[str, Any]]:
    out = []
    for w in words[start:start + min(count, MAX_BOXES)]:
        out.append({"page": w[1], "x": w[2], "y": w[3], "w": w[4], "h": w[5]})
    return out


class _Anchor:
    """Nearest aligned output word for a PDF word index (drops live only in the PDF)."""

    def __init__(self, cmap: dict[str, dict[str, Any]]):
        pairs = sorted(
            (v["i"], tuple(int(x) for x in k.split("_")))
            for k, v in cmap.items() if "i" in v
        )
        self.pdf_idx = [p[0] for p in pairs]
        self.keys = [p[1] for p in pairs]

    def at(self, pdf_i: int) -> tuple[int, int]:
        if not self.keys:
            return 0, 0
        j = bisect.bisect_right(self.pdf_idx, pdf_i) - 1
        li, wi = self.keys[max(j, 0)]
        return li, wi


def _marker(**kw: Any) -> dict[str, Any]:
    base = {
        "issue_id": None, "line": 0, "w0": None, "end_line": None, "w1": None,
        "page": None, "boxes": [], "expected": "", "output": "", "detail": "",
        "insertion": False, "rule": None, "scope": None, "parent": None, "children": 0,
    }
    base.update(kw)
    return base


def _text_range(doc: _Doc, start: int, count: int) -> tuple[int, int, int, int]:
    end = min(start + count, len(doc.keys)) - 1
    (l0, w0), (l1, w1) = doc.keys[start], doc.keys[max(end, start)]
    return l0, w0, l1, w1


# ── Content issues ───────────────────────────────────────────────────────────

def _content_markers(report, doc, words, pdf_idx, anchor, cmap) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    t_cursor = p_cursor = 0
    for issue in report.get("issues", []):
        expected, output = issue.get("expected") or "", issue.get("output") or ""
        sev = "error" if issue.get("severity") == "critical" else "warning"
        cat = issue.get("category") or "content"
        title = {
            "content_drop": "Missing text",
            "paragraph_drop": "Missing paragraph",
            "content_added": "Extra text",
            "content_reordered": "Same words, different order",
        }.get(cat, "Text changed" if cat != "content" else "Content difference")
        m = _marker(
            id=f"c{issue['id']}", source="content", kind=cat, severity=sev,
            title=title, issue_id=issue["id"], expected=expected, output=output,
            detail=issue.get("why", ""),  # the source/output texts are shown side by side under it, not repeated here
        )

        found_text = False
        if output:
            pos, take = _find(doc.norm, doc.index, _norm_tokens(output), t_cursor)
            if pos >= 0:
                n = max(len(_norm_tokens(output)), take)
                l0, w0, l1, w1 = _text_range(doc, pos, n)
                m.update(line=l0, w0=w0, end_line=l1, w1=w1)
                t_cursor = pos
                found_text = True
                # Aligned output words already know their PDF boxes.
                boxes = [cmap[f"{a}_{b}"] for a, b in doc.keys[pos:pos + n][:MAX_BOXES]
                         if f"{a}_{b}" in cmap]
                if boxes:
                    m["page"] = boxes[0]["page"]
                    m["boxes"] = [{k: b[k] for k in ("page", "x", "y", "w", "h")} for b in boxes]

        if expected and not m["boxes"]:
            exp_tokens = _norm_tokens(expected)
            pos, take = _find([w[0] for w in words], pdf_idx, exp_tokens, p_cursor)
            if pos >= 0:
                n = max(len(exp_tokens), take)
                m["boxes"] = _boxes(words, pos, n)
                m["page"] = words[pos][1]
                p_cursor = pos
                if not found_text:
                    li, wi = anchor.at(pos)
                    m.update(line=li, w0=wi, insertion=True)

        out.append(m)
    return out


def _contrast(expected: str, output: str) -> str:
    """One plain sentence: what the source says and what the output says."""
    clip = lambda s: (s[:90] + "…") if len(s) > 90 else s  # noqa: E731
    if expected and output:
        return f"Source: “{clip(expected)}”. Output: “{clip(output)}”."
    if expected:
        return f"Source: “{clip(expected)}”."
    if output:
        return f"Output: “{clip(output)}”."
    return ""


def _layout_plausible(doc, line_idx: int, description: str) -> Optional[str]:
    """The PDF-geometry checker says "new paragraph starts at position N of this line".
    If the text before N does not end a sentence and the text after N is not a list/section
    marker, it is almost certainly just a wrapped line — say so instead of crying merge."""
    m = re.search(r"position (\d+)", description or "")
    if not m or line_idx >= len(doc.lines):
        return None
    pos = int(m.group(1))
    line = doc.lines[line_idx].rstrip("\r\n")
    before, after = rules._plain(line[:pos]).rstrip(), rules._plain(line[pos:]).lstrip()
    if not before or not after:
        return None
    if before[-1] in ".;:?!\"”')" or re.match(r"(?:\(\w{1,3}\)|\w\.|Sec(?:tion|\.)|SECTION)\b", after):
        return None
    return (f"The text before it (“…{before[-30:]}”) does not end a sentence and what follows is not a "
            "list item or section heading, so this is probably one paragraph wrapped over two PDF lines.")


# ── Layout issues ────────────────────────────────────────────────────────────

def _layout_markers(report, doc, cmap) -> list[dict[str, Any]]:
    out = []
    for n, row in enumerate(report.get("layout_issues", [])):
        line = row.get("line")
        if line is None:
            continue
        li = min(max(int(line) - 1, 0), max(len(doc.lines) - 1, 0))
        kind = str(row.get("kind") or "layout")
        title = {"merge": "Paragraphs wrongly merged", "split": "Paragraph wrongly split"}.get(
            kind.split("_")[0], f"Layout: {kind}")
        desc = row.get("description") or row.get("context") or ""
        doubt = _layout_plausible(doc, li, desc) if kind.startswith(("merge", "split")) else None
        likely_ok = bool(row.get("likely_ok"))
        if likely_ok:
            doubt = ("Both lines are short and each stands on its own, so this is probably an address, signature or "
                     "heading block that is meant to be line by line. Check it against the PDF.")
        m = _marker(
            id=f"l{n}", source="layout", kind=kind, severity="info" if doubt else "warning",
            title=("Short lines kept apart — probably intentional" if likely_ok
                   else "Possible paragraph break — probably a wrapped line" if doubt else title),
            line=li, detail=(desc + (" " + doubt if doubt else "")).strip(),
        )
        # Anchor boxes to the first aligned word on that line.
        for wi in range(len(doc.lines[li].split(" ")) if doc.lines else 0):
            box = cmap.get(f"{li}_{wi}")
            if box:
                m["page"] = box["page"]
                m["boxes"] = [{k: box[k] for k in ("page", "x", "y", "w", "h")}]
                break
        out.append(m)
    return out


# ── Rule findings (paragraph / tag structure / state rules) ───────────────────

def _span_marker(doc, cmap, mid, f) -> dict[str, Any]:
    """A rules.py finding (character range) -> marker with word range and PDF boxes."""
    start, end = f["start"], f["end"]
    l0, w0 = doc.offset_to_pos(start)
    l1, w1 = doc.offset_to_pos(max(end - 1, start))
    m = _marker(id=mid, source=f["source"], kind=f["rule"], severity=f["severity"],
                title=f["title"], detail=f["detail"], line=l0, w0=w0, end_line=l1, w1=w1,
                rule=f["rule"], scope=f.get("scope"))
    boxes = []
    for li in range(l0, min(l1, l0 + 60) + 1):
        for wi in range(len(doc.lines[li].split(" "))):
            if li == l0 and wi < w0:
                continue
            if li == l1 and wi > w1:
                break
            box = cmap.get(f"{li}_{wi}")
            if box:
                boxes.append({k: box[k] for k in ("page", "x", "y", "w", "h")})
            if len(boxes) >= MAX_BOXES:
                break
    if boxes:
        m["page"] = boxes[0]["page"]
        m["boxes"] = boxes
    return m


_DRIFT_TITLES = {
    "tag_dropped": ("Source formatting lost",
                    "These words were underlined/struck in the source PDF but carry no tag in the output."),
    "tag_added": ("Plain source words tagged",
                  "These words have no formatting in the source PDF but sit inside a tag in the output."),
    "tag_type_changed": ("Tag type changed",
                         "The source marks these words as one type (add/delete) and the output as the other."),
}


def _drift_markers(report, doc, cmap) -> list[dict[str, Any]]:
    """Word-level tag drift against the source PDF (the real tag-correctness check)."""
    drift = (report.get("integrity") or {}).get("boundary_drift") or {}
    out = []
    cursor = 0
    for n, f in enumerate(drift.get("findings") or []):
        title, why = _DRIFT_TITLES.get(f.get("kind"), ("Tag drift", ""))
        tokens = _norm_tokens(f.get("text", ""))
        pos, take = _find(doc.norm, doc.index, tokens[:12], cursor)
        if pos < 0:
            continue
        cursor = pos
        span = max(min(int(f.get("words") or len(tokens)), len(tokens)), take, 1)
        l0, w0, l1, w1 = _text_range(doc, pos, span)
        m = _marker(id=f"d{n}", source="tags", kind=f.get("kind", "tag_drift"), severity="error",
                    title=title, detail=f"{why} {f.get('words', '?')} word(s): “{f.get('text', '')[:120]}”",
                    line=l0, w0=w0, end_line=l1, w1=w1, rule="tag_drift", scope="ALL")
        boxes = [cmap[f"{a}_{b}"] for a, b in doc.keys[pos:pos + span][:MAX_BOXES] if f"{a}_{b}" in cmap]
        if boxes:
            m["page"] = boxes[0]["page"]
            m["boxes"] = [{k: b[k] for k in ("page", "x", "y", "w", "h")} for b in boxes]
        out.append(m)
    return out


def _revision_markers(text, doc, cmap, state_code) -> list[dict[str, Any]]:
    """Every marked revision ([A>…<A] addition, [D>…<D] deletion) as a checklist item.

    These are not defects. They let a reviewer step through each revision, see it on
    the PDF and confirm it against the source, next to the real findings.
    """
    spec = rules.state_spec(state_code) or {}
    add_kinds = [k for k in ("underline", "bold", "italic", "all_caps", "brackets") if (spec.get("add_markup") or {}).get(k)]
    del_kinds = [k for k in ("strike_out", "brackets") if (spec.get("delete_markup") or {}).get(k)]
    spans, _ = rules.parse_tags(text)
    out = []
    for i, s in enumerate(spans[:MAX_REVISIONS]):
        is_add = s["type"] == "A"
        kinds = add_kinds if is_add else del_kinds
        inner = re.sub(r"\s+", " ", rules._plain(text[s["o1"]:s["c0"]])).strip()
        f = {
            "rule": "revision", "source": "revisions", "severity": "info", "scope": "ALL",
            "title": "Addition" if is_add else "Deletion",
            "detail": ("Added text" if is_add else "Deleted text")
                      + (f" ({' / '.join(kinds).replace('_', ' ')} in the source)" if kinds else "")
                      + f": “{inner[:160]}{'…' if len(inner) > 160 else ''}”",
            "start": s["o0"], "end": s["c1"],
        }
        m = _span_marker(doc, cmap, f"v{i}", f)
        m["kind"] = "revision_add" if is_add else "revision_del"
        m["output"] = inner[:300]
        out.append(m)
    return out


# ── Entry point ──────────────────────────────────────────────────────────────

def locate_all(
    text: str,
    pdf_path: str,
    text_path: str,
    report: dict[str, Any],
    precleaned: Optional[str],
    state_code: Optional[str] = None,
) -> dict[str, Any]:
    data = compare.build_compare(pdf_path, text_path, text)
    words = compare.load_words(data["key"])
    cmap = data["map"]
    doc = _Doc(text)
    pdf_idx = _pdf_index(words)
    anchor = _Anchor(cmap)

    findings = rules.check_all(text, state_code, precleaned)
    rule_markers = [_span_marker(doc, cmap, f"r{i}", f) for i, f in enumerate(findings)]

    markers = (
        _content_markers(report, doc, words, pdf_idx, anchor, cmap)
        + _layout_markers(report, doc, cmap)
        + _drift_markers(report, doc, cmap)
        + rule_markers
        + _revision_markers(text, doc, cmap, state_code)
    )
    markers.sort(key=lambda m: (m["line"], m["w0"] or 0))
    return {
        "markers": markers,
        "compare_key": data["key"],
        "catalog": rules.catalog(state_code, findings),
    }
