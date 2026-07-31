"""
Unit tests for ``app/extraction_validator.py``.

These don't need a fixture PDF — they verify the validator's regex
catalogue catches the symptoms it claims to and stays quiet on text
that is known-clean.  Cheap, fast, and deterministic.
"""

from __future__ import annotations

import re

import pytest

from extraction_validator import (
    ExtractionAnomaly,
    validate_extraction,
)


# ---------------------------------------------------------------------------
# Should-trigger cases (one per pattern in the catalogue)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind, text",
    [
        # The 2026-06-29 trailing-$ bug itself.
        ("trailing_dollar", "Some line ending with the value 3,880,543 $\n"),
        ("trailing_dollar", "[A>TOTAL - Office of the Secretary          3,880,543 $<A]\n"),

        # Number split on thousands separator / decimal point.
        ("split_thousands_separator", "the amount is 3 ,880 dollars"),
        ("split_decimal_point", "the rate of 12 .5 percent"),

        # Empty tags should never survive cleanup.
        ("empty_add_tag", "blah blah [A><A] blah blah"),
        ("empty_del_tag", "blah blah [D>   <D] blah blah"),

        # Unmerged adjacent same-type tags.
        ("unmerged_adjacent_add_tags", "[A>foo<A][A>bar<A]"),
        ("unmerged_adjacent_del_tags", "[D>foo<D][D>bar<D]"),

        # Failed dehyphenation.
        ("failed_dehyphenation", "appropri-\n  ated to the City of Dover"),

        # Section header split from its number.
        ("split_section_header", "Sec.\n  123. The following shall apply"),

        # Orphan tag opener / closer on a line by itself.
        ("orphan_tag_opener", "previous line text\n[A>\nnext line text"),
        ("orphan_tag_closer", "previous line text\n<A]\nnext line text"),

        # DIVISION header missing blank-line separator.
        ("division_missing_blank_line", "DIVISION V\nIOWA PUBLIC EMPLOYEES RETIREMENT SYSTEM"),

        # Duplicate paragraph run (chunk merge failure).
        ("duplicate_paragraph_run",
         "\nThe authority shall allocate ten million dollars for purposes of this paragraph and the board shall determine amounts.\n\n"
         "\nThe authority shall allocate ten million dollars for purposes of this paragraph and the board shall determine amounts.\n"),

        # Signature line merged with Approved date.
        ("signature_line_merged", "Approved _______________, 2026 ______________________________"),
    ],
)
def test_validator_catches_known_antipattern(kind: str, text: str) -> None:
    anomalies = validate_extraction(text)
    kinds_found = {a.kind for a in anomalies}
    assert kind in kinds_found, (
        f"Expected validator to catch '{kind}' in {text!r}; "
        f"actually found: {sorted(kinds_found) or 'nothing'}"
    )


# ---------------------------------------------------------------------------
# Should-NOT-trigger cases (clean text)
# ---------------------------------------------------------------------------


_CLEAN_SAMPLES = [
    # Properly ordered currency.
    "County Seat Package $         3,880,543",
    "[A>TOTAL - Office of the Secretary $         3,880,543<A]",

    # Inline currency in paragraph prose.
    "any Grant-in-Aid recipient receiving an amount of $6,000 or less",

    # Numbers without spurious whitespace.
    "appropriates $5,000,000 for Health Care Contingency",
    "the rate is 12.5 percent",

    # Adjacent A and D tags (mix is fine, not a real anti-pattern).
    "[D>old<D][A>new<A]",

    # Section header on its own line — but with the number on the same line.
    "Sec. 123. The following shall apply",

    # DIVISION with proper blank line (should NOT trigger).
    "DIVISION V\n\nIOWA PUBLIC EMPLOYEES RETIREMENT SYSTEM",

    # Signature already on separate lines (should NOT trigger).
    "Approved _______________, 2026\n\n______________________________",

    # Legitimate hyphenated compound across a wrap is rare here, but
    # the dehyphenation pattern requires lowercase-on-the-right with
    # >= 2 letters, so "high-\nspeed" *would* match.  Keep this case
    # out of the clean samples — it's a known limitation of the
    # current regex.  Instead, verify a non-wrapping hyphen passes.
    "high-speed rail",
]


@pytest.mark.parametrize("text", _CLEAN_SAMPLES)
def test_validator_quiet_on_clean_text(text: str) -> None:
    anomalies = validate_extraction(text)
    assert not anomalies, (
        f"Validator flagged clean text {text!r}: "
        f"{[(a.kind, a.snippet) for a in anomalies]}"
    )


# ---------------------------------------------------------------------------
# Behavioural tests
# ---------------------------------------------------------------------------


def test_anomaly_carries_metadata() -> None:
    anomalies = validate_extraction(
        "amount: 3,880,543 $\n",
        state_code="DE",
        pdf_basename="bill.pdf",
    )
    assert len(anomalies) == 1
    a = anomalies[0]
    assert a.state_code == "DE"
    assert a.pdf_basename == "bill.pdf"
    assert a.kind == "trailing_dollar"
    assert "trailing_dollar" in a.to_log_extra()["anomaly_kind"]


def test_anomaly_order_is_stable_by_offset() -> None:
    text = (
        "line 1: bad value 100 $\n"
        "line 2: another bad value 200 $\n"
        "line 3: split decimal 12 .5\n"
    )
    anomalies = validate_extraction(text)
    offsets = [a.char_offset for a in anomalies]
    assert offsets == sorted(offsets), "Anomalies must be returned in offset order"


def test_validator_max_per_kind_caps_runaway() -> None:
    # Build text with 1000 trailing-$ hits; validator should cap at 50
    # and append one "_TRUNCATED" marker so observers know the cap fired.
    text = "line value 100 $\n" * 1000
    anomalies = validate_extraction(text, max_per_kind=10)
    trailing_real = [a for a in anomalies if a.kind == "trailing_dollar"]
    truncated_markers = [a for a in anomalies if a.kind.endswith("_TRUNCATED")]
    assert len(trailing_real) == 10
    assert len(truncated_markers) == 1


def test_validator_returns_empty_for_empty_text() -> None:
    assert validate_extraction("") == []
    assert validate_extraction(None) == []  # type: ignore[arg-type]


def test_validator_assigns_page_numbers_when_markers_present() -> None:
    text = (
        "preamble\n\n--- Page 1 ---\n\nfirst page content\n"
        "--- Page 2 ---\n\nsecond page bad value 100 $\n"
    )
    anomalies = validate_extraction(text)
    bad = [a for a in anomalies if a.kind == "trailing_dollar"]
    assert len(bad) == 1
    assert bad[0].page_number == 2


# ── Anti-pattern 12: orphan closer mid-line ──────────────────────────


def test_orphan_closer_midline_detected() -> None:
    """A line with <A] but no [A> on the same line should be flagged."""
    text = "SECTION 1. (a) The Legislature finds and declares all of the following:<A]\n"
    anomalies = validate_extraction(text)
    assert any(a.kind == "orphan_closer_midline" for a in anomalies)


def test_orphan_closer_midline_clean() -> None:
    """A properly tagged line should NOT trigger orphan_closer_midline."""
    text = "[A>SECTION 1. (a) The Legislature finds and declares all of the following:<A]\n"
    anomalies = validate_extraction(text)
    assert not any(a.kind == "orphan_closer_midline" for a in anomalies)


# ── Anti-pattern 13: vote fraction split ─────────────────────────────


def test_vote_fraction_split_detected() -> None:
    """²[D>⁄3<D] should trigger vote_fraction_split."""
    text = "Vote: ²[D>⁄3<D][A>majority<A]. Appropriation: no.\n"
    anomalies = validate_extraction(text)
    assert any(a.kind == "vote_fraction_split" for a in anomalies)


def test_vote_fraction_split_clean() -> None:
    """[D>²⁄3<D] (fixed) should NOT trigger vote_fraction_split."""
    text = "Vote: [D>²⁄3<D][A>majority<A]. Appropriation: no.\n"
    anomalies = validate_extraction(text)
    assert not any(a.kind == "vote_fraction_split" for a in anomalies)


# ── split_numbered_item_paragraph_welds ──────────────────────────────

from pymupdf_extractor import split_numbered_item_paragraph_welds
from pymupdf_extractor import _split_in_section_heading_welds
from pymupdf_extractor import _split_in_merged_table_rows
from pymupdf_extractor import _ensure_in_item_spacing
from pymupdf_extractor import _split_in_midline_items


def test_split_numbered_item_weld_basic() -> None:
    """A short (N) item welded to a long paragraph should be split."""
    welded = (
        "[A>(2) interview each new executive committee member. "
        "The chair of the judicial selection committee shall consider the "
        "feedback of incumbent executive committee members and superior "
        "court judges in an executive session and shall select one (1) "
        "member of the executive committee as the chief judge.<A]"
    )
    result = split_numbered_item_paragraph_welds(welded)
    lines = [l for l in result.split('\n') if l.strip()]
    assert len(lines) == 2, f"Expected 2 non-blank lines, got {len(lines)}"
    assert lines[0] == "[A>(2) interview each new executive committee member.<A]"
    assert lines[1].startswith("[A>The chair of the judicial")
    assert lines[1].endswith("<A]")


def test_split_numbered_item_weld_no_false_positive_short() -> None:
    """A short multi-sentence item (total <200 chars) must NOT be split."""
    short = "[A>(1) The court shall hold a hearing. The hearing must be within thirty days.<A]"
    result = split_numbered_item_paragraph_welds(short)
    assert result == short, "Short item should not be split"


def test_split_numbered_item_weld_no_false_positive_citation() -> None:
    """Periods inside citations (IC 35-43-5-3.8) must NOT cause a split."""
    citation = (
        "[A>(3) as described in IC 35-43-5-3.8 the defendant shall be "
        "subject to the penalties outlined in this chapter and any "
        "subsequent amendments that may apply to the original "
        "conviction as determined by the sentencing court.<A]"
    )
    result = split_numbered_item_paragraph_welds(citation)
    # After "3.8" comes a space then lowercase "the" → [A-Z][a-z] won't match digit.
    # Total length is also under 200 or item part > 120 for any candidate split.
    assert result == citation, "Citation period should not cause a split"


def test_split_numbered_item_weld_idempotent() -> None:
    """Already-split text must not be changed."""
    already_split = (
        "[A>(2) interview each new executive committee member.<A]\n"
        "\n"
        "[A>The chair of the judicial selection committee shall consider "
        "the feedback of incumbent executive committee members and "
        "superior court judges in session.<A]"
    )
    result = split_numbered_item_paragraph_welds(already_split)
    assert result == already_split, "Already-split text should be unchanged"


def test_split_numbered_item_weld_semicolon_lowercase() -> None:
    """Semicolon + lowercase continuation should trigger a split."""
    welded = (
        "[A>(2) nonresidential onsite sewage systems (as defined in "
        "IC 16-19-3.5-3.5); since 2023 shall continue to allow for the "
        "disposal of septage picked up from within the unit or a neighboring "
        "unit at a wastewater treatment facility within the unit.<A]"
    )
    result = split_numbered_item_paragraph_welds(welded)
    lines = [l for l in result.split('\n') if l.strip()]
    assert len(lines) == 2, f"Expected 2 non-blank lines, got {len(lines)}"
    assert lines[0].endswith("3.5-3.5);<A]")
    assert lines[1].startswith("[A>since 2023")


def test_split_numbered_item_weld_semicolon_lettered() -> None:
    """Lettered item (C) + semicolon + lowercase continuation should split."""
    welded = (
        "[A>(C) specific location; of a grease trap or grease interceptor "
        "if a regulated entity has the properly sized and maintained grease "
        "control equipment that prevents FOG discharges exceeding the "
        "allowable concentration.<A]"
    )
    result = split_numbered_item_paragraph_welds(welded)
    lines = [l for l in result.split('\n') if l.strip()]
    assert len(lines) == 2, f"Expected 2 non-blank lines, got {len(lines)}"
    assert lines[0] == "[A>(C) specific location;<A]"
    assert lines[1].startswith("[A>of a grease trap")


# ── _split_in_section_heading_welds ──────────────────────────────────


def test_split_section_heading_weld_chapter_sec() -> None:
    """Chapter heading welded to Sec. N. should be split."""
    welded = (
        '[A>Chapter 16. Hoosier Homesteads Sec. 1. As used in this '
        'chapter, "farm" means an area of real property that:<A]'
    )
    result = _split_in_section_heading_welds(welded)
    lines = [l for l in result.split('\n') if l.strip()]
    assert len(lines) == 2, f"Expected 2 non-blank lines, got {len(lines)}"
    assert lines[0] == '[A>Chapter 16. Hoosier Homesteads<A]'
    assert lines[1].startswith('[A>Sec. 1.')


def test_split_section_heading_weld_multi_sec() -> None:
    """Multiple Sec. definitions welded together should all be split."""
    welded = (
        '[A>Sec. 5. As used in this chapter, "program" means the Hoosier '
        'homestead program established by this chapter. '
        'Sec. 6. As used in this chapter, "registry" means the Hoosier '
        'homestead registry established under this chapter. '
        'Sec. 7. (a) The Hoosier homestead program is established for '
        'the following purposes:<A]'
    )
    result = _split_in_section_heading_welds(welded)
    lines = [l for l in result.split('\n') if l.strip()]
    assert len(lines) == 3, f"Expected 3 non-blank lines, got {len(lines)}: {lines}"
    assert lines[0].startswith('[A>Sec. 5.')
    assert lines[0].endswith('this chapter.<A]')
    assert lines[1].startswith('[A>Sec. 6.')
    assert lines[1].endswith('this chapter.<A]')
    assert lines[2].startswith('[A>Sec. 7.')


def test_split_section_heading_weld_subsection_then_sec() -> None:
    """Subsection welded to next Sec. should be split."""
    welded = (
        '[A>(b) The department shall administer the program. '
        'Sec. 8. (a) Any portion of a farm may be eligible for a '
        'Hoosier homestead designation, if the property meets the '
        'following requirements:<A]'
    )
    result = _split_in_section_heading_welds(welded)
    lines = [l for l in result.split('\n') if l.strip()]
    assert len(lines) == 2, f"Expected 2 non-blank lines, got {len(lines)}"
    assert lines[0].endswith('the program.<A]')
    assert lines[1].startswith('[A>Sec. 8.')


def test_split_section_heading_weld_no_split_cross_ref() -> None:
    """Cross-references like 'under Sec. 13.4' must NOT be split."""
    ref = (
        '[A>The committee shall consider the factors described in '
        'Sec. 13.4 of this chapter when evaluating candidates for '
        'judicial appointment to the court.<A]'
    )
    result = _split_in_section_heading_welds(ref)
    assert result == ref, "Cross-reference 'in Sec.' should not be split"


def test_split_section_heading_weld_starts_with_sec() -> None:
    """Tags that start with Sec. must NOT be split (nothing before)."""
    starts = '[A>Sec. 1. As used in this chapter, "farm" means an area of real property that:<A]'
    result = _split_in_section_heading_welds(starts)
    assert result == starts, "Tag starting with Sec. should not be split"


def test_split_numbered_item_weld_semicolon_short_continuation() -> None:
    """Semicolon-delimited items with shorter continuations should still split."""
    # (4) food solids; from high volume wastewater...
    welded = (
        "[A>(4) food solids; from high volume wastewater flows before "
        "the wastewater is discharged into a sewer system.<A]"
    )
    result = split_numbered_item_paragraph_welds(welded)
    lines = [l for l in result.split('\n') if l.strip()]
    assert len(lines) == 2, f"Expected 2 non-blank lines, got {len(lines)}"
    assert lines[0] == "[A>(4) food solids;<A]"
    assert lines[1].startswith("[A>from high volume")

    # (3) retain or remove; fats, oils, and grease...
    welded2 = (
        "[A>(3) retain or remove; fats, oils, and grease from wastewater "
        "before the wastewater is discharged into a sewer system.<A]"
    )
    result2 = split_numbered_item_paragraph_welds(welded2)
    lines2 = [l for l in result2.split('\n') if l.strip()]
    assert len(lines2) == 2, f"Expected 2 non-blank lines, got {len(lines2)}"
    assert lines2[0] == "[A>(3) retain or remove;<A]"
    assert lines2[1].startswith("[A>fats, oils")


def test_split_numbered_item_weld_semicolon_conjunction_no_split() -> None:
    """Semicolon + conjunction continuation must NOT be split."""
    nosplit = (
        "[A>(2) review the submitted applications for completeness "
        "and qualification; and the committee shall promptly schedule "
        "a hearing to interview qualified candidates for the vacancy.<A]"
    )
    result = split_numbered_item_paragraph_welds(nosplit)
    assert result == nosplit, "Semicolon + 'and' continuation should not be split"


def test_split_numbered_item_weld_no_split_subsection_letter() -> None:
    """Single lowercase letter markers (a)-(z) are subsection identifiers — never split."""
    # (h) subsection from IN HB 1033 — two sentences in one provision
    subsec_h = (
        "[A>(h) The committee shall provide notice to the clerk of each "
        "decision made under subsection (f) or (g). If a judge has not been "
        "found suitable for retention under this section, the clerk shall not "
        "include the question of the judge's retention on the ballot.<A]"
    )
    result = split_numbered_item_paragraph_welds(subsec_h)
    assert result == subsec_h, "Subsection (h) must not be split"

    # (e) subsection — also two sentences, same provision
    subsec_e = (
        "[A>(e) A judge is presumed suitable to continue to hold judicial "
        "office. The affirmative votes of at least nine (9) committee members "
        "are required to find that a judge is not suitable to continue to hold "
        "judicial office.<A]"
    )
    result = split_numbered_item_paragraph_welds(subsec_e)
    assert result == subsec_e, "Subsection (e) must not be split"


def test_split_numbered_item_weld_no_split_multi_sentence_subsection() -> None:
    """Multi-sentence numbered items must NOT be split at sentence boundaries.

    IN subsections like (1) often contain 2-3 sentences that are all
    part of the same item.  The period-split item_part threshold (55)
    prevents false splits on these — only genuinely short welded items
    (≤ 55 chars) are eligible for period-based splitting.
    """
    # Case 1: (1) long first sentence + two more sentences (item_part=112 chars)
    text_long = (
        "[A>(1) The property has been under the continuous ownership of the same "
        "family for at least one hundred (100) years. The line of ownership from "
        "the first family member that owned the property to the current owner may "
        "only be through spouses, children, siblings, nephews, nieces, aunts, "
        "uncles, or cousins. Legally adopted family members are recognized equally "
        "with blood relatives.<A]"
    )
    result = split_numbered_item_paragraph_welds(text_long)
    assert result == text_long, "Multi-sentence subsection (1) must not be split"

    # Case 2: (1) shorter first sentence + continuation (item_part=60 chars)
    text_short = (
        "[A>(1) A Hoosier homestead designation may expire or be revoked. "
        "The department and the department's online registry can provide "
        "information regarding the current status of the Hoosier homestead "
        "designation.<A]"
    )
    result = split_numbered_item_paragraph_welds(text_short)
    assert result == text_short, "Multi-sentence subsection (1) with 60-char item must not be split"


def test_split_numbered_item_weld_trailing_punctuation() -> None:
    """Lines with trailing punctuation after <A] (committee report quotes) should still split."""
    welded = (
        '[A>(6) Mandate insurance coverage for treatment under this chapter. '
        'Sec. 9. A practitioner, eligible facility (as defined in '
        'IC 16-42-26.5-1), research institution, or other person '
        'participating in providing treatment that complies with the '
        'requirements of this chapter is immune from criminal or '
        'civil liability.<A]".'
    )
    result = split_numbered_item_paragraph_welds(welded)
    lines = [l for l in result.split('\n') if l.strip()]
    assert len(lines) == 2, f"Expected 2 non-blank lines, got {len(lines)}: {lines}"
    assert lines[0] == "[A>(6) Mandate insurance coverage for treatment under this chapter.<A]"
    # Trailing ". should stay on the second line
    assert lines[1].startswith("[A>Sec. 9.")
    assert lines[1].endswith('<A]".')


# ---------------------------------------------------------------------------
# Tests for _split_in_merged_table_rows
# ---------------------------------------------------------------------------

def test_split_merged_table_rows_basic() -> None:
    """Lines with 3+ dollar amounts should be split into rows with cell-level tags."""
    merged = (
        "[A>If a Member's Creditable Service Is:          The Amount Is: "
        "At least 5 years, but less than 10 years          $150  "
        "At least 10 years, but less than 20 years          $275  "
        "At least 20 years, but less than 30 years          $375  "
        "At least 30 years          $450<A]"
    )
    result = _split_in_merged_table_rows(merged)
    lines = [l for l in result.split('\n') if l.strip()]
    assert len(lines) >= 4, f"Expected at least 4 lines, got {len(lines)}: {lines}"
    # Each row with a dollar amount should have it in its own [A>$NNN<A] tag
    for line in lines:
        dollars = re.findall(r'\$\d[\d,]*', line)
        if dollars:
            assert len(dollars) == 1, f"Expected 1 dollar amount per row, got {dollars} in: {line}"
            assert f'[A>{dollars[0]}<A]' in line, (
                f"Dollar amount should be in its own tag: {line}")


def test_split_merged_table_rows_single_space() -> None:
    """LLM-normalised single-space gaps should also be split when 3+ amounts."""
    merged = (
        "[A>If a Member's Creditable Service Is: The Amount Is: "
        "At least 5 years, but less than 10 years $150 "
        "At least 10 years, but less than 20 years $275 "
        "At least 20 years, but less than 30 years $375 "
        "At least 30 years $450<A]"
    )
    result = _split_in_merged_table_rows(merged)
    lines = [l for l in result.split('\n') if l.strip()]
    assert len(lines) >= 4, f"Expected at least 4 lines, got {len(lines)}: {lines}"
    for line in lines:
        dollars = re.findall(r'\$\d[\d,]*', line)
        if dollars:
            assert len(dollars) == 1, f"Expected 1 dollar amount per row, got {dollars} in: {line}"
            assert f'[A>{dollars[0]}<A]' in line, (
                f"Dollar amount should be in its own tag: {line}")


def test_split_merged_table_rows_single_row() -> None:
    """A single table row with description + $amount should split into cell tags."""
    row = "[A>At least 5 years, but less than 10 years          $150<A]"
    result = _split_in_merged_table_rows(row)
    assert "[A>At least 5 years, but less than 10 years<A]" in result
    assert "[A>$150<A]" in result
    assert result.count("[A>") == 2


def test_split_merged_table_rows_no_false_positive() -> None:
    """Prose with dollar amounts (no multi-space gap or no tags) should not be split."""
    normal = "appropriates $5,000,000 for Health Care and $3,000,000 for Education."
    result = _split_in_merged_table_rows(normal)
    assert result == normal
    # Tagged prose with dollar amount mid-sentence should not be split
    tagged = "[A>appropriates $5,000,000 for Health Care<A]"
    result2 = _split_in_merged_table_rows(tagged)
    assert result2 == tagged


# ---------------------------------------------------------------------------
# Tests for _ensure_in_item_spacing
# ---------------------------------------------------------------------------

def test_ensure_in_item_spacing_inserts_blank_lines() -> None:
    """Missing blank lines between lettered/numbered items should be added."""
    text = (
        "(a) First subsection content.\n"
        "(b) Second subsection content.\n"
        "(c) Third subsection content."
    )
    result = _ensure_in_item_spacing(text)
    lines = result.split('\n')
    # Should have blank lines: (a), blank, (b), blank, (c) = 5 lines
    assert len(lines) == 5, f"Expected 5 lines, got {len(lines)}: {lines}"
    assert lines[0].startswith("(a)")
    assert lines[1] == ""
    assert lines[2].startswith("(b)")
    assert lines[3] == ""
    assert lines[4].startswith("(c)")


def test_ensure_in_item_spacing_numbered() -> None:
    """Numbered items (1), (2) should also get blank lines."""
    text = (
        "commits a Level 2 felony if:\n"
        "(1) the amount is at least 10 grams;\n"
        "(2) delivered to a person under 18;\n"
        "(3) within 1,000 feet of school."
    )
    result = _ensure_in_item_spacing(text)
    lines = result.split('\n')
    # Should have: colon line, blank, (1), blank, (2), blank, (3) = 7
    assert len(lines) == 7, f"Expected 7 lines, got {len(lines)}: {lines}"


def test_ensure_in_item_spacing_tagged() -> None:
    """Tagged items like [A>(a) should also get blank lines."""
    text = (
        "[A>(a) First item.<A]\n"
        "[A>(b) Second item.<A]"
    )
    result = _ensure_in_item_spacing(text)
    lines = result.split('\n')
    assert len(lines) == 3
    assert lines[1] == ""


def test_ensure_in_item_spacing_already_spaced() -> None:
    """Items that already have blank lines should not get doubles."""
    text = (
        "(a) First subsection.\n"
        "\n"
        "(b) Second subsection."
    )
    result = _ensure_in_item_spacing(text)
    assert result == text  # No change — already spaced


# ---------------------------------------------------------------------------
# Tests for _split_in_midline_items
# ---------------------------------------------------------------------------

def test_split_midline_items_and() -> None:
    """'; and (2)' inside a [D> tag should split with proper tag boundaries."""
    text = (
        "[D>(1) successfully passes the examination provided in "
        "section 12 of this chapter; and (2)<D] meets the requirements "
        "of section 8 of this chapter."
    )
    result = _split_in_midline_items(text)
    lines = [l for l in result.split('\n') if l.strip()]
    assert len(lines) == 2, f"Expected 2 non-blank lines, got {len(lines)}: {lines}"
    # First line: tag closed after "; and"
    assert lines[0].endswith("; and<D]"), f"Expected tag close after '; and': {lines[0]}"
    # Second line: tag re-opened before "(2)"
    assert lines[1].startswith("[D>(2)<D]"), f"Expected tag re-open before (2): {lines[1]}"


def test_split_midline_items_or() -> None:
    """'; or (b)' mid-line without tags should split with just newline."""
    text = "fails to comply with subsection (a); or (b) makes a false statement."
    result = _split_in_midline_items(text)
    lines = [l for l in result.split('\n') if l.strip()]
    assert len(lines) == 2
    assert lines[0].endswith("; or")
    assert lines[1].startswith("(b)")


def test_split_midline_items_tagged_add() -> None:
    """'; and (2)' inside an [A> tag should also split correctly."""
    text = (
        "[A>(1) first requirement; and (2) second requirement; and "
        "(3) third requirement.<A]"
    )
    result = _split_in_midline_items(text)
    lines = [l for l in result.split('\n') if l.strip()]
    assert len(lines) == 3, f"Expected 3 non-blank lines, got {len(lines)}: {lines}"
    assert lines[0].endswith("; and<A]")
    assert lines[1].startswith("[A>(2)")
    assert lines[2].startswith("[A>(3)")


def test_split_midline_items_no_false_positive_colon_list() -> None:
    """Colon-introduced inline enumerations must NOT be split.

    Synopsis text like 'entitled to: (1) testify ...; and (2) compensation'
    is a flowing inline list — not separate paragraphs.
    """
    synopsis = (
        "Synopsis: Provides with certain exceptions, that if a county, "
        "city, town, or school corporation (unit) exercises eminent domain "
        "to take a fee simple interest in a Hoosier homestead, the property "
        "owner is entitled to: (1) testify at a public meeting of the "
        "condemning unit's legislative body; and (2) compensation that is "
        "the greater of: (A) 200% of the fair market value of the parcel; "
        "or (2) 200% of the value of the land."
    )
    result = _split_in_midline_items(synopsis)
    assert result == synopsis, "Colon-introduced inline list should not be split"


def test_split_midline_items_no_false_positive() -> None:
    """Inline references without '; and/or' should not be split."""
    # subsection reference mid-sentence
    text = "under subsection (b) of this chapter and section (c) applies."
    result = _split_in_midline_items(text)
    assert result == text  # No change
    # IC reference
    text2 = "IC 5-10.2-3-7.5 or IC 5-10.4-4-10 to pay"
    result2 = _split_in_midline_items(text2)
    assert result2 == text2  # No change


# ── _merge_page_break_paragraph_splits ───────────────────────────────

import importlib.util
import builtins
from pathlib import Path as _Path
import sys as _sys

_opus_path = _Path(__file__).resolve().parent.parent / "Opus4.6" / "Claude_Opus_4.6_Test.py"
# Ensure Opus4.6 dir is on sys.path so transitive imports (paragraph_break_detector, etc.) resolve
_opus_dir = str(_opus_path.parent)
if _opus_dir not in _sys.path:
    _sys.path.insert(0, _opus_dir)
_opus_spec = importlib.util.spec_from_file_location(
    "claude_opus_converter", str(_opus_path)
)
_opus_mod = importlib.util.module_from_spec(_opus_spec)
_opus_spec.loader.exec_module(_opus_mod)
_merge_page_break_paragraph_splits = _opus_mod._merge_page_break_paragraph_splits


def test_opus_import_without_optional_paragraph_break_detector(monkeypatch) -> None:
    """Importing Opus script should not fail if optional runtime helper is missing."""
    real_import = builtins.__import__

    def _import_with_missing_optional(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "paragraph_break_detector":
            raise ImportError("simulated missing optional module")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _import_with_missing_optional)
    spec = importlib.util.spec_from_file_location("claude_opus_converter_optional_missing", str(_opus_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.strip_table_markers("sample text") == "sample text"
    assert mod.pdf_unwrap_paragraphs("sample text", state_code="FL") == "sample text"


def test_fl_merge_no_merge_list_conclusion_clause() -> None:
    """FL: (d) list item ending with comma + 'commits a felony…' must stay separate.

    The 'commits a felony…' paragraph completes the sentence started before
    the (a)-(d) list — it is NOT a continuation of list item (d).
    """
    text = (
        "(d) [A>Provides<A] [D>Provide<D] information to the law "
        "enforcement agency regarding the career offender which the "
        "person knows to be false [A>information,<A]\n\n"
        "[A>commits a felony of the third degree, punishable as "
        "provided in s. 775.082, s. 775.083, or s. 775.084<A]."
    )
    result = _merge_page_break_paragraph_splits(text, state_code="FL")
    assert "\n\n" in result, (
        "Conclusion clause should remain a separate paragraph, "
        f"but was merged into list item: {result!r}"
    )


def test_fl_merge_list_conclusion_per_line_tags() -> None:
    """FL: per-line tagged version of the same pattern must also stay separate."""
    text = (
        "[A>(d) Provides information to the law enforcement agency<A] "
        "[A>regarding the career offender which the person knows to be "
        "false<A] [A>information,<A]\n\n"
        "[A>commits a felony of the third degree, punishable as provided "
        "in<A] [A>s. 775.082, s. 775.083, or s. 775.084<A]."
    )
    result = _merge_page_break_paragraph_splits(text, state_code="FL")
    assert "\n\n" in result, (
        "Conclusion clause should remain separate even with per-line tags: "
        f"{result!r}"
    )


def test_fl_merge_mid_sentence_still_merges() -> None:
    """FL Pattern 1a should still merge genuine mid-sentence page breaks."""
    text = (
        "(d) [A>Provides information to the law enforcement<A]\n\n"
        "[A>agency regarding the career offender which the person "
        "knows to be false information,<A]"
    )
    result = _merge_page_break_paragraph_splits(text, state_code="FL")
    # text_before ends with 'enforcement' (not comma) → Pattern 1a merges
    assert "\n\n" not in result, (
        "Genuine mid-sentence split should be merged: " f"{result!r}"
    )
