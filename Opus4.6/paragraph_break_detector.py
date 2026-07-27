"""
Paragraph Break Detector for Precleaned Legislative Text Files.

Identifies and marks paragraph breaks based on structural patterns found in
legislative documents (e.g., Kansas Senate Rules, bills, resolutions).

Patterns detected:
  - Page separators: "--- Page X ---"
  - Rule headings: [A>Rule N. Title<A]
  - Standalone tagged headers: [A>...<A] on their own line
  - Lettered subsections: (a), (b), (c)...
  - Numbered subsections: (1), (2), (3)...
  - Numbered list items: 1. , 2. , 10. ...
  - Blank lines (existing paragraph breaks)
  - Indented table-like lines (committee listings)
"""

import re
import sys
from pathlib import Path


# --- Table Marker Utilities ---

_TABLE_MARKER_RE = re.compile(r"^---TABLE (?:START|END)---\n?", re.MULTILINE)


def strip_table_markers(text: str) -> str:
    """Remove ---TABLE START--- / ---TABLE END--- markers from text.

    Use this before sending text to the LLM, content-accuracy checker,
    or audit pipeline.  The markers are kept only in the *_unwrapped.txt*
    file for human readability.
    """
    return _TABLE_MARKER_RE.sub("", text)


# --- Pattern Definitions ---

PATTERNS = {
    "page_separator": re.compile(r"^--- Page \d+ ---$"),
    "page_header": re.compile(r"^(SR|HB|SB|HCR|SCR|HR)\s+\d+\s+\d+$"),
    "rule_heading": re.compile(r"^\[A>Rule \d+\..+<A\]"),
    "tagged_header": re.compile(r"^\[A>.+<A\]$"),
    "alpha_subsection": re.compile(r"^\([a-z]\)\s"),
    "numeric_subsection": re.compile(r"^\(\d+\)\s"),
    "numbered_list": re.compile(r"^\d+\.\s"),
    "table_row": re.compile(r".{2,}\s{4,}\S"),  # text + 4+ spaces + more text = table
    "short_standalone": re.compile(r"^.{1,20}$"),  # very short lines (table headers, labels)
    "blank_line": re.compile(r"^$"),
}


def detect_paragraph_breaks(text: str) -> list[dict]:
    """
    Analyze text and return a list of line entries with break classification.

    Returns:
        List of dicts with keys: line_num, text, break_type, is_break
    """
    lines = text.split("\n")
    results = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        break_type = None

        for pattern_name, pattern in PATTERNS.items():
            if pattern_name == "short_standalone":
                # Only match short lines that start with uppercase or are
                # clearly non-continuation (not starting with lowercase word)
                if pattern.match(stripped) and stripped and not stripped[0].islower():
                    break_type = pattern_name
                    break
            elif pattern.match(stripped):
                break_type = pattern_name
                break

        # Additional heuristic: line following a blank line starts a new para
        is_break = break_type is not None

        results.append({
            "line_num": i + 1,
            "text": line,
            "break_type": break_type,
            "is_break": is_break,
        })

    return results


def summarize_breaks(results: list[dict]) -> dict:
    """Summarize paragraph break counts by type."""
    from collections import Counter
    counts = Counter(r["break_type"] for r in results if r["is_break"])
    return dict(counts)


def print_report(results: list[dict], show_context: bool = False):
    """Print a readable report of detected paragraph breaks."""
    summary = summarize_breaks(results)

    print("=" * 60)
    print("PARAGRAPH BREAK DETECTION REPORT")
    print("=" * 60)
    print(f"\nTotal lines: {len(results)}")
    print(f"Paragraph breaks detected: {sum(1 for r in results if r['is_break'])}")
    print("\nBreaks by type:")
    for btype, count in sorted(summary.items(), key=lambda x: -x[1]):
        print(f"  {btype:<25} {count:>4}")

    if show_context:
        print("\n" + "-" * 60)
        print("BREAK LOCATIONS (with context):")
        print("-" * 60)
        for r in results:
            if r["is_break"] and r["break_type"] != "blank_line":
                print(f"\n  Line {r['line_num']:>4} [{r['break_type']}]:")
                print(f"    {r['text'][:80]}")


def insert_break_markers(text: str, marker: str = "\n") -> str:
    """
    Return text with explicit paragraph break markers inserted
    before each detected break point (excluding already-blank lines).
    """
    results = detect_paragraph_breaks(text)
    output_lines = []

    for i, r in enumerate(results):
        # Insert a blank line before break points if previous line isn't blank
        if (r["is_break"]
                and r["break_type"] != "blank_line"
                and i > 0
                and results[i - 1]["text"].strip() != ""):
            output_lines.append(marker)
        output_lines.append(r["text"])

    return "\n".join(output_lines)


def _find_table_regions(results: list[dict]) -> set[int]:
    """
    Pre-pass: identify line indices that belong to table regions.
    
    A table region is a contiguous run of lines (ignoring blanks) where
    the content lines have 4+ internal spaces (text…spaces…text), indicating
    columnar layout.  Short header lines (e.g. "Number", "of members") that
    appear immediately before such runs are included in the region.
    
    Page separators and page headers are never considered table lines.
    """
    TABLE_GAP = re.compile(r"\S.{1,}\s{4,}\S")  # text + 4+ spaces + text
    
    n = len(results)
    # Step 1: mark each content line as "tabular" or not
    is_tabular = [False] * n
    for i, r in enumerate(results):
        s = r["text"].strip()
        if not s:
            continue
        if r["break_type"] in ("page_separator", "page_header"):
            continue
        if TABLE_GAP.search(s):
            is_tabular[i] = True
    
    # Step 2: find contiguous runs of tabular lines (spans of ≥2, bridging blanks
    # and up to 2 non-tabular content lines that look like table items)
    table_set: set[int] = set()
    i = 0
    while i < n:
        if is_tabular[i]:
            # Start of a potential table run
            run_start = i
            run_end = i
            tabular_count = 1
            j = i + 1
            while j < n:
                s = results[j]["text"].strip()
                if not s:
                    # blank line — skip and keep looking
                    j += 1
                    continue
                if results[j]["break_type"] in ("page_separator", "page_header",
                                                  "alpha_subsection", "rule_heading",
                                                  "tagged_header"):
                    break  # structural break — end of table
                if is_tabular[j]:
                    run_end = j
                    tabular_count += 1
                    j += 1
                else:
                    # Non-tabular content line — bridge if a tabular line follows soon
                    lookahead = j + 1
                    gap = 1
                    found_next = False
                    while lookahead < n and gap <= 3:
                        ls = results[lookahead]["text"].strip()
                        if not ls:
                            lookahead += 1
                            continue
                        if is_tabular[lookahead]:
                            found_next = True
                            break
                        gap += 1
                        lookahead += 1
                    if found_next:
                        # Bridge: include this line and continue
                        run_end = j
                        j += 1
                    else:
                        break
            
            if tabular_count >= 2:
                # Mark all lines in this run (including bridging blanks)
                for k in range(run_start, run_end + 1):
                    table_set.add(k)
                
                # Also pull in short header lines immediately before the run
                # (e.g. "Number", "of members" above a committee table)
                k = run_start - 1
                while k >= 0:
                    s = results[k]["text"].strip()
                    if not s:
                        # blank line — include it and keep looking
                        table_set.add(k)
                        k -= 1
                        continue
                    if len(s) <= 30 and results[k]["break_type"] not in (
                        "page_separator", "page_header", "alpha_subsection",
                        "numeric_subsection", "rule_heading", "tagged_header",
                    ):
                        table_set.add(k)
                        k -= 1
                    else:
                        break
            
            i = j
        else:
            i += 1
    
    return table_set


def _format_table_block(lines: list[str]) -> list[str]:
    """
    Format table lines so the trailing number column is right-aligned,
    making two distinct visual columns.
    
    Detects lines of the form "text      number" and pads the text so
    the number is right-aligned at a consistent column position.
    Short header lines (e.g. "Number", "of members") are right-aligned
    to the same column.
    """
    import re
    # Pattern: text content followed by spaces and a trailing number
    ROW_RE = re.compile(r"^(.+?)\s{2,}(\d+)\s*$")
    
    # First pass: identify data rows and find the target column width
    parsed: list[tuple[str, str] | None] = []
    max_text_len = 0
    for line in lines:
        s = line.strip()
        m = ROW_RE.match(s)
        if m:
            text_part, num_part = m.group(1), m.group(2)
            parsed.append((text_part, num_part))
            max_text_len = max(max_text_len, len(text_part))
        else:
            parsed.append(None)
    
    if max_text_len == 0:
        return lines  # no data rows found, return as-is
    
    # Target width: longest text + padding for the number column
    # Align numbers to a column position after the longest text
    num_col = max_text_len + 2  # at least 2 spaces after longest text
    
    # Second pass: format each line
    formatted: list[str] = []
    for i, line in enumerate(lines):
        if parsed[i] is not None:
            text_part, num_part = parsed[i]
            # Pad text to align the number at num_col
            padded = text_part + " " * (num_col - len(text_part)) + num_part
            formatted.append(padded)
        else:
            # Header lines like "Number", "of members" — right-align to num_col
            s = line.strip()
            if s and len(s) <= 30:
                # Right-align to the number column area
                formatted.append(" " * (num_col - len(s)) + s)
            else:
                formatted.append(line)
    
    return formatted


def unwrap_paragraphs(text: str) -> str:
    """
    Join multi-line paragraphs into single lines.
    
    Uses detected paragraph breaks to identify where paragraphs start,
    then joins all continuation lines (lines that are NOT a break point
    and not blank) with the preceding line using a space.
    
    Table regions (contiguous columnar content with 4+ internal spaces)
    are wrapped in ---TABLE START--- / ---TABLE END--- markers and
    preserved as-is (no unwrapping).
    """
    results = detect_paragraph_breaks(text)
    table_lines = _find_table_regions(results)
    output_lines = []
    
    prev_was_blank = False
    in_table = False
    table_buffer: list[str] = []  # collect table lines for formatting
    
    for i, r in enumerate(results):
        line = r["text"]
        stripped = line.strip()
        is_in_table = i in table_lines
        
        # --- Table region handling ---
        if is_in_table:
            if not in_table:
                # Entering table region
                in_table = True
                table_buffer = []
                if output_lines:
                    output_lines.append("")
            if stripped:  # skip blank lines inside table
                table_buffer.append(line)
            prev_was_blank = False
            continue
        
        if in_table and not is_in_table:
            # Leaving table region — format and emit
            formatted = _format_table_block(table_buffer)
            output_lines.append("---TABLE START---")
            output_lines.extend(formatted)
            output_lines.append("---TABLE END---")
            output_lines.append("")
            in_table = False
            table_buffer = []
        
        # --- Normal paragraph handling ---
        if not stripped:
            prev_was_blank = True
            continue
        
        if r["is_break"] or prev_was_blank:
            if output_lines:
                output_lines.append("")
            output_lines.append(line)
            prev_was_blank = False
            continue
        
        prev_was_blank = False
        
        # Continuation line — join to previous
        # But if the previous line ended with a sentence-ending period
        # and this line starts with a capital letter, it's a new paragraph
        if output_lines and output_lines[-1].strip():
            prev_text = output_lines[-1].rstrip()
            if (prev_text.endswith('.')
                    and not prev_text.endswith('etc.')
                    and not prev_text.endswith('seq.')
                    and stripped[0:1].isupper()):
                # New paragraph — previous sentence is complete
                output_lines.append("")
                output_lines.append(line)
            else:
                output_lines[-1] = prev_text + " " + stripped
        else:
            output_lines.append(line)
    
    # Close any open table region
    if in_table:
        formatted = _format_table_block(table_buffer)
        output_lines.append("---TABLE START---")
        output_lines.extend(formatted)
        output_lines.append("---TABLE END---")
    
    return "\n".join(output_lines)


def mark_tables(text: str) -> str:
    """
    Insert ---TABLE START--- / ---TABLE END--- markers around detected table
    regions and format columns for readability, WITHOUT unwrapping paragraphs.
    
    All non-table lines are preserved exactly as-is.
    """
    results = detect_paragraph_breaks(text)
    table_line_set = _find_table_regions(results)
    
    if not table_line_set:
        return text  # no tables found
    
    # Identify contiguous table regions
    sorted_indices = sorted(table_line_set)
    regions: list[tuple[int, int]] = []  # (start, end) inclusive
    region_start = sorted_indices[0]
    prev = sorted_indices[0]
    for idx in sorted_indices[1:]:
        if idx > prev + 1:
            regions.append((region_start, prev))
            region_start = idx
        prev = idx
    regions.append((region_start, prev))
    
    # Build output: copy lines, inserting markers and formatting table content
    output_lines: list[str] = []
    table_region_idx = 0
    i = 0
    
    while i < len(results):
        if table_region_idx < len(regions) and i == regions[table_region_idx][0]:
            start, end = regions[table_region_idx]
            # Collect non-blank table lines for formatting
            table_content = []
            for j in range(start, end + 1):
                if results[j]["text"].strip():
                    table_content.append(results[j]["text"])
            
            formatted = _format_table_block(table_content)
            output_lines.append("---TABLE START---")
            output_lines.extend(formatted)
            output_lines.append("---TABLE END---")
            
            i = end + 1
            table_region_idx += 1
        else:
            output_lines.append(results[i]["text"])
            i += 1
    
    return "\n".join(output_lines)


def main():
    if len(sys.argv) < 2:
        print("Usage: python paragraph_break_detector.py <file_path> [--context] [--mark] [--unwrap] [--tables]")
        print("\nOptions:")
        print("  --context   Show break locations with surrounding text")
        print("  --mark      Output file with break markers inserted")
        print("  --unwrap    Join multi-line paragraphs into single lines")
        print("  --tables    Insert TABLE markers and format columns (no unwrap)")
        sys.exit(1)

    file_path = Path(sys.argv[1])
    show_context = "--context" in sys.argv
    mark_output = "--mark" in sys.argv
    unwrap = "--unwrap" in sys.argv
    tables_only = "--tables" in sys.argv

    if not file_path.exists():
        print(f"Error: File not found: {file_path}")
        sys.exit(1)

    text = file_path.read_text(encoding="utf-8")

    if tables_only:
        result = mark_tables(text)
        out_path = file_path.with_stem(file_path.stem + "_tables")
        out_path.write_text(result, encoding="utf-8")
        print(f"Table-marked output written to: {out_path}")
    elif unwrap:
        unwrapped = unwrap_paragraphs(text)
        out_path = file_path.with_stem(file_path.stem + "_unwrapped")
        out_path.write_text(unwrapped, encoding="utf-8")
        print(f"Unwrapped output written to: {out_path}")
        # Stats
        orig_lines = len(text.split("\n"))
        new_lines = len(unwrapped.split("\n"))
        print(f"Original lines: {orig_lines}")
        print(f"Unwrapped lines: {new_lines}")
        print(f"Lines reduced by: {orig_lines - new_lines}")
    elif mark_output:
        marked = insert_break_markers(text)
        out_path = file_path.with_stem(file_path.stem + "_breaks")
        out_path.write_text(marked, encoding="utf-8")
        print(f"Marked output written to: {out_path}")
    else:
        results = detect_paragraph_breaks(text)
        print_report(results, show_context=show_context)


if __name__ == "__main__":
    main()
