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
    pymupdf_words = extract_words(pymupdf_clean_text)
    converted_words = extract_words(converted_text)

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
    base.update(
        status=status,
        real_diffs=real_diffs,
        repositioned=repositioned,
        real_diff_count=len(real_diffs),
        repositioned_count=len(repositioned),
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

    print(f"  STATUS: DIFFERENT - {report['real_diff_count']} real difference(s), "
          f"{report['repositioned_count']} repositioned block(s).")

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

        if dtype == "REPLACE":
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
    print(f"  SUMMARY: {report['real_diff_count']} real difference(s), "
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
