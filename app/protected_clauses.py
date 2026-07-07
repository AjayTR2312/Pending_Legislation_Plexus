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
        # Bill enacting clause
        "Be it enacted by the Legislature of the State of Kansas:",
        # Concurrent / proposition resolving clause (e.g. constitutional
        # amendment Senate Concurrent Resolutions like SCR 1616)
        "Be it resolved by the Legislature of the State of Kansas",
        # Senate Resolution (ceremonial / honorary)
        "Be it resolved by the Senate of the State of Kansas",
        # House Resolution / House Concurrent Resolution
        "Be it resolved by the House of Representatives of the State of Kansas",
        # Additional resolving clauses inside resolutions
        "Be it further resolved",
    ],
    "DE": [
        "SYNOPSIS",
    ],
    "IN": [
        "Citations Affected:",
        "Synopsis:",
        "Effective:",
        "Be it enacted by the General Assembly of the State of Indiana:",
    ],
    "CA": [
        "The people of the State of California do enact as follows:",
        # Individual "Resolved" variants are no longer needed here —
        # prefix-based matching in PROTECTED_CLAUSE_PREFIXES handles
        # ALL resolving-clause forms automatically.
        "Resolved,",
    ],
}

# ──────────────────────────────────────────────────────────────────────────────
#  Prefix-based protection — any tagged span whose stripped content starts
#  with one of these prefixes has its tags removed, regardless of what
#  follows the prefix.  This avoids having to enumerate every resolving-
#  clause variant (e.g. "jointly,", "the Assembly thereof concurring,").
# ──────────────────────────────────────────────────────────────────────────────
PROTECTED_CLAUSE_PREFIXES: dict[str, list[str]] = {
    "CA": [
        "Resolved by the Senate",
        "Resolved by the Assembly",
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
    prefixes = PROTECTED_CLAUSE_PREFIXES.get(state_code.upper(), [])
    if not clauses and not prefixes:
        return text

    for clause in clauses:
        pattern = _build_pattern(clause)

        def _strip_tags(m: re.Match) -> str:
            matched = m.group(0)
            # Detect if the match consumed an opening tag [A> or [D>
            leading_tag_m = re.match(r'\[(A|D)>', matched)
            leading_tag = leading_tag_m.group(1) if leading_tag_m else None

            # Detect if the match consumed a closing tag <A] or <D]
            trailing_tag_m = re.search(r'<(A|D)\]$', matched)
            trailing_tag = trailing_tag_m.group(1) if trailing_tag_m else None

            # Strip all tags from the matched span
            cleaned = re.sub(r'\[A>', '', matched)
            cleaned = re.sub(r'<A\]', '', cleaned)
            cleaned = re.sub(r'\[D>', '', cleaned)
            cleaned = re.sub(r'<D\]', '', cleaned)

            # If the match consumed an opening tag but NOT a closing tag,
            # the text AFTER the protected clause in the original span is
            # now orphaned (has a closer but no opener).
            # → Close the tag before the clause, then re-open it after.
            # But since the clause is at the start of the span, we just
            # need to re-open the tag after the cleaned clause text.
            if leading_tag and not trailing_tag:
                cleaned = cleaned + f'[{leading_tag}>'

            # If the match consumed a closing tag but NOT an opening tag,
            # the text BEFORE the clause is orphaned.
            # → Close the tag before the clause text.
            if trailing_tag and not leading_tag:
                cleaned = f'<{trailing_tag}]' + cleaned

            # If both consumed, the clause was a complete tagged span — just
            # return the cleaned text (no re-opening/closing needed).

            return cleaned

        text = pattern.sub(_strip_tags, text)

    # ── Prefix-based stripping ───────────────────────────────────────
    # For each prefix, find tagged spans [A>…<A] or [D>…<D] whose inner
    # text (after stripping any nested tags) starts with the prefix.
    # Strip the outer tags so the resolving clause appears untagged.
    for prefix in prefixes:
        escaped = re.escape(prefix)
        for tag in ('A', 'D'):
            _prefix_pat = re.compile(
                r'\[' + tag + r'>(' + escaped + r'.*?)<' + tag + r'\]',
                re.IGNORECASE,
            )
            text = _prefix_pat.sub(r'\1', text)

    return text
