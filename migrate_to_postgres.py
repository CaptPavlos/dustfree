#!/usr/bin/env python3
"""
Migration script to transfer data from local SQLite to Railway PostgreSQL.

Usage:
    DATABASE_URL="postgresql://..." python migrate_to_postgres.py

The DATABASE_URL should be your Railway PostgreSQL connection string.
"""
import os
import sys
import sqlite3

# Check for DATABASE_URL
DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    print("ERROR: DATABASE_URL environment variable is required.")
    print("Usage: DATABASE_URL='postgresql://...' python migrate_to_postgres.py")
    sys.exit(1)

# Fix Railway's postgres:// URL
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

import psycopg2
from psycopg2.extras import execute_values

SQLITE_PATH = 'email_archive.db'

# Tables to migrate (in order to respect foreign keys)
TABLES = [
    'emails',
    'attachments', 
    'parsed_invoices',
    'app_settings',
    'email_read_status',
    'entity_categories',
    'organization_files',
    'organization_names',
    'email_organization_assignments',
    'organization_relationships',
    'production_feedback',
    'production_runs',
    'products',
    'clients',
    'client_product_prices',
    'production_files',
]


def get_sqlite_connection():
    """Get SQLite connection."""
    if not os.path.exists(SQLITE_PATH):
        print(f"ERROR: SQLite database not found at {SQLITE_PATH}")
        sys.exit(1)
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_postgres_connection():
    """Get PostgreSQL connection."""
    return psycopg2.connect(DATABASE_URL)


def get_table_columns(sqlite_cursor, table_name):
    """Get column names for a table."""
    sqlite_cursor.execute(f"PRAGMA table_info({table_name})")
    return [row[1] for row in sqlite_cursor.fetchall()]


def clean_row_data(row_data, columns):
    """Clean row data - convert empty strings to None for date columns."""
    date_columns = ['date', 'date_ordered', 'date_prod_start', 'date_prod_end', 
                    'date_warehouse', 'date_delivered', 'feedback_date', 'date_received',
                    'read_at', 'created_at', 'updated_at']
    cleaned = list(row_data)
    for i, (val, col) in enumerate(zip(cleaned, columns)):
        if col in date_columns and val == '':
            cleaned[i] = None
    return tuple(cleaned)


def migrate_table(sqlite_conn, pg_conn, table_name):
    """Migrate a single table from SQLite to PostgreSQL."""
    sqlite_cursor = sqlite_conn.cursor()
    pg_cursor = pg_conn.cursor()
    
    # Check if table exists in SQLite
    sqlite_cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,)
    )
    if not sqlite_cursor.fetchone():
        print(f"  Skipping {table_name} (not found in SQLite)")
        return 0
    
    # Get columns
    columns = get_table_columns(sqlite_cursor, table_name)
    if not columns:
        print(f"  Skipping {table_name} (no columns)")
        return 0
    
    # Skip 'id' column for tables with SERIAL primary key (PostgreSQL auto-generates)
    if 'id' in columns and table_name not in ['app_settings', 'entity_categories', 
                                                'organization_names', 'email_organization_assignments',
                                                'organization_relationships', 'email_read_status']:
        insert_columns = [c for c in columns if c != 'id']
    else:
        insert_columns = columns
    
    # Get data from SQLite
    sqlite_cursor.execute(f"SELECT * FROM {table_name}")
    rows = sqlite_cursor.fetchall()
    
    if not rows:
        print(f"  {table_name}: 0 rows (empty)")
        return 0
    
    # Prepare data for insertion (exclude 'id' if needed)
    if 'id' in columns and 'id' not in insert_columns:
        id_index = columns.index('id')
        data = [tuple(row[i] for i in range(len(row)) if i != id_index) for row in rows]
    else:
        data = [tuple(row) for row in rows]
    
    # Clear existing data in PostgreSQL table
    pg_cursor.execute(f"DELETE FROM {table_name}")
    
    # Disable foreign key checks for this table during insert
    pg_cursor.execute("SET session_replication_role = 'replica';")
    
    # Insert data
    columns_str = ', '.join(insert_columns)
    placeholders = ', '.join(['%s'] * len(insert_columns))
    
    migrated = 0
    errors = 0
    try:
        for row_data in data:
            try:
                # Clean the data (convert empty strings to None for dates)
                cleaned_data = clean_row_data(row_data, insert_columns)
                pg_cursor.execute(
                    f"INSERT INTO {table_name} ({columns_str}) VALUES ({placeholders})",
                    cleaned_data
                )
                migrated += 1
            except Exception as row_error:
                errors += 1
                if errors <= 3:  # Only show first 3 errors
                    print(f"    Row error: {row_error}")
        
        # Re-enable foreign key checks
        pg_cursor.execute("SET session_replication_role = 'origin';")
        
        # Reset sequence for tables with SERIAL id
        if 'id' in columns and table_name not in ['app_settings', 'entity_categories',
                                                    'organization_names', 'email_organization_assignments',
                                                    'organization_relationships', 'email_read_status']:
            pg_cursor.execute(f"""
                SELECT setval(pg_get_serial_sequence('{table_name}', 'id'), 
                              COALESCE((SELECT MAX(id) FROM {table_name}), 1), 
                              true)
            """)
        
        if errors > 0:
            print(f"  {table_name}: {migrated} rows migrated ({errors} errors)")
        else:
            print(f"  {table_name}: {migrated} rows migrated")
        return migrated
    except Exception as e:
        print(f"  ERROR migrating {table_name}: {e}")
        pg_cursor.execute("SET session_replication_role = 'origin';")
        pg_conn.rollback()
        return 0


def main():
    print("=" * 60)
    print("SQLite to PostgreSQL Migration")
    print("=" * 60)
    print(f"\nSource: {SQLITE_PATH}")
    print(f"Target: {DATABASE_URL[:50]}...")
    print()
    
    # Connect to databases
    print("Connecting to databases...")
    sqlite_conn = get_sqlite_connection()
    pg_conn = get_postgres_connection()
    
    # Initialize PostgreSQL tables
    print("\nInitializing PostgreSQL tables...")
    from database import init_all_tables, USE_POSTGRES
    if not USE_POSTGRES:
        print("ERROR: DATABASE_URL is set but USE_POSTGRES is False. Check database.py")
        sys.exit(1)
    init_all_tables()
    print("Tables initialized.")
    
    # Migrate each table
    print("\nMigrating tables...")
    total_rows = 0
    for table in TABLES:
        rows = migrate_table(sqlite_conn, pg_conn, table)
        total_rows += rows
    
    # Commit changes
    pg_conn.commit()
    
    print("\n" + "=" * 60)
    print(f"Migration complete! Total rows migrated: {total_rows}")
    print("=" * 60)
    
    # Close connections
    sqlite_conn.close()
    pg_conn.close()


if __name__ == '__main__':
    main()
