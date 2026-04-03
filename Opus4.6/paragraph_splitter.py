"""
Legislative Paragraph Splitter
================================
Inserts line breaks at structural subsection boundaries in legislative text,
correctly splitting [A>...<A] and [D>...<D] amendment tags when a break
falls inside them.

Break points:
  1. After statute section numbers like "8-1566."
  2. Before numbered subsection markers like (1), (2), (3)
  3. (Optional) Before lettered subsection markers like (a), (b), (c)

When a break falls inside a tag the tag is closed before the break
and re-opened after it so that tags remain well-formed.

Usage:
    from paragraph_splitter import split_legislative_paragraphs
    formatted = split_legislative_paragraphs(raw_text)
"""

import re


# ── configurable patterns ────────────────────────────────────────────────
# Numbered subsections: (1), (2), (10), …
_NUMBERED_RE = re.compile(r'\(\d+\)')

# Lettered subsections: (a), (b), … (z), (aa), (bb), …
_LETTERED_RE = re.compile(r'\([a-z]{1,2}\)')

# Roman-numeral subsections: (i), (ii), (iii), (iv), (ix), (xi), …
_ROMAN_RE = re.compile(r'\((?:i{1,3}|iv|vi{0,3}|ix|xi{0,3}|xiv|xv)\)')

# Upper-case lettered subsections: (A), (B), …
_UPPER_RE = re.compile(r'\([A-Z]{1,2}\)')

# Statute section number at the very start of a paragraph
_SECTION_NUM_RE = re.compile(r'^(\d+-\d+(?:\.\d+)*\.)\s*')

# Reference contexts — if the text just before the marker ends with one of
# these we do NOT insert a break (the marker is a cross-reference, not a
# structural subsection start).
_REF_TAILS = re.compile(
    r'(?:'
    r'K\.S\.A\.\s*'           # K.S.A. 8-1506(a)
    r'|\d+-\d+\s*'            # 41-311(a)(2)
    r'|\)\s*'                 # nested like (a)(2)
    r'|subsection\s*'
    r'|section\s*'
    r'|paragraph\s*'
    r'|subdivision\s*'
    r')$',
    re.IGNORECASE,
)


def split_legislative_paragraphs(
    text: str,
    *,
    split_numbered: bool = True,
    split_lettered: bool = False,
    split_roman: bool = False,
    split_upper: bool = False,
) -> str:
    """
    Insert line breaks at structural subsection boundaries.

    Parameters
    ----------
    text : str
        Raw legislative text (may contain [A>…<A] / [D>…<D] tags).
    split_numbered : bool
        Break before (1), (2), … markers.
    split_lettered : bool
        Break before (a), (b), … markers.
    split_roman : bool
        Break before (i), (ii), … markers.
    split_upper : bool
        Break before (A), (B), … markers.

    Returns
    -------
    str
        Formatted text with line breaks inserted (tags correctly split).
    """
    # Build the set of patterns we want to break on
    patterns: list[re.Pattern] = []
    if split_numbered:
        patterns.append(_NUMBERED_RE)
    if split_lettered:
        patterns.append(_LETTERED_RE)
    if split_roman:
        patterns.append(_ROMAN_RE)
    if split_upper:
        patterns.append(_UPPER_RE)

    if not patterns:
        return text

    # Combined pattern that matches any enabled subsection marker
    combined = re.compile('|'.join(p.pattern for p in patterns))

    result: list[str] = []
    i = 0
    n = len(text)
    in_tag: str | None = None      # None | 'A' | 'D'

    # ── Step 1: section number at the start ──────────────────────────────
    sec = _SECTION_NUM_RE.match(text)
    if sec:
        result.append(sec.group(1))
        result.append('\n')
        i = sec.end()

    # ── Step 2: walk the string character by character ────────────────────
    while i < n:

        # ── tag open ──
        if in_tag is None and text[i:i+3] in ('[A>', '[D>'):
            in_tag = text[i + 1]
            result.append(text[i:i+3])
            i += 3
            continue

        # ── tag close ──
        if in_tag == 'A' and text[i:i+3] == '<A]':
            in_tag = None
            result.append('<A]')
            i += 3
            continue
        if in_tag == 'D' and text[i:i+3] == '<D]':
            in_tag = None
            result.append('<D]')
            i += 3
            continue

        # ── subsection marker? ──
        sm = combined.match(text, i)
        if sm:
            so_far = ''.join(result)

            # Only break if there is preceding content on the same line
            if so_far and not so_far.endswith('\n'):
                # Check it's structural, not a cross-reference
                stripped = so_far.rstrip()
                is_reference = bool(_REF_TAILS.search(stripped))

                if not is_reference:
                    if in_tag:
                        # Close current tag  →  \n  →  reopen tag
                        # Strip trailing whitespace before closing
                        while result and result[-1] in (' ', '\t'):
                            result.pop()
                        result.append(f'<{in_tag}]')
                        result.append('\n')
                        result.append(f'[{in_tag}>')
                    else:
                        while result and result[-1] in (' ', '\t'):
                            result.pop()
                        result.append('\n')

            # Emit the marker itself
            result.append(sm.group())
            i = sm.end()
            continue

        # ── ordinary character ──
        result.append(text[i])
        i += 1

    return ''.join(result)


# ─────────────────────────────────────────────────────────────────────────
# Quick helper to apply to a whole file (line by line)
# ─────────────────────────────────────────────────────────────────────────
def split_file(input_path: str, output_path: str | None = None, **kwargs) -> str:
    """Read a converted .txt file, apply paragraph splitting, write result."""
    with open(input_path, 'r', encoding='utf-8') as f:
        text = f.read()

    formatted = split_legislative_paragraphs(text, **kwargs)

    if output_path is None:
        # Default: write next to input with _split suffix
        base, ext = input_path.rsplit('.', 1)
        output_path = f"{base}_split.{ext}"

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(formatted)

    return output_path


# ─────────────────────────────────────────────────────────────────────────
# Demo / self-test
# ─────────────────────────────────────────────────────────────────────────
if __name__ == '__main__':

    test_input = (
        '8-1566. (a) Any person [D>who drives any<D] '
        '[A>shall be guilty of reckless driving if such person operates a<A] '
        'vehicle[A>: (1)<A] in willful or wanton disregard for the safety of '
        'persons or property [D>is guilty of reckless driving.<D] '
        '[A>; or (2) at a speed of<A] [D>100 miles per hour or more or<D] '
        '[A>35 miles per hour or more over the maximum posted speed limit, '
        'except as provided in K.S.A. 8-1506, and amendments thereto, '
        'without regard to any culpable mental state.<A]'
    )

    print("=" * 70)
    print("INPUT (single line):")
    print("=" * 70)
    print(test_input)

    result = split_legislative_paragraphs(test_input, split_numbered=True)

    print()
    print("=" * 70)
    print("OUTPUT (with paragraph splits):")
    print("=" * 70)
    print(result)

    # ── Verify tags are still balanced ──
    open_a  = result.count('[A>')
    close_a = result.count('<A]')
    open_d  = result.count('[D>')
    close_d = result.count('<D]')
    print()
    print(f"Tag balance:  [A>={open_a}  <A]={close_a}  [D>={open_d}  <D]={close_d}")
    assert open_a == close_a, "Unbalanced [A> tags!"
    assert open_d == close_d, "Unbalanced [D> tags!"
    print("✅ All tags balanced.")
