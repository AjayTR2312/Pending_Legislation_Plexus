"""The content score must agree with the findings list, not be dragged down by things that are not defects."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import validation  # noqa: E402

CONTENT = {"accuracy_pct": 97.65, "precleaned_words": 38734}


def row(words, cat="content_changed"):
    return {"words_affected": words, "category": cat}


def test_footers_and_moved_text_no_longer_pull_the_score_down():
    # the checker's 97.65% was ~700 footer words + a moved block; only two tiny "text changed" rows remain listed
    acc = validation.adjusted_content_accuracy(CONTENT, [row(3), row(4)])
    assert acc == round(100 * (1 - 7 / 38734), 2) and acc > 99.9


def test_a_reordered_block_counts_as_moved_not_lost():
    assert validation.adjusted_content_accuracy(CONTENT, [row(70, "content_reordered")]) == 100.0 or \
        validation.adjusted_content_accuracy(CONTENT, [row(70, "content_reordered")]) > 99.99


def test_real_loss_still_lowers_the_score():
    acc = validation.adjusted_content_accuracy(CONTENT, [row(1000, "paragraph_drop")])
    assert acc < 98 and acc >= CONTENT["accuracy_pct"]  # never worse than the checker's own number


def test_never_lower_than_the_checkers_number():
    assert validation.adjusted_content_accuracy({"accuracy_pct": 99.0, "precleaned_words": 100}, [row(50)]) == 99.0


def test_no_baseline_means_no_score():
    assert validation.adjusted_content_accuracy(None, []) is None
    assert validation.adjusted_content_accuracy({"accuracy_pct": None, "precleaned_words": 10}, []) is None
