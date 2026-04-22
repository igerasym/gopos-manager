"""Telegram bot for The Frame Manager — daily reports & commands."""
import logging
import os
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.parse import quote
import json

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / '.env')
log = logging.getLogger(__name__)

TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
API = f'https://api.telegram.org/bot{TOKEN}'


def send_message(text: str, chat_id: str = ''):
    """Send a message via Telegram bot."""
    if not TOKEN:
        log.warning('Telegram bot token not set')
        return
    cid = chat_id or CHAT_ID
    if not cid:
        log.warning('Telegram chat ID not set')
        return
    try:
        url = f'{API}/sendMessage'
        data = json.dumps({
            'chat_id': cid,
            'text': text,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True,
        }).encode()
        req = Request(url, data=data, headers={'Content-Type': 'application/json'})
        urlopen(req, timeout=10)
    except Exception as e:
        log.error(f'Telegram send failed: {e}')


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
        GROUP BY product_name ORDER BY rev DESC LIMIT 5
    ''', (today,)).fetchall()

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

    if top:
        lines.append('')
        lines.append('🏆 <b>Top 5:</b>')
        for i, t in enumerate(top, 1):
            lines.append(f'  {i}. {t["product_name"]} — {int(t["qty"])} pcs ({t["rev"]:,.0f} zł)')

    if low:
        lines.append('')
        lines.append('⚠️ <b>Low stock:</b>')
        for l in low:
            lines.append(f'  • {l["name"]} ({l["quantity"]:.0f} {l["unit"]})')

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
