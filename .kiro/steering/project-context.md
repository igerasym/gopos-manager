---
inclusion: auto
---

# The Frame Manager — Project Context

## What is this
Internal management app for **The Frame Cafe** (Warsaw, Jana Kazimierza 32). FastAPI + SQLite + Jinja2 templates. Deployed on AWS EC2 (us-west-2, 52.39.186.224).

## Owner
Anna Hulvanska (NIP: 9512616602). The user (Yaroslav) is the developer/co-owner. Communicates in Ukrainian, UI in Ukrainian, ingredient names in Polish (as on Makro invoices).

## Tech Stack
- Python 3.11, FastAPI, Uvicorn, Jinja2, SQLite (`data/cafe.db`)
- Playwright for GoPos scraping (headless Chromium)
- APScheduler for daily auto-sync at 21:00 UTC (23:00 CEST)
- Google Drive API + AWS Textract for invoice parsing
- Telegram bot for alerts
- Docker container, nginx reverse proxy
- No JS frameworks — vanilla JS only

## Deployment
- EC2 instance: us-west-2, 52.39.186.224 (shared with catchmyactions.com)
- SSH key: `~/git/cafe-manager/catchmyaction-key.pem`, user `ec2-user`
- Docker container on port 8000, nginx on port 80
- Deploy: `git push` then SSH: `cd cafe-manager && git pull && docker compose up -d --build`
- 4GB swap for Playwright
- Auto-sync at 21:00 UTC, Telegram alerts on failure
- **Never push DB to prod** — prod DB is source of truth
- Admin password on prod: `TheFrame2025!`

## GoPos Integration
- POS system: app.gopos.io
- `gopos_sync.py` logs in via Playwright, sets date filter via base64-encoded URL param, downloads CSV, imports to DB
- Date filter format: `_\xc3\xa7date_range|bt=DATE_FROM%2006%3A00%3A00\xc2\xa5DATE_TO%2005%3A00%3A00` (base64, raw bytes)
- GoPos "business day" = 06:00 → next day 05:00
- CSV delimiter is comma, columns: Product, Quantity sold, Value of sales, Net sales value, Value of discounts, Value without discounts, Profit, Cost
- `sync_range(date_from, date_to)` for bulk historical sync
- Auto-sync missing days on container start

## Database Schema
- **sales**: date, product_name, quantity, total_money, net_total, discount, net_profit. UNIQUE(date, product_name)
- **ingredients**: name, unit, quantity, min_quantity, unit_price (BRUTTO), supplier_id
- **recipes**: product_name, ingredient_id, amount. UNIQUE(product_name, ingredient_id)
- **recipe_cards**: product_name (PK), category, portion_weight, description
- **sub_recipes**: ingredient_id (UNIQUE), yield_amount, yield_unit, description
- **sub_recipe_items**: sub_recipe_id, ingredient_id, amount
- **deliveries**: date, ingredient_id, quantity, price, note, supplier_id
- **inventory_deductions**: date, ingredient_id, amount (idempotent)
- **expenses**: name, category, amount, month, recurring (legacy, always 0 now), note
- **parsed_invoices**: file_id (UNIQUE), file_name, invoice_number, folder, month, vendor, category, total, items_json, status (pending/approved/skipped), expense_id
- **ingredient_mappings**: invoice_name (UNIQUE), ingredient_id, action (match/skip/new)
- **invoice_items_pending**: parsed_invoice_id, invoice_name, quantity, unit_price, status, ingredient_id, suggested_ingredient, confidence
- **vendor_rules**: vendor_pattern (UNIQUE), category, expense_name
- **ingredient_price_history**: ingredient_id, price, date, invoice_id, note
- **expenses_dismissed**: name, month (legacy, can be removed)
- **suppliers**: name (UNIQUE), contact, note
- **users**: username (UNIQUE), password_hash, role, display_name
- **stock_counts** / **stock_count_items**: for inventory checks
- **sync_log**: started_at, finished_at, status, message

## Key Design Decisions
- All prices stored in **BRUTTO** (with VAT)
- Inventory units: kg/L for weight/volume, szt for pieces
- Recipe display: convert kg→g, L→ml for readability (app/services/units.py)
- Inventory deductions are idempotent — reversed before re-applying
- Recipe costs calculated dynamically from ingredient unit_price × recipe amount
- Auto-match resale: if ingredient name = product name in sales → COGS calculated without recipe
- Cost/price info visible only to admin role

## Expenses System (Simplified)
- **All expenses come from invoices** (Google Drive → Textract → approve) or manual entry
- No recurring/fixed/variable types anymore — just flat list per month
- P&L on expenses page: Revenue - All Expenses = Net Profit + Рентабельність %
- Categories: Оренда, Зарплати, Бухгалтерія, Комунальні, Побут, Податки і ZUS, Логістика, Продукти, Інше
- Inline edit amount (click on sum, Escape/click outside to cancel)
- Delete via form submit (works with auth cookies)

## Google Drive Invoice Integration
- Service account: `cafe-invoices@theframemanager.iam.gserviceaccount.com`
- Credentials: `data/google-credentials.json` (on prod, gitignored)
- Folder ID: `1f9UJ0-BskYgC_dppr7G8bLQrECg67fH6`
- Folder naming: "Фактури (травень 2026)" — matched by Ukrainian month name + year
- Recursive scan of subfolders
- `list_invoices_for_month(month)` — strict match: month name + year in path
- Multi-page PDF handling: convert to images via pdfplumber, parse each page with Textract
- Deduplication: by file_id (UNIQUE) + invoice_number check
- Status flow: pending → approved (added to expenses) / skipped

## Vendor Rules (Auto-classification)
- 20 default rules in code (Makro→Продукти, PGE→Комунальні, etc.)
- Table `vendor_rules` for custom rules
- `classify_vendor(vendor, file_name)` → returns (category, expense_name)
- Applied automatically during invoice parsing

## Price Alerts
- When ingredient price changes ≥10% on invoice approve → Telegram alert
- `ingredient_price_history` table tracks all price changes
- Format: "📈 Mleko 3.2%: 3.85 → 4.50 zł/L (+17%)"

## Invoice → Expenses Flow
1. PDF uploaded to Google Drive folder for current month
2. Daily sync (21:00) or manual "⚡ Обробити нові" button
3. Textract parses PDF → extracts vendor, items, total, invoice_number
4. Vendor auto-classified via rules
5. Saved as `parsed_invoices` with status=pending
6. UI shows pending invoices with [✓ Додати] + category selector + [✗ Skip]
7. On approve → creates expense record, links invoice
8. On approve → checks ingredient_mappings, updates prices, sends alerts
9. Telegram notification with summary

## LLM Setup (active)
- **Primary:** Claude Haiku 4.5 (`us.anthropic.claude-haiku-4-5-20251001-v1:0`) on Bedrock us-west-2
- **Fallback:** AWS Textract `analyze_expense` (used only if Claude fails)
- **Cost:** ~$1-2/month for 50-80 invoices
- **Future option:** Nova 2 Lite ($0.17/1M input vs Claude $1/1M, ~6x cheaper) — try later if cost matters
- **Vision-based parsing:** PDF → images via pdfplumber → Claude vision → JSON
- Single LLM call extracts: vendor, invoice_number, date, total, items[]
- Then second call for classification + ingredient mapping
- IAM role `CatchMyActions-EC2-Role` has `AmazonBedrockFullAccess` and `AmazonTextractFullAccess`
- Note: Claude 3 Haiku and 3.5 Haiku are marked Legacy by Anthropic for new accounts — use Haiku 4.5

## Telegram Bot
- Token and chat_id in `.env` on prod (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
- Daily report after sync: revenue, top 5, low stock
- Sync failure alerts
- Invoice processing notifications
- Price change alerts (≥10%)

## Pages & Features

### Dashboard (/) — admin only
- Date range filter with presets
- KPI cards: Revenue, COGS, Gross Profit, Food Cost %
- ABC analysis (A/B/C badges)
- Top 5 by revenue + quantity
- Daily revenue trend
- Previous period comparison
- Low stock alerts
- Sync button (only visible on dashboard)

### Expenses (/expenses) — admin only
- Month selector
- P&L summary: Revenue − Expenses = Profit (рентабельність %)
- Categories breakdown with percentages
- Manual add form (name, category, amount, note)
- Pending invoices section (approve/skip)
- All expenses list (sorted by amount desc, inline edit)
- Google Drive invoices section (parsed status, "⚡ Обробити нові")

### Inventory (/inventory)
- Add Ingredient / Add Delivery forms
- Current Stock table with inline editing
- Supplier filter, search
- Delete with confirmation

### Recipes (/recipes, /recipes/bar)
- /recipes = all except Бар category
- /recipes/bar = Бар category only
- Collapsible tech cards with ingredients, costs, selling price
- Inline add/edit ingredients
- Sub-recipes (напівфабрикати): Соус шакшука, Лосось солений, Паста з авокадо, Сирники тісто, Цибуля карамель, Sok pomarańczowy

### Stock Count (/inventory/count)
- Full inventory check with expected vs actual

## Auth & Roles
- Cookie-based auth with HMAC-signed sessions
- Roles: admin (full), chef (inventory+recipes), barista (inventory+recipes)
- Dashboard, Expenses, Users, Sync — admin only

## Current Data (as of May 2026)
- Sales: March-May 2026 (3,481 records, 156 products)
- Recipes: 53 products with tech cards (160 recipe lines)
- Ingredients: 118 items
- Sub-recipes: 6 (напівфабрикати)
- Expenses: March + April data
- Revenue: Mar=113.5k, Apr=108.9k zł
- Avg daily: ~3,630 zł
- Weekends +49% vs weekdays

## Business Insights
- Food cost ~37% (target: 28-35%)
- Rent 13% of revenue (target: <10%)
- Salaries ~24% (normal for cafe)
- 103 products without recipe (66%) — biggest gap in food cost tracking
- Batch Brew = most profitable drink (low cost, high volume)
- Plant milk surcharge too low (2 zł vs 4-5 zł cost)

## Seasonal Drinks (May 2026)
- Signature Coffee Purple Bloom Matcha (matcha + lavender syrup + lavender powder + milk)
- Signature Coffee Sunset Espresso Tonic (espresso + lavender syrup + OJ + tonic)
- Signature Coffee Cherry Ice Latte (espresso + cherry syrup + milk)

## What's Next (backlog)
- Try Nova 2 Lite to reduce LLM costs (~6x cheaper than Claude Haiku 4.5)
- Auto-delivery creation on invoice approve
- Waste tracking
- Stock forecast
- Order generator from low stock
- Print tech cards as PDF
- Shift checklists
