"""
Page-by-Page Processing Pipeline (Tag-Then-Clean Approach)
===========================================================
Processes PDF page by page with accurate tagging:

1. Split PDF into individual 1-page PDFs
2. For each page:
   - Extract with pdfplumber → raw text
   - Convert to DOCX → analyze formatting → generate summary  
   - Tag the raw pdfplumber text using summary (Para/Run locations match raw structure)
3. Merge all tagged pages into one file
4. Clean the merged file once using LLM

This approach ensures Para/Run locations in summary match the text being tagged.
"""

import os
import json
import re
import logging
from pathlib import Path
from datetime import datetime
import pdfplumber
import anthropic
import requests
import certifi
from typing import Dict, List
from PyPDF2 import PdfReader, PdfWriter
from concurrent.futures import ThreadPoolExecutor, as_completed

# Import existing pipeline components
from pdf_docx_analysis_pipeline import PDFToDocxAnalysisPipeline

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(message)s')

# Configuration for Thomson Reuters credential service
WORKSPACE_ID = "AgentTraininJaH4"
TOKEN_URL = "https://aiplatform.gcs.int.thomsonreuters.com/v1/anthropic/token"


def get_anthropic_client():
    """Get Anthropic API client with credentials from Thomson Reuters platform."""
    try:
        payload = {"workspace_id": WORKSPACE_ID}
        resp = requests.post(TOKEN_URL, headers=None, json=payload, verify=certifi.where())
        credentials = json.loads(resp.content)
        
        if 'anthropic_api_key' in credentials:
            print("✅ Successfully obtained Anthropic API key")
            return anthropic.Anthropic(api_key=credentials["anthropic_api_key"])
        else:
            print(f"❌ No Anthropic API key found: {credentials}")
            return None
    except Exception as e:
        print(f"❌ Error getting Anthropic client: {e}")
        return None


def split_pdf_into_pages(pdf_path: str, output_dir: str) -> List[str]:
    """Split PDF into individual 1-page PDF files."""
    print(f"\n📄 Splitting PDF into individual pages...")
    
    os.makedirs(output_dir, exist_ok=True)
    page_pdfs = []
    
    try:
        reader = PdfReader(pdf_path)
        total_pages = len(reader.pages)
        print(f"   Total pages: {total_pages}")
        
        for page_num in range(total_pages):
            writer = PdfWriter()
            writer.add_page(reader.pages[page_num])
            
            page_pdf_path = os.path.join(output_dir, f"page_{page_num + 1}.pdf")
            with open(page_pdf_path, 'wb') as output_file:
                writer.write(output_file)
            
            page_pdfs.append(page_pdf_path)
            print(f"   ✓ Page {page_num + 1} → {page_pdf_path}")
        
        print(f"✅ Split into {len(page_pdfs)} individual pages")
        return page_pdfs
        
    except Exception as e:
        print(f"❌ Error splitting PDF: {e}")
        return []


def load_verify_prompt(state_code: str) -> str:
    """Load state-specific verification prompt."""
    project_root = Path(__file__).parent.parent
    prompt_file = project_root / "data" / "Verify_Prompts" / f"{state_code}_Prompt.txt"
    
    if prompt_file.exists():
        with open(prompt_file, 'r', encoding='utf-8') as f:
            return f.read()
    else:
        raise FileNotFoundError(f"Verify prompt not found: {prompt_file}")


def load_clean_prompt(state_code: str) -> str:
    """Load state-specific cleaning prompt."""
    project_root = Path(__file__).parent.parent
    prompt_file = project_root / "data" / "Prompts" / f"{state_code}_Clean_Prompt.txt"
    
    if prompt_file.exists():
        with open(prompt_file, 'r', encoding='utf-8') as f:
            return f.read()
    else:
        raise FileNotFoundError(f"Clean prompt not found: {prompt_file}")


def call_claude_for_tagging(client, raw_text: str, summary_content: str, verify_prompt_template: str):
    """Call Claude to tag raw text using summary."""
    
    # Replace placeholders in verify prompt
    user_prompt = verify_prompt_template.replace(
        "{SUMMARY_CONTENT}", summary_content
    ).replace(
        "{OUTPUT_TEXT_CONTENT}", raw_text
    )
    
    # System prompt emphasizing content preservation
    system_prompt = """You are a legislative document tag verification expert with perfect accuracy.

CRITICAL DIRECTIVE: Your ONLY task is to add formatting tags [A>...<A] and [D>...<D] to existing text. 
You MUST preserve every single word from the input. Tags are markup annotations - they do NOT replace text.

Absolute rule: Input word count = Output word count (excluding tag markers).

If you delete even a single word, you have FAILED the task."""
    
    try:
        response_text = ""
        with client.messages.stream(
            model="claude-sonnet-4-20250514",
            max_tokens=16000,
            temperature=0,
            top_p=1.0,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}]
        ) as stream:
            for text in stream.text_stream:
                response_text += text
        
        return response_text
        
    except Exception as e:
        logging.error(f"❌ Error calling Claude API: {e}")
        raise


def call_claude_for_cleaning(client, tagged_text: str, clean_prompt: str):
    """Call Claude to clean tagged text."""
    
    user_prompt = f"""{clean_prompt}

CRITICAL: PRESERVE ALL TAGS [A>...<A] and [D>...<D] during cleaning. Only remove artifacts listed above.

---RAW TEXT TO CLEAN---

{tagged_text}"""
    
    system_prompt = """You are a deterministic cleaning system for legislative document PDF artifacts.

ABSOLUTE CRITICAL RULES (NO EXCEPTIONS):
1. DO NOT REMOVE ANY TAGS: Every [A>...<A] and [D>...<D] tag MUST appear in your output exactly as in input
2. DO NOT REMOVE ANY LEGISLATIVE CONTENT: Every word of actual legislative text MUST be preserved
3. DO NOT ADD ANY TEXT: No explanations, no conversational text, no preamble, no commentary
4. ONLY REMOVE: PDF artifacts (page markers, line numbers, headers, etc.)

OUTPUT: Return ONLY the cleaned text - NO preamble, NO explanation."""
    
    try:
        response_text = ""
        with client.messages.stream(
            model="claude-opus-4-20250514",  # Opus 4 for cleaning
            max_tokens=16000,
            temperature=0,
            top_p=1.0,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}]
        ) as stream:
            for text in stream.text_stream:
                response_text += text
        
        return response_text
        
    except Exception as e:
        logging.error(f"❌ Error calling Claude API: {e}")
        raise


def consolidate_adjacent_tags(text: str) -> str:
    """
    Consolidate adjacent tags of the same type separated by spaces or newlines.
    
    Handles cases like:
    [A>text1<A] [A>text2<A] [A>text3<A] → [A>text1 text2 text3<A]
    [A>line1<A]\n[A>line2<A] → [A>line1 line2<A]
    """
    
    # Count tags before consolidation
    a_open_before = text.count('[A>')
    a_close_before = text.count('<A]')
    d_open_before = text.count('[D>')
    d_close_before = text.count('<D]')
    
    print(f"   📊 Before consolidation: [A> tags: {a_open_before}, [D> tags: {d_open_before}")
    
    # Consolidate [A>...<A] tags
    iterations = 0
    max_iterations = 100
    total_a_consolidations = 0
    
    while iterations < max_iterations:
        original = text
        
        # Pattern: Tags separated by whitespace (spaces, newlines, tabs, etc.)
        # This pattern will match [A>...<A] followed by whitespace then [A>...<A]
        # We need to be greedy enough to catch multiple in sequence
        count_before = text.count('[A>')
        
        text = re.sub(
            r'\[A>((?:(?!\[A>|<A\]).)*?)<A\]\s*\[A>((?:(?!\[A>|<A\]).)*?)<A\]',
            lambda m: f'[A>{m.group(1)} {m.group(2)}<A]',
            text,
            flags=re.DOTALL
        )
        
        count_after = text.count('[A>')
        consolidations_this_round = count_before - count_after
        total_a_consolidations += consolidations_this_round
        
        if text == original:
            break
        iterations += 1
    
    if total_a_consolidations > 0:
        print(f"   ✓ Consolidated {total_a_consolidations} [A> tag pairs in {iterations} iterations")
    
    # Consolidate [D>...<D] tags (same logic)
    iterations = 0
    total_d_consolidations = 0
    
    while iterations < max_iterations:
        original = text
        
        count_before = text.count('[D>')
        
        text = re.sub(
            r'\[D>((?:(?!\[D>|<D\]).)*?)<D\]\s*\[D>((?:(?!\[D>|<D\]).)*?)<D\]',
            lambda m: f'[D>{m.group(1)} {m.group(2)}<D]',
            text,
            flags=re.DOTALL
        )
        
        count_after = text.count('[D>')
        consolidations_this_round = count_before - count_after
        total_d_consolidations += consolidations_this_round
        
        if text == original:
            break
        iterations += 1
    
    if total_d_consolidations > 0:
        print(f"   ✓ Consolidated {total_d_consolidations} [D> tag pairs in {iterations} iterations")
    
    # Count tags after consolidation
    a_open_after = text.count('[A>')
    a_close_after = text.count('<A]')
    d_open_after = text.count('[D>')
    d_close_after = text.count('<D]')
    
    print(f"   📊 After consolidation: [A> tags: {a_open_after}, [D> tags: {d_open_after}")
    
    # Verify tag pairing is still correct
    if a_open_after != a_close_after:
        print(f"   ⚠️ WARNING: Mismatched [A> tags! Opens: {a_open_after}, Closes: {a_close_after}")
    if d_open_after != d_close_after:
        print(f"   ⚠️ WARNING: Mismatched [D> tags! Opens: {d_open_after}, Closes: {d_close_after}")
    
    return text


def process_single_page(page_pdf_path: str, page_number: int, state_code: str, output_dir: str, client, verify_prompt_template: str) -> Dict:
    """
    Process a single page: PDFPlumber extract → DOCX analysis → Tag raw text.
    
    Returns:
        dict: {'page_number': int, 'tagged_text': str, 'success': bool}
    """
    print(f"\n{'='*60}")
    print(f"📄 Processing Page {page_number}")
    print(f"{'='*60}")
    
    try:
        # Step 2a: PDFPlumber extraction → raw text
        print(f"   📋 Step 2a: PDFPlumber extraction...")
        with pdfplumber.open(page_pdf_path) as pdf:
            page = pdf.pages[0]  # Single page
            raw_text = page.extract_text(layout=True)
        
        if not raw_text:
            print(f"   ⚠️ No text extracted from page {page_number}")
            return {'page_number': page_number, 'tagged_text': '', 'success': False}
        
        print(f"   ✅ Extracted {len(raw_text)} characters")
        
        # Save raw text for this page
        raw_output_path = os.path.join(output_dir, f"page_{page_number}_raw.txt")
        with open(raw_output_path, 'w', encoding='utf-8') as f:
            f.write(raw_text)
        
        # Step 2b: PDF→DOCX analysis → summary
        print(f"   📋 Step 2b: DOCX analysis & summary generation...")
        pipeline = PDFToDocxAnalysisPipeline(page_pdf_path, output_dir, state_code=state_code)
        docx_results = pipeline.run_complete_pipeline()
        
        if not docx_results or not docx_results['success']:
            print(f"   ⚠️ DOCX analysis failed for page {page_number}")
            return {'page_number': page_number, 'tagged_text': raw_text, 'success': True}  # Return untagged
        
        summary_path = docx_results.get('summary_path')
        if not summary_path or not os.path.exists(summary_path):
            print(f"   ℹ️ No summary generated for page {page_number} (no formatting found)")
            return {'page_number': page_number, 'tagged_text': raw_text, 'success': True}  # Return untagged
        
        # Load summary
        with open(summary_path, 'r', encoding='utf-8') as f:
            summary_content = f.read()
        
        print(f"   ✅ Summary: {len(summary_content)} characters")
        
        # Step 2c: Tag raw text using summary
        print(f"   🏷️ Step 2c: Tagging raw text with LLM...")
        tagged_text = call_claude_for_tagging(
            client=client,
            raw_text=raw_text,
            summary_content=summary_content,
            verify_prompt_template=verify_prompt_template
        )
        
        # Consolidate adjacent tags
        tagged_text = consolidate_adjacent_tags(tagged_text)
        
        print(f"   ✅ Tagged: {len(tagged_text)} characters")
        
        # Save tagged text for this page
        tagged_output_path = os.path.join(output_dir, f"page_{page_number}_tagged.txt")
        with open(tagged_output_path, 'w', encoding='utf-8') as f:
            f.write(tagged_text)
        
        return {'page_number': page_number, 'tagged_text': tagged_text, 'success': True}
        
    except Exception as e:
        print(f"   ❌ Error processing page {page_number}: {e}")
        import traceback
        traceback.print_exc()
        return {'page_number': page_number, 'tagged_text': '', 'success': False}


def run_page_by_page_pipeline(pdf_path: str, output_folder: str, state_code: str) -> Dict:
    """
    Main pipeline: Process PDF page by page, then merge and clean.
    
    Workflow:
    1. Split PDF into 1-page PDFs
    2. For each page: PDFPlumber → DOCX → Tag
    3. Merge all tagged pages
    4. Clean merged text once
    """
    print("🔄 STARTING PAGE-BY-PAGE TAG-THEN-CLEAN PIPELINE")
    print("=" * 70)
    print(f"📄 PDF: {pdf_path}")
    print(f"🏷️ State: {state_code}")
    print("=" * 70)
    
    # Initialize Claude client
    client = get_anthropic_client()
    if not client:
        return {'success': False, 'error': 'Failed to initialize Claude client'}
    
    # Load prompts
    try:
        verify_prompt_template = load_verify_prompt(state_code)
        clean_prompt = load_clean_prompt(state_code)
    except Exception as e:
        print(f"❌ Failed to load prompts: {e}")
        return {'success': False, 'error': str(e)}
    
    # Create working directory
    pdf_filename = Path(pdf_path).stem
    pages_dir = os.path.join(output_folder, "pages_temp", pdf_filename)
    os.makedirs(pages_dir, exist_ok=True)
    
    # STEP 1: Split PDF into individual pages
    print(f"\n{'='*70}")
    print("STEP 1: Split PDF into individual pages")
    print("=" * 70)
    
    page_pdfs = split_pdf_into_pages(pdf_path, pages_dir)
    if not page_pdfs:
        return {'success': False, 'error': 'Failed to split PDF'}
    
    # STEP 2: Process each page (PDFPlumber → DOCX → Tag) IN PARALLEL
    print(f"\n{'='*70}")
    print("STEP 2: Process each page IN PARALLEL (PDFPlumber → DOCX → Tag)")
    print("=" * 70)
    
    processed_pages = []
    
    # Process pages in parallel using ThreadPoolExecutor
    max_workers = min(3, len(page_pdfs))  # Max 3 concurrent pages to avoid rate limits
    print(f"🔄 Processing {len(page_pdfs)} pages with {max_workers} parallel workers...")
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all page processing tasks
        future_to_page = {
            executor.submit(
                process_single_page,
                page_pdf_path=page_pdf,
                page_number=idx,
                state_code=state_code,
                output_dir=pages_dir,
                client=client,
                verify_prompt_template=verify_prompt_template
            ): idx
            for idx, page_pdf in enumerate(page_pdfs, start=1)
        }
        
        # Collect results as they complete
        for future in as_completed(future_to_page):
            page_num = future_to_page[future]
            try:
                result = future.result()
                if result['success']:
                    processed_pages.append(result)
                    print(f"   ✅ Page {page_num} completed")
                else:
                    print(f"   ⚠️ Page {page_num} failed")
            except Exception as e:
                print(f"   ❌ Page {page_num} raised exception: {e}")
    
    # Sort pages by page number (parallel processing may finish out of order)
    processed_pages.sort(key=lambda x: x['page_number'])
    print(f"\n✅ Processed {len(processed_pages)}/{len(page_pdfs)} pages successfully")
    
    if not processed_pages:
        return {'success': False, 'error': 'No pages were successfully processed'}
    
    # STEP 3: Merge all tagged pages
    print(f"\n{'='*70}")
    print("STEP 3: Merge all tagged pages")
    print("=" * 70)
    
    merged_tagged_text = "\n\n".join([p['tagged_text'] for p in processed_pages if p['tagged_text']])
    print(f"✅ Merged {len(processed_pages)} pages: {len(merged_tagged_text):,} characters")
    
    # Consolidate tags after merging (catch cross-page adjacent tags)
    print(f"🔧 Consolidating adjacent tags across pages...")
    merged_tagged_text = consolidate_adjacent_tags(merged_tagged_text)
    print(f"✅ Consolidation complete: {len(merged_tagged_text):,} characters")
    
    # Save merged tagged text
    tagged_folder = os.path.join(output_folder, "Tagged", state_code)
    os.makedirs(tagged_folder, exist_ok=True)
    
    merged_tagged_path = os.path.join(tagged_folder, f"{pdf_filename}_Tagged_Merged.txt")
    with open(merged_tagged_path, 'w', encoding='utf-8') as f:
        f.write(merged_tagged_text)
    print(f"💾 Saved: {merged_tagged_path}")
    
    # STEP 4: Clean the merged tagged text (ONCE)
    print(f"\n{'='*70}")
    print("STEP 4: Clean merged tagged text (single pass)")
    print("=" * 70)
    
    try:
        print(f"🧹 Calling Claude for cleaning...")
        clean_text = call_claude_for_cleaning(
            client=client,
            tagged_text=merged_tagged_text,
            clean_prompt=clean_prompt
        )
        print(f"✅ Cleaned text: {len(clean_text):,} characters")
        
        # Save final clean output
        final_output_path = os.path.join(tagged_folder, f"{pdf_filename}_Final.txt")
        with open(final_output_path, 'w', encoding='utf-8') as f:
            f.write(clean_text)
        
        print(f"💾 Saved: {final_output_path}")
        
        # Verify tags preserved
        original_a_tags = merged_tagged_text.count('[A>')
        original_d_tags = merged_tagged_text.count('[D>')
        final_a_tags = clean_text.count('[A>')
        final_d_tags = clean_text.count('[D>')
        
        print(f"\n📊 Tag preservation check:")
        print(f"   [A> tags: {original_a_tags} → {final_a_tags} {'✅' if original_a_tags == final_a_tags else '⚠️'}")
        print(f"   [D> tags: {original_d_tags} → {final_d_tags} {'✅' if original_d_tags == final_d_tags else '⚠️'}")
        
        print(f"\n{'='*70}")
        print("✅ PIPELINE COMPLETED SUCCESSFULLY!")
        print("=" * 70)
        print(f"📄 Processed {len(processed_pages)} pages")
        print(f"📁 Final output: {final_output_path}")
        print("=" * 70)
        
        return {
            'success': True,
            'pdf_path': pdf_path,
            'total_pages': len(page_pdfs),
            'processed_pages': len(processed_pages),
            'tagged_merged_path': merged_tagged_path,
            'final_output_path': final_output_path
        }
        
    except Exception as e:
        print(f"❌ Error cleaning merged text: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 3:
        print("Usage: python page_by_page_pipeline_new.py <pdf_path> <state_code>")
        print("Example: python page_by_page_pipeline_new.py ../data/input/FL/bill.pdf FL")
        sys.exit(1)
    
    pdf_path = sys.argv[1]
    state_code = sys.argv[2]
    output_folder = "../data/output"
    
    results = run_page_by_page_pipeline(pdf_path, output_folder, state_code)
    
    if results['success']:
        print("\n🎉 Pipeline completed successfully!")
    else:
        print(f"\n💥 Pipeline failed: {results.get('error', 'Unknown error')}")
