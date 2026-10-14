"""
SFTP Download Script - LCPR UAT LandingStrips
Downloads files from the remote server via SFTP (paramiko).
"""

import os
import stat
import sys
import getpass
from pathlib import Path

import paramiko

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))
import settings  # every value below is configurable: see `python app/settings.py --show`

SFTP_HOST = settings.SFTP_HOST
SFTP_PORT = settings.SFTP_PORT
SFTP_USER = settings.SFTP_USER
REMOTE_DIR = settings.SFTP_DOWNLOAD_DIR

# Local directory to save downloaded files (DOWNLOAD_DEST_DIR, default tmp/pending_downloads/)
LOCAL_DIR = str(settings.DOWNLOAD_DEST_DIR)


def connect_sftp(username: str, password: str) -> tuple[paramiko.Transport, paramiko.SFTPClient]:
    """Connect to SFTP server and return transport + sftp client."""
    transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
    transport.connect(username=username, password=password)
    sftp = paramiko.SFTPClient.from_transport(transport)
    print(f"[OK] Connected to {SFTP_HOST} (SFTP)")
    return transport, sftp


def download_files(sftp: paramiko.SFTPClient, remote_dir: str, local_dir: str):
    """Download all files from remote_dir to local_dir."""
    local_path = Path(local_dir)
    local_path.mkdir(parents=True, exist_ok=True)

    print(f"[INFO] Listing files in {remote_dir} ...")
    try:
        entries = sftp.listdir_attr(remote_dir)
    except IOError as e:
        print(f"[ERROR] Cannot access remote directory: {remote_dir} - {e}")
        return

    # Filter to regular files only (skip directories)
    files = [entry for entry in entries if stat.S_ISREG(entry.st_mode)]

    if not files:
        print(f"[INFO] No files found in {remote_dir}")
        return

    print(f"[INFO] Found {len(files)} file(s) to download")

    downloaded = 0
    failed = 0
    skipped = 0
    for entry in files:
        filename = entry.filename
        local_filepath = local_path / filename
        if local_filepath.exists():
            print(f"  [SKIPPED]    {filename} (already exists)")
            skipped += 1
            continue

        remote_path = f"{remote_dir}/{filename}"
        print(f"  Downloading {filename} ...")
        try:
            sftp.get(remote_path, str(local_filepath))
            size = local_filepath.stat().st_size
            print(f"  [DOWNLOADED] {filename} ({size:,} bytes)")
            downloaded += 1
        except Exception as e:
            print(f"  [FAILED]     {filename} - {e}")
            if local_filepath.exists():
                local_filepath.unlink()
            failed += 1

    print(f"\n[SUMMARY] Downloaded: {downloaded} | Skipped: {skipped} | Failed: {failed}")


def main():
    password = settings.FTP_PASSWORD or getpass.getpass(f"Password for {SFTP_USER}@{SFTP_HOST}: ")
    password = password.strip()

    transport, sftp = connect_sftp(SFTP_USER, password)
    try:
        download_files(sftp, REMOTE_DIR, LOCAL_DIR)
    finally:
        sftp.close()
        transport.close()
        print("[OK] Disconnected")


if __name__ == "__main__":
    main()
