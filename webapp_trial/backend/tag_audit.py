"""
LLM tag audit — Claude checklist review of formatting tags (model: app/llm_models.py).

Wraps Opus4.6/pymupdf_compare.py unchanged. This is the most expensive validation
layer (page images + checklist, one call per chunk), so it is never run
automatically — the UI must request it explicitly.

Results are cached per output file so repeat views cost nothing.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import Any, Optional

import db

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
APP_DIR = PROJECT_ROOT / "app"
OPUS_DIR = PROJECT_ROOT / "Opus4.6"
for _p in (APP_DIR, OPUS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

AUDIT_DIR = db.STORAGE_DIR / "audits"
log = logging.getLogger("trial.tag_audit")


def _cache_key(text_path: str, pdf_path: str) -> str:
    return hashlib.sha256(f"{text_path}|{pdf_path}".encode("utf-8")).hexdigest()[:16]


def cached_result(text_path: str, pdf_path: str) -> Optional[dict[str, Any]]:
    path = AUDIT_DIR / _cache_key(text_path, pdf_path) / "result.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
    return None


def _load_pymupdf_compare():
    spec = importlib.util.spec_from_file_location(
        "pymupdf_compare_trial", str(OPUS_DIR / "pymupdf_compare.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mark_flags(state_cfg: Optional[dict[str, Any]]) -> dict[str, bool]:
    """Which formatting the state treats as markup (same rules the Streamlit pipeline uses)."""
    if not state_cfg:
        return {}
    add, dele = state_cfg.get("add_markup", {}), state_cfg.get("delete_markup", {})
    return {
        "mark_underline": add.get("underline", True),
        "mark_italic": bool(add.get("italic", False) or dele.get("italic", False) or add.get("italic_section_only")),
        "mark_bold": add.get("bold", False),
        "mark_strikethrough": dele.get("strike_out", True),
        "mark_brackets": bool(add.get("brackets", False) or dele.get("brackets", False)),
    }


def _drop_out_of_scope_italics(report: dict[str, Any], tagged_text: str, state_cfg: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Some states (Indiana) only mark italics inside one section (PRINTING CODE): drop italic-only spans elsewhere,
    so the audit does not demand tags the state does not want."""
    scope = ((state_cfg or {}).get("add_markup") or {}).get("italic_section_only")
    if not scope or not report or "formatted_spans" not in report or not tagged_text:
        return report
    upper = tagged_text.upper()
    start = upper.find(scope.upper())
    if start < 0:
        return report
    end = upper.find("SECTION 1", start)
    section = tagged_text[start: end if end >= 0 else start + 2000]
    kept = []
    for span in report["formatted_spans"]:
        if span.get("format_types", []) == ["italic"]:
            text = (span.get("text") or "").strip()
            if not text or text not in section:
                continue
        kept.append(span)
    return {**report, "formatted_spans": kept}


def prepare_audit(pdf_path: str, state_code: Optional[str], out_dir: Path) -> tuple[dict[str, Any], list[str], str]:
    """Everything the audit needs before the model is called (free): the formatting report, the highlighted
    page images (base64) and the state's rules as text. Mirrors the Streamlit pipeline's step 6."""
    import rules
    from pymupdf_extractor import (
        extract_pdf_json_report, extract_pdf_with_pymupdf, highlight_pdf_formatting,
    )

    cfg = rules.state_spec(state_code)
    flags = _mark_flags(cfg)
    report = extract_pdf_json_report(pdf_path, state_code=state_code, **flags)
    if not report:
        raise RuntimeError("PyMuPDF produced no formatting report for this PDF")
    if (cfg or {}).get("add_markup", {}).get("italic_section_only"):
        tagged = extract_pdf_with_pymupdf(pdf_path, join_pages=True, state_code=state_code, **flags)
        report = _drop_out_of_scope_italics(report, tagged if isinstance(tagged, str) else "\n".join(tagged), cfg)

    highlight_dir = out_dir / "highlighted"
    highlight_dir.mkdir(parents=True, exist_ok=True)
    for old in highlight_dir.glob("page_*.png"):  # a previous run's pages must not be mixed in
        old.unlink()
    highlight_pdf_formatting(pdf_path, report=report, output_dir=highlight_dir, **flags)
    images_b64 = [base64.b64encode(img.read_bytes()).decode() for img in sorted(highlight_dir.glob("page_*.png"))]
    if not images_b64:
        raise RuntimeError("No highlighted page images were produced")

    parts = [f"State: {(cfg or {}).get('state_name') or state_code or 'unknown'}"]
    if cfg:
        a_cfg, d_cfg = cfg.get("add_markup", {}), cfg.get("delete_markup", {})
        parts += [f"Underline={a_cfg.get('underline', False)}", f"Strikethrough={d_cfg.get('strike_out', False)}",
                  f"Italic={a_cfg.get('italic', False)}", f"Bold={a_cfg.get('bold', False)}"]
        if d_cfg.get("strike_out") and (a_cfg.get("underline") or a_cfg.get("italic") or a_cfg.get("bold")):
            parts.append("PRIORITY: strikethrough + any other formatting → tag as [D>...<D] only, never [A>...<A]")
    return report, images_b64, ", ".join(parts)


def run_audit(text_path: str, pdf_path: str, state_code: Optional[str]) -> dict[str, Any]:
    """Run the LLM tag checklist audit. Costs tokens — call deliberately."""
    key = _cache_key(text_path, pdf_path)
    out_dir = AUDIT_DIR / key
    out_dir.mkdir(parents=True, exist_ok=True)

    pymupdf_report, images_b64, state_rules = prepare_audit(pdf_path, state_code, out_dir)

    import llm_models  # app/llm_models.py

    compare = _load_pymupdf_compare()
    xlsx_path = out_dir / "tag_audit.xlsx"
    llm_models.ledger_reset()
    with llm_models.phase("validation"):  # validation cost is tracked apart from conversion cost
        summary = compare.pymupdf_compare_tags(
            highlighted_images_b64=images_b64,
            pymupdf_report=pymupdf_report,
            pymupdf_clean_text=Path(text_path).read_text(encoding="utf-8"),
            state_code=state_code or "XX",
            state_rules=state_rules,
            output_xlsx=str(xlsx_path),
        )
    spent = llm_models.ledger_totals("validation")

    result = {
        "key": key,
        "passes": summary.get("passes", 0),
        "warnings": summary.get("warnings", 0),
        "errors": summary.get("errors", 0),
        "total": summary.get("total_checklist", 0),
        "xlsx_path": str(xlsx_path) if xlsx_path.exists() else None,
        "page_images": len(images_b64),
        "usage": {
            "input_tokens": spent["input_tokens"], "output_tokens": spent["output_tokens"],
            "cache_write_tokens": spent["cache_write_tokens"], "cache_read_tokens": spent["cache_read_tokens"],
            "cost_usd": round(spent["cost_usd"], 4), "api_calls": spent["api_calls"], "model": spent["model"],
        },
    }
    total = result["total"] or 0
    # An empty checklist is a result, not a failure: say why, so the screen does not just show zeros.
    spans = len(pymupdf_report.get("formatted_spans", []))
    result["spans_in_pdf"] = spans
    # "Verified" means the model really looked at the pages. With an empty checklist (a scanned PDF has no formatting
    # layer to compare against) nothing is sent, and any "passes" are just tags nobody checked: never show them as 100%.
    result["verified"] = spent["api_calls"] > 0
    if total == 0:
        if spans == 0:
            result["note"] = "The PDF has no underlined, bold or struck-through text that this state tags, so there was nothing to check."
        elif (state_code or "").upper() == "DC":
            result["note"] = (f"The PDF has {spans} formatted span(s), but DC only tags text in the body with printed line numbers "
                              "and none of them are there, so there was nothing to check.")
        else:
            result["note"] = f"The PDF has {spans} formatted span(s), but the state's rules exclude all of them, so there was nothing to check."
    elif not result["verified"]:
        result["note"] = (f"Not verified. The PDF has {spans} formatted span(s) to compare against (a scan has none), so the model was "
                          f"not called and the {total} tag(s) in the text were not checked. Compare them with the PDF by eye.")
    result["tag_accuracy_pct"] = (
        round(result["passes"] / total * 100, 2) if total and result["verified"] else None
    )

    (out_dir / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
