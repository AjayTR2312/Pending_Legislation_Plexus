"""Verify the upload race fix: the mode must be set before a job is claimable.

Run with the worker stopped so no paid conversion is triggered.
"""

from __future__ import annotations

import glob
import json
import sys
import urllib.request
import uuid
from pathlib import Path

import db

API = "http://localhost:8800"


def find_pdf(fragment: str = "") -> Path:
    pattern = str(db.JOBS_DIR / "**" / "*.pdf")
    matches = [p for p in glob.glob(pattern, recursive=True) if fragment in p]
    if not matches:
        raise SystemExit("No PDF found in job storage to replay")
    return Path(matches[0])


def upload(pdf: Path) -> dict:
    boundary = uuid.uuid4().hex
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{pdf.name}"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    body = head + pdf.read_bytes() + tail
    req = urllib.request.Request(
        f"{API}/api/jobs",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.load(resp)


def main() -> int:
    pdf = find_pdf("000299")
    print(f"replaying: {pdf.name}")

    job = upload(pdf)
    print("\nafter upload:")
    for field in ("status", "mode", "mode_reason", "page_count", "display_type"):
        print(f"  {field:<12}: {job.get(field)}")

    ok = True
    if job.get("mode") is None:
        print("\n  [FAIL] mode was not set before the job became visible")
        ok = False
    if job.get("status") not in (db.STATUS_QUEUED, db.STATUS_AWAITING_APPROVAL):
        print(f"\n  [FAIL] unexpected status {job.get('status')!r}")
        ok = False

    pages = job.get("page_count") or 0
    expected = "hybrid" if pages > 15 else "pymupdf_clean_only"
    if job.get("mode") != expected:
        print(f"\n  [FAIL] {pages} pages should route to {expected}, got {job.get('mode')}")
        ok = False

    # Clean up so the queue is not left holding a job for a paid conversion.
    db.set_status(job["id"], db.STATUS_SKIPPED)
    print(f"\n  (test job {job['id']} marked skipped — no conversion will run)")

    if ok:
        print(f"\n  [OK] {pages} pages routed to {job['mode']} before becoming claimable")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
