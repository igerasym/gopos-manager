---
inclusion: auto
---

# The Frame Manager — Project Context

## What is this
Internal management app for **The Frame Cafe** (Warsaw, Jana Kazimierza 32). FastAPI + SQLite + Jinja2 templates. Runs locally and will be deployed on Raspberry Pi.

## Owner
Anna Hulvanska (NIP: 9512616602). The user (Yaroslav) is the developer/co-owner. Communicates in Ukrainian, UI is in English, ingredient names in Polish (as on Makro invoices).

## Tech Stack
- Python 3.14, FastAPI, Uvicorn, Jinja2, SQLite (`data/cafe.db`)
- Playwright for GoPos scraping (headless Chromium)
- APScheduler for daily auto-sync at 23:00
- No JS frameworks — vanilla JS only, CSS-only charts

## GoPos Integration
- POS system: app.gopos.io
- `gopos_sync.py` logs in via Playwright, sets date filter via base64-encoded URL param, downloads CSV, imports to DB
- Date filter format: `_\xc3\xa7date_range|bt=DATE_FROM%2006%3A00%3A00\xc2\xa5DATE_TO%2005%3A00%3A00` (base64, raw bytes)
- GoPos "business day" = 06:00 → next day 05:00
- CSV delimiter is comma, columns: Product, Quantity sold, Value of sales, Net sales value, Value of discounts, Value without discounts, Profit, Cost
- Export button: `.export-dropdown button.dropdown-toggle` → click → `text=/^Export CSV$/`

## Database Schema
- **sales**: date, product_name, quantity, total_money, net_total, discount, net_profit. UNIQUE(date, product_name)
- **ingredients**: name, unit, quantity, min_quantity, unit_price (BRUTTO), supplier_id
- **recipes**: product_name, ingredient_id, amount. UNIQUE(product_name, ingredient_id)
- **recipe_cards**: product_name (PK), category, portion_weight, description
- **deliveries**: date, ingredient_id, quantity, price, note, supplier_id
- **inventory_deductions**: date, ingredient_id, amount (for idempotent sync)
- **suppliers**: name (UNIQUE), contact, note
- **sync_log**: started_at, finished_at, status (running/done/error), message

## Key Design Decisions
- All ingredient prices are stored in **BRUTTO** (with VAT). Food = 5%, some items 8% or 23%
- Inventory deductions are idempotent — reversed before re-applying on each sync
- Ingredient names are short and clean (e.g., "Jaja L", "Masło 200g", "Łosoś filet")
- Recipe costs calculated dynamically from ingredient unit_price × recipe amount
- Starlette 1.0+ TemplateResponse API: `templates.TemplateResponse(request, 'name.html', context={...})`

## Pages & Features

### Dashboard (/)
- Date range filter with presets: Today, Yesterday, Last 7/30 days, This week/month/year
- KPI cards: Revenue, COGS, Gross Profit, Food Cost %
- P&L summary table: Revenue → Discounts → Net → COGS → Gross Profit
- ABC analysis: A (80% revenue), B (next 15%), C (last 5%)
- Top 5 by revenue + Top 5 by quantity (CSS bar charts)
- Daily revenue trend (CSS bar chart, multi-day only)
- All Products table: ABC badge, Qty, Revenue, Unit Cost, COGS, Profit, Food %, sortable
- Previous period comparison (% change)
- Low stock alerts

### Inventory (/inventory)
- Add Ingredient / Add Delivery forms
- Current Stock table with inline editing: name, quantity, unit, price/unit, min alert, supplier
- Supplier filter buttons (All / Makro / Bakers House / Inne)
- Search by ingredient name
- Delete ingredient with confirmation
- Supplier dropdown auto-submits on change

### Recipes (/recipes)
- Tech cards grouped by category (Breakfast, Side dishes, etc.)
- Collapsible cards — click to expand ingredients
- Ingredient table with: amount (editable), unit (editable), price/unit, cost per line, total cost
- Add ingredient inline per card
- New recipe card form with product name (datalist from GoPos), category, portion size, preparation text

### Invoice Upload (/inventory/upload) — admin only
- Drag & drop PDF upload
- AI parsing via AWS Bedrock Claude 3.5 Haiku
- Preview page: edit ingredient names, quantities, prices, skip items
- Confirm → creates deliveries + updates stock + creates new ingredients
- Supplier selection on confirm

### Telegram Bot
- Daily report after 23:00 sync: revenue, top 5, low stock
- Commands: /today, /stock, /help
- Polling-based (no webhook needed)

### Stock Count (/inventory/count)
- New count form: all ingredients with system qty, enter actual
- Editable detail view: change quantities, add missing items
- History with date, time, user, shortage count
- KPIs: items counted, shortages, waste value (zł)

## Auth & Roles
- Cookie-based auth with signed sessions (HMAC)
- Roles: admin (full access), chef (inventory + recipes), barista (inventory + recipes)
- Dashboard, Users, Sync, Upload Invoice — admin only
- Default user: admin/admin
- Users page: add/delete users, change role/password

## Suppliers
- Makro Cash and Carry (main supplier, weekly deliveries)
- Bakers House (bread, bagels)
- Inne (ad-hoc purchases)

## Current Recipes
- Shakshuka, French omlet, Salmon bagel, Avocado toast (Breakfast)
- Side dishes: Becon, Salmon, Avocado, Egg, Asparagus, Bread, Bread and butter

## What's Next (backlog)
- Auto-parse PDF invoices with LLM (Bedrock quota pending)
- Waste tracking with reasons (spoiled, staff meal, error)
- Stock forecast — predict needs based on avg sales
- Order generator — auto-create supplier order from low stock + forecast
- Print tech cards as PDF for kitchen
- Shift checklists (open/close)
- Quick waste log (broken bottle, spoiled item)
- Shift notes between staff
