"""
Legislative Line Unwrapper
============================
Joins hard-wrapped continuation lines within each paragraph into a single
line.  Paragraphs are delimited by blank lines; those boundaries are preserved.

This is the *complement* of paragraph_splitter.py:
  - paragraph_splitter.py  →  inserts structural line breaks
  - line_unwrapper.py      →  removes hard-wrap line breaks

Typical workflow:
  1. unwrap  (this script)  — join wrapped lines into single-line paragraphs
  2. split   (paragraph_splitter) — re-insert breaks only at true subsection
     boundaries like (1), (2), etc.

Usage:
    from line_unwrapper import unwrap_lines, unwrap_file

    single_line_text = unwrap_lines(wrapped_text)
    unwrap_file("input.txt")                 # writes input_unwrapped.txt
    unwrap_file("input.txt", "output.txt")   # explicit output path
"""

from __future__ import annotations
import re, sys, glob


def unwrap_lines(text: str) -> str:
    """
    Join hard-wrapped lines within each paragraph into a single line.

    Rules:
      • Blank lines (or lines that are only whitespace) mark paragraph
        boundaries and are preserved as a single blank line.
      • Within a paragraph, consecutive non-blank lines are joined with
        a single space.
      • Leading/trailing whitespace on each line is stripped before joining.

    Parameters
    ----------
    text : str
        Hard-wrapped legislative text.

    Returns
    -------
    str
        Text with each paragraph collapsed to one line.
    """
    paragraphs: list[list[str]] = []
    current: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        if stripped == '':
            # paragraph boundary
            if current:
                paragraphs.append(current)
                current = []
            paragraphs.append([])        # represents the blank line
        else:
            current.append(stripped)

    if current:
        paragraphs.append(current)

    out_lines: list[str] = []
    for para in paragraphs:
        if not para:
            out_lines.append('')         # blank separator line
        else:
            out_lines.append(' '.join(para))

    return '\n'.join(out_lines) + '\n'


# ─────────────────────────────────────────────────────────────────────────
# File-level helpers
# ─────────────────────────────────────────────────────────────────────────
def unwrap_file(
    input_path: str,
    output_path: str | None = None,
) -> str:
    """Read a text file, unwrap lines, write result.  Returns output path."""
    with open(input_path, 'r', encoding='utf-8') as f:
        text = f.read()

    result = unwrap_lines(text)

    if output_path is None:
        base, ext = input_path.rsplit('.', 1)
        output_path = f"{base}_unwrapped.{ext}"

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(result)

    print(f"✅ Wrote {output_path}")
    return output_path


# ─────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    # Accept a filename or glob pattern
    if len(sys.argv) < 2:
        print("Usage: python line_unwrapper.py <file_or_glob> [output_file]")
        print("       python line_unwrapper.py *_converted.txt")
        sys.exit(1)

    pattern = sys.argv[1]
    explicit_out = sys.argv[2] if len(sys.argv) > 2 else None

    files = glob.glob(pattern)
    if not files:
        print(f"No files matched: {pattern}")
        sys.exit(1)

    for fp in sorted(files):
        # When processing multiple files, ignore explicit_out
        out = explicit_out if len(files) == 1 else None
        unwrap_file(fp, out)
