"""
Text-to-Clean-Text LLM Processor (Prompt-Based)
Cleans pdfplumber extracted text using state-specific cleaning prompts.

Input:  Raw pdfplumber text (has page markers, line numbers, headers, footers, spacing)
Output: Clean plain text (formatted like legislative document, no line numbers/headers/footers)

This version uses explicit cleaning prompts instead of few-shot learning for:
- Faster execution (no large examples to send)
- More predictable results (explicit instructions)
- State-specific artifact removal
"""

import os
import logging
from pathlib import Path
from datetime import datetime
import anthropic

# Import existing utilities
from pdf_to_text_llm import (
    get_anthropic_client,
    strip_conversational_headers,
)

# Get the project root directory
PROJECT_ROOT = Path(__file__).parent.parent.absolute()


def load_cleaning_prompt(state_code):
    """
    Load state-specific cleaning prompt.
    
    Args:
        state_code: State code (e.g., 'FL', 'CA', 'CT')
    
    Returns:
        str: Cleaning prompt text, or default prompt if not found
    """
    try:
        prompt_file = os.path.join(PROJECT_ROOT, "data", "Prompts", f"{state_code}_Clean_Prompt.txt")
        
        if os.path.exists(prompt_file):
            with open(prompt_file, 'r', encoding='utf-8') as f:
                prompt = f.read()
            logging.info(f"Loaded cleaning prompt for {state_code}: {prompt_file}")
            return prompt
        else:
            logging.warning(f"No cleaning prompt found for {state_code}, using default")
            return get_default_cleaning_prompt()
            
    except Exception as e:
        logging.error(f"Error loading cleaning prompt for {state_code}: {str(e)}")
        return get_default_cleaning_prompt()


def get_default_cleaning_prompt():
    """
    Get default cleaning prompt if state-specific prompt not found.
    
    Returns:
        str: Default cleaning instructions
    """
    return """You are cleaning raw text extracted from a legislative document PDF.

REMOVE these artifacts:
- Headers: Repeated state/legislative body names
- Footers: Page numbers, coding instructions
- Line numbers: Numbers at the beginning of lines (1, 2, 3, etc.)
- Page markers: <<<PAGE_N_START>>> and <<<PAGE_N_END>>>
- Bill identifiers when repeated as headers
- Excessive blank lines (more than 2 consecutive blank lines)

PRESERVE everything else:
- ALL legislative content (do not add any text, do not drop any text)
- Section numbers and titles
- Paragraph breaks and structure
- Line breaks within sections
- All substantive text

FORMATTING RULES:
- Maintain paragraph breaks (blank lines between paragraphs)
- Maintain line breaks within sections where they naturally occur
- Keep section structure intact
- Remove only the artifacts listed above, nothing else

CRITICAL: Do not add any conversational text, explanations, or comments. Return ONLY the clean legislative text."""


def clean_pdfplumber_text(client, raw_text, cleaning_prompt, max_retries=3):
    """
    Clean raw pdfplumber text using LLM with state-specific cleaning prompt.
    
    Args:
        client: Anthropic client
        raw_text: Raw text from pdfplumber
        cleaning_prompt: State-specific cleaning instructions
        max_retries: Maximum retry attempts
        
    Returns:
        str: Clean formatted text
    """
    logging.info(f"\n{'='*60}")
    logging.info(f"TEXT CLEANING WITH LLM (PROMPT-BASED)")
    logging.info(f"{'='*60}")
    logging.info(f"Input length: {len(raw_text):,} characters")
    
    # Build simple message with prompt + raw text
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": f"{cleaning_prompt}\n\n---RAW TEXT TO CLEAN---\n\n{raw_text}"
                }
            ]
        }
    ]
    
    for attempt in range(1, max_retries + 1):
        try:
            logging.info(f"\nAttempt {attempt}/{max_retries}: Calling Claude API with streaming...")
            
            # Use streaming for long requests
            clean_text = ""
            thinking_text = ""
            
            with client.messages.stream(
                model="claude-opus-4-20250514",
                max_tokens=16384,
                temperature=1.0,  # Required when thinking is enabled
                thinking={
                    "type": "enabled",
                    "budget_tokens": 3000
                },
                messages=messages
            ) as stream:
                for event in stream:
                    if event.type == "content_block_delta":
                        if event.delta.type == "text_delta":
                            clean_text += event.delta.text
                        elif event.delta.type == "thinking_delta":
                            thinking_text += event.delta.thinking
            
            # Get final response for token usage
            response = stream.get_final_message()
            
            if not clean_text:
                logging.error(f"Attempt {attempt}: No text content in response")
                if attempt < max_retries:
                    continue
                return None
            
            # Clean up conversational headers
            clean_text = strip_conversational_headers(clean_text)
            
            logging.info(f"Success! Cleaned text")
            logging.info(f"  Output length: {len(clean_text):,} characters")
            logging.info(f"  Reduction: {len(raw_text) - len(clean_text):,} characters removed")
            
            # Log token usage
            if hasattr(response, 'usage'):
                logging.info(f"  Tokens used: {response.usage.input_tokens:,} input, {response.usage.output_tokens:,} output")
            
            return clean_text
            
        except anthropic.RateLimitError as e:
            logging.warning(f"Attempt {attempt}: Rate limit hit - {str(e)}")
            if attempt < max_retries:
                wait_time = 5 * attempt
                logging.info(f"Waiting {wait_time} seconds before retry...")
                import time
                time.sleep(wait_time)
                continue
            else:
                logging.error("Max retries reached. Rate limit exceeded.")
                return None
                
        except Exception as e:
            logging.error(f"Attempt {attempt}: Error cleaning text - {str(e)}")
            if attempt < max_retries:
                logging.info("Retrying...")
                continue
            else:
                logging.error("Max retries reached. Cleaning failed.")
                return None
    
    return None


def convert_pdfplumber_to_clean(pdfplumber_file, state_code, output_file=None, client=None):
    """
    Main function: Clean raw pdfplumber text using LLM with state-specific prompt.
    
    Args:
        pdfplumber_file: Path to raw pdfplumber text file
        state_code: State code (e.g., 'FL', 'CA', 'CT')
        output_file: Optional path to save clean output
        client: Optional Anthropic client (will create if not provided)
        
    Returns:
        str: Clean text, or None if error
    """
    try:
        logging.info(f"\n{'='*80}")
        logging.info(f"PDFPLUMBER TEXT CLEANING (PROMPT-BASED)")
        logging.info(f"{'='*80}")
        logging.info(f"Input file: {pdfplumber_file}")
        logging.info(f"State: {state_code}")
        
        # Read raw text from pdfplumber
        with open(pdfplumber_file, 'r', encoding='utf-8') as f:
            raw_text = f.read()
        
        logging.info(f"Loaded raw text: {len(raw_text):,} characters")
        
        # Load state-specific cleaning prompt
        cleaning_prompt = load_cleaning_prompt(state_code)
        if not cleaning_prompt:
            logging.error(f"No cleaning prompt available for {state_code}")
            return None
        
        # Get or create Anthropic client
        if client is None:
            client = get_anthropic_client()
        
        # Clean text with LLM
        clean_text = clean_pdfplumber_text(
            client, 
            raw_text,
            cleaning_prompt
        )
        
        if not clean_text:
            logging.error("Failed to clean text")
            return None
        
        # Save output if path provided
        if output_file:
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(clean_text)
            logging.info(f"Saved clean output to: {output_file}")
        
        logging.info(f"\n{'='*80}")
        logging.info(f"CLEANING COMPLETE")
        logging.info(f"{'='*80}\n")
        
        return clean_text
        
    except Exception as e:
        logging.error(f"Error in pdfplumber text cleaning: {str(e)}")
        return None


def main():
    """Example usage of text cleaning."""
    import sys
    
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler()]
    )
    
    if len(sys.argv) < 3:
        print("Usage: python text_to_clean_prompt.py <pdfplumber_text_file> <state_code> [output_file]")
        print("\nExample:")
        print("  python text_to_clean_prompt.py pdfplumber_FL_output.txt FL clean_output.txt")
        print("\nState-specific cleaning prompts should be in:")
        print("  data/Prompts/FL_Clean_Prompt.txt")
        print("  data/Prompts/CA_Clean_Prompt.txt")
        print("  data/Prompts/CT_Clean_Prompt.txt")
        sys.exit(1)
    
    pdfplumber_file = sys.argv[1]
    state_code = sys.argv[2]
    output_file = sys.argv[3] if len(sys.argv) > 3 else None
    
    # Clean the text
    clean_text = convert_pdfplumber_to_clean(
        pdfplumber_file,
        state_code,
        output_file
    )
    
    if clean_text:
        print(f"\n{'='*80}")
        print("SUCCESS! Clean text preview (first 2000 chars):")
        print(f"{'='*80}")
        print(clean_text[:2000])
        print("...")
    else:
        print("\nERROR: Cleaning failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
