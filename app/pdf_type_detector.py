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
            high_dpi_images = 0  # Images with DPI > 150 (likely scanned)
            
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
                                
                                # Try to get image dimensions and DPI to classify type
                                try:
                                    width = obj.get('/Width', 0)
                                    height = obj.get('/Height', 0)
                                    
                                    # Calculate DPI if possible
                                    # DPI = pixels / inches
                                    # PDF uses points (1/72 inch), so we need page dimensions
                                    dpi = None
                                    coverage_ratio = 0  # How much of the page does this image cover?
                                    try:
                                        # Get page dimensions in points
                                        page_box = page.mediabox
                                        page_width_pts = float(page_box.width)
                                        page_height_pts = float(page_box.height)
                                        
                                        # Convert to inches (1 point = 1/72 inch)
                                        page_width_inches = page_width_pts / 72.0
                                        page_height_inches = page_height_pts / 72.0
                                        
                                        # Calculate DPI (assuming image covers most of page)
                                        if page_width_inches > 0 and page_height_inches > 0:
                                            dpi_x = width / page_width_inches
                                            dpi_y = height / page_height_inches
                                            dpi = (dpi_x + dpi_y) / 2  # Average DPI
                                            
                                            # Calculate coverage ratio (what % of page is this image?)
                                            # If image has similar dimensions to page, it's likely a full-page scan
                                            coverage_ratio = min(dpi_x / 72.0, dpi_y / 72.0)
                                            
                                            # Log DPI for analysis
                                            logging.debug(f"  Image {image_count}: {width}x{height}px, DPI={dpi:.1f}, coverage={coverage_ratio:.2f}")
                                            
                                            # High DPI (>150) suggests scanned document
                                            if dpi > 150:
                                                high_dpi_images += 1
                                                logging.info(f"  HIGH DPI IMAGE detected: {width}x{height}px, DPI={dpi:.1f}")
                                            
                                            # Medium-high DPI (100-150) with high coverage also suggests scan
                                            elif dpi > 100 and coverage_ratio > 0.8:
                                                high_dpi_images += 1
                                                logging.info(f"  MEDIUM-HIGH DPI IMAGE with full coverage: {width}x{height}px, DPI={dpi:.1f}, coverage={coverage_ratio:.2f}")
                                    except:
                                        pass
                                    
                                    # Large images (likely scanned pages): Multiple thresholds
                                    # Threshold 1: Traditional large image (> 500px in both dimensions)
                                    if width > 500 and height > 500:
                                        large_images += 1
                                    # Threshold 2: Medium image with high aspect ratio similar to page (portrait/landscape scan)
                                    elif width > 400 and height > 400:
                                        aspect_ratio = width / height if height > 0 else 0
                                        # Check if aspect ratio is page-like (portrait: 0.7-0.85, landscape: 1.2-1.5)
                                        if (0.65 < aspect_ratio < 0.9) or (1.1 < aspect_ratio < 1.6):
                                            large_images += 1
                                            logging.info(f"  MEDIUM IMAGE with page-like aspect: {width}x{height}px, ratio={aspect_ratio:.2f}")
                                    # Small images (likely signatures, logos, diagrams): < 400px or non-page-like
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
            
            # Also check for inline images in the content stream.
            # Some scanned PDFs embed page images directly using the BI/EI
            # (Begin Inline Image / End Inline Image) operators rather than
            # referencing them via /XObject.  PyPDF2 doesn't expose these,
            # but we can do a quick byte-level scan of the raw stream.
            if not has_images:
                try:
                    if hasattr(page, '/Contents'):
                        contents = page['/Contents']
                        if contents is not None:
                            # contents may be an array of streams or a single stream
                            raw_streams = []
                            try:
                                obj = contents.get_object()
                                if isinstance(obj, list):
                                    for s in obj:
                                        raw_streams.append(s.get_object().get_data())
                                else:
                                    raw_streams.append(obj.get_data())
                            except Exception:
                                pass
                            for raw in raw_streams:
                                if b' BI ' in raw or b'\nBI ' in raw or b'\nBI\n' in raw:
                                    has_images = True
                                    has_embedded_images = True
                                    image_count += 1
                                    large_images += 1  # inline images in scans are full-page
                                    logging.info(f"  Page {page_num}: Inline image (BI/EI) detected in content stream")
                                    break
                except Exception:
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
            
            # Check text quality (scanned PDFs with OCR often have poor quality text)
            text_quality_score = 0
            if char_count > 0:
                # Check for common OCR artifacts and poor quality indicators
                # 1. High ratio of special characters (OCR garbage)
                special_chars = sum(1 for c in text if not c.isalnum() and not c.isspace())
                special_ratio = special_chars / char_count
                
                # 2. High ratio of single characters (fragmented OCR)
                words = text.split()
                single_char_words = sum(1 for w in words if len(w) == 1 and w.isalpha())
                single_char_ratio = single_char_words / len(words) if len(words) > 0 else 0
                
                # 3. Very short average word length (OCR fragmentation)
                avg_word_len = sum(len(w) for w in words) / len(words) if len(words) > 0 else 0
                
                # Calculate quality score (0-100, higher is better)
                text_quality_score = 100
                if special_ratio > 0.3:  # Too many special characters
                    text_quality_score -= 40
                if single_char_ratio > 0.2:  # Too many single-letter words
                    text_quality_score -= 30
                if avg_word_len < 3:  # Words too short (OCR fragmentation)
                    text_quality_score -= 30
                    
                text_quality_score = max(0, text_quality_score)
            
            # Try to count fonts (scanned PDFs often have 1-2 OCR fonts, digital PDFs have multiple)
            font_count = 0
            try:
                if '/Resources' in page and '/Font' in page['/Resources']:
                    fonts = page['/Resources']['/Font']
                    font_count = len(fonts) if fonts else 0
            except:
                pass
            
            # Determine page type
            has_text = char_count >= min_text_threshold
            is_poor_quality_text = (text_quality_score < 50 and char_count > 0)
            
            # Enhanced classification using DPI, image size, and text quality
            # If page has high DPI images (>150 DPI), it's likely a scanned page
            if high_dpi_images > 0:
                image_pages += 1
                page_type = "image"
            # If page has large images, it's likely a scanned page
            elif large_images > 0:
                image_pages += 1
                page_type = "image"
            # If text quality is poor (likely OCR garbage), treat as image page
            elif is_poor_quality_text:
                image_pages += 1
                page_type = "image"
                logging.info(f"  Page {page_num}: Poor quality text detected (score={text_quality_score}), treating as image page")
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
                'high_dpi_images': high_dpi_images,
                'has_text': has_text,
                'text_quality_score': text_quality_score,
                'font_count': font_count
            })
            
            logging.info(f"Page {page_num}: type={page_type}, chars={char_count}, "
                         f"images={image_count} [L:{large_images}/S:{small_images}/HD:{high_dpi_images}], "
                         f"has_text={has_text}")
        
        # Calculate statistics
        avg_chars = total_chars / total_pages if total_pages > 0 else 0
        text_ratio = text_pages / total_pages if total_pages > 0 else 0
        image_ratio = image_pages / total_pages if total_pages > 0 else 0
        
        # Count total small images (likely signatures/stamps)
        total_small_images = sum(details['small_images'] for details in page_details)
        total_large_images = sum(details['large_images'] for details in page_details)
        total_high_dpi_images = sum(details['high_dpi_images'] for details in page_details)
        avg_text_quality = sum(d['text_quality_score'] for d in page_details) / total_pages if total_pages > 0 else 0
        avg_fonts_per_page = sum(d['font_count'] for d in page_details) / total_pages if total_pages > 0 else 0
        
        logging.info(f"Summary: avg_chars={avg_chars:.1f}, text_quality={avg_text_quality:.1f}, fonts={avg_fonts_per_page:.1f}, large_imgs={total_large_images}, high_dpi={total_high_dpi_images}")
        
        # Determine overall PDF type
        # PRIORITY 0: Check for scanned with OCR (has images AND poor text quality)
        # This catches scanned PDFs that have OCR text but it's garbage/artifacts
        if has_embedded_images and avg_text_quality < 50 and avg_chars > 50:
            # Has images, text exists but is poor quality (OCR garbage)
            pdf_type = "scanned"
            logging.info(f"Scanned detection (OCR garbage): images={has_embedded_images}, text_quality={avg_text_quality:.1f}, avg_chars={avg_chars:.1f}")
        # PRIORITY 1: Check for scanned PDFs with large images
        # Scanned PDFs have large images covering most of the page
        elif total_large_images > 0:
            # Has large images (>500x500px) - likely full-page scans
            # Even if text extraction works, it's still a scanned document
            if image_ratio >= 0.8:
                # 80%+ pages have large images - definitely scanned
                pdf_type = "scanned"
                logging.info(f"Scanned detection (80%+ image pages): large_images={total_large_images}, image_ratio={image_ratio:.1%}")
            elif image_ratio >= 0.5:
                # 50%+ pages have large images - likely scanned or hybrid
                # Check text quality to distinguish
                if avg_chars < 500:
                    # Low text extraction = scanned (text is garbage/artifacts)
                    pdf_type = "scanned"
                    logging.info(f"Scanned detection (50%+ images, low text): large_images={total_large_images}, avg_chars={avg_chars:.1f}")
                else:
                    # Has both images and good text = hybrid
                    pdf_type = "hybrid"
                    logging.info(f"Hybrid detection (images + text): large_images={total_large_images}, avg_chars={avg_chars:.1f}")
            elif avg_chars < 300:
                # Has large images but low text extraction = scanned
                pdf_type = "scanned"
                logging.info(f"Scanned detection (large images + low text): large_images={total_large_images}, avg_chars={avg_chars:.1f}")
            else:
                # Has large images and decent text = hybrid
                pdf_type = "hybrid"
                logging.info(f"Hybrid detection (large images + decent text): large_images={total_large_images}, avg_chars={avg_chars:.1f}")
        
        # PRIORITY 2: Check for high DPI images (strong indicator of scanning)
        elif total_high_dpi_images > 0:
            # Has high DPI images (>150 DPI) - strong indicator of scanned content
            if avg_chars < 500:
                # Low text extraction with high DPI images = scanned
                pdf_type = "scanned"
                logging.info(f"Scanned detection (high DPI + low text): high_dpi={total_high_dpi_images}, avg_chars={avg_chars:.1f}")
            else:
                # High DPI images with good text = hybrid (scanned with good OCR)
                pdf_type = "hybrid"
                logging.info(f"Hybrid detection (high DPI + good text): high_dpi={total_high_dpi_images}, avg_chars={avg_chars:.1f}")
        
        # PRIORITY 3: Check for very little text (likely scanned with no/poor OCR)
        elif avg_chars < 100:
            # Very little text per page - likely scanned with no OCR
            pdf_type = "scanned"
            logging.info(f"Scanned detection (very low text): avg_chars={avg_chars:.1f}, total_chars={total_chars}")
        
        # PRIORITY 4: Check for mostly image pages (based on classification)
        elif image_ratio >= 0.8:
            # 80%+ pages classified as images - scanned
            pdf_type = "scanned"
            logging.info(f"Scanned detection (80%+ image pages): image_ratio={image_ratio:.1%}")
        
        # PRIORITY 5: Check for hybrid document (has text but also signatures/stamps)
        elif text_ratio >= 0.8 and total_small_images >= 3:
            # Document has text but contains multiple small images (likely signatures)
            pdf_type = "hybrid"
            logging.info(f"Hybrid detection (text + signatures): text_ratio={text_ratio:.1%}, small_images={total_small_images}")
        
        # PRIORITY 5b: Zero fonts = scanned PDF with OCR text layer
        # Real digital PDFs ALWAYS have embedded fonts in /Resources/Font.
        # If we extracted text but found ZERO fonts, the text came from an
        # invisible OCR layer sitting on top of scanned page images.
        elif avg_fonts_per_page == 0 and avg_chars > 50:
            pdf_type = "scanned"
            logging.info(
                f"Scanned detection (zero fonts + OCR text): "
                f"avg_fonts={avg_fonts_per_page:.1f}, avg_chars={avg_chars:.1f}, "
                f"text_quality={avg_text_quality:.1f}"
            )
        
        # PRIORITY 6: Check for mostly text pages (digital PDF)
        elif text_ratio >= 0.8:
            # 80%+ pages have good text - digital PDF
            pdf_type = "digital"
            logging.info(f"Digital detection: text_ratio={text_ratio:.1%}, avg_chars={avg_chars:.1f}")
        
        # PRIORITY 7: Mix of text and image pages
        elif text_pages > 0 and image_pages > 0:
            # Mix of text and image pages - hybrid
            pdf_type = "hybrid"
            logging.info(f"Hybrid detection (mixed pages): text_pages={text_pages}, image_pages={image_pages}")
        
        # PRIORITY 8: Very little content overall
        elif total_chars < (total_pages * 10):
            # Less than 10 characters per page - likely scanned with no OCR
            pdf_type = "scanned"
            logging.info(f"Scanned detection (minimal text): total_chars={total_chars}, pages={total_pages}")
        
        # PRIORITY 9: Unclear/empty
        else:
            pdf_type = "empty"
            logging.info(f"Empty/unclear detection: text_pages={text_pages}, image_pages={image_pages}")
        
        result = {
            'type': pdf_type,
            'total_pages': total_pages,
            'text_pages': text_pages,
            'image_pages': image_pages,
            'pages_with_images': pages_with_images,
            'total_images': total_images,
            'small_images': total_small_images,
            'large_images': total_large_images,
            'high_dpi_images': total_high_dpi_images,
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
