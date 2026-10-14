"""One reordered block must be one finding, not an "extra text" plus a "missing paragraph"."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import validation  # noqa: E402

A = ("pending, at the time this amended Article IV of this Constitution becomes effective, on May 14, 1951, "
     "in the Supreme Court as heretofore constituted shall be proceeded within the Supreme Court hereby established")
D = ("Supreme Court as heretofore constituted shall be proceeded within the Supreme Court hereby established "
     "pending, at the time this amended Article IV of this Constitution becomes effective, on May 14, 1951, in the")


def diff(pre, out, cat, sev="critical"):
    return {"precleaned": pre, "output": out, "category": cat, "severity": sev, "words_affected": 40, "type": "x"}


def rows_for(*diffs):
    rows, hidden = validation._issue_rows({"differences": list(diffs)})
    return rows


def test_same_words_in_two_places_become_one_reordered_finding():
    rows = rows_for(diff("", A, "content_added"), diff(D, "", "paragraph_drop"))
    assert len(rows) == 1
    r = rows[0]
    assert r["category"] == "content_reordered" and r["severity"] == "warning"
    assert r["expected"] == D and r["output"] == A            # both texts kept, so the UI can show them side by side
    assert "different order" in r["why"]


def test_unrelated_added_and_missing_text_stay_separate():
    rows = rows_for(diff("", "The Governor shall appoint a commission of nine members to review the statute", "content_added"),
                    diff("Judges hold office during good behaviour and receive a stated salary for services", "", "paragraph_drop"))
    assert sorted(r["category"] for r in rows) == ["content_added", "paragraph_drop"]


def test_short_fragments_are_never_paired():
    rows = rows_for(diff("", "of the Court", "content_added"), diff("the Court of", "", "content_drop"))
    assert len(rows) == 2


def test_one_missing_block_pairs_with_only_one_added_block():
    rows = rows_for(diff("", A, "content_added"), diff("", A, "content_added"), diff(D, "", "paragraph_drop"))
    assert sorted(r["category"] for r in rows) == ["content_added", "content_reordered"]
