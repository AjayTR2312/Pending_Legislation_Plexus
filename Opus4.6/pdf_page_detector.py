"""
PDF Page Type Detector
======================

Analyzes PDF files to classify each page as SCANNED or DIGITAL before
running the main conversion script (Claude_Opus_4.6_Test.py).

Detection method:
  - Checks if a page contains a large image covering >50% of the page area.
  - Scanned pages are full-page images (often with an unreliable OCR text layer).
  - Digital pages have native text objects with no large background image.

Uses pypdfium2 (same as the main converter) - no additional dependencies.

Usage:
    python pdf_page_detector.py <pdf_file>
    python pdf_page_detector.py <pdf_file> --threshold 60
    python pdf_page_detector.py <pdf_file> --table

Options:
    --threshold N   Image coverage percentage to classify as scanned (default: 50)
    --table         Output results as a formatted table instead of JSON

Examples:
    python pdf_page_detector.py document.pdf
    python pdf_page_detector.py document.pdf --threshold 40
    python pdf_page_detector.py "path/to/my file.pdf" --table
"""

import sys
import os
import json
import argparse
from pathlib import Path

import pypdfium2 as pdfium


def classify_pdf_pages(pdf_path, threshold=50):
    """
    Classify each page in a PDF as SCANNED or DIGITAL.

    A page is classified as SCANNED if it contains an image covering
    more than `threshold`% of the page area. This catches scanned documents
    that may have an embedded OCR text layer but are fundamentally images.

    Args:
        pdf_path (str): Path to the PDF file.
        threshold (int): Minimum image coverage percentage to classify
                         a page as scanned (default: 50).

    Returns:
        dict: {
            "file": str,
            "total_pages": int,
            "scanned_pages": int,
            "digital_pages": int,
            "threshold": int,
            "pages": [
                {
                    "page": int,
                    "type": "SCANNED" | "DIGITAL",
                    "char_count": int,
                    "image_count": int,
                    "max_image_coverage": float
                },
                ...
            ]
        }
    """
    pdf = pdfium.PdfDocument(pdf_path)
    total = len(pdf)

    results = {
        "file": str(pdf_path),
        "total_pages": total,
        "scanned_pages": 0,
        "digital_pages": 0,
        "threshold": threshold,
        "pages": []
    }

    for i in range(total):
        page = pdf[i]
        pw = page.get_width()
        ph = page.get_height()
        page_area = pw * ph

        # Extract text character count
        textpage = page.get_textpage()
        text = textpage.get_text_range().strip()
        char_count = len(text)

        # Count images and find the largest one relative to page size
        img_count = 0
        max_img_coverage = 0.0

        for obj in page.get_objects():
            if obj.type == 3:  # Image object type
                img_count += 1
                try:
                    left, bottom, right, top = obj.get_pos()
                    img_area = abs(right - left) * abs(top - bottom)
                    coverage = (img_area / page_area * 100) if page_area > 0 else 0
                    if coverage > max_img_coverage:
                        max_img_coverage = coverage
                except Exception:
                    pass

        # Classify page
        if max_img_coverage > threshold:
            page_type = "SCANNED"
            results["scanned_pages"] += 1
        else:
            page_type = "DIGITAL"
            results["digital_pages"] += 1

        results["pages"].append({
            "page": i + 1,
            "type": page_type,
            "char_count": char_count,
            "image_count": img_count,
            "max_image_coverage": round(max_img_coverage, 1)
        })

    pdf.close()
    return results


def print_report(results):
    """
    Print a formatted report of the page classification results.

    Args:
        results (dict): Output from classify_pdf_pages().
    """
    print("=" * 65)
    print("PDF Page Type Detection Report")
    print("=" * 65)
    print(f"File:      {results['file']}")
    print(f"Pages:     {results['total_pages']}")
    print(f"Threshold: {results['threshold']}% image coverage")
    print()

    # Table header
    print(f"{'Page':<6} {'Type':<10} {'Chars':<8} {'Images':<8} {'Max Coverage':<12}")
    print("-" * 50)

    for page in results["pages"]:
        print(
            f"{page['page']:<6} "
            f"{page['type']:<10} "
            f"{page['char_count']:<8} "
            f"{page['image_count']:<8} "
            f"{page['max_image_coverage']}%"
        )

    print("-" * 50)
    print(
        f"Digital: {results['digital_pages']}  |  "
        f"Scanned: {results['scanned_pages']}  |  "
        f"Total: {results['total_pages']}"
    )

    # Summary message
    print()
    if results["scanned_pages"] == 0:
        print("All pages are digital - text extraction should be reliable.")
    elif results["digital_pages"] == 0:
        print("All pages are scanned - vision model required for all pages.")
    else:
        scanned_list = [
            str(p["page"]) for p in results["pages"] if p["type"] == "SCANNED"
        ]
        print(
            f"Mixed PDF detected. Scanned pages: {', '.join(scanned_list)}"
        )
        print("Vision model is needed for scanned pages; digital pages can use text extraction.")

    print("=" * 65)


def main():
    parser = argparse.ArgumentParser(
        description="Detect scanned vs digital pages in a PDF file."
    )
    parser.add_argument("pdf_file", help="Path to the PDF file to analyze")
    parser.add_argument(
        "--threshold",
        type=int,
        default=50,
        help="Image coverage %% to classify as scanned (default: 50)"
    )
    parser.add_argument(
        "--table",
        action="store_true",
        dest="table_output",
        help="Output results as a formatted table instead of JSON"
    )

    args = parser.parse_args()

    # Validate file exists
    if not os.path.exists(args.pdf_file):
        print(f"Error: File not found: {args.pdf_file}", file=sys.stderr)
        sys.exit(1)

    # Run detection
    results = classify_pdf_pages(args.pdf_file, threshold=args.threshold)

    # Output
    if args.table_output:
        print_report(results)
    else:
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
