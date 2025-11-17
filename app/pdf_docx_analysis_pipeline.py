# Integrated PDF to DOCX Analysis Pipeline
"""
Modular approach combining:
1. PDF to DOCX conversion using Adobe PDF Services
2. DOCX formatting analysis and tagging
3. Summary generation for verification

This replaces running separate scripts manually.
"""

import logging
import os
import json
import ssl
import urllib3
import requests
from datetime import datetime
from pathlib import Path

# DOCX processing imports
from docx import Document
from docx.shared import RGBColor
from docx.enum.text import WD_COLOR_INDEX

# Adobe PDF Services imports
from adobe.pdfservices.operation.auth.service_principal_credentials import ServicePrincipalCredentials
from adobe.pdfservices.operation.exception.exceptions import ServiceApiException, ServiceUsageException, SdkException
from adobe.pdfservices.operation.io.cloud_asset import CloudAsset
from adobe.pdfservices.operation.io.stream_asset import StreamAsset
from adobe.pdfservices.operation.pdf_services import PDFServices
from adobe.pdfservices.operation.pdf_services_media_type import PDFServicesMediaType
from adobe.pdfservices.operation.pdfjobs.jobs.export_pdf_job import ExportPDFJob
from adobe.pdfservices.operation.pdfjobs.params.export_pdf.export_pdf_params import ExportPDFParams
from adobe.pdfservices.operation.pdfjobs.params.export_pdf.export_pdf_target_format import ExportPDFTargetFormat
from adobe.pdfservices.operation.pdfjobs.result.export_pdf_result import ExportPDFResult

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Monkey patch requests to disable SSL verification
original_request = requests.Session.request
def patched_request(self, method, url, **kwargs):
    kwargs['verify'] = False
    return original_request(self, method, url, **kwargs)
requests.Session.request = patched_request

# Also patch the global requests methods
requests.packages.urllib3.disable_warnings()
ssl._create_default_https_context = ssl._create_unverified_context

# Initialize the logger - Only if not already configured
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO)

class PDFToDocxAnalysisPipeline:
    """
    Integrated pipeline for PDF to DOCX conversion and formatting analysis.
    """
    
    def __init__(self, input_pdf_path, output_base_folder="../data/output", state_code=None):
        self.input_pdf_path = input_pdf_path
        self.output_base_folder = output_base_folder
        self.state_code = state_code
        self.docx_path = None
        self.timestamp = datetime.now().strftime('%Y-%m-%dT%H-%M-%S')
        
        # Extract PDF filename without extension for consistent naming
        self.pdf_filename = Path(input_pdf_path).stem
        
        # Set up folder paths with state-specific subfolders if state_code is provided
        if state_code:
            self.docx_folder = f"{output_base_folder}/Output_Docx/{state_code}"
            self.verify_folder = f"{output_base_folder}/Verify/{state_code}"
        else:
            self.docx_folder = f"{output_base_folder}/Output_Docx"
            self.verify_folder = f"{output_base_folder}/Verify"
        
        # Ensure output directories exist
        os.makedirs(self.docx_folder, exist_ok=True)
        os.makedirs(self.verify_folder, exist_ok=True)
        
        print(f"🚀 Starting PDF to DOCX Analysis Pipeline")
        print(f"📄 Input PDF: {input_pdf_path}")
        print(f"📁 Output folder: {output_base_folder}")
        print("=" * 60)

    def step1_convert_pdf_to_docx(self):
        """
        Step 1: Convert PDF to DOCX using Adobe PDF Services.
        """
        print("📋 STEP 1: Converting PDF to DOCX...")
        
        try:
            # Read the input PDF file
            with open(self.input_pdf_path, 'rb') as file:
                input_stream = file.read()

            # Initialize Adobe credentials from config folder
            cred_file_path = os.path.join("../config", "pdfservices-api-credentials.json")
            with open(cred_file_path, 'r') as cred_file:
                cred_data = json.load(cred_file)
            
            credentials = ServicePrincipalCredentials(
                client_id=cred_data['client_credentials']['client_id'],
                client_secret=cred_data['client_credentials']['client_secret']
            )

            # Create PDF Services instance
            pdf_services = PDFServices(credentials=credentials)

            # Upload PDF and create asset
            input_asset = pdf_services.upload(input_stream=input_stream, mime_type=PDFServicesMediaType.PDF)

            # Create parameters for DOCX export
            export_pdf_params = ExportPDFParams(target_format=ExportPDFTargetFormat.DOCX)

            # Create and submit job
            export_pdf_job = ExportPDFJob(input_asset=input_asset, export_pdf_params=export_pdf_params)
            location = pdf_services.submit(export_pdf_job)
            pdf_services_response = pdf_services.get_job_result(location, ExportPDFResult)

            # Get the result and save to file
            result_asset: CloudAsset = pdf_services_response.get_result().get_asset()
            stream_asset: StreamAsset = pdf_services.get_content(result_asset)

            # Create output file path using PDF filename
            self.docx_path = f"{self.docx_folder}/{self.pdf_filename}.docx"
            
            with open(self.docx_path, "wb") as file:
                file.write(stream_asset.get_input_stream())

            print(f"✅ PDF converted successfully to: {self.docx_path}")
            return True

        except (ServiceApiException, ServiceUsageException, SdkException) as e:
            print(f"❌ Adobe PDF Services error: {e}")
            return False
        except Exception as e:
            print(f"❌ Error in PDF conversion: {e}")
            return False

    def step2_analyze_docx_formatting(self):
        """
        Step 2: Analyze DOCX for formatting (strikethrough, bold, italic, underline).
        """
        print(f"\n📋 STEP 2: Analyzing DOCX formatting...")
        
        if not self.docx_path or not os.path.exists(self.docx_path):
            print("❌ DOCX file not found. Step 1 must be completed first.")
            return None
        
        try:
            # Open the document
            doc = Document(self.docx_path)
            formatted_items = []
            
            print(f"📖 Analyzing: {self.docx_path}")
            
            # Iterate through paragraphs
            for para_idx, paragraph in enumerate(doc.paragraphs, 1):
                if paragraph.text.strip():  # Skip empty paragraphs
                    # Check each run in the paragraph
                    for run_idx, run in enumerate(paragraph.runs):
                        if run.text.strip():  # Skip empty runs
                            # Check for different formatting types
                            formatting_types = []
                            
                            # Check strikethrough
                            if run.font.strike or run.font.double_strike:
                                strike_type = 'double' if run.font.double_strike else 'single'
                                formatting_types.append(f"strikethrough ({strike_type})")
                            
                            # Check bold
                            if run.font.bold:
                                formatting_types.append("bold")
                            
                            # Check italic
                            if run.font.italic:
                                formatting_types.append("italic")
                            
                            # Check underline
                            if run.font.underline:
                                formatting_types.append("underline")
                            
                            # If any formatting is found, add to results
                            if formatting_types:
                                formatted_item = {
                                    'text': run.text,
                                    'paragraph': para_idx,
                                    'run': run_idx + 1,
                                    'formatting_types': formatting_types,
                                    'is_bold': bool(run.font.bold),
                                    'is_italic': bool(run.font.italic),
                                    'is_underline': bool(run.font.underline),
                                    'paragraph_text': paragraph.text[:100] + "..." if len(paragraph.text) > 100 else paragraph.text
                                }
                                formatted_items.append(formatted_item)
            
            # Check tables for formatted text
            table_count = 0
            for table in doc.tables:
                table_count += 1
                for row_idx, row in enumerate(table.rows):
                    for cell_idx, cell in enumerate(row.cells):
                        for para_idx, paragraph in enumerate(cell.paragraphs):
                            for run_idx, run in enumerate(paragraph.runs):
                                if run.text.strip():
                                    # Check for different formatting types in tables
                                    formatting_types = []
                                    
                                    # Check strikethrough
                                    if run.font.strike or run.font.double_strike:
                                        strike_type = 'double' if run.font.double_strike else 'single'
                                        formatting_types.append(f"strikethrough ({strike_type})")
                                    
                                    # Check bold
                                    if run.font.bold:
                                        formatting_types.append("bold")
                                    
                                    # Check italic
                                    if run.font.italic:
                                        formatting_types.append("italic")
                                    
                                    # Check underline
                                    if run.font.underline:
                                        formatting_types.append("underline")
                                    
                                    # If any formatting is found, add to results
                                    if formatting_types:
                                        formatted_item = {
                                            'text': run.text,
                                            'location': f'Table {table_count}, Row {row_idx + 1}, Cell {cell_idx + 1}',
                                            'paragraph': para_idx + 1,
                                            'run': run_idx + 1,
                                            'formatting_types': formatting_types,
                                            'is_bold': bool(run.font.bold),
                                            'is_italic': bool(run.font.italic),
                                            'is_underline': bool(run.font.underline),
                                            'paragraph_text': paragraph.text[:100] + "..." if len(paragraph.text) > 100 else paragraph.text
                                        }
                                        formatted_items.append(formatted_item)
            
            print(f"✅ Found {len(formatted_items)} formatted text items")
            return formatted_items
            
        except Exception as e:
            print(f"❌ Error analyzing DOCX formatting: {e}")
            return None

    def step3_generate_summary(self, formatted_items):
        """
        Step 3: Generate summary verification file.
        """
        print(f"\n📋 STEP 3: Generating summary verification...")
        
        if not formatted_items:
            print("⚠️ No formatted items to summarize")
            return None
        
        try:
            # Generate summary filename using PDF filename
            summary_filename = f"{self.pdf_filename}_Summary.txt"
            summary_path = os.path.join(self.verify_folder, summary_filename)
            
            # Save summary to file
            with open(summary_path, 'w', encoding='utf-8') as f:
                f.write("FORMATTED TEXT SUMMARY VERIFICATION\n")
                f.write("=" * 60 + "\n\n")
                f.write(f"Source PDF File: {self.input_pdf_path}\n")
                f.write(f"Source DOCX File: {self.docx_path}\n")
                f.write(f"Analysis Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"Output File: {summary_filename}\n\n")
                
                f.write("SUMMARY:\n")
                f.write("-" * 20 + "\n")
                f.write(f"Total formatted text items found: {len(formatted_items)}\n\n")
                
                # Add All formatted text section
                f.write("All formatted text:\n")
                for i, item in enumerate(formatted_items, 1):
                    location = item.get('location', f"Paragraph {item['paragraph']}")
                    formatting = ', '.join(item['formatting_types'])
                    f.write(f"{i}. '{item['text']}' (Location: {location}, Formatting: {formatting})\n")
                f.write("\n")
                
                # Count by type
                strikethrough_count = sum(1 for item in formatted_items if any('strikethrough' in fmt for fmt in item['formatting_types']))
                bold_count = sum(1 for item in formatted_items if item.get('is_bold', False))
                italic_count = sum(1 for item in formatted_items if item.get('is_italic', False))
                underline_count = sum(1 for item in formatted_items if item.get('is_underline', False))
                
                f.write("BREAKDOWN BY FORMATTING TYPE:\n")
                f.write("-" * 30 + "\n")
                f.write(f"  - Strikethrough: {strikethrough_count}\n")
                f.write(f"  - Bold: {bold_count}\n")
                f.write(f"  - Italic: {italic_count}\n")
                f.write(f"  - Underline: {underline_count}\n\n")
            
            print(f"✅ Summary saved to: {summary_path}")
            return summary_path
            
        except Exception as e:
            print(f"❌ Error generating summary: {e}")
            return None

    def step4_create_tagged_document(self, formatted_items):
        """
        Step 4: Create tagged DOCX document (optional).
        """
        print(f"\n📋 STEP 4: Creating tagged document...")
        
        if not formatted_items:
            print("⚠️ No formatted items to tag")
            return None
        
        try:
            # Create tagged document path using PDF filename
            tagged_docx_path = f"{self.docx_folder}/{self.pdf_filename}_TAGGED.docx"
            
            # Open original document
            original_doc = Document(self.docx_path)
            
            # Create new document for tagged version
            new_doc = Document()
            
            # Add a simple header
            new_doc.add_heading('Formatted Text Analysis Results', 0)
            new_doc.add_paragraph(f'Source PDF: {self.input_pdf_path}')
            new_doc.add_paragraph(f'Total formatted items: {len(formatted_items)}')
            new_doc.add_paragraph('')
            
            # Add summary of formatted items
            new_doc.add_heading('Formatted Text Summary', level=1)
            
            for i, item in enumerate(formatted_items, 1):
                para = new_doc.add_paragraph()
                para.add_run(f"{i}. ").bold = True
                para.add_run(f"'{item['text']}' ")
                para.add_run(f"({', '.join(item['formatting_types'])}) ")
                location = item.get('location', f"Paragraph {item['paragraph']}")
                para.add_run(f"- {location}")
            
            # Save tagged document
            new_doc.save(tagged_docx_path)
            print(f"✅ Tagged document saved to: {tagged_docx_path}")
            
            return tagged_docx_path
            
        except Exception as e:
            print(f"❌ Error creating tagged document: {e}")
            return None

    def run_complete_pipeline(self):
        """
        Execute the complete pipeline from PDF to final analysis.
        """
        print("🔄 STARTING COMPLETE PDF-TO-DOCX ANALYSIS PIPELINE")
        print("=" * 60)
        
        results = {
            'success': False,
            'input_pdf': self.input_pdf_path,
            'docx_path': None,
            'summary_path': None,
            'formatted_items_count': 0,
            'errors': []
        }
        
        # Step 1: Convert PDF to DOCX
        if not self.step1_convert_pdf_to_docx():
            results['errors'].append("PDF to DOCX conversion failed")
            return results
        
        results['docx_path'] = self.docx_path
        
        # Step 2: Analyze DOCX formatting
        formatted_items = self.step2_analyze_docx_formatting()
        if formatted_items is None:
            results['errors'].append("DOCX formatting analysis failed")
            return results
        
        results['formatted_items_count'] = len(formatted_items)
        
        # Step 3: Generate summary
        summary_path = self.step3_generate_summary(formatted_items)
        if summary_path:
            results['summary_path'] = summary_path
        else:
            results['errors'].append("Summary generation failed")
        
        # Mark as successful if we got through the core steps
        if results['docx_path'] and results['summary_path']:
            results['success'] = True
        
        return results

    def print_pipeline_summary(self, results):
        """
        Print a summary of the pipeline execution.
        """
        print("\n" + "=" * 60)
        print("🎯 PIPELINE EXECUTION SUMMARY")
        print("=" * 60)
        
        if results['success']:
            print("✅ PIPELINE COMPLETED SUCCESSFULLY!")
        else:
            print("❌ PIPELINE COMPLETED WITH ERRORS")
        
        print(f"\n📄 INPUT:")
        print(f"   PDF File: {results['input_pdf']}")
        
        print(f"\n📁 OUTPUTS:")
        if results['docx_path']:
            print(f"   ✅ DOCX File: {results['docx_path']}")
        if results['summary_path']:
            print(f"   ✅ Summary: {results['summary_path']}")
        
        print(f"\n📊 ANALYSIS RESULTS:")
        print(f"   Total formatted items found: {results['formatted_items_count']}")
        
        if results['errors']:
            print(f"\n⚠️ ERRORS ENCOUNTERED:")
            for error in results['errors']:
                print(f"   • {error}")
        
        print("=" * 60)


def run_pipeline_for_file(pdf_path, output_folder="../data/output"):
    """
    Convenience function to run the complete pipeline for a single PDF file.
    
    Args:
        pdf_path (str): Path to the input PDF file
        output_folder (str): Base folder for outputs
    
    Returns:
        dict: Results of the pipeline execution
    """
    if not os.path.exists(pdf_path):
        print(f"❌ Input PDF file not found: {pdf_path}")
        return None
    
    # Create pipeline instance
    pipeline = PDFToDocxAnalysisPipeline(pdf_path, output_folder)
    
    # Run complete pipeline
    results = pipeline.run_complete_pipeline()
    
    # Print summary
    pipeline.print_pipeline_summary(results)
    
    return results


# Example usage
if __name__ == "__main__":
    # Configuration
    input_pdf = r"../data/input/IA/IA HSB117.pdf"
    output_base = "../data/output"
    
    try:
        print("🚀 Starting Integrated PDF-to-DOCX Analysis Pipeline")
        print(f"📄 Processing: {input_pdf}")
        
        # Run the complete pipeline
        results = run_pipeline_for_file(input_pdf, output_base)
        
        if results and results['success']:
            print("\n🎉 Pipeline completed successfully!")
            if results.get('state_code'):
                print(f"📁 Check the '{output_base}/Output_Docx/{results['state_code']}' folder for results")
                print(f"📁 Check the '{output_base}/Verify/{results['state_code']}' folder for summary file")
            else:
                print(f"📁 Check the '{output_base}/Output_Docx' folder for results")
                print(f"📁 Check the '{output_base}/Verify' folder for summary file")
        else:
            print("\n💥 Pipeline failed - check error messages above")
            
    except Exception as e:
        print(f"💥 Fatal error: {e}")