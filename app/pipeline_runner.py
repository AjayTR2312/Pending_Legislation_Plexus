"""
Pipeline Runner — Automated SFTP Download → Convert → Stage for Review
=======================================================================

Polls the SFTP server for new keying ZIPs, extracts PDFs, converts them
using Claude Opus 4.6 (hybrid mode), and stages output text files for
human validation in the Streamlit app.

Flow:
  1. Connect to SFTP, list /ftp/out for new ZIPs
  2. Download new ZIPs to pending_downloads/
  3. Extract PDFs, detect state from ZIP filename prefix (e.g. NC_, DE_)
  4. Convert each PDF → text using Claude (hybrid mode)
  5. Save converted .txt to session folder: pending_processing/{session_id}/
  6. Human reviews in Streamlit UI
  7. After approval → user clicks "Submit to Keyed" → uploads to /ftp/keyed

Usage:
    # One-shot (download + convert whatever is new):
    python pipeline_runner.py

    # Polling daemon (runs every N minutes):
    python pipeline_runner.py --poll --interval 5

    # Download only (no conversion):
    python pipeline_runner.py --download-only

    # Convert already-downloaded files (skip SFTP):
    python pipeline_runner.py --convert-only
"""

import os
import re
import sys
import json
import time
import shutil
import zipfile
import logging
import argparse
from pathlib import Path
from datetime import datetime
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv not needed in container deployments

# ── Paths ──────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent  # app/ -> project root

import settings  # app/settings.py — folders, limits and credentials come from here


def _get_tmp_root() -> Path:
    """Writable temp root: APP_TMP_DIR > PROJECT_ROOT/tmp > /tmp/pending_legislation."""
    return settings.tmp_root()

TMP_ROOT = _get_tmp_root()
DOWNLOADS_DIR = settings.DOWNLOAD_DEST_DIR
PROCESSING_DIR = TMP_ROOT / "pending_processing"
PROCESSED_LOG = TMP_ROOT / "pending_processing" / ".processed_files.json"


def get_today_folder() -> Path:
    """Return today's date-stamped folder (MMddyyyy) inside pending_processing/."""
    today = datetime.now().strftime("%m%d%Y")
    folder = PROCESSING_DIR / today
    folder.mkdir(parents=True, exist_ok=True)
    return folder

# Add Opus4.6 to path for conversion
OPUS_DIR = PROJECT_ROOT / "Opus4.6"
if str(OPUS_DIR) not in sys.path:
    sys.path.insert(0, str(OPUS_DIR))

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("pipeline")

# ── State detection from ZIP filename ──────────────────────────────────────
# ZIP names like: NC_I8B7124A0659011F19B26ED739A4647E0_keying.zip
# The first 2 chars before _ are the state code
STATE_CODES = {
    "AR", "CA", "CO", "CT", "DC", "DE", "FL", "GA", "HI", "IA", "ID", "IL",
    "IN", "KS", "LA", "ME", "MD", "MN", "NC", "ND", "NE", "NY", "OH", "OK",
    "PA", "SD", "TN", "TX", "VT", "WI", "WY",
}


def detect_state_from_zip(zip_name: str) -> str | None:
    """Extract state code from ZIP filename prefix."""
    prefix = zip_name.split("_")[0].upper()
    return prefix if prefix in STATE_CODES else None


def load_processed_log() -> dict:
    """Load the log of already-processed files."""
    if PROCESSED_LOG.exists():
        with open(PROCESSED_LOG, "r") as f:
            return json.load(f)
    return {"downloaded": [], "converted": []}


def save_processed_log(log_data: dict):
    """Save the processed files log."""
    PROCESSED_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(PROCESSED_LOG, "w") as f:
        json.dump(log_data, f, indent=2)


def download_new_files() -> list[Path]:
    """Download new ZIPs from SFTP. Returns list of new local ZIP paths."""
    from ftp_download import connect_sftp, SFTP_USER, REMOTE_DIR
    import stat as stat_mod

    password = settings.FTP_PASSWORD
    if not password:
        log.error("FTP_PASSWORD not set in environment/.env")
        return []

    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

    transport, sftp = connect_sftp(SFTP_USER, password)
    new_files = []
    try:
        entries = sftp.listdir_attr(REMOTE_DIR)
        files = [e for e in entries if stat_mod.S_ISREG(e.st_mode)]

        for entry in files:
            local_path = DOWNLOADS_DIR / entry.filename
            if local_path.exists():
                continue  # Already downloaded

            remote_path = f"{REMOTE_DIR}/{entry.filename}"
            log.info(f"  Downloading {entry.filename} ...")
            try:
                sftp.get(remote_path, str(local_path))
                new_files.append(local_path)
                log.info(f"  [OK] {entry.filename} ({local_path.stat().st_size:,} bytes)")
            except Exception as e:
                log.error(f"  [FAILED] {entry.filename} - {e}")
                if local_path.exists():
                    local_path.unlink()
    finally:
        sftp.close()
        transport.close()

    log.info(f"Downloaded {len(new_files)} new file(s)")
    return new_files


def extract_zips(zip_paths: list[Path] | None = None) -> list[dict]:
    """Extract PDFs from ZIPs. Returns list of {pdf_path, state, zip_name, session_id}."""
    if zip_paths is None:
        # Process all ZIPs in downloads dir
        zip_paths = sorted(DOWNLOADS_DIR.glob("*.zip"))

    processed_log = load_processed_log()
    already_converted = set(processed_log.get("converted", []))

    extracted = []
    for zip_path in zip_paths:
        if zip_path.name in already_converted:
            continue

        state_code = detect_state_from_zip(zip_path.name)
        if not state_code:
            log.warning(f"  Cannot detect state from: {zip_path.name}, skipping")
            continue

        # Create a date-stamped folder (MMddyyyy) with state subfolder
        date_folder = get_today_folder()
        session_id = f"{state_code}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{zip_path.stem[:20]}"
        session_dir = date_folder / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        # Extract PDFs into input/ subfolder
        input_dir = session_dir / "input"
        input_dir.mkdir(parents=True, exist_ok=True)

        # Extract PDFs
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                pdf_names = [n for n in zf.namelist() if n.lower().endswith(".pdf")]
                for pdf_name in pdf_names:
                    zf.extract(pdf_name, input_dir)
                    extracted.append({
                        "pdf_path": str(input_dir / pdf_name),
                        "state": state_code,
                        "zip_name": zip_path.name,
                        "session_id": session_id,
                        "date_folder": str(date_folder),
                    })
            log.info(f"  Extracted {len(pdf_names)} PDF(s) from {zip_path.name} → {date_folder.name}/{session_id}/")
        except zipfile.BadZipFile:
            log.error(f"  Bad ZIP file: {zip_path.name}")
            continue

    return extracted


def convert_pdf(pdf_path: str, state_code: str, output_dir: Path) -> str | None:
    """Convert a single PDF using Claude Opus 4.6 hybrid mode. Returns output path or None."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "claude_opus_test", str(OPUS_DIR / "Claude_Opus_4.6_Test.py")
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        get_anthropic_client = mod.get_anthropic_client
        convert_pdf_hybrid_text = mod.convert_pdf_hybrid_text
        load_state_template = mod.load_state_template
        deterministic_cleanup = mod.deterministic_cleanup
        post_process_output = mod.post_process_output
    except Exception as e:
        log.error(f"Cannot import conversion engine: {e}")
        return None

    client = get_anthropic_client()
    if not client:
        log.error("Failed to initialize Claude API client")
        return None

    state_config = load_state_template(state_code)
    log.info(f"    Converting {Path(pdf_path).name} (state={state_code}, mode=hybrid)")

    converted_text = convert_pdf_hybrid_text(client, pdf_path, state_config, state_code)
    if not converted_text:
        log.error(f"    Conversion failed for {Path(pdf_path).name}")
        return None

    # Post-process
    converted_text = deterministic_cleanup(converted_text, state_config=state_config)
    if state_config:
        converted_text = post_process_output(converted_text, state_config)

    # ── Final whitespace normalization ─────────────────────────────
    # Collapse double/triple spaces between words to single space
    # but preserve 4+ space gaps (intentional table/column alignment)
    converted_text = re.sub(r'(?<=\S) {2,3}(?=\S)', ' ', converted_text)
    # Rejoin line-break hyphenations: "export- limiting" → "export-limiting"
    converted_text = re.sub(r'(\w)- (\w)', r'\1-\2', converted_text)

    # Save output
    output_dir.mkdir(parents=True, exist_ok=True)
    output_filename = Path(pdf_path).stem + ".txt"
    output_path = output_dir / output_filename

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(converted_text)

    log.info(f"    [OK] {output_filename} ({len(converted_text):,} chars)")

    # ── Run validation checks ─────────────────────────────────────────────
    validate_output(str(output_path), pdf_path, state_code, converted_text)

    return str(output_path)


def validate_output(output_path: str, pdf_path: str, state_code: str, converted_text: str):
    """Run validation checks on a converted file and save reports.

    Matches the Streamlit UI validation flow:
    1. Content Accuracy — word-level precleaned vs final output
    2. Tag Validation — PyMuPDF formatting report vs tagged text (vision audit)

    Reports are saved as JSON/XLSX siblings alongside the output file.
    """
    output = Path(output_path)
    stem = output.stem

    # ── Extract raw PyMuPDF text (needed as precleaned baseline) ───────────
    raw_text = None
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from pymupdf_extractor import extract_text_with_formatting
        result = extract_text_with_formatting(pdf_path)
        raw_text = result.get("text") if isinstance(result, dict) else result
        log.info(f"    [VALIDATE] Raw extraction: {len(raw_text):,} chars")
    except Exception as e:
        log.warning(f"    [VALIDATE] Could not extract raw text: {e}")

    # ── 1. Content Accuracy Check (precleaned vs final) ────────────────────
    try:
        from content_accuracy_checker import check_content_accuracy
        if raw_text and converted_text:
            content_report = check_content_accuracy(raw_text, converted_text)
            c_acc = content_report.get("accuracy", 0)
            c_icon = "✅" if c_acc >= 99 else "⚠️" if c_acc >= 95 else "❌"
            log.info(f"    {c_icon} Content accuracy: {c_acc:.1f}%")

            content_json_path = output.parent / f"{stem}_content_accuracy.json"
            import json as _json
            with open(content_json_path, "w", encoding="utf-8") as f:
                _json.dump(content_report, f, indent=2, ensure_ascii=False, default=str)
            log.info(f"    📊 Content accuracy report: {content_json_path.name}")
    except ImportError:
        log.warning("    [VALIDATE] content_accuracy_checker not available — skipping")
    except Exception as e:
        log.warning(f"    [VALIDATE] Content accuracy check failed: {e}")

    # ── 2. Tag Validation (PyMuPDF report vs tagged text, vision audit) ────
    try:
        from pymupdf_extractor import extract_text_with_formatting, highlight_pdf_formatting
        sys.path.insert(0, str(OPUS_DIR))
        from pymupdf_compare import pymupdf_compare_tags

        # Get the formatting report
        report_result = extract_text_with_formatting(pdf_path, include_report=True)
        pymupdf_report = report_result.get("report") if isinstance(report_result, dict) else None

        if pymupdf_report:
            # Render highlighted images
            highlight_dir = output.parent / f"{Path(pdf_path).stem}_highlighted"
            hi_imgs = highlight_pdf_formatting(pdf_path, report=pymupdf_report, output_dir=highlight_dir)

            import base64
            hi_images_b64 = []
            for img_file in sorted(highlight_dir.glob("page_*.png")):
                hi_images_b64.append(base64.b64encode(img_file.read_bytes()).decode())

            if hi_images_b64:
                audit_xlsx = str(output.parent / f"{stem}_tag_validation.xlsx")
                audit_summary = pymupdf_compare_tags(
                    highlighted_images_b64=hi_images_b64,
                    pymupdf_report=pymupdf_report,
                    pymupdf_clean_text=converted_text,
                    state_code=state_code,
                    state_rules=f"State: {state_code}",
                    output_xlsx=audit_xlsx,
                )
                _passes = audit_summary.get("passes", 0)
                _warns = audit_summary.get("warnings", 0)
                log.info(f"    🔍 Tag validation: {_passes} pass, {_warns} warnings → {Path(audit_xlsx).name}")
    except ImportError as ie:
        log.warning(f"    [VALIDATE] Tag validation modules not available: {ie}")
    except Exception as e:
        log.warning(f"    [VALIDATE] Tag validation failed: {e}")

    # Save a summary flag so the UI knows validation ran
    try:
        import json as _json
        _acc_json = output.parent / f"{stem}_accuracy.json"
        _summary = {"validated": True, "state_code": state_code, "checks": ["content_accuracy", "tag_validation"]}
        with open(_acc_json, "w", encoding="utf-8") as f:
            _json.dump(_summary, f, indent=2)
    except Exception:
        pass


def convert_all(extracted_files: list[dict]) -> list[dict]:
    """Convert all extracted PDFs. Returns list of results with output paths."""
    results = []
    total = len(extracted_files)

    for i, item in enumerate(extracted_files, 1):
        pdf_path = item["pdf_path"]
        state_code = item["state"]
        session_id = item["session_id"]

        log.info(f"[{i}/{total}] Processing {Path(pdf_path).name}")

        # Output goes alongside input in the same session folder
        date_folder = item.get("date_folder", "")
        if date_folder:
            output_dir = Path(date_folder) / session_id / "output"
        else:
            output_dir = PROCESSING_DIR / session_id / "output"
        output_path = convert_pdf(pdf_path, state_code, output_dir)

        result = {**item, "output_path": output_path, "status": "success" if output_path else "failed"}
        results.append(result)

    # Update processed log
    processed_log = load_processed_log()
    converted_zips = {r["zip_name"] for r in results if r["status"] == "success"}
    for zn in converted_zips:
        if zn not in processed_log["converted"]:
            processed_log["converted"].append(zn)
    save_processed_log(processed_log)

    # Summary
    success = sum(1 for r in results if r["status"] == "success")
    failed = total - success
    log.info(f"\n[SUMMARY] Converted: {success} | Failed: {failed}")

    return results


def submit_to_keyed(file_paths: list[str]) -> bool:
    """Upload approved files to SFTP /ftp/keyed directly from their session folders."""
    from ftp_upload import connect_sftp, SFTP_USER, REMOTE_DIR as UPLOAD_REMOTE_DIR
    import paramiko

    # Upload via SFTP
    password = settings.FTP_PASSWORD
    if not password:
        log.error("FTP_PASSWORD not set — cannot upload")
        return False

    transport, sftp = connect_sftp(SFTP_USER, password)
    uploaded = 0
    try:
        for fp in file_paths:
            src = Path(fp)
            if not src.exists():
                continue
            remote_path = f"{UPLOAD_REMOTE_DIR}/{src.name}"
            try:
                sftp.put(str(src), remote_path)
                log.info(f"  [UPLOADED] {src.name}")
                uploaded += 1
            except Exception as e:
                log.error(f"  [FAILED]   {src.name} - {e}")
    finally:
        sftp.close()
        transport.close()

    log.info(f"Uploaded {uploaded}/{len(file_paths)} file(s) to SFTP")
    return uploaded == len(file_paths)


def list_pending_sessions() -> list[dict]:
    """List all pending processing sessions with their converted files.
    Searches both date-stamped folders (MMddyyyy/) and legacy flat sessions."""
    sessions = []
    if not PROCESSING_DIR.exists():
        return sessions

    for entry in sorted(PROCESSING_DIR.iterdir(), reverse=True):
        if not entry.is_dir() or entry.name.startswith("."):
            continue

        # Check if this is a date folder (MMddyyyy) containing session subdirs
        if len(entry.name) == 8 and entry.name.isdigit():
            for session_dir in sorted(entry.iterdir(), reverse=True):
                if not session_dir.is_dir():
                    continue
                _add_session(sessions, session_dir, date_folder=entry.name)
        else:
            # Legacy flat session folder
            _add_session(sessions, entry)

    return sessions


def _add_session(sessions: list, session_dir: Path, date_folder: str = ""):
    """Helper to add a session directory to the sessions list."""
    output_dir = session_dir / "output"
    input_dir = session_dir / "input"
    txt_files = sorted(output_dir.glob("*.txt")) if output_dir.exists() else []
    # PDFs can be in input/ subfolder or directly in session dir (legacy)
    pdf_files = sorted(input_dir.glob("*.pdf")) if input_dir.exists() else sorted(session_dir.glob("*.pdf"))

    if txt_files or pdf_files:
        sessions.append({
            "session_id": session_dir.name,
            "path": str(session_dir),
            "date_folder": date_folder,
            "output_files": [str(f) for f in txt_files],
            "pdf_files": [str(f) for f in pdf_files],
            "input_dir": str(input_dir) if input_dir.exists() else str(session_dir),
            "state": session_dir.name.split("_")[0] if "_" in session_dir.name else "??",
            "created": datetime.fromtimestamp(session_dir.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
        })


def run_pipeline(download: bool = True, convert: bool = True):
    """Run the full pipeline: download → extract → convert."""
    log.info("=" * 60)
    log.info("Pipeline Runner — LCPR Pending Legislation")
    log.info("=" * 60)

    new_zips = []
    if download:
        log.info("\n[STEP 1] Checking SFTP for new files...")
        new_zips = download_new_files()
    else:
        log.info("\n[STEP 1] Skipping download (--convert-only)")

    if convert:
        log.info("\n[STEP 2] Extracting & converting...")
        extracted = extract_zips(new_zips if new_zips else None)

        if not extracted:
            log.info("No new files to convert")
            return []

        log.info(f"\n[STEP 3] Converting {len(extracted)} PDF(s)...")
        results = convert_all(extracted)
        return results
    else:
        log.info("\n[STEP 2] Skipping conversion (--download-only)")
        return []


def poll_loop(interval_minutes: int = 5, download: bool = True, convert: bool = True):
    """Continuously poll for new files."""
    log.info(f"Starting poll loop (every {interval_minutes} min). Press Ctrl+C to stop.")
    while True:
        try:
            run_pipeline(download=download, convert=convert)
        except KeyboardInterrupt:
            log.info("Stopped by user")
            break
        except Exception as e:
            log.error(f"Pipeline error: {e}", exc_info=True)

        log.info(f"Sleeping {interval_minutes} minutes...")
        time.sleep(interval_minutes * 60)


def main():
    parser = argparse.ArgumentParser(description="LCPR Pipeline Runner")
    parser.add_argument("--poll", action="store_true", help="Run as polling daemon")
    parser.add_argument("--interval", type=int, default=settings.PIPELINE_POLL_MINUTES,
                        help=f"Poll interval in minutes (default: {settings.PIPELINE_POLL_MINUTES}, PIPELINE_POLL_MINUTES)")
    parser.add_argument("--download-only", action="store_true", help="Only download, skip conversion")
    parser.add_argument("--convert-only", action="store_true", help="Only convert already-downloaded files")
    parser.add_argument("--list-sessions", action="store_true", help="List pending sessions")
    parser.add_argument("--submit", nargs="+", help="Submit specific file(s) to keyed")
    args = parser.parse_args()

    if args.list_sessions:
        sessions = list_pending_sessions()
        if not sessions:
            print("No pending sessions.")
            return
        for s in sessions:
            print(f"\n  Session: {s['session_id']}")
            print(f"  State:   {s['state']} | Created: {s['created']}")
            print(f"  PDFs:    {len(s['pdf_files'])}")
            print(f"  Outputs: {len(s['output_files'])}")
            for f in s['output_files']:
                print(f"    → {Path(f).name}")
        return

    if args.submit:
        submit_to_keyed(args.submit)
        return

    do_download = not args.convert_only
    do_convert = not args.download_only

    if args.poll:
        poll_loop(interval_minutes=args.interval, download=do_download, convert=do_convert)
    else:
        run_pipeline(download=do_download, convert=do_convert)


if __name__ == "__main__":
    main()
