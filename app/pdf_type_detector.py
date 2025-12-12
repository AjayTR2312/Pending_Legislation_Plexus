"""
PDF Type Detector - Determines if a PDF is scanned (image-based) or digital (text-based)

This script analyzes a PDF to detect:
- Digital/Normal PDF: Contains extractable text
- Scanned PDF: Contains only images (requires OCR)
- Hybrid PDF: Contains both text and images
"""

import os
import sys
from PyPDF2 import PdfReader
from pathlib import Path
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)


def detect_pdf_type(pdf_path, min_text_threshold=50):
    """
    Detect if a PDF is scanned (image-based) or digital (text-based).
    Also detects signatures, handwritten text, and embedded images/objects.
    
    Args:
        pdf_path (str): Path to the PDF file
        min_text_threshold (int): Minimum characters per page to consider it text-based (default: 50)
        
    Returns:
        dict: {
            'type': 'digital' | 'scanned' | 'hybrid' | 'empty',
            'total_pages': int,
            'text_pages': int,
            'image_pages': int,
            'pages_with_images': int,
            'total_images': int,
            'average_chars_per_page': float,
            'has_signatures': bool,
            'has_handwriting': bool,
            'has_embedded_images': bool,
            'details': list of per-page info
        }
    """
    try:
        if not os.path.exists(pdf_path):
            logging.error(f"File not found: {pdf_path}")
            return None
        
        reader = PdfReader(pdf_path)
        total_pages = len(reader.pages)
        
        logging.info(f"Analyzing PDF: {pdf_path}")
        logging.info(f"Total pages: {total_pages}")
        
        page_details = []
        text_pages = 0
        image_pages = 0
        total_chars = 0
        total_images = 0
        pages_with_images = 0
        
        # Detection flags for special content
        has_signatures = False
        has_handwriting = False
        has_embedded_images = False
        
        # Analyze each page
        for page_num, page in enumerate(reader.pages, start=1):
            # Extract text from page
            text = page.extract_text() or ""
            char_count = len(text.strip())
            text_lower = text.lower()
            
            # Check for images and analyze them
            has_images = False
            image_count = 0
            large_images = 0  # Images that might be scanned pages
            small_images = 0  # Images that might be signatures/logos
            
            if '/Resources' in page and '/XObject' in page['/Resources']:
                try:
                    xobjects = page['/Resources']['/XObject'].get_object()
                    for obj_name in xobjects:
                        try:
                            obj = xobjects[obj_name]
                            if obj['/Subtype'] == '/Image':
                                has_images = True
                                has_embedded_images = True
                                image_count += 1
                                
                                # Try to get image dimensions to classify type
                                try:
                                    width = obj.get('/Width', 0)
                                    height = obj.get('/Height', 0)
                                    
                                    # Large images (likely scanned pages): > 500px in both dimensions
                                    if width > 500 and height > 500:
                                        large_images += 1
                                    # Small images (likely signatures, logos, diagrams): < 500px
                                    elif width > 0 and height > 0:
                                        small_images += 1
                                        
                                        # Likely signature if small and horizontal
                                        if width > height and width < 800 and height < 200:
                                            has_signatures = True
                                            
                                except:
                                    pass
                        except:
                            continue
                except:
                    pass
            
            if has_images:
                pages_with_images += 1
                total_images += image_count
            
            # Detect potential handwriting/signatures from text patterns
            signature_keywords = [
                'signature', 'signed', 'signed by', 'electronically signed',
                '/s/', 'digitally signed', 'authorized signature', 'signed this'
            ]
            
            handwriting_indicators = [
                'handwritten', 'written by hand', 'manuscript',
                'original signature', 'ink signature'
            ]
            
            for keyword in signature_keywords:
                if keyword in text_lower:
                    has_signatures = True
                    break
            
            for indicator in handwriting_indicators:
                if indicator in text_lower:
                    has_handwriting = True
                    break
            
            # Determine page type
            has_text = char_count >= min_text_threshold
            
            # If page has large images, it's likely a scanned page
            if large_images > 0:
                image_pages += 1
                page_type = "image"
            elif has_text:
                text_pages += 1
                page_type = "text"
            elif has_images:
                # Has only small images (signatures/logos) but little text
                image_pages += 1
                page_type = "image"
            else:
                page_type = "empty"
            
            total_chars += char_count
            
            page_details.append({
                'page': page_num,
                'type': page_type,
                'char_count': char_count,
                'has_images': has_images,
                'image_count': image_count,
                'large_images': large_images,
                'small_images': small_images,
                'has_text': has_text
            })
            
            logging.debug(f"Page {page_num}: {page_type} ({char_count} chars, "
                         f"images: {image_count} [{large_images}L/{small_images}S])")
        
        # Calculate statistics
        avg_chars = total_chars / total_pages if total_pages > 0 else 0
        text_ratio = text_pages / total_pages if total_pages > 0 else 0
        image_ratio = image_pages / total_pages if total_pages > 0 else 0
        
        # Determine overall PDF type
        if text_ratio >= 0.8:
            # 80%+ pages have text - it's a digital PDF
            pdf_type = "digital"
        elif image_ratio >= 0.8:
            # 80%+ pages are images - it's a scanned PDF
            pdf_type = "scanned"
        elif text_pages > 0 and image_pages > 0:
            # Mix of text and image pages - hybrid
            pdf_type = "hybrid"
        elif total_chars < (total_pages * 10):
            # Very little text overall - likely scanned
            pdf_type = "scanned"
        else:
            # Unclear - default to empty
            pdf_type = "empty"
        
        result = {
            'type': pdf_type,
            'total_pages': total_pages,
            'text_pages': text_pages,
            'image_pages': image_pages,
            'pages_with_images': pages_with_images,
            'total_images': total_images,
            'average_chars_per_page': round(avg_chars, 2),
            'text_ratio': round(text_ratio * 100, 1),
            'image_ratio': round(image_ratio * 100, 1),
            'total_characters': total_chars,
            'has_signatures': has_signatures,
            'has_handwriting': has_handwriting,
            'has_embedded_images': has_embedded_images,
            'details': page_details
        }
        
        return result
        
    except Exception as e:
        logging.error(f"Error analyzing PDF: {e}")
        return None


def print_pdf_analysis(result):
    """
    Print a formatted analysis of the PDF type detection.
    
    Args:
        result (dict): Result from detect_pdf_type()
    """
    if not result:
        print("❌ Failed to analyze PDF")
        return
    
    print("\n" + "="*60)
    print("📄 PDF TYPE ANALYSIS")
    print("="*60)
    
    # Determine emoji and message
    type_info = {
        'digital': ('📝', 'Digital/Normal PDF', 'Contains extractable text'),
        'scanned': ('📸', 'Scanned PDF', 'Image-based, may need OCR'),
        'hybrid': ('📋', 'Hybrid PDF', 'Contains both text and images'),
        'empty': ('📄', 'Empty PDF', 'No significant content detected')
    }
    
    emoji, type_name, description = type_info.get(result['type'], ('❓', 'Unknown', 'Type unclear'))
    
    print(f"\n{emoji} Type: {type_name}")
    print(f"   {description}")
    print(f"\n📊 Statistics:")
    print(f"   Total pages: {result['total_pages']}")
    print(f"   Text pages: {result['text_pages']} ({result['text_ratio']}%)")
    print(f"   Image pages: {result['image_pages']} ({result['image_ratio']}%)")
    print(f"   Pages with images: {result['pages_with_images']}")
    print(f"   Total images found: {result['total_images']}")
    print(f"   Avg chars/page: {result['average_chars_per_page']:.0f}")
    print(f"   Total characters: {result['total_characters']:,}")
    
    print(f"\n🔍 Content Analysis:")
    print(f"   Embedded images: {'Yes ✓' if result['has_embedded_images'] else 'No'}")
    print(f"   Signatures detected: {'Yes ✓' if result['has_signatures'] else 'No'}")
    print(f"   Handwriting detected: {'Yes ✓' if result['has_handwriting'] else 'No'}")
    
    # Recommendation
    print(f"\n💡 Recommendation:")
    if result['type'] == 'digital':
        if result['has_signatures'] or result['has_embedded_images']:
            print("   ✅ Ready for text extraction and processing")
            if result['has_signatures']:
                print("   ℹ️ Contains signatures (may be digital or image-based)")
            if result['has_embedded_images']:
                print(f"   ℹ️ Contains {result['total_images']} embedded image(s) (logos, diagrams, etc.)")
        else:
            print("   ✅ Ready for text extraction and processing")
    elif result['type'] == 'scanned':
        print("   ⚠️ OCR (Optical Character Recognition) required")
        print("   Consider using Adobe PDF Services or Tesseract OCR")
        if result['has_signatures']:
            print("   ⚠️ Contains signatures that may need special handling")
    elif result['type'] == 'hybrid':
        print("   ℹ️ Some pages may need OCR, others can be processed directly")
        if result['has_signatures']:
            print("   ℹ️ Contains signatures (mix of digital and image-based)")
    else:
        print("   ❓ Manual inspection recommended")
    
    print("="*60 + "\n")


def analyze_pdf_file(pdf_path, show_details=False):
    """
    Main function to analyze a single PDF file.
    
    Args:
        pdf_path (str): Path to the PDF file
        show_details (bool): If True, show per-page details
        
    Returns:
        dict: Analysis result
    """
    result = detect_pdf_type(pdf_path)
    
    if result:
        print_pdf_analysis(result)
        
        if show_details and result['details']:
            print("📋 Page-by-Page Details:")
            print("-" * 60)
            for page_info in result['details']:
                page_type_emoji = {
                    'text': '📝',
                    'image': '📸',
                    'empty': '📄'
                }.get(page_info['type'], '❓')
                
                # Build image info string
                image_info = "no"
                if page_info['has_images']:
                    if page_info['large_images'] > 0 and page_info['small_images'] > 0:
                        image_info = f"{page_info['image_count']} ({page_info['large_images']}L/{page_info['small_images']}S)"
                    elif page_info['large_images'] > 0:
                        image_info = f"{page_info['image_count']} large"
                    elif page_info['small_images'] > 0:
                        image_info = f"{page_info['image_count']} small"
                    else:
                        image_info = f"{page_info['image_count']}"
                
                print(f"   Page {page_info['page']:3d}: {page_type_emoji} {page_info['type']:6s} "
                      f"({page_info['char_count']:5d} chars, "
                      f"images: {image_info})")
            print("-" * 60 + "\n")
    
    return result


def analyze_folder(folder_path, show_details=False, max_files=None):
    """
    Analyze all PDF files in a folder.
    
    Args:
        folder_path (str): Path to folder containing PDFs
        show_details (bool): If True, show per-page details for each PDF
        max_files (int): Maximum number of files to analyze (None = all files)
    """
    folder = Path(folder_path)
    
    if not folder.exists():
        print(f"❌ Folder not found: {folder_path}")
        return
    
    pdf_files = list(folder.glob("*.pdf"))
    
    if not pdf_files:
        print(f"❌ No PDF files found in: {folder_path}")
        return
    
    # Limit number of files if specified
    if max_files and len(pdf_files) > max_files:
        print(f"ℹ️ Limiting analysis to first {max_files} of {len(pdf_files)} PDF files")
        pdf_files = pdf_files[:max_files]
    
    print(f"\n🔍 Analyzing {len(pdf_files)} PDF file(s) in: {folder_path}\n")
    
    results = []
    for pdf_file in pdf_files:
        print(f"\n{'='*60}")
        print(f"📁 File: {pdf_file.name}")
        print(f"{'='*60}")
        
        result = analyze_pdf_file(str(pdf_file), show_details=show_details)
        if result:
            result['filename'] = pdf_file.name
            results.append(result)
    
    # Summary
    if results:
        print("\n" + "="*60)
        print("📊 FOLDER SUMMARY")
        print("="*60)
        
        digital_count = sum(1 for r in results if r['type'] == 'digital')
        scanned_count = sum(1 for r in results if r['type'] == 'scanned')
        hybrid_count = sum(1 for r in results if r['type'] == 'hybrid')
        empty_count = sum(1 for r in results if r['type'] == 'empty')
        
        print(f"\n   Total files: {len(results)}")
        print(f"   📝 Digital: {digital_count}")
        print(f"   📸 Scanned: {scanned_count}")
        print(f"   📋 Hybrid: {hybrid_count}")
        print(f"   📄 Empty: {empty_count}")
        print("="*60 + "\n")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Detect if PDF files are scanned (image-based) or digital (text-based)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Analyze a single PDF
  python pdf_type_detector.py document.pdf
  
  # Analyze with detailed page info
  python pdf_type_detector.py document.pdf --details
  
  # Analyze all PDFs in a folder
  python pdf_type_detector.py C:\\path\\to\\folder --folder
  
  # Analyze first 10 PDFs in a folder
  python pdf_type_detector.py C:\\path\\to\\folder --folder --limit 10
  
  # Analyze folder with details
  python pdf_type_detector.py C:\\path\\to\\folder --folder --details
        """
    )
    
    parser.add_argument('path', help='Path to PDF file or folder')
    parser.add_argument('--folder', '-f', action='store_true', 
                       help='Analyze all PDFs in the specified folder')
    parser.add_argument('--details', '-d', action='store_true',
                       help='Show page-by-page details')
    parser.add_argument('--limit', '-l', type=int, default=None,
                       help='Maximum number of files to analyze (use with --folder)')
    
    args = parser.parse_args()
    
    if args.folder:
        analyze_folder(args.path, show_details=args.details, max_files=args.limit)
    else:
        analyze_pdf_file(args.path, show_details=args.details)
