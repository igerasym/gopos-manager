"""GoPos API client — replaces Playwright scraping."""
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

import requests
from dotenv import load_dotenv
from pathlib import Path

from app.db import get_db, init_db

load_dotenv(Path(__file__).parent.parent / '.env')
log = logging.getLogger(__name__)

# API credentials (from .env)
GOPOS_CLIENT_ID = os.getenv('GOPOS_CLIENT_ID', '')
GOPOS_CLIENT_SECRET = os.getenv('GOPOS_CLIENT_SECRET', '')
GOPOS_ORG_ID = os.getenv('GOPOS_ORG_ID', '')
GOPOS_BASE_URL = 'https://app.gopos.io'

_token: Optional[str] = None
_token_expires: Optional[datetime] = None


def get_token() -> str:
    """Get OAuth2 token (cached until expiry)."""
    global _token, _token_expires

    if _token and _token_expires and datetime.now() < _token_expires:
        return _token

    r = requests.post(f'{GOPOS_BASE_URL}/oauth/token', data={
        'grant_type': 'organization',
        'client_id': GOPOS_CLIENT_ID,
        'client_secret': GOPOS_CLIENT_SECRET,
        'organization_id': GOPOS_ORG_ID,
    }, timeout=15)
    r.raise_for_status()
    data = r.json()
    _token = data['access_token']
    # Token typically expires in 1 hour, refresh 5 min early
    expires_in = data.get('expires_in', 3600)
    _token_expires = datetime.now() + timedelta(seconds=expires_in - 300)
    log.info('GoPos API token obtained')
    return _token


def api_get(path: str, params: dict = None) -> dict:
    """Make authenticated GET request to GoPos API."""
    token = get_token()
    url = f'{GOPOS_BASE_URL}/api/v3/{GOPOS_ORG_ID}/{path}'
    r = requests.get(url, headers={'Authorization': f'Bearer {token}'}, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def api_get_all(path: str, params: dict = None) -> list:
    """Get all pages from a paginated endpoint."""
    params = params or {}
    all_data = []
    page = 1
    while page < 100:  # safety limit
        params['page'] = page
        data = api_get(path, params)
        items = data.get('data', [])
        if not items:
            break
        all_data.extend(items)
        page += 1
    return all_data


# ─── Products & Categories ───


def sync_products():
    """Sync all products and categories from GoPos to pos_products table."""
    db = get_db()

    # Get categories
    cats_data = api_get('categories')
    categories = {c['id']: c['name'] for c in cats_data.get('data', [])}
    log.info(f'GoPos categories: {categories}')

    # Get all products
    items = api_get_all('items')
    log.info(f'GoPos products: {len(items)}')

    # Ensure pos_products table exists
    db.execute('''
        CREATE TABLE IF NOT EXISTS pos_products (
            product_name TEXT PRIMARY KEY,
            pos_kind TEXT NOT NULL DEFAULT 'unclassified',
            resale_ingredient_id INTEGER,
            category TEXT,
            classified_at TEXT,
            classified_by TEXT,
            gopos_item_id INTEGER,
            gopos_price REAL,
            gopos_group_id INTEGER,
            FOREIGN KEY (resale_ingredient_id) REFERENCES ingredients(id)
        )
    ''')

    now = datetime.now().isoformat()
    new_count = 0
    updated_count = 0

    for item in items:
        name = item.get('name', '').strip()
        if not name:
            continue

        category = categories.get(item.get('category_id'), None)
        price = item.get('price', {}).get('amount', 0)
        item_id = item.get('id')

        existing = db.execute('SELECT pos_kind, category FROM pos_products WHERE product_name = ?', (name,)).fetchone()

        if existing:
            # Update category and price from API (don't override manual pos_kind)
            db.execute('''
                UPDATE pos_products SET category = ?, gopos_item_id = ?, gopos_price = ?, gopos_group_id = ?
                WHERE product_name = ?
            ''', (category, item_id, price, item.get('item_group_id'), name))
            updated_count += 1
        else:
            # Auto-classify based on category
            pos_kind = _guess_pos_kind(category)
            db.execute('''
                INSERT INTO pos_products (product_name, pos_kind, category, classified_at, classified_by, gopos_item_id, gopos_price, gopos_group_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (name, pos_kind, category, now, 'api', item_id, price, item.get('item_group_id')))
            new_count += 1

    db.commit()
    db.close()
    log.info(f'Products sync: {new_count} new, {updated_count} updated')
    return {'new': new_count, 'updated': updated_count, 'total': len(items)}


def _guess_pos_kind(category: str) -> str:
    """Guess pos_kind from GoPos category."""
    if not category:
        return 'unclassified'
    resale_categories = {'Beans', 'Ice Cream', 'Soft Drinks', 'Brewware'}
    if category in resale_categories:
        return 'resale'
    elif category in {'Coffee', 'Kitchen', 'Bakery', 'Bar'}:
        return 'prepared'
    return 'unclassified'


# ─── Sales Sync ───


def sync_orders_for_date(date_str: str) -> int:
    """Sync all closed orders for a given date into sales table.

    Uses closed_at filter with GoPos business day (06:00 → next day 05:00).
    Returns number of products imported.
    """
    from datetime import datetime, timedelta

    db = get_db()

    # GoPos business day: 06:00 → next day 05:00
    next_day = (datetime.strptime(date_str, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
    closed_from = f'{date_str}T06:00:00'
    closed_to = f'{next_day}T05:00:00'

    # Get all closed orders with product names
    all_orders = []
    page = 0
    while page < 500:  # safety limit (~10k orders max)
        data = api_get('orders', {
            'closed_at_from': closed_from,
            'closed_at_to': closed_to,
            'status': 'CLOSED',
            'include': 'items,items.product',
            'size': 100,
            'page': page,
        })
        orders = data.get('data', [])
        if not orders:
            break
        all_orders.extend(orders)
        page += 1

    log.info(f'Got {len(all_orders)} orders for {date_str}')

    # Aggregate sales by product name
    sales_agg = {}  # product_name → {quantity, total_money, net_total, discount}
    for order in all_orders:
        for item in order.get('items', []):
            if item.get('status') != 'ACTIVE':
                continue
            name = item.get('name')
            if not name:
                continue

            qty = item.get('quantity', 1)
            total = item.get('total_price', {}).get('amount', 0)
            sub_total = item.get('sub_total_price', {}).get('amount', 0)
            # sub_total_price = price after discount, total_price = original
            # In GoPos: sub_total ≤ total (sub_total is after promotions)
            discount = total - sub_total if total > sub_total else 0
            net = sub_total  # net = after discount

            if name not in sales_agg:
                sales_agg[name] = {'quantity': 0, 'total_money': 0, 'net_total': 0, 'discount': 0}
            sales_agg[name]['quantity'] += qty
            sales_agg[name]['total_money'] += total
            sales_agg[name]['net_total'] += net
            sales_agg[name]['discount'] += discount

    # Upsert into sales table
    for product_name, s in sales_agg.items():
        db.execute('''
            INSERT INTO sales (date, product_name, quantity, total_money, net_total, discount, net_profit)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, product_name) DO UPDATE SET
                quantity=excluded.quantity,
                total_money=excluded.total_money,
                net_total=excluded.net_total,
                discount=excluded.discount,
                net_profit=excluded.net_profit
        ''', (date_str, product_name, s['quantity'], s['total_money'],
              s['net_total'], s['discount'], s['net_total']))

    db.commit()
    db.close()
    log.info(f'Imported {len(sales_agg)} products for {date_str} ({len(all_orders)} orders)')
    return len(sales_agg)


# ─── High-level sync functions ───


def sync_today():
    """Sync today's sales + products."""
    sync_id = _start_sync_log('API sync today')
    try:
        sync_products()
        today = datetime.now().strftime('%Y-%m-%d')
        count = sync_orders_for_date(today)
        _deduct_inventory(today)
        _finish_sync_log(sync_id, 'done', f'Synced {today}: {count} products via API')
        return count
    except Exception as e:
        _finish_sync_log(sync_id, 'error', str(e)[:200])
        raise


def sync_date(date_str: str):
    """Sync a single date."""
    sync_id = _start_sync_log(f'API sync {date_str}')
    try:
        sync_products()
        count = sync_orders_for_date(date_str)
        _deduct_inventory(date_str)
        _finish_sync_log(sync_id, 'done', f'Synced {date_str}: {count} products via API')
        return count
    except Exception as e:
        _finish_sync_log(sync_id, 'error', str(e)[:200])
        raise


def sync_range(date_from: str, date_to: str):
    """Sync a range of dates."""
    sync_id = _start_sync_log(f'API sync {date_from} → {date_to}')
    try:
        sync_products()
        start = datetime.strptime(date_from, '%Y-%m-%d')
        end = datetime.strptime(date_to, '%Y-%m-%d')
        total = 0
        current = start
        while current <= end:
            date_str = current.strftime('%Y-%m-%d')
            count = sync_orders_for_date(date_str)
            _deduct_inventory(date_str)
            total += count
            current += timedelta(days=1)
        _finish_sync_log(sync_id, 'done', f'Synced {date_from} → {date_to}: {total} products via API')
        return total
    except Exception as e:
        _finish_sync_log(sync_id, 'error', str(e)[:200])
        raise


def _deduct_inventory(date_str: str):
    """Deduct ingredients from inventory based on sales and recipes."""
    from app.gopos_sync import deduct_inventory
    deduct_inventory(date_str)


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


def sync_products_and_categories():
    """Alias for sync_products — called from gopos_sync.py."""
    return sync_products()


def get_selling_prices() -> dict:
    """Get current selling prices from GoPos API."""
    items = api_get_all('items')
    return {item['name']: item.get('price', {}).get('amount', 0) for item in items}


def register_webhook(callback_url: str):
    """Register a webhook in GoPos to receive real-time notifications."""
    token = get_token()
    url = f'{GOPOS_BASE_URL}/api/v3/{GOPOS_ORG_ID}/webhooks'

    # Check if webhook already registered
    r = requests.get(url, headers={'Authorization': f'Bearer {token}'}, timeout=15)
    if r.status_code == 200:
        existing = r.json().get('data', [])
        for wh in existing:
            if wh.get('url') == callback_url:
                log.info(f'Webhook already registered: {callback_url}')
                return wh

    # Register new webhook
    payload = {
        'url': callback_url,
        'name': 'The Frame Manager sync',
        'resource_type': 'ORDER',
    }
    r = requests.post(url, headers={
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    }, json=payload, timeout=15)

    if r.status_code in (200, 201):
        log.info(f'Webhook registered: {callback_url}')
        return r.json()
    else:
        log.warning(f'Webhook registration failed: {r.status_code} {r.text[:200]}')
        return None
