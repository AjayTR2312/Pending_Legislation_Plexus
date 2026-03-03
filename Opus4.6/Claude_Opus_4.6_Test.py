"""
Simple PDF to Text Converter using Claude API (Visual Mode)
============================================================
 
Converts PDFs to text using Claude Opus 4.6 with visual interpretation.
Each PDF page is rendered as an image and sent to Claude for visual reading,
which provides more accurate detection of strikethrough formatting.
 
Uses pypdfium2 (Google PDFium) for PDF-to-image rendering.
 
Usage:
    python simple_pdf_to_text.py <pdf_file> [state_code] [output_file] [--reconcile]
 
Options:
    --reconcile    Enable 3-pass reconciliation (visual + document + merge).
                   Disabled by default for faster runtime.
 
Examples:
    python simple_pdf_to_text.py document.pdf
    python simple_pdf_to_text.py document.pdf FL
    python simple_pdf_to_text.py document.pdf FL output.txt
    python simple_pdf_to_text.py document.pdf FL output.txt --reconcile
    python simple_pdf_to_text.py document.pdf FL --reconcile
"""
 
import anthropic
import requests
import json
import ssl
import os
import time
import sys
import base64
import io
import logging
from pathlib import Path
from datetime import datetime
 
import pypdfium2 as pdfium
from PIL import Image
 
# Fix encoding for Windows console
if sys.platform == 'win32':
    import codecs
    sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')
    sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'strict')
 
# Disable SSL warnings
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
ssl._create_default_https_context = ssl._create_unverified_context
 
# Configuration
WORKSPACE_ID = "AgentTraininJaH4"
TOKEN_URL = "https://aiplatform.gcs.int.thomsonreuters.com/v1/anthropic/token"
PROJECT_ROOT = Path(__file__).parent.absolute()
PROMPTS_FILE = os.path.join(PROJECT_ROOT, "conversion_prompts.md")
 
# Available states for prompts
AVAILABLE_STATES = {
    "AR": "Arkansas", "CA": "California", "CO": "Colorado", "CT": "Connecticut",
    "DC": "District of Columbia", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "IA": "Iowa", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana",
    "KS": "Kansas", "ME": "Maine", "MN": "Minnesota", "NC": "North Carolina",
    "ND": "North Dakota", "NE": "Nebraska", "NY": "New York", "OH": "Ohio",
    "OK": "Oklahoma", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "VT": "Vermont", "WI": "Wisconsin", "WY": "Wyoming"
}
 
# Cached prompts from markdown file
_PROMPTS_CACHE = None
 
def load_prompts_from_markdown():
    """
    Load prompt templates from conversion_prompts.md file.
   
    Returns:
        dict: Dictionary containing all prompt templates
    """
    global _PROMPTS_CACHE
   
    if _PROMPTS_CACHE is not None:
        return _PROMPTS_CACHE
   
    try:
        with open(PROMPTS_FILE, 'r', encoding='utf-8') as f:
            content = f.read()
       
        # Parse sections from markdown
        sections = {}
        current_section = None
        current_content = []
       
        for line in content.split('\n'):
            if line.startswith('## '):
                if current_section:
                    sections[current_section] = '\n'.join(current_content).strip()
                current_section = line[3:].strip()
                current_content = []
            elif line.startswith('---'):
                continue
            elif current_section:
                current_content.append(line)
       
        # Add last section
        if current_section:
            sections[current_section] = '\n'.join(current_content).strip()
       
        _PROMPTS_CACHE = sections
        logging.info(f"✅ Loaded {len(sections)} prompt templates from {PROMPTS_FILE}")
        return sections
       
    except Exception as e:
        logging.error(f"❌ Error loading prompts from markdown: {e}")
        return {}
 
def setup_logging():
    """Setup simple logging configuration."""
    log_folder = os.path.join(PROJECT_ROOT, "logs")
    os.makedirs(log_folder, exist_ok=True)
   
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = os.path.join(log_folder, f'simple_pdf_to_text_{timestamp}.log')
   
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    logging.info(f"Logging to: {log_file}")
 
def get_anthropic_client():
    """
    Get Anthropic API client with credentials from Thomson Reuters platform.
   
    Returns:
        anthropic.Anthropic: Client instance or None if failed
    """
    try:
        payload = {"workspace_id": WORKSPACE_ID}
        # Use verify=False since SSL verification is disabled globally
        resp = requests.post(TOKEN_URL, headers=None, json=payload, verify=False)
        credentials = json.loads(resp.content)
       
        if 'anthropic_api_key' in credentials:
            logging.info("✅ Successfully obtained Anthropic API key")
            return anthropic.Anthropic(api_key=credentials["anthropic_api_key"])
        else:
            logging.error(f"❌ No Anthropic API key found in response")
            return None
    except Exception as e:
        logging.error(f"❌ Failed to initialize Anthropic client: {e}")
        return None
 
def load_state_template(state_code=None):
    """
    Load state-specific rules from netscan_state_template.json.
 
    Args:
        state_code (str): Two-letter state code, or None for default
 
    Returns:
        dict: State config dict, or None if not found
    """
    template_path = os.path.join(PROJECT_ROOT, "netscan_state_template.json")
    if not os.path.exists(template_path):
        logging.warning(f"Template file not found: {template_path}")
        return None
 
    try:
        with open(template_path, 'r', encoding='utf-8') as f:
            template = json.load(f)
 
        if state_code and state_code in template.get("states", {}):
            state_config = template["states"][state_code]
            logging.info(f"Loaded template for {state_code} - {state_config.get('state_name', '')}")
            return state_config
        elif state_code:
            logging.warning(f"State '{state_code}' not found in template")
            return None
        else:
            return None
    except Exception as e:
        logging.error(f"Error loading template: {e}")
        return None
 
def detect_state_from_filename(pdf_path):
    """
    Attempt to detect state code from filename using numerical_id patterns
    in the template.
 
    Args:
        pdf_path (str): Path to PDF file
 
    Returns:
        str: Detected state code, or None
    """
    template_path = os.path.join(PROJECT_ROOT, "netscan_state_template.json")
    if not os.path.exists(template_path):
        return None
 
    try:
        with open(template_path, 'r', encoding='utf-8') as f:
            template = json.load(f)
 
        filename = Path(pdf_path).name
        for code, config in template.get("states", {}).items():
            num_id = config.get("numerical_id", "")
            if num_id and filename.startswith(num_id):
                logging.info(f"🔍 Auto-detected state {code} ({config['state_name']}) from filename pattern")
                return code
        return None
    except Exception:
        return None
 
def detect_state_from_content(client, pdf_path):
    """
    Detect state from PDF content by analyzing the first page.
    Uses Claude to identify state based on bill format, structure, language, and content.
 
    Args:
        client: Anthropic client instance
        pdf_path (str): Path to PDF file
 
    Returns:
        str: Detected state code, or None
    """
    try:
        logging.info("🔍 Analyzing PDF content to detect state...")
       
        # Render first page only
        pdf = pdfium.PdfDocument(pdf_path)
        if len(pdf) == 0:
            return None
       
        page = pdf[0]
        bitmap = page.render(scale=400/72)
        pil_image = bitmap.to_pil()
       
        # Convert to base64
        buf = io.BytesIO()
        pil_image.save(buf, format="PNG", optimize=True)
        png_bytes = buf.getvalue()
        b64_data = base64.b64encode(png_bytes).decode('utf-8')
        pdf.close()
 
        # Ask Claude to identify the state
        template_path = os.path.join(PROJECT_ROOT, "netscan_state_template.json")
        with open(template_path, 'r', encoding='utf-8') as f:
            template = json.load(f)
       
        state_list = ", ".join([f"{code} ({config['state_name']})"
                                for code, config in template.get("states", {}).items()])
 
        prompt = f"""Analyze this legislative bill document and identify which US state it is from.
 
Available states: {state_list}
 
Examine the ENTIRE PAGE for clues:
1. Bill number format (e.g., HB 1234, SB 567, different states use different formats)
2. Legislative language patterns (e.g., "Be it enacted by the Legislature of the State of...")
3. Document structure and layout (margins, line numbers, formatting style)
4. Any reference to state name, legislature name, or government body
5. Footer/header patterns (some states have distinctive formats)
6. Legal citation formats (different states cite statutes differently)
7. Committee names, sponsor names, or procedural language
 
State name may NOT appear explicitly in the header. You must judge based on the overall bill structure, format, and legislative language patterns that are unique to each state.
 
Respond with ONLY the 2-letter state code (e.g., "KS", "FL", "CO"). If you cannot confidently determine the state, respond with "UNKNOWN"."""
 
        message = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=100,
            temperature=0,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": b64_data,
                        },
                    },
                    {
                        "type": "text",
                        "text": prompt
                    }
                ],
            }]
        )
 
        response = message.content[0].text.strip().upper()
       
        # Validate response is a known state code
        if response in template.get("states", {}):
            state_name = template["states"][response]["state_name"]
            logging.info(f"✅ Auto-detected state {response} ({state_name}) from PDF content analysis")
            return response
       
        logging.warning(f"⚠️  Could not confidently detect state (response: {response})")
        return None
 
    except Exception as e:
        logging.error(f"❌ Content-based state detection failed: {e}")
        return None
 
def get_conversion_prompt(state_config=None):
    """
    Build conversion prompt from state template rules.
    Uses state-specific formatting rules for accurate markup detection.
 
    Args:
        state_config (dict): State config from netscan_state_template.json, or None for default
 
    Returns:
        str: Conversion prompt
    """
    prompts = load_prompts_from_markdown()
    precision_guidance = prompts.get("🎯 PRECISION GUIDANCE (Common to All Templates)", "")
 
    if not state_config:
        default_prompt = prompts.get("📋 TEMPLATE 1: Default Visual Mode Prompt (No State Specified)", "")
        return default_prompt.replace("{PRECISION_GUIDANCE}", precision_guidance)
 
    # Build deletions instruction
    deletions = state_config.get("deletions", "")
    delete_instruction = f"REMOVE the following from the output: {deletions}." if deletions else ""
 
    # Build delete markup instruction
    delete_markup = state_config.get("delete_markup", {})
    delete_rules = []
    if delete_markup.get("strike_out"):
        delete_rules.append("text with strikethrough/stricken formatting (a horizontal line drawn through it)")
    if delete_markup.get("brackets"):
        bracket_type = delete_markup.get("bracket_type", "bracketed")
        delete_rules.append(f"{bracket_type} text INCLUDING THE BRACKET CHARACTERS THEMSELVES (e.g., [D>[text]<D] NOT [D>text<D])")
    if delete_markup.get("italic"):
        delete_rules.append("italic text that is also bracketed")
    if delete_markup.get("bold"):
        delete_rules.append("bold text that is also bracketed")
    for rule in delete_markup.get("custom_rules", []):
        delete_rules.append(rule)
 
    delete_markup_instruction = ""
    if delete_rules:
        delete_markup_instruction = (
            "DELETED TEXT MARKUP: Wrap the following types of text with [D> and <D] tags "
            "(opening tag is [D> and closing tag is <D]): "
            + "; ".join(delete_rules) + ". "
        )
 
    # Build add markup instruction
    add_markup = state_config.get("add_markup", {})
    add_rules = []
    if add_markup.get("underline"):
        add_rules.append("underlined text (including special characters such as commas, periods, semicolons, etc. If a special character like ',' is underlined, it MUST be tagged)")
    if add_markup.get("bold"):
        add_rules.append("bold text")
    if add_markup.get("italic"):
        add_rules.append("italic text")
    if add_markup.get("all_caps"):
        condition = add_markup.get("all_caps_condition", "")
        if condition:
            add_rules.append(f"ALL CAPS text ({condition})")
        else:
            add_rules.append("ALL CAPS text")
    if add_markup.get("brackets"):
        add_rules.append("bracketed text")
    for rule in add_markup.get("custom_rules", []):
        add_rules.append(rule)
   
    # Log the add_rules for debugging
    if add_rules:
        logging.info(f"Add markup rules for {state_config.get('state_name', 'Unknown')}: {', '.join(add_rules)}")
    else:
        logging.info(f"No add markup rules for {state_config.get('state_name', 'Unknown')}")
 
    add_markup_instruction = ""
    if add_rules:
        # Build explicit negative instructions FIRST for maximum emphasis
        negative_instructions = []
        if not add_markup.get("bold"):
            negative_instructions.append(
                "CRITICAL: Do NOT tag bold text. Bold text has thicker strokes but should NOT be tagged. "
                "Even if bold text looks important or different, do NOT tag it."
            )
        if not add_markup.get("underline"):
            negative_instructions.append(
                "CRITICAL: Do NOT tag underlined text. Even if text has a line underneath, do NOT tag it."
            )
        if not add_markup.get("italic"):
            negative_instructions.append(
                "CRITICAL: Do NOT tag italic text. Even if text is slanted, do NOT tag it."
            )
        if not add_markup.get("all_caps"):
            negative_instructions.append(
                "CRITICAL: Do NOT tag ALL CAPS text. Even if text is in all capital letters, do NOT tag it."
            )
       
        # Build format-specific visual detection hints ONLY for enabled formats
        visual_hints = []
        if add_markup.get("italic"):
            visual_hints.append(
                "For italic: italic characters are visually slanted/tilted to the right. "
                "Regular (roman) text has upright vertical strokes. "
                "If text has the SAME upright style as the body text, it is NOT italic."
            )
        if add_markup.get("underline"):
            visual_hints.append(
                "For underline: look for a visible horizontal line drawn BENEATH the text baseline. "
                "The underline runs UNDER the characters, not through them. "
                "Do NOT confuse underline with strikethrough (which goes THROUGH the middle of text horizontally). "
                "CRITICAL DISTINCTION: Bold text has thicker strokes but NO line underneath - bold is NOT underlined. "
                "ALL CAPS text is NOT underlined unless it has a visible line beneath it. "
                "If there is no visible horizontal line beneath the text, it is NOT underlined, even if it looks different from body text."
            )
        if add_markup.get("bold"):
            visual_hints.append(
                "For bold: bold text has visually thicker/heavier strokes compared to regular body text."
            )
        if add_markup.get("all_caps"):
            visual_hints.append(
                "For ALL CAPS: text where every letter is uppercase, distinct from normal mixed-case text."
            )
        if add_markup.get("brackets"):
            visual_hints.append(
                "For brackets: text enclosed within bracket characters [ ]."
            )
       
        # Combine instructions with negative rules FIRST
        formatting_guidance = " ".join(negative_instructions + visual_hints)
 
        add_markup_instruction = (
            "ADDED TEXT MARKUP: Wrap ONLY the following types of text with [A> and <A] tags "
            "(opening tag is [A> and closing tag is <A]): "
            + "; ".join(add_rules) + ". "
            "CRITICAL TAG FORMAT REQUIREMENTS: "
            "1. You MUST use [A> and <A] tags exactly as specified. Example: [A>new text<A] "
            "2. Do NOT use asterisks (*), do NOT use Markdown formatting (**bold**, *italic*), do NOT use any other markup style. "
            "3. NEVER write *text* or **text** - this is WRONG. Always write [A>text<A] instead. "
            "4. For text that should NOT be tagged: output it as PLAIN TEXT with NO formatting markers at all. "
            "5. Do NOT output **text** for bold text that should not be tagged - just output the plain text without any markers. "
            "STRICT VISUAL DETECTION RULES: "
            "6. Tag ONLY THE EXACT formatting types listed above: " + "; ".join(add_rules) + ". "
            "7. Do NOT tag ANY other formatting types, regardless of how they look. "
            "8. ONLY use visual appearance in the image to detect the SPECIFIC formatting types listed. "
            "9. Do NOT use your knowledge of legislative conventions, bill formatting rules, or "
            "how states typically mark new/added sections. Ignore what you know about how bills work. "
            "10. Compare each word ONLY to the surrounding body text on the same page. "
            "If it looks the same as the body text, do NOT tag it. "
            f"{formatting_guidance} "
        )
 
    state_name = state_config.get("state_name", "")
    prompt = (
        f"Convert these PDF page images of a {state_name} legislative bill to text. "
        f"{delete_instruction} "
        f"{delete_markup_instruction}"
        f"{add_markup_instruction}"
        "Use regular quotes, not smart quotes. "
        f"{precision_guidance} "
        "Output only the converted text with markup tags, nothing else. "
        "Combine all pages into one continuous document."
    )
 
    logging.info(f"Built prompt for {state_name}: {len(prompt)} chars")
    return prompt
 
def get_document_mode_prompt(state_config=None):
    """
    Build a document mode prompt from state template rules.
    Similar to visual mode prompt but adapted for PDF document text extraction.
 
    Args:
        state_config (dict): State config from netscan_state_template.json, or None for default
 
    Returns:
        str: Document mode conversion prompt
    """
    if not state_config:
        return (
            "Convert this PDF to text. "
            "For any text that has strikethrough/stricken formatting, wrap it with [D> and <D] tags "
            "(opening tag is [D> and closing tag is <D]). "
            "Remove line numbers, headers, footers, and page numbers. "
            "Use regular quotes, not smart quotes. "
            "Output only the converted text, nothing else."
        )
 
    state_name = state_config.get("state_name", "")
    deletions = state_config.get("deletions", "")
    delete_instruction = f"REMOVE the following from the output: {deletions}." if deletions else ""
 
    # Build delete markup rules
    delete_markup = state_config.get("delete_markup", {})
    delete_rules = []
    if delete_markup.get("strike_out"):
        delete_rules.append("text with strikethrough/stricken formatting")
    if delete_markup.get("brackets"):
        bracket_type = delete_markup.get("bracket_type", "bracketed")
        delete_rules.append(f"{bracket_type} text (text enclosed in brackets)")
    if delete_markup.get("italic"):
        delete_rules.append("italic text that is also bracketed")
    if delete_markup.get("bold"):
        delete_rules.append("bold text that is also bracketed")
    for rule in delete_markup.get("custom_rules", []):
        delete_rules.append(rule)
 
    delete_markup_instruction = ""
    if delete_rules:
        delete_markup_instruction = (
            "DELETED TEXT MARKUP: Wrap the following types of text with [D> and <D] tags "
            "(opening tag is [D> and closing tag is <D]): "
            + "; ".join(delete_rules) + ". "
        )
 
    # Build add markup rules
    add_markup = state_config.get("add_markup", {})
    add_rules = []
    if add_markup.get("underline"):
        add_rules.append("underlined text")
    if add_markup.get("bold"):
        add_rules.append("bold text")
    if add_markup.get("italic"):
        add_rules.append("italic text")
    if add_markup.get("all_caps"):
        condition = add_markup.get("all_caps_condition", "")
        if condition:
            add_rules.append(f"ALL CAPS text ({condition})")
        else:
            add_rules.append("ALL CAPS text")
    if add_markup.get("brackets"):
        add_rules.append("bracketed text")
    for rule in add_markup.get("custom_rules", []):
        add_rules.append(rule)
 
    add_markup_instruction = ""
    if add_rules:
        # Build format-specific hints
        visual_hints = []
        if add_markup.get("italic"):
            visual_hints.append("For italic: only text visually slanted/tilted, not regular upright text.")
        if add_markup.get("underline"):
            visual_hints.append(
                "For underline: a visible line beneath the text, not strikethrough. "
                "Bold text is NOT underlined."
            )
        if add_markup.get("bold"):
            visual_hints.append("For bold: visually thicker/heavier strokes than body text.")
        if add_markup.get("all_caps"):
            visual_hints.append("For ALL CAPS: every letter uppercase, distinct from mixed-case.")
        if add_markup.get("brackets"):
            visual_hints.append("For brackets: text enclosed in [ ].")
       
        # Add explicit negative instructions for formatting types NOT in the rules
        negative_hints = []
        if not add_markup.get("bold"):
            negative_hints.append("Do NOT tag bold text.")
        if not add_markup.get("underline"):
            negative_hints.append("Do NOT tag underlined text.")
        if not add_markup.get("italic"):
            negative_hints.append("Do NOT tag italic text.")
        if not add_markup.get("all_caps"):
            negative_hints.append("Do NOT tag ALL CAPS text.")
       
        visual_guidance = " ".join(visual_hints)
        if negative_hints:
            visual_guidance += " " + " ".join(negative_hints)
 
        add_markup_instruction = (
            "ADDED TEXT MARKUP: Wrap the following types of text with [A> and <A] tags "
            "(opening tag is [A> and closing tag is <A]): "
            + "; ".join(add_rules) + ". "
            "CRITICAL TAG FORMAT REQUIREMENTS: "
            "1. You MUST use [A> and <A] tags exactly as specified. Example: [A>new text<A] "
            "2. Do NOT use asterisks (*), do NOT use Markdown formatting, do NOT use any other markup style. "
            "3. NEVER write *text* or **text** - this is WRONG. Always write [A>text<A] instead. "
            "STRICT DETECTION: Only tag the EXACT formatting types listed above. "
            "Only tag text that is VISUALLY rendered in the SPECIFIC formatting types listed. "
            f"{visual_guidance} "
            "Do NOT infer or guess formatting. Do NOT tag formatting types not explicitly listed. "
        )
 
    prompt = (
        f"Convert this {state_name} legislative bill PDF to text. "
        f"{delete_instruction} "
        f"{delete_markup_instruction}"
        f"{add_markup_instruction}"
        "Use regular quotes, not smart quotes. "
        "Pay close attention to subsection labels like (a), (b), (c) - they may be independently "
        "struck through even when surrounding text is not. "
        "Output only the converted text with markup tags, nothing else."
    )
 
    logging.info(f"Built document mode prompt for {state_name}: {len(prompt)} chars")
    return prompt
 
def trim_image_borders(image, border_percent=2):
    """
    Trim borders from an image to remove headers, footers, page numbers, and margins.
   
    Args:
        image: PIL Image object
        border_percent (int): Percentage of each edge to trim (default 5%)
       
    Returns:
        PIL Image: Cropped image
    """
    width, height = image.size
   
    # Calculate pixels to trim from each edge
    left_trim = int(width * border_percent / 100)
    right_trim = width - int(width * border_percent / 100)
    top_trim = int(height * border_percent / 100)
    bottom_trim = height - int(height * border_percent / 100)
   
    # Crop the image
    cropped = image.crop((left_trim, top_trim, right_trim, bottom_trim))
   
    logging.debug(f"  Trimmed borders: {width}x{height} → {cropped.size[0]}x{cropped.size[1]}")
    return cropped
 
def pdf_pages_to_images(pdf_path, dpi=400, trim_borders=True, border_percent=5):
    """
    Convert each PDF page to a base64-encoded PNG image using pypdfium2.
 
    Args:
        pdf_path (str): Path to PDF file
        dpi (int): Resolution for rendering (default 400 DPI)
 
    Returns:
        list: List of base64-encoded PNG strings, or None if failed
    """
    try:
        pdf = pdfium.PdfDocument(pdf_path)
        num_pages = len(pdf)
        logging.info(f"📄 PDF loaded: {num_pages} pages, rendering at {dpi} DPI")

        images = []
        scale = dpi / 72  # pypdfium2 default is 72 DPI

        for i in range(num_pages):
            page = pdf[i]
            bitmap = page.render(scale=scale)
            pil_image = bitmap.to_pil()

            # Convert to PNG bytes entirely in-memory — no disk writes needed
            buf = io.BytesIO()
            pil_image.save(buf, format="PNG", optimize=True)
            png_bytes = buf.getvalue()
            b64_data = base64.b64encode(png_bytes).decode('utf-8')

            images.append(b64_data)
            logging.info(f"  Page {i+1}/{num_pages}: {len(png_bytes)/1024:.0f} KB rendered in-memory")

        pdf.close()
        logging.info(f"📄 All {num_pages} pages rendered to images (in-memory, no temp files)")
        return images
 
    except Exception as e:
        logging.error(f"❌ Error converting PDF pages to images: {e}")
        return None
 
def convert_pdf_to_text(client, pdf_path, prompt, batch_size=None):
    """
    Convert PDF to text using Claude API with visual interpretation.
    Processes all pages in a single API call for maximum consistency.
 
    Args:
        client: Anthropic client instance
        pdf_path (str): Path to PDF file
        prompt (str): Conversion prompt
        batch_size (int): Ignored - kept for backward compatibility
 
    Returns:
        str: Converted text or None if failed
    """
    try:
        start_time = time.time()
        logging.info(f"🔄 Converting PDF (visual mode, ALL PAGES IN ONE CALL): {pdf_path}")
 
        # Render PDF pages to images
        page_images = pdf_pages_to_images(pdf_path)
        if not page_images:
            return None
 
        total_pages = len(page_images)
       
        # Build content array with ALL pages
        content = []
        for img_b64 in page_images:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": img_b64,
                },
            })
 
        # Add prompt
        content.append({
            "type": "text",
            "text": prompt
        })
 
        messages = [{"role": "user", "content": content}]
 
        # Call Claude API with streaming - ALL PAGES AT ONCE
        logging.info(f"🤖 Processing all {total_pages} pages in single API call...")
 
        # Retry loop for transient overload / server errors
        max_retries = 5
        retry_delays = [10, 30, 60, 120, 180]  # seconds between retries
        converted_text = ""
        for attempt in range(max_retries):
            try:
                converted_text = ""
                with client.messages.stream(
                    model="claude-opus-4-6",
                    max_tokens=128000,
                    temperature=0,
                    messages=messages
                ) as stream:
                    for text in stream.text_stream:
                        converted_text += text
                break  # success — exit retry loop
            except Exception as e:
                err_str = str(e)
                is_retryable = (
                    "overloaded" in err_str.lower()
                    or "529" in err_str
                    or "500" in err_str
                    or "503" in err_str
                )
                if is_retryable and attempt < max_retries - 1:
                    delay = retry_delays[attempt]
                    logging.warning(f"  ⚠️  API error (attempt {attempt+1}/{max_retries}): {err_str[:120]}. Retrying in {delay}s...")
                    print(f"  ⚠️  API error — retrying in {delay}s (attempt {attempt+1}/{max_retries})...")
                    time.sleep(delay)
                else:
                    raise  # non-retryable or out of retries

        # Log total runtime
        total_time = time.time() - start_time
        minutes = int(total_time // 60)
        seconds = int(total_time % 60)
        logging.info(f"✅ Conversion complete: {len(converted_text)} characters (single pass)")
        logging.info(f"⏱️  Total runtime: {minutes}m {seconds}s ({total_time:.1f} seconds)")
        return converted_text
 
    except Exception as e:
        logging.error(f"❌ Error converting PDF: {e}")
        return None
 
def convert_pdf_document_mode(client, pdf_path, prompt):
    """
    Convert PDF to text using document mode (PDF sent as document, not images).
    Used as a second pass for verification against visual mode output.
 
    Args:
        client: Anthropic client instance
        pdf_path (str): Path to PDF file
        prompt (str): Conversion prompt
 
    Returns:
        str: Converted text or None if failed
    """
    try:
        logging.info(f"🔄 Document mode pass: {pdf_path}")
 
        with open(pdf_path, "rb") as f:
            pdf_data = base64.b64encode(f.read()).decode('utf-8')
 
        messages = [
            {
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
                        "text": prompt
                    }
                ],
            }
        ]
 
        converted_text = ""
        with client.messages.stream(
            model="claude-opus-4-6",
            max_tokens=128000,
            temperature=0,
            messages=messages
        ) as stream:
            for text in stream.text_stream:
                converted_text += text

        logging.info(f"✅ Document mode complete:{len(converted_text)} characters")
        return converted_text
 
    except Exception as e:
        logging.error(f"❌ Error in document mode conversion: {e}")
        return None
 
def reconcile_outputs(client, visual_text, doc_text):
    """
    Compare visual mode and document mode outputs using Claude to identify
    and fix discrepancies in [D> tag placement.
 
    Args:
        client: Anthropic client instance
        visual_text (str): Output from visual (image) mode
        doc_text (str): Output from document mode
 
    Returns:
        str: Reconciled text with corrections applied
    """
    try:
        logging.info("🔍 Reconciling visual and document mode outputs...")
 
        reconcile_prompt = (
            "You are given two versions of the same legislative bill text converted from PDF. "
            "Both use [D> and <D] tags to mark strikethrough (deleted) text.\n\n"
            "VERSION A (from visual/image analysis - generally more accurate for formatting):\n"
            "---\n"
            f"{visual_text}\n"
            "---\n\n"
            "VERSION B (from document text extraction - may catch items visual mode missed):\n"
            "---\n"
            f"{doc_text}\n"
            "---\n\n"
            "TASK: Produce the best possible merged output by following these rules:\n"
            "1. Use VERSION A as the base text since its formatting and structure is more accurate.\n"
            "2. Compare the [D> tag placement between both versions line by line.\n"
            "3. If VERSION B has a [D> tag around text that VERSION A left untagged, "
            "this is likely a miss by the visual mode - ADD the [D> tag from VERSION B.\n"
            "4. If VERSION A has a [D> tag that VERSION B does not, KEEP the VERSION A tag "
            "(visual mode is more reliable for detecting formatting).\n"
            "5. Pay special attention to subsection labels like (a), (b), (c) etc. - "
            "if either version tags them, include the tag in the output.\n"
            "6. Do NOT change any text content - only adjust [D> and <D] tag placement.\n"
            "7. Output ONLY the merged text, nothing else. No explanations or commentary."
        )
 
        messages = [{"role": "user", "content": reconcile_prompt}]
 
        reconciled = ""
        with client.messages.stream(
            model="claude-opus-4-6",
            max_tokens=128000,
            temperature=0,
            messages=messages
        ) as stream:
            for text in stream.text_stream:
                reconciled += text
 
        logging.info(f"✅ Reconciliation complete:{len(reconciled)} characters")
        return reconciled
 
    except Exception as e:
        logging.error(f"❌ Error during reconciliation: {e}")
        return visual_text  # Fall back to visual output on error
 
def save_text_file(text, output_path):
    """
    Save text to file.
   
    Args:
        text (str): Text content to save
        output_path (str): Output file path
   
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        # Ensure output directory exists
        output_dir = os.path.dirname(output_path)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
       
        # Save text file
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(text)
       
        logging.info(f"💾 Saved output to: {output_path}")
        return True
    except Exception as e:
        logging.error(f"❌ Error saving text file: {e}")
        return False
 
def print_usage():
    """Print usage information."""
    print(__doc__)
    print("\nAvailable State Codes:")
    for i, (code, name) in enumerate(AVAILABLE_STATES.items(), 1):
        if i % 4 == 1:
            print()
        print(f"  {code:3} - {name:20}", end="")
    print("\n")
 
def main():
    """Main function."""
    setup_logging()
   
    print("="*70)
    print("PDF to Text Converter - Visual Mode (Claude Opus 4.6)")
    print("="*70)
   
    # Parse command line arguments
    if len(sys.argv) < 2:
        print("\n❌ Error: PDF file path required\n")
        print_usage()
        sys.exit(1)
 
    # Check for --reconcile flag
    args = [a for a in sys.argv[1:] if a != '--reconcile']
    reconcile = '--reconcile' in sys.argv
 
    pdf_path = args[0] if len(args) > 0 else None
    state_code = args[1].upper() if len(args) > 1 else None
    output_path = args[2] if len(args) > 2 else None
   
    # Validate PDF file exists
    if not os.path.exists(pdf_path):
        print(f"\n❌ Error: PDF file not found: {pdf_path}\n")
        sys.exit(1)
   
    # Initialize Anthropic client (needed for content-based detection)
    print("🔧 Initializing Claude API client...")
    client = get_anthropic_client()
    if not client:
        print("❌ Failed to initialize API client")
        sys.exit(1)
   
    # Auto-detect state if not provided
    if not state_code:
        # Use content analysis to detect state
        detected = detect_state_from_content(client, pdf_path)
        if detected:
            state_code = detected
            print(f"✅ Auto-detected state: {state_code} - {AVAILABLE_STATES[state_code]}")
        else:
            print("⚠️  Could not auto-detect state, using default prompt")
 
    # Validate state code if provided
    if state_code and state_code not in AVAILABLE_STATES:
        print(f"\n❌ Error: Invalid state code '{state_code}'")
        print(f"Available codes: {', '.join(AVAILABLE_STATES.keys())}\n")
        sys.exit(1)
   
    # Generate default output path if not provided
    if not output_path:
        pdf_name = Path(pdf_path).stem
        output_path = f"{pdf_name}_converted.txt"
   
    # Display configuration
    print(f"\n📄 Input PDF:    {pdf_path}")
    if state_code:
        print(f"🏛️  State:        {state_code} - {AVAILABLE_STATES[state_code]}")
    else:
        print(f"🏛️  State:        None (using default prompt)")
    print(f"💾 Output:       {output_path}")
    print(f"🔄 Reconcile:   {'Enabled' if reconcile else 'Disabled'}")
    print()
   
    # Load state template and build prompt
    state_config = load_state_template(state_code) if state_code else None
    if state_config:
        print(f"📋 Using state-specific rules for {state_config['state_name']}...")
    else:
        print(f"📋 Using default visual interpretation prompt...")
    prompt = get_conversion_prompt(state_config)
   
    # Pass 1: Visual mode conversion (images)
    print(f"🔄 Visual mode conversion...")
    visual_text = convert_pdf_to_text(client, pdf_path, prompt)
 
    if not visual_text:
        print("❌ Visual mode conversion failed")
        sys.exit(1)
 
    converted_text = visual_text
 
    # Pass 2 & 3: Document mode + Reconciliation (only if --reconcile is enabled)
    if reconcile:
        print(f"🔄 Document mode verification...")
        doc_prompt = get_document_mode_prompt(state_config)
        doc_text = convert_pdf_document_mode(client, pdf_path, doc_prompt)
 
        if doc_text:
            print(f"🔍 Reconciling outputs...")
            converted_text = reconcile_outputs(client, visual_text, doc_text)
        else:
            print("⚠️ Document mode failed, using visual output only")
 
    # Save output
    print(f"💾 Saving output...")
    if save_text_file(converted_text, output_path):
        print(f"\n✅ SUCCESS! Conversion complete")
        print(f"📄 Output saved to: {output_path}")
        print(f"📊 Output size: {len(converted_text):,} characters")
    else:
        print("❌ Failed to save output file")
        sys.exit(1)
   
    print("\n" + "="*70)
 
if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️ Interrupted by user")
        sys.exit(1)
    except Exception as e:
        logging.error(f"Fatal error: {e}", exc_info=True)
        print(f"\n❌ Fatal error: {e}")
        sys.exit(1)