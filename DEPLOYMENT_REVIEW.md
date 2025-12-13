# Code Review & Deployment Guide

## Current Architecture Overview

### File Sizes
| Component | Size |
|-----------|------|
| `email_archive.db` | 113 MB |
| `attachments/` | 647 MB |
| `chroma_db/` | 164 KB |
| `uploads/` | 3.3 MB |
| `app.py` | 2,745 lines |
| `templates/index.html` | 3,919 lines |

### Tech Stack
- **Backend**: Flask (Python 3.x)
- **Database**: SQLite (local file)
- **Vector DB**: ChromaDB with sentence-transformers
- **AI**: Perplexity API for chat
- **File Storage**: Local filesystem
- **Frontend**: Single HTML file with inline JS/CSS (TailwindCSS CDN)

---

## Part 1: Localhost Optimization

### Current Issues

#### 1. **Large Single Files**
- `app.py` is 2,745 lines - hard to maintain
- `index.html` is 3,919 lines - slow to load/edit

#### 2. **Database Performance**
- SQLite is fine for single-user localhost
- No connection pooling
- No query optimization/indexes

#### 3. **Memory Usage**
- ChromaDB + sentence-transformers loads ~500MB model into RAM
- All attachments parsed on demand

#### 4. **No Caching**
- Cache headers disabled for development
- API responses not cached

### Recommended Optimizations

#### A. Code Organization
```
windsurf-project/
├── app/
│   ├── __init__.py          # Flask app factory
│   ├── config.py             # Configuration
│   ├── models.py             # Database models
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── api_emails.py
│   │   ├── api_invoices.py
│   │   ├── api_production.py
│   │   └── api_search.py
│   └── utils/
│       ├── db.py
│       └── parsers.py
├── static/
│   ├── css/
│   │   └── styles.css
│   └── js/
│       ├── main.js
│       ├── invoices.js
│       └── production.js
├── templates/
│   ├── base.html
│   ├── partials/
│   └── index.html
└── run.py
```

#### B. Database Optimizations
```python
# Add indexes to frequently queried columns
CREATE INDEX idx_emails_from ON emails(from_address);
CREATE INDEX idx_emails_date ON emails(date_received);
CREATE INDEX idx_parsed_inv_number ON parsed_invoices(invoice_number);
CREATE INDEX idx_parsed_assigned ON parsed_invoices(assigned_tab);
```

#### C. Caching (for production)
```python
from functools import lru_cache

@lru_cache(maxsize=100)
def get_invoice_summary(tab_name):
    # Cache invoice totals
    pass
```

#### D. Lazy Loading ChromaDB
```python
# Only load when needed
_chroma_client = None

def get_chroma():
    global _chroma_client
    if _chroma_client is None:
        _chroma_client = init_chromadb()
    return _chroma_client
```

---

## Part 2: Vercel Deployment

### Critical Blockers for Vercel

Vercel is designed for **serverless functions** and **static sites**. Your app has several incompatibilities:

| Feature | Current | Vercel Support |
|---------|---------|----------------|
| SQLite DB (113MB) | Local file | ❌ No persistent filesystem |
| Attachments (647MB) | Local folder | ❌ No persistent storage |
| ChromaDB | Local folder | ❌ No persistent storage |
| File uploads | Local disk | ❌ Serverless = ephemeral |
| Flask server | Long-running | ⚠️ Serverless only |
| Model loading | 500MB RAM | ⚠️ Limited to 1GB |

### Why Vercel Won't Work (As-Is)

1. **No Persistent Filesystem**: Vercel functions are ephemeral - files disappear
2. **Database Size**: 113MB SQLite can't be bundled with functions
3. **Cold Start**: Loading sentence-transformers takes 10-30 seconds
4. **Function Timeout**: Max 60 seconds (Pro), your queries may exceed this
5. **Memory Limit**: 1GB max, ChromaDB + model needs ~600MB

---

## Part 3: Alternative Hosting Options

### Option A: Railway.app (Recommended) ✅ PREPARED
**Best for your use case**

| Feature | Support |
|---------|---------|
| SQLite | ✅ Persistent volumes |
| File uploads | ✅ Persistent storage |
| Flask | ✅ Full support |
| Memory | ✅ Up to 8GB |
| Cost | $5-20/month |

**Files Created for Railway:**
- `Procfile` - Gunicorn server configuration
- `railway.json` - Railway deployment settings
- `.gitignore` - Excludes sensitive/large files
- `requirements.txt` - Updated with gunicorn

**Deployment Steps:**
```bash
# 1. Install Railway CLI
npm install -g @railway/cli

# 2. Login to Railway
railway login

# 3. Initialize new project (in your project directory)
cd /path/to/windsurf-project
railway init

# 4. Create a volume for persistent storage
railway volume add --mount /app/data

# 5. Set environment variables
railway variables set IMAP_EMAIL=your_email
railway variables set IMAP_PASSWORD=your_password
railway variables set IMAP_SERVER=your_server
railway variables set ADOBE_CLIENT_ID=your_id
railway variables set ADOBE_CLIENT_SECRET=your_secret

# 6. Deploy
railway up

# 7. Open your deployed app
railway open
```

**Post-Deployment:**
1. Upload your `email_archive.db` to the volume
2. Upload the `attachments/` folder
3. The app will auto-regenerate ChromaDB indexes

### Option B: Render.com
Similar to Railway, good Flask support.

### Option C: DigitalOcean App Platform
Good for larger apps, persistent disks available.

### Option D: Self-hosted VPS (Cheapest)
- DigitalOcean Droplet: $6/month
- Hetzner: €4/month
- Full control, SSH access

---

## Part 4: If You MUST Use Vercel

You'd need to completely re-architect:

### Required Changes

#### 1. Database → Supabase/PlanetScale
```python
# Replace SQLite with PostgreSQL
import psycopg2
from supabase import create_client

SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
```

#### 2. File Storage → Supabase Storage / AWS S3
```python
# Upload files to cloud storage
from supabase import create_client

def upload_file(file, bucket='attachments'):
    supabase.storage.from_(bucket).upload(file.filename, file.read())
```

#### 3. Vector Search → Supabase pgvector / Pinecone
```python
# Replace ChromaDB with Pinecone
import pinecone
pinecone.init(api_key=os.getenv('PINECONE_KEY'))
index = pinecone.Index('emails')
```

#### 4. Convert to Serverless
```python
# vercel.json
{
  "builds": [
    { "src": "api/*.py", "use": "@vercel/python" }
  ],
  "routes": [
    { "src": "/api/(.*)", "dest": "api/$1.py" }
  ]
}
```

#### 5. Split app.py into API Functions
```
api/
├── emails.py
├── invoices.py
├── production.py
└── search.py
```

### Vercel Project Structure
```
project/
├── api/
│   ├── emails.py
│   ├── invoices/
│   │   ├── dfw.py
│   │   └── [tab].py
│   └── search.py
├── public/
│   └── index.html
├── vercel.json
└── requirements.txt
```

### Cost Comparison (Vercel Full Stack)

| Service | Cost |
|---------|------|
| Vercel Pro | $20/month |
| Supabase Pro | $25/month |
| Pinecone Starter | $0-70/month |
| **Total** | **$45-115/month** |

vs.

| Service | Cost |
|---------|------|
| Railway/Render | $5-20/month |

---

## Part 5: Quick Wins for Current Setup

### Immediate Optimizations (No Re-architecture)

#### 1. Add Database Indexes
```sql
-- Run this in SQLite
CREATE INDEX IF NOT EXISTS idx_emails_from ON emails(from_address);
CREATE INDEX IF NOT EXISTS idx_emails_date ON emails(date_received DESC);
CREATE INDEX IF NOT EXISTS idx_parsed_number ON parsed_invoices(invoice_number);
CREATE INDEX IF NOT EXISTS idx_parsed_tab ON parsed_invoices(assigned_tab);
CREATE INDEX IF NOT EXISTS idx_parsed_hidden ON parsed_invoices(hidden);
```

#### 2. Enable Gzip Compression
```python
from flask_compress import Compress
Compress(app)
```

#### 3. Production Mode
```python
if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=8080, threaded=True)
```

#### 4. Use Gunicorn (Production Server)
```bash
pip install gunicorn
gunicorn -w 4 -b 0.0.0.0:8080 app:app
```

---

## Recommendation Summary

| Scenario | Recommended Solution |
|----------|---------------------|
| **Personal use (localhost)** | Keep current setup + add indexes |
| **Share with team (2-5 users)** | Railway.app or Render.com |
| **Public deployment** | VPS (DigitalOcean/Hetzner) |
| **Enterprise/Scale** | Full re-architecture to cloud services |

### My Recommendation

**For your use case**: Deploy to **Railway.app**
- Minimal code changes needed
- Persistent SQLite works
- File uploads work
- ~$10/month
- Easy deployment with Git push

Would you like me to prepare the code for Railway deployment?
