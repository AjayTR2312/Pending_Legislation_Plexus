"""
Word-level PDF ↔ text mapping for the compare viewer.

Mirrors the approach in app/pdf_compare_viewer.py (PyMuPDF word boxes plus a
global SequenceMatcher alignment) but without the Streamlit coupling — that
module uses @st.cache_data and st.info at import/call time, which cannot run
inside the API process.

Page images are rendered once to disk and served by URL. Returning them as
base64 inside the JSON payload would make the response tens of megabytes.
Bounding boxes are normalised to 0..1 so the frontend can scale them to
whatever size it renders the page at.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

import db
from appconfig import settings

CACHE_DIR = db.STORAGE_DIR / "compare"
RENDER_DPI = settings.COMPARE_RENDER_DPI
CACHE_VERSION = 2  # bump when meta.json / words.json layout changes

log = logging.getLogger("trial.compare")

_TAG_RE = re.compile(r"\[A>|<A\]|\[D>|<D\]")


def _key(pdf_path: str, text_path: str) -> str:
    return hashlib.sha256(f"{pdf_path}|{text_path}".encode("utf-8")).hexdigest()[:16]


def _strip_tags(value: str) -> str:
    return _TAG_RE.sub("", value)


def _normalise(value: str) -> str:
    return re.sub(r"[^\w]", "", value.lower())


def _render_pages(pdf_path: Path, out_dir: Path) -> list[dict[str, Any]]:
    import pymupdf

    doc = pymupdf.open(str(pdf_path))
    matrix = pymupdf.Matrix(RENDER_DPI / 72, RENDER_DPI / 72)
    pages = []
    try:
        for index, page in enumerate(doc):
            image_path = out_dir / f"page_{index}.png"
            if not image_path.exists():
                page.get_pixmap(matrix=matrix).save(str(image_path))
            rect = page.rect
            pages.append({
                "index": index,
                "width_pt": rect.width,
                "height_pt": rect.height,
                "aspect": (rect.height / rect.width) if rect.width else 1.414,
            })
    finally:
        doc.close()
    return pages


def _extract_words(pdf_path: Path) -> list[dict[str, Any]]:
    import pymupdf

    doc = pymupdf.open(str(pdf_path))
    words: list[dict[str, Any]] = []
    try:
        for index, page in enumerate(doc):
            rect = page.rect
            native = page.get_text("words")
            if len(native) < 5:
                # Likely a scanned page — try the OCR text layer if available.
                try:
                    tp = page.get_textpage_ocr(flags=0, full=True)
                    native = page.get_text("words", textpage=tp)
                except Exception:  # noqa: BLE001 — OCR is optional
                    pass
            for w in native:
                words.append({
                    "text": w[4],
                    "page": index,
                    "x": w[0] / rect.width if rect.width else 0,
                    "y": w[1] / rect.height if rect.height else 0,
                    "w": (w[2] - w[0]) / rect.width if rect.width else 0,
                    "h": (w[3] - w[1]) / rect.height if rect.height else 0,
                })
    finally:
        doc.close()
    return words


def _filtered_words(words: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    """PDF words with a non-empty normalised form — the alignment's PDF side."""
    pdf_norm, pdf_info = [], []
    for w in words:
        norm = _normalise(w["text"])
        if norm:
            pdf_norm.append(norm)
            pdf_info.append(w)
    return pdf_norm, pdf_info


def _build_map(text_lines: list[str], words: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Align PDF words to output words, keyed "lineIndex_wordIndex".

    Each entry also carries "i", the word's index in the filtered PDF word list
    (see words.json), so findings can be located in either direction.
    """
    pdf_norm, pdf_info = _filtered_words(words)

    out_norm, out_keys = [], []
    for li, line in enumerate(text_lines):
        for wi, raw in enumerate(line.split(" ")):
            norm = _normalise(_strip_tags(raw))
            if norm:
                out_norm.append(norm)
                out_keys.append(f"{li}_{wi}")

    if not pdf_norm or not out_norm:
        return {}

    matcher = difflib.SequenceMatcher(None, pdf_norm, out_norm, autojunk=False)
    mapping: dict[str, dict[str, Any]] = {}
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op != "equal":
            continue
        for offset in range(i2 - i1):
            info = pdf_info[i1 + offset]
            mapping[out_keys[j1 + offset]] = {
                "page": info["page"],
                "x": round(info["x"], 5),
                "y": round(info["y"], 5),
                "w": round(info["w"], 5),
                "h": round(info["h"], 5),
                "i": i1 + offset,
            }
    return mapping


def build_compare(pdf_path: str, text_path: str, text: str) -> dict[str, Any]:
    """Render pages and align words. Cached on disk per (pdf, text) pair."""
    key = _key(pdf_path, text_path)
    out_dir = CACHE_DIR / key
    out_dir.mkdir(parents=True, exist_ok=True)

    lines = text.splitlines()
    meta_file = out_dir / "meta.json"
    fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    if meta_file.exists():
        try:
            cached = json.loads(meta_file.read_text(encoding="utf-8"))
            if (
                cached.get("fingerprint") == fingerprint
                and cached.get("version") == CACHE_VERSION
                and (out_dir / "words.json").exists()
            ):
                cached["lines"] = lines
                return cached
        except json.JSONDecodeError:
            pass

    pages = _render_pages(Path(pdf_path), out_dir)
    words = _extract_words(Path(pdf_path))
    mapping = _build_map(lines, words)

    _, pdf_info = _filtered_words(words)
    (out_dir / "words.json").write_text(
        json.dumps([
            [_normalise(w["text"]), w["page"], round(w["x"], 5), round(w["y"], 5),
             round(w["w"], 5), round(w["h"], 5)]
            for w in pdf_info
        ]),
        encoding="utf-8",
    )

    result = {
        "key": key,
        "version": CACHE_VERSION,
        "fingerprint": fingerprint,
        "pages": pages,
        "map": mapping,
        "mapped_words": len(mapping),
        "pdf_words": len(words),
    }
    meta_file.write_text(json.dumps(result), encoding="utf-8")
    result["lines"] = lines
    return result


def load_words(key: str) -> list[list[Any]]:
    """Filtered PDF words: [norm, page, x, y, w, h], index-aligned with map["i"]."""
    path = CACHE_DIR / key / "words.json"
    if not path.is_file():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def page_image(key: str, page: int) -> Optional[Path]:
    candidate = (CACHE_DIR / key / f"page_{int(page)}.png").resolve()
    try:
        candidate.relative_to(CACHE_DIR.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None
