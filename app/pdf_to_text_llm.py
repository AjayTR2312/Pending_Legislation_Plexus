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
import base64

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
    call_claude_api
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
    "IA": {"name": "Iowa"},
    "IL": {"name": "Illinois"},
    "IN": {"name": "Indiana"},
    "KS": {"name": "Kansas"},
    "NY": {"name": "New York"},
    "OH": {"name": "Ohio"},
    "TX": {"name": "Texas"}
}

# Set up logging
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
        state_prompt_file = os.path.join("../data/Prompts", f"{state_code}_Prompt.txt")
        if os.path.exists(state_prompt_file):
            try:
                with open(state_prompt_file, "r", encoding='utf-8') as f:
                    prompt = f.read().strip()
                    logging.info(f"Successfully loaded state-specific prompt for {state_code} from {state_prompt_file}")
                    return prompt
            except Exception as e:
                logging.error(f"Error loading state-specific prompt file {state_prompt_file}: {e}")
    
    # Fall back to default prompt
    prompt_file = os.path.join("../data/Prompts", "PDF_to_Text_Prompt.txt")
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
        cred_file_path = os.path.join("../config", "pdfservices-api-credentials.json")
        
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
            examples_folder = os.path.join("../data/Examples", state_code)
        else:
            # Fallback to a default if no state_code provided
            examples_folder = "../data/Examples"
            
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

def process_pdf_with_claude(client, pdf_path, examples, conversion_prompt):
    """Process PDF using Claude Opus 4 with few-shot examples."""
    try:
        logging.info(f"Processing PDF: {pdf_path}")
        
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
        
        # Create message with Claude
        logging.info("Sending request to Claude Opus 4...")
        message = client.messages.create(
            model="claude-opus-4-20250514",
            max_tokens=8192,  # Increased for longer documents with examples
            temperature=0,
            messages=messages
        )
        
        # Extract text from response
        formatted_text = ""
        for block in message.content:
            if hasattr(block, 'text'):
                formatted_text += block.text
        
        logging.info(f"Successfully processed PDF with Claude")
        return formatted_text
        
    except Exception as e:
        logging.error(f"Error processing PDF with Claude: {e}")
        return None

def save_to_text_file(content, output_path):
    """Save content to a text file."""
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(content)
        logging.info(f"Saved output to: {output_path}")
        return True
    except Exception as e:
        logging.error(f"Error saving file: {e}")
        return False

def read_file_content(file_path, encoding='utf-8'):
    """Read file content with error handling."""
    try:
        with open(file_path, 'r', encoding=encoding) as f:
            return f.read()
    except Exception as e:
        logging.error(f"Error reading file {file_path}: {e}")
        return None

def verify_text_with_llm(text_file_path, summary_file_path, state_code, client):
    """
    Verify text file using LLM with summary reference.
    
    Args:
        text_file_path (str): Path to the text file to verify
        summary_file_path (str): Path to the summary reference file
        state_code (str): State code for state-specific verification
        client: Anthropic client instance
    
    Returns:
        str: Corrected text or None if failed
    """
    try:
        print("🤖 Running LLM-based tag verification...")
        
        # Read summary file
        logging.info(f"Reading summary file: {summary_file_path}")
        summary_content = read_file_content(summary_file_path)
        if not summary_content:
            logging.error("Failed to read summary file")
            return None
        
        # Extract formatted items section
        formatted_summary = extract_formatted_items_summary(summary_content)
        logging.info(f"Extracted formatted items summary")
        
        # Read text file
        logging.info(f"Reading text file: {text_file_path}")
        text_content = read_file_content(text_file_path)
        if not text_content:
            logging.error("Failed to read text file")
            return None
        
        # Create verification prompt
        logging.info(f"Creating {state_code}-specific verification prompt")
        prompt = create_verification_prompt(formatted_summary, text_content, state_code)
        if not prompt:
            logging.error("Failed to create verification prompt")
            return None
        
        # Call Claude API for verification
        logging.info("Calling Claude API for verification...")
        corrected_text = call_claude_api(prompt, client)
        
        if corrected_text:
            logging.info("Successfully received corrected text from Claude")
            return corrected_text
        else:
            logging.error("Failed to get corrected text from Claude")
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
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        verified_filename = f"{original_path.stem}_VERIFIED_{timestamp}.txt"
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
                if save_to_text_file(formatted_text, output_path):
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
            if text_success and docx_success and summary_file_path:
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
        print(f"  Successful: {docx_success_count}")
        print(f"  Failed: {len(docx_failed_files)}")
        print(f"\nLLM VERIFICATION:")
        print(f"  Successful: {verification_success_count}")
        print(f"  Failed: {len(verification_failed_files)}")
        
        if failed_files:
            print(f"\nText conversion failed files:")
            for file in failed_files:
                print(f"  - {file}")
        
        if docx_failed_files:
            print(f"\nDOCX analysis failed files:")
            for file in docx_failed_files:
                print(f"  - {file}")
        
        if verification_failed_files:
            print(f"\nVerification failed files:")
            for file in verification_failed_files:
                print(f"  - {file}")
        
        print(f"\nOutput locations:")
        print(f"  Text files: {output_folder}")
        print(f"  DOCX files: ../data/output/Output_Docx/{state_code}")
        print(f"  Summary files: ../data/output/Verify/{state_code}")
        print(f"  Verified files: ../data/output/Verified/{state_code}")
        print(f"  Reports: ../data/output/Reports/{state_code}")
        print(f"{'='*60}")
        
        logging.info(f"\n{'='*60}")
        logging.info(f"Processing Summary for {state_code}:")
        logging.info(f"  Total files: {len(pdf_files)}")
        logging.info(f"  Text conversion successful: {success_count}")
        logging.info(f"  Text conversion failed: {len(failed_files)}")
        logging.info(f"  DOCX analysis successful: {docx_success_count}")
        logging.info(f"  DOCX analysis failed: {len(docx_failed_files)}")
        logging.info(f"  Verification successful: {verification_success_count}")
        logging.info(f"  Verification failed: {len(verification_failed_files)}")
        
        if failed_files:
            logging.info(f"\nText failed files:")
            for file in failed_files:
                logging.info(f"  - {file}")
                
        if docx_failed_files:
            logging.info(f"\nDOCX failed files:")
            for file in docx_failed_files:
                logging.info(f"  - {file}")
        
        if verification_failed_files:
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
        examples_path = os.path.join("../data/Examples", code)
        
        # Check examples
        has_examples = False
        examples_count = 0
        
        if os.path.exists(examples_path):
            examples_count = len([f for f in os.listdir(examples_path) if f.endswith('_input.pdf')])
            has_examples = examples_count > 0
        
        # Check if state-specific prompt exists
        prompt_path = os.path.join("../data/Prompts", f"{code}_Prompt.txt")
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
    
    # Examples will be loaded using state_code: ../data/Examples/{state_code}
    examples_folder = os.path.join("../data/Examples", state_code)
    
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
            examples_folder = os.path.join("../data/Examples", selected_state)
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