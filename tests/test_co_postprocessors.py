"""
Unit tests for the Colorado-specific deterministic post-processors in
``app/pymupdf_extractor.py``.

CO is converted in vision mode and marks added language by CAPITALISATION
rather than underlining, so the model has to infer tag boundaries from letter
case.  That misfires in a handful of reproducible ways, and each of these
functions repairs one of them.  Every repair must be **idempotent** and must
never change the words of the document — only markup and whitespace.
"""

from __future__ import annotations

import pytest

from pymupdf_extractor import (
    _find_co_body_start_lines,
    _split_co_deadline_rows,
    enforce_tag_integrity_gate,
    find_dropped_content,
    flatten_markdown_tables,
    strip_co_preamble_tags,
)


def _words(text: str) -> list[str]:
    """Words with all markup removed — used to prove nothing was lost."""
    import re

    return re.sub(r"\[[AD]>|<[AD]\]", " ", text).split()


# ---------------------------------------------------------------------------
# strip_co_preamble_tags — markup may only begin at the first body line "1"
# ---------------------------------------------------------------------------

# Title block is numbered 101+, body restarts at 1.
_RESOLUTION_REF = """SENATE RESOLUTION 26-005

101 CONCERNING THE RECOGNITION OF SINGLE PARENT DAY.

1         WHEREAS, The greatest need of every child is to be loved; and

2 WHEREAS, Parents invest an immeasurable amount of time,

3 energy, and devotion to raise their children; and

4 WHEREAS, The responsibility of raising children is difficult; and
"""


def test_strip_co_preamble_removes_title_tag() -> None:
    out = strip_co_preamble_tags(
        "SENATE RESOLUTION 26-005\n\n"
        "[A>CONCERNING THE RECOGNITION OF SINGLE PARENT DAY.<A]\n\n"
        "WHEREAS, The greatest need of every child is to be loved; and",
        _RESOLUTION_REF,
    )
    assert "[A>" not in out
    assert "CONCERNING THE RECOGNITION OF SINGLE PARENT DAY." in out


def test_strip_co_preamble_keeps_body_tags() -> None:
    text = (
        "SENATE RESOLUTION 26-005\n\n"
        "CONCERNING THE RECOGNITION OF SINGLE PARENT DAY.\n\n"
        "WHEREAS, The greatest need of every child is to be loved; and\n\n"
        "[A>NEW ADDED BODY LANGUAGE<A]"
    )
    assert strip_co_preamble_tags(text, _RESOLUTION_REF) == text


def test_strip_co_preamble_is_noop_without_reference() -> None:
    text = "[A>CONCERNING SOMETHING.<A]\n\nWHEREAS, a thing happened; and"
    assert strip_co_preamble_tags(text, None) == text
    assert strip_co_preamble_tags(text, "") == text


def test_strip_co_preamble_is_noop_when_anchor_absent_from_output() -> None:
    # Nothing from the reference body appears here, so the boundary is unknown
    # and the function must leave the document alone rather than guess.
    text = "[A>CONCERNING SOMETHING ENTIRELY DIFFERENT.<A]"
    assert strip_co_preamble_tags(text, _RESOLUTION_REF) == text


def test_find_co_body_start_skips_digest_prose_starting_with_1() -> None:
    """A wrapped digest sentence can begin a line with "1" (real HB26-139)."""
    ref = (
        "101 CONCERNING FUNDING.\n"
        "Section 1 expands school district powers. Specifically, section\n"
        "1 allows for school districts to:\n"
        "1 Be it enacted by the General Assembly of the State of Colorado:\n"
        "2 SECTION 1. In Colorado Revised Statutes, add 22-32-124.7\n"
        "3 follows:\n"
        "4 22-32-124.7. Workforce housing.\n"
    )
    assert _find_co_body_start_lines(ref)[0].startswith("Be it enacted")


def test_find_co_body_start_handles_numbering_restart() -> None:
    """The enacting clause is its own line 1 and numbering then restarts."""
    ref = (
        "101 CONCERNING A REPORT.\n"
        "1 Be it enacted by the General Assembly of the State of Colorado:\n"
        "1 SECTION 1. In Colorado Revised Statutes, amend 42-4-1609 as\n"
        "2 follows:\n"
        "3 42-4-1609. Coroners to report.\n"
        "4 Every coroner shall report.\n"
    )
    assert _find_co_body_start_lines(ref)[0].startswith("Be it enacted")


# ---------------------------------------------------------------------------
# _split_co_deadline_rows — two-column table, five observed broken shapes
# ---------------------------------------------------------------------------


def test_deadline_row_already_correct_is_untouched() -> None:
    text = "[A>47TH DAY (FRIDAY 02/26/27)<A]      [A>DEADLINE FOR HOUSE COMMITTEES.<A]"
    assert _split_co_deadline_rows(text) == text


def test_deadline_row_label_fused_with_description_is_split() -> None:
    out = _split_co_deadline_rows(
        "[A>1ST DAY (MONDAY 01/11/27) DEADLINE FOR THE INTRODUCTION OF BILLS.<A]"
    )
    assert out.startswith("[A>1ST DAY (MONDAY 01/11/27)<A]")
    assert "[A>DEADLINE FOR THE INTRODUCTION OF BILLS.<A]" in out


def test_deadline_row_cascade_is_split_into_separate_rows() -> None:
    """A footnote marker makes the model swallow every following row."""
    out = _split_co_deadline_rows(
        "[A>66TH DAY (WEDNESDAY 03/17/27) DEADLINE FOR FINAL PASSAGE IN THE "
        "SENATE.** 78TH DAY (MONDAY 03/29/27) DEADLINE FOR INTRODUCTION OF "
        "THE LONG BILL.<A]"
    )
    assert "[A>66TH DAY (WEDNESDAY 03/17/27)<A]" in out
    assert "[A>78TH DAY (MONDAY 03/29/27)<A]" in out
    # Each row gets its own paragraph.
    assert "\n\n[A>78TH DAY" in out


def test_deadline_row_ordinal_outside_tag_is_pulled_in() -> None:
    out = _split_co_deadline_rows(
        "1ST [A>DAY (MONDAY 01/11/27) DEADLINE FOR THE INTRODUCTION.<A]"
    )
    assert out.startswith("[A>1ST DAY (MONDAY 01/11/27)<A]")


def test_deadline_row_days_prior_prefix_outside_tag_is_pulled_in() -> None:
    out = _split_co_deadline_rows(
        "5 [A>DAYS PRIOR TO 1ST DAY (WEDNESDAY 01/06/27)* DEADLINE FOR FILING.<A]"
    )
    assert out.startswith("[A>5 DAYS PRIOR TO 1ST DAY (WEDNESDAY 01/06/27)*<A]")


def test_deadline_row_fully_untagged_row_is_retagged() -> None:
    """The footnote rows sometimes come back with no markup at all."""
    out = _split_co_deadline_rows(
        "66TH DAY (WEDNESDAY 03/17/27) DEADLINE FOR FINAL PASSAGE.** "
        "[A>78TH DAY (MONDAY 03/29/27)<A]      [A>DEADLINE FOR INTRODUCTION.<A]"
    )
    assert out.startswith("[A>66TH DAY (WEDNESDAY 03/17/27)<A]")
    # The recovered row must not sit on the same line as the previous one.
    assert any(
        line.startswith("[A>78TH DAY (MONDAY 03/29/27)<A]")
        for line in out.splitlines()
    )


def test_deadline_split_does_not_match_prose_day_reference() -> None:
    """"12TH LEGISLATIVE DAY." has no parenthetical date, so it is not a label."""
    text = (
        "[A>47TH DAY (FRIDAY 02/26/27)<A]      "
        "[A>TO REPORT BILLS INTRODUCED ON OR BEFORE THE 12TH LEGISLATIVE DAY.<A]"
    )
    assert _split_co_deadline_rows(text) == text


def test_deadline_split_preserves_words_and_is_idempotent() -> None:
    src = (
        "[A>66TH DAY (WEDNESDAY 03/17/27) DEADLINE FOR FINAL PASSAGE.** "
        "78TH DAY (MONDAY 03/29/27) DEADLINE FOR INTRODUCTION.<A]"
    )
    once = _split_co_deadline_rows(src)
    assert _words(once) == _words(src)
    assert _split_co_deadline_rows(once) == once


def test_deadline_split_leaves_non_co_text_alone() -> None:
    text = "[A>SECTION 1. A new subsection is added.<A]\n\nPlain body text."
    assert _split_co_deadline_rows(text) == text


# ---------------------------------------------------------------------------
# flatten_markdown_tables — vision emits real tables as markdown pipe grids
# ---------------------------------------------------------------------------

_PIPE_TABLE = (
    "APPENDIX\n"
    "\n"
    "| C.R.S. Section | Section in bill | Reason for Amendment |\n"
    "|---|---|---|\n"
    "| 2-2-307 (3)(d) | 1 | Repeals this provision as obsolete. |\n"
    "| 2-2-2103 (1)(b)(VI) | 2 | Provides notice of the reports. |\n"
)


def test_flatten_markdown_table_removes_pipes() -> None:
    out = flatten_markdown_tables(_PIPE_TABLE)
    assert "|" not in out
    assert "---" not in out


def test_flatten_markdown_table_aligns_columns() -> None:
    import re

    lines = [l for l in flatten_markdown_tables(_PIPE_TABLE).splitlines() if l.strip()]
    rows = [l for l in lines if l.startswith(("C.R.S. Section", "2-2-307", "2-2-2103"))]
    assert len(rows) == 3
    # Column 2 must start at the same offset on every row.  The first-column
    # cells differ in width (14 vs 14 vs 19 chars), so this only holds if the
    # cells are actually padded.
    starts = {re.search(r"\s{2,}", r).end() for r in rows}
    assert len(starts) == 1


def test_flatten_markdown_table_preserves_all_words() -> None:
    out = flatten_markdown_tables(_PIPE_TABLE)
    keep = [w for w in _PIPE_TABLE.replace("|", " ").split() if set(w) - set("-:")]
    assert _words(out) == keep


def test_flatten_markdown_table_requires_separator_row() -> None:
    """Without a separator row it is not a markdown table — leave it alone."""
    text = "| this is not | really a table |\n| just some | piped text |"
    assert flatten_markdown_tables(text) == text


def test_flatten_markdown_table_is_noop_without_pipes() -> None:
    text = "APPENDIX\n\nPlain prose with no table at all."
    assert flatten_markdown_tables(text) == text


def test_flatten_markdown_table_is_idempotent() -> None:
    once = flatten_markdown_tables(_PIPE_TABLE)
    assert flatten_markdown_tables(once) == once


# ---------------------------------------------------------------------------
# find_dropped_content — guard against the model skipping a page region
# ---------------------------------------------------------------------------

_RECITAL = (
    "WHEREAS, Section 2-2-303.5 (3), Colorado Revised Statutes, authorizes "
    "the General Assembly to designate the convening date for regular "
    "sessions by joint resolution; now, therefore, Be It Resolved by the "
    "House of Representatives of the Seventy-fifth General Assembly of the "
    "State of Colorado, the Senate concurring herein:"
)


def test_find_dropped_content_flags_a_missing_block() -> None:
    reference = f"HOUSE JOINT RESOLUTION 26-1031\n\n{_RECITAL}\n\nThat, pursuant to section 2-2-303.5 (3), the General Assembly designates Monday."
    output = "HOUSE JOINT RESOLUTION 26-1031\n\nThat, pursuant to section 2-2-303.5 (3), the General Assembly designates Monday."
    report = find_dropped_content(reference, output)
    assert report["missing_count"] >= 25
    assert "WHEREAS" in report["runs"][0]["text"]


def test_find_dropped_content_quiet_when_nothing_lost() -> None:
    reference = f"HOUSE JOINT RESOLUTION 26-1031\n\n{_RECITAL}"
    assert find_dropped_content(reference, reference)["missing_count"] == 0


def test_find_dropped_content_ignores_tag_markup_differences() -> None:
    reference = f"[A>{_RECITAL}<A]"
    assert find_dropped_content(reference, _RECITAL)["missing_count"] == 0


def test_find_dropped_content_ignores_letter_spaced_masthead() -> None:
    """FL mastheads tokenize as single characters and are removed on purpose."""
    masthead = " ".join("FLORIDAHOUSEOFREPRESENTATIVES") + " " + " ".join(
        "FLORIDAHOUSEOFREPRESENTATIVES"
    )
    report = find_dropped_content(f"{masthead}\n\nA bill to be entitled an act.",
                                  "A bill to be entitled an act.")
    assert report["missing_count"] == 0


def test_find_dropped_content_ignores_short_gaps() -> None:
    reference = "The quick brown fox jumped over the lazy dog near the river."
    output = "The quick brown fox jumped near the river."
    assert find_dropped_content(reference, output)["missing_count"] == 0


@pytest.mark.parametrize("reference, output", [("", "x"), ("x", ""), ("", "")])
def test_find_dropped_content_handles_empty_input(reference, output) -> None:
    assert find_dropped_content(reference, output)["missing_count"] == 0


# ---------------------------------------------------------------------------
# enforce_tag_integrity_gate — now runs on the visual-mode output before save
# ---------------------------------------------------------------------------


def test_gate_leaves_balanced_text_untouched() -> None:
    text = "Plain [A>added text<A] and [D>deleted<D] here."
    assert enforce_tag_integrity_gate(text, state_code="CO") == text


@pytest.mark.parametrize(
    "text, issue",
    [
        ("Start [A>added text with no closer.", "Removed unmatched [A> opener"),
        ("Start added text<A] with no opener.", "Removed unmatched <A] closer"),
    ],
)
def test_gate_repairs_unmatched_delimiters(text: str, issue: str) -> None:
    out = enforce_tag_integrity_gate(text, state_code="CO")
    assert out.count("[A>") == out.count("<A]") == 0
    assert _words(out) == _words(text)
    assert issue in enforce_tag_integrity_gate._last_report["issues"]


def test_gate_handles_empty_text() -> None:
    assert enforce_tag_integrity_gate("", state_code="CO") == ""
