# LLM-Based Tag Verification and Correction Script - Updated for Temp Structure
import anthropic
import requests
import json
import certifi
import ssl
import os
import sys
import logging
import urllib3
import re
from datetime import datetime
from pathlib import Path
from document_chunker import DocumentChunker, TextChunk, SummaryItem

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Get the project root directory (parent of app directory)
PROJECT_ROOT = Path(__file__).parent.parent.absolute()

# Configuration (same as pdf_to_text_llm.py)
WORKSPACE_ID = "AgentTraininJaH4"
TOKEN_URL = "https://aiplatform.gcs.int.thomsonreuters.com/v1/anthropic/token"

# State-specific formatting type filters
STATE_FORMATTING_FILTERS = {
    'FL': ['underline', 'strikethrough'],  # Florida only cares about underline and strikethrough
    'CT': ['strikethrough'],  # Connecticut only cares about deletions (handled separately)
    'CA': ['underline', 'strikethrough'],  # California similar to Florida
    'CO': ['underline'],  # Colorado example
    # Add more states as needed
    'DEFAULT': ['underline', 'strikethrough', 'bold', 'italic']  # Default filter
}

def strip_conversational_headers(text):
    """
    Remove conversational preambles that LLMs sometimes add despite instructions.
    Detects and strips common patterns like:
    - "I'll add the missing tags..."
    - "Let me review..."
    - "Here's the corrected text:"
    
    Args:
        text (str): LLM output text that may contain conversational headers
        
    Returns:
        str: Text with conversational headers removed
    """
    if not text:
        return text
    
    original_length = len(text)
    
    # Common conversational patterns that appear at the start of LLM responses
    conversational_patterns = [
        r"^I'll add the missing tags.*?(?:\n|:)\s*",
        r"^Let me review.*?(?:\n|:)\s*",
        r"^Here's the corrected text.*?(?:\n|:)\s*",
        r"^Looking at the summary.*?(?:\n|:)\s*",
        r"^Based on the summary.*?(?:\n|:)\s*",
        r"^I'll now add.*?(?:\n|:)\s*",
        r"^Here is the.*?(?:\n|:)\s*",
    ]
    
    # Try each pattern
    for pattern in conversational_patterns:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE | re.MULTILINE)
    
    # If we removed something, log it
    if len(text) < original_length:
        removed_chars = original_length - len(text)
        logging.info(f"[VERIFICATION POST-PROCESSING] Stripped conversational header ({removed_chars} characters)")
    
    # Strip any leading/trailing whitespace after removal
    text = text.strip()
    
    return text

def filter_summary_by_formatting_types(summary_content, state_code):
    """
    Filter summary items to only include formatting types relevant to the state.
    This dramatically reduces summary size and keeps prompts under token limits.
    
    Args:
        summary_content (str): Full summary content
        state_code (str): State code (e.g., 'FL', 'CT')
        
    Returns:
        str: Filtered summary content with only relevant formatting types
    """
    # Get allowed formatting types for this state
    allowed_types = STATE_FORMATTING_FILTERS.get(state_code, STATE_FORMATTING_FILTERS['DEFAULT'])
    
    print(f"🔍 Filtering summary for {state_code} - keeping only: {', '.join(allowed_types)}")
    
    lines = summary_content.split('\n')
    filtered_lines = []
    current_item = []
    keep_current = False
    items_kept = 0
    items_filtered = 0
    
    i = 0
    while i < len(lines):
        line = lines[i]
        
        # Check if this is a numbered item line (start of new item)
        if line.strip() and line[0:3].replace('.', '').replace(' ', '').isdigit():
            # Save previous item if we were keeping it
            if current_item and keep_current:
                filtered_lines.extend(current_item)
                filtered_lines.append('')  # Add blank line between items
                items_kept += 1
            elif current_item:
                items_filtered += 1
            
            # Start new item
            current_item = [line]
            keep_current = False
            
            # Check if next lines contain formatting type
            if i + 2 < len(lines):
                formatting_line = lines[i + 2].strip()
                if 'Formatting:' in formatting_line:
                    # Extract formatting type
                    formatting_type = formatting_line.replace('Formatting:', '').strip().lower()
                    
                    # Check if this formatting type should be kept
                    keep_current = any(allowed_type in formatting_type for allowed_type in allowed_types)
            
            i += 1
        elif current_item:
            # Add line to current item
            current_item.append(line)
            i += 1
        else:
            # Header lines before items start
            filtered_lines.append(line)
            i += 1
    
    # Don't forget the last item
    if current_item and keep_current:
        filtered_lines.extend(current_item)
        items_kept += 1
    elif current_item:
        items_filtered += 1
    
    filtered_summary = '\n'.join(filtered_lines)
    
    print(f"✅ Summary filtered: kept {items_kept} items, removed {items_filtered} items")
    print(f"   Original size: {len(summary_content):,} characters")
    print(f"   Filtered size: {len(filtered_summary):,} characters")
    print(f"   Reduction: {((1 - len(filtered_summary)/len(summary_content)) * 100):.1f}%")
    
    return filtered_summary

def read_file_content(file_path, encoding='utf-8'):
    """
    Read file content with error handling.
    """
    try:
        with open(file_path, 'r', encoding=encoding) as f:
            return f.read()
    except Exception as e:
        print(f"Error reading file {file_path}: {e}")
        return None

def load_verification_prompt(state_code):
    """
    Load the verification prompt from external file for the specified state.
    
    Args:
        state_code (str): State code (e.g., 'FL', 'TX', 'CA')
    
    Returns:
        str: The prompt template with placeholders {SUMMARY_CONTENT} and {OUTPUT_TEXT_CONTENT}
    """
    try:
        # Try state-specific prompt first from data/Verify_Prompts folder
        prompt_file = os.path.join(PROJECT_ROOT, "data", "Verify_Prompts", f"{state_code}_Prompt.txt")
        
        if os.path.exists(prompt_file):
            with open(prompt_file, "r", encoding='utf-8') as f:
                prompt_template = f.read().strip()
                print(f"✅ Loaded state-specific verification prompt for {state_code}")
                return prompt_template
        else:
            print(f"⚠️ No state-specific prompt found for {state_code} at {prompt_file}")
            print("📝 Using default FL prompt as fallback")
            
            # Fall back to FL prompt as default
            fl_prompt_file = os.path.join(PROJECT_ROOT, "data", "Verify_Prompts", "FL_Prompt.txt")
            if os.path.exists(fl_prompt_file):
                with open(fl_prompt_file, "r", encoding='utf-8') as f:
                    prompt_template = f.read().strip()
                    print(f"✅ Loaded fallback FL verification prompt")
                    return prompt_template
            else:
                print(f"❌ No verification prompts found in {PROJECT_ROOT}/data/Verify_Prompts/")
                return None
                
    except Exception as e:
        print(f"❌ Error loading verification prompt: {e}")
        return None

def wrap_ct_bracketed_text(text):
    """
    Wrap Connecticut bracketed text with [D>...<D] tags.
    
    In CT documents, text enclosed in brackets [like this] represents deletions/amendments
    and should be tagged as [D>[like this]<D].
    
    Args:
        text (str): The text to process
    
    Returns:
        str: Text with bracketed content wrapped in [D>...<D] tags
    """
    import re
    
    # Strategy: Find all bracketed text and wrap it if not already wrapped
    # We need to handle nested brackets carefully
    
    # Pattern explanation:
    # (?<!\[D>) - negative lookbehind: not preceded by [D>
    # \[ - opening bracket
    # [^\[\]]+ - one or more characters that are not [ or ] (avoids nested brackets)
    # \] - closing bracket  
    # (?!<D\]) - negative lookahead: not followed by <D]
    
    # Simple pattern for non-nested brackets
    pattern = r'(?<!\[D>)\[([^\[\]]+)\](?!<D\])'
    
    def replace_bracket(match):
        bracketed_content = match.group(1)
        return f"[D>[{bracketed_content}]<D]"
    
    # Apply the replacement
    result = re.sub(pattern, replace_bracket, text)
    
    return result

def create_verification_prompt(summary_content, output_text_content, state_code="FL"):
    """
    Create a verification prompt by loading the template and replacing placeholders.
    
    Args:
        summary_content (str): The formatted items summary content
        output_text_content (str): The current output text content
        state_code (str): State code for state-specific prompts (default: "FL")
    
    Returns:
        str: Complete prompt with content inserted, or None for DC (no verification needed)
    """
    
    print(f"🔨 Loading verification prompt for state: {state_code}")
    
    # Skip verification for DC and IN states
    if state_code in ["DC", "IN"]:
        print(f"⏭️ {state_code} state doesn't require tag verification - skipping prompt creation")
        return None
    
    # Load the prompt template for the state
    prompt_template = load_verification_prompt(state_code)
    
    if not prompt_template:
        print(f"❌ Could not load verification prompt for {state_code}")
        return None
    
    # Replace placeholders with actual content
    try:
        prompt = prompt_template.replace("{SUMMARY_CONTENT}", summary_content)
        prompt = prompt.replace("{OUTPUT_TEXT_CONTENT}", output_text_content)
        
        print(f"✅ Verification prompt created for {state_code} ({len(prompt)} characters)")
        return prompt
        
    except Exception as e:
        print(f"❌ Error creating verification prompt: {e}")
        return None

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

def call_claude_api(prompt, client):
    """
    Call Claude API for tag verification using the same approach as pdf_to_text_llm.py.
    """
    
    try:
        print("🔄 Calling Claude API for tag verification...")
        
        # Create message with Claude (using highest accuracy settings)
        message = client.messages.create(
            model="claude-opus-4-20250514",  # Keep the most accurate model
            max_tokens=8192,  # Increased for large legislative documents
            temperature=0.1,  # Maximum consistency for accuracy
            top_p=0.1,  # Add top_p for even more deterministic results
            messages=[{
                "role": "user", 
                "content": prompt
            }]
        )
        
        # Extract text from response (same as pdf_to_text_llm.py)
        formatted_text = ""
        for block in message.content:
            if hasattr(block, 'text'):
                formatted_text += block.text
        
        print("✅ Successfully received response from Claude")
        return formatted_text
        
    except Exception as e:
        print(f"❌ Claude API request failed: {e}")
        return None

def parse_verification_response(response_text):
    """
    Parse the verification response that contains both corrected text and verification CSV.
    Also strips any conversational headers that the LLM might have added.
    
    Args:
        response_text (str): The complete response from Claude
        
    Returns:
        tuple: (corrected_text, verification_csv) or (response_text, None) if no CSV found
    """
    try:
        # STEP 1: Strip conversational headers first (before parsing)
        response_text = strip_conversational_headers(response_text)
        
        # STEP 2: Look for the verification CSV section
        if "=== VERIFICATION LOG CSV ===" in response_text:
            parts = response_text.split("=== VERIFICATION LOG CSV ===")
            
            # Extract corrected text (everything before the CSV)
            corrected_text = parts[0].replace("=== CORRECTED TEXT ===", "").strip()
            
            # Extract verification CSV (everything after the CSV header)
            verification_csv = parts[1].strip() if len(parts) > 1 else ""
            
            print("📊 Parsed response with verification CSV")
            return corrected_text, verification_csv
            
        elif "=== VERIFICATION LOG TABLE ===" in response_text:
            # Backward compatibility with old table format
            parts = response_text.split("=== VERIFICATION LOG TABLE ===")
            corrected_text = parts[0].replace("=== CORRECTED TEXT ===", "").strip()
            verification_log = parts[1].strip() if len(parts) > 1 else ""
            print("📊 Parsed response with old table format")
            return corrected_text, verification_log
            
        elif "=== CORRECTED TEXT ===" in response_text:
            # Only corrected text section found
            corrected_text = response_text.replace("=== CORRECTED TEXT ===", "").strip()
            print("📄 Parsed response with corrected text only")
            return corrected_text, None
            
        else:
            # No special formatting, return as-is
            print("📄 Response in standard format")
            return response_text.strip(), None
            
    except Exception as e:
        print(f"⚠️ Error parsing verification response: {e}")
        return response_text.strip(), None

def save_verification_log(verification_data, output_file_path):
    """
    Save the verification log as a CSV file for Excel compatibility.
    
    Args:
        verification_data (str): The verification CSV data from Claude
        output_file_path (str): Path to the output file (used to determine log file name)
        
    Returns:
        str: Path to the saved CSV file, or None if saving failed
    """
    try:
        if not verification_data:
            return None
            
        original_path = Path(output_file_path)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        # Create CSV filename
        csv_filename = f"{original_path.stem}_VERIFICATION_LOG_{timestamp}.csv"
        csv_file_path = "../Temp/Verification"/ csv_filename
        
        with open(csv_file_path, 'w', encoding='utf-8', newline='') as f:
            f.write("# LLM TAG VERIFICATION LOG\n")
            f.write(f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"# Source file: {output_file_path}\n")
            f.write("# \n")
            f.write("# Column Definitions:\n")
            f.write("# - Word: The text being analyzed\n")
            f.write("# - Location: Contextual location from summary\n")
            f.write("# - Formatting: underline, strikethrough, bold, italic, etc.\n")
            f.write("# - Original_Status: Tagged_Correctly, Tagged_Incorrectly, Not_Tagged, Wrong_Tag_Type\n")
            f.write("# - Action_Taken: Added_Tag, Removed_Tag, Corrected_Tag, No_Change\n")
            f.write("# - Final_Status: Tagged_Correctly, Not_Tagged\n")
            f.write("# \n")
            
            # Write the CSV data
            f.write(verification_data)
            
        print(f"� Verification CSV saved to: {csv_file_path}")
        return str(csv_file_path)
        
    except Exception as e:
        print(f"❌ Error saving verification CSV: {e}")
        return None

def get_api_key():
    """
    Get API client using the same approach as pdf_to_text_llm.py.
    """
    return get_anthropic_client()

def extract_formatted_items_summary(summary_content):
    """
    Extract formatted items section from summary, handling the new contextual location format.
    """
    lines = summary_content.split('\n')
    formatted_section = []
    
    in_formatted_section = False
    for line in lines:
        if "All formatted text with contextual locations:" in line:
            in_formatted_section = True
            formatted_section.append(line)
            continue
        elif "All formatted text:" in line:  # Fallback for old format
            in_formatted_section = True
            formatted_section.append(line)
            continue
        
        if in_formatted_section:
            if "BREAKDOWN BY FORMATTING TYPE:" in line:
                break
            formatted_section.append(line)
    
    # Convert new format to format expected by verification prompts
    if any("contextual locations:" in line for line in formatted_section):
        # Parse new format and convert to cleaner format for LLM
        converted_section = ["Formatted items with contextual locations:\n"]
        
        i = 0
        while i < len(formatted_section):
            line = formatted_section[i]
            if line.strip() and line[0].isdigit() and "'" in line:
                # This is a formatted item line like "28. 'associate'"
                converted_section.append(line)
                
                # Look for the location and formatting lines that follow
                if i + 1 < len(formatted_section) and "Location:" in formatted_section[i + 1]:
                    location_line = formatted_section[i + 1].strip()
                    converted_section.append(f"   {location_line}")
                    
                if i + 2 < len(formatted_section) and "Formatting:" in formatted_section[i + 2]:
                    formatting_line = formatted_section[i + 2].strip()
                    converted_section.append(f"   {formatting_line}")
                    
                converted_section.append("")  # Add spacing
                i += 3
            else:
                i += 1
                
        return '\n'.join(converted_section)
    
    # Return original format if it's the old style
    return '\n'.join(formatted_section)

def verify_chunk_with_llm(chunk_text, mini_summary, state_code, client, max_retries=3):
    """
    Verify a single chunk of text with its relevant summary items (with retry logic).
    
    Args:
        chunk_text: Text content of the chunk
        mini_summary: Summary content containing only relevant items for this chunk
        state_code: State code for verification
        client: Anthropic client instance
        max_retries: Maximum number of retry attempts (default: 3)
        
    Returns:
        str: Verified chunk text, or original if all retries fail
    """
    import time
    
    for attempt in range(1, max_retries + 1):
        try:
            if attempt > 1:
                print(f"      🔄 Retry {attempt}/{max_retries}...")
                time.sleep(attempt * 2)  # Exponential backoff
            
            # Create verification prompt for this chunk
            prompt = create_verification_prompt(mini_summary, chunk_text, state_code)
            
            if not prompt:
                print("      ❌ Failed to create verification prompt for chunk")
                return chunk_text  # Return original if prompt creation fails
            
            # Call Claude API
            claude_response = call_claude_api(prompt, client)
            
            if not claude_response:
                if attempt < max_retries:
                    print(f"      ⚠️ No response from Claude (attempt {attempt}/{max_retries})")
                    continue
                else:
                    print(f"      ❌ Failed after {max_retries} attempts, using original chunk")
                    return chunk_text
            
            # Parse response (extract just the corrected text, ignore CSV)
            corrected_text, _ = parse_verification_response(claude_response)
            
            return corrected_text
            
        except Exception as e:
            error_msg = str(e)
            print(f"      ❌ Error (attempt {attempt}/{max_retries}): {error_msg[:100]}")
            
            # Check if retryable
            retryable_errors = [
                "peer closed connection",
                "incomplete chunked read",
                "timeout",
                "connection reset",
                "rate_limit"
            ]
            
            is_retryable = any(err in error_msg.lower() for err in retryable_errors)
            
            if attempt < max_retries and is_retryable:
                print(f"      ⏳ Retryable error, waiting before retry...")
                continue
            else:
                print(f"      ❌ Returning original chunk after {attempt} attempts")
                return chunk_text
    
    return chunk_text


def verify_with_chunking(summary_file_path, output_text_file_path, state_code, client):
    """
    Verify large documents using chunking strategy.
    
    Strategy: 
    1. Split text into chunks
    2. Split summary items into batches  
    3. For each text chunk, verify with a batch of summary items
    4. Use context matching to find relevant items
    
    Args:
        summary_file_path: Path to summary file
        output_text_file_path: Path to output text file
        state_code: State code for verification
        client: Anthropic client instance
        
    Returns:
        str: Complete verified text
    """
    print("\n🔄 Using CHUNKED verification strategy...")
    print("=" * 60)
    
    # Read files
    print("📖 Reading files...")
    summary_content = read_file_content(summary_file_path)
    output_text = read_file_content(output_text_file_path)
    
    if not summary_content or not output_text:
        print("❌ Failed to read files")
        return None
    
    # Extract formatted items section
    formatted_summary = extract_formatted_items_summary(summary_content)
    
    if not formatted_summary:
        print("⚠️ No formatted summary found - returning original text")
        return output_text
    
    print(f"✅ Extracted formatted summary ({len(formatted_summary)} characters)")
    
    # Filter summary by state-specific formatting types
    formatted_summary = filter_summary_by_formatting_types(formatted_summary, state_code)
    
    # Initialize chunker
    chunker = DocumentChunker(max_tokens=50000)
    
    # Parse summary items
    print("\n📋 Parsing summary items...")
    summary_items = chunker.parse_summary_items(formatted_summary)
    
    if not summary_items:
        print("⚠️ No summary items found - returning original text")
        return output_text
    
    print(f"✅ Parsed {len(summary_items)} summary items")
    
    # Chunk the text
    print("\n📄 Chunking text...")
    text_chunks = chunker.chunk_text(output_text)
    
    if not text_chunks:
        print("❌ Failed to create text chunks")
        return None
    
    print(f"✅ Created {len(text_chunks)} text chunks")
    
    # Split summary items into batches for each chunk
    items_per_chunk = len(summary_items) // len(text_chunks) + 1
    print(f"📊 Batching strategy: ~{items_per_chunk} items per chunk")
    
    chunk_assignments = []
    for i, chunk in enumerate(text_chunks):
        start_idx = i * items_per_chunk
        end_idx = min((i + 1) * items_per_chunk, len(summary_items))
        batch_items = summary_items[start_idx:end_idx]
        chunk_assignments.append((chunk, batch_items))
        print(f"   Chunk {i+1}: items {start_idx+1}-{end_idx} ({len(batch_items)} items)")
    
    # Verify each chunk with its batch of summary items
    print("\n🔍 Verifying chunks...")
    verified_chunks = []
    
    for chunk, items in chunk_assignments:
        print(f"\n   📦 Chunk {chunk.chunk_number}/{chunk.total_chunks}")
        print(f"      Text: {chunk.token_estimate:,} tokens")
        print(f"      Items to verify: {len(items)}")
        
        if not items:
            # No items to verify in this chunk - keep original
            print(f"      ⏭️ No items to verify, keeping original")
            verified_chunks.append(chunk.content)
            continue
        
        # Create mini-summary for this chunk
        mini_summary = chunker.create_mini_summary(items)
        mini_summary_tokens = chunker.estimate_tokens(mini_summary)
        total_tokens = chunk.token_estimate + mini_summary_tokens + 2000  # +2000 for prompt
        
        print(f"      Mini-summary: {mini_summary_tokens:,} tokens")
        print(f"      Total: {total_tokens:,} tokens")
        
        # Safety check - if still too large, split items into smaller batches
        if total_tokens > 180000:  # Leave safety margin below 200K
            print(f"      ⚠️ Still too large! Splitting items further...")
            # Calculate how many sub-batches needed
            num_sub_batches = (total_tokens // 180000) + 1
            sub_batch_size = max(1, len(items) // num_sub_batches)
            
            print(f"      Creating {num_sub_batches} sub-batches of ~{sub_batch_size} items each")
            
            current_chunk_text = chunk.content
            for sub_start in range(0, len(items), sub_batch_size):
                sub_items = items[sub_start:sub_start + sub_batch_size]
                sub_mini_summary = chunker.create_mini_summary(sub_items)
                sub_summary_tokens = chunker.estimate_tokens(sub_mini_summary)
                sub_total_tokens = chunk.token_estimate + sub_summary_tokens + 2000
                
                print(f"         Sub-batch: {len(sub_items)} items (~{sub_total_tokens:,} total tokens)")
                
                # Apply CT-specific bracket wrapping if needed
                if state_code == "CT":
                    current_chunk_text = wrap_ct_bracketed_text(current_chunk_text)
                
                current_chunk_text = verify_chunk_with_llm(current_chunk_text, sub_mini_summary, state_code, client)
                if not current_chunk_text:
                    current_chunk_text = chunk.content
                    break
            
            verified_chunk = current_chunk_text
        else:
            # Apply CT-specific bracket wrapping if needed
            chunk_text = chunk.content
            if state_code == "CT":
                chunk_text = wrap_ct_bracketed_text(chunk_text)
            
            # Verify this chunk with mini-summary
            print(f"      🚀 Sending to Claude...")
            verified_chunk = verify_chunk_with_llm(chunk_text, mini_summary, state_code, client)
        
        if verified_chunk:
            print(f"      ✅ Chunk verified ({len(verified_chunk)} characters)")
            verified_chunks.append(verified_chunk)
        else:
            print(f"      ⚠️ Verification failed, using original chunk")
            verified_chunks.append(chunk.content)
    
    # Combine verified chunks
    print("\n🔗 Combining verified chunks...")
    combined_text = '\n\n'.join(verified_chunks)
    
    # Apply tag consolidation
    print("🔧 Applying tag consolidation...")
    from tag_consolidator import consolidate_tags
    combined_text = consolidate_tags(combined_text)
    
    print(f"✅ Chunked verification complete ({len(combined_text)} characters)")
    
    return combined_text


def should_chunk_for_verification(pdf_path):
    """
    Decide if PDF should be chunked for verification based on page count.
    
    Args:
        pdf_path (str): Path to the PDF file
        
    Returns:
        tuple: (should_chunk, page_count, reason)
    """
    from pdf_to_text_llm import get_pdf_page_count
    
    page_count = get_pdf_page_count(pdf_path)
    
    if page_count is None:
        return False, 0, "Could not determine page count"
    
    if page_count > 15:
        return True, page_count, f"Document has {page_count} pages (>15 threshold)"
    else:
        return False, page_count, f"Document has {page_count} pages (≤15 threshold)"


def process_verification_chunk(chunk_info, state_code, client, session_dir):
    """
    Process a single verification chunk: PDF→Text, PDF→DOCX→Summary, LLM verify.
    
    Args:
        chunk_info (dict): Chunk metadata with paths and page info
        state_code (str): State code (FL, CT, etc.)
        client: Anthropic API client
        session_dir (str): Session directory for output
        
    Returns:
        dict: Result with verified text and metadata
    """
    import os
    from pdf_to_text_llm import process_pdf_with_claude, load_example_pairs, load_conversion_prompt
    from pdf_docx_analysis_pipeline import PDFToDocxAnalysisPipeline
    
    chunk_num = chunk_info['chunk_num']
    chunk_pdf_path = chunk_info['pdf_path']
    chunk_dir = chunk_info['chunk_dir']
    pages = chunk_info['pages']
    
    print(f"\n{'='*60}")
    print(f"📦 CHUNK {chunk_num} (Pages {pages[0]}-{pages[1]})")
    print(f"{'='*60}")
    
    try:
        # Step 1: PDF → Text
        print(f"📄 Step 1/3: Converting PDF to text...")
        examples = load_example_pairs(state_code)
        conversion_prompt = load_conversion_prompt(state_code)
        
        chunk_text = process_pdf_with_claude(client, chunk_pdf_path, examples, conversion_prompt)
        
        if not chunk_text:
            return {
                'chunk_num': chunk_num,
                'success': False,
                'error': 'PDF to text conversion failed',
                'text': None
            }
        
        # Post-process: Remove unwanted tags (same as normal workflow)
        print(f"🔧 Post-processing: Removing unwanted tags...")
        from pdf_to_text_llm import remove_unwanted_tags
        chunk_text = remove_unwanted_tags(chunk_text)
        
        chunk_text_path = os.path.join(chunk_dir, f'chunk_{chunk_num}_text.txt')
        with open(chunk_text_path, 'w', encoding='utf-8') as f:
            f.write(chunk_text)
        print(f"✅ Saved text to {os.path.basename(chunk_text_path)} ({len(chunk_text)} chars)")
        
        # Step 2: PDF → DOCX → Summary (using pipeline)
        print(f"📋 Step 2/3: Generating summary...")
        
        # Use the pipeline to convert PDF to DOCX and generate summary
        pipeline = PDFToDocxAnalysisPipeline(chunk_pdf_path, chunk_dir, state_code=state_code)
        docx_results = pipeline.run_complete_pipeline()
        
        if not docx_results or not docx_results['success']:
            return {
                'chunk_num': chunk_num,
                'success': False,
                'error': 'PDF to DOCX conversion or summary generation failed',
                'text': None
            }
        
        chunk_summary_path = docx_results['summary_path']
        
        # Check if summary was generated (it will be None if no formatted items found)
        if chunk_summary_path and os.path.exists(chunk_summary_path):
            print(f"✅ Saved summary to {os.path.basename(chunk_summary_path)}")
            
            # Step 3: LLM Verification (skip filtering - chunk already has small summary)
            print(f"✅ Step 3/3: Verifying with LLM...")
            verified_text = verify_fl_tags_with_llm(
                chunk_summary_path,
                chunk_text_path,
                state_code,
                client,
                skip_filtering=True  # Skip filtering for chunks
            )
            
            if not verified_text:
                print(f"⚠️ LLM verification failed, using original text")
                verified_text = chunk_text  # Return unverified text as fallback
        else:
            # No formatted items found - skip verification and use original text
            print(f"⏭️ No formatted items found - skipping verification, using original text")
            verified_text = chunk_text
        
        # Save verified output
        chunk_verified_path = os.path.join(chunk_dir, f'chunk_{chunk_num}_verified.txt')
        with open(chunk_verified_path, 'w', encoding='utf-8') as f:
            f.write(verified_text)
        print(f"✅ Saved verified text to {os.path.basename(chunk_verified_path)} ({len(verified_text)} chars)")
        
        return {
            'chunk_num': chunk_num,
            'success': True,
            'text': verified_text,
            'pages': pages
        }
        
    except Exception as e:
        logging.error(f"Error processing chunk {chunk_num}: {e}")
        return {
            'chunk_num': chunk_num,
            'success': False,
            'error': str(e),
            'text': None
        }


def smart_merge_chunks(chunk_texts):
    """
    Intelligently merge chunk texts by:
    1. Detecting and removing duplicate content at chunk boundaries
    2. Merging split sentences across chunk boundaries
    
    This approach doesn't rely on hardcoded patterns - it detects duplicates
    by comparing content overlap between consecutive chunks.
    
    Args:
        chunk_texts (list): List of text strings from each chunk
        
    Returns:
        str: Smartly merged text
    """
    import re
    
    if not chunk_texts:
        return ""
    
    if len(chunk_texts) == 1:
        return chunk_texts[0]
    
    print(f"\n🧠 Smart merging {len(chunk_texts)} chunks...")
    
    merged_chunks = []
    
    for i, current_chunk in enumerate(chunk_texts):
        if i == 0:
            # First chunk - keep as is
            merged_chunks.append(current_chunk.rstrip())
            print(f"  Chunk 1: {len(current_chunk)} chars (kept as-is)")
        else:
            # For subsequent chunks, detect and remove duplicate content
            lines = current_chunk.split('\n')
            
            # Get previous merged content
            previous_content = merged_chunks[-1]
            
            # Find duplicate lines at the start of current chunk
            start_idx = 0
            removed_lines = []
            
            for j, line in enumerate(lines):
                line_stripped = line.strip()
                
                if not line_stripped:
                    # Skip empty lines at the beginning
                    start_idx = j + 1
                    continue
                
                # Check if this line (or very similar line) exists in previous content
                # This catches duplicate headers, repeated text, etc.
                if line_stripped in previous_content:
                    # Exact match found in previous chunk - likely a duplicate header
                    removed_lines.append(line_stripped[:50])  # Store first 50 chars for logging
                    start_idx = j + 1
                else:
                    # Check for high similarity (>80% match) using word overlap
                    line_words = set(line_stripped.lower().split())
                    if len(line_words) >= 3:  # Only check lines with meaningful content
                        # Check last 10 lines of previous content for similar lines
                        prev_lines = previous_content.split('\n')[-10:]
                        for prev_line in prev_lines:
                            prev_words = set(prev_line.strip().lower().split())
                            if len(prev_words) >= 3:
                                # Calculate word overlap
                                overlap = len(line_words & prev_words)
                                similarity = overlap / max(len(line_words), len(prev_words))
                                
                                if similarity > 0.8:  # 80% similar
                                    removed_lines.append(line_stripped[:50])
                                    start_idx = j + 1
                                    break
                        else:
                            # No similar line found - this is new content
                            break
                    else:
                        # Short line, but not found in previous - keep it
                        break
            
            # Get the cleaned chunk (without duplicate content)
            cleaned_chunk = '\n'.join(lines[start_idx:]).lstrip()
            
            if removed_lines:
                print(f"  Chunk {i+1}: Removed {len(removed_lines)} duplicate line(s)")
            
            if not cleaned_chunk:
                print(f"  Chunk {i+1}: Empty after header removal, skipping")
                continue
            
            # Check if previous chunk ended mid-sentence
            previous_chunk = merged_chunks[-1]
            previous_lines = previous_chunk.split('\n')
            last_line = previous_lines[-1].rstrip() if previous_lines else ""
            
            # Detect incomplete sentences using multiple heuristics
            ends_mid_sentence = False
            
            if last_line:
                # Remove tags for analysis
                last_line_clean = re.sub(r'\[([AD])>([^\]]*?)<\1\]', r'\2', last_line)
                
                # Heuristic 1: Ends with comma
                if last_line_clean.rstrip().endswith(','):
                    ends_mid_sentence = True
                    print(f"  → Detected comma at end of previous chunk")
                
                # Heuristic 2: Doesn't end with sentence-ending punctuation
                elif not re.search(r'[.!?:;]\s*$', last_line_clean):
                    # Check last few words for incomplete patterns
                    words = last_line_clean.split()
                    if len(words) >= 2:
                        last_words = ' '.join(words[-3:]).lower() if len(words) >= 3 else ' '.join(words[-2:]).lower()
                        
                        # Common incomplete patterns in legal text
                        incomplete_patterns = [
                            r'\band\s*$', r'\bor\s*$', r'\bof\s*$', r'\bin\s*$', r'\bto\s*$',
                            r'\bthe\s*$', r'\ba\s*$', r'\ban\s*$', r'\bfor\s*$', r'\bwith\s*$',
                            r'\bby\s*$', r'\bas\s*$', r'\bat\s*$', r'\bfrom\s*$', r'\bon\s*$',
                            r'\bunder\s*$', r'\bpursuant\s*$', r'\bshall\s*$', r'\bmay\s*$',
                            r'\bwhich\s*$', r'\bthat\s*$', r'\bsuch\s*$', r'\bsaid\s*$',
                            r'\bnot\s+less\s*$', r'\bnot\s+more\s*$', r'\bper\s+cent\s*$'
                        ]
                        
                        for pattern in incomplete_patterns:
                            if re.search(pattern, last_words):
                                ends_mid_sentence = True
                                matched = re.search(pattern, last_words).group(0).strip()
                                print(f"  → Detected incomplete ending: '...{matched}'")
                                break
            
            if ends_mid_sentence:
                # Merge with space (no newline between chunks)
                first_line_of_chunk = cleaned_chunk.split('\n')[0] if '\n' in cleaned_chunk else cleaned_chunk
                
                # Remove the last line from previous chunk temporarily
                previous_without_last = '\n'.join(previous_lines[:-1]) if len(previous_lines) > 1 else ""
                
                # Merge the last line with first line of current chunk
                merged_line = last_line + ' ' + first_line_of_chunk.lstrip()
                
                # Remove first line from current chunk
                remaining_chunk = '\n'.join(cleaned_chunk.split('\n')[1:]) if '\n' in cleaned_chunk else ""
                
                # Reconstruct
                if previous_without_last:
                    merged_chunks[-1] = previous_without_last + '\n' + merged_line
                else:
                    merged_chunks[-1] = merged_line
                
                if remaining_chunk:
                    merged_chunks[-1] += '\n' + remaining_chunk
                
                print(f"  Chunk {i+1}: {len(cleaned_chunk)} chars (merged with previous - sentence split detected)")
            else:
                # Normal merge with double newline separator
                merged_chunks.append(cleaned_chunk.rstrip())
                print(f"  Chunk {i+1}: {len(cleaned_chunk)} chars (normal merge)")
    
    # Join all chunks with double newline
    final_text = '\n\n'.join(merged_chunks)
    
    print(f"✅ Smart merge complete: {len(final_text)} characters")
    
    return final_text


def verify_with_pdf_chunking(pdf_path, state_code, client, session_dir):
    """
    Main orchestrator for PDF-level chunked verification.
    
    Workflow:
    1. Split PDF into 5-page chunks
    2. For each chunk: PDF→Text, PDF→DOCX→Summary, LLM verify
    3. Merge all verified chunks with simple concatenation
    
    Args:
        pdf_path (str): Path to the original PDF
        state_code (str): State code (FL, CT, etc.)
        client: Anthropic API client
        session_dir (str): Session directory for output files
        
    Returns:
        str: Final merged verified text
    """
    import os
    import shutil
    import uuid
    from pdf_to_text_llm import get_pdf_page_count, extract_pdf_pages_to_file
    
    print("\n" + "="*60)
    print("🔄 PDF-LEVEL CHUNKED VERIFICATION")
    print("="*60)
    
    # Get page count
    page_count = get_pdf_page_count(pdf_path)
    if page_count is None:
        logging.error("Failed to get PDF page count")
        return None
    
    print(f"📄 Document: {page_count} pages")
    
    # Create chunks directory
    chunks_base = os.path.join(session_dir, f"chunks_{uuid.uuid4().hex[:8]}")
    os.makedirs(chunks_base, exist_ok=True)
    print(f"📁 Chunks directory: {chunks_base}")
    
    # Phase 1: Split PDF into chunks
    print(f"\n📦 Phase 1: Splitting PDF into 5-page chunks...")
    chunks = []
    pages_per_chunk = 5
    
    # Calculate chunk ranges with smart last chunk handling
    chunk_ranges = []
    start_page = 1
    
    while start_page <= page_count:
        end_page = min(start_page + pages_per_chunk - 1, page_count)
        remaining_pages = page_count - end_page
        
        # If last chunk would be 1-2 pages, merge it with current chunk
        if 0 < remaining_pages <= 2:
            end_page = page_count  # Extend current chunk to include the small remainder
            print(f"📌 Merging last {remaining_pages} page(s) into current chunk to avoid tiny chunk")
        
        chunk_ranges.append((start_page, end_page))
        start_page = end_page + 1
    
    print(f"📊 Will create {len(chunk_ranges)} chunks from {page_count} pages")
    
    # Create chunks from calculated ranges
    for idx, (start_page, end_page) in enumerate(chunk_ranges):
        chunk_num = idx + 1
        page_span = end_page - start_page + 1
        
        # Create chunk directory
        chunk_dir = os.path.join(chunks_base, f"chunk_{chunk_num}")
        os.makedirs(chunk_dir, exist_ok=True)
        
        # Extract pages to new PDF
        chunk_pdf_path = os.path.join(chunk_dir, f"chunk_{chunk_num}.pdf")
        success = extract_pdf_pages_to_file(pdf_path, start_page, end_page, chunk_pdf_path)
        
        if not success:
            logging.error(f"Failed to extract chunk {chunk_num} (pages {start_page}-{end_page})")
            continue
        
        chunks.append({
            'chunk_num': chunk_num,
            'pdf_path': chunk_pdf_path,
            'chunk_dir': chunk_dir,
            'pages': (start_page, end_page)
        })
        
        if page_span > pages_per_chunk:
            print(f"  ✓ Chunk {chunk_num}: Pages {start_page}-{end_page} ({page_span} pages - extended)")
        else:
            print(f"  ✓ Chunk {chunk_num}: Pages {start_page}-{end_page} ({page_span} pages)")
    
    print(f"✅ Created {len(chunks)} chunks")
    
    if not chunks:
        logging.error("No chunks were created")
        return None
    
    # Phase 2: Process each chunk
    print(f"\n🔄 Phase 2: Processing {len(chunks)} chunks...")
    chunk_results = []
    
    for chunk_info in chunks:
        result = process_verification_chunk(chunk_info, state_code, client, session_dir)
        chunk_results.append(result)
        
        if not result['success']:
            logging.warning(f"Chunk {result['chunk_num']} failed: {result.get('error')}")
    
    # Check if any chunks succeeded
    successful_chunks = [r for r in chunk_results if r['success']]
    if not successful_chunks:
        logging.error("All chunks failed processing")
        return None
    
    # Phase 3: Smart merge results
    print(f"\n🔗 Phase 3: Smart merging {len(successful_chunks)} chunks...")
    chunk_texts = [r['text'] for r in successful_chunks]
    merged_text = smart_merge_chunks(chunk_texts)
    
    print(f"✅ Smart merge complete ({len(merged_text)} characters)")
    
    # Phase 4: Apply final tag consolidation after merging
    print(f"\n🔧 Phase 4: Applying tag consolidation to merged text...")
    from tag_consolidator import consolidate_tags
    merged_text = consolidate_tags(merged_text)
    print(f"✅ Tag consolidation complete ({len(merged_text)} characters)")
    
    # Cleanup temp files (optional - comment out for debugging)
    # print(f"\n🧹 Cleaning up temporary files...")
    # shutil.rmtree(chunks_base)
    
    print(f"\n{'='*60}")
    print(f"🎉 PDF-LEVEL CHUNKING COMPLETE")
    print(f"{'='*60}")
    print(f"   Total chunks: {len(chunks)}")
    print(f"   Successful: {len(successful_chunks)}")
    print(f"   Failed: {len(chunks) - len(successful_chunks)}")
    print(f"   Pages processed: {page_count}")
    print(f"   Output size: {len(merged_text)} characters")
    print(f"{'='*60}\n")
    
    return merged_text


def verify_fl_tags_with_llm(summary_file_path, output_text_file_path, state_code="FL", client=None, skip_filtering=False):
    """
    Main function to verify tags using Claude LLM with state-specific prompts.
    Automatically uses chunking for large documents.
    
    Args:
        summary_file_path (str): Path to the summary file
        output_text_file_path (str): Path to the output text file
        state_code (str): State code for state-specific verification (default: "FL")
        client: Anthropic client instance
        skip_filtering (bool): If True, skip state-specific filtering (used in chunking)
    
    Returns:
        str: Corrected text, or original text for DC (no verification needed)
    """
    
    print("🤖 Starting LLM-based Tag Verification...")
    print("=" * 60)
    print(f"State: {state_code}")
    print(f"Summary file: {summary_file_path}")
    print(f"Output text file: {output_text_file_path}")
    
    # Skip verification for DC and IN states - just return the original text
    if state_code in ["DC", "IN"]:
        print(f"⏭️ {state_code} state doesn't require tag verification")
        print("📄 Returning original text without modifications")
        
        # Read and return original text
        output_text = read_file_content(output_text_file_path)
        if output_text:
            print(f"✅ Retrieved original {state_code} text ({len(output_text)} characters)")
            return output_text
        else:
            print("❌ Failed to read output text file")
            return None
    
    # Get API client
    if not client:
        client = get_api_key()
        if not client:
            return None
    
    # Read summary file
    print("\n📖 Reading summary file...")
    summary_content = read_file_content(summary_file_path)
    if not summary_content:
        print("❌ Failed to read summary file")
        return None
    
    # Extract the formatted items section with support for new contextual format
    formatted_summary = extract_formatted_items_summary(summary_content)
    
    if "contextual locations:" in formatted_summary:
        print(f"✅ Using new contextual location format ({len(formatted_summary.split(chr(10)))} lines)")
        print("🎯 Contextual locations will help Claude identify exact text positions")
    else:
        print(f"✅ Using standard format ({len(formatted_summary.split(chr(10)))} lines)")
    
    # Additional validation for the summary format
    item_count = len([line for line in formatted_summary.split('\n') if line.strip() and line[0:3].replace('.', '').replace(' ', '').isdigit()])
    print(f"📊 Found {item_count} formatted items to verify")
    
    # NEVER filter summary for standard documents - filtering is only for PDF-level chunking
    # The skip_filtering parameter is for a different purpose (skipping in nested calls)
    # Summary filtering should only happen inside process_chunk_for_verification() for large chunks
    print(f"ℹ️ Using full summary for verification (no filtering for standard documents)")
    
    # Read output text file
    print("📖 Reading output text file...")
    output_text = read_file_content(output_text_file_path)
    if not output_text:
        print("❌ Failed to read output text file")
        return None
    
    print(f"✅ Read output text ({len(output_text)} characters)")
    
    # Check if document needs chunking
    print("\n🔍 Checking if document needs chunking...")
    chunker = DocumentChunker(max_tokens=50000)
    needs_chunking, text_tokens, total_tokens = chunker.should_chunk_document(output_text, formatted_summary)
    
    if needs_chunking:
        print(f"📊 Document requires chunking:")
        print(f"   Text: ~{text_tokens:,} tokens")
        print(f"   Total with summary: ~{total_tokens:,} tokens")
        print(f"   Threshold: 150,000 tokens")
        print("🔄 Switching to chunked verification...")
        
        # Use chunked verification
        return verify_with_chunking(summary_file_path, output_text_file_path, state_code, client)
    
    # Document is small enough for single verification
    print(f"✅ Document size OK for single verification (~{total_tokens:,} tokens)")
    
    # CT-specific: Wrap bracketed text with [D>...<D] tags BEFORE LLM verification
    if state_code == "CT":
        print("🔧 Applying CT-specific bracket wrapping (pre-verification)...")
        output_text = wrap_ct_bracketed_text(output_text)
        print("✅ CT bracket wrapping complete")
    
    # Create verification prompt using state-specific template
    print(f"🔨 Creating {state_code}-specific verification prompt...")
    prompt = create_verification_prompt(formatted_summary, output_text, state_code)
    
    if not prompt:
        print("❌ Failed to create verification prompt")
        return None
    
    print(f"✅ Prompt created ({len(prompt)} characters)")
    
    # Call Claude API
    print("🚀 Sending to Claude for verification...")
    claude_response = call_claude_api(prompt, client)
    
    if not claude_response:
        print("❌ Failed to get response from Claude")
        return None
    
    print(f"✅ Received response from Claude ({len(claude_response)} characters)")
    
    # Parse the response to separate corrected text and verification CSV
    corrected_text, verification_csv = parse_verification_response(claude_response)
    
    if verification_csv:
        print("📊 Verification CSV detected in response")
        # Save the verification CSV to a separate file
        csv_file_path = save_verification_log(verification_csv, output_text_file_path)
        if csv_file_path:
            print(f"💾 Verification CSV saved to: {csv_file_path}")
    else:
        print("📄 No verification CSV found in response")
    
    # Apply Python-based tag consolidation (more reliable than LLM)
    print("🔧 Applying Python-based tag consolidation...")
    from tag_consolidator import consolidate_tags
    corrected_text = consolidate_tags(corrected_text)
    print("✅ Tag consolidation complete")
    
    return corrected_text

def save_corrected_file(corrected_text, original_file_path):
    """
    Save the corrected text to a new file with proper header.
    """
    
    original_path = Path(original_file_path)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    
    # Create new filename
    new_filename = f"{original_path.stem}_LLM_CORRECTED_{timestamp}{original_path.suffix}"
    new_file_path = original_path.parent / new_filename
    
    try:
        with open(new_file_path, 'w', encoding='utf-8') as f:
            f.write(corrected_text)
        
        print(f"✅ Corrected text saved to: {new_file_path}")
        return str(new_file_path)
        
    except Exception as e:
        print(f"❌ Error saving corrected file: {e}")
        return None

def compare_files(original_path, corrected_path, state_code="FL"):
    """
    Compare original and corrected files to show changes made, including removed tags.
    
    Args:
        original_path: Path to original file
        corrected_path: Path to corrected file  
        state_code: State code for context
    """
    
    try:
        original_text = read_file_content(original_path)
        corrected_text = read_file_content(corrected_path)
        
        if not original_text or not corrected_text:
            print("❌ Could not compare files")
            return
        
        # Simple comparison
        original_lines = original_text.splitlines()
        corrected_lines = corrected_text.splitlines()
        
        print(f"\n📊 FILE COMPARISON:")
        print(f"Original lines: {len(original_lines)}")
        print(f"Corrected lines: {len(corrected_lines)}")
        print(f"Original size: {len(original_text)} characters")
        print(f"Corrected size: {len(corrected_text)} characters")
        
        # Skip tag counting for DC and IN since they don't use tags/verification
        if state_code in ["DC", "IN"]:
            reason = "doesn't use formatting tags" if state_code == "DC" else "verification skipped per configuration"
            print(f"\n📝 {state_code} STATE: No tag verification performed ({reason})")
            if original_text == corrected_text:
                print(f"✅ Files are identical (as expected for {state_code})")
            else:
                print(f"⚠️  Files differ (unexpected for {state_code})")
        else:
            # Count tags for other states
            original_a_tags = original_text.count('[A>')
            corrected_a_tags = corrected_text.count('[A>')
            original_d_tags = original_text.count('[D>')
            corrected_d_tags = corrected_text.count('[D>')
            
            print(f"\n🏷️  TAG COMPARISON:")
            print(f"[A> tags - Original: {original_a_tags}, Corrected: {corrected_a_tags} (Δ{corrected_a_tags - original_a_tags:+d})")
            print(f"[D> tags - Original: {original_d_tags}, Corrected: {corrected_d_tags} (Δ{corrected_d_tags - original_d_tags:+d})")
            print(f"Total tags - Original: {original_a_tags + original_d_tags}, Corrected: {corrected_a_tags + corrected_d_tags}")
            
            # Analyze changes
            total_change = abs((corrected_a_tags - original_a_tags)) + abs((corrected_d_tags - original_d_tags))
            if total_change == 0:
                print("✅ No tag changes made")
            else:
                print(f"🔄 Total tag changes: {total_change}")
        
    except Exception as e:
        print(f"❌ Error comparing files: {e}")

def create_verification_report(summary_file, original_file, corrected_file, report_file, state_code="FL"):
    """
    Create a verification report.
    
    Args:
        summary_file: Path to summary file
        original_file: Path to original file
        corrected_file: Path to corrected file
        report_file: Path to report file
        state_code: State code for context
    """
    
    try:
        with open(report_file, 'w', encoding='utf-8') as f:
            f.write(f"LLM-BASED {state_code} TAG VERIFICATION REPORT\n")
            f.write("=" * 60 + "\n\n")
            f.write(f"Report Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"State: {state_code}\n\n")
            
            f.write("FILES PROCESSED:\n")
            f.write(f"Summary Reference: {summary_file}\n")
            f.write(f"Original Output: {original_file}\n")
            f.write(f"Corrected Output: {corrected_file}\n\n")
            
            if state_code in ["DC", "IN"]:
                bypass_reason = "No tagging required" if state_code == "DC" else "Verification skipped per configuration"
                tag_note = "don't use formatting tags" if state_code == "DC" else "verification not performed"
                f.write(f"VERIFICATION METHOD: {state_code} Bypass ({bypass_reason})\n")
                f.write(f"TAG RULES APPLIED: None - {state_code} documents {tag_note}\n\n")
                
                f.write("PROCESS:\n")
                f.write(f"1. {state_code} state identified - no verification needed\n")
                f.write("2. Original text copied without modification\n") 
                f.write("3. No tags added or removed\n")
                f.write("4. File saved to maintain consistent structure\n\n")
                
                f.write("RESULTS:\n")
                f.write("DC processing complete - no tags added (as expected)\n")
                f.write("File ready for legislative document processing\n\n")
            else:
                f.write("VERIFICATION METHOD: Claude LLM\n")
                f.write(f"TAG RULES APPLIED: {state_code}-specific formatting rules\n\n")
                
                f.write("PROCESS:\n")
                f.write("1. Extracted formatted items from summary file\n")
                f.write("2. Created specialized verification prompt\n") 
                f.write("3. Used Claude to intelligently add missing tags\n")
                f.write("4. Preserved existing correct formatting\n")
                f.write(f"5. Applied {state_code}-specific tag rules only\n\n")
                
                # Add comparison data if available
                original_text = read_file_content(original_file)
                corrected_text = read_file_content(corrected_file)
                
                if original_text and corrected_text:
                    original_a_tags = original_text.count('[A>')
                    corrected_a_tags = corrected_text.count('[A>')
                    original_d_tags = original_text.count('[D>')
                    corrected_d_tags = corrected_text.count('[D>')
                    
                    f.write("RESULTS:\n")
                    f.write(f"[A> tags added: {corrected_a_tags - original_a_tags}\n")
                    f.write(f"[D> tags added: {corrected_d_tags - original_d_tags}\n")
                    f.write(f"Total new tags: {(corrected_a_tags - original_a_tags) + (corrected_d_tags - original_d_tags)}\n\n")
                
                f.write("QUALITY: LLM-based verification provides contextual understanding\n")
                f.write("and prevents over-tagging issues common with algorithmic approaches.\n")
            
            f.write(f"\nThis file can be used for {state_code} legislative document processing.\n")
        
        print(f"📋 Verification report saved to: {report_file}")
        
    except Exception as e:
        print(f"❌ Error creating verification report: {e}")

# Main execution
if __name__ == "__main__":
    try:
        # File paths - update these for your specific files
        summary_file = r"../data/output/verify/export2025-11-11T15-44-41_summary_20251111_154451.txt"
        output_text_file = r"../data/output/IA/IA HSB117.txt"  # Original output file
        
        print("🤖 LLM-Based Tag Verification System")
        print("=" * 60)
        
        # Verify files exist
        if not os.path.exists(summary_file):
            print(f"❌ Summary file not found: {summary_file}")
            exit(1)
            
        if not os.path.exists(output_text_file):
            print(f"❌ Output text file not found: {output_text_file}")
            exit(1)
        
        # Run LLM verification with state code
        state_code = "FL"  # You can make this dynamic later
        corrected_text = verify_fl_tags_with_llm(summary_file, output_text_file, state_code)
        
        if corrected_text:
            # Save corrected file
            corrected_file_path = save_corrected_file(corrected_text, output_text_file)
            
            if corrected_file_path:
                # Compare files
                compare_files(output_text_file, corrected_file_path)
                
                # Create report (using Temp sessions structure)
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                report_file = f"../Temp/sessions/[session-id]/output/verify/{state_code}_LLM_verification_report_{timestamp}.txt"
                create_verification_report(summary_file, output_text_file, corrected_file_path, report_file)
                
                print(f"\n🎯 LLM VERIFICATION COMPLETE!")
                print(f"📁 Corrected file: {corrected_file_path}")
                print(f"📋 Report: {report_file}")
                print("=" * 60)
            else:
                print("❌ Failed to save corrected file")
        else:
            print("❌ LLM verification failed")
            
    except Exception as e:
        print(f"❌ An error occurred: {e}")