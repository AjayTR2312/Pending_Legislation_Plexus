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
from datetime import datetime
from pathlib import Path

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Get the project root directory (parent of app directory)
PROJECT_ROOT = Path(__file__).parent.parent.absolute()

# Configuration (same as pdf_to_text_llm.py)
WORKSPACE_ID = "AgentTraininJaH4"
TOKEN_URL = "https://aiplatform.gcs.int.thomsonreuters.com/v1/anthropic/token"

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

def create_verification_prompt(summary_content, output_text_content, state_code="FL"):
    """
    Create a verification prompt by loading the template and replacing placeholders.
    
    Args:
        summary_content (str): The formatted items summary content
        output_text_content (str): The current output text content
        state_code (str): State code for state-specific prompts (default: "FL")
    
    Returns:
        str: Complete prompt with content inserted
    """
    
    print(f"🔨 Loading verification prompt for state: {state_code}")
    
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
        
        # Create message with Claude (same model as pdf_to_text_llm.py)
        message = client.messages.create(
            model="claude-opus-4-20250514",
            max_tokens=8192,
            temperature=0.1,  # Slightly higher for some creativity in tag placement
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

def get_api_key():
    """
    Get API client using the same approach as pdf_to_text_llm.py.
    """
    return get_anthropic_client()

def extract_formatted_items_summary(summary_content):
    """
    Extract just the formatted items section from summary for cleaner prompt.
    """
    lines = summary_content.split('\n')
    formatted_section = []
    
    in_formatted_section = False
    for line in lines:
        if "All formatted text:" in line:
            in_formatted_section = True
            formatted_section.append(line)
            continue
        
        if in_formatted_section:
            if "BREAKDOWN BY FORMATTING TYPE:" in line:
                break
            formatted_section.append(line)
    
    return '\n'.join(formatted_section)

def verify_fl_tags_with_llm(summary_file_path, output_text_file_path, state_code="FL", client=None):
    """
    Main function to verify tags using Claude LLM with state-specific prompts.
    
    Args:
        summary_file_path (str): Path to the summary file
        output_text_file_path (str): Path to the output text file
        state_code (str): State code for state-specific verification (default: "FL")
        client: Anthropic client instance
    """
    
    print("🤖 Starting LLM-based Tag Verification...")
    print("=" * 60)
    print(f"State: {state_code}")
    print(f"Summary file: {summary_file_path}")
    print(f"Output text file: {output_text_file_path}")
    
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
    
    # Extract just the formatted items section
    formatted_summary = extract_formatted_items_summary(summary_content)
    print(f"✅ Extracted formatted items summary ({len(formatted_summary.split(chr(10)))} lines)")
    
    # Read output text file
    print("📖 Reading output text file...")
    output_text = read_file_content(output_text_file_path)
    if not output_text:
        print("❌ Failed to read output text file")
        return None
    
    print(f"✅ Read output text ({len(output_text)} characters)")
    
    # Create verification prompt using state-specific template
    print(f"🔨 Creating {state_code}-specific verification prompt...")
    prompt = create_verification_prompt(formatted_summary, output_text, state_code)
    
    if not prompt:
        print("❌ Failed to create verification prompt")
        return None
    
    print(f"✅ Prompt created ({len(prompt)} characters)")
    
    # Call Claude API
    print("🚀 Sending to Claude for verification...")
    corrected_text = call_claude_api(prompt, client)
    
    if not corrected_text:
        print("❌ Failed to get response from Claude")
        return None
    
    print(f"✅ Received corrected text from Claude ({len(corrected_text)} characters)")
    
    return corrected_text

def save_corrected_file(corrected_text, original_file_path):
    """
    Save the corrected text to a new file.
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

def compare_files(original_path, corrected_path):
    """
    Compare original and corrected files to show changes made.
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
        
        # Count tags
        original_a_tags = original_text.count('[A>')
        corrected_a_tags = corrected_text.count('[A>')
        original_d_tags = original_text.count('[D>')
        corrected_d_tags = corrected_text.count('[D>')
        
        print(f"\n🏷️  TAG COMPARISON:")
        print(f"[A> tags - Original: {original_a_tags}, Corrected: {corrected_a_tags} (+{corrected_a_tags - original_a_tags})")
        print(f"[D> tags - Original: {original_d_tags}, Corrected: {corrected_d_tags} (+{corrected_d_tags - original_d_tags})")
        
    except Exception as e:
        print(f"❌ Error comparing files: {e}")

def create_verification_report(summary_file, original_file, corrected_file, report_file):
    """
    Create a verification report.
    """
    
    try:
        with open(report_file, 'w', encoding='utf-8') as f:
            f.write("LLM-BASED TAG VERIFICATION REPORT\n")
            f.write("=" * 60 + "\n\n")
            f.write(f"Report Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            
            f.write("FILES PROCESSED:\n")
            f.write(f"Summary Reference: {summary_file}\n")
            f.write(f"Original Output: {original_file}\n")
            f.write(f"Corrected Output: {corrected_file}\n\n")
            
            f.write("VERIFICATION METHOD: Claude LLM\n")
            f.write("TAG RULES APPLIED: State-specific formatting rules\n\n")
            
            f.write("PROCESS:\n")
            f.write("1. Extracted formatted items from summary file\n")
            f.write("2. Created specialized verification prompt\n") 
            f.write("3. Used Claude to intelligently add missing tags\n")
            f.write("4. Preserved existing correct formatting\n")
            f.write("5. Applied state-specific tag rules only\n\n")
            
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