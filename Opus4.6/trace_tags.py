import json, re

with open("2025_2025_7_3_2_001193_0_4_1_20250130_0_converted_pymupdf_report.json", "r") as f:
    report = json.load(f)

spans = report["formatted_spans"]
headers = [s for s in spans if s.get("bill_line") is None]
content = [s for s in spans if s.get("bill_line") is not None]

print(f"JSON report: {len(headers)} header spans, {len(content)} content spans")
print()

print("=== ALL HEADER SPANS (bill_line=None — filtered OUT of comparison) ===")
for i, s in enumerate(headers):
    print(f"  {i+1}. page={s['page']}, fmt={s.get('format_types')}, text={repr(s['text'][:70])}")
print()

# Check if effective-date table is in headers
print("=== Spans containing 'Section 1' or '16-2' or '16-6' ===")
for s in spans:
    if "Section 1" in s.get("text", "") or "16-2" in s.get("text", "") or "16-6" in s.get("text", ""):
        print(f"  bill_line={s.get('bill_line')}, page={s['page']}, text={repr(s['text'][:70])}")
print()

# Count [A>] tags in each file
with open("2025_2025_7_3_2_001193_0_4_1_20250130_0_converted_pymupdf_clean.txt", "r") as f:
    pymupdf = f.read()
with open("2025_2025_7_3_2_001193_0_4_1_20250130_0_converted.txt", "r") as f:
    claude = f.read()

a_re = re.compile(r"\[A>(.*?)<A\]", re.DOTALL)
py_tags = a_re.findall(pymupdf)
cl_tags = a_re.findall(claude)

print(f"PyMuPDF clean [A>] tags: {len(py_tags)}")
print(f"Claude converted [A>] tags: {len(cl_tags)}")
print()

# Show the ones in PyMuPDF but NOT in the comparison (non-content)
norm = lambda t: " ".join(t.split())
py_norms = [norm(t) for t in py_tags]
# The comparison used 19 content spans; PyMuPDF clean text has more
print(f"=== PyMuPDF [A>] tags NOT in JSON content spans ===")
content_texts = set(norm(s["text"]) for s in content)
for i, t in enumerate(py_norms):
    # Check if this tag's text appears in content spans
    found = any(t in ct or ct in t for ct in content_texts)
    if not found:
        print(f"  #{i+1}: {repr(t[:80])}")
