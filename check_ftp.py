"""
List what is currently sitting in the SFTP directories.

    python check_ftp.py            # both directories
    python check_ftp.py keyed      # output side only
    python check_ftp.py out        # input side only

Read-only: nothing is uploaded, downloaded or deleted.
"""

from __future__ import annotations

import os
import stat
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")
sys.path.insert(0, str(PROJECT_ROOT / "app"))


def listing(sftp, label: str, remote_dir: str) -> None:
    print(f"\n{label}: {remote_dir}")
    try:
        entries = [e for e in sftp.listdir_attr(remote_dir) if stat.S_ISREG(e.st_mode)]
    except IOError as exc:
        print(f"  cannot read: {exc}")
        return

    if not entries:
        print("  (empty)")
        return

    print(f"  {len(entries)} file(s)")
    print("  " + "NAME".ljust(52) + "SIZE".rjust(11) + "   MODIFIED")
    for e in sorted(entries, key=lambda x: -x.st_mtime):
        ts = datetime.fromtimestamp(e.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        print("  " + e.filename[:50].ljust(52) + f"{e.st_size:,}".rjust(11) + f"   {ts}")


def main() -> int:
    password = os.environ.get("FTP_PASSWORD")
    if not password:
        print("FTP_PASSWORD not set — add it to .env")
        return 1

    which = sys.argv[1].lower() if len(sys.argv) > 1 else "both"

    from ftp_download import REMOTE_DIR as OUT_DIR
    from ftp_upload import REMOTE_DIR as KEYED_DIR, SFTP_HOST, SFTP_USER, connect_sftp

    transport, sftp = connect_sftp(SFTP_USER, password)
    try:
        if which in ("both", "out"):
            listing(sftp, "INPUT  (bills waiting)", OUT_DIR)
        if which in ("both", "keyed"):
            listing(sftp, "OUTPUT (submitted to NetScan)", KEYED_DIR)
    finally:
        sftp.close()
        transport.close()
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
