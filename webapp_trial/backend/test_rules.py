"""Rule engine: each user-facing rule has a positive case and the exemption that must NOT fire."""

import rules


def ids(text, state=None, rule=None):
    found = rules.check_all(text, state)
    return [f["rule"] for f in found if rule is None or f["rule"] == rule]


# ── one paragraph = one tag ──────────────────────────────────────────────────

def test_single_tag_per_paragraph_is_clean():
    text = "[A>(a) A whole new paragraph of text.<A]\n\n[A>(b) Another whole new paragraph.<A]\n"
    assert not [i for i in ids(text) if i.startswith(("para_", "tag_"))]


def test_tag_across_paragraphs_is_flagged():
    text = "[A>(a) First paragraph.\n\n(b) Second paragraph.<A]\n"
    assert "para_tag_crosses_paragraph" in ids(text)


def test_tag_with_line_break_inside_is_flagged_but_not_as_paragraph_merge():
    text = "[A>(a) A paragraph that was\nwrapped inside its tag.<A]\n"
    got = ids(text)
    assert "para_tag_crosses_line" in got
    assert "para_tag_crosses_paragraph" not in got


def test_adjacent_same_type_tags_are_fragments():
    assert "para_tag_fragmented" in ids("[A>FOR AN<A] [A>APPLICANT WHO<A] [A>IS A PHYSICIAN<A]\n")
    assert "para_tag_fragmented" in ids("[D>old<D][D>text<D]\n")


def test_exemptions_for_fragment_rule():
    assert "para_tag_fragmented" not in ids("[D>old<D] [A>new<A]\n")                       # different types
    assert "para_tag_fragmented" not in ids("[A>one<A] plain words [A>two<A]\n")           # plain between
    assert "para_tag_fragmented" not in ids("[A>Description<A]          [A>$150<A]\n")     # table columns
    assert "para_tag_fragmented" not in ids("[A>one paragraph<A]\n\n[A>next paragraph<A]\n")  # paragraph break


def test_table_rows_on_separate_lines_are_exempt():
    text = "[A>At least 5 years<A]          [A>$150<A]\n[A>At least 10 years<A]          [A>$275<A]\n"
    assert "para_tag_fragmented" not in ids(text, "IN")


# ── tag well-formedness ──────────────────────────────────────────────────────

def test_unbalanced_and_mismatched():
    assert "tag_unbalanced" in ids("[A>never closed\n")
    assert "tag_unbalanced" in ids("closed never opened<A]\n")
    assert "tag_unbalanced" in ids("[D>text<A]\n")
    assert "tag_malformed" in ids("[A>text<A>\n")


def test_nested_and_empty():
    assert "tag_nested" in ids("[A>outer [A>inner<A] more<A]\n")
    assert "tag_nested" in ids("[A>outer [D>gone<D] more<A]\n")
    assert "tag_empty" in ids("text [A><A] text\n")


# ── no merging / no splitting of paragraphs ──────────────────────────────────

def test_merged_subsections_flagged():
    text = "(a) The office shall report. (b) The office shall audit.\n"
    assert "para_merged" in ids(text)


def test_label_after_statute_number_is_not_a_merge():
    assert "para_merged" not in ids("Sec. 2. (a) The commissioner shall act.\n")
    assert "para_merged" not in ids("38-2201a. (a) This section applies.\n")


def test_inline_sub_items_in_one_tag_are_flagged():
    text = "[A>\"Status\" means any of: a. A member of the forces. b. A veteran. c. A dependent.<A]\n"
    got = rules.check_all(text, "DE")
    assert any(f["rule"] == "para_merged" for f in got)


def test_section_heading_mid_paragraph():
    assert "para_merged" in ids("The act is effective. Section 2. Another section begins here.\n")


def test_paragraph_split_mid_sentence_and_unjoined_wrap():
    assert "para_split" in ids("The office shall report to the legislature and the\n\ncouncil on all matters.\n")
    assert "para_split" in ids("The office shall report to the\nlegislature on all matters.\n")


def test_split_exemptions():
    assert "para_split" not in ids("(1) the first item;\n\nand (2) the second item.\n")   # prev ends with ;
    assert "para_split" not in ids("Alprazolam............2882\nbarbital............2145\n")


def test_missing_blank_line_between_items_state_dependent():
    text = "(a) First item.\n(b) Second item.\n"
    assert "fmt_spacing" in ids(text, "FL")
    assert "fmt_spacing" not in ids(text, "TX")
    assert "fmt_spacing" not in ids("(1) one\n(2) two\n", "KS")  # KS numbered lists are single-spaced


# ── leftovers / format ───────────────────────────────────────────────────────

def test_artifacts_and_markdown():
    assert "fmt_artifacts" in ids("<<<PAGE_2_START>>>\ntext\n")
    assert "fmt_artifacts" in ids("**bold** text\n")
    assert "fmt_artifacts" in ids("Here is the cleaned text:\n\nSection 1.\n")
    assert "fmt_artifacts" in ids("```plaintext\ntext\n```\n")


def test_asterisk_only_where_the_state_forbids_it():
    assert "fmt_asterisk" in ids("*Ninth* item\n", "KS")
    assert "fmt_asterisk" not in ids("5 * 3 = 15\n", "TX")


def test_em_dash_only_flagged_when_source_has_double_hyphen():
    out = "(1) DEFINITIONS.—For purposes\n"
    got = lambda ref: [f["rule"] for f in rules.check_all(out, "FL", ref)]
    assert "fmt_double_hyphen" in got("(1) DEFINITIONS.--For purposes\n")   # converted
    assert "fmt_double_hyphen" not in got("(1) DEFINITIONS.—For purposes\n")  # printed em dash: fine
    assert "fmt_double_hyphen" not in got(None)                              # no source: cannot tell


def test_florida_header_artifacts():
    text = "HB 385                      2025\nCS/HB 177\n40-00657B-25\nA bill to be entitled\n"
    got = ids(text, "FL", "st_artifacts")
    assert len(got) >= 2


def test_line_numbers_need_a_pattern():
    text = "    1  Section 1. Text here\n    2  more text\n    3  more text again\n"
    assert "fmt_line_numbers" in ids(text)
    assert "fmt_line_numbers" not in ids("    1  Section 1. Text here\n")


# ── state rules ──────────────────────────────────────────────────────────────

def test_kansas_enacting_clause_and_header_must_not_be_tagged():
    t = "[A>Be it enacted by the Legislature of the State of Kansas:<A]\n\n[A>HOUSE BILL No. 2203<A]\n"
    got = ids(t, "KS")
    assert "st_never_tag_clauses" in got
    assert "st_ks_header_signature" in got


def test_california_protected_clause():
    assert "st_never_tag_clauses" in ids("[A>The people of the State of California do enact as follows:<A]\n", "CA")


def test_indiana_tags_only_after_printing_code():
    t = "[A>HOUSE BILL No. 1690<A]\nPRINTING CODE: x\n[A>SECTION 1.<A]\n"
    got = rules.check_all(t, "IN")
    assert sum(f["rule"] == "st_tag_start" for f in got) == 1
    assert any(f["rule"] == "st_in_header" for f in got)


def test_indiana_new_must_be_tagged():
    assert "st_in_new" in ids("PRINTING CODE\nIC 1-1 IS ADDED TO THE INDIANA CODE AS A NEW SECTION TO READ\n", "IN")
    assert "st_in_new" not in ids("PRINTING CODE\nIC 1-1 IS ADDED TO THE INDIANA CODE AS A [A>NEW<A] SECTION\n", "IN")


def test_iowa_new_section_and_first_line():
    got = ids("An act\n\nNEW SECTION. 1. x\n", "IA")
    assert got.count("st_ia_new_section") >= 2
    assert "st_ia_new_section" not in ids("Senate File 173 - Introduced\n\n[A>NEW SECTION<A]. 1. x\n", "IA")


def test_connecticut_brackets_and_labels():
    assert "st_ct_brackets" in ids("The [text here] remained.\n", "CT")
    assert "st_ct_brackets" not in ids("The [D>[text here]<D] remained.\n", "CT")
    assert "st_ct_brackets" in ids("[D>(j)<D] The individual paper ballots\n", "CT")


def test_kentucky_brackets_removed_inside_delete_tags():
    assert "st_bracket_policy" in ids("old [D>[removed text]<D] new\n", "KY")
    assert "st_bracket_policy" not in ids("old [D>removed text<D] new\n", "KY")


def test_colorado_caps_and_footer():
    assert "st_co_caps" in ids("[A>the quick brown fox jumps over the lazy dog<A]\n", "CO")
    assert "st_artifacts" in ids("Shading denotes HOUSE amendment.\n", "CO")


def test_arkansas_header_elements_share_a_line():
    t = "State of Arkansas\n95th General Assembly                                A Bill\nRegular Session, 2025\n"
    assert "st_ar_header" in ids(t, "AR")


def test_arkansas_for_an_act_line_is_not_the_act_number_line():
    t = ("Stricken language would be deleted from and underlined language would be added to present law.\n"
         "HOUSE BILL 1033\nA Bill\n\nState of Arkansas\n95th General Assembly\nRegular Session, 2025\n\n"
         "For An Act To Be Entitled\nAN ACT CONCERNING\n")
    assert "st_ar_header" not in ids(t, "AR")
    out_of_order = "State of Arkansas\nHOUSE BILL 1033\nA Bill\n"
    assert "st_ar_header" in ids(out_of_order, "AR")


def test_page_furniture_in_the_source_is_not_a_defect():
    ex = rules.explain_artifact
    # footer timestamp landed inside a hyphenated word; the output is the correct text
    assert ex("one-11/20/2024 9:45:21 AM year", "one-year")
    # the whole "missing text" is the footer
    assert ex("11/20/2024 9:45:21 AM CEB028", "")
    assert ex("*HAR003*", "") and ex("Page 2 of 4", "")
    assert ex("( 4 )( d)", "(4)(d)") == "only spacing differs"
    assert ex("Capital letters or bold & italic numbers indicate new material added to existing law;", "")
    # real differences are NOT explained away
    assert ex("( 4 )( d)", "(4)(e)") is None
    assert ex("one-11/20/2024 9:45:21 AM year", "two-year") is None
    assert ex("the quick brown fox", "") is None
    assert ex("", "anything") is None
    assert ex("Page 2 of 4 and also a real clause was dropped here", "") is None


def test_catalog_lists_ran_rules_and_manual_items():
    cat = rules.catalog("KS", rules.check_all("text\n", "KS"))
    assert cat["state"] == "KS" and cat["spec"]["name"] == "Kansas"
    assert any(r["id"] == "st_never_tag_clauses" for r in cat["rules"])
    assert isinstance(cat["manual"], list)
    assert rules.catalog(None, [])["rules"]  # global rules still listed with no state
