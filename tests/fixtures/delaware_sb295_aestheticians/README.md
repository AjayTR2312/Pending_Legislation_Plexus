# Delaware SB 295 (Aestheticians scope-of-practice update)

**Bill ID:** 2025_2026_8_3_2_000295_52_4_1_20260430_0
**Bug ticket:** internal (June 29, 2026)
**Fix commit:** added "underline signature" guard to `_detect_table_zones()` in `app/pymupdf_extractor.py`

## Bug

The user noticed that on page 7 of this bill, three numbered items plus
a continuation line (`1. Diagnose illness…`, `2. Perform ablation…`,
`3. Prescribe medication…`, `dermatologist.`) were visually underlined
in the source PDF but came out of the extractor with NO `[A>…<A]` tags.

Root cause was a chain of two interactions:

1. The PDF emits its underlines as **stroked horizontal line segments**
   (PDF path item kind `"l"`) rather than the more common **filled
   rectangles** (path item kind `"re"`).
2. PyMuPDF's `Page.find_tables()` saw four roughly-parallel horizontal
   line segments and inferred a *table* zone covering the upper half
   of the page (`bbox=(77.8, 57.6, 577.5, 218.6)`).
3. The `_detect_table_zones()` validator was supposed to reject false
   positives by requiring a *stroked-line* border, but accepted the
   widest underline (310.7 pt wide, > 40% of the zone width) as
   "evidence" because it *was* a stroked line.
4. Once the zone was accepted, the `line_in_table` guard in
   `_extract_page_tagged_text()` suppressed all decoration tagging for
   lines inside it — silently stripping the legislative additions.

The previous comment in `_detect_table_zones()` already noted:
> Filled rectangles (`"re"` items, the usual rendering for underlines
> and strike-throughs) do NOT count as structural evidence …

…but this PDF demonstrates the same problem for stroked-line underlines.

## Fix

`_detect_table_zones()` now collects every text-line bbox up front and,
before counting a stroked horizontal line as table-border evidence,
checks whether it sits within `UNDERLINE_TOLERANCE_PT` below a text
baseline AND horizontally overlaps that text's x-range by ≥ 80 %.  Lines
matching that signature are excluded from the evidence pool because they
are decoration, not structure.

With the fix, page 7 detects zero table zones (correct — page 7 has no
tables), all four underline hlines reach the per-character classifier,
and the missing `[A>…<A]` tags appear in the output.
