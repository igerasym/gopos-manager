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
- **Monthly report** — 1st of the month 06:30 UTC: last month vs the month before (calendar-adjusted
  like-for-like, weekdays, hours, categories, movers, discounts, staff) + AI summary (Claude Sonnet 4.5,
  `MONTHLY_REPORT_MODEL_ID`). Manual: `docker exec cafe-manager-cafe-1 python3 scripts/monthly_report.py 2026-10 --admin`

## Deployment

Runs on **AWS EC2** (us-west-2) in Docker container with nginx reverse proxy.

**Auto-deploy:** merge to `main` → within ~5 min the host deploys it. `deploy/auto-deploy.sh`
(installed as `~/bin/cafe-auto-deploy.sh`, systemd user timer `cafe-autodeploy.timer`) builds the
image in `~/cafe-build`, swaps only the `cafe` container, health-checks `:8000`, auto-rolls back on
failure and sends a Telegram alert to the first `TELEGRAM_CHAT_ID`. It shares `~/.deploy.lock` with
the CatchMyAction deploy so two builds never run at once on the 2 GB host.

```bash
tail ~/cafe-deploy.log                          # history
systemctl --user stop cafe-autodeploy.timer     # pause
systemctl --user start cafe-autodeploy.timer    # resume
rm ~/.cafe-deploy-failed-sha                    # retry a commit that failed
docker tag cafe-manager-cafe:rollback-<sha> cafe-manager-cafe:latest && docker compose up -d --no-build cafe  # manual rollback
```

Install / update the deployer on the host (after `git pull` in `~/cafe-manager`):
```bash
install -m 755 deploy/auto-deploy.sh ~/bin/cafe-auto-deploy.sh
cp deploy/cafe-autodeploy.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now cafe-autodeploy.timer
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
TELEGRAM_CHAT_ID=...        # one or more, comma-separated: 111111,222222
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
