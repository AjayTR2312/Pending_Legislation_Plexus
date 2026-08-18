import streamlit as st
import os
import sys
import base64
import shutil
from datetime import datetime
from pathlib import Path
import time
import logging
import threading
import copy

# Streamlit re-executes this script on every rerun; sys.stdout/stderr can be
# replaced or closed between reruns, so avoid any stream-backed handler here.
# setup_simple_logging() (called later in the script) installs real handlers.
logging.getLogger().addHandler(logging.NullHandler())
logging.raiseExceptions = False  # prevent handler I/O failures from crashing the app

# Add the current directory and the project root to the Python path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import our processing functions
try:
    from pdf_to_text_llm import (
        get_anthropic_client, load_conversion_prompt, load_example_pairs,
        process_pdf_with_claude, save_to_text_file, remove_unwanted_tags, 
        AVAILABLE_STATES, read_file_content, verify_text_with_llm,
        save_verified_file, create_verification_report
    )
except ImportError:
    get_anthropic_client = load_conversion_prompt = load_example_pairs = None
    process_pdf_with_claude = save_to_text_file = remove_unwanted_tags = None
    AVAILABLE_STATES = {}
    read_file_content = verify_text_with_llm = save_verified_file = create_verification_report = None
    logging.warning("pdf_to_text_llm module not found - legacy pipeline disabled")
try:
    from pdf_docx_analysis_pipeline import PDFToDocxAnalysisPipeline
except ImportError:
    PDFToDocxAnalysisPipeline = None
    logging.warning("pdf_docx_analysis_pipeline module not found - DOCX analysis disabled")
try:
    from session_manager import get_session_manager
except ImportError:
    get_session_manager = None
    logging.critical("session_manager module not found - app cannot function")
try:
    from pdf_plumber_extractor import extract_pdf_with_pdfplumber
except ImportError:
    extract_pdf_with_pdfplumber = None
    logging.warning("pdf_plumber_extractor module not found - pdfplumber extraction disabled")
try:
    from text_to_clean_prompt import convert_pdfplumber_to_clean
except ImportError:
    convert_pdfplumber_to_clean = None
    logging.warning("text_to_clean_prompt module not found - pdfplumber clean pipeline disabled")
try:
    from tag_then_clean_pipeline import tag_then_clean_pipeline
except ImportError:
    tag_then_clean_pipeline = None
    logging.warning("tag_then_clean_pipeline module not found - tag-then-clean pipeline disabled")
try:
    from validation_review_inline import render_inline_validation_review
except ImportError:
    render_inline_validation_review = None
    logging.warning("validation_review_inline module not found - validation review disabled")
try:
    from state_id_lookup import detect_state_from_numerical_id, get_state_name as _id_state_name
except ImportError:
    detect_state_from_numerical_id = None
    _id_state_name = None
    logging.warning("state_id_lookup module not found - numerical ID state detection disabled")
try:
    # Ensure Opus4.6 dir is on path for compare_files
    _opus46 = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'Opus4.6'))
    if _opus46 not in sys.path:
        sys.path.insert(0, _opus46)
    from compare_files import compare_texts
except ImportError:
    compare_texts = None
    logging.warning("compare_files module not found - content accuracy check disabled")
try:
    from protected_clauses import strip_protected_tags
except ImportError:
    strip_protected_tags = None
    logging.warning("protected_clauses module not found - protected clause stripping disabled")
try:
    from pdf_type_detector import detect_pdf_type
except ImportError:
    detect_pdf_type = None
    logging.warning("pdf_type_detector module not found - scanned PDF detection disabled")
try:
    from pdf_margin_cropper import auto_crop_if_needed
except ImportError:
    auto_crop_if_needed = None
    logging.warning("pdf_margin_cropper module not found - broad-margin auto-crop disabled")
try:
    from pdf_compare_viewer import (
        render_pdf_pages as compare_render_pdf_pages,
        extract_word_bboxes as compare_extract_word_bboxes,
        build_word_level_map as compare_build_word_level_map,
        build_viewer_html as compare_build_viewer_html,
    )
except ImportError:
    compare_render_pdf_pages = compare_extract_word_bboxes = None
    compare_build_word_level_map = compare_build_viewer_html = None
    logging.warning("pdf_compare_viewer module not found - PDF<->Text compare viewer disabled")

# ── SFTP Submit to Keyed helper ────────────────────────────────────────────
def _submit_to_keyed_sftp(file_path: str, edited_text: str = None):
    """Upload file to SFTP /ftp/keyed directly from its session folder.

    If edited_text is provided, the file is updated in-place before uploading.
    """
    try:
        from dotenv import load_dotenv as _ld
        # .env may be in parent dir (project root) or cwd
        _env_path = Path(__file__).resolve().parent.parent / ".env"
        if _env_path.exists():
            _ld(dotenv_path=str(_env_path))
        else:
            _ld()
    except ImportError:
        pass
    import paramiko

    src = Path(file_path)

    # Update the file with edited text if provided (HITL edits saved back)
    if edited_text is not None:
        with open(src, "w", encoding="utf-8") as f:
            f.write(edited_text)
        logging.info(f"[HITL] Updated {src.name} with validated edits")

    # SFTP upload directly from source
    _host = "10.17.38.130"
    _port = 22
    _user = "mag2ftp"
    _remote_dir = "/apps/Codes/MP2/LandingStrips/LCPR/ftp/keyed"
    _password = os.environ.get("FTP_PASSWORD", "")

    if not _password:
        raise RuntimeError("FTP_PASSWORD not configured in environment")

    transport = paramiko.Transport((_host, _port))
    transport.connect(username=_user, password=_password)
    sftp = paramiko.SFTPClient.from_transport(transport)
    try:
        sftp.put(str(src), f"{_remote_dir}/{src.name}")
        logging.info(f"[SFTP] Uploaded {src.name} to {_remote_dir}")

        # Write upload log via session manager
        session_mgr = st.session_state.get('session_manager') or get_session_manager()
        session_mgr.write_upload_log(f"UPLOADED {src.name} → {_remote_dir}/{src.name}")
    finally:
        sftp.close()
        transport.close()


# Full state name lookup for display

def _filter_italic_by_section(pymupdf_report, pymupdf_text, state_config):
    """Filter italic-only spans/tags to only the scoped section (e.g. PRINTING CODE).

    When a state config has add_markup.italic_section_only set, italic-only spans
    outside that section are removed from formatted_spans (so validation doesn't
    flag them) and their [A>...<A] tags are stripped from the tagged text.
    """
    if not state_config:
        return pymupdf_report, pymupdf_text
    section_scope = (state_config.get("add_markup") or {}).get("italic_section_only")
    if not section_scope:
        return pymupdf_report, pymupdf_text

    import re as _re

    # --- Filter formatted_spans ---
    if pymupdf_report and "formatted_spans" in pymupdf_report:
        # Find which pages contain the section scope marker
        # We'll track whether each span is in the scoped section
        # Strategy: find the section marker in the text, then only keep italic-only
        # spans whose context or surrounding text is within that section.
        # Simpler approach: find the section marker position in pymupdf_text,
        # and keep italic-only spans only between the marker and "SECTION 1"
        section_start = None
        section_end = None
        if pymupdf_text:
            marker_pos = pymupdf_text.upper().find(section_scope.upper())
            if marker_pos >= 0:
                section_start = marker_pos
                # Section ends at SECTION 1 or similar
                sec1_pos = pymupdf_text.upper().find("SECTION 1", marker_pos)
                section_end = sec1_pos if sec1_pos >= 0 else marker_pos + 2000  # fallback

        filtered_spans = []
        for span in pymupdf_report["formatted_spans"]:
            fmt_types = span.get("format_types", [])
            is_italic_only = fmt_types == ["italic"]

            if is_italic_only and section_start is not None:
                # Check if span text appears in the scoped section
                span_text = span.get("text", "").strip()
                if span_text:
                    section_text = pymupdf_text[section_start:section_end] if section_end else pymupdf_text[section_start:]
                    if span_text in section_text:
                        filtered_spans.append(span)  # Keep — it's in scope
                    else:
                        continue  # Skip — italic outside scope
                else:
                    continue
            else:
                filtered_spans.append(span)  # Not italic-only, keep

        pymupdf_report = dict(pymupdf_report)
        pymupdf_report["formatted_spans"] = filtered_spans

    # --- Filter tagged text: strip [A>...<A] around italic-only text outside section ---
    # The PyMuPDF tagged text marks ALL italic with [A>...<A].
    # We need to remove those tags outside the scoped section but keep the text.
    if pymupdf_text and section_scope:
        upper_text = pymupdf_text.upper()
        scope_pos = upper_text.find(section_scope.upper())
        if scope_pos >= 0:
            sec1_pos = upper_text.find("SECTION 1", scope_pos)
            if sec1_pos < 0:
                sec1_pos = scope_pos + 2000

            # Process in three parts: before scope, in scope, after scope
            before = pymupdf_text[:scope_pos]
            in_scope = pymupdf_text[scope_pos:sec1_pos]
            after = pymupdf_text[sec1_pos:]

            # In before and after, strip italic [A>...<A] tags (keep text)
            # We only want to strip italic tags, not bold tags.
            # Since PyMuPDF doesn't distinguish tag source, we need to be careful.
            # For states with both bold and italic as [A>, we strip ALL [A> outside scope
            # only if the span was italic-only. But we can't distinguish in tagged text.
            # Safer approach: only strip in before section (before PRINTING CODE),
            # leave after section alone since bold tags there are wanted.
            # Actually, the main issue is: italic tags exist throughout. Bold tags also
            # exist from PRINTING CODE onwards. We can't distinguish them in plain text.
            # So let's only filter the formatted_spans (done above) and leave tagged text as-is.
            # The validation uses formatted_spans, not the tagged text directly.
            pass

    return pymupdf_report, pymupdf_text

US_STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine",
    "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}

# Configure Streamlit page
#
# initial_sidebar_state="collapsed":
#   The sidebar holds Session Info (ID / Status / Created / System Stats)
#   and the "New Session" reset button.  This is debug/admin info that
#   most users don't need by default — show only the » toggle button so
#   the main content area gets full width.  Users can click » to expand
#   the sidebar when they want to inspect session details.
st.set_page_config(
    page_title="Pending Legislation Processor",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded"
)


# ─────────────────────────────────────────────────────────────────────────
# Local-development detection
# ─────────────────────────────────────────────────────────────────────────
def _is_local_dev() -> bool:
    """Return ``True`` when the app is running on a developer machine
    (i.e. NOT inside a Kubernetes pod or Docker container).

    Used to gate operational/debug-only UI such as the *Session Info*
    sidebar — useful for developers but noise for end users.

    Detection order
    ---------------
    1. Explicit override via ``STREAMLIT_LOCAL_DEV`` env var
       (``1``/``true``/``yes`` → local; ``0``/``false``/``no`` → prod).
    2. Kubernetes always sets ``KUBERNETES_SERVICE_HOST`` in pods → prod.
    3. Docker containers expose ``/.dockerenv`` → prod.
    4. Default: assume local.
    """
    _env = os.environ.get("STREAMLIT_LOCAL_DEV", "").strip().lower()
    if _env in ("1", "true", "yes"):
        return True
    if _env in ("0", "false", "no"):
        return False
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return False
    if os.path.exists("/.dockerenv"):
        return False
    return True


IS_LOCAL_DEV = _is_local_dev()


# ─────────────────────────────────────────────────────────────────────────
# Fast (API-free) state detection at upload time
# ─────────────────────────────────────────────────────────────────────────
# Strong page-1 text markers we look for to identify the bill's state.
# Each pattern is matched case-insensitively against the first 4000 chars
# of page 1.  The list is ordered most-specific → least-specific so that
# unique tokens (e.g. "STATE OF COLORADO") win over generic ones (e.g.
# "Colorado") when multiple states' names happen to appear (rare, but
# can happen in interstate-compact bills).
_STATE_TEXT_MARKERS: dict[str, tuple[str, ...]] = {
    # Highly distinctive patterns first
    "CO": (
        "STATE OF COLORADO",
        "COLORADO GENERAL ASSEMBLY",
        "COLORADO LEGISLATURE",
        "LLS NO.",
        "Colorado",
    ),
    "CT": (
        "General Assembly Raised Bill",
        "General Assembly Committee Bill",
        "General Assembly Substitute Bill",
        "LCO No.",
        "Connecticut",
    ),
    "IA": (
        "GENERAL ASSEMBLY OF THE STATE OF IOWA",
        "House File",
        "Senate File",
        "Iowa",
    ),
    "DE": (
        "GENERAL ASSEMBLY OF THE STATE OF DELAWARE",
        "Delaware",
    ),
    "IN": (
        "DIGEST OF INTRODUCED BILL",
        "Indiana Code",
        "Indiana General Assembly",
        "Indiana",
    ),
    "KS": (
        "K.S.A.",
        "STATE OF KANSAS",
        "Kansas Legislature",
        "Kansas",
    ),
    "CA": (
        "LEGISLATURE OF THE STATE OF CALIFORNIA",
        "California Legislature",
        "California",
    ),
    "AR": (
        "State of Arkansas",
        "Arkansas General Assembly",
        "Arkansas",
    ),
}


def _detect_state_fast(pdf_path: str) -> str | None:
    """Detect the bill's US-state code from page-1 text — no API call.

    Strategy
    --------
    1. Open the PDF with PyMuPDF (already a project dependency).
    2. Pull the first 4 000 characters of plain text from page 1.
    3. Walk through ``_STATE_TEXT_MARKERS`` in dict order; the first
       state whose marker substring is found in the text wins.

    Returns
    -------
    Two-letter state code (e.g. ``"CO"``) or ``None`` if no marker
    matched.  When ``None`` is returned, the downstream pipeline will
    fall back to its existing Vision-API content-detection step.
    """
    try:
        import pymupdf  # local import: keeps module-load path quick
    except Exception:
        return None
    try:
        doc = pymupdf.open(pdf_path)
        if len(doc) == 0:
            doc.close()
            return None
        page1_text = doc[0].get_text("text")[:4000]
        doc.close()
    except Exception as exc:
        logging.debug("Fast state detection failed to read page 1: %s", exc)
        return None
    if not page1_text:
        return None

    text_upper = page1_text.upper()
    for code, markers in _STATE_TEXT_MARKERS.items():
        for m in markers:
            if m.upper() in text_upper:
                logging.info(
                    "🔍 Fast state detection matched %s via marker %r",
                    code, m,
                )
                return code
    return None



# Initialize session state with SessionManager with error handling
try:
    if 'session_id' not in st.session_state:
        session_mgr = get_session_manager()
        st.session_state.session_id = session_mgr.create_session()
        st.session_state.session_manager = session_mgr

    if 'detected_state' not in st.session_state:
        st.session_state.detected_state = None
    if 'uploaded_file' not in st.session_state:
        st.session_state.uploaded_file = None
    if 'processing' not in st.session_state:
        st.session_state.processing = False
    if 'current_step' not in st.session_state:
        st.session_state.current_step = 0
    if 'step_status' not in st.session_state:
        st.session_state.step_status = {}
    if 'processed_file_path' not in st.session_state:
        st.session_state.processed_file_path = None
    if 'processed_file_data' not in st.session_state:
        st.session_state.processed_file_data = None  # cached bytes — survives pod rerouting
    if 'client' not in st.session_state:
        st.session_state.client = None
    if 'temp_pdf_path' not in st.session_state:
        st.session_state.temp_pdf_path = None
    if 'processing_time' not in st.session_state:
        st.session_state.processing_time = None
    if 'conversion_time' not in st.session_state:
        st.session_state.conversion_time = None
    if 'process_method' not in st.session_state:
        st.session_state.process_method = "vision"  # default: "vision" or "pdfplumber"
    if 'chunk_stats' not in st.session_state:
        st.session_state.chunk_stats = None
    if 'processing_logs' not in st.session_state:
        st.session_state.processing_logs = []
    if 'step_timings' not in st.session_state:
        st.session_state.step_timings = {}
    if 'audit_excel_path' not in st.session_state:
        st.session_state.audit_excel_path = None
    if 'audit_highlighted_dir' not in st.session_state:
        st.session_state.audit_highlighted_dir = None
    if 'audit_json_report' not in st.session_state:
        st.session_state.audit_json_report = None
    if 'audit_counts' not in st.session_state:
        # Holds the dict returned by pymupdf_compare_tags / gpt_compare_tags so
        # the Summary tab can show a prominent st.success/warning/error banner
        # without having to re-parse the Excel file every rerun.
        st.session_state.audit_counts = None
    if 'content_accuracy_report' not in st.session_state:
        st.session_state.content_accuracy_report = None
    if 'content_accuracy_detail' not in st.session_state:
        st.session_state.content_accuracy_detail = None
    if 'gpt_audit_failed' not in st.session_state:
        st.session_state.gpt_audit_failed = False
    if 'last_uploaded_file_id' not in st.session_state:
        st.session_state.last_uploaded_file_id = None

    # ── Batch processing state ──────────────────────────────────────────────
    if 'batch_results' not in st.session_state:
        st.session_state.batch_results = []          # list of dicts per file
    if 'batch_processing' not in st.session_state:
        st.session_state.batch_processing = False
    if 'batch_current_idx' not in st.session_state:
        st.session_state.batch_current_idx = 0
    if 'batch_pdf_paths' not in st.session_state:
        st.session_state.batch_pdf_paths = []        # temp paths of uploaded PDFs
    if 'batch_file_names' not in st.session_state:
        st.session_state.batch_file_names = []       # original file names
    if 'batch_selected_file' not in st.session_state:
        st.session_state.batch_selected_file = 0     # index for validation review
    if 'batch_upload_id' not in st.session_state:
        st.session_state.batch_upload_id = None
    if 'uploader_key_counter' not in st.session_state:
        st.session_state.uploader_key_counter = 0
    if 'batch_worker' not in st.session_state:
        st.session_state.batch_worker = None       # BatchWorker instance (background thread)

    # ── Validation Review inline (vri) state ────────────────────────────────
    # Pre-initialise so the widget tree is stable from the very first render
    # of the Validation Review tab.  Without this, the first button click
    # inside the tab causes new keys to appear → Streamlit's st.tabs() loses
    # track of the active tab and bounces back to the first tab.
    if 'vri_fixed_ids' not in st.session_state:
        st.session_state.vri_fixed_ids = []
    if 'vri_ignored_ids' not in st.session_state:
        st.session_state.vri_ignored_ids = []
    if 'vri_undo_history' not in st.session_state:
        st.session_state.vri_undo_history = []
    if 'vri_edit_mode' not in st.session_state:
        st.session_state.vri_edit_mode = False
    if 'vri_filter_mode' not in st.session_state:
        st.session_state.vri_filter_mode = "all"
    if 'vri_last_sel' not in st.session_state:
        st.session_state.vri_last_sel = None
    if '_vri_nav_btn_fired' not in st.session_state:
        st.session_state._vri_nav_btn_fired = False
    if '_vri_last_table_click' not in st.session_state:
        st.session_state._vri_last_table_click = None

    # Get session folders
    session_mgr = st.session_state.session_manager
    session_folders = session_mgr.get_session_folders(st.session_state.session_id)

    # Setup basic logging with session-specific folder
    LOG_FOLDER = session_folders['logs_folder']
    os.makedirs(LOG_FOLDER, exist_ok=True)
    
except Exception as e:
    st.error(f"❌ **Session Initialization Failed**")
    st.error(f"Error: {str(e)}")
    st.code(f"Type: {type(e).__name__}\nDetails: {str(e)}")
    import traceback
    with st.expander("Full Error Trace"):
        st.code(traceback.format_exc())
    st.stop()

def setup_simple_logging():
    """Setup simple logging for the app with session-specific folder"""
    fmt = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    # Close file handles before removing to avoid descriptor leaks across reruns
    for handler in logging.root.handlers[:]:
        try:
            handler.close()
        except Exception:
            pass
        logging.root.removeHandler(handler)

    logging.root.setLevel(logging.INFO)

    current_time = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_file_path = os.path.join(LOG_FOLDER, f'simple_app_{current_time}.log')

    try:
        fh = logging.FileHandler(log_file_path, encoding="utf-8")
        fh.setFormatter(fmt)
        logging.root.addHandler(fh)
    except Exception:
        pass  # log folder unavailable; continue without file logging

    try:
        _stream = open(sys.stderr.fileno(), mode='w', encoding='utf-8', closefd=False)
    except (ValueError, OSError):
        _stream = sys.stdout
    sh = logging.StreamHandler(_stream)
    sh.setFormatter(fmt)
    logging.root.addHandler(sh)

    logging.info(f"Session-specific logging configured. Logs saved to: {log_file_path}")
    logging.info(f"Session ID: {st.session_state.session_id}")
    return log_file_path

# Utility functions
def format_file_size(size_bytes):
    """Format file size in human readable format"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"

def highlight_legislative_tags(content):
    """Highlight legislative tags in text"""
    import re
    
    # Escape HTML characters first
    content = content.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    
    # Highlight [A>text<A] tags in blue
    content = re.sub(
        r'\[A&gt;(.*?)&lt;A\]', 
        r'<span style="background-color:#e3f2fd; color:#1976d2; font-weight:bold; padding:2px 4px; border-radius:3px;">[A&gt;\1&lt;A]</span>', 
        content
    )
    
    # Highlight [D>text<D] tags in red  
    content = re.sub(
        r'\[D&gt;(.*?)&lt;D\]', 
        r'<span style="background-color:#ffebee; color:#d32f2f; font-weight:bold; padding:2px 4px; border-radius:3px;">[D&gt;\1&lt;D]</span>', 
        content
    )
    
    return f'<div style="font-family:Consolas,monospace; white-space:pre-wrap; line-height:1.6; background:#f8f9fa; padding:16px; border-radius:8px; border-left:4px solid #2196f3; max-height:400px; overflow-y:auto;">{content}</div>'

# Simple processing functions
def process_single_pdf_simple(pdf_path, state_code, session_id, progress_placeholder=None, status_placeholder=None, log_placeholder=None):
    """Process a single PDF through all 5 steps with session management and UI updates"""
    # Start timing
    start_time = time.time()
    
    session_mgr = st.session_state.session_manager
    
    # Helper function to update UI
    def update_progress():
        if progress_placeholder:
            with progress_placeholder.container():
                show_processing_steps()
    
    # Start processing
    session_mgr.start_processing(session_id)
    
    steps = [
        {"name": "PDF to Text", "icon": "📄"},
        {"name": "PDF to DOCX", "icon": "📋"}, 
        {"name": "DOCX Analysis", "icon": "🔍"},
        {"name": "Summary Generation", "icon": "📊"},
        {"name": "LLM Verification", "icon": "🤖"}
    ]
    
    st.session_state.step_status = {i: "⏳" for i in range(5)}
    update_progress()  # Show initial progress
    
    # Initialize client if needed
    if not st.session_state.client:
        st.session_state.client = get_anthropic_client()
        if not st.session_state.client:
            st.error("❌ Failed to initialize AI client")
            return None
    
    try:
        # Get session-specific output folder
        session_folders = session_mgr.get_session_folders(session_id)
        output_folder = session_mgr.get_output_file_path(session_id, state_code, Path(pdf_path).stem)
        
        # Check if document needs PDF-level chunking
        from llm_tag_verifier import should_chunk_for_verification
        should_chunk, page_count, reason = should_chunk_for_verification(pdf_path)
        
        if should_chunk:
            # Large document - use PDF-level chunking (skip normal steps 1-4)
            print(f"\n🔄 Large document detected ({page_count} pages)")
            print(f"📦 Using PDF-level chunking workflow...")
            
            # Mark all steps as in progress
            for step in range(5):
                st.session_state.step_status[step] = "🔄"
            update_progress()
            
            # Call overlapping chunk verification for 100% accuracy
            from llm_tag_verifier import verify_with_overlapping_chunks
            corrected_text = verify_with_overlapping_chunks(
                pdf_path, 
                state_code, 
                st.session_state.client, 
                session_folders['session_dir']
            )
            
            if not corrected_text:
                for step in range(5):
                    st.session_state.step_status[step] = "❌"
                return None
            
            # Mark all steps as complete
            for step in range(5):
                st.session_state.step_status[step] = "✅"
            update_progress()
            
            # Save the final verified text
            output_filename = Path(pdf_path).stem + ".txt"
            text_output_path = os.path.join(output_folder, output_filename)
            
            # Determine verified output path
            verified_folder = os.path.join(session_folders['output_folder'], 'Verified', state_code)
            os.makedirs(verified_folder, exist_ok=True)
            verified_file_path = os.path.join(verified_folder, output_filename)
            
            with open(verified_file_path, 'w', encoding='utf-8') as f:
                f.write(corrected_text)
            
            print(f"✅ Chunked processing complete!")
            print(f"📄 Verified file saved to: {verified_file_path}")
            
            # Create verification report for chunked processing
            # Note: We don't have original_text and docx_results here, but we have corrected_text
            # For now, skip report creation for chunked docs or create simplified version
            
            # Mark processing complete in session manager
            session_mgr.complete_processing(session_id, verified_file_path)
            
            # Calculate total processing time
            end_time = time.time()
            total_time = end_time - start_time
            minutes = int(total_time // 60)
            seconds = int(total_time % 60)
            
            # Store processing time in session state for display
            st.session_state.processing_time = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"
            
            # Log completion message
            completion_msg = f"🎉 Chunked processing completed successfully! Total time: {st.session_state.processing_time}"
            print("\n" + "="*60)
            print(completion_msg)
            print("="*60)
            logging.info(completion_msg)
            
            # Return file path (consistent with normal workflow)
            return verified_file_path
        
        # Small document - use normal workflow with PARALLEL processing
        print(f"\n📄 Standard document ({page_count} pages)")
        print(f"⚡ Using parallel processing workflow (PDF→Text + PDF→DOCX simultaneously)...")
        
        # Mark Steps 1-4 as in progress (they'll run in parallel)
        for step in range(4):
            st.session_state.step_status[step] = "🔄"
            session_mgr.update_processing_step(session_id, step)
        update_progress()  # Update UI
        time.sleep(0.5)  # Brief pause for UI update
        
        # Load conversion resources once
        conversion_prompt = load_conversion_prompt(state_code)
        examples = load_example_pairs(state_code)
        
        # Get client reference (must be done BEFORE threading)
        client = st.session_state.client
        
        # ============================================================
        # PARALLEL EXECUTION: PDF→Text + PDF→DOCX run simultaneously
        # ============================================================
        from concurrent.futures import ThreadPoolExecutor
        
        def run_pdf_to_text(client, pdf_path, examples, conversion_prompt, output_folder, state_code):
            """Convert PDF to text (Step 1) - ~40 seconds"""
            print("  🔄 Thread 1: Starting PDF→Text conversion...")
            formatted_text = process_pdf_with_claude(
                client, pdf_path, examples, conversion_prompt
            )
            
            if not formatted_text:
                print("  ❌ Thread 1: PDF→Text conversion failed")
                return None
            
            # Save text file in session output folder
            output_filename = Path(pdf_path).stem + ".txt"
            text_output_path = os.path.join(output_folder, output_filename)
            
            if not save_to_text_file(formatted_text, text_output_path, state_code=state_code):
                print("  ❌ Thread 1: Failed to save text file")
                return None
            
            print(f"  ✅ Thread 1: PDF→Text complete ({len(formatted_text)} chars)")
            return text_output_path
        
        def run_pdf_to_docx(pdf_path, output_folder, state_code):
            """Convert PDF→DOCX→Analysis→Summary (Steps 2-4) - ~50 seconds"""
            print("  🔄 Thread 2: Starting PDF→DOCX pipeline...")
            pipeline = PDFToDocxAnalysisPipeline(pdf_path, output_folder, state_code=state_code)
            docx_results = pipeline.run_complete_pipeline()
            
            if not docx_results or not docx_results['success']:
                print("  ❌ Thread 2: DOCX pipeline failed")
                return None
            
            print(f"  ✅ Thread 2: DOCX pipeline complete")
            return docx_results
        
        # Execute both operations in parallel
        print("\n⚡ Starting parallel processing...")
        parallel_start = time.time()
        
        with ThreadPoolExecutor(max_workers=2) as executor:
            future_text = executor.submit(run_pdf_to_text, client, pdf_path, examples, conversion_prompt, output_folder, state_code)
            future_docx = executor.submit(run_pdf_to_docx, pdf_path, session_folders['output_folder'], state_code)
            
            # Wait for both to complete
            text_output_path = future_text.result()
            docx_results = future_docx.result()
        
        parallel_time = time.time() - parallel_start
        print(f"✅ Parallel processing complete in {parallel_time:.1f}s")
        
        # Check if both succeeded
        if not text_output_path:
            st.session_state.step_status[0] = "❌"
            return None
        
        if not docx_results:
            for step in [1, 2, 3]:
                st.session_state.step_status[step] = "❌"
            return None
        
        # Mark all steps as complete
        for step in range(4):
            st.session_state.step_status[step] = "✅"
        time.sleep(0.1)  # Brief pause for UI update
        update_progress()  # Update UI
        
        # Step 5: LLM Verification (only if there are formatted items)
        st.session_state.current_step = 4
        st.session_state.step_status[4] = "🔄"
        session_mgr.update_processing_step(session_id, 4)
        update_progress()  # Update UI
        time.sleep(0.5)  # Brief pause for UI update
        
        # Check if summary file exists (only if there were formatted items)
        if docx_results.get('summary_path'):
            corrected_text = verify_text_with_llm(
                text_output_path, docx_results['summary_path'], 
                state_code, st.session_state.client,
                pdf_path=None,  # Don't pass PDF path - already decided not to chunk
                session_dir=None  # Don't pass session dir
            )
            
            if not corrected_text:
                st.session_state.step_status[4] = "❌"
                return None
                
            # Save verified file in session-specific Verified folder
            verified_file_path = save_verified_file(
                corrected_text, 
                text_output_path, 
                state_code, 
                session_folders['output_folder']  # Pass session output folder
            )
            
            if not verified_file_path:
                st.session_state.step_status[4] = "❌"
                return None
                
            # Create verification report
            original_text = read_file_content(text_output_path)
            create_verification_report(
                docx_results['summary_path'], text_output_path, verified_file_path,
                state_code, original_text, corrected_text
            )
        else:
            # No formatted items found - copy original text to Verified folder
            st.info("ℹ️ No formatted items found - copying to Verified folder without verification")
            original_text = read_file_content(text_output_path)
            
            if not original_text:
                st.session_state.step_status[4] = "❌"
                st.error("Failed to read converted text file")
                return None
                
            verified_file_path = save_verified_file(
                original_text, 
                text_output_path, 
                state_code, 
                session_folders['output_folder']  # Pass session output folder
            )
            
            if not verified_file_path:
                st.session_state.step_status[4] = "❌"
                st.error("Failed to save verified file")
                return None
        
        st.session_state.step_status[4] = "✅"
        st.session_state.current_step = 5  # Complete
        time.sleep(0.1)  # Brief pause for UI update
        update_progress()  # Update UI
        
        # Mark processing complete in session manager
        session_mgr.complete_processing(session_id, verified_file_path)
        
        # Calculate total processing time
        end_time = time.time()
        total_time = end_time - start_time
        minutes = int(total_time // 60)
        seconds = int(total_time % 60)
        
        # Store processing time in session state for display
        st.session_state.processing_time = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"
        
        # Log completion message (shows in terminal)
        completion_msg = f"🎉 Processing completed successfully! Total time: {st.session_state.processing_time}"
        print("\n" + "="*60)
        print(completion_msg)
        print("="*60)
        logging.info(completion_msg)
        
        return verified_file_path
        
    except Exception as e:
        st.error(f"❌ Processing error: {str(e)}")
        logging.error(f"Processing error: {e}", exc_info=True)
        return None

def process_with_pdfplumber_pipeline(pdf_path, state_code, session_id, progress_placeholder=None, status_placeholder=None):
    """Process PDF using pdfplumber extraction + LLM prompt cleaning workflow"""
    start_time = time.time()
    
    session_mgr = st.session_state.session_manager
    
    # Helper function to update UI
    def update_progress():
        if progress_placeholder:
            with progress_placeholder.container():
                show_processing_steps()
    
    # Start processing
    session_mgr.start_processing(session_id)
    
    # Get session-specific folders
    session_folders = session_mgr.get_session_folders(session_id)
    output_folder = session_mgr.get_output_file_path(session_id, state_code, Path(pdf_path).stem)
    
    # Initialize client if needed
    if not st.session_state.client:
        st.session_state.client = get_anthropic_client()
        if not st.session_state.client:
            st.error("❌ Failed to initialize AI client")
            return None
    
    try:
        # Check if document needs chunking (same 7-page threshold as vision workflow)
        from llm_tag_verifier import should_chunk_for_verification
        should_chunk, page_count, reason = should_chunk_for_verification(pdf_path)
        
        if should_chunk:
            # Large document - use PDFPlumber chunking workflow
            print(f"\n🔄 Large document detected ({page_count} pages)")
            print(f"📦 Using PDFPlumber chunking workflow...")
            
            steps = [
                {"name": "PDFPlumber Chunks", "icon": "📦"},
                {"name": "LLM Clean Chunks", "icon": "🧹"}, 
                {"name": "DOCX Analysis", "icon": "📋"},
                {"name": "Verify Chunks", "icon": "🤖"},
                {"name": "Merge Results", "icon": "🔗"}
            ]
            
            st.session_state.step_status = {i: "🔄" for i in range(5)}
            update_progress()
            
            # Call PDFPlumber chunk verification
            from llm_tag_verifier import verify_with_pdfplumber_chunks
            corrected_text = verify_with_pdfplumber_chunks(
                pdf_path, 
                state_code, 
                st.session_state.client, 
                session_folders['session_dir']
            )
            
            if not corrected_text:
                for step in range(5):
                    st.session_state.step_status[step] = "❌"
                return None
            
            # Mark all steps as complete
            for step in range(5):
                st.session_state.step_status[step] = "✅"
            update_progress()
            
            # Save the final verified text
            output_filename = Path(pdf_path).stem + ".txt"
            
            # Determine verified output path
            verified_folder = os.path.join(session_folders['output_folder'], 'Verified', state_code)
            os.makedirs(verified_folder, exist_ok=True)
            verified_file_path = os.path.join(verified_folder, output_filename)
            
            with open(verified_file_path, 'w', encoding='utf-8') as f:
                f.write(corrected_text)
            
            print(f"✅ Chunked PDFPlumber processing complete!")
            print(f"📄 Verified file saved to: {verified_file_path}")
            
            # Mark processing complete in session manager
            session_mgr.complete_processing(session_id, verified_file_path)
            
            # Calculate total processing time
            end_time = time.time()
            total_time = end_time - start_time
            minutes = int(total_time // 60)
            seconds = int(total_time % 60)
            
            # Store processing time
            st.session_state.processing_time = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"
            
            # Log completion
            completion_msg = f"🎉 Chunked PDFPlumber processing completed! Total time: {st.session_state.processing_time}"
            print("\n" + "="*60)
            print(completion_msg)
            print("="*60)
            logging.info(completion_msg)
            
            return verified_file_path
        
        # Small document - use normal PDFPlumber workflow (5 steps)
        print(f"\n📄 Standard document ({page_count} pages)")
        print(f"⚡ Using normal PDFPlumber workflow...")
        
        steps = [
            {"name": "PDFPlumber Extract", "icon": "📄"},
            {"name": "LLM Text Clean", "icon": "🧹"}, 
            {"name": "DOCX Analysis", "icon": "📋"},
            {"name": "Summary Generation", "icon": "📊"},
            {"name": "LLM Verification", "icon": "🤖"}
        ]
        
        st.session_state.step_status = {i: "⏳" for i in range(5)}
        update_progress()  # Show initial progress
        
        # Step 1: Extract with pdfplumber
        st.session_state.current_step = 0
        st.session_state.step_status[0] = "🔄"
        session_mgr.update_processing_step(session_id, 0)
        update_progress()
        
        if status_placeholder:
            status_placeholder.info("📄 Extracting text with pdfplumber...")
        
        print("\n🔄 Step 1/5: PDFPlumber extraction...")
        raw_output = os.path.join(session_folders['output_folder'], 'pdfplumber_raw.txt')
        
        try:
            extract_pdf_with_pdfplumber(pdf_path, raw_output, validate_sections=True)
            if not os.path.exists(raw_output):
                st.session_state.step_status[0] = "❌"
                st.error("❌ PDFPlumber extraction failed - no output file")
                return None
            print(f"✅ PDFPlumber extraction complete: {raw_output}")
        except Exception as e:
            st.session_state.step_status[0] = "❌"
            st.error(f"❌ PDFPlumber extraction error: {str(e)}")
            logging.error(f"PDFPlumber extraction error: {e}", exc_info=True)
            return None
        
        st.session_state.step_status[0] = "✅"
        update_progress()
        
        # Step 2: Clean with LLM prompt
        st.session_state.current_step = 1
        st.session_state.step_status[1] = "🔄"
        session_mgr.update_processing_step(session_id, 1)
        update_progress()
        
        if status_placeholder:
            status_placeholder.info(f"🧹 Cleaning text with {state_code}-specific prompt...")
        
        print(f"\n🔄 Step 2/5: LLM text cleaning with {state_code} prompt...")
        clean_output = os.path.join(output_folder, Path(pdf_path).stem + ".txt")
        
        try:
            clean_text = convert_pdfplumber_to_clean(
                raw_output, 
                state_code, 
                clean_output, 
                st.session_state.client
            )
            
            if not clean_text:
                st.session_state.step_status[1] = "❌"
                st.error("❌ Text cleaning failed")
                return None
            
            print(f"✅ Text cleaning complete: {len(clean_text)} chars")
        except Exception as e:
            st.session_state.step_status[1] = "❌"
            st.error(f"❌ Text cleaning error: {str(e)}")
            logging.error(f"Text cleaning error: {e}", exc_info=True)
            return None
        
        st.session_state.step_status[1] = "✅"
        update_progress()
        
        # Steps 3-4: DOCX Analysis Pipeline (same as vision workflow)
        for step in [2, 3]:
            st.session_state.step_status[step] = "🔄"
            session_mgr.update_processing_step(session_id, step)
        update_progress()
        
        if status_placeholder:
            status_placeholder.info("📋 Analyzing DOCX formatting...")
        
        print(f"\n🔄 Steps 3-4/5: DOCX analysis pipeline...")
        
        try:
            pipeline = PDFToDocxAnalysisPipeline(pdf_path, session_folders['output_folder'], state_code=state_code)
            docx_results = pipeline.run_complete_pipeline()
            
            if not docx_results or not docx_results['success']:
                for step in [2, 3]:
                    st.session_state.step_status[step] = "❌"
                st.error("❌ DOCX analysis failed")
                return None
            
            print(f"✅ DOCX analysis complete")
        except Exception as e:
            for step in [2, 3]:
                st.session_state.step_status[step] = "❌"
            st.error(f"❌ DOCX analysis error: {str(e)}")
            logging.error(f"DOCX analysis error: {e}", exc_info=True)
            return None
        
        for step in [2, 3]:
            st.session_state.step_status[step] = "✅"
        update_progress()
        
        # Step 5: LLM Verification (only if there are formatted items)
        st.session_state.current_step = 4
        st.session_state.step_status[4] = "🔄"
        session_mgr.update_processing_step(session_id, 4)
        update_progress()
        
        if status_placeholder:
            status_placeholder.info("🤖 Verifying with LLM...")
        
        print(f"\n🔄 Step 5/5: LLM verification...")
        
        # Check if summary file exists (only if there were formatted items)
        if docx_results.get('summary_path'):
            corrected_text = verify_text_with_llm(
                clean_output, 
                docx_results['summary_path'], 
                state_code, 
                st.session_state.client,
                pdf_path=None,
                session_dir=None
            )
            
            if not corrected_text:
                st.session_state.step_status[4] = "❌"
                st.error("❌ LLM verification failed")
                return None
                
            # Save verified file
            verified_file_path = save_verified_file(
                corrected_text, 
                clean_output, 
                state_code, 
                session_folders['output_folder']
            )
            
            if not verified_file_path:
                st.session_state.step_status[4] = "❌"
                st.error("❌ Failed to save verified file")
                return None
                
            # Create verification report
            original_text = clean_text  # Use clean text as "original"
            create_verification_report(
                docx_results['summary_path'], clean_output, verified_file_path,
                state_code, original_text, corrected_text
            )
            
            print(f"✅ LLM verification complete")
        else:
            # No formatted items found - copy clean text to Verified folder
            st.info("ℹ️ No formatted items found - copying to Verified folder without verification")
            
            verified_file_path = save_verified_file(
                clean_text, 
                clean_output, 
                state_code, 
                session_folders['output_folder']
            )
            
            if not verified_file_path:
                st.session_state.step_status[4] = "❌"
                st.error("❌ Failed to save verified file")
                return None
            
            print(f"✅ Clean text saved to Verified folder")
        
        st.session_state.step_status[4] = "✅"
        st.session_state.current_step = 5  # Complete
        update_progress()
        
        # Mark processing complete
        session_mgr.complete_processing(session_id, verified_file_path)
        
        # Calculate total processing time
        end_time = time.time()
        total_time = end_time - start_time
        minutes = int(total_time // 60)
        seconds = int(total_time % 60)
        
        st.session_state.processing_time = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"
        
        completion_msg = f"🎉 PDFPlumber processing completed! Total time: {st.session_state.processing_time}"
        print("\n" + "="*60)
        print(completion_msg)
        print("="*60)
        logging.info(completion_msg)
        
        return verified_file_path
        
    except Exception as e:
        st.error(f"❌ PDFPlumber processing error: {str(e)}")
        logging.error(f"PDFPlumber processing error: {e}", exc_info=True)
        return None

def process_with_tag_then_clean_pipeline(pdf_path, state_code, session_id, progress_placeholder=None, status_placeholder=None):
    """Process PDF using Tag-Then-Clean workflow: Extract raw → Tag → Clean"""
    start_time = time.time()
    session_mgr = st.session_state.session_manager
    session_folders = session_mgr.get_session_folders(session_id)
    
    def update_progress():
        if progress_placeholder:
            with progress_placeholder.container():
                show_processing_steps()
    
    # Start processing
    session_mgr.start_processing(session_id)
    
    try:
        # Check if document needs chunking
        from llm_tag_verifier import should_chunk_for_verification
        should_chunk, page_count, reason = should_chunk_for_verification(pdf_path)
        
        if should_chunk:
            # Large document - use chunking workflow
            print(f"\n🔄 Large document detected ({page_count} pages)")
            print(f"📦 Using Tag-Then-Clean chunking workflow...")
            
            steps = [
                {"name": "Chunk Processing", "icon": "📦"},
                {"name": "Tagging Chunks", "icon": "🏷️"},
                {"name": "Cleaning Chunks", "icon": "🧹"},
                {"name": "Merging Results", "icon": "🔗"},
                {"name": "Finalizing", "icon": "✅"}
            ]
            
            st.session_state.step_status = {i: "⏳" for i in range(5)}
            update_progress()
            
            # Use chunked processing from tag_then_clean_pipeline
            # This handles ALL steps internally: extract chunks, DOCX, summary, tag, clean, merge
            st.session_state.current_step = 0
            st.session_state.step_status[0] = "🔄"
            update_progress()
            
            if status_placeholder:
                status_placeholder.info("🏷️ Processing chunks with Tag-Then-Clean...")
            
            print("\n🔄 Tag-Then-Clean chunked pipeline...")
            
            # Get client using helper function (matching other workflows)
            try:
                from tag_then_clean_pipeline import tag_then_clean_with_pdfplumber_chunks, get_anthropic_client
            except ImportError:
                raise Exception("tag_then_clean_pipeline module not found — this pipeline is not available")
            
            try:
                # Get fresh client
                client = get_anthropic_client()
                
                # Update progress as processing continues
                st.session_state.step_status[0] = "✅"
                st.session_state.step_status[1] = "🔄"
                update_progress()
                
                corrected_text = tag_then_clean_with_pdfplumber_chunks(
                    pdf_path=pdf_path,
                    state_code=state_code,
                    client=client,
                    session_dir=session_folders['session_dir']
                )
                
                if not corrected_text or not corrected_text.get('success'):
                    st.session_state.step_status[1] = "❌"
                    st.session_state.step_status[2] = "❌"
                    st.session_state.step_status[3] = "❌"
                    st.session_state.step_status[4] = "❌"
                    st.error("❌ Chunked Tag-Then-Clean failed")
                    return None
                
                # Mark all steps complete
                st.session_state.step_status[1] = "✅"
                st.session_state.step_status[2] = "✅"
                st.session_state.step_status[3] = "✅"
                st.session_state.step_status[4] = "✅"
                update_progress()
                print("✅ Tag-Then-Clean chunked pipeline complete")
                
                # Extract file path from results
                verified_file_path = corrected_text.get('output_path')
                
                if not verified_file_path or not os.path.exists(verified_file_path):
                    st.error("❌ Output file not found")
                    return None
                
                # Mark processing complete
                session_mgr.complete_processing(session_id, verified_file_path)
                
                # Calculate total time
                end_time = time.time()
                total_time = end_time - start_time
                minutes = int(total_time // 60)
                seconds = int(total_time % 60)
                
                st.session_state.processing_time = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"
                
                # Store chunk processing stats for display
                st.session_state.chunk_stats = {
                    'chunks_processed': corrected_text.get('chunks_processed', 0),
                    'total_chunks': corrected_text.get('total_chunks', 0),
                    'character_count': corrected_text.get('character_count', 0)
                }
                
                completion_msg = f"🎉 Tag-Then-Clean chunked processing completed! " \
                                f"Processed {st.session_state.chunk_stats['chunks_processed']}/{st.session_state.chunk_stats['total_chunks']} chunks. " \
                                f"Total time: {st.session_state.processing_time}"
                print("\n" + "="*60)
                print(completion_msg)
                print("="*60)
                logging.info(completion_msg)
                
                return verified_file_path
                
            except Exception as e:
                st.session_state.step_status[0] = "❌"
                st.session_state.step_status[1] = "❌"
                st.session_state.step_status[2] = "❌"
                st.session_state.step_status[3] = "❌"
                st.session_state.step_status[4] = "❌"
                st.error(f"❌ Tag-Then-Clean chunked failed: {e}")
                logging.error(f"Tag-Then-Clean chunked failed: {e}")
                import traceback
                traceback.print_exc()
                return None
        
        # Small document - use normal Tag-Then-Clean workflow (no chunking)
        print(f"\n📄 Standard document ({page_count} pages)")
        print(f"⚡ Using normal Tag-Then-Clean workflow...")
        
        steps = [
            {"name": "PDFPlumber Extract", "icon": "📄"},
            {"name": "DOCX Analysis", "icon": "📋"},
            {"name": "Summary Generation", "icon": "📊"},
            {"name": "Tag Raw Text", "icon": "🏷️"},
            {"name": "Clean Tagged Text", "icon": "🧹"}
        ]
        
        st.session_state.step_status = {i: "⏳" for i in range(5)}
        update_progress()
        
        # Step 1: Extract with pdfplumber
        st.session_state.current_step = 0
        st.session_state.step_status[0] = "🔄"
        update_progress()
        
        if status_placeholder:
            status_placeholder.info("📄 Extracting text with pdfplumber...")
        
        print("\n🔄 Step 1/5: PDFPlumber extraction...")
        raw_output = os.path.join(session_folders['output_folder'], 'pdfplumber_raw.txt')
        
        extract_pdf_with_pdfplumber(pdf_path, raw_output, validate_sections=True)
        if not os.path.exists(raw_output):
            st.session_state.step_status[0] = "❌"
            st.error("❌ PDFPlumber extraction failed")
            return None
        
        st.session_state.step_status[0] = "✅"
        update_progress()
        print("✅ PDFPlumber extraction complete")
        
        # Step 2: DOCX Analysis
        st.session_state.current_step = 1
        st.session_state.step_status[1] = "🔄"
        update_progress()
        
        if status_placeholder:
            status_placeholder.info("📋 Analyzing DOCX structure...")
        
        print("\n🔄 Step 2/5: DOCX analysis...")
        docx_output_dir = os.path.join(session_folders['output_folder'], 'Output_Docx', state_code)
        os.makedirs(docx_output_dir, exist_ok=True)
        
        docx_pipeline = PDFToDocxAnalysisPipeline(pdf_path, docx_output_dir, state_code=state_code)
        docx_results = docx_pipeline.run_complete_pipeline()
        
        if not docx_results or not docx_results['success']:
            st.session_state.step_status[1] = "❌"
            st.error("❌ DOCX conversion failed")
            return None
        
        docx_file = docx_results['docx_path']
        summary_file = docx_results.get('summary_path')  # May be None if no formatting
        
        st.session_state.step_status[1] = "✅"
        update_progress()
        print("✅ DOCX analysis complete")
        
        # Step 3: Summary already generated by run_complete_pipeline
        st.session_state.current_step = 2
        st.session_state.step_status[2] = "✅"  # Already done
        update_progress()
        print("✅ Summary generation complete")
        
        if not summary_file:
            st.error("❌ Summary file not generated")
            return None
        
        # Step 4 & 5: Tag-Then-Clean Pipeline
        st.session_state.current_step = 3
        st.session_state.step_status[3] = "🔄"
        st.session_state.step_status[4] = "⏳"
        update_progress()
        
        if status_placeholder:
            status_placeholder.info("🏷️ Running Tag-Then-Clean pipeline...")
        
        print("\n🔄 Step 4-5/5: Tag-Then-Clean pipeline...")
        output_dir = os.path.join(session_folders['output_folder'], 'TagThenClean')
        
        try:
            results = tag_then_clean_pipeline(
                raw_text_path=raw_output,
                summary_path=summary_file,
                state_code=state_code,
                output_dir=output_dir,
                pdf_path=pdf_path
            )
            
            st.session_state.step_status[3] = "✅"
            st.session_state.step_status[4] = "✅"
            update_progress()
            print("✅ Tag-Then-Clean pipeline complete")
            
            # Mark processing complete
            final_output = results['final_output']
            session_mgr.complete_processing(session_id, final_output)
            
            # Calculate total time
            end_time = time.time()
            total_time = end_time - start_time
            minutes = int(total_time // 60)
            seconds = int(total_time % 60)
            
            st.session_state.processing_time = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"
            
            completion_msg = f"🎉 Tag-Then-Clean processing completed! Total time: {st.session_state.processing_time}"
            print("\n" + "="*60)
            print(completion_msg)
            print("="*60)
            logging.info(completion_msg)
            
            return final_output
            
        except Exception as e:
            st.session_state.step_status[3] = "❌"
            st.session_state.step_status[4] = "❌"
            st.error(f"❌ Tag-Then-Clean pipeline failed: {e}")
            print(f"❌ Error: {e}")
            return None
        
    except Exception as e:
        st.error(f"❌ Tag-Then-Clean processing error: {str(e)}")
        logging.error(f"Tag-Then-Clean processing error: {e}", exc_info=True)
        return None

def _format_accuracy_text_report(
    accuracy_report: dict,
    *,
    file_label: str | None = None,
    state_code: str | None = None,
) -> str:
    """Render the accuracy_report dict into a human-readable text file.

    Designed to be quick to scan: headline numbers up top, then a per-bucket
    breakdown, then full per-diff listings.  Used for the ``*_accuracy.txt``
    artifact saved alongside ``*_removals.json``.
    """
    from datetime import datetime

    acc       = accuracy_report.get("accuracy", 0.0)
    raw_acc   = accuracy_report.get("raw_accuracy", acc)
    status    = accuracy_report.get("status", "?")
    n_word    = accuracy_report.get("word_diff_count", 0)
    n_trans   = accuracy_report.get("transposed_count", 0)
    n_punct   = accuracy_report.get("punctuation_count", 0)
    n_spacing = accuracy_report.get("spacing_count", 0)
    n_repos   = accuracy_report.get("repositioned_count", 0)
    n_allowed = accuracy_report.get("allowed_diff_count", 0)
    real_diffs   = accuracy_report.get("real_diffs", []) or []
    repositioned = accuracy_report.get("repositioned", []) or []
    allowed      = accuracy_report.get("allowed_diffs", []) or []
    spacing      = accuracy_report.get("spacing_diffs", []) or []
    allowed_cats = accuracy_report.get("allowed_categories", {}) or {}
    pym_wc       = accuracy_report.get("pymupdf_word_count", 0)
    cnv_wc       = accuracy_report.get("converted_word_count", 0)
    match_wc     = accuracy_report.get("matching_words", 0)

    if status == "IDENTICAL":
        status_icon = "✅"
    elif status == "CONTENT_MATCH":
        status_icon = "✅"
    elif acc >= 99:
        status_icon = "✅"
    elif acc >= 95:
        status_icon = "⚠️"
    else:
        status_icon = "❌"

    sep = "=" * 80
    sub = "-" * 80
    lines: list[str] = []

    # ── Header ──────────────────────────────────────────────────────
    lines += [sep, "TEXT ACCURACY REPORT", sep]
    lines.append(f"Generated:  {datetime.now():%Y-%m-%d %H:%M:%S}")
    if file_label:
        lines.append(f"File:       {file_label}")
    if state_code:
        lines.append(f"State:      {state_code}")
    lines.append(f"Status:     {status}  {status_icon}")
    lines.append("")

    # ── Headline ────────────────────────────────────────────────────
    lines += [sub, "HEADLINE", sub]
    lines.append(f"  Adjusted accuracy:    {acc:6.2f}%   {status_icon}  (shown in UI)")
    if n_allowed and raw_acc < acc:
        lines.append(f"  Raw accuracy:         {raw_acc:6.2f}%      (before allowlist filter)")
        lines.append(f"  Rescued by allowlist: {acc - raw_acc:+6.2f} pp")
    elif n_allowed:
        lines.append(f"  Raw accuracy:         {raw_acc:6.2f}%      (allowlist hits but no rescue)")
    lines.append("")

    # ── Word counts ────────────────────────────────────────────────
    lines += [sub, "WORD COUNTS", sub]
    lines.append(f"  Raw PyMuPDF:   {pym_wc:>7,} words")
    lines.append(f"  LLM-cleaned:   {cnv_wc:>7,} words")
    lines.append(f"  Matching:      {match_wc:>7,} words")
    lines.append("")

    # ── Diff breakdown ─────────────────────────────────────────────
    lines += [sub, "DIFFERENCE BREAKDOWN", sub]
    lines.append(f"  Word differences:     {n_word:>4}  (counted against accuracy)")
    lines.append(f"  Transposed:           {n_trans:>4}  (counted against accuracy)")
    lines.append(f"  Punctuation:          {n_punct:>4}  (counted against accuracy)")
    lines.append(f"  Spacing:              {n_spacing:>4}  (IGNORED — whitespace only)")
    lines.append(f"  Repositioned blocks:  {n_repos:>4}  (IGNORED — content present, just moved)")
    lines.append(f"  Allowed removals:     {n_allowed:>4}  (IGNORED — intentional cleanup per state prompt)")
    lines.append("")

    # ── Allowed category breakdown ─────────────────────────────────
    if allowed_cats:
        lines += [sub, f"ALLOWED REMOVAL CATEGORIES ({n_allowed} items)", sub]
        for cat, n in sorted(allowed_cats.items(), key=lambda kv: -kv[1]):
            lines.append(f"  • {n:>3}  {cat}")
        lines += [
            "",
            "  These are intentional cleanups per the state cleaning prompt",
            "  (page numbers, line numbers, repeated headers/footers, etc.).",
            "  They are EXCLUDED from the accuracy calculation.",
            "",
        ]

    # ── Real differences ───────────────────────────────────────────
    lines += [sep, f"REAL DIFFERENCES ({len(real_diffs)})", sep]
    if not real_diffs:
        lines.append("  (none — all content present)")
    else:
        for idx, d in enumerate(real_diffs, 1):
            dtype = d.get("type", "?")
            sub_t = d.get("sub_type", "")
            page  = d.get("page")
            tag = f"{dtype}" + (f"/{sub_t}" if sub_t else "")
            page_str = f"  [Page {page}]" if page else ""
            if dtype == "MISSING_IN_CONVERTED":
                txt = d.get("missing_text") or d.get("pymupdf_text") or ""
                lines.append(f"  {idx:>3}. [{tag}]{page_str}  MISSING from clean output:")
                lines.append(f"        \"{txt}\"")
            elif dtype == "EXTRA_IN_CONVERTED":
                txt = d.get("extra_text") or d.get("converted_text") or ""
                lines.append(f"  {idx:>3}. [{tag}]{page_str}  EXTRA in clean output:")
                lines.append(f"        \"{txt}\"")
            elif dtype == "REPLACE":
                lines.append(f"  {idx:>3}. [{tag}]{page_str}  REPLACED:")
                lines.append(f"        Expected: \"{d.get('pymupdf_text','')}\"")
                lines.append(f"        Got:      \"{d.get('converted_text','')}\"")
                if sub_t == "TRANSPOSED":
                    for m in d.get("moved_words", []):
                        lines.append(f"          - pos {m.get('position')}: \"{m.get('expected')}\" → \"{m.get('got')}\"")
                elif sub_t == "PUNCTUATION":
                    for cd in d.get("char_diffs", []):
                        lines.append(f"          - {cd}")
            else:
                lines.append(f"  {idx:>3}. [{tag}]  {d}")
            lines.append("")

    # ── Repositioned blocks ────────────────────────────────────────
    lines += [sep, f"REPOSITIONED BLOCKS ({len(repositioned)})", sep]
    if not repositioned:
        lines.append("  (none)")
    else:
        for idx, r in enumerate(repositioned, 1):
            txt = r.get("text") or r.get("missing_text") or r.get("extra_text") or str(r)
            txt_short = txt if len(txt) <= 200 else txt[:197] + "..."
            lines.append(f"  {idx:>3}. {txt_short}")
    lines.append("")

    # ── Allowed removals (full listing) ────────────────────────────
    lines += [sep, f"ALLOWED REMOVALS ({len(allowed)})", sep]
    if not allowed:
        lines.append("  (none — no artifact removals detected)")
    else:
        for idx, d in enumerate(allowed, 1):
            cat = d.get("allowed_category") or d.get("category") or "?"
            rule = d.get("allowed_rule") or d.get("rule") or ""
            txt = (
                d.get("missing_text")
                or d.get("extra_text")
                or d.get("pymupdf_text")
                or d.get("converted_text")
                or d.get("text")
                or ""
            )
            txt_short = txt if len(txt) <= 200 else txt[:197] + "..."
            lines.append(f"  {idx:>3}. [{cat}]  \"{txt_short}\"")
            if rule:
                lines.append(f"        Rule: {rule}")
    lines.append("")

    # ── Spacing-only differences (ignored, but logged for audit) ───
    if spacing:
        lines += [sep, f"SPACING-ONLY DIFFERENCES ({len(spacing)}, ignored)", sep]
        for idx, d in enumerate(spacing, 1):
            txt = (
                d.get("missing_text")
                or d.get("extra_text")
                or d.get("pymupdf_text")
                or d.get("converted_text")
                or ""
            )
            txt_short = txt if len(txt) <= 200 else txt[:197] + "..."
            lines.append(f"  {idx:>3}. \"{txt_short}\"")
        lines.append("")

    lines.append(sep)
    lines.append("END OF REPORT")
    lines.append(sep)
    return "\n".join(lines)


def _render_extraction_anomaly_banner(anomalies) -> None:
    """Render a Streamlit banner + expander for extraction anomalies.

    Anomalies come from :func:`extraction_validator.validate_extraction`
    which is invoked by :func:`pymupdf_extractor.clean_extracted_text`
    as a read-only watchdog at the end of the cleaning pipeline.

    The banner is intentionally lightweight: it never blocks the
    pipeline, just makes regressions visible to QA before users discover
    them in downstream tools.  Severities are bucketed into Streamlit's
    three banner colours (error / warning / info) and grouped by kind
    so a single layout bug doesn't drown the panel in repeats.
    """
    if not anomalies:
        return
    try:
        # Group by (severity, kind) so repeats collapse into a count.
        from collections import Counter
        kind_counts = Counter((a.severity, a.kind) for a in anomalies)
        err_n = sum(c for (sev, _k), c in kind_counts.items() if sev == "error")
        warn_n = sum(c for (sev, _k), c in kind_counts.items() if sev == "warning")
        info_n = sum(c for (sev, _k), c in kind_counts.items() if sev == "info")

        headline = (
            f"🚨 {len(anomalies)} extraction anomaly{'ies' if len(anomalies) != 1 else ''} "
            f"detected ({err_n} error / {warn_n} warning / {info_n} info)"
        )
        if err_n:
            st.error(headline)
        elif warn_n:
            st.warning(headline)
        else:
            st.info(headline)

        with st.expander("Show extraction-validator details", expanded=False):
            st.caption(
                "These patterns indicate likely PyMuPDF layout-extraction "
                "issues (currency symbol order, split numbers, empty tags, "
                "etc.).  Each row links the offending snippet to the "
                "approximate page so you can verify against the source PDF."
            )
            # Per-kind summary table first.
            rows = []
            for (sev, kind), count in sorted(
                kind_counts.items(),
                key=lambda kv: (-kv[1], kv[0][1]),
            ):
                # Pull the first description we see for this kind.
                desc = next(
                    (a.description for a in anomalies if a.kind == kind),
                    "",
                )
                rows.append(
                    {"Severity": sev, "Kind": kind, "Count": count, "Description": desc}
                )
            try:
                import pandas as _pd
                st.dataframe(_pd.DataFrame(rows), use_container_width=True, hide_index=True)
            except Exception:
                for r in rows:
                    st.write(f"- **{r['Severity']}** `{r['Kind']}` (×{r['Count']}): {r['Description']}")
            # Then the raw snippets (capped to 50 to keep the UI snappy).
            st.markdown("**First snippets**")
            for a in anomalies[:50]:
                page = f" p.{a.page_number}" if a.page_number is not None else ""
                st.code(f"[{a.severity}] {a.kind}{page} @offset={a.char_offset}\n  {a.snippet}")
            if len(anomalies) > 50:
                st.caption(f"… {len(anomalies) - 50} more not shown (see saved JSON for full list).")
    except Exception as _exc:
        # The banner is a courtesy — never let it crash the pipeline.
        logging.warning(f"Anomaly banner render failed (non-critical): {_exc}")


def _run_text_accuracy_check(
    raw_text: str | None,
    clean_text: str | None,
    state_code: str | None,
    add_log,
    *,
    save_removals_to: str | None = None,
    save_accuracy_to: str | None = None,
    llm_removed_lines: list[str] | None = None,
    pdf_path: str | None = None,
):
    """Run compare_texts() with state-aware artifact allowlist + emit log.

    Shared by all three processing pipelines (visual, pymupdf_clean_only,
    hybrid) so the text-accuracy step looks identical everywhere.

    Parameters
    ----------
    raw_text
        Raw PyMuPDF extraction (with page markers, line numbers, headers).
        Used as the ground-truth side of the diff.
    clean_text
        LLM-cleaned output (the validation target).
    state_code
        Two-letter state code.  Passed to compare_texts so artifact lines
        like page markers / line numbers / state headers are bucketed as
        ``allowed_diffs`` instead of penalising accuracy.
    add_log
        The pipeline-local logging callable.
    save_removals_to
        Optional path.  When provided AND state_code is set, ``build_removals_report``
        is run on the raw vs clean texts and the structured JSON report is
        saved to disk for auditing.
    save_accuracy_to
        Optional path.  When provided, the full accuracy_report dict is
        serialised as JSON AND a human-readable ``.txt`` companion is written
        next to it (sibling ``.txt`` with the same stem).

    Returns
    -------
    dict | None
        The full accuracy_report dict (also stored on st.session_state).
        ``None`` if compare_texts isn't available or either text is empty.
    """
    if not (raw_text and clean_text and compare_texts):
        return None
    try:
        # ── Load PDF metadata for header/footer allowlist ─────────
        if pdf_path:
            try:
                from paragraph_accuracy_checker import extract_full_metadata
                from text_accuracy_helpers import set_pdf_metadata_allowlist
                _meta = extract_full_metadata(pdf_path)
                if _meta:
                    set_pdf_metadata_allowlist(_meta)
                    add_log("📋 PDF metadata loaded for header/footer allowlist")
                    # Save metadata JSON alongside accuracy artifacts
                    try:
                        import json as _json
                        _meta_path = str(Path(pdf_path).with_suffix('')) + "_metadata.json"
                        Path(_meta_path).parent.mkdir(parents=True, exist_ok=True)
                        with open(_meta_path, "w", encoding="utf-8") as _mf:
                            _json.dump(_meta, _mf, indent=2, ensure_ascii=False, default=str)
                        add_log(f"📋 PDF metadata saved: {Path(_meta_path).name}")
                    except Exception as _save_err:
                        logging.debug("Could not save PDF metadata: %s", _save_err)
            except Exception as _meta_err:
                logging.debug("Could not load PDF metadata for allowlist: %s", _meta_err)

        # Diagnostic: log input sizes so truncation bugs are immediately visible
        _raw_len = len(raw_text) if raw_text else 0
        _clean_len = len(clean_text) if clean_text else 0
        logging.info(
            "🔍 Text accuracy inputs: raw=%s chars, clean=%s chars (ratio=%.1f%%)",
            f"{_raw_len:,}", f"{_clean_len:,}",
            (_clean_len / max(_raw_len, 1)) * 100,
        )

        accuracy_report = compare_texts(raw_text, clean_text, state_code=state_code, llm_removed_lines=llm_removed_lines)
        st.session_state.content_accuracy_report = accuracy_report

        _acc       = accuracy_report['accuracy']
        _raw_acc   = accuracy_report.get('raw_accuracy', _acc)
        _diffs     = accuracy_report['real_diff_count']
        _allowed   = accuracy_report.get('allowed_diff_count', 0)
        _repos     = accuracy_report['repositioned_count']
        _n_trans   = accuracy_report.get('transposed_count', 0)
        _n_punct   = accuracy_report.get('punctuation_count', 0)
        _n_word    = accuracy_report.get('word_diff_count', _diffs)
        _icon      = "✅" if _acc >= 99 else "⚠️" if _acc >= 95 else "❌"

        _parts = []
        if _n_word:  _parts.append(f"{_n_word} word")
        if _n_trans: _parts.append(f"{_n_trans} transposed")
        if _n_punct: _parts.append(f"{_n_punct} punctuation")
        _brkdn = ", ".join(_parts) if _parts else f"{_diffs} diff(s)"

        _max_words = max(accuracy_report['pymupdf_word_count'], accuracy_report['converted_word_count'])
        add_log(
            f"{_icon} Text accuracy: {_acc:.1f}% "
            f"({accuracy_report['matching_words']:,}/{_max_words:,} words — "
            f"{_brkdn}, {_repos} repositioned)"
        )

        # Log page-level summary for missing content so users can navigate
        _real_diffs = accuracy_report.get('real_diffs', []) or []
        _missing_pages: dict[int, int] = {}
        for _d in _real_diffs:
            _pg = _d.get("page")
            if _pg and _d.get("type") in ("MISSING_IN_CONVERTED", "REPLACE"):
                _missing_pages[_pg] = _missing_pages.get(_pg, 0) + 1
        if _missing_pages:
            _pg_summary = ", ".join(
                f"p{pg}({n})" for pg, n in sorted(_missing_pages.items())
            )
            add_log(f"📄 Content differences by page: {_pg_summary}")

        if _allowed and state_code:
            _cat = accuracy_report.get('allowed_categories', {})
            _cat_str = ", ".join(f"{n} {k}" for k, n in sorted(_cat.items(), key=lambda kv: -kv[1]))
            add_log(
                f"🛡️ {_allowed} intentional removal(s) excluded from accuracy "
                f"({_cat_str or 'mixed'}) — raw_accuracy was {_raw_acc:.1f}%"
            )

        # Persist the structured removals report alongside the cleaned text
        if save_removals_to and state_code:
            try:
                from text_accuracy_helpers import build_removals_report
                import json as _json
                _rm_report = build_removals_report(raw_text, clean_text, state_code=state_code)
                with open(save_removals_to, "w", encoding="utf-8") as _rf:
                    _json.dump(_rm_report, _rf, indent=2, ensure_ascii=False)
                add_log(f"📋 Removals report saved: {os.path.basename(save_removals_to)}")
                # Warn if footers/headers/artifacts remain in the cleaned output
                _fw = _rm_report.get("footer_warnings", [])
                if _fw:
                    for _w in _fw[:5]:
                        _wcat = _w.get('category', 'artifact')
                        _wtype = _w.get('warning', 'not_removed')
                        add_log(f"⚠️ {_wcat} not removed (line {_w['line']}): {_w['text'][:80]}", "warning")
                    if len(_fw) > 5:
                        add_log(f"⚠️ ... and {len(_fw) - 5} more artifact(s) not removed", "warning")
                # Log repeating artifacts detected in raw PyMuPDF text
                _da = _rm_report.get("detected_artifacts", [])
                if _da:
                    add_log(f"🔍 Detected {len(_da)} repeating header/footer pattern(s) in raw PDF text")
                    for _a in _da[:3]:
                        _positions = ", ".join(_a.get("positions", []))
                        add_log(
                            f"   📌 {_positions} artifact ({_a['occurrences']} pages): "
                            f"{_a['text'][:80]}"
                        )
            except Exception as _rm_err:
                logging.warning(f"Failed to save removals report: {_rm_err}")

        # Persist the accuracy report — both a machine-readable .json and a
        # human-readable .txt sibling so users can quickly eyeball what's
        # going on without opening the Streamlit UI.
        if save_accuracy_to:
            try:
                import json as _json
                # JSON dump (full machine-readable report)
                with open(save_accuracy_to, "w", encoding="utf-8") as _af:
                    _json.dump(accuracy_report, _af, indent=2, ensure_ascii=False, default=str)
                # Sibling .txt with the human-readable summary
                _txt_path = os.path.splitext(save_accuracy_to)[0] + ".txt"
                _file_label = os.path.basename(_txt_path).replace("_accuracy.txt", ".txt")
                _txt_body = _format_accuracy_text_report(
                    accuracy_report,
                    file_label=_file_label,
                    state_code=state_code,
                )
                with open(_txt_path, "w", encoding="utf-8") as _tf:
                    _tf.write(_txt_body)
                add_log(f"📊 Accuracy report saved: {os.path.basename(_txt_path)}")
            except Exception as _ar_err:
                logging.warning(f"Failed to save accuracy report: {_ar_err}")

        return accuracy_report
    except Exception as acc_err:
        add_log(f"⚠️ Content accuracy check failed: {acc_err}", "warning")
        logging.error(f"Content accuracy error: {acc_err}", exc_info=True)
        return None


def _run_content_accuracy_check(
    precleaned_text: str | None,
    output_text: str | None,
    add_log,
    *,
    save_to: str | None = None,
    pdf_metadata: dict | None = None,
    pdf_path: str | None = None,
):
    """Run word-level content accuracy check (precleaned vs final output).

    This compares content-to-content (not raw extraction vs output),
    giving a true measure of whether the LLM preserved all legislative text.
    Each difference is classified: content_drop, paragraph_drop, content_added,
    hyphen_rejoin, header_reorder, artifact_removed, formatting_fix.

    Parameters
    ----------
    precleaned_text : str
        The precleaned/pre-LLM text (artifacts already removed by extraction).
    output_text : str
        The final LLM-cleaned output text.
    add_log : callable
        Pipeline logging function.
    save_to : str, optional
        Path to save the JSON report.

    Returns
    -------
    dict | None
        The content accuracy report, also stored on st.session_state.
    """
    if not precleaned_text or not output_text:
        return None

    # Strip TABLE markers — they are for human readability only
    import re as _re
    _tbl_re = _re.compile(r"^---TABLE (?:START|END)---\n?", _re.MULTILINE)
    precleaned_text = _tbl_re.sub("", precleaned_text)

    try:
        from content_accuracy_checker import check_content_accuracy
    except ImportError:
        logging.warning("content_accuracy_checker not available")
        return None

    # Load PDF metadata for header/footer recognition if not provided
    if pdf_metadata is None and pdf_path:
        try:
            import json as _json
            _meta_path = str(Path(pdf_path).with_suffix('')) + "_metadata.json"
            if Path(_meta_path).exists():
                with open(_meta_path, "r", encoding="utf-8") as _mf:
                    pdf_metadata = _json.load(_mf)
        except Exception:
            pass

    # Load detected artifacts from removals report if available
    _detected_artifacts = None
    if save_to:
        import json as _json2
        _removals_path = save_to.replace("_content_accuracy.json", "_removals.json")
        if Path(_removals_path).exists():
            try:
                with open(_removals_path, "r", encoding="utf-8") as _rf:
                    _rm_data = _json2.load(_rf)
                _detected_artifacts = _rm_data.get("detected_artifacts", [])
            except Exception:
                pass

    try:
        report = check_content_accuracy(precleaned_text, output_text, metadata=pdf_metadata, detected_artifacts=_detected_artifacts)
        st.session_state.content_accuracy_detail = report

        _acc = report["accuracy_pct"]
        _matching = report["matching_words"]
        _max_words = max(report["precleaned_words"], report["output_words"])
        _blocks = report["difference_blocks"]
        _issues = report.get("content_issue_count", 0)
        _cats = report.get("category_summary", {})

        _icon = "✅" if _acc >= 99 else "⚠️" if _acc >= 95 else "❌"

        # Build category breakdown string
        _cat_parts = []
        for cat, count in sorted(_cats.items()):
            _cat_parts.append(f"{count} {cat}")
        _cat_str = ", ".join(_cat_parts) if _cat_parts else "no differences"

        add_log(
            f"{_icon} Content accuracy: {_acc:.2f}% "
            f"({_matching:,}/{_max_words:,} words — "
            f"{_blocks} blocks: {_cat_str})"
        )

        if _issues > 0:
            add_log(
                f"⚠️ {_issues} content issue(s) detected — "
                f"check Content Accuracy report for details"
            )
            # Log each content issue
            for ci in report.get("content_issues", [])[:5]:
                _sev = ci.get("severity", "warning")
                _expl = ci.get("explanation", "")
                _log_level = "error" if _sev == "critical" else "warning"
                add_log(f"  ⚠️ {_expl[:150]}", _log_level)
            if _issues > 5:
                add_log(f"  ... and {_issues - 5} more issue(s)")

        # Save JSON report
        if save_to:
            import json as _json
            Path(save_to).parent.mkdir(parents=True, exist_ok=True)
            with open(save_to, "w", encoding="utf-8") as f:
                _json.dump(report, f, indent=2, ensure_ascii=False, default=str)
            add_log(f"📋 Content accuracy report saved: {Path(save_to).name}")

        return report

    except Exception as e:
        add_log(f"⚠️ Content accuracy check failed: {e}", "warning")
        logging.error(f"Content accuracy error: {e}", exc_info=True)
        return None


def _run_llm_diff_verification(
    accuracy_report: dict | None,
    highlighted_images_b64: list | None,
    state_code: str | None,
    add_log,
    *,
    save_accuracy_to: str | None = None,
):
    """Optionally run the LLM diff verifier on residual ``real_diffs``.

    Acts as a SECOND-OPINION layer on top of the regex-based artifact
    allowlist already applied by :func:`_run_text_accuracy_check`.  When
    enabled (sidebar toggle ``use_llm_diff_verification``), the residual
    ``real_diffs`` list is sent to Claude Opus 4.7 along with the highlighted
    page images.  Each diff is classified as a page artifact or real
    legislative content; the safety policy then either:

      - **AUTO-DEMOTES** diffs that pass ALL four guards (verdict=artifact,
        confidence=high, category ∈ {header, footer, decorative_rule,
        page_number}, ≤ 15 words) — silently moves them to ``allowed_diffs``.
      - **ATTACHES A REVIEW-AID** suggestion on everything else (the diff
        STAYS as ``real_diff`` so a human reviews it; the LLM's hint is
        surfaced in the UI for context).

    The LLM can therefore ONLY DEMOTE, NEVER PROMOTE — a diff classified
    by the existing regex layer as ``allowed`` stays allowed.

    Parameters
    ----------
    accuracy_report
        The dict from :func:`compare_files.compare_texts`.  Mutated in place.
        ``None`` (or missing ``real_diffs``) → no-op.
    highlighted_images_b64
        Base64 PNGs of the highlighted PDF pages.  These are the same images
        already loaded for the tag-accuracy audit, so this call adds no I/O.
        Empty / ``None`` → no-op.
    state_code
        Two-letter state code (informational; passed to the LLM prompt).
    add_log
        Pipeline-local logger.
    save_accuracy_to
        Optional JSON path.  When provided, the updated accuracy report is
        re-serialised so the on-disk artifact reflects the LLM-corrected
        verdicts (matches the human-readable .txt sibling rule below).

    Returns
    -------
    dict | None
        The (mutated) accuracy_report dict.  Returns the input unchanged on
        any failure — LLM verification is BEST EFFORT and never breaks the
        pipeline.
    """
    # ── Opt-in gate (sidebar toggle, default OFF for first deployment) ──
    if not st.session_state.get("use_llm_diff_verification", False):
        return accuracy_report

    if not accuracy_report:
        return accuracy_report
    real_diffs = accuracy_report.get("real_diffs") or []
    if not real_diffs:
        # Nothing to verify — regex layer already cleared everything.
        return accuracy_report
    if not highlighted_images_b64:
        add_log("ℹ️ LLM diff verifier: no page images available — skipping.")
        return accuracy_report

    try:
        # Ensure Opus4.6/ is on sys.path so we can import the verifier.
        opus46_dir = os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         '..', 'Opus4.6')
        )
        if opus46_dir not in sys.path:
            sys.path.insert(0, opus46_dir)
        from llm_diff_verifier import (
            verify_real_diffs_with_llm,
            apply_llm_verifications_to_report,
        )
    except Exception as imp_err:
        add_log(f"⚠️ LLM diff verifier import failed: {imp_err}", "warning")
        logging.error(f"LLM verifier import error: {imp_err}", exc_info=True)
        return accuracy_report

    try:
        add_log(
            f"🤖 LLM diff verifier: classifying {len(real_diffs)} residual "
            f"real_diff(s) against {len(highlighted_images_b64)} page images "
            f"(state={state_code or '?'}, auto-demote whitelist: "
            f"header/footer/decorative_rule/page_number @ high confidence ≤15 words)..."
        )
        verifications = verify_real_diffs_with_llm(
            real_diffs=real_diffs,
            page_images_b64=highlighted_images_b64,
            state_code=state_code,
            log_callable=add_log,
        )
        if not verifications:
            add_log("ℹ️ LLM diff verifier: no verifications returned — leaving report unchanged.")
            return accuracy_report

        accuracy_report = apply_llm_verifications_to_report(
            accuracy_report,
            verifications,
            log_callable=add_log,
        )
        # Push the updated report back to session_state so the UI sees it.
        st.session_state.content_accuracy_report = accuracy_report

        # Re-persist the JSON artifact so the on-disk copy reflects the
        # LLM-corrected verdicts (the .txt sibling is regenerated below).
        if save_accuracy_to:
            try:
                import json as _json
                with open(save_accuracy_to, "w", encoding="utf-8") as _af:
                    _json.dump(accuracy_report, _af, indent=2,
                               ensure_ascii=False, default=str)
                _txt_path = os.path.splitext(save_accuracy_to)[0] + ".txt"
                _file_label = os.path.basename(_txt_path).replace("_accuracy.txt", ".txt")
                _txt_body = _format_accuracy_text_report(
                    accuracy_report,
                    file_label=_file_label,
                    state_code=state_code,
                )
                with open(_txt_path, "w", encoding="utf-8") as _tf:
                    _tf.write(_txt_body)
                add_log(f"📊 Accuracy report re-saved after LLM verification: "
                        f"{os.path.basename(_txt_path)}")
            except Exception as _save_err:
                logging.warning(f"Failed to re-save accuracy report after LLM "
                                f"verification: {_save_err}")
    except Exception as v_err:
        add_log(f"⚠️ LLM diff verifier failed (continuing without): {v_err}",
                "warning")
        logging.error(f"LLM verifier error: {v_err}", exc_info=True)

    return accuracy_report


def process_with_claude_opus(pdf_path, state_code, session_id, progress_placeholder=None, status_placeholder=None, log_placeholder=None):
    """Process PDF using Claude Opus 4.6 visual conversion (image-based, page by page)"""
    import importlib.util

    start_time = time.time()
    session_mgr = st.session_state.session_manager
    session_folders = session_mgr.get_session_folders(session_id)

    # Clear previous logs
    st.session_state.processing_logs = []

    # 8 steps to match the shared show_processing_steps() display
    steps_labels = [
        {"name": "Initialize Claude Opus",    "icon": "🤖"},
        {"name": "Detect State",              "icon": "🔍"},
        {"name": "Build Conversion Prompt",   "icon": "📋"},
        {"name": "Visual Conversion",         "icon": "👁️"},
        {"name": "Save Output",              "icon": "💾"},
        {"name": "PyMuPDF Extraction",        "icon": "📄"},
        {"name": "Claude LLM Clean",          "icon": "🧹"},
        {"name": "GPT Audit",                 "icon": "🤖"},
    ]
    st.session_state.step_status = {i: "⏳" for i in range(8)}

    def add_log(message, level="info"):
        """Add log message to processing logs and update live display"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        step_idx = st.session_state.get('current_step', 0)
        st.session_state.processing_logs.append(
            {"time": timestamp, "message": message, "level": level, "step": step_idx}
        )
        logging.info(message)
        # Live-update the log placeholder with styled activity feed
        if log_placeholder is not None:
            with log_placeholder.container():
                st.markdown(_build_activity_feed_html(st.session_state.processing_logs), unsafe_allow_html=True)

    def update_progress():
        if progress_placeholder:
            with progress_placeholder.container():
                show_processing_steps()

    session_mgr.start_processing(session_id)
    add_log("Starting Claude Opus 4.6 processing...")

    try:
        # ── Load the Opus4.6 module via importlib (filename contains dots) ──────
        opus_module_path = os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         '..', 'Opus4.6', 'Claude_Opus_4.6_Test.py')
        )
        spec = importlib.util.spec_from_file_location("claude_opus_converter", opus_module_path)
        opus = importlib.util.module_from_spec(spec)
        # Guard stdout/stderr so Streamlit's streams aren't replaced by the module's win32 patch
        _orig_stdout, _orig_stderr = sys.stdout, sys.stderr
        spec.loader.exec_module(opus)
        sys.stdout, sys.stderr = _orig_stdout, _orig_stderr

        # ── Step 1: Initialise client ────────────────────────────────────────────
        st.session_state.current_step = 0
        st.session_state.step_status[0] = "🔄"
        update_progress()
        add_log("🤖 Initializing Claude Opus 4.6 client...")
        if status_placeholder:
            status_placeholder.info("🤖 Initializing Claude Opus 4.6 client...")

        client = opus.get_anthropic_client()
        if not client:
            raise Exception("Failed to initialize Anthropic client")

        st.session_state.step_status[0] = "✅"
        update_progress()
        add_log("✅ Claude Opus client initialized")

        # ── Step 2: Detect State ──────────────────────────────────────────────────
        st.session_state.current_step = 1
        st.session_state.step_status[1] = "🔄"
        update_progress()
        add_log("🔍 Detecting state from filename...")
        if status_placeholder:
            status_placeholder.info("🔍 Detecting state from filename...")

        # Reuse the state detected at upload time (page-1 text scan) if we
        # have one — saves a redundant filename + Vision-API round-trip.
        _pre_detected = st.session_state.get('detected_state')
        if _pre_detected:
            detected_state = _pre_detected
            add_log(f"✅ Using pre-detected state from upload: {detected_state}")
        else:
            # Try numerical ID from filename first (instant, no API call)
            detected_state = detect_state_from_numerical_id(pdf_path) if detect_state_from_numerical_id else None
            if detected_state:
                add_log(f"✅ State detected from filename numerical ID: {detected_state}")
            else:
                # Fall back to filename pattern match via opus
                detected_state = opus.detect_state_from_filename(pdf_path)
                if detected_state:
                    add_log(f"✅ State detected from filename pattern: {detected_state}")

        # Store and display the detected state
        st.session_state.detected_state = detected_state
        if detected_state:
            _state_name = US_STATE_NAMES.get(detected_state, detected_state)
            add_log(f"✅ Detected state: {detected_state} — {_state_name}")
            if status_placeholder:
                status_placeholder.success(f"🏛️ Detected State: **{detected_state} — {_state_name}**")
        else:
            add_log("⚠️ Could not detect state — using default prompt", "warning")
            if status_placeholder:
                status_placeholder.warning("⚠️ Could not detect state — using default conversion prompt")

        st.session_state.step_status[1] = "✅"
        update_progress()

        # ── Step 3: Build conversion prompt ──────────────────────────────────────
        st.session_state.current_step = 2
        st.session_state.step_status[2] = "🔄"
        update_progress()
        add_log("📋 Building state-specific conversion prompt...")
        if status_placeholder:
            status_placeholder.info("📋 Building state-specific conversion prompt...")

        state_config = opus.load_state_template(detected_state) if detected_state else None
        prompt = opus.get_conversion_prompt(state_config)

        st.session_state.step_status[2] = "✅"
        update_progress()
        add_log(f"✅ Conversion prompt built ({len(prompt)} chars)")

        # ── Step 4: Visual conversion via Claude Opus ────────────────────────────
        st.session_state.current_step = 3
        st.session_state.step_status[3] = "🔄"
        update_progress()
        add_log("👁️ Running Claude Opus 4.6 visual conversion (this may take a few minutes)...")
        if status_placeholder:
            status_placeholder.info("👁️ Running Claude Opus 4.6 visual conversion (this may take a few minutes)...")

        result_data = opus.convert_pdf_to_text(client, pdf_path, prompt)
        # Handle both tuple (text, images) and plain text return
        if isinstance(result_data, tuple):
            converted_text, cached_page_images = result_data
        else:
            converted_text = result_data
            cached_page_images = None
        if not converted_text:
            raise Exception("Claude Opus visual conversion returned an empty result")

        st.session_state.step_status[3] = "✅"
        update_progress()
        add_log(f"✅ Visual conversion complete: {len(converted_text)} characters")

        # ── Step 4b: Deterministic cleanup of common Claude inconsistencies ─────
        converted_text = opus.deterministic_cleanup(converted_text, state_config=state_config, state_code=detected_state)
        add_log("🔧 Deterministic cleanup complete (markdown→tags, malformed tags, etc.)")

        # ── Step 4c: Post-process — strip tags from protected clauses ────────────
        if state_config:
            converted_text = opus.post_process_output(converted_text, state_config)
            add_log("🔧 Post-processing: stripped tags from protected clauses (if any)")
        if strip_protected_tags and detected_state:
            _before_strip = converted_text
            converted_text = strip_protected_tags(converted_text, detected_state)
            if converted_text != _before_strip:
                add_log(f"🛡️ Stripped tags from protected clauses for {detected_state}")

        # ── Step 4d: Merge paragraphs split at page breaks ──────────────────────
        # Claude's visual conversion sometimes splits a paragraph into two
        # when text wraps across a page boundary (e.g. a sentence that starts
        # at the bottom of page 3 and continues at the top of page 4 ends up
        # as two separate paragraphs).  The hybrid pipeline already handles
        # this via _merge_page_break_paragraph_splits(); apply the same fix
        # to the visual-mode output so CO (and any future vision-only state)
        # gets consistent paragraph joining.
        _before_merge = converted_text
        converted_text = opus._merge_page_break_paragraph_splits(
            converted_text, state_code=detected_state
        )
        if converted_text != _before_merge:
            add_log("🔗 Merged paragraphs split at page breaks")

        # ── Step 5: Save output ──────────────────────────────────────────────────
        st.session_state.current_step = 4
        st.session_state.step_status[4] = "🔄"
        update_progress()
        add_log("💾 Saving output...")
        if status_placeholder:
            status_placeholder.info("💾 Saving output...")

        output_filename = Path(pdf_path).stem + ".txt"
        output_path = os.path.join(session_folders['output_folder'], output_filename)
        os.makedirs(session_folders['output_folder'], exist_ok=True)

        if not opus.save_text_file(converted_text, output_path):
            raise Exception(f"Failed to save output to {output_path}")

        st.session_state.step_status[4] = "✅"
        update_progress()
        add_log(f"✅ Output saved: {output_path}")

        # ── Conversion milestone — record time for Phase 1 ───────────────
        conversion_elapsed = time.time() - start_time
        st.session_state.conversion_elapsed_secs = conversion_elapsed
        _cm, _cs = int(conversion_elapsed // 60), int(conversion_elapsed % 60)
        st.session_state.conversion_time = f"{_cm}m {_cs}s" if _cm > 0 else f"{_cs}s"
        add_log(f"🏁 PDF Conversion complete in {st.session_state.conversion_time}")
        if status_placeholder:
            status_placeholder.success(f"✅ Conversion complete ({st.session_state.conversion_time}) — now generating validation report…")

        # ── Validation gate ───────────────────────────────────────────────
        # Per product requirements, certain states do NOT need the
        # PyMuPDF/GPT validation pipeline (it's expensive and not useful
        # for them).  For those states we skip Steps 6-8 entirely and go
        # straight to the "Wrap up" stage.  The downstream "Step 7: Claude
        # LLM Clean", "Content Accuracy Check" and "Step 8: GPT Audit"
        # blocks are already guarded by ``if pymupdf_text``,
        # ``if pymupdf_clean_text``, etc., so leaving those unset here is
        # enough to make them all skip cleanly.
        _skip_validation_states: set = set()  # Previously {"CO"} — now ALL CAPS detection provides ground truth
        _skip_audit = (
            (detected_state or "").upper() in _skip_validation_states
            or st.session_state.get('_pdf_is_scanned', False)
        )

        # Default values that downstream steps will use if Step 6 is skipped
        pymupdf_text = None
        pymupdf_report = None
        pymupdf_clean_text = None
        highlight_dir = None

        if _skip_audit:
            if st.session_state.get('_pdf_is_scanned', False):
                add_log(
                    "⏭️ Validation report skipped — Scanned PDF requires human review"
                )
            else:
                _state_lbl = US_STATE_NAMES.get(detected_state, detected_state)
                add_log(
                    f"⏭️ Validation report skipped — {_state_lbl} ({detected_state}) "
                    f"uses Visual mode without audit"
                )
            # Mark optional steps complete so the UI doesn't show "in progress"
            for _s_idx in (5, 6, 7):
                if _s_idx in st.session_state.get('step_status', {}):
                    st.session_state.step_status[_s_idx] = "⏭️"
            update_progress()

        # ── Step 6: PyMuPDF Extraction ───────────────────────────────────────────
        if not _skip_audit:
            st.session_state.current_step = 5
            st.session_state.step_status[5] = "🔄"
            update_progress()
            add_log("📄 Running PyMuPDF extraction (JSON report + tagged text + highlighted images)...")
            if status_placeholder:
                status_placeholder.info("📄 Running PyMuPDF extraction...")

            # Reset audit data
            st.session_state.audit_excel_path = None
            st.session_state.audit_counts = None
            st.session_state.audit_highlighted_dir = None
            st.session_state.audit_json_report = None
            st.session_state.content_accuracy_report = None
            st.session_state.gpt_audit_failed = False

            # Reset validation review fixed/ignored state from any previous run
            for _vri_key in ("vri_fixed_ids", "vri_ignored_ids", "vri_undo_history",
                             "vri_selected_item", "vri_current_page", "vri_filter_mode",
                             "vri_last_sel", "vri_edit_mode", "vri_file_key",
                             "vri_per_file_state"):
                st.session_state.pop(_vri_key, None)

            try:
                from pymupdf_extractor import (
                    extract_pdf_json_report,
                    extract_pdf_with_pymupdf,
                    highlight_pdf_formatting,
                    clean_extracted_text,
                    consolidate_adjacent_tags,
                    merge_cross_page_break_tags,
                    split_ar_subsection_welds,
                )

                # Determine state-specific marking flags
                _mark_flags = {}
                if state_config:
                    add_cfg = state_config.get("add_markup", {})
                    del_cfg = state_config.get("delete_markup", {})
                    _mark_flags["mark_underline"] = add_cfg.get("underline", True)
                    _mark_flags["mark_italic"] = add_cfg.get("italic", False) or del_cfg.get("italic", False) or bool(add_cfg.get("italic_section_only"))
                    _mark_flags["mark_bold"] = add_cfg.get("bold", False)
                    _mark_flags["mark_strikethrough"] = del_cfg.get("strike_out", True)
                    _mark_flags["mark_brackets"] = add_cfg.get("brackets", False) or del_cfg.get("brackets", False)

                # 6a: JSON report
                pymupdf_report = extract_pdf_json_report(pdf_path, state_code=detected_state, **_mark_flags)
                n_spans = len(pymupdf_report.get("formatted_spans", []))
                add_log(f"📊 PyMuPDF JSON report: {n_spans} formatted spans detected")

                # Save JSON report alongside output
                pymupdf_json_path = output_path.replace(".txt", "_pymupdf_report.json") if isinstance(output_path, str) else str(output_path).replace(".txt", "_pymupdf_report.json")
                import json as _json
                with open(pymupdf_json_path, "w", encoding="utf-8") as jf:
                    _json.dump(pymupdf_report, jf, indent=2, ensure_ascii=False)
                add_log(f"✅ JSON report saved: {pymupdf_json_path}")

                # 6b: Tagged text
                pymupdf_text = extract_pdf_with_pymupdf(pdf_path, join_pages=True, state_code=detected_state, **_mark_flags)
                pymupdf_txt_path = output_path.replace(".txt", "_pymupdf.txt") if isinstance(output_path, str) else str(output_path).replace(".txt", "_pymupdf.txt")
                with open(pymupdf_txt_path, "w", encoding="utf-8") as tf:
                    tf.write(pymupdf_text)
                add_log(f"✅ PyMuPDF tagged text saved: {len(pymupdf_text):,} chars")

                # 6b.1: Filter italic spans by section scope (e.g. Indiana PRINTING CODE only)
                pymupdf_report, pymupdf_text = _filter_italic_by_section(pymupdf_report, pymupdf_text, state_config)
                _n_after = len(pymupdf_report.get("formatted_spans", [])) if pymupdf_report else 0
                if _n_after != n_spans:
                    add_log(f"🔍 Italic section filter: {n_spans} → {_n_after} spans (removed {n_spans - _n_after} out-of-scope italic spans)")

                # 6c: Highlighted page images
                highlight_dir = Path(output_path).parent / f"{Path(pdf_path).stem}_highlighted"
                hi_imgs = highlight_pdf_formatting(pdf_path, report=pymupdf_report, output_dir=highlight_dir, **_mark_flags)
                add_log(f"✅ {len(hi_imgs)} highlighted page images saved to {highlight_dir}")

                st.session_state.audit_highlighted_dir = str(highlight_dir)
                st.session_state.audit_json_report = pymupdf_report
                st.session_state.step_status[5] = "✅"
            except Exception as pmu_err:
                add_log(f"⚠️ PyMuPDF extraction failed: {pmu_err}", "warning")
                logging.error(f"PyMuPDF extraction error: {pmu_err}", exc_info=True)
                st.session_state.step_status[5] = "⚠️"
                pymupdf_text = None
                pymupdf_report = None
            update_progress()

        # ── Step 7: Claude LLM Clean ─────────────────────────────────────────────
        pymupdf_clean_text = None
        if pymupdf_text:
            st.session_state.current_step = 6
            st.session_state.step_status[6] = "🔄"
            update_progress()
            add_log("🧹 Cleaning PyMuPDF text with Claude (removing headers/footers/line numbers)...")
            if status_placeholder:
                status_placeholder.info("🧹 Cleaning PyMuPDF text with Claude Opus...")

            try:
                pymupdf_clean_text = clean_extracted_text(
                    pymupdf_text,
                    state_code=detected_state,
                    pdf_path=pdf_path,
                    anthropic_client=client,
                    mark_brackets=_mark_flags.get("mark_brackets", False),
                )
                pymupdf_clean_text = consolidate_adjacent_tags(
                    pymupdf_clean_text, state_code=detected_state
                )
                pymupdf_clean_text = opus.remove_nested_tags(pymupdf_clean_text)
                pymupdf_clean_text = opus.merge_split_tags(pymupdf_clean_text)
                if detected_state and detected_state.upper() == "AR":
                    pymupdf_clean_text = split_ar_subsection_welds(pymupdf_clean_text)
                # Merge same-paragraph tags split across a page break.
                pymupdf_clean_text = merge_cross_page_break_tags(pymupdf_clean_text)
                pymupdf_clean_text = opus.split_tagged_subsections(pymupdf_clean_text)

                # ── Post-process: strip tags from state-specific protected clauses ──
                if strip_protected_tags and detected_state:
                    _before_strip = pymupdf_clean_text
                    pymupdf_clean_text = strip_protected_tags(pymupdf_clean_text, detected_state)
                    if pymupdf_clean_text != _before_strip:
                        add_log(f"🛡️ Stripped tags from protected clauses for state {detected_state}")

                pymupdf_clean_path = output_path.replace(".txt", "_pymupdf_clean.txt") if isinstance(output_path, str) else str(output_path).replace(".txt", "_pymupdf_clean.txt")
                with open(pymupdf_clean_path, "w", encoding="utf-8") as cf:
                    cf.write(pymupdf_clean_text)
                add_log(f"✅ Clean text saved: {len(pymupdf_text):,} → {len(pymupdf_clean_text):,} chars")

                # Save LLM diff report (what did the LLM remove/change?)
                _diff_report = getattr(clean_extracted_text, '_last_diff_report', None)
                if _diff_report:
                    diff_path = pymupdf_clean_path.replace("_pymupdf_clean.txt", "_clean_diff.txt")
                    with open(diff_path, "w", encoding="utf-8") as df:
                        df.write(_diff_report)
                    add_log(f"📋 LLM diff report saved: {diff_path}")

                _preclean_guard = getattr(clean_extracted_text, '_last_preclean_guard', None)
                if _preclean_guard:
                    guard_path = pymupdf_clean_path.replace("_pymupdf_clean.txt", "_preclean_guard.txt")
                    with open(guard_path, "w", encoding="utf-8") as gf:
                        gf.write(_json.dumps(_preclean_guard, indent=2, ensure_ascii=False))
                    if _preclean_guard.get("ok"):
                        add_log("✅ Pre-clean guard passed (only line-number artifacts removed)")
                    else:
                        add_log(
                            f"⚠️ Pre-clean guard failed: {_preclean_guard.get('unexpected_removed_count', 0)} unexpected removals",
                            "warning",
                        )
                    add_log(f"📋 Pre-clean guard report saved: {guard_path}")

                # Rebuild report context using cleaned text for better anchor matching
                if pymupdf_report:
                    try:
                        from pymupdf_extractor import rebuild_report_context
                        pymupdf_report = rebuild_report_context(pymupdf_report, pymupdf_clean_text)
                        # Re-save the updated report
                        with open(pymupdf_json_path, "w", encoding="utf-8") as jf:
                            _json.dump(pymupdf_report, jf, indent=2, ensure_ascii=False)
                        add_log("🔄 Report context rebuilt from cleaned text")
                        st.session_state.audit_json_report = pymupdf_report
                    except Exception as _ctx_err:
                        logging.warning(f"Context rebuild failed (non-critical): {_ctx_err}")

                st.session_state.step_status[6] = "✅"

                # Restore special symbols dropped by Claude vision
                converted_before = converted_text
                converted_text = opus.restore_special_symbols(converted_text, pymupdf_clean_text)
                if converted_text != converted_before:
                    add_log("🔧 Restored special symbols (§ etc.) from PyMuPDF reference")
                    with open(output_path, "w", encoding="utf-8") as f:
                        f.write(converted_text)
            except Exception as clean_err:
                add_log(f"⚠️ LLM clean failed: {clean_err}", "warning")
                logging.error(f"LLM clean error: {clean_err}", exc_info=True)
                st.session_state.step_status[6] = "⚠️"
            update_progress()
        else:
            st.session_state.step_status[6] = "⚠️"
            update_progress()

        # ── Content Accuracy Check (between Step 7 and Step 8) ─────────────────
        # Compare the RAW PyMuPDF extraction (with line numbers, headers,
        # footers, page markers still in it) to the final Visual-mode Claude
        # output.  state_code lets compare_texts bucket intentional artifact
        # removals into ``allowed_diffs`` so they don't count against accuracy.
        st.session_state.content_accuracy_report = None

        # Save precleaned file (pymupdf_clean_text = line numbers/headers stripped)
        _pre_cleaned_for_accuracy = pymupdf_clean_text if pymupdf_clean_text else pymupdf_text
        if pymupdf_clean_text and output_path:
            _pre_clean_path = output_path.replace(".txt", "_pre_cleaned.txt")
            with open(_pre_clean_path, "w", encoding="utf-8") as _pcf:
                _pcf.write(pymupdf_clean_text)
            add_log(f"📋 Pre-cleaned text saved: {len(pymupdf_text):,} → {len(pymupdf_clean_text):,} chars")

        # Content accuracy — word-level precleaned vs output comparison (Visual mode)
        _content_acc_json = output_path.replace(".txt", "_content_accuracy.json") if output_path else None
        _run_content_accuracy_check(
            precleaned_text=_pre_cleaned_for_accuracy,
            output_text=converted_text,
            add_log=add_log,
            save_to=_content_acc_json,
            pdf_path=st.session_state.get('temp_pdf_path'),
        )

        # Paragraph accuracy (if future was submitted earlier)
        _para_report = None
        if '_fut_para' in dir() and _fut_para is not None:
            try:
                _para_report = _fut_para.result()
                _para_executor.shutdown(wait=False)
            except Exception:
                pass

        if _para_report:
            _para_acc = _para_report.get("accuracy_pct", 100.0)
            _splits = len(_para_report.get("splits_needed", []))
            _merges = len(_para_report.get("merges_needed", []))
            _total = _para_report.get("total_boundaries", 0)
            st.session_state.paragraph_accuracy_report = _para_report
            if _splits or _merges:
                add_log(f"📐 Paragraph accuracy: {_para_acc:.1f}% ({_total} boundaries — {_splits} splits, {_merges} merges needed)")
            else:
                add_log(f"📐 Paragraph accuracy: {_para_acc:.1f}% ({_total} boundaries — all correct)")
            if _para_json:
                import json as _json
                with open(_para_json, "w", encoding="utf-8") as _pf:
                    _json.dump(_para_report, _pf, indent=2, default=str)

        # ── Step 8: Tag Validation Audit (pymupdf_compare) ─────────────────────
        if pymupdf_clean_text and pymupdf_report and highlight_dir and Path(highlight_dir).exists():
            n_spans = len(pymupdf_report.get("formatted_spans", []))
            if n_spans == 0:
                st.session_state.step_status[7] = "⏭️"
                add_log("⏭️ Tag audit skipped — 0 formatted spans detected")
                st.session_state.audit_counts = {
                    'total_checklist': 0, 'passes': 0, 'warnings': 0,
                    'errors': 0, 'tag_accuracy_pct': 100.0,
                }
            else:
                st.session_state.current_step = 7
                st.session_state.step_status[7] = "🔄"
                update_progress()
                add_log("🤖 Running Claude Opus 4.7 tag validation audit...")
                if status_placeholder:
                    status_placeholder.info("🤖 Running tag validation audit (comparing PyMuPDF report vs converted text)...")

                try:
                    opus46_dir = os.path.normpath(
                        os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'Opus4.6')
                    )
                    if opus46_dir not in sys.path:
                        sys.path.insert(0, opus46_dir)
                    from pymupdf_compare import pymupdf_compare_tags

                    import base64 as _b64
                    hi_images_b64 = []
                    for img_file in sorted(Path(highlight_dir).glob("page_*.png")):
                        hi_images_b64.append(_b64.b64encode(img_file.read_bytes()).decode())
                    add_log(f"📷 Loaded {len(hi_images_b64)} highlighted page images for audit")

                    # Build state rules description
                    sr_parts = []
                    if state_config:
                        sr_parts.append(f"State: {state_config.get('state_name', detected_state or 'unknown')}")
                        a_cfg = state_config.get("add_markup", {})
                        d_cfg = state_config.get("delete_markup", {})
                        sr_parts.append(f"Underline={a_cfg.get('underline', False)}")
                        sr_parts.append(f"Strikethrough={d_cfg.get('strike_out', False)}")
                        sr_parts.append(f"Italic={a_cfg.get('italic', False)}")
                        sr_parts.append(f"Bold={a_cfg.get('bold', False)}")
                        sr_parts.append(f"AllCaps={a_cfg.get('all_caps', False)}")
                        if d_cfg.get('strike_out', False) and (
                            a_cfg.get('underline', False) or a_cfg.get('italic', False)
                            or a_cfg.get('bold', False)
                        ):
                            sr_parts.append(
                                "PRIORITY: strikethrough + any other formatting → tag "
                                "as [D>...<D] only, never [A>...<A]"
                            )
                    state_rules_str = ", ".join(sr_parts) if sr_parts else f"State: {detected_state or 'unknown'}"

                    audit_xlsx = output_path.replace(".txt", "_tag_validation.xlsx") \
                        if isinstance(output_path, str) \
                        else str(output_path).replace(".txt", "_tag_validation.xlsx")

                    audit_summary = pymupdf_compare_tags(
                        highlighted_images_b64=hi_images_b64,
                        pymupdf_report=pymupdf_report,
                        pymupdf_clean_text=converted_text,
                        state_code=detected_state or "XX",
                        state_rules=state_rules_str,
                        output_xlsx=audit_xlsx,
                    )

                    st.session_state.audit_excel_path = audit_xlsx
                    st.session_state.audit_counts = {
                        'total_checklist': audit_summary.get('total_checklist', 0),
                        'passes': audit_summary.get('passes', 0),
                        'warnings': audit_summary.get('warnings', 0),
                        'errors': audit_summary.get('errors', 0),
                        'tag_accuracy_pct': audit_summary.get('tag_accuracy_pct', 0),
                    }
                    add_log(
                        f"✅ Tag audit complete — "
                        f"Checklist: {audit_summary.get('total_checklist', 0)} items | "
                        f"✅ Pass: {audit_summary.get('passes', 0)} | "
                        f"⚠️ Warnings: {audit_summary.get('warnings', 0)} | "
                        f"❌ Errors: {audit_summary.get('errors', 0)}"
                    )
                    st.session_state.step_status[7] = "✅"
                except Exception as audit_err:
                    add_log(f"⚠️ Tag audit failed: {audit_err}", "warning")
                    logging.error(f"Tag audit error: {audit_err}", exc_info=True)
                    st.session_state.step_status[7] = "⚠️"
                    st.session_state.gpt_audit_failed = True
            update_progress()
        else:
            st.session_state.step_status[7] = "⚠️"
            if not pymupdf_clean_text:
                add_log("⚠️ Tag audit skipped — PyMuPDF clean text not available", "warning")
            update_progress()

        # ── Wrap up ──────────────────────────────────────────────────────────────
        elapsed = time.time() - start_time
        st.session_state.total_elapsed_secs = elapsed
        minutes, seconds = int(elapsed // 60), int(elapsed % 60)
        st.session_state.processing_time = f"{minutes}m {seconds}s"

        session_mgr.complete_processing(session_id, output_path)
        add_log(f"✅ Claude Opus processing complete in {st.session_state.processing_time}")
        return output_path

    except Exception as e:
        add_log(f"❌ Claude Opus processing error: {str(e)}", "error")
        st.error(f"❌ Claude Opus processing error: {str(e)}")
        logging.error(f"Claude Opus processing error: {e}", exc_info=True)
        return None


def process_pymupdf_clean_only(pdf_path, state_code, session_id, progress_placeholder=None, status_placeholder=None, log_placeholder=None):
    """Process PDF using only PyMuPDF extraction + Claude LLM clean (no visual conversion, no GPT audit)."""
    import importlib.util

    start_time = time.time()
    session_mgr = st.session_state.session_manager
    session_folders = session_mgr.get_session_folders(session_id)

    st.session_state.processing_logs = []

    steps_labels = [
        {"name": "Initialize",               "icon": "🤖"},
        {"name": "Detect State",              "icon": "🔍"},
        {"name": "PyMuPDF Extraction",        "icon": "📄"},
        {"name": "Claude LLM Clean",          "icon": "🧹"},
        {"name": "Save Output",              "icon": "💾"},
        # {"name": "Claude Opus Audit",         "icon": "🤖"},  # Validation review disabled for this branch
    ]
    st.session_state.step_status = {i: "⏳" for i in range(len(steps_labels))}
    # Override the steps display for this mode
    st.session_state._pymupdf_only_steps = steps_labels

    def add_log(message, level="info"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        step_idx = st.session_state.get('current_step', 0)
        st.session_state.processing_logs.append(
            {"time": timestamp, "message": message, "level": level, "step": step_idx}
        )
        logging.info(message)
        if log_placeholder is not None:
            with log_placeholder.container():
                st.markdown(_build_activity_feed_html(st.session_state.processing_logs), unsafe_allow_html=True)

    def update_progress():
        if progress_placeholder:
            with progress_placeholder.container():
                _show_steps(steps_labels)

    def _show_steps(steps):
        cols = st.columns(len(steps))
        for i, step in enumerate(steps):
            status = st.session_state.step_status.get(i, "⏳")
            with cols[i]:
                st.markdown(f"<div style='text-align:center'>{status}<br><small>{step['icon']} {step['name']}</small></div>", unsafe_allow_html=True)

    session_mgr.start_processing(session_id)
    add_log("Starting PyMuPDF Extract + Clean pipeline...")

    try:
        # ── Load opus module for utilities ──────────────────────────────────────
        opus_module_path = os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         '..', 'Opus4.6', 'Claude_Opus_4.6_Test.py')
        )
        spec = importlib.util.spec_from_file_location("claude_opus_converter", opus_module_path)
        opus = importlib.util.module_from_spec(spec)
        _orig_stdout, _orig_stderr = sys.stdout, sys.stderr
        spec.loader.exec_module(opus)
        sys.stdout, sys.stderr = _orig_stdout, _orig_stderr

        # Reset token tracker for this run
        if hasattr(opus, 'reset_token_tracker'):
            opus.reset_token_tracker()

        # ── Step 1: Initialize client ────────────────────────────────────────────
        st.session_state.current_step = 0
        st.session_state.step_status[0] = "🔄"
        update_progress()
        add_log("🤖 Initializing Anthropic client...")
        if status_placeholder:
            status_placeholder.info("🤖 Initializing Anthropic client...")

        client = opus.get_anthropic_client()
        if not client:
            raise Exception("Failed to initialize Anthropic client")

        st.session_state.step_status[0] = "✅"
        update_progress()
        add_log("✅ Client initialized")

        # ── Step 2: Detect State ──────────────────────────────────────────────────
        st.session_state.current_step = 1
        st.session_state.step_status[1] = "🔄"
        update_progress()
        add_log("🔍 Detecting state from filename...")

        detected_state = detect_state_from_numerical_id(pdf_path) if detect_state_from_numerical_id else None
        if detected_state:
            add_log(f"✅ State detected from filename numerical ID: {detected_state}")
        else:
            detected_state = opus.detect_state_from_filename(pdf_path)
            if detected_state:
                add_log(f"✅ State detected from filename pattern: {detected_state}")
            else:
                add_log("⚠️ Could not detect state from filename — using defaults", "warning")

        st.session_state.detected_state = detected_state
        if detected_state:
            _state_name = US_STATE_NAMES.get(detected_state, detected_state)
            add_log(f"✅ Detected state: {detected_state} — {_state_name}")
        else:
            add_log("⚠️ Could not detect state — using defaults", "warning")

        state_config = opus.load_state_template(detected_state) if detected_state else None
        st.session_state.step_status[1] = "✅"
        update_progress()

        # ── Step 3: PyMuPDF Extraction ───────────────────────────────────────────
        st.session_state.current_step = 2
        st.session_state.step_status[2] = "🔄"
        update_progress()
        add_log("📄 Running PyMuPDF extraction...")
        if status_placeholder:
            status_placeholder.info("📄 Running PyMuPDF extraction...")

        # Reset audit data
        st.session_state.audit_excel_path = None
        st.session_state.audit_counts = None
        st.session_state.audit_highlighted_dir = None
        st.session_state.audit_json_report = None
        st.session_state.content_accuracy_report = None
        st.session_state.gpt_audit_failed = False

        from pymupdf_extractor import (
            extract_pdf_json_report,
            extract_pdf_with_pymupdf,
            clean_extracted_text,
            consolidate_adjacent_tags,
            merge_cross_page_break_tags,
            split_ar_subsection_welds,
        )

        # Determine state-specific marking flags
        _mark_flags = {}
        if state_config:
            add_cfg = state_config.get("add_markup", {})
            del_cfg = state_config.get("delete_markup", {})
            _mark_flags["mark_underline"] = add_cfg.get("underline", True)
            _mark_flags["mark_italic"] = add_cfg.get("italic", False) or del_cfg.get("italic", False) or bool(add_cfg.get("italic_section_only"))
            _mark_flags["mark_bold"] = add_cfg.get("bold", False)
            _mark_flags["mark_strikethrough"] = del_cfg.get("strike_out", True)
            _mark_flags["mark_brackets"] = add_cfg.get("brackets", False) or del_cfg.get("brackets", False)

        # Output paths
        output_filename = Path(pdf_path).stem + ".txt"
        output_path = os.path.join(session_folders['output_folder'], output_filename)
        os.makedirs(session_folders['output_folder'], exist_ok=True)

        # JSON report
        pymupdf_report = extract_pdf_json_report(pdf_path, state_code=detected_state, **_mark_flags)
        n_spans = len(pymupdf_report.get("formatted_spans", []))
        add_log(f"📊 PyMuPDF JSON report: {n_spans} formatted spans detected")

        pymupdf_json_path = output_path.replace(".txt", "_report.json")
        import json as _json
        with open(pymupdf_json_path, "w", encoding="utf-8") as jf:
            _json.dump(pymupdf_report, jf, indent=2, ensure_ascii=False)

        # Tagged text
        pymupdf_text = extract_pdf_with_pymupdf(pdf_path, join_pages=True, state_code=detected_state, **_mark_flags)
        pymupdf_txt_path = output_path.replace(".txt", "_pymupdf.txt")
        with open(pymupdf_txt_path, "w", encoding="utf-8") as tf:
            tf.write(pymupdf_text)
        add_log(f"✅ PyMuPDF tagged text: {len(pymupdf_text):,} chars")

        # Filter italic by section if needed
        pymupdf_report, pymupdf_text = _filter_italic_by_section(pymupdf_report, pymupdf_text, state_config)

        # NOTE: Highlighted page images are intentionally NOT generated in
        # Extract+Clean mode — they're only required by the Visual-mode
        # Validation Review tab.  Skipping the render step keeps this
        # pipeline fast and avoids writing dozens of PNGs per bill.

        st.session_state.audit_json_report = pymupdf_report
        st.session_state.step_status[2] = "✅"
        update_progress()

        # ── Step 4: Claude LLM Clean ─────────────────────────────────────────────
        st.session_state.current_step = 3
        st.session_state.step_status[3] = "🔄"
        update_progress()
        add_log("🧹 Cleaning PyMuPDF text with Claude...")
        if status_placeholder:
            status_placeholder.info("🧹 Cleaning PyMuPDF text with Claude...")

        pymupdf_clean_text = clean_extracted_text(
            pymupdf_text,
            state_code=detected_state,
            pdf_path=pdf_path,
            anthropic_client=client,
            mark_brackets=_mark_flags.get("mark_brackets", False),
        )

        # Track LLM usage from the clean call
        _clean_usage = getattr(clean_extracted_text, '_last_usage', None)
        if _clean_usage and hasattr(opus, '_track_usage'):
            class _UsageObj:
                def __init__(self, d):
                    self.input_tokens = d.get('input_tokens', 0)
                    self.output_tokens = d.get('output_tokens', 0)
            opus._track_usage("LLM Clean", _UsageObj(_clean_usage), _clean_usage.get('model', 'claude-opus-4-6'))

        pymupdf_clean_text = consolidate_adjacent_tags(
            pymupdf_clean_text, state_code=detected_state
        )
        pymupdf_clean_text = opus.remove_nested_tags(pymupdf_clean_text)
        pymupdf_clean_text = opus.merge_split_tags(pymupdf_clean_text)
        if detected_state and detected_state.upper() == "AR":
            pymupdf_clean_text = split_ar_subsection_welds(pymupdf_clean_text)
        # Merge same-paragraph tags split across a page break.
        pymupdf_clean_text = merge_cross_page_break_tags(pymupdf_clean_text)
        pymupdf_clean_text = opus.split_tagged_subsections(pymupdf_clean_text)

        # Strip tags from protected clauses
        if strip_protected_tags and detected_state:
            _before = pymupdf_clean_text
            pymupdf_clean_text = strip_protected_tags(pymupdf_clean_text, detected_state)
            if pymupdf_clean_text != _before:
                add_log(f"🛡️ Stripped tags from protected clauses for {detected_state}")

        st.session_state.step_status[3] = "✅"
        update_progress()
        add_log(f"✅ Clean text: {len(pymupdf_text):,} → {len(pymupdf_clean_text):,} chars")

        # ── Conversion milestone (extraction + LLM clean) ────────────────────────
        conversion_elapsed = time.time() - start_time
        st.session_state.conversion_elapsed_secs = conversion_elapsed
        _cm, _cs = int(conversion_elapsed // 60), int(conversion_elapsed % 60)
        st.session_state.conversion_time = f"{_cm}m {_cs}s" if _cm > 0 else f"{_cs}s"

        # Save pre-cleaned text (what the LLM received as input)
        _pre_cleaned = getattr(clean_extracted_text, '_last_pre_cleaned', None)
        if _pre_cleaned:
            _pre_clean_path = output_path.replace(".txt", "_pre_cleaned.txt")
            with open(_pre_clean_path, "w", encoding="utf-8") as pf:
                pf.write(_pre_cleaned)
            add_log(
                f"📋 Pre-cleaned text saved: {len(pymupdf_text):,} → {len(_pre_cleaned):,} → "
                f"{len(pymupdf_clean_text):,} chars (raw → pre-cleaned → LLM)"
            )

        # Save LLM diff report (what did the LLM remove/change?)
        _diff_report = getattr(clean_extracted_text, '_last_diff_report', None)
        if _diff_report:
            diff_path = pymupdf_json_path.replace("_report.json", "_clean_diff.txt")
            with open(diff_path, "w", encoding="utf-8") as df:
                df.write(_diff_report)
            add_log(f"📋 LLM diff report saved")

        _preclean_guard = getattr(clean_extracted_text, '_last_preclean_guard', None)
        if _preclean_guard:
            guard_path = output_path.replace(".txt", "_preclean_guard.txt")
            with open(guard_path, "w", encoding="utf-8") as gf:
                gf.write(_json.dumps(_preclean_guard, indent=2, ensure_ascii=False))
            if _preclean_guard.get("ok"):
                add_log("✅ Pre-clean guard passed (only line-number artifacts removed)")
            else:
                add_log(
                    f"⚠️ Pre-clean guard failed: {_preclean_guard.get('unexpected_removed_count', 0)} unexpected removals",
                    "warning",
                )
            add_log(f"📋 Pre-clean guard report saved")

        # Rebuild report context using cleaned text for better anchor matching
        if pymupdf_report:
            try:
                from pymupdf_extractor import rebuild_report_context
                pymupdf_report = rebuild_report_context(pymupdf_report, pymupdf_clean_text)
                with open(pymupdf_json_path, "w", encoding="utf-8") as jf:
                    _json.dump(pymupdf_report, jf, indent=2, ensure_ascii=False)
                add_log("🔄 Report context rebuilt from cleaned text")
                st.session_state.audit_json_report = pymupdf_report
            except Exception as _ctx_err:
                logging.warning(f"Context rebuild failed (non-critical): {_ctx_err}")

        # ── Step 5: Save Output ──────────────────────────────────────────────────
        st.session_state.current_step = 4
        st.session_state.step_status[4] = "🔄"
        update_progress()

        with open(output_path, "w", encoding="utf-8") as cf:
            cf.write(pymupdf_clean_text)
        add_log(f"💾 Output saved: {output_path}")

        st.session_state.step_status[4] = "✅"
        update_progress()

        # ── Content Accuracy Check ─────────────────────────────────────
        # Use a dedicated accuracy reference text (artifact-stripped),
        # while keeping saved _pre_cleaned as strict line-number-only
        # cleanup + LLM input reference.
        st.session_state.content_accuracy_report = None
        _removals_json = output_path.replace(".txt", "_removals.json")
        _accuracy_json = output_path.replace(".txt", "_accuracy.json")
        _pre_cleaned_accuracy = getattr(clean_extracted_text, '_last_pre_cleaned_accuracy', None)
        _accuracy_raw = _pre_cleaned_accuracy if _pre_cleaned_accuracy else (_pre_cleaned if _pre_cleaned else pymupdf_text)
        # Extract lines removed by LLM (in pre-cleaned but not in final output)
        _llm_removed = None
        if _pre_cleaned:
            _pre_lines = set(_pre_cleaned.splitlines())
            _clean_lines = set(pymupdf_clean_text.splitlines())
            _llm_removed = [l for l in _pre_lines - _clean_lines if l.strip()]
        from concurrent.futures import ThreadPoolExecutor

        # Content accuracy — word-level precleaned vs output (PyMuPDF Clean mode)
        _content_acc_json = output_path.replace(".txt", "_content_accuracy.json") if output_path else None
        _run_content_accuracy_check(
            precleaned_text=_accuracy_raw,
            output_text=pymupdf_clean_text,
            add_log=add_log,
            save_to=_content_acc_json,
            pdf_path=st.session_state.get('temp_pdf_path'),
        )

        # ── Step 6: Tag Validation (PyMuPDF report → clean text) ────────────
        # 1. Render highlighted PDF page images
        # 2. Run deterministic pre-check (checklist from report vs clean tags)
        # 3. Send images + checklist + clean text to Claude Opus 4.7 for
        #    visual verification of every tag
        st.session_state.current_step = 5
        st.session_state.step_status[5] = "🔄"
        update_progress()
        add_log("🔍 Running tag validation (report → clean text)...")
        if status_placeholder:
            status_placeholder.info("🔍 Validating tags against PyMuPDF report...")

        try:
            from pymupdf_extractor import highlight_pdf_formatting

            # Import the PyMuPDF-clean validator from Opus4.6
            opus46_dir = os.path.normpath(
                os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             '..', 'Opus4.6')
            )
            if opus46_dir not in sys.path:
                sys.path.insert(0, opus46_dir)
            from pymupdf_compare import pymupdf_compare_tags

            # ── 6a: Render highlighted page images ──────────────────────
            highlight_dir = Path(output_path).parent / f"{Path(pdf_path).stem}_highlighted"
            hi_imgs = highlight_pdf_formatting(
                pdf_path,
                report=pymupdf_report,
                output_dir=highlight_dir,
                **_mark_flags,
            )
            add_log(f"🖍️ {len(hi_imgs)} highlighted page images saved")
            st.session_state.audit_highlighted_dir = str(highlight_dir)

            # Load highlighted images as base64
            import base64 as _b64
            hi_images_b64 = []
            for img_file in sorted(highlight_dir.glob("page_*.png")):
                hi_images_b64.append(_b64.b64encode(img_file.read_bytes()).decode())

            # ── 6b: Build state-rules description for the prompt ────────
            sr_parts = []
            if state_config:
                sr_parts.append(
                    f"State: {state_config.get('state_name', detected_state or 'unknown')}"
                )
                a_cfg = state_config.get("add_markup", {})
                d_cfg = state_config.get("delete_markup", {})
                sr_parts.append(f"Underline={a_cfg.get('underline', False)}")
                sr_parts.append(f"Strikethrough={d_cfg.get('strike_out', False)}")
                sr_parts.append(f"Italic={a_cfg.get('italic', False)}")
                sr_parts.append(f"Bold={a_cfg.get('bold', False)}")
                if d_cfg.get('strike_out', False) and (
                    a_cfg.get('underline', False)
                    or a_cfg.get('italic', False)
                    or a_cfg.get('bold', False)
                ):
                    sr_parts.append(
                        "PRIORITY: strikethrough + any other formatting → tag "
                        "as [D>...<D] only, never [A>...<A]"
                    )
            state_rules_str = ", ".join(sr_parts) if sr_parts else (
                f"State: {detected_state or 'unknown'}"
            )

            # ── 6c: Run Claude Opus 4.7 vision audit ───────────────────
            add_log("🤖 Running Claude Opus 4.7 audit on cleaned text...")
            if status_placeholder:
                status_placeholder.info("🤖 Running Claude Opus 4.7 audit (comparing PyMuPDF report vs clean text)...")

            audit_xlsx = output_path.replace(".txt", "_tag_validation.xlsx") \
                if isinstance(output_path, str) \
                else str(output_path).replace(".txt", "_tag_validation.xlsx")

            audit_summary = pymupdf_compare_tags(
                highlighted_images_b64=hi_images_b64,
                pymupdf_report=pymupdf_report,
                pymupdf_clean_text=pymupdf_clean_text,
                state_code=detected_state or "XX",
                state_rules=state_rules_str,
                output_xlsx=audit_xlsx,
            )

            st.session_state.audit_excel_path = audit_xlsx
            st.session_state.audit_counts = {
                'total_checklist': audit_summary.get('total_checklist', 0),
                'passes': audit_summary.get('passes', 0),
                'warnings': audit_summary.get('warnings', 0),
                'errors': audit_summary.get('errors', 0),
                'additional_findings': audit_summary.get('additional_findings', 0),
            }
            add_log(
                f"✅ Audit complete — "
                f"Checklist: {audit_summary['total_checklist']} | "
                f"✅ Pass: {audit_summary['passes']} | "
                f"⚠️ Warn: {audit_summary['warnings']} | "
                f"❌ Error: {audit_summary['errors']}"
            )
            if audit_summary.get('additional_findings'):
                add_log(f"🔍 Additional findings: {audit_summary['additional_findings']}")
            st.session_state.step_status[5] = "✅"
        except Exception as audit_err:
            add_log(f"⚠️ Audit failed: {audit_err}", "warning")
            logging.error(f"PyMuPDF audit error: {audit_err}", exc_info=True)
            st.session_state.step_status[5] = "⚠️"
            st.session_state.gpt_audit_failed = True
        update_progress()

        # ── Wrap up ──────────────────────────────────────────────────────────────
        elapsed = time.time() - start_time
        st.session_state.total_elapsed_secs = elapsed
        minutes, seconds = int(elapsed // 60), int(elapsed % 60)
        st.session_state.processing_time = f"{minutes}m {seconds}s"

        session_mgr.complete_processing(session_id, output_path)
        add_log(f"✅ PyMuPDF Extract + Clean complete in {st.session_state.processing_time}")

        # Capture token/cost data
        if hasattr(opus, 'get_token_tracker'):
            _cost_data = opus.get_token_tracker()
            st.session_state['_api_cost_data'] = _cost_data
            if _cost_data['total_cost'] > 0:
                add_log(f"💰 API Cost: ${_cost_data['total_cost']:.4f} "
                        f"({_cost_data['total_input']:,} input + {_cost_data['total_output']:,} output tokens)")

        return output_path

    except Exception as e:
        add_log(f"❌ PyMuPDF processing error: {str(e)}", "error")
        st.error(f"❌ PyMuPDF processing error: {str(e)}")
        logging.error(f"PyMuPDF processing error: {e}", exc_info=True)
        return None


def process_with_hybrid_mode(pdf_path, state_code, session_id, progress_placeholder=None, status_placeholder=None, log_placeholder=None):
    """Process PDF using Hybrid mode: PyMuPDF text extraction + LLM chunk cleaning.
    
    Best for long PDFs (>7 pages). Much cheaper than image-based visual mode.
    Pipeline: PyMuPDF extract → split into chunks → LLM clean each chunk → merge → post-process.
    """
    import importlib.util

    start_time = time.time()
    session_mgr = st.session_state.session_manager
    session_folders = session_mgr.get_session_folders(session_id)

    st.session_state.processing_logs = []

    # 5 steps for hybrid mode (Extract + Clean — audit disabled on this branch)
    steps_labels = [
        {"name": "Initialize Claude Opus",    "icon": "🤖"},
        {"name": "Detect State",              "icon": "🔍"},
        {"name": "PyMuPDF Extraction",        "icon": "📄"},
        {"name": "Chunk + LLM Clean",         "icon": "🧹"},
        {"name": "Save Output",              "icon": "💾"},
        # {"name": "Claude Opus Audit",         "icon": "🤖"},  # Validation review disabled for this branch
    ]
    st.session_state.step_status = {i: "⏳" for i in range(len(steps_labels))}

    def add_log(message, level="info"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        step_idx = st.session_state.get('current_step', 0)
        st.session_state.processing_logs.append(
            {"time": timestamp, "message": message, "level": level, "step": step_idx}
        )
        logging.info(message)
        if log_placeholder is not None:
            with log_placeholder.container():
                st.markdown(_build_activity_feed_html(st.session_state.processing_logs), unsafe_allow_html=True)

    def update_progress():
        if progress_placeholder:
            with progress_placeholder.container():
                _show_hybrid_steps(steps_labels)

    def _show_hybrid_steps(steps):
        cols = st.columns(len(steps))
        for i, step in enumerate(steps):
            status = st.session_state.step_status.get(i, "⏳")
            with cols[i]:
                st.markdown(f"<div style='text-align:center'>{status}<br><small>{step['icon']} {step['name']}</small></div>", unsafe_allow_html=True)

    session_mgr.start_processing(session_id)
    add_log("Starting Hybrid Mode (PyMuPDF + LLM chunk cleaning)...")

    try:
        # ── Load the Opus4.6 module ──────────────────────────────────────────────
        opus_module_path = os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         '..', 'Opus4.6', 'Claude_Opus_4.6_Test.py')
        )
        spec = importlib.util.spec_from_file_location("claude_opus_converter", opus_module_path)
        opus = importlib.util.module_from_spec(spec)
        _orig_stdout, _orig_stderr = sys.stdout, sys.stderr
        spec.loader.exec_module(opus)
        sys.stdout, sys.stderr = _orig_stdout, _orig_stderr

        # Reset token tracker for this run
        opus.reset_token_tracker()

        # ── Step 1: Initialize client ────────────────────────────────────────────
        st.session_state.current_step = 0
        st.session_state.step_status[0] = "🔄"
        update_progress()
        add_log("🤖 Initializing Claude Opus 4.6 client...")
        if status_placeholder:
            status_placeholder.info("🤖 Initializing Claude Opus 4.6 client...")

        client = opus.get_anthropic_client()
        if not client:
            raise Exception("Failed to initialize Anthropic client")

        st.session_state.step_status[0] = "✅"
        update_progress()
        add_log("✅ Claude Opus client initialized")

        # ── Step 2: Detect State ──────────────────────────────────────────────────
        st.session_state.current_step = 1
        st.session_state.step_status[1] = "🔄"
        update_progress()
        add_log("🔍 Detecting state from filename...")
        if status_placeholder:
            status_placeholder.info("🔍 Detecting state...")

        detected_state = detect_state_from_numerical_id(pdf_path) if detect_state_from_numerical_id else None
        if detected_state:
            add_log(f"✅ State detected from filename numerical ID: {detected_state}")
        else:
            detected_state = opus.detect_state_from_filename(pdf_path)
            if detected_state:
                add_log(f"✅ State detected from filename pattern: {detected_state}")
            else:
                add_log("⚠️ Could not detect state from filename — using defaults", "warning")

        st.session_state.detected_state = detected_state
        if detected_state:
            _state_name = US_STATE_NAMES.get(detected_state, detected_state)
            add_log(f"✅ Detected state: {detected_state} — {_state_name}")
        else:
            add_log("⚠️ Could not detect state — using defaults", "warning")

        state_config = opus.load_state_template(detected_state) if detected_state else None
        st.session_state.step_status[1] = "✅"
        update_progress()

        # ── Step 3: PyMuPDF Extraction (for tagged text) ─────────────────────────
        st.session_state.current_step = 2
        st.session_state.step_status[2] = "🔄"
        update_progress()
        add_log("📄 Running PyMuPDF extraction with formatting detection...")
        if status_placeholder:
            status_placeholder.info("📄 Running PyMuPDF extraction...")

        # Reset audit data
        st.session_state.audit_excel_path = None
        st.session_state.audit_counts = None
        st.session_state.audit_highlighted_dir = None
        st.session_state.audit_json_report = None
        st.session_state.content_accuracy_report = None
        st.session_state.gpt_audit_failed = False

        from pymupdf_extractor import (
            extract_pdf_json_report,
            extract_pdf_with_pymupdf,
            clean_extracted_text,
            consolidate_adjacent_tags,
            merge_cross_page_break_tags,
        )

        # Determine state-specific marking flags
        _mark_flags = {}
        if state_config:
            add_cfg = state_config.get("add_markup", {})
            del_cfg = state_config.get("delete_markup", {})
            _mark_flags["mark_underline"] = add_cfg.get("underline", True)
            _mark_flags["mark_italic"] = add_cfg.get("italic", False) or del_cfg.get("italic", False) or bool(add_cfg.get("italic_section_only"))
            _mark_flags["mark_bold"] = add_cfg.get("bold", False)
            _mark_flags["mark_strikethrough"] = del_cfg.get("strike_out", True)
            _mark_flags["mark_brackets"] = add_cfg.get("brackets", False) or del_cfg.get("brackets", False)

        # JSON report + Tagged text — run in parallel (each opens its own
        # PDF handle so there's no contention; saves ~20-25s on large PDFs).
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import contextlib

        def _extract_report():
            return extract_pdf_json_report(pdf_path, state_code=detected_state, **_mark_flags)

        def _extract_text():
            return extract_pdf_with_pymupdf(pdf_path, join_pages=True, state_code=detected_state, **_mark_flags)

        # Suppress PyMuPDF's noisy "find_tables: exception occurred" stderr
        # output during parallel runs — the exceptions are already caught in
        # Python code, but the C layer prints before Python can intercept.
        with open(os.devnull, 'w') as _devnull, contextlib.redirect_stderr(_devnull):
            with ThreadPoolExecutor(max_workers=2) as executor:
                fut_report = executor.submit(_extract_report)
                fut_text = executor.submit(_extract_text)
                pymupdf_report = fut_report.result()
                pymupdf_text = fut_text.result()

        n_spans = len(pymupdf_report.get("formatted_spans", []))
        add_log(f"📊 PyMuPDF report: {n_spans} formatted spans detected")
        add_log(f"✅ PyMuPDF tagged text: {len(pymupdf_text):,} chars")

        # Filter italic by section if needed
        pymupdf_report, pymupdf_text = _filter_italic_by_section(pymupdf_report, pymupdf_text, state_config)

        # NOTE: Highlighted page images are intentionally NOT generated in
        # Hybrid mode — they're only required by the Visual-mode
        # Validation Review tab.

        st.session_state.audit_json_report = pymupdf_report
        st.session_state.step_status[2] = "✅"
        update_progress()

        # ── Step 4: Hybrid Chunk + LLM Clean ─────────────────────────────────────
        st.session_state.current_step = 3
        st.session_state.step_status[3] = "🔄"
        update_progress()
        add_log("🧹 Running hybrid chunk cleaning (splitting text → cleaning each chunk → merging)...")
        if status_placeholder:
            status_placeholder.info("🧹 Hybrid: splitting text into chunks and cleaning with LLM...")

        # ── Launch highlight rendering in background (only if there are spans) ──
        _highlight_future = None
        _highlight_executor = None
        _highlight_dir = None
        if n_spans > 0:
            from concurrent.futures import ThreadPoolExecutor
            from pymupdf_extractor import highlight_pdf_formatting

            _highlight_executor = ThreadPoolExecutor(max_workers=1)
            _output_path_for_highlight = os.path.join(session_folders['output_folder'], Path(pdf_path).stem + ".txt")
            _highlight_dir = Path(_output_path_for_highlight).parent / f"{Path(pdf_path).stem}_highlighted"

            def _run_highlight():
                return highlight_pdf_formatting(
                    pdf_path,
                    report=pymupdf_report,
                    output_dir=_highlight_dir,
                    **_mark_flags,
                )

            _highlight_future = _highlight_executor.submit(_run_highlight)
        else:
            add_log("⏭️ No formatted spans — skipping highlight rendering")

        # Use the Opus module's hybrid conversion function
        # Pass pre-extracted text to avoid duplicate PyMuPDF extraction
        converted_text = opus.convert_pdf_hybrid_text(client, pdf_path, state_config, detected_state, raw_text=pymupdf_text)

        if not converted_text:
            raise Exception("Hybrid conversion returned empty result")

        add_log(f"✅ Hybrid conversion complete: {len(converted_text):,} characters")

        # Apply deterministic cleanup (same as visual mode)
        # Load precleaned text as reference for merged-line restoration
        _precleaned_path = str(Path(pdf_path).with_suffix('')) + "_hybrid_precleaned.txt"
        _precleaned_text = None
        if os.path.exists(_precleaned_path):
            with open(_precleaned_path, "r", encoding="utf-8") as _pf:
                _precleaned_text = _pf.read()
        converted_text = opus.deterministic_cleanup(
            converted_text, state_config=state_config, state_code=detected_state,
            reference_text=_precleaned_text,
        )
        add_log("🔧 Deterministic cleanup applied")

        # Strip protected clauses
        if state_config:
            converted_text = opus.post_process_output(converted_text, state_config)
        if strip_protected_tags and detected_state:
            _before_strip = converted_text
            converted_text = strip_protected_tags(converted_text, detected_state)
            if converted_text != _before_strip:
                add_log(f"🛡️ Stripped tags from protected clauses for {detected_state}")

        st.session_state.step_status[3] = "✅"
        update_progress()

        # ── Step 5: Save output ──────────────────────────────────────────────────
        st.session_state.current_step = 4
        st.session_state.step_status[4] = "🔄"
        update_progress()
        add_log("💾 Saving output...")
        if status_placeholder:
            status_placeholder.info("💾 Saving output...")

        output_filename = Path(pdf_path).stem + ".txt"
        output_path = os.path.join(session_folders['output_folder'], output_filename)
        os.makedirs(session_folders['output_folder'], exist_ok=True)

        if not opus.save_text_file(converted_text, output_path):
            raise Exception(f"Failed to save output to {output_path}")

        # Save LLM diff report for hybrid mode
        _diff_report = getattr(opus.convert_pdf_hybrid_text, '_last_diff_report', None)
        if _diff_report:
            diff_path = output_path.replace(".txt", "_clean_diff.txt")
            with open(diff_path, "w", encoding="utf-8") as df:
                df.write(_diff_report)
            add_log(f"📋 LLM diff report saved: {diff_path}")

        # Save per-chunk breakdown (raw input → cleaned output for each chunk)
        _chunks_report = getattr(opus.convert_pdf_hybrid_text, '_last_chunks_report', None)
        if _chunks_report:
            chunks_path = output_path.replace(".txt", "_hybrid_chunks.txt")
            with open(chunks_path, "w", encoding="utf-8") as cf:
                cf.write(_chunks_report)
            add_log(f"📦 Chunks breakdown saved: {chunks_path}")

        st.session_state.step_status[4] = "✅"
        update_progress()
        add_log(f"✅ Output saved: {output_path}")

        # ── Conversion milestone ─────────────────────────────────────────────────
        conversion_elapsed = time.time() - start_time
        st.session_state.conversion_elapsed_secs = conversion_elapsed
        _cm, _cs = int(conversion_elapsed // 60), int(conversion_elapsed % 60)
        st.session_state.conversion_time = f"{_cm}m {_cs}s" if _cm > 0 else f"{_cs}s"
        add_log(f"🏁 Hybrid conversion complete in {st.session_state.conversion_time}")
        if status_placeholder:
            status_placeholder.success(f"✅ Hybrid conversion complete ({st.session_state.conversion_time})")

        # Audit state will be populated by Step 6 below; only clear stale flags here
        st.session_state.audit_excel_path = None
        st.session_state.audit_counts = None
        st.session_state.audit_highlighted_dir = None
        st.session_state.gpt_audit_failed = False
        st.session_state.content_accuracy_report = None

        # ── Content Accuracy ───────────────────────────────────────────
        _removals_json = output_path.replace(".txt", "_removals.json") if output_path else None
        _accuracy_json = output_path.replace(".txt", "_accuracy.json") if output_path else None

        from concurrent.futures import ThreadPoolExecutor

        # Content accuracy — word-level precleaned vs output comparison
        # _precleaned_text already loaded above for deterministic_cleanup
        # Apply repeating-page-artifact stripping so headers/footers the LLM
        # correctly removed don't count as false-positive "content drops".
        _precleaned_for_acc = _precleaned_text
        if _precleaned_for_acc:
            try:
                from pymupdf_extractor import _strip_repeating_page_artifacts
                _precleaned_for_acc = _strip_repeating_page_artifacts(_precleaned_for_acc)
            except ImportError:
                pass
        _content_acc_json = output_path.replace(".txt", "_content_accuracy.json") if output_path else None
        _run_content_accuracy_check(
            precleaned_text=_precleaned_for_acc,
            output_text=converted_text,
            add_log=add_log,
            save_to=_content_acc_json,
            pdf_path=st.session_state.get('temp_pdf_path'),
        )

        # ── Step 6: Claude Opus 4.7 Audit (Validation Report) ─────────────
        st.session_state.current_step = 5
        st.session_state.step_status[5] = "🔄"
        update_progress()

        if n_spans == 0:
            # No formatting detected — skip audit entirely
            add_log("⏭️ No formatted spans detected — skipping tag audit (no tagging required)")
            st.session_state.audit_counts = {
                'total_checklist': 0, 'passes': 0, 'warnings': 0, 'errors': 0, 'additional_findings': 0,
            }
            st.session_state.step_status[5] = "✅"
        else:
            add_log("🤖 Running Claude Opus 4.7 audit on hybrid-cleaned text...")
            if status_placeholder:
                status_placeholder.info("🤖 Running Claude Opus 4.7 audit...")

            try:
                from pymupdf_extractor import highlight_pdf_formatting

                # Collect pre-rendered highlight images (launched in parallel with LLM)
                highlight_dir = _highlight_dir
                hi_imgs = _highlight_future.result()  # blocks only if not yet done
                _highlight_executor.shutdown(wait=False)
                add_log(f"🖍️ {len(hi_imgs)} highlighted page images saved")
                st.session_state.audit_highlighted_dir = str(highlight_dir)

                import base64 as _b64
                hi_images_b64 = []
                for img_file in sorted(highlight_dir.glob("page_*.png")):
                    hi_images_b64.append(_b64.b64encode(img_file.read_bytes()).decode())

                opus46_dir = os.path.normpath(
                    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 '..', 'Opus4.6')
                )
                if opus46_dir not in sys.path:
                    sys.path.insert(0, opus46_dir)
                from pymupdf_compare import pymupdf_compare_tags

                sr_parts = []
                if state_config:
                    sr_parts.append(
                        f"State: {state_config.get('state_name', detected_state or 'unknown')}"
                    )
                    a_cfg = state_config.get("add_markup", {})
                    d_cfg = state_config.get("delete_markup", {})
                    sr_parts.append(f"Underline={a_cfg.get('underline', False)}")
                    sr_parts.append(f"Strikethrough={d_cfg.get('strike_out', False)}")
                    sr_parts.append(f"Italic={a_cfg.get('italic', False)}")
                    sr_parts.append(f"Bold={a_cfg.get('bold', False)}")
                    if d_cfg.get('strike_out', False) and (
                        a_cfg.get('underline', False)
                        or a_cfg.get('italic', False)
                        or a_cfg.get('bold', False)
                    ):
                        sr_parts.append(
                            "PRIORITY: strikethrough + any other formatting → tag "
                            "as [D>...<D] only, never [A>...<A]"
                        )
                state_rules_str = ", ".join(sr_parts) if sr_parts else (
                    f"State: {detected_state or 'unknown'}"
                )

                audit_xlsx = output_path.replace(".txt", "_audit.xlsx") if isinstance(output_path, str) else str(output_path).replace(".txt", "_audit.xlsx")
                audit_summary = pymupdf_compare_tags(
                    highlighted_images_b64=hi_images_b64,
                    pymupdf_report=pymupdf_report,
                    pymupdf_clean_text=converted_text,
                    state_code=detected_state or "XX",
                    state_rules=state_rules_str,
                    output_xlsx=audit_xlsx,
                )

                st.session_state.audit_excel_path = audit_xlsx
                st.session_state.audit_counts = {
                    'total_checklist': audit_summary.get('total_checklist', 0),
                    'passes': audit_summary.get('passes', 0),
                    'warnings': audit_summary.get('warnings', 0),
                    'errors': audit_summary.get('errors', 0),
                    'additional_findings': audit_summary.get('additional_findings', 0),
                }
                add_log(
                    f"✅ Audit complete — "
                    f"Checklist: {audit_summary['total_checklist']} | "
                    f"✅ Pass: {audit_summary['passes']} | "
                    f"⚠️ Warn: {audit_summary['warnings']} | "
                    f"❌ Error: {audit_summary['errors']}"
                )
                if audit_summary.get('additional_findings'):
                    add_log(f"🔍 Additional findings: {audit_summary['additional_findings']}")
                st.session_state.step_status[5] = "✅"
            except Exception as audit_err:
                add_log(f"⚠️ Audit failed: {audit_err}", "warning")
                logging.error(f"Hybrid mode audit error: {audit_err}", exc_info=True)
                st.session_state.step_status[5] = "⚠️"
                st.session_state.gpt_audit_failed = True
        update_progress()

        # ── Wrap up ──────────────────────────────────────────────────────────────
        elapsed = time.time() - start_time
        st.session_state.total_elapsed_secs = elapsed
        minutes, seconds = int(elapsed // 60), int(elapsed % 60)
        st.session_state.processing_time = f"{minutes}m {seconds}s"

        session_mgr.complete_processing(session_id, output_path)
        add_log(f"✅ Hybrid mode processing complete in {st.session_state.processing_time}")

        # Capture token/cost data
        _cost_data = opus.get_token_tracker()
        st.session_state['_api_cost_data'] = _cost_data
        if _cost_data['total_cost'] > 0:
            add_log(f"💰 API Cost: ${_cost_data['total_cost']:.4f} "
                    f"({_cost_data['total_input']:,} input + {_cost_data['total_output']:,} output tokens)")

        return output_path

    except Exception as e:
        add_log(f"❌ Hybrid mode processing error: {str(e)}", "error")
        st.error(f"❌ Hybrid mode processing error: {str(e)}")
        logging.error(f"Hybrid mode processing error: {e}", exc_info=True)
        return None

def show_simple_ui():
    """Simple UI for single file processing with session management"""

    # ── Global chrome hide — strip Streamlit's dev-only UI ─────────────
    # We hide ONLY the items users of this internal tool don't need:
    #   • Deploy button (stToolbar contents)
    #   • Hamburger menu (#MainMenu)
    #   • Top gradient strip (stDecoration)
    #   • Status widget / footer
    # The header itself is left at its NATIVE size so the sidebar toggle
    # (» / «) button stays visible and clickable.  Shrinking the header
    # was clipping the toggle off-screen — user couldn't open the
    # Session Info sidebar anymore.
    st.markdown("""
    <style>
        /* Hide dev chrome */
        [data-testid="stToolbar"],
        [data-testid="stDecoration"],
        [data-testid="stStatusWidget"],
        #MainMenu,
        footer {
            display: none !important;
            visibility: hidden !important;
        }
        /* Make header transparent so it blends with the page,
           but DO NOT shrink its height — that would hide the
           sidebar collapse/expand button.                       */
        header[data-testid="stHeader"],
        .stApp > header {
            background: transparent !important;
        }
    </style>
    """, unsafe_allow_html=True)

    # Determine if results are showing (need wider layout for validation review)
    _has_results = (
        (st.session_state.get('processed_file_path') and
         (st.session_state.get('processed_file_data') is not None
          or (st.session_state.get('processed_file_path') and os.path.exists(st.session_state.processed_file_path))))
        or bool(st.session_state.get('batch_results'))
    )
    _max_width = '98%' if _has_results else '1200px'
    # When a document is already processed, the header (title + subtitle
    # + file card) is just chrome — collapse padding so the PDF↔Text
    # comparator below gets nearly the full viewport height.  Keep enough
    # padding-top to clear the Streamlit toolbar (Deploy / menu) above.
    _pad_top = '0.9rem' if _has_results else '2rem'
    _pad_bot = '0.3rem' if _has_results else '2rem'

    # Dynamic container width (must be f-string for variable)
    st.markdown(f"""
    <style>
        .block-container {{
            padding-top: {_pad_top} !important;
            padding-bottom: {_pad_bot} !important;
            padding-left: 0.6rem !important;
            padding-right: 0.6rem !important;
            max-width: {_max_width} !important;
        }}
        /* Compact-mode tweaks — only active after a doc is processed */
        .compact-header h1 {{
            font-size: 1.1rem !important;
            margin: 0 !important;
            padding: 6px 0 8px 0 !important;
            line-height: 1.4 !important;
            text-align: left !important;
            display: block !important;
        }}
        .compact-header .subtitle {{ display: none !important; }}
        .compact-header {{
            display: block;
            padding: 0 0 6px 0;
            margin-bottom: 4px;
            min-height: 36px;
        }}
    </style>
    """, unsafe_allow_html=True)

    # ── Aggressive gap-removal CSS (only when results are showing) ────
    # Streamlit's default vertical-block layout inserts ~1rem gap between
    # sibling elements.  When a document is processed we want the file-info
    # card and the tabs to sit almost flush so the PDF↔Text comparator can
    # claim the rest of the page.
    if _has_results:
        st.markdown("""
        <style>
            /* ── Hide footer / decorations ─────────────────────────────
               Note: header[data-testid="stHeader"] is intentionally NOT
               hidden here.  The sidebar-toggle button (»/«) lives inside
               the header and we need it visible so users can access the
               Session Info sidebar.  Header is slimmed (not hidden) in
               the global CSS block at the top of show_simple_ui().      */
            footer,
            [data-testid="stDecoration"],
            [data-testid="stStatusWidget"],
            [data-testid="stToolbar"] {
                display: none !important;
                height: 0 !important;
            }

            /* ── Lock app to viewport so there is NO outer scroll ──────── */
            html, body, .stApp,
            [data-testid="stAppViewContainer"],
            section.main, .main {
                height: 100vh !important;
                max-height: 100vh !important;
                overflow: hidden !important;
            }

            /* ── Block-container is a flex column that fills viewport ──── */
            .block-container {
                height: 100vh !important;
                max-height: 100vh !important;
                overflow: hidden !important;
                display: flex !important;
                flex-direction: column !important;
                padding-top: 0.5rem !important;
                padding-bottom: 0 !important;
                gap: 0 !important;
            }

            /* All vertical-blocks become flex columns with no gap.
               The OUTER vertical-block (direct child of block-container)
               is what needs flex:1 chain — but children control their own
               grow via flex-shrink:0 (chrome) or flex:1 (the iframe row). */
            [data-testid="stVerticalBlock"] {
                gap: 0 !important;
                display: flex !important;
                flex-direction: column !important;
                min-height: 0 !important;
            }
            /* Direct children of block-container's vertical block fill it */
            .block-container > [data-testid="stVerticalBlock"],
            .block-container [data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] iframe) {
                flex: 1 1 auto !important;
                height: 100% !important;
            }

            /* Element-containers default to NO grow (chrome rows) */
            [data-testid="element-container"] {
                flex-shrink: 0 !important;
                margin-bottom: 0 !important;
            }
            /* Element-container that wraps the iframe DOES grow.
               JS in the iframe measures its own top position and
               sizes itself + these wrappers exactly.  These rules
               are SAFETY NETS only — min-height ensures the iframe
               isn't squeezed; max-height is a generous cap to keep
               it from extending wildly past viewport if JS fails. */
            [data-testid="element-container"]:has(iframe),
            [data-testid="element-container"]:has([data-testid="stIFrame"]),
            [data-testid="element-container"]:has([data-testid="stCustomComponentV1Host"]) {
                flex: 1 1 auto !important;
                min-height: 500px !important;
                max-height: calc(100vh - 50px) !important;
                display: flex !important;
                flex-direction: column !important;
                margin-top: 0 !important;
                padding-top: 0 !important;
            }

            /* iframe wrappers fill their parent */
            [data-testid="stIFrame"],
            [data-testid="stCustomComponentV1Host"] {
                flex: 1 1 auto !important;
                min-height: 500px !important;
                max-height: calc(100vh - 50px) !important;
                display: flex !important;
                flex-direction: column !important;
                border-radius: 8px !important;
                overflow: hidden !important;
                margin-top: 0 !important;
                padding-top: 0 !important;
            }
            iframe {
                flex: 1 1 auto !important;
                /* Min/max safety nets — actual height is set by JS in
                   the iframe via window.frameElement (measures top
                   position dynamically and fills remaining viewport). */
                min-height: 500px !important;
                max-height: calc(100vh - 50px) !important;
                width: 100% !important;
                border: 1px solid #d0d7de !important;
                border-radius: 8px !important;
                box-shadow: 0 1px 4px rgba(0,0,0,.06) !important;
                background: #ffffff !important;
                display: block !important;
                margin: 0 !important;
            }

            /* Horizontal block (file pill + New button) is a fixed row
               with guaranteed height so it can't overlap the title above. */
            div[data-testid="stHorizontalBlock"] {
                gap: 8px !important;
                margin-top: 0 !important;
                margin-bottom: 6px !important;
                align-items: center !important;
                flex-shrink: 0 !important;
                min-height: 38px !important;
            }

            /* ── Tabs: list at top, panel fills remaining space ──────── */
            .stTabs {
                margin-top: -8px !important;
                margin-bottom: 0 !important;
                display: flex !important;
                flex-direction: column !important;
                flex: 1 1 auto !important;
                min-height: 0 !important;
            }
            .stTabs [data-baseweb="tab-list"] {
                padding: 0 !important;
                margin-bottom: 0 !important;
                flex-shrink: 0 !important;
                /* Remove the default 2px bottom border that adds visual
                   space — the iframe's own border provides separation. */
                border-bottom: none !important;
            }
            /* CRITICAL: only the VISIBLE panel becomes a flex column.
               Inactive panels keep BaseWeb's display:none from [hidden].
               Without :not([hidden]) the inactive Summary + Activity Log
               panels would render below the active one.                 */
            .stTabs [data-baseweb="tab-panel"]:not([hidden]) {
                display: flex !important;
                flex-direction: column !important;
                flex: 1 1 auto !important;
                min-height: 0 !important;
                overflow: hidden !important;
                padding: 0 !important;
                margin: 0 !important;
                /* Pull tab content up flush against the tab-list */
                margin-top: -2px !important;
            }
            /* Pull the tab-panel's first child (the element-container
               wrapping the iframe) flush against the tab-list above. */
            .stTabs [data-baseweb="tab-panel"]:not([hidden]) > div:first-child,
            .stTabs [data-baseweb="tab-panel"]:not([hidden]) [data-testid="stVerticalBlock"]:first-child {
                margin-top: 0 !important;
                padding-top: 0 !important;
                gap: 0 !important;
            }
            /* Zero-gap on the element-container wrapping the iframe */
            .stTabs [data-baseweb="tab-panel"]:not([hidden]) [data-testid="element-container"] {
                margin-top: 0 !important;
                padding-top: 0 !important;
            }

            /* Compact New-Document button (single line, file-pill height) */
            div[data-testid="stHorizontalBlock"] .stButton > button {
                padding: 4px 14px !important;
                font-size: 0.85rem !important;
                line-height: 1 !important;
                min-height: 32px !important;
                height: 32px !important;
                white-space: nowrap !important;
                width: auto !important;
                min-width: 0 !important;
                display: inline-flex !important;
                align-items: center !important;
                justify-content: center !important;
                gap: 4px !important;
            }
            div[data-testid="stHorizontalBlock"] .stButton {
                display: flex !important;
                justify-content: flex-end !important;
            }
            /* Tighten any markdown caption / paragraph blocks above tabs */
            div[data-testid="stMarkdownContainer"] p {
                margin-bottom: 0 !important;
            }
        </style>
        """, unsafe_allow_html=True)

    # Professional CSS styling (static — no f-string needed)
    st.markdown("""
    <style>
        /* Professional color scheme */
        .stApp {
            background-color: #F5F5F0;
        }
        
        /* Header styling */
        h1 {
            color: #2C3E50 !important;
            font-weight: 600 !important;
            font-size: 2.5rem !important;
            margin-bottom: 0.5rem !important;
        }
        
        h2 {
            color: #34495E !important;
            font-weight: 500 !important;
            font-size: 1.5rem !important;
            margin-top: 2rem !important;
            margin-bottom: 1rem !important;
        }
        
        h3 {
            color: #34495E !important;
            font-weight: 500 !important;
            font-size: 1.2rem !important;
        }
        
        /* Card-style containers */
        .stAlert, div[data-testid="stMetricValue"], div[data-testid="stExpander"] {
            background-color: white !important;
            border: 1px solid #E0E0E0 !important;
            border-radius: 8px !important;
            box-shadow: 0 2px 4px rgba(0,0,0,0.05) !important;
        }
        
        /* Success/Info/Warning boxes */
        .stAlert {
            padding: 1rem !important;
        }
        
        /* File uploader styling */
        div[data-testid="stFileUploader"] {
            background-color: white;
            border: 2px dashed #BDC3C7;
            border-radius: 8px;
            padding: 2rem;
        }

        /* Multi-file upload supported */
        
        /* Button styling */
        .stButton > button {
            background-color: #27AE60 !important;
            color: white !important;
            border: none !important;
            border-radius: 6px !important;
            padding: 0.6rem 1.5rem !important;
            font-weight: 500 !important;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1) !important;
            transition: all 0.3s ease !important;
        }
        
        .stButton > button:hover {
            background-color: #229954 !important;
            box-shadow: 0 4px 8px rgba(0,0,0,0.15) !important;
        }
        
        /* Download button */
        .stDownloadButton > button {
            background-color: #3498DB !important;
            color: white !important;
            border-radius: 6px !important;
            font-weight: 500 !important;
        }
        
        .stDownloadButton > button:hover {
            background-color: #2980B9 !important;
        }
        
        /* Metrics styling */
        div[data-testid="stMetricValue"] {
            font-size: 1.8rem !important;
            font-weight: 600 !important;
            color: #2C3E50 !important;
        }
        
        div[data-testid="stMetricLabel"] {
            color: #7F8C8D !important;
            font-size: 0.9rem !important;
            font-weight: 500 !important;
        }
        
        /* Progress bar styling */
        .stProgress > div > div > div {
            background-color: #27AE60 !important;
        }
        
        /* ── Sidebar styling ──────────────────────────────────────
           Background is dark navy (#2C3E50) for an admin/console look.
           EVERY text element inside the sidebar must be explicitly
           colored light, otherwise Streamlit's global rules (which
           paint metric values dark #2C3E50, p/span default to dark)
           cause text to vanish into the background.                  */
        section[data-testid="stSidebar"] {
            background-color: #2C3E50 !important;
        }

        /* Headings, paragraphs, generic text, st.text (pre/code),
           captions, metric labels & values, list items, links —
           force all to a light color so they're readable on dark BG. */
        section[data-testid="stSidebar"] h1,
        section[data-testid="stSidebar"] h2,
        section[data-testid="stSidebar"] h3,
        section[data-testid="stSidebar"] h4,
        section[data-testid="stSidebar"] p,
        section[data-testid="stSidebar"] span,
        section[data-testid="stSidebar"] label,
        section[data-testid="stSidebar"] li,
        section[data-testid="stSidebar"] a,
        section[data-testid="stSidebar"] pre,
        section[data-testid="stSidebar"] code,
        section[data-testid="stSidebar"] [data-testid="stText"],
        section[data-testid="stSidebar"] [data-testid="stMarkdownContainer"],
        section[data-testid="stSidebar"] [data-testid="stCaptionContainer"],
        section[data-testid="stSidebar"] [data-testid="stMetricLabel"],
        section[data-testid="stSidebar"] [data-testid="stMetricValue"],
        section[data-testid="stSidebar"] div[class*="stText"],
        section[data-testid="stSidebar"] div[class*="stMarkdown"] {
            color: #ECF0F1 !important;
        }

        /* st.text renders a <pre> — give it a subtle dark panel so the
           Session ID / Status / Created lines look readable & tidy. */
        section[data-testid="stSidebar"] pre,
        section[data-testid="stSidebar"] [data-testid="stText"] {
            background-color: rgba(255,255,255,0.06) !important;
            color: #ECF0F1 !important;
            border: 1px solid rgba(255,255,255,0.12) !important;
            border-radius: 4px !important;
            padding: 4px 8px !important;
            margin: 2px 0 !important;
            font-size: 0.85rem !important;
            white-space: pre-wrap !important;
        }

        /* Metric value (big number) — make it pop on dark BG. */
        section[data-testid="stSidebar"] [data-testid="stMetricValue"] {
            color: #ECF0F1 !important;
            font-size: 1.4rem !important;
        }
        /* Metric label (small caption above number) */
        section[data-testid="stSidebar"] [data-testid="stMetricLabel"] {
            color: #BDC3C7 !important;
        }

        /* Sidebar button styling */
        section[data-testid="stSidebar"] .stButton button {
            background-color: #229954 !important;
            color: white !important;
            border: none !important;
            border-radius: 6px !important;
            padding: 0.6rem 1.5rem !important;
            font-weight: 500 !important;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1) !important;
        }
        
        section[data-testid="stSidebar"] .stButton button:hover {
            background-color: #1E8449 !important;
            box-shadow: 0 4px 8px rgba(0,0,0,0.15) !important;
        }
        
        /* Text area styling */
        .stTextArea textarea {
            background-color: white !important;
            border: 1px solid #E0E0E0 !important;
            border-radius: 6px !important;
        }
        
        /* Expander styling */
        div[data-testid="stExpander"] {
            background-color: white !important;
            border: 1px solid #E0E0E0 !important;
        }
        
        /* Radio buttons */
        div[role="radiogroup"] {
            background-color: white;
            padding: 0.5rem;
            border-radius: 6px;
            border: 1px solid #E0E0E0;
        }
        
        /* Subtle subtitle */
        .subtitle {
            color: #7F8C8D;
            font-size: 1.1rem;
            margin-bottom: 2rem;
        }
        
        /* Card wrapper */
        .card {
            background-color: white;
            padding: 1.5rem;
            border-radius: 8px;
            border: 1px solid #E0E0E0;
            box-shadow: 0 2px 4px rgba(0,0,0,0.05);
            margin-bottom: 1rem;
        }
        
        /* Metrics card styling */
        .metrics-card {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            padding: 2rem;
            border-radius: 12px;
            color: white;
            margin-bottom: 2rem;
            box-shadow: 0 4px 12px rgba(0,0,0,0.15);
        }
        
        .metric-box {
            background-color: rgba(255, 255, 255, 0.95);
            padding: 1.2rem;
            border-radius: 8px;
            text-align: center;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }
        
        .metric-value {
            font-size: 2rem;
            font-weight: 700;
            color: #2C3E50;
            margin: 0.5rem 0;
        }
        
        .metric-label {
            font-size: 0.85rem;
            color: #7F8C8D;
            font-weight: 500;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        
        /* Tab styling */
        .stTabs [data-baseweb="tab-list"] {
            gap: 8px;
            background-color: #f8fafc;
            padding: 6px 8px;
            border-radius: 10px;
            border: 1px solid #e2e8f0;
        }
        .stTabs [data-baseweb="tab"] {
            border-radius: 8px;
            padding: 8px 20px;
            font-weight: 600;
            font-size: 0.9rem;
        }
        .stTabs [aria-selected="true"] {
            background-color: #ffffff !important;
            box-shadow: 0 1px 4px rgba(0,0,0,.08);
            border-radius: 8px;
        }
        }}
    </style>
    """, unsafe_allow_html=True)
    
    if _has_results:
        # Compact header — single small inline title, no subtitle.
        # Wrapping div lets the CSS rules above target the children.
        st.markdown(
            '<div class="compact-header">'
            '<h1>📄 Pending Legislative Processor</h1>'
            '</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown('<h1 style="text-align: center;">📄 Pending Legislative Processor</h1>', unsafe_allow_html=True)
        st.markdown('<p class="subtitle" style="text-align: center;">AI-powered text extraction and verification for legislative documents</p>', unsafe_allow_html=True)
    
    # ── Sidebar ────────────────────────────────────────────────────────
    # Session info (Session ID, Status, Created, System Stats) is a
    # developer/debug aid — useful when running locally but noise for
    # end users in production.  We only show it when the app detects
    # it is running on a local developer machine; in Kubernetes /
    # Docker deployments only the "New Session" reset button is shown.
    with st.sidebar:
        if IS_LOCAL_DEV:
            st.header("🔧 Session Info")
            session_info = st.session_state.session_manager.get_session_info(st.session_state.session_id)
            if session_info:
                st.text(f"Session ID: {st.session_state.session_id[:8]}...")
                st.text(f"Status: {session_info['status']}")
                st.text(f"Created: {session_info['created_at'].strftime('%H:%M:%S')}")

                # Show session stats
                stats = st.session_state.session_manager.get_session_stats()
                st.subheader("📊 System Stats")
                st.metric("Active Sessions", stats['active_sessions'])
                st.metric("Processing", stats['processing_sessions'])
        else:
            st.header("⚙️ Session")

        # ── Accuracy review options ─────────────────────────────────────
        # The LLM diff verifier sends any residual ``real_diffs`` (after
        # the regex artifact allowlist runs) to Claude Opus 4.7 along
        # with the highlighted page images that are already loaded for
        # the tag-accuracy audit.  It then applies a strict two-tier
        # safety policy:
        #   AUTO-DEMOTE  → moves to allowed_diffs only when verdict is
        #                  high-confidence header/footer/decorative_rule/
        #                  page_number AND ≤15 words.
        #   REVIEW-AID   → everything else stays as a real_diff but gets
        #                  an LLM suggestion attached for the human reviewer.
        # The LLM can ONLY DEMOTE — never promote — so a regex-classified
        # ``allowed`` diff can never become a ``real`` diff via this path.
        # Default OFF for first deployment; flip ON once confidence is built.
        st.subheader("🤖 Accuracy Review")
        _llm_verify_default = st.session_state.get("use_llm_diff_verification", False)
        st.session_state.use_llm_diff_verification = st.checkbox(
            "LLM-verify residual text diffs",
            value=_llm_verify_default,
            help=(
                "When ON, any text-accuracy difference that survives the "
                "regex allowlist is sent to Claude Opus 4.7 (with the "
                "highlighted page images) for a second opinion.  The LLM "
                "may auto-demote obvious page artifacts (header / footer / "
                "decorative rule / page number, ≤15 words, high confidence) "
                "and attach a review-aid suggestion for the rest.  It can "
                "never promote an allowed diff back to a real diff.  "
                "Adds one Claude API call per processed bill — only when "
                "residual diffs exist."
            ),
            key="sidebar_llm_verify_toggle",
        )

        st.markdown("---")

        if st.button("🆕 New Session", help="Start fresh session"):
            # Get old session ID before creating new one
            old_session_id = st.session_state.session_id
            
            # Delete old session
            try:
                st.session_state.session_manager.delete_session(old_session_id)
                logging.info(f"Deleted old session: {old_session_id}")
            except Exception as e:
                logging.warning(f"Could not delete old session {old_session_id}: {e}")
            
            # Create new session
            new_session_id = st.session_state.session_manager.create_session()
            st.session_state.session_id = new_session_id
            
            # Reset all session state variables
            st.session_state.detected_state = None
            st.session_state.uploaded_file = None
            st.session_state.processing = False
            st.session_state.current_step = 0
            st.session_state.step_status = {}
            st.session_state.processed_file_path = None
            st.session_state.client = None
            st.session_state.temp_pdf_path = None
            st.session_state.processing_time = None
            st.session_state.conversion_time = None
            st.session_state.chunk_stats = None
            st.session_state.last_uploaded_file_id = None  # Reset upload tracking
            st.session_state.processed_file_data = None  # Clear cached bytes (multi-pod safety)
            st.session_state.processing_logs = []  # Clear activity logs
            st.session_state.step_timings = {}  # Clear step timings
            st.session_state.total_elapsed_secs = 0
            st.session_state.conversion_elapsed_secs = 0
            st.session_state.audit_excel_path = None  # Reset audit data
            st.session_state.audit_counts = None
            st.session_state.audit_highlighted_dir = None
            st.session_state.audit_json_report = None
            st.session_state.content_accuracy_report = None
            st.session_state.gpt_audit_failed = False

            # Reset batch processing state
            st.session_state.batch_results = []
            st.session_state.batch_processing = False
            st.session_state.batch_current_idx = 0
            st.session_state.batch_pdf_paths = []
            st.session_state.batch_file_names = []
            st.session_state.batch_selected_file = 0
            st.session_state.batch_upload_id = None
            st.session_state.uploader_key_counter = st.session_state.get('uploader_key_counter', 0) + 1
            st.session_state.batch_worker = None
            
            # Reset validation review state (fixed/ignored/undo/selection)
            for _vri_key in ("vri_fixed_ids", "vri_ignored_ids", "vri_undo_history",
                             "vri_selected_item", "vri_current_page", "vri_filter_mode",
                             "vri_last_sel", "vri_edit_mode", "vri_file_key",
                             "vri_per_file_state"):
                st.session_state.pop(_vri_key, None)
            st.session_state.pop("vri_undo_history", None)
            st.session_state.pop("vri_selected_item", None)
            st.session_state.pop("vri_current_page", None)
            st.session_state.pop("vri_filter_mode", None)

            st.success(f"✅ New session created: {new_session_id[:8]}...")
            st.rerun()

    # File upload section — collapse to compact bar when results are showing
    _is_batch_done = bool(st.session_state.get('batch_results'))
    _is_single_done = (
        st.session_state.get('processed_file_path') and
        (st.session_state.get('processed_file_data') is not None
         or (st.session_state.get('processed_file_path') and os.path.exists(st.session_state.processed_file_path)))
    )

    if _has_results and (_is_single_done or _is_batch_done):
        # ── Clean results header — hide upload clutter ────────────────────
        _col_info, _col_submit_btn, _col_btn = st.columns([5, 1, 1])
        with _col_info:
            if _is_batch_done:
                _n_total = len(st.session_state.get('batch_pdf_paths', st.session_state.batch_results))
                _n_ok = sum(1 for r in st.session_state.batch_results if r.get('status') == 'success')
                st.markdown(
                    f'<div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:8px; '
                    f'padding:6px 14px; display:flex; align-items:center; gap:10px; margin-bottom:0; '
                    f'box-shadow:0 1px 3px rgba(0,0,0,.04); font-size:0.88rem;">'
                    f'<span style="font-size:1.0rem;">📦</span>'
                    f'<span style="font-weight:600; color:#1e293b;">Batch: {_n_total} files</span>'
                    f'<span style="margin-left:auto; color:#16a34a; font-weight:600; font-size:0.78rem;">'
                    f'✅ {_n_ok}/{_n_total} Processed</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
            elif st.session_state.get('temp_pdf_path'):
                _upload_id = st.session_state.get('last_uploaded_file_id') or ''
                _fname = _upload_id.rsplit('_', 1)[0] if _upload_id else Path(st.session_state.temp_pdf_path).name
                _state_code = st.session_state.get('detected_state', '')
                _state_name = US_STATE_NAMES.get(_state_code, '') if _state_code else ''
                _state_badge = (
                    f'<span style="background:#e0e7ff; color:#3730a3; padding:1px 8px; '
                    f'border-radius:10px; font-size:0.72rem; font-weight:600; margin-left:6px;">'
                    f'🏛️ {_state_code} — {_state_name}</span>'
                ) if _state_code else ''
                st.markdown(
                    f'<div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:8px; '
                    f'padding:6px 14px; display:flex; align-items:center; gap:10px; margin-bottom:0; '
                    f'box-shadow:0 1px 3px rgba(0,0,0,.04); font-size:0.88rem;">'
                    f'<span style="font-size:1.0rem;">📄</span>'
                    f'<span style="font-weight:600; color:#1e293b;">{_fname}</span>'
                    f'{_state_badge}'
                    f'<span style="margin-left:auto; color:#16a34a; font-weight:600; font-size:0.78rem;">✅ Processed</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
        with _col_submit_btn:
            _file_to_submit = st.session_state.get('processed_file_path')
            if _file_to_submit and os.path.exists(_file_to_submit):
                if st.button("🚀 Submit to Keyed", use_container_width=True, key="header_submit_keyed_btn"):
                    try:
                        _was_edited = st.session_state.get('_compare_already_saved', False)
                        _submit_to_keyed_sftp(_file_to_submit)
                        st.session_state['_last_submitted_name'] = Path(_file_to_submit).name
                        _msg = "Submitted with edits" if _was_edited else "Submitted"
                        st.toast(f"{_msg} → /ftp/keyed", icon="✅")
                        logging.info(f"[Submit to Keyed] Uploaded {Path(_file_to_submit).name} (edited={_was_edited})")
                    except Exception as _upload_err:
                        st.error(f"❌ Upload failed: {_upload_err}")
        # Show submitted confirmation bar below file info (full width, matches info bar style)
        if st.session_state.get('_last_submitted_name'):
            _sub_name = st.session_state.pop('_last_submitted_name')
            st.markdown(
                f'<div style="background:#f0fdf4; border:1px solid #bbf7d0; border-radius:10px; '
                f'padding:12px 20px; display:flex; align-items:center; gap:12px; margin-bottom:8px; '
                f'box-shadow:0 1px 4px rgba(0,0,0,.04);">'
                f'<span style="font-size:1.2rem;">✅</span>'
                f'<span style="font-weight:600; color:#166534;">Submitted</span>'
                f'<span style="font-size:0.88rem; color:#1e293b; word-break:break-all;">{_sub_name}</span>'
                f'<span style="margin-left:auto; color:#64748b; font-size:0.85rem;">→ /ftp/keyed</span>'
                f'</div>',
                unsafe_allow_html=True,
            )
        with _col_btn:
            if st.button("➕ New", type="primary", help="Process a new document"):
                # Reset all processing state so user can upload a fresh file
                st.session_state.processed_file_path = None
                st.session_state.temp_pdf_path = None
                st.session_state.last_uploaded_file_id = None
                st.session_state.processed_file_data = None
                st.session_state.processing_logs = []
                st.session_state.step_timings = {}
                st.session_state.total_elapsed_secs = 0
                st.session_state.conversion_elapsed_secs = 0
                st.session_state.audit_excel_path = None
                st.session_state.audit_counts = None
                st.session_state.audit_highlighted_dir = None
                st.session_state.audit_json_report = None
                st.session_state.content_accuracy_report = None
                st.session_state.gpt_audit_failed = False
                st.session_state.batch_results = []
                st.session_state.batch_processing = False
                st.session_state.batch_current_idx = 0
                st.session_state.batch_pdf_paths = []
                st.session_state.batch_file_names = []
                st.session_state.batch_selected_file = 0
                st.session_state.batch_upload_id = None
                st.session_state.uploader_key_counter = st.session_state.get('uploader_key_counter', 0) + 1
                st.session_state.batch_worker = None
                for _vri_key in ("vri_fixed_ids", "vri_ignored_ids", "vri_undo_history",
                                 "vri_selected_item", "vri_current_page", "vri_filter_mode",
                                 "vri_last_sel", "vri_edit_mode", "vri_file_key",
                                 "vri_per_file_state"):
                    st.session_state.pop(_vri_key, None)
                st.rerun()

        # No file uploader shown — results take full space
        uploaded_files = []
    elif st.session_state.get('processing'):
        # ── Mid-processing: hide the bulky uploader + "File ready" banner
        # and show ONLY a slim status pill with the filename being
        # processed.  The processing-steps + activity log below take care
        # of the live status; the uploader UI just adds visual noise.
        _proc_name = ''
        if st.session_state.get('temp_pdf_path'):
            _proc_name = Path(st.session_state.temp_pdf_path).name
        elif st.session_state.get('batch_file_names'):
            _bn = st.session_state.batch_file_names
            _bi = st.session_state.get('batch_current_idx', 0)
            if 0 <= _bi < len(_bn):
                _proc_name = f"{_bn[_bi]}  ({_bi+1}/{len(_bn)})"
        _proc_state_code = st.session_state.get('detected_state', '')
        _proc_state_name = US_STATE_NAMES.get(_proc_state_code, '') if _proc_state_code else ''
        _proc_state_badge = (
            f'<span style="background:#e0e7ff; color:#3730a3; padding:1px 8px; '
            f'border-radius:10px; font-size:0.72rem; font-weight:600; margin-left:6px;">'
            f'🏛️ {_proc_state_code} — {_proc_state_name}</span>'
        ) if _proc_state_code else ''
        st.markdown(
            f'<div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:8px; '
            f'padding:6px 14px; display:flex; align-items:center; gap:10px; margin-bottom:8px; '
            f'box-shadow:0 1px 3px rgba(0,0,0,.04); font-size:0.88rem;">'
            f'<span style="font-size:1.0rem;">⏳</span>'
            f'<span style="font-weight:600; color:#1e293b;">{_proc_name or "Processing…"}</span>'
            f'{_proc_state_badge}'
            f'<span style="margin-left:auto; color:#2563eb; font-weight:600; font-size:0.78rem;">'
            f'🔄 Processing</span>'
            f'</div>',
            unsafe_allow_html=True,
        )
        uploaded_files = []
    else:
        # Radio toggle between Upload, Downloads, and Pipeline Sessions
        _input_mode = st.radio(
            "Input source",
            ["📁 Upload PDF Files", "📥 Downloaded Files", "📂 Pipeline Sessions"],
            horizontal=True,
            key="input_mode_radio",
            label_visibility="collapsed",
        )

        if _input_mode == "📁 Upload PDF Files":
            # Full upload section — before processing
            st.header("📁 Upload PDF Files")
            st.caption("Upload one file for single processing, or multiple files (up to 20, max 50 MB total) for serial batch processing.")
            _uploader_key = f"pdf_uploader_{st.session_state.uploader_key_counter}"
            uploaded_files = st.file_uploader(
                "Choose PDF file(s) to process",
                type=['pdf'],
                accept_multiple_files=True,
                key=_uploader_key,
                help="Upload one or more PDF files (max 15 pages each)"
            )
        elif _input_mode == "📥 Downloaded Files":
            # ── Process files from pending_downloads/ ─────────────────────
            st.header("📥 Downloaded Files")
            uploaded_files = []
            _proj_root = Path(__file__).resolve().parent.parent
            if os.environ.get('APP_TMP_DIR'):
                _downloads_dir = Path(os.environ['APP_TMP_DIR']) / "pending_downloads"
            else:
                _downloads_dir = _proj_root / "tmp" / "pending_downloads"
            _downloads_dir.mkdir(parents=True, exist_ok=True)
            st.caption(f"📂 `{_downloads_dir}`")

            # Check SFTP button
            if st.button("🔄 Check SFTP for New Files", use_container_width=True, key="dl_check_sftp_btn"):
                from pipeline_runner import download_new_files
                with st.spinner("Connecting to SFTP..."):
                    _new_files = download_new_files()
                if _new_files:
                    st.success(f"📥 Downloaded {len(_new_files)} new file(s) from SFTP")
                    st.rerun()
                else:
                    st.info("No new files on SFTP server")

            # List available ZIPs
            _zip_files = sorted(_downloads_dir.glob("*.zip"), key=lambda x: x.stat().st_mtime, reverse=True)
            if _zip_files:
                _zip_labels = [f"{z.name}  ({z.stat().st_size // 1024} KB)" for z in _zip_files]
                _selected_zips = st.multiselect(
                    "Select ZIP file(s) to process",
                    range(len(_zip_files)),
                    format_func=lambda i: _zip_labels[i],
                    key="dl_zip_select",
                )

                if _selected_zips:
                    st.caption(f"Selected {len(_selected_zips)} ZIP(s) for conversion")
                    if st.button("⚙️ Extract & Convert", type="primary", use_container_width=True, key="dl_convert_btn"):
                        import shutil as _shutil2
                        from pipeline_runner import extract_zips, convert_pdf, get_today_folder

                        _chosen_zips = [_zip_files[i] for i in _selected_zips]
                        with st.spinner(f"Extracting {len(_chosen_zips)} ZIP(s)..."):
                            _extracted = extract_zips(_chosen_zips)

                        if _extracted:
                            _progress = st.progress(0, text="Converting...")
                            _total = len(_extracted)
                            _success = 0
                            _converted_paths = []
                            for _idx, _item in enumerate(_extracted):
                                _progress.progress((_idx + 1) / _total, text=f"Converting {_idx + 1}/{_total}: {Path(_item['pdf_path']).name}")
                                _out_dir = Path(_item['date_folder']) / _item['session_id'] / 'output'
                                _result = convert_pdf(_item['pdf_path'], _item['state'], _out_dir)
                                if _result:
                                    _success += 1
                                    _converted_paths.append(_result)
                            _progress.progress(1.0, text="Uploading...")

                            # Upload to SFTP /ftp/keyed
                            _upload_ok = 0
                            _upload_log_entries = []
                            try:
                                from ftp_upload import connect_sftp as _up_connect, SFTP_USER as _up_user, REMOTE_DIR as _up_remote
                                _ftp_pw = os.environ.get("FTP_PASSWORD", "")
                                if _ftp_pw and _converted_paths:
                                    _transport, _sftp = _up_connect(_up_user, _ftp_pw)
                                    try:
                                        for _cp in _converted_paths:
                                            _sftp.put(_cp, f"{_up_remote}/{Path(_cp).name}")
                                            _upload_ok += 1
                                            _upload_log_entries.append(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] UPLOADED {Path(_cp).name} → {_up_remote}/{Path(_cp).name}")
                                            logging.info(f"[SFTP] Uploaded {Path(_cp).name} to {_up_remote}")
                                    finally:
                                        _sftp.close()
                                        _transport.close()
                            except Exception as _ue:
                                st.warning(f"⚠️ SFTP upload error: {_ue}")
                                _upload_log_entries.append(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ERROR: {_ue}")

                            # Write upload log via session manager
                            if _upload_log_entries:
                                _sm = st.session_state.get('session_manager') or get_session_manager()
                                for _entry in _upload_log_entries:
                                    _sm.write_upload_log(_entry)

                            _progress.empty()
                            st.markdown(
                                f'<div style="background:#f0fdf4; border:1px solid #bbf7d0; border-radius:10px; '
                                f'padding:12px 20px; display:flex; align-items:center; gap:12px; margin-bottom:8px; '
                                f'box-shadow:0 1px 4px rgba(0,0,0,.04);">'
                                f'<span style="font-size:1.2rem;">✅</span>'
                                f'<span style="font-weight:600; color:#166534;">Done</span>'
                                f'<span style="font-size:0.88rem; color:#1e293b;">{_success}/{_total} converted</span>'
                                f'<span style="font-size:0.82rem; color:#475569;">| {_upload_ok} uploaded to /ftp/keyed</span>'
                                f'<span style="margin-left:auto; color:#64748b; font-size:0.85rem;">→ tmp/pending_processing/{get_today_folder().name}/</span>'
                                f'</div>',
                                unsafe_allow_html=True,
                            )
                            st.rerun()
                        else:
                            st.warning("No new PDFs to extract (already processed)")
            else:
                st.info("No ZIP files in pending_downloads/. Click 'Check SFTP' to download.")
        else:
            # ── Pipeline Sessions (SFTP → Convert → Review) ───────────────
            st.header("📂 Pipeline Sessions")
            uploaded_files = []

            _pipeline_sm = st.session_state.get('session_manager') or get_session_manager()
            _p_sessions = _pipeline_sm.list_persisted_sessions()
            if _p_sessions:
                    _session_labels = []
                    for _ps in _p_sessions:
                        _prefix = f"[{_ps['date_label']}] " if _ps['date_label'] else ""
                        _label = f"{_prefix}{_ps['state']} | {_ps['n_pdfs']} PDFs | {_ps['n_outputs']} converted"
                        _session_labels.append(_label)

                    _sel_idx = st.selectbox(
                        "Select session",
                        range(len(_p_sessions)),
                        format_func=lambda i: _session_labels[i],
                        key="pipeline_session_select",
                    )
                    _selected_ps = _p_sessions[_sel_idx]
                    _selected_session = _selected_ps['path']
                    _sel_output_dir = _selected_session / "output"
                    _sel_input_dir = _selected_session / "input"

                    _converted_files = _selected_ps['output_files']
                    if _converted_files:
                        st.caption(f"✅ {len(_converted_files)} file(s) ready for review")
                        _file_choice = st.selectbox(
                            "Pick file to review",
                            range(len(_converted_files)),
                            format_func=lambda i: _converted_files[i].name,
                            key="pipeline_file_select",
                        )
                        _chosen_file = _converted_files[_file_choice]

                        _col_load, _col_submit_one, _col_submit_all = st.columns(3)
                        with _col_load:
                            if st.button("📄 Load for Review", use_container_width=True, key="load_pipeline_file_btn"):
                                st.session_state.processed_file_path = str(_chosen_file)
                                try:
                                    with open(_chosen_file, 'rb') as _f:
                                        st.session_state.processed_file_data = _f.read()
                                except Exception:
                                    st.session_state.processed_file_data = None
                                _src_pdf = _sel_input_dir / (_chosen_file.stem + ".pdf")
                                if not _src_pdf.exists():
                                    _src_pdf = _selected_session / (_chosen_file.stem + ".pdf")
                                if _src_pdf.exists():
                                    st.session_state.temp_pdf_path = str(_src_pdf)
                                _det_state = _selected_session.name.split("_")[0]
                                st.session_state.detected_state = _det_state
                                st.session_state.processing_time = "pipeline"

                                # ── Run validation if reports don't exist yet ──────
                                _acc_json = _chosen_file.parent / f"{_chosen_file.stem}_accuracy.json"
                                if not _acc_json.exists() and _src_pdf.exists():
                                    try:
                                        import importlib.util as _ilu
                                        _pr_spec = _ilu.spec_from_file_location(
                                            "pipeline_runner",
                                            str(Path(__file__).parent / "pipeline_runner.py"),
                                        )
                                        _pr_mod = _ilu.module_from_spec(_pr_spec)
                                        _pr_spec.loader.exec_module(_pr_mod)
                                        _converted_text = _chosen_file.read_text(encoding="utf-8")
                                        _pr_mod.validate_output(
                                            str(_chosen_file), str(_src_pdf),
                                            _det_state, _converted_text,
                                        )
                                        logging.info(f"[Pipeline] Ran validation for existing file: {_chosen_file.name}")
                                    except Exception as _val_err:
                                        logging.warning(f"[Pipeline] Validation on load failed: {_val_err}")

                                # Load accuracy report into session state if available
                                if _acc_json.exists():
                                    try:
                                        import json as _json
                                        st.session_state.content_accuracy_report = _json.loads(
                                            _acc_json.read_text(encoding="utf-8")
                                        )
                                    except Exception:
                                        pass

                                st.success(f"Loaded: {_chosen_file.name}")
                                st.rerun()
                        with _col_submit_one:
                            if st.button("🚀 Submit File", type="primary", use_container_width=True, key="submit_one_keyed_btn"):
                                try:
                                    _submit_to_keyed_sftp(str(_chosen_file))
                                    st.toast(f"Submitted to /ftp/keyed", icon="✅")
                                    st.markdown(
                                        f'<div style="background:#f0fdf4; border:1px solid #bbf7d0; border-radius:10px; '
                                        f'padding:12px 20px; display:flex; align-items:center; gap:12px; margin-bottom:8px; '
                                        f'box-shadow:0 1px 4px rgba(0,0,0,.04);">'
                                        f'<span style="font-size:1.2rem;">✅</span>'
                                        f'<span style="font-weight:600; color:#166534;">Submitted</span>'
                                        f'<span style="font-size:0.88rem; color:#1e293b; word-break:break-all;">{_chosen_file.name}</span>'
                                        f'<span style="margin-left:auto; color:#64748b; font-size:0.85rem;">→ /ftp/keyed</span>'
                                        f'</div>',
                                        unsafe_allow_html=True,
                                    )
                                except Exception as _e:
                                    st.error(f"❌ Upload failed: {_e}")
                        with _col_submit_all:
                            if st.button("🚀 Submit All", use_container_width=True, key="submit_all_keyed_btn"):
                                _submitted = 0
                                for _cf in _converted_files:
                                    try:
                                        _submit_to_keyed_sftp(str(_cf))
                                        _submitted += 1
                                    except Exception as _e:
                                        st.error(f"❌ {_cf.name}: {_e}")
                                if _submitted:
                                    st.toast(f"{_submitted} file(s) submitted", icon="✅")
                                    st.markdown(
                                        f'<div style="background:#f0fdf4; border:1px solid #bbf7d0; border-radius:10px; '
                                        f'padding:12px 20px; display:flex; align-items:center; gap:12px; margin-bottom:8px; '
                                        f'box-shadow:0 1px 4px rgba(0,0,0,.04);">'
                                        f'<span style="font-size:1.2rem;">✅</span>'
                                        f'<span style="font-weight:600; color:#166534;">Submitted</span>'
                                        f'<span style="font-size:0.88rem; color:#1e293b;">{_submitted}/{len(_converted_files)} files</span>'
                                        f'<span style="margin-left:auto; color:#64748b; font-size:0.85rem;">→ /ftp/keyed</span>'
                                        f'</div>',
                                        unsafe_allow_html=True,
                                    )
                    else:
                        _pdfs = sorted(_sel_input_dir.glob("*.pdf")) if _sel_input_dir.exists() else sorted(_selected_session.glob("*.pdf"))
                        if _pdfs:
                            st.caption(f"⏳ {len(_pdfs)} PDF(s) awaiting conversion")
                        else:
                            st.caption("Empty session")
            else:
                st.info("No pipeline sessions yet")

    # Normalize: uploaded_files is always a list (possibly empty)
    if uploaded_files is None:
        uploaded_files = []
    elif not isinstance(uploaded_files, list):
        uploaded_files = [uploaded_files]

    if uploaded_files:
        # ── Validate all files ────────────────────────────────────────────
        MAX_TOTAL_SIZE = 5 * 1024 * 1024   # 5 MB total for all files combined
        MAX_BATCH = 20
        MAX_PAGES = 15              # Default page limit for PyMuPDF/Hybrid modes
        MAX_PAGES_VISION = 50       # Higher limit for Vision mode (scanned PDFs, CO state)
        valid_files = []
        skipped_names = []
        skipped_size_names = []
        _running_size = 0

        for uf in uploaded_files:
            # Quick page-count check (read bytes in memory, no temp file needed)
            try:
                import pypdfium2 as pdfium
                _bytes = uf.read()
                uf.seek(0)  # reset so downstream code can re-read
                _pdf = pdfium.PdfDocument(_bytes)
                _pc = len(_pdf)
                _pdf.close()
                # Only enforce page limit for batch uploads (>1 file).
                # Single files are allowed through — the UI shows Hybrid/Extract+Clean buttons instead.
                if _pc > MAX_PAGES and len(uploaded_files) > 1:
                    # Check if this file would use Vision mode (CO state or scanned PDF)
                    # — if so, allow up to MAX_PAGES_VISION
                    _is_vision_file = False
                    try:
                        import pymupdf as _pym
                        _doc = _pym.open(stream=_bytes, filetype="pdf")
                        _p1 = _doc[0].get_text("text")[:4000] if len(_doc) > 0 else ""
                        _doc.close()
                        # CO state detection
                        if any(m in _p1.upper() for m in ("STATE OF COLORADO", "COLORADO GENERAL ASSEMBLY", "LLS NO.")):
                            _is_vision_file = True
                        # Scanned PDF detection (very little extractable text)
                        elif len(_p1.strip()) < 50:
                            _is_vision_file = True
                    except Exception:
                        pass
                    _effective_limit = MAX_PAGES_VISION if _is_vision_file else MAX_PAGES
                    if _pc > _effective_limit:
                        skipped_names.append(f"{uf.name} ({_pc} pages)")
                        continue
            except Exception:
                pass  # if we can't check, let it through — batch upload will catch it

            # Check if adding this file would exceed the 5 MB total limit
            if _running_size + uf.size > MAX_TOTAL_SIZE:
                skipped_size_names.append(f"{uf.name} ({format_file_size(uf.size)})")
                continue

            _running_size += uf.size
            valid_files.append(uf)

        # Only show skip messages BEFORE processing starts — hide during/after
        _is_processing = (
            st.session_state.get('batch_processing')
            or st.session_state.get('processing')
            or bool(st.session_state.get('batch_results'))
            or bool(st.session_state.get('processed_file_path'))
        )

        if not _is_processing:
            if skipped_names:
                st.info(f"ℹ️ {len(skipped_names)} file(s) skipped (page limit): {', '.join(skipped_names)}")

            if skipped_size_names:
                st.warning(
                    f"⚠️ {len(skipped_size_names)} file(s) auto-skipped to stay within "
                    f"**{format_file_size(MAX_TOTAL_SIZE)}** total limit: {', '.join(skipped_size_names)}"
                )

        if not valid_files:
            st.error("❌ No files to process — all files were skipped due to page or size limits.")
            st.stop()

        # ── Deduplicate files (same name + same size = same file) ─────────
        _seen_file_ids = set()
        _deduped_files = []
        _dup_names = []
        for uf in valid_files:
            _fid = f"{uf.name}_{uf.size}"
            if _fid in _seen_file_ids:
                _dup_names.append(uf.name)
                continue
            _seen_file_ids.add(_fid)
            _deduped_files.append(uf)
        if _dup_names and not _is_processing:
            st.warning(f"⚠️ Duplicate file(s) removed: {', '.join(_dup_names)}")
        valid_files = _deduped_files

        if not valid_files:
            st.error("❌ No files to process — all files were duplicates.")
            st.stop()

        if len(valid_files) > MAX_BATCH:
            st.error(f"❌ **Too many valid files!** Maximum {MAX_BATCH} files per batch. You have {len(valid_files)} valid files.")
            st.stop()
        # ── Detect single vs batch mode ───────────────────────────────────
        is_batch = len(valid_files) > 1

        # Build a unique ID for the current upload set
        current_upload_id = "|".join(sorted(f"{f.name}_{f.size}" for f in valid_files))

        # ── Auto-clear old results only when a DIFFERENT set of files is uploaded ──
        _prev_upload_id = st.session_state.get('batch_upload_id')
        _prev_single_id = st.session_state.get('last_uploaded_file_id')
        _has_old_results = bool(st.session_state.get('batch_results')) or bool(st.session_state.get('processed_file_path'))
        _is_different_files = (
            _has_old_results
            and not st.session_state.get('batch_processing')  # don't clear mid-batch!
            and _prev_upload_id != current_upload_id
            and _prev_single_id != (f"{valid_files[0].name}_{valid_files[0].size}" if len(valid_files) == 1 else None)
        )
        if _is_different_files:
            st.session_state.batch_results = []
            st.session_state.batch_processing = False
            st.session_state.batch_current_idx = 0
            st.session_state.batch_pdf_paths = []
            st.session_state.batch_file_names = []
            st.session_state.batch_selected_file = 0
            st.session_state.batch_upload_id = None
            st.session_state.batch_worker = None
            st.session_state.processed_file_path = None
            st.session_state.processed_file_data = None
            st.session_state.processing = False
            st.session_state.temp_pdf_path = None
            st.session_state.last_uploaded_file_id = None
            st.session_state.processing_time = None
            st.session_state.detected_state = None
            st.session_state.processing_logs = []
            st.session_state.audit_excel_path = None
            st.session_state.audit_counts = None
            st.session_state.audit_highlighted_dir = None
            st.session_state.audit_json_report = None
            st.session_state.content_accuracy_report = None
            st.session_state.gpt_audit_failed = False
            for _vri_key in ("vri_fixed_ids", "vri_ignored_ids", "vri_undo_history",
                             "vri_selected_item", "vri_current_page", "vri_filter_mode",
                             "vri_last_sel", "vri_edit_mode", "vri_file_key",
                             "vri_per_file_state"):
                st.session_state.pop(_vri_key, None)

        if is_batch:
            # ══════════════════════════════════════════════════════════════
            # BATCH MODE: multiple files
            # ══════════════════════════════════════════════════════════════
            if st.session_state.get('batch_upload_id') != current_upload_id:
                # New batch of files — upload & validate all
                st.session_state.batch_results = []
                st.session_state.batch_processing = False
                st.session_state.batch_current_idx = 0
                st.session_state.batch_pdf_paths = []
                st.session_state.batch_file_names = []
                st.session_state.batch_selected_file = 0
                st.session_state.batch_worker = None
                st.session_state.processed_file_path = None
                st.session_state.processed_file_data = None

                session_mgr = st.session_state.session_manager
                uploaded_paths = []
                uploaded_names = []
                all_ok = True

                for uf in valid_files:
                    with st.spinner(f"Uploading {uf.name}..."):
                        temp_path = session_mgr.upload_file(
                            st.session_state.session_id, uf, "AUTO"
                        )
                    if not temp_path:
                        st.error(f"❌ Failed to upload {uf.name}")
                        all_ok = False
                        continue

                    # Scanned PDF detection (no longer blocks — routes to visual mode)
                    if detect_pdf_type is not None:
                        with st.spinner(f"🔍 Analyzing **{uf.name}** — detecting document type..."):
                            pdf_analysis = detect_pdf_type(temp_path)
                        if pdf_analysis and pdf_analysis['type'] == 'scanned':
                            logging.info(f"Scanned PDF detected: {uf.name} — will use Visual mode")

                    # Page count gate
                    try:
                        import pypdfium2 as pdfium
                        _pdf = pdfium.PdfDocument(temp_path)
                        _pc = len(_pdf)
                        _pdf.close()
                        if _pc > 15:
                            logging.info(f"Skipped {uf.name}: {_pc} pages (max 15)")
                            try:
                                os.remove(temp_path)
                            except Exception:
                                pass
                            continue
                    except Exception:
                        pass

                    uploaded_paths.append(temp_path)
                    uploaded_names.append(uf.name)

                if uploaded_paths:
                    st.session_state.batch_pdf_paths = uploaded_paths
                    st.session_state.batch_file_names = uploaded_names
                    st.session_state.batch_upload_id = current_upload_id
                    st.success(f"✅ {len(uploaded_paths)} file(s) ready for batch processing")
                else:
                    st.error("❌ No valid files to process.")
                    st.stop()
            else:
                # Same batch already uploaded
                st.success(f"✅ {len(st.session_state.batch_file_names)} file(s) ready")

            # Show file list only before processing starts (not during or after)
            if (st.session_state.batch_file_names
                    and not st.session_state.batch_results
                    and not st.session_state.batch_processing):
                _file_list_html = '<div style="background:white;border:1px solid #e2e8f0;border-radius:8px;padding:12px 16px;margin:8px 0;">'
                for i, fn in enumerate(st.session_state.batch_file_names):
                    _file_list_html += f'<div style="padding:4px 0;font-size:0.88rem;"><span style="color:#64748b;">#{i+1}</span> 📄 <strong>{fn}</strong></div>'
                _file_list_html += '</div>'
                st.markdown(_file_list_html, unsafe_allow_html=True)

            # Show batch processing UI
            show_batch_processing()

        else:
            # ══════════════════════════════════════════════════════════════
            # SINGLE FILE MODE (existing behavior)
            # ══════════════════════════════════════════════════════════════
            uploaded_file = valid_files[0]
            current_file_id = f"{uploaded_file.name}_{uploaded_file.size}"

            # Clear batch state if switching from batch to single
            if st.session_state.get('batch_results'):
                st.session_state.batch_results = []
                st.session_state.batch_upload_id = None

            if 'last_uploaded_file_id' not in st.session_state:
                st.session_state.last_uploaded_file_id = None

            if st.session_state.last_uploaded_file_id != current_file_id:
                try:
                    # Clear all previous run state so the new file starts fresh
                    st.session_state.processed_file_path = None
                    st.session_state.processed_file_data = None
                    st.session_state.processing = False
                    st.session_state.current_step = 0
                    st.session_state.step_status = {}
                    st.session_state.processing_time = None
                    st.session_state.conversion_time = None
                    st.session_state.chunk_stats = None
                    st.session_state.detected_state = None
                    st.session_state.audit_excel_path = None
                    st.session_state.audit_counts = None
                    st.session_state.audit_highlighted_dir = None
                    st.session_state.audit_json_report = None
                    st.session_state.content_accuracy_report = None
                    st.session_state.gpt_audit_failed = False
                    st.session_state.pop('_scanned_hitl_approved', None)
                    st.session_state.pop('_pdf_is_scanned', None)

                    # Reset validation review state (fixed/ignored from previous file)
                    for _vri_key in ("vri_fixed_ids", "vri_ignored_ids", "vri_undo_history",
                                     "vri_selected_item", "vri_current_page", "vri_filter_mode",
                                     "vri_last_sel", "vri_edit_mode", "vri_file_key",
                                     "vri_per_file_state"):
                        st.session_state.pop(_vri_key, None)
                    st.session_state.pop("vri_undo_history", None)
                    st.session_state.pop("vri_selected_item", None)
                    st.session_state.pop("vri_current_page", None)
                    st.session_state.pop("vri_filter_mode", None)

                    # Upload file using session manager
                    session_mgr = st.session_state.session_manager

                    _scanned_detected = False
                    temp_path = None
                    with st.status(f"📄 Processing **{uploaded_file.name}**...", expanded=True) as _upload_status:
                        st.write("📤 Uploading file...")
                        temp_path = session_mgr.upload_file(
                            st.session_state.session_id,
                            uploaded_file,
                            "AUTO"
                        )

                        if temp_path:
                            # ── Scanned PDF gate ──────────────────────────────────
                            if detect_pdf_type is not None:
                                st.write("🔍 Analyzing PDF — detecting document type...")
                                pdf_analysis = detect_pdf_type(temp_path)
                                st.session_state['_pdf_analysis'] = pdf_analysis
                                if pdf_analysis:
                                    _pg = pdf_analysis.get('total_pages', '?')
                                    _tp = pdf_analysis.get('type', 'unknown')
                                    # If any pages have images, treat as hybrid for display
                                    _any_images = any(d.get('has_images') for d in pdf_analysis.get('details', []))
                                    if _any_images and _tp == 'digital':
                                        _tp_label = 'Hybrid (text + images)'
                                    elif _tp == 'digital':
                                        _tp_label = 'Digital (text-based)'
                                    elif _tp == 'hybrid':
                                        _tp_label = 'Hybrid (text + images)'
                                    else:
                                        _tp_label = _tp.capitalize()
                                    st.write(f"📊 **{_pg} pages** detected — Type: **{_tp_label}**")
                                if pdf_analysis and pdf_analysis['type'] == 'scanned':
                                    st.session_state['_pdf_is_scanned'] = True
                                    _upload_status.update(
                                        label=f"📷 {uploaded_file.name} — Scanned PDF (will use Visual mode)",
                                        state="complete", expanded=False)
                                else:
                                    st.session_state['_pdf_is_scanned'] = False
                                    _upload_status.update(
                                        label=f"✅ {uploaded_file.name} — Ready to process",
                                        state="complete", expanded=False)
                            else:
                                _upload_status.update(
                                    label=f"✅ {uploaded_file.name} — Uploaded",
                                    state="complete", expanded=False)
                            # ──────────────────────────────────────────────────────
                        else:
                            _upload_status.update(
                                label=f"❌ {uploaded_file.name} — Upload failed",
                                state="error")
                            st.error("❌ Failed to upload file - check logs for details")

                    # Show scanned-PDF info banner (no longer blocks processing)
                    if st.session_state.get('_pdf_is_scanned', False):
                        st.info(
                            f"📷 **Scanned PDF Detected**\n\n"
                            f"The uploaded file **{uploaded_file.name}** is a "
                            f"**scanned (image-based) PDF**.\n\n"
                            f"It will be processed using the **Visual (Claude Opus)** "
                            f"pipeline which converts page images to text."
                        )

                    if temp_path:
                        st.session_state.temp_pdf_path = temp_path
                        st.session_state.last_uploaded_file_id = current_file_id
                        # No success banner — the slim processing pill
                        # (or the file-ready pill once processing finishes)
                        # makes the upload status implicit.
                except Exception as e:
                    st.error(f"❌ **File Upload Failed**")
                    st.error(f"Error: {str(e)}")
                    import traceback
                    with st.expander("Error Details"):
                        st.code(traceback.format_exc())
            else:
                # File already uploaded — no banner needed; the processing
                # pill or the auto-start info message takes over.
                pass

            # Determine PDF page count for mode gating
            if st.session_state.temp_pdf_path:
                try:
                    import pypdfium2 as pdfium
                    _pdf = pdfium.PdfDocument(st.session_state.temp_pdf_path)
                    st.session_state['_pdf_page_count'] = len(_pdf)
                    _pdf.close()
                except Exception:
                    st.session_state['_pdf_page_count'] = 0

                # ── Fast state detection (numerical ID → page-1 text scan) ──
                # We pre-detect the state at upload time so the auto-trigger
                # logic can route per-state (e.g. CO must always use the
                # Visual / Process Document pipeline).  The pipelines still
                # run their own detection; this just lets us pick the right
                # one before kicking it off.
                try:
                    # 1) Try numerical ID from filename (instant, no file I/O)
                    _early_state = (
                        detect_state_from_numerical_id(st.session_state.temp_pdf_path)
                        if detect_state_from_numerical_id else None
                    )
                    # 2) Fall back to page-1 text markers
                    if not _early_state:
                        _early_state = _detect_state_fast(st.session_state.temp_pdf_path)
                    if _early_state:
                        st.session_state.detected_state = _early_state
                        logging.info(
                            "Pre-detected state %s for %s",
                            _early_state, Path(st.session_state.temp_pdf_path).name,
                        )
                except Exception as _state_err:
                    logging.debug("Pre-upload state detection failed: %s", _state_err)

            show_single_file_processing()
    else:
        # No files in uploader — but check if we have results or active worker
        if _has_results and _is_single_done:
            # Results already exist — show them directly (upload hidden by "New Document" UI)
            show_single_file_processing()
        elif st.session_state.get('processing'):
            # Single-file processing is mid-run (uploader was hidden by the
            # processing-state pill we render above).  We still need to call
            # show_single_file_processing() so the live progress + activity
            # log keep rendering on each rerun.
            show_single_file_processing()
        elif st.session_state.get('batch_processing') and st.session_state.get('batch_worker'):
            # Worker still running (uploader was cleared by key bump) — show live UI
            show_batch_processing()
        elif st.session_state.get('batch_results') or (_has_results and _is_batch_done):
            n_files = len(st.session_state.batch_pdf_paths) if st.session_state.get('batch_pdf_paths') else len(st.session_state.get('batch_results', []))
            if st.session_state.get('batch_results') and len(st.session_state.batch_results) >= n_files and n_files > 0:
                show_batch_results_section()
            else:
                # Partial results exist but worker stopped — show what we have
                show_batch_results_section()
        else:
            if st.session_state.get('input_mode_radio') == "📁 Upload PDF Files":
                st.info("👆 Please upload one or more PDF files to begin processing")


# ══════════════════════════════════════════════════════════════════════════════
#  UI Helper: Styled Activity Feed & Pipeline Tracker
# ══════════════════════════════════════════════════════════════════════════════

def _build_activity_feed_html(logs: list[dict]) -> str:
    """Build a modern styled activity-feed HTML for processing logs.

    Each log entry is rendered as a timeline row with a colored dot,
    monospace timestamp badge, and the message text.
    """
    if not logs:
        return ""

    _css = """
    <style>
    .af-wrap{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
             padding:4px 0;}
    .af-row{display:flex;align-items:flex-start;gap:10px;padding:5px 0 5px 4px;
            border-left:2px solid #e2e8f0;margin-left:6px;position:relative;}
    .af-row:last-child{border-left-color:transparent;}
    .af-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0;margin-top:5px;
            position:relative;left:-10px;margin-right:-10px;}
    .af-dot-info{background:#3b82f6;}
    .af-dot-ok{background:#22c55e;}
    .af-dot-warn{background:#f59e0b;}
    .af-dot-err{background:#ef4444;}
    .af-time{font-family:'SF Mono',SFMono-Regular,Consolas,'Liberation Mono',Menlo,monospace;
             font-size:0.72rem;background:#f1f5f9;color:#64748b;padding:2px 7px;
             border-radius:4px;white-space:nowrap;flex-shrink:0;}
    .af-msg{font-size:0.82rem;color:#334155;line-height:1.4;word-break:break-word;}
    .af-msg-warn{color:#92400e;}
    .af-msg-err{color:#991b1b;font-weight:600;}
    </style>"""

    rows = []
    for log in logs:
        lvl = log.get("level", "info")
        msg = log.get("message", "")
        ts = log.get("time", "")

        # Determine dot color and message class from content + level
        if lvl == "error" or msg.startswith("❌"):
            dot_cls, msg_cls = "af-dot-err", "af-msg-err"
        elif lvl == "warning" or msg.startswith("⚠️"):
            dot_cls, msg_cls = "af-dot-warn", "af-msg-warn"
        elif "✅" in msg or "🏁" in msg:
            dot_cls, msg_cls = "af-dot-ok", "af-msg"
        else:
            dot_cls, msg_cls = "af-dot-info", "af-msg"

        rows.append(
            f'<div class="af-row">'
            f'  <span class="af-dot {dot_cls}"></span>'
            f'  <span class="af-time">{ts}</span>'
            f'  <span class="{msg_cls}">{msg}</span>'
            f'</div>'
        )

    return _css + '<div class="af-wrap">' + "\n".join(rows) + '</div>'


def _build_pipeline_tracker_html(conversion_steps: list[dict],
                                  validation_steps: list[dict]) -> str:
    """Build a modern pipeline tracker card grid showing each phase as
    a group of step tiles with status indicators.
    """
    _css = """
    <style>
    .pt-phase{margin-bottom:18px;}
    .pt-phase-hdr{display:flex;align-items:center;gap:8px;margin-bottom:10px;}
    .pt-phase-badge{font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.05em;
                    padding:3px 10px;border-radius:12px;}
    .pt-phase-1{background:#dcfce7;color:#166534;}
    .pt-phase-2{background:#dbeafe;color:#1e40af;}
    .pt-phase-title{font-size:0.92rem;font-weight:700;color:#1e293b;}
    .pt-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px;}
    .pt-card{background:#ffffff;border:1px solid #e2e8f0;border-radius:10px;padding:12px 14px;
             display:flex;align-items:center;gap:10px;transition:box-shadow .15s,border-color .15s;}
    .pt-card:hover{box-shadow:0 2px 8px rgba(0,0,0,.06);border-color:#cbd5e1;}
    .pt-card-ok{border-left:3px solid #22c55e;}
    .pt-card-warn{border-left:3px solid #f59e0b;}
    .pt-card-fail{border-left:3px solid #ef4444;}
    .pt-card-run{border-left:3px solid #3b82f6;background:#eff6ff;}
    .pt-card-wait{border-left:3px solid #e2e8f0;opacity:0.6;}
    .pt-icon{font-size:1.3rem;flex-shrink:0;}
    .pt-info{display:flex;flex-direction:column;gap:1px;min-width:0;}
    .pt-name{font-size:0.82rem;font-weight:600;color:#334155;white-space:nowrap;
             overflow:hidden;text-overflow:ellipsis;}
    .pt-status{font-size:0.7rem;font-weight:500;}
    .pt-st-ok{color:#16a34a;}
    .pt-st-warn{color:#d97706;}
    .pt-st-fail{color:#dc2626;}
    .pt-st-run{color:#2563eb;}
    .pt-st-wait{color:#94a3b8;}
    .pt-connector{width:100%;height:1px;background:linear-gradient(90deg,#e2e8f0 0%,transparent 100%);
                  margin:4px 0 14px;}
    </style>"""

    def _render_phase(steps: list[dict], phase_label: str, badge_cls: str) -> str:
        if not steps:
            return ""
        cards = []
        for s in steps:
            status = st.session_state.step_status.get(s["idx"], "⏳")
            if status == "✅":
                card_cls, st_cls, st_text = "pt-card-ok", "pt-st-ok", "✓ Completed"
            elif status == "⚠️":
                card_cls, st_cls, st_text = "pt-card-warn", "pt-st-warn", "⚠ Warnings"
            elif status == "❌":
                card_cls, st_cls, st_text = "pt-card-fail", "pt-st-fail", "✗ Failed"
            elif status == "🔄":
                card_cls, st_cls, st_text = "pt-card-run", "pt-st-run", "● Running…"
            else:
                card_cls, st_cls, st_text = "pt-card-wait", "pt-st-wait", "Pending"

            cards.append(
                f'<div class="pt-card {card_cls}">'
                f'  <span class="pt-icon">{s["icon"]}</span>'
                f'  <div class="pt-info">'
                f'    <span class="pt-name">{s["name"]}</span>'
                f'    <span class="pt-status {st_cls}">{st_text}</span>'
                f'  </div>'
                f'</div>'
            )

        return (
            f'<div class="pt-phase">'
            f'  <div class="pt-phase-hdr">'
            f'    <span class="pt-phase-badge {badge_cls}">{phase_label}</span>'
            f'  </div>'
            f'  <div class="pt-grid">{"".join(cards)}</div>'
            f'</div>'
        )

    html = _css
    html += _render_phase(conversion_steps, "Phase 1 — Conversion", "pt-phase-1")
    if validation_steps:
        html += '<div class="pt-connector"></div>'
        html += _render_phase(validation_steps, "Phase 2 — Validation", "pt-phase-2")
    return html


def show_processing_steps():
    """Show processing steps with two-phase progress (Conversion + Validation)"""

    is_opus = st.session_state.get('process_method') == 'claude_opus'

    if is_opus:
        # ── Phase 1: PDF → Text Conversion (steps 0-4) ────────────────────
        conversion_steps = [
            {"name": "Initialize Claude Opus",  "icon": "🤖", "idx": 0},
            {"name": "Detect State",            "icon": "🔍", "idx": 1},
            {"name": "Build Conversion Prompt", "icon": "📋", "idx": 2},
            {"name": "Visual Conversion",       "icon": "👁️", "idx": 3},
            {"name": "Save Output",             "icon": "💾", "idx": 4},
        ]
        # ── Phase 2: Validation Report (steps 5-7) ────────────────────────
        validation_steps = [
            {"name": "PyMuPDF Extraction",  "icon": "📄", "idx": 5},
            {"name": "Claude LLM Clean",    "icon": "🧹", "idx": 6},
            {"name": "GPT Audit",           "icon": "🤖", "idx": 7},
        ]
    else:
        conversion_steps = [
            {"name": "PDF to Text",        "icon": "📄", "idx": 0},
            {"name": "PDF to DOCX",        "icon": "📋", "idx": 1},
            {"name": "DOCX Analysis",      "icon": "🔍", "idx": 2},
            {"name": "Summary Generation", "icon": "📊", "idx": 3},
            {"name": "LLM Verification",   "icon": "🤖", "idx": 4},
        ]
        validation_steps = []

    # Show detected state pill (compact) as soon as it's known
    if is_opus and st.session_state.get('detected_state'):
        _ds = st.session_state.detected_state
        _dsname = US_STATE_NAMES.get(_ds, _ds)
        st.markdown(
            f'<div style="display:inline-block; background:#dcfce7; color:#166534; '
            f'padding:4px 14px; border-radius:20px; font-size:0.85rem; font-weight:600; margin-bottom:10px;">'
            f'🏛️ {_ds} — {_dsname}</div>',
            unsafe_allow_html=True,
        )

    # ── PHASE 1 ────────────────────────────────────────────────────────────
    conv_done = sum(1 for s in conversion_steps if st.session_state.step_status.get(s["idx"], "") == "✅")
    conv_total = len(conversion_steps)
    conv_pct = conv_done / conv_total if conv_total else 0
    conv_complete = (conv_done == conv_total)
    current_step = st.session_state.current_step

    if conv_complete:
        # Conversion finished — styled milestone
        st.markdown(
            '<div style="background:#dcfce7; border-radius:10px; padding:12px 20px; '
            'border-left:4px solid #16a34a; margin-bottom:12px;">'
            '<strong style="color:#15803d;">✅ PDF Conversion Complete</strong>  '
            '<span style="color:#166534; font-size:0.85rem;">  — All conversion steps finished successfully</span>'
            '</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown("##### 📄 PDF Conversion")
        st.progress(conv_pct, text=f"Step {conv_done}/{conv_total}")
        # Current step message
        if current_step < conv_total:
            status = st.session_state.step_status.get(current_step, "⏳")
            info = conversion_steps[current_step]
            if status == "🔄":
                st.info(f"🔄 {info['icon']} {info['name']}…")
            elif status == "❌":
                st.error(f"❌ Failed: {info['icon']} {info['name']}")

    # ── PHASE 2 (Opus only) ────────────────────────────────────────────────
    if is_opus and validation_steps:
        val_done = sum(1 for s in validation_steps if st.session_state.step_status.get(s["idx"], "") in ("✅", "⚠️"))
        val_total = len(validation_steps)
        val_pct = val_done / val_total if val_total else 0
        val_started = any(
            st.session_state.step_status.get(s["idx"], "") in ("🔄", "✅", "⚠️")
            for s in validation_steps
        )
        val_complete = (val_done == val_total)

        if val_complete:
            st.markdown(
                '<div style="background:#dbeafe; border-radius:10px; padding:12px 20px; '
                'border-left:4px solid #2563eb; margin-bottom:12px;">'
                '<strong style="color:#1e40af;">✅ Validation Report Ready</strong>  '
                '<span style="color:#1e40af; font-size:0.85rem;">  — Scroll down to review the audit results</span>'
                '</div>',
                unsafe_allow_html=True,
            )
        elif val_started or conv_complete:
            st.markdown("---")
            st.markdown("##### 🔍 Generating Validation Report")
            st.caption("Validating the Claude converted output against the original PDF — this may take a few minutes…")
            st.progress(val_pct, text=f"Step {val_done}/{val_total}")
            # Current step message
            if current_step >= 5:
                s_idx = current_step
                status = st.session_state.step_status.get(s_idx, "⏳")
                info = next((s for s in validation_steps if s["idx"] == s_idx), None)
                if info:
                    if status == "🔄":
                        st.info(f"🔄 {info['icon']} {info['name']}…")
                    elif status == "❌":
                        st.error(f"❌ Failed: {info['icon']} {info['name']}")

    # ── Detailed step list — modern pipeline tracker ─────────────────────
    all_steps = conversion_steps + validation_steps
    with st.expander("🗂️ Pipeline Steps", expanded=False):
        st.markdown(_build_pipeline_tracker_html(conversion_steps, validation_steps), unsafe_allow_html=True)

def show_results_section():
    """Show results in a clean tabbed layout: Summary, Validation Review, Activity Log"""
    file_name = Path(st.session_state.processed_file_path).name

    # File size (prefer cached bytes for multi-pod safety)
    cached_data = st.session_state.get('processed_file_data')
    if cached_data is not None:
        file_size = len(cached_data)
    else:
        try:
            file_size = os.path.getsize(st.session_state.processed_file_path)
        except Exception:
            file_size = 0

    _has_audit = bool(st.session_state.get('audit_excel_path'))
    _has_accuracy = bool(st.session_state.get('content_accuracy_report'))
    _has_highlighted = bool(st.session_state.get('audit_highlighted_dir'))
    _audit_failed = bool(st.session_state.get('gpt_audit_failed'))

    # ── Build tab list dynamically ─────────────────────────────────────────────
    # Default tab order: PDF ↔ Text Compare first (when available), then Summary,
    # Validation Review, Activity Log. PDF Compare is the most useful immediate
    # view after processing completes, so we surface it as the landing tab.
    _method = st.session_state.get('process_method', '')
    _detected_state = (st.session_state.get('detected_state') or "").upper()
    # States that intentionally skip the validation/audit pipeline even when
    # processed via Visual mode (claude_opus). Keep this in sync with the
    # _skip_validation_states set inside process_with_claude_opus.
    _state_skips_validation = _detected_state in {"CO"}
    # Scanned PDFs skip validation — human review is required instead
    _is_scanned_pdf = st.session_state.get('_pdf_is_scanned', False)
    # NOTE: pymupdf_clean_only and hybrid pipelines now run their own audit
    # (pymupdf_compare_tags) at step 6, so they DO have a Validation Review
    # tab to show. We only skip when the audit was explicitly skipped at the
    # state level, OR when no audit data was produced, OR scanned PDF.
    _skip_validation = _state_skips_validation or _is_scanned_pdf
    _show_pdf_compare = (
        compare_build_viewer_html is not None
        and st.session_state.get('temp_pdf_path')
        and os.path.exists(st.session_state.get('temp_pdf_path', ''))
    )
    tab_labels = []
    if _show_pdf_compare:
        tab_labels.append("📄 PDF ↔ Text Compare")
    tab_labels.append("📊 Summary")
    if st.session_state.get('processing_logs'):
        tab_labels.append("📋 Activity Log")

    tabs = st.tabs(tab_labels)
    _tab_idx = 0

    # ══════════════════════════════════════════════════════════════════════════
    # TAB — PDF ↔ Text Compare (default landing tab when available)
    # ══════════════════════════════════════════════════════════════════════════
    if _show_pdf_compare:
        with tabs[_tab_idx]:
            _pdf_path = st.session_state.temp_pdf_path
            _txt_path = st.session_state.processed_file_path
            try:
                with open(_pdf_path, 'rb') as _pf:
                    _pdf_bytes = _pf.read()

                # ── Pre-check: if bridge component returned a save value on the
                # PREVIOUS rerun, apply it NOW before building the viewer so
                # the viewer shows updated text on this render (no double-render).
                _pending_save = st.session_state.get('pdf_save_bridge_v2')
                if _pending_save and len(_pending_save) > 5:
                    _current_data = st.session_state.get('processed_file_data')
                    _current_text = _current_data.decode('utf-8', errors='replace') if isinstance(_current_data, bytes) else (_current_data or '')
                    if _pending_save != _current_text:
                        st.session_state['processed_file_data'] = _pending_save.encode('utf-8')
                        with open(_txt_path, 'w', encoding='utf-8') as _wf:
                            _wf.write(_pending_save)
                        st.session_state['_compare_already_saved'] = True
                        st.session_state['_show_save_success'] = True
                        logging.info(f"[PDF Compare Save] Saved {len(_pending_save)} chars to {_txt_path}")

                # Read the output text (prefer cached bytes — now includes any just-saved edits)
                _txt_data = st.session_state.get('processed_file_data')
                if _txt_data is None:
                    with open(_txt_path, 'r', encoding='utf-8') as _tf:
                        _clean_text = _tf.read()
                else:
                    _clean_text = _txt_data.decode('utf-8', errors='replace') if isinstance(_txt_data, bytes) else _txt_data
                _clean_lines = _clean_text.splitlines()

                with st.spinner("Extracting words & mapping…"):
                    _words = compare_extract_word_bboxes(_pdf_bytes)
                    _word_map = compare_build_word_level_map(_clean_lines, _words)

                _viewer_html = compare_build_viewer_html(
                    _pdf_bytes, _clean_lines, _word_map,
                    filename=Path(_txt_path).name,
                )
                # ── Iframe height strategy ─────────────────────────
                # The iframe is sized via THREE mechanisms in priority
                # order:
                #   1. CSS `max-height: calc(100vh - 130px)` (HARDEST cap)
                #      — ensures iframe never extends past viewport bottom.
                #   2. Dynamic CSS flex chain `flex: 1 1 auto` inside a
                #      100vh column so the iframe grows to fill available
                #      space (within the max-height cap).
                #   3. Static `height=600` attribute below as fallback for
                #      browsers without :has() support.  Sized for typical
                #      laptop viewports; the CSS max-height clamps it
                #      tighter on shorter screens.
                # `scrolling=True` ensures that if anything overflows the
                # iframe content area, the user can still scroll inside
                # the iframe to reach the bottom.
                st.components.v1.html(_viewer_html, height=1400, scrolling=True)

                # ── Save bridge component (always rendered) ───────────────────
                _save_component_dir = Path(__file__).parent / "pdf_save_component"
                _pdf_save_comp = st.components.v1.declare_component("pdf_save_bridge", path=str(_save_component_dir))
                _pdf_save_comp(key="pdf_save_bridge_v2", default=None)

                # Show save confirmation after rerun
                if st.session_state.pop('_show_save_success', False):
                    st.toast("✅ Changes saved successfully!", icon="💾")
                    st.success("✅ Changes saved! Click **Submit to Keyed** to upload with your edits.")

                # Show persistent save indicator
                if st.session_state.get('_compare_already_saved'):
                    st.caption("💾 _Edits saved — Submit to Keyed will include your changes._")

            except Exception as _cmp_err:
                st.error(f"❌ Could not render PDF ↔ Text Compare viewer: {_cmp_err}")
                logging.error(f"PDF Compare viewer error: {_cmp_err}", exc_info=True)
        _tab_idx += 1

    # ══════════════════════════════════════════════════════════════════════════
    # TAB — Summary + Download
    # ══════════════════════════════════════════════════════════════════════════
    with tabs[_tab_idx]:
        # ── Summary data ───────────────────────────────────────────────────
        _state_label = ""
        if st.session_state.get('detected_state'):
            _s = st.session_state.detected_state
            _sname = US_STATE_NAMES.get(_s, _s)
            _state_label = f"{_s} — {_sname}"

        _conv_time = st.session_state.get('conversion_time') or "—"
        _total_time = st.session_state.processing_time or "—"
        _size_label = format_file_size(file_size)

        # Compute validation-only time from raw seconds
        _val_time = "—"
        _total_secs = st.session_state.get('total_elapsed_secs', 0)
        _conv_secs = st.session_state.get('conversion_elapsed_secs', 0)
        if _total_secs and _conv_secs and _has_audit:
            _val_secs = max(0, _total_secs - _conv_secs)
            _vm, _vs = int(_val_secs // 60), int(_val_secs % 60)
            _val_time = f"{_vm}m {_vs}s" if _vm > 0 else f"{_vs}s"

        # Build state pill
        _state_pill = (
            f'<span style="background:#e0e7ff; color:#3730a3; padding:3px 12px; '
            f'border-radius:16px; font-size:0.82rem; font-weight:600;">'
            f'🏛️ {_state_label}</span>'
        ) if _state_label else ""

        # Build time chips
        _chip = (
            'display:inline-flex; align-items:center; gap:6px; '
            'padding:8px 16px; border-radius:10px; font-size:0.85rem; font-weight:600;'
        )
        _conv_chip = (
            f'<div style="{_chip} background:#f0fdf4; color:#166534; border:1px solid #bbf7d0;">'
            f'<span style="font-size:1rem;">📝</span> Conversion '
            f'<span style="font-weight:800;">{_conv_time}</span></div>'
        )
        _val_chip = (
            f'<div style="{_chip} background:#eff6ff; color:#1e40af; border:1px solid #bfdbfe;">'
            f'<span style="font-size:1rem;">🔍</span> Validation '
            f'<span style="font-weight:800;">{_val_time}</span></div>'
        ) if _has_audit else ""
        _size_chip = (
            f'<div style="{_chip} background:#f5f3ff; color:#5b21b6; border:1px solid #ddd6fe;">'
            f'<span style="font-size:1rem;">📄</span> Output '
            f'<span style="font-weight:800;">{_size_label}</span></div>'
        )

        # Content accuracy chip (precleaned vs final output — word-level)
        _content_acc_chip = ""
        _content_rpt = st.session_state.get('content_accuracy_detail')
        if _content_rpt:
            _content_val = _content_rpt.get('accuracy_pct', 100.0)
            _content_words = _content_rpt.get('matching_words', 0)
            _content_max = max(_content_rpt.get('precleaned_words', 0), _content_rpt.get('output_words', 0))
            _content_issues = _content_rpt.get('content_issue_count', 0)
            if _content_val >= 99:
                _ca_bg, _ca_fg, _ca_bdr = '#f0fdf4', '#166534', '#bbf7d0'
            elif _content_val >= 95:
                _ca_bg, _ca_fg, _ca_bdr = '#fefce8', '#854d0e', '#fde68a'
            else:
                _ca_bg, _ca_fg, _ca_bdr = '#fef2f2', '#991b1b', '#fecaca'
            _ca_detail = f" ({_content_words:,}/{_content_max:,} words"
            if _content_issues:
                _ca_detail += f", {_content_issues} issue{'s' if _content_issues != 1 else ''}"
            _ca_detail += ")"
            _content_acc_chip = (
                f'<div style="{_chip} background:{_ca_bg}; color:{_ca_fg}; border:1px solid {_ca_bdr};">'
                f'<span style="font-size:1rem;">📊</span> Content Accuracy '
                f'<span style="font-weight:800;">{_content_val:.2f}%</span>'
                f'<span style="font-size:0.75rem; font-weight:400; margin-left:4px;">{_ca_detail}</span></div>'
            )

        # Tag accuracy chip (from audit counts)
        _tag_acc_chip = ""
        _cached_audit = st.session_state.get('audit_counts')
        if isinstance(_cached_audit, dict):
            _ta_total = int(_cached_audit.get('total_checklist') or 0)
            _ta_pass = int(_cached_audit.get('passes') or 0)
            _ta_err = int(_cached_audit.get('errors') or 0)
            _ta_warn = int(_cached_audit.get('warnings') or 0)
            if _ta_total == 0:
                # No formatted spans detected — nothing to tag
                _ta_pct = 100.0
                _ta_bg, _ta_fg, _ta_bdr = '#f0fdf4', '#166534', '#bbf7d0'
                _ta_detail = " (No formatting modifications detected — no tagging required)"
            else:
                _ta_pct = (_ta_pass / _ta_total * 100.0)
                if _ta_pct >= 99:
                    _ta_bg, _ta_fg, _ta_bdr = '#f0fdf4', '#166534', '#bbf7d0'
                elif _ta_pct >= 95:
                    _ta_bg, _ta_fg, _ta_bdr = '#fefce8', '#854d0e', '#fde68a'
                else:
                    _ta_bg, _ta_fg, _ta_bdr = '#fef2f2', '#991b1b', '#fecaca'
                _ta_detail = f" ({_ta_pass}/{_ta_total}"
                if _ta_err:
                    _ta_detail += f", {_ta_err} error{'s' if _ta_err != 1 else ''}"
                if _ta_warn:
                    _ta_detail += f", {_ta_warn} warning{'s' if _ta_warn != 1 else ''}"
                _ta_detail += ")"
            _tag_acc_chip = (
                f'<div style="{_chip} background:{_ta_bg}; color:{_ta_fg}; border:1px solid {_ta_bdr};">'
                f'<span style="font-size:1rem;">🏷️</span> Tag Accuracy '
                f'<span style="font-weight:800;">{_ta_pct:.1f}%</span>'
                f'<span style="font-size:0.75rem; font-weight:400; margin-left:4px;">{_ta_detail}</span></div>'
            )

        _summary_html = (
            '<div style="background:#ffffff; border-radius:14px; padding:24px 28px; margin:8px 0 20px;'
            ' border:1px solid #e2e8f0; box-shadow:0 2px 12px rgba(0,0,0,.05);">'
            '<div style="display:flex; align-items:center; gap:12px; margin-bottom:16px; flex-wrap:wrap;">'
            '<span style="font-size:1.5rem;">✅</span>'
            '<span style="font-size:1.2rem; font-weight:700; color:#15803d;">Processing Complete</span>'
            f'{_state_pill}'
            '</div>'
            '<div style="display:flex; gap:10px; flex-wrap:wrap; margin-bottom:14px;">'
            f'{_conv_chip}{_val_chip}{_size_chip}{_content_acc_chip}{_tag_acc_chip}'
            '</div>'
            '<div style="background:#f8fafc; border-radius:8px; padding:8px 16px;'
            ' display:flex; align-items:center; justify-content:space-between;'
            ' border:1px solid #e2e8f0;">'
            '<span style="font-size:0.84rem; color:#475569;">⏱️ Total processing time</span>'
            f'<span style="font-size:1rem; font-weight:800; color:#0f172a;">{_total_time}</span>'
            '</div>'
            '<p style="margin:12px 0 0; font-size:0.82rem; color:#64748b;">'
            'Your PDF has been converted to tagged legislative text with Claude Opus 4.6. '
            'Review the chips above for accuracy metrics.'
            '</p></div>'
        )
        st.markdown(_summary_html, unsafe_allow_html=True)

        # ── Pages with images warning: flag for human review ───────────
        _pdf_analysis = st.session_state.get('_pdf_analysis')
        if _pdf_analysis:
            _page_details = _pdf_analysis.get('details', [])
            _img_pages = [str(pd['page']) for pd in _page_details if pd.get('has_images')]
            if _img_pages:
                if _pdf_analysis.get('type') == 'scanned':
                    st.warning(
                        "📷 **Scanned PDF Detected** — This document is a scanned "
                        "(image-based) PDF. Text was extracted using visual conversion "
                        "which may introduce errors. "
                        "**Full human review is required** to ensure accuracy."
                    )
                else:
                    _pages_str = ', '.join(_img_pages)
                    _n = len(_img_pages)
                    st.warning(
                        f"📷 **Hybrid PDF Detected** — This document contains "
                        f"embedded images on **{_n} page{'s' if _n != 1 else ''}** "
                        f"(page{'s' if _n != 1 else ''} {_pages_str}). "
                        f"Text extraction may be incomplete on those pages. "
                        f"**Please review {'these pages' if _n > 1 else 'this page'}** "
                        f"to ensure no content was missed."
                    )

        # ── Tag Issues (compact, only when tag accuracy < 100%) ───────────
        if isinstance(_cached_audit, dict):
            _ta_err_count = int(_cached_audit.get('errors') or 0)
            _ta_warn_count = int(_cached_audit.get('warnings') or 0)
            if _ta_err_count > 0 or _ta_warn_count > 0:
                _issue_label = []
                if _ta_err_count:
                    _issue_label.append(f"{_ta_err_count} error{'s' if _ta_err_count != 1 else ''}")
                if _ta_warn_count:
                    _issue_label.append(f"{_ta_warn_count} warning{'s' if _ta_warn_count != 1 else ''}")
                with st.expander(f"🏷️ Tag Issues ({', '.join(_issue_label)})", expanded=False):
                    # Try to load from the audit Excel
                    _excel_path = st.session_state.get('audit_excel_path')
                    if _excel_path and os.path.exists(_excel_path):
                        try:
                            import openpyxl as _xl
                            _wb = _xl.load_workbook(str(_excel_path), read_only=True, data_only=True)
                            _ws = _wb["Checklist Audit"]
                            _rows = list(_ws.iter_rows(min_row=4, values_only=True))
                            _wb.close()
                            if _rows:
                                _hdrs = [str(h).strip() if h else f"col{i}" for i, h in enumerate(_rows[0])]
                                _all_items = [dict(zip(_hdrs, r)) for r in _rows[1:] if r[0] is not None]
                                _issue_items = [
                                    r for r in _all_items
                                    if (str(r.get("Verdict", "")).startswith("❌")
                                        or str(r.get("Verdict", "")).startswith("⚠"))
                                    and "dash normali" not in str(r.get("Notes", "") or "").lower()
                                ]
                                if _issue_items:
                                    _tbl_rows = []
                                    for _it in _issue_items[:30]:  # cap at 30
                                        _verdict = str(_it.get("Verdict", ""))
                                        _status = "❌ Error" if _verdict.startswith("❌") else "⚠️ Warning"
                                        _page = str(_it.get("Page", "") or "")
                                        _line = str(_it.get("Line", "") or "")
                                        _text = str(_it.get("PyMuPDF Text", "") or _it.get("Clean Text", "") or "")[:80]
                                        _note = str(_it.get("Notes", "") or "")[:120]
                                        _fix = str(_it.get("Suggested Fix", "") or "")[:120]
                                        _tbl_rows.append({
                                            "Status": _status,
                                            "Page": _page,
                                            "Line": _line,
                                            "Text": _text,
                                            "Issue": _note,
                                            "Suggested Fix": _fix,
                                        })
                                    import pandas as _pd
                                    st.dataframe(
                                        _pd.DataFrame(_tbl_rows),
                                        use_container_width=True,
                                        hide_index=True,
                                    )
                                    if len(_issue_items) > 30:
                                        st.caption(f"Showing first 30 of {len(_issue_items)} issues. Download the audit Excel for the full list.")
                        except Exception as _tag_ex:
                            st.caption(f"Could not load tag issues: {_tag_ex}")
                    else:
                        st.caption("Audit Excel not available — re-run processing to generate.")

        # ── Content Issues (compact, only when content accuracy has issues) ─
        # Check both report sources: content_accuracy_detail (from _run_content_accuracy_check)
        # and content_accuracy_report (from _run_text_accuracy_check / compare_files)
        _content_rpt = st.session_state.get('content_accuracy_detail') or st.session_state.get('content_accuracy_report')
        if _content_rpt:
            # Support both compare_files (real_diffs) and content_accuracy_checker (content_issues) formats
            _ci_list = _content_rpt.get('content_issues') or _content_rpt.get('real_diffs') or []
            _ci_count = len(_ci_list)
            if _ci_count > 0:
                with st.expander(f"📊 Content Issues ({_ci_count} difference{'s' if _ci_count != 1 else ''})", expanded=False):
                    # Build word-index-to-line-number map from the output text
                    _out_text_for_lines = None
                    _out_path = st.session_state.get('processed_file_path')
                    if _out_path and os.path.exists(_out_path):
                        try:
                            with open(_out_path, 'r', encoding='utf-8') as _of:
                                _out_text_for_lines = _of.read()
                        except Exception:
                            pass
                    _word_to_line = {}
                    if _out_text_for_lines:
                        _w_idx = 0
                        for _ln_no, _ln_text in enumerate(_out_text_for_lines.splitlines(), start=1):
                            _ln_words = _ln_text.split()
                            for _ in _ln_words:
                                _word_to_line[_w_idx] = _ln_no
                                _w_idx += 1

                    _ci_rows = []
                    for _ci in _ci_list[:30]:
                        _ci_type = str(_ci.get("type", "")).capitalize()
                        # Support both field naming conventions
                        _ci_pre = str(_ci.get("precleaned") or _ci.get("pymupdf_text") or _ci.get("missing_text") or "—")[:80]
                        _ci_out = str(_ci.get("output") or _ci.get("converted_text") or _ci.get("extra_text") or "—")[:80]
                        _ci_cat = str(_ci.get("category") or _ci.get("sub_type") or _ci.get("allowed_category") or "").replace("_", " ").title()
                        _ci_line = ""
                        _ci_word_start = _ci.get("output_word_start") or _ci.get("j1")
                        if _ci_word_start is not None and _word_to_line:
                            _ci_line = str(_word_to_line.get(_ci_word_start, ""))
                        _ci_rows.append({
                            "Line": _ci_line,
                            "Type": _ci_type,
                            "Expected (PDF)": _ci_pre,
                            "Output": _ci_out,
                            "Category": _ci_cat,
                        })
                    import pandas as _pd
                    st.dataframe(
                        _pd.DataFrame(_ci_rows),
                        use_container_width=True,
                        hide_index=True,
                    )
                    if _ci_count > 30:
                        st.caption(f"Showing first 30 of {_ci_count} issues. Download the accuracy JSON for the full list.")

        # ── Submit to Keyed (SFTP Upload) ─────────────────────────────────────
        st.markdown("---")
        _submit_col1, _submit_col2 = st.columns([3, 1])
        with _submit_col1:
            st.markdown(
                '<p style="font-size:0.88rem; color:#475569; margin:4px 0;">'
                '📤 Submit approved file to <code>/ftp/keyed</code> for downstream processing.</p>',
                unsafe_allow_html=True,
            )
        with _submit_col2:
            if st.button("🚀 Submit to Keyed", type="primary", use_container_width=True, key="submit_to_keyed_btn"):
                _file_to_submit = st.session_state.processed_file_path
                if _file_to_submit and os.path.exists(_file_to_submit):
                    try:
                        _was_edited = st.session_state.get('_compare_already_saved', False)
                        _submit_to_keyed_sftp(_file_to_submit)
                        _msg = "Submitted with edits" if _was_edited else "Submitted"
                        st.toast(f"{_msg} → /ftp/keyed", icon="✅")
                        logging.info(f"[Submit to Keyed] Uploaded {Path(_file_to_submit).name} (edited={_was_edited})")
                        st.markdown(
                            f'<div style="background:#f0fdf4; border:1px solid #bbf7d0; border-radius:10px; '
                            f'padding:12px 20px; display:flex; align-items:center; gap:12px; margin-bottom:8px; '
                            f'box-shadow:0 1px 4px rgba(0,0,0,.04);">'
                            f'<span style="font-size:1.2rem;">✅</span>'
                            f'<span style="font-weight:600; color:#166534;">{_msg}</span>'
                            f'<span style="font-size:0.88rem; color:#1e293b; word-break:break-all;">{Path(_file_to_submit).name}</span>'
                            f'<span style="margin-left:auto; color:#64748b; font-size:0.85rem;">→ /ftp/keyed</span>'
                            f'</div>',
                            unsafe_allow_html=True,
                        )
                    except Exception as _upload_err:
                        st.error(f"❌ Upload failed: {_upload_err}")
                else:
                    st.warning("No processed file available to submit.")

    _tab_idx += 1

    # ══════════════════════════════════════════════════════════════════════════
    # TAB — Activity Log
    # ══════════════════════════════════════════════════════════════════════════
    if st.session_state.get('processing_logs'):
        with tabs[_tab_idx]:
            st.markdown(_build_activity_feed_html(st.session_state.processing_logs), unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
#  BATCH PROCESSING — Serial (one-by-one) multi-file workflow
# ══════════════════════════════════════════════════════════════════════════════

def _swap_batch_vri_state(from_idx: int | None, to_idx: int | None):
    """Clear stale vri_ widget keys when switching batch files.

    The actual per-file state (fixed_ids, ignored_ids, etc.) is saved and
    restored inside ``render_inline_validation_review`` using the file path
    as key.  This helper only needs to clear widget keys so stale buttons/
    radio values don't leak between files.
    """
    # Clear ALL vri_ widget keys so no stale values leak between files.
    _stale_prefixes = (
        "vri_edit_", "vri_pending_apply_", "vri_apply_",
        "vri_confirm_", "vri_cancel_", "vri_copy_",
        "vri_ignore_nochange_", "vri_ignore_", "vri_unignore_",
        "vri_undo_", "vri_search_", "vri_pending_search_",
    )
    # Static widget keys that must also be reset between files
    _static_widget_keys = {
        "vri_pg_slider", "vri_pg_prev", "vri_pg_next",
        "vri_jump_to", "vri_prev_item_btn", "vri_next_item_btn",
        "vri_prev_err", "vri_next_err", "vri_twocol",
        "vri_enter_edit", "vri_exit_edit", "vri_save_edit",
        "vri_cancel_edit", "vri_edit_text_buffer",
        "vri_table_click", "_vri_last_table_click",
    }
    stale_keys = [k for k in list(st.session_state.keys())
                  if any(k.startswith(p) for p in _stale_prefixes)
                  or k in _static_widget_keys]
    for k in stale_keys:
        del st.session_state[k]


# ══════════════════════════════════════════════════════════════════════════════
#  Background Batch Worker — processes files in a daemon thread
# ══════════════════════════════════════════════════════════════════════════════

class BatchWorker:
    """Thread-safe batch processor that runs in the background.

    Results are written to an internal list protected by a lock.
    The Streamlit UI polls ``get_completed_results()`` on each rerun
    (driven by st.fragment auto-refresh) to sync finished files into
    ``st.session_state.batch_results`` without blocking the UI.
    """

    def __init__(self, pdf_paths: list[str], file_names: list[str],
                 session_id: str, session_mgr, session_folders: dict):
        self._pdf_paths = list(pdf_paths)
        self._file_names = list(file_names)
        self._session_id = session_id
        self._session_mgr = session_mgr
        self._session_folders = session_folders
        self._lock = threading.Lock()
        self._results: list[dict] = []          # completed file results
        self._current_idx: int = 0              # file currently being processed
        self._current_file: str = ""            # name of current file
        self._current_logs: list[dict] = []     # live logs for current file
        self._done: bool = False
        self._thread: threading.Thread | None = None

    # ── Public API (called from main Streamlit thread) ──────────────
    @property
    def total(self) -> int:
        return len(self._pdf_paths)

    def start(self):
        """Launch the background processing thread."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def is_done(self) -> bool:
        with self._lock:
            return self._done

    def get_completed_results(self) -> list[dict]:
        """Return a snapshot of all completed file results (thread-safe copy)."""
        with self._lock:
            return list(self._results)

    def get_progress(self) -> dict:
        """Return a snapshot of current progress."""
        with self._lock:
            return {
                "current_idx": self._current_idx,
                "current_file": self._current_file,
                "n_completed": len(self._results),
                "total": self.total,
                "done": self._done,
                "current_logs": list(self._current_logs),
            }

    # ── Background thread entry point ───────────────────────────────
    def _run(self):
        """Process every file serially (runs in daemon thread)."""
        import importlib.util

        for idx, (pdf_path, file_name) in enumerate(
            zip(self._pdf_paths, self._file_names)
        ):
            with self._lock:
                self._current_idx = idx
                self._current_file = file_name
                self._current_logs = []

            try:
                file_result = self._process_one(pdf_path, file_name, idx)
            except Exception as e:
                logging.error(f"BatchWorker error on {file_name}: {e}", exc_info=True)

                # Classify the error for user-friendly messaging
                _err_str = str(e).lower()
                _api_network_keywords = (
                    "timed out", "timeout", "overloaded", "connection",
                    "disconnected", "429", "500", "502", "503", "504", "529",
                    "rate limit", "empty text", "api error", "request failed",
                )
                if any(kw in _err_str for kw in _api_network_keywords):
                    _err_category = "api_network"
                    _err_reason = (
                        "API/Network interruption — the Claude API timed out or was "
                        "temporarily unavailable. This is not a code issue; the file "
                        "can be retried."
                    )
                else:
                    _err_category = "processing"
                    _err_reason = f"Processing error: {e}"

                file_result = {
                    "file_name": file_name,
                    "pdf_path": pdf_path,
                    "file_size": 0,
                    "output_path": None,
                    "status": "failed",
                    "error_category": _err_category,
                    "error_reason": _err_reason,
                    "detected_state": "",
                    "processing_time": "—",
                    "conversion_time": "—",
                    "total_elapsed_secs": 0,
                    "conversion_elapsed_secs": 0,
                    "audit_excel_path": None,
                    "audit_highlighted_dir": None,
                    "audit_json_report": None,
                    "content_accuracy_report": None,
                    "processing_logs": [{"time": datetime.now().strftime("%H:%M:%S"),
                                         "message": f"❌ {e}", "level": "error", "step": 0}],
                    "output_data": None,
                }

            with self._lock:
                self._results.append(file_result)

        with self._lock:
            self._done = True

    # ── Single-file processing (no st.session_state access) ─────────
    def _process_one(self, pdf_path: str, file_name: str, idx: int) -> dict:
        """Process one PDF entirely without touching Streamlit state."""
        import importlib.util

        start_time = time.time()
        session_id = self._session_id
        session_mgr = self._session_mgr
        session_folders = self._session_folders
        logs: list[dict] = []

        def add_log(message, level="info"):
            entry = {"time": datetime.now().strftime("%H:%M:%S"),
                     "message": message, "level": level,
                     "step": 0}
            logs.append(entry)
            with self._lock:
                self._current_logs = list(logs)
            logging.info(message)

        add_log(f"Starting Claude Opus 4.6 processing for {file_name}...")

        # ── Load Opus module ─────────────────────────────────────────
        opus_module_path = os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         '..', 'Opus4.6', 'Claude_Opus_4.6_Test.py')
        )
        spec = importlib.util.spec_from_file_location("claude_opus_converter", opus_module_path)
        opus = importlib.util.module_from_spec(spec)
        _orig_stdout, _orig_stderr = sys.stdout, sys.stderr
        spec.loader.exec_module(opus)
        sys.stdout, sys.stderr = _orig_stdout, _orig_stderr

        # Step 1: Client
        add_log("🤖 Initializing Claude Opus 4.6 client...")
        client = opus.get_anthropic_client()
        if not client:
            raise RuntimeError("Failed to initialize Claude Opus client")
        add_log("✅ Claude Opus client initialized")

        # Step 2: Detect state
        add_log("🔍 Detecting state from filename...")
        detected_state = detect_state_from_numerical_id(pdf_path) if detect_state_from_numerical_id else None
        if detected_state:
            _state_name = US_STATE_NAMES.get(detected_state, detected_state)
            add_log(f"✅ State detected from filename numerical ID: {detected_state} — {_state_name}")
        else:
            detected_state = opus.detect_state_from_filename(pdf_path)
            if detected_state:
                _state_name = US_STATE_NAMES.get(detected_state, detected_state)
                add_log(f"✅ State detected from filename pattern: {detected_state} — {_state_name}")
            else:
                add_log("⚠️ Could not detect state — using default prompt", "warning")

        # Step 3: Build prompt
        add_log("📋 Building state-specific conversion prompt...")
        state_config = opus.load_state_template(detected_state) if detected_state else None
        prompt = opus.get_conversion_prompt(state_config)
        add_log(f"✅ Conversion prompt built ({len(prompt):,} chars)")

        # Step 4: Visual conversion
        add_log("👁️ Running Claude Opus 4.6 visual conversion (this may take a few minutes)...")
        add_log("⏳ Sending PDF pages to Claude, waiting for output response...")

        # Heartbeat: emit a log every 15s so the user knows it's still running
        _heartbeat_stop = threading.Event()
        _conv_start = time.time()

        def _heartbeat():
            while not _heartbeat_stop.is_set():
                _heartbeat_stop.wait(15)
                if _heartbeat_stop.is_set():
                    break
                _elapsed = int(time.time() - _conv_start)
                _m, _s = divmod(_elapsed, 60)
                add_log(f"⏳ Still waiting for output response from Claude… ({_m}m {_s}s elapsed)")

        _hb_thread = threading.Thread(target=_heartbeat, daemon=True)
        _hb_thread.start()

        try:
            result_data = opus.convert_pdf_to_text(client, pdf_path, prompt)
        finally:
            _heartbeat_stop.set()
            _hb_thread.join(timeout=2)

        if isinstance(result_data, tuple):
            converted_text = result_data[0]
        else:
            converted_text = result_data
        if not converted_text:
            raise RuntimeError("Visual conversion returned empty text")
        add_log(f"✅ Visual conversion complete: {len(converted_text):,} characters")

        # Step 4b: Deterministic cleanup + Strip protected clauses
        try:
            converted_text = opus.deterministic_cleanup(converted_text, state_config=state_config, state_code=detected_state)
            add_log("🔧 Deterministic cleanup complete (markdown→tags, malformed tags, etc.)")
        except Exception:
            pass
        if state_config:
            # First: deterministic post-process from netscan_state_template (no_tag_clauses)
            try:
                converted_text = opus.post_process_output(converted_text, state_config)
                add_log("🔧 Post-processing: stripped tags from protected clauses (if any)")
            except Exception:
                pass
            # Second: strip protected clauses from protected_clauses.py (hardcoded list)
            try:
                from protected_clauses import strip_protected_tags as _strip
                if _strip and detected_state:
                    _before_strip = converted_text
                    converted_text = _strip(converted_text, detected_state)
                    if converted_text != _before_strip:
                        add_log(f"🛡️ Stripped tags from protected clauses for {detected_state}")
            except Exception:
                pass

        # Step 5: Save output
        conversion_elapsed = time.time() - start_time
        _cm, _cs = int(conversion_elapsed // 60), int(conversion_elapsed % 60)
        conversion_time_str = f"{_cm}m {_cs}s" if _cm > 0 else f"{_cs}s"
        add_log(f"🏁 PDF Conversion complete in {conversion_time_str}")
        add_log("💾 Saving converted output...")

        output_filename = Path(pdf_path).stem + ".txt"
        output_path = os.path.join(session_folders['output_folder'], output_filename)
        os.makedirs(session_folders['output_folder'], exist_ok=True)
        if not opus.save_text_file(converted_text, output_path):
            raise RuntimeError(f"Failed to save output to {output_path}")
        add_log(f"✅ Output saved — now generating validation report…")

        # Step 6: PyMuPDF extraction
        add_log("📄 Running PyMuPDF extraction...")
        audit_excel_path = None
        audit_highlighted_dir = None
        audit_json_report = None
        content_accuracy_report = None
        gpt_audit_failed = False
        pymupdf_text = None
        pymupdf_report = None
        highlight_dir = None

        try:
            from pymupdf_extractor import (
                extract_pdf_json_report,
                extract_pdf_with_pymupdf,
                highlight_pdf_formatting,
                clean_extracted_text,
                consolidate_adjacent_tags,
                merge_cross_page_break_tags,
                split_ar_subsection_welds,
            )

            # Determine state-specific marking flags
            _mark_flags = {}
            if state_config:
                add_cfg = state_config.get("add_markup", {})
                del_cfg = state_config.get("delete_markup", {})
                _mark_flags["mark_underline"] = add_cfg.get("underline", True)
                _mark_flags["mark_italic"] = add_cfg.get("italic", False) or del_cfg.get("italic", False) or bool(add_cfg.get("italic_section_only"))
                _mark_flags["mark_bold"] = add_cfg.get("bold", False)
                _mark_flags["mark_strikethrough"] = del_cfg.get("strike_out", True)
                _mark_flags["mark_brackets"] = add_cfg.get("brackets", False) or del_cfg.get("brackets", False)

            # 6a: JSON report
            pymupdf_report = extract_pdf_json_report(pdf_path, state_code=detected_state, **_mark_flags)
            n_spans = len(pymupdf_report.get("formatted_spans", []))
            add_log(f"📊 PyMuPDF JSON report: {n_spans} formatted spans detected")

            # Save JSON report alongside output
            pymupdf_json_path = output_path.replace(".txt", "_pymupdf_report.json")
            import json as _json
            with open(pymupdf_json_path, "w", encoding="utf-8") as jf:
                _json.dump(pymupdf_report, jf, indent=2, ensure_ascii=False)
            add_log(f"✅ JSON report saved")

            # 6b: Tagged text
            pymupdf_text = extract_pdf_with_pymupdf(pdf_path, join_pages=True, state_code=detected_state, **_mark_flags)
            pymupdf_txt_path = output_path.replace(".txt", "_pymupdf.txt")
            with open(pymupdf_txt_path, "w", encoding="utf-8") as tf:
                tf.write(pymupdf_text)
            add_log(f"✅ PyMuPDF tagged text saved: {len(pymupdf_text):,} chars")

            # 6b.1: Filter italic spans by section scope (e.g. Indiana PRINTING CODE only)
            pymupdf_report, pymupdf_text = _filter_italic_by_section(pymupdf_report, pymupdf_text, state_config)
            _n_after = len(pymupdf_report.get("formatted_spans", [])) if pymupdf_report else 0
            if _n_after != n_spans:
                add_log(f"🔍 Italic section filter: {n_spans} → {_n_after} spans (removed {n_spans - _n_after} out-of-scope italic spans)")

            # 6c: Highlighted page images
            highlight_dir = Path(output_path).parent / f"{Path(pdf_path).stem}_highlighted"
            hi_imgs = highlight_pdf_formatting(pdf_path, report=pymupdf_report,
                                               output_dir=highlight_dir, **_mark_flags)
            add_log(f"✅ {len(hi_imgs)} highlighted page images saved")

            audit_highlighted_dir = str(highlight_dir)
            audit_json_report = pymupdf_report
        except Exception as pmu_err:
            add_log(f"⚠️ PyMuPDF extraction error: {pmu_err}", "warning")
            logging.error(f"BatchWorker PyMuPDF error: {pmu_err}", exc_info=True)

        # Step 7: Claude LLM Clean
        pymupdf_clean_text = None
        if pymupdf_text:
            try:
                add_log("🧹 Cleaning PyMuPDF text with Claude — removing headers/footers/line numbers...")
                add_log("⏳ Sending text to Claude for cleanup, please wait...")
                pymupdf_clean_text = clean_extracted_text(
                    pymupdf_text,
                    state_code=detected_state,
                    pdf_path=pdf_path,
                    anthropic_client=client,
                    mark_brackets=_mark_flags.get("mark_brackets", False),
                )
                pymupdf_clean_text = consolidate_adjacent_tags(
                    pymupdf_clean_text, state_code=detected_state
                )
                pymupdf_clean_text = opus.remove_nested_tags(pymupdf_clean_text)
                pymupdf_clean_text = opus.merge_split_tags(pymupdf_clean_text)
                if detected_state and detected_state.upper() == "AR":
                    pymupdf_clean_text = split_ar_subsection_welds(pymupdf_clean_text)
                # Merge same-paragraph tags split across a page break.
                pymupdf_clean_text = merge_cross_page_break_tags(pymupdf_clean_text)
                pymupdf_clean_text = opus.split_tagged_subsections(pymupdf_clean_text)

                # Strip tags from protected clauses
                try:
                    from protected_clauses import strip_protected_tags as _strip_prot
                    if _strip_prot and detected_state:
                        pymupdf_clean_text = _strip_prot(pymupdf_clean_text, detected_state)
                except Exception:
                    pass

                pymupdf_clean_path = output_path.replace(".txt", "_pymupdf_clean.txt")
                with open(pymupdf_clean_path, "w", encoding="utf-8") as cf:
                    cf.write(pymupdf_clean_text)
                add_log(f"✅ Clean text saved: {len(pymupdf_text):,} → {len(pymupdf_clean_text):,} chars")

                # Save LLM diff report
                _diff_report = getattr(clean_extracted_text, '_last_diff_report', None)
                if _diff_report:
                    diff_path = pymupdf_clean_path.replace("_pymupdf_clean.txt", "_clean_diff.txt")
                    with open(diff_path, "w", encoding="utf-8") as df:
                        df.write(_diff_report)
                    add_log(f"📋 LLM diff report saved")

                _preclean_guard = getattr(clean_extracted_text, '_last_preclean_guard', None)
                if _preclean_guard:
                    guard_path = pymupdf_clean_path.replace("_pymupdf_clean.txt", "_preclean_guard.txt")
                    with open(guard_path, "w", encoding="utf-8") as gf:
                        gf.write(_json.dumps(_preclean_guard, indent=2, ensure_ascii=False))
                    if _preclean_guard.get("ok"):
                        add_log("✅ Pre-clean guard passed (only line-number artifacts removed)")
                    else:
                        add_log(
                            f"⚠️ Pre-clean guard failed: {_preclean_guard.get('unexpected_removed_count', 0)} unexpected removals",
                            "warning",
                        )
                    add_log(f"📋 Pre-clean guard report saved")

                # Rebuild report context using cleaned text for better anchor matching
                if pymupdf_report:
                    try:
                        from pymupdf_extractor import rebuild_report_context
                        pymupdf_report = rebuild_report_context(pymupdf_report, pymupdf_clean_text)
                        with open(pymupdf_json_path, "w", encoding="utf-8") as jf:
                            _json.dump(pymupdf_report, jf, indent=2, ensure_ascii=False)
                        add_log("🔄 Report context rebuilt from cleaned text")
                        audit_json_report = pymupdf_report
                    except Exception as _ctx_err:
                        logging.warning(f"Context rebuild failed (non-critical): {_ctx_err}")

                # Restore special symbols dropped by Claude vision
                converted_before = converted_text
                converted_text = opus.restore_special_symbols(converted_text, pymupdf_clean_text)
                if converted_text != converted_before:
                    add_log("🔧 Restored special symbols (§ etc.) from PyMuPDF reference")
                    with open(output_path, "w", encoding="utf-8") as f:
                        f.write(converted_text)
            except Exception as e:
                add_log(f"⚠️ LLM clean error: {e}", "warning")
                pymupdf_clean_text = pymupdf_text
        else:
            add_log("⏭️ Skipping LLM clean (no PyMuPDF text)")

        # Content accuracy check
        if pymupdf_clean_text and converted_text:
            try:
                _opus46 = os.path.normpath(os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), '..', 'Opus4.6'))
                if _opus46 not in sys.path:
                    sys.path.insert(0, _opus46)
                from compare_files import compare_texts as _cmp
                if _cmp:
                    content_accuracy_report = _cmp(pymupdf_clean_text, converted_text)
                    _acc = content_accuracy_report.get('accuracy', 0)
                    _diffs = content_accuracy_report.get('real_diff_count', 0)
                    add_log(f"📊 Text accuracy: {_acc:.1f}% ({_diffs} diffs)")
            except Exception as e:
                add_log(f"⚠️ Content accuracy check error: {e}", "warning")

        # Step 8: GPT Audit — DISABLED
        # Disabled to save cost/time; extraction_validator.py passive watchdog
        # still runs automatically after conversion as the safety net.
        add_log("⏭️ GPT audit skipped (disabled)")
        gpt_audit_failed = False
        # if pymupdf_clean_text and highlight_dir and highlight_dir.exists():
        #     try:
        #         add_log("🤖 Running GPT audit comparison — this may take 1–2 minutes...")
        #         _opus46 = os.path.normpath(os.path.join(
        #             os.path.dirname(os.path.abspath(__file__)), '..', 'Opus4.6'))
        #         if _opus46 not in sys.path:
        #             sys.path.insert(0, _opus46)
        #         from gpt_compare import gpt_compare_tags
        #
        #         # Load highlighted images as base64
        #         import base64 as _b64
        #         hi_images_b64 = []
        #         for img_file in sorted(highlight_dir.glob("page_*.png")):
        #             hi_images_b64.append(_b64.b64encode(img_file.read_bytes()).decode())
        #         add_log(f"📷 Loaded {len(hi_images_b64)} highlighted page images for GPT")
        #
        #         # Build state rules
        #         sr_parts = []
        #         if state_config:
        #             sr_parts.append(f"State: {state_config.get('state_name', detected_state or 'unknown')}")
        #             a_cfg = state_config.get("add_markup", {})
        #             d_cfg = state_config.get("delete_markup", {})
        #             sr_parts.append(f"Underline={a_cfg.get('underline', False)}")
        #             sr_parts.append(f"Strikethrough={d_cfg.get('strike_out', False)}")
        #             sr_parts.append(f"Italic={a_cfg.get('italic', False)}")
        #             sr_parts.append(f"Bold={a_cfg.get('bold', False)}")
        #             # Add strikethrough priority rule when both add and delete formats are active
        #             if d_cfg.get('strike_out', False) and (a_cfg.get('underline', False) or a_cfg.get('italic', False) or a_cfg.get('bold', False)):
        #                 sr_parts.append("PRIORITY: If text has strikethrough AND any other formatting (underline/italic/bold), strikethrough ALWAYS wins → tag as [D>...<D] only, NEVER [A>...<A]")
        #         state_rules_str = ", ".join(sr_parts) if sr_parts else f"State: {detected_state or 'unknown'}"
        #
        #         gpt_xlsx = output_path.replace(".txt", "_gpt_comparison.xlsx")
        #         gpt_cmp = gpt_compare_tags(
        #             highlighted_images_b64=hi_images_b64,
        #             pymupdf_clean_text=pymupdf_clean_text,
        #             claude_converted_text=converted_text,
        #             state_code=detected_state or "XX",
        #             state_rules=state_rules_str,
        #             output_xlsx=gpt_xlsx,
        #             pymupdf_report=pymupdf_report,
        #             pymupdf_raw_text=pymupdf_text,
        #         )
        #
        #         audit_excel_path = gpt_xlsx
        #         add_log(
        #             f"✅ GPT audit complete — "
        #             f"Checklist: {gpt_cmp['total_checklist']} items | "
        #             f"✅ Pass: {gpt_cmp['passes']} | "
        #             f"⚠️ Warnings: {gpt_cmp['warnings']} | "
        #             f"❌ Errors: {gpt_cmp['errors']}"
        #         )
        #         gpt_audit_failed = False
        #     except Exception as e:
        #         add_log(f"⚠️ GPT audit error: {e}", "warning")
        #         logging.error(f"BatchWorker GPT audit error: {e}", exc_info=True)
        #         gpt_audit_failed = True
        # else:
        #     add_log("⏭️ Skipping GPT audit (no PyMuPDF data)")

        # Wrap up
        elapsed = time.time() - start_time
        minutes, seconds = int(elapsed // 60), int(elapsed % 60)
        processing_time_str = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"
        add_log(f"✅ Processing complete in {processing_time_str}")

        session_mgr.complete_processing(session_id, output_path)

        # Read output bytes for download
        output_data = None
        try:
            with open(output_path, 'rb') as f:
                output_data = f.read()
        except Exception:
            pass

        try:
            _file_size_bytes = os.path.getsize(pdf_path)
        except Exception:
            _file_size_bytes = 0

        return {
            "file_name": file_name,
            "pdf_path": pdf_path,
            "file_size": _file_size_bytes,
            "output_path": output_path,
            "status": "success",
            "detected_state": detected_state or "",
            "processing_time": processing_time_str,
            "conversion_time": conversion_time_str,
            "total_elapsed_secs": elapsed,
            "conversion_elapsed_secs": conversion_elapsed,
            "audit_excel_path": audit_excel_path,
            "audit_highlighted_dir": audit_highlighted_dir,
            "audit_json_report": audit_json_report,
            "content_accuracy_report": content_accuracy_report,
            "gpt_audit_failed": gpt_audit_failed,
            "processing_logs": logs,
            "output_data": output_data,
        }


def show_batch_processing():
    """Show batch processing UI with background worker.

    Files process in a background thread.  The UI auto-refreshes every
    5 seconds so the user can review completed files while remaining
    files are still being processed.
    """
    n_files = len(st.session_state.batch_pdf_paths)
    worker: BatchWorker | None = st.session_state.get('batch_worker')

    # ── Sync completed results from worker → session state ────────────
    if worker is not None:
        completed = worker.get_completed_results()
        # Only append new results (worker list is append-only).
        # batch_results_offset tracks how many results existed before
        # this worker started (needed for retries that keep prior results).
        _offset = st.session_state.get('batch_results_offset', 0)
        n_from_worker = len(st.session_state.batch_results) - _offset
        if len(completed) > n_from_worker:
            for r in completed[n_from_worker:]:
                st.session_state.batch_results.append(r)
        # Check if done
        if worker.is_done():
            st.session_state.batch_processing = False
            st.session_state.uploader_key_counter += 1

    batch_results = st.session_state.batch_results
    is_processing = st.session_state.batch_processing

    # ── If batch is fully done, show final results ────────────────────
    # Only switch to the final results view if the user explicitly opts in
    # (via a button) so we don't disrupt validation-review work mid-flow.
    if not is_processing and batch_results and len(batch_results) == n_files:
        if st.session_state.get("_batch_show_final"):
            st.session_state.pop("_batch_show_final", None)
            show_batch_results_section()
            return
        # Otherwise fall through to the live view — it already shows
        # "batch complete" banners and the Validation Review tab keeps
        # working undisturbed.

    # ── Process button (before processing starts) ─────────────────────
    if not is_processing and not batch_results:
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            if st.button(
                f"🚀 Process {n_files} Documents",
                type="primary",
                use_container_width=True,
                help="Process all uploaded PDFs using Claude Opus 4.6. "
                     "You can review completed files while others are still processing.",
            ):
                session_mgr = st.session_state.session_manager
                session_folders = session_mgr.get_session_folders(st.session_state.session_id)
                session_mgr.start_processing(st.session_state.session_id)

                worker = BatchWorker(
                    pdf_paths=st.session_state.batch_pdf_paths,
                    file_names=st.session_state.batch_file_names,
                    session_id=st.session_state.session_id,
                    session_mgr=session_mgr,
                    session_folders=session_folders,
                )
                st.session_state.batch_worker = worker
                st.session_state.batch_processing = True
                st.session_state.batch_current_idx = 0
                st.session_state.batch_results = []
                st.session_state.batch_results_offset = 0
                st.session_state.process_method = "claude_opus"
                st.session_state.pop("_batch_show_final", None)
                worker.start()
                st.rerun()
        return

    # ── Processing in progress OR partially complete ──────────────────
    progress_info = worker.get_progress() if worker else {
        "current_idx": 0, "current_file": "", "n_completed": len(batch_results),
        "total": n_files, "done": not is_processing, "current_logs": [],
    }
    n_completed = len(batch_results)
    n_total = n_files

    # ── Processing status banner (above tabs, always visible) ─────────
    if is_processing:
        _cur_file = progress_info["current_file"]
        _cur_idx = progress_info["current_idx"]
        _offset = st.session_state.get('batch_results_offset', 0)
        _is_retry = _offset > 0
        _worker_total = worker.total if worker else (n_total - _offset)
        _worker_done = len(batch_results) - _offset

        if _is_retry:
            _header_text = (
                f'<strong style="color:#1e40af;">🔄 Retrying file '
                f'{_cur_idx + 1} of {_worker_total}</strong>'
                f' — <span style="color:#1e40af;">{_cur_file}</span>'
                f'<br><span style="font-size:0.85rem; color:#3b82f6;">'
                f'{_worker_done} retried · {_offset} previously succeeded</span>'
            )
        else:
            _header_text = (
                f'<strong style="color:#1e40af;">⏳ Processing file '
                f'{_cur_idx + 1} of {n_total}</strong>'
                f' — <span style="color:#1e40af;">{_cur_file}</span>'
                f'<br><span style="font-size:0.85rem; color:#3b82f6;">'
                f'{n_completed} completed · '
                f'{n_total - n_completed - 1} remaining after current</span>'
            )
        st.markdown(
            f'<div style="background:#eff6ff; border-radius:10px; padding:14px 20px; '
            f'border-left:4px solid #3b82f6; margin-bottom:12px;">'
            f'{_header_text}</div>',
            unsafe_allow_html=True,
        )
        st.progress(n_completed / n_total,
                    text=f"Completed {n_completed}/{n_total} files")
    else:
        _n_ok_live = sum(1 for r in batch_results if r['status'] == 'success')
        _n_fail_live = n_total - _n_ok_live
        if _n_fail_live:
            st.warning(
                f"⚠️ **Batch complete** — "
                f"{_n_ok_live}/{n_total} succeeded, "
                f"{_n_fail_live} failed. "
                f"Switch to the **Batch Summary** tab to retry failed files."
            )
        else:
            st.success(
                f"🎉 **Batch complete!** "
                f"{_n_ok_live}/{n_total} files processed successfully."
            )

    # ── Tab layout (live) ─────────────────────────────────────────────
    tab_labels = ["📦 Progress"]
    if any(r.get('processing_logs') for r in batch_results):
        tab_labels.append("📋 Activity Logs")

    tabs = st.tabs(tab_labels)

    # ══════════════════════════════════════════════════════════════════
    # TAB 1 — Live Progress
    # ══════════════════════════════════════════════════════════════════
    with tabs[0]:
        if is_processing:
            # Auto-refreshing live log fragment (only refreshes this fragment, not the whole page)
            _batch_live_log_fragment()

        # Completed files mini-table
        if batch_results:
            st.markdown("#### ✅ Completed Files")
            _completed_html = '<div style="margin-bottom:12px;">'
            for i, res in enumerate(batch_results):
                _icon = "✅" if res['status'] == 'success' else "❌"
                _t = res.get('processing_time', '—')
                _s = res.get('detected_state', '')
                _completed_html += (
                    f'<div style="padding:4px 8px;font-size:0.85rem;">'
                    f'{_icon} <strong>{res["file_name"]}</strong>'
                    f'{f" — {_s}" if _s else ""}'
                    f' <span style="color:#64748b;">({_t})</span></div>'
                )
            _completed_html += '</div>'
            st.markdown(_completed_html, unsafe_allow_html=True)

        # Pending files
        if is_processing:
            _pending_start = n_completed + 1  # skip current (shown above)
            _pending_names = st.session_state.batch_file_names[_pending_start:]
            if _pending_names:
                with st.expander(f"⏳ Pending ({len(_pending_names)} files)", expanded=False):
                    for i, fn in enumerate(_pending_names):
                        st.markdown(f"<span style='font-size:0.85rem;color:#64748b;'>#{_pending_start + i + 1} — {fn}</span>",
                                    unsafe_allow_html=True)

        # "View Batch Summary" button (shown only after all files are done)
        if not is_processing and batch_results and len(batch_results) == n_total:
            st.markdown("")
            _bc1, _bc2, _bc3 = st.columns([1, 2, 1])
            with _bc2:
                if st.button(
                    "📊 View Batch Summary",
                    type="primary",
                    use_container_width=True,
                    key="batch_view_summary_btn",
                    help="Switch to the final Batch Summary view with aggregated stats and retry options.",
                ):
                    st.session_state["_batch_show_final"] = True
                    st.rerun()

    # ══════════════════════════════════════════════════════════════════
    # TAB 2 — Activity Logs
    # ══════════════════════════════════════════════════════════════════
    if any(r.get('processing_logs') for r in batch_results) and len(tabs) > 1:
        with tabs[1]:
            for i, r in enumerate(batch_results):
                if r.get('processing_logs'):
                    with st.expander(f"📋 {r['file_name']}", expanded=(i == len(batch_results) - 1)):
                        st.markdown(
                            _build_activity_feed_html(r['processing_logs']),
                            unsafe_allow_html=True,
                        )

    # ── Auto-refresh (polls worker every 5s for NEW RESULTS or DONE only) ──
    # Log updates are handled by the fragment inside the Progress tab,
    # so the Validation Review tab is not disrupted.
    if is_processing:
        _batch_result_refresh()


@st.fragment(run_every=5)
def _batch_live_log_fragment():
    """Auto-refreshing fragment that shows the live log for the current file.

    This runs inside the Progress tab only, so it does NOT disrupt the
    Validation Review or Activity Logs tabs.
    """
    worker: BatchWorker | None = st.session_state.get('batch_worker')
    if worker is None:
        return
    _progress = worker.get_progress()
    _cur_file = _progress.get('current_file', '')
    _cur_logs = _progress.get('current_logs', [])
    if _cur_logs:
        with st.expander(f"📋 Live Log — {_cur_file}", expanded=True):
            st.markdown(_build_activity_feed_html(_cur_logs), unsafe_allow_html=True)
    else:
        st.caption("⏳ Waiting for log output…")


@st.fragment(run_every=5)
def _batch_result_refresh():
    """Auto-refresh that triggers a full page rerun ONLY when a new file
    result arrives or the worker finishes.  Log-only changes are ignored
    here (handled by the in-tab fragment) so the Validation Review tab
    is never disrupted."""
    worker: BatchWorker | None = st.session_state.get('batch_worker')
    if worker is None:
        return

    completed = worker.get_completed_results()
    _offset = st.session_state.get('batch_results_offset', 0)
    n_from_worker = len(st.session_state.batch_results) - _offset

    # Only rerun for important events: new file completed or worker done
    if len(completed) > n_from_worker or worker.is_done():
        st.rerun()


def show_batch_results_section():
    """Show batch results: summary table + file selector + per-file validation review."""
    results = st.session_state.batch_results
    n_ok = sum(1 for r in results if r['status'] == 'success')
    n_fail = len(results) - n_ok

    # Compute aggregate stats
    _total_time_secs = sum(r.get('total_elapsed_secs', 0) for r in results)
    _total_conv_secs = sum(r.get('conversion_elapsed_secs', 0) for r in results)
    _total_val_secs = max(0, _total_time_secs - _total_conv_secs)
    _tm, _ts = int(_total_time_secs // 60), int(_total_time_secs % 60)
    _total_time_str = f"{_tm}m {_ts}s" if _tm > 0 else f"{_ts}s"
    _cm, _cs = int(_total_conv_secs // 60), int(_total_conv_secs % 60)
    _total_conv_str = f"{_cm}m {_cs}s" if _cm > 0 else f"{_cs}s"
    _vm, _vs = int(_total_val_secs // 60), int(_total_val_secs % 60)
    _total_val_str = f"{_vm}m {_vs}s" if _vm > 0 else f"{_vs}s"

    # ── Tab layout ────────────────────────────────────────────────────
    tab_labels = ["📊 Batch Summary"]
    if any(r.get('processing_logs') for r in results):
        tab_labels.append("📋 Activity Logs")

    tabs = st.tabs(tab_labels)

    # ══════════════════════════════════════════════════════════════════
    # TAB 1 — Batch Summary
    # ══════════════════════════════════════════════════════════════════
    with tabs[0]:
        # ── Summary card with chips (matches single-file style) ───────
        _chip = (
            'display:inline-flex; align-items:center; gap:6px; '
            'padding:8px 16px; border-radius:10px; font-size:0.85rem; font-weight:600;'
        )
        _conv_chip = (
            f'<div style="{_chip} background:#f0fdf4; color:#166534; border:1px solid #bbf7d0;">'
            f'<span style="font-size:1rem;">📝</span> Conversion '
            f'<span style="font-weight:800;">{_total_conv_str}</span></div>'
        )
        _val_chip = (
            f'<div style="{_chip} background:#eff6ff; color:#1e40af; border:1px solid #bfdbfe;">'
            f'<span style="font-size:1rem;">🔍</span> Validation '
            f'<span style="font-weight:800;">{_total_val_str}</span></div>'
        ) if _total_val_secs > 0 else ""
        _files_chip = (
            f'<div style="{_chip} background:#f5f3ff; color:#5b21b6; border:1px solid #ddd6fe;">'
            f'<span style="font-size:1rem;">📦</span> Files '
            f'<span style="font-weight:800;">{n_ok}/{len(results)}</span></div>'
        )
        _total_size_bytes = sum(r.get('file_size', 0) for r in results)
        _total_size_str = format_file_size(_total_size_bytes) if _total_size_bytes > 0 else '—'
        _size_chip = (
            f'<div style="{_chip} background:#fefce8; color:#854d0e; border:1px solid #fde68a;">'
            f'<span style="font-size:1rem;">💾</span> Total Size '
            f'<span style="font-weight:800;">{_total_size_str}</span></div>'
        )

        st.markdown(
            f'<div style="background:#ffffff; border-radius:14px; padding:24px 28px; margin:8px 0 20px;'
            f' border:1px solid #e2e8f0; box-shadow:0 2px 12px rgba(0,0,0,.05);">'
            f'<div style="display:flex; align-items:center; gap:12px; margin-bottom:16px; flex-wrap:wrap;">'
            f'<span style="font-size:1.5rem;">✅</span>'
            f'<span style="font-size:1.2rem; font-weight:700; color:#15803d;">Batch Processing Complete</span>'
            f'{"<span style=&quot;background:#fef2f2; color:#991b1b; padding:3px 12px; border-radius:16px; font-size:0.82rem; font-weight:600;&quot;>" + str(n_fail) + " failed</span>" if n_fail else ""}'
            f'</div>'
            f'<div style="display:flex; gap:10px; flex-wrap:wrap; margin-bottom:14px;">'
            f'{_conv_chip}{_val_chip}{_files_chip}{_size_chip}'
            f'</div>'
            f'<div style="background:#f8fafc; border-radius:8px; padding:8px 16px;'
            f' display:flex; align-items:center; justify-content:space-between;'
            f' border:1px solid #e2e8f0;">'
            f'<span style="font-size:0.84rem; color:#475569;">⏱️ Total batch time (all files combined)</span>'
            f'<span style="font-size:1rem; font-weight:800; color:#0f172a;">{_total_time_str}</span>'
            f'</div>'
            f'<p style="margin:12px 0 0; font-size:0.82rem; color:#64748b;">'
            f'{n_ok} PDF(s) converted to tagged legislative text with Claude Opus 4.6. '
            f'{"Click the <b>Validation Review</b> tab to inspect each file." if any(r.get("audit_excel_path") for r in results) else ""}'
            f'</p></div>',
            unsafe_allow_html=True,
        )

        # ── Results table with Validation Time column ─────────────────
        _tbl = (
            '<table style="width:100%;border-collapse:collapse;font-size:0.85rem;">'
            '<thead><tr style="background:#1e293b;color:white;">'
            '<th style="padding:10px;text-align:left;">#</th>'
            '<th style="padding:10px;text-align:left;">File</th>'
            '<th style="padding:10px;text-align:center;">State</th>'
            '<th style="padding:10px;text-align:center;">Conv. Time</th>'
            '<th style="padding:10px;text-align:center;">Valid. Time</th>'
            '<th style="padding:10px;text-align:center;">Total Time</th>'
            '<th style="padding:10px;text-align:center;">Text Accuracy</th>'
            '<th style="padding:10px;text-align:center;">Status</th>'
            '</tr></thead><tbody>'
        )
        for i, r in enumerate(results):
            _bg = '#f0fdf4' if r['status'] == 'success' else '#fef2f2'
            if r['status'] == 'success':
                _status_badge = '<span style="background:#bbf7d0;color:#166534;padding:2px 10px;border-radius:12px;font-weight:600;">✅ Done</span>'
            elif r.get('error_category') == 'api_network':
                _status_badge = (
                    '<span style="background:#fef3c7;color:#92400e;padding:2px 10px;'
                    'border-radius:12px;font-weight:600;" '
                    'title="API/Network issue — can be retried">🔄 Retry Needed</span>'
                )
            else:
                _status_badge = '<span style="background:#fecaca;color:#991b1b;padding:2px 10px;border-radius:12px;font-weight:600;">❌ Failed</span>'
            _state = r.get('detected_state', '')
            _state_name = US_STATE_NAMES.get(_state, _state) if _state else '—'
            _acc = '—'
            _acc_report = r.get('content_accuracy_report')
            if _acc_report:
                _acc = f"{_acc_report['accuracy']:.1f}%"

            # Compute per-file validation time
            _r_total = r.get('total_elapsed_secs', 0)
            _r_conv = r.get('conversion_elapsed_secs', 0)
            if _r_total and _r_conv and r.get('audit_excel_path'):
                _r_val = max(0, _r_total - _r_conv)
                _rvm, _rvs = int(_r_val // 60), int(_r_val % 60)
                _val_time_str = f"{_rvm}m {_rvs}s" if _rvm > 0 else f"{_rvs}s"
            else:
                _val_time_str = '—'

            # Build file name cell — add reason line for failures
            _fname_cell = r["file_name"]
            if r['status'] != 'success' and r.get('error_reason'):
                _fname_cell += (
                    f'<br><span style="font-size:0.78rem;color:#92400e;font-style:italic;">'
                    f'{r["error_reason"]}</span>'
                )

            _tbl += (
                f'<tr style="background:{_bg};border-bottom:1px solid #e2e8f0;">'
                f'<td style="padding:8px 10px;font-weight:600;">{i+1}</td>'
                f'<td style="padding:8px 10px;">{_fname_cell}</td>'
                f'<td style="padding:8px 10px;text-align:center;">{_state_name}</td>'
                f'<td style="padding:8px 10px;text-align:center;">{r.get("conversion_time", "—")}</td>'
                f'<td style="padding:8px 10px;text-align:center;">{_val_time_str}</td>'
                f'<td style="padding:8px 10px;text-align:center;">{r.get("processing_time", "—")}</td>'
                f'<td style="padding:8px 10px;text-align:center;">{_acc}</td>'
                f'<td style="padding:8px 10px;text-align:center;">{_status_badge}</td>'
                f'</tr>'
            )
        _tbl += '</tbody></table>'
        st.markdown(_tbl, unsafe_allow_html=True)

        # ── Retry Failed Files button ─────────────────────────────────
        _failed_results = [r for r in results if r['status'] != 'success']
        if _failed_results:
            # Show info callout explaining the failures
            _n_api = sum(1 for r in _failed_results if r.get('error_category') == 'api_network')
            _n_other = len(_failed_results) - _n_api
            _parts = []
            if _n_api:
                _parts.append(
                    f"**{_n_api}** file{'s' if _n_api > 1 else ''} failed due to "
                    f"an **API / network interruption** (not a code issue)"
                )
            if _n_other:
                _parts.append(
                    f"**{_n_other}** file{'s' if _n_other > 1 else ''} had a "
                    f"processing error"
                )
            st.info(
                " · ".join(_parts)
                + ". You can retry the failed files below.",
                icon="ℹ️",
            )

            col_r1, col_r2, col_r3 = st.columns([1, 2, 1])
            with col_r2:
                if st.button(
                    f"🔄 Retry {len(_failed_results)} Failed File{'s' if len(_failed_results) > 1 else ''}",
                    type="primary",
                    use_container_width=True,
                    key="batch_retry_failed",
                ):
                    # Collect failed pdf_paths / file_names
                    _retry_paths = [r['pdf_path'] for r in _failed_results]
                    _retry_names = [r['file_name'] for r in _failed_results]

                    # Keep only the successful results
                    st.session_state.batch_results = [
                        r for r in results if r['status'] == 'success'
                    ]
                    # Update file lists to only the retry set
                    st.session_state.batch_pdf_paths = (
                        [r['pdf_path'] for r in st.session_state.batch_results] + _retry_paths
                    )
                    st.session_state.batch_file_names = (
                        [r['file_name'] for r in st.session_state.batch_results] + _retry_names
                    )

                    session_mgr = st.session_state.session_manager
                    session_folders = session_mgr.get_session_folders(
                        st.session_state.session_id
                    )

                    worker = BatchWorker(
                        pdf_paths=_retry_paths,
                        file_names=_retry_names,
                        session_id=st.session_state.session_id,
                        session_mgr=session_mgr,
                        session_folders=session_folders,
                    )
                    st.session_state.batch_worker = worker
                    st.session_state.batch_processing = True
                    st.session_state.batch_results_offset = len(
                        st.session_state.batch_results
                    )
                    st.session_state.pop("_batch_show_final", None)
                    worker.start()
                    st.rerun()

    # ══════════════════════════════════════════════════════════════════
    # TAB 2 — Activity Logs (per-file)
    # ══════════════════════════════════════════════════════════════════
    if any(r.get('processing_logs') for r in results):
        with tabs[1]:
            for i, r in enumerate(results):
                if r.get('processing_logs'):
                    with st.expander(f"📋 {r['file_name']}", expanded=(i == 0)):
                        st.markdown(
                            _build_activity_feed_html(r['processing_logs']),
                            unsafe_allow_html=True,
                        )


def _build_batch_zip(results: list[dict]) -> bytes | None:
    """Create a ZIP file in memory with all successful output files."""
    import zipfile
    import io

    buf = io.BytesIO()
    has_files = False

    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for r in results:
            if r.get('output_data') and r['status'] == 'success':
                fname = Path(r['file_name']).stem + "_claude_opus.txt"
                zf.writestr(fname, r['output_data'])
                has_files = True

    if not has_files:
        return None

    buf.seek(0)
    return buf.getvalue()


def show_single_file_processing():
    """Show single file processing interface"""
    # Don't show buttons if processing is already complete and results exist
    _has_results = (
        st.session_state.get('processed_file_path')
        and (
            st.session_state.get('processed_file_data') is not None
            or os.path.exists(st.session_state.get('processed_file_path', ''))
        )
    )

    # Center the layout with columns
    col1, col2, col3 = st.columns([1, 2, 1])

    _pdf_pages = st.session_state.get('_pdf_page_count', 0)

    # ── Auto-trigger processing on upload ──────────────────────────────
    # The pipeline is chosen automatically based on **state first**, then
    # page count:
    #
    #   1. State-specific override:
    #        • CO  → Visual / Process Document  (Claude Opus vision)
    #               — Colorado bills don't need PyMuPDF tagging or the
    #                 GPT validation audit, so we go straight to Vision.
    #
    #   2. Default (when no state-specific rule matched):
    #        • ≤ 15 pages  → PyMuPDF Extract + Clean (cheap, fast)
    #        • > 15 pages  → Hybrid Mode (PyMuPDF + chunked LLM cleaning)
    #
    # We track the file ID we auto-started so processing failures don't
    # cause an infinite re-trigger loop.  On failure the user sees a
    # single explicit "Retry" button as a recovery affordance.
    _current_file_id = st.session_state.get('last_uploaded_file_id')
    _auto_started_for = st.session_state.get('_auto_started_file_id')
    _already_triggered = (_auto_started_for == _current_file_id)
    _early_state = (st.session_state.get('detected_state') or "").upper()

    # States that must always use the Visual / Process Document pipeline,
    # regardless of page count.  Add more state codes here as needed.
    _VISION_ONLY_STATES = {"CO"}

    with col2:
        if (not st.session_state.processing
                and not _has_results
                and st.session_state.get('temp_pdf_path')
                and _current_file_id):

            if not _already_triggered:
                # First time we see this file with no results → kick off
                # the right pipeline.  State override beats page count.
                # Scanned PDFs must always use Visual mode (no extractable text).
                _is_scanned = st.session_state.get('_pdf_is_scanned', False)

                # ── HITL gate for scanned PDFs ────────────────────────────
                # Require explicit user confirmation before processing scanned PDFs.
                if _is_scanned and not st.session_state.get('_scanned_hitl_approved', False):
                    st.warning(
                        f"📷 **Scanned PDF — Human Review Required**\n\n"
                        f"This file appears to be a **scanned (image-based) PDF** "
                        f"with **{_pdf_pages} page(s)**.\n\n"
                        f"Processing will use the **Visual (Claude Opus)** pipeline "
                        f"which sends each page as an image to the LLM for text extraction.\n\n"
                        f"⚠️ **Please confirm:**\n"
                        f"- The file contains legislative text (not forms, maps, etc.)\n"
                        f"- You accept the higher token cost for image-based conversion"
                    )
                    _col_approve, _col_skip = st.columns([1, 1])
                    with _col_approve:
                        if st.button("✅ Approve & Process", key="hitl_approve_scanned", type="primary", use_container_width=True):
                            st.session_state['_scanned_hitl_approved'] = True
                            st.rerun()
                    with _col_skip:
                        if st.button("❌ Skip this file", key="hitl_skip_scanned", use_container_width=True):
                            # Clean up and reset
                            try:
                                os.remove(st.session_state.temp_pdf_path)
                            except Exception:
                                pass
                            st.session_state.temp_pdf_path = None
                            st.session_state.last_uploaded_file_id = None
                            st.session_state['_pdf_is_scanned'] = False
                            st.toast("⏭️ Scanned PDF skipped.")
                            st.rerun()
                    # Don't auto-start — wait for user action
                    pass
                elif _is_scanned:
                    # User approved via HITL → proceed with visual mode
                    st.session_state.process_method = "claude_opus"
                    _msg = "📷 Scanned PDF (approved) — running **Process Document (Visual)** pipeline"
                # ──────────────────────────────────────────────────────────
                elif _early_state in _VISION_ONLY_STATES:
                    st.session_state.process_method = "claude_opus"
                    _state_label = US_STATE_NAMES.get(_early_state, _early_state)
                    _msg = (
                        f"🏛️ {_state_label} ({_early_state}) detected — "
                        f"running **Process Document (Visual)** pipeline"
                    )
                elif _pdf_pages > 15:
                    st.session_state.process_method = "hybrid"
                    _msg = f"📐 Long PDF detected ({_pdf_pages} pages) — running **Hybrid Mode**"
                else:
                    st.session_state.process_method = "pymupdf_clean_only"
                    _msg = "📄 Running **Extract + Clean** pipeline"

                # Only trigger processing if we're not waiting for HITL approval
                _waiting_for_hitl = (_is_scanned and not st.session_state.get('_scanned_hitl_approved', False))
                if not _waiting_for_hitl and st.session_state.get('process_method'):
                    st.session_state.processing = True
                    st.session_state.current_step = 0
                    st.session_state.processing_time = None
                    st.session_state.chunk_stats = None
                    st.session_state.pop('_pymupdf_only_steps', None)
                    st.session_state._auto_started_file_id = _current_file_id

                    logging.info(
                        f"Auto-triggered {st.session_state.process_method} "
                        f"for {_pdf_pages}-page PDF (state={_early_state or 'unknown'})"
                    )
                    st.info(_msg)
                    st.rerun()
            else:
                # We already attempted processing for this file but it
                # didn't produce results.  Show a single recovery button.
                st.error("❌ Processing did not complete for this document.")
                if st.button(
                    "🔄 Retry Processing",
                    type="primary",
                    use_container_width=True,
                    help="Re-run the pipeline for the same document",
                ):
                    # Force re-trigger on next rerun
                    st.session_state._auto_started_file_id = None
                    st.rerun()

        # While `st.session_state.processing` is True, the activity-feed
        # progress display (step icons + progress bar + live activity log)
        # below this section already conveys "we're working on it" — no
        # need for an extra yellow "Please wait" banner here.

    # Commented out alternative processing methods
    # with col1:
    #     if not st.session_state.processing:
    #         if st.button("🚀 Process (Vision)", type="primary", use_container_width=True, 
    #                     help="Fast processing with Claude Vision API"):
    #             st.session_state.processing = True
    #             st.session_state.current_step = 0
    #             st.session_state.processing_time = None
    #             st.session_state.chunk_stats = None
    #             st.session_state.process_method = "vision"
    #             st.rerun()
    #     else:
    #         if st.button("⏹️ Stop Processing", type="secondary", use_container_width=True):
    #             st.session_state.processing = False
    #             st.session_state.current_step = 0
    #             st.rerun()
    
    # with col2:
    #     if not st.session_state.processing:
    #         if st.button("📄 Process (PDFPlumber)", use_container_width=True, 
    #                     help="Reliable extraction with state-specific cleaning"):
    #             st.session_state.processing = True
    #             st.session_state.current_step = 0
    #             st.session_state.processing_time = None
    #             st.session_state.chunk_stats = None
    #             st.session_state.process_method = "pdfplumber"
    #             st.rerun()
    #     else:
    #         if st.button("🗑️ Clear & Reset", use_container_width=True):
    #             # Reset everything
    #             st.session_state.processing = False
    #             st.session_state.current_step = 0
    #             st.session_state.step_status = {}
    #             st.session_state.processed_file_path = None
    #             st.session_state.processing_time = None
    #             st.session_state.chunk_stats = None
    #             if st.session_state.temp_pdf_path and os.path.exists(st.session_state.temp_pdf_path):
    #                 os.remove(st.session_state.temp_pdf_path)
    #             st.session_state.temp_pdf_path = None
    #             st.rerun()
    
    # Processing progress
    if st.session_state.processing:
        # No step-icon row / "Step N/M" progress bar at the top — the
        # Activity Log expander below already shows live, timestamped
        # progress with the same information.  Keep only the inline
        # status banner and the activity log itself.
        status_placeholder = st.empty()
        log_container = st.expander("📋 Activity Log", expanded=True)
        log_placeholder = log_container.empty()  # live-updating slot inside the expander

        # Display any logs already collected (e.g. from a previous partial run)
        with log_placeholder.container():
            if st.session_state.processing_logs:
                st.markdown(_build_activity_feed_html(st.session_state.processing_logs), unsafe_allow_html=True)
        
        # Choose processing method
        _state = st.session_state.get('detected_state', 'AUTO')
        if st.session_state.process_method == "pdfplumber":
            result = process_with_pdfplumber_pipeline(
                st.session_state.temp_pdf_path,
                _state,
                st.session_state.session_id,
                None,                       # progress_placeholder — disabled
                status_placeholder
            )
        elif st.session_state.process_method == "tag_then_clean":
            result = process_with_tag_then_clean_pipeline(
                st.session_state.temp_pdf_path,
                _state,
                st.session_state.session_id,
                None,                       # progress_placeholder — disabled
                status_placeholder
            )
        elif st.session_state.process_method == "claude_opus":
            result = process_with_claude_opus(
                st.session_state.temp_pdf_path,
                _state,
                st.session_state.session_id,
                None,                       # progress_placeholder — disabled
                status_placeholder,
                log_placeholder
            )
        elif st.session_state.process_method == "pymupdf_clean_only":
            result = process_pymupdf_clean_only(
                st.session_state.temp_pdf_path,
                _state,
                st.session_state.session_id,
                None,                       # progress_placeholder — disabled
                status_placeholder,
                log_placeholder
            )
        elif st.session_state.process_method == "hybrid":
            result = process_with_hybrid_mode(
                st.session_state.temp_pdf_path,
                _state,
                st.session_state.session_id,
                None,                       # progress_placeholder — disabled
                status_placeholder,
                log_placeholder
            )
        else:
            # Default vision processing
            result = process_single_pdf_simple(
                st.session_state.temp_pdf_path,
                _state,
                st.session_state.session_id,
                None,                       # progress_placeholder — disabled
                status_placeholder,
                log_placeholder
            )
        
        if result:
            st.session_state.processed_file_path = result
            st.session_state.processing = False
            # Cache file bytes immediately into session_state so the download
            # button works even if a subsequent request is routed to a different
            # Kubernetes pod (which has no local copy of the file).
            try:
                with open(result, 'rb') as _f:
                    st.session_state.processed_file_data = _f.read()
            except Exception as _read_err:
                logging.warning(f"Could not cache output file bytes: {_read_err}")
                st.session_state.processed_file_data = None

            # ── Persist input + output via SessionManager ────────────
            try:
                _pdf_src = st.session_state.temp_pdf_path
                _state_code = st.session_state.get('detected_state', 'UNK') or 'UNK'
                _persist_sm = st.session_state.get('session_manager') or get_session_manager()
                _pp_dir = _persist_sm.persist_session(_pdf_src, result, _state_code)
                if _pp_dir:
                    logging.info(f"[Pipeline] Persisted session: {_pp_dir}")
            except Exception as _pp_err:
                logging.warning(f"Could not persist session: {_pp_err}")

            success_msg = "🎉 Processing completed successfully!"
            if st.session_state.processing_time:
                success_msg += f" ⏱️ Total time: {st.session_state.processing_time}"
            st.success(success_msg)
        else:
            st.session_state.processing = False
            st.error("❌ Processing failed!")
        
        st.rerun()
    
    # Results section — show if we have a path OR cached bytes.
    # Do NOT gate on os.path.exists() because on multi-pod Kubernetes deployments
    # the file lives on whichever pod did the processing; subsequent requests may
    # be routed to a different pod where the file does not exist on disk.
    if st.session_state.processed_file_path and (
        st.session_state.get('processed_file_data') is not None
        or os.path.exists(st.session_state.processed_file_path)
    ):
        show_results_section()

# Main app
def main():
    """Enterprise main application with session management"""
    # Setup logging
    setup_simple_logging()

    # Show the enterprise UI with session management
    show_simple_ui()

if __name__ == "__main__":
    main()