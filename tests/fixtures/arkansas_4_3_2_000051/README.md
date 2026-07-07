# Arkansas SB 8 (Act 154, 2026 Fiscal Session) — extraction fixture

**Source bill id:** `2026_2026_4_3_2_000051_0_8_1_20260501_0`
**Filename in production:** `2026_2026_4_3_2_000051_0_8_1_20260501_0.pdf`
**State code:** `AR`
**Date added:** 2026 (from a user-reported bug)

## Why this fixture exists

The bill is a 6-page appropriation act for the Arkansas Auditor of State.
Page 2 contains a salary classification table where each row reads, e.g.:

```
(16)     ADMINISTRATIVE SPECIALIST       2     $48,575
```

The "2" sits in the *Maximum No. of Employees* column.  The PDF draws a
thin horizontal rule at the row baseline that runs across the whitespace
between the job title and the employee count, ending a few points inside
the digit's bbox.

Before the fix, [`_classify_char_decoration`](../../../app/pymupdf_extractor.py)
saw a horizontal line overlapping ~50 % of the "2" character, well above
the `CHAR_OVERLAP_RATIO = 0.30` threshold, and tagged it as a legislative
addition:

```
(16)     ADMINISTRATIVE SPECIALIST      [A>2<A]     $48,575
```

The same leader-line pattern produced eight other false-positive tags
across pages 1–4 (column headers, lone digits in employee counts).

## The fix

[`_filter_ar_leader_lines`](../../../app/pymupdf_extractor.py) (Arkansas-only,
gated by `state_code == "AR"` at all three hline-gathering call sites)
removes horizontal lines whose extent is covered by < 50 % of text on
their baseline.

In the per-page hline survey of this bill:

| Population            | Coverage range |
|-----------------------|----------------|
| Real underlines       | 69 – 100 %     |
| Leader / decorative   | 0 – 35 %       |

A 50 % threshold sits in the middle of the 34-point gap between the
populations — robust to minor geometry drift.

After the fix: 31 → 22 `[A>…<A]` tags on this bill, with the 9 dropped
tags being exactly the false positives the user flagged.

## Assertions

The fixture uses both *required* and *forbidden* excerpts (the new `!`
prefix in `expected_excerpts.txt`):

* **Forbidden:** `[A>2<A]`, `[A> 3<A]`, `[A>            0<A]`, and two
  column-header leader rules — every false tag that pre-fix extraction
  emitted.
* **Required:** the ADMINISTRATIVE SPECIALIST row in its corrected,
  un-tagged form, plus four genuine dollar-amount additions, two year
  labels, and the SECTION 13 emergency clause endpoints — every
  legitimate addition that the filter must preserve.

This guarantees that any future regression which either re-introduces
the false tag OR strips a real underline will fail the snapshot test.

## Maintenance

If the Arkansas legislature changes the underlying PDF authoring tool
(e.g. switches typesetter), the leader-line geometry may shift.  In that
case:

1. Re-run the per-page hline survey (see the inline analysis in commit
   message for the original AR fix) and confirm the coverage populations
   are still cleanly separated.
2. Adjust `AR_LEADER_LINE_COVERAGE_MAX` if needed (default 0.50).
3. Add new forbidden/required excerpts here for any new false-positive
   or missed-positive patterns.
