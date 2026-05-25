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

# API credentials
GOPOS_CLIENT_ID = os.getenv('GOPOS_CLIENT_ID', 'bf2a2942-ece7-4dcb-aed2-c2586e2d2bca')
GOPOS_CLIENT_SECRET = os.getenv('GOPOS_CLIENT_SECRET', '454d0ef9-90c4-4e94-b6a6-f14f9547564e')
GOPOS_ORG_ID = os.getenv('GOPOS_ORG_ID', '9388')
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
                UPDATE pos_products SET category = ?, gopos_item_id = ?, gopos_price = ?
                WHERE product_name = ?
            ''', (category, item_id, price, name))
            updated_count += 1
        else:
            # Auto-classify based on category
            pos_kind = _guess_pos_kind(category)
            db.execute('''
                INSERT INTO pos_products (product_name, pos_kind, category, classified_at, classified_by, gopos_item_id, gopos_price)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (name, pos_kind, category, now, 'api', item_id, price))
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
# NOTE: GoPos API does not provide product-level daily sales data.
# Order items don't include product names, and reports ignore date filters.
# We keep Playwright CSV export for daily sales sync until GoPos fixes this.
# This module handles: products catalog, categories, selling prices.


def get_selling_prices() -> dict:
    """Get current selling prices from GoPos API."""
    items = api_get_all('items')
    return {item['name']: item.get('price', {}).get('amount', 0) for item in items}


# ─── High-level sync functions ───


def sync_products_and_categories():
    """Sync products and categories from GoPos API. Called during daily sync."""
    return sync_products()


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
