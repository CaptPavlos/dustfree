# DFW Management Dashboard

A comprehensive business management dashboard for email archiving, invoice management, production tracking, and organization management.

## Architecture Overview

### Technology Stack
- **Backend**: Python Flask (149KB, 93 API endpoints)
- **Database**: SQLite (`email_archive.db` - 114MB)
- **Vector Database**: ChromaDB with sentence-transformers for semantic search
- **Frontend**: Single-page HTML with Tailwind CSS & vanilla JavaScript
- **AI Assistant**: Jimmy (powered by Perplexity API)

### Database Schema (17 tables)
| Table | Purpose |
|-------|---------|
| `emails` | 4,592 archived emails |
| `attachments` | 3,904 PDF/file attachments (647MB) |
| `parsed_invoices` | 914 OCR-parsed invoices |
| `production_runs` | Production tracking |
| `production_feedback` | Production notes |
| `clients` | Client management |
| `products` | Product catalog |
| `client_product_prices` | Client-specific pricing |
| `proforma_invoices` | Pro forma invoices |
| `entity_categories` | Organization categorization |
| `organization_names` | Custom org display names |
| `organization_files` | Files linked to orgs |
| `organization_relationships` | Org-to-org relationships |
| `email_organization_assignments` | Email-to-org mapping |
| `email_read_status` | Read/unread tracking |
| `organisation_details` | Org metadata |
| `app_settings` | App configuration |

### Vector Database (ChromaDB)
Two collections for semantic search:
1. **emails** - Email subjects + bodies for natural language search
2. **invoices** - OCR-extracted invoice text for invoice search

Uses `all-MiniLM-L6-v2` model (runs locally, no API calls)

### Key Features
- **Email Management**: Archive, search, categorize emails
- **Invoice Processing**: OCR parsing, amount extraction, tab organization
- **Production Tracking**: Orders, runs, scheduling
- **Organization Management**: Contacts, relationships, file linking
- **AI Chat**: Natural language queries via Jimmy
- **Calendar Integration**: Google Calendar/Tasks sync
- **Relationship Chart**: Visual org categorization

## Original Features

- Download emails from any IMAP server (Gmail, Outlook, etc.)
- Store emails in a local SQLite database
- View downloaded emails in the terminal
- Supports both text and HTML emails
- Preserves email headers and metadata

## Prerequisites

- Python 3.7+
- pip (Python package manager)

## Installation

1. Clone this repository or download the files
2. Install the required dependencies:

```bash
pip install -r requirements.txt
```

## Configuration

1. For Gmail, you'll need to enable "Less secure app access" or use an App Password if you have 2FA enabled.
2. For other email providers, make sure IMAP access is enabled in your email settings.

## Usage

### Download Emails

```bash
python email_archiver.py download your.email@example.com --server imap.example.com
```

You'll be prompted for your email password. To avoid entering it each time, you can provide it as an argument (not recommended for security reasons):

```bash
python email_archiver.py download your.email@example.com --password yourpassword --server imap.example.com
```

### List Downloaded Emails

```bash
python email_archiver.py list --limit 20
```

### View a Specific Email

```bash
python email_archiver.py view 1
```

## Command Line Options

### Download Command

```
python email_archiver.py download EMAIL [--password PASSWORD] [--server SERVER] [--folder FOLDER] [--limit LIMIT]

positional arguments:
  email                 Email address to connect to

options:
  --password PASSWORD   Email password (or leave empty to be prompted)
  --server SERVER       IMAP server (default: imap.gmail.com)
  --folder FOLDER       Email folder to download from (default: INBOX)
  --limit LIMIT         Maximum number of emails to download (default: 100)
```

### List Command

```
python email_archiver.py list [--limit LIMIT]

options:
  --limit LIMIT  Maximum number of emails to show (default: 10)
```

### View Command

```
python email_archiver.py view EMAIL_ID

positional arguments:
  email_id    Email ID to view (get the ID from the list command)
```

## Deployment (Railway/Render)

### Environment Variables Required
```
FLASK_SECRET_KEY=your-production-secret-key
PERPLEXITY_API_KEY=your-perplexity-key
GOOGLE_OAUTH_CLIENT_ID=your-client-id
GOOGLE_OAUTH_CLIENT_SECRET=your-client-secret
GOOGLE_REDIRECT_URI=https://your-domain.com/api/calendar/callback
PORT=8080
```

### Files to Upload
- `app.py` - Main application
- `templates/` - HTML templates
- `requirements.txt` - Dependencies
- `Procfile` - Gunicorn config
- `railway.json` - Railway config
- `email_archive.db` - Database (114MB)
- `attachments/` - PDF files (647MB)
- `chroma_db/` - Vector database

### Production Command
```bash
gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120
```

## Security Notes

- Your email password is only used to authenticate with the IMAP server and is not stored anywhere.
- The SQLite database is stored locally on your machine.
- For added security, consider using an App Password instead of your main email password.
- In production, ensure `FLASK_SECRET_KEY` is a strong random value.

## License

This project is open source and available under the MIT License.
