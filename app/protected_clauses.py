"""
Protected Clauses — state-specific phrases that must never carry [A>…<A] or [D>…<D] tags.

After the PyMuPDF LLM-clean step, call ``strip_protected_tags(text, state_code)``
to remove any tags that wrap (fully or partially) a protected clause.
"""

import re

# ──────────────────────────────────────────────────────────────────────────────
#  State → list of protected phrases  (case-insensitive matching)
# ──────────────────────────────────────────────────────────────────────────────
PROTECTED_CLAUSES: dict[str, list[str]] = {
    "KS": [
        "Be it enacted by the Legislature of the State of Kansas:",
    ],
    "DE": [
        "SYNOPSIS",
    ],
    "IN": [
        "Citations Affected:",
        "Synopsis:",
        "Effective:",
    ],
    "CA": [
        "The people of the State of California do enact as follows:",
        "Resolved by the Senate of the State of California, the Assembly thereof concurring,",
        "Resolved,",
        "Resolved by the Assembly of the State of California, the Senate thereof concurring,",
    ],
}


def _build_pattern(clause: str) -> re.Pattern:
    """Build a regex that matches *clause* even when it is wrapped — fully or
    partially — by ``[A>…<A]`` or ``[D>…<D]`` tags.

    Strategy:
      • Escape the literal clause so special regex chars are safe.
      • Allow optional ``[A>`` / ``[D>`` before every word boundary and
        optional ``<A]`` / ``<D]`` after every word boundary.
      • Allow optional tags around the whole phrase as well.

    This is deliberately *greedy* so that it captures the largest possible
    tagged span that contains the clause.
    """
    # Escape clause for regex, then split on whitespace to get words
    words = clause.split()
    escaped_words = [re.escape(w) for w in words]

    # Optional tag open/close that might appear between words
    _tag_open = r'(?:\[(?:A|D)>)?'
    _tag_close = r'(?:<(?:A|D)\])?'

    # Build pattern: optional leading tag, then each word with optional
    # tags and flexible whitespace between them
    parts = []
    for i, ew in enumerate(escaped_words):
        parts.append(_tag_open + ew + _tag_close)

    inner = r'\s*'.join(parts)

    # Wrap with optional outer tags (in case the entire clause is tagged)
    pattern = _tag_open + inner + _tag_close

    return re.compile(pattern, re.IGNORECASE)


def strip_protected_tags(text: str, state_code: str | None) -> str:
    """Remove ``[A>…<A]`` and ``[D>…<D]`` tags that overlap any protected
    clause for the given *state_code*.

    Parameters
    ----------
    text : str
        The PyMuPDF-clean output (may contain legislative tags).
    state_code : str | None
        Two-letter state abbreviation (e.g. ``"KS"``).  If ``None`` or not
        found in :data:`PROTECTED_CLAUSES`, *text* is returned unchanged.

    Returns
    -------
    str
        Text with tags stripped from protected clauses only.
    """
    if not state_code or not text:
        return text

    clauses = PROTECTED_CLAUSES.get(state_code.upper(), [])
    if not clauses:
        return text

    for clause in clauses:
        pattern = _build_pattern(clause)
        # Replace each match with a tag-free version (just strip all tags)
        def _strip_tags(m: re.Match) -> str:
            matched = m.group(0)
            # Remove all [A>…<A] and [D>…<D] markers from the matched span
            cleaned = re.sub(r'\[A>', '', cleaned := matched)
            cleaned = re.sub(r'<A\]', '', cleaned)
            cleaned = re.sub(r'\[D>', '', cleaned)
            cleaned = re.sub(r'<D\]', '', cleaned)
            return cleaned

        text = pattern.sub(_strip_tags, text)

    return text
