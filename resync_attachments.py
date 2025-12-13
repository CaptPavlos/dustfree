#!/usr/bin/env python3
"""
Re-sync attachments and check for missing parsed invoices.
This script will:
1. Check all attachments in the database
2. Find PDFs that haven't been parsed yet
3. Parse them and extract invoice data
"""

import os
import sqlite3
import re
from datetime import datetime

# Try to import PDF parsing libraries
try:
    import fitz  # PyMuPDF
    HAS_FITZ = True
except ImportError:
    HAS_FITZ = False
    print("Warning: PyMuPDF not installed. Install with: pip install PyMuPDF")

DB_PATH = 'email_archive.db'

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def extract_text_from_pdf(file_path):
    """Extract text from a PDF file."""
    if not HAS_FITZ:
        return None
    
    if not os.path.exists(file_path):
        return None
    
    try:
        doc = fitz.open(file_path)
        text = ""
        for page in doc:
            text += page.get_text()
        doc.close()
        return text
    except Exception as e:
        print(f"  Error extracting text from {file_path}: {e}")
        return None

def extract_invoice_data(raw_text):
    """Extract invoice number, date, amount, and currency from text."""
    if not raw_text:
        return None, None, None, None
    
    # Invoice number patterns
    inv_number = None
    inv_patterns = [
        r'INVOICE\s*No\.?\s*:?\s*(\d+)',
        r'Invoice\s*Number\s*:?\s*(\S+)',
        r'Nr\.?\s*Factur[ia]+\s*:?\s*(\d+)',
        r'Seria\s+\w+\s+nr:?\s*(\d+)',
        r'PO\s*#?\s*:?\s*([A-Z]?\d+)',
    ]
    for pattern in inv_patterns:
        match = re.search(pattern, raw_text, re.IGNORECASE)
        if match:
            inv_number = match.group(1)
            break
    
    # Date patterns
    inv_date = None
    date_patterns = [
        r'Date\s*:?\s*(\d{1,2}[/\.-]\d{1,2}[/\.-]\d{2,4})',
        r'(\d{1,2}\s+\w+\s+\d{4})',
        r'Data\s*\([^)]+\)\s*:?\s*(\d{1,2}[\./-]\d{1,2}[\./-]\d{2,4})',
    ]
    for pattern in date_patterns:
        match = re.search(pattern, raw_text, re.IGNORECASE)
        if match:
            inv_date = match.group(1)
            break
    
    # Amount patterns - order matters, more specific first
    amount = None
    amount_patterns = [
        r'TOTAL\s+AMOUNT\s*[€]?\s*([\d.,]+)',
        r'Total\s*(?:EUR|€)?\s*:?\s*€?\s*([\d.,]+)',
        r'TOTAL\s*EUR\s*€?\s*([\d.,]+)',
        r'Grand\s*Total\s*:?\s*€?\s*([\d.,]+)',
        r'Amount\s*Due\s*:?\s*€?\s*([\d.,]+)',
        r'Σύνολο\s*:?\s*([\d.,]+)',  # Greek "Total"
        r'Καθαρή Αξία:\s*([\d.,]+)',  # Greek "Net Value"
        r'AMOUNT\s*\(?\s*€?\s*\)?\s*([\d.,]+)',
    ]
    for pattern in amount_patterns:
        match = re.search(pattern, raw_text, re.IGNORECASE)
        if match:
            try:
                amt_str = match.group(1)
                # Handle European number format (4.070,00 -> 4070.00)
                # European: periods are thousands sep, comma is decimal
                # Check if it looks European (has both . and , with comma after last period)
                if ',' in amt_str and '.' in amt_str:
                    # European format: 4.070,00 -> remove dots, replace comma with dot
                    amt_str = amt_str.replace('.', '').replace(',', '.')
                elif ',' in amt_str:
                    # Could be European with just comma decimal (4070,00)
                    # Or could be US thousands (4,070) - check position
                    parts = amt_str.split(',')
                    if len(parts) == 2 and len(parts[1]) == 2:
                        # Likely European decimal: 4070,00 -> 4070.00
                        amt_str = amt_str.replace(',', '.')
                    else:
                        # Likely US thousands: 4,070 -> 4070
                        amt_str = amt_str.replace(',', '')
                amount = float(amt_str)
                break
            except:
                pass
    
    # Currency detection
    currency = 'EUR'
    if 'RON' in raw_text or 'LEI' in raw_text:
        currency = 'RON'
    elif 'GBP' in raw_text or '£' in raw_text:
        currency = 'GBP'
    elif 'USD' in raw_text or '$' in raw_text:
        currency = 'USD'
    
    return inv_number, inv_date, amount, currency

def check_missing_attachments():
    """Check for attachments that haven't been parsed yet."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Get all PDF attachments (case insensitive)
    cursor.execute('''
        SELECT a.id, a.filename, a.file_path, a.email_id, e.subject
        FROM attachments a
        JOIN emails e ON a.email_id = e.id
        WHERE lower(a.filename) LIKE '%.pdf'
    ''')
    all_pdfs = cursor.fetchall()
    
    # Get parsed attachment IDs
    cursor.execute('SELECT DISTINCT attachment_id FROM parsed_invoices WHERE attachment_id IS NOT NULL')
    parsed_ids = set(row[0] for row in cursor.fetchall())
    
    # Find missing
    missing = []
    for pdf in all_pdfs:
        if pdf['id'] not in parsed_ids:
            missing.append(pdf)
    
    conn.close()
    return all_pdfs, missing

def parse_missing_attachments(missing_attachments, dry_run=True):
    """Parse attachments that haven't been processed yet."""
    if not HAS_FITZ:
        print("Cannot parse PDFs without PyMuPDF. Install with: pip install PyMuPDF")
        return
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    parsed_count = 0
    error_count = 0
    missing_file_count = 0
    
    for att in missing_attachments:
        file_path = att['file_path']
        
        if not file_path:
            print(f"  No path: {att['filename']} (ID: {att['id']})")
            missing_file_count += 1
            continue
            
        if not os.path.exists(file_path):
            print(f"  Missing file: {att['filename']} (ID: {att['id']}) - {file_path}")
            missing_file_count += 1
            continue
        
        raw_text = extract_text_from_pdf(file_path)
        if raw_text is None:
            error_count += 1
            continue
        
        # Even if empty, store it so we don't try again
        if not raw_text:
            raw_text = "[No text content]"
        
        inv_number, inv_date, amount, currency = extract_invoice_data(raw_text)
        
        if dry_run:
            print(f"  Would parse: {att['filename']}")
            print(f"    Invoice: {inv_number}, Date: {inv_date}, Amount: {amount} {currency}")
        else:
            try:
                cursor.execute('''
                    INSERT INTO parsed_invoices 
                    (email_id, attachment_id, invoice_number, invoice_date, amount, currency, raw_text)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (att['email_id'], att['id'], inv_number, inv_date, amount, currency, raw_text[:10000]))
                parsed_count += 1
                if inv_number:
                    print(f"  ✓ Parsed: {att['filename']} -> Invoice {inv_number}")
                else:
                    print(f"  ✓ Parsed: {att['filename']} (no invoice detected)")
            except Exception as e:
                print(f"  ✗ Error inserting {att['filename']}: {e}")
                error_count += 1
    
    if not dry_run:
        conn.commit()
        print(f"\n=== Summary ===")
        print(f"Parsed: {parsed_count}")
        print(f"Missing files: {missing_file_count}")
        print(f"Errors: {error_count}")
    
    conn.close()

def main():
    print("=" * 60)
    print("ATTACHMENT SYNC CHECK")
    print("=" * 60)
    
    all_pdfs, missing = check_missing_attachments()
    
    print(f"\nTotal PDF attachments: {len(all_pdfs)}")
    print(f"Already parsed: {len(all_pdfs) - len(missing)}")
    print(f"Missing/unparsed: {len(missing)}")
    
    if missing:
        print(f"\n--- Missing Attachments ({len(missing)}) ---")
        for att in missing[:20]:  # Show first 20
            file_exists = "✓" if att['file_path'] and os.path.exists(att['file_path']) else "✗"
            print(f"  [{file_exists}] ID:{att['id']} - {att['filename']}")
            print(f"      Subject: {(att['subject'] or '')[:50]}")
        
        if len(missing) > 20:
            print(f"  ... and {len(missing) - 20} more")
        
        print("\n--- Actions ---")
        print("1. Run with --parse to parse missing attachments")
        print("2. Run with --parse --commit to save to database")
    else:
        print("\nAll attachments have been parsed!")
    
    # Check for files that exist but aren't in parsed_invoices
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM parsed_invoices')
    parsed_count = cursor.fetchone()[0]
    print(f"\nTotal parsed invoices in database: {parsed_count}")
    conn.close()

if __name__ == '__main__':
    import sys
    
    if '--parse' in sys.argv:
        _, missing = check_missing_attachments()
        dry_run = '--commit' not in sys.argv
        if dry_run:
            print("\n--- DRY RUN (add --commit to save) ---\n")
        parse_missing_attachments(missing, dry_run=dry_run)
    else:
        main()
