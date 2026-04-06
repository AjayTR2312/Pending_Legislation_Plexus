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


def extract_words(text):
    """Extract list of words from text after stripping tags and normalizing whitespace."""
    cleaned = strip_tags(text)
    normalized = re.sub(r'\s+', ' ', cleaned).strip()
    return normalized.split()


def get_clean_text(text):
    """Get tag-stripped, whitespace-normalized text."""
    cleaned = strip_tags(text)
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

    # ── 2. PUNCTUATION-only: words identical ignoring punctuation ──
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
def compare_texts(pymupdf_clean_text: str, converted_text: str) -> dict:
    """Compare two text strings and return a structured accuracy report.

    Parameters
    ----------
    pymupdf_clean_text : str
        Tag-stripped PyMuPDF clean text (ground truth).
    converted_text : str
        Claude Opus converted text (validation target).

    Returns
    -------
    dict with keys:
        accuracy              – float, 0–100
        pymupdf_word_count    – int
        converted_word_count  – int
        matching_words        – int
        status                – "IDENTICAL" | "CONTENT_MATCH" | "DIFFERENT"
        real_diffs            – list of diff dicts
        repositioned          – list of repositioned dicts
        real_diff_count       – int
        repositioned_count    – int
    """
    pymupdf_words = [w for w in extract_words(pymupdf_clean_text)
                      if not _is_underscore_fill([w])]
    converted_words = [w for w in extract_words(converted_text)
                       if not _is_underscore_fill([w])]

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
        base.update(status="IDENTICAL", real_diffs=[], repositioned=[],
                    real_diff_count=0, repositioned_count=0)
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
            }
            real_diffs.append(diff)

    status = "CONTENT_MATCH" if not real_diffs else "DIFFERENT"

    # ── Post-pass: detect transpositions across adjacent delete+insert ──
    # SequenceMatcher may split "A B C" → "C B A" into separate delete/insert
    # opcodes rather than a single REPLACE.  Merge nearby delete+insert pairs
    # whose combined words are the same bag-of-words (= transposition).
    real_diffs = _merge_transposed_pairs(real_diffs, pymupdf_words, converted_words)

    # Count sub-types for quick access
    n_transposed = sum(1 for d in real_diffs if d.get("sub_type") == "TRANSPOSED")
    n_punctuation = sum(1 for d in real_diffs if d.get("sub_type") == "PUNCTUATION")
    n_word_diffs = len(real_diffs) - n_transposed - n_punctuation

    base.update(
        status=status,
        real_diffs=real_diffs,
        repositioned=repositioned,
        real_diff_count=len(real_diffs),
        repositioned_count=len(repositioned),
        transposed_count=n_transposed,
        punctuation_count=n_punctuation,
        word_diff_count=n_word_diffs,
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
