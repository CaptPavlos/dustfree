#!/usr/bin/env python3
"""
Script to check for missing DFW invoice numbers and display them visually in a web interface.
This helps identify gaps in the invoice sequence (1-90).
"""

import sqlite3
from flask import Flask, render_template_string
import re

app = Flask(__name__)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>DFW Invoice Number Checker</title>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
            background: #1a1a1a;
            color: #e0e0e0;
            margin: 20px;
            padding: 20px;
        }
        h1 {
            color: #4CAF50;
            margin-bottom: 30px;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
        }
        .summary {
            background: #2a2a2a;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 30px;
            border-left: 4px solid #4CAF50;
        }
        .summary h2 {
            margin-top: 0;
            color: #4CAF50;
        }
        .stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-top: 15px;
        }
        .stat {
            background: #333;
            padding: 15px;
            border-radius: 6px;
        }
        .stat-label {
            font-size: 12px;
            color: #999;
            text-transform: uppercase;
        }
        .stat-value {
            font-size: 24px;
            font-weight: bold;
            margin-top: 5px;
        }
        .grid {
            display: grid;
            grid-template-columns: repeat(10, 1fr);
            gap: 10px;
            margin-bottom: 30px;
        }
        .invoice-box {
            aspect-ratio: 1;
            display: flex;
            align-items: center;
            justify-content: center;
            border-radius: 6px;
            font-weight: bold;
            font-size: 14px;
            cursor: pointer;
            transition: transform 0.2s;
        }
        .invoice-box:hover {
            transform: scale(1.05);
        }
        .found {
            background: #4CAF50;
            color: white;
        }
        .missing {
            background: #f44336;
            color: white;
        }
        .duplicate {
            background: #FF9800;
            color: white;
        }
        .legend {
            display: flex;
            gap: 20px;
            margin-bottom: 20px;
            padding: 15px;
            background: #2a2a2a;
            border-radius: 6px;
        }
        .legend-item {
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .legend-box {
            width: 20px;
            height: 20px;
            border-radius: 4px;
        }
        .details {
            background: #2a2a2a;
            padding: 20px;
            border-radius: 8px;
            margin-top: 20px;
        }
        .details h3 {
            color: #4CAF50;
            margin-top: 0;
        }
        .invoice-list {
            max-height: 400px;
            overflow-y: auto;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 10px;
        }
        th, td {
            padding: 10px;
            text-align: left;
            border-bottom: 1px solid #444;
        }
        th {
            background: #333;
            position: sticky;
            top: 0;
        }
        .filename {
            font-family: monospace;
            font-size: 12px;
            color: #888;
        }
        .amount {
            color: #4CAF50;
            font-weight: bold;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>📊 DFW Invoice Number Checker</h1>
        
        <div class="summary">
            <h2>Summary</h2>
            <div class="stats">
                <div class="stat">
                    <div class="stat-label">Expected Range</div>
                    <div class="stat-value" style="color: #2196F3;">1 - 90</div>
                </div>
                <div class="stat">
                    <div class="stat-label">Found Invoices</div>
                    <div class="stat-value" style="color: #4CAF50;">{{ found_count }}</div>
                </div>
                <div class="stat">
                    <div class="stat-label">Missing</div>
                    <div class="stat-value" style="color: #f44336;">{{ missing_count }}</div>
                </div>
                <div class="stat">
                    <div class="stat-label">Duplicates</div>
                    <div class="stat-value" style="color: #FF9800;">{{ duplicate_count }}</div>
                </div>
            </div>
        </div>
        
        <div class="legend">
            <div class="legend-item">
                <div class="legend-box found"></div>
                <span>Found ({{ found_count }})</span>
            </div>
            <div class="legend-item">
                <div class="legend-box missing"></div>
                <span>Missing ({{ missing_count }})</span>
            </div>
            <div class="legend-item">
                <div class="legend-box duplicate"></div>
                <span>Duplicate ({{ duplicate_count }})</span>
            </div>
        </div>
        
        <h2>Invoice Number Grid (1-90)</h2>
        <div class="grid">
            {% for num in range(1, 91) %}
                {% if num in duplicates %}
                    <div class="invoice-box duplicate" title="Invoice #{{ num }} - Duplicate ({{ duplicates[num] }} times)">{{ num }}</div>
                {% elif num in found_invoices %}
                    <div class="invoice-box found" title="Invoice #{{ num }} - Found">{{ num }}</div>
                {% else %}
                    <div class="invoice-box missing" title="Invoice #{{ num }} - MISSING">{{ num }}</div>
                {% endif %}
            {% endfor %}
        </div>
        
        {% if missing_list %}
        <div class="details">
            <h3>🔴 Missing Invoice Numbers ({{ missing_count }})</h3>
            <p>{{ missing_list|join(', ') }}</p>
        </div>
        {% endif %}
        
        {% if duplicate_list %}
        <div class="details">
            <h3>🟠 Duplicate Invoice Numbers</h3>
            <table>
                <thead>
                    <tr>
                        <th>Invoice #</th>
                        <th>Count</th>
                        <th>Filenames</th>
                    </tr>
                </thead>
                <tbody>
                    {% for dup in duplicate_list %}
                    <tr>
                        <td><strong>{{ dup.number }}</strong></td>
                        <td>{{ dup.count }}</td>
                        <td class="filename">{{ dup.files|join(', ') }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
        {% endif %}
        
        <div class="details">
            <h3>✅ All Found DFW Invoices</h3>
            <div class="invoice-list">
                <table>
                    <thead>
                        <tr>
                            <th>Invoice #</th>
                            <th>Amount</th>
                            <th>Currency</th>
                            <th>Date</th>
                            <th>Filename</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for inv in all_invoices %}
                        <tr>
                            <td><strong>{{ inv.number }}</strong></td>
                            <td class="amount">{{ '%.2f'|format(inv.amount) if inv.amount else 'N/A' }}</td>
                            <td>{{ inv.currency or 'N/A' }}</td>
                            <td>{{ inv.date or 'N/A' }}</td>
                            <td class="filename">{{ inv.filename }}</td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>
    </div>
</body>
</html>
"""

def get_dfw_invoices():
    """Get all DFW invoices from the database."""
    conn = sqlite3.connect('email_archive.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT 
            pi.invoice_number,
            pi.invoice_date,
            pi.amount,
            pi.currency,
            pi.raw_text,
            a.filename
        FROM parsed_invoices pi
        JOIN attachments a ON pi.attachment_id = a.id
        JOIN emails e ON pi.email_id = e.id
        WHERE pi.raw_text LIKE '%DFW PROFESSIONAL%'
        ORDER BY pi.invoice_number
    ''')
    
    invoices = []
    for row in cursor.fetchall():
        raw_text = row['raw_text'] or ''
        raw_upper = raw_text.upper()
        filename = row['filename'] or ''
        
        # Skip excluded documents
        exclusions = ['PROFORMA', 'ORDIN DE PLATA', 'ROHEL TRANS', 'PACKING LIST']
        if any(excl in raw_upper or excl in filename.upper() for excl in exclusions):
            continue
        
        # Try to extract invoice number
        invoice_num = None
        
        # Pattern 1: INVOICE No format
        invoice_match = re.search(r'INVOICE\s*N[oOοº°]\.?\s*:?\s*(\d+)', raw_text, re.IGNORECASE)
        if invoice_match:
            invoice_num = int(invoice_match.group(1))
        else:
            # Pattern 2: From filename
            filename_match = re.search(r'[Ii]nvoice\s*[Nn][oOοº°]?\s*(\d+)', filename)
            if filename_match:
                invoice_num = int(filename_match.group(1))
        
        # Pattern 3: FACTURA format
        if not invoice_num and 'FACTURA' in raw_upper:
            factura_match = re.search(r'FACTURA\s+(?:FISCALĂ|SERIA)?\s*[\w\s]*?[:\s]*([A-Z]?\d+)', raw_text, re.IGNORECASE)
            if factura_match:
                num_str = factura_match.group(1)
                digit_match = re.search(r'(\d+)', num_str)
                if digit_match:
                    invoice_num = int(digit_match.group(1))
        
        if invoice_num and 1 <= invoice_num <= 90:
            invoices.append({
                'number': invoice_num,
                'amount': row['amount'],
                'currency': row['currency'],
                'date': row['invoice_date'],
                'filename': filename
            })
    
    conn.close()
    return invoices

@app.route('/')
def index():
    # Get all DFW invoices
    invoices = get_dfw_invoices()
    
    # Track found invoice numbers
    found_invoices = {}
    for inv in invoices:
        num = inv['number']
        if num not in found_invoices:
            found_invoices[num] = []
        found_invoices[num].append(inv)
    
    # Find missing numbers
    all_numbers = set(range(1, 91))
    found_numbers = set(found_invoices.keys())
    missing_numbers = sorted(all_numbers - found_numbers)
    
    # Find duplicates
    duplicates = {num: len(invs) for num, invs in found_invoices.items() if len(invs) > 1}
    duplicate_list = [
        {
            'number': num,
            'count': count,
            'files': [inv['filename'] for inv in found_invoices[num]]
        }
        for num, count in sorted(duplicates.items())
    ]
    
    # Flatten all invoices for display
    all_invoices = sorted(invoices, key=lambda x: x['number'])
    
    return render_template_string(
        HTML_TEMPLATE,
        found_count=len(found_numbers),
        missing_count=len(missing_numbers),
        duplicate_count=len(duplicates),
        found_invoices=found_numbers,
        missing_list=missing_numbers,
        duplicates=duplicates,
        duplicate_list=duplicate_list,
        all_invoices=all_invoices
    )

if __name__ == '__main__':
    print("=" * 60)
    print("DFW Invoice Number Checker")
    print("=" * 60)
    print("\nStarting web server on http://localhost:5001")
    print("Open this URL in your browser to see the visual invoice checker.")
    print("\nPress CTRL+C to stop the server.")
    print("=" * 60)
    app.run(host='0.0.0.0', port=5001, debug=True)
