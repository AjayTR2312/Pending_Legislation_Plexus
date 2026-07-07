"""
State Detection from Filename Numerical ID
============================================

Detects the US state from a PDF filename using the Numerical ID field
(3rd underscore-delimited segment) as defined in the NetScan Data Conversion
State Specific Instructions document.

Filename pattern:
    {year1}_{year2}_{numerical_id}_{bill_type}_{chamber}_{bill_number}_{...}.pdf

Example:
    2025_2026_33_3_6_001085_52_4_1_20260602_0.pdf  →  NC (North Carolina, ID=33)
    2026_2026_9_3_2_007030_0_3_1_20260122_0.pdf    →  FL (Florida, ID=9)

Usage:
    from state_id_lookup import detect_state_from_numerical_id

    state_code = detect_state_from_numerical_id("2025_2026_33_3_6_001085_52_4_1_20260602_0.pdf")
    # Returns "NC"
"""

from pathlib import Path

# Mapping: Numerical ID → (state_abbreviation, state_name)
# Source: "NetScan Data Conversion State Specific Instructions.pdf" — Numerical ID column
NUMERICAL_ID_TO_STATE = {
    1:  ("AL", "Alabama"),
    2:  ("AK", "Alaska"),
    3:  ("AZ", "Arizona"),
    4:  ("AR", "Arkansas"),
    5:  ("CA", "California"),
    6:  ("CO", "Colorado"),
    7:  ("CT", "Connecticut"),
    8:  ("DE", "Delaware"),
    9:  ("FL", "Florida"),
    10: ("GA", "Georgia"),
    11: ("HI", "Hawaii"),
    12: ("ID", "Idaho"),
    13: ("IL", "Illinois"),
    14: ("IN", "Indiana"),
    15: ("IA", "Iowa"),
    16: ("KS", "Kansas"),
    17: ("KY", "Kentucky"),
    18: ("LA", "Louisiana"),
    19: ("ME", "Maine"),
    20: ("MD", "Maryland"),
    21: ("MA", "Massachusetts"),
    22: ("MI", "Michigan"),
    23: ("MN", "Minnesota"),
    24: ("MS", "Mississippi"),
    25: ("MO", "Missouri"),
    26: ("MT", "Montana"),
    27: ("NE", "Nebraska"),
    28: ("NV", "Nevada"),
    29: ("NH", "New Hampshire"),
    30: ("NJ", "New Jersey"),
    31: ("NM", "New Mexico"),
    32: ("NY", "New York"),
    33: ("NC", "North Carolina"),
    34: ("ND", "North Dakota"),
    35: ("OH", "Ohio"),
    36: ("OK", "Oklahoma"),
    37: ("OR", "Oregon"),
    38: ("PA", "Pennsylvania"),
    39: ("RI", "Rhode Island"),
    40: ("SC", "South Carolina"),
    41: ("SD", "South Dakota"),
    42: ("TN", "Tennessee"),
    43: ("TX", "Texas"),
    44: ("UT", "Utah"),
    45: ("VT", "Vermont"),
    46: ("VA", "Virginia"),
    47: ("WA", "Washington"),
    48: ("WV", "West Virginia"),
    49: ("WI", "Wisconsin"),
    50: ("WY", "Wyoming"),
    51: ("DC", "District of Columbia"),
}

# Reverse lookup: state abbreviation → numerical ID
STATE_TO_NUMERICAL_ID = {abbr: num_id for num_id, (abbr, _) in NUMERICAL_ID_TO_STATE.items()}


def detect_state_from_numerical_id(filename):
    """
    Detect the US state from a filename using the Numerical ID segment.

    The filename follows the NetScan convention:
        {year1}_{year2}_{numerical_id}_{remaining_fields}...

    The 3rd underscore-delimited segment (index 2) is the state numerical ID.

    Args:
        filename (str): Filename or full path to the PDF/text file.

    Returns:
        str or None: Two-letter state abbreviation (e.g., "NC", "FL"),
                     or None if the numerical ID cannot be parsed or is unknown.
    """
    # Extract just the filename (strip directory path and extension)
    basename = Path(filename).stem if '/' in filename or '\\' in filename else filename
    # Strip known suffixes that get appended to output files
    for suffix in ('_converted', '_pymupdf', '_pymupdf_clean', '_pymupdf_report',
                   '_hybrid_raw', '_gpt_comparison'):
        if basename.endswith(suffix):
            basename = basename[:basename.rfind(suffix)]
            break

    segments = basename.split('_')

    # Need at least 3 segments: year1, year2, numerical_id
    if len(segments) < 3:
        return None

    try:
        numerical_id = int(segments[2])
    except (ValueError, IndexError):
        return None

    entry = NUMERICAL_ID_TO_STATE.get(numerical_id)
    if entry:
        return entry[0]  # Return state abbreviation

    return None


def get_state_name(state_code):
    """
    Get the full state name from a two-letter abbreviation.

    Args:
        state_code (str): Two-letter state abbreviation.

    Returns:
        str or None: Full state name, or None if not found.
    """
    num_id = STATE_TO_NUMERICAL_ID.get(state_code.upper())
    if num_id is None:
        return None
    return NUMERICAL_ID_TO_STATE[num_id][1]


def get_numerical_id(state_code):
    """
    Get the numerical ID for a given state abbreviation.

    Args:
        state_code (str): Two-letter state abbreviation.

    Returns:
        int or None: Numerical ID, or None if not found.
    """
    return STATE_TO_NUMERICAL_ID.get(state_code.upper())


if __name__ == "__main__":
    import sys

    # Self-test with known examples
    test_cases = [
        ("2025_2026_33_3_6_001085_52_4_1_20260602_0.txt", "NC"),
        ("2026_2026_9_3_2_007030_0_3_1_20260122_0.pdf", "FL"),
        ("2025_2026_5_1_2_000123_0_1_0_20260501_0.pdf", "CA"),
        ("2010_2010_14_2_1_000500_0_1_0_20250101_0.pdf", "IN"),
        ("2009_2010_16_1_3_000200_0_2_0_20250301_0.pdf", "KS"),
        ("2025_2026_35_4_1_000900_0_1_0_20260101_0.pdf", "OH"),
        ("/path/to/2025_2026_33_3_6_001085_52_4_1_20260602_0.pdf", "NC"),
        ("2025_2026_33_3_6_001085_52_4_1_20260602_0_converted.txt", "NC"),
        ("invalid_filename.pdf", None),
        ("short.pdf", None),
    ]

    if len(sys.argv) > 1:
        # Detect state for a given filename
        filename = sys.argv[1]
        state = detect_state_from_numerical_id(filename)
        if state:
            name = get_state_name(state)
            num_id = get_numerical_id(state)
            print(f"File:  {filename}")
            print(f"State: {state} ({name})")
            print(f"ID:    {num_id}")
        else:
            print(f"File:  {filename}")
            print(f"State: UNKNOWN (could not detect from filename)")
        sys.exit(0)

    # Run self-tests
    print("State ID Lookup — Self-Test")
    print("=" * 60)
    all_pass = True
    for filename, expected in test_cases:
        result = detect_state_from_numerical_id(filename)
        status = "PASS" if result == expected else "FAIL"
        if result != expected:
            all_pass = False
        print(f"  {status}: {Path(filename).name:<55} → {result or 'None':<4} (expected {expected or 'None'})")

    print()
    print(f"{'All tests passed!' if all_pass else 'SOME TESTS FAILED'}")
    print()
    print(f"Total states mapped: {len(NUMERICAL_ID_TO_STATE)}")
