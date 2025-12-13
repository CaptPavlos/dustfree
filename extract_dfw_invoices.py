#!/usr/bin/env python3
"""Extract invoices/factura originating from DFW Romania."""

import sqlite3
import re
from datetime import datetime
from collections import defaultdict

DB_PATH = 'email_archive.db'

def extract_dfw_invoices():
    """Extract invoices that DFW Romania issued (factura)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Get all emails
    cursor.execute('''
        SELECT id, subject, from_address, to_address, date_received, body_text
        FROM emails
        ORDER BY date_received ASC
    ''')
    
    emails = cursor.fetchall()
    conn.close()
    
    invoices = []
    
    # Patterns for DFW invoice numbers
    invoice_patterns = [
        r'factura\s*(?:nr\.?|numar|#)?\s*[:\s]*([A-Z]*\d+)',
        r'invoice\s*(?:nr\.?|no\.?|number|#)?\s*[:\s]*([A-Z]*[\d\-]+)',
        r'inv[:\-\s]*([A-Z]*\d+)',
        r'factura\s+emisa.*?(\d+)',
        r'seria\s+([A-Z]+)\s*nr\.?\s*(\d+)',
        r'DFW.*?invoice.*?(\d+)',
        r'proforma.*?(\d+)',
    ]
    
    # Amount patterns
    amount_patterns = [
        r'(?:€|EUR|eur)\s*([\d,\.]+)',
        r'([\d,\.]+)\s*(?:€|EUR|eur)',
        r'(?:RON|ron|Lei|lei)\s*([\d,\.]+)',
        r'([\d,\.]+)\s*(?:RON|ron|Lei|lei)',
        r'total[:\s]*([\d,\.]+)',
        r'valoare[:\s]*([\d,\.]+)',
    ]
    
    for email in emails:
        subject = email['subject'] or ''
        body = email['body_text'] or ''
        from_addr = email['from_address'] or ''
        to_addr = email['to_address'] or ''
        text = f"{subject}\n{body}"
        text_lower = text.lower()
        
        # Check if this is a DFW Romania invoice
        # Look for: factura, invoice from DFW, or mentions of DFW issuing invoice
        is_dfw_invoice = False
        invoice_direction = 'unknown'
        
        # Check if it's FROM DFW (outgoing invoice)
        if 'dfwprofessional' in from_addr.lower() or 'dfw' in from_addr.lower():
            if 'factura' in text_lower or 'invoice' in text_lower:
                is_dfw_invoice = True
                invoice_direction = 'outgoing'
        
        # Check if subject/body indicates DFW issued the invoice
        if 'factura emisa' in text_lower and 'dfw' in text_lower:
            is_dfw_invoice = True
            invoice_direction = 'outgoing'
        
        # Check for DFW invoice mentions in body
        if ('dfw professional' in text_lower or 'dfw romania' in text_lower) and ('factura' in text_lower or 'invoice' in text_lower):
            # Check context - is DFW the issuer?
            if any(phrase in text_lower for phrase in ['factura dfw', 'invoice from dfw', 'dfw invoice', 'factura emisa']):
                is_dfw_invoice = True
                invoice_direction = 'outgoing'
        
        # Also include invoices TO DFW (incoming) for completeness but mark them
        if 'dfwprofessional' in to_addr.lower() or 'dfw' in to_addr.lower():
            if 'factura' in text_lower or 'invoice' in text_lower:
                if not is_dfw_invoice:
                    is_dfw_invoice = True
                    invoice_direction = 'incoming'
        
        # Check for accounting invoices from George Gologan (DFW accountant)
        if 'georgegologan' in from_addr.lower() and ('accounting invoice' in text_lower or 'factura' in text_lower):
            is_dfw_invoice = True
            invoice_direction = 'accounting'
        
        # Check for SmartTAX invoices (DFW's tax service)
        if 'smarttax' in from_addr.lower() and 'factura' in text_lower:
            is_dfw_invoice = True
            invoice_direction = 'tax_service'
        
        # Check for Orbit Streem invoices to DFW
        if 'orbit' in from_addr.lower() and ('factura' in text_lower or 'invoice' in text_lower) and 'dfw' in text_lower:
            is_dfw_invoice = True
            invoice_direction = 'supplier'
        
        if not is_dfw_invoice:
            continue
        
        # Extract invoice number
        invoice_number = 'N/A'
        for pattern in invoice_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                if len(match.groups()) == 2:
                    invoice_number = f"{match.group(1)}{match.group(2)}"
                else:
                    invoice_number = match.group(1)
                break
        
        # Extract amount
        amount = None
        currency = 'EUR'
        for pattern in amount_patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            for m in matches:
                try:
                    amount_str = m.replace(',', '').replace(' ', '')
                    amt = float(amount_str)
                    if 10 < amt < 10000000:
                        amount = amt
                        if 'ron' in pattern.lower() or 'lei' in pattern.lower():
                            currency = 'RON'
                        break
                except:
                    pass
            if amount:
                break
        
        # Extract customer/vendor
        customer = ''
        if invoice_direction == 'outgoing':
            # Customer is in TO field or mentioned in body
            customer = to_addr
        elif invoice_direction == 'incoming':
            # Vendor is in FROM field
            customer = from_addr
        else:
            customer = from_addr
        
        # Clean up customer name
        customer_match = re.match(r'^([^<]+)', customer)
        customer_name = customer_match.group(1).strip().strip('"') if customer_match else customer
        
        invoices.append({
            'email_id': email['id'],
            'date': email['date_received'],
            'invoice_number': invoice_number,
            'direction': invoice_direction,
            'customer_vendor': customer_name[:50],
            'subject': subject[:80],
            'amount': amount,
            'currency': currency
        })
    
    return invoices

def generate_report(invoices):
    """Generate DFW invoice report."""
    report = []
    report.append("=" * 130)
    report.append("DFW ROMANIA - INVOICE LIST (FACTURA)")
    report.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    report.append(f"Total Invoice-Related Records: {len(invoices)}")
    report.append("=" * 130)
    report.append("")
    
    # Summary by direction
    direction_counts = defaultdict(int)
    for inv in invoices:
        direction_counts[inv['direction']] += 1
    
    report.append("SUMMARY BY TYPE:")
    report.append("-" * 50)
    direction_labels = {
        'outgoing': 'Outgoing (DFW issued to customers)',
        'incoming': 'Incoming (Received by DFW)',
        'accounting': 'Accounting Records',
        'tax_service': 'Tax Service (SmartTAX)',
        'supplier': 'Supplier Invoices (to DFW)',
        'unknown': 'Unknown Direction'
    }
    for direction, count in sorted(direction_counts.items(), key=lambda x: -x[1]):
        label = direction_labels.get(direction, direction)
        report.append(f"  {label}: {count}")
    report.append("")
    
    # Group by direction for detailed lists
    for direction in ['outgoing', 'accounting', 'incoming', 'supplier', 'tax_service', 'unknown']:
        dir_invoices = [inv for inv in invoices if inv['direction'] == direction]
        if not dir_invoices:
            continue
        
        label = direction_labels.get(direction, direction)
        report.append("=" * 130)
        report.append(f"{label.upper()} ({len(dir_invoices)} records)")
        report.append("=" * 130)
        report.append("")
        report.append(f"{'Date':<12} | {'Invoice #':<20} | {'Amount':<15} | {'Customer/Vendor':<30} | {'Subject':<40}")
        report.append("-" * 130)
        
        for inv in dir_invoices:
            date_str = inv['date'][:10] if inv['date'] else 'Unknown'
            inv_num = str(inv['invoice_number'])[:20]
            if inv['amount']:
                amount = f"{inv['currency']} {inv['amount']:,.2f}"
            else:
                amount = 'N/A'
            customer = inv['customer_vendor'][:30]
            subject = inv['subject'][:40]
            
            report.append(f"{date_str:<12} | {inv_num:<20} | {amount:<15} | {customer:<30} | {subject:<40}")
        
        report.append("")
    
    report.append("=" * 130)
    report.append("END OF REPORT")
    report.append("=" * 130)
    
    return '\n'.join(report)

def main():
    print("Extracting DFW Romania invoices...")
    invoices = extract_dfw_invoices()
    print(f"Found {len(invoices)} invoice-related records")
    
    report = generate_report(invoices)
    
    # Save to file
    with open('DFW_INVOICES.txt', 'w', encoding='utf-8') as f:
        f.write(report)
    
    print(f"Invoice list saved to DFW_INVOICES.txt")
    print("\n" + report)

if __name__ == "__main__":
    main()
