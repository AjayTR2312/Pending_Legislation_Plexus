import os
import uuid
import shutil
import threading
import time
import gc
import atexit
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import zipfile
import logging
from pathlib import Path

class SessionManager:
    """Enterprise Session Manager for PDF Processing Application"""
    
    def __init__(self, base_sessions_dir: str = None, session_timeout_hours: int = 24):
        if base_sessions_dir is None:
            base_sessions_dir = self._get_sessions_directory()
        
        # Log where sessions will be created for debugging
        logging.info(f"Session manager initialized with base directory: {os.path.abspath(base_sessions_dir)}")
                
        self.base_sessions_dir = base_sessions_dir
        self.session_timeout = timedelta(hours=session_timeout_hours)
        self.sessions: Dict[str, dict] = {}
        self.lock = threading.Lock()
        
        # Ensure base directory exists with proper error handling
        try:
            os.makedirs(base_sessions_dir, exist_ok=True)
            logging.info(f"✓ Session directory created/verified: {base_sessions_dir}")
            
            # Test write permissions immediately.
            # Use a unique name to avoid OneDrive / antivirus lock
            # collisions, and retry the delete in case the sync agent
            # briefly holds the file open (WinError 32).
            test_file = os.path.join(base_sessions_dir,
                                     f".write_test_{os.getpid()}")
            with open(test_file, 'w') as f:
                f.write("test")
            for _attempt in range(5):
                try:
                    os.remove(test_file)
                    break
                except PermissionError:
                    time.sleep(0.2)
            else:
                # Could not delete — not fatal, just warn
                logging.warning(f"Could not remove write-test file "
                                f"{test_file} (OneDrive lock?)")
            logging.info(f"✓ Write test successful in {base_sessions_dir}")
            
        except Exception as e:
            logging.error(f"❌ Failed to create or write to session directory {base_sessions_dir}: {e}")
            logging.error(f"Error type: {type(e).__name__}")
            raise Exception(f"Session directory not writable: {base_sessions_dir}") from e
        
        # NOTE: We no longer delete session directories on shutdown.
        # Session files (input PDFs, output text, accuracy reports) are
        # valuable artifacts the user may need after the app stops.
        # Stale sessions are still cleaned by the background thread
        # (_start_cleanup_thread) based on expiry age.
        # atexit.register(self._cleanup_all_sessions)  # disabled
        
        # Start cleanup thread
        self._start_cleanup_thread()
    
    def _get_sessions_directory(self) -> str:
        """Get sessions temp directory with fallback to writable directories"""
        import tempfile
        import platform
        
        # Try environment variable first
        env_temp_dir = os.getenv('SESSIONS_TEMP_DIR')
        if env_temp_dir:
            temp_dir = Path(env_temp_dir)
            try:
                if temp_dir.exists() or temp_dir.parent.exists():
                    temp_dir.mkdir(parents=True, exist_ok=True)
                    # Test write permission
                    test_file = temp_dir / '.test_write'
                    test_file.touch()
                    test_file.unlink()
                    return str(temp_dir)
            except (PermissionError, OSError) as e:
                logging.warning(f"Cannot write to {temp_dir}: {e}, using fallback")
        
        # Primary: Use project directory tmp/sessions
        try:
            # Get the project root (parent of 'app' directory)
            current_file = Path(__file__).resolve()
            project_root = current_file.parent.parent  # Go up from app/ to project root
            if os.environ.get('APP_TMP_DIR'):
                temp_dir = Path(os.environ['APP_TMP_DIR']) / 'sessions'
            else:
                temp_dir = project_root / 'tmp' / 'sessions'
            temp_dir.mkdir(parents=True, exist_ok=True)
            
            # Test write permission
            test_file = temp_dir / '.test_write'
            test_file.touch()
            test_file.unlink()
            
            logging.info(f"Using project directory: {temp_dir}")
            return str(temp_dir)
        except (PermissionError, OSError) as e:
            logging.warning(f"Cannot write to project tmp directory: {e}, using fallback")
        
        # Fallback 1: Use system temp directory (works on Windows, Linux, Mac)
        try:
            system_temp = Path(tempfile.gettempdir())
            temp_dir = system_temp / 'pending_legislation_sessions'
            temp_dir.mkdir(parents=True, exist_ok=True)
            
            # Test write permission
            test_file = temp_dir / '.test_write'
            test_file.touch()
            test_file.unlink()
            
            logging.info(f"Using system temp directory: {temp_dir}")
            return str(temp_dir)
        except (PermissionError, OSError) as e:
            logging.warning(f"Cannot write to system temp {temp_dir}: {e}")
        
        # Fallback 2: Try /tmp/pending_legislation_sessions (Unix/Linux/Mac systems)
        if platform.system() != 'Windows':
            try:
                temp_dir = Path('/tmp/pending_legislation_sessions')
                temp_dir.mkdir(parents=True, exist_ok=True)
                temp_dir.chmod(0o777)
                # Test write permission
                test_file = temp_dir / '.test_write'
                test_file.touch()
                test_file.unlink()
                return str(temp_dir)
            except (PermissionError, OSError) as e:
                logging.warning(f"Cannot write to /tmp/pending_legislation_sessions: {e}")
        
        # Last resort - use absolute path in current working directory
        try:
            temp_dir = Path(os.getcwd()) / 'tmp' / 'sessions'
            temp_dir.mkdir(parents=True, exist_ok=True)
            logging.warning(f"Using fallback directory in current path: {temp_dir}")
            return str(temp_dir)
        except (PermissionError, OSError) as e:
            # Absolute last resort - use current directory
            logging.error(f"Cannot create temp directory: {e}, using current directory")
            return os.path.abspath(os.path.join(os.getcwd(), 'tmp', 'sessions'))
    
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
        
        try:
            self._create_folder_structure(session_dir, session_structure)
            logging.info(f"Created session folder structure at: {session_dir}")
        except Exception as e:
            logging.error(f"Failed to create session folder structure: {e}")
            raise Exception(f"Cannot create session directory at {session_dir}: {str(e)}")
        
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
            # Log upload attempt
            logging.info(f"Attempting to upload {uploaded_file.name} ({uploaded_file.size} bytes) to {input_folder}")
            
            # Verify folder exists and is writable
            if not os.path.exists(input_folder):
                logging.error(f"Input folder does not exist: {input_folder}")
                os.makedirs(input_folder, exist_ok=True)
                logging.info(f"Created input folder: {input_folder}")
            
            # Save uploaded file
            file_path = os.path.join(input_folder, uploaded_file.name)
            logging.info(f"Writing file to: {file_path}")
            
            with open(file_path, 'wb') as f:
                f.write(uploaded_file.read())
            
            logging.info(f"File written successfully, size: {os.path.getsize(file_path)} bytes")
            
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
            logging.error(f"Error type: {type(e).__name__}")
            import traceback
            logging.error(f"Traceback: {traceback.format_exc()}")
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
    
    def delete_session(self, session_id: str) -> bool:
        """Immediately delete a session (called when user clicks New Session)"""
        return self.cleanup_session(session_id)
    
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
    
    def _cleanup_all_sessions(self):
        """Clean up all sessions on shutdown"""
        logging.info("Cleaning up all sessions on shutdown...")
        session_ids = list(self.sessions.keys())
        for session_id in session_ids:
            try:
                self.cleanup_session(session_id)
            except Exception as e:
                logging.warning(f"Failed to cleanup session {session_id} during shutdown: {e}")

    # ── Persistent session archive ──────────────────────────────────────────

    def _get_persist_dir(self) -> Path:
        """Return the root persistent-processing directory."""
        current_file = Path(__file__).resolve()
        project_root = current_file.parent.parent
        if os.environ.get("APP_TMP_DIR"):
            return Path(os.environ["APP_TMP_DIR"]) / 'pending_processing'
        candidate = project_root / 'tmp' / 'pending_processing'
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except OSError:
            fallback = Path('/tmp/pending_legislation/pending_processing')
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback

    def _get_today_folder(self) -> Path:
        """Return today's date-stamped folder (MMddyyyy) inside pending_processing/."""
        from datetime import datetime as _dt
        today = _dt.now().strftime("%m%d%Y")
        folder = self._get_persist_dir() / today
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def persist_session(self, pdf_path: str, output_path: str, state_code: str = None) -> Optional[str]:
        """Archive a processed session's input + output to pending_processing/{date}/{name}/.

        Returns the persisted session directory path, or None on failure.
        """
        try:
            _state = state_code or 'UNK'
            _ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            _pdf_stem = Path(pdf_path).stem
            _session_name = f"{_state}_{_ts}_{_pdf_stem}"
            _pp_dir = self._get_today_folder() / _session_name
            _pp_input = _pp_dir / "input"
            _pp_output = _pp_dir / "output"
            _pp_input.mkdir(parents=True, exist_ok=True)
            _pp_output.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pdf_path, _pp_input / Path(pdf_path).name)
            shutil.copy2(output_path, _pp_output / Path(output_path).name)
            logging.info(f"[SessionManager] Persisted session: {_pp_dir}")
            return str(_pp_dir)
        except Exception as e:
            logging.warning(f"[SessionManager] Could not persist session: {e}")
            return None

    def list_persisted_sessions(self) -> List[dict]:
        """List all persisted sessions from pending_processing/, newest first.

        Returns list of dicts:
            {date_label, name, path, state, n_pdfs, n_outputs, output_files}
        """
        persist_dir = self._get_persist_dir()
        if not persist_dir.exists():
            return []

        results = []
        for entry in sorted(persist_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            # Date-stamped folder (MMddyyyy)
            if len(entry.name) == 8 and entry.name.isdigit():
                for sub in sorted(entry.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
                    if sub.is_dir():
                        results.append(self._describe_persisted(entry.name, sub))
            else:
                # Legacy flat session
                results.append(self._describe_persisted("", entry))
        return results

    @staticmethod
    def _describe_persisted(date_label: str, session_path: Path) -> dict:
        out_dir = session_path / "output"
        in_dir = session_path / "input"
        output_files = sorted(out_dir.glob("*.txt")) if out_dir.exists() else []
        n_pdfs = len(list(in_dir.glob("*.pdf"))) if in_dir.exists() else len(list(session_path.glob("*.pdf")))
        state = session_path.name.split("_")[0]
        return {
            "date_label": date_label,
            "name": session_path.name,
            "path": session_path,
            "state": state,
            "n_pdfs": n_pdfs,
            "n_outputs": len(output_files),
            "output_files": output_files,
        }

    def write_upload_log(self, message: str):
        """Append an upload log entry to today's date folder."""
        _log_file = self._get_today_folder() / "upload_log.txt"
        _ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(_log_file, "a", encoding="utf-8") as f:
            f.write(f"[{_ts}] {message}\n")


# Global session manager instance
session_manager = None

def get_session_manager():
    """Get or create global session manager instance"""
    global session_manager
    if session_manager is None:
        session_manager = SessionManager()
    return session_manager
