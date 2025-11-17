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
    process_pdf_with_claude, save_to_text_file, 
    AVAILABLE_STATES, read_file_content, verify_text_with_llm,
    save_verified_file, create_verification_report
)
from pdf_docx_analysis_pipeline import PDFToDocxAnalysisPipeline
from session_manager import get_session_manager

# Configure Streamlit page
st.set_page_config(
    page_title="Pending Legislation Processor",
    page_icon="📄",
    layout="centered",
    initial_sidebar_state="collapsed"
)

# Initialize session state with SessionManager
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

# Get session folders
session_mgr = st.session_state.session_manager
session_folders = session_mgr.get_session_folders(st.session_state.session_id)

# Setup basic logging with session-specific folder
LOG_FOLDER = session_folders['logs_folder']
os.makedirs(LOG_FOLDER, exist_ok=True)

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
        
        # Step 1: PDF to Text
        st.session_state.current_step = 0
        st.session_state.step_status[0] = "🔄"
        session_mgr.update_processing_step(session_id, 0)
        update_progress()  # Update UI
        time.sleep(0.5)  # Brief pause for UI update
        
        conversion_prompt = load_conversion_prompt(state_code)
        examples = load_example_pairs(state_code)
        
        formatted_text = process_pdf_with_claude(
            st.session_state.client, pdf_path, examples, conversion_prompt
        )
        
        if not formatted_text:
            st.session_state.step_status[0] = "❌"
            return None
            
        # Save text file in session output folder
        output_filename = Path(pdf_path).stem + ".txt"
        text_output_path = os.path.join(output_folder, output_filename)
        
        if not save_to_text_file(formatted_text, text_output_path):
            st.session_state.step_status[0] = "❌"
            return None
            
        st.session_state.step_status[0] = "✅"
        time.sleep(0.1)  # Brief pause for UI update
        update_progress()  # Update UI
        
        # Step 2-4: DOCX Pipeline (combined)
        for step in [1, 2, 3]:
            st.session_state.current_step = step
            st.session_state.step_status[step] = "🔄"
            session_mgr.update_processing_step(session_id, step)
            update_progress()  # Update UI
            time.sleep(0.3)  # Brief pause for UI update
        
        # Use session output folder for pipeline
        pipeline = PDFToDocxAnalysisPipeline(pdf_path, session_folders['output_folder'], state_code=state_code)
        docx_results = pipeline.run_complete_pipeline()
        
        if not docx_results or not docx_results['success']:
            for step in [1, 2, 3]:
                st.session_state.step_status[step] = "❌"
            return None
            
        # Mark steps 2-4 as complete
        for step in [1, 2, 3]:
            st.session_state.step_status[step] = "✅"
        time.sleep(0.1)  # Brief pause for UI update
        update_progress()  # Update UI
        
        # Step 5: LLM Verification
        st.session_state.current_step = 4
        st.session_state.step_status[4] = "🔄"
        session_mgr.update_processing_step(session_id, 4)
        update_progress()  # Update UI
        time.sleep(0.5)  # Brief pause for UI update
        
        corrected_text = verify_text_with_llm(
            text_output_path, docx_results['summary_path'], 
            state_code, st.session_state.client
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
            # Create new session
            new_session_id = st.session_state.session_manager.create_session()
            # Cleanup old session if needed
            old_session_id = st.session_state.session_id
            st.session_state.session_id = new_session_id
            # Reset all session state
            st.session_state.processing = False
            st.session_state.current_step = 0
            st.session_state.step_status = {}
            st.session_state.processed_file_path = None
            st.session_state.temp_pdf_path = None
            st.rerun()
    
    # State selection
    st.header("🏛️ Step 1: Select State")
    
    state_options = [f"{code} - {info['name']}" for code, info in AVAILABLE_STATES.items()]
    selected_state_option = st.selectbox(
        "Choose the state for processing:",
        state_options,
        index=list(AVAILABLE_STATES.keys()).index(st.session_state.selected_state),
        key="state_selector"
    )
    
    st.session_state.selected_state = selected_state_option.split(" - ")[0]
    
    # File upload
    st.header("📁 Step 2: Upload PDF File")
    
    uploaded_file = st.file_uploader(
        "Choose a PDF file to process",
        type=['pdf'],
        accept_multiple_files=False,
        key="pdf_uploader"
    )
    
    if uploaded_file is not None:
        # Upload file using session manager
        session_mgr = st.session_state.session_manager
        temp_path = session_mgr.upload_file(
            st.session_state.session_id, 
            uploaded_file, 
            st.session_state.selected_state
        )
        
        if temp_path:
            st.session_state.temp_pdf_path = temp_path
            st.success(f"✅ Uploaded: {uploaded_file.name} ({format_file_size(uploaded_file.size)})")
        else:
            st.error("❌ Failed to upload file")
            return
        
        # Processing section
        st.header("🔄 Step 3: Process File")
        
        col1, col2 = st.columns([1, 1])
        
        with col1:
            if not st.session_state.processing:
                if st.button("🚀 Start Processing", type="primary", use_container_width=True):
                    st.session_state.processing = True
                    st.session_state.current_step = 0
                    st.session_state.processing_time = None  # Reset processing time
                    st.rerun()
            else:
                if st.button("⏹️ Stop Processing", type="secondary", use_container_width=True):
                    st.session_state.processing = False
                    st.session_state.current_step = 0
                    st.rerun()
        
        with col2:
            if st.button("🗑️ Clear & Reset", use_container_width=True):
                # Reset everything
                st.session_state.processing = False
                st.session_state.current_step = 0
                st.session_state.step_status = {}
                st.session_state.processed_file_path = None
                st.session_state.processing_time = None  # Reset processing time
                if st.session_state.temp_pdf_path and os.path.exists(st.session_state.temp_pdf_path):
                    os.remove(st.session_state.temp_pdf_path)
                st.session_state.temp_pdf_path = None
                st.rerun()
        
        # Processing progress
        if st.session_state.processing:
            # Create a placeholder for dynamic updates
            progress_placeholder = st.empty()
            status_placeholder = st.empty()
            
            with progress_placeholder.container():
                show_processing_steps()
            
            # Auto-process with real-time updates
            result = process_single_pdf_simple(
                st.session_state.temp_pdf_path, 
                st.session_state.selected_state,
                st.session_state.session_id,
                progress_placeholder,  # Pass placeholder for updates
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

# Main app
def main():
    """Enterprise main application with session management"""
    # Setup logging
    setup_simple_logging()
    
    # Show the enterprise UI with session management
    show_simple_ui()

if __name__ == "__main__":
    main()