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

from app.db import get_db, init_db

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
    # Wait for redirect after login (may land on / or /VENUE_ID/...)
    await page.wait_for_load_state('networkidle', timeout=20000)
    log.info(f'Logged in to GoPOS, current URL: {page.url}')


async def download_csv(page, date_from: str, date_to: str) -> str:
    """Navigate to reports and download CSV. Returns CSV text."""
    import base64
    from urllib.parse import quote

    # Build date filter param (GoPos custom base64 encoding)
    raw = (
        b'_\xc3\xa7date_range|bt='
        + quote(date_from + ' 06:00:00').encode()
        + b'\xc2\xa5'
        + quote(date_to + ' 05:00:00').encode()
    )
    f_param = base64.b64encode(raw).decode().rstrip('=')

    reports_url = (
        f'{GOPOS_URL}/{VENUE_ID}/reports/products'
        f'?s=all'
        f'&c=product_quantity,total_money,net_total_money,'
        f'discount_money,sub_total_money,net_profit_money,'
        f'net_production_money'
        f'&f={f_param}'
        f'&groups=PRODUCT'
        f'&chart_type=REPORT_DATE_DAY_OF_MONTH'
    )
    await page.goto(reports_url)
    await page.wait_for_load_state('networkidle')

    # Set date range via the date picker if available
    # The filter is encoded in the URL; we navigate with the right params
    # Wait for the table/data to load
    await page.wait_for_timeout(3000)

    # Click CSV export button (icon-only button inside .export-dropdown)
    export_btn = page.locator('.export-dropdown button.dropdown-toggle')
    await export_btn.click()
    await page.wait_for_timeout(1000)

    # Look for CSV option in the dropdown menu
    async with page.expect_download() as dl_info:
        csv_option = page.locator('text=/CSV/')
        if await csv_option.count() > 0:
            await csv_option.first.click()
        else:
            # Fallback: click first menu item
            menu_item = page.locator('.export-dropdown .dropdown-menu a, .export-dropdown .dropdown-menu button')
            await menu_item.first.click()

    download = await dl_info.value
    dest = Path(__file__).parent.parent / 'data' / download.suggested_filename
    await download.save_as(dest)
    log.info(f'Downloaded CSV: {dest}')
    return dest.read_text(encoding='utf-8-sig')


def import_csv_to_db(csv_text: str, date: str):
    """Parse GoPOS CSV and upsert into sales table."""
    db = get_db()
    reader = csv.DictReader(io.StringIO(csv_text), delimiter=',')

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
    """Deduct ingredients from inventory based on sales and recipes.
    Safe to call multiple times — reverses previous deduction for this date first.
    """
    db = get_db()

    # Reverse previous deduction for this date (if any)
    prev = db.execute(
        'SELECT ingredient_id, SUM(amount) as total '
        'FROM inventory_deductions WHERE date = ? GROUP BY ingredient_id',
        (date,)
    ).fetchall()
    for p in prev:
        db.execute(
            'UPDATE ingredients SET quantity = quantity + ? WHERE id = ?',
            (p['total'], p['ingredient_id'])
        )
    db.execute('DELETE FROM inventory_deductions WHERE date = ?', (date,))

    # Deduct based on current sales
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
            db.execute(
                'INSERT INTO inventory_deductions (date, ingredient_id, amount) VALUES (?, ?, ?)',
                (date, recipe['ingredient_id'], total)
            )

    db.commit()
    db.close()
    log.info(f'Inventory deducted for {date}')


def _start_sync_log(message=''):
    db = get_db()
    cur = db.execute(
        'INSERT INTO sync_log (started_at, status, message) VALUES (?, ?, ?)',
        (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'running', message)
    )
    sync_id = cur.lastrowid
    db.commit()
    db.close()
    return sync_id


def _finish_sync_log(sync_id, status='done', message=''):
    db = get_db()
    db.execute(
        'UPDATE sync_log SET finished_at = ?, status = ?, message = ? WHERE id = ?',
        (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), status, message, sync_id)
    )
    db.commit()
    db.close()


async def sync_date(date_str: str):
    """Sync a single date: login, download CSV, import, deduct inventory."""
    init_db()
    sync_id = _start_sync_log(f'Syncing {date_str}')
    date_from = date_str
    next_day = (datetime.strptime(date_str, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
    date_to = next_day

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                await login(page)
                csv_text = await download_csv(page, date_from, date_to)
                import_csv_to_db(csv_text, date_str)
                deduct_inventory(date_str)
                log.info(f'Sync complete for {date_str}')
            finally:
                await browser.close()
        _finish_sync_log(sync_id, 'done', f'Synced {date_str} successfully')
    except Exception as e:
        log.exception(f'Sync failed for {date_str}')
        _finish_sync_log(sync_id, 'error', str(e)[:200])


async def sync_today():
    """Sync today's data."""
    today = datetime.now().strftime('%Y-%m-%d')
    await sync_date(today)


async def sync_range(date_from: str, date_to: str):
    """Sync a range of dates, one day at a time."""
    init_db()
    sync_id = _start_sync_log(f'Syncing {date_from} → {date_to}')
    start = datetime.strptime(date_from, '%Y-%m-%d')
    end = datetime.strptime(date_to, '%Y-%m-%d')

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                await login(page)
                current = start
                while current <= end:
                    date_str = current.strftime('%Y-%m-%d')
                    next_day = (current + timedelta(days=1)).strftime('%Y-%m-%d')
                    csv_text = await download_csv(page, date_str, next_day)
                    import_csv_to_db(csv_text, date_str)
                    deduct_inventory(date_str)
                    log.info(f'Synced {date_str}')
                    current += timedelta(days=1)
            finally:
                await browser.close()
        _finish_sync_log(sync_id, 'done', f'Synced {date_from} → {date_to} successfully')
    except Exception as e:
        log.exception(f'Range sync failed')
        _finish_sync_log(sync_id, 'error', str(e)[:200])


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    asyncio.run(sync_today())
