# Test fixtures

Each subdirectory under `fixtures/` represents one real-world PDF that
once produced an extraction bug.  The pair of files is:

```
fixtures/<bill_id>/
├── input.pdf              # the actual source PDF (kept in repo, NOT git-lfs)
├── expected_excerpts.txt  # one or more substrings that MUST appear in
│                          # the extractor's cleaned output
└── README.md              # one-paragraph history of the bug
```

`tests/test_extraction_snapshots.py` walks every subdirectory, runs the
PyMuPDF extractor + the deterministic post-processors (NO LLM call), and
asserts every excerpt from `expected_excerpts.txt` appears verbatim
somewhere in the output.

## Adding a new fixture

1. Create `fixtures/<descriptive_name>/`.
2. Drop the offending PDF in as `input.pdf` (keep it small — strip
   pages you don't need with `qpdf --pages input.pdf 1-3 -- trimmed.pdf`
   if the file is large).
3. Run the extractor on the PDF manually, copy a stable 10–80 char
   substring that covers the previously buggy region, and paste it
   into `expected_excerpts.txt`.  One excerpt per line; blank lines and
   `#` comments are ignored.
4. Add a `README.md` that links to the bug ticket / commit and shows the
   before/after extraction.
5. Run `pytest tests/` to confirm the new test passes.

## Excerpt format

```
# Comments and blank lines are ignored.
# Each non-comment, non-blank line is an exact substring that must
# appear in the extracted output AT LEAST ONCE.

County Seat Package $         3,880,543
TOTAL - Office of the Secretary $         3,880,543
```

Tabs and multiple spaces are preserved verbatim — the extractor's
output is checked with plain `in`, no regex / whitespace normalisation.
This keeps the test deterministic and forces us to fix the extractor
rather than loosen the assertion when whitespace shifts.
