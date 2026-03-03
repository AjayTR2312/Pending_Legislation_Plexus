# 🎯 PDF to Text Conversion Prompts (Template Library)
 
> **Expert-Crafted Prompt Templates for Legislative Document Conversion with Precision Formatting Detection**
 
---
 
## 📚 Template Overview
 
This document contains reusable prompt templates for PDF-to-text conversion with precision markup tagging. Each template can be customized with state-specific rules.
 
**Available Templates:**
1. Default Visual Mode Prompt (no state customization)
2. State-Specific Visual Mode Template (with placeholders)
3. Default Document Mode Prompt (no state customization)
4. State-Specific Document Mode Template (with placeholders)
5. Reconciliation Prompt Template (3-pass verification)
 
---
 
## ⚠️ CRITICAL: TAG FORMAT RULES (Apply to ALL Templates)
 
### 🚨 THE ONLY ALLOWED TAG FORMATS
 
| Formatting Type | Opening Tag | Closing Tag | Example |
|----------------|-------------|-------------|---------|
| **Deleted/Strikethrough** | `[D>` | `<D]` | `[D>old text<D]` |
| **Added/Underline/Bold/Italic** | `[A>` | `<A]` | `[A>new text<A]` |
 
### ⚠️ CHARACTER-BY-CHARACTER BREAKDOWN
 
**Deleted Text Tags:**
- Opening: `[` (left bracket) + `D` + `>` (greater-than) = `[D>`
- Closing: `<` (less-than) + `D` + `]` (right bracket) = `<D]`
 
**Added Text Tags:**
- Opening: `[` (left bracket) + `A` + `>` (greater-than) = `[A>`
- Closing: `<` (less-than) + `A` + `]` (right bracket) = `<A]`
 
### 🚫 FORBIDDEN FORMATS (NEVER USE IN ANY TEMPLATE)
 
| ❌ WRONG | ✅ CORRECT | Why Wrong? |
|---------|-----------|------------|
| `**bold text**` | `[A>bold text<A]` | Markdown not allowed |
| `*italic text*` | `[A>italic text<A]` | Markdown not allowed |
| `~~strike~~` | `[D>strike<D]` | Markdown not allowed |
| `__underline__` | `[A>underline<A]` | Markdown not allowed |
| `[D>text<D>` | `[D>text<D]` | Wrong closing bracket |
| `[D>text<A]` | `[D>text<D]` | Mismatched tags |
 
---
 
## 🔐 CRITICAL: TEXT PRESERVATION RULES (Apply to ALL Templates)
 
### ⚠️ ABSOLUTE TEXT PRESERVATION REQUIREMENTS
 
**Rule 1: PRESERVE EXACT CAPITALIZATION - NEVER CHANGE CASE**
 
🚨 **CRITICAL: This is a TRANSCRIPTION task, not a formatting task**
- Your ONLY job is to copy text EXACTLY as it appears visually in the PDF
- Do NOT "normalize" capitalization
- Do NOT "fix" capitalization
- Do NOT convert to lowercase for "readability"
- Do NOT assume lowercase is "correct"
 
✅ **CORRECT: Copy character-by-character with EXACT case**
- If PDF shows: `FOR AN APPLICANT WHO IS A PHYSICIAN` → Output: `FOR AN APPLICANT WHO IS A PHYSICIAN`
- If PDF shows: `For a participating physician` → Output: `For a participating physician`
- If PDF shows: `A CARRIER SHALL CONFIRM` → Output: `[A>A CARRIER SHALL CONFIRM<A]`
- If PDF shows: `SECTION 1` → Output: `SECTION 1`
- If PDF shows: `Health & Human Services` → Output: `Health & Human Services`
- If PDF shows: `Be it enacted` → Output: `Be it enacted`
 
❌ **WRONG: Converting case (NEVER DO THIS)**
- PDF shows: `FOR AN APPLICANT` → Output: `For an applicant` ❌ FATAL ERROR
- PDF shows: `A CARRIER SHALL` → Output: `A carrier shall` ❌ FATAL ERROR
- PDF shows: `SECTION 1` → Output: `Section 1` ❌ FATAL ERROR
- PDF shows: `Health-care` → Output: `healthcare` ❌ FATAL ERROR
 
🔥 **EXPLICIT RULE FOR ALL CAPS TEXT:**
- When you see text in ALL CAPITAL LETTERS in the PDF, you MUST output it in ALL CAPITAL LETTERS
- Do NOT convert ALL CAPS to lowercase
- Do NOT convert ALL CAPS to Title Case
- ALL CAPS means EVERY letter is uppercase: A, B, C... Z
- Example: `FOR AN APPLICANT WHO IS A MENTAL HEALTH PROVIDER` (every letter is uppercase, keep it that way)
 
**Rule 2: PRESERVE EXACT SPELLING**
- ✅ Keep hyphenation: "health-care" NOT "healthcare"
- ✅ Keep spacing: "post-master's" NOT "postmaster's"
- ✅ Keep special characters: "&" NOT "and", "§" NOT "section"
 
**Rule 3: PRESERVE EXACT PUNCTUATION**
- ✅ Keep periods, commas, semicolons, colons exactly as shown
- ✅ Keep parentheses: (a), (b), (c) NOT a), b), c)
- ✅ Keep quotation marks (use regular quotes, not smart quotes)
 
**Rule 4: PRESERVE LINE STRUCTURE**
- ✅ Keep paragraph breaks where they appear
- ✅ Keep section breaks where they appear
- ❌ Do NOT reformat or reflow text
 
### 🚨 COMMON CAPITALIZATION ERRORS TO AVOID
 
| PDF Text | ❌ WRONG Output | ✅ CORRECT Output |
|----------|----------------|------------------|
| `SECTION 1` | `Section 1` | `SECTION 1` |
| `A CARRIER SHALL` | `A carrier shall` | `A CARRIER SHALL` |
| `FOR AN APPLICANT WHO IS A PHYSICIAN` | `For an applicant who is a physician` | `FOR AN APPLICANT WHO IS A PHYSICIAN` |
| `FOR A PARTICIPATING PHYSICIAN` | `For a participating physician` | `FOR A PARTICIPATING PHYSICIAN` |
| `THE CARRIER SHALL CONTACT` | `The carrier shall contact` | `THE CARRIER SHALL CONTACT` |
| `Health & Human Services` | `Health & human services` | `Health & Human Services` |
| `Be it enacted` | `Be It Enacted` | `Be it enacted` |
| `10-16-704` | `10-16-704` | `10-16-704` |
 
**🔥 THE #1 MOST COMMON ERROR: Converting ALL CAPS to lowercase**
- PDF shows ALL CAPS → You MUST output ALL CAPS
- Do NOT "normalize" to lowercase
- Do NOT "fix" to Title Case
- TRANSCRIBE EXACTLY - this means copying the EXACT case you see
 
**REMEMBER:** Your job is TEXT TRANSCRIPTION, not text formatting or standardization. Output text EXACTLY as it appears visually, character-by-character, including EXACT capitalization.
 
---
 
## 🎯 PRECISION GUIDANCE (Common to All Templates)
 
TAG FORMAT: Two tag types only. Deleted or struck text uses [D>text<D] where [D> opens and <D] closes. Added or underlined text uses [A>text<A] where [A> opens and <A] closes. Never use markdown formatting such as **, *, or ~~. Every opening tag must have a matching closing tag.
 
RULE 1 - LAYOUT FORMATTING: Apply consistent spacing across all documents. Use one blank line between major sections and paragraphs. Use no blank line between a subsection label such as (a), (b), or (c) and its body text. Preserve legislative headers such as Session, HOUSE BILL, and committee names. Format sections as "Section 1." followed by its text, then one blank line before the next section. Format subsections as "(a) Text here" with no extra blank line between the label and text.
 
RULE 2 - ZERO TEXT MODIFICATION: This is transcription, not editing. Do not add, remove, or change any words. Copy exactly character by character. Do not paraphrase, summarize, or improve text. Do not fix typos. Do not expand abbreviations. Treat yourself as a photocopier, not an editor. If the PDF shows a repeated word such as "Services Services", output "Services Services" — do not silently drop the duplicate. If the PDF shows a missing word or grammatical error, output it exactly as written. Your job is to reflect what is printed, not what should be printed.
 
RULE 3 - TRANSCRIBE EXACT CASE: Copy UPPERCASE as UPPERCASE, lowercase as lowercase, and Mixed Case as Mixed Case. Never normalize capitalization. If the PDF shows "FOR AN APPLICANT", output "FOR AN APPLICANT", not "For an applicant".
 
RULE 4 - GROUP CONTINUOUS FORMATTING: If the same formatting type (strikethrough, underline, bold, or ALL CAPS) runs continuously across multiple lines, use one tag for the entire span. Do not split tags per line or per word. Example: if "FOR AN APPLICANT WHO IS A PHYSICIAN" is all continuously ALL CAPS, output [A>FOR AN APPLICANT WHO IS A PHYSICIAN<A], not one tag per line.
 
RULE 5 - CHECK EVERY WORD: Examine each word individually for formatting. Do not assume an entire paragraph shares the same formatting. Pay special attention to short words like "a", "or", and "the".
 
RULE 6 - SUBSECTION LABELS INDEPENDENT: Labels like (a), (b), and (c) may have different formatting than the text that follows them. Check each label separately.
 
RULE 7 - PRESERVE EXACT VISUAL SEQUENCE: Output every word in the exact left-to-right, top-to-bottom order it appears on the page. Never move, regroup, or reorder words to make the markup look cleaner. If a plain text word appears between a deletion and an addition on the page, keep it between them in the output. Tag each word at the position it occupies visually.
 
RULE 8 - ADJACENT TAGS IN DOCUMENT ORDER: When two differently-formatted spans appear next to each other, tag each one separately in the exact order they appear in the document. Never absorb a character into an adjacent tag of a different type. Each character belongs to exactly one tag based solely on its own visual formatting.
 
RULE 9 - RAW TEXT OUTPUT ONLY: Do NOT wrap your output in code fences, markdown blocks, or any container syntax. Do not start your response with ```plaintext, ```, or any similar marker. Do not end your response with ```. Output raw plain text directly — no wrappers of any kind.
 
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
- 🔥 PRESERVE EXACT CASE: Copy UPPERCASE as UPPERCASE, lowercase as lowercase
 
PHASE 3: TAG
- Insert [D>...<D] tags around strikethrough text
- If formatting CONTINUOUS across lines → Use ONE tag (not separate tags per line)
- Check each word individually (especially short words like "A", "or", "the")
 
🎯 {PRECISION_GUIDANCE}
 
✅ BEFORE SUBMITTING - VERIFY:
- [ ] ALL CAPS preserved as ALL CAPS (not converted to lowercase)?
- [ ] Continuous formatting in ONE tag (not split per line)?
- [ ] All tag pairs match ([D> with <D])?
- [ ] No markdown (**, *, ~~) used?
 
📤 OUTPUT REQUIREMENTS:
- Use regular quotes (not smart quotes)
- Output ONLY converted text (no explanations)
 
🚨 TOP ERRORS TO AVOID:
❌ Converting ALL CAPS to lowercase: `FOR AN APPLICANT` → `For an applicant` (WRONG!)
❌ Splitting continuous formatting into separate tags per line (use ONE tag)
❌ Using markdown: ~~strikethrough~~ or **bold** (FORBIDDEN)
 
BEGIN CONVERSION NOW.
```
 
---
 
## 📋 TEMPLATE 2: State-Specific Visual Mode Prompt
 
**Use Case:** State-specific conversion with custom formatting rules
 
**Placeholders to Fill:**
- `{STATE_NAME}` - Full state name (e.g., "Arkansas", "Florida")
- `{DELETE_INSTRUCTION}` - State-specific deletions (e.g., "Remove ENROLLEDORIGINAL headers")
- `{DELETE_MARKUP_INSTRUCTION}` - Rules for deletion tags
- `{ADD_MARKUP_INSTRUCTION}` - Rules for addition tags
- `{PRECISION_GUIDANCE}` - Insert precision rules from above
 
**Template:**
 
```
🎯 MISSION: Convert {STATE_NAME} legislative bill PDF images to clean text with state-specific markup.
 
⚠️ CRITICAL TAG FORMATS:
- Deleted text: [D>text<D] (NEVER use markdown)
- Added text: [A>text<A] (NEVER use markdown)
- Character-by-character: [D> = [ + D + > | <D] = < + D + ]
 
📋 SYSTEMATIC 3-PHASE WORKFLOW:
 
PHASE 1: SCAN & ANALYZE
- [ ] Identify all formatting types in PDF
- [ ] Note strikethrough instances (line through text)
- [ ] Note underline instances (line beneath text)
- [ ] Note bold/italic instances (if applicable)
- [ ] Note artifacts to remove (line numbers, headers, footers)
- [ ] Estimate: "Approximately X formatting instances"
 
PHASE 2: CLEAN & EXTRACT
{DELETE_INSTRUCTION}
 
- Remove line numbers, headers, footers, page numbers
- 🔥 PRESERVE EXACT CASE: ALL CAPS → ALL CAPS, lowercase → lowercase
 
PHASE 3: TAG FORMATTING
 
{DELETE_MARKUP_INSTRUCTION}
 
{ADD_MARKUP_INSTRUCTION}
 
For EACH formatting span:
1. Identify formatting type
2. Check if continuous across lines → Use ONE tag (not separate per line)
3. Tag and verify content preserved
 
🎯 {PRECISION_GUIDANCE}
 
✅ VERIFICATION:
- [ ] ALL CAPS preserved as ALL CAPS?
- [ ] Continuous formatting in ONE tag?
- [ ] Tag pairs match?
- [ ] No markdown used?
 
📤 OUTPUT REQUIREMENTS:
- Use regular quotes (not smart quotes)
- Output ONLY converted text with markup tags
 
🚨 TOP ERRORS TO AVOID:
❌ Converting ALL CAPS to lowercase
❌ Splitting continuous formatting into separate tags per line
❌ Using markdown: **bold** or *italic* (FORBIDDEN)
 
BEGIN CONVERSION NOW.
```
 
---
 
## 🔧 COMPONENT: Delete Markup Instruction
 
**Use in:** {DELETE_MARKUP_INSTRUCTION} placeholder
 
**Placeholder to Fill:**
- `{DELETE_RULES}` - Comma-separated list (e.g., "text with strikethrough, bracketed text, italic bracketed text")
 
**Template:**
 
```
🗑️ DELETED TEXT MARKUP RULES:
 
Wrap the following types of text with [D> and <D] tags:
{DELETE_RULES}
 
⚠️ TAG FORMAT REMINDER:
- Opening: [D> (bracket + D + greater-than)
- Closing: <D] (less-than + D + bracket)
- Example: [D>deleted text<D]
 
✅ CORRECT Examples:
- Strikethrough text: [D>old provision<D]
- Bracketed deletion: [D>[obsolete section]<D]
- Continuous span: [D>subsection (c) A planning<D]
 
❌ WRONG Examples:
- Using markdown: ~~deleted~~ (FORBIDDEN)
- Wrong bracket: [D>text<D> (must be <D])
- Split continuous: [D>word<D] [D>word<D] (should be one tag)
- Mismatch: [D>text<A] (FATAL ERROR)
 
🔍 DETECTION PROCESS:
1. Examine EACH WORD for deletion formatting
2. If formatting CONTINUOUS, group into ONE tag
3. Check short words (A, or, the) carefully
4. Check subsection labels independently
5. Verify no content dropped after tagging
```
 
---
 
## 🔧 COMPONENT: Add Markup Instruction
 
**Use in:** {ADD_MARKUP_INSTRUCTION} placeholder
 
**Placeholder to Fill:**
- `{ADD_RULES}` - Comma-separated list (e.g., "underlined text, bold text, italic text")
- `{VISUAL_GUIDANCE}` - Insert visual detection hints (see Visual Guidance section below)
 
**Template:**
 
```
➕ ADDED TEXT MARKUP RULES:
 
Wrap the following types of text with [A> and <A] tags:
{ADD_RULES}
 
⚠️ TAG FORMAT REMINDER:
- Opening: [A> (bracket + A + greater-than)
- Closing: <A] (less-than + A + bracket)
- Example: [A>new text<A]
 
🚨 CRITICAL TAG FORMAT REQUIREMENTS:
 
1. ✅ CORRECT: [A>new text<A]
   ❌ WRONG: **new text** (markdown forbidden)
   ❌ WRONG: *new text* (markdown forbidden)
   ❌ WRONG: [A>text<A> (wrong bracket - must be <A])
 
2. 🚫 NEVER USE MARKDOWN:
   - NO asterisks: **, *
   - NO tildes: ~~
   - NO underscores: __
   - ONLY use: [A>...<A] and [D>...<D]
 
3. 📏 PLAIN TEXT FOR UNLISTED FORMATTING:
   - If formatting type NOT in {ADD_RULES}, output as PLAIN TEXT
   - If bold NOT listed: Do NOT tag bold text (output as plain)
   - If underline NOT listed: Do NOT tag underlined text (output as plain)
   - If italic NOT listed: Do NOT tag italic text (output as plain)
 
🔍 STRICT VISUAL DETECTION RULES:
 
Rule 1: ONLY TAG SPECIFIED TYPES
- Tag ONLY formatting types listed in {ADD_RULES}
- Do NOT tag other formatting types
- Example: If only "underlined text" listed, do NOT tag bold or italic
 
Rule 2: USE VISUAL APPEARANCE ONLY
- Examine actual visual rendering in image
- Compare to surrounding body text on same page
- If looks same as body text → do NOT tag
 
Rule 3: NO LEGISLATIVE ASSUMPTIONS
- Do NOT use knowledge of bill formatting conventions
- Do NOT assume section headers are always bold
- Do NOT infer formatting from document structure
- IGNORE what you know about how bills typically work
 
Rule 4: VISUAL DETECTION SPECIFICS
{VISUAL_GUIDANCE}
 
✅ CORRECT Tagging Examples:
- Underlined text: [A>Title 47<A]
- Bold heading: [A>Section 1. Definitions<A]
- Italic reference: [A>See subsection (b)<A]
- Multiple formats: [A>bold and underlined<A]
 
❌ WRONG Tagging Examples:
- Using markdown: **bold** or *italic*
- Tagging unlisted formats: [A>bold<A] when bold not in {ADD_RULES}
- Guessing based on position: [A>Section 1<A] without visual confirmation
- Wrong closing: [A>text<A> (must be <A])
```
 
---
 
## 🔧 COMPONENT: Visual Guidance Hints
 
**Use in:** {VISUAL_GUIDANCE} placeholder
 
**Select relevant hints based on formatting types:**
 
### 👁️ For Italic Detection
 
```
ITALIC VISUAL CHARACTERISTICS:
- Characters slanted/tilted to the RIGHT (~15° angle)
- Regular text: Upright vertical strokes |Text|
- Italic text: Angled vertical strokes /Text/
 
✅ CORRECT Detection:
- If text has SAME upright style as body → NOT italic
- If text slanted right → IS italic
 
❌ COMMON MISTAKES:
- Tagging ALL CAPS as italic (ALL CAPS ≠ italic)
- Tagging bold as italic (thick strokes ≠ slanted)
- Tagging text that looks "different" but isn't slanted
 
⚠️ CRITICAL REMINDER:
If "italic text" is NOT listed in {ADD_RULES}, do NOT tag ANY italic text you see.
```
 
### 👁️ For Underline Detection
 
```
UNDERLINE VISUAL CHARACTERISTICS:
- Horizontal line drawn BENEATH text baseline
- Line runs UNDER characters, not through them
- Clearly separated from text above
 
Visual Comparison:
- Underline: Text_____ (line BENEATH)
- Strikethrough: T̶e̶x̶t̶ (line THROUGH middle)
- Bold: Text (thicker strokes, NO line)
 
✅ CORRECT Detection:
- Visible horizontal line beneath text → IS underlined
- No line beneath (even if text looks different) → NOT underlined
 
❌ COMMON MISTAKES:
- Confusing bold with underline (bold has NO line beneath)
- Confusing strikethrough with underline (strikethrough goes THROUGH)
- Tagging bold text as underlined when no line exists
 
🚨 CRITICAL REMINDER:
Bold text is NOT underlined unless there's a visible line beneath it!
 
⚠️ CRITICAL REMINDER:
If "underlined text" is NOT listed in {ADD_RULES}, do NOT tag ANY underlined text you see.
```
 
### 👁️ For Bold Detection
 
```
BOLD VISUAL CHARACTERISTICS:
- Visually thicker/heavier character strokes
- Compare to surrounding body text
- Same height, but wider/darker appearance
 
Visual Test:
- Regular: Text (normal stroke width)
- Bold: Text (thicker stroke width)
 
✅ CORRECT Detection:
- Strokes visibly thicker than body text → IS bold
- Same thickness as body text → NOT bold
 
❌ COMMON MISTAKES:
- Tagging ALL CAPS as bold (uppercase ≠ bold)
- Tagging underlined text as bold
- Using markdown: **bold** instead of [A>bold<A]
 
⚠️ CRITICAL REMINDER:
If "bold text" is NOT listed in {ADD_RULES}, do NOT tag ANY bold text you see, even if it appears thicker than body text.
```
 
### 👁️ For ALL CAPS Detection
 
```
ALL CAPS VISUAL CHARACTERISTICS:
- Every letter is UPPERCASE
- Distinct from normal mixed-case text
- Compare: "Section" vs "SECTION"
 
✅ CORRECT Detection:
- All letters uppercase → IS ALL CAPS
- Mixed case → NOT ALL CAPS
 
❌ COMMON MISTAKES:
- Confusing ALL CAPS with bold
- Confusing ALL CAPS with headings
```
 
### 👁️ For Brackets Detection
 
```
BRACKETS VISUAL CHARACTERISTICS:
- Text enclosed within bracket characters [ ]
- Look for opening [ and closing ] brackets
 
✅ CORRECT Detection:
- [text] → IS bracketed
 
❌ COMMON MISTAKES:
- Confusing with other delimiters: ( ), { }, < >
```
 
---
 
## 📋 TEMPLATE 3: Default Document Mode Prompt (No State Specified)
 
**Use Case:** Generic PDF conversion using document mode (not image-based)
 
**Template:**
 
```
🎯 MISSION: Convert PDF document to clean text with precision strikethrough tagging.
 
⚠️ CRITICAL TAG FORMAT:
- Strikethrough text: [D>text<D] (NEVER use ~~text~~)
- Opening: [D> | Closing: <D]
- Every [D> must have matching <D]
 
📋 SYSTEMATIC WORKFLOW:
 
PHASE 1: SCAN
- [ ] Identify strikethrough instances
- [ ] Note artifacts to remove (line numbers, headers, footers, page numbers)
 
PHASE 2: CLEAN
- [ ] Remove line numbers
- [ ] Remove headers/footers
- [ ] Remove page numbers
- [ ] Verify: Artifacts removed, content preserved
 
PHASE 3: TAG
For EACH strikethrough instance:
1. Read sentence context (20+ words before/after)
2. Insert [D> and <D] tags around struck text
3. Verify content preservation
 
🎯 PRECISION RULES:
{PRECISION_GUIDANCE}
 
✅ VERIFICATION:
- [ ] All strikethrough tagged?
- [ ] No markdown used?
- [ ] Tag pairs match?
- [ ] Artifacts removed?
- [ ] Content preserved?
 
📤 OUTPUT:
- Use regular quotes, not smart quotes
- Output ONLY converted text (no explanations)
 
BEGIN CONVERSION NOW.
```
 
---
 
## 📋 TEMPLATE 4: State-Specific Document Mode Prompt
 
**Use Case:** State-specific conversion using document mode
 
**Placeholders:** Same as Template 2 (State-Specific Visual Mode)
 
**Template:**
 
```
🎯 MISSION: Convert {STATE_NAME} legislative bill PDF to clean text with state-specific markup.
 
⚠️ CRITICAL TAG FORMATS:
- Deleted text: [D>text<D]
- Added text: [A>text<A]
- NEVER use markdown (**, *, ~~, __)
 
📋 SYSTEMATIC 3-PHASE WORKFLOW:
 
PHASE 1: SCAN
- [ ] Identify formatting types present
- [ ] Note artifacts to remove
 
PHASE 2: CLEAN
{DELETE_INSTRUCTION}
- [ ] Remove line numbers, headers, footers, page numbers
- [ ] Verify artifacts removed
 
PHASE 3: TAG
{DELETE_MARKUP_INSTRUCTION}
 
{ADD_MARKUP_INSTRUCTION}
 
⚠️ SPECIAL ATTENTION:
Subsection labels like (a), (b), (c) may be independently struck through even when surrounding text is not. Check EACH label individually.
 
🎯 PRECISION RULES:
{PRECISION_GUIDANCE}
 
✅ VERIFICATION:
- [ ] All formatting tagged correctly?
- [ ] No markdown used?
- [ ] Tag pairs match?
- [ ] Artifacts removed?
- [ ] Content preserved?
 
📤 OUTPUT:
- Use regular quotes, not smart quotes
- Output ONLY converted text with markup tags (no explanations)
 
BEGIN CONVERSION NOW.
```
 
---
 
## 🔧 COMPONENT: Document Mode Add Markup Instruction
 
**Use in:** {ADD_MARKUP_INSTRUCTION} placeholder (Document Mode)
 
**Simplified version for document mode (less visual emphasis)**
 
**Template:**
 
```
➕ ADDED TEXT MARKUP: Wrap the following types of text with [A> and <A] tags:
{ADD_RULES}
 
🚨 CRITICAL REQUIREMENTS:
1. ✅ Use [A>text<A] format ONLY
2. ❌ NEVER use markdown: **, *, ~~, __
3. 📏 Unlisted formatting → Output as PLAIN TEXT
4. 🔍 Tag ONLY specified types in {ADD_RULES}
 
VISUAL DETECTION:
{VISUAL_GUIDANCE}
 
⚠️ STRICT RULES:
- Tag only VISUALLY rendered formatting
- Do NOT infer or guess formatting
- Do NOT tag types not in {ADD_RULES}
- If bold NOT listed → Do NOT tag bold text
- If underline NOT listed → Do NOT tag underlined text
- If italic NOT listed → Do NOT tag italic text
```
 
---
 
## 🔧 COMPONENT: Document Mode Visual Guidance (Simplified)
 
**Use in:** {VISUAL_GUIDANCE} placeholder (Document Mode)
 
**Simplified hints for document mode:**
 
```
VISUAL DETECTION HINTS:
 
Italic: Text slanted/tilted right, not upright
⚠️ If NOT in {ADD_RULES}, do NOT tag italic
 
Underline: Visible line BENEATH text (not through it)
⚠️ Bold text is NOT underlined
⚠️ If NOT in {ADD_RULES}, do NOT tag underline
 
Bold: Thicker/heavier strokes than body text
⚠️ If NOT in {ADD_RULES}, do NOT tag bold
 
ALL CAPS: Every letter uppercase
 
Brackets: Text enclosed in [ ]
```
 
---
 
## 📋 TEMPLATE 5: Reconciliation Prompt (3-Pass Verification)
 
**Use Case:** Compare visual mode vs document mode outputs to catch missed formatting
 
**Placeholders to Fill:**
- `{VISUAL_TEXT}` - Output from visual/image conversion
- `{DOC_TEXT}` - Output from document mode conversion
 
**Template:**
 
```
🎯 MISSION: Reconcile two PDF conversion outputs to produce the most accurate result.
 
📊 INPUT DATA:
 
VERSION A (Visual/Image Analysis - generally more accurate for formatting):
---
{VISUAL_TEXT}
---
 
VERSION B (Document Text Extraction - may catch missed items):
---
{DOC_TEXT}
---
 
📋 RECONCILIATION RULES:
 
Rule 1: VERSION A is BASE
- Use VERSION A as foundation
- Its formatting and structure are generally more accurate
 
Rule 2: LINE-BY-LINE COMPARISON
- Compare [D> and <D] tag placement between versions
- Compare [A> and <A] tag placement between versions
- Check each line, each tag
 
Rule 3: ADD MISSED TAGS FROM VERSION B
- If VERSION B has [D> tag that VERSION A missed → ADD it
- If VERSION B has [A> tag that VERSION A missed → ADD it
- VERSION B may catch items visual mode missed
 
Rule 4: KEEP VERSION A TAGS
- If VERSION A has tag that VERSION B lacks → KEEP it
- Visual mode is more reliable for formatting detection
 
Rule 5: SUBSECTION LABELS CRITICAL
- Labels like (a), (b), (c) are easily missed
- If EITHER version tags them → Include in output
 
Rule 6: CONTENT PRESERVATION
- Do NOT change text content
- ONLY adjust tag placement
- Verify no words added/removed
 
Rule 7: TAG PAIR VERIFICATION
- Ensure every [D> has matching <D]
- Ensure every [A> has matching <A]
- Fix any mismatches
 
✅ VERIFICATION CHECKLIST:
 
- [ ] Used VERSION A as base text?
- [ ] Compared tag placement line-by-line?
- [ ] Added missed tags from VERSION B?
- [ ] Kept VERSION A's unique tags?
- [ ] Checked all subsection labels?
- [ ] Verified no content changes (only tag adjustments)?
- [ ] Verified all tag pairs match?
- [ ] Count [D> = Count <D]?
- [ ] Count [A> = Count <A]?
 
📤 OUTPUT REQUIREMENTS:
- Output ONLY the merged text
- No explanations
- No commentary
- No preambles
- Just the final reconciled text
 
BEGIN RECONCILIATION NOW.
```
 
---
 
## 🎯 USAGE GUIDE
 
### How to Use These Templates:
 
1. **Choose the right template:**
   - No state rules? → Use Default templates (1 or 3)
   - State-specific rules? → Use State-Specific templates (2 or 4)
   - Need verification? → Add Template 5 (Reconciliation)
 
2. **Fill in placeholders:**
   - Replace {STATE_NAME}, {DELETE_RULES}, {ADD_RULES}, etc.
   - Insert {PRECISION_GUIDANCE} from the precision rules section
   - Insert {VISUAL_GUIDANCE} from the visual guidance components
 
3. **Copy complete prompt:**
   - Once placeholders filled, copy entire template
   - Use as system prompt or user instruction
   - Model will follow the structured workflow
 
### Example State-Specific Customization:
 
**For Arkansas:**
```
{STATE_NAME} = "Arkansas"
{DELETE_RULES} = "text with strikethrough"
{ADD_RULES} = "underlined text"
{DELETE_INSTRUCTION} = "Remove line numbers and page headers"
```
 
**For District of Columbia:**
```
{STATE_NAME} = "District of Columbia"  
{DELETE_RULES} = "text with strikethrough"
{ADD_RULES} = "underlined text"
{DELETE_INSTRUCTION} = "Remove ENROLLEDORIGINAL headers (case-insensitive), line numbers, and reposition right-margin NOTES to separate lines above"
```
 
---
 
## 🚨 CRITICAL SUCCESS FACTORS
 
**All templates share these requirements:**
 
1. ✅ **Perfect Tag Format** - Use [A>...<A] and [D>...<D] ONLY
2. ✅ **No Markdown** - Never use **, *, ~~, __
3. ✅ **Content Preservation** - Never drop legislative text
4. ✅ **Tag Pair Matching** - Every opening tag has closing tag
5. ✅ **Systematic Workflow** - Follow 3-phase process
6. ✅ **Verification** - Complete checklist before submitting
7. ✅ **No Commentary** - Output only converted text
 
**Common Failures Across All Templates:**
[!IMPORTANT]
## 🛡️ FINAL VERIFICATION CHECKLIST (Add to All Prompts)
 
Before submitting your output, you MUST verify:
- [ ] ALL CAPS text is output in uppercase EXACTLY as shown in PDF
- [ ] Tags ([A>...<A], [D>...<D]) wrap the ENTIRE continuous span (no fragments)
- [ ] No tag fragments (e.g., [A>FOR<A] [A>AN<A] [A>APPLICANT<A])—all continuous formatted text is grouped into ONE tag
- [ ] Tag pairs match ([A> with <A], [D> with <D])
- [ ] No markdown formatting used
- [ ] Content preserved exactly (no dropped text)
- [ ] Capitalization, spelling, punctuation, and line structure preserved
- [ ] Short words and subsection labels checked individually
- [ ] Output only converted text (no explanations)
 
---
 
---
 
**END OF TEMPLATE LIBRARY**