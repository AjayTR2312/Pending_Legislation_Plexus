"""
Simple PDF to Text Converter using Claude API (Visual Mode)
============================================================
 
Converts PDFs to text using Claude Opus 4.6 with visual interpretation.
Each PDF page is rendered as an image and sent to Claude for visual reading,
which provides more accurate detection of strikethrough formatting.
 
Uses pypdfium2 (Google PDFium) for PDF-to-image rendering.
 
Usage:
    python simple_pdf_to_text.py <pdf_file> [state_code] [output_file] [--reconcile] [--self-correct]
 
Options:
    --reconcile      Enable 3-pass reconciliation (visual + document + merge).
                     Disabled by default for faster runtime.
    --self-correct   Enable self-correction pass that re-examines images vs output
                     to catch missed/false tags. Adds one extra API call.
    --hybrid         Use PyMuPDF text extraction + LLM text-chunk cleaning.
                     Best for long PDFs (>7 pages). No images needed, much cheaper.
 
Examples:
    python simple_pdf_to_text.py document.pdf
    python simple_pdf_to_text.py document.pdf FL
    python simple_pdf_to_text.py document.pdf FL output.txt
    python simple_pdf_to_text.py document.pdf FL output.txt --reconcile
    python simple_pdf_to_text.py document.pdf FL --reconcile
    python simple_pdf_to_text.py document.pdf FL --self-correct
    python simple_pdf_to_text.py document.pdf FL output.txt --self-correct --reconcile
    python simple_pdf_to_text.py document.pdf FL --hybrid

Modes:
    --hybrid         Use PyMuPDF text extraction + LLM text-chunk cleaning.
                     Best for long PDFs (>7 pages). No images needed, much cheaper.
"""
 
import anthropic
import requests
import json
import ssl
import os
import re
import time
import sys
import base64
import io
import logging
from pathlib import Path
from datetime import datetime

from paragraph_break_detector import strip_table_markers, pdf_unwrap_paragraphs
 
import pypdfium2 as pdfium
from PIL import Image
 
# Fix encoding for Windows console (skip when stdout is already wrapped, e.g. Streamlit)
if sys.platform == 'win32':
    import codecs
    try:
        sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')
        sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'strict')
    except Exception:
        pass
 
# GPT 5.4 intelligent tag comparison
try:
    from gpt_compare import gpt_compare_tags
    _HAS_GPT_COMPARE = True
except ImportError:
    _HAS_GPT_COMPARE = False

# PyMuPDF extractor — lives in app/ directory
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
    from pymupdf_extractor import (
        extract_pdf_json_report, extract_pdf_with_pymupdf,
        highlight_pdf_formatting, clean_extracted_text,
        consolidate_adjacent_tags, _load_state_clean_prompt,
        _tag_brackets, _generate_clean_diff_report,
        _strip_ct_header_tags, _strip_ct_compact_designator,
        _strip_ct_false_underline_headings, _fix_midword_page_break_tags,
        _strip_false_footer_line_underline, _strip_ct_analysis_section_tags,
        _strip_ct_raw_footer_line_tags,
        _strip_false_subsection_d_tags, _restructure_ct_header,
        _strip_in_artifacts, _strip_in_header_tags, _strip_in_leading_indent,
        _split_in_merged_table_rows, _ensure_in_item_spacing,
        _split_in_midline_items, _split_in_section_heading_welds,
        _strip_ks_header_tags, _strip_ks_signature_block_tags,
        _inject_ks_signature_lines,
        _strip_ca_signature_block_tags,
        _strip_fl_repeated_headers,
        pre_merge_paragraph_tags, _clean_tagged_text,
        remove_nested_tags, merge_split_tags, split_tagged_subsections,
        split_renumber_paragraph_breaks, split_numbered_item_paragraph_welds,
        rejoin_amendment_intro_breaks,
        rejoin_inline_renumber_list_breaks,
        _split_merged_grant_tag_rows,
        _split_tabular_tag_at_dollar,
        _split_tabular_header_from_data,
    )
    _HAS_PYMUPDF = True
except ImportError:
    _HAS_PYMUPDF = False

# (compare_tags and Validation.py removed — replaced by gpt_compare.py)

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
 
# ── Token & Cost Tracking ─────────────────────────────────────────────
MODEL_PRICING = {
    "claude-opus-4-6": {"input_cost_per_1k": 0.005, "output_cost_per_1k": 0.025},
    "claude-opus-4-7": {"input_cost_per_1k": 0.005, "output_cost_per_1k": 0.025},
}

_TOKEN_TRACKER = {
    "calls": [],          # list of {step, input_tokens, output_tokens, cost}
    "total_input": 0,
    "total_output": 0,
    "total_cost": 0.0,
}

def _calculate_cost(input_tokens, output_tokens, model="claude-opus-4-6"):
    pricing = MODEL_PRICING.get(model, MODEL_PRICING["claude-opus-4-6"])
    input_cost = (input_tokens / 1000) * pricing["input_cost_per_1k"]
    output_cost = (output_tokens / 1000) * pricing["output_cost_per_1k"]
    return input_cost + output_cost

def _track_usage(step_name, usage, model="claude-opus-4-6"):
    """Record token usage from an API call."""
    inp = getattr(usage, 'input_tokens', 0) if usage else 0
    out = getattr(usage, 'output_tokens', 0) if usage else 0
    cost = _calculate_cost(inp, out, model)
    _TOKEN_TRACKER["calls"].append({
        "step": step_name,
        "input_tokens": inp,
        "output_tokens": out,
        "cost": cost,
    })
    _TOKEN_TRACKER["total_input"] += inp
    _TOKEN_TRACKER["total_output"] += out
    _TOKEN_TRACKER["total_cost"] += cost
    logging.info(f"💰 [{step_name}] Tokens: {inp:,} in / {out:,} out | Cost: ${cost:.4f}")

def reset_token_tracker():
    """Reset the token tracker for a fresh processing run."""
    _TOKEN_TRACKER["calls"].clear()
    _TOKEN_TRACKER["total_input"] = 0
    _TOKEN_TRACKER["total_output"] = 0
    _TOKEN_TRACKER["total_cost"] = 0.0

def get_token_tracker():
    """Return a copy of the current token tracker data."""
    import copy
    return copy.deepcopy(_TOKEN_TRACKER)

def print_token_summary(num_pages=None):
    """Print a formatted summary of all API calls and total cost."""
    t = _TOKEN_TRACKER
    print("\n" + "="*70)
    print("💰 TOKEN USAGE & COST SUMMARY")
    print("="*70)
    print(f"{'Step':<30} {'Input':>10} {'Output':>10} {'Cost':>10}")
    print("-"*62)
    for call in t["calls"]:
        print(f"{call['step']:<30} {call['input_tokens']:>10,} {call['output_tokens']:>10,} ${call['cost']:>9.4f}")
    print("-"*62)
    print(f"{'TOTAL':<30} {t['total_input']:>10,} {t['total_output']:>10,} ${t['total_cost']:>9.4f}")
    print(f"\n📊 Total tokens: {t['total_input'] + t['total_output']:,}")
    print(f"💵 Total cost:   ${t['total_cost']:.4f}")

    # ── Per-page & volume projections ─────────────────────────────
    if num_pages and num_pages > 0:
        cost_per_page = t['total_cost'] / num_pages
        tokens_per_page = (t['total_input'] + t['total_output']) / num_pages
        print(f"\n{'─'*62}")
        print(f"📄 PER-PAGE & VOLUME PROJECTIONS  ({num_pages} pages in this document)")
        print(f"{'─'*62}")
        print(f"   Cost per page:          ${cost_per_page:.4f}")
        print(f"   Tokens per page:        {tokens_per_page:,.0f}")
        print()
        print(f"   {'Volume':<28} {'Pages':>8} {'Est. Cost':>12}")
        print(f"   {'─'*50}")
        for docs, avg_pages, label in [
            (10,   num_pages, "10 docs (same size)"),
            (50,   num_pages, "50 docs (same size)"),
            (100,  num_pages, "100 docs (same size)"),
            (500,  num_pages, "500 docs (same size)"),
            (1000, num_pages, "1,000 docs (same size)"),
            (100,  5,  "100 docs × 5 pg avg"),
            (100,  10, "100 docs × 10 pg avg"),
            (100,  20, "100 docs × 20 pg avg"),
            (500,  5,  "500 docs × 5 pg avg"),
            (500,  10, "500 docs × 10 pg avg"),
            (500,  20, "500 docs × 20 pg avg"),
            (1000, 5,  "1,000 docs × 5 pg avg"),
            (1000, 10, "1,000 docs × 10 pg avg"),
            (1000, 20, "1,000 docs × 20 pg avg"),
        ]:
            total_pages = docs * avg_pages
            est_cost = total_pages * cost_per_page
            print(f"   {label:<28} {total_pages:>8,} ${est_cost:>11.2f}")
        print()
        print(f"   ⚠️  Estimates assume similar page density. Actual cost varies")
        print(f"      with text length, formatting complexity, and image size.")
    print("="*70)

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
            import httpx
            http_client = httpx.Client(verify=False)
            return anthropic.Anthropic(api_key=credentials["anthropic_api_key"], http_client=http_client)
        else:
            logging.error(f"❌ No Anthropic API key found in response")
            return None
    except Exception as e:
        logging.error(f"❌ Failed to initialize Anthropic client: {e}")
        return None
 
def _pymupdf_flags_from_state(state_config):
    """
    Derive PyMuPDF extractor flags from a netscan_state_template config.

    Mapping logic:
      add_markup.underline   → mark_underline   (default True)
      add_markup.italic      → mark_italic      (default False)
      add_markup.bold        → mark_bold         (default False)
      delete_markup.strike_out → mark_strikethrough (default True)
      deletions contains "line number" → strip_line_numbers True

    Returns:
        dict with keys matching extract_pdf_json_report / extract_pdf_with_pymupdf kwargs
    """
    flags = {
        "mark_underline": True,
        "mark_strikethrough": True,
        "mark_italic": False,
        "mark_bold": False,
        "mark_brackets": False,
        "strip_line_numbers": False,
    }
    if not state_config:
        return flags

    add = state_config.get("add_markup", {})
    delete = state_config.get("delete_markup", {})

    # Additions: only mark what the template says
    flags["mark_underline"] = bool(add.get("underline", False))
    flags["mark_italic"]    = bool(add.get("italic", False))
    flags["mark_bold"]      = bool(add.get("bold", False))

    # Deletions
    flags["mark_strikethrough"] = bool(delete.get("strike_out", True))
    flags["mark_brackets"]     = bool(delete.get("brackets", False))

    # Line-number stripping: if the template says to remove line numbers
    deletions_text = (state_config.get("deletions", "") or "").lower()
    if "line number" in deletions_text:
        flags["strip_line_numbers"] = True

    return flags


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
        _track_usage("State Detection", message.usage)
 
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
                "For bold: bold text has visually thicker/heavier strokes compared to regular (non-bold) text. "
                "IMPORTANT: In some bills, the MAJORITY of the statutory body text is bold — this is intentional "
                "and means that text IS newly added legislative language. You MUST tag ALL bold text as [A>...<A] "
                "even if most or all of the body text appears bold. Do NOT skip bold text just because it "
                "'looks the same as surrounding text' — if the strokes are thick/heavy, it IS bold and MUST be tagged."
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
            "If it looks the same as the body text, do NOT tag it — EXCEPT for formatting types "
            "explicitly listed above (e.g., if bold is listed, tag ALL bold text even if the majority of the page is bold). "
            f"{formatting_guidance} "
        )
    
    state_name = state_config.get("state_name", "")
    abbreviation = state_config.get("abbreviation", "")

    # Build header order instruction — AR only
    header_order = state_config.get("header_order", "")
    header_order_instruction = f"HEADER ORDER: {header_order} " if (header_order and abbreviation == "AR") else ""

    prompt = (
        f"Convert these PDF page images of a {state_name} legislative bill to text. "
        f"{header_order_instruction}"
        f"{delete_instruction} "
        f"{delete_markup_instruction}"
        f"{add_markup_instruction}"
        "Use regular quotes, not smart quotes. "
        f"{precision_guidance} "
        "CRITICAL ANTI-PATTERN — NO SEMANTIC PAIRING: "
        "When deleted words and added words appear next to each other, "
        "output them in the EXACT visual reading order from the PDF page. "
        "Do NOT rearrange words to pair each deletion with its semantic replacement. "
        "WRONG: [D>old-adj<D] [A>NEW-ADJ<A] [D>old-noun<D] [A>NEW-NOUN<A] "
        "(reorders to pair each deletion with its replacement). "
        "CORRECT: [D>old-adj<D] [D>old-noun<D] [A>NEW-ADJ<A] [A>NEW-NOUN<A] "
        "(preserves the visual left-to-right order from the page). "
        "Output only the converted text with markup tags, nothing else. "
        "IMPORTANT: Extract ALL text from EVERY page, including cover letters, "
        "transmittal letters, certification pages, and signature blocks. "
        "Do NOT skip or omit any pages — the cleaning step will handle removals. "
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
            "STRIKETHROUGH PRIORITY RULE — CRITICAL: If text has strikethrough AND any other formatting "
            "(bold, italic, underline, or any combination), strikethrough ALWAYS wins — tag it as [D>...<D] only. "
            "Do NOT tag such text as [A>]. Deletion overrides addition without exception. "
            "CRITICAL TAG FORMAT REQUIREMENTS: "
            "1. You MUST use [A> and <A] tags exactly as specified. Example: [A>new text<A] "
            "2. Do NOT use asterisks (*), do NOT use Markdown formatting, do NOT use any other markup style. "
            "3. NEVER write *text* or **text** - this is WRONG. Always write [A>text<A] instead. "
            "STRICT DETECTION: Only tag the EXACT formatting types listed above. "
            "Only tag text that is VISUALLY rendered in the SPECIFIC formatting types listed. "
            f"{visual_guidance} "
            "Do NOT infer or guess formatting. Do NOT tag formatting types not explicitly listed. "
        )
 
    # Build header order instruction — AR only

    header_order = state_config.get("header_order", "")
    header_order_instruction = f"HEADER ORDER: {header_order} " if (header_order and state_config.get("abbreviation", "") == "AR") else ""
    prompt = (
        f"Convert this {state_name} legislative bill PDF to text. "
        f"{header_order_instruction}"
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
 
def pdf_pages_to_images(pdf_path, dpi=400, trim_borders=True, border_percent=5, adaptive_dpi=True):
    """
    Convert each PDF page to a base64-encoded PNG image using pypdfium2.
    
    With adaptive_dpi=True, pages whose default render is too small
    (indicating small text or low-density content) are automatically
    re-rendered at a higher DPI for better formatting detection.

    Args:
        pdf_path (str): Path to PDF file
        dpi (int): Base resolution for rendering (default 400 DPI)
        trim_borders (bool): Whether to trim page borders
        border_percent (int): Border trim percentage
        adaptive_dpi (bool): Auto-increase DPI for small/dense pages (default True)

    Returns:
        list: List of base64-encoded PNG strings, or None if failed
    """
    # Adaptive thresholds
    MIN_WIDTH_PX = 2000    # If page renders narrower than this, bump DPI
    MAX_DPI = 600          # Safety cap to avoid huge images
    
    try:
        pdf = pdfium.PdfDocument(pdf_path)
        num_pages = len(pdf)
        logging.info(f"📄 PDF loaded: {num_pages} pages, base DPI={dpi}, adaptive={adaptive_dpi}")

        images = []

        for i in range(num_pages):
            page = pdf[i]
            page_dpi = dpi
            scale = page_dpi / 72  # pypdfium2 default is 72 DPI
            bitmap = page.render(scale=scale)
            pil_image = bitmap.to_pil()
            
            # Adaptive DPI: if rendered image is too small, re-render at higher DPI
            if adaptive_dpi and pil_image.width < MIN_WIDTH_PX and page_dpi < MAX_DPI:
                page_dpi = min(MAX_DPI, int(page_dpi * MIN_WIDTH_PX / pil_image.width) + 50)
                scale = page_dpi / 72
                bitmap = page.render(scale=scale)
                pil_image = bitmap.to_pil()
                logging.info(f"  Page {i+1}: adaptive DPI bumped to {page_dpi} (width was {pil_image.width}px)")

            # Convert to PNG bytes entirely in-memory — no disk writes needed
            buf = io.BytesIO()
            pil_image.save(buf, format="PNG", optimize=True)
            png_bytes = buf.getvalue()
            b64_data = base64.b64encode(png_bytes).decode('utf-8')

            images.append(b64_data)
            logging.info(f"  Page {i+1}/{num_pages}: {len(png_bytes)/1024:.0f} KB @ {page_dpi} DPI ({pil_image.width}x{pil_image.height})")

        pdf.close()
        logging.info(f"📄 All {num_pages} pages rendered to images (in-memory, no temp files)")
        return images
 
    except Exception as e:
        logging.error(f"❌ Error converting PDF pages to images: {e}")
        return None
 
def convert_pdf_to_text(client, pdf_path, prompt, batch_size=None, dpi=400):
    """
    Convert PDF to text using Claude API with visual interpretation.
    Processes all pages in a single API call for maximum consistency.
 
    Args:
        client: Anthropic client instance
        pdf_path (str): Path to PDF file
        prompt (str): Conversion prompt
        batch_size (int): Ignored - kept for backward compatibility
        dpi (int): Resolution for rendering (default 400, use 600 for bold detection)
 
    Returns:
        tuple: (converted_text, page_images) where page_images is list of
               base64-encoded PNGs, or (None, None) if failed
    """
    try:
        start_time = time.time()
        logging.info(f"🔄 Converting PDF (visual mode, ALL PAGES IN ONE CALL): {pdf_path}")
 
        # Render PDF pages to images
        page_images = pdf_pages_to_images(pdf_path, dpi=dpi)
        if not page_images:
            return None, None
 
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

        # Log full prompt for debugging
        logging.info(f"📝 FULL CONVERSION PROMPT ({len(prompt)} chars):\n{prompt}")
 
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
                    final_msg = stream.get_final_message()
                    _track_usage("Visual Conversion", final_msg.usage)
                break  # success — exit retry loop
            except Exception as e:
                err_str = str(e).lower()
                is_retryable = (
                    "overloaded" in err_str
                    or "timed out" in err_str
                    or "timeout" in err_str
                    or "connection" in err_str
                    or "disconnected" in err_str
                    or "529" in err_str
                    or "500" in err_str
                    or "502" in err_str
                    or "503" in err_str
                    or "504" in err_str
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
        return converted_text, page_images
 
    except Exception as e:
        logging.error(f"❌ Error converting PDF: {e}")
        return None, None
 
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
            final_msg = stream.get_final_message()
            _track_usage("Document Mode", final_msg.usage)

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
            final_msg = stream.get_final_message()
            _track_usage("Reconciliation", final_msg.usage)
 
        logging.info(f"✅ Reconciliation complete:{len(reconciled)} characters")
        return reconciled
 
    except Exception as e:
        logging.error(f"❌ Error during reconciliation: {e}")
        return visual_text  # Fall back to visual output on error


def self_correction_pass(client, converted_text, page_images, state_config=None):
    """
    Lightweight self-correction pass: send the original page images PLUS
    the first-pass output back to Claude and ask it to fix ONLY tagging
    errors it can spot — without re-transcribing the entire document.

    This catches the most common intermittent errors:
      - Missed tags on formatted text
      - False tags on unformatted text
      - Markdown asterisks that slipped through
      - Dropped words near tag boundaries

    Args:
        client: Anthropic client instance
        converted_text (str): First-pass converted text
        page_images (list): Base64-encoded PNG page images
        state_config (dict): State config, or None

    Returns:
        str: Corrected text (or original if self-correction fails)
    """
    try:
        logging.info("🔍 Running self-correction pass...")
        
        # Build state-aware correction hints
        state_hints = ""
        if state_config:
            add_markup = state_config.get("add_markup", {})
            del_markup = state_config.get("delete_markup", {})
            add_types = []
            if add_markup.get("underline"): add_types.append("underlined")
            if add_markup.get("bold"): add_types.append("bold")
            if add_markup.get("italic"): add_types.append("italic")
            if add_markup.get("all_caps"): add_types.append("ALL CAPS")
            del_types = []
            if del_markup.get("strike_out"): del_types.append("strikethrough")
            if del_markup.get("brackets"): del_types.append("bracketed")
            
            state_hints = (
                f"State: {state_config.get('state_name', '')}. "
                f"[A>] tags should mark: {', '.join(add_types) if add_types else 'underlined'} text. "
                f"[D>] tags should mark: {', '.join(del_types) if del_types else 'strikethrough'} text. "
            )

        correction_prompt = (
            "You are reviewing a PREVIOUS conversion of these PDF page images. "
            "The conversion is shown below. Your job is to FIX any errors — "
            "do NOT re-transcribe from scratch.\n\n"
            f"{state_hints}\n"
            "REVIEW CHECKLIST — scan each page image and check the output for:\n"
            "1. MISSED TAGS: text that is clearly formatted (strikethrough, underline, bold, italic, ALL CAPS) "
            "in the PDF but appears as plain text in the output → add the correct [A> or [D> tag\n"
            "   PRIORITY RULE: If text has BOTH strikethrough AND other formatting (bold/italic/underline), "
            "strikethrough ALWAYS wins → tag as [D>...<D] only, NEVER as [A>...<A].\n"
            "2. FALSE TAGS: text tagged with [A> or [D> but NOT formatted in the PDF → remove the tag\n"
            "3. MARKDOWN: any *text* or **text** → replace with [A>text<A] or remove asterisks\n"
            "4. DROPPED WORDS: words visible in the PDF that are missing from the output → add them back\n"
            "5. TAG BOUNDARIES: tags that start too early/late or end too early/late → adjust\n"
            "6. PRESERVE everything that IS correct — only change what's wrong\n\n"
            "RULES:\n"
            "- Output the COMPLETE corrected text (not a diff or list of changes)\n"
            "- Do NOT change text content, capitalization, or word order\n"
            "- Do NOT add explanations — output ONLY the corrected conversion\n"
            "- Use [A>...<A] and [D>...<D] tags exclusively (no markdown)\n"
            "- Use regular quotes, not smart quotes\n\n"
            "PREVIOUS CONVERSION TO REVIEW AND FIX:\n"
            "───────────────────────────────────────\n"
            f"{converted_text}\n"
            "───────────────────────────────────────\n\n"
            "Output the corrected version now."
        )

        # Build content: images first, then the correction prompt
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
        content.append({"type": "text", "text": correction_prompt})

        corrected = ""
        with client.messages.stream(
            model="claude-opus-4-6",
            max_tokens=128000,
            temperature=0,
            messages=[{"role": "user", "content": content}]
        ) as stream:
            for text in stream.text_stream:
                corrected += text
            final_msg = stream.get_final_message()
            _track_usage("Self-Correction", final_msg.usage)

        if corrected and len(corrected) > len(converted_text) * 0.5:
            # Sanity check: corrected output should be roughly similar in length
            len_diff = abs(len(corrected) - len(converted_text)) / len(converted_text)
            if len_diff > 0.3:
                logging.warning(f"⚠️  Self-correction changed length by {len_diff:.0%}, using original")
                return converted_text
            logging.info(f"✅ Self-correction complete: {len(corrected)} chars (was {len(converted_text)})")
            return corrected
        else:
            logging.warning("⚠️  Self-correction returned empty/short result, using original")
            return converted_text

    except Exception as e:
        logging.error(f"⚠️  Self-correction pass failed: {e}")
        return converted_text  # Fall back to original


def _split_text_into_chunks(text, max_chars=40000, overlap_chars=2000):
    """
    Split extracted text into chunks at section/paragraph boundaries.
    
    Prefers splitting at:
      1. Section headers (SECTION N, Sec. N, § N)
      2. Double newlines (paragraph breaks)
      3. Single newlines (line breaks)
    
    Each chunk includes `overlap_chars` characters from the end of the
    previous chunk so the LLM has boundary context for clean merging.

    Args:
        text (str): Full extracted text
        max_chars (int): Target max characters per chunk (~10K tokens)
        overlap_chars (int): Characters of overlap between chunks

    Returns:
        list[str]: Text chunks with overlap
    """
    if len(text) <= max_chars:
        return [text]

    # Regex for section-level split points
    section_re = re.compile(
        r'\n(?=\s*(?:SECTION|Section|Sec\.|SEC\.|ARTICLE|Article|§)\s*\d)',
    )

    chunks = []
    pos = 0
    while pos < len(text):
        end = min(pos + max_chars, len(text))
        if end >= len(text):
            chunks.append(text[pos:])
            break

        # Find the best split point working backward from `end`
        window = text[pos + max_chars - 5000 : end]  # last 5K chars
        split_at = None

        # Try section boundary first
        for m in section_re.finditer(window):
            split_at = pos + max_chars - 5000 + m.start()
        # Fall back to double newline
        if split_at is None:
            dn = window.rfind('\n\n')
            if dn != -1:
                split_at = pos + max_chars - 5000 + dn
        # Fall back to single newline
        if split_at is None:
            sn = window.rfind('\n')
            if sn != -1:
                split_at = pos + max_chars - 5000 + sn
        # Last resort: hard cut
        if split_at is None or split_at <= pos:
            split_at = end

        chunks.append(text[pos:split_at])
        # Next chunk starts `overlap_chars` before the split point
        pos = max(split_at - overlap_chars, pos + 1)

    logging.info(f"📐 Split text ({len(text):,} chars) into {len(chunks)} chunks "
                 f"(max {max_chars:,}, overlap {overlap_chars:,})")
    return chunks


def convert_pdf_hybrid_text(client, pdf_path, state_config=None, state_code=None, raw_text=None):
    """
    Hybrid conversion: PyMuPDF text extraction + LLM cleaning in chunks.
    
    This approach works well for long PDFs because:
    - PyMuPDF extracts ALL text with formatting tags ([A>…<A], [D>…<D])
    - Text is split into manageable chunks at section boundaries
    - Each chunk is cleaned by Claude (remove headers/footers/line numbers)
    - Chunks are stitched together with overlap deduplication
    - Much cheaper than image-based (text tokens << image tokens)
    - No chunk-merge accuracy issues since we split at natural boundaries
    
    Args:
        client: Anthropic client instance
        pdf_path (str): Path to PDF file
        state_config (dict): State config from template, or None
        state_code (str): Two-letter state code, or None
        raw_text (str): Pre-extracted PyMuPDF text.  When provided, skips
            the internal extraction step (avoids duplicate processing).
    
    Returns:
        str: Converted and cleaned text, or None if failed
    """
    if not _HAS_PYMUPDF:
        logging.error("❌ PyMuPDF extractor not available for hybrid mode")
        return None

    # Reset per-run reports so callers don't read stale data from a prior run
    convert_pdf_hybrid_text._last_diff_report = None
    convert_pdf_hybrid_text._last_chunks_report = None

    try:
        start_time = time.time()
        logging.info(f"🔄 HYBRID MODE: PyMuPDF extract + LLM text-chunk cleaning: {pdf_path}")

        # ── Step 1: PyMuPDF extraction with formatting tags ──────────
        flags = _pymupdf_flags_from_state(state_config)
        active = [k.replace("mark_", "") for k, v in flags.items() if v and k.startswith("mark_")]
        logging.info(f"   Detecting formats: {', '.join(active) if active else 'none'}")

        if raw_text:
            logging.info(f"   ⏩ Using pre-extracted PyMuPDF text ({len(raw_text):,} chars) — skipping re-extraction")
        else:
            raw_text = extract_pdf_with_pymupdf(pdf_path, join_pages=True, state_code=state_code, **flags)
        if not raw_text or len(raw_text.strip()) < 50:
            logging.error("❌ PyMuPDF extraction returned empty/minimal text")
            return None

        logging.info(f"   ✅ PyMuPDF extracted {len(raw_text):,} characters")

        # Save raw PyMuPDF output for reference
        raw_path = str(Path(pdf_path).with_suffix('')) + "_hybrid_raw.txt"
        with open(raw_path, "w", encoding="utf-8") as f:
            f.write(raw_text)
        logging.info(f"   💾 Raw PyMuPDF saved to: {raw_path}")

        # CT-specific raw text pre-processing (before LLM)
        # NOTE: _strip_ct_raw_footer_line_tags disabled — analysis showed ALL
        # cases it stripped were legitimate underlined text (preceding lines
        # have [A>] tags or tag starts mid-word).  Footer-line false positives
        # are handled post-LLM by _strip_false_footer_line_underline instead.
        # if state_code and state_code.upper() == "CT":
        #     raw_text = _strip_ct_raw_footer_line_tags(raw_text)

        # ── Pre-merge per-line tags into paragraph-level tags (FL only) ─
        # Uses the PDF’s own \n vs \n\n signals as ground truth:
        #   \n  = same paragraph (wrapped continuation) → merge tags
        #   \n\n = paragraph boundary → keep separate
        # This gives the LLM correct paragraph-level tags from the start,
        # eliminating most post-LLM merge/split heuristics and their
        # associated edge cases.  FL-only – other states work fine as-is.
        if state_code and state_code.upper() == "FL":
            raw_text = pre_merge_paragraph_tags(raw_text)
            logging.info(f"   ✅ Pre-merged FL to paragraph-level tags: {len(raw_text):,} chars")

        # ── Step 1b: Pre-cleaning (line number stripping) ────────────
        # Same deterministic pre-cleaning that clean_extracted_text() does:
        # strip trailing, leading, and bare-block line numbers before LLM.
        raw_text = _clean_tagged_text(raw_text, strip_line_numbers=True)
        try:
            from text_accuracy_helpers import (
                strip_trailing_line_numbers,
                strip_leading_line_numbers,
                strip_bare_line_number_blocks,
            )
            _before = len(raw_text)
            raw_text = strip_trailing_line_numbers(raw_text, state_code=state_code)
            raw_text = strip_leading_line_numbers(raw_text, state_code=state_code)
            raw_text = strip_bare_line_number_blocks(raw_text, state_code=state_code)
            _after = len(raw_text)
            if _after < _before:
                logging.info(
                    f"   🧹 Pre-clean stripped line numbers: {_before:,} → {_after:,} chars"
                )
        except ImportError:
            pass

        # ── Step 1c: PDF-based paragraph unwrapping — DISABLED ──────
        # Previously joined continuation lines using PDF layout boundaries
        # and added TABLE markers.  Removed because it was causing false
        # positives and the LLM handles paragraph structure correctly
        # without pre-unwrapping.

        # Save pre-cleaned text for accuracy comparison
        _llm_text = strip_table_markers(raw_text)
        pre_cleaned_path = str(Path(pdf_path).with_suffix('')) + "_hybrid_precleaned.txt"
        with open(pre_cleaned_path, "w", encoding="utf-8") as f:
            f.write(_llm_text)
        logging.info(f"   💾 Pre-cleaned text saved to: {pre_cleaned_path}")

        # ── Step 2: Split into chunks ────────────────────────────────
        # _llm_text already has TABLE markers stripped
        chunks = _split_text_into_chunks(_llm_text, max_chars=40000, overlap_chars=2000)
        total_chunks = len(chunks)
        logging.info(f"   📐 Split into {total_chunks} chunk(s)")

        # ── Step 3: Clean each chunk with LLM ────────────────────────
        system_prompt = _load_state_clean_prompt(state_code)

        cleaned_chunks = []
        max_retries = 5

        for i, chunk in enumerate(chunks, 1):
            logging.info(f"   🧹 Cleaning chunk {i}/{total_chunks} ({len(chunk):,} chars)...")
            print(f"   🧹 Cleaning chunk {i}/{total_chunks} ({len(chunk):,} chars)...")

            # Add chunk context to help LLM understand position
            chunk_header = ""
            if total_chunks > 1:
                chunk_header = (
                    f"[This is chunk {i} of {total_chunks} of a legislative document. "
                )
                if i == 1:
                    chunk_header += "This is the BEGINNING of the document. Preserve the bill header.]\n\n"
                elif i == total_chunks:
                    chunk_header += "This is the END of the document.]\n\n"
                else:
                    chunk_header += "This is a MIDDLE section. Do NOT add any header — just clean and return the text.]\n\n"

            user_message = chunk_header + chunk

            for attempt in range(1, max_retries + 1):
                try:
                    cleaned = ""
                    final_msg = None
                    with client.messages.stream(
                        model="claude-opus-4-6",
                        max_tokens=64000,
                        timeout=600.0,  # 10-minute timeout for large chunks
                        system=system_prompt,
                        messages=[{"role": "user", "content": user_message}],
                    ) as stream:
                        for text in stream.text_stream:
                            cleaned += text
                        final_msg = stream.get_final_message()
                        _track_usage(f"Hybrid Chunk {i}/{total_chunks}", final_msg.usage)
                    # ── Validate stream completion ──────────────────────
                    # ``stop_reason`` tells us WHY the model stopped.  We
                    # treat anything other than ``end_turn`` as a hard
                    # error so silent ``max_tokens`` truncation cannot
                    # slip through and drop content from the output.
                    stop_reason = getattr(final_msg, "stop_reason", None) if final_msg else None
                    if stop_reason and stop_reason != "end_turn":
                        msg = (
                            f"Hybrid Chunk {i}/{total_chunks}: stream ended with "
                            f"stop_reason={stop_reason!r} (expected 'end_turn'). "
                            f"Output may be truncated — got {len(cleaned):,} cleaned chars "
                            f"from {len(chunk):,} raw chars."
                        )
                        if attempt < max_retries:
                            logging.warning(f"   ⏳ {msg}  Retrying...")
                            print(f"   ⏳ Retry {attempt}/{max_retries} after stop_reason={stop_reason}")
                            time.sleep(min(2 ** attempt * 5, 60))
                            continue
                        raise RuntimeError(msg)
                    break
                except Exception as api_err:
                    err_str = str(api_err).lower()
                    if any(k in err_str for k in ("overloaded", "529", "500", "502", "503", "timeout", "timed out", "readtimeout", "connection", "disconnected")) and attempt < max_retries:
                        wait = min(2 ** attempt * 5, 180)
                        logging.warning(f"   ⏳ API error (attempt {attempt}/{max_retries}): {err_str[:120]}. Retrying in {wait}s...")
                        print(f"   ⏳ Retry {attempt}/{max_retries} in {wait}s...")
                        time.sleep(wait)
                    else:
                        raise

            cleaned_chunks.append(cleaned.strip())
            # Quality gate: warn if the cleaned chunk is suspiciously
            # short relative to its raw input.  Real LLM cleaning
            # typically *grows* the text (whitespace normalisation can
            # net increase chars or stay roughly equal).  A drop below
            # ~70% strongly suggests truncation or summarisation.
            ratio = len(cleaned) / max(1, len(chunk))
            if ratio < 0.70:
                logging.warning(
                    f"   ⚠️  Chunk {i}/{total_chunks}: cleaned output is "
                    f"only {ratio:.0%} of raw size ({len(cleaned):,} / "
                    f"{len(chunk):,} chars). Possible silent truncation "
                    f"or unwanted summarisation."
                )
                # Retry with explicit anti-summarisation nudge
                if attempt < max_retries:
                    logging.info(f"   🔄 Retrying chunk {i} with anti-truncation prompt...")
                    anti_trunc_prefix = (
                        "[CRITICAL: Your previous attempt only produced "
                        f"{len(cleaned):,} chars from {len(chunk):,} chars of input. "
                        "You MUST output the COMPLETE cleaned text — do NOT summarise, "
                        "abbreviate, or skip any sections. Output every single line.]\n\n"
                    )
                    user_message_retry = anti_trunc_prefix + user_message
                    try:
                        cleaned = ""
                        final_msg = None
                        with client.messages.stream(
                            model="claude-opus-4-6",
                            max_tokens=64000,
                            timeout=600.0,
                            system=system_prompt,
                            messages=[{"role": "user", "content": user_message_retry}],
                        ) as stream:
                            for text in stream.text_stream:
                                cleaned += text
                            final_msg = stream.get_final_message()
                            _track_usage(f"Hybrid Chunk {i}/{total_chunks} (retry)", final_msg.usage)
                        retry_ratio = len(cleaned) / max(1, len(chunk))
                        logging.info(
                            f"   🔄 Retry result: {len(cleaned):,} chars "
                            f"({retry_ratio:.0%} of raw). "
                            f"{'✅ Accepted' if retry_ratio >= 0.70 else '⚠️ Still short'}"
                        )
                        if retry_ratio > ratio:
                            # Use retry result if it's better
                            cleaned_chunks[-1] = cleaned.strip()
                    except Exception as retry_err:
                        logging.warning(f"   ⚠️ Retry failed: {retry_err}")
            logging.info(f"   ✅ Chunk {i}: {len(chunk):,} → {len(cleaned):,} chars")

        # ── Step 4: Merge cleaned chunks ─────────────────────────────
        # Pre-normalise tag formatting in each chunk so overlap detection
        # sees identical strings for the same content.  Without this,
        # one chunk may have line-level tags ([A>word<A] [A>word<A]) while
        # another has merged sentence-level tags ([A>word word<A]), causing
        # all stitch strategies to fail and duplicate text to appear.
        for ci in range(len(cleaned_chunks)):
            cleaned_chunks[ci] = merge_split_tags(cleaned_chunks[ci])

        if len(cleaned_chunks) == 1:
            merged = cleaned_chunks[0]
        else:
            merged = _merge_cleaned_chunks(cleaned_chunks)

        # ── Step 5: Post-processing (same as visual mode) ────────────
        # Generate diff report: what did the LLM change?
        diff_report = _generate_clean_diff_report(raw_text, merged, stage="Hybrid LLM Clean")
        convert_pdf_hybrid_text._last_diff_report = diff_report

        # Build per-chunk breakdown report (raw input → cleaned output for
        # every chunk). Saved alongside the final hybrid output by the
        # caller for diagnostic / QA purposes.
        _total_raw_chars = sum(len(c) for c in chunks)
        _total_cleaned_chars = sum(len(c) for c in cleaned_chunks)
        _chunks_report_lines = [
            "=" * 80,
            "HYBRID MODE — CHUNK BREAKDOWN",
            "=" * 80,
            f"PDF:                 {pdf_path}",
            f"State:               {state_code or '(none)'}",
            f"Total chunks:        {total_chunks}",
            f"Total raw chars:     {_total_raw_chars:,}",
            f"Total cleaned chars: {_total_cleaned_chars:,}",
            f"Max chunk size:      40,000 chars",
            f"Overlap:             2,000 chars",
            "=" * 80,
        ]
        for _idx, (_raw_chunk, _cleaned_chunk) in enumerate(zip(chunks, cleaned_chunks), 1):
            _chunks_report_lines.extend([
                "",
                "─" * 80,
                f"CHUNK {_idx} / {total_chunks} — RAW INPUT ({len(_raw_chunk):,} chars)",
                "─" * 80,
                _raw_chunk,
                "",
                "─" * 80,
                f"CHUNK {_idx} / {total_chunks} — CLEANED OUTPUT ({len(_cleaned_chunk):,} chars)",
                "─" * 80,
                _cleaned_chunk,
            ])
        convert_pdf_hybrid_text._last_chunks_report = "\n".join(_chunks_report_lines)

        # Bracket tagging runs AFTER LLM and AFTER CT-specific fixes to avoid
        # confusing the LLM with nested [D>[…]<D] structures, and to ensure
        # CT analysis-section tag-stripping doesn't remove bracket [D> tags.
        if flags.get("mark_brackets"):
            merged = _tag_brackets(merged)

        # CT-specific deterministic fixes
        if state_code and state_code.upper() == "CT":
            merged = _strip_ct_header_tags(merged)
            merged = _strip_ct_compact_designator(merged)
            merged = _strip_ct_false_underline_headings(merged)
            merged = _fix_midword_page_break_tags(merged)
            merged = _strip_false_footer_line_underline(merged)
            merged = _strip_ct_analysis_section_tags(merged)
            merged = _strip_false_subsection_d_tags(merged)
            merged = _restructure_ct_header(merged)
            # Re-run bracket tagging after analysis-section stripping
            # removed [D> tags from legitimate bracket deletions
            merged = _tag_brackets(merged)

        # IN-specific: strip false headers, footers, barcodes, year lines
        if state_code and state_code.upper() == "IN":
            merged = _strip_in_header_tags(merged)
            merged = _strip_in_artifacts(merged)

        # KS-specific: strip false header tags + signature block tags
        if state_code and state_code.upper() == "KS":
            merged = _strip_ks_header_tags(merged)
            merged = _strip_ks_signature_block_tags(merged)
            # Re-inject signature/fill-in underscore lines from PDF
            # vector drawings.  The extraction step already injected
            # them, but the LLM cleaning strips them.  Re-open the
            # PDF and scan the last page for drawing-based lines.
            try:
                import fitz as _fitz
                _doc = _fitz.open(pdf_path)
                _last = _doc[-1]
                merged = _inject_ks_signature_lines(_last, merged)
                _doc.close()
            except Exception as _sig_err:
                logging.warning("KS signature line re-injection failed: %s", _sig_err)

        # CA-specific: strip false [A>…<A] tags from signature block
        if state_code and state_code.upper() == "CA":
            merged = _strip_ca_signature_block_tags(merged)

        # FL-specific: deterministically strip repeated page headers/footers.
        # Catches ALL bill-header variations (CS, CS for CS, engrossed,
        # enrolled, doc-ref codes like 20261028er, etc.) via regex so
        # the LLM prompt never needs to be updated for new header styles.
        if state_code and state_code.upper() == "FL":
            merged = _strip_fl_repeated_headers(merged)

        # ── Normalise malformed close-tags BEFORE structural passes ──────
        # The chunk-level LLM occasionally emits `<A>` or `<D>` (mirrored
        # opening bracket) instead of the correct `<A]` / `<D]` closer.
        # If left in place, ``remove_nested_tags`` does NOT recognise
        # `<A>` as a closer, so its depth counter never decrements — the
        # next legitimate `[A>` opener is then skipped as "nested" and
        # the matching `<A]` becomes orphaned in the output.
        # Symptom: a closer appearing on its own line with no preceding
        # opener (e.g. ``... or subsection (4):<A]`` on FL SB 468).
        # Fix: rewrite mirrored closers to canonical form so depth
        # tracking stays balanced.  Must run BEFORE remove_nested_tags.
        merged = _fix_malformed_close_tags(merged)
        merged = _merge_page_break_paragraph_splits(merged, state_code=state_code)

        # ── Post-merge dedup ─────────────────────────────────────────────
        # The earlier dedup (inside _merge_cleaned_chunks) runs on the
        # raw per-line tagged text.  After _merge_page_break_paragraph_splits
        # collapses those lines into full paragraphs, duplicate overlap
        # content that was spread across many raw lines is now visible as
        # adjacent duplicate paragraphs.  Run dedup again to catch them.
        merged = _dedupe_consecutive_duplicate_blocks(merged)

        merged = consolidate_adjacent_tags(merged, state_code=state_code)
        merged = remove_nested_tags(merged)
        merged = merge_split_tags(merged)
        if state_code and state_code.upper() == "IA":
            merged = _split_tagged_subsections_safe(merged)
        # Restore paragraph breaks at renumber-only [D>(N)<D] [A>(M)<A] /
        # [D>SEC. N.<D] [A>SEC. M.<A] / [D>2182.<D] [A>2183.<A] pairs that
        # the chunk-level LLM clean tends to collapse onto a single line.
        merged = split_renumber_paragraph_breaks(merged)
        # Split [D>…<D] / [A>…<A] tags that the LLM merged across
        # consecutive table rows.  In appropriation / grants bills
        # (DE, AR, …) each grant line is its own row with its own
        # "$ amount"; the LLM sometimes fuses N consecutive same-tag
        # rows into one long tag.  This deterministic pass detects
        # tags with 2+ "$ amount" patterns and re-splits them onto
        # separate lines.  Safe for non-tabular states because prose
        # tags almost never contain two dollar amounts.
        merged = _split_merged_grant_tag_rows(merged)
        # DE/IN-specific: split tags that span description + dollar amount
        # columns, and header:data merged tags.
        if state_code and state_code.upper() in ("DE", "IN"):
            merged = _split_tabular_header_from_data(merged)
            merged = _split_tabular_tag_at_dollar(merged)
        # IN-specific: split tagged lines where a short numbered item
        # (e.g. "(2) interview each member.") got welded to the
        # following paragraph by the LLM.  Scoped to IN only so other
        # states are never affected.
        if state_code and state_code.upper() == "IN":
            merged = _split_in_merged_table_rows(merged)
            merged = _split_in_section_heading_welds(merged)
            merged = split_numbered_item_paragraph_welds(merged)
        # KS-specific: re-join LLM-inserted false paragraph breaks between
        # a "K.S.A. <num> ... amended to read as follows:" intro and the
        # statute body that follows.  Scoped to KS only (mirrors the
        # `_strip_ks_header_tags` block above) so we never disturb output
        # for CA / CT / IN / etc.
        if state_code and state_code.upper() == "KS":
            merged = rejoin_amendment_intro_breaks(merged)

        # IA / DE / IN-specific: re-join LLM-inserted false paragraph
        # breaks between [D>marker<D] and [A>marker<A] for Iowa
        # numeric subsection renumbers, Delaware parenthesised
        # paragraph renumbers, and Indiana parenthesised list
        # renumbers (all keep their markers on the same paragraph as
        # the body prose).  Scoped to IA + DE + IN only — California
        # uses the opposite convention (renumber on its own header
        # line).
        if state_code and state_code.upper() in {"IA", "DE", "IN"}:
            merged = rejoin_inline_renumber_list_breaks(merged)

        # IN-specific: split mid-line item merges like "; and (2)".
        if state_code and state_code.upper() == "IN":
            merged = _split_in_midline_items(merged)

        # IN-specific: ensure blank lines between (a)/(1) items.
        if state_code and state_code.upper() == "IN":
            merged = _ensure_in_item_spacing(merged)

        # IN-specific: strip leading indentation as the VERY LAST step.
        # Must run AFTER all merge/split/rejoin steps because those can
        # re-introduce leading whitespace.  Every IN output line should
        # start at the left margin.
        if state_code and state_code.upper() == "IN":
            merged = _strip_in_leading_indent(merged)

        # Universal: ensure multi-line ALL-CAPS headings have paragraph
        # breaks between them.  The LLM sometimes emits consecutive
        # heading lines (e.g. "ECONOMIC DEVELOPMENT PROGRAMS\n
        # CONFORMING CHANGES") with only \n instead of \n\n.
        merged = _split_consecutive_caps_heading_lines(merged)

        total_time = time.time() - start_time
        minutes = int(total_time // 60)
        seconds = int(total_time % 60)
        logging.info(f"✅ Hybrid conversion complete: {len(merged):,} characters")
        logging.info(f"⏱️  Total runtime: {minutes}m {seconds}s")
        print(f"   ✅ Hybrid conversion complete: {len(merged):,} chars in {minutes}m {seconds}s")

        return merged

    except Exception as e:
        logging.error(f"❌ Hybrid conversion failed: {e}", exc_info=True)
        return None


# ────────────────────────────────────────────────────────────────────────────
# Safeguarded subsection splitter for the hybrid pipeline
# ────────────────────────────────────────────────────────────────────────────
# The original ``split_tagged_subsections`` (from pymupdf_extractor.py) was
# disabled because it caused citation truncation (``K.S.A. 65-1501``,
# ``IC 36-1-3-1.8``), section-header bleed (``Sec. 1.``), and edit
# fragmentation.  This replacement adds explicit safeguards that prevent
# those problems while still splitting at genuine paragraph-level markers
# like ``a.``, ``b.``, ``(1)``, ``(a)`` that appear inside a single tag.
#
# Key safeguards:
#   1. **Citation guard** — text before the marker is checked for citation
#      prefixes (K.S.A., IC, U.S.C., §, section, subsection, chapter,
#      paragraph).  If found, the marker is skipped.
#   2. **Section-header guard** — markers that look like ``Sec. N.`` or
#      ``Section N.`` are excluded.
#   3. **Minimum content guard** — the piece AFTER the split must contain
#      at least 50 characters of content (prevents tiny garbage fragments).
#   4. **Inline-list guard** — if all markers are preceded by semicolons,
#      the tag is treated as an inline list and not split.
# ────────────────────────────────────────────────────────────────────────────
_RE_SUBSEC_MARKER = re.compile(
    r'(?:(?<=[.:;])|(?<=\band\b)|(?<=\bor\b))\s*'
    r'('
    r'\(\d+\)[a-z]\.\d*\.?'           # (1)a.
    r'|\(\d+\)|\([A-Z]+\)|\([a-z]+\)' # (1), (A), (a)
    r'|[a-z]\.\d*\.?'                  # a., b.
    r'|\d+\.'                          # 1., 2.
    r')(?:\s|$)',
    re.IGNORECASE,
)

# Citation prefixes that precede a marker and indicate it's part of a
# legal citation, NOT a subsection boundary.  E.g. "section 15.329",
# "IC 36-1-3", "K.S.A. 65-1501(a)", "subsection 1, paragraph"
_RE_CITATION_PREFIX = re.compile(
    r'(?:section|subsection|paragraph|chapter|subchapter|part|division'
    r'|IC|K\.S\.A\.|U\.S\.C\.|§|Sec\.|SEC\.|Section|SECTION'
    r'|Article|ARTICLE|Art\.|ART\.)'
    r'\s*[\d.,()\-a-zA-Z]*\s*$'
    r'|'
    # Also catch "42 U.S.C." / "29 U.S.C." etc. where a preceding number
    # is part of the citation (e.g. "42 U.S.C. §1751–1785")
    r'\d+\s+U\.S\.C\.\s*[\d§.,()\-a-zA-Z]*\s*$',
    re.IGNORECASE,
)


def _split_tagged_subsections_safe(text):
    """Split tagged blocks at subsection markers with citation-aware safeguards.

    Unlike the original ``split_tagged_subsections`` (disabled as a no-op),
    this version avoids false splits on legal citations, section headers,
    and inline lists.  Only genuine paragraph-level markers trigger a split.
    """

    def _is_inline_list(content, markers):
        """Return True if markers are separated by semicolons (inline list)."""
        if not markers:
            return False
        list_sep_count = 0
        period_count = 0
        for m in markers:
            preceding = content[:m.start()].rstrip()
            if preceding.endswith(';'):
                list_sep_count += 1
            elif preceding.endswith(('.', ':')):
                period_count += 1
            else:
                list_sep_count += 1
        return list_sep_count > period_count

    def _split_block(match):
        tag_open = match.group(1)
        content = match.group(2)
        tag_close = match.group(3)

        # Only split long-ish tags (short tags are unlikely to contain
        # genuine multi-paragraph content).
        if len(content) < 80:
            return match.group(0)

        markers = list(_RE_SUBSEC_MARKER.finditer(content))
        if not markers:
            return match.group(0)

        # ── Filter out false-positive markers ──────────────────────────
        safe_markers = []
        for m in markers:
            preceding = content[:m.start()].rstrip()
            marker_text = m.group(1)

            # Guard 1: Citation prefix — the text before the marker ends
            # with a citation keyword or abbreviation.
            if _RE_CITATION_PREFIX.search(preceding):
                continue

            # Guard 1b: Abbreviation component — the marker is a single
            # letter + period (e.g. "C." from "U.S.C.") preceded by
            # another single-letter abbreviation component (e.g. "S.").
            # This catches dotted abbreviations like U.S.C., P.L., etc.
            if (re.match(r'^[a-zA-Z]\.$', marker_text)
                    and re.search(r'[A-Z]\.\s*$', preceding)):
                continue

            # Guard 1c: Statute number continuation — the marker is
            # directly adjacent to a preceding dotted number with NO
            # whitespace gap, e.g. "256F.5." where "5." is part of
            # the statute number 256F.5, not a new subsection.
            # Check the raw (un-rstripped) text: if the character
            # immediately before the marker is a period (from a
            # number/code like 256F.), it's a dotted continuation.
            raw_before_marker = content[:m.start(1)]
            if (raw_before_marker
                    and raw_before_marker[-1] == '.'
                    and len(raw_before_marker) >= 2
                    and raw_before_marker[-2].isalnum()):
                continue

            # Guard 2: Section header — skip "Sec. N." / "SECTION N."
            # patterns that are section headers, not subsection markers.
            if re.match(r'(?:Sec|SEC|Section|SECTION|Art|ART|Article|ARTICLE)\.?$',
                        marker_text, re.IGNORECASE):
                continue

            # Guard 3: The content AFTER this marker must be substantial
            # (at least 50 chars) to avoid producing tiny garbage fragments.
            remaining = content[m.start():].strip()
            if len(remaining) < 50:
                continue

            safe_markers.append(m)

        if not safe_markers:
            return match.group(0)

        # Guard 4: Inline list detection — if markers are separated by
        # semicolons, this is an inline enumeration, not separate paragraphs.
        if _is_inline_list(content, safe_markers):
            return match.group(0)

        # ── Split at safe marker positions ─────────────────────────────
        pieces = []
        prev_end = 0
        for m in safe_markers:
            split_at = m.start()
            before = content[prev_end:split_at].rstrip()
            if before:
                pieces.append(before)
            prev_end = split_at
        last = content[prev_end:].strip()
        if last:
            pieces.append(last)

        # ── Guard 5: Bare-marker orphan prevention ─────────────────────
        # If a non-final piece is just a bare subsection marker (e.g.,
        # "2.", "a.", "(1)"), merge it into the next piece to avoid
        # creating a meaningless orphan tag like [D>2.<D].
        _bare_marker_re = re.compile(
            r'^\s*(?:\d+\.?|[a-z]\.?|\(\s*\d+\s*\)|\(\s*[a-z]+\s*\))\s*$',
            re.IGNORECASE,
        )
        if len(pieces) > 1:
            merged_pieces = []
            i = 0
            while i < len(pieces):
                if (i < len(pieces) - 1
                        and _bare_marker_re.match(pieces[i].strip())):
                    # Merge bare marker with the next piece
                    pieces[i + 1] = pieces[i].rstrip() + ' ' + pieces[i + 1].lstrip()
                    i += 1
                else:
                    merged_pieces.append(pieces[i])
                    i += 1
            pieces = merged_pieces

        tagged_pieces = [f"{tag_open}{p.strip()}{tag_close}" for p in pieces]
        result = '\n\n'.join(tagged_pieces)
        if result != match.group(0):
            logging.info(
                f"   ✂️  Split tagged block into {len(tagged_pieces)} pieces "
                f"at markers: {[m.group(1) for m in safe_markers]}"
            )
        return result

    text = re.sub(r'(\[A>)(.*?)(<A\])', _split_block, text, flags=re.DOTALL)
    text = re.sub(r'(\[D>)(.*?)(<D\])', _split_block, text, flags=re.DOTALL)
    return text


def _split_consecutive_caps_heading_lines(text):
    """Insert paragraph breaks between consecutive ALL-CAPS heading lines.

    The LLM sometimes emits multi-line headings (e.g. a division title
    followed by a subtitle) with only ``\\n`` between them instead of
    ``\\n\\n``.  Example::

        ECONOMIC DEVELOPMENT PROGRAMS —— TAX CREDIT LIMITS
        CONFORMING CHANGES

    This function detects consecutive non-blank lines that are both
    ALL-CAPS (letters only, after stripping tags) and inserts a blank
    line between them so they render as separate paragraphs.

    Safe for all states — only touches lines that are entirely uppercase
    with at least 4 letter characters (avoids matching short tokens).
    """
    _TAG_RE = re.compile(r'\[([AD])>|<([AD])\]')
    lines = text.split('\n')
    result = []
    i = 0
    fix_count = 0
    while i < len(lines):
        result.append(lines[i])
        if i + 1 < len(lines):
            a = lines[i].rstrip()
            b = lines[i + 1].rstrip()
            if a and b:
                a_clean = _TAG_RE.sub('', a).strip()
                b_clean = _TAG_RE.sub('', b).strip()
                a_letters = re.sub(r'[^a-zA-Z]', '', a_clean)
                b_letters = re.sub(r'[^a-zA-Z]', '', b_clean)
                if (a_letters and b_letters
                        and len(a_letters) >= 4 and len(b_letters) >= 4
                        and a_letters == a_letters.upper()
                        and b_letters == b_letters.upper()):
                    result.append('')  # insert blank line
                    fix_count += 1
        i += 1
    if fix_count:
        logging.info(
            f"   📐 Inserted {fix_count} paragraph break(s) between "
            f"consecutive ALL-CAPS heading lines"
        )
    return '\n'.join(result)


def _merge_page_break_paragraph_splits(text, state_code=None):
    """Merge paragraphs that were split mid-sentence at a PDF page break.

    The chunk-level LLM sometimes fails to rejoin paragraphs that cross a
    page boundary in the raw PyMuPDF extraction.  Three patterns are
    detected and repaired:

    **Pattern 1 — both tags present**:
      A ends ``<TAG]``, B starts ``[TAG>`` (same type).

      *Default (all states)*: merges only when B starts with a lowercase
      letter and A does not end with terminal punctuation — the original
      conservative behaviour.

      *FL-specific enhancement* (``state_code="FL"``):

      * **1a — mid-sentence**: A does *not* end with terminal punctuation
        (``. ! ? : ;``) or a section-header delimiter (em/en dash).
        Merge regardless of B's case (uppercase continuations like
        ``…Technology<A]  [A>AI Risk Management…`` are common in FL).
        Sets the ``_p1a_active`` flag for the current paragraph.

      * **1b — terminal punct, raw-format continuation**: A ends with
        terminal punctuation BUT ``_p1a_active`` is True — meaning
        Pattern 1a has been actively merging per-line tags from raw PDF
        extraction into this paragraph.  Terminal punct at a per-line
        boundary is just where the PDF line happened to end on a
        sentence boundary, *not* a real paragraph break.  Only fires
        when ``_p1a_active`` is True; when False the paragraph was
        already a complete LLM-cleaned unit and the following tag is
        genuinely separate (avoids SB 1028 / SB 1332 regressions).

    **Pattern 2 — missing re-open tag** (new):
      A ends ``<TAG]`` but B does *not* start with any tag.  The LLM
      closed the tag at the page break but forgot to reopen.  Same
      lowercase-start and no-terminal-punctuation guards apply, plus
      B must not begin with a subsection marker (``a. ``) and A's
      tag-stripped text must be ≥ 40 chars (avoids merging headings).

    **Pattern 3 — no tags at boundary** (new):
      Neither paragraph has a tag at the join.  Same guards as Pattern 2.

    Runs **before** ``consolidate_adjacent_tags`` / ``merge_split_tags``
    so the structural tag passes see the corrected paragraph topology.
    """
    _is_fl = state_code and state_code.upper() == "FL"
    _is_ct = state_code and state_code.upper() == "CT"

    _BARE_SUBSEC = re.compile(r'^[a-z]\.\s')
    _TAG_STRIP   = re.compile(r'\[([AD])>|<([AD])\]')
    # Subsection / section markers that signal a NEW paragraph —
    # never merge across these (same for ALL states including FL).
    # The FL-specific logic is in Pattern 1 below (line ~1988):
    # it allows merging across uppercase starts and sentence
    # boundaries, but ONLY when B doesn't start with any of these
    # markers.
    _SUBSEC_OR_SECTION = re.compile(
        r'^\(\d+\)'            # (1)
        r'|\([a-zA-Z]{1,4}\)'  # (a), (A), (iv)
        r'|\d+\.[\s<]'         # 1.  (incl. standalone [D>1.<D] tags)
        r'|[a-z]\.[\s<]'       # a.  (incl. standalone [A>a.<A] tags)
        r'|Sec\.[\s<]'         # Sec. (incl. standalone tags)
        r'|SECTION[\s<]'       # SECTION (incl. standalone tags)
        r'|\d{2,}\.\d'         # 501.9986 (statute numbers)
        r'|"'                  # "Definition..." start
    )

    paras = text.split('\n\n')
    if len(paras) <= 1:
        return text

    merged_paras: list[str] = []
    i = 0
    merge_count = 0
    while i < len(paras):
        curr = paras[i]
        _p1a_active = False   # True once Pattern 1a merges in this run
        # Try to merge with one or more subsequent paragraphs whose
        # tag was split at a page break.
        while i + 1 < len(paras):
            nxt = paras[i + 1]
            curr_s = curr.rstrip()
            nxt_s = nxt.lstrip()

            did_merge = False
            for tag in ('A', 'D'):
                close_tag = f'<{tag}]'
                open_tag = f'[{tag}>'

                # --- Pattern 1: A ends <TAG], B starts [TAG> (same) ---
                if curr_s.endswith(close_tag) and nxt_s.startswith(open_tag):
                    inner = nxt_s[len(open_tag):].lstrip()
                    text_before = curr_s[:-len(close_tag)].rstrip()
                    if not inner or not text_before:
                        continue
                    # Guard: subsection / section marker → new paragraph.
                    # FL-specific exceptions prevent false blocking:
                    #
                    # (a) Tag A ends with a section word/abbreviation
                    #     ("Section", " s.", " S.", " sec.", " Sec.")
                    #     and tag B starts with a statute number.  E.g.
                    #     "established in s." → "627.351(6)(c)5.c.,…"
                    #     "the corporation. Section" → "627.351(6)…"
                    #
                    # (b) Tag A ends with a preposition and tag B starts
                    #     with "s. <digits>" (section reference split at
                    #     the page break).  E.g.
                    #     "punishable as provided in" → "s. 775.082,…"
                    _skip_subsec_guard = False
                    if _is_fl and _SUBSEC_OR_SECTION.match(inner):
                        _skip_subsec_guard = bool(
                            re.search(r'\b[sS]\.$|\b[sS]ec\.$|\bSection$', text_before)
                        ) or bool(
                            re.search(r'\b(?:in|under|of|per|to|see)\s*$', text_before, re.IGNORECASE)
                            and re.match(r'[sS]\.\s+\d', inner)
                        )
                    if _SUBSEC_OR_SECTION.match(inner) and not _skip_subsec_guard:
                        continue
                    # Guard: section-header delimiter (em/en dash)
                    if text_before[-1] in '\u2014\u2013':
                        continue
                    has_terminal = text_before[-1] in '.!?:;'
                    if _is_fl:
                        # ── FL-specific enhanced merging ──────────────
                        if not has_terminal:
                            # Guard: list-item conclusion pattern.
                            # When the paragraph starts with a letter
                            # subsection marker like (d), ends with a
                            # comma, and _p1a_active is False (we are
                            # NOT in per-line raw merge mode), the next
                            # paragraph is typically a conclusion/
                            # penalty clause that completes the sentence
                            # started *before* the list — e.g.
                            #   "Any person who …:  (a) …; (b) …;
                            #    (c) …; or (d) …false information,
                            #
                            #    commits a felony …"
                            # Do NOT merge.
                            if (not _p1a_active
                                    and text_before.rstrip()[-1:] == ','
                                    and re.match(
                                        r'\([a-z]\)\s',
                                        _TAG_STRIP.sub('', curr).strip())):
                                continue
                            # 1a — mid-sentence: no terminal punct →
                            # always merge regardless of B's case
                            curr = text_before + ' ' + nxt_s[len(open_tag):]
                            i += 1
                            merge_count += 1
                            did_merge = True
                            _p1a_active = True
                            break
                        # 1b — terminal punct, raw-format continuation.
                        # Only fires when Pattern 1a already merged
                        # per-line tags into curr (_p1a_active=True),
                        # meaning the text is raw PDF extraction where
                        # terminal punct at a line break is NOT a real
                        # paragraph boundary.  When _p1a_active is
                        # False the paragraph came from the LLM as a
                        # complete unit and the next tag is genuinely
                        # separate (SB 1028 / SB 1332 regression guard).
                        if _p1a_active:
                            curr = text_before + ' ' + nxt_s[len(open_tag):]
                            i += 1
                            merge_count += 1
                            did_merge = True
                            break
                        continue
                    else:
                        # ── Default (all other states) ────────────────
                        # Original conservative logic: merge only when
                        # B starts lowercase and A has no terminal punct
                        if not inner[0].islower():
                            continue
                        if has_terminal:
                            # CT-specific: semicolon + list continuation.
                            # When A ends with ";" and B starts with a
                            # conjunction + parenthesised marker like
                            # "and (2)" or "or (B)", the semicolon is a
                            # list-item separator, not a paragraph
                            # boundary.  Allow the merge.
                            if not (_is_ct
                                    and text_before[-1] == ';'
                                    and re.match(r'(?:and|or)\s+\(', inner)):
                                continue
                        curr = text_before + ' ' + nxt_s[len(open_tag):]
                        i += 1
                        merge_count += 1
                        did_merge = True
                        break

                # --- Pattern 2: A ends <TAG], B has NO open tag ---
                # LLM closed the tag at page break but forgot to reopen.
                if (curr_s.endswith(close_tag)
                        and not nxt_s.startswith('[A>')
                        and not nxt_s.startswith('[D>')):
                    inner = nxt_s.lstrip()
                    if not inner or not inner[0].islower():
                        continue
                    if _BARE_SUBSEC.match(inner):
                        continue
                    text_before = curr_s[:-len(close_tag)].rstrip()
                    if text_before and text_before[-1] in '.!?':
                        continue
                    # A's tag-stripped content must be substantial
                    if len(_TAG_STRIP.sub('', text_before).strip()) < 40:
                        continue
                    # Merge: strip close tag from A, keep B as-is
                    curr = text_before + ' ' + nxt_s
                    i += 1
                    merge_count += 1
                    did_merge = True
                    break

            # --- Pattern 3: no tags at boundary (general mid-sentence) ---
            if not did_merge:
                if (not curr_s.endswith('<A]')
                        and not curr_s.endswith('<D]')
                        and not nxt_s.startswith('[A>')
                        and not nxt_s.startswith('[D>')):
                    inner = nxt_s.lstrip()
                    if inner and inner[0].islower():
                        a_clean = _TAG_STRIP.sub('', curr_s).rstrip()
                        if (a_clean
                                and a_clean[-1] not in '.!?:;'
                                and not _BARE_SUBSEC.match(inner)
                                and len(a_clean) >= 40):
                            curr = curr_s + ' ' + nxt_s
                            i += 1
                            merge_count += 1
                            did_merge = True

            if not did_merge:
                break

        merged_paras.append(curr)
        i += 1

    if merge_count:
        logging.info(
            f"   🔗 Merged {merge_count} page-break paragraph split(s)"
        )

    return '\n\n'.join(merged_paras)


def _merge_cleaned_chunks(chunks):
    """
    Merge cleaned text chunks by deduplicating overlap regions.

    The chunks are produced by the hybrid pipeline (40K chars each, 2K-char
    overlap).  Each chunk is cleaned by the LLM independently, so the
    overlap region may have minor differences between consecutive chunks
    (slightly different whitespace, dot-leader counts, em-dash variants,
    etc.).  Strict character-level matching is unreliable; this function
    therefore tries several strategies in order:

      1. **Section-anchor merge** — find a section marker (``Sec. N.``,
         ``SECTION N.``, ``§ N.``, etc.) that appears in BOTH the tail of
         the running merge AND the head of the next chunk; cut both at
         that anchor and stitch.  Robust against LLM rephrasing because
         it only needs the section label itself to match.
      2. **Exact suffix-prefix** — longest L such that
         ``tail[-L:] == head[:L]`` after whitespace normalisation.
      3. **Normalised line match** — find a non-trivial line in the tail
         that appears verbatim in the head.
      4. **Concatenate with separator** — last resort; logs a warning.

    After all chunks are merged, a final pass removes any consecutive
    duplicate paragraph runs that survived (defence in depth — catches
    cases where step 4 fired or where the chosen anchor missed an
    earlier duplicate).

    Args:
        chunks (list[str]): Cleaned text chunks in order.

    Returns:
        str: Merged text.
    """
    if not chunks:
        return ""

    merged = chunks[0]
    any_fallback = False
    for i in range(1, len(chunks)):
        merged, used_fallback = _stitch_two_chunks(merged, chunks[i], chunk_index=i + 1)
        if used_fallback:
            any_fallback = True

    # Only run the dedup pass when Strategy 4 (raw concatenation) was used.
    # Strategies 1-3 already handle overlap precisely — running dedup after
    # them risks removing LEGITIMATE duplicate content that appears in the
    # source PDF (e.g. identical boilerplate subsections across different
    # numbered sections in legislation).
    if any_fallback:
        deduped = _dedupe_consecutive_duplicate_blocks(merged)
        if deduped != merged:
            logging.info(
                f"   🧹 Post-merge dedup removed {len(merged) - len(deduped):,} "
                f"chars of duplicate paragraph runs"
            )
        return deduped
    return merged


# ────────────────────────────────────────────────────────────────────────────
# Section-anchor regex used by the hybrid chunk merger.
# Matches a section header at the start of a paragraph.  Examples that
# match:
#     "Sec. 85."          (Iowa, Kansas)
#     "SECTION 1."        (California, Indiana)
#     "Section 12.5."     (with decimal)
#     "§ 1234.5"          (Code section reference)
#     "ARTICLE III."      (Roman numerals)
#     "Sec. 85a."         (alphanumeric suffix)
# The capture group is the canonical anchor label used to compare across
# chunks (whitespace-normalised).
# ────────────────────────────────────────────────────────────────────────────
_RE_SECTION_ANCHOR = re.compile(
    r'(?:^|\n)[ \t]*'
    r'((?:'
    r'Sec\.|SEC\.|Section|SECTION'                  # English section
    r'|Art\.|ART\.|Article|ARTICLE'                  # Article
    r'|§'                                            # Section symbol
    r')\s+'
    r'(?:'
    r'\d+[a-zA-Z]?(?:\.\d+)?'                        # 85, 85a, 12.5
    r'|[IVXLCDM]+'                                   # Roman numerals
    r')'
    r'\.?)\s',
    re.MULTILINE,
)


def _section_anchors(text):
    """Yield (label, position) for every section anchor in ``text``.

    The label is canonicalised by stripping whitespace and trailing dots
    so ``"Sec. 85"``, ``"Sec. 85."`` and ``"sec.\t85"`` all map to the
    same key (``"sec. 85"``).
    """
    for m in _RE_SECTION_ANCHOR.finditer(text):
        raw_label = m.group(1)
        label = ' '.join(raw_label.lower().split()).rstrip('.')
        yield label, m.start(1)


def _stitch_two_chunks(merged, next_chunk, *, chunk_index, window=4000):
    """Stitch ``next_chunk`` onto ``merged``, deduplicating any overlap.

    Returns a tuple ``(merged_string, used_fallback)`` where
    ``used_fallback`` is True only when Strategy 4 (raw concatenation)
    was used.  See ``_merge_cleaned_chunks`` for the strategy hierarchy.
    """
    # ── Strategy 1: section-anchor merge ────────────────────────────────
    tail_window = merged[-window:]
    head_window = next_chunk[:window]
    tail_offset = len(merged) - len(tail_window)

    head_anchors = list(_section_anchors(head_window))
    if head_anchors:
        # Map each head anchor label to its earliest position in head_window
        head_anchor_first = {}
        for label, pos in head_anchors:
            head_anchor_first.setdefault(label, pos)

        # Walk the tail anchors in REVERSE so we pick the LATEST shared
        # anchor — that gives us the longest possible deduplication.
        tail_anchors = list(_section_anchors(tail_window))
        for label, tail_pos in reversed(tail_anchors):
            if label in head_anchor_first:
                head_pos = head_anchor_first[label]
                global_tail_pos = tail_offset + tail_pos

                # ── Sanity check: verify the merge doesn't drop content ──
                # The overlap region is ~2K chars.  If this anchor match
                # would cut MORE than the overlap + a generous margin from
                # the tail, it's a false match (e.g. an effectiveness table
                # "Sec. 3" matching the actual Section 3 body).  Skip it
                # and try the next anchor.
                tail_chars_dropped = len(merged) - global_tail_pos
                head_chars_skipped = head_pos
                expected_overlap = 6000  # generous upper bound for real overlap
                if tail_chars_dropped + head_chars_skipped > expected_overlap:
                    logging.warning(
                        f"   ⚠️  Chunk {chunk_index}: skipping section-anchor "
                        f"'{label}' — would drop {tail_chars_dropped:,} tail + "
                        f"{head_chars_skipped:,} head chars "
                        f"(>{expected_overlap:,} expected overlap). "
                        f"Likely a false match (e.g. effectiveness table)."
                    )
                    continue

                logging.info(
                    f"   🔗 Chunk {chunk_index}: section-anchor merge at "
                    f"'{label}' (tail offset {global_tail_pos:,}, "
                    f"head offset {head_pos:,})"
                )
                return merged[:global_tail_pos].rstrip() + '\n\n' + next_chunk[head_pos:], False

    # ── Strategy 2: exact suffix-prefix ─────────────────────────────────
    # Whitespace-normalised character match.  Cheaper to compute than the
    # original O(N²) loop because we walk the head from the longest plausible
    # prefix backward and stop at the first match.
    best_overlap = 0
    search_len = min(window, len(merged), len(next_chunk))
    tail = merged[-search_len:]
    head = next_chunk[:search_len]
    # Normalise once, then compare slices of the normalised strings.  This
    # makes the comparison O(N) per length value rather than O(N) for the
    # split() each iteration.
    norm_tail = ' '.join(tail.split())
    norm_head = ' '.join(head.split())
    norm_min = min(len(norm_tail), len(norm_head))
    for length in range(norm_min, 50, -1):
        if norm_tail[-length:] == norm_head[:length]:
            best_overlap = length
            break
    if best_overlap >= 50:
        # We matched on normalised lengths — find the corresponding raw
        # head length so the join is byte-accurate.  Walk forward through
        # head, counting normalised chars until we hit `best_overlap`.
        normed = 0
        in_ws = False
        raw_cut = 0
        for j, ch in enumerate(head):
            if ch.isspace():
                if not in_ws:
                    normed += 1  # collapsed whitespace counts as 1
                    in_ws = True
            else:
                normed += 1
                in_ws = False
            if normed >= best_overlap:
                raw_cut = j + 1
                break
        if raw_cut > 0:
            logging.info(
                f"   🔗 Chunk {chunk_index}: exact suffix-prefix overlap "
                f"({best_overlap} normalised chars, {raw_cut} raw chars)"
            )
            return merged + next_chunk[raw_cut:], False

    # ── Strategy 2b: tail-probe overlap ─────────────────────────────────
    # When the LLM starts the head mid-sentence (e.g. "n section 10-264s,
    # shall not charge tuition…"), Strategies 1-2 fail because (1) no
    # shared section anchor exists between the tail and head windows and
    # (2) the boundary characters don't align for an exact suffix-prefix
    # match.
    #
    # Fix: take a normalised probe from the END of *merged* and search
    # for it inside the beginning of *next_chunk*.  If the probe is
    # found, everything in next_chunk before the probe's end is overlap
    # that should be skipped.  merged is kept in full (it has the
    # complete start of the overlapping paragraph) and next_chunk
    # continues from where merged left off.
    _PROBE_LEN = 200          # normalised chars — long enough to avoid false hits
    _PROBE_MIN = 80           # minimum viable probe length
    tail_for_probe = merged[-3000:]
    tail_probe_norm = ' '.join(tail_for_probe.split())
    actual_probe_len = min(_PROBE_LEN, len(tail_probe_norm))
    if actual_probe_len >= _PROBE_MIN:
        probe = tail_probe_norm[-actual_probe_len:]
        head_for_probe = next_chunk[:6000]
        head_probe_norm = ' '.join(head_for_probe.split())
        probe_hit = head_probe_norm.find(probe)
        if probe_hit >= 0:
            # Overlap confirmed.  Cut next_chunk just after the matched
            # probe so the duplicated text is skipped.
            cut_norm = probe_hit + actual_probe_len
            # Map normalised position → raw position in next_chunk.
            normed_2b = 0
            in_ws_2b = False
            raw_cut_2b = 0
            for j, ch in enumerate(head_for_probe):
                if ch.isspace():
                    if not in_ws_2b:
                        normed_2b += 1
                        in_ws_2b = True
                else:
                    normed_2b += 1
                    in_ws_2b = False
                if normed_2b >= cut_norm:
                    raw_cut_2b = j + 1
                    break
            if raw_cut_2b > 0:
                candidate = merged.rstrip() + next_chunk[raw_cut_2b:]
                # Sanity: stitched result should not be shorter than the
                # longer input (overlap dedup shortens by at most a few K).
                min_expected = max(len(merged), len(next_chunk))
                if len(candidate) >= min_expected:
                    logging.info(
                        f"   🔗 Chunk {chunk_index}: tail-probe overlap "
                        f"({actual_probe_len} probe chars matched at norm "
                        f"offset {probe_hit}, raw cut at {raw_cut_2b})"
                    )
                    return candidate, False
                else:
                    logging.warning(
                        f"   ⚠️  Chunk {chunk_index}: tail-probe cut would "
                        f"shrink result to {len(candidate):,} "
                        f"(below {min_expected:,}). Skipping to Strategy 3."
                    )

    # ── Strategy 2c: tag-stripped overlap detection ─────────────────────
    # When the LLM tags the overlap region differently in each chunk
    # (e.g. chunk 1 has per-line [D>…<D] tags while chunk 2 merges them
    # into paragraph-level tags), all prior strategies fail because the
    # tagged text doesn't match.  Fix: strip formatting tags from both
    # overlap windows, do a probe match on the bare text, then map the
    # match position back to the original tagged next_chunk.
    _TAG_RE = re.compile(r'\[([AD])>|<([AD])\]')
    def _strip_tags(s):
        return _TAG_RE.sub('', s)

    _PROBE_2C_LEN = 300       # longer probe for tag-stripped matching
    _PROBE_2C_MIN = 100
    tail_for_2c = merged[-4000:]
    tail_stripped = _strip_tags(tail_for_2c)
    tail_2c_norm = ' '.join(tail_stripped.split())
    actual_2c_len = min(_PROBE_2C_LEN, len(tail_2c_norm))
    if actual_2c_len >= _PROBE_2C_MIN:
        probe_2c = tail_2c_norm[-actual_2c_len:]
        head_for_2c = next_chunk[:8000]
        head_stripped = _strip_tags(head_for_2c)
        head_2c_norm = ' '.join(head_stripped.split())
        probe_2c_hit = head_2c_norm.find(probe_2c)
        if probe_2c_hit >= 0:
            # Map normalised-stripped position → raw position in next_chunk.
            # Walk the original (tagged) head text, stripping tags and
            # collapsing whitespace, counting normalised-stripped chars
            # until we reach the cut point.
            cut_2c_norm = probe_2c_hit + actual_2c_len
            normed_2c = 0
            in_ws_2c = False
            raw_cut_2c = 0
            j_2c = 0
            while j_2c < len(head_for_2c):
                # Skip tag sequences in the raw text
                tag_m = _TAG_RE.match(head_for_2c, j_2c)
                if tag_m:
                    j_2c = tag_m.end()
                    continue
                ch = head_for_2c[j_2c]
                if ch.isspace():
                    if not in_ws_2c:
                        normed_2c += 1
                        in_ws_2c = True
                else:
                    normed_2c += 1
                    in_ws_2c = False
                j_2c += 1
                if normed_2c >= cut_2c_norm:
                    raw_cut_2c = j_2c
                    break
            if raw_cut_2c > 0:
                candidate_2c = merged.rstrip() + '\n\n' + next_chunk[raw_cut_2c:].lstrip('\n')
                min_expected_2c = max(len(merged), len(next_chunk))
                if len(candidate_2c) >= min_expected_2c:
                    logging.info(
                        f"   🔗 Chunk {chunk_index}: tag-stripped overlap "
                        f"({actual_2c_len} probe chars matched at stripped "
                        f"offset {probe_2c_hit}, raw cut at {raw_cut_2c})"
                    )
                    return candidate_2c, False
                else:
                    logging.warning(
                        f"   ⚠️  Chunk {chunk_index}: tag-stripped cut "
                        f"would shrink result to {len(candidate_2c):,} "
                        f"(below {min_expected_2c:,}). Skipping."
                    )

    # ── Strategy 2d: paragraph-level tag-stripped overlap ───────────────
    # When the LLM produces different subsection numbering or truncates
    # content at the chunk boundary, the character-level probes in
    # Strategies 2b/2c fail.  This strategy compares whole paragraphs
    # (tag-stripped, normalised) from the head against those in the tail.
    # The FIRST exact paragraph match establishes the overlap anchor:
    #   • merged is kept up to (not including) the matching tail paragraph
    #   • next_chunk is used from the matching head paragraph onwards
    # This cleanly handles the HF 2754 case where subsections b. and c.
    # appear identically in both chunks but surrounding text differs.
    _TAG_2D = re.compile(r'\[([AD])>|<([AD])\]')
    _MIN_PARA_2D = 50  # minimum chars for a paragraph to be match-worthy

    tail_win_2d = merged[-6000:]
    head_win_2d = next_chunk[:10000]

    # Build list of (raw_offset_in_window, normalised_stripped_text) for tail
    tail_paras_2d = []
    t_pos = 0
    for t_part in tail_win_2d.split('\n\n'):
        stripped = ' '.join(_TAG_2D.sub('', t_part).split())
        if len(stripped) >= _MIN_PARA_2D:
            tail_paras_2d.append((t_pos, stripped))
        t_pos += len(t_part) + 2  # +2 for \n\n

    # Build index of tail paragraph text → position for O(1) lookup
    tail_para_index = {}
    for t_pos_2d, t_text in tail_paras_2d:
        tail_para_index.setdefault(t_text, t_pos_2d)

    # Scan head paragraphs and find the first that exactly matches a tail para
    h_pos = 0
    first_match_2d = None
    for h_part in head_win_2d.split('\n\n'):
        stripped_h = ' '.join(_TAG_2D.sub('', h_part).split())
        if len(stripped_h) >= _MIN_PARA_2D and stripped_h in tail_para_index:
            first_match_2d = (h_pos, tail_para_index[stripped_h], stripped_h)
            break
        h_pos += len(h_part) + 2

    if first_match_2d:
        h_cut_pos, t_cut_pos, match_text = first_match_2d
        # Cut merged at the START of the matching tail paragraph
        global_cut = len(merged) - len(tail_win_2d) + t_cut_pos
        # next_chunk from the matching head paragraph onwards
        candidate_2d = merged[:global_cut].rstrip() + '\n\n' + next_chunk[h_cut_pos:].lstrip('\n')
        min_expected_2d = max(len(merged), len(next_chunk))
        if len(candidate_2d) >= min_expected_2d:
            logging.info(
                f"   🔗 Chunk {chunk_index}: paragraph-level tag-stripped "
                f"overlap (matched {len(match_text)} stripped chars, "
                f"tail cut at -{len(tail_win_2d) - t_cut_pos}, "
                f"head cut at {h_cut_pos})"
            )
            return candidate_2d, False
        else:
            logging.warning(
                f"   ⚠️  Chunk {chunk_index}: paragraph-level cut would "
                f"shrink result to {len(candidate_2d):,} "
                f"(below {min_expected_2d:,}). Skipping."
            )

    # ── Strategy 3: normalised line match ───────────────────────────────
    # IMPORTANT: use ``tail_window`` / ``head_window`` (the full ``window``-
    # sized slices computed at the top of the function) rather than the
    # ``tail`` / ``head`` variables Strategy 2 uses.  Strategy 2's slices
    # are sized to ``min(window, len(merged), len(next_chunk))`` so when
    # one chunk is shorter than ``window`` (e.g. a small trailing chunk
    # of 1.8K chars vs. a 36K main chunk) the Strategy 2 ``tail`` is
    # *narrower* than ``tail_window`` — but ``tail_offset`` was computed
    # for ``tail_window``.  Using the smaller ``tail`` here would make
    # ``tail_offset + tl_local_end`` land at the WRONG byte offset in
    # ``merged`` and slice off legitimate content (real-world bug seen
    # on Indiana HB 1105 where the cut landed mid-Sec 14 instead of at
    # "(Reference is to HB 1105 as introduced.)" at end of chunk 1).
    tail_lines = tail_window.splitlines()
    head_lines = head_window.splitlines()
    # Index head lines by normalised content for O(1) lookup.
    head_index = {}
    head_running = 0
    head_line_offsets = []  # raw byte offsets of each head line in head_window
    for hl in head_lines:
        head_line_offsets.append(head_running)
        head_running += len(hl) + 1  # +1 for the newline

    for hl_idx, hl in enumerate(head_lines):
        norm = ' '.join(hl.split())
        if len(norm) >= 25:  # only index "substantial" lines
            head_index.setdefault(norm, hl_idx)

    # Walk tail lines from the bottom up; the LATEST match gives the
    # longest possible overlap.
    for tl_idx in range(len(tail_lines) - 1, -1, -1):
        tl = tail_lines[tl_idx]
        norm = ' '.join(tl.split())
        if len(norm) < 25:
            continue
        if norm in head_index:
            # Cut the head at the END of the matched line (so the line
            # itself is not duplicated).
            hl_idx = head_index[norm]
            cut_offset_in_head = head_line_offsets[hl_idx] + len(head_lines[hl_idx])
            # And cut merged at the END of the tail-side matched line.
            tl_local_end = sum(len(line) + 1 for line in tail_lines[: tl_idx + 1]) - 1
            global_tail_end = tail_offset + tl_local_end
            logging.info(
                f"   🔗 Chunk {chunk_index}: normalised line-match overlap "
                f"at tail line {tl_idx}, head line {hl_idx}"
            )
            # Defensive sanity check: the stitched result should never be
            # *shorter* than the larger of the two inputs.  If it is, the
            # cut was misaligned and we'd be silently dropping content.
            stitched_len = global_tail_end + 2 + (len(next_chunk) - cut_offset_in_head)
            min_expected = max(len(merged), len(next_chunk))
            if stitched_len < min_expected:
                logging.warning(
                    f"   ⚠️  Chunk {chunk_index}: line-match cut would shrink "
                    f"merged from {len(merged):,} to {stitched_len:,} "
                    f"(below max input {min_expected:,}). Falling back to "
                    f"concatenation to preserve content."
                )
                break  # fall through to Strategy 4
            # Use the version from `merged` (cleaner because earlier pipeline
            # state) and append the rest of next_chunk after the match.
            return merged[:global_tail_end].rstrip() + '\n\n' + next_chunk[cut_offset_in_head:].lstrip('\n'), False

    # ── Strategy 4: concatenate with separator (last resort) ────────────
    # When no overlap is found, the LLM likely truncated content at the
    # chunk boundary.  Both chunks may have corrupt tails/heads:
    #   merged tail:     "...courts of thi"       (mid-word)
    #   next_chunk head: "he safety of persons..." (mid-word)
    #
    # Fix: strip the last paragraph from merged and the first paragraph
    # from next_chunk if they look truncated (no terminal punctuation at
    # the end, or start mid-word without an opening tag).  This loses a
    # small amount of text at the boundary but avoids corrupt merges and
    # duplicate content.
    logging.warning(
        f"   ⚠️  Chunk {chunk_index}: no overlap found "
        f"(tail/head {search_len:,} chars each), concatenating with "
        f"truncation cleanup."
    )
    # Strip truncated tail of merged: if the last non-blank paragraph
    # does NOT end with sentence-terminal punctuation followed by a
    # close tag, it's likely truncated.
    _merged_stripped = merged.rstrip()
    _tail_paras = _merged_stripped.rsplit('\n\n', 1)
    if len(_tail_paras) == 2:
        _last_para = _tail_paras[1].rstrip()
        # Truncated if: doesn't end with <A] or <D] after terminal punct
        _ends_clean = bool(re.search(r'[.!?:;]\s*<[AD]\]\s*$', _last_para))
        _ends_section = bool(re.search(r'^Section\s+\d', _last_para))
        if not _ends_clean and not _ends_section:
            logging.info(
                f"   ✂️  Chunk {chunk_index}: trimming truncated tail paragraph "
                f"({len(_last_para)} chars): ...{_last_para[-60:]}"
            )
            _merged_stripped = _tail_paras[0].rstrip()

    # Strip truncated head of next_chunk: if the first non-blank paragraph
    # does NOT start with an open tag [A> or [D>, or starts mid-word,
    # it's the tail of a sentence that was cut at the chunk split.
    _head_stripped = next_chunk.lstrip()
    _head_paras = _head_stripped.split('\n\n', 1)
    if len(_head_paras) == 2:
        _first_para = _head_paras[0].strip()
        _starts_clean = (
            _first_para.startswith('[A>') or _first_para.startswith('[D>')
            or _first_para.startswith('Section ')
            or _first_para.startswith('(')
            or (len(_first_para) > 0 and _first_para[0].isupper())
        )
        # Extra guard: a tag like [A>he safety... still looks truncated —
        # the inner text starts with a lowercase non-subsection character,
        # meaning the original word was split mid-word (e.g. "the" → "he").
        if _starts_clean and re.match(r'\[[AD]>', _first_para):
            _inner = _first_para[3:]          # strip [A> or [D>
            if (_inner and _inner[0].islower()
                    and not re.match(r'^[a-z]\.\s', _inner)):
                _starts_clean = False
        if not _starts_clean:
            logging.info(
                f"   ✂️  Chunk {chunk_index}: trimming truncated head paragraph "
                f"({len(_first_para)} chars): {_first_para[:60]}..."
            )
            _head_stripped = _head_paras[1].lstrip()

    return _merged_stripped.rstrip() + '\n\n' + _head_stripped.lstrip(), True


def _dedupe_consecutive_duplicate_blocks(text, *, min_run_chars=200):
    """Remove consecutive duplicate paragraph runs from a merged document.

    Defence-in-depth pass that runs after ``_stitch_two_chunks``.  Finds
    cases where the chunk merge concatenated two chunks and the same
    paragraph (or run of paragraphs) was emitted by both — for example
    the IA SF 619 ``Sec. 85. DISASTER RECOVERY HOUSING ASSISTANCE FUND``
    block appearing twice when both adjacent chunks contain the section
    in their cleaned output.

    Algorithm
    ---------
    1. Split *text* on blank lines into paragraph blocks.
    2. For each candidate run length R (largest first), look for two
       consecutive runs ``paras[i:i+R]`` and ``paras[i+R:i+2R]`` that
       are equivalent after whitespace/case normalisation.
    3. If found AND the run is "substantial" (≥ ``min_run_chars`` total
       characters in normalised form), drop the second run.

    The minimum-character threshold prevents false positives on short
    repeated phrases like ``[A>NEW SECTION<A]`` (which legitimately
    repeats throughout a bill body).

    Parameters
    ----------
    text : str
        The merged document text.
    min_run_chars : int
        Minimum normalised-character length for a run to be considered
        a real duplicate.  Default 200 covers the IA SF 619 case
        (~700 chars) while protecting tiny repeated tokens.

    Returns
    -------
    str
        Text with duplicate paragraph runs removed.
    """
    # Split, keeping non-blank paragraph blocks.  We drop the original
    # separators because we'll rebuild with consistent ``\n\n`` joins.
    paras = [p for p in re.split(r'\n[ \t]*\n', text) if p.strip()]
    n = len(paras)
    if n < 2:
        return text

    # Strip formatting tags before normalising so the same content with
    # different tag structure (e.g. per-line [D>…<D] vs paragraph-level)
    # is treated as equivalent.  This catches overlap duplicates that
    # survived _stitch_two_chunks due to tag formatting differences.
    _tag_strip_re = re.compile(r'\[([AD])>|<([AD])\]')
    # Also normalise hyphen-space (PDF line breaks inside hyphenated
    # words produce "calendar- day" vs "calendar-day").
    def _norm(p):
        t = _tag_strip_re.sub('', p)
        t = re.sub(r'-\s+', '-', t)   # "calendar- day" → "calendar-day"
        return ' '.join(t.lower().split())
    norms = [_norm(p) for p in paras]
    keep = [True] * n

    max_run = min(50, n // 2)
    # Greedy from longest run length downward so a 5-paragraph duplicate
    # is matched as a single 5-run rather than multiple 1-runs.
    for run_len in range(max_run, 0, -1):
        i = 0
        while i + 2 * run_len <= n:
            # Skip windows that already contain a deletion mark.
            if not all(keep[j] for j in range(i, i + 2 * run_len)):
                i += 1
                continue
            run1 = norms[i : i + run_len]
            run2 = norms[i + run_len : i + 2 * run_len]
            if run1 == run2 and sum(len(p) for p in run1) >= min_run_chars:
                # Drop the second occurrence.
                for j in range(i + run_len, i + 2 * run_len):
                    keep[j] = False
                i += 2 * run_len
            else:
                i += 1

    # ── Near-consecutive dedup (gap ≤ 2 paragraphs) ─────────────────
    # When Strategy 4 concatenated chunks and the LLM truncated the
    # overlap, a corrupt stitch paragraph (e.g. "(5)...section. he
    # safety of persons...") sits between the first and second copy
    # of the overlapping content.  The strict consecutive check above
    # misses this because the gap breaks the run.  This pass allows a
    # gap of 1-2 paragraphs and removes both the gap and the second
    # copy of the run.
    for run_len in range(min(20, n // 3), 0, -1):
        i = 0
        while i + run_len < n:
            if not all(keep[j] for j in range(i, i + run_len)):
                i += 1
                continue
            run1 = norms[i : i + run_len]
            total_chars = sum(len(p) for p in run1)
            if total_chars < min_run_chars:
                i += 1
                continue
            # Try gaps of 1 and 2 paragraphs after run1
            for gap in (1, 2):
                j = i + run_len + gap
                if j + run_len > n:
                    continue
                if not all(keep[k] for k in range(j, j + run_len)):
                    continue
                run2 = norms[j : j + run_len]
                if run1 == run2:
                    # ── Guard: don't remove if the gap contains a section
                    # anchor.  In legislation, identical subsection text
                    # (e.g. "(a) File an application...") legitimately
                    # repeats across different numbered sections.  If the
                    # gap has "Sec. N" it's a new section, not a duplicate.
                    gap_raw = '\n'.join(
                        paras[k] for k in range(i + run_len, j)
                    )
                    if _RE_SECTION_ANCHOR.search('\n' + gap_raw):
                        i += 1
                        break  # skip this gap, try next i

                    # Also require higher char threshold for gapped dedup
                    # to avoid false positives on short repeated phrases
                    # common in legislation.
                    if total_chars < 500:
                        i += 1
                        break

                    # Drop the gap paragraphs and the second run
                    for k in range(i + run_len, j + run_len):
                        keep[k] = False
                    logging.info(
                        f"   🧹 Near-consecutive dedup: removed {run_len} "
                        f"duplicate paragraphs + {gap} gap paragraph(s) "
                        f"at positions {i+run_len}-{j+run_len-1}"
                    )
                    i = j + run_len
                    break
            else:
                i += 1

    # Also strip any stitch sentinels that may survive from older runs
    result_paras = [p for p, k in zip(paras, keep) if k]
    result_paras = [p for p in result_paras if p.strip() not in ('--- Chunk Stitch ---', '')]
    return '\n\n'.join(result_paras)


# ────────────────────────────────────────────────────────────────────────────
# Malformed close-tag normalisation
# ────────────────────────────────────────────────────────────────────────────
# Regex matches a mirrored opening bracket used as a CLOSER, e.g.:
#     "[A>does not<A>"      ← wrong: should be "<A]"
#     "[D>shall<D>"         ← wrong: should be "<D]"
# The rewrite condition uses a look-behind to confirm the bracket sits at
# the END of a tagged run (preceded by non-tag content) so we don't
# accidentally rewrite legitimate openers like "[A>".  In practice the
# look-behind ``(?<![\[A-Z])`` is sufficient: a real opener is preceded
# by ``[A`` or ``[D`` (the previous char is ``A`` or ``D``); a real
# closer is preceded by content (the previous char is anything else).
_RE_MALFORMED_A_CLOSE = re.compile(r'(?<![\[])<A>')
_RE_MALFORMED_D_CLOSE = re.compile(r'(?<![\[])<D>')


def _fix_malformed_close_tags(text):
    """Rewrite mirrored close-tags ``<A>`` / ``<D>`` to canonical ``<A]`` / ``<D]``.

    Background
    ----------
    The chunk-level LLM occasionally emits ``<A>`` or ``<D>`` (closing
    angle bracket reused) instead of the correct ``<A]`` / ``<D]``
    closer.  Example from a real Florida hybrid run::

        [A>does not<A> [D>shall<D]

    Why this matters
    ----------------
    ``remove_nested_tags`` walks the text counting opener / closer
    pairs to enforce a depth of 1.  It only recognises ``<A]`` /
    ``<D]`` as closers, so a malformed ``<A>`` is treated as plain
    text and the depth counter never decrements.  When the next
    legitimate ``[A>`` opener arrives, ``depth`` is still 1 — the
    function classifies it as nested and SILENTLY DELETES IT.  The
    matching ``<A]`` then survives unpaired, producing the
    user-visible symptom of an orphan closer with no opener::

        (5) Except as otherwise provided, a violation of subsection
        (1), subsection (2), subsection (3), or subsection (4):<A]

    Parameters
    ----------
    text : str
        Merged hybrid output, before structural tag passes.

    Returns
    -------
    str
        Same text with every ``<A>`` rewritten to ``<A]`` and every
        ``<D>`` rewritten to ``<D]`` (only at positions where the
        bracket is being used as a closer, never inside a real opener).
    """
    fixed_a = len(_RE_MALFORMED_A_CLOSE.findall(text))
    fixed_d = len(_RE_MALFORMED_D_CLOSE.findall(text))
    if fixed_a:
        text = _RE_MALFORMED_A_CLOSE.sub('<A]', text)
    if fixed_d:
        text = _RE_MALFORMED_D_CLOSE.sub('<D]', text)
    if fixed_a or fixed_d:
        logging.info(
            f"   🔧 Normalised malformed close-tags: "
            f"{fixed_a} <A> → <A] | {fixed_d} <D> → <D]"
        )
    return text


def fix_overlapping_cross_tags(text):
    """
    Fix overlapping tags between different types (A and D).

    When an [A> tag contains a [D>...<D] inside it (or vice versa),
    split them so they don't overlap:
        [A>added text[D>deleted text<D] more added<A]
    becomes:
        [A>added text<A][D>deleted text<D] [A>more added<A]

    Similarly for [D> containing [A>...<A].
    """
    result = []
    i = 0
    stack = []  # track which tag type is currently open: 'A' or 'D'

    while i < len(text):
        # Check for opening tags
        if text[i:i+3] == '[A>' or text[i:i+3] == '[D>':
            tag_type = text[i+1]  # 'A' or 'D'
            other = 'D' if tag_type == 'A' else 'A'

            if stack and stack[-1] == other:
                # We're inside the other type — close it first, then open this one
                result.append(f'<{other}]')
                stack.pop()
                result.append(f'[{tag_type}>')
                stack.append(tag_type)
                # Mark that we need to reopen the other type after this tag closes
                stack.append(f'reopen_{other}')
            else:
                result.append(text[i:i+3])
                stack.append(tag_type)
            i += 3

        # Check for closing tags
        elif text[i:i+3] == '<A]' or text[i:i+3] == '<D]':
            tag_type = text[i+1]  # 'A' or 'D'
            result.append(text[i:i+3])

            # Remove from stack
            # First check if there's a reopen marker above
            new_stack = []
            found = False
            reopen = None
            for s in reversed(stack):
                if not found and s == tag_type:
                    found = True
                    continue
                if not found and s.startswith('reopen_'):
                    reopen = s
                    continue
                new_stack.insert(0, s)
            stack = new_stack

            # If we had a reopen marker, reopen that tag type
            if reopen:
                reopen_type = reopen.split('_')[1]
                # Only reopen if there's content before the next close
                result.append(f' [{reopen_type}>')
                stack.append(reopen_type)
            i += 3
        else:
            result.append(text[i])
            i += 1

    return ''.join(result)


def restore_special_symbols(claude_text, pymupdf_text):
    """
    Restore special symbols (§, ¶, †, ‡, etc.) that Claude's vision may drop.

    Compares PyMuPDF-extracted text against Claude output and patches in
    missing symbols by finding matching context around each symbol.
    """
    import re as _re
    if not pymupdf_text or not claude_text:
        return claude_text

    # Symbols that Claude's vision commonly drops
    SYMBOLS = ['§', '¶', '†', '‡', '©', '®', '™', '°']

    # Strip tags from both for plain-text alignment
    tag_re = _re.compile(r'\[([AD])>|<([AD])\]')
    pymupdf_plain = tag_re.sub('', pymupdf_text)
    claude_plain = tag_re.sub('', claude_text)

    fixes = 0
    for sym in SYMBOLS:
        if sym not in pymupdf_plain:
            continue
        if claude_plain.count(sym) >= pymupdf_plain.count(sym):
            continue  # Claude has at least as many — nothing to restore

        # Find each occurrence of the symbol in PyMuPDF plain text
        for m in _re.finditer(_re.escape(sym), pymupdf_plain):
            pos = m.start()
            # Get surrounding context (without the symbol itself)
            left = pymupdf_plain[max(0, pos - 25):pos].strip()
            right = pymupdf_plain[pos + 1:pos + 26].strip()
            if len(left) < 3 and len(right) < 3:
                continue

            # Build pattern: left_context + optional_space + right_context
            # (Claude may have dropped the symbol, leaving adjacent text)
            left_esc = _re.escape(left[-15:]) if left else ''
            right_esc = _re.escape(right[:15]) if right else ''

            if left_esc and right_esc:
                # Look for left+space+right WITHOUT the symbol in Claude text
                pattern = f'({left_esc})\\s*({right_esc})'
                replacement = f'\\1 {sym} \\2'
                new_claude = _re.sub(pattern, replacement, claude_text, count=1)
                if new_claude != claude_text:
                    claude_text = new_claude
                    fixes += 1
                    # Clean up any double spaces
                    claude_text = claude_text.replace(f'  {sym}', f' {sym}').replace(f'{sym}  ', f'{sym} ')

    if fixes:
        logging.info(f"🔧 Restored {fixes} special symbol(s) from PyMuPDF reference")

    return claude_text


def _reformat_ia_signature_block(text: str, state_code: str | None = None) -> str:
    """Reformat Iowa signature block into proper two-column layout.

    Iowa enrolled bills have a two-column signature block:
        ______________________________      ______________________________
        PAT GRASSLEY                        AMY SINCLAIR
        Speaker of the House                President of the Senate

        I hereby certify that this bill originated in the House and is known as
        House File 472, Ninety-first General Assembly.

                                            ______________________________
                                            MEGHAN NELSON
                                            Chief Clerk of the House

        Approved _______________, 2025      ______________________________
                                             KIM REYNOLDS
                                             Governor

    Claude often outputs this as single-column (no indentation). This function
    detects and reformats it.
    """
    if not text:
        return text
    if state_code and state_code.upper() != "IA":
        return text

    # Column position for right-side elements
    COL2 = 40

    # Pattern: Speaker/President block on separate lines or same line without spacing
    # Look for the signature block by finding "Speaker of the House"
    lines = text.split('\n')
    result_lines = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # Detect signature line separator (______ ______) — already correct, pass through
        # Detect: NAME1 (spaces) NAME2 on one line (Speaker/President names)
        # e.g. "PAT GRASSLEY         AMY SINCLAIR"
        if (i + 1 < len(lines)
            and re.match(r'^[A-Z][A-Z .]+\s{2,}[A-Z][A-Z .]+\s*$', line)
            and ('Speaker' in lines[i + 1] or 'President' in lines[i + 1])):
            # Split into two names
            parts = re.split(r'\s{2,}', line.strip())
            if len(parts) == 2:
                name1 = parts[0].strip()
                name2 = parts[1].strip()
                result_lines.append(f"{name1:<{COL2}}{name2}")
                i += 1
                # Next line: titles
                if i < len(lines):
                    title_parts = re.split(r'\s{2,}', lines[i].strip())
                    if len(title_parts) == 2:
                        result_lines.append(f"{title_parts[0]:<{COL2}}{title_parts[1]}")
                    else:
                        result_lines.append(lines[i])
                    i += 1
                continue
            
        # Detect: "Speaker of the House" followed by "President of the Senate"
        # on separate lines (bad layout — should be on same line)
        if (re.match(r'^\s*Speaker of the House\s*$', line)
            and i + 1 < len(lines)
            and re.match(r'^\s*President of the Senate\s*$', lines[i + 1])):
            result_lines.append(f"{'Speaker of the House':<{COL2}}President of the Senate")
            i += 2
            # Also fix the names above if we haven't already
            continue

        # Detect: "MEGHAN NELSON" (or similar) at col 0 followed by "Chief Clerk"
        if (re.match(r'^[A-Z][A-Z ]+$', line.strip())
            and i + 1 < len(lines)
            and 'Chief Clerk' in lines[i + 1]
            and not line.startswith(' ' * 8)):
            # Indent to right column
            name = line.strip()
            result_lines.append(f"{'':<{COL2}}{name}")
            i += 1
            # Chief Clerk line
            result_lines.append(f"{'':<{COL2}}{lines[i].strip()}")
            i += 1
            # Add blank line after if not present
            if i < len(lines) and lines[i].strip():
                result_lines.append("")
            continue

        # Detect: underscore line before Chief Clerk (indent it)
        if (re.match(r'^\s*_{10,}\s*$', line)
            and i + 1 < len(lines)
            and re.match(r'^\s*[A-Z][A-Z ]+$', lines[i + 1].strip())
            and i + 2 < len(lines)
            and 'Chief Clerk' in lines[i + 2]
            and not line.startswith(' ' * 8)):
            result_lines.append(f"{'':<{COL2}}{'_' * 30}")
            i += 1
            continue

        # Detect: "KIM REYNOLDS" (or similar) at col 0 followed by "Governor"
        if (re.match(r'^[A-Z][A-Z ]+$', line.strip())
            and i + 1 < len(lines)
            and re.match(r'^\s*Governor\s*$', lines[i + 1])
            and not line.startswith(' ' * 8)):
            # Check if preceded by "Approved" line
            name = line.strip()
            result_lines.append(f"{'':<{COL2}} {name}")
            i += 1
            result_lines.append(f"{'':<{COL2}} {lines[i].strip()}")
            i += 1
            continue

        # Detect: "I hereby certify" without preceding blank line
        if 'I hereby certify' in line:
            # Ensure blank line before
            if result_lines and result_lines[-1].strip():
                result_lines.append("")
            result_lines.append(line)
            i += 1
            # Consume the rest of the certification paragraph.
            # Stop at blank line OR underscore line (next sig block element).
            while i < len(lines) and lines[i].strip() and not re.match(r'^\s*_{10,}', lines[i]):
                result_lines.append(lines[i])
                i += 1
            # Add blank separator
            result_lines.append("")
            if i < len(lines) and not lines[i].strip():
                i += 1  # skip existing blank line
            continue

        result_lines.append(line)
        i += 1

    return '\n'.join(result_lines)


def _ensure_blank_line_spacing(text):
    """Ensure every non-blank line is followed by a blank line.

    Converts single line breaks between content lines to double line breaks
    for consistent double-spaced output.  Already-blank lines are preserved
    without creating triple+ blank lines.

    Returns:
        str: Text with uniform blank-line spacing.
    """
    if not text:
        return text

    lines = text.split('\n')
    result = []
    for i, line in enumerate(lines):
        result.append(line)
        # If this is a non-blank line and the next line is also non-blank,
        # insert a blank line between them.
        if (line.strip()
                and i + 1 < len(lines)
                and lines[i + 1].strip()):
            result.append('')

    # Collapse any triple+ blank lines that may result
    text = '\n'.join(result)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text


def _restore_merged_lines(text, reference_text):
    """Restore line breaks the LLM incorrectly removed.

    Compares each output line against the reference (precleaned) text.
    If an output line appears to be two or more consecutive reference
    lines concatenated with whitespace, it is split back to match the
    reference layout.

    This is a *general* fix — it works for any table format, header
    block, or structured layout, not just specific patterns.

    Only "structured" lines are restored — the algorithm skips lines
    that look like intentional paragraph unwraps (prose sentences the
    LLM correctly joined from hard-wrapped source lines).
    """
    if not reference_text:
        return text

    ref_lines = reference_text.split('\n')
    # Build lookup: stripped line → list of original (index, line) pairs
    # Use stripped versions for matching (LLM may trim whitespace)
    ref_stripped = [l.rstrip() for l in ref_lines]

    # Build a set of consecutive-pair signatures for fast lookup.
    # Key = normalised merged form, Value = list of (line_a, line_b) originals
    # We normalise by collapsing internal whitespace runs to single space
    # for matching, since the LLM may change spacing when merging.
    def _norm(s):
        return re.sub(r'\s+', ' ', s.strip())

    pair_map = {}  # norm(a + " " + b) → [(a, b), ...]
    for i in range(len(ref_stripped) - 1):
        a = ref_stripped[i]
        b = ref_stripped[i + 1]
        if not a.strip() or not b.strip():
            continue  # skip pairs involving blank lines
        # Also build triples and quads for multi-line merges
        key2 = _norm(a + " " + b)
        pair_map.setdefault(key2, []).append((a, b))

    # Build triple map for 3-line merges
    triple_map = {}
    for i in range(len(ref_stripped) - 2):
        a, b, c = ref_stripped[i], ref_stripped[i+1], ref_stripped[i+2]
        if not a.strip() or not b.strip() or not c.strip():
            continue
        key3 = _norm(a + " " + b + " " + c)
        triple_map.setdefault(key3, []).append((a, b, c))

    # Build quad map for 4-line merges
    quad_map = {}
    for i in range(len(ref_stripped) - 3):
        a, b, c, d = ref_stripped[i], ref_stripped[i+1], ref_stripped[i+2], ref_stripped[i+3]
        if not a.strip() or not b.strip() or not c.strip() or not d.strip():
            continue
        key4 = _norm(a + " " + b + " " + c + " " + d)
        quad_map.setdefault(key4, []).append((a, b, c, d))

    # ── Helper: detect if a group of reference lines is just hard-wrapped
    # prose that the LLM correctly unwrapped into a single paragraph.
    # Structured lines (tables, headers, numbered items) should be split
    # back; prose continuation lines should stay merged.
    _RE_STRUCTURED = re.compile(
        r'^\s*(?:'
        r'\d+[\.\)]\s'               # numbered list: "1. " or "2) "
        r'|[a-z][\.\)]\s'            # lettered list: "a. " or "b) "
        r'|\([a-z0-9]+\)\s'          # parenthesised: "(a) " or "(1) "
        r'|\[A>'                     # tag start
        r'|---'                      # separator / page break
        r'|SR \d|HB \d|SB \d'       # bill/resolution headers
        r'|Number of members'        # table header
        r')',
        re.IGNORECASE,
    )
    # Lines that look like table data (multiple large whitespace gaps)
    _RE_TABLE_DATA = re.compile(r'\S\s{3,}\S')

    def _is_structured(line):
        """True if a line looks like a structured/tabular element."""
        s = line.strip()
        if not s:
            return False
        if _RE_STRUCTURED.match(s):
            return True
        if _RE_TABLE_DATA.search(s):
            return True
        return False

    def _is_prose_wrap(lines_tuple):
        """True if the reference lines are just hard-wrapped prose continuation.

        Heuristic: if NONE of the lines are structured (tags, numbered items,
        table data), they're just hard-wrapped prose the LLM correctly merged.
        If ANY line is structured, return False so the split is applied.
        """
        if len(lines_tuple) < 2:
            return False
        # If ANY line in the group is structured, it's NOT simple prose wrap
        for ln in lines_tuple:
            if _is_structured(ln):
                return False  # structured → DO split
        # All lines are plain prose continuations → LLM was right to merge
        return True

    out_lines = text.split('\n')
    result = []
    splits_done = 0

    for line in out_lines:
        norm_line = _norm(line)

        # Skip short lines or blank lines — no merge possible
        if len(norm_line) < 20:
            result.append(line)
            continue

        # Try quad split first (most specific)
        if norm_line in quad_map:
            parts = quad_map[norm_line][0]
            if _is_prose_wrap(parts):
                result.append(line)  # prose — keep merged
            else:
                result.extend(parts)
                splits_done += 1
            continue

        # Try triple split
        if norm_line in triple_map:
            parts = triple_map[norm_line][0]
            if _is_prose_wrap(parts):
                result.append(line)
            else:
                result.extend(parts)
                splits_done += 1
            continue

        # Try pair split
        if norm_line in pair_map:
            parts = pair_map[norm_line][0]
            if _is_prose_wrap(parts):
                result.append(line)
            else:
                result.extend(parts)
                splits_done += 1
            continue

        result.append(line)

    if splits_done:
        logging.info(
            f"   🔧 Restored {splits_done} line-merge(s) using precleaned reference"
        )

    return '\n'.join(result)


def deterministic_cleanup(text, state_config=None, state_code=None, reference_text=None):
    """
    Apply deterministic fixes for common Claude output inconsistencies.
    These are formatting errors that Claude makes intermittently regardless
    of the prompt — catching them here ensures 100% consistency.

    When *state_config* is provided and bold/italic are enabled in add_markup,
    markdown **bold** / *italic* are converted to [A>...<A] tags instead of
    being stripped to plain text.

    Fixes applied:
      1. Strip markdown code fences (```plaintext ... ```)
      2. Remove markdown bold/italic asterisks that slip through (or convert to [A> tags)
      3. Fix malformed tag variants ([D>text<D>, [A>text<A>, etc.)
      4. Fix empty tags ([A><A], [D><D])
      5. Fix nested/overlapping tags
      6. Fix smart quotes → regular quotes
      7. Normalize whitespace inside tags (leading/trailing spaces)
      8. Fix mismatched tag pairs (orphaned openers/closers)

    Args:
        text (str): Raw converted text from Claude

    Returns:
        str: Cleaned text with deterministic fixes applied
    """
    if not text:
        return text

    original = text
    fix_log = []

    # ── Fix 1: Strip markdown code fences ─────────────────────────
    # Claude sometimes wraps output in ```plaintext ... ``` despite instructions
    if text.strip().startswith('```'):
        lines = text.strip().split('\n')
        # Remove first line if it's a code fence
        if lines[0].strip().startswith('```'):
            lines = lines[1:]
        # Remove last line if it's a code fence
        if lines and lines[-1].strip() == '```':
            lines = lines[:-1]
        text = '\n'.join(lines)
        if text != original:
            fix_log.append("Stripped markdown code fences")

    # ── Fix 2: Remove markdown asterisk formatting ────────────────
    # Claude sometimes outputs *italic* or **bold** instead of [A>...<A]
    # Only fix asterisks that are NOT inside existing tags
    
    # First, protect existing tags by replacing them with placeholders
    tag_placeholder_map = {}
    tag_counter = [0]
    def _protect_tag(m):
        key = f"__TAG_PLACEHOLDER_{tag_counter[0]}__"
        tag_placeholder_map[key] = m.group(0)
        tag_counter[0] += 1
        return key
    
    protected = re.sub(r'\[A>.*?<A\]|\[D>.*?<D\]', _protect_tag, text, flags=re.DOTALL)
    
    # Determine whether bold / italic should be converted to [A> tags or stripped
    add_cfg = (state_config or {}).get("add_markup", {})
    bold_enabled = bool(add_cfg.get("bold", False))
    italic_enabled = bool(add_cfg.get("italic", False))

    # Fix **bold** markdown — use innermost-pair pattern that won't match
    # across tag placeholders (handles Claude's unbalanced ** markers)
    bold_pattern = r'\*\*((?:(?!\*\*)(?!__TAG_PLACEHOLDER_).)+?)\*\*'
    bold_count = 0
    while True:
        if bold_enabled:
            new_p, n = re.subn(bold_pattern, r'[A>\1<A]', protected, flags=re.DOTALL)
        else:
            new_p, n = re.subn(bold_pattern, r'\1', protected, flags=re.DOTALL)
        if n == 0:
            break
        bold_count += n
        protected = new_p
    if bold_count > 0:
        if bold_enabled:
            fix_log.append(f"Converted {bold_count} markdown bold (**) → [A>...<A] tags")
        else:
            fix_log.append(f"Removed {bold_count} markdown bold (**) instances")

    # Strip any remaining orphaned ** markers (unbalanced bold)
    orphaned_bold = protected.count('**')
    if orphaned_bold > 0:
        protected = protected.replace('**', '')
        fix_log.append(f"Stripped {orphaned_bold} orphaned ** markers")

    # Fix *italic* markdown → [A>...<A] if italic enabled, else plain text
    italic_pattern = r'(?<!\*)\*(?!\*)([^*]+)(?<!\*)\*(?!\*)'
    italic_count = len(re.findall(italic_pattern, protected))
    if italic_count > 0:
        if italic_enabled:
            protected = re.sub(italic_pattern, r'[A>\1<A]', protected)
            fix_log.append(f"Converted {italic_count} markdown italic (*) → [A>...<A] tags")
        else:
            protected = re.sub(italic_pattern, r'\1', protected)
            fix_log.append(f"Removed {italic_count} markdown italic (*) instances")
    
    # Restore tags from placeholders
    for key, value in tag_placeholder_map.items():
        protected = protected.replace(key, value)
    text = protected

    # ── Fix 3: Fix malformed tag variants ─────────────────────────
    # [D>text<D>  → [D>text<D]  (wrong closing bracket)
    malformed_d_close = len(re.findall(r'<D>', text))
    if malformed_d_close > 0:
        text = text.replace('<D>', '<D]')
        fix_log.append(f"Fixed {malformed_d_close} malformed <D> → <D]")
    
    # [A>text<A>  → [A>text<A]  (wrong closing bracket)
    malformed_a_close = len(re.findall(r'<A>', text))
    if malformed_a_close > 0:
        text = text.replace('<A>', '<A]')
        fix_log.append(f"Fixed {malformed_a_close} malformed <A> → <A]")

    # <D>text[D>  → mirrored tags (very rare but happens)
    # [D]text<D>  → [D>text<D]
    text = re.sub(r'\[D\]', '[D>', text)
    text = re.sub(r'\[A\]', '[A>', text)

    # ── Fix 4: Remove empty tags ──────────────────────────────────
    empty_a = len(re.findall(r'\[A>\s*<A\]', text))
    empty_d = len(re.findall(r'\[D>\s*<D\]', text))
    if empty_a > 0:
        text = re.sub(r'\[A>\s*<A\]', '', text)
        fix_log.append(f"Removed {empty_a} empty [A><A] tags")
    if empty_d > 0:
        text = re.sub(r'\[D>\s*<D\]', '', text)
        fix_log.append(f"Removed {empty_d} empty [D><D] tags")

    # ── Fix 5: Fix smart quotes → regular quotes ─────────────────
    smart_quote_count = len(re.findall(r'[\u201c\u201d\u2018\u2019\u2032\u2033]', text))
    if smart_quote_count > 0:
        text = text.replace('\u201c', '"').replace('\u201d', '"')   # " "
        text = text.replace('\u2018', "'").replace('\u2019', "'")   # ' '
        text = text.replace('\u2032', "'").replace('\u2033', '"')   # ′ ″
        fix_log.append(f"Fixed {smart_quote_count} smart quotes → regular quotes")

    # ── Fix 6: Normalize whitespace inside tags ───────────────────
    # Trim trailing spaces before close tags: "text <A]" → "text<A]"
    # Trim leading spaces only when followed by more spaces (excess indent):
    #   [A>  text<A] → [A> text<A] — but preserve single leading space like [D> if<D]
    # The single leading space is meaningful — it represents whitespace in the PDF.
    text = re.sub(r'\[A> {2,}', '[A> ', text)   # collapse 2+ leading spaces to 1
    text = re.sub(r' +<A\]', '<A]', text)        # trim trailing spaces before close
    text = re.sub(r'\[D> {2,}', '[D> ', text)   # collapse 2+ leading spaces to 1
    text = re.sub(r' +<D\]', '<D]', text)        # trim trailing spaces before close

    # ── Fix 7: Merge adjacent same-type tags ──────────────────────
    # [A>text1<A] [A>text2<A] → [A>text1 text2<A]  (if only spaces/tabs between, NOT newlines)
    # Using [^\S\n]{0,5} instead of [^\S\n]* to avoid merging tags
    # that are separated by ≥ 6 horizontal spaces.  A gap of 6+ spaces
    # signals a tabular column boundary (COLUMN_GAP_MIN_SPACES = 6 in
    # pymupdf_extractor), and merging those tags destroys the multi-
    # column layout of DE Grants-in-Aid and IN pension tables.  The
    # {0,5} threshold mirrors ``consolidate_adjacent_tags`` Phase 1a
    # which also uses ``[ \t]{1,5}``.
    merge_a = len(re.findall(r'<A\][^\S\n]{0,5}\[A>', text))
    merge_d = len(re.findall(r'<D\][^\S\n]{0,5}\[D>', text))
    if merge_a > 0:
        text = re.sub(r'<A\][^\S\n]{0,5}\[A>', ' ', text)
        fix_log.append(f"Merged {merge_a} adjacent [A>...<A] tag pairs")
    if merge_d > 0:
        text = re.sub(r'<D\][^\S\n]{0,5}\[D>', ' ', text)
        fix_log.append(f"Merged {merge_d} adjacent [D>...<D] tag pairs")

    # ── Fix 8: Fix orphaned/mismatched tags ───────────────────────
    # Count openers vs closers
    open_a = len(re.findall(r'\[A>', text))
    close_a = len(re.findall(r'<A\]', text))
    open_d = len(re.findall(r'\[D>', text))
    close_d = len(re.findall(r'<D\]', text))

    if open_a != close_a:
        fix_log.append(f"⚠️  Mismatched [A> tags: {open_a} openers vs {close_a} closers")
        # Remove orphaned closers (no preceding opener)
        if close_a > open_a:
            # Find closers without openers and remove them
            diff = close_a - open_a
            for _ in range(diff):
                # Find first <A] that doesn't have a matching [A> before it
                text = re.sub(r'^([^\[]*?)<A\]', r'\1', text, count=1)
            fix_log.append(f"  Removed {diff} orphaned <A] closer(s)")
        elif open_a > close_a:
            diff = open_a - close_a
            # Remove orphaned openers from the end
            for _ in range(diff):
                text = re.sub(r'\[A>([^<]*?)$', r'\1', text, count=1)
            fix_log.append(f"  Removed {diff} orphaned [A> opener(s)")

    if open_d != close_d:
        fix_log.append(f"⚠️  Mismatched [D> tags: {open_d} openers vs {close_d} closers")
        if close_d > open_d:
            diff = close_d - open_d
            for _ in range(diff):
                text = re.sub(r'^([^\[]*?)<D\]', r'\1', text, count=1)
            fix_log.append(f"  Removed {diff} orphaned <D] closer(s)")
        elif open_d > close_d:
            diff = open_d - close_d
            for _ in range(diff):
                text = re.sub(r'\[D>([^<]*?)$', r'\1', text, count=1)
            fix_log.append(f"  Removed {diff} orphaned [D> opener(s)")

    # Log summary
    if fix_log:
        logging.info(f"🔧 Deterministic cleanup applied {len(fix_log)} fix(es):")
        for msg in fix_log:
            logging.info(f"   • {msg}")
    else:
        logging.info("✅ Deterministic cleanup: no fixes needed")

    # ── Fix 9: Ensure blank line between DIVISION headers and subtitles ──
    # The LLM often collapses "DIVISION X\nSUBTITLE" onto adjacent lines
    # without the expected blank separator.  Insert one when the line
    # immediately after a DIVISION header is an ALL-CAPS subtitle (not a
    # "Sec." / "Section" line).
    _div_hdr_re = re.compile(
        r'^(DIVISION\s+[IVXLCDM]+(?:\s*[-\u2014\u2013]+\s*\S.*)?)$\n'  # DIVISION line
        r'(?!\n)'                                                        # NOT already followed by blank
        r'([A-Z][A-Z \u2014\u2013\'&/.,;:\-\(\)]+\S)$',                # ALL-CAPS subtitle (incl. apostrophe, &, /)
        re.MULTILINE,
    )
    div_fixed = _div_hdr_re.sub(r'\1\n\n\2', text)
    if div_fixed != text:
        n_div = len(_div_hdr_re.findall(text))
        fix_log.append(f"Inserted blank line after {n_div} DIVISION header(s)")
        text = div_fixed

    # ── Fix 10: Normalize enrolled-bill signature block ──────────────
    # PyMuPDF merges the "Approved ___, YYYY" date and the Governor's
    # signature line onto one text line because they share the same
    # vertical position in the PDF.  Split them so the signature line
    # sits above the Governor's name (matching the visual layout and
    # the format of other signature blocks in the document).
    _sig_block_re = re.compile(
        r'^(Approved\s+_{3,},\s*\d{4})\s+(_{10,})$',
        re.MULTILINE,
    )
    sig_fixed = _sig_block_re.sub(r'\1\n\n\2', text)
    if sig_fixed != text:
        fix_log.append("Split Governor signature line from Approved date line")
        text = sig_fixed

    # ── Fix 11: CA vote fraction — ² outside [D> tag ────────────────
    # PyMuPDF renders the "⅔" fraction as separate Unicode chars
    # (² = U+00B2, ⁄ = U+2044, 3) and splits them across a tag
    # boundary: "²[D>⁄3<D]".  Move the superscript digit inside the
    # delete tag so the full fraction is tagged correctly: "[D>²⁄3<D]".
    _frac_re = re.compile(r'([²³⁴⁵⁶⁷⁸⁹])\s*\[D>(⁄\d+)')
    frac_fixed = _frac_re.sub(r'[D>\1\2', text)
    if frac_fixed != text:
        fix_log.append("Moved superscript digit inside [D> tag for vote fraction")
        text = frac_fixed

    # ── Fix 12: ALL-CAPS-only states — strip [A>] from non-CAPS text ──
    # When a state uses `all_caps: true` as the ONLY addition marker
    # (underline/bold/italic are all false), the vision model sometimes
    # still tags bold section headers (mixed-case) with [A>...<A].
    # Two-pass fix:
    #   Pass A — Purely non-CAPS tags: strip [A>...<A] entirely.
    #   Pass B — Mixed tags (CAPS body + bold header in one tag):
    #            split at the CAPS/non-CAPS boundary so the CAPS
    #            portion stays tagged and the bold header becomes
    #            plain text.
    add_cfg = (state_config or {}).get("add_markup", {})
    if (add_cfg.get("all_caps")
            and not add_cfg.get("underline")
            and not add_cfg.get("bold")
            and not add_cfg.get("italic")):

        def _word_is_caps(w: str) -> bool:
            """True if the word's alphabetic chars are all uppercase."""
            letters = [c for c in w if c.isalpha()]
            return len(letters) > 0 and all(c.isupper() for c in letters)

        def _word_is_neutral(w: str) -> bool:
            """True if word has no letters (numbers, punctuation, parens)."""
            return not any(c.isalpha() for c in w)

        # Subsection label pattern: standalone (a), (1), (I) OR compound
        # cross-references like (1)(a)(I), (1)(a)(I);, (2)(c)(II), etc.
        _subsec_label_re = re.compile(
            r'^\([a-z]+\)$'                           # (a), (bb)
            r'|^\(\d+(?:\.\d+)?\)$'                   # (1), (2.5)
            r'|^\([IVXLC]+\)$'                        # (I), (II)
            r'|^(?:\([a-zA-Z0-9.]+\)){2,}[;,.:]*$'   # compound: (1)(a)(I), (1)(a)(I);
        )

        def _is_alphanumeric_ref(w: str) -> bool:
            """True if word is an alphanumeric reference, not real text.

            Catches statutory section numbers (2000d, 1232g), mixed
            references ((a.1), 4th), or any token that contains at
            least one digit and at most 3 lowercase letters.  Real
            words like 'Investigation', 'and', 'procedures' have many
            more lowercase letters and no digits, so they stay 'L'.
            """
            lower_count = sum(1 for c in w if c.islower())
            has_digit = any(c.isdigit() for c in w)
            return has_digit and lower_count <= 3

        # Roman numeral pattern for false-positive detection.
        # Bare Roman numerals like "VI" in "Title VI" are section/title
        # references, NOT ALL-CAPS new-text headings.
        _roman_numeral_re = re.compile(
            r'^(?=[IVXLCDM])M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$'
        )

        def _split_caps_tag(m):
            """Split a single [A>...<A] tag at CAPS/non-CAPS boundaries.

            Returns:
              - The original tag unchanged if ALL content is CAPS.
              - Plain text if NO content is CAPS.
              - A mix of [A>CAPS<A] and plain-text segments when the
                tag contains both (e.g. bold header + CAPS body).
            """
            content = m.group(1)
            words = content.split()
            if not words:
                return content

            # Classify each word:
            #   'C' = caps  (all letters uppercase)
            #   'L' = lowercase/mixed  (has lowercase letters)
            #   'N' = neutral  (no letters, or subsection label)
            classes = []
            for w in words:
                if _subsec_label_re.match(w) or _is_alphanumeric_ref(w):
                    classes.append('N')  # subsection / alphanumeric ref → neutral
                elif _word_is_caps(w):
                    classes.append('C')
                elif _word_is_neutral(w):
                    classes.append('N')
                else:
                    classes.append('L')

            # If no CAPS words at all:
            if 'C' not in classes:
                # ALL neutral (subsection labels, numbers, refs) — keep
                # the tag.  merge_split_tags runs later and will merge
                # it into the adjacent CAPS tag on the same line.
                # e.g. [A>(4)<A] [A>(c)<A] [A>ON JULY 1,...<A] → merged
                if 'L' not in classes:
                    return m.group(0)      # keep tag
                # Has real lowercase words → strip entire tag
                return content

            # If no lowercase words at all → keep entire tag
            # UNLESS every CAPS word is a bare Roman numeral — that's a
            # false positive from "Title VI", "Article IX", etc.
            if 'L' not in classes:
                caps_words = [w for w, c in zip(words, classes) if c == 'C']
                if caps_words and all(_roman_numeral_re.match(w) for w in caps_words):
                    return content          # strip tag – bare Roman numeral
                return m.group(0)

            # Mixed: split into runs.  Neutral words attach to the
            # CAPS context when adjacent to CAPS, otherwise to lowercase.
            # Resolve neutrals: assign each N to C or L based on neighbors.
            # IMPORTANT: Leading neutrals before CAPS (e.g. "(a)" before
            # "UPON RECEIVING...") should be tagged WITH the CAPS block
            # because CO rules say "include the subsection label inside
            # the [A> tag" when the entire subsection is new.
            resolved = list(classes)
            for i, c in enumerate(resolved):
                if c != 'N':
                    continue
                # Look left and right for nearest non-N
                left = next((resolved[j] for j in range(i - 1, -1, -1) if resolved[j] != 'N'), None)
                right = next((resolved[j] for j in range(i + 1, len(resolved)) if resolved[j] != 'N'), None)
                # Prefer CAPS context — if EITHER neighbor is CAPS,
                # attach the neutral to CAPS.  This ensures leading
                # subsection labels "(a)", "(4)" etc. are included in
                # the CAPS tag when followed by ALL-CAPS body text.
                if left == 'C' or right == 'C':
                    resolved[i] = 'C'
                else:
                    resolved[i] = 'L'

            # Special case: leading neutrals (subsection labels) before
            # a lowercase run that is followed by CAPS.  Pattern:
            #   N L L L C C C ...  →  the leading N should become C
            # (e.g. "(4) Investigation and procedures. IF THE RESPONDENT...")
            # The N was assigned L because its right neighbor is L.
            # But since there IS caps content later, the subsection label
            # is part of the new addition and should get its own tag.
            if resolved and resolved[0] == 'L' and _subsec_label_re.match(words[0]):
                if 'C' in resolved:
                    resolved[0] = 'C'

            # Build output segments
            segments = []
            run_start = 0
            for i in range(1, len(resolved) + 1):
                if i < len(resolved) and resolved[i] == resolved[run_start]:
                    continue
                run_type = resolved[run_start]
                run_words = words[run_start:i]
                run_text = ' '.join(run_words)
                if run_type == 'C':
                    # Guard: bare Roman numeral runs are false positives
                    if all(_roman_numeral_re.match(rw) for rw in run_words):
                        segments.append(run_text)
                    else:
                        segments.append(f'[A>{run_text}<A]')
                else:
                    segments.append(run_text)
                run_start = i

            return ' '.join(segments)

        before_caps = text
        text = re.sub(r'\[A>(.*?)<A\]', _split_caps_tag, text, flags=re.DOTALL)
        if text != before_caps:
            n_orig = before_caps.count('[A>')
            n_now = text.count('[A>')
            n_stripped = n_orig - n_now
            if n_stripped > 0:
                fix_log.append(f"ALL-CAPS enforcement: stripped/split {n_stripped} non-CAPS [A>] tag(s)")
            elif n_now > n_orig:
                fix_log.append(f"ALL-CAPS enforcement: split mixed tags into {n_now - n_orig + n_orig} CAPS-only segments")
            logging.info(f"🔧 ALL-CAPS tag enforcement: {n_orig} → {n_now} [A>] tags")

    # ── Final: Fix cross-type overlaps, remove nested tags, merge mid-sentence splits, then split at subsection boundaries ──
    text = fix_overlapping_cross_tags(text)
    text = remove_nested_tags(text)
    text = merge_split_tags(text)
    text = split_tagged_subsections(text)

    # ── Absolute last step: split tabular tags at column boundaries ──
    # DE-specific: re-split multi-row merged tags, then split single
    # Label $ Amount tags into separate per-cell tags.
    # _split_merged_grant_tag_rows must run AFTER merge_split_tags
    # (which re-merges tags across lines) so the split survives.
    # _split_tabular_tag_at_dollar then handles remaining single-dollar
    # "Label $ Amount" tags (e.g. [A>TOTAL - Secretary $ 3,880,543<A]).
    # Scoped to DE + IN so prose tags in other states are never split.
    if _HAS_PYMUPDF and state_code and state_code.upper() in ("DE", "IN"):
        text = _split_merged_grant_tag_rows(text)
        text = _split_tabular_header_from_data(text)
        text = _split_tabular_tag_at_dollar(text)

    # ── Fix: Restore lines the LLM incorrectly merged ──────────
    # Instead of pattern-matching specific table formats, compare against
    # the precleaned reference text.  If an output line is a concatenation
    # of 2+ consecutive reference lines, split it back.
    if reference_text:
        text = _restore_merged_lines(text, reference_text)

    # ── Final: collapse double/triple spaces between words ────────
    # Preserves 4+ space gaps (intentional table/column alignment)
    text = re.sub(r'(?<=\S) {2,3}(?=\S)', ' ', text)
    # Rejoin line-break hyphenations: "export- limiting" → "export-limiting"
    text = re.sub(r'(\w)- (\w)', r'\1-\2', text)

    return text


# ── remove_nested_tags, merge_split_tags, split_tagged_subsections ────
# Canonical versions live in pymupdf_extractor.py and are imported above.
# Full definitions are kept here as fallback when pymupdf_extractor is
# not available (e.g. standalone CLI usage without the app/ directory).
if not _HAS_PYMUPDF:

    def remove_nested_tags(text):
        """Remove nested/duplicate opening tags within the same tag type."""
        for tag_type in ['A', 'D']:
            open_tag = '[' + tag_type + '>'
            close_tag = '<' + tag_type + ']'
            changed = True
            while changed:
                changed = False
                result = []
                depth = 0
                i = 0
                while i < len(text):
                    if text[i:i+len(open_tag)] == open_tag:
                        if depth == 0:
                            result.append(open_tag)
                            depth = 1
                        else:
                            changed = True
                        i += len(open_tag)
                    elif text[i:i+len(close_tag)] == close_tag:
                        result.append(close_tag)
                        depth = max(0, depth - 1)
                        i += len(close_tag)
                    else:
                        result.append(text[i])
                        i += 1
                text = ''.join(result)
        return text

    def merge_split_tags(text):
        """Merge adjacent same-type tags split mid-sentence.

        Handles both ``<D][D>`` (no space) and ``<D] [D>`` (space-separated)
        patterns.  The space-separated variant is the most common LLM output
        format and must be handled so that overlap detection during chunk
        merging sees identical strings for the same content.
        """
        for tag_type in ['A', 'D']:
            _no_sub = (
                r'(?!\s*\(\d+\))'
                r'(?!\s*\([A-Z]+\))'
                r'(?!\s*\([a-z]+\))'
                r'(?!\s*[a-z]\.\d*\.?\s)'
                r'(?!\s*\d+\.\s)'
            )
            # Inline continuation: merge adjacent same-type tags on the
            # same line.  The ``([ \t]*)`` group captures any horizontal
            # whitespace between ``<D]`` and ``[D>`` so it is preserved
            # in the replacement — this keeps word boundaries intact for
            # the common ``<D] [D>`` (space-separated) pattern.
            inline_cont = r'<' + tag_type + r'\]([ \t]*)\[' + tag_type + r'>' + _no_sub
            text = re.sub(inline_cont, r'\1', text)
            inline_sub = r'<' + tag_type + r'\][ \t]*\[' + tag_type + r'>'
            text = re.sub(inline_sub, '<' + tag_type + ']\n\n[' + tag_type + '>', text)
            same_cont = r'\n<' + tag_type + r'\][ \t]*\[' + tag_type + r'>' + _no_sub
            text = re.sub(same_cont, ' ', text)
            same_sub = r'\n<' + tag_type + r'\][ \t]*\[' + tag_type + r'>'
            text = re.sub(same_sub, '<' + tag_type + ']\n\n[' + tag_type + '>', text)
            # Cross-line: cap indent at 15 chars so column-aligned table
            # cells (e.g. FL 921.0022 description column at offset 66)
            # are NOT merged across visual lines.  Mirrors the canonical
            # implementation in pymupdf_extractor.py.
            cross_line = (
                r'<' + tag_type + r'\]'
                r'([ \t]*\n[ \t]{0,15})'
                r'\[' + tag_type + r'>' + _no_sub
            )
            text = re.sub(cross_line, ' ', text)
        return text

    def split_tagged_subsections(text):
        """Split long tagged blocks at subsection markers."""
        subsection_re = re.compile(
            r'(?:(?<=[.:;])|(?<=\band\b)|(?<=\bor\b))\s*'
            r'('
            r'\(\d+\)[a-z]\.\d*\.?'
            r'|\(\d+\)|\([A-Z]+\)|\([a-z]+\)'
            r'|[a-z]\.\d*\.?'
            r'|\d+\.'
            r')(?:\s|$)',
            re.IGNORECASE,
        )
        _nosplit_re = re.compile(
            r'(?:^|\n)\s*(?:Sec|Section|Art|Article)\.\s*$'
            r'|(?<!\$)(?<!\d)(?:\d{2,}\.\d*\.?\s*)$',
            re.IGNORECASE,
        )

        def _is_inline_list(content, markers):
            if not markers:
                return False
            list_sep_count = 0
            period_count = 0
            for m in markers:
                preceding = content[:m.start()].rstrip()
                if preceding.endswith(';'):
                    list_sep_count += 1
                elif preceding.endswith(('.', ':')):
                    period_count += 1
                else:
                    list_sep_count += 1
            return list_sep_count > period_count

        def _split_block(match):
            tag_open = match.group(1)
            content = match.group(2)
            tag_close = match.group(3)
            markers = list(subsection_re.finditer(content))
            if len(markers) < 1:
                return match.group(0)
            markers = [m for m in markers
                       if not _nosplit_re.search(content[:m.start()])]
            if len(markers) < 1:
                return match.group(0)
            if _is_inline_list(content, markers):
                return match.group(0)
            pieces = []
            prev_end = 0
            for m in markers:
                split_at = m.start()
                before = content[prev_end:split_at].rstrip()
                if before:
                    pieces.append(before)
                prev_end = split_at
            last = content[prev_end:].strip()
            if last:
                pieces.append(last)
            tagged_pieces = [f"{tag_open}{p.strip()}{tag_close}" for p in pieces]
            return '\n\n'.join(tagged_pieces)

        text = re.sub(r'(\[A>)(.*?)(<A\])', _split_block, text, flags=re.DOTALL)
        text = re.sub(r'(\[D>)(.*?)(<D\])', _split_block, text, flags=re.DOTALL)
        return text


def post_process_output(text, state_config):
    """
    Apply deterministic post-processing fixes to converted text based on state rules.
    Strips [A>...<A] and [D>...<D] tags from specific clauses that should never be tagged.

    Args:
        text (str): Converted text from Claude
        state_config (dict): State config from netscan_state_template.json, or None

    Returns:
        str: Cleaned text with tags removed from protected clauses
    """
    if not text or not state_config:
        return text

    no_tag_clauses = state_config.get("no_tag_clauses", [])
    if not no_tag_clauses:
        return text

    state_name = state_config.get('state_name', 'Unknown')
    fix_count = 0

    # Regex fragment that matches any [A>, <A], [D>, or <D] tag (with optional surrounding whitespace)
    TAG_RE = r'(?:\[A>|<A\]|\[D>|<D\])'
    # Opening-only / closing-only fragments — the leading match should only
    # consume *opening* tags (the clause "starts" inside a tagged span)
    # and the trailing match should only consume *closing* tags (the clause
    # "ends" the span).  Previously both used TAG_RE which matches ALL
    # four tokens; this caused the trailing `(?:\s*TAG_RE)*` to greedily
    # eat the NEXT paragraph's `[A>` opener, orphaning its `<A]` closer.
    TAG_OPEN  = r'(?:\[A>|\[D>)'
    TAG_CLOSE = r'(?:<A\]|<D\])'

    for clause in no_tag_clauses:
        # Build a regex that matches the clause text even if it has [A>/<A]/[D>/<D] tags
        # scattered within it. E.g. "[A>The people of the State<A] of California..."
        # Strategy: escape the clause, then allow optional tags around/between words
        escaped = re.escape(clause)
        # Allow any combination of add/delete tags (with optional whitespace) between words
        pattern = escaped.replace(r'\ ', rf'\s*(?:{TAG_RE}\s*)*')
        # Leading: only opening tags; trailing: only closing tags.
        full_pattern = rf'(?:{TAG_OPEN}\s*)*' + pattern + rf'(?:\s*{TAG_CLOSE})*'

        def strip_tags(match):
            """Remove all [A>, <A], [D>, <D] tags from the matched text."""
            matched = match.group(0)
            cleaned = matched.replace('[A>', '').replace('<A]', '').replace('[D>', '').replace('<D]', '')
            return cleaned

        new_text = re.sub(full_pattern, strip_tags, text, flags=re.IGNORECASE)
        if new_text != text:
            occurrences = len(re.findall(full_pattern, text, flags=re.IGNORECASE))
            fix_count += occurrences
            logging.info(f"🔧 Stripped tags from protected clause '{clause}' ({occurrences} occurrence(s))")
            text = new_text

    if fix_count > 0:
        logging.info(f"🔧 Post-processing for {state_name}: stripped tags from {fix_count} protected clause(s)")
    else:
        logging.info(f"✅ Post-processing for {state_name}: no tag cleanup needed")

    return text


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
 
    # Check for --reconcile, --self-correct, and --hybrid flags
    args = [a for a in sys.argv[1:] if a not in ('--reconcile', '--self-correct', '--hybrid')]
    reconcile = '--reconcile' in sys.argv
    self_correct = '--self-correct' in sys.argv
    hybrid_mode = '--hybrid' in sys.argv
 
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
    print(f"🔄 Reconcile:    {'Enabled' if reconcile else 'Disabled'}")
    print(f"🔧 Self-correct: {'Enabled' if self_correct else 'Disabled'}")
    print(f"📐 Hybrid mode:  {'Enabled' if hybrid_mode else 'Disabled'}")
    print()
   
    # Load state template and build prompt
    state_config = load_state_template(state_code) if state_code else None
    if state_config:
        print(f"📋 Using state-specific rules for {state_config['state_name']}...")
    else:
        print(f"📋 Using default visual interpretation prompt...")
    prompt = get_conversion_prompt(state_config)
   
    # Log the full prompt for debugging
    logging.info(f"📝 FULL CONVERSION PROMPT ({len(prompt)} chars):\n{prompt}")

    # Save the full prompt to a file for review
    prompt_dump_path = os.path.join(os.path.dirname(pdf_path) if os.path.dirname(pdf_path) else ".", "prompt_dump.txt")
    with open(prompt_dump_path, 'w', encoding='utf-8') as pf:
        pf.write(f"FULL PROMPT BEING SENT TO CLAUDE FOR CONVERSION\n")
        pf.write(f"{'='*80}\n")
        pf.write(prompt)
        pf.write(f"\n{'='*80}\n")
        pf.write(f"Prompt length: {len(prompt)} characters\n")
    print(f"📝 Full prompt saved to: {prompt_dump_path} ({len(prompt)} chars)")

    # ── HYBRID MODE: text-based extraction + LLM cleaning ──────────
    if hybrid_mode:
        print(f"📐 HYBRID MODE: PyMuPDF text extraction + LLM text-chunk cleaning")
        converted_text = convert_pdf_hybrid_text(client, pdf_path, state_config, state_code)
        cached_page_images = None
        if not converted_text:
            print("❌ Hybrid mode conversion failed")
            sys.exit(1)
    else:
        # Pass 1: Visual mode conversion (images)
        print(f"🔄 Visual mode conversion...")
        state_dpi = state_config.get("dpi", 400) if state_config else 400
        visual_text, cached_page_images = convert_pdf_to_text(client, pdf_path, prompt, dpi=state_dpi)

        if not visual_text:
            print("❌ Visual mode conversion failed")
            sys.exit(1)

        converted_text = visual_text
 
    # Pass 2 & 3: Document mode + Reconciliation (only if --reconcile is enabled)
    if reconcile and not hybrid_mode:
        print(f"🔄 Document mode verification...")
        doc_prompt = get_document_mode_prompt(state_config)
        doc_text = convert_pdf_document_mode(client, pdf_path, doc_prompt)
 
        if doc_text:
            print(f"🔍 Reconciling outputs...")
            converted_text = reconcile_outputs(client, visual_text, doc_text)
        else:
            print("⚠️ Document mode failed, using visual output only")

    # Self-correction pass: re-examine images vs output to catch tagging errors
    if self_correct and cached_page_images:
        print(f"🔍 Self-correction pass (reviewing output against images)...")
        converted_text = self_correction_pass(client, converted_text, cached_page_images, state_config)

    # Post-process: deterministic cleanup of common Claude inconsistencies
    # For hybrid mode, load the precleaned text as reference so merged lines
    # can be restored generically (not just for specific table patterns).
    _reference_text = None
    if hybrid_mode:
        _precleaned_path = str(Path(pdf_path).with_suffix('')) + "_hybrid_precleaned.txt"
        if os.path.isfile(_precleaned_path):
            with open(_precleaned_path, "r", encoding="utf-8") as _pf:
                _reference_text = _pf.read()
    print(f"🔧 Running deterministic cleanup...")
    converted_text = deterministic_cleanup(
        converted_text, state_config=state_config, state_code=state_code,
        reference_text=_reference_text,
    )

    # Post-process: fix Iowa signature block layout
    converted_text = _reformat_ia_signature_block(converted_text, state_code=state_code)

    # Post-process: strip tags from protected clauses (state-specific cleanup)
    if state_config:
        converted_text = post_process_output(converted_text, state_config)

    # Save output
    print(f"💾 Saving output...")
    if save_text_file(converted_text, output_path):
        print(f"\n✅ SUCCESS! Conversion complete")
        print(f"📄 Output saved to: {output_path}")
        print(f"📊 Output size: {len(converted_text):,} characters")
    else:
        print("❌ Failed to save output file")
        sys.exit(1)

    # ── PyMuPDF Extraction ─────────────────────────────────────────────
    print("\n" + "="*70)
    print("📐 PyMuPDF EXTRACTION — Formatting Detection Report")
    print("="*70)

    if not _HAS_PYMUPDF:
        print("⚠️  PyMuPDF extractor not available (app/pymupdf_extractor.py not found)")
    else:
        try:
            pymupdf_start = time.time()
            flags = _pymupdf_flags_from_state(state_config)

            # Log which formats will be detected
            active_formats = []
            if flags["mark_underline"]:      active_formats.append("underline")
            if flags["mark_strikethrough"]:  active_formats.append("strikethrough")
            if flags["mark_italic"]:          active_formats.append("italic")
            if flags["mark_bold"]:            active_formats.append("bold")
            if flags["mark_brackets"]:        active_formats.append("brackets [→D]")
            state_label = state_config['state_name'] if state_config else 'Default'
            print(f"   State rules: {state_label}")
            print(f"   Detecting:   {', '.join(active_formats) if active_formats else 'none'}")
            print(f"   Strip line#: {'Yes' if flags['strip_line_numbers'] else 'No'}")
            print()

            # --- JSON report ---
            print("🔄 Generating PyMuPDF JSON report...")
            report = extract_pdf_json_report(pdf_path, state_code=state_code, **flags)
            pymupdf_json_path = output_path.replace(".txt", "_pymupdf_report.json")
            import json as json_mod2
            with open(pymupdf_json_path, "w", encoding="utf-8") as f:
                json_mod2.dump(report, f, indent=2, ensure_ascii=False)
            summary = report.get("summary", {})
            print(f"   ✅ JSON report saved to: {pymupdf_json_path}")
            span_summary = (
                f"UL={summary.get('underline_spans', 0)}, "
                f"SO={summary.get('strikethrough_spans', 0)}, "
                f"IT={summary.get('italic_spans', 0)}, "
                f"BD={summary.get('bold_spans', 0)}"
            )
            if summary.get('bracket_spans', 0):
                span_summary += f", BK={summary['bracket_spans']}"
            print(f"   📊 {summary.get('total_formatted_spans', 0)} formatted spans "
                  f"({span_summary})")

            # --- Tagged text ---
            print("🔄 Generating PyMuPDF tagged text...")
            pymupdf_text = extract_pdf_with_pymupdf(
                pdf_path,
                join_pages=True,
                state_code=state_code,
                **flags,
            )
            pymupdf_txt_path = output_path.replace(".txt", "_pymupdf.txt")
            with open(pymupdf_txt_path, "w", encoding="utf-8") as f:
                f.write(pymupdf_text)
            print(f"   ✅ Tagged text saved to: {pymupdf_txt_path}")
            print(f"   📊 {len(pymupdf_text):,} characters")

            # --- Filter italic spans by section scope (e.g. Indiana PRINTING CODE only) ---
            italic_section = (state_config.get("add_markup") or {}).get("italic_section_only") if state_config else None
            if italic_section and report:
                before_count = len(report.get("formatted_spans", []))
                upper_text = pymupdf_text.upper()
                scope_pos = upper_text.find(italic_section.upper())
                sec1_pos = upper_text.find("SECTION 1", scope_pos) if scope_pos >= 0 else -1
                if scope_pos >= 0:
                    section_text = pymupdf_text[scope_pos:(sec1_pos if sec1_pos >= 0 else scope_pos + 2000)]
                    filtered = []
                    for sp in report.get("formatted_spans", []):
                        if sp.get("format_types") == ["italic"]:
                            if sp.get("text", "").strip() not in section_text:
                                continue  # Skip italic outside scoped section
                        filtered.append(sp)
                    report["formatted_spans"] = filtered
                    removed = before_count - len(filtered)
                    if removed:
                        print(f"   🔍 Italic section filter: removed {removed} out-of-scope italic spans")

            # --- Highlighted images ---
            print("� Generating highlighted page images...")
            highlight_dir = Path(output_path).parent / f"{Path(pdf_path).stem}_highlighted"
            imgs = highlight_pdf_formatting(
                pdf_path, report=report, output_dir=highlight_dir, **flags,
            )
            print(f"   ✅ {len(imgs)} highlighted pages → {highlight_dir}")
            print(f"   🎨 Legend: {highlight_dir / 'legend.png'}")

            # --- LLM Clean + GPT 5.4 Intelligent Tag Comparison ---
            print()
            print("🧹 LLM-cleaning PyMuPDF text with Claude Opus 4.6 (removing headers/footers/line numbers)...")
            try:
                pymupdf_clean_text = clean_extracted_text(
                    pymupdf_text,
                    state_code=state_code,
                    anthropic_client=client,   # reuse the Claude client from conversion
                )
                # Track PyMuPDF clean tokens (estimate from text lengths since
                # clean_extracted_text doesn't return usage directly)
                try:
                    import tiktoken
                    _enc = tiktoken.get_encoding("cl100k_base")
                    _clean_in = len(_enc.encode(pymupdf_text))
                    _clean_out = len(_enc.encode(pymupdf_clean_text))
                except Exception:
                    _clean_in = len(pymupdf_text) // 4
                    _clean_out = len(pymupdf_clean_text) // 4
                _clean_cost = _calculate_cost(_clean_in, _clean_out)
                _TOKEN_TRACKER["calls"].append({
                    "step": "PyMuPDF LLM Clean",
                    "input_tokens": _clean_in,
                    "output_tokens": _clean_out,
                    "cost": _clean_cost,
                })
                _TOKEN_TRACKER["total_input"] += _clean_in
                _TOKEN_TRACKER["total_output"] += _clean_out
                _TOKEN_TRACKER["total_cost"] += _clean_cost
                logging.info(f"💰 [PyMuPDF LLM Clean] Tokens: {_clean_in:,} in / {_clean_out:,} out | Cost: ${_clean_cost:.4f} (estimated)")
                pymupdf_clean_text = consolidate_adjacent_tags(pymupdf_clean_text)
                pymupdf_clean_text = remove_nested_tags(pymupdf_clean_text)
                pymupdf_clean_text = merge_split_tags(pymupdf_clean_text)
                pymupdf_clean_text = split_tagged_subsections(pymupdf_clean_text)
                pymupdf_clean_path = output_path.replace(".txt", "_pymupdf_clean.txt")
                with open(pymupdf_clean_path, "w", encoding="utf-8") as f:
                    f.write(pymupdf_clean_text)
                print(f"   ✅ Clean text saved to: {pymupdf_clean_path}")
                print(f"   📊 {len(pymupdf_text):,} → {len(pymupdf_clean_text):,} chars")

                # --- Restore special symbols dropped by Claude vision ---
                converted_text_before = converted_text
                converted_text = restore_special_symbols(converted_text, pymupdf_clean_text)
                if converted_text != converted_text_before:
                    print("   🔧 Restored special symbols from PyMuPDF reference")
                    # Re-save the corrected output
                    with open(output_path, "w", encoding="utf-8") as f:
                        f.write(converted_text)
                    print(f"   💾 Updated output saved")

                # --- GPT 5.4 Intelligent Tag Comparison ---
                if _HAS_GPT_COMPARE:
                    print()
                    print("🤖 GPT 5.4 Intelligent Tag Comparison...")
                    # Load highlighted images as base64
                    import base64 as _b64
                    hi_images_b64 = []
                    for img_file in sorted(highlight_dir.glob("page_*.png")):
                        hi_images_b64.append(_b64.b64encode(img_file.read_bytes()).decode())
                    print(f"   📷 Loaded {len(hi_images_b64)} highlighted page images")

                    gpt_xlsx = output_path.replace(".txt", "_gpt_comparison.xlsx")
                    # Build state rules description
                    sr_parts = []
                    if state_config:
                        sr_parts.append(f"State: {state_config.get('state_name', state_code)}")
                        sr_parts.append(f"Underline={state_config.get('underline', False)}")
                        sr_parts.append(f"Strikethrough={state_config.get('strikethrough', False)}")
                        sr_parts.append(f"Italic={state_config.get('italic', False)}")
                        sr_parts.append(f"Bold={state_config.get('bold', False)}")
                    state_rules_str = ", ".join(sr_parts) if sr_parts else f"State: {state_code}"

                    gpt_cmp = gpt_compare_tags(
                        highlighted_images_b64=hi_images_b64,
                        pymupdf_clean_text=pymupdf_clean_text,
                        claude_converted_text=converted_text,
                        state_code=state_code,
                        state_rules=state_rules_str,
                        output_xlsx=gpt_xlsx,
                        pymupdf_report=report,
                    )
                    print(f"   ✅ GPT audit Excel saved to: {gpt_xlsx}")
                    print(f"   📋 Checklist: {gpt_cmp['total_checklist']} items")
                    print(f"   ✅ Pass: {gpt_cmp['passes']}  |  "
                          f"⚠️ Warnings: {gpt_cmp['warnings']}  |  "
                          f"❌ Errors: {gpt_cmp['errors']}")
                    print(f"      Matches: {gpt_cmp['matches']}  |  "
                          f"Partial: {gpt_cmp['partial_matches']}  |  "
                          f"Headers: {gpt_cmp['headers']}  |  "
                          f"Table: {gpt_cmp['table_content']}")
                    if gpt_cmp['content_dropped'] or gpt_cmp['real_misses']:
                        print(f"   ❌ Content dropped: {gpt_cmp['content_dropped']}  |  "
                              f"Real misses: {gpt_cmp['real_misses']}")
                    if gpt_cmp['additional_findings']:
                        print(f"   🔍 Additional findings: {gpt_cmp['additional_findings']}")
                else:
                    print("   ⚠️  gpt_compare module not available, skipping intelligent tag comparison")
            except Exception as clean_err:
                print(f"   ⚠️  LLM clean / tag comparison failed: {clean_err}")
                logging.error(f"LLM clean error: {clean_err}", exc_info=True)

            pymupdf_time = time.time() - pymupdf_start
            print(f"\n⏱️  PyMuPDF extraction took: {int(pymupdf_time//60)}m {int(pymupdf_time%60)}s")

        except Exception as e:
            print(f"⚠️  PyMuPDF extraction failed: {e}")
            logging.error(f"PyMuPDF extraction error: {e}", exc_info=True)

    # ── Token & Cost Summary ─────────────────────────────────────────
    # Determine page count for per-page projections
    try:
        _pdf_for_count = pdfium.PdfDocument(pdf_path)
        _num_pages = len(_pdf_for_count)
        _pdf_for_count.close()
    except Exception:
        _num_pages = None
    print_token_summary(num_pages=_num_pages)

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