import os
import imaplib
import email
import sqlite3
import logging
import hashlib
import re
from datetime import datetime
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import List, Dict, Optional
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich.progress import Progress
import email_validator
from dateutil import parser

# PDF parsing - optional
try:
    import pdfplumber
    PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False
    logging.warning("pdfplumber not installed. PDF parsing disabled. Install with: pip install pdfplumber")

# Adobe PDF Services API for OCR - optional
try:
    from adobe.pdfservices.operation.auth.service_principal_credentials import ServicePrincipalCredentials
    from adobe.pdfservices.operation.pdf_services import PDFServices
    from adobe.pdfservices.operation.pdf_services_media_type import PDFServicesMediaType
    from adobe.pdfservices.operation.pdfjobs.jobs.ocr_pdf_job import OCRPDFJob
    from adobe.pdfservices.operation.pdfjobs.params.ocr_pdf.ocr_params import OCRParams
    from adobe.pdfservices.operation.pdfjobs.params.ocr_pdf.ocr_supported_locale import OCRSupportedLocale
    from adobe.pdfservices.operation.pdfjobs.params.ocr_pdf.ocr_supported_type import OCRSupportedType
    from adobe.pdfservices.operation.pdfjobs.result.ocr_pdf_result import OCRPDFResult
    from adobe.pdfservices.operation.io.cloud_asset import CloudAsset
    from adobe.pdfservices.operation.io.stream_asset import StreamAsset
    ADOBE_OCR_SUPPORT = True
except ImportError:
    ADOBE_OCR_SUPPORT = False
    logging.warning("Adobe PDF Services SDK not installed. OCR disabled. Install with: pip install pdfservices-sdk")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('email_archiver.log'),
        logging.StreamHandler()
    ]
)

console = Console()

class EmailArchiver:
    def __init__(self):
        self.db_path = 'email_archive.db'
        self._init_db()
        load_dotenv()
        
        # Initialize Adobe PDF Services if available
        self.pdf_services = None
        if ADOBE_OCR_SUPPORT:
            self._init_adobe_services()
        
    def _init_db(self):
        """Initialize the SQLite database."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS emails (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT UNIQUE,
                    subject TEXT,
                    from_address TEXT,
                    to_address TEXT,
                    date_received DATETIME,
                    body_text TEXT,
                    body_html TEXT,
                    headers TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Attachments table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS attachments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email_id INTEGER,
                    filename TEXT,
                    content_type TEXT,
                    size INTEGER,
                    file_path TEXT,
                    file_hash TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (email_id) REFERENCES emails(id)
                )
            ''')
            
            # Parsed invoices from attachments
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS parsed_invoices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    attachment_id INTEGER,
                    email_id INTEGER,
                    invoice_number TEXT,
                    invoice_date TEXT,
                    amount REAL,
                    currency TEXT,
                    vendor TEXT,
                    raw_text TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (attachment_id) REFERENCES attachments(id),
                    FOREIGN KEY (email_id) REFERENCES emails(id)
                )
            ''')
            
            conn.commit()
        
        # Create attachments directory
        self.attachments_dir = 'attachments'
        os.makedirs(self.attachments_dir, exist_ok=True)
    
    def _init_adobe_services(self):
        """Initialize Adobe PDF Services for OCR."""
        try:
            client_id = os.getenv('ADOBE_CLIENT_ID')
            client_secret = os.getenv('ADOBE_CLIENT_SECRET')
            
            if not client_id or not client_secret:
                logging.warning("Adobe credentials not found in .env file. OCR disabled.")
                return
            
            credentials = ServicePrincipalCredentials(
                client_id=client_id,
                client_secret=client_secret
            )
            self.pdf_services = PDFServices(credentials=credentials)
            logging.info("Adobe PDF Services initialized successfully")
            
        except Exception as e:
            logging.warning(f"Failed to initialize Adobe PDF Services: {str(e)}")
            self.pdf_services = None
    
    def ocr_pdf(self, input_path: str) -> Optional[str]:
        """Perform OCR on a PDF and return the text content."""
        if not self.pdf_services:
            return None
        
        try:
            # Read the PDF file
            with open(input_path, 'rb') as f:
                input_stream = f.read()
            
            # Create input asset
            input_asset = self.pdf_services.upload(
                input_stream=input_stream,
                mime_type=PDFServicesMediaType.PDF
            )
            
            # Set OCR parameters
            ocr_params = OCRParams(
                ocr_locale=OCRSupportedLocale.EN_US,
                ocr_type=OCRSupportedType.SEARCHABLE_IMAGE
            )
            
            # Create and submit OCR job
            ocr_job = OCRPDFJob(input_asset=input_asset, ocr_params=ocr_params)
            location = self.pdf_services.submit(ocr_job)
            
            # Get the result
            pdf_services_response = self.pdf_services.get_job_result(
                location,
                OCRPDFResult
            )
            
            # Get the OCR'd PDF
            result_asset: CloudAsset = pdf_services_response.get_result().get_asset()
            stream_asset: StreamAsset = self.pdf_services.get_content(result_asset)
            
            # Save the OCR'd PDF
            ocr_output_path = input_path.replace('.pdf', '_ocr.pdf')
            with open(ocr_output_path, 'wb') as f:
                f.write(stream_asset.get_input_stream())
            
            # Now extract text from the OCR'd PDF using pdfplumber
            if PDF_SUPPORT:
                with pdfplumber.open(ocr_output_path) as pdf:
                    full_text = ""
                    for page in pdf.pages[:5]:  # First 5 pages
                        text = page.extract_text() or ""
                        full_text += text + "\n"
                    return full_text.strip()
            
            return None
            
        except Exception as e:
            logging.warning(f"OCR failed for {input_path}: {str(e)}")
            return None
    
    def connect_to_email(self, email_address: str, password: str, imap_server: str = 'imap.gmail.com'):
        """Connect to the IMAP server."""
        try:
            # Validate email address
            email_validator.validate_email(email_address)
            
            # Connect to the IMAP server
            self.mail = imaplib.IMAP4_SSL(imap_server)
            self.mail.login(email_address, password)
            logging.info(f"Successfully connected to {imap_server}")
            return True
            
        except Exception as e:
            logging.error(f"Failed to connect to email: {str(e)}")
            return False
    
    def download_emails(self, folder: str = 'INBOX', limit: int = 100):
        """Download emails from the specified folder."""
        if not hasattr(self, 'mail'):
            logging.error("Not connected to email server. Call connect_to_email() first.")
            return
            
        try:
            self.mail.select(folder)
            status, messages = self.mail.search(None, 'ALL')
            
            if status != 'OK':
                logging.error("Failed to search emails.")
                return
                
            message_ids = messages[0].split()
            message_ids = message_ids[-limit:]  # Get most recent emails
            
            saved_count = 0
            with Progress() as progress:
                task = progress.add_task("[green]Downloading emails...", total=len(message_ids))
                
                for msg_id in message_ids:
                    status, msg_data = self.mail.fetch(msg_id, '(RFC822)')
                    
                    if status == 'OK':
                        raw_email = msg_data[0][1]
                        email_message = email.message_from_bytes(raw_email)
                        
                        if self._save_email(email_message, folder):
                            saved_count += 1
                            
                    progress.update(task, advance=1)
            
            logging.info(f"Successfully saved {saved_count} emails to the database.")
            
        except Exception as e:
            logging.error(f"Error downloading emails: {str(e)}")
    
    def _save_email(self, email_message, folder: str = 'INBOX') -> bool:
        """Save email to the database."""
        try:
            # Extract email data
            message_id = email_message.get('Message-ID', '')
            subject = self._decode_header(email_message.get('Subject', 'No Subject'))
            from_address = email_message.get('From', 'Unknown Sender')
            to_address = email_message.get('To', '')
            # Determine if sent email
            is_sent = 'sent' in folder.lower() or 'Sent' in folder
            
            # Parse date
            date_str = email_message.get('Date')
            date_received = None
            if date_str:
                try:
                    date_received = parsedate_to_datetime(date_str)
                except (ValueError, TypeError):
                    try:
                        date_received = parser.parse(date_str)
                    except:
                        date_received = datetime.now()
            else:
                date_received = datetime.now()
            
            # Get email body
            body_text, body_html = self._get_email_body(email_message)
            
            # Get all headers
            headers = '\n'.join(f"{k}: {v}" for k, v in email_message.items())
            
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                # Add folder column if it doesn't exist
                try:
                    cursor.execute('ALTER TABLE emails ADD COLUMN folder TEXT DEFAULT "INBOX"')
                except:
                    pass
                cursor.execute('''
                    INSERT OR IGNORE INTO emails 
                    (message_id, subject, from_address, to_address, date_received, 
                     body_text, body_html, headers, folder)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    message_id, subject, from_address, to_address, 
                    date_received.isoformat(), body_text, body_html, headers, folder
                ))
                conn.commit()
                
                if cursor.rowcount > 0:
                    # Get the email ID
                    cursor.execute('SELECT id FROM emails WHERE message_id = ?', (message_id,))
                    row = cursor.fetchone()
                    if row:
                        email_id = row[0]
                        # Save attachments
                        self._save_attachments(email_message, email_id, date_received)
                    return True
                return False
                
        except Exception as e:
            logging.error(f"Error saving email to database: {str(e)}")
            return False
    
    def _save_attachments(self, email_message, email_id: int, email_date: datetime):
        """Extract and save attachments from email."""
        if not email_message.is_multipart():
            return
        
        for part in email_message.walk():
            content_disposition = str(part.get("Content-Disposition", ""))
            
            if "attachment" in content_disposition or part.get_filename():
                filename = part.get_filename()
                if not filename:
                    continue
                
                # Decode filename if needed
                filename = self._decode_header(filename)
                
                # Clean filename
                filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
                
                try:
                    # Get attachment content
                    payload = part.get_payload(decode=True)
                    if not payload:
                        continue
                    
                    # Calculate hash to avoid duplicates
                    file_hash = hashlib.md5(payload).hexdigest()
                    
                    # Create date-based subdirectory
                    date_dir = email_date.strftime('%Y-%m') if email_date else 'unknown'
                    save_dir = os.path.join(self.attachments_dir, date_dir)
                    os.makedirs(save_dir, exist_ok=True)
                    
                    # Create unique filename
                    base, ext = os.path.splitext(filename)
                    file_path = os.path.join(save_dir, f"{base}_{file_hash[:8]}{ext}")
                    
                    # Save file
                    with open(file_path, 'wb') as f:
                        f.write(payload)
                    
                    # Save to database
                    content_type = part.get_content_type()
                    file_size = len(payload)
                    
                    with sqlite3.connect(self.db_path) as conn:
                        cursor = conn.cursor()
                        cursor.execute('''
                            INSERT INTO attachments 
                            (email_id, filename, content_type, size, file_path, file_hash)
                            VALUES (?, ?, ?, ?, ?, ?)
                        ''', (email_id, filename, content_type, file_size, file_path, file_hash))
                        conn.commit()
                        attachment_id = cursor.lastrowid
                    
                    # Parse PDF if applicable
                    if ext.lower() == '.pdf' and PDF_SUPPORT:
                        self._parse_pdf_invoice(file_path, attachment_id, email_id)
                    
                    logging.debug(f"Saved attachment: {filename}")
                    
                except Exception as e:
                    logging.warning(f"Error saving attachment {filename}: {str(e)}")
    
    def _parse_pdf_invoice(self, file_path: str, attachment_id: int, email_id: int):
        """Parse PDF to extract invoice information. Uses OCR if text extraction fails."""
        try:
            full_text = ""
            
            # First try pdfplumber
            with pdfplumber.open(file_path) as pdf:
                for page in pdf.pages[:3]:  # First 3 pages only
                    text = page.extract_text() or ""
                    full_text += text + "\n"
            
            # If no text extracted, try Adobe OCR
            if not full_text.strip() and self.pdf_services:
                logging.info(f"No text in PDF, attempting OCR: {file_path}")
                ocr_text = self.ocr_pdf(file_path)
                if ocr_text:
                    full_text = ocr_text
                    logging.info(f"OCR successful for: {file_path}")
                
            if not full_text.strip():
                return
            
            # Extract invoice number
            invoice_number = None
            invoice_patterns = [
                r'(?:invoice|factura|inv)[\s#:№\.]*([A-Z]*[\d\-/]+)',
                r'(?:nr\.?|numar|number)[\s:]*([A-Z]*[\d\-/]+)',
                r'(?:document|doc)[\s#:]*([A-Z]*[\d\-/]+)',
            ]
            for pattern in invoice_patterns:
                match = re.search(pattern, full_text, re.IGNORECASE)
                if match:
                    invoice_number = match.group(1)
                    break
            
            # Extract date
            invoice_date = None
            date_patterns = [
                r'(?:date|data)[\s:]*([\d]{1,2}[/\-\.][\d]{1,2}[/\-\.][\d]{2,4})',
                r'([\d]{1,2}[/\-\.][\d]{1,2}[/\-\.][\d]{4})',
            ]
            for pattern in date_patterns:
                match = re.search(pattern, full_text, re.IGNORECASE)
                if match:
                    invoice_date = match.group(1)
                    break
            
            # Extract amount
            amount = None
            currency = 'EUR'
            amount_patterns = [
                r'(?:total|amount|suma|valoare)[\s:]*(?:€|EUR|RON|USD|£)?\s*([\d,\.]+)',
                r'(?:€|EUR)\s*([\d,\.]+)',
                r'([\d,\.]+)\s*(?:€|EUR)',
                r'(?:RON|Lei)\s*([\d,\.]+)',
                r'([\d,\.]+)\s*(?:RON|Lei)',
            ]
            for pattern in amount_patterns:
                matches = re.findall(pattern, full_text, re.IGNORECASE)
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
            
            # Extract vendor
            vendor = None
            vendor_patterns = [
                r'(?:from|de la|furnizor)[\s:]*([A-Za-z\s]+(?:SRL|SA|LLC|Ltd|GmbH)?)',
            ]
            for pattern in vendor_patterns:
                match = re.search(pattern, full_text, re.IGNORECASE)
                if match:
                    vendor = match.group(1).strip()[:100]
                    break
            
            # Save parsed data
            if invoice_number or amount:
                with sqlite3.connect(self.db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute('''
                        INSERT INTO parsed_invoices 
                        (attachment_id, email_id, invoice_number, invoice_date, amount, currency, vendor, raw_text)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (attachment_id, email_id, invoice_number, invoice_date, amount, currency, vendor, full_text[:5000]))
                    conn.commit()
                
                logging.info(f"Parsed invoice: {invoice_number}, Amount: {currency} {amount}")
                    
        except Exception as e:
            logging.warning(f"Error parsing PDF {file_path}: {str(e)}")
    
    def _get_email_body(self, email_message) -> tuple[str, str]:
        """Extract text and HTML body from email."""
        body_text = ""
        body_html = ""
        
        if email_message.is_multipart():
            for part in email_message.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition"))
                
                if "attachment" not in content_disposition:
                    if content_type == "text/plain" and not body_text:
                        body_text = self._get_decoded_payload(part)
                    elif content_type == "text/html" and not body_html:
                        body_html = self._get_decoded_payload(part)
        else:
            content_type = email_message.get_content_type()
            if content_type == "text/plain":
                body_text = self._get_decoded_payload(email_message)
            elif content_type == "text/html":
                body_html = self._get_decoded_payload(email_message)
        
        return body_text, body_html
    
    def _get_decoded_payload(self, part) -> str:
        """Get decoded payload from email part."""
        try:
            payload = part.get_payload(decode=True)
            charset = part.get_content_charset() or 'utf-8'
            return payload.decode(charset, errors='replace')
        except Exception as e:
            logging.warning(f"Error decoding email part: {str(e)}")
            return "[Content could not be decoded]"
    
    def _decode_header(self, header) -> str:
        """Decode email header."""
        try:
            decoded = decode_header(header)
            return ' '.join(
                str(part[0].decode(part[1] or 'utf-8', errors='replace')) 
                if isinstance(part[0], bytes) else str(part[0])
                for part in decoded
            )
        except Exception as e:
            logging.warning(f"Error decoding header: {str(e)}")
            return str(header)
    
    def list_emails(self, limit: int = 10):
        """List emails from the database."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT id, message_id, subject, from_address, date_received 
                    FROM emails 
                    ORDER BY date_received DESC 
                    LIMIT ?
                ''', (limit,))
                
                rows = cursor.fetchall()
                
                if not rows:
                    console.print("[yellow]No emails found in the database.[/]")
                    return
                
                # Create a table
                table = Table(show_header=True, header_style="bold magenta")
                table.add_column("ID", style="dim", width=4)
                table.add_column("From", width=30)
                table.add_column("Subject", width=50)
                table.add_column("Date", style="dim", width=20)
                
                for row in rows:
                    table.add_row(
                        str(row['id']),
                        row['from_address'][:30] + ('...' if len(row['from_address']) > 30 else ''),
                        row['subject'][:50] + ('...' if len(row['subject']) > 50 else ''),
                        row['date_received']
                    )
                
                console.print(table)
                
        except Exception as e:
            logging.error(f"Error listing emails: {str(e)}")
    
    def get_email(self, email_id: int):
        """Get full email details by ID."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM emails WHERE id = ?', (email_id,))
                
                row = cursor.fetchone()
                
                if not row:
                    console.print(f"[red]Email with ID {email_id} not found.[/]")
                    return
                
                # Display email details
                console.print(f"\n[bold]From:[/] {row['from_address']}")
                console.print(f"[bold]To:[/] {row['to_address']}")
                console.print(f"[bold]Date:[/] {row['date_received']}")
                console.print(f"[bold]Subject:[/] {row['subject']}")
                console.print("\n[bold]Body:[/]")
                
                # Display HTML if available, otherwise text
                if row['body_html']:
                    console.print("(HTML content - view in a browser for full formatting)")
                    # For simplicity, we're just showing the text version here
                    console.print(row['body_text'] or "[No text content]")
                else:
                    console.print(row['body_text'] or "[No content]")
                
                console.print("\n[bold]Headers:[/]")
                console.print(row['headers'])
                
        except Exception as e:
            logging.error(f"Error retrieving email: {str(e)}")

def main():
    """Main function to run the email archiver."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Email Archiver - Download and store emails locally')
    subparsers = parser.add_subparsers(dest='command', help='Command to execute')
    
    # Download command
    download_parser = subparsers.add_parser('download', help='Download emails')
    download_parser.add_argument('email', help='Email address')
    download_parser.add_argument('--password', help='Email password (or leave empty to be prompted)')
    download_parser.add_argument('--server', default='imap.gmail.com', help='IMAP server (default: imap.gmail.com)')
    download_parser.add_argument('--folder', default='INBOX', help='Email folder to download from (default: INBOX)')
    download_parser.add_argument('--limit', type=int, default=100, help='Maximum number of emails to download (default: 100)')
    
    # List command
    list_parser = subparsers.add_parser('list', help='List downloaded emails')
    list_parser.add_argument('--limit', type=int, default=10, help='Maximum number of emails to show (default: 10)')
    
    # View command
    view_parser = subparsers.add_parser('view', help='View a specific email')
    view_parser.add_argument('email_id', type=int, help='Email ID to view')
    
    args = parser.parse_args()
    
    archiver = EmailArchiver()
    
    if args.command == 'download':
        password = args.password or input("Enter your email password: ")
        if archiver.connect_to_email(args.email, password, args.server):
            archiver.download_emails(folder=args.folder, limit=args.limit)
    
    elif args.command == 'list':
        archiver.list_emails(limit=args.limit)
    
    elif args.command == 'view':
        archiver.get_email(args.email_id)
    
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
