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
from pdf_to_text_llm import (
    get_anthropic_client, load_conversion_prompt, load_example_pairs,
    process_pdf_with_claude, save_to_text_file, remove_unwanted_tags, 
    AVAILABLE_STATES, read_file_content, verify_text_with_llm,
    save_verified_file, create_verification_report
)
from pdf_docx_analysis_pipeline import PDFToDocxAnalysisPipeline
from session_manager import get_session_manager
from pdf_type_detector import detect_pdf_type
from pdf_plumber_extractor import extract_pdf_with_pdfplumber
from text_to_clean_prompt import convert_pdfplumber_to_clean
from tag_then_clean_pipeline import tag_then_clean_pipeline

# Configure Streamlit page
st.set_page_config(
    page_title="Pending Legislation Processor",
    page_icon="🤖",
    layout="centered",
    initial_sidebar_state="collapsed"
)

# Initialize session state with SessionManager with error handling
try:
    if 'session_id' not in st.session_state:
        session_mgr = get_session_manager()
        st.session_state.session_id = session_mgr.create_session()
        st.session_state.session_manager = session_mgr

    if 'selected_state' not in st.session_state:
        st.session_state.selected_state = "FL"
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
    if 'client' not in st.session_state:
        st.session_state.client = None
    if 'temp_pdf_path' not in st.session_state:
        st.session_state.temp_pdf_path = None
    if 'processing_time' not in st.session_state:
        st.session_state.processing_time = None
    if 'process_method' not in st.session_state:
        st.session_state.process_method = "vision"  # default: "vision" or "pdfplumber"
    if 'chunk_stats' not in st.session_state:
        st.session_state.chunk_stats = None
    
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

# Maximum allowed pages for processing
MAX_PAGES_ALLOWED = 15

def validate_pdf_not_scanned(pdf_path, filename):
    """
    Check if PDF is valid for processing:
    1. Not scanned (must be digital/native PDF)
    2. Not exceeding page limit (max 15 pages)
    Returns: (is_valid, error_message)
    """
    try:
        result = detect_pdf_type(pdf_path)
        
        if result:
            total_pages = result.get('total_pages', 0)
            pdf_type = result.get('type', 'unknown')
            high_dpi_images = result.get('high_dpi_images', 0)
            large_images = result.get('large_images', 0)
            total_images = result.get('total_images', 0)
            avg_chars = result.get('average_chars_per_page', 0)
            text_ratio = result.get('text_ratio', 0)
            image_ratio = result.get('image_ratio', 0)
            
            # Log detailed detection results
            logging.info(f"PDF Detection: type={pdf_type}, pages={total_pages}, large_imgs={large_images}/{total_images}, high_dpi={high_dpi_images}, avg_chars={avg_chars:.1f}, text={text_ratio}%, image={image_ratio}%")
            
            # Check 1: Page count validation (CRITICAL - must be first)
            if total_pages > MAX_PAGES_ALLOWED:
                error_msg = f"**{filename}** — Document exceeds maximum page limit ({total_pages} pages > {MAX_PAGES_ALLOWED} pages allowed). Please split into smaller documents."
                logging.warning(f"File rejected: {filename} has {total_pages} pages (limit: {MAX_PAGES_ALLOWED})")
                return False, error_msg.strip()
            
            # Check 2: Scanned PDF validation
            if pdf_type == 'scanned':
                error_msg = f"**{filename}** — Scanned document detected. This file format cannot be processed due to technical limitations in text extraction."
                logging.warning(f"File rejected: {filename} is a scanned PDF (type: {pdf_type})")
                return False, error_msg.strip()
            
            # Check 3: Hybrid PDF validation (mixed digital/scanned content)
            if pdf_type == 'hybrid':
                error_msg = f"**{filename}** — Hybrid document detected (contains signatures or scanned content). This file format cannot be processed due to technical limitations."
                logging.warning(f"File rejected: {filename} is a hybrid PDF (type: {pdf_type})")
                return False, error_msg.strip()
        
        # Digital or empty PDFs with acceptable page count are OK
        return True, None
        
    except Exception as e:
        # If detection fails, allow processing but log the error
        logging.warning(f"PDF type detection failed for {filename}: {str(e)}")
        return True, None

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
    """Add uploaded files to batch processing queue"""
    session_mgr = st.session_state.session_manager
    added_count = 0
    skipped_files = []
    hybrid_warnings = []
    
    for uploaded_file in uploaded_files:
        # Upload file using session manager
        temp_path = session_mgr.upload_file(session_id, uploaded_file, state_code)
        
        if temp_path:
            # Validate PDF (page count and scanned check)
            is_valid, message = validate_pdf_not_scanned(temp_path, uploaded_file.name)
            
            if not is_valid:
                # Invalid PDF (scanned or too many pages) - skip and clean up
                skipped_files.append((uploaded_file.name, message))
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                continue
            elif message:
                # Hybrid PDF - add to queue but track warning
                hybrid_warnings.append((uploaded_file.name, message))
            
            # Add to batch queue
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
    
    # Show summary messages
    if skipped_files:
        st.error(f"**{len(skipped_files)} file(s) skipped** — Files are either too large or incompatible format (scanned documents).")
        
        # Show detailed error for each skipped file
        with st.expander("📋 View Details", expanded=False):
            for name, error_msg in skipped_files:
                st.markdown(f"• {error_msg}")
    
    if hybrid_warnings:
        st.warning(f"**{len(hybrid_warnings)} file(s) contain mixed content** — Processing will continue with reduced accuracy on scanned pages.")
    
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
def process_single_pdf_simple(pdf_path, state_code, session_id, progress_placeholder=None, status_placeholder=None):
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
            from tag_then_clean_pipeline import tag_then_clean_with_pdfplumber_chunks, get_anthropic_client
            
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
            st.session_state.selected_state = "FL"
            st.session_state.uploaded_file = None
            st.session_state.processing = False
            st.session_state.current_step = 0
            st.session_state.step_status = {}
            st.session_state.processed_file_path = None
            st.session_state.client = None
            st.session_state.temp_pdf_path = None
            st.session_state.processing_time = None
            st.session_state.chunk_stats = None
            st.session_state.last_uploaded_file_id = None  # Reset upload tracking
            
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
    
    # Processing mode toggle
    col1, col2 = st.columns([3, 1])
    with col1:
        st.header("🏛️ Step 1: Select State & Mode")
    with col2:
        batch_mode = st.toggle("📦 Batch Mode", value=st.session_state.batch_mode, help="Process multiple files at once")
        if batch_mode != st.session_state.batch_mode:
            st.session_state.batch_mode = batch_mode
            st.rerun()
    
    # State selection
    state_options = [f"{code} - {info['name']}" for code, info in AVAILABLE_STATES.items()]
    selected_state_option = st.selectbox(
        "Choose the state for processing:",
        state_options,
        index=list(AVAILABLE_STATES.keys()).index(st.session_state.selected_state),
        key="state_selector"
    )
    
    st.session_state.selected_state = selected_state_option.split(" - ")[0]
    
    # File upload
    st.header("📁 Step 2: Upload PDF File(s)")
    
    # Display file size limit warning
    if st.session_state.batch_mode:
        st.info("📊 **Batch Mode**: Upload multiple files (max 15 pages each)")
    else:
        st.info("📊 **Single File Mode**: Upload one file (max 15 pages)")
    
    # File uploader - multiple files in batch mode
    uploaded_file = st.file_uploader(
        "Choose PDF file(s) to process",
        type=['pdf'],
        accept_multiple_files=st.session_state.batch_mode,
        key="pdf_uploader",
        help="Upload PDF file(s) (max 15 pages each)"
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
                                st.session_state.selected_state,
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
                    # Upload file using session manager
                    session_mgr = st.session_state.session_manager
                    
                    with st.spinner(f"Uploading {uploaded_file.name}..."):
                        temp_path = session_mgr.upload_file(
                            st.session_state.session_id, 
                            uploaded_file, 
                            st.session_state.selected_state
                        )
                    
                    if temp_path:
                        # Validate PDF is not scanned
                        is_valid, message = validate_pdf_not_scanned(temp_path, uploaded_file.name)
                        
                        if not is_valid:
                            # Scanned PDF detected - reject and clean up
                            st.error(message)
                            # Remove the uploaded file
                            if os.path.exists(temp_path):
                                os.remove(temp_path)
                            st.session_state.temp_pdf_path = None
                            st.stop()
                        elif message:
                            # Hybrid PDF warning
                            st.warning(message)
                        
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
            show_single_file_processing()
    else:
        st.info("👆 Please upload a PDF file to begin processing")

def show_processing_steps():
    """Show processing steps with progress bar"""
    st.subheader("📊 Processing Progress")
    
    steps = [
        {"name": "PDF to Text", "icon": "📄"},
        {"name": "PDF to DOCX", "icon": "📋"}, 
        {"name": "DOCX Analysis", "icon": "🔍"},
        {"name": "Summary Generation", "icon": "📊"},
        {"name": "LLM Verification", "icon": "🤖"}
    ]
    
    # Calculate progress
    completed_steps = sum(1 for status in st.session_state.step_status.values() if status == "✅")
    total_steps = len(steps)
    progress_percentage = completed_steps / total_steps
    
    # Show progress bar
    st.progress(progress_percentage, text=f"Completed {completed_steps} of {total_steps} steps")
    
    # Show current step status
    current_step = st.session_state.current_step
    if current_step < len(steps):
        current_status = st.session_state.step_status.get(current_step, "⏳")
        step_info = steps[current_step]
        
        if current_status == "🔄":
            st.info(f"🔄 Currently processing: {step_info['icon']} {step_info['name']}")
        elif current_status == "✅":
            if completed_steps == total_steps:
                st.success("🎉 All steps completed successfully!")
            else:
                st.success(f"✅ Completed: {step_info['icon']} {step_info['name']}")
        elif current_status == "❌":
            st.error(f"❌ Failed: {step_info['icon']} {step_info['name']}")
    
    # Show step list in a more compact format
    with st.expander("� View Step Details", expanded=False):
        for i, step in enumerate(steps):
            status = st.session_state.step_status.get(i, "⏳")
            if status == "✅":
                st.markdown(f"✅ **{step['icon']} {step['name']}** - Completed")
            elif status == "❌":
                st.markdown(f"❌ **{step['icon']} {step['name']}** - Failed")
            elif status == "🔄":
                st.markdown(f"🔄 **{step['icon']} {step['name']}** - In Progress")
            else:
                st.markdown(f"⏳ **{step['icon']} {step['name']}** - Pending")

def show_results_section():
    """Show results and download options"""
    st.header("🎉 Processing Complete!")
    
    file_name = Path(st.session_state.processed_file_path).name
    file_size = os.path.getsize(st.session_state.processed_file_path)
    
    # Show success message with processing time
    success_msg = "✅ Your document has been successfully processed with AI verification and legislative tagging!"
    if st.session_state.processing_time:
        success_msg += f" ⏱️ Processing time: {st.session_state.processing_time}"
    
    st.success(success_msg)
    
    # Show chunk processing stats if available (for chunked documents)
    if hasattr(st.session_state, 'chunk_stats') and st.session_state.chunk_stats:
        chunk_info = st.session_state.chunk_stats
        st.info(
            f"📊 **Chunked Processing Details:** Processed {chunk_info['chunks_processed']}/{chunk_info['total_chunks']} chunks | "
            f"Final output: {chunk_info['character_count']:,} characters"
        )
    
    col1, col2 = st.columns([2, 1])
    
    with col1:
        st.subheader("📄 Final Output Preview")
        
        try:
            with open(st.session_state.processed_file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # Show highlighted version
            highlighted_content = highlight_legislative_tags(content)
            st.markdown(highlighted_content, unsafe_allow_html=True)
            
        except Exception as e:
            st.error(f"Error loading file: {e}")
    
    with col2:
        st.subheader("📥 Download")
        
        st.metric("File Size", format_file_size(file_size))
        if st.session_state.processing_time:
            st.metric("Processing Time", st.session_state.processing_time)
        
        # Show chunk stats in metrics if available
        if hasattr(st.session_state, 'chunk_stats') and st.session_state.chunk_stats:
            chunk_info = st.session_state.chunk_stats
            st.metric("Chunks Processed", f"{chunk_info['chunks_processed']}/{chunk_info['total_chunks']}")
            st.metric("Character Count", f"{chunk_info['character_count']:,}")
        
        # Download button
        try:
            with open(st.session_state.processed_file_path, 'rb') as f:
                file_data = f.read()
            
            st.download_button(
                label="📥 Download Final Output",
                data=file_data,
                file_name=file_name,
                mime="text/plain",
                use_container_width=True
            )
        except Exception as e:
            st.error(f"Download error: {e}")

def show_single_file_processing():
    """Show single file processing interface"""
    st.header("🔄 Step 3: Process File")
    
    col1, col2, col3 = st.columns([1, 1, 1])
    
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
    
    with col2:
        if not st.session_state.processing:
            # Custom CSS for red button
            st.markdown("""
                <style>
                div[data-testid="stButton"] > button[kind="primary"] {
                    background-color: #dc3545;
                    border-color: #dc3545;
                }
                div[data-testid="stButton"] > button[kind="primary"]:hover {
                    background-color: #c82333;
                    border-color: #bd2130;
                }
                </style>
            """, unsafe_allow_html=True)
            if st.button("🚀 Process", type="primary", use_container_width=True, 
                        help="Reliable extraction with state-specific cleaning"):
                st.session_state.processing = True
                st.session_state.current_step = 0
                st.session_state.processing_time = None
                st.session_state.chunk_stats = None
                st.session_state.process_method = "pdfplumber"
                st.rerun()
        else:
            if st.button("🗑️ Clear & Reset", use_container_width=True):
                # Reset everything
                st.session_state.processing = False
                st.session_state.current_step = 0
                st.session_state.step_status = {}
                st.session_state.processed_file_path = None
                st.session_state.processing_time = None
                st.session_state.chunk_stats = None
                if st.session_state.temp_pdf_path and os.path.exists(st.session_state.temp_pdf_path):
                    os.remove(st.session_state.temp_pdf_path)
                st.session_state.temp_pdf_path = None
                st.rerun()
    
    # with col3:
    #     if not st.session_state.processing:
    #         if st.button("🏷️ Tag-Then-Clean", use_container_width=True,
    #                     help="Tag raw text first, then clean (most accurate)"):
    #             st.session_state.processing = True
    #             st.session_state.current_step = 0
    #             st.session_state.processing_time = None
    #             st.session_state.chunk_stats = None
    #             st.session_state.process_method = "tag_then_clean"
    #             st.rerun()
    
    # Processing progress
    if st.session_state.processing:
        # Create a placeholder for dynamic updates
        progress_placeholder = st.empty()
        status_placeholder = st.empty()
        
        with progress_placeholder.container():
            show_processing_steps()
        
        # Choose processing method
        if st.session_state.process_method == "pdfplumber":
            result = process_with_pdfplumber_pipeline(
                st.session_state.temp_pdf_path, 
                st.session_state.selected_state,
                st.session_state.session_id,
                progress_placeholder,
                status_placeholder
            )
        elif st.session_state.process_method == "tag_then_clean":
            result = process_with_tag_then_clean_pipeline(
                st.session_state.temp_pdf_path,
                st.session_state.selected_state,
                st.session_state.session_id,
                progress_placeholder,
                status_placeholder
            )
        else:
            # Default vision processing
            result = process_single_pdf_simple(
                st.session_state.temp_pdf_path, 
                st.session_state.selected_state,
                st.session_state.session_id,
                progress_placeholder,
                status_placeholder
            )
        
        if result:
            st.session_state.processed_file_path = result
            st.session_state.processing = False
            success_msg = "🎉 Processing completed successfully!"
            if st.session_state.processing_time:
                success_msg += f" ⏱️ Total time: {st.session_state.processing_time}"
            st.success(success_msg)
        else:
            st.session_state.processing = False
            st.error("❌ Processing failed!")
        
        st.rerun()
    
    # Results section
    if st.session_state.processed_file_path and os.path.exists(st.session_state.processed_file_path):
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