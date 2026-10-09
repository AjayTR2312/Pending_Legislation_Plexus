"""Compare trial extraction against the flags Streamlit derives from the state
template, to explain character-count differences between the two UIs."""

import glob
import sys
from pathlib import Path

import conversion
import db

sys.path.insert(0, str(conversion.APP_DIR))


def flags_from_state(state_config: dict | None) -> dict:
    """Mirror _mark_flags in streamlit_app.process_pymupdf_clean_only."""
    flags = {
        "mark_underline": True,
        "mark_strikethrough": True,
        "mark_italic": False,
        "mark_bold": False,
        "mark_brackets": False,
    }
    if not state_config:
        return flags
    add_cfg = state_config.get("add_markup", {}) or {}
    del_cfg = state_config.get("delete_markup", {}) or {}
    flags["mark_underline"] = add_cfg.get("underline", True)
    flags["mark_italic"] = (
        add_cfg.get("italic", False)
        or del_cfg.get("italic", False)
        or bool(add_cfg.get("italic_section_only"))
    )
    flags["mark_bold"] = add_cfg.get("bold", False)
    flags["mark_strikethrough"] = del_cfg.get("strike_out", True)
    flags["mark_brackets"] = add_cfg.get("brackets", False) or del_cfg.get("brackets", False)
    return flags


def main() -> int:
    from pymupdf_extractor import extract_pdf_with_pymupdf

    matches = [p for p in glob.glob(str(db.JOBS_DIR / "**" / "*.pdf"), recursive=True)
               if "000299" in p]
    if not matches:
        raise SystemExit("test PDF not found")
    pdf = Path(matches[0])
    print(f"pdf: {pdf.name}\n")

    opus = conversion.get_opus()
    state_config = opus.load_state_template("FL")
    st_flags = flags_from_state(state_config)
    print("flags Streamlit would use for FL:")
    for k, v in st_flags.items():
        print(f"  {k:<22}: {v}")

    trial = extract_pdf_with_pymupdf(str(pdf), join_pages=True, state_code="FL")
    streamlit_like = extract_pdf_with_pymupdf(str(pdf), join_pages=True, state_code="FL", **st_flags)
    no_sep = extract_pdf_with_pymupdf(str(pdf), join_pages=True, state_code="FL", page_separator="")

    print(f"\ntrial defaults            : {len(trial):,} chars")
    print(f"streamlit-derived flags   : {len(streamlit_like):,} chars")
    print(f"defaults, no page separator: {len(no_sep):,} chars")
    print(f"\nflag delta      : {len(trial) - len(streamlit_like):,}")
    print(f"separator delta : {len(trial) - len(no_sep):,}")
    print(f"identical text  : {trial == streamlit_like}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
