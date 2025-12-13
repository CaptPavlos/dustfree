#!/usr/bin/env python3
"""Extract all invoices from emails and create a comprehensive list."""

import sqlite3
import re
from datetime import datetime
from collections import defaultdict

DB_PATH = 'email_archive.db'

def extract_invoices():
    """Extract all invoice references from emails."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT id, subject, from_address, to_address, date_received, body_text
        FROM emails
        ORDER BY date_received ASC
    ''')
    
    emails = cursor.fetchall()
    conn.close()
    
    # Invoice patterns
    invoice_patterns = [
        r'invoice\s*[#:№]?\s*(\d{3,})',
        r'inv\s*[#:№]?\s*(\d{3,})',
        r'factura\s*[#:№]?\s*(\d{3,})',
        r'proforma\s*[#:№]?\s*(\d{3,})',
        r'pi\s*[#:№]?\s*(\d{3,})',
        r'bill\s*[#:№]?\s*(\d{3,})',
        r'receipt\s*[#:№]?\s*(\d{3,})',
        r'credit\s*note\s*[#:№]?\s*(\d{3,})',
        r'debit\s*note\s*[#:№]?\s*(\d{3,})',
        r'payment\s*[#:№]?\s*(\d{3,})',
        r'order\s*[#:№]?\s*(\d{4,})',
        r'po\s*[#:№]?\s*(\d{4,})',
        r'#(\d{5,})',  # Generic number with 5+ digits
    ]
    
    # Amount patterns
    amount_patterns = [
        r'(?:€|EUR|eur)\s*([\d,\.]+)',
        r'([\d,\.]+)\s*(?:€|EUR|eur)',
        r'(?:\$|USD|usd)\s*([\d,\.]+)',
        r'([\d,\.]+)\s*(?:\$|USD|usd)',
        r'(?:£|GBP|gbp)\s*([\d,\.]+)',
        r'([\d,\.]+)\s*(?:£|GBP|gbp)',
        r'(?:RON|ron|Lei|lei)\s*([\d,\.]+)',
        r'([\d,\.]+)\s*(?:RON|ron|Lei|lei)',
        r'total[:\s]*([\d,\.]+)',
        r'amount[:\s]*([\d,\.]+)',
    ]
    
    invoices = []
    seen_invoices = set()
    
    for email in emails:
        subject = email['subject'] or ''
        body = email['body_text'] or ''
        text = f"{subject} {body}".lower()
        
        # Check if this email is invoice-related
        is_invoice_email = any(kw in text for kw in [
            'invoice', 'inv ', 'factura', 'proforma', 'payment', 'bill',
            'receipt', 'credit note', 'debit note', 'pi ', 'amount due',
            'total amount', 'please pay', 'payment due', 'remittance'
        ])
        
        if not is_invoice_email:
            continue
        
        # Extract invoice numbers
        invoice_numbers = []
        for pattern in invoice_patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            invoice_numbers.extend(matches)
        
        # Extract amounts
        amounts = []
        for pattern in amount_patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            for m in matches:
                try:
                    # Clean and parse amount
                    amount_str = m.replace(',', '').replace(' ', '')
                    amount = float(amount_str)
                    if 10 < amount < 10000000:  # Reasonable invoice amount range
                        amounts.append(amount)
                except:
                    pass
        
        # Determine invoice type
        invoice_type = 'Invoice'
        if 'proforma' in text:
            invoice_type = 'Proforma'
        elif 'credit note' in text:
            invoice_type = 'Credit Note'
        elif 'debit note' in text:
            invoice_type = 'Debit Note'
        elif 'receipt' in text:
            invoice_type = 'Receipt'
        elif 'payment' in text and 'invoice' not in text:
            invoice_type = 'Payment'
        
        # Determine status
        status = 'Unknown'
        if 'paid' in text or 'received' in text or 'settled' in text:
            status = 'Paid'
        elif 'overdue' in text or 'reminder' in text or 'urgent' in text:
            status = 'Overdue'
        elif 'pending' in text or 'awaiting' in text:
            status = 'Pending'
        elif 'cancelled' in text or 'canceled' in text:
            status = 'Cancelled'
        
        # Extract sender/company
        from_addr = email['from_address'] or ''
        company_match = re.match(r'^([^<]+)', from_addr)
        company = company_match.group(1).strip().strip('"') if company_match else ''
        
        email_match = re.search(r'<([^>]+)>', from_addr)
        if email_match:
            sender_email = email_match.group(1)
        else:
            sender_email = re.search(r'[\w\.-]+@[\w\.-]+', from_addr)
            sender_email = sender_email.group(0) if sender_email else from_addr
        
        # Create invoice record
        for inv_num in invoice_numbers[:3]:  # Limit to first 3 invoice numbers per email
            inv_key = f"{inv_num}_{sender_email}"
            if inv_key not in seen_invoices:
                seen_invoices.add(inv_key)
                invoices.append({
                    'email_id': email['id'],
                    'date': email['date_received'],
                    'invoice_number': inv_num,
                    'type': invoice_type,
                    'company': company,
                    'email': sender_email,
                    'subject': subject[:100],
                    'amount': max(amounts) if amounts else None,
                    'status': status
                })
        
        # If no invoice number found but it's clearly an invoice email
        if not invoice_numbers and is_invoice_email:
            inv_key = f"email_{email['id']}"
            if inv_key not in seen_invoices:
                seen_invoices.add(inv_key)
                invoices.append({
                    'email_id': email['id'],
                    'date': email['date_received'],
                    'invoice_number': 'N/A',
                    'type': invoice_type,
                    'company': company,
                    'email': sender_email,
                    'subject': subject[:100],
                    'amount': max(amounts) if amounts else None,
                    'status': status
                })
    
    return invoices

def generate_report(invoices):
    """Generate invoice report."""
    report = []
    report.append("=" * 120)
    report.append("INVOICE LIST - DFW PROFESSIONAL")
    report.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    report.append(f"Total Invoice-Related Emails: {len(invoices)}")
    report.append("=" * 120)
    report.append("")
    
    # Summary by type
    type_counts = defaultdict(int)
    for inv in invoices:
        type_counts[inv['type']] += 1
    
    report.append("SUMMARY BY TYPE:")
    report.append("-" * 40)
    for inv_type, count in sorted(type_counts.items(), key=lambda x: -x[1]):
        report.append(f"  {inv_type}: {count}")
    report.append("")
    
    # Summary by status
    status_counts = defaultdict(int)
    for inv in invoices:
        status_counts[inv['status']] += 1
    
    report.append("SUMMARY BY STATUS:")
    report.append("-" * 40)
    for status, count in sorted(status_counts.items(), key=lambda x: -x[1]):
        report.append(f"  {status}: {count}")
    report.append("")
    
    # Summary by company
    company_counts = defaultdict(lambda: {'count': 0, 'total': 0})
    for inv in invoices:
        company = inv['company'] or inv['email']
        company_counts[company]['count'] += 1
        if inv['amount']:
            company_counts[company]['total'] += inv['amount']
    
    report.append("TOP COMPANIES BY INVOICE COUNT:")
    report.append("-" * 60)
    for company, data in sorted(company_counts.items(), key=lambda x: -x[1]['count'])[:20]:
        total_str = f"€{data['total']:,.2f}" if data['total'] > 0 else "N/A"
        report.append(f"  {company[:40]:<40} | {data['count']:>3} invoices | Total: {total_str}")
    report.append("")
    
    # Detailed list
    report.append("=" * 120)
    report.append("DETAILED INVOICE LIST (Chronological Order)")
    report.append("=" * 120)
    report.append("")
    report.append(f"{'Date':<20} | {'Invoice #':<15} | {'Type':<12} | {'Status':<10} | {'Amount':<15} | {'Company/Sender':<30}")
    report.append("-" * 120)
    
    for inv in invoices:
        date_str = inv['date'][:10] if inv['date'] else 'Unknown'
        inv_num = str(inv['invoice_number'])[:15]
        inv_type = inv['type'][:12]
        status = inv['status'][:10]
        amount = f"€{inv['amount']:,.2f}" if inv['amount'] else 'N/A'
        company = (inv['company'] or inv['email'])[:30]
        
        report.append(f"{date_str:<20} | {inv_num:<15} | {inv_type:<12} | {status:<10} | {amount:<15} | {company:<30}")
    
    report.append("")
    report.append("=" * 120)
    report.append("END OF REPORT")
    report.append("=" * 120)
    
    return '\n'.join(report)

def main():
    print("Extracting invoices from emails...")
    invoices = extract_invoices()
    print(f"Found {len(invoices)} invoice-related records")
    
    report = generate_report(invoices)
    
    # Save to file
    with open('INVOICE_LIST.txt', 'w', encoding='utf-8') as f:
        f.write(report)
    
    print(f"Invoice list saved to INVOICE_LIST.txt")
    print("\nPreview:")
    print(report[:3000])

if __name__ == "__main__":
    main()
