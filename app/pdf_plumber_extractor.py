"""
PDF Text Extraction using pdfplumber with preserved formatting.
Alternative to Claude Vision API for better section header extraction.
"""

import pdfplumber
from typing import Dict, List, Optional
import re


class PDFPlumberExtractor:
    """Extract text from PDF files using pdfplumber with preserved formatting."""
    
    def __init__(self, pdf_path: str):
        """
        Initialize the extractor.
        
        Args:
            pdf_path: Path to the PDF file
        """
        self.pdf_path = pdf_path
        self.text_content = ""
        self.page_texts = []
        self.metadata = {}
    
    def extract_text_with_formatting(self, 
                                    layout: bool = True,
                                    preserve_spacing: bool = True,
                                    x_tolerance: int = 3,
                                    y_tolerance: int = 3) -> str:
        """
        Extract text from PDF with preserved formatting.
        
        Args:
            layout: If True, preserves layout and spacing
            preserve_spacing: If True, maintains whitespace between words
            x_tolerance: Horizontal tolerance for text grouping (pixels)
            y_tolerance: Vertical tolerance for text grouping (pixels)
            
        Returns:
            Extracted text with preserved formatting
        """
        try:
            with pdfplumber.open(self.pdf_path) as pdf:
                self.metadata = pdf.metadata
                all_text = []
                
                print(f"Extracting text from {len(pdf.pages)} pages...")
                
                for page_num, page in enumerate(pdf.pages, start=1):
                    print(f"   Processing page {page_num}/{len(pdf.pages)}...")
                    
                    # Add page marker for chunking workflows
                    page_marker_start = f"<<<PAGE_{page_num}_START>>>"
                    page_marker_end = f"<<<PAGE_{page_num}_END>>>"
                    
                    # Extract text with layout preservation
                    if layout:
                        page_text = page.extract_text(
                            layout=True,
                            x_tolerance=x_tolerance,
                            y_tolerance=y_tolerance
                        )
                    else:
                        page_text = page.extract_text()
                    
                    if page_text:
                        # Clean up excessive blank lines but preserve structure
                        page_text = self._clean_text(page_text, preserve_spacing)
                        
                        # Add page markers
                        page_text = f"{page_marker_start}\n{page_text}\n{page_marker_end}"
                        
                        all_text.append(page_text)
                        self.page_texts.append(page_text)
                    else:
                        print(f"   Warning: No text extracted from page {page_num}")
                
                self.text_content = "\n\n".join(all_text)
                print(f"Extraction complete: {len(self.text_content)} characters")
                
                return self.text_content
                
        except Exception as e:
            print(f"Error extracting text: {str(e)}")
            raise
    
    def _clean_text(self, text: str, preserve_spacing: bool = True) -> str:
        """
        Clean extracted text while preserving formatting.
        
        Args:
            text: Raw extracted text
            preserve_spacing: If True, maintains reasonable whitespace
            
        Returns:
            Cleaned text
        """
        if not text:
            return ""
        
        # Remove excessive blank lines (more than 2 consecutive)
        text = re.sub(r'\n{3,}', '\n\n', text)
        
        if not preserve_spacing:
            # Remove extra spaces within lines
            lines = text.split('\n')
            cleaned_lines = []
            for line in lines:
                # Normalize spaces within line but keep line structure
                cleaned_line = re.sub(r' {2,}', ' ', line)
                cleaned_lines.append(cleaned_line)
            text = '\n'.join(cleaned_lines)
        
        return text.strip()
    
    def extract_with_char_details(self) -> List[Dict]:
        """
        Extract text with character-level details (font, size, position).
        Useful for detecting formatting changes like bold, italic, etc.
        
        Returns:
            List of character dictionaries with formatting details
        """
        char_details = []
        
        try:
            with pdfplumber.open(self.pdf_path) as pdf:
                for page_num, page in enumerate(pdf.pages, start=1):
                    chars = page.chars
                    
                    for char in chars:
                        char_info = {
                            'page': page_num,
                            'text': char.get('text', ''),
                            'x0': char.get('x0'),
                            'y0': char.get('y0'),
                            'x1': char.get('x1'),
                            'y1': char.get('y1'),
                            'fontname': char.get('fontname', ''),
                            'size': char.get('size', 0),
                            'width': char.get('width', 0),
                            'height': char.get('height', 0)
                        }
                        char_details.append(char_info)
                
                print(f"Extracted {len(char_details)} characters with formatting details")
                
        except Exception as e:
            print(f"Error extracting character details: {str(e)}")
            raise
        
        return char_details
    
    def extract_tables(self) -> List[List[List[str]]]:
        """
        Extract tables from PDF.
        
        Returns:
            List of tables (one per page), each table is a list of rows
        """
        tables_by_page = []
        
        try:
            with pdfplumber.open(self.pdf_path) as pdf:
                print(f"Extracting tables from {len(pdf.pages)} pages...")
                
                for page_num, page in enumerate(pdf.pages, start=1):
                    tables = page.extract_tables()
                    
                    if tables:
                        print(f"   Found {len(tables)} table(s) on page {page_num}")
                        tables_by_page.append(tables)
                    else:
                        tables_by_page.append([])
                
                total_tables = sum(len(tables) for tables in tables_by_page)
                print(f"Extracted {total_tables} tables total")
                
        except Exception as e:
            print(f"Error extracting tables: {str(e)}")
            raise
        
        return tables_by_page
    
    def detect_section_headers(self, text: Optional[str] = None) -> List[Dict]:
        """
        Detect section headers in the extracted text.
        Looks for patterns like "Sec. 1.", "Sec. 16.", etc.
        
        Args:
            text: Text to search (uses self.text_content if None)
            
        Returns:
            List of detected section headers with positions
        """
        if text is None:
            text = self.text_content
        
        if not text:
            print("Warning: No text available for section detection")
            return []
        
        # Pattern for section headers: "Sec. N." or "Section N."
        section_pattern = r'^(Sec\.|Section)\s+(\d+)\.\s*(.+?)$'
        
        sections = []
        lines = text.split('\n')
        
        for line_num, line in enumerate(lines, start=1):
            match = re.match(section_pattern, line.strip(), re.IGNORECASE)
            if match:
                section_info = {
                    'line_number': line_num,
                    'section_number': int(match.group(2)),
                    'full_text': line.strip(),
                    'header_text': match.group(3).strip()
                }
                sections.append(section_info)
        
        print(f"Detected {len(sections)} section headers")
        for sec in sections:
            print(f"   Line {sec['line_number']}: Sec. {sec['section_number']}")
        
        return sections
    
    def validate_sequential_sections(self, sections: Optional[List[Dict]] = None) -> Dict:
        """
        Validate that section numbers are sequential.
        
        Args:
            sections: List of section dictionaries (auto-detects if None)
            
        Returns:
            Validation results with missing section numbers
        """
        if sections is None:
            sections = self.detect_section_headers()
        
        if not sections:
            return {
                'valid': False,
                'message': 'No sections detected',
                'missing_sections': []
            }
        
        section_numbers = sorted([sec['section_number'] for sec in sections])
        expected_range = range(1, section_numbers[-1] + 1)
        missing_sections = [n for n in expected_range if n not in section_numbers]
        
        is_valid = len(missing_sections) == 0
        
        result = {
            'valid': is_valid,
            'total_sections': len(sections),
            'max_section_number': section_numbers[-1],
            'missing_sections': missing_sections,
            'message': f"All sections present (1-{section_numbers[-1]})" if is_valid 
                      else f"Missing sections: {missing_sections}"
        }
        
        if missing_sections:
            print(f"Warning: {result['message']}")
        else:
            print(f"Success: {result['message']}")
        
        return result
    
    def save_to_file(self, output_path: str, text: Optional[str] = None):
        """
        Save extracted text to file.
        
        Args:
            output_path: Path to save the text file
            text: Text to save (uses self.text_content if None)
        """
        if text is None:
            text = self.text_content
        
        if not text:
            print("Warning: No text to save")
            return
        
        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(text)
            
            print(f"Text saved to: {output_path}")
            print(f"   File size: {len(text)} characters")
            
        except Exception as e:
            print(f"Error saving file: {str(e)}")
            raise
    
    def get_metadata(self) -> Dict:
        """Get PDF metadata."""
        return self.metadata


def extract_pdf_with_pdfplumber(pdf_path: str, 
                                output_path: Optional[str] = None,
                                validate_sections: bool = True) -> str:
    """
    Convenience function to extract text from PDF using pdfplumber.
    
    Args:
        pdf_path: Path to PDF file
        output_path: Optional path to save extracted text
        validate_sections: If True, validates section numbering
        
    Returns:
        Extracted text with preserved formatting
    """
    print(f"\n{'='*60}")
    print(f"PDF Text Extraction with pdfplumber")
    print(f"{'='*60}")
    print(f"Input: {pdf_path}")
    
    extractor = PDFPlumberExtractor(pdf_path)
    
    # Extract text with preserved formatting
    text = extractor.extract_text_with_formatting(
        layout=True,
        preserve_spacing=True,
        x_tolerance=3,
        y_tolerance=3
    )
    
    # Validate section numbering
    if validate_sections:
        print(f"\n{'='*60}")
        print(f"Section Validation")
        print(f"{'='*60}")
        sections = extractor.detect_section_headers()
        validation = extractor.validate_sequential_sections(sections)
        
        if not validation['valid']:
            print(f"\nWARNING: Missing sections detected!")
            print(f"   Missing: {validation['missing_sections']}")
    
    # Save to file if requested
    if output_path:
        print(f"\n{'='*60}")
        print(f"Saving Output")
        print(f"{'='*60}")
        extractor.save_to_file(output_path, text)
    
    print(f"\n{'='*60}")
    print(f"Extraction Complete")
    print(f"{'='*60}\n")
    
    return text


# Example usage
if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python pdf_plumber_extractor.py <pdf_path> [output_path]")
        sys.exit(1)
    
    pdf_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else None
    
    # Extract text
    text = extract_pdf_with_pdfplumber(pdf_path, output_path, validate_sections=True)
    
    # Print first 1000 characters as preview
    print("\n" + "="*60)
    print("Text Preview (first 1000 chars)")
    print("="*60)
    print(text[:1000])
    print("...")
