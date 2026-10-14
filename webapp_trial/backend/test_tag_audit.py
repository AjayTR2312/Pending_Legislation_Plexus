"""The tag audit used to import a function that does not exist, so the button never worked.
These tests run everything except the model call (a fake stands in, so no tokens are spent)."""
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import tag_audit  # noqa: E402


@pytest.fixture()
def pdf(tmp_path):
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "SECTION 1. The court shall hear the appeal.", fontsize=11)
    page.insert_text((72, 130), "Deleted words and added words follow.", fontsize=11)
    path = tmp_path / "bill.pdf"
    doc.save(str(path))
    return str(path)


def test_prepare_audit_builds_report_images_and_state_rules(pdf, tmp_path):
    report, images, rules_text = tag_audit.prepare_audit(pdf, "IN", tmp_path / "out")
    assert isinstance(report, dict) and "formatted_spans" in report
    assert len(images) == 1 and len(images[0]) > 1000          # one base64 PNG per page
    assert "Indiana" in rules_text and "Bold=True" in rules_text


def test_prepare_audit_never_mixes_in_pages_from_an_earlier_run(pdf, tmp_path):
    out = tmp_path / "out"
    (out / "highlighted").mkdir(parents=True)
    (out / "highlighted" / "page_099.png").write_bytes(b"stale")
    _, images, _ = tag_audit.prepare_audit(pdf, "FL", out)
    assert len(images) == 1


def test_mark_flags_follow_the_state_spec():
    flags = tag_audit._mark_flags({"add_markup": {"underline": False, "bold": True, "italic_section_only": "PRINTING CODE"},
                                   "delete_markup": {"strike_out": True}})
    assert flags["mark_bold"] is True and flags["mark_underline"] is False
    assert flags["mark_italic"] is True and flags["mark_strikethrough"] is True


def test_italics_outside_the_scoped_section_are_dropped():
    cfg = {"add_markup": {"italic_section_only": "PRINTING CODE"}}
    text = "intro italic words\nPRINTING CODE: additions in this italic block\nSECTION 1. body"
    report = {"formatted_spans": [
        {"text": "intro italic words", "format_types": ["italic"]},
        {"text": "additions in this italic block", "format_types": ["italic"]},
        {"text": "bold thing", "format_types": ["bold"]},
    ]}
    kept = [s["text"] for s in tag_audit._drop_out_of_scope_italics(report, text, cfg)["formatted_spans"]]
    assert kept == ["additions in this italic block", "bold thing"]


def test_run_audit_calls_the_model_function_with_arguments_it_accepts(pdf, tmp_path, monkeypatch):
    monkeypatch.setattr(tag_audit, "AUDIT_DIR", tmp_path / "audits")
    real = tag_audit._load_pymupdf_compare()
    seen = {}

    def fake(**kwargs):
        inspect.signature(real.pymupdf_compare_tags).bind(**kwargs)   # raises if the real function would reject them
        seen.update(kwargs)
        import llm_models  # a real call is recorded by the client wrapper; do the same here
        llm_models._ledger_add("claude-opus-4-8", SimpleNamespace(
            input_tokens=1000, output_tokens=100, cache_creation_input_tokens=0, cache_read_input_tokens=0))
        return {"passes": 3, "warnings": 1, "errors": 0, "total_checklist": 4}

    monkeypatch.setattr(tag_audit, "_load_pymupdf_compare", lambda: SimpleNamespace(pymupdf_compare_tags=fake))
    text = tmp_path / "out.txt"
    text.write_text("SECTION 1. The court shall hear the appeal.\n", encoding="utf-8")
    result = tag_audit.run_audit(str(text), pdf, "IN")
    assert result["tag_accuracy_pct"] == 75.0 and result["page_images"] == 1
    assert result["usage"]["cost_usd"] > 0 and result["verified"] is True
    assert seen["state_code"] == "IN" and "Indiana" in seen["state_rules"]
    assert tag_audit.cached_result(str(text), pdf)["passes"] == 3


def test_an_empty_checklist_is_reported_as_nothing_to_audit_not_as_zeros(pdf, tmp_path, monkeypatch):
    monkeypatch.setattr(tag_audit, "AUDIT_DIR", tmp_path / "audits")
    called = []
    fake = lambda **kw: (called.append(1), {"passes": 0, "warnings": 0, "errors": 0, "total_checklist": 0})[1]  # noqa: E731
    monkeypatch.setattr(tag_audit, "_load_pymupdf_compare", lambda: SimpleNamespace(pymupdf_compare_tags=fake))
    text = tmp_path / "out.txt"
    text.write_text("SECTION 1. The court shall hear the appeal.\n", encoding="utf-8")
    result = tag_audit.run_audit(str(text), pdf, "DC")
    assert result["total"] == 0 and result["tag_accuracy_pct"] is None
    assert result["usage"]["api_calls"] == 0 and result["usage"]["cost_usd"] == 0
    assert "nothing to check" in result["note"]
    assert result["spans_in_pdf"] == 0


def test_tags_nobody_checked_are_not_reported_as_100_percent(pdf, tmp_path, monkeypatch):
    """A scanned PDF has no formatting layer: the checklist is empty, the model is never called, and the checker
    still counts the tags in the text as "pass". That must show as not verified, not as 100%."""
    monkeypatch.setattr(tag_audit, "AUDIT_DIR", tmp_path / "audits")
    fake = lambda **kw: {"passes": 3, "warnings": 0, "errors": 0, "total_checklist": 3}  # noqa: E731 - no ledger entry: no call
    monkeypatch.setattr(tag_audit, "_load_pymupdf_compare", lambda: SimpleNamespace(pymupdf_compare_tags=fake))
    text = tmp_path / "out.txt"
    text.write_text("[A>D.C. ACT 26-3<A]\n", encoding="utf-8")
    result = tag_audit.run_audit(str(text), pdf, "DC")
    assert result["verified"] is False and result["tag_accuracy_pct"] is None
    assert "Not verified" in result["note"] and "3 tag(s)" in result["note"]
