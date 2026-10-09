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


def run_audit(text_path: str, pdf_path: str, state_code: Optional[str]) -> dict[str, Any]:
    """Run the LLM tag checklist audit. Costs tokens — call deliberately."""
    key = _cache_key(text_path, pdf_path)
    out_dir = AUDIT_DIR / key
    out_dir.mkdir(parents=True, exist_ok=True)

    from pymupdf_extractor import extract_text_with_formatting, highlight_pdf_formatting

    report_result = extract_text_with_formatting(pdf_path, include_report=True)
    pymupdf_report = (
        report_result.get("report") if isinstance(report_result, dict) else None
    )
    if not pymupdf_report:
        raise RuntimeError("PyMuPDF produced no formatting report for this PDF")

    highlight_dir = out_dir / "highlighted"
    highlight_pdf_formatting(pdf_path, report=pymupdf_report, output_dir=highlight_dir)

    images_b64 = [
        base64.b64encode(img.read_bytes()).decode()
        for img in sorted(highlight_dir.glob("page_*.png"))
    ]
    if not images_b64:
        raise RuntimeError("No highlighted page images were produced")

    import llm_models  # app/llm_models.py

    compare = _load_pymupdf_compare()
    xlsx_path = out_dir / "tag_audit.xlsx"
    llm_models.ledger_reset()
    with llm_models.phase("validation"):  # validation cost is tracked apart from conversion cost
        summary = compare.pymupdf_compare_tags(
            highlighted_images_b64=images_b64,
            pymupdf_report=pymupdf_report,
            pymupdf_clean_text=Path(text_path).read_text(encoding="utf-8"),
            state_code=state_code,
            state_rules=f"State: {state_code}",
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
    result["tag_accuracy_pct"] = (
        round(result["passes"] / total * 100, 2) if total else None
    )

    (out_dir / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
