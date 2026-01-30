from docx import Document  
from docx.opc.constants import RELATIONSHIP_TYPE as RT
  
def find_visual_vs_internal_strikethrough(docx_path):  
    """  
    Detect text that may appear struck through visually but isn't   
    formatted as strikethrough internally  
    """  
    doc = Document(docx_path)
      
    results = []
      
    # Iterate through all paragraphs  
    for para_idx, paragraph in enumerate(doc.paragraphs):  
        # Check each run (formatted text segment) in the paragraph  
        for run_idx, run in enumerate(paragraph.runs):  
            # Check if strikethrough is applied  
            has_strikethrough = run.font.strike
            
            # Check for revision tracking (deletions appear struck through)
            has_deletion = False
            if run._element.xpath('.//w:del'):
                has_deletion = True
              
            # Get the text content  
            text = run.text
              
            if text.strip():  # Only check non-empty text  
                results.append({  
                    'paragraph': para_idx,  
                    'run': run_idx,  
                    'text': text,  
                    'has_strikethrough': has_strikethrough,
                    'has_deletion': has_deletion,
                    'full_paragraph': paragraph.text  
                })
      
    return results
  
def analyze_strikethrough(docx_path):  
    """  
    Analyze and report on strikethrough formatting  
    """  
    results = find_visual_vs_internal_strikethrough(docx_path)
      
    print("=== Strikethrough Analysis ===\n")
    
    strikethrough_count = 0
    no_strikethrough_count = 0
    deletion_count = 0
      
    for item in results:  
        if item['has_deletion']:
            deletion_count += 1
            print(f"[DEL] TRACK CHANGES DELETION (shows as strikethrough):")  
            print(f"  Text: '{item['text']}'")  
            print(f"  Paragraph {item['paragraph']}, Run {item['run']}")  
            print()
        elif item['has_strikethrough']:  
            strikethrough_count += 1
            print(f"[+] STRIKETHROUGH APPLIED:")  
            print(f"  Text: '{item['text']}'")  
            print(f"  Paragraph {item['paragraph']}, Run {item['run']}")  
            print()  
        else:
            # Only show first 20 to avoid too much output
            if no_strikethrough_count < 20:
                no_strikethrough_count += 1
                print(f"[!] NO STRIKETHROUGH:")  
                print(f"  Text: '{item['text']}'")  
                print(f"  Paragraph {item['paragraph']}, Run {item['run']}")  
                print()
            else:
                no_strikethrough_count += 1
    
    print(f"\n=== SUMMARY ===")
    print(f"Total text runs: {len(results)}")
    print(f"With strikethrough formatting: {strikethrough_count}")
    print(f"With track changes deletion: {deletion_count}")
    print(f"Without strikethrough: {no_strikethrough_count}")
  
# Usage  
docx_path = r"C:\Users\C302602\OneDrive - Thomson Reuters Incorporated\Documents\Pending_Legislation\tmp\sessions\3814cc41-6a87-41d5-afdf-f09f82f657dc\output\Output_Docx\CA\2025_2026_5_3_7_000011_0_7_1_20250130_0.docx"

# Debug: Check if file exists and is valid
import os
import zipfile

print(f"File exists: {os.path.exists(docx_path)}")
print(f"File size: {os.path.getsize(docx_path)} bytes")
print(f"File path: {docx_path}")

# Try to open as ZIP
try:
    with zipfile.ZipFile(docx_path, 'r') as zip_ref:
        print(f"Valid ZIP file with {len(zip_ref.namelist())} entries")
except Exception as e:
    print(f"ERROR: Not a valid ZIP file: {e}")
    exit(1)

analyze_strikethrough(docx_path)  