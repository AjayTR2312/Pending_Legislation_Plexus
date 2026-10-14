"""The Rules tab lists every jurisdiction in the spec, with its instructions and its checks."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import rules  # noqa: E402


def test_every_state_in_the_spec_is_listed_once_and_in_order():
    codes = [s["code"] for s in rules.overview()["states"]]
    assert codes == sorted(rules.load_spec()["states"]) and len(codes) == len(set(codes)) and len(codes) >= 20


def test_each_state_carries_its_name_markup_and_the_specs_own_instructions():
    ov = {s["code"]: s for s in rules.overview()["states"]}
    dc, ind = ov["DC"], ov["IN"]
    assert dc["spec"]["name"] == "District of Columbia" and "underline" in dc["spec"]["add"]
    assert dc["spec"]["tag_scope"] == "line_numbered_only"
    assert any("visually underlined" in t for t in dc["instructions"]["add"])          # the full wording, not a summary
    assert "bold" in ind["spec"]["add"] and any("PRINTING CODE" in t for t in ind["instructions"]["add"])


def test_global_rules_are_listed_once_and_state_rules_only_under_their_state():
    ov = rules.overview()
    assert len(ov["global"]) >= 5 and all(r["scope"] == "ALL" for r in ov["global"])
    assert all(r["scope"] != "ALL" for s in ov["states"] for r in s["rules"])
    asterisk = {s["code"] for s in ov["states"] if any(r["id"] == "fmt_asterisk" for r in s["rules"])}
    assert asterisk == {"DC", "IN", "DE", "KS", "IA"}                                   # exactly the states that rule names


def test_the_overview_is_json_friendly():
    import json
    assert json.dumps(rules.overview())
