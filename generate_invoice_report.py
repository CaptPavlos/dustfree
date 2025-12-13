#!/usr/bin/env python3
"""Generate comprehensive invoice report from parsed PDFs and email data."""

import sqlite3
from datetime import datetime
from collections import defaultdict

DB_PATH = 'email_archive.db'

def generate_report():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    report = []
    report.append("=" * 140)
    report.append("DFW PROFESSIONAL - COMPREHENSIVE INVOICE REPORT")
    report.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    report.append("=" * 140)
    
    # Get stats
    cursor.execute("SELECT COUNT(*) FROM emails")
    email_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM attachments")
    attachment_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM parsed_invoices")
    invoice_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM attachments WHERE filename LIKE '%.pdf'")
    pdf_count = cursor.fetchone()[0]
    
    report.append(f"\nTotal Emails: {email_count}")
    report.append(f"Total Attachments: {attachment_count}")
    report.append(f"PDF Attachments: {pdf_count}")
    report.append(f"Parsed Invoices from PDFs: {invoice_count}")
    report.append("")
    
    # Get parsed invoices with amounts
    cursor.execute('''
        SELECT 
            pi.invoice_number,
            pi.invoice_date,
            pi.amount,
            pi.currency,
            pi.vendor,
            a.filename,
            a.file_path,
            e.date_received,
            e.from_address,
            e.subject
        FROM parsed_invoices pi
        JOIN attachments a ON pi.attachment_id = a.id
        JOIN emails e ON pi.email_id = e.id
        WHERE pi.amount IS NOT NULL AND pi.amount > 0
        ORDER BY e.date_received ASC
    ''')
    
    invoices_with_amounts = cursor.fetchall()
    
    # Summary by currency
    currency_totals = defaultdict(float)
    for inv in invoices_with_amounts:
        currency_totals[inv['currency']] += inv['amount']
    
    report.append("=" * 140)
    report.append("SUMMARY BY CURRENCY (from parsed PDFs with amounts)")
    report.append("-" * 60)
    for currency, total in sorted(currency_totals.items()):
        report.append(f"  {currency}: {total:,.2f}")
    report.append("")
    
    # Summary by year
    year_totals = defaultdict(lambda: defaultdict(float))
    for inv in invoices_with_amounts:
        date_str = inv['date_received']
        if date_str:
            year = date_str[:4]
            year_totals[year][inv['currency']] += inv['amount']
    
    report.append("SUMMARY BY YEAR:")
    report.append("-" * 60)
    for year in sorted(year_totals.keys()):
        totals = year_totals[year]
        totals_str = ", ".join(f"{curr} {amt:,.2f}" for curr, amt in totals.items())
        report.append(f"  {year}: {totals_str}")
    report.append("")
    
    # Summary by sender/vendor
    sender_totals = defaultdict(lambda: {'count': 0, 'eur': 0, 'ron': 0})
    for inv in invoices_with_amounts:
        from_addr = inv['from_address'] or 'Unknown'
        # Extract name or email
        if '<' in from_addr:
            sender = from_addr.split('<')[0].strip().strip('"')
        else:
            sender = from_addr
        sender = sender[:40]
        
        sender_totals[sender]['count'] += 1
        if inv['currency'] == 'EUR':
            sender_totals[sender]['eur'] += inv['amount']
        else:
            sender_totals[sender]['ron'] += inv['amount']
    
    report.append("TOP SENDERS BY INVOICE COUNT:")
    report.append("-" * 100)
    for sender, data in sorted(sender_totals.items(), key=lambda x: -x[1]['count'])[:30]:
        eur_str = f"EUR {data['eur']:,.2f}" if data['eur'] > 0 else ""
        ron_str = f"RON {data['ron']:,.2f}" if data['ron'] > 0 else ""
        total_str = f"{eur_str} {ron_str}".strip()
        report.append(f"  {sender:<40} | {data['count']:>3} invoices | {total_str}")
    report.append("")
    
    # Detailed invoice list
    report.append("=" * 140)
    report.append("DETAILED INVOICE LIST (Parsed from PDF Attachments)")
    report.append("=" * 140)
    report.append("")
    report.append(f"{'Date':<12} | {'Invoice #':<20} | {'Amount':<18} | {'Filename':<40} | {'Sender':<30}")
    report.append("-" * 140)
    
    for inv in invoices_with_amounts:
        date_str = inv['date_received'][:10] if inv['date_received'] else 'Unknown'
        inv_num = str(inv['invoice_number'] or 'N/A')[:20]
        amount = f"{inv['currency']} {inv['amount']:,.2f}"
        filename = str(inv['filename'] or '')[:40]
        
        from_addr = inv['from_address'] or 'Unknown'
        if '<' in from_addr:
            sender = from_addr.split('<')[0].strip().strip('"')[:30]
        else:
            sender = from_addr[:30]
        
        report.append(f"{date_str:<12} | {inv_num:<20} | {amount:<18} | {filename:<40} | {sender:<30}")
    
    report.append("")
    
    # Also get invoices from email subjects/body (not just PDFs)
    report.append("=" * 140)
    report.append("INVOICE-RELATED EMAILS (by subject)")
    report.append("=" * 140)
    
    cursor.execute('''
        SELECT id, subject, from_address, date_received
        FROM emails
        WHERE subject LIKE '%invoice%' 
           OR subject LIKE '%factura%'
           OR subject LIKE '%Invoice%'
           OR subject LIKE '%Factura%'
        ORDER BY date_received ASC
    ''')
    
    invoice_emails = cursor.fetchall()
    report.append(f"\nTotal invoice-related emails: {len(invoice_emails)}")
    report.append("")
    report.append(f"{'Date':<12} | {'Subject':<60} | {'From':<40}")
    report.append("-" * 140)
    
    for email in invoice_emails:
        date_str = email['date_received'][:10] if email['date_received'] else 'Unknown'
        subject = str(email['subject'] or '')[:60]
        
        from_addr = email['from_address'] or 'Unknown'
        if '<' in from_addr:
            sender = from_addr.split('<')[0].strip().strip('"')[:40]
        else:
            sender = from_addr[:40]
        
        report.append(f"{date_str:<12} | {subject:<60} | {sender:<40}")
    
    report.append("")
    report.append("=" * 140)
    report.append("END OF REPORT")
    report.append("=" * 140)
    
    conn.close()
    return '\n'.join(report)

def main():
    print("Generating comprehensive invoice report...")
    report = generate_report()
    
    with open('COMPREHENSIVE_INVOICE_REPORT.txt', 'w', encoding='utf-8') as f:
        f.write(report)
    
    print("Report saved to COMPREHENSIVE_INVOICE_REPORT.txt")
    print("\n" + report[:5000])

if __name__ == "__main__":
    main()
