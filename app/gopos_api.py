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
    global _tax_rates_cache
    _tax_rates_cache = None  # catalog changed → re-read VAT rates on next order sync
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

_tax_rates_cache: Optional[dict] = None


def get_item_tax_rates(refresh: bool = False) -> dict:
    """{catalog item_id: VAT rate in %} — from GoPos items.tax_id → taxes.rate. Cached per process."""
    global _tax_rates_cache
    if _tax_rates_cache is None or refresh:
        rates = {t['id']: t.get('rate', 0) or 0 for t in api_get('taxes').get('data', [])}
        _tax_rates_cache = {i['id']: rates.get(i.get('tax_id'), 0) for i in api_get_all('items')}
    return _tax_rates_cache


def item_amounts(item: dict, order: dict, tax_rates: dict) -> dict:
    """Money fields for one order item, in the same meaning the sales table always had
    (and the old GoPos export used):

      total_money — paid, gross (incl. VAT), after discount
      net_total   — paid, net (excl. VAT)
      discount    — how much was discounted (gross)
      net_profit  — gross price before discount (legacy name, kept for consistency)

    NB: in the GoPos API `sub_total_price` is the price BEFORE discount and
    `total_price` the price AFTER it.
    """
    paid = (item.get('total_price') or {}).get('amount', 0)
    before = (item.get('sub_total_price') or {}).get('amount', 0)
    rate = tax_rates.get(item.get('item_id'))
    if rate is not None:
        net = paid / (1 + rate / 100)
    else:  # unknown catalog item: use the order's own net/gross ratio
        o_gross = (order.get('total_price') or {}).get('amount', 0)
        o_net = (order.get('total_price_net') or {}).get('amount', 0)
        net = paid * (o_net / o_gross) if o_gross else paid
    return {
        'total_money': paid,
        'net_total': round(net, 2),
        'discount': max(before - paid, 0),
        'net_profit': max(before, paid),
    }


def order_item_amounts(order: dict, tax_rates: dict) -> list:
    """[(item, amounts)] for the order's ACTIVE items, with net values that add up to the
    order's own total_price_net: items whose VAT rate is unknown (variants/modifiers not in
    the catalog) share whatever net is left after the known items, pro rata to what was paid.
    """
    active = [it for it in order.get('items', []) if it.get('status') == 'ACTIVE']
    rows = [(it, item_amounts(it, order, tax_rates)) for it in active]
    unknown = [(it, a) for it, a in rows if it.get('item_id') not in tax_rates]
    order_net = (order.get('total_price_net') or {}).get('amount')
    if unknown and order_net is not None:
        known_net = sum(a['net_total'] for it, a in rows if it.get('item_id') in tax_rates)
        unknown_paid = sum(a['total_money'] for _, a in unknown)
        residual = max(order_net - known_net, 0)
        for _, a in unknown:
            a['net_total'] = round(residual * a['total_money'] / unknown_paid, 2) if unknown_paid else 0
    return rows


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

    tax_rates = get_item_tax_rates()

    # Aggregate sales by product name
    sales_agg = {}  # product_name → {quantity, total_money, net_total, discount, net_profit}
    for order in all_orders:
        for item, a in order_item_amounts(order, tax_rates):
            name = item.get('name')
            if not name:
                continue

            s = sales_agg.setdefault(name, {'quantity': 0, 'total_money': 0, 'net_total': 0,
                                            'discount': 0, 'net_profit': 0})
            s['quantity'] += item.get('quantity', 1)
            for k, v in a.items():
                s[k] += v

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
              round(s['net_total'], 2), s['discount'], s['net_profit']))

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


def resync_sales(date_from: str, date_to: str) -> int:
    """Re-import sales money fields for a date range WITHOUT touching inventory.

    Safe to re-run: rows are upserted per (date, product). Unlike sync_range() it does
    not call deduct_inventory, so stock is not deducted a second time.
    """
    sync_id = _start_sync_log(f'API resync sales {date_from} → {date_to}')
    try:
        current = datetime.strptime(date_from, '%Y-%m-%d')
        end = datetime.strptime(date_to, '%Y-%m-%d')
        days = 0
        while current <= end:
            sync_orders_for_date(current.strftime('%Y-%m-%d'))
            days += 1
            current += timedelta(days=1)
        _finish_sync_log(sync_id, 'done', f'Resynced sales {date_from} → {date_to}: {days} days via API')
        return days
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


def fetch_closed_orders(date_str: str, include: str) -> list:
    """All closed orders of a GoPos business day (06:00 → next day 05:00)."""
    next_day = (datetime.strptime(date_str, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
    result = []
    page = 0
    while page < 50:  # GoPos rejects page >= 50 (422)
        orders = api_get('orders', {
            'closed_at_from': f'{date_str}T06:00:00',
            'closed_at_to': f'{next_day}T05:00:00',
            'status': 'CLOSED',
            'include': include,
            'size': 100,
            'page': page,
        }).get('data', [])
        if not orders:
            break
        result.extend(orders)
        page += 1
    return result


def shift_staff(orders: list) -> list:
    """Who worked, from the employee logged in on each order.

    Returns [{'name', 'orders', 'first', 'last'}] sorted by first order time,
    'first'/'last' = HH:MM of the first/last closed order.
    """
    staff = {}
    for o in orders:
        name = (o.get('employee') or {}).get('name') or '—'
        t = (o.get('closed_at') or '')[11:16]
        s = staff.setdefault(name, {'name': name, 'orders': 0, 'first': t, 'last': t})
        s['orders'] += 1
        if t and (not s['first'] or t < s['first']):
            s['first'] = t
        if t > s['last']:
            s['last'] = t
    return sorted(staff.values(), key=lambda s: s['first'] or '99:99')


def discounted_items(orders: list) -> dict:
    """Items sold under each discount: {discount_name_lower: {item_name: {'qty', 'value', 'unit_price'}}}.

    Only items whose price was actually reduced count. 'value' = zł given away
    (sub_total_price − total_price; in GoPos sub_total is the pre-discount price).
    """
    result = {}
    for o in orders:
        names = {(p.get('name') or '').strip().lower() for p in o.get('promotions') or []}
        names.discard('')
        if not names:
            continue
        for it in o.get('items', []):
            if it.get('status') != 'ACTIVE':
                continue
            full = (it.get('sub_total_price') or {}).get('amount', 0)
            paid = (it.get('total_price') or {}).get('amount', 0)
            if paid >= full:
                continue
            item_name = it.get('name') or (it.get('product') or {}).get('name') or '?'
            for n in names:
                e = result.setdefault(n, {}).setdefault(
                    item_name, {'qty': 0, 'value': 0.0,
                                'unit_price': (it.get('base_unit_price') or {}).get('amount', 0)})
                e['qty'] += it.get('quantity', 1)
                e['value'] += full - paid
    return result


def get_shift_staff(date_str: str) -> list:
    """Who worked on a business day (see shift_staff)."""
    return shift_staff(fetch_closed_orders(date_str, 'employee'))


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
