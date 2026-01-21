"""
Tag-Then-Clean Pipeline
======================
Two-step process for accurate tagging:
1. Tag raw pdfplumber text using summary (Para/Run locations match raw structure)
2. Clean the tagged text (preserving tags)

This approach is more accurate because summary locations align with raw text structure.
"""

import anthropic
import os
import logging
import requests
import json
import certifi
import re
from datetime import datetime
from pathlib import Path

# Configuration - Thomson Reuters internal authentication
WORKSPACE_ID = "AgentTraininJaH4"
TOKEN_URL = "https://aiplatform.gcs.int.thomsonreuters.com/v1/anthropic/token"

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

def load_file(file_path):
    """Load content from a file."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        logging.error(f"Error loading file {file_path}: {e}")
        raise

def save_file(content, file_path):
    """Save content to a file."""
    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        logging.info(f"Saved output to: {file_path}")
    except Exception as e:
        logging.error(f"Error saving file {file_path}: {e}")
        raise

def get_anthropic_client():
    """Get Anthropic API client with credentials from Thomson Reuters platform."""
    try:
        payload = {"workspace_id": WORKSPACE_ID}
        resp = requests.post(TOKEN_URL, headers=None, json=payload, verify=certifi.where())
        credentials = json.loads(resp.content)
        
        if 'anthropic_api_key' in credentials:
            logging.info("✓ Successfully obtained Anthropic API key")
            return anthropic.Anthropic(api_key=credentials["anthropic_api_key"])
        else:
            logging.error(f"No Anthropic API key found: {credentials}")
            raise ValueError("No Anthropic API key in credentials")
    except Exception as e:
        logging.error(f"Failed to initialize Anthropic client: {e}")
        raise

def load_verify_prompt(state_code):
    """Load state-specific verification prompt."""
    project_root = Path(__file__).parent.parent
    prompt_file = project_root / "data" / "Verify_Prompts" / f"{state_code}_Prompt.txt"
    
    if prompt_file.exists():
        with open(prompt_file, 'r', encoding='utf-8') as f:
            return f.read()
    else:
        raise FileNotFoundError(f"Verify prompt not found: {prompt_file}")

def load_clean_prompt(state_code):
    """Load state-specific cleaning prompt."""
    project_root = Path(__file__).parent.parent
    prompt_file = project_root / "data" / "Prompts" / f"{state_code}_Clean_Prompt.txt"
    
    if prompt_file.exists():
        with open(prompt_file, 'r', encoding='utf-8') as f:
            return f.read()
    else:
        raise FileNotFoundError(f"Clean prompt not found: {prompt_file}")

def call_claude_with_system_prompt(client, system_prompt, user_prompt, max_tokens=16000, model="claude-opus-4-20250514"):
    """
    Call Claude API with system/user prompt split.
    
    Args:
        client: Anthropic client
        system_prompt: System-level instructions (role, general rules)
        user_prompt: User-level content (state-specific details + document)
        max_tokens: Maximum tokens for response
        model: Claude model to use (default: claude-opus-4-20250514)
        
    Returns:
        str: Claude's response
    """
    try:
        logging.info(f"🔄 Calling Claude API ({model})...")
        
        # Use streaming for long operations (matching pdf_to_text_llm.py)
        response_text = ""
        with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            temperature=0,  # Deterministic output for consistent tagging
            top_p=1.0,  # Use default top_p with temperature=0
            system=system_prompt,  # System prompt for role/general rules
            messages=[{
                "role": "user",
                "content": user_prompt  # User prompt for specific task + document
            }]
        ) as stream:
            for text in stream.text_stream:
                response_text += text
        
        logging.info(f"✓ Received response ({len(response_text)} characters)")
        return response_text
        
    except Exception as e:
        logging.error(f"❌ Error calling Claude API: {e}")
        raise

def step1_tag_raw_text(client, raw_text, summary_content, verify_prompt_template):
    """
    STEP 1: Tag raw text using summary (Para/Run locations match raw structure).
    
    Args:
        client: Anthropic client
        raw_text: Raw pdfplumber text (with page markers, line numbers)
        summary_content: Summary file with Para/Run locations
        verify_prompt_template: State-specific verify prompt template
        
    Returns:
        str: Tagged raw text
    """
    logging.info("\n" + "="*60)
    logging.info("STEP 1: TAGGING RAW TEXT")
    logging.info("="*60)
    
    # Replace placeholders in verify prompt
    user_prompt = verify_prompt_template.replace(
        "{SUMMARY_CONTENT}", summary_content
    ).replace(
        "{OUTPUT_TEXT_CONTENT}", raw_text
    )
    
    # Use strong system prompt to emphasize content preservation
    system_prompt = """You are a legislative document tag verification expert with perfect accuracy.

CRITICAL DIRECTIVE: Your ONLY task is to add formatting tags [A>...<A] and [D>...<D] to existing text. 
You MUST preserve every single word from the input. Tags are markup annotations - they do NOT replace text.

EXACT TAG PLACEMENT RULE:
- Add tags at the EXACT locations specified in the summary - no more, no less
- DO NOT merge or consolidate separate formatting locations
- If the summary shows two separate deletions with untagged text between them, you MUST maintain that separation
- Example: If summary indicates "[D>text1<D] normaltext [D>text2<D]", do NOT output "[D>text1 normaltext text2<D]"
- Tag boundaries must match Para/Run locations in the summary PRECISELY
- Even if two tags are close together, keep them separate if the summary shows them as separate

Absolute rule: Input word count = Output word count (excluding tag markers).

If you delete even a single word, you have FAILED the task.
If you merge separate tags that should be distinct, you have FAILED the task."""
    
    tagged_text = call_claude_with_system_prompt(
        client=client,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_tokens=16000,
        model="claude-sonnet-4-20250514"  # Sonnet 4.5 for tagging
    )
    
    logging.info(f"✓ Tagged text: {len(tagged_text)} characters")
    
    # Post-process to consolidate adjacent tags of the same type
    tagged_text = consolidate_adjacent_tags(tagged_text)
    
    return tagged_text

def consolidate_adjacent_tags(text):
    """
    Consolidate adjacent tags of the same type that are only separated by spaces.
    
    Converts:
    [D>prior<D] [D>to<D] [D>March<D] → [D>prior to March<D]
    [A>word1<A] [A>word2<A] → [A>word1 word2<A]
    [A>(9)<A]  [A>"Food<A] → [A>(9) "Food<A]  (handles multiple spaces)
    
    Args:
        text: Text with potentially fragmented tags
        
    Returns:
        str: Text with adjacent tags consolidated
    """
    logging.info("🔧 Post-processing: Consolidating adjacent tags...")
    
    # Consolidate [A>...<A] tags that are adjacent (with or without spaces)
    # Patterns: 
    #   [A>text<A][A>text<A] → [A>text text<A]
    #   [A>text<A] [A>text<A] → [A>text text<A]
    #   [A>text<A]  [A>text<A] → [A>text text<A] (multiple spaces)
    while True:
        original = text
        # Handle tags with no space between them
        text = re.sub(r'\[A>(.*?)<A\]\[A>(.*?)<A\]', r'[A>\1 \2<A]', text, flags=re.DOTALL)
        # Handle tags with one or more whitespace characters (spaces, tabs, newlines)
        # Use re.DOTALL to handle newlines within tag content
        text = re.sub(r'\[A>(.*?)<A\]\s+\[A>(.*?)<A\]', r'[A>\1 \2<A]', text, flags=re.DOTALL)
        if text == original:
            break
    
    # Consolidate [D>...<D] tags that are adjacent (with or without spaces)
    # Patterns: 
    #   [D>text<D][D>text<D] → [D>text text<D]
    #   [D>text<D] [D>text<D] → [D>text text<D]
    #   [D>text<D]  [D>text<D] → [D>text text<D] (multiple spaces)
    while True:
        original = text
        # Handle tags with no space between them
        text = re.sub(r'\[D>(.*?)<D\]\[D>(.*?)<D\]', r'[D>\1 \2<D]', text, flags=re.DOTALL)
        # Handle tags with one or more whitespace characters (spaces, tabs, newlines)
        # Use re.DOTALL to handle newlines within tag content
        text = re.sub(r'\[D>(.*?)<D\]\s+\[D>(.*?)<D\]', r'[D>\1 \2<D]', text, flags=re.DOTALL)
        if text == original:
            break
    
    logging.info("✓ Adjacent tags consolidated")
    return text

def step2_clean_tagged_text(client, tagged_text, clean_prompt):
    """
    STEP 2: Clean tagged text (remove artifacts, preserve tags).
    
    Args:
        client: Anthropic client
        tagged_text: Tagged raw text from Step 1
        clean_prompt: State-specific cleaning instructions
        
    Returns:
        str: Clean tagged text
    """
    logging.info("\n" + "="*60)
    logging.info("STEP 2: CLEANING TAGGED TEXT")
    logging.info("="*60)
    
    # Build user prompt with cleaning instructions + tagged text
    user_prompt = f"""{clean_prompt}

CRITICAL: PRESERVE ALL TAGS [A>...<A] and [D>...<D] during cleaning. Only remove artifacts listed above.

---RAW TEXT TO CLEAN---

{tagged_text}"""
    
    # Use system/user split with STRONG preservation directive
    system_prompt = """You are a deterministic cleaning system for legislative document PDF artifacts. You MUST produce CONSISTENT, IDENTICAL output for the same input every time.

CRITICAL OUTPUT FORMAT:
- Return ONLY the cleaned legislative text - NO preamble, NO explanation, NO conversational text
- DO NOT start with phrases like "I'll clean this document..." or "Here is the cleaned text..."
- Your first line of output MUST be the first line of the cleaned legislative document
- DO NOT add ANY text that wasn't in the input

ABSOLUTE CRITICAL RULES (NO EXCEPTIONS):
1. DO NOT REMOVE ANY TAGS: Every [A>...<A] and [D>...<D] tag MUST appear in your output exactly as in input
2. DO NOT REMOVE ANY LEGISLATIVE CONTENT: Every word of actual legislative text MUST be preserved
3. DO NOT ADD ANY TEXT: No explanations, no conversational text, no preamble, no commentary
4. DO NOT CREATE LINE BREAKS: Keep paragraphs together, do NOT split them into multiple lines
5. DO NOT SPLIT TAGS: If input has [A>entire paragraph text<A], output MUST have the same single tag pair
6. NORMALIZE SPACING: Remove excessive spaces, centered alignment, and layout formatting
7. ONLY REMOVE: PDF artifacts (see specific list below) - NOTHING ELSE

CONSISTENCY REQUIREMENT:
- Apply the same cleaning logic to EVERY chunk/document
- Bill headers like "HB 2207" or "SB 1234" MUST ALWAYS be removed when they appear standalone mid-document
- Page markers MUST ALWAYS be removed in every single occurrence
- If a pattern appears in multiple chunks, handle it IDENTICALLY in all chunks
- Your output must be 100% deterministic and reproducible

FORMATTING CONSISTENCY ACROSS CHUNKS (CRITICAL):
- Use the SAME formatting rules for ALL chunks - chunk 1, chunk 2, chunk 3, etc. must have IDENTICAL formatting style
- Section definitions (a), (b), (c): ALWAYS add blank line after each in ALL chunks
- Numbered lists (1), (2), (3): ALWAYS single-spaced (no blank lines) in ALL chunks
- Subsection headings with colons: ALWAYS blank line before, no blank after in ALL chunks
- Paragraph spacing: Apply SAME spacing rules to ALL chunks
- If chunk 1 has blank line after "(a) definition", then chunk 2 MUST also have blank line after "(a) definition"
- The merged output should look like ONE continuous document, not multiple different styles pasted together
- When in doubt about formatting, follow Kansas legislative document conventions consistently

MUST REMOVE THESE ARTIFACTS (CONSISTENTLY IN EVERY CHUNK):
- Page markers: <<<PAGE_N_START>>> and <<<PAGE_N_END>>> (ALWAYS remove these, no exceptions)
- Repeated bill headers: Bill identifiers appearing mid-document (e.g., "HB 2207", "SB 1234" standalone on a line)
  * CRITICAL: Even if header appears between tagged content, REMOVE IT and join the tags
  * Example: [A>text<A] HB 2207 [A>more text<A] → [A>text more text<A]
- Artificial line breaks within paragraphs (text wrapped at ~80 characters) - join into continuous paragraphs
- Excessive spacing: Multiple spaces → single space (except necessary readability spacing)
- Centered/aligned text formatting: Convert to left-aligned, normalized spacing
- Lines with only spaces or whitespace
- Other artifacts specified in the user prompt (footers, line numbers, etc.)

CONFLICT RESOLUTION:
When a bill header appears between tagged content, ALWAYS:
1. Remove the header (it's a PDF artifact)
2. Join the surrounding tags if they're the same type
3. Preserve all legislative text, just remove the header artifact

CRITICAL: Your output MUST be consistent across all chunks. Same input = same output, ALWAYS.
If you produce different results for similar inputs, you have FAILED.
If you add ANY conversational text or explanations, you have FAILED."""
    
    clean_text = call_claude_with_system_prompt(
        client=client,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_tokens=16000,
        model="claude-opus-4-20250514"  # Opus 4 for cleaning
    )
    
    logging.info(f"✓ Cleaned text: {len(clean_text)} characters")
    
    # Post-process to deconsolidate tags at paragraph boundaries
    clean_text = deconsolidate_tags(clean_text)
    
    return clean_text

def deconsolidate_tags(text):
    """
    Split consolidated tags at paragraph boundaries.
    
    Converts:
    [A>para1\n\npara2<A] → [A>para1<A]\n\n[A>para2<A]
    
    Args:
        text: Text with potentially consolidated tags
        
    Returns:
        str: Text with tags split at paragraph boundaries
    """
    logging.info("🔧 Post-processing: Deconsolidating tags at paragraph boundaries...")
    
    # Pattern to find tags that span multiple paragraphs
    # Matches: [A>text\n\nmore text<A] or [D>text\n\nmore text<D]
    
    def split_a_tag(match):
        """Split an [A>...<A] tag at double newlines (paragraph breaks)."""
        content = match.group(1)
        
        # Split content at double newlines (paragraph boundaries)
        paragraphs = content.split('\n\n')
        
        # If only one paragraph, return as-is
        if len(paragraphs) == 1:
            return match.group(0)
        
        # Create separate tags for each paragraph
        result = []
        for para in paragraphs:
            para = para.strip()
            if para:  # Only add non-empty paragraphs
                result.append(f"[A>{para}<A]")
        
        # Join with double newlines to preserve paragraph structure
        return '\n\n'.join(result)
    
    def split_d_tag(match):
        """Split a [D>...<D] tag at double newlines (paragraph breaks)."""
        content = match.group(1)
        
        # Split content at double newlines (paragraph boundaries)
        paragraphs = content.split('\n\n')
        
        # If only one paragraph, return as-is
        if len(paragraphs) == 1:
            return match.group(0)
        
        # Create separate tags for each paragraph
        result = []
        for para in paragraphs:
            para = para.strip()
            if para:  # Only add non-empty paragraphs
                result.append(f"[D>{para}<D]")
        
        # Join with double newlines to preserve paragraph structure
        return '\n\n'.join(result)
    
    # Process [A>...<A] tags
    original_length = len(text)
    text = re.sub(r'\[A>(.*?)<A\]', split_a_tag, text, flags=re.DOTALL)
    
    # Process [D>...<D] tags
    text = re.sub(r'\[D>(.*?)<D\]', split_d_tag, text, flags=re.DOTALL)
    
    if len(text) != original_length:
        logging.info("✓ Deconsolidated tags at paragraph boundaries")
    else:
        logging.info("✓ No tag consolidation found")
    
    return text

def tag_then_clean_pipeline(
    raw_text_path,
    summary_path,
    state_code,
    output_dir,
    pdf_path=None
):
    """
    Main pipeline: Tag raw text first, then clean it.
    
    Args:
        raw_text_path: Path to pdfplumber_raw.txt
        summary_path: Path to summary file
        state_code: State code (e.g., 'KS')
        output_dir: Directory to save outputs
        pdf_path: Optional path to original PDF (for output naming)
        
    Returns:
        dict: Paths to output files
    """
    logging.info("\n" + "="*60)
    logging.info("TAG-THEN-CLEAN PIPELINE")
    logging.info("="*60)
    logging.info(f"State: {state_code}")
    logging.info(f"Raw text: {raw_text_path}")
    logging.info(f"Summary: {summary_path}")
    
    # Initialize Claude client using TR authentication
    client = get_anthropic_client()
    
    # Load inputs
    logging.info("\n📂 Loading files...")
    raw_text = load_file(raw_text_path)
    summary_content = load_file(summary_path)
    verify_prompt = load_verify_prompt(state_code)
    clean_prompt = load_clean_prompt(state_code)
    
    logging.info(f"✓ Raw text: {len(raw_text):,} characters")
    logging.info(f"✓ Summary: {len(summary_content):,} characters")
    
    # STEP 1: Tag raw text
    tagged_raw = step1_tag_raw_text(
        client=client,
        raw_text=raw_text,
        summary_content=summary_content,
        verify_prompt_template=verify_prompt
    )
    
    # Save intermediate output
    tagged_raw_path = os.path.join(output_dir, "tagged_raw.txt")
    save_file(tagged_raw, tagged_raw_path)
    
    # STEP 2: Clean tagged text
    clean_tagged = step2_clean_tagged_text(
        client=client,
        tagged_text=tagged_raw,
        clean_prompt=clean_prompt
    )
    
    # Save final output - use PDF filename if available
    if pdf_path:
        pdf_filename = os.path.splitext(os.path.basename(pdf_path))[0]
        final_output_path = os.path.join(output_dir, f"{pdf_filename}.txt")
    else:
        final_output_path = os.path.join(output_dir, "final_clean_tagged.txt")
    save_file(clean_tagged, final_output_path)
    
    # Generate summary report
    report = f"""
TAG-THEN-CLEAN PIPELINE REPORT
{'='*60}
State: {state_code}
Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

INPUT FILES:
- Raw text: {raw_text_path}
- Summary: {summary_path}

OUTPUT FILES:
- Step 1 (Tagged Raw): {tagged_raw_path}
- Step 2 (Final Clean Tagged): {final_output_path}

STATISTICS:
- Raw text length: {len(raw_text):,} characters
- Tagged raw length: {len(tagged_raw):,} characters
- Final clean length: {len(clean_tagged):,} characters

WORKFLOW:
1. ✓ Tagged raw text using summary (Para/Run locations match)
2. ✓ Cleaned tagged text (preserved all tags)

SUCCESS: Pipeline completed successfully!
"""
    
    report_path = os.path.join(output_dir, "pipeline_report.txt")
    save_file(report, report_path)
    
    logging.info("\n" + "="*60)
    logging.info("✅ PIPELINE COMPLETED SUCCESSFULLY!")
    logging.info("="*60)
    logging.info(f"📊 Report: {report_path}")
    logging.info(f"📄 Final output: {final_output_path}")
    
    return {
        'tagged_raw': tagged_raw_path,
        'final_output': final_output_path,
        'report': report_path
    }


def tag_then_clean_with_pdfplumber_chunks(pdf_path, state_code, client, session_dir):
    """
    Tag-Then-Clean workflow with PDFPlumber chunks for large documents.
    
    Workflow:
    1. For each chunk: PDFPlumber extract → Tag with summary → Clean
    2. Merge all cleaned chunks
    
    Args:
        pdf_path (str): Path to the original PDF
        state_code (str): State code (KS, FL, etc.)
        client: Anthropic API client
        session_dir (str): Session directory for output files
        
    Returns:
        str: Final merged cleaned tagged text
    """
    import os
    import uuid
    import time
    from pdf_to_text_llm import (
        get_pdf_page_count, 
        extract_pdf_pages_to_file
    )
    from pdf_plumber_extractor import extract_pdf_with_pdfplumber
    from pdf_docx_analysis_pipeline import PDFToDocxAnalysisPipeline
    
    print("\n" + "="*70)
    print("🏷️ TAG-THEN-CLEAN SEQUENTIAL CHUNK WORKFLOW (10+ PAGE DOCUMENTS)")
    print("="*70)
    
    # Get page count
    page_count = get_pdf_page_count(pdf_path)
    if page_count is None:
        logging.error("Failed to get PDF page count")
        return None
    
    print(f"📄 Document: {page_count} pages")
    
    # Create chunks directory
    chunks_base = os.path.join(session_dir, f"chunks_tag_clean_{uuid.uuid4().hex[:8]}")
    os.makedirs(chunks_base, exist_ok=True)
    print(f"📁 Chunks directory: {chunks_base}")
    
    # ============================================================
    # Phase 1: Create sequential chunks (no overlap)
    # ============================================================
    print(f"\n📦 Phase 1: Creating sequential chunks...")
    
    pages_per_chunk = 5
    
    # Create sequential chunks
    chunks = []
    chunk_num = 1
    start_page = 0
    
    while start_page < page_count:
        end_page = min(start_page + pages_per_chunk, page_count)
        
        # If this would be the last chunk and it has <= 2 pages, merge with previous
        if end_page < page_count and (page_count - end_page) <= 2:
            # Extend current chunk to include remaining pages
            end_page = page_count
        
        chunk_pages = list(range(start_page, end_page))
        chunks.append({
            'chunk_num': chunk_num,
            'pages': chunk_pages,
            'page_range': f"{start_page + 1}-{end_page}"
        })
        
        start_page = end_page
        chunk_num += 1
    
    print(f"📊 Created {len(chunks)} sequential chunks (no overlap):")
    for chunk in chunks:
        print(f"   Chunk {chunk['chunk_num']}: Pages {chunk['page_range']}")
    
    # Load prompts once
    verify_prompt = load_verify_prompt(state_code)
    clean_prompt = load_clean_prompt(state_code)
    
    if not verify_prompt or not clean_prompt:
        logging.error(f"Failed to load prompts for {state_code}")
        return None
    
    # ============================================================
    # Phase 2: Process ALL chunks in PARALLEL
    # ============================================================
    print(f"\n🔄 Phase 2: Processing {len(chunks)} chunks in PARALLEL...")
    print(f"   Each chunk: [PDFPlumber extract] → [DOCX+Summary] → [Tag] → [Clean]")
    
    start_time = time.time()
    processed_chunks = []
    
    def process_single_chunk(chunk_info):
        """Process a single chunk through the tag-then-clean pipeline"""
        chunk_num = chunk_info['chunk_num']
        
        try:
            print(f"\n{'='*60}")
            print(f"🔄 Processing Chunk {chunk_num}/{len(chunks)}: Pages {chunk_info['page_range']}")
            print(f"{'='*60}")
            
            # Create temporary directory for this chunk
            chunk_temp_dir = os.path.join(chunks_base, f"chunk_{chunk_num}")
            os.makedirs(chunk_temp_dir, exist_ok=True)
            
            # Extract chunk pages to temporary PDF file
            chunk_pdf_path = os.path.join(chunk_temp_dir, f"chunk_{chunk_num}.pdf")
            start_page = chunk_info['pages'][0] + 1  # Convert to 1-indexed
            end_page = chunk_info['pages'][-1] + 1
            
            success = extract_pdf_pages_to_file(pdf_path, start_page, end_page, chunk_pdf_path)
            if not success:
                logging.error(f"Chunk {chunk_num}: Failed to create chunk PDF")
                return None
            
            # Step 1: PDFPlumber extract raw text
            print(f"   📄 Step 1: PDFPlumber extraction...")
            raw_output = os.path.join(chunk_temp_dir, f"chunk_{chunk_num}_raw.txt")
            
            extract_pdf_with_pdfplumber(chunk_pdf_path, raw_output, validate_sections=True)
            
            if not os.path.exists(raw_output):
                logging.error(f"Chunk {chunk_num}: PDFPlumber extraction failed")
                return None
            
            with open(raw_output, 'r', encoding='utf-8') as f:
                raw_text = f.read()
            
            print(f"   ✅ Extracted {len(raw_text)} characters")
            
            # Step 2: Generate DOCX and summary
            print(f"   📋 Step 2: DOCX analysis & summary generation...")
            pipeline = PDFToDocxAnalysisPipeline(chunk_pdf_path, chunk_temp_dir, state_code=state_code)
            docx_results = pipeline.run_complete_pipeline()
            
            if not docx_results or not docx_results['success']:
                logging.warning(f"Chunk {chunk_num}: DOCX generation failed")
                return None
            
            summary_path = docx_results.get('summary_path')
            if not summary_path or not os.path.exists(summary_path):
                logging.warning(f"Chunk {chunk_num}: Summary not found")
                return None
            
            with open(summary_path, 'r', encoding='utf-8') as f:
                summary_content = f.read()
            
            print(f"   ✅ Summary generated ({len(summary_content)} characters)")
            
            # Step 3: Tag raw text
            print(f"   🏷️ Step 3: Tagging raw text...")
            tagged_text = step1_tag_raw_text(
                client=client,
                raw_text=raw_text,
                summary_content=summary_content,
                verify_prompt_template=verify_prompt
            )
            
            # Save intermediate tagged output
            tagged_path = os.path.join(chunk_temp_dir, f"chunk_{chunk_num}_tagged.txt")
            with open(tagged_path, 'w', encoding='utf-8') as f:
                f.write(tagged_text)
            
            print(f"   ✅ Tagged text ({len(tagged_text)} characters)")
            print(f"✅ Chunk {chunk_num} complete!")
            
            return {
                'chunk_num': chunk_num,
                'pages': chunk_info['pages'],
                'page_range': chunk_info['page_range'],
                'text': tagged_text  # Return tagged text, NOT cleaned
            }
            
        except Exception as e:
            logging.error(f"Chunk {chunk_num}: Processing error: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    # Process chunks in parallel using ThreadPoolExecutor
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    with ThreadPoolExecutor(max_workers=3) as executor:
        # Submit all chunk processing tasks
        future_to_chunk = {executor.submit(process_single_chunk, chunk_info): chunk_info for chunk_info in chunks}
        
        # Collect results as they complete
        for future in as_completed(future_to_chunk):
            chunk_info = future_to_chunk[future]
            try:
                result = future.result()
                if result:
                    processed_chunks.append(result)
            except Exception as e:
                logging.error(f"Chunk {chunk_info['chunk_num']}: Exception during processing: {e}")
    
    # ============================================================
    # Phase 3: Merge all tagged chunks
    # ============================================================
    print(f"\n{'='*60}")
    print(f"🔗 Phase 3: Merging {len(processed_chunks)} tagged chunks...")
    print(f"{'='*60}")
    
    if not processed_chunks:
        logging.error("No chunks were successfully processed")
        return None
    
    # Sort chunks by chunk_num to ensure correct order (parallel processing may finish out of order)
    processed_chunks.sort(key=lambda x: x['chunk_num'])
    
    # Simple concatenation (chunks are sequential, no overlap)
    merged_tagged_text = "\n\n".join([chunk['text'] for chunk in processed_chunks])
    
    print(f"✅ Merged {len(processed_chunks)} chunks: {len(merged_tagged_text):,} characters")
    
    # ============================================================
    # Phase 4: Clean the merged tagged text (ONCE)
    # ============================================================
    print(f"\n{'='*60}")
    print(f"🧹 Phase 4: Cleaning merged tagged text (single pass)...")
    print(f"{'='*60}")
    
    try:
        clean_text = step2_clean_tagged_text(
            client=client,
            tagged_text=merged_tagged_text,
            clean_prompt=clean_prompt
        )
        print(f"✅ Cleaned text: {len(clean_text):,} characters")
    except Exception as e:
        logging.error(f"Error cleaning merged text: {e}")
        import traceback
        traceback.print_exc()
        return None
    
    # Save final clean output with same filename as input PDF
    output_dir = os.path.join(session_dir, "output", "TagThenClean")
    os.makedirs(output_dir, exist_ok=True)
    
    # Use same filename as PDF, just change extension to .txt
    pdf_filename = os.path.splitext(os.path.basename(pdf_path))[0]
    final_output_path = os.path.join(output_dir, f"{pdf_filename}.txt")
    
    with open(final_output_path, 'w', encoding='utf-8') as f:
        f.write(clean_text)
    
    elapsed_time = time.time() - start_time
    print(f"\n✅ Tag-Then-Clean chunked processing complete!")
    print(f"   📄 Processed {len(processed_chunks)}/{len(chunks)} chunks")
    print(f"   📊 Final output: {len(clean_text):,} characters")
    print(f"   ⏱️ Total time: {elapsed_time:.1f} seconds")
    print(f"   💾 Saved to: {final_output_path}")
    
    return clean_text


if __name__ == "__main__":
    # Hardcoded paths for testing - modify these for your test case
    raw_text_path = r"tmp\sessions\3ac1948e-2eec-47a2-a9e7-cb6acc2c7cca\output\pdfplumber_raw.txt"
    summary_path = r"tmp\sessions\3ac1948e-2eec-47a2-a9e7-cb6acc2c7cca\output\Verify\KS\2025_2026_16_2_2_002222_0_4_1_20250203_0_Summary.txt"
    state_code = "KS"
    output_dir = r"tmp\sessions\3ac1948e-2eec-47a2-a9e7-cb6acc2c7cca\output\test_pipeline"
    
    print("="*60)
    print("RUNNING TAG-THEN-CLEAN PIPELINE WITH TEST DATA")
    print("="*60)
    print(f"Raw text: {raw_text_path}")
    print(f"Summary: {summary_path}")
    print(f"State: {state_code}")
    print(f"Output: {output_dir}")
    print("="*60)
    
    try:
        results = tag_then_clean_pipeline(
            raw_text_path=raw_text_path,
            summary_path=summary_path,
            state_code=state_code,
            output_dir=output_dir
        )
        
        print("\n✅ Pipeline completed successfully!")
        print(f"📄 Final output: {results['final_output']}")
        
    except Exception as e:
        logging.error(f"❌ Pipeline failed: {e}")
        import sys
        sys.exit(1)
