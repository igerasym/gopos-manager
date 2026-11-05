"""Telegram bot for The Frame Manager — daily reports & commands."""
import logging
import os
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.parse import quote
import json
import sqlite3
from html import escape

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / '.env')
log = logging.getLogger(__name__)

TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
# One or more recipients, comma-separated: TELEGRAM_CHAT_ID=111111,222222
CHAT_IDS = [c.strip() for c in os.getenv('TELEGRAM_CHAT_ID', '').split(',') if c.strip()]
API = f'https://api.telegram.org/bot{TOKEN}'


def _send_one(text: str, cid: str):
    try:
        data = json.dumps({
            'chat_id': cid,
            'text': text,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True,
        }).encode()
        req = Request(f'{API}/sendMessage', data=data, headers={'Content-Type': 'application/json'})
        urlopen(req, timeout=10)
    except Exception as e:
        # One failing recipient (e.g. blocked the bot) must not stop the others
        log.error(f'Telegram send to {cid} failed: {e}')


def send_message(text: str, chat_id: str = ''):
    """Send a message to chat_id, or to every TELEGRAM_CHAT_ID recipient if not given."""
    if not TOKEN:
        log.warning('Telegram bot token not set')
        return
    recipients = [chat_id] if chat_id else CHAT_IDS
    if not recipients:
        log.warning('Telegram chat ID not set')
        return
    for cid in recipients:
        _send_one(text, cid)


TOP_N = 7
# GoPOS discount names (case-insensitive) that mark staff drinks / spoiled items
STAFF_DISCOUNTS = {'staff'}
WRITEOFF_DISCOUNTS = {'write-off'}
DRINK_CATEGORY = 'Coffee'
# Add-ons in the Coffee category (milk swaps, cups) cost less than this and aren't drinks
MIN_DRINK_PRICE = 5


def _merge(discounts: dict, names: set) -> dict:
    """Combine item stats of several discount names into one {item: {'qty','value',...}}."""
    merged = {}
    for n in names:
        for item, e in discounts.get(n, {}).items():
            m = merged.setdefault(item, {'qty': 0, 'value': 0.0, 'unit_price': e['unit_price']})
            m['qty'] += e['qty']
            m['value'] += e['value']
    return merged


def _is_drink(name: str, e: dict, categories: dict) -> bool:
    return categories.get(name) == DRINK_CATEGORY and e['unit_price'] >= MIN_DRINK_PRICE


def _item_list(items: dict) -> str:
    """'Cappuccino ×2, Brownie' — biggest first."""
    parts = []
    for name, e in sorted(items.items(), key=lambda kv: -kv[1]['qty']):
        q = int(e['qty']) if float(e['qty']).is_integer() else e['qty']
        parts.append(escape(name) + (f' ×{q}' if q != 1 else ''))
    return ', '.join(parts)


def daily_report():
    """Generate and send daily sales report."""
    from app.db import get_db

    today = datetime.now().strftime('%Y-%m-%d')
    db = get_db()

    totals = db.execute('''
        SELECT COALESCE(SUM(total_money), 0) as revenue,
               COALESCE(SUM(quantity), 0) as items,
               COUNT(DISTINCT product_name) as products
        FROM sales WHERE date = ?
    ''', (today,)).fetchone()

    top = db.execute('''
        SELECT product_name, SUM(quantity) as qty, SUM(total_money) as rev
        FROM sales WHERE date = ?
        GROUP BY product_name ORDER BY rev DESC LIMIT ?
    ''', (today, TOP_N)).fetchall()

    try:  # pos_products is created by the first GoPOS product sync, not init_db
        categories = {r['product_name']: r['category']
                      for r in db.execute('SELECT product_name, category FROM pos_products')}
    except sqlite3.OperationalError:
        categories = {}

    # Low stock
    low = db.execute('''
        SELECT name, quantity, unit
        FROM ingredients WHERE quantity <= min_quantity AND min_quantity > 0
    ''').fetchall()

    db.close()

    # Build message
    lines = [
        f'☕ <b>The Frame — {today}</b>',
        '',
        f'💰 Revenue: <b>{totals["revenue"]:,.0f} zł</b>',
        f'📦 Items sold: {int(totals["items"])}',
        f'🍽 Products: {totals["products"]}',
    ]

    # Shift + discounts — live from GoPOS; the report must still go out if this fails
    staff, discounts = None, {}
    try:
        from app import gopos_api
        orders = gopos_api.fetch_closed_orders(today, 'employee,promotions,items,items.product')
        staff = gopos_api.shift_staff(orders)
        discounts = gopos_api.discounted_items(orders)
    except Exception as e:
        log.warning(f'GoPOS shift/discount lookup failed: {e}')

    if staff:
        lines.append('')
        lines.append('👤 <b>Shift:</b>')
        for s in staff:
            lines.append(f'  {escape(s["name"])} — {s["first"]}–{s["last"]}, {s["orders"]} orders')

    staff_items = _merge(discounts, STAFF_DISCOUNTS)
    if staff_items:
        drinks = {n: e for n, e in staff_items.items() if _is_drink(n, e, categories)}
        n_drinks = int(sum(e['qty'] for e in drinks.values()))
        value = sum(e['value'] for e in staff_items.values())
        lines.append('')
        lines.append(f'☕ <b>Staff drinks:</b> {n_drinks} — {value:,.0f} zł')
        if drinks:
            lines.append(f'  {_item_list(drinks)}')
        other = {n: e for n, e in staff_items.items()
                 if n not in drinks and categories.get(n) != DRINK_CATEGORY}
        if other:
            lines.append(f'  + {_item_list(other)}')

    writeoff = _merge(discounts, WRITEOFF_DISCOUNTS)
    if writeoff:
        qty = int(sum(e['qty'] for e in writeoff.values()))
        value = sum(e['value'] for e in writeoff.values())
        lines.append('')
        lines.append(f'🗑 <b>Write-off:</b> {qty} pcs — {value:,.0f} zł')
        lines.append(f'  {_item_list(writeoff)}')

    if top:
        lines.append('')
        lines.append(f'🏆 <b>Top {TOP_N}:</b>')
        for i, t in enumerate(top, 1):
            lines.append(f'  {i}. {t["product_name"]} — {int(t["qty"])} pcs ({t["rev"]:,.0f} zł)')

    send_message('\n'.join(lines))
    log.info('Daily report sent to Telegram')


def handle_command(text: str, chat_id: str):
    """Handle incoming bot commands."""
    from app.db import get_db

    cmd = text.strip().lower()

    if cmd in ('/today', '/report'):
        daily_report()
        return

    if cmd in ('/stock', '/inventory'):
        db = get_db()
        low = db.execute('''
            SELECT name, quantity, unit, min_quantity
            FROM ingredients WHERE quantity <= min_quantity AND min_quantity > 0
            ORDER BY name
        ''').fetchall()
        db.close()

        if low:
            lines = ['⚠️ <b>Low stock items:</b>', '']
            for l in low:
                lines.append(f'• {l["name"]} — {l["quantity"]:.1f} {l["unit"]} (min: {l["min_quantity"]:.0f})')
        else:
            lines = ['✅ All stock levels OK']

        send_message('\n'.join(lines), chat_id)
        return

    if cmd == '/help':
        send_message(
            '☕ <b>The Frame Bot</b>\n\n'
            '/today — daily sales report\n'
            '/stock — low stock alerts\n'
            '/help — this message',
            chat_id
        )
        return

    send_message('Unknown command. Try /help', chat_id)
