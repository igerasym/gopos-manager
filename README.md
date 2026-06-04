# ☕ The Frame Manager

Internal management system for **The Frame Cafe** (Warsaw). FastAPI + SQLite + Jinja2.

## Features

- **Sales Dashboard** — daily sales from GoPOS with P&L, ABC analysis, food cost tracking
- **Expenses** — invoice parsing from Google Drive, auto-classification with AI, P&L per month
- **Inventory** — ingredients, deliveries, low-stock alerts, supplier management
- **Recipes** — tech cards with cost calculations, sub-recipes, auto-match resale items
- **Invoice AI** — Claude Haiku 4.5 (vision) parses PDF invoices, maps items to inventory
- **GoPOS Sync** — Playwright scraper syncs sales daily at 21:00 UTC
- **Telegram Bot** — daily reports, sync alerts, price change notifications

## Deployment

Runs on **AWS EC2** (us-west-2) in Docker container with nginx reverse proxy.

```bash
# Deploy (from local)
git push
ssh -i catchmyaction-key.pem ec2-user@52.39.186.224 "cd cafe-manager && git pull && docker compose up -d --build"
```

## Quick Start (local dev)

```bash
# Download prod DB first (source of truth)
scp -i catchmyaction-key.pem ec2-user@52.39.186.224:cafe-manager/data/cafe.db data/

# Run
pip install -r requirements.txt
playwright install chromium
uvicorn app.main:app --reload --port 8000
```

## Configuration

`.env` file (not in git):
```
GOPOS_EMAIL=...
GOPOS_PASSWORD=...
GOPOS_VENUE_ID=...
GOPOS_URL=https://app.gopos.io
SESSION_SECRET=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

Additional on prod:
- `data/google-credentials.json` — Google Drive service account
- `data/cafe.db` — SQLite database (source of truth on prod)

## Project Structure

```
cafe-manager/
├── app/
│   ├── main.py              # FastAPI app + scheduler
│   ├── db.py                # SQLite schema
│   ├── auth.py              # Cookie auth + roles
│   ├── gopos_sync.py        # GoPOS Playwright scraper
│   ├── gdrive_invoices.py   # Google Drive + Textract
│   ├── llm.py               # Claude Haiku 4.5 (vision + classification)
│   ├── telegram_bot.py      # Telegram notifications
│   ├── services/
│   │   ├── recipes.py       # Cost calculations
│   │   └── units.py         # Unit conversions (kg→g, L→ml)
│   ├── routes/              # FastAPI routers
│   │   ├── dashboard.py
│   │   ├── expenses.py
│   │   ├── inventory.py
│   │   ├── recipes.py
│   │   └── ...
│   ├── templates/           # Jinja2 HTML
│   └── static/              # CSS + JS
├── data/                    # SQLite DB (gitignored)
├── scripts/                 # One-off maintenance scripts
├── .kiro/steering/          # Project context for AI
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env                     # Secrets (gitignored)
```

## Key Integrations

| Service | Purpose | Cost |
|---------|---------|------|
| GoPOS | POS sales data | included |
| Google Drive | Invoice storage | free |
| AWS Bedrock (Claude Haiku 4.5) | Invoice parsing + classification | ~$1-2/month |
| AWS Textract | OCR fallback | pay-per-use |
| Telegram | Alerts & reports | free |

## Auth

- Cookie-based, HMAC-signed sessions
- Roles: `admin` (full access), `chef` (inventory+recipes), `barista` (inventory+recipes)
