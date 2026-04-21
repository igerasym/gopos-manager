"""GoPOS CSV export via Playwright headless browser."""
import asyncio
import csv
import io
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

from db import get_db, init_db

load_dotenv(Path(__file__).parent.parent / '.env')
log = logging.getLogger(__name__)

GOPOS_URL = os.getenv('GOPOS_URL', 'https://app.gopos.io')
VENUE_ID = os.getenv('GOPOS_VENUE_ID', '9388')
EMAIL = os.getenv('GOPOS_EMAIL', '')
PASSWORD = os.getenv('GOPOS_PASSWORD', '')


async def login(page):
    """Log in to GoPOS."""
    await page.goto(f'{GOPOS_URL}/login')
    await page.fill('input[type="email"], input[name="email"]', EMAIL)
    await page.fill('input[type="password"], input[name="password"]', PASSWORD)
    await page.click('button[type="submit"]')
    await page.wait_for_url(f'**/{VENUE_ID}/**', timeout=15000)
    log.info('Logged in to GoPOS')


async def download_csv(page, date_from: str, date_to: str) -> str:
    """Navigate to reports and download CSV. Returns CSV text."""
    reports_url = (
        f'{GOPOS_URL}/{VENUE_ID}/reports/products'
        f'?s=all'
        f'&c=product_quantity,total_money,net_total_money,'
        f'discount_money,sub_total_money,net_profit_money,'
        f'net_production_money'
        f'&groups=PRODUCT'
        f'&chart_type=REPORT_DATE_DAY_OF_MONTH'
    )
    await page.goto(reports_url)
    await page.wait_for_load_state('networkidle')

    # Set date range via the date picker if available
    # The filter is encoded in the URL; we navigate with the right params
    # Wait for the table/data to load
    await page.wait_for_timeout(3000)

    # Click CSV export button
    async with page.expect_download() as dl_info:
        # Look for export button - GoPOS uses Polish UI
        export_btn = page.locator(
            'button:has-text("Eksportuj"), '
            'button:has-text("Export"), '
            'a:has-text("CSV")'
        )
        await export_btn.first.click()

        # If there's a submenu "all columns" option, click it
        all_cols = page.locator(
            'text="Eksportuj CSV z wszystkimi kolumnami", '
            'text="Export CSV with all columns"'
        )
        if await all_cols.count() > 0:
            await all_cols.first.click()

    download = await dl_info.value
    dest = Path(__file__).parent.parent / 'data' / download.suggested_filename
    await download.save_as(dest)
    log.info(f'Downloaded CSV: {dest}')
    return dest.read_text(encoding='utf-8-sig')


def import_csv_to_db(csv_text: str, date: str):
    """Parse GoPOS CSV and upsert into sales table."""
    db = get_db()
    reader = csv.DictReader(io.StringIO(csv_text), delimiter=';')

    for row in reader:
        # GoPOS CSV columns (Polish): Produkt, Ilość, Suma, Netto, Rabat, ...
        # Map flexibly by position if headers vary
        values = list(row.values())
        if len(values) < 4:
            continue

        product = values[0].strip()
        if not product or product.lower() in ('suma', 'total', ''):
            continue

        def parse_num(v):
            try:
                return float(v.replace(',', '.').replace(' ', ''))
            except (ValueError, AttributeError):
                return 0.0

        db.execute('''
            INSERT INTO sales (date, product_name, quantity, total_money,
                             net_total, discount, net_profit)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, product_name) DO UPDATE SET
                quantity=excluded.quantity,
                total_money=excluded.total_money,
                net_total=excluded.net_total,
                discount=excluded.discount,
                net_profit=excluded.net_profit
        ''', (
            date,
            product,
            parse_num(values[1]) if len(values) > 1 else 0,
            parse_num(values[2]) if len(values) > 2 else 0,
            parse_num(values[3]) if len(values) > 3 else 0,
            parse_num(values[4]) if len(values) > 4 else 0,
            parse_num(values[5]) if len(values) > 5 else 0,
        ))

    db.commit()
    db.close()
    log.info(f'Imported sales for {date}')


def deduct_inventory(date: str):
    """Deduct ingredients from inventory based on sales and recipes."""
    db = get_db()
    rows = db.execute(
        'SELECT product_name, quantity FROM sales WHERE date = ?', (date,)
    ).fetchall()

    for row in rows:
        recipes = db.execute('''
            SELECT r.ingredient_id, r.amount, i.name
            FROM recipes r JOIN ingredients i ON r.ingredient_id = i.id
            WHERE r.product_name = ?
        ''', (row['product_name'],)).fetchall()

        for recipe in recipes:
            total = recipe['amount'] * row['quantity']
            db.execute(
                'UPDATE ingredients SET quantity = MAX(0, quantity - ?) WHERE id = ?',
                (total, recipe['ingredient_id'])
            )

    db.commit()
    db.close()
    log.info(f'Inventory deducted for {date}')


async def sync_today():
    """Full sync: login, download CSV, import, deduct inventory."""
    init_db()
    today = datetime.now().strftime('%Y-%m-%d')

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await login(page)
            csv_text = await download_csv(page, today, today)
            import_csv_to_db(csv_text, today)
            deduct_inventory(today)
            log.info(f'Sync complete for {today}')
        finally:
            await browser.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    asyncio.run(sync_today())
