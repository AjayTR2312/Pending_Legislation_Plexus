"""
Simple PDF to Text Converter using Claude API (Visual Mode)
============================================================

Converts PDFs to text using Claude Opus 4.6 with visual interpretation.
Each PDF page is rendered as an image and sent to Claude for visual reading,
which provides more accurate detection of strikethrough formatting.

Uses pypdfium2 (Google PDFium) for PDF-to-image rendering.

Usage:
    python simple_pdf_to_text.py <pdf_file> [state_code] [output_file]

Examples:
    python simple_pdf_to_text.py document.pdf
    python simple_pdf_to_text.py document.pdf FL
    python simple_pdf_to_text.py document.pdf FL output.txt
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
PROMPTS_FILE = os.path.join(PROJECT_ROOT, "conversion_prompts.txt")

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

# Pricing for claude-opus-4-6 (per 1,000 tokens)
MODEL_PRICING = {
    "input_cost_per_1k":  0.005,
    "output_cost_per_1k": 0.025,
}

# Cached prompts from markdown file
_PROMPTS_CACHE = None

def load_prompts_from_markdown():
    """
    Load prompt templates from conversion_prompts.txt file.
    
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
        detect_start = time.time()
        logging.info("🔍 Analyzing PDF content to detect state...")
        
        # Render first page only
        pdf = pdfium.PdfDocument(pdf_path)
        if len(pdf) == 0:
            return None
        
        page = pdf[0]
        bitmap = page.render(scale=100/72)  # 100 DPI is enough to read state/bill text
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
            elapsed = time.time() - detect_start
            logging.info(f"✅ Auto-detected state {response} ({state_name}) from PDF content analysis")
            print(f"⏱️  State detection took: {elapsed:.1f}s")
            return response
        
        elapsed = time.time() - detect_start
        logging.warning(f"⚠️  Could not confidently detect state (response: {response}) — {elapsed:.1f}s")
        print(f"⏱️  State detection took: {elapsed:.1f}s (inconclusive)")
        return None

    except Exception as e:
        elapsed = time.time() - detect_start
        logging.error(f"❌ Content-based state detection failed: {e} — {elapsed:.1f}s")
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
    if precision_guidance:
        logging.info(f"✅ Precision guidance loaded: {len(precision_guidance)} chars")
    else:
        logging.warning("⚠️  Precision guidance NOT found in conversion_prompts.txt")

    if not state_config:
        raw = prompts.get("📋 TEMPLATE 1: Default Visual Mode Prompt (No State Specified)", "")
        if "```" in raw:
            parts = raw.split("```")
            default_prompt = parts[1].strip() if len(parts) > 1 else raw
        else:
            default_prompt = raw
        if default_prompt:
            logging.info(f"✅ Default prompt template loaded: {len(default_prompt)} chars")
        else:
            logging.warning("⚠️  Default prompt template NOT found in conversion_prompts.txt")
        return default_prompt.replace("{PRECISION_GUIDANCE}", precision_guidance)

    state_name = state_config.get("state_name", "")

    # -------------------------------------------------------------------------
    # Section 1 — Task
    # -------------------------------------------------------------------------
    sections = []
    sections.append(
        f"Convert these PDF page images of a {state_name} legislative bill to plain text with markup tags."
    )

    # -------------------------------------------------------------------------
    # Section 2 — What to remove
    # -------------------------------------------------------------------------
    deletions = state_config.get("deletions", "")
    if deletions:
        sections.append(
            "WHAT TO REMOVE FROM OUTPUT:\n"
            f"{deletions}"
        )

    # -------------------------------------------------------------------------
    # Section 3 — Tag format (always first so the model knows the syntax)
    # -------------------------------------------------------------------------
    sections.append(
        "TAG FORMAT:\n"
        "Two tag types only:\n"
        "  Deleted / struck-through text  →  [D>text<D]   ([D> opens,  <D] closes)\n"
        "  Added / underlined text        →  [A>text<A]   ([A> opens,  <A] closes)\n"
        "Never use markdown formatting (**, *, ~~) anywhere in the output.\n"
        "Every opening tag must have a matching closing tag.\n"
        "Content inside tags must be completely plain text — no asterisks, no underscores."
    )

    # -------------------------------------------------------------------------
    # Section 4 — Deleted text rules
    # -------------------------------------------------------------------------
    delete_markup = state_config.get("delete_markup", {})
    delete_rules = []
    if delete_markup.get("strike_out"):
        delete_rules.append("text with strikethrough formatting (a horizontal line drawn through it)")
    if delete_markup.get("brackets"):
        bracket_type = delete_markup.get("bracket_type", "bracketed")
        delete_rules.append(f"{bracket_type} text — include the bracket characters inside the tag: [D>[text]<D]")
    if delete_markup.get("italic"):
        delete_rules.append("italic text that is also bracketed")
    if delete_markup.get("bold"):
        delete_rules.append("bold text that is also bracketed")

    delete_custom_rules = delete_markup.get("custom_rules", [])

    if delete_rules or delete_custom_rules:
        block = "DELETED TEXT — wrap these with [D>...<D]:\n"
        for r in delete_rules:
            block += f"  - {r}\n"
        if delete_custom_rules:
            block += "Additional delete rules:\n"
            for r in delete_custom_rules:
                block += f"  - {r}\n"
        sections.append(block.rstrip())

    # -------------------------------------------------------------------------
    # Section 5 — Added text rules
    # -------------------------------------------------------------------------
    add_markup = state_config.get("add_markup", {})
    add_rules = []
    if add_markup.get("underline"):
        add_rules.append(
            "underlined text — including special characters (commas, periods, semicolons). "
            "If a special character is underlined it MUST be tagged."
        )
    if add_markup.get("bold"):
        add_rules.append("bold text (visibly thicker strokes than surrounding body text)")
    if add_markup.get("italic"):
        add_rules.append("italic text (characters slant right; upright = not italic)")
    if add_markup.get("all_caps"):
        condition = add_markup.get("all_caps_condition", "")
        add_rules.append(f"ALL CAPS text{' (' + condition + ')' if condition else ''}")
    if add_markup.get("brackets"):
        add_rules.append("bracketed text")

    add_custom_rules = add_markup.get("custom_rules", [])

    if add_rules:
        logging.info(f"Add markup rules for {state_name}: {', '.join(add_rules)}")
    if add_custom_rules:
        logging.info(f"Add custom rules for {state_name}: {', '.join(add_custom_rules)}")

    if add_rules or add_custom_rules:
        block = "ADDED TEXT — wrap these with [A>...<A]:\n"
        for r in add_rules:
            block += f"  - {r}\n"

        # Negative instructions for disabled formats
        no_tag_types = []
        if not add_markup.get("bold"):
            no_tag_types.append("bold")
        if not add_markup.get("underline"):
            no_tag_types.append("underlined")
        if not add_markup.get("italic"):
            no_tag_types.append("italic")
        if not add_markup.get("all_caps"):
            no_tag_types.append("ALL CAPS")
        if no_tag_types:
            block += f"Do NOT tag {', '.join(no_tag_types)} text — output as plain text with no markers.\n"

        if not add_markup.get("italic"):
            block += (
                "Italic text in the PDF = completely plain text in the output. "
                "No asterisks (*text*), no underscores, no markers of any kind.\n"
            )

        # Visual detection hints for enabled formats
        if add_markup.get("underline"):
            block += (
                "Underline detection: look for a visible line BENEATH the text baseline (not through it). "
                "Bold thick strokes are not underline. ALL CAPS without a line beneath is not underline. "
                "Underline bleed: a word's underline often extends a few pixels past its last character — "
                "only tag punctuation if it has a complete, unambiguous underline clearly separate from the "
                "preceding word's underline tail. When in doubt, do NOT tag it.\n"
            )

        if add_custom_rules:
            block += "Additional rules:\n"
            for r in add_custom_rules:
                block += f"  - {r}\n"

        sections.append(block.rstrip())

    # -------------------------------------------------------------------------
    # Section 6 — Visual-only tagging rule
    # -------------------------------------------------------------------------
    sections.append(
        "VISUAL ONLY — tagging is based solely on what you see in the PDF image:\n"
        "  - Compare each word's formatting to the surrounding body text on the same page.\n"
        "  - Do not use document position or legislative conventions to infer formatting.\n"
        "  - Check every word individually, including short words like 'a', 'or', 'the'.\n"
        "  - Subsection labels like (a), (b), (c) may differ in formatting from their body text — check each separately."
    )

    # -------------------------------------------------------------------------
    # Section 7 — General transcription rules (from precision_guidance)
    # -------------------------------------------------------------------------
    if precision_guidance:
        sections.append(precision_guidance)

    # -------------------------------------------------------------------------
    # Section 8 — Output format
    # -------------------------------------------------------------------------
    sections.append(
        "OUTPUT:\n"
        "  - Use regular quotes, not smart quotes.\n"
        "  - Output only the converted text with markup tags — no explanations, no commentary.\n"
        "  - Combine all pages into one continuous document."
    )

    prompt = "\n\n".join(sections)

    logging.info(f"Built prompt for {state_name}: {len(prompt)} chars")
    logging.info(f"\n{'='*70}")
    logging.info(f"FULL PROMPT BEING SENT TO CLAUDE:")
    logging.info(f"{'='*70}")
    logging.info(prompt)
    logging.info(f"{'='*70}\n")
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

        # Create temp folder for images
        script_dir = os.path.dirname(os.path.abspath(__file__))
        temp_dir = os.path.join(script_dir, "temp_images")
        os.makedirs(temp_dir, exist_ok=True)
        
        # Clear old images from previous runs
        old_files = [f for f in os.listdir(temp_dir) if f.endswith('.png')]
        if old_files:
            logging.info(f"🗑️  Clearing {len(old_files)} old images from temp folder")
            for old_file in old_files:
                try:
                    os.remove(os.path.join(temp_dir, old_file))
                except Exception:
                    pass  # Ignore errors if file is locked or doesn't exist
        
        # Get PDF base name for temp file naming
        pdf_basename = os.path.splitext(os.path.basename(pdf_path))[0]

        images = []
        scale = dpi / 72  # pypdfium2 default is 72 DPI

        for i in range(num_pages):
            page = pdf[i]
            bitmap = page.render(scale=scale)
            pil_image = bitmap.to_pil()

            # Save image to temp folder for review
            temp_image_path = os.path.join(temp_dir, f"{pdf_basename}_page_{i+1:03d}.png")
            pil_image.save(temp_image_path, format="PNG", optimize=True)

            # Convert to PNG bytes for API
            buf = io.BytesIO()
            pil_image.save(buf, format="PNG", optimize=True)
            png_bytes = buf.getvalue()
            b64_data = base64.b64encode(png_bytes).decode('utf-8')

            images.append(b64_data)
            logging.info(f"  Page {i+1}/{num_pages}: {len(png_bytes)/1024:.0f} KB → {temp_image_path}")

        pdf.close()
        logging.info(f"📄 All {num_pages} pages rendered to images")
        logging.info(f"🖼️  Temp images saved to: {temp_dir}")
        return images

    except Exception as e:
        logging.error(f"❌ Error converting PDF pages to images: {e}")
        return None

def fix_page_break_tag_splits(text):
    """
    Fix deletion/addition tags that were split at page-batch boundaries.

    With batch_size=1, each page is its own API call. When a bracketed
    deletion spans a page break, the model splits it into two tags:
        [D>[partial text<D]\n\ncontinuation[D>]<D]
    This merges them back into one:
        [D>[partial text continuation]<D]

    Also handles plain (non-bracket) [D> and [A> continuations where
    a short word or phrase at the top of the next page belongs to the
    same formatting span.
    """
    import re

    # CT-style bracket deletions: [D>[text (no ] inside)<D]\n\nword(s)[D>]<D]
    # The first tag has '[' but no ']' inside; the second tag has ']' but no '['
    ct_bracket = re.compile(r'\[D>\[([^\]<]+)<D\]\n\n([^\[<\n]+)\[D\]<D\]')
    text = ct_bracket.sub(r'[D>[\1 \2]<D]', text)

    # General plain split: <X]\n\nword(s)[X>rest<X] where middle is ≤ 4 words
    # (short middle = continuation; long middle = new unrelated tag, leave alone)
    for tag in ['D', 'A']:
        ot = re.escape(f'[{tag}>')
        ct = re.escape(f'<{tag}]')
        pattern = re.compile(ct + r'\n\n([^\[<\n]{1,40})' + ot)
        def _merge(m, _ct=f'<{tag}]', _ot=f'[{tag}>'):
            middle = m.group(1)
            # Only merge if the middle text is short (≤ 4 words) — clear continuation
            if len(middle.split()) <= 4:
                return ' ' + middle
            return m.group(0)
        text = pattern.sub(_merge, text)

    return text


def convert_pdf_to_text(client, pdf_path, prompt, batch_size=1):
    """
    Convert PDF to text using Claude API with visual interpretation.
    Pages are processed in small batches so each call focuses on a narrow
    window — preventing Claude from silently dropping content when
    processing many pages and rules at once.

    Args:
        client: Anthropic client instance
        pdf_path (str): Path to PDF file
        prompt (str): Conversion prompt
        batch_size (int): Number of pages per API call (default 3)

    Returns:
        str: Converted text or None if failed
    """
    try:
        start_time = time.time()
        logging.info(f"🔄 Converting PDF (visual mode, batch_size={batch_size}): {pdf_path}")

        # Render PDF pages to images
        page_images = pdf_pages_to_images(pdf_path)
        if not page_images:
            return None

        total_pages = len(page_images)
        num_batches = (total_pages + batch_size - 1) // batch_size
        all_text_parts = []
        total_input_tokens  = 0
        total_output_tokens = 0

        for batch_idx in range(num_batches):
            start = batch_idx * batch_size
            end = min(start + batch_size, total_pages)
            batch_images = page_images[start:end]

            # Build content array for this batch
            content = []
            for img_b64 in batch_images:
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": img_b64,
                    },
                })

            # Add batch-specific prompt
            batch_prompt = prompt
            if num_batches > 1:
                batch_prompt += (
                    f" This is pages {start+1}-{end} of {total_pages}. "
                    "Convert only these pages. Do not add any preamble or summary."
                )

            content.append({
                "type": "text",
                "text": batch_prompt
            })

            messages = [{"role": "user", "content": content}]

            logging.info(f"🤖 Batch {batch_idx+1}/{num_batches}: pages {start+1}-{end} ({len(batch_images)} images)...")

            # Retry loop for transient overload/server errors
            max_retries = 5
            retry_delays = [10, 30, 60, 120, 180]  # seconds between retries
            batch_text = ""
            for attempt in range(max_retries):
                try:
                    batch_text = ""
                    with client.messages.stream(
                        model="claude-opus-4-6",
                        max_tokens=32768,
                        temperature=0,
                        messages=messages
                    ) as stream:
                        for text in stream.text_stream:
                            batch_text += text
                        final_message = stream.get_final_message()

                    # Accumulate token usage
                    in_tok  = final_message.usage.input_tokens
                    out_tok = final_message.usage.output_tokens
                    total_input_tokens  += in_tok
                    total_output_tokens += out_tok
                    b_in_cost  = (in_tok  / 1000) * MODEL_PRICING["input_cost_per_1k"]
                    b_out_cost = (out_tok / 1000) * MODEL_PRICING["output_cost_per_1k"]
                    logging.info(
                        f"  Batch {batch_idx+1} tokens — "
                        f"input: {in_tok:,}  output: {out_tok:,}  "
                        f"cost: ${b_in_cost + b_out_cost:.4f}"
                    )
                    break  # success — exit retry loop
                except Exception as e:
                    err_str = str(e)
                    is_overload = "overloaded" in err_str.lower() or "529" in err_str or "500" in err_str or "503" in err_str
                    if is_overload and attempt < max_retries - 1:
                        delay = retry_delays[attempt]
                        logging.warning(f"  ⚠️  API overloaded (attempt {attempt+1}/{max_retries}). Retrying in {delay}s...")
                        print(f"  ⚠️  API overloaded — retrying in {delay}s (attempt {attempt+1}/{max_retries})...")
                        time.sleep(delay)
                    else:
                        raise  # non-retryable or out of retries

            logging.info(f"  Batch {batch_idx+1} complete: {len(batch_text)} characters")
            all_text_parts.append(batch_text)

        converted_text = "\n\n".join(all_text_parts)

        # Merge any tag spans that were split at page-batch boundaries
        converted_text = fix_page_break_tag_splits(converted_text)

        # Calculate and report total cost
        total_in_cost  = (total_input_tokens  / 1000) * MODEL_PRICING["input_cost_per_1k"]
        total_out_cost = (total_output_tokens / 1000) * MODEL_PRICING["output_cost_per_1k"]
        total_cost     = total_in_cost + total_out_cost
        cost_summary = (
            f"💰 Cost — input: {total_input_tokens:,} tokens (${total_in_cost:.4f})  "
            f"output: {total_output_tokens:,} tokens (${total_out_cost:.4f})  "
            f"TOTAL: ${total_cost:.4f}"
        )
        logging.info(cost_summary)
        print(cost_summary)

        # Log total runtime
        total_time = time.time() - start_time
        minutes = int(total_time // 60)
        seconds = int(total_time % 60)
        logging.info(f"✅ Conversion complete: {len(converted_text)} characters ({num_batches} batches)")
        logging.info(f"⏱️  Total runtime: {minutes}m {seconds}s ({total_time:.1f} seconds)")
        return converted_text

    except Exception as e:
        logging.error(f"❌ Error converting PDF: {e}")
        return None

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
    global _PROMPTS_CACHE
    _PROMPTS_CACHE = None  # Clear prompt cache on each run
    setup_logging()
    
    print("="*70)
    print("PDF to Text Converter - Visual Mode (Claude Opus 4.6)")
    print("="*70)
    
    # Parse command line arguments
    if len(sys.argv) < 2:
        print("\n❌ Error: PDF file path required\n")
        print_usage()
        sys.exit(1)

    args = sys.argv[1:]

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
        # 1. Try to infer from the parent directory name first (free, instant)
        parent_dir = os.path.basename(os.path.dirname(os.path.abspath(pdf_path))).upper()
        if parent_dir in AVAILABLE_STATES:
            state_code = parent_dir
            print(f"✅ State inferred from folder name: {state_code} - {AVAILABLE_STATES[state_code]} (0.0s)")
        else:
            # 2. Fall back to API content analysis only when needed
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
    print()
    
    # Load state template and build prompt
    state_config = load_state_template(state_code) if state_code else None
    if state_config:
        print(f"📋 Using state-specific rules for {state_config['state_name']}...")
        logging.info(f"\n{'='*70}")
        logging.info(f"STATE CONFIG FOR {state_config['state_name']}:")
        logging.info(f"{'='*70}")
        logging.info(f"Add Markup: {state_config.get('add_markup', {})}")
        logging.info(f"Delete Markup: {state_config.get('delete_markup', {})}")
        logging.info(f"Deletions: {state_config.get('deletions', '')}")
        logging.info(f"{'='*70}\n")
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
