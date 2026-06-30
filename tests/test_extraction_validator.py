"""
Unit tests for ``app/extraction_validator.py``.

These don't need a fixture PDF — they verify the validator's regex
catalogue catches the symptoms it claims to and stays quiet on text
that is known-clean.  Cheap, fast, and deterministic.
"""

from __future__ import annotations

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
