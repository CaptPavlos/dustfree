"""
Database abstraction layer supporting both SQLite (local) and PostgreSQL (Railway).
"""
import os
import sqlite3
from contextlib import contextmanager

# Check for PostgreSQL connection string (Railway provides DATABASE_URL)
DATABASE_URL = os.environ.get('DATABASE_URL')

# Determine which database to use
USE_POSTGRES = DATABASE_URL is not None

if USE_POSTGRES:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from psycopg2 import IntegrityError as PgIntegrityError
    # Fix Railway's postgres:// URL to postgresql://
    if DATABASE_URL.startswith('postgres://'):
        DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
    # Export the right IntegrityError
    IntegrityError = PgIntegrityError
else:
    IntegrityError = sqlite3.IntegrityError

# SQLite path for local development
SQLITE_PATH = 'email_archive.db'


class DictRow(dict):
    """A dict subclass that allows attribute access like sqlite3.Row."""
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)
    
    def keys(self):
        return super().keys()


def dict_factory(cursor, row):
    """Convert SQLite rows to dictionaries."""
    d = DictRow()
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d


@contextmanager
def get_db_connection():
    """Get a database connection (PostgreSQL or SQLite based on environment)."""
    if USE_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        conn.row_factory = dict_factory
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def get_cursor(conn):
    """Get a cursor with dict-like row access."""
    if USE_POSTGRES:
        return conn.cursor(cursor_factory=RealDictCursor)
    else:
        return conn.cursor()


def execute_query(conn, query, params=None):
    """Execute a query with automatic placeholder conversion."""
    cursor = get_cursor(conn)
    
    if USE_POSTGRES and params:
        # Convert ? to %s for PostgreSQL
        query = query.replace('?', '%s')
    
    if params:
        cursor.execute(query, params)
    else:
        cursor.execute(query)
    
    return cursor


def fetchall(cursor):
    """Fetch all results as list of dicts."""
    rows = cursor.fetchall()
    if USE_POSTGRES:
        return [DictRow(row) for row in rows]
    return rows


def fetchone(cursor):
    """Fetch one result as dict."""
    row = cursor.fetchone()
    if row is None:
        return None
    if USE_POSTGRES:
        return DictRow(row)
    return row


def init_all_tables():
    """Initialize all database tables."""
    with get_db_connection() as conn:
        cursor = get_cursor(conn)
        
        if USE_POSTGRES:
            _init_postgres_tables(cursor)
        else:
            _init_sqlite_tables(cursor)
        
        conn.commit()


def _init_postgres_tables(cursor):
    """Create all tables for PostgreSQL."""
    
    # Emails table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS emails (
            id SERIAL PRIMARY KEY,
            message_id TEXT UNIQUE,
            subject TEXT,
            from_address TEXT,
            to_address TEXT,
            date_received TIMESTAMP,
            body_text TEXT,
            body_html TEXT,
            headers TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            folder TEXT DEFAULT 'INBOX'
        )
    ''')
    
    # Attachments table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS attachments (
            id SERIAL PRIMARY KEY,
            email_id INTEGER REFERENCES emails(id),
            filename TEXT,
            content_type TEXT,
            size INTEGER,
            file_path TEXT,
            file_hash TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Parsed invoices table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS parsed_invoices (
            id SERIAL PRIMARY KEY,
            attachment_id INTEGER REFERENCES attachments(id),
            email_id INTEGER,
            invoice_number TEXT,
            invoice_date TEXT,
            amount REAL,
            currency TEXT,
            vendor TEXT,
            raw_text TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            hidden INTEGER DEFAULT 0,
            assigned_tab TEXT,
            amount_edited INTEGER DEFAULT 0
        )
    ''')
    
    # App settings table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Email read status table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS email_read_status (
            email_id INTEGER PRIMARY KEY,
            is_read INTEGER DEFAULT 0,
            read_at TIMESTAMP
        )
    ''')
    
    # Entity categories table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS entity_categories (
            domain TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Organization files table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS organization_files (
            id SERIAL PRIMARY KEY,
            domain TEXT NOT NULL,
            attachment_id INTEGER REFERENCES attachments(id),
            filename TEXT,
            file_path TEXT,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Organization names table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS organization_names (
            domain TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Email organization assignments table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS email_organization_assignments (
            email_address TEXT PRIMARY KEY,
            organization_domain TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Organization relationships table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS organization_relationships (
            domain TEXT PRIMARY KEY,
            related_domain TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Production feedback table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS production_feedback (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT,
            status TEXT DEFAULT 'pending',
            files TEXT,
            feedback_date DATE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Production runs table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS production_runs (
            id SERIAL PRIMARY KEY,
            order_ref TEXT,
            product TEXT,
            quantity INTEGER,
            status TEXT DEFAULT 'pending',
            notes TEXT,
            date DATE DEFAULT CURRENT_DATE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            client TEXT,
            scheduled_month TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            eta_month TEXT,
            date_ordered DATE,
            downpayment_paid INTEGER DEFAULT 0,
            date_prod_start DATE,
            date_prod_end DATE,
            date_warehouse DATE,
            paid_off INTEGER DEFAULT 0,
            date_delivered DATE,
            price_per_roll REAL DEFAULT 0,
            cost_per_roll REAL DEFAULT 0
        )
    ''')
    
    # Products table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS products (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            description TEXT,
            price REAL DEFAULT 0,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Clients table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS clients (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            contact_info TEXT,
            billing_address TEXT,
            shipping_address TEXT,
            country TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Client-Product pricing table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS client_product_prices (
            id SERIAL PRIMARY KEY,
            client_id INTEGER NOT NULL REFERENCES clients(id),
            product_id INTEGER NOT NULL REFERENCES products(id),
            price REAL NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(client_id, product_id)
        )
    ''')
    
    # Production files table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS production_files (
            id SERIAL PRIMARY KEY,
            client TEXT,
            filename TEXT,
            filepath TEXT,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Create indexes
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_emails_from ON emails(from_address)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_emails_date ON emails(date_received DESC)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_emails_subject ON emails(subject)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_attachments_email ON attachments(email_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_attachments_filename ON attachments(filename)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_org_files_domain ON organization_files(domain)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_production_runs_client ON production_runs(client)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_production_runs_status ON production_runs(status)')


def _init_sqlite_tables(cursor):
    """Create all tables for SQLite (same as before, kept for local dev)."""
    
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
    
    # Email read status table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS email_read_status (
            email_id INTEGER PRIMARY KEY,
            is_read INTEGER DEFAULT 0,
            read_at DATETIME
        )
    ''')
    
    # Entity categories table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS entity_categories (
            domain TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Organization files table
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
    
    # Organization names table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS organization_names (
            domain TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Email organization assignments table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS email_organization_assignments (
            email_address TEXT PRIMARY KEY,
            organization_domain TEXT NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Organization relationships table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS organization_relationships (
            domain TEXT PRIMARY KEY,
            related_domain TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
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
            eta_month TEXT,
            date_ordered DATE,
            downpayment_paid INTEGER DEFAULT 0,
            date_prod_start DATE,
            date_prod_end DATE,
            date_warehouse DATE,
            paid_off INTEGER DEFAULT 0,
            date_delivered DATE,
            price_per_roll REAL DEFAULT 0,
            cost_per_roll REAL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
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
    
    # Clients table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            contact_info TEXT,
            billing_address TEXT,
            shipping_address TEXT,
            country TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Client-Product pricing table
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
    
    # Production files table
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
    
    # Create indexes
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_emails_from ON emails(from_address)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_emails_date ON emails(date_received DESC)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_emails_subject ON emails(subject)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_attachments_email ON attachments(email_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_attachments_filename ON attachments(filename)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_org_files_domain ON organization_files(domain)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_production_runs_client ON production_runs(client)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_production_runs_status ON production_runs(status)')


def get_setting(key, default=None):
    """Get a setting from the database."""
    with get_db_connection() as conn:
        cursor = execute_query(conn, 'SELECT value FROM app_settings WHERE key = ?', (key,))
        row = fetchone(cursor)
        return row['value'] if row else default


def set_setting(key, value):
    """Set a setting in the database."""
    with get_db_connection() as conn:
        if USE_POSTGRES:
            cursor = get_cursor(conn)
            cursor.execute('''
                INSERT INTO app_settings (key, value, updated_at) 
                VALUES (%s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = CURRENT_TIMESTAMP
            ''', (key, value))
        else:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO app_settings (key, value, updated_at) 
                VALUES (?, ?, CURRENT_TIMESTAMP)
            ''', (key, value))
