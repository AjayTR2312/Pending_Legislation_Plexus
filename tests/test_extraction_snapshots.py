"""
Snapshot tests for the PyMuPDF extractor pipeline.

Each subdirectory under ``tests/fixtures/`` represents one real-world PDF
that previously triggered a layout-extraction bug.  Layout:

    tests/fixtures/<bill_id>/
    ├── input.pdf              # the source PDF kept in the repo
    └── expected_excerpts.txt  # one stable substring per line:
                               #   "<text>"  → MUST appear in output
                               #   "!<text>" → must NOT appear in output
                               # blank lines and "#…" comments are ignored

The test extracts the PDF with :func:`extract_pdf_with_pymupdf` (the
same entry point used by the Streamlit pipeline) and then runs the
deterministic post-processors that don't require the LLM.  It then
asserts:

  * every *required* excerpt appears verbatim in the resulting text;
  * every *forbidden* (``!``-prefixed) excerpt is absent.

This catches:

    * regressions in line-ordering (e.g. the 2026-06-29 "$ before
      number" bug fixed by ``_sort_block_lines_visually``);
    * regressions in :func:`consolidate_adjacent_tags`;
    * accidental tag-marker corruption;
    * resurgence of state-specific false-positive tags
      (e.g. the AR ``[A>2<A]`` leader-line bug fixed by
      ``_filter_ar_leader_lines``).

The tests intentionally do NOT call ``clean_extracted_text`` because
that depends on an Anthropic API key.  Add LLM-dependent assertions in a
separate, opt-in test module if/when needed.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import pytest

import pymupdf_extractor
from extraction_validator import validate_extraction


_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _discover_fixtures() -> List[Path]:
    """Return every fixture directory that contains the required files."""
    if not _FIXTURES_DIR.exists():
        return []
    out: List[Path] = []
    for child in sorted(_FIXTURES_DIR.iterdir()):
        if not child.is_dir():
            continue
        if (child / "input.pdf").exists() and (child / "expected_excerpts.txt").exists():
            out.append(child)
    return out


def _load_excerpts(path: Path) -> tuple[List[str], List[str]]:
    """Parse expected_excerpts.txt → ``(required, forbidden)``.

    Each non-comment line is either:
      * ``!<string>`` — *forbidden*: the extractor output must NOT
        contain ``<string>``.  Used to lock in negative assertions
        like "the AR ADMINISTRATIVE SPECIALIST row must not produce
        the stray ``[A>2<A]`` tag we just fixed."
      * ``<string>`` — *required*: the output MUST contain it verbatim.

    Lines that are blank or begin with ``#`` are ignored.
    """
    required: List[str] = []
    forbidden: List[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        # Preserve trailing whitespace inside the line; strip only the
        # surrounding newline.  Skip blank lines and "#" comments.
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if raw_line.startswith("!"):
            forbidden.append(raw_line[1:])
        else:
            required.append(raw_line)
    return required, forbidden


def _extract_deterministic(pdf_path: Path, state_code: str | None) -> str:
    """Run the extractor + deterministic post-processors (NO LLM)."""
    text = pymupdf_extractor.extract_pdf_with_pymupdf(
        str(pdf_path),
        join_pages=True,
        state_code=state_code,
    )
    # Apply the same consolidator the Streamlit pipeline runs after
    # ``clean_extracted_text``.  Without it the tag boundaries don't
    # match the cleaned output users see.
    return pymupdf_extractor.consolidate_adjacent_tags(text)


# ---------------------------------------------------------------------------
# Parametrised over every fixture directory
# ---------------------------------------------------------------------------


_FIXTURES = _discover_fixtures()


@pytest.mark.parametrize(
    "fixture_dir",
    _FIXTURES,
    ids=[f.name for f in _FIXTURES] if _FIXTURES else ["NO_FIXTURES"],
)
def test_expected_excerpts_present(fixture_dir: Path) -> None:
    """Every excerpt in expected_excerpts.txt must appear in the output."""
    pdf_path = fixture_dir / "input.pdf"
    excerpts_path = fixture_dir / "expected_excerpts.txt"

    # State code is the leading uppercase letters of the directory name
    # when present (e.g. "delaware_8_2_2_000230" → no upper-case ⇒ infer
    # "DE" from the leading word).  We use a tiny hand-rolled map so the
    # test stays dependency-free.
    _STATE_MAP = {
        "delaware": "DE",
        "connecticut": "CT",
        "california": "CA",
        "kansas": "KS",
        "indiana": "IN",
        "iowa": "IA",
        "arkansas": "AR",
        "dc": "DC",
        "florida": "FL",
    }
    leading = fixture_dir.name.split("_", 1)[0].lower()
    state_code = _STATE_MAP.get(leading)

    text = _extract_deterministic(pdf_path, state_code)

    required, forbidden = _load_excerpts(excerpts_path)
    assert required or forbidden, (
        f"Fixture {fixture_dir.name} has no expected or forbidden excerpts"
    )

    missing = [e for e in required if e not in text]
    assert not missing, (
        f"\nFixture {fixture_dir.name} missing {len(missing)} expected "
        f"excerpt(s):\n  - "
        + "\n  - ".join(repr(e) for e in missing)
        + f"\n\nExtractor output (first 2000 chars):\n{text[:2000]}"
    )

    present_forbidden = [e for e in forbidden if e in text]
    assert not present_forbidden, (
        f"\nFixture {fixture_dir.name} contains {len(present_forbidden)} "
        f"FORBIDDEN excerpt(s) that should have been stripped:\n  - "
        + "\n  - ".join(repr(e) for e in present_forbidden)
        + f"\n\nExtractor output (first 2000 chars):\n{text[:2000]}"
    )


@pytest.mark.parametrize(
    "fixture_dir",
    _FIXTURES,
    ids=[f.name for f in _FIXTURES] if _FIXTURES else ["NO_FIXTURES"],
)
def test_no_critical_extraction_anomalies(fixture_dir: Path) -> None:
    """The validator must not raise *error*-severity anomalies on any fixture.

    Warning- and info-level anomalies are allowed (they're advisory),
    but anything escalated to *error* indicates an unambiguous bug that
    used to ship undetected.  Adding a new error-severity pattern to
    ``extraction_validator.py`` should be paired with fixture changes
    until this test stays green.
    """
    pdf_path = fixture_dir / "input.pdf"
    text = _extract_deterministic(pdf_path, state_code=None)

    anomalies = validate_extraction(
        text,
        pdf_basename=pdf_path.name,
    )
    errors = [a for a in anomalies if a.severity == "error"]
    assert not errors, (
        f"\nFixture {fixture_dir.name} produced {len(errors)} ERROR-level "
        f"extraction anomalies:\n"
        + "\n".join(f"  [{a.kind}] {a.snippet}" for a in errors[:20])
    )
