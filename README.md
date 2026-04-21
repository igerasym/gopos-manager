# ☕ Cafe Manager

Sales analytics from GoPOS + inventory management. Runs on Raspberry Pi 5.

## Features

- **Sales Dashboard** — daily sales imported from GoPOS CSV export
- **Inventory** — track ingredients, deliveries, low-stock alerts
- **Recipes** — map menu products to ingredients, auto-deduct on sale
- **GoPOS Sync** — Playwright scraper logs in and downloads reports

## Quick Start

### 1. Configure

Edit `.env` with your GoPOS credentials:

```
GOPOS_EMAIL=your-email@example.com
GOPOS_PASSWORD=your-password
GOPOS_VENUE_ID=9388
```

### 2. Run with Docker (recommended for Pi)

```bash
docker compose up -d --build
```

Open `http://<pi-ip>:8000` in your browser.

### 3. Run locally (development)

```bash
pip install -r requirements.txt
playwright install chromium
cd app && python main.py
```

## Usage

1. Click **Sync GoPOS** to import today's sales
2. Go to **Inventory** → add your ingredients (coffee, milk, etc.) with units and min-alert levels
3. Go to **Recipes** → map each GoPOS product to ingredients (e.g., Latte = 18g coffee + 200ml milk)
4. After sync, inventory auto-deducts based on recipes
5. Dashboard shows low-stock warnings

## Auto-sync (cron)

Add to Pi's crontab to sync daily at 23:00:

```bash
crontab -e
# add:
0 23 * * * cd /home/pi/git/cafe-manager && docker compose exec cafe python -m app.gopos_sync
```

## Project Structure

```
cafe-manager/
├── app/
│   ├── main.py          # FastAPI server
│   ├── db.py            # SQLite models
│   ├── gopos_sync.py    # GoPOS Playwright scraper
│   └── templates/       # HTML pages
├── data/                # SQLite DB + CSV downloads
├── Dockerfile
├── docker-compose.yml
└── .env                 # credentials (not in git)
```
