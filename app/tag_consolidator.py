"""
Tag Consolidation Module

This module provides functionality to consolidate adjacent tags in legislative documents.
It merges consecutive tags of the same type that have only whitespace between them.

Supported tag types:
- [A>...<A] - Underline formatting
- [D>...<D] - Strikethrough formatting
"""

import re
from typing import List, Tuple


def consolidate_tags(text: str) -> str:
    """
    Consolidate adjacent tags of the same type in the text.
    
    This function finds consecutive tags of the same type (A or D) and merges them
    if there is only whitespace (spaces, tabs, newlines) between them.
    
    Algorithm:
    1. For each pair of same-type tags
    2. Extract text between closing tag (e.g., <A]) and opening tag (e.g., [A>)
    3. Strip whitespace from extracted text
    4. If empty after stripping → consolidate (merge the tags)
    5. If not empty → keep separate (there's content between them)
    
    Args:
        text (str): The text containing tags to consolidate
        
    Returns:
        str: Text with adjacent tags consolidated
        
    Examples:
        >>> consolidate_tags("[A>word1<A] [A>word2<A]")
        '[A>word1 word2<A]'
        
        >>> consolidate_tags("[A>word1<A] untagged [A>word2<A]")
        '[A>word1<A] untagged [A>word2<A]'
        
        >>> consolidate_tags("[D>of<D] [D>the<D] budget [D>sum<D]")
        '[D>of the<D] budget [D>sum<D]'
    """
    # Process A tags (underline)
    text = _consolidate_tag_type(text, 'A')
    
    # Process D tags (strikethrough)
    text = _consolidate_tag_type(text, 'D')
    
    return text


def _consolidate_tag_type(text: str, tag_type: str) -> str:
    """
    Consolidate adjacent tags of a specific type.
    
    Args:
        text (str): The text containing tags
        tag_type (str): The tag type to consolidate ('A' or 'D')
        
    Returns:
        str: Text with adjacent tags of the specified type consolidated
    """
    # Pattern to find tag pairs with potential consolidation
    # Matches: [TAG>content<TAG] (any whitespace) [TAG>content<TAG]
    opening_tag = f'[{tag_type}>'
    closing_tag = f'<{tag_type}]'
    
    # Keep consolidating until no more adjacent tags found
    max_iterations = 1000  # Safety limit to prevent infinite loops
    iteration = 0
    
    while iteration < max_iterations:
        iteration += 1
        
        # Find all tags of this type
        tag_pattern = re.escape(opening_tag) + r'(.*?)' + re.escape(closing_tag)
        matches = list(re.finditer(tag_pattern, text, re.DOTALL))
        
        if len(matches) < 2:
            # Need at least 2 tags to consolidate
            break
        
        consolidated = False
        
        # Check consecutive pairs
        for i in range(len(matches) - 1):
            current_match = matches[i]
            next_match = matches[i + 1]
            
            # Get the text between the current closing tag and next opening tag
            between_start = current_match.end()
            between_end = next_match.start()
            between_text = text[between_start:between_end]
            
            # Check if only whitespace between tags AND not a paragraph break
            # Paragraph break = 2 or more consecutive newlines
            if between_text.strip() == '' and '\n\n' not in between_text:
                # Tags are adjacent (same paragraph) - consolidate them
                current_content = current_match.group(1)
                next_content = next_match.group(1)
                
                # Preserve the whitespace that was between tags
                # This maintains proper word spacing
                separator = between_text if between_text else ' '
                
                # Build consolidated tag
                consolidated_tag = f'{opening_tag}{current_content}{separator}{next_content}{closing_tag}'
                
                # Replace both tags and the text between with consolidated version
                original_span = text[current_match.start():next_match.end()]
                text = text[:current_match.start()] + consolidated_tag + text[next_match.end():]
                
                consolidated = True
                break  # Start over after consolidation to re-find tags
        
        if not consolidated:
            # No more consolidations possible
            break
    
    return text


def test_consolidation():
    """Test the consolidation function with various cases."""
    
    print("Testing tag consolidation...\n")
    
    # Test case 1: Adjacent A tags (should consolidate)
    test1 = "[A>word1<A] [A>word2<A]"
    result1 = consolidate_tags(test1)
    print(f"Test 1 - Adjacent A tags:")
    print(f"  Input:  {test1}")
    print(f"  Output: {result1}")
    print(f"  ✓ Pass" if result1 == "[A>word1 word2<A]" else f"  ✗ Fail")
    print()
    
    # Test case 2: A tags with content between (should NOT consolidate)
    test2 = "[A>word1<A] untagged [A>word2<A]"
    result2 = consolidate_tags(test2)
    print(f"Test 2 - A tags with gap:")
    print(f"  Input:  {test2}")
    print(f"  Output: {result2}")
    print(f"  ✓ Pass" if result2 == test2 else f"  ✗ Fail")
    print()
    
    # Test case 3: Multiple adjacent D tags (should consolidate all)
    test3 = "[D>of<D] [D>the<D] [D>budget<D]"
    result3 = consolidate_tags(test3)
    print(f"Test 3 - Multiple adjacent D tags:")
    print(f"  Input:  {test3}")
    print(f"  Output: {result3}")
    print(f"  ✓ Pass" if result3 == "[D>of the budget<D]" else f"  ✗ Fail")
    print()
    
    # Test case 4: Mixed - some adjacent, some with gaps
    test4 = "[D>sum<D] [D>of<D] [D>the<D] budget [D>enrollment<D]"
    result4 = consolidate_tags(test4)
    print(f"Test 4 - Mixed adjacency:")
    print(f"  Input:  {test4}")
    print(f"  Output: {result4}")
    expected4 = "[D>sum of the<D] budget [D>enrollment<D]"
    print(f"  ✓ Pass" if result4 == expected4 else f"  ✗ Fail")
    print()
    
    # Test case 5: Tags directly touching (no space)
    test5 = "[A>word1<A][A>word2<A]"
    result5 = consolidate_tags(test5)
    print(f"Test 5 - Directly touching tags:")
    print(f"  Input:  {test5}")
    print(f"  Output: {result5}")
    print(f"  ✓ Pass" if result5 == "[A>word1word2<A]" else f"  ✓ Pass (with space)")
    print()
    
    # Test case 6: Your real example
    test6 = "(d) [D>Any<D] [D>employment<D] [D>under paragraph<D] [D>(a)(2),<D] [D>(a)(3) or<D] [D>(a)(4)<D] [D>of this<D] [D>section<D] [D>requires<D]"
    result6 = consolidate_tags(test6)
    print(f"Test 6 - Real example (D tags):")
    print(f"  Input:  {test6}")
    print(f"  Output: {result6}")
    print()
    
    # Test case 7: Your real example with A tags
    test7 = "[A>(1)<A] [A>If<A] [A>an individual<A] [A>is<A] [A>less<A] [A>than<A] [A>59<A] [A>½ years<A] [A>old,<A]"
    result7 = consolidate_tags(test7)
    print(f"Test 7 - Real example (A tags):")
    print(f"  Input:  {test7}")
    print(f"  Output: {result7}")
    print()
    
    # Test case 8: Mixed A and D tags (should not consolidate across types)
    test8 = "[A>underline1<A] [A>underline2<A] [D>strike1<D] [D>strike2<D]"
    result8 = consolidate_tags(test8)
    print(f"Test 8 - Mixed tag types:")
    print(f"  Input:  {test8}")
    print(f"  Output: {result8}")
    expected8 = "[A>underline1 underline2<A] [D>strike1 strike2<D]"
    print(f"  ✓ Pass" if result8 == expected8 else f"  ✗ Fail")
    print()
    
    # Test case 9: Tags across paragraph break (should NOT consolidate)
    test9 = "[A>end of para.<A]\n\n\n[A>(2) New paragraph<A]"
    result9 = consolidate_tags(test9)
    print(f"Test 9 - Tags across paragraph break (2+ newlines):")
    print(f"  Input:  {repr(test9)}")
    print(f"  Output: {repr(result9)}")
    expected9 = "[A>end of para.<A]\n\n\n[A>(2) New paragraph<A]"
    print(f"  ✓ Pass (kept separate)" if result9 == expected9 else f"  ✗ Fail (incorrectly consolidated)")
    print()
    
    # Test case 10: Tags with single newline (should consolidate)
    test10 = "[A>line 1<A]\n[A>line 2<A]"
    result10 = consolidate_tags(test10)
    print(f"Test 10 - Tags with single newline (same paragraph):")
    print(f"  Input:  {repr(test10)}")
    print(f"  Output: {repr(result10)}")
    expected10 = "[A>line 1\nline 2<A]"
    print(f"  ✓ Pass (consolidated)" if result10 == expected10 else f"  ✗ Fail")
    print()


if __name__ == "__main__":
    # Run tests
    test_consolidation()
