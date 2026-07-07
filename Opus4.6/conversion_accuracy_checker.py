"""
conversion_accuracy_checker.py — Deterministic Conversion Accuracy Validator
=============================================================================

Checks the accuracy of PDF→Text conversion with state-specific rules.
Produces a CONFIDENCE PERCENTAGE based on multiple measurable dimensions:

  1. Word-level accuracy (SequenceMatcher — exact token matching)
  2. Tag integrity (balanced [A>...<A] and [D>...<D] counts)
  3. Content completeness (no dropped sections/pages)
  4. State-specific artifact removal validation
  5. Structural formatting checks

NO LLM involved — 100% deterministic and reproducible.

Usage:
    python conversion_accuracy_checker.py <pymupdf_clean.txt> <converted.txt> --state CA

    # Or programmatically:
    from conversion_accuracy_checker import check_conversion_accuracy
    result = check_conversion_accuracy(pymupdf_text, converted_text, state="CA")
    print(f"Confidence: {result['confidence_pct']}%")
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# ═══════════════════════════════════════════════════════════════════
# TAG PATTERNS
# ═══════════════════════════════════════════════════════════════════
_ADD_OPEN, _ADD_CLOSE = "[A>", "<A]"
_DEL_OPEN, _DEL_CLOSE = "[D>", "<D]"
_TAG_RE = re.compile(r"\[([AD])>(.*?)<\1\]", re.DOTALL)


# ═══════════════════════════════════════════════════════════════════
# STATE-SPECIFIC RULES
# ═══════════════════════════════════════════════════════════════════

STATE_RULES: Dict[str, dict] = {
    "CA": {
        "name": "California",
        "line_numbers": True,       # must be removed
        "page1_header_preserve": ["CALIFORNIA LEGISLATURE", "REGULAR SESSION"],
        "page2plus_headers_remove": True,
        "footer_patterns": [r"^\d+$", r"^-\d+-$"],
        "section_pattern": r"(?:SEC(?:TION)?\.?\s*\d+|SECTION\s+\d+)",
        "known_artifacts": [r"^line\s+\d+\s*$"],
    },
    "CT": {
        "name": "Connecticut",
        "line_numbers": True,
        "page1_header_preserve": ["Raised Bill", "General Assembly"],
        "page2plus_headers_remove": True,
        "footer_patterns": [r"^\d+$", r"^-\d+-$", r"^LCO\s+\d+"],
        "section_pattern": r"(?:Sec(?:tion)?\.?\s*\d+)",
        "known_artifacts": [r"^LCO\s+No\.\s+\d+", r"^\d+\s+of\s+\d+$"],
    },
    "DE": {
        "name": "Delaware",
        "line_numbers": True,
        "page1_header_preserve": ["GENERAL ASSEMBLY", "DELAWARE"],
        "page2plus_headers_remove": True,
        "footer_patterns": [r"^\d+$", r"^Page\s+\d+"],
        "section_pattern": r"(?:Section\s+\d+|§\s*\d+)",
        "known_artifacts": [],
    },
    "FL": {
        "name": "Florida",
        "line_numbers": True,
        "page1_header_preserve": ["Florida", "Legislature"],
        "page2plus_headers_remove": True,
        "footer_patterns": [r"^\d+$", r"^Page\s+\d+\s+of\s+\d+"],
        "section_pattern": r"(?:Section\s+\d+)",
        "known_artifacts": [r"^CODING:"],
    },
    "CO": {
        "name": "Colorado",
        "line_numbers": True,
        "page1_header_preserve": ["Colorado", "General Assembly"],
        "page2plus_headers_remove": True,
        "footer_patterns": [r"^\d+$", r"^-\d+-$"],
        "section_pattern": r"(?:SECTION\s+\d+|\d+-\d+-\d+)",
        "known_artifacts": [],
    },
    "DC": {
        "name": "District of Columbia",
        "line_numbers": True,
        "page1_header_preserve": ["Council of the District of Columbia"],
        "page2plus_headers_remove": True,
        "footer_patterns": [r"^\d+$"],
        "section_pattern": r"(?:Sec(?:tion)?\.?\s*\d+|§\s*\d+)",
        "known_artifacts": [],
    },
    "IA": {
        "name": "Iowa",
        "line_numbers": True,
        "page1_header_preserve": ["Iowa", "General Assembly"],
        "page2plus_headers_remove": True,
        "footer_patterns": [r"^\d+$", r"^-\d+-$"],
        "section_pattern": r"(?:Sec(?:tion)?\.?\s*\d+)",
        "known_artifacts": [],
    },
    "IN": {
        "name": "Indiana",
        "line_numbers": True,
        "page1_header_preserve": ["Indiana", "General Assembly"],
        "page2plus_headers_remove": True,
        "footer_patterns": [r"^\d+$"],
        "section_pattern": r"(?:Sec(?:tion)?\.?\s*\d+|IC\s+\d+)",
        "known_artifacts": [],
    },
    "KS": {
        "name": "Kansas",
        "line_numbers": True,
        "page1_header_preserve": ["Kansas", "Legislature"],
        "page2plus_headers_remove": True,
        "footer_patterns": [r"^\d+$"],
        "section_pattern": r"(?:Sec(?:tion)?\.?\s*\d+|K\.S\.A\.)",
        "known_artifacts": [],
    },
    "AR": {
        "name": "Arkansas",
        "line_numbers": True,
        "page1_header_preserve": ["Arkansas", "General Assembly"],
        "page2plus_headers_remove": True,
        "footer_patterns": [r"^\d+$"],
        "section_pattern": r"(?:SECTION\s+\d+|§\s*\d+)",
        "known_artifacts": [],
    },
}


# ═══════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════

# Unicode normalization map: smart quotes, dashes, special chars → ASCII equivalents
_UNICODE_NORM_MAP = str.maketrans({
    '\u2014': '-',   # em-dash → hyphen
    '\u2013': '-',   # en-dash → hyphen
    '\u2012': '-',   # figure dash
    '\u2015': '-',   # horizontal bar
    '\u201c': '"',   # left double quote
    '\u201d': '"',   # right double quote
    '\u2018': "'",   # left single quote
    '\u2019': "'",   # right single quote
    '\u2026': '...',  # ellipsis (handled below)
    '\u00a0': ' ',   # non-breaking space
    '\u200b': '',    # zero-width space
    '\u00ad': '',    # soft hyphen
    '\ufeff': '',    # BOM
})

# Additional multi-char replacements
_MULTI_CHAR_NORMS = [
    ('\u2026', '...'),   # ellipsis
    ('--', '-'),         # double-hyphen → single (after em-dash norm)
]


def _normalize_unicode(text: str) -> str:
    """Normalize unicode punctuation to ASCII equivalents for fair comparison."""
    text = text.translate(_UNICODE_NORM_MAP)
    for old, new in _MULTI_CHAR_NORMS:
        text = text.replace(old, new)
    return text


def _normalize_word(word: str) -> str:
    """Normalize a single word: lowercase, strip surrounding punct, normalize unicode."""
    w = _normalize_unicode(word).lower()
    # Strip leading/trailing punctuation for comparison
    return w.strip('.,;:!?\'"()[]{}\u2014\u2013-/\\@#$%^&*+=<>~`')


def strip_tags(text: str) -> str:
    """Remove all [A>...<A] and [D>...<D] tag delimiters, keeping inner content."""
    text = text.replace(_ADD_OPEN, "").replace(_ADD_CLOSE, "")
    text = text.replace(_DEL_OPEN, "").replace(_DEL_CLOSE, "")
    return text


def extract_words(text: str) -> List[str]:
    """Extract normalized word list from text after stripping tags."""
    cleaned = strip_tags(text)
    normalized = re.sub(r"\s+", " ", cleaned).strip()
    return normalized.split() if normalized else []


def _extract_normalized_words(text: str) -> List[str]:
    """Extract words with unicode normalization applied for fair comparison."""
    cleaned = strip_tags(text)
    cleaned = _normalize_unicode(cleaned)
    normalized = re.sub(r"\s+", " ", cleaned).strip()
    return normalized.split() if normalized else []


def count_tags(text: str) -> Dict[str, int]:
    """Count tag openers and closers."""
    return {
        "add_open": text.count(_ADD_OPEN),
        "add_close": text.count(_ADD_CLOSE),
        "del_open": text.count(_DEL_OPEN),
        "del_close": text.count(_DEL_CLOSE),
    }


def extract_tagged_spans(text: str) -> List[Dict]:
    """Extract all tagged spans with their type and content."""
    spans = []
    for m in _TAG_RE.finditer(text):
        tag_type = "addition" if m.group(1) == "A" else "deletion"
        spans.append({
            "type": tag_type,
            "text": m.group(2),
            "start": m.start(),
            "end": m.end(),
        })
    return spans


def find_sections(text: str, pattern: str) -> List[str]:
    """Find all section numbers/references in text."""
    return re.findall(pattern, text, re.IGNORECASE)


# ═══════════════════════════════════════════════════════════════════
# DIMENSION 1: WORD-LEVEL ACCURACY
# ═══════════════════════════════════════════════════════════════════

def _is_punct_only_diff(word_a: str, word_b: str) -> bool:
    """Check if two words differ only in punctuation (not actual content)."""
    a_norm = re.sub(r'[^\w]', '', _normalize_unicode(word_a).lower())
    b_norm = re.sub(r'[^\w]', '', _normalize_unicode(word_b).lower())
    return a_norm == b_norm and a_norm != ''


def check_word_accuracy(pymupdf_text: str, converted_text: str) -> Dict:
    """
    Word-level comparison with unicode normalization and reorder detection.
    Returns accuracy as matching_words / max(total_words).

    Handles:
    - Spacing-only diffs (word split/join)
    - Punctuation-only diffs (em-dash vs --, smart quotes vs straight)
    - Reordered content (same words, different position)
    """
    # Use normalized words for matching (fair comparison)
    pymupdf_words_raw = extract_words(pymupdf_text)
    converted_words_raw = extract_words(converted_text)
    pymupdf_words = _extract_normalized_words(pymupdf_text)
    converted_words = _extract_normalized_words(converted_text)

    if not pymupdf_words and not converted_words:
        return {"score": 100.0, "matching": 0, "pymupdf_count": 0,
                "converted_count": 0, "details": "Both empty",
                "diff_count": 0, "diffs": [], "reorder_detected": False,
                "punct_only_diffs": 0}

    matcher = difflib.SequenceMatcher(None, pymupdf_words, converted_words)
    matching = sum(i2 - i1 for tag, i1, i2, j1, j2 in matcher.get_opcodes()
                   if tag == "equal")

    spacing_bonus = 0
    punct_only_diffs = 0
    reorder_words = 0

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace":
            p_seg = pymupdf_words[i1:i2]
            c_seg = converted_words[j1:j2]
            # Spacing-only: joined characters identical
            p_joined = "".join(p_seg)
            c_joined = "".join(c_seg)
            if p_joined == c_joined:
                spacing_bonus += max(i2 - i1, j2 - j1)
                continue
            # Punctuation-only per word
            if len(p_seg) == len(c_seg):
                all_punct = True
                for pw, cw in zip(p_seg, c_seg):
                    if not _is_punct_only_diff(pw, cw):
                        all_punct = False
                        break
                if all_punct:
                    punct_only_diffs += len(p_seg)
                    continue
        # Check for reordering: content exists elsewhere in the other list
        if tag in ('delete', 'replace'):
            p_seg = pymupdf_words[i1:i2]
            # Check if these words exist as a contiguous block in converted
            p_str = ' '.join(p_seg)
            c_full = ' '.join(converted_words)
            if p_str in c_full:
                reorder_words += len(p_seg)
        if tag in ('insert', 'replace'):
            c_seg = converted_words[j1:j2]
            c_str = ' '.join(c_seg)
            p_full = ' '.join(pymupdf_words)
            if c_str in p_full:
                reorder_words += len(c_seg)

    # Reorder detection: use bag-of-words overlap
    pymupdf_bag = sorted(pymupdf_words)
    converted_bag = sorted(converted_words)
    bag_matcher = difflib.SequenceMatcher(None, pymupdf_bag, converted_bag)
    bag_matching = sum(i2 - i1 for tag, i1, i2, j1, j2 in bag_matcher.get_opcodes()
                       if tag == "equal")

    max_words = max(len(pymupdf_words), len(converted_words))

    # Effective matching: direct matches + spacing + punct equivalents + reordered
    # But don't double-count: use max(sequential_match, bag_match) as base
    sequential_match = matching + spacing_bonus + punct_only_diffs
    # Bag match tells us: content is all there, just moved
    effective_matching = max(sequential_match, bag_matching)

    # If bag match is much higher than sequential, flag reordering
    reorder_detected = (bag_matching - matching) > max(3, max_words * 0.05)

    score = (effective_matching / max_words * 100) if max_words > 0 else 100.0
    # Cap at 100
    score = min(100.0, score)

    # If reorder detected, apply a mild penalty (content is there, order isn't)
    if reorder_detected and score > 90:
        reorder_penalty = min(5.0, (bag_matching - matching) / max_words * 10)
        score = score - reorder_penalty

    # Collect actual differences for reporting (using raw words for display)
    diffs = []
    raw_matcher = difflib.SequenceMatcher(None, pymupdf_words_raw, converted_words_raw)
    for tag, i1, i2, j1, j2 in raw_matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace":
            # Skip spacing-only
            if "".join(pymupdf_words_raw[i1:i2]) == "".join(converted_words_raw[j1:j2]):
                continue
            # Skip punct-only
            p_seg = pymupdf_words_raw[i1:i2]
            c_seg = converted_words_raw[j1:j2]
            if len(p_seg) == len(c_seg) and all(_is_punct_only_diff(a, b) for a, b in zip(p_seg, c_seg)):
                continue
        diffs.append({
            "op": tag,
            "pymupdf": " ".join(pymupdf_words_raw[i1:i2]),
            "converted": " ".join(converted_words_raw[j1:j2]),
        })

    return {
        "score": round(score, 4),
        "matching": effective_matching,
        "pymupdf_count": len(pymupdf_words),
        "converted_count": len(converted_words),
        "diff_count": len(diffs),
        "diffs": diffs[:20],
        "reorder_detected": reorder_detected,
        "punct_only_diffs": punct_only_diffs,
        "bag_of_words_match": bag_matching,
    }


# ═══════════════════════════════════════════════════════════════════
# DIMENSION 2: TAG INTEGRITY
# ═══════════════════════════════════════════════════════════════════

def check_tag_integrity(pymupdf_text: str, converted_text: str) -> Dict:
    """
    Verify all tags from source are preserved in conversion.
    Score: 100 if all tags preserved, penalized per missing/extra tag.
    """
    src_tags = count_tags(pymupdf_text)
    dst_tags = count_tags(converted_text)

    issues = []

    # Check balanced tags in source
    if src_tags["add_open"] != src_tags["add_close"]:
        issues.append(f"Source has unbalanced [A> tags: {src_tags['add_open']} open vs {src_tags['add_close']} close")
    if src_tags["del_open"] != src_tags["del_close"]:
        issues.append(f"Source has unbalanced [D> tags: {src_tags['del_open']} open vs {src_tags['del_close']} close")

    # Check balanced tags in converted
    if dst_tags["add_open"] != dst_tags["add_close"]:
        issues.append(f"Converted has unbalanced [A> tags: {dst_tags['add_open']} open vs {dst_tags['add_close']} close")
    if dst_tags["del_open"] != dst_tags["del_close"]:
        issues.append(f"Converted has unbalanced [D> tags: {dst_tags['del_open']} open vs {dst_tags['del_close']} close")

    # Check tag counts match (converted should have same or more from splitting)
    total_src = src_tags["add_open"] + src_tags["del_open"]
    total_dst = dst_tags["add_open"] + dst_tags["del_open"]

    if total_src == 0 and total_dst == 0:
        return {"score": 100.0, "issues": [], "src_tags": src_tags,
                "dst_tags": dst_tags, "details": "No tags in either file",
                "missing_tag_count": 0, "missing_content_spans": [],
                "boundary_shifts": 0}

    # Tags should never decrease
    missing_add = max(0, src_tags["add_open"] - dst_tags["add_open"])
    missing_del = max(0, src_tags["del_open"] - dst_tags["del_open"])
    total_missing = missing_add + missing_del

    if total_missing > 0:
        issues.append(f"MISSING TAGS: {missing_add} [A> and {missing_del} [D> tags lost in conversion")

    # Verify tagged CONTENT is preserved (not just tag counts)
    src_spans = extract_tagged_spans(pymupdf_text)
    dst_spans = extract_tagged_spans(converted_text)

    src_content = set(s["text"].strip() for s in src_spans if len(s["text"].strip()) > 2)
    dst_content = set(s["text"].strip() for s in dst_spans if len(s["text"].strip()) > 2)

    # What tagged content from source is missing in destination?
    missing_content = src_content - dst_content
    # Check if missing content exists with minor whitespace differences
    truly_missing = []
    for mc in missing_content:
        mc_norm = re.sub(r"\s+", " ", _normalize_unicode(mc)).strip()
        found = False
        for dc in dst_content:
            dc_norm = re.sub(r"\s+", " ", _normalize_unicode(dc)).strip()
            if mc_norm == dc_norm:
                found = True
                break
            # Check substring containment for split tags
            if mc_norm in dc_norm or dc_norm in mc_norm:
                found = True
                break
        if not found:
            truly_missing.append(mc)

    if truly_missing:
        issues.append(f"MISSING TAGGED CONTENT: {len(truly_missing)} tagged span(s) not found in converted")

    # ── Tag boundary shift detection ──
    # Even if tag counts match, the BOUNDARIES may have shifted
    # Compare what's inside each tag — if text changed, boundaries moved
    boundary_shifts = 0
    if len(src_spans) == len(dst_spans) and total_missing == 0:
        for ss, ds in zip(
            sorted(src_spans, key=lambda s: s["start"]),
            sorted(dst_spans, key=lambda s: s["start"]),
        ):
            if ss["type"] == ds["type"]:
                s_norm = re.sub(r"\s+", " ", _normalize_unicode(ss["text"])).strip()
                d_norm = re.sub(r"\s+", " ", _normalize_unicode(ds["text"])).strip()
                if s_norm != d_norm:
                    # Check if it's a significant boundary shift vs minor whitespace
                    s_words = set(s_norm.lower().split())
                    d_words = set(d_norm.lower().split())
                    # If the word sets are very different, it's a boundary shift
                    if s_words != d_words:
                        overlap = len(s_words & d_words)
                        total = max(len(s_words), len(d_words))
                        if total > 0 and overlap / total < 0.8:
                            boundary_shifts += 1
                            issues.append(
                                f"TAG BOUNDARY SHIFTED: [{ss['type'][0].upper()}> "
                                f"'{ss['text'][:30]}...' → '{ds['text'][:30]}...'"
                            )

    if boundary_shifts:
        issues.append(f"TOTAL BOUNDARY SHIFTS: {boundary_shifts}")

    # Score calculation
    if total_src == 0:
        score = 100.0
    else:
        # Penalize: each missing tag = heavy penalty, boundary shift = moderate
        penalty = (total_missing * 5.0) + (len(truly_missing) * 10.0) + (boundary_shifts * 8.0)
        score = max(0.0, 100.0 - penalty)

    return {
        "score": round(score, 4),
        "issues": issues,
        "src_tags": src_tags,
        "dst_tags": dst_tags,
        "missing_tag_count": total_missing,
        "missing_content_spans": truly_missing[:10],
        "boundary_shifts": boundary_shifts,
    }


# ═══════════════════════════════════════════════════════════════════
# DIMENSION 3: CONTENT COMPLETENESS (no dropped pages/sections)
# ═══════════════════════════════════════════════════════════════════

def check_content_completeness(pymupdf_text: str, converted_text: str,
                                state: str = "") -> Dict:
    """
    Check that all sections/numbered items from source exist in converted.
    Detects dropped pages or large blocks of content.
    """
    rules = STATE_RULES.get(state.upper(), {})
    section_pat = rules.get("section_pattern", r"(?:Sec(?:tion)?\.?\s*\d+|§\s*\d+)")

    src_stripped = strip_tags(pymupdf_text)
    dst_stripped = strip_tags(converted_text)

    # Find sections in both
    src_sections = find_sections(src_stripped, section_pat)
    dst_sections = find_sections(dst_stripped, section_pat)

    # Normalize for comparison
    src_sec_norm = [re.sub(r"\s+", " ", s).strip().lower() for s in src_sections]
    dst_sec_norm = [re.sub(r"\s+", " ", s).strip().lower() for s in dst_sections]

    missing_sections = [s for s in src_sec_norm if s not in dst_sec_norm]

    # Check for large content blocks dropped (paragraphs > 50 words)
    # Skip this check if word content is identical (avoids false positives
    # from line-break differences in identical files)
    src_words_flat = re.sub(r"\s+", " ", src_stripped).strip()
    dst_words_flat = re.sub(r"\s+", " ", dst_stripped).strip()
    dropped_paragraphs = []
    if src_words_flat != dst_words_flat:
        src_paragraphs = [p.strip() for p in src_stripped.split("\n\n") if len(p.split()) > 50]
        for para in src_paragraphs:
            # Check if substantial content of paragraph exists in converted
            para_words = para.split()[:20]  # first 20 words as signature
            signature = " ".join(para_words)
            if signature.lower() not in dst_stripped.lower():
                # Try a shorter signature
                short_sig = " ".join(para_words[:10])
                if short_sig.lower() not in dst_stripped.lower():
                    dropped_paragraphs.append(signature[:80] + "...")

    # Character-length ratio check
    src_len = len(src_stripped.replace(" ", "").replace("\n", ""))
    dst_len = len(dst_stripped.replace(" ", "").replace("\n", ""))
    length_ratio = dst_len / src_len if src_len > 0 else 1.0

    issues = []
    if missing_sections:
        issues.append(f"MISSING SECTIONS: {missing_sections}")
    if dropped_paragraphs:
        issues.append(f"POSSIBLY DROPPED PARAGRAPHS: {len(dropped_paragraphs)}")
    if length_ratio < 0.85:
        issues.append(f"Content significantly shorter: {length_ratio:.1%} of source length")
    if length_ratio > 1.15:
        issues.append(f"Content significantly longer: {length_ratio:.1%} of source (possible duplication)")

    # Score: penalize for missing sections and dropped content
    penalty = (len(missing_sections) * 8.0) + (len(dropped_paragraphs) * 5.0)
    if length_ratio < 0.85:
        penalty += (1.0 - length_ratio) * 50
    score = max(0.0, 100.0 - penalty)

    return {
        "score": round(score, 4),
        "issues": issues,
        "src_section_count": len(src_sections),
        "dst_section_count": len(dst_sections),
        "missing_sections": missing_sections,
        "dropped_paragraphs": dropped_paragraphs[:5],
        "length_ratio": round(length_ratio, 4),
    }


# ═══════════════════════════════════════════════════════════════════
# DIMENSION 4: STATE-SPECIFIC ARTIFACT REMOVAL
# ═══════════════════════════════════════════════════════════════════

def check_artifact_removal(converted_text: str, state: str) -> Dict:
    """
    Verify that state-specific artifacts were properly removed from conversion.
    Checks: line numbers gone, repeated headers gone, footers gone.
    """
    rules = STATE_RULES.get(state.upper())
    if not rules:
        return {"score": 100.0, "issues": [],
                "details": f"No rules defined for state '{state}'"}

    lines = converted_text.splitlines()
    issues = []
    artifact_count = 0

    # Check for remaining line numbers (pattern: line starts with just a number)
    if rules.get("line_numbers"):
        line_number_pattern = re.compile(r"^\s{0,6}\d{1,3}\s*$")
        remaining_line_nums = []
        for i, line in enumerate(lines):
            if line_number_pattern.match(line) and i > 5:  # skip first few lines
                remaining_line_nums.append((i + 1, line.strip()))
        if len(remaining_line_nums) > 3:  # a few isolated numbers are OK
            issues.append(f"LINE NUMBERS NOT REMOVED: {len(remaining_line_nums)} found")
            artifact_count += len(remaining_line_nums)

    # Check for remaining footer patterns
    for fp in rules.get("footer_patterns", []):
        footer_re = re.compile(fp, re.IGNORECASE)
        for i, line in enumerate(lines):
            if footer_re.match(line.strip()):
                # Only flag if it looks like a standalone footer line
                if len(line.strip()) < 20:
                    artifact_count += 1

    # Check for known artifact patterns
    for ap in rules.get("known_artifacts", []):
        art_re = re.compile(ap, re.IGNORECASE)
        for line in lines:
            if art_re.match(line.strip()):
                artifact_count += 1
                issues.append(f"Known artifact present: {line.strip()[:50]}")

    # Score: minor penalty per artifact (these are cosmetic, not content loss)
    penalty = min(30.0, artifact_count * 2.0)
    score = max(0.0, 100.0 - penalty)

    return {
        "score": round(score, 4),
        "issues": issues,
        "artifact_count": artifact_count,
    }


# ═══════════════════════════════════════════════════════════════════
# DIMENSION 5: STRUCTURAL FORMATTING
# ═══════════════════════════════════════════════════════════════════

def check_structural_formatting(pymupdf_text: str, converted_text: str) -> Dict:
    """
    Check structural integrity: line count ratio, paragraph structure,
    no mangled/concatenated lines, proper whitespace.
    """
    src_lines = [l for l in pymupdf_text.splitlines() if l.strip()]
    dst_lines = [l for l in converted_text.splitlines() if l.strip()]

    issues = []

    # Check for excessively long lines (possible concatenation errors)
    long_lines = [l for l in dst_lines if len(l) > 500]
    if long_lines:
        issues.append(f"Suspiciously long lines: {len(long_lines)} lines > 500 chars")

    # Check for obvious encoding issues
    encoding_issues = 0
    bad_chars = re.compile(r"[\ufffd\x00-\x08\x0b\x0c\x0e-\x1f]")
    for line in dst_lines:
        if bad_chars.search(line):
            encoding_issues += 1
    if encoding_issues:
        issues.append(f"Encoding issues: {encoding_issues} lines with bad characters")

    # Check paragraph breaks preserved (converted should have similar structure)
    src_empty_lines = pymupdf_text.count("\n\n")
    dst_empty_lines = converted_text.count("\n\n")
    # Allow some variance (artifact removal changes structure)
    if src_empty_lines > 5 and dst_empty_lines < src_empty_lines * 0.3:
        issues.append("Paragraph breaks may be lost (significantly fewer blank lines)")

    # Score
    penalty = (len(long_lines) * 3.0) + (encoding_issues * 5.0)
    score = max(0.0, 100.0 - penalty)

    return {
        "score": round(score, 4),
        "issues": issues,
        "src_line_count": len(src_lines),
        "dst_line_count": len(dst_lines),
        "long_lines": len(long_lines),
        "encoding_issues": encoding_issues,
    }


# ═══════════════════════════════════════════════════════════════════
# DIMENSION 6: CHARACTER-LEVEL SIMILARITY (n-gram / fingerprint)
# ═══════════════════════════════════════════════════════════════════

def check_char_level_similarity(pymupdf_text: str, converted_text: str,
                                 ngram_size: int = 5) -> Dict:
    """
    Character-level n-gram (shingle) similarity.
    More granular than word-level — catches typos, OCR errors, char swaps.
    Uses Jaccard similarity of character n-grams.
    """
    src = re.sub(r"\s+", " ", strip_tags(pymupdf_text)).strip().lower()
    dst = re.sub(r"\s+", " ", strip_tags(converted_text)).strip().lower()

    if not src and not dst:
        return {"score": 100.0, "details": "Both empty"}

    def _ngrams(text: str, n: int) -> set:
        return {text[i:i+n] for i in range(len(text) - n + 1)} if len(text) >= n else {text}

    src_ngrams = _ngrams(src, ngram_size)
    dst_ngrams = _ngrams(dst, ngram_size)

    intersection = len(src_ngrams & dst_ngrams)
    union = len(src_ngrams | dst_ngrams)

    jaccard = (intersection / union * 100) if union > 0 else 100.0

    return {
        "score": round(jaccard, 4),
        "src_ngrams": len(src_ngrams),
        "dst_ngrams": len(dst_ngrams),
        "intersection": intersection,
    }


# ═══════════════════════════════════════════════════════════════════
# DIMENSION 7: HALLUCINATION / EXTRA CONTENT DETECTION
# ═══════════════════════════════════════════════════════════════════

def check_hallucination(pymupdf_text: str, converted_text: str) -> Dict:
    """
    Detect content in converted that does NOT exist in the source.
    Hallucinated content = words/sentences added by LLM that aren't from the PDF.

    Distinguishes between:
    - Minor additions (whitespace, punctuation normalization) — acceptable
    - Reordered content (same words, different position) — NOT hallucination
    - Major additions (new sentences, commentary, notes) — hallucination
    """
    src_words = _extract_normalized_words(pymupdf_text)
    dst_words = _extract_normalized_words(converted_text)

    if not dst_words:
        return {"score": 100.0, "extra_words": 0, "extra_ratio": 0.0,
                "hallucinated_phrases": [], "issues": []}

    # Build a multiset (counter) of source words for reorder-tolerant comparison
    from collections import Counter
    src_counter = Counter(w.lower() for w in src_words)
    dst_counter = Counter(w.lower() for w in dst_words)

    # Words that appear MORE in converted than in source = truly new content
    truly_extra = dst_counter - src_counter
    truly_extra_count = sum(truly_extra.values())

    # Use SequenceMatcher to find CONTIGUOUS inserted blocks (stronger signal)
    matcher = difflib.SequenceMatcher(None, src_words, dst_words)
    hallucinated_phrases = []
    contiguous_extra = 0

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "insert":
            phrase_words = dst_words[j1:j2]
            phrase = " ".join(phrase_words)
            phrase_len = j2 - j1
            # Check: are these words actually from the source (reordered)?
            phrase_new_words = sum(1 for w in phrase_words
                                  if w.lower() not in src_counter)
            # Only flag if most words are genuinely new (not reordered)
            if phrase_len > 3 and phrase_new_words > phrase_len * 0.5:
                hallucinated_phrases.append(phrase[:100])
                contiguous_extra += phrase_new_words
        elif tag == "replace":
            src_len = i2 - i1
            dst_len = j2 - j1
            if dst_len > src_len * 2 and dst_len - src_len > 3:
                extra_words_here = dst_words[j1+src_len:j2]
                new_in_extra = sum(1 for w in extra_words_here
                                   if w.lower() not in src_counter)
                if new_in_extra > len(extra_words_here) * 0.5:
                    extra = " ".join(extra_words_here)
                    hallucinated_phrases.append(extra[:100])
                    contiguous_extra += new_in_extra

    # Use the stronger signal: truly new words (not in source at all)
    total_extra_words = max(truly_extra_count, contiguous_extra)

    # Ratio of genuinely new content to source content
    extra_ratio = total_extra_words / len(src_words) if src_words else 0.0

    issues = []
    if hallucinated_phrases:
        issues.append(f"HALLUCINATED CONTENT: {len(hallucinated_phrases)} phrase(s), "
                      f"{total_extra_words} extra words ({extra_ratio:.1%} of source)")

    # Score: penalize based on extra content ratio
    if extra_ratio <= 0.02:
        score = 100.0
    elif extra_ratio <= 0.05:
        score = 95.0 - (extra_ratio - 0.02) * 200
    elif extra_ratio <= 0.15:
        score = 89.0 - (extra_ratio - 0.05) * 200
    else:
        score = max(0.0, 69.0 - (extra_ratio - 0.15) * 300)

    return {
        "score": round(score, 4),
        "extra_words": total_extra_words,
        "extra_ratio": round(extra_ratio, 4),
        "hallucinated_phrases": hallucinated_phrases[:5],
        "issues": issues,
    }


# ═══════════════════════════════════════════════════════════════════
# MAIN: COMPOSITE CONFIDENCE SCORE
# ═══════════════════════════════════════════════════════════════════

# Weights for each dimension (must sum to 1.0)
DIMENSION_WEIGHTS = {
    "word_accuracy":       0.35,   # Most important: are the words correct?
    "tag_integrity":       0.25,   # Critical: are legislative markings preserved?
    "content_completeness": 0.12,  # Are all sections present?
    "hallucination":       0.10,   # No extra content added by LLM?
    "char_similarity":     0.08,   # Character-level granularity
    "artifact_removal":    0.05,   # State-specific cleanup done?
    "structural":          0.05,   # Formatting sanity
}


def check_conversion_accuracy(pymupdf_text: str, converted_text: str,
                               state: str = "") -> Dict:
    """
    Run all accuracy checks and produce a composite confidence percentage.

    Parameters
    ----------
    pymupdf_text : str
        The PyMuPDF extracted text (with [A>/<A] and [D>/<D] tags).
    converted_text : str
        The LLM-converted/cleaned text (with tags preserved).
    state : str
        Two-letter state code (CA, CT, DE, FL, etc.) for state-specific checks.

    Returns
    -------
    dict with:
        confidence_pct    – float, 0–100 (the composite score)
        grade             – str, "EXCELLENT"/"GOOD"/"ACCEPTABLE"/"POOR"/"FAIL"
        dimensions        – dict of individual dimension results
        issues_summary    – list of all issues found
        verdict           – human-readable summary
    """
    state = state.upper() if state else ""

    # Run all dimension checks
    d1 = check_word_accuracy(pymupdf_text, converted_text)
    d2 = check_tag_integrity(pymupdf_text, converted_text)
    d3 = check_content_completeness(pymupdf_text, converted_text, state)
    d4 = check_artifact_removal(converted_text, state)
    d5 = check_structural_formatting(pymupdf_text, converted_text)
    d6 = check_char_level_similarity(pymupdf_text, converted_text)
    d7 = check_hallucination(pymupdf_text, converted_text)

    dimensions = {
        "word_accuracy": d1,
        "tag_integrity": d2,
        "content_completeness": d3,
        "hallucination": d7,
        "artifact_removal": d4,
        "structural": d5,
        "char_similarity": d6,
    }

    # Compute weighted composite score
    composite = (
        d1["score"] * DIMENSION_WEIGHTS["word_accuracy"]
        + d2["score"] * DIMENSION_WEIGHTS["tag_integrity"]
        + d3["score"] * DIMENSION_WEIGHTS["content_completeness"]
        + d7["score"] * DIMENSION_WEIGHTS["hallucination"]
        + d6["score"] * DIMENSION_WEIGHTS["char_similarity"]
        + d4["score"] * DIMENSION_WEIGHTS["artifact_removal"]
        + d5["score"] * DIMENSION_WEIGHTS["structural"]
    )

    # Apply hard penalties for critical failures
    if d2["score"] < 50:  # Tag integrity failure is critical
        composite = min(composite, 60.0)
    if d3["score"] < 50:  # Content loss is critical
        composite = min(composite, 65.0)
    if d1["score"] < 80:  # Word accuracy below 80% is serious
        composite = min(composite, 75.0)
    if d7["score"] < 50:  # Major hallucination is critical
        composite = min(composite, 60.0)

    composite = round(composite, 2)

    # Grade assignment
    if composite >= 98:
        grade = "EXCELLENT"
    elif composite >= 95:
        grade = "GOOD"
    elif composite >= 90:
        grade = "ACCEPTABLE"
    elif composite >= 80:
        grade = "POOR"
    else:
        grade = "FAIL"

    # Collect all issues
    all_issues = []
    all_issues.extend(d2.get("issues", []))
    all_issues.extend(d3.get("issues", []))
    all_issues.extend(d7.get("issues", []))
    all_issues.extend(d4.get("issues", []))
    all_issues.extend(d5.get("issues", []))

    # Build verdict
    state_name = STATE_RULES.get(state, {}).get("name", state or "Unknown")
    verdict_parts = [
        f"State: {state_name}",
        f"Word Accuracy: {d1['score']:.2f}% ({d1['matching']}/{max(d1['pymupdf_count'], d1['converted_count'])} words)",
        f"Tag Integrity: {d2['score']:.2f}%",
        f"Content Completeness: {d3['score']:.2f}%",
        f"Hallucination: {d7['score']:.2f}%",
        f"Char Similarity: {d6['score']:.2f}%",
    ]
    if d1.get("reorder_detected"):
        verdict_parts.append("REORDER DETECTED")
    if all_issues:
        verdict_parts.append(f"Issues: {len(all_issues)}")

    return {
        "confidence_pct": composite,
        "grade": grade,
        "state": state,
        "dimensions": dimensions,
        "issues_summary": all_issues,
        "verdict": " | ".join(verdict_parts),
        "dimension_scores": {
            "word_accuracy": d1["score"],
            "tag_integrity": d2["score"],
            "content_completeness": d3["score"],
            "hallucination": d7["score"],
            "char_similarity": d6["score"],
            "artifact_removal": d4["score"],
            "structural": d5["score"],
        },
        # ── STRICT CLASSIFICATION (for segregation) ──
        # Use raw text comparison for PERFECT — no normalization tolerance
        "is_perfect": _is_strictly_identical(pymupdf_text, converted_text),
        "is_content_match": (d1["diff_count"] == 0
                             and d2["missing_tag_count"] == 0
                             and d7.get("extra_words", 0) == 0),
        "classification": _classify_result(
            d1, d2, d7, composite,
            _is_strictly_identical(pymupdf_text, converted_text)
        ),
    }


def _classify_result(d1: Dict, d2: Dict, d7: Dict, composite: float,
                     is_strictly_identical: bool) -> str:
    """
    Strict file classification for segregation. NO MISS guaranteed for PERFECT.

    Returns one of:
        "PERFECT"           — Zero differences of any kind (raw text identical after tag-strip)
        "NEAR_PERFECT"      — Only unicode/punctuation normalization diffs (content identical)
        "NEEDS_REVIEW"      — Has real differences but score >= 90
        "INACCURATE"        — Score < 90 or critical failures
    """
    has_tag_issues = d2["missing_tag_count"] > 0 or d2.get("boundary_shifts", 0) > 0
    has_hallucination = d7.get("extra_words", 0) > 0

    # PERFECT: raw text comparison says byte-identical (after whitespace norm)
    if is_strictly_identical:
        return "PERFECT"

    # If tag issues or hallucination → can never be near-perfect
    if has_tag_issues:
        # Missing tags = always INACCURATE (legislative markup integrity is critical)
        return "INACCURATE"
    if has_hallucination:
        if composite < 90:
            return "INACCURATE"
        return "NEEDS_REVIEW"

    # NEAR_PERFECT: high word accuracy with no real diffs (only punct/unicode/spacing)
    has_word_diffs = d1["diff_count"] > 0
    has_reorder = d1.get("reorder_detected", False)

    if not has_word_diffs and not has_reorder:
        return "NEAR_PERFECT"

    # NEEDS_REVIEW: composite high but has some differences
    if composite >= 90:
        return "NEEDS_REVIEW"

    return "INACCURATE"


def _is_strictly_identical(pymupdf_text: str, converted_text: str) -> bool:
    """
    Raw text comparison — the ONLY gate for PERFECT classification.
    Tolerates ONLY: whitespace differences (spaces, tabs, line endings).
    Does NOT tolerate: unicode normalization, punctuation changes, any char diff.

    This is the zero-miss guarantee. If this returns True, the files are
    character-for-character identical in content.
    """
    # Compare with tags intact (tag placement must also be identical)
    src = re.sub(r"[ \t]+", " ", pymupdf_text).strip()
    dst = re.sub(r"[ \t]+", " ", converted_text).strip()
    # Normalize line endings
    src = re.sub(r"\r\n?", "\n", src)
    dst = re.sub(r"\r\n?", "\n", dst)
    # Collapse multiple newlines to single (artifact removal may change blank lines)
    src = re.sub(r"\n{2,}", "\n", src)
    dst = re.sub(r"\n{2,}", "\n", dst)

    return src == dst


# ═══════════════════════════════════════════════════════════════════
# BATCH SEGREGATION — Process multiple files and sort them
# ═══════════════════════════════════════════════════════════════════

def segregate_files(file_pairs: List[Dict], state: str = "") -> Dict:
    """
    Segregate multiple file pairs into accuracy buckets.

    Parameters
    ----------
    file_pairs : list of dict
        Each dict has:
            "name": str (filename for identification)
            "pymupdf_text": str (source text)
            "converted_text": str (converted text)
    state : str
        State code for state-specific checks.

    Returns
    -------
    dict with:
        "perfect"       — list of files with ZERO differences (100% reliable)
        "near_perfect"  — list of files with only unicode/punct diffs
        "needs_review"  — list of files needing human spot-check
        "inaccurate"    — list of files with real errors
        "summary"       — counts and stats
    """
    buckets = {
        "perfect": [],
        "near_perfect": [],
        "needs_review": [],
        "inaccurate": [],
    }

    for fp in file_pairs:
        name = fp["name"]
        result = check_conversion_accuracy(
            fp["pymupdf_text"], fp["converted_text"], state=state
        )
        entry = {
            "name": name,
            "confidence_pct": result["confidence_pct"],
            "grade": result["grade"],
            "classification": result["classification"],
            "is_perfect": result["is_perfect"],
            "issues": result["issues_summary"],
            "dimension_scores": result["dimension_scores"],
        }

        classification = result["classification"]
        buckets[classification.lower()].append(entry)

    total = len(file_pairs)
    summary = {
        "total_files": total,
        "perfect_count": len(buckets["perfect"]),
        "near_perfect_count": len(buckets["near_perfect"]),
        "needs_review_count": len(buckets["needs_review"]),
        "inaccurate_count": len(buckets["inaccurate"]),
        "perfect_pct": round(len(buckets["perfect"]) / total * 100, 1) if total else 0,
        "pass_rate": round(
            (len(buckets["perfect"]) + len(buckets["near_perfect"])) / total * 100, 1
        ) if total else 0,
    }

    buckets["summary"] = summary
    return buckets


def _strict_text_compare(pymupdf_text: str, converted_text: str) -> Dict:
    """
    Raw byte-level comparison. Used to guarantee PERFECT classification.
    No normalization, no tolerance. If even one character differs, it's not perfect.
    """
    src = strip_tags(pymupdf_text)
    dst = strip_tags(converted_text)

    # Normalize whitespace only (line breaks, trailing spaces don't matter)
    src_norm = re.sub(r"[ \t]+", " ", src).strip()
    dst_norm = re.sub(r"[ \t]+", " ", dst).strip()
    # Normalize line endings
    src_norm = re.sub(r"\r\n?", "\n", src_norm)
    dst_norm = re.sub(r"\r\n?", "\n", dst_norm)

    if src_norm == dst_norm:
        return {"is_identical": True, "char_diffs": 0, "first_diff_pos": -1}

    # Find first difference position
    first_diff = -1
    for i, (a, b) in enumerate(zip(src_norm, dst_norm)):
        if a != b:
            first_diff = i
            break
    if first_diff == -1:
        first_diff = min(len(src_norm), len(dst_norm))

    # Count total differing characters
    char_diffs = sum(1 for a, b in zip(src_norm, dst_norm) if a != b)
    char_diffs += abs(len(src_norm) - len(dst_norm))

    return {
        "is_identical": False,
        "char_diffs": char_diffs,
        "first_diff_pos": first_diff,
        "context_at_diff": src_norm[max(0, first_diff-20):first_diff+20] if first_diff >= 0 else "",
    }


# ═══════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════

def _print_report(result: Dict) -> None:
    """Pretty-print the accuracy report to console."""
    print("\n" + "=" * 78)
    print("  CONVERSION ACCURACY REPORT")
    print("=" * 78)

    classification = result.get("classification", "UNKNOWN")
    is_perfect = result.get("is_perfect", False)

    print(f"\n  ╔{'═' * 50}╗")
    print(f"  ║  CONFIDENCE: {result['confidence_pct']:6.2f}%   Grade: {result['grade']:<12} ║")
    print(f"  ║  Classification: {classification:<14}  Perfect: {'YES' if is_perfect else 'NO':>3}  ║")
    print(f"  ╚{'═' * 50}╝\n")

    print("  Dimension Breakdown:")
    print("  " + "-" * 55)
    scores = result["dimension_scores"]
    weights = DIMENSION_WEIGHTS
    for dim, score in scores.items():
        w = weights[dim]
        bar_len = int(score / 100 * 30)
        bar = "█" * bar_len + "░" * (30 - bar_len)
        label = dim.replace("_", " ").title()
        print(f"  {label:<24} {score:6.2f}%  [{bar}]  (w={w:.0%})")

    print("\n  " + "-" * 55)

    # Word-level details
    d1 = result["dimensions"]["word_accuracy"]
    print(f"\n  Words: {d1['pymupdf_count']} (source) → {d1['converted_count']} (converted)")
    print(f"  Matching: {d1['matching']} | Diffs: {d1['diff_count']}")

    # Tag details
    d2 = result["dimensions"]["tag_integrity"]
    st = d2["src_tags"]
    dt = d2["dst_tags"]
    print(f"\n  Tags [A>: {st['add_open']}→{dt['add_open']}  [D>: {st['del_open']}→{dt['del_open']}")

    # Content
    d3 = result["dimensions"]["content_completeness"]
    print(f"  Sections: {d3['src_section_count']} (source) → {d3['dst_section_count']} (converted)")
    print(f"  Length ratio: {d3['length_ratio']:.2%}")

    # Issues
    if result["issues_summary"]:
        print(f"\n  ⚠ Issues ({len(result['issues_summary'])}):")
        for issue in result["issues_summary"][:10]:
            print(f"    • {issue}")

    # Sample diffs
    if d1.get("diffs"):
        print(f"\n  Sample differences (first 5):")
        for i, diff in enumerate(d1["diffs"][:5], 1):
            op = diff["op"].upper()
            if op == "REPLACE":
                print(f"    {i}. [{op}] '{diff['pymupdf'][:40]}' → '{diff['converted'][:40]}'")
            elif op == "DELETE":
                print(f"    {i}. [MISSING] '{diff['pymupdf'][:60]}'")
            elif op == "INSERT":
                print(f"    {i}. [EXTRA] '{diff['converted'][:60]}'")

    print("\n" + "=" * 78)


def main():
    parser = argparse.ArgumentParser(
        description="Check PDF conversion accuracy with state-specific validation"
    )
    parser.add_argument("pymupdf_file", help="PyMuPDF extracted text file (source/ground truth)")
    parser.add_argument("converted_file", help="LLM-converted text file (validation target)")
    parser.add_argument("--state", "-s", default="", help="State code (CA, CT, DE, FL, CO, etc.)")
    parser.add_argument("--json", "-j", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    try:
        with open(args.pymupdf_file, "r", encoding="utf-8") as f:
            pymupdf_text = f.read()
        with open(args.converted_file, "r", encoding="utf-8") as f:
            converted_text = f.read()
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    result = check_conversion_accuracy(pymupdf_text, converted_text, state=args.state)

    if args.json:
        # Remove non-serializable bits for clean JSON
        output = {k: v for k, v in result.items() if k != "dimensions"}
        output["dimension_scores"] = result["dimension_scores"]
        print(json.dumps(output, indent=2))
    else:
        _print_report(result)

    # Exit code: 0 = GOOD+, 1 = ACCEPTABLE/POOR, 2 = FAIL
    if result["grade"] in ("EXCELLENT", "GOOD"):
        sys.exit(0)
    elif result["grade"] in ("ACCEPTABLE", "POOR"):
        sys.exit(1)
    else:
        sys.exit(2)


if __name__ == "__main__":
    main()
