import os
import uuid
import shutil
import threading
import time
import gc
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import zipfile
import logging
from pathlib import Path

class SessionManager:
    """Enterprise Session Manager for PDF Processing Application"""
    
    def __init__(self, base_sessions_dir: str = None, session_timeout_hours: int = 24):
        if base_sessions_dir is None:
            # Use Temp/sessions as default
            base_sessions_dir = "../Temp/sessions"
        
        # Log where sessions will be created for debugging
        logging.info(f"Session manager initialized with base directory: {os.path.abspath(base_sessions_dir)}")
                
        self.base_sessions_dir = base_sessions_dir
        self.session_timeout = timedelta(hours=session_timeout_hours)
        self.sessions: Dict[str, dict] = {}
        self.lock = threading.Lock()
        
        # Ensure base directory exists
        os.makedirs(base_sessions_dir, exist_ok=True)
        
        # Start cleanup thread
        self._start_cleanup_thread()
    
    def create_session(self) -> str:
        """Create a new session with unique ID and folder structure"""
        session_id = str(uuid.uuid4())
        session_dir = os.path.join(self.base_sessions_dir, session_id)
        
        # Create session folder structure for PDF processing
        session_structure = {
            'input': {},           # Uploaded PDFs go here
            'output': {},          # Processed outputs
            'logs': {}             # Session-specific logs
        }
        
        self._create_folder_structure(session_dir, session_structure)
        
        with self.lock:
            self.sessions[session_id] = {
                'created_at': datetime.now(),
                'last_accessed': datetime.now(),
                'session_dir': session_dir,
                'status': 'active',
                'processing_active': False,
                'uploaded_files': [],
                'processed_files': [],
                'current_state': None,
                'processing_step': 0
            }
        
        logging.info(f"Created new session: {session_id} at {session_dir}")
        return session_id
    
    def get_session_folders(self, session_id: str) -> Optional[Dict[str, str]]:
        """Get folder paths for a session"""
        if not self.is_valid_session(session_id):
            return None
            
        session_dir = self.sessions[session_id]['session_dir']
        return {
            'session_dir': session_dir,
            'input_folder': os.path.join(session_dir, 'input'),
            'output_folder': os.path.join(session_dir, 'output'),
            'logs_folder': os.path.join(session_dir, 'logs')
        }
    
    def is_valid_session(self, session_id: str) -> bool:
        """Check if session exists and is valid"""
        if not session_id or session_id not in self.sessions:
            return False
        
        # Update last accessed time
        with self.lock:
            self.sessions[session_id]['last_accessed'] = datetime.now()
        
        return True
    
    def get_session_info(self, session_id: str) -> Optional[dict]:
        """Get session information"""
        if not self.is_valid_session(session_id):
            return None
        
        with self.lock:
            session = self.sessions[session_id].copy()
            session['session_id'] = session_id
            return session
    
    def upload_file(self, session_id: str, uploaded_file, state_code: str = None) -> Optional[str]:
        """Upload a file to session input folder"""
        if not self.is_valid_session(session_id):
            logging.error(f"Invalid session ID: {session_id}")
            return None
            
        folders = self.get_session_folders(session_id)
        input_folder = folders['input_folder']
        
        try:
            # Save uploaded file
            file_path = os.path.join(input_folder, uploaded_file.name)
            with open(file_path, 'wb') as f:
                f.write(uploaded_file.read())
            
            # Update session info
            with self.lock:
                self.sessions[session_id]['uploaded_files'].append({
                    'filename': uploaded_file.name,
                    'path': file_path,
                    'size': uploaded_file.size,
                    'uploaded_at': datetime.now(),
                    'state_code': state_code
                })
                self.sessions[session_id]['current_state'] = state_code
                self.sessions[session_id]['last_accessed'] = datetime.now()
            
            logging.info(f"Uploaded file: {uploaded_file.name} to session {session_id}")
            return file_path
            
        except Exception as e:
            logging.error(f"Error uploading file {uploaded_file.name} to session {session_id}: {e}")
            return None
    
    def start_processing(self, session_id: str) -> bool:
        """Mark session as processing started"""
        if not self.is_valid_session(session_id):
            return False
        
        with self.lock:
            self.sessions[session_id]['processing_active'] = True
            self.sessions[session_id]['processing_step'] = 0
            self.sessions[session_id]['status'] = 'processing'
            self.sessions[session_id]['last_accessed'] = datetime.now()
        
        logging.info(f"Started processing for session: {session_id}")
        return True
    
    def update_processing_step(self, session_id: str, step: int) -> bool:
        """Update current processing step"""
        if not self.is_valid_session(session_id):
            return False
        
        with self.lock:
            self.sessions[session_id]['processing_step'] = step
            self.sessions[session_id]['last_accessed'] = datetime.now()
        
        return True
    
    def complete_processing(self, session_id: str, output_file_path: str) -> bool:
        """Mark processing as complete and save output file info"""
        if not self.is_valid_session(session_id):
            return False
        
        with self.lock:
            self.sessions[session_id]['processing_active'] = False
            self.sessions[session_id]['status'] = 'completed'
            self.sessions[session_id]['processed_files'].append({
                'path': output_file_path,
                'filename': Path(output_file_path).name,
                'completed_at': datetime.now()
            })
            self.sessions[session_id]['last_accessed'] = datetime.now()
        
        logging.info(f"Completed processing for session: {session_id}")
        return True
    
    def get_output_file_path(self, session_id: str, state_code: str, filename_stem: str) -> str:
        """Get standardized output file path for session"""
        folders = self.get_session_folders(session_id)
        output_folder = folders['output_folder']
        
        # Create state-specific subfolder
        state_output_folder = os.path.join(output_folder, state_code)
        os.makedirs(state_output_folder, exist_ok=True)
        
        return state_output_folder
    
    def cleanup_session(self, session_id: str) -> bool:
        """Clean up session data with retry logic"""
        if session_id not in self.sessions:
            logging.warning(f"Session {session_id} not found for cleanup")
            return False
            
        try:
            session_dir = self.sessions[session_id]['session_dir']
            
            # First remove from memory to prevent new operations
            with self.lock:
                if session_id in self.sessions:
                    del self.sessions[session_id]
            
            # Attempt to remove directory with retry logic
            if os.path.exists(session_dir):
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        # Force garbage collection to close file handles
                        gc.collect()
                        time.sleep(0.1)  # Brief pause
                        
                        # Try to remove the directory
                        shutil.rmtree(session_dir)
                        logging.info(f"Successfully cleaned up session: {session_id}")
                        return True
                        
                    except PermissionError as pe:
                        if attempt < max_retries - 1:
                            logging.warning(f"Cleanup attempt {attempt + 1} failed for session {session_id}: {pe}. Retrying...")
                            time.sleep(1)  # Wait before retry
                        else:
                            # Last attempt failed - mark for delayed cleanup
                            logging.error(f"Failed to cleanup session directory {session_id} after {max_retries} attempts: {pe}")
                            self._mark_for_delayed_cleanup(session_dir)
                            return True  # Return true since session is removed from memory
                    except Exception as e:
                        logging.error(f"Unexpected error during cleanup attempt {attempt + 1}: {e}")
                        if attempt == max_retries - 1:
                            raise
            
            logging.info(f"Cleaned up session: {session_id}")
            return True
            
        except Exception as e:
            logging.error(f"Critical error cleaning up session {session_id}: {e}")
            # Even if directory cleanup fails, remove from memory
            with self.lock:
                if session_id in self.sessions:
                    del self.sessions[session_id]
            return False
    
    def get_active_sessions_count(self) -> int:
        """Get count of active sessions"""
        return len(self.sessions)
    
    def get_session_stats(self) -> Dict[str, int]:
        """Get session statistics"""
        with self.lock:
            stats = {
                'total_sessions': len(self.sessions),
                'active_sessions': len([s for s in self.sessions.values() if s['status'] == 'active']),
                'processing_sessions': len([s for s in self.sessions.values() if s['processing_active']]),
                'completed_sessions': len([s for s in self.sessions.values() if s['status'] == 'completed'])
            }
        return stats
    
    def _create_folder_structure(self, base_path: str, structure: dict):
        """Recursively create folder structure"""
        os.makedirs(base_path, exist_ok=True)
        for name, sub_structure in structure.items():
            folder_path = os.path.join(base_path, name)
            if isinstance(sub_structure, dict):
                self._create_folder_structure(folder_path, sub_structure)
            else:
                os.makedirs(folder_path, exist_ok=True)
    
    def _mark_for_delayed_cleanup(self, session_dir: str):
        """Mark directory for delayed cleanup (Windows file locking workaround)"""
        if not hasattr(self, '_delayed_cleanup_dirs'):
            self._delayed_cleanup_dirs = []
        self._delayed_cleanup_dirs.append(session_dir)
        logging.info(f"Marked directory for delayed cleanup: {session_dir}")
    
    def _cleanup_delayed_directories(self):
        """Attempt to cleanup previously failed directories"""
        if not hasattr(self, '_delayed_cleanup_dirs'):
            return
            
        cleaned_dirs = []
        for session_dir in self._delayed_cleanup_dirs[:]:  # Copy list to avoid modification during iteration
            try:
                if os.path.exists(session_dir):
                    shutil.rmtree(session_dir)
                    logging.info(f"Successfully cleaned up delayed directory: {session_dir}")
                cleaned_dirs.append(session_dir)
            except Exception as e:
                logging.debug(f"Delayed cleanup still failing for {session_dir}: {e}")
        
        # Remove successfully cleaned directories from delayed list
        for cleaned_dir in cleaned_dirs:
            self._delayed_cleanup_dirs.remove(cleaned_dir)
    
    def _start_cleanup_thread(self):
        """Start background thread for session cleanup"""
        def cleanup_expired_sessions():
            while True:
                try:
                    current_time = datetime.now()
                    expired_sessions = []
                    
                    with self.lock:
                        for session_id, session_data in self.sessions.items():
                            if current_time - session_data['last_accessed'] > self.session_timeout:
                                expired_sessions.append(session_id)
                    
                    for session_id in expired_sessions:
                        logging.info(f"Auto-cleaning expired session: {session_id}")
                        self.cleanup_session(session_id)
                    
                    # Also attempt cleanup of delayed directories
                    self._cleanup_delayed_directories()
                    
                    # Sleep for 1 hour before next cleanup check
                    time.sleep(3600)
                    
                except Exception as e:
                    logging.error(f"Error in cleanup thread: {e}")
                    time.sleep(3600)  # Continue after error
        
        cleanup_thread = threading.Thread(target=cleanup_expired_sessions, daemon=True)
        cleanup_thread.start()
        logging.info("Session cleanup thread started")


# Global session manager instance
session_manager = None

def get_session_manager():
    """Get or create global session manager instance"""
    global session_manager
    if session_manager is None:
        session_manager = SessionManager()
    return session_manager