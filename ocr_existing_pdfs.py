#!/usr/bin/env python3
"""
OCR existing PDF attachments that have no parsed invoice data.
Uses Adobe PDF Services API for scanned documents.
"""

import os
import sqlite3
import logging
from dotenv import load_dotenv

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

DB_PATH = 'email_archive.db'

# Check for pdfplumber
try:
    import pdfplumber
    PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False
    logging.error("pdfplumber not installed. Install with: pip install pdfplumber")

# Check for Adobe PDF Services
try:
    from adobe.pdfservices.operation.auth.service_principal_credentials import ServicePrincipalCredentials
    from adobe.pdfservices.operation.pdf_services import PDFServices
    from adobe.pdfservices.operation.pdf_services_media_type import PDFServicesMediaType
    from adobe.pdfservices.operation.pdfjobs.jobs.ocr_pdf_job import OCRPDFJob
    from adobe.pdfservices.operation.pdfjobs.params.ocr_pdf.ocr_params import OCRParams
    from adobe.pdfservices.operation.pdfjobs.params.ocr_pdf.ocr_supported_locale import OCRSupportedLocale
    from adobe.pdfservices.operation.pdfjobs.params.ocr_pdf.ocr_supported_type import OCRSupportedType
    from adobe.pdfservices.operation.pdfjobs.result.ocr_pdf_result import OCRPDFResult
    ADOBE_SUPPORT = True
except ImportError:
    ADOBE_SUPPORT = False
    logging.warning("Adobe PDF Services SDK not installed. OCR disabled.")

import re

def init_adobe_services():
    """Initialize Adobe PDF Services."""
    if not ADOBE_SUPPORT:
        return None
    
    client_id = os.getenv('ADOBE_CLIENT_ID')
    client_secret = os.getenv('ADOBE_CLIENT_SECRET')
    
    if not client_id or not client_secret:
        logging.warning("Adobe credentials not found in .env")
        return None
    
    try:
        credentials = ServicePrincipalCredentials(
            client_id=client_id,
            client_secret=client_secret
        )
        return PDFServices(credentials=credentials)
    except Exception as e:
        logging.error(f"Failed to init Adobe services: {e}")
        return None

def ocr_pdf(pdf_services, input_path):
    """Perform OCR on a PDF and return text."""
    if not pdf_services:
        return None
    
    try:
        with open(input_path, 'rb') as f:
            input_stream = f.read()
        
        input_asset = pdf_services.upload(
            input_stream=input_stream,
            mime_type=PDFServicesMediaType.PDF
        )
        
        ocr_params = OCRParams(
            ocr_locale=OCRSupportedLocale.EN_US,
            ocr_type=OCRSupportedType.SEARCHABLE_IMAGE
        )
        
        ocr_job = OCRPDFJob(input_asset=input_asset, ocr_params=ocr_params)
        location = pdf_services.submit(ocr_job)
        
        pdf_services_response = pdf_services.get_job_result(location, OCRPDFResult)
        result_asset = pdf_services_response.get_result().get_asset()
        stream_asset = pdf_services.get_content(result_asset)
        
        # Save OCR'd PDF
        ocr_output_path = input_path.replace('.pdf', '_ocr.pdf').replace('.PDF', '_ocr.pdf')
        with open(ocr_output_path, 'wb') as f:
            f.write(stream_asset.get_input_stream())
        
        # Extract text
        if PDF_SUPPORT:
            with pdfplumber.open(ocr_output_path) as pdf:
                full_text = ""
                for page in pdf.pages[:5]:
                    text = page.extract_text() or ""
                    full_text += text + "\n"
                return full_text.strip()
        
        return None
    except Exception as e:
        logging.warning(f"OCR failed for {input_path}: {e}")
        return None

def extract_invoice_data(text):
    """Extract invoice data from text."""
    if not text:
        return None
    
    # Invoice number patterns
    invoice_number = None
    for pattern in [
        r'(?:invoice|factura|inv)[\s#:№\.]*([A-Z]*[\d\-/]+)',
        r'(?:nr\.?|numar|number)[\s:]*([A-Z]*[\d\-/]+)',
    ]:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            invoice_number = match.group(1)
            break
    
    # Date patterns
    invoice_date = None
    for pattern in [
        r'(?:date|data)[\s:]*([\d]{1,2}[/\-\.][\d]{1,2}[/\-\.][\d]{2,4})',
        r'([\d]{1,2}[/\-\.][\d]{1,2}[/\-\.][\d]{4})',
    ]:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            invoice_date = match.group(1)
            break
    
    # Amount patterns
    amount = None
    currency = 'EUR'
    for pattern in [
        r'(?:total|amount|suma|valoare)[\s:]*(?:€|EUR|RON|USD|£)?\s*([\d,\.]+)',
        r'(?:€|EUR)\s*([\d,\.]+)',
        r'([\d,\.]+)\s*(?:€|EUR)',
        r'(?:RON|Lei)\s*([\d,\.]+)',
        r'([\d,\.]+)\s*(?:RON|Lei)',
    ]:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for m in matches:
            try:
                amt_str = m.replace(',', '').replace(' ', '')
                amt = float(amt_str)
                if 10 < amt < 10000000:
                    amount = amt
                    if 'RON' in pattern or 'Lei' in pattern:
                        currency = 'RON'
                    break
            except:
                pass
        if amount:
            break
    
    return {
        'invoice_number': invoice_number,
        'invoice_date': invoice_date,
        'amount': amount,
        'currency': currency,
        'raw_text': text[:5000]
    }

def process_unprocessed_pdfs():
    """Find and process PDFs that have no parsed invoice data."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Find PDF attachments without parsed invoices
    cursor.execute('''
        SELECT a.id, a.email_id, a.filename, a.file_path
        FROM attachments a
        LEFT JOIN parsed_invoices pi ON a.id = pi.attachment_id
        WHERE a.filename LIKE '%.pdf' OR a.filename LIKE '%.PDF'
        AND pi.id IS NULL
        AND a.file_path IS NOT NULL
    ''')
    
    unprocessed = cursor.fetchall()
    logging.info(f"Found {len(unprocessed)} PDFs without parsed invoice data")
    
    if not unprocessed:
        logging.info("All PDFs have been processed")
        conn.close()
        return
    
    pdf_services = init_adobe_services()
    processed = 0
    ocr_used = 0
    
    for attachment in unprocessed:
        file_path = attachment['file_path']
        
        if not os.path.exists(file_path):
            logging.warning(f"File not found: {file_path}")
            continue
        
        logging.info(f"Processing: {attachment['filename']}")
        
        # Try pdfplumber first
        text = ""
        if PDF_SUPPORT:
            try:
                with pdfplumber.open(file_path) as pdf:
                    for page in pdf.pages[:3]:
                        page_text = page.extract_text() or ""
                        text += page_text + "\n"
            except Exception as e:
                logging.warning(f"pdfplumber failed: {e}")
        
        # If no text, try OCR
        if not text.strip() and pdf_services:
            logging.info(f"  -> Attempting OCR...")
            text = ocr_pdf(pdf_services, file_path)
            if text:
                ocr_used += 1
        
        if not text or not text.strip():
            logging.info(f"  -> No text extracted, skipping")
            continue
        
        # Extract invoice data
        data = extract_invoice_data(text)
        
        if data and (data['invoice_number'] or data['amount']):
            cursor.execute('''
                INSERT INTO parsed_invoices 
                (attachment_id, email_id, invoice_number, invoice_date, amount, currency, vendor, raw_text)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                attachment['id'],
                attachment['email_id'],
                data['invoice_number'],
                data['invoice_date'],
                data['amount'],
                data['currency'],
                None,
                data['raw_text']
            ))
            conn.commit()
            processed += 1
            logging.info(f"  -> Parsed: Invoice #{data['invoice_number']}, Amount: {data['currency']} {data['amount']}")
    
    conn.close()
    logging.info(f"\nProcessed {processed} PDFs, OCR used on {ocr_used} files")

if __name__ == "__main__":
    process_unprocessed_pdfs()
