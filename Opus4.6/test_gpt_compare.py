"""Quick test: run GPT 5.4 comparison on CT PDF 005164 outputs."""
import base64
import sys
from pathlib import Path
from gpt_compare import gpt_compare_tags

STEM = "2026_2026_7_2_2_005164_0_7_1_20260316_0_converted"

# Load the two text files
with open(f"{STEM}_pymupdf_clean.txt", "r", encoding="utf-8") as f:
    pymupdf_clean = f.read()
with open(f"{STEM[:-10]}_converted.txt", "r", encoding="utf-8") as f:  # strip "_converted"
    converted = f.read()

# Load highlighted images
hi_dir = Path(f"{STEM[:-10]}_highlighted")
images_b64 = []
for img in sorted(hi_dir.glob("page_*.png")):
    images_b64.append(base64.b64encode(img.read_bytes()).decode())

print(f"Loaded {len(images_b64)} highlighted images")
print(f"PyMuPDF clean: {len(pymupdf_clean):,} chars")
print(f"Claude converted: {len(converted):,} chars")
print()

# Run GPT 5.4 comparison
out_xlsx = f"{STEM[:-10]}_gpt_comparison.xlsx"
result = gpt_compare_tags(
    highlighted_images_b64=images_b64,
    pymupdf_clean_text=pymupdf_clean,
    claude_converted_text=converted,
    state_code="CT",
    state_rules="State: Connecticut, Underline=True, Strikethrough=True, Italic=False, Bold=False",
    output_xlsx=out_xlsx,
)

print()
print("=" * 60)
print("GPT 5.4 COMPARISON RESULTS")
print("=" * 60)
print(f"Total comparisons: {result['total_comparisons']}")
print(f"Matched tags:      {result['matched_tags']}")
print(f"PyMuPDF-only:      {result['pymupdf_only_tags']}")
print(f"Claude-only:       {result['claude_only_tags']}")
print(f"Content dropped:   {result['content_dropped']}")
print(f"Content added:     {result['content_added']}")
print(f"Real misses:       {result['real_misses']}")
print(f"Real extras:       {result['real_extras']}")
print(f"Warnings:          {result['warnings']}")
print(f"Dropped text:      {result['dropped_text']}")
print(f"Extra text:        {result['extra_text']}")
print(f"Excel saved:       {result['excel_path']}")
print("=" * 60)
