"""The model must drop page furniture silently, not write a note about it into the bill text."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import rules  # noqa: E402


def titles(text, state="IN"):
    return [f["title"] for f in rules.check_all(text, state, None)]


def test_a_note_about_a_removed_barcode_is_flagged():
    got = titles("SJ9609 removed as barcode text\nIntroduced Version\n")
    assert "The model's note about a removal is in the output" in got


def test_other_phrasings_are_flagged():
    assert "The model's note about a removal is in the output" in titles("Line numbers omitted as page furniture\nSection 1.\n".replace("page furniture", "line number text"))
    assert "The model's note about a removal is in the output" in titles("Footer removed because it is a page footer\nSection 1.\n")


def test_real_bill_text_with_similar_words_is_not_flagged():
    ok = "The commission shall be removed as chair if it fails to meet.\nA header shall be filed with the clerk.\n"
    assert "The model's note about a removal is in the output" not in titles(ok)
