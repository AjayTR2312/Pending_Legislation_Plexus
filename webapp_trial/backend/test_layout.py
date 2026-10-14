"""The layout score and the layout list must describe the same problems."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import validation  # noqa: E402

LINES = ["", "October 24, 2024", "", "The Honorable Phil Mendelson", "Chairman", "Council of the District of Columbia",
         "", "The council shall consider the", "proposal at its next meeting.", ""]
LAYOUT = {
    "accuracy_pct": 63.2, "boundaries_checked": 5, "merge_checkpoints": 5,
    "splits_needed": [{"line": 8, "char_position": 3, "description": "Split required at line 8, position 3"}],
    "merges_needed": [
        {"line": 4, "action": "MERGE", "description": "Merge lines 4 and 5"},   # name + title: intentional
        {"line": 8, "action": "MERGE", "description": "Merge lines 8 and 9"},   # a wrapped sentence: real
    ],
    "merge_errors": [{"line": 8, "description": "Split required at line 8, position 3", "type": "merge"}],
}


def test_over_split_findings_are_listed_not_only_counted():
    rows = validation._layout_rows(LAYOUT, LINES)
    assert len(rows) == 3  # 1 split-needed + 2 merge-needed (the screen used to show only the first kind)


def test_short_lines_kept_apart_are_marked_probably_fine():
    rows = validation._layout_rows(LAYOUT, LINES)
    by_line = {(r["line"], r["description"][:5]): r for r in rows}
    assert by_line[(4, "Merge")]["likely_ok"] is True
    assert by_line[(8, "Merge")]["likely_ok"] is False


def test_score_excludes_only_the_probably_fine_ones_and_never_drops_below_raw():
    rows = validation._layout_rows(LAYOUT, LINES)
    # checkpoints 10; issues = 1 split + 2 merge = 3, minus 1 ignored = 2 -> 80%
    assert validation.adjusted_layout_accuracy(LAYOUT, rows) == 80.0
    assert validation.adjusted_layout_accuracy({**LAYOUT, "accuracy_pct": 95.0}, rows) == 95.0


def test_no_boundaries_means_the_checkers_value_is_kept():
    assert validation.adjusted_layout_accuracy({"accuracy_pct": 100.0, "boundary_detection_failed": True}, []) == 100.0
    assert validation.adjusted_layout_accuracy(None, []) is None
