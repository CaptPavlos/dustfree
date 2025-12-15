import os
import sqlite3
import json
import re
from flask import Flask, render_template, request, jsonify, redirect, session, url_for
from flask_compress import Compress
from collections import defaultdict
import requests
import threading
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev-secret-key-change-in-production')

# Enable gzip compression for responses
Compress(app)
app.config['COMPRESS_MIMETYPES'] = ['text/html', 'text/css', 'text/javascript', 'application/json', 'application/javascript']
app.config['COMPRESS_LEVEL'] = 6
app.config['COMPRESS_MIN_SIZE'] = 500

# Google Calendar API configuration
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY', '')
GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_OAUTH_CLIENT_ID', '')
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_OAUTH_CLIENT_SECRET', '')
GOOGLE_REDIRECT_URI = os.environ.get('GOOGLE_REDIRECT_URI', 'http://localhost:8080/api/calendar/callback')
GOOGLE_SCOPES = ['https://www.googleapis.com/auth/calendar.readonly', 'https://www.googleapis.com/auth/tasks.readonly']
GOOGLE_TOKEN_FILE = 'google_tokens.json'

def save_google_tokens(tokens):
    """Save Google tokens to file."""
    with open(GOOGLE_TOKEN_FILE, 'w') as f:
        json.dump(tokens, f)

def load_google_tokens():
    """Load Google tokens from file."""
    if os.path.exists(GOOGLE_TOKEN_FILE):
        with open(GOOGLE_TOKEN_FILE, 'r') as f:
            return json.load(f)
    return None

def get_google_access_token():
    """Get valid Google access token, refreshing if needed."""
    tokens = load_google_tokens()
    if not tokens:
        return None
    return tokens.get('access_token')

# Disable caching for development
@app.after_request
def add_header(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

# Perplexity API configuration
PERPLEXITY_API_KEY = os.environ.get('PERPLEXITY_API_KEY', '')
PERPLEXITY_API_URL = "https://api.perplexity.ai/chat/completions"

DB_PATH = 'email_archive.db'

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_base_tables():
    """Create base tables if they don't exist (for fresh deployments)."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        # Emails table
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
                file_path TEXT,
                file_size INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (email_id) REFERENCES emails(id)
            )
        ''')
        # Parsed invoices table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS parsed_invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attachment_id INTEGER,
                invoice_number TEXT,
                invoice_date TEXT,
                total_amount REAL,
                currency TEXT,
                vendor TEXT,
                raw_text TEXT,
                parsed_data TEXT,
                amount_edited INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (attachment_id) REFERENCES attachments(id)
            )
        ''')
        # App settings table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Create indexes
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_emails_from ON emails(from_address)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_emails_date ON emails(date_received DESC)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_attachments_email ON attachments(email_id)')
        conn.commit()

def init_read_status_table():
    """Create table to track read status of emails."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS email_read_status (
                email_id INTEGER PRIMARY KEY,
                is_read INTEGER DEFAULT 0,
                read_at DATETIME
            )
        ''')
        conn.commit()

def init_entity_categories_table():
    """Create table to store user-defined entity category overrides."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS entity_categories (
                domain TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Table for organization files
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS organization_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                domain TEXT NOT NULL,
                attachment_id INTEGER,
                filename TEXT,
                file_path TEXT,
                notes TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (attachment_id) REFERENCES attachments(id)
            )
        ''')
        
        # Table for organization names (aliases)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS organization_names (
                domain TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Table for email-to-organization assignments
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS email_organization_assignments (
                email_address TEXT PRIMARY KEY,
                organization_domain TEXT NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Table for organization relationships
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS organization_relationships (
                domain TEXT PRIMARY KEY,
                related_domain TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Performance indexes
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_attachments_email ON attachments(email_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_attachments_filename ON attachments(filename)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_org_files_domain ON organization_files(domain)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_production_runs_client ON production_runs(client)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_production_runs_status ON production_runs(status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_emails_subject ON emails(subject)')
        
        conn.commit()

def add_amount_edited_column():
    """Add amount_edited column to parsed_invoices table if it doesn't exist."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute('ALTER TABLE parsed_invoices ADD COLUMN amount_edited INTEGER DEFAULT 0')
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists

def init_production_tables():
    """Create tables for production tracking."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        # Production feedback table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS production_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                status TEXT DEFAULT 'pending',
                files TEXT,
                feedback_date DATE,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Add feedback_date column if not exists
        try:
            cursor.execute('ALTER TABLE production_feedback ADD COLUMN feedback_date DATE')
        except sqlite3.OperationalError:
            pass
        # Production runs table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS production_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client TEXT,
                order_ref TEXT,
                product TEXT,
                quantity INTEGER,
                status TEXT DEFAULT 'pending',
                notes TEXT,
                scheduled_month TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Add new columns if they don't exist
        new_columns = [
            ('client', 'TEXT'),
            ('scheduled_month', 'TEXT'),
            ('updated_at', 'DATETIME DEFAULT CURRENT_TIMESTAMP'),
            ('eta_month', 'TEXT'),
            ('date_ordered', 'DATE'),
            ('downpayment_paid', 'INTEGER DEFAULT 0'),
            ('date_prod_start', 'DATE'),
            ('date_prod_end', 'DATE'),
            ('date_warehouse', 'DATE'),
            ('paid_off', 'INTEGER DEFAULT 0'),
            ('date_delivered', 'DATE'),
            ('price_per_roll', 'REAL DEFAULT 0'),
            ('cost_per_roll', 'REAL DEFAULT 0')
        ]
        for col_name, col_type in new_columns:
            try:
                cursor.execute(f'ALTER TABLE production_runs ADD COLUMN {col_name} {col_type}')
            except sqlite3.OperationalError:
                pass
        conn.commit()

def init_products_clients_tables():
    """Create tables for products and clients management."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        # Products table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT,
                price REAL DEFAULT 0,
                notes TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Add price and notes columns if they don't exist
        for col_name, col_type in [('price', 'REAL DEFAULT 0'), ('notes', 'TEXT')]:
            try:
                cursor.execute(f'ALTER TABLE products ADD COLUMN {col_name} {col_type}')
            except sqlite3.OperationalError:
                pass
        # Clients table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                contact_info TEXT,
                billing_address TEXT,
                shipping_address TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Add billing/shipping/country columns if they don't exist
        for col_name in ['billing_address', 'shipping_address', 'country']:
            try:
                cursor.execute(f'ALTER TABLE clients ADD COLUMN {col_name} TEXT')
            except sqlite3.OperationalError:
                pass
        # Client-Product pricing table (client-specific prices)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS client_product_prices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                price REAL NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (client_id) REFERENCES clients(id),
                FOREIGN KEY (product_id) REFERENCES products(id),
                UNIQUE(client_id, product_id)
            )
        ''')
        # App settings table for persisting sync status etc.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()

def get_setting(key, default=None):
    """Get a setting from the database."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT value FROM app_settings WHERE key = ?', (key,))
        row = cursor.fetchone()
        return row[0] if row else default

def set_setting(key, value):
    """Set a setting in the database."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO app_settings (key, value, updated_at) 
            VALUES (?, ?, CURRENT_TIMESTAMP)
        ''', (key, value))
        conn.commit()

# Initialize tables
init_base_tables()
init_read_status_table()
init_production_tables()
init_entity_categories_table()
add_amount_edited_column()
init_products_clients_tables()

# Initialize ChromaDB
def init_chromadb():
    """Initialize ChromaDB for semantic search."""
    try:
        import chromadb
        from chromadb.utils import embedding_functions
        
        # Create persistent client
        chroma_client = chromadb.PersistentClient(path="./chroma_db")
        
        # Use sentence transformers for embeddings (runs locally, no API calls)
        embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="all-MiniLM-L6-v2"  # Fast, lightweight model
        )
        
        # Get or create collections
        emails_collection = chroma_client.get_or_create_collection(
            name="emails",
            embedding_function=embedding_fn,
            metadata={"description": "Email subjects and bodies for semantic search"}
        )
        
        invoices_collection = chroma_client.get_or_create_collection(
            name="invoices",
            embedding_function=embedding_fn,
            metadata={"description": "Invoice OCR text for semantic search"}
        )
        
        return chroma_client, emails_collection, invoices_collection
    except ImportError:
        print("ChromaDB not installed. Install with: pip install chromadb sentence-transformers")
        return None, None, None

chroma_client, emails_collection, invoices_collection = init_chromadb()

def extract_entities_and_orders():
    """Extract all entities with their emails and orders."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT id, subject, from_address, to_address, date_received, body_text 
        FROM emails 
        ORDER BY date_received DESC
    ''')
    
    emails = cursor.fetchall()
    conn.close()
    
    # Entity extraction
    entities = defaultdict(lambda: {
        'name': '',
        'email': '',
        'company': '',
        'orders': [],
        'last_contact': '',
        'email_count': 0,
        'statuses': set()
    })
    
    # Order patterns
    order_patterns = [
        r'order\s*#?\s*(\d{4,})',
        r'order\s*number[:\s]*(\d{4,})',
        r'po[:\s#]*(\d{4,})',
        r'invoice[:\s#]*(\d{4,})',
        r'#(\d{5,})',
    ]
    
    # Status keywords
    status_keywords = {
        'pending': ['pending', 'waiting', 'on hold', 'processing'],
        'shipped': ['shipped', 'dispatched', 'sent', 'delivered', 'tracking'],
        'cancelled': ['cancelled', 'canceled', 'refund'],
        'problem': ['problem', 'issue', 'error', 'failed', 'delay', 'urgent', 'asap'],
        'confirmed': ['confirmed', 'confirmation', 'approved'],
        'payment': ['payment', 'paid', 'invoice'],
    }
    
    for email in emails:
        from_addr = email['from_address'] or ''
        subject = email['subject'] or ''
        body = email['body_text'] or ''
        text = f"{subject} {body}".lower()
        
        # Extract email address
        email_match = re.search(r'<([^>]+@[^>]+)>', from_addr)
        if email_match:
            addr = email_match.group(1).lower()
        else:
            addr_match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', from_addr)
            addr = addr_match.group(0).lower() if addr_match else from_addr.lower()
        
        # Skip system emails
        if any(x in addr for x in ['mailer-daemon', 'noreply', 'no-reply', 'accounts.google']):
            continue
        
        # Extract name
        name_match = re.match(r'^([^<]+)', from_addr)
        name = name_match.group(1).strip().strip('"') if name_match else addr.split('@')[0]
        
        # Extract company from domain
        domain = addr.split('@')[-1] if '@' in addr else ''
        company = domain.split('.')[0].title() if domain else ''
        
        # Update entity
        entities[addr]['email'] = addr
        entities[addr]['name'] = name
        entities[addr]['company'] = company
        entities[addr]['email_count'] += 1
        
        if not entities[addr]['last_contact'] or email['date_received'] > entities[addr]['last_contact']:
            entities[addr]['last_contact'] = email['date_received']
        
        # Extract orders
        for pattern in order_patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            for order_num in matches:
                if order_num not in [o['number'] for o in entities[addr]['orders']]:
                    # Determine status
                    order_statuses = []
                    for status, keywords in status_keywords.items():
                        if any(kw in text for kw in keywords):
                            order_statuses.append(status)
                    
                    entities[addr]['orders'].append({
                        'number': order_num,
                        'subject': subject[:100],
                        'statuses': order_statuses or ['unknown']
                    })
                    entities[addr]['statuses'].update(order_statuses)
    
    # Convert to list and sort by email count
    result = []
    for addr, data in entities.items():
        data['statuses'] = list(data['statuses'])
        result.append(data)
    
    result.sort(key=lambda x: x['email_count'], reverse=True)
    return result

def get_email_context(limit=50):
    """Get recent emails as context for the AI."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT subject, from_address, to_address, date_received, body_text 
        FROM emails 
        ORDER BY date_received DESC 
        LIMIT ?
    ''', (limit,))
    
    emails = cursor.fetchall()
    conn.close()
    
    context = "Here are the recent emails from the DFW Professional sales inbox:\n\n"
    for i, email in enumerate(emails, 1):
        context += f"--- Email {i} ---\n"
        context += f"From: {email['from_address']}\n"
        context += f"To: {email['to_address']}\n"
        context += f"Date: {email['date_received']}\n"
        context += f"Subject: {email['subject']}\n"
        body = (email['body_text'] or '')[:500]
        context += f"Body: {body}...\n\n"
    
    return context

def ask_perplexity(question, context):
    """Send a question to Perplexity API."""
    headers = {
        "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
        "Content-Type": "application/json"
    }
    
    system_prompt = """You are an AI assistant analyzing business emails for DFW Professional, 
a company dealing with industrial products, shipping, and B2B orders. 
Answer questions based on the email data provided. Be specific about order numbers, 
dates, people involved, and action items. If you don't have enough information, say so."""
    
    payload = {
        "model": "sonar-pro",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"{context}\n\nQuestion: {question}"}
        ],
        "max_tokens": 1000,
        "temperature": 0.2
    }
    
    try:
        response = requests.post(PERPLEXITY_API_URL, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        return data['choices'][0]['message']['content']
    except Exception as e:
        return f"Error querying Perplexity API: {str(e)}"

def search_emails(query):
    """Search emails for specific terms."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT id, subject, from_address, date_received, body_text 
        FROM emails 
        WHERE subject LIKE ? OR body_text LIKE ? OR from_address LIKE ?
        ORDER BY date_received DESC 
        LIMIT 20
    ''', (f'%{query}%', f'%{query}%', f'%{query}%'))
    
    results = cursor.fetchall()
    conn.close()
    
    return [dict(row) for row in results]

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/entities')
def get_entities():
    entities = extract_entities_and_orders()
    return jsonify(entities)

@app.route('/api/organisations')
def get_organisations():
    """Get customer organisations grouped by domain with billing/shipping details."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Get category overrides to identify customers
    cursor.execute('SELECT domain, category FROM entity_categories')
    category_overrides = {row['domain']: row['category'] for row in cursor.fetchall()}
    
    entities = extract_entities_and_orders()
    
    # Group by domain - only include customers
    organisations = {}
    for entity in entities:
        email = entity.get('email', '')
        if '@' not in email:
            continue
        domain = email.split('@')[-1]
        
        # Check if this domain is a customer
        category = category_overrides.get(domain, 'other')
        if category != 'customers':
            continue
        
        if domain not in organisations:
            organisations[domain] = {
                'domain': domain,
                'name': domain.split('.')[0].upper(),
                'contacts': [],
                'total_emails': 0,
                'total_orders': 0
            }
        
        organisations[domain]['contacts'].append(entity)
        organisations[domain]['total_emails'] += entity.get('email_count', 0)
        organisations[domain]['total_orders'] += len(entity.get('orders', []))
    
    # Get saved billing/shipping details from database
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS organisation_details (
            domain TEXT PRIMARY KEY,
            billing_address TEXT,
            shipping_address TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('SELECT domain, billing_address, shipping_address FROM organisation_details')
    saved_details = {row['domain']: {'billing': row['billing_address'], 'shipping': row['shipping_address']} 
                     for row in cursor.fetchall()}
    conn.close()
    
    # Add saved details to organisations
    result = []
    for domain, org in organisations.items():
        if domain in saved_details:
            org['billing_address'] = saved_details[domain]['billing']
            org['shipping_address'] = saved_details[domain]['shipping']
        result.append(org)
    
    # Sort by total orders
    result.sort(key=lambda x: x['total_orders'], reverse=True)
    return jsonify(result)

@app.route('/api/organisation/details', methods=['POST'])
def save_organisation_details():
    """Save billing/shipping details for an organisation."""
    data = request.get_json() or {}
    domain = data.get('domain')
    billing = data.get('billing_address', '')
    shipping = data.get('shipping_address', '')
    
    if not domain:
        return jsonify({'error': 'Domain required'}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS organisation_details (
            domain TEXT PRIMARY KEY,
            billing_address TEXT,
            shipping_address TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        INSERT OR REPLACE INTO organisation_details (domain, billing_address, shipping_address, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
    ''', (domain, billing, shipping))
    
    conn.commit()
    conn.close()
    
    return jsonify({'success': True})

@app.route('/api/ask', methods=['POST'])
def ask_question():
    data = request.json
    question = data.get('question', '')
    greek_mode = data.get('greek_mode', False)
    
    if not question:
        return jsonify({'error': 'No question provided'}), 400
    
    # Get email context
    context = get_email_context(limit=30)
    
    # Also search for relevant emails
    search_results = search_emails(question)
    if search_results:
        context += "\n\nRelevant emails found:\n"
        for email in search_results[:10]:
            context += f"- {email['subject']} (from {email['from_address']})\n"
    
    # Get additional data context (products, invoices, production runs, etc.)
    related_data = []
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Search products
    cursor.execute('SELECT * FROM products WHERE name LIKE ? OR description LIKE ? OR notes LIKE ?', 
                   (f'%{question}%', f'%{question}%', f'%{question}%'))
    products = cursor.fetchall()
    for p in products:
        related_data.append({'type': 'product', 'name': p['name'], 'price': p['price'], 'description': p['description']})
        context += f"\nProduct: {p['name']} - €{p['price'] or 0} - {p['description'] or ''}\n"
    
    # Search production runs
    cursor.execute('SELECT * FROM production_runs WHERE client LIKE ? OR product LIKE ? OR order_ref LIKE ? OR notes LIKE ?',
                   (f'%{question}%', f'%{question}%', f'%{question}%', f'%{question}%'))
    runs = cursor.fetchall()
    for r in runs:
        related_data.append({'type': 'production', 'title': f"{r['client']} - {r['product']}", 'order_ref': r['order_ref']})
        context += f"\nProduction Run: {r['client']} - {r['product']} - Ref: {r['order_ref']} - Qty: {r['quantity']}\n"
    
    # Search invoices
    try:
        cursor.execute('SELECT * FROM invoices WHERE invoice_number LIKE ? OR bill_to LIKE ? OR notes LIKE ?',
                       (f'%{question}%', f'%{question}%', f'%{question}%'))
        invoices = cursor.fetchall()
        for inv in invoices:
            related_data.append({'type': 'invoice', 'title': inv['invoice_number'], 'total': inv['total']})
            context += f"\nInvoice: {inv['invoice_number']} - €{inv['total'] or 0} - {inv['bill_to'] or ''}\n"
    except:
        pass  # Table may not exist yet
    
    # Search proforma invoices
    try:
        cursor.execute('SELECT * FROM proforma_invoices WHERE invoice_number LIKE ? OR bill_to LIKE ? OR notes LIKE ?',
                       (f'%{question}%', f'%{question}%', f'%{question}%'))
        proformas = cursor.fetchall()
        for pf in proformas:
            related_data.append({'type': 'proforma', 'title': pf['invoice_number'], 'total': pf['total']})
            context += f"\nPro Forma: {pf['invoice_number']} - €{pf['total'] or 0} - {pf['bill_to'] or ''}\n"
    except:
        pass  # Table may not exist yet
    
    # Search clients
    cursor.execute('SELECT * FROM clients WHERE name LIKE ? OR contact_info LIKE ?',
                   (f'%{question}%', f'%{question}%'))
    clients = cursor.fetchall()
    for c in clients:
        related_data.append({'type': 'client', 'name': c['name']})
        context += f"\nClient: {c['name']} - {c['contact_info'] or ''}\n"
    
    conn.close()
    
    # Modify question for Greek response if needed
    if greek_mode:
        question = f"{question}\n\nPlease respond in Greek (Ελληνικά)."
    
    # Ask Perplexity
    answer = ask_perplexity(question, context)
    
    return jsonify({
        'question': question,
        'answer': answer,
        'related_emails': search_results[:5],
        'related_data': related_data[:10]
    })

@app.route('/api/search', methods=['GET'])
def search():
    query = request.args.get('q', '')
    if not query:
        return jsonify([])
    
    results = search_emails(query)
    return jsonify(results)

@app.route('/api/emails', methods=['GET'])
def get_all_emails():
    """Get all emails sorted by date with read status."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Add folder column if it doesn't exist
    try:
        cursor.execute('ALTER TABLE emails ADD COLUMN folder TEXT DEFAULT "INBOX"')
        conn.commit()
    except:
        pass
    
    cursor.execute('''
        SELECT e.id, e.subject, e.from_address, e.to_address, e.date_received,
               COALESCE(r.is_read, 0) as is_read, COALESCE(e.folder, 'INBOX') as folder
        FROM emails e
        LEFT JOIN email_read_status r ON e.id = r.email_id
        ORDER BY e.date_received DESC
    ''')
    
    emails = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify(emails)

@app.route('/api/emails/<int:email_id>/read', methods=['POST'])
def mark_email_read(email_id):
    """Toggle read status of an email."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Check current status
    cursor.execute('SELECT is_read FROM email_read_status WHERE email_id = ?', (email_id,))
    row = cursor.fetchone()
    
    if row:
        new_status = 0 if row['is_read'] else 1
        cursor.execute('UPDATE email_read_status SET is_read = ?, read_at = CURRENT_TIMESTAMP WHERE email_id = ?', 
                       (new_status, email_id))
    else:
        cursor.execute('INSERT INTO email_read_status (email_id, is_read, read_at) VALUES (?, 1, CURRENT_TIMESTAMP)', 
                       (email_id,))
        new_status = 1
    
    conn.commit()
    conn.close()
    return jsonify({'email_id': email_id, 'is_read': new_status})

@app.route('/api/entity-relationships')
def get_entity_relationships():
    """Get entities categorized by their relationship to the company."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT id, subject, from_address, to_address, body_text, date_received
        FROM emails 
        ORDER BY date_received DESC
    ''')
    
    emails = cursor.fetchall()
    
    # Load user-defined category overrides
    cursor.execute('SELECT domain, category FROM entity_categories')
    category_overrides = {row['domain']: row['category'] for row in cursor.fetchall()}
    
    conn.close()
    
    # Categorize entities based on email content and domain
    categories = {
        'customers': {},
        'transport': {},
        'suppliers': {},
        'taxation': {},
        'legal': {},
        'internal': {},
        'other': {}
    }
    
    # Keywords for categorization
    # NOTE: Our only real suppliers are Rotopak IKE and Central Pack (for cardboard boxes)
    # Most other companies are our customers
    category_keywords = {
        'transport': ['shipping', 'freight', 'cargo', 'transport', 'delivery', 'shipment', 'logistics', 'forwarding', 'customs', 'export', 'import', 'delamode', 'gava', 'dimensions', 'cargologistix'],
        'taxation': ['tax', 'vat', 'payment', 'accounting', 'fiscal', 'smarttax', 'bank'],
        'legal': ['lawyer', 'legal', 'contract', 'patent', 'agreement', 'law'],
        'suppliers': ['rotopak', 'central pack', 'centralpack'],
        'customers': ['order', 'purchase', 'buy', 'customer', 'jtape', 'dtc', 'baxt', 'bodyshop', 'bellini', 'bolest', 'orbit', 'streem', 'gyso', 'amba', 'rohel']
    }
    
    # Known domain categorizations
    # NOTE: Rotopak IKE and Central Pack are our only real suppliers
    domain_categories = {
        'delamode-group.com': 'transport',
        'gavagroup.com': 'transport',
        'dimensions-forwarding.com': 'transport',
        'cargologistix-forwarding.ro': 'transport',
        'mtrading.ro': 'transport',
        'hartrodt.com': 'transport',
        'klgeurope.com': 'transport',
        'geis-group.de': 'transport',
        'orbit-streem.com': 'customers',
        'rohel.ro': 'customers',
        'gyso.ch': 'customers',
        'smarttax.ro': 'taxation',
        'librabank.ro': 'taxation',
        'customs.ro': 'taxation',
        'jtape.com': 'customers',
        'dtc-uk.com': 'customers',
        'baxt-products.com': 'customers',
        'bodyshopaustralia.com.au': 'customers',
        'bellinisystems.it': 'customers',
        'bolest.se': 'customers',
        'amba.co.uk': 'customers',
        'rotopak.gr': 'suppliers',
        'centralpack.gr': 'suppliers',
        'ntova.gr': 'legal',
        'dfwprofessional.eu': 'internal'
    }
    
    for email in emails:
        from_addr = email['from_address'] or ''
        body = (email['body_text'] or '').lower()
        subject = (email['subject'] or '').lower()
        text = f"{subject} {body}"
        
        # Extract email
        email_match = re.search(r'<([^>]+@[^>]+)>', from_addr)
        if email_match:
            addr = email_match.group(1).lower()
        else:
            addr_match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', from_addr)
            addr = addr_match.group(0).lower() if addr_match else ''
        
        if not addr or any(x in addr for x in ['mailer-daemon', 'noreply', 'no-reply', 'accounts.google']):
            continue
        
        domain = addr.split('@')[-1] if '@' in addr else ''
        
        # Extract name
        name_match = re.match(r'^([^<]+)', from_addr)
        name = name_match.group(1).strip().strip('"') if name_match else addr.split('@')[0]
        
        # Determine category
        category = 'other'
        
        # First check user overrides
        if domain in category_overrides:
            category = category_overrides[domain]
        else:
            # Then check default domain categories
            for dom, cat in domain_categories.items():
                if dom in domain:
                    category = cat
                    break
            
            # If still other, check keywords
            if category == 'other':
                for cat, keywords in category_keywords.items():
                    if any(kw in text or kw in domain for kw in keywords):
                        category = cat
                        break
        
        # Add to category
        if addr not in categories[category]:
            categories[category][addr] = {
                'email': addr,
                'name': name,
                'domain': domain,
                'email_count': 0,
                'first_contact': email['date_received'],
                'last_contact': email['date_received']
            }
        
        categories[category][addr]['email_count'] += 1
        if email['date_received']:
            if email['date_received'] < categories[category][addr]['first_contact']:
                categories[category][addr]['first_contact'] = email['date_received']
            if email['date_received'] > categories[category][addr]['last_contact']:
                categories[category][addr]['last_contact'] = email['date_received']
    
    # Convert to list format
    result = {}
    for cat, entities in categories.items():
        result[cat] = sorted(entities.values(), key=lambda x: x['email_count'], reverse=True)
    
    return jsonify(result)

@app.route('/api/email/<int:email_id>')
def get_email(email_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM emails WHERE id = ?', (email_id,))
    email = cursor.fetchone()
    conn.close()
    
    if email:
        return jsonify(dict(email))
    return jsonify({'error': 'Email not found'}), 404

@app.route('/api/stats')
def get_stats():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('SELECT COUNT(*) as total FROM emails')
    total = cursor.fetchone()['total']
    
    cursor.execute('SELECT COUNT(DISTINCT from_address) as contacts FROM emails')
    contacts = cursor.fetchone()['contacts']
    
    cursor.execute('''
        SELECT from_address, COUNT(*) as count 
        FROM emails 
        GROUP BY from_address 
        ORDER BY count DESC 
        LIMIT 10
    ''')
    top_senders = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    
    return jsonify({
        'total_emails': total,
        'unique_contacts': contacts,
        'top_senders': top_senders
    })

@app.route('/api/invoices')
def get_invoices():
    """Get all parsed invoices from PDF attachments."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Get invoices from parsed_invoices table (from PDF attachments)
    cursor.execute('''
        SELECT 
            pi.id,
            pi.invoice_number,
            pi.invoice_date,
            pi.amount,
            pi.currency,
            pi.vendor,
            pi.email_id,
            a.filename,
            a.file_path,
            a.id as attachment_id,
            e.date_received,
            e.from_address,
            e.subject
        FROM parsed_invoices pi
        JOIN attachments a ON pi.attachment_id = a.id
        JOIN emails e ON pi.email_id = e.id
        ORDER BY e.date_received DESC
    ''')
    
    parsed_invoices = []
    for row in cursor.fetchall():
        from_addr = row['from_address'] or ''
        # Extract sender name
        name_match = re.match(r'^([^<]+)', from_addr)
        sender = name_match.group(1).strip().strip('"') if name_match else from_addr.split('@')[0] if '@' in from_addr else from_addr
        
        parsed_invoices.append({
            'id': row['id'],
            'invoice_number': row['invoice_number'] or 'N/A',
            'invoice_date': row['invoice_date'] or '',
            'amount': row['amount'],
            'currency': row['currency'] or 'EUR',
            'vendor': row['vendor'] or '',
            'filename': row['filename'],
            'file_path': row['file_path'],
            'email_date': row['date_received'][:10] if row['date_received'] else '',
            'sender': sender[:40],
            'subject': row['subject'] or '',
            'email_id': row['email_id'],
            'attachment_id': row['attachment_id']
        })
    
    # Also get invoice-related emails (from subject)
    cursor.execute('''
        SELECT id, subject, from_address, date_received, body_text
        FROM emails
        WHERE subject LIKE '%invoice%' 
           OR subject LIKE '%factura%'
           OR subject LIKE '%Invoice%'
           OR subject LIKE '%Factura%'
        ORDER BY date_received DESC
    ''')
    
    email_invoices = []
    for row in cursor.fetchall():
        from_addr = row['from_address'] or ''
        name_match = re.match(r'^([^<]+)', from_addr)
        sender = name_match.group(1).strip().strip('"') if name_match else from_addr.split('@')[0] if '@' in from_addr else from_addr
        
        # Try to extract invoice number from subject
        inv_match = re.search(r'(?:invoice|factura|inv)[#:\s]*([A-Z0-9\-/]+)', row['subject'] or '', re.IGNORECASE)
        inv_number = inv_match.group(1) if inv_match else 'N/A'
        
        # Try to extract amount
        amount = None
        currency = 'EUR'
        text = (row['subject'] or '') + ' ' + (row['body_text'] or '')
        amt_match = re.search(r'[€$]?\s*([\d,]+\.?\d*)\s*(?:EUR|RON|€)?', text)
        if amt_match:
            try:
                amount = float(amt_match.group(1).replace(',', ''))
            except:
                pass
        
        email_invoices.append({
            'id': row['id'],
            'invoice_number': inv_number,
            'email_date': row['date_received'][:10] if row['date_received'] else '',
            'sender': sender[:40],
            'subject': row['subject'] or '',
            'amount': amount,
            'currency': currency,
            'source': 'email'
        })
    
    # Get summary stats
    cursor.execute('SELECT SUM(amount) as total, currency FROM parsed_invoices WHERE amount IS NOT NULL GROUP BY currency')
    totals = {row['currency']: row['total'] for row in cursor.fetchall()}
    
    cursor.execute('SELECT COUNT(*) as count FROM parsed_invoices')
    pdf_count = cursor.fetchone()['count']
    
    cursor.execute('SELECT COUNT(*) as count FROM attachments WHERE filename LIKE "%.pdf"')
    attachment_count = cursor.fetchone()['count']
    
    conn.close()
    
    return jsonify({
        'parsed_invoices': parsed_invoices,
        'email_invoices': email_invoices[:100],  # Limit email invoices
        'summary': {
            'pdf_invoices': pdf_count,
            'pdf_attachments': attachment_count,
            'totals': totals
        }
    })

@app.route('/api/invoices/dfw')
def get_dfw_invoices():
    """Get invoices issued by DFW PROFESSIONAL SRL - both 'INVOICE No' and 'FACTURA' formats.
    
    INCLUDES:
    - INVOICE No format (1-90)
    - FACTURA FISCALĂ issued by DFW Professional SRL
    
    EXCLUDES:
    - PROFORMA INVOICE (proforma documents)
    - FACTURA from Rohel Trans or other transport companies
    - ORDIN DE PLATA (payment orders)
    - CMR / shipping documents
    - Transport contracts
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT 
            pi.id,
            pi.invoice_number,
            pi.invoice_date,
            pi.amount,
            pi.currency,
            pi.raw_text,
            a.filename,
            a.file_path,
            a.id as attachment_id,
            e.id as email_id,
            e.date_received,
            e.from_address,
            e.to_address,
            e.subject
        FROM parsed_invoices pi
        JOIN attachments a ON pi.attachment_id = a.id
        JOIN emails e ON pi.email_id = e.id
        ORDER BY pi.invoice_number ASC
    ''')
    
    dfw_invoices = []
    seen_invoice_numbers = set()
    
    for row in cursor.fetchall():
        raw_text = row['raw_text'] or ''
        raw_upper = raw_text.upper()
        filename_upper = (row['filename'] or '').upper()
        
        # EXCLUSION PATTERNS - skip these document types:
        exclusion_keywords = [
            'PROFORMA',           # Proforma invoices
            'PRO-FORMA',
            'PRO FORMA',
            'ORDIN DE PLATA',     # Payment orders
            'ROHEL TRANS',        # Transport company invoices (even if FACTURA)
            'ROTO BOP',           # Shipping documents
            'INTERNATIONAL TRANS',
            'CONTRACT DE TRANSPORT',
            'PACKING LIST',
            'DELIVERY NOTE',
            'BILL OF LADING',
            'PAYMENT_',           # Payment confirmations
            'TRANSACTION_',
            'BONIFICO',           # Bank transfers
        ]
        
        is_excluded = any(kw in raw_upper or kw in filename_upper for kw in exclusion_keywords)
        if is_excluded:
            continue
        
        # MUST have DFW PROFESSIONAL SRL as issuer
        has_dfw = 'DFW PROFESSIONAL' in raw_upper
        if not has_dfw:
            continue
        
        # Try to match either INVOICE or FACTURA format
        invoice_number = None
        
        # Pattern 1: "INVOICE No 63" format
        invoice_match = re.search(r'INVOICE\s*N[oOοº°]\.?\s*:?\s*(\d+)', raw_text, re.IGNORECASE)
        if invoice_match:
            invoice_number = int(invoice_match.group(1))
        else:
            # Check filename for invoice number
            filename_match = re.search(r'[Ii]nvoice\s*[Nn][oOοº°]?\s*(\d+)', row['filename'] or '')
            if filename_match:
                invoice_number = int(filename_match.group(1))
        
        # Pattern 2: "FACTURA" with DFW format (look for invoice number)
        if not invoice_number and 'FACTURA' in raw_upper:
            # Try to extract factura number from text or filename
            factura_match = re.search(r'FACTURA\s+(?:FISCALĂ|SERIA)?\s*[\w\s]*?[:\s]*([A-Z]?\d+)', raw_text, re.IGNORECASE)
            if factura_match:
                num_str = factura_match.group(1)
                # Extract just the digits
                digit_match = re.search(r'(\d+)', num_str)
                if digit_match:
                    invoice_number = int(digit_match.group(1))
        
        if not invoice_number:
            continue
        
        # Must be in range 1-90
        if invoice_number < 1 or invoice_number > 90:
            continue
        
        # Handle duplicates - keep the one with amount if available
        if invoice_number in seen_invoice_numbers:
            continue
        seen_invoice_numbers.add(invoice_number)
        
        # Extract amount from text if not already parsed
        amount = row['amount']
        currency = row['currency'] or 'EUR'
        
        if not amount:
            # Try various patterns for amount extraction
            amount_patterns = [
                # INVOICE format patterns
                r'TOTAL\s*EUR\s*€?([\d,\.]+)',
                r'TOTAL\s*:?\s*€?([\d,\.]+)',
                r'AMOUNT\s*:?\s*€?([\d,\.]+)',
                # FACTURA format patterns (Romanian)
                r'TOTAL\s*DE\s*PLATĂ\s*:?\s*€?([\d,\.]+)',
                r'TOTAL\s*FACTURĂ\s*:?\s*€?([\d,\.]+)',
                r'TOTAL\s*GENERAL\s*:?\s*€?([\d,\.]+)',
                r'DE\s*PLATĂ\s*:?\s*€?([\d,\.]+)',
            ]
            
            for pattern in amount_patterns:
                total_match = re.search(pattern, raw_text, re.IGNORECASE)
                if total_match:
                    try:
                        amount_str = total_match.group(1).replace(',', '')
                        # Handle European number format: 1.234,56 -> 1234.56
                        if '.' in amount_str and amount_str.count('.') > 1:
                            amount_str = amount_str.replace('.', '')
                        amount = float(amount_str)
                        break
                    except:
                        pass
        
        # Convert RON amounts to EUR (exchange rate ~4.97)
        if currency == 'RON' and amount:
            amount = amount / 4.97
            currency = 'EUR'
        
        # Extract invoice date
        invoice_date = row['invoice_date']
        if not invoice_date:
            date_match = re.search(r'Date\s*:?\s*(\d{1,2}[/\.-]\d{1,2}[/\.-]\d{2,4})', raw_text, re.IGNORECASE)
            invoice_date = date_match.group(1) if date_match else ''
        
        # Extract recipient/customer
        recipient = ''
        customer_patterns = [
            r'(JTAPE\s+Limited)',
            r'(Amba\s+Group\s+Ltd)',
            r'(DTC\s+[A-Za-z\s]+Ltd)',
            r'(Bellini[^\n]{0,30})',
            r'(Bodyshop[^\n]{0,30})',
            r'(BAXT[^\n]{0,30})',
        ]
        for pattern in customer_patterns:
            match = re.search(pattern, raw_text, re.IGNORECASE)
            if match:
                recipient = match.group(1).strip()[:40]
                break
        
        dfw_invoices.append({
            'id': row['id'],
            'email_id': row['email_id'],
            'attachment_id': row['attachment_id'],
            'invoice_number': str(invoice_number),
            'invoice_date': invoice_date,
            'email_date': row['date_received'][:10] if row['date_received'] else '',
            'recipient': recipient,
            'amount': amount,
            'currency': currency,
            'filename': row['filename'],
            'file_path': row['file_path']
        })
    
    # Also include manually assigned items (assigned_tab = 'dfw')
    seen_ids = set(inv['id'] for inv in dfw_invoices)
    
    cursor.execute('''
        SELECT 
            pi.id, pi.invoice_number, pi.invoice_date, pi.amount, pi.currency,
            a.filename, a.file_path, a.id as attachment_id,
            e.id as email_id, e.date_received, e.from_address
        FROM parsed_invoices pi
        JOIN attachments a ON pi.attachment_id = a.id
        JOIN emails e ON pi.email_id = e.id
        WHERE pi.assigned_tab = 'dfw' AND (pi.hidden IS NULL OR pi.hidden = 0)
    ''')
    
    for row in cursor.fetchall():
        if row['id'] not in seen_ids:
            amount = row['amount']
            currency = row['currency'] or 'EUR'
            if currency == 'RON' and amount:
                amount = amount / 4.97
                currency = 'EUR'
            
            dfw_invoices.append({
                'id': row['id'],
                'email_id': row['email_id'],
                'attachment_id': row['attachment_id'],
                'invoice_number': row['invoice_number'] or row['filename'][:20],
                'invoice_date': row['invoice_date'] or '',
                'email_date': row['date_received'][:10] if row['date_received'] else '',
                'recipient': row['from_address'][:30] if row['from_address'] else '',
                'amount': amount,
                'currency': currency,
                'filename': row['filename'],
                'file_path': row['file_path'],
                'manually_assigned': True
            })
    
    # Sort by invoice number (manually assigned at end)
    dfw_invoices.sort(key=lambda x: (not x.get('manually_assigned', False), str(x.get('invoice_number', 'zzz'))))
    
    conn.close()
    return jsonify(dfw_invoices)

@app.route('/api/invoices/jtape')
def get_jtape_invoices():
    """Get all JTAPE-related documents (invoices, proformas, POs)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT 
            pi.id,
            pi.invoice_number,
            pi.invoice_date,
            pi.amount,
            pi.currency,
            pi.raw_text,
            a.filename,
            a.file_path,
            a.id as attachment_id,
            e.id as email_id,
            e.date_received,
            e.from_address,
            e.subject
        FROM parsed_invoices pi
        JOIN attachments a ON pi.attachment_id = a.id
        JOIN emails e ON pi.email_id = e.id
        ORDER BY e.date_received DESC
    ''')
    
    jtape_invoices = []
    seen_files = set()
    
    # Exclusion patterns
    exclude_patterns = [
        'packing list', 'rohel trans', 'cmr', 'fedex', 'dhl'
    ]
    
    for row in cursor.fetchall():
        raw_text = row['raw_text'] or ''
        raw_upper = raw_text.upper()
        filename = row['filename'] or ''
        filename_lower = filename.lower()
        
        # Skip excluded documents
        combined = (raw_text + ' ' + filename).lower()
        if any(exc in combined for exc in exclude_patterns):
            continue
        
        # Must have JTAPE reference in text or filename
        has_jtape = 'JTAPE' in raw_upper or 'J-TAPE' in raw_upper or 'jtape' in filename_lower or 'j-tape' in filename_lower
        if not has_jtape:
            continue
        
        # Skip duplicates by filename
        if filename in seen_files:
            continue
        seen_files.add(filename)
        
        # Extract document number - try PO #, Invoice No, Proforma No
        doc_number = None
        # PO format like J9851
        po_match = re.search(r'\b(J\d{4,6})\b', raw_text + ' ' + filename)
        if po_match:
            doc_number = po_match.group(1)
        else:
            # Invoice No format
            inv_match = re.search(r'(?:INVOICE|PROFORMA)\s*N[oOοº°]?\.?\s*:?\s*(\d+)', raw_text, re.IGNORECASE)
            if inv_match:
                doc_number = inv_match.group(1)
            else:
                # Extract number from filename
                fn_match = re.search(r'(\d+)', filename)
                doc_number = fn_match.group(1) if fn_match else 'N/A'
        
        # Determine document type
        doc_type = 'Invoice'
        if 'PROFORMA' in raw_upper or 'proforma' in filename_lower:
            doc_type = 'Proforma'
        elif 'PURCHASE ORDER' in raw_upper or 'PO' in filename.upper():
            doc_type = 'PO'
        
        # Extract amount (European format)
        amount = row['amount']
        if not amount:
            total_match = re.search(r'Total\s*[€£]?\s*:?\s*([\d.,]+)', raw_text, re.IGNORECASE)
            if total_match:
                try:
                    amt_str = total_match.group(1)
                    if ',' in amt_str:
                        amt_str = amt_str.replace('.', '').replace(',', '.')
                    amount = float(amt_str)
                except:
                    pass
        
        # Extract date
        date_match = re.search(r'(?:Date|Order Date)\s*:?\s*(\d{1,2}[/\.]\d{1,2}[/\.]\d{2,4})', raw_text)
        order_date = date_match.group(1) if date_match else row['invoice_date'] or ''
        
        jtape_invoices.append({
            'id': row['id'],
            'attachment_id': row['attachment_id'],
            'invoice_number': doc_number,
            'doc_type': doc_type,
            'invoice_date': order_date,
            'email_date': row['date_received'][:10] if row['date_received'] else '',
            'amount': amount,
            'currency': 'EUR',
            'filename': filename
        })
    
    # Add manually assigned items (assigned_tab = 'jtape')
    seen_ids = set(inv['id'] for inv in jtape_invoices)
    cursor.execute('''
        SELECT pi.id, pi.invoice_number, pi.invoice_date, pi.amount, pi.currency,
               a.filename, a.file_path, a.id as attachment_id, e.date_received, e.from_address
        FROM parsed_invoices pi
        JOIN attachments a ON pi.attachment_id = a.id
        JOIN emails e ON pi.email_id = e.id
        WHERE pi.assigned_tab = 'jtape' AND (pi.hidden IS NULL OR pi.hidden = 0)
    ''')
    for row in cursor.fetchall():
        if row['id'] not in seen_ids:
            amount = row['amount']
            if row['currency'] == 'RON' and amount:
                amount = amount / 4.97
            jtape_invoices.append({
                'id': row['id'], 'attachment_id': row['attachment_id'],
                'invoice_number': row['invoice_number'] or row['filename'][:15],
                'doc_type': 'Assigned', 'invoice_date': row['invoice_date'] or '',
                'email_date': row['date_received'][:10] if row['date_received'] else '',
                'amount': amount, 'currency': 'EUR', 'filename': row['filename'],
                'manually_assigned': True
            })
    
    conn.close()
    return jsonify(jtape_invoices)

@app.route('/api/invoices/amba')
def get_amba_invoices():
    """Get all Amba Group/BAXT-related documents (invoices, proformas, POs)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT 
            pi.id,
            pi.invoice_number,
            pi.invoice_date,
            pi.amount,
            pi.currency,
            pi.raw_text,
            a.filename,
            a.file_path,
            a.id as attachment_id,
            e.id as email_id,
            e.date_received,
            e.from_address,
            e.subject
        FROM parsed_invoices pi
        JOIN attachments a ON pi.attachment_id = a.id
        JOIN emails e ON pi.email_id = e.id
        ORDER BY e.date_received DESC
    ''')
    
    amba_invoices = []
    seen_files = set()
    
    # Exclusion patterns
    exclude_patterns = [
        'packing list', 'rohel trans', 'cmr', 'fedex', 'dhl', 'dtc5',  # DTC5xxxx are design files
        'cds import', 'transaction confirmation'
    ]
    
    for row in cursor.fetchall():
        raw_text = row['raw_text'] or ''
        raw_upper = raw_text.upper()
        filename = row['filename'] or ''
        filename_lower = filename.lower()
        
        # Skip excluded documents
        combined = (raw_text + ' ' + filename).lower()
        if any(exc in combined for exc in exclude_patterns):
            continue
        
        # Must have Amba or BAXT reference
        has_amba = 'AMBA' in raw_upper or 'amba' in filename_lower
        has_baxt = 'BAXT' in raw_upper or 'baxt' in filename_lower
        
        if not (has_amba or has_baxt):
            continue
        
        # Skip duplicates by filename
        if filename in seen_files:
            continue
        seen_files.add(filename)
        
        # Extract document number - try PO #, Invoice No, Proforma No
        doc_number = None
        
        # PO format
        po_match = re.search(r'PO\s*NO\s*([A-Z]?[-\d]+\w*)', raw_text, re.IGNORECASE)
        if po_match:
            doc_number = po_match.group(1)
        else:
            po_match = re.search(r'Order\s*No\.?\s*(\d{6,})', raw_text, re.IGNORECASE)
            if po_match:
                doc_number = po_match.group(1)
        
        if not doc_number:
            # Invoice/Proforma No format
            inv_match = re.search(r'(?:INVOICE|PROFORMA)\s*N[oOοº°]?\.?\s*:?\s*(\d+)', raw_text, re.IGNORECASE)
            if inv_match:
                doc_number = inv_match.group(1)
            else:
                # Extract number from filename
                fn_match = re.search(r'(\d+)', filename)
                doc_number = fn_match.group(1) if fn_match else 'N/A'
        
        # Extract amount
        amount = row['amount']
        if not amount:
            total_match = re.search(r'TOTAL\s*(?:EUR)?\s*€?([\d,\.]+)', raw_text, re.IGNORECASE)
            if total_match:
                try:
                    amount = float(total_match.group(1).replace(',', ''))
                except:
                    pass
        
        # Determine document type
        doc_type = 'Invoice'
        if 'PROFORMA' in raw_upper or 'proforma' in filename_lower:
            doc_type = 'Proforma'
        elif 'PURCHASE ORDER' in raw_upper or 'PO' in filename.upper():
            doc_type = 'PO'
        
        # Extract date
        date_match = re.search(r'(?:Date|Purchase Order Date)\s*:?\s*(\d{1,2}[\s/\.]\w+[\s/\.]\d{2,4})', raw_text)
        order_date = date_match.group(1) if date_match else row['invoice_date'] or ''
        
        customer = 'BAXT' if has_baxt else 'Amba Group'
        
        amba_invoices.append({
            'id': row['id'],
            'attachment_id': row['attachment_id'],
            'invoice_number': doc_number,
            'doc_type': doc_type,
            'invoice_date': order_date,
            'email_date': row['date_received'][:10] if row['date_received'] else '',
            'customer': customer,
            'amount': amount,
            'currency': 'EUR',
            'filename': filename
        })
    
    # Add manually assigned items (assigned_tab = 'amba')
    seen_ids = set(inv['id'] for inv in amba_invoices)
    cursor.execute('''
        SELECT pi.id, pi.invoice_number, pi.invoice_date, pi.amount, pi.currency,
               a.filename, a.id as attachment_id, e.date_received
        FROM parsed_invoices pi
        JOIN attachments a ON pi.attachment_id = a.id
        JOIN emails e ON pi.email_id = e.id
        WHERE pi.assigned_tab = 'amba' AND (pi.hidden IS NULL OR pi.hidden = 0)
    ''')
    for row in cursor.fetchall():
        if row['id'] not in seen_ids:
            amount = row['amount']
            if row['currency'] == 'RON' and amount:
                amount = amount / 4.97
            amba_invoices.append({
                'id': row['id'], 'attachment_id': row['attachment_id'],
                'invoice_number': row['invoice_number'] or row['filename'][:15],
                'doc_type': 'Assigned', 'invoice_date': row['invoice_date'] or '',
                'email_date': row['date_received'][:10] if row['date_received'] else '',
                'customer': 'Assigned', 'amount': amount, 'currency': 'EUR',
                'filename': row['filename'], 'manually_assigned': True
            })
    
    conn.close()
    return jsonify(amba_invoices)

@app.route('/api/invoices/supplier')
def get_supplier_invoices():
    """Get supplier invoices - ΡΟΤΟΜΠΟΠ ΙΚΕ / ROTO BOP IKE invoices starting with ΤΙΜ (Τιμολόγιο)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT 
            pi.id,
            pi.invoice_number,
            pi.invoice_date,
            pi.amount,
            pi.currency,
            pi.raw_text,
            a.filename,
            a.file_path,
            a.id as attachment_id,
            e.id as email_id,
            e.date_received,
            e.from_address,
            e.subject
        FROM parsed_invoices pi
        JOIN attachments a ON pi.attachment_id = a.id
        JOIN emails e ON pi.email_id = e.id
        ORDER BY e.date_received DESC
    ''')
    
    supplier_invoices = []
    seen_inv = set()
    
    for row in cursor.fetchall():
        raw_text = row['raw_text'] or ''
        filename = row['filename'] or ''
        
        # Must be a ΡΟΤΟΜΠΟΠ (Rotopak) invoice starting with ΤΙΜ or ΔΑ (as user specified)
        # ΤΙΜ = Τιμολόγιο (Invoice), ΔΑ = Δελτίο Αποστολής (Delivery Note)
        is_greek_invoice = filename.startswith('ΤΙΜ') or filename.startswith('ΔΑ')
        
        if not is_greek_invoice:
            continue
        
        # Extract invoice number from filename (e.g., ΤΙΜ0000156.pdf, ΔΑ00000015.pdf)
        inv_number = row['invoice_number']
        if not inv_number or inv_number == 'N/A':
            # Try ΤΙΜ format
            inv_match = re.search(r'(ΤΙΜ\d+)', filename)
            if inv_match:
                inv_number = inv_match.group(1)
            else:
                # Try ΔΑ format
                inv_match = re.search(r'(ΔΑ\d+)', filename)
                if inv_match:
                    inv_number = inv_match.group(1)
                else:
                    # Try Greek text format
                    inv_match = re.search(r'Νούμερο[^\d]*(\d+)', raw_text)
                    inv_number = inv_match.group(1) if inv_match else filename.split('.')[0]
        
        # Skip duplicates
        if inv_number in seen_inv:
            continue
        seen_inv.add(inv_number)
        
        # Extract amount - look for Greek "Καθαρή Αξία" (Net Value) or "Σύνολο" (Total)
        amount = None
        # Try Καθαρή Αξία: 13275,00 € pattern
        total_match = re.search(r'Καθαρή Αξία:\s*([\d.,]+)\s*€?', raw_text)
        if not total_match:
            total_match = re.search(r'Σύνολο[^\d]*([\d.,]+)', raw_text)
        if not total_match:
            total_match = re.search(r'Αξία \(€\)[^\d]*([\d.,]+)', raw_text)
        
        if total_match:
            try:
                amt_str = total_match.group(1)
                # European format: periods are thousands sep, comma is decimal
                if ',' in amt_str:
                    amt_str = amt_str.replace('.', '').replace(',', '.')
                amount = float(amt_str)
            except:
                pass
        
        if not amount:
            amount = row['amount']
        
        supplier_invoices.append({
            'id': row['id'],
            'attachment_id': row['attachment_id'],
            'invoice_number': inv_number,
            'invoice_date': row['invoice_date'] or '',
            'email_date': row['date_received'][:10] if row['date_received'] else '',
            'supplier': 'ΡΟΤΟΜΠΟΠ ΙΚΕ',
            'amount': amount,
            'currency': 'EUR',
            'filename': filename
        })
    
    # Add manually assigned items (assigned_tab = 'supplier')
    seen_ids = set(inv['id'] for inv in supplier_invoices)
    cursor.execute('''
        SELECT pi.id, pi.invoice_number, pi.invoice_date, pi.amount, pi.currency,
               a.filename, a.id as attachment_id, e.date_received
        FROM parsed_invoices pi
        JOIN attachments a ON pi.attachment_id = a.id
        JOIN emails e ON pi.email_id = e.id
        WHERE pi.assigned_tab = 'supplier' AND (pi.hidden IS NULL OR pi.hidden = 0)
    ''')
    for row in cursor.fetchall():
        if row['id'] not in seen_ids:
            amount = row['amount']
            if row['currency'] == 'RON' and amount:
                amount = amount / 4.97
            supplier_invoices.append({
                'id': row['id'], 'attachment_id': row['attachment_id'],
                'invoice_number': row['invoice_number'] or row['filename'][:15],
                'invoice_date': row['invoice_date'] or '',
                'email_date': row['date_received'][:10] if row['date_received'] else '',
                'supplier': 'Assigned', 'amount': amount, 'currency': 'EUR',
                'filename': row['filename'], 'manually_assigned': True
            })
    
    conn.close()
    return jsonify(supplier_invoices)

@app.route('/api/invoices/contrast')
def get_contrast_invoices():
    """Get Contrast/Accounting invoices - FACTURA FISCALA documents.
    
    Includes:
    - CONTRAST ACCOUNTANCY SRL invoices
    - SmartTax/GITS Tax invoices
    - Any manually assigned to 'contrast' tab
    - Romanian FACTURA documents from service providers
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT 
            pi.id,
            pi.invoice_number,
            pi.invoice_date,
            pi.amount,
            pi.currency,
            pi.raw_text,
            pi.assigned_tab,
            a.filename,
            a.file_path,
            a.id as attachment_id,
            e.id as email_id,
            e.date_received,
            e.from_address,
            e.subject
        FROM parsed_invoices pi
        JOIN attachments a ON pi.attachment_id = a.id
        JOIN emails e ON pi.email_id = e.id
        WHERE pi.hidden IS NULL OR pi.hidden = 0
        ORDER BY e.date_received DESC
    ''')
    
    contrast_invoices = []
    seen_files = set()
    
    for row in cursor.fetchall():
        raw_text = row['raw_text'] or ''
        raw_upper = raw_text.upper()
        filename = row['filename'] or ''
        
        # Include if manually assigned to contrast
        is_assigned = row['assigned_tab'] == 'contrast'
        
        # Exclude Rohel Trans (transport company) unless manually assigned
        if 'ROHEL' in raw_upper and not is_assigned:
            continue
        
        # Check for accounting/service invoice markers
        has_contrast = ('CONTRAST ACCOUNTANCY' in raw_upper or 
                       ('CONTRAST' in raw_upper and 'J40/9369/2015' in raw_text))
        has_smarttax = 'SMARTTAX' in raw_upper or 'GITS TAX' in raw_upper
        has_factura = ('factura' in filename.lower() and 'ROH' not in filename.upper())
        has_osr = 'OSR' in filename.upper()  # OSR invoices
        
        # Include if assigned OR matches accounting criteria
        if not is_assigned and not (has_contrast or has_smarttax or has_factura or has_osr):
            continue
        
        # Skip duplicates by filename
        if filename in seen_files:
            continue
        seen_files.add(filename)
        
        # Extract invoice number
        inv_number = row['invoice_number']
        if not inv_number or inv_number == 'N/A' or inv_number == '/':
            # Try various patterns
            nr_match = re.search(r'Nr\.?\s*Factur[ia]+\s*:?\s*(\d+)', raw_text, re.IGNORECASE)
            if not nr_match:
                nr_match = re.search(r'Seria\s+\w+\s+nr:?\s*(\d+)', raw_text, re.IGNORECASE)
            if not nr_match:
                # Try filename patterns like "FACTURAINVOICE_2025_INV_OSR_162028.pdf"
                nr_match = re.search(r'OSR[_-]?(\d+)', filename, re.IGNORECASE)
            if not nr_match:
                nr_match = re.search(r'Factura[_-]nr[_-]?(\w+)', filename, re.IGNORECASE)
            if not nr_match:
                nr_match = re.search(r'factura[_-]?(\d+)', filename, re.IGNORECASE)
            if not nr_match:
                nr_match = re.search(r'(\d+)', filename)
            inv_number = nr_match.group(1) if nr_match else filename.split('.')[0][:20]
        
        # Extract date
        date_match = re.search(r'Data\s*\([^)]+\)\s*:?\s*(\d{1,2}[\./-]\d{1,2}[\./-]\d{2,4})', raw_text)
        invoice_date = date_match.group(1) if date_match else row['invoice_date'] or ''
        
        # Extract amount (Total de plata)
        amount = row['amount']
        if not amount:
            total_match = re.search(r'Total\s*(?:de plat[aă])?\s*:?\s*([\d\.,]+)', raw_text, re.IGNORECASE)
            if total_match:
                try:
                    amount = float(total_match.group(1).replace('.', '').replace(',', '.'))
                except:
                    pass
        
        contrast_invoices.append({
            'id': row['id'],
            'attachment_id': row['attachment_id'],
            'invoice_number': inv_number,
            'invoice_date': invoice_date,
            'email_date': row['date_received'][:10] if row['date_received'] else '',
            'amount': amount,
            'currency': 'RON',  # Contrast invoices are in RON
            'filename': row['filename']
        })
    
    # Add manually assigned items (assigned_tab = 'contrast')
    seen_ids = set(inv['id'] for inv in contrast_invoices)
    cursor.execute('''
        SELECT pi.id, pi.invoice_number, pi.invoice_date, pi.amount, pi.currency,
               a.filename, a.id as attachment_id, e.date_received
        FROM parsed_invoices pi
        JOIN attachments a ON pi.attachment_id = a.id
        JOIN emails e ON pi.email_id = e.id
        WHERE pi.assigned_tab = 'contrast' AND (pi.hidden IS NULL OR pi.hidden = 0)
    ''')
    for row in cursor.fetchall():
        if row['id'] not in seen_ids:
            contrast_invoices.append({
                'id': row['id'], 'attachment_id': row['attachment_id'],
                'invoice_number': row['invoice_number'] or row['filename'][:15],
                'invoice_date': row['invoice_date'] or '',
                'email_date': row['date_received'][:10] if row['date_received'] else '',
                'amount': row['amount'], 'currency': row['currency'] or 'EUR',
                'filename': row['filename'], 'manually_assigned': True
            })
    
    # Sort by invoice number
    contrast_invoices.sort(key=lambda x: int(x['invoice_number']) if x['invoice_number'].isdigit() else 0)
    
    conn.close()
    return jsonify(contrast_invoices)

@app.route('/api/invoices/proforma')
def get_proforma_invoices():
    """Get all Pro Forma invoices."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT 
            pi.id,
            pi.invoice_number,
            pi.invoice_date,
            pi.amount,
            pi.currency,
            pi.raw_text,
            pi.assigned_tab,
            a.filename,
            a.file_path,
            a.id as attachment_id,
            e.id as email_id,
            e.date_received,
            e.from_address,
            e.subject
        FROM parsed_invoices pi
        JOIN attachments a ON pi.attachment_id = a.id
        JOIN emails e ON pi.email_id = e.id
        WHERE (pi.hidden IS NULL OR pi.hidden = 0)
        ORDER BY e.date_received DESC
    ''')
    
    proforma_invoices = []
    seen_files = set()
    
    for row in cursor.fetchall():
        raw_text = row['raw_text'] or ''
        raw_upper = raw_text.upper()
        filename = row['filename'] or ''
        filename_lower = filename.lower()
        
        # Include if manually assigned to proforma
        is_assigned = row['assigned_tab'] == 'proforma'
        
        # Check for proforma markers
        has_proforma = ('PROFORMA' in raw_upper or 'PRO FORMA' in raw_upper or
                       'proforma' in filename_lower or 'pro forma' in filename_lower)
        
        if not is_assigned and not has_proforma:
            continue
        
        # Skip duplicates by filename
        if filename in seen_files:
            continue
        seen_files.add(filename)
        
        # Extract invoice number
        inv_number = row['invoice_number']
        if not inv_number or inv_number == 'N/A':
            nr_match = re.search(r'(?:PROFORMA|PRO\s*FORMA)\s*(?:INVOICE\s*)?N[oOοº°]?\.?\s*:?\s*(\d+)', raw_text, re.IGNORECASE)
            if not nr_match:
                nr_match = re.search(r'Proforma\s*No\s*(\d+)', filename, re.IGNORECASE)
            if not nr_match:
                nr_match = re.search(r'(\d+)', filename)
            inv_number = nr_match.group(1) if nr_match else filename.split('.')[0][:20]
        
        # Extract amount (European format)
        amount = row['amount']
        if not amount:
            total_match = re.search(r'Total\s*(?:EUR|€)?\s*:?\s*€?\s*([\d.,]+)', raw_text, re.IGNORECASE)
            if total_match:
                try:
                    amt_str = total_match.group(1)
                    if ',' in amt_str and '.' in amt_str:
                        amt_str = amt_str.replace('.', '').replace(',', '.')
                    elif ',' in amt_str:
                        parts = amt_str.split(',')
                        if len(parts) == 2 and len(parts[1]) == 2:
                            amt_str = amt_str.replace(',', '.')
                        else:
                            amt_str = amt_str.replace(',', '')
                    amount = float(amt_str)
                except:
                    pass
        
        proforma_invoices.append({
            'id': row['id'],
            'attachment_id': row['attachment_id'],
            'invoice_number': inv_number,
            'invoice_date': row['invoice_date'] or '',
            'email_date': row['date_received'][:10] if row['date_received'] else '',
            'amount': amount,
            'currency': row['currency'] or 'EUR',
            'filename': filename
        })
    
    # Add manually assigned items (assigned_tab = 'proforma')
    seen_ids = set(inv['id'] for inv in proforma_invoices)
    cursor.execute('''
        SELECT pi.id, pi.invoice_number, pi.invoice_date, pi.amount, pi.currency,
               a.filename, a.id as attachment_id, e.date_received
        FROM parsed_invoices pi
        JOIN attachments a ON pi.attachment_id = a.id
        JOIN emails e ON pi.email_id = e.id
        WHERE pi.assigned_tab = 'proforma' AND (pi.hidden IS NULL OR pi.hidden = 0)
    ''')
    for row in cursor.fetchall():
        if row['id'] not in seen_ids:
            proforma_invoices.append({
                'id': row['id'], 'attachment_id': row['attachment_id'],
                'invoice_number': row['invoice_number'] or row['filename'][:15],
                'invoice_date': row['invoice_date'] or '',
                'email_date': row['date_received'][:10] if row['date_received'] else '',
                'amount': row['amount'], 'currency': row['currency'] or 'EUR',
                'filename': row['filename'], 'manually_assigned': True
            })
    
    conn.close()
    return jsonify(proforma_invoices)

@app.route('/api/attachment/<int:attachment_id>')
def get_attachment(attachment_id):
    """Get attachment file for viewing."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('SELECT filename, file_path, content_type FROM attachments WHERE id = ?', (attachment_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        return jsonify({'error': 'Attachment not found'}), 404
    
    file_path = row['file_path']
    if not os.path.exists(file_path):
        return jsonify({'error': 'File not found on disk'}), 404
    
    from flask import send_file
    return send_file(file_path, mimetype=row['content_type'] or 'application/pdf')

@app.route('/api/attachment/info/<int:attachment_id>')
def get_attachment_info(attachment_id):
    """Get attachment metadata."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT a.*, e.subject, e.from_address, e.to_address, e.date_received
        FROM attachments a
        JOIN emails e ON a.email_id = e.id
        WHERE a.id = ?
    ''', (attachment_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        return jsonify({'error': 'Attachment not found'}), 404
    
    return jsonify(dict(row))

@app.route('/api/attachments/all')
def get_all_attachments():
    """Get ALL PDF attachments - for the All Attachments tab."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT 
            a.id,
            a.filename,
            a.file_path,
            a.content_type,
            a.size,
            e.id as email_id,
            e.date_received,
            e.from_address,
            e.subject,
            pi.id as parsed_id,
            pi.invoice_number,
            pi.amount,
            pi.currency,
            pi.hidden,
            pi.assigned_tab,
            pi.raw_text,
            pi.amount_edited
        FROM attachments a
        JOIN emails e ON a.email_id = e.id
        LEFT JOIN parsed_invoices pi ON a.id = pi.attachment_id
        WHERE a.filename LIKE '%.pdf' AND (pi.hidden IS NULL OR pi.hidden = 0)
        ORDER BY e.date_received DESC
    ''')
    
    attachments = []
    
    for row in cursor.fetchall():
        filename = row['filename'] or ''
        
        amount = row['amount']
        
        # Detect which tab this naturally belongs to
        detected_tab = row['assigned_tab'] or ''
        raw_upper = (row['raw_text'] or '').upper()
        filename_upper = filename.upper()
        
        if not detected_tab:
            # Check for Pro Forma first (before other checks)
            if 'PROFORMA' in raw_upper or 'PRO FORMA' in raw_upper or 'proforma' in filename.lower():
                detected_tab = 'proforma'
            # Check for DFW invoices
            elif 'DFW PROFESSIONAL' in raw_upper or 'INVOICE NO' in filename_upper:
                detected_tab = 'dfw'
            # Check for JTape
            elif 'JTAPE' in raw_upper or 'JTAPE' in filename_upper or 'J-TAPE' in filename_upper:
                detected_tab = 'jtape'
            # Check for Amba/BAXT
            elif 'AMBA' in raw_upper or 'BAXT' in raw_upper or 'AMBA' in filename_upper or 'BAXT' in filename_upper:
                detected_tab = 'amba'
            # Check for Rotopak/Supplier
            elif filename.startswith('ΤΙΜ') or filename.startswith('ΔΑ'):
                detected_tab = 'supplier'
            # Check for Contrast/Accounting
            elif 'CONTRAST' in raw_upper or 'SMARTTAX' in raw_upper or 'GITS TAX' in raw_upper:
                detected_tab = 'contrast'
        
        attachments.append({
            'id': row['id'],
            'parsed_id': row['parsed_id'],
            'filename': filename,
            'file_path': row['file_path'],
            'content_type': row['content_type'],
            'size': row['size'],
            'email_id': row['email_id'],
            'email_date': row['date_received'][:10] if row['date_received'] else '',
            'sender': row['from_address'][:40] if row['from_address'] else '',
            'subject': row['subject'] or '',
            'invoice_number': row['invoice_number'],
            'amount': amount,
            'currency': row['currency'] or 'EUR',
            'assigned_tab': row['assigned_tab'],
            'detected_tab': detected_tab,
            'amount_edited': row['amount_edited'] or 0
        })
    
    conn.close()
    return jsonify(attachments)

@app.route('/api/attachment/hide/<int:parsed_id>', methods=['POST'])
def hide_attachment(parsed_id):
    """Hide an attachment from view."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('UPDATE parsed_invoices SET hidden = 1 WHERE id = ?', (parsed_id,))
    conn.commit()
    conn.close()
    
    return jsonify({'success': True, 'message': 'Attachment hidden'})

@app.route('/api/attachment/unhide/<int:parsed_id>', methods=['POST'])
def unhide_attachment(parsed_id):
    """Unhide an attachment."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('UPDATE parsed_invoices SET hidden = 0 WHERE id = ?', (parsed_id,))
    conn.commit()
    conn.close()
    
    return jsonify({'success': True, 'message': 'Attachment unhidden'})

@app.route('/api/attachment/assign/<int:parsed_id>', methods=['POST'])
def assign_attachment_to_tab(parsed_id):
    """Assign an attachment to a specific tab, or unassign by passing null."""
    from flask import request
    data = request.get_json() or {}
    tab = data.get('tab')  # Can be None to unassign
    
    # Convert None or empty string to None for unassignment
    if tab is None or tab == '':
        tab = None
    else:
        valid_tabs = ['dfw', 'jtape', 'amba', 'supplier', 'contrast', 'proforma']
        if tab not in valid_tabs:
            return jsonify({'error': f'Invalid tab. Must be one of: {valid_tabs}'}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('UPDATE parsed_invoices SET assigned_tab = ? WHERE id = ?', (tab, parsed_id))
    conn.commit()
    conn.close()
    
    return jsonify({'success': True, 'message': f'Attachment {"unassigned" if tab is None else f"assigned to {tab}"}'})

@app.route('/api/invoice/update_amount/<int:parsed_id>', methods=['POST'])
def update_invoice_amount(parsed_id):
    """Update the amount and currency for an invoice."""
    from flask import request
    data = request.get_json() or {}
    amount = data.get('amount')
    currency = data.get('currency', 'EUR')
    
    if amount is None:
        return jsonify({'error': 'Amount is required'}), 400
    
    try:
        amount = float(amount)
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid amount format'}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Set amount_edited = 1 to mark manually edited amounts
    cursor.execute('UPDATE parsed_invoices SET amount = ?, currency = ?, amount_edited = 1 WHERE id = ?', 
                   (amount, currency, parsed_id))
    conn.commit()
    conn.close()
    
    return jsonify({'success': True, 'message': 'Amount updated', 'amount': amount, 'currency': currency})

@app.route('/api/invoice/update_number/<int:parsed_id>', methods=['POST'])
def update_invoice_number(parsed_id):
    """Update the invoice number for an invoice."""
    data = request.get_json() or {}
    invoice_number = data.get('invoice_number')
    
    if not invoice_number:
        return jsonify({'error': 'Invoice number is required'}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('UPDATE parsed_invoices SET invoice_number = ? WHERE id = ?', 
                   (invoice_number, parsed_id))
    conn.commit()
    conn.close()
    
    return jsonify({'success': True, 'message': 'Invoice number updated', 'invoice_number': invoice_number})

@app.route('/api/entity/update_category', methods=['POST'])
def update_entity_category():
    """Update the category for an entity/domain."""
    from flask import request
    data = request.get_json() or {}
    domain = data.get('domain')
    category = data.get('category')
    
    if not domain or not category:
        return jsonify({'error': 'Domain and category are required'}), 400
    
    valid_categories = ['customers', 'transport', 'suppliers', 'taxation', 'legal', 'internal', 'other']
    if category not in valid_categories:
        return jsonify({'error': f'Invalid category. Must be one of: {valid_categories}'}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Insert or update the category override
    cursor.execute('''
        INSERT INTO entity_categories (domain, category, updated_at) 
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(domain) DO UPDATE SET category = ?, updated_at = CURRENT_TIMESTAMP
    ''', (domain, category, category))
    conn.commit()
    conn.close()
    
    return jsonify({'success': True, 'message': 'Category updated', 'domain': domain, 'category': category})


@app.route('/api/attachments/hidden')
def get_hidden_attachments():
    """Get all hidden attachments."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT 
            a.id,
            a.filename,
            pi.id as parsed_id,
            e.date_received
        FROM attachments a
        JOIN parsed_invoices pi ON a.id = pi.attachment_id
        JOIN emails e ON a.email_id = e.id
        WHERE pi.hidden = 1
        ORDER BY e.date_received DESC
    ''')
    
    attachments = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify(attachments)

# Global variable to track sync status (last_sync loaded from DB)
sync_status = {'running': False, 'last_sync': get_setting('last_sync'), 'message': 'Ready'}

@app.route('/api/sync/status')
def get_sync_status():
    """Get the current sync status."""
    # Always get fresh last_sync from database
    sync_status['last_sync'] = get_setting('last_sync')
    return jsonify(sync_status)

@app.route('/api/sync/start', methods=['POST'])
def start_sync():
    """Start IMAP sync in background thread."""
    global sync_status
    
    if sync_status['running']:
        return jsonify({'error': 'Sync already in progress'}), 400
    
    def run_sync():
        global sync_status
        try:
            sync_status['running'] = True
            sync_status['message'] = 'Syncing...'
            
            # Import here to avoid circular imports
            from email_archiver import EmailArchiver
            
            archiver = EmailArchiver()
            
            # Get credentials from environment
            email_address = os.getenv('IMAP_EMAIL')
            password = os.getenv('IMAP_PASSWORD')
            imap_server = os.getenv('IMAP_SERVER', 'imap.gmail.com')
            
            if not email_address or not password:
                sync_status['message'] = 'IMAP credentials not configured'
                sync_status['running'] = False
                return
            
            # Connect and sync
            if archiver.connect_to_email(email_address, password, imap_server):
                # Sync INBOX
                archiver.download_emails(folder='INBOX', limit=500)
                
                # Try to sync Sent folder (different names on different servers)
                try:
                    archiver.download_emails(folder='Sent', limit=500)
                except:
                    try:
                        archiver.download_emails(folder='[Gmail]/Sent Mail', limit=500)
                    except:
                        pass
                
                sync_status['message'] = 'Sync completed successfully'
                sync_status['last_sync'] = datetime.now().isoformat()
                set_setting('last_sync', sync_status['last_sync'])
            else:
                sync_status['message'] = 'Failed to connect to IMAP server'
        
        except Exception as e:
            sync_status['message'] = f'Sync error: {str(e)}'
        
        finally:
            sync_status['running'] = False
    
    # Start sync in background thread
    thread = threading.Thread(target=run_sync)
    thread.daemon = True
    thread.start()
    
    return jsonify({'success': True, 'message': 'Sync started'})

@app.route('/api/chromadb/index_emails', methods=['POST'])
def index_emails_to_chroma():
    """Index all emails into ChromaDB for semantic search."""
    if not emails_collection:
        return jsonify({'error': 'ChromaDB not initialized'}), 500
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, subject, from_address, body_text, date_received
            FROM emails
            ORDER BY date_received DESC
        ''')
        
        emails = cursor.fetchall()
        conn.close()
        
        # Prepare data for ChromaDB
        documents = []
        metadatas = []
        ids = []
        
        for email in emails:
            # Combine subject and body for better semantic search
            text = f"{email['subject'] or ''}\n{email['body_text'] or ''}"
            text = text.strip()
            
            if not text:
                continue
            
            documents.append(text)
            metadatas.append({
                'email_id': email['id'],
                'subject': email['subject'] or '',
                'from_address': email['from_address'] or '',
                'date': email['date_received'] or ''
            })
            ids.append(f"email_{email['id']}")
        
        # Add to ChromaDB in batches
        batch_size = 100
        indexed_count = 0
        
        for i in range(0, len(documents), batch_size):
            batch_docs = documents[i:i+batch_size]
            batch_metas = metadatas[i:i+batch_size]
            batch_ids = ids[i:i+batch_size]
            
            emails_collection.upsert(
                documents=batch_docs,
                metadatas=batch_metas,
                ids=batch_ids
            )
            indexed_count += len(batch_docs)
        
        return jsonify({
            'success': True,
            'indexed_count': indexed_count,
            'message': f'Indexed {indexed_count} emails'
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/chromadb/search', methods=['POST'])
def semantic_search():
    """Perform semantic search across emails and invoices."""
    if not emails_collection:
        return jsonify({'error': 'ChromaDB not initialized'}), 500
    
    data = request.get_json() or {}
    query = data.get('query', '')
    limit = data.get('limit', 10)
    collection_type = data.get('collection', 'emails')  # 'emails' or 'invoices'
    
    if not query:
        return jsonify({'error': 'Query is required'}), 400
    
    try:
        # Choose collection
        collection = emails_collection if collection_type == 'emails' else invoices_collection
        
        if not collection:
            return jsonify({'error': f'{collection_type} collection not available'}), 500
        
        # Perform semantic search
        results = collection.query(
            query_texts=[query],
            n_results=min(limit, 50),
            include=['documents', 'metadatas', 'distances']
        )
        
        # Format results
        formatted_results = []
        if results and results['ids'] and len(results['ids']) > 0:
            for i in range(len(results['ids'][0])):
                formatted_results.append({
                    'id': results['ids'][0][i],
                    'document': results['documents'][0][i][:500],  # Truncate for preview
                    'metadata': results['metadatas'][0][i],
                    'similarity_score': 1 - results['distances'][0][i]  # Convert distance to similarity
                })
        
        return jsonify({
            'query': query,
            'results': formatted_results,
            'count': len(formatted_results)
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/chromadb/status')
def chromadb_status():
    """Get ChromaDB status and collection counts."""
    if not chroma_client:
        return jsonify({'initialized': False, 'message': 'ChromaDB not initialized'})
    
    try:
        email_count = emails_collection.count() if emails_collection else 0
        invoice_count = invoices_collection.count() if invoices_collection else 0
        
        return jsonify({
            'initialized': True,
            'emails_indexed': email_count,
            'invoices_indexed': invoice_count
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Organization file management endpoints
@app.route('/api/organization/<domain>/files')
def get_organization_files(domain):
    """Get all files associated with an organization."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT 
                of.id,
                of.filename,
                of.notes,
                of.created_at,
                of.attachment_id,
                a.filename as original_filename,
                a.file_path
            FROM organization_files of
            LEFT JOIN attachments a ON of.attachment_id = a.id
            WHERE of.domain = ?
            ORDER BY of.created_at DESC
        ''', (domain,))
        
        files = [dict(row) for row in cursor.fetchall()]
        conn.close()
        
        return jsonify({'files': files})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/organization/<domain>/link_attachment', methods=['POST'])
def link_attachment_to_organization(domain):
    """Link an existing attachment to an organization."""
    data = request.get_json() or {}
    attachment_id = data.get('attachment_id')
    notes = data.get('notes', '')
    
    if not attachment_id:
        return jsonify({'error': 'attachment_id required'}), 400
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get attachment filename
        cursor.execute('SELECT filename FROM attachments WHERE id = ?', (attachment_id,))
        result = cursor.fetchone()
        
        if not result:
            return jsonify({'error': 'Attachment not found'}), 404
        
        filename = result['filename']
        
        # Link to organization
        cursor.execute('''
            INSERT INTO organization_files (domain, attachment_id, filename, notes)
            VALUES (?, ?, ?, ?)
        ''', (domain, attachment_id, filename, notes))
        
        conn.commit()
        conn.close()
        
        return jsonify({'success': True, 'message': 'Attachment linked to organization'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/organization/<domain>/files/<int:file_id>', methods=['DELETE'])
def delete_organization_file(domain, file_id):
    """Remove a file from an organization."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute('DELETE FROM organization_files WHERE id = ? AND domain = ?', (file_id, domain))
        conn.commit()
        conn.close()
        
        return jsonify({'success': True, 'message': 'File removed from organization'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Organization name and email assignment endpoints
@app.route('/api/organization/rename', methods=['POST'])
def rename_organization():
    """Rename an organization (set display name)."""
    data = request.get_json() or {}
    domain = data.get('domain')
    display_name = data.get('display_name', '').strip()
    
    if not domain:
        return jsonify({'error': 'Domain required'}), 400
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        if display_name:
            cursor.execute('''
                INSERT OR REPLACE INTO organization_names (domain, display_name, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
            ''', (domain, display_name))
        else:
            # Remove custom name to revert to default
            cursor.execute('DELETE FROM organization_names WHERE domain = ?', (domain,))
        
        conn.commit()
        conn.close()
        
        return jsonify({'success': True, 'domain': domain, 'display_name': display_name})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/organization/names')
def get_organization_names():
    """Get all custom organization names."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT domain, display_name FROM organization_names')
        names = {row['domain']: row['display_name'] for row in cursor.fetchall()}
        conn.close()
        return jsonify(names)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/organization/assign_email', methods=['POST'])
def assign_email_to_organization():
    """Assign an email address to an organization."""
    data = request.get_json() or {}
    email_address = data.get('email_address', '').lower().strip()
    organization_domain = data.get('organization_domain', '').strip()
    
    if not email_address:
        return jsonify({'error': 'Email address required'}), 400
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        if organization_domain:
            cursor.execute('''
                INSERT OR REPLACE INTO email_organization_assignments (email_address, organization_domain, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
            ''', (email_address, organization_domain))
        else:
            # Remove assignment to revert to default
            cursor.execute('DELETE FROM email_organization_assignments WHERE email_address = ?', (email_address,))
        
        conn.commit()
        conn.close()
        
        return jsonify({'success': True, 'email_address': email_address, 'organization_domain': organization_domain})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/organization/email_assignments')
def get_email_assignments():
    """Get all email-to-organization assignments."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT email_address, organization_domain FROM email_organization_assignments')
        assignments = {row['email_address']: row['organization_domain'] for row in cursor.fetchall()}
        conn.close()
        return jsonify(assignments)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/organization/list')
def list_organizations():
    """Get list of all organizations with their display names."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get custom names
        cursor.execute('SELECT domain, display_name FROM organization_names')
        custom_names = {row['domain']: row['display_name'] for row in cursor.fetchall()}
        
        # Get all unique domains from emails
        cursor.execute('''
            SELECT DISTINCT 
                CASE 
                    WHEN sender LIKE '%@%' THEN LOWER(SUBSTR(sender, INSTR(sender, '@') + 1))
                    ELSE NULL
                END as domain
            FROM emails
            WHERE domain IS NOT NULL
        ''')
        
        domains = set()
        for row in cursor.fetchall():
            if row['domain']:
                domains.add(row['domain'])
        
        conn.close()
        
        # Build organization list
        orgs = []
        for domain in sorted(domains):
            orgs.append({
                'domain': domain,
                'display_name': custom_names.get(domain, domain.split('.')[0].title())
            })
        
        return jsonify(orgs)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/organization/set_related', methods=['POST'])
def set_organization_related():
    """Set a related organization for a domain."""
    data = request.get_json() or {}
    domain = data.get('domain')
    related_domain = data.get('related_domain', '').strip()
    
    if not domain:
        return jsonify({'error': 'Domain required'}), 400
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        if related_domain:
            cursor.execute('''
                INSERT OR REPLACE INTO organization_relationships (domain, related_domain, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
            ''', (domain, related_domain))
        else:
            cursor.execute('DELETE FROM organization_relationships WHERE domain = ?', (domain,))
        
        conn.commit()
        conn.close()
        
        return jsonify({'success': True, 'domain': domain, 'related_domain': related_domain})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/organization/relationships')
def get_organization_relationships():
    """Get all organization relationships."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT domain, related_domain FROM organization_relationships')
        relationships = {row['domain']: row['related_domain'] for row in cursor.fetchall()}
        conn.close()
        return jsonify(relationships)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Vector DB amount suggestion endpoint
@app.route('/api/invoice/suggest_amount', methods=['POST'])
def suggest_amount_from_vectors():
    """Use vector similarity to suggest invoice amounts based on similar invoices."""
    if not invoices_collection:
        return jsonify({'error': 'ChromaDB not initialized'}), 500
    
    data = request.get_json() or {}
    parsed_id = data.get('parsed_id')
    
    if not parsed_id:
        return jsonify({'error': 'parsed_id required'}), 400
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get the invoice text for semantic search
        cursor.execute('''
            SELECT pi.invoice_text, pi.invoice_number, pi.amount, pi.currency, a.filename
            FROM parsed_invoices pi
            JOIN attachments a ON pi.attachment_id = a.id
            WHERE pi.id = ?
        ''', (parsed_id,))
        
        invoice = cursor.fetchone()
        
        if not invoice or not invoice['invoice_text']:
            return jsonify({'error': 'Invoice not found or has no text'}), 404
        
        # Search for similar invoices in ChromaDB
        try:
            # First check if we have indexed invoices
            if invoices_collection.count() == 0:
                # If not indexed, index current invoice database
                cursor.execute('''
                    SELECT pi.id, pi.invoice_text, pi.invoice_number, pi.amount, pi.currency
                    FROM parsed_invoices pi
                    WHERE pi.invoice_text IS NOT NULL AND pi.invoice_text != ''
                ''')
                
                all_invoices = cursor.fetchall()
                
                if len(all_invoices) > 0:
                    documents = []
                    metadatas = []
                    ids = []
                    
                    for inv in all_invoices:
                        documents.append(inv['invoice_text'])
                        metadatas.append({
                            'invoice_id': inv['id'],
                            'invoice_number': inv['invoice_number'] or '',
                            'amount': float(inv['amount']) if inv['amount'] else 0,
                            'currency': inv['currency'] or 'EUR'
                        })
                        ids.append(f"invoice_{inv['id']}")
                    
                    # Add to ChromaDB
                    batch_size = 100
                    for i in range(0, len(documents), batch_size):
                        batch_docs = documents[i:i+batch_size]
                        batch_metas = metadatas[i:i+batch_size]
                        batch_ids = ids[i:i+batch_size]
                        
                        invoices_collection.upsert(
                            documents=batch_docs,
                            metadatas=batch_metas,
                            ids=batch_ids
                        )
            
            # Now perform semantic search
            results = invoices_collection.query(
                query_texts=[invoice['invoice_text']],
                n_results=10,
                include=['documents', 'metadatas', 'distances']
            )
            
            # Extract amounts from similar invoices
            similar_amounts = []
            if results and results['metadatas'] and len(results['metadatas']) > 0:
                for i, metadata in enumerate(results['metadatas'][0]):
                    # Skip self
                    if metadata.get('invoice_id') == parsed_id:
                        continue
                    
                    amount = metadata.get('amount', 0)
                    if amount and amount > 0:
                        similarity = 1 - results['distances'][0][i]
                        similar_amounts.append({
                            'amount': amount,
                            'currency': metadata.get('currency', 'EUR'),
                            'invoice_number': metadata.get('invoice_number', ''),
                            'similarity': round(similarity * 100, 1)
                        })
            
            # Calculate suggested amount (weighted average by similarity)
            if similar_amounts:
                total_weight = sum(a['similarity'] for a in similar_amounts)
                if total_weight > 0:
                    suggested_amount = sum(a['amount'] * a['similarity'] for a in similar_amounts) / total_weight
                else:
                    suggested_amount = similar_amounts[0]['amount']
                
                return jsonify({
                    'success': True,
                    'suggested_amount': round(suggested_amount, 2),
                    'currency': 'EUR',
                    'confidence': round(similar_amounts[0]['similarity'], 1) if similar_amounts else 0,
                    'similar_invoices': similar_amounts[:5]
                })
            else:
                return jsonify({
                    'success': False,
                    'message': 'No similar invoices found with amounts'
                })
                
        except Exception as e:
            return jsonify({'error': f'Vector search error: {str(e)}'}), 500
        
        finally:
            conn.close()
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ========== PRODUCTION API ENDPOINTS ==========

@app.route('/api/production/feedback')
def get_production_feedback():
    """Get all production feedback."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM production_feedback ORDER BY created_at DESC')
    feedback = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    # Parse files JSON if exists
    for fb in feedback:
        if fb.get('files'):
            try:
                fb['files'] = json.loads(fb['files'])
            except:
                fb['files'] = []
    
    return jsonify(feedback)

@app.route('/api/production/feedback', methods=['POST'])
def add_production_feedback():
    """Add new production feedback."""
    data = request.get_json() or {}
    title = data.get('title', '')
    description = data.get('description', '')
    status = data.get('status', 'pending')
    files = json.dumps(data.get('files', []))
    feedback_date = data.get('feedback_date')  # Can be null
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO production_feedback (title, description, status, files, feedback_date)
        VALUES (?, ?, ?, ?, ?)
    ''', (title, description, status, files, feedback_date))
    conn.commit()
    conn.close()
    
    return jsonify({'success': True, 'message': 'Feedback added'})

@app.route('/api/production/feedback/<int:feedback_id>', methods=['PUT'])
def update_feedback(feedback_id):
    """Update feedback status."""
    data = request.get_json() or {}
    conn = get_db_connection()
    cursor = conn.cursor()
    
    updates = []
    params = []
    for field in ['title', 'description', 'status', 'feedback_date']:
        if field in data:
            updates.append(f'{field} = ?')
            params.append(data[field])
    
    if updates:
        updates.append('updated_at = CURRENT_TIMESTAMP')
        params.append(feedback_id)
        cursor.execute(f'UPDATE production_feedback SET {", ".join(updates)} WHERE id = ?', params)
        conn.commit()
    
    conn.close()
    return jsonify({'success': True, 'message': 'Feedback updated'})

@app.route('/api/production/feedback/<int:feedback_id>', methods=['DELETE'])
def delete_feedback(feedback_id):
    """Delete feedback."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM production_feedback WHERE id = ?', (feedback_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': 'Feedback deleted'})

@app.route('/api/production/runs')
def get_production_runs():
    """Get all production runs."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM production_runs ORDER BY eta_month ASC, created_at DESC')
    runs = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify(runs)

@app.route('/api/production/runs', methods=['POST'])
def add_production_run():
    """Add new production run."""
    data = request.get_json() or {}
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO production_runs (client, order_ref, product, quantity, status, notes, eta_month, date_ordered, price_per_roll, cost_per_roll)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (data.get('client'), data.get('order_ref'), data.get('product'), 
          data.get('quantity', 0), data.get('status', 'pending'), 
          data.get('notes', ''), data.get('eta_month'), data.get('date_ordered'), data.get('price_per_roll', 0), data.get('cost_per_roll', 0)))
    conn.commit()
    conn.close()
    
    return jsonify({'success': True, 'message': 'Production run added'})

@app.route('/api/production/runs/<int:run_id>', methods=['PUT'])
def update_production_run(run_id):
    """Update a production run - automatically tracks updated_at."""
    data = request.get_json() or {}
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Build update query dynamically based on provided fields
    updates = []
    params = []
    allowed_fields = [
        'client', 'order_ref', 'product', 'quantity', 'status', 'notes', 
        'scheduled_month', 'eta_month', 'date_ordered', 'downpayment_paid',
        'date_prod_start', 'date_prod_end', 'date_warehouse', 'paid_off', 'date_delivered', 'price_per_roll'
    ]
    for field in allowed_fields:
        if field in data:
            updates.append(f'{field} = ?')
            params.append(data[field])
    
    if updates:
        # Always update the updated_at timestamp
        updates.append('updated_at = CURRENT_TIMESTAMP')
        params.append(run_id)
        cursor.execute(f'UPDATE production_runs SET {", ".join(updates)} WHERE id = ?', params)
        conn.commit()
    
    conn.close()
    return jsonify({'success': True, 'message': 'Production run updated'})

@app.route('/api/production/runs/<int:run_id>', methods=['DELETE'])
def delete_production_run(run_id):
    """Delete a production run."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM production_runs WHERE id = ?', (run_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': 'Production run deleted'})

@app.route('/api/production/products')
def get_products():
    """Get all products."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM products ORDER BY name ASC')
    products = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify(products)

@app.route('/api/production/products', methods=['POST'])
def add_product():
    """Add a new product."""
    data = request.get_json() or {}
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'success': False, 'error': 'Product name is required'}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('INSERT INTO products (name, description, price, notes) VALUES (?, ?, ?, ?)',
                      (name, data.get('description', ''), data.get('price', 0), data.get('notes', '')))
        conn.commit()
        product_id = cursor.lastrowid
        conn.close()
        return jsonify({'success': True, 'id': product_id, 'name': name})
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'success': False, 'error': 'Product already exists'}), 400

@app.route('/api/production/products/<int:product_id>', methods=['PUT'])
def update_product(product_id):
    """Update a product."""
    data = request.get_json() or {}
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''UPDATE products SET name = ?, description = ?, price = ?, notes = ? WHERE id = ?''',
                  (data.get('name', ''), data.get('description', ''), data.get('price', 0), data.get('notes', ''), product_id))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/production/products/<int:product_id>', methods=['DELETE'])
def delete_product(product_id):
    """Delete a product."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM products WHERE id = ?', (product_id,))
    cursor.execute('DELETE FROM client_product_prices WHERE product_id = ?', (product_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/production/client-prices')
def get_client_prices():
    """Get all client-product prices."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT cpp.id, cpp.client_id, cpp.product_id, cpp.price,
               c.name as client_name, p.name as product_name
        FROM client_product_prices cpp
        JOIN clients c ON cpp.client_id = c.id
        JOIN products p ON cpp.product_id = p.id
        ORDER BY c.name, p.name
    ''')
    prices = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify(prices)

@app.route('/api/production/client-prices', methods=['POST'])
def set_client_price():
    """Set or update a client-specific price for a product."""
    data = request.get_json() or {}
    client_id = data.get('client_id')
    product_id = data.get('product_id')
    price = data.get('price', 0)
    
    if not client_id or not product_id:
        return jsonify({'success': False, 'error': 'client_id and product_id required'}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO client_product_prices (client_id, product_id, price)
        VALUES (?, ?, ?)
        ON CONFLICT(client_id, product_id) DO UPDATE SET price = ?, updated_at = CURRENT_TIMESTAMP
    ''', (client_id, product_id, price, price))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/production/client-prices/<int:price_id>', methods=['DELETE'])
def delete_client_price(price_id):
    """Delete a client-specific price."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM client_product_prices WHERE id = ?', (price_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/production/product-price')
def get_product_price_for_client():
    """Get the price for a product for a specific client."""
    client_id = request.args.get('client_id')
    product_id = request.args.get('product_id')
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Try to get client-specific price first
    if client_id:
        cursor.execute('''
            SELECT price FROM client_product_prices 
            WHERE client_id = ? AND product_id = ?
        ''', (client_id, product_id))
        row = cursor.fetchone()
        if row:
            conn.close()
            return jsonify({'price': row['price'], 'source': 'client'})
    
    # Fall back to default product price
    cursor.execute('SELECT price FROM products WHERE id = ?', (product_id,))
    row = cursor.fetchone()
    conn.close()
    
    if row:
        return jsonify({'price': row['price'] or 0, 'source': 'default'})
    return jsonify({'price': 0, 'source': 'none'})

@app.route('/api/production/clients')
def get_clients():
    """Get all clients."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM clients ORDER BY name ASC')
    clients = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify(clients)

@app.route('/api/production/clients', methods=['POST'])
def add_client():
    """Add a new client."""
    data = request.get_json() or {}
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'success': False, 'error': 'Client name is required'}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('INSERT INTO clients (name, contact_info, country) VALUES (?, ?, ?)',
                      (name, data.get('contact_info', ''), data.get('country', '')))
        conn.commit()
        client_id = cursor.lastrowid
        conn.close()
        return jsonify({'success': True, 'id': client_id, 'name': name})
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'success': False, 'error': 'Client already exists'}), 400

@app.route('/api/production/clients/<int:client_id>', methods=['DELETE'])
def delete_client(client_id):
    """Delete a client."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM clients WHERE id = ?', (client_id,))
    cursor.execute('DELETE FROM client_product_prices WHERE client_id = ?', (client_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/production/clients/<int:client_id>', methods=['PUT'])
def update_client(client_id):
    """Update a client's details including billing/shipping addresses."""
    data = request.get_json() or {}
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Build update query dynamically
    updates = []
    values = []
    
    if 'name' in data:
        updates.append('name = ?')
        values.append(data['name'])
    if 'contact_info' in data:
        updates.append('contact_info = ?')
        values.append(data['contact_info'])
    if 'billing_address' in data:
        updates.append('billing_address = ?')
        values.append(data['billing_address'])
    if 'shipping_address' in data:
        updates.append('shipping_address = ?')
        values.append(data['shipping_address'])
    if 'country' in data:
        updates.append('country = ?')
        values.append(data['country'])
    
    if updates:
        values.append(client_id)
        cursor.execute(f'UPDATE clients SET {", ".join(updates)} WHERE id = ?', values)
        conn.commit()
    
    conn.close()
    return jsonify({'success': True})

@app.route('/api/production/orders')
def get_latest_orders():
    """Get latest orders for the timeline view - extracted from JTape and other invoices."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Get orders from parsed invoices that look like orders (PO numbers, etc.)
    cursor.execute('''
        SELECT 
            pi.id,
            pi.invoice_number as order_number,
            pi.invoice_date as date,
            pi.amount,
            pi.currency,
            e.from_address as customer,
            e.date_received
        FROM parsed_invoices pi
        JOIN emails e ON pi.email_id = e.id
        WHERE pi.invoice_number IS NOT NULL 
          AND pi.invoice_number != ''
          AND (pi.hidden IS NULL OR pi.hidden = 0)
        ORDER BY e.date_received DESC
        LIMIT 20
    ''')
    
    orders = []
    for row in cursor.fetchall():
        amount = row['amount']
        if row['currency'] == 'RON' and amount:
            amount = amount / 4.97
        
        orders.append({
            'id': row['id'],
            'order_number': row['order_number'],
            'date': row['date_received'][:10] if row['date_received'] else row['date'],
            'amount': amount,
            'customer': row['customer'][:30] if row['customer'] else ''
        })
    
    conn.close()
    return jsonify(orders)

# ========== FILE UPLOAD ENDPOINTS ==========

# Create upload directories
import os
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads')
PRODUCTION_FILES_FOLDER = os.path.join(UPLOAD_FOLDER, 'production')
FEEDBACK_FILES_FOLDER = os.path.join(UPLOAD_FOLDER, 'feedback')
ORG_FILES_FOLDER = os.path.join(UPLOAD_FOLDER, 'organizations')

for folder in [UPLOAD_FOLDER, PRODUCTION_FILES_FOLDER, FEEDBACK_FILES_FOLDER, ORG_FILES_FOLDER]:
    os.makedirs(folder, exist_ok=True)

# Create production_files table
def init_production_files_table():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS production_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client TEXT,
                filename TEXT,
                filepath TEXT,
                description TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS organization_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                domain TEXT,
                filename TEXT,
                filepath TEXT,
                description TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()

init_production_files_table()

@app.route('/uploads/<path:filename>')
def serve_upload(filename):
    """Serve uploaded files."""
    return send_from_directory(UPLOAD_FOLDER, filename)

@app.route('/api/production/feedback/upload', methods=['POST'])
def upload_feedback_image():
    """Upload image for a feedback entry."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    
    file = request.files['file']
    feedback_id = request.form.get('feedback_id')
    
    if not file.filename or not feedback_id:
        return jsonify({'error': 'Missing file or feedback_id'}), 400
    
    # Save file
    from werkzeug.utils import secure_filename
    import uuid
    filename = f"{uuid.uuid4().hex}_{secure_filename(file.filename)}"
    filepath = os.path.join(FEEDBACK_FILES_FOLDER, filename)
    file.save(filepath)
    
    # Update feedback with file info
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT files FROM production_feedback WHERE id = ?', (feedback_id,))
    row = cursor.fetchone()
    
    if row:
        files = json.loads(row['files'] or '[]')
        files.append({
            'name': file.filename,
            'path': f'/uploads/feedback/{filename}'
        })
        cursor.execute('UPDATE production_feedback SET files = ? WHERE id = ?', (json.dumps(files), feedback_id))
        conn.commit()
    
    conn.close()
    return jsonify({'success': True, 'path': f'/uploads/feedback/{filename}'})

@app.route('/api/open-file', methods=['POST'])
def open_file_in_finder():
    """Open a file in Finder (macOS) or file explorer."""
    import subprocess
    import os
    
    data = request.get_json() or {}
    filepath = data.get('path', '')
    
    if not filepath:
        return jsonify({'success': False, 'error': 'No path provided'})
    
    # Convert relative path to absolute if needed
    if filepath.startswith('/'):
        # Check if it's a URL path (starts with /uploads or similar)
        if filepath.startswith('/uploads') or filepath.startswith('/static'):
            # Convert to absolute filesystem path
            base_dir = os.path.dirname(os.path.abspath(__file__))
            filepath = os.path.join(base_dir, filepath.lstrip('/'))
    
    try:
        if os.path.exists(filepath):
            # macOS: open in Finder and reveal the file
            subprocess.run(['open', '-R', filepath], check=True)
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'File not found'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/production/files', methods=['GET'])
def get_production_files():
    """Get production files, optionally filtered by client."""
    client = request.args.get('client', '')
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if client:
        cursor.execute('SELECT * FROM production_files WHERE client = ? ORDER BY created_at DESC', (client,))
    else:
        cursor.execute('SELECT * FROM production_files ORDER BY created_at DESC')
    
    files = []
    for row in cursor.fetchall():
        files.append({
            'id': row['id'],
            'client': row['client'],
            'filename': row['filename'],
            'path': row['filepath'],
            'description': row['description'],
            'created_at': row['created_at']
        })
    
    conn.close()
    return jsonify(files)

@app.route('/api/production/files/upload', methods=['POST'])
def upload_production_file():
    """Upload a production file."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    
    file = request.files['file']
    client = request.form.get('client', '')
    description = request.form.get('description', '')
    
    if not file.filename:
        return jsonify({'error': 'No filename'}), 400
    
    from werkzeug.utils import secure_filename
    import uuid
    
    # Create client subfolder
    client_folder = os.path.join(PRODUCTION_FILES_FOLDER, secure_filename(client) or 'uncategorized')
    os.makedirs(client_folder, exist_ok=True)
    
    filename = f"{uuid.uuid4().hex[:8]}_{secure_filename(file.filename)}"
    filepath = os.path.join(client_folder, filename)
    file.save(filepath)
    
    # Store in database
    relative_path = f'/uploads/production/{secure_filename(client) or "uncategorized"}/{filename}'
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO production_files (client, filename, filepath, description)
        VALUES (?, ?, ?, ?)
    ''', (client, file.filename, relative_path, description))
    conn.commit()
    conn.close()
    
    return jsonify({'success': True, 'path': relative_path})

@app.route('/api/production/files/<int:file_id>', methods=['DELETE'])
def delete_production_file(file_id):
    """Delete a production file."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('SELECT filepath FROM production_files WHERE id = ?', (file_id,))
    row = cursor.fetchone()
    
    if row:
        # Delete file from disk
        filepath = os.path.join(os.path.dirname(__file__), row['filepath'].lstrip('/'))
        if os.path.exists(filepath):
            os.remove(filepath)
        
        cursor.execute('DELETE FROM production_files WHERE id = ?', (file_id,))
        conn.commit()
    
    conn.close()
    return jsonify({'success': True})

@app.route('/api/organization/files/upload', methods=['POST'])
def upload_org_file():
    """Upload a file for an organization."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    
    file = request.files['file']
    domain = request.form.get('domain', '')
    description = request.form.get('description', '')
    
    if not file.filename:
        return jsonify({'error': 'No filename'}), 400
    
    from werkzeug.utils import secure_filename
    import uuid
    
    # Create domain subfolder
    domain_folder = os.path.join(ORG_FILES_FOLDER, secure_filename(domain) or 'uncategorized')
    os.makedirs(domain_folder, exist_ok=True)
    
    filename = f"{uuid.uuid4().hex[:8]}_{secure_filename(file.filename)}"
    filepath = os.path.join(domain_folder, filename)
    file.save(filepath)
    
    # Store in database
    relative_path = f'/uploads/organizations/{secure_filename(domain) or "uncategorized"}/{filename}'
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO organization_files (domain, filename, filepath, description)
        VALUES (?, ?, ?, ?)
    ''', (domain, file.filename, relative_path, description))
    conn.commit()
    conn.close()
    
    return jsonify({'success': True, 'path': relative_path})

@app.route('/api/organization/files/<domain>')
def get_org_files(domain):
    """Get files for an organization."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM organization_files WHERE domain = ? ORDER BY created_at DESC', (domain,))
    
    files = []
    for row in cursor.fetchall():
        files.append({
            'id': row['id'],
            'filename': row['filename'],
            'path': row['filepath'],
            'description': row['description'],
            'created_at': row['created_at']
        })
    
    conn.close()
    return jsonify(files)

@app.route('/api/organization/files/delete/<int:file_id>', methods=['DELETE'])
def delete_org_file(file_id):
    """Delete an organization file."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('SELECT filepath FROM organization_files WHERE id = ?', (file_id,))
    row = cursor.fetchone()
    
    if row:
        # Delete file from disk
        filepath = os.path.join(os.path.dirname(__file__), row['filepath'].lstrip('/'))
        if os.path.exists(filepath):
            os.remove(filepath)
        
        cursor.execute('DELETE FROM organization_files WHERE id = ?', (file_id,))
        conn.commit()
    
    conn.close()
    return jsonify({'success': True})

# Pro Forma Invoice Routes
@app.route('/proforma')
def proforma_page():
    """Serve the pro forma invoice creation page."""
    return render_template('proforma.html')

@app.route('/api/proforma/save', methods=['POST'])
def save_proforma():
    """Save a pro forma invoice."""
    data = request.get_json() or {}
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Create table if not exists
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS proforma_invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_number TEXT UNIQUE,
            invoice_date DATE,
            valid_until DATE,
            buyers_ref TEXT,
            bill_to TEXT,
            ship_to TEXT,
            items TEXT,
            tax_rate REAL DEFAULT 0,
            shipping REAL DEFAULT 0,
            subtotal REAL DEFAULT 0,
            total REAL DEFAULT 0,
            notes TEXT,
            status TEXT DEFAULT 'draft',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Add buyers_ref column if it doesn't exist (for existing databases)
    try:
        cursor.execute('ALTER TABLE proforma_invoices ADD COLUMN buyers_ref TEXT')
    except:
        pass  # Column already exists
    
    # Check if invoice number already exists
    cursor.execute('SELECT id FROM proforma_invoices WHERE invoice_number = ?', (data.get('invoice_number'),))
    existing = cursor.fetchone()
    
    if existing:
        # Update existing
        cursor.execute('''
            UPDATE proforma_invoices SET
                invoice_date = ?, valid_until = ?, buyers_ref = ?, bill_to = ?, ship_to = ?,
                items = ?, tax_rate = ?, shipping = ?, subtotal = ?, total = ?,
                notes = ?, status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE invoice_number = ?
        ''', (
            data.get('invoice_date'), data.get('valid_until'), data.get('buyers_ref', ''),
            data.get('bill_to'), data.get('ship_to'),
            json.dumps(data.get('items', [])), data.get('tax_rate', 0),
            data.get('shipping', 0), data.get('subtotal', 0), data.get('total', 0),
            data.get('notes'), data.get('status', 'UNPAID'), data.get('invoice_number')
        ))
    else:
        # Insert new
        cursor.execute('''
            INSERT INTO proforma_invoices 
            (invoice_number, invoice_date, valid_until, buyers_ref, bill_to, ship_to, items, tax_rate, shipping, subtotal, total, notes, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            data.get('invoice_number'), data.get('invoice_date'), data.get('valid_until'),
            data.get('buyers_ref', ''), data.get('bill_to'), data.get('ship_to'),
            json.dumps(data.get('items', [])), data.get('tax_rate', 0),
            data.get('shipping', 0), data.get('subtotal', 0), data.get('total', 0),
            data.get('notes'), data.get('status', 'UNPAID')
        ))
    
    conn.commit()
    conn.close()
    
    return jsonify({'success': True, 'message': 'Pro forma invoice saved'})

@app.route('/api/proforma/list')
def list_proformas():
    """List all pro forma invoices."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Create table if not exists
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS proforma_invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_number TEXT UNIQUE,
            invoice_date DATE,
            valid_until DATE,
            bill_to TEXT,
            ship_to TEXT,
            items TEXT,
            tax_rate REAL DEFAULT 0,
            shipping REAL DEFAULT 0,
            subtotal REAL DEFAULT 0,
            total REAL DEFAULT 0,
            notes TEXT,
            status TEXT DEFAULT 'draft',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('SELECT * FROM proforma_invoices ORDER BY created_at DESC')
    proformas = []
    for row in cursor.fetchall():
        proformas.append({
            'id': row['id'],
            'invoice_number': row['invoice_number'],
            'invoice_date': row['invoice_date'],
            'valid_until': row['valid_until'],
            'bill_to': row['bill_to'],
            'total': row['total'],
            'status': row['status'],
            'created_at': row['created_at']
        })
    
    conn.close()
    return jsonify(proformas)

@app.route('/api/proforma/<int:proforma_id>')
def get_proforma(proforma_id):
    """Get a single pro forma invoice."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM proforma_invoices WHERE id = ?', (proforma_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        return jsonify({'error': 'Not found'}), 404
    
    return jsonify({
        'id': row['id'],
        'invoice_number': row['invoice_number'],
        'invoice_date': row['invoice_date'],
        'valid_until': row['valid_until'],
        'buyers_ref': row['buyers_ref'] if 'buyers_ref' in row.keys() else '',
        'bill_to': row['bill_to'],
        'ship_to': row['ship_to'],
        'items': json.loads(row['items'] or '[]'),
        'tax_rate': row['tax_rate'],
        'shipping': row['shipping'],
        'subtotal': row['subtotal'],
        'total': row['total'],
        'notes': row['notes'],
        'status': row['status']
    })

@app.route('/api/proforma/delete/<int:proforma_id>', methods=['DELETE'])
def delete_proforma(proforma_id):
    """Delete a pro forma invoice."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM proforma_invoices WHERE id = ?', (proforma_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/proforma/status/<int:proforma_id>', methods=['POST'])
def update_proforma_status(proforma_id):
    """Update pro forma invoice status (PAID/UNPAID)."""
    data = request.get_json() or {}
    status = data.get('status', 'UNPAID')
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE proforma_invoices SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?', 
                   (status, proforma_id))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

# ============== INVOICE (My Invoices) ROUTES ==============

@app.route('/invoice')
def invoice_page():
    """Serve the invoice creation page."""
    return render_template('invoice.html')

@app.route('/api/invoice/save', methods=['POST'])
def save_invoice():
    """Save an invoice."""
    data = request.get_json() or {}
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Create table if not exists
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_number TEXT UNIQUE,
            invoice_date DATE,
            due_date DATE,
            po_ref TEXT,
            bill_to TEXT,
            ship_to TEXT,
            items TEXT,
            tax_rate REAL DEFAULT 0,
            shipping REAL DEFAULT 0,
            subtotal REAL DEFAULT 0,
            total REAL DEFAULT 0,
            notes TEXT,
            status TEXT DEFAULT 'UNPAID',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Check if invoice number already exists
    cursor.execute('SELECT id FROM invoices WHERE invoice_number = ?', (data.get('invoice_number'),))
    existing = cursor.fetchone()
    
    if existing:
        # Update existing
        cursor.execute('''
            UPDATE invoices SET
                invoice_date = ?, due_date = ?, po_ref = ?, bill_to = ?, ship_to = ?,
                items = ?, tax_rate = ?, shipping = ?, subtotal = ?, total = ?,
                notes = ?, status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE invoice_number = ?
        ''', (
            data.get('invoice_date'), data.get('due_date'), data.get('po_ref', ''),
            data.get('bill_to'), data.get('ship_to'),
            json.dumps(data.get('items', [])), data.get('tax_rate', 0),
            data.get('shipping', 0), data.get('subtotal', 0), data.get('total', 0),
            data.get('notes'), data.get('status', 'UNPAID'), data.get('invoice_number')
        ))
    else:
        # Insert new
        cursor.execute('''
            INSERT INTO invoices 
            (invoice_number, invoice_date, due_date, po_ref, bill_to, ship_to, items, tax_rate, shipping, subtotal, total, notes, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            data.get('invoice_number'), data.get('invoice_date'), data.get('due_date'),
            data.get('po_ref', ''), data.get('bill_to'), data.get('ship_to'),
            json.dumps(data.get('items', [])), data.get('tax_rate', 0),
            data.get('shipping', 0), data.get('subtotal', 0), data.get('total', 0),
            data.get('notes'), data.get('status', 'UNPAID')
        ))
    
    conn.commit()
    conn.close()
    
    return jsonify({'success': True, 'message': 'Invoice saved'})

@app.route('/api/invoice/list')
def list_invoices():
    """List all invoices."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Create table if not exists
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_number TEXT UNIQUE,
            invoice_date DATE,
            due_date DATE,
            po_ref TEXT,
            bill_to TEXT,
            ship_to TEXT,
            items TEXT,
            tax_rate REAL DEFAULT 0,
            shipping REAL DEFAULT 0,
            subtotal REAL DEFAULT 0,
            total REAL DEFAULT 0,
            notes TEXT,
            status TEXT DEFAULT 'UNPAID',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('SELECT * FROM invoices ORDER BY created_at DESC')
    invoices = []
    for row in cursor.fetchall():
        invoices.append({
            'id': row['id'],
            'invoice_number': row['invoice_number'],
            'invoice_date': row['invoice_date'],
            'due_date': row['due_date'],
            'bill_to': row['bill_to'],
            'total': row['total'],
            'status': row['status'],
            'created_at': row['created_at']
        })
    
    conn.close()
    return jsonify(invoices)

@app.route('/api/invoice/<int:invoice_id>')
def get_invoice(invoice_id):
    """Get a single invoice."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM invoices WHERE id = ?', (invoice_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        return jsonify({'error': 'Not found'}), 404
    
    return jsonify({
        'id': row['id'],
        'invoice_number': row['invoice_number'],
        'invoice_date': row['invoice_date'],
        'due_date': row['due_date'],
        'po_ref': row['po_ref'] if 'po_ref' in row.keys() else '',
        'bill_to': row['bill_to'],
        'ship_to': row['ship_to'],
        'items': json.loads(row['items'] or '[]'),
        'tax_rate': row['tax_rate'],
        'shipping': row['shipping'],
        'subtotal': row['subtotal'],
        'total': row['total'],
        'notes': row['notes'],
        'status': row['status']
    })

@app.route('/api/invoice/delete/<int:invoice_id>', methods=['DELETE'])
def delete_invoice(invoice_id):
    """Delete an invoice."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM invoices WHERE id = ?', (invoice_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/invoice/status/<int:invoice_id>', methods=['POST'])
def update_invoice_status(invoice_id):
    """Update invoice status."""
    data = request.get_json() or {}
    status = data.get('status', 'UNPAID')
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE invoices SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?', 
                   (status, invoice_id))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

# ============ Google Calendar API Routes ============

@app.route('/api/calendar/auth')
def calendar_auth():
    """Initiate Google OAuth flow."""
    auth_url = (
        f"https://accounts.google.com/o/oauth2/v2/auth?"
        f"client_id={GOOGLE_CLIENT_ID}&"
        f"redirect_uri={GOOGLE_REDIRECT_URI}&"
        f"response_type=code&"
        f"scope={' '.join(GOOGLE_SCOPES)}&"
        f"access_type=offline&"
        f"prompt=consent"
    )
    return redirect(auth_url)

@app.route('/api/calendar/callback')
def calendar_callback():
    """Handle OAuth callback from Google."""
    code = request.args.get('code')
    if not code:
        return redirect('/?calendar_error=no_code')
    
    # Exchange code for tokens
    token_url = 'https://oauth2.googleapis.com/token'
    token_data = {
        'code': code,
        'client_id': GOOGLE_CLIENT_ID,
        'client_secret': GOOGLE_CLIENT_SECRET,
        'redirect_uri': GOOGLE_REDIRECT_URI,
        'grant_type': 'authorization_code'
    }
    
    try:
        response = requests.post(token_url, data=token_data)
        tokens = response.json()
        
        if 'access_token' in tokens:
            # Save tokens to file for persistence
            save_google_tokens(tokens)
            return redirect('/?calendar_connected=true')
        else:
            return redirect(f'/?calendar_error={tokens.get("error", "unknown")}')
    except Exception as e:
        return redirect(f'/?calendar_error={str(e)}')

@app.route('/api/calendar/status')
def calendar_status():
    """Check if calendar is connected."""
    tokens = load_google_tokens()
    connected = tokens is not None and 'access_token' in tokens
    return jsonify({'connected': connected})

@app.route('/api/calendar/disconnect', methods=['POST'])
def calendar_disconnect():
    """Disconnect Google Calendar."""
    if os.path.exists(GOOGLE_TOKEN_FILE):
        os.remove(GOOGLE_TOKEN_FILE)
    return jsonify({'success': True})

@app.route('/api/calendar/events')
def get_calendar_events():
    """Get calendar events from 'Dust Free Calendar' for the specified month."""
    access_token = get_google_access_token()
    if not access_token:
        return jsonify({'error': 'Not connected', 'events': []})
    
    # Get month/year from query params (default to current month)
    year = request.args.get('year', datetime.now().year, type=int)
    month = request.args.get('month', datetime.now().month, type=int)
    
    # Calculate start and end of month
    from calendar import monthrange
    first_day = datetime(year, month, 1)
    last_day = datetime(year, month, monthrange(year, month)[1], 23, 59, 59)
    
    time_min = first_day.isoformat() + 'Z'
    time_max = last_day.isoformat() + 'Z'
    
    try:
        headers = {'Authorization': f'Bearer {access_token}'}
        
        # First, get list of calendars to find "Dust Free Calendar"
        calendars_url = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
        cal_response = requests.get(calendars_url, headers=headers)
        
        if cal_response.status_code == 401:
            if os.path.exists(GOOGLE_TOKEN_FILE):
                os.remove(GOOGLE_TOKEN_FILE)
            return jsonify({'error': 'Token expired', 'events': []})
        
        calendars_data = cal_response.json()
        dust_free_calendar_id = None
        
        # Find the "Dust Free Calendar" or "Dust Free" calendar
        for cal in calendars_data.get('items', []):
            cal_name = cal.get('summary', '').lower()
            if 'dust free' in cal_name:
                dust_free_calendar_id = cal.get('id')
                break
        
        if not dust_free_calendar_id:
            return jsonify({'error': 'Dust Free Calendar not found', 'events': [], 'calendars': [c.get('summary') for c in calendars_data.get('items', [])]})
        
        # Get events from Dust Free Calendar for the month
        events_url = (
            f"https://www.googleapis.com/calendar/v3/calendars/{dust_free_calendar_id}/events?"
            f"timeMin={time_min}&timeMax={time_max}&maxResults=100&singleEvents=true&orderBy=startTime"
        )
        
        response = requests.get(events_url, headers=headers)
        data = response.json()
        
        events = []
        for event in data.get('items', []):
            start = event.get('start', {})
            end = event.get('end', {})
            events.append({
                'id': event.get('id'),
                'title': event.get('summary', 'No title'),
                'start': start.get('dateTime', start.get('date', '')),
                'end': end.get('dateTime', end.get('date', '')),
                'location': event.get('location', ''),
                'description': event.get('description', '')[:100] if event.get('description') else '',
                'allDay': 'date' in start and 'dateTime' not in start
            })
        
        return jsonify({'events': events, 'year': year, 'month': month})
    except Exception as e:
        return jsonify({'error': str(e), 'events': []})

@app.route('/api/calendar/debug')
def calendar_debug():
    """Debug endpoint to list all calendars and task lists."""
    access_token = get_google_access_token()
    if not access_token:
        return jsonify({'error': 'Not connected - click Connect Google Calendar button first'})
    
    headers = {'Authorization': f'Bearer {access_token}'}
    result = {'calendars': [], 'task_lists': []}
    
    # Get all calendars
    try:
        cal_response = requests.get("https://www.googleapis.com/calendar/v3/users/me/calendarList", headers=headers)
        if cal_response.status_code == 200:
            for cal in cal_response.json().get('items', []):
                result['calendars'].append({'id': cal.get('id'), 'name': cal.get('summary')})
        else:
            result['calendar_error'] = cal_response.json()
    except Exception as e:
        result['calendar_error'] = str(e)
    
    # Get all task lists
    try:
        tasks_response = requests.get("https://tasks.googleapis.com/tasks/v1/users/@me/lists", headers=headers)
        if tasks_response.status_code == 200:
            for tl in tasks_response.json().get('items', []):
                result['task_lists'].append({'id': tl.get('id'), 'name': tl.get('title')})
        else:
            result['tasks_error'] = tasks_response.json()
    except Exception as e:
        result['tasks_error'] = str(e)
    
    return jsonify(result)

@app.route('/api/calendar/tasks')
def get_calendar_tasks():
    """Get tasks from Google Tasks - only from 'Dust Free' list."""
    access_token = get_google_access_token()
    if not access_token:
        return jsonify({'error': 'Not connected', 'tasks': []})
    
    try:
        # First get task lists
        lists_url = "https://tasks.googleapis.com/tasks/v1/users/@me/lists"
        headers = {'Authorization': f'Bearer {access_token}'}
        
        lists_response = requests.get(lists_url, headers=headers)
        if lists_response.status_code == 401:
            if os.path.exists(GOOGLE_TOKEN_FILE):
                os.remove(GOOGLE_TOKEN_FILE)
            return jsonify({'error': 'Token expired', 'tasks': []})
        
        lists_data = lists_response.json()
        all_tasks = []
        
        # Only get tasks from "Dust Free" list
        for task_list in lists_data.get('items', []):
            list_id = task_list.get('id')
            list_title = task_list.get('title', 'Tasks')
            
            # Only process "Dust Free" list (strip whitespace for matching)
            if list_title.strip().lower() != 'dust free':
                continue
            
            tasks_url = f"https://tasks.googleapis.com/tasks/v1/lists/{list_id}/tasks?showCompleted=false&maxResults=50"
            tasks_response = requests.get(tasks_url, headers=headers)
            tasks_data = tasks_response.json()
            
            for task in tasks_data.get('items', []):
                if task.get('title'):  # Skip empty tasks
                    all_tasks.append({
                        'id': task.get('id'),
                        'title': task.get('title'),
                        'notes': task.get('notes', ''),
                        'due': task.get('due', ''),
                        'status': task.get('status'),
                        'list': list_title
                    })
        
        return jsonify({'tasks': all_tasks})
    except Exception as e:
        return jsonify({'error': str(e), 'tasks': []})

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=8080)
