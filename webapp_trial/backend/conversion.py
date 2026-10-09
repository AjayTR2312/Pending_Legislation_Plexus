"""
Conversion logic shared by the worker processes.

Wraps the existing production modules without modifying them. The Opus module is
loaded once per process and cached — processing.py reloaded all ~4,800 lines on
every job, which is wasteful under real load.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
APP_DIR = PROJECT_ROOT / "app"
OPUS_DIR = PROJECT_ROOT / "Opus4.6"

for _p in (APP_DIR, OPUS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

log = logging.getLogger("trial.conversion")

STEP_SEQUENCE = ["detect_state", "convert", "postprocess", "save"]

MODE_VISUAL = "claude_opus"
MODE_HYBRID = "hybrid"
MODE_CLEAN_ONLY = "pymupdf_clean_only"
SUPPORTED_MODES = {MODE_VISUAL, MODE_HYBRID, MODE_CLEAN_ONLY}

_opus_module: Any = None
_state_lookup_module: Any = None


def _load_module_from_path(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module {name} from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def get_opus():
    """Load Claude_Opus_4.6_Test.py once per process (filename contains dots)."""
    global _opus_module
    if _opus_module is None:
        log.info("Loading conversion engine from %s", OPUS_DIR)
        _opus_module = _load_module_from_path(
            "claude_opus_trial", OPUS_DIR / "Claude_Opus_4.6_Test.py"
        )
    return _opus_module


def get_state_lookup():
    global _state_lookup_module
    if _state_lookup_module is None:
        _state_lookup_module = _load_module_from_path(
            "state_id_lookup_trial", APP_DIR / "state_id_lookup.py"
        )
    return _state_lookup_module


def detect_state(filename: str, explicit: Optional[str] = None) -> Optional[str]:
    """Resolve the state code. Detection reads the NetScan numeric ID out of the
    filename, so the original upload name must be passed here, not a temp path."""
    if explicit:
        return explicit.strip().upper() or None

    try:
        code = get_state_lookup().detect_state_from_numerical_id(filename)
        if code:
            return code
    except Exception as exc:  # noqa: BLE001
        log.warning("Numeric-ID state detection failed: %s", exc)

    try:
        return get_opus().detect_state_from_filename(filename)
    except Exception as exc:  # noqa: BLE001
        log.warning("Filename-pattern state detection failed: %s", exc)
        return None


def convert_pdf(
    pdf_path: str,
    filename: str,
    state_code: Optional[str],
    output_dir: Path,
    on_step=None,
    mode: str = "hybrid",
) -> dict[str, Any]:
    """Run a conversion. Mirrors the corresponding Streamlit pipeline per mode.

    on_step(step, status, message) is called for progress reporting.
    """

    def report(step: str, status: str, message: str = "") -> None:
        if on_step is not None:
            on_step(step, status, message)

    opus = get_opus()
    if hasattr(opus, "reset_token_tracker"):
        opus.reset_token_tracker()
    import llm_models  # app/llm_models.py (on sys.path above)

    llm_models.reset_usage()
    llm_models.ledger_reset()

    report("detect_state", "running")
    resolved_state = detect_state(filename, state_code)
    report("detect_state", "done", resolved_state or "unknown — using default prompt")

    client = opus.get_anthropic_client()
    if not client:
        raise RuntimeError("Anthropic client could not be initialized")

    state_config = opus.load_state_template(resolved_state) if resolved_state else None

    report("convert", "running", f"{mode} conversion")
    _convert_clean_only.last_usage = None
    if mode == MODE_VISUAL:
        converted_text = _convert_visual(opus, client, pdf_path, state_config)
    elif mode == MODE_CLEAN_ONLY:
        converted_text = _convert_clean_only(client, pdf_path, resolved_state)
    else:
        converted_text = opus.convert_pdf_hybrid_text(
            client, pdf_path, state_config, resolved_state
        )
    if not converted_text:
        raise RuntimeError("Conversion returned an empty result")
    report("convert", "done", f"{len(converted_text):,} characters")

    report("postprocess", "running")
    converted_text = opus.deterministic_cleanup(
        converted_text, state_config=state_config, state_code=resolved_state
    )
    if state_config:
        converted_text = opus.post_process_output(converted_text, state_config)
    converted_text = _apply_integrity_passes(converted_text, resolved_state)
    report("postprocess", "done")

    report("save", "running")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / (Path(filename).stem + ".txt")
    if not opus.save_text_file(converted_text, str(output_path)):
        raise RuntimeError(f"Failed to write output to {output_path}")
    report("save", "done", output_path.name)

    usage = {}
    if hasattr(opus, "get_token_tracker"):
        try:
            tracker = opus.get_token_tracker()
            usage = {
                "input_tokens": tracker.get("total_input", 0),
                "output_tokens": tracker.get("total_output", 0),
                "cost_usd": round(tracker.get("total_cost", 0.0), 4),
                "api_calls": len(tracker.get("calls", [])),
            }
        except Exception:  # noqa: BLE001 — cost reporting is best-effort
            usage = {}

    extra = getattr(_convert_clean_only, "last_usage", None)
    if extra:
        inp = extra.get("input_tokens", 0)
        out = extra.get("output_tokens", 0)
        usage["input_tokens"] = usage.get("input_tokens", 0) + inp
        usage["output_tokens"] = usage.get("output_tokens", 0) + out
        usage["api_calls"] = usage.get("api_calls", 0) + 1
        try:
            cost = opus._calculate_cost(inp, out, extra.get("model") or llm_models.PRIMARY_MODEL)
            usage["cost_usd"] = round(usage.get("cost_usd", 0.0) + cost, 4)
        except Exception:  # noqa: BLE001
            pass

    # The ledger records every API call exactly once, with its cache tokens and the model that answered.
    # Conversion cost is reported on its own; validation cost is added later, and only if someone runs it.
    totals = llm_models.ledger_totals("conversion")
    if totals["api_calls"]:
        usage = {
            "input_tokens": totals["input_tokens"], "output_tokens": totals["output_tokens"],
            "cache_write_tokens": totals["cache_write_tokens"], "cache_read_tokens": totals["cache_read_tokens"],
            "cache_saving_usd": round(totals["cache_saving_usd"], 4),
            "cost_usd": round(totals["cost_usd"], 4), "api_calls": totals["api_calls"],
        }

    summary = llm_models.usage_summary()
    usage["model"] = ", ".join(summary["models"]) or llm_models.PRIMARY_MODEL
    usage["fallback_used"] = summary["fallback_used"]
    if summary["fallback_used"]:
        ev = summary["fallback_events"][0]
        report("convert", "warning",
               f"{ev['requested']} could not serve the request — used fallback {ev['used']} "
               f"({len(summary['fallback_events'])} call(s))")

    return {
        "output_path": str(output_path),
        "state_code": resolved_state,
        "char_count": len(converted_text),
        "mode": mode,
        "usage": usage,
    }


def _convert_visual(opus, client, pdf_path: str, state_config) -> Optional[str]:
    """Page-image conversion for scanned PDFs."""
    prompt = opus.get_conversion_prompt(state_config)
    result = opus.convert_pdf_to_text(client, pdf_path, prompt)
    return result[0] if isinstance(result, tuple) else result


def _convert_clean_only(client, pdf_path: str, state_code: Optional[str]) -> Optional[str]:
    """PyMuPDF extraction followed by a single LLM cleaning pass."""
    from pymupdf_extractor import clean_extracted_text, extract_pdf_with_pymupdf

    raw = extract_pdf_with_pymupdf(pdf_path, join_pages=True, state_code=state_code)
    if not raw:
        return None
    cleaned = clean_extracted_text(
        raw, state_code=state_code, pdf_path=pdf_path, anthropic_client=client
    )
    # This path bills through pymupdf_extractor, which keeps its own usage
    # record rather than the Opus module's tracker.
    _convert_clean_only.last_usage = getattr(clean_extracted_text, "_last_usage", None)
    return cleaned


def _apply_integrity_passes(text: str, state_code: Optional[str]) -> str:
    """Soft-wrap rejoin, protected clauses and the tag integrity gate.

    The Streamlit pipelines run these; app/pipeline_runner.py does not. Running
    them here keeps trial output consistent with what a reviewer sees in the UI.
    """
    try:
        from pymupdf_extractor import (
            enforce_tag_integrity_gate,
            rejoin_mid_sentence_line_breaks,
        )

        text = rejoin_mid_sentence_line_breaks(text)
        text = enforce_tag_integrity_gate(text, state_code=state_code)
    except Exception as exc:  # noqa: BLE001
        log.warning("Integrity passes skipped: %s", exc)

    try:
        from protected_clauses import strip_protected_tags

        if state_code:
            text = strip_protected_tags(text, state_code)
    except Exception as exc:  # noqa: BLE001
        log.warning("Protected-clause stripping skipped: %s", exc)

    return text
