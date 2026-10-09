"""Page furniture in the source extraction is not a defect, whatever the state's footer looks like."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import rules  # noqa: E402

DE_FOOTER = "DLS : MJC : AF 4801530050 Released: 01/16/2025 10:46 AM"


def test_delaware_tracking_footer_is_page_furniture():
    assert rules.explain_artifact(DE_FOOTER, "")


def test_footer_followed_by_a_margin_line_number_is_still_furniture():
    assert rules.explain_artifact(DE_FOOTER + " 2096", "")


def test_older_timestamp_with_seconds_still_recognised():
    assert rules.explain_artifact("1/2/2025 10:46:05 AM", "")


def test_real_text_is_never_hidden():
    assert rules.explain_artifact("Supreme Court as heretofore constituted shall be proceeded within the", "") is None
    assert rules.explain_artifact(DE_FOOTER + " The Chancellor shall serve", "") is None
    assert rules.explain_artifact("2096", "") is None  # a bare number is not evidence of a footer by itself
