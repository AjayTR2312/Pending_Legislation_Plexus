# PDF to Text Conversion Prompts

> **Active sections used by Claude_Opus_4.6_Test.py**
>
> - ## PRECISION GUIDANCE  injected at the end of every prompt (all states)
> - ## TEMPLATE 1  fallback prompt when no state is detected
>
> Everything else (delete rules, add rules, visual hints, negative instructions) is
> built dynamically in Python get_conversion_prompt() from 
etscan_state_template.json.

---

## 🎯 PRECISION GUIDANCE (Common to All Templates)

🚨 **TAG FORMAT** - Two tag types only. Deleted/struck text: [D>text<D] ([ + D + > opens, < + D + ] closes). Added/underlined text: [A>text<A] ([ + A + > opens, < + A + ] closes). NEVER use markdown (**, *, ~~). Every opening tag must have a matching closing tag.

**RULE #0: LAYOUT FORMATTING** — Apply these spacing rules mechanically to every page:

**0-A  Statute number after "read as follows"**
When a sentence ends with "is hereby amended to read as follows:" the statute number that immediately follows (e.g., "72-3404.") MUST appear on its own separate line, with a blank line between it and the "read as follows:" line.
Correct:
  Section 1. K.S.A. 2024 Supp. 72-3404 is hereby amended to read as follows:

  72-3404. As used in this act:
Wrong: "...is hereby amended to read as follows: 72-3404. As used in this act:" (merged on one line)

**0-B  Blank line BETWEEN every definition paragraph**
In a definition section, EVERY paragraph that begins with a lettered label — (a), (b), (c) ... (z), (aa), (bb) ... (ff), (gg) — or a numbered sub-item — (1), (2), (3) — must have exactly ONE blank line before it AND one blank line after it. This rule applies to every page of the document without exception.
Correct:
  (k) "State plan" means...

  (l) "Agency" means...

  (m) "Parent" means:

  (1) A natural parent;

  (2) an adoptive parent;
Wrong: running (k), (l), (m), (1), (2) together with no blank lines between them.

**0-C  NO blank line between a label and its OWN body text**
The blank line rule in 0-B applies BETWEEN separate definitions, not inside one. The label and its own body text stay together on consecutive lines with no blank line between them.
Correct: "(m) \"Parent\" means:" immediately followed on the next line by "(1) A natural parent;"
Wrong: inserting a blank line between "(m) \"Parent\" means:" and "(1) A natural parent;"

**0-D  Section and header spacing**
- ONE blank line between major sections and paragraphs.
- Preserve all legislative headers exactly (Session, HOUSE BILL, committee names, sponsors).
- "Section 1." / "Sec. 2." lines get ONE blank line before and after them.

🚨 **RULE #0: ZERO TEXT MODIFICATION** - This is TRANSCRIPTION, not editing. Do NOT add, remove, or change ANY words. Copy EXACTLY character-by-character. Do NOT paraphrase, summarize, or "improve" text. Do NOT fix typos. Do NOT expand abbreviations. You are a photocopier, not an editor.

🚨 **RULE #1: TRANSCRIBE EXACT CASE** - Copy UPPERCASE as UPPERCASE, lowercase as lowercase, Mixed as Mixed. Never "fix" capitalization. Example: PDF shows `FOR AN APPLICANT` → Output `FOR AN APPLICANT` (NOT `For an applicant`).

🚨 **RULE #2: GROUP CONTINUOUS FORMATTING** - If formatting (strikethrough/underline/bold/ALL CAPS) runs continuously across multiple lines, use ONE tag for entire span. Do NOT split tags per line or per word. Example: If "FOR AN APPLICANT WHO IS A PHYSICIAN" is all in ALL CAPS continuously → `[A>FOR AN APPLICANT WHO IS A PHYSICIAN<A]` (NOT separate tags per line).

🚨 **RULE #3: CHECK EVERY WORD** - Examine each word individually for formatting. Don't assume entire paragraphs. Check short words like "A", "or", "the" carefully.

🚨 **RULE #4: SUBSECTION LABELS INDEPENDENT** - Labels like (a), (b), (c) can have DIFFERENT formatting than following text. Check each label separately.

 **RULE #5: PRESERVE EXACT VISUAL SEQUENCE — NEVER REARRANGE** - Output every word in the EXACT left-to-right, top-to-bottom order it appears on the page. NEVER move, regroup, or reorder words to make the markup look cleaner. If plain text word "X" appears between a deletion and an addition on the page, it stays between them in the output — do NOT move "X" to before or after adjacent tagged spans. Each word is tagged (or not) exactly at the position it occupies visually.

 **RULE #6: ADJACENT TAGS  TAG IN DOCUMENT ORDER, NEVER MERGE** - When two differently-formatted spans appear next to each other, tag each one separately and in the exact order they appear in the document. NEVER absorb a character into an adjacent tag of a different type. Each character belongs to exactly one tag based on its own formatting. Order in output must match order in document.

---

---

## 📋 TEMPLATE 1: Default Visual Mode Prompt (No State Specified)

**Use Case:** Generic PDF conversion without state-specific rules

**Template:**

```
🎯 MISSION: Convert PDF page images to clean text with precision strikethrough tagging.

⚠️ CRITICAL TAG FORMAT:
- Strikethrough text: [D>text<D] (NEVER use ~~text~~)
- Opening: [D> | Closing: <D]
- Every [D> must have matching <D]

📋 SYSTEMATIC WORKFLOW:

PHASE 1: SCAN
- [ ] Scan all pages for strikethrough text (horizontal lines through text)
- [ ] Note line numbers, headers, footers to remove
- [ ] Estimate formatting instances: "Approximately X strikethrough spans"

PHASE 2: CLEAN & TRANSCRIBE
- Remove line numbers, headers, footers, page numbers
- ✅ PRESERVE legislative headers at top (Session, HOUSE BILL, committee names, sponsors)
- 🔥 PRESERVE EXACT CASE: Copy UPPERCASE as UPPERCASE, lowercase as lowercase
- 🔥 CONSISTENT SPACING: ONE blank line between sections/paragraphs, NO blank line between subsection label and text

PHASE 3: TAG
- Insert [D>...<D] tags around strikethrough text
- If formatting CONTINUOUS across lines → Use ONE tag (not separate tags per line)
- Check each word individually (especially short words like "A", "or", "the")

🎯 {PRECISION_GUIDANCE}

✅ BEFORE SUBMITTING - VERIFY:
- [ ] NO words added that aren't in PDF?
- [ ] NO words removed that are in PDF?
- [ ] NO words changed/paraphrased?
- [ ] NO text from logos/seals/graphics included?
- [ ] ALL abbreviations kept as-is (K.S.A., etc.)?
- [ ] ALL legal terms unchanged ("pursuant to", "shall", "notwithstanding")?
- [ ] Legislative headers preserved at top?
- [ ] Consistent spacing: ONE blank line between sections, NO extra blank lines?
- [ ] Subsections formatted correctly: "(a) Text" not "(a)\n\nText"?
- [ ] ALL CAPS preserved as ALL CAPS (not converted to lowercase)?
- [ ] Continuous formatting in ONE tag (not split per line)?
- [ ] All tag pairs match ([D> with <D])?
- [ ] No markdown (**, *, ~~) used?

📤 OUTPUT REQUIREMENTS:
- Use regular quotes (not smart quotes)
- Output ONLY converted text (no explanations)

🚨 TOP ERRORS TO AVOID:
❌ Adding/removing/changing words (this is transcription, not editing!)
❌ Paraphrasing: "pursuant to" → "according to" (FORBIDDEN!)
❌ Expanding abbreviations: "K.S.A." → "Kansas Statutes Annotated" (FORBIDDEN!)
❌ Converting ALL CAPS to lowercase: `FOR AN APPLICANT` → `For an applicant` (WRONG!)
❌ Splitting continuous formatting into separate tags per line (use ONE tag)
❌ Using markdown: ~~strikethrough~~ or **bold** (FORBIDDEN)

BEGIN CONVERSION NOW.
```

---
