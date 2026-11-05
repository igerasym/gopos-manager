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
- GoPos REST API for sales sync (replaced Playwright in May 2026)
- APScheduler (`app/main.py`): daily job at **19:00 UTC** (sync → daily report → invoices), **04:30 UTC** full
  re-sync of yesterday (evening orders + their stock deduction), monthly report on the 1st at 06:30 UTC
- Google Drive API + AWS Bedrock Claude for invoice parsing
- Telegram bot for alerts
- Docker container, nginx reverse proxy
- No JS frameworks — vanilla JS only
- Any server/LLM/invoice text inserted via `innerHTML` must go through `esc()` (`app/static/app.js`):
  invoice item names come from vendor PDFs and are untrusted. Bump `app.js?v=` in `base.html` when it changes.

## Deployment
- EC2 instance: us-west-2, 52.39.186.224, AWS account 923917779583 — **shared with catchmyactions.com**
  (containers `app-app-1`, `app-postgres-1`). Never touch those; cafe is `cafe-manager-cafe-1` only.
- SSH as `ec2-user`. Agent key: `~/.ssh/kiro-cma-deploy` on Yaroslav's Mac (use `-o IdentitiesOnly=yes`).
  Port 22 is open only to the owner's IP.
- Repo on host: `~/cafe-manager` (live checkout, untracked `.env` + `data/` mounted into the container).
  Docker container published on **127.0.0.1:8000** only; host nginx (`/etc/nginx/conf.d/cafe.conf`) proxies port 80 to it.
  `SESSION_SECRET` must be set in `.env`: compose and app startup refuse an unset/default value.
- **Auto-deploy**: merge to `main` → within ~5 min live. `deploy/auto-deploy.sh` (installed as
  `~/bin/cafe-auto-deploy.sh`, systemd user timer `cafe-autodeploy.timer`) builds in `~/cafe-build`,
  swaps only `cafe`, health-checks `:8000` (302 = up), auto-rollback, Telegram alert to admin.
  Log `~/cafe-deploy.log`. Shares `~/.deploy.lock` with the CatchMyAction deployer (2 GB RAM — never two builds).
  Do NOT deploy by hand with `docker compose up --build` after a merge — let the timer do it.
- Disk is tight (~85% of 20 GB): keep only 2 rollback images, delete backups you no longer need.
- Docker image: ~380MB (python:3.11-slim + deps, no Chromium). Image contains `app/` and `scripts/` only.
- Run code in prod context: `docker compose exec -T cafe python -c "..."` (cwd `/app`, `.env` loaded).
- **Never push DB to prod** — prod DB is source of truth. Back up before bulk writes:
  sqlite `backup()` into `/app/data/backups/` (host `~/cafe-manager/data/backups/`).
  DB runs in **WAL mode** (since 2026-11-06): never back up by copying `cafe.db` alone — use `backup()`,
  or copy it together with `cafe.db-wal`/`-shm`.
- Admin password on prod: stored in `.env` (not in git)

## GoPos Integration
- POS system: app.gopos.io
- **API-based sync** (replaced Playwright scraping in May 2026)
- OAuth2: grant_type=organization, client_id + client_secret + organization_id=9388
- Credentials in env: GOPOS_CLIENT_ID, GOPOS_CLIENT_SECRET, GOPOS_ORG_ID
- `gopos_api.py` — token management, products sync, orders sync, inventory deduction
- `GET /api/v3/{org}/orders?include=items,items.product` — daily sales with product names
- `GET /api/v3/{org}/items` — product catalog with categories, prices, item_group_id
- `GET /api/v3/{org}/categories` — GoPos categories (Coffee, Kitchen, Bakery, Bar, Beans, Ice Cream, Soft Drinks, Brewware)
- Variant groups via `item_group_id` — "Cappuccino Cappuccino" + "Cappuccino Iced" = group 3
- Business day = 05:00 → next day 05:00 Warsaw (`BUSINESS_DAY_START_HOUR`, `business_date()`, `warsaw_today()`);
  the container runs in UTC, so never use bare `datetime.now()` for a business date.
- Daily job 19:00 UTC: sync_products() + sync_orders_for_date(today) + deduct_inventory()
- 04:30 UTC: `sync_date(yesterday)` — the 19:00 sync misses everything sold later that evening.
- `sync_orders_for_date` **replaces** the day (DELETE + INSERT in one transaction) and rewrites
  `processed_orders` for it. An empty API answer for a day that already has rows is treated as an API
  hiccup and the day is kept.
- Real-time: GoPos webhook `POST /api/webhook/gopos?token=<GOPOS_WEBHOOK_TOKEN>` adds each closed order
  to `sales` once: `processed_orders(order_id)` is written in the same transaction, so retries/replays
  are ignored. `resource_id` must be digits. ITEM/CATEGORY events trigger at most one catalog sync per 5 min.
  If `GOPOS_WEBHOOK_TOKEN` is unset the webhook is accepted without auth (logged warning) — set it and
  re-register the GoPOS webhook URL with `?token=`.
- Missing days are NOT auto-detected on start (only products are synced on startup).
- No Playwright, no Chromium, no swap needed

### GoPos API gotchas (verified 2026-11)
- **`sub_total_price` = price BEFORE discount, `total_price` = AFTER** (counter-intuitive). Staff drink: sub 22, total 0.
- Pages are **0-based** (`page=0` is the first page; `api_get_all` used to start at 1 and dropped 20 catalog items
  until 2026-11-06). Max **page < 50** per query (page 50 → 422). Fetch per business day — `fetch_closed_orders(date, include)`.
- Order item names need `include=items,items.product`; items carry `item_id` → catalog `items.tax_id` →
  `GET taxes` (23/8/5/0%). ~30 items/day have an item_id NOT in the catalog (variants) → `order_item_amounts()`
  gives them the order's residual net so per-order net matches `total_price_net` exactly.
- Useful includes: `employee` (who was logged in on the POS), `promotions` (discount name + `used` amount),
  `tax_items` (exact net/tax/gross per VAT rate per receipt).
- Discount (promotion) names: `Staff` (staff drinks), `write-off` (spoiled goods), `Owner`, `Friends/Staff`,
  `50% Cookie Countdown` (end-of-day half price), `Apology treat`, `15`.
- Employees: Alina, Alya, Nastia + shared account **`Barista`** (can't attribute those orders to a person).
- `work_times` (clock-in) exists but is barely used (last entry Aug 2026) — don't rely on it for shifts.
- No to-go flag: every order is `type=DINE_IN`.
- `sync_date()` / `sync_range()` also DEDUCT INVENTORY. To re-import money fields only, use
  **`resync_sales(from, to)`** (no inventory side effects). History was resynced 2026-05-25 → 2026-11-05.

### Stock deduction
- `deduct_inventory(date)` reverses that date's previous deduction, then deducts again, in one
  `BEGIN IMMEDIATE` transaction under a process lock. `inventory_deductions.amount` is what was ACTUALLY
  taken off (stock is clamped at 0), so re-runs don't inflate stock. Known gap (not fixed yet): a stock
  count mid-day is followed by that whole day's deduction, so counted stock ends up too low.

## Database Schema
- **sales**: date, product_name, quantity, total_money, net_total, discount, net_profit. UNIQUE(date, product_name).
  Meaning (same as the old GoPos export, fixed for API data in Nov 2026):
  `total_money` = paid gross (incl. VAT, after discount) — this is "Revenue";
  `net_total` = paid net (excl. VAT); `discount` = discount amount gross;
  `net_profit` = gross price BEFORE discount (legacy name — it is NOT profit).
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
- **Monthly report summary:** Claude Sonnet 4.5 (`us.anthropic.claude-sonnet-4-5-20250929-v1:0`,
  env `MONTHLY_REPORT_MODEL_ID`), ~5k tokens/month ≈ $0.03. Sonnet 4 is not enabled in this account.
- ⚠️ **The first call to any not-yet-used Bedrock model auto-creates an AWS Marketplace agreement**
  (Anthropic) on account 923917779583. Ask the owner BEFORE calling a new model, even for a quick test.

## Telegram Bot (`app/telegram_bot.py`)
- Token and chat ids in `.env` on prod: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` = comma-separated
  (Yaroslav first, his partner 380004220 second). Each recipient gets it in their own DM.
- **Admin vs functional**: `ADMIN_CHAT_ID` = `TELEGRAM_ADMIN_CHAT_ID` or the first id. Partner gets only
  functional messages; admin also gets technical ones. Use `send_message()` for functional, `send_admin()` /
  `alert_admin(where, exc)` for technical. Never `except: pass` in jobs — call `alert_admin`.
- Daily report: revenue, items, 👤 Shift (employee per order), ☕ Staff drinks (Coffee items ≥5 zł under
  `Staff` discount), 🗑 Write-off, 🏆 Top 7. Blocks appear only when non-empty. Admin gets a 🔧 debug
  footer ONLY when something is wrong (GoPos failed, DB revenue ≠ GoPos paid, last sync not `done`) —
  the owner explicitly doesn't want healthy debug noise.
- Monthly report (`app/monthly_report.py`): last month vs month before — calendar-adjusted like-for-like
  (prev weekday averages × current calendar), weekdays + weekend trend, hours, categories, movers, new/gone,
  discounts by type, POS accounts, VAT by rate (from `tax_items`), expenses/P&L if invoices exist; plus an
  AI summary written ONLY from precomputed numbers (model must not compute its own %).
  Manual: `docker compose exec -T cafe python3 scripts/monthly_report.py 2026-10 --admin|--print`.
- Invoice summaries (functional) / parse errors (admin), price change alerts ≥10% (functional),
  deploy alerts (admin).
- The bot does NOT read incoming messages yet (`handle_command` is unwired) — interactive chat is planned:
  long polling (no public webhook), allowlist = TELEGRAM_CHAT_ID, read-only tools, Haiku, no message text in logs.

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
- Roles: admin (full), chef (inventory+recipes), barista (inventory+recipes). Any other role (e.g. the
  legacy DB default `staff`) gets NO access — `can_access` used to treat unknown roles as admin.
  `/users` only accepts roles from `ROLE_ACCESS`.
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

## Sep vs Oct 2026 snapshot (for context)
- Revenue Sep 130.2k → Oct 137.1k zł (+5.3%) but calendar-adjusted ≈ 0%; avg ticket flat ~37.5 zł.
- Lunch 10–15 up, mornings (<10) −10%, Mondays −14%, weekends weakening in the 2nd half of October.
- Autumn signatures (Pumpkin Spice Latte, Mulled Filter, Napar) ~9k; iced summer drinks collapsed.
- VAT on sales ~20k/month, ~85% of it at 23% (worth checking coffee VAT rates with the accountant).
- October invoices not uploaded to Drive yet → no October P&L.

## What's Next (backlog)
- Interactive Telegram chat with read-only tools (planned next)
- Staff hygiene (not code): personal POS logins instead of `Barista`; record every write-off via `write-off`
- VAT payable = sales VAT − invoice VAT (needs VAT split from invoices)
- Try Nova 2 Lite to reduce LLM costs (~6x cheaper than Claude Haiku 4.5)
- Auto-delivery creation on invoice approve
- Waste tracking
- Stock forecast
- Order generator from low stock
- Print tech cards as PDF
- Shift checklists
