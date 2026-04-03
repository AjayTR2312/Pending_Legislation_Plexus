import streamlit as st
import os
import sys
import base64
import shutil
from datetime import datetime
from pathlib import Path
import time
import logging

# Add the current directory to the Python path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

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
    logging.warning("⚠️ pdf_to_text_llm module not found — legacy pipeline disabled")
try:
    from pdf_docx_analysis_pipeline import PDFToDocxAnalysisPipeline
except ImportError:
    PDFToDocxAnalysisPipeline = None
    logging.warning("⚠️ pdf_docx_analysis_pipeline module not found — DOCX analysis disabled")
from session_manager import get_session_manager
try:
    from pdf_plumber_extractor import extract_pdf_with_pdfplumber
except ImportError:
    extract_pdf_with_pdfplumber = None
    logging.warning("⚠️ pdf_plumber_extractor module not found — pdfplumber extraction disabled")
try:
    from text_to_clean_prompt import convert_pdfplumber_to_clean
except ImportError:
    convert_pdfplumber_to_clean = None
    logging.warning("⚠️ text_to_clean_prompt module not found — pdfplumber clean pipeline disabled")
try:
    from tag_then_clean_pipeline import tag_then_clean_pipeline
except ImportError:
    tag_then_clean_pipeline = None
    logging.warning("⚠️ tag_then_clean_pipeline module not found — tag-then-clean pipeline disabled")
try:
    from validation_review_inline import render_inline_validation_review
except ImportError:
    render_inline_validation_review = None
    logging.warning("⚠️ validation_review_inline module not found — validation review disabled")
try:
    # Ensure Opus4.6 dir is on path for compare_files
    _opus46 = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'Opus4.6'))
    if _opus46 not in sys.path:
        sys.path.insert(0, _opus46)
    from compare_files import compare_texts
except ImportError:
    compare_texts = None
    logging.warning("⚠️ compare_files module not found — content accuracy check disabled")
try:
    from pdf_type_detector import detect_pdf_type
except ImportError:
    detect_pdf_type = None
    logging.warning("⚠️ pdf_type_detector module not found — scanned PDF detection disabled")

# Configure Streamlit page
st.set_page_config(
    page_title="Pending Legislation Processor",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="collapsed"
)

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
    if 'content_accuracy_report' not in st.session_state:
        st.session_state.content_accuracy_report = None
    
    # Batch processing state
    if 'batch_mode' not in st.session_state:
        st.session_state.batch_mode = False
    if 'batch_queue' not in st.session_state:
        st.session_state.batch_queue = []
    if 'batch_processing' not in st.session_state:
        st.session_state.batch_processing = False
    if 'batch_current_file' not in st.session_state:
        st.session_state.batch_current_file = None
    if 'batch_results' not in st.session_state:
        st.session_state.batch_results = []
    if 'batch_progress' not in st.session_state:
        st.session_state.batch_progress = 0
    if 'batch_paused' not in st.session_state:
        st.session_state.batch_paused = False
    if 'uploaded_files' not in st.session_state:
        st.session_state.uploaded_files = []
    if 'last_uploaded_file_id' not in st.session_state:
        st.session_state.last_uploaded_file_id = None

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
    # Clear any existing handlers to prevent duplicates
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)
        
    current_time = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_file_path = os.path.join(LOG_FOLDER, f'simple_app_{current_time}.log')
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file_path, encoding="utf-8"),
            logging.StreamHandler()
        ]
    )
    
    logging.info(f"✅ Session-specific logging configured. Logs saved to: {log_file_path}")
    logging.info(f"📁 Session ID: {st.session_state.session_id}")
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

# Batch processing functions
def add_files_to_batch(uploaded_files, state_code, session_id):
    """Add uploaded files to batch processing queue (skips scanned PDFs)"""
    session_mgr = st.session_state.session_manager
    added_count = 0
    skipped_scanned = []

    for uploaded_file in uploaded_files:
        # Upload file using session manager
        temp_path = session_mgr.upload_file(session_id, uploaded_file, state_code)

        if temp_path:
            # ── Scanned PDF gate ──────────────────────────────────
            if detect_pdf_type is not None:
                pdf_analysis = detect_pdf_type(temp_path)
                if pdf_analysis and pdf_analysis['type'] == 'scanned':
                    skipped_scanned.append(uploaded_file.name)
                    logging.warning(f"Skipping scanned PDF in batch: {uploaded_file.name}")
                    try:
                        os.remove(temp_path)
                    except Exception:
                        pass
                    continue
            # ──────────────────────────────────────────────────────

            batch_item = {
                'original_name': uploaded_file.name,
                'temp_path': temp_path,
                'state_code': state_code,
                'status': 'pending',
                'result_path': None,
                'error': None,
                'processing_time': None,
                'uploaded_at': datetime.now()
            }
            st.session_state.batch_queue.append(batch_item)
            added_count += 1

    # Report any skipped scanned PDFs to the user
    if skipped_scanned:
        st.warning(
            f"🚫 **{len(skipped_scanned)} scanned PDF(s) skipped** — "
            f"only digital PDFs can be processed:\n\n"
            + "\n".join(f"- {name}" for name in skipped_scanned)
        )

    return added_count

def process_batch_sequentially():
    """Process all files in batch queue sequentially"""
    if not st.session_state.batch_queue:
        return
    
    st.session_state.batch_processing = True
    st.session_state.batch_results = []
    
    total_files = len(st.session_state.batch_queue)
    
    for index, batch_item in enumerate(st.session_state.batch_queue):
        if st.session_state.batch_paused:
            break
            
        st.session_state.batch_current_file = batch_item['original_name']
        st.session_state.batch_progress = index / total_files
        
        # Update status
        batch_item['status'] = 'processing'
        
        try:
            # Process the file
            start_time = time.time()
            
            result = process_single_pdf_simple(
                batch_item['temp_path'],
                batch_item['state_code'],
                st.session_state.session_id
            )
            
            end_time = time.time()
            processing_time = end_time - start_time
            minutes = int(processing_time // 60)
            seconds = int(processing_time % 60)
            time_str = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"
            
            if result:
                batch_item['status'] = 'completed'
                batch_item['result_path'] = result
                batch_item['processing_time'] = time_str
            else:
                batch_item['status'] = 'failed'
                batch_item['error'] = 'Processing failed'
                
        except Exception as e:
            batch_item['status'] = 'failed'
            batch_item['error'] = str(e)
            logging.error(f"Batch processing error for {batch_item['original_name']}: {e}")
        
        # Add to results
        st.session_state.batch_results.append(batch_item.copy())
        
        # Reset individual processing state
        st.session_state.processing = False
        st.session_state.current_step = 0
        st.session_state.step_status = {}
    
    # Final progress update
    st.session_state.batch_progress = 1.0
    st.session_state.batch_processing = False
    st.session_state.batch_current_file = None

def reset_batch_processing():
    """Reset all batch processing state"""
    # Clean up temp files
    for batch_item in st.session_state.batch_queue:
        if batch_item['temp_path'] and os.path.exists(batch_item['temp_path']):
            try:
                os.remove(batch_item['temp_path'])
            except:
                pass
    
    st.session_state.batch_queue = []
    st.session_state.batch_processing = False
    st.session_state.batch_current_file = None
    st.session_state.batch_results = []
    st.session_state.batch_progress = 0
    st.session_state.batch_paused = False
    st.session_state.uploaded_files = []

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

        # Try filename pattern first (fast, no API call)
        detected_state = opus.detect_state_from_filename(pdf_path)
        if detected_state:
            add_log(f"✅ State detected from filename: {detected_state}")
        else:
            # Fall back to Claude visual analysis of first page
            add_log("🔍 Analyzing PDF content to identify state via AI...")
            if status_placeholder:
                status_placeholder.info("🔍 Analyzing PDF content to identify state via AI...")
            detected_state = opus.detect_state_from_content(client, pdf_path)

        # Store and display the detected state
        st.session_state.detected_state = detected_state
        if detected_state:
            _state_name = opus.AVAILABLE_STATES.get(detected_state, detected_state)
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

        # ── Step 4b: Post-process — strip tags from protected clauses ────────────
        if state_config:
            converted_text = opus.post_process_output(converted_text, state_config)
            add_log("🔧 Post-processing: stripped tags from protected clauses (if any)")

        # ── Step 5: Save output ──────────────────────────────────────────────────
        st.session_state.current_step = 4
        st.session_state.step_status[4] = "🔄"
        update_progress()
        add_log("💾 Saving output...")
        if status_placeholder:
            status_placeholder.info("💾 Saving output...")

        output_filename = Path(pdf_path).stem + "_claude_opus.txt"
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

        # ── Step 6: PyMuPDF Extraction ───────────────────────────────────────────
        st.session_state.current_step = 5
        st.session_state.step_status[5] = "🔄"
        update_progress()
        add_log("📄 Running PyMuPDF extraction (JSON report + tagged text + highlighted images)...")
        if status_placeholder:
            status_placeholder.info("📄 Running PyMuPDF extraction...")

        # Reset audit data
        st.session_state.audit_excel_path = None
        st.session_state.audit_highlighted_dir = None
        st.session_state.audit_json_report = None
        st.session_state.content_accuracy_report = None

        try:
            from pymupdf_extractor import (
                extract_pdf_json_report,
                extract_pdf_with_pymupdf,
                highlight_pdf_formatting,
                clean_extracted_text,
                consolidate_adjacent_tags,
            )

            # Determine state-specific marking flags
            _mark_flags = {}
            if state_config:
                add_cfg = state_config.get("add_markup", {})
                del_cfg = state_config.get("delete_markup", {})
                _mark_flags["mark_underline"] = add_cfg.get("underline", True)
                _mark_flags["mark_italic"] = add_cfg.get("italic", False) or del_cfg.get("italic", False)
                _mark_flags["mark_bold"] = add_cfg.get("bold", False)
                _mark_flags["mark_strikethrough"] = del_cfg.get("strike_out", True)
                _mark_flags["mark_brackets"] = add_cfg.get("brackets", False) or del_cfg.get("brackets", False)

            # 6a: JSON report
            pymupdf_report = extract_pdf_json_report(pdf_path, **_mark_flags)
            n_spans = len(pymupdf_report.get("formatted_spans", []))
            add_log(f"📊 PyMuPDF JSON report: {n_spans} formatted spans detected")

            # Save JSON report alongside output
            pymupdf_json_path = output_path.replace(".txt", "_pymupdf_report.json") if isinstance(output_path, str) else str(output_path).replace(".txt", "_pymupdf_report.json")
            import json as _json
            with open(pymupdf_json_path, "w", encoding="utf-8") as jf:
                _json.dump(pymupdf_report, jf, indent=2, ensure_ascii=False)
            add_log(f"✅ JSON report saved: {pymupdf_json_path}")

            # 6b: Tagged text
            pymupdf_text = extract_pdf_with_pymupdf(pdf_path, join_pages=True, **_mark_flags)
            pymupdf_txt_path = output_path.replace(".txt", "_pymupdf.txt") if isinstance(output_path, str) else str(output_path).replace(".txt", "_pymupdf.txt")
            with open(pymupdf_txt_path, "w", encoding="utf-8") as tf:
                tf.write(pymupdf_text)
            add_log(f"✅ PyMuPDF tagged text saved: {len(pymupdf_text):,} chars")

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
                    anthropic_client=client,
                )
                pymupdf_clean_text = consolidate_adjacent_tags(pymupdf_clean_text)
                pymupdf_clean_path = output_path.replace(".txt", "_pymupdf_clean.txt") if isinstance(output_path, str) else str(output_path).replace(".txt", "_pymupdf_clean.txt")
                with open(pymupdf_clean_path, "w", encoding="utf-8") as cf:
                    cf.write(pymupdf_clean_text)
                add_log(f"✅ Clean text saved: {len(pymupdf_text):,} → {len(pymupdf_clean_text):,} chars")
                st.session_state.step_status[6] = "✅"
            except Exception as clean_err:
                add_log(f"⚠️ LLM clean failed: {clean_err}", "warning")
                logging.error(f"LLM clean error: {clean_err}", exc_info=True)
                st.session_state.step_status[6] = "⚠️"
            update_progress()
        else:
            st.session_state.step_status[6] = "⚠️"
            update_progress()

        # ── Content Accuracy Check (between Step 7 and Step 8) ─────────────────
        st.session_state.content_accuracy_report = None
        if pymupdf_clean_text and converted_text and compare_texts:
            try:
                accuracy_report = compare_texts(pymupdf_clean_text, converted_text)
                st.session_state.content_accuracy_report = accuracy_report
                _acc = accuracy_report['accuracy']
                _diffs = accuracy_report['real_diff_count']
                _repos = accuracy_report['repositioned_count']
                _n_trans = accuracy_report.get('transposed_count', 0)
                _n_punct = accuracy_report.get('punctuation_count', 0)
                _n_word = accuracy_report.get('word_diff_count', _diffs)
                _icon = "✅" if _acc >= 99 else "⚠️" if _acc >= 95 else "❌"
                # Build compact breakdown
                _parts = []
                if _n_word:  _parts.append(f"{_n_word} word")
                if _n_trans: _parts.append(f"{_n_trans} transposed")
                if _n_punct: _parts.append(f"{_n_punct} punctuation")
                _brkdn = ", ".join(_parts) if _parts else f"{_diffs} diff(s)"
                add_log(
                    f"{_icon} Content accuracy: {_acc:.1f}% "
                    f"({accuracy_report['matching_words']:,}/{max(accuracy_report['pymupdf_word_count'], accuracy_report['converted_word_count']):,} words — "
                    f"{_brkdn}, {_repos} repositioned)"
                )
            except Exception as acc_err:
                add_log(f"⚠️ Content accuracy check failed: {acc_err}", "warning")
                logging.error(f"Content accuracy error: {acc_err}", exc_info=True)

        # ── Step 8: GPT Audit (comparison Excel) ─────────────────────────────────
        if pymupdf_clean_text and highlight_dir and highlight_dir.exists():
            st.session_state.current_step = 7
            st.session_state.step_status[7] = "🔄"
            update_progress()
            add_log("🤖 Running GPT 5.4 checklist-driven audit...")
            if status_placeholder:
                status_placeholder.info("🤖 Running GPT 5.4 audit (comparing PyMuPDF vs Claude)...")

            try:
                # Import gpt_compare from Opus4.6
                opus46_dir = os.path.normpath(
                    os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'Opus4.6')
                )
                if opus46_dir not in sys.path:
                    sys.path.insert(0, opus46_dir)
                from gpt_compare import gpt_compare_tags

                # Load highlighted images as base64
                import base64 as _b64
                hi_images_b64 = []
                for img_file in sorted(highlight_dir.glob("page_*.png")):
                    hi_images_b64.append(_b64.b64encode(img_file.read_bytes()).decode())
                add_log(f"📷 Loaded {len(hi_images_b64)} highlighted page images for GPT")

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
                state_rules_str = ", ".join(sr_parts) if sr_parts else f"State: {detected_state or 'unknown'}"

                gpt_xlsx = output_path.replace(".txt", "_gpt_comparison.xlsx") if isinstance(output_path, str) else str(output_path).replace(".txt", "_gpt_comparison.xlsx")
                gpt_cmp = gpt_compare_tags(
                    highlighted_images_b64=hi_images_b64,
                    pymupdf_clean_text=pymupdf_clean_text,
                    claude_converted_text=converted_text,
                    state_code=detected_state or "XX",
                    state_rules=state_rules_str,
                    output_xlsx=gpt_xlsx,
                    pymupdf_report=pymupdf_report,
                )

                st.session_state.audit_excel_path = gpt_xlsx
                add_log(
                    f"✅ GPT audit complete — "
                    f"Checklist: {gpt_cmp['total_checklist']} items | "
                    f"✅ Pass: {gpt_cmp['passes']} | "
                    f"⚠️ Warnings: {gpt_cmp['warnings']} | "
                    f"❌ Errors: {gpt_cmp['errors']}"
                )
                if gpt_cmp.get('additional_findings'):
                    add_log(f"🔍 Additional findings: {gpt_cmp['additional_findings']}")
                st.session_state.step_status[7] = "✅"
            except Exception as gpt_err:
                add_log(f"⚠️ GPT audit failed: {gpt_err}", "warning")
                logging.error(f"GPT audit error: {gpt_err}", exc_info=True)
                st.session_state.step_status[7] = "⚠️"
            update_progress()
        else:
            st.session_state.step_status[7] = "⚠️"
            if not pymupdf_clean_text:
                add_log("⚠️ GPT audit skipped — PyMuPDF clean text not available", "warning")
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

def show_simple_ui():
    """Simple UI for single file processing with session management"""
    st.title("📄 Pending Legislative PDF to Text Processor")
    st.markdown("*Convert legislative PDFs with AI-powered text extraction and verification*")
    
    # Session info sidebar
    with st.sidebar:
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
            st.session_state.audit_excel_path = None  # Reset audit data
            st.session_state.audit_highlighted_dir = None
            st.session_state.audit_json_report = None
            st.session_state.content_accuracy_report = None
            
            # Reset batch processing state
            st.session_state.batch_mode = False
            st.session_state.batch_queue = []
            st.session_state.batch_processing = False
            st.session_state.batch_current_file = None
            st.session_state.batch_results = []
            st.session_state.batch_progress = 0
            st.session_state.batch_paused = False
            st.session_state.uploaded_files = []
            
            st.success(f"✅ New session created: {new_session_id[:8]}...")
            st.rerun()

        # Batch mode toggle — tucked in sidebar
        st.markdown("---")
        batch_mode = st.toggle("📦 Batch Mode", value=st.session_state.batch_mode, help="Process multiple files at once")
        if batch_mode != st.session_state.batch_mode:
            st.session_state.batch_mode = batch_mode
            st.rerun()

    # File upload — first thing the user sees after the title
    st.header("📁 Upload PDF File(s)")

    # File uploader - multiple files in batch mode
    uploaded_file = st.file_uploader(
        "Choose PDF file(s) to process",
        type=['pdf'],
        accept_multiple_files=st.session_state.batch_mode,
        key="pdf_uploader",
        help="Upload one or more PDF files"
    )
    
    if uploaded_file is not None:
        # Batch mode - handle multiple files
        if st.session_state.batch_mode:
            if isinstance(uploaded_file, list) and len(uploaded_file) > 0:
                col1, col2 = st.columns([3, 1])
                with col1:
                    st.success(f"✅ {len(uploaded_file)} file(s) selected")
                with col2:
                    if st.button("➕ Add to Queue", type="primary", use_container_width=True):
                        with st.spinner("Adding files to batch queue..."):
                            added = add_files_to_batch(
                                uploaded_file,
                                "AUTO",
                                st.session_state.session_id
                            )
                        if added > 0:
                            st.success(f"✅ Added {added} file(s) to batch queue")
                        st.rerun()
                
                # Show batch processing interface
                show_batch_processing_interface()
            else:
                st.info("👆 Please select files to add to batch queue")
        
        # Single file mode - handle one file
        else:
            # Check file size (500 MB limit)
            MAX_FILE_SIZE = 500 * 1024 * 1024  # 500 MB in bytes
            
            if uploaded_file.size > MAX_FILE_SIZE:
                st.error(f"❌ **File too large!** ({format_file_size(uploaded_file.size)})")
                st.error(f"Maximum allowed size is {format_file_size(MAX_FILE_SIZE)}")
                st.stop()
            
            # Check if this is a new file (prevent re-uploading on every render)
            current_file_id = f"{uploaded_file.name}_{uploaded_file.size}"
            
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
                    st.session_state.audit_highlighted_dir = None
                    st.session_state.audit_json_report = None

                    # Upload file using session manager
                    session_mgr = st.session_state.session_manager
                    
                    with st.spinner(f"Uploading {uploaded_file.name}..."):
                        temp_path = session_mgr.upload_file(
                            st.session_state.session_id,
                            uploaded_file,
                            "AUTO"
                        )
                    
                    if temp_path:
                        # ── Scanned PDF gate ──────────────────────────────────
                        if detect_pdf_type is not None:
                            with st.spinner("🔍 Analyzing PDF type (scanned vs digital)..."):
                                pdf_analysis = detect_pdf_type(temp_path)
                            if pdf_analysis and pdf_analysis['type'] == 'scanned':
                                st.error(
                                    f"🚫 **Scanned PDF Detected — Cannot Process**\n\n"
                                    f"The uploaded file **{uploaded_file.name}** appears to be a "
                                    f"**scanned (image-based) PDF**.\n\n"
                                    f"- **Pages:** {pdf_analysis['total_pages']}\n"
                                    f"- **Image pages:** {pdf_analysis['image_pages']} "
                                    f"({pdf_analysis['image_ratio']}%)\n"
                                    f"- **Avg characters/page:** {pdf_analysis['average_chars_per_page']}\n\n"
                                    f"This application only supports **digital (text-based) PDFs**. "
                                    f"Please upload a digital PDF or run OCR on this file first."
                                )
                                # Clean up the temp file
                                try:
                                    os.remove(temp_path)
                                except Exception:
                                    pass
                                st.session_state.temp_pdf_path = None
                                st.session_state.last_uploaded_file_id = None
                                st.stop()
                        # ──────────────────────────────────────────────────────
                        st.session_state.temp_pdf_path = temp_path
                        st.session_state.last_uploaded_file_id = current_file_id
                        st.success(f"✅ Uploaded: {uploaded_file.name} ({format_file_size(uploaded_file.size)})")
                    else:
                        st.error("❌ Failed to upload file - check logs for details")
                except Exception as e:
                    st.error(f"❌ **File Upload Failed**")
                    st.error(f"Error: {str(e)}")
                    import traceback
                    with st.expander("Error Details"):
                        st.code(traceback.format_exc())
            else:
                # File already uploaded
                if st.session_state.temp_pdf_path:
                    st.success(f"✅ File ready: {uploaded_file.name} ({format_file_size(uploaded_file.size)})")
            
            # Call the single file processing function (has both buttons)
            # Show page count hint if PDF is large
            if st.session_state.temp_pdf_path:
                try:
                    import pypdfium2 as pdfium
                    _pdf = pdfium.PdfDocument(st.session_state.temp_pdf_path)
                    _page_count = len(_pdf)
                    _pdf.close()
                    if _page_count > 15:
                        st.warning(
                            f"⏱️ **Large document detected ({_page_count} pages).** "
                            f"Processing will take a while as each page is visually analyzed by Claude Opus. "
                            f"Please keep this tab open and do not close the browser."
                        )
                except Exception:
                    pass  # Don't block processing if page count fails
            show_single_file_processing()
    else:
        st.info("👆 Please upload a PDF file to begin processing")


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
        _dsval = AVAILABLE_STATES.get(_ds, _ds)
        _dsname = _dsval.get('name', _ds) if isinstance(_dsval, dict) else _dsval
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
    """Show results: compact summary bar + validation review"""
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

    # ── Conversion Complete — styled summary card ──────────────────────────────
    _state_label = ""
    if st.session_state.get('detected_state'):
        _s = st.session_state.detected_state
        _sval = AVAILABLE_STATES.get(_s, _s)
        _sname = _sval.get('name', _s) if isinstance(_sval, dict) else _sval
        _state_label = f"{_s} — {_sname}"

    _conv_time = st.session_state.get('conversion_time') or "—"
    _total_time = st.session_state.processing_time or "—"
    _size_label = format_file_size(file_size)
    _has_audit = bool(st.session_state.get('audit_excel_path'))

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

    # Content accuracy chip
    _acc_chip = ""
    _acc_report = st.session_state.get('content_accuracy_report')
    if _acc_report:
        _acc_val = _acc_report['accuracy']
        if _acc_val >= 99:
            _acc_bg, _acc_fg, _acc_bdr = '#f0fdf4', '#166534', '#bbf7d0'
        elif _acc_val >= 95:
            _acc_bg, _acc_fg, _acc_bdr = '#fefce8', '#854d0e', '#fde68a'
        else:
            _acc_bg, _acc_fg, _acc_bdr = '#fef2f2', '#991b1b', '#fecaca'
        _acc_chip = (
            f'<div style="{_chip} background:{_acc_bg}; color:{_acc_fg}; border:1px solid {_acc_bdr};">'
            f'<span style="font-size:1rem;">📊</span> Accuracy '
            f'<span style="font-weight:800;">{_acc_val:.1f}%</span></div>'
        )

    st.markdown(f"""
    <div style="background:#ffffff; border-radius:14px; padding:24px 28px; margin:8px 0 20px;
                border:1px solid #e2e8f0; box-shadow:0 2px 12px rgba(0,0,0,.05);">
        <!-- Header row -->
        <div style="display:flex; align-items:center; gap:12px; margin-bottom:16px; flex-wrap:wrap;">
            <span style="font-size:1.5rem;">✅</span>
            <span style="font-size:1.2rem; font-weight:700; color:#15803d;">Processing Complete</span>
            {_state_pill}
        </div>
        <!-- Time & size chips -->
        <div style="display:flex; gap:10px; flex-wrap:wrap; margin-bottom:14px;">
            {_conv_chip}
            {_val_chip}
            {_size_chip}
            {_acc_chip}
        </div>
        <!-- Total bar -->
        <div style="background:#f8fafc; border-radius:8px; padding:8px 16px;
                    display:flex; align-items:center; justify-content:space-between;
                    border:1px solid #e2e8f0;">
            <span style="font-size:0.84rem; color:#475569;">⏱️ Total processing time</span>
            <span style="font-size:1rem; font-weight:800; color:#0f172a;">{_total_time}</span>
        </div>
        <p style="margin:12px 0 0; font-size:0.82rem; color:#64748b;">
            Your PDF has been converted to tagged legislative text with Claude Opus 4.6.
            {"The validation report below compares the converted output against the original PDF." if _has_audit else ""}
        </p>
    </div>
    """, unsafe_allow_html=True)

    # ── Processing Activity Log (collapsed) ────────────────────────────────────
    if st.session_state.get('processing_logs'):
        with st.expander("📋 Activity Log", expanded=False):
            st.markdown(_build_activity_feed_html(st.session_state.processing_logs), unsafe_allow_html=True)

    # ── Validation Review — GPT Audit Results ──────────────────────────────────
    if st.session_state.get('audit_excel_path') and render_inline_validation_review:
        render_inline_validation_review(
            excel_path=st.session_state.audit_excel_path,
            converted_txt_path=st.session_state.processed_file_path,
            highlighted_dir=st.session_state.get('audit_highlighted_dir'),
            json_report=st.session_state.get('audit_json_report'),
            accuracy_report=st.session_state.get('content_accuracy_report'),
        )

def show_single_file_processing():
    """Show single file processing interface"""
    # Center the button with columns - wider button in middle
    col1, col2, col3 = st.columns([1, 2, 1])
    
    with col2:
        if not st.session_state.processing:
            # Main process button - centered and prominent
            if st.button("🚀 Process Document", 
                        type="primary", 
                        use_container_width=True,
                        help="Start processing with Claude Opus 4.6 visual conversion"):
                st.session_state.processing = True
                st.session_state.current_step = 0
                st.session_state.processing_time = None
                st.session_state.chunk_stats = None
                st.session_state.process_method = "claude_opus"
                st.rerun()
        else:
            # Show processing status
            st.warning("⏳ **Processing...**\n\nPlease wait while we process your document")
    
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
        # Create a placeholder for dynamic updates
        progress_placeholder = st.empty()
        status_placeholder = st.empty()
        log_container = st.expander("📋 Activity Log", expanded=True)
        log_placeholder = log_container.empty()  # live-updating slot inside the expander
        
        with progress_placeholder.container():
            show_processing_steps()
        
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
                progress_placeholder,
                status_placeholder
            )
        elif st.session_state.process_method == "tag_then_clean":
            result = process_with_tag_then_clean_pipeline(
                st.session_state.temp_pdf_path,
                _state,
                st.session_state.session_id,
                progress_placeholder,
                status_placeholder
            )
        elif st.session_state.process_method == "claude_opus":
            result = process_with_claude_opus(
                st.session_state.temp_pdf_path,
                _state,
                st.session_state.session_id,
                progress_placeholder,
                status_placeholder,
                log_placeholder
            )
        else:
            # Default vision processing
            result = process_single_pdf_simple(
                st.session_state.temp_pdf_path,
                _state,
                st.session_state.session_id,
                progress_placeholder,
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

def show_batch_processing_interface():
    """Show batch processing interface with queue management"""
    if not st.session_state.batch_queue:
        st.info("📦 No files in batch queue. Upload files above to get started.")
        return
    
    st.header("📦 Step 3: Batch Processing")
    
    # Queue overview
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Files in Queue", len(st.session_state.batch_queue))
    with col2:
        pending_count = len([f for f in st.session_state.batch_queue if f['status'] == 'pending'])
        st.metric("Pending", pending_count)
    with col3:
        if st.session_state.batch_results:
            completed_count = len([f for f in st.session_state.batch_results if f['status'] == 'completed'])
            st.metric("Completed", completed_count)
    
    # Control buttons
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        if not st.session_state.batch_processing:
            if st.button("🚀 Start Batch Processing", type="primary", use_container_width=True):
                st.session_state.batch_processing = True
                st.session_state.batch_paused = False
                st.rerun()
        else:
            if not st.session_state.batch_paused:
                if st.button("⏸️ Pause Processing", type="secondary", use_container_width=True):
                    st.session_state.batch_paused = True
                    st.rerun()
            else:
                if st.button("▶️ Resume Processing", type="primary", use_container_width=True):
                    st.session_state.batch_paused = False
                    st.rerun()
    
    with col2:
        if st.session_state.batch_processing:
            if st.button("⏹️ Stop Processing", type="secondary", use_container_width=True):
                st.session_state.batch_processing = False
                st.session_state.batch_paused = False
                st.rerun()
    
    with col3:
        if st.button("🗑️ Clear Queue", use_container_width=True):
            if not st.session_state.batch_processing:
                reset_batch_processing()
                st.rerun()
            else:
                st.error("❌ Stop processing before clearing queue")
    
    with col4:
        if st.session_state.batch_results:
            if st.button("📥 Download All Results", use_container_width=True):
                show_batch_download_options()
    
    # Processing status
    if st.session_state.batch_processing:
        if st.session_state.batch_current_file:
            st.info(f"🔄 Currently processing: {st.session_state.batch_current_file}")
        
        # Progress bar
        if st.session_state.batch_progress > 0:
            progress_text = f"Progress: {int(st.session_state.batch_progress * 100)}%"
            st.progress(st.session_state.batch_progress, text=progress_text)
        
        # Auto-process batch
        process_batch_sequentially()
        st.rerun()
    
    # Show queue details
    show_batch_queue_details()
    
    # Show results if any
    if st.session_state.batch_results:
        show_batch_results()

def show_batch_queue_details():
    """Show detailed batch queue with individual file status"""
    with st.expander(f"📋 Queue Details ({len(st.session_state.batch_queue)} files)", expanded=True):
        for i, batch_item in enumerate(st.session_state.batch_queue):
            col1, col2, col3, col4 = st.columns([3, 2, 2, 1])
            
            with col1:
                st.text(f"{i+1:2d}. {batch_item['original_name']}")
            
            with col2:
                status_icons = {
                    'pending': '⏳ Pending',
                    'processing': '🔄 Processing',
                    'completed': '✅ Completed', 
                    'failed': '❌ Failed'
                }
                st.text(status_icons.get(batch_item['status'], batch_item['status']))
            
            with col3:
                if batch_item['processing_time']:
                    st.text(f"⏱️ {batch_item['processing_time']}")
                elif batch_item['status'] == 'failed' and batch_item['error']:
                    st.text(f"💥 Error")
            
            with col4:
                if batch_item['status'] == 'pending' and not st.session_state.batch_processing:
                    if st.button(f"🗑️", key=f"remove_{i}", help="Remove from queue"):
                        if batch_item['temp_path'] and os.path.exists(batch_item['temp_path']):
                            os.remove(batch_item['temp_path'])
                        st.session_state.batch_queue.pop(i)
                        st.rerun()

def show_batch_results():
    """Show batch processing results summary"""
    st.header("📊 Batch Processing Results")
    
    completed_files = [f for f in st.session_state.batch_results if f['status'] == 'completed']
    failed_files = [f for f in st.session_state.batch_results if f['status'] == 'failed']
    
    # Summary metrics
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Total Processed", len(st.session_state.batch_results))
    with col2:
        st.metric("Successful", len(completed_files), delta=len(completed_files))
    with col3:
        st.metric("Failed", len(failed_files), delta=-len(failed_files) if failed_files else 0)
    with col4:
        if completed_files:
            avg_time = sum(float(f['processing_time'].replace('m', '*60+').replace('s', '')) for f in completed_files if f.get('processing_time')) / len(completed_files)
            avg_minutes = int(avg_time // 60)
            avg_seconds = int(avg_time % 60)
            avg_time_str = f"{avg_minutes}m {avg_seconds}s" if avg_minutes > 0 else f"{avg_seconds}s"
            st.metric("Avg Time", avg_time_str)
    
    # Results table
    with st.expander("📋 Detailed Results", expanded=True):
        for i, result in enumerate(st.session_state.batch_results):
            col1, col2, col3, col4 = st.columns([3, 1, 2, 1])
            
            with col1:
                st.text(f"{i+1:2d}. {result['original_name']}")
            
            with col2:
                if result['status'] == 'completed':
                    st.text("✅ Success")
                else:
                    st.text("❌ Failed")
            
            with col3:
                if result['processing_time']:
                    st.text(f"⏱️ {result['processing_time']}")
                elif result['error']:
                    st.text(f"💥 {result['error'][:30]}...")
            
            with col4:
                if result['status'] == 'completed' and result['result_path']:
                    if os.path.exists(result['result_path']):
                        with open(result['result_path'], 'rb') as f:
                            file_data = f.read()
                        
                        download_name = Path(result['original_name']).stem + "_processed.txt"
                        st.download_button(
                            label="📥",
                            data=file_data,
                            file_name=download_name,
                            mime="text/plain",
                            key=f"download_{i}",
                            help="Download processed file"
                        )

def show_batch_download_options():
    """Show options to download all batch results"""
    completed_files = [f for f in st.session_state.batch_results if f['status'] == 'completed' and f.get('result_path')]
    
    if not completed_files:
        st.warning("⚠️ No completed files to download")
        return
    
    st.subheader("📥 Batch Download Options")
    
    # Option 1: Download all as ZIP
    col1, col2 = st.columns(2)
    
    with col1:
        if st.button("📦 Download All as ZIP", use_container_width=True):
            import zipfile
            import io
            
            zip_buffer = io.BytesIO()
            
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                for result in completed_files:
                    if os.path.exists(result['result_path']):
                        file_name = Path(result['original_name']).stem + "_processed.txt"
                        zip_file.write(result['result_path'], file_name)
            
            zip_buffer.seek(0)
            
            st.download_button(
                label=f"📦 Download ZIP ({len(completed_files)} files)",
                data=zip_buffer.getvalue(),
                file_name=f"batch_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
                mime="application/zip",
                use_container_width=True
            )
    
    with col2:
        st.metric("Files Ready", len(completed_files))
        total_size = sum(os.path.getsize(f['result_path']) for f in completed_files if os.path.exists(f['result_path']))
        st.metric("Total Size", format_file_size(total_size))

# Main app
def main():
    """Enterprise main application with session management"""
    # Setup logging
    setup_simple_logging()

    # Show the enterprise UI with session management
    show_simple_ui()

if __name__ == "__main__":
    main()