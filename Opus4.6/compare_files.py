"""
Compare pymupdf_clean.txt and converted.txt files.
Strips all tags ([D>...<D], [A>...<A]) from both files,
compares words, and shows full sentences for any missing content.

Provides both a CLI ``compare_two_files()`` for terminal use and a
programmatic ``compare_texts()`` that returns a structured dict for
integration into the Streamlit pipeline.

Usage (CLI):
    python compare_files.py <pymupdf_clean.txt> <converted.txt>

Usage (API):
    from compare_files import compare_texts
    report = compare_texts(pymupdf_clean_str, converted_str)
    # report = {
    #     "accuracy": 99.63,
    #     "pymupdf_word_count": 2450,
    #     "converted_word_count": 2447,
    #     "matching_words": 2441,
    #     "status": "DIFFERENT",      # IDENTICAL | CONTENT_MATCH | DIFFERENT
    #     "real_diffs": [ ... ],
    #     "repositioned": [ ... ],
    # }
"""

import re
import os
import sys
import codecs
import difflib

# Optional import: text_accuracy_helpers lives in /app, so we add that to
# sys.path when running as a standalone script (CLI mode).  If the import
# fails we still work — just without the "allowed-removal" allowlist.
try:
    from text_accuracy_helpers import (  # type: ignore
        classify_removed_text,
        strip_trailing_line_numbers,
        merge_wrapped_artifact_lines,
        strip_table_row_markers,
        normalise_ct_title_block,
        strip_ca_inline_line_numbers,
        strip_ca_page_headers,
        merge_ca_caption_wrap,
        strip_in_page_headers,
    )
except ImportError:  # pragma: no cover
    _app_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
    if os.path.isdir(_app_dir) and _app_dir not in sys.path:
        sys.path.insert(0, _app_dir)
    try:
        from text_accuracy_helpers import (  # type: ignore
            classify_removed_text,
            strip_trailing_line_numbers,
            merge_wrapped_artifact_lines,
            strip_table_row_markers,
            normalise_ct_title_block,
            strip_ca_inline_line_numbers,
            strip_ca_page_headers,
            merge_ca_caption_wrap,
            strip_in_page_headers,
        )
    except ImportError:
        classify_removed_text = None  # type: ignore
        strip_trailing_line_numbers = None  # type: ignore
        merge_wrapped_artifact_lines = None  # type: ignore
        strip_table_row_markers = None  # type: ignore
        normalise_ct_title_block = None  # type: ignore
        strip_ca_inline_line_numbers = None  # type: ignore
        strip_ca_page_headers = None  # type: ignore
        merge_ca_caption_wrap = None  # type: ignore
        strip_in_page_headers = None  # type: ignore

# Fix encoding for Windows console/redirect
if sys.platform == 'win32':
    try:
        sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'replace')
        sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'replace')
    except Exception:
        pass


def strip_tags(text):
    """Remove all [D>...<D] and [A>...<A] tags from text, keeping inner content."""
    text = re.sub(r'\[D>', '', text)
    text = re.sub(r'<D\]', '', text)
    text = re.sub(r'\[A>', '', text)
    text = re.sub(r'<A\]', '', text)
    return text


# Unicode → ASCII punctuation map.  The LLM cleaning step routinely
# normalises curly quotes, en/em dashes, ellipsis and non-breaking spaces
# to their ASCII equivalents.  If we leave the raw PyMuPDF Unicode forms
# in place during word comparison, every ``analyst’s`` vs ``analyst's``
# pair shows up as a REPLACE diff even though the underlying word is
# identical.  Normalising both sides before tokenisation eliminates that
# whole class of false positives.
_UNICODE_PUNCT_NORMALISATION = str.maketrans({
    '\u2018': "'",    # ‘  left single quotation mark
    '\u2019': "'",    # ’  right single quotation mark (curly apostrophe)
    '\u201A': "'",    # ‚  single low-9 quotation mark
    '\u201B': "'",    # ‛  single high-reversed-9 quotation mark
    '\u201C': '"',    # “  left double quotation mark
    '\u201D': '"',    # ”  right double quotation mark
    '\u201E': '"',    # „  double low-9 quotation mark
    '\u2013': '-',    # –  en-dash
    '\u2014': '-',    # —  em-dash
    '\u2212': '-',    # −  minus sign
    '\u00A0': ' ',    #    non-breaking space
    '\u2009': ' ',    #    thin space
    '\u200A': ' ',    #    hair space
    '\u202F': ' ',    #    narrow no-break space
    '\u2026': '...',  # …  horizontal ellipsis
})


def normalise_unicode_punct(text):
    """Map curly quotes / em-dash / ellipsis / NBSP to ASCII equivalents."""
    if not text:
        return text
    return text.translate(_UNICODE_PUNCT_NORMALISATION)


def extract_words(text):
    """Extract list of words from text after stripping tags and normalizing whitespace."""
    cleaned = strip_tags(text)
    cleaned = normalise_unicode_punct(cleaned)
    normalized = re.sub(r'\s+', ' ', cleaned).strip()
    return normalized.split()


def get_clean_text(text):
    """Get tag-stripped, whitespace-normalized text."""
    cleaned = strip_tags(text)
    cleaned = normalise_unicode_punct(cleaned)
    return re.sub(r'\s+', ' ', cleaned).strip()


def find_sentence(clean_text, word_index, words):
    """Find the full sentence (full stop to full stop) containing the word at word_index.
    Returns the sentence string.
    """
    target = words[word_index]

    # Walk backwards from word_index to find sentence start (after a period)
    start_idx = word_index
    while start_idx > 0:
        prev_word = words[start_idx - 1]
        if prev_word.endswith('.') or prev_word.endswith('.)'):
            break
        start_idx -= 1

    # Walk forwards from word_index to find sentence end (at a period)
    end_idx = word_index
    while end_idx < len(words) - 1:
        if words[end_idx].endswith('.') or words[end_idx].endswith('.)'):
            break
        end_idx += 1

    sentence_words = words[start_idx:end_idx + 1]
    return ' '.join(sentence_words)


def phrase_exists_in(phrase_words, all_words):
    """Check if a phrase (list of words) exists as a contiguous subsequence."""
    if not phrase_words:
        return False
    phrase_len = len(phrase_words)
    for i in range(len(all_words) - phrase_len + 1):
        if all_words[i:i + phrase_len] == phrase_words:
            return True
    return False

# ─── Underscore / separator helper ──────────────────────────────────
def _is_underscore_fill(words: list[str]) -> bool:
    """Return True if every word consists solely of underscores.

    Catches separator lines like '___________________________________'
    that may differ in length between files.
    """
    return bool(words) and all(w.strip() and set(w) <= {'_'} for w in words)

# ─── Punctuation / transposition helpers ────────────────────────────
_PUNCT_CHARS = set('.,;:!?\'\"()[]{}\u2014\u2013-\u2026/\\@#$%^&*+=<>~`')


def _strip_punct(word: str) -> str:
    """Strip leading/trailing punctuation from a word."""
    return word.strip(''.join(_PUNCT_CHARS))


def _classify_replace(pymupdf_words: list[str], converted_words: list[str]) -> dict | None:
    """Sub-classify a REPLACE opcode as TRANSPOSED or PUNCTUATION.

    Returns a dict with extra keys to merge into the diff entry, or None.
    """
    # ── 1. TRANSPOSED: same bag-of-words, different order ─────────
    if sorted(pymupdf_words) == sorted(converted_words) and pymupdf_words != converted_words:
        moved = []
        for idx, (pw, cw) in enumerate(zip(pymupdf_words, converted_words)):
            if pw != cw:
                moved.append({"position": idx, "expected": pw, "got": cw})
        for idx in range(min(len(pymupdf_words), len(converted_words)), max(len(pymupdf_words), len(converted_words))):
            exp = pymupdf_words[idx] if idx < len(pymupdf_words) else "\u2014"
            got = converted_words[idx] if idx < len(converted_words) else "\u2014"
            moved.append({"position": idx, "expected": exp, "got": got})
        return {"sub_type": "TRANSPOSED", "moved_words": moved}

    # ── 1b. LINE_NUMBER_HYPHENATION: line numbers embedded in hyphenated words
    # Pattern: PyMuPDF has ["word-", "29", "companion"] → clean has ["word-companion"]
    # or ["20-", "3", "670"] → clean has ["20-670"]
    # A bare number immediately following a hyphen-ending token is a line number.
    _RE_BARE_NUM = re.compile(r"^\d{1,4}$")
    p_filtered = []
    skip_next_num = False
    for i, w in enumerate(pymupdf_words):
        # If previous token ends with '-' and current is bare number, skip ONE number
        if skip_next_num and _RE_BARE_NUM.match(w):
            skip_next_num = False
            continue
        skip_next_num = w.endswith("-")
        p_filtered.append(w)
    if p_filtered != pymupdf_words:  # at least one number was removed
        # Join tokens: if a token ends with '-', concatenate directly with next
        joined = ""
        for w in p_filtered:
            if joined.endswith("-"):
                joined += w
            else:
                joined += (" " if joined else "") + w
        c_joined = " ".join(converted_words)
        if joined == c_joined:
            return {"sub_type": "LINE_NUMBER_HYPHENATION"}

    # ── 1c. LINE_NUMBER_EDGE: bare line numbers at start/end of phrase
    # Pattern: PyMuPDF has ["19", "days", ..., "than", "20"] → clean has ["days", ..., "than"]
    # Strip leading and trailing bare numbers (1-4 digits) from PyMuPDF side.
    p_trimmed = list(pymupdf_words)
    while p_trimmed and _RE_BARE_NUM.match(p_trimmed[0]):
        p_trimmed.pop(0)
    while p_trimmed and _RE_BARE_NUM.match(p_trimmed[-1]):
        p_trimmed.pop()
    if p_trimmed != pymupdf_words and p_trimmed == converted_words:
        return {"sub_type": "LINE_NUMBER_HYPHENATION"}
    # Also check if trimmed words match after joining (spacing difference)
    if p_trimmed != pymupdf_words and p_trimmed and ''.join(p_trimmed) == ''.join(converted_words):
        return {"sub_type": "LINE_NUMBER_HYPHENATION"}

    # ── 1d. FILL_UNDERSCORE: word + trailing fill underscores (± line number)
    # Pattern: PyMuPDF has ["the______________________________", "2"] → clean has ["the"]
    # The underscores are decorative fill lines in the PDF, not content.
    _RE_FILL = re.compile(r"^(.+?)_{3,}$")
    p_stripped_fill = []
    for w in pymupdf_words:
        m = _RE_FILL.match(w)
        if m:
            p_stripped_fill.append(m.group(1))
        elif _RE_BARE_NUM.match(w):
            continue  # skip trailing line numbers
        else:
            p_stripped_fill.append(w)
    if p_stripped_fill == list(converted_words):
        return {"sub_type": "LINE_NUMBER_HYPHENATION"}

    # ── 2. SPACING-only: joined text is identical (e.g. "Therelease" vs "The release")
    if ''.join(pymupdf_words) == ''.join(converted_words):
        return {"sub_type": "SPACING"}

    # ── 3. PUNCTUATION-only: words identical ignoring punctuation ──
    p_stripped = [_strip_punct(w) for w in pymupdf_words]
    c_stripped = [_strip_punct(w) for w in converted_words]
    if p_stripped == c_stripped:
        char_diffs = _char_level_punct_diff(' '.join(pymupdf_words), ' '.join(converted_words))
        return {"sub_type": "PUNCTUATION", "char_diffs": char_diffs}

    # ── 3. Same-length: check if *every* word-level change is just punctuation
    if len(pymupdf_words) == len(converted_words):
        punct_changes = []
        word_changes = 0
        for pw, cw in zip(pymupdf_words, converted_words):
            if pw == cw:
                continue
            if _strip_punct(pw) == _strip_punct(cw):
                punct_changes.append({"expected": pw, "got": cw})
            else:
                word_changes += 1
        if word_changes == 0 and punct_changes:
            char_diffs = _char_level_punct_diff(' '.join(pymupdf_words), ' '.join(converted_words))
            return {"sub_type": "PUNCTUATION", "char_diffs": char_diffs}

    return None


def _char_level_punct_diff(text_a: str, text_b: str) -> list[dict]:
    """Return individual character-level punctuation differences.

    Each entry:
        {"kind": "MISSING" | "EXTRA" | "CHANGED",
         "char": the punctuation character(s),
         "position": approx index in text_a,
         "context": short surrounding text}
    """
    sm = difflib.SequenceMatcher(None, text_a, text_b, autojunk=False)
    diffs = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            continue
        a_seg = text_a[i1:i2]
        b_seg = text_b[j1:j2]
        ctx_s = max(0, i1 - 15)
        ctx_e = min(len(text_a), i2 + 15)
        context = text_a[ctx_s:ctx_e]

        if tag == 'delete':
            for ch in a_seg:
                if ch in _PUNCT_CHARS or ch.isspace():
                    diffs.append({"kind": "MISSING", "char": repr(ch), "position": i1, "context": context})
        elif tag == 'insert':
            ctx_sb = max(0, j1 - 15)
            ctx_eb = min(len(text_b), j2 + 15)
            context_b = text_b[ctx_sb:ctx_eb]
            for ch in b_seg:
                if ch in _PUNCT_CHARS or ch.isspace():
                    diffs.append({"kind": "EXTRA", "char": repr(ch), "position": j1, "context": context_b})
        elif tag == 'replace':
            for ch in a_seg:
                if ch in _PUNCT_CHARS:
                    diffs.append({"kind": "CHANGED", "char": f"{repr(ch)} \u2192 {repr(b_seg)}",
                                  "position": i1, "context": context})
                    break  # one entry per replace block
    return diffs


def _merge_transposed_pairs(diffs: list[dict],
                             pymupdf_words: list[str],
                             converted_words: list[str]) -> list[dict]:
    """Merge adjacent MISSING + EXTRA diffs whose words are a transposition.

    SequenceMatcher may split "board of directors" → "directors of board" into
    separate DELETE and INSERT opcodes.  This pass detects when a
    MISSING_IN_CONVERTED and a nearby EXTRA_IN_CONVERTED together form the same
    bag-of-words (exact or after stripping punctuation), and merges them into a
    single TRANSPOSED diff.
    """
    if len(diffs) < 2:
        return diffs

    merged = []
    used = set()

    def _bags_match(words_a, words_b):
        """Return True if same words ignoring order; try exact first, then punct-stripped."""
        if sorted(words_a) == sorted(words_b):
            return True
        sa = sorted(_strip_punct(w) for w in words_a)
        sb = sorted(_strip_punct(w) for w in words_b)
        return sa == sb and sa  # non-empty

    for i, d in enumerate(diffs):
        if i in used:
            continue

        if d["type"] in ("MISSING_IN_CONVERTED", "EXTRA_IN_CONVERTED"):
            is_miss = d["type"] == "MISSING_IN_CONVERTED"
            my_words = (d["missing_text"] if is_miss else d["extra_text"]).split()
            partner_type = "EXTRA_IN_CONVERTED" if is_miss else "MISSING_IN_CONVERTED"

            for j in range(i + 1, min(i + 6, len(diffs))):
                if j in used:
                    continue
                d2 = diffs[j]
                if d2["type"] != partner_type:
                    continue

                other_words = (d2["extra_text"] if is_miss else d2["missing_text"]).split()
                miss_words = my_words if is_miss else other_words
                extra_words = other_words if is_miss else my_words
                miss_diff = d if is_miss else d2
                extra_diff = d2 if is_miss else d

                if _bags_match(miss_words, extra_words) and miss_words != extra_words:
                    moved = []
                    for idx2, (pw, cw) in enumerate(zip(miss_words, extra_words)):
                        if pw != cw:
                            moved.append({"position": idx2, "expected": pw, "got": cw})
                    for idx2 in range(min(len(miss_words), len(extra_words)),
                                      max(len(miss_words), len(extra_words))):
                        exp = miss_words[idx2] if idx2 < len(miss_words) else "—"
                        got = extra_words[idx2] if idx2 < len(extra_words) else "—"
                        moved.append({"position": idx2, "expected": exp, "got": got})

                    merged.append({
                        "type": "REPLACE",
                        "sub_type": "TRANSPOSED",
                        "pymupdf_text": ' '.join(miss_words),
                        "converted_text": ' '.join(extra_words),
                        "sentence_pymupdf": miss_diff.get("sentence", ""),
                        "sentence_converted": extra_diff.get("sentence", ""),
                        "moved_words": moved,
                    })
                    used.add(i)
                    used.add(j)
                    break

            if i not in used:
                merged.append(d)
        else:
            merged.append(d)

    return merged


# ─────────────────────────────────────────────────────────────────────
# Programmatic API — returns structured dict
# ─────────────────────────────────────────────────────────────────────
def compare_texts(
    pymupdf_clean_text: str,
    converted_text: str,
    state_code: str | None = None,
    llm_removed_lines: list[str] | None = None,
) -> dict:
    """Compare two text strings and return a structured accuracy report.

    Parameters
    ----------
    pymupdf_clean_text : str
        Tag-stripped PyMuPDF clean text (ground truth).
    converted_text : str
        Claude Opus converted text (validation target).
    state_code : str, optional
        Two-letter state code.  When provided, any MISSING / EXTRA diff is
        passed through ``classify_removed_text`` and — if it matches a known
        artifact category (line numbers, headers, footers, page markers,
        etc.) — is moved to the ``allowed_diffs`` bucket and counted as a
        matching word rather than a real-content drop.  Eliminates false
        positives caused by one side stripping an artifact the other kept.
    llm_removed_lines : list[str], optional
        Lines the LLM intentionally removed during cleaning.  Any
        MISSING_IN_CONVERTED diff whose text is fully contained within these
        removed lines is classified as an allowed removal (category
        "llm_intentional").  This provides a universal fallback for
        state-specific artifacts not covered by regex patterns.

    Returns
    -------
    dict with keys:
        accuracy              – float, 0–100  (excludes allowed removals)
        raw_accuracy          – float, 0–100  (everything, no allowlist)
        pymupdf_word_count    – int
        converted_word_count  – int
        matching_words        – int   (includes allowed-removal words)
        status                – "IDENTICAL" | "CONTENT_MATCH" | "DIFFERENT"
        real_diffs            – list of diff dicts (after allowlist filter)
        allowed_diffs         – list of diff dicts (intentional cleanup)
        repositioned          – list of repositioned dicts
        real_diff_count       – int
        allowed_diff_count    – int
        repositioned_count    – int
        allowed_categories    – dict[str, int]   {"line_number": 12, ...}
    """
    # ── Pre-pre-filter: strip right-aligned trailing line numbers ───────
    # Some states (notably Connecticut) print line numbers in a right-margin
    # column.  PyMuPDF extracts them as ordinary trailing words on every body
    # line, where they:
    #   • leak through the "whole-line-is-just-a-number" classifier (they
    #     aren't on a line by themselves), and
    #   • get glued onto adjacent words and headers, producing false REPLACE
    #     and MISSING_IN_CONVERTED diffs ("Enfield- 8 Medium,",
    #     "15 Raised Bill No. 6845").
    # ``strip_trailing_line_numbers`` only fires when the trailing numbers
    # form a near-monotonic sequence so legitimate trailing digits in body
    # text (e.g. "rate of 5") are never touched.
    if strip_trailing_line_numbers is not None:
        try:
            pymupdf_clean_text = strip_trailing_line_numbers(
                pymupdf_clean_text, state_code=state_code
            )
            converted_text = strip_trailing_line_numbers(
                converted_text, state_code=state_code
            )
        except Exception:  # pragma: no cover — defensive
            pass

    # ── Pre-pre-filter (1b): strip PyMuPDF ``T<n>`` table-row markers ─────
    # When PyMuPDF extracts a tabular layout (e.g. tax-rate schedules in CT
    # bills) it prefixes every cell line with a ``T1 ...``, ``T2 ...`` row
    # marker.  The LLM correctly drops them, but the word-level diff then
    # flags every ``T<n>`` as MISSING_IN_CONVERTED — a 28-row tax table
    # generates 28 false positives.  ``strip_table_row_markers`` removes
    # only the markers (keeping cell content intact) and only when the
    # ``T<n>`` numbers form a near-monotonic sequence starting near 1.
    if strip_table_row_markers is not None:
        try:
            pymupdf_clean_text = strip_table_row_markers(
                pymupdf_clean_text, state_code=state_code
            )
            converted_text = strip_table_row_markers(
                converted_text, state_code=state_code
            )
        except Exception:  # pragma: no cover — defensive
            pass

    # ── Pre-pre-filter (2): fuse wrapped header/footer lines ─────────────
    # PyMuPDF often splits long headers across two text lines, e.g.
    #     "Referred to Committee on EXECUTIVE AND LEGISLATIVE"
    #     "NOMINATIONS"
    # The first line classifies as HEADER (and is stripped) but the lonely
    # "NOMINATIONS" continuation falls through to the body word stream and
    # gets reported as a MISSING_IN_CONVERTED diff against the clean text
    # (where the whole header sits on one line and IS fully stripped).
    # ``merge_wrapped_artifact_lines`` joins those wrap pairs back together
    # so the classifier sees the complete header as a single line.
    if merge_wrapped_artifact_lines is not None and state_code:
        try:
            pymupdf_clean_text = merge_wrapped_artifact_lines(
                pymupdf_clean_text, state_code=state_code
            )
            converted_text = merge_wrapped_artifact_lines(
                converted_text, state_code=state_code
            )
        except Exception:  # pragma: no cover — defensive
            pass

    # ── Pre-pre-filter (3): normalise CT 2-column title-block artifact ───
    # CT bills render the page-1 title block in two visual columns but
    # PyMuPDF reads them in row-major order, producing a single jammed
    # line like ``"General Assembly Proposed Bill No. 1200"`` or
    # ``"General Assembly [A>File No. 629 <A]"`` (for post-committee
    # substitute bills).  The LLM cleaning step correctly reorders these
    # into the canonical CT title-block order (bill / file identifier
    # first on its own line, then ``General Assembly`` on its own line),
    # but the resulting positional mismatch costs ~3 pp of accuracy on
    # every CT bill — caught as a ``repositioned`` block in the report.
    # ``normalise_ct_title_block`` splits the artifact line into two
    # canonical-order lines so the downstream diff sees identical word
    # streams.  CT-only — the helper itself no-ops for other states.
    if normalise_ct_title_block is not None and state_code == "CT":
        try:
            pymupdf_clean_text = normalise_ct_title_block(
                pymupdf_clean_text, state_code=state_code
            )
            converted_text = normalise_ct_title_block(
                converted_text, state_code=state_code
            )
        except Exception:  # pragma: no cover — defensive
            pass

    # ── Pre-pre-filter (4): strip California's inline ``line N`` wrap markers ──
    # California's official LegInfo PDFs print body line numbers as the
    # literal token ``line 2``, ``line 3``, ... at the start of every
    # wrapped continuation line.  PyMuPDF extracts them verbatim; the
    # LLM cleaning step correctly strips them.  Without this filter
    # every ``line N`` token appears as a missing-words diff on the
    # raw side, costing 10-15 pp of accuracy on every CA bill.  The
    # helper is line-start anchored so it never touches legitimate
    # sentence-internal uses like ``"after line 57"``.  CA-only — the
    # helper itself no-ops for other states.
    if strip_ca_inline_line_numbers is not None and state_code == "CA":
        try:
            pymupdf_clean_text = strip_ca_inline_line_numbers(
                pymupdf_clean_text, state_code=state_code
            )
            converted_text = strip_ca_inline_line_numbers(
                converted_text, state_code=state_code
            )
        except Exception:  # pragma: no cover — defensive
            pass

    # ── Pre-pre-filter (5): strip California's running page header/footer ──
    # Every printed page of a CA bill has a running header in one of two
    # alternating forms (em-dash U+2014):
    #     — 2 — AB 45       (even pages)
    #     AB 45 — 3 —       (odd pages)
    # PyMuPDF extracts these verbatim; the LLM cleaning step correctly
    # drops them.  Without this filter every such header appears as a
    # false missing-words diff (typically 6-12 per CA bill).  The regex
    # requires the CA bill identifier sandwiched by dashes around a page
    # number AND the match must occupy the entire line — so body text
    # can never present in this exact shape.  CA-only.
    if strip_ca_page_headers is not None and state_code == "CA":
        try:
            pymupdf_clean_text = strip_ca_page_headers(
                pymupdf_clean_text, state_code=state_code
            )
            converted_text = strip_ca_page_headers(
                converted_text, state_code=state_code
            )
        except Exception:  # pragma: no cover — defensive
            pass

    # ── Pre-pre-filter (6): fuse wrapped CA Legislative Counsel's Digest captions ──
    # Every CA bill's Legislative Counsel's Digest opens with a caption
    # like ``AB 45, as amended, Bauer-Kahan. Privacy: health data:
    # location and research.``  When the subject phrase is long enough
    # PyMuPDF soft-wraps it onto a second physical line
    # (``... location and \n research.``).  The line-level classifier
    # strips the first line as a header but the orphaned ``research.``
    # continuation falls through to the body word stream as a false
    # ``MISSING_IN_CONVERTED`` diff — because the LLM correctly joined
    # the wrap onto one line in the cleaned output and the whole caption
    # is stripped as one unit there.  This helper joins the wrap pair
    # back together so the classifier sees the same complete caption on
    # both sides.  CA-only.
    if merge_ca_caption_wrap is not None and state_code == "CA":
        try:
            pymupdf_clean_text = merge_ca_caption_wrap(
                pymupdf_clean_text, state_code=state_code
            )
            converted_text = merge_ca_caption_wrap(
                converted_text, state_code=state_code
            )
        except Exception:  # pragma: no cover — defensive
            pass

    # ── Pre-pre-filter (7): strip Indiana's running page footer ──────────
    # Every printed page of an Indiana bill carries a single-line footer
    # identifying the bill plus its Legislative Service (LS) and
    # Drafter Identifier (DI) codes::
    #     2025 IN 1701—LS 7775/DI 92          (with year prefix)
    #     IN 1701—LS 7775/DI 92               (without year)
    #     SB 85—LS 6422/DI 104                (Senate Bill variant)
    # PyMuPDF extracts these verbatim on every page; the LLM cleaning
    # step correctly drops them.  Without this filter every footer
    # surfaces as a false MISSING_IN_CONVERTED diff in the accuracy
    # comparison (typically 1 per page).  The regex requires the bill
    # identifier + em-dash + ``LS \d+/DI \d+`` token sequence to occupy
    # the entire line — false positives in legislative body text are
    # effectively impossible.  IN-only.
    if strip_in_page_headers is not None and state_code == "IN":
        try:
            pymupdf_clean_text = strip_in_page_headers(
                pymupdf_clean_text, state_code=state_code
            )
            converted_text = strip_in_page_headers(
                converted_text, state_code=state_code
            )
        except Exception:  # pragma: no cover — defensive
            pass

    # ── Pre-filter: strip artifact lines from BOTH sides when state_code is set
    # SequenceMatcher tends to glue long runs of consecutive removed artifact
    # lines into one giant MISSING_IN_CONVERTED block that no per-line pattern
    # matches. By stripping artifact lines up front, the word-level diff sees
    # only real content on both sides — far fewer false positives.
    line_allowed_diffs: list[dict] = []
    line_allowed_categories: dict[str, int] = {}
    pymupdf_for_compare = pymupdf_clean_text
    converted_for_compare = converted_text

    if state_code and classify_removed_text is not None:
        def _strip_artifact_lines(text: str) -> tuple[str, list[dict], dict[str, int]]:
            kept_lines = []
            stripped_records = []
            cat_counts: dict[str, int] = {}
            for ln in text.splitlines():
                verdict = classify_removed_text(ln, state_code)
                if verdict is None:
                    kept_lines.append(ln)
                else:
                    cat = verdict["category"]
                    cat_counts[cat] = cat_counts.get(cat, 0) + 1
                    stripped_records.append({
                        "type":             "MISSING_IN_CONVERTED",
                        "missing_text":     ln.strip(),
                        "sentence":         ln,
                        "allowed_category": cat,
                        "allowed_rule":     verdict["rule"],
                    })
            return "\n".join(kept_lines), stripped_records, cat_counts

        pymupdf_for_compare, _p_stripped, _p_cats = _strip_artifact_lines(pymupdf_clean_text)
        converted_for_compare, _c_stripped, _c_cats = _strip_artifact_lines(converted_text)

        # Combine; tag converted-side strips as EXTRA_IN_CONVERTED for clarity
        line_allowed_diffs.extend(_p_stripped)
        for rec in _c_stripped:
            rec["type"] = "EXTRA_IN_CONVERTED"
            rec["extra_text"] = rec.pop("missing_text")
            line_allowed_diffs.append(rec)
        for cat, n in {**_p_cats, **_c_cats}.items():
            line_allowed_categories[cat] = (
                _p_cats.get(cat, 0) + _c_cats.get(cat, 0)
            )

    pymupdf_words = [w for w in extract_words(pymupdf_for_compare)
                      if not _is_underscore_fill([w])]
    converted_words = [w for w in extract_words(converted_for_compare)
                       if not _is_underscore_fill([w])]

    # ── Build word-index → page-number map from raw text ────────────
    # The raw PyMuPDF text contains ``--- PAGE N ---`` markers.  We map
    # each word index in pymupdf_words back to its source page so diffs
    # can report which page content was lost from.
    _page_map: list[int] = []  # _page_map[word_idx] = page_number
    _PAGE_RE = re.compile(r'---\s*PAGE\s+(\d+)\s*---', re.IGNORECASE)
    _current_page = 1
    _raw_lines = pymupdf_for_compare.splitlines()
    _word_idx = 0
    for _line in _raw_lines:
        _pm = _PAGE_RE.match(_line.strip())
        if _pm:
            _current_page = int(_pm.group(1))
            continue
        _line_words = [w for w in extract_words(_line) if not _is_underscore_fill([w])]
        for _ in _line_words:
            if _word_idx < len(pymupdf_words):
                _page_map.append(_current_page)
                _word_idx += 1
    # Pad if any words weren't covered
    while len(_page_map) < len(pymupdf_words):
        _page_map.append(_current_page)

    # ── Strip inline line numbers from PyMuPDF words before diffing ──
    # Line numbers (bare 1-4 digit tokens) interspersed in the text
    # disrupt SequenceMatcher alignment, causing huge false-positive
    # MISSING blocks.  Remove them from the PyMuPDF side and count as
    # allowed removals — the LLM is expected to strip them.
    _RE_INLINE_LINE_NUM = re.compile(r"^\d{1,4}$")
    _stripped_line_nums = 0
    if state_code:
        _conv_word_set = set(converted_words)
        _filtered_pymupdf = []
        for w in pymupdf_words:
            if _RE_INLINE_LINE_NUM.match(w) and w not in _conv_word_set:
                _stripped_line_nums += 1
            else:
                _filtered_pymupdf.append(w)
        if _stripped_line_nums:
            pymupdf_words = _filtered_pymupdf
            line_allowed_categories["line_number"] = (
                line_allowed_categories.get("line_number", 0) + _stripped_line_nums
            )

    matcher = difflib.SequenceMatcher(None, pymupdf_words, converted_words)
    matching_words = sum(
        i2 - i1
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
        if tag == 'equal'
    )
    max_words = max(len(pymupdf_words), len(converted_words))
    accuracy = (matching_words / max_words * 100) if max_words > 0 else 100.0

    base = {
        "accuracy": round(accuracy, 2),
        "pymupdf_word_count": len(pymupdf_words),
        "converted_word_count": len(converted_words),
        "matching_words": matching_words,
    }

    if pymupdf_words == converted_words:
        base.update(
            status="IDENTICAL",
            real_diffs=[], repositioned=[], allowed_diffs=line_allowed_diffs,
            real_diff_count=0, repositioned_count=0,
            allowed_diff_count=len(line_allowed_diffs),
            allowed_categories=dict(line_allowed_categories),
            raw_accuracy=base["accuracy"],
        )
        return base

    # Classify opcodes
    real_diffs = []
    repositioned = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            continue

        if tag == 'replace':
            p_phrase = pymupdf_words[i1:i2]
            c_phrase = converted_words[j1:j2]
            p_found = phrase_exists_in(p_phrase, converted_words)
            c_found = phrase_exists_in(c_phrase, pymupdf_words)
            if p_found and c_found:
                repositioned.append({
                    "pymupdf_text": ' '.join(p_phrase),
                    "converted_text": ' '.join(c_phrase),
                })
                continue
            diff = {
                "type": "REPLACE",
                "pymupdf_text": ' '.join(p_phrase),
                "converted_text": ' '.join(c_phrase),
                "sentence_pymupdf": find_sentence('', i1, pymupdf_words),
                "sentence_converted": find_sentence('', j1, converted_words),
                "page": _page_map[i1] if i1 < len(_page_map) else None,
            }
            # Sub-classify as TRANSPOSED or PUNCTUATION when possible
            sub = _classify_replace(p_phrase, c_phrase)
            if sub:
                diff.update(sub)
            real_diffs.append(diff)

        elif tag == 'delete':
            phrase = pymupdf_words[i1:i2]
            if phrase_exists_in(phrase, converted_words):
                repositioned.append({"pymupdf_text": ' '.join(phrase), "converted_text": None})
                continue
            diff = {
                "type": "MISSING_IN_CONVERTED",
                "missing_text": ' '.join(phrase),
                "sentence": find_sentence('', i1, pymupdf_words),
                "page": _page_map[i1] if i1 < len(_page_map) else None,
            }
            real_diffs.append(diff)

        elif tag == 'insert':
            phrase = converted_words[j1:j2]
            if phrase_exists_in(phrase, pymupdf_words):
                repositioned.append({"pymupdf_text": None, "converted_text": ' '.join(phrase)})
                continue
            diff = {
                "type": "EXTRA_IN_CONVERTED",
                "extra_text": ' '.join(phrase),
                "sentence": find_sentence('', j1, converted_words),
                "page": None,  # extra text has no source page
            }
            real_diffs.append(diff)

    status = "CONTENT_MATCH" if not real_diffs else "DIFFERENT"

    # ── Post-pass: filter spurious repositioned blocks ──────────────────
    # Single bare numbers (1-3 digits) that appear as "repositioned" are
    # almost always line numbers that were stripped from the raw side but
    # coincidentally match a digit elsewhere in the clean text (e.g. "1"
    # matches "Section 1", "(1)", etc.).  Move them to allowed_diffs so
    # they don't clutter the report.
    _RE_BARE_NUMBER = re.compile(r"^\d{1,3}$")
    if state_code and repositioned:
        _kept_repos = []
        for r in repositioned:
            text = r.get("pymupdf_text") or r.get("converted_text") or ""
            if _RE_BARE_NUMBER.match(text.strip()):
                # Treat as an allowed line-number removal
                line_allowed_diffs.append({
                    "type": "MISSING_IN_CONVERTED" if r.get("pymupdf_text") else "EXTRA_IN_CONVERTED",
                    "missing_text": text.strip() if r.get("pymupdf_text") else "",
                    "extra_text": text.strip() if r.get("converted_text") else "",
                    "allowed_category": "line_number",
                    "allowed_rule": "Line numbers at start of line",
                })
                line_allowed_categories["line_number"] = line_allowed_categories.get("line_number", 0) + 1
            else:
                _kept_repos.append(r)
        repositioned = _kept_repos

    # ── Repositioned blocks don't reduce accuracy — content IS present ──
    if repositioned:
        repo_bonus = 0
        for r in repositioned:
            text = r.get("pymupdf_text") or r.get("converted_text") or ""
            repo_bonus += len(text.split())
        matching_words += repo_bonus
        accuracy = (matching_words / max_words * 100) if max_words > 0 else 100.0
        base["accuracy"] = round(accuracy, 2)
        base["matching_words"] = matching_words

    # ── Post-pass: detect transpositions across adjacent delete+insert ──
    # SequenceMatcher may split "A B C" → "C B A" into separate delete/insert
    # opcodes rather than a single REPLACE.  Merge nearby delete+insert pairs
    # whose combined words are the same bag-of-words (= transposition).
    real_diffs = _merge_transposed_pairs(real_diffs, pymupdf_words, converted_words)

    # Count sub-types for quick access
    n_spacing = sum(1 for d in real_diffs if d.get("sub_type") == "SPACING")
    n_transposed = sum(1 for d in real_diffs if d.get("sub_type") == "TRANSPOSED")
    n_punctuation = sum(1 for d in real_diffs if d.get("sub_type") == "PUNCTUATION")

    # Separate spacing-only diffs — these are not real content differences
    spacing_diffs = [d for d in real_diffs if d.get("sub_type") == "SPACING"]
    real_diffs = [d for d in real_diffs if d.get("sub_type") != "SPACING"]

    # Separate line-number-in-hyphenation diffs — move to allowed later
    ln_hyph_diffs = [d for d in real_diffs if d.get("sub_type") == "LINE_NUMBER_HYPHENATION"]
    if ln_hyph_diffs:
        real_diffs = [d for d in real_diffs if d.get("sub_type") != "LINE_NUMBER_HYPHENATION"]

    # Recalculate accuracy excluding spacing-only diffs.
    # For each spacing diff, the words on both sides are the same content
    # just split differently, so count the larger side as "matching".
    if spacing_diffs:
        spacing_bonus = sum(
            max(len(d.get("pymupdf_text", "").split()),
                len(d.get("converted_text", "").split()))
            for d in spacing_diffs
        )
        adjusted_matching = matching_words + spacing_bonus
        accuracy = (adjusted_matching / max_words * 100) if max_words > 0 else 100.0
        base["accuracy"] = round(accuracy, 2)
        base["matching_words"] = adjusted_matching

    n_word_diffs = len(real_diffs) - n_transposed - n_punctuation

    # Recompute status after removing spacing diffs
    if not real_diffs and status == "DIFFERENT":
        status = "CONTENT_MATCH"

    # ── Allowlist filter: pull "intentional cleanup" diffs out ──────────
    # If state_code is provided, classify every MISSING_IN_CONVERTED and
    # EXTRA_IN_CONVERTED diff against the known artifact patterns (page
    # markers, line numbers, headers, footers, decorative fill, …).  Any
    # diff that matches a known pattern is moved into ``allowed_diffs``
    # and its word count is added back into ``matching_words`` so it no
    # longer reduces accuracy.  Real content drops stay in ``real_diffs``.
    allowed_diffs: list[dict] = []
    allowed_categories: dict[str, int] = {}
    raw_accuracy = base["accuracy"]  # preserve pre-allowlist accuracy

    # Add line-number-in-hyphenation diffs to allowed
    if ln_hyph_diffs:
        for d in ln_hyph_diffs:
            d_copy = dict(d)
            d_copy["allowed_category"] = "line_number"
            d_copy["allowed_rule"] = "Line number embedded in hyphenated word across line break"
            d_copy["_ln_hyph"] = True  # mark to avoid double-counting
            allowed_diffs.append(d_copy)
            allowed_categories["line_number"] = allowed_categories.get("line_number", 0) + 1

    if state_code and classify_removed_text is not None and real_diffs:
        _kept: list[dict] = []
        for d in real_diffs:
            text = (
                d.get("missing_text")
                or d.get("extra_text")
                or d.get("pymupdf_text")
                or d.get("converted_text")
                or ""
            )
            verdict = classify_removed_text(text, state_code) if text else None
            if verdict:
                d_copy = dict(d)
                d_copy["allowed_category"] = verdict["category"]
                d_copy["allowed_rule"]     = verdict["rule"]
                allowed_diffs.append(d_copy)
                cat = verdict["category"]
                allowed_categories[cat] = allowed_categories.get(cat, 0) + 1
            else:
                _kept.append(d)
        real_diffs = _kept

    # ── LLM-removed-lines fallback: if a diff's text appears in lines the
    # LLM intentionally removed, classify as allowed (universal, no regex needed)
    if llm_removed_lines and real_diffs:
        # Normalize dashes (em/en dash → hyphen) for consistent matching
        def _norm_dash(s: str) -> str:
            return s.replace("\u2014", "-").replace("\u2013", "-").replace("\ufffd", "-")

        _removed_normed = [_norm_dash(l.strip()) for l in llm_removed_lines]
        _removed_blob = " ".join(_removed_normed)
        _kept2: list[dict] = []
        for d in real_diffs:
            text = (
                d.get("missing_text")
                or d.get("extra_text")
                or d.get("pymupdf_text")
                or d.get("converted_text")
                or ""
            ).strip()
            text_normed = _norm_dash(text)
            # Check if the entire diff text appears as a substring in removed lines
            # (handles multi-word phrases that span across removed line boundaries)
            if text_normed and (text_normed in _removed_blob or all(
                any(w in removed_line for removed_line in _removed_normed)
                for w in text_normed.split()
            )):
                d_copy = dict(d)
                d_copy["allowed_category"] = "llm_intentional"
                d_copy["allowed_rule"] = "Content intentionally removed by LLM during cleaning"
                allowed_diffs.append(d_copy)
                allowed_categories["llm_intentional"] = allowed_categories.get("llm_intentional", 0) + 1
            else:
                _kept2.append(d)
        real_diffs = _kept2

    if allowed_diffs:
        # Each allowed diff's words count back as matching.
        # For LINE_NUMBER_HYPHENATION diffs, only count converted side and
        # reduce max_words by the artifact line-number words.
        allowed_word_bonus = 0
        ln_artifact_reduction = 0
        for d in allowed_diffs:
            if d.get("_ln_hyph"):
                # Only count converted (correct) side; artifact words shrink max
                c_wc = len((d.get("converted_text") or "").split())
                p_wc = len((d.get("pymupdf_text") or "").split())
                allowed_word_bonus += c_wc
                ln_artifact_reduction += (p_wc - c_wc)
            elif d.get("type") == "REPLACE":
                allowed_word_bonus += max(
                    len((d.get("pymupdf_text") or "").split()),
                    len((d.get("converted_text") or "").split()),
                )
            elif d.get("type") == "MISSING_IN_CONVERTED":
                allowed_word_bonus += len((d.get("missing_text") or "").split())
            elif d.get("type") == "EXTRA_IN_CONVERTED":
                allowed_word_bonus += len((d.get("extra_text") or "").split())
        max_words -= ln_artifact_reduction
        new_matching = base["matching_words"] + allowed_word_bonus
        new_accuracy = (new_matching / max_words * 100) if max_words > 0 else 100.0
        new_accuracy = min(new_accuracy, 100.0)  # cap at 100%
        base["matching_words"] = new_matching
        base["accuracy"] = round(new_accuracy, 2)

        # Recount real-diff sub-types after allowlist filter
        n_transposed  = sum(1 for d in real_diffs if d.get("sub_type") == "TRANSPOSED")
        n_punctuation = sum(1 for d in real_diffs if d.get("sub_type") == "PUNCTUATION")
        n_word_diffs  = len(real_diffs) - n_transposed - n_punctuation

        # Re-evaluate status
        if not real_diffs:
            status = "CONTENT_MATCH"

    # ── Merge in the line-level allowlist (pre-filtered above) ───────────
    # These are full lines that were stripped before the word-level diff
    # ran (page markers, headers, footers, etc.).  They don't reduce
    # accuracy — they never reached the diff stage — but we surface them
    # in ``allowed_diffs`` so the UI can show *all* intentional removals.
    if line_allowed_diffs:
        allowed_diffs = line_allowed_diffs + allowed_diffs
        for cat, n in line_allowed_categories.items():
            allowed_categories[cat] = allowed_categories.get(cat, 0) + n

    # If no real differences remain, accuracy is 100% — repositioned blocks
    # are content that IS present (just moved, e.g. table cells), not errors.
    if not real_diffs:
        status = "CONTENT_MATCH"
        base["accuracy"] = 100.0

    base.update(
        status=status,
        real_diffs=real_diffs,
        repositioned=repositioned,
        allowed_diffs=allowed_diffs,
        real_diff_count=len(real_diffs),
        repositioned_count=len(repositioned),
        allowed_diff_count=len(allowed_diffs),
        allowed_categories=allowed_categories,
        transposed_count=n_transposed,
        punctuation_count=n_punctuation,
        spacing_count=n_spacing,
        spacing_diffs=spacing_diffs,
        word_diff_count=n_word_diffs,
        raw_accuracy=raw_accuracy,
    )
    return base


# ─────────────────────────────────────────────────────────────────────
# CLI — pretty-printed console report (original behaviour)
# ─────────────────────────────────────────────────────────────────────
def compare_two_files(pymupdf_clean_path, converted_path):
    """Compare two files and print detailed report with full sentences."""

    print("=" * 90)
    print(f"  File 1 (pymupdf_clean): {os.path.basename(pymupdf_clean_path)}")
    print(f"  File 2 (converted)    : {os.path.basename(converted_path)}")
    print("=" * 90)

    # Read files
    try:
        with open(pymupdf_clean_path, 'r', encoding='utf-8') as f:
            pymupdf_raw = f.read()
        with open(converted_path, 'r', encoding='utf-8') as f:
            converted_raw = f.read()
    except Exception as e:
        print(f"  ERROR reading files: {e}")
        return

    # Use the programmatic API
    report = compare_texts(pymupdf_raw, converted_raw)

    print(f"\n  Word count (pymupdf_clean) : {report['pymupdf_word_count']}")
    print(f"  Word count (converted)     : {report['converted_word_count']}")
    print(f"  Matching words             : {report['matching_words']}")
    print(f"  Accuracy                   : {report['accuracy']:.2f}%")
    print()

    if report["status"] == "IDENTICAL":
        print("  STATUS: IDENTICAL - All words match exactly after tag removal.")
        return

    if report["status"] == "CONTENT_MATCH":
        print(f"  STATUS: CONTENT MATCH - All text present in both files.")
        if report["repositioned"]:
            print(f"  ({report['repositioned_count']} block(s) appear at different positions but all content exists.)")
            print()
            for idx, rp in enumerate(report["repositioned"], 1):
                text = rp.get("pymupdf_text") or rp.get("converted_text", "")
                display = text if len(text) <= 120 else text[:117] + '...'
                print(f"    Repositioned #{idx}: {display}")
        return

    # Breakdown by sub-type
    _n_trans = report.get('transposed_count', 0)
    _n_punct = report.get('punctuation_count', 0)
    _n_word  = report.get('word_diff_count', 0)
    _parts = []
    if _n_word:  _parts.append(f"{_n_word} word diff(s)")
    if _n_trans: _parts.append(f"{_n_trans} transposed")
    if _n_punct: _parts.append(f"{_n_punct} punctuation")
    _breakdown = ", ".join(_parts) if _parts else "0"
    print(f"  STATUS: DIFFERENT - {report['real_diff_count']} real difference(s) "
          f"[{_breakdown}], {report['repositioned_count']} repositioned block(s).")

    # Print repositioned (informational)
    if report["repositioned"]:
        print(f"\n  --- Repositioned (present in both, different location) ---")
        for idx, rp in enumerate(report["repositioned"], 1):
            text = rp.get("pymupdf_text") or rp.get("converted_text", "")
            display = text if len(text) <= 120 else text[:117] + '...'
            print(f"    #{idx}: {display}")

    # Print real differences with full sentences
    print(f"\n  --- Real Differences ---\n")

    for idx, d in enumerate(report["real_diffs"], 1):
        dtype = d["type"]
        sub = d.get("sub_type", "")

        if dtype == "REPLACE":
            if sub == "TRANSPOSED":
                print(f"  #{idx} \U0001f500 TRANSPOSED WORDS")
                print(f"      Expected order : {d['pymupdf_text']}")
                print(f"      Got order      : {d['converted_text']}")
                print(f"      Words moved:")
                for mw in d.get('moved_words', []):
                    print(f"        pos {mw['position']}: \"{mw['expected']}\" \u2192 \"{mw['got']}\"")
            elif sub == "PUNCTUATION":
                print(f"  #{idx} \u2702 PUNCTUATION DIFFERENCE")
                print(f"      Expected : {d['pymupdf_text']}")
                print(f"      Got      : {d['converted_text']}")
                for cd_ in d.get('char_diffs', []):
                    kind = cd_['kind']
                    ch   = cd_['char']
                    if kind == 'MISSING':
                        print(f"        \u2717 Missing {ch}  \u2026{cd_['context']}\u2026")
                    elif kind == 'EXTRA':
                        print(f"        \u2717 Extra {ch}  \u2026{cd_['context']}\u2026")
                    elif kind == 'CHANGED':
                        print(f"        \u2717 Changed {ch}  \u2026{cd_['context']}\u2026")
            else:
                print(f"  #{idx} REPLACE")
                print(f"      In pymupdf_clean : {d['pymupdf_text']}")
                print(f"      In converted     : {d['converted_text']}")
            print(f"      Full sentence (pymupdf_clean):")
            print(f"        {d['sentence_pymupdf']}")
            print(f"      Full sentence (converted):")
            print(f"        {d['sentence_converted']}")

        elif dtype == "MISSING_IN_CONVERTED":
            print(f"  #{idx} MISSING in converted")
            print(f"      Missing text: {d['missing_text']}")
            print(f"      Full sentence (from pymupdf_clean):")
            print(f"        {d['sentence']}")

        elif dtype == "EXTRA_IN_CONVERTED":
            print(f"  #{idx} EXTRA in converted")
            print(f"      Extra text: {d['extra_text']}")
            print(f"      Full sentence (from converted):")
            print(f"        {d['sentence']}")

        print()

    # Summary
    print("-" * 90)
    print(f"  SUMMARY: {report['real_diff_count']} real difference(s) [{_breakdown}], "
          f"{report['repositioned_count']} repositioned block(s)")
    print("=" * 90)


def main():
    # ── Input files (hardcoded) ──
    pymupdf_clean_path = r"C:\Users\C303365\Downloads\Pending_Legislation\Opus4.6\2025_2025_4_2_2_001300_0_8_1_20250410_0_converted_pymupdf_clean.txt"
    converted_path = r"C:\Users\C303365\Downloads\Pending_Legislation\Opus4.6\2025_2025_4_2_2_001300_0_8_1_20250410_0_converted.txt"

    # Validate files exist
    if not os.path.exists(pymupdf_clean_path):
        print(f"ERROR: File not found: {pymupdf_clean_path}")
        sys.exit(1)
    if not os.path.exists(converted_path):
        print(f"ERROR: File not found: {converted_path}")
        sys.exit(1)

    compare_two_files(pymupdf_clean_path, converted_path)


if __name__ == '__main__':
    main()
