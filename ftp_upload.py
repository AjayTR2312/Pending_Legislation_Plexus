"""
FTP Upload Script - LCPR UAT LandingStrips
Uploads converted text files to the remote FTP server.
"""

import os
import ftplib
import getpass
from pathlib import Path


FTP_HOST = "uat.ftp.magellan3.int.westgroup.com"
FTP_PORT = 21
FTP_USER = "mag2ftp"
REMOTE_DIR = "/apps/Codes/MP2/LandingStrips/LCPR/ftp/keyed"

# Local directory containing files to upload
LOCAL_DIR = os.environ.get("UPLOAD_SOURCE_DIR", "/home/m6119788/Pending_Legislation/pending_outputs")


def connect_ftp(password: str) -> ftplib.FTP:
    """Connect to FTP server. Attempts TLS first, falls back to plain FTP."""
    # Try TLS first
    try:
        ftp = ftplib.FTP_TLS()
        ftp.connect(FTP_HOST, FTP_PORT, timeout=30)
        ftp.auth()
        ftp.login(FTP_USER, password)
        ftp.prot_p()
        print(f"[OK] Connected to {FTP_HOST} (TLS secured)")
        return ftp
    except ftplib.error_perm as e:
        if "504" in str(e):
            print(f"[INFO] TLS not supported, falling back to plain FTP")
        else:
            raise
    finally:
        try:
            ftp.close()
        except Exception:
            pass

    # Fall back to plain FTP
    ftp = ftplib.FTP()
    ftp.connect(FTP_HOST, FTP_PORT, timeout=30)
    ftp.login(FTP_USER, password)
    print(f"[OK] Connected to {FTP_HOST} (plain FTP - no TLS)")
    return ftp


def ensure_remote_dir(ftp: ftplib.FTP, remote_dir: str):
    """Navigate to remote directory, creating it if needed."""
    dirs = remote_dir.strip("/").split("/")
    for d in dirs:
        try:
            ftp.cwd(d)
        except ftplib.error_perm:
            ftp.mkd(d)
            ftp.cwd(d)


def upload_files(ftp: ftplib.FTP, local_dir: str, remote_dir: str, extensions: tuple = (".txt",)):
    """Upload files from local_dir to remote_dir."""
    ensure_remote_dir(ftp, remote_dir)

    local_path = Path(local_dir)
    if not local_path.exists():
        print(f"[ERROR] Local directory not found: {local_dir}")
        return

    files = [f for f in local_path.iterdir() if f.is_file() and f.suffix.lower() in extensions]

    if not files:
        print(f"[INFO] No matching files found in {local_dir}")
        return

    print(f"[INFO] Found {len(files)} file(s) to upload")

    for filepath in files:
        try:
            with open(filepath, "rb") as f:
                ftp.storbinary(f"STOR {filepath.name}", f)
            print(f"  [UPLOADED] {filepath.name}")
        except ftplib.error_perm as e:
            print(f"  [FAILED]   {filepath.name} - {e}")


def main():
    password = os.environ.get("FTP_PASSWORD") or getpass.getpass(f"Password for {FTP_USER}@{FTP_HOST}: ")
    password = password.strip()

    ftp = connect_ftp(password)
    try:
        upload_files(ftp, LOCAL_DIR, REMOTE_DIR)
    finally:
        ftp.quit()
        print("[OK] Disconnected")


if __name__ == "__main__":
    main()
