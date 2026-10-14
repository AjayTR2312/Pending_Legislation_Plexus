"""
SFTP Upload Script - LCPR UAT LandingStrips
Uploads converted text files to the remote server via SFTP (paramiko).
"""

import os
import stat
import getpass
from pathlib import Path

import paramiko

import settings  # every value below is configurable: see `python app/settings.py --show`

SFTP_HOST = settings.SFTP_HOST
SFTP_PORT = settings.SFTP_PORT
SFTP_USER = settings.SFTP_USER
REMOTE_DIR = settings.SFTP_UPLOAD_DIR

# Local directory the sessions are stored in (UPLOAD_SOURCE_DIR, default tmp/pending_processing/)
LOCAL_DIR = str(settings.UPLOAD_SOURCE_DIR)


def connect_sftp(username: str, password: str) -> tuple[paramiko.Transport, paramiko.SFTPClient]:
    """Connect to SFTP server and return transport + sftp client."""
    transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
    transport.connect(username=username, password=password)
    sftp = paramiko.SFTPClient.from_transport(transport)
    print(f"[OK] Connected to {SFTP_HOST} (SFTP)")
    return transport, sftp


def ensure_remote_dir(sftp: paramiko.SFTPClient, remote_dir: str):
    """Navigate to remote directory, creating it if needed."""
    current = ""
    for d in remote_dir.strip("/").split("/"):
        current += f"/{d}"
        try:
            sftp.stat(current)
        except IOError:
            sftp.mkdir(current)


def upload_files(sftp: paramiko.SFTPClient, local_dir: str, remote_dir: str, extensions: tuple = (".txt",)):
    """Upload output files from session folders to remote_dir.

    Scans local_dir recursively for files in output/ subdirectories
    (e.g. pending_processing/{date}/{session}/output/*.txt).
    """
    ensure_remote_dir(sftp, remote_dir)

    local_path = Path(local_dir)
    if not local_path.exists():
        print(f"[ERROR] Local directory not found: {local_dir}")
        return

    # Collect output files from session folders
    files = []
    for ext in extensions:
        files.extend(local_path.glob(f"**/output/*{ext}"))
    # Deduplicate and sort
    files = sorted(set(files))

    if not files:
        print(f"[INFO] No matching output files found under {local_dir}")
        return

    print(f"[INFO] Found {len(files)} output file(s) to upload")

    uploaded = 0
    failed = 0
    for filepath in files:
        remote_path = f"{remote_dir}/{filepath.name}"
        try:
            sftp.put(str(filepath), remote_path)
            print(f"  [UPLOADED] {filepath.name}")
            uploaded += 1
        except Exception as e:
            print(f"  [FAILED]   {filepath.name} - {e}")
            failed += 1

    print(f"\n[SUMMARY] Uploaded: {uploaded} | Failed: {failed}")


def main():
    password = settings.FTP_PASSWORD or getpass.getpass(f"Password for {SFTP_USER}@{SFTP_HOST}: ")
    password = password.strip()

    transport, sftp = connect_sftp(SFTP_USER, password)
    try:
        upload_files(sftp, LOCAL_DIR, REMOTE_DIR)
    finally:
        sftp.close()
        transport.close()
        print("[OK] Disconnected")


if __name__ == "__main__":
    main()
