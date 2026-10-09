"""
Upload-time PDF analysis and processing-mode routing.

Reuses app/pdf_type_detector.py unchanged and mirrors the routing rules in
app/streamlit_app.py (show_single_file_processing, approx. lines 8211-8310):

    scanned            -> visual  (requires human approval first)
    state in CO        -> visual
    pages > 15         -> hybrid
    otherwise          -> pymupdf_clean_only

Only `hybrid` is wired to a real converter in the trial today; the others are
reported so the UI can show the same routing decision Streamlit would make.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
APP_DIR = PROJECT_ROOT / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

log = logging.getLogger("trial.analysis")

from appconfig import settings

MAX_PAGES = settings.MAX_PAGES_PYMUPDF
MAX_PAGES_VISION = settings.MAX_PAGES_VISION
VISION_ONLY_STATES = {"CO"}

MODE_VISUAL = "claude_opus"
MODE_HYBRID = "hybrid"
MODE_CLEAN_ONLY = "pymupdf_clean_only"

# All three routed modes are implemented by the trial worker.
IMPLEMENTED_MODES = {MODE_HYBRID, MODE_VISUAL, MODE_CLEAN_ONLY}


def page_count(pdf_path: str) -> Optional[int]:
    try:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(pdf_path)
        n = len(doc)
        doc.close()
        return n
    except Exception as exc:  # noqa: BLE001
        log.warning("Page count failed for %s: %s", pdf_path, exc)
        return None


def detect_pdf_type(pdf_path: str) -> Optional[dict[str, Any]]:
    try:
        from pdf_type_detector import detect_pdf_type as _detect

        return _detect(pdf_path)
    except Exception as exc:  # noqa: BLE001
        log.warning("PDF type detection failed for %s: %s", pdf_path, exc)
        return None


def _display_type(analysis: Optional[dict[str, Any]]) -> str:
    """Match the label Streamlit shows, including its hybrid display rule."""
    if not analysis:
        return "unknown"
    kind = analysis.get("type", "unknown")
    if kind == "digital":
        any_images = any(d.get("has_images") for d in analysis.get("details", []))
        return "hybrid" if any_images else "digital"
    return kind


def analyse(pdf_path: str, state_code: Optional[str]) -> dict[str, Any]:
    """Return PDF facts plus the processing mode Streamlit would choose."""
    analysis = detect_pdf_type(pdf_path)
    pages = page_count(pdf_path)
    if pages is None and analysis:
        pages = analysis.get("total_pages")

    raw_type = (analysis or {}).get("type", "unknown")
    is_scanned = raw_type == "scanned"
    state = (state_code or "").upper() or None

    if is_scanned:
        mode = MODE_VISUAL
        reason = "Scanned PDF — image-based conversion required"
    elif state in VISION_ONLY_STATES:
        mode = MODE_VISUAL
        reason = f"{state} always uses the visual pipeline"
    elif pages is not None and pages > MAX_PAGES:
        mode = MODE_HYBRID
        reason = f"Long PDF ({pages} pages) — chunked hybrid conversion"
    else:
        mode = MODE_CLEAN_ONLY
        reason = f"Standard PDF ({pages if pages is not None else '?'} pages)"

    page_limit = MAX_PAGES_VISION if mode == MODE_VISUAL else MAX_PAGES

    return {
        "pdf_type": raw_type,
        "display_type": _display_type(analysis),
        "page_count": pages,
        "is_scanned": is_scanned,
        "requires_approval": is_scanned,
        "mode": mode,
        "mode_reason": reason,
        "mode_implemented": mode in IMPLEMENTED_MODES,
        "page_limit": page_limit,
        "over_page_limit": bool(pages is not None and pages > page_limit),
        "has_signatures": (analysis or {}).get("has_signatures", False),
        "has_embedded_images": (analysis or {}).get("has_embedded_images", False),
        "pages_with_images": (analysis or {}).get("pages_with_images", 0),
        "average_chars_per_page": (analysis or {}).get("average_chars_per_page"),
    }
