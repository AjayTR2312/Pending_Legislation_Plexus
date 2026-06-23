"""
SFTP Upload Script - LCPR UAT LandingStrips
Uploads converted text files to the remote server via SFTP (paramiko).
"""

import os
import stat
import getpass
from pathlib import Path
from dotenv import load_dotenv

import paramiko

load_dotenv()


SFTP_HOST = "10.17.38.130"
SFTP_PORT = 22
SFTP_USER = "mag2ftp"
REMOTE_DIR = "/apps/Codes/MP2/LandingStrips/LCPR/ftp/keyed"

# Local directory containing files to upload
LOCAL_DIR = os.environ.get("UPLOAD_SOURCE_DIR", "/home/m6119788/Pending_Legislation/pending_outputs")


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
    """Upload files from local_dir to remote_dir."""
    ensure_remote_dir(sftp, remote_dir)

    local_path = Path(local_dir)
    if not local_path.exists():
        print(f"[ERROR] Local directory not found: {local_dir}")
        return

    files = [f for f in local_path.iterdir() if f.is_file() and f.suffix.lower() in extensions]

    if not files:
        print(f"[INFO] No matching files found in {local_dir}")
        return

    print(f"[INFO] Found {len(files)} file(s) to upload")

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
    password = os.environ.get("FTP_PASSWORD") or getpass.getpass(f"Password for {SFTP_USER}@{SFTP_HOST}: ")
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
