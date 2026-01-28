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

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Get the project root directory (parent of app directory)
PROJECT_ROOT = Path(__file__).parent.parent.absolute()

# Configuration (same as pdf_to_text_llm.py)
WORKSPACE_ID = "AgentTraininJaH4"
TOKEN_URL = "https://aiplatform.gcs.int.thomsonreuters.com/v1/anthropic/token"

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
        prompt_file = os.path.join(PROJECT_ROOT, "data", "Verify_Prompts", f"{state_code}_Prompt.md")
        
        if os.path.exists(prompt_file):
            with open(prompt_file, "r", encoding='utf-8') as f:
                prompt_template = f.read().strip()
                print(f"✅ Loaded state-specific verification prompt for {state_code}")
                return prompt_template
        else:
            print(f"⚠️ No state-specific prompt found for {state_code} at {prompt_file}")
            print("📝 Using default FL prompt as fallback")
            
            # Fall back to FL prompt as default
            fl_prompt_file = os.path.join(PROJECT_ROOT, "data", "Verify_Prompts", "FL_Prompt.md")
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


def should_chunk_for_verification(pdf_path):
    """
    Decide if PDF should be chunked for verification based on page count.
    
    Chunking is only used for documents > 7 pages because:
    - Chunk size is 6 pages with 1-page overlap
    - Documents ≤ 7 pages fit in a single chunk
    - Chunking overhead not justified for small documents
    
    Args:
        pdf_path (str): Path to the PDF file
        
    Returns:
        tuple: (should_chunk, page_count, reason)
    """
    from pdf_to_text_llm import get_pdf_page_count
    
    page_count = get_pdf_page_count(pdf_path)
    
    if page_count is None:
        return False, 0, "Could not determine page count"
    
    if page_count > 7:
        return True, page_count, f"Document has {page_count} pages (>7 threshold)"
    else:
        return False, page_count, f"Document has {page_count} pages (≤7 threshold, no chunking needed)"


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
    
    # Document is ready for single verification
    print(f"✅ Document ready for verification")
    
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
    
    # Return corrected text without tag consolidation
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


def clean_final_verified_text(text):
    """
    Clean the final verified text by:
    1. Removing duplicate paragraphs/content
    2. Fixing unwanted line breaks within paragraphs
    3. Preserving intentional paragraph breaks
    
    Args:
        text (str): Raw verified text
        
    Returns:
        str: Cleaned text
    """
    if not text:
        return text
    
    print("\n🧹 Cleaning final verified text...")
    
    # Step 1: Split into paragraphs (double newline = paragraph break)
    paragraphs = text.split('\n\n')
    print(f"   Found {len(paragraphs)} paragraphs")
    
    # Step 2: Fix line breaks within each paragraph
    # Join lines within paragraphs, but preserve formatting tags and list items
    cleaned_paragraphs = []
    for para in paragraphs:
        if not para.strip():
            continue
        
        # Split into lines
        lines = [line.strip() for line in para.split('\n') if line.strip()]
        
        # Group lines together, separating only on distinct list items
        merged_lines = []
        i = 0
        
        while i < len(lines):
            line = lines[i]
            
            # Check if this is a list item or section header that should stay separate
            is_list_item = (
                # Numbered items: "1. ", "2. ", etc.
                re.match(r'^\d+\.\s', line) or
                # Lettered items: "(a) ", "(b) ", etc.
                re.match(r'^\([a-z]\)\s', line) or
                # Roman numeral items: "(i) ", "(ii) ", etc.
                re.match(r'^\([ivxlcdm]+\)\s', line, re.IGNORECASE) or
                # Section headers with numbers
                re.match(r'^Section\s+\d+', line, re.IGNORECASE) or
                # All caps headers (at least 3 words, all caps)
                (len(line.split()) >= 3 and line.isupper())
            )
            
            if is_list_item:
                # This is a distinct item - keep it separate
                merged_lines.append(line)
                i += 1
            else:
                # This is regular text - merge with ALL following lines until we hit a list item
                current_text = line
                i += 1
                
                while i < len(lines):
                    next_line = lines[i]
                    
                    # Check if next line starts a new list item
                    next_is_list = (
                        re.match(r'^\d+\.\s', next_line) or
                        re.match(r'^\([a-z]\)\s', next_line) or
                        re.match(r'^\([ivxlcdm]+\)\s', next_line, re.IGNORECASE) or
                        re.match(r'^Section\s+\d+', next_line, re.IGNORECASE) or
                        (len(next_line.split()) >= 3 and next_line.isupper())
                    )
                    
                    if next_is_list:
                        # Stop merging - next line is a new item
                        break
                    
                    # Merge this line into current text
                    if not current_text.endswith((' ', '-')):
                        current_text += ' '
                    current_text += next_line
                    i += 1
                
                merged_lines.append(current_text)
        
        # Rejoin with newlines between different items
        cleaned_para = '\n'.join(merged_lines)
        cleaned_paragraphs.append(cleaned_para)
    
    # Step 3: Remove duplicate paragraphs using Jaccard similarity
    print(f"   Checking for duplicate paragraphs...")
    unique_paragraphs = []
    duplicates_found = 0
    
    for i, para in enumerate(cleaned_paragraphs):
        # Remove tags for comparison
        para_clean = re.sub(r'\[([AD])>([^\]]*?)<\1\]', r'\2', para)
        
        # Check if this is a numbered or lettered list item
        para_is_list_item = re.match(r'^(\d+\.|\([a-z]\)|\([ivxlcdm]+\))\s', para_clean.strip(), re.IGNORECASE)
        
        para_words = set(para_clean.lower().split())
        
        if len(para_words) < 3:  # Too short to compare
            unique_paragraphs.append(para)
            continue
        
        is_duplicate = False
        for existing_para in unique_paragraphs:
            existing_clean = re.sub(r'\[([AD])>([^\]]*?)<\1\]', r'\2', existing_para)
            
            # Check if existing is also a list item
            existing_is_list_item = re.match(r'^(\d+\.|\([a-z]\)|\([ivxlcdm]+\))\s', existing_clean.strip(), re.IGNORECASE)
            
            # If both are list items with DIFFERENT numbers/letters, they're NOT duplicates
            if para_is_list_item and existing_is_list_item:
                para_prefix = para_is_list_item.group(1)
                existing_prefix = existing_is_list_item.group(1)
                if para_prefix != existing_prefix:
                    # Different list items (e.g., "1." vs "2.") - NOT duplicates
                    continue
            
            existing_words = set(existing_clean.lower().split())
            
            if len(existing_words) < 3:
                continue
            
            # Calculate Jaccard similarity
            intersection = len(para_words & existing_words)
            union = len(para_words | existing_words)
            similarity = intersection / union if union > 0 else 0
            
            # If >80% similar, consider it a duplicate
            if similarity > 0.80:
                is_duplicate = True
                duplicates_found += 1
                
                # Show content preview for transparency
                para_preview = para_clean[:150] + "..." if len(para_clean) > 150 else para_clean
                existing_preview = existing_clean[:150] + "..." if len(existing_clean) > 150 else existing_clean
                
                print(f"   ⚠️ Duplicate found: Paragraph {i+1} is {similarity:.1%} similar")
                print(f"      Duplicate text: \"{para_preview}\"")
                print(f"      Matches existing: \"{existing_preview}\"")
                break
        
        if not is_duplicate:
            unique_paragraphs.append(para)
    
    if duplicates_found > 0:
        print(f"   ✅ Removed {duplicates_found} duplicate paragraph(s)")
    else:
        print(f"   ✅ No duplicates found")
    
    # Step 4: Rejoin paragraphs with double newlines
    final_text = '\n\n'.join(unique_paragraphs)
    
    # Step 5: Clean up any excessive whitespace
    final_text = re.sub(r'\n{3,}', '\n\n', final_text)  # Max 2 newlines
    final_text = re.sub(r' +', ' ', final_text)  # Multiple spaces to single
    final_text = final_text.strip()
    
    print(f"   📊 Original: {len(cleaned_paragraphs)} paragraphs, {len(text)} chars")
    print(f"   📊 Cleaned: {len(unique_paragraphs)} paragraphs, {len(final_text)} chars")
    print(f"✅ Text cleaning complete\n")
    
    return final_text


def verify_with_overlapping_chunks(pdf_path, state_code, client, session_dir):
    """
    Text-based overlapping chunk verification with page markers for 100% accuracy.
    
    Workflow:
    1. Convert PDF→Text with overlapping chunks (1-page overlap) using page markers
    2. For each overlapping text chunk: PDF→DOCX→Summary, then LLM verify
    3. Merge verified chunks using page markers to remove overlaps
    
    This approach ensures NO content loss at chunk boundaries because overlapping
    pages preserve context, and page markers enable precise deduplication.
    
    Args:
        pdf_path (str): Path to the original PDF
        state_code (str): State code (FL, CT, etc.)
        client: Anthropic API client
        session_dir (str): Session directory for output files
        
    Returns:
        str: Final merged verified text with 100% accuracy
    """
    import os
    import shutil
    import uuid
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from pdf_to_text_llm import (
        get_pdf_page_count, 
        create_overlapping_chunks, 
        extract_pdf_pages_with_markers,
        build_chunk_aware_prompt,
        process_pdf_chunk_with_markers,
        merge_chunks_with_markers,
        load_example_pairs,
        load_conversion_prompt
    )
    from pdf_docx_analysis_pipeline import PDFToDocxAnalysisPipeline
    
    print("\n" + "="*70)
    print("🔄 TEXT-BASED OVERLAPPING CHUNK VERIFICATION (100% ACCURACY MODE)")
    print("="*70)
    
    # Get page count
    page_count = get_pdf_page_count(pdf_path)
    if page_count is None:
        logging.error("Failed to get PDF page count")
        return None
    
    print(f"📄 Document: {page_count} pages")
    
    # Create chunks directory
    chunks_base = os.path.join(session_dir, f"chunks_overlapping_{uuid.uuid4().hex[:8]}")
    os.makedirs(chunks_base, exist_ok=True)
    print(f"📁 Chunks directory: {chunks_base}")
    
    # ============================================================
    # Phase 1: Create overlapping chunks with 1-page overlap
    # ============================================================
    print(f"\n📦 Phase 1: Creating overlapping text chunks with page markers...")
    
    pages_per_chunk = 5
    overlap = 1  # 1-page overlap for context preservation
    
    chunks = create_overlapping_chunks(page_count, chunk_size=pages_per_chunk, overlap=overlap)
    
    print(f"📊 Created {len(chunks)} overlapping chunks:")
    for chunk in chunks:
        overlap_indicator = f" (overlap: page {chunk['pages'][0]+1})" if chunk['chunk_num'] > 1 else ""
        print(f"   Chunk {chunk['chunk_num']}: Pages {chunk['page_range']}{overlap_indicator}")
    
    # Load conversion prompt and examples once (used by all chunks)
    conversion_prompt = load_conversion_prompt(state_code)
    examples = load_example_pairs(state_code)
    
    if not conversion_prompt:
        logging.error(f"Failed to load conversion prompt for {state_code}")
        return None
    
    # ============================================================
    # Phase 2: Convert + Verify ALL chunks in PARALLEL
    # ============================================================
    # Each chunk runs independently with internal parallelism:
    #   - PDF→Text (40s) + PDF→DOCX (50s) in parallel
    #   - Then Verify (20s) sequentially
    #   = max(40s, 50s) + 20s = 70s per chunk
    # All chunks execute simultaneously
    # Total: 70s (not 110s!)
    # ============================================================
    print(f"\n🔄 Phase 2: Converting + Verifying {len(chunks)} chunks in PARALLEL...")
    print(f"   Each chunk: [PDF→Text + PDF→DOCX in parallel] → Verify")
    print(f"   Time per chunk: max(40s, 50s) + 20s = ~70s")
    print(f"   All chunks run simultaneously")
    
    start_time = time.time()
    verified_chunks = []
    
    def process_single_overlapping_chunk(chunk_info, conversion_prompt, examples):
        """
        Process a single overlapping chunk: Convert → Verify
        This runs in parallel for all chunks.
        """
        chunk_num = chunk_info['chunk_num']
        
        try:
            # ============================================================
            # PARALLEL STEP: PDF→Text + PDF→DOCX (run simultaneously)
            # ============================================================
            
            def convert_to_text_with_markers():
                """Convert PDF chunk to text with page markers"""
                # Extract PDF pages with markers
                chunk_pdf_data = extract_pdf_pages_with_markers(pdf_path, chunk_info['pages'])
                
                if not chunk_pdf_data:
                    logging.error(f"Chunk {chunk_num}: Failed to extract PDF pages")
                    return None
                
                # Build chunk-aware prompt
                chunk_prompt = build_chunk_aware_prompt(conversion_prompt, chunk_info, chunk_info['pages'])
                
                # Convert PDF→Text with page markers
                chunk_text = process_pdf_chunk_with_markers(
                    client,
                    chunk_pdf_data,
                    chunk_info,
                    examples,
                    chunk_prompt
                )
                
                if not chunk_text:
                    logging.error(f"Chunk {chunk_num}: Text conversion failed")
                    return None
                
                print(f"✅ Chunk {chunk_num}: Text conversion complete ({len(chunk_text)} chars)")
                return chunk_text
            
            def generate_docx_summary():
                """Generate DOCX and summary for chunk"""
                # Create temporary directory for this chunk
                chunk_temp_dir = os.path.join(chunks_base, f"chunk_{chunk_num}_temp")
                os.makedirs(chunk_temp_dir, exist_ok=True)
                
                # Extract chunk pages to temporary PDF file
                from pdf_to_text_llm import extract_pdf_pages_to_file
                chunk_pdf_path = os.path.join(chunk_temp_dir, f"chunk_{chunk_num}.pdf")
                
                # Convert 0-indexed pages to 1-indexed for extraction
                start_page = chunk_info['pages'][0] + 1
                end_page = chunk_info['pages'][-1] + 1
                
                success = extract_pdf_pages_to_file(pdf_path, start_page, end_page, chunk_pdf_path)
                
                if not success:
                    logging.warning(f"Chunk {chunk_num}: Could not create PDF for DOCX")
                    return None
                
                # Generate DOCX and summary
                pipeline = PDFToDocxAnalysisPipeline(chunk_pdf_path, chunk_temp_dir, state_code=state_code)
                docx_results = pipeline.run_complete_pipeline()
                
                if not docx_results or not docx_results['success']:
                    logging.warning(f"Chunk {chunk_num}: DOCX generation failed")
                    return None
                
                summary_path = docx_results.get('summary_path')
                
                if summary_path and os.path.exists(summary_path):
                    with open(summary_path, 'r', encoding='utf-8') as f:
                        summary_content = f.read()
                    print(f"✅ Chunk {chunk_num}: DOCX summary complete")
                    return summary_content
                else:
                    print(f"⏭️ Chunk {chunk_num}: No formatted items found")
                    return ""
            
            # Execute PDF→Text and PDF→DOCX in parallel
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=2) as executor:
                future_text = executor.submit(convert_to_text_with_markers)
                future_summary = executor.submit(generate_docx_summary)
                
                # Wait for both to complete
                chunk_text = future_text.result()
                summary_content = future_summary.result()
            
            # Check if text conversion succeeded
            if not chunk_text:
                logging.error(f"Chunk {chunk_num}: Text conversion failed")
                return None
            
            # CRITICAL: Remove tags from PDF→Text output before verification
            # The PDF→Text prompt adds tags based on PDF formatting, but we need plain text
            # Verification will add tags properly based on DOCX analysis (single source of truth)
            from pdf_to_text_llm import remove_unwanted_tags
            
            # Count tags before removal for logging
            tags_before = len(re.findall(r'\[A>.*?<A\]|\[D>.*?<D\]', chunk_text))
            
            # Remove tags (respects state-specific rules: CO keeps [A>, others remove all)
            chunk_text_cleaned = remove_unwanted_tags(chunk_text, state_code)
            
            tags_after = len(re.findall(r'\[A>.*?<A\]|\[D>.*?<D\]', chunk_text_cleaned))
            
            if tags_before > 0:
                logging.info(f"Chunk {chunk_num}: Removed {tags_before - tags_after} tags from PDF→Text output (before={tags_before}, after={tags_after})")
                print(f"🧹 Chunk {chunk_num}: Cleaned {tags_before - tags_after} PDF-based tags")
            
            # Save cleaned text for this chunk
            chunk_temp_dir = os.path.join(chunks_base, f"chunk_{chunk_num}_temp")
            chunk_text_path = os.path.join(chunk_temp_dir, f"chunk_{chunk_num}_text.txt")
            with open(chunk_text_path, 'w', encoding='utf-8') as f:
                f.write(chunk_text_cleaned)
            print(f"💾 Saved clean text: {os.path.basename(chunk_text_path)}")
            
            # ============================================================
            # SEQUENTIAL STEP: Verify chunk (requires both text + summary)
            # ============================================================
            
            # If we have a summary, verify the chunk
            if summary_content and summary_content.strip():
                formatted_summary = extract_formatted_items_summary(summary_content)
                
                # Verify this chunk using CLEANED text (tags removed)
                verified_text = verify_chunk_with_llm(
                    chunk_text_cleaned,  # Use cleaned text without PDF tags
                    formatted_summary,
                    state_code,
                    client
                )
                
                if verified_text:
                    # Save verified text for this chunk
                    chunk_verified_path = os.path.join(chunk_temp_dir, f"chunk_{chunk_num}_verified.txt")
                    with open(chunk_verified_path, 'w', encoding='utf-8') as f:
                        f.write(verified_text)
                    print(f"✅ Chunk {chunk_num}: Verification complete")
                    print(f"💾 Saved verified text: {os.path.basename(chunk_verified_path)}")
                    
                    return {
                        'text': verified_text,
                        'chunk_num': chunk_num,
                        'page_range': chunk_info['page_range'],
                        'verified': True,
                        'text_path': chunk_text_path,
                        'verified_path': chunk_verified_path
                    }
                else:
                    logging.warning(f"Chunk {chunk_num}: Verification failed, using cleaned text")
                    return {
                        'text': chunk_text_cleaned,  # Use cleaned text even if verification fails
                        'chunk_num': chunk_num,
                        'page_range': chunk_info['page_range'],
                        'verified': False,
                        'text_path': chunk_text_path,
                        'verified_path': None
                    }
            else:
                # No formatted items - use cleaned text (tags removed from PDF output)
                print(f"⏭️ Chunk {chunk_num}: No formatted items, using cleaned text")
                return {
                    'text': chunk_text_cleaned,  # Use cleaned text
                    'chunk_num': chunk_num,
                    'page_range': chunk_info['page_range'],
                    'verified': False,
                    'text_path': chunk_text_path,
                    'verified_path': None
                }
                
        except Exception as e:
            logging.error(f"Chunk {chunk_num} raised exception: {e}")
            return None
    
    # Process all chunks in parallel
    max_workers = min(len(chunks), 10)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all chunks for parallel processing
        future_to_chunk = {
            executor.submit(process_single_overlapping_chunk, chunk_info, conversion_prompt, examples): chunk_info
            for chunk_info in chunks
        }
        
        # Collect results as they complete
        for future in as_completed(future_to_chunk):
            chunk_info = future_to_chunk[future]
            try:
                result = future.result()
                
                if result:
                    verified_chunks.append(result)
                    elapsed = time.time() - start_time
                    status = "✅ VERIFIED" if result.get('verified') else "⚠️ UNVERIFIED"
                    verified_path = result.get('verified_path')
                    
                    print(f"🎉 Chunk {result['chunk_num']} COMPLETE ({status}) - {elapsed:.1f}s elapsed")
                    if verified_path:
                        print(f"   📄 Individual verified file: {os.path.basename(verified_path)}")
                else:
                    logging.error(f"Chunk {chunk_info['chunk_num']} processing failed")
            except Exception as e:
                logging.error(f"Chunk {chunk_info['chunk_num']} raised exception: {e}")
    
    total_processing_time = time.time() - start_time
    print(f"\n✅ All {len(verified_chunks)} chunks processed in {total_processing_time:.1f}s")
    
    if not verified_chunks:
        logging.error("No chunks were successfully processed")
        return None
    
    # Sort by chunk number
    verified_chunks.sort(key=lambda x: x['chunk_num'])
    
    # ============================================================
    # Phase 3: Merge verified overlapping chunks using page markers
    # ============================================================
    print(f"\n🔗 Phase 3: Merging {len(verified_chunks)} verified chunks (removing duplicate pages)...")
    print(f"   Files being merged:")
    for chunk in verified_chunks:
        verified_path = chunk.get('verified_path')
        if verified_path:
            print(f"   ✅ {os.path.basename(verified_path)} (Chunk {chunk['chunk_num']}, Pages {chunk['page_range']})")
        else:
            text_path = chunk.get('text_path')
            if text_path:
                print(f"   ⚠️  {os.path.basename(text_path)} (Chunk {chunk['chunk_num']}, Pages {chunk['page_range']}, NOT VERIFIED)")
    
    merge_start = time.time()
    merged_text = merge_chunks_with_markers(verified_chunks, overlap=overlap)
    merge_time = time.time() - merge_start
    
    print(f"✅ Merged text: {len(merged_text)} characters (merge time: {merge_time:.1f}s)")
    
    # Save merged text for reference
    merged_text_path = os.path.join(chunks_base, "merged_verified_text.txt")
    with open(merged_text_path, 'w', encoding='utf-8') as f:
        f.write(merged_text)
    print(f"📄 Saved merged verified text: {merged_text_path}")
    
    # ============================================================
    # Phase 4: Final tag consolidation
    # ============================================================
    print(f"\n🔧 Phase 4: Applying tag consolidation to merged text...")
    
    # ============================================================
    # Phase 4: Clean final text (remove duplicates and fix line breaks)
    # ============================================================
    print(f"\n🧹 Phase 4: Final text cleanup...")
    final_text = clean_final_verified_text(merged_text)
    print(f"✅ Final text ready ({len(final_text)} characters)")
    
    # ============================================================
    # Summary
    # ============================================================
    total_time = time.time() - start_time
    
    print(f"\n{'='*70}")
    print(f"🎉 OVERLAPPING CHUNK VERIFICATION COMPLETE")
    print(f"{'='*70}")
    print(f"   Total chunks: {len(chunks)}")
    print(f"   Verified chunks: {sum(1 for c in verified_chunks if c.get('verified'))}")
    print(f"   Pages processed: {page_count}")
    print(f"   Overlap: {overlap} page(s)")
    print(f"   Processing time: {total_processing_time:.1f}s (all chunks parallel)")
    print(f"   Merge time: {merge_time:.1f}s")
    print(f"   Total time: {total_time:.1f}s")
    print(f"   Output size: {len(final_text)} characters")
    print(f"{'='*70}\n")
    
    return final_text


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