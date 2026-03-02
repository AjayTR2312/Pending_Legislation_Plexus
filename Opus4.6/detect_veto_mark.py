"""
PDF Veto/Strike Mark Detection
================================

Checks all pages of a PDF for diagonal strike/veto marks (LLM-based).
If marks found -> moves PDF to "Manual_Review" folder.

Usage:
    python detect_veto_mark.py <pdf_file_or_folder>

Examples:
    python detect_veto_mark.py document.pdf
    python detect_veto_mark.py "C:\\path\\to\\folder"   (scans all PDFs in folder)
"""

import sys
import os
import io
import json
import ssl
import shutil
import base64
import time
import requests
import pypdfium2 as pdfium
from PIL import Image
from pathlib import Path

# Fix encoding for Windows console
if sys.platform == 'win32':
    import codecs
    sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')
    sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'strict')

# Disable SSL warnings
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
ssl._create_default_https_context = ssl._create_unverified_context

# Configuration
WORKSPACE_ID = "AgentTraininJaH4"
TOKEN_URL = "https://aiplatform.gcs.int.thomsonreuters.com/v1/anthropic/token"
REVIEW_FOLDER_NAME = "Manual_Review"


def get_anthropic_client():
    """Get Anthropic API client with TR platform credentials."""
    import anthropic
    try:
        payload = {"workspace_id": WORKSPACE_ID}
        resp = requests.post(TOKEN_URL, headers=None, json=payload, verify=False)
        credentials = json.loads(resp.content)
        if 'anthropic_api_key' in credentials:
            return anthropic.Anthropic(api_key=credentials["anthropic_api_key"])
        else:
            print("  ERROR: No API key in response")
            return None
    except Exception as e:
        print(f"  ERROR: Failed to get API client: {e}")
        return None


def render_all_pages_to_base64(pdf_path, dpi=200):
    """
    Render ALL pages of a PDF to base64 PNG images.
    Uses lower DPI (200) since we only need to detect marks, not read text.
    Copies file to temp if path has special characters (OneDrive paths).

    Returns:
        tuple: (list of base64 strings, total page count)
    """
    import tempfile
    temp_copy = None
    try:
        # pypdfium2 can struggle with long/special paths - copy to temp
        try:
            pdf = pdfium.PdfDocument(pdf_path)
        except Exception:
            temp_copy = os.path.join(tempfile.gettempdir(), "veto_check_temp.pdf")
            shutil.copy2(pdf_path, temp_copy)
            pdf = pdfium.PdfDocument(temp_copy)

        total = len(pdf)
        scale = dpi / 72

        images = []
        for i in range(total):
            page = pdf[i]
            bitmap = page.render(scale=scale)
            pil_image = bitmap.to_pil()

            buf = io.BytesIO()
            pil_image.save(buf, format="PNG", optimize=True)
            b64_data = base64.b64encode(buf.getvalue()).decode('utf-8')
            images.append(b64_data)

        pdf.close()
        return images, total
    except Exception as e:
        print(f"    ERROR rendering PDF: {e}")
        return None, 0
    finally:
        if temp_copy and os.path.exists(temp_copy):
            try:
                os.remove(temp_copy)
            except Exception:
                pass


def check_batch_for_veto_mark(client, page_images, page_start, total_pages):
    """
    Send a batch of page images to Claude and ask which pages have
    diagonal strike/veto marks.

    Args:
        client: Anthropic client
        page_images: list of base64 PNG strings for this batch
        page_start: 1-based page number of the first image in this batch
        total_pages: total pages in the PDF

    Returns:
        tuple: (flagged_pages: list of int, description: str)
    """
    content = []
    page_labels = []
    for idx, img_b64 in enumerate(page_images):
        page_num = page_start + idx
        page_labels.append(str(page_num))
        content.append({
            "type": "text",
            "text": f"Page {page_num}:"
        })
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": img_b64,
            },
        })

    page_range = f"{page_start}-{page_start + len(page_images) - 1}"
    content.append({
        "type": "text",
        "text": (
            f"These are pages {page_range} of {total_pages} from a legislative bill.\n\n"
            "For EACH page, determine if there is a large diagonal line, slash mark, "
            "X mark, or Z-shaped mark drawn across it. This would be a thick colored "
            "line (often red, but could be any color) going diagonally across the "
            "page content, typically used to indicate a veto or rejection.\n\n"
            "Do NOT confuse this with:\n"
            "- Strikethrough text (horizontal lines through individual words/lines)\n"
            "- Underlines beneath text\n"
            "- Normal formatting marks\n\n"
            "Respond in this exact JSON format only:\n"
            '{"flagged_pages": [list of page numbers that have the mark], '
            '"description": "brief description of what you see"}\n\n'
            "If NO pages have a large diagonal/slash/Z mark, return an empty list: "
            '{"flagged_pages": [], "description": "No veto marks found"}'
        )
    })

    try:
        message = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=300,
            temperature=0,
            messages=[{"role": "user", "content": content}]
        )

        response_text = message.content[0].text.strip()

        # Parse JSON from response (handle markdown code blocks)
        if "```" in response_text:
            response_text = response_text.split("```")[1]
            if response_text.startswith("json"):
                response_text = response_text[4:]
            response_text = response_text.strip()

        result = json.loads(response_text)
        flagged = result.get("flagged_pages", [])
        desc = result.get("description", "")
        return flagged, desc

    except json.JSONDecodeError:
        # Fallback: try to extract page numbers from raw text
        resp_lower = message.content[0].text.lower()
        flagged = []
        for p in range(page_start, page_start + len(page_images)):
            if str(p) in resp_lower and ("mark" in resp_lower or "diagonal" in resp_lower or "line" in resp_lower):
                flagged.append(p)
        return flagged, message.content[0].text.strip()
    except Exception as e:
        print(f"    ERROR in LLM check: {e}")
        return [], str(e)


def move_to_review(pdf_path, review_folder):
    """
    Copy PDF to the Manual_Review folder, then delete original.
    Uses copy+delete instead of move to handle OneDrive file locks.
    """
    os.makedirs(review_folder, exist_ok=True)
    dest = os.path.join(review_folder, os.path.basename(pdf_path))

    # Handle duplicate filenames
    if os.path.exists(dest):
        base, ext = os.path.splitext(os.path.basename(pdf_path))
        counter = 1
        while os.path.exists(dest):
            dest = os.path.join(review_folder, f"{base}_{counter}{ext}")
            counter += 1

    # Copy first, then try to delete original (OneDrive may lock files)
    shutil.copy2(pdf_path, dest)

    # Try to delete original with retries for OneDrive locks
    for attempt in range(3):
        try:
            os.remove(pdf_path)
            break
        except PermissionError:
            if attempt < 2:
                time.sleep(1)
            else:
                print(f"    WARNING: Could not delete original (OneDrive lock). Copy created at: {dest}")

    return dest


def process_pdf(client, pdf_path, review_folder, batch_size=5):
    """
    Check a PDF for veto/diagonal marks on all pages.
    If marks found -> moves to Manual_Review.

    Args:
        client: Anthropic client
        pdf_path: Path to PDF
        review_folder: Folder to move flagged PDFs to
        batch_size: Number of pages per LLM call (default 5)

    Returns:
        tuple: (has_issue: bool, flagged_pages: list, description: str)
    """
    filename = os.path.basename(pdf_path)
    print(f"\n  Checking: {filename}")

    # Render all pages to images
    page_images, total_pages = render_all_pages_to_base64(pdf_path)
    if not page_images:
        print(f"    SKIP - Could not render PDF")
        return False, [], "Could not render"

    print(f"    [Veto Check] {total_pages} pages, checking all in batches of {batch_size}")

    all_flagged = []
    all_descriptions = []
    num_batches = (total_pages + batch_size - 1) // batch_size

    for batch_idx in range(num_batches):
        start_idx = batch_idx * batch_size
        end_idx = min(start_idx + batch_size, total_pages)
        batch_images = page_images[start_idx:end_idx]
        page_start = start_idx + 1  # 1-based

        print(f"    Batch {batch_idx + 1}/{num_batches}: pages {page_start}-{start_idx + len(batch_images)}")

        flagged, desc = check_batch_for_veto_mark(client, batch_images, page_start, total_pages)

        if flagged:
            all_flagged.extend(flagged)
            all_descriptions.append(desc)
            print(f"      VETO MARK on pages: {flagged} - {desc}")
        else:
            print(f"      Clean")

    has_veto = len(all_flagged) > 0

    if has_veto:
        summary = f"Veto mark on pages {all_flagged}. {'; '.join(all_descriptions)}"
        print(f"    RESULT: VETO MARK on {len(all_flagged)} pages")
        dest = move_to_review(pdf_path, review_folder)
        print(f"    MOVED TO: {dest}")
    else:
        summary = "Clean - no veto marks found"
        print(f"    RESULT: Clean - all {total_pages} pages checked")

    return has_veto, all_flagged, summary


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    target = sys.argv[1]

    # Determine if target is a file or folder
    if os.path.isfile(target) and target.lower().endswith('.pdf'):
        pdf_files = [target]
        base_folder = os.path.dirname(target)
    elif os.path.isdir(target):
        pdf_files = [
            os.path.join(target, f)
            for f in os.listdir(target)
            if f.lower().endswith('.pdf')
        ]
        base_folder = target
    else:
        print(f"ERROR: '{target}' is not a valid PDF file or folder")
        sys.exit(1)

    if not pdf_files:
        print("No PDF files found")
        sys.exit(0)

    review_folder = os.path.join(base_folder, REVIEW_FOLDER_NAME)

    print("=" * 60)
    print("PDF PRE-CHECK: Veto Mark Detection")
    print("=" * 60)
    print(f"  PDFs to check: {len(pdf_files)}")
    print(f"  Review folder: {review_folder}")

    # Initialize API client
    print("\n  Initializing Claude API...")
    client = get_anthropic_client()
    if not client:
        print("  FATAL: Could not initialize API client")
        sys.exit(1)
    print("  API client ready")

    # Process each PDF
    start_time = time.time()
    flagged = []
    clean = []

    for pdf_path in sorted(pdf_files):
        has_mark, flagged_pages, desc = process_pdf(client, pdf_path, review_folder)
        if has_mark:
            flagged.append((os.path.basename(pdf_path), flagged_pages, desc))
        else:
            clean.append(os.path.basename(pdf_path))

    elapsed = time.time() - start_time

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Total PDFs checked: {len(pdf_files)}")
    print(f"  Clean:              {len(clean)}")
    print(f"  Flagged (moved):    {len(flagged)}")
    print(f"  Time:               {elapsed:.1f}s")

    if flagged:
        print(f"\n  Flagged PDFs (moved to {REVIEW_FOLDER_NAME}/):")
        for name, pages, desc in flagged:
            print(f"    - {name}")
            print(f"      Pages with marks: {pages}")
            print(f"      Detail: {desc}")

    if clean:
        print(f"\n  Clean PDFs:")
        for name in clean:
            print(f"    - {name}")

    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\nFATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
