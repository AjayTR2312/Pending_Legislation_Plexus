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
import io
from datetime import datetime
from pathlib import Path
import base64
from PyPDF2 import PdfReader, PdfWriter

# Adobe PDF Services imports
from adobe.pdfservices.operation.auth.service_principal_credentials import ServicePrincipalCredentials
from adobe.pdfservices.operation.exception.exceptions import ServiceApiException, ServiceUsageException, SdkException
from adobe.pdfservices.operation.io.cloud_asset import CloudAsset
from adobe.pdfservices.operation.io.stream_asset import StreamAsset
from adobe.pdfservices.operation.pdf_services import PDFServices
from adobe.pdfservices.operation.pdf_services_media_type import PDFServicesMediaType

# Import the DOCX analysis pipeline
from pdf_docx_analysis_pipeline import PDFToDocxAnalysisPipeline

# Import verification functions from llm_tag_verifier
from llm_tag_verifier import (
    load_verification_prompt,
    create_verification_prompt, 
    extract_formatted_items_summary,
    call_claude_api,
    verify_fl_tags_with_llm,
    should_chunk_for_verification
)

# Disable SSL warnings for Adobe API
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Monkey patch requests to disable SSL verification
original_request = requests.Session.request
def patched_request(self, method, url, **kwargs):
    kwargs['verify'] = False
    return original_request(self, method, url, **kwargs)
requests.Session.request = patched_request

# Also patch the global requests methods
requests.packages.urllib3.disable_warnings()
ssl._create_default_https_context = ssl._create_unverified_context

# Get the project root directory (parent of app directory)
PROJECT_ROOT = Path(__file__).parent.parent.absolute()

# Configuration - Updated for enterprise session management
WORKSPACE_ID = "AgentTraininJaH4"
TOKEN_URL = "https://aiplatform.gcs.int.thomsonreuters.com/v1/anthropic/token"
INPUT_FOLDER = "../Temp/sessions"  # Sessions will manage input folders
OUTPUT_FOLDER = "../Temp/sessions"  # Sessions will manage output folders
LOG_FOLDER = "../Temp/logs"  # Default fallback, overridden by session management

# Available states configuration
AVAILABLE_STATES = {
    "AR": {"name": "Arkansas"},
    "CA": {"name": "California"},
    "CO": {"name": "Colorado"},
    "CT": {"name": "Connecticut"},
    "DC": {"name": "District of Columbia"},
    "DE": {"name": "Delaware"},
    "FL": {"name": "Florida"},
    "GA": {"name": "Georgia"},
    "HI": {"name": "Hawaii"},
    "IA": {"name": "Iowa"},
    "ID": {"name": "Idaho"},
    "IL": {"name": "Illinois"},
    "IN": {"name": "Indiana"},
    "KS": {"name": "Kansas"},
    "ME": {"name": "Maine"},
    "MN": {"name": "Minnesota"},
    "NC": {"name": "North Carolina"},
    "ND": {"name": "North Dakota"},
    "NE": {"name": "Nebraska"},
    "NY": {"name": "New York"},
    "OH": {"name": "Ohio"},
    "OK": {"name": "Oklahoma"},
    "SD": {"name": "South Dakota"},
    "TN": {"name": "Tennessee"},
    "TX": {"name": "Texas"},
    "VT": {"name": "Vermont"},
    "WI": {"name": "Wisconsin"},
    "WY": {"name": "Wyoming"}
}

# Chunking Configuration
CHUNK_CONFIG = {
    # Token thresholds
    'max_tokens_no_chunk': 12000,          # Don't chunk if under 12K tokens
    'max_output_tokens_per_chunk': 16384,  # Claude output limit per chunk
    
    # Page thresholds  
    'max_pages_no_chunk': 5,               # Don't chunk if ≤5 pages
    'pages_per_chunk': 5,                  # 5 pages per chunk
    
    # Token estimation
    'estimated_tokens_per_page': 1200,     # Conservative estimate (varies by content density)
    
    # Error handling
    'stop_on_chunk_failure': True,         # Stop entire PDF if any chunk fails
}

# Set up logging
def strip_conversational_headers(text):
    """
    Remove conversational preambles that LLMs sometimes add despite instructions.
    Detects and strips common patterns like:
    - "I'll convert the PDF content..."
    - "Looking at the document..."
    - "Here's the formatted content:"
    - "Let me process..."
    - "**Page 1:**" markers
    
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
        r"^I'll convert the PDF content.*?(?:\n|:)\s*",
        r"^Looking at the document.*?(?:\n|:)\s*",
        r"^Here's the formatted content.*?(?:\n|:)\s*",
        r"^Let me process.*?(?:\n|:)\s*",
        r"^Since this appears to be.*?(?:\n|:)\s*",
        r"^I'll focus on.*?(?:\n|:)\s*",
        r"^\*\*Page \d+:\*\*\s*",
        r"^Here is the.*?(?:\n|:)\s*",
        r"^I'll add the missing tags.*?(?:\n|:)\s*",
        r"^Let me review.*?(?:\n|:)\s*",
    ]
    
    # Try each pattern
    for pattern in conversational_patterns:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE | re.MULTILINE)
    
    # If we removed something, log it
    if len(text) < original_length:
        removed_chars = original_length - len(text)
        logging.info(f"[POST-PROCESSING] Stripped conversational header ({removed_chars} characters)")
    
    # Strip any leading/trailing whitespace after removal
    text = text.strip()
    
    return text

def remove_unwanted_tags(text, state_code=None):
    """
    Remove [A>...<A] and [D>...<D] tags from text, keeping only the content inside.
    This is a post-processing step after PDF-to-text conversion to clean any unwanted tags.
    
    For Colorado (CO): Only removes [D>...<D] tags (strikethrough), keeps [A>...<A] tags (underline)
    For other states: Removes both [A>...<A] and [D>...<D] tags
    
    Args:
        text (str): Input text with potential tags
        state_code (str): State code (e.g., 'CO', 'CA', etc.)
        
    Returns:
        str: Clean text with tags removed
    """
    if not text:
        return text
    
    # Count tags before removal for logging
    a_tags = len(re.findall(r'\[A>.*?<A\]', text))
    d_tags = len(re.findall(r'\[D>.*?<D\]', text))
    total_tags = a_tags + d_tags
    
    if total_tags > 0:
        logging.info(f"[POST-PROCESSING] Found {total_tags} tags ([A>: {a_tags}, [D>: {d_tags})")
        
        # Colorado (CO): Only remove [D>...<D] tags, keep [A>...<A] tags
        if state_code == "CO":
            # Remove [D>content<D] tags - keep the content
            text = re.sub(r'\[D>(.*?)<D\]', r'\1', text, flags=re.DOTALL)
            logging.info(f"[POST-PROCESSING] Colorado: Removed {d_tags} [D>...<D] tags, kept {a_tags} [A>...<A] tags")
        else:
            # All other states: Remove both [A>...<A] and [D>...<D] tags
            # Remove [A>content<A] tags - keep the content
            text = re.sub(r'\[A>(.*?)<A\]', r'\1', text, flags=re.DOTALL)
            
            # Remove [D>content<D] tags - keep the content
            text = re.sub(r'\[D>(.*?)<D\]', r'\1', text, flags=re.DOTALL)
            
            logging.info(f"[POST-PROCESSING] Removed all {total_tags} tags ([A>: {a_tags}, [D>: {d_tags})")
    else:
        logging.info("[POST-PROCESSING] No tags found - text is clean")
    
    return text

def remove_dc_formatting_markers(text, state_code=None):
    """
    Remove DC-specific formatting markers and unwanted text.
    Intelligently removes "ENROLLED ORIGINAL" and markdown formatting markers
    (asterisks and hashes used for bold/italic/headers in markdown) while preserving
    these characters when they appear as part of actual legislative content.
    
    Args:
        text (str): Input text with potential formatting markers
        state_code (str): State code (only applies cleanup if state_code == "DC")
        
    Returns:
        str: Clean text with formatting markers removed
    """
    if not text or state_code != "DC":
        return text
    
    removed_items = []
    
    # 1. Remove "ENROLLED ORIGINAL" (with or without asterisks)
    # This is always unwanted in DC documents
    if "ENROLLED ORIGINAL" in text.upper():
        removed_items.append("ENROLLED ORIGINAL")
        # Remove with double asterisks (e.g., **ENROLLED ORIGINAL**)
        text = re.sub(r'\*\*\s*ENROLLED\s+ORIGINAL\s*\*\*', '', text, flags=re.IGNORECASE)
        # Remove with single asterisks (e.g., *ENROLLED ORIGINAL*)
        text = re.sub(r'\*\s*ENROLLED\s+ORIGINAL\s*\*', '', text, flags=re.IGNORECASE)
        # Remove without asterisks
        text = re.sub(r'ENROLLED\s+ORIGINAL', '', text, flags=re.IGNORECASE)
    
    # 2. Remove markdown formatting asterisks (for bold/italic)
    # Only remove asterisks that are clearly markdown formatting:
    # - **text** (bold)
    # - *text* (italic)
    # - Standalone * at beginning/end of lines
    # But preserve: "Section 5*", "item*", or other content uses
    
    # Count markdown patterns before removal
    double_asterisks = len(re.findall(r'\*\*[^*]+\*\*', text))
    single_asterisks = len(re.findall(r'(?<!\*)\*(?!\*)([^*\n]+)\*(?!\*)', text))
    
    if double_asterisks > 0 or single_asterisks > 0:
        # Remove double asterisks (bold markdown: **text**)
        text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
        # Remove single asterisks (italic markdown: *text* but not part of content)
        # Only remove if asterisk pairs surround text on the same line
        text = re.sub(r'(?<!\w)\*([^*\n]+?)\*(?!\w)', r'\1', text)
        
        removed_items.append(f"markdown asterisks (bold/italic formatting)")
    
    # 3. Remove markdown hash symbols for headers (## Heading, ### Subheading)
    # Only remove hashes at the beginning of lines followed by space (markdown headers)
    # Preserve: "Section #5", "item #3", or other content uses
    hash_headers = len(re.findall(r'^#{1,6}\s', text, flags=re.MULTILINE))
    if hash_headers > 0:
        # Remove hash symbols only at start of line followed by space (markdown headers)
        text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
        removed_items.append(f"{hash_headers} markdown headers")
    
    # 4. Clean up extra blank lines that might result from removals
    # Replace 3+ consecutive newlines with just 2 (preserve paragraph breaks)
    text = re.sub(r'\n{3,}', '\n\n', text)
    
    # 5. Strip leading/trailing whitespace
    text = text.strip()
    
    # Log what was removed
    if removed_items:
        logging.info(f"[DC POST-PROCESSING] Removed: {', '.join(removed_items)}")
    else:
        logging.info("[DC POST-PROCESSING] No DC formatting markers found")
    
    return text

def setup_logging(custom_log_folder=None):
    """Sets up logging configuration if not already configured."""
    # Check if logging is already configured (e.g., by streamlit app)
    if logging.getLogger().handlers:
        logging.info("Logging already configured, skipping setup")
        return
        
    # Use custom folder if provided, otherwise use default
    log_folder = custom_log_folder or LOG_FOLDER
    os.makedirs(log_folder, exist_ok=True)
    current_time = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    log_file_path = os.path.join(log_folder, f'log_pdf_to_text_{current_time}.log')

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file_path),
            logging.StreamHandler()
        ]
    )
    logging.info("Logging configuration complete. Logs will be stored in: " + log_file_path)

# Initialize logging - Only if not already configured (session-aware)
# setup_logging()  # Commented out - handled by streamlit app session management

# Load prompt from external file
def load_conversion_prompt(state_code=None):
    """Load the conversion prompt from external file, with state-specific support."""
    # Try state-specific prompt first from prompts folder
    if state_code:
        state_prompt_file = os.path.join(PROJECT_ROOT, "data", "Prompts", f"{state_code}_Prompt.txt")
        if os.path.exists(state_prompt_file):
            try:
                with open(state_prompt_file, "r", encoding='utf-8') as f:
                    prompt = f.read().strip()
                    logging.info(f"Successfully loaded state-specific prompt for {state_code} from {state_prompt_file}")
                    return prompt
            except Exception as e:
                logging.error(f"Error loading state-specific prompt file {state_prompt_file}: {e}")
    
    # Fall back to default prompt
    prompt_file = os.path.join(PROJECT_ROOT, "data", "Prompts", "PDF_to_Text_Prompt.txt")
    try:
        with open(prompt_file, "r", encoding='utf-8') as f:
            prompt = f.read().strip()
            logging.info(f"Successfully loaded conversion prompt from {prompt_file}")
            return prompt
    except Exception as e:
        logging.error(f"Error loading prompt file {prompt_file}: {e}")
        logging.info("Using default prompt instead")
        # Fallback to default prompt if file not found
        return """You are an expert in converting PDF documents to clean, text format. Please clean and format the content."""

def get_pdf_page_count(pdf_path):
    """
    Get the number of pages in a PDF file.
    
    Args:
        pdf_path (str): Path to the PDF file
        
    Returns:
        int: Number of pages, or None if error
    """
    try:
        reader = PdfReader(pdf_path)
        page_count = len(reader.pages)
        logging.info(f"PDF page count: {page_count} pages")
        return page_count
    except Exception as e:
        logging.error(f"Error reading PDF page count: {e}")
        return None

def estimate_document_tokens(page_count):
    """
    Estimate total tokens for a document based on page count.
    
    Args:
        page_count (int): Number of pages in the document
        
    Returns:
        int: Estimated token count
    """
    estimated_tokens = page_count * CHUNK_CONFIG['estimated_tokens_per_page']
    logging.info(f"Estimated tokens for {page_count} pages: ~{estimated_tokens:,} tokens")
    return estimated_tokens

def should_chunk_document(pdf_path):
    """
    Determine if document needs chunking based on page count and estimated tokens.
    
    Args:
        pdf_path (str): Path to the PDF file
        
    Returns:
        tuple: (needs_chunking: bool, page_count: int, estimated_tokens: int, chunk_count: int)
    """
    try:
        # Get page count
        page_count = get_pdf_page_count(pdf_path)
        if page_count is None:
            logging.warning("Could not determine page count, defaulting to no chunking")
            return False, 0, 0, 0
        
        # Estimate tokens
        estimated_tokens = estimate_document_tokens(page_count)
        
        # Decision thresholds
        max_pages = CHUNK_CONFIG['max_pages_no_chunk']
        max_tokens = CHUNK_CONFIG['max_tokens_no_chunk']
        
        # Determine if chunking is needed
        needs_chunking = (page_count > max_pages) or (estimated_tokens > max_tokens)
        
        # Calculate number of chunks if needed
        chunk_count = 0
        if needs_chunking:
            pages_per_chunk = CHUNK_CONFIG['pages_per_chunk']
            chunk_count = (page_count + pages_per_chunk - 1) // pages_per_chunk  # Ceiling division
        
        # Log decision
        if needs_chunking:
            logging.info(f"Document REQUIRES chunking: {page_count} pages > {max_pages} page threshold")
            logging.info(f"Will create {chunk_count} chunks of {CHUNK_CONFIG['pages_per_chunk']} pages each")
        else:
            logging.info(f"Document does NOT require chunking: {page_count} pages ≤ {max_pages} page threshold")
        
        return needs_chunking, page_count, estimated_tokens, chunk_count
        
    except Exception as e:
        logging.error(f"Error in should_chunk_document: {e}")
        return False, 0, 0, 0

def get_anthropic_client():
    """Get Anthropic API client with credentials from Thomson Reuters platform."""
    try:
        payload = {"workspace_id": WORKSPACE_ID}
        resp = requests.post(TOKEN_URL, headers=None, json=payload, verify=certifi.where())
        credentials = json.loads(resp.content)
        
        if 'anthropic_api_key' in credentials:
            logging.info("Successfully obtained Anthropic API key")
            return anthropic.Anthropic(api_key=credentials["anthropic_api_key"])
        else:
            logging.error(f"No Anthropic API key found: {credentials}")
            return None
    except Exception as e:
        logging.error(f"Error getting Anthropic client: {e}")
        return None

def get_adobe_pdf_services():
    """Get Adobe PDF Services client with credentials."""
    try:
        # Load Adobe credentials from config folder
        cred_file_path = os.path.join(PROJECT_ROOT, "config", "pdfservices-api-credentials.json")
        
        if not os.path.exists(cred_file_path):
            logging.info("Adobe PDF Services credentials not found - using fallback PDF processing")
            return None
            
        with open(cred_file_path, 'r') as cred_file:
            cred_data = json.load(cred_file)
        
        credentials = ServicePrincipalCredentials(
            client_id=cred_data['client_credentials']['client_id'],
            client_secret=cred_data['client_credentials']['client_secret']
        )

        # Creates a PDF Services instance
        pdf_services = PDFServices(credentials=credentials)
        logging.info("Successfully initialized Adobe PDF Services")
        return pdf_services
        
    except Exception as e:
        logging.info(f"Adobe PDF Services not available - using fallback PDF processing: {e}")
        return None

def create_page_chunks(total_pages, pages_per_chunk):
    """
    Create chunk metadata for processing a PDF in page-based chunks.
    
    Args:
        total_pages (int): Total number of pages in the PDF
        pages_per_chunk (int): Number of pages per chunk
        
    Returns:
        list: List of chunk dictionaries with start_page, end_page, chunk_num, total_chunks
    """
    chunks = []
    chunk_num = 1
    total_chunks = (total_pages + pages_per_chunk - 1) // pages_per_chunk
    
    for start_page in range(1, total_pages + 1, pages_per_chunk):
        end_page = min(start_page + pages_per_chunk - 1, total_pages)
        chunks.append({
            'start_page': start_page,
            'end_page': end_page,
            'chunk_num': chunk_num,
            'total_chunks': total_chunks,
            'page_range': f"{start_page}-{end_page}"
        })
        chunk_num += 1
    
    logging.info(f"Created {len(chunks)} chunks for {total_pages} pages ({pages_per_chunk} pages per chunk)")
    return chunks

def extract_pdf_page_range(pdf_path, start_page, end_page):
    """
    Extract a specific page range from a PDF and return as base64 string.
    
    Args:
        pdf_path (str): Path to the PDF file
        start_page (int): Starting page number (1-indexed)
        end_page (int): Ending page number (1-indexed, inclusive)
        
    Returns:
        str: Base64 encoded PDF containing only the specified pages, or None if error
    """
    try:
        from PyPDF2 import PdfReader, PdfWriter
        from io import BytesIO
        import warnings
        
        # Suppress PyPDF2 warnings for corrupted objects
        warnings.filterwarnings('ignore', category=UserWarning, module='PyPDF2')
        
        reader = PdfReader(pdf_path, strict=False)  # Use non-strict mode
        writer = PdfWriter()
        
        # Add pages (convert from 1-indexed to 0-indexed)
        pages_added = 0
        for page_num in range(start_page - 1, end_page):
            if page_num < len(reader.pages):
                try:
                    page = reader.pages[page_num]
                    writer.add_page(page)
                    pages_added += 1
                except Exception as page_error:
                    logging.warning(f"Error extracting page {page_num + 1}: {page_error}")
                    # Continue with other pages
                    continue
        
        if pages_added == 0:
            logging.error(f"Failed to extract any pages from range {start_page}-{end_page}")
            return None
        
        # Write to bytes buffer
        buffer = BytesIO()
        writer.write(buffer)
        buffer.seek(0)
        
        # Convert to base64
        pdf_bytes = buffer.read()
        base64_pdf = base64.b64encode(pdf_bytes).decode('utf-8')
        
        logging.info(f"Extracted {pages_added} pages ({start_page}-{end_page}) from PDF (base64 size: {len(base64_pdf)} chars)")
        return base64_pdf
        
    except Exception as e:
        logging.error(f"Error extracting page range {start_page}-{end_page}: {e}")
        logging.error(f"PDF may be corrupted or have malformed objects")
        return None

def extract_pdf_pages_to_file(pdf_path, start_page, end_page, output_path):
    """
    Extract a specific page range from a PDF and save to a new PDF file.
    
    Args:
        pdf_path (str): Path to the source PDF file
        start_page (int): Starting page number (1-indexed)
        end_page (int): Ending page number (1-indexed, inclusive)
        output_path (str): Path where the output PDF should be saved
        
    Returns:
        bool: True if successful, False if error
    """
    try:
        from PyPDF2 import PdfReader, PdfWriter
        import warnings
        
        # Suppress PyPDF2 warnings for corrupted objects
        warnings.filterwarnings('ignore', category=UserWarning, module='PyPDF2')
        
        reader = PdfReader(pdf_path, strict=False)
        writer = PdfWriter()
        
        # Add pages (convert from 1-indexed to 0-indexed)
        pages_added = 0
        for page_num in range(start_page - 1, end_page):
            if page_num < len(reader.pages):
                try:
                    page = reader.pages[page_num]
                    writer.add_page(page)
                    pages_added += 1
                except Exception as page_error:
                    logging.warning(f"Error extracting page {page_num + 1}: {page_error}")
                    continue
        
        if pages_added == 0:
            logging.error(f"Failed to extract any pages from range {start_page}-{end_page}")
            return False
        
        # Write to output file
        with open(output_path, 'wb') as output_file:
            writer.write(output_file)
        
        logging.info(f"Extracted {pages_added} pages ({start_page}-{end_page}) to {output_path}")
        return True
        
    except Exception as e:
        logging.error(f"Error extracting pages {start_page}-{end_page} to file: {e}")
        return False

def pdf_to_base64_adobe(pdf_path):
    """Convert PDF file to base64 string using Adobe PDF Services for validation."""
    try:
        # First try to validate the PDF with Adobe services
        pdf_services = get_adobe_pdf_services()
        if pdf_services:
            try:
                with open(pdf_path, 'rb') as file:
                    input_stream = file.read()
                
                # Upload to Adobe for validation
                input_asset = pdf_services.upload(input_stream=input_stream, mime_type=PDFServicesMediaType.PDF)
                logging.info(f"Successfully validated PDF with Adobe PDF Services: {pdf_path}")
                
                # Return base64 for LLM processing
                return base64.standard_b64encode(input_stream).decode("utf-8")
                
            except (ServiceApiException, ServiceUsageException, SdkException) as e:
                logging.warning(f"Adobe PDF Services validation failed for {pdf_path}: {e}")
                # Fall back to direct base64 encoding
                with open(pdf_path, "rb") as pdf_file:
                    return base64.standard_b64encode(pdf_file.read()).decode("utf-8")
        else:
            # Fall back to direct base64 encoding if Adobe services unavailable
            with open(pdf_path, "rb") as pdf_file:
                return base64.standard_b64encode(pdf_file.read()).decode("utf-8")
                
    except Exception as e:
        logging.error(f"Error converting PDF to base64: {e}")
        return None

def load_example_pairs(state_code=None):
    """
    Load example PDF-Text pairs for few-shot learning.
    
    Expected folder structure:
    ../data/Examples/[STATE]/
        ├── example1_input.pdf
        ├── example1_output.txt
        ├── example2_input.pdf
        ├── example2_output.txt
        └── ...
    
    Args:
        state_code: State code to load examples for (e.g., 'AR', 'CA')
    
    Returns:
        List of tuples: [(pdf_base64, expected_text, filename), ...]
    """
    try:
        # Build examples folder path using state_code
        if state_code:
            examples_folder = os.path.join(PROJECT_ROOT, "data", "Examples", state_code)
        else:
            # Fallback to a default if no state_code provided
            examples_folder = os.path.join(PROJECT_ROOT, "data", "Examples")
            
        if not os.path.exists(examples_folder):
            logging.warning(f"Examples folder not found: {examples_folder}")
            return []
        
        # Find all input PDFs
        input_files = sorted([f for f in os.listdir(examples_folder) if f.endswith('_input.pdf')])
        
        if not input_files:
            logging.warning("No example input files found (looking for *_input.pdf)")
            return []
        
        examples = []
        for input_file in input_files:
            # Get corresponding output file
            base_name = input_file.replace('_input.pdf', '')
            output_file = f"{base_name}_output.txt"
            
            input_path = os.path.join(examples_folder, input_file)
            output_path = os.path.join(examples_folder, output_file)
            
            if not os.path.exists(output_path):
                logging.warning(f"Missing output file for {input_file}: {output_file}")
                continue
            
            # Load PDF as base64 using Adobe PDF Services
            pdf_data = pdf_to_base64_adobe(input_path)
            if not pdf_data:
                logging.warning(f"Failed to load example PDF: {input_file}")
                continue
            
            # Load expected text output
            try:
                # Try UTF-8 first, fall back to CP1252 (Windows encoding)
                try:
                    with open(output_path, 'r', encoding='utf-8') as f:
                        expected_output = f.read()
                except UnicodeDecodeError:
                    logging.info(f"UTF-8 decode failed for {output_file}, trying CP1252 encoding")
                    with open(output_path, 'r', encoding='cp1252') as f:
                        expected_output = f.read()
                
                examples.append((pdf_data, expected_output, input_file))
                logging.info(f"Loaded example pair: {input_file} <-> {output_file}")
            except Exception as e:
                logging.error(f"Error reading output file {output_file}: {e}")
                continue
        
        logging.info(f"Successfully loaded {len(examples)} example pair(s)")
        return examples
        
    except Exception as e:
        logging.error(f"Error loading example pairs: {e}")
        return []

def build_fewshot_messages(examples, new_pdf_data):
    """
    Build messages array with few-shot examples.
    Pure few-shot learning - Claude learns from examples with minimal instruction.
    
    Args:
        examples: List of (pdf_base64, expected_text, filename) tuples
        new_pdf_data: Base64 encoded PDF to process
    
    Returns:
        List of message dictionaries for Claude API
    """
    messages = []
    
    # Simple instruction - let examples do the teaching
    simple_instruction = "Convert this PDF to formatted text, removing line numbers, headers, footers, and page numbers. Apply formatting markup as shown in the examples."
    
    # Add each example as a user-assistant pair
    for pdf_data, expected_output, filename in examples:
        # User message with example PDF - minimal instruction
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": pdf_data,
                    },
                },
                {
                    "type": "text",
                    "text": simple_instruction
                }
            ],
        })
        
        # Assistant message with expected output - Claude learns the pattern from this
        messages.append({
            "role": "assistant",
            "content": expected_output
        })
        
        logging.info(f"Added example to conversation: {filename}")
    
    # Add the new PDF to process - same simple instruction
    messages.append({
        "role": "user",
        "content": [
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": new_pdf_data,
                },
            },
            {
                "type": "text",
                "text": simple_instruction
            }
        ],
    })
    
    return messages

def process_pdf_chunk(client, chunk_pdf_data, chunk_info, examples, conversion_prompt, max_retries=3):
    """
    Process a single PDF chunk with Claude Opus 4 with retry logic.
    
    Args:
        client: Anthropic client
        chunk_pdf_data (str): Base64 encoded PDF chunk
        chunk_info (dict): Chunk metadata (start_page, end_page, chunk_num, total_chunks)
        examples (list): Few-shot examples
        conversion_prompt (str): Conversion prompt for zero-shot
        max_retries (int): Maximum number of retry attempts (default: 3)
        
    Returns:
        str: Formatted text for this chunk, or None if error after all retries
    """
    import time
    
    chunk_num = chunk_info['chunk_num']
    total_chunks = chunk_info['total_chunks']
    page_range = chunk_info['page_range']
    
    for attempt in range(1, max_retries + 1):
        try:
            if attempt == 1:
                logging.info(f"📄 Processing chunk {chunk_num}/{total_chunks} (pages {page_range})...")
            else:
                logging.info(f"🔄 Retry {attempt}/{max_retries} for chunk {chunk_num}/{total_chunks}...")
            
            # Build messages with few-shot examples
            if examples:
                messages = build_fewshot_messages(examples, chunk_pdf_data)
            else:
                messages = [{
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": chunk_pdf_data,
                            },
                        },
                        {
                            "type": "text",
                            "text": conversion_prompt
                        }
                    ],
                }]
            
            # Process chunk with Claude
            formatted_text = ""
            with client.messages.stream(
                model="claude-opus-4-20250514",
                max_tokens=CHUNK_CONFIG['max_output_tokens_per_chunk'],
                temperature=0.1,
                messages=messages
            ) as stream:
                for text in stream.text_stream:
                    formatted_text += text
            
            logging.info(f"✓ Chunk {chunk_num}/{total_chunks} complete ({len(formatted_text)} characters)")
            return formatted_text
            
        except Exception as e:
            error_msg = str(e)
            logging.error(f"❌ Error processing chunk {chunk_num}/{total_chunks} (attempt {attempt}/{max_retries}): {error_msg}")
            
            # Check if it's a retryable error
            retryable_errors = [
                "peer closed connection",
                "incomplete chunked read",
                "timeout",
                "connection reset",
                "connection refused",
                "rate_limit"
            ]
            
            is_retryable = any(err in error_msg.lower() for err in retryable_errors)
            
            if attempt < max_retries and is_retryable:
                wait_time = attempt * 2  # Exponential backoff: 2s, 4s, 6s
                logging.warning(f"⏳ Retryable error detected. Waiting {wait_time}s before retry...")
                time.sleep(wait_time)
            else:
                if not is_retryable:
                    logging.error(f"❌ Non-retryable error encountered")
                else:
                    logging.error(f"❌ Max retries ({max_retries}) reached")
                return None
    
    return None

def create_overlapping_chunks(page_count, chunk_size=5, overlap=1):
    """
    Create overlapping page chunks for better context preservation.
    
    Args:
        page_count (int): Total number of pages in the document
        chunk_size (int): Number of pages per chunk
        overlap (int): Number of pages to overlap between chunks
        
    Returns:
        list: List of chunk dictionaries with start/end pages
        
    Example with chunk_size=5, overlap=1:
        Chunk 1: Pages 1-6 (pages 0-5 in 0-indexed)
        Chunk 2: Pages 6-11 (pages 5-10 in 0-indexed) - page 6 overlaps
        Chunk 3: Pages 11-15 (pages 10-14 in 0-indexed) - page 11 overlaps
    """
    chunks = []
    start = 0
    chunk_num = 1
    
    while start < page_count:
        # Calculate end page (inclusive overlap)
        end = min(start + chunk_size + overlap, page_count)
        
        chunk = {
            'chunk_num': chunk_num,
            'start_page': start,  # 0-indexed
            'end_page': end,      # 0-indexed (exclusive)
            'page_range': f"{start + 1}-{end}",  # 1-indexed for display
            'pages': list(range(start, end))
        }
        chunks.append(chunk)
        
        # Next chunk starts at current end minus overlap
        start = end - overlap
        chunk_num += 1
        
        # Avoid tiny last chunk - merge with previous if too small
        remaining = page_count - start
        if remaining > 0 and remaining < chunk_size / 2 and len(chunks) > 0:
            # Extend the last chunk to include remaining pages
            chunks[-1]['end_page'] = page_count
            chunks[-1]['page_range'] = f"{chunks[-1]['start_page'] + 1}-{page_count}"
            chunks[-1]['pages'] = list(range(chunks[-1]['start_page'], page_count))
            break
    
    # Update total_chunks for all chunks
    total_chunks = len(chunks)
    for chunk in chunks:
        chunk['total_chunks'] = total_chunks
    
    return chunks

def extract_pdf_pages_with_markers(pdf_path, page_numbers):
    """
    Extract specific pages from PDF and add text markers to track page boundaries.
    
    Args:
        pdf_path (str): Path to the PDF file
        page_numbers (list): List of 0-indexed page numbers to extract
        
    Returns:
        str: Base64-encoded PDF containing only the specified pages
    """
    try:
        reader = PdfReader(pdf_path)
        writer = PdfWriter()
        
        for page_num in page_numbers:
            if page_num < len(reader.pages):
                page = reader.pages[page_num]
                writer.add_page(page)
        
        # Convert to base64
        output = io.BytesIO()
        writer.write(output)
        output.seek(0)
        pdf_bytes = output.read()
        
        return base64.b64encode(pdf_bytes).decode('utf-8')
        
    except Exception as e:
        logging.error(f"Error extracting pages: {e}")
        return None

def build_chunk_aware_prompt(conversion_prompt, chunk_info, page_numbers):
    """
    Enhance conversion prompt with chunk awareness and page marker instructions.
    
    Args:
        conversion_prompt (str): Original conversion prompt
        chunk_info (dict): Chunk metadata (chunk_num, total_chunks, page_range, etc.)
        page_numbers (list): List of page numbers in this chunk (1-indexed for display)
        
    Returns:
        str: Enhanced prompt with chunk context and page marker instructions
    """
    page_list = ", ".join([str(p + 1) for p in page_numbers])  # Convert to 1-indexed
    
    chunk_context = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 CHUNKING CONTEXT - PLEASE READ CAREFULLY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

You are processing CHUNK {chunk_info['chunk_num']} of {chunk_info['total_chunks']} from a legislative document.

📄 Pages in this chunk: {page_list}
📝 Page range: {chunk_info['page_range']}

⚠️ CRITICAL REQUIREMENTS FOR PAGE MARKERS:

1. **Add page boundary markers** - Before processing each page, you MUST add:
   <<<PAGE_X_START>>>
   [... page content here ...]
   <<<PAGE_X_END>>>
   
   Where X is the actual page number (1-indexed).

2. **Example format:**
   <<<PAGE_1_START>>>
   [A>Section 1<A] Introduction text with underline...
   More content from page 1...
   <<<PAGE_1_END>>>
   <<<PAGE_2_START>>>
   Continued text from page 2...
   [D>Deleted text<D] with strikethrough...
   <<<PAGE_2_END>>>

3. **Overlap handling:**
   - Some pages may appear in multiple chunks for context
   - Process EVERY page completely, even if it might be in another chunk
   - The merging process will handle duplicates using your page markers

4. **NO DUPLICATION ALLOWED:**
   - ⚠️ CRITICAL: Do NOT repeat or duplicate any section/paragraph/subsection
   - Each piece of content should appear EXACTLY ONCE in your output
   - If you see similar content, it's likely a page boundary - process it ONCE
   - Check your output carefully before completing

5. **Formatting consistency:**
   - If formatting (like [A>underline<A]) spans across pages, maintain it
   - Process each page with full attention to detail
   - Don't abbreviate or truncate content

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STANDARD CONVERSION INSTRUCTIONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{conversion_prompt}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔁 REMINDER: Include <<<PAGE_X_START>>> and <<<PAGE_X_END>>> markers for EVERY page!
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
    return chunk_context


def detect_and_remove_duplicates(text, min_length=100):
    """
    Detect and remove duplicate paragraphs/sections in converted text.
    This is a safety net to catch LLM-generated duplicates during streaming.
    
    Handles:
    - Exact duplicates (same hash)
    - Partial duplicates (one paragraph is a prefix/truncation of another)
    - Header/section duplicates
    
    Args:
        text (str): Text with potential duplicates
        min_length (int): Minimum paragraph length to check for duplicates
        
    Returns:
        tuple: (cleaned_text, duplicates_found_count)
    """
    import re
    import hashlib
    
    # Split into paragraphs (preserving page markers)
    lines = text.split('\n')
    
    seen_paragraphs = []  # Store actual paragraph text for similarity comparison
    cleaned_lines = []
    duplicates_removed = 0
    current_paragraph = []
    
    def is_duplicate_or_partial(para_text, seen_paragraphs, threshold=0.8):
        """
        Check if paragraph is a duplicate or partial duplicate of any seen paragraph.
        
        Args:
            para_text: Current paragraph text
            seen_paragraphs: List of previously seen paragraph texts
            threshold: Similarity threshold (0.8 = 80% similar)
            
        Returns:
            True if duplicate/partial, False otherwise
        """
        para_stripped = para_text.strip()
        
        for seen_para in seen_paragraphs:
            seen_stripped = seen_para.strip()
            
            # Exact match
            if para_stripped == seen_stripped:
                return True
            
            # Check if current para is a truncated version of seen para
            # (duplicate but incomplete - common in streaming)
            if seen_stripped.startswith(para_stripped) and len(para_stripped) > min_length:
                # Current is a prefix of seen (truncated duplicate)
                return True
            
            if para_stripped.startswith(seen_stripped) and len(seen_stripped) > min_length:
                # Seen is a prefix of current (current is longer version)
                # Remove the shorter one from seen_paragraphs and keep current
                return True
            
            # Check for high similarity (handles minor differences)
            if len(para_stripped) > min_length and len(seen_stripped) > min_length:
                # Use Jaccard similarity on words
                words_current = set(para_stripped.lower().split())
                words_seen = set(seen_stripped.lower().split())
                
                if len(words_current) > 20 and len(words_seen) > 20:  # Only for substantial paragraphs
                    intersection = len(words_current & words_seen)
                    union = len(words_current | words_seen)
                    similarity = intersection / union if union > 0 else 0
                    
                    if similarity >= threshold:
                        return True
        
        return False
    
    for line in lines:
        # Check if it's a page marker
        is_marker = bool(re.match(r'<<<PAGE_\d+_(START|END)>>>', line.strip()))
        
        if is_marker:
            # Process accumulated paragraph before marker
            if current_paragraph:
                para_text = '\n'.join(current_paragraph)
                
                if len(para_text.strip()) >= min_length:
                    if is_duplicate_or_partial(para_text, seen_paragraphs):
                        duplicates_removed += 1
                        logging.warning(f"🔍 Duplicate detected and removed: {para_text.strip()[:80]}...")
                    else:
                        seen_paragraphs.append(para_text)
                        cleaned_lines.extend(current_paragraph)
                else:
                    # Short paragraphs always added (likely headers/titles)
                    cleaned_lines.extend(current_paragraph)
                
                current_paragraph = []
            
            # Add marker
            cleaned_lines.append(line)
            
        elif line.strip() == '':
            # Empty line - end of paragraph
            if current_paragraph:
                para_text = '\n'.join(current_paragraph)
                
                if len(para_text.strip()) >= min_length:
                    if is_duplicate_or_partial(para_text, seen_paragraphs):
                        duplicates_removed += 1
                        logging.warning(f"🔍 Duplicate detected and removed: {para_text.strip()[:80]}...")
                    else:
                        seen_paragraphs.append(para_text)
                        cleaned_lines.extend(current_paragraph)
                        cleaned_lines.append(line)  # Keep empty line
                else:
                    cleaned_lines.extend(current_paragraph)
                    cleaned_lines.append(line)
                
                current_paragraph = []
            else:
                cleaned_lines.append(line)
        else:
            # Regular content line
            current_paragraph.append(line)
    
    # Process final paragraph
    if current_paragraph:
        para_text = '\n'.join(current_paragraph)
        
        if len(para_text.strip()) >= min_length:
            if not is_duplicate_or_partial(para_text, seen_paragraphs):
                seen_paragraphs.append(para_text)
                cleaned_lines.extend(current_paragraph)
            else:
                duplicates_removed += 1
                logging.warning(f"🔍 Duplicate detected and removed: {para_text.strip()[:80]}...")
        else:
            cleaned_lines.extend(current_paragraph)
    
    cleaned_text = '\n'.join(cleaned_lines)
    
    if duplicates_removed > 0:
        logging.warning(f"⚠️ Removed {duplicates_removed} duplicate paragraph(s) from chunk")
    else:
        logging.info("✅ No duplicates detected in chunk")
    
    return cleaned_text, duplicates_removed


def merge_chunks_with_markers(chunk_results, overlap=1):
    """
    Merge overlapping chunks by removing duplicate pages using markers.
    
    Args:
        chunk_results (list): List of chunk result dictionaries with 'text' field
        overlap (int): Number of overlapping pages between chunks
        
    Returns:
        str: Merged text with duplicates removed and markers cleaned
    """
    import re
    
    if not chunk_results:
        return ""
    
    merged_text = []
    
    for i, chunk_result in enumerate(chunk_results):
        chunk_text = chunk_result['text']
        
        if i == 0:
            # First chunk - keep everything
            merged_text.append(chunk_text)
            logging.info(f"  Chunk 1: Keeping all pages")
        else:
            # Remove first 'overlap' pages from this chunk
            # They were already included in the previous chunk
            
            lines = chunk_text.split('\n')
            pages_to_skip = overlap
            pages_skipped = 0
            output_lines = []
            skip_mode = False
            
            for line in lines:
                # Check for page start marker
                start_match = re.search(r'<<<PAGE_(\d+)_START>>>', line)
                if start_match:
                    current_page_num = int(start_match.group(1))
                    pages_skipped += 1
                    
                    if pages_skipped <= pages_to_skip:
                        skip_mode = True  # Start skipping this page
                        logging.debug(f"    Chunk {i+1}: Skipping duplicate page {current_page_num}")
                    else:
                        skip_mode = False  # Done skipping, process remaining pages
                        logging.debug(f"    Chunk {i+1}: Keeping page {current_page_num}")
                
                # Add line if not in skip mode
                if not skip_mode:
                    output_lines.append(line)
            
            merged_text.append('\n'.join(output_lines))
            logging.info(f"  Chunk {i+1}: Skipped {pages_to_skip} overlapping page(s), kept remaining content")
    
    # Join all chunks
    final = '\n'.join(merged_text)
    
    # Remove all page markers from final output
    markers_removed = len(re.findall(r'<<<PAGE_\d+_(START|END)>>>', final))
    final = re.sub(r'<<<PAGE_\d+_START>>>\n?', '', final)
    final = re.sub(r'<<<PAGE_\d+_END>>>\n?', '', final)
    
    # Clean up any extra blank lines
    final = re.sub(r'\n{3,}', '\n\n', final)
    
    logging.info(f"✓ Merged {len(chunk_results)} chunks, removed {markers_removed} page markers")
    
    return final

def process_pdf_chunk_with_markers(client, chunk_pdf_data, chunk_info, examples, chunk_prompt):
    """
    Process a single PDF chunk with page markers and chunk-aware prompting.
    
    Args:
        client: Anthropic client
        chunk_pdf_data (str): Base64-encoded PDF chunk
        chunk_info (dict): Chunk metadata (chunk_num, total_chunks, page_range, etc.)
        examples (list): Few-shot examples
        chunk_prompt (str): Chunk-aware conversion prompt
        
    Returns:
        str: Formatted text with page markers, or None if error
    """
    import time
    
    chunk_num = chunk_info['chunk_num']
    total_chunks = chunk_info['total_chunks']
    max_retries = 3
    
    for attempt in range(1, max_retries + 1):
        try:
            logging.info(f"🔄 Processing chunk {chunk_num}/{total_chunks} (pages {chunk_info['page_range']}) - Attempt {attempt}/{max_retries}")
            
            # Build messages
            if examples:
                messages = build_fewshot_messages(examples, chunk_pdf_data)
                # Replace the last text content with chunk-aware prompt
                for content in messages[-1]['content']:
                    if content.get('type') == 'text':
                        content['text'] = chunk_prompt
            else:
                messages = [{
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": chunk_pdf_data,
                            },
                        },
                        {
                            "type": "text",
                            "text": chunk_prompt
                        }
                    ],
                }]
            
            # Process chunk with Claude
            formatted_text = ""
            with client.messages.stream(
                model="claude-opus-4-20250514",
                max_tokens=CHUNK_CONFIG['max_output_tokens_per_chunk'],
                temperature=0.1,
                messages=messages
            ) as stream:
                for text in stream.text_stream:
                    formatted_text += text
            
            logging.info(f"✅ Chunk {chunk_num}/{total_chunks} complete ({len(formatted_text)} characters)")
            
            # Layer 2: Deduplication safety net
            cleaned_text, duplicates_removed = detect_and_remove_duplicates(formatted_text)
            
            if duplicates_removed > 0:
                logging.warning(f"🧹 Cleaned chunk {chunk_num}/{total_chunks}: removed {duplicates_removed} duplicate(s)")
                return cleaned_text
            
            return formatted_text
            
        except Exception as e:
            error_msg = str(e)
            logging.error(f"❌ Error processing chunk {chunk_num}/{total_chunks} (attempt {attempt}/{max_retries}): {error_msg}")
            
            # Check if it's a retryable error
            retryable_errors = [
                "peer closed connection",
                "incomplete chunked read",
                "timeout",
                "connection reset",
                "connection refused",
                "rate_limit"
            ]
            
            is_retryable = any(err in error_msg.lower() for err in retryable_errors)
            
            if attempt < max_retries and is_retryable:
                wait_time = attempt * 2  # Exponential backoff: 2s, 4s, 6s
                logging.warning(f"⏳ Retryable error detected. Waiting {wait_time}s before retry...")
                time.sleep(wait_time)
            else:
                if not is_retryable:
                    logging.error(f"❌ Non-retryable error encountered")
                else:
                    logging.error(f"❌ Max retries ({max_retries}) reached")
                return None
    
    return None

def process_pdf_with_chunking(client, pdf_path, examples, conversion_prompt):
    """
    Process a large PDF in page-based chunks and combine the results.
    
    Args:
        client: Anthropic client
        pdf_path (str): Path to the PDF file
        examples (list): Few-shot examples
        conversion_prompt (str): Conversion prompt
        
    Returns:
        str: Combined formatted text from all chunks, or None if error
    """
    try:
        # Determine chunking strategy
        needs_chunking, page_count, estimated_tokens, chunk_count = should_chunk_document(pdf_path)
        
        if not needs_chunking:
            logging.info("Document does not require chunking, processing as single document")
            # Use original single-call processing
            pdf_data = pdf_to_base64_adobe(pdf_path)
            if not pdf_data:
                logging.error(f"Failed to read PDF file: {pdf_path}")
                return None
            
            # Build messages
            if examples:
                messages = build_fewshot_messages(examples, pdf_data)
            else:
                messages = [{
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": pdf_data,
                            },
                        },
                        {
                            "type": "text",
                            "text": conversion_prompt
                        }
                    ],
                }]
            
            # Process single document
            formatted_text = ""
            with client.messages.stream(
                model="claude-opus-4-20250514",
                max_tokens=16384,
                temperature=0.1,
                messages=messages
            ) as stream:
                for text in stream.text_stream:
                    formatted_text += text
            
            logging.info(f"Successfully processed PDF ({len(formatted_text)} characters)")
            return formatted_text
        
        # Chunking required
        logging.info(f"🔄 Processing {page_count}-page document with OVERLAPPING chunks...")
        
        # Create overlapping chunks (overlap=1 means 1 page overlap)
        pages_per_chunk = CHUNK_CONFIG['pages_per_chunk']
        overlap = 1  # One page overlap between chunks
        chunks = create_overlapping_chunks(page_count, pages_per_chunk, overlap)
        
        logging.info(f"📊 Created {len(chunks)} overlapping chunks:")
        for chunk in chunks:
            logging.info(f"   Chunk {chunk['chunk_num']}: Pages {chunk['page_range']} (overlap={overlap if chunk['chunk_num'] > 1 else 0})")
        
        # Process each chunk with page markers
        chunk_results = []
        chunk_extraction_failed = False
        
        for chunk_info in chunks:
            # Extract pages with markers
            chunk_pdf_data = extract_pdf_pages_with_markers(pdf_path, chunk_info['pages'])
            
            if not chunk_pdf_data:
                error_msg = f"Failed to extract pages {chunk_info['page_range']}"
                logging.error(f"❌ {error_msg}")
                chunk_extraction_failed = True
                
                if CHUNK_CONFIG['stop_on_chunk_failure']:
                    logging.error("⛔ PDF chunking failed - falling back to full document processing")
                    break
                else:
                    logging.warning("⚠ Continuing with remaining chunks...")
                    continue
            
            # Build chunk-aware prompt with page marker instructions
            chunk_prompt = build_chunk_aware_prompt(conversion_prompt, chunk_info, chunk_info['pages'])
            
            # Process the chunk
            chunk_text = process_pdf_chunk_with_markers(
                client, 
                chunk_pdf_data, 
                chunk_info, 
                examples, 
                chunk_prompt
            )
            
            if chunk_text is None:
                error_msg = f"Failed to process chunk {chunk_info['chunk_num']}/{chunk_info['total_chunks']}"
                logging.error(f"❌ {error_msg}")
                
                if CHUNK_CONFIG['stop_on_chunk_failure']:
                    logging.error("⛔ Stopping PDF processing due to chunk failure")
                    return None
                else:
                    logging.warning("⚠ Continuing with remaining chunks...")
                    continue
            
            # Store result with metadata (Option B: no page markers in text)
            chunk_results.append({
                'text': chunk_text,
                'chunk_num': chunk_info['chunk_num'],
                'page_range': chunk_info['page_range']
            })
        
        # Check if we got any results
        if not chunk_results:
            if chunk_extraction_failed:
                logging.warning("⚠️ Chunk extraction failed - attempting to process full PDF instead")
                logging.info("📄 Processing entire PDF without chunking (may result in truncated output)...")
                
                # Try processing the entire PDF as fallback
                pdf_data = pdf_to_base64_adobe(pdf_path)
                if not pdf_data:
                    logging.error("❌ Failed to convert PDF to base64")
                    return None
                
                # Build messages
                if examples:
                    messages = build_fewshot_messages(examples, pdf_data)
                else:
                    messages = [{
                        "role": "user",
                        "content": [
                            {
                                "type": "document",
                                "source": {
                                    "type": "base64",
                                    "media_type": "application/pdf",
                                    "data": pdf_data,
                                },
                            },
                            {
                                "type": "text",
                                "text": conversion_prompt
                            }
                        ],
                    }]
                
                # Process full document
                formatted_text = ""
                with client.messages.stream(
                    model="claude-opus-4-20250514",
                    max_tokens=16384,
                    temperature=0.1,
                    messages=messages
                ) as stream:
                    for text in stream.text_stream:
                        formatted_text += text
                
                logging.warning(f"⚠️ Processed full PDF as fallback ({len(formatted_text)} characters)")
                logging.warning("⚠️ Note: Output may be truncated if document exceeds token limits")
                return formatted_text
            else:
                logging.error("❌ No chunks were successfully processed")
                return None
        
        # Merge chunks using page markers (remove duplicate overlapping pages)
        logging.info(f"🔗 Merging {len(chunk_results)} overlapping chunks...")
        combined_text = merge_chunks_with_markers(chunk_results, overlap=overlap)
        
        logging.info(f"✅ Successfully processed and merged all {len(chunk_results)} chunks")
        logging.info(f"✅ Final output: {len(combined_text)} characters")
        
        return combined_text
        
    except Exception as e:
        logging.error(f"❌ Error in chunked PDF processing: {e}")
        return None

def process_pdf_with_claude(client, pdf_path, examples, conversion_prompt):
    """Process PDF using Claude Opus 4 with few-shot examples."""
    try:
        logging.info(f"Processing PDF: {pdf_path}")
        
        # Check if document needs chunking
        needs_chunking, page_count, estimated_tokens, chunk_count = should_chunk_document(pdf_path)
        
        if needs_chunking:
            logging.info(f"Document requires chunking ({page_count} pages, ~{estimated_tokens:,} tokens)")
            return process_pdf_with_chunking(client, pdf_path, examples, conversion_prompt)
        
        # Original single-document processing for small PDFs
        logging.info(f"Processing as single document ({page_count} pages, ~{estimated_tokens:,} tokens)")
        
        # Convert PDF to base64 using Adobe PDF Services
        pdf_data = pdf_to_base64_adobe(pdf_path)
        if not pdf_data:
            logging.error(f"Failed to read PDF file: {pdf_path}")
            return None
        
        # Build messages with few-shot examples
        if examples:
            logging.info(f"Using {len(examples)} example(s) for pure few-shot learning")
            logging.info("Claude will learn formatting patterns from examples")
            messages = build_fewshot_messages(examples, pdf_data)
        else:
            logging.info("No examples provided, using zero-shot approach with detailed prompt")
            messages = [{
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_data,
                        },
                    },
                    {
                        "type": "text",
                        "text": conversion_prompt
                    }
                ],
            }]
        
        # Create message with Claude using streaming (required for long operations)
        logging.info("Sending request to Claude Opus 4 with streaming...")
        
        formatted_text = ""
        
        with client.messages.stream(
            model="claude-opus-4-20250514",  # Most accurate model available
            max_tokens=16384,  # Maximum output tokens for Claude - handles large legislative documents
            temperature=0.1,  # Maximum consistency
            messages=messages
        ) as stream:
            for text in stream.text_stream:
                formatted_text += text
        
        logging.info(f"Successfully processed PDF with Claude (received {len(formatted_text)} characters)")
        
        # Apply deduplication safety net
        cleaned_text, duplicates_removed = detect_and_remove_duplicates(formatted_text)
        
        if duplicates_removed > 0:
            logging.warning(f"🧹 Removed {duplicates_removed} duplicate paragraph(s) from output")
            return cleaned_text
        
        return formatted_text
        
    except Exception as e:
        logging.error(f"Error processing PDF with Claude: {e}")
        return None

def save_to_text_file(content, output_path, enable_tag_cleaning=True, state_code=None):
    """
    Save content to a text file with optional post-processing to remove unwanted tags.
    
    Args:
        content (str): Text content to save
        output_path (str): Path to save the file
        enable_tag_cleaning (bool): Whether to clean unwanted tags (default: True)
        state_code (str): State code to determine processing rules (e.g., "IN" to skip tag cleaning)
        
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        # CRITICAL: PDF→Text tags should be REMOVED (cleaned) for verification step
        # The verification step will add tags properly based on DOCX analysis
        # Only skip tag cleaning for states that explicitly don't use verification (IN)
        # CO uses selective tag cleaning (removes [D>...<D], keeps [A>...<A])
        
        if state_code == "IN":
            # IN doesn't use verification - keep text as-is
            enable_tag_cleaning = False
            logging.info(f"[POST-PROCESSING] IN doesn't use verification - keeping text as-is")
        elif state_code == "CO":
            # CO uses selective tag cleaning - remove [D>...<D], keep [A>...<A]
            enable_tag_cleaning = True
            logging.info(f"[POST-PROCESSING] CO: Removing [D>...<D] tags, keeping [A>...<A] tags")
        else:
            # All other states: remove tags from PDF→Text so verification can add them properly
            enable_tag_cleaning = True
            logging.info(f"[POST-PROCESSING] Removing tags from PDF→Text output (verification will add them)")
        
        # STEP 1: Strip conversational headers first (always enabled)
        print("🔧 Post-processing: Removing conversational headers...")
        cleaned_content = strip_conversational_headers(content)
        
        # STEP 2: Remove unwanted tags if enabled
        if enable_tag_cleaning:
            print("🔧 Post-processing: Removing unwanted tags...")
            cleaned_content = remove_unwanted_tags(cleaned_content, state_code)
        else:
            logging.info("[POST-PROCESSING] Tag cleaning disabled")
        
        # STEP 3: DC-specific post-processing: Remove "ENROLLED ORIGINAL", asterisks, and hash symbols
        cleaned_content = remove_dc_formatting_markers(cleaned_content, state_code)
        
        # Save the cleaned content
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(cleaned_content)
        logging.info(f"[POST-PROCESSING] Saved output to: {output_path}")
        return True
    except Exception as e:
        logging.error(f"❌ Error saving file: {e}")
        return False

def read_file_content(file_path, encoding='utf-8'):
    """Read file content with error handling."""
    try:
        with open(file_path, 'r', encoding=encoding) as f:
            return f.read()
    except Exception as e:
        logging.error(f"Error reading file {file_path}: {e}")
        return None

def verify_text_with_llm(text_file_path, summary_file_path, state_code, client, pdf_path=None, session_dir=None):
    """
    Verify text file using LLM with summary reference.
    Routes to PDF-level chunking if document has >10 pages.
    
    Args:
        text_file_path (str): Path to the text file to verify
        summary_file_path (str): Path to the summary reference file (can be None for DC)
        state_code (str): State code for state-specific verification
        client: Anthropic client instance
        pdf_path (str, optional): Path to original PDF for chunking decision
        session_dir (str, optional): Session directory for chunk files
    
    Returns:
        str: Corrected text or original text for DC
    """
    try:
        # Continue with standard verification (existing code)
        print("🤖 Running LLM-based tag verification...")
        
        # Handle DC and IN states - no verification needed, return original text
        if state_code in ["DC", "IN"]:
            skip_reason = "doesn't require tag verification" if state_code == "DC" else "verification skipped per configuration"
            print(f"⏭️ {state_code} state {skip_reason}")
            print("📄 Returning original text without modifications")
            
            # Read and return original text
            text_content = read_file_content(text_file_path)
            if text_content:
                logging.info("Successfully returned original DC text without verification")
                return text_content
            else:
                logging.error("Failed to read DC text file")
                return None
        
        # For other states, proceed with normal verification using the comprehensive verifier
        if not summary_file_path:
            logging.error(f"Summary file path is required for {state_code} verification but was not provided")
            return None
        
        # Use the comprehensive LLM verifier that handles CSV separation
        logging.info(f"Running comprehensive LLM verification for {state_code}")
        corrected_text = verify_fl_tags_with_llm(summary_file_path, text_file_path, state_code, client)
        
        if corrected_text:
            logging.info("Successfully received corrected text from comprehensive verifier")
            return corrected_text
        else:
            logging.error("Failed to get corrected text from comprehensive verifier")
            return None
            
    except Exception as e:
        logging.error(f"Error in LLM verification: {e}")
        return None

def save_verified_file(corrected_text, original_file_path, state_code, output_base_folder=None):
    """
    Save verified text to the Verified folder with state organization.
    
    Args:
        corrected_text (str): The corrected text content
        original_file_path (str): Original file path to extract filename
        state_code (str): State code for folder organization
        output_base_folder (str): Base output folder (for session management)
    
    Returns:
        str: Path to saved verified file or None if failed
    """
    try:
        # Use session-specific folder if provided, otherwise use default
        if output_base_folder:
            verified_folder = os.path.join(output_base_folder, "Verified", state_code)
        else:
            verified_folder = os.path.join("../data/output", "Verified", state_code)
            
        os.makedirs(verified_folder, exist_ok=True)
        
        # Generate verified filename
        original_path = Path(original_file_path)
        verified_filename = f"{original_path.stem}.txt"
        verified_file_path = os.path.join(verified_folder, verified_filename)
        
        # Save verified file
        with open(verified_file_path, 'w', encoding='utf-8') as f:
            f.write(corrected_text)
        
        logging.info(f"Saved verified file to: {verified_file_path}")
        
        return verified_file_path
        
    except Exception as e:
        logging.error(f"Error saving verified file: {e}")
        return None

def create_verification_report(summary_file, original_file, verified_file, state_code, original_text, corrected_text):
    """
    Create a verification report in the state-specific Report folder.
    
    Args:
        summary_file (str): Path to summary file
        original_file (str): Path to original text file
        verified_file (str): Path to verified text file
        state_code (str): State code
        original_text (str): Original text content
        corrected_text (str): Corrected text content
    
    Returns:
        str: Path to report file or None if failed
    """
    try:
        # Create report folder structure
        report_folder = os.path.join("../data/output", "Reports", state_code)
        os.makedirs(report_folder, exist_ok=True)
        
        # Generate report filename
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        original_filename = Path(original_file).stem
        report_filename = f"{original_filename}_verification_report_{timestamp}.txt"
        report_file_path = os.path.join(report_folder, report_filename)
        
        # Create report content
        with open(report_file_path, 'w', encoding='utf-8') as f:
            f.write(f"LLM-BASED {state_code} TAG VERIFICATION REPORT\n")
            f.write("=" * 60 + "\n\n")
            f.write(f"Report Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"State: {state_code}\n\n")
            
            f.write("FILES PROCESSED:\n")
            f.write(f"Summary Reference: {summary_file}\n")
            f.write(f"Original Text File: {original_file}\n")
            f.write(f"Verified Text File: {verified_file}\n\n")
            
            f.write("VERIFICATION METHOD: Claude Opus 4 LLM\n")
            f.write(f"TAG RULES APPLIED: {state_code}-specific formatting rules\n\n")
            
            f.write("PROCESS:\n")
            f.write("1. Extracted formatted items from DOCX analysis summary\n")
            f.write(f"2. Loaded {state_code}-specific verification prompt\n")
            f.write("3. Used Claude to intelligently add missing tags\n")
            f.write("4. Preserved existing correct formatting\n")
            f.write(f"5. Applied {state_code}-specific tag rules only\n\n")
            
            # Count tags for comparison
            if original_text and corrected_text:
                # Generic tag counting (adjust patterns based on state)
                original_tags = len([match for match in ['[A>', '[D>', '<del>', '<ins>', '[U>', '[S>'] 
                                   if match in original_text])
                corrected_tags = len([match for match in ['[A>', '[D>', '<del>', '<ins>', '[U>', '[S>'] 
                                    if match in corrected_text])
                
                f.write("RESULTS:\n")
                f.write(f"Original file size: {len(original_text)} characters\n")
                f.write(f"Verified file size: {len(corrected_text)} characters\n")
                f.write(f"Estimated tags added: {corrected_tags - original_tags}\n\n")
            
            f.write("QUALITY: LLM-based verification provides contextual understanding\n")
            f.write("and prevents over-tagging issues common with algorithmic approaches.\n")
            f.write(f"\nThis file can be used for {state_code} legislative document processing.\n")
        
        logging.info(f"Verification report saved to: {report_file_path}")
        return report_file_path
        
    except Exception as e:
        logging.error(f"Error creating verification report: {e}")
        return None

def process_state_pdfs(client, examples, state_code, input_folder, output_folder, conversion_prompt):
    """Process all PDF files for a specific state with integrated DOCX analysis."""
    try:
        # Ensure directories exist
        os.makedirs(input_folder, exist_ok=True)
        os.makedirs(output_folder, exist_ok=True)
        
        # Get all PDF files
        pdf_files = [f for f in os.listdir(input_folder) if f.lower().endswith('.pdf')]
        
        if not pdf_files:
            logging.warning(f"No PDF files found in {input_folder}")
            print(f"\nNo PDF files found in {input_folder}")
            print(f"Please place PDF files in this folder and run again.")
            return
        
        logging.info(f"Found {len(pdf_files)} PDF file(s) to process for {state_code}")
        print(f"\nFound {len(pdf_files)} PDF file(s) to process for {state_code}")
        print(f"Each file will be converted to TEXT, DOCX analysis, and LLM verification")
        
        success_count = 0
        failed_files = []
        docx_success_count = 0
        docx_failed_files = []
        verification_success_count = 0
        verification_failed_files = []
        
        # Process each PDF
        for i, pdf_file in enumerate(pdf_files, 1):
            pdf_path = os.path.join(input_folder, pdf_file)
            output_filename = Path(pdf_file).stem + ".txt"
            output_path = os.path.join(output_folder, output_filename)
            
            print(f"\nProcessing file {i}/{len(pdf_files)}: {pdf_file}")
            print("─" * 50)
            
            logging.info(f"\n{'='*60}")
            logging.info(f"Processing: {pdf_file}")
            logging.info(f"{'='*60}")
            
            # Initialize tracking variables for this file
            text_success = False
            docx_success = False
            summary_file_path = None
            
            # STEP 1: PDF to Text conversion (existing functionality)
            print("📄 Converting PDF to TEXT...")
            formatted_text = process_pdf_with_claude(client, pdf_path, examples, conversion_prompt)
            
            if formatted_text:
                # Save to text file
                if save_to_text_file(formatted_text, output_path, state_code=state_code):
                    logging.info(f"[SUCCESS] Converted: {pdf_file} -> {output_filename}")
                    success_count += 1
                    text_success = True
                    print(f"  ✓ Text conversion successful: {output_filename}")
                else:
                    logging.error(f"[FAILED] Could not save: {output_filename}")
                    failed_files.append(pdf_file)
                    print(f"  ✗ Text conversion failed to save: {output_filename}")
            else:
                logging.error(f"[FAILED] Could not process: {pdf_file}")
                failed_files.append(pdf_file)
                print(f"  ✗ Text conversion failed: {pdf_file}")
            
            # STEP 2: PDF to DOCX Analysis Pipeline (new functionality)
            # Skip DOCX analysis for DC and IN states
            if state_code in ["DC", "IN"]:
                skip_reason = "no formatting to analyze" if state_code == "DC" else "DOCX analysis not required"
                print(f"⏭️  Skipping DOCX analysis for {state_code} ({skip_reason})")
                logging.info(f"[SKIPPED] DOCX analysis skipped for {state_code} state: {pdf_file}")
                docx_success = True  # Mark as success since DC doesn't need this step
                docx_success_count += 1  # Count it as successful
            else:
                print("📋 Running DOCX Analysis Pipeline...")
                try:
                    # Create pipeline instance with main output folder (state subfolder will be created automatically)
                    docx_output_folder = "../data/output"
                    pipeline = PDFToDocxAnalysisPipeline(pdf_path, docx_output_folder, state_code=state_code)
                    
                    # Run the complete DOCX analysis pipeline
                    docx_results = pipeline.run_complete_pipeline()
                    
                    if docx_results and docx_results['success']:
                        logging.info(f"[SUCCESS] DOCX Analysis completed for: {pdf_file}")
                        docx_success_count += 1
                        docx_success = True
                        summary_file_path = docx_results['summary_path']
                        print(f"  ✓ DOCX analysis successful")
                        print(f"    - DOCX: {docx_results['docx_path']}")
                        print(f"    - Summary: {docx_results['summary_path']}")
                        print(f"    - Formatted items: {docx_results['formatted_items_count']}")
                    else:
                        logging.error(f"[FAILED] DOCX Analysis failed for: {pdf_file}")
                        docx_failed_files.append(pdf_file)
                        print(f"  ✗ DOCX analysis failed")
                        if docx_results and docx_results['errors']:
                            for error in docx_results['errors']:
                                print(f"    Error: {error}")
                                
                except Exception as docx_error:
                    logging.error(f"[ERROR] DOCX Analysis exception for {pdf_file}: {docx_error}")
                    docx_failed_files.append(pdf_file)
                    print(f"  ✗ DOCX analysis error: {docx_error}")
            
            # STEP 3: LLM Verification Process (new functionality)
            # Skip verification for DC and IN states
            if state_code in ["DC", "IN"]:
                skip_reason = "no tagging required" if state_code == "DC" else "verification skipped per configuration"
                print(f"⏭️  Skipping verification for {state_code} ({skip_reason})")
                print("📁 Copying to Verified folder for consistent structure...")
                
                # Copy the converted text file to Verified folder for consistent output structure
                if text_success:
                    original_text = read_file_content(output_path)
                    if original_text:
                        verified_file_path = save_verified_file(original_text, output_path, state_code, output_base_folder=output_folder)
                        if verified_file_path:
                            verification_success_count += 1
                            print(f"  ✓ File copied to Verified folder")
                            print(f"    - Verified file: {verified_file_path}")
                            logging.info(f"[SUCCESS] {state_code} file copied to Verified folder: {pdf_file}")
                        else:
                            verification_failed_files.append(pdf_file)
                            print(f"  ✗ Failed to copy to Verified folder")
                            logging.error(f"[FAILED] Could not copy {state_code} file to Verified folder: {pdf_file}")
                    else:
                        verification_failed_files.append(pdf_file)
                        print(f"  ✗ Could not read converted text file")
                        logging.error(f"[FAILED] Could not read converted text for {state_code} file: {pdf_file}")
                else:
                    verification_failed_files.append(pdf_file)
                    print(f"  ⚠️ Skipping copy (text conversion failed)")
                    logging.warning(f"Skipping Verified copy for {pdf_file} - text conversion failed")
                
                logging.info(f"[PROCESSED] {state_code} verification skipped, file copied to Verified: {pdf_file}")
            elif text_success and docx_success:
                # Check if summary file exists (only if there were formatted items)
                if summary_file_path:
                    print("🤖 Running LLM Tag Verification...")
                    try:
                        # Verify text using LLM
                        corrected_text = verify_text_with_llm(output_path, summary_file_path, state_code, client)
                        
                        if corrected_text:
                            # Save verified file
                            verified_file_path = save_verified_file(corrected_text, output_path, state_code)
                            
                            if verified_file_path:
                                # Create verification report
                                original_text = read_file_content(output_path)
                                report_file_path = create_verification_report(
                                    summary_file_path, output_path, verified_file_path, 
                                    state_code, original_text, corrected_text
                                )
                                
                                verification_success_count += 1
                                print(f"  ✓ Verification successful")
                                print(f"    - Verified file: {verified_file_path}")
                                if report_file_path:
                                    print(f"    - Report: {report_file_path}")
                                
                                logging.info(f"[SUCCESS] Verification completed for: {pdf_file}")
                            else:
                                verification_failed_files.append(pdf_file)
                                print(f"  ✗ Failed to save verified file")
                                logging.error(f"[FAILED] Could not save verified file for: {pdf_file}")
                        else:
                            verification_failed_files.append(pdf_file)
                            print(f"  ✗ LLM verification failed")
                            logging.error(f"[FAILED] LLM verification failed for: {pdf_file}")
                            
                    except Exception as verify_error:
                        verification_failed_files.append(pdf_file)
                        print(f"  ✗ Verification error: {verify_error}")
                        logging.error(f"[ERROR] Verification exception for {pdf_file}: {verify_error}")
                else:
                    # No formatted items found - copy original text to Verified folder
                    print("ℹ️ No formatted items found - copying to Verified folder without verification")
                    text_content = read_file_content(output_path)
                    if text_content:
                        verified_file_path = save_verified_file(text_content, output_path, state_code)
                        if verified_file_path:
                            verification_success_count += 1
                            print(f"  ✓ Copied to Verified folder")
                            print(f"    - Verified file: {verified_file_path}")
                            logging.info(f"[PROCESSED] {state_code} file with no formatting copied to Verified: {pdf_file}")
                        else:
                            verification_failed_files.append(pdf_file)
                            print(f"  ✗ Failed to copy to Verified folder")
                            logging.error(f"[FAILED] Could not copy {state_code} file to Verified folder: {pdf_file}")
                    else:
                        verification_failed_files.append(pdf_file)
                        print(f"  ✗ Could not read converted text file")
                        logging.error(f"[FAILED] Could not read converted text for {state_code} file: {pdf_file}")
            else:
                verification_failed_files.append(pdf_file)
                print(f"  ⚠️ Skipping verification (previous steps failed)")
                logging.warning(f"Skipping verification for {pdf_file} - prerequisite steps failed")
        
        # Summary
        print(f"\n{'='*60}")
        print(f"PROCESSING SUMMARY FOR {state_code}")
        print(f"{'='*60}")
        print(f"Total files processed: {len(pdf_files)}")
        print(f"\nTEXT CONVERSION:")
        print(f"  Successful: {success_count}")
        print(f"  Failed: {len(failed_files)}")
        print(f"\nDOCX ANALYSIS:")
        if state_code in ["DC", "IN"]:
            skip_reason = "no formatting to analyze" if state_code == "DC" else "not required"
            print(f"  Skipped ({skip_reason}): {len(pdf_files)}")
        else:
            print(f"  Successful: {docx_success_count}")
            print(f"  Failed: {len(docx_failed_files)}")
        print(f"\nLLM VERIFICATION:")
        if state_code in ["DC", "IN"]:
            print(f"  Skipped (copied to Verified folder): {verification_success_count}")
            print(f"  Failed to copy: {len(verification_failed_files)}")
        else:
            print(f"  Successful: {verification_success_count}")
            print(f"  Failed: {len(verification_failed_files)}")
        
        if failed_files:
            print(f"\nText conversion failed files:")
            for file in failed_files:
                print(f"  - {file}")
        
        if docx_failed_files and state_code not in ["DC", "IN"]:
            print(f"\nDOCX analysis failed files:")
            for file in docx_failed_files:
                print(f"  - {file}")
        
        if verification_failed_files:
            if state_code in ["DC", "IN"]:
                print(f"\nFailed to copy to Verified folder:")
            else:
                print(f"\nVerification failed files:")
            for file in verification_failed_files:
                print(f"  - {file}")
        
        print(f"\nOutput locations:")
        print(f"  Text files: {output_folder}")
        if state_code not in ["DC", "IN"]:
            print(f"  DOCX files: ../data/output/Output_Docx/{state_code}")
            print(f"  Summary files: ../data/output/Verify/{state_code}")
        print(f"  Verified files: ../data/output/Verified/{state_code}")
        if state_code in ["DC", "IN"]:
            note = "no analysis/verification needed" if state_code == "DC" else "verification skipped per configuration"
            print(f"  Note: {state_code} files copied to Verified folder ({note})")
        else:
            print(f"  Reports: ../data/output/Reports/{state_code}")
        print(f"{'='*60}")
        
        logging.info(f"\n{'='*60}")
        logging.info(f"Processing Summary for {state_code}:")
        logging.info(f"  Total files: {len(pdf_files)}")
        logging.info(f"  Text conversion successful: {success_count}")
        logging.info(f"  Text conversion failed: {len(failed_files)}")
        if state_code in ["DC", "IN"]:
            skip_reason = "has no formatting to analyze" if state_code == "DC" else "DOCX analysis not required"
            logging.info(f"  DOCX analysis skipped: {len(pdf_files)} ({state_code} {skip_reason})")
        else:
            logging.info(f"  DOCX analysis successful: {docx_success_count}")
            logging.info(f"  DOCX analysis failed: {len(docx_failed_files)}")
        if state_code in ["DC", "IN"]:
            logging.info(f"  Files copied to Verified: {verification_success_count}")
            logging.info(f"  Failed to copy to Verified: {len(verification_failed_files)}")
        else:
            logging.info(f"  Verification successful: {verification_success_count}")
            logging.info(f"  Verification failed: {len(verification_failed_files)}")
        
        if failed_files:
            logging.info(f"\nText failed files:")
            for file in failed_files:
                logging.info(f"  - {file}")
                
        if docx_failed_files and state_code not in ["DC", "IN"]:
            logging.info(f"\nDOCX failed files:")
            for file in docx_failed_files:
                logging.info(f"  - {file}")
        
        if verification_failed_files:
            if state_code in ["DC", "IN"]:
                logging.info(f"\nFailed to copy to Verified folder:")
            else:
                logging.info(f"\nVerification failed files:")
            for file in verification_failed_files:
                logging.info(f"  - {file}")
        
        logging.info(f"{'='*60}")
        
    except Exception as e:
        logging.error(f"Error in process_state_pdfs: {e}")
        print(f"Error processing PDFs: {e}")

def display_available_states():
    """Display available states for selection."""
    print("\n" + "="*60)
    print("AVAILABLE STATES FOR PDF TO TEXT CONVERSION")
    print("="*60)
    
    available_states = []
    for i, (code, info) in enumerate(AVAILABLE_STATES.items(), 1):
        # Check if examples exist for this state using state_code approach
        examples_path = os.path.join(PROJECT_ROOT, "data", "Examples", code)
        
        # Check examples
        has_examples = False
        examples_count = 0
        
        if os.path.exists(examples_path):
            examples_count = len([f for f in os.listdir(examples_path) if f.endswith('_input.pdf')])
            has_examples = examples_count > 0
        
        # Check if state-specific prompt exists
        prompt_path = os.path.join(PROJECT_ROOT, "data", "Prompts", f"{code}_Prompt.txt")
        has_prompt = os.path.exists(prompt_path)
        
        # Build status string
        status_parts = []
        if has_examples:
            status_parts.append(f"{examples_count} examples")
        else:
            status_parts.append("no examples")
        
        if has_prompt:
            status_parts.append("custom prompt")
        else:
            status_parts.append("default prompt")
        
        status = f"({', '.join(status_parts)})"
        print(f"{i:2}. {code} - {info['name']} {status}")
        available_states.append(code)
    
    print("="*60)
    return available_states

def select_state():
    """Allow user to select a state for processing."""
    while True:
        available_states = display_available_states()
        
        try:
            choice = input(f"\nSelect a state (1-{len(available_states)}) or 'q' to quit: ").strip()
            
            if choice.lower() == 'q':
                print("Exiting...")
                sys.exit(0)
            
            choice_num = int(choice)
            if 1 <= choice_num <= len(available_states):
                selected_state = available_states[choice_num - 1]
                state_name = AVAILABLE_STATES[selected_state]["name"]
                
                print(f"\nSelected: {selected_state} - {state_name}")
                
                # Confirm selection
                confirm = input("Proceed with this selection? (y/n): ").strip().lower()
                if confirm in ['y', 'yes']:
                    return selected_state
                else:
                    continue
            else:
                print(f"Invalid choice. Please select a number between 1 and {len(available_states)}")
                
        except ValueError:
            print("Invalid input. Please enter a number or 'q' to quit.")
        except KeyboardInterrupt:
            print("\nExiting...")
            sys.exit(0)

def setup_state_configuration(state_code):
    """Set up configuration for the selected state."""
    logging.info(f"Setting up configuration for {state_code}")
    
    # Ensure input/output folders exist for this state
    state_input_folder = os.path.join(INPUT_FOLDER, state_code)
    state_output_folder = os.path.join(OUTPUT_FOLDER, state_code)
    
    os.makedirs(state_input_folder, exist_ok=True)
    os.makedirs(state_output_folder, exist_ok=True)
    
    # Examples will be loaded using state_code
    examples_folder = os.path.join(PROJECT_ROOT, "data", "Examples", state_code)
    
    logging.info(f"State-specific folders:")
    logging.info(f"  Input: {state_input_folder}")
    logging.info(f"  Output: {state_output_folder}")
    logging.info(f"  Examples: {examples_folder}")
    
    return state_input_folder, state_output_folder

def main():
    """Main function."""
    try:
        print("PDF to Text Conversion using Claude Opus 4")
        print("Multi-State Support with Few-Shot Learning")
        print("="*60)
        
        # State selection
        selected_state = select_state()
        state_input_folder, state_output_folder = setup_state_configuration(selected_state)
        
        logging.info(f"Starting PDF to Text Conversion for {selected_state}")
        logging.info(f"Input folder: {state_input_folder}")
        logging.info(f"Output folder: {state_output_folder}")
        
        # Get Anthropic client
        client = get_anthropic_client()
        if not client:
            logging.error("Failed to initialize Anthropic client. Exiting.")
            sys.exit(1)
        
        # Load state-specific prompt
        conversion_prompt = load_conversion_prompt(selected_state)
        
        # Load example pairs for few-shot learning
        logging.info(f"\nLoading example pairs for {selected_state}...")
        examples = load_example_pairs(selected_state)
        
        if examples:
            examples_folder = os.path.join(PROJECT_ROOT, "data", "Examples", selected_state)
            logging.info(f"Loaded {len(examples)} example pair(s) from {examples_folder}")
            for i, (_, _, filename) in enumerate(examples, 1):
                logging.info(f"  Example {i}: {filename}")
        else:
            logging.info("No examples found - using zero-shot prompting")
        
        # Process all PDFs for the selected state
        process_state_pdfs(client, examples, selected_state, state_input_folder, state_output_folder, conversion_prompt)
        
        logging.info("Program completed successfully!")
        
    except Exception as e:
        logging.error(f"Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    # Set up logging for CLI usage
    setup_logging()
    main()
    print("\nPDF to Text conversion complete! Check the data/output folder for results.")