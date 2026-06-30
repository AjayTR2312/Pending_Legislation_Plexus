# Delaware HB 230 (Grants-in-Aid, FY 2026)

**Bill ID:** 2025_2026_8_2_2_000230_0_8_1_20250701_0
**Bug ticket:** internal (June 29, 2026)
**Fix commit:** `_sort_block_lines_visually()` in `app/pymupdf_extractor.py`

## Bug

PyMuPDF returned tabular currency cells in PDF *drawing* order rather
than visual reading order.  In this PDF, the dollar number is drawn
first and the "$" sigil is then placed visually to the LEFT of the
number:

| span | x range | text |
|------|---------|------|
| 1 | 528.9 – 567.3 | `3,880,543` |
| 2 | 506.0 – 530.0 | `$` |

Without reordering, the extractor emitted `3,880,543 $` (currency symbol
trailing the amount), which is wrong for US legal text.

## Fix

[`_sort_block_lines_visually`](../../../app/pymupdf_extractor.py) groups
consecutive PyMuPDF "lines" whose y-midpoints fall within
`SAME_ROW_Y_TOL_PT` of each other and sorts each group by leftmost x
before the main text-output loop iterates them.

## What `expected_excerpts.txt` checks

Each line of [expected_excerpts.txt](expected_excerpts.txt) is a stable
substring from the cleaned output that *must* still be present after any
future change.  All excerpts are from page 1 of the PDF, which is the
canonical regression surface for this bug.
